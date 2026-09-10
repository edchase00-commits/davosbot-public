import ast
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from test_model_routing import _load_routing_helpers


ROOT = Path(__file__).resolve().parents[1]


class _FakeOllamaResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": "local ok"}}


class _FakeEmptyOllamaResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": "   ", "thinking": "synthetic private reasoning"}}


class _FakeCompletedOllamaResponse(_FakeOllamaResponse):
    def __init__(self, content, done_reason):
        self.content = content
        self.done_reason = done_reason

    def json(self):
        return {
            "message": {"content": self.content, "thinking": "synthetic private reasoning"},
            "done": True,
            "done_reason": self.done_reason,
        }


def _load_ollama_call_helpers(fake_post):
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "OLLAMA_TIMEOUT",
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
        "_OLLAMA_DEFAULT_CONTEXT_TOKENS",
        "_OLLAMA_REPLY_RESERVE_TOKENS",
        "_OLLAMA_CONTEXT_SAFETY_TOKENS",
        "_OLLAMA_MESSAGE_OVERHEAD_TOKENS",
        "_MAX_HISTORY_MODEL_CHARS",
        "_MAX_HISTORY_TURN_CHARS",
        "_SLOW_MODEL_CALL_SECONDS",
    }
    wanted_funcs = {
        "_close_response",
        "_clip_text_for_model",
        "_clip_middle_for_local_prompt",
        "_reduce_prompt_budget",
        "_split_ollama_system_sections",
        "_fit_history_for_model",
        "_estimate_ollama_tokens",
        "_ollama_history_budget",
        "_fit_ollama_history",
        "_fit_system_for_ollama",
        "_ollama_keep_alive_value",
        "_call_ollama",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if target_names & wanted_assigns:
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        "OLLAMA_MODEL": "gemma4",
        "OLLAMA_NUM_CTX": 8192,
        "OLLAMA_KEEP_ALIVE": "1h",
        "_request_context": types.SimpleNamespace(split_request_context=lambda system: ("", system)),
        "logger": types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        ),
        "requests": types.SimpleNamespace(post=fake_post, exceptions=requests.exceptions),
        "time": __import__("time"),
        "_try_restart_ollama": lambda: None,
        "_log_bot_event": lambda *args, **kwargs: None,
    }
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    return namespace


class OllamaPromptRoutingTests(unittest.TestCase):
    def test_call_ollama_uses_compacted_prompt_history_and_num_ctx(self):
        payloads = []

        def fake_post(url, json, timeout):
            payloads.append((url, json, timeout))
            return _FakeOllamaResponse()

        helpers = _load_ollama_call_helpers(fake_post)
        system = (
            "DavosBot identity and active persona stay at the front.\n"
            + ("old system detail " * 900)
            + "\n\n## CURRENT TIME (use this for ALL time math)\n- UTC: 2026-06-05 22:00:00\n"
            + "\n\n## FACTS — treat these as ground truth.\n"
            + "Core owner fact at the top.\n"
            + ("older memory detail " * 900)
            + "Most recent owner fact stays at the tail."
        )
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn-{i} " + ("x" * 1400)}
            for i in range(30)
        ]

        reply = helpers["_call_ollama"](system, history, "hi")

        self.assertEqual("local ok", reply)
        _url, payload, _timeout = payloads[0]
        self.assertEqual("gemma4", payload["model"])
        self.assertEqual("1h", payload["keep_alive"])
        self.assertEqual({"num_ctx": 8192}, payload["options"])
        self.assertNotIn("think", payload)
        self.assertLessEqual(len(payload["messages"][0]["content"]), helpers["_MAX_OLLAMA_SYSTEM_CHARS"])
        self.assertIn("DavosBot identity", payload["messages"][0]["content"])
        self.assertIn("CURRENT TIME", payload["messages"][0]["content"])
        self.assertIn("Most recent owner fact", payload["messages"][0]["content"])

        history_chars = sum(len(message["content"]) for message in payload["messages"][1:-1])
        self.assertLessEqual(history_chars, helpers["_MAX_OLLAMA_HISTORY_CHARS"])
        self.assertEqual({"role": "user", "content": "hi"}, payload["messages"][-1])

    def test_call_ollama_can_cap_prediction_budget(self):
        payloads = []

        def fake_post(url, json, timeout):
            payloads.append((url, json, timeout))
            return _FakeOllamaResponse()

        helpers = _load_ollama_call_helpers(fake_post)

        reply = helpers["_call_ollama"]("system", [], "hi", num_predict=64, temperature=0)

        self.assertEqual("local ok", reply)
        self.assertEqual("1h", payloads[0][1]["keep_alive"])
        self.assertEqual({"num_ctx": 8192, "num_predict": 64, "temperature": 0.0}, payloads[0][1]["options"])
        self.assertIs(False, payloads[0][1]["think"])

    def test_call_ollama_rejects_token_capped_output_with_metadata_only(self):
        partial = "Choose pickup. The total is $"
        for budget in (64, None):
            for fallback in (None, "Please try again."):
                with self.subTest(budget=budget, fallback=fallback):
                    post = Mock(return_value=_FakeCompletedOllamaResponse(partial, "length"))
                    helpers = _load_ollama_call_helpers(post)
                    events = Mock()
                    logger = Mock()
                    helpers["_log_bot_event"] = events
                    helpers["logger"] = logger

                    reply = helpers["_call_ollama"](
                        "system", [], "what do you think?", model="gemma3",
                        num_predict=budget, empty_fallback=fallback,
                    )

                    self.assertEqual(fallback, reply)
                    post.assert_called_once()
                    events.assert_called_once_with(
                        "ollama_truncated_reply",
                        {"model": "gemma3", "num_predict": budget, "output_chars": len(partial)},
                    )
                    recorded = repr((events.mock_calls, logger.mock_calls))
                    self.assertNotIn(partial, recorded)
                    self.assertNotIn("synthetic private reasoning", recorded)

    def test_call_ollama_accepts_stopped_and_legacy_completed_output(self):
        for response in (
            _FakeCompletedOllamaResponse("local ok", "stop"),
            _FakeOllamaResponse(),
        ):
            with self.subTest(response=type(response).__name__):
                helpers = _load_ollama_call_helpers(Mock(return_value=response))
                events = Mock()
                helpers["_log_bot_event"] = events

                reply = helpers["_call_ollama"]("system", [], "what do you think?", num_predict=64)

                self.assertEqual("local ok", reply)
                events.assert_not_called()

    def test_call_ollama_can_distinguish_truncation_from_empty_output(self):
        for response, expected in (
            (_FakeCompletedOllamaResponse("Choose pickup. The total is $", "length"), "truncated"),
            (_FakeEmptyOllamaResponse(), "empty"),
        ):
            with self.subTest(response=type(response).__name__):
                helpers = _load_ollama_call_helpers(Mock(return_value=response))

                reply = helpers["_call_ollama"](
                    "system", [], "what do you think?", num_predict=64,
                    empty_fallback="empty", truncated_fallback="truncated",
                )

                self.assertEqual(expected, reply)

    def test_token_capped_local_output_uses_existing_gemini_fallback(self):
        partial = "Choose pickup. The total is $"
        history = [{"role": "user", "content": "Pickup is $108; delivery is $184; budget is $150."}]
        for simple_chat in (False, True):
            for recovering in (False, True):
                for gemini_reply in ("Pickup fits the budget at $108.", None):
                    with self.subTest(simple_chat=simple_chat, recovering=recovering, gemini_reply=gemini_reply):
                        routes, gemini, _ollama, events = _load_routing_helpers(
                            gemini_reply=gemini_reply, ollama_health=True,
                        )
                        post = Mock(return_value=_FakeCompletedOllamaResponse(partial, "length"))
                        provider = _load_ollama_call_helpers(post)
                        routes["_call_ollama"] = provider["_call_ollama"]
                        routes["_ollama_down"] = recovering
                        routes["check_ollama_recovery"] = Mock(return_value=True)

                        reply = routes["get_response"](
                            "system", history, "what do you think?", sender="+15550000001",
                            simple_chat=simple_chat,
                        )

                        expected = gemini_reply or routes["_failure_copy"].DIRECT_CHAT_FAILURE_REPLY
                        self.assertEqual(expected, reply)
                        self.assertNotIn(partial, reply)
                        self.assertNotIn(routes["_OLLAMA_TRUNCATED_SENTINEL"], reply)
                        post.assert_called_once()
                        gemini.assert_called_once_with("system", history, "what do you think?")
                        self.assertEqual([], routes["_mark_down_calls"])
                        route = "recovered_direct" if recovering else "direct"
                        self.assertIn(("ollama_soft_miss", {"route": route, "reason": "output_limit"}), events)

    def test_local_timeout_still_uses_existing_outage_path(self):
        for recovering in (False, True):
            with self.subTest(recovering=recovering):
                routes, gemini, _ollama, events = _load_routing_helpers(gemini_reply=None)
                post = Mock(side_effect=requests.exceptions.Timeout())
                provider = _load_ollama_call_helpers(post)
                routes["_call_ollama"] = provider["_call_ollama"]
                routes["_ollama_down"] = recovering
                routes["check_ollama_recovery"] = Mock(return_value=True)

                reply = routes["get_response"]("system", [], "what happened?", sender="+15550000001")

                self.assertEqual(routes["_failure_copy"].DIRECT_CHAT_FAILURE_REPLY, reply)
                self.assertNotIn(routes["_OLLAMA_TRUNCATED_SENTINEL"], reply)
                self.assertEqual([False, True], routes["_mark_down_calls"])
                self.assertEqual([], events)
                post.assert_called_once()
                gemini.assert_called_once_with("system", [], "what happened?")

    def test_bounded_gemma4_model_override_requests_final_output(self):
        payloads = []
        helpers = _load_ollama_call_helpers(
            lambda _url, json, timeout: payloads.append(json) or _FakeOllamaResponse()
        )
        helpers["OLLAMA_MODEL"] = "gemma3"
        for model in ("gemma4", "gemma4:latest", "gemma4:e4b", " GEMMA4:latest "):
            with self.subTest(model=model):
                self.assertEqual("local ok", helpers["_call_ollama"]("system", [], "hi", model=model, num_predict=180))
                self.assertIs(False, payloads[-1]["think"])
                self.assertEqual(model.strip(), payloads[-1]["model"])

    def test_other_models_keep_their_default_thinking_behavior(self):
        payloads = []
        helpers = _load_ollama_call_helpers(
            lambda _url, json, timeout: payloads.append(json) or _FakeOllamaResponse()
        )
        for model in ("gemma3:latest", "qwen3", "gpt-oss", "custom-gemma4", "gemma4-custom", "team/gemma4"):
            with self.subTest(model=model):
                self.assertEqual("local ok", helpers["_call_ollama"]("system", [], "hi", model=model, num_predict=64))
                self.assertNotIn("think", payloads[-1])

    def test_unbounded_gemma4_keeps_default_thinking_behavior(self):
        payloads = []
        helpers = _load_ollama_call_helpers(
            lambda _url, json, timeout: payloads.append(json) or _FakeOllamaResponse()
        )
        for budget in (None, 0, -1, -2):
            with self.subTest(budget=budget):
                self.assertEqual("local ok", helpers["_call_ollama"]("system", [], "hi", model="gemma4:latest", num_predict=budget))
                self.assertNotIn("think", payloads[-1])
                self.assertNotIn("num_predict", payloads[-1]["options"])

    def test_call_ollama_can_use_empty_reply_fallback(self):
        def fake_post(url, json, timeout):
            return _FakeEmptyOllamaResponse()

        helpers = _load_ollama_call_helpers(fake_post)

        reply = helpers["_call_ollama"]("system", [], "Reply with exactly one word: pong", empty_fallback="pong")

        self.assertEqual("pong", reply)

    def test_call_ollama_timeout_can_return_simple_chat_fallback(self):
        payloads = []

        def fake_post(url, json, timeout):
            payloads.append((url, json, timeout))
            raise requests.exceptions.Timeout()

        helpers = _load_ollama_call_helpers(fake_post)

        reply = helpers["_call_ollama"]("system", [], "hi", empty_fallback="I'm here.", timeout=1.5)

        self.assertEqual("I'm here.", reply)
        self.assertEqual(1.5, payloads[0][2])


if __name__ == "__main__":
    unittest.main()
