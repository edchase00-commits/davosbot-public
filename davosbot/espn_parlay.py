"""ESPN-backed snapshots and grading for the owner's Nov. 20-21 parlay.

This module deliberately contains no scheduler, persistence, or delivery code.
Callers decide when to poll and send.  Game phase comes only from ESPN's
status object; a scheduled timestamp is exposed for poll pacing but is never
used to infer that a game or bout started.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

import requests


TEAM_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/{api_path}/summary"
UFC_EVENT_URL = (
    "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/events/{event_id}"
)
UFC_BOUT_STATUS_URL = UFC_EVENT_URL + "/competitions/{bout_id}/status"

HTTP_TIMEOUT_SECONDS = 15.0
MAX_CACHE_AGE_SECONDS = 120.0
MAX_SOURCE_CLOCK_SKEW_SECONDS = 300.0
MAX_SCORE = 999


class ESPNDataError(RuntimeError):
    """A bounded, operator-safe ESPN fetch or validation failure."""

    def __init__(self, code: str, event_key: str, message: str):
        self.code = code
        self.event_key = event_key
        self.message = message[:240]
        super().__init__(f"{event_key}: {self.message}")


@dataclass(frozen=True)
class CompetitorSpec:
    id: str
    name: str
    score_name: str
    home_away: str = ""


@dataclass(frozen=True)
class PickSpec:
    key: str
    label: str
    kind: str
    selected_id: str = ""
    opponent_id: str = ""
    line: float | None = None
    banner: str = ""

    @property
    def alert_label(self) -> str:
        return f"{self.banner} | {self.label}" if self.banner else self.label


@dataclass(frozen=True)
class EventSpec:
    key: str
    date_et: str
    league: str
    api_path: str
    event_id: str
    competitors: tuple[CompetitorSpec, ...]
    picks: tuple[PickSpec, ...]
    bout_id: str = ""

    @property
    def is_ufc(self) -> bool:
        return bool(self.bout_id)


@dataclass(frozen=True)
class EventSnapshot:
    event_key: str
    event_id: str
    bout_id: str
    state: str
    status_name: str
    status_detail: str
    completed: bool
    period: int | None
    display_clock: str
    clock_seconds: float | None
    scheduled_at: datetime | None
    scores: Mapping[str, int] = field(default_factory=dict)
    period_scores: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    winner_ids: frozenset[str] = field(default_factory=frozenset)
    ufc_result: str = ""
    diagnostics: tuple[str, ...] = ()

    @property
    def status_state(self) -> str:
        return self.state

    @property
    def is_live(self) -> bool:
        return self.state == "in" and not self.completed

    @property
    def is_final(self) -> bool:
        return self.state == "post" and self.completed

    @property
    def phase(self) -> str:
        if self.is_live:
            return "live"
        if self.is_final:
            return "final"
        return self.state


@dataclass(frozen=True)
class PickGrade:
    pick_key: str
    label: str
    result: str
    text: str
    settled: bool


def _team(
    team_id: str,
    name: str,
    score_name: str,
    home_away: str,
) -> CompetitorSpec:
    return CompetitorSpec(team_id, name, score_name, home_away)


def _spread(
    key: str,
    label: str,
    selected_id: str,
    opponent_id: str,
    line: float,
) -> PickSpec:
    return PickSpec(key, label, "spread", selected_id, opponent_id, line)


def _ml(
    key: str,
    label: str,
    selected_id: str,
    opponent_id: str,
    *,
    banner: str = "",
) -> PickSpec:
    return PickSpec(key, label, "moneyline", selected_id, opponent_id, banner=banner)


TRACKED_EVENTS: tuple[EventSpec, ...] = (
    EventSpec(
        "raptors_pistons",
        "2026-11-20",
        "NBA",
        "basketball/nba",
        "401909308",
        (
            _team("28", "Toronto Raptors", "Toronto", "away"),
            _team("8", "Detroit Pistons", "Detroit", "home"),
        ),
        (
            _spread("raptors_plus_7", "Raptors +7", "28", "8", 7.0),
            _ml("raptors_ml", "Raptors ML", "28", "8"),
        ),
    ),
    EventSpec(
        "pacers_cavaliers",
        "2026-11-20",
        "NBA",
        "basketball/nba",
        "401909307",
        (
            _team("11", "Indiana Pacers", "Indiana", "away"),
            _team("5", "Cleveland Cavaliers", "Cleveland", "home"),
        ),
        (
            _spread("pacers_plus_6", "Pacers +6", "11", "5", 6.0),
            _ml("pacers_ml", "Pacers ML", "11", "5"),
        ),
    ),
    EventSpec(
        "clemson_duke",
        "2026-11-20",
        "CFB",
        "football/college-football",
        "401858303",
        (
            _team("228", "Clemson Tigers", "Clemson", "away"),
            _team("150", "Duke Blue Devils", "Duke", "home"),
        ),
        (
            PickSpec(
                "clemson_duke_under_41_5",
                "Clemson/Duke under 41.5",
                "total_under",
                line=41.5,
            ),
        ),
    ),
    EventSpec(
        "marshall_unc",
        "2026-11-20",
        "NCAAM",
        "basketball/mens-college-basketball",
        "401912710",
        (
            _team("276", "Marshall Thundering Herd", "Marshall", "away"),
            _team("153", "North Carolina Tar Heels", "North Carolina", "home"),
        ),
        (
            _spread("unc_minus_18_5", "UNC -18.5", "153", "276", -18.5),
        ),
    ),
    EventSpec(
        "new_mexico_wyoming",
        "2026-11-20",
        "CFB",
        "football/college-football",
        "401864542",
        (
            _team("167", "New Mexico Lobos", "New Mexico", "away"),
            _team("2751", "Wyoming Cowboys", "Wyoming", "home"),
        ),
        (
            _spread("new_mexico_minus_9", "New Mexico -9", "167", "2751", -9.0),
        ),
    ),
    EventSpec(
        "fulham_man_city",
        "2026-11-21",
        "EPL",
        "soccer/eng.1",
        "401879233",
        (
            _team("370", "Fulham", "Fulham", "away"),
            _team("382", "Manchester City", "Man City", "home"),
        ),
        (
            PickSpec(
                "man_city_first_half_ml",
                "Man City FIRST-HALF ML",
                "first_half_moneyline",
                "382",
                "370",
            ),
        ),
    ),
    EventSpec(
        "byu_kansas",
        "2026-11-21",
        "CFB",
        "football/college-football",
        "401856866",
        (
            _team("252", "BYU Cougars", "BYU", "away"),
            _team("2305", "Kansas Jayhawks", "Kansas", "home"),
        ),
        (_spread("byu_minus_5_5", "BYU -5.5", "252", "2305", -5.5),),
    ),
    EventSpec(
        "ucla_michigan",
        "2026-11-21",
        "CFB",
        "football/college-football",
        "401858528",
        (
            _team("26", "UCLA Bruins", "UCLA", "away"),
            _team("130", "Michigan Wolverines", "Michigan", "home"),
        ),
        (_spread("ucla_minus_3_5", "UCLA -3.5", "26", "130", -3.5),),
    ),
    EventSpec(
        "indiana_washington",
        "2026-11-21",
        "CFB",
        "football/college-football",
        "401858535",
        (
            _team("84", "Indiana Hoosiers", "Indiana", "away"),
            _team("264", "Washington Huskies", "Washington", "home"),
        ),
        (_spread("indiana_minus_7_5", "Indiana -7.5", "84", "264", -7.5),),
    ),
    EventSpec(
        "lsu_tennessee",
        "2026-11-21",
        "CFB",
        "football/college-football",
        "401856754",
        (
            _team("99", "LSU Tigers", "LSU", "away"),
            _team("2633", "Tennessee Volunteers", "Tennessee", "home"),
        ),
        (_spread("tennessee_plus_1", "Tennessee +1", "2633", "99", 1.0),),
    ),
    EventSpec(
        "texas_tech_baylor",
        "2026-11-21",
        "CFB",
        "football/college-football",
        "401856871",
        (
            _team("2641", "Texas Tech Red Raiders", "Texas Tech", "away"),
            _team("239", "Baylor Bears", "Baylor", "home"),
        ),
        (
            _spread(
                "texas_tech_minus_4_5",
                "Texas Tech -4.5",
                "2641",
                "239",
                -4.5,
            ),
        ),
    ),
    EventSpec(
        "georgia_south_carolina",
        "2026-11-21",
        "CFB",
        "football/college-football",
        "401856753",
        (
            _team("61", "Georgia Bulldogs", "Georgia", "away"),
            _team("2579", "South Carolina Gamecocks", "South Carolina", "home"),
        ),
        (
            _spread(
                "south_carolina_plus_15",
                "South Carolina +15",
                "2579",
                "61",
                15.0,
            ),
        ),
    ),
)

EVENTS_BY_KEY: Mapping[str, EventSpec] = {event.key: event for event in TRACKED_EVENTS}


def _bounded(value: object, limit: int = 80) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _error(spec: EventSpec, code: str, message: str) -> ESPNDataError:
    return ESPNDataError(code, spec.key, message)


def _response_header(headers: object, name: str) -> object | None:
    if not hasattr(headers, "get"):
        return None
    value = headers.get(name)  # type: ignore[union-attr]
    if value is not None:
        return value
    value = headers.get(name.lower())  # type: ignore[union-attr]
    if value is not None:
        return value
    try:
        for key, candidate in headers.items():  # type: ignore[union-attr]
            if str(key).lower() == name.lower():
                return candidate
    except (AttributeError, TypeError):
        return None
    return None


def _validate_freshness(
    spec: EventSpec,
    response: object,
    now_utc: datetime,
) -> None:
    headers = getattr(response, "headers", {}) or {}
    if not hasattr(headers, "get"):
        raise _error(spec, "malformed_headers", "ESPN response headers were malformed")

    age = _response_header(headers, "Age")
    if age is not None:
        try:
            age_seconds = float(age)
        except (TypeError, ValueError):
            raise _error(spec, "malformed_headers", "ESPN cache age was malformed") from None
        if (
            not math.isfinite(age_seconds)
            or age_seconds < 0
            or age_seconds > MAX_CACHE_AGE_SECONDS
        ):
            raise _error(spec, "stale_response", "ESPN response was stale")

    source_date = _response_header(headers, "Date")
    if source_date:
        try:
            source_time = parsedate_to_datetime(str(source_date))
            if source_time.tzinfo is None:
                source_time = source_time.replace(tzinfo=timezone.utc)
            source_time = source_time.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            raise _error(spec, "malformed_headers", "ESPN response date was malformed") from None
        if abs((now_utc - source_time).total_seconds()) > MAX_SOURCE_CLOCK_SKEW_SECONDS:
            raise _error(spec, "stale_response", "ESPN response clock was stale")

    warning = str(_response_header(headers, "Warning") or "")
    if re.search(r"(?:^|\s|,)110(?:\s|$)", warning):
        raise _error(spec, "stale_response", "ESPN response declared stale")


def _fetch_json(
    spec: EventSpec,
    http: object,
    url: str,
    *,
    params: Mapping[str, str],
    timeout: float,
    now_utc: datetime,
) -> dict:
    response = None
    try:
        response = http.get(url, params=dict(params), timeout=timeout)  # type: ignore[union-attr]
        response.raise_for_status()
        _validate_freshness(spec, response, now_utc)
        payload = response.json()
    except ESPNDataError:
        raise
    except Exception as exc:
        raise _error(
            spec,
            "fetch_failed",
            f"ESPN request failed ({type(exc).__name__})",
        ) from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not isinstance(payload, dict):
        raise _error(spec, "malformed_payload", "ESPN response was not an object")
    return payload


def _parse_datetime(spec: EventSpec, value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise _error(spec, "malformed_payload", "ESPN event timestamp was malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _error(spec, "malformed_payload", "ESPN event timestamp was malformed") from None
    if parsed.tzinfo is None:
        raise _error(spec, "malformed_payload", "ESPN event timestamp lacked a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_status(
    spec: EventSpec,
    status: object,
) -> tuple[str, str, str, bool, int | None, str, float | None, tuple[str, ...]]:
    if not isinstance(status, dict) or not isinstance(status.get("type"), dict):
        raise _error(spec, "malformed_status", "ESPN status was missing")
    status_type = status["type"]
    state = status_type.get("state")
    name = status_type.get("name")
    completed = status_type.get("completed")
    if state not in {"pre", "in", "post"}:
        raise _error(spec, "malformed_status", "ESPN status state was unsupported")
    if not isinstance(name, str) or not name:
        raise _error(spec, "malformed_status", "ESPN status name was missing")
    if not isinstance(completed, bool):
        raise _error(spec, "malformed_status", "ESPN completion state was missing")
    if completed and state != "post":
        raise _error(spec, "status_conflict", "ESPN completion state contradicted its phase")

    upper_name = name.upper()
    if state == "pre" and any(
        token in upper_name for token in ("IN_PROGRESS", "HALFTIME", "FINAL", "FULL_TIME")
    ):
        raise _error(spec, "status_conflict", "ESPN pregame status contradicted its name")
    if state == "in" and any(token in upper_name for token in ("SCHEDULED", "FINAL", "FULL_TIME")):
        raise _error(spec, "status_conflict", "ESPN live status contradicted its name")
    if state == "post" and "IN_PROGRESS" in upper_name:
        raise _error(spec, "status_conflict", "ESPN postgame status contradicted its name")

    detail = next(
        (
            str(value)
            for value in (
                status_type.get("shortDetail"),
                status_type.get("detail"),
                status_type.get("description"),
            )
            if isinstance(value, str) and value
        ),
        name,
    )
    diagnostics: list[str] = []

    raw_period = status.get("period")
    period = None
    if raw_period not in (None, ""):
        if isinstance(raw_period, bool):
            raise _error(spec, "malformed_status", "ESPN period was malformed")
        try:
            parsed_period = int(raw_period)
        except (TypeError, ValueError):
            raise _error(spec, "malformed_status", "ESPN period was malformed") from None
        if parsed_period < 0 or parsed_period > 20:
            raise _error(spec, "malformed_status", "ESPN period was out of range")
        period = parsed_period or None

    raw_display_clock = status.get("displayClock")
    if raw_display_clock in (None, "", "-"):
        display_clock = ""
    elif isinstance(raw_display_clock, str):
        display_clock = raw_display_clock.strip()
    else:
        raise _error(spec, "malformed_status", "ESPN display clock was malformed")

    raw_clock = status.get("clock")
    clock_seconds = None
    if raw_clock not in (None, ""):
        if isinstance(raw_clock, bool):
            raise _error(spec, "malformed_status", "ESPN clock was malformed")
        try:
            clock_seconds = float(raw_clock)
        except (TypeError, ValueError):
            raise _error(spec, "malformed_status", "ESPN clock was malformed") from None
        if not math.isfinite(clock_seconds) or clock_seconds < 0 or clock_seconds > 7200:
            raise _error(spec, "malformed_status", "ESPN clock was out of range")

    if state == "in":
        if period is None:
            diagnostics.append("ESPN live period unavailable")
        if not display_clock:
            diagnostics.append("ESPN live clock unavailable")
    return (
        state,
        name,
        detail,
        completed,
        period,
        display_clock,
        clock_seconds,
        tuple(diagnostics),
    )


def _parse_score(spec: EventSpec, value: object) -> int:
    if isinstance(value, bool):
        raise _error(spec, "malformed_score", "ESPN score was malformed")
    try:
        numeric = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        raise _error(spec, "malformed_score", "ESPN score was malformed") from None
    if not math.isfinite(numeric) or not numeric.is_integer() or not 0 <= numeric <= MAX_SCORE:
        raise _error(spec, "malformed_score", "ESPN score was out of range")
    return int(numeric)


def _parse_linescores(spec: EventSpec, value: object) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise _error(spec, "malformed_score", "ESPN linescores were malformed")
    parsed: list[int] = []
    for row in value:
        if not isinstance(row, dict):
            raise _error(spec, "malformed_score", "ESPN linescore was malformed")
        raw_value = row.get("displayValue", row.get("value"))
        if raw_value in (None, ""):
            raise _error(spec, "malformed_score", "ESPN linescore value was missing")
        parsed.append(_parse_score(spec, raw_value))
    return tuple(parsed)


def _parse_team_snapshot(spec: EventSpec, payload: dict) -> EventSnapshot:
    header = payload.get("header")
    if not isinstance(header, dict):
        raise _error(spec, "malformed_payload", "ESPN summary header was missing")
    if str(header.get("id") or "") != spec.event_id:
        actual = _bounded(header.get("id") or "missing")
        raise _error(
            spec,
            "identity_mismatch",
            f"expected ESPN event {spec.event_id}; received {actual}",
        )
    competitions = header.get("competitions")
    if not isinstance(competitions, list):
        raise _error(spec, "malformed_payload", "ESPN competitions were malformed")
    matches = [
        row
        for row in competitions
        if isinstance(row, dict) and str(row.get("id") or "") == spec.event_id
    ]
    if len(matches) != 1:
        raise _error(spec, "identity_mismatch", "expected ESPN competition was not unique")
    competition = matches[0]
    (
        state,
        status_name,
        status_detail,
        completed,
        period,
        display_clock,
        clock_seconds,
        status_diagnostics,
    ) = _parse_status(spec, competition.get("status"))

    rows = competition.get("competitors")
    if not isinstance(rows, list) or len(rows) != 2 or any(not isinstance(row, dict) for row in rows):
        raise _error(spec, "malformed_payload", "ESPN competitors were malformed")
    expected = {competitor.id: competitor for competitor in spec.competitors}
    actual_ids: set[str] = set()
    scores: dict[str, int] = {}
    period_scores: dict[str, tuple[int, ...]] = {}
    winner_ids: set[str] = set()
    diagnostics = list(status_diagnostics)

    for row in rows:
        team = row.get("team")
        if not isinstance(team, dict):
            raise _error(spec, "malformed_payload", "ESPN team identity was missing")
        row_id = str(row.get("id") or "")
        team_id = str(team.get("id") or "")
        if not row_id or row_id != team_id or team_id not in expected:
            raise _error(spec, "identity_mismatch", "ESPN competitor identity did not match")
        if team_id in actual_ids:
            raise _error(spec, "identity_mismatch", "ESPN competitor identity was duplicated")
        actual_ids.add(team_id)
        if row.get("homeAway") != expected[team_id].home_away:
            raise _error(spec, "identity_mismatch", "ESPN home/away identity did not match")

        winner = row.get("winner")
        if winner not in (None, True, False):
            raise _error(spec, "malformed_payload", "ESPN winner flag was malformed")
        if winner is True:
            winner_ids.add(team_id)

        raw_score = row.get("score")
        if raw_score in (None, ""):
            if state != "pre":
                diagnostics.append(f"ESPN score unavailable for {expected[team_id].score_name}")
        else:
            scores[team_id] = _parse_score(spec, raw_score)
        period_scores[team_id] = _parse_linescores(spec, row.get("linescores"))

    if actual_ids != set(expected):
        raise _error(spec, "identity_mismatch", "ESPN matchup teams did not match")
    if state != "post" and winner_ids:
        raise _error(spec, "status_conflict", "ESPN nonfinal game declared a winner")
    if completed and state == "post" and len(scores) == 2:
        first, second = (competitor.id for competitor in spec.competitors)
        if scores[first] == scores[second]:
            if spec.league in {"NBA", "NCAAM"}:
                raise _error(spec, "status_conflict", "ESPN basketball final score was tied")
            if winner_ids:
                raise _error(spec, "status_conflict", "ESPN tied final declared a winner")
        elif winner_ids:
            score_winner = first if scores[first] > scores[second] else second
            if winner_ids != {score_winner}:
                raise _error(spec, "status_conflict", "ESPN final winner contradicted its score")

    if spec.league == "EPL" and _first_half_is_complete_fields(
        state, completed, status_name, status_detail, period
    ):
        for competitor in spec.competitors:
            if not period_scores.get(competitor.id) and not _status_is_halftime(
                status_name, status_detail
            ):
                diagnostics.append(
                    f"ESPN first-half linescore unavailable for {competitor.score_name}"
                )

    return EventSnapshot(
        event_key=spec.key,
        event_id=spec.event_id,
        bout_id="",
        state=state,
        status_name=status_name,
        status_detail=status_detail,
        completed=completed,
        period=period,
        display_clock=display_clock,
        clock_seconds=clock_seconds,
        scheduled_at=_parse_datetime(spec, competition.get("date")),
        scores=scores,
        period_scores=period_scores,
        winner_ids=frozenset(winner_ids),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )


def _athlete_id(spec: EventSpec, competitor: dict) -> str:
    competitor_id = str(competitor.get("id") or "")
    athlete = competitor.get("athlete")
    if not isinstance(athlete, dict):
        raise _error(spec, "malformed_payload", "ESPN UFC athlete identity was missing")
    athlete_id = str(athlete.get("id") or "")
    ref = athlete.get("$ref")
    if not athlete_id and isinstance(ref, str):
        match = re.search(r"/athletes/(\d+)(?:\?|$)", ref)
        athlete_id = match.group(1) if match else ""
    if not competitor_id or competitor_id != athlete_id:
        raise _error(spec, "identity_mismatch", "ESPN UFC athlete identity did not match")
    return competitor_id


def _parse_ufc_snapshot(spec: EventSpec, event_payload: dict, status_payload: dict) -> EventSnapshot:
    if str(event_payload.get("id") or "") != spec.event_id:
        actual = _bounded(event_payload.get("id") or "missing")
        raise _error(
            spec,
            "identity_mismatch",
            f"expected UFC card {spec.event_id}; received {actual}",
        )
    competitions = event_payload.get("competitions")
    if not isinstance(competitions, list):
        raise _error(spec, "malformed_payload", "ESPN UFC competitions were malformed")
    bouts = [
        row
        for row in competitions
        if isinstance(row, dict) and str(row.get("id") or "") == spec.bout_id
    ]
    if len(bouts) != 1:
        raise _error(spec, "identity_mismatch", "expected ESPN UFC bout was not unique")
    bout = bouts[0]
    rows = bout.get("competitors")
    if not isinstance(rows, list) or len(rows) != 2 or any(not isinstance(row, dict) for row in rows):
        raise _error(spec, "malformed_payload", "ESPN UFC competitors were malformed")

    expected_ids = {competitor.id for competitor in spec.competitors}
    actual_ids: set[str] = set()
    winner_ids: set[str] = set()
    for row in rows:
        athlete_id = _athlete_id(spec, row)
        if athlete_id not in expected_ids or athlete_id in actual_ids:
            raise _error(spec, "identity_mismatch", "ESPN UFC matchup did not match")
        actual_ids.add(athlete_id)
        winner = row.get("winner")
        if winner not in (None, True, False):
            raise _error(spec, "malformed_payload", "ESPN UFC winner flag was malformed")
        if winner is True:
            winner_ids.add(athlete_id)
    if actual_ids != expected_ids:
        raise _error(spec, "identity_mismatch", "ESPN UFC matchup athletes did not match")

    (
        state,
        status_name,
        status_detail,
        completed,
        period,
        display_clock,
        clock_seconds,
        status_diagnostics,
    ) = _parse_status(spec, status_payload)
    # ESPN's MMA status clock is elapsed time within the round.  Completed
    # stoppages expose the official finish as, for example, clock=203 and
    # displayClock="3:23"; completed decisions expose 300 / "5:00".  Require
    # those two provider fields to agree before using the number for the 2.5
    # round cutoff.  The same status resource is used while a bout is live.
    display_elapsed = _mma_display_clock_seconds(display_clock)
    if clock_seconds is not None and clock_seconds > 300:
        raise _error(spec, "malformed_status", "ESPN UFC round clock was out of range")
    if clock_seconds is None and display_elapsed is not None:
        clock_seconds = float(display_elapsed)
    elif (
        clock_seconds is not None
        and display_elapsed is not None
        and abs(clock_seconds - display_elapsed) > 1
    ):
        raise _error(spec, "status_conflict", "ESPN UFC clock fields contradicted each other")
    if state != "post" and winner_ids:
        raise _error(spec, "status_conflict", "ESPN nonfinal UFC bout declared a winner")
    if len(winner_ids) > 1:
        raise _error(spec, "status_conflict", "ESPN UFC bout declared multiple winners")

    raw_result = status_payload.get("result")
    if raw_result in (None, ""):
        raw_result = {}
    if not isinstance(raw_result, dict):
        raise _error(spec, "malformed_status", "ESPN UFC result was malformed")
    result_name = next(
        (
            str(raw_result.get(key))
            for key in ("displayName", "name", "shortDisplayName")
            if isinstance(raw_result.get(key), str) and raw_result.get(key)
        ),
        "",
    )
    result_key = str(raw_result.get("name") or result_name).lower().replace("_", " ")
    if completed and any(token in result_key for token in ("draw", "no contest")) and winner_ids:
        raise _error(spec, "status_conflict", "ESPN UFC result contradicted its winner")

    diagnostics = list(status_diagnostics)
    if state == "in" and not display_clock:
        diagnostics.append("ESPN live UFC bout clock unavailable")
    if completed and not winner_ids and not any(
        token in result_key for token in ("draw", "no contest")
    ):
        diagnostics.append("ESPN final UFC winner unavailable")
    if completed and (period is None or clock_seconds is None):
        diagnostics.append("ESPN official UFC finish round/time unavailable")

    return EventSnapshot(
        event_key=spec.key,
        event_id=spec.event_id,
        bout_id=spec.bout_id,
        state=state,
        status_name=status_name,
        status_detail=status_detail,
        completed=completed,
        period=period,
        display_clock=display_clock,
        clock_seconds=clock_seconds,
        scheduled_at=_parse_datetime(spec, bout.get("date")),
        winner_ids=frozenset(winner_ids),
        ufc_result=result_name,
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )


def _mma_display_clock_seconds(display_clock: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", display_clock or "")
    if not match:
        return None
    minutes, seconds = (int(part) for part in match.groups())
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


def fetch_snapshot(
    spec: EventSpec,
    *,
    session: object | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    now_utc: datetime | None = None,
) -> EventSnapshot:
    """Fetch and validate one direct ESPN event/bout snapshot.

    ``scheduled_at`` is metadata only.  ``is_live`` and ``is_final`` are
    derived exclusively from ESPN's returned status state and completion flag.
    """

    if spec.key not in EVENTS_BY_KEY or EVENTS_BY_KEY[spec.key] != spec:
        raise _error(spec, "unknown_event", "event is not in the tracked parlay")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise _error(spec, "invalid_timeout", "ESPN timeout must be positive")
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        raise _error(spec, "invalid_clock", "freshness clock lacked a timezone")
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    http = session or requests

    if not spec.is_ufc:
        payload = _fetch_json(
            spec,
            http,
            TEAM_SUMMARY_URL.format(api_path=spec.api_path),
            params={"event": spec.event_id},
            timeout=float(timeout),
            now_utc=now_utc,
        )
        return _parse_team_snapshot(spec, payload)

    event_payload = _fetch_json(
        spec,
        http,
        UFC_EVENT_URL.format(event_id=spec.event_id),
        params={"lang": "en", "region": "us"},
        timeout=float(timeout),
        now_utc=now_utc,
    )
    status_payload = _fetch_json(
        spec,
        http,
        UFC_BOUT_STATUS_URL.format(event_id=spec.event_id, bout_id=spec.bout_id),
        params={"lang": "en", "region": "us"},
        timeout=float(timeout),
        now_utc=now_utc,
    )
    return _parse_ufc_snapshot(spec, event_payload, status_payload)


def _number(value: float) -> str:
    rounded = round(value, 6)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


def _missing_score_grade(pick: PickSpec) -> PickGrade:
    return PickGrade(
        pick.key,
        pick.alert_label,
        "review",
        "grading unavailable; ESPN score is missing",
        False,
    )


def _grade_spread(pick: PickSpec, snapshot: EventSnapshot) -> PickGrade:
    if (
        pick.selected_id not in snapshot.scores
        or pick.opponent_id not in snapshot.scores
        or pick.line is None
    ):
        return _missing_score_grade(pick)
    margin = (
        snapshot.scores[pick.selected_id]
        - snapshot.scores[pick.opponent_id]
        + pick.line
    )
    if snapshot.is_final:
        if margin > 0:
            return PickGrade(
                pick.key,
                pick.alert_label,
                "won",
                f"won; covered by {_number(margin)}",
                True,
            )
        if margin == 0:
            return PickGrade(pick.key, pick.alert_label, "push", "push; on the number", True)
        return PickGrade(
            pick.key,
            pick.alert_label,
            "lost",
            f"lost; off cover by {_number(-margin)}",
            True,
        )
    if margin > 0:
        text = f"covering by {_number(margin)}"
    elif margin == 0:
        text = "on the number"
    else:
        text = f"off cover by {_number(-margin)}"
    return PickGrade(pick.key, pick.alert_label, "live", text, False)


def _grade_moneyline(pick: PickSpec, snapshot: EventSnapshot, *, ufc: bool) -> PickGrade:
    if ufc:
        if not snapshot.is_final:
            return PickGrade(
                pick.key,
                pick.alert_label,
                "live" if snapshot.is_live else "pending",
                "official result pending; ESPN has not declared a winner",
                False,
            )
        if pick.selected_id in snapshot.winner_ids:
            return PickGrade(pick.key, pick.alert_label, "won", "won", True)
        if snapshot.winner_ids:
            return PickGrade(pick.key, pick.alert_label, "lost", "lost", True)
        result = snapshot.ufc_result or "no official winner"
        result_key = result.lower().replace("_", " ")
        explicit_terminal_review = any(
            token in result_key for token in ("draw", "no contest")
        )
        return PickGrade(
            pick.key,
            pick.alert_label,
            "review",
            f"{result}; book grading needed",
            explicit_terminal_review,
        )

    if pick.selected_id not in snapshot.scores or pick.opponent_id not in snapshot.scores:
        return _missing_score_grade(pick)
    margin = snapshot.scores[pick.selected_id] - snapshot.scores[pick.opponent_id]
    if snapshot.is_final:
        if margin > 0:
            return PickGrade(pick.key, pick.alert_label, "won", "won", True)
        if margin < 0:
            return PickGrade(pick.key, pick.alert_label, "lost", "lost", True)
        return PickGrade(
            pick.key,
            pick.alert_label,
            "review",
            "final score tied; book grading needed",
            True,
        )
    if margin > 0:
        text = f"leading by {_number(margin)}"
    elif margin == 0:
        text = "tied"
    else:
        text = f"trailing by {_number(-margin)}"
    return PickGrade(pick.key, pick.alert_label, "live", text, False)


def _grade_total_under(pick: PickSpec, spec: EventSpec, snapshot: EventSnapshot) -> PickGrade:
    if pick.line is None or any(competitor.id not in snapshot.scores for competitor in spec.competitors):
        return _missing_score_grade(pick)
    total = sum(snapshot.scores[competitor.id] for competitor in spec.competitors)
    distance = pick.line - total
    if distance > 0:
        comparison = f"{_number(distance)} under 41.5"
    elif distance == 0:
        comparison = "on 41.5"
    else:
        comparison = f"{_number(-distance)} over 41.5"
    if snapshot.is_final:
        result = "won" if distance > 0 else "push" if distance == 0 else "lost"
        return PickGrade(
            pick.key,
            pick.alert_label,
            result,
            f"{result}; combined {total}, {comparison}",
            True,
        )
    return PickGrade(
        pick.key,
        pick.alert_label,
        "live",
        f"combined {total}, {comparison}",
        False,
    )


def _status_is_halftime(status_name: str, status_detail: str) -> bool:
    joined = f"{status_name} {status_detail}".lower().replace("_", " ")
    return "halftime" in joined or "half time" in joined or status_detail.strip().upper() == "HT"


def _first_half_is_complete_fields(
    state: str,
    completed: bool,
    status_name: str,
    status_detail: str,
    period: int | None,
) -> bool:
    if _status_is_halftime(status_name, status_detail):
        return True
    if state == "post" and completed:
        return True
    return state == "in" and period is not None and period >= 2


def _first_half_scores(
    pick: PickSpec,
    snapshot: EventSnapshot,
) -> tuple[int, int] | None:
    is_halftime = _status_is_halftime(snapshot.status_name, snapshot.status_detail)
    selected_lines = snapshot.period_scores.get(pick.selected_id, ())
    opponent_lines = snapshot.period_scores.get(pick.opponent_id, ())
    if selected_lines and opponent_lines:
        return selected_lines[0], opponent_lines[0]
    if is_halftime and (
        pick.selected_id in snapshot.scores and pick.opponent_id in snapshot.scores
    ):
        return snapshot.scores[pick.selected_id], snapshot.scores[pick.opponent_id]
    return None


def _grade_first_half_moneyline(pick: PickSpec, snapshot: EventSnapshot) -> PickGrade:
    half_complete = _first_half_is_complete_fields(
        snapshot.state,
        snapshot.completed,
        snapshot.status_name,
        snapshot.status_detail,
        snapshot.period,
    )
    if half_complete:
        scores = _first_half_scores(pick, snapshot)
        if scores is None:
            return PickGrade(
                pick.key,
                pick.alert_label,
                "review",
                "halftime grading unavailable; ESPN first-half score is missing",
                False,
            )
        selected, opponent = scores
        if selected > opponent:
            return PickGrade(
                pick.key,
                pick.alert_label,
                "won",
                f"won; led {selected}-{opponent} at halftime",
                True,
            )
        if selected == opponent:
            return PickGrade(
                pick.key,
                pick.alert_label,
                "lost",
                f"lost; halftime draw {selected}-{opponent}",
                True,
            )
        return PickGrade(
            pick.key,
            pick.alert_label,
            "lost",
            f"lost; trailed {selected}-{opponent} at halftime",
            True,
        )

    if pick.selected_id not in snapshot.scores or pick.opponent_id not in snapshot.scores:
        return _missing_score_grade(pick)
    margin = snapshot.scores[pick.selected_id] - snapshot.scores[pick.opponent_id]
    if margin > 0:
        text = f"leading by {_number(margin)}"
    elif margin == 0:
        text = "tied; a halftime draw loses"
    else:
        text = f"trailing by {_number(-margin)}"
    return PickGrade(pick.key, pick.alert_label, "live", text, False)


def _grade_ufc_under(pick: PickSpec, snapshot: EventSnapshot) -> PickGrade:
    period = snapshot.period
    elapsed = snapshot.clock_seconds
    result_key = snapshot.ufc_result.lower().replace("_", " ")
    if snapshot.is_final:
        if any(token in result_key for token in ("draw", "no contest")):
            result = snapshot.ufc_result or "no official winner"
            return PickGrade(
                pick.key,
                pick.alert_label,
                "review",
                f"{result}; book grading needed",
                True,
            )
        if period is None or elapsed is None:
            return PickGrade(
                pick.key,
                pick.alert_label,
                "review",
                "official finish round/time unavailable from ESPN; book grading needed",
                False,
            )
        if period < 3 or (period == 3 and elapsed < 150):
            return PickGrade(pick.key, pick.alert_label, "won", "won", True)
        if period == 3 and elapsed == 150:
            return PickGrade(
                pick.key,
                pick.alert_label,
                "review",
                "official finish exactly at R3 2:30 elapsed; book grading needed",
                True,
            )
        return PickGrade(pick.key, pick.alert_label, "lost", "lost; cutoff passed", True)

    if period is None:
        return PickGrade(
            pick.key,
            pick.alert_label,
            "live" if snapshot.is_live else "pending",
            "cutoff status unavailable; ESPN live round is missing",
            False,
        )
    if period < 3:
        suffix = ""
        if snapshot.is_live and not snapshot.display_clock:
            suffix = "; live bout clock unavailable from ESPN"
        return PickGrade(
            pick.key,
            pick.alert_label,
            "live",
            f"R3 2:30 elapsed cutoff still ahead{suffix}",
            False,
        )
    if period > 3:
        return PickGrade(
            pick.key,
            pick.alert_label,
            "live",
            "R3 2:30 elapsed cutoff has passed",
            False,
        )
    if elapsed is None:
        return PickGrade(
            pick.key,
            pick.alert_label,
            "live",
            "cutoff status unavailable; ESPN live bout clock is missing",
            False,
        )
    if elapsed < 150:
        text = "R3 2:30 elapsed cutoff still ahead"
    elif elapsed == 150:
        text = "at the R3 2:30 elapsed cutoff; official finish pending"
    else:
        text = "R3 2:30 elapsed cutoff has passed"
    return PickGrade(pick.key, pick.alert_label, "live", text, False)


def grade_picks(spec: EventSpec, snapshot: EventSnapshot) -> tuple[PickGrade, ...]:
    if snapshot.event_key != spec.key or snapshot.event_id != spec.event_id:
        raise _error(spec, "identity_mismatch", "snapshot does not belong to this event")
    if spec.is_ufc and snapshot.bout_id != spec.bout_id:
        raise _error(spec, "identity_mismatch", "snapshot does not belong to this UFC bout")

    grades: list[PickGrade] = []
    for pick in spec.picks:
        if snapshot.state == "pre":
            grades.append(
                PickGrade(
                    pick.key,
                    pick.alert_label,
                    "pending",
                    "pending; ESPN has not marked this event live",
                    False,
                )
            )
        elif not snapshot.is_live and not snapshot.is_final:
            grades.append(
                PickGrade(
                    pick.key,
                    pick.alert_label,
                    "pending",
                    f"not settled; ESPN status is {snapshot.status_detail}",
                    False,
                )
            )
        elif pick.kind == "spread":
            grades.append(_grade_spread(pick, snapshot))
        elif pick.kind == "moneyline":
            grades.append(_grade_moneyline(pick, snapshot, ufc=spec.is_ufc))
        elif pick.kind == "total_under":
            grades.append(_grade_total_under(pick, spec, snapshot))
        elif pick.kind == "first_half_moneyline":
            grades.append(_grade_first_half_moneyline(pick, snapshot))
        elif pick.kind == "ufc_under_2_5":
            grades.append(_grade_ufc_under(pick, snapshot))
        else:
            raise _error(spec, "unsupported_pick", "tracked pick type was unsupported")
    return tuple(grades)


def is_terminal_boundary(spec: EventSpec, snapshot: EventSnapshot) -> bool:
    """Return whether ESPN reached the event's authoritative stop boundary."""

    if snapshot.event_key != spec.key or snapshot.event_id != spec.event_id:
        raise _error(spec, "identity_mismatch", "snapshot does not belong to this event")
    if spec.is_ufc:
        if snapshot.bout_id != spec.bout_id:
            raise _error(spec, "identity_mismatch", "snapshot does not belong to this UFC bout")
        return snapshot.is_final
    if any(pick.kind == "first_half_moneyline" for pick in spec.picks):
        return _first_half_is_complete_fields(
            snapshot.state,
            snapshot.completed,
            snapshot.status_name,
            snapshot.status_detail,
            snapshot.period,
        )
    return snapshot.is_final


def is_settled(spec: EventSpec, snapshot: EventSnapshot) -> bool:
    """Return whether every pick has enough official information to grade.

    Reaching a terminal provider status is not sufficient when ESPN has not
    yet populated a required score, winner, or finish round/time.  Explicit
    draw/no-contest results and an exact R3 2:30 finish are settled review
    outcomes because additional polling cannot remove the book-grading need.
    """

    if not is_terminal_boundary(spec, snapshot):
        return False
    grades = grade_picks(spec, snapshot)
    return bool(grades) and all(grade.settled for grade in grades)


def _status_segment(spec: EventSpec, snapshot: EventSnapshot) -> str:
    if spec.is_ufc:
        if snapshot.period is None:
            round_text = "Round unavailable from ESPN"
        else:
            round_text = f"R{snapshot.period}"
        clock_text = (
            f"{snapshot.display_clock} elapsed"
            if snapshot.display_clock
            else "clock unavailable from ESPN"
        )
        prefix = "Final " if snapshot.is_final else ""
        return f"{prefix}{round_text} {clock_text}".strip()
    if spec.league == "EPL" and _first_half_is_complete_fields(
        snapshot.state,
        snapshot.completed,
        snapshot.status_name,
        snapshot.status_detail,
        snapshot.period,
    ):
        return "HT" if _status_is_halftime(snapshot.status_name, snapshot.status_detail) else "1H Final"
    if snapshot.is_final:
        return "Final"
    clock_text = snapshot.display_clock or "clock unavailable from ESPN"
    if snapshot.period is None:
        return f"Period unavailable from ESPN {clock_text}"
    if spec.league in {"NBA", "CFB"}:
        return f"Q{snapshot.period} {clock_text}"
    if spec.league == "NCAAM":
        half = {1: "1st Half", 2: "2nd Half"}.get(snapshot.period, f"Period {snapshot.period}")
        return f"{half} {clock_text}"
    if spec.league == "EPL":
        half = "1H" if snapshot.period == 1 else "2H" if snapshot.period == 2 else f"P{snapshot.period}"
        return f"{half} {clock_text}"
    return f"Period {snapshot.period} {clock_text}"


def _score_segment(spec: EventSpec, snapshot: EventSnapshot) -> str:
    if spec.is_ufc:
        return " vs ".join(competitor.name for competitor in spec.competitors)
    ordered = sorted(spec.competitors, key=lambda competitor: competitor.home_away != "home")
    display_scores = snapshot.scores
    first_half_pick = next(
        (pick for pick in spec.picks if pick.kind == "first_half_moneyline"),
        None,
    )
    if first_half_pick and _first_half_is_complete_fields(
        snapshot.state,
        snapshot.completed,
        snapshot.status_name,
        snapshot.status_detail,
        snapshot.period,
    ):
        first_half_scores = _first_half_scores(first_half_pick, snapshot)
        if first_half_scores is not None:
            display_scores = {
                first_half_pick.selected_id: first_half_scores[0],
                first_half_pick.opponent_id: first_half_scores[1],
            }
        else:
            # Never label a second-half or full-time score as the halftime
            # score when ESPN omitted its first-period linescores.
            display_scores = {}
    parts = []
    for competitor in ordered:
        score = display_scores.get(competitor.id)
        score_text = str(score) if score is not None else "score unavailable from ESPN"
        parts.append(f"{competitor.score_name} {score_text}")
    return ", ".join(parts)


def format_alert(spec: EventSpec, snapshot: EventSnapshot) -> str:
    """Render one combined event alert with every tracked original line."""

    grades = grade_picks(spec, snapshot)
    parts = [_status_segment(spec, snapshot), _score_segment(spec, snapshot)]
    parts.extend(f"{grade.label}: {grade.text}" for grade in grades)
    return " | ".join(parts) + "."


__all__ = [
    "CompetitorSpec",
    "ESPNDataError",
    "EVENTS_BY_KEY",
    "EventSnapshot",
    "EventSpec",
    "PickGrade",
    "PickSpec",
    "TRACKED_EVENTS",
    "fetch_snapshot",
    "format_alert",
    "grade_picks",
    "is_settled",
    "is_terminal_boundary",
]
