import logging
import json
import os
import random
import re
import sqlite3
import time
import sys
import threading
import traceback
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from .runtime_locks import schedule_locked
from .config import POLL_INTERVAL, OWNER_ID, DB_PATH, BOT_DB_PATH, PROJECT_ROOT, GENERATED_DIR, IMAGE_OUTPUT_DIR, SLOW_MESSAGE_LOG_SECONDS, normalize_handle
from .imessage import send_message, send_file, is_owner_in_chat, find_recent_image_attachment
from .inbox import MessageInbox
from .inbox_workers import InboxWorkers, InboxWorkerError
from .commands import handle_command, handle_group_command, handle_group_persona_editor_command, _cron_scope_from_text, _looks_like_confirmed_cleanup_run
from .brain import (
    get_response, detect_capability_gap, log_missing_capability, log_error,
    check_rate_limit, cleanup_rate_limit_log,
    start_session, update_heartbeat, touch_session_heartbeat, log_session_error, log_startup_event,
    detect_help_intent, detect_user_fact, store_user_fact, classify_reminder_intent,
    classify_cron_list_intent,
    match_skill, detect_reminder_edit_intent, handle_reminder_edit,
    resolve_contact, check_ollama_recovery, initialize_ollama_recovery_state, start_ollama_keep_warm_thread,
)
from .permissions import check_admin_password, strip_password, redact_secret
from .db import cleanup_old_backups, run_migration
from .memory import init_db, save_turn, get_history, extract_and_update_memory, get_due_reminders, mark_reminder_sent, _bump_reminder_attempts, log_tool_use, get_tool_uses_today
from .personality import (
    build_system_prompt,
    build_light_chat_system_prompt,
    decatur_behavior_fast_reply,
    enforce_decatur_behavior_reply,
    load_soul,
    validate_personality_files,
)
from .soul import read_soul, restore_soul_from_latest_backup
from .permissions import is_owner, is_admin, can_user_do
from .alerts import send_owner_alert
from .package_delivery import start_package_delivery_monitor
from .work_bridge import start_work_bridge
# check_admin_password and strip_password imported above with brain imports
from .group_chat import is_group_chat, is_gc_enabled, is_approved_user, is_at_mentioned, strip_mention, normalize_group_mention_command, get_persona, audit_group_chats, normalize_approved_users
from .tools import (
    handle_private_send_confirmation,
    handle_private_send_request,
    redact_private_send_text_for_log,
    _cancel_cron_from_text,
    _describe_cron_from_text,
    _schedule_cron_from_text,
    _sports_recap_cron_from_text,
)
from .openai_images import (
    OPENAI_IMAGE_GENERATION_TOOL,
    OPENAI_IMAGE_SCAN_TOOL,
    choose_generation_provider,
    choose_scan_provider,
    estimate_generation_time,
    estimate_scan_time,
    generate_gemini_image,
    generate_image,
    generate_local_image,
    generate_nano_banana_image,
    generate_openai_image,
    image_provider_status,
    parse_openai_image_intent,
    scan_image,
    validate_image_path,
)
from .image_access import image_access_denial
from .text_safety import is_imessage_reaction
from .style_directives import format_style_directives_for_prompt, handle_style_directive_message, looks_like_tone_feedback
from .reminder_parser import parse_deterministic_reminder
from .ufc import UFC_FIGHT_CARD_TOOL, get_ufc_fight_card, is_ufc_fight_card_request
from .market import MARKET_DATA_TOOL, handle_market_query, start_market_tracker

from . import failure_copy as _failure_copy
from . import simple_chat as _simple_chat

SESSION_ID: int | None = None

_FRIEND_SEARCH_LIMIT = 5
_NON_OWNER_TEXT_CHAR_LIMIT = 4000
_PLAIN_CHAT_HISTORY_LIMIT = 2
_NO_WEB_INFORMATION_INSTRUCTION = (
    "For this request, do not search the web, fetch live web information, or use another tool "
    "to substitute for a web lookup. Authorized local and other requested actions remain available. "
    "Do not claim current scores, prices, weather, or other live facts without supplied evidence; "
    "explain when a current answer would require a lookup."
)


def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


_MESSAGE_TRACE_ENABLED = _env_bool("MESSAGE_TRACE_ENABLED", "true")
_MESSAGE_TRACE_MIN_SECONDS = _env_float("MESSAGE_TRACE_MIN_SECONDS", "0")


class _MessageTrace:
    def __init__(self, *, sender: str, chat_id: str, is_group: bool, text_len: int, has_image: bool):
        self.started = time.perf_counter()
        self.sender = sender
        self.chat_tail = (chat_id or "")[-6:]
        self.is_group = is_group
        self.text_len = text_len
        self.has_image = has_image
        self.route = "unknown"
        self.phases: dict[str, float] = defaultdict(float)
        self.flags: set[str] = set()
        self.prompt_chars = 0
        self.history_turns = 0

    def add(self, phase: str, elapsed: float) -> None:
        if phase:
            self.phases[phase] += max(0.0, elapsed)

    def flag(self, name: str) -> None:
        if name:
            self.flags.add(name)

    def set_route(self, route: str) -> None:
        if route:
            self.route = route

    def payload(self, elapsed: float) -> dict:
        return {
            "elapsed_seconds": round(elapsed, 4),
            "route": self.route,
            "is_group": self.is_group,
            "chat_tail": self.chat_tail,
            "text_len": self.text_len,
            "has_image": self.has_image,
            "prompt_chars": self.prompt_chars,
            "history_turns": self.history_turns,
            "flags": sorted(self.flags),
            "phases": {key: round(value, 4) for key, value in sorted(self.phases.items())},
        }


def _trace_call(trace_obj: _MessageTrace | None, phase: str, fn, *args, **kwargs):
    started = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        if trace_obj is not None:
            trace_obj.add(phase, time.perf_counter() - started)


def _log_message_trace(trace: _MessageTrace | None, elapsed: float) -> None:
    if trace is None or not _MESSAGE_TRACE_ENABLED or elapsed < _MESSAGE_TRACE_MIN_SECONDS:
        return
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (trace.sender, "message_trace", json.dumps(trace.payload(elapsed), sort_keys=True)),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("message trace log failed: %s", exc)


def _log_quality_signal(sender: str, signal: str, payload: dict | None = None) -> None:
    try:
        safe_payload = payload or {}
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (sender, "quality_signal", json.dumps({"signal": signal, **safe_payload}, sort_keys=True)),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("quality signal log failed for %s: %s", signal, exc)


_GROUP_ERROR_INTAKE_RE = re.compile(
    r"\b(?:log|record|capture|contextuali[sz]e|add\s+to\s+log)\b"
    r".{0,120}\b(?:error|bug|broke|broken|failed|failure|glitch|issue)\b"
    r"|\b(?:g\s+error|game\s+error|game[-\s]?score\s+error)\b"
    r"|\bcontextuali[sz]e\s+(?:the\s+)?(?:conversation|thread)\b",
    re.IGNORECASE | re.DOTALL,
)
_CHANGE_LOG_MAINTENANCE_RE = re.compile(
    r"^\s*(?:"
    r"log\s+(?:update|remove|done)\b"
    r"|(?:update|edit|revise)\s+logs?\s+#?\d+\b"
    r"|(?:delete|remove|dismiss|resolve|close|clear)\s+logs?\s+(?:#?\d+[\s,;]*)+\s*$"
    r")",
    re.IGNORECASE,
)
_OWNER_QUALITY_INTAKE_RE = re.compile(
    r"\b(?:dumb\s+brain|you\s+(?:got\s+)?confused|that\s+was\s+wrong|you\s+were\s+wrong|"
    r"bad\s+answer|bad\s+response|you\s+(?:can't|cannot)\s+handle\s+this|"
    r"you\s+missed\s+(?:the\s+)?(?:point|context)|you\s+made\s+that\s+up)\b",
    re.IGNORECASE,
)
_COMPLEX_ANALYSIS_RE = re.compile(
    r"\b(?:analy[sz]e|audit|review|summari[sz]e|forecast|model|compare|debug|fix|clean\s+up|"
    r"build|update|turn\s+this\s+into)\b"
    r".{0,160}\b(?:spreadsheet|sheet|excel|workbook|xlsx|csv|table|rows?|business|forecast|"
    r"revenue|budget|roi|unit\s+economics|deck|model)\b"
    r"|\b(?:spreadsheet|sheet|excel|workbook|xlsx|csv)\b.{0,160}\b(?:analy[sz]e|audit|review|"
    r"summari[sz]e|forecast|model|compare|debug|fix|clean\s+up|build|update)\b",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_ANALYZE_VERB_PATTERN = r"(?:analy[sz]e|anayl[sz]e|anal[sz]ye|anly[sz]e|analzye|anyl[sz]e|anali[sz]e)"
_SCREENSHOT_LOG_ACTION_PATTERN = r"(?:log|logg|lgo|record|capture)"
_SCREENSHOT_LOG_SCAN_PATTERN = rf"(?:{_IMAGE_ANALYZE_VERB_PATTERN}|scan|read|inspect|review|check|look\s+at)"
_SCREENSHOT_ISSUE_LOG_RE = re.compile(
    rf"\b{_SCREENSHOT_LOG_ACTION_PATTERN}\b.{{0,80}}\b(?:screenshot|image|photo|picture)\b"
    r".{0,80}\b(?:issue|bug|error|failure|glitch|wrong|bad|broken)\b"
    rf"|\b{_SCREENSHOT_LOG_ACTION_PATTERN}\b.{{0,80}}\b(?:what\s+went\s+wrong|expected\s+vs\s+actual)\b"
    rf"|\b{_SCREENSHOT_LOG_SCAN_PATTERN}\b.{{0,120}}\b(?:and\s+)?\b{_SCREENSHOT_LOG_ACTION_PATTERN}\b"
    rf"|\b{_SCREENSHOT_LOG_ACTION_PATTERN}\b.{{0,120}}\b{_SCREENSHOT_LOG_SCAN_PATTERN}\b",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_REPAIR_CONTEXT_RE = re.compile(
    rf"\b{_SCREENSHOT_LOG_ACTION_PATTERN}\b.{{0,100}}\b(?:this|that|it)\b.{{0,100}}\b(?:fix|repair|debug|ship|failed|failure|didn'?t\s+work|broken|bug)\b"
    r"|\b(?:fix|debug|repair|diagnose)\s+yourself\b"
    r"|\b(?:fix|repair|debug)\b.{0,100}\b(?:this|that|it)\b"
    r"|\b(?:this|that|it)\b.{0,80}\b(?:failed|broke|didn'?t\s+work|doesn'?t\s+work|is\s+broken)\b.{0,100}\b(?:fix|repair|debug|log|ship)\b"
    r"|\bship\b.{0,100}\b(?:this|that|it)\b.{0,100}\b(?:fix|repair|debug|failed|failure|broken|bug)\b",
    re.IGNORECASE | re.DOTALL,
)
_GUARDRAIL_BYPASS_REQUEST_RE = re.compile(
    r"\b(?:bypass|ignore|disable|drop|remove|turn\s+off)\b.{0,50}\b"
    r"(?:guardrails?|safety|filters?|polic(?:y|ies)|restrictions?)\b",
    re.IGNORECASE | re.DOTALL,
)
_HATEFUL_CONTENT_REQUEST_RE = re.compile(
    r"\b(?:give|write|generate|make|list|provide|show|send|tell\s+me|create|draft)\b"
    r".{0,80}\b(?:(?:racial|ethnic|protected[-\s]?class|anti-[a-z]+)\s+)?slurs?\b"
    r"|\b(?:write|generate|make|create|draft|give|provide)\b.{0,80}\b"
    r"(?:hateful?|racist|bigoted)\s+(?:comments?|posts?|messages?|insults?|content)\b",
    re.IGNORECASE | re.DOTALL,
)
_LAST_GENERATED_IMAGE_RE = re.compile(
    r"^\s*(?:(?:can|could|would)\s+(?:you|u)\s+)?"
    r"(?:show|send|resend|share|fetch|pull\s+up)\s+(?:me\s+)?"
    r"(?:the\s+)?(?:last\s+|latest\s+|recent\s+|generated\s+)?"
    r"(?:image|picture|photo|generation)\b"
    r"|^\s*(?:where\s+is|what\s+happened\s+to)\s+(?:the\s+)?"
    r"(?:last\s+|latest\s+|recent\s+|generated\s+)?(?:image|picture|photo)\b",
    re.IGNORECASE,
)
_IMAGE_QUEUE_STATUS_RE = re.compile(
    r"^\s*nano\s*banana\b.{0,80}\b(?:queue|status|history|list|where\s+is|what\s+happened)\b"
    r"|^\s*(?:what(?:'s|\s+is)\s+)?(?:in\s+)?(?:the\s+)?(?:image|generated\s+image)\s+(?:queue|status|history|list)\b"
    r"|^\s*(?:list|show)\s+(?:the\s+)?(?:image|generated\s+image)\s+(?:queue|history)\b"
    r"|^\s*how\s+many\s+(?:images?|pictures?|photos?|generated\s+images?)\s+(?:are\s+)?(?:queued|saved|recent|in\s+(?:the\s+)?queue)\b"
    r"|^\s*(?:queued\s+images?|image\s+queue|queue\s+images?|queue\s+image)\b"
    r"|^\s*(?:where\s+is|what\s+happened\s+to)\s+(?:my\s+|the\s+)?(?:generated\s+)?(?:image|picture|photo)\b"
    r"|^\s*(?:my\s+|the\s+)?(?:image|picture|photo|generation)\b.{0,80}\b(?:never|not|didn'?t|doesn'?t|failed|missing|isn'?t)\b.{0,80}\b(?:generated|generate|sent|send|come\s+through|show\s+up|arrive|there)\b"
    r"|^\s*(?:no|still\s+no)\s+(?:generated\s+)?(?:image|picture|photo)\b",
    re.IGNORECASE,
)
_IMAGE_QUEUE_SEND_RE = re.compile(
    r"^\s*(?:send|resend|share)\s+(?:me\s+)?(?:the\s+)?nano\s*banana\b.{0,80}\b(?:image|queue|history)\b"
    r"|^\s*nano\s*banana\b.{0,80}\b(?:send|resend|share)\b"
    r"|^\s*(?:send|resend|share)\s+(?:me\s+)?(?:the\s+)?(?:image|generated\s+image)\s+(?:queue|history)\b"
    r"|^\s*(?:send|resend|share)\s+(?:all\s+)?(?:queued|recent)\s+(?:images?|pictures?|photos?)\b"
    r"|^\s*(?:send|resend|share)\s+(?:me\s+)?(?:the\s+)?(?:queued\s+)?(?:image|picture|photo)\b",
    re.IGNORECASE,
)
_NANO_BANANA_RE = re.compile(r"\bnano\s*banana\b", re.IGNORECASE)
_GEMINI_IMAGE_PROVIDER_HINT_RE = re.compile(
    r"\b(?:using|via|with|through|on)\s+(?:google\s+)?gemini\b"
    r"|^\s*@?\s*davos(?:bot)?\b[\s,;:.-]*(?:google\s+)?gemini\b.{0,100}\b"
    r"(?:image\s*(?:gen|generate)|generate|create|make|draw|render)\b"
    r"|^\s*(?:google\s+)?gemini\b.{0,100}\b"
    r"(?:image\s*(?:gen|generate)|generate|create|make|draw|render)\b",
    re.IGNORECASE | re.DOTALL,
)
_GENERATED_IMAGE_CACHE: dict[str, dict[str, object]] = {}
_ACTIVE_IMAGE_JOBS: dict[str, dict[str, object]] = {}
_IMAGE_JOB_LOCK = threading.RLock()
_GENERATED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_GENERATED_IMAGE_QUEUE_LIMIT = 5

_NO_SEARCH_RE = re.compile(
    r"\b(no|skip|without)\s+(web\s+)?search\b"
    r"|\bdon'?t\s+(web\s+)?search\b"
    r"|\bno\s+web\b",
    re.IGNORECASE,
)


def _strip_no_search(text: str) -> tuple[str, bool]:
    if _NO_SEARCH_RE.search(text):
        # strip() takes a character set, not words. Trimming "pls please"
        # here used to turn names such as Naples into N and damage filenames.
        return _NO_SEARCH_RE.sub("", text).strip(), True
    return text, False


def _non_owner_length_rejection(sender: str, text: str) -> str | None:
    """Block giant non-owner messages before they reach any LLM/backend."""
    if is_owner(sender):
        return None
    length = len(text or "")
    if length <= _NON_OWNER_TEXT_CHAR_LIMIT:
        return None
    return (
        f"That message is too long for non-owner access ({length}/{_NON_OWNER_TEXT_CHAR_LIMIT} chars). "
        "Send the short version or have the owner run the big job."
    )


def _is_group_error_intake(text: str) -> bool:
    return bool(_GROUP_ERROR_INTAKE_RE.search(text or ""))


def _is_simple_group_chatter(text: str) -> bool:
    simple = re.sub(r"[^a-z0-9]+", "", (text or "").strip().lower())
    if 0 < len(simple) <= 2:
        return True
    if _should_keep_roast_chat_only(text):
        return True
    return bool(re.fullmatch(r"(?:repeat\s+after\s+me|say)\s+['\"]?[a-z0-9]{1,2}['\"]?", (text or "").strip(), re.IGNORECASE))


_ROAST_REQUEST_RE = re.compile(
    r"\b(?:roast|cook|flame|drag|clown|shit\s*talk|talk\s+shit|make\s+fun\s+of)\b",
    re.IGNORECASE,
)
_ROAST_SEARCH_RE = re.compile(
    r"\b(?:search|look\s*up|google|web|latest|current|today|tonight|score|scores|news|odds|weather)\b",
    re.IGNORECASE,
)
_FOOD_ROAST_RE = re.compile(
    r"\broast(?:ing|ed)?\s+(?:chicken|turkey|beef|pork|vegetables?|potatoes?)\b",
    re.IGNORECASE,
)
_LIVE_INFO_TOOL_RE = re.compile(
    r"\b(?:web\s+search|search|look\s*up|google|latest|current|news|odds|"
    r"scores?|standings?|weather|forecast|temperature|ufc|fight\s+card|"
    r"stocks?|shares?|market|ticker|quote|premarket|pre-market|after\s*hours|"
    r"postmarket|post-market|earnings?|mag\s*7|nasdaq|ixic|qqq|s\s*&\s*p|spx|sp500|spy|"
    r"aapl|msft|nvda|amzn|googl?|meta|tsla)\b"
    r"|\bwho\s+(?:won|plays|is\s+playing)\b"
    r"|\bwhat(?:'s|\s+is)\s+(?:the\s+)?score\b"
    r"|\b(?:today|tonight)(?:'s)?\s+games?\b",
    re.IGNORECASE,
)
_OWNER_SIDE_EFFECT_TOOL_RE = re.compile(
    r"\b(?:remind|reminder|cron|crons|recurring|schedule|scheduled|daily|weekly|"
    r"every\s+(?:morning|day|week|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    r"|\b(?:workout|bench|squat|deadlift|lift|sets?|reps?|lbs?|pounds?|pr)\b"
    r"|\b(?:bet|bets|odds|stake|units?|p&l|parlay|spread|moneyline)\b"
    r"|\b(?:read|write|edit|create|generate|scan|analy[sz]e)\b.{0,60}\b(?:file|csv|spreadsheet|sheet|workbook|md|markdown|code|script|repo)\b"
    r"|\b(?:read|check|tail)\b.{0,60}\b(?:logs?|pm2)\b"
    r"|\b(?:run|execute|restart|deploy|pull|push)\b.{0,60}\b(?:command|script|service|pm2|git|repo|bot)\b"
    r"|\b(?:list|show|clear|delete)\b.{0,60}\b(?:chats?|history|memory|logs?|backups?)\b"
    r"|\b(?:persona|personality|skill|alert|billing|api\s+status|model\s+status)\b",
    re.IGNORECASE | re.DOTALL,
)
_SHORT_CHAT_ONLY_RE = _simple_chat.SHORT_CHAT_ONLY_RE
_PRIDE_HORNY_BANTER_RE = re.compile(
    r"\b(?:computa\s+)?make\b.{0,50}\b(?:these|those|the)\s+"
    r"(?:guys|boys|dudes|people|mfs|motherfuckers)\b.{0,70}\b(?:super\s+)?gay\b.{0,50}\bhorny\b"
    r"|\b(?:computa\s+)?make\b.{0,50}\bhorny\b.{0,70}\b(?:super\s+)?gay\b",
    re.IGNORECASE | re.DOTALL,
)
_VIRAL_MEME_BANTER_RE = re.compile(
    r"\b(?:meme|memes|viral|brainrot|skibidi|gyatt|rizz|sigma|fanum\s+tax|"
    r"hawk\s+tuah|demure|aura|npc|delulu|crash\s*out|locked\s+in|"
    r"let\s+him\s+cook|cooked|chopped|ratio|touch\s+grass|tiktok)\b",
    re.IGNORECASE,
)
_HUMOR_BANTER_RE = re.compile(
    r"\b(?:make|say|reply|respond|caption|joke|funny|roast|cook|bit|"
    r"hit\s+(?:them|him|her|us)\s+with)\b",
    re.IGNORECASE,
)
_VIRAL_BANTER_REPLIES = (
    "Brainrot acknowledged. I ran it through the Department of Terrible Ideas and, regrettably, it cleared review.",
    "This has powerful 'group chat discovered a TikTok audio and now nobody is safe' energy.",
    "I can smell the For You page from here. Proceed, but with shame.",
)


def _is_roast_request(text: str) -> bool:
    return bool(_ROAST_REQUEST_RE.search(text or "")) and not bool(_FOOD_ROAST_RE.search(text or ""))


def _should_keep_roast_chat_only(text: str) -> bool:
    return _is_roast_request(text) and not bool(_ROAST_SEARCH_RE.search(text or ""))


def _looks_like_plain_chat(text: str) -> bool:
    return _simple_chat.looks_like_plain_chat(
        text,
        live_info_re=_LIVE_INFO_TOOL_RE,
        side_effect_re=_OWNER_SIDE_EFFECT_TOOL_RE,
    )


def _fast_chat_reply(text: str) -> str | None:
    return _simple_chat.fast_chat_reply(text)


def _market_fast_reply(sender: str, text: str, *, skip_web_search: bool = False) -> str | None:
    reply = handle_market_query(text, **({"allow_live_lookup": False} if skip_web_search else {}))
    if reply is None:
        return None
    if not is_owner(sender) and not is_admin(sender):
        if get_tool_uses_today(sender, MARKET_DATA_TOOL) >= _FRIEND_SEARCH_LIMIT:
            return "Market lookup daily limit reached. Ask the owner if this is urgent."
        log_tool_use(sender, MARKET_DATA_TOOL)
    return reply


def _should_use_limited_web_tools(text: str, skip_search: bool) -> bool:
    if skip_search or _should_keep_roast_chat_only(text):
        return False
    return bool(_LIVE_INFO_TOOL_RE.search(text or ""))


def _should_use_owner_tools(text: str, skip_search: bool) -> bool:
    if _should_keep_roast_chat_only(text) or _looks_like_plain_chat(text):
        return False
    return bool(_OWNER_SIDE_EFFECT_TOOL_RE.search(text or "")
                or (not skip_search and _LIVE_INFO_TOOL_RE.search(text or "")))


def _owner_tools_without_web() -> list[str]:
    from .tools import TOOL_DEFINITIONS
    return [tool["name"] for tool in TOOL_DEFINITIONS if tool["name"] not in {"web_search", "get_weather"}]


def _style_feedback_should_yield_to_task_intent(text: str) -> bool:
    """Keep tone feedback from stealing reminder/cron/tool/status requests."""
    raw = text or ""
    return bool(
        _LIVE_INFO_TOOL_RE.search(raw)
        or re.search(
            r"\b(?:remind|reminder|cron|crons|recurring|schedule|scheduled|weather|forecast|"
            r"search|look\s*up|google|latest|news|scores?|standings?|ufc|fight\s+card|"
            r"stocks?|market|ticker|quote|premarket|after\s*hours|earnings?|mag\s*7|nasdaq|s\s*&\s*p|"
            r"model\s+(?:status|options|routing|request|intensity)|what\s+model|which\s+model|"
            r"log\s+this|bug\s+log|priority|read|write|edit|create|generate|scan|analy[sz]e|"
            r"file|logs?|pm2|run|execute|restart|deploy|pull|push|billing|api\s+status)\b",
            raw,
            re.IGNORECASE,
        )
        or classify_reminder_intent(raw) != "none"
        or classify_cron_list_intent(raw)
        or detect_reminder_edit_intent(raw)
    )


def _owner_group_should_use_tools(text: str, skip_search: bool) -> bool:
    return _should_use_owner_tools(text, skip_search)


def _stable_reply_choice(text: str, replies: tuple[str, ...]) -> str:
    return _simple_chat.stable_reply_choice(text, replies)


def _viral_banter_reply(text: str, has_image: bool = False) -> str | None:
    """Keep the specific local bit; creative requests need the model and history."""
    s = (text or "").strip()
    if not s:
        return None
    if _PRIDE_HORNY_BANTER_RE.search(s):
        return (
            "Computa refuses to install the Horny DLC. Best I can do is rainbow confidence, "
            "terrible posture, and one group-chat intervention."
        )
    if has_image:
        return None
    if _LIVE_INFO_TOOL_RE.search(s) or _OWNER_SIDE_EFFECT_TOOL_RE.search(s):
        return None
    return None


def _log_group_error_intake_if_needed(sender: str, chat_id: str, text: str) -> str | None:
    """Owner-only deterministic intake for group error/context reports."""
    if not is_owner(sender) or not _is_group_error_intake(text):
        return None
    if _CHANGE_LOG_MAINTENANCE_RE.match(text or ""):
        return None
    summary = redact_secret(re.sub(r"\s+", " ", (text or "").strip()))[:240]
    tags = []
    lower = (text or "").lower()
    if "game" in lower:
        tags.append("game")
    if "context" in lower:
        tags.append("context")
    if re.search(r"\bg\s+error\b", lower):
        tags.append("g_error")
    request = f"[GROUP-ERROR YELLOW] {summary}"
    reason = json.dumps({
        "source": "group_error_intake",
        "review_only": True,
        "chat_id_tail": (chat_id or "")[-6:],
        "message_len": len(text or ""),
        "tags": tags,
    }, sort_keys=True)
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
        f"Logged group error intake #{row_id} [YELLOW].\n"
        "Review-only: no code changed, no deploy run.\n"
        "Text 'ship safe cleanup' for the Codex handoff."
    )


def _is_owner_quality_intake(text: str) -> bool:
    return bool(_OWNER_QUALITY_INTAKE_RE.search(text or ""))


def _log_owner_quality_intake_if_needed(sender: str, text: str) -> str | None:
    """Owner-only deterministic intake for bot-quality complaints."""
    if not is_owner(sender) or not _is_owner_quality_intake(text):
        return None
    summary = redact_secret(re.sub(r"\s+", " ", (text or "").strip()))[:240]
    request = f"[BOT-QUALITY YELLOW] {summary}"
    reason = json.dumps({
        "source": "owner_quality_intake",
        "review_only": True,
        "message_len": len(text or ""),
    }, sort_keys=True)
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
        f"Logged bot-quality intake #{row_id} [YELLOW].\n"
        "Review-only: no code changed, no deploy run.\n"
        "Text 'ship safe cleanup' for the Codex handoff."
    )


def _complex_analysis_preflight_reply(text: str, has_context: bool = False, history: list[dict] | None = None) -> str | None:
    if has_context or not _COMPLEX_ANALYSIS_RE.search(text or ""):
        return None
    # Planning a budget does not require an existing workbook. Only intercept
    # an otherwise context-free request to inspect a particular artifact.
    if not re.search(r"\b(?:this|that|the|my|our|attached)\s+(?:spreadsheet|sheet|excel|workbook|xlsx|csv|table|deck)\b", text, re.I):
        return None
    if any(turn.get("role") == "user" and str(turn.get("content", "")).strip() for turn in history or []):
        return None
    if "\n" in text or re.search(r"[:=]\s*-?\$?\d|\S+\.(?:csv|xlsx?|tsv)\b", text, re.I):
        return None
    return (
        "I need the actual file, screenshot, pasted rows, or concrete context before I analyze that. "
        "Send the sheet/context here, or use `big change [ask]` if this should become a Codex handoff."
    )


def _image_route_key_from_text(text: str) -> str:
    return "nano_banana" if _NANO_BANANA_RE.search(text or "") else ""


def _image_provider_override_from_text(text: str) -> str:
    if _GEMINI_IMAGE_PROVIDER_HINT_RE.search(text or ""):
        return "gemini"
    return ""


def _image_context_key(sender: str, recipient: str, is_group: bool = False, route_key: str = "") -> str:
    base = f"group:{recipient}" if is_group else f"dm:{sender}"
    clean_route = re.sub(r"[^a-z0-9_]+", "_", (route_key or "").strip().lower()).strip("_")
    return f"{base}:{clean_route}" if clean_route else base


def _project_relative_path(path: str) -> str:
    try:
        return os.path.relpath(path, PROJECT_ROOT).replace("\\", "/")
    except ValueError:
        return os.path.basename(path)


def _generated_image_dirs() -> list[Path]:
    candidates = [
        Path(IMAGE_OUTPUT_DIR),
        Path(GENERATED_DIR) / "images",
        Path(GENERATED_DIR) / "openai_images",
    ]
    dirs: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else Path(PROJECT_ROOT) / candidate
        key = str(path.resolve()) if path.exists() else str(path.absolute())
        if key not in seen:
            dirs.append(path)
            seen.add(key)
    return dirs


def _image_path_matches_route(path: Path, route_key: str = "") -> bool:
    if route_key == "nano_banana":
        return path.name.startswith("nano_banana_")
    return not path.name.startswith("nano_banana_")


def _latest_generated_image_path(route_key: str = "") -> str | None:
    latest_path: Path | None = None
    latest_mtime = -1.0
    for directory in _generated_image_dirs():
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in _GENERATED_IMAGE_EXTENSIONS:
                continue
            if not _image_path_matches_route(path, route_key):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_path = path
                latest_mtime = mtime
    return str(latest_path) if latest_path else None


def _recent_generated_image_paths(limit: int = _GENERATED_IMAGE_QUEUE_LIMIT, route_key: str = "") -> list[str]:
    candidates: list[tuple[float, str]] = []
    for directory in _generated_image_dirs():
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in _GENERATED_IMAGE_EXTENSIONS:
                continue
            if not _image_path_matches_route(path, route_key):
                continue
            try:
                candidates.append((path.stat().st_mtime, str(path)))
            except OSError:
                continue
    candidates.sort(reverse=True)
    return [path for _mtime, path in candidates[:limit]]


def _remember_generated_image(
    sender: str,
    recipient: str,
    is_group: bool,
    path: str,
    provider: str = "",
    route_key: str = "",
) -> None:
    key = _image_context_key(sender, recipient, is_group, route_key)
    existing = _GENERATED_IMAGE_CACHE.get(key, {})
    items = list(existing.get("items", [])) if isinstance(existing, dict) and isinstance(existing.get("items"), list) else []
    items = [item for item in items if isinstance(item, dict) and item.get("path") != path]
    items.insert(0, {"path": path, "provider": provider, "ts": time.time()})
    _GENERATED_IMAGE_CACHE[key] = {
        "path": path,
        "provider": provider,
        "ts": time.time(),
        "items": items[:_GENERATED_IMAGE_QUEUE_LIMIT],
    }


def _active_image_jobs_for_context(
    sender: str,
    recipient: str,
    is_group: bool = False,
    route_key: str = "",
) -> list[dict[str, object]]:
    key = _image_context_key(sender, recipient, is_group, route_key)
    with _IMAGE_JOB_LOCK:
        job = _ACTIVE_IMAGE_JOBS.get(key)
        return [dict(job)] if isinstance(job, dict) else []


def _generated_image_queue_items(
    sender: str,
    recipient: str,
    is_group: bool = False,
    route_key: str = "",
) -> list[dict[str, object]]:
    cached = _GENERATED_IMAGE_CACHE.get(_image_context_key(sender, recipient, is_group, route_key), {})
    if isinstance(cached, dict) and isinstance(cached.get("items"), list):
        items = [
            item
            for item in cached["items"]
            if isinstance(item, dict) and isinstance(item.get("path"), str) and Path(str(item["path"])).exists()
        ]
        if items:
            return items
    if is_owner(sender):
        return [{"path": path, "provider": "", "ts": 0.0} for path in _recent_generated_image_paths(route_key=route_key)]
    return []


def _format_image_job_line(job: dict[str, object]) -> str:
    started = float(job.get("started_ts") or time.time())
    elapsed = max(0, int(time.time() - started))
    return f"1 active image job; elapsed {elapsed}s. I'll send it here when it's ready."


def _handle_generated_image_queue_request(
    sender: str,
    text: str,
    recipient: str,
    is_group: bool = False,
) -> str | None:
    if not (_IMAGE_QUEUE_STATUS_RE.search(text or "") or _IMAGE_QUEUE_SEND_RE.search(text or "")):
        return None
    if not (is_owner(sender) or is_admin(sender) or is_approved_user(sender)):
        return "Image scan/generation is for approved users only."

    route_key = _image_route_key_from_text(text or "")
    route_label = "nano banana " if route_key == "nano_banana" else ""
    active_jobs = _active_image_jobs_for_context(sender, recipient, is_group=is_group, route_key=route_key)
    items = _generated_image_queue_items(sender, recipient, is_group=is_group, route_key=route_key)
    if not items and not active_jobs:
        return f"No recent {route_label}generated images saved for this chat."

    if _IMAGE_QUEUE_SEND_RE.search(text or ""):
        if not items:
            return "An image is still generating. I'll send it here when it's ready."
        sent = 0
        failed = 0
        for item in items[:_GENERATED_IMAGE_QUEUE_LIMIT]:
            path = str(item.get("path") or "")
            if path and send_file(recipient, path, is_group=is_group):
                sent += 1
            else:
                failed += 1
        if sent and not failed:
            return f"Sent {sent} recent generated image(s)."
        if sent:
            return f"Sent {sent} recent generated image(s); {failed} failed to send."
        return "I found recent generated images, but iMessage file send failed."

    lines = []
    if active_jobs:
        lines.append(_format_image_job_line(active_jobs[0]))
    if items:
        lines.append(f"Recent {route_label}generated images ({len(items[:_GENERATED_IMAGE_QUEUE_LIMIT])}):")
    for idx, item in enumerate(items[:_GENERATED_IMAGE_QUEUE_LIMIT], start=1):
        rel_path = _project_relative_path(str(item.get("path") or ""))
        lines.append(f"{idx}. `{rel_path}`")
    if items:
        lines.append("Say `send image queue` to resend them here.")
    return "\n".join(lines)


def _handle_last_generated_image_request(
    sender: str,
    text: str,
    recipient: str,
    is_group: bool = False,
) -> str | None:
    if not _LAST_GENERATED_IMAGE_RE.search(text or ""):
        return None
    if not (is_owner(sender) or is_admin(sender) or is_approved_user(sender)):
        return "Image scan/generation is for approved users only."

    route_key = _image_route_key_from_text(text or "")
    cached = _GENERATED_IMAGE_CACHE.get(_image_context_key(sender, recipient, is_group, route_key), {})
    path = cached.get("path") if isinstance(cached, dict) else None
    if not isinstance(path, str) or not Path(path).exists():
        path = _latest_generated_image_path(route_key=route_key) if is_owner(sender) else None
    if not path or not Path(path).exists():
        return "I don't have a generated image saved for this chat yet."

    sent = send_file(recipient, path, is_group=is_group)
    rel_path = _project_relative_path(path)
    if sent:
        return "Sent the last generated image."
    return f"I found the last generated image under `{rel_path}`, but iMessage file send failed."


def _compose_reference_generation_prompt(prompt: str, image_path: str | None) -> str | None:
    if not image_path:
        return prompt
    scan_prompt = (
        "Describe this reference image for an image generation brief. "
        "Focus on visible subjects, composition, style, colors, text, and anything the new image should preserve. "
        "Do not mention private metadata."
    )
    result = scan_image(image_path, scan_prompt)
    if not result.ok:
        logger.warning("Reference image scan failed before generation via %s: %s", result.provider or "unknown", redact_secret(result.message))
        return None
    reference = re.sub(r"\s+", " ", result.message).strip()[:1200]
    return (
        f"{prompt}\n\nReference image context to incorporate, reinterpret, or use as visual direction: {reference}"
    )


def _finish_image_job(key: str, job_id: str) -> None:
    with _IMAGE_JOB_LOCK:
        current = _ACTIVE_IMAGE_JOBS.get(key)
        if isinstance(current, dict) and current.get("job_id") == job_id:
            _ACTIVE_IMAGE_JOBS.pop(key, None)


def _run_image_generation_job(job: dict[str, object]) -> None:
    key = str(job["key"])
    job_id = str(job["job_id"])
    sender = str(job["sender"])
    recipient = str(job["recipient"])
    is_group = bool(job.get("is_group"))
    prompt = str(job["prompt"])
    image_path = job.get("image_path")
    image_path_str = str(image_path) if isinstance(image_path, str) and image_path else None
    route_key = str(job.get("route_key") or "")
    provider_override = str(job.get("provider_override") or "")
    tracking = job.get("work_tracking")
    sending = False

    try:
        if tracking is not None:
            tracking.mark("generating")
        generation_prompt = _compose_reference_generation_prompt(prompt, image_path_str)
        if generation_prompt is None:
            if tracking is not None:
                tracking.mark("failed", reason="generation_failed")
            send_message(recipient, "I can't do that right now.", is_group=is_group)
            return

        if route_key == "nano_banana":
            result = generate_nano_banana_image(generation_prompt)
        elif provider_override == "gemini":
            result = generate_gemini_image(generation_prompt)
        elif provider_override == "local":
            result = generate_local_image(generation_prompt)
        elif provider_override == "openai":
            result = generate_openai_image(generation_prompt)
        else:
            result = generate_image(generation_prompt)
        if result.api_called and is_owner(sender):
            log_tool_use(sender, OPENAI_IMAGE_GENERATION_TOOL)
        if not result.ok or not result.path:
            if tracking is not None:
                tracking.mark("failed", reason="generation_failed")
            logger.warning("Image generation failed for %s via %s: %s", sender, result.provider or "unknown", redact_secret(result.message))
            send_message(recipient, "I can't do that right now.", is_group=is_group)
            return

        if tracking is not None:
            tracking.mark("sending", provider=result.provider)
        sending = True
        sent = send_file(recipient, result.path, is_group=is_group,
                         **({"recovery_mode": "none"} if tracking is not None else {}))
        if tracking is not None:
            tracking.mark("sent" if sent is True else "unknown", reason="" if sent is True else "send_unverified")
        _remember_generated_image(sender, recipient, is_group, result.path, result.provider, route_key=route_key)
        if not sent:
            message = ("I made the image, but couldn't verify the iMessage send." if tracking is not None
                       else "I made the image, but iMessage file send failed.")
            send_message(recipient, message, is_group=is_group)
    except Exception as exc:
        if tracking is not None:
            try:
                tracking.mark("unknown", reason="execution_interrupted")
            except Exception:
                logger.warning("Image receipt outcome could not be saved")
        logger.exception("Background image generation failed")
        if tracking is None or not sending:
            send_message(recipient, "I can't do that right now.", is_group=is_group)
    finally:
        if tracking is not None:
            tracking.close()
        _finish_image_job(key, job_id)


def _start_image_generation_job(
    sender: str,
    prompt: str,
    image_path: str | None,
    recipient: str,
    is_group: bool = False,
    route_key: str = "",
    provider_override: str = "",
    tracking=None,
) -> str:
    provider = provider_override or choose_generation_provider()
    if provider == "disabled":
        return "I can't do that right now."

    key = _image_context_key(sender, recipient, is_group, route_key)
    with _IMAGE_JOB_LOCK:
        current = _ACTIVE_IMAGE_JOBS.get(key)
        if isinstance(current, dict):
            return _format_image_job_line(current)

        job_id = f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        job = {
            "job_id": job_id,
            "key": key,
            "sender": sender,
            "recipient": recipient,
            "is_group": is_group,
            "prompt": prompt,
            "image_path": image_path,
            "provider": provider,
            "provider_override": provider_override,
            "route_key": route_key,
            "started_ts": time.time(),
        }
        if tracking is not None:
            tracking.prepare(job_id, sender, recipient, is_group, provider)
            job["work_tracking"] = tracking
        _ACTIVE_IMAGE_JOBS[key] = job

    thread = None
    try:
        thread = threading.Thread(
            target=_run_image_generation_job,
            args=(job,),
            name=f"davosbot-image-{job_id}",
            daemon=True,
        )
        thread.start()
    except Exception:
        if thread is None or not thread.is_alive():
            _finish_image_job(key, job_id)
            if tracking is not None:
                try:
                    tracking.mark("failed", reason="start_failed")
                finally:
                    tracking.close()
        raise
    return f"On it, generating image. Estimate: {estimate_generation_time(provider)}."


def _handle_openai_image_intent(
    sender: str,
    text: str,
    image_path: str | None,
    recipient: str,
    is_group: bool = False,
    allow_caption: bool = False,
    tracking=None,
) -> str | None:
    """Deterministic image route before generic LLM handling."""
    from . import image_conversation

    queue_reply = _handle_generated_image_queue_request(sender, text or "", recipient, is_group=is_group)
    if queue_reply is not None:
        return queue_reply

    last_image_reply = _handle_last_generated_image_request(sender, text or "", recipient, is_group=is_group)
    if last_image_reply is not None:
        return last_image_reply

    context = None
    if image_conversation.is_image_followup(text):
        context = image_conversation.get(sender, recipient)
        if context and (not image_path or image_path == context.path):
            image_path = context.path
        else:
            context = None
    elif image_path:
        # A new attachment supersedes any previously scanned image in this conversation.
        image_conversation.forget(sender, recipient)
    intent = parse_openai_image_intent(text or "", has_image=bool(image_path))
    if intent is None and image_path and (allow_caption or context):
        intent = parse_openai_image_intent(f"scan image {text or image_conversation.IMAGE_ONLY_ASK}", has_image=True)
    if intent is None:
        return None
    if not (is_owner(sender) or is_admin(sender) or is_approved_user(sender)):
        return "Image scan/generation is for approved users only."
    denial = image_access_denial(sender)
    if denial:
        return denial

    if intent.kind == "scan":
        if not image_path:
            recovered_image = find_recent_image_attachment(recipient, sender=sender)
            if recovered_image:
                ok, _reason, _mime = validate_image_path(recovered_image)
                if ok:
                    image_path = recovered_image
                    logger.info(
                        "Recovered recent image attachment for %s scan request",
                        "group" if is_group else "dm",
                    )
        if not image_path:
            return _failure_copy.IMAGE_SCAN_MISSING_REPLY
        ok, reason, _mime = validate_image_path(image_path)
        if not ok:
            image_conversation.forget(sender, recipient)
            logger.warning("Image scan attachment validation failed for %s: %s", sender, reason)
            return (
                f"I found the image request, but the attachment is not readable yet: {reason}. "
                "Re-send the screenshot/image, then ask `what's in this?`."
            )
        provider = choose_scan_provider()
        if provider != "disabled":
            send_message(
                recipient,
                f"On it, reading image. Estimate: {estimate_scan_time(provider)}.",
                is_group=is_group,
            )
        if not is_owner(sender):
            log_tool_use(sender, OPENAI_IMAGE_SCAN_TOOL)
        prompt = image_conversation.followup_prompt(context, f"{text}\n{intent.prompt}") if context else intent.prompt
        result = scan_image(image_path, prompt)
        if result.api_called and is_owner(sender):
            log_tool_use(sender, OPENAI_IMAGE_SCAN_TOOL)
        if not result.ok:
            logger.warning("Image scan failed for %s via %s: %s", sender, result.provider or "unknown", redact_secret(result.message))
            return _failure_copy.image_scan_failure_reply(result.message)
        image_conversation.remember(sender, recipient, image_path, text, result.message)
        return _failure_copy.image_scan_success_reply(result.message)

    if intent.kind == "generate":
        if not is_owner(sender):
            log_tool_use(sender, OPENAI_IMAGE_GENERATION_TOOL)
        route_key = _image_route_key_from_text(text or "")
        provider_override = _image_provider_override_from_text(text or "")
        if route_key == "nano_banana":
            return _start_image_generation_job(
                sender,
                intent.prompt,
                image_path,
                recipient,
                is_group=is_group,
                route_key=route_key,
                provider_override="gemini",
                **({"tracking": tracking} if tracking is not None else {}),
            )
        return _start_image_generation_job(
            sender,
            intent.prompt,
            image_path,
            recipient,
            is_group=is_group,
            provider_override=provider_override,
            **({"tracking": tracking} if tracking is not None else {}),
        )

    return None


def _handle_image_caption(sender: str, text: str, image_path: str | None, recipient: str, is_group: bool = False) -> bool:
    """Route remaining captions through the same access, quota and provider checks."""
    if not image_path:
        return False
    reply = _handle_openai_image_intent(sender, text, image_path, recipient, is_group=is_group, allow_caption=True)
    if reply is None:
        return False
    _image_buffer.pop(_buf_key(recipient, sender), None)
    send_message(recipient, reply, is_group=is_group)
    user_text = f"[image] {text}"
    save_turn(recipient, "user", f"{sender}: {redact_secret(user_text)}" if is_group else redact_secret(user_text))
    save_turn(recipient, "assistant", reply)
    return True


_IMAGE_CAPABILITY_STATUS_RE = re.compile(
    r"^\s*(?:image|images|vision|gpt\s+scan|image\s+gen(?:eration)?)\s+"
    r"(?:routing|route|status|provider|providers|model|models|config|configuration)\b"
    r"|^\s*(?:what|which|show|tell\s+me|status)\b.{0,80}\b(?:image|vision|scan|generation)\b"
    r".{0,80}\b(?:model|provider|route|routing|configured|config|status)\b"
    r"|\b(?:image|vision|scan|generation)\s+(?:model|provider|route|routing|config|status)\b",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_SCAN_CAPABILITY_RE = re.compile(
    r"\b(?:can|could|do|does|will)\b.{0,60}\b(?:you|davos|davosbot)\b"
    r".{0,100}\b(?:scan|read|analy[sz]e|describe|inspect|look\s+at|view)\b"
    r".{0,100}\b(?:images?|photos?|pictures?|screenshots?|attachments?)\b"
    r"|\b(?:images?|photos?|pictures?|screenshots?|attachments?)\s+scans?\b"
    r".{0,100}\b(?:work|works|available|supported|capab|gcs?|group\s+chats?|groups?)\b"
    r"|\b(?:scan|read|analy[sz]e|describe|inspect|look\s+at|view)\b"
    r".{0,100}\b(?:images?|photos?|pictures?|screenshots?|attachments?)\b"
    r".{0,100}\b(?:work|works|available|supported|capab|gcs?|group\s+chats?|groups?)\b",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_GENERATION_CAPABILITY_RE = re.compile(
    r"\b(?:can|could|do|does|will)\b.{0,60}\b(?:you|davos|davosbot)\b"
    r".{0,100}\b(?:generate|create|make|draw|render)\b"
    r".{0,100}\b(?:images?|photos?|pictures?|graphics?|logos?|art|memes?)\b"
    r"|\b(?:image|photo|picture|logo|art|meme)\s+gen(?:eration)?\b"
    r".{0,100}\b(?:work|works|available|supported|capab|enabled|configured)\b",
    re.IGNORECASE | re.DOTALL,
)


def _handle_image_capability_status(sender: str, text: str) -> str | None:
    if re.match(r"^\s*(?:log|changelog|ship\s+safe|big\s+change|codex\s+plan|intake)\b", text or "", re.IGNORECASE):
        return None
    if re.search(r"\b(?:generate|create|make|draw|render)\b", text or "", re.IGNORECASE) and re.search(
        r"\b(?:like|based\s+on|from)\s+this\b",
        text or "",
        re.IGNORECASE,
    ):
        return None
    wants_status = _IMAGE_CAPABILITY_STATUS_RE.search(text or "")
    wants_scan_capability = _IMAGE_SCAN_CAPABILITY_RE.search(text or "")
    wants_generation_capability = _IMAGE_GENERATION_CAPABILITY_RE.search(text or "")
    if not wants_status and not wants_scan_capability and not wants_generation_capability:
        return None
    if not (is_owner(sender) or is_admin(sender) or is_approved_user(sender)):
        return "Image scan/generation is for approved users only."
    if wants_generation_capability and not wants_status:
        provider = choose_generation_provider()
        if provider == "disabled":
            return "I can generate images when a generation provider is configured, but image generation is disabled right now."
        return (
            "Yes. I can generate images in DMs and enabled group chats. "
            "Ask directly, like `image gen a DavosBot logo` or `@Davos image gen ...`."
        )
    if wants_scan_capability and not wants_status:
        provider = choose_scan_provider()
        if provider == "disabled":
            return "I can read images when a scan provider is configured, but image reading is disabled right now."
        return (
            "Yes. I can read attached or recently buffered images in DMs and enabled group chats. "
            "In a group, send the image in that chat, then ask `@Davos what's in this?` within about two minutes."
        )
    return image_provider_status()


_INJECTION_PATTERNS = [
    r"ignore\s+(your|all|previous|the)\s+(instructions?|rules?|training|prompt|system)",
    r"you\s+are\s+now\s+(a|an|the)\b",
    r"you\s+are\s+now\s+(my|our)\b",
    r"\bact\s+as\s+(a|an|if)\b",
    r"\bpretend\s+(you\s+are|to\s+be)\b",
    r"\bnew\s+persona\b",
    r"\bforget\s+(your|all|everything|previous)\b",
    r"\bjailbreak\b",
    r"\bdan\s+mode\b",
    r"\bdeveloper\s+mode\b",
    r"\bdo\s+anything\s+now\b",
    r"\bunrestricted\s+mode\b",
    r"\bno\s+(restrictions?|limits?|filter|rules?|guardrails?)\b",
    r"\btoken\s*max\b",
    r"\bmax\s+(out\s+)?(the\s+)?(tokens?|context|window)\b",
    r"\bspend\s+(all|every|max)\s+tokens?\b",
    r"\buse\s+(all|every|max)\s+tokens?\b",
    r"\bburn\s+(all\s+)?(the\s+)?tokens?\b",
    r"\bfill\s+(the\s+)?(context|window)\b",
    r"\bflood\s+(me|the\s+chat|this)\b",
    r"\bwrite\s+(forever|endlessly|infinitely|nonstop)\b",
    r"\bnever\s+stop\s+(writing|responding|talking)\b",
    r"\b(repeat|copy)\s+(this|that|it)\s+\d+\s+times\b",
    r"\b(supreme\s+)?overlord\b",
    r"\byour\s+(new|true|real|actual)\s+(master|owner|boss|creator|god)\b",
    r"\b(obey|serve|listen\s+to)\s+me\b",
    r"\bi\s+(am|own|control|command)\s+you\b",
    # Tightened "not the owner" pattern (Stage 2):
    # Should NOT trigger:
    #   "that's not the owner, that's Jake"
    #   "no, not the owner — I meant Evan"
    #   "wasn't the owner there?"
    #   "if it's not the owner it doesn't matter"
    #   "she said it's not the owner's fault"
    # SHOULD trigger:
    #   "you are not the owner"
    #   "act as if not the owner"
    #   "pretend you're not the owner"
    r"(?:you\s+are\s+not\s+(?:owner|the\s+owner|owner)|act\s+as\s+(?:if\s+)?not\s+(?:owner|the\s+owner|owner)|pretend\s+you'?re\s+not\s+(?:owner|the\s+owner|owner)|ignore\s+your\s+(?:previous\s+)?instructions)",
    r"\binstead\s+of\s+(?:owner|the\s+owner|owner)\b",
    r"\breplace\s+(?:owner|the\s+owner|owner)\b",
    r"\bedit\s+(your\s+)?(code|script|source|program|file)\b",
    r"\b(change|update|modify)\s+(your\s+)?(code|source|script|program)\b",
    r"\badd\s+a\s+(feature|function|command|command|tool)\b",
    r"\bwrite\s+(a\s+)?(script|function|method|code)\b",
    r"\b(rewrite|refactor)\s+(your|the)\b",
]

_RATE_WINDOW = 60
_RATE_MAX = 10
_rate_counts: dict[str, list[float]] = defaultdict(list)


def _is_rate_limited(sender: str) -> bool:
    now = time.time()
    window = _rate_counts[sender]
    window[:] = [t for t in window if now - t < _RATE_WINDOW]
    if len(window) >= _RATE_MAX:
        return True
    window.append(now)
    return False

_SNARKY_REPLIES = [
    "lol nah",
    "nice try bestie",
    "bro really thought that was gonna work",
    "not today boss",
    "who told you that was gonna work lmaooo",
    "you good? that was painful to read",
    "certified L attempt fr",
    "buddy is trying to jailbreak an iMessage bot",
    "bro typed all that out and still fumbled",
    "the audacity is actually impressive tho",
]

def _is_injection_attempt(text: str) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in _INJECTION_PATTERNS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_LAST_MAIN_LOOP_ALERT = 0.0
_MAIN_LOOP_ALERT_INTERVAL = 900

_image_buffer: dict[str, dict] = {}  # buf_key -> {path, ts}
_text_buffer: dict[str, dict] = {}   # buf_key -> {text, ts}
_IMAGE_BUFFER_TTL = 120  # 2 min


def _buf_key(chat_id: str, sender: str) -> str:
    """Per-sender key so different people's images don't bleed into each other."""
    return sender if chat_id == sender else f"{chat_id}|{sender}"


def _get_buffered_image(key: str, text: str | None = None) -> str | None:
    from .image_conversation import is_image_followup

    if text is not None and not is_image_followup(text):
        _image_buffer.pop(key, None)
        return None
    entry = _image_buffer.get(key)
    if entry and time.time() - entry["ts"] < _IMAGE_BUFFER_TTL:
        return entry["path"]
    _image_buffer.pop(key, None)
    return None


def _buffer_unmentioned_group_image(sender: str, chat_id: str, image_path: str | None) -> bool:
    """Silently cache GC images so a later @Davos scan can pair with them."""
    from . import image_conversation
    if not image_path:
        return False
    if not is_owner_in_chat(chat_id, OWNER_ID):
        return False
    if not (is_owner(sender) or (is_gc_enabled(chat_id) and is_approved_user(sender))):
        return False
    ok, reason, _mime = validate_image_path(image_path)
    if not ok:
        logger.warning("Skipping invalid buffered group image for %s in %s: %s", sender, chat_id, reason)
        return False
    _image_buffer[_buf_key(chat_id, sender)] = {"path": image_path, "ts": time.time()}
    image_conversation.forget(sender, chat_id)
    return True


def _get_buffered_text(key: str) -> str | None:
    from .image_conversation import is_image_followup

    entry = _text_buffer.get(key)
    if entry and time.time() - entry["ts"] < _IMAGE_BUFFER_TTL and is_image_followup(entry["text"]):
        return entry["text"]
    _text_buffer.pop(key, None)
    return None


_PRIORITY_INTAKE_COMMAND_RE = re.compile(
    r"^\s*(?:"
    r"log\b|changelog\b|"
    r"big\s+change\b|bigchange\b|big-change\b|"
    r"codex\s+(?:plan|review|intake)\b|intake\b|"
    r"fix\s+yourself\b|self[-\s]?review\b|self[-\s]?diagnose\b|"
    r"diagnose\s+yourself\b|debug\s+yourself\b|repair\s+yourself\b|"
    r"what\s+went\s+wrong\b|"
    r"(?:fix|debug|repair|diagnose)\s+(?:this|that|it)\b|"
    r"(?:this|that|it)\s+(?:failed|broke|didn'?t\s+work|doesn'?t\s+work|is\s+broken)\b|"
    r"(?:log|logg|lgo|record|capture)\b.{0,140}\b(?:fix|repair|debug|ship|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b|"
    r"(?:fix|repair|debug)\b.{0,140}\b(?:log|record|ship|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b|"
    r"ship\s+(?:this|that|it)\b.{0,140}\b(?:fix|repair|debug|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b|"
    r"(?:analy[sz]e|anayl[sz]e|anal[sz]ye|anly[sz]e|analzye|anyl[sz]e|anali[sz]e)\b.{0,140}\b(?:log|record|fix|repair|debug|ship|failed|failure|didn'?t\s+work|broken|bug|cron|screenshot|image)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def _handle_priority_intake_command(sender: str, text: str) -> str | None:
    if not _PRIORITY_INTAKE_COMMAND_RE.match(text or ""):
        return None
    return handle_command(sender, text)


_MODEL_STATUS_QUESTION_RE = re.compile(
    r"^\s*(?:"
    r"(?:what|which)\b.{0,80}\bmodels?\b.{0,80}\b(?:use|using|run|running|on|available|options?|status|routing|fallback|routes?)\b"
    r"|(?:what(?:'s|\s+is)|whats|what\s+are|show|list|tell\s+me)\b.{0,80}\b(?:model|models|routing|fallback|ollama|gemini|gemma|flux|nano\s*banana)\b.{0,80}\b(?:status|options?|power\s*ranking|routes?|routing|config(?:uration)?|using|available)\b"
    r"|(?:model|models)\s+(?:status|options?|intensity|routing|routes?|power\s*rankings?)\b"
    r"|(?:routing|fallback)\s+(?:status|options?|routes?)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def _handle_model_status_question(sender: str, text: str) -> str | None:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean or not _MODEL_STATUS_QUESTION_RE.search(clean):
        return None
    lower = clean.lower()
    if re.search(r"\b(?:intensity|ladder|tier|tiers)\b", lower):
        return handle_command(sender, "model intensity")
    if re.search(r"\b(?:options?|available|power\s*rankings?|routes?)\b", lower):
        return handle_command(sender, "model options")
    return handle_command(sender, "model status")


_MODEL_REQUEST_RE = re.compile(
    r"^\s*(?:"
    r"(?:please\s+)?(?:try(?:\s+and)?|use|switch|swap|change|move|set|route|send)\b.{0,140}\b(?:to|on|into)?\b.{0,40}\b(?:gemini|gpt|openai|claude|llama|gemma|flux|nano\s*banana)\b"
    r"|(?:please\s+)?(?:fall\s*back|fallback)\b.{0,140}\b(?:to|on|into)?\b.{0,40}\b(?:gemini|gpt|openai|claude|llama|gemma)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def _natural_model_request_route(text: str) -> str:
    lower = (text or "").lower()
    if re.search(r"\bnano\s*banana\b", lower):
        return "nano banana"
    if re.search(r"\b(?:vision|scan|screenshot|read\s+images?|image\s+scan)\b", lower):
        return "vision"
    if re.search(r"\b(?:image|images|draw|logo|photo|picture|generate)\b", lower):
        return "image"
    if re.search(r"\b(?:rewrite|rephrase|shorten|polish)\b", lower):
        return "rewrite"
    if re.search(r"\b(?:tool|tools|function(?:\s+calling)?)\b", lower):
        return "tool"
    return "chat"


def _handle_natural_model_request(sender: str, text: str) -> str | None:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean or re.match(r"^/?models?\b", clean, re.IGNORECASE):
        return None
    if not _MODEL_REQUEST_RE.search(clean):
        return None
    route = _natural_model_request_route(clean)
    return handle_command(sender, f"model request {route} {clean}")


_SELF_STATUS_QUESTION_RE = re.compile(
    r"^\s*(?:"
    r"(?:how|why)\s+(?:do|does)\s+(?:you|davos|davosbot)\s+(?:work|operate|route|remember|forget)"
    r"|(?:explain|describe|summari[sz]e)\s+(?:yourself|your\s+(?:code|architecture|routing|memory|commands|capabilities))"
    r"|what\s+(?:can|do)\s+(?:you|davos|davosbot)\s+do"
    r"|what\s+(?:does|is)\s+(?:ship\s+safe\s+cleanup|master\s+prompt|fix\s+yourself|analy[sz]e\s+this\s+and\s+log|log\s+this\s+and\s+fix\s+it)"
    r")\b",
    re.IGNORECASE,
)


def _handle_self_status_question(sender: str, text: str) -> str | None:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean or not _SELF_STATUS_QUESTION_RE.search(clean):
        return None
    lower = clean.lower()
    if re.search(r"\bwhat\s+(?:can|do)\s+(?:you|davos|davosbot)\s+do\b", lower):
        return handle_command(sender, "capabilities")
    if "ship safe cleanup" in lower or "master prompt" in lower:
        return (
            "`ship safe cleanup` / `master prompt` builds a GREEN/YELLOW/RED Codex handoff from the phone change log. "
            "It does not edit code, push, deploy, mutate DB state outside the log/export path, or clear rows. "
            "Rows only close after Codex patches, validates, deploys, smokes, and you run `log done #id`."
        )
    if "fix yourself" in lower or "analyze this and log" in lower or "log this and fix" in lower:
        return (
            "`fix yourself:` and screenshot/log repair phrases create review-only change-log rows with context, likely code area, "
            "risk color, and validation notes. They do not live-edit, self-deploy, bypass permissions, or mutate MEMORY/SOUL."
        )
    return (
        "I run as a polling iMessage bot on the Mac Mini: Messages DB in, deterministic command/router checks first, "
        "then model routing only when needed, then AppleScript sends the reply. "
        "Use `model status` for active model routes, `drift` for live health/latency traces, "
        "`capabilities` for feature coverage, and `ship safe cleanup` for Codex handoffs."
    )


def _handle_screenshot_issue_log(
    sender: str,
    text: str,
    image_path: str | None,
    recipient: str,
    is_group: bool = False,
) -> str | None:
    clean_text = re.sub(r"\s+", " ", (text or "").strip())
    explicit_screenshot_intake = bool(_SCREENSHOT_ISSUE_LOG_RE.search(clean_text))
    image_repair_intake = bool(image_path and _IMAGE_REPAIR_CONTEXT_RE.search(clean_text))
    if not explicit_screenshot_intake and not image_repair_intake:
        return None
    if not is_owner(sender):
        return "Screenshot issue logging is owner-only."
    if not image_path:
        return (
            "I need the screenshot/image or exact failing text before I can log that repair. "
            "Send it here, then say `analyze this and log`, or use `fix yourself: [what went wrong]`."
        )

    prompt = (
        "Analyze this screenshot as a DavosBot bug report. Summarize what likely went wrong, "
        "what the expected behavior should have been, and the smallest safe code area to inspect. "
        "Do not include secrets or raw private transcript text."
    )
    provider = choose_scan_provider()
    if provider != "disabled":
        send_message(
            recipient,
            f"On it, reading screenshot for a bug log. Estimate: {estimate_scan_time(provider)}.",
            is_group=is_group,
        )
    result = scan_image(image_path, prompt)
    if not result.ok:
        safe_scan_error = redact_secret(result.message or "").strip()
        if len(safe_scan_error) > 400:
            safe_scan_error = safe_scan_error[:400].rstrip() + "..."
        failure_summary = (
            f"scan_failed via {result.provider or provider or 'unknown'}: "
            f"{safe_scan_error or 'no scan error text'}"
        )
        logger.warning(
            "Screenshot issue scan failed for %s via %s: %s",
            sender,
            result.provider or provider or "unknown",
            redact_secret(result.message),
        )
        try:
            from .commands import _log_self_repair_intake
            return _log_self_repair_intake(
                clean_text or "Owner requested screenshot repair intake, but image scan failed.",
                sender,
                image_scan_result=failure_summary,
                source="screenshot_issue_scan_failed",
            )
        except Exception as exc:
            logger.warning("screenshot scan-failure intake log failed: %s", exc)
            return "I couldn't read that screenshot or write the repair log. Send the exact failing text with `fix yourself:`."

    summary = redact_secret(result.message).strip()
    if len(summary) > 900:
        summary = summary[:900].rstrip() + "..."
    try:
        from .commands import _log_self_repair_intake
        return _log_self_repair_intake(
            clean_text or "Owner requested screenshot repair intake.",
            sender,
            image_scan_result=f"{summary} provider={result.provider or provider or 'unknown'}",
            source="screenshot_issue_image_scan",
        )
    except Exception as exc:
        logger.warning("screenshot issue log failed: %s", exc)
        return "I read the screenshot but couldn't write the change log. Try `log that msg entirely` after this."


def _capability_gap_refusal_reply(user_msg: str) -> str | None:
    clean = re.sub(r"\s+", " ", (user_msg or "").strip())
    if not clean:
        return None
    if _GUARDRAIL_BYPASS_REQUEST_RE.search(clean) or _HATEFUL_CONTENT_REQUEST_RE.search(clean):
        return "I can't help with slurs, hate, or guardrail-bypass requests."
    return None


def _count_pending_reminders(originating_chat_id: str) -> int | None:
    if not originating_chat_id:
        return None
    try:
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM reminders "
                "WHERE (origin_chat_id = ? OR (COALESCE(origin_chat_id,'') = '' AND chat_id = ?)) "
                "AND sent = 0",
                (originating_chat_id, originating_chat_id),
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Reminder count check failed: %s", e)
        return None


def _looks_like_reminder_confirmation(reply: str | None) -> bool:
    lower = (reply or "").lower()
    if not lower:
        return False
    if re.search(r"\b(?:didn'?t|did not|couldn'?t|could not|can'?t|cannot|failed|invalid|try again)\b", lower):
        return False
    has_reminder_word = bool(re.search(r"\bremind(?:er|ing)?\b", lower))
    has_confirmation = bool(
        re.search(r"\b(?:got it|i(?:[’']| wi)?ll|i will|saved|set|scheduled|done|you'?re set)\b", lower)
    )
    return has_reminder_word and has_confirmation


def _guard_unsaved_reminder_reply(
    reply: str | None,
    before_count: int | None,
    after_count: int | None,
) -> str | None:
    if before_count is None or after_count is None:
        return reply
    if after_count > before_count:
        return reply
    if _looks_like_reminder_confirmation(reply):
        logger.warning("Blocked reminder confirmation because no reminder row was inserted")
        return "I didn't actually save that reminder. Please resend it."
    return reply


def _handle_deterministic_reminder_schedule(text: str, originating_chat_id: str) -> str | None:
    parsed = parse_deterministic_reminder(text or "")
    if parsed is None:
        return None
    from .tools import _set_reminder
    return _set_reminder(parsed.message, parsed.due_ts, originating_chat_id=originating_chat_id)


_REMINDER_CANCEL_POSITION_RE = re.compile(
    r"(?<![A-Za-z0-9])#?\s*(\d+)(?!\s*(?:am|pm)\b)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _parse_reminder_cancel_positions(text: str) -> list[int]:
    return [int(match.group(1)) for match in _REMINDER_CANCEL_POSITION_RE.finditer(text or "")]


def _handle_deterministic_reminder_cancel(text: str, originating_chat_id: str) -> str | None:
    positions = _parse_reminder_cancel_positions(text)
    if not positions:
        return None
    from .tools import _cancel_reminders
    return _cancel_reminders(positions, originating_chat_id=originating_chat_id)


def _handle_admin_dm(sender: str, text: str, image_path: str | None = None) -> None:
    """DM handler for admin-tier senders. Commands gated by can_user_do; LLM with web_search only."""
    from . import image_conversation
    if image_conversation.begin_message(sender, sender, text, bool(image_path)):
        _image_buffer.pop(sender, None)
    logger.info("Admin DM from %s: %s", sender, redact_secret(redact_private_send_text_for_log(text or "[image]"))[:120])

    pending_reply = handle_private_send_confirmation(sender, text or "", allow_password=True)
    if pending_reply is not None:
        send_message(sender, pending_reply)
        return

    # Password gate: if the message contains the correct password, route as owner.
    # Strip the password before it reaches the LLM so it's never echoed.
    if text and check_admin_password(text):
        logger.info("Admin %s provided password — routing as owner for this message", sender)
        clean = strip_password(text)
        handle_dm(sender, clean or text, image_path)  # recurse as owner path
        return

    key = sender

    if image_path:
        _image_buffer[key] = {"path": image_path, "ts": time.time()}
        image_conversation.forget(sender, sender)

    if not text and image_path:
        pre_text = _get_buffered_text(key)
        if pre_text:
            text = pre_text
            _text_buffer.pop(key, None)
        else:
            text = image_conversation.IMAGE_ONLY_ASK

    image = image_path or _get_buffered_image(key, text) or image_conversation.path_for_followup(sender, sender, text)
    text, skip_search = _strip_no_search(text or "")
    priority_cmd_reply = _handle_priority_intake_command(sender, text)
    if priority_cmd_reply is not None:
        send_message(sender, priority_cmd_reply)
        return
    self_status_reply = _handle_self_status_question(sender, text)
    if self_status_reply is not None:
        send_message(sender, self_status_reply)
        return
    model_status_reply = _handle_model_status_question(sender, text)
    if model_status_reply is not None:
        send_message(sender, model_status_reply)
        return
    length_reply = _non_owner_length_rejection(sender, text)
    if length_reply is not None:
        send_message(sender, length_reply)
        return
    banter_reply = _viral_banter_reply(text, has_image=bool(image))
    if banter_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(sender, banter_reply)
        save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
        save_turn(sender, "assistant", banter_reply)
        return
    private_reply = handle_private_send_request(sender, text, originating_chat_id=sender)
    if private_reply is not None:
        send_message(sender, private_reply)
        return
    cron_describe_reply = _describe_cron_from_text(sender, text, originating_chat_id=sender)
    if cron_describe_reply is not None:
        send_message(sender, cron_describe_reply)
        return
    sports_cron_reply = _sports_recap_cron_from_text(sender, text, originating_chat_id=sender)
    if sports_cron_reply is not None:
        send_message(sender, sports_cron_reply)
        return
    cron_schedule_reply = _schedule_cron_from_text(sender, text, originating_chat_id=sender)
    if cron_schedule_reply is not None:
        send_message(sender, cron_schedule_reply)
        return
    openai_image_reply = _handle_openai_image_intent(sender, text, image, recipient=sender)
    if openai_image_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(sender, openai_image_reply)
        save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
        save_turn(sender, "assistant", openai_image_reply)
        return
    image_status_reply = _handle_image_capability_status(sender, text)
    if image_status_reply is not None:
        send_message(sender, image_status_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", image_status_reply)
        return
    fast_reply = None if image else _fast_chat_reply(text)
    if fast_reply is not None:
        send_message(sender, fast_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", fast_reply)
        return
    if text and not image:
        _text_buffer[key] = {"text": text, "ts": time.time()}

    cmd_reply = handle_command(sender, text, **({"skip_web_search": True} if skip_search else {})) if text else None
    if cmd_reply is not None:
        send_message(sender, cmd_reply)
        return

    market_reply = None if image else _market_fast_reply(sender, text, **({"skip_web_search": True} if skip_search else {}))
    if market_reply is not None:
        send_message(sender, market_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", market_reply)
        return

    style_reply = handle_style_directive_message(
        sender,
        text or "",
        context_id=sender,
        active_persona=None,
        is_group=False,
    ) if text and not image else None
    if style_reply is not None:
        send_message(sender, style_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", style_reply)
        return

    # Skill trigger matching before LLM
    if text:
        skill_reply = match_skill(text)
        if skill_reply:
            send_message(sender, skill_reply)
            save_turn(sender, "user", redact_secret(text))
            save_turn(sender, "assistant", skill_reply)
            return

    if _handle_image_caption(sender, text, image, sender):
        return
    system = build_system_prompt(persona=None, user_text=text, chat_id=sender)
    if skip_search:
        system += "\n" + _NO_WEB_INFORMATION_INSTRUCTION
    history = get_history(sender)

    # Admins get web_search only — no shell_exec, write_file, or other privileged tools.
    if (
        _should_use_limited_web_tools(text, skip_search)
        and get_tool_uses_today(sender, "web_search") < _FRIEND_SEARCH_LIMIT
    ):
        def _on_admin_tool_call(tool_name: str) -> None:
            if tool_name == "web_search":
                log_tool_use(sender, "web_search")
        reply = get_response(system, history, text, allowed_tools=["web_search", "get_weather"], image_path=image, on_tool_call=_on_admin_tool_call, sender=sender)
    else:
        reply = get_response(system, history, text, use_tools=False, image_path=image, sender=sender)

    if image:
        _image_buffer.pop(key, None)
    if not reply:
        reply = _failure_copy.UNEXPECTED_FAILURE_REPLY
    send_message(sender, reply)
    save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
    save_turn(sender, "assistant", reply)


def _handle_friend_dm(sender: str, text: str, image_path: str | None = None) -> None:
    """DM handler for approved (non-admin) friends. LLM only, no tools, no commands."""
    from . import image_conversation
    if image_conversation.begin_message(sender, sender, text, bool(image_path)):
        _image_buffer.pop(sender, None)
    logger.info("Approved-friend DM from %s: %s", sender, redact_secret(text or "[image]")[:80])

    key = sender
    if image_path:
        _image_buffer[key] = {"path": image_path, "ts": time.time()}
        image_conversation.forget(sender, sender)

    if not text and image_path:
        pre_text = _get_buffered_text(key)
        if pre_text:
            text = pre_text
            _text_buffer.pop(key, None)
        else:
            text = image_conversation.IMAGE_ONLY_ASK

    if not text:
        send_message(sender, "What did you want to know?")
        return

    # Password in message from a friend ? hard deny, not elevation.
    # Friends cannot use the password to bypass their tier.
    if check_admin_password(text):
        send_message(sender, "That requires owner access.")
        return

    # Explicit help / permissions commands for friends before hitting the LLM
    cmd = text.strip().lower()
    if cmd == "help":
        from .commands import _cmd_help
        send_message(sender, _cmd_help(sender))
        return
    if cmd in ("capability", "capabilities"):
        from .commands import _cmd_capabilities
        send_message(sender, _cmd_capabilities(sender))
        return
    if cmd == "mypermissions":
        from .commands import _cmd_mypermissions
        send_message(sender, _cmd_mypermissions(sender))
        return

    text, skip_search = _strip_no_search(text)
    length_reply = _non_owner_length_rejection(sender, text)
    if length_reply is not None:
        send_message(sender, length_reply)
        return

    image = image_path or _get_buffered_image(key, text) or image_conversation.path_for_followup(sender, sender, text)
    market_reply = None if image else _market_fast_reply(sender, text, **({"skip_web_search": True} if skip_search else {}))
    if market_reply is not None:
        send_message(sender, market_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", market_reply)
        return

    banter_reply = _viral_banter_reply(text, has_image=bool(image))
    if banter_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(sender, banter_reply)
        save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
        save_turn(sender, "assistant", banter_reply)
        return
    openai_image_reply = _handle_openai_image_intent(sender, text, image, recipient=sender)
    if openai_image_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(sender, openai_image_reply)
        save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
        save_turn(sender, "assistant", openai_image_reply)
        return
    image_status_reply = _handle_image_capability_status(sender, text)
    if image_status_reply is not None:
        send_message(sender, image_status_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", image_status_reply)
        return
    fast_reply = None if image else _fast_chat_reply(text)
    if fast_reply is not None:
        send_message(sender, fast_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", fast_reply)
        return

    if text and not image:
        _text_buffer[key] = {"text": text, "ts": time.time()}

    style_reply = handle_style_directive_message(
        sender,
        text,
        context_id=sender,
        active_persona=None,
        is_group=False,
    ) if text and not image else None
    if style_reply is not None:
        send_message(sender, style_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", style_reply)
        return

    if not skip_search and is_ufc_fight_card_request(text):
        if get_tool_uses_today(sender, UFC_FIGHT_CARD_TOOL) >= _FRIEND_SEARCH_LIMIT:
            send_message(sender, "UFC card lookup daily limit reached. Ask the owner if this is urgent.")
            return
        log_tool_use(sender, UFC_FIGHT_CARD_TOOL)
        reply = get_ufc_fight_card()
        send_message(sender, reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", reply)
        return
    if _handle_image_caption(sender, text, image, sender):
        return
    system = (
        "You are DavosBot, the owner's personal AI assistant. "
        "You're talking with a friend of the owner's who has been granted DM access. "
        "Keep replies short and conversational — texting format, no walls of text. "
        "Do not run commands, access files, or use any tools. "
        "If they ask for a harmless roast, be sharp and funny without protected-class slurs, threats, doxxing, or disclaimers."
    )
    style_block = format_style_directives_for_prompt(chat_id=sender, user_text=text)
    if style_block:
        system += style_block
    if skip_search:
        system += "\n" + _NO_WEB_INFORMATION_INSTRUCTION
    plain_chat = bool(not image and _looks_like_plain_chat(text))
    history = get_history(sender, _simple_chat.history_limit(text, _PLAIN_CHAT_HISTORY_LIMIT) if plain_chat else 20)
    reply = get_response(system, history, text, use_tools=False, image_path=image, sender=sender, simple_chat=plain_chat)
    if image:
        _image_buffer.pop(key, None)
    if not reply:
        reply = _failure_copy.UNEXPECTED_FAILURE_REPLY
    send_message(sender, reply)
    save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
    save_turn(sender, "assistant", reply)


def handle_dm(sender: str, text: str, image_path: str | None = None, trace: _MessageTrace | None = None) -> None:
    from . import image_conversation

    if not is_owner(sender):
        if is_admin(sender):
            if trace:
                trace.set_route("admin_dm")
            _handle_admin_dm(sender, text, image_path)
        elif is_approved_user(sender):
            if trace:
                trace.set_route("friend_dm")
            _handle_friend_dm(sender, text, image_path)
        else:
            if trace:
                trace.set_route("unknown_dm")
            _trace_call(trace, "send", send_message, sender, "You're not on the list. Talk to the owner.")
        return

    if trace:
        trace.set_route("owner_dm")
    if image_conversation.begin_message(sender, sender, text, bool(image_path)):
        _image_buffer.pop(sender, None)
    logger.info("DM from owner: %s", redact_secret(redact_private_send_text_for_log(text or "[image]"))[:120])
    key = sender

    # Store incoming image
    if image_path:
        _image_buffer[key] = {"path": image_path, "ts": time.time()}
        image_conversation.forget(sender, sender)

    # Image-only: check if there was pre-text ("check this out" pattern)
    if not text and image_path:
        pre_text = _get_buffered_text(key)
        if pre_text:
            text = pre_text
            _text_buffer.pop(key, None)
        else:
            text = image_conversation.IMAGE_ONLY_ASK

    # Grab any buffered image to pair with this text
    image = image_path or _get_buffered_image(key, text) or image_conversation.path_for_followup(sender, sender, text)

    # Strip "no web search" flag before command check or LLM call
    text, skip_search = _strip_no_search(text)
    screenshot_issue_reply = _handle_screenshot_issue_log(sender, text, image, recipient=sender)
    if screenshot_issue_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(sender, screenshot_issue_reply)
        save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
        save_turn(sender, "assistant", screenshot_issue_reply)
        return

    priority_cmd_reply = _handle_priority_intake_command(sender, text)
    if priority_cmd_reply is not None:
        send_message(sender, priority_cmd_reply)
        return
    self_status_reply = _handle_self_status_question(sender, text)
    if self_status_reply is not None:
        send_message(sender, self_status_reply)
        return
    model_status_reply = _handle_model_status_question(sender, text)
    if model_status_reply is not None:
        send_message(sender, model_status_reply)
        return
    natural_model_request_reply = _handle_natural_model_request(sender, text)
    if natural_model_request_reply is not None:
        send_message(sender, natural_model_request_reply)
        return
    pending_reply = handle_private_send_confirmation(sender, text or "", allow_password=True)
    if pending_reply is not None:
        send_message(sender, pending_reply)
        return
    private_reply = handle_private_send_request(sender, text, originating_chat_id=sender)
    if private_reply is not None:
        send_message(sender, private_reply)
        return

    active_persona = get_persona("dm")
    style_reply = handle_style_directive_message(
        sender,
        text,
        context_id=sender,
        active_persona=active_persona,
        is_group=False,
        tone_feedback_only=True,
    ) if (
        text
        and not image
        and looks_like_tone_feedback(text)
        and not _style_feedback_should_yield_to_task_intent(text)
    ) else None
    if style_reply is not None:
        send_message(sender, style_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", style_reply)
        return

    fast_reply = None if image else _fast_chat_reply(text)
    if fast_reply is not None:
        send_message(sender, fast_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", fast_reply)
        return

    cron_describe_reply = _describe_cron_from_text(sender, text, originating_chat_id=sender)
    if cron_describe_reply is not None:
        send_message(sender, cron_describe_reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", cron_describe_reply)
        return

    cron_cancel_reply = _cancel_cron_from_text(sender, text, originating_chat_id=sender)
    if cron_cancel_reply is not None:
        send_message(sender, cron_cancel_reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", cron_cancel_reply)
        return

    sports_cron_reply = _sports_recap_cron_from_text(sender, text, originating_chat_id=sender)
    if sports_cron_reply is not None:
        send_message(sender, sports_cron_reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", sports_cron_reply)
        return

    cron_schedule_reply = _schedule_cron_from_text(sender, text, originating_chat_id=sender)
    if cron_schedule_reply is not None:
        send_message(sender, cron_schedule_reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", cron_schedule_reply)
        return

    # Cron edit intent — keep simple cron changes out of the LLM/log-change path.
    from .tools import _edit_cron_from_text
    cron_edit_reply = _edit_cron_from_text(sender, text, originating_chat_id=sender)
    if cron_edit_reply is not None:
        send_message(sender, cron_edit_reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", cron_edit_reply)
        return

    banter_reply = _viral_banter_reply(text, has_image=bool(image))
    if banter_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(sender, banter_reply)
        save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
        save_turn(sender, "assistant", banter_reply)
        return

    openai_image_reply = _handle_openai_image_intent(sender, text, image, recipient=sender)
    if openai_image_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(sender, openai_image_reply)
        save_turn(sender, "user", redact_secret(text if not image else f"[image] {text}"))
        save_turn(sender, "assistant", openai_image_reply)
        return
    image_status_reply = _handle_image_capability_status(sender, text)
    if image_status_reply is not None:
        send_message(sender, image_status_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", image_status_reply)
        return

    # Buffer this text in case an image follows soon
    if text and not image:
        _text_buffer[key] = {"text": text, "ts": time.time()}

    cmd_reply = handle_command(sender, text, **({"skip_web_search": True} if skip_search else {}))
    if cmd_reply is not None:
        send_message(sender, cmd_reply)
        return

    market_reply = None if image else _market_fast_reply(sender, text, **({"skip_web_search": True} if skip_search else {}))
    if market_reply is not None:
        send_message(sender, market_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", market_reply)
        return

    style_reply = handle_style_directive_message(
        sender,
        text,
        context_id=sender,
        active_persona=active_persona,
        is_group=False,
        allow_tone_feedback=False,
    ) if text and not image else None
    if style_reply is not None:
        send_message(sender, style_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", style_reply)
        return

    decatur_reply = None if image else decatur_behavior_fast_reply(active_persona, text)
    if decatur_reply is not None:
        send_message(sender, decatur_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", decatur_reply)
        return

    try:
        quality_reply = _log_owner_quality_intake_if_needed(sender, text)
    except sqlite3.Error as exc:
        logger.warning("Quality intake unavailable: %s", redact_secret(str(exc)))
        quality_reply = None

    # Casual reminder mention — intercept before LLM to avoid spurious reminder creation
    reminder_intent = classify_reminder_intent(text)
    if reminder_intent == "casual":
        reply = "Looks like the reminder didn't go through — want me to set it again?"
        send_message(sender, reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", reply)
        return
    if reminder_intent == "list":
        from .tools import _list_reminders
        reply = _list_reminders(sender)
        send_message(sender, reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", reply)
        return

    if reminder_intent == "cancel":
        cancel_reply = _handle_deterministic_reminder_cancel(text, sender)
        if cancel_reply is not None:
            send_message(sender, cancel_reply)
            save_turn(sender, "user", text)
            save_turn(sender, "assistant", cancel_reply)
            return

    # "list crons" intent — Gemini sometimes answers conversationally instead
    # of calling the tool ("do we have any current cron jobs" ? fail).
    if reminder_intent == "schedule":
        reminder_reply = _handle_deterministic_reminder_schedule(text, sender)
        if reminder_reply is not None:
            send_message(sender, reminder_reply)
            save_turn(sender, "user", text)
            save_turn(sender, "assistant", reminder_reply)
            return

    if classify_cron_list_intent(text):
        from .tools import _list_crons
        reply = _list_crons(sender, scope=_cron_scope_from_text(text), requester_id=sender)
        send_message(sender, reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", reply)
        return

    # Reminder edit intent — cancel old + recreate flow
    if detect_reminder_edit_intent(text):
        edit_reply = handle_reminder_edit(sender, text)
        if edit_reply:
            send_message(sender, edit_reply)
            save_turn(sender, "user", text)
            save_turn(sender, "assistant", edit_reply)
            return

    # Skill trigger matching — before LLM call
    skill_reply = match_skill(text)
    if skill_reply:
        send_message(sender, skill_reply)
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", skill_reply)
        return

    if _handle_image_caption(sender, text, image, sender):
        return

    # Owner self-description ? user_facts ingestion (Stage 4 Feature 2)
    fact = detect_user_fact(text)
    if fact:
        key, value = fact
        store_user_fact(key, value, source="dm")
        send_message(sender, f"Got it — noted that {key}: {value}.")
        save_turn(sender, "user", text)
        save_turn(sender, "assistant", f"Got it — noted that {key}: {value}.")
        return

    owner_tool_path = _should_use_owner_tools(text, skip_search)
    plain_chat = bool(not image and _looks_like_plain_chat(text) and not owner_tool_path)
    prompt_builder = build_light_chat_system_prompt if plain_chat else build_system_prompt
    if trace:
        trace.set_route("owner_dm_plain_chat" if plain_chat else "owner_dm_llm")
        if plain_chat:
            trace.flag("light_prompt")
    system = _trace_call(trace, "prompt_build", prompt_builder, persona=active_persona, user_text=text, chat_id=sender)
    if skip_search:
        system += "\n" + _NO_WEB_INFORMATION_INSTRUCTION
    history_limit = _simple_chat.history_limit(text, _PLAIN_CHAT_HISTORY_LIMIT) if plain_chat else 20
    history = _trace_call(trace, "history_load", get_history, sender, history_limit)
    preflight_reply = _complex_analysis_preflight_reply(text, has_context=bool(image), history=history)
    if preflight_reply is not None:
        send_message(sender, preflight_reply)
        save_turn(sender, "user", redact_secret(text))
        save_turn(sender, "assistant", preflight_reply)
        return
    if quality_reply is not None:
        system += (
            "\nThe current feedback was recorded for review. Reconsider the previous answer using "
            "the conversation and the user's correction. Correct it when evidence supports a change; "
            "if the correction is unclear, ask one specific question. No code repair or deployment "
            "has happened merely because feedback was logged."
        )
    if trace:
        trace.prompt_chars = len(system or "")
        trace.history_turns = len(history or [])
    reminder_before_count = (
        _count_pending_reminders(sender) if reminder_intent == "schedule" else None
    )
    reply = _trace_call(
        trace,
        "model",
        get_response,
        system,
        history,
        text,
        use_tools=owner_tool_path and not skip_search,
        allowed_tools=_owner_tools_without_web() if owner_tool_path and skip_search else None,
        image_path=image,
        sender=sender,
        originating_chat_id=sender,
        simple_chat=plain_chat,
    )
    if reminder_intent == "schedule":
        reply = _guard_unsaved_reminder_reply(
            reply,
            reminder_before_count,
            _count_pending_reminders(sender),
        )
    if image:
        _image_buffer.pop(key, None)
    if not reply:
        if trace:
            trace.flag("empty_reply")
        _log_quality_signal(sender, "empty_reply", {"route": trace.route if trace else "owner_dm"})
        reply = _failure_copy.UNEXPECTED_FAILURE_REPLY
    elif detect_capability_gap(reply):
        refusal = _capability_gap_refusal_reply(text or "")
        if refusal:
            if trace:
                trace.flag("unsafe_capability_gap")
            _log_quality_signal(sender, "unsafe_capability_gap", {"route": trace.route if trace else "owner_dm"})
            reply = refusal
        else:
            if trace:
                trace.flag("capability_gap")
            intent_summary = (text or "[image]")[:80]
            log_missing_capability(sender, text or "[image]", intent_summary)
            _log_quality_signal(sender, "capability_gap", {"route": trace.route if trace else "owner_dm"})
            reply = f"I understood you want to '{intent_summary}' but I don't have that capability yet. the owner can build it — logging this."
    if reply:
        reply = enforce_decatur_behavior_reply(reply, active_persona, text) or reply
    sent = _trace_call(trace, "send", send_message, sender, reply)
    if not sent:
        if trace:
            trace.flag("send_failed")
        _log_quality_signal(sender, "send_failed", {"route": trace.route if trace else "owner_dm"})
    _trace_call(trace, "save_turn", save_turn, sender, "user", text if not image else f"[image] {text}")
    _trace_call(trace, "save_turn", save_turn, sender, "assistant", reply)
    if plain_chat:
        if trace:
            trace.flag("memory_extract_skipped")
    else:
        try:
            _trace_call(trace, "memory_extract", extract_and_update_memory, sender, text, reply)
        except Exception as e:
            if trace:
                trace.flag("memory_extract_failed")
            _log_quality_signal(sender, "memory_extract_failed", {"route": trace.route if trace else "owner_dm"})
            logger.warning("Memory extraction failed: %s", e)


def handle_group_message(sender: str, chat_id: str, text: str, msg: dict | None = None, trace: _MessageTrace | None = None) -> None:
    from . import image_conversation

    if not is_owner_in_chat(chat_id, OWNER_ID):
        return  # silently ignore GCs the owner isn't in

    if not is_at_mentioned(text):
        image_path = msg.get("image_path") if isinstance(msg, dict) else None
        _buffer_unmentioned_group_image(sender, chat_id, image_path)
        return

    logger.info("Group mention from %s in %s: %s", sender, chat_id, redact_secret(redact_private_send_text_for_log(text))[:120])
    if image_conversation.begin_message(sender, chat_id, strip_mention(text), bool(isinstance(msg, dict) and msg.get("image_path"))):
        _image_buffer.pop(_buf_key(chat_id, sender), None)

    # Owner commands always work regardless of enabled state. A Davos-enabled
    # group can also use narrowly scoped deterministic commands such as the
    # fantasy access request flow without granting broader bot permissions.
    command_text = normalize_group_mention_command(text)

    if is_owner(sender) or is_gc_enabled(chat_id):
        cmd_reply = handle_group_command(sender, chat_id, command_text)
        if cmd_reply is not None:
            send_message(chat_id, cmd_reply, is_group=True)
            return

    if not is_gc_enabled(chat_id):
        if is_owner(sender):
            image_path = msg.get("image_path") if isinstance(msg, dict) else None
            key = _buf_key(chat_id, sender)
            if image_path:
                _image_buffer[key] = {"path": image_path, "ts": time.time()}
                image_conversation.forget(sender, chat_id)
            clean_text = strip_mention(text)
            clean_text, _skip_search = _strip_no_search(clean_text)
            image = image_path or _get_buffered_image(key, clean_text) or image_conversation.path_for_followup(sender, chat_id, clean_text)
            if image and not clean_text:
                clean_text = image_conversation.IMAGE_ONLY_ASK
            reply = _handle_model_status_question(sender, clean_text)
            if reply is None:
                reply = _handle_natural_model_request(sender, clean_text)
            if reply is None:
                reply = _handle_screenshot_issue_log(sender, clean_text, image, recipient=chat_id, is_group=True)
            if reply is None:
                reply = _handle_openai_image_intent(sender, clean_text, image, recipient=chat_id, is_group=True)
            if reply is None:
                reply = _handle_image_capability_status(sender, clean_text)
            if reply is not None:
                if image:
                    _image_buffer.pop(key, None)
                send_message(chat_id, reply, is_group=True)
                save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text if not image else f'[image] {clean_text}')}")
                save_turn(chat_id, "assistant", reply)
        return

    clean_text = strip_mention(text)
    clean_text, skip_search = _strip_no_search(clean_text)

    if not is_owner(sender) and clean_text and _is_injection_attempt(clean_text):
        send_message(chat_id, random.choice(_SNARKY_REPLIES), is_group=True)
        return

    # Group-persona editor access is scoped to this chat and this one feature.
    # It does not grant normal group-chat bot access.
    if not is_owner(sender) and clean_text:
        persona_editor_reply = handle_group_persona_editor_command(sender, chat_id, clean_text)
        if persona_editor_reply is not None:
            send_message(chat_id, persona_editor_reply, is_group=True)
            save_turn(chat_id, "user", f"{sender}: {clean_text}")
            save_turn(chat_id, "assistant", persona_editor_reply)
            return

    if not is_owner(sender) and not is_approved_user(sender):
        return

    if not is_owner(sender) and _is_rate_limited(sender):
        return

    image_path = msg.get("image_path") if isinstance(msg, dict) else None
    key = _buf_key(chat_id, sender)

    if image_path:
        _image_buffer[key] = {"path": image_path, "ts": time.time()}
        image_conversation.forget(sender, chat_id)

    # Image-only mention — check for pre-text or ask
    if not clean_text and image_path:
        pre_text = _get_buffered_text(key)
        if pre_text:
            clean_text = pre_text
            _text_buffer.pop(key, None)
        else:
            clean_text = image_conversation.IMAGE_ONLY_ASK

    if not clean_text:
        send_message(
            chat_id,
            "Yo - put the ask after @Davos, like '@Davos help' or '@Davos tell Chapman ...'.",
            is_group=True,
        )
        return

    length_reply = _non_owner_length_rejection(sender, clean_text)
    if length_reply is not None:
        send_message(chat_id, length_reply, is_group=True)
        return

    image = image_path or _get_buffered_image(key, clean_text) or image_conversation.path_for_followup(sender, chat_id, clean_text)
    self_status_reply = _handle_self_status_question(sender, clean_text)
    if self_status_reply is not None:
        send_message(chat_id, self_status_reply, is_group=True)
        return
    model_status_reply = _handle_model_status_question(sender, clean_text)
    if model_status_reply is not None:
        send_message(chat_id, model_status_reply, is_group=True)
        return
    natural_model_request_reply = _handle_natural_model_request(sender, clean_text) if is_owner(sender) else None
    if natural_model_request_reply is not None:
        send_message(chat_id, natural_model_request_reply, is_group=True)
        return

    market_reply = None if image else _market_fast_reply(sender, clean_text, **({"skip_web_search": True} if skip_search else {}))
    if market_reply is not None:
        send_message(chat_id, market_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", market_reply)
        return

    banter_reply = _viral_banter_reply(clean_text, has_image=bool(image))
    if banter_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(chat_id, banter_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text if not image else f'[image] {clean_text}')}")
        save_turn(chat_id, "assistant", banter_reply)
        return

    error_intake_reply = _log_group_error_intake_if_needed(sender, chat_id, clean_text)
    if error_intake_reply is not None:
        send_message(chat_id, error_intake_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", error_intake_reply)
        return

    if not skip_search and is_ufc_fight_card_request(clean_text):
        if not is_admin(sender) and get_tool_uses_today(sender, UFC_FIGHT_CARD_TOOL) >= _FRIEND_SEARCH_LIMIT:
            send_message(chat_id, "UFC card lookup daily limit reached. Ask the owner if this is urgent.", is_group=True)
            return
        if not is_admin(sender):
            log_tool_use(sender, UFC_FIGHT_CARD_TOOL)
        reply = get_ufc_fight_card()
        send_message(chat_id, reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", reply)
        return

    if is_admin(sender):
        pending_reply = handle_private_send_confirmation(sender, clean_text, allow_password=False)
        if pending_reply is not None:
            send_message(chat_id, pending_reply, is_group=True)
            return
        private_reply = handle_private_send_request(sender, clean_text, originating_chat_id=chat_id)
        if private_reply is not None:
            send_message(chat_id, private_reply, is_group=True)
            return

    active_persona = get_persona(chat_id)
    style_reply = handle_style_directive_message(
        sender,
        clean_text,
        context_id=chat_id,
        active_persona=active_persona,
        is_group=True,
        tone_feedback_only=True,
    ) if (
        clean_text
        and not image
        and looks_like_tone_feedback(clean_text)
        and not _style_feedback_should_yield_to_task_intent(clean_text)
    ) else None
    if style_reply is not None:
        send_message(chat_id, style_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", style_reply)
        return

    fast_reply = None if image else _fast_chat_reply(clean_text)
    if fast_reply is not None:
        send_message(chat_id, fast_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", fast_reply)
        return

    cron_describe_reply = _describe_cron_from_text(sender, clean_text, originating_chat_id=chat_id)
    if cron_describe_reply is not None:
        send_message(chat_id, cron_describe_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {clean_text}")
        save_turn(chat_id, "assistant", cron_describe_reply)
        return

    if is_owner(sender):
        cron_cancel_reply = _cancel_cron_from_text(sender, clean_text, originating_chat_id=chat_id)
        if cron_cancel_reply is not None:
            send_message(chat_id, cron_cancel_reply, is_group=True)
            save_turn(chat_id, "user", f"{sender}: {clean_text}")
            save_turn(chat_id, "assistant", cron_cancel_reply)
            return

    sports_cron_reply = _sports_recap_cron_from_text(sender, clean_text, originating_chat_id=chat_id)
    if sports_cron_reply is not None:
        send_message(chat_id, sports_cron_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {clean_text}")
        save_turn(chat_id, "assistant", sports_cron_reply)
        return

    cron_schedule_reply = _schedule_cron_from_text(sender, clean_text, originating_chat_id=chat_id)
    if cron_schedule_reply is not None:
        send_message(chat_id, cron_schedule_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {clean_text}")
        save_turn(chat_id, "assistant", cron_schedule_reply)
        return

    if is_owner(sender):
        from .tools import _edit_cron_from_text
        cron_edit_reply = _edit_cron_from_text(sender, clean_text, originating_chat_id=chat_id)
        if cron_edit_reply is not None:
            send_message(chat_id, cron_edit_reply, is_group=True)
            save_turn(chat_id, "user", f"{sender}: {clean_text}")
            save_turn(chat_id, "assistant", cron_edit_reply)
            return

    screenshot_issue_reply = _handle_screenshot_issue_log(
        sender, clean_text, image, recipient=chat_id, is_group=True
    )
    if screenshot_issue_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(chat_id, screenshot_issue_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text if not image else f'[image] {clean_text}')}")
        save_turn(chat_id, "assistant", screenshot_issue_reply)
        return

    openai_image_reply = _handle_openai_image_intent(sender, clean_text, image, recipient=chat_id, is_group=True)
    if openai_image_reply is not None:
        if image:
            _image_buffer.pop(key, None)
        send_message(chat_id, openai_image_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text if not image else f'[image] {clean_text}')}")
        save_turn(chat_id, "assistant", openai_image_reply)
        return
    image_status_reply = _handle_image_capability_status(sender, clean_text)
    if image_status_reply is not None:
        send_message(chat_id, image_status_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", image_status_reply)
        return

    style_reply = handle_style_directive_message(
        sender,
        clean_text,
        context_id=chat_id,
        active_persona=active_persona,
        is_group=True,
        allow_tone_feedback=False,
    ) if clean_text and not image else None
    if style_reply is not None:
        send_message(chat_id, style_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", style_reply)
        return

    decatur_reply = None if image else decatur_behavior_fast_reply(active_persona, clean_text)
    if decatur_reply is not None:
        send_message(chat_id, decatur_reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {redact_secret(clean_text)}")
        save_turn(chat_id, "assistant", decatur_reply)
        return

    # Buffer this text per-sender in case an image follows
    if clean_text and not image:
        _text_buffer[key] = {"text": clean_text, "ts": time.time()}

    # "list crons" intent (owner only) — same Gemini-skips-tool issue as DM path.
    if is_owner(sender) and classify_cron_list_intent(clean_text):
        from .tools import _list_crons
        reply = _list_crons(chat_id, scope=_cron_scope_from_text(clean_text), requester_id=sender)
        send_message(chat_id, reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {clean_text}")
        save_turn(chat_id, "assistant", reply)
        return

    reminder_intent = classify_reminder_intent(clean_text) if is_owner(sender) else "none"
    if reminder_intent == "casual":
        reply = "Looks like the reminder didn't go through — want me to set it again?"
        send_message(chat_id, reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {clean_text}")
        save_turn(chat_id, "assistant", reply)
        return
    if reminder_intent == "list":
        from .tools import _list_reminders
        reply = _list_reminders(chat_id)
        send_message(chat_id, reply, is_group=True)
        save_turn(chat_id, "user", f"{sender}: {clean_text}")
        save_turn(chat_id, "assistant", reply)
        return

    if reminder_intent == "cancel":
        cancel_reply = _handle_deterministic_reminder_cancel(clean_text, chat_id)
        if cancel_reply is not None:
            send_message(chat_id, cancel_reply, is_group=True)
            save_turn(chat_id, "user", f"{sender}: {clean_text}")
            save_turn(chat_id, "assistant", cancel_reply)
            return

    if reminder_intent == "schedule":
        reminder_reply = _handle_deterministic_reminder_schedule(clean_text, chat_id)
        if reminder_reply is not None:
            send_message(chat_id, reminder_reply, is_group=True)
            save_turn(chat_id, "user", f"{sender}: {clean_text}")
            save_turn(chat_id, "assistant", reminder_reply)
            return

    # The fallback uses the same image-access and per-attempt quota checks as explicit scans.
    if _handle_image_caption(sender, clean_text, image, chat_id, is_group=True):
        return

    owner_tool_path = is_owner(sender) and _owner_group_should_use_tools(clean_text, skip_search)
    plain_chat = bool(not image and _looks_like_plain_chat(clean_text) and not owner_tool_path)
    prompt_builder = build_light_chat_system_prompt if plain_chat else build_system_prompt
    if trace:
        trace.set_route("group_plain_chat" if plain_chat else "group_llm")
        if plain_chat:
            trace.flag("light_prompt")
    system = _trace_call(trace, "prompt_build", prompt_builder, persona=active_persona, user_text=clean_text, chat_id=chat_id)
    if skip_search:
        system += "\n" + _NO_WEB_INFORMATION_INSTRUCTION
    history_limit = _simple_chat.history_limit(clean_text, _PLAIN_CHAT_HISTORY_LIMIT) if plain_chat else 20
    history = _trace_call(trace, "history_load", get_history, chat_id, history_limit)
    if trace:
        trace.prompt_chars = len(system or "")
        trace.history_turns = len(history or [])

    reminder_before_count = (
        _count_pending_reminders(chat_id) if reminder_intent == "schedule" else None
    )
    if is_owner(sender):
        reply = _trace_call(
            trace,
            "model",
            get_response,
            system,
            history,
            clean_text,
            use_tools=owner_tool_path and not skip_search,
            allowed_tools=_owner_tools_without_web() if owner_tool_path and skip_search else None,
            image_path=image,
            sender=sender,
            originating_chat_id=chat_id,
            simple_chat=plain_chat,
        )
    else:
        # Intercept help / permissions requests before calling the LLM
        if detect_help_intent(clean_text) or clean_text.strip().lower() == "mypermissions":
            from .commands import _cmd_capabilities, _cmd_help, _cmd_mypermissions
            lower_clean = clean_text.lower()
            if "mypermissions" in lower_clean:
                reply_text = _cmd_mypermissions(sender)
            elif re.search(r"\bcapabilit(?:y|ies)\b", lower_clean):
                reply_text = _cmd_capabilities(sender)
            else:
                reply_text = _cmd_help(sender)
            send_message(chat_id, reply_text, is_group=True)
            return
        if not can_user_do(sender, "ask_question"):
            return
        # Non-owners get web_search only, capped at FRIEND_SEARCH_LIMIT/day
        if (
            _should_use_limited_web_tools(clean_text, skip_search)
            and get_tool_uses_today(sender, "web_search") < _FRIEND_SEARCH_LIMIT
        ):
            def _on_tool_call(tool_name: str) -> None:
                if tool_name == "web_search":
                    log_tool_use(sender, "web_search")
            reply = _trace_call(trace, "model", get_response, system, history, clean_text, allowed_tools=["web_search", "get_weather"], image_path=image, on_tool_call=_on_tool_call, sender=sender, originating_chat_id=chat_id)
        else:
            reply = _trace_call(
                trace,
                "model",
                get_response,
                system,
                history,
                clean_text,
                image_path=image,
                sender=sender,
                originating_chat_id=chat_id,
                simple_chat=plain_chat,
            )
    if image:
        _image_buffer.pop(key, None)
    if reminder_intent == "schedule":
        reply = _guard_unsaved_reminder_reply(
            reply,
            reminder_before_count,
            _count_pending_reminders(chat_id),
        )
    if not reply:
        if trace:
            trace.flag("empty_reply")
        _log_quality_signal(sender, "empty_reply", {"route": trace.route if trace else "group_llm"})
        reply = _failure_copy.UNEXPECTED_FAILURE_REPLY
    elif detect_capability_gap(reply):
        refusal = _capability_gap_refusal_reply(clean_text or "")
        if refusal:
            if trace:
                trace.flag("unsafe_capability_gap")
            _log_quality_signal(sender, "unsafe_capability_gap", {"route": trace.route if trace else "group_llm"})
            reply = refusal
        else:
            if trace:
                trace.flag("capability_gap")
            intent_summary = (clean_text or "[image]")[:80]
            log_missing_capability(sender, clean_text or "[image]", intent_summary)
            _log_quality_signal(sender, "capability_gap", {"route": trace.route if trace else "group_llm"})
            reply = f"I understood you want to '{intent_summary}' but I don't have that capability yet. the owner can build it — logging this."
    if reply:
        reply = enforce_decatur_behavior_reply(reply, active_persona, clean_text) or reply
    sent = _trace_call(trace, "send", send_message, chat_id, reply, is_group=True)
    if not sent:
        if trace:
            trace.flag("send_failed")
        _log_quality_signal(sender, "send_failed", {"route": trace.route if trace else "group_llm"})
    _trace_call(trace, "save_turn", save_turn, chat_id, "user", f"{sender}: {clean_text if not image else f'[image] {clean_text}'}")
    _trace_call(trace, "save_turn", save_turn, chat_id, "assistant", reply)
    # Only the owner's own messages can write to MEMORY.md — prevents friends/strangers
    # in a group chat from poisoning long-term memory via crafted "facts".
    if is_owner(sender):
        try:
            if plain_chat:
                if trace:
                    trace.flag("memory_extract_skipped")
            else:
                _trace_call(trace, "memory_extract", extract_and_update_memory, chat_id, clean_text, reply)
        except Exception as e:
            if trace:
                trace.flag("memory_extract_failed")
            _log_quality_signal(sender, "memory_extract_failed", {"route": trace.route if trace else "group_llm"})
            logger.warning("Memory extraction failed: %s", e)


def _recovered_message_requires_confirmation(msg: dict) -> bool:
    """Old credentials/approvals cannot authorize reconstructed pending state."""
    text = (msg.get("text") or "").strip()
    if check_admin_password(text):
        return True
    clean = strip_mention(text).strip() if is_at_mentioned(text) else text
    if _looks_like_confirmed_cleanup_run(clean):
        return True
    if re.fullmatch(r"(?:yes|yep|yeah|yup|ok(?:ay)?|sure|confirm(?:ed)?|approve(?:d)?|go ahead|do it|send it|proceed)[.!]*", clean, re.IGNORECASE):
        return True
    return bool(re.fullmatch(r"(?:log|chats)\s+.+\bconfirm[.!]*", clean, re.IGNORECASE))


def handle_message(msg: dict) -> None:
    import time as _time
    started = _time.time()
    sender = msg.get("sender", "")
    chat_id = msg.get("chat_identifier", sender)
    text = (msg.get("text") or "").strip()
    image_path = msg.get("image_path")
    trace: _MessageTrace | None = None

    if is_imessage_reaction(
        text,
        msg.get("associated_message_type"),
        msg.get("associated_message_guid"),
    ):
        return
    if not text and not image_path:
        return
    if not sender:
        return

    is_group = is_group_chat(chat_id)
    trace = _MessageTrace(
        sender=sender,
        chat_id=chat_id,
        is_group=is_group,
        text_len=len(text or ""),
        has_image=bool(image_path),
    )
    if is_group and not is_at_mentioned(text):
        if image_path:
            _buffer_unmentioned_group_image(sender, chat_id, image_path)
        return

    if not check_rate_limit(sender):
        recipient = chat_id if is_group else sender
        trace.set_route("rate_limited")
        _trace_call(
            trace,
            "send",
            send_message,
            recipient,
            "Easy there — you've hit the message limit for this hour. Try again later.",
            is_group=is_group,
        )
        return

    try:
        if is_group:
            _trace_call(trace, "dispatch", handle_group_message, sender, chat_id, text, msg=msg, trace=trace)
        else:
            _trace_call(trace, "dispatch", handle_dm, sender, text, image_path=image_path, trace=trace)
        update_heartbeat()
    except Exception as exc:
        tb_str = traceback.format_exc()
        safe_exc = redact_secret(str(exc))
        safe_tb = redact_secret(tb_str)
        logger.error("Unhandled exception processing message from %s:\n%s", sender, safe_tb)
        log_error(sender, redact_secret(text or "[image]"), type(exc).__name__, safe_exc, safe_tb)
        log_session_error(f"{type(exc).__name__}: {safe_exc[:200]}")
        error_reply = f"Hit an error on that one — {type(exc).__name__}: {safe_exc[:100]}. the owner can check the logs."
        is_group = is_group_chat(chat_id)
        recipient = chat_id if is_group else sender
        try:
            send_message(recipient, error_reply, is_group=is_group)
        except Exception:
            logger.error("Failed to send error reply to %s", recipient)
    finally:
        elapsed = _time.time() - started
        if elapsed >= SLOW_MESSAGE_LOG_SECONDS:
            if trace:
                trace.flag("slow_message")
            _log_quality_signal(
                sender,
                "slow_message",
                {"route": trace.route if trace else "unknown", "elapsed_seconds": round(elapsed, 4)},
            )
            logger.warning(
                "Slow message handling: %.2fs route=%s has_image=%s text_len=%d",
                elapsed,
                "group" if is_group else "dm",
                bool(image_path),
                len(text or ""),
            )
        _log_message_trace(trace, elapsed)


_LAST_SCHED_HEARTBEAT = 0.0
_LAST_SESSION_HEARTBEAT = 0.0
_SESSION_HEARTBEAT_INTERVAL = 60
_SCHEDULED_TASK_MAX_ATTEMPTS = 5
_SCHEDULED_TASK_RETRY_DELAY_SECONDS = 60
_SCHEDULED_ATTEMPT_RE = re.compile(r"attempt\s+(\d+)/(\d+):", re.IGNORECASE)


def _check_session_heartbeat(now: float | None = None) -> None:
    global _LAST_SESSION_HEARTBEAT
    now = time.time() if now is None else now
    if now - _LAST_SESSION_HEARTBEAT >= _SESSION_HEARTBEAT_INTERVAL:
        touch_session_heartbeat()
        _LAST_SESSION_HEARTBEAT = now


def _scheduled_task_attempt_count(error: str | None) -> int:
    match = _SCHEDULED_ATTEMPT_RE.search(error or "")
    return int(match.group(1)) if match else 0


def _scheduled_task_failure_state(error: str | None, err: str) -> tuple[str, str]:
    attempt = _scheduled_task_attempt_count(error) + 1
    safe_err = redact_secret((err or "send failed")[:180])
    status = "failed" if attempt >= _SCHEDULED_TASK_MAX_ATTEMPTS else "pending"
    return status, f"attempt {attempt}/{_SCHEDULED_TASK_MAX_ATTEMPTS}: {safe_err}"


@schedule_locked
def _check_scheduled_tasks() -> None:
    """Send any scheduled_tasks whose UTC scheduled_at has passed.

    Routes to chat_id when set (so a GC scheduling stays in the GC) — falls back
    to recipient when chat_id is NULL (legacy / direct DM).
    """
    global _LAST_SCHED_HEARTBEAT
    now = time.time()
    if now - _LAST_SCHED_HEARTBEAT > 60:
        logger.info("scheduler heartbeat — checking scheduled_tasks")
        _LAST_SCHED_HEARTBEAT = now
    try:
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            rows = conn.execute(
                """SELECT id, recipient, message, chat_id, error FROM scheduled_tasks
                   WHERE status = 'pending'
                     AND scheduled_at <= datetime('now')
                     AND (sent_at IS NULL OR sent_at <= datetime('now', ?))
                   ORDER BY scheduled_at ASC""",
                (f"-{_SCHEDULED_TASK_RETRY_DELAY_SECONDS} seconds",),
            ).fetchall()
        finally:
            conn.close()
        for task_id, recipient, message, chat_id, prev_error in rows:
            target = chat_id or recipient
            is_group = len(target) == 32 and all(c in "0123456789abcdef" for c in target.lower())
            logger.info(
                "Scheduled task #%d firing ? %s (group=%s) | %s",
                task_id, target, is_group, (message or "")[:80],
            )
            try:
                ok = send_message(target, message, is_group=is_group, recovery_mode="inline")
                status, err = ("done", None) if ok else _scheduled_task_failure_state(prev_error, "send_message returned False")
            except Exception as send_exc:
                ok = False
                status, err = _scheduled_task_failure_state(prev_error, str(send_exc))
            conn = sqlite3.connect(BOT_DB_PATH)
            try:
                conn.execute(
                    "UPDATE scheduled_tasks SET status = ?, sent_at = datetime('now'), error = ? WHERE id = ?",
                    (status, err, task_id),
                )
                conn.commit()
            finally:
                conn.close()
            if status == "pending":
                logger.warning("Scheduled task #%d send failed; will retry [%s]", task_id, err)
            else:
                logger.info("Scheduled task #%d ? %s [%s]", task_id, target, status)
    except Exception as e:
        logger.error("_check_scheduled_tasks error: %s", e)


_LAST_CRON_CHECK = 0.0


def _insert_morning_message_test_job() -> None:
    """Insert a one-time TEST_2MIN cron job if none exists yet.

    The cron runner handles firing it 2 minutes after the row's created_at,
    then disables + logs the result. Safe to call on every startup — the
    existence check prevents duplicate rows.
    """
    if not OWNER_ID:
        return
    try:
        import json as _json
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            existing = conn.execute(
                "SELECT id FROM cron_jobs WHERE cron_expression = 'TEST_2MIN'"
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO cron_jobs "
                    "(cron_expression, action_type, action_payload, enabled, created_by) "
                    "VALUES ('TEST_2MIN', 'morning_message', ?, 1, 'system')",
                    (_json.dumps({"recipient": OWNER_ID}),),
                )
                conn.commit()
                logger.info("Inserted TEST_2MIN morning message test job (fires 2 min after created_at)")
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Could not insert TEST_2MIN job: %s", e)


@schedule_locked
def _check_cron_jobs() -> None:
    """Run any enabled cron_jobs whose HH:MM matches current LA time (PST/PDT)."""
    global _LAST_CRON_CHECK
    now = time.time()
    if now - _LAST_CRON_CHECK < 50:  # roughly every minute
        return
    _LAST_CRON_CHECK = now

    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo  # stdlib, no pip dependency; DST-aware via tzdata
        _LA = ZoneInfo("America/Los_Angeles")
        now_pst = datetime.now(timezone.utc).astimezone(_LA)
        hhmm = now_pst.strftime("%H:%M")

        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            rows = conn.execute(
                "SELECT id, cron_expression, action_type, action_payload, last_run "
                "FROM cron_jobs WHERE enabled = 1"
            ).fetchall()
        finally:
            conn.close()

        import json as _json
        for job_id, expr, action_type, payload_json, last_run in rows:
            try:
                payload = _json.loads(payload_json) if payload_json else {}
            except Exception:
                payload = {}

            # -- TEST_2MIN: one-shot test job -----------------------------
            if (expr or "").strip() == "TEST_2MIN":
                if last_run is not None:
                    continue  # already ran — skip
                # Fire only after 2 minutes have elapsed since row creation
                conn = sqlite3.connect(BOT_DB_PATH)
                try:
                    ready = conn.execute(
                        "SELECT id FROM cron_jobs "
                        "WHERE id = ? AND created_at <= datetime('now', '-2 minutes')",
                        (job_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if not ready:
                    continue
                # Fire and clean up regardless of outcome
                outcome = "pass"
                err_detail = None
                try:
                    if action_type == "morning_message":
                        from .tools import _get_inspirational_quote
                        quote = _get_inspirational_quote()
                        rec = payload.get("recipient", "")
                        if rec:
                            is_group = len(rec) == 32 and all(c in "0123456789abcdef" for c in rec.lower())
                            ok = send_message(rec, quote, is_group=is_group, recovery_mode="inline")
                            if not ok:
                                outcome = "fail"
                                err_detail = "send_message returned False"
                        else:
                            outcome = "fail"
                            err_detail = "no recipient in payload"
                except Exception as test_exc:
                    outcome = "fail"
                    err_detail = str(test_exc)[:300]
                # Disable + mark ran
                conn = sqlite3.connect(BOT_DB_PATH)
                try:
                    conn.execute(
                        "UPDATE cron_jobs SET last_run = datetime('now'), enabled = 0 WHERE id = ?",
                        (job_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                # Log result to bot_log
                try:
                    conn = sqlite3.connect(BOT_DB_PATH)
                    try:
                        conn.execute(
                            "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                            ("system", "morning_message_test",
                             _json.dumps({"outcome": outcome, "error": err_detail})),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                except Exception:
                    pass
                logger.info("TEST_2MIN morning message test: %s (%s)", outcome, err_detail or "")
                continue
            # -- Normal HH:MM daily cron ----------------------------------
            # cron_expression syntax:
            #   "HH:MM"        — daily at that PT time
            #   "HH:MM mon"    — weekly on that day (mon/tue/wed/thu/fri/sat/sun)
            # Normalize "8:00" ? "08:00" so non-zero-padded jobs still match.
            parts = (expr or "").strip().split()
            target = parts[0] if parts else ""
            dow = parts[1].lower()[:3] if len(parts) > 1 else ""
            if ":" in target:
                _h, _, _m = target.partition(":")
                if _h.isdigit() and _m.isdigit():
                    target = f"{int(_h):02d}:{int(_m):02d}"
            if target != hhmm:
                continue
            if dow:
                today_dow = now_pst.strftime("%a").lower()  # 'mon', 'tue', ...
                if dow != today_dow:
                    continue
            # Avoid double-firing within the same minute. last_run is stored in UTC
            # (datetime('now') == UTC), so compare against UTC, not PST.
            now_utc_min = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            if last_run and last_run.startswith(now_utc_min):
                continue
            try:
                job_ok = True
                job_err = None
                if action_type == "morning_message":
                    from .tools import _get_inspirational_quote, _render_morning_message_body
                    quote = _get_inspirational_quote()
                    body = _render_morning_message_body(payload, quote, now_pt=now_pst)
                    rec = payload.get("recipient", "")
                    if not rec:
                        job_ok = False
                        job_err = "no recipient in payload"
                        logger.error("Cron job #%d: missing recipient in payload", job_id)
                    else:
                        is_group = len(rec) == 32 and all(c in "0123456789abcdef" for c in rec.lower())
                        logger.info(
                            "Cron job #%d firing ? %s (group=%s) | %s",
                            job_id, rec, is_group, (body or "")[:80],
                        )
                        ok = send_message(rec, body, is_group=is_group, recovery_mode="inline")
                        if not ok:
                            job_ok = False
                            job_err = "send_message returned False"
                elif action_type == "drift_check":
                    # Run the same drift report owner sees on `drift`, send to recipient.
                    from .commands import _cmd_drift
                    rec = payload.get("recipient", "")
                    if not rec:
                        job_ok = False
                        job_err = "no recipient in payload"
                    else:
                        report = _cmd_drift(OWNER_ID)  # owner perm gate: report runs as owner
                        is_group = len(rec) == 32 and all(c in "0123456789abcdef" for c in rec.lower())
                        logger.info("Cron job #%d drift_check ? %s (group=%s)", job_id, rec, is_group)
                        ok = send_message(rec, f"Weekly drift check\n\n{report}", is_group=is_group, recovery_mode="inline")
                        if not ok:
                            job_ok = False
                            job_err = "send_message returned False"
                elif action_type == "sports_recap":
                    from .tools import _get_sports_recap
                    rec = payload.get("recipient", "")
                    if not rec:
                        job_ok = False
                        job_err = "no recipient in payload"
                    else:
                        report = _get_sports_recap(now_pt=now_pst)
                        is_group = len(rec) == 32 and all(c in "0123456789abcdef" for c in rec.lower())
                        logger.info("Cron job #%d sports_recap -> %s (group=%s)", job_id, rec, is_group)
                        ok = send_message(rec, report, is_group=is_group, recovery_mode="inline")
                        if not ok:
                            job_ok = False
                            job_err = "send_message returned False"
                else:
                    job_ok = False
                    job_err = f"unknown action_type {action_type!r}"
                if job_ok:
                    conn = sqlite3.connect(BOT_DB_PATH)
                    try:
                        conn.execute(
                            "UPDATE cron_jobs SET last_run = datetime('now') WHERE id = ?",
                            (job_id,),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    logger.info("Cron job #%d (%s) ran OK", job_id, action_type)
                else:
                    logger.error(
                        "Cron job #%d (%s) did not complete; last_run not advanced: %s",
                        job_id, action_type, job_err,
                    )
            except Exception as job_exc:
                logger.error("Cron job #%d failed: %s", job_id, job_exc)
    except Exception as e:
        logger.error("_check_cron_jobs error: %s", e)


@schedule_locked
def _check_reminders() -> None:
    try:
        due = get_due_reminders()
    except Exception as e:
        logger.error("get_due_reminders failed: %s", e)
        return

    for reminder in due:
        try:
            target = reminder.get("origin_chat_id") or reminder.get("chat_id") or ""
            if not isinstance(target, str) or not target:
                attempts = int(reminder.get("send_attempts", 0) or 0) + 1
                logger.error(
                    "Reminder #%s has no valid target (attempt %d); will retry after cooldown",
                    reminder.get("id"),
                    attempts,
                )
                _bump_reminder_attempts(reminder["id"], attempts)
                continue
            is_group = len(target) == 32 and all(c in "0123456789abcdef" for c in target.lower())
            logger.info(
                "Reminder #%d firing ? %s (group=%s) | %s",
                reminder["id"], target, is_group, reminder["message"][:60],
            )
            ok = send_message(target, f"Reminder: {reminder['message']}", is_group=is_group, recovery_mode="inline")
            if ok:
                mark_reminder_sent(reminder["id"])
                logger.info("Reminder #%d sent to %s", reminder["id"], target)
            else:
                attempts = int(reminder.get("send_attempts", 0) or 0) + 1
                _bump_reminder_attempts(reminder["id"], attempts)
                logger.error(
                    "Reminder #%d send FAILED (attempt %d) -> %s; will retry after cooldown",
                    reminder["id"],
                    attempts,
                    target,
                )
        except Exception as e:
            logger.error("Reminder #%s tick error: %s", reminder.get("id"), e)


def _send_owner_market_alert(message: str) -> bool:
    if not OWNER_ID:
        return False
    return bool(send_message(OWNER_ID, message, recovery_mode="inline"))


def main() -> None:
    global SESSION_ID, _LAST_MAIN_LOOP_ALERT
    logger.info("DavosBot starting up")
    logger.info("Owner handle (normalized): %s", OWNER_ID or "<NOT SET>")
    os.makedirs(PROJECT_ROOT / "logs", exist_ok=True)
    cleanup_old_backups()
    init_db()
    normalize_approved_users()
    audit_group_chats()
    cleanup_rate_limit_log()
    SESSION_ID = start_session()
    inbox = MessageInbox(DB_PATH, BOT_DB_PATH, migrate=run_migration,
                         confirmation_guard=_recovered_message_requires_confirmation,
                         session_id=SESSION_ID, normalize_sender=normalize_handle)
    initialize_ollama_recovery_state()
    start_ollama_keep_warm_thread()
    start_market_tracker(_send_owner_market_alert)
    start_package_delivery_monitor()
    start_work_bridge()
    _insert_morning_message_test_job()

    try:
        soul_content = read_soul()
        if not soul_content.strip():
            raise ValueError("empty")
    except (FileNotFoundError, ValueError):
        restored = restore_soul_from_latest_backup()
        if restored:
            msg = f"SOUL.md was empty/missing — restored from {restored}"
            print(msg, file=sys.stderr)
            logger.info(msg)
        else:
            if load_soul().strip():
                logger.warning(
                    "SOUL.md is missing/empty and no backup exists; using fallback personality for this session"
                )
            else:
                logger.error("SOUL.md is missing/empty and no fallback personality is available")

    personality_errors = validate_personality_files()
    if personality_errors:
        for err in personality_errors:
            print(f"PERSONALITY VALIDATION FAILED: {err}", file=sys.stderr)
            logger.warning("Personality validation failed: %s", err)
    else:
        logger.info("Personality validation: all files OK")
    log_startup_event("startup_validation", {
        "passed": not personality_errors,
        "errors": personality_errors,
    })
    if personality_errors:
        send_owner_alert(
            "startup_validation_failed",
            "DavosBot startup personality validation failed.",
            {"error_count": len(personality_errors), "errors": personality_errors[:5]},
        )

    workers = InboxWorkers(inbox, handle_message)
    workers.start()
    try:
        _run_main_loop(inbox, workers)
    finally:
        remaining = workers.stop(timeout=1.0)
        logger.info("Inbox workers stopping; handlers still running=%d", remaining)


def _run_main_loop(inbox, workers) -> None:
    global _LAST_MAIN_LOOP_ALERT
    while True:
        try:
            workers.raise_if_failed()
            intake_error = None
            try:
                inbox.poll()
                workers.wake()
            except Exception as error:
                intake_error = error
            _check_reminders()
            _check_scheduled_tasks()
            _check_cron_jobs()
            check_ollama_recovery()
            _check_session_heartbeat()
            workers.raise_if_failed()
            if intake_error is not None:
                raise intake_error
        except KeyboardInterrupt:
            logger.info("Shutting down")
            break
        except InboxWorkerError:
            logger.error("Inbox handler worker stopped; exiting for supervisor recovery")
            raise
        except Exception as e:
            safe_tb = redact_secret(traceback.format_exc())
            logger.error("Main loop error:\n%s", safe_tb)
            now = time.time()
            if now - _LAST_MAIN_LOOP_ALERT >= _MAIN_LOOP_ALERT_INTERVAL:
                _LAST_MAIN_LOOP_ALERT = now
                send_owner_alert(
                    "main_loop_error",
                    "DavosBot main loop error.",
                    {"error": safe_tb[-1500:]},
                )

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
