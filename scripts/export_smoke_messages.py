#!/usr/bin/env python3
"""Export filtered Apple Messages smoke-test snippets to a private local file.

This is intentionally query-first: broad transcript export should stay explicit.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
DEFAULT_OUTPUT_DIR = ROOT / "exports" / "private"

_PRINTABLE_RE = re.compile(rb"[\x09\x0a\x0d\x20-\x7e]{3,}")
_PHONE_RE = re.compile(r"\+1\d{10}\b")
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|AIza[0-9A-Za-z_\-]{20,})\b"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|webhook)\s*[:=]\s*\S+"
)
_ATTRIBUTED_METADATA = {
    "streamtyped",
    "NSAttributedString",
    "NSObject",
    "NSString",
    "NSDictionary",
    "NSNumber",
    "NSValue",
    "__kIMMessagePartAttributeName",
}


@dataclass
class SmokeMessage:
    rowid: int
    ts: str
    direction: str
    text: str


def redact_text(text: str) -> str:
    text = _TOKEN_RE.sub("[redacted]", text or "")
    text = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return _PHONE_RE.sub(lambda m: f"{m.group(0)[:3]}...{m.group(0)[-2:]}", text)


def decode_attributed_body(blob: bytes | None) -> str:
    if not blob:
        return ""
    chunks = []
    for raw in _PRINTABLE_RE.findall(blob):
        chunk = raw.decode("utf-8", "ignore").strip("\x00")
        if not chunk or chunk in _ATTRIBUTED_METADATA or chunk.startswith("__k"):
            continue
        chunks.append(chunk)
    if not chunks:
        return ""
    return max(chunks, key=len).strip()


def fetch_messages(db_path: Path, *, scan_limit: int) -> list[SmokeMessage]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                ROWID AS rowid,
                is_from_me,
                text,
                attributedBody,
                datetime(date/1000000000 + strftime('%s','2001-01-01'), 'unixepoch', 'localtime') AS ts
            FROM message
            ORDER BY date DESC
            LIMIT ?
            """,
            (scan_limit,),
        ).fetchall()
    finally:
        conn.close()

    messages = []
    for row in rows:
        text = row["text"] or decode_attributed_body(row["attributedBody"])
        text = redact_text(text).strip()
        if not text:
            continue
        messages.append(
            SmokeMessage(
                rowid=int(row["rowid"]),
                ts=row["ts"] or "",
                direction="from Mini" if row["is_from_me"] else "to Mini",
                text=text,
            )
        )
    return messages


def filter_messages(messages: list[SmokeMessage], query: str, *, limit: int) -> list[SmokeMessage]:
    terms = [term.lower() for term in re.split(r"\s+", query or "") if term.strip()]
    if terms:
        messages = [
            message
            for message in messages
            if all(term in message.text.lower() for term in terms)
        ]
    return messages[:limit]


def format_markdown(messages: list[SmokeMessage], query: str) -> str:
    exported = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Exported: {exported}",
        f"Query: {query or '(recent explicit)'}",
        "",
    ]
    if not messages:
        lines.append("No matching smoke messages found.")
        return "\n".join(lines) + "\n"
    for message in messages:
        lines.extend([
            f"## {message.ts} | {message.direction} | message #{message.rowid}",
            "",
            "```text",
            message.text[:4000],
            "```",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def write_snapshot(content: str, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stable = output_dir / "smoke_messages.md"
    stamped = output_dir / f"smoke_messages_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    stable.write_text(content, encoding="utf-8")
    stamped.write_text(content, encoding="utf-8")
    return stable, stamped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", "-q", default="", help="Required unless --recent is set. Example: model options")
    parser.add_argument("--recent", action="store_true", help="Export the most recent decoded messages without query filtering")
    parser.add_argument("--limit", type=int, default=8, help="Maximum matching messages to export")
    parser.add_argument("--scan-limit", type=int, default=300, help="Recent Messages rows to scan before filtering")
    parser.add_argument("--db", type=Path, default=DEFAULT_CHAT_DB, help="Apple Messages chat.db path")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()

    if not args.query and not args.recent:
        parser.error("provide --query, or pass --recent explicitly")

    messages = fetch_messages(args.db, scan_limit=max(args.scan_limit, args.limit))
    filtered = messages[: args.limit] if args.recent else filter_messages(messages, args.query, limit=args.limit)
    content = format_markdown(filtered, args.query)
    stable, stamped = write_snapshot(content, args.output_dir)
    if args.stdout:
        print(content, end="")
    else:
        print(f"Wrote {stable}")
        print(f"Wrote {stamped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
