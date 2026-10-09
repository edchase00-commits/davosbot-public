import unittest

from davosbot import failure_copy


_BACKEND_LEAK_TERMS = (
    "gemini",
    "google",
    "ollama",
    "openai",
    "503",
    "502",
    "504",
    "429",
    "timeout",
    "connection",
    "http",
)


class FailureCopyTests(unittest.TestCase):
    def assert_provider_neutral(self, text: str) -> None:
        lower = text.lower()
        for term in _BACKEND_LEAK_TERMS:
            self.assertNotIn(term, lower)

    def test_humanize_transient_error_passthrough(self):
        self.assertIsNone(failure_copy.humanize_transient_error(None))
        self.assertEqual("normal reply", failure_copy.humanize_transient_error("normal reply"))

    def test_humanize_transient_error_is_provider_neutral(self):
        sentinels = [
            "__transient_error__:HTTP 503",
            "__transient_error__:HTTP 502",
            "__transient_error__:HTTP 504",
            "__transient_error__:HTTP 429",
            "__transient_error__:Timeout",
            "__transient_error__:ConnectionError",
            "__transient_error__:Google backend exploded",
        ]
        for sentinel in sentinels:
            with self.subTest(sentinel=sentinel):
                reply = failure_copy.humanize_transient_error(sentinel)
                self.assertIsInstance(reply, str)
                self.assert_provider_neutral(reply)

    def test_roast_fallback_no_backend_name(self):
        reply = failure_copy.harmless_roast_fallback("you're a bum")

        self.assertIsInstance(reply, str)
        self.assert_provider_neutral(reply)
        self.assertIsNone(failure_copy.harmless_roast_fallback("normal question"))

    def test_image_scan_wrappers_are_provider_neutral(self):
        success = failure_copy.image_scan_success_reply("scan result")
        failed = failure_copy.image_scan_failure_reply("Gemini image scan failed (503): Google outage")

        self.assertEqual("scan result", success)
        self.assert_provider_neutral(failed)


if __name__ == "__main__":
    unittest.main()
