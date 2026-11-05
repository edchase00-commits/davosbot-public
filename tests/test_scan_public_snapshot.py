import tempfile
import unittest
from pathlib import Path

from scripts import clean_public_snapshot, scan_public_snapshot


class PublicSnapshotScanTests(unittest.TestCase):
    def test_clean_snapshot_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("Sanitized bot snapshot\n", encoding="utf-8")

            self.assertEqual([], scan_public_snapshot.scan_snapshot(root))

    def test_private_marker_is_reported_without_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = "Et" + "han"
            (root / "README.md").write_text(f"Private bot for {marker}\n", encoding="utf-8")

            findings = scan_public_snapshot.scan_snapshot(root)

            self.assertEqual(1, len(findings))
            self.assertEqual("README.md", findings[0].path)
            self.assertIn("owner_first_name", findings[0].label)
            self.assertNotIn(marker, findings[0].format())

    def test_tests_are_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            marker = "Et" + "han"
            (tests / "fixture.py").write_text(f'PRIVATE = "{marker}"\n', encoding="utf-8")

            self.assertEqual([], scan_public_snapshot.scan_snapshot(root))

            findings = scan_public_snapshot.scan_snapshot(root, include_tests=True)

            self.assertEqual(1, len(findings))
            self.assertEqual("tests/fixture.py", findings[0].path)

    def test_private_runtime_files_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("KEY=value\n", encoding="utf-8")
            generated = root / "generated"
            generated.mkdir()
            (generated / "image.png").write_bytes(b"png")

            findings = scan_public_snapshot.scan_snapshot(root)
            formatted = "\n".join(finding.format() for finding in findings)

            self.assertIn(".env: private runtime file present", formatted)
            self.assertIn("generated/image.png: private/generated path present", formatted)

    def test_public_cleanup_removes_runtime_artifacts_before_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("Sanitized bot snapshot\n", encoding="utf-8")
            (root / "davosbot.db").write_bytes(b"sqlite")
            backups = root / "backups"
            backups.mkdir()
            (backups / "snapshot.db").write_bytes(b"sqlite")
            cache = root / "davosbot" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "main.pyc").write_bytes(b"cache")

            removed = clean_public_snapshot.clean_snapshot(root)

            self.assertIn("davosbot.db", removed)
            self.assertIn("backups/", removed)
            self.assertIn("davosbot/__pycache__/", removed)
            self.assertEqual([], scan_public_snapshot.scan_snapshot(root))


if __name__ == "__main__":
    unittest.main()
