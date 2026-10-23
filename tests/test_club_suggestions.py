import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from davosbot import club_suggestions


def _create_tables(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE bot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                sender TEXT,
                event_type TEXT,
                payload TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts TEXT NOT NULL
            )
            """
        )


class ClubSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bot.db"
        _create_tables(self.db_path)
        self.owner = "+15550000001"
        self.payload = {
            "name": "Ping G410 4 Hybrid 22°",
            "category": "4 hybrid",
            "price": "$89 delivered",
            "condition": "Used, very good",
            "url": "https://example.com/ping-g410-4h",
            "rationale": "Forgiving and gaps cleanly above the 25° 5-iron.",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _patch_runtime(self):
        return patch.multiple(
            club_suggestions,
            BOT_DB_PATH=str(self.db_path),
            OWNER_ID=self.owner,
        )

    def test_send_records_log_and_conversation_context(self):
        with self._patch_runtime(), patch.object(
            club_suggestions, "send_message", return_value=True
        ) as send:
            result = club_suggestions.send_club_suggestion(self.payload)

        self.assertTrue(result["sent"])
        send.assert_called_once()
        sent_message = send.call_args.args[1]
        self.assertTrue(sent_message.startswith("Club suggestion: Ping G410"))
        self.assertIn("Reply yes or no", sent_message)
        with closing(sqlite3.connect(self.db_path)) as conn:
            event = conn.execute(
                "SELECT event_type, payload FROM bot_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            history = conn.execute(
                "SELECT sender, role, content FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(club_suggestions.SUGGESTION_EVENT, event[0])
        self.assertEqual("Ping G410 4 Hybrid 22°", json.loads(event[1])["name"])
        self.assertEqual((self.owner, "assistant", sent_message), history)

    def test_failed_delivery_does_not_record_suggestion(self):
        with self._patch_runtime(), patch.object(
            club_suggestions, "send_message", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                club_suggestions.send_club_suggestion(self.payload)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM bot_log").fetchone()[0])

    def test_bare_no_records_feedback_when_suggestion_is_latest_context(self):
        with self._patch_runtime(), patch.object(
            club_suggestions, "send_message", return_value=True
        ), patch.object(club_suggestions, "is_owner", return_value=True):
            club_suggestions.send_club_suggestion(self.payload)
            reply = club_suggestions.handle_club_command(self.owner, "no")
            state = club_suggestions.get_club_suggestion_state(limit=1)

        self.assertIn("skip Ping G410", reply)
        self.assertEqual("no", state["suggestions"][0]["decision"])

    def test_bare_yes_does_not_hijack_unrelated_conversation(self):
        with self._patch_runtime(), patch.object(
            club_suggestions, "send_message", return_value=True
        ), patch.object(club_suggestions, "is_owner", return_value=True):
            club_suggestions.send_club_suggestion(self.payload)
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    "INSERT INTO messages (sender, role, content, ts) VALUES (?, ?, ?, ?)",
                    (self.owner, "assistant", "Different question?", "2026-09-03T20:00:00"),
                )
                conn.commit()
            self.assertIsNone(club_suggestions.handle_club_command(self.owner, "yes"))

    def test_explicit_club_yes_uses_latest_open_suggestion(self):
        with self._patch_runtime(), patch.object(
            club_suggestions, "send_message", return_value=True
        ), patch.object(club_suggestions, "is_owner", return_value=True):
            club_suggestions.send_club_suggestion(self.payload)
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    "INSERT INTO messages (sender, role, content, ts) VALUES (?, ?, ?, ?)",
                    (self.owner, "assistant", "Different question?", "2026-09-03T20:00:00"),
                )
                conn.commit()
            reply = club_suggestions.handle_club_command(self.owner, "club yes")
        self.assertIn("shortlisted Ping G410", reply)

    def test_feedback_does_not_fall_back_to_an_older_pending_suggestion(self):
        older = dict(self.payload, name="Older option", url="https://example.com/older")
        latest = dict(self.payload, name="Latest option", url="https://example.com/latest")
        with self._patch_runtime(), patch.object(
            club_suggestions, "send_message", return_value=True
        ), patch.object(club_suggestions, "is_owner", return_value=True):
            club_suggestions.send_club_suggestion(older)
            club_suggestions.send_club_suggestion(latest)
            club_suggestions.handle_club_command(self.owner, "club yes")
            reply = club_suggestions.handle_club_command(self.owner, "club no")
        self.assertEqual("I don’t have an unanswered club suggestion right now.", reply)

    def test_non_owner_cannot_submit_explicit_feedback(self):
        with self._patch_runtime(), patch.object(
            club_suggestions, "is_owner", return_value=False
        ):
            self.assertEqual(
                "Club suggestion feedback is owner-only.",
                club_suggestions.handle_club_command("friend", "club no"),
            )
            self.assertIsNone(club_suggestions.handle_club_command("friend", "no"))

    def test_rejects_non_http_listing(self):
        payload = dict(self.payload, url="file:///tmp/fake")
        with self._patch_runtime():
            with self.assertRaisesRegex(ValueError, "http"):
                club_suggestions.format_suggestion_message(payload)


if __name__ == "__main__":
    unittest.main()
