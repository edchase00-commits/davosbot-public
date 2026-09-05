import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from davosbot import failure_copy, simple_chat


ROOT = Path(__file__).resolve().parents[1]


def _load_routing_helpers(
    *,
    simple_route: str = "ollama:gemma4",
    complex_route: str = "gemini:gemini-3.5-flash",
    code_route: str = "gemini:gemini-3.5-flash",
    gemini_reply: str | None = "advanced reply",
    ollama_reply: str | None = "ollama reply",
    ollama_health: bool = False,
):
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "_CODE_REVIEW_RE",
        "_COMPLEX_REASONING_RE",
        "_CAPABILITY_GAP_RE",
        "_MODEL_ROUTING_QUERY_RE",
        "_PROVIDER_STATUS_REPLY_RE",
        "_BLAND_SIMPLE_CHAT_RE",
        "_last_ollama_check",
        "_ollama_down",
    }
    wanted_funcs = {
        "_callable_gemini_model",
        "_model_route_parts",
        "_owner_advanced_direct_route",
        "_simple_chat_direct_route",
        "_ollama_simple_chat_model",
        "_simple_chat_empty_fallback",
        "_simple_chat_personality_fallback",
        "_mark_ollama_down_after_direct_miss",
        "detect_capability_gap",
        "_ollama_soft_miss_reason",
        "_provider_status_narration_reason",
        "_polish_simple_chat_reply",
        "_suppress_provider_status_reply",
        "get_structured_response",
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

    events = []
    mark_down_calls = []
    gemini = Mock(return_value=gemini_reply)
    ollama = Mock(return_value=ollama_reply)
    tags = Mock(return_value=ollama_health)
    namespace = {
        "MODEL_ROUTE_SIMPLE_CHAT": simple_route,
        "OLLAMA_SIMPLE_CHAT_MODEL": "gemma3",
        "MODEL_ROUTE_CODE_REVIEW": code_route,
        "MODEL_ROUTE_COMPLEX_REASONING": complex_route,
        "OLLAMA_SIMPLE_CHAT_NUM_PREDICT": 64,
        "OLLAMA_SIMPLE_CHAT_TEMPERATURE": 0.7,
        "OLLAMA_SIMPLE_CHAT_TIMEOUT": 3.5,
        "OLLAMA_CHECK_INTERVAL": 300,
        "_failure_copy": failure_copy,
        "_simple_chat": simple_chat,
        "OWNER_ID": "+15550000001",
        "_call_gemini": gemini,
        "_call_gemini_agentic": Mock(side_effect=AssertionError("agentic route should not run")),
        "_call_ollama": ollama,
        "_ollama_health_check": Mock(return_value=ollama_health),
        "_ollama_tags_available": tags,
        "_harmless_roast_fallback": lambda _msg: None,
        "_humanize_transient_error": lambda reply: reply,
        "_log_bot_event": lambda event_type, payload=None: events.append((event_type, payload or {})),
        "_mark_ollama_down": lambda notify=False: mark_down_calls.append(notify),
        "_maybe_sass_large_prompt": lambda reply: reply,
        "_user_prompt_too_large": lambda _msg: False,
        "check_ollama_recovery": lambda: False,
        "logger": SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        "re": re,
    }
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    namespace["_mark_down_calls"] = mark_down_calls
    namespace["_tags_check"] = tags
    return namespace, gemini, ollama, events


class ModelRoutingTests(unittest.TestCase):
    def test_owner_complex_direct_chat_uses_configured_gemini_route(self):
        helpers, gemini, ollama, events = _load_routing_helpers()

        reply = helpers["get_response"](
            "",
            [],
            "plan the model routing architecture and tradeoffs",
            sender="+15550000001",
        )

        self.assertEqual("advanced reply", reply)
        gemini.assert_called_once()
        self.assertEqual("gemini-3.5-flash", gemini.call_args.kwargs["model"])
        self.assertEqual("complex_reasoning_direct", gemini.call_args.kwargs["source"])
        ollama.assert_not_called()
        self.assertEqual("model_route_selected", events[0][0])
        self.assertEqual("complex_reasoning", events[0][1]["route"])

    def test_non_owner_complex_chat_stays_on_ollama(self):
        helpers, gemini, ollama, events = _load_routing_helpers()

        reply = helpers["get_response"](
            "",
            [],
            "plan the model routing architecture",
            sender="+15550000002",
        )

        self.assertEqual("ollama reply", reply)
        gemini.assert_not_called()
        ollama.assert_called_once()
        self.assertEqual([], events)

    def test_owner_simple_chat_stays_on_ollama(self):
        helpers, gemini, ollama, events = _load_routing_helpers()

        reply = helpers["get_response"]("", [], "lol what are you doing", sender="+15550000001")

        self.assertEqual("ollama reply", reply)
        gemini.assert_not_called()
        ollama.assert_called_once()
        self.assertEqual([], events)

    def test_simple_chat_caps_ollama_prediction_budget(self):
        helpers, gemini, ollama, events = _load_routing_helpers()

        reply = helpers["get_response"]("", [], "lol what are you doing", sender="+15550000001", simple_chat=True)

        self.assertEqual("ollama reply", reply)
        gemini.assert_not_called()
        self.assertEqual(64, ollama.call_args.kwargs["num_predict"])
        self.assertEqual(0.7, ollama.call_args.kwargs["temperature"])
        self.assertEqual(3.5, ollama.call_args.kwargs["timeout"])
        self.assertEqual("gemma3", ollama.call_args.kwargs["model"])
        self.assertNotIn("empty_fallback", ollama.call_args.kwargs)
        self.assertEqual([], events)

    def test_simple_chat_ollama_miss_falls_to_gemini_before_copy_fallback(self):
        helpers, gemini, ollama, _events = _load_routing_helpers(
            ollama_reply=None,
            gemini_reply="gemini casual reply",
        )

        reply = helpers["get_response"]("", [], "what's up bro", sender="+15550000001", simple_chat=True)

        self.assertEqual("gemini casual reply", reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        self.assertEqual([False], helpers["_mark_down_calls"])
        helpers["_tags_check"].assert_called_once()

    def test_simple_chat_ollama_miss_does_not_mark_down_when_tags_check_passes(self):
        helpers, gemini, ollama, _events = _load_routing_helpers(
            ollama_reply=None,
            gemini_reply="gemini casual reply",
            ollama_health=True,
        )

        reply = helpers["get_response"]("", [], "what's up bro", sender="+15550000001", simple_chat=True)

        self.assertEqual("gemini casual reply", reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        helpers["_tags_check"].assert_called_once()
        self.assertEqual([], helpers["_mark_down_calls"])

    def test_ollama_capability_gap_soft_misses_to_gemini(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            ollama_reply="I can't do that right now.",
            gemini_reply="gemini handled it",
        )

        reply = helpers["get_response"]("", [], "can you figure this out?", sender="+15550000001")

        self.assertEqual("gemini handled it", reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        self.assertEqual([], helpers["_mark_down_calls"])
        self.assertIn(("ollama_soft_miss", {"route": "direct", "reason": "capability_gap"}), events)

    def test_ollama_provider_status_soft_misses_to_gemini(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            ollama_reply="Ollama failed, so I'm trying Gemini now.",
            gemini_reply="clean reply",
        )

        reply = helpers["get_response"]("", [], "what's up?", sender="+15550000001")

        self.assertEqual("clean reply", reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        self.assertEqual([], helpers["_mark_down_calls"])
        self.assertIn(("ollama_soft_miss", {"route": "direct", "reason": "provider_status"}), events)

    def test_provider_status_allowed_for_model_routing_questions(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            ollama_reply="Ollama is the local model and Gemini is the fallback provider.",
            gemini_reply="gemini should not run",
        )

        reply = helpers["get_response"]("", [], "which model fallback do you use?", sender="+15550000001")

        self.assertEqual("Ollama is the local model and Gemini is the fallback provider.", reply)
        ollama.assert_called_once()
        gemini.assert_not_called()
        self.assertEqual([], helpers["_mark_down_calls"])
        self.assertEqual([], events)

    def test_provider_status_not_allowed_for_non_model_fallback_word(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            ollama_reply="Ollama failed, so I'm trying Gemini now.",
            gemini_reply="actual fallback plan",
        )

        reply = helpers["get_response"]("", [], "what's our fallback dinner?", sender="+15550000001")

        self.assertEqual("actual fallback plan", reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        self.assertIn(("ollama_soft_miss", {"route": "direct", "reason": "provider_status"}), events)

    def test_gemini_provider_status_reply_is_suppressed(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            ollama_reply=None,
            gemini_reply="Ollama failed, so I switched to Gemini.",
        )

        reply = helpers["get_response"]("", [], "what's up?", sender="+15550000001")

        self.assertEqual(failure_copy.DIRECT_CHAT_FAILURE_REPLY, reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        self.assertEqual([False, True], helpers["_mark_down_calls"])
        self.assertIn(
            ("model_debug_reply_suppressed", {"source": "direct_gemini_fallback", "reason": "provider_status"}),
            events,
        )

    def test_simple_chat_empty_fallback_handles_common_casual_prompts(self):
        helpers, _gemini, _ollama, _events = _load_routing_helpers()

        self.assertEqual("Here. What's up?", helpers["_simple_chat_empty_fallback"]("lol what are you doing"))
        self.assertEqual("There we go. What did I miss?", helpers["_simple_chat_empty_fallback"]("welcome back"))
        self.assertEqual("Missed you too. What's up?", helpers["_simple_chat_empty_fallback"]("we missed you"))
        self.assertEqual("Alive. What's up?", helpers["_simple_chat_empty_fallback"]("are you alive?"))
        self.assertEqual("pong", helpers["_simple_chat_empty_fallback"]("Reply with exactly one word: pong"))

    def test_simple_chat_empty_fallback_varies_for_distinct_repro_texts(self):
        helpers, _gemini, _ollama, _events = _load_routing_helpers()

        replies = [
            helpers["_simple_chat_empty_fallback"]("Yoooo wassup bro what\u2019s good"),
            helpers["_simple_chat_empty_fallback"]("Davos why you weird all of a sudden"),
            helpers["_simple_chat_empty_fallback"]("Can you go back to being a chiller?"),
        ]

        self.assertEqual(3, len(set(replies)))
        for reply in replies:
            self.assertNotIn("Ollama", reply)
            self.assertNotIn("Gemini", reply)
            self.assertNotIn("failed", reply.lower())
            self.assertNotEqual("Present and already judging whatever happened while I blinked.", reply)

    def test_simple_chat_polishes_bland_model_replies(self):
        helpers, gemini, ollama, events = _load_routing_helpers(ollama_reply="I'm here.")

        reply = helpers["get_response"]("", [], "we missed you", sender="+15550000001", simple_chat=True)

        self.assertEqual("Missed you too. What's up?", reply)
        gemini.assert_not_called()
        ollama.assert_called_once()
        self.assertEqual([], events)

    def test_simple_chat_total_backend_failure_stays_in_character(self):
        helpers, gemini, ollama, _events = _load_routing_helpers(ollama_reply=None, gemini_reply=None)

        reply = helpers["get_response"]("", [], "we missed you", sender="+15550000001", simple_chat=True)

        self.assertEqual("Missed you too. What's up?", reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        self.assertNotIn("Ollama", reply)
        self.assertNotIn("Gemini", reply)
        self.assertNotIn("failed", reply.lower())

    def test_total_backend_failure_is_provider_neutral(self):
        helpers, gemini, ollama, _events = _load_routing_helpers(ollama_reply=None, gemini_reply=None)

        reply = helpers["get_response"]("", [], "what happened?", sender="+15550000001")

        self.assertEqual(failure_copy.DIRECT_CHAT_FAILURE_REPLY, reply)
        ollama.assert_called_once()
        gemini.assert_called_once()
        self.assertEqual([False, True], helpers["_mark_down_calls"])
        self.assertNotIn("Ollama", reply)
        self.assertNotIn("Gemini", reply)
        self.assertNotIn("failed", reply.lower())

    def test_ollama_down_falls_to_gemini_without_owner_alert(self):
        helpers, gemini, ollama, _events = _load_routing_helpers(
            ollama_reply=None,
            gemini_reply="gemini backup reply",
        )
        helpers["_ollama_down"] = True

        reply = helpers["get_response"]("", [], "what happened?", sender="+15550000001")

        self.assertEqual("gemini backup reply", reply)
        ollama.assert_not_called()
        gemini.assert_called_once()
        self.assertEqual([], helpers["_mark_down_calls"])

    def test_simple_chat_uses_local_model_when_global_ollama_state_is_down_but_tags_pass(self):
        helpers, gemini, ollama, _events = _load_routing_helpers(
            ollama_reply="local casual reply",
            gemini_reply="gemini backup reply",
            ollama_health=True,
        )
        helpers["_ollama_down"] = True

        reply = helpers["get_response"]("", [], "pacers lol", sender="+15550000001", simple_chat=True)

        self.assertEqual("local casual reply", reply)
        helpers["_tags_check"].assert_called_once()
        ollama.assert_called_once()
        self.assertEqual("gemma3", ollama.call_args.kwargs["model"])
        gemini.assert_not_called()
        self.assertEqual([], helpers["_mark_down_calls"])

    def test_ollama_down_total_backend_failure_escalates_once(self):
        helpers, gemini, ollama, _events = _load_routing_helpers(
            ollama_reply=None,
            gemini_reply=None,
        )
        helpers["_ollama_down"] = True

        reply = helpers["get_response"]("", [], "what happened?", sender="+15550000001")

        self.assertEqual(failure_copy.DIRECT_CHAT_FAILURE_REPLY, reply)
        ollama.assert_not_called()
        gemini.assert_called_once()
        self.assertEqual([True], helpers["_mark_down_calls"])

    def test_configured_simple_chat_gemini_route_runs_before_ollama(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            simple_route="gemini:gemini-2.5-flash-lite",
            gemini_reply="fast reply",
        )

        reply = helpers["get_response"]("", [], "lol what are you doing", sender="+15550000001")

        self.assertEqual("fast reply", reply)
        gemini.assert_called_once()
        self.assertEqual("gemini-2.5-flash-lite", gemini.call_args.kwargs["model"])
        self.assertEqual("simple_chat_direct", gemini.call_args.kwargs["source"])
        ollama.assert_not_called()
        self.assertEqual("simple_chat", events[0][1]["route"])

    def test_configured_simple_chat_gemini_route_falls_back_to_ollama(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            simple_route="gemini:gemini-2.5-flash-lite",
            gemini_reply=None,
        )

        reply = helpers["get_response"]("", [], "lol what are you doing", sender="+15550000001")

        self.assertEqual("ollama reply", reply)
        gemini.assert_called_once()
        ollama.assert_called_once()
        self.assertEqual("simple_chat", events[0][1]["route"])

    def test_callable_code_review_route_uses_gemini_code_model(self):
        helpers, gemini, ollama, events = _load_routing_helpers(code_route="gemini:gemini-3.5-flash")

        reply = helpers["get_response"]("", [], "review this diff for bugs", sender="+15550000001")

        self.assertEqual("advanced reply", reply)
        self.assertEqual("gemini-3.5-flash", gemini.call_args.kwargs["model"])
        self.assertEqual("code_review_direct", gemini.call_args.kwargs["source"])
        ollama.assert_not_called()
        self.assertEqual("code_review", events[0][1]["route"])

    def test_codex_code_label_falls_back_to_complex_gemini_not_live_codex(self):
        helpers, gemini, ollama, events = _load_routing_helpers(
            code_route="codex:gpt-5.4",
            complex_route="gemini:gemini-3.5-flash",
        )

        reply = helpers["get_response"]("", [], "review this pull request", sender="+15550000001")

        self.assertEqual("advanced reply", reply)
        self.assertEqual("gemini-3.5-flash", gemini.call_args.kwargs["model"])
        self.assertEqual("complex_reasoning_direct", gemini.call_args.kwargs["source"])
        ollama.assert_not_called()
        self.assertEqual("complex_reasoning", events[0][1]["route"])

    def test_failed_advanced_route_falls_back_to_ollama(self):
        helpers, gemini, ollama, events = _load_routing_helpers(gemini_reply=None)

        reply = helpers["get_response"]("", [], "diagnose this routing issue", sender="+15550000001")

        self.assertEqual("ollama reply", reply)
        gemini.assert_called_once()
        ollama.assert_called_once()
        self.assertEqual("model_route_selected", events[0][0])

    def test_structured_response_labels_memory_extraction_usage(self):
        helpers, gemini, _ollama, _events = _load_routing_helpers()

        reply = helpers["get_structured_response"]("extract facts", source="memory_extraction")

        self.assertEqual("advanced reply", reply)
        self.assertEqual("memory_extraction", gemini.call_args.kwargs["source"])


if __name__ == "__main__":
    unittest.main()
