import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from davosbot import failure_copy


ROOT = Path(__file__).resolve().parents[1]


def _load_prompt_helpers():
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "_MAX_USER_MODEL_CHARS",
        "_MAX_HISTORY_MODEL_CHARS",
        "_MAX_HISTORY_TURN_CHARS",
        "_MAX_OLLAMA_SYSTEM_CHARS",
        "_MAX_OLLAMA_IDENTITY_CHARS",
        "_MIN_OLLAMA_IDENTITY_CHARS",
        "_MAX_OLLAMA_RULES_CHARS",
        "_MIN_OLLAMA_RULES_CHARS",
        "_MAX_OLLAMA_RELEVANT_FACTS_CHARS",
        "_MIN_OLLAMA_RELEVANT_FACTS_CHARS",
        "_MAX_OLLAMA_FACTS_CHARS",
        "_MIN_OLLAMA_FACTS_CHARS",
        "_MAX_OLLAMA_HISTORY_CHARS",
        "_MAX_OLLAMA_HISTORY_TURN_CHARS",
        "_LARGE_PROMPT_SENTINEL",
        "_LARGE_PROMPT_SASS",
        "_ollama_down",
        "_last_ollama_check",
    }
    wanted_funcs = {
        "_user_prompt_too_large",
        "_clip_text_for_model",
        "_clip_middle_for_local_prompt",
        "_reduce_prompt_budget",
        "_split_ollama_system_sections",
        "_fit_history_for_model",
        "_fit_system_for_ollama",
        "_is_large_prompt_error",
        "_maybe_sass_large_prompt",
        "_oversized_prompt_risk",
        "_oversized_prompt_preview",
        "_log_oversized_owner_prompt_intake",
        "get_response",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in wanted_assigns for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "logger": SimpleNamespace(info=lambda *a, **k: None),
        "re": __import__("re"),
        "sqlite3": sqlite3,
        "closing": closing,
        "BOT_DB_PATH": "",
        "OWNER_ID": "+15550000001",
        "redact_secret": lambda text: text.replace("sk-test", "[REDACTED]"),
        "_call_ollama": Mock(side_effect=AssertionError),
        "_call_gemini": Mock(side_effect=AssertionError),
        "_call_gemini_agentic": Mock(side_effect=AssertionError),
        "_humanize_transient_error": lambda reply: reply,
        "_harmless_roast_fallback": lambda _user_msg: None,
        "_notify_owner": lambda _message: None,
        "time": SimpleNamespace(time=lambda: 0),
        "OLLAMA_CHECK_INTERVAL": 60,
        "_failure_copy": failure_copy,
    }
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    return namespace


class PromptSizeGuardTests(unittest.TestCase):
    def test_oversized_user_prompt_is_rejected_before_backends(self):
        helpers = _load_prompt_helpers()
        huge_prompt = "x" * (helpers["_MAX_USER_MODEL_CHARS"] + 1)

        reply = helpers["get_response"]("", [], huge_prompt)

        self.assertIn(reply, helpers["_LARGE_PROMPT_SASS"])
        helpers["_call_ollama"].assert_not_called()
        helpers["_call_gemini"].assert_not_called()
        helpers["_call_gemini_agentic"].assert_not_called()

    def test_oversized_owner_prompt_logs_guarded_intake_before_backends(self):
        helpers = _load_prompt_helpers()
        huge_prompt = (
            "build this whole new robust setup and fix the model routing sk-test "
            + ("x" * helpers["_MAX_USER_MODEL_CHARS"])
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            helpers["BOT_DB_PATH"] = db_path
            with closing(sqlite3.connect(db_path)) as conn:
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

            reply = helpers["get_response"]("", [], huge_prompt, sender="+15550000001")
            with closing(sqlite3.connect(db_path)) as conn:
                request, reason = conn.execute("SELECT request, reason FROM change_log").fetchone()

        self.assertIn("Logged oversized Codex intake #1", reply)
        self.assertIn("[OVERSIZED-INTAKE YELLOW]", request)
        self.assertIn("type=oversized_owner_prompt_intake", reason)
        self.assertIn("message_len=", reason)
        self.assertIn("[REDACTED]", reason)
        self.assertNotIn("sk-test", reason)
        self.assertIn("safe_auto_fix_pipeline=Codex only", reason)
        helpers["_call_ollama"].assert_not_called()
        helpers["_call_gemini"].assert_not_called()
        helpers["_call_gemini_agentic"].assert_not_called()

    def test_oversized_owner_prompt_marks_cron_repair_red(self):
        helpers = _load_prompt_helpers()

        self.assertEqual("RED", helpers["_oversized_prompt_risk"]("ship this cron fix"))

    def test_history_is_compacted_to_recent_bounded_context(self):
        helpers = _load_prompt_helpers()
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn-{i} " + ("x" * 1200)}
            for i in range(40)
        ]

        fitted = helpers["_fit_history_for_model"](history)
        total_chars = sum(len(turn["content"]) for turn in fitted)

        self.assertLessEqual(total_chars, helpers["_MAX_HISTORY_MODEL_CHARS"])
        self.assertEqual("turn-39 ", fitted[-1]["content"][:8])

    def test_ollama_system_prompt_is_compacted_without_losing_identity_time_or_recent_facts(self):
        helpers = _load_prompt_helpers()
        system = (
            "DavosBot identity and active persona stay at the front.\n"
            + ("old system detail " * 900)
            + "\n\n## CURRENT TIME (use this for ALL time math)\n- UTC: 2026-06-05 22:00:00\n"
            + "\n\n## FACTS — treat these as ground truth.\n"
            + "Core owner fact at the top.\n"
            + ("older memory detail " * 900)
            + "Most recent owner fact stays at the tail."
        )

        fitted = helpers["_fit_system_for_ollama"](system)

        self.assertLessEqual(len(fitted), helpers["_MAX_OLLAMA_SYSTEM_CHARS"])
        self.assertIn("DavosBot identity", fitted)
        self.assertIn("CURRENT TIME", fitted)
        self.assertIn("FACTS", fitted)
        self.assertIn("Most recent owner fact", fitted)
        self.assertIn("compacted for local Ollama context", fitted)

    def test_ollama_system_prompt_preserves_relevant_memory_section(self):
        helpers = _load_prompt_helpers()
        relevant_rule = "DECATUR_RULE_KEEP_ME when explicitly invoked."
        system = (
            "# Active Persona\n"
            + ("persona opening detail " * 500)
            + "\n\n## Voice and boundaries\n"
            + "Core Davos voice stays intact.\n"
            + ("core behavior detail " * 250)
            + "\n\n## CURRENT TIME (use this for ALL time math)\n- UTC: 2026-06-05 22:00:00\n"
            + "\n\n## RELEVANT FACTS - highest priority for this message\n"
            + "## FACTS from old memory heading\n"
            + relevant_rule
            + "\n\n## FACTS - treat these as ground truth.\n"
            + ("older memory detail " * 1200)
            + "Most recent owner fact stays at the tail."
        )

        fitted = helpers["_fit_system_for_ollama"](system)

        self.assertLessEqual(len(fitted), helpers["_MAX_OLLAMA_SYSTEM_CHARS"])
        self.assertIn("Active Persona", fitted)
        self.assertIn("Voice and boundaries", fitted)
        self.assertIn("CURRENT TIME", fitted)
        self.assertIn("RELEVANT FACTS", fitted)
        self.assertIn(relevant_rule, fitted)
        self.assertIn("Most recent owner fact", fitted)

    def test_ollama_history_can_use_smaller_local_budget(self):
        helpers = _load_prompt_helpers()
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn-{i} " + ("x" * 840)}
            for i in range(20)
        ]

        fitted = helpers["_fit_history_for_model"](
            history,
            max_chars=helpers["_MAX_OLLAMA_HISTORY_CHARS"],
            max_turn_chars=helpers["_MAX_OLLAMA_HISTORY_TURN_CHARS"],
        )
        total_chars = sum(len(turn["content"]) for turn in fitted)

        self.assertLessEqual(total_chars, helpers["_MAX_OLLAMA_HISTORY_CHARS"])
        self.assertEqual("turn-19 ", fitted[-1]["content"][:8])

    def test_large_prompt_error_detector_handles_too_many_messages(self):
        helpers = _load_prompt_helpers()
        body = '{"error":"too many messages in request contents"}'

        self.assertTrue(helpers["_is_large_prompt_error"](400, body))


if __name__ == "__main__":
    unittest.main()
