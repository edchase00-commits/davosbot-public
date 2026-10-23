import os
import unittest
from unittest.mock import patch

from davosbot import config


class ConfigModelTests(unittest.TestCase):
    def test_stale_gemini_image_env_uses_current_default(self):
        with patch.dict(os.environ, {"GEMINI_IMAGE_MODEL": "gemini-2.5-flash-image"}):
            self.assertEqual(
                "gemini-3.1-flash-image",
                config._str_env_unless_legacy(
                    "GEMINI_IMAGE_MODEL",
                    "gemini-3.1-flash-image",
                    {"gemini-2.5-flash-image"},
                ),
            )


if __name__ == "__main__":
    unittest.main()
