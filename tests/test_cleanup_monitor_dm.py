import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cleanup_monitor_dm", ROOT / "scripts" / "cleanup_monitor_dm.py")
cleanup_monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup_monitor
SPEC.loader.exec_module(cleanup_monitor)


class CleanupMonitorDmTests(unittest.TestCase):
    def test_sends_once_then_throttles_unchanged_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            state_path = Path(tmp) / "state.json"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute("INSERT INTO change_log (request) VALUES (?)", ("docs cleanup",))
                conn.commit()
            finally:
                conn.close()

            sent = []
            with patch.object(cleanup_monitor.commands, "BOT_DB_PATH", db_path), \
                 patch.object(cleanup_monitor, "OWNER_ID", "+15550000001"), \
                 patch.object(cleanup_monitor, "send_message", lambda dest, msg: sent.append((dest, msg)) or True):
                first = cleanup_monitor.maybe_send_cleanup_dm(state_path=state_path, repeat_hours=6)
                second = cleanup_monitor.maybe_send_cleanup_dm(state_path=state_path, repeat_hours=6)

            self.assertEqual("sent", first)
            self.assertEqual("unchanged", second)
            self.assertEqual(1, len(sent))
            self.assertIn("Davos cleanup monitor: GREEN 1", sent[0][1])
            self.assertIn("yes fix", sent[0][1])
            self.assertIn("run Codex cleanup on the Mini now", sent[0][1])
            self.assertIn("master prompt", sent[0][1])


if __name__ == "__main__":
    unittest.main()
