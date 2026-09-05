import csv
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .runtime_locks import PERSONALITY_FILE_LOCK, SCHEDULE_LOCK, schedule_locked
from .config import GEMINI_API_KEY, GEMINI_REWRITE_MODEL, TAVILY_API_KEY, BOT_DB_PATH, GENERATED_DIR, DB_PATH, PROJECT_ROOT
from .billing import check_gemini_budget
from .tool_outcomes import ToolOutcome, as_outcome
from .db import connect_bot_db
from .change_request_tools import (
    _tool_change_request_preview,
    _tool_change_request_risk,
)
from . import web_search as _web_search_helpers
from . import workouts as _workout_helpers
from . import morning_quotes as _morning_quote_helpers
from .reminder_tools import (
    _cancel_reminder,
    _cancel_reminders,
    _humanize_due,
    _list_reminders,
    _reminder_delivery_suffix,
    _set_reminder,
    _visible_reminder_rows,
)
from .weather import _get_weather

logger = logging.getLogger(__name__)
_FALLBACK_QUOTES = _morning_quote_helpers._FALLBACK_QUOTES
_morning_quote_mentions_date = _morning_quote_helpers._morning_quote_mentions_date
_quote_hash = _morning_quote_helpers._quote_hash
_ROTATING_MORNING_INTROS = _morning_quote_helpers._ROTATING_MORNING_INTROS

_GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_REWRITE_MODEL}:generateContent"
_PROJECT_DIR = str(PROJECT_ROOT)


def _utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def _log_gemini_usage(prompt_tokens: int, candidates_tokens: int, total_tokens: int, source: str) -> None:
    try:
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            conn.execute(
                "INSERT INTO gemini_usage (prompt_tokens, candidates_tokens, total_tokens, source) VALUES (?,?,?,?)",
                (prompt_tokens, candidates_tokens, total_tokens, source),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to log Gemini usage from tools.%s: %s", source, e)

TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": "Search the web for current information — sports standings, scores, odds, news, UFC fight cards, anything that changes over time. Use this whenever asked about live or recent facts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_weather",
        "description": "Get live current weather for a location. Use for weather, temperature, forecast, Seattle, Belltown, or location-weather questions. Defaults to Belltown, Seattle if no location is provided.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City/neighborhood, e.g. Seattle, Belltown, Indianapolis"}
            }
        }
    },
    {
        "name": "read_file",
        "description": "Read the contents of any file on the Mac Mini.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (use ~ for home directory)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to a file on the Mac Mini. Use for editing code, config files, or creating new files. Python files are syntax-checked before writing.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write to the file"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "shell_exec",
        "description": "Execute a shell command on the Mac Mini. Use for running scripts, checking logs, restarting services, git operations, installing packages.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout seconds (default 30)"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "sqlite_query",
        "description": "Run a SQL query against a SQLite database. Use for reading or writing conversation history, workout logs, or any bot data.",
        "parameters": {
            "type": "object",
            "properties": {
                "db_path": {"type": "string", "description": "Path to SQLite database"},
                "query": {"type": "string", "description": "SQL query"},
                "params": {"type": "array", "items": {"type": "string"}, "description": "Query parameters"}
            },
            "required": ["db_path", "query"]
        }
    },
    {
        "name": "edit_persona",
        "description": "Edit an existing persona to change how it behaves. Use when asked to make a persona more/less of something, or change its style.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Persona name (gruden, jarjar, babylu, or 'soul' for the default personality)"},
                "instruction": {"type": "string", "description": "What to change about the persona"}
            },
            "required": ["name", "instruction"]
        }
    },
    {
        "name": "create_persona",
        "description": "Create a brand new persona from a description. Use when asked to add a new personality.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name for the new persona"},
                "description": {"type": "string", "description": "Full description of the personality and style"}
            },
            "required": ["name", "description"]
        }
    },
    {
        "name": "generate_file",
        "description": "Generate a spreadsheet, CSV, or text file and send it via iMessage.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Filename with extension (e.g. pnl.csv, fights.csv)"},
                "file_type": {"type": "string", "enum": ["csv", "txt", "md"], "description": "File type"},
                "content": {"type": "string", "description": "File content — for csv, use comma-separated rows with newlines; for txt/md, plain text"},
                "recipient": {"type": "string", "description": "iMessage recipient (phone number or chat ID)"},
                "is_group": {"type": "boolean", "description": "True if sending to a group chat"}
            },
            "required": ["filename", "file_type", "content", "recipient"]
        }
    },
    {
        "name": "log_workout",
        "description": "Log a workout exercise. Use when told about a workout, sets, reps, or weight.",
        "parameters": {
            "type": "object",
            "properties": {
                "exercise": {"type": "string", "description": "Exercise name"},
                "sets": {"type": "integer", "description": "Number of sets"},
                "reps": {"type": "integer", "description": "Reps per set"},
                "weight_lbs": {"type": "number", "description": "Weight in lbs (0 for bodyweight)"},
                "notes": {"type": "string", "description": "Optional notes"}
            },
            "required": ["exercise"]
        }
    },
    {
        "name": "log_change_request",
        "description": "Save a large, complex, setup, or repair request to the change log as a guarded Codex handoff. Use when the right answer is a planned branch/test/push/CI/deploy workflow rather than immediate live mutation. Never frame size as impossible; log the handoff and explain the safe next step.",
        "parameters": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "What was requested"},
                "reason": {"type": "string", "description": "Why this needs the guarded Codex pipeline, what context is known, and what expected behavior should be"}
            },
            "required": ["request"]
        }
    },
    {
        "name": "set_reminder",
        "description": "Set a reminder. Convert ALL times to UTC in 'YYYY-MM-DD HH:MM:SS' format using the CURRENT TIME from the system prompt. Routing is automatic — do NOT supply a chat_id, recipient, or any routing field; the system fills it from context. Never mention internal reminder IDs/numbers to the user — the tool returns a friendly confirmation; pass it through verbatim.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to remind about"},
                "due_ts": {"type": "string", "description": "When, in UTC: YYYY-MM-DD HH:MM:SS"}
            },
            "required": ["message", "due_ts"]
        }
    },
    {
        "name": "list_reminders",
        "description": "List pending reminders for the current chat. No arguments needed — system uses context.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by its POSITION in the user's list (1 = soonest, 2 = next, etc). If unsure which one the user means, call list_reminders first. Never mention internal IDs to the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "position": {"type": "integer", "description": "1-based position in the pending list (1 = soonest)"}
            },
            "required": ["position"]
        }
    },
    {
        "name": "schedule_cron",
        "description": "Schedule a recurring message in the CURRENT chat (DM or GC). Default = daily inspirational quote (action='morning_message'). For a weekly bot health report, use action='drift_check'. Use when the user says 'every morning at 6:30', 'daily at 9am', 'every Monday at 9am drift check', etc. Time is Pacific. Routing is automatic — never supply recipient. Owner-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "time_pt": {"type": "string", "description": "Fire time in Pacific, 24-hour HH:MM (e.g. '06:30', '21:00')"},
                "action": {"type": "string", "enum": ["morning_message", "drift_check", "sports_recap"], "description": "morning_message = inspirational quote (default). drift_check = weekly bot health report (cron status, recent errors, queues). sports_recap = daily ESPN-based sports recap for this chat."},
                "day_of_week": {"type": "string", "enum": ["mon","tue","wed","thu","fri","sat","sun"], "description": "Optional. If set, fires WEEKLY on that day. If omitted, fires DAILY."},
                "intro": {"type": "string", "description": "Optional opening line for morning_message only (ignored for drift_check). E.g. 'good morning boys!'."},
                "intro_mode": {"type": "string", "enum": ["fixed", "rotate"], "description": "rotate = vary the morning greeting daily instead of using one fixed intro."}
            },
            "required": ["time_pt"]
        }
    },
    {
        "name": "list_crons",
        "description": "List recurring daily jobs. Default is the CURRENT chat. Use scope='all' only when the owner asks for all jobs across chats.",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["current", "all", "mine", "direct", "groups"], "description": "current = this chat only; all = every active cron; mine = owner DM only; direct = all 1:1/DM cron jobs; groups = all group-chat cron jobs. Owner-only except current."}
            }
        }
    },
    {
        "name": "cancel_cron",
        "description": "Cancel a recurring daily job. Prefer cron_id when the owner names a stable ID from list_crons scope='all'; otherwise use POSITION in the current chat's list.",
        "parameters": {
            "type": "object",
            "properties": {
                "position": {"type": "integer", "description": "1-based position in this chat's cron list"},
                "cron_id": {"type": "integer", "description": "Stable cron_jobs.id from list_crons scope='all'"}
            }
        }
    },
    {
        "name": "edit_cron",
        "description": "Edit an existing recurring cron job by stable cron_id from list_crons. Use for changing time, day, action, greeting/intro, or rotating the greeting. Do not use log_change_request for cron edits. Owner-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "cron_id": {"type": "integer", "description": "Stable cron_jobs.id from list_crons scope='all'"},
                "time_pt": {"type": "string", "description": "Optional new Pacific fire time, HH:MM or casual like '6:30am'"},
                "day_of_week": {"type": "string", "enum": ["mon","tue","wed","thu","fri","sat","sun"], "description": "Optional new weekly day. Omit to keep existing cadence."},
                "action": {"type": "string", "enum": ["morning_message", "drift_check", "sports_recap"], "description": "Optional new action. Omit to keep existing action."},
                "intro": {"type": "string", "description": "Optional new morning_message intro/greeting."},
                "intro_mode": {"type": "string", "enum": ["fixed", "rotate", "clear"], "description": "rotate = vary the morning greeting daily; clear = remove intro; fixed = use intro. Omit to keep existing intro mode."}
            },
            "required": ["cron_id"]
        }
    },
    {
        "name": "get_group_chat_status",
        "description": "Show which group chats have the bot enabled, their names or members, and active personas. Use when asked anything like 'what chats are on', 'show my group chats', 'which groups are active', 'list chats', etc.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "list_chats",
        "description": "List all chats with recent conversation history, showing chat IDs and recent messages so you can identify which chat the user is referring to (e.g. 'the group chat with X', 'the xyz chat').",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max number of chats to show (default 10)"}
            },
            "required": []
        }
    },
    {
        "name": "clear_chat_history",
        "description": "Clear conversation history for a specific chat. Use when asked to delete/clear/forget recent messages. First use list_chats if you need to identify the chat ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "The chat ID or sender identifier to clear history for"},
                "mode": {"type": "string", "enum": ["all", "minutes", "count"], "description": "all=clear everything, minutes=clear last N minutes, count=clear last N messages"},
                "value": {"type": "integer", "description": "For minutes/count mode: number of minutes or messages"}
            },
            "required": ["chat_id", "mode"]
        }
    },
    {
        "name": "query_workout",
        "description": "Look up workout history and stats.",
        "parameters": {
            "type": "object",
            "properties": {
                "query_type": {"type": "string", "enum": ["recent", "exercise", "summary"], "description": "recent=last sessions, exercise=history for one exercise, summary=overall stats"},
                "exercise": {"type": "string", "description": "Exercise name (for exercise query type)"},
                "days": {"type": "integer", "description": "Days to look back (default 30)"}
            },
            "required": ["query_type"]
        }
    },
    {
        "name": "bet_log",
        "description": "Log a new sports bet. Use when user provides event, odds, and stake.",
        "parameters": {
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "What the bet is on, e.g. 'Lakers' or 'Patriots -3.5'"},
                "odds": {"type": "integer", "description": "Odds as integer e.g. -110 or +200"},
                "stake": {"type": "number", "description": "Stake in units e.g. 1.5"},
                "bet_type": {"type": "string", "description": "moneyline, spread, parlay, or other"},
                "notes": {"type": "string", "description": "Optional notes"}
            },
            "required": ["event", "odds", "stake"]
        }
    },
    {
        "name": "bet_settle",
        "description": "Settle a pending sports bet as win, loss, or push.",
        "parameters": {
            "type": "object",
            "properties": {
                "result": {"type": "string", "enum": ["win", "loss", "push"], "description": "Outcome of the bet"},
                "bet_id": {"type": "integer", "description": "Optional specific bet ID to settle"},
                "event": {"type": "string", "description": "Optional event name to match if no bet_id"}
            },
            "required": ["result"]
        }
    },
    {
        "name": "bet_stats",
        "description": "Return bet stats for a user or the group. Use for 'how am I doing', 'group p&l', 'my stats'.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Sender handle, 'group', or '@name'. Default: current sender."},
                "timeframe": {"type": "string", "description": "today, this week, last 7 days, this month. Default: this week."}
            },
            "required": []
        }
    },
    {
        "name": "workout_log",
        "description": "Log a workout set from natural language input. Use when owner describes an exercise with weight and reps, e.g. 'bench 185 x 5 x 3' or 'squats 225 5x5 felt heavy'.",
        "parameters": {
            "type": "object",
            "properties": {
                "exercise_name": {"type": "string", "description": "Name of the exercise"},
                "sets": {"type": "array", "items": {"type": "object", "properties": {"weight": {"type": "number"}, "reps": {"type": "integer"}}}, "description": "Array of {weight, reps} objects. weight=0 for bodyweight."},
                "muscle_group": {"type": "string", "description": "Optional: chest, back, legs, shoulders, arms, core, cardio, other"},
                "notes": {"type": "string", "description": "Optional fatigue, difficulty, or other notes"}
            },
            "required": ["exercise_name", "sets"]
        }
    },
    {
        "name": "create_skill",
        "description": "Create a new bot skill — a custom trigger phrase that maps to a fixed response. Admin and owner only. Use when owner says 'create a skill that does X when I say Y'.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "Short identifier for the skill, e.g. 'weather'"},
                "trigger_phrase": {"type": "string", "description": "The phrase that activates this skill, e.g. 'weather in'"},
                "response_template": {"type": "string", "description": "What the bot responds. Use {input} as a placeholder for the matched message text."}
            },
            "required": ["skill_name", "trigger_phrase", "response_template"]
        }
    },
    {
        "name": "get_inspirational_quote",
        "description": "Return one short original inspirational quote (used by the daily morning message cron).",
        "parameters": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "send_imessage",
        "description": "Prepare a private 1:1 iMessage confirmation for the owner only when the user explicitly asks to text/DM/message someone privately. This tool never sends immediately; the user must confirm with the admin password in a later message. Do NOT use this for '@Davos tell NAME ...' in a group chat; that means reply in the group. Recipient can be a name ('Cole'), E.164 phone (<phone>), or Apple ID email. Names are resolved automatically via contacts. Convert relative times like '8pm tonight' to ISO 8601 UTC before calling.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Contact name (e.g. 'Cole'), E.164 phone number (e.g. <phone>), or Apple ID email. Names are resolved automatically."},
                "message": {"type": "string", "description": "The message text to send"},
                "scheduled_time_utc": {"type": "string", "description": "Optional ISO 8601 UTC datetime to schedule delivery (YYYY-MM-DDTHH:MM:SS). If omitted, sends immediately."}
            },
            "required": ["recipient", "message"]
        }
    }
]


_OWNER_ONLY_TOOLS = frozenset({
    "write_file", "shell_exec", "sqlite_query", "edit_persona", "create_persona",
    "read_file", "generate_file", "log_change_request", "clear_chat_history",
    "get_group_chat_status", "list_chats",
    "schedule_cron", "list_crons", "cancel_cron", "edit_cron", "send_imessage",
})


def execute_tool(name: str, args: dict, sender: str = "", originating_chat_id: str = "") -> str:
    """Compatibility boundary for existing callers that consume plain text."""
    return execute_tool_outcome(name, args, sender=sender, originating_chat_id=originating_chat_id).text


def execute_tool_outcome(name: str, args: dict, sender: str = "", originating_chat_id: str = "") -> ToolOutcome:
    return as_outcome(_execute_tool(name, args, sender=sender, originating_chat_id=originating_chat_id))


def _execute_tool(name: str, args: dict, sender: str = "", originating_chat_id: str = "") -> str | ToolOutcome:
    from .permissions import is_owner
    log_name = name if any(tool["name"] == name for tool in TOOL_DEFINITIONS) else "unknown_tool"
    if name in _OWNER_ONLY_TOOLS and not is_owner(sender):
        logger.warning("Non-owner attempted to call owner-only tool %s — blocked", log_name)
        return ToolOutcome("denied", f"Permission denied — {name} is restricted to the owner.", "authorization", error="owner_required")
    logger.info("Executing tool: %s metadata: %s", log_name, _safe_tool_args_for_log(name, args))
    try:
        if name == "web_search":
            return _web_search(args["query"])
        elif name == "get_weather":
            return _get_weather(args.get("location", ""))
        elif name == "read_file":
            return _read_file(args["path"])
        elif name == "write_file":
            return _write_file(args["path"], args["content"])
        elif name == "shell_exec":
            return _shell_exec_outcome(args["command"], int(args.get("timeout", 30)))
        elif name == "sqlite_query":
            return _sqlite_query(args["db_path"], args["query"], args.get("params", []))
        elif name == "edit_persona":
            return _edit_persona(args["name"], args["instruction"])
        elif name == "create_persona":
            return _create_persona(args["name"], args["description"])
        elif name == "generate_file":
            return _generate_file(
                args["filename"], args["file_type"], args["content"],
                args["recipient"], bool(args.get("is_group", False))
            )
        elif name == "bet_log":
            from .commands import _cmd_bet_log
            return _cmd_bet_log(f"/bet log {args.get('event','')} {args.get('odds',0):+d} {args.get('stake',1)}u {args.get('notes','')}", sender)
        elif name == "bet_settle":
            from .commands import _cmd_bet_settle
            result = args.get("result", "")
            bid = args.get("bet_id", "")
            return _cmd_bet_settle(f"/bet settle {bid} {result}".strip(), sender)
        elif name == "bet_stats":
            from .commands import _cmd_bet_stats
            target = args.get("target", sender)
            timeframe = args.get("timeframe", "this week")
            return _cmd_bet_stats(f"/bet stats {target} {timeframe}", sender)
        elif name == "workout_log":
            return _workout_log_tool(args, sender)
        elif name == "log_workout":
            return _log_workout(args, sender)
        elif name == "query_workout":
            return _query_workout(args, sender)
        elif name == "log_change_request":
            return _log_change_request(args["request"], args.get("reason", ""))
        elif name == "set_reminder":
            # Routing is controlled by originating_chat_id; chat_id from the LLM is ignored.
            return _set_reminder(
                args["message"], args["due_ts"],
                originating_chat_id=originating_chat_id,
            )
        elif name == "list_reminders":
            # Ignore any chat_id the LLM tries to pass; always scope to the originating chat.
            return _list_reminders(originating_chat_id)
        elif name == "cancel_reminder":
            # Accept either new "position" or legacy "reminder_id" (Gemini sometimes
            # falls back to old schema names). Both are treated as 1-based position.
            pos = args.get("position", args.get("reminder_id", 0))
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                pos = 0
            return _cancel_reminder(pos, originating_chat_id=originating_chat_id)
        elif name == "schedule_cron":
            return _schedule_cron(
                args.get("time_pt", ""),
                args.get("intro", ""),
                action=args.get("action", "morning_message"),
                day_of_week=args.get("day_of_week", ""),
                intro_mode=args.get("intro_mode", ""),
                originating_chat_id=originating_chat_id,
            )
        elif name == "list_crons":
            return _list_crons(
                originating_chat_id,
                include_all=args.get("scope") == "all",
                scope=args.get("scope", "current"),
                requester_id=sender,
            )
        elif name == "cancel_cron":
            cron_id = args.get("cron_id")
            if cron_id:
                return _cancel_cron_by_id(int(cron_id), sender=sender)
            try:
                pos = int(args.get("position", 0))
            except (TypeError, ValueError):
                pos = 0
            return _cancel_cron(pos, originating_chat_id=originating_chat_id)
        elif name == "edit_cron":
            return _edit_cron(
                int(args.get("cron_id", 0)),
                sender=sender,
                time_pt=args.get("time_pt", ""),
                day_of_week=args.get("day_of_week", None),
                action=args.get("action", ""),
                intro=args.get("intro", None),
                intro_mode=args.get("intro_mode", ""),
            )
        elif name == "get_group_chat_status":
            from .commands import _cmd_chats
            from .config import OWNER_ID
            return _cmd_chats(OWNER_ID)  # tool is only reachable via owner-gated LLM calls
        elif name == "list_chats":
            return _list_chats(int(args.get("limit", 10)))
        elif name == "clear_chat_history":
            return _clear_chat_history(args["chat_id"], args["mode"], int(args.get("value", 0)))
        elif name == "send_imessage":
            return _send_imessage(
                args.get("recipient", ""),
                args["message"],
                args.get("scheduled_time_utc", ""),
                sender=sender,
                originating_chat_id=originating_chat_id,
            )
        elif name == "get_inspirational_quote":
            return _get_inspirational_quote()
        elif name == "create_skill":
            from .commands import create_skill
            return create_skill(
                sender,
                args.get("skill_name", ""),
                args.get("trigger_phrase", ""),
                args.get("response_template", ""),
            )
        else:
            return ToolOutcome("failed", f"Unknown tool: {name}", "dispatch", error="unknown_tool")
    except Exception as e:
        logger.error("Tool %s error type: %s", log_name, type(e).__name__)
        return ToolOutcome("unverified", f"Tool error: {e}", "execution_exception", error=type(e).__name__)


def _web_search(query: str) -> str:
    return _web_search_helpers._web_search(query, api_key=TAVILY_API_KEY, requests_module=requests)


def _read_file(path: str) -> str:
    path = os.path.expanduser(path)
    with PERSONALITY_FILE_LOCK, open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_file(path: str, content: str) -> str:
    path = os.path.expanduser(path)

    if path.endswith(".py"):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ["python3", "-m", "py_compile", tmp_path],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                os.unlink(tmp_path)
                return f"Syntax error — file NOT written:\n{result.stderr}"
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with PERSONALITY_FILE_LOCK, open(path, "w", encoding="utf-8") as f:
        f.write(content)

    if path.startswith(_PROJECT_DIR):
        logger.warning("Project file written without auto-push: %s", path)
        return (
            f"Written: {path}\n"
            "Auto-push is disabled for safety. Review, commit, and deploy from the normal repo workflow."
        )

    return f"Written: {path}"


def _auto_push(changed_path: str) -> None:
    rel = os.path.relpath(changed_path, _PROJECT_DIR)
    logger.warning("Auto-push is disabled for safety; %s needs normal review/deploy workflow", rel)


def _manual_persona_review_notice(action: str, name: str) -> str:
    return (
        f"{action} '{name}' persona locally.\n"
        "Auto-push is disabled for safety. Review, commit, and deploy from the normal repo workflow."
    )


def _shell_exec(command: str, timeout: int = 30) -> str:
    return _shell_exec_outcome(command, timeout).text


def _shell_exec_outcome(command: str, timeout: int = 30) -> ToolOutcome:
    if "pm2 restart" in command:
        subprocess.Popen(["bash", "-c", f"sleep 2 && {command}"])
        return ToolOutcome("pending", f"Scheduled: {command} (running in 2s)", "process_started")

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=_PROJECT_DIR
        )
    except subprocess.TimeoutExpired as exc:
        return ToolOutcome("unverified", f"Tool error: {exc}", "process_timeout", error="timeout")
    out = (result.stdout + result.stderr).strip()
    out = out[-3000:] if len(out) > 3000 else out or "(no output)"
    if result.returncode:
        return ToolOutcome("failed", f"Command exited with status {result.returncode}.\n{out}",
                           "process_exit", exit_code=result.returncode, error="nonzero_exit")
    return ToolOutcome("confirmed", out, "process_exit", exit_code=0)


def _sqlite_query(db_path: str, query: str, params: list = []) -> str:
    db_path = os.path.expanduser(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        if cursor.description:
            rows = cursor.fetchall()
            return json.dumps([dict(r) for r in rows], default=str)
        return "OK"


def _gemini_rewrite(prompt: str) -> str:
    budget = check_gemini_budget("tool_rewrite")
    if not budget.allowed:
        logger.warning("Gemini rewrite blocked by budget guard: %s", budget.reason)
        raise RuntimeError(budget.reason)

    resp = requests.post(
        _GEMINI_URL,
        params={"key": GEMINI_API_KEY},
        json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usageMetadata", {})
    _log_gemini_usage(
        usage.get("promptTokenCount", 0),
        usage.get("candidatesTokenCount", 0),
        usage.get("totalTokenCount", 0),
        "tool_rewrite",
    )
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _edit_persona(name: str, instruction: str) -> str:
    is_soul = name.lower() == "soul"
    path = (
        Path(_PROJECT_DIR) / "SOUL.md"
        if is_soul
        else Path(_PROJECT_DIR) / "personalities" / f"{name.lower()}.md"
    )

    if not path.exists():
        return f"Persona '{name}' not found."

    with PERSONALITY_FILE_LOCK:
        current = path.read_text(encoding="utf-8")
    new_content = _gemini_rewrite(
        f"Update this persona/personality file based on this instruction: '{instruction}'\n\n"
        f"Current file:\n{current}\n\n"
        f"Return ONLY the updated file content, nothing else."
    )

    with PERSONALITY_FILE_LOCK:
        if not path.exists() or path.read_text(encoding="utf-8") != current:
            return "Persona changed while I was preparing the edit. I kept the newer file; review it before trying again."
        if is_soul:
            from .soul import write_soul
            from .config import OWNER_ID
            write_soul(new_content, f"edit via tool: {instruction[:80]}", sender=OWNER_ID)
        else:
            path.write_text(new_content, encoding="utf-8")

    return _manual_persona_review_notice("Updated", name)


def _create_persona(name: str, description: str) -> str:
    path = Path(_PROJECT_DIR) / "personalities" / f"{name.lower()}.md"
    with PERSONALITY_FILE_LOCK:
        expected = path.read_text(encoding="utf-8") if path.exists() else None
    content = _gemini_rewrite(
        f"Create a persona .md file for an AI assistant with this personality:\n{description}\n\n"
        f"Name: {name}\n\n"
        f"Format it like a character description with ## Personality and ## Style sections. "
        f"Return ONLY the file content."
    )
    with PERSONALITY_FILE_LOCK:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != expected:
            return "Persona changed while I was preparing it. I kept the newer file; review it before trying again."
        path.write_text(content, encoding="utf-8")
    return (
        _manual_persona_review_notice("Created", name)
        + f"\nSwitch to it with: persona {name.lower()}"
    )


def _generate_file(filename: str, file_type: str, content: str, recipient: str, is_group: bool = False) -> str:
    from .imessage import send_file

    os.makedirs(GENERATED_DIR, exist_ok=True)
    out_path = os.path.join(GENERATED_DIR, filename)

    if file_type == "csv":
        rows = [line.split(",") for line in content.strip().splitlines()]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)

    success = send_file(recipient, out_path, is_group=is_group)
    return f"Sent {filename}." if success else f"File created at {out_path} but send failed."


def _log_change_request(request: str, reason: str = "") -> str:
    from .change_request_tools import _log_change_request as _write_change_request
    return _write_change_request(request, reason, db_path=BOT_DB_PATH)


# --- Cron jobs (recurring daily messages) ---------------------------------

def _normalize_hhmm(s: str) -> str | None:
    """Accept '6:30', '06:30', '6:30am', '6:30 PM', '21:00' ? 'HH:MM' 24-hour PT."""
    if not s:
        return None
    raw = s.strip().lower().replace(".", "")
    ampm = None
    if raw.endswith("am") or raw.endswith("pm"):
        ampm = raw[-2:]
        raw = raw[:-2].strip()
    if ":" not in raw:
        # bare "6" or "21" ? assume HH
        if raw.isdigit():
            raw = f"{raw}:00"
        else:
            return None
    h, _, m = raw.partition(":")
    if not (h.isdigit() and m.isdigit()):
        return None
    hh, mm = int(h), int(m)
    if ampm and not 1 <= hh <= 12:
        return None
    if ampm == "pm" and hh < 12:
        hh += 12
    if ampm == "am" and hh == 12:
        hh = 0
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return f"{hh:02d}:{mm:02d}"


_VALID_ACTIONS = {"morning_message", "drift_check", "sports_recap"}
_VALID_DOW = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_DOW_WORDS = {
    "monday": "mon", "mon": "mon",
    "tuesday": "tue", "tues": "tue", "tue": "tue",
    "wednesday": "wed", "weds": "wed", "wed": "wed",
    "thursday": "thu", "thurs": "thu", "thu": "thu",
    "friday": "fri", "fri": "fri",
    "saturday": "sat", "sat": "sat",
    "sunday": "sun", "sun": "sun",
}
@schedule_locked
def _schedule_cron(
    time_pt: str,
    intro: str = "",
    action: str = "morning_message",
    day_of_week: str = "",
    intro_mode: str = "",
    originating_chat_id: str = "",
) -> str:
    if not originating_chat_id:
        return "No chat context — ask from a DM or GC."
    hhmm = _normalize_hhmm(time_pt)
    if not hhmm:
        return f"Couldn't parse '{time_pt}' as a time. Try '6:30' or '9pm'."
    if action not in _VALID_ACTIONS:
        return f"Unknown action '{action}'. Try: morning_message, drift_check, or sports_recap."
    dow = (day_of_week or "").strip().lower()[:3]
    if dow and dow not in _VALID_DOW:
        return f"Unknown day '{day_of_week}'. Try: mon, tue, wed, thu, fri, sat, sun."
    expr = f"{hhmm} {dow}" if dow else hhmm

    payload = {"recipient": originating_chat_id}
    if action == "morning_message" and (intro_mode or "").strip().lower() == "rotate":
        payload["intro_mode"] = "rotate"
    elif action == "morning_message" and intro and intro.strip():
        payload["intro"] = intro.strip()
        payload["intro_mode"] = "fixed"

    import json as _json
    with connect_bot_db(BOT_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) "
            "VALUES (?, ?, ?, 1, 'owner')",
            (expr, action, _json.dumps(payload)),
        )

    nice_time = _humanize_hhmm_pt(hhmm)
    cadence = f"every {dow.capitalize()}" if dow else "daily"
    if action == "drift_check":
        return f"Done — drift check scheduled {cadence} at {nice_time} PT."
    if action == "sports_recap":
        return f"Done - sports recap scheduled {cadence} at {nice_time} PT in this chat."
    if payload.get("intro_mode") == "rotate":
        intro_note = " with rotating greeting"
    else:
        intro_note = f" with intro '{intro.strip()}'" if intro and intro.strip() else ""
    return f"Done — {cadence} inspirational message scheduled for {nice_time} PT{intro_note}."


def _parse_day_of_week(text: str) -> str | None:
    lower = (text or "").lower()
    day_words = "|".join(sorted((re.escape(word) for word in _DOW_WORDS), key=len, reverse=True))
    patterns = [
        rf"\b(?:on|every|weekly|for)\s+({day_words})\b",
        rf"\b({day_words})s?\s+at\s+\d{{1,2}}(?::\d{{2}})?\s*(?:a\.?m\.?|p\.?m\.?)?\b",
        rf"\b\d{{1,2}}(?::\d{{2}})?\s*(?:a\.?m\.?|p\.?m\.?)?\s+({day_words})\b",
        rf"\bto\s+({day_words})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, lower, re.IGNORECASE)
        if m:
            return _DOW_WORDS[m.group(1).lower()]
    return None


def _parse_time_from_text(text: str) -> str | None:
    word_time = re.search(
        r"\b(?:at|to|for|move\s+to|change\s+to)\s+(noon|midnight)\b",
        text or "",
        re.IGNORECASE,
    )
    if word_time:
        return "12:00" if word_time.group(1).lower() == "noon" else "00:00"
    m = re.search(
        r"\b(?:at|to|for|move\s+to|change\s+to)\s+(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\b",
        text or "",
        re.IGNORECASE,
    )
    if not m:
        m = re.search(r"\b(\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?)\b", text or "", re.IGNORECASE)
    if not m:
        m = re.search(
            r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))\b",
            text or "",
            re.IGNORECASE,
        )
    return _normalize_hhmm(m.group(1)) if m else None


def _parse_intro_from_text(text: str) -> tuple[str | None, str]:
    raw = (text or "").strip()
    lower = raw.lower()
    rotates = re.search(r"\b(?:rotat(?:e|ing)|cycl(?:e|ing)|vary|varying|change\s+it\s+up|mix\s+it\s+up)\b.*\b(?:intro|greeting|opening|good\s+morning)\b", lower)
    rotates = rotates or re.search(r"\b(?:intro|greeting|opening|good\s+morning)\b.*\b(?:rotat(?:e|ing)|cycl(?:e|ing)|vary|varying|change\s+it\s+up|mix\s+it\s+up)\b", lower)
    if rotates:
        return None, "rotate"
    if re.search(r"\b(?:clear|remove|delete|drop)\b.*\b(?:intro|greeting|opening)\b", lower):
        return "", "clear"
    quoted = re.search(r"(?:intro|greeting|opening|say|says|to say)\s+(?:to\s+)?[\"'“”](.+?)[\"'“”]\s*$", raw, re.IGNORECASE | re.DOTALL)
    if quoted:
        return quoted.group(1).strip(), "fixed"
    m = re.search(
        r"\b(?:intro|greeting|opening|say|says|to say)\s+(?:to\s+)?(.+?)\s*$",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        intro = m.group(1).strip(" .")
        intro = re.sub(r"^(?:be|as|is|=)\s+", "", intro, flags=re.IGNORECASE).strip()
        if intro and len(intro) <= 160:
            return intro, "fixed"
    return None, ""


def _parse_cron_schedule_command(text: str) -> dict | None:
    raw = re.sub(r"^@davos\b[:,]?\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    if not raw:
        return None
    lower = raw.lower()
    if re.search(
        r"(?:\b(?:cron|job)\s*)?(?:id\s*)?#\s*\d+\b|"
        r"\b(?:cron|job)\s+(?:id\s*)?\d+\b",
        lower,
    ):
        return None
    if re.search(r"\b(?:change|edit|update|modify|fix|move|reschedule|cancel|delete|remove|stop|disable)\b", lower):
        return None
    has_create_verb = bool(re.search(r"\b(?:schedule|set\s+up|setup|add|create|new|make|start)\b", lower))
    has_cron_noun = bool(re.search(
        r"\b(?:cron|crons|cron\s+jobs?|job|jobs|automation|recurring\s+(?:job|jobs|message|messages)|"
        r"daily\s+(?:message|quote)|morning\s+(?:message|quote)|inspirational\s+(?:message|quote)|drift\s+check)\b",
        lower,
    ))
    has_recurring_phrase = bool(re.search(
        r"\b(?:every\s+(?:day|morning|night|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)|"
        r"daily\s+(?:at|quote\b|message\b)|nightly\s+(?:at|quote\b|message\b)|"
        r"weekly\s+at|each\s+(?:morning|night|day|week))\b",
        lower,
    ))
    if not ((has_create_verb and has_cron_noun) or has_recurring_phrase):
        return None
    supported_recurring_content = bool(re.search(
        r"\b(?:cron|automation|recurring|daily|morning|nightly|quote|inspir|motivational|"
        r"drift|maintenance|health\s+report|sports?\s+recap)\b",
        lower,
    ))
    if has_recurring_phrase and not supported_recurring_content:
        return None

    time_pt = _parse_time_from_text(raw)
    day_of_week = _parse_day_of_week(raw)
    action = "morning_message"
    if re.search(r"\b(?:drift|maintenance|weekly\s+report|health\s+report|bot\s+health)\b", lower):
        action = "drift_check"
    elif re.search(r"\bsports?\s+recap\b", lower):
        action = "sports_recap"
    intro, intro_mode = _parse_intro_from_text(raw)
    return {
        "time_pt": time_pt or "",
        "day_of_week": day_of_week or "",
        "action": action,
        "intro": intro or "",
        "intro_mode": intro_mode,
    }


def _schedule_cron_from_text(sender: str, text: str, originating_chat_id: str = "") -> str | None:
    from . import cron_creation
    parsed = _parse_cron_schedule_command(text)
    starting = parsed is not None or cron_creation.is_creation_request(text)
    from .permissions import is_owner
    if not is_owner(sender):
        return "Cron creation is the owner-only." if starting else None
    if not originating_chat_id:
        return "No chat context - ask from a DM or GC." if starting else None
    parsed = cron_creation.prepare_creation(sender, originating_chat_id, text, parsed, _normalize_hhmm)
    if not isinstance(parsed, dict):
        return parsed
    if parsed["action"] == "sports_recap":
        day = f" every {parsed['day_of_week']}" if parsed.get("day_of_week") else " daily"
        return _sports_recap_cron_from_text(
            sender, f"create sports recap cron at {parsed['time_pt']}{day}",
            originating_chat_id=originating_chat_id,
        )
    return _schedule_cron(
        parsed.get("time_pt", ""),
        parsed.get("intro", ""),
        action=parsed.get("action", "morning_message"),
        day_of_week=parsed.get("day_of_week", ""),
        intro_mode=parsed.get("intro_mode", ""),
        originating_chat_id=originating_chat_id,
    )


def _parse_cron_edit_command(text: str) -> dict | None:
    raw = re.sub(r"^@davos\b[:,]?\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    if not re.search(
        r"\b(?:change|edit|update|modify|fix|move|reschedule|rotate|cycle|vary|"
        r"set|make|switch|push)\b",
        raw,
        re.IGNORECASE,
    ):
        return None

    cron_id = None
    id_match = re.search(
        r"(?:cron|job|daily\s+job)?\s*(?:id\s*)?#\s*(\d+)\b|(?:cron|job|daily\s+job)\s+(?:id\s*)?(\d+)\b",
        raw,
        re.IGNORECASE,
    )
    if id_match:
        cron_id = int(id_match.group(1) or id_match.group(2))
    has_named_target = bool(
        re.search(
            r"\b(?:cron|crons|job|jobs|automation|daily\s+(?:message|job|one)|"
            r"morning\s+(?:message|job|quote|one)|quote\s+(?:job|one)|"
            r"drift\s+(?:check|job|one)|sports?\s+(?:recap|job|one)|"
            r"greeting|intro)\b",
            raw,
            re.IGNORECASE,
        )
    )
    if cron_id is None and not has_named_target:
        return None

    time_pt = _parse_time_from_text(raw)
    day_of_week = _parse_day_of_week(raw)
    lower = raw.lower()
    selector_action = ""
    if re.search(r"\b(?:drift|maintenance|health\s+report)\b", lower):
        selector_action = "drift_check"
    elif re.search(r"\bsports?\s+(?:recap|job|one)\b", lower):
        selector_action = "sports_recap"
    elif re.search(r"\b(?:morning|quote|inspir|motivational)\b", lower):
        selector_action = "morning_message"

    action = ""
    if re.search(
        r"\b(?:to|into|as)\s+(?:a\s+)?(?:drift|maintenance|health\s+report)\b|"
        r"\bmake\s+(?:cron\s+|job\s+)?(?:id\s*)?#?\s*\d+\s+"
        r"(?:a\s+)?(?:drift|maintenance|health\s+report)\b",
        lower,
    ):
        action = "drift_check"
    elif re.search(r"\b(?:to|into|as)\s+(?:a\s+)?sports?\s+recap\b", lower):
        action = "sports_recap"
    elif re.search(
        r"\b(?:to|into|as)\s+(?:a\s+)?(?:morning|quote|inspir|motivational)\b",
        lower,
    ):
        action = "morning_message"
    intro, intro_mode = _parse_intro_from_text(raw)

    if cron_id is None and not any(
        [time_pt, day_of_week, action, intro is not None, intro_mode]
    ):
        return None
    return {
        "cron_id": cron_id,
        "time_pt": time_pt or "",
        "day_of_week": day_of_week,
        "action": action,
        "selector_action": selector_action,
        "intro": intro,
        "intro_mode": intro_mode,
    }


def _cron_expr_parts(expr: str) -> tuple[str, str]:
    parts = (expr or "").strip().split()
    hhmm = _normalize_hhmm(parts[0]) if parts else None
    dow = parts[1].lower()[:3] if len(parts) > 1 else ""
    return hhmm or (parts[0] if parts else ""), dow if dow in _VALID_DOW else ""


def _resolve_cron_id_for_edit(parsed: dict, originating_chat_id: str) -> tuple[int | None, str | None]:
    if parsed.get("cron_id"):
        return int(parsed["cron_id"]), None
    if not originating_chat_id:
        return None, "Tell me the stable cron #id from `list all crons`, then say `change cron #ID ...`."
    import json as _json
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, action_type, action_payload FROM cron_jobs "
            "WHERE enabled = 1 ORDER BY cron_expression ASC"
        ).fetchall()
    finally:
        conn.close()
    matches = []
    selector_action = (parsed.get("selector_action") or "").strip()
    for rid, action, raw in rows:
        try:
            payload = _json.loads(raw or "{}")
        except Exception:
            payload = {}
        if payload.get("recipient") != originating_chat_id:
            continue
        if selector_action and action != selector_action:
            continue
        matches.append(rid)
    if len(matches) == 1:
        return int(matches[0]), None
    if not matches:
        return None, "No active cron job found in this chat. Use `list all crons` if you want to edit another chat's job by #id."
    return None, f"This chat has {len(matches)} cron jobs. Use `list crons`, then `change cron #ID ...`."


def _edit_cron_from_text(sender: str, text: str, originating_chat_id: str = "") -> str | None:
    parsed = _parse_cron_edit_command(text)
    if not parsed:
        return None
    with SCHEDULE_LOCK:
        cron_id, error = _resolve_cron_id_for_edit(parsed, originating_chat_id)
        if error:
            return error
        if not any(
            [
                parsed.get("time_pt"),
                parsed.get("day_of_week") is not None,
                parsed.get("action"),
                parsed.get("intro") is not None,
                parsed.get("intro_mode"),
            ]
        ):
            return (
                f"I found cron #{cron_id}. Tell me what to change, like "
                f"`set #{cron_id} to 7am` or `make #{cron_id} a drift check`."
            )
        return _edit_cron(
            cron_id or 0,
            sender=sender,
            time_pt=parsed.get("time_pt", ""),
            day_of_week=parsed.get("day_of_week", None),
            action=parsed.get("action", ""),
            intro=parsed.get("intro", None),
            intro_mode=parsed.get("intro_mode", ""),
        )


@schedule_locked
def _edit_cron(
    cron_id: int,
    sender: str = "",
    time_pt: str = "",
    day_of_week: str | None = None,
    action: str = "",
    intro: str | None = None,
    intro_mode: str = "",
) -> str:
    from .permissions import is_owner
    if not is_owner(sender):
        return "Permission denied - editing cron jobs is owner-only."
    if cron_id < 1:
        return "Cron ID must be 1 or higher."

    import json as _json
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        row = conn.execute(
            "SELECT id, cron_expression, action_type, action_payload, enabled FROM cron_jobs WHERE id = ?",
            (cron_id,),
        ).fetchone()
        if not row:
            return f"Cron #{cron_id} not found."
        rid, old_expr, old_action, raw_payload, enabled = row
        if not enabled:
            return f"Cron #{rid} is disabled. I won't re-enable old jobs through edit; schedule a new one or ask from Mini after auditing the row."
        try:
            payload = _json.loads(raw_payload or "{}")
        except Exception:
            payload = {}
        original_payload = dict(payload)
        hhmm, old_dow = _cron_expr_parts(old_expr)
        new_hhmm = _normalize_hhmm(time_pt) if time_pt else hhmm
        if not new_hhmm:
            return f"Cron #{rid} has invalid existing time '{old_expr}'. Set a new time like `change cron #{rid} to 6:30am`."
        if day_of_week is None:
            new_dow = old_dow
        else:
            new_dow = (day_of_week or "").strip().lower()[:3]
            if new_dow and new_dow not in _VALID_DOW:
                return f"Unknown day '{day_of_week}'. Try mon, tue, wed, thu, fri, sat, sun."
        new_action = (action or old_action or "morning_message").strip()
        if new_action not in _VALID_ACTIONS:
            return f"Unknown action '{new_action}'. Try: morning_message, drift_check, or sports_recap."
        mode = (intro_mode or "").strip().lower()
        if mode == "rotate":
            payload.pop("intro", None)
            payload["intro_mode"] = "rotate"
        elif mode == "clear":
            payload.pop("intro", None)
            payload.pop("intro_mode", None)
        elif intro is not None:
            payload["intro"] = intro.strip()
            payload["intro_mode"] = "fixed"
        if new_action != "morning_message":
            payload.pop("intro", None)
            payload.pop("intro_mode", None)

        new_expr = f"{new_hhmm} {new_dow}" if new_dow else new_hhmm
        if new_expr == old_expr and new_action == old_action and payload == original_payload:
            parts = [f"Cron #{rid} already matches: {new_expr} {new_action}"]
            if payload.get("intro_mode") == "rotate":
                parts.append("rotating greeting")
            elif payload.get("intro"):
                parts.append(f"intro '{payload['intro']}'")
            parts.append(f"-> {_cron_recipient_label(payload.get('recipient', ''))}")
            return " - ".join(parts) + "."

        conn.execute(
            "UPDATE cron_jobs SET cron_expression = ?, action_type = ?, action_payload = ? WHERE id = ?",
            (new_expr, new_action, _json.dumps(payload), rid),
        )
        conn.commit()
    finally:
        conn.close()

    parts = [f"Updated cron #{rid}: {new_expr} {new_action}"]
    if payload.get("intro_mode") == "rotate":
        parts.append("rotating greeting")
    elif payload.get("intro"):
        parts.append(f"intro '{payload['intro']}'")
    parts.append(f"-> {_cron_recipient_label(payload.get('recipient', ''))}")
    return " - ".join(parts) + "."


def _parse_cron_cancel_command(text: str) -> dict | None:
    raw = re.sub(r"^@davos\b[:,]?\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    if not re.search(r"\b(?:cancel|delete|remove|kill|stop|disable|turn\s+off)\b", raw, re.IGNORECASE):
        return None
    id_match = re.search(
        r"(?:cron|job)?\s*(?:id\s*)?#\s*(\d+)\b|(?:cron|job)\s+(?:id\s*)?(\d+)\b",
        raw,
        re.IGNORECASE,
    )
    has_named_target = re.search(
        r"\b(?:cron|crons|cron\s+jobs?|daily|morning\s+(?:job|message|quote)|"
        r"morning\s+one|recurring\s+(?:job|message)|scheduled\s+job|"
        r"drift\s+(?:check|one)|sports?\s+(?:recap|one)|quote\s+(?:job|one))\b",
        raw,
        re.IGNORECASE,
    )
    if not id_match and not has_named_target:
        return None

    lower = raw.lower()
    action = ""
    if re.search(r"\b(?:drift|maintenance|health\s+report)\b", lower):
        action = "drift_check"
    elif re.search(r"\bsports?\s+recap\b", lower):
        action = "sports_recap"
    elif re.search(r"\b(?:morning|quote|inspir|motivational)\b", lower):
        action = "morning_message"
    return {
        "cron_id": int(id_match.group(1) or id_match.group(2)) if id_match else None,
        "time_pt": _parse_time_from_text(raw) or "",
        "action": action,
    }


def _cancel_cron_from_text(sender: str, text: str, originating_chat_id: str = "") -> str | None:
    from . import cron_creation
    from .permissions import is_owner
    if cron_creation.is_draft_cancel(text) and "cancel" in text.lower():
        if not is_owner(sender):
            return "Permission denied - cancelling cron drafts is owner-only."
        if not originating_chat_id:
            return "No chat context - ask from a DM or GC."
        if cron_creation.clear_draft(sender, originating_chat_id):
            return "Cancelled the new cron draft."
        return "No new cron draft is pending in this chat."
    parsed = _parse_cron_cancel_command(text)
    if not parsed:
        return None

    if not is_owner(sender):
        return "Permission denied - cancelling cron jobs is owner-only."
    if parsed.get("cron_id"):
        return _cancel_cron_by_id(int(parsed["cron_id"]), sender=sender)
    if not originating_chat_id:
        return "No chat context - ask from a DM or GC."

    with SCHEDULE_LOCK:
        import json as _json
        with connect_bot_db(BOT_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, cron_expression, action_type, action_payload "
                "FROM cron_jobs WHERE enabled = 1 ORDER BY cron_expression, id"
            ).fetchall()
            matches = []
            for rid, expr, action, raw_payload in rows:
                try:
                    payload = _json.loads(raw_payload or "{}")
                except Exception:
                    payload = {}
                if payload.get("recipient") != originating_chat_id:
                    continue
                if parsed.get("time_pt") and _cron_expr_parts(expr)[0] != parsed["time_pt"]:
                    continue
                if parsed.get("action") and action != parsed["action"]:
                    continue
                matches.append((int(rid), expr, action, payload))

            if not matches:
                return "No active cron in this chat matches that. Use `list crons` to see the stable #ids."
            if len(matches) > 1:
                ids = ", ".join(f"#{row[0]}" for row in matches)
                return f"I found {len(matches)} possible jobs ({ids}). Use `list crons`, then cancel one by #id."

            rid, expr, action, payload = matches[0]
            conn.execute("UPDATE cron_jobs SET enabled = 0 WHERE id = ?", (rid,))

        logger.info("Cron #%d disabled by %s via natural-language match", rid, sender)
        return (
            f"Disabled cron #{rid}: {expr} {action} -> "
            f"{_cron_recipient_label(payload.get('recipient', ''))}."
        )


def _humanize_hhmm_pt(hhmm: str) -> str:
    try:
        from datetime import datetime as _dt
        return _dt.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0").lower()
    except Exception:
        return hhmm


def _is_group_recipient(recipient: str) -> bool:
    return bool(recipient and len(recipient) == 32 and all(c in "0123456789abcdef" for c in recipient.lower()))


def _mask_direct_recipient(recipient: str) -> str:
    if not recipient:
        return "?"
    if recipient.startswith("+") and len(recipient) >= 5:
        return f"{recipient[:2]}***{recipient[-4:]}"
    if "@" in recipient:
        name, _, domain = recipient.partition("@")
        return f"{name[:1]}***@{domain}" if domain else f"{recipient[:1]}***"
    return recipient


def _group_chat_label(chat_id: str) -> str:
    fallback = f"GC {chat_id[:8]}..."
    try:
        db_path = Path(os.path.expanduser(DB_PATH))
        if not db_path.exists():
            return fallback
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            chat = conn.execute(
                "SELECT room_name FROM chat WHERE chat_identifier = ? LIMIT 1",
                (chat_id,),
            ).fetchone()
            room_name = (chat["room_name"] if chat else "") or ""
            participants = conn.execute(
                """
                SELECT h.id
                FROM chat_handle_join chj
                JOIN chat c ON c.ROWID = chj.chat_id
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE c.chat_identifier = ?
                ORDER BY h.id
                LIMIT 4
                """,
                (chat_id,),
            ).fetchall()
        if room_name:
            return f"GC {room_name} ({chat_id[:8]}...)"
        if participants:
            names = ", ".join(_mask_direct_recipient(row["id"]) for row in participants)
            suffix = "..." if len(participants) >= 4 else ""
            return f"GC {chat_id[:8]}... [{names}{suffix}]"
    except Exception as e:
        logger.debug("Could not label group cron recipient %s: %s", chat_id[:8], e)
    return fallback


def _cron_recipient_label(recipient: str, requester_id: str = "") -> str:
    if not recipient:
        return "?"
    if _is_group_recipient(recipient):
        return _group_chat_label(recipient)
    if requester_id and recipient == requester_id:
        return "DM with you"
    return f"DM {_mask_direct_recipient(recipient)}"


def _short_recipient_id(recipient: str) -> str:
    if not recipient:
        return "?"
    if _is_group_recipient(recipient):
        return f"{recipient[:8]}...{recipient[-6:]}"
    return _mask_direct_recipient(recipient)


def _cron_destination_label(recipient: str, current_chat_id: str = "", requester_id: str = "") -> str:
    kind = "GC" if _is_group_recipient(recipient) else "DM"
    label = _cron_recipient_label(recipient, requester_id=requester_id)
    markers = []
    if recipient and recipient == current_chat_id:
        markers.append("this chat")
    if requester_id and recipient == requester_id:
        markers.append("your DM")
    marker_note = f" ({', '.join(markers)})" if markers else ""
    return f"{kind}: {label} [id {_short_recipient_id(recipient)}]{marker_note}"


def _select_morning_intro(payload: dict, date_key: str) -> str:
    return _morning_quote_helpers._select_morning_intro(payload, date_key)


def _strip_duplicate_morning_greeting(intro: str, quote: str) -> str:
    return _morning_quote_helpers._strip_duplicate_morning_greeting(intro, quote)


def _render_morning_message_body(payload: dict, quote: str, now_pt=None) -> str:
    return _morning_quote_helpers._render_morning_message_body(payload, quote, now_pt=now_pt)


_SPORTS_RECAP_LEAGUES = [
    ("NBA", "basketball", "nba", "pro"),
    ("MLB", "baseball", "mlb", "pro"),
    ("NFL", "football", "nfl", "pro"),
    ("NHL", "hockey", "nhl", "pro"),
    ("NCAAM", "basketball", "mens-college-basketball", "college_major"),
    ("CFB", "football", "college-football", "college_major"),
    ("NCAA Baseball", "baseball", "college-baseball", "college_unc"),
]
_SPORTS_LEAGUE_ORDER = {league: idx for idx, (league, *_rest) in enumerate(_SPORTS_RECAP_LEAGUES)}
_SEATTLE_TEAM_WORDS = ("mariners", "seahawks", "kraken")
_ETHAN_SPORT_WORDS = ("pacers", "haliburton")
_UNC_TEAM_WORDS = ("tar heels", "north carolina tar heels")
_COLLEGE_SPOTLIGHT_WORDS = (
    "college football playoff",
    "cfp",
    "march madness",
    "ncaa tournament",
    "college world series",
    "super regional",
    "regional",
    "conference tournament",
    "tournament",
    "championship",
    "semifinal",
    "quarterfinal",
)
_PLAYOFF_WORDS = (
    "playoff",
    "postseason",
    "post-season",
    "post season",
    "finals",
    "conference",
    "stanley",
    "wild card",
    "division series",
)
_SPORTS_RECAP_LOOKBACK_DAYS = 1
_SPORTS_RECAP_LOOKAHEAD_DAYS = 2
_SPORTS_RECAP_MAX_EVENTS = 32


def _event_competitor_name(competitor: dict) -> str:
    team = competitor.get("team") or {}
    return team.get("shortDisplayName") or team.get("displayName") or team.get("abbreviation") or "TBD"


def _espn_status_text(event: dict, comp: dict) -> str:
    status = event.get("status") or comp.get("status") or {}
    status_type = status.get("type") or {}
    text = (
        status_type.get("shortDetail")
        or status_type.get("detail")
        or status.get("shortDetail")
        or status.get("detail")
        or ""
    )
    if text:
        return text
    raw_date = event.get("date") or comp.get("date")
    if raw_date:
        try:
            from zoneinfo import ZoneInfo as _ZoneInfo
            dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            return dt.astimezone(_ZoneInfo("America/Los_Angeles")).strftime("%a %I:%M %p PT").replace(" 0", " ")
        except Exception:
            return "scheduled"
    return "status unknown"


def _event_is_pregame(event: dict, comp: dict) -> bool:
    status = event.get("status") or comp.get("status") or {}
    status_type = status.get("type") or {}
    return (status_type.get("state") or "").lower() == "pre" or (status_type.get("name") or "").upper() == "STATUS_SCHEDULED"


def _event_phase_order(event: dict, comp: dict, status_text: str) -> int:
    status = event.get("status") or comp.get("status") or {}
    status_type = status.get("type") or {}
    state = (status_type.get("state") or "").lower()
    name = (status_type.get("name") or "").upper()
    completed = bool(status_type.get("completed") or status.get("completed"))
    text = (status_text or "").lower()
    if state == "in" or name in {"STATUS_IN_PROGRESS", "STATUS_HALFTIME"}:
        return 0
    if re.search(r"\b(?:live|top|bottom|bot|mid|end|halftime|half|q[1-4]|[1-9](?:st|nd|rd|th))\b", text):
        return 0
    if completed or state == "post" or name in {"STATUS_FINAL", "STATUS_FINAL_OT", "STATUS_FULL_TIME"}:
        return 1
    if re.search(r"\b(?:final|final/ot|ft|full time)\b", text):
        return 1
    return 2


def _event_words_blob(event: dict, league: str, status_text: str) -> str:
    parts = [
        league,
        event.get("name"),
        event.get("shortName"),
        status_text,
    ]
    season = event.get("season")
    if isinstance(season, dict):
        parts.extend([season.get("slug"), season.get("type"), season.get("name")])
    for note in event.get("notes") or []:
        if isinstance(note, dict):
            parts.extend([note.get("headline"), note.get("type")])
    for comp in event.get("competitions") or []:
        for note in comp.get("notes") or []:
            if isinstance(note, dict):
                parts.extend([note.get("headline"), note.get("type")])
        for competitor in comp.get("competitors") or []:
            parts.append(_event_competitor_name(competitor))
            team = competitor.get("team") or {}
            parts.extend(
                team.get(key)
                for key in ("displayName", "shortDisplayName", "location", "name", "abbreviation")
            )
    return " ".join(str(part or "") for part in parts).lower()


def _event_dedupe_key(event: dict, league: str) -> str:
    event_id = event.get("id") or event.get("uid")
    if event_id:
        return f"{league}:{event_id}"
    competitions = event.get("competitions") or []
    comp = competitions[0] if competitions else {}
    teams = "|".join(_event_competitor_name(c) for c in comp.get("competitors") or [])
    return f"{league}:{event.get('name') or event.get('shortName') or ''}:{event.get('date') or comp.get('date') or ''}:{teams}"


def _ranked_team_score(event: dict) -> int:
    best = 999
    for comp in event.get("competitions") or []:
        for competitor in comp.get("competitors") or []:
            team = competitor.get("team") or {}
            candidates = [
                competitor.get("rank"),
                team.get("rank"),
                (competitor.get("curatedRank") or {}).get("current") if isinstance(competitor.get("curatedRank"), dict) else None,
                (team.get("curatedRank") or {}).get("current") if isinstance(team.get("curatedRank"), dict) else None,
            ]
            for value in candidates:
                try:
                    rank = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 < rank < best:
                    best = rank
    return best


def _event_has_unc(event: dict, names_blob: str) -> bool:
    if any(word in names_blob for word in _UNC_TEAM_WORDS):
        return True
    for comp in event.get("competitions") or []:
        for competitor in comp.get("competitors") or []:
            team = competitor.get("team") or {}
            abbrev = str(team.get("abbreviation") or competitor.get("abbreviation") or "").strip().lower()
            display = " ".join(
                str(team.get(key) or "")
                for key in ("displayName", "shortDisplayName", "location", "name")
            ).lower()
            if abbrev == "unc" or "tar heels" in display:
                return True
    return False


def _is_college_spotlight(event: dict, category: str, names_blob: str, is_unc: bool) -> bool:
    if not category.startswith("college"):
        return True
    if category == "college_unc":
        return is_unc
    if is_unc:
        return True
    if any(word in names_blob for word in _COLLEGE_SPOTLIGHT_WORDS):
        return True
    return _ranked_team_score(event) <= 25


def _format_espn_event(event: dict, league: str, category: str) -> tuple[tuple[int, int, int, str], str] | None:
    competitions = event.get("competitions") or []
    comp = competitions[0] if competitions else {}
    competitors = comp.get("competitors") or []
    status_text = _espn_status_text(event, comp)
    phase_order = _event_phase_order(event, comp, status_text)
    names_blob = _event_words_blob(event, league, status_text)
    is_seattle = category == "pro" and any(word in names_blob for word in _SEATTLE_TEAM_WORDS)
    is_ethan = any(word in names_blob for word in _ETHAN_SPORT_WORDS)
    is_unc = _event_has_unc(event, names_blob)
    is_playoff = any(word in names_blob for word in _PLAYOFF_WORDS)
    if category == "college_unc" and is_unc and phase_order == 2:
        return None
    if not _is_college_spotlight(event, category, names_blob, is_unc):
        return None

    by_homeaway = {c.get("homeAway"): c for c in competitors}
    if by_homeaway.get("away") and by_homeaway.get("home"):
        away = by_homeaway["away"]
        home = by_homeaway["home"]
        away_score = away.get("score", "")
        home_score = home.get("score", "")
        away_name = _event_competitor_name(away)
        home_name = _event_competitor_name(home)
        if _event_is_pregame(event, comp) or (away_score in ("", "0") and home_score in ("", "0")):
            score = f"{away_name} @ {home_name}"
        else:
            score = f"{away_name} {away_score} @ {home_name} {home_score}".strip()
    elif competitors:
        score = " vs ".join(_event_competitor_name(c) for c in competitors[:2])
    else:
        score = event.get("shortName") or event.get("name") or "event"

    pro_playoff = category == "pro" and is_playoff
    priority = 0 if pro_playoff else 1 if is_unc else 2 if is_ethan else 3 if is_seattle else 4
    ethan_rank = 0 if is_ethan else 1
    return (
        phase_order,
        priority,
        ethan_rank,
        _SPORTS_LEAGUE_ORDER.get(league, 99),
        score,
    ), f"{league}: {score} - {status_text}"


def _sports_recap_date_keys(now_pt: datetime) -> list[str]:
    base_date = now_pt.date()
    return [
        (base_date + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(-_SPORTS_RECAP_LOOKBACK_DAYS, _SPORTS_RECAP_LOOKAHEAD_DAYS + 1)
    ]


def _append_sports_recap_sections(lines: list[str], ranked_events: list[tuple[tuple[int, int, int, int, str], str]]) -> None:
    groups = [
        ("Live", lambda key: key[0] == 0),
        ("Finished", lambda key: key[0] == 1),
        ("Scheduled", lambda key: key[0] == 2),
    ]
    sorted_events = sorted(ranked_events, key=lambda item: item[0])
    appended = 0
    for title, predicate in groups:
        section = [(key, line) for key, line in sorted_events if predicate(key)]
        if not section:
            continue
        if len(lines) > 1:
            lines.append("")
        lines.append(title)
        for _, line in section[: max(0, _SPORTS_RECAP_MAX_EVENTS - appended)]:
            lines.append(line)
            appended += 1
        if appended >= _SPORTS_RECAP_MAX_EVENTS:
            break


def _get_sports_recap(now_pt=None) -> str:
    """Daily sports recap using ESPN public scoreboard APIs. No LLM required."""
    if now_pt is None:
        from datetime import datetime as _dt, timezone as _tz
        from zoneinfo import ZoneInfo as _ZoneInfo
        now_pt = _dt.now(_tz.utc).astimezone(_ZoneInfo("America/Los_Angeles"))
    pretty_date = now_pt.strftime("%b %-d") if os.name != "nt" else now_pt.strftime("%b %#d")
    lines = [f"Sports Recap - {pretty_date}"]
    failures = []
    ranked_events = []
    date_keys = _sports_recap_date_keys(now_pt)
    current_date_key = now_pt.strftime("%Y%m%d")

    for league, sport, league_slug, category in _SPORTS_RECAP_LEAGUES:
        league_events = []
        seen_events = set()
        try:
            league_date_keys = [current_date_key] if league == "MLB" else date_keys
            for date_key in league_date_keys:
                resp = requests.get(
                    f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league_slug}/scoreboard",
                    params={"dates": date_key},
                    timeout=12,
                )
                resp.raise_for_status()
                for event in resp.json().get("events") or []:
                    event_key = _event_dedupe_key(event, league)
                    if event_key in seen_events:
                        continue
                    seen_events.add(event_key)
                    league_events.append(event)
        except Exception as exc:
            logger.warning("sports recap ESPN fetch failed for %s: %s", league, exc)
            failures.append(league)
            continue
        if not league_events:
            continue
        for event in league_events:
            formatted = _format_espn_event(event, league, category)
            if formatted is not None:
                ranked_events.append(formatted)

    if ranked_events:
        _append_sports_recap_sections(lines, ranked_events)
    else:
        lines.append("No priority games on the ESPN board yet.")

    if failures:
        lines.append(f"ESPN fetch issue: {', '.join(failures)}.")
    return "\n".join(lines)


def _sports_recap_request_kind(text: str) -> str:
    lower = (text or "").lower()
    if not re.search(r"\b(?:sports?|espn|mariners|seahawks|kraken|playoffs?|recap)\b", lower):
        return ""
    if not re.search(r"\b(?:cron|recurring|daily|every\s+day|scheduled?\s+job|job)\b", lower) and not re.search(r"\bsports\s+recap\b", lower):
        return ""
    if re.search(r"\b(?:change|edit|update|modify|move|reschedule|fix)\b", lower):
        return "edit"
    if re.search(r"\b(?:create|new|set\s+up|setup|add|make|start|schedule)\b", lower):
        return "create"
    return ""


def _sports_recap_mentions_other_chat(text: str, originating_chat_id: str) -> bool:
    if _is_group_recipient(originating_chat_id):
        return False
    lower = (text or "").lower()
    return bool(re.search(r"\b(?:cole|cole'?s|group|gc|chat|boys)\b", lower))


def _sports_recap_cron_from_text(sender: str, text: str, originating_chat_id: str = "") -> str | None:
    kind = _sports_recap_request_kind(text)
    if not kind:
        return None
    from .permissions import is_admin, is_owner
    if not is_admin(sender):
        return "Sports recap cron creation is admin-only."
    if not originating_chat_id:
        return "No chat context - ask from the DM or group chat where this should post."
    if _sports_recap_mentions_other_chat(text, originating_chat_id):
        return (
            "I won't guess a different chat for sports recap. "
            "Ask from the target group chat, or use `list all crons` then `change cron #ID ...`."
        )
    from . import cron_creation
    requested, parse_error = cron_creation.inspect_request(text, _normalize_hhmm)
    if parse_error:
        return parse_error
    requested_hhmm = requested.get("time_pt")
    requested_dow = requested.get("day_of_week", "")
    if requested.get("ambiguous_hour"):
        if kind != "edit" and is_owner(sender):
            return _schedule_cron_from_text(sender, text, originating_chat_id=originating_chat_id)
        return "Is that AM or PM? Use a Pacific time like `8am`, `8pm`, or `08:00`."

    with SCHEDULE_LOCK:
        import json as _json
        existing = []
        conn = sqlite3.connect(BOT_DB_PATH)
        try:
            rows = conn.execute(
                "SELECT id, cron_expression, action_payload FROM cron_jobs "
                "WHERE enabled = 1 AND action_type = 'sports_recap' ORDER BY id ASC"
            ).fetchall()
            for rid, expr, raw in rows:
                try:
                    payload = _json.loads(raw or "{}")
                except Exception:
                    payload = {}
                if payload.get("recipient") == originating_chat_id:
                    existing.append((int(rid), expr, payload))

            payload = {
                "recipient": originating_chat_id,
                "style": "clean_scoreboard",
                "focus": "pro_playoffs_unc_seattle_college",
                "source": "espn_public_api",
            }
            if existing:
                rid, old_expr, old_payload = existing[0]
                if kind == "edit":
                    old_hhmm, old_dow = _cron_expr_parts(old_expr)
                    hhmm = requested_hhmm or old_hhmm
                    if not hhmm:
                        return "I found the sports recap cron, but its saved time is malformed. Use `change sports cron to 6pm`."
                    old_payload.update(payload)
                    dow = requested_dow or (old_dow if requested.get("weekly") is not False else "")
                    if requested.get("weekly") and not dow:
                        return "Which weekday should the sports recap run? Use `change sports cron to Friday at 6pm`."
                    expr = f"{hhmm} {dow}" if dow else hhmm
                    cadence = f"every {dow.capitalize()}" if dow else "daily"
                    conn.execute(
                        "UPDATE cron_jobs SET cron_expression = ?, action_payload = ? WHERE id = ?",
                        (expr, _json.dumps(old_payload), rid),
                    )
                    conn.execute(
                        "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                        (
                            sender,
                            "sports_recap_cron_updated",
                            _json.dumps({"cron_id": rid, "chat_id": originating_chat_id, "time_pt": hhmm, "refreshed": not bool(requested_hhmm)}),
                        ),
                    )
                    if requested_hhmm:
                        action_line = f"Updated sports recap cron #{rid}: {cadence} at {_humanize_hhmm_pt(hhmm)} PT in this chat."
                    else:
                        action_line = f"Refreshed sports recap cron #{rid}: keeping {cadence} at {_humanize_hhmm_pt(hhmm)} PT in this chat."
                else:
                    action_line = (
                        f"Sports recap cron already exists here as #{rid} at "
                        f"{_humanize_hhmm_pt(_cron_expr_parts(old_expr)[0])} PT. Not duplicating it."
                    )
            elif kind == "edit":
                return "No active sports recap cron found in this chat. Ask from the target chat to create one with a time like `6pm PT`."
            else:
                if not requested_hhmm or (requested.get("weekly") and not requested_dow):
                    if is_owner(sender):
                        return _schedule_cron_from_text(sender, text, originating_chat_id=originating_chat_id)
                    if requested.get("weekly") and not requested_dow:
                        return "Which weekday should the sports recap run? Ask for one weekday and a Pacific time."
                    return "I can make the sports recap cron, but I need a time like `6pm PT`."
                expr = f"{requested_hhmm} {requested_dow}" if requested_dow else requested_hhmm
                cadence = f"every {requested_dow.capitalize()}" if requested_dow else "daily"
                cur = conn.execute(
                    "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) "
                    "VALUES (?, 'sports_recap', ?, 1, ?)",
                    (expr, _json.dumps(payload), sender),
                )
                rid = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                    (
                        sender,
                        "sports_recap_cron_created",
                        _json.dumps({"cron_id": rid, "chat_id": originating_chat_id, "time_pt": requested_hhmm}),
                    ),
                )
                action_line = f"Created sports recap cron #{rid}: {cadence} at {_humanize_hhmm_pt(requested_hhmm)} PT in this chat."
            conn.commit()
        finally:
            conn.close()

    if kind != "edit" and is_owner(sender):
        cron_creation.clear_draft(sender, originating_chat_id)
    try:
        preview = _get_sports_recap()
    except Exception as exc:
        logger.warning("sports recap preview failed: %s", exc)
        preview = "Test update hit an ESPN fetch error. The cron is saved; try the preview again later."
    return (
        f"{action_line}\n\n"
        "Plan:\n"
        "1. Pull ESPN public scoreboards for MLB, NFL, NBA, NHL, college football, men's college hoops, and college baseball.\n"
        "2. Order games Live, then Finished, then Scheduled; within each bucket highlight playoffs, UNC, and Seattle.\n"
        "3. Drop a clean scoreboard in this chat on the saved schedule.\n\n"
        f"TEST UPDATE:\n{preview}"
    )


def _describe_cron_from_text(sender: str, text: str, originating_chat_id: str = "") -> str | None:
    lower = (text or "").lower()
    if not re.search(r"\b(?:describe|explain|details?|what(?:'s|\s+is)|break\s+down)\b", lower):
        return None
    if not re.search(r"\b(?:cron|crons|job|jobs|recurring|daily\s+message|sports\s+recap)\b", lower):
        return None
    from .permissions import is_admin, is_owner
    if not is_admin(sender):
        return "Cron details are admin-only."
    if not originating_chat_id:
        return "No chat context - ask from a DM or GC."
    import json as _json
    id_match = re.search(r"(?:#|id\s*#?|cron\s+|job\s+)(\d+)\b", lower)
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        if id_match:
            cron_id = int(id_match.group(1))
            row = conn.execute(
                "SELECT id, cron_expression, action_type, action_payload, enabled FROM cron_jobs WHERE id = ?",
                (cron_id,),
            ).fetchone()
            if not row:
                return f"Cron #{cron_id} not found."
            rows = [row]
        else:
            rows = conn.execute(
                "SELECT id, cron_expression, action_type, action_payload, enabled FROM cron_jobs WHERE enabled = 1"
            ).fetchall()
    finally:
        conn.close()
    parsed = []
    for rid, expr, action, raw, enabled in rows:
        try:
            payload = _json.loads(raw or "{}")
        except Exception:
            payload = {}
        recipient = payload.get("recipient", "")
        if id_match and not is_owner(sender) and recipient != originating_chat_id:
            return f"Cron #{rid} is not in this chat. Ask the owner for cross-chat cron details."
        if not id_match and recipient == originating_chat_id:
            parsed.append((rid, expr, action, payload, enabled))
        elif id_match:
            parsed.append((rid, expr, action, payload, enabled))

    if not parsed:
        return "No active cron job found in this chat. Try `list all cron jobs` from the owner's DM for the full board."
    if not id_match and len(parsed) > 1:
        return "This chat has multiple cron jobs. Use `describe cron #ID`.\n\n" + _list_crons(
            originating_chat_id,
            scope="current",
            requester_id=sender,
        )
    rid, expr, action, payload, enabled = parsed[0]
    return _format_cron_description(rid, expr, action, payload, enabled, originating_chat_id, sender)


def _format_cron_description(
    cron_id: int,
    expr: str,
    action: str,
    payload: dict,
    enabled: int,
    current_chat_id: str = "",
    requester_id: str = "",
) -> str:
    hhmm, dow = _cron_expr_parts(expr)
    when = f"{_humanize_hhmm_pt(hhmm)} PT"
    cadence = f"every {dow.capitalize()}" if dow else "daily"
    destination = _cron_destination_label(payload.get("recipient", ""), current_chat_id=current_chat_id, requester_id=requester_id)
    status = "enabled" if enabled else "disabled"
    if action == "morning_message":
        if payload.get("intro_mode") == "rotate":
            behavior = "Morning message with a rotating greeting plus the daily quote."
        elif payload.get("intro"):
            behavior = f"Morning message with fixed intro {payload['intro']!r} plus the daily quote."
        else:
            behavior = "Morning message with the daily quote."
    elif action == "drift_check":
        behavior = "Weekly maintenance/drift report: cron status, recent errors, and backlog health."
    elif action == "sports_recap":
        behavior = "Daily ESPN sports recap: Live, Finished, then Scheduled; highlights playoffs, UNC, and Seattle teams."
    else:
        behavior = f"Custom cron action: {action}."
    return (
        f"Cron #{cron_id} ({status})\n"
        f"When: {cadence} at {when}\n"
        f"Action: {action}\n"
        f"Destination: {destination}\n"
        f"What it does: {behavior}"
    )


def _list_crons(
    originating_chat_id: str,
    include_all: bool = False,
    scope: str = "current",
    requester_id: str = "",
) -> str:
    if not originating_chat_id:
        return "No chat context - ask from a DM or GC."
    scope = (scope or "current").strip().lower()
    if include_all:
        scope = "all"
    aliases = {
        "dm": "direct",
        "dms": "direct",
        "directs": "direct",
        "1on1": "direct",
        "1:1": "direct",
        "group": "groups",
        "gc": "groups",
        "gcs": "groups",
        "self": "mine",
        "me": "mine",
    }
    scope = aliases.get(scope, scope)
    if scope not in {"current", "all", "mine", "direct", "groups"}:
        scope = "current"
    import json as _json
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, cron_expression, action_type, action_payload "
            "FROM cron_jobs WHERE enabled = 1 ORDER BY cron_expression ASC"
        ).fetchall()
    finally:
        conn.close()
    matching = []
    for rid, expr, action, raw in rows:
        try:
            p = _json.loads(raw) if raw else {}
        except Exception:
            p = {}
        recipient = p.get("recipient", "")
        is_group = _is_group_recipient(recipient)
        if scope == "all":
            matching.append((rid, expr, action, p))
        elif scope == "direct" and recipient and not is_group:
            matching.append((rid, expr, action, p))
        elif scope == "groups" and is_group:
            matching.append((rid, expr, action, p))
        elif scope == "mine" and recipient == (requester_id or originating_chat_id):
            matching.append((rid, expr, action, p))
        elif scope == "current" and recipient == originating_chat_id:
            matching.append((rid, expr, action, p))
    if not matching:
        empty = {
            "all": "No active recurring jobs found across chats.",
            "direct": "No active recurring jobs found in 1:1 / DM chats.",
            "groups": "No active recurring jobs found in group chats.",
            "mine": "No active recurring jobs found to you.",
        }.get(scope, "No daily jobs scheduled in this chat.")
        return empty

    lines = []
    exact_groups: dict[tuple[str, str, str], list[int]] = {}
    destination_groups: dict[str, list[int]] = {}
    for rid, expr, action, p in matching:
        parts = (expr or "").split()
        time_str = _humanize_hhmm_pt(parts[0]) if parts else expr
        cadence = f"every {parts[1].capitalize()}" if len(parts) > 1 else "daily"
        intro = p.get("intro", "")
        if p.get("intro_mode") == "rotate":
            intro_note = " | intro: rotating"
        else:
            intro_note = f" | intro: {intro!r}" if intro else ""
        recipient = p.get("recipient", "")
        dest = _cron_destination_label(recipient, current_chat_id=originating_chat_id, requester_id=requester_id)
        exact_groups.setdefault((recipient, expr, action), []).append(int(rid))
        if recipient:
            destination_groups.setdefault(recipient, []).append(int(rid))
        lines.append(f"#{rid} | {cadence} at {time_str} PT | {action}{intro_note} | {dest}")

    heading = {
        "all": "Active recurring jobs across all chats:",
        "direct": "Active recurring jobs in 1:1 / DM chats:",
        "groups": "Active recurring jobs in group chats:",
        "mine": "Active recurring jobs to you:",
    }.get(scope, "Recurring jobs in this chat:")

    warnings = []
    exact_duplicate_sets = []
    for (recipient, expr, action), ids in sorted(exact_groups.items(), key=lambda item: item[1][0]):
        if len(ids) > 1:
            exact_duplicate_sets.append(set(ids))
            parts = (expr or "").split()
            when = f"{_humanize_hhmm_pt(parts[0])} PT" if parts else expr
            if len(parts) > 1:
                when = f"{when} every {parts[1].capitalize()}"
            dest = _cron_destination_label(recipient, current_chat_id=originating_chat_id, requester_id=requester_id)
            warnings.append(f"Possible duplicate: #{', #'.join(str(i) for i in ids)} -> {when} | {action} | {dest}")
    for recipient, ids in sorted(destination_groups.items(), key=lambda item: item[1][0]):
        if len(ids) > 1 and not any(set(ids).issubset(group) for group in exact_duplicate_sets):
            dest = _cron_destination_label(recipient, current_chat_id=originating_chat_id, requester_id=requester_id)
            warnings.append(f"Multiple jobs in same destination: #{', #'.join(str(i) for i in ids)} -> {dest}")
    suffix = ""
    if warnings:
        suffix = "\n\nCheck these before deleting:\n" + "\n".join(f"- {w}" for w in warnings)
    return heading + "\n" + "\n".join(lines) + suffix


@schedule_locked
def _cancel_cron_by_id(cron_id: int, sender: str = "") -> str:
    from .permissions import is_owner
    if not is_owner(sender):
        return "Permission denied — cancelling cron jobs by ID is owner-only."
    if cron_id < 1:
        return "Cron ID must be 1 or higher."
    import json as _json
    with connect_bot_db(BOT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, cron_expression, action_type, action_payload, enabled FROM cron_jobs WHERE id = ?",
            (cron_id,),
        ).fetchone()
        if not row:
            return f"Cron #{cron_id} not found."
        rid, expr, action, raw, enabled = row
        if not enabled:
            return f"Cron #{rid} is already disabled."
        conn.execute("UPDATE cron_jobs SET enabled = 0 WHERE id = ?", (rid,))
    try:
        p = _json.loads(raw) if raw else {}
    except Exception:
        p = {}
    logger.info("Cron #%d disabled by %s via stable ID", rid, sender)
    return f"Disabled cron #{rid}: {expr} {action} -> {_cron_recipient_label(p.get('recipient', ''))}."


@schedule_locked
def _cancel_cron(position: int, originating_chat_id: str = "") -> str:
    if not originating_chat_id:
        return "No chat context — ask from a DM or GC."
    if position < 1:
        return "Position must be 1 or higher."
    import json as _json
    with connect_bot_db(BOT_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, cron_expression, action_payload FROM cron_jobs "
            "WHERE enabled = 1 ORDER BY cron_expression ASC"
        ).fetchall()
        matching = []
        for rid, expr, raw in rows:
            try:
                p = _json.loads(raw) if raw else {}
            except Exception:
                p = {}
            if p.get("recipient") == originating_chat_id:
                matching.append((rid, expr))
        if not matching:
            return "No daily jobs to cancel in this chat."
        if position > len(matching):
            return f"This chat only has {len(matching)} daily job(s)."
        rid, expr = matching[position - 1]
        conn.execute("UPDATE cron_jobs SET enabled = 0 WHERE id = ?", (rid,))
    parts = expr.split()
    cadence = f"every {parts[1].capitalize()}" if len(parts) > 1 else "daily"
    return f"Cancelled the {cadence} {_humanize_hhmm_pt(parts[0])} PT job."


def _list_chats(limit: int = 10) -> str:
    with connect_bot_db(BOT_DB_PATH) as conn:
        rows = conn.execute("""
            SELECT sender, COUNT(*) as msg_count, MAX(ts) as last_msg,
                   GROUP_CONCAT(content, ' | ') as sample
            FROM (SELECT sender, ts, content FROM messages ORDER BY id DESC LIMIT 200)
            GROUP BY sender
            ORDER BY last_msg DESC
            LIMIT ?
        """, (limit,)).fetchall()
    if not rows:
        return "No chat history found."
    lines = []
    for sender, count, last, sample in rows:
        snippet = (sample or "")[:80].replace("\n", " ")
        lines.append(f"{sender} — {count} msgs, last: {last[:16]}\n  recent: {snippet}")
    return "\n\n".join(lines)


def _clear_chat_history(chat_id: str, mode: str, value: int = 0) -> str:
    from .memory import clear_history, clear_history_minutes, clear_history_count
    if mode == "all":
        clear_history(chat_id)
        return f"Cleared all history for {chat_id}."
    elif mode == "minutes":
        n = clear_history_minutes(chat_id, value)
        return f"Cleared {n} messages from last {value} min for {chat_id}."
    elif mode == "count":
        n = clear_history_count(chat_id, value)
        return f"Cleared last {n} messages for {chat_id}."
    return "Unknown mode."


def _workout_log_tool(args: dict, sender: str) -> str:
    return _workout_helpers.workout_log_tool(
        args,
        sender,
        db_path=BOT_DB_PATH,
        connect_fn=connect_bot_db,
    )


def _log_workout(args: dict, sender: str = "") -> str:
    return _workout_helpers.log_workout(args, sender, db_path=BOT_DB_PATH)


def _looks_like_chat_guid(s: str) -> bool:
    return len(s) == 32 and all(c in "0123456789abcdef" for c in s.lower())


_PRIVATE_SEND_TTL_SECONDS = 10 * 60
_pending_private_sends: dict[str, dict] = {}


def _pending_key(sender: str) -> str:
    from .config import normalize_handle
    return normalize_handle(sender or "")


def _get_pending_private_send(sender: str) -> dict | None:
    key = _pending_key(sender)
    pending = _pending_private_sends.get(key)
    if not pending:
        return None
    if _utc_timestamp() > float(pending.get("expires_at", 0)):
        _pending_private_sends.pop(key, None)
        return None
    return pending


def _message_hash(message: str) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256((message or "").encode("utf-8")).hexdigest()[:12]


def _destination_hash(destination: str) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256((destination or "").encode("utf-8")).hexdigest()[:12]


def _message_preview(message: str, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", (message or "").strip())
    return clean if len(clean) <= limit else clean[: limit - 1] + "..."


def redact_private_send_text_for_log(text: str) -> str:
    parsed = parse_private_send_command(text)
    if not parsed:
        return text
    label = (parsed.get("label") or parsed.get("recipient") or "unknown")[:80]
    msg = parsed.get("message") or ""
    return f"[private send request recipient={label!r} message_hash={_message_hash(msg)} message_len={len(msg)}]"


def _safe_tool_args_for_log(name: str, args: dict) -> dict:
    # Only schema field names are safe metadata. Unknown argument names and
    # every value may contain a file, query, command, credential or message.
    supplied = args if isinstance(args, dict) else {}
    definition = next((tool for tool in TOOL_DEFINITIONS if tool["name"] == name), {})
    fields = definition.get("parameters", {}).get("properties", {})
    safe = {"argument_count": len(supplied), "known_fields": sorted(key for key in fields if key in supplied)}
    if name != "send_imessage":
        return safe
    message = supplied.get("message") if isinstance(supplied.get("message"), str) else ""
    recipient = supplied.get("recipient") if isinstance(supplied.get("recipient"), str) else ""
    if recipient:
        safe["recipient_masked"] = _mask_destination(recipient)
        safe["recipient_hash"] = _destination_hash(recipient)
    safe["message_hash"] = _message_hash(message)
    safe["message_len"] = len(message)
    return safe


def _mask_destination(destination: str) -> str:
    dest = (destination or "").strip()
    if "@" in dest:
        local, _, domain = dest.partition("@")
        if not local:
            return f"***@{domain}"
        return f"{local[0]}***@{domain}"
    digits = re.sub(r"\D", "", dest)
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    if len(dest) > 6:
        return f"{dest[:2]}***{dest[-2:]}"
    return "***"


def _parse_private_phone_recipient(rest: str) -> dict | None:
    """Split one phone from its body; ask for a delimiter when ambiguous."""
    from .config import normalize_handle

    if not re.match(r"^[+0-9(]", rest) or "@" in rest.split(None, 1)[0]:
        return None
    if re.match(r"^[0-9][^\s]*[A-Za-z]", rest):
        return None  # Numeric business/contact names such as 7-Eleven are aliases.
    error = {"error": "Use one complete phone number followed by a colon and your message, like `text +1 555-000-0001: hello`."}
    us_phone = r"(?:\+?1[\s.\-]*)?(?:\([0-9]{3}\)|[0-9]{3})[\s.\-]*[0-9]{3}[\s.\-]*[0-9]{4}"
    # US/CA numbers have a fixed length, so numeric message bodies such as
    # "1234 is the code" cannot be swallowed into the destination.
    match = re.match(rf"^({us_phone})(?=$|\s|[:,;])\s*(.*)$", rest, re.DOTALL)
    explicit = False
    if match:
        raw_phone, body = match.groups()
        recipient = normalize_handle(raw_phone)
        if body.startswith(":"):
            explicit = True
            body = body[1:].lstrip()
    else:
        delimited = re.match(r"^([+0-9()\s.\-]+):\s*(.*)$", rest, re.DOTALL)
        if delimited:
            explicit = True
            raw_phone, body = delimited.groups()
        else:
            # Canonical international numbers have an unambiguous token boundary.
            # Formatted international numbers need ':' because lengths vary.
            match = re.match(r"^(\+[2-9][0-9]{7,14})(?=$|\s)\s*(.*)$", rest, re.DOTALL)
            if not match:
                return error
            raw_phone, body = match.groups()
        digits = re.sub(r"[^0-9]", "", raw_phone)
        if re.fullmatch(r"\+[0-9()\s.\-]+", raw_phone.strip()) and 8 <= len(digits) <= 15 and digits[0] not in "01":
            recipient = "+" + digits
        else:
            return error
    if normalize_handle(recipient) != recipient:
        # Do not reinterpret an international identity under the shared
        # normalizer's existing ten-digit US/CA rule.
        return error
    if not explicit:
        other_number = re.sub(r"^(?:[,;&]\s*|and\s+)", "", body, flags=re.IGNORECASE)
        if re.match(rf"^(?:{us_phone}|\+[1-9][0-9]{{7,14}})(?=$|\s|[:,;])", other_number):
            return error
    body = re.sub(
        r"^(?:1\s*on\s*1|1:1|one\s+on\s+one|privately|private|direct(?:ly)?|saying|says|that|-)\s+",
        "", body, count=1, flags=re.IGNORECASE,
    ) if not explicit else body
    return {
        "recipient": recipient,
        "label": recipient,
        "message": body.strip(),
        "resolution_path": "phone_command",
    }


def parse_private_send_command(text: str) -> dict | None:
    """Parse explicit private-message commands such as 'msg Cole 1on1 hello'."""
    raw = re.sub(r"^@davos\b[:,]?\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    if not raw:
        return None

    rest = None
    for pattern in (
        r"^(?:msg|dm|text|message)\s+(.+)$",
        r"^send\s+(?:a\s+)?(?:private|direct|1on1|1:1|one\s+on\s+one)\s+(?:message|text)?\s*(?:to\s+)?(.+)$",
        r"^send\s+(?:a\s+)?(?:message|text)\s+to\s+(.+)$",
    ):
        m = re.match(pattern, raw, re.IGNORECASE | re.DOTALL)
        if m:
            rest = m.group(1).strip()
            break
    if not rest:
        return None

    quoted = re.match(r"""^["']([^"']{1,80})["']\s+(.+)$""", rest, re.DOTALL)
    if quoted:
        if re.fullmatch(r"[+0-9()\s.\-]+", quoted.group(1)):
            phone = _parse_private_phone_recipient(f"{quoted.group(1)}: {quoted.group(2)}")
            if phone is not None:
                return phone
        return {
            "recipient": quoted.group(1).strip(),
            "label": quoted.group(1).strip(),
            "message": quoted.group(2).strip(),
            "resolution_path": "quoted_alias",
        }

    phone = _parse_private_phone_recipient(rest)
    if phone is not None:
        return phone

    phone_alias = re.match(
        r"^([A-Za-z][A-Za-z0-9 .'\-]{0,50}?)\s+((?:[+(][0-9]|[0-9][0-9\s().\-]{6,}).*)$",
        rest,
        re.DOTALL,
    )
    if phone_alias:
        candidate = phone_alias.group(2).strip()
        phone = _parse_private_phone_recipient(candidate)
        numeric_prefix = re.match(r"^[0-9][0-9\s().\-]*", candidate)
        short_numeric_body = numeric_prefix and len(re.sub(r"\D", "", numeric_prefix.group())) < 10
        if phone and "error" in phone and short_numeric_body:
            # A short code, date, or account number after a contact name is
            # ordinary message text. Explicit '+'/'(' phone attempts still
            # require a complete valid recipient instead of changing intent.
            phone = None
        if phone is not None:
            return {
                **phone,
                "label": phone_alias.group(1).strip(),
                "resolution_path": "phone_provided_for_alias",
                "store_alias": True,
            }

    marker = re.match(
        r"^(.{1,80}?)(?:\s+(?:1\s*on\s*1|1:1|one\s+on\s+one|privately|private|direct(?:ly)?|saying|says|that)|\s*[:\-])\s+(.+)$",
        rest,
        re.IGNORECASE | re.DOTALL,
    )
    if marker:
        return {
            "recipient": marker.group(1).strip(),
            "label": marker.group(1).strip(),
            "message": marker.group(2).strip(),
            "resolution_path": "command_marker",
        }

    parts = rest.split(None, 1)
    if len(parts) != 2:
        return None
    return {
        "recipient": parts[0].strip(),
        "label": parts[0].strip(),
        "message": parts[1].strip(),
        "resolution_path": "command_split",
    }


def handle_private_send_request(sender: str, text: str, originating_chat_id: str = "") -> str | None:
    parsed = parse_private_send_command(text)
    if not parsed:
        return None
    if "error" in parsed:
        return parsed["error"]
    return request_private_send_confirmation(
        sender=sender,
        recipient=parsed["recipient"],
        message=parsed["message"],
        scheduled_time_utc="",
        originating_chat_id=originating_chat_id,
        source=parsed.get("resolution_path", "command"),
        label=parsed.get("label") or parsed["recipient"],
        store_alias=bool(parsed.get("store_alias")),
    )


def handle_private_send_confirmation(sender: str, text: str, allow_password: bool = True) -> str | None:
    """Confirm/cancel a pending private send. Returns None when no pending send exists."""
    from .config import ADMIN_PASSWORD
    from .permissions import check_admin_password

    pending = _get_pending_private_send(sender)
    if not pending:
        return None

    clean = (text or "").strip()
    if clean.lower() in {"cancel", "stop", "nevermind", "never mind"}:
        _pending_private_sends.pop(_pending_key(sender), None)
        _log_send_imessage_call(
            pending.get("destination", ""), pending["message"], pending.get("scheduled_time_utc", ""),
            sender, pending.get("resolution_path", ""), event_type="private_send_cancelled",
            label=pending.get("label", ""),
        )
        return "Cancelled. No private message sent."

    if pending.get("awaiting_contact"):
        clarified_phone = _extract_phone(clean)
        if not clarified_phone:
            return f"I still need {pending.get('label', 'their')} phone number. Send +1XXXXXXXXXX, or say cancel."
        resolved = _resolve_private_destination(
            clarified_phone,
            label=pending.get("label", ""),
            source="phone_clarification",
        )
        if "error" in resolved:
            return resolved["error"]
        pending.update({
            "awaiting_contact": False,
            "destination": resolved["destination"],
            "resolution_path": resolved["resolution_path"],
            "store_alias": True,
            "expires_at": _utc_timestamp() + _PRIVATE_SEND_TTL_SECONDS,
        })
        _pending_private_sends[_pending_key(sender)] = pending
        _log_send_imessage_call(
            pending["destination"], pending["message"], pending.get("scheduled_time_utc", ""),
            sender, pending.get("resolution_path", ""), event_type="private_send_confirmation_requested",
            label=pending.get("label", ""),
        )
        return _private_send_confirmation_prompt(pending)

    if not allow_password:
        if check_admin_password(clean):
            return "Not sending from a group password reply. DM me the admin password to send, or say cancel."
        return None

    _pending_private_sends.pop(_pending_key(sender), None)
    if not ADMIN_PASSWORD:
        _log_send_imessage_call(
            pending["destination"], pending["message"], pending.get("scheduled_time_utc", ""),
            sender, pending.get("resolution_path", ""), event_type="private_send_denied",
            label=pending.get("label", ""), extra={"reason": "password_not_configured"},
        )
        return "Private 1-on-1 sends are disabled because the admin password is not configured."

    if not check_admin_password(clean):
        _log_send_imessage_call(
            pending["destination"], pending["message"], pending.get("scheduled_time_utc", ""),
            sender, pending.get("resolution_path", ""), event_type="private_send_denied",
            label=pending.get("label", ""), extra={"reason": "bad_password"},
        )
        return "Denied. Password did not match, so I did not send it."

    return _execute_confirmed_private_send(sender, pending)


def request_private_send_confirmation(
    sender: str,
    recipient: str,
    message: str,
    scheduled_time_utc: str = "",
    originating_chat_id: str = "",
    source: str = "tool",
    label: str = "",
    store_alias: bool = False,
) -> str:
    from .permissions import is_admin

    if not is_admin(sender):
        return "Permission denied - private 1-on-1 sends require admin access."
    if not (message or "").strip():
        return "What message do you want me to send?"

    resolved = _resolve_private_destination(recipient, label=label, source=source)
    if "error" in resolved:
        if _could_be_contact_alias(recipient):
            pending = {
                "awaiting_contact": True,
                "label": (label or recipient).strip(),
                "message": message.strip(),
                "scheduled_time_utc": scheduled_time_utc or "",
                "resolution_path": f"{source}:contact_missing",
                "originating_chat_id": originating_chat_id,
                "sender": sender,
                "expires_at": _utc_timestamp() + _PRIVATE_SEND_TTL_SECONDS,
            }
            _pending_private_sends[_pending_key(sender)] = pending
            _log_send_imessage_call(
                "", pending["message"], pending["scheduled_time_utc"], sender,
                pending["resolution_path"], event_type="private_send_contact_needed",
                label=pending["label"],
            )
            return f"I don't have {pending['label']}'s number. Reply with their phone number, or cancel."
        return resolved["error"]

    pending = {
        "destination": resolved["destination"],
        "label": resolved["label"],
        "message": message.strip(),
        "scheduled_time_utc": scheduled_time_utc or "",
        "resolution_path": resolved["resolution_path"],
        "store_alias": bool(store_alias and resolved.get("phone_provided")),
        "originating_chat_id": originating_chat_id,
        "sender": sender,
        "expires_at": _utc_timestamp() + _PRIVATE_SEND_TTL_SECONDS,
    }
    _pending_private_sends[_pending_key(sender)] = pending
    _log_send_imessage_call(
        pending["destination"], pending["message"], pending["scheduled_time_utc"],
        sender, pending["resolution_path"], event_type="private_send_confirmation_requested",
        label=pending["label"],
    )

    return _private_send_confirmation_prompt(pending)


def _private_send_confirmation_prompt(pending: dict) -> str:
    preview = _message_preview(pending["message"])
    masked = _mask_destination(pending["destination"])
    target = pending.get("label") or masked
    timing = f" scheduled for {pending['scheduled_time_utc']}" if pending.get("scheduled_time_utc") else ""
    if pending.get("originating_chat_id") and pending.get("originating_chat_id") != pending.get("sender"):
        instruction = "DM me the admin password to send, or reply cancel."
    else:
        instruction = "Reply with the admin password to send, or cancel."
    return f"Confirm 1-on-1 message to {target} at {masked}{timing}: \"{preview}\". {instruction}"


def _could_be_contact_alias(recipient: str) -> bool:
    raw = (recipient or "").strip()
    return bool(raw and "@" not in raw and not re.match(r"^\+?[\d\s\-().]{7,}$", raw) and not _looks_like_chat_guid(raw))


def _extract_phone(text: str) -> str | None:
    m = re.search(r"(\+?1?[\d][\d\s().\-]{9,}\d)", text or "")
    return m.group(1).strip() if m else None


def _resolve_private_destination(recipient: str, label: str = "", source: str = "") -> dict:
    from .config import normalize_handle

    raw = (recipient or "").strip()
    display = (label or raw).strip()
    if not raw:
        return {"error": "Who do you want me to text 1-on-1?"}

    lowered = raw.lower()
    if lowered in {"here", "this chat", "this group", "this gc", "self"} or _looks_like_chat_guid(raw):
        return {"error": "That is a chat route, not a private 1-on-1 recipient. Use public tell in the GC instead."}

    if "@" in raw:
        return {
            "destination": raw.lower(),
            "label": display or raw,
            "resolution_path": f"{source}:email",
            "phone_provided": False,
        }

    if re.match(r"^\+?[\d\s\-().]{7,}$", raw):
        return {
            "destination": normalize_handle(raw),
            "label": display if display and display != raw else "that number",
            "resolution_path": f"{source}:phone_provided",
            "phone_provided": True,
        }

    from .brain import resolve_contact
    resolved = resolve_contact(raw)
    if not resolved:
        return {
            "error": (
                f"I don't have {display or raw}'s number. "
                f"Use `msg {display or raw} +1XXXXXXXXXX [message]` or add the contact first."
            )
        }
    return {
        "destination": normalize_handle(resolved),
        "label": display or raw,
        "resolution_path": f"{source}:alias_match",
        "phone_provided": False,
    }


def _execute_confirmed_private_send(sender: str, pending: dict) -> str:
    destination = pending["destination"]
    message = pending["message"]
    scheduled_time_utc = pending.get("scheduled_time_utc", "")
    label = pending.get("label") or _mask_destination(destination)
    resolution_path = pending.get("resolution_path", "")

    if pending.get("store_alias"):
        try:
            from .permissions import is_owner
            if is_owner(sender):
                from .brain import store_user_fact
                store_user_fact(f"contact:{label.lower()}", destination, source="private_send_confirmation")
        except Exception as e:
            logger.warning("contact alias store skipped: %s", e)

    if scheduled_time_utc:
        task_id, time_str = _schedule_confirmed_private_send(destination, message, scheduled_time_utc, sender)
        _log_send_imessage_call(
            destination, message, scheduled_time_utc, sender, resolution_path,
            event_type="private_send_scheduled", label=label, extra={"task_id": task_id},
        )
        return f"Scheduled 1-on-1 message to {label} at {time_str}."

    success = _send_private_imessage(destination, message)
    event_type = "private_send_sent" if success else "private_send_failed"
    _log_send_imessage_call(
        destination, message, "", sender, resolution_path,
        event_type=event_type, label=label,
    )
    if success:
        logger.info("Sent private iMessage to %s", _mask_destination(destination))
        return f"Sent 1-on-1 message to {label}."
    return f"Failed to send to {label} - check that the number/email is correct."


def _schedule_confirmed_private_send(destination: str, message: str, scheduled_time_utc: str, sender: str) -> tuple[int, str]:
    from datetime import timezone

    ts = scheduled_time_utc.strip().rstrip("Z").replace("T", " ")[:19]
    with connect_bot_db(BOT_DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_tasks (task_type, recipient, message, scheduled_at, chat_id, sender)"
            " VALUES ('send_imessage', ?, ?, ?, NULL, ?)",
            (destination, message, ts, sender),
        )
        task_id = cur.lastrowid

    from zoneinfo import ZoneInfo
    _LA = ZoneInfo("America/Los_Angeles")
    try:
        dt_la = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(_LA)
        time_str = dt_la.strftime("%b %d %I:%M %p %Z")
    except ValueError:
        time_str = scheduled_time_utc
    return int(task_id), time_str


def _send_private_imessage(destination: str, message: str) -> bool:
    from .imessage import send_message as _imsg
    return bool(_imsg(destination, message, is_group=False))


def _send_imessage(recipient: str, message: str, scheduled_time_utc: str = "",
                   sender: str = "", originating_chat_id: str = "") -> str:
    """Prepare a private 1:1 iMessage. Never sends until password confirmation."""
    return request_private_send_confirmation(
        sender=sender,
        recipient=recipient,
        message=message,
        scheduled_time_utc=scheduled_time_utc,
        originating_chat_id=originating_chat_id,
        source="tool",
    )


def _log_send_imessage_call(
    recipient: str,
    message: str,
    scheduled_time_utc: str,
    sender: str,
    resolution_path: str = "",
    event_type: str = "send_imessage_call",
    label: str = "",
    extra: dict | None = None,
) -> None:
    """Persist private-send routing metadata without storing message bodies."""
    from datetime import datetime, timezone

    payload = {
        "recipient_masked": _mask_destination(recipient),
        "recipient_hash": _destination_hash(recipient),
        "recipient_label": (label or "")[:80],
        "message_hash": _message_hash(message),
        "message_len": len(message or ""),
        "scheduled_time_utc": scheduled_time_utc,
        "resolution_path": resolution_path,
        "now_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        payload.update(extra)
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (sender, event_type, json.dumps(payload)),
            )
    except Exception as e:
        logger.warning("send_imessage call-log failed: %s", e)


def _send_contact_card(email: str) -> str:
    """Email Davos.vcf via SMTP. Stage 5 implementation."""
    import smtplib
    import os as _os
    from email.message import EmailMessage
    from .config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_ADDRESS

    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM_ADDRESS):
        return "SMTP not configured. Set SMTP_HOST/USER/PASSWORD/FROM_ADDRESS in .env."

    vcf_path = _os.path.join(_PROJECT_DIR, "generated", "Davos.vcf")
    if not _os.path.exists(vcf_path):
        return f"Davos.vcf not found at {vcf_path}."

    msg = EmailMessage()
    msg["From"] = SMTP_FROM_ADDRESS
    msg["To"] = email
    msg["Subject"] = "DavosBot contact card"
    msg.set_content("Save this contact so you can DM DavosBot.")
    with open(vcf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="text", subtype="vcard", filename="Davos.vcf")

    try:
        port = int(SMTP_PORT or 465)
        if port == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, port, timeout=20) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, port, timeout=20) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        return f"Sent contact card to {email}."
    except Exception as e:
        return f"SMTP error: {e}"


def _get_inspirational_quote() -> str:
    return _morning_quote_helpers._get_inspirational_quote(
        gemini_api_key=GEMINI_API_KEY,
        rewrite_fn=_gemini_rewrite,
        recent_hashes_fn=_recent_quote_hashes,
        log_choice_fn=_log_quote_choice,
        logger_obj=logger,
    )


def _log_quote_choice(date_key: str, source: str, quote: str) -> None:
    return _morning_quote_helpers._log_quote_choice(
        date_key,
        source,
        quote,
        db_path=BOT_DB_PATH,
        logger_obj=logger,
    )


def _recent_quote_hashes(date_key: str) -> set[str]:
    return _morning_quote_helpers._recent_quote_hashes(
        date_key,
        db_path=BOT_DB_PATH,
        logger_obj=logger,
    )


def _quote_seen_recently(date_key: str, quote: str) -> bool:
    return _quote_hash(quote) in _recent_quote_hashes(date_key)


def _scan_file(filename: str) -> str:
    """Code-review a file via Gemini. Stage 5 implementation."""
    safe_root = os.path.realpath(_PROJECT_DIR)
    candidate = os.path.realpath(os.path.join(_PROJECT_DIR, filename))
    if not candidate.startswith(safe_root + os.sep) and candidate != safe_root:
        return "I can only scan files inside the davosbot directory."
    if not os.path.exists(candidate):
        return f"File not found: {filename}"
    try:
        with open(candidate, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"Couldn't read {filename}: {e}"
    prompt = (
        "You are a senior Python code reviewer. Review this file for bugs, "
        "security issues, and improvements. Max 10 bullet points.\n\n"
        f"FILE: {filename}\n```\n{content[:30000]}\n```"
    )
    try:
        return _gemini_rewrite(prompt)
    except Exception as e:
        return f"scan failed: {e}"


def _query_legacy_workout(args: dict) -> str:
    return _workout_helpers.query_legacy_workout(args, db_path=BOT_DB_PATH)


def _query_canonical_workout(conn: sqlite3.Connection, args: dict, sender: str) -> str | None:
    return _workout_helpers.query_canonical_workout(conn, args, sender)


def _query_workout(args: dict, sender: str = "") -> str:
    return _workout_helpers.query_workout(args, sender, db_path=BOT_DB_PATH)
