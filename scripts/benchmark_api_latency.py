#!/usr/bin/env python3
"""Benchmark DavosBot model and handler latency.

This script is intentionally side-effect light:
- direct provider calls use a tiny prompt and capped output
- synthetic handler runs monkeypatch outbound iMessage sends
- synthetic handler runs use a temporary bot DB by default
- results are written as JSONL under exports/private/latency/
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import importlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "exports" / "private" / "latency"
DEFAULT_PROMPT = "Reply with exactly one word: pong"
DEFAULT_GEMINI_CANDIDATES = (
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
)
DEFAULT_OPENAI_CANDIDATES = (
    "gpt-5.4-nano",
    "gpt-5.4-mini",
)
_HANDLER_STATE: dict[str, Any] | None = None


def _load_dotenv(path: Path | None) -> None:
    if path is None:
        return
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(path, override=False)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 6)


def _safe_model_name(model: str) -> str:
    return (model or "").replace("models/", "").strip()


def _unique(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _safe_model_name(value)
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _write_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = min(len(ordered) - 1, max(0.0, (len(ordered) - 1) * pct))
    lo = int(pos)
    hi = min(len(ordered) - 1, lo + 1)
    if lo == hi:
        return round(ordered[lo], 4)
    weight = pos - lo
    return round(ordered[lo] + ((ordered[hi] - ordered[lo]) * weight), 4)


def _summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in records:
        key = (
            str(row.get("layer", "")),
            str(row.get("provider", "")),
            str(row.get("model", "")),
        )
        groups.setdefault(key, []).append(row)

    rows: list[dict[str, Any]] = []
    for (layer, provider, model), items in sorted(groups.items()):
        durations = [
            float(item["elapsed_seconds"])
            for item in items
            if item.get("ok") and isinstance(item.get("elapsed_seconds"), (int, float))
        ]
        rows.append({
            "layer": layer,
            "provider": provider,
            "model": model,
            "runs": len(items),
            "ok": sum(1 for item in items if item.get("ok")),
            "fail": sum(1 for item in items if not item.get("ok")),
            "median_s": _percentile(durations, 0.50),
            "p90_s": _percentile(durations, 0.90),
            "p95_s": _percentile(durations, 0.95),
            "min_s": round(min(durations), 4) if durations else None,
            "max_s": round(max(durations), 4) if durations else None,
        })
    return rows


def _print_summary(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No benchmark rows produced.")
        return
    print("layer provider model runs ok fail median_s p90_s p95_s min_s max_s")
    for row in rows:
        print(
            f"{row['layer']} {row['provider']} {row['model'] or '-'} "
            f"{row['runs']} {row['ok']} {row['fail']} "
            f"{row['median_s']} {row['p90_s']} {row['p95_s']} "
            f"{row['min_s']} {row['max_s']}"
        )


def _request_json(
    method: str,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, int | None, dict[str, Any] | None, str | None]:
    resp = None
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=timeout,
        )
        status = resp.status_code
        if not resp.ok:
            return False, status, None, f"http_{status}"
        try:
            return True, status, resp.json(), None
        except Exception:
            return False, status, None, "json_parse_error"
    except requests.exceptions.Timeout:
        return False, None, None, "timeout"
    except requests.exceptions.ConnectionError:
        return False, None, None, "connection_error"
    except Exception as exc:
        return False, None, None, type(exc).__name__
    finally:
        if resp is not None:
            resp.close()


def discover_gemini_models(timeout: float) -> list[str]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return []
    ok, _status, data, _err = _request_json(
        "GET",
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": api_key},
        timeout=timeout,
    )
    if not ok or not isinstance(data, dict):
        return []
    models = []
    for item in data.get("models", []):
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        models.append(_safe_model_name(str(item.get("name", ""))))
    return _unique(models)


def discover_ollama_models(host: str, timeout: float) -> list[str]:
    ok, _status, data, _err = _request_json("GET", f"{host.rstrip('/')}/api/tags", timeout=timeout)
    if not ok or not isinstance(data, dict):
        return []
    names = []
    for item in data.get("models", []):
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("model") or ""))
    return _unique(names)


def configured_models() -> dict[str, Any]:
    config = importlib.import_module("davosbot.config")
    gemini = _unique([
        getattr(config, "GEMINI_MODEL", ""),
        getattr(config, "GEMINI_REWRITE_MODEL", ""),
        getattr(config, "ADVANCED_TEXT_MODEL", ""),
        *DEFAULT_GEMINI_CANDIDATES,
    ])
    return {
        "gemini": gemini,
        "ollama": _unique([getattr(config, "OLLAMA_MODEL", "")]),
        "openai": list(DEFAULT_OPENAI_CANDIDATES),
        "ollama_host": getattr(config, "OLLAMA_HOST", "http://localhost:11434"),
        "owner_id": getattr(config, "OWNER_ID", ""),
    }


def bench_gemini(model: str, prompt: str, timeout: float) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    started = time.perf_counter()
    if not api_key:
        return {"ok": False, "elapsed_seconds": 0.0, "error": "missing_gemini_api_key"}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
    }
    ok, status, data, err = _request_json(
        "POST",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        payload=payload,
        timeout=timeout,
    )
    row: dict[str, Any] = {
        "ok": ok,
        "elapsed_seconds": _elapsed(started),
        "status_code": status,
    }
    if err:
        row["error"] = err
    if ok and data:
        usage = data.get("usageMetadata") or {}
        row["prompt_tokens"] = usage.get("promptTokenCount")
        row["output_tokens"] = usage.get("candidatesTokenCount")
        row["total_tokens"] = usage.get("totalTokenCount")
    return row


def bench_ollama(host: str, model: str, prompt: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_predict": 16, "temperature": 0},
    }
    ok, status, data, err = _request_json(
        "POST",
        f"{host.rstrip('/')}/api/chat",
        payload=payload,
        timeout=timeout,
    )
    row: dict[str, Any] = {
        "ok": ok,
        "elapsed_seconds": _elapsed(started),
        "status_code": status,
    }
    if err:
        row["error"] = err
    if ok and data:
        row["eval_count"] = data.get("eval_count")
        row["eval_duration_ns"] = data.get("eval_duration")
        row["prompt_eval_duration_ns"] = data.get("prompt_eval_duration")
    return row


def bench_openai(model: str, prompt: str, timeout: float) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    started = time.perf_counter()
    if not api_key:
        return {"ok": False, "elapsed_seconds": 0.0, "error": "missing_openai_api_key"}
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": 16,
    }
    ok, status, data, err = _request_json(
        "POST",
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}"},
        payload=payload,
        timeout=timeout,
    )
    row: dict[str, Any] = {
        "ok": ok,
        "elapsed_seconds": _elapsed(started),
        "status_code": status,
    }
    if err:
        row["error"] = err
    if ok and data:
        usage = data.get("usage") or {}
        row["input_tokens"] = usage.get("input_tokens")
        row["output_tokens"] = usage.get("output_tokens")
        row["total_tokens"] = usage.get("total_tokens")
    return row


@contextlib.contextmanager
def _patched(obj: object, name: str, value: object) -> Any:
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _clear_davosbot_modules() -> None:
    for name in list(sys.modules):
        if name == "davosbot" or name.startswith("davosbot."):
            sys.modules.pop(name, None)


def _handler_state() -> dict[str, Any]:
    global _HANDLER_STATE
    if _HANDLER_STATE is not None:
        return _HANDLER_STATE

    fd, path = tempfile.mkstemp(prefix="davosbot-latency-", suffix=".db")
    os.close(fd)
    os.environ["BOT_DB_PATH"] = path
    _clear_davosbot_modules()

    import davosbot.brain as brain
    import davosbot.main as main
    import davosbot.memory as memory

    memory.init_db()
    send_calls: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    phase: dict[str, float] = {}

    def fake_send(recipient: str, text: str, is_group: bool = False, recovery_mode: str = "background") -> bool:
        send_started = time.perf_counter()
        send_calls.append({
            "recipient_tail": recipient[-4:] if recipient else "",
            "text_len": len(text or ""),
            "is_group": is_group,
            "recovery_mode": recovery_mode,
        })
        phase["fake_send_seconds"] = phase.get("fake_send_seconds", 0.0) + (time.perf_counter() - send_started)
        return True

    patches = [
        _patched(main, "send_message", fake_send),
        _patched(main, "check_rate_limit", lambda _sender: True),
        _patched(main, "update_heartbeat", lambda: None),
        _patched(main, "save_turn", lambda *_args, **_kwargs: None),
        _patched(main, "extract_and_update_memory", lambda *_args, **_kwargs: None),
        _patched(main, "_log_message_trace", lambda trace, elapsed: trace_rows.append(trace.payload(elapsed)) if trace else None),
        _patched(main, "_log_quality_signal", lambda *_args, **_kwargs: None),
        _patched(brain, "send_message", fake_send),
        _patched(brain, "_mark_ollama_down", lambda notify=True: None),
        _patched(brain, "_notify_owner", lambda *_args, **_kwargs: None),
        _patched(brain, "_try_restart_ollama", lambda: None),
    ]
    stack = contextlib.ExitStack()
    for patch in patches:
        stack.enter_context(patch)

    def cleanup() -> None:
        stack.close()
        with contextlib.suppress(OSError):
            os.unlink(path)

    atexit.register(cleanup)
    _HANDLER_STATE = {
        "main": main,
        "db_path": path,
        "send_calls": send_calls,
        "trace_rows": trace_rows,
        "phase": phase,
        "owner_id": main.OWNER_ID or os.getenv("OWNER_ID") or "+15550000001",
    }
    return _HANDLER_STATE


def bench_synthetic_handler(prompt: str, mode: str, timeout: float) -> dict[str, Any]:
    """Run DavosBot's DM handler without sending a real iMessage."""
    del timeout
    state = _handler_state()
    main = state["main"]
    owner_id = state["owner_id"]
    send_calls = state["send_calls"]
    trace_rows = state["trace_rows"]
    phase = state["phase"]
    send_before = len(send_calls)
    trace_before = len(trace_rows)
    phase_before = dict(phase)
    started = time.perf_counter()

    def fake_get_response(*_args: Any, **_kwargs: Any) -> str:
        model_started = time.perf_counter()
        phase["fake_model_seconds"] = phase.get("fake_model_seconds", 0.0) + (time.perf_counter() - model_started)
        return "pong"

    stack = contextlib.ExitStack()
    try:
        if mode == "fake_model":
            stack.enter_context(_patched(main, "get_response", fake_get_response))
        msg = {
            "sender": owner_id,
            "chat_identifier": owner_id,
            "text": prompt,
            "image_path": None,
        }
        main.handle_message(msg)
        ok = True
        err = None
    except Exception as exc:
        ok = False
        err = type(exc).__name__
    finally:
        stack.close()

    elapsed = _elapsed(started)
    row: dict[str, Any] = {
        "ok": ok,
        "elapsed_seconds": elapsed,
        "send_calls": len(send_calls) - send_before,
        "mode": mode,
    }
    for key, value in phase.items():
        row[key] = round(value - phase_before.get(key, 0.0), 6)
    if len(trace_rows) > trace_before:
        trace_payload = trace_rows[-1]
        row["trace_route"] = trace_payload.get("route")
        row["trace_prompt_chars"] = trace_payload.get("prompt_chars")
        row["trace_history_turns"] = trace_payload.get("history_turns")
        row["trace_flags"] = trace_payload.get("flags")
        phases = trace_payload.get("phases") or {}
        if isinstance(phases, dict):
            for key, value in phases.items():
                if isinstance(value, (int, float)):
                    row[f"trace_phase_{key}"] = value
    if err:
        row["error"] = err
    return row


def _pick_models(args: argparse.Namespace, discovered: dict[str, list[str]], configured: dict[str, Any]) -> dict[str, list[str]]:
    gemini = _unique(args.gemini_model or configured["gemini"])
    if discovered["gemini"]:
        available = set(discovered["gemini"])
        gemini = [model for model in gemini if model in available]
    ollama = _unique(args.ollama_model or configured["ollama"] or discovered["ollama"])
    if discovered["ollama"]:
        available_o = set(discovered["ollama"])
        ollama = [
            model
            for model in ollama
            if (
                model in available_o
                or any(name == model or name.startswith(f"{model}:") for name in available_o)
            )
        ]
    openai = _unique(args.openai_model or configured["openai"])
    return {"gemini": gemini, "ollama": ollama, "openai": openai}


def _layers(args: argparse.Namespace) -> list[str]:
    layers = []
    if args.include_api:
        layers.append("api")
    if args.include_handler_real:
        layers.append("handler_real")
    if args.include_handler_fake:
        layers.append("handler_fake")
    return layers


def run(args: argparse.Namespace) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    if args.env_file:
        _load_dotenv(Path(args.env_file))
    os.environ.setdefault("DAVOSBOT_SUPPRESS_CONFIG_WARNINGS", "1")
    if args.simple_chat_route:
        os.environ["MODEL_ROUTE_SIMPLE_CHAT"] = args.simple_chat_route
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    configured = configured_models()
    discovered = {
        "gemini": discover_gemini_models(args.timeout) if args.discover else [],
        "ollama": discover_ollama_models(configured["ollama_host"], args.timeout) if args.discover else [],
        "openai": [],
    }
    models = _pick_models(args, discovered, configured)

    run_id = args.run_id or f"latency-{_now_ms()}-{uuid.uuid4().hex[:8]}"
    output_path = Path(args.output_dir) / f"{run_id}.jsonl"
    records: list[dict[str, Any]] = []
    layers = _layers(args)
    if not layers:
        raise SystemExit("No layers selected.")

    api_jobs: list[tuple[str, str, Callable[[], dict[str, Any]]]] = []
    if args.include_api:
        for model in models["gemini"]:
            api_jobs.append(("gemini", model, lambda m=model: bench_gemini(m, args.prompt, args.timeout)))
        for model in models["ollama"]:
            api_jobs.append(("ollama", model, lambda m=model: bench_ollama(configured["ollama_host"], m, args.prompt, args.timeout)))
        if args.include_openai:
            for model in models["openai"]:
                api_jobs.append(("openai", model, lambda m=model: bench_openai(m, args.prompt, args.timeout)))
    if args.include_api and not api_jobs:
        print("No API models available after discovery/filtering; API layer skipped.", file=sys.stderr)

    handler_jobs: list[tuple[str, str, str]] = []
    if args.include_handler_real:
        handler_jobs.append(("synthetic_handler", "davosbot", "real_model"))
    if args.include_handler_fake:
        handler_jobs.append(("synthetic_handler", "davosbot", "fake_model"))

    schedule: list[tuple[str, str, str, Callable[[], dict[str, Any]]]] = []
    for provider, model, fn in api_jobs:
        schedule.append(("api", provider, model, fn))
    for provider, model, mode in handler_jobs:
        schedule.append((mode, provider, mode, lambda m=mode: bench_synthetic_handler(args.prompt, m, args.timeout)))
    if not schedule:
        raise SystemExit("No experiments available to run.")

    for idx in range(args.experiments):
        layer, provider, model, fn = schedule[idx % len(schedule)]
        row = {
            "run_id": run_id,
            "experiment": idx + 1,
            "layer": layer,
            "provider": provider,
            "model": model,
            "prompt_len": len(args.prompt),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        result = fn()
        row.update(result)
        _write_jsonl(output_path, row)
        records.append(row)
        if args.progress and (idx + 1) % args.progress == 0:
            print(f"completed {idx + 1}/{args.experiments}")

    summary_rows = _summary(records)
    summary_path = output_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary_rows, indent=2, sort_keys=True), encoding="utf-8")
    return output_path, summary_rows, records


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiments", type=int, default=100)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--progress", type=int, default=10)
    parser.add_argument("--discover", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-api", action="store_true")
    parser.add_argument("--include-openai", action="store_true")
    parser.add_argument("--include-handler-real", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-handler-fake", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--simple-chat-route", default="")
    parser.add_argument("--gemini-model", action="append", default=[])
    parser.add_argument("--ollama-model", action="append", default=[])
    parser.add_argument("--openai-model", action="append", default=[])
    args = parser.parse_args(argv)
    if args.experiments < 1:
        parser.error("--experiments must be >= 1")
    if args.progress < 0:
        parser.error("--progress must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_path, summary_rows, _records = run(args)
    _print_summary(summary_rows)
    print(f"results={output_path}")
    print(f"summary={output_path.with_suffix('.summary.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
