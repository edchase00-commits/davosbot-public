"""Explicit arithmetic requests must not become model guesses or saved bets."""

import unittest
from unittest.mock import patch

from davosbot import commands, simple_chat
import test_owner_action_routing as action_routes


class OddsArithmeticTests(unittest.TestCase):
    def test_negative_odds_target_profit_is_not_mistaken_for_risk(self):
        expected = "At -125: risk 1.25u to win 1u profit. Total return if it wins: 2.25u."
        for prompt in (
            "-125 to win1u", "-125 to win 1 unit",
            "How much do I risk at -125 to win 1u?",
            "to win 1u at -125",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(expected, simple_chat.fast_chat_reply(prompt))

    def test_positive_odds_and_explicit_dollars_keep_distinct_profit_and_return(self):
        self.assertEqual(
            "At +200: risk 0.5u to win 1u profit. Total return if it wins: 1.5u.",
            simple_chat.fast_chat_reply("+200 to win 1u"),
        )
        self.assertEqual(
            "At -125: risk $125.00 to win $100.00 profit. Total return if it wins: $225.00.",
            simple_chat.fast_chat_reply("-125 to win $100"),
        )

    def test_calculator_leaves_actions_live_odds_and_ambiguous_messages_alone(self):
        for prompt in (
            "/bet log Lakers -125 to win1u", "log -125 to win1u", "bet settle 4 win",
            "send -125 to win1u to Morgan", "-125 to win1u and remind me tomorrow",
            "is Lakers -125 a good bet to win1u?", "latest odds for Lakers",
            "-125 to win $100u", "-125 to win 1u or +200 to win 2u",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(simple_chat.fast_chat_reply(prompt))

    def test_recurring_decimals_are_marked_approximate(self):
        self.assertEqual(
            "At +150: risk ~0.6667u to win 1u profit. Total return if it wins: ~1.6667u.",
            simple_chat.fast_chat_reply("+150 to win 1u"),
        )

    def test_invalid_numeric_requests_get_clarification_instead_of_arithmetic(self):
        for prompt in ("+0 to win1u", "-90 to win1u", "-125 to win0u", "-125 to win -1u"):
            with self.subTest(prompt=prompt):
                self.assertIn("Use valid American odds", simple_chat.fast_chat_reply(prompt))

    def test_owner_calculation_answers_without_model_or_bet_log(self):
        fixture = action_routes.OwnerActionRoutingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(commands, "_cmd_bet_log") as log_bet:
            fixture.route("-125 to win1u")
        fixture.model.assert_not_called()
        log_bet.assert_not_called()
        fixture.send.assert_called_once()
        self.assertIn("risk 1.25u", fixture.send.call_args.args[1])

    def test_explicit_log_retains_the_existing_native_action_route(self):
        fixture = action_routes.OwnerActionRoutingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prompt = "/bet log Lakers -125 to win1u"
        with patch.object(commands, "_cmd_bet_log", return_value="synthetic saved bet") as log_bet:
            fixture.route(prompt)
        log_bet.assert_called_once_with(prompt, action_routes.OWNER)
        fixture.model.assert_not_called()
        self.assertEqual("synthetic saved bet", fixture.send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
