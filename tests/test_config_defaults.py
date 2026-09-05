import os
import unittest
from unittest.mock import patch

from davosbot.config import _str_env


class ConfigDefaultTests(unittest.TestCase):
    def test_blank_string_env_uses_default(self):
        with patch.dict(os.environ, {"DAVOSBOT_TEST_MODEL": ""}, clear=False):
            self.assertEqual("fallback-model", _str_env("DAVOSBOT_TEST_MODEL", "fallback-model"))

    def test_present_string_env_is_stripped(self):
        with patch.dict(os.environ, {"DAVOSBOT_TEST_MODEL": "  configured-model  "}, clear=False):
            self.assertEqual("configured-model", _str_env("DAVOSBOT_TEST_MODEL", "fallback-model"))


if __name__ == "__main__":
    unittest.main()
