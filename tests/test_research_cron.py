from concurrent.futures import Future
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from davosbot import config, main, research_cron as cron, research_reports as reports, tools
import test_cron_creation_permissions as existing_cron_tests
from test_scheduler_retry import _load_scheduler_helpers

try:
    from cron_occurrence_fixtures import install_legacy_timing_schema
except ModuleNotFoundError as exc:
    if exc.name != "cron_occurrence_fixtures":
        raise
    # The separately reviewed legacy scheduler change is optional on this branch.
    install_legacy_timing_schema = None


OWNER = "+15550000001"
GROUP = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 9, 8, 18, 0, tzinfo=cron.PACIFIC)
REQUEST = "Create a weekly fantasy waiver wire report with the top 10 and why, FAAB budget $100, every Wednesday at 00:59 PT starting upcoming Wednesday through first Wednesday in January"


class _Frozen(datetime):
    value = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


class _InlineExecutor:
    def submit(self, function, *args):
        future = Future()
        future.set_result(function(*args))
        return future


def _sources(now=NOW):
    return [{"id": 1, "url": "https://example.com/waivers", "title": "Current waiver research",
             "content": " ".join(f"Player {number} has a larger role after an injury." for number in range(10)),
             "published": now.date().isoformat()}]


def _waivers():
    return json.dumps({"players": [{"name": f"Player {number}", "reason": "A larger role gives this player useful upside.",
                                     "bid_min": number, "bid_max": number + 2, "source_ids": [1]} for number in range(10)]})


class ResearchCronTests(unittest.TestCase):
    route = existing_cron_tests.CronCreationPermissionTests.route

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "research.sqlite")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""CREATE TABLE cron_jobs (
                id INTEGER PRIMARY KEY, cron_expression TEXT, action_type TEXT, action_payload TEXT,
                enabled INTEGER DEFAULT 1, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_run TEXT);
                CREATE TABLE bot_log(id INTEGER PRIMARY KEY, sender TEXT, event_type TEXT, payload TEXT);""")
        if install_legacy_timing_schema is not None:
            install_legacy_timing_schema(self.db_path)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cron, "_pending", {}))
        self.stack.enter_context(patch.object(main, "BOT_DB_PATH", self.db_path))
        self.stack.enter_context(patch.object(tools, "BOT_DB_PATH", self.db_path))
        self.stack.enter_context(patch("davosbot.permissions.is_owner", lambda sender: sender == OWNER))
        self.stack.enter_context(patch("davosbot.permissions.is_admin", lambda sender: sender in {OWNER, "admin"}))
        self.stack.enter_context(patch.object(cron, "datetime", _Frozen))
        _Frozen.value = NOW

    def save(self, text=REQUEST, sender=OWNER, recipient=GROUP):
        return cron.schedule_from_text(sender, text, recipient, db_path=self.db_path, now_pt=NOW)

    def rows(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT id, cron_expression, action_type, action_payload, enabled, last_run FROM cron_jobs ORDER BY id").fetchall()

    def test_complete_group_request_saves_exact_scoped_row_and_receipt_without_research(self):
        with patch.object(reports, "fetch_sources", side_effect=AssertionError("No research during creation")):
            reply = self.route(REQUEST, group=True)
        row = self.rows()[0]
        payload = json.loads(row[3])
        self.assertEqual(("00:59 wed", "research_report"), row[1:3])
        self.assertEqual((GROUP, "2026-09-09", "2027-01-06", 10, 100),
                         (payload["recipient"], payload["start_date"], payload["end_date"], payload["top_n"], payload["faab_budget"]))
        self.assertIn("#1", reply)
        self.assertIn("final run 2027-01-06 (included)", reply)
        self.assertIn("This request did not send a report", reply)
        self.assertNotIn("run", payload)
        outcome = self.save()
        self.assertEqual(("confirmed", "cron_row_readback"), (outcome.status, outcome.verification_scope))
        self.assertIn("Already saved", outcome.text)
        self.assertEqual(1, len(self.rows()))

    def test_generic_public_report_is_extensible_and_keeps_output_instructions(self):
        request = "Schedule a weekly research report about battery technology every Friday at 9am PT. Compare the practical tradeoffs."
        result = self.save(request, recipient=OWNER)
        self.assertEqual("confirmed", result.status)
        payload = json.loads(self.rows()[0][3])
        self.assertEqual("public_research", payload["kind"])
        self.assertEqual("battery technology", payload["research_topic"])
        self.assertIn("Compare the practical tradeoffs", payload["instructions"])
        self.assertIsNone(payload["end_date"])
        self.assertIn("repeats until cancelled", result.text)
        self.assertIsInstance(cron.parse_creation(request.replace("battery technology", "Central bank news"), GROUP, NOW), tuple)

    def test_native_owner_dm_uses_current_sender_and_legacy_edits_do_not_change_payload(self):
        reply = self.route(REQUEST)
        self.assertIn("Saved research cron #1", reply)
        self.assertEqual(OWNER, json.loads(self.rows()[0][3])["recipient"])
        before = self.rows()
        self.assertIn("haven't changed", tools._edit_cron(1, sender=OWNER, time_pt="09:00"))
        self.assertEqual(before, self.rows())

    def test_creation_receipt_history_requires_successful_dm_or_group_send(self):
        for group, sent, topic in ((False, False, "materials"), (False, True, "storage"),
                                   (True, False, "recycling"), (True, True, "production")):
            with self.subTest(group=group, sent=sent):
                request = f"Schedule a weekly research report about battery {topic} every Friday at 09:00 PT"
                history = Mock()
                before = len(self.rows())
                with patch.object(reports, "fetch_sources", side_effect=AssertionError("Creation cannot run research")):
                    reply = self.route(request, group=group, send_result=sent, history=history)
                self.assertEqual(before + 1, len(self.rows()))
                target = GROUP if group else OWNER
                self.assertEqual(target, json.loads(self.rows()[-1][3])["recipient"])
                self.assertIn("Saved research cron", reply)
                user_text = f"{OWNER}: {request}" if group else request
                expected = [call(target, "user", user_text)]
                if sent:
                    expected.append(call(target, "assistant", reply))
                self.assertEqual(expected, history.call_args_list)

    def test_explicit_out_of_and_total_faab_budget_forms(self):
        template = "Create a weekly fantasy waiver report with top7 and why, {budget}, every Tuesday at 00:59 PT starting upcoming Tuesday through first Tuesday in January"
        for budget in ("$$$ FAAB(out of 200)", "FAAB (out of $200)", "total budget 200", "FAAB total budget $200"):
            with self.subTest(budget=budget):
                parsed = cron.parse_creation(template.format(budget=budget), GROUP, NOW)
                self.assertIsInstance(parsed, tuple)
                self.assertEqual((7, 200), (parsed[1]["top_n"], parsed[1]["faab_budget"]))

    def test_active_duplicate_after_first_run_keeps_original_calendar_receipt(self):
        self.save()
        with closing(sqlite3.connect(self.db_path)) as conn:
            payload = json.loads(self.rows()[0][3])
            payload["run"] = {"date": "2026-09-09", "status": "submitted"}
            conn.execute("UPDATE cron_jobs SET action_payload = ? WHERE id = 1", (json.dumps(payload),))
            conn.commit()
        result = cron.schedule_from_text(OWNER, REQUEST, GROUP, db_path=self.db_path, now_pt=NOW + timedelta(days=8))
        self.assertEqual("confirmed", result.status)
        self.assertIn("Already saved", result.text)
        self.assertIn("First run 2026-09-09", result.text)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual("2026-09-09", json.loads(self.rows()[0][3])["start_date"])

    def test_group_public_research_precedes_market_lookup_and_preserves_access_gates(self):
        text = "@Davos Schedule a weekly research report about Central bank news every Friday at 9am PT"
        with ExitStack() as stack:
            for name in ("handle_group_command", "_get_buffered_image", "_handle_self_status_question", "_handle_model_status_question", "_handle_natural_model_request"):
                stack.enter_context(patch.object(main, name, return_value=None))
            stack.enter_context(patch.object(main, "is_owner", lambda sender: sender == OWNER))
            present = stack.enter_context(patch.object(main, "is_owner_in_chat", return_value=False))
            enabled = stack.enter_context(patch.object(main, "is_gc_enabled", return_value=True))
            stack.enter_context(patch.object(main, "save_turn"))
            stack.enter_context(patch.object(main, "_market_fast_reply", side_effect=AssertionError("Research must reach native creation before market lookup")))
            stack.enter_context(patch.object(main, "get_response", side_effect=AssertionError("Native research must not call conversation model")))
            send = stack.enter_context(patch.object(main, "send_message", return_value=True))
            main.handle_group_message(OWNER, GROUP, text)
            self.assertEqual([], self.rows())
            present.return_value = True
            enabled.return_value = False
            main.handle_group_message(OWNER, GROUP, text)
            self.assertEqual([], self.rows())
            enabled.return_value = True
            main.handle_group_message(OWNER, GROUP, text.removeprefix("@Davos "))
            self.assertEqual([], self.rows())
            main.handle_group_message(OWNER, GROUP, text)
            self.assertEqual(1, len(self.rows()))
            self.assertEqual(GROUP, send.call_args.args[0])
            self.assertIn("Saved research cron", send.call_args.args[1])

    def test_owner_only_missing_context_and_unsupported_requests_never_write(self):
        for sender in ("admin", "friend", "unknown"):
            self.assertEqual("denied", self.save(sender=sender).status)
        self.assertEqual("failed", self.save(recipient="").status)
        bad = (
            REQUEST.replace("through", "stop on"),
            REQUEST.replace("Wednesday at", "Wednesday and Friday at", 1),
            REQUEST.replace("00:59 PT", "00:59 and 9pm PT"),
            REQUEST.replace("00:59 PT", "00:59 ET"),
            REQUEST.replace("every Wednesday", "every other Wednesday"),
            REQUEST.replace("every Wednesday", "every second Wednesday"),
            REQUEST.replace("00:59 PT", "8 and 9pm PT"),
            REQUEST.replace("top 10", "top 20"),
            REQUEST.replace("top 10", "top 10 or top 5"),
            REQUEST.replace("budget $100", "budget $100 or budget $200"),
            REQUEST.replace("budget $100", "budget $100.50"),
            REQUEST.replace("FAAB budget $100,", ""),
            REQUEST + " and buy the best one",
            REQUEST + " in another group",
            REQUEST + " to Alice",
            REQUEST + " using my Gmail inbox",
            REQUEST.replace("through first Wednesday in January", "through 2026-09-01"),
            REQUEST.replace("through first Wednesday in January", "through 2027-02-30"),
            REQUEST.replace("starting upcoming Wednesday", "starting after football starts"),
            REQUEST + "; don't schedule it yet",
        )
        for text in bad:
            with self.subTest(text=text):
                self.assertEqual("failed", self.save(text).status)
        self.assertEqual([], self.rows())

    def test_preserves_existing_actions_and_noncreation_conversation(self):
        for request in ("create a weekly bot health report cron at 8am", "create sports recap cron at 8am",
                        "what is a research cron?", "cancel research cron #1", "don't stop research cron #1"):
            self.assertIsNone(cron.schedule_from_text(OWNER, request, GROUP, db_path=self.db_path))

    def test_calendar_rollover_same_day_and_leap_year_are_explicit(self):
        for now, end_phrase, first, last in (
            (NOW, "first Wednesday in January", "2026-09-09", "2027-01-06"),
            (datetime(2026, 12, 31, 18, tzinfo=cron.PACIFIC), "first Wednesday in Jan", "2027-01-06", "2027-01-06"),
            (datetime(2026, 9, 9, 0, 58, tzinfo=cron.PACIFIC), "2026-09-09", "2026-09-09", "2026-09-09"),
            (datetime(2026, 9, 9, 1, 0, tzinfo=cron.PACIFIC), "2026-09-16", "2026-09-16", "2026-09-16"),
            (datetime(2028, 2, 28, 12, tzinfo=cron.PACIFIC), "2028-03-08", "2028-03-01", "2028-03-08"),
        ):
            with self.subTest(now=now):
                parsed = cron.parse_creation(REQUEST.replace("first Wednesday in January", end_phrase), GROUP, now)
                self.assertIsInstance(parsed, tuple)
                expr, payload = parsed
                self.assertEqual(first, payload["start_date"])
                self.assertEqual(last, cron.first_and_last(expr, payload)[1].isoformat())
        parsed = cron.parse_creation(REQUEST.replace("upcoming Wednesday", "2026-09-02"), GROUP,
                                     datetime(2026, 9, 9, 0, 58, tzinfo=cron.PACIFIC))
        self.assertEqual("2026-09-09", parsed[1]["start_date"])

    def test_malformed_unrelated_row_cannot_block_creation_and_saved_run_dedupes(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO cron_jobs (action_type, action_payload, enabled, created_by) VALUES ('research_report', 'bad-json', 1, 'owner')")
            conn.commit()
        self.assertEqual("confirmed", self.save().status)
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = self.rows()[1]
            payload = json.loads(row[3])
            payload["run"] = {"date": "2026-09-09", "status": "submitted"}
            conn.execute("UPDATE cron_jobs SET action_payload = ? WHERE id = ?", (json.dumps(payload), row[0]))
            conn.commit()
        self.assertIn("Already saved", self.save().text)
        self.assertEqual(2, len(self.rows()))

    def test_list_and_describe_show_scope_date_window_and_faab(self):
        self.save()
        listed = tools._list_crons(GROUP, requester_id=OWNER)
        self.assertIn("FAAB budget $100", listed)
        self.assertIn("final run 2027-01-06", listed)
        self.assertNotIn("research_report", tools._list_crons("other-chat", requester_id=OWNER))
        described = tools._describe_cron_from_text(OWNER, "describe cron #1", GROUP)
        self.assertIn("submission only", described)

    def run_at(self, when, send, *, executor=None, helpers=None):
        helpers = helpers if helpers is not None else _load_scheduler_helpers(send)
        helpers["BOT_DB_PATH"] = self.db_path
        helpers["time"] = Mock(time=lambda: when.timestamp(), monotonic=lambda: when.timestamp())
        _Frozen.value = when
        with patch("datetime.datetime", _Frozen), patch.object(reports, "_executor", executor or _InlineExecutor()):
            helpers["_check_cron_jobs"]()
        return helpers

    def test_throttled_scheduler_recovers_a_missed_minute_with_current_research(self):
        self.save()
        send = Mock(return_value=True)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()) as synth:
            helpers = self.run_at(fire - timedelta(seconds=5), send)
            self.run_at(fire + timedelta(seconds=35), send, helpers=helpers)
            fetch.assert_not_called()  # The real 50-second scheduler gate skipped this tick.
            actual = fire + timedelta(minutes=1, seconds=10)
            self.run_at(actual, send, helpers=helpers)
            self.run_at(actual + timedelta(minutes=2), send, helpers=helpers)
        fetch.assert_called_once()
        synth.assert_called_once()
        self.assertEqual(actual, fetch.call_args.args[1])
        self.assertEqual(actual, synth.call_args.args[2])
        self.assertEqual("2026-09-09", json.loads(self.rows()[0][3])["run"]["date"])
        send.assert_called_once()
        self.assertEqual(GROUP, send.call_args.args[0])
        self.assertIn("2026-09-09 01:00", send.call_args.args[1])
        self.assertIn("https://example.com/waivers", send.call_args.args[1])

    def test_long_outage_skips_old_occurrence_but_next_week_can_catch_up(self):
        self.save()
        send = Mock(return_value=True)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()):
            self.run_at(fire + timedelta(minutes=16), send)
            fetch.assert_not_called()
            self.run_at(fire + timedelta(days=7, minutes=4), send)
            self.run_at(fire + timedelta(days=7, minutes=8), send)
        fetch.assert_called_once()
        send.assert_called_once()
        self.assertEqual("2026-09-16", json.loads(self.rows()[0][3])["run"]["date"])

    def test_clock_rollback_cannot_replay_an_older_week_or_replace_its_successor(self):
        self.save()
        send = Mock(return_value=True)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()):
            self.run_at(fire, send)
            self.run_at(fire + timedelta(days=7, minutes=2), send)
            saved = self.rows()[0]
            # A restarted scheduler has a fresh throttle but retains its receipt.
            self.run_at(fire + timedelta(minutes=4), send)
        self.assertEqual(2, fetch.call_count)
        self.assertEqual(2, send.call_count)
        self.assertEqual(saved, self.rows()[0])
        self.assertEqual("2026-09-16", json.loads(saved[3])["run"]["date"])

    def test_final_midnight_grace_uses_scheduled_date_and_expires_after_window(self):
        request = REQUEST.replace("00:59", "23:59").replace(
            "starting upcoming Wednesday through first Wednesday in January", "starting 2027-01-06 through 2027-01-06",
        )
        self.save(request)
        send = Mock(return_value=True)
        actual = datetime(2027, 1, 7, 0, 5, tzinfo=cron.PACIFIC)
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()):
            self.run_at(actual, send)
            self.run_at(actual + timedelta(minutes=2), send)
            self.assertEqual(1, self.rows()[0][4])
            self.run_at(actual + timedelta(minutes=10), send)
        fetch.assert_called_once()
        send.assert_called_once()
        payload = json.loads(self.rows()[0][3])
        self.assertEqual("2027-01-06", payload["run"]["date"])
        self.assertEqual("submitted", payload["run"]["status"])
        self.assertEqual(0, self.rows()[0][4])
        self.assertIn("2027-01-07 00:05", send.call_args.args[1])

    def test_new_schedule_during_grace_does_not_run_retroactively(self):
        created = datetime(2026, 9, 10, 0, 4, tzinfo=cron.PACIFIC)
        request = "Schedule a weekly research report about battery storage every Wednesday at 23:59 PT"
        result = cron.schedule_from_text(OWNER, request, GROUP, db_path=self.db_path, now_pt=created)
        self.assertIsNotNone(result)
        self.assertEqual("2026-09-16", json.loads(self.rows()[0][3])["start_date"])
        send = Mock()
        with patch.object(reports, "fetch_sources") as fetch:
            self.run_at(created + timedelta(minutes=1), send)
        fetch.assert_not_called()
        send.assert_not_called()

    def test_final_admitted_worker_can_finish_after_the_grace_window(self):
        request = REQUEST.replace("00:59", "23:59").replace(
            "starting upcoming Wednesday through first Wednesday in January", "starting 2027-01-06 through 2027-01-06",
        )
        self.save(request)
        send = Mock(return_value=True)
        executor = Mock()
        admitted = datetime(2027, 1, 7, 0, 13, tzinfo=cron.PACIFIC)
        self.run_at(admitted, send, executor=executor)
        function, *args = executor.submit.call_args.args
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)), patch.object(reports, "synthesize", return_value=_waivers()):
            try:
                self.run_at(admitted + timedelta(minutes=2), send, executor=executor)
                self.assertEqual(1, self.rows()[0][4])
                self.assertEqual("queued", json.loads(self.rows()[0][3])["run"]["status"])
                executor.submit.assert_called_once()
            finally:
                function(*args)  # Finish the admitted worker and release its slot.
        send.assert_called_once()
        self.assertEqual("submitted", json.loads(self.rows()[0][3])["run"]["status"])
        self.run_at(admitted + timedelta(minutes=3), send, executor=executor)
        self.assertEqual(0, self.rows()[0][4])
        executor.submit.assert_called_once()
        send.assert_called_once()

    def test_deferred_capacity_can_admit_once_on_a_later_grace_tick(self):
        self.save()
        send = Mock(return_value=True)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        slots = Mock(acquire=Mock(side_effect=[False, True]))
        with patch.object(reports, "_slots", slots), patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()):
            helpers = self.run_at(fire, send)
            self.assertNotIn("run", json.loads(self.rows()[0][3]))
            fetch.assert_not_called()
            self.run_at(fire + timedelta(minutes=2), send, helpers=helpers)
            self.run_at(fire + timedelta(minutes=4), send, helpers=helpers)
        self.assertEqual(2, slots.acquire.call_count)
        slots.release.assert_called_once()
        fetch.assert_called_once()
        send.assert_called_once()
        self.assertEqual("submitted", json.loads(self.rows()[0][3])["run"]["status"])

    def test_grace_failure_notice_uses_occurrence_date_for_final_run_claim(self):
        request = REQUEST.replace("00:59", "23:59").replace(
            "starting upcoming Wednesday through first Wednesday in January", "starting 2027-01-06 through 2027-01-13",
        )
        self.save(request)
        send = Mock(return_value=True)
        with patch.object(reports, "fetch_sources", side_effect=reports.ReportFailure("no_recent_sources")):
            self.run_at(datetime(2027, 1, 7, 0, 5, tzinfo=cron.PACIFIC), send)
            self.assertIn("next weekly run", send.call_args.args[1])
            self.run_at(datetime(2027, 1, 14, 0, 5, tzinfo=cron.PACIFIC), send)
        self.assertEqual(2, send.call_count)
        self.assertIn("final scheduled run", send.call_args.args[1])
        self.assertEqual("2027-01-13", json.loads(self.rows()[0][3])["run"]["date"])

    def test_grace_boundaries_never_start_early_or_after_fifteen_minutes(self):
        expr, payload = cron.parse_creation(REQUEST, GROUP, NOW)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        self.assertIsNone(reports._due_occurrence(expr, payload, fire - timedelta(microseconds=1)))
        self.assertEqual(fire, reports._due_occurrence(expr, payload, fire))
        self.assertEqual(fire, reports._due_occurrence(expr, payload, fire + timedelta(minutes=15)))
        self.assertIsNone(reports._due_occurrence(expr, payload, fire + timedelta(minutes=15, microseconds=1)))

    def test_nonexistent_spring_time_is_skipped_and_next_valid_week_runs(self):
        request = REQUEST.replace("Wednesday", "Sunday").replace("00:59", "02:30").replace(
            "starting upcoming Sunday through first Sunday in January", "starting 2027-03-14 through 2027-03-21",
        )
        self.save(request)
        send = Mock(return_value=True)
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()):
            self.run_at(datetime(2027, 3, 14, 3, 35, tzinfo=cron.PACIFIC), send)
            fetch.assert_not_called()
            self.run_at(datetime(2027, 3, 21, 2, 35, tzinfo=cron.PACIFIC), send)
        fetch.assert_called_once()
        send.assert_called_once()
        self.assertEqual("2027-03-21", json.loads(self.rows()[0][3])["run"]["date"])

    def test_unclaimed_fall_back_second_fold_does_not_create_an_extra_occurrence(self):
        request = REQUEST.replace("Wednesday", "Sunday").replace("00:59", "01:30").replace(
            "starting upcoming Sunday through first Sunday in January", "starting 2026-11-01 through 2026-11-08",
        )
        self.save(request)
        send = Mock(return_value=True)
        repeated = datetime(2026, 11, 1, 1, 30, tzinfo=cron.PACIFIC, fold=1)
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()):
            self.run_at(repeated, send)
            fetch.assert_not_called()
            self.run_at(datetime(2026, 11, 8, 1, 35, tzinfo=cron.PACIFIC), send)
        fetch.assert_called_once()
        send.assert_called_once()
        self.assertEqual("2026-11-08", json.loads(self.rows()[0][3])["run"]["date"])

    def test_actual_scheduler_fetches_fresh_at_fire_and_sends_current_group_once(self):
        self.save()
        send = Mock(return_value=True)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        with patch.object(reports, "fetch_sources", side_effect=lambda payload, now: _sources(now)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()) as synth:
            self.run_at(fire - timedelta(days=1), send)
            fetch.assert_not_called()
            self.run_at(fire, send)
            self.run_at(fire + timedelta(seconds=51), send)
            self.assertEqual(1, fetch.call_count)
            self.assertEqual(1, send.call_count)
            self.assertEqual(GROUP, send.call_args.args[0])
            self.assertTrue(send.call_args.kwargs["is_group"])
            self.assertEqual("none", send.call_args.kwargs["recovery_mode"])
            self.assertIn("https://example.com/waivers", send.call_args.args[1])
            self.assertIn("FAAB budget $100", send.call_args.args[1])
            run = json.loads(self.rows()[0][3])["run"]
            self.assertEqual(("confirmed", "submitted", "imessage_submission"), (run["generation"], run["delivery"], run["verification_scope"]))
            self.assertIsNotNone(self.rows()[0][5])
            self.run_at(fire + timedelta(days=7), send)
            self.assertEqual(2, fetch.call_count)
            self.assertEqual((fire + timedelta(days=7)).date().isoformat(), synth.call_args.args[1][0]["published"])

    def test_scheduler_aggregate_public_http_synthesis_and_captured_submission(self):
        self.save()
        send = Mock(return_value=True)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        def public_provider(url, **kwargs):
            date_string = _Frozen.value.date().isoformat()
            if url == "https://api.tavily.com/search":
                self.assertIn(date_string, kwargs["json"]["query"])
                source = _sources(_Frozen.value)[0]
                return Mock(json=lambda: {"results": [dict(source, published_date=_Frozen.value.isoformat())]})
            self.assertTrue(url.startswith("https://generativelanguage.googleapis.com/"))
            self.assertNotIn("tools", kwargs["json"])
            prompt = json.loads(kwargs["json"]["contents"][0]["parts"][0]["text"])
            self.assertEqual(date_string, prompt["sources"][0]["published"])
            self.assertNotIn(GROUP, json.dumps(prompt))
            data = json.loads(_waivers())
            for entry in data["players"]:
                entry["reason"] = f"The supplied {date_string} source describes a larger role."
            return Mock(json=lambda: {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(data)}]}}], "usageMetadata": {}})
        with patch.object(config, "TAVILY_API_KEY", "fixture-key"), patch.object(config, "GEMINI_API_KEY", "fixture-key"), patch("davosbot.billing.check_gemini_budget", return_value=Mock(allowed=True)), patch("davosbot.billing.log_gemini_usage"), patch.object(reports.requests, "post", side_effect=public_provider) as post:
            first_helpers = self.run_at(fire, send)
            next_helpers = self.run_at(fire + timedelta(days=7), send)
        self.assertEqual(6, post.call_count)
        self.assertEqual(2, send.call_count)
        first, second = (call.args[1] for call in send.call_args_list)
        self.assertIn("2026-09-09 source", first)
        self.assertIn("2026-09-16 source", second)
        self.assertIn("https://example.com/waivers", second)
        self.assertIn("Player 9", second)
        self.assertEqual("submitted", json.loads(self.rows()[0][3])["run"]["status"])
        first_helpers["save_turn"].assert_called_once_with(GROUP, "assistant", first)
        next_helpers["save_turn"].assert_called_once_with(GROUP, "assistant", second)

    def test_worker_failure_and_ambiguous_delivery_never_replay_same_occurrence(self):
        self.save()
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        send = Mock(return_value=False)
        with patch.object(reports, "fetch_sources", return_value=_sources()), patch.object(reports, "synthesize", return_value=_waivers()):
            self.run_at(fire, send)
            self.run_at(fire + timedelta(seconds=51), send)
            self.run_at(fire + timedelta(minutes=8), send)
        self.assertEqual(1, send.call_count)
        self.assertEqual("delivery_unverified", json.loads(self.rows()[0][3])["run"]["status"])
        self.assertIsNone(self.rows()[0][5])
        with patch.object(reports, "fetch_sources", side_effect=reports.ReportFailure("no_recent_sources")):
            self.run_at(fire + timedelta(days=7), send)
        self.assertEqual(2, send.call_count)
        self.assertIn("couldn't produce a complete report", send.call_args.args[1])
        self.assertEqual("failed", json.loads(self.rows()[0][3])["run"]["status"])

    def test_final_date_included_then_disabled_without_research(self):
        self.save()
        send = Mock(return_value=True)
        final = datetime(2027, 1, 6, 0, 59, tzinfo=cron.PACIFIC)
        with patch.object(reports, "fetch_sources", return_value=_sources(final)) as fetch, patch.object(reports, "synthesize", return_value=_waivers()):
            self.run_at(final, send)
            self.run_at(final + timedelta(days=7), send)
        self.assertEqual(1, fetch.call_count)
        self.assertEqual(1, send.call_count)
        self.assertEqual(0, self.rows()[0][4])

    def test_failed_final_occurrence_does_not_promise_a_future_run(self):
        self.save()
        send = Mock(return_value=True)
        with patch.object(reports, "fetch_sources", side_effect=reports.ReportFailure("no_recent_sources")):
            self.run_at(datetime(2027, 1, 6, 0, 59, tzinfo=cron.PACIFIC), send)
        send.assert_called_once()
        self.assertIn("final scheduled run", send.call_args.args[1])
        self.assertNotIn("next weekly run", send.call_args.args[1])

    def test_background_submission_does_not_block_other_due_crons(self):
        self.save()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) VALUES ('00:59 wed', 'morning_message', ?, 1, 'owner')", (json.dumps({"recipient": OWNER}),))
            conn.commit()
        queued = []
        executor = Mock(submit=lambda function, *args: queued.append((function, args)))
        send = Mock(return_value=True)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        with patch.object(reports, "fetch_sources", side_effect=AssertionError("Must not run in poll loop")), patch.object(tools, "_get_inspirational_quote", return_value="old action"), patch.object(tools, "_render_morning_message_body", return_value="old action"):
            self.run_at(fire, send, executor=executor)
        self.assertEqual(1, len(queued))
        mode = "none" if install_legacy_timing_schema is not None else "inline"
        send.assert_called_once_with(OWNER, "old action", is_group=False, recovery_mode=mode)
        # Complete the queued fixture to release the bounded worker slot.
        with patch.object(reports, "fetch_sources", side_effect=reports.ReportFailure("fixture_complete")):
            queued[0][0](*queued[0][1])

    def test_cancel_during_research_prevents_outbound_send(self):
        self.save()
        def cancel_then_return(*args):
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute("UPDATE cron_jobs SET enabled = 0 WHERE id = 1")
                conn.commit()
            return _waivers()
        send = Mock(return_value=True)
        with patch.object(reports, "fetch_sources", return_value=_sources()), patch.object(reports, "synthesize", side_effect=cancel_then_return):
            self.run_at(datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC), send)
        send.assert_not_called()
        self.assertEqual("cancelled_or_changed", json.loads(self.rows()[0][3])["run"]["status"])

    def test_checkpointed_submission_history_failure_never_replays(self):
        self.save()
        send = Mock(return_value=True)
        def remember(job_id, recipient, report):
            self.assertEqual((1, GROUP), (job_id, recipient))
            self.assertIn("Player 9", report)
            self.assertIsNotNone(self.rows()[0][5])
            raise RuntimeError("fixture history write failure")
        callback = Mock(side_effect=remember)
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        with patch.object(reports, "_executor", _InlineExecutor()), patch.object(reports, "fetch_sources", return_value=_sources()), patch.object(reports, "synthesize", return_value=_waivers()):
            self.assertTrue(reports.dispatch(self.db_path, 1, fire, send, on_submitted=callback))
            self.assertFalse(reports.dispatch(self.db_path, 1, fire, send, on_submitted=callback))
        self.assertEqual(1, send.call_count)
        self.assertEqual(1, callback.call_count)
        self.assertEqual("submitted", json.loads(self.rows()[0][3])["run"]["status"])

    def test_changed_or_deleted_row_at_send_transition_prevents_outbound(self):
        self.save()
        initial = self.rows()[0][3]
        send = Mock(return_value=True)
        original_finish = reports._finish
        for mutation in ("disable", "owner", "schedule", "payload", "claim", "delete"):
            with self.subTest(mutation=mutation):
                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute("DELETE FROM cron_jobs")
                    conn.execute("INSERT INTO cron_jobs (id, cron_expression, action_type, action_payload, enabled, created_by) VALUES (1, '00:59 wed', 'research_report', ?, 1, 'owner')", (initial,))
                    conn.commit()
                def interleave(*args, **kwargs):
                    if args[3].get("status") == "sending":
                        with closing(sqlite3.connect(self.db_path)) as conn:
                            if mutation == "delete":
                                conn.execute("DELETE FROM cron_jobs WHERE id = 1")
                            elif mutation == "disable":
                                conn.execute("UPDATE cron_jobs SET enabled = 0 WHERE id = 1")
                            elif mutation == "owner":
                                conn.execute("UPDATE cron_jobs SET created_by = 'admin' WHERE id = 1")
                            elif mutation == "schedule":
                                conn.execute("UPDATE cron_jobs SET cron_expression = '01:00 wed' WHERE id = 1")
                            else:
                                payload = json.loads(conn.execute("SELECT action_payload FROM cron_jobs WHERE id = 1").fetchone()[0])
                                if mutation == "claim":
                                    payload["run"]["id"] = "replacement-claim"
                                else:
                                    payload["recipient"] = OWNER
                                conn.execute("UPDATE cron_jobs SET action_payload = ? WHERE id = 1", (json.dumps(payload),))
                            conn.commit()
                    return original_finish(*args, **kwargs)
                with patch.object(reports, "_finish", side_effect=interleave), patch.object(reports, "fetch_sources", return_value=_sources()), patch.object(reports, "synthesize", return_value=_waivers()):
                    self.run_at(datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC), send)
                send.assert_not_called()

    def test_failure_notice_requires_committed_unchanged_row(self):
        self.save()
        send = Mock(return_value=True)
        original_finish = reports._finish
        def interleave(*args, **kwargs):
            if args[3].get("delivery") == "notice_pending":
                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute("UPDATE cron_jobs SET enabled = 0 WHERE id = 1")
                    conn.commit()
            return original_finish(*args, **kwargs)
        with patch.object(reports, "_finish", side_effect=interleave), patch.object(reports, "fetch_sources", side_effect=reports.ReportFailure("no_recent_sources")):
            self.run_at(datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC), send)
        send.assert_not_called()

    def test_capacity_preserves_eligibility_and_worker_start_failure_stays_durable(self):
        fire = datetime(2026, 9, 9, 0, 59, tzinfo=cron.PACIFIC)
        self.save()
        send = Mock()
        slots = Mock(acquire=Mock(return_value=False))
        with patch.object(reports, "_slots", slots):
            self.assertFalse(reports.dispatch(self.db_path, 1, fire, send))
        self.assertNotIn("run", json.loads(self.rows()[0][3]))
        slots.release.assert_not_called()
        executor = Mock(submit=Mock(side_effect=RuntimeError("fixture startup failure")))
        with patch.object(reports, "_executor", executor):
            self.assertFalse(reports.dispatch(self.db_path, 1, fire + timedelta(days=7), send))
            self.assertFalse(reports.dispatch(self.db_path, 1, fire + timedelta(days=7), send))
        self.assertEqual(1, executor.submit.call_count)
        self.assertEqual("worker_start_failed", json.loads(self.rows()[0][3])["run"]["error"])
        send.assert_not_called()


class ResearchProviderTests(unittest.TestCase):
    def setUp(self):
        self.expr, self.payload = cron.parse_creation(REQUEST, GROUP, NOW)

    def test_search_filters_old_future_and_private_sources_and_caps_requests(self):
        rows = [
            {"url": "https://example.com/current", "title": "Current", "content": "Useful evidence", "published_date": NOW.isoformat()},
            {"url": "https://example.com/old", "content": "Old", "published_date": (NOW - timedelta(days=9)).isoformat()},
            {"url": "https://example.com/future", "content": "Future", "published_date": (NOW + timedelta(days=1)).isoformat()},
            {"url": "https://127.0.0.1/private", "content": "Not public", "published_date": NOW.isoformat()},
            {"url": "https://example.com/undated", "content": "Unknown date"},
        ]
        response = Mock(json=lambda: {"results": rows})
        with patch.object(config, "TAVILY_API_KEY", "fixture-key"), patch.object(reports.requests, "post", return_value=response) as post:
            sources = reports.fetch_sources(self.payload, NOW)
        self.assertEqual(["https://example.com/current"], [source["url"] for source in sources])
        self.assertEqual(2, post.call_count)
        for call in post.call_args_list:
            self.assertEqual("2026-09-01", call.kwargs["json"]["start_date"])
            self.assertFalse(call.kwargs["json"]["include_raw_content"])
            self.assertEqual(15, call.kwargs["timeout"])

    def test_model_has_no_tools_or_private_context_and_keeps_budget_accounting(self):
        response = Mock(json=lambda: {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": _waivers()}]}}], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 200, "totalTokenCount": 300}})
        with patch.object(config, "GEMINI_API_KEY", "fixture-key"), patch("davosbot.billing.check_gemini_budget", return_value=Mock(allowed=True)), patch("davosbot.billing.log_gemini_usage") as usage, patch.object(reports.requests, "post", return_value=response) as post:
            self.assertEqual(_waivers(), reports.synthesize(self.payload, _sources(), NOW))
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("tools", payload)
        self.assertEqual(4096, payload["generationConfig"]["maxOutputTokens"])
        self.assertEqual("application/json", payload["generationConfig"]["responseMimeType"])
        self.assertNotIn(GROUP, json.dumps(payload))
        usage.assert_called_once_with(100, 200, 300, "research_report")

    def test_billing_denial_and_token_capped_report_stop_before_delivery(self):
        with patch.object(config, "GEMINI_API_KEY", "fixture-key"), patch("davosbot.billing.check_gemini_budget", return_value=Mock(allowed=False)), patch.object(reports.requests, "post") as post:
            with self.assertRaises(reports.ReportFailure):
                reports.synthesize(self.payload, _sources(), NOW)
            post.assert_not_called()
        response = Mock(json=lambda: {"candidates": [{"finishReason": "MAX_TOKENS"}], "usageMetadata": {}})
        with patch.object(config, "GEMINI_API_KEY", "fixture-key"), patch("davosbot.billing.check_gemini_budget", return_value=Mock(allowed=True)), patch("davosbot.billing.log_gemini_usage"), patch.object(reports.requests, "post", return_value=response):
            with self.assertRaisesRegex(reports.ReportFailure, "incomplete_report"):
                reports.synthesize(self.payload, _sources(), NOW)

    def test_citations_targets_and_budget_must_validate(self):
        for field, value in (("name", "Invented Player"), ("source_ids", [99]), ("bid_min", -1), ("bid_max", 101), ("reason", "See https://invented.example")):
            data = json.loads(_waivers())
            data["players"][0][field] = value
            with self.subTest(field=field), self.assertRaises(reports.ReportFailure):
                reports.render_report(self.payload, _sources(), json.dumps(data), NOW)
        data = json.loads(_waivers())
        data["players"].pop()
        with self.assertRaisesRegex(reports.ReportFailure, "insufficient_waiver_targets"):
            reports.render_report(self.payload, _sources(), json.dumps(data), NOW)
        rendered = reports.render_report(self.payload, _sources(), _waivers(), NOW)
        self.assertIn("Player 9", rendered)
        self.assertIn("[1]", rendered)
        self.assertIn("https://example.com/waivers", rendered)

    def test_generic_summary_and_findings_require_real_source_ids(self):
        _, payload = cron.parse_creation("Schedule a weekly research report about battery technology every Friday at 9am PT", GROUP, NOW)
        data = {"summary": "A new design shows promise.", "source_ids": [1], "findings": [
            {"heading": "New design", "detail": "Results remain preliminary.", "source_ids": [1]}]}
        report = reports.render_report(payload, _sources(), json.dumps(data), NOW)
        self.assertIn("A new design shows promise. [1]", report)
        del data["source_ids"]
        with self.assertRaisesRegex(reports.ReportFailure, "invalid_report_sources"):
            reports.render_report(payload, _sources(), json.dumps(data), NOW)


if __name__ == "__main__":
    unittest.main()
