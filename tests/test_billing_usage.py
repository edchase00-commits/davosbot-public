import ast
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path


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


def _load_billing_command():
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    needed_assigns = {"_MODEL_ROUTE_ALIASES", "_MODEL_ROUTE_DESCRIPTIONS"}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_cmd_billing",
            "_model_route_snapshot",
            "_model_provider_detail",
            "_optional_env_note",
            "_normalize_model_route",
            "_parse_model_request",
            "_model_request_risk",
            "_cmd_model_options",
            "_cmd_model_intensity",
            "_cmd_model_request",
            "_cmd_model",
            "_cmd_model_status",
        }
    ]
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if target_names & needed_assigns:
                nodes.insert(0, node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": __import__("re"),
        "sqlite3": types.SimpleNamespace(connect=_ClosingConnection),
        "BOT_DB_PATH": "",
        "os": __import__("os"),
        "check_action_permission": lambda sender, action: None,
        "is_owner": lambda sender: sender == "+15550000001",
        "redact_secret": lambda text: text.replace("sk-test", "[REDACTED]"),
        "_refresh_change_log_export": lambda: "\nSnapshot refreshed for SSH: exports/private/change_log_board.md",
        "ADVANCED_CODE_MODEL": "gemini:gemini-3.5-flash",
        "ADVANCED_TEXT_MODEL": "gemini-3.5-flash",
        "ADVANCED_VISION_MODEL": "gemini-3.1-flash-image",
        "GEMINI_DAILY_ALERT_USD": 0.25,
        "GEMINI_DAILY_BUDGET_USD": 1.00,
        "GEMINI_ENABLED": True,
        "GEMINI_API_KEY": "gemini-key",
        "GEMINI_IMAGE_MODEL": "gemini-3.1-flash-image",
        "GEMINI_INPUT_RATE_USD": 0.30 / 1_000_000,
        "GEMINI_MODEL": "gemini-3.1-flash-lite",
        "GEMINI_OUTPUT_RATE_USD": 2.50 / 1_000_000,
        "GEMINI_REWRITE_MODEL": "gemini-3.1-flash-lite",
        "IMAGE_PROVIDER": "auto",
        "IMAGE_SCAN_PROVIDER": "auto",
        "LOCAL_IMAGE_ENDPOINT": "http://127.0.0.1:7861/generate",
        "LOCAL_IMAGE_MODEL": "flux",
        "MODEL_ROUTE_CODE_REVIEW": "gemini:gemini-3.5-flash",
        "MODEL_ROUTE_COMPLEX_REASONING": "gemini:gemini-3.5-flash",
        "MODEL_ROUTE_HELPER_REWRITE": "gemini:gemini-3.1-flash-lite",
        "MODEL_ROUTE_IMAGE_GENERATION": "auto:flux",
        "MODEL_ROUTE_IMAGE_SCAN": "auto:gemini-3.1-flash-image",
        "MODEL_ROUTE_NANO_BANANA_IMAGE": "gemini:gemini-3.1-flash-image",
        "MODEL_ROUTE_SIMPLE_CHAT": "ollama:gemma3",
        "MODEL_ROUTE_TOOL_USE": "gemini:gemini-3.1-flash-lite",
        "NANO_BANANA_IMAGE_ASPECT_RATIO": "1:1",
        "NANO_BANANA_IMAGE_MODEL": "gemini-3.1-flash-image",
        "NANO_BANANA_IMAGE_SIZE": "2K",
        "OLLAMA_MODEL": "gemma4",
        "OLLAMA_NUM_CTX": 8192,
        "OLLAMA_SIMPLE_CHAT_MODEL": "gemma3",
        "OPENAI_API_KEY": "",
        "OPENAI_IMAGE_MODEL": "",
        "OPENAI_VISION_MODEL": "",
        "choose_generation_provider": lambda: "local",
        "choose_scan_provider": lambda: "gemini",
    }
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


def _load_tool_rewrite_helpers(fake_requests):
    tree = ast.parse((ROOT / "davosbot" / "tools.py").read_text(encoding="utf-8"))
    names = {"_log_gemini_usage", "_gemini_rewrite"}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "BOT_DB_PATH": "",
        "check_gemini_budget": lambda source: types.SimpleNamespace(allowed=True, reason=""),
        "GEMINI_API_KEY": "test-key",
        "_GEMINI_URL": "https://example.invalid/gemini",
        "requests": fake_requests,
        "sqlite3": types.SimpleNamespace(connect=_ClosingConnection),
        "logger": types.SimpleNamespace(warning=lambda *args, **kwargs: None),
    }
    exec(compile(module, str(ROOT / "davosbot" / "tools.py"), "exec"), namespace)
    return namespace


class BillingUsageTests(unittest.TestCase):
    def test_billing_uses_current_gemini_flash_rates(self):
        helpers = _load_billing_command()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            helpers["BOT_DB_PATH"] = db_path
            with _ClosingConnection(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE gemini_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                        prompt_tokens INTEGER NOT NULL,
                        candidates_tokens INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL,
                        source TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO gemini_usage (prompt_tokens, candidates_tokens, total_tokens, source) VALUES (?,?,?,?)",
                    (10_000, 2_000, 12_000, "direct"),
                )

            reply = helpers["_cmd_billing"]("+15550000001")

        self.assertIn("Pricing basis: configured Gemini estimate.", reply)
        self.assertIn("mixed-model rows need manual review", reply)
        self.assertIn("Today: $0.0080", reply)
        self.assertIn("Est. cost: $0.0080", reply)
        self.assertIn("All-time est. cost: $0.0080", reply)
        self.assertIn("Daily hard budget: $1.00", reply)
        self.assertIn("Emergency shutoff", reply)

    def test_model_status_shows_configured_routes_without_secrets(self):
        helpers = _load_billing_command()

        reply = helpers["_cmd_model_status"]("+15550000001")

        self.assertIn("Chat primary: Ollama gemma3", reply)
        self.assertIn("Ollama keep-warm/default model: gemma4", reply)
        self.assertIn("Gemini fallback/tool-use: gemini-3.1-flash-lite", reply)
        self.assertIn("Ollama context window: num_ctx=8192", reply)
        self.assertIn("Gemini rewrite helpers: gemini-3.1-flash-lite", reply)
        self.assertIn("Route complex reasoning/planning: gemini:gemini-3.5-flash", reply)
        self.assertIn("Route code review/cleanup: gemini:gemini-3.5-flash", reply)
        self.assertIn("Gemini enabled: yes", reply)
        self.assertIn("Gemini key configured: yes", reply)
        self.assertIn("Gemini daily alert/budget: $0.25 / $1.00", reply)
        self.assertIn("Image generation provider: auto", reply)
        self.assertIn("Image scan provider: auto", reply)
        self.assertIn("OpenAI key configured: no", reply)
        self.assertIn("Image generation active detail: local flux worker", reply)
        self.assertIn("Image scan active detail: Gemini gemini-3.1-flash-image", reply)
        self.assertIn("Route Nano Banana image: gemini:gemini-3.1-flash-image", reply)
        self.assertIn("Nano Banana image: gemini-3.1-flash-image; output 2K 1:1", reply)
        self.assertIn("`gpt scan image` is legacy wording", reply)
        self.assertIn("OpenAI/GPT is not used by default routes.", reply)
        self.assertIn("model request", reply)
        self.assertNotIn("API_KEY", reply)

    def test_model_options_explain_review_only_phone_handoff(self):
        helpers = _load_billing_command()

        reply = helpers["_cmd_model"]("model options", "+15550000001")

        self.assertIn("model request [route] [model or goal]", reply)
        self.assertIn("model intensity", reply)
        self.assertIn("chat: Ollama gemma3 simple-chat primary", reply)
        self.assertIn("keep-warm model=gemma4", reply)
        self.assertIn("num_ctx=8192", reply)
        self.assertIn("image: active local (local flux worker); auto falls back only to Gemini. Label: auto:flux", reply)
        self.assertIn("nano banana: explicit Gemini image route gemini:gemini-3.1-flash-image; output 2K 1:1; separate queue.", reply)
        self.assertIn("vision: active gemini (Gemini gemini-3.1-flash-image). Label: auto:gemini-3.1-flash-image", reply)
        self.assertIn("Power ranking:", reply)
        self.assertIn("Gemini gemini-3.5-flash: rare owner-only pro thinking/code-review route.", reply)
        self.assertIn("Empty optional route envs fall back", reply)
        self.assertNotIn("(GEMINI_MODEL)", reply)
        self.assertIn("Codex labels stay review-only", reply)

    def test_model_intensity_explains_ladder_without_secrets(self):
        helpers = _load_billing_command()

        reply = helpers["_cmd_model"]("model intensity", "+15550000001")

        self.assertIn("Model intensity ladder:", reply)
        self.assertIn("Casual chat: Ollama gemma3", reply)
        self.assertIn("Tool mode: Gemini gemini-3.1-flash-lite", reply)
        self.assertIn("Helper rewrite: Gemini gemini-3.1-flash-lite", reply)
        self.assertIn("Complex reasoning/planning: gemini:gemini-3.5-flash", reply)
        self.assertIn("Image gen: auto:flux, currently resolves to local", reply)
        self.assertIn("Nano Banana: gemini:gemini-3.1-flash-image, explicit only, 2K 1:1.", reply)
        self.assertIn("Image scan: auto:gemini-3.1-flash-image, currently resolves to gemini", reply)
        self.assertIn("Code review / cleanup: gemini:gemini-3.5-flash", reply)
        self.assertIn("Stronger models do not bypass permissions.", reply)
        self.assertIn("Premium direct routes are owner-only and logged.", reply)
        self.assertNotIn("gemini-key", reply)
        self.assertNotIn("API_KEY", reply)

    def test_model_request_logs_review_only_change(self):
        helpers = _load_billing_command()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            helpers["BOT_DB_PATH"] = db_path
            with _ClosingConnection(db_path) as conn:
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

            reply = helpers["_cmd_model"]("model request rewrite gemini-3.1-flash-lite", "+15550000001")
            with _ClosingConnection(db_path) as conn:
                row = conn.execute("SELECT request, reason FROM change_log").fetchone()

        self.assertIn("Model request logged #1 [YELLOW]", reply)
        self.assertIn("Review-only", reply)
        self.assertIn("Snapshot refreshed for SSH", reply)
        self.assertIn("[MODEL-CHANGE YELLOW]", row[0])
        self.assertIn("requested_route=rewrite", row[1])
        self.assertIn("requested_model=gemini-3.1-flash-lite", row[1])
        self.assertIn("current_route_complex_reasoning=gemini:gemini-3.5-flash", row[1])
        self.assertIn("current_route_nano_banana_image=gemini:gemini-3.1-flash-image", row[1])
        self.assertIn("no .env edit", row[1])

    def test_parse_model_request_keeps_multi_word_model_name(self):
        helpers = _load_billing_command()

        route, model, safe_request = helpers["_parse_model_request"](
            "chat try and fall back to Gemini pro for the next reply"
        )

        self.assertEqual("chat", route)
        self.assertEqual("Gemini pro", model)
        self.assertEqual("chat try and fall back to Gemini pro for the next reply", safe_request)

    def test_model_request_is_owner_only(self):
        helpers = _load_billing_command()

        reply = helpers["_cmd_model"]("model request rewrite gemini-3.1-flash-lite", "+15550000002")

        self.assertIn("owner-only", reply)

    def test_tools_rewrite_url_uses_configured_model(self):
        from davosbot import tools

        self.assertIn(f"/models/{tools.GEMINI_REWRITE_MODEL}:generateContent", tools._GEMINI_URL)

    def test_tool_rewrite_logs_gemini_usage(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "usageMetadata": {
                        "promptTokenCount": 123,
                        "candidatesTokenCount": 45,
                        "totalTokenCount": 168,
                    },
                    "candidates": [
                        {"content": {"parts": [{"text": "rewritten text"}]}}
                    ],
                }

        fake_requests = types.SimpleNamespace(post=lambda *args, **kwargs: FakeResponse())
        helpers = _load_tool_rewrite_helpers(fake_requests)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            helpers["BOT_DB_PATH"] = db_path
            with _ClosingConnection(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE gemini_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                        prompt_tokens INTEGER NOT NULL,
                        candidates_tokens INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL,
                        source TEXT NOT NULL
                    )
                    """
                )

            result = helpers["_gemini_rewrite"]("rewrite this")

            with _ClosingConnection(db_path) as conn:
                row = conn.execute(
                    "SELECT prompt_tokens, candidates_tokens, total_tokens, source FROM gemini_usage"
                ).fetchone()

        self.assertEqual("rewritten text", result)
        self.assertEqual((123, 45, 168, "tool_rewrite"), row)


if __name__ == "__main__":
    unittest.main()
