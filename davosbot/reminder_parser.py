import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ParsedReminder:
    message: str
    due_ts: str


class ReminderParseError(ValueError):
    """A recognized reminder constraint needs clarification before any write."""


_PREFIX = (
    r"(?:(?:please\s+)?(?:can\s+you|could\s+you)\s+)?"
    r"(?:remind\s+me|set\s+(?:a\s+)?reminder|add\s+(?:a\s+)?reminder|schedule\s+(?:a\s+)?reminder)"
)
_NUMBER = r"(?P<num>\d+|a|an|one)"
_UNIT = r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?|m|h|d|w)\b"
# Match complete clock tokens, never the hour or minute fragment of a malformed
# time, date, decimal, or identifier.
_TIME = r"(?<![\w:./+-])(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?(?![\w:./+-])"
_RELATIVE_DAY_WORDS = r"today|tomorrow|tmw|tmrw|tonight"
_WEEKDAY_WORDS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
_MONTH_WORDS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_IN_BEFORE_MESSAGE_RE = re.compile(
    rf"^\s*{_PREFIX}\s+in\s+{_NUMBER}\s*{_UNIT}\s+(?:(?:to|that|about)\s+)?(?P<message>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_IN_AFTER_MESSAGE_RE = re.compile(
    rf"^\s*{_PREFIX}\s+(?:to\s+|that\s+|about\s+)?(?P<message>.+?)\s+in\s+{_NUMBER}\s*{_UNIT}\s*$",
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
    rf"^\s*(?:(?P<weekday>{_WEEKDAY_WORDS}),?\s+)?(?P<month_name>{_MONTH_WORDS})\s+(?P<month_day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<month_year>\d{{2}}|\d{{4}}))?\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_MONTH_WHEN_SUFFIX_RE = re.compile(
    rf"^(?P<prefix>.*?)(?:(?P<weekday>{_WEEKDAY_WORDS}),?\s+)?(?P<month_name>{_MONTH_WORDS})\s+(?P<month_day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<month_year>\d{{2}}|\d{{4}}))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_NUMERIC_WHEN_PREFIX_RE = re.compile(
    rf"^\s*(?:(?P<weekday>{_WEEKDAY_WORDS}),?\s+)?(?P<num_month>\d{{1,2}})[/-](?P<num_day>\d{{1,2}})(?:[/-](?P<num_year>\d{{2}}|\d{{4}}))?\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_NUMERIC_WHEN_SUFFIX_RE = re.compile(
    rf"^(?P<prefix>.*?)(?:(?P<weekday>{_WEEKDAY_WORDS}),?\s+)?(?P<num_month>\d{{1,2}})[/-](?P<num_day>\d{{1,2}})(?:[/-](?P<num_year>\d{{2}}|\d{{4}}))?\s*$",
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
    return (message or "").strip()


def _number_to_int(raw: str) -> int:
    if (raw or "").lower() in {"a", "an", "one"}:
        return 1
    return int(raw)


def _unit_delta(n: int, unit: str) -> timedelta:
    unit = (unit or "").lower()
    if unit == "m" or unit.startswith(("min", "minute")):
        return timedelta(minutes=n)
    if unit == "h" or unit.startswith(("hr", "hour")):
        return timedelta(hours=n)
    if unit == "d" or unit.startswith("day"):
        return timedelta(days=n)
    if unit == "w" or unit.startswith("week"):
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
        weekday = parts.get("weekday")
        if weekday and dated.weekday() != _WEEKDAY_INDEX[weekday]:
            raise ReminderParseError("The weekday and calendar date don't match. Please confirm the date.")
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


def _utc_string(dt_local: datetime) -> str:
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parts_from_match(match: re.Match[str]) -> dict[str, str]:
    groups = {k: (v or "").strip().lower() for k, v in match.groupdict().items()}
    if groups.get("next"):
        groups["day"] = "next"
    return groups


def _split_when_prefix(text: str) -> tuple[dict[str, str] | None, str]:
    clean = (text or "").strip()
    for pattern in (_MONTH_WHEN_PREFIX_RE, _NUMERIC_WHEN_PREFIX_RE, _RELATIVE_WHEN_PREFIX_RE):
        match = pattern.match(clean)
        if match:
            return _parts_from_match(match), (match.groupdict().get("rest") or "").lstrip(" ,").rstrip()
    return None, clean


def _split_when_suffix(text: str) -> tuple[dict[str, str] | None, str]:
    clean = (text or "").strip()
    for pattern in (_MONTH_WHEN_SUFFIX_RE, _NUMERIC_WHEN_SUFFIX_RE, _RELATIVE_WHEN_SUFFIX_RE):
        match = pattern.match(clean)
        if match:
            message = (match.groupdict().get("prefix") or "").strip(" ,")
            message = re.sub(r"\b(?:on|for)\s*$", "", message, flags=re.IGNORECASE).rstrip()
            return _parts_from_match(match), message
    return None, clean


def _parse_relaxed_time_reminder(text: str, local_now: datetime) -> ParsedReminder | None:
    prefix = _PREFIX_MATCH_RE.match(text or "")
    if not prefix:
        return None
    body = (text[prefix.end():] or "").strip()
    if not body:
        return None

    matches = []
    for match in _TIME_SEARCH_RE.finditer(body):
        raw_before = body[:match.start()]
        has_time_marker = bool(
            match.group("ampm")
            or match.group("minute")
            or re.search(r"\b(?:at|for|by)\s*$", raw_before, re.IGNORECASE)
        )
        if not has_time_marker:
            continue
        matches.append(match)
    if len(matches) > 1:
        raise ReminderParseError("I found multiple clock times. Please state one reminder time.")
    if not matches and re.search(r"\b\d+[.:]\d+\s*(?:a\.?m\.?|p\.?m\.?)?", body, re.IGNORECASE):
        raise ReminderParseError("I couldn't read that clock time. Please use a time such as 7:35am.")
    for match in matches:
        raw_before = body[:match.start()]
        raw_after = body[match.end():]
        parsed_time = _parse_hour(match.group("hour"), match.group("minute"), match.group("ampm"))
        if not parsed_time:
            raise ReminderParseError("That clock time is invalid. Please give a time such as 7:35am.")

        before = re.sub(r"\b(?:at|for|by)\s*$", "", raw_before, flags=re.IGNORECASE).strip(" ,")
        before = re.sub(r"^\s*(?:for|on|to|that|about)\s+", "", before, flags=re.IGNORECASE).strip(" ,")
        after = re.sub(r"^\s*(?:to|that|about)\s+", "", raw_after.lstrip(" ,").rstrip(), flags=re.IGNORECASE)
        combined = f"{before} {after}".strip()

        parts, message = _split_when_prefix(combined)
        if parts is None:
            parts, message = _split_when_suffix(combined)
        if parts is None:
            parts, message = {}, combined

        # A leftover leading calendar expression is a second or unsupported
        # constraint, not reminder prose to silently carry to another date.
        if re.match(rf"(?:on\s+)?(?:(?:{_MONTH_WORDS})\s*\d|\d{{1,4}}[/-]\d|(?:{_WEEKDAY_WORDS})\s+(?:{_MONTH_WORDS}))", message, re.IGNORECASE):
            raise ReminderParseError("I couldn't resolve the full calendar date. Please give one date and time.")

        message = _clean_message(message)
        if not message:
            continue
        try:
            due_local = _target_date_from_parts(parts, local_now, parsed_time[0], parsed_time[1])
        except ReminderParseError:
            raise
        except (ValueError, OverflowError):
            raise ReminderParseError("That calendar date is invalid. Please confirm the date.") from None
        if due_local <= local_now:
            raise ReminderParseError("That date and time are in the past. Please give a future time.")
        return ParsedReminder(message=message, due_ts=_utc_string(due_local))
    return None


_CANCEL_SELECTOR_RE = re.compile(
    r"^\s*(?:please\s+)?(?:cancel|delete|remove)\s+"
    r"(?:(?:the|my|these|those)\s+)?reminders?\s+(?P<selectors>.+?)"
    r"\s*(?:(?:please|pls)\s*)?[.!]?\s*$", re.IGNORECASE | re.DOTALL,
)
_POSITION_ITEM = r"#?\d+(?:\s*(?:-|\u2013|\u2014|to|through)\s*#?\d+)?"
_POSITION_LIST_RE = re.compile(rf"{_POSITION_ITEM}(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s+){_POSITION_ITEM})*", re.IGNORECASE)
_POSITION_ITEM_RE = re.compile(r"#?(\d+)(?:\s*(?:-|\u2013|\u2014|to|through)\s*#?(\d+))?", re.IGNORECASE)


def parse_reminder_cancel_positions(text: str) -> list[int]:
    """Parse a whole positional selector, including bounded inclusive ranges.

    Non-positional requests remain available to the existing matching route.
    Invalid numeric selectors must stop before any cancellation or model call.
    """
    match = _CANCEL_SELECTOR_RE.fullmatch(text or "")
    if not match:
        return []
    selectors = match.group("selectors").strip()
    if not re.match(r"[#\d+-]", selectors):
        return []
    if len(selectors) > 4096:
        raise ReminderParseError("Cancel at most 100 reminder numbers at once.")
    if not _POSITION_LIST_RE.fullmatch(selectors):
        raise ReminderParseError("Use reminder numbers such as 2 and 5, or a range such as 2-5.")
    positions = set()
    for item in _POSITION_ITEM_RE.finditer(selectors):
        if len(item.group(1)) > 9 or len(item.group(2) or "") > 9:
            raise ReminderParseError("Use the reminder numbers shown by 'list reminders'.")
        start = int(item.group(1))
        end = int(item.group(2) or start)
        if start < 1 or end < start or end - start >= 100:
            raise ReminderParseError("Use an ascending range of positive reminder numbers, at most 100 at once.")
        positions.update(range(start, end + 1))
        if len(positions) > 100:
            raise ReminderParseError("Cancel at most 100 reminder numbers at once.")
    return sorted(positions)


def parse_deterministic_reminder(
    text: str,
    *,
    now: datetime | None = None,
    tz_name: str = "America/Los_Angeles",
) -> ParsedReminder | None:
    """Parse phone reminders; raise ReminderParseError for unsafe partial parses."""
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
        if re.match(r"\d+\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|m|h|d|w)\b", message, re.IGNORECASE):
            raise ReminderParseError("Please combine the duration into one unit, such as 90 minutes.")
        try:
            amount = _number_to_int(match.group("num"))
            if amount < 1:
                raise ValueError("nonpositive duration")
            due_local = local_now + _unit_delta(amount, match.group("unit"))
        except (ValueError, OverflowError):
            raise ReminderParseError("Please give a positive reminder duration within the supported calendar.") from None
        return ParsedReminder(message=message, due_ts=_utc_string(due_local))

    prefix = _PREFIX_MATCH_RE.match(text)
    if prefix and re.match(r"in\s+", text[prefix.end():], re.IGNORECASE):
        raise ReminderParseError("Please give a duration such as 'in 4 hours', followed by the reminder text.")

    relaxed = _parse_relaxed_time_reminder(text, local_now)
    if relaxed is not None:
        return relaxed

    return None
