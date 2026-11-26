"""Owner-DM rescheduling without cancelling the original reminder.

Drafts are volatile, short-lived, sender/origin scoped and never authorization.
The delivery worker and Work-channel reminder edits are deliberately unchanged.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time

from .db import connect_bot_db
from .permissions import is_owner
from .reminder_parser import ReminderParseError, parse_deterministic_reminder
from .reminder_tools import _humanize_due, _visible_reminder_rows
from .runtime_locks import schedule_locked


_COMMAND = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(?:move|reschedule|change|update|push|edit)\s+"
    r"(?:(?:my|the|this|that)\s+)?reminder\b"
    r"(?:\s+(?:number\s+|#)?(?P<position>\d+)(?=\s|$))?"
    r"(?:\s+(?P<when>.+))?$", re.I,
)
_POSITION = re.compile(r"^(?:number\s+|#)?(\d+)$", re.I)
_CANCEL_DRAFT = re.compile(r"^(?:never\s*mind|cancel(?:\s+(?:this\s+)?edit)?|stop\s+editing)$", re.I)
_TIME_START = re.compile(
    r"^(?:at\s+|in\s+|today\b|tomorrow\b|tmw\b|tmrw\b|tonight\b|next\s+|"
    r"mon(?:day)?\b|tue(?:sday)?\b|wed(?:nesday)?\b|thu(?:rsday)?\b|fri(?:day)?\b|"
    r"sat(?:urday)?\b|sun(?:day)?\b|jan\w*\b|feb\w*\b|mar\w*\b|apr\w*\b|may\b|"
    r"jun\w*\b|jul\w*\b|aug\w*\b|sep\w*\b|oct\w*\b|nov\w*\b|dec\w*\b|"
    r"\d{1,2}(?:[:./-]|\s*(?:am|pm)\b))", re.I,
)
_EXPLICIT_CLOCK = re.compile(r"\b\d{1,2}(?:[:.]\d{2})\b|\b\d{1,2}(?:[:.]\d{2})?\s*[ap]\.?m\.?\b", re.I)
_SENTINEL = "reschedule this reminder"
_DRAFT_SECONDS = 300
_MAX_ROWS = 100
_drafts = {}


@dataclass(frozen=True)
class _Draft:
    expires: float
    snapshot: tuple
    target_id: int | None
    due_ts: str | None


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").strip()).rstrip(".!?").strip()


def is_edit_request(text: str) -> bool:
    return bool(_COMMAND.fullmatch(_clean(text)))


@schedule_locked
def clear_drafts(origin: str) -> None:
    for key in list(_drafts):
        if origin in key:
            _drafts.pop(key, None)


def _eligible_followup(text, draft):
    return bool(_CANCEL_DRAFT.fullmatch(text) or _TIME_START.match(text)
                or _POSITION.fullmatch(text))


@schedule_locked
def discard_unrelated(sender: str, origin: str, text: str, *, authorized: bool) -> None:
    """Called before native early returns; never reads or writes reminder rows."""
    key = (sender, origin)
    draft = _drafts.get(key)
    if draft and (not authorized or sender != origin or time.monotonic() >= draft.expires
                  or not _eligible_followup(_clean(text), draft)):
        _drafts.pop(key, None)


def _snapshot(conn, origin):
    visible = _visible_reminder_rows(conn, origin)
    if len(visible) > _MAX_ROWS:
        raise ValueError("too_many_rows")
    # Retain the same visible positions as list reminders, including legacy
    # failure rows. Those rows are displayed but cannot be revived by editing.
    return tuple(tuple(row) + tuple(conn.execute(
        "SELECT chat_id,COALESCE(origin_chat_id,''),COALESCE(last_attempt_ts,'') "
        "FROM reminders WHERE id=?", (row[0],),
    ).fetchone()) for row in visible)


def _now():
    return datetime.now(timezone.utc)


def _future(due_ts, now):
    try:
        due = datetime.strptime(due_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return due > now
    except (ValueError, TypeError):
        return False


def _due(text, now):
    when = re.sub(r"^(?:to|for)\s+", "", text.strip(), flags=re.I)
    if not (re.match(r"in\s+", when, re.I) or _EXPLICIT_CLOCK.search(when)):
        raise ReminderParseError("Give one time with am/pm or HH:MM, such as tomorrow at 8am.")
    parsed = parse_deterministic_reminder(f"remind me {when} to {_SENTINEL}", now=now)
    if parsed is None or parsed.message != _SENTINEL or not _future(parsed.due_ts, now):
        raise ReminderParseError("Give one future time, such as tomorrow at 8am.")
    return parsed.due_ts


def _ask(snapshot, target_id):
    if target_id is None:
        lines = ["Which reminder number should I move? Nothing has changed."]
        lines.extend(f"{index}. {_humanize_due(row[2])}: {row[1]}"
                     for index, row in enumerate(snapshot, 1))
        return "\n".join(lines)
    return "What future time should I move it to? The original reminder is still active."


def _save_time(sender, origin, snapshot, target_id, due_ts, db_path):
    """Update the exact still-future row under the shared scheduler lock and CAS."""
    if not is_owner(sender) or sender != origin:
        return "Only the owner can change that reminder. Nothing was changed."
    with connect_bot_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _snapshot(conn, origin)
        if current != snapshot:
            return "The reminder list changed. Nothing was changed by this edit; use list reminders and ask again."
        row = next((item for item in current if item[0] == target_id), None)
        now = _now()
        if row is None or row[3] or row[4] or not _future(row[2], now):
            return "That reminder is already due, sent, or has delivery attempts. I haven't changed or restarted it."
        if not _future(due_ts, now):
            return "That replacement time is no longer in the future. The original reminder is unchanged."
        if row[2] != due_ts:
            changed = conn.execute(
                "UPDATE reminders SET due_ts=? WHERE id=? AND message=? AND due_ts=? AND sent=0 "
                "AND COALESCE(send_attempts,0)=0 "
                "AND (origin_chat_id=? OR (COALESCE(origin_chat_id,'')='' AND chat_id=?))",
                (due_ts, target_id, row[1], row[2], origin, origin),
            )
            if changed.rowcount != 1:
                raise ValueError("update_not_confirmed")
    # Confirmation is based on reopened saved fields, not model text or an
    # update count alone. A failed readback never triggers a repeated update.
    expected = row[:2] + (due_ts,) + row[3:]
    with connect_bot_db(db_path) as conn:
        saved = next((item for item in _snapshot(conn, origin) if item[0] == target_id), None)
    if saved != expected:
        return "I couldn't verify the saved reminder time. Use list reminders before trying another change."
    prefix = "No change needed" if row[2] == due_ts else "Moved the reminder"
    return f"{prefix}: '{saved[1]}' is set for {_humanize_due(saved[2])}."


@schedule_locked
def handle_edit(sender: str, text: str, *, originating_chat_id: str, db_path: str) -> str | None:
    origin = originating_chat_id
    key = (sender, origin)
    clean = _clean(text)
    command = _COMMAND.fullmatch(clean)
    draft = _drafts.get(key)
    if command:
        _drafts.pop(key, None)
        draft = None
    if draft and time.monotonic() >= draft.expires:
        _drafts.pop(key, None)
        draft = None
    if not command and not (draft and _eligible_followup(clean, draft)):
        _drafts.pop(key, None)
        return None
    if not is_owner(sender) or sender != origin:
        _drafts.pop(key, None)
        return None
    if draft and not command and _CANCEL_DRAFT.fullmatch(clean):
        _drafts.pop(key, None)
        return "Reminder edit cancelled. I haven't changed the original reminder."
    try:
        with connect_bot_db(db_path) as conn:
            snapshot = _snapshot(conn, origin)
        if not snapshot:
            _drafts.pop(key, None)
            return "No pending reminders to move in this chat."
        if draft and not command and snapshot != draft.snapshot:
            _drafts.pop(key, None)
            return "The reminder list changed. Nothing was changed by this edit; use list reminders and ask again."
        target_id = draft.target_id if draft else None
        due_ts = draft.due_ts if draft else None
        position = command["position"] if command else None
        when = command["when"] if command else clean
        if not command and target_id is None and _POSITION.fullmatch(clean):
            position = _POSITION.fullmatch(clean)[1]
            when = None
        if position:
            index = int(position)
            if not 1 <= index <= len(snapshot):
                return f"Choose one of the {len(snapshot)} reminder numbers. Nothing has changed."
            target_id = snapshot[index - 1][0]
        elif target_id is None and len(snapshot) == 1:
            target_id = snapshot[0][0]
        target = next((row for row in snapshot if row[0] == target_id), None)
        if target and (target[3] or target[4] or not _future(target[2], _now())):
            _drafts.pop(key, None)
            return "That reminder is already due, sent, or has delivery attempts. I haven't changed or restarted it."
        if when:
            try:
                due_ts = _due(when, _now())
            except ReminderParseError as exc:
                _drafts[key] = _Draft(time.monotonic() + _DRAFT_SECONDS, snapshot, target_id, None)
                return f"I haven't changed the original reminder. {exc}"
        if target_id is None or due_ts is None:
            _drafts[key] = _Draft(time.monotonic() + _DRAFT_SECONDS, snapshot, target_id, due_ts)
            return _ask(snapshot, target_id)
        _drafts.pop(key, None)
        return _save_time(sender, origin, snapshot, target_id, due_ts, db_path)
    except Exception:
        _drafts.pop(key, None)
        return "I couldn't verify a reminder change. Use list reminders to check the saved time; I won't retry it automatically."
