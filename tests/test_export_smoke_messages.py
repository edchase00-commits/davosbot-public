import tempfile
import unittest
from pathlib import Path

from scripts import export_smoke_messages


class ExportSmokeMessagesTests(unittest.TestCase):
    def test_decode_attributed_body_extracts_message_text(self):
        blob = (
            b"streamtyped\x00NSAttributedString\x00NSObject\x00NSString\x00"
            b"Model commands:\n  model status\n  model options\x00NSDictionary\x00"
        )

        text = export_smoke_messages.decode_attributed_body(blob)

        self.assertIn("Model commands:", text)
        self.assertIn("model options", text)
        self.assertNotIn("NSAttributedString", text)

    def test_format_redacts_handles_and_tokens(self):
        text = export_smoke_messages.redact_text("send +13369700454 token=sk-abc123456789")

        self.assertIn("+13...54", text)
        self.assertIn("token=[redacted]", text)
        self.assertNotIn("sk-abc", text)

    def test_write_snapshot_creates_private_smoke_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            stable, stamped = export_smoke_messages.write_snapshot("smoke text", Path(tmp))

            self.assertEqual("smoke_messages.md", stable.name)
            self.assertTrue(stable.exists())
            self.assertTrue(stamped.exists())
            self.assertIn("smoke text", stable.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
