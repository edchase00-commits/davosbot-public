import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests


logger = logging.getLogger(__name__)

UFC_FIGHT_CARD_TOOL = "ufc_fight_card"

_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
_EVENT_URL = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/events/{event_id}"
_LA = ZoneInfo("America/Los_Angeles")
_REQUEST_RE = re.compile(
    r"\b(?:ufc|mma)\b.{0,80}\b(?:card|fight(?:s)?|event|tonight|tomorrow|main|prelim|odds)\b"
    r"|\b(?:fight\s+card|main\s+card|prelims?)\b",
    re.IGNORECASE | re.DOTALL,
)


def is_ufc_fight_card_request(text: str) -> bool:
    return bool(_REQUEST_RE.search(text or ""))


def _fetch_json(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url.replace("http://", "https://"), params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _event_id_from_ref(ref: str | None) -> str:
    match = re.search(r"/events/(\d+)", ref or "")
    return match.group(1) if match else ""


def _select_event(scoreboard: dict, now_utc: datetime) -> tuple[str, str, datetime | None]:
    events = scoreboard.get("events") or []
    if events:
        event = events[0]
        return str(event.get("id") or ""), event.get("name") or event.get("shortName") or "UFC event", _parse_dt(event.get("date"))

    calendar = []
    for league in scoreboard.get("leagues") or []:
        calendar.extend(league.get("calendar") or [])
    candidates = []
    earliest_current = now_utc - timedelta(hours=12)
    for item in calendar:
        start = _parse_dt(item.get("startDate"))
        event_id = _event_id_from_ref((item.get("event") or {}).get("$ref"))
        if start and event_id and start >= earliest_current:
            candidates.append((start, event_id, item.get("label") or "UFC event"))
    if not candidates:
        return "", "", None
    start, event_id, label = sorted(candidates, key=lambda row: row[0])[0]
    return event_id, label, start


def _format_event_time(dt: datetime | None) -> str:
    if not dt:
        return "time TBD"
    local = dt.astimezone(_LA)
    return local.strftime("%a %b %d, %I:%M %p PT").replace(" 0", " ")


def _venue_text(event: dict) -> str:
    for comp in event.get("competitions") or []:
        venue = comp.get("venue") or {}
        if not venue:
            continue
        name = venue.get("fullName") or ""
        address = venue.get("address") or {}
        city = address.get("city") or ""
        state = address.get("state") or address.get("country") or ""
        location = ", ".join(part for part in (city, state) if part)
        if name and location:
            return f"{name}, {location}"
        return name or location
    return "venue TBD"


def _athlete_name(comp: dict, cache: dict[str, dict]) -> str:
    athlete = comp.get("athlete") or {}
    ref = athlete.get("$ref")
    if ref:
        ref = ref.replace("http://", "https://")
        if ref not in cache:
            cache[ref] = _fetch_json(ref)
        data = cache[ref]
        return data.get("displayName") or data.get("fullName") or data.get("shortName") or "TBD"
    return athlete.get("displayName") or athlete.get("fullName") or "TBD"


def _fighter_label(comp: dict, cache: dict[str, dict]) -> str:
    return _athlete_name(comp, cache)


def _fight_text(comp: dict, cache: dict[str, dict]) -> str:
    competitors = sorted(comp.get("competitors") or [], key=lambda c: c.get("order", 99))
    fighters = [_fighter_label(c, cache) for c in competitors[:2]]
    matchup = " vs ".join(fighters) if fighters else "TBD vs TBD"
    weight = (comp.get("type") or {}).get("text") or "division TBD"
    rounds = comp.get("description") or ""
    extra = f" - {weight}"
    if rounds:
        extra += f", {rounds}"
    return f"{matchup}{extra}"


def _append_fights(lines: list[str], title: str, fights: list[dict], cache: dict[str, dict], limit: int) -> None:
    if not fights:
        return
    lines.append("")
    lines.append(title)
    for idx, comp in enumerate(fights[:limit], start=1):
        lines.append(f"{idx}. {_fight_text(comp, cache)}")


def _segment_name(comp: dict) -> str:
    segment = comp.get("cardSegment") or {}
    raw = (segment.get("name") or segment.get("description") or "").lower()
    if "main" in raw:
        return "main"
    if "prelim" in raw:
        return "prelims"
    return ""


def _split_card(competitions: list[dict]) -> tuple[list[dict], list[dict]]:
    main = [comp for comp in competitions if _segment_name(comp) == "main"]
    prelims = [comp for comp in competitions if _segment_name(comp) == "prelims"]
    unknown = [comp for comp in competitions if comp not in main and comp not in prelims]
    if main or prelims:
        prelims.extend(unknown)
        return main, prelims
    by_date = sorted(competitions, key=lambda comp: comp.get("date") or "")
    return by_date[-5:], by_date[:-5]


def _prefetch_athletes(competitions: list[dict], cache: dict[str, dict]) -> None:
    refs = []
    for comp in competitions:
        for competitor in comp.get("competitors") or []:
            ref = ((competitor.get("athlete") or {}).get("$ref") or "").replace("http://", "https://")
            if ref and ref not in cache:
                refs.append(ref)
    refs = list(dict.fromkeys(refs))
    if not refs:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(refs))) as executor:
        future_map = {executor.submit(_fetch_json, ref): ref for ref in refs}
        for future in as_completed(future_map):
            ref = future_map[future]
            try:
                cache[ref] = future.result()
            except Exception as exc:
                logger.debug("UFC athlete fetch failed for %s: %s", ref, exc)


def get_ufc_fight_card(now_utc: datetime | None = None) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    try:
        scoreboard = _fetch_json(_SCOREBOARD_URL)
        event_id, label, start = _select_event(scoreboard, now_utc)
        if not event_id:
            return "No upcoming UFC card found on ESPN right now."
        event = _fetch_json(_EVENT_URL.format(event_id=event_id), params={"lang": "en", "region": "us"})
    except Exception as exc:
        logger.warning("UFC fight card fetch failed: %s", exc)
        return "UFC card fetch failed from ESPN. Try again later."

    title = event.get("name") or label or "Upcoming UFC card"
    start = _parse_dt(event.get("date")) or start
    competitions = event.get("competitions") or []
    if not competitions:
        return f"{title}\n{_format_event_time(start)} | {_venue_text(event)}\nFight list is not posted on ESPN yet."

    main, prelims = _split_card(competitions)
    cache: dict[str, dict] = {}
    _prefetch_athletes([*main[:6], *prelims[:8]], cache)
    lines = [
        title,
        f"{_format_event_time(start)}",
        f"{_venue_text(event)}",
    ]
    _append_fights(lines, "Main Card", main, cache, 6)
    _append_fights(lines, "Prelims", prelims, cache, 8)
    return "\n".join(lines)
