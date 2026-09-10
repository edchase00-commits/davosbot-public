#!/usr/bin/env python3
"""Run DavosBot quality checks as small local check agents.

This is a scan-first workflow. It writes a private report, can log a deduped
change-log row when checks fail, and leaves actual code repair to the existing
Codex cleanup runner and deploy gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DAVOSBOT_SUPPRESS_CONFIG_WARNINGS", "1")

REPORT_DIR = ROOT / "exports" / "private"
DEFAULT_STATE_PATH = ROOT / ".auto_deploy" / "quality_sweep_state.json"
DEFAULT_DB_PATH = ROOT / "davosbot.db"
MAX_OUTPUT = 3000


@dataclass(frozen=True)
class SweepResult:
    name: str
    status: str
    detail: str
    output: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "FAIL"


@dataclass(frozen=True)
class SweepAgent:
    name: str
    description: str
    modes: tuple[str, ...]
    run: Callable[[], SweepResult]


def _clean_output(text: str, limit: int = MAX_OUTPUT) -> str:
    from scripts.codex_operator import redact_text

    clean = redact_text(text or "").strip()
    if len(clean) <= limit:
        return clean
    marker = "\n...[middle omitted]\n"
    head = (limit - len(marker)) // 3
    tail = limit - len(marker) - head
    return clean[:head] + marker + clean[-tail:]


def _run(args: list[str], *, timeout: int = 300, shell: bool = False) -> SweepResult:
    name = " ".join(args if isinstance(args, list) else [str(args)])
    try:
        proc = subprocess.run(
            args,
            cwd=ROOT,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DAVOSBOT_SUPPRESS_CONFIG_WARNINGS": "1"},
        )
    except FileNotFoundError as exc:
        return SweepResult(name, "FAIL", f"missing executable: {exc.filename}")
    except subprocess.TimeoutExpired:
        return SweepResult(name, "FAIL", f"timed out after {timeout}s")
    output = _clean_output((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))
    status = "PASS" if proc.returncode == 0 else "FAIL"
    detail = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
    return SweepResult(name, status, detail, output)


def _powershell() -> str | None:
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _is_windows() -> bool:
    return platform.system().lower().startswith("windows")


def _can_ssh_macmini() -> bool:
    return shutil.which("ssh") is not None and _is_windows()


def _project_python() -> str:
    from scripts.python_env import resolve_python_bin

    return resolve_python_bin(ROOT)


def check_repo_guard() -> SweepResult:
    result = _run([_project_python(), "scripts/codex_operator.py", "run", "repo_guard"], timeout=180)
    return SweepResult("repo_guard_agent", result.status, result.detail, result.output)


def check_quick_smoke() -> SweepResult:
    result = _run([_project_python(), "scripts/codex_operator.py", "run", "quick_smoke"], timeout=240)
    return SweepResult("quick_smoke_agent", result.status, result.detail, result.output)


def check_full_validate() -> SweepResult:
    result = _run([_project_python(), "scripts/review_validation.py", "--timeout", "650"], timeout=700)
    return SweepResult("validation_agent", result.status, result.detail, result.output)


def check_public_snapshot() -> SweepResult:
    shell = _powershell()
    if not shell:
        return SweepResult(
            "public_snapshot_agent",
            "SKIP",
            "PowerShell unavailable; public snapshot is still validated in GitHub Actions",
        )
    result = _run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/publish_public_snapshot.ps1", "-SkipPush"],
        timeout=700,
    )
    return SweepResult("public_snapshot_agent", result.status, result.detail, result.output)


def check_runtime_smoke() -> SweepResult:
    if _is_windows():
        if not _can_ssh_macmini():
            return SweepResult("runtime_smoke_agent", "SKIP", "ssh unavailable")
        result = _run(
            [
                "ssh",
                "macmini",
                "cd /Users/<you>/projects/davosbot && venv/bin/python scripts/runtime_smoke.py",
            ],
            timeout=420,
        )
    else:
        result = _run([_project_python(), "scripts/runtime_smoke.py"], timeout=420)
    return SweepResult("runtime_smoke_agent", result.status, result.detail, result.output)


def check_maintenance() -> SweepResult:
    result = _run([_project_python(), "scripts/codex_operator.py", "run", "maintenance_report"], timeout=260)
    return SweepResult("maintenance_agent", result.status, result.detail, result.output)


def _noisy_cron_issues(crontab_text: str) -> list[str]:
    issues: list[str] = []
    for raw_line in (crontab_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "git add MEMORY.md" in line and "--allow-empty" in line and "git push" in line:
            issues.append("legacy MEMORY.md backup cron can create empty daily commits")
    return issues


def _installed_crontab_text() -> str:
    if not shutil.which("crontab"):
        return ""
    result = _run(["crontab", "-l"], timeout=30)
    if result.status != "PASS":
        return ""
    return result.output


def check_alert_audit() -> SweepResult:
    text = (ROOT / "scripts" / "auto_deploy.py").read_text(encoding="utf-8", errors="ignore")
    issues: list[str] = []
    if 'AUTO_DEPLOY_WAIT_ALERT_SECONDS", "0"' not in text:
        issues.append("deploy wait alert default is not 0")
    if "if _should_alert_waiting(remote_sha, ci.detail)" not in text:
        issues.append("auto_deploy_waiting is not gated by wait threshold")
    if re.search(r"alert\(\"auto_deploy_waiting\"", text) and "if _should_alert_waiting" not in text:
        issues.append("auto_deploy_waiting appears ungated")
    issues.extend(_noisy_cron_issues(_installed_crontab_text()))
    if issues:
        return SweepResult("alert_audit_agent", "FAIL", "; ".join(issues))
    return SweepResult(
        "alert_audit_agent",
        "PASS",
        "normal CI waiting is silent; owner webhook remains reserved for failures, blocks, successes, budgets, and runtime health",
    )


def check_queue_status() -> SweepResult:
    result = _run([_project_python(), "scripts/codex_operator.py", "run", "queue_status"], timeout=120)
    return SweepResult("queue_agent", result.status, result.detail, result.output)


def build_agents() -> list[SweepAgent]:
    return [
        SweepAgent("repo_guard_agent", "tracked-path and worktree cleanliness", ("light", "full"), check_repo_guard),
        SweepAgent("quick_smoke_agent", "compile plus deterministic smoke", ("light", "full"), check_quick_smoke),
        SweepAgent("validation_agent", "full local validation suite", ("full",), check_full_validate),
        SweepAgent("public_snapshot_agent", "sanitized public snapshot validation", ("full",), check_public_snapshot),
        SweepAgent("runtime_smoke_agent", "Mac Mini runtime smoke", ("full",), check_runtime_smoke),
        SweepAgent("maintenance_agent", "private maintenance diagnostics", ("light", "full"), check_maintenance),
        SweepAgent("alert_audit_agent", "owner alert and notification noise audit", ("light", "full"), check_alert_audit),
        SweepAgent("queue_agent", "queue/backlog status", ("light", "full"), check_queue_status),
    ]


def run_sweep(mode: str) -> list[SweepResult]:
    results: list[SweepResult] = []
    for agent in build_agents():
        if mode in agent.modes:
            results.append(agent.run())
    return results


def fingerprint_failures(results: list[SweepResult]) -> str:
    failures = [
        {"name": result.name, "detail": result.detail, "status": result.status}
        for result in results
        if result.failed
    ]
    raw = json.dumps(failures, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_json_load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def should_log_failures(results: list[SweepResult], state_path: Path, repeat_hours: float = 24.0) -> bool:
    failures = [result for result in results if result.failed]
    if not failures:
        return False
    state = _safe_json_load(state_path)
    now = datetime.now(timezone.utc).timestamp()
    fingerprint = fingerprint_failures(results)
    last_ts = float(state.get("last_failure_logged_ts") or 0)
    if state.get("last_failure_fingerprint") == fingerprint and now - last_ts < repeat_hours * 3600:
        return False
    return True


def _write_state(state_path: Path, payload: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_report(results: list[SweepResult], *, mode: str) -> Path:
    if mode not in {"light", "full"}:
        raise ValueError("Unknown quality mode")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stable = REPORT_DIR / "quality_sweep.md"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped = REPORT_DIR / f"quality_sweep_{stamp}.md"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    failures = [result for result in results if result.failed]
    passed = bool(results) and not failures
    lines = [
        "# DavosBot Quality Sweep",
        "",
        f"Generated: {now}",
        f"Mode: {mode}",
        "Policy: scan first; no direct production mutation, no deploy, no PM2 restart, no secret output.",
        "",
        f"Overall: {'PASS' if passed else 'FAIL'}",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"## {result.status} {result.name}",
                _clean_output(result.detail),
            ]
        )
        if result.output:
            lines.extend(["```", result.output, "```"])
        lines.append("")
    content = "\n".join(lines)
    stable.write_text(content, encoding="utf-8")
    timestamped.write_text(content, encoding="utf-8")
    # Hourly light checks must not overwrite the evidence for the nightly full
    # run. These files record completion, including failure, independently.
    (REPORT_DIR / f"quality_sweep_{mode}.md").write_text(content, encoding="utf-8")
    state = {"completed_at": now, "mode": mode, "ok": passed,
             "checks": [{"name": result.name, "status": result.status} for result in results]}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=REPORT_DIR, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        json.dump(state, temporary, sort_keys=True)
    try:
        temporary_path.replace(REPORT_DIR / f"quality_sweep_{mode}_state.json")
    finally:
        temporary_path.unlink(missing_ok=True)
    return stable


def record_failure_change_log(results: list[SweepResult], report: Path, state_path: Path, db_path: Path) -> int | None:
    failures = [result for result in results if result.failed]
    if not failures:
        _write_state(
            state_path,
            {
                "last_pass_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_failure_fingerprint": "",
            },
        )
        return None
    if not should_log_failures(results, state_path):
        return None
    if not db_path.exists():
        return None

    summary = ", ".join(result.name for result in failures)
    detail = "\n".join(f"{result.name}: {result.detail}" for result in failures)
    request = f"[QUALITY-SWEEP YELLOW] Automated quality sweep failed: {summary}"
    reason = "\n".join(
        [
            "type=quality_sweep",
            "risk=YELLOW",
            "status=auto_logged_review_item",
            f"report={report}",
            "expected_fix=Codex should inspect the report, patch only failing/noisy checks, validate, push, wait for CI/autodeploy, and runtime-smoke before clearing this row.",
            detail,
        ]
    )
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("INSERT INTO change_log (request, reason) VALUES (?, ?)", (request, reason))
        row_id = int(cur.lastrowid)
        conn.commit()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    _write_state(
        state_path,
        {
            "last_failure_logged_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_failure_logged_ts": datetime.now(timezone.utc).timestamp(),
            "last_failure_fingerprint": fingerprint_failures(results),
            "last_change_log_id": row_id,
        },
    )
    return row_id


def _default_db_path() -> Path:
    try:
        from davosbot.config import BOT_DB_PATH

        return Path(BOT_DB_PATH)
    except Exception:
        return DEFAULT_DB_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("light", "full"), default="light")
    parser.add_argument("--fix", action="store_true", help="Log a deduped change-log row for failures.")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args(argv)

    results = run_sweep(args.mode)
    report = write_report(results, mode=args.mode)
    failures = [result for result in results if result.failed]
    row_id = None
    if args.fix:
        row_id = record_failure_change_log(results, report, args.state_path, args.db or _default_db_path())

    print(f"Quality sweep report: {report}")
    print(f"Mode: {args.mode}")
    print(f"Overall: {'FAIL' if failures else 'PASS'}")
    if row_id:
        print(f"Logged change-log row: #{row_id}")
    elif failures and args.fix:
        print("Failure row not logged: duplicate, missing DB, or DB unavailable.")
    for result in results:
        print(f"{result.status} {result.name}: {result.detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
