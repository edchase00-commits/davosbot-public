"""Tests for davosbot.roster: parsing, persistence, failure modes, dispatch."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import commands
from davosbot import roster as roster_mod


def _patch_roster_path(testcase, tmpdir):
    """Point roster.ROSTER_PATH at a temp file for the duration of a test."""
    target = Path(tmpdir) / "roster.json"
    patcher = patch.object(roster_mod, "ROSTER_PATH", target)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return target


class ParseTests(unittest.TestCase):
    def test_leading_colon_stripped_from_first_entry(self):
        # The reported bug: first entry parsed as ": Mahomes"
        players = roster_mod.parse_roster_text("save my roster: Mahomes, Barkley, Jefferson")
        self.assertEqual(players, ["Mahomes", "Barkley", "Jefferson"])

    def test_numbered_list(self):
        players = roster_mod.parse_roster_text("save my roster\n1. Mahomes\n2. Barkley\n3. Jefferson")
        self.assertEqual(players, ["Mahomes", "Barkley", "Jefferson"])

    def test_bullets_and_dashes(self):
        players = roster_mod.parse_roster_text("here's my roster: - Mahomes; • Barkley; – Jefferson")
        self.assertEqual(players, ["Mahomes", "Barkley", "Jefferson"])

    def test_ambiguous_names_preserved_verbatim(self):
        # "A. Brown" / "M. Lemon" must survive parsing untouched (never expanded).
        players = roster_mod.parse_roster_text("save my roster: A. Brown, M. Lemon, Brian Thomas Jr.")
        self.assertEqual(players, ["A. Brown", "M. Lemon", "Brian Thomas Jr."])

    def test_defense_special_teams_name(self):
        players = roster_mod.parse_roster_text("save my roster: Philadelphia Eagles D/ST, Mahomes")
        self.assertEqual(players, ["Philadelphia Eagles D/ST", "Mahomes"])

    def test_malformed_empty(self):
        self.assertEqual(roster_mod.parse_roster_text("save my roster"), [])
        self.assertEqual(roster_mod.parse_roster_text("save my roster: , , ,"), [])
        self.assertEqual(roster_mod.parse_roster_text("save my roster: ;;;"), [])

    def test_skip_words_filtered(self):
        players = roster_mod.parse_roster_text("save my roster: my team roster, Mahomes")
        self.assertEqual(players, ["Mahomes"])

    def test_detection_helpers(self):
        self.assertTrue(roster_mod.is_roster_save_request("save my roster: Mahomes"))
        self.assertTrue(roster_mod.is_roster_save_request("here's my roster"))
        self.assertTrue(roster_mod.is_roster_clear_request("clear my roster"))
        self.assertTrue(roster_mod.is_roster_show_request("show my roster"))
        self.assertTrue(roster_mod.is_roster_show_request("what's my roster?"))
        self.assertFalse(roster_mod.is_roster_save_request("how is my team doing"))
        self.assertFalse(roster_mod.is_roster_show_request("is my roster good this week"))


class PersistenceTests(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            _patch_roster_path(self, tmp)
            ok, msg = roster_mod.save_roster(["Mahomes", "Barkley"])
            self.assertTrue(ok, msg)
            self.assertEqual(roster_mod.load_roster(), ["Mahomes", "Barkley"])

    def test_save_replaces_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _patch_roster_path(self, tmp)
            roster_mod.save_roster(["Mahomes"])
            roster_mod.save_roster(["Barkley", "Jefferson"])
            self.assertEqual(roster_mod.load_roster(), ["Barkley", "Jefferson"])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["players"], ["Barkley", "Jefferson"])

    def test_save_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _patch_roster_path(self, tmp)
            target.write_text(json.dumps({
                "players": ["Old Guy"],
                "team": "RIP Ser Davos",
                "statuses": {"Saquon Barkley": "QUES"},
            }), encoding="utf-8")
            ok, _ = roster_mod.save_roster(["Mahomes"])
            self.assertTrue(ok)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["players"], ["Mahomes"])
            self.assertEqual(data["team"], "RIP Ser Davos")
            self.assertEqual(data["statuses"], {"Saquon Barkley": "QUES"})

    def test_load_dict_shaped_players(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _patch_roster_path(self, tmp)
            target.write_text(json.dumps({
                "players": [{"name": "Mahomes", "status": "OK"}, {"name": "Barkley"}],
            }), encoding="utf-8")
            self.assertEqual(roster_mod.load_roster(), ["Mahomes", "Barkley"])

    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            _patch_roster_path(self, tmp)
            self.assertEqual(roster_mod.load_roster(), [])

    def test_load_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _patch_roster_path(self, tmp)
            target.write_text("{not json", encoding="utf-8")
            self.assertEqual(roster_mod.load_roster(), [])

    def test_save_empty_players_rejected(self):
        ok, msg = roster_mod.save_roster([])
        self.assertFalse(ok)
        self.assertIn("No players", msg)

    def test_save_write_failure(self):
        # ROSTER_PATH under a regular file: parent mkdir must fail.
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            target = blocker / "roster.json"
            with patch.object(roster_mod, "ROSTER_PATH", target):
                ok, msg = roster_mod.save_roster(["Mahomes"])
            self.assertFalse(ok)
            self.assertIn("Failed to save roster", msg)

    def test_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _patch_roster_path(self, tmp)
            roster_mod.save_roster(["Mahomes"])
            ok, _ = roster_mod.clear_roster()
            self.assertTrue(ok)
            self.assertFalse(target.exists())
            self.assertEqual(roster_mod.load_roster(), [])

    def test_clear_missing_file_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            _patch_roster_path(self, tmp)
            ok, msg = roster_mod.clear_roster()
            self.assertTrue(ok)


class DispatchTests(unittest.TestCase):
    def test_non_owner_gets_none(self):
        with patch.object(roster_mod, "is_owner", return_value=False):
            self.assertIsNone(roster_mod.handle_roster_message("rando", "save my roster: Mahomes"))

    def test_owner_save_show_clear_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            _patch_roster_path(self, tmp)
            with patch.object(roster_mod, "is_owner", return_value=True):
                reply = roster_mod.handle_roster_message("owner", "save my roster: Mahomes, Barkley")
                self.assertIn("Saved 2 players", reply)
                self.assertIn("Mahomes", reply)

                reply = roster_mod.handle_roster_message("owner", "show my roster")
                self.assertIn("1. Mahomes", reply)
                self.assertIn("2. Barkley", reply)

                reply = roster_mod.handle_roster_message("owner", "clear my roster")
                self.assertIn("cleared", reply.lower())

                reply = roster_mod.handle_roster_message("owner", "show my roster")
                self.assertIn("empty", reply.lower())

    def test_owner_save_with_no_names(self):
        with patch.object(roster_mod, "is_owner", return_value=True):
            reply = roster_mod.handle_roster_message("owner", "save my roster")
            self.assertIn("couldn't find any player names", reply)

    def test_unrelated_text_returns_none(self):
        with patch.object(roster_mod, "is_owner", return_value=True):
            self.assertIsNone(roster_mod.handle_roster_message("owner", "what's the weather"))


class CommandIntegrationTests(unittest.TestCase):
    """Proves roster commands are wired into the real inbound dispatch."""

    def test_handle_command_routes_roster_save_show_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "roster.json"
            with (
                patch.object(commands, "is_owner", return_value=True),
                patch.object(roster_mod, "is_owner", return_value=True),
                patch.object(roster_mod, "ROSTER_PATH", target),
            ):
                reply = commands.handle_command("owner", "save my roster: Mahomes, Barkley, Jefferson")
                self.assertIsNotNone(reply)
                self.assertIn("Saved 3 players", reply)
                self.assertIn("Mahomes, Barkley, Jefferson", reply)

                reply = commands.handle_command("owner", "show my roster")
                self.assertIsNotNone(reply)
                self.assertIn("1. Mahomes", reply)
                self.assertIn("3. Jefferson", reply)

                reply = commands.handle_command("owner", "clear my roster")
                self.assertIsNotNone(reply)
                self.assertIn("cleared", reply.lower())

    def test_handle_command_ignores_roster_for_non_owner(self):
        with patch.object(commands, "is_owner", return_value=False):
            with patch.object(roster_mod, "is_owner", return_value=False):
                reply = commands.handle_command("rando", "save my roster: Mahomes")
                # Must not save or confirm; falls through to normal handling.
                self.assertNotIn("Saved", reply or "")


if __name__ == "__main__":
    unittest.main()
