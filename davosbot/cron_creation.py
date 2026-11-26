"""Bounded conversation drafts for existing cron actions, with no DB or execution access."""

import re
import time
from threading import RLock

from .config import normalize_handle


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


def draft_key(sender: str, chat_id: str) -> tuple[str, str]:
    """Canonical identity, not an authorization decision or chosen destination."""
    chat = (chat_id or "").strip()
    return normalize_handle(sender), chat.lower() if re.fullmatch(r"[0-9a-f]{32}", chat, re.I) else normalize_handle(chat)


def pending_draft(sender: str, chat_id: str) -> dict | None:
    """Read a live draft for a caller that still must check current access."""
    with _lock:
        draft = _pending.get(draft_key(sender, chat_id))
        return dict(draft) if draft and draft["expires"] > time.monotonic() else None


def _schedule_text(text: str) -> str:
    # A quoted greeting is content, never a second schedule or destination.
    return re.sub(r'''"[^"\n]*"|“[^”\n]*”|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w))*'(?!\w)''', "", _clean(text))


def control_text(text: str) -> str:
    """Quoted content and negated controls are not requests to mutate a saved job."""
    return re.sub(
        r"\b(?:don['’]t|do\s+not|never)\s+(?:ever\s+)?"
        r"(?:stop|cancel|delete|remove|disable|kill|turn\s+off|change|edit|update|modify|fix|move|reschedule|rotate|cycle|vary|switch|push)\b",
        "", _schedule_text(text), flags=re.I,
    )


def is_schedule_discussion(text: str) -> bool:
    raw = _schedule_text(text)
    if not re.search(r"\b(?:cron|jobs?|daily|nightly|weekly|every|each|morning)\b", raw, re.I):
        return False
    if re.match(r"^(?:please\s+)?(?:don['’]t|do\s+not|never)\s+", raw, re.I):
        return True
    if not re.search(r"[?]|\b(?:why|how)\b", raw, re.I):
        return False
    request = re.match(
        r"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
        r"(?:create|make|add|start|set(?:\s+up)?|setup|schedule|send|give|post|"
        r"change|edit|update|modify|fix|move|reschedule|rotate|cycle|vary|switch|push|"
        r"cancel|delete|remove|kill|stop|disable|turn\s+off)\b", raw, re.I,
    )
    return request is None


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
    raw = control_text(text)
    if is_schedule_discussion(text):
        return False
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
    raw = re.sub(r"^(?:let['’]s\s+(?:do|use)|(?:make|set)\s+it(?:\s+for)?)\s+", "", raw)
    raw = re.sub(r"^i\s+(?:want|would like)\s+", "", raw)
    raw = re.sub(r"\s+works(?:\s+for\s+(?:me|us))?(?:\s+please)?$", "", raw)
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
    if re.search(r"\b(?:waiver(?:\s+wire)?|faab|fantasy\s+(?:football|baseball|basketball)|research)\b", lower):
        return fields, f"That custom research report is not a supported cron action yet. I can schedule {_ACTION_CHOICES}; I haven't saved a substitute."
    if re.search(r"\b(?:until|through|end(?:ing)?\s+(?:on|after)|stop\s+(?:on|after)|"
                 r"(?:start(?:ing)?|begin(?:ning)?)\s+(?:this|next|on))\b", lower):
        return fields, "Cron start and end dates are not supported yet. I haven't saved a job with different dates."
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
        draft = _pending.pop(draft_key(sender, chat_id), None)
        return bool(draft and draft["expires"] > time.monotonic())


def clear_question(sender: str, chat_id: str, token: object) -> bool:
    """Clear only the exact unfinished question whose native reply failed."""
    with _lock:
        key = draft_key(sender, chat_id)
        draft = _pending.get(key)
        if draft is not None and draft.get("question_token") is token:
            _pending.pop(key, None)
            return True
        return False


def is_draft_cancel(text: str) -> bool:
    # Match the whole control, including ordinary text-message punctuation.
    # Do not strip quotes or negations into executable cancellation commands.
    return bool(re.fullmatch(
        r"(?:cancel\s+(?:new\s+cron|cron\s+draft)|never\s*mind)(?:\s*[.!?])*",
        _clean(text), re.I,
    ))


def is_pending_followup(sender: str, chat_id: str, text: str, *, required_action: str = "") -> bool:
    """Check a bounded continuation without granting access or consuming its draft."""
    with _lock:
        draft = _pending.get(draft_key(sender, chat_id))
        return bool(
            draft and draft["expires"] > time.monotonic()
            and (not required_action or draft.get("action") == required_action)
            and (_is_followup(text) or is_draft_cancel(text))
        )


def prepare_creation(sender: str, chat_id: str, text: str, parsed: dict | None, normalize_time, *, required_action: str = "") -> dict | str | None:
    """An authorized caller supplies the actual sender/chat and any narrower action limit."""
    key = draft_key(sender, chat_id)
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
        if required_action and fields.get("action", required_action) != required_action:
            return "This sports recap draft can only schedule a sports recap."
        draft = {} if not existing or (starting and not _is_followup(raw)) else dict(existing)
        draft.update(fields)
        if required_action:
            draft["action"] = required_action
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
            reply = f"What should the new cron send: {_ACTION_CHOICES}, or a weekly public research/waiver report?"
        elif draft.get("weekly") and not draft.get("day_of_week"):
            reply = "Which weekday should it run? For example, Friday."
        elif draft.get("ambiguous_hour"):
            reply = "Is that AM or PM? Reply with a Pacific time like `8am`, `8pm`, or `08:00`."
        elif not draft.get("time_pt"):
            reply = "I can create that cron, but I need a Pacific time like `6:30am` or `9pm`."
        else:
            _pending.pop(key, None)
            return {field: draft.get(field, "") for field in ("action", "time_pt", "day_of_week", "intro", "intro_mode")}
        from .cron_clarifications import generated
        draft["question_token"] = generated("ordinary", sender, chat_id)
        _pending[key] = draft
        return reply
