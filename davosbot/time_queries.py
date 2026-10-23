"""Deterministic replies for narrow current-time questions."""

from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo


_PACIFIC = ZoneInfo("America/Los_Angeles")
_EASTERN = ZoneInfo("America/New_York")

_ZONE_ALIASES = {
    "eastern": "eastern",
    "eastern time": "eastern",
    "et": "eastern",
    "est": "eastern",
    "edt": "eastern",
    "pacific": "pacific",
    "pacific time": "pacific",
    "pt": "pacific",
    "pst": "pacific",
    "pdt": "pacific",
}
_ZONE_TEXT = r"eastern(?:\s+time)?|et|est|edt|pacific(?:\s+time)?|pt|pst|pdt"
_CURRENT_TIME_QUERY_RE = re.compile(
    rf"(?:what\s+time\s+is\s+it|what(?:'s|\s+is)\s+the\s+time)"
    rf"(?:\s+right\s+now|\s+now)?(?:\s+(?:in\s+)?(?:the\s+)?(?P<zone>{_ZONE_TEXT}))?",
    re.IGNORECASE,
)
_PACIFIC_CORRECTION_RE = re.compile(
    r"(?:actually[,:]?\s+)?(?:we\s+are|we're)\s+in\s+(?:the\s+)?pacific(?:\s+time)?(?:\s+zone)?"
    r"(?:,?\s+not\s+(?:in\s+)?(?:the\s+)?(?:eastern(?:\s+time)?|et|est|edt))?",
    re.IGNORECASE,
)
_SCHEDULING_RE = re.compile(
    r"\b(?:alarm|cron|every|remind|reminder|schedule|scheduled|scheduling|send|text|tomorrow)\b",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    normalized = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", normalized).strip().rstrip(".!?").strip()


def _clock_text(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%I:%M %p %Z").lstrip("0")


def current_time_reply(text: str, *, now: datetime | None = None) -> str | None:
    """Answer only complete current-time questions or a complete PT correction.

    Abbreviations such as EST and PST are treated as colloquial zone names. The
    reply uses the abbreviation actually in effect at that instant, including
    daylight-saving time.
    """
    clean = _clean(text)
    if not clean or _SCHEDULING_RE.search(clean):
        return None

    correction = _PACIFIC_CORRECTION_RE.fullmatch(clean)
    query = _CURRENT_TIME_QUERY_RE.fullmatch(clean)
    if correction is None and query is None:
        return None

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    else:
        instant = instant.astimezone(timezone.utc)

    pacific = _clock_text(instant, _PACIFIC)
    eastern = _clock_text(instant, _EASTERN)
    if correction is not None:
        return f"Right. It's {pacific} here; Eastern is {eastern}."

    requested = _ZONE_ALIASES[re.sub(r"\s+", " ", (query.group("zone") or "pacific").lower())]
    if requested == "eastern":
        return f"It's {eastern} right now. Pacific time is {pacific}."
    return f"It's {pacific} right now. Eastern time is {eastern}."
