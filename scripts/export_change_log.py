#!/usr/bin/env python3
"""Export DavosBot phone change_log rows for SSH/operator handoff.

This script never writes to tracked docs by default. It writes a full triage
board to exports/private/change_log_board.md plus a timestamped snapshot.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from davosbot.config import BOT_DB_PATH  # noqa: E402
from davosbot.permissions import redact_secret  # noqa: E402


EMPTY_CHANGE_LOG_MESSAGE = (
    "Change log is empty.\n"
    "Use `log [thing]` to add a backlog row.\n"
    "For screenshot/self-repair bugs, send the screenshot or exact failing text, then say "
    "`analyze this and log` or `fix yourself: [what went wrong]`.\n"
    "`ship safe cleanup` builds the Codex handoff after rows exist."
)


def redact_export_text(text: str) -> str:
    redacted = redact_secret(text or "")
    return re.sub(
        r"\b(api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*['\"]?[^,\s)]+",
        r"\1=[redacted]",
        redacted,
        flags=re.IGNORECASE,
    )


def classify_change_request(text: str) -> str:
    lower = (text or "").lower()
    explicit_color = re.match(r"^\s*\[[^\]]*\b(green|yellow|red)\b[^\]]*\]", lower)
    if explicit_color:
        return explicit_color.group(1)
    red_patterns = [
        r"\bgithub\s+pat\b", r"\bgemini\s+(?:api\s+)?key\b", r"\bapi\s*key\b", r"\bsecret\b", r"\btoken\b",
        r"\bpermissions?\.py\b", r"\bpermissions?\b", r"\badmin\b", r"\badmin_password\b", r"\bpassword\b",
        r"\bmemory\.md\b", r"\bsoul\.md\b", r"\bmemory mutation\b",
        r"\bprivate\s+(?:message|send|text|imessage)\b", r"\b1\s*on\s*1\b", r"\b1on1\b",
        r"\bdm\s+(?:send|text|message)\b", r"\bdirect message\b", r"\boutbound\b", r"\bsend routing\b",
        r"\breminders?\b", r"\bcron execution\b", r"\bdb schema\b", r"\bmigration\b",
        r"\btool permission\b", r"\bself[- ]?(?:edit|deploy)\b", r"\bmodel routing\b.*\btool\b",
    ]
    yellow_patterns = [
        r"\bpersona\b", r"\bcron\b", r"\bjobs?\b", r"\bimage\b", r"\bgpt\b", r"\bgemini\b",
        r"\bmodel\b", r"\bimessage\b", r"@davos", r"\bmention\b", r"\bweather\b", r"\blocation\b",
        r"\bcopilot\b", r"\bgithub\b", r"\bworkflow\b", r"\bactions?\b",
    ]
    green_patterns = [
        r"\bdocs?\b", r"\breadme\b", r"\bhelp text\b", r"\bwording\b", r"\btone\b", r"\bprompt\b",
        r"\btests?\b", r"\bcleanup\b", r"\bformat\b", r"\btypo\b", r"\bdependency\b",
        r"\bsports bias\b", r"\bhomer\b",
    ]
    for pattern in red_patterns:
        if re.search(pattern, lower):
            return "red"
    for pattern in yellow_patterns:
        if re.search(pattern, lower):
            return "yellow"
    for pattern in green_patterns:
        if re.search(pattern, lower):
            return "green"
    return "yellow"


def fetch_rows(db_path: str | Path) -> list[tuple[int, str, str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, request, reason, created_ts FROM change_log ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()


def format_board(rows: list[tuple[int, str, str, str]], max_per_bucket: int = 1000) -> str:
    if not rows:
        return EMPTY_CHANGE_LOG_MESSAGE
    buckets = {"green": [], "yellow": [], "red": []}
    for row_id, request, reason, created_ts in rows:
        request = redact_export_text(request or "")
        reason = redact_export_text(reason or "")
        buckets[classify_change_request(request)].append((row_id, request, reason, created_ts or ""))

    counts = {color: len(items) for color, items in buckets.items()}
    sections = [f"Triage board: GREEN {counts['green']} | YELLOW {counts['yellow']} | RED {counts['red']}"]
    labels = {
        "green": "GREEN - safe Codex batch candidates",
        "yellow": "YELLOW - review one at a time / may need Mini smoke",
        "red": "RED - no phone shipping; isolate with owner review",
    }
    for color in ("green", "yellow", "red"):
        items = buckets[color]
        if not items:
            continue
        lines = []
        for row_id, request, reason, created_ts in items[:max_per_bucket]:
            date = created_ts[:10] if created_ts else "unknown-date"
            suffix = f" -> {reason}" if reason else ""
            lines.append(f"#{row_id} ({date}): {request}{suffix}")
        sections.append(labels[color] + ":\n" + "\n".join(lines))
        if len(items) > max_per_bucket:
            sections.append(f"...and {len(items) - max_per_bucket} more {color.upper()} item(s).")
    sections.append(
        "Commands: log [thing] adds + colors it. log done [id] removes it. "
        "ship safe cleanup builds a Codex handoff; it never edits or deploys."
    )
    return "\n\n".join(sections)


def write_snapshot(board: str, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    content = f"# DavosBot Phone Change Log\n\nExported: {now}\n\n{board}\n"
    stable_path = output_dir / "change_log_board.md"
    timestamped_path = output_dir / f"change_log_board_{stamp}.md"
    stable_path.write_text(content, encoding="utf-8")
    timestamped_path.write_text(content, encoding="utf-8")
    return stable_path, timestamped_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export DavosBot phone change_log board.")
    parser.add_argument("--db", default=BOT_DB_PATH, help="Path to davosbot.db")
    parser.add_argument("--output-dir", default=str(ROOT / "exports" / "private"))
    parser.add_argument("--stdout", action="store_true", help="Print the board to stdout instead of writing files.")
    args = parser.parse_args()

    rows = fetch_rows(args.db)
    board = format_board(rows)
    if args.stdout:
        print(board)
        return 0

    stable_path, timestamped_path = write_snapshot(board, Path(args.output_dir))
    print(f"Exported {len(rows)} change-log row(s).")
    print(f"Stable: {stable_path}")
    print(f"Snapshot: {timestamped_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
