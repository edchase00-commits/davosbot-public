import ast
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import requests
from davosbot.runtime_locks import MODEL_STATE_LOCK


ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, data=None):
        self._data = data if data is not None else {}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def _create_bot_log(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE bot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT,
                event_type TEXT,
                payload TEXT
            )
            """
        )
        conn.commit()


def _load_ollama_helpers(fake_get=None, fake_post=None, now=1_000.0, db_path=""):
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "_ollama_down",
        "_last_ollama_check",
        "_ollama_down_alerted",
        "_ollama_state_epoch",
        "_OLLAMA_RECOVERY_PROBE_SYSTEM",
    }
    wanted_funcs = {
        "_close_response",
        "_log_bot_event",
        "_ollama_model_available",
        "_ollama_keep_alive_value",
        "_ollama_simple_chat_model",
        "_ollama_keep_warm_models",
        "_ollama_probe_payload",
        "_ollama_generation_available",
        "_warm_single_ollama_model",
        "_ollama_keep_warm_loop",
        "_log_ollama_state",
        "_latest_ollama_state",
        "initialize_ollama_recovery_state",
        "_ollama_health_check",
        "_mark_ollama_down",
        "check_ollama_recovery",
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

    alerts = []
    restarts = []
    namespace = {
        "MODEL_STATE_LOCK": MODEL_STATE_LOCK,
        "OLLAMA_CHECK_INTERVAL": 300,
        "OLLAMA_HEALTH_TIMEOUT": 5,
        "OLLAMA_SIMPLE_CHAT_NUM_PREDICT": 64,
        "OLLAMA_SIMPLE_CHAT_TIMEOUT": 3.5,
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        "OLLAMA_MODEL": "gemma4",
        "OLLAMA_SIMPLE_CHAT_MODEL": "gemma3",
        "OLLAMA_NUM_CTX": 8192,
        "OLLAMA_KEEP_ALIVE": "1h",
        "BOT_DB_PATH": db_path,
        "closing": closing,
        "json": json,
        "sqlite3": sqlite3,
        "logger": SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None),
        "redact_secret": lambda value: value,
        "requests": SimpleNamespace(
            get=fake_get or (lambda *args, **kwargs: _FakeResponse()),
            post=fake_post or (lambda *args, **kwargs: _FakeResponse({"message": {"content": "pong"}})),
            exceptions=requests.exceptions,
        ),
        "time": SimpleNamespace(time=lambda: now),
        "_notify_owner": lambda message, event_type="owner_notice": alerts.append((event_type, message)),
        "_try_restart_ollama": lambda: restarts.append(True),
    }
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    return namespace, alerts, restarts


class OllamaRecoveryTests(unittest.TestCase):
    def test_recovery_check_waits_for_interval(self):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("health check should not run yet")

        helpers, alerts, _restarts = _load_ollama_helpers(fail_if_called)
        helpers["_ollama_down"] = True
        helpers["_last_ollama_check"] = 800.0

        recovered = helpers["check_ollama_recovery"](now=900.0)

        self.assertFalse(recovered)
        self.assertTrue(helpers["_ollama_down"])
        self.assertEqual([], alerts)

    def test_recovery_check_is_silent_when_only_fallback_was_used(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            _create_bot_log(db_path)
            helpers, alerts, _restarts = _load_ollama_helpers(
                lambda *args, **kwargs: _FakeResponse({"models": [{"name": "gemma4:latest"}]}),
                db_path=db_path,
            )
            helpers["_ollama_down"] = True
            helpers["_last_ollama_check"] = 600.0

            recovered = helpers["check_ollama_recovery"](now=1_000.0)

            self.assertTrue(recovered)
            self.assertFalse(helpers["_ollama_down"])
            self.assertFalse(helpers["_ollama_down_alerted"])
            self.assertEqual([], alerts)
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute("SELECT event_type FROM bot_log ORDER BY id").fetchall()
            self.assertEqual([("ollama_recovered",)], rows)

    def test_recovery_check_alerts_once_when_escalated_outage_recovers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            _create_bot_log(db_path)
            helpers, alerts, _restarts = _load_ollama_helpers(
                lambda *args, **kwargs: _FakeResponse({"models": [{"name": "gemma4:latest"}]}),
                db_path=db_path,
            )
            helpers["_ollama_down"] = True
            helpers["_ollama_down_alerted"] = True
            helpers["_last_ollama_check"] = 600.0

            recovered = helpers["check_ollama_recovery"](now=1_000.0)

            self.assertTrue(recovered)
            self.assertFalse(helpers["_ollama_down"])
            self.assertFalse(helpers["_ollama_down_alerted"])
            self.assertEqual(1, len(alerts))
            self.assertEqual("ollama_recovered", alerts[0][0])
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute("SELECT event_type FROM bot_log ORDER BY id").fetchall()
            self.assertEqual([("ollama_recovered",)], rows)

    def test_recovery_check_keeps_down_when_model_missing(self):
        helpers, alerts, _restarts = _load_ollama_helpers(
            lambda *args, **kwargs: _FakeResponse({"models": [{"name": "llama3:latest"}]})
        )
        helpers["_ollama_down"] = True
        helpers["_last_ollama_check"] = 600.0

        recovered = helpers["check_ollama_recovery"](now=1_000.0)

        self.assertFalse(recovered)
        self.assertTrue(helpers["_ollama_down"])
        self.assertEqual([], alerts)
        self.assertEqual(1_000.0, helpers["_last_ollama_check"])

    def test_recovery_check_keeps_down_when_generation_probe_times_out(self):
        def fake_get(*args, **kwargs):
            return _FakeResponse({"models": [{"name": "gemma4:latest"}]})

        def fake_post(*args, **kwargs):
            raise requests.exceptions.Timeout()

        helpers, alerts, _restarts = _load_ollama_helpers(fake_get, fake_post=fake_post)
        helpers["_ollama_down"] = True
        helpers["_last_ollama_check"] = 600.0

        recovered = helpers["check_ollama_recovery"](now=1_000.0)

        self.assertFalse(recovered)
        self.assertTrue(helpers["_ollama_down"])
        self.assertEqual([], alerts)
        self.assertEqual(1_000.0, helpers["_last_ollama_check"])

    def test_recovery_check_requires_generation_probe_content(self):
        calls = []

        def fake_get(*args, **kwargs):
            return _FakeResponse({"models": [{"name": "gemma4:latest"}]})

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return _FakeResponse({"message": {"content": ""}})

        helpers, alerts, _restarts = _load_ollama_helpers(fake_get, fake_post=fake_post)
        helpers["_ollama_down"] = True
        helpers["_last_ollama_check"] = 600.0

        recovered = helpers["check_ollama_recovery"](now=1_000.0)

        self.assertFalse(recovered)
        self.assertTrue(helpers["_ollama_down"])
        self.assertEqual([], alerts)
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0][0].endswith("/api/chat"))
        self.assertEqual("1h", calls[0][1]["json"]["keep_alive"])
        self.assertEqual(8192, calls[0][1]["json"]["options"]["num_ctx"])
        self.assertEqual(1, calls[0][1]["json"]["options"]["num_predict"])
        self.assertIs(False, calls[0][1]["json"]["think"])
        self.assertEqual([{"role": "user", "content": "Reply with exactly one word: pong"}], calls[0][1]["json"]["messages"])
        self.assertEqual(3.5, calls[0][1]["timeout"])

    def test_gemma4_short_and_realistic_probes_request_final_output(self):
        helpers, _alerts, _restarts = _load_ollama_helpers()
        for model in ("gemma4", "gemma4:latest", "gemma4:e4b"):
            for realistic in (False, True):
                with self.subTest(model=model, realistic=realistic):
                    payload = helpers["_ollama_probe_payload"](model=model, realistic=realistic)
                    self.assertIs(False, payload["think"])
                    self.assertEqual(model, payload["model"])
                    self.assertEqual(64 if realistic else 1, payload["options"]["num_predict"])
                    self.assertEqual(8192, payload["options"]["num_ctx"])

    def test_other_model_probes_keep_default_thinking_behavior(self):
        helpers, _alerts, _restarts = _load_ollama_helpers()
        for model in ("gemma3:latest", "qwen3", "gpt-oss", "custom-gemma4", "gemma4-custom", "team/gemma4"):
            with self.subTest(model=model):
                payload = helpers["_ollama_probe_payload"](model=model)
                self.assertNotIn("think", payload)
                self.assertEqual(model, payload["model"])
                self.assertEqual(1, payload["options"]["num_predict"])

    def test_health_check_restarts_on_connection_error(self):
        def connection_error(*args, **kwargs):
            raise requests.exceptions.ConnectionError("refused")

        helpers, alerts, restarts = _load_ollama_helpers(connection_error)
        helpers["_ollama_down"] = True
        helpers["_last_ollama_check"] = 600.0

        recovered = helpers["check_ollama_recovery"](now=1_000.0)

        self.assertFalse(recovered)
        self.assertTrue(helpers["_ollama_down"])
        self.assertEqual([], alerts)
        self.assertEqual([True], restarts)

    def test_keep_warm_loop_marks_down_after_failed_generation_health(self):
        class StopLoop(Exception):
            pass

        helpers, _alerts, _restarts = _load_ollama_helpers()
        calls = []

        def stop_sleep(_interval):
            raise StopLoop()

        helpers["warm_ollama_model"] = lambda source="manual": calls.append(("warm", source)) or True
        helpers["_ollama_tags_available"] = lambda: False
        helpers["_mark_ollama_down"] = lambda notify=False: calls.append(("down", notify))
        helpers["time"] = SimpleNamespace(sleep=stop_sleep)

        with self.assertRaises(StopLoop):
            helpers["_ollama_keep_warm_loop"](60)

        self.assertEqual([("down", False), ("warm", "startup")], calls)

    def test_mark_down_logs_silent_by_default(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            _create_bot_log(db_path)
            helpers, alerts, _restarts = _load_ollama_helpers(db_path=db_path)

            helpers["_mark_ollama_down"]()
            helpers["_mark_ollama_down"]()

            self.assertTrue(helpers["_ollama_down"])
            self.assertFalse(helpers["_ollama_down_alerted"])
            self.assertEqual([], alerts)
            self.assertEqual(1_000.0, helpers["_last_ollama_check"])
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute("SELECT event_type FROM bot_log ORDER BY id").fetchall()
            self.assertEqual([("ollama_down",)], rows)

    def test_mark_down_can_escalate_alert_once(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            _create_bot_log(db_path)
            helpers, alerts, _restarts = _load_ollama_helpers(db_path=db_path)

            helpers["_mark_ollama_down"](notify=True)
            helpers["_mark_ollama_down"](notify=True)

            self.assertTrue(helpers["_ollama_down"])
            self.assertTrue(helpers["_ollama_down_alerted"])
            self.assertEqual(1, len(alerts))
            self.assertEqual("model_fallback_failed", alerts[0][0])
            self.assertEqual(1_000.0, helpers["_last_ollama_check"])
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute("SELECT event_type FROM bot_log ORDER BY id").fetchall()
            self.assertEqual([("ollama_down",)], rows)

    def test_startup_restores_pending_down_state_from_bot_log(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            _create_bot_log(db_path)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                    ("system", "ollama_down", "{}"),
                )
                conn.commit()
            helpers, alerts, _restarts = _load_ollama_helpers(db_path=db_path)

            state = helpers["initialize_ollama_recovery_state"]()

            self.assertEqual("ollama_down", state)
            self.assertTrue(helpers["_ollama_down"])
            self.assertFalse(helpers["_ollama_down_alerted"])
            self.assertEqual(0.0, helpers["_last_ollama_check"])
            self.assertEqual([], alerts)


if __name__ == "__main__":
    unittest.main()
