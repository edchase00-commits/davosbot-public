"""Consumer-copy acceptance tests use synthetic plans, never private payloads."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from davosbot import fitness_context as fitness, permissions, work_actions_extra as extra
import test_fitness_context as fixtures


OWNER, OTHER = fixtures.OWNER, fixtures.OTHER
NOW = datetime(2026, 10, 7, 18, tzinfo=timezone.utc)
HABITS = ["Synthetic habit " + str(n) + "." for n in range(1, 10)]
WORK = "Warm up 3 minutes easy.\n1. Synthetic lift: 1 x 4-6; rest 45 sec\nUse a controlled pace. Stop for sharp pain."
QUOTE = '"Synthetic motivation for the fixture."\nSynthetic attribution'


def consumer_fixture():
    plan = fixtures.fixture()
    plan["status"] = "provisional"
    plan["daily_habits"] = list(HABITS)
    plan["consumer_quote"] = QUOTE
    plan["progression"] = ["INTERNAL ONLY: never infer completion from this synthetic note."]
    plan["safety"] = ["PRIVATE REFERENCE CANARY: not ordinary consumer copy."]
    for number, day in enumerate(fitness.DAYS):
        strength = day in {"Monday", "Wednesday", "Friday"}
        plan["weekly_schedule"][day] = {
            "name": "SYNTHETIC PRIVATE PROGRAM LABEL",
            "details": WORK if strength else f"1. Synthetic easy walk: {10 + number} minutes\nRest is fine if tired.",
            "exercise_ids": ["leg_press"] if strength else [],
        }
    return plan


class FitnessConsumerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(temporary) / "synthetic.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE user_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, value TEXT, source TEXT)")
        self.stack.enter_context(patch.object(permissions, "OWNER_ID", OWNER))
        self.stack.enter_context(patch.object(fitness.config, "OWNER_ID", OWNER))
        self.stack.enter_context(patch.object(fitness.config, "BOT_DB_PATH", str(self.path)))
        self.plan = consumer_fixture()
        self.snapshot = fitness.set_plan(json.dumps(self.plan), "absent", OWNER)["snapshot"]

    def test_all_seven_days_match_reviewed_consumer_goldens(self):
        goldens = json.loads((Path(__file__).parent / "fixtures" / "fitness_consumer_golden.json").read_text())
        self.assertEqual(set(goldens), set(fitness.DAYS))
        for day in fitness.DAYS:
            with self.subTest(day=day):
                reply = fitness.plan_command_reply("What's my workout for " + day, sender=OWNER, chat_id=OWNER, is_group=False, now=NOW)
                self.assertEqual(reply, goldens[day])
                self.assertIn(fitness._consumer_typography(self.plan["weekly_schedule"][day]["details"]), reply)
                self.assertIn("DAILY HABITS", reply)
                self.assertEqual(reply.count("• "), 9)
                for habit in HABITS:
                    self.assertIn(habit, reply)
                self.assertLess(len(reply), 2000)
        self.assertEqual(fitness.load_current()[1], self.snapshot)

    def test_screenshot_style_does_not_dump_metadata_or_private_reference(self):
        reply = fitness.plan_command_reply("What’s my workout for tmw", sender=OWNER, chat_id=OWNER, is_group=False, now=NOW)
        self.assertTrue(reply.startswith("Workout date: Thursday, October 8, 2026.\n\nIt's a movement and recovery day. Here's Thursday's workout and habits.\n\n" + QUOTE))
        self.assertIn("\n\nMOVEMENT\n", reply)
        self.assertIn("\n\nDAILY HABITS\n", reply)
        for forbidden in ("provisional", "plan v", "plan_id", "schema_version", "Progression:", "Safety:",
                          "never infer", "never invent", "PRIVATE REFERENCE", "INTERNAL ONLY", "SYNTHETIC PRIVATE PROGRAM LABEL"):
            self.assertNotIn(forbidden.casefold(), reply.casefold())

    def test_consumer_preview_is_the_same_formatter_without_send_or_model(self):
        query = "What’s my workout for tmw"
        formatter = fitness.plan_command_reply
        with patch.object(fitness, "plan_command_reply", side_effect=lambda text, **kwargs: formatter(text, now=NOW, **kwargs)):
            result = extra.execute_extra_action("fitness.plan.preview", {"query": query}, OWNER)["result"]
        self.assertEqual(result["plan_reply"], formatter(query, sender=OWNER, chat_id=OWNER, is_group=False, now=NOW))
        self.assertFalse(result["model_called"])
        self.assertFalse(result["sent"])

    def test_renderer_preserves_reviewed_copy_without_scraping_unknown_prose(self):
        plan = deepcopy(self.plan)
        plan["weekly_schedule"]["Monday"]["details"] = "A precise future prescription: 3 x 7; rest 83 sec."
        plan["daily_habits"] = ["A distinct future habit with its full condition."]
        before = deepcopy(plan)
        reply = fitness.render_consumer_plan(plan, date(2026, 10, 12))
        self.assertIn(fitness._consumer_typography(plan["weekly_schedule"]["Monday"]["details"]), reply)
        self.assertIn(plan["daily_habits"][0], reply)
        self.assertEqual(plan, before)

    def test_paused_start_end_and_private_scope_are_still_guarded(self):
        with patch.object(fitness, "load_current", side_effect=AssertionError("private read")):
            for sender, chat, group in ((OTHER, OTHER, False), (OWNER, OTHER, False), (OWNER, "a" * 32, True)):
                self.assertIsNone(fitness.plan_command_reply("What’s my workout for tmw", sender=sender, chat_id=chat, is_group=group, now=NOW))
        for change, phrase in (({"status": "paused"}, "plan is paused"),
                               ({"valid_from_local": "2026-10-09"}, "starts Friday, October 9"),
                               ({"valid_until_local": "2026-10-07"}, "beyond the current plan")):
            plan = {**self.plan, **change}
            with patch.object(fitness, "load_current", return_value=(plan, "synthetic")):
                reply = fitness.plan_command_reply("What’s my workout for tmw", sender=OWNER, chat_id=OWNER, is_group=False, now=NOW)
            self.assertIn(phrase, reply)
            self.assertNotIn("DAILY HABITS", reply)
            self.assertNotIn("provisional", reply)

    def test_typography_keeps_dates_identifiers_and_all_words(self):
        text = "On 2026-10-08, A-1-2 stays literal. Lift: 1-2 x 8-12; rest 90-120 sec."
        expected = "On 2026-10-08, A-1-2 stays literal. Lift: 1–2 × 8–12; rest 90–120 sec."
        self.assertEqual(fitness._consumer_typography(text), expected)

    def test_quote_is_optional_and_never_rewritten(self):
        target = date(2026, 10, 8)
        original = fitness.render_consumer_plan(self.plan, target)
        plan = deepcopy(self.plan)
        del plan["consumer_quote"]
        fitness.parse_plan(json.dumps(plan))
        without_quote = fitness.render_consumer_plan(plan, target)
        self.assertEqual(original.replace("\n\n" + QUOTE, "", 1), without_quote)
        plan["consumer_quote"] = '"Keep 1-2 x 3 exactly."\nAttribution'
        self.assertIn(plan["consumer_quote"], fitness.render_consumer_plan(plan, target))

    def test_quote_is_excluded_from_grounding_demos_and_metadata(self):
        plain = deepcopy(self.plan)
        del plain["consumer_quote"]
        for query in ("today workout", "tomorrow workout", "habits", "leg press video"):
            self.assertEqual(fitness.render_context(self.plan, query, now=NOW),
                             fitness.render_context(plain, query, now=NOW))
        self.assertNotIn(QUOTE, json.dumps(fitness.metadata(self.plan, "synthetic")))
        args = dict(sender=OWNER, chat_id=OWNER, is_group=False, now=NOW)
        demo = fitness.deterministic_reply("leg press video", **args)
        with patch.object(fitness, "load_current", return_value=(plain, "synthetic")):
            self.assertEqual(demo, fitness.deterministic_reply("leg press video", **args))

    def test_selected_date_is_local_and_future_queries_never_claim_today(self):
        checks = [
            (datetime(2026, 10, 8, 6, 59, tzinfo=timezone.utc), "today", "Wednesday, October 7, 2026"),
            (datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc), "today", "Thursday, October 8, 2026"),
            (datetime(2026, 10, 8, 6, 59, tzinfo=timezone.utc), "tomorrow", "Thursday, October 8, 2026"),
            (datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc), "Monday", "Monday, October 12, 2026"),
            (datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc), "today", "Sunday, November 1, 2026"),
            (datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc), "today", "Sunday, November 1, 2026"),
        ]
        for now, requested, expected in checks:
            with self.subTest(now=now, requested=requested):
                reply = fitness.plan_command_reply("What's my workout for " + requested,
                                                  sender=OWNER, chat_id=OWNER, is_group=False, now=now)
                prefix = "Today's date is " if requested == "today" else "Workout date: "
                self.assertEqual(reply.splitlines()[0], prefix + expected + ".")
                if requested != "today":
                    self.assertNotIn("today", reply.casefold())
        plan = {**self.plan, "valid_until_local": "2027-01-03"}
        with patch.object(fitness, "load_current", return_value=(plan, "synthetic")):
            reply = fitness.plan_command_reply("What's my workout for tomorrow", sender=OWNER,
                                              chat_id=OWNER, is_group=False,
                                              now=datetime(2027, 1, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(reply.splitlines()[0], "Workout date: Friday, January 1, 2027.")

    def test_quote_validation_retains_old_bounds_and_exact_fields(self):
        for invalid in (None, "", " leading", "trailing ", "x" * 501, "bad\tquote", "bad\x00quote", "é" * 201):
            plan = {**self.plan, "consumer_quote": invalid}
            with self.subTest(quote_type=type(invalid).__name__), self.assertRaises(ValueError):
                fitness.parse_plan(json.dumps(plan))
        plan = {**self.plan, "consumer_quote": "Q" * 500}
        self.assertEqual(fitness.parse_plan(json.dumps(plan)), plan)
        self.assertEqual(extra.EXTRA_ACTIONS["fitness.plan.set"]["fields"]["plan_json"]["maxLength"],
                         fitness.MAX_PLAN_BYTES)
        base = deepcopy(self.plan)
        del base["consumer_quote"]
        raw = fitness.canonical(base)
        with self.assertRaises(ValueError):
            fitness.parse_plan(raw + " " * (fitness.MAX_BASE_PLAN_BYTES - len(raw) + 1))
        for habits, extra_field in ((["x" * 181], {}), (base["daily_habits"], {"quote": "unexpected"})):
            invalid = {**self.plan, "daily_habits": habits, **extra_field}
            with self.assertRaises(ValueError):
                fitness.parse_plan(json.dumps(invalid))

    def test_quote_allowance_cannot_expand_other_plan_data(self):
        plan = deepcopy(self.plan)
        for day in plan["weekly_schedule"].values():
            day["details"] = "D" * 1000
        plan["daily_habits"] = ["H" * 180 for _ in range(12)]
        plan["progression"] = ["P" * 220 for _ in range(8)]
        plan["safety"] = ["S" * 220 for _ in range(6)]
        self.assertGreater(len(fitness.canonical(plan)), fitness.MAX_BASE_PLAN_BYTES)
        with self.assertRaises(ValueError):
            fitness.parse_plan(fitness.canonical(plan))

    def test_demo_labels_remain_truthful_without_private_metadata(self):
        reply = fitness.deterministic_reply("leg press video", sender=OWNER, chat_id=OWNER, is_group=False, now=NOW)
        self.assertTrue(reply.startswith("Leg press\n"))
        self.assertNotIn("provisional", reply)
        self.assertIn(fixtures.URL, reply)
        self.assertIn("playback not checked", reply)
        photo = deepcopy(self.plan)
        photo["verified_sources"]["leg_press_demo"]["media"] = "photo_written_guide"
        with patch.object(fitness, "load_current", return_value=(photo, "synthetic")):
            reply = fitness.deterministic_reply("leg press video", sender=OWNER, chat_id=OWNER, is_group=False, now=NOW)
        self.assertIn("Photo/written form guide", reply)
        self.assertIn("no verified video", reply)


if __name__ == "__main__":
    unittest.main()
