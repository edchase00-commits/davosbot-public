import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ParsedReminder:
    message: str
    due_ts: str


_PREFIX = (
    r"(?:(?:please\s+)?(?:can\s+you|could\s+you)\s+)?"
    r"(?:remind\s+me|set\s+(?:a\s+)?reminder|add\s+(?:a\s+)?reminder|schedule\s+(?:a\s+)?reminder)"
)
_NUMBER = r"(?P<num>\d+|a|an|one)"
_UNIT = r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)"
_TIME = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?|am|pm)?"
_RELATIVE_DAY_WORDS = r"today|tomorrow|tmw|tmrw|tonight"
_DAY = rf"(?P<day>{_RELATIVE_DAY_WORDS}|next\s+)?(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)?"
_WEEKDAY_WORDS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
_MONTH_WORDS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_IN_BEFORE_MESSAGE_RE = re.compile(
    rf"^\s*{_PREFIX}\s+in\s+{_NUMBER}\s+{_UNIT}\s+(?:to|that|about)?\s*(?P<message>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_IN_AFTER_MESSAGE_RE = re.compile(
    rf"^\s*{_PREFIX}\s+(?:to\s+|that\s+|about\s+)?(?P<message>.+?)\s+in\s+{_NUMBER}\s+{_UNIT}\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TIME_BEFORE_MESSAGE_RE = re.compile(
    rf"^\s*{_PREFIX}\s+(?:for\s+)?(?P<when>{_DAY})?\s*(?:(?:at|for|by)\s+)?{_TIME}\s+(?:to|that|about)\s+(?P<message>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TIME_AFTER_MESSAGE_RE = re.compile(
    rf"^\s*{_PREFIX}\s+(?:to\s+|that\s+|about\s+)?(?P<message>.+?)\s+(?:for\s+)?(?P<when>{_DAY})?\s*(?:(?:at|for|by)\s+){_TIME}\s*$",
    re.IGNORECASE | re.DOTALL,
)
_PREFIX_MATCH_RE = re.compile(rf"^\s*{_PREFIX}\b\s*", re.IGNORECASE | re.DOTALL)
_TIME_SEARCH_RE = re.compile(_TIME, re.IGNORECASE)
_RELATIVE_WHEN_PREFIX_RE = re.compile(
    rf"^\s*(?:(?P<day>{_RELATIVE_DAY_WORDS})|(?:(?P<next>next)\s+)?(?P<weekday>{_WEEKDAY_WORDS}))\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_RELATIVE_WHEN_SUFFIX_RE = re.compile(
    rf"^(?P<prefix>.*?)(?:(?P<day>{_RELATIVE_DAY_WORDS})|(?:(?P<next>next)\s+)?(?P<weekday>{_WEEKDAY_WORDS}))\s*$",
    re.IGNORECASE | re.DOTALL,
)
_MONTH_WHEN_PREFIX_RE = re.compile(
    rf"^\s*(?P<month_name>{_MONTH_WORDS})\s+(?P<month_day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<month_year>\d{{2,4}}))?\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_MONTH_WHEN_SUFFIX_RE = re.compile(
    rf"^(?P<prefix>.*?)(?P<month_name>{_MONTH_WORDS})\s+(?P<month_day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<month_year>\d{{2,4}}))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_NUMERIC_WHEN_PREFIX_RE = re.compile(
    r"^\s*(?P<num_month>\d{1,2})[/-](?P<num_day>\d{1,2})(?:[/-](?P<num_year>\d{2,4}))?\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_NUMERIC_WHEN_SUFFIX_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<num_month>\d{1,2})[/-](?P<num_day>\d{1,2})(?:[/-](?P<num_year>\d{2,4}))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_WEEKDAY_INDEX = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
_MONTH_INDEX = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _clean_message(message: str) -> str:
    return re.sub(r"\s+", " ", (message or "").strip(" .,:;-\n\t"))


def _number_to_int(raw: str) -> int:
    if (raw or "").lower() in {"a", "an", "one"}:
        return 1
    return int(raw)


def _unit_delta(n: int, unit: str) -> timedelta:
    unit = (unit or "").lower()
    if unit.startswith(("min", "minute")):
        return timedelta(minutes=n)
    if unit.startswith(("hr", "hour")):
        return timedelta(hours=n)
    if unit.startswith("day"):
        return timedelta(days=n)
    if unit.startswith("week"):
        return timedelta(weeks=n)
    raise ValueError("unsupported reminder unit")


def _parse_hour(hour_raw: str, minute_raw: str | None, ampm_raw: str | None) -> tuple[int, int] | None:
    hour = int(hour_raw)
    minute = int(minute_raw or 0)
    if minute > 59:
        return None
    ampm = (ampm_raw or "").lower().replace(".", "")
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    else:
        if not 0 <= hour <= 23:
            return None
        # Text-message shorthand: "at 5" usually means 5pm, not 5am.
        if 1 <= hour <= 7:
            hour += 12
    return hour, minute


def _coerce_year(raw: str | None, fallback: int) -> tuple[int, bool]:
    if not raw:
        return fallback, False
    year = int(raw)
    if year < 100:
        year += 2000
    return year, True


def _date_from_month_parts(parts: dict[str, str], local_now: datetime, hour: int, minute: int) -> datetime | None:
    if parts.get("month_name"):
        month = _MONTH_INDEX.get(parts["month_name"].lower()[:4].rstrip("."))
        if month is None:
            month = _MONTH_INDEX.get(parts["month_name"].lower())
        day = int(parts.get("month_day") or 0)
        year, explicit_year = _coerce_year(parts.get("month_year"), local_now.year)
    elif parts.get("num_month"):
        month = int(parts["num_month"])
        day = int(parts.get("num_day") or 0)
        year, explicit_year = _coerce_year(parts.get("num_year"), local_now.year)
    else:
        return None

    candidate = datetime(year, month, day, hour, minute, tzinfo=local_now.tzinfo)
    if not explicit_year and candidate <= local_now + timedelta(seconds=30):
        candidate = datetime(year + 1, month, day, hour, minute, tzinfo=local_now.tzinfo)
    return candidate


def _target_date_from_parts(parts: dict[str, str], local_now: datetime, hour: int, minute: int) -> datetime:
    dated = _date_from_month_parts(parts, local_now, hour, minute)
    if dated is not None:
        return dated

    day = (parts.get("day") or "").strip().lower()
    weekday = (parts.get("weekday") or "").strip().lower()
    base_date = local_now.date()

    if weekday:
        target_weekday = _WEEKDAY_INDEX[weekday]
        days = (target_weekday - local_now.weekday()) % 7
        if day.startswith("next") and days == 0:
            days = 7
        base_date = base_date + timedelta(days=days)
    elif day in {"tomorrow", "tmw", "tmrw"}:
        base_date = base_date + timedelta(days=1)

    candidate = datetime.combine(base_date, datetime.min.time(), tzinfo=local_now.tzinfo).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if weekday and not day.startswith("next") and candidate <= local_now + timedelta(seconds=30):
        candidate += timedelta(days=7)
    if not day and not weekday and candidate <= local_now + timedelta(seconds=30):
        candidate += timedelta(days=1)
    return candidate


def _target_date(match: re.Match[str], local_now: datetime, hour: int, minute: int) -> datetime:
    return _target_date_from_parts(match.groupdict(), local_now, hour, minute)


def _utc_string(dt_local: datetime) -> str:
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parts_from_match(match: re.Match[str]) -> dict[str, str]:
    groups = {k: (v or "").strip().lower() for k, v in match.groupdict().items()}
    if groups.get("next"):
        groups["day"] = "next"
    return groups


def _split_when_prefix(text: str) -> tuple[dict[str, str] | None, str]:
    clean = (text or "").strip(" ,")
    for pattern in (_MONTH_WHEN_PREFIX_RE, _NUMERIC_WHEN_PREFIX_RE, _RELATIVE_WHEN_PREFIX_RE):
        match = pattern.match(clean)
        if match:
            return _parts_from_match(match), (match.groupdict().get("rest") or "").strip(" ,")
    return None, clean


def _split_when_suffix(text: str) -> tuple[dict[str, str] | None, str]:
    clean = (text or "").strip(" ,")
    for pattern in (_MONTH_WHEN_SUFFIX_RE, _NUMERIC_WHEN_SUFFIX_RE, _RELATIVE_WHEN_SUFFIX_RE):
        match = pattern.match(clean)
        if match:
            return _parts_from_match(match), (match.groupdict().get("prefix") or "").strip(" ,")
    return None, clean


def _parse_relaxed_time_reminder(text: str, local_now: datetime) -> ParsedReminder | None:
    prefix = _PREFIX_MATCH_RE.match(text or "")
    if not prefix:
        return None
    body = (text[prefix.end():] or "").strip(" ,")
    if not body:
        return None

    for match in _TIME_SEARCH_RE.finditer(body):
        raw_before = body[:match.start()]
        raw_after = body[match.end():]
        has_time_marker = bool(
            match.group("ampm")
            or match.group("minute")
            or re.search(r"\b(?:at|for|by)\s*$", raw_before, re.IGNORECASE)
        )
        if not has_time_marker:
            continue
        parsed_time = _parse_hour(match.group("hour"), match.group("minute"), match.group("ampm"))
        if not parsed_time:
            continue

        before = re.sub(r"\b(?:at|for|by)\s*$", "", raw_before, flags=re.IGNORECASE).strip(" ,")
        before = re.sub(r"^\s*(?:for|on)\s+", "", before, flags=re.IGNORECASE).strip(" ,")
        after = re.sub(r"^\s*(?:to|that|about)\s+", "", raw_after.strip(" ,"), flags=re.IGNORECASE)
        combined = f"{before} {after}".strip(" ,")

        parts, message = _split_when_prefix(combined)
        if parts is None:
            parts, message = _split_when_suffix(combined)
        if parts is None:
            parts, message = {}, combined

        message = _clean_message(message)
        if not message:
            continue
        due_local = _target_date_from_parts(parts, local_now, parsed_time[0], parsed_time[1])
        return ParsedReminder(message=message, due_ts=_utc_string(due_local))
    return None


def parse_deterministic_reminder(
    text: str,
    *,
    now: datetime | None = None,
    tz_name: str = "America/Los_Angeles",
) -> ParsedReminder | None:
    """Parse common phone reminder phrases without involving the LLM."""
    text = (text or "").strip()
    if not text:
        return None
    tz = ZoneInfo(tz_name)
    local_now = (now or datetime.now(tz)).astimezone(tz)

    for pattern in (_IN_BEFORE_MESSAGE_RE, _IN_AFTER_MESSAGE_RE):
        match = pattern.match(text)
        if not match:
            continue
        message = _clean_message(match.group("message"))
        if not message:
            return None
        due_local = local_now + _unit_delta(_number_to_int(match.group("num")), match.group("unit"))
        return ParsedReminder(message=message, due_ts=_utc_string(due_local))

    for pattern in (_TIME_BEFORE_MESSAGE_RE, _TIME_AFTER_MESSAGE_RE):
        match = pattern.match(text)
        if not match:
            continue
        message = _clean_message(match.group("message"))
        parsed_time = _parse_hour(match.group("hour"), match.group("minute"), match.group("ampm"))
        if not message or not parsed_time:
            return None
        due_local = _target_date(match, local_now, parsed_time[0], parsed_time[1])
        return ParsedReminder(message=message, due_ts=_utc_string(due_local))

    relaxed = _parse_relaxed_time_reminder(text, local_now)
    if relaxed is not None:
        return relaxed

    return None
