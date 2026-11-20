import unittest
from datetime import datetime, timezone

from davosbot import espn_parlay


NOW = datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload, *, headers=None, error=None):
        self.payload = payload
        self.headers = headers or {}
        self.error = error
        self.closed = False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if not self.responses:
            raise AssertionError("unexpected ESPN request")
        return self.responses.pop(0)


def _status(
    state="in",
    *,
    name=None,
    completed=None,
    period=3,
    display_clock="6:42",
    clock=198,
    detail=None,
    result=None,
):
    if name is None:
        name = {
            "pre": "STATUS_SCHEDULED",
            "in": "STATUS_IN_PROGRESS",
            "post": "STATUS_FINAL",
        }[state]
    if completed is None:
        completed = state == "post"
    value = {
        "clock": clock,
        "displayClock": display_clock,
        "period": period,
        "type": {
            "id": "2" if state == "in" else "3" if state == "post" else "1",
            "name": name,
            "state": state,
            "completed": completed,
            "description": detail or ("Final" if completed else "In Progress"),
            "detail": detail or ("Final" if completed else "6:42 - 3rd Quarter"),
            "shortDetail": detail or ("Final" if completed else "6:42 - 3rd"),
        },
    }
    if result is not None:
        value["result"] = result
    return value


def _team_payload(
    spec,
    scores,
    *,
    state="in",
    status=None,
    linescores=None,
    winners=None,
    event_id=None,
    scheduled_at="2026-11-21T00:30Z",
):
    linescores = linescores or {}
    winners = winners or set()
    competitors = []
    for competitor in spec.competitors:
        row = {
            "id": competitor.id,
            "homeAway": competitor.home_away,
            "winner": competitor.id in winners,
            "team": {
                "id": competitor.id,
                "displayName": competitor.name,
            },
        }
        if competitor.id in scores:
            row["score"] = str(scores[competitor.id])
        if competitor.id in linescores:
            row["linescores"] = [
                {"displayValue": str(value)} for value in linescores[competitor.id]
            ]
        competitors.append(row)
    actual_id = event_id or spec.event_id
    return {
        "header": {
            "id": actual_id,
            "competitions": [
                {
                    "id": actual_id,
                    "date": scheduled_at,
                    "status": status or _status(state),
                    "competitors": competitors,
                }
            ],
        }
    }


def _ufc_event_payload(spec, *, winners=None, athlete_ids=None):
    winners = winners or set()
    athlete_ids = athlete_ids or [competitor.id for competitor in spec.competitors]
    return {
        "id": spec.event_id,
        # The card may still be marked pre while an individual bout status is
        # the authoritative live signal.
        "status": _status("pre", period=0, display_clock="-", clock=0),
        "competitions": [
            {
                "id": spec.bout_id,
                "date": "2026-11-21T18:00Z",
                "competitors": [
                    {
                        "id": athlete_id,
                        "winner": athlete_id in winners,
                        "athlete": {
                            "$ref": (
                                "http://sports.core.api.espn.com/v2/sports/mma/"
                                f"athletes/{athlete_id}?lang=en&region=us"
                            )
                        },
                    }
                    for athlete_id in athlete_ids
                ],
            }
        ],
    }


def _snapshot(
    spec,
    *,
    state="in",
    completed=None,
    period=3,
    display_clock="6:42",
    clock_seconds=198,
    scores=None,
    period_scores=None,
    winner_ids=(),
    status_name=None,
    status_detail=None,
    ufc_result="",
):
    if completed is None:
        completed = state == "post"
    if status_name is None:
        status_name = {
            "pre": "STATUS_SCHEDULED",
            "in": "STATUS_IN_PROGRESS",
            "post": "STATUS_FINAL",
        }[state]
    if status_detail is None:
        status_detail = "Final" if completed else "In Progress"
    return espn_parlay.EventSnapshot(
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
        scheduled_at=None,
        scores=scores or {},
        period_scores=period_scores or {},
        winner_ids=frozenset(winner_ids),
        ufc_result=ufc_result,
    )


class ParlayInventoryTests(unittest.TestCase):
    def test_exact_twelve_events_and_fourteen_non_ufc_picks(self):
        events = espn_parlay.TRACKED_EVENTS
        self.assertEqual(12, len(events))
        self.assertEqual(14, sum(len(event.picks) for event in events))
        self.assertEqual(5, sum(event.date_et == "2026-11-20" for event in events))
        self.assertEqual(7, sum(event.date_et == "2026-11-21" for event in events))
        self.assertFalse(any(event.is_ufc for event in events))

        expected = {
            "raptors_pistons": ("401909308", "", {"28", "8"}),
            "pacers_cavaliers": ("401909307", "", {"11", "5"}),
            "clemson_duke": ("401858303", "", {"228", "150"}),
            "marshall_unc": ("401912710", "", {"276", "153"}),
            "new_mexico_wyoming": ("401864542", "", {"167", "2751"}),
            "fulham_man_city": ("401879233", "", {"370", "382"}),
            "byu_kansas": ("401856866", "", {"252", "2305"}),
            "ucla_michigan": ("401858528", "", {"26", "130"}),
            "indiana_washington": ("401858535", "", {"84", "264"}),
            "lsu_tennessee": ("401856754", "", {"99", "2633"}),
            "texas_tech_baylor": ("401856871", "", {"2641", "239"}),
            "georgia_south_carolina": ("401856753", "", {"61", "2579"}),
        }
        self.assertEqual(set(expected), set(espn_parlay.EVENTS_BY_KEY))
        for key, (event_id, bout_id, competitor_ids) in expected.items():
            with self.subTest(key=key):
                event = espn_parlay.EVENTS_BY_KEY[key]
                self.assertEqual(event_id, event.event_id)
                self.assertEqual(bout_id, event.bout_id)
                self.assertEqual(competitor_ids, {row.id for row in event.competitors})

    def test_exact_pick_labels_and_signed_lines(self):
        actual = {
            pick.label: pick.line
            for event in espn_parlay.TRACKED_EVENTS
            for pick in event.picks
        }
        self.assertEqual(
            {
                "Raptors +7": 7.0,
                "Raptors ML": None,
                "Pacers +6": 6.0,
                "Pacers ML": None,
                "Clemson/Duke under 41.5": 41.5,
                "UNC -18.5": -18.5,
                "New Mexico -9": -9.0,
                "Man City FIRST-HALF ML": None,
                "BYU -5.5": -5.5,
                "UCLA -3.5": -3.5,
                "Indiana -7.5": -7.5,
                "Tennessee +1": 1.0,
                "Texas Tech -4.5": -4.5,
                "South Carolina +15": 15.0,
            },
            actual,
        )
        self.assertTrue(
            {"albazi_costa", "ortega_hooker", "jiri_stirling"}.isdisjoint(
                espn_parlay.EVENTS_BY_KEY
            )
        )


class ESPNFetchTests(unittest.TestCase):
    def test_team_summary_fetch_validates_identity_and_uses_returned_status_only(self):
        spec = espn_parlay.EVENTS_BY_KEY["raptors_pistons"]
        response = _Response(_team_payload(spec, {}, state="pre"))
        session = _Session(response)

        snapshot = espn_parlay.fetch_snapshot(
            spec,
            session=session,
            now_utc=NOW,
        )

        self.assertFalse(snapshot.is_live)
        self.assertFalse(snapshot.is_final)
        self.assertEqual("pre", snapshot.phase)
        self.assertEqual(datetime(2026, 11, 21, 0, 30, tzinfo=timezone.utc), snapshot.scheduled_at)
        self.assertTrue(response.closed)
        self.assertEqual(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary",
            session.calls[0][0],
        )
        self.assertEqual({"event": "401909308"}, session.calls[0][1])

    def test_team_fetch_rejects_unmatched_event_and_stale_response(self):
        spec = espn_parlay.EVENTS_BY_KEY["pacers_cavaliers"]
        bad_identity = _Response(
            _team_payload(spec, {}, state="pre", event_id="wrong-event")
        )
        with self.assertRaises(espn_parlay.ESPNDataError) as mismatch:
            espn_parlay.fetch_snapshot(
                spec,
                session=_Session(bad_identity),
                now_utc=NOW,
            )
        self.assertEqual("identity_mismatch", mismatch.exception.code)
        self.assertEqual(spec.key, mismatch.exception.event_key)

        stale = _Response(
            _team_payload(spec, {}, state="pre"),
            headers={"Age": "121"},
        )
        with self.assertRaises(espn_parlay.ESPNDataError) as stale_error:
            espn_parlay.fetch_snapshot(
                spec,
                session=_Session(stale),
                now_utc=NOW,
            )
        self.assertEqual("stale_response", stale_error.exception.code)

    def test_team_fetch_rejects_final_winner_score_conflict(self):
        spec = espn_parlay.EVENTS_BY_KEY["byu_kansas"]
        payload = _team_payload(
            spec,
            {"252": 31, "2305": 24},
            state="post",
            status=_status("post", period=4, display_clock="0:00", clock=0),
            winners={"2305"},
        )
        with self.assertRaises(espn_parlay.ESPNDataError) as error:
            espn_parlay.fetch_snapshot(
                spec,
                session=_Session(_Response(payload)),
                now_utc=NOW,
            )
        self.assertEqual("status_conflict", error.exception.code)

class GradingAndFormattingTests(unittest.TestCase):
    def test_raptors_two_pick_live_alert_uses_exact_spread_formula(self):
        spec = espn_parlay.EVENTS_BY_KEY["raptors_pistons"]
        snapshot = _snapshot(
            spec,
            scores={"8": 74, "28": 69},
            period=3,
            display_clock="6:42",
        )

        self.assertEqual(
            "Q3 6:42 | Detroit 74, Toronto 69 | "
            "Raptors +7: covering by 2 | Raptors ML: trailing by 5.",
            espn_parlay.format_alert(spec, snapshot),
        )

    def test_pacers_two_pick_alert_is_combined(self):
        spec = espn_parlay.EVENTS_BY_KEY["pacers_cavaliers"]
        alert = espn_parlay.format_alert(
            spec,
            _snapshot(spec, scores={"5": 84, "11": 88}, period=2, display_clock="3:10"),
        )
        self.assertEqual(1, alert.count("Pacers +6"))
        self.assertEqual(1, alert.count("Pacers ML"))
        self.assertIn("Pacers +6: covering by 10", alert)
        self.assertIn("Pacers ML: leading by 4", alert)

    def test_final_spreads_win_push_and_lose(self):
        cases = (
            ("marshall_unc", {"153": 90, "276": 70}, "won; covered by 1.5", "won"),
            ("new_mexico_wyoming", {"167": 27, "2751": 18}, "push; on the number", "push"),
            ("byu_kansas", {"252": 24, "2305": 20}, "lost; off cover by 1.5", "lost"),
            ("lsu_tennessee", {"2633": 28, "99": 28}, "won; covered by 1", "won"),
        )
        for key, scores, text, result in cases:
            with self.subTest(key=key):
                spec = espn_parlay.EVENTS_BY_KEY[key]
                snapshot = _snapshot(
                    spec,
                    state="post",
                    period=4,
                    display_clock="0:00",
                    clock_seconds=0,
                    scores=scores,
                )
                grades = espn_parlay.grade_picks(spec, snapshot)
                self.assertEqual(result, grades[0].result)
                self.assertEqual(text, grades[0].text)
                self.assertTrue(espn_parlay.is_settled(spec, snapshot))

    def test_clemson_duke_combined_score_is_always_against_41_5(self):
        spec = espn_parlay.EVENTS_BY_KEY["clemson_duke"]
        live = _snapshot(spec, scores={"228": 14, "150": 10})
        self.assertIn(
            "Clemson/Duke under 41.5: combined 24, 17.5 under 41.5",
            espn_parlay.format_alert(spec, live),
        )
        final = _snapshot(
            spec,
            state="post",
            period=4,
            scores={"228": 24, "150": 18},
        )
        self.assertEqual(
            "lost; combined 42, 0.5 over 41.5",
            espn_parlay.grade_picks(spec, final)[0].text,
        )

    def test_first_half_moneyline_grades_at_halftime_and_draw_loses(self):
        spec = espn_parlay.EVENTS_BY_KEY["fulham_man_city"]
        halftime = _snapshot(
            spec,
            state="in",
            period=1,
            display_clock="45'+2'",
            scores={"382": 1, "370": 1},
            status_name="STATUS_HALFTIME",
            status_detail="HT",
        )
        grade = espn_parlay.grade_picks(spec, halftime)[0]
        self.assertEqual("lost", grade.result)
        self.assertEqual("lost; halftime draw 1-1", grade.text)
        self.assertTrue(espn_parlay.is_settled(spec, halftime))
        self.assertTrue(espn_parlay.format_alert(spec, halftime).startswith("HT | Man City 1, Fulham 1"))

    def test_first_half_moneyline_uses_linescore_if_poll_resumes_after_halftime(self):
        spec = espn_parlay.EVENTS_BY_KEY["fulham_man_city"]
        second_half = _snapshot(
            spec,
            state="in",
            period=2,
            display_clock="61'",
            scores={"382": 1, "370": 2},
            period_scores={"382": (1, 0), "370": (0, 2)},
        )
        grade = espn_parlay.grade_picks(spec, second_half)[0]
        self.assertEqual("won", grade.result)
        self.assertEqual("won; led 1-0 at halftime", grade.text)
        self.assertTrue(grade.settled)
        self.assertTrue(espn_parlay.is_settled(spec, second_half))
        self.assertEqual(
            "1H Final | Man City 1, Fulham 0 | "
            "Man City FIRST-HALF ML: won; led 1-0 at halftime.",
            espn_parlay.format_alert(spec, second_half),
        )

    def test_first_half_final_never_mislabels_full_time_score_as_halftime(self):
        spec = espn_parlay.EVENTS_BY_KEY["fulham_man_city"]
        no_linescores = _snapshot(
            spec,
            state="post",
            period=2,
            display_clock="90'+4'",
            scores={"382": 3, "370": 2},
        )
        alert = espn_parlay.format_alert(spec, no_linescores)
        self.assertIn("1H Final", alert)
        self.assertIn("Man City score unavailable from ESPN", alert)
        self.assertNotIn("Man City 3, Fulham 2", alert)
        self.assertIn("halftime grading unavailable", alert)
        self.assertTrue(espn_parlay.is_terminal_boundary(spec, no_linescores))
        self.assertFalse(espn_parlay.is_settled(spec, no_linescores))

    def test_full_game_final_with_missing_score_is_boundary_but_not_settled(self):
        spec = espn_parlay.EVENTS_BY_KEY["byu_kansas"]
        missing_score = _snapshot(
            spec,
            state="post",
            period=4,
            display_clock="0:00",
            clock_seconds=0,
            scores={"252": 24},
        )
        grade = espn_parlay.grade_picks(spec, missing_score)[0]
        self.assertEqual("review", grade.result)
        self.assertFalse(grade.settled)
        self.assertTrue(espn_parlay.is_terminal_boundary(spec, missing_score))
        self.assertFalse(espn_parlay.is_settled(spec, missing_score))

if __name__ == "__main__":
    unittest.main()
