"""Bounded conversation drafts for existing cron actions, with no DB or execution access."""

import re
import time
from threading import RLock


_DRAFT_TTL_SECONDS = 300
_pending: dict[tuple[str, str], dict] = {}
_lock = RLock()
_DAYS = {
    "mon": "mon", "monday": "mon", "tue": "tue", "tues": "tue", "tuesday": "tue",
    "wed": "wed", "weds": "wed", "wednesday": "wed", "thu": "thu", "thur": "thu",
    "thurs": "thu", "thursday": "thu", "fri": "fri", "friday": "fri",
    "sat": "sat", "saturday": "sat", "sun": "sun", "sunday": "sun",
}
_DAY_RE = re.compile(r"\b(" + "|".join(sorted(_DAYS, key=len, reverse=True)) + r")s?\b", re.I)
_TIME_RE = re.compile(
    r"(?<![\w:])(?:\d{1,2}(?::\d{1,2})?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"\d{1,2}:\d{1,2}|noon|midnight)(?!\w)", re.I,
)
_ACTION_CHOICES = "an inspirational quote, sports recap, or bot health report"
_FOLLOWUP_WORDS = set(
    "a an the at please quote quotes inspirational inspiration motivational motivation morning message "
    "greeting sports sport recap scores scoreboard espn bot health report check drift maintenance "
    "daily nightly every each day night week weekly once on am pm a m p pt pst pdt pacific noon midnight".split()
) | set(_DAYS) | set(
    "eastern central mountain est edt cst cdt mst mdt et ct mt utc gmt weekdays weekends "
    "hourly monthly yearly first second third fourth fifth last other two three four hours minutes "
    "america europe asia africa australia new york london tokyo berlin uk time timezone cet cest bst ist jst".split()
)


def _clean(text: str) -> str:
    return re.sub(r"^@davos\b[:,]?\s*", "", (text or "").strip(), flags=re.I)


def _schedule_text(text: str) -> str:
    # A quoted greeting is content, never a second schedule or destination.
    return re.sub(r'''"[^"\n]*"|“[^”\n]*”|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w))*'(?!\w)''', "", _clean(text))


def _actions(text: str) -> set[str]:
    choices = set()
    if re.search(r"\b(?:quotes?|inspirational|inspiration|motivational|motivation|greeting)\b|\bmorning\s+(?:message|cron)\b", text, re.I):
        choices.add("morning_message")
    if re.search(r"\b(?:sports?|espn|scoreboard)\b", text, re.I):
        choices.add("sports_recap")
    if re.search(r"\b(?:drift|maintenance|bot\s+health|health\s+(?:report|check))\b", text, re.I):
        choices.add("drift_check")
    return choices


def is_creation_request(text: str) -> bool:
    raw = _schedule_text(text)
    if re.search(r"#\s*\d+|\b(?:cron|job)\s+(?:id\s*)?\d+\b", raw, re.I):
        return False
    if re.search(r"\b(?:change|edit|update|modify|fix|move|reschedule|cancel|delete|remove|stop|disable)\b", raw, re.I):
        return False
    explicit = bool(re.search(r"\b(?:create|make|add|start|set\s+up|setup|schedule|new)\b", raw, re.I))
    cron_noun = bool(re.search(r"\b(?:crons?|jobs?|automation)\b", raw, re.I))
    recurring = bool(re.search(r"\b(?:daily|nightly|weekly|every|each)\b", raw, re.I))
    return (explicit and cron_noun) or (bool(_actions(raw)) and recurring)


def _is_followup(text: str) -> bool:
    raw = _clean(text).strip(" .!?").lower()
    if len(raw) > 120 or not raw:
        return False
    if raw in {"weather", "weather report", "reminder", "backups", "custom message"}:
        return True
    words = re.findall(r"[a-z]+", raw)
    if any(word not in _FOLLOWUP_WORDS for word in words):
        return False
    return bool(_actions(raw) or _TIME_RE.search(raw) or _DAY_RE.search(raw)
                or re.fullmatch(r"(?:at\s+)?\d{1,2}", raw)
                or re.fullmatch(r"(?:daily|nightly|weekly|weekdays|weekends|hourly|monthly|yearly|every day|every week)", raw))


def inspect_request(text: str, normalize_time) -> tuple[dict, str | None]:
    """Parse only supported schedules; reject ambiguity before callers write anything."""
    raw = _schedule_text(text)
    lower = raw.lower()
    fields: dict = {}
    if re.search(r"\b(?:weather|forecast|temperature|remind(?:er)?s?|backup|shell|script|execute|"
                 r"buy|purchase|order|email|stock|stocks|market|news|calendar)\b", lower):
        return fields, f"That needs a different cron action. I can schedule {_ACTION_CHOICES} in this chat."
    if re.search(r"\b(?:weekdays?|weekends?|hourly|monthly|yearly|fortnightly|biweekly|"
                 r"business\s+days|(?:every|each)\s+other|(?:every|each)\s+(?:\d+(?:st|nd|rd|th)?|first|second|third|fourth|fifth|last|two|three|four|six|twelve)|"
                 r"(?:every|each)\s+(?:hour|minute|month|year)|twice)\b", lower):
        return fields, "I can schedule daily or on one weekday per cron. Choose one of those so I don't save the wrong schedule."
    if re.search(r"\b(?:except|excluding|but\s+not)\b", lower):
        return fields, "I can't save weekday exclusions. Choose daily or one weekday for this cron."
    if re.search(r"\b(?:eastern|central|mountain|atlantic|alaska|hawaii|est|edt|cst|cdt|mst|mdt|et|ct|mt|utc|gmt|cet|cest|bst|ist|jst)\b|"
                 r"\b[a-z_]+/[a-z_]+(?:/[a-z_]+)?\b|"
                 r"\b(?:new york|london|tokyo|berlin|uk)\s+(?:time|timezone)\b|[+-]\d\d:?\d\d\b", lower):
        return fields, "Cron times use Pacific time. Give me the Pacific time you want, such as 8am PT."
    destination_text = re.sub(
        r"\b(?:for|to)\s+([a-z]+)\b",
        lambda match: "" if match.group(1) in _DAYS else match.group(0), lower,
    )
    if re.search(r"\b(?:another|other|different)\s+(?:chat|group|gc|dm)\b|"
                 r"\b(?:for|to|in)\s+(?:my|his|her|their|the)\s+(?:chat|group|gc|dm|boys)\b|"
                 r"\b(?:for|to)\s+(?!me\b|us\b|this\b|daily\b|every\b|noon\b|midnight\b|run\b|post\b|send\b)"
                 r"[a-z][\w'.-]*(?:\s+(?:at|every|daily)\b|\s*$)|\+\d[\d ()-]{8,}", destination_text):
        return fields, "New crons post in the chat where you ask. Ask from the target DM or group so I use the right destination."
    actions = _actions(raw)
    if len(actions) > 1:
        return fields, f"Choose one action for this cron: {_ACTION_CHOICES}."
    if actions:
        fields["action"] = next(iter(actions))
    days = {_DAYS[match.group(1).lower()] for match in _DAY_RE.finditer(raw)}
    daily = bool(re.search(r"\b(?:daily|nightly|(?:every|each)\s+(?:day|morning|night))\b", lower))
    if len(days) > 1:
        return fields, "Choose one weekday for this cron. I won't silently drop the other days."
    if days and daily:
        return fields, "Choose daily or one weekday for this cron; those schedules conflict."
    if days:
        fields["day_of_week"] = next(iter(days))
        fields["weekly"] = True
    elif re.search(r"\b(?:weekly|every\s+week|once\s+a\s+week)\b", lower):
        fields["weekly"] = True
    elif daily:
        fields["weekly"] = False
        fields["day_of_week"] = ""
    times = [match.group(0).strip() for match in _TIME_RE.finditer(raw)]
    if re.search(r"\b(?:between|from)\s+\d|"
                 r"\b\d{1,2}(?::\d{1,2})?\s*(?:a\.?m\.?|p\.?m\.?)?\s*"
                 r"(?:and|&|,|-|to)\s*\d{1,2}(?!\d)", lower):
        return fields, "Choose one time for this cron. I won't silently drop the other times."
    if not times:
        bare = re.search(r"\b(?:at|to|for)\s+(\d{1,2})\b", raw, re.I)
        if not bare:
            bare = re.fullmatch(r"\s*(\d{1,2})\s*[.!?]?", raw)
        if bare:
            fields["ambiguous_hour"] = True
    normalized = []
    for value in times:
        hhmm = {"noon": "12:00", "midnight": "00:00"}.get(value.lower()) or normalize_time(value)
        if not hhmm:
            return fields, "Use a valid Pacific time: 1-12 with am/pm, or 00:00 to 23:59."
        normalized.append(hhmm)
    if len(set(normalized)) > 1:
        return fields, "Choose one time for this cron. I won't silently drop the other times."
    if normalized:
        fields["time_pt"] = normalized[0]
    return fields, None


def clear_draft(sender: str, chat_id: str) -> bool:
    """Caller has checked ownership and supplies the actual current chat."""
    with _lock:
        draft = _pending.pop((sender, chat_id), None)
        return bool(draft and draft["expires"] > time.monotonic())


def is_draft_cancel(text: str) -> bool:
    return bool(re.fullmatch(r"(?:cancel (?:new cron|cron draft)|never\s*mind)", _clean(text), re.I))


def prepare_creation(sender: str, chat_id: str, text: str, parsed: dict | None, normalize_time) -> dict | str | None:
    """Owner-gated caller supplies immutable sender/chat context and the scheduler callback later."""
    key = (sender, chat_id)
    now = time.monotonic()
    with _lock:
        for expired in [item for item, draft in _pending.items() if draft["expires"] <= now]:
            _pending.pop(expired, None)
        existing = _pending.get(key)
        raw = _clean(text)
        if existing and is_draft_cancel(raw):
            _pending.pop(key, None)
            return "Cancelled the new cron draft."
        starting = parsed is not None or is_creation_request(raw)
        if not starting and not (existing and _is_followup(raw)):
            return None
        fields, error = inspect_request(raw, normalize_time)
        if error:
            if starting:
                _pending.pop(key, None)
            return error
        draft = {} if not existing or (starting and not _is_followup(raw)) else dict(existing)
        draft.update(fields)
        if fields.get("time_pt"):
            draft.pop("ambiguous_hour", None)
        elif fields.get("ambiguous_hour"):
            draft.pop("time_pt", None)
        if parsed:
            for field in ("intro", "intro_mode"):
                if parsed.get(field):
                    draft[field] = parsed[field]
            if parsed.get("intro_mode") == "rotate":
                draft.setdefault("action", "morning_message")
        draft["expires"] = now + _DRAFT_TTL_SECONDS
        if not draft.get("action"):
            reply = f"What should the new cron send: {_ACTION_CHOICES}?"
        elif draft.get("weekly") and not draft.get("day_of_week"):
            reply = "Which weekday should it run? For example, Friday."
        elif draft.get("ambiguous_hour"):
            reply = "Is that AM or PM? Reply with a Pacific time like `8am`, `8pm`, or `08:00`."
        elif not draft.get("time_pt"):
            reply = "I can create that cron, but I need a Pacific time like `6:30am` or `9pm`."
        else:
            _pending.pop(key, None)
            return {field: draft.get(field, "") for field in ("action", "time_pt", "day_of_week", "intro", "intro_mode")}
        _pending[key] = draft
        return reply
