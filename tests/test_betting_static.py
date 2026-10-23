import ast
import re
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from davosbot import american_odds


ROOT = Path(__file__).resolve().parents[1]


class _ClosingConnection:
    def __init__(self, *args, **kwargs):
        self._conn = sqlite3.connect(*args, **kwargs)

    def __enter__(self):
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _load_betting_helpers():
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    wanted = {
        "_parse_bet_input",
        "_calc_payout",
        "_get_unit_size",
        "_cmd_bet_settle",
        "_cmd_bet_log",
        "_cmd_bets",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "_american_odds": american_odds,
        "sqlite3": types.SimpleNamespace(connect=_ClosingConnection, OperationalError=sqlite3.OperationalError),
        "BOT_DB_PATH": "",
        "normalize_handle": lambda value: value,
        "is_admin": lambda sender: sender in {"owner", "admin"},
    }
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


class BettingStaticTests(unittest.TestCase):
    def setUp(self):
        self.helpers = _load_betting_helpers()
        self.parse_bet_input = self.helpers["_parse_bet_input"]
        self.calc_payout = self.helpers["_calc_payout"]
        self.cmd_bet_settle = self.helpers["_cmd_bet_settle"]
        self.cmd_bets = self.helpers["_cmd_bets"]
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.tmp.name) / "davosbot.db")
        self.helpers["BOT_DB_PATH"] = self.db_path
        with _ClosingConnection(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE sports_bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT,
                    event TEXT,
                    bet_type TEXT,
                    odds INTEGER,
                    stake REAL,
                    unit_size REAL,
                    notes TEXT,
                    result TEXT DEFAULT 'pending',
                    payout REAL,
                    date TEXT DEFAULT CURRENT_DATE,
                    settled_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE bet_config (
                    sender TEXT,
                    key TEXT,
                    value TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by TEXT,
                    challenger TEXT,
                    opponent TEXT,
                    description TEXT,
                    amount REAL,
                    status TEXT,
                    winner TEXT,
                    settled_at TEXT
                )
                """
            )
        self.addCleanup(self.tmp.cleanup)

    def test_parse_sports_bet_log_input(self):
        parsed = self.parse_bet_input("/bet log Pacers ML +120 1.5u")
        self.assertEqual("Pacers ML", parsed["event"])
        self.assertEqual(120, parsed["odds"])
        self.assertEqual(1.5, parsed["stake"])
        self.assertEqual("moneyline", parsed["bet_type"])

    def test_parse_bet_without_odds_asks_for_clarification(self):
        reply = self.parse_bet_input("/bet log Pacers 1u")
        self.assertIn("what are the odds", reply)

    def test_target_profit_is_converted_to_stake_for_both_odds_signs(self):
        for text, stake in (
            ("/bet log Lakers -125 to win1u", 1.25),
            ("/bet log Pacers +200 to win 1u", .5),
            ("bet log Pacers +100 to win .75u", .75),
            ("bet log Lakers -250 to win 2 units spread", 5),
        ):
            with self.subTest(text=text):
                parsed = self.parse_bet_input(text)
                self.assertIsInstance(parsed, dict)
                self.assertAlmostEqual(stake, parsed["stake"])

    def test_explicit_to_win_log_saves_converted_stake_only_once(self):
        reply = self.helpers["_cmd_bet_log"]("/bet log Lakers -125 to win1u", "owner")
        with _ClosingConnection(self.db_path) as conn:
            rows = conn.execute("SELECT odds,stake FROM sports_bets").fetchall()
        self.assertEqual([(-125, 1.25)], rows)
        self.assertIn("1.25u", reply)

    def test_invalid_odds_and_amounts_do_not_log_a_bet(self):
        for text in (
            "/bet log Lakers +0 1u", "/bet log Lakers -90 1u",
            "/bet log Lakers -125 to win0u", "/bet log Lakers -125 0u",
            "/bet log Lakers -125 1..2u",
        ):
            with self.subTest(text=text):
                self.assertIsInstance(self.parse_bet_input(text), str)
                self.helpers["_cmd_bet_log"](text, "owner")
        with _ClosingConnection(self.db_path) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM sports_bets").fetchone()[0])

    def test_payout_calculation(self):
        self.assertEqual(1.2, self.calc_payout(120, 1.0, "win"))
        self.assertEqual(1.0, self.calc_payout(-200, 2.0, "win"))
        self.assertEqual(-2.0, self.calc_payout(-110, 2.0, "loss"))
        self.assertEqual(0.0, self.calc_payout(150, 2.0, "push"))

    def test_sports_bet_settle_by_id_requires_owner_or_admin(self):
        with _ClosingConnection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sports_bets
                    (sender, event, bet_type, odds, stake, unit_size, notes)
                VALUES ('other', 'Pacers ML', 'moneyline', 120, 1.0, 10.0, '')
                """
            )

        denied = self.cmd_bet_settle("/bet settle 1 win", "friend")

        self.assertIn("Only the bet owner", denied)
        with _ClosingConnection(self.db_path) as conn:
            result = conn.execute("SELECT result FROM sports_bets WHERE id = 1").fetchone()[0]
        self.assertEqual("pending", result)

        settled = self.cmd_bet_settle("/bet settle 1 win", "admin")
        self.assertIn("cashed", settled)

    def test_social_bets_are_admin_only(self):
        denied = self.cmd_bets("bets new friend 50 who wins", "friend")

        self.assertEqual("Social bets are admin-only.", denied)
        with _ClosingConnection(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        self.assertEqual(0, count)

        created = self.cmd_bets("bets new friend 50 who wins", "admin")
        self.assertIn("Bet #1 created", created)


if __name__ == "__main__":
    unittest.main()
