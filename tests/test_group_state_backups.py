import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import group_chat


class GroupStateBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "gc_state.json"
        self.backups_dir = self.root / "backups"
        self.state_patch = patch.object(group_chat, "_STATE_FILE", self.state_path)
        self.backups_patch = patch.object(group_chat, "_BACKUPS_DIR", self.backups_dir)
        self.state_patch.start()
        self.backups_patch.start()
        group_chat._state = group_chat._fresh_state()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.backups_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_save_backs_up_existing_state_before_overwrite(self):
        old_state = {
            "enabled_chats": ["old-chat"],
            "approved_users": [],
            "personas": {},
            "group_personas": {},
        }
        self.state_path.write_text(json.dumps(old_state), encoding="utf-8")

        group_chat.enable_gc("new-chat")

        backups = list(self.backups_dir.glob("gc_state_*.json"))
        self.assertEqual(1, len(backups))
        self.assertEqual(old_state, json.loads(backups[0].read_text(encoding="utf-8")))
        saved_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(["old-chat", "new-chat"], saved_state["enabled_chats"])
        self.assertFalse(self.state_path.with_name("gc_state.json.tmp").exists())

    def test_first_save_does_not_create_empty_backup(self):
        group_chat.enable_gc("first-chat")

        self.assertEqual([], list(self.backups_dir.glob("gc_state_*.json")))
        saved_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(["first-chat"], saved_state["enabled_chats"])


if __name__ == "__main__":
    unittest.main()
