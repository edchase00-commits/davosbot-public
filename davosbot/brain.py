import base64
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
import requests
from .runtime_locks import MODEL_STATE_LOCK, PERSONALITY_FILE_LOCK, schedule_locked
from .config import (
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_KEEP_WARM_ENABLED,
    OLLAMA_KEEP_WARM_INTERVAL_SECONDS,
    OLLAMA_KEEP_WARM_TIMEOUT,
    OLLAMA_SIMPLE_CHAT_NUM_PREDICT,
    OLLAMA_SIMPLE_CHAT_MODEL,
    OLLAMA_SIMPLE_CHAT_TEMPERATURE,
    OLLAMA_SIMPLE_CHAT_TIMEOUT,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    MODEL_ROUTE_CODE_REVIEW,
    MODEL_ROUTE_COMPLEX_REASONING,
    MODEL_ROUTE_SIMPLE_CHAT,
    OWNER_ID,
    BOT_DB_PATH,
    ADMIN_PASSWORD,
)
from .billing import check_gemini_budget
from .db import connect_bot_db, run_migration
from .imessage import send_message
from .permissions import redact_secret
from .alerts import send_owner_alert
from . import failure_copy as _failure_copy
from . import simple_chat as _simple_chat

_IMAGE_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".heic": "image/heic", ".heif": "image/heif",
    ".webp": "image/webp",
}

def _image_part(image_path: str) -> dict | None:
    try:
        ext = os.path.splitext(image_path)[1].lower()
        mime = _IMAGE_MIME_MAP.get(ext, "image/jpeg")
        with open(image_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return {"inline_data": {"mime_type": mime, "data": data}}
    except Exception as e:
        logger.warning("Failed to load image attachment: %s", type(e).__name__)
        return None

logger = logging.getLogger(__name__)


def _close_response(resp: object | None) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        close()

OLLAMA_TIMEOUT = 30
OLLAMA_HEALTH_TIMEOUT = 5
OLLAMA_CHECK_INTERVAL = 300  # seconds between re-checks when Ollama is down
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

_ollama_down = False
_last_ollama_check = 0.0
_ollama_down_alerted = False
_ollama_state_epoch = 0
_ollama_keep_warm_thread_started = False
_OLLAMA_RECOVERY_PROBE_SYSTEM = (
    "You are DavosBot running a local light-chat health check. "
    "Reply with one short sentence, no provider status. "
    + ("Keep the reply natural, concise, and useful. " * 80)
)[:3800]

_COMPLEX_REASONING_RE = re.compile(
    r"\b(?:analy[sz]e|architecture|build|compare|debug|deep\s+dive|design|diagnose|"
    r"evaluate|implement|investigate|plan|prioriti[sz]e|reason|review|roadmap|"
    r"root\s+cause|strategy|system\s+design|trade[-\s]?offs?)\b",
    re.IGNORECASE,
)

_CODE_REVIEW_RE = re.compile(
    r"\b(?:branch|ci|code\s+review|codex|commit|diff|github|patch|pr|pull\s+request|"
    r"refactor|repo|ship\s+this|test\s+fail|tests?\s+fail)\b",
    re.IGNORECASE,
)


_CAPABILITY_GAP_RE = re.compile(
    r"I (don't|do not|can't|cannot) have (access|the ability|a way|that capability)"
    r"|I('m| am) (not able|unable) to (access|browse|connect|retrieve|check|send|create|schedule|set|call|run|use|read|write)"
    r"|I (don't|do not|can't|cannot) (access|browse|connect|retrieve|schedule|make (a |an |phone )?call|send (emails?|texts?)|control)"
    r"|I (?:can't|cannot|couldn't|could not|am unable|am not able) (?:do|help with|complete|handle) (?:that|this|it)(?:\s+right now)?"
    r"|(that'?s?|this is) beyond (my|what I can) (capabilities?|abilities?|do|handle)"
    r"|I (?:need|would need) (?:the|a|more) (?:file|spreadsheet|sheet|workbook|csv|context|data)"
    r"|(?:please|you(?:'ll| will) need to) (?:upload|provide|attach|share|send) (?:the|a) (?:file|spreadsheet|sheet|workbook|csv|context|data)"
    r"|I (?:don't|do not) have enough (?:context|information|data)",
    re.IGNORECASE,
)

_PROVIDER_STATUS_REPLY_RE = re.compile(
    r"\b(?:ollama|gemma|gemini|local model|backend|provider)\b.{0,100}\b"
    r"(?:failed|down|unavailable|timed out|timeout|trying|switch(?:ing|ed)?|fall(?:ing)? back)\b"
    r"|\b(?:fall(?:ing)? back|switch(?:ing|ed)?|trying)\b.{0,100}\b"
    r"(?:gemini|ollama|gemma|local model|backend|provider)\b",
    re.IGNORECASE | re.DOTALL,
)

_MODEL_ROUTING_QUERY_RE = re.compile(
    r"\b(?:models?|ollama|gemma|gemini|provider|backend)\b"
    r"|\b(?:routing|fallback)\s+(?:status|options?|routes?|model|models?)\b"
    r"|\b(?:model|models?)\s+(?:routing|fallback|status|options?|routes?)\b",
    re.IGNORECASE,
)


_HELP_INTENT_RE = re.compile(
    r"^help$"
    r"|^capabilit(?:y|ies)$"
    r"|what can you do"
    r"|what capabilities"
    r"|what are (my )?commands"
    r"|what do you do"
    r"|how do (i|you) use (this|you)",
    re.IGNORECASE,
)


_USER_FACT_PATTERNS = [
    # "[Name]'s number/phone/cell is +1XXXXXXXXXX"  ? key="contact:[name]"
    (re.compile(
        r"^\s*(\w[\w\s]*?)'?s\s+"
        r"(?:number|phone(?:\s+number)?|cell(?:\s+(?:number|phone)?)?|contact)\s+"
        r"is\s+(\+?1?[\d\s\-().]{9,15})\s*$",
        re.IGNORECASE,
    ), "contact"),
    # "I am X" / "I'm X"  ? key="identity", value=rest
    (re.compile(r"^\s*i(?:'m|\s+am)\s+(.{2,80})$", re.IGNORECASE), "identity"),
    # "I work [at|in|as] X"
    (re.compile(r"^\s*i\s+work\s+(?:at|in|as)\s+(.{2,80})$", re.IGNORECASE), "work"),
    # "I like X" / "I love X"
    (re.compile(r"^\s*i\s+(?:like|love|enjoy)\s+(.{2,80})$", re.IGNORECASE), "preference"),
    # "my X is Y"
    (re.compile(r"^\s*my\s+(\w+)\s+is\s+(.{2,80})$", re.IGNORECASE), "attribute"),
    # "I just [verb] X"
    (re.compile(r"^\s*i\s+just\s+(.{3,80})$", re.IGNORECASE), "recent"),
]


def _normalize_phone(raw: str) -> str:
    """Coerce a raw phone string to E.164 format."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return raw.strip()


def detect_user_fact(text: str) -> tuple[str, str] | None:
    """Return (key, value) if text is owner self-description, else None."""
    s = (text or "").strip().rstrip(".!?")
    if not s or len(s) > 200:
        return None
    for rx, key in _USER_FACT_PATTERNS:
        m = rx.match(s)
        if m:
            if key == "contact":
                name = m.group(1).strip().lower()
                phone = _normalize_phone(m.group(2))
                return (f"contact:{name}", phone)
            if key == "attribute":
                return (f"my_{m.group(1).lower()}", m.group(2).strip())
            return (key, m.group(1).strip())
    return None


def resolve_contact(name: str) -> str | None:
    """Resolve a contact name to an E.164 phone number.

    Priority:
      1. user_facts table (key="contact:[name]")
      2. MEMORY.md ## Contacts section ("- Name: +1XXX")
    Returns None if not found.
    """
    from .config import MEMORY_PATH
    name_lower = name.strip().lower()

    # 1. user_facts
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM user_facts WHERE key = ? ORDER BY id DESC LIMIT 1",
                (f"contact:{name_lower}",),
            ).fetchone()
        if row:
            return row[0]
    except Exception:
        pass

    # 2. MEMORY.md ## Contacts section
    try:
        import os as _os
        with PERSONALITY_FILE_LOCK, open(MEMORY_PATH, encoding="utf-8") as stream:
            content = stream.read()
        section_m = re.search(r"##\s+Contacts\n(.*?)(?:\n##|\Z)", content, re.DOTALL)
        if section_m:
            section = section_m.group(1)
            line_m = re.search(
                r"^\s*-?\s*" + re.escape(name.strip()) + r"\s*:\s*(\+?1?\d{10,11})",
                section,
                re.IGNORECASE | re.MULTILINE,
            )
            if line_m:
                return _normalize_phone(line_m.group(1))
    except Exception:
        pass

    return None


def store_user_fact(key: str, value: str, source: str = "self") -> None:
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO user_facts (key, value, source) VALUES (?, ?, ?)",
                (key, value, source),
            )
    except Exception as e:
        logger.warning("store_user_fact failed: %s", e)


def detect_help_intent(text: str) -> bool:
    """True if the message is clearly asking for a command list or capability overview."""
    return bool(_HELP_INTENT_RE.search(text.strip()))


def detect_capability_gap(response: str) -> bool:
    """Return True if the LLM response signals a genuine capability limitation."""
    return bool(_CAPABILITY_GAP_RE.search(response))


def _provider_status_narration_reason(user_msg: str, reply: str | None) -> str | None:
    if not reply:
        return None
    if _MODEL_ROUTING_QUERY_RE.search(user_msg or ""):
        return None
    if _PROVIDER_STATUS_REPLY_RE.search(reply):
        return "provider_status"
    return None


def _ollama_soft_miss_reason(user_msg: str, reply: str | None) -> str | None:
    if not reply:
        return None
    if detect_capability_gap(reply):
        return "capability_gap"
    return _provider_status_narration_reason(user_msg, reply)


def _suppress_provider_status_reply(user_msg: str, reply: str | None, source: str) -> str | None:
    reason = _provider_status_narration_reason(user_msg, reply)
    if reason:
        _log_bot_event("model_debug_reply_suppressed", {"source": source, "reason": reason})
        return None
    return reply


def _init_db_tables() -> None:
    run_migration("""
        CREATE TABLE IF NOT EXISTS gemini_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            prompt_tokens INTEGER NOT NULL,
            candidates_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            source TEXT NOT NULL
        )
    """, "gemini_usage table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS missing_capabilities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            sender TEXT,
            raw_message TEXT,
            detected_intent TEXT
        )
    """, "missing_capabilities table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS bot_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            sender TEXT,
            raw_message TEXT,
            exc_type TEXT,
            exc_msg TEXT,
            traceback TEXT
        )
    """, "bot_log table")

    # Only ALTER if the column isn't already there — avoids a backup on every start.
    with closing(sqlite3.connect(BOT_DB_PATH)) as _c:
        _existing = {r[1] for r in _c.execute("PRAGMA table_info(bot_log)").fetchall()}
    for _col, _col_type in [("event_type", "TEXT"), ("payload", "TEXT")]:
        if _col not in _existing:
            run_migration(
                f"ALTER TABLE bot_log ADD COLUMN {_col} {_col_type}",
                f"add {_col} to bot_log",
            )

    run_migration("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            granted_at TEXT NOT NULL DEFAULT (datetime('now')),
            revoked_at TEXT
        )
    """, "admins table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            handle TEXT NOT NULL,
            actor TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """, "admin_audit table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS rate_limit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """, "rate_limit_log table")

    run_migration("""
        CREATE INDEX IF NOT EXISTS idx_rate_limit_sender_ts
        ON rate_limit_log(sender, timestamp)
    """, "index rate_limit_log(sender, timestamp)")

    run_migration("""
        CREATE TABLE IF NOT EXISTS bot_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_heartbeat TEXT,
            messages_processed INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_error_at TEXT
        )
    """, "bot_sessions table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL DEFAULT 'send_imessage',
            recipient TEXT NOT NULL,
            message TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at TEXT
        )
    """, "scheduled_tasks table")

    # P0: ensure chat_id and sender columns exist on scheduled_tasks
    with closing(sqlite3.connect(BOT_DB_PATH)) as _c:
        _cols = {r[1] for r in _c.execute("PRAGMA table_info(scheduled_tasks)").fetchall()}
    for _col, _type in [("chat_id", "TEXT"), ("sender", "TEXT"), ("error", "TEXT")]:
        if _col not in _cols:
            run_migration(
                f"ALTER TABLE scheduled_tasks ADD COLUMN {_col} {_type}",
                f"add {_col} to scheduled_tasks",
            )

    # NOTE: reminders schema (origin_chat_id, send_attempts) is owned by memory.init_db().
    # Don't add ALTERs here — duplicate migrations race and one wins without DEFAULT ''.

    run_migration("""
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request TEXT NOT NULL,
            reason TEXT,
            created_ts TEXT DEFAULT (datetime('now'))
        )
    """, "change_log table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS user_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT
        )
    """, "user_facts table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_by TEXT NOT NULL,
            challenger TEXT NOT NULL,
            opponent TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            winner TEXT,
            settled_at TEXT
        )
    """, "bets table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS sports_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            date TEXT NOT NULL DEFAULT (date('now')),
            sender TEXT NOT NULL,
            chat_id TEXT,
            event TEXT NOT NULL,
            bet_type TEXT NOT NULL DEFAULT 'moneyline',
            odds INTEGER NOT NULL,
            stake REAL NOT NULL,
            unit_size REAL,
            result TEXT NOT NULL DEFAULT 'pending',
            payout REAL,
            notes TEXT,
            settled_at TEXT
        )
    """, "sports_bets table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS bet_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(sender, key)
        )
    """, "bet_config table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            cron_expression TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_payload TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by TEXT,
            last_run TEXT
        )
    """, "cron_jobs table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_by TEXT NOT NULL,
            skill_name TEXT NOT NULL UNIQUE,
            trigger_phrase TEXT NOT NULL,
            response_template TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """, "skills table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS workout_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL DEFAULT (date('now')),
            sender TEXT NOT NULL,
            muscle_group TEXT,
            exercise_name TEXT NOT NULL,
            sets_json TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """, "workout_entries table")

    run_migration("""
        CREATE TABLE IF NOT EXISTS workout_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(sender, key)
        )
    """, "workout_config table")

    from .style_directives import init_style_directives_db
    init_style_directives_db()

def _log_gemini_usage(prompt_tokens: int, candidates_tokens: int, total_tokens: int, source: str) -> None:
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO gemini_usage (prompt_tokens, candidates_tokens, total_tokens, source) VALUES (?,?,?,?)",
                (prompt_tokens, candidates_tokens, total_tokens, source),
            )
    except Exception as e:
        logger.warning("Failed to log Gemini usage: %s", e)


_init_db_tables()

if not ADMIN_PASSWORD and not os.getenv("DAVOSBOT_SUPPRESS_CONFIG_WARNINGS"):
    import sys as _sys
    print(
        "WARNING: ADMIN_PASSWORD not set in .env - password gate is disabled. "
        "Non-owners cannot be temporarily elevated.",
        file=_sys.stderr,
    )


def match_skill(text: str) -> str | None:
    """Check enabled skills for a trigger-phrase match. Returns response or None.

    Trigger matching is case-insensitive substring. {input} in the response
    template is replaced with the full message text.
    """
    if not text:
        return None
    lower = text.lower()
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT trigger_phrase, response_template FROM skills WHERE enabled = 1"
            ).fetchall()
    except Exception:
        return None
    for trigger, template in rows:
        if trigger.lower() in lower:
            return template.replace("{input}", text)
    return None


_REMINDER_EDIT_RE = re.compile(
    r"\b(?:change|move|update|reschedule|push)\s+(?:my\s+)?reminder\b"
    r"|\bedit\s+(?:my\s+)?reminder\b",
    re.IGNORECASE,
)


def detect_reminder_edit_intent(text: str) -> bool:
    """True if the message is asking to modify an existing reminder."""
    return bool(_REMINDER_EDIT_RE.search(text or ""))


@schedule_locked
def handle_reminder_edit(sender: str, text: str) -> str | None:
    """Cancel the matching pending reminder and ask for or apply the new time.

    Returns a reply string if handled, or None if no pending reminders exist.
    """
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            # Match by origin_chat_id (post-fix routing) with chat_id fallback for legacy rows.
            rows = conn.execute(
                "SELECT id, message, due_ts FROM reminders "
                "WHERE (origin_chat_id = ? OR (COALESCE(origin_chat_id,'') = '' AND chat_id = ?)) "
                "AND sent = 0 ORDER BY due_ts ASC",
                (sender, sender),
            ).fetchall()
    except Exception:
        return None
    if not rows:
        return "No pending reminders to modify. Want me to set a new one?"
    if len(rows) == 1:
        rid, msg, due_ts = rows[0]
        # Cancel it and ask for new time
        try:
            with connect_bot_db(BOT_DB_PATH) as conn:
                conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (rid,))
        except Exception:
            return None
        from .tools import _humanize_due
        return (
            f"Cancelled '{msg}' (was set for {_humanize_due(due_ts)}). "
            "What time should I reschedule it for?"
        )
    # Multiple reminders — list them positionally (no internal IDs).
    from .tools import _humanize_due
    lines = [f"{i+1}. {_humanize_due(r[2])} — {r[1]}" for i, r in enumerate(rows)]
    return "Multiple pending reminders — which one?\n" + "\n".join(lines)


def log_missing_capability(sender: str, raw_message: str, detected_intent: str) -> None:
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO missing_capabilities (sender, raw_message, detected_intent) VALUES (?,?,?)",
                (sender, raw_message[:500], detected_intent[:500]),
            )
        logger.info("Logged missing capability for %s: %s", sender, detected_intent[:80])
    except Exception as e:
        logger.warning("Failed to log missing capability: %s", e)


def log_error(sender: str, raw_message: str, exc_type: str, exc_msg: str, tb: str) -> None:
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, raw_message, exc_type, exc_msg, traceback) VALUES (?,?,?,?,?)",
                (sender, raw_message[:500], exc_type, exc_msg[:500], tb[:3000]),
            )
    except Exception as e:
        logger.warning("Failed to log error to bot_log: %s", e)


_SESSION_ID: int | None = None


def start_session() -> int:
    """Insert a new bot_sessions row and cache its ID. Returns the row ID."""
    global _SESSION_ID
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO bot_sessions (messages_processed) VALUES (0)"
            )
            _SESSION_ID = cur.lastrowid
        logger.info("Session started: id=%d", _SESSION_ID)
        return _SESSION_ID
    except Exception as e:
        logger.error("Failed to start session: %s", e)
        return -1


def update_heartbeat() -> None:
    """Increment messages_processed and stamp last_heartbeat for the current session."""
    if not _SESSION_ID or _SESSION_ID < 0:
        return
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                """UPDATE bot_sessions
                   SET messages_processed = messages_processed + 1,
                       last_heartbeat = datetime('now')
                   WHERE id = ?""",
                (_SESSION_ID,),
            )
            conn.commit()
    except Exception as e:
        logger.warning("update_heartbeat failed: %s", e)


def touch_session_heartbeat() -> None:
    """Stamp last_heartbeat for liveness checks without incrementing messages."""
    if not _SESSION_ID or _SESSION_ID < 0:
        return
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                """UPDATE bot_sessions
                   SET last_heartbeat = datetime('now')
                   WHERE id = ?""",
                (_SESSION_ID,),
            )
            conn.commit()
    except Exception as e:
        logger.warning("touch_session_heartbeat failed: %s", e)


def log_session_error(error: str) -> None:
    """Record the most recent error string against the current session."""
    if not _SESSION_ID or _SESSION_ID < 0:
        return
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.execute(
                """UPDATE bot_sessions
                   SET last_error = ?,
                       last_error_at = datetime('now')
                   WHERE id = ?""",
                (error[:500], _SESSION_ID),
            )
    except Exception as e:
        logger.warning("log_session_error failed: %s", e)


def get_session_info() -> dict | None:
    """Return the current session row as a dict, or None if no session exists."""
    if not _SESSION_ID or _SESSION_ID < 0:
        return None
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM bot_sessions WHERE id = ?",
                (_SESSION_ID,),
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning("get_session_info failed: %s", e)
        return None


_RATE_LIMIT_MAX = 20
_RATE_LIMIT_WINDOW = "-1 hour"


def check_rate_limit(sender: str) -> bool:
    """Return True if sender may send a message, False if they are rate-limited.

    Owners bypass the check entirely. Everyone else is allowed up to
    _RATE_LIMIT_MAX messages per hour. An allowed message is logged immediately.
    On any DB error, fails open (returns True) so a DB hiccup doesn't silence
    the bot for all users.
    """
    from .permissions import is_owner
    if is_owner(sender):
        return True
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_log WHERE sender = ? AND timestamp >= datetime('now', ?)",
                (sender, _RATE_LIMIT_WINDOW),
            ).fetchone()
            count = row[0] if row else 0
            if count >= _RATE_LIMIT_MAX:
                logger.info("Rate-limited %s: %d/%d messages in last hour", sender, count, _RATE_LIMIT_MAX)
                return False
            conn.execute("INSERT INTO rate_limit_log (sender) VALUES (?)", (sender,))
        return True
    except Exception as e:
        logger.warning("Rate limit check failed for %s (failing open): %s", sender, e)
        return True


def cleanup_rate_limit_log() -> None:
    """Delete rate_limit_log rows older than 24 hours. Call once at startup."""
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            cur = conn.execute(
                "DELETE FROM rate_limit_log WHERE timestamp < datetime('now', '-24 hours')"
            )
        logger.info("Rate limit cleanup: removed %d expired row(s)", cur.rowcount)
    except Exception as e:
        logger.warning("Rate limit cleanup failed: %s", e)


def _log_bot_event(event_type: str, payload: dict | None = None) -> None:
    """Write a structured system event to bot_log."""
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                ("system", event_type, json.dumps(payload or {})),
            )
            conn.commit()
    except Exception as e:
        logger.warning("bot_log event failed for %s: %s", event_type, e)


def log_startup_event(event_type: str, payload: dict) -> None:
    """Write a structured startup event to bot_log."""
    _log_bot_event(event_type, payload)


_REMINDER_WORD_RE = re.compile(
    r"\b(?:reminders?|remind)\b",
    re.IGNORECASE,
)
_SCHEDULING_REMINDER_RE = re.compile(
    r"\b(?:remind\s+me|set\s+(?:a\s+)?reminder|add\s+(?:a\s+)?reminder|schedule\s+(?:a\s+)?reminder)\b"
    r"|(?:can\s+you|could\s+you)\s+remind\s+me\b",
    re.IGNORECASE,
)
_CANCEL_REMINDER_RE = re.compile(
    r"\b(?:cancel|delete|remove)\s+(?:(?:the|that|this|my|a)\s+)?reminders?\b",
    re.IGNORECASE,
)
_LIST_REMINDER_RE = re.compile(
    r"(?:\b(?:list|show|check|see|view|what|which|any|current|pending|open|active|do\s+i\s+have)\b.{0,80}\breminders?\b)"
    r"|(?:\breminders?\b.{0,80}\b(?:list|show|check|what|which|any|pending|open|active|current|left|do\s+i\s+have|are\s+what)\b)",
    re.IGNORECASE,
)
# Negative signal: reminder is broken/missed — only then do we intercept as casual.
# This prevents intercepting positive/neutral mentions like "the reminder worked great".
_REMINDER_NEGATIVE_RE = re.compile(
    r"\b(?:didn'?t|did\s+not|not\s+working|never|broke|broken|failed|missed|wrong|fucked\s+up|didn'?t\s+fire|didn'?t\s+go\s+off|didn'?t\s+work)\b",
    re.IGNORECASE,
)


_CRON_NOUN_RE = re.compile(
    r"\bcrons?\b|\bcron\s+jobs?\b|\bdaily\s+jobs?\b|\bscheduled\s+jobs?\b|"
    r"\bjobs?\b|\bautomations?\b|\brecurring\s+(?:jobs?|messages?|schedules?)\b",
    re.IGNORECASE,
)
_CRON_LIST_VERBS_RE = re.compile(
    r"\b(?:list|show|see|view|what(?:'s|\s+are)?|which|current|existing|any|"
    r"do\s+we\s+have|we\s+got|got\s+any|have\s+(?:we\s+got|any))\b",
    re.IGNORECASE,
)
_CRON_SCHEDULE_VERBS_RE = re.compile(
    r"\b(?:schedule|set\s+up|setup|add|create|new|make|start|every|daily\s+at|weekly\s+at|each\s+(?:morning|day|week))\b",
    re.IGNORECASE,
)
_CRON_CANCEL_VERBS_RE = re.compile(
    r"\b(?:cancel|delete|remove|kill|stop|drop|disable)\b",
    re.IGNORECASE,
)


def classify_cron_list_intent(text: str) -> bool:
    """Return True if the message is asking to LIST recurring/cron jobs.

    Catches phrasings the LLM sometimes answers conversationally instead of
    calling list_crons (e.g. "do we have any current cron jobs"). Only fires
    on listing intent — schedule/cancel phrasings pass through to the LLM.
    """
    if not text:
        return False
    if not _CRON_NOUN_RE.search(text):
        return False
    if _CRON_SCHEDULE_VERBS_RE.search(text):
        return False
    if _CRON_CANCEL_VERBS_RE.search(text):
        return False
    return bool(_CRON_LIST_VERBS_RE.search(text))


def classify_reminder_intent(text: str) -> str:
    """Classify reminder mentions into: 'schedule', 'cancel', 'list', 'casual', or 'none'.

    'casual' is ONLY returned when the message mentions a reminder AND contains
    a negative signal word — meaning the reminder is broken/missed. Neutral or
    positive mentions pass straight to the LLM.

    Should NOT trigger (no negative signal):
      - "the reminder worked great"
      - "good reminder, thanks"
      - "yeah the reminder fired on time"
      - "can you set a reminder? the last one was fine"
      - "reminder received!"
    Should trigger:
      - "the reminder didn't work"
      - "you missed the reminder"
      - "that reminder never fired"
    """
    lower = text.lower()
    if not _REMINDER_WORD_RE.search(lower):
        return "none"
    if _CANCEL_REMINDER_RE.search(lower):
        return "cancel"
    if _SCHEDULING_REMINDER_RE.search(lower):
        return "schedule"
    if _LIST_REMINDER_RE.search(lower):
        return "list"
    # Only intercept if there is ALSO a negative-failure signal.
    if _REMINDER_NEGATIVE_RE.search(lower):
        return "casual"
    return "none"  # neutral/positive mention ? pass to LLM


def check_action_permission(sender: str, action: str) -> str | None:
    """Return a refusal string if sender lacks permission for action, else None.

    Import and call this before executing any permission-gated operation.
    Returning non-None means the caller should send that string and stop.
    """
    from .permissions import can_user_do, OWNER_ONLY_ACTIONS
    if can_user_do(sender, action):
        return None
    if action in OWNER_ONLY_ACTIONS:
        return "That one's the owner-only — even admins can't touch it."
    return "You don't have permission for that."


_MAX_USER_MODEL_CHARS = 24000
_MAX_HISTORY_MODEL_CHARS = 18000
_MAX_HISTORY_TURN_CHARS = 3000
_MAX_OLLAMA_SYSTEM_CHARS = 8000
_MAX_OLLAMA_IDENTITY_CHARS = 5500
_MIN_OLLAMA_IDENTITY_CHARS = 3500
_MAX_OLLAMA_RULES_CHARS = 3200
_MIN_OLLAMA_RULES_CHARS = 2200
_MAX_OLLAMA_RELEVANT_FACTS_CHARS = 1800
_MIN_OLLAMA_RELEVANT_FACTS_CHARS = 1000
_MAX_OLLAMA_FACTS_CHARS = 2200
_MIN_OLLAMA_FACTS_CHARS = 500
_MAX_OLLAMA_HISTORY_CHARS = 2500
_MAX_OLLAMA_HISTORY_TURN_CHARS = 900
_SLOW_MODEL_CALL_SECONDS = 8.0


def _user_prompt_too_large(user_msg: str | None) -> bool:
    return len(user_msg or "") > _MAX_USER_MODEL_CHARS


def _clip_text_for_model(text: object, max_chars: int) -> str:
    raw = str(text or "")
    if len(raw) <= max_chars:
        return raw
    keep = max_chars - 46
    if keep <= 0:
        return raw[:max_chars]
    return "[older content truncated]\n" + raw[-keep:]


def _fit_history_for_model(
    history: list[dict],
    max_chars: int = _MAX_HISTORY_MODEL_CHARS,
    max_turn_chars: int = _MAX_HISTORY_TURN_CHARS,
) -> list[dict]:
    """Keep recent chat context within a bounded character budget."""
    fitted: list[dict] = []
    remaining = max_chars
    for turn in reversed(history or []):
        role = turn.get("role", "")
        if role not in {"user", "assistant", "model"}:
            continue
        content = _clip_text_for_model(turn.get("content", ""), max_turn_chars)
        if not content:
            continue
        if len(content) > remaining:
            if remaining <= 200:
                break
            content = _clip_text_for_model(content, remaining)
        fitted.append({"role": "assistant" if role == "model" else role, "content": content})
        remaining -= len(content)
        if remaining <= 0:
            break
    return list(reversed(fitted))


def _clip_middle_for_local_prompt(text: str, max_chars: int, label: str) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"\n\n[{label} compacted for local Ollama context]\n\n"
    keep = max_chars - len(marker)
    if keep <= 0:
        return text[:max_chars]
    head = max(1, keep // 2)
    tail = max(1, keep - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def _reduce_prompt_budget(budget: int, floor: int, overflow: int) -> tuple[int, int]:
    if overflow <= 0 or budget <= floor:
        return budget, overflow
    new_budget = max(floor, budget - overflow)
    return new_budget, overflow - (budget - new_budget)


def _split_ollama_system_sections(raw: str) -> tuple[str, str, str, str]:
    facts_marker = "\n\n## FACTS"
    relevant_marker = "\n\n## RELEVANT FACTS"
    rules_marker = "\n\n## Voice and boundaries"

    if facts_marker in raw:
        # The relevant-memory excerpt can itself contain markdown headings named
        # "## FACTS"; the full bulk-memory section is appended last.
        pre_facts, facts = raw.rsplit(facts_marker, 1)
        facts = facts_marker + facts
    else:
        pre_facts, facts = raw, ""

    if relevant_marker in pre_facts:
        pre_relevant, relevant = pre_facts.split(relevant_marker, 1)
        relevant = relevant_marker + relevant
    else:
        pre_relevant, relevant = pre_facts, ""

    if rules_marker in pre_relevant:
        identity, rules = pre_relevant.split(rules_marker, 1)
        rules = rules_marker + rules
    else:
        identity, rules = pre_relevant, ""

    return identity, rules, relevant, facts


def _fit_system_for_ollama(system: str) -> str:
    raw = str(system or "")
    if len(raw) <= _MAX_OLLAMA_SYSTEM_CHARS:
        return raw

    identity, rules, relevant, facts = _split_ollama_system_sections(raw)
    if rules or relevant or facts:
        identity_budget = min(len(identity), _MAX_OLLAMA_IDENTITY_CHARS)
        rules_budget = min(len(rules), _MAX_OLLAMA_RULES_CHARS)
        relevant_budget = min(len(relevant), _MAX_OLLAMA_RELEVANT_FACTS_CHARS)
        facts_budget = min(len(facts), _MAX_OLLAMA_FACTS_CHARS)

        separator_budget = 3 * 2
        total_budget = identity_budget + rules_budget + relevant_budget + facts_budget + separator_budget
        overflow = max(0, total_budget - _MAX_OLLAMA_SYSTEM_CHARS)
        facts_budget, overflow = _reduce_prompt_budget(
            facts_budget,
            min(len(facts), _MIN_OLLAMA_FACTS_CHARS),
            overflow,
        )
        rules_budget, overflow = _reduce_prompt_budget(
            rules_budget,
            min(len(rules), _MIN_OLLAMA_RULES_CHARS),
            overflow,
        )
        identity_budget, overflow = _reduce_prompt_budget(
            identity_budget,
            min(len(identity), _MIN_OLLAMA_IDENTITY_CHARS),
            overflow,
        )
        relevant_budget, overflow = _reduce_prompt_budget(
            relevant_budget,
            min(len(relevant), _MIN_OLLAMA_RELEVANT_FACTS_CHARS),
            overflow,
        )
        if overflow > 0:
            facts_budget, overflow = _reduce_prompt_budget(facts_budget, 0, overflow)
            rules_budget, overflow = _reduce_prompt_budget(rules_budget, 0, overflow)
            identity_budget, overflow = _reduce_prompt_budget(identity_budget, 0, overflow)
            relevant_budget, overflow = _reduce_prompt_budget(relevant_budget, 0, overflow)

        parts = [
            _clip_middle_for_local_prompt(identity, identity_budget, "active persona/system identity"),
            _clip_middle_for_local_prompt(rules, rules_budget, "core behavior rules"),
            _clip_middle_for_local_prompt(relevant, relevant_budget, "relevant durable facts"),
            _clip_middle_for_local_prompt(facts, facts_budget, "durable facts"),
        ]
        fitted = "\n".join(part.strip() for part in parts if part.strip())
    else:
        fitted = _clip_middle_for_local_prompt(raw, _MAX_OLLAMA_SYSTEM_CHARS, "system prompt")

    if len(fitted) > _MAX_OLLAMA_SYSTEM_CHARS:
        fitted = _clip_middle_for_local_prompt(fitted, _MAX_OLLAMA_SYSTEM_CHARS, "system prompt")
    logger.info("Compacted Ollama system prompt from %d to %d chars", len(raw), len(fitted))
    return fitted


def _is_large_prompt_error(status: object, body: str) -> bool:
    lower = (body or "").lower()
    return (
        str(status) == "400"
        and (
            "context_length_exceeded" in lower
            or "too many" in lower
            or "token" in lower
            or "contents" in lower
            or "parts" in lower
        )
    )


def _call_ollama(
    system: str,
    history: list[dict],
    user_msg: str,
    num_predict: int | None = None,
    temperature: float | None = None,
    empty_fallback: str | None = None,
    timeout: float | None = None,
    model: str | None = None,
) -> str | None:
    started = time.monotonic()
    request_timeout = timeout if timeout and timeout > 0 else OLLAMA_TIMEOUT
    model_name = (model or OLLAMA_MODEL or "").strip() or OLLAMA_MODEL
    system = _fit_system_for_ollama(system)
    history = _fit_history_for_model(
        history,
        max_chars=_MAX_OLLAMA_HISTORY_CHARS,
        max_turn_chars=_MAX_OLLAMA_HISTORY_TURN_CHARS,
    )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    for turn in history:
        messages.append(turn)
    messages.append({"role": "user", "content": user_msg})

    resp = None
    try:
        payload = {"model": model_name, "messages": messages, "stream": False}
        keep_alive = _ollama_keep_alive_value()
        if keep_alive:
            payload["keep_alive"] = keep_alive
        options = {}
        if OLLAMA_NUM_CTX > 0:
            options["num_ctx"] = OLLAMA_NUM_CTX
        if num_predict and num_predict > 0:
            options["num_predict"] = int(num_predict)
            if model_name.partition(":")[0].lower() == "gemma4":
                # Keep short replies from exhausting their budget before final output.
                payload["think"] = False
        if temperature is not None and temperature >= 0:
            options["temperature"] = float(temperature)
        if options:
            payload["options"] = options
        resp = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json=payload,
            timeout=request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        elapsed = time.monotonic() - started
        logger.warning("Ollama timed out after %.1fs model=%s elapsed=%.2fs", request_timeout, model_name, elapsed)
        if empty_fallback is not None:
            _log_bot_event(
                "ollama_simple_timeout",
                {"model": model_name, "timeout_seconds": request_timeout, "elapsed_seconds": round(elapsed, 3)},
            )
            return empty_fallback
        return None
    except requests.exceptions.ConnectionError as e:
        logger.warning("Ollama connection refused — attempting restart: %s", e)
        _try_restart_ollama()
        return None
    except requests.exceptions.HTTPError as e:
        logger.warning("Ollama HTTP %s after %.2fs: %s", e.response.status_code if e.response is not None else "?", time.monotonic() - started, e)
        return None
    except Exception as e:
        logger.warning("Ollama request failed after %.2fs (%s): %s", time.monotonic() - started, type(e).__name__, e)
        return None
    finally:
        if resp is not None:
            _close_response(resp)

    try:
        content = data["message"]["content"].strip()
        elapsed = time.monotonic() - started
        log = logger.warning if elapsed >= _SLOW_MODEL_CALL_SECONDS else logger.info
        log(
            "Ollama chat completed in %.2fs model=%s prompt_chars=%d history_turns=%d",
            elapsed,
            model_name,
            len(system),
            len(history),
        )
        if content:
            return content
        logger.warning("Ollama returned an empty chat reply after %.2fs model=%s", elapsed, model_name)
        return empty_fallback
    except (KeyError, IndexError) as e:
        logger.warning("Ollama response parse error (missing %s): %s", e, str(resp.text)[:200])
        return None


def _ollama_model_available(data: dict, model: str | None = None) -> bool:
    wanted = (model or OLLAMA_MODEL or "").strip()
    if not wanted:
        return True
    models = data.get("models")
    if not isinstance(models, list):
        return True
    names = {
        str(row.get("name") or row.get("model") or "").strip()
        for row in models
        if isinstance(row, dict)
    }
    if not names:
        return True
    return any(name == wanted or name.startswith(f"{wanted}:") for name in names)


def _ollama_keep_alive_value() -> str | None:
    value = str(OLLAMA_KEEP_ALIVE or "").strip()
    return value or None


def _ollama_simple_chat_model() -> str:
    return (OLLAMA_SIMPLE_CHAT_MODEL or OLLAMA_MODEL or "").strip() or OLLAMA_MODEL


def _ollama_keep_warm_models() -> list[str]:
    models = []
    for candidate in (OLLAMA_MODEL, _ollama_simple_chat_model()):
        clean = (candidate or "").strip()
        if clean and clean not in models:
            models.append(clean)
    return models


def _ollama_probe_payload(realistic: bool = False, model: str | None = None) -> dict:
    messages = [{"role": "user", "content": "Reply with exactly one word: pong"}]
    num_predict = 1
    if realistic:
        messages = [
            {"role": "system", "content": _OLLAMA_RECOVERY_PROBE_SYSTEM},
            {"role": "user", "content": "Say one short dinner-planning sentence."},
        ]
        num_predict = OLLAMA_SIMPLE_CHAT_NUM_PREDICT if OLLAMA_SIMPLE_CHAT_NUM_PREDICT > 0 else 64
    payload = {
        "model": (model or OLLAMA_MODEL or "").strip() or OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
    }
    if payload["model"].partition(":")[0].lower() == "gemma4":
        payload["think"] = False
    keep_alive = _ollama_keep_alive_value()
    if keep_alive:
        payload["keep_alive"] = keep_alive
    options = {"num_predict": num_predict, "temperature": 0}
    if OLLAMA_NUM_CTX > 0:
        options["num_ctx"] = OLLAMA_NUM_CTX
    payload["options"] = options
    return payload


def _ollama_generation_available(model: str | None = None) -> bool:
    resp = None
    timeout = OLLAMA_SIMPLE_CHAT_TIMEOUT if OLLAMA_SIMPLE_CHAT_TIMEOUT > 0 else OLLAMA_HEALTH_TIMEOUT
    try:
        resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=_ollama_probe_payload(model=model), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        content = str((data.get("message") or {}).get("content") or "").strip()
        if content:
            return True
        logger.warning("Ollama recovery generation probe returned empty content")
        return False
    except requests.exceptions.Timeout:
        logger.warning("Ollama recovery generation probe timed out after %.1fs", timeout)
        return False
    except requests.exceptions.ConnectionError as e:
        logger.warning("Ollama recovery generation probe connection refused - attempting restart: %s", e)
        _try_restart_ollama()
        return False
    except requests.exceptions.HTTPError as e:
        logger.warning(
            "Ollama recovery generation probe HTTP %s: %s",
            e.response.status_code if e.response is not None else "?",
            e,
        )
        return False
    except Exception as e:
        logger.warning("Ollama recovery generation probe failed (%s): %s", type(e).__name__, e)
        return False
    finally:
        if resp is not None:
            _close_response(resp)


def _ollama_tags_available() -> bool:
    resp = None
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=OLLAMA_HEALTH_TIMEOUT)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            return True
        return _ollama_model_available(data)
    except requests.exceptions.ConnectionError as e:
        logger.warning("Ollama tags check connection refused - attempting restart: %s", e)
        _try_restart_ollama()
        return False
    except requests.exceptions.Timeout:
        logger.warning("Ollama tags check timed out after %ds", OLLAMA_HEALTH_TIMEOUT)
        return False
    except requests.exceptions.HTTPError as e:
        logger.warning(
            "Ollama tags check HTTP %s: %s",
            e.response.status_code if e.response is not None else "?",
            e,
        )
        return False
    except Exception as e:
        logger.warning("Ollama tags check failed (%s): %s", type(e).__name__, e)
        return False
    finally:
        if resp is not None:
            _close_response(resp)


def _mark_ollama_down_after_direct_miss(simple_chat: bool = False) -> None:
    if simple_chat:
        try:
            if _ollama_tags_available():
                logger.info("Ollama simple chat missed, but tags check passed; keeping local route available")
                return
        except Exception as e:
            logger.warning("Ollama post-miss tags check failed (%s): %s", type(e).__name__, e)
    _mark_ollama_down(notify=False)


def warm_ollama_model(source: str = "manual") -> bool:
    """Load configured Ollama chat models without blocking the message loop."""
    ok = True
    for model_name in _ollama_keep_warm_models():
        ok = _warm_single_ollama_model(model_name, source=source) and ok
    return ok


def _warm_single_ollama_model(model_name: str, source: str = "manual") -> bool:
    resp = None
    timeout = OLLAMA_KEEP_WARM_TIMEOUT if OLLAMA_KEEP_WARM_TIMEOUT > 0 else OLLAMA_TIMEOUT
    started = time.monotonic()
    try:
        resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=_ollama_probe_payload(model=model_name), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        content = str((data.get("message") or {}).get("content") or "").strip()
        elapsed = time.monotonic() - started
        if not content:
            logger.warning(
                "Ollama keep-warm returned empty content after %.2fs model=%s source=%s",
                elapsed,
                model_name,
                source,
            )
            return False
        logger.info(
            "Ollama keep-warm completed in %.2fs model=%s keep_alive=%s source=%s",
            elapsed,
            model_name,
            _ollama_keep_alive_value() or "<default>",
            source,
        )
        return True
    except requests.exceptions.Timeout:
        logger.warning("Ollama keep-warm timed out after %.1fs model=%s source=%s", timeout, model_name, source)
        return False
    except requests.exceptions.ConnectionError as e:
        logger.warning("Ollama keep-warm connection refused - attempting restart: %s", e)
        _try_restart_ollama()
        return False
    except requests.exceptions.HTTPError as e:
        logger.warning(
            "Ollama keep-warm HTTP %s model=%s source=%s: %s",
            e.response.status_code if e.response is not None else "?",
            model_name,
            source,
            e,
        )
        return False
    except Exception as e:
        logger.warning("Ollama keep-warm failed (%s) model=%s source=%s: %s", type(e).__name__, model_name, source, e)
        return False
    finally:
        if resp is not None:
            _close_response(resp)


def _ollama_keep_warm_loop(interval: float) -> None:
    source = "startup"
    while True:
        if not _ollama_tags_available():
            _mark_ollama_down(notify=False)
        warm_ollama_model(source=source)
        source = "interval"
        time.sleep(interval)


def start_ollama_keep_warm_thread() -> bool:
    """Start a single background loop that keeps the configured local model resident."""
    global _ollama_keep_warm_thread_started
    if _ollama_keep_warm_thread_started or not OLLAMA_KEEP_WARM_ENABLED:
        return False
    interval = max(60.0, float(OLLAMA_KEEP_WARM_INTERVAL_SECONDS or 0))
    thread = threading.Thread(
        target=_ollama_keep_warm_loop,
        args=(interval,),
        name="ollama-keep-warm",
        daemon=True,
    )
    thread.start()
    _ollama_keep_warm_thread_started = True
    logger.info(
        "Started Ollama keep-warm thread models=%s keep_alive=%s interval=%.0fs",
        ",".join(_ollama_keep_warm_models()),
        _ollama_keep_alive_value() or "<default>",
        interval,
    )
    return True


def _log_ollama_state(event_type: str) -> None:
    _log_bot_event(
        event_type,
        {
            "host": redact_secret(OLLAMA_HOST),
            "model": OLLAMA_MODEL,
            "simple_chat_model": _ollama_simple_chat_model(),
            "check_interval_seconds": OLLAMA_CHECK_INTERVAL,
        },
    )


def _latest_ollama_state() -> str | None:
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            row = conn.execute(
                """
                SELECT event_type
                FROM bot_log
                WHERE event_type IN ('ollama_down', 'ollama_recovered')
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.warning("Failed to read latest Ollama state: %s", e)
        return None


def initialize_ollama_recovery_state() -> str | None:
    """Restore pending Ollama recovery monitoring after a process restart."""
    global _ollama_down, _last_ollama_check, _ollama_down_alerted
    state = _latest_ollama_state()
    if state == "ollama_down":
        _ollama_down = True
        _last_ollama_check = 0.0
        _ollama_down_alerted = False
        logger.info("Restored Ollama down state from bot_log; recovery monitor active")
    elif state == "ollama_recovered":
        _ollama_down = False
        _last_ollama_check = 0.0
        _ollama_down_alerted = False
    return state


def _ollama_health_check() -> bool:
    resp = None
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=OLLAMA_HEALTH_TIMEOUT)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            return True
        if _ollama_model_available(data):
            return _ollama_generation_available()
        logger.warning("Ollama health check up but model %s is not listed", OLLAMA_MODEL)
        return False
    except requests.exceptions.Timeout:
        logger.warning("Ollama health check timed out after %ds", OLLAMA_HEALTH_TIMEOUT)
        return False
    except requests.exceptions.ConnectionError as e:
        logger.warning("Ollama health check connection refused - attempting restart: %s", e)
        _try_restart_ollama()
        return False
    except requests.exceptions.HTTPError as e:
        logger.warning(
            "Ollama health check HTTP %s: %s",
            e.response.status_code if e.response is not None else "?",
            e,
        )
        return False
    except Exception as e:
        logger.warning("Ollama health check failed (%s): %s", type(e).__name__, e)
        return False
    finally:
        if resp is not None:
            _close_response(resp)


def _mark_ollama_down(notify: bool = False) -> None:
    global _ollama_down, _last_ollama_check, _ollama_down_alerted, _ollama_state_epoch
    with MODEL_STATE_LOCK:
        already_down = _ollama_down
        _ollama_down = True
        _ollama_state_epoch += 1
        if not already_down:
            _last_ollama_check = time.time()
            _log_ollama_state("ollama_down")
        should_notify = notify and not _ollama_down_alerted
        if should_notify:
            _ollama_down_alerted = True
    if should_notify:
        _notify_owner(
            "Model fallback degraded: Ollama missed and Gemini also returned no reply. Check the Mac Mini.",
            "model_fallback_failed",
        )


def check_ollama_recovery(now: float | None = None) -> bool:
    """Poll Ollama while in fallback mode and only notify recovery after an escalated outage."""
    global _ollama_down, _last_ollama_check, _ollama_down_alerted
    now = time.time() if now is None else now
    with MODEL_STATE_LOCK:
        if not _ollama_down or now - _last_ollama_check < OLLAMA_CHECK_INTERVAL:
            return False
        _last_ollama_check = now
        probe_epoch = _ollama_state_epoch
    if not _ollama_health_check():
        return False
    with MODEL_STATE_LOCK:
        # A newer handler failure makes this completed probe stale.
        if probe_epoch != _ollama_state_epoch or not _ollama_down:
            return False
        was_alerted = _ollama_down_alerted
        _ollama_down = False
        _ollama_down_alerted = False
        _log_ollama_state("ollama_recovered")
    if was_alerted:
        _notify_owner("Ollama is back online; model fallback recovered.", "ollama_recovered")
    return True


def _gemini_generate_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _call_gemini(
    system: str,
    history: list[dict],
    user_msg: str,
    image_path: str | None = None,
    model: str | None = None,
    source: str = "direct",
) -> str | None:
    started = time.monotonic()
    if not GEMINI_API_KEY:
        logger.error("Gemini unavailable — GEMINI_API_KEY is not set")
        return None
    budget = check_gemini_budget(source)
    if not budget.allowed:
        logger.warning("Gemini %s blocked by budget guard: %s", source, budget.reason)
        return None
    target_model = (model or GEMINI_MODEL).strip() or GEMINI_MODEL

    # Build contents list for Gemini
    history = _fit_history_for_model(history)
    contents = []
    for turn in history:
        role = "user" if turn["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    user_parts = [{"text": user_msg}] if user_msg else []
    if image_path:
        part = _image_part(image_path)
        if part:
            user_parts.append(part)
    contents.append({"role": "user", "parts": user_parts})

    payload = {
        "system_instruction": {"parts": [{"text": system}]} if system else None,
        "contents": contents,
    }
    if not system:
        del payload["system_instruction"]

    resp = None
    try:
        resp = requests.post(
            GEMINI_URL if target_model == GEMINI_MODEL else _gemini_generate_url(target_model),
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        logger.error("Gemini timed out after 30s")
        return None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        body = (e.response.text if e.response is not None else "")[:300]
        if status == 429:
            logger.error("Gemini rate limited (429) — daily quota exceeded")
        elif status == 401:
            logger.error("Gemini auth failed (401) — GEMINI_API_KEY is invalid or expired")
        elif status == 503:
            logger.error("Gemini service unavailable (503) — Google outage")
        elif _is_large_prompt_error(status, body):
            logger.error("Gemini 400 context length: %s", redact_secret(body))
            return _LARGE_PROMPT_SENTINEL
        else:
            logger.error("Gemini HTTP %s: %s", status, redact_secret(str(e)))
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error("Gemini connection failed — check network: %s", redact_secret(str(e)))
        return None
    except Exception as e:
        logger.error("Gemini request failed (%s): %s", type(e).__name__, redact_secret(str(e)))
        return None
    finally:
        if resp is not None:
            _close_response(resp)

    try:
        usage = data.get("usageMetadata", {})
        _log_gemini_usage(
            usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0),
            usage.get("totalTokenCount", 0),
            source,
        )
        elapsed = time.monotonic() - started
        log = logger.warning if elapsed >= _SLOW_MODEL_CALL_SECONDS else logger.info
        log("Gemini call completed in %.2fs model=%s source=%s image=%s", elapsed, target_model, source, bool(image_path))
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text or None  # don't return empty string
    except (KeyError, IndexError) as e:
        logger.error("Gemini response parse error (missing %s): %s", e, redact_secret(str(data)[:300]))
        return None


_LARGE_PROMPT_SENTINEL = "__LARGE_PROMPT__"
_LARGE_PROMPT_SASS = [
    "That is too much for one model pass. Send `big change [ask]` or `fix yourself: [issue]`, and I will turn it into a guarded Codex handoff.",
    "I need that split or logged as an intake. Use `big change [ask]` for setup work or `fix yourself: [issue]` for repair work.",
    "Too large for a single model call. Paste the core ask after `big change` or `fix yourself:` so I can make the Codex handoff clean.",
]


def _harmless_roast_fallback(user_msg: str) -> str | None:
    return _failure_copy.harmless_roast_fallback(user_msg)


_BLAND_SIMPLE_CHAT_RE = _simple_chat.BLAND_SIMPLE_CHAT_RE


def _simple_chat_personality_fallback(user_msg: str) -> str:
    return _simple_chat.simple_chat_personality_fallback(user_msg)


def _simple_chat_empty_fallback(user_msg: str) -> str:
    return _simple_chat.simple_chat_empty_fallback(user_msg)


def _polish_simple_chat_reply(user_msg: str, reply: str | None) -> str | None:
    return _simple_chat.polish_simple_chat_reply(user_msg, reply)


def _maybe_sass_large_prompt(reply: str | None) -> str | None:
    if reply == _LARGE_PROMPT_SENTINEL:
        import random as _r
        return _r.choice(_LARGE_PROMPT_SASS)
    return reply


def _oversized_prompt_risk(user_msg: str) -> str:
    lower = (user_msg or "").lower()
    if re.search(
        r"\b(permission|admin|password|private\s+(?:send|message|text)|send_imessage|"
        r"memory|soul|schema|migration|database|db\s+schema|tool\s+gate|owner[-\s]?only|"
        r"write_file|shell_exec|deploy|self[-\s]?edit|auto[-\s]?push|cron|reminder)\b",
        lower,
    ):
        return "RED"
    return "YELLOW"


def _oversized_prompt_preview(user_msg: str, max_chars: int = 1800) -> str:
    safe = redact_secret(user_msg or "")
    safe = re.sub(r"\s+", " ", safe).strip()
    if len(safe) > max_chars:
        return safe[:max_chars].rstrip() + "..."
    return safe


def _log_oversized_owner_prompt_intake(sender: str, user_msg: str) -> str | None:
    if not sender or sender != OWNER_ID:
        return None
    preview = _oversized_prompt_preview(user_msg)
    if not preview:
        preview = "Owner sent an oversized message with no usable text preview."
    risk = _oversized_prompt_risk(user_msg)
    summary = preview[:180].rstrip()
    request = f"[OVERSIZED-INTAKE {risk}] {summary}"
    reason = "\n".join([
        "type=oversized_owner_prompt_intake",
        f"risk={risk}",
        "status=review_only",
        f"message_len={len(user_msg or '')}",
        f"message_preview={preview}",
        "expected_bot_behavior=never pretend an oversized request was handled; capture a durable Codex setup/repair handoff and ask for split context when needed",
        "safe_auto_fix_pipeline=Codex only: create a codex/... branch/worktree, patch, test, push, wait for CI, then Mini deploy/smoke; Davos must not edit production directly.",
        "blocked_actions=no live self-edit, no deploy, no shell/file/DB mutation outside change_log",
        "codex_prompt=Inspect this oversized owner request preview. If context is missing, ask the owner for the specific missing artifact. Otherwise create the smallest safe patch plan, tests, validation, and rollback. Keep side effects deterministic and permission gates unchanged.",
    ])
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            cur = conn.execute(
                "INSERT INTO change_log (request, reason) VALUES (?, ?)",
                (request, reason),
            )
            row_id = cur.lastrowid
            conn.commit()
    except Exception as exc:
        logger.warning("oversized owner prompt intake failed: %s", type(exc).__name__)
        return None
    return (
        f"Logged oversized Codex intake #{row_id} [{risk}].\n"
        "That message was too large for one model pass, so I captured a review-only setup/repair handoff instead of dropping it.\n"
        "Next: text `ship safe cleanup` for the Codex board."
    )


def _humanize_transient_error(reply: str | None) -> str | None:
    return _failure_copy.humanize_transient_error(reply)


def _notify_owner(message: str, event_type: str = "owner_notice") -> None:
    send_owner_alert(event_type, message)
    if OWNER_ID:
        send_message(OWNER_ID, message)


def _try_restart_ollama() -> None:
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Ollama restart attempted")
    except Exception as e:
        logger.warning("Ollama restart failed: %s", e)


def get_structured_response(prompt: str, source: str = "structured") -> str | None:
    """For structured tasks like memory extraction — goes straight to Gemini, skips Ollama."""
    return _call_gemini("", [], prompt, source=source)


def _record_agentic_usage(data: object) -> None:
    """Record reported counters once, without treating missing usage as free."""
    usage = data.get("usageMetadata") if isinstance(data, dict) else None
    fields = ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
    counters = {
        field: usage[field] for field in fields
        if isinstance(usage, dict) and type(usage.get(field)) is int and usage[field] >= 0
    }
    if not counters:
        logger.warning("Gemini agentic usage unreported: metadata_type=%s valid_counters=0/3",
                       type(usage).__name__)
        return
    complete = len(counters) == len(fields)
    if not complete:
        logger.warning("Gemini agentic usage partial: valid_fields=%s; missing counters are lower bounds",
                       ",".join(counters))
    _log_gemini_usage(
        *(counters.get(field, 0) for field in fields),
        "agentic" if complete else "agentic_partial",
    )


def _call_gemini_agentic(
    system: str,
    history: list[dict],
    user_msg: str,
    image_path: str | None = None,
    allowed_tools: list[str] | None = None,
    on_tool_call: object = None,
    sender: str = "",
    originating_chat_id: str = "",
) -> str | None:
    """Gemini with function calling — tool-use agent loop.

    allowed_tools: if set, restricts which tools are exposed to the model.
    on_tool_call: optional callable(tool_name) fired before each tool execution.
    """
    from .tools import execute_tool_outcome, TOOL_DEFINITIONS
    from .tool_outcomes import OUTCOME_INSTRUCTION, ToolOutcome, ToolTrace

    budget = check_gemini_budget("agentic")
    if not budget.allowed:
        logger.warning("Gemini agentic blocked by budget guard: %s", budget.reason)
        return None

    tool_defs = (
        [t for t in TOOL_DEFINITIONS if t["name"] in allowed_tools]
        if allowed_tools is not None
        else TOOL_DEFINITIONS
    )
    if not tool_defs:
        return _call_gemini(system, history, user_msg, image_path=image_path)
    available_tool_names = frozenset(tool["name"] for tool in tool_defs)

    history = _fit_history_for_model(history)
    contents = []
    for turn in history:
        role = "user" if turn["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    user_parts = [{"text": user_msg}] if user_msg else []
    if image_path:
        part = _image_part(image_path)
        if part:
            user_parts.append(part)
    contents.append({"role": "user", "parts": user_parts})

    payload = {
        "contents": contents,
        "tools": [{"functionDeclarations": tool_defs}],
    }
    instructions = (system + "\n\n" if system else "") + OUTCOME_INSTRUCTION
    payload["system_instruction"] = {"parts": [{"text": instructions}]}

    # Record all outcomes, including denied, partial, and unverified actions.
    # No completed action is inferred from a helper's free-form result text.
    trace = ToolTrace()
    declared_tool_names = {tool["name"] for tool in TOOL_DEFINITIONS}

    for iteration in range(10):
        # Retry transient Gemini errors (503 Service Unavailable, 429 rate limit,
        # connection drops). Exponential backoff: 1s, 3s, 7s.
        resp = None
        last_err = None
        for retry_idx, backoff_s in enumerate([0, 1, 3, 7]):
            if iteration or retry_idx:
                budget = check_gemini_budget("agentic")
                if not budget.allowed:
                    logger.warning("Gemini agentic continuation blocked by budget guard: %s", budget.reason)
                    return trace.reply()
            if backoff_s:
                import time as _time
                _time.sleep(backoff_s)
            try:
                resp = requests.post(
                    GEMINI_URL,
                    params={"key": GEMINI_API_KEY},
                    json=payload,
                    timeout=60,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {resp.status_code}"
                    logger.warning("Gemini transient %s (retry %d/3)", last_err, retry_idx)
                    _close_response(resp)
                    resp = None
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else "unknown"
                body = (e.response.text if e.response is not None else "")[:300]
                logger.error("Gemini agentic HTTP %s: %s", status, redact_secret(str(e)))
                if resp is not None:
                    _close_response(resp)
                    resp = None
                if trace.receipts:
                    return trace.reply()
                if _is_large_prompt_error(status, body):
                    logger.error("Gemini agentic prompt too large: %s", redact_secret(body))
                    return _LARGE_PROMPT_SENTINEL
                return f"__transient_error__:HTTP {status}"
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_err = type(e).__name__
                logger.warning("Gemini transient %s (retry %d/3): %s", last_err, retry_idx, redact_secret(str(e)))
                if resp is not None:
                    _close_response(resp)
                resp = None
                continue
        if resp is None:
            logger.error("Gemini agentic gave up after retries: %s", last_err)
            if trace.receipts:
                return trace.reply()
            return f"__transient_error__:{last_err}"

        data = None
        try:
            data = resp.json()
            _record_agentic_usage(data)
            candidate = data["candidates"][0]
            content = candidate["content"]

            function_calls = [p for p in content["parts"] if "functionCall" in p]
            text_parts = [p for p in content["parts"] if "text" in p]

            if function_calls:
                contents.append(content)
                function_responses = []
                for part in function_calls:
                    fc = part["functionCall"]
                    name = fc["name"]
                    args = fc.get("args", {})
                    # Capture the canonical arguments before callbacks or helpers
                    # can mutate them. This journal lasts only for this turn.
                    key = trace.invocation_key(name, args)
                    # Model output is untrusted. Advertising a restricted schema
                    # does not authorize a different tool returned by the model.
                    if name not in available_tool_names:
                        logger.warning("Rejected unadvertised tool: %s", name if name in declared_tool_names else "unknown_tool")
                        receipt = trace.record(name, ToolOutcome(
                            "denied", f"Permission denied: {name} is not available for this request.",
                            "advertised_tools", error="unavailable_tool",
                        ), key)
                    else:
                        previous = trace.previous(key)
                        if previous is not None:
                            receipt = trace.reuse(previous)
                        else:
                            try:
                                if on_tool_call:
                                    on_tool_call(name)
                                outcome = execute_tool_outcome(
                                    name, args, sender=sender, originating_chat_id=originating_chat_id,
                                )
                            except Exception as exc:
                                # Execution may have had an effect before raising.
                                # Retain the uncertainty and never replay it here.
                                outcome = ToolOutcome("unverified", "Tool execution could not be verified.",
                                                      "execution_exception", error=type(exc).__name__)
                            receipt = trace.record(name, outcome, key)
                    logger.info("Tool %s status=%s scope=%s %s",
                                name if name in declared_tool_names else "unknown_tool",
                                receipt.outcome.status, receipt.outcome.verification_scope,
                                _safe_tool_result_for_log(name, receipt.outcome.text))
                    function_responses.append({
                        "functionResponse": {
                            "name": name,
                            "response": receipt.response(),
                        }
                    })
                contents.append({"role": "user", "parts": function_responses})
                payload["contents"] = contents

            elif text_parts:
                text = text_parts[0]["text"].strip()
                return trace.reply(text)
            else:
                usage = data.get("usageMetadata", {}) if isinstance(data, dict) else {}
                parts = content.get("parts", []) if isinstance(content, dict) else []
                logger.error(
                    "Gemini agentic iteration %d returned no text and no tool calls; "
                    "finish_reason=%s tool_count=%d prompt_tokens=%s parts=%d",
                    iteration,
                    candidate.get("finishReason", "?"),
                    len(tool_defs),
                    usage.get("promptTokenCount", "?"),
                    len(parts or []),
                )
                return trace.reply()

        except requests.exceptions.Timeout:
            logger.error("Gemini agentic timed out on iteration %d", iteration)
            return trace.reply()
        except (KeyError, IndexError) as e:
            # Common when Gemini returns a candidate with no `parts` (safety filter,
            # MAX_TOKENS, or partial response). Keep any outcomes already received.
            finish_reason = "?"
            prompt_tokens = "?"
            parts_count = "?"
            try:
                if isinstance(data, dict):
                    candidate_meta = (data.get("candidates") or [{}])[0]
                    finish_reason = candidate_meta.get("finishReason", "?")
                    content_meta = candidate_meta.get("content") or {}
                    parts_meta = content_meta.get("parts") if isinstance(content_meta, dict) else None
                    parts_count = len(parts_meta or [])
                    prompt_tokens = (data.get("usageMetadata") or {}).get("promptTokenCount", "?")
            except Exception:
                pass
            logger.error(
                "Gemini agentic parse error on iteration %d (missing %s); "
                "finish_reason=%s tool_count=%d prompt_tokens=%s parts=%s",
                iteration,
                e,
                finish_reason,
                len(tool_defs),
                prompt_tokens,
                parts_count,
            )
            return trace.reply()
        except Exception as e:
            logger.error("Gemini agentic error on iteration %d (%s): %s", iteration, type(e).__name__, redact_secret(str(e)))
            return trace.reply()
        finally:
            _close_response(resp)

    logger.error("Gemini agentic loop hit 10-iteration limit without a final text response")
    return trace.reply()


def _safe_tool_result_for_log(tool_name: str, result: object) -> str:
    return f"[result content omitted; text_chars={len(result) if isinstance(result, str) else 0}]"


def _model_route_parts(route: str) -> tuple[str, str]:
    clean = (route or "").strip()
    if ":" not in clean:
        return "", clean
    provider, model = clean.split(":", 1)
    return provider.strip().lower(), model.strip()


def _callable_gemini_model(route: str) -> str | None:
    provider, model = _model_route_parts(route)
    if provider == "gemini" and model:
        return model
    if not provider and model.lower().startswith("gemini"):
        return model
    return None


def _owner_advanced_direct_route(user_msg: str, sender: str = "") -> tuple[str, str] | None:
    """Return (route_name, gemini_model) for owner-only direct advanced chat."""
    if not OWNER_ID or sender != OWNER_ID:
        return None
    text = user_msg or ""
    if _CODE_REVIEW_RE.search(text):
        code_model = _callable_gemini_model(MODEL_ROUTE_CODE_REVIEW)
        if code_model:
            return "code_review", code_model
        complex_model = _callable_gemini_model(MODEL_ROUTE_COMPLEX_REASONING)
        if complex_model:
            return "complex_reasoning", complex_model
    if _COMPLEX_REASONING_RE.search(text):
        complex_model = _callable_gemini_model(MODEL_ROUTE_COMPLEX_REASONING)
        if complex_model:
            return "complex_reasoning", complex_model
    return None


def _simple_chat_direct_route() -> tuple[str, str] | None:
    """Return (route_name, gemini_model) when simple chat is configured for Gemini."""
    model = _callable_gemini_model(MODEL_ROUTE_SIMPLE_CHAT)
    if model:
        return "simple_chat", model
    return None


def get_response(
    system: str,
    history: list[dict],
    user_msg: str,
    use_tools: bool = False,
    image_path: str | None = None,
    allowed_tools: list[str] | None = None,
    on_tool_call: object = None,
    sender: str = "",
    originating_chat_id: str = "",
    simple_chat: bool = False,
) -> str:
    global _ollama_down, _last_ollama_check

    if _user_prompt_too_large(user_msg):
        logger.info("Rejected oversized prompt before model call: %d chars", len(user_msg or ""))
        intake_reply = _log_oversized_owner_prompt_intake(sender, user_msg or "")
        if intake_reply:
            return intake_reply
        return _maybe_sass_large_prompt(_LARGE_PROMPT_SENTINEL) or _LARGE_PROMPT_SASS[0]

    if image_path:
        if use_tools:
            reply = _call_gemini_agentic(system, history, user_msg, image_path=image_path, on_tool_call=on_tool_call, sender=sender, originating_chat_id=originating_chat_id)
        elif allowed_tools:
            reply = _call_gemini_agentic(system, history, user_msg, image_path=image_path, allowed_tools=allowed_tools, on_tool_call=on_tool_call, sender=sender, originating_chat_id=originating_chat_id)
        else:
            reply = _call_gemini(system, history, user_msg, image_path=image_path)
        reply = _humanize_transient_error(reply)
        reply = _maybe_sass_large_prompt(reply)
        return reply if reply else _failure_copy.IMAGE_PROCESSING_FAILURE_REPLY

    if use_tools:
        reply = _call_gemini_agentic(system, history, user_msg, on_tool_call=on_tool_call, sender=sender, originating_chat_id=originating_chat_id)
        reply = _humanize_transient_error(reply)
        reply = _maybe_sass_large_prompt(reply)
        if reply:
            return reply
        fallback = _call_ollama(system, history, user_msg)
        if fallback:
            soft_miss = _ollama_soft_miss_reason(user_msg, fallback)
            if not soft_miss:
                return fallback
            _log_bot_event("ollama_soft_miss", {"route": "tool_fallback", "reason": soft_miss})
        fallback = _call_gemini(system, history, user_msg)
        fallback = _suppress_provider_status_reply(user_msg, fallback, "tool_gemini_fallback")
        if fallback:
            return fallback
        roast = _harmless_roast_fallback(user_msg)
        return roast if roast else _failure_copy.TOOL_CHAT_FAILURE_REPLY

    if allowed_tools:
        reply = _call_gemini_agentic(system, history, user_msg, allowed_tools=allowed_tools, on_tool_call=on_tool_call, sender=sender, originating_chat_id=originating_chat_id)
        reply = _humanize_transient_error(reply)
        reply = _maybe_sass_large_prompt(reply)
        if reply:
            return reply
        fallback = _call_ollama(system, history, user_msg)
        if fallback:
            soft_miss = _ollama_soft_miss_reason(user_msg, fallback)
            if not soft_miss:
                return fallback
            _log_bot_event("ollama_soft_miss", {"route": "allowed_tools_fallback", "reason": soft_miss})
        fallback = _call_gemini(system, history, user_msg)
        fallback = _suppress_provider_status_reply(user_msg, fallback, "allowed_tools_gemini_fallback")
        if fallback:
            return fallback
        roast = _harmless_roast_fallback(user_msg)
        return roast if roast else _failure_copy.TOOL_CHAT_FAILURE_REPLY

    advanced_route = _owner_advanced_direct_route(user_msg, sender)
    if advanced_route:
        route_name, model = advanced_route
        _log_bot_event(
            "model_route_selected",
            {"route": route_name, "provider": "gemini", "model": model},
        )
        reply = _call_gemini(
            system,
            history,
            user_msg,
            model=model,
            source=f"{route_name}_direct",
        )
        reply = _humanize_transient_error(reply)
        reply = _maybe_sass_large_prompt(reply)
        if simple_chat:
            reply = _polish_simple_chat_reply(user_msg, reply)
        reply = _suppress_provider_status_reply(user_msg, reply, f"{route_name}_direct")
        if reply:
            return reply
        logger.warning("Advanced %s route failed; falling back to normal direct chat routing", route_name)

    simple_route = _simple_chat_direct_route()
    if simple_route:
        route_name, model = simple_route
        _log_bot_event(
            "model_route_selected",
            {"route": route_name, "provider": "gemini", "model": model},
        )
        reply = _call_gemini(
            system,
            history,
            user_msg,
            model=model,
            source=f"{route_name}_direct",
        )
        reply = _humanize_transient_error(reply)
        reply = _maybe_sass_large_prompt(reply)
        if simple_chat:
            reply = _polish_simple_chat_reply(user_msg, reply)
        reply = _suppress_provider_status_reply(user_msg, reply, f"{route_name}_direct")
        if reply:
            return reply
        logger.warning("Simple chat route failed; falling back to normal direct chat routing")

    if _ollama_down:
        ollama_hard_miss = True
        local_chat_available = _ollama_tags_available() if simple_chat else check_ollama_recovery()
        if local_chat_available:
            reply = _call_ollama(
                system,
                history,
                user_msg,
                num_predict=OLLAMA_SIMPLE_CHAT_NUM_PREDICT if simple_chat else None,
                temperature=OLLAMA_SIMPLE_CHAT_TEMPERATURE if simple_chat else None,
                timeout=OLLAMA_SIMPLE_CHAT_TIMEOUT if simple_chat else None,
                model=_ollama_simple_chat_model() if simple_chat else None,
            )
            reply = _polish_simple_chat_reply(user_msg, reply) if simple_chat else reply
            if reply:
                soft_miss = _ollama_soft_miss_reason(user_msg, reply)
                if not soft_miss:
                    return reply
                ollama_hard_miss = False
                _log_bot_event("ollama_soft_miss", {"route": "recovered_direct", "reason": soft_miss})
            else:
                logger.warning("Ollama health check recovered, but chat call still failed")
                _mark_ollama_down_after_direct_miss(simple_chat=simple_chat)
        reply = _call_gemini(system, history, user_msg)
        reply = _polish_simple_chat_reply(user_msg, reply) if simple_chat else reply
        reply = _suppress_provider_status_reply(user_msg, reply, "direct_gemini_fallback")
        if reply:
            return reply
        if simple_chat:
            return _simple_chat_empty_fallback(user_msg)
        if ollama_hard_miss:
            _mark_ollama_down(notify=True)
        return _failure_copy.DIRECT_CHAT_FAILURE_REPLY
    else:
        reply = _call_ollama(
            system,
            history,
            user_msg,
            num_predict=OLLAMA_SIMPLE_CHAT_NUM_PREDICT if simple_chat else None,
            temperature=OLLAMA_SIMPLE_CHAT_TEMPERATURE if simple_chat else None,
            timeout=OLLAMA_SIMPLE_CHAT_TIMEOUT if simple_chat else None,
            model=_ollama_simple_chat_model() if simple_chat else None,
        )
        reply = _polish_simple_chat_reply(user_msg, reply) if simple_chat else reply
        if reply:
            soft_miss = _ollama_soft_miss_reason(user_msg, reply)
            if not soft_miss:
                return reply
            ollama_hard_miss = False
            _log_bot_event("ollama_soft_miss", {"route": "direct", "reason": soft_miss})
        else:
            ollama_hard_miss = True
            _mark_ollama_down_after_direct_miss(simple_chat=simple_chat)
        reply = _call_gemini(system, history, user_msg)
        reply = _polish_simple_chat_reply(user_msg, reply) if simple_chat else reply
        reply = _suppress_provider_status_reply(user_msg, reply, "direct_gemini_fallback")
        if reply:
            return reply
        if simple_chat:
            return _simple_chat_empty_fallback(user_msg)
        if ollama_hard_miss:
            _mark_ollama_down(notify=True)
        return _failure_copy.DIRECT_CHAT_FAILURE_REPLY

    return _failure_copy.UNEXPECTED_FAILURE_REPLY
