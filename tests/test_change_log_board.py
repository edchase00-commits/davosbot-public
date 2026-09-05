import ast
import json
import logging
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_change_log_helpers():
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    triage = ast.parse((ROOT / "davosbot" / "change_log_triage.py").read_text(encoding="utf-8"))
    names = {
        "_classify_change_request",
        "_change_log_row_parts",
        "_bucket_change_log_rows",
        "_truncate_log_display",
        "_format_bucket_item",
        "_format_change_log_board",
        "_change_log_export_content",
        "_write_change_log_export",
        "_refresh_change_log_export",
        "_format_safe_cleanup_plan",
        "_fetch_change_log_rows",
        "_looks_like_cleanup_prompt_confirmation",
        "_looks_like_confirmed_cleanup_run",
        "_latest_loggable_turn",
        "_log_payload_from_subcommand",
        "_format_logged_change_reply",
        "_rewrite_change_log_delete_alias",
        "_rewrite_change_log_update_alias",
        "_looks_like_big_change_intake",
        "_parse_big_change_intake",
        "_build_big_change_intake",
        "_cmd_big_change_intake",
        "_looks_like_self_repair_intake",
        "_normalize_self_repair_request",
        "_parse_self_repair_issue",
        "_self_repair_issue_needs_clarification",
        "_classify_self_repair_issue",
        "_self_repair_risk",
        "_self_repair_prompts",
        "_truncate_self_repair_field",
        "_self_repair_table_columns",
        "_self_repair_table_exists",
        "_summarize_self_repair_payload",
        "_recent_bot_log_context",
        "_self_repair_count",
        "_self_repair_latest_rows",
        "_self_repair_db_snapshots",
        "_self_repair_likely_code_area",
        "_self_repair_expected_behavior",
        "_build_self_repair_intake",
        "_log_self_repair_intake",
        "_cmd_self_repair_intake",
        "_cmd_capabilities",
        "_cmd_log",
    }
    nodes = []
    for node in [*triage.body, *tree.body]:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if (
                "_BIG_CHANGE_INTAKE_RE" in target_names
                or "_SELF_REPAIR_INTAKE_RE" in target_names
                or "_SELF_REPAIR_ANALYZE_VERB_RE" in target_names
                or "_CLEANUP_PROMPT_CONFIRM_RE" in target_names
                or "_CONFIRMED_CLEANUP_RUN_RE" in target_names
                or "_LOG_BOARD_ITEM_PREVIEW_CHARS" in target_names
                or "_LOG_REPLY_PREVIEW_CHARS" in target_names
                or "OWNER_COMMANDS" in target_names
            ):
                nodes.append(node)
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "json": json,
        "sqlite3": sqlite3,
        "closing": closing,
        "BOT_DB_PATH": "",
        "check_action_permission": lambda sender, action: None,
        "redact_secret": lambda text: text.replace("sk-test", "[REDACTED]"),
        "is_owner": lambda sender: sender == "+15550000001",
        "is_admin": lambda sender: sender == "+15550000001",
        "choose_generation_provider": lambda: "local",
        "choose_scan_provider": lambda: "gemini",
        "Path": Path,
        "datetime": __import__("datetime").datetime,
        "logger": logging.getLogger("test_change_log_board"),
        "__file__": str(ROOT / "davosbot" / "commands.py"),
    }
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


class ChangeLogBoardTests(unittest.TestCase):
    def test_polite_bot_addressed_repair_requests_keep_issue_text(self):
        helpers = _load_change_log_helpers()
        for text in ("Davos fix phone number formatting", "please fix yourself: phone numbers are wrong", "can you fix your phone number formatting", "hey Davos fix phone number formatting", "fix davos phone number formatting"):
            with self.subTest(text=text):
                self.assertTrue(helpers["_looks_like_self_repair_intake"](text))
                self.assertIn("phone number", helpers["_parse_self_repair_issue"](text))
        for text in ("please fix my resume", "fix this sentence: hello", "Davos can you order wings", "Davos fix this sentence: I has wings", "please Davos fix my resume"):
            self.assertFalse(helpers["_looks_like_self_repair_intake"](text))

    def setUp(self):
        helpers = _load_change_log_helpers()
        self.helpers = helpers
        self.helpers["_refresh_change_log_export"] = (
            lambda: "\nSnapshot refreshed for SSH: exports/private/change_log_board.md"
        )
        self.board = helpers["_format_change_log_board"]
        self.plan = helpers["_format_safe_cleanup_plan"]

    def test_board_groups_rows_by_color(self):
        rows = [
            (3, "touch permissions.py admin password gate", "", "2026-05-07 01:00:00"),
            (2, "cron list UX for all chats", "", "2026-05-07 00:30:00"),
            (1, "docs cleanup and help text wording", "", "2026-05-07 00:00:00"),
        ]

        text = self.board(rows)

        self.assertIn("Triage board: GREEN 1 | YELLOW 1 | RED 1", text)
        self.assertIn("GREEN - safe Codex batch candidates", text)
        self.assertIn("YELLOW - review one at a time", text)
        self.assertIn("RED - no phone shipping", text)
        self.assertIn("#1 (2026-05-07): docs cleanup", text)

    def test_explicit_red_log_language_stays_red(self):
        classify = self.helpers["_classify_change_request"]

        self.assertEqual("red", classify("P0 red log: bot lied about reminder state"))
        self.assertEqual("red", classify("log red image scan gaslighting failure"))

    def test_explicit_bracketed_risk_prefix_wins(self):
        classify = self.helpers["_classify_change_request"]

        self.assertEqual("red", classify("[SELF-REPAIR RED] analyze this cron issue"))
        self.assertEqual("yellow", classify("[GROUP-ERROR YELLOW] summarize this issue"))
        self.assertEqual("green", classify("[DOCS GREEN] cleanup wording only"))
        self.assertEqual("red", classify("mark it red: dangerous model routing bug"))

    def test_empty_board_shows_logging_guidance(self):
        text = self.board([])

        self.assertIn("Change log is empty.", text)
        self.assertIn("Use `log [thing]`", text)
        self.assertIn("`analyze this and log`", text)
        self.assertIn("`ship safe cleanup`", text)

    def test_board_redacts_inline_tokens(self):
        rows = [(1, "image failed token=abc123", "password: sk-test", "2026-05-07 00:00:00")]

        text = self.board(rows)

        self.assertNotIn("abc123", text)
        self.assertNotIn("sk-test", text)
        self.assertIn("token=[redacted]", text)
        self.assertIn("password=[redacted]", text)

    def test_phone_board_truncates_massive_rows_but_export_keeps_full_text(self):
        giant = "Image scan failed. " + ("A" * 1800)
        rows = [(1, giant, "", "2026-05-07 00:00:00")]

        phone = self.board(rows)
        export = self.helpers["_change_log_export_content"](rows)

        self.assertIn("[truncated;", phone)
        self.assertNotIn("A" * 1000, phone)
        self.assertIn("A" * 1000, export)

    def test_safe_cleanup_plan_never_claims_to_ship(self):
        rows = [(1, "docs cleanup", "", "2026-05-07 00:00:00")]

        text = self.plan(rows)

        self.assertIn("no code changed, no deploy run", text)
        self.assertIn("GREEN batch candidates: #1", text)
        self.assertIn("Fix GREEN items only first", text)
        self.assertIn("Copy/paste this into Codex", text)
        self.assertIn("ssh macmini", text)
        self.assertIn("log done #1", text)

    def test_log_safe_cleanup_alias_returns_codex_handoff(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute("INSERT INTO change_log (request) VALUES (?)", ("docs cleanup",))
                conn.commit()
            finally:
                conn.close()

            reply = cmd("log safe cleanup", "+15550000001")

            self.assertIn("Copy/paste this into Codex", reply)
            self.assertIn("log done #1", reply)

    def test_capabilities_command_lists_major_routes(self):
        cmd = self.helpers["_cmd_capabilities"]

        reply = cmd("+15550000001")

        self.assertIn("Image scan: on via gemini", reply)
        self.assertIn("Image generation: on via local", reply)
        self.assertIn("gpt scan image", reply)
        self.assertIn("image gen [prompt]", reply)
        self.assertIn("Sports recap cron", reply)
        self.assertIn("ship safe cleanup", reply)

    def test_cleanup_prompt_confirmation_is_narrow(self):
        prompt = self.helpers["_looks_like_cleanup_prompt_confirmation"]
        confirmed = self.helpers["_looks_like_confirmed_cleanup_run"]

        self.assertTrue(confirmed("yes fix"))
        self.assertTrue(confirmed("yes ship fixes"))
        self.assertTrue(prompt("send codex prompt"))
        self.assertTrue(prompt("cleanup prompt"))
        self.assertTrue(prompt("master prompt"))
        self.assertFalse(prompt("yes fix"))
        self.assertFalse(confirmed("master prompt"))
        self.assertFalse(confirmed("yes"))
        self.assertFalse(confirmed("fix the image scan bug"))
        self.assertFalse(prompt("can you fix this reply"))

    def test_safe_cleanup_plan_keeps_red_rows_out_of_done_command(self):
        rows = [
            (3, "touch permissions.py admin password gate", "", "2026-05-07 01:00:00"),
            (2, "cron list UX for all chats", "", "2026-05-07 00:30:00"),
            (1, "docs cleanup and help text wording", "", "2026-05-07 00:00:00"),
        ]

        text = self.plan(rows)

        self.assertIn("Do not touch RED items", text)
        self.assertIn("log done #1 #2", text)
        self.assertNotIn("log done #1 #2 #3", text)
        self.assertIn("Do not run log clear while RED rows remain", text)

    def test_write_change_log_export_writes_private_snapshot(self):
        export = self.helpers["_write_change_log_export"]
        rows = [(1, "image gen failed", "missing key", "2026-05-16 00:00:00")]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            stable, snapshot = export(rows, output_dir=Path(tmp))

            self.assertTrue(stable.exists())
            self.assertTrue(snapshot.exists())
            self.assertEqual("change_log_board.md", stable.name)
            self.assertIn("image gen failed", stable.read_text(encoding="utf-8"))

    def test_big_change_intake_builds_review_only_payload(self):
        build = self.helpers["_build_big_change_intake"]

        request, reason, risk = build("change private message send routing and use sk-test")

        self.assertEqual("red", risk)
        self.assertIn("[BIG-CHANGE RED]", request)
        self.assertIn("status=review_only", reason)
        self.assertIn("no file edits, no deploy", reason)
        self.assertIn("[REDACTED]", reason)

    def test_big_change_intake_command_only_writes_change_log(self):
        cmd = self.helpers["_cmd_big_change_intake"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            finally:
                conn.close()

            reply = cmd("big change let admins submit large plans safely", "+15550000001")

            self.assertIn("Big-change intake logged #1 [YELLOW]", reply)
            self.assertIn("no code changed", reply)
            self.assertIn("Snapshot refreshed for SSH", reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request, reason FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertIn("[BIG-CHANGE YELLOW]", row[0])
            self.assertIn("review_only", row[1])

    def test_self_repair_intake_builds_diagnostic_handoff(self):
        build = self.helpers["_build_self_repair_intake"]

        request, reason, category, diagnosis, risk = build("you guessed instead of asking for the spreadsheet with sk-test")

        self.assertEqual("missing_context", category)
        self.assertEqual("YELLOW", risk)
        self.assertIn("[SELF-REPAIR YELLOW]", request)
        self.assertIn("type=self_repair_intake", reason)
        self.assertIn("status=review_only", reason)
        self.assertIn("codex_prompt=", reason)
        self.assertIn("validation_prompt=", reason)
        self.assertIn("[REDACTED]", reason)
        self.assertIn("file/context", diagnosis)

    def test_self_repair_intake_flags_sensitive_repairs_red(self):
        build = self.helpers["_build_self_repair_intake"]

        request, reason, category, diagnosis, risk = build("fix the admin password private send gate")

        self.assertEqual("security_boundary", category)
        self.assertEqual("RED", risk)
        self.assertIn("[SELF-REPAIR RED]", request)
        self.assertIn("CODE RED review first", reason)
        self.assertIn("permission", diagnosis.lower())

    def test_self_repair_intake_has_model_routing_category(self):
        build = self.helpers["_build_self_repair_intake"]

        request, reason, category, _diagnosis, risk = build("Gemini missing parts and billing usage seem wrong")

        self.assertEqual("model_routing", category)
        self.assertEqual("YELLOW", risk)
        self.assertIn("[SELF-REPAIR YELLOW]", request)
        self.assertIn("usage logging", reason)

    def test_self_repair_intake_detects_diagnose_yourself(self):
        looks_like = self.helpers["_looks_like_self_repair_intake"]

        self.assertTrue(looks_like("diagnose yourself: why did billing look wrong"))

    def test_self_repair_intake_detects_log_fix_ship_phrase_family(self):
        looks_like = self.helpers["_looks_like_self_repair_intake"]
        parse = self.helpers["_parse_self_repair_issue"]

        examples = [
            "FIX IT",
            "log this and fix it",
            "Log that my image was never generated",
            "ship this cron fix",
            "Ship this fix so it doesn't happen again",
            "this failed, fix yourself",
            "analzye this and ship",
        ]

        for example in examples:
            with self.subTest(example=example):
                self.assertTrue(looks_like(example))
                self.assertTrue(parse(example))

        self.assertFalse(looks_like("set up daily sports recap cron for Cole at 6pm"))
        self.assertFalse(looks_like("fix it in post"))
        self.assertFalse(looks_like("ship safe cleanup"))

    def test_self_repair_intake_routes_image_generation_complaint_to_command_routing(self):
        build = self.helpers["_build_self_repair_intake"]

        request, reason, category, diagnosis, risk = build("Log that my image was never generated")

        self.assertEqual("command_routing", category)
        self.assertEqual("YELLOW", risk)
        self.assertIn("[SELF-REPAIR YELLOW]", request)
        self.assertIn("direct command route", diagnosis.lower())
        self.assertIn("exact_expected_behavior=Log/fix/ship command intent reaches the guarded intake", reason)

    def test_self_repair_intake_reason_has_guarded_repair_context(self):
        build = self.helpers["_build_self_repair_intake"]

        request, reason, category, _diagnosis, risk = build(
            "log this and fix it",
            image_scan_result="Screenshot says the sports cron recap did not update.",
            source="screenshot_issue_image_scan",
        )

        self.assertEqual("deterministic_routing", category)
        self.assertEqual("RED", risk)
        self.assertIn("[SELF-REPAIR RED]", request)
        self.assertIn("message_text=log this and fix it", reason)
        self.assertIn("image_scan_result=Screenshot says the sports cron recap did not update.", reason)
        self.assertIn("recent_bot_logs=", reason)
        self.assertIn("relevant_db_rows=", reason)
        self.assertIn("likely_code_area=", reason)
        self.assertIn("exact_expected_behavior=", reason)
        self.assertIn("safe_auto_fix_pipeline=Codex only", reason)

    def test_self_repair_intake_command_only_writes_change_log(self):
        cmd = self.helpers["_cmd_self_repair_intake"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            finally:
                conn.close()

            reply = cmd("fix yourself: reminder answer was wrong", "+15550000001")

            self.assertIn("Self-repair logged #1 [RED/deterministic_routing]", reply)
            self.assertIn("Review-only", reply)
            self.assertIn("Snapshot refreshed for SSH", reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request, reason FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertIn("[SELF-REPAIR RED]", row[0])
            self.assertIn("category=deterministic_routing", row[1])
            self.assertIn("blocked_actions=no live self-edit", row[1])

    def test_self_repair_intake_asks_for_clarification_on_vague_fix_phrases(self):
        cmd = self.helpers["_cmd_self_repair_intake"]
        examples = [
            "FIX IT",
            "Ship this fix so it doesn’t happen again",
        ]

        for example in examples:
            with self.subTest(example=example):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                    db_path = str(Path(tmp) / "davosbot.db")
                    self.helpers["BOT_DB_PATH"] = db_path
                    conn = sqlite3.connect(db_path)
                    try:
                        conn.execute(
                            """
                            CREATE TABLE change_log (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                request TEXT NOT NULL,
                                reason TEXT,
                                created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                            """
                        )
                    finally:
                        conn.close()

                    reply = cmd(example, "+15550000001")

                    self.assertIn("I can turn that into a repair handoff", reply)
                    self.assertIn("fix yourself: [what went wrong]", reply)
                    self.assertIn("I did not create a change-log row yet.", reply)
                    conn = sqlite3.connect(db_path)
                    try:
                        count = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
                    finally:
                        conn.close()
                    self.assertEqual(0, count)

    def test_self_repair_does_not_make_fix_a_broad_command(self):
        commands = self.helpers["OWNER_COMMANDS"]

        self.assertNotIn("fix", commands)
        self.assertNotIn("self", commands)

    def test_log_mutation_refreshes_private_snapshot(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            finally:
                conn.close()

            reply = cmd("log image generation still needs smoke", "+15550000001")

            self.assertIn("Logged [YELLOW]", reply)
            self.assertIn("Snapshot refreshed for SSH", reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertEqual("image generation still needs smoke", row[0])

    def test_log_done_accepts_multiple_hash_ids(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO change_log (request) VALUES (?)",
                    [("first",), ("second",), ("third",)],
                )
                conn.commit()
            finally:
                conn.close()

            reply = cmd("log done #1 #3 #99", "+15550000001")

            self.assertIn("Removed logs: #1, #3.", reply)
            self.assertIn("Not found: #99.", reply)
            self.assertIn("Snapshot refreshed for SSH", reply)
            conn = sqlite3.connect(db_path)
            try:
                remaining = conn.execute("SELECT id, request FROM change_log").fetchall()
            finally:
                conn.close()
            self.assertEqual([(2, "second")], remaining)

    def test_delete_log_alias_rewrites_to_remove_command(self):
        rewrite = self.helpers["_rewrite_change_log_delete_alias"]

        self.assertEqual("log remove 126", rewrite("Delete log 126"))
        self.assertEqual("log remove #1 #3", rewrite("close logs #1, #3"))
        self.assertIsNone(rewrite("delete the latest log"))

    def test_update_log_alias_rewrites_to_update_command(self):
        rewrite = self.helpers["_rewrite_change_log_update_alias"]

        self.assertEqual(
            "log update #136 better summary",
            rewrite("update log 136 to better summary"),
        )
        self.assertEqual(
            "log update #4 issue still needs context",
            rewrite("edit log #4: issue still needs context"),
        )
        self.assertIsNone(rewrite("update the latest log"))

    def test_log_update_changes_existing_row(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO change_log (request, reason) VALUES (?, ?)",
                    ("this inefficiency", "source=manual"),
                )
                conn.commit()
            finally:
                conn.close()

            reply = cmd(
                "log update #1 I can validate the logic here, but I cannot deploy a backend system or switch my own LLM environment.",
                "+15550000001",
            )

            self.assertIn("Updated log #1 [YELLOW]", reply)
            self.assertIn("Snapshot refreshed for SSH", reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request, reason FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertEqual(
                "I can validate the logic here, but I cannot deploy a backend system or switch my own LLM environment.",
                row[0],
            )
            self.assertEqual("source=manual", row[1])

    def test_log_this_message_entirely_preserves_payload(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            finally:
                conn.close()

            reply = cmd(
                "log this message entirely: This image shows a chat where image routing failed.",
                "+15550000001",
            )

            self.assertIn("Logged [YELLOW]", reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertEqual("This image shows a chat where image routing failed.", row[0])

    def test_large_log_payload_is_saved_without_echoing_wall_to_phone(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            finally:
                conn.close()

            payload = (
                "Image read via gemini: sports recap cron for Cole needs changes. "
                + "A" * 2500
            )
            reply = cmd(f"log {payload}", "+15550000001")

            self.assertIn("Logged [YELLOW] #1", reply)
            self.assertIn(f"({len(payload)} chars)", reply)
            self.assertIn("Full text saved", reply)
            self.assertNotIn("A" * 1000, reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertEqual(payload, row[0])

    def test_log_that_msg_entirely_uses_previous_assistant_turn(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                        ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO messages (sender, role, content) VALUES (?, ?, ?)",
                    ("+15550000001", "assistant", "Image read via gemini: the screenshot shows image gen failed."),
                )
                conn.commit()
            finally:
                conn.close()

            reply = cmd("log that msg entirely", "+15550000001")

            self.assertIn("Logged [YELLOW]", reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertEqual("Image read via gemini: the screenshot shows image gen failed.", row[0])

    def test_log_that_msg_entirely_can_append_owner_note(self):
        cmd = self.helpers["_cmd_log"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            self.helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                        ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO messages (sender, role, content) VALUES (?, ?, ?)",
                    ("+15550000001", "assistant", "Image read via gemini: Franklin is visible."),
                )
                conn.commit()
            finally:
                conn.close()

            reply = cmd("log that msg entirely and also note image scan worked", "+15550000001")

            self.assertIn("Logged [YELLOW]", reply)
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertEqual(
                "Image read via gemini: Franklin is visible.\n\nOwner note: image scan worked",
                row[0],
            )


if __name__ == "__main__":
    unittest.main()
