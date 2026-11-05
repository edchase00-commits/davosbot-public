"""The standalone service cannot replace the grant-bound runtime package."""

from contextlib import closing
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from davosbot import platinum_emerald_watch as watch


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts/run_standalone_platinum_watch.py"
CHILD = r'''
import hashlib, importlib.util, json, pathlib, sys
from unittest.mock import patch
spec = importlib.util.spec_from_file_location("bootstrap", sys.argv[1])
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
root, artifact = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
hashes = {p: hashlib.sha256((artifact / p).read_bytes()).hexdigest() for p in b.WATCH_FILES}
mode = sys.argv[4]
if mode == "bad_hash":
    hashes["davosbot/platinum_emerald_watch.py"] = "0" * 64
def git(*args, **kwargs):
    command = args[0]
    value = "wrong" if mode == "bad_revision" else "reviewed"
    if "status" in command:
        value = " M davosbot/config.py" if mode == "dirty" else ""
    return type("Result", (), {"stdout": value})()
try:
    with patch.object(b.subprocess, "run", git):
        entry = b._load_entrypoint(root, artifact, "reviewed", hashes)
    import davosbot
    print(json.dumps({"result": entry.main(), "paths": list(davosbot.__path__),
                      "artifact_on_path": str(artifact) in sys.path,
                      "bytecode_disabled": sys.dont_write_bytecode}))
except b.BootstrapError as exc:
    print(str(exc))
'''


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "runtime"
        self.artifact = Path(self.temp.name).resolve() / "artifact"
        package = self.root / "davosbot"
        package.mkdir(parents=True)
        for name in ("__init__", "runtime_locks", "db", "reminder_tools"):
            (package / (name + ".py")).write_text("ORIGIN = 'production'\n")
        (package / "config.py").write_text("from pathlib import Path\nPROJECT_ROOT = Path(__file__).resolve().parents[1]\n")
        (self.artifact / "davosbot").mkdir(parents=True)
        (self.artifact / "scripts").mkdir()
        (self.artifact / "davosbot/platinum_emerald_watch.py").write_text("ORIGIN = 'reviewed watch'\n")
        (self.artifact / "scripts/check_platinum_emerald.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
            "from davosbot import platinum_emerald_watch, reminder_tools\n"
            "def main(): return [platinum_emerald_watch.ORIGIN, reminder_tools.ORIGIN]\n")
        # A complete, hostile external package must never shadow the pinned package.
        (self.artifact / "davosbot/__init__.py").write_text("raise RuntimeError('overlay executed')\n")
        (self.artifact / "davosbot/reminder_tools.py").write_text("raise RuntimeError('overlay executed')\n")

    def run_child(self, mode="valid"):
        result = subprocess.run([sys.executable, "-I", "-B", "-c", CHILD, str(BOOTSTRAP),
                                 str(self.root), str(self.artifact), mode],
                                capture_output=True, text=True, check=True, timeout=20)
        return result.stdout.strip()

    def test_only_watch_is_external_and_entrypoint_cannot_overlay_package(self):
        result = json.loads(self.run_child())
        self.assertEqual(result["result"], ["reviewed watch", "production"])
        self.assertEqual(result["paths"], [str(self.root / "davosbot")])
        self.assertFalse(result["artifact_on_path"])
        self.assertTrue(result["bytecode_disabled"])
        self.assertFalse(list(Path(self.temp.name).rglob("*.pyc")))

    def test_hash_revision_and_tracked_change_fail_closed(self):
        for mode, error in (("bad_hash", "watch_artifact_mismatch"),
                            ("bad_revision", "runtime_revision_mismatch"),
                            ("dirty", "runtime_tracked_files_changed")):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_child(mode), error)

    def test_changed_package_search_path_is_rejected(self):
        (self.root / "davosbot/__init__.py").write_text("__path__.append('/unexpected')\n")
        self.assertEqual(self.run_child(), "runtime_package_path_mismatch")

    def test_changed_support_module_origin_is_rejected(self):
        (self.root / "davosbot/db.py").write_text("__file__ = '/unexpected/db.py'\n")
        self.assertEqual(self.run_child(), "runtime_module_origin_mismatch")

    def test_changed_project_root_is_rejected(self):
        (self.root / "davosbot/config.py").write_text("PROJECT_ROOT = '/unexpected'\n")
        self.assertEqual(self.run_child(), "runtime_project_root_mismatch")


class NativeReminderIntegrationTests(unittest.TestCase):
    def test_actual_native_helpers_use_temp_sqlite_and_duplicate_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, chat_id TEXT, message TEXT, "
                             "due_ts TEXT, origin_chat_id TEXT, sent INTEGER DEFAULT 0, send_attempts INTEGER DEFAULT 0)")
            # Load real helper/db/lock source into a fixture-only package with no live config.
            name = "platinum_native_fixture"
            package = ModuleType(name)
            package.__path__ = [str(ROOT / "davosbot")]
            config = SimpleNamespace(BOT_DB_PATH=path, OWNER_ID="synthetic-owner")
            with patch.dict(sys.modules, {name: package, name + ".config": config}):
                native = importlib.import_module(name + ".reminder_tools")
                with patch.dict(sys.modules, {"davosbot.config": config, "davosbot.reminder_tools": native}):
                    queue, pending = watch.native_reminders()
                    now = datetime.now(timezone.utc)
                    manifest = {"game": "Platinum Emerald", "version": "test", "output": {"bytes": 32, "sha1": "b" * 40}}
                    preview = watch.inspect_release("<h1>Platinum Emerald</h1><p>Status: development preview.</p>", manifest, now.date())
                    release = watch.inspect_release("<h1>Platinum Emerald</h1><p>Status: full release.</p>", manifest, now.date())
                    state = Path(temp) / "state.json"
                    self.assertEqual(watch.process_observation(state, preview, now, queue, pending), "unchanged")
                    self.assertEqual(native._list_reminders("synthetic-owner"), "No pending reminders.")
                    self.assertEqual(watch.process_observation(state, release, now, queue, pending), "queued")
                    self.assertEqual(watch.process_observation(state, release, now, queue, pending), "unchanged")
                    with closing(sqlite3.connect(path)) as conn:
                        rows = conn.execute("SELECT chat_id, origin_chat_id, message FROM reminders").fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0][:2], ("synthetic-owner", "synthetic-owner"))
                    self.assertTrue(pending(rows[0][2]))


if __name__ == "__main__":
    unittest.main()
