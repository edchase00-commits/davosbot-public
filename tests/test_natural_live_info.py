"""Natural place and event questions use the existing bounded lookup route."""

import unittest
from unittest.mock import patch

from davosbot import main
import test_no_web_tool_permissions as route_fixtures


class NaturalLiveInfoTests(unittest.TestCase):
    setUp = route_fixtures.NoWebRouteTests.setUp
    route = route_fixtures.NoWebRouteTests.route

    def test_owner_place_and_event_questions_reach_lookup_in_dm_and_group(self):
        for group in (False, True):
            for request in (
                "What's good to eat in Terminal B at BOS?",
                "Where should we get dinner in Portland?",
                "Find the nearest In-N-Out to Denver.",
                "Closest Thai restaurant?",
                "Can you recommend a coffee shop near Union Station?",
                "Is the cafe near Union Station open tonight?",
                "When do the Mariners play next?",
                "What time does the concert start tonight?",
            ):
                with self.subTest(group=group, request=request):
                    self.route(request, group=group)
                    self.model.assert_called_once()
                    self.assertTrue(self.model.call_args.kwargs["use_tools"])
                    self.assertFalse(self.model.call_args.kwargs.get("simple_chat", False))
                    self.assertEqual(request, self.model.call_args.args[2])

    def test_nonowner_lookup_remains_limited_and_respects_quota(self):
        for sender in (route_fixtures.ADMIN, route_fixtures.FRIEND):
            for group in (False, True):
                with self.subTest(sender=sender, group=group):
                    self.route("Where should we eat near Union Station?", sender=sender, group=group)
                    self.model.assert_called_once()
                    if sender == route_fixtures.FRIEND and not group:
                        self.assertFalse(self.model.call_args.kwargs.get("allowed_tools"))
                    else:
                        self.assertEqual(["web_search", "get_weather"], self.model.call_args.kwargs["allowed_tools"])
                    self.assertFalse(self.model.call_args.kwargs.get("use_tools", False))
        with patch.object(main, "get_tool_uses_today", return_value=main._FRIEND_SEARCH_LIMIT):
            self.route("Closest Thai restaurant?", sender=route_fixtures.FRIEND, group=True)
        self.assertFalse(self.model.call_args.kwargs.get("use_tools", False))
        self.assertFalse(self.model.call_args.kwargs.get("allowed_tools"))

    def test_no_search_keeps_place_lookup_disabled(self):
        for sender in (route_fixtures.OWNER, route_fixtures.ADMIN, route_fixtures.FRIEND):
            for group in (False, True):
                with self.subTest(sender=sender, group=group):
                    self.route("no search where should we eat near Union Station?", sender=sender, group=group)
                    self.model.assert_called_once()
                    self.assertFalse(self.model.call_args.kwargs.get("use_tools", False))
                    self.assertFalse(self.model.call_args.kwargs.get("allowed_tools"))
                    self.assertIn("do not search the web", self.model.call_args.args[0])

    def test_supplied_choices_howto_and_creative_prompts_do_not_request_lookup(self):
        for request in (
            "Explain the nearest neighbor algorithm.",
            "How do I open a coffee shop?",
            "Why do people eat dinner late?",
            "Write a poem about a restaurant near a river.",
            "Choose dinner from these options: pizza $12 or pasta $16. Budget $14.",
            "What time should our fictional concert start?",
        ):
            with self.subTest(request=request):
                self.route(request)
                self.model.assert_called_once()
                self.assertFalse(self.model.call_args.kwargs["use_tools"])


if __name__ == "__main__":
    unittest.main()
