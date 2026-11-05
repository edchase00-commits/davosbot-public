import io
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import review_validation as review


class ReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def make_archive(self, records):
        archive = self.base / "source.tar"
        with tarfile.open(archive, "w") as output:
            for name, body, kind in records:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(body)
                member.linkname = "../../outside"
                output.addfile(member, io.BytesIO(body))
        destination = self.base / "review"
        destination.mkdir(exist_ok=True)
        return archive, destination

    def test_snapshot_rejects_traversal_links_and_oversize(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("C:/escape", tarfile.REGTYPE), ("dir\\escape", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE)):
            with self.subTest(name=name):
                archive, destination = self.make_archive([(name, b"x", kind)])
                with self.assertRaises(review.ReviewError):
                    review.extract_snapshot(archive, destination)
        archive, destination = self.make_archive([("large", b"1234", tarfile.REGTYPE)])
        with patch.object(review, "MAX_ARCHIVE_BYTES", 3), self.assertRaises(review.ReviewError):
            review.extract_snapshot(archive, destination)

    def test_private_state_is_not_copied_from_committed_snapshot(self):
        private = [".env", "SOUL.md", "MEMORY.md", "gc_state.json", "davosbot.db",
                   "backups/private", "generated/image", "exports/private/log", ".work_bridge/state.json"]
        archive, destination = self.make_archive(
            [(name, b"synthetic-private", tarfile.REGTYPE) for name in private]
            + [("main.py", b"# source", tarfile.REGTYPE)])
        review.extract_snapshot(archive, destination)
        self.assertEqual(["main.py"], [path.name for path in destination.iterdir()])

    def test_lock_excludes_second_run_and_releases_without_deleting_file(self):
        path = self.base / "review.lock"
        with review.single_run(path):
            with self.assertRaisesRegex(review.ReviewError, "Another"):
                with review.single_run(path):
                    self.fail("Second lock should not succeed")
        self.assertTrue(path.exists())
        with review.single_run(path):
            pass

    def make_repo(self, *, failing=False):
        root = self.base / "source"
        root.mkdir()
        for directory in ("scripts", "tests", "davosbot"):
            (root / directory).mkdir()
        (root / "main.py").write_text("# synthetic source\n")
        (root / "davosbot" / "__init__.py").write_text("")
        (root / "tests" / "test_synthetic.py").write_text('''import os, unittest
from pathlib import Path
class Synthetic(unittest.TestCase):
    def test_isolation(self):
        self.assertIsNone(os.environ.get("REVIEW_PARENT_SECRET"))
        self.assertEqual("", os.environ["GEMINI_API_KEY"])
        self.assertEqual("1", os.environ["PYTHON_DOTENV_DISABLED"])
        self.assertNotEqual(Path.cwd(), Path(os.environ["HOME"]))
        self.assertFalse((Path.cwd() / ".env").exists())
        self.assertIn("Synthetic", Path(os.environ["SOUL_PATH"]).read_text())
''' + ("        self.fail('synthetic test failure')\n" if failing else ""))
        (root / "scripts" / "quality_check.py").write_text('''from pathlib import Path
assert not (Path.cwd() / "uncommitted-marker").exists()
assert not (Path.cwd() / ".git").exists()
print("synthetic guarded checks")
''')
        for args in (("init",), ("add", "."), ("-c", "user.name=Review Test", "-c", "user.email=review@example.invalid",
                                               "commit", "-m", "synthetic source")):
            subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
                           cwd=root, check=True, capture_output=True)
        # These files must not be read/exported by git archive.
        (root / ".env").write_text("synthetic-private")
        (root / "uncommitted-marker").write_text("uncommitted")
        return root

    def test_real_committed_snapshot_runs_full_compile_and_focused_with_no_parent_secrets(self):
        root = self.make_repo()
        def source_snapshot():
            # Git's background maintenance may create/remove its own lock files.
            # Check actual source contents and repository state, not those locks.
            files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts}
            git_state = [subprocess.check_output(["git", *args], cwd=root)
                         for args in (("rev-parse", "HEAD"), ("status", "--porcelain"))]
            return files, git_state
        before = source_snapshot()
        with patch.dict(os.environ, {"REVIEW_PARENT_SECRET": "synthetic", "GEMINI_API_KEY": "synthetic"}):
            self.assertEqual(0, review.run_review(root, temporary_root=self.base))
        self.assertEqual(before, source_snapshot())
        self.assertFalse(any(path.name.startswith("davosbot-review-") and path.is_dir() for path in self.base.iterdir()))

    def test_real_failure_does_not_report_a_pass(self):
        root = self.make_repo(failing=True)
        self.assertEqual(1, review.run_review(root, temporary_root=self.base))

    def test_full_discovery_rejects_empty_and_unittest_invisible_files(self):
        source = self.base / "tests"
        source.mkdir()
        for content in (None, "def test_invisible():\n    assert True\n"):
            if content:
                (source / "test_invisible.py").write_text(content)
            result = subprocess.run([review.sys.executable, "-c", review.FULL_TEST_CODE], cwd=self.base,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("RuntimeError", result.stderr)

    def test_real_phase_timeout_terminates_the_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            review._run_phase([review.sys.executable, "-c", "import time; time.sleep(30)"],
                              cwd=self.base, env=os.environ.copy(), timeout=0.2)

    def test_real_timeout_terminates_spawned_test_descendants(self):
        heartbeat = self.base / "heartbeat"
        grandchild = ("import pathlib,time\nend=time.monotonic()+30\n"
                      "while time.monotonic()<end:\n"
                      "    pathlib.Path('heartbeat').write_text(str(time.monotonic_ns()))\n"
                      "    time.sleep(0.05)\n")
        child = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(grandchild)
                 + "]); time.sleep(30)")
        with self.assertRaises(subprocess.TimeoutExpired):
            review._run_phase([review.sys.executable, "-c", child], cwd=self.base,
                              env=os.environ.copy(), timeout=0.8)
        self.assertTrue(heartbeat.exists(), "Grandchild must actually start for this test to cover tree termination")
        last_heartbeat = heartbeat.read_text()
        time.sleep(0.3)
        self.assertEqual(last_heartbeat, heartbeat.read_text(),
                         "Test descendants must stop before timeout cleanup returns")

    def test_production_temporary_path_fails_before_any_subprocess(self):
        root = self.base / "production"
        root.mkdir()
        with patch.object(review.subprocess, "run") as run:
            with self.assertRaisesRegex(review.ReviewError, "outside"):
                review.run_review(root, temporary_root=root)
        run.assert_not_called()

    def test_timeout_has_nonzero_exit_and_releases_lock(self):
        with patch.object(review, "ROOT", self.base), patch.object(
            review.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 1),
        ):
            self.assertEqual(124, review.main(["--timeout", "1"]))


if __name__ == "__main__":
    unittest.main()
