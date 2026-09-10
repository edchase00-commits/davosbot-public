"""Client preparation is local, explicit and cannot silently downsize or upload."""

import base64
from contextlib import redirect_stdout
from io import StringIO, BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_work_image as prepare
from davosbot import work_image_input as images
from test_work_image_input_permissions import CANARY, Image, png


@unittest.skipIf(Image is None, "optional Pillow decoder not installed")
class PrepareWorkImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "selected.png"
        self.source.write_bytes(png())

    def test_default_preserves_dimensions_pixels_and_strips_metadata_without_changing_original(self):
        original = self.source.read_bytes()
        prepared = prepare.prepare_image(self.source)
        self.assertEqual((32, 16), (prepared["width"], prepared["height"]))
        raw = base64.b64decode(prepared["content"])
        self.assertNotIn(CANARY.encode(), raw)
        with Image.open(BytesIO(raw)) as decoded:
            self.assertEqual((255, 0, 0), decoded.getpixel((0, 0)))
        self.assertEqual(original, self.source.read_bytes())
        blob = {"sha": prepared["image_blob_sha"], "size": prepared["byte_count"],
                "encoding": prepared["encoding"], "content": prepared["content"]}
        self.assertEqual(raw, images._blob_bytes(blob, prepared))

    def test_oversized_pixels_require_explicit_resize_and_resize_never_enlarges(self):
        self.source.write_bytes(png(4097, 1))
        with self.assertRaisesRegex(ValueError, "^explicit_resize_required$"):
            prepare.prepare_image(self.source)
        resized = prepare.prepare_image(self.source, maximum_dimension=1024)
        self.assertEqual((1024, 1), (resized["width"], resized["height"]))
        self.source.write_bytes(png())
        unchanged = prepare.prepare_image(self.source, maximum_dimension=1024)
        self.assertEqual((32, 16), (unchanged["width"], unchanged["height"]))

    def test_jpeg_output_requires_explicit_lossy_option(self):
        self.assertEqual("image/png", prepare.prepare_image(self.source)["mime_type"])
        result = prepare.prepare_image(self.source, jpeg_quality=85)
        self.assertEqual("image/jpeg", result["mime_type"])
        self.assertTrue(base64.b64decode(result["content"]).startswith(b"\xff\xd8"))

    def test_local_size_limit_and_runtime_output_limit_fail_closed(self):
        with patch.object(prepare, "MAX_LOCAL_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "^local_image_size_limit$"):
                prepare.prepare_image(self.source)
        with patch.object(prepare, "MAX_IMAGE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "^explicit_resize_or_jpeg_compression_required$"):
                prepare.prepare_image(self.source)

    def test_cli_writes_new_private_json_without_printing_image_data(self):
        target = self.root / "private-upload.json"
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(0, prepare.main([str(self.source), "--output", str(target)]))
        result = json.loads(target.read_text())
        self.assertNotIn(result["content"], stdout.getvalue())
        self.assertNotIn(str(self.source), stdout.getvalue())
        self.assertNotIn(CANARY, stdout.getvalue())
        if os.name != "nt":
            self.assertEqual(0o600, target.stat().st_mode & 0o777)

    def test_existing_output_and_original_are_never_overwritten(self):
        original = self.source.read_bytes()
        with self.assertRaises(SystemExit):
            prepare.main([str(self.source), "--output", str(self.source)])
        self.assertEqual(original, self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
