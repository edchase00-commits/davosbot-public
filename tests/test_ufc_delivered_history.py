"""Native card-to-follow-up history through real dispatch and local payloads."""

import unittest
from unittest.mock import patch

import test_native_command_history as fixture
from davosbot import brain, commands, group_chat, main, memory
from test_ollama_prompt_routing import _FakeOllamaResponse


CARD = "Synthetic UFC card: Morgan vs Riley, main card Saturday at 7pm Pacific."


class UfcDeliveredHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.NativeCommandHistoryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.stack.enter_context(patch.object(brain, "BOT_DB_PATH", self.f.db_path))
        self.f.stack.enter_context(patch.object(brain, "_log_bot_event"))
        self.f.stack.enter_context(patch.object(brain, "_ollama_down", False))
        for name in ("MODEL_ROUTE_SIMPLE_CHAT", "MODEL_ROUTE_COMPLEX_REASONING", "MODEL_ROUTE_CODE_REVIEW"):
            self.f.stack.enter_context(patch.object(brain, name, "ollama:gemma3"))
        self.f.stack.enter_context(patch.object(group_chat, "is_approved_user", return_value=True))
        self.quota = self.f.stack.enter_context(patch.object(main, "get_tool_uses_today", return_value=0))
        self.usage = self.f.stack.enter_context(patch.object(main, "log_tool_use"))
        self.command_card = self.f.stack.enter_context(patch.object(commands, "get_ufc_fight_card", return_value=CARD))
        self.direct_card = self.f.stack.enter_context(patch.object(main, "get_ufc_fight_card", return_value=CARD))
        self.cloud = self.f.stack.enter_context(patch.object(brain, "_call_gemini", side_effect=AssertionError("Unexpected cloud route")))
        self.agentic = self.f.stack.enter_context(patch.object(brain, "_call_gemini_agentic", side_effect=AssertionError("Unexpected tool route")))
        self.post = self.f.stack.enter_context(patch.object(brain.requests, "post", return_value=_FakeOllamaResponse()))
        self.f.model.side_effect = brain.get_response

    def route(self, text, sender, group):
        self.f.route(("@Davos " if group else "") + text, sender=sender, chat=fixture.GROUP if group else sender)

    def test_delivered_card_reaches_actual_followup_provider_for_each_existing_role(self):
        for group in (False, True):
            for sender in (fixture.OWNER, fixture.ADMIN, fixture.FRIEND):
                with self.subTest(group=group, sender=sender):
                    self.f.clear_history()
                    self.post.reset_mock()
                    self.route("ufc card", sender, group)
                    self.post.assert_not_called()
                    self.assertEqual(CARD, self.f.send.call_args.args[1])
                    self.assertEqual(fixture.GROUP if group else sender, self.f.send.call_args.args[0])
                    self.route("Who is in the main event from that list?", sender, group)
                    previous = self.post.call_args.kwargs["json"]["messages"][1:-1]
                    self.assertTrue(any(CARD == turn["content"] for turn in previous))
                    self.assertEqual((sender + ": " if group else "") + "ufc card", previous[0]["content"])
                    self.assertEqual([], memory.get_history(fixture.OTHER_GROUP))
                    if group:
                        self.assertEqual([], memory.get_history(sender))

    def test_failed_send_does_not_enter_card_context_for_any_existing_role(self):
        self.f.send.return_value = False
        for group in (False, True):
            for sender in (fixture.OWNER, fixture.ADMIN, fixture.FRIEND):
                with self.subTest(group=group, sender=sender):
                    self.f.clear_history()
                    self.route("ufc card", sender, group)
                    self.f.send.assert_called_once()
                    self.assertEqual([], memory.get_history(fixture.GROUP if group else sender))

    def test_history_failure_after_send_is_contained_without_second_send(self):
        for group in (False, True):
            with self.subTest(group=group), patch.object(main, "save_turn", side_effect=RuntimeError("synthetic history unavailable")):
                self.f.send.reset_mock()
                self.route("ufc card", fixture.FRIEND, group)
                self.f.send.assert_called_once()
                self.assertEqual(CARD, self.f.send.call_args.args[1])

    def test_friend_quota_denial_does_not_lookup_or_store_card(self):
        self.quota.return_value = main._FRIEND_SEARCH_LIMIT
        for group in (False, True):
            with self.subTest(group=group):
                self.f.clear_history()
                self.route("ufc card", fixture.FRIEND, group)
                self.assertIn("daily limit", self.f.send.call_args.args[1])
                self.assertEqual([], memory.get_history(fixture.GROUP if group else fixture.FRIEND))
        self.direct_card.assert_not_called()
        self.command_card.assert_not_called()
        self.usage.assert_not_called()

    def test_no_search_keeps_native_card_lookup_disabled(self):
        for group in (False, True):
            for sender in (fixture.OWNER, fixture.ADMIN, fixture.FRIEND):
                with self.subTest(group=group, sender=sender):
                    self.f.clear_history()
                    self.route("no search ufc card", sender, group)
                    self.assertFalse(any(CARD in turn["content"] for turn in memory.get_history(fixture.GROUP if group else sender)))
        self.direct_card.assert_not_called()
        self.command_card.assert_not_called()
        self.usage.assert_not_called()

    def test_disabled_group_does_not_lookup_or_store_card(self):
        with patch.object(main, "is_gc_enabled", return_value=False):
            self.route("ufc card", fixture.FRIEND, True)
        self.direct_card.assert_not_called()
        self.command_card.assert_not_called()
        self.assertEqual([], memory.get_history(fixture.GROUP))

    def test_history_whitelist_reuses_bounded_query_not_report_or_numbered_event(self):
        for text in ("ufc card", "what is the main card?", "please show me the UFC card tonight"):
            self.assertTrue(main._native_command_has_safe_history(text))
        for text in ('assert is_ufc_fight_card_request("ufc card")', '"ufc card"', "ufc card was fun", "ufc 325 main card"):
            self.assertFalse(main._native_command_has_safe_history(text))


if __name__ == "__main__":
    unittest.main()
