"""Owner-only conversational state for automated golf-club suggestions.

The hourly shopper runs outside the DavosBot poll loop, so outbound suggestions
must be written into the bot's existing history before a short owner reply such
as ``yes`` or ``no`` can have reliable context.  This module intentionally uses
the existing ``bot_log`` and ``messages`` tables rather than adding schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .config import BOT_DB_PATH, OWNER_ID, normalize_handle
from .db import connect_bot_db
from .imessage import send_message
from .permissions import is_owner


SUGGESTION_EVENT = "club_suggestion_sent"
FEEDBACK_EVENT = "club_suggestion_feedback"
_SUGGESTION_PREFIX = "Club suggestion:"
_BARE_FEEDBACK = {
    "yes": "yes",
    "yep": "yes",
    "yeah": "yes",
    "no": "no",
    "nope": "no",
    "pass": "no",
}
_EXPLICIT_FEEDBACK_RE = re.compile(
    r"^(?:club|golf\s+club|suggestion)\s+(yes|yep|yeah|no|nope|pass)\s*[.!]?$",
    re.IGNORECASE,
)
_NATURAL_FEEDBACK_RE = re.compile(
    r"^(yes|yep|yeah|no|nope|pass)\s+(?:on|for)\s+(?:that|this)(?:\s+(?:golf\s+)?club)?\s*[.!]?$",
    re.IGNORECASE,
)


def _clean_text(value: Any, field: str, *, required: bool = False, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} is too long")
    return text


def _normalize_payload(payload: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("suggestion payload must be an object")
    normalized = {
        "name": _clean_text(payload.get("name"), "name", required=True, limit=160),
        "category": _clean_text(payload.get("category"), "category", limit=80),
        "price": _clean_text(payload.get("price"), "price", limit=80),
        "condition": _clean_text(payload.get("condition"), "condition", limit=120),
        "url": _clean_text(payload.get("url"), "url", required=True, limit=1000),
        "rationale": _clean_text(
            payload.get("rationale"), "rationale", required=True, limit=600
        ),
    }
    parsed = urlparse(normalized["url"])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an http(s) listing")
    fingerprint_source = f"{normalized['name'].lower()}|{normalized['url'].lower()}"
    normalized["fingerprint"] = hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()[:16]
    return normalized


def format_suggestion_message(payload: dict[str, Any]) -> str:
    item = _normalize_payload(payload)
    lines = [f"{_SUGGESTION_PREFIX_FIX}{item['name']}"]
    if item["category"]:
        lines.append(f"Type: {item['category']}")
    if item["price"]:
        lines.append(f"Price: {item['price']}")
    if item["condition"]:
        lines.append(f"Condition: {item['condition']}")
    lines.extend(
        [
            f"Why it fits: {item['rationale']}",
            item["url"],
            "Reply yes or no. I’ll use that feedback and keep a new suggestion coming every hour.",
        ]
    )
    return "\n".join(lines)


# Kept separate so the human-facing prefix is easy to find in history checks.
_SUGGESTION_PREFIX_FIX = _SUGGESTION_PREFIX + " "


def send_club_suggestion(payload: dict[str, Any]) -> dict[str, Any]:
    """Send one suggestion to the configured owner and persist its context."""
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID is not configured")
    item = _normalize_payload(payload)
    message = format_suggestion_message(item)
    if not send_message(OWNER_ID, message, is_group=False):
        raise RuntimeError("iMessage delivery failed")

    event_payload = dict(item)
    event_payload["message"] = message
    with connect_bot_db(BOT_DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
            ("system", SUGGESTION_EVENT, json.dumps(event_payload, sort_keys=True)),
        )
        event_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO messages (sender, role, content, ts) VALUES (?, ?, ?, ?)",
            (
                normalize_handle(OWNER_ID),
                "assistant",
                message,
                datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            ),
        )
    return {
        "sent": True,
        "suggestion_event_id": event_id,
        "fingerprint": item["fingerprint"],
    }


def _load_event_rows(limit: int = 100) -> list[dict[str, Any]]:
    with connect_bot_db(BOT_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, event_type, payload FROM bot_log "
            "WHERE event_type IN (?, ?) ORDER BY id DESC LIMIT ?",
            (SUGGESTION_EVENT, FEEDBACK_EVENT, limit),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for event_id, timestamp, event_type, raw_payload in rows:
        try:
            payload = json.loads(raw_payload or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        events.append(
            {
                "event_id": int(event_id),
                "timestamp": str(timestamp or ""),
                "event_type": str(event_type or ""),
                "payload": payload,
            }
        )
    return events


def get_club_suggestion_state(limit: int = 20) -> dict[str, Any]:
    """Return safe, owner-handle-free history for the hourly shopping task."""
    events = _load_event_rows(max(40, limit * 4))
    feedback_by_suggestion: dict[int, dict[str, Any]] = {}
    for event in events:
        if event["event_type"] != FEEDBACK_EVENT:
            continue
        try:
            suggestion_id = int(event["payload"].get("suggestion_event_id"))
        except (TypeError, ValueError):
            continue
        feedback_by_suggestion.setdefault(suggestion_id, event["payload"])

    suggestions = []
    for event in events:
        if event["event_type"] != SUGGESTION_EVENT:
            continue
        payload = event["payload"]
        suggestions.append(
            {
                "suggestion_event_id": event["event_id"],
                "timestamp": event["timestamp"],
                "name": payload.get("name", ""),
                "category": payload.get("category", ""),
                "price": payload.get("price", ""),
                "condition": payload.get("condition", ""),
                "url": payload.get("url", ""),
                "rationale": payload.get("rationale", ""),
                "fingerprint": payload.get("fingerprint", ""),
                "decision": feedback_by_suggestion.get(event["event_id"], {}).get(
                    "decision", "pending"
                ),
            }
        )
        if len(suggestions) >= limit:
            break
    return {"suggestions": suggestions}


def _latest_open_suggestion() -> dict[str, Any] | None:
    state = get_club_suggestion_state(limit=20)
    if not state["suggestions"]:
        return None
    suggestion = state["suggestions"][0]
    if suggestion.get("decision") != "pending":
        return None
    try:
        sent_at = datetime.fromisoformat(str(suggestion.get("timestamp") or ""))
    except ValueError:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - sent_at.astimezone(timezone.utc)
    if age < -timedelta(minutes=5) or age > timedelta(hours=24):
        return None
    return suggestion


def _latest_message_is_suggestion(sender: str) -> bool:
    with connect_bot_db(BOT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT role, content FROM messages WHERE sender = ? ORDER BY id DESC LIMIT 1",
            (normalize_handle(sender),),
        ).fetchone()
    return bool(row and row[0] == "assistant" and str(row[1]).startswith(_SUGGESTION_PREFIX))


def _parse_feedback(text: str) -> tuple[str | None, bool]:
    clean = re.sub(r"\s+", " ", (text or "").strip()).lower()
    bare = _BARE_FEEDBACK.get(clean.strip(".! "))
    if bare:
        return bare, False
    match = _EXPLICIT_FEEDBACK_RE.fullmatch(clean) or _NATURAL_FEEDBACK_RE.fullmatch(clean)
    if not match:
        return None, False
    return _BARE_FEEDBACK[match.group(1).lower()], True


def _record_feedback(
    sender: str,
    original_text: str,
    suggestion: dict[str, Any],
    decision: str,
) -> str:
    name = str(suggestion.get("name") or "that club")
    if decision == "yes":
        reply = (
            f"Yep. I shortlisted {name}. I’ll lean toward similar fits and keep the hourly "
            "suggestions coming."
        )
    else:
        reply = (
            f"Got it. I’ll skip {name}, use that to make the next pick different, and keep "
            "the hourly suggestions coming."
        )
    payload = {
        "suggestion_event_id": int(suggestion["suggestion_event_id"]),
        "suggestion_fingerprint": str(suggestion.get("fingerprint") or ""),
        "name": name,
        "decision": decision,
    }
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    with connect_bot_db(BOT_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
            (normalize_handle(sender), FEEDBACK_EVENT, json.dumps(payload, sort_keys=True)),
        )
        conn.execute(
            "INSERT INTO messages (sender, role, content, ts) VALUES (?, ?, ?, ?)",
            (normalize_handle(sender), "user", original_text.strip(), now),
        )
        conn.execute(
            "INSERT INTO messages (sender, role, content, ts) VALUES (?, ?, ?, ?)",
            (normalize_handle(sender), "assistant", reply, now),
        )
    return reply


def handle_club_command(sender: str, text: str) -> str | None:
    """Handle owner feedback/status while leaving unrelated chat untouched."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    lower = clean.lower().strip(".! ")
    is_explicit_club_command = bool(
        re.match(r"^(?:club|golf\s+club|suggestion)\b", lower)
    ) or lower in {"club status", "club suggestions"}

    decision, explicit = _parse_feedback(clean)
    if decision is None and lower not in {"club status", "club suggestions"}:
        return None
    if not is_owner(sender):
        return "Club suggestion feedback is owner-only." if is_explicit_club_command else None

    if lower in {"club status", "club suggestions"}:
        state = get_club_suggestion_state(limit=5)
        suggestions = state["suggestions"]
        if not suggestions:
            return "No club suggestions have been sent yet."
        latest = suggestions[0]
        return (
            f"Latest club suggestion: {latest['name']} ({latest['decision']}). "
            "The hourly suggestion loop is managed by Codex."
        )

    suggestion = _latest_open_suggestion()
    if suggestion is None:
        return "I don’t have an unanswered club suggestion right now." if explicit else None
    if not explicit and not _latest_message_is_suggestion(sender):
        return None
    return _record_feedback(sender, clean, suggestion, decision)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DavosBot club suggestion helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("send", help="read one JSON suggestion from stdin and send it")
    status_parser = subparsers.add_parser("status", help="print recent safe suggestion state")
    status_parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if args.command == "send":
        payload = json.load(sys.stdin)
        print(json.dumps(send_club_suggestion(payload), sort_keys=True))
        return 0
    print(json.dumps(get_club_suggestion_state(limit=max(1, min(args.limit, 50))), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
