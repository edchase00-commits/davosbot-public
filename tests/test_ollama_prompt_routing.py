import ast
import types
import unittest
from pathlib import Path

import requests


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
