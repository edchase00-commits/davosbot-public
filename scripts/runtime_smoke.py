#!/usr/bin/env python3
"""Mac Mini runtime smoke checks for DavosBot.

This script is intentionally metadata-first. It does not print secrets or raw
message bodies. Use --send-image only when a real owner DM image smoke is
wanted.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PM2_FALLBACK_PATHS = (
    "/opt/homebrew/bin/pm2",
    "/usr/local/bin/pm2",
    "/usr/bin/pm2",
)
MESSAGES_APPLESCRIPT_ATTEMPTS = 3
MESSAGES_APPLESCRIPT_RETRY_DELAY_SECONDS = 2
MAX_HEARTBEAT_AGE_SECONDS = 300
MAX_INBOX_AGE_SECONDS = 300

# Tiny red PNG. Runtime smoke should never re-send a user's generated image.
_RUNTIME_SMOKE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def _run(cmd: list[str], timeout: int = 30, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    path_prefix = "/opt/homebrew/bin:/usr/local/bin"
    env["PATH"] = path_prefix + (":" + env["PATH"] if env.get("PATH") else "")
    return subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        input=input_text,
    )


def _resolve_executable(name: str, fallbacks: tuple[str, ...] = ()) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if Path(candidate).exists():
            return candidate
    return name


def _bool_text(value: bool) -> str:
    return "yes" if value else "no"


def _db_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _looks_like_messages_db_access_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "unable to open database file",
            "operation not permitted",
            "access denied",
            "not authorized",
        )
    )


def _is_applescript_timeout(detail: str) -> bool:
    text = (detail or "").lower()
    return "appleevent timed out" in text or "osascript process timed out" in text


def _recent_pm2_messages_db_errors(lines: int = 120) -> list[str] | None:
    result = _run(
        [_resolve_executable("pm2", PM2_FALLBACK_PATHS), "logs", "davosbot", "--lines", str(lines), "--nostream"],
        timeout=20,
    )
    if result.returncode != 0:
        return None
    matches: list[str] = []
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    for raw_line in combined.splitlines():
        line = raw_line.lower()
        if "poll_new_messages error:" in line or "is_owner_in_chat error:" in line:
            matches.append(" ".join(raw_line.split()))
    return matches[-5:]


def _recent_pm2_messages_applescript_errors(lines: int = 120) -> list[str] | None:
    result = _run(
        [_resolve_executable("pm2", PM2_FALLBACK_PATHS), "logs", "davosbot", "--lines", str(lines), "--nostream"],
        timeout=20,
    )
    if result.returncode != 0:
        return None
    matches: list[str] = []
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    for raw_line in combined.splitlines():
        line = raw_line.lower()
        if (
            "applescript timed out for " in line
            or "applescript error for " in line
            or "hard relaunching messages after applescript failure" in line
        ):
            matches.append(" ".join(raw_line.split()))
    return matches[-5:]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row else 0)


def check_git() -> CheckResult:
    status = _run(["git", "status", "--short"])
    head = _run(["git", "log", "-1", "--oneline"])
    ok = status.returncode == 0 and head.returncode == 0 and not status.stdout.strip()
    detail = (head.stdout.strip() or "unknown") + ("; clean" if ok else "; dirty or unreadable")
    return CheckResult("git", ok, detail, {"status_lines": len(status.stdout.splitlines())})


def check_pm2() -> CheckResult:
    result = _run([_resolve_executable("pm2", PM2_FALLBACK_PATHS), "jlist"], timeout=20)
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "pm2 jlist failed").split())[:500]
        return CheckResult("pm2", False, f"pm2 jlist failed: {detail}")
    try:
        processes = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return CheckResult("pm2", False, "pm2 jlist returned invalid JSON")
    wanted = {
        "davosbot",
        "davosbot-autodeploy",
        "davosbot-comfyui",
        "davosbot-local-image-worker",
    }
    statuses = {}
    for proc in processes:
        name = proc.get("name")
        if name in wanted:
            statuses[name] = proc.get("pm2_env", {}).get("status", "unknown")
    missing = sorted(wanted - set(statuses))
    offline = {name: status for name, status in statuses.items() if status != "online"}
    ok = not missing and not offline
    detail = "all expected PM2 processes online" if ok else f"missing={missing} offline={offline}"
    return CheckResult("pm2", ok, detail, statuses)


def check_messages_db() -> CheckResult:
    from davosbot.config import DB_PATH

    db_path = Path(DB_PATH).expanduser()
    try:
        with closing(_db_conn(DB_PATH)) as conn:
            row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
        max_rowid = int(row[0] if row else 0)
        return CheckResult("messages_db", max_rowid > 0, f"readable; max_rowid={max_rowid}", {"max_rowid": max_rowid})
    except sqlite3.OperationalError as exc:
        if db_path.exists() and _looks_like_messages_db_access_error(exc):
            recent_errors = _recent_pm2_messages_db_errors()
            if recent_errors == []:
                return CheckResult(
                    "messages_db",
                    True,
                    "direct probe blocked by macOS privacy; PM2 has no recent chat.db poll errors",
                    {"verified_via": "pm2_logs"},
                )
            if recent_errors:
                return CheckResult(
                    "messages_db",
                    False,
                    f"PM2 shows recent chat.db poll errors ({len(recent_errors)})",
                    {"recent_errors": recent_errors},
                )
            return CheckResult("messages_db", False, "unreadable: OperationalError; PM2 log fallback unavailable")
    except Exception as exc:
        return CheckResult("messages_db", False, f"unreadable: {type(exc).__name__}")


def check_messages_applescript() -> CheckResult:
    if sys.platform != "darwin":
        return CheckResult("messages_applescript", True, "skipped off macOS")
    script = """
with timeout of 8 seconds
    tell application "Messages"
        count of services
    end tell
end timeout
""".strip()
    last_result: CheckResult | None = None
    for attempt in range(1, MESSAGES_APPLESCRIPT_ATTEMPTS + 1):
        try:
            result = _run(["osascript"], timeout=12, input_text=script)
        except subprocess.TimeoutExpired:
            last_result = CheckResult("messages_applescript", False, "osascript process timed out")
        else:
            detail = " ".join((result.stderr or result.stdout or "").split())[:500]
            count_text = (result.stdout or "").strip()
            ok = result.returncode == 0 and count_text.isdigit() and int(count_text) > 0
            if ok:
                suffix = f" after {attempt} attempt" + ("" if attempt == 1 else "s")
                return CheckResult("messages_applescript", True, f"Messages AppleScript bridge ok; services={count_text}{suffix}")
            last_result = CheckResult(
                "messages_applescript",
                False,
                f"Messages AppleScript bridge failed: {detail or 'no output'}",
                {"returncode": result.returncode},
            )

        if attempt < MESSAGES_APPLESCRIPT_ATTEMPTS and last_result and _is_applescript_timeout(last_result.detail):
            time.sleep(MESSAGES_APPLESCRIPT_RETRY_DELAY_SECONDS)
            continue
        break

    assert last_result is not None
    if _is_applescript_timeout(last_result.detail):
        recent_errors = _recent_pm2_messages_applescript_errors()
        if recent_errors == []:
            return CheckResult(
                "messages_applescript",
                True,
                "direct Messages AppleScript probe timed out from shell; PM2 has no recent send errors",
                {"verified_via": "pm2_logs"},
            )
        if recent_errors:
            return CheckResult(
                "messages_applescript",
                False,
                f"PM2 shows recent Messages AppleScript send errors ({len(recent_errors)})",
                {"recent_errors": recent_errors},
            )
    return last_result


def check_bot_db() -> CheckResult:
    from davosbot.config import BOT_DB_PATH

    try:
        with closing(_db_conn(BOT_DB_PATH)) as conn:
            tables = {
                table: _table_exists(conn, table)
                for table in ("reminders", "scheduled_tasks", "cron_jobs", "change_log")
            }
            if not all(tables.values()):
                return CheckResult("bot_db", False, f"missing tables: {[k for k, v in tables.items() if not v]}")
            unsent = _count(conn, "SELECT COUNT(*) FROM reminders WHERE sent = 0")
            overdue = _count(
                conn,
                "SELECT COUNT(*) FROM reminders WHERE sent = 0 AND datetime(due_ts) <= datetime('now')",
            )
            attempted = _count(conn, "SELECT COUNT(*) FROM reminders WHERE send_attempts > 0")
            scheduled_failed = _count(conn, "SELECT COUNT(*) FROM scheduled_tasks WHERE status = 'failed'")
            scheduled_pending = _count(conn, "SELECT COUNT(*) FROM scheduled_tasks WHERE status = 'pending'")
            active_crons = conn.execute(
                "SELECT id, action_payload FROM cron_jobs WHERE enabled = 1"
            ).fetchall()
            malformed_crons = []
            for row in active_crons:
                try:
                    json.loads(row["action_payload"] or "{}")
                except json.JSONDecodeError:
                    malformed_crons.append(row["id"])
            change_log_rows = _count(conn, "SELECT COUNT(*) FROM change_log")
        ok = overdue == 0 and scheduled_failed == 0 and not malformed_crons
        detail = (
            f"reminders unsent={unsent} overdue={overdue} attempts={attempted}; "
            f"scheduled pending={scheduled_pending} failed={scheduled_failed}; "
            f"active_crons={len(active_crons)}; change_log={change_log_rows}"
        )
        return CheckResult(
            "bot_db",
            ok,
            detail,
            {
                "unsent_reminders": unsent,
                "overdue_reminders": overdue,
                "reminder_attempt_rows": attempted,
                "scheduled_pending": scheduled_pending,
                "scheduled_failed": scheduled_failed,
                "active_crons": len(active_crons),
                "malformed_crons": malformed_crons,
                "change_log_rows": change_log_rows,
            },
        )
    except Exception as exc:
        return CheckResult("bot_db", False, f"DB check failed: {type(exc).__name__}")


def check_session_heartbeat() -> CheckResult:
    """Verify the newest bot session is ticking, not merely online in PM2."""
    from davosbot.config import BOT_DB_PATH

    try:
        with closing(_db_conn(BOT_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT id, CAST((julianday('now') - julianday(last_heartbeat)) * 86400 "
                "AS INTEGER) AS age_seconds FROM bot_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None or row["age_seconds"] is None:
            return CheckResult("heartbeat", False, "latest session has no valid heartbeat")
        age = int(row["age_seconds"])
        ok = 0 <= age <= MAX_HEARTBEAT_AGE_SECONDS
        detail = f"latest session heartbeat age={age}s; limit={MAX_HEARTBEAT_AGE_SECONDS}s"
        return CheckResult("heartbeat", ok, detail, {"age_seconds": age})
    except (OSError, sqlite3.Error) as exc:
        return CheckResult("heartbeat", False, f"heartbeat unavailable: {type(exc).__name__}")


def check_inbox(db_path=None, *, now=None) -> CheckResult:
    """Read intake health without initializing state or treating old holds as outages."""
    from davosbot.config import BOT_DB_PATH
    from davosbot.inbox import inbox_health

    try:
        health = inbox_health(BOT_DB_PATH if db_path is None else db_path, now=now)
        if not isinstance(health, dict) or health.get("available") is not True:
            return CheckResult("inbox", False, "intake schema or database unavailable; no state was created")
        states = ("pending", "processing", "handler_returned", "uncertain", "ignored", "held")
        raw_counts = health.get("counts")
        if not isinstance(raw_counts, dict):
            raise ValueError("invalid_counts")
        counts = {state: raw_counts.get(state, 0) for state in states}
        if any(type(count) is not int or count < 0 for count in counts.values()):
            raise ValueError("invalid_counts")
        ages = {key: health.get(key) for key in (
            "last_poll_age_seconds", "oldest_pending_age_seconds", "oldest_processing_age_seconds",
        )}
        for age in ages.values():
            if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age < 0):
                raise ValueError("invalid_age")
        problems = []
        if health.get("initialized") is not True:
            problems.append("intake is not initialized")
        if health.get("source_error"):
            problems.append("active source/session hold")
        for key, label, required in (
            ("last_poll_age_seconds", "poll", True),
            ("oldest_pending_age_seconds", "pending work", counts["pending"] > 0),
            ("oldest_processing_age_seconds", "processing work", counts["processing"] > 0),
        ):
            age = ages[key]
            if required and age is None:
                problems.append(f"{label} age is unavailable")
            elif age is not None and age > MAX_INBOX_AGE_SECONDS:
                problems.append(f"{label} is stale/delayed ({int(age)}s > {MAX_INBOX_AGE_SECONDS}s)")
        summary = ", ".join(f"{state}={counts[state]}" for state in ("pending", "processing", "held", "uncertain"))
        detail = ("; ".join(problems) if problems else "intake is initialized and current") + "; " + summary
        return CheckResult("inbox", not problems, detail, {
            "initialized": health.get("initialized") is True, "counts": counts, **ages,
            "source_error_present": bool(health.get("source_error")),
            "anchor_missing": bool(health.get("anchor_missing")), "age_limit_seconds": MAX_INBOX_AGE_SECONDS,
        })
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return CheckResult("inbox", False, "intake health is unreadable or invalid")


def check_image_routes() -> CheckResult:
    from davosbot import main, openai_images

    generation = openai_images.choose_generation_provider()
    scan = openai_images.choose_scan_provider()
    cases = {
        "generate": openai_images.parse_openai_image_intent("image gen a fish", has_image=False),
        "scan": openai_images.parse_openai_image_intent("what's in this screenshot?", has_image=True),
        "reference": openai_images.parse_openai_image_intent("make an image based on this", has_image=True),
    }
    casual_status = main._handle_image_capability_status("+15550000001", "can you make an image like this?")
    explicit_status = main._handle_image_capability_status("+15550000001", "image status")
    active_jobs = main._active_image_jobs_for_context("+15550000001", "+15550000001", False)
    ok = (
        cases["generate"] is not None
        and cases["generate"].kind == "generate"
        and cases["scan"] is not None
        and cases["scan"].kind == "scan"
        and cases["reference"] is not None
        and cases["reference"].kind == "generate"
        and casual_status is None
        and isinstance(explicit_status, str)
    )
    detail = (
        f"generation={generation}; scan={scan}; "
        f"local_endpoint={_bool_text(bool(openai_images.LOCAL_IMAGE_ENDPOINT))}; "
        f"active_jobs={len(active_jobs)}"
    )
    return CheckResult(
        "image_routes",
        ok,
        detail,
        {
            "generation_provider": generation,
            "scan_provider": scan,
            "local_endpoint": bool(openai_images.LOCAL_IMAGE_ENDPOINT),
            "active_jobs": len(active_jobs),
        },
    )


def _latest_generated_image() -> Path | None:
    from davosbot.config import GENERATED_DIR, IMAGE_OUTPUT_DIR, PROJECT_ROOT

    dirs = [
        Path(IMAGE_OUTPUT_DIR),
        Path(GENERATED_DIR) / "images",
        Path(GENERATED_DIR) / "openai_images",
    ]
    candidates: list[Path] = []
    for directory in dirs:
        path = directory if directory.is_absolute() else Path(PROJECT_ROOT) / directory
        if path.exists():
            candidates.extend(
                child
                for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".heic"}
            )
    if not candidates:
        return None
    return max(candidates, key=lambda child: child.stat().st_mtime)


def _runtime_smoke_image() -> Path:
    from davosbot.config import GENERATED_DIR, PROJECT_ROOT

    directory = Path(GENERATED_DIR) / "runtime_smoke"
    if not directory.is_absolute():
        directory = Path(PROJECT_ROOT) / directory
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "davosbot_runtime_smoke.png"
    if not image.exists():
        image.write_bytes(base64.b64decode(_RUNTIME_SMOKE_PNG_B64))
    return image


def smoke_send_image() -> CheckResult:
    from davosbot.config import DB_PATH, OWNER_ID
    from davosbot.imessage import send_file

    if not OWNER_ID:
        return CheckResult("image_send", False, "OWNER_ID is not configured")
    image = _runtime_smoke_image()

    with closing(_db_conn(DB_PATH)) as conn:
        before = _count(conn, "SELECT COALESCE(MAX(ROWID), 0) FROM message")
    ok = send_file(OWNER_ID, str(image), is_group=False)
    time.sleep(2)
    with closing(_db_conn(DB_PATH)) as conn:
        row = conn.execute(
            """
            SELECT
                m.ROWID,
                m.is_sent,
                m.is_delivered,
                m.error,
                a.transfer_state
            FROM message m
            JOIN message_attachment_join maj ON maj.message_id = m.ROWID
            JOIN attachment a ON a.ROWID = maj.attachment_id
            WHERE m.ROWID > ?
              AND m.is_from_me = 1
            ORDER BY m.ROWID DESC
            LIMIT 1
            """,
            (before,),
        ).fetchone()
    verified = bool(row and int(row["is_sent"] or 0) == 1 and int(row["error"] or 0) == 0)
    detail = "sent and verified" if ok and verified else "send_file failed or DB did not verify"
    data = dict(row) if row else {}
    data["image_name"] = image.name
    return CheckResult("image_send", ok and verified, detail, data)


def smoke_async_image_job(send_image: bool) -> CheckResult:
    from davosbot import main
    from davosbot.config import DB_PATH, OWNER_ID
    from davosbot.openai_images import OpenAIImageResult

    if not OWNER_ID:
        return CheckResult("async_image_job", False, "OWNER_ID is not configured")
    image = _runtime_smoke_image()

    original_generate = main.generate_image
    original_choose = main.choose_generation_provider
    original_send_file = main.send_file

    sent_calls: list[tuple[str, str, bool]] = []

    def fake_generate(prompt: str) -> OpenAIImageResult:
        time.sleep(0.5)
        return OpenAIImageResult(True, "smoke ok", path=str(image), api_called=False, provider="local")

    def fake_send_file(recipient: str, path: str, is_group: bool = False) -> bool:
        sent_calls.append((recipient, Path(path).name, is_group))
        return True

    try:
        main.generate_image = fake_generate
        main.choose_generation_provider = lambda: "local"
        if not send_image:
            main.send_file = fake_send_file

        before = 0
        if send_image:
            with closing(_db_conn(DB_PATH)) as conn:
                before = _count(conn, "SELECT COALESCE(MAX(ROWID), 0) FROM message")

        start = time.monotonic()
        reply = main._handle_openai_image_intent(
            OWNER_ID,
            "image gen runtime smoke",
            None,
            OWNER_ID,
            is_group=False,
        )
        elapsed = time.monotonic() - start
        if elapsed > 1.0:
            return CheckResult("async_image_job", False, f"handler blocked for {elapsed:.2f}s")
        if "On it, generating image" not in str(reply):
            return CheckResult("async_image_job", False, f"unexpected reply: {reply!r}")

        deadline = time.time() + 20
        while time.time() < deadline and main._active_image_jobs_for_context(OWNER_ID, OWNER_ID, False):
            time.sleep(0.25)
        active = main._active_image_jobs_for_context(OWNER_ID, OWNER_ID, False)
        if active:
            return CheckResult("async_image_job", False, "background image job did not finish")

        if send_image:
            with closing(_db_conn(DB_PATH)) as conn:
                row = conn.execute(
                    """
                    SELECT m.ROWID, m.is_sent, m.is_delivered, m.error, a.transfer_state
                    FROM message m
                    JOIN message_attachment_join maj ON maj.message_id = m.ROWID
                    JOIN attachment a ON a.ROWID = maj.attachment_id
                    WHERE m.ROWID > ? AND m.is_from_me = 1
                    ORDER BY m.ROWID DESC
                    LIMIT 1
                    """,
                    (before,),
                ).fetchone()
            verified = bool(row and int(row["is_sent"] or 0) == 1 and int(row["error"] or 0) == 0)
            return CheckResult(
                "async_image_job",
                verified,
                "background job sent real image" if verified else "background job did not verify image send",
                dict(row) if row else {},
            )

        return CheckResult(
            "async_image_job",
            bool(sent_calls),
            f"returned in {elapsed:.2f}s; fake send calls={len(sent_calls)}",
            {"elapsed_seconds": round(elapsed, 3), "send_calls": len(sent_calls)},
        )
    finally:
        main.generate_image = original_generate
        main.choose_generation_provider = original_choose
        main.send_file = original_send_file


def run_checks(args: argparse.Namespace) -> list[CheckResult]:
    return [
        check_git(),
        check_pm2(),
        check_messages_db(),
        check_messages_applescript(),
        check_bot_db(),
        check_session_heartbeat(),
        check_inbox(),
        check_image_routes(),
        smoke_async_image_job(send_image=args.send_image),
    ]


def format_results(results: list[CheckResult]) -> str:
    lines = ["Runtime smoke results:"]
    for result in results:
        marker = "PASS" if result.ok else "FAIL"
        lines.append(f"- {marker} {result.name}: {result.detail}")
    failures = [result.name for result in results if not result.ok]
    lines.append("")
    lines.append("Overall: " + ("PASS" if not failures else f"FAIL ({', '.join(failures)})"))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send-image",
        action="store_true",
        help="Send a real owner DM image attachment while verifying the iMessage DB row.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    results = run_checks(args)
    if args.json:
        print(json.dumps([result.__dict__ for result in results], indent=2, default=str))
    else:
        print(format_results(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
