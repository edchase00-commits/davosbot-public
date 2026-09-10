import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import tools
class FileWriteSafetyTests(unittest.TestCase):
    def test_project_write_does_not_auto_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.txt"
            with (
                patch.object(tools, "_PROJECT_DIR", str(Path(tmp))),
                patch.object(tools, "_auto_push") as auto_push,
            ):
                reply = tools._write_file(str(target), "hello")

        auto_push.assert_not_called()
        self.assertIn("Auto-push is disabled", reply)

    def test_auto_push_is_disabled_no_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "personalities" / "test.md"
            with (
                patch.object(tools, "_PROJECT_DIR", str(Path(tmp))),
                patch.object(tools.subprocess, "run") as run,
            ):
                tools._auto_push(str(target))

        run.assert_not_called()

    def test_edit_persona_does_not_auto_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persona = root / "personalities" / "test.md"
            persona.parent.mkdir()
            persona.write_text("old persona", encoding="utf-8")
            with (
                patch.object(tools, "_PROJECT_DIR", str(root)),
                patch.object(tools, "_gemini_rewrite", return_value="new persona"),
                patch.object(tools, "_auto_push") as auto_push,
            ):
                reply = tools._edit_persona("test", "make it sharper")

            auto_push.assert_not_called()
            self.assertEqual("new persona", persona.read_text(encoding="utf-8"))
            self.assertIn("Auto-push is disabled", reply)
            self.assertIn("normal repo workflow", reply)

    def test_create_persona_does_not_auto_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "personalities").mkdir()
            with (
                patch.object(tools, "_PROJECT_DIR", str(root)),
                patch.object(tools, "_gemini_rewrite", return_value="fresh persona"),
                patch.object(tools, "_auto_push") as auto_push,
            ):
                reply = tools._create_persona("fresh", "fresh personality")

            created = root / "personalities" / "fresh.md"

            auto_push.assert_not_called()
            self.assertEqual("fresh persona", created.read_text(encoding="utf-8"))
            self.assertIn("Auto-push is disabled", reply)
            self.assertIn("persona fresh", reply)


if __name__ == "__main__":
    unittest.main()
