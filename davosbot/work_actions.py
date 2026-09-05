"""Explicit owner operations for Work; no command, shell, or SQL dispatcher.

The transport authenticates requests and persists execution IDs. This layer
validates each operation again, binds the actor locally, and never retries a
side effect. Reminder/cron snapshot mutations use one SQLite transaction so
an intervening native iMessage edit cannot change the selected object.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import sqlite3
import time
from .runtime_locks import schedule_locked

MAX_RESULT = 8000
MAX_ROWS = 100
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_DAYS = {"", "mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_CRON_ACTIONS = {"morning_message", "drift_check", "sports_recap"}
_DIAGNOSTICS = {
    "status": "_cmd_status", "uptime": "get_uptime", "drift": "_cmd_drift",
    "billing": "_cmd_billing", "api": "_cmd_api_status",
    "model_status": "_cmd_model_status", "model_options": "_cmd_model_options",
    "model_intensity": "_cmd_model_intensity", "cleanup": "_cmd_cleanup_status",
}


class _TransactionRejected(ValueError):
    """A failed precondition from a transaction which was fully rolled back."""


def _spec(description, fields=None, required=(), mutates=False):
    return {"description": description, "fields": fields or {},
            "required": list(required), "mutates": mutates}


ACTIONS = {"capabilities": _spec("List operation schemas in bounded pages, or inspect one action.",
                                {"offset": "integer:0..1000", "limit": "integer:1..200", "action": "operation name"})}
for _name in _DIAGNOSTICS:
    ACTIONS["diagnostics." + _name] = _spec("Read " + _name.replace("_", " ") + ".")
ACTIONS.update({
    "requests.receipt": _spec("Read a retained request's recorded outcome without repeating its action.",
                              {"request_id": "canonical UUID4", "request_comment_id": "positive integer"},
                              ("request_id", "request_comment_id")),
    "notify.self": _spec("Send one owner-only notification; receipt may remain pending.",
                         {"message": "text:1..2000"}, ("message",), True),
    "reminders.list": _spec("Read pending owner reminders and their snapshot."),
    "reminders.create": _spec("Create an owner reminder.",
                              {"message": "text:1..1000", "due_at": "ISO8601 with timezone"},
                              ("message", "due_at"), True),
    "reminders.cancel": _spec("Cancel the exact reminder from a fresh snapshot.",
                              {"id": "positive integer", "snapshot": "sha256"},
                              ("id", "snapshot"), True),
    "reminders.edit": _spec("Edit the exact reminder atomically, preserving its origin.",
                            {"id": "positive integer", "snapshot": "sha256",
                             "message": "text:1..1000", "due_at": "ISO8601 with timezone"},
                            ("id", "snapshot"), True),
    "crons.list": _spec("Read active owner jobs; all-chat scope must be explicit.", {"scope": "owner|all"}),
    "crons.create": _spec("Create a native recurring job to the owner DM.",
                          {"time_pt": "HH:MM", "day_of_week": "mon..sun or empty",
                           "action": "morning_message|drift_check|sports_recap",
                           "intro": "text:0..1000", "intro_mode": "fixed|rotate|clear"},
                          ("time_pt", "action"), True),
    "crons.edit": _spec("Edit an active job without changing its destination.",
                        {"id": "positive integer", "snapshot": "sha256", "scope": "owner|all", "time_pt": "HH:MM",
                         "day_of_week": "mon..sun or empty", "action": "morning_message|drift_check|sports_recap",
                         "intro": "text:0..1000", "intro_mode": "fixed|rotate|clear"},
                        ("id", "snapshot"), True),
    "crons.cancel": _spec("Disable the exact active job; never revive old jobs.",
                          {"id": "positive integer", "snapshot": "sha256", "scope": "owner|all"}, ("id", "snapshot"), True),
    "market.status": _spec("Read native market watch status."),
    "market.quote": _spec("Read a quote or native market snapshot.",
                          {"symbols": "list of 1..12 tickers", "view": "snapshot|quote|movers"}),
    "market.alerts": _spec("Read or change the native market alert toggle.",
                           {"enabled": "boolean"}, (), True),
    "model.request": _spec("Record a model change for review; does not change the live model.",
                           {"goal": "text:1..1000"}, ("goal",), True),
})


def _extras():
    try:
        from . import work_actions_extra
        return work_actions_extra
    except ImportError:
        return None


def action_catalogue():
    result = dict(ACTIONS)
    extra = _extras()
    if extra is not None:
        result.update(extra.EXTRA_ACTIONS)
    return result


def _fail(code):
    raise ValueError(code)


def _text(value, maximum, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or
            (not empty and not value.strip()) or
            any(ord(c) < 32 and c not in "\n\t" or 127 <= ord(c) <= 159 for c in value)):
        _fail("invalid_text")


def _due(value, *, future=False):
    _text(value, 40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _fail("timezone_required")
        parsed = parsed.astimezone(timezone.utc)
        if future and parsed <= datetime.now(timezone.utc):
            _fail("time_not_future")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, TypeError, ValueError):
        _fail("invalid_future_time" if future else "invalid_time")


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        _fail("invalid_clock")


def validate_action(action, args):
    if not isinstance(action, str) or not isinstance(args, dict):
        _fail("invalid_action")
    if action not in ACTIONS:
        extra = _extras()
        if extra is None or action not in extra.EXTRA_ACTIONS:
            _fail("unsupported_action")
        extra.validate_extra_action(action, args)
        return
    spec = ACTIONS[action]
    if set(args) - set(spec["fields"]) or set(spec["required"]) - set(args):
        _fail("invalid_fields")
    if "id" in args and (type(args["id"]) is not int or not 1 <= args["id"] <= 2**53):
        _fail("invalid_id")
    if action == "requests.receipt":
        from .work_bridge import _uuid4
        if not _uuid4(args["request_id"]):
            _fail("invalid_request_id")
        if type(args["request_comment_id"]) is not int or not 1 <= args["request_comment_id"] <= 2**53:
            _fail("invalid_comment_id")
    if "snapshot" in args and (not isinstance(args["snapshot"], str) or not _HEX.fullmatch(args["snapshot"])):
        _fail("invalid_snapshot")
    if "message" in args:
        _text(args["message"], 2000 if action == "notify.self" else 1000)
    if "due_at" in args:
        _due(args["due_at"])
    if "time_pt" in args:
        _clock(args["time_pt"])
    if "day_of_week" in args and (not isinstance(args["day_of_week"], str) or args["day_of_week"] not in _DAYS):
        _fail("invalid_day")
    if "action" in args and action.startswith("crons.") and (not isinstance(args["action"], str) or args["action"] not in _CRON_ACTIONS):
        _fail("invalid_cron_action")
    if "intro" in args:
        _text(args["intro"], 1000, empty=True)
    if "intro_mode" in args and (not isinstance(args["intro_mode"], str) or args["intro_mode"] not in {"fixed", "rotate", "clear"}):
        _fail("invalid_intro_mode")
    if action.endswith(".edit") and not (set(args) - {"id", "snapshot", "scope"}):
        _fail("no_changes")
    if "scope" in args and (not isinstance(args["scope"], str) or args["scope"] not in {"owner", "all"}):
        _fail("invalid_scope")
    if action == "capabilities":
        for key, low, high in (("offset", 0, 1000), ("limit", 1, 200)):
            if key in args and (type(args[key]) is not int or not low <= args[key] <= high):
                _fail("invalid_pagination")
        if "action" in args and (not isinstance(args["action"], str) or args["action"] not in action_catalogue()):
            _fail("unsupported_action")
    if "enabled" in args and type(args["enabled"]) is not bool:
        _fail("invalid_boolean")
    if "goal" in args:
        _text(args["goal"], 1000)
    if "view" in args and (not isinstance(args["view"], str) or args["view"] not in {"snapshot", "quote", "movers"}):
        _fail("invalid_view")
    if "symbols" in args:
        symbols = args["symbols"]
        if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 12 or
                any(not isinstance(s, str) or not re.fullmatch(r"\^?[A-Z][A-Z0-9.=-]{0,11}", s) for s in symbols)):
            _fail("invalid_symbols")


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _clean(value, depth=0, *, max_depth=8):
    """Keep responses bounded and redact credentials even from native diagnostics."""
    from .permissions import redact_secret
    if depth > max_depth:
        return "[truncated]"
    if isinstance(value, str):
        value = redact_secret(value)
        value = re.sub(r"(?i)\b(bearer)\s+\S+", r"\1 [redacted]", value)
        value = re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", value)
        value = re.sub(r"https?://[^/\s:@]+:[^/\s@]+@", "https://[redacted]@", value)
        return value[:MAX_RESULT]
    if isinstance(value, dict):
        return {str(k)[:100]: _clean(v, depth + 1, max_depth=max_depth) for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1, max_depth=max_depth) for v in value[:MAX_ROWS]]
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return "[unsupported value]"


def _reply(result, status="ok", **evidence):
    return {"status": status, "result": result, "evidence": evidence}


def _connection():
    from .config import BOT_DB_PATH
    from pathlib import Path
    conn = sqlite3.connect(Path(BOT_DB_PATH).resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _reminder_rows(conn, owner):
    rows = conn.execute(
        "SELECT id,message,due_ts,COALESCE(sent,0) AS sent,COALESCE(send_attempts,0) AS attempts,"
        "chat_id,COALESCE(origin_chat_id,'') AS origin_chat_id FROM reminders "
        "WHERE (origin_chat_id=? OR (COALESCE(origin_chat_id,'')='' AND chat_id=?)) "
        "AND (sent=0 OR (sent=1 AND COALESCE(send_attempts,0)>=4)) ORDER BY due_ts,id LIMIT ?",
        (owner, owner, MAX_ROWS + 1)).fetchall()
    if len(rows) > MAX_ROWS:
        _fail("too_many_reminders")
    return [dict(row) for row in rows]


def _cron_rows(conn, owner, scope="owner"):
    if scope == "owner":
        # json_valid avoids JSON1 errors on an unrelated legacy malformed row.
        rows = conn.execute("SELECT id,cron_expression,action_type,action_payload,enabled FROM cron_jobs "
                            "WHERE enabled=1 AND CASE WHEN json_valid(action_payload) "
                            "THEN json_extract(action_payload,'$.recipient')=? ELSE 0 END "
                            "ORDER BY id LIMIT ?", (owner, MAX_ROWS + 1)).fetchall()
    else:
        rows = conn.execute("SELECT id,cron_expression,action_type,action_payload,enabled FROM cron_jobs "
                            "WHERE enabled=1 ORDER BY id LIMIT ?", (MAX_ROWS + 1,)).fetchall()
    if len(rows) > MAX_ROWS:
        _fail("too_many_crons")
    return [dict(row) for row in rows]


def _snapshot(kind, rows, owner):
    return hashlib.sha256(_canonical({"kind": kind, "owner": owner, "rows": rows}).encode()).hexdigest()


def _reminder_view(rows):
    return [{"id": r["id"], "message": r["message"], "due_at": r["due_ts"] + "Z",
             "delivery_failed": bool(r["sent"]), "attempts": r["attempts"]} for r in rows]


def _cron_view(rows, owner):
    result = []
    for row in rows:
        try:
            payload = json.loads(row["action_payload"] or "{}")
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, TypeError):
            payload = {}
        target = payload.get("recipient", "")
        result.append({"id": row["id"], "time_pt": row["cron_expression"], "action": row["action_type"],
                       "destination": "owner" if target == owner else "group" if re.fullmatch(r"[a-fA-F0-9]{32}", str(target)) else "other",
                       "intro": str(payload.get("intro", ""))[:1000], "intro_mode": payload.get("intro_mode", "")})
    return result


def _list_records(kind, owner, scope="owner"):
    with closing(_connection()) as conn:
        rows = _reminder_rows(conn, owner) if kind == "reminders" else _cron_rows(conn, owner, scope)
    view = _reminder_view(rows) if kind == "reminders" else _cron_view(rows, owner)
    return _reply("Current " + kind + ".", snapshot=_snapshot(kind + ":" + scope, rows, owner), records=view, scope=scope)


@schedule_locked
def _mutate_snapshot(kind, action, args, owner):
    scope = args.get("scope", "owner")
    may_be_in_flight = False
    with closing(_connection()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = _reminder_rows(conn, owner) if kind == "reminders" else _cron_rows(conn, owner, scope)
            if args["snapshot"] != _snapshot(kind + ":" + scope, rows, owner):
                _fail("stale_snapshot")
            row = next((r for r in rows if r["id"] == args["id"]), None)
            if row is None:
                _fail("record_not_found")
            if kind == "reminders":
                if action == "cancel":
                    may_be_in_flight = (not row["sent"] and
                        datetime.strptime(row["due_ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc))
                    conn.execute("DELETE FROM reminders WHERE id=?", (row["id"],))
                else:
                    if row["sent"]:
                        _fail("failed_reminder_requires_recreate")
                    if datetime.strptime(row["due_ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
                        _fail("reminder_already_due")
                    due = _due(args["due_at"], future=True) if "due_at" in args else row["due_ts"]
                    if datetime.strptime(due, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
                        _fail("reminder_already_due")
                    conn.execute("UPDATE reminders SET message=?,due_ts=? WHERE id=?",
                                 (args.get("message", row["message"]), due, row["id"]))
            elif action == "cancel":
                conn.execute("UPDATE cron_jobs SET enabled=0 WHERE id=?", (row["id"],))
            else:
                try:
                    payload = json.loads(row["action_payload"] or "{}")
                    if not isinstance(payload, dict) or not isinstance(payload.get("recipient"), str) or not payload["recipient"]:
                        raise ValueError
                except (ValueError, TypeError):
                    _fail("invalid_existing_cron")
                old = row["cron_expression"].split()
                if not old or len(old) > 2:
                    _fail("invalid_existing_cron")
                hour = args.get("time_pt", old[0])
                day = args.get("day_of_week", old[1] if len(old) == 2 else "")
                _clock(hour)
                if day not in _DAYS:
                    _fail("invalid_existing_cron")
                job_action = args.get("action", row["action_type"])
                if job_action not in _CRON_ACTIONS:
                    _fail("unsupported_existing_cron")
                mode = args.get("intro_mode", "")
                if mode == "rotate":
                    payload.pop("intro", None)
                    payload["intro_mode"] = "rotate"
                elif mode == "clear":
                    payload.pop("intro", None)
                    payload.pop("intro_mode", None)
                elif "intro" in args:
                    payload.update(intro=args["intro"].strip(), intro_mode="fixed")
                if job_action != "morning_message":
                    payload.pop("intro", None)
                    payload.pop("intro_mode", None)
                conn.execute("UPDATE cron_jobs SET cron_expression=?,action_type=?,action_payload=? WHERE id=?",
                             (hour + (" " + day if day else ""), job_action, _canonical(payload), row["id"]))
        except ValueError as exc:
            conn.rollback()
            raise _TransactionRejected(str(exc)) from None
        except Exception:
            conn.rollback()
            raise
        # A commit/readback failure is not a precondition rejection: the write
        # may already be durable, so execute_action must report ambiguity.
        conn.commit()
    output = _list_records(kind, owner, scope)
    output["result"] = "Cancelled." if action == "cancel" else "Updated."
    output["evidence"]["id"] = args["id"]
    if may_be_in_flight:
        output.update(status="accepted", result="Cancelled future reminder retries; an already-running send may still finish.")
        output["evidence"]["delivery_may_be_in_flight"] = True
    return output


@schedule_locked
def _create_reminder(args, owner):
    from .reminder_tools import _set_reminder
    due = _due(args["due_at"], future=True)
    with closing(_connection()) as conn:
        before = conn.execute("SELECT COALESCE(MAX(id),0) FROM reminders").fetchone()[0]
    result = _set_reminder(args["message"], due, originating_chat_id=owner)
    with closing(_connection()) as conn:
        row = conn.execute("SELECT id FROM reminders WHERE id>? AND message=? AND due_ts=? "
                           "AND origin_chat_id=? AND chat_id=? AND sent=0 ORDER BY id DESC LIMIT 1",
                           (before, args["message"], due, owner, owner)).fetchone()
    if not row:
        return _reply("Reminder creation was not verified; do not repeat automatically.", "error", code="reminder_not_verified", ambiguous=True)
    return _reply(result, id=row[0], due_at=due + "Z")


@schedule_locked
def _create_cron(args, owner):
    from .tools import _schedule_cron
    with closing(_connection()) as conn:
        before = conn.execute("SELECT COALESCE(MAX(id),0) FROM cron_jobs").fetchone()[0]
    result = _schedule_cron(args["time_pt"], args.get("intro", ""), action=args["action"],
                            day_of_week=args.get("day_of_week", ""), intro_mode=args.get("intro_mode", ""),
                            originating_chat_id=owner)
    with closing(_connection()) as conn:
        rows = _cron_rows(conn, owner)
    schedule = args["time_pt"] + (" " + args["day_of_week"] if args.get("day_of_week") else "")
    found = []
    for row in rows:
        try:
            payload = json.loads(row["action_payload"] or "{}")
            if (row["id"] > before and row["cron_expression"] == schedule and
                    row["action_type"] == args["action"] and payload.get("recipient") == owner):
                found.append(row)
        except (ValueError, TypeError, AttributeError):
            continue
    if not found:
        return _reply("Recurring job creation was not verified; do not repeat automatically.", "error", code="cron_not_verified", ambiguous=True)
    return _reply(result, id=found[-1]["id"], snapshot=_snapshot("crons:owner", rows, owner), records=_cron_view(rows, owner))


def _notify(args, owner):
    from .config import DB_PATH, normalize_handle
    from .imessage import send_message
    from .text_safety import normalize_bot_text
    text = normalize_bot_text(args["message"])
    from pathlib import Path
    with closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        watermark = conn.execute("SELECT COALESCE(MAX(ROWID),0) FROM message").fetchone()[0]
    # Do not pre-deduplicate by text: two separately authorized identical messages
    # are distinct requests. Transport request IDs provide durable deduplication.
    accepted = send_message(owner, text, is_group=False, recovery_mode="none")
    deadline = time.monotonic() + 3
    state = "unknown"
    while True:
        state = _new_message_state(DB_PATH, owner, text, normalize_handle, watermark)
        if state == "sent" or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if state == "sent":
        return _reply("Owner notification is marked sent by Messages.", message_state=state)
    return _reply("Owner notification delivery is not yet confirmed.",
                  "error" if state == "failed" or accepted is False and state == "unknown" else "accepted", message_state=state,
                  accepted_by_sender=accepted is True, ambiguous=state == "unknown")


def _new_message_state(db_path, owner, text, normalize, watermark):
    """Verify this attempt only; historical identical text cannot prove a send."""
    from pathlib import Path
    from .message_body import decode_attributed_body
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
        body = "m.attributedBody" if "attributedBody" in columns else "NULL"
        rows = conn.execute(f"SELECT m.text,{body},m.is_sent,m.error,c.chat_identifier "
                            "FROM message m JOIN chat_message_join j ON j.message_id=m.ROWID "
                            "JOIN chat c ON c.ROWID=j.chat_id WHERE m.ROWID>? AND m.is_from_me=1 "
                            "ORDER BY m.ROWID DESC LIMIT 101", (watermark,)).fetchall()
    if len(rows) > 100:
        return "unknown"
    states = []
    for raw, attributed, sent, error, recipient in rows:
        if normalize(recipient or "") != normalize(owner):
            continue
        actual = raw if raw is not None else decode_attributed_body(attributed)
        if actual != text:
            continue
        states.append("sent" if sent == 1 and error == 0 else "failed" if isinstance(error, int) and error else "pending")
    return next((state for state in ("sent", "pending", "failed") if state in states), "unknown")


def _request_receipt(args):
    """Read only bounded outcome metadata after execute_action's owner gate."""
    from .config import PROJECT_ROOT
    from .work_bridge import BridgeError, _load
    evidence = {
        "request_id": args["request_id"], "request_comment_id": args["request_comment_id"],
        "observed_at": datetime.now(timezone.utc).isoformat(), "receipt_state": "unknown",
    }
    try:
        journal_root = PROJECT_ROOT / ".work_bridge"
        if journal_root.is_symlink():
            raise BridgeError("unsafe_state_path")
        record = _load(journal_root / "state.json")["records"].get(args["request_id"])
    except (BridgeError, OSError, ValueError, TypeError):
        record = None
    if record is None or record["comment_id"] != args["request_comment_id"]:
        return _reply("No matching retained receipt is available. The action may have run; do not repeat it automatically.",
                      **evidence)
    if record["phase"] != "finished":
        return _reply("The request was recorded, but its completion is unknown. Do not repeat it automatically.",
                      **evidence)
    response = record["response"]
    result = response["result"]
    action_status = result.get("status")
    if not isinstance(action_status, str) or action_status not in {"ok", "error", "accepted", "native_confirmation_required"}:
        action_status = "unknown"
    published = record["published_comment_id"]
    evidence.update(
        receipt_state="recorded", request_state=response["state"], action_status=action_status,
        publication_state="published" if published is not None else "pending",
        published_comment_id=published,
    )
    revision = response.get("runtime_revision")
    evidence["runtime_revision"] = revision if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40,64}", revision) else "unknown"
    completed = response.get("completed_at")
    try:
        parsed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        evidence["completed_at"] = parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        evidence["completed_at"] = None
    confirmation = result.get("evidence")
    if isinstance(confirmation, dict):
        saved = {}
        if isinstance(confirmation.get("message_state"), str) and confirmation["message_state"] in {"sent", "pending", "failed", "unknown"}:
            saved["message_state"] = confirmation["message_state"]
        for key in ("delivery_confirmed", "accepted_by_sender", "ambiguous", "review_only"):
            if type(confirmation.get(key)) is bool:
                saved[key] = confirmation[key]
        if saved:
            evidence["saved_confirmation"] = saved
    return _reply("Found the saved receipt. Its action status and publication status are separate; this lookup does not rerun or recheck delivery.",
                  **evidence)


def _execute(action, args, owner):
    if action == "requests.receipt":
        return _request_receipt(args)
    if action == "capabilities":
        catalogue = action_catalogue()
        if "action" in args:
            return _reply("Operation schema.", actions={args["action"]: catalogue[args["action"]]})
        names = sorted(catalogue)
        offset, limit = args.get("offset", 0), args.get("limit", 200)
        selected = names[offset:offset + limit]
        return _reply("Implemented operation schemas.", actions={name: catalogue[name] for name in selected},
                      total=len(names), next_offset=offset + len(selected) if offset + len(selected) < len(names) else None)
    if action.startswith("diagnostics."):
        from . import commands
        result = getattr(commands, _DIAGNOSTICS[action.split(".", 1)[1]])(owner)
        return _reply(result)
    if action == "notify.self":
        return _notify(args, owner)
    if action in {"reminders.list", "crons.list"}:
        return _list_records(action.split(".")[0], owner, args.get("scope", "owner"))
    if action == "reminders.create":
        return _create_reminder(args, owner)
    if action == "crons.create":
        return _create_cron(args, owner)
    if action in {"reminders.edit", "reminders.cancel", "crons.edit", "crons.cancel"}:
        kind, verb = action.split(".")
        return _mutate_snapshot(kind, verb, args, owner)
    if action.startswith("market."):
        from . import market
        if action == "market.status":
            return _reply(market.market_status_summary())
        if action == "market.quote":
            return _reply(market.get_market_data(view=args.get("view", "snapshot"), symbols=args.get("symbols")))
        if "enabled" not in args:
            return _reply(market.market_status_summary(), enabled=market.market_alerts_enabled())
        result = market.set_market_alerts_enabled(args["enabled"])
        actual = market.market_alerts_enabled(force=True)
        return _reply(result, "ok" if actual == args["enabled"] else "error", enabled=actual)
    if action == "model.request":
        from .commands import _cmd_model_request
        with closing(_connection()) as conn:
            before = conn.execute("SELECT COALESCE(MAX(id),0) FROM change_log").fetchone()[0]
        result = _cmd_model_request("model request " + args["goal"], owner)
        matched = re.search(r"Model request logged #(\d+)", result or "")
        with closing(_connection()) as conn:
            row = conn.execute("SELECT id FROM change_log WHERE id=? AND id>? AND request LIKE '[MODEL-CHANGE %' "
                               "AND instr(reason,'status=review_only')>0",
                               (int(matched[1]) if matched else 0, before)).fetchone()
        if not row:
            return _reply("Model change request was not verified; do not repeat automatically.", "error", code="model_request_not_verified", ambiguous=True)
        return _reply(result, review_only=True, id=row[0])
    extra = _extras()
    if extra is not None and action in extra.EXTRA_ACTIONS:
        return extra.execute_extra_action(action, args, owner)
    _fail("unsupported_action")


def execute_action(action, args, owner=None):
    """Execute one authenticated owner action; failures contain no raw exceptions."""
    from .config import OWNER_ID, normalize_handle
    from .permissions import is_owner
    actor = OWNER_ID if owner is None else owner
    if not isinstance(actor, str) or not OWNER_ID or normalize_handle(actor) != OWNER_ID or not is_owner(actor):
        return _reply("Owner authorization required.", "error", code="owner_required")
    try:
        validate_action(action, args)
    except ValueError as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "invalid_request"
        return _reply("Operation was rejected before execution.", "error", code=code)
    except Exception:
        return _reply("Operation validation failed before execution.", "error", code="validation_failed")
    mutates = bool(action_catalogue().get(action, {}).get("mutates"))
    try:
        # Nested capability schemas need the transport's bounded depth of 12.
        reply = _clean(_execute(action, args, OWNER_ID), max_depth=12 if action == "capabilities" else 8)
        if not isinstance(reply, dict) or reply.get("status") not in {"ok", "error", "native_confirmation_required", "accepted"}:
            return _reply("Operation returned an invalid result; do not repeat automatically.", "error", code="invalid_result", ambiguous=mutates)
        # Preserve the outcome and precondition when trimming large readbacks.
        while len(_canonical(reply).encode()) > (40000 if action == "capabilities" else 7800):
            evidence = reply.get("evidence", {})
            records = evidence.get("records")
            if isinstance(records, list) and records:
                evidence.setdefault("total_records", len(records))
                records.pop()
                evidence["truncated"] = True
            elif isinstance(reply.get("result"), str) and len(reply["result"]) > 1000:
                reply["result"] = reply["result"][:len(reply["result"]) // 2]
                evidence["truncated"] = True
            else:
                return _reply("Operation result exceeds the response limit.", "error", code="result_too_large", ambiguous=bool(action_catalogue().get(action, {}).get("mutates")))
        return reply
    except _TransactionRejected as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "invalid_request"
        return _reply("Operation was rejected; its transaction was rolled back.", "error", code=code)
    except ValueError as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "operation_failed"
        return _reply("Operation could not be verified; do not repeat automatically.", "error", code=code, ambiguous=mutates)
    except Exception:
        return _reply("Operation could not be verified; do not repeat automatically.", "error", code="operation_failed", ambiguous=mutates)
