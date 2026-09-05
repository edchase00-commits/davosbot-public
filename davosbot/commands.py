import json
import re
import sqlite3
import subprocess
import logging
import os
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .runtime_locks import PERSONALITY_FILE_LOCK, schedule_locked
from .cleanup_runner import cleanup_lock_state

_PROJECT_DIR = ""
from .permissions import is_owner, is_admin, can_user_do, redact_secret
from .group_chat import (
    append_group_persona_note,
    create_group_persona,
    enable_gc,
    disable_gc,
    get_group_persona,
    grant_group_persona_editor,
    group_persona_display_name,
    group_persona_token,
    is_group_persona_token,
    list_group_personas,
    parse_group_persona_token,
    approve_user,
    revoke_user,
    resolve_group_persona_slug,
    set_persona,
    get_persona,
    is_approved_user,
)
from .personality import is_persona_hidden, list_personas, persona_file_for, resolve_persona_name
from .memory import (
    add_owner_memory_item,
    clear_history,
    clear_history_minutes,
    clear_history_count,
    list_owner_memory_items,
    search_owner_memory_items,
)
from .config import (
    ADVANCED_CODE_MODEL,
    ADVANCED_TEXT_MODEL,
    ADVANCED_VISION_MODEL,
    BOT_DB_PATH,
    DB_PATH,
    FANTASY_DASHBOARD_URL,
    GEMINI_API_KEY,
    GEMINI_DAILY_ALERT_USD,
    GEMINI_DAILY_BUDGET_USD,
    GEMINI_ENABLED,
    GEMINI_IMAGE_MODEL,
    GEMINI_MODEL,
    GEMINI_REWRITE_MODEL,
    IMAGE_PROVIDER,
    IMAGE_SCAN_PROVIDER,
    LOCAL_IMAGE_ENDPOINT,
    LOCAL_IMAGE_MODEL,
    MAC_MINI_APPLE_ID,
    MEMORY_PATH,
    MODEL_ROUTE_CODE_REVIEW,
    MODEL_ROUTE_COMPLEX_REASONING,
    MODEL_ROUTE_HELPER_REWRITE,
    MODEL_ROUTE_IMAGE_GENERATION,
    MODEL_ROUTE_NANO_BANANA_IMAGE,
    MODEL_ROUTE_IMAGE_SCAN,
    MODEL_ROUTE_SIMPLE_CHAT,
    MODEL_ROUTE_TOOL_USE,
    NANO_BANANA_IMAGE_ASPECT_RATIO,
    NANO_BANANA_IMAGE_MODEL,
    NANO_BANANA_IMAGE_SIZE,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_SIMPLE_CHAT_MODEL,
    OWNER_ALERT_WEBHOOK_URL,
    OPENAI_API_KEY,
    OPENAI_IMAGE_MODEL,
    OPENAI_VISION_MODEL,
    OWNER_ID,
    PROJECT_ROOT,
    TAVILY_API_KEY,
    normalize_handle,
)
from .billing import GEMINI_INPUT_RATE_USD, GEMINI_OUTPUT_RATE_USD
from .brain import get_session_info, check_action_permission
from .openai_images import choose_generation_provider, choose_scan_provider
from .personality import validate_personality_files
from .soul import restore_soul_from_backup
from .ufc import get_ufc_fight_card, is_ufc_fight_card_request
from .market import handle_market_command
from .club_suggestions import handle_club_command
from . import fantasy_access

logger = logging.getLogger(__name__)
_PROJECT_DIR = str(PROJECT_ROOT)


def _utc_today():
    return datetime.now(timezone.utc).date()


def _ensure_git_hooks_path() -> str:
    """Activate repo-managed hooks when present; never block deploy on failure."""
    hook_dir = Path(_PROJECT_DIR) / ".githooks"
    post_merge = hook_dir / "post-merge"
    if not post_merge.exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=_PROJECT_DIR,
        )
        if result.returncode == 0:
            return "hooks: .githooks active"
        logger.warning("git hooks install failed: %s", (result.stderr or result.stdout).strip()[:300])
    except Exception as exc:
        logger.warning("git hooks install skipped: %s", exc)
    return "hooks: not installed"

OWNER_COMMANDS = {
    # System
    "pull", "status", "uptime", "logs", "billing", "model", "models", "api", "apis", "tools", "backups", "alert", "alerts",
    # Memory
    "memory", "myfacts", "enrichsoul",
    # Persona
    "persona", "soulversion", "restoresoul", "personalities",
    # Admin
    "grant", "revoke", "admins", "ratelimit", "mypermissions", "changelog", "log", "ship", "triage",
    "intake", "bigchange", "big-change", "big change", "codex",
    # Features
    "bets", "fantasy", "sharecontact", "scan", "workout", "scheduled", "cancel", "cron", "crons", "jobs",
    "image", "images", "market", "markets", "stock", "stocks", "quote",
    # Skills
    "skills", "skill",
    # Group chat
    "chats", "ping",
    # Health
    "drift", "weekly", "maintenance",
    # Help
    "help", "capability", "capabilities",
}

# Matches natural-language persona switch requests and captures the candidate name.
# Validated against list_personas() before acting so "be careful" never triggers.
_PERSONA_SWITCH_RE = re.compile(
    r"^(?:"
    r"switch\s+(?:persona\s+)?to"
    r"|change\s+(?:persona\s+)?to"
    r"|be"
    r"|activate"
    r"|use(?:\s+the)?"
    r"|go(?:\s+full)?"           # "go gruden" or "go full gruden"
    r")\s+(.+?)(?:\s+(?:persona|mode|personality))?$",
    re.IGNORECASE,
)

_DEFAULT_PERSONA_TARGET_RE = re.compile(
    r"^(?:the\s+)?(?:default|normal)(?:\s+one)?$",
    re.IGNORECASE,
)

_CONFIRMED_CLEANUP_RUN_RE = re.compile(
    r"^(?:"
    r"yes\s+fix(?:\s+it|\s+them|\s+the\s+log)?"
    r"|yes\s+ship(?:\s+(?:it|them|the\s+fixes|fixes))?"
    r"|ship\s+(?:the\s+)?fixes"
    r"|fix\s+(?:the\s+)?log"
    r")$",
    re.IGNORECASE,
)

_CLEANUP_PROMPT_CONFIRM_RE = re.compile(
    r"^(?:"
    r"send\s+(?:me\s+)?(?:the\s+)?codex\s+prompt"
    r"|send\s+(?:me\s+)?(?:the\s+)?master\s+prompt"
    r"|codex\s+prompt"
    r"|cleanup\s+prompt"
    r"|safe\s+cleanup\s+prompt"
    r"|master\s+(?:codex\s+)?prompt"
    r"|codex\s+master\s+prompt"
    r"|phone\s+codex\s+prompt"
    r")$",
    re.IGNORECASE,
)

_CLEANUP_STATUS_REQUESTS = {
    "cleanup status",
    "safe cleanup status",
    "codex cleanup status",
    "codex safe cleanup status",
    "status of cleanup",
    "status of safe cleanup",
    "status of codex cleanup",
    "status of codex safe cleanup",
    "what is the cleanup status",
    "what's the cleanup status",
    "what is the status of cleanup",
    "what's the status of cleanup",
    "what is the status of safe cleanup",
    "what's the status of safe cleanup",
    "what is the status of codex cleanup",
    "what's the status of codex cleanup",
    "what is the status of codex safe cleanup",
    "what's the status of codex safe cleanup",
    "is cleanup running",
    "is safe cleanup running",
    "is codex cleanup running",
    "is codex safe cleanup running",
}

_PERSONA_RESET_RE = re.compile(
    r"^(?:"
    r"persona\s+(?:reset|clear|(?:the\s+)?(?:default|normal)(?:\s+one)?)"
    r"|(?:reset|clear)\s+(?:the\s+)?persona"
    r"|(?:the\s+)?(?:default|normal)(?:\s+one)?(?:\s+(?:persona|mode))?"
    r"|(?:switch|change)\s+(?:persona\s+)?to\s+(?:the\s+)?(?:default|normal)(?:\s+one)?"
    r"|(?:be|use|go)\s+(?:the\s+)?(?:default|normal)(?:\s+one)?"
    r"|back\s+to\s+(?:the\s+)?(?:default|normal)(?:\s+one)?"
    r")$",
    re.IGNORECASE,
)


def _detect_persona_switch(text: str) -> str | None:
    """Return the normalized persona name if text is a NL switch request, else None.

    Validates the extracted name against the on-disk persona list so unrelated
    phrases like 'be careful' or 'use the browser' fall through to the LLM.
    """
    m = _PERSONA_SWITCH_RE.match(text.strip())
    if not m:
        return None
    return resolve_persona_name(m.group(1), include_hidden=True)


def _detect_persona_reset(text: str) -> bool:
    return bool(_PERSONA_RESET_RE.match(text.strip()))


def _is_default_persona_request(text: str) -> bool:
    return bool(_DEFAULT_PERSONA_TARGET_RE.match(text.strip()))


def _looks_like_cleanup_prompt_confirmation(text: str) -> bool:
    return bool(_CLEANUP_PROMPT_CONFIRM_RE.match((text or "").strip()))


def _looks_like_confirmed_cleanup_run(text: str) -> bool:
    return bool(_CONFIRMED_CLEANUP_RUN_RE.match((text or "").strip()))


def _looks_like_cleanup_status_request(text: str) -> bool:
    normalized = re.sub(r"[?!.,]+", " ", (text or "").strip().lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized in _CLEANUP_STATUS_REQUESTS


def _persona_status(context: str) -> str:
    available = list_personas()
    current = get_persona(context) or "default"
    if is_group_persona_token(current):
        current_display = group_persona_display_name(current) or "missing group persona"
    else:
        current_display = "hidden persona" if current != "default" and is_persona_hidden(current) else current
    lines = [
        f"Current: {current_display}",
        f"Available global: {', '.join(available) or 'none'}",
    ]
    group_personas = list_group_personas(context)
    if group_personas:
        names = ", ".join(persona.get("name", persona.get("slug", "")) for persona in group_personas)
        lines.append(f"This chat: {names}")
    return "\n".join(lines)


def _wants_all_crons(text: str) -> bool:
    return _cron_scope_from_text(text) == "all"


def _cron_scope_from_text(text: str) -> str:
    lower = (text or "").lower()
    if re.search(r"\b(?:just|only)\s+(?:to\s+)?me\b|\b(?:my|me)\s+(?:dm|dms|1:1|1on1|one\s+on\s+one)\b", lower):
        return "mine"
    if re.search(r"\b(?:dm|dms|direct|private|1:1|1on1|one\s+on\s+one)\b", lower):
        return "direct"
    if re.search(r"\b(?:gc|gcs|group|groups|group\s+chat|group\s+chats)\b", lower):
        return "groups"
    if re.search(r"\b(?:all|every|across\s+(?:all\s+)?chats?|global)\b", lower):
        return "all"
    return "current"


def _parse_cancel_cron_id(text: str) -> int | None:
    m = re.search(
        r"^(?:cancel|delete|remove|kill|stop|disable)\s+(?:#|id\s*#?|(?:cron|job|daily\s+job)\s+(?:id\s*#?|#)?)(\d+)\b",
        text.strip(),
        re.IGNORECASE,
    )
    return int(m.group(1)) if m else None


def _parse_group_tell(text: str) -> tuple[str, str] | None:
    """Parse '@Davos tell NAME MESSAGE' as an in-chat relay, not a private text."""
    m = re.match(r"^@davos\s+tell\s+(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    rest = m.group(1).strip()
    if not rest:
        return None
    if re.match(r"^(?:me|us|everyone|everybody|the\s+chat|this\s+chat)\b", rest, re.IGNORECASE):
        return None

    target = ""
    message = ""
    quoted = re.match(r"""^["']([^"']{1,40})["']\s+(.+)$""", rest, re.DOTALL)
    if quoted:
        target, message = quoted.group(1), quoted.group(2)
    else:
        marker = re.match(
            r"^(.{1,40}?)(?:\s+(?:that|to|saying|says)|\s*[:\-])\s+(.+)$",
            rest,
            re.IGNORECASE | re.DOTALL,
        )
        if marker:
            target, message = marker.group(1), marker.group(2)
        else:
            parts = rest.split(None, 1)
            if len(parts) != 2:
                return None
            target, message = parts

    target = re.sub(r"\s+", " ", target).strip(" ,:;-")
    message = message.strip()
    if not target or not message:
        return None
    if target.lower() in {"me", "us", "everyone", "everybody", "the chat", "this chat"}:
        return None
    if len(target) > 40 or len(message) > 800:
        return None
    return target, message


def _format_group_tell(target: str, message: str, chat_id: str = "", sender: str = "") -> str:
    fallback = f"{target}, they said {message}. Don't shoot the messenger."
    if not chat_id:
        return fallback
    try:
        from .brain import get_response
        from .personality import build_system_prompt
        persona = get_persona(chat_id)
        try:
            system = build_system_prompt(persona=persona, user_text=message, chat_id=chat_id)
        except TypeError:
            system = build_system_prompt(persona=persona, user_text=message)
        prompt = (
            "Write one short public group-chat relay in the active persona's voice.\n"
            f"Target person: {target}\n"
            f"Message to relay: {message}\n"
            "Rules: keep the meaning, clearly address the target by name, do not say you sent a private text, "
            "do not claim any private action happened, max 35 words."
        )
        reply = get_response(system, [], prompt, use_tools=False, sender=sender, originating_chat_id=chat_id)
        reply = (reply or "").strip()
        if reply:
            return reply
    except Exception as e:
        logger.warning("group_tell styling failed: %s", e)
    return fallback


def _strip_group_command_prefix(text: str) -> str:
    return re.sub(r"^@davos\b", "", (text or "").strip(), flags=re.IGNORECASE).strip()


def _parse_group_persona_create(text: str) -> tuple[str, str] | None:
    cmd = _strip_group_command_prefix(text)
    if re.match(r"^add\s+(?:a\s+)?(?:group\s+|gc\s+|this\s+chat\s+)?persona\s+note\b", cmd, re.IGNORECASE):
        return None
    m = re.match(
        r"^(?:create|make|add)\s+(?:a\s+)?(?:(?:group|gc|this\s+chat)\s+)?persona\s+(.+)$",
        cmd,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    rest = m.group(1).strip()
    if ":" not in rest:
        raise ValueError("Use: @Davos create group persona Name: short style description")
    name, description = rest.split(":", 1)
    return name.strip(), description.strip()


def _parse_group_persona_editor_grant(text: str) -> str | None:
    cmd = _strip_group_command_prefix(text)
    patterns = [
        r"^(?:grant|allow)\s+(?:persona\s+)?editor\s+(\S+)\s*$",
        r"^(?:grant|allow)\s+(\S+)\s+(?:to\s+)?(?:edit|customize)\s+(?:the\s+)?(?:group\s+|gc\s+|this\s+chat\s+)?persona\s*$",
        r"^let\s+(\S+)\s+(?:edit|customize)\s+(?:the\s+)?(?:group\s+|gc\s+|this\s+chat\s+)?persona\s*$",
    ]
    for pattern in patterns:
        m = re.match(pattern, cmd, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _parse_group_persona_update(text: str) -> str | None:
    cmd = _strip_group_command_prefix(text)
    patterns = [
        r"^(?:update|edit|tweak|customize)\s+(?:the\s+)?(?:group|gc|this\s+chat)\s+persona\s*(?::|-|\bto\b)?\s*(.+)$",
        r"^(?:spice\s+up|make\s+spicier)\s+(?:the\s+)?(?:group\s+|gc\s+|this\s+chat\s+)?persona\s*(?::|-)?\s*(.+)$",
        r"^add\s+(?:a\s+)?(?:group\s+|gc\s+|this\s+chat\s+)?persona\s+note\s*(?::|-)?\s*(.+)$",
    ]
    for pattern in patterns:
        m = re.match(pattern, cmd, re.IGNORECASE | re.DOTALL)
        if m:
            note = m.group(1).strip()
            return note or ""
    return None


def _active_group_persona_slug(chat_id: str) -> str | None:
    parsed = parse_group_persona_token(get_persona(chat_id))
    if not parsed or parsed[0] != chat_id:
        return None
    return parsed[1]


def _log_group_persona_event(sender: str, event_type: str, payload: dict) -> None:
    try:
        import json as _json
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (sender, event_type, _json.dumps(payload)),
            )
            conn.commit()
    except Exception as e:
        logger.warning("group persona log failed: %s", e)


def handle_group_persona_editor_command(sender: str, chat_id: str, text: str) -> str | None:
    """Let granted users tweak only the active group-scoped persona."""
    note = _parse_group_persona_update(text)
    if note is None:
        return None
    try:
        name = append_group_persona_note(chat_id, sender, note)
    except PermissionError as e:
        return str(e)
    except ValueError as e:
        return str(e)
    _log_group_persona_event(
        sender,
        "group_persona_updated",
        {"chat_id": chat_id, "persona": name, "note_len": len(note)},
    )
    clear_history(chat_id)
    return f"Updated this chat's {name} persona. This stays scoped to this group only."


def _log_persona_switch(actor: str, persona_name: str, success: bool) -> None:
    action = "persona_switch" if success else "persona_switch_denied"
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO admin_audit (action, handle, actor) VALUES (?, ?, ?)",
                (action, persona_name, actor),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to log persona switch: %s", e)

_MEMORY_BASELINE = """# Memory

- the owner's name is the owner Chase
- the owner's number is the configured OWNER_ID ({owner_id})
- Cole is an admin and dogfooding partner
- Jon Jones is the GOAT UFC fighter (not Anderson Silva)
- the owner's favorite UFC event location is Atlanta
- the owner does not like Arsenal — trolls them constantly, uses the phrase "one kiss is all it takes"
- Bot runs local Ollama first (Gemma4 after the Mini pulls it; Gemma3 may remain live until then) with Gemini 3.1 Flash-Lite as fallback/tool-use, Gemini 3.5 Flash for rare owner-only pro thinking/code review, local Flux first for images, and Gemini 3.1 Flash Image for scan/Nano Banana.
- the owner prefers short responses from the bot across all personas
"""


def _memory_baseline() -> str:
    return _MEMORY_BASELINE.format(owner_id=OWNER_ID or "configured in .env")


def handle_command(sender: str, text: str, *, skip_web_search: bool = False) -> str | None:
    """
    Returns a response string if the text is a recognized command,
    or None if it should be handled as a normal conversation.
    """
    stripped = text.strip()
    cmd = stripped.lower()

    club_reply = handle_club_command(sender, stripped)
    if club_reply is not None:
        return club_reply

    # Open to everyone — informational
    if cmd == "help":
        return _cmd_help(sender)
    if cmd in ("capability", "capabilities"):
        return _cmd_capabilities(sender)
    if cmd == "mypermissions":
        return _cmd_mypermissions(sender)
    if re.fullmatch(r"(?:cancel\s+(?:food|food order|the food order)|food cancel)[.!]?", cmd) and is_admin(sender):
        from .food_order import handle_food_order
        return handle_food_order(sender, stripped) or "No food draft is open. No order was placed."
    if _looks_like_confirmed_cleanup_run(stripped):
        if not is_owner(sender):
            return "That cleanup runner is owner-only."
        return _cmd_confirmed_safe_cleanup(sender)
    if _looks_like_cleanup_prompt_confirmation(stripped):
        if not is_owner(sender):
            return "That cleanup prompt is owner-only."
        return _cmd_safe_cleanup(sender)
    if _looks_like_cleanup_status_request(stripped):
        if not is_owner(sender):
            return "That cleanup status is owner-only."
        return _cmd_cleanup_status(sender)

    if _detect_persona_reset(stripped):
        return _cmd_persona("persona reset", sender=sender)

    log_delete_alias = _rewrite_change_log_delete_alias(stripped)
    if log_delete_alias is not None:
        return _cmd_log(log_delete_alias, sender)
    log_update_alias = _rewrite_change_log_update_alias(stripped)
    if log_update_alias is not None:
        return _cmd_log(log_update_alias, sender)

    # Natural-language persona switch (e.g. "switch to gruden")
    detected_persona = _detect_persona_switch(cmd)
    if detected_persona is not None:
        return _cmd_persona(f"persona {detected_persona}", sender=sender)

    # /bet commands are open to everyone in GC (friends can log their own bets).
    # Intercept before the admin gate below.
    if cmd.startswith("/bet ") or (cmd.startswith("bet ") and not cmd.startswith("bets")):
        sub = cmd.split(None, 1)[1].lstrip("/").strip()
        if sub.startswith("log"):
            return _cmd_bet_log(text, sender)
        if sub.startswith("settle"):
            return _cmd_bet_settle(text, sender)
        if sub.startswith("stats"):
            return _cmd_bet_stats(text, sender)

    if _looks_like_self_repair_intake(stripped):
        return _cmd_self_repair_intake(stripped, sender)

    if is_ufc_fight_card_request(stripped):
        return None if skip_web_search else get_ufc_fight_card()

    if re.match(r"^\s*/?(?:market|markets|stocks?|quote)\b", stripped, re.IGNORECASE):
        return handle_market_command(stripped, sender_is_owner=is_owner(sender),
                                     **({"allow_live_lookup": False} if skip_web_search else {}))

    # Recognise OWNER_COMMANDS by exact match or prefix.
    # Also accept /workout and /bet slash-prefixed variants.
    slash_cmd = cmd.lstrip("/")
    if cmd not in OWNER_COMMANDS and not any(
        cmd.startswith(c + " ") or cmd == c or
        slash_cmd.startswith(c + " ") or slash_cmd == c
        for c in OWNER_COMMANDS
    ):
        from .food_order import handle_food_order
        return handle_food_order(sender, stripped) if is_admin(sender) else None

    if not is_admin(sender):
        return "That's an owner command. Type 'mypermissions' to see what you can do."

    # System
    if cmd == "pull":
        return _cmd_pull(sender)
    if cmd == "status":
        return _cmd_status(sender)
    if cmd == "uptime":
        return get_uptime(sender)
    if cmd == "logs":
        return _cmd_logs(sender)
    if cmd == "billing":
        return _cmd_billing(sender)
    if cmd in ("api", "apis", "api status", "apis status", "tool status", "tools status", "tools"):
        return _cmd_api_status(sender)
    if cmd == "model" or cmd.startswith("model ") or cmd == "models" or cmd.startswith("models "):
        return _cmd_model(stripped, sender)
    if cmd == "backups":
        return _cmd_backups(sender)
    if cmd == "fantasy" or cmd.startswith("fantasy "):
        return _cmd_fantasy(stripped, sender)
    if cmd == "alert" or cmd.startswith("alert ") or cmd == "alerts" or cmd.startswith("alerts "):
        return _cmd_owner_alert(stripped, sender)
    if cmd == "image access" or cmd.startswith("image access ") or cmd.startswith("images access "):
        return _cmd_image_access(stripped, sender)

    # Memory
    if cmd == "memory" or cmd.startswith("memory "):
        return _cmd_memory(text, sender=sender)
    if cmd == "myfacts":
        return _cmd_myfacts(sender)
    if cmd == "enrichsoul":
        return _cmd_enrichsoul(sender)

    # Persona
    if cmd.startswith("persona"):
        return _cmd_persona(text, sender=sender)
    if cmd == "soulversion":
        return _cmd_soulversion(sender)
    if cmd.startswith("restoresoul"):
        return _cmd_restoresoul(stripped, sender=sender)
    if cmd == "personalities":
        return _cmd_personalities(sender)

    # Admin
    if cmd.startswith("grant"):
        return _cmd_grant(stripped, actor=sender)
    if cmd.startswith("revoke"):
        return _cmd_revoke(stripped, actor=sender)
    if cmd == "admins":
        return _cmd_admins(sender)
    if cmd.startswith("ratelimit"):
        return _cmd_ratelimit(stripped, sender)
    if (
        cmd in ("ship safe cleanup", "ship cleanup", "ship greens", "triage", "triage log", "log plan", "log triage")
        or cmd.startswith("ship safe ")
    ):
        return _cmd_safe_cleanup(sender)
    if _looks_like_big_change_intake(cmd):
        return _cmd_big_change_intake(stripped, sender)
    if cmd == "changelog" or cmd == "log" or cmd.startswith("log "):
        return _cmd_log(text, sender)

    # Features
    # Sports bet tracker: /bet log, /bet settle, /bet stats
    if cmd.startswith("/bet ") or cmd.startswith("bet "):
        sub = cmd.split(None, 1)[1].lstrip("/").strip()
        if sub.startswith("log"):
            return _cmd_bet_log(text, sender)
        if sub.startswith("settle"):
            return _cmd_bet_settle(text, sender)
        if sub.startswith("stats"):
            return _cmd_bet_stats(text, sender)
    # Social bets (legacy simple system)
    if cmd == "bets" or cmd.startswith("bets "):
        return _cmd_bets(stripped, sender)
    if cmd.startswith("sharecontact"):
        return _cmd_sharecontact(stripped, sender)
    if cmd.startswith("scan"):
        return _cmd_scan(stripped, sender)
    # Workout commands — bare "workout" shows today, subcommands route to tracker
    if cmd == "workout":
        return _cmd_workout(sender)
    if cmd.startswith("/workout ") or cmd.startswith("workout "):
        sub = cmd.split(None, 1)[1].lstrip("/").strip()
        if sub.startswith("log"):
            return _cmd_workout_log(text, sender)
        if sub.startswith("summary"):
            return _cmd_workout_summary(sender)
        if sub.startswith("plan"):
            return _cmd_workout_plan(sender)
    if cmd == "scheduled":
        return _cmd_scheduled(sender)
    if cmd in ("cron help", "crons help", "jobs help"):
        return _cmd_cron_help()
    cron_id = _parse_cancel_cron_id(stripped)
    if cron_id is not None:
        from .tools import _cancel_cron_by_id
        return _cancel_cron_by_id(cron_id, sender=sender)
    if cmd in ("cron", "crons", "jobs", "cron jobs") or cmd.startswith(("cron ", "crons ", "jobs ")):
        from .tools import _list_crons
        return _list_crons(sender, scope=_cron_scope_from_text(stripped), requester_id=sender)
    if cmd.startswith("cancel"):
        return _cmd_cancel(stripped, sender)

    # Skills
    if cmd == "skills":
        return _cmd_skills(sender)
    if cmd.startswith("skill "):
        return _cmd_skill_manage(stripped, sender)

    # Group chat
    if cmd == "chats" or cmd.startswith("chats "):
        return _cmd_chats(sender, stripped)
    if cmd.startswith("ping"):
        return _cmd_ping(stripped, sender)

    # Health / drift check
    if cmd in ("drift", "weekly", "maintenance", "weekly maintenance"):
        return _cmd_drift(sender)

    return None


def _cmd_pull(sender: str) -> str:
    denial = check_action_permission(sender, "deploy")
    if denial:
        return denial
    try:
        hook_note = _ensure_git_hooks_path()
        pull = subprocess.run(
            ["git", "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=_PROJECT_DIR,
        )
        out = pull.stdout.strip() or pull.stderr.strip()
        if pull.returncode != 0:
            detail = (pull.stderr.strip() or pull.stdout.strip() or "git pull failed")[-1000:]
            return f"pull failed:\n{detail}"
        if "Already up to date" in out:
            return "Already up to date — nothing to pull."
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%h — %s (%an, %ar)"],
            capture_output=True, text=True, timeout=10, cwd=_PROJECT_DIR,
        )
        commit_info = log.stdout.strip()
        subprocess.Popen(["bash", "-c", "sleep 2 && pm2 restart davosbot"])
        return f"Pulled: {commit_info}\n{hook_note}\nrestarting..." if hook_note else f"Pulled: {commit_info}\nrestarting..."
    except Exception as e:
        return f"pull failed: {e}"


def _cmd_status(sender: str) -> str:
    denial = check_action_permission(sender, "view_logs")
    if denial:
        return denial
    # PM2 process info
    try:
        result = subprocess.run(
            ["pm2", "show", "davosbot"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            detail = redact_secret((result.stderr.strip() or result.stdout.strip() or "pm2 show failed")[-500:])
            pm2_text = f"DEGRADED: pm2 show failed: {detail}"
        else:
            lines = [l for l in result.stdout.splitlines() if any(k in l for k in ("status", "uptime", "restarts", "memory"))]
            pm2_text = "\n".join(lines) if lines else result.stdout.strip()[:500]
    except Exception as e:
        pm2_text = f"(pm2 error: {e})"

    # DB session info — formerly !status
    try:
        session_text = get_status(sender)
    except Exception as e:
        session_text = f"(session error: {e})"

    return f"PM2 Process:\n{pm2_text}\n-----\nDB Session:\n{session_text}"


def _cmd_fantasy(text: str, sender: str) -> str:
    if not is_owner(sender):
        return "The fantasy dashboard is owner-only."
    command = re.sub(r"\s+", " ", text.strip().lower())

    if command in ("fantasy requests", "fantasy request list"):
        return _format_fantasy_access_list(pending_only=True)
    if command in (
        "fantasy access",
        "fantasy access list",
        "fantasy users",
        "fantasy members",
    ):
        return _format_fantasy_access_list(pending_only=False)

    grant = re.fullmatch(
        r"fantasy\s+grant\s+#?(\d+)\s+(viewer|editor|owner)", command
    )
    if grant:
        return _fantasy_access_change(
            "grant", int(grant.group(1)), grant.group(2)
        )

    role_change = re.fullmatch(
        r"fantasy\s+(?:promote|role)\s+#?(\d+)\s+(viewer|editor|owner)",
        command,
    )
    if role_change:
        return _fantasy_access_change(
            "set_role", int(role_change.group(1)), role_change.group(2)
        )

    revoke = re.fullmatch(r"fantasy\s+revoke\s+#?(\d+)", command)
    if revoke:
        return _fantasy_access_change("revoke", int(revoke.group(1)))

    if command != "fantasy":
        return (
            "Fantasy access commands:\n"
            "- fantasy requests\n"
            "- fantasy users\n"
            "- fantasy grant #ID viewer|editor|owner\n"
            "- fantasy promote #ID viewer|editor|owner\n"
            "- fantasy revoke #ID - return access to pending"
        )

    return _fantasy_link_response()


def _fantasy_link_response(role: str | None = None) -> str:
    if not re.match(r"^https?://", FANTASY_DASHBOARD_URL, re.IGNORECASE):
        return (
            "The fantasy dashboard is ready for a URL. "
            "Set FANTASY_DASHBOARD_URL on the Mac Mini and restart DavosBot."
        )
    role_line = f"\nAccess: {role}" if role else ""
    return (
        "Fourth Down\n"
        f"{FANTASY_DASHBOARD_URL}"
        f"{role_line}\n"
        "Open it to manage the lineup, matchup, projections, and league settings."
    )


def _format_fantasy_access_list(*, pending_only: bool) -> str:
    try:
        result = fantasy_access.list_access(pending_only=pending_only)
    except fantasy_access.FantasyAccessError as exc:
        return str(exc)
    members = result.get("members", [])
    if not isinstance(members, list) or not members:
        return (
            "No pending fantasy access requests."
            if pending_only
            else "No fantasy access records yet."
        )
    heading = "Pending fantasy requests" if pending_only else "Fantasy access"
    lines = [heading]
    for member in members:
        if not isinstance(member, dict):
            continue
        label = member.get("displayName") or member.get("handle", "unknown")
        lines.append(
            "#{id} {label} - {email} - {status} - {role}".format(
                id=member.get("id", "?"),
                label=label,
                email=member.get("emailHint", "email hidden"),
                status=member.get("status", "unknown"),
                role=member.get("role", "viewer"),
            )
        )
    if pending_only:
        lines.append("Approve with: fantasy grant #ID viewer|editor|owner")
    return "\n".join(lines)


def _fantasy_access_change(
    action: str, member_id: int, role: str | None = None
) -> str:
    try:
        if action == "grant":
            result = fantasy_access.grant_access(member_id, role or "")
            verb = "granted"
        elif action == "set_role":
            result = fantasy_access.set_access_role(member_id, role or "")
            verb = "updated"
        else:
            result = fantasy_access.revoke_access(member_id)
    except fantasy_access.FantasyAccessError as exc:
        return str(exc)
    member = result.get("member", {})
    if not isinstance(member, dict):
        return "Fourth Down returned an invalid access record."
    if action == "revoke":
        return (
            f"Fantasy access #{member.get('id', member_id)} returned to pending "
            f"for {member.get('handle', 'that user')}. They are blocked and will "
            "see the request meme until approved again."
        )
    return (
        f"Fantasy access #{member.get('id', member_id)} {verb}: "
        f"{member.get('handle', 'user')} is now {member.get('role', role)}."
    )


def _cmd_group_fantasy(sender: str, chat_id: str, command: str) -> str:
    if command == "fantasy":
        role = "owner" if is_owner(sender) else None
        return _fantasy_link_response(role)
    if re.fullmatch(r"fantasy\s+request(?:\s+\S+)?", command, re.IGNORECASE):
        return (
            f"{_fantasy_link_response()}\n"
            "No email command is needed. Open the link and sign in with ChatGPT; "
            "that first sign-in creates the request automatically."
        )
    return (
        f"{_fantasy_link_response()}\n"
        "Open the link and sign in with ChatGPT to request access."
    )


def _cmd_logs(sender: str) -> str:
    denial = check_action_permission(sender, "view_logs")
    if denial:
        return denial
    try:
        result = subprocess.run(
            ["pm2", "logs", "davosbot", "--nostream", "--lines", "20"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return output[-1500:] if len(output) > 1500 else output
    except Exception as e:
        return f"logs error: {e}"


_log_clear_confirm: dict[str, float] = {}  # sender ? unix ts of last "log clear" prompt


from .change_log_triage import (
    _classify_change_request, _change_log_row_parts, _bucket_change_log_rows,
)


_LOG_BOARD_ITEM_PREVIEW_CHARS = 700

def _truncate_log_display(text: str, max_chars: int | None) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"... [truncated; {len(text)} chars full text in private export]"


def _format_bucket_item(item: tuple[int, str, str, str], *, max_chars: int | None = _LOG_BOARD_ITEM_PREVIEW_CHARS) -> str:
    row_id, request, reason, created_ts = item
    request = redact_secret(request or "")
    reason = redact_secret(reason or "")
    request = re.sub(
        r"\b(api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*['\"]?[^,\s)]+",
        r"\1=[redacted]",
        request,
        flags=re.IGNORECASE,
    )
    reason = re.sub(
        r"\b(api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*['\"]?[^,\s)]+",
        r"\1=[redacted]",
        reason,
        flags=re.IGNORECASE,
    )
    request = _truncate_log_display(request, max_chars)
    reason = _truncate_log_display(reason, max_chars)
    date = created_ts[:10] if created_ts else "unknown-date"
    suffix = f" -> {reason}" if reason else ""
    return f"#{row_id} ({date}): {request}{suffix}"


def _format_change_log_board(
    rows,
    *,
    max_per_bucket: int = 8,
    max_item_chars: int | None = _LOG_BOARD_ITEM_PREVIEW_CHARS,
) -> str:
    if not rows:
        return (
            "Change log is empty.\n"
            "Use `log [thing]` to add a backlog row.\n"
            "For screenshot/self-repair bugs, send the screenshot or exact failing text, then say "
            "`analyze this and log` or `fix yourself: [what went wrong]`.\n"
            "`ship safe cleanup` builds the Codex handoff after rows exist."
        )
    buckets = _bucket_change_log_rows(rows)
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
        shown = items[:max_per_bucket]
        sections.append(
            labels[color] + ":\n" + "\n".join(
                _format_bucket_item(item, max_chars=max_item_chars) for item in shown
            )
        )
        if len(items) > len(shown):
            sections.append(f"...and {len(items) - len(shown)} more {color.upper()} item(s).")
    sections.append("Commands: log [thing] adds + colors it. log done [id] removes it. ship safe cleanup builds a Codex handoff; it never edits or deploys.")
    return "\n\n".join(sections)


def _change_log_export_content(rows) -> str:
    board = _format_change_log_board(rows, max_per_bucket=1000, max_item_chars=None)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "# DavosBot Phone Change Log\n\n"
        f"Exported: {now}\n\n"
        f"{board}\n"
    )


def _write_change_log_export(rows=None, *, output_dir: Path | None = None) -> tuple[Path, Path]:
    rows = _fetch_change_log_rows() if rows is None else rows
    export_dir = output_dir or PROJECT_ROOT / "exports" / "private"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    content = _change_log_export_content(rows)
    stable_path = export_dir / "change_log_board.md"
    timestamped_path = export_dir / f"change_log_board_{stamp}.md"
    stable_path.write_text(content, encoding="utf-8")
    timestamped_path.write_text(content, encoding="utf-8")
    return stable_path, timestamped_path


def _refresh_change_log_export() -> str:
    """Refresh the stable private change-log snapshot after phone mutations."""
    try:
        rows = _fetch_change_log_rows()
        export_dir = PROJECT_ROOT / "exports" / "private"
        export_dir.mkdir(parents=True, exist_ok=True)
        stable_path = export_dir / "change_log_board.md"
        stable_path.write_text(_change_log_export_content(rows), encoding="utf-8")
        try:
            display_path = stable_path.relative_to(PROJECT_ROOT)
        except ValueError:
            display_path = stable_path
        return f"\nSnapshot refreshed for SSH: {display_path}"
    except Exception as exc:
        logger.warning("change log auto-export failed: %s", exc)
        return "\nSnapshot refresh failed; run `log export` later."


def _format_safe_cleanup_plan(rows) -> str:
    if not rows:
        return "Safe cleanup plan: change log is empty. Nothing to ship."
    buckets = _bucket_change_log_rows(rows)
    counts = {color: len(items) for color, items in buckets.items()}
    all_ids = [item[0] for color in ("green", "yellow", "red") for item in buckets[color]]
    green_ids = [item[0] for item in buckets["green"]]
    yellow_ids = [item[0] for item in buckets["yellow"]]
    lines = [
        "Safe cleanup plan - no code changed, no deploy run.",
        f"GREEN {counts['green']} | YELLOW {counts['yellow']} | RED {counts['red']}",
        "",
    ]
    if buckets["green"]:
        ids = ", ".join(f"#{item[0]}" for item in buckets["green"])
        lines.append(f"GREEN batch candidates: {ids}")
        lines.extend(_format_bucket_item(item) for item in buckets["green"][:8])
    else:
        lines.append("GREEN batch candidates: none.")
    lines.append("")
    if buckets["yellow"]:
        ids = ", ".join(f"#{item[0]}" for item in buckets["yellow"])
        lines.append(f"YELLOW review queue: {ids}")
        lines.extend(_format_bucket_item(item) for item in buckets["yellow"][:8])
    else:
        lines.append("YELLOW review queue: none.")
    lines.append("")
    if buckets["red"]:
        ids = ", ".join(f"#{item[0]}" for item in buckets["red"])
        lines.append(f"RED blocked: {ids}")
        lines.extend(_format_bucket_item(item) for item in buckets["red"][:8])
    else:
        lines.append("RED blocked: none.")
    lines.extend([
        "",
        "Codex handoff:",
        "Copy/paste this into Codex:",
        "",
        "You are Codex on Windows in C:\\Users\\<windows-user>\\davosbot.",
        "Do not use C:\\Users\\<windows-user>\\projects\\davosbot.",
        "Read AGENTS.md, docs/RUNBOOK.md, and docs/TASKS.md first.",
        "Pull the live phone backlog with:",
        "ssh macmini 'cd /Users/<you>/projects/davosbot && venv/bin/python scripts/export_change_log.py --stdout'",
        "Fix GREEN items only first. For YELLOW items, inspect and patch only if the fix is small, deterministic, and within the existing safety boundaries.",
        "Do not touch RED items, permissions/admin gates, private sends, memory/SOUL, reminders, cron execution, DB schema, tool gates, .env, gc_state.json, davosbot.db, backups, generated files, or exports/private.",
        "For screenshot/image/self-repair bugs: inspect davosbot/main.py image buffering and priority intake, davosbot/imessage.py attachment polling, davosbot/openai_images.py scan validation, and davosbot/commands.py self-repair context. Owner phrases like `analyze this and log`, `log this and fix it`, `ship this cron fix`, and `fix yourself:` should create review-only repair rows, not live edits.",
        "Make the smallest safe patch. Add or update tests. Run `python scripts/master_smoke.py`, then .\\scripts\\validate.ps1.",
        "If validation fails, run `python scripts/self_fix_loop.py` and do at most 3 targeted fix passes.",
        "Work in a dedicated codex/<task> branch/worktree. If validation passes, commit and push that task branch. Let the GitHub fast integrator merge eligible work into master; never push master directly.",
        "Wait for GitHub Actions. If auto-deploy advances production, run:",
        "ssh macmini 'cd /Users/<you>/projects/davosbot && venv/bin/python scripts/runtime_smoke.py'",
        "Report: changed files, completed log IDs, validation, deployed SHA, and any phone smoke still needed.",
        "",
        "After Codex reports deploy + smoke PASS:",
    ])
    shippable_ids = green_ids + yellow_ids
    if shippable_ids:
        lines.append("Text this only for the rows Codex actually completed:")
        lines.append("log done " + " ".join(f"#{row_id}" for row_id in shippable_ids))
    else:
        lines.append("No GREEN/YELLOW rows are safe to auto-close.")
    if all_ids and not buckets["red"]:
        lines.append("If every listed row is fully validated, that same log done line clears the whole board.")
    elif buckets["red"]:
        lines.append("Do not run log clear while RED rows remain; leave RED rows visible for isolated review.")
    return "\n".join(lines)


def _format_short_age(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, _seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _cleanup_runner_status_lines(rows=None, *, project_root: Path | None = None) -> list[str]:
    project_root = project_root or PROJECT_ROOT
    rows = _fetch_change_log_rows() if rows is None else rows
    buckets = _bucket_change_log_rows(rows)
    counts = {color: len(items) for color, items in buckets.items()}

    auto_deploy_dir = project_root / ".auto_deploy"
    lock_dir = auto_deploy_dir / "codex_cleanup.lock"
    lock_state = cleanup_lock_state(project_root)
    log_dir = auto_deploy_dir / "codex_cleanup_logs"
    latest_log = None
    latest_log_mtime = None

    if log_dir.exists():
        for candidate in sorted(log_dir.glob("*_safe_cleanup_*.log")):
            try:
                candidate_mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if latest_log is None or candidate_mtime >= (latest_log_mtime or 0):
                latest_log = candidate
                latest_log_mtime = candidate_mtime

    now = datetime.now(timezone.utc).timestamp()
    lines = [
        f"Codex safe cleanup status: {lock_state}.",
        f"Change log: GREEN {counts['green']} | YELLOW {counts['yellow']} | RED {counts['red']}.",
    ]
    if not rows:
        lines.append("Change log is empty.")

    if lock_dir.exists():
        try:
            lock_age = now - lock_dir.stat().st_mtime
            lines.append(f"Lock age: {_format_short_age(lock_age)}.")
        except OSError:
            lines.append("Lock age: unavailable.")

    if latest_log is not None:
        try:
            rel_log = latest_log.relative_to(project_root)
        except ValueError:
            rel_log = latest_log.name
        age = _format_short_age(now - (latest_log_mtime or now))
        label = "Current run log" if lock_state == "running" else "Last run log"
        lines.append(f"{label}: {rel_log} (updated {age} ago).")
    else:
        lines.append("Cleanup run log: none recorded yet.")

    status_path = auto_deploy_dir / "cleanup_status.json"
    try:
        last_state = json.loads(status_path.read_text(encoding="utf-8")).get("state")
        if last_state in {"finished", "failed", "timed_out"} and lock_state != "running":
            lines.append(f"Last run outcome: {last_state.replace('_', ' ')}.")
    except (OSError, ValueError, AttributeError):
        pass
    if lock_state == "stale":
        lines.append("The previous cleanup process has stopped. The next run will recover its stale lock.")
    elif lock_state == "unknown":
        lines.append("Lock ownership could not be verified. Existing work is preserved; check the Mini before retrying.")
    if lock_state in {"idle", "stale"} and rows:
        lines.append("Text `yes fix` to start now, or `ship safe cleanup` for the board.")

    return lines


def _fetch_change_log_rows():
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        return conn.execute(
            "SELECT id, request, reason, created_ts FROM change_log ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()


def _cmd_safe_cleanup(sender: str = "") -> str:
    denial = check_action_permission(sender, "view_changelog") if sender else None
    if denial:
        return denial
    return _format_safe_cleanup_plan(_fetch_change_log_rows())


def _cmd_cleanup_status(sender: str = "") -> str:
    denial = check_action_permission(sender, "view_changelog") if sender else None
    if denial:
        return denial
    return "\n".join(_cleanup_runner_status_lines())


def _can_autorun_cleanup_here() -> bool:
    return os.name != "nt"


def _cmd_confirmed_safe_cleanup(sender: str = "") -> str:
    return _confirmed_safe_cleanup_result(sender)["result"]


def _confirmed_safe_cleanup_result(sender: str) -> dict:
    """Launch the fixed cleanup runner once; acceptance is not repair completion.

    iMessage keeps its text reply. The authenticated Work adapter consumes the
    same native permission checks and typed result without interpreting prose.
    The supervisor, not Popen, owns the inter-process execution lock.
    """
    def result(state, message, status="error"):
        return {"status": status, "result": message, "evidence": {
            "launch_state": state, "scope": "safe_backlog",
            "repairs_verified": False,
        }}

    denial = check_action_permission(sender, "deploy") if sender else None
    if denial:
        return result("denied", denial)
    rows = _fetch_change_log_rows()
    if not rows:
        return result("empty", "Change log is empty. Nothing for Codex to fix.", "ok")

    lock_state = cleanup_lock_state(PROJECT_ROOT)
    if lock_state in {"running", "unknown"}:
        return result("already_running" if lock_state == "running" else "lock_unknown",
                      "\n".join(_cleanup_runner_status_lines(rows)),
                      "ok" if lock_state == "running" else "error")

    script = PROJECT_ROOT / "scripts" / "nightly_safe_cleanup_codex.sh"
    if not script.exists():
        return result("runner_missing", "Cleanup runner is missing. Use `master prompt` and run it from phone Codex.")

    if not _can_autorun_cleanup_here():
        return result("mini_required",
            "Auto-run cleanup is Mini-only. Here is the phone Codex prompt instead:\n\n"
            + _format_safe_cleanup_plan(rows)
        )

    try:
        subprocess.Popen(
            ["/bin/bash", str(script), "--confirmed", "--notify"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("confirmed safe cleanup launch failed: %s", type(exc).__name__)
        return result("launch_failed", f"Could not start Codex cleanup: {type(exc).__name__}. Use `master prompt` as fallback.")

    buckets = _bucket_change_log_rows(rows)
    counts = {color: len(items) for color, items in buckets.items()}
    return result("launch_requested",
        "On it. Starting Codex safe cleanup on the Mini now. "
        "I will text when it finishes.\n"
        f"Current log: GREEN {counts['green']} | YELLOW {counts['yellow']} | RED {counts['red']}.\n"
        "Lock is enabled, so nightly cleanup will skip if this is still running.",
        "accepted",
    )


_SELF_REPAIR_ANALYZE_VERB_RE = r"(?:analy[sz]e|anayl[sz]e|anal[sz]ye|anly[sz]e|analzye|anyl[sz]e|anali[sz]e)"
_SELF_REPAIR_INTAKE_RE = re.compile(
    r"^(?:"
    r"(?P<direct>"
    r"(?:fix|debug|repair|diagnose)\s+(?:yourself|davos(?:bot)?|your)(?=\s|:|$)|"
    r"self[-\s]?review|"
    r"self[-\s]?diagnose|"
    r"diagnose\s+yourself|"
    r"debug\s+yourself|"
    r"repair\s+yourself|"
    r"what\s+went\s+wrong"
    r")\s*:?\s*(?P<issue>.*)"
    r"|(?P<full>"
    r"(?:fix|debug|repair|diagnose)\s+(?:this|that|it|yourself)\b.*|"
    r"(?:this|that|it)\s+(?:failed|broke|didn'?t\s+work|doesn'?t\s+work|is\s+broken)\b.*|"
    r"(?:log|logg|lgo|record|capture)\b.{0,140}\b(?:fix|repair|debug|ship|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b.*|"
    r"(?:fix|repair|debug)\b.{0,140}\b(?:log|record|ship|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b.*|"
    r"ship\s+(?:this|that|it)\b.{0,140}\b(?:fix|repair|debug|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b.*|"
    rf"{_SELF_REPAIR_ANALYZE_VERB_RE}\b.{{0,140}}\b(?:log|record|fix|repair|debug|ship|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b.*"
    r")"
    r")$",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_self_repair_request(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip()).replace("’", "'")
    addressed = False
    for _ in range(4):
        prefix = re.match(r"^(hey|okay|ok|please|pls|plz|can\s+you|could\s+you|would\s+you|@?davos(?:bot)?)\b[,\s]+", clean, re.I)
        if not prefix:
            break
        addressed = addressed or "davos" in prefix.group(1).lower()
        clean = clean[prefix.end():]
    if addressed:
        direct = re.match(r"^(?:fix|debug|repair|diagnose)\s+(.+)$", clean, re.I)
        ordinary_edit = direct and re.match(r"^(?:this|that|my|the)\s+(?:sentence|paragraph|grammar|spelling|resume|essay|email|code|math|formula)\b", direct.group(1), re.I)
        if direct and not ordinary_edit and not re.match(r"^(?:yourself|your|davos(?:bot)?)\b", direct.group(1), re.I):
            clean = "fix yourself: " + direct.group(1)
    return clean


def _looks_like_self_repair_intake(text: str) -> bool:
    clean = _normalize_self_repair_request(text)
    if not _SELF_REPAIR_INTAKE_RE.match(clean):
        return False
    short_fix = re.match(
        r"^(?:fix|debug|repair|diagnose)\s+(?:this|that|it)\b(?P<tail>.*)$",
        clean,
        re.IGNORECASE,
    )
    if short_fix:
        tail = (short_fix.group("tail") or "").strip(" \t\r\n.!?,:;'\"")
        if tail and not re.search(
            r"\b(?:again|because|failed|failure|broke|broken|wrong|bug|cron|reminder|image|"
            r"screenshot|answer|reply|response|message|log|record|ship|repair|fix|debug|"
            r"diagnose|doesn'?t\s+work|didn'?t\s+work)\b",
            tail,
            re.IGNORECASE,
        ):
            return False
    return True


def _parse_self_repair_issue(text: str) -> str:
    m = _SELF_REPAIR_INTAKE_RE.match(_normalize_self_repair_request(text))
    if not m:
        issue = text
    elif m.groupdict().get("issue") is not None:
        issue = m.group("issue")
    else:
        issue = m.group("full")
    issue = re.sub(r"\s+", " ", (issue or "").strip()).strip()
    return issue or "Review the most recent answer and identify what went wrong."


def _self_repair_issue_needs_clarification(issue: str) -> bool:
    clean_issue = re.sub(r"\s+", " ", (issue or "").strip())
    normalized_issue = clean_issue.replace("’", "'").replace("`", "'")
    if clean_issue == "Review the most recent answer and identify what went wrong.":
        return True
    if re.fullmatch(
        r"(?:fix|debug|repair|diagnose)\s+(?:this|that|it|yourself)"
        r"(?:\s+(?:again|please|now|asap|for\s+real))?[.!?]*",
        normalized_issue,
        re.IGNORECASE,
    ):
        return True
    if re.fullmatch(
        r"ship\s+(?:this|that|it)\s+fix(?:\s+so\s+it\s+doesn'?t\s+happen\s+again)?"
        r"(?:\s+(?:please|now|asap))?[.!?]*",
        normalized_issue,
        re.IGNORECASE,
    ):
        return True
    return False


def _classify_self_repair_issue(issue: str) -> tuple[str, str]:
    lower = (issue or "").lower()
    if re.search(
        r"\b(permission|admin|password|private\s+(?:send|message|text)|send_imessage|"
        r"memory|soul|schema|migration|database|db\s+schema|tool\s+gate|owner[-\s]?only|"
        r"write_file|shell_exec|deploy|self[-\s]?edit|auto[-\s]?push)\b",
        lower,
    ):
        return "security_boundary", "Touches a permission, private-send, memory, DB, deploy, or self-edit boundary; handle as code-red review first."
    if re.search(r"\b(gemini|ollama|model|routing|fallback|billing|token|cost|usage|missing\s+parts|agentic)\b", lower):
        return "model_routing", "Likely needs model-routing, fallback, or usage logging review before swapping models."
    if re.search(r"\b(spreadsheet|sheet|excel|workbook|xlsx|csv|file|context|rows?|deck)\b", lower):
        return "missing_context", "Likely needed explicit file/context handling instead of guessing from conversation."
    if re.search(r"\b(reminder|cron|scheduled?|alarm|didn'?t\s+(?:fire|send|go\s+off))\b", lower):
        return "deterministic_routing", "Likely needed a deterministic command/tool route with a DB postcondition."
    if re.search(r"\b(log|ship\s+safe|change\s+log|work\s+queue|command|permission|admin)\b", lower):
        return "command_routing", "Likely needed command self-knowledge or a direct command route before the LLM."
    if re.search(r"\b(image|photo|picture|scan|vision|generate)\b", lower):
        return "media_tooling", "Likely needed an image/vision route, permission gate, or clearer media fallback."
    if re.search(r"\b(error|traceback|exception|crash|missing\s+parts|failed|failure)\b", lower):
        return "runtime_error", "Likely needs log inspection before any patch."
    if re.search(r"\b(wrong|confused|dumb|bad|made\s+up|hallucinat|missed)\b", lower):
        return "answer_quality", "Likely answered with weak context or failed to ask a clarifying question."
    return "answer_quality", "Likely needs a context/routing review before changing model selection."


def _self_repair_risk(category: str, issue: str) -> str:
    lower = (issue or "").lower()
    if category == "security_boundary":
        return "RED"
    if category == "deterministic_routing" and re.search(r"\b(reminder|cron|scheduled?|database|db)\b", lower):
        return "RED"
    return "YELLOW"


def _self_repair_prompts(category: str, risk: str) -> tuple[str, str, str]:
    guides = {
        "security_boundary": (
            "Mini read-only only. Use a separate worktree/clone for isolated validation; do not switch the live production checkout.",
            "CODE RED review first. Inspect permissions/private-send/memory/DB/deploy boundaries, report exact risks, and patch only after the owner approves the isolated plan.",
            "Add focused permission/security tests before any patch; Mini validation must avoid DB writes and outbound messages unless explicitly approved.",
        ),
        "model_routing": (
            "Check PM2 logs and gemini_usage metadata only; do not print prompts, secrets, or raw chats.",
            "Review brain.py model routing, Gemini agentic fallback, usage logging, and billing command. Propose model changes separately from permission/tool changes.",
            "Run routing/unit tests plus a billing command smoke after pull; confirm no owner-only tool access widened.",
        ),
        "missing_context": (
            "If live context is needed, inspect only redacted message metadata or a user-provided file; do not dump transcripts.",
            "Inspect preflight/context handling. Patch Davos to ask for the missing file/context instead of guessing, with a regression test.",
            "Smoke with the original ask and a no-file spreadsheet/business ask; expected behavior is a clear context request.",
        ),
        "deterministic_routing": (
            "Inspect relevant DB rows/log metadata read-only first; do not change reminders, crons, or scheduled tasks on Mini.",
            "Patch the smallest deterministic route before the LLM, add a postcondition when there is a side effect, and keep originating-chat routing intact.",
            "Add tests for both the route and the failure/no-side-effect case; Mini smoke should verify PM2 and DB metadata only.",
        ),
        "command_routing": (
            "Runtime validation can be compile/tests/PM2 plus phone smoke; no DB surgery.",
            "Inspect command parsing before LLM. Add a direct parser or self-knowledge injection so Davos does not bluff about its commands.",
            "Add command parser tests and a phone smoke phrase matching the owner's original wording.",
        ),
        "media_tooling": (
            "Validate image paths/counts only; do not expose private media contents.",
            "Inspect image buffering, Gemini vision path, permission gates, and chat-safe output handling before adding any new media tool.",
            "Add tests for owner/admin gates, rate/cost limits, and safe failure text.",
        ),
        "runtime_error": (
            "Mini read-only logs first: PM2 status/logs, structured bot_log metadata, latest commit, repo clean.",
            "Reproduce from logs or a focused unit test before patching. Avoid broad fallback/model swaps without evidence.",
            "Run focused regression plus compileall; Mini smoke confirms no fresh traceback after pull.",
        ),
        "answer_quality": (
            "Runtime validation is usually phone smoke; logs only if a concrete failure timestamp exists.",
            "Improve ask-vs-answer behavior, capability-gap detection, or deterministic intake. Do not change model routing unless the review proves it is the cause.",
            "Add tests for the exact complaint phrase and one nearby normal-chat non-match.",
        ),
    }
    default = guides["answer_quality"]
    return guides.get(category, default)


def _truncate_self_repair_field(value: object, max_chars: int = 900) -> str:
    safe = redact_secret("" if value is None else str(value))
    safe = re.sub(r"\s+", " ", safe).strip()
    if len(safe) > max_chars:
        return safe[:max_chars].rstrip() + "..."
    return safe


def _self_repair_table_columns(conn, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _self_repair_table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _summarize_self_repair_payload(raw: object) -> dict:
    if raw is None or raw == "":
        return {}
    text = redact_secret(str(raw))
    try:
        parsed = json.loads(text)
    except Exception:
        return {"preview": _truncate_self_repair_field(text, 180)}
    if isinstance(parsed, dict):
        summary = {"keys": sorted(str(key) for key in parsed.keys())[:12]}
        for key in ("event", "action", "status", "outcome", "error", "error_count", "count"):
            if key in parsed:
                summary[key] = _truncate_self_repair_field(parsed.get(key), 120)
        return summary
    if isinstance(parsed, list):
        return {"items": len(parsed)}
    return {"value": _truncate_self_repair_field(parsed, 120)}


def _recent_bot_log_context(limit: int = 5) -> list[dict]:
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            if not _self_repair_table_exists(conn, "bot_log"):
                return [{"status": "bot_log table missing"}]
            columns = _self_repair_table_columns(conn, "bot_log")
            selected = [
                column
                for column in ("id", "timestamp", "sender", "event_type", "exc_type", "exc_msg", "payload")
                if column in columns
            ]
            if not selected:
                return [{"status": "bot_log columns unavailable"}]
            rows = conn.execute(
                f"SELECT {', '.join(selected)} FROM bot_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception as exc:
        return [{"status": f"bot_log unavailable: {type(exc).__name__}"}]

    entries = []
    for row in rows:
        raw = dict(zip(selected, row))
        entry = {}
        if "id" in raw:
            entry["id"] = raw["id"]
        if "timestamp" in raw:
            entry["timestamp"] = raw["timestamp"]
        if "sender" in raw and raw["sender"]:
            entry["sender_tail"] = str(raw["sender"])[-6:]
        if "event_type" in raw and raw["event_type"]:
            entry["event_type"] = _truncate_self_repair_field(raw["event_type"], 120)
        if "exc_type" in raw and raw["exc_type"]:
            entry["exc_type"] = _truncate_self_repair_field(raw["exc_type"], 120)
        if "exc_msg" in raw and raw["exc_msg"]:
            entry["exc_msg"] = _truncate_self_repair_field(raw["exc_msg"], 220)
        if "payload" in raw and raw["payload"]:
            entry["payload"] = _summarize_self_repair_payload(raw["payload"])
        entries.append(entry)
    return entries or [{"status": "bot_log empty"}]


def _self_repair_count(conn, table: str, where: str = "") -> int | None:
    if not _self_repair_table_exists(conn, table):
        return None
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(conn.execute(sql).fetchone()[0])
    except Exception:
        return None


def _self_repair_latest_rows(conn, table: str, wanted_columns: tuple[str, ...], limit: int = 3) -> list[dict]:
    if not _self_repair_table_exists(conn, table):
        return []
    columns = _self_repair_table_columns(conn, table)
    selected = [column for column in wanted_columns if column in columns]
    if not selected:
        return []
    order_column = "id" if "id" in columns else selected[0]
    try:
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM {table} ORDER BY {order_column} DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return []
    result = []
    for row in rows:
        item = {}
        for column, value in zip(selected, row):
            if column in ("action_payload", "payload"):
                item[column] = _summarize_self_repair_payload(value)
            elif column == "request":
                item[column] = _truncate_self_repair_field(value, 180)
                if value is not None:
                    item[f"{column}_len"] = len(str(value))
            elif column in ("message", "content"):
                item[f"{column}_len"] = len(str(value)) if value is not None else 0
            elif column in ("sender", "chat_id", "origin_chat_id", "recipient", "created_by"):
                item[f"{column}_tail"] = str(value)[-6:] if value else ""
            else:
                item[column] = _truncate_self_repair_field(value, 160)
        result.append(item)
    return result


def _self_repair_db_snapshots(issue: str, category: str) -> dict:
    lower = (issue or "").lower()
    snapshots: dict[str, object] = {}
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            change_total = _self_repair_count(conn, "change_log")
            if change_total is not None:
                snapshots["change_log"] = {
                    "total": change_total,
                    "latest": _self_repair_latest_rows(conn, "change_log", ("id", "request", "created_ts"), 3),
                }
            bot_total = _self_repair_count(conn, "bot_log")
            if bot_total is not None:
                snapshots["bot_log"] = {"total": bot_total}
            if category == "deterministic_routing" or re.search(r"\b(cron|scheduled?|reminder|alarm)\b", lower):
                cron_total = _self_repair_count(conn, "cron_jobs")
                if cron_total is not None:
                    snapshots["cron_jobs"] = {
                        "total": cron_total,
                        "enabled": _self_repair_count(conn, "cron_jobs", "enabled = 1"),
                        "latest": _self_repair_latest_rows(
                            conn,
                            "cron_jobs",
                            ("id", "cron_expression", "action_type", "enabled", "last_run", "action_payload", "created_by"),
                            3,
                        ),
                    }
                reminder_total = _self_repair_count(conn, "reminders")
                if reminder_total is not None:
                    snapshots["reminders"] = {
                        "total": reminder_total,
                        "latest": _self_repair_latest_rows(
                            conn,
                            "reminders",
                            ("id", "message", "due_ts", "sent", "origin_chat_id", "chat_id"),
                            3,
                        ),
                    }
            if category in ("media_tooling", "answer_quality", "command_routing"):
                message_total = _self_repair_count(conn, "messages")
                if message_total is not None:
                    snapshots["messages"] = {
                        "total": message_total,
                        "latest_metadata": _self_repair_latest_rows(
                            conn,
                            "messages",
                            ("id", "sender", "role", "content", "ts"),
                            3,
                        ),
                    }
    except Exception as exc:
        snapshots["status"] = f"db snapshot unavailable: {type(exc).__name__}"
    return snapshots or {"status": "no db snapshot available"}


def _self_repair_likely_code_area(category: str, issue: str) -> str:
    lower = (issue or "").lower()
    if category == "security_boundary":
        return "davosbot/permissions.py, davosbot/tools.py owner-only gates, private-send confirmation paths, and related tests"
    if category == "model_routing":
        return "davosbot/brain.py model/tool routing, davosbot/config.py route labels, davosbot/commands.py model status/options"
    if category == "missing_context":
        return "davosbot/main.py preflight/context handling plus the file/image route that should ask for missing context"
    if category == "deterministic_routing":
        if "sports" in lower and "cron" in lower:
            return "davosbot/main.py cron runner, davosbot/tools.py sports recap cron parser, davosbot/commands.py cron listing/log handoff"
        if "cron" in lower:
            return "davosbot/main.py cron runner, davosbot/tools.py cron create/edit helpers, davosbot/commands.py cron commands"
        if "reminder" in lower:
            return "davosbot/main.py reminder dispatch and davosbot/memory.py reminder persistence"
        return "davosbot/main.py deterministic dispatch and davosbot/tools.py side-effect helpers"
    if category == "command_routing":
        return "davosbot/main.py priority intake routing and davosbot/commands.py handle_command parser"
    if category == "media_tooling":
        return "davosbot/main.py image buffering/intake, davosbot/openai_images.py scan/generation, davosbot/image_access.py quotas"
    if category == "runtime_error":
        return "davosbot/main.py runtime loop, davosbot/brain.py error logging, PM2/runtime smoke scripts"
    return "davosbot/main.py dispatch, davosbot/brain.py response routing, and focused tests for the exact owner phrase"


def _self_repair_expected_behavior(category: str, issue: str, image_scan_result: str | None = None) -> str:
    lower = (issue or "").lower()
    if image_scan_result:
        return "With an attached screenshot/image, Davos scans it, logs a review-only self-repair row with the scan result, and never routes the phrase as a plain log or casual chat."
    if category == "deterministic_routing":
        return "Side-effect intent routes deterministically, verifies or reports the postcondition, and logs/asks for missing context instead of falling through silently."
    if category == "command_routing":
        return "Log/fix/ship command intent reaches the guarded intake or asks a short clarification; it must not be swallowed by normal chat or a generic log row."
    if category == "media_tooling":
        return "Image scan/generation intent uses the configured media route with permission/cost gates and clear failure text when context or provider support is missing."
    if category == "model_routing":
        return "Model changes are reviewed as routing policy with cost and permission invariants; model choice never bypasses owner-only tool gates."
    if re.search(r"\b(didn'?t\s+work|failed|broke|wrong)\b", lower):
        return "Failure feedback creates a Codex-ready repair handoff or asks for the missing artifact/context instead of doing nothing."
    return "Davos acknowledges the repair/log intent, captures enough context for Codex, and asks for clarification when the requested fix target is unclear."


def _build_self_repair_intake(
    issue: str,
    *,
    image_scan_result: str | None = None,
    source: str = "self_repair_intake",
) -> tuple[str, str, str, str, str]:
    safe_issue = redact_secret(issue or "").strip()
    if len(safe_issue) > 2200:
        safe_issue = safe_issue[:2200].rstrip() + "..."
    safe_scan = _truncate_self_repair_field(image_scan_result, 1200) if image_scan_result else ""
    classification_text = f"{safe_issue} {safe_scan}".strip()
    category, diagnosis = _classify_self_repair_issue(classification_text)
    risk = _self_repair_risk(category, classification_text)
    mini_prompt, codex_prompt, validation_prompt = _self_repair_prompts(category, risk)
    summary_source = safe_issue
    if safe_scan:
        summary_source = f"{safe_issue} | image scan: {safe_scan}" if safe_issue else f"image scan: {safe_scan}"
    summary = _truncate_self_repair_field(summary_source, 180)
    request = f"[SELF-REPAIR {risk}] {summary}"
    recent_logs = _recent_bot_log_context()
    db_snapshots = _self_repair_db_snapshots(classification_text, category)
    likely_code_area = _self_repair_likely_code_area(category, classification_text)
    expected_behavior = _self_repair_expected_behavior(category, safe_issue, safe_scan or None)
    reason = "\n".join([
        "type=self_repair_intake",
        f"source={_truncate_self_repair_field(source, 120)}",
        f"risk={risk}",
        "status=review_only",
        f"category={category}",
        f"diagnosis={diagnosis}",
        f"message_text={safe_issue}",
        f"image_scan_result={safe_scan or 'not_provided'}",
        f"recent_bot_logs={json.dumps(recent_logs, sort_keys=True)}",
        f"relevant_db_rows={json.dumps(db_snapshots, sort_keys=True)}",
        f"likely_code_area={likely_code_area}",
        f"exact_expected_behavior={expected_behavior}",
        "expected_bot_behavior=acknowledge owner feedback, avoid bluffing, ask for missing context, and prepare a concrete Codex repair handoff",
        "safe_auto_fix_pipeline=Codex only: create a codex/... branch/worktree, patch, test, push, wait for CI, then Mini deploy/smoke; Davos must not edit production directly.",
        "blocked_actions=no live self-edit, no deploy, no shell/file/DB mutation outside change_log",
        f"mini_prompt={mini_prompt}",
        f"codex_prompt={codex_prompt}",
        f"validation_prompt={validation_prompt}",
        f"owner_feedback={safe_issue}",
    ])
    return request, reason, category, diagnosis, risk


def _log_self_repair_intake(
    issue: str,
    sender: str = "",
    *,
    image_scan_result: str | None = None,
    source: str = "self_repair_intake",
) -> str:
    denial = check_action_permission(sender, "view_changelog") if sender else None
    if denial:
        return denial
    request, reason, category, diagnosis, risk = _build_self_repair_intake(
        issue,
        image_scan_result=image_scan_result,
        source=source,
    )
    with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO change_log (request, reason) VALUES (?, ?)",
            (request, reason),
        )
        row_id = cur.lastrowid
        conn.commit()
    return (
        f"Self-repair logged #{row_id} [{risk}/{category}].\n"
        f"My first read: {diagnosis}\n"
        "Review-only: I did not edit code, deploy, or mutate runtime config.\n"
        "Next: text 'ship safe cleanup' for the Codex handoff."
        f"{_refresh_change_log_export()}"
    )


def _cmd_self_repair_intake(text: str, sender: str = "") -> str:
    issue = _parse_self_repair_issue(text)
    if _self_repair_issue_needs_clarification(issue):
        denial = check_action_permission(sender, "view_changelog") if sender else None
        if denial:
            return denial
        return (
            "I can turn that into a repair handoff, but I still need the exact failing behavior.\n"
            "Reply with `fix yourself: [what went wrong]`, or send the screenshot and say `analyze this and log`.\n"
            "I did not create a change-log row yet."
        )
    return _log_self_repair_intake(issue, sender, source="self_repair_command")


_BIG_CHANGE_INTAKE_RE = re.compile(
    r"^(?:"
    r"big\s+change|bigchange|big-change|"
    r"codex\s+(?:plan|review|intake)|"
    r"intake"
    r")\s*:?\s*(?P<idea>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _looks_like_big_change_intake(text: str) -> bool:
    return bool(_BIG_CHANGE_INTAKE_RE.match((text or "").strip()))


def _parse_big_change_intake(text: str) -> str | None:
    m = _BIG_CHANGE_INTAKE_RE.match((text or "").strip())
    if not m:
        return None
    idea = re.sub(r"\s+", " ", (m.group("idea") or "")).strip()
    return idea or None


def _build_big_change_intake(idea: str) -> tuple[str, str, str]:
    safe_idea = redact_secret(idea or "").strip()
    if len(safe_idea) > 2200:
        safe_idea = safe_idea[:2200].rstrip() + "..."
    risk = _classify_change_request(safe_idea)
    review_risk = "red" if risk == "red" else "yellow"
    summary = safe_idea[:180].rstrip()
    request = f"[BIG-CHANGE {review_risk.upper()}] {summary}"
    reason = "\n".join([
        "type=big_change_intake",
        f"risk={review_risk.upper()}",
        "status=review_only",
        "allowed_action=Codex investigation/plan only",
        "blocked_actions=no file edits, no deploy, no cross-chat DB writes, no model/runtime config changes",
        "codex_prompt=Investigate this request first. Propose a smallest safe patch plan, validation, rollback, and owner questions. Do not patch until the owner approves.",
        f"owner_idea={safe_idea}",
    ])
    return request, reason, review_risk


def _cmd_big_change_intake(text: str, sender: str = "") -> str:
    denial = check_action_permission(sender, "view_changelog") if sender else None
    if denial:
        return denial
    idea = _parse_big_change_intake(text)
    if not idea:
        return "Use: big change [idea]  |  codex plan [idea]  |  intake [idea]"
    request, reason, risk = _build_big_change_intake(idea)
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO change_log (request, reason) VALUES (?, ?)",
            (request, reason),
        )
        row_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return (
        f"Big-change intake logged #{row_id} [{risk.upper()}].\n"
        "Review-only: no code changed, no deploy run, no DB/runtime config mutated.\n"
        "Next: text 'ship safe cleanup' and paste the board to Codex."
        f"{_refresh_change_log_export()}"
    )


def _parse_image_access_target(raw: str) -> str:
    target = (raw or "").strip()
    if not target:
        return ""
    try:
        from .brain import resolve_contact

        resolved = resolve_contact(target)
        if resolved:
            target = resolved
    except Exception:
        pass
    return normalize_handle(target) or target


def _cmd_image_access(text: str, sender: str = "") -> str:
    denial = check_action_permission(sender, "manage_image_access") if sender else None
    if denial:
        return denial

    parts = text.strip().split()
    if len(parts) < 3:
        return (
            "Usage: image access status [handle] / revoke [handle] / "
            "allow [handle] / extend [handle] / reset [handle]"
        )

    action = parts[2].lower()
    target_raw = " ".join(parts[3:]).strip()
    if action in {"status", "show"}:
        target = _parse_image_access_target(target_raw or sender)
        from .image_access import format_image_access_status

        return format_image_access_status(target)

    if action in {"+5", "extend"}:
        target = _parse_image_access_target(target_raw)
        if not target:
            return "Usage: image access extend [handle]"
        from .image_access import format_image_access_status, record_image_access_policy

        record_image_access_policy(sender, target, "extend", amount=5)
        return "Extended by 5. " + format_image_access_status(target)

    if action in {"revoke", "allow", "reset"}:
        target = _parse_image_access_target(target_raw)
        if not target:
            return f"Usage: image access {action} [handle]"
        from .image_access import format_image_access_status, record_image_access_policy

        record_image_access_policy(sender, target, action)
        prefix = {
            "revoke": "Revoked image access. ",
            "allow": "Restored image access. ",
            "reset": "Reset image access allowance. ",
        }[action]
        return prefix + format_image_access_status(target)

    return (
        "Usage: image access status [handle] / revoke [handle] / "
        "allow [handle] / extend [handle] / reset [handle]"
    )


def _latest_loggable_turn(sender: str) -> str:
    if not sender:
        return ""
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE sender = ? ORDER BY id DESC LIMIT 12",
                (sender,),
            ).fetchall()
    except Exception as exc:
        logger.warning("latest loggable turn lookup failed: %s", exc)
        return ""
    for role, content in rows:
        if role != "assistant" or not content:
            continue
        lowered = content.lower()
        if lowered.startswith(("logged [", "change log", "exported ", "triage board")):
            continue
        return content.strip()
    for _role, content in rows:
        if content:
            return content.strip()
    return ""


def _log_payload_from_subcommand(raw_subcmd: str, sender: str) -> str:
    raw = (raw_subcmd or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    previous_patterns = (
        "that msg entirely",
        "that message entirely",
        "the last msg entirely",
        "the last message entirely",
        "last msg entirely",
        "last message entirely",
        "previous msg entirely",
        "previous message entirely",
    )
    if lower in previous_patterns:
        return _latest_loggable_turn(sender)
    match = re.match(
        r"^(?:that|the\s+last|last|previous)\s+(?:msg|message)\s+entirely"
        r"\s+(?:and\s+)?(?:also\s+)?(?:note|add|say)\s+(?P<note>.+)$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        previous = _latest_loggable_turn(sender)
        note = match.group("note").strip(" :.-\n\t")
        if previous and note:
            return f"{previous}\n\nOwner note: {note}"
        return previous or note
    match = re.match(
        r"^(?:this\s+)?(?:message|msg)\s+(?:entirely|fully|whole|verbatim)\s*:?\s*(?P<payload>.*)$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        payload = match.group("payload").strip()
        return payload or _latest_loggable_turn(sender)
    match = re.match(r"^(?:this|that)\s*:\s*(?P<payload>.*)$", raw, flags=re.IGNORECASE | re.DOTALL)
    if match:
        payload = match.group("payload").strip()
        return payload or _latest_loggable_turn(sender)
    return raw


_LOG_REPLY_PREVIEW_CHARS = 700


def _format_logged_change_reply(row_id: int, color: str, payload: str) -> str:
    safe_payload = redact_secret(payload or "")
    preview = re.sub(r"\s+", " ", safe_payload).strip()
    if len(preview) > _LOG_REPLY_PREVIEW_CHARS:
        preview = preview[:_LOG_REPLY_PREVIEW_CHARS].rstrip() + "..."
    if not preview:
        preview = "(empty)"
    return "\n".join([
        f"Logged [{color}] #{row_id} ({len(payload or '')} chars).",
        f"Preview: {preview}",
        "Full text saved in the private change-log export.",
        "Text 'ship safe cleanup' for a Codex-ready triage board.",
    ])


def _cmd_log(text: str = "log", sender: str = "") -> str:
    denial = check_action_permission(sender, "view_changelog") if sender else None
    if denial:
        return denial
    import time as _t
    parts = text.strip().split(None, 1)
    raw_subcmd = parts[1].strip() if len(parts) > 1 else ""
    subcmd = raw_subcmd.lower().strip()

    update_match = re.match(
        r"^(?:update|edit|revise)\s+#?(?P<id>\d+)\s*(?:(?:to|with)\s+)?(?P<summary>.+?)\s*$",
        raw_subcmd,
        flags=re.IGNORECASE,
    )
    if update_match:
        item_id = int(update_match.group("id"))
        payload = update_match.group("summary").strip(" “”\"")
        if not payload:
            return "Usage: log update [id] [new summary]"
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            cur = conn.execute(
                "UPDATE change_log SET request = ? WHERE id = ?",
                (payload, item_id),
            )
            conn.commit()
        finally:
            conn.close()
        if not cur.rowcount:
            return f"Log #{item_id} not found."
        color = _classify_change_request(payload).upper()
        preview = payload.strip()
        if len(preview) > _LOG_REPLY_PREVIEW_CHARS:
            preview = preview[:_LOG_REPLY_PREVIEW_CHARS].rstrip() + "..."
        if not preview:
            preview = "(empty)"
        return "\n".join([
            f"Updated log #{item_id} [{color}] ({len(payload)} chars).",
            f"Preview: {preview}",
            "Full text saved in the private change-log export.",
        ]) + _refresh_change_log_export()

    if subcmd.startswith("remove ") or subcmd.startswith("done "):
        _, rest = subcmd.split(None, 1)
        item_ids = []
        for match in re.findall(r"#?(\d+)", rest):
            item_id = int(match)
            if item_id not in item_ids:
                item_ids.append(item_id)
        if not item_ids:
            return "Usage: log remove [id] / log done [id]. Multiple IDs are OK: log done #1 #2 #3"
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            placeholders = ",".join("?" for _ in item_ids)
            existing = {
                int(row[0])
                for row in conn.execute(
                    f"SELECT id FROM change_log WHERE id IN ({placeholders})",
                    item_ids,
                ).fetchall()
            }
            cur = conn.execute(
                f"DELETE FROM change_log WHERE id IN ({placeholders})",
                item_ids,
            )
            conn.commit()
        finally:
            conn.close()
        if len(item_ids) == 1:
            item_id = item_ids[0]
            if cur.rowcount:
                return f"Log #{item_id} removed.{_refresh_change_log_export()}"
            return f"Log #{item_id} not found."
        removed = [item_id for item_id in item_ids if item_id in existing]
        missing = [item_id for item_id in item_ids if item_id not in existing]
        parts = []
        if removed:
            parts.append("Removed logs: " + ", ".join(f"#{item_id}" for item_id in removed) + ".")
        if missing:
            parts.append("Not found: " + ", ".join(f"#{item_id}" for item_id in missing) + ".")
        if removed:
            parts.append(_refresh_change_log_export().lstrip())
        return " ".join(parts) if parts else "No matching log rows."

    if subcmd == "clear":
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            n = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
        finally:
            conn.close()
        _log_clear_confirm[sender] = _t.time()
        return f"Are you sure? Reply 'log clear confirm' to wipe all {n} entries."

    if subcmd == "clear confirm":
        prompted_at = _log_clear_confirm.get(sender, 0)
        if _t.time() - prompted_at > 60:
            return "Confirm window expired. Send 'log clear' first."
        # Count + delete inside the connection's implicit transaction; VACUUM must
        # run OUTSIDE any transaction (sqlite3 raises "cannot VACUUM from within
        # a transaction" if it shares the connection's autobegun txn).
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            n = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
            conn.execute("DELETE FROM change_log")
            conn.commit()
        finally:
            conn.close()
        # New connection in autocommit mode for VACUUM.
        try:
            _vac = sqlite3.connect(BOT_DB_PATH, isolation_level=None)
            _vac.execute("VACUUM")
            _vac.close()
        except Exception as _e:
            logger.warning("VACUUM after log clear skipped: %s", _e)
        _log_clear_confirm.pop(sender, None)
        try:
            import json as _json
            conn = sqlite3.connect(BOT_DB_PATH)
            try:
                conn.execute(
                    "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                    (sender, "log_cleared", _json.dumps({"count": n})),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return f"Change log cleared. {n} entries removed. Ready for fresh logging.{_refresh_change_log_export()}"

    if subcmd in ("export", "snapshot", "save"):
        stable_path, timestamped_path = _write_change_log_export()
        count = len(_fetch_change_log_rows())
        return (
            f"Exported {count} change-log row(s) to:\n"
            f"{stable_path}\n"
            f"Snapshot: {timestamped_path}\n"
            "This is local/private and gitignored. SSH can read it without copying from phone."
        )

    if subcmd in ("plan", "triage", "safe cleanup", "ship safe cleanup"):
        return _format_safe_cleanup_plan(_fetch_change_log_rows())

    if subcmd == "board":
        return _format_change_log_board(_fetch_change_log_rows())

    # "log [text]" -> write intent: log the text as a change request
    if subcmd and subcmd not in ("clear", "clear confirm"):
        payload = _log_payload_from_subcommand(raw_subcmd, sender)
        if not payload:
            return "I couldn't find the previous message to log. Paste the text after `log this:`."
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            cur = conn.execute("INSERT INTO change_log (request) VALUES (?)", (payload,))
            row_id = int(cur.lastrowid or 0)
            conn.commit()
        finally:
            conn.close()
        color = _classify_change_request(payload).upper()
        return _format_logged_change_reply(row_id, color, payload) + _refresh_change_log_export()

    return _format_change_log_board(_fetch_change_log_rows())


def _rewrite_change_log_delete_alias(text: str) -> str | None:
    match = re.match(
        r"^\s*(?:delete|remove|dismiss|resolve|close|clear)\s+logs?\s+(?P<ids>(?:#?\d+[\s,;]*)+)\s*$",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    ids = " ".join(re.findall(r"#?\d+", match.group("ids")))
    return f"log remove {ids}" if ids else None


def _rewrite_change_log_update_alias(text: str) -> str | None:
    match = re.match(
        r"^\s*(?:update|edit|revise)\s+logs?\s+#?(?P<id>\d+)\s*"
        r"(?:(?:to|with)\s+|[:\-]\s*)?(?P<summary>.+?)\s*$",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    summary = match.group("summary").strip(" “”\"")
    if not summary:
        return None
    return f"log update #{match.group('id')} {summary}"


def _chat_audit_summary(chat_id: str) -> dict:
    fallback = chat_id[:12] + "..."
    summary = {
        "label": fallback,
        "status": "UNKNOWN",
        "stale": False,
        "detail": "chat.db lookup failed",
    }
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        chat_row = conn.execute(
            """
            SELECT guid, account_id, account_login, last_addressed_handle, room_name, display_name
            FROM chat
            WHERE chat_identifier = ?
            """,
            (chat_id,),
        ).fetchone()
        if not chat_row:
            return {
                "label": fallback,
                "status": "MISSING",
                "stale": True,
                "detail": "enabled in gc_state.json but not found in chat.db",
            }
        label = (
            (chat_row["display_name"] or "").strip()
            or (chat_row["room_name"] or "").strip()
        )
        if not label:
            members = conn.execute(
                """
                SELECT h.id FROM handle h
                JOIN chat_handle_join chj ON chj.handle_id = h.ROWID
                JOIN chat c ON c.ROWID = chj.chat_id
                WHERE c.chat_identifier = ?
                """,
                (chat_id,),
            ).fetchall()
            if members:
                label = ", ".join(r["id"] for r in members)
        label = label or fallback

        configured_id = (MAC_MINI_APPLE_ID or "").lower()
        identity_fields = (
            (chat_row["account_id"] or "").lower(),
            (chat_row["account_login"] or "").lower(),
            (chat_row["last_addressed_handle"] or "").lower(),
            (chat_row["guid"] or "").lower(),
        )
        if not configured_id:
            status = "UNCHECKED"
            stale = False
            detail = "MAC_MINI_APPLE_ID is not set"
        elif any(configured_id in field for field in identity_fields):
            status = "OK"
            stale = False
            detail = "bound to configured Mac Mini Apple ID"
        else:
            status = "STALE"
            stale = True
            detail = "recreate this group from the Mac Mini Apple ID, then turn the old one off"
        return {"label": label, "status": status, "stale": stale, "detail": detail}
    except Exception:
        return summary
    finally:
        if conn is not None:
            conn.close()


def _stale_chat_audit_rows(enabled: list[str] | None = None) -> list[dict]:
    if enabled is None:
        from .group_chat import get_state_snapshot
        enabled = get_state_snapshot().get("enabled_chats", [])

    stale_rows = []
    for chat_id in enabled:
        audit = _chat_audit_summary(chat_id)
        if audit.get("stale"):
            stale_rows.append({"chat_id": chat_id, **audit})
    return stale_rows


def _format_stale_chat_rows(rows: list[dict]) -> str:
    if not rows:
        return "No stale group-chat routing warnings right now."

    lines = ["Stale group-chat routing warnings:"]
    for row in rows:
        chat_id = row.get("chat_id", "")
        label = row.get("label") or f"{chat_id[:12]}..."
        status = row.get("status") or "STALE"
        detail = row.get("detail") or "routing needs review"
        lines.append(f"- {status} {label} | id {chat_id} | {detail}")
    return "\n".join(lines)


def _cmd_chats(sender: str = "", text: str = "chats") -> str:
    denial = check_action_permission(sender, "view_chats") if sender else None
    if denial:
        return denial

    clean = re.sub(r"^\s*chats?\b", "", (text or "chats").strip(), flags=re.IGNORECASE).strip().lower()
    if clean in ("stale", "stale chats", "stale gcs", "warnings", "routing warnings", "audit"):
        return _format_stale_chat_rows(_stale_chat_audit_rows())

    if re.fullmatch(
        r"(?:(?:disable|cleanup|clean up|turn off)\s+stale(?:\s+(?:chats?|gcs?|warnings?))?(?:\s+confirm)?"
        r"|stale\s+(?:disable|cleanup|clean up|off)(?:\s+confirm)?)",
        clean,
    ):
        rows = _stale_chat_audit_rows()
        if not rows:
            return "No stale group-chat routing warnings right now."
        if not clean.endswith("confirm"):
            return (
                _format_stale_chat_rows(rows)
                + "\nReply `chats disable stale confirm` to remove only these stale IDs from enabled group chats. "
                "This does not delete chat history or personas."
            )
        for row in rows:
            disable_gc(row["chat_id"])
        ids = "\n".join(f"- {row['chat_id']} ({row.get('label', 'unknown')})" for row in rows)
        return (
            f"Disabled {len(rows)} stale group-chat routing warning(s):\n"
            f"{ids}\n"
            "Recreate the current group from the Mac Mini Apple ID, then use `@Davos on` in the new thread."
        )

    from .group_chat import get_state_snapshot
    snapshot = get_state_snapshot()
    enabled = snapshot.get("enabled_chats", [])
    personas = snapshot.get("personas", {})
    if not enabled:
        return "No group chats currently enabled."

    lines = []
    stale_count = 0
    for chat_id in enabled:
        audit = _chat_audit_summary(chat_id)
        stale_count += 1 if audit["stale"] else 0
        persona = personas.get(chat_id) or "default"
        lines.append(
            f"• {audit['status']} {audit['label']}  |  {persona}  |  id {chat_id[:8]}..."
        )

    header = f"{len(enabled)} chat(s) on"
    if stale_count:
        header += f" — {stale_count} stale routing warning(s)"
    return header + ":\n" + "\n".join(lines)


def _cmd_ping(text: str, sender: str) -> str:
    """Send a test message to a group chat by hex ID to verify routing.

    Usage:
      ping [chat_id]          — send "pong" to that group chat
      ping                    — list enabled chats with their IDs
    """
    denial = check_action_permission(sender, "view_chats")
    if denial:
        return denial

    parts = text.strip().split(None, 1)
    target_id = parts[1].strip() if len(parts) > 1 else ""

    if not target_id:
        # Show IDs so the owner can copy one for testing
        from .group_chat import get_state_snapshot
        enabled = get_state_snapshot().get("enabled_chats", [])
        if not enabled:
            return "No group chats enabled. Use 'chats' to see full details."
        lines = []
        for cid in enabled:
            audit = _chat_audit_summary(cid)
            lines.append(f"• {cid}  |  {audit['status']}  |  {audit['label']}")
        return "Enabled chat IDs (copy one and run: ping <id>):\n" + "\n".join(lines)

    is_group = len(target_id) == 32 and all(c in "0123456789abcdef" for c in target_id.lower())
    if not is_group:
        return f"'{target_id}' doesn't look like a 32-char hex group chat ID. Run 'ping' (no args) to see them."

    from .imessage import send_message as _send
    ok = _send(target_id, "Pong — routing confirmed", is_group=True)
    if ok:
        return f"Sent to {target_id} — check the group chat."
    return f"Send failed for {target_id} — check logs (pm2 logs davosbot)."


def _cmd_drift(sender: str) -> str:
    """Owner health/drift report — cron firings, recent errors, change-log backlog,
    pending reminders + scheduled tasks. Local-only; reads DB and PM2 logs directly."""
    denial = check_action_permission(sender, "view_logs")
    if denial:
        return denial

    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path as _P
    import re as _re
    import subprocess as _sp
    from collections import Counter as _C

    sections = []
    cutoff = _dt.now(_tz.utc).replace(tzinfo=None) - _td(days=14)

    # -- Cron jobs --------------------------------------------------------
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            crons = conn.execute(
                "SELECT id, cron_expression, action_type, enabled, last_run, action_payload "
                "FROM cron_jobs ORDER BY enabled DESC, cron_expression ASC"
            ).fetchall()
        if not crons:
            sections.append("CRON JOBS\n  none scheduled")
        else:
            lines = ["CRON JOBS"]
            for cid, expr, action, enabled, last_run, payload in crons:
                state = "on" if enabled else "off"
                last = last_run[:16] if last_run else "never"
                try:
                    p = _json.loads(payload or "{}")
                    rec = p.get("recipient", "")
                except Exception:
                    rec = ""
                rec_short = rec[:8] + "…" if len(rec) > 12 else rec
                lines.append(f"  [{state}] {expr} {action} ? {rec_short or '?'} (last: {last} UTC)")
            sections.append("\n".join(lines))
    except Exception as e:
        sections.append(f"CRON JOBS\n  query failed: {e}")

    # -- Recent errors (PM2 logs) -----------------------------------------
    try:
        log_path = _P.home() / ".pm2" / "logs" / "davosbot-error.log"
        if log_path.exists():
            res = _sp.run(["tail", "-n", "2000", str(log_path)], capture_output=True, text=True, timeout=10)
            err_re = _re.compile(r"(?:ERROR|Error|error)[^\n]*")
            matches = err_re.findall(res.stdout or "")
            # Strip noise: KeyboardInterrupt traceback frames, restart spam
            filtered = [m[:120] for m in matches if "KeyboardInterrupt" not in m and "ADMIN_PASSWORD" not in m]
            if not filtered:
                sections.append("RECENT ERRORS\n  none")
            else:
                top = _C(filtered).most_common(5)
                lines = ["RECENT ERRORS (top 5 from last ~2000 lines)"]
                for msg, n in top:
                    lines.append(f"  {n}× {msg}")
                sections.append("\n".join(lines))
        else:
            sections.append("RECENT ERRORS\n  log file not found")
    except Exception as e:
        sections.append(f"RECENT ERRORS\n  scan failed: {e}")

    # -- Live latency / quality traces ------------------------------------
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT event_type, payload FROM bot_log "
                "WHERE event_type IN ('message_trace', 'quality_signal') "
                "AND timestamp >= datetime('now', '-24 hours') "
                "ORDER BY id DESC LIMIT 500"
            ).fetchall()
        traces = []
        signals = []
        for event_type, payload in rows:
            try:
                data = _json.loads(payload or "{}")
            except Exception:
                continue
            if event_type == "message_trace":
                traces.append(data)
            elif event_type == "quality_signal":
                signals.append(data)
        if not traces and not signals:
            sections.append("LATENCY / QUALITY (24h)\n  no trace rows yet")
        else:
            lines = ["LATENCY / QUALITY (24h)"]
            if traces:
                elapsed = sorted(
                    float(row.get("elapsed_seconds", 0))
                    for row in traces
                    if isinstance(row.get("elapsed_seconds"), (int, float))
                )
                if elapsed:
                    mid = elapsed[len(elapsed) // 2]
                    p90 = elapsed[min(len(elapsed) - 1, int((len(elapsed) - 1) * 0.9))]
                    lines.append(f"  traces: {len(traces)} | median {mid:.2f}s | p90 {p90:.2f}s")
                routes = _C(str(row.get("route") or "unknown") for row in traces)
                for route, n in routes.most_common(4):
                    lines.append(f"  route {route}: {n}")
                slow = [row for row in traces if "slow_message" in set(row.get("flags") or [])]
                if slow:
                    lines.append(f"  slow messages: {len(slow)}")
            if signals:
                counts = _C(str(row.get("signal") or "unknown") for row in signals)
                lines.append("  quality signals: " + ", ".join(f"{sig}={n}" for sig, n in counts.most_common(5)))
            sections.append("\n".join(lines))
    except Exception as e:
        sections.append(f"LATENCY / QUALITY (24h)\n  query failed: {e}")

    # -- Change log backlog -----------------------------------------------
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            n_total = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
            recent = conn.execute(
                "SELECT id, request, created_ts FROM change_log "
                "ORDER BY id DESC LIMIT 3"
            ).fetchall()
        if n_total == 0:
            sections.append("CHANGE LOG\n  empty")
        else:
            lines = [f"CHANGE LOG ({n_total} entries)"]
            for cid, req, ts in recent:
                color = _classify_change_request(req).upper()
                lines.append(f"  #{cid} [{color}] ({ts[:10]}): {req[:80]}")
            sections.append("\n".join(lines))
    except Exception as e:
        sections.append(f"CHANGE LOG\n  query failed: {e}")

    # -- Pending reminders + scheduled tasks ------------------------------
    try:
        res = _sp.run(
            ["git", "log", "--oneline", "-5"],
            cwd=_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        commits = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
        sections.append("RECENT CODE CHANGES\n  " + ("\n  ".join(commits) if commits else "none"))
    except Exception as e:
        sections.append(f"RECENT CODE CHANGES\n  query failed: {e}")

    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            n_rem = conn.execute("SELECT COUNT(*) FROM reminders WHERE sent = 0").fetchone()[0]
            n_sch = conn.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE status = 'pending'").fetchone()[0]
            n_failed = conn.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE status = 'failed'").fetchone()[0]
        sections.append(
            f"QUEUES\n  reminders pending: {n_rem}\n  scheduled tasks pending: {n_sch}\n  scheduled tasks failed: {n_failed}"
        )
    except Exception as e:
        sections.append(f"QUEUES\n  query failed: {e}")

    return "\n\n".join(sections)



def _cmd_memory(text: str, sender: str = "") -> str:
    denial = check_action_permission(sender, "manage_memory") if sender else None
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    arg_raw = parts[1].strip() if len(parts) > 1 else ""
    subcmd = arg_raw.lower()

    if subcmd == "wipe":
        with PERSONALITY_FILE_LOCK:
            Path(MEMORY_PATH).write_text(_memory_baseline(), encoding="utf-8")
        return "Long-term memory wiped. Reset to baseline facts."

    if subcmd.startswith("add "):
        fact = arg_raw[4:].strip()
        if not fact:
            return "Usage: memory add [fact]"
        with PERSONALITY_FILE_LOCK, open(MEMORY_PATH, "a", encoding="utf-8") as f:
            f.write(f"- {fact}\n")
        return f"Added to memory: {fact}"

    if subcmd.startswith("note ") or subcmd.startswith("remember "):
        prefix_len = 5 if subcmd.startswith("note ") else 9
        note = arg_raw[prefix_len:].strip()
        if not note:
            return "Usage: memory note [private fact]"
        item_id = add_owner_memory_item(note, source="owner_manual")
        return (
            f"Saved private memory note #{item_id}. "
            "It is searchable with `memory search ...` and is not injected into group chats."
        )

    if subcmd in ("notes", "list notes", "private", "private notes"):
        rows = list_owner_memory_items(limit=5)
        return _format_owner_memory_items(rows, "Private memory notes")

    if subcmd.startswith("search "):
        query = arg_raw[7:].strip()
        if not query:
            return "Usage: memory search [query]"
        rows = search_owner_memory_items(query, limit=5)
        return _format_owner_memory_items(rows, f"Private memory matches for {query!r}")

    if subcmd.startswith("clear"):
        arg = subcmd[5:].strip()
        if not arg:
            clear_history(sender)
            return "Conversation history cleared."
        if arg.endswith("m") and arg[:-1].isdigit():
            mins = int(arg[:-1])
            n = clear_history_minutes(sender, mins)
            return f"Cleared {n} messages from the last {mins} min."
        if arg.isdigit():
            n = clear_history_count(sender, int(arg))
            return f"Cleared last {n} messages."
        return "Usage: memory clear / memory clear 30m / memory clear 10"

    # Default: show current memory
    try:
        with PERSONALITY_FILE_LOCK:
            content = Path(MEMORY_PATH).read_text(encoding="utf-8").strip()
        return content or "(memory is empty)"
    except FileNotFoundError:
        return "(no memory file found)"


def _format_owner_memory_items(rows: list[dict], title: str) -> str:
    if not rows:
        return f"{title}: none."
    lines = [title + ":"]
    for row in rows:
        ts = (row.get("timestamp") or "")[:10]
        lines.append(f"#{row.get('id')} ({ts}): {row.get('text')}")
    return "\n".join(lines)


_MODEL_ROUTE_ALIASES = {
    "chat": "chat",
    "brain": "chat",
    "primary": "chat",
    "fallback": "tool",
    "tool": "tool",
    "tools": "tool",
    "agentic": "tool",
    "rewrite": "rewrite",
    "helper": "rewrite",
    "helpers": "rewrite",
    "image": "image",
    "images": "image",
    "gen": "image",
    "generation": "image",
    "nano": "nano_banana",
    "banana": "nano_banana",
    "nano-banana": "nano_banana",
    "nanobanana": "nano_banana",
    "vision": "vision",
    "scan": "vision",
    "analysis": "vision",
}

_MODEL_ROUTE_DESCRIPTIONS = {
    "chat": "normal chat fallback after local Ollama",
    "tool": "tool-use and planning calls",
    "rewrite": "small helper rewrites",
    "image": "image generation",
    "nano_banana": "explicit Nano Banana image generation",
    "vision": "image reads and screenshot analysis",
}


def _model_route_snapshot() -> dict[str, str]:
    try:
        active_generation_provider = choose_generation_provider()
    except Exception:
        active_generation_provider = "unknown"
    try:
        active_scan_provider = choose_scan_provider()
    except Exception:
        active_scan_provider = "unknown"
    return {
        "active_generation_provider": active_generation_provider,
        "active_scan_provider": active_scan_provider,
    }


def _model_provider_detail(provider: str, kind: str) -> str:
    if provider == "local":
        return f"local {LOCAL_IMAGE_MODEL} worker"
    if provider == "gemini":
        return GEMINI_IMAGE_MODEL if kind == "image" else f"Gemini {ADVANCED_VISION_MODEL}"
    if provider == "openai":
        model = OPENAI_IMAGE_MODEL if kind == "image" else OPENAI_VISION_MODEL
        return f"legacy explicit OpenAI {model or '<unset>'}"
    if provider == "disabled":
        return "disabled"
    return provider or "unknown"


def _optional_env_note(name: str, effective: str) -> str:
    raw = os.getenv(name, "").strip()
    if raw:
        return f"{name}={effective}"
    return f"{name}=<unset>; using {effective}"


def _normalize_model_route(value: str) -> str:
    return _MODEL_ROUTE_ALIASES.get((value or "").strip().lower(), "")


def _parse_model_request(body: str) -> tuple[str, str, str]:
    clean = re.sub(r"\s+", " ", redact_secret(body or "")).strip()
    route = ""
    model = ""
    if not clean:
        return "", "", ""

    words = [w.strip(" .,:;()[]{}") for w in clean.split()]
    for word in words:
        route = _normalize_model_route(word)
        if route:
            break

    model_patterns = (
        r"\b(?:gemini|gpt|openai|oai|claude|llama|gemma|flux|sdxl)"
        r"(?:[\w.\-/:]*)(?:\s+(?:\d+(?:\.\d+)?|pro|flash(?:-lite|-image)?|image|lite|mini|nano|banana|schnell|dev)){0,4}\b",
        r"\b(?:pro|flash|nano|mini|schnell|dev)\b",
    )
    for pattern in model_patterns:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            model = m.group(0).strip(" .,:;")
            break
    return route or "unspecified", model or "unspecified", clean


def _model_request_risk(clean: str) -> str:
    if re.search(
        r"\b(?:permission|admin|password|private\s+send|deploy|self[-\s]?edit|shell|database|db\s+schema|memory|soul)\b",
        clean or "",
        re.IGNORECASE,
    ):
        return "RED"
    return "YELLOW"


def _cmd_model_options(sender: str) -> str:
    denial = check_action_permission(sender, "view_billing")
    if denial:
        return denial
    snapshot = _model_route_snapshot()
    gen_provider = snapshot["active_generation_provider"]
    scan_provider = snapshot["active_scan_provider"]
    lines = [
        "Model commands:",
        "  model status",
        "  model options",
        "  model intensity",
        "  model request [route] [model or goal]",
        "",
        "Current live routing:",
        f"  chat: Ollama {OLLAMA_SIMPLE_CHAT_MODEL} simple-chat primary on the Mini; fallback Gemini {GEMINI_MODEL}. Label: {MODEL_ROUTE_SIMPLE_CHAT} -> {MODEL_ROUTE_TOOL_USE}; num_ctx={OLLAMA_NUM_CTX}; keep-warm model={OLLAMA_MODEL}",
        f"  tool: Gemini {GEMINI_MODEL} for function calling. Label: {MODEL_ROUTE_TOOL_USE}",
        f"  rewrite: Gemini {GEMINI_REWRITE_MODEL} for tiny helper rewrites. Label: {MODEL_ROUTE_HELPER_REWRITE}",
        f"  image: active {gen_provider} ({_model_provider_detail(gen_provider, 'image')}); auto falls back only to Gemini. Label: {MODEL_ROUTE_IMAGE_GENERATION}",
        f"  nano banana: explicit Gemini image route {MODEL_ROUTE_NANO_BANANA_IMAGE}; output {NANO_BANANA_IMAGE_SIZE} {NANO_BANANA_IMAGE_ASPECT_RATIO}; separate queue.",
        f"  vision: active {scan_provider} ({_model_provider_detail(scan_provider, 'vision')}). Label: {MODEL_ROUTE_IMAGE_SCAN}",
        f"  planning: owner-only direct route {MODEL_ROUTE_COMPLEX_REASONING} when it resolves to Gemini; otherwise normal chat fallback.",
        f"  code/review: {MODEL_ROUTE_CODE_REVIEW}; callable Gemini labels can answer directly, Codex labels stay review-only handoffs.",
        "",
        "Power ranking:",
        f"  1. Local Ollama {OLLAMA_SIMPLE_CHAT_MODEL} chat + {LOCAL_IMAGE_MODEL} images: cheapest/private, depends on Mini health.",
        f"  2. Gemini {GEMINI_MODEL}: cloud backup, tool-use, and helper/rewrite work.",
        f"  3. Gemini {ADVANCED_TEXT_MODEL}: rare owner-only pro thinking/code-review route.",
        f"  4. Gemini {GEMINI_IMAGE_MODEL}: image scan and cloud image fallback.",
        f"  5. Nano Banana {NANO_BANANA_IMAGE_MODEL}: explicit 2K image lane only when requested.",
        "  6. OpenAI/GPT: not used by auto routes; legacy explicit provider only.",
        "",
        "Config note:",
        f"  {_optional_env_note('GEMINI_MODEL', GEMINI_MODEL)}",
        f"  {_optional_env_note('GEMINI_REWRITE_MODEL', GEMINI_REWRITE_MODEL)}",
        f"  {_optional_env_note('ADVANCED_TEXT_MODEL', ADVANCED_TEXT_MODEL)}",
        f"  {_optional_env_note('ADVANCED_VISION_MODEL', ADVANCED_VISION_MODEL)}",
        f"  {_optional_env_note('ADVANCED_CODE_MODEL', ADVANCED_CODE_MODEL)}",
        f"  {_optional_env_note('NANO_BANANA_IMAGE_MODEL', NANO_BANANA_IMAGE_MODEL)}",
        "  Empty optional route envs fall back to the base models above.",
    ]
    lines.extend([
        "",
        "Examples:",
        "  model request rewrite gemini-3.1-flash-lite",
        "  model request image keep local Flux first, Gemini fallback",
        "  model request nano banana test Gemini 3.1 Flash Image at 2K",
        "",
        "Requests are review-only. They log a Codex handoff and do not edit .env, restart, deploy, or widen permissions.",
    ])
    return "\n".join(lines)


def _cmd_model_intensity(sender: str) -> str:
    denial = check_action_permission(sender, "view_billing")
    if denial:
        return denial
    snapshot = _model_route_snapshot()
    active_generation_provider = snapshot["active_generation_provider"]
    active_scan_provider = snapshot["active_scan_provider"]
    return "\n".join([
        "Model intensity ladder:",
        f"  1. Casual chat: Ollama {OLLAMA_SIMPLE_CHAT_MODEL} on the Mini with compacted persona/history context.",
        f"  2. Tool mode: Gemini {GEMINI_MODEL} for owner/admin tool-capable turns and fallback planning.",
        f"  3. Helper rewrite: Gemini {GEMINI_REWRITE_MODEL} for tiny rewrite/classification helpers.",
        f"  4. Complex reasoning/planning: {MODEL_ROUTE_COMPLEX_REASONING} for owner-only direct complex turns when callable.",
        f"  5. Image gen: {MODEL_ROUTE_IMAGE_GENERATION}, currently resolves to {active_generation_provider}.",
        f"  6. Nano Banana: {MODEL_ROUTE_NANO_BANANA_IMAGE}, explicit only, {NANO_BANANA_IMAGE_SIZE} {NANO_BANANA_IMAGE_ASPECT_RATIO}.",
        f"  7. Image scan: {MODEL_ROUTE_IMAGE_SCAN}, currently resolves to {active_scan_provider}.",
        f"  8. Code review / cleanup: {MODEL_ROUTE_CODE_REVIEW}; Gemini labels are callable, Codex labels are review-only.",
        "",
        "Policy:",
        "  Side effects stay guarded by deterministic permission/tool checks. Stronger models do not bypass permissions.",
        "  Cheap/local routes stay default. Premium direct routes are owner-only and logged.",
        "  Secrets/API keys are never shown here. OpenAI/GPT is not part of the default ladder.",
        "",
        "Phone workflow:",
        "  model request chat stronger owner-only planning",
        "  model request rewrite gemini-3.1-flash-lite",
        "  model request image use local Flux first, Gemini fallback, no OpenAI auto fallback",
    ])


def _cmd_model_request(text: str, sender: str) -> str:
    if not is_owner(sender):
        return "Model change requests are owner-only. Admins can still use model status/options/intensity."

    clean = re.sub(
        r"^\s*/?models?\s+(?:request|plan|use|set|change|fix|ship)\b:?",
        "",
        text or "",
        flags=re.IGNORECASE,
    ).strip()
    route, model, safe_request = _parse_model_request(clean)
    if not safe_request:
        return "Use: model request [route] [model or goal]"

    risk = _model_request_risk(safe_request)
    summary = safe_request[:180].rstrip()
    request = f"[MODEL-CHANGE {risk}] {summary}"
    reason = "\n".join([
        "type=model_change_request",
        f"risk={risk}",
        "status=review_only",
        f"requested_route={route}",
        f"requested_model={model}",
        "blocked_actions=no .env edit, no live model switch, no deploy, no permission change",
        f"current_chat_primary=Ollama {OLLAMA_SIMPLE_CHAT_MODEL}",
        f"current_ollama_keep_warm_model={OLLAMA_MODEL}",
        f"current_gemini_tool_use={GEMINI_MODEL}",
        f"current_gemini_rewrite={GEMINI_REWRITE_MODEL}",
        f"current_advanced_text_model={ADVANCED_TEXT_MODEL}",
        f"current_advanced_code_model={ADVANCED_CODE_MODEL}",
        f"current_advanced_vision_model={ADVANCED_VISION_MODEL}",
        f"current_route_simple_chat={MODEL_ROUTE_SIMPLE_CHAT}",
        f"current_route_tool_use={MODEL_ROUTE_TOOL_USE}",
        f"current_route_helper_rewrite={MODEL_ROUTE_HELPER_REWRITE}",
        f"current_route_complex_reasoning={MODEL_ROUTE_COMPLEX_REASONING}",
        f"current_route_nano_banana_image={MODEL_ROUTE_NANO_BANANA_IMAGE}",
        f"current_route_code_review={MODEL_ROUTE_CODE_REVIEW}",
        f"current_image_provider={IMAGE_PROVIDER}",
        f"current_image_scan_provider={IMAGE_SCAN_PROVIDER}",
        f"current_gemini_image={GEMINI_IMAGE_MODEL}",
        f"current_nano_banana_image={NANO_BANANA_IMAGE_MODEL}",
        f"current_nano_banana_output={NANO_BANANA_IMAGE_SIZE} {NANO_BANANA_IMAGE_ASPECT_RATIO}",
        f"current_openai_image={OPENAI_IMAGE_MODEL}",
        f"current_openai_vision={OPENAI_VISION_MODEL}",
        "codex_prompt=Review this requested model/provider change. Propose or implement the smallest safe config/code patch with tests, cost notes, permission invariants, and Mini validation. Do not treat phone chat as permission to self-edit live runtime config.",
        "mini_validation=After pull, run model status, billing, focused tests, compileall, PM2 status/logs, and a phone smoke for the affected route.",
        f"owner_request={safe_request}",
    ])

    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO change_log (request, reason) VALUES (?, ?)",
            (request, reason),
        )
        row_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    return (
        f"Model request logged #{row_id} [{risk}].\n"
        f"Route: {route}; model/goal: {model}.\n"
        "Review-only: I did not edit .env, restart, deploy, or widen permissions.\n"
        "Next: text 'ship safe cleanup' for the Codex handoff."
        f"{_refresh_change_log_export()}"
    )


def _cmd_model(text: str, sender: str) -> str:
    clean = re.sub(r"^\s*/?models?\b", "", text or "", flags=re.IGNORECASE).strip()
    lower = clean.lower()
    if lower in ("", "status", "routing", "routes"):
        return _cmd_model_status(sender)
    if lower in ("help", "options", "option", "models", "list"):
        return _cmd_model_options(sender)
    if lower in ("intensity", "ladder", "tiers", "modes", "mode"):
        return _cmd_model_intensity(sender)
    if re.match(r"^(?:request|plan|use|set|change|fix|ship)\b", lower):
        return _cmd_model_request(text, sender)
    return _cmd_model_options(sender)


def _cmd_billing(sender: str) -> str:
    from contextlib import closing
    denial = check_action_permission(sender, "view_billing")
    if denial:
        return denial
    # Shared Gemini estimate per 1M tokens.
    # This remains an estimate until gemini_usage stores model names per row.
    INPUT_RATE = globals().get("GEMINI_INPUT_RATE_USD", 0.30 / 1_000_000)
    OUTPUT_RATE = globals().get("GEMINI_OUTPUT_RATE_USD", 2.50 / 1_000_000)
    enabled = bool(globals().get("GEMINI_ENABLED", True))
    alert_usd = float(globals().get("GEMINI_DAILY_ALERT_USD", 0.25) or 0)
    budget_usd = float(globals().get("GEMINI_DAILY_BUDGET_USD", 1.00) or 0)

    def est_cost(prompt, candidates):
        return prompt * INPUT_RATE + candidates * OUTPUT_RATE

    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            today_row = conn.execute("""
                SELECT SUM(prompt_tokens), SUM(candidates_tokens), SUM(total_tokens), COUNT(*)
                FROM gemini_usage
                WHERE date(timestamp) = date('now')
            """).fetchone()

            month_row = conn.execute("""
                SELECT SUM(prompt_tokens), SUM(candidates_tokens), SUM(total_tokens), COUNT(*)
                FROM gemini_usage
                WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')
            """).fetchone()

            alltime_row = conn.execute("""
                SELECT SUM(prompt_tokens), SUM(candidates_tokens), SUM(total_tokens), COUNT(*)
                FROM gemini_usage
            """).fetchone()

            daily_rows = conn.execute("""
                SELECT
                    date(timestamp) as day,
                    SUM(prompt_tokens),
                    SUM(candidates_tokens),
                    COUNT(*)
                FROM gemini_usage
                WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')
                GROUP BY day
                ORDER BY day DESC
                LIMIT 7
            """).fetchall()

        if not alltime_row or alltime_row[2] is None:
            lines = [
                "No Gemini usage recorded yet.",
                "",
                "Gemini controls:",
                f"  Enabled: {'yes' if enabled else 'NO - blocked by GEMINI_ENABLED=false'}",
                f"  Daily alert: ${alert_usd:.2f}",
                f"  Daily hard budget: ${budget_usd:.2f}",
                "  Emergency shutoff: set GEMINI_ENABLED=false in .env and restart/pull.",
            ]
            return "\n".join(lines)

        t_prompt, t_cand, t_total, t_calls = today_row if today_row and today_row[2] else (0, 0, 0, 0)
        m_prompt, m_cand, m_total, m_calls = month_row if month_row[2] else (0, 0, 0, 0)
        a_prompt, a_cand, a_total, a_calls = alltime_row
        today_cost = est_cost(t_prompt, t_cand)

        lines = [
            "Gemini usage this month:",
            "Pricing basis: configured Gemini estimate.",
            "If model envs change tiers, mixed-model rows need manual review.",
            f"Today: ${today_cost:.4f} ({t_calls} calls)",
            f"  Calls: {m_calls}",
            f"  Input: {m_prompt:,} tokens",
            f"  Output: {m_cand:,} tokens",
            f"  Est. cost: ${est_cost(m_prompt, m_cand):.4f}",
            "",
            f"All-time est. cost: ${est_cost(a_prompt, a_cand):.4f} ({a_calls} calls)",
            "",
            "Gemini controls:",
            f"  Enabled: {'yes' if enabled else 'NO - blocked by GEMINI_ENABLED=false'}",
            f"  Daily alert: ${alert_usd:.2f}",
            f"  Daily hard budget: ${budget_usd:.2f}",
            "  Emergency shutoff: set GEMINI_ENABLED=false in .env and restart/pull.",
        ]
        if enabled and budget_usd > 0 and today_cost >= budget_usd:
            lines.append("  Status: blocked today by spend guard.")
        elif enabled and alert_usd > 0 and today_cost >= alert_usd:
            lines.append("  Status: above alert threshold; webhook alert should be throttled, not spammy.")

        if daily_rows:
            lines.append("\nDaily (last 7 days):")
            for day, d_prompt, d_cand, d_calls in daily_rows:
                lines.append(f"  {day}: ${est_cost(d_prompt, d_cand):.4f} ({d_calls} calls)")

        return "\n".join(lines)
    except Exception as e:
        return f"billing error: {e}"


def _cmd_model_status(sender: str) -> str:
    denial = check_action_permission(sender, "view_billing")
    if denial:
        return denial
    snapshot = _model_route_snapshot()
    active_generation_provider = snapshot["active_generation_provider"]
    active_scan_provider = snapshot["active_scan_provider"]
    return "\n".join([
        "Model routing:",
        f"  Chat primary: Ollama {OLLAMA_SIMPLE_CHAT_MODEL}",
        f"  Ollama keep-warm/default model: {OLLAMA_MODEL}",
        f"  Ollama context window: num_ctx={OLLAMA_NUM_CTX}; prompt/history compacted before local chat",
        f"  Gemini fallback/tool-use: {GEMINI_MODEL}",
        f"  Gemini rewrite helpers: {GEMINI_REWRITE_MODEL}",
        f"  Advanced text model: {ADVANCED_TEXT_MODEL}",
        f"  Advanced vision model: {ADVANCED_VISION_MODEL}",
        f"  Code/Codex cleanup model: {ADVANCED_CODE_MODEL}",
        f"  Route simple chat: {MODEL_ROUTE_SIMPLE_CHAT}",
        f"  Route tool-use: {MODEL_ROUTE_TOOL_USE}",
        f"  Route helper rewrite: {MODEL_ROUTE_HELPER_REWRITE}",
        f"  Route complex reasoning/planning: {MODEL_ROUTE_COMPLEX_REASONING}",
        f"  Route image generation: {MODEL_ROUTE_IMAGE_GENERATION}",
        f"  Route Nano Banana image: {MODEL_ROUTE_NANO_BANANA_IMAGE}",
        f"  Route image scan: {MODEL_ROUTE_IMAGE_SCAN}",
        f"  Route code review/cleanup: {MODEL_ROUTE_CODE_REVIEW}",
        f"  Gemini enabled: {'yes' if GEMINI_ENABLED else 'NO - blocked by GEMINI_ENABLED=false'}",
        f"  Gemini key configured: {'yes' if GEMINI_API_KEY else 'no'}",
        f"  Gemini daily alert/budget: ${GEMINI_DAILY_ALERT_USD:.2f} / ${GEMINI_DAILY_BUDGET_USD:.2f}",
        f"  Image generation provider: {IMAGE_PROVIDER} (active: {active_generation_provider})",
        f"  Image generation active detail: {_model_provider_detail(active_generation_provider, 'image')}",
        f"  Image scan provider: {IMAGE_SCAN_PROVIDER} (active: {active_scan_provider})",
        f"  Image scan active detail: {_model_provider_detail(active_scan_provider, 'vision')}",
        f"  Local image endpoint: {'configured' if LOCAL_IMAGE_ENDPOINT else 'not configured'}",
        f"  Local image model label: {LOCAL_IMAGE_MODEL}",
        f"  Gemini image: {GEMINI_IMAGE_MODEL}",
        f"  Nano Banana image: {NANO_BANANA_IMAGE_MODEL}; output {NANO_BANANA_IMAGE_SIZE} {NANO_BANANA_IMAGE_ASPECT_RATIO}; explicit only",
        f"  OpenAI key configured: {'yes' if OPENAI_API_KEY else 'no'} (legacy explicit only; auto routes do not use it)",
        f"  OpenAI image: {OPENAI_IMAGE_MODEL or '<unset>'}",
        f"  OpenAI vision: {OPENAI_VISION_MODEL or '<unset>'}",
        "",
        "Notes:",
        "  Tool-use stays deterministic through Gemini function calling.",
        "  Complex owner planning/review turns can use the configured Gemini advanced route; Codex/self-edit labels remain review-only.",
        "  `gpt scan image` is legacy wording; it uses the active scan provider, not necessarily OpenAI.",
        "  OpenAI/GPT is not used by default routes.",
        "  Use model request [route] [model or goal] to log a safe Codex handoff from phone.",
        "  Model requests do not edit .env, restart, deploy, or widen permissions.",
    ])


def _cmd_api_status(sender: str) -> str:
    denial = check_action_permission(sender, "view_session")
    if denial:
        return denial
    try:
        gen_provider = choose_generation_provider()
    except Exception:
        gen_provider = "unknown"
    try:
        scan_provider = choose_scan_provider()
    except Exception:
        scan_provider = "unknown"
    rows = [
        "API/tool status (no secrets):",
        f"- iMessage receive/send: configured via macOS Messages DB + AppleScript; Mini Apple ID {'set' if MAC_MINI_APPLE_ID else 'missing'}",
        f"- Chat LLM: Ollama {OLLAMA_MODEL}; Gemini fallback/tool-use {'configured' if GEMINI_API_KEY else 'missing'} ({GEMINI_MODEL})",
        f"- Web search: {'configured' if TAVILY_API_KEY else 'missing'} via Tavily",
        "- Weather: available through the Gemini tool route when tool-use is enabled",
        "- ESPN sports: available through public scoreboard APIs for sports recap/UFC flows; no secret key required",
        "- Markets: Yahoo Finance current/extended-hours charts; no secret key required",
        f"- Image scan: {'available' if scan_provider not in ('disabled', 'unknown') else 'not configured'} via {scan_provider}",
        f"- Image generation: {'available' if gen_provider not in ('disabled', 'unknown') else 'not configured'} via {gen_provider}",
        f"- Local image worker: {'configured' if LOCAL_IMAGE_ENDPOINT else 'not configured'} ({LOCAL_IMAGE_MODEL})",
        f"- Nano Banana image: explicit route {NANO_BANANA_IMAGE_MODEL} at {NANO_BANANA_IMAGE_SIZE} {NANO_BANANA_IMAGE_ASPECT_RATIO}",
        f"- OpenAI image/vision: {'configured' if OPENAI_API_KEY and (OPENAI_IMAGE_MODEL or OPENAI_VISION_MODEL) else 'missing'} but legacy explicit only ({OPENAI_IMAGE_MODEL or '<unset>'} / {OPENAI_VISION_MODEL or '<unset>'})",
        f"- Owner alert webhook: {'configured' if OWNER_ALERT_WEBHOOK_URL else 'not configured'}",
        "",
        "Useful commands: `capabilities`, `market status`, `image status`, `model status`, `billing`, `ufc card`, `list crons`, `ship safe cleanup`.",
    ]
    return "\n".join(rows)


def _cmd_owner_alert(text: str, sender: str) -> str:
    denial = check_action_permission(sender, "manage_owner_alerts")
    if denial:
        return denial

    clean = re.sub(r"^\s*alerts?\b", "", (text or "")).strip().lower()
    from .config import OWNER_ALERT_WEBHOOK_URL

    configured = bool(OWNER_ALERT_WEBHOOK_URL)
    if clean in ("", "status", "config", "configured"):
        state = "configured" if configured else "not configured"
        return (
            f"Owner alert webhook: {state}.\n"
            "Use `alert test` after setting OWNER_ALERT_WEBHOOK_URL on the Mini. "
            "The URL is never printed."
        )

    if clean in ("test", "smoke", "send test", "test webhook"):
        from .alerts import send_owner_alert
        ok = send_owner_alert(
            "manual_smoke",
            "Davos owner alert smoke test",
            {"source": "owner_alert_command"},
        )
        if ok:
            return "Owner alert webhook test sent."
        return "Owner alert webhook is not configured or did not accept the test."

    return "Try `alert status` or `alert test`."


def _cmd_soulversion(sender: str) -> str:
    denial = check_action_permission(sender, "view_audit_log")
    if denial:
        return denial
    import re
    from .soul import read_soul

    try:
        content = read_soul()
    except FileNotFoundError:
        return "SOUL.md not found."

    entries = re.findall(r"<!-- (v\d+ \| .+?) -->", content)
    if not entries:
        return "No version entries in SOUL.md yet."

    last5 = entries[-5:]
    lines = [f"• {e}" for e in last5]
    return f"SOUL.md version history (last {len(last5)}):\n" + "\n".join(lines)


def _cmd_restoresoul(text: str, sender: str) -> str:
    denial = check_action_permission(sender, "modify_soul")
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return "Usage: !restoresoul [backup_filename]  (e.g. !restoresoul SOUL_20260428_183000.md)"
    backup_name = parts[1].strip()
    try:
        return restore_soul_from_backup(backup_name, sender=sender)
    except PermissionError:
        return "Permission denied — owner only."
    except Exception as e:
        return f"Restore failed: {e}"


def _cmd_backups(sender: str) -> str:
    denial = check_action_permission(sender, "view_backups")
    if denial:
        return denial
    from datetime import datetime
    from .config import BOT_DB_PATH

    backups_dir = Path(BOT_DB_PATH).parent / "backups"
    if not backups_dir.exists():
        return "No backups directory found."

    backups = sorted(
        backups_dir.glob("davosbot_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:5]

    if not backups:
        return "No backups found."

    lines = []
    for path in backups:
        size_kb = path.stat().st_size / 1024
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"• {path.name}  {size_kb:.0f} KB  {mtime}")

    return f"Recent backups ({len(backups)}):\n" + "\n".join(lines)


def _cmd_personalities(sender: str) -> str:
    denial = check_action_permission(sender, "view_personalities")
    if denial:
        return denial
    personas_dir = Path(_PROJECT_DIR) / "personalities"
    if not personas_dir.exists():
        return "personalities/ directory not found."

    visible_names = list_personas()
    files = [(name, persona_file_for(name, include_hidden=False)) for name in visible_names]
    if not files:
        return "No personality files found."

    errors = validate_personality_files()
    visible_filenames = {path.name for _name, path in files}
    visible_errors = [err for err in errors if err.split(" — ")[0] in visible_filenames]
    failed = {err.split(" — ")[0] for err in visible_errors}

    lines = []
    for name, path in files:
        size = path.stat().st_size
        mark = "?" if path.name in failed else "?"
        lines.append(f"{mark} {name}  ({size:,} bytes)")

    if visible_errors:
        lines.append("")
        lines.append("Validation errors:")
        for err in visible_errors:
            lines.append(f"  • {err}")

    return f"Personality files ({len(files)}):\n" + "\n".join(lines)


def get_status(sender: str = "") -> str:
    denial = check_action_permission(sender, "view_session") if sender else None
    if denial:
        return denial
    from datetime import datetime, timezone
    _LA = ZoneInfo("America/Los_Angeles")  # DST-aware; never ask LLM for time

    info = get_session_info()
    if not info:
        return "No session data available — bot may have just started or session init failed."

    now_utc = datetime.now(timezone.utc)

    started_utc = datetime.strptime(info["started_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    started_la = started_utc.astimezone(_LA)
    started_str = started_la.strftime("%b %d %I:%M %p %Z")

    if info.get("last_heartbeat"):
        hb_utc = datetime.strptime(info["last_heartbeat"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        hb_secs = int((now_utc - hb_utc).total_seconds())
        if hb_secs < 60:
            hb_str = "just now"
        else:
            hb_str = f"{hb_secs // 60}m ago"
    else:
        hb_str = "none yet"

    last_error = info.get("last_error") or "none"
    if last_error != "none" and len(last_error) > 80:
        last_error = last_error[:80] + "…"

    msgs = info.get("messages_processed") or 0
    return (
        f"Up since {started_str}\n"
        f"Messages processed: {msgs}\n"
        f"Last heartbeat: {hb_str}\n"
        f"Last error: {last_error}"
    )


def get_uptime(sender: str = "") -> str:
    denial = check_action_permission(sender, "view_session") if sender else None
    if denial:
        return denial
    from datetime import datetime, timezone

    info = get_session_info()
    if not info:
        return "No session data — cannot calculate uptime."

    started_utc = datetime.strptime(info["started_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - started_utc
    total_secs = int(delta.total_seconds())
    hours, rem = divmod(total_secs, 3600)
    mins = rem // 60
    h_label = "hour" if hours == 1 else "hours"
    m_label = "minute" if mins == 1 else "minutes"
    return f"Uptime: {hours} {h_label}, {mins} {m_label}."


def _cmd_ratelimit(text: str, sender: str = "") -> str:
    denial = check_action_permission(sender, "manage_ratelimit") if sender else None
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return "Usage: !ratelimit [handle]"
    handle = normalize_handle(parts[1].strip())
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_log WHERE sender = ? AND timestamp >= datetime('now', '-1 hour')",
                (handle,),
            ).fetchone()
        count = row[0] if row else 0
        return f"{handle}: {count}/20 messages in the last hour."
    except Exception as e:
        return f"ratelimit error: {e}"


def _parse_access_handle(raw: str) -> str | None:
    """Accept one complete phone/email handle, never part of a sentence."""
    handle = raw.strip()
    if re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+", handle):
        return normalize_handle(handle)
    if re.fullmatch(r"\+?[0-9()\s.\-]+", handle):
        digits = re.sub(r"[^0-9]", "", handle)
        if (len(digits) == 10 and not handle.startswith("+")) or (len(digits) == 11 and digits.startswith("1")):
            return normalize_handle(handle)
    # Preserve existing canonical international identities without changing the
    # shared normalizer's comparison rules.
    if re.fullmatch(r"\+[2-9][0-9]{7,14}", handle) and normalize_handle(handle) == handle:
        return handle
    return None


def _cmd_grant(text: str, actor: str) -> str:
    denial = check_action_permission(actor, "grant_admin")
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return "Usage: grant [handle]"
    handle = _parse_access_handle(parts[1])
    if handle is None:
        return "Usage: grant [one complete phone number or email]"
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            existing = conn.execute(
                "SELECT 1 FROM admins WHERE handle = ? AND revoked_at IS NULL",
                (handle,),
            ).fetchone()
            if existing:
                return f"{handle} is already an active admin."
            conn.execute(
                "INSERT INTO admins (handle, granted_by) VALUES (?, ?)",
                (handle, actor),
            )
            conn.execute(
                "INSERT INTO admin_audit (action, handle, actor) VALUES ('grant', ?, ?)",
                (handle, actor),
            )
            conn.commit()
    except Exception as e:
        return f"grant failed: {e}"

    # Admin INSERT committed above. Now sync the GC allow list — failures here are
    # logged but don't roll back the admin grant (it's already real).
    gc_note = ""
    try:
        from .group_chat import approve_user, is_approved_user
        if not is_approved_user(handle):
            approve_user(handle)
            logger.info("Admin %s also added to approved_users (GC visibility)", handle)
            gc_note = " (also on GC allow list)"
        else:
            gc_note = " (already on GC allow list)"
    except Exception as e:
        logger.warning("approve_user sync failed for %s: %s", handle, e)
        gc_note = " — GC allow-list sync FAILED, run `@Davos allow` manually"
    logger.info("Admin granted to %s by %s", handle, actor)
    return f"Granted admin to {handle}{gc_note}."


def _cmd_revoke(text: str, actor: str) -> str:
    denial = check_action_permission(actor, "revoke_admin")
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return "Usage: !revoke [handle]"
    handle = _parse_access_handle(parts[1])
    if handle is None:
        return "Usage: revoke [one complete phone number or email]"
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            cur = conn.execute(
                "UPDATE admins SET revoked_at = datetime('now') WHERE handle = ? AND revoked_at IS NULL",
                (handle,),
            )
            if cur.rowcount == 0:
                return f"{handle} is not an active admin."
            conn.execute(
                "INSERT INTO admin_audit (action, handle, actor) VALUES ('revoke', ?, ?)",
                (handle, actor),
            )
            conn.commit()
        logger.info("Admin revoked from %s by %s", handle, actor)
        return f"Revoked admin from {handle}."
    except Exception as e:
        return f"revoke failed: {e}"


def _cmd_admins(sender: str) -> str:
    denial = check_action_permission(sender, "view_audit_log")
    if denial:
        return denial
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT handle, granted_by, granted_at FROM admins WHERE revoked_at IS NULL ORDER BY granted_at ASC",
            ).fetchall()
        if not rows:
            return "No active admins."
        lines = [f"• {r[0]}  (granted by {r[1]} on {r[2][:10]})" for r in rows]
        return f"{len(rows)} active admin(s):\n" + "\n".join(lines)
    except Exception as e:
        return f"admins error: {e}"


def _cmd_capabilities(sender: str = "") -> str:
    try:
        gen_provider = choose_generation_provider()
    except Exception:
        gen_provider = "unknown"
    try:
        scan_provider = choose_scan_provider()
    except Exception:
        scan_provider = "unknown"

    scan_state = "on" if scan_provider not in ("disabled", "unknown") else scan_provider
    gen_state = "on" if gen_provider not in ("disabled", "unknown") else gen_provider
    lines = [
        "Major capabilities:",
        f"- Image scan: {scan_state} via {scan_provider}. Attach/send an image, then ask `what's in this screenshot?` or `gpt scan image [ask]`. In GCs, send the image in that chat and ask `@Davos what's in this?` within about two minutes.",
        f"- Image generation: {gen_state} via {gen_provider}. Use `image gen [prompt]`; generated files are sent back as iMessage attachments. Use `image queue` if a send needs retry.",
        "- UFC / sports lookup: ask `ufc card` for the upcoming card. Sports recap crons use ESPN scoreboards.",
        "- Markets: `market`, `market movers`, `quote NVDA`, `market status`; owner can use `market alerts on|off|status`.",
        "- Fantasy dashboard: people in a Davos-enabled group use `@Davos fantasy`, open the link, and sign in with ChatGPT. The first sign-in creates the access request automatically.",
        "- Sports recap cron: owner can ask from the target chat, e.g. `create daily sports recap cron at 6pm`. Output is Live, Finished, then Scheduled; MLB is current-day only; UNC futures are suppressed.",
        "- Owner reminders and one-offs: natural language works, e.g. `remind me tomorrow at 3pm`, `list reminders`, `delete reminders 1 and 2`, `move my 9am to 10am`, `send Cole \"happy birthday\" tomorrow at 9am`.",
        "- Web/weather: owner/admin DMs and approved GCs can ask current questions or `weather in Belltown`; add `no web search` to skip live search.",
        "- Food in owner/admin DMs: `order wings` asks which service, pickup/delivery, and area. Optional DoorDash/Uber Eats checkout needs your own signed-in Mini browser, an exact cart/total review, and `food confirm CODE`. Use `food status`, `food details [choices]`, `food resume`, or `food cancel`. Unconnected/direct services still get ordering links; a purchase is reported only from a verified merchant receipt.",
        "- API/tool status: `api status` shows configured tools without secrets.",
        "- Model/routing status: `model status`, `model options`, `model intensity`, `billing`.",
        "- Change log/Codex repair: `log [thing]`, `analyze this and log`, `log this and fix it`, `fix yourself: [issue]` or `Davos fix [issue]`, `log board`, `ship safe cleanup` / `master prompt`, then after deploy smoke `log done #id #id`. `log clear` still requires confirm.",
    ]
    if is_owner(sender):
        lines.extend([
            "- Owner tools: personas, group-chat enable/allow/revoke, skills, workouts, social bets, code review scan, cron/job management.",
            "- Cleanup automation: Mini hourly monitor can DM `want me to fix`; nightly 3am safe cleanup can run Codex silently and clears only completed IDs.",
            "- Boundary: Davos does not wipe logs blindly. Safe cleanup produces a Codex handoff and an exact post-smoke `log done` line.",
        ])
    elif is_admin(sender):
        lines.append("- Admin access: chat, search/weather, image scan/gen, UFC, bets, skills, social bets, and approved admin commands. Owner-only changes still stay owner-only.")
    else:
        lines.append("- Friend access: chat, approved-GC web/weather, image scan/gen quota, UFC card, and sports bet commands. Type `mypermissions` for your tier.")
    return "\n".join(lines)


def _cmd_cron_help() -> str:
    return "\n".join([
        "Cron commands:",
        "- List: `list crons`, `list all crons`, `list group crons`, `list crons just to me`.",
        "- Describe: `describe cron #7`.",
        "- Edit: `set #7 to 6:30am`, `make the morning one 7am`, `change cron #7 to friday 8pm`.",
        "- Disable: `turn off #7` or `delete #7`. Use `list all crons` first if you are not 100% sure.",
        "- Sports recap: from the target chat, `create daily sports recap cron at 6pm` or `fix the sports cron`.",
        "- Bad schedules fail closed; malformed cron JSON is checked by `runtime_smoke.py` and `master_smoke.py`.",
    ])


def _cmd_help(sender: str = "") -> str:
    # Friend / unapproved view
    if sender and not is_admin(sender):
        return (
            "Here's what I can do for you:\n"
            "- Just ask me anything (web search included, 5/day)\n"
            "- Market quotes: `market`, `market movers`, or `quote NVDA` (5/day)\n"
            "- Send me an image and I'll describe / analyze it (5/day)\n"
            "- Reminders — just talk to me, no syntax:\n"
            "    'remind me to call mom in 2 hours'\n"
            "    'what reminders do i have?'\n"
            "    'cancel my 3pm reminder' / 'delete reminders 1 and 2'\n"
            "    'move my 9am to 10am'\n"
            "- Sports bets (slash commands):\n"
            "    ufc card — upcoming UFC card, main card, prelims\n"
            "    /bet log Lakers -110 2u — log a bet\n"
            "    /bet settle win|loss|push — settle your last open bet\n"
            "    /bet stats [week/month/all] — P&L, win rate, ROI\n"
            "- Fourth Down access in a Davos-enabled group:\n"
            "    @Davos fantasy — open the link and sign in to request access\n"
            "- Add 'no web search' to any message to skip Tavily\n"
            "\nType 'capabilities' for the major feature map, or 'mypermissions' to see your access level."
        )

    # Admin view — what admins can actually use (not owner-only commands)
    if sender and not is_owner(sender):
        return (
            "CONVERSATION\n"
            "- Just ask anything — full LLM with web search\n"
            "- Send images for analysis (vision model)\n"
            "- Image scan/gen (configured provider): `make me a logo`, `image gen ...`, `what's in this screenshot?`\n"
            "- Add 'no web search' / 'skip search' to skip Tavily\n"
            "- Market quotes: `market`, `market movers`, `quote NVDA`\n"
            "\n"
            "REMINDERS — just talk to me, no syntax\n"
            "  Set: 'remind me to call mom in 2 hours' / 'remind me tomorrow at 3pm'\n"
            "  See: 'what reminders do i have?' / 'list my reminders'\n"
            "  Edit: 'move my 3pm reminder to 4pm'\n"
            "  Cancel: 'cancel reminder 2' / 'delete reminders 1 and 2' / 'cancel my gym reminder'\n"
            "  Routes back to wherever you asked (DM or this GC)\n"
            "\n"
            "SPORTS BETS (everyone, units-based tracker)\n"
            "- ufc card — upcoming UFC card, main card, prelims\n"
            "- /bet log [event] [odds] [stake]u — log a bet\n"
            "    e.g. /bet log Lakers -110 2u\n"
            "    e.g. /bet log Knicks ML +150 1.5u\n"
            "- /bet settle [win/loss/push] — settle your last open bet\n"
            "    e.g. /bet settle win\n"
            "    e.g. /bet settle 3 loss — settle by bet ID\n"
            "- /bet stats [week/month/all] — P&L, win rate, ROI\n"
            "\n"
            "SOCIAL BETS (Admin+, head-to-head)\n"
            "- bets — list your open social bets\n"
            "- bets new [handle] [amount] [description]\n"
            "    e.g. bets new <handle> 50 who wins the fight\n"
            "- bets settle [id] [winner_handle]\n"
            "\n"
            "SKILLS (Admin+)\n"
            "- skills — list registered skills + status\n"
            "- skill enable [name] / skill disable [name]\n"
            "\n"
            "OTHER (Admin+)\n"
            "- sharecontact [email] — email the Davos contact card\n"
            "- mypermissions — show your access level\n"
            "- help — show this list\n"
        )

    # Owner full view
    personas = ", ".join(list_personas()) or "none"
    return (
        "SYSTEM\n"
        "- status — PM2 process + DB session combined\n"
        "- uptime — current session duration\n"
        "- logs — last 20 PM2 log lines\n"
        "- pull — git pull --ff-only + hook check + pm2 restart (shows commit hash/msg/author)\n"
        "- billing — Gemini token usage + estimated cost\n"
        "- api status — configured API/tool map without secret values\n"
        "- model status/options/intensity — current model/provider routing without secrets\n"
        "- model request [route] [model or goal] — log a review-only model-change handoff\n"
        "- backups — last 5 DB backups with size + timestamp\n"
        "- alert status / alert test — owner alert webhook config + smoke\n"
        "- fantasy — open Fourth Down\n"
        "- fantasy requests — pending dashboard access requests\n"
        "- fantasy users — all dashboard access records\n"
        "- fantasy grant #ID viewer|editor|owner\n"
        "- fantasy promote #ID viewer|editor|owner\n"
        "- fantasy revoke #ID - block access and return the user to pending\n"
        "\n"
        "MARKETS\n"
        "- market / stocks: live Mag 7, Nasdaq, and S&P 500 snapshot\n"
        "- market movers: biggest Mag 7 moves, including extended hours\n"
        "- quote NVDA: one-symbol quote with timestamp, session, freshness, range, and volume\n"
        "- market status / market help: watchlist, sources, thresholds, and commands\n"
        "- market alerts on|off|status: owner-only persistent alert control\n"
        "\n"
        "MEMORY\n"
        "- memory — show long-term facts (MEMORY.md)\n"
        "- memory wipe — reset MEMORY.md to baseline\n"
        "- memory add [fact] — append a single fact\n"
        "- memory clear — wipe full conversation history\n"
        "- memory clear 30m — clear last 30 minutes\n"
        "- memory clear 10 — clear last 10 messages\n"
        "- memory note [fact] — save an owner-private searchable note\n"
        "- memory search [query] / memory notes — search or list private notes\n"
        "- myfacts — list extracted user_facts from DB\n"
        "- enrichsoul — append user_facts as section to SOUL.md\n"
        "- (only YOUR messages can write to MEMORY now — friends can't poison it)\n"
        "\n"
        "PERSONA\n"
        "- persona — show current persona + available\n"
        "- persona [name] — switch active DM persona\n"
        "- persona reset — back to default (SOUL.md)\n"
        "- personalities — list + validate persona files\n"
        "- soulversion — last 5 SOUL.md write entries\n"
        "- restoresoul [file] — restore SOUL.md from named backup\n"
        "- switch to [name] / be [name] / activate [name] — NL switch\n"
        "\n"
        "ADMIN MANAGEMENT\n"
        "- grant [handle] — elevate handle to admin tier\n"
        "- revoke [handle] — remove admin status\n"
        "- admins — list active admins\n"
        "- ratelimit [handle] — hourly message count for handle\n"
        "- log / changelog — view triaged change log\n"
        "- ship safe cleanup / master prompt — build GREEN/YELLOW/RED Codex handoff; no edits or deploy\n"
        "- big change [idea] / codex plan [idea] — log a review-only Codex intake\n"
        "- analyze/log/fix/ship repair phrases — `analyze this and log`, `log this and fix it`, `ship this cron fix`, or `fix yourself: [issue]` create review-only Codex repair rows\n"
        "- log screenshot issue — after sending a screenshot, scan it and use the same self-repair handoff\n"
        "- diagnose yourself: [issue] — same repair workflow with RED/YELLOW risk tagging\n"
        "- image access status/revoke/allow/extend/reset [handle] — owner-only image quota controls\n"
        "- log [text] — write new entry\n"
        "- log export — write a private gitignored change-log snapshot for SSH/Codex\n"
        "- log remove [id] — delete one entry\n"
        "- log clear — wipe all entries (confirm required)\n"
        "\n"
        "REMINDERS — just talk to me, no syntax needed\n"
        "  Set: 'remind me to call mom in 2 hours' / 'remind me tomorrow at 3pm'\n"
        "  See: 'what reminders do i have?' / 'list my reminders'\n"
        "  Edit: 'move my 3pm reminder to 4pm' / 'change tomorrow's gym to 7am'\n"
        "  Cancel: 'cancel reminder 2' / 'delete reminders 1 and 2' / 'cancel my 9am' / 'drop the gym one'\n"
        "  Routes back to wherever you asked (DM stays in DM, GC stays in GC)\n"
        "  Internal IDs are hidden — list shows positional 1, 2, 3\n"
        "\n"
        "DAILY JOBS / CRON — also just ask\n"
        "  Set: 'every morning at 6:30 PT send good morning boys + a quote'\n"
        "       'daily at 9am send me an inspirational message'\n"
        "       'every day at 10pm in this chat post a wind-down quote'\n"
        "  See: 'what daily jobs do we have?' / 'list crons'\n"
        "       'list all crons' shows every active job with stable #ids\n"
        "       'list crons just to me' / 'list group crons' filters the view\n"
        "  Cancel: 'cancel the 6:30 daily' / 'kill the morning job'\n"
        "          'cancel cron 7' / 'delete #7' disables a specific stable cron id\n"
        "  Edit: 'set #7 to 6:30am' / 'make the morning one 7am'\n"
        "  Help: 'cron help' / 'api status' if Davos forgets what tools exist\n"
        "  Each fire uses ZenQuotes with attribution, then Gemini/local fallback if the API is unavailable\n"
        "  Scoped to the chat you ask from (DM stays DM, GC stays GC)\n"
        "  Owner-only (admins can elevate with the password gate)\n"
        "\n"
        "SPORTS BETS (everyone)\n"
        "- ufc card — upcoming UFC card, main card, prelims\n"
        "- /bet log [event] [odds] [stake]u — log a bet\n"
        "    e.g. /bet log Lakers -110 2u  |  /bet log Knicks ML +150 1.5u\n"
        "- /bet settle [win/loss/push] — settle last open bet\n"
        "    e.g. /bet settle 3 loss — settle specific bet by ID\n"
        "- /bet stats [week/month/all] — P&L, win rate, ROI\n"
        "\n"
        "SOCIAL BETS (Admin+, head-to-head)\n"
        "- bets — list your open social bets\n"
        "- bets new [handle] [amount] [description]\n"
        "- bets settle [id] [winner_handle]\n"
        "\n"
        "WORKOUTS\n"
        "- workout — today's logged entries\n"
        "- /workout log [exercise] [sets] — e.g. /workout log bench 185x5x3\n"
        "- /workout summary — weekly volume + top lifts + progression\n"
        "- /workout plan — adaptive AI recommendation for today\n"
        "\n"
        "ONE-OFF SCHEDULED iMESSAGES (different from cron — fires once)\n"
        "- 'send Cole \"happy birthday\" tomorrow at 9am' — LLM schedules\n"
        "- scheduled — list pending one-off scheduled iMessages\n"
        "- cancel [id] — cancel a pending one-off task\n"
        "- ping — list enabled group chat IDs\n"
        "- ping [id] — send test pong to verify GC routing\n"
        "\n"
        "- chats stale - preview stale group-routing warnings\n"
        "- chats disable stale confirm - owner-only cleanup for stale enabled GC IDs\n"
        "\n"
        "SKILLS\n"
        "- skills — list registered skills + status\n"
        "- skill enable [name] / skill disable [name]\n"
        "- Create new skills via natural language (LLM creates skill + handler)\n"
        "\n"
        "TOOLS / FILES (LLM via tool calls — owner-only)\n"
        "- read_file / write_file / shell_exec / sqlite_query\n"
        "- web_search (Tavily) — add 'no web search' to skip\n"
        "- Image analysis — send any image, vision model describes/analyzes\n"
        "- Image scan/gen - `make me a logo`, `image gen [prompt]`, or `what's in this screenshot?`\n"
        "- Screenshot/self-repair bug loop - send screenshot or describe the failure, then `analyze this and log`, `log this and fix it`, or `fix yourself: [issue]`; use `master prompt` for Codex handoff\n"
        "- generate_file — CSV/TXT created and sent over iMessage\n"
        "- edit_persona / create_persona — modify or add persona files\n"
        "- send_imessage — send NOW or schedule (scheduled_time_utc)\n"
        "- log_change_request — append to change log (batch from numbered lists)\n"
        "\n"
        "OTHER\n"
        "- drift / weekly / maintenance — health report (crons, recent errors, change log, queues, recent commits)\n"
        "- scan [filename] — Gemini code review of a project file\n"
        "- sharecontact [email] — send Davos.vcf via SMTP\n"
        "- mypermissions — show your access level\n"
        "- chats — list all enabled group chats (DM only)\n"
        "\n"
        "- chats stale / chats disable stale confirm - inspect or clean stale GC routing warnings\n"
        "\n"
        "GROUP CHAT (@Davos in GC)\n"
        "- @Davos on / off — enable or disable bot in this GC\n"
        "- @Davos allow [+number] — grant friend access (all GCs)\n"
        "- @Davos revoke [+number] — remove friend access\n"
        "- @Davos persona [name] / persona reset — switch GC persona\n"
        "- @Davos create group persona Name: style notes — create a persona only for this GC\n"
        "- @Davos grant persona editor [+number] — let that user tweak this GC's active group persona\n"
        "- @Davos update group persona: style tweak — granted editors can customize only this GC persona\n"
        "- @Davos tell Chapman [message] — persona-styled public relay in this GC (does not private-text)\n"
        "- @Davos help — show help in GC\n"
        "- Code-change tools blocked in GC (defense against prompt injection)\n"
        "\n"
        "INTENT CLASSIFIERS (auto, before LLM)\n"
        "- 'remind me ...' / 'set a reminder' ? schedules\n"
        "- 'cancel reminder ...' ? cancels\n"
        "- 'the reminder didn't go through' ? does NOT touch DB; bot offers to re-set\n"
        "- bare 'log' ? shows log; 'log [text]' ? writes; 'we should log this' ? LLM\n"
        "\n"
        "PASSWORD GATE\n"
        "- Admins can include the admin password in a message to be routed as owner\n"
        "  for that single message (password stripped before LLM sees it)\n"
        "\n"
        f"Personas: {personas}"
    )


def _cmd_mypermissions(sender: str) -> str:
    from .permissions import ADMIN_ALLOWED_ACTIONS
    if is_owner(sender):
        return "You're the owner. Full access."
    if is_admin(sender):
        actions = ", ".join(ADMIN_ALLOWED_ACTIONS)
        return f"You're an admin. You can: {actions}"
    if is_approved_user(sender):
        return (
            "You're an approved friend. You can:\n"
            "• Ask questions and get answers\n"
            "• Get web search results in group chats\n"
            "• Check reminders\n"
            "Contact the owner to request more access."
        )
    return "You don't have access. Talk to the owner to get added."


def _cmd_myfacts(sender: str) -> str:
    denial = check_action_permission(sender, "manage_memory")
    if denial:
        return denial
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT key, value, source, timestamp FROM user_facts ORDER BY id DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return "No facts recorded yet (table not initialized)."
    if not rows:
        return "No user facts recorded yet."
    lines = [f"• {k}: {v}  ({s}, {t[:10]})" for k, v, s, t in rows]
    return f"User facts ({len(rows)}):\n" + "\n".join(lines)


def _cmd_enrichsoul(sender: str) -> str:
    denial = check_action_permission(sender, "modify_soul")
    if denial:
        return denial
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute("SELECT key, value FROM user_facts ORDER BY id ASC").fetchall()
    except sqlite3.OperationalError:
        return "No facts to enrich."
    if not rows:
        return "No user facts to add."
    from .soul import read_soul, write_soul
    soul = read_soul()
    base = re.sub(r"\n+## Known about the owner.*?(?=\n## |\Z)", "", soul, flags=re.DOTALL).rstrip()
    section = "\n\n## Known about the owner\n" + "\n".join(f"- {k}: {v}" for k, v in rows)
    new_content = base + section
    write_soul(new_content, "enrichsoul: appended user_facts", sender=sender)
    return f"Enriched SOUL.md with {len(rows)} fact(s)."


def create_skill(sender: str, skill_name: str, trigger_phrase: str, response_template: str) -> str:
    """Insert a new skill into the skills table. Admin and owner only."""
    denial = check_action_permission(sender, "create_skill")
    if denial:
        return denial
    skill_name = skill_name.strip().lower()
    trigger_phrase = trigger_phrase.strip()
    response_template = response_template.strip()
    if not skill_name or not trigger_phrase or not response_template:
        return "Usage: create_skill name | trigger phrase | response template"
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO skills (created_by, skill_name, trigger_phrase, response_template) "
                "VALUES (?, ?, ?, ?)",
                (sender, skill_name, trigger_phrase, response_template),
            )
            conn.commit()
        return f"Skill '{skill_name}' created. Trigger: '{trigger_phrase}'"
    except sqlite3.IntegrityError:
        return f"A skill named '{skill_name}' already exists. Use 'skill disable {skill_name}' first."
    except Exception as e:
        return f"Skill creation failed: {e}"


def _cmd_skills(sender: str) -> str:
    """List all registered skills with trigger phrase and status."""
    denial = check_action_permission(sender, "create_skill")
    if denial:
        return denial
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT skill_name, trigger_phrase, enabled, created_by FROM skills ORDER BY skill_name"
            ).fetchall()
    except sqlite3.OperationalError:
        return "Skills table not initialized."
    if not rows:
        return "No skills registered yet."
    lines = [
        f"{'?' if r[2] else '?'} {r[0]} — trigger: '{r[1]}' (by {r[3]})"
        for r in rows
    ]
    return f"Skills ({len(rows)}):\n" + "\n".join(lines)


def _cmd_skill_manage(text: str, sender: str) -> str:
    """Handle 'skill enable [name]' and 'skill disable [name]'."""
    denial = check_action_permission(sender, "create_skill")
    if denial:
        return denial
    parts = text.strip().split(None, 2)
    if len(parts) < 3:
        return "Usage: skill enable [name] | skill disable [name]"
    action = parts[1].lower()
    name = parts[2].strip().lower()
    if action not in ("enable", "disable"):
        return "Usage: skill enable [name] | skill disable [name]"
    enabled = 1 if action == "enable" else 0
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            cur = conn.execute(
                "UPDATE skills SET enabled = ? WHERE skill_name = ?", (enabled, name)
            )
            conn.commit()
        if cur.rowcount == 0:
            return f"Skill '{name}' not found."
        return f"Skill '{name}' {action}d."
    except Exception as e:
        return f"skill {action} failed: {e}"


def _get_unit_size(sender: str) -> float:
    """Return sender's configured unit size in dollars (default $10)."""
    from contextlib import closing
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT value FROM bet_config WHERE sender = ? AND key = 'unit_size'",
                (sender,),
            ).fetchone()
        return float(row[0]) if row else 10.0
    except Exception:
        return 10.0


def _parse_bet_input(text: str) -> dict | str:
    """Parse '/bet log Lakers -110 2u' into structured fields.

    Returns dict: {event, odds, stake, bet_type, notes}
    Returns str (error) if ambiguous.
    """
    # Strip command prefix
    raw = re.sub(r"^/bet\s+log\s*|^bet\s+log\s*", "", text.strip(), flags=re.IGNORECASE).strip()
    if not raw:
        return "What bet? Try: /bet log Lakers -110 2u"

    # Regex: <event> <odds> <stake>[u]
    # "Lakers -110 2u", "Knicks ML +150 1.5u", "Patriots -3.5 spread 2u"
    m = re.match(
        r"^(.+?)\s+([+-]\d+)\s+([\d.]+)u?(.*)?$",
        raw, re.IGNORECASE
    )
    if not m:
        # Try without odds: "Lakers 2u" — ask for odds
        m2 = re.match(r"^(.+?)\s+([\d.]+)u(.*)$", raw, re.IGNORECASE)
        if m2:
            return f"Got the bet on {m2.group(1).strip()} for {m2.group(2)}u — what are the odds? (e.g. -110 or +200)"
        return (
            f"Couldn't parse '{raw}'.\n"
            "Try: /bet log Lakers -110 2u\n"
            "     /bet log Knicks ML +150 1.5u"
        )

    event = m.group(1).strip()
    odds = int(m.group(2))
    stake = float(m.group(3))
    notes = m.group(4).strip() if m.group(4) else ""
    # Detect bet type from common keywords
    bet_type = "moneyline"
    if re.search(r"\bspread\b", notes or event, re.IGNORECASE):
        bet_type = "spread"
    elif re.search(r"\bparlay\b", notes or event, re.IGNORECASE):
        bet_type = "parlay"
    return {"event": event, "odds": odds, "stake": stake, "bet_type": bet_type, "notes": notes}


def _calc_payout(odds: int, stake: float, result: str) -> float:
    """Calculate payout in units given result."""
    if result == "push":
        return 0.0
    if result == "loss":
        return -stake
    # Win
    if odds > 0:
        return stake * (odds / 100)
    else:
        return stake * (100 / abs(odds))


def _cmd_bet_log(text: str, sender: str) -> str:
    """Log a sports bet. Open to everyone."""
    from contextlib import closing
    parsed = _parse_bet_input(text)
    if isinstance(parsed, str):
        return parsed
    unit = _get_unit_size(sender)
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            cur = conn.execute(
                "INSERT INTO sports_bets "
                "(sender, event, bet_type, odds, stake, unit_size, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sender, parsed["event"], parsed["bet_type"],
                 parsed["odds"], parsed["stake"], unit, parsed["notes"]),
            )
            bid = cur.lastrowid
            conn.commit()
    except Exception as e:
        return f"Bet log failed: {e}"
    dollar = parsed["stake"] * unit
    return (
        f"Bet #{bid} logged — {parsed['event']} {parsed['odds']:+d} | "
        f"{parsed['stake']}u (${dollar:.2f}) | pending"
    )


def _cmd_bet_settle(text: str, sender: str) -> str:
    """Settle a pending sports bet."""
    from contextlib import closing
    # Parse: "/bet settle [win/loss/push]" or "/bet settle [id] [win/loss/push]"
    raw = re.sub(r"^/bet\s+settle\s*|^bet\s+settle\s*", "", text.strip(), flags=re.IGNORECASE).strip()

    result = None
    bet_id = None
    for word in raw.split():
        if word.lower() in ("win", "won", "cashed", "hit"):
            result = "win"
        elif word.lower() in ("loss", "lost", "missed"):
            result = "loss"
        elif word.lower() in ("push", "refund"):
            result = "push"
        elif word.isdigit():
            bet_id = int(word)

    if result is None:
        return "Settle as what? Try: /bet settle win | /bet settle loss | /bet settle push"

    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            if bet_id:
                row = conn.execute(
                    "SELECT id, event, odds, stake, unit_size, sender FROM sports_bets "
                    "WHERE id = ? AND result = 'pending'",
                    (bet_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, event, odds, stake, unit_size, sender FROM sports_bets "
                    "WHERE sender = ? AND result = 'pending' ORDER BY id DESC LIMIT 1",
                    (sender,),
                ).fetchone()
            if not row:
                return (
                    "No pending bet found. "
                    "Use '/bet log' first or specify the bet ID."
                )
            rid, event, odds, stake, unit, bet_sender = row
            if bet_sender != sender and not is_admin(sender):
                return "Only the bet owner, an admin, or owner can settle that bet."
            payout = _calc_payout(odds, stake, result)
            conn.execute(
                "UPDATE sports_bets SET result = ?, payout = ?, settled_at = datetime('now') WHERE id = ?",
                (result, payout, rid),
            )
            conn.commit()
    except Exception as e:
        return f"Settle failed: {e}"

    unit_size = unit or _get_unit_size(sender)
    dollar = abs(payout) * unit_size
    if result == "win":
        return f"{bet_sender} cashed: +{payout:.2f}u (+${dollar:.2f}) on {event}"
    elif result == "loss":
        return f"Bet #{rid} settled as loss: {payout:.2f}u on {event}."
    else:
        return f"Bet #{rid} pushed — no P&L on {event}."


def _cmd_bet_stats(text: str, sender: str) -> str:
    """Return bet stats for a user or the group."""
    from contextlib import closing
    raw = re.sub(r"^/bet\s+stats\s*|^bet\s+stats\s*", "", text.strip(), flags=re.IGNORECASE).strip()

    # Parse target and timeframe
    group_mode = bool(re.search(r"\bgroup\b|\ball\b", raw, re.IGNORECASE))
    timeframe_sql = "AND date >= date('now', 'weekday 0', '-7 days')"  # default this week
    if re.search(r"\btoday\b", raw, re.IGNORECASE):
        timeframe_sql = "AND date = date('now')"
    elif re.search(r"\bmonth\b", raw, re.IGNORECASE):
        timeframe_sql = "AND strftime('%Y-%m', date) = strftime('%Y-%m', 'now')"
    elif re.search(r"\blast\s+7\b", raw, re.IGNORECASE):
        timeframe_sql = "AND date >= date('now', '-7 days')"

    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            if group_mode:
                # Group leaderboard
                rows = conn.execute(
                    f"SELECT sender, SUM(CASE WHEN result='win' THEN payout WHEN result='loss' THEN payout ELSE 0 END) as net_units, "
                    f"COUNT(*) as total, SUM(stake) as wagered "
                    f"FROM sports_bets WHERE result != 'pending' {timeframe_sql} "
                    f"GROUP BY sender ORDER BY net_units DESC"
                ).fetchall()
                if not rows:
                    return "No settled bets this period."
                unit_size = _get_unit_size(sender)
                lines = ["Group P&L:"]
                for s, net, total, wagered in rows:
                    lines.append(f"{s}: {net:+.2f}u (${net * unit_size:+.2f})")
                net_total = sum(r[1] for r in rows)
                lines.append(f"-----\nTotal: {net_total:+.2f}u")
                return "\n".join(lines)
            else:
                rows = conn.execute(
                    f"SELECT result, odds, stake, payout, event FROM sports_bets "
                    f"WHERE sender = ? AND result != 'pending' {timeframe_sql} ORDER BY id",
                    (sender,),
                ).fetchall()
                if not rows:
                    return "No settled bets this period. Use /bet log to add one."
                wins = sum(1 for r in rows if r[0] == "win")
                losses = sum(1 for r in rows if r[0] == "loss")
                pushes = sum(1 for r in rows if r[0] == "push")
                wagered = sum(r[2] for r in rows)
                net = sum((r[3] or 0) for r in rows)
                roi = (net / wagered * 100) if wagered > 0 else 0
                win_rate = (wins / len(rows) * 100) if rows else 0
                unit_size = _get_unit_size(sender)
                best = max(rows, key=lambda r: (r[3] or 0))
                worst = min(rows, key=lambda r: (r[3] or 0))
                return (
                    f"{sender} — This Week:\n"
                    f"Bets: {len(rows)} | W: {wins} L: {losses} P: {pushes}\n"
                    f"Win rate: {win_rate:.1f}%\n"
                    f"Net: {net:+.2f}u (${net * unit_size:+.2f})\n"
                    f"ROI: {roi:+.1f}%\n"
                    f"Best: {best[4]} {best[1]:+d} | {(best[3] or 0):+.2f}u\n"
                    f"Worst: {worst[4]} {worst[1]:+d} | {(worst[3] or 0):+.2f}u"
                )
    except Exception as e:
        return f"Stats error: {e}"


def _cmd_bets(text: str, sender: str) -> str:
    """List / create / settle bets.

    Syntax:
      bets                                — list
      bets new [opponent] [amount] [desc] — create
      bets settle [id] [winner]           — settle (creator/owner/admin only)
    """
    from contextlib import closing
    if not is_admin(sender):
        return "Social bets are admin-only."

    parts = text.strip().split(None, 3)
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub in ("new", "create"):
        if len(parts) < 4:
            return "Usage: bets new [opponent] [amount] [description]"
        opponent = normalize_handle(parts[2]) if "@" not in parts[2] else parts[2].lower()
        amt_desc = parts[3].split(None, 1)
        try:
            amount = float(amt_desc[0])
        except ValueError:
            return "Amount must be a number."
        description = amt_desc[1] if len(amt_desc) > 1 else ""
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            cur = conn.execute(
                "INSERT INTO bets (created_by, challenger, opponent, description, amount, status) "
                "VALUES (?, ?, ?, ?, ?, 'open')",
                (sender, sender, opponent, description, amount),
            )
            bid = cur.lastrowid
            conn.commit()
        return f"Bet #{bid} created: {sender} vs {opponent} for ${amount} — {description}"

    if sub == "settle":
        if len(parts) < 4:
            return "Usage: bets settle [id] [winner]"
        try:
            bid = int(parts[2])
        except ValueError:
            return "Bet id must be a number."
        winner_raw = parts[3].strip()
        winner = normalize_handle(winner_raw) if "@" not in winner_raw else winner_raw.lower()
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT created_by, status FROM bets WHERE id = ?", (bid,)
            ).fetchone()
            if not row:
                return f"Bet #{bid} not found."
            created_by, status = row
            if status != "open":
                return f"Bet #{bid} already {status}."
            if sender != created_by and not is_admin(sender):
                return "Only the bet creator, an admin, or owner can settle this."
            conn.execute(
                "UPDATE bets SET status='settled', winner=?, settled_at=datetime('now') WHERE id=?",
                (winner, bid),
            )
            conn.commit()
        return f"Bet #{bid} settled. Winner: {winner}."

    # Default: list
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            if is_admin(sender):
                rows = conn.execute(
                    "SELECT id, challenger, opponent, description, amount, status "
                    "FROM bets WHERE status = 'open' ORDER BY id DESC LIMIT 10"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, challenger, opponent, description, amount, status "
                    "FROM bets WHERE status = 'open' AND (challenger = ? OR opponent = ?) "
                    "ORDER BY id DESC LIMIT 10",
                    (sender, sender),
                ).fetchall()
    except sqlite3.OperationalError:
        return "No bets yet (table not initialized)."
    if not rows:
        return "No open bets involving you."
    lines = [f"#{r[0]} {r[1]} vs {r[2]}: {r[3]} (${r[4]}) [{r[5]}]" for r in rows]
    return "Open bets:\n" + "\n".join(lines)


def _cmd_sharecontact(text: str, sender: str) -> str:
    denial = check_action_permission(sender, "send_contact_card")
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or "@" not in parts[1]:
        return "Usage: sharecontact [email]"
    email = parts[1].strip()
    try:
        from .tools import _send_contact_card
        return _send_contact_card(email)
    except Exception as e:
        return f"sharecontact failed: {e}"


def _cmd_scan(text: str, sender: str) -> str:
    denial = check_action_permission(sender, "view_logs")
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return "Usage: scan [filename]"
    try:
        from .tools import _scan_file
        return _scan_file(parts[1].strip())
    except Exception as e:
        return f"scan failed: {e}"


def _parse_workout_input(text: str) -> dict | str:
    """Parse natural language workout input into structured data.

    Accepts: "bench 185x5x3", "squat 225 5x5 felt heavy",
             "bench press 3 sets: 185x5, 190x5, 195x4"
    Returns dict with keys: exercise_name, sets_json, muscle_group (guessed), notes
    Returns str (error message) if input is ambiguous.
    """
    import json as _json
    # Strip command prefix
    text = re.sub(r"^/workout\s+log\s*|^workout\s+log\s*", "", text.strip(), flags=re.IGNORECASE).strip()
    if not text:
        return "What did you do? Try: /workout log bench 185x5x3"

    # Try to match various weight+sets+reps formats.
    # Pattern 1a: "bench 185x5x3" — weight × reps × sets (all joined)
    m = re.match(
        r"^(.+?)\s+(\d+(?:\.\d+)?)\s*x\s*(\d+)\s*x\s*(\d+)(.*)?$",
        text, re.IGNORECASE
    )
    if m:
        exercise = m.group(1).strip()
        weight = float(m.group(2))
        reps = int(m.group(3))
        sets_count = int(m.group(4))
        notes = m.group(5).strip() if m.group(5) else ""
        sets_json = _json.dumps([{"weight": weight, "reps": reps}] * sets_count)
        return {
            "exercise_name": exercise,
            "sets_json": sets_json,
            "muscle_group": _guess_muscle_group(exercise),
            "notes": notes,
            "summary": f"{exercise} {sets_count}x{reps} @ {weight}lbs",
        }

    # Pattern 1b: "squat 225 5x5" — exercise weight sets×reps (space before sets×reps)
    m1b = re.match(
        r"^(.+?)\s+(\d+(?:\.\d+)?)\s+(\d+)\s*x\s*(\d+)(.*)?$",
        text, re.IGNORECASE
    )
    if m1b:
        exercise = m1b.group(1).strip()
        weight = float(m1b.group(2))
        sets_count = int(m1b.group(3))
        reps = int(m1b.group(4))
        notes = m1b.group(5).strip() if m1b.group(5) else ""
        sets_json = _json.dumps([{"weight": weight, "reps": reps}] * sets_count)
        return {
            "exercise_name": exercise,
            "sets_json": sets_json,
            "muscle_group": _guess_muscle_group(exercise),
            "notes": notes,
            "summary": f"{exercise} {sets_count}x{reps} @ {weight}lbs",
        }

    # Pattern 2: "bench 3 sets: 185x5, 190x5, 195x4"
    m2 = re.match(r"^(.+?)\s+\d+\s+sets?:\s*(.+)$", text, re.IGNORECASE)
    if m2:
        exercise = m2.group(1).strip()
        raw_sets = m2.group(2)
        sets = []
        for s in re.findall(r"(\d+(?:\.\d+)?)\s*x\s*(\d+)", raw_sets):
            sets.append({"weight": float(s[0]), "reps": int(s[1])})
        if sets:
            summary = f"{exercise} {len(sets)} sets"
            return {
                "exercise_name": exercise,
                "sets_json": _json.dumps(sets),
                "muscle_group": _guess_muscle_group(exercise),
                "notes": "",
                "summary": summary,
            }

    # Pattern 3: bodyweight "pushups 3x20" or "situps 20x3"
    m3 = re.match(r"^(.+?)\s+(\d+)\s*x\s*(\d+)(.*)?$", text, re.IGNORECASE)
    if m3:
        exercise = m3.group(1).strip()
        a, b = int(m3.group(2)), int(m3.group(3))
        # Heuristic: if one number looks like sets (1-10) and other is reps
        if a <= 10 and b > a:
            sets_count, reps = a, b
        else:
            sets_count, reps = b, a
        notes = m3.group(4).strip() if m3.group(4) else ""
        sets_json = _json.dumps([{"weight": 0, "reps": reps}] * sets_count)
        return {
            "exercise_name": exercise,
            "sets_json": sets_json,
            "muscle_group": _guess_muscle_group(exercise),
            "notes": notes,
            "summary": f"{exercise} {sets_count}x{reps} (bodyweight)",
        }

    # Ambiguous
    return (
        f"Couldn't parse '{text}'. Try:\n"
        "- bench 185x5x3  (weight x reps x sets)\n"
        "- squat 225 5x5\n"
        "- pushups 3x20 (bodyweight)"
    )


_MUSCLE_MAP = {
    "bench": "chest", "press": "chest", "fly": "chest", "flye": "chest",
    "squat": "legs", "leg": "legs", "lunge": "legs", "deadlift": "legs",
    "curl": "arms", "tricep": "arms", "dip": "arms",
    "row": "back", "pullup": "back", "pulldown": "back", "lat": "back",
    "shoulder": "shoulders", "shrug": "shoulders", "ohp": "shoulders",
    "plank": "core", "crunch": "core", "situp": "core", "ab": "core",
    "run": "cardio", "bike": "cardio", "swim": "cardio", "cardio": "cardio",
}


def _guess_muscle_group(exercise: str) -> str:
    lower = exercise.lower()
    for kw, group in _MUSCLE_MAP.items():
        if kw in lower:
            return group
    return "other"


def _cmd_workout_log(text: str, sender: str) -> str:
    """Parse and log a workout set. Triggered by /workout log or 'workout log'."""
    import json as _json
    parsed = _parse_workout_input(text)
    if isinstance(parsed, str):
        return parsed  # error / ambiguous — return guidance
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO workout_entries "
                "(sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (sender, parsed["muscle_group"], parsed["exercise_name"],
                 parsed["sets_json"], parsed["notes"]),
            )
            conn.commit()
    except Exception as e:
        return f"Workout log failed: {e}"
    return f"Logged — {parsed['summary']}"


def _cmd_workout_summary(sender: str) -> str:
    """Return this week's workout summary."""
    import json as _json
    from datetime import timedelta
    today = _utc_today()
    monday = today - timedelta(days=today.weekday())

    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT date, muscle_group, exercise_name, sets_json FROM workout_entries "
                "WHERE sender = ? AND date >= ? ORDER BY date, id",
                (sender, str(monday)),
            ).fetchall()
    except Exception as e:
        return f"Summary error: {e}"

    if not rows:
        return "No workouts logged this week. Use /workout log to add one."

    # Aggregate
    sessions = set()
    muscle_sets: dict[str, int] = {}
    exercise_weights: dict[str, list[float]] = {}

    for date, muscle, exercise, sets_raw in rows:
        sessions.add(date)
        try:
            sets = _json.loads(sets_raw)
        except Exception:
            sets = []
        n_sets = len(sets)
        muscle_sets[muscle or "other"] = muscle_sets.get(muscle or "other", 0) + n_sets
        for s in sets:
            w = s.get("weight", 0)
            if w > 0:
                exercise_weights.setdefault(exercise, []).append(w)

    lines = [f"Week of {monday}:", f"{len(sessions)} workout(s)"]

    muscle_line = " | ".join(f"{m}: {v} sets" for m, v in sorted(muscle_sets.items()))
    if muscle_line:
        lines.append(muscle_line)

    top = {ex: max(ws) for ex, ws in exercise_weights.items()}
    if top:
        lines.append("Top lifts:")
        for ex, w in sorted(top.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"- {ex}: {w}lbs")

    return "\n".join(lines)


def _cmd_workout_plan(sender: str) -> str:
    """Generate a workout recommendation based on recent history."""
    import json as _json
    from datetime import timedelta
    today = _utc_today()
    week_ago = today - timedelta(days=7)

    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT date, muscle_group, exercise_name, sets_json FROM workout_entries "
                "WHERE sender = ? AND date >= ? ORDER BY date DESC",
                (sender, str(week_ago)),
            ).fetchall()
    except Exception as e:
        return f"Plan error: {e}"

    if not rows:
        return "No workout history — what do you want to work on today?"

    # Find which muscle groups were trained each day
    day_muscles: dict[str, set[str]] = {}
    exercise_last: dict[str, dict] = {}  # exercise ? {date, max_weight, max_reps}

    for date, muscle, exercise, sets_raw in rows:
        day_muscles.setdefault(date, set()).add(muscle or "other")
        try:
            sets = _json.loads(sets_raw)
        except Exception:
            sets = []
        max_w = max((s.get("weight", 0) for s in sets), default=0)
        max_r = max((s.get("reps", 0) for s in sets), default=0)
        if exercise not in exercise_last or date > exercise_last[exercise]["date"]:
            exercise_last[exercise] = {"date": date, "weight": max_w, "reps": max_r}

    # Find muscle groups not trained in last 2 days
    recent_two = sorted(day_muscles.keys(), reverse=True)[:2]
    recent_muscles = set().union(*[day_muscles[d] for d in recent_two])
    all_trained = set().union(*day_muscles.values())
    fresh_muscles = all_trained - recent_muscles
    if not fresh_muscles:
        fresh_muscles = {"chest", "back", "legs"} - recent_muscles

    suggestions = []
    for ex, info in exercise_last.items():
        if _guess_muscle_group(ex) in fresh_muscles and info["weight"] > 0:
            next_w = info["weight"] + 5
            if next_w - info["weight"] <= 20:
                suggestions.append(
                    f"Next {ex}: {next_w}lbs x {info['reps']} "
                    f"(last: {info['weight']}lbs)"
                )

    lines = []
    if fresh_muscles:
        lines.append(f"Suggested today: {', '.join(sorted(fresh_muscles))}")
    if suggestions:
        lines.extend(suggestions[:3])
    else:
        lines.append("No specific suggestions — any of your usual lifts work.")

    return "\n".join(lines) if lines else "Keep it up — choose any muscle group you haven't hit recently."


def _cmd_workout(sender: str) -> str:
    """Show today's workout entries (legacy workouts table + new workout_entries)."""
    lines = []
    # New table
    try:
        import json as _json
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT exercise_name, sets_json, notes FROM workout_entries "
                "WHERE sender = ? AND date = date('now') ORDER BY id ASC",
                (sender,),
            ).fetchall()
        for ex, sets_raw, note in rows:
            try:
                sets = _json.loads(sets_raw)
                set_str = ", ".join(
                    f"{s['weight']}x{s['reps']}" if s.get('weight') else f"{s['reps']} reps"
                    for s in sets
                )
            except Exception:
                set_str = sets_raw
            entry = f"{ex}: {set_str}"
            if note:
                entry += f" ({note})"
            lines.append(entry)
    except Exception:
        pass
    # Legacy workouts table
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            old_rows = conn.execute(
                "SELECT exercise, sets, reps, weight_lbs, notes FROM workouts "
                "WHERE date(ts) = date('now') ORDER BY id ASC"
            ).fetchall()
        for ex, s, r, w, n in old_rows:
            parts = [ex]
            if s and r:
                parts.append(f"{s}x{r}")
            if w:
                parts.append(f"@ {w}lbs")
            if n:
                parts.append(f"({n})")
            lines.append(" ".join(parts))
    except Exception:
        pass

    if not lines:
        return "No workout logged today. Use /workout log to add one."
    return "Today's workout:\n" + "\n".join(lines)


def _cmd_scheduled(sender: str) -> str:
    denial = check_action_permission(sender, "view_session")
    if denial:
        return denial
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT id, recipient, scheduled_at, message FROM scheduled_tasks "
                "WHERE status = 'pending' ORDER BY scheduled_at ASC"
            ).fetchall()
    except sqlite3.OperationalError:
        return "No scheduled tasks (table not initialized)."
    if not rows:
        return "No pending scheduled tasks."
    from datetime import datetime, timezone
    _LA = ZoneInfo("America/Los_Angeles")
    lines = []
    for tid, rec, sched, msg in rows:
        try:
            dt = datetime.strptime(sched, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(_LA)
            tstr = dt.strftime("%b %d %I:%M %p %Z")
        except Exception:
            tstr = sched
        lines.append(f"#{tid} - {rec} at {tstr}: {msg[:40]}")
    return "Scheduled:\n" + "\n".join(lines)


@schedule_locked
def register_cron(sender: str, cron_expression: str, action_type: str, action_payload: dict) -> str:
    """Register a cron job. Owner-only via 'schedule_cron' action.

    Returns a friendly confirmation. Never punts to a generic future handoff
    when deterministic parsing is required; if parsing fails, returns the explicit error
    string per Stage 3 spec.
    """
    denial = check_action_permission(sender, "schedule_cron")
    if denial:
        return denial
    expr = cron_expression.strip()
    # Clock schedules must be real times. Other cron syntax needs the optional parser.
    clock = re.fullmatch(r"(\d{1,2}):(\d{2})", expr)
    if clock:
        if int(clock.group(1)) > 23 or int(clock.group(2)) > 59:
            return "Couldn't parse that schedule - use a time from 00:00 to 23:59."
    else:
        try:
            from croniter import croniter
            valid = croniter.is_valid(expr)
        except Exception:
            valid = False
        if not valid:
            return "Couldn't parse that schedule — try: 'every day at 8am PST'."
    import json as _json
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by)"
                " VALUES (?, ?, ?, 1, ?)",
                (expr, action_type, _json.dumps(action_payload or {}), sender),
            )
            conn.commit()
        return f"Cron job registered: {action_type} @ {expr}"
    except Exception as e:
        return f"Couldn't register cron: {e}"


def register_morning_message(sender: str, recipient: str, time_pst: str) -> str:
    """Owner intent shortcut: schedule daily good morning to a contact at HH:MM PST.

    time_pst can be 'HH:MM' or 'Ham'/'Hpm'. We normalize to HH:MM (24h, in PST).
    """
    from .config import normalize_handle
    rec = normalize_handle(recipient) if "@" not in recipient else recipient
    t = time_pst.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", t)
    if not m:
        return "Couldn't parse time — try '8am' or '08:00'."
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ampm = m.group(3)
    if mm > 59 or (ampm and not 1 <= hh <= 12) or (not ampm and hh > 23):
        return "Couldn't parse time - use 1-12 with am/pm, or 00:00 to 23:59."
    if ampm == "pm" and hh < 12:
        hh += 12
    elif ampm == "am" and hh == 12:
        hh = 0
    return register_cron(sender, f"{hh:02d}:{mm:02d}", "morning_message", {"recipient": rec})


@schedule_locked
def _cmd_cancel(text: str, sender: str) -> str:
    denial = check_action_permission(sender, "view_session")
    if denial:
        return denial
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return "Usage: cancel [id]"
    tid = int(parts[1].strip())
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            cur = conn.execute(
                "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (tid,),
            )
            conn.commit()
        if cur.rowcount == 0:
            return f"#{tid} not found or already done."
        return f"Cancelled #{tid}."
    except Exception as e:
        return f"cancel failed: {e}"


def _cmd_persona(text: str, sender: str = "") -> str:
    # Extract requested name early so we can log it even on denial.
    parts = text.strip().split(None, 1)
    requested = parts[1].lower().strip() if len(parts) > 1 else ""

    denial = check_action_permission(sender, "change_personality") if sender else None
    if denial:
        if requested:
            _log_persona_switch(sender, requested, success=False)
        return denial

    if not requested:
        return _persona_status("dm")

    name = requested
    if name in ("reset", "clear") or _is_default_persona_request(name):
        set_persona("dm", None)
        if sender:
            clear_history(sender)
        _log_persona_switch(sender, "default", success=True)
        return "Back to default personality."

    resolved = resolve_persona_name(name, include_hidden=True)
    if not resolved:
        visible = list_personas()
        return f"Unknown persona '{name}'. Available: {', '.join(visible)}"

    set_persona("dm", resolved)
    if sender:
        clear_history(sender)
    _log_persona_switch(sender, resolved, success=True)
    return f"Switched to {resolved}."


def handle_group_command(sender: str, chat_id: str, text: str) -> str | None:
    """Handle deterministic @Davos group commands."""
    lower = text.strip()
    group_command = re.sub(r"^@davos\b", "", lower, flags=re.IGNORECASE).strip()

    if re.fullmatch(r"fantasy(?:\s+.*)?", group_command, re.IGNORECASE):
        return _cmd_group_fantasy(sender, chat_id, group_command)

    if (
        group_command in ("help", "capability", "capabilities")
        and is_approved_user(sender)
    ):
        return (
            _cmd_help(sender)
            if group_command == "help"
            else _cmd_capabilities(sender)
        )

    if not is_owner(sender):
        return None

    group_tell = _parse_group_tell(lower)
    if group_tell:
        target, message = group_tell
        try:
            import hashlib as _hashlib
            import json as _json
            message_hash = _hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
            with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
                conn.execute(
                    "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                    (
                        sender,
                        "group_tell",
                        _json.dumps({
                            "chat_id": chat_id,
                            "target": target[:80],
                            "message_hash": message_hash,
                            "message_len": len(message),
                            "resolution_path": "group_in_chat_relay",
                        }),
                    ),
                )
        except Exception as e:
            logger.warning("group_tell log failed: %s", e)
        return _format_group_tell(target, message, chat_id=chat_id, sender=sender)

    if re.search(r"@davos\s+help\b", lower, re.IGNORECASE):
        return _cmd_help(sender)

    if re.search(r"@davos\s+capabilit(?:y|ies)\b", lower, re.IGNORECASE):
        return _cmd_capabilities(sender)

    if re.fullmatch(r"models?(?:\s+.*)?", group_command, re.IGNORECASE):
        return _cmd_model(group_command, sender)

    if re.fullmatch(r"@davos\s+ping\s*", lower, re.IGNORECASE):
        return "pong — routing confirmed"

    if re.search(r"@davos\s+(?:persona(?:\s+list)?|personas|personalities)\s*$", lower, re.IGNORECASE):
        return _persona_status(chat_id)

    try:
        create_req = _parse_group_persona_create(lower)
    except ValueError as e:
        return str(e)
    if create_req:
        name, description = create_req
        try:
            token = create_group_persona(chat_id, name, description, sender)
        except ValueError as e:
            return str(e)
        set_persona(chat_id, token)
        clear_history(chat_id)
        display = group_persona_display_name(token) or name
        _log_group_persona_event(
            sender,
            "group_persona_created",
            {"chat_id": chat_id, "persona": display, "description_len": len(description)},
        )
        return f"Created {display} and switched this chat to it. It only exists in this group."

    editor = _parse_group_persona_editor_grant(lower)
    if editor:
        slug = _active_group_persona_slug(chat_id)
        if not slug:
            return "No group-specific persona is active here. Create one first with: @Davos create group persona Name: description"
        try:
            grant_group_persona_editor(chat_id, slug, editor)
        except ValueError as e:
            return str(e)
        persona = get_group_persona(chat_id, slug) or {}
        _log_group_persona_event(
            sender,
            "group_persona_editor_granted",
            {"chat_id": chat_id, "persona": persona.get("name", slug), "editor": normalize_handle(editor)},
        )
        return f"Done - {normalize_handle(editor)} can customize {persona.get('name', slug)} in this chat only."

    editor_reply = handle_group_persona_editor_command(sender, chat_id, lower)
    if editor_reply is not None:
        return editor_reply

    if re.search(r"@davos\s+on\b", lower, re.IGNORECASE):
        enable_gc(chat_id)
        contact_hint = MAC_MINI_APPLE_ID or "the bot Apple ID"
        return f"I'm on. If you can't see my messages, add {contact_hint} to your contacts."

    if re.search(r"@davos\s+off\b", lower, re.IGNORECASE):
        disable_gc(chat_id)
        return "Going quiet."

    m = re.search(r"@davos\s+allow(?:\s+(.*))?$", lower, re.IGNORECASE | re.DOTALL)
    if m:
        target = _parse_access_handle(m.group(1) or "")
        if target is None:
            return "Usage: @Davos allow [one complete phone number or email]"
        approve_user(target)
        return f"Done — {target} can now invoke me in any group."

    m = re.search(r"@davos\s+revoke(?:\s+(.*))?$", lower, re.IGNORECASE | re.DOTALL)
    if m:
        target = _parse_access_handle(m.group(1) or "")
        if target is None:
            return "Usage: @Davos revoke [one complete phone number or email]"
        revoke_user(target)
        return f"Done — {target} access revoked."

    if group_command in ("capability", "capabilities"):
        return _cmd_capabilities(sender)

    if _looks_like_confirmed_cleanup_run(group_command):
        return "Confirmed Codex cleanup runs from owner DM only. Text me `yes fix` there."

    if _looks_like_cleanup_prompt_confirmation(group_command):
        return _cmd_safe_cleanup(sender)

    if _looks_like_cleanup_status_request(group_command):
        return _cmd_cleanup_status(sender)

    if (
        group_command in ("ship safe cleanup", "ship cleanup", "ship greens", "triage", "triage log")
        or group_command.startswith("ship safe ")
    ):
        return _cmd_safe_cleanup(sender)

    log_update_alias = _rewrite_change_log_update_alias(group_command)
    if log_update_alias is not None:
        return _cmd_log(log_update_alias, sender)

    if group_command.startswith("log "):
        log_subcmd = group_command.split(None, 1)[1].strip()
        if log_subcmd in ("plan", "triage", "board", "safe cleanup", "ship safe cleanup"):
            return _cmd_log("log " + log_subcmd, sender)

    if _looks_like_self_repair_intake(group_command):
        return _cmd_self_repair_intake(group_command, sender)

    if _detect_persona_reset(group_command):
        set_persona(chat_id, None)
        clear_history(chat_id)
        return "Back to default personality."

    m = re.search(r"@davos\s+persona\s+(.+?)\s*$", lower, re.IGNORECASE)
    if m:
        name = m.group(1).lower().strip()
        if name in ("reset", "clear") or _is_default_persona_request(name):
            set_persona(chat_id, None)
            clear_history(chat_id)
            return "Back to default personality."
        group_slug = resolve_group_persona_slug(chat_id, name)
        if group_slug:
            token = group_persona_token(chat_id, group_slug)
            set_persona(chat_id, token)
            clear_history(chat_id)
            display = group_persona_display_name(token) or group_slug
            return f"Switched to {display}."
        resolved = resolve_persona_name(name, include_hidden=True)
        if not resolved:
            visible = list_personas()
            group_names = [persona.get("name", persona.get("slug", "")) for persona in list_group_personas(chat_id)]
            available = ", ".join(visible + group_names)
            return f"Unknown persona '{name}'. Available: {available}"
        set_persona(chat_id, resolved)
        clear_history(chat_id)
        return f"Switched to {resolved}."

    # Change-log: mirror DM intent classifier so '@davos log' shows the log
    # and '@davos log [text]' / '@davos add to log [text]' writes a new entry.
    # Routing this here avoids the LLM hallucinating "I can only log new entries"
    # and avoids transient agentic-loop failures on simple writes.
    log_view = re.fullmatch(r"@davos\s+log", lower.strip(), re.IGNORECASE)
    if log_view:
        return _cmd_log("log", sender)
    log_write = re.search(
        r"@davos\s+(?:add\s+to\s+log|log)\s+[\"“]?(.+?)[\"”]?\s*$",
        lower,
        re.IGNORECASE,
    )
    if log_write:
        entry = log_write.group(1).strip(" “”\"")
        if entry and entry.lower() not in ("log", "view", "show"):
            return _cmd_log(f"log {entry}", sender)

    return None
