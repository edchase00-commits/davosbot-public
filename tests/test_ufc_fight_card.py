import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from davosbot import ufc
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
