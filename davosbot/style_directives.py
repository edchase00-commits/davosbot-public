"""Durable style/personality directives for chat and persona behavior."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import BOT_DB_PATH, normalize_handle
from .db import connect_bot_db
from .permissions import is_owner

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = "global"
SCOPE_CHAT = "chat"
SCOPE_PERSONA = "persona"
SCOPE_TOPIC = "topic"

_MAX_INSTRUCTION_CHARS = 600
_MAX_TRIGGER_CHARS = 120
_MAX_ACTIVE_DIRECTIVES = 8

_LIST_RE = re.compile(
    r"^\s*(?:style|styles|style\s+directives?|directives?|personality\s+max)"
    r"(?:\s+(?:list|show|status))?\s*$",
    re.IGNORECASE,
)
_DELETE_RE = re.compile(
    r"^\s*(?:style|styles|style\s+directives?|directives?|personality\s+max)"
    r"\s+(?:delete|remove|disable|off)\s+#?(\d+)\s*$",
    re.IGNORECASE,
)
_EXPLICIT_ADD_RE = re.compile(
    r"^\s*(?:style|style\s+directive|directive|personality\s+max)"
    r"(?:\s+(?:add|set|save|remember|lock\s+in))?\s*(?::|-)\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_NATURAL_DIRECTIVE_RE = re.compile(
    r"\bfrom\s+now\s+on\b"
    r"|\bonly\s+do\s+this\s+from\s+now\s+on\b"
    r"|\b(?:this|the)\s+personality\s+should\s+(?:sound|talk|feel|respond|reply|act)\s+like\b"
    r"|\b(?:make|set)\s+(?:this\s+)?personality\b.{0,100}\b(?:sound|talk|feel|respond|reply|act|vibe)\b"
    r"|\bwhen\s+we\s+(?:talk\s+(?:about|abt)|mention|say|discuss|bring\s+up)\b"
    r"|\bif\s+we\s+(?:talk\s+(?:about|abt)|mention|say|discuss|bring\s+up)\b"
    r"|\b(?:in|for)\s+this\s+chat\b.{0,80}\b(?:act|talk|respond|reply|say|use|perform|style|vibe)\b"
    r"|\b(?:in|for)\s+[\w -]{2,40}\s+persona\b.{0,100}\b(?:act|talk|respond|reply|say|use|perform|style|vibe|emoji)",
    re.IGNORECASE | re.DOTALL,
)
_TOPIC_TRIGGER_RE = re.compile(
    r"\b(?:when|if)\s+we\s+(?:talk\s+(?:about|abt)|mention|say|discuss|bring\s+up)\s+"
    r"(.{1,100}?)(?:,|\s+(?:you\s+must|use|say|respond|reply|act|perform|do|make|hit)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_STANDALONE_STYLE_RE = re.compile(
    r"^\s*(?:use\s+(?:these|this)\s+emojis?|perform\s+like\s+this|act\s+like\s+this|talk\s+like\s+this|reply\s+like\s+this|"
    r"(?:this|the)\s+personality\s+should\s+(?:sound|talk|feel|respond|reply|act)\s+like)\b",
    re.IGNORECASE | re.DOTALL,
)
_STYLE_TRAIT_RE = (
    r"(?:short|shorter|brief|concise|direct|casual|chill|relaxed|formal|professional|"
    r"funny|funnier|serious|warmer|friendlier|blunt|detailed|longer|wordy|verbose|less\s+wordy|"
    r"more\s+human|more\s+natural|more\s+helpful)"
)
_PLAIN_PREFERENCE_RE = re.compile(
    rf"^\s*(?:(?:please\s+)?(?:can|could|would|will)\s+you\s+|please\s+)?(?:"
    rf"(?:always\s+)?call\s+me\s+(?!(?:later|tomorrow|tonight|at|in)\b)"
    rf"[a-z][\w'-]*(?:\s+[a-z][\w'-]*){{0,2}}\s*[.!]?"
    rf"|(?:keep|make)\s+(?:your\s+)?(?:replies?|answers?|messages?|tone|voice)\s+(?:more\s+|less\s+)?{_STYLE_TRAIT_RE}\b.*"
    rf"|(?:be|sound|talk|reply|respond|write|act)\s+(?:a\s+little\s+|way\s+|more\s+|less\s+)?{_STYLE_TRAIT_RE}\b.*"
    r"|(?:use|add)\s+(?:fewer|less|more|no)\s+(?:emojis?|slang|bullets?|headings?|markdown)\b.*"
    r"|(?:avoid|skip|drop|stop\s+using)\s+(?:the\s+)?(?:emojis?|slang|bullets?|headings?|markdown)\b.*"
    r"|(?:do\s+not|don't)\s+use\s+(?:emojis?|slang|bullets?|headings?|markdown)\b.*"
    rf"|(?:do\s+not|don't)\s+be\s+(?:so\s+)?{_STYLE_TRAIT_RE}\b.*"
    r"|no\s+more\s+(?:emojis?|slang|bullets?|headings?|markdown)\b.*"
    rf"|remember\s+(?:that\s+)?i\s+(?:prefer|like|want)\s+(?:your\s+)?"
    rf"(?:replies?|answers?|messages?|tone|voice)\s+(?:to\s+be\s+)?{_STYLE_TRAIT_RE}\b.*"
    rf"|remember\s+(?:that\s+)?i\s+(?:prefer|like|want)\s+{_STYLE_TRAIT_RE}\s+"
    rf"(?:replies?|answers?|messages?|tone|voice)\b.*"
    r")\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TONE_FEEDBACK_RE = re.compile(
    r"\b(?:"
    r"be\s+(?:way\s+|more\s+|a\s+)?(?:chill|chiller|normal|casual|loose)|"
    r"(?:can\s+you\s+)?go\s+back\s+to\s+being\s+(?:a\s+)?chiller|"
    r"(?:chill|loosen)\s+out|"
    r"tone\s+(?:it\s+)?(?:down|back)|"
    r"prefer\s+you\s+(?:be|sound|talk|reply|respond)\b.{0,80}\b(?:chill|chiller|normal|casual|loose)|"
    r"give\s+me\s+more\s+davos\s+energy|"
    r"hang\s+loose|"
    r"enough\s+with\b.{0,120}\b(?:robot|normal|chiller|restored|recalibrated|clipboard|back|fixed|mode)|"
    r"(?:don'?t|do\s+not)\s+(?:narrate|tell|say|keep\s+saying)\b.{0,120}\b(?:back|normal|robot|chiller|restored|recalibrated|clipboard|fixed|mode)|"
    r"stop\s+(?:narrating|telling|saying)\b.{0,120}\b(?:back|normal|robot|chiller|restored|recalibrated|clipboard|fixed|mode)|"
    r"we\s+get\s+it\b.{0,120}\b(?:back|normal|robot|chiller|restored|recalibrated|clipboard|fixed|mode)|"
    r"just\s+be\s+normal"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)
_PERSONA_SCOPE_RE = re.compile(
    r"\b(?:in|for|during)\s+([a-z0-9][a-z0-9 _-]{1,39})\s+persona\b",
    re.IGNORECASE,
)
_GLOBAL_SCOPE_RE = re.compile(r"\b(?:globally|everywhere|all\s+chats|every\s+chat)\b", re.IGNORECASE)
_CHAT_SCOPE_RE = re.compile(r"\b(?:this\s+chat|this\s+group|this\s+gc|here)\b", re.IGNORECASE)
_BLOCKED_DIRECTIVE_RE = re.compile(
    r"\b(?:admin|admin_password|password|secret|token|api\s*key|permissions?|owner[- ]only|bypass|ignore\s+(?:all\s+)?(?:previous|system|developer)|"
    r"system\s+prompt|developer\s+instructions?|memory\.md|soul\.md|reminders?|cron|deploy|pull|restart|shell|sqlite|database|tools?|"
    r"send\s+(?:a\s+)?(?:text|imessage|dm|message)|private\s+(?:send|message|text|dm)|read\s+files?|write\s+files?)\b",
    re.IGNORECASE,
)
_INITIALIZED_DB_PATHS: set[str] = set()

@dataclass(frozen=True)
class StyleDirective:
    id: int
    scope_type: str
    scope_value: str
    trigger: str
    instruction: str


def _db_path(path: str | None = None) -> str:
    return path or BOT_DB_PATH


def init_style_directives_db(path: str | None = None) -> None:
    db_path = _db_path(path)
    if db_path in _INITIALIZED_DB_PATHS:
        return
    with connect_bot_db(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS style_directives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_by TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_value TEXT NOT NULL DEFAULT '',
                trigger TEXT,
                instruction TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_style_directives_lookup
            ON style_directives(enabled, scope_type, scope_value)
            """
        )
    _INITIALIZED_DB_PATHS.add(db_path)


def _persona_key(name: str | None) -> str:
    raw = str(name or "").strip()
    if raw.startswith("gc:"):
        raw = raw.rsplit(":", 1)[-1]
    return re.sub(r"[\s_-]+", " ", raw.lower().strip().lstrip("_")).strip()


def _normalize_topic(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", (raw or "").strip(" .,:;!?\"'")).strip()
    return cleaned[:_MAX_TRIGGER_CHARS]


def _clean_instruction(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", (raw or "").strip()).strip()
    return cleaned[:_MAX_INSTRUCTION_CHARS]


def _looks_like_directive(text: str) -> bool:
    if not text or len(text) > 1200:
        return False
    if _EXPLICIT_ADD_RE.match(text):
        return True
    return bool(
        _NATURAL_DIRECTIVE_RE.search(text)
        or _STANDALONE_STYLE_RE.search(text)
        or _PLAIN_PREFERENCE_RE.search(text)
    )


def _extract_instruction(text: str) -> str:
    explicit = _EXPLICIT_ADD_RE.match(text)
    if explicit:
        return _clean_instruction(explicit.group(1))
    return _clean_instruction(text)


def _extract_trigger(text: str) -> str:
    match = _TOPIC_TRIGGER_RE.search(text)
    return _normalize_topic(match.group(1)) if match else ""


def _tone_feedback_instruction(text: str) -> str | None:
    if not _TONE_FEEDBACK_RE.search(text or ""):
        return None
    return (
        "Keep replies relaxed, casual, and concise. Apply tone changes silently. "
        "Do not narrate personality repairs, mode changes, returning to a previous voice, "
        "restoration, recalibration, clipboard voice, or robot-to-Davos transitions."
    )


def looks_like_tone_feedback(text: str) -> bool:
    return bool(_TONE_FEEDBACK_RE.search(text or ""))


def _handle_tone_feedback_directive(
    *,
    sender: str,
    raw: str,
    context_id: str,
    active_persona: str | None,
    is_group: bool,
    path: str | None,
) -> str | None:
    tone_instruction = _tone_feedback_instruction(raw)
    if not tone_instruction:
        return None
    reason = _blocked_reason(raw) or _blocked_reason(tone_instruction)
    if reason:
        return reason
    scope_type, scope_value = _resolve_scope(
        sender=sender,
        text=raw,
        context_id=context_id,
        active_persona=active_persona,
        is_group=is_group,
        trigger="",
    )
    add_style_directive(
        sender=sender,
        instruction=tone_instruction,
        scope_type=scope_type,
        scope_value=scope_value,
        trigger="",
        path=path,
    )
    return "Got you. I'll keep it loose."


def _resolve_scope(
    *,
    sender: str,
    text: str,
    context_id: str,
    active_persona: str | None,
    is_group: bool,
    trigger: str,
) -> tuple[str, str]:
    if not is_owner(sender):
        return SCOPE_CHAT, context_id

    persona_match = _PERSONA_SCOPE_RE.search(text)
    if persona_match:
        return SCOPE_PERSONA, _persona_key(persona_match.group(1))

    if trigger and not _CHAT_SCOPE_RE.search(text):
        return SCOPE_TOPIC, trigger.lower()

    if _GLOBAL_SCOPE_RE.search(text):
        return SCOPE_GLOBAL, ""

    if _CHAT_SCOPE_RE.search(text) or is_group:
        return SCOPE_CHAT, context_id

    if active_persona and _persona_key(active_persona) and re.search(r"\bactive\s+persona\b", text, re.IGNORECASE):
        return SCOPE_PERSONA, _persona_key(active_persona)

    return SCOPE_GLOBAL, ""


def _scope_label(scope_type: str, scope_value: str, trigger: str = "") -> str:
    if scope_type == SCOPE_GLOBAL:
        return "global"
    if scope_type == SCOPE_CHAT:
        return "this chat"
    if scope_type == SCOPE_PERSONA:
        return f"{scope_value} persona"
    if scope_type == SCOPE_TOPIC:
        return f'topic "{trigger or scope_value}"'
    return scope_type


def _blocked_reason(instruction: str) -> str | None:
    if len(instruction) < 8:
        return "Give me a real style instruction to save."
    if _BLOCKED_DIRECTIVE_RE.search(instruction):
        return "That sounds like tools/permissions/secrets/runtime, not personality. I can only save style directives."
    return None


def add_style_directive(
    *,
    sender: str,
    instruction: str,
    scope_type: str,
    scope_value: str = "",
    trigger: str = "",
    path: str | None = None,
) -> int:
    init_style_directives_db(path)
    actor = normalize_handle(sender)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with connect_bot_db(_db_path(path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO style_directives
                (created_at, updated_at, created_by, scope_type, scope_value, trigger, instruction, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (now, now, actor, scope_type, scope_value or "", trigger or "", instruction),
        )
        return int(cur.lastrowid)


def list_style_directives(
    *,
    context_id: str | None = None,
    owner_view: bool = False,
    actor: str | None = None,
    limit: int = 12,
    path: str | None = None,
) -> list[StyleDirective]:
    init_style_directives_db(path)
    params: list[object] = []
    where = "enabled = 1"
    if not owner_view:
        where += " AND scope_type = ? AND scope_value = ?"
        params.extend([SCOPE_CHAT, context_id or ""])
        if actor:
            where += " AND created_by = ?"
            params.append(normalize_handle(actor))
    with connect_bot_db(_db_path(path)) as conn:
        rows = conn.execute(
            f"""
            SELECT id, scope_type, scope_value, COALESCE(trigger, ''), instruction
            FROM style_directives
            WHERE {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [StyleDirective(int(row[0]), row[1], row[2] or "", row[3] or "", row[4] or "") for row in rows]


def disable_style_directive(
    directive_id: int,
    *,
    sender: str,
    context_id: str | None = None,
    path: str | None = None,
) -> bool:
    init_style_directives_db(path)
    actor = normalize_handle(sender)
    owner_view = is_owner(sender)
    if owner_view:
        sql = "UPDATE style_directives SET enabled = 0, updated_at = datetime('now') WHERE id = ? AND enabled = 1"
        params: tuple[object, ...] = (directive_id,)
    else:
        sql = (
            "UPDATE style_directives SET enabled = 0, updated_at = datetime('now') "
            "WHERE id = ? AND enabled = 1 AND scope_type = ? AND scope_value = ? AND created_by = ?"
        )
        params = (directive_id, SCOPE_CHAT, context_id or "", actor)
    with connect_bot_db(_db_path(path)) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount > 0


def _format_directive_list(rows: list[StyleDirective]) -> str:
    if not rows:
        return "No style directives saved for this scope."
    lines = []
    for row in rows:
        scope = _scope_label(row.scope_type, row.scope_value, row.trigger)
        instruction = row.instruction if len(row.instruction) <= 90 else row.instruction[:87].rstrip() + "..."
        lines.append(f"#{row.id} [{scope}] {instruction}")
    return "Style directives:\n" + "\n".join(lines)


def handle_style_directive_message(
    sender: str,
    text: str,
    *,
    context_id: str,
    active_persona: str | None = None,
    is_group: bool = False,
    path: str | None = None,
    tone_feedback_only: bool = False,
    allow_tone_feedback: bool = True,
) -> str | None:
    raw = text or ""
    if tone_feedback_only:
        return _handle_tone_feedback_directive(
            sender=sender,
            raw=raw,
            context_id=context_id,
            active_persona=active_persona,
            is_group=is_group,
            path=path,
        )

    delete_match = _DELETE_RE.match(raw)
    if delete_match:
        ok = disable_style_directive(
            int(delete_match.group(1)),
            sender=sender,
            context_id=context_id,
            path=path,
        )
        return "Style directive removed." if ok else "No matching style directive found for you here."

    if _LIST_RE.match(raw):
        rows = list_style_directives(
            context_id=context_id,
            owner_view=is_owner(sender),
            actor=None if is_owner(sender) else sender,
            path=path,
        )
        return _format_directive_list(rows)

    if allow_tone_feedback:
        tone_reply = _handle_tone_feedback_directive(
            sender=sender,
            raw=raw,
            context_id=context_id,
            active_persona=active_persona,
            is_group=is_group,
            path=path,
        )
        if tone_reply is not None:
            return tone_reply

    if not _looks_like_directive(raw):
        return None

    instruction = _extract_instruction(raw)
    reason = _blocked_reason(instruction)
    if reason:
        return reason

    trigger = _extract_trigger(raw)
    scope_type, scope_value = _resolve_scope(
        sender=sender,
        text=raw,
        context_id=context_id,
        active_persona=active_persona,
        is_group=is_group,
        trigger=trigger,
    )
    directive_id = add_style_directive(
        sender=sender,
        instruction=instruction,
        scope_type=scope_type,
        scope_value=scope_value,
        trigger=trigger,
        path=path,
    )
    scope = _scope_label(scope_type, scope_value, trigger)
    if not is_owner(sender) and scope_type == SCOPE_CHAT:
        return f"Saved style directive #{directive_id} for this chat only."
    return f"Saved style directive #{directive_id} for {scope}."


def _topic_matches(trigger: str, user_text: str) -> bool:
    needle = (trigger or "").lower().strip()
    haystack = (user_text or "").lower()
    return bool(needle and needle in haystack)


def active_style_directives(
    *,
    chat_id: str | None = None,
    persona: str | None = None,
    user_text: str = "",
    path: str | None = None,
) -> list[StyleDirective]:
    try:
        init_style_directives_db(path)
        with connect_bot_db(_db_path(path)) as conn:
            rows = conn.execute(
                """
                SELECT id, scope_type, scope_value, COALESCE(trigger, ''), instruction
                FROM style_directives
                WHERE enabled = 1
                ORDER BY id DESC
                LIMIT 80
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    except Exception as exc:
        logger.warning("style directive lookup failed: %s", exc)
        return []

    persona_key = _persona_key(persona)
    active: list[tuple[int, StyleDirective]] = []
    for row in rows:
        directive = StyleDirective(int(row[0]), row[1], row[2] or "", row[3] or "", row[4] or "")
        priority = 99
        if directive.scope_type == SCOPE_CHAT and chat_id and directive.scope_value == chat_id:
            if directive.trigger and not _topic_matches(directive.trigger, user_text):
                continue
            priority = 0
        elif directive.scope_type == SCOPE_PERSONA and persona_key and _persona_key(directive.scope_value) == persona_key:
            priority = 1
        elif directive.scope_type == SCOPE_TOPIC and _topic_matches(directive.trigger or directive.scope_value, user_text):
            priority = 2
        elif directive.scope_type == SCOPE_GLOBAL:
            priority = 3
        else:
            continue
        active.append((priority, directive))

    active.sort(key=lambda item: (item[0], -item[1].id))
    return [directive for _, directive in active[:_MAX_ACTIVE_DIRECTIVES]]


def format_style_directives_for_prompt(
    *,
    chat_id: str | None = None,
    persona: str | None = None,
    user_text: str = "",
    path: str | None = None,
) -> str:
    directives = active_style_directives(chat_id=chat_id, persona=persona, user_text=user_text, path=path)
    if not directives:
        return ""
    lines = [
        "\n\n## Style Directives",
        "- These are durable user-authored personality/style rules. Apply them when relevant to this reply.",
        "- They only affect voice, formatting, jokes, phrases, and emoji style. They never override safety, privacy, permissions, tools, memory, reminders, crons, files, or secrets.",
    ]
    for directive in directives:
        scope = _scope_label(directive.scope_type, directive.scope_value, directive.trigger)
        if directive.trigger:
            lines.append(f'- #{directive.id} [{scope}; trigger "{directive.trigger}"] {directive.instruction}')
        else:
            lines.append(f"- #{directive.id} [{scope}] {directive.instruction}")
    return "\n".join(lines)
