import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from .runtime_locks import schedule_locked
from .config import BOT_DB_PATH
from .db import connect_bot_db

logger = logging.getLogger("davosbot.tools")

_REMINDER_MAX_SEND_ATTEMPTS = 5  # Legacy display threshold for old failed rows.
_REMINDER_LEGACY_HIDDEN_FAILURE_ATTEMPTS = _REMINDER_MAX_SEND_ATTEMPTS - 1


def _utcnow_naive() -> datetime:
    """Return UTC without tzinfo for existing SQLite string comparisons."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@schedule_locked
def _set_reminder(message: str, due_ts: str, originating_chat_id: str = "") -> str:
    """Insert a reminder. The originating chat is the ONLY source of routing truth."""
    if not originating_chat_id:
        return "Couldn't determine where to send the reminder \u2014 please ask from a DM or @mention me in a group chat."

    # Normalize due_ts: LLMs often emit ISO-T or trailing Z, but SQLite's lexical
    # `due_ts <= datetime('now')` comparison expects 'YYYY-MM-DD HH:MM:SS'.
    due_ts = (due_ts or "").strip().replace("T", " ").rstrip("Z").strip()[:19]
    try:
        due_dt = datetime.strptime(due_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return f"Invalid time format '{due_ts}'. Use UTC YYYY-MM-DD HH:MM:SS (e.g. 2026-04-29 15:00:00)."
    # Allow up to 2 minutes in the past to absorb LLM rounding ("in 1 minute" computed
    # at second :59 would otherwise reject). Anything older than 2 min is genuinely stale.
    now_utc = _utcnow_naive()
    if due_dt <= now_utc - timedelta(minutes=2):
        return f"That time ({due_ts} UTC) is in the past. Pick a future time."
    if due_dt <= now_utc:
        # Round forward so the reminder fires on the next tick rather than firing instantly
        # before the user has read the confirmation.
        due_dt = now_utc + timedelta(seconds=30)
        due_ts = due_dt.strftime("%Y-%m-%d %H:%M:%S")

    with connect_bot_db(BOT_DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO reminders (chat_id, message, due_ts, origin_chat_id) VALUES (?, ?, ?, ?)",
            (originating_chat_id, message, due_ts, originating_chat_id),
        )
        reminder_id = cur.lastrowid
    logger.info("Reminder #%d set: chat=%s due=%s msg=%r", reminder_id, originating_chat_id, due_ts, message[:60])
    return f"Got it \u2014 I'll remind you {_humanize_due(due_ts)}: '{message}'"


def _humanize_due(due_ts: str) -> str:
    """Render a UTC 'YYYY-MM-DD HH:MM:SS' string in Pacific local time, conversationally."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone
        dt_utc = datetime.strptime(due_ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(ZoneInfo("America/Los_Angeles"))
        now_local = datetime.now(ZoneInfo("America/Los_Angeles"))
        same_day = dt_local.date() == now_local.date()
        tomorrow = dt_local.date() == (now_local.date() + timedelta(days=1))
        time_str = dt_local.strftime("%I:%M %p").lstrip("0").lower()
        if same_day:
            return f"today at {time_str} PT"
        if tomorrow:
            return f"tomorrow at {time_str} PT"
        # Within the next week: weekday name; further out: date.
        delta_days = (dt_local.date() - now_local.date()).days
        if 0 < delta_days < 7:
            return f"{dt_local.strftime('%A')} at {time_str} PT"
        return f"{dt_local.strftime('%b')} {dt_local.day} at {time_str} PT"
    except Exception:
        return f"at {due_ts} UTC"


@schedule_locked
def _list_reminders(chat_id: str) -> str:
    """Return a positional list (1, 2, 3...) - never expose internal DB ids to the user."""
    if not chat_id:
        return "No chat context \u2014 ask from a DM or GC."
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        rows = _visible_reminder_rows(conn, chat_id)
    finally:
        conn.close()
    if not rows:
        return "No pending reminders."
    lines = [
        f"{i+1}. {_humanize_due(row[2])} \u2014 {row[1]}{_reminder_delivery_suffix(row[3], row[4])}"
        for i, row in enumerate(rows)
    ]
    return "Pending reminders:\n" + "\n".join(lines)


def _visible_reminder_rows(conn: sqlite3.Connection, chat_id: str) -> list:
    return conn.execute(
        "SELECT id, message, due_ts, COALESCE(sent, 0), COALESCE(send_attempts, 0) "
        "FROM reminders "
        "WHERE (origin_chat_id = ? OR (COALESCE(origin_chat_id,'') = '' AND chat_id = ?)) "
        "AND (sent = 0 OR (sent = 1 AND COALESCE(send_attempts, 0) >= ?)) "
        "ORDER BY due_ts ASC",
        (chat_id, chat_id, _REMINDER_LEGACY_HIDDEN_FAILURE_ATTEMPTS),
    ).fetchall()


def _reminder_delivery_suffix(sent: int, attempts: int) -> str:
    try:
        sent_i = int(sent or 0)
        attempts_i = int(attempts or 0)
    except (TypeError, ValueError):
        return ""
    if attempts_i >= _REMINDER_MAX_SEND_ATTEMPTS:
        if sent_i:
            return f" (delivery failed after {_REMINDER_MAX_SEND_ATTEMPTS} attempts; cancel or recreate it)"
        return ""
    if sent_i and attempts_i >= _REMINDER_LEGACY_HIDDEN_FAILURE_ATTEMPTS:
        return " (delivery failed before this fix; cancel or recreate it)"
    return ""


@schedule_locked
def _cancel_reminder(position: int, originating_chat_id: str = "") -> str:
    """Cancel by 1-based position within the current chat's pending list.

    Internal DB ids are never exposed; the LLM and user both work with positions.
    """
    if not originating_chat_id:
        return "No chat context \u2014 ask from a DM or GC."
    if position < 1:
        return "Position must be 1 or higher."
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        rows = _visible_reminder_rows(conn, originating_chat_id)
        if not rows:
            return "No pending reminders to cancel."
        if position > len(rows):
            return f"You only have {len(rows)} pending reminder(s)."
        rid, msg, due_ts = rows[position - 1][0], rows[position - 1][1], rows[position - 1][2]
        conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        conn.commit()
    finally:
        conn.close()
    return f"Cancelled: {msg} ({_humanize_due(due_ts)})."


@schedule_locked
def _cancel_reminders(positions: list[int], originating_chat_id: str = "") -> str:
    """Cancel multiple reminders by their visible 1-based positions."""
    if not originating_chat_id:
        return "No chat context \u2014 ask from a DM or GC."
    parsed_positions = []
    for pos in positions:
        try:
            parsed = int(pos)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            parsed_positions.append(parsed)
    unique_positions = sorted(set(parsed_positions))
    if not unique_positions:
        return "Tell me which reminder number to cancel."
    if len(unique_positions) == 1:
        return _cancel_reminder(unique_positions[0], originating_chat_id=originating_chat_id)

    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        rows = _visible_reminder_rows(conn, originating_chat_id)
        if not rows:
            return "No pending reminders to cancel."
        invalid = [pos for pos in unique_positions if pos > len(rows)]
        if invalid:
            return f"You only have {len(rows)} pending reminder(s)."

        cancelled = []
        for pos in unique_positions:
            row = rows[pos - 1]
            rid, msg, due_ts = row[0], row[1], row[2]
            conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
            cancelled.append(f"- {msg} ({_humanize_due(due_ts)})")
        conn.commit()
    finally:
        conn.close()

    return "Cancelled reminders:\n" + "\n".join(cancelled)
