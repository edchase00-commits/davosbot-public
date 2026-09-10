import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from davosbot import commands, ufc


FAILURE_REPORT = (
    "Add UFC fight card command #39\n"
    "Triggered via push 1 minute ago\n"
    "Status Failure\n"
    "Yeah that didn't work I think"
)

def _competitor(name: str, order: int) -> dict:
    slug = name.lower().replace(" ", "-")
    return {
        "order": order,
        "athlete": {"$ref": f"https://example.test/athletes/{slug}"},
    }


def _competition(segment: str, weight: str, description: str, a: str, b: str, date: str, idx: int) -> dict:
    return {
        "id": str(idx),
        "date": date,
        "description": description,
        "type": {"text": weight},
        "cardSegment": {"name": segment},
        "venue": {
            "fullName": "State Farm Arena",
            "address": {"city": "Atlanta", "state": "GA"},
        },
        "competitors": [
            _competitor(a, 1),
            _competitor(b, 2),
        ],
    }


class UFCFightCardTests(unittest.TestCase):
    def test_detects_ufc_card_requests_without_grabbing_normal_sports_text(self):
        self.assertTrue(ufc.is_ufc_fight_card_request("ufc card tonight"))
        self.assertTrue(ufc.is_ufc_fight_card_request("what is the main card?"))
        self.assertFalse(ufc.is_ufc_fight_card_request("how are the Mariners doing?"))

    def test_genuine_card_questions_and_short_commands(self):
        for text in (
            "ufc card", "ufc card tonight", "MMA fights tomorrow", "UFC tonight?",
            "fight card", "prelims?", "early prelims",
            "what is the main card?", "What's the UFC card this weekend?",
            "What’s tonight’s UFC card?", "which UFC fights are on tonight?",
            "who is on the UFC card?", "who's fighting in UFC tonight?",
            "when do the UFC prelims start?", "what time are the prelims?",
            "show me the UFC main card and prelims", "list the next UFC fights",
            "Can you please show me the full UFC card?", "Can I get the UFC card?",
            "please send us the UFC card", "give me UFC odds", "ufc card\nplease",
        ):
            with self.subTest(text=text):
                self.assertTrue(ufc.is_ufc_fight_card_request(text))

    def test_report_code_quotes_and_incidental_mentions_are_not_requests(self):
        for text in (
            FAILURE_REPORT,
            "Show me the error from the UFC fight card command",
            "What is wrong with the UFC card command?",
            "Why did the UFC card fetch fail?", "UFC card fetch failed from ESPN.",
            "UFC fight card command #39\nStatus: failure",
            "ufc card\nTraceback (most recent call last):",
            'assert is_ufc_fight_card_request("ufc card")',
            '```python\nrequest = "ufc card"\n```',
            "> what is the UFC card?", '"ufc card"', "'ufc card'", "`ufc card`",
            "He said 'what is the main card?'", "Report: UFC fight card",
            "ufc card was fun", "I enjoyed the UFC fights last night",
            "I have a UFC trading card", "Write a poem about UFC fights",
            "Can you explain what a UFC fight card is?", "Tell me about UFC history",
            "Don't show me the UFC card", "No UFC fights please", "my main card failed",
            "ufc 325 main card", "show me the UFC 325 card", "UFC #325 prelims",
            "What's the UFC 325 fight card?", "UFC Jones vs Miocic card",
            "How are the Mariners doing?", "", "ufc", "card",
        ):
            with self.subTest(text=text):
                self.assertFalse(ufc.is_ufc_fight_card_request(text))

    def test_actual_command_dispatch_does_not_fetch_card_for_pasted_failure(self):
        with (
            patch.object(commands, "handle_club_command", return_value=None),
            patch.object(commands, "_looks_like_self_repair_intake", return_value=False),
            patch.object(commands, "is_owner", return_value=True),
            patch.object(commands, "is_admin", return_value=True),
            patch("davosbot.food_order.handle_food_order", return_value=None),
            patch.object(commands, "get_ufc_fight_card", return_value="synthetic card") as provider,
        ):
            self.assertIsNone(commands.handle_command("+15550000001", FAILURE_REPORT))
            self.assertIsNone(commands.handle_command("+15550000001", "show me the UFC 325 card"))
            provider.assert_not_called()
            self.assertEqual("synthetic card", commands.handle_command("+15550000001", "ufc card"))
            provider.assert_called_once_with()
            provider.reset_mock()
            self.assertIsNone(commands.handle_command("+15550000001", "ufc card", skip_web_search=True))
            provider.assert_not_called()

    def test_formats_professional_main_card_and_prelims(self):
        scoreboard = {
            "events": [],
            "leagues": [
                {
                    "calendar": [
                        {
                            "label": "UFC Test: Jones vs Miocic",
                            "startDate": "2026-05-17T00:00Z",
                            "event": {"$ref": "https://example.test/events/600"},
                        }
                    ]
                }
            ],
        }
        event = {
            "id": "600",
            "name": "UFC Test: Jones vs Miocic",
            "date": "2026-05-17T00:00Z",
            "competitions": [
                _competition("prelims", "Welterweight", "3 Rnd", "Prelim One", "Prelim Two", "2026-05-16T21:00Z", 1),
                _competition("main", "Heavyweight", "5 Rnd", "Jon Jones", "Stipe Miocic", "2026-05-17T00:00Z", 2),
                _competition("main", "Lightweight", "3 Rnd", "Co Main", "Other Guy", "2026-05-17T00:00Z", 3),
            ],
        }

        def fake_fetch(url, params=None):
            if "scoreboard" in url:
                return scoreboard
            if "events/600" in url:
                return event
            if "/athletes/" in url:
                slug = url.rsplit("/", 1)[-1]
                return {"displayName": slug.replace("-", " ").title()}
            raise AssertionError(url)

        with patch.object(ufc, "_fetch_json", side_effect=fake_fetch):
            reply = ufc.get_ufc_fight_card(datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc))

        self.assertIn("UFC Test: Jones vs Miocic", reply)
        self.assertIn("State Farm Arena, Atlanta, GA", reply)
        self.assertIn("Main Card", reply)
        self.assertIn("1. Jon Jones vs Stipe Miocic - Heavyweight, 5 Rnd", reply)
        self.assertIn("Prelims", reply)
        self.assertIn("1. Prelim One vs Prelim Two - Welterweight, 3 Rnd", reply)
        self.assertNotIn("House bias", reply)
        self.assertNotIn("not betting advice", reply.lower())


if __name__ == "__main__":
    unittest.main()
