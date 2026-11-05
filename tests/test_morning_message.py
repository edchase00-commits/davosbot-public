import re
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from davosbot import morning_quotes, tools
class MorningMessageTests(unittest.TestCase):
    def test_datey_gemini_quote_uses_fallback(self):
        with (
            patch.object(tools, "GEMINI_API_KEY", "test-key"),
            patch.object(tools, "_gemini_rewrite", return_value="May 20, 2026: Go win the day."),
            patch.object(tools, "_recent_quote_hashes", return_value=set()),
            patch.object(tools, "_log_quote_choice") as log_choice,
            patch.object(morning_quotes, "_fetch_zenquotes_quote", side_effect=RuntimeError("offline")),
        ):
            quote = tools._get_inspirational_quote()

        self.assertFalse(tools._morning_quote_mentions_date(quote))
        self.assertEqual("fallback:gemini_date_leak", log_choice.call_args.args[1])

    def test_morning_quote_date_detector_allows_today_but_not_dates(self):
        self.assertFalse(morning_quotes._morning_quote_mentions_date("Keep it simple today and take the next step."))
        self.assertTrue(morning_quotes._morning_quote_mentions_date("Tuesday is yours."))
        self.assertTrue(morning_quotes._morning_quote_mentions_date("May 20, 2026 is yours."))
        self.assertTrue(morning_quotes._morning_quote_mentions_date("2026-05-20 is yours."))

    def test_zenquotes_response_is_formatted_with_required_attribution(self):
        response = Mock()
        response.json.return_value = [{"q": "Do the next right thing.", "a": "Test Author"}]

        quote = morning_quotes._fetch_zenquotes_quote(
            request_get=lambda url, timeout: response,
        )

        response.raise_for_status.assert_called_once_with()
        self.assertIn("Do the next right thing.", quote)
        self.assertIn("- Test Author", quote)
        self.assertIn("https://zenquotes.io/", quote)

    def test_zenquotes_is_primary_and_skips_gemini(self):
        rewrite = Mock(side_effect=AssertionError("Gemini should not run"))
        log_choice = Mock()

        quote = morning_quotes._get_inspirational_quote(
            gemini_api_key="test-key",
            rewrite_fn=rewrite,
            recent_hashes_fn=lambda _date: set(),
            log_choice_fn=log_choice,
            zenquotes_fn=lambda: "Keep moving.\n- Test\n\nSource: https://zenquotes.io/",
        )

        self.assertIn("Keep moving.", quote)
        self.assertEqual("zenquotes", log_choice.call_args.args[1])
        rewrite.assert_not_called()

    def test_fallback_quotes_are_short_and_do_not_name_dates(self):
        dateish = re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|20\d{2})\b", re.I)

        for quote in morning_quotes._FALLBACK_QUOTES:
            self.assertLessEqual(len(quote.split()), 22)
            self.assertNotRegex(quote, dateish)

    def test_morning_body_strips_duplicate_quote_greeting(self):
        body = morning_quotes._render_morning_message_body(
            {"intro": "Good morning fellas."},
            "Good morning boys, stack the small wins early.",
            now_pt=datetime(2026, 5, 21, 6, 30),
        )

        self.assertEqual("Good morning fellas.\n\nStack the small wins early.", body)
        self.assertEqual(1, body.lower().count("good morning"))

    def test_morning_body_keeps_non_greeting_quote(self):
        body = morning_quotes._render_morning_message_body(
            {"intro": "Fresh slate today."},
            "Good morning starts with one clean rep.",
            now_pt=datetime(2026, 5, 21, 6, 30),
        )

        self.assertIn("Good morning starts with one clean rep.", body)


if __name__ == "__main__":
    unittest.main()
