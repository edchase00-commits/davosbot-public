#!/usr/bin/env python3
"""Safe DavosBot operator tools for Codex and MCP clients.

The tools here are intentionally review-first. They may run local diagnostics
or dry-runs, but they do not commit, push, deploy, restart PM2, clear logs, or
print secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DAVOSBOT_SUPPRESS_CONFIG_WARNINGS", "1")

MAX_OUTPUT_CHARS = 8000
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[0-9A-Za-z_]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"sk-[0-9A-Za-z_-]{20,}"),
    re.compile(
        r"\b(api[_-]?key|access[_-]?token|token|password|secret|webhook)\s*[:=]\s*['\"]?[^,\s)]+",
        re.IGNORECASE,
    ),
    re.compile(r"https://[^@\s]+:[^@\s]+@github\.com/[^\s]+", re.IGNORECASE),
)


@dataclass(frozen=True)
class OperatorTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class OperatorResult:
    ok: bool
    text: str
    data: dict[str, Any] | None = None


TOOLS: tuple[OperatorTool, ...] = (
    OperatorTool(
        "sync_status",
        "Show current Codex workspace, git state, branch alignment, and safe operating rules.",
        {
            "type": "object",
            "properties": {"brief": {"type": "boolean", "default": True}},
            "additionalProperties": False,
        },
    ),
    OperatorTool(
        "queue_status",
        "Summarize tracked queue/backlog markers from docs without reading private runtime files.",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 18}},
            "additionalProperties": False,
        },
    ),
    OperatorTool(
        "change_log",
        "Export a redacted phone change-log triage board from a local DavosBot DB path.",
        {
            "type": "object",
            "properties": {
                "db": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            "additionalProperties": False,
        },
    ),
    OperatorTool(
        "quick_smoke",
        "Run the quick deterministic local smoke suite.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    OperatorTool(
        "repo_guard",
        "Run tracked-path cleanliness checks and report whether the worktree is dirty.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    OperatorTool(
        "public_sync_dry_run",
        "Run the sanitized public snapshot dry-run. It copies nothing, commits nothing, and pushes nothing.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    OperatorTool(
        "maintenance_report",
        "Write the private review-only maintenance diagnostics report and return its path.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    OperatorTool(
        "quality_sweep",
        "Run DavosBot's scan-first quality agents and write a private report.",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["light", "full"], "default": "light"},
                "fix": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    ),
)


def _tool_map() -> dict[str, OperatorTool]:
    return {tool.name: tool for tool in TOOLS}


def redact_text(text: str) -> str:
    """Remove obvious credential shapes from operator output."""
    redacted = text or ""
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: _redaction_replacement(match), redacted)
    return redacted


def _redaction_replacement(match: re.Match[str]) -> str:
    if match.lastindex:
        return f"{match.group(1)}=[redacted]"
    return "[redacted]"


def _trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    clean = redact_text(text)
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "\n...[truncated]"


def _run(args: list[str], *, timeout: int = 240) -> OperatorResult:
    try:
        env = os.environ.copy()
        env.setdefault("DAVOSBOT_SUPPRESS_CONFIG_WARNINGS", "1")
        proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        return OperatorResult(False, f"missing executable: {exc.filename}")
    except subprocess.TimeoutExpired:
        return OperatorResult(False, f"command timed out after {timeout}s: {' '.join(args)}")
    output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
    if not output and proc.returncode != 0:
        output = f"exit {proc.returncode}"
    return OperatorResult(proc.returncode == 0, _trim(output))


def _tool_sync_status(arguments: dict[str, Any]) -> OperatorResult:
    from scripts import codex_sync_status

    brief = bool(arguments.get("brief", True))
    status = codex_sync_status.build_status()
    return OperatorResult(True, _trim(codex_sync_status.format_status(status, brief=brief)), status)


def _docs_unchecked_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = raw.strip()
        if stripped.startswith("- [ ]") or stripped.startswith("* [ ]"):
            lines.append(f"{path.as_posix()}: {stripped}")
        if len(lines) >= limit:
            break
    return lines


def _tool_queue_status(arguments: dict[str, Any]) -> OperatorResult:
    limit = int(arguments.get("limit") or 18)
    limit = max(1, min(limit, 40))
    paths = [ROOT / "docs" / "WORK_QUEUE.md", ROOT / "docs" / "TASKS.md"]
    items: list[str] = []
    for path in paths:
        items.extend(_docs_unchecked_lines(path, max(0, limit - len(items))))
        if len(items) >= limit:
            break
    if not items:
        return OperatorResult(True, "No unchecked queue markers found in docs/WORK_QUEUE.md or docs/TASKS.md.")
    text = "DavosBot queue markers:\n" + "\n".join(f"- {item}" for item in items)
    return OperatorResult(True, _trim(text), {"count": len(items)})


def _tool_change_log(arguments: dict[str, Any]) -> OperatorResult:
    from davosbot import config
    from scripts import export_change_log

    db = Path(str(arguments.get("db") or config.BOT_DB_PATH))
    if not db.exists():
        return OperatorResult(True, f"Change log DB not found at {db}. On the Mini, run from the production checkout or pass db.")
    limit = int(arguments.get("limit") or 30)
    limit = max(1, min(limit, 100))
    rows = export_change_log.fetch_rows(db)
    board = export_change_log.format_board(rows, max_per_bucket=limit)
    return OperatorResult(True, _trim(board), {"row_count": len(rows)})


def _project_python() -> str:
    from scripts.python_env import resolve_python_bin

    return resolve_python_bin(ROOT)


def _tool_quick_smoke(_arguments: dict[str, Any]) -> OperatorResult:
    return _run([_project_python(), "scripts/master_smoke.py", "--quick"], timeout=240)


def _tool_repo_guard(_arguments: dict[str, Any]) -> OperatorResult:
    guard = _run([sys.executable, "scripts/check_repo_cleanliness.py", "--all"], timeout=120)
    status = _run(["git", "status", "--short"], timeout=60)
    dirty_lines = [line for line in status.text.splitlines() if line.strip()] if status.ok else []
    text = "\n".join(
        [
            "Repo guard:",
            "PASS tracked-path cleanliness" if guard.ok else "FAIL tracked-path cleanliness",
            guard.text,
            ("worktree: unavailable" if not status.ok else
             f"worktree: {'clean' if not dirty_lines else f'dirty ({len(dirty_lines)} file(s))'}"),
        ]
    )
    return OperatorResult(guard.ok and status.ok and not dirty_lines, _trim(text),
                          {"dirty_count": len(dirty_lines) if status.ok else None})


def _powershell_executable() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _tool_public_sync_dry_run(_arguments: dict[str, Any]) -> OperatorResult:
    shell = _powershell_executable()
    if not shell:
        return OperatorResult(False, "PowerShell is unavailable; cannot run public snapshot dry-run on this host.")
    return _run(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/publish_public_snapshot.ps1",
            "-DryRun",
        ],
        timeout=240,
    )


def _tool_maintenance_report(_arguments: dict[str, Any]) -> OperatorResult:
    from scripts import maintenance_diagnostics

    result = maintenance_diagnostics.collect_diagnostics(update_state=False)
    report = result.report
    rel = report.relative_to(ROOT) if report.is_relative_to(ROOT) else report
    return OperatorResult(result.ok, f"Maintenance diagnostics {'PASS' if result.ok else 'FAIL'}: {rel}",
                          {"report": str(report), "smoke_ok": result.smoke_ok, "inbox_ok": result.inbox_ok,
                           "recent_error_count": result.recent_error_count})


def _tool_quality_sweep(arguments: dict[str, Any]) -> OperatorResult:
    mode = str(arguments.get("mode") or "light").lower()
    if mode not in {"light", "full"}:
        mode = "light"
    args = [_project_python(), "scripts/quality_sweep.py", "--mode", mode]
    if bool(arguments.get("fix", False)):
        args.append("--fix")
    return _run(args, timeout=900 if mode == "full" else 360)


_RUNNERS = {
    "sync_status": _tool_sync_status,
    "queue_status": _tool_queue_status,
    "change_log": _tool_change_log,
    "quick_smoke": _tool_quick_smoke,
    "repo_guard": _tool_repo_guard,
    "public_sync_dry_run": _tool_public_sync_dry_run,
    "maintenance_report": _tool_maintenance_report,
    "quality_sweep": _tool_quality_sweep,
}


def tool_specs_for_mcp() -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        for tool in TOOLS
    ]


def run_tool(name: str, arguments: dict[str, Any] | None = None) -> OperatorResult:
    runner = _RUNNERS.get(name)
    if runner is None:
        return OperatorResult(False, f"Unknown DavosBot operator tool: {name}")
    try:
        return runner(arguments or {})
    except Exception as exc:
        return OperatorResult(False, f"{name} failed: {type(exc).__name__}: {_trim(str(exc), 1000)}")


def _parse_json_arg(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--args must decode to a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List available operator tools.")
    run_parser = sub.add_parser("run", help="Run one operator tool.")
    run_parser.add_argument("tool", choices=sorted(_tool_map()))
    run_parser.add_argument("--args", default="", help="JSON object of tool arguments.")
    run_parser.add_argument("--json", action="store_true", help="Emit machine-readable result JSON.")
    args = parser.parse_args(argv)

    if args.command == "list":
        print(json.dumps(tool_specs_for_mcp(), indent=2, sort_keys=True))
        return 0

    tool_args = _parse_json_arg(args.args)
    result = run_tool(args.tool, tool_args)
    if args.json:
        print(json.dumps({"ok": result.ok, "text": result.text, "data": result.data or {}}, indent=2, sort_keys=True))
    else:
        print(result.text)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
