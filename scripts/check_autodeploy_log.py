#!/usr/bin/env python3
"""Check auto-deploy logs for fresh errors after the latest deploy marker."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS = ROOT / ".auto_deploy" / "status.json"
DEFAULT_LOGS = (
    ROOT / "logs" / "autodeploy-err.log",
    ROOT / "logs" / "autodeploy-out.log",
)
ERROR_PATTERNS = (
    re.compile(r"\bTraceback \(most recent call last\):"),
    re.compile(r"\bKeyboardInterrupt\b"),
    re.compile(r"\bERROR\b"),
    re.compile(r"\bException\b"),
)


@dataclass(frozen=True)
class LogCheck:
    ok: bool
    detail: str
    source: str = ""
    marker_line: int = 0
    error_lines: tuple[str, ...] = ()


def _short_sha(value: str) -> str:
    return (value or "").strip()[:7]


def _run(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=15)


def current_sha() -> str:
    result = _run(["git", "rev-parse", "--short", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


def sha_from_status(path: Path = DEFAULT_STATUS) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return _short_sha(payload.get("remote_sha") or payload.get("local_sha") or "")


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def check_logs_for_sha(sha: str, log_paths: tuple[Path, ...] = DEFAULT_LOGS) -> LogCheck:
    short = _short_sha(sha)
    if not short:
        return LogCheck(False, "no deploy SHA provided")

    best_marker: tuple[Path, int, list[str]] | None = None
    for path in log_paths:
        lines = _read_lines(path)
        for index, line in enumerate(lines):
            if f"deployed {short}" in line:
                best_marker = (path, index, lines)

    if best_marker is None:
        logs = ", ".join(str(path) for path in log_paths)
        return LogCheck(False, f"deploy marker for {short} not found in logs: {logs}")

    path, marker_index, lines = best_marker
    after = lines[marker_index + 1 :]
    error_lines = tuple(
        line.strip()
        for line in after
        if any(pattern.search(line) for pattern in ERROR_PATTERNS)
    )
    if error_lines:
        detail = f"{len(error_lines)} error marker(s) after deployed {short}"
        return LogCheck(False, detail, str(path), marker_index + 1, error_lines[:10])
    return LogCheck(True, f"no error markers after deployed {short}", str(path), marker_index + 1)


def format_result(result: LogCheck) -> str:
    prefix = "PASS" if result.ok else "FAIL"
    lines = [f"{prefix} autodeploy_log: {result.detail}"]
    if result.source:
        lines.append(f"- marker: {result.source}:{result.marker_line}")
    for line in result.error_lines:
        lines.append(f"- {line[:500]}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default="", help="Deploy SHA to check. Defaults to status file, then HEAD.")
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS, help="Auto-deploy status JSON path.")
    parser.add_argument("--log", action="append", type=Path, default=[], help="Log file to scan. Can repeat.")
    args = parser.parse_args()

    sha = args.sha or sha_from_status(args.status) or current_sha()
    log_paths = tuple(args.log) if args.log else DEFAULT_LOGS
    result = check_logs_for_sha(sha, log_paths)
    print(format_result(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
