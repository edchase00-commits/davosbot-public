"""Owner-created public research schedules; destination comes only from runtime."""

from datetime import date, datetime, timedelta
from dataclasses import dataclass
import json
import re
import time
from threading import RLock
from zoneinfo import ZoneInfo

from .db import connect_bot_db
from .tool_outcomes import ToolOutcome


PACIFIC = ZoneInfo("America/Los_Angeles")
ACTION = "research_report"
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY = r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_MONTH = "(?:" + "|".join(month[:3] + "(?:" + month[3:] + ")?" if len(month) > 3 else month for month in _MONTHS) + ")"
_DATE = rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|(?:the\s+)?first\s+{_DAY}\s+(?:(?:in|of)\s+)?{_MONTH}(?:\s+\d{{4}})?|(?:(?:this\s+)?upcoming|next|this)\s+{_DAY})"
_CREATE = re.compile(r"^(?:(?:hey\s+)?davos[,!:]?\s+|@davos[,!:]?\s+)?(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:create|schedule|set\s+up|setup|start|add|make|send|give|new\s+(?:cron|automation)|i\s+(?:want|would like))\b", re.I)
_REPORT = re.compile(r"\b(?:research|reports?|briefings?|digests?|waivers?|waiver[ -]?wire)\b", re.I)
_TIME = re.compile(r"(?<![\w:])(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2})(?!\w)", re.I)
_FIELDS = {"version", "kind", "research_topic", "instructions", "top_n", "faab_budget", "recipient", "start_date", "end_date", "run"}
_DRAFT_TTL_SECONDS = 300
_pending: dict[tuple[str, str], dict] = {}
_draft_lock = RLock()


@dataclass(frozen=True)
class _MissingField:
    field: str
    text: str


def _clean_followup(text: str) -> str:
    raw = re.sub(r"^@davos\b[:,]?\s*", "", (text or "").strip(), flags=re.I)
    raw = re.sub(r"^(?:let['’]s\s+(?:do|use)|(?:make|set)\s+it(?:\s+for)?)\s+", "", raw, flags=re.I)
    return re.sub(r"\s+(?:please|works(?:\s+for\s+me)?)\s*[.!]?$", "", raw, flags=re.I).strip(" .!")


def _field_answer(field: str, text: str) -> str | None:
    """Only the requested slot can be supplied by an unmentioned continuation."""
    raw = _clean_followup(text)
    if field == "weekday" and re.fullmatch(rf"(?:every\s+|on\s+)?{_DAY}s?", raw, re.I):
        return " every " + re.sub(r"^(?:every|on)\s+", "", raw, flags=re.I)
    if field == "time" and re.fullmatch(r"(?:at\s+)?(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2})(?:\s+(?:PT|PST|PDT|Pacific))?", raw, re.I):
        return " at " + re.sub(r"^at\s+", "", raw, flags=re.I)
    if field == "budget" and re.fullmatch(r"(?:(?:faab(?:\s+budget)?|budget)(?:\s+is)?\s*[:=]?\s*)?\$?\d{1,5}(?:\s+dollars)?", raw, re.I):
        return " FAAB budget $" + re.search(r"\d+", raw)[0]
    if field == "topic" and re.fullmatch(r"(?:about|on|research)\s+.{2,600}", raw, re.I):
        return " report about " + re.sub(r"^(?:about|on|research)\s+", "", raw, flags=re.I)
    return None


def _generic_handoff(sender: str, recipient: str, text: str) -> str | None:
    from . import cron_creation
    draft = cron_creation.pending_draft(sender, recipient)
    raw = _clean_followup(text)
    if not draft or draft.get("action") or len(raw) > 1000 or not _REPORT.search(raw):
        return None
    if re.match(r"^(?:why|how|what|can|could|would|don't|do not|never|cancel|stop|delete|forget)\b", raw, re.I):
        return None
    if not re.match(r"^(?:a |an |weekly |public |fantasy |nfl |top \d+ )*(?:research|report|briefing|digest|waivers?|waiver[ -]?wire)\b", raw, re.I):
        return None
    # Preserve a previously supplied cadence, including an unsupported daily
    # research request, so handoff cannot silently turn it into a weekly job.
    cadence = "daily" if draft.get("weekly") is False else "weekly"
    combined = "Create a " + cadence + " " + raw
    if draft.get("time_pt") and not _TIME.search(raw):
        combined += " at " + draft["time_pt"] + " PT"
    if draft.get("day_of_week") and not re.search(rf"\b{_DAY}s?\b", raw, re.I):
        combined += " every " + draft["day_of_week"]
    return combined


def clear_draft(sender: str, recipient: str) -> bool:
    """Discard only ephemeral same-actor/chat setup, never a saved cron."""
    from . import cron_creation
    with _draft_lock:
        return _pending.pop(cron_creation.draft_key(sender, recipient), None) is not None


def clear_question(sender: str, recipient: str, token: object) -> bool:
    """Compare identity before discarding an undelivered native clarification."""
    from . import cron_creation
    with _draft_lock:
        key = cron_creation.draft_key(sender, recipient)
        draft = _pending.get(key)
        if draft is not None and draft.get("question_token") is token:
            _pending.pop(key, None)
            return True
        return False


def is_pending_followup(sender: str, recipient: str, text: str) -> bool:
    """Candidate check only; the message handler still enforces every access gate."""
    from . import cron_creation
    with _draft_lock:
        draft = _pending.get(cron_creation.draft_key(sender, recipient))
        if draft and draft["expires"] > time.monotonic():
            return cron_creation.is_draft_cancel(text) or _field_answer(draft["field"], text) is not None
    return _generic_handoff(sender, recipient, text) is not None


def is_creation_request(text: str) -> bool:
    raw = (text or "").strip()
    if re.search(r"\b(?:bot\s+health|health\s+(?:report|check)|drift|maintenance|sports?\s+recap)\b", raw, re.I) and not re.search(r"\bresearch\b", raw, re.I):
        return False
    return bool(_CREATE.search(raw) and _REPORT.search(raw) and re.search(r"\b(?:cron|automation|recurring|weekly|every|each)\b", raw, re.I))


def _weekday(value: str) -> int:
    return next(index for index, day in enumerate(_DAYS) if day.startswith(value.lower()[:3]))


def _time(value: str) -> str | None:
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value.lower().replace(".", "").strip())
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    if minute > 59:
        return None
    if match[3]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match[3] == "pm" else 0)
    elif match[2] is None or hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _boundary(value: str, anchor: date, hhmm: str, now: datetime, *, ending: bool) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    ordinal = re.fullmatch(rf"(?:the\s+)?first\s+({_DAY})\s+(?:(?:in|of)\s+)?({_MONTH})(?:\s+(\d{{4}}))?", value, re.I)
    if ordinal:
        year = int(ordinal[3]) if ordinal[3] else anchor.year
        month = next(index + 1 for index, name in enumerate(_MONTHS) if name.startswith(ordinal[2].lower()))
        first = date(year, month, 1)
        result = first + timedelta(days=(_weekday(ordinal[1]) - first.weekday()) % 7)
        if not ordinal[3] and result < anchor:
            first = first.replace(year=year + 1)
            result = first + timedelta(days=(_weekday(ordinal[1]) - first.weekday()) % 7)
        return result
    upcoming = re.fullmatch(rf"(?:(?:this\s+)?upcoming|next|this)\s+({_DAY})", value, re.I)
    if upcoming and not ending:
        result = now.date() + timedelta(days=(_weekday(upcoming[1]) - now.weekday()) % 7)
        if datetime.fromisoformat(f"{result.isoformat()}T{hhmm}").replace(tzinfo=PACIFIC) <= now:
            result += timedelta(days=7)
        return result
    raise ValueError("unsupported_date")


def first_and_last(expr: str, payload: dict, now: datetime | None = None) -> tuple[date, date | None]:
    hhmm, day = expr.split()
    weekday = _weekday(day)
    start = date.fromisoformat(payload["start_date"])
    first = start + timedelta(days=(weekday - start.weekday()) % 7)
    if now is not None and datetime.fromisoformat(f"{first.isoformat()}T{hhmm}").replace(tzinfo=PACIFIC) <= now:
        first += timedelta(days=7 * ((now.date() - first).days // 7))
        if datetime.fromisoformat(f"{first.isoformat()}T{hhmm}").replace(tzinfo=PACIFIC) <= now:
            first += timedelta(days=7)
    end = date.fromisoformat(payload["end_date"]) if payload.get("end_date") else None
    last = end - timedelta(days=(end.weekday() - weekday) % 7) if end else None
    if last is not None and first > last:
        raise ValueError("empty_date_window")
    return first, last


def validate_payload(expr: str, payload: dict) -> None:
    if not isinstance(payload, dict) or set(payload) - _FIELDS:
        raise ValueError("invalid_research_payload")
    if payload.get("version") != 1 or payload.get("kind") not in {"public_research", "fantasy_waivers"}:
        raise ValueError("unsupported_report_kind")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d (?:mon|tue|wed|thu|fri|sat|sun)", expr or ""):
        raise ValueError("unsupported_report_schedule")
    for field, limit in (("research_topic", 600), ("instructions", 1000)):
        if not isinstance(payload.get(field), str) or not payload[field].strip() or len(payload[field]) > limit:
            raise ValueError("invalid_report_text")
    recipient = payload.get("recipient")
    if not isinstance(recipient, str) or not recipient or len(recipient) > 255 or any(char.isspace() for char in recipient):
        raise ValueError("invalid_destination")
    if payload["kind"] == "fantasy_waivers":
        if type(payload.get("top_n")) is not int or not 1 <= payload["top_n"] <= 10:
            raise ValueError("invalid_top_n")
        if type(payload.get("faab_budget")) is not int or not 1 <= payload["faab_budget"] <= 10000:
            raise ValueError("invalid_faab_budget")
    elif payload.get("top_n") is not None or payload.get("faab_budget") is not None:
        raise ValueError("unexpected_fantasy_fields")
    first_and_last(expr, payload)


def _parse_creation(text: str, recipient: str, now_pt: datetime | None = None) -> tuple[str, dict] | str | _MissingField | None:
    """Return validated public work plus schedule, clarification, or no match."""
    if not is_creation_request(text):
        return None
    raw = text.strip()
    now = (now_pt or datetime.now(PACIFIC)).astimezone(PACIFIC)
    if len(raw) > 2400:
        return "Keep the research topic and instructions under 1,000 characters, then give one weekday and Pacific time."
    if re.search(r"\b(?:do\s+not|don't|don’t|never)\s+(?:create|schedule|send|start)|\bdraft\s+only\b", raw, re.I):
        return "I haven't scheduled anything. Send the complete request when you want it active."
    if re.search(r"\b(?:buy|purchase|execute|delete|modify|transfer|submit|shell|script|password|credentials|gmail|inbox|private\s+(?:files?|messages?))\b|\bplace\s+(?:an?\s+)?(?:order|bid)\b", raw, re.I):
        return "Research crons only read public web sources and post a report here. Purchases, commands, private data, and other actions need a separate request."
    if re.search(r"\b(?:another|other|different)\s+(?:chat|group|gc|dm)\b|\b(?:to|in|for)\s+(?:my|his|her|their|the)\s+(?:group|chat|gc|dm)\b|\+\d[\d ()-]{8,}|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", raw, re.I) or re.search(r"\b(?:to|for)\s+[A-Z][a-z]+\b", raw):
        return "Ask from the target chat. Research reports always post in the DM or group where you create them."
    if re.search(r"\b(?:stop|cancel|disable)\s+(?:before|on|after|in|the|first|at)\b", raw, re.I):
        return "Should that final date receive a report? Use `through YYYY-MM-DD` to include it, or give the previous final report date. Nothing is scheduled yet."
    schedule_text = raw
    bounds = {}
    for field, prefix in (("start", r"start(?:ing)?(?:\s+on)?"), ("end", r"until|through|ending(?:\s+on)?")):
        matches = list(re.finditer(rf"\b(?:{prefix})\s+({_DATE})\b", raw, re.I))
        if len(matches) > 1:
            return "Give one start date and one inclusive final date for this report."
        if matches:
            bounds[field] = matches[0][1].lower()
            schedule_text = schedule_text.replace(matches[0][0], " ")
    if re.search(r"\b(?:starting|until|through|ending)\b", schedule_text, re.I):
        return "Use a date like `starting 2027-09-01 through 2028-01-05`, or `starting upcoming Wednesday through first Wednesday in January`."
    topic_match = re.search(r"\b(?:research|report|briefing|digest)\s+(?:about|on|covering)\s+(.+?)(?=\b(?:every|each|weekly|starting|until|through|at\s+\d)\b|$)", schedule_text, re.I)
    # A topic such as "Central bank news" does not declare a timezone.
    if topic_match:
        schedule_text = schedule_text[:topic_match.start(1)] + " " + schedule_text[topic_match.end(1):]
    if re.search(r"\b(?:weekdays?|weekends?|daily|nightly|hourly|monthly|yearly|biweekly|fortnightly|twice|except|excluding|but\s+not|(?:every|each)\s+(?:other|first|second|third|fourth|fifth|last|\d+))\b", schedule_text, re.I):
        return "Research reports support one weekly weekday and time per cron. Give that schedule so I don't save a substitute."
    if re.search(r"\b(?:eastern|central|mountain|est|edt|cst|cdt|mst|mdt|et|ct|mt|utc|gmt|cet|jst)\b|\b[a-z_]+/[a-z_]+\b", schedule_text, re.I):
        return "Research cron times use Pacific time. Give the Pacific time explicitly."
    days = {_weekday(match[0]) for match in re.finditer(rf"\b{_DAY}s?\b", schedule_text, re.I)}
    times = {_time(match[0]) for match in _TIME.finditer(schedule_text)}
    if re.search(r"\b(?:at|between|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:and|&|,|-|to)\s*\d", schedule_text, re.I):
        return "Choose one weekly time for this research cron; I won't drop the other time."
    if len(days) != 1:
        if not days:
            return _MissingField("weekday", "Which weekday should the research report run? Reply with, for example, `Wednesday`.")
        return "Choose one weekday for the research report; I won't drop the other days."
    if len(times) != 1 or None in times:
        if not times:
            return _MissingField("time", "What Pacific time should it run? Reply with, for example, `8am PT` or `00:59 PT`.")
        return "Give one valid Pacific time with AM/PM or 24-hour HH:MM, along with the research topic and weekday."
    hhmm = next(iter(times))
    day = next(iter(days))
    fantasy = bool(re.search(r"\b(?:waivers?|waiver[ -]?wire)\b", raw, re.I))
    top_n, faab = None, None
    if fantasy:
        if re.search(r"\b(?:baseball|basketball|hockey|mlb|nba|nhl)\b", raw, re.I):
            return "The waiver template currently covers NFL fantasy football. Other sports can use a public research report with a topic and instructions."
        tops = {10 if value.lower() == "ten" else int(value) for value in re.findall(r"\btop\s*(\d+|ten)\b", raw, re.I)}
        budgets = re.findall(r"\b(?:faab(?:\s+(?:total\s+)?budget)?|(?:total\s+)?budget)\s*[(=:]?\s*(?:(?:out\s+of|of|is)\s*)?\$?\s*(\d+(?:\.\d+)?)\b", raw, re.I)
        if not budgets:
            return _MissingField("budget", "What total FAAB budget should the waiver bids use? Reply with, for example, `$100`.")
        if len(tops) > 1 or len(set(budgets)) > 1 or any("." in value for value in budgets):
            return "Give one target count and one whole-dollar total FAAB budget so I don't choose between conflicting instructions."
        top_n = next(iter(tops)) if tops else 10
        faab = int(budgets[0])
        if not 1 <= top_n <= 10 or not 1 <= faab <= 10000:
            return "Choose 1-10 waiver targets and a whole-dollar FAAB budget from $1 to $10,000."
        topic = "NFL fantasy football waiver wire pickups, opportunity, injuries, and FAAB recommendations"
        if len(raw) > 1000:
            return "Keep the public report instructions under 1,000 characters."
        instructions = raw
    else:
        if not topic_match or not topic_match[1].strip(" ,.;"):
            return _MissingField("topic", "What public topic should it research? Reply with, for example, `about battery technology`.")
        topic = topic_match[1].strip(" ,.;")
        if len(topic) > 600:
            return "Keep the public research topic under 600 characters."
        if len(raw) > 1000:
            return "Keep the public report instructions under 1,000 characters."
        instructions = raw
    expr = f"{hhmm} {_DAYS[day][:3]}"
    try:
        start = _boundary(bounds["start"], now.date(), hhmm, now, ending=False) if "start" in bounds else now.date()
        end = _boundary(bounds["end"], max(start, now.date()), hhmm, now, ending=True) if "end" in bounds else None
        payload = {"version": 1, "kind": "fantasy_waivers" if fantasy else "public_research", "research_topic": topic,
                   "instructions": instructions, "top_n": top_n, "faab_budget": faab, "recipient": recipient,
                   "start_date": start.isoformat(), "end_date": end.isoformat() if end else None}
        validate_payload(expr, payload)
        first, _last = first_and_last(expr, payload, now)
        payload["start_date"] = first.isoformat()
        return expr, payload
    except (ValueError, TypeError, StopIteration, OverflowError):
        return "That date window has no future weekly run, or a date is invalid. Give a future start and an inclusive final date."


def describe(expr: str, payload: dict) -> str:
    first, last = first_and_last(expr, payload)
    detail = f"NFL fantasy waiver top {payload['top_n']}; FAAB budget ${payload['faab_budget']}" if payload["kind"] == "fantasy_waivers" else f"Public research: {payload['research_topic']}"
    dates = f"First run {first.isoformat()}; final run {last.isoformat()} (included)" if last else f"First run {first.isoformat()}; repeats until cancelled"
    run = payload.get("run") or {}
    status = f" Last attempt: {run.get('date', '')} {run.get('status', '')}." if run else ""
    return f"{detail}. {dates}.{status}"


def parse_creation(text: str, recipient: str, now_pt: datetime | None = None) -> tuple[str, dict] | str | None:
    """Stateless parser compatibility; only the authorized intake retains drafts."""
    parsed = _parse_creation(text, recipient, now_pt)
    return parsed.text if isinstance(parsed, _MissingField) else parsed


def schedule_from_text(sender: str, text: str, recipient: str, *, db_path: str, now_pt: datetime | None = None) -> ToolOutcome | None:
    from . import cron_creation
    from .permissions import is_owner
    starting = is_creation_request(text)
    continuation = is_pending_followup(sender, recipient, text)
    if not starting and not continuation:
        if cron_creation.is_creation_request(text) and is_owner(sender):
            with _draft_lock:
                _pending.pop(cron_creation.draft_key(sender, recipient), None)
        return None
    if not is_owner(sender):
        return ToolOutcome("denied", "Research cron creation is the owner-only.", error="owner_required")
    if not recipient:
        return ToolOutcome("failed", "Ask from the target DM or group so I have its destination.", error="missing_chat")
    key = cron_creation.draft_key(sender, recipient)
    recipient = key[1]
    with _draft_lock:
        now = time.monotonic()
        for expired in [item for item, draft in _pending.items() if draft["expires"] <= now]:
            _pending.pop(expired, None)
        existing = _pending.get(key)
        if existing and cron_creation.is_draft_cancel(text):
            _pending.pop(key, None)
            return ToolOutcome("confirmed", "Cancelled the new research cron draft. No saved cron was changed.", verification_scope="draft_cleared")
        request = text if starting else _generic_handoff(sender, recipient, text)
        if request is None and existing:
            answer = _field_answer(existing["field"], text)
            if answer is not None:
                request = existing["request"] + answer
        if request is None:
            return None
        parsed = _parse_creation(request, recipient, now_pt)
        if isinstance(parsed, _MissingField):
            from .cron_clarifications import generated
            _pending[key] = {"request": request, "field": parsed.field, "expires": now + _DRAFT_TTL_SECONDS,
                             "question_token": generated("research", sender, recipient)}
            cron_creation.clear_draft(sender, recipient)
            return ToolOutcome("failed", parsed.text + " Nothing is scheduled yet.", error="clarification_required")
        if isinstance(parsed, str):
            # A rejected slot answer does not throw away the prior valid draft.
            # A distinct new request supersedes it, even when that request errs.
            if starting:
                _pending.pop(key, None)
            return ToolOutcome("failed", parsed, error="clarification_required")
        _pending.pop(key, None)
    cron_creation.clear_draft(sender, recipient)
    expr, payload = parsed
    encoded = json.dumps(payload, sort_keys=True)
    try:
        with connect_bot_db(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id, cron_expression, action_payload FROM cron_jobs WHERE action_type = ? AND enabled = 1 AND created_by = 'owner'", (ACTION,)).fetchall()
            existing = None
            for rid, saved_expr, raw in rows:
                try:
                    saved = json.loads(raw or "{}")
                    validate_payload(saved_expr, saved)
                except (ValueError, TypeError, KeyError):
                    continue
                saved.pop("run", None)
                # Repeating the same request after its first run must not create
                # a second active job because "upcoming" now resolves later.
                comparison = dict(payload, start_date=saved["start_date"])
                if saved_expr == expr and saved == comparison and saved["start_date"] <= payload["start_date"]:
                    existing = rid
                    payload = saved
                    break
            if existing is None:
                cursor = conn.execute("INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) VALUES (?, ?, ?, 1, 'owner')", (expr, ACTION, encoded))
                rid = int(cursor.lastrowid)
            else:
                rid = existing
            conn.commit()
            row = conn.execute("SELECT cron_expression, action_type, action_payload, enabled, created_by FROM cron_jobs WHERE id = ?", (rid,)).fetchone()
            readback = json.loads(row[2]) if row else {}
            readback.pop("run", None)
            if not row or row[0:2] != (expr, ACTION) or row[3:] != (1, "owner") or readback != payload:
                raise ValueError("readback_mismatch")
        lead = "Already saved" if existing is not None else "Saved"
        return ToolOutcome("confirmed", f"{lead} research cron #{rid}: every {expr.split()[1].capitalize()} at {expr.split()[0]} PT in this chat.\n{describe(expr, payload)}\nI research fresh sources when it runs. This request did not send a report.", verification_scope="cron_row_readback")
    except Exception:
        return ToolOutcome("unverified", "I couldn't verify the saved research cron. Check `list crons` before trying again.", error="save_unverified")
