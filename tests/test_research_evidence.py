"""Public evidence collection regressions. All provider calls are mocked."""

from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch

import requests

from davosbot import config, research_cron, research_reports as reports


NOW = datetime(2026, 9, 9, 19, 0, tzinfo=research_cron.PACIFIC)
WAIVERS = {"kind": "fantasy_waivers", "research_topic": "NFL fantasy football waiver wire"}
GENERIC = {"kind": "generic", "research_topic": "battery technology"}


def source(path="one", **changes):
    value = {"url": "https://example.com/" + path, "title": "Public report",
             "content": "A dated source supports Player One.", "published_date": NOW.isoformat()}
    value.update(changes)
    return value


def response(rows):
    return Mock(json=Mock(return_value={"results": rows}))


class ResearchEvidenceTests(unittest.TestCase):
    def fetch(self, replies, payload=WAIVERS):
        with patch.object(config, "TAVILY_API_KEY", "synthetic-key"), patch.object(
                reports.requests, "post", side_effect=replies) as post:
            result = reports.fetch_sources(payload, NOW)
        return result, post

    def test_two_distinct_waiver_queries_keep_basic_cost_and_call_limits(self):
        result, post = self.fetch([response([source()]), response([source("two")])])
        first, second = [call.kwargs["json"] for call in post.call_args_list]
        self.assertIn("FAAB rankings pickups", first["query"])
        self.assertIn("sleepers running backs wide receivers", second["query"])
        for call in post.call_args_list:
            body = call.kwargs["json"]
            self.assertEqual("basic", body["search_depth"])
            self.assertFalse(body["auto_parameters"])
            self.assertFalse(body["include_answer"])
            self.assertFalse(body["include_raw_content"])
            self.assertEqual(5, body["max_results"])
            self.assertEqual(15, call.kwargs["timeout"])
        self.assertEqual(2, len(result))

    def test_supplemental_timeout_does_not_discard_valid_first_evidence(self):
        good = response([source()])
        result, post = self.fetch([good, requests.Timeout("synthetic")])
        self.assertEqual(["https://example.com/one"], [item["url"] for item in result])
        self.assertEqual(2, post.call_count)
        good.close.assert_called_once_with()

    def test_first_timeout_still_attempts_the_independent_second_query(self):
        good = response([source("second")])
        result, post = self.fetch([requests.Timeout("synthetic"), good])
        self.assertEqual(["https://example.com/second"], [item["url"] for item in result])
        self.assertEqual(2, post.call_count)
        good.close.assert_called_once_with()

    def test_total_provider_failure_has_fixed_reason_and_no_retry(self):
        with patch.object(config, "TAVILY_API_KEY", "synthetic-key"), patch.object(
                reports.requests, "post", side_effect=requests.Timeout("private-provider-detail")) as post:
            with self.assertRaisesRegex(reports.ReportFailure, "^search_failed$"):
                reports.fetch_sources(WAIVERS, NOW)
        self.assertEqual(2, post.call_count)

    def test_malformed_response_does_not_hide_other_query_evidence(self):
        for malformed in (None, [], {"results": None}, {"results": "invalid"}):
            with self.subTest(malformed=malformed):
                bad = Mock(json=Mock(return_value=malformed))
                result, _ = self.fetch([bad, response([source()])])
                self.assertEqual(1, len(result))
                bad.close.assert_called_once_with()

    def test_invalid_content_types_cannot_become_stringified_evidence(self):
        rows = [source("dict", content={"unsupported": "value"}),
                source("list", content=["not evidence"]), source("number", content=123),
                source("bad-title", title={"name": "Invented Player"}), source()]
        result, _ = self.fetch([response(rows), response([])])
        self.assertEqual(["https://example.com/one"], [item["url"] for item in result])

    def test_repeated_url_retains_complementary_bounded_snippets(self):
        result, _ = self.fetch([response([source(content="Player One has more snaps.")]),
                               response([source(content="Player Two has more targets.")])])
        self.assertEqual(1, len(result))
        self.assertEqual(1, result[0]["id"])
        self.assertIn("Player One", result[0]["content"])
        self.assertIn("Player Two", result[0]["content"])

    def test_duplicate_same_content_does_not_repeat_or_extend_budget(self):
        row = source(content="x" * 2500)
        result, _ = self.fetch([response([row]), response([row])])
        self.assertEqual("x" * 1800, result[0]["content"])

    def test_recent_public_evidence_and_existing_source_budget_still_required(self):
        invalid = [source("private", url="https://127.0.0.1/test"),
                   source("old", published_date=(NOW-timedelta(days=8)).isoformat()),
                   source("future", published_date=(NOW+timedelta(minutes=6)).isoformat()),
                   source("undated", published_date=None)]
        rows = [source(str(index), content="evidence " * 400) for index in range(12)]
        result, post = self.fetch([response(invalid + rows), response([])])
        self.assertEqual(8, len(result))
        self.assertEqual(list(range(1, 9)), [item["id"] for item in result])
        self.assertLessEqual(sum(len(item["content"]) for item in result), 14400)
        self.assertEqual(1, post.call_count)

    def test_no_recent_evidence_is_not_misreported_as_provider_failure(self):
        with patch.object(config, "TAVILY_API_KEY", "synthetic-key"), patch.object(
                reports.requests, "post", side_effect=[response([]), response([])]) as post:
            with self.assertRaisesRegex(reports.ReportFailure, "^no_recent_sources$"):
                reports.fetch_sources(WAIVERS, NOW)
        self.assertEqual(2, post.call_count)

    def test_generic_search_stays_one_query_with_no_waiver_substitution(self):
        result, post = self.fetch([response([source()])], GENERIC)
        self.assertEqual(1, post.call_count)
        self.assertEqual("battery technology latest 2026-09-09", post.call_args.kwargs["json"]["query"])
        self.assertEqual(1, len(result))


if __name__ == "__main__":
    unittest.main()
