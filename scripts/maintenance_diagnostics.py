"""Review-only DavosBot maintenance diagnostics.

Summarizes recent local errors, phone change-log counts, and smoke status.
Safe for cron: it writes a private report and never mutates production behavior.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPORT_DIR = ROOT / "exports" / "private"
STATE_PATH = ROOT / ".auto_deploy" / "maintenance_diagnostics_state.json"


@dataclass(frozen=True)
class MaintenanceResult:
    report: Path
    smoke_ok: bool
    recent_error_count: int
    inbox_ok: bool = True

    @property
    def ok(self) -> bool:
        return self.smoke_ok and self.inbox_ok and self.recent_error_count == 0


def _load_config():
    from davosbot import config

    return config


def _safe_json_load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_change_log_counts(db_path: str) -> dict[str, int]:
    from davosbot.change_log_triage import _bucket_change_log_rows

    if not Path(db_path).exists():
        return {"green": 0, "yellow": 0, "red": 0, "total": 0}
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        rows = conn.execute("SELECT id, request, reason, created_ts FROM change_log ORDER BY id DESC").fetchall()
    buckets = _bucket_change_log_rows(rows)
    return {
        "green": len(buckets["green"]),
        "yellow": len(buckets["yellow"]),
        "red": len(buckets["red"]),
        "total": len(rows),
    }


def _recent_bot_errors(db_path: str, limit: int = 8) -> list[str]:
    if not Path(db_path).exists():
        return ["bot DB not found in this checkout"]
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            rows = conn.execute(
                """
                SELECT CASE WHEN datetime(timestamp) IS NULL THEN 'unknown time'
                            ELSE substr(timestamp, 1, 64) END,
                       substr(event_type, 1, 120)
                FROM bot_log
                WHERE (lower(event_type) LIKE '%error%' OR lower(payload) LIKE '%traceback%')
                  AND (datetime(timestamp) IS NULL OR datetime(timestamp) >= datetime('now', '-24 hours'))
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error as exc:
        return [f"bot_log unavailable: {type(exc).__name__}"]
    from scripts.codex_operator import redact_text
    return [redact_text(f"{ts or '?'} {event or '?'}") for ts, event in rows]


def _quick_smoke() -> tuple[bool, str]:
    from scripts.python_env import resolve_python_bin

    proc = subprocess.run(
        [resolve_python_bin(ROOT), "scripts/master_smoke.py", "--quick"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    body = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, body[-4000:] if body else "(no output)"


def collect_diagnostics(*, update_state: bool = False) -> MaintenanceResult:
    config = _load_config()
    previous = _safe_json_load(STATE_PATH)
    count_error = []
    try:
        counts = _read_change_log_counts(config.BOT_DB_PATH)
    except (sqlite3.Error, OSError) as exc:
        counts = dict.fromkeys(("green", "yellow", "red", "total"), 0)
        count_error = [f"change_log unavailable: {type(exc).__name__}"]
    errors = _recent_bot_errors(config.BOT_DB_PATH)
    errors.extend(count_error)
    try:
        smoke_ok, smoke = _quick_smoke()
    except (OSError, subprocess.TimeoutExpired) as exc:
        smoke_ok, smoke = False, f"Quick smoke unavailable: {type(exc).__name__}"
    from scripts.codex_operator import redact_text
    from scripts.runtime_smoke import check_inbox
    inbox = check_inbox(config.BOT_DB_PATH)
    overall_ok = smoke_ok and not errors and inbox.ok
    smoke = redact_text(smoke)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / "maintenance_diagnostics.md"
    lines = [
        "# DavosBot Maintenance Diagnostics",
        "",
        f"Generated: {now}",
        f"Overall: {'PASS' if overall_ok else 'FAIL'}",
        "Mode: review-only; no production mutation, deploy, PM2 restart, or log clear.",
        "",
        "## Change Log",
        f"Total {counts['total']} | GREEN {counts['green']} | YELLOW {counts['yellow']} | RED {counts['red']}",
        "",
        "## Errors / Signals (last 24 hours)",
    ]
    if errors:
        lines.extend(f"- {item}" for item in errors)
    else:
        lines.append("- No recent structured errors found locally.")
    lines.extend(
        [
            "",
            "## Intake",
            f"{'PASS' if inbox.ok else 'FAIL'}: {inbox.detail}",
            "Historical held/uncertain counts are informational; no request is replayed or restarted.",
            "",
            "## Smoke",
            "PASS" if smoke_ok else "FAIL",
            "```",
            smoke,
            "```",
            "",
            "## Suggested Fix",
            "If smoke fails or new errors are present, log a YELLOW review row or run `ship safe cleanup` for a Codex handoff. Do not auto-change production from this report.",
            "",
        ]
    )
    report.write_text("\n".join(lines), encoding="utf-8")

    if update_state:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps({"last_run_utc": now, "previous": previous.get("last_run_utc", ""),
                        "ok": overall_ok, "smoke_ok": smoke_ok, "inbox_ok": inbox.ok,
                        "recent_error_count": len(errors)}, indent=2),
            encoding="utf-8",
        )
    return MaintenanceResult(report, smoke_ok, len(errors), inbox.ok)


def run_diagnostics(*, update_state: bool = False) -> Path:
    """Compatibility entrypoint for callers that only need the report path."""
    return collect_diagnostics(update_state=update_state).report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-state", action="store_true")
    args = parser.parse_args(argv)
    result = collect_diagnostics(update_state=args.update_state)
    print(f"Maintenance diagnostics {'PASS' if result.ok else 'FAIL'}: {result.report}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
