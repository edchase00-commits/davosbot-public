"""Current clock questions bypass model arithmetic without stealing schedules."""

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from davosbot import main, time_queries
import test_no_web_tool_permissions as route_fixtures


NOW = datetime(2026, 10, 22, 2, 48, tzinfo=timezone.utc)
WINTER = datetime(2026, 12, 16, 3, 48, tzinfo=timezone.utc)


class CurrentTimeParserTests(unittest.TestCase):
    def test_eastern_query_uses_current_daylight_abbreviations(self):
        for text in (
            "what time is it eastern time",
            "What time is it in ET?",
            "what's the time EST",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    "It's 10:48 PM EDT right now. Pacific time is 7:48 PM PDT.",
                    time_queries.current_time_reply(text, now=NOW),
                )

    def test_pacific_query_and_complete_correction_are_deterministic(self):
        self.assertEqual(
            "It's 7:48 PM PDT right now. Eastern time is 10:48 PM EDT.",
            time_queries.current_time_reply("what time is it PST?", now=NOW),
        )
        for text in ("we are in pacific time", "We're in the Pacific time zone.", "Actually, we are in Pacific, not Eastern."):
            with self.subTest(text=text):
                self.assertEqual(
                    "Right. It's 7:48 PM PDT here; Eastern is 10:48 PM EDT.",
                    time_queries.current_time_reply(text, now=NOW),
                )

    def test_winter_clock_reports_standard_time_instead_of_fixed_labels(self):
        self.assertEqual(
            "It's 10:48 PM EST right now. Pacific time is 7:48 PM PST.",
            time_queries.current_time_reply("what time is it eastern time", now=WINTER),
        )

    def test_scheduling_and_incidental_timezone_text_are_not_consumed(self):
        for text in (
            "remind me tomorrow at 8 eastern time",
            "schedule a cron for 8pm eastern time",
            "we are in pacific time, schedule it for 8",
            "what time is the cron in eastern time?",
            "the game starts at 8 eastern time",
            "we are in pacific time for this trip",
            "explain eastern time",
        ):
            with self.subTest(text=text):
                self.assertIsNone(time_queries.current_time_reply(text, now=NOW))


class CurrentTimeRoutingTests(unittest.TestCase):
    setUp = route_fixtures.NoWebRouteTests.setUp
    route = route_fixtures.NoWebRouteTests.route

    def test_authorized_dm_and_group_routes_reply_without_model(self):
        original = time_queries.current_time_reply
        with patch.object(main, "current_time_reply", side_effect=lambda text: original(text, now=NOW)):
            for sender in (route_fixtures.OWNER, route_fixtures.ADMIN, route_fixtures.FRIEND):
                for group in (False, True):
                    with self.subTest(sender=sender, group=group):
                        self.route("what time is it eastern time", sender=sender, group=group)
                        self.model.assert_not_called()
                        self.send.assert_called_once()
                        self.assertEqual(
                            route_fixtures.GROUP if group else sender,
                            self.send.call_args.args[0],
                        )
                        self.assertEqual(
                            "It's 10:48 PM EDT right now. Pacific time is 7:48 PM PDT.",
                            self.send.call_args.args[1],
                        )
                        self.assertEqual(group, self.send.call_args.kwargs.get("is_group", False))

    def test_full_pacific_correction_bypasses_model(self):
        original = time_queries.current_time_reply
        with patch.object(main, "current_time_reply", side_effect=lambda text: original(text, now=NOW)):
            self.route("we are in pacific time", group=True)
        self.model.assert_not_called()
        self.assertEqual(
            "Right. It's 7:48 PM PDT here; Eastern is 10:48 PM EDT.",
            self.send.call_args.args[1],
        )

    def test_scheduling_statement_keeps_existing_route(self):
        original = time_queries.current_time_reply
        with patch.object(main, "current_time_reply", side_effect=lambda text: original(text, now=NOW)):
            self.route("we are in pacific time, schedule a cron for 8", group=True)
        self.model.assert_called_once()
        self.assertEqual("synthetic answer", self.send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
