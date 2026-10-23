import unittest
import threading
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import permutations
from unittest.mock import Mock, patch

from davosbot import main, tools
import test_no_web_tool_permissions as route_fixtures


NOW = datetime(2026, 10, 22, 2, 53, tzinfo=timezone.utc)


class _Response:
    def __init__(self, events, *, headers=None):
        self.events = events
        self.headers = headers or {}
        self.close_calls = 0

    def raise_for_status(self):
        return None

    def json(self):
        return {"events": self.events}

    def close(self):
        self.close_calls += 1


def _event(
    event_id,
    *,
    date,
    away,
    home,
    away_score="",
    home_score="",
    state="pre",
    name="STATUS_SCHEDULED",
    detail="Scheduled",
    away_winner=None,
    home_winner=None,
):
    return {
        "id": event_id,
        "date": date,
        "name": f"{away} at {home}",
        "shortName": f"{away} @ {home}",
        "status": {
            "type": {
                "state": state,
                "name": name,
                "completed": state == "post",
                "shortDetail": detail,
            }
        },
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "away",
                        "score": away_score,
                        "winner": away_winner,
                        "team": {
                            "displayName": away,
                            "shortDisplayName": away.split()[-1],
                            "location": " ".join(away.split()[:-1]),
                            "name": away.split()[-1],
                            "abbreviation": "IND" if "Pacers" in away else "AWY",
                        },
                    },
                    {
                        "homeAway": "home",
                        "score": home_score,
                        "winner": home_winner,
                        "team": {
                            "displayName": home,
                            "shortDisplayName": home.split()[-1],
                            "location": " ".join(home.split()[:-1]),
                            "name": home.split()[-1],
                            "abbreviation": "IND" if "Pacers" in home else "HME",
                        },
                    },
                ]
            }
        ],
    }


FINAL_PACERS = _event(
    "final-pacers",
    date="2026-10-22T00:00:00Z",
    away="Indiana Pacers",
    home="New Orleans Pelicans",
    away_score="123",
    home_score="120",
    state="post",
    name="STATUS_FINAL",
    detail="Final",
    away_winner=True,
    home_winner=False,
)

UPCOMING_PACERS = _event(
    "upcoming-pacers",
    date="2026-10-23T23:00:00Z",
    away="Dallas Mavericks",
    home="Indiana Pacers",
    state="pre",
    detail="10/23 - 7:00 PM EDT",
)


class LiveSportsAnswerTests(unittest.TestCase):
    @staticmethod
    def _scoreboard(url, params=None, timeout=12):
        del url, timeout
        date_key = (params or {}).get("dates")
        if date_key == "20261021":
            return _Response([FINAL_PACERS])
        if date_key == "20261023":
            return _Response([UPCOMING_PACERS])
        return _Response([])

    def test_did_pacers_win_uses_fresh_final_not_next_scheduled_game(self):
        with patch.object(tools.requests, "get", side_effect=self._scoreboard):
            reply = tools._get_live_sports_score("did the pacers win?", now_utc=NOW)

        self.assertIn("Yes — Indiana Pacers beat New Orleans Pelicans 123-120", reply)
        self.assertIn("Final", reply)
        self.assertIn("ESPN checked 7:53 PM PDT", reply)
        self.assertNotIn("Dallas Mavericks", reply)

    def test_native_route_recognizes_exact_incident_wording(self):
        self.assertTrue(tools.is_live_sports_score_request("did the pacers win?"))
        with patch.object(tools.requests, "get", side_effect=self._scoreboard):
            reply = tools.live_sports_native_reply("did the pacers win?", now_utc=NOW)
        self.assertIn("Pacers beat", reply)

    def test_contextless_live_api_followup_asks_for_team_without_network(self):
        for query in (
            "tap into live api and lmk the score",
            "tap into the live api and let me know the score",
            "tap into the live api and let me know the final score",
            "tap into the live api and look up the score",
            "ESPN API: check the score",
            "score, check the ESPN API",
            "check the sports API result",
            "fetch the score from ESPN API",
        ):
            with self.subTest(query=query), patch.object(tools.requests, "get") as lookup:
                self.assertTrue(tools.is_contextless_live_sports_request(query))
                reply = tools.live_sports_native_reply(query, now_utc=NOW)

            lookup.assert_not_called()
            self.assertIn("Which team and league", reply)
            self.assertIn("don't guess", reply)

    def test_unsupported_team_can_continue_to_other_lookup_route(self):
        self.assertTrue(tools.is_live_sports_score_request("did Barcelona win?"))
        self.assertIsNone(tools.live_sports_native_reply("did Barcelona win?", now_utc=NOW))

    def test_named_unsupported_team_in_api_request_is_not_contextless(self):
        for query in (
            "tap into live api and lmk Barcelona score",
            "tap into ESPN api and lmk Barcelona result",
            "check ESPN API and tell me the Inter Miami result",
            "Barcelona result, check the ESPN API",
            "check Barcelona result in the ESPN API",
            "check the Barcelona result using ESPN API",
            "ESPN API: check the Barcelona result",
            "get the Barcelona result from ESPN API",
            "show me the Barcelona result from ESPN API",
            "tell me the Barcelona result from ESPN API",
            "fetch the Barcelona result from ESPN API",
            "pull the Barcelona result from ESPN API",
            "Barcelona result via ESPN API, look it up",
        ):
            with self.subTest(query=query), patch.object(tools.requests, "get") as lookup:
                self.assertTrue(tools.is_live_sports_score_request(query))
                self.assertTrue(tools.is_explicit_live_sports_api_request(query))
                self.assertFalse(tools.is_contextless_live_sports_request(query))
                reply = tools.live_sports_native_reply(query, now_utc=NOW)

            lookup.assert_not_called()
            self.assertIsNone(reply)

    def test_explicit_dates_defer_native_routing_and_direct_tool_requests_web(self):
        for query in (
            "did the Pacers win Monday?",
            "Pacers score Oct 19",
            "Pacers score 10/19",
            "did the Pacers win yesterday?",
            "did the Pacers win two days ago?",
            "did the Pacers win last week?",
            "did the Pacers win this season?",
            "did the Pacers win last weekend?",
            "did the Pacers win the previous season?",
            "did the Pacers win in 2025?",
            "did the Pacers win in October?",
            "Pacers score on the 19th",
            "Pacers score the 19th",
            "Pacers score 19th",
            "did the Pacers win the past 3 weeks?",
            "did the Pacers win their last three games?",
        ):
            with self.subTest(query=query), patch.object(tools.requests, "get") as lookup:
                self.assertTrue(tools.is_live_sports_score_request(query))
                self.assertFalse(tools.supports_live_sports_native_request(query))
                self.assertIsNone(tools.live_sports_native_reply(query, now_utc=NOW))
                direct_reply = tools._get_live_sports_score(query, now_utc=NOW)

            lookup.assert_not_called()
            self.assertIn("dated web lookup", direct_reply)
            self.assertIn("don't guess", direct_reply)

    def test_today_and_tonight_remain_supported_native_dates(self):
        for query in (
            "Pacers score today",
            "what's the Pacers score tonight?",
            "did the Pacers win tonight?",
            "did the Pacers win their last game?",
            "Pacers score in the 1st quarter",
            "did the 49ers win tonight?",
            "76ers score today",
        ):
            with self.subTest(query=query):
                self.assertTrue(tools.supports_live_sports_native_request(query))
        self.assertFalse(tools._has_unsupported_live_sports_date("Pacers won 123-120"))

    def test_non_sports_finish_wording_is_not_a_sports_request(self):
        for text in ("did the upload finish?", "has the backup ended?", "did you finish the report?"):
            with self.subTest(text=text):
                self.assertFalse(tools.is_live_sports_score_request(text))
        self.assertTrue(tools.is_live_sports_score_request("has the game ended?"))

    def test_pregame_espn_eastern_detail_is_normalized_to_pacific(self):
        comp = UPCOMING_PACERS["competitions"][0]
        self.assertEqual(
            "Fri Oct 23, 4:00 PM PDT",
            tools._espn_status_text(UPCOMING_PACERS, comp),
        )

    def test_live_game_reports_current_state_without_declaring_a_winner(self):
        live = _event(
            "live-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="88",
            home_score="84",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="4:39 - 3rd Quarter",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response([live] if (params or {}).get("dates") == "20261021" else [])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("pacers score", now_utc=NOW)
        self.assertIn("Live — NBA", reply)
        self.assertIn("Indiana Pacers 88", reply)
        self.assertIn("4:39 - 3rd Quarter", reply)
        self.assertNotIn("won", reply.lower())

    def test_result_question_prefers_current_live_game_over_older_final(self):
        old_final = _event(
            "old-final-pacers",
            date="2026-10-20T23:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="110",
            home_score="101",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        live = _event(
            "live-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="88",
            home_score="84",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="4:39 - 3rd Quarter",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([old_final])
            if date_key == "20261021":
                return _Response([live])
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("did the pacers win?", now_utc=NOW)

        self.assertIn("Live — NBA", reply)
        self.assertIn("Indiana Pacers 88", reply)
        self.assertNotIn("Chicago Bulls", reply)
        self.assertNotIn("Yes —", reply)

    def test_current_state_question_prefers_tonights_pregame_over_older_final(self):
        old_final = _event(
            "old-final-pacers",
            date="2026-10-21T00:30:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="110",
            home_score="101",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        tonight = _event(
            "tonight-pacers",
            date="2026-10-22T03:30:00Z",
            away="Dallas Mavericks",
            home="Indiana Pacers",
            state="pre",
            detail="Scheduled",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([old_final])
            if date_key == "20261021":
                return _Response([tonight])
            return _Response([])

        for query in (
            "has the pacers game started?",
            "is the pacers game over?",
            "is the pacers game finished?",
            "is the pacers game still going?",
            "what's the Pacers score tonight?",
            "Pacers score today",
        ):
            with self.subTest(query=query), \
                 patch.object(tools.requests, "get", side_effect=scoreboard):
                self.assertTrue(tools.is_live_sports_score_request(query))
                self.assertEqual("status", tools._live_sports_intent(query))
                reply = tools._get_live_sports_score(query, now_utc=NOW)

            self.assertIn("Not started — NBA", reply)
            self.assertIn("Dallas Mavericks @ Indiana Pacers", reply)
            self.assertNotIn("Chicago Bulls", reply)

        self.assertEqual("score", tools._live_sports_intent("Pacers score"))

    def test_result_question_still_prefers_final_over_tonights_pregame(self):
        old_final = _event(
            "old-final-pacers",
            date="2026-10-21T00:30:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="110",
            home_score="101",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        tonight = _event(
            "tonight-pacers",
            date="2026-10-22T03:30:00Z",
            away="Dallas Mavericks",
            home="Indiana Pacers",
            state="pre",
            detail="Scheduled",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([old_final])
            if date_key == "20261021":
                return _Response([tonight])
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("did the pacers win?", now_utc=NOW)

        self.assertIn("Yes — Indiana Pacers beat Chicago Bulls 110-101", reply)
        self.assertNotIn("Dallas Mavericks", reply)

    def test_current_result_before_game_prefers_tonights_pregame_over_old_final(self):
        old_final = _event(
            "old-final-pacers",
            date="2026-10-21T00:30:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="110",
            home_score="101",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        tonight = _event(
            "tonight-pacers",
            date="2026-10-22T03:30:00Z",
            away="Dallas Mavericks",
            home="Indiana Pacers",
            state="pre",
            detail="Scheduled",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([old_final])
            if date_key == "20261021":
                return _Response([tonight])
            return _Response([])

        for query in ("did the Pacers win tonight?", "did the Pacers win today?"):
            with self.subTest(query=query), \
                 patch.object(tools.requests, "get", side_effect=scoreboard):
                self.assertEqual("current_result", tools._live_sports_intent(query))
                reply = tools._get_live_sports_score(query, now_utc=NOW)

            self.assertIn("Not started — NBA", reply)
            self.assertIn("Dallas Mavericks @ Indiana Pacers", reply)
            self.assertNotIn("Chicago Bulls", reply)

    def test_current_result_completed_game_keeps_yes_no_result_format(self):
        old_final = _event(
            "old-final-pacers",
            date="2026-10-21T00:30:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="110",
            home_score="101",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        current_final = _event(
            "current-final-pacers",
            date="2026-10-22T00:30:00Z",
            away="New Orleans Pelicans",
            home="Indiana Pacers",
            away_score="120",
            home_score="123",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=False,
            home_winner=True,
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([old_final])
            if date_key == "20261021":
                return _Response([current_final])
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score(
                "did the Pacers win tonight?",
                now_utc=NOW,
            )

        self.assertIn(
            "Yes — Indiana Pacers beat New Orleans Pelicans 123-120",
            reply,
        )
        self.assertNotIn("Chicago Bulls", reply)

    def test_current_state_question_prefers_recent_final_over_distant_upcoming(self):
        recent_final = _event(
            "recent-final-pacers",
            date="2026-10-22T01:30:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="110",
            home_score="101",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        distant_upcoming = _event(
            "distant-upcoming-pacers",
            date="2026-10-24T02:00:00Z",
            away="Dallas Mavericks",
            home="Indiana Pacers",
            state="pre",
            detail="Scheduled",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261021":
                return _Response([recent_final])
            if date_key == "20261023":
                return _Response([distant_upcoming])
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score(
                "is the pacers game over?",
                now_utc=NOW,
            )

        self.assertIn("Final — NBA", reply)
        self.assertIn("Indiana Pacers 110, Chicago Bulls 101", reply)
        self.assertNotIn("Dallas Mavericks", reply)

    def test_shared_team_nickname_requires_a_league_before_fetching(self):
        with patch.object(tools.requests, "get") as lookup:
            reply = tools._get_live_sports_score("did the kings win?", now_utc=NOW)

        lookup.assert_not_called()
        self.assertIn("ambiguous", reply.lower())
        self.assertIn("NBA", reply)
        self.assertIn("NHL", reply)
        self.assertIn("Name the league", reply)

    def test_contextless_tool_call_asks_for_team_before_fetching(self):
        with patch.object(tools.requests, "get") as lookup:
            reply = tools._get_live_sports_score("tap into the live api and lmk the score", now_utc=NOW)

        lookup.assert_not_called()
        self.assertIn("Name the team and league", reply)

    def test_explicit_league_disambiguates_shared_team_nickname(self):
        rows = tools._live_sports_league_rows("what's the NBA Kings score?", fallback=False)
        self.assertEqual(["NBA"], [row[0] for row in rows])

    def test_college_team_plus_sport_infers_the_college_league(self):
        cases = (
            ("UNC basketball score", "NCAAM"),
            ("Alabama football score", "CFB"),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                rows = tools._live_sports_league_rows(query, fallback=False)
                self.assertEqual([expected], [row[0] for row in rows])

    def test_partial_college_name_does_not_select_a_different_team(self):
        south_alabama = _event(
            "south-alabama-final",
            date="2026-10-22T00:00:00Z",
            away="South Alabama Jaguars",
            home="Troy Trojans",
            away_score="24",
            home_score="17",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        south_team = south_alabama["competitions"][0]["competitors"][0]["team"]
        south_team.update({
            "shortDisplayName": "South Alabama",
            "location": "South Alabama",
            "name": "Jaguars",
            "abbreviation": "USA",
        })

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response(
                [south_alabama] if (params or {}).get("dates") == "20261021" else []
            )

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Alabama football score", now_utc=NOW)

        self.assertIn("couldn't find", reply)
        self.assertNotIn("South Alabama", reply)
        self.assertNotIn("Final — CFB", reply)

    def test_exact_college_name_and_abbreviation_still_match(self):
        alabama = _event(
            "alabama-final",
            date="2026-10-22T00:00:00Z",
            away="Alabama Crimson Tide",
            home="Auburn Tigers",
            away_score="31",
            home_score="24",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        alabama_team = alabama["competitions"][0]["competitors"][0]["team"]
        alabama_team.update({
            "shortDisplayName": "Alabama",
            "location": "Alabama",
            "name": "Crimson Tide",
            "abbreviation": "ALA",
        })

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response([alabama] if (params or {}).get("dates") == "20261021" else [])

        for query in ("Alabama football score", "CFB ALA score"):
            with self.subTest(query=query), \
                 patch.object(tools.requests, "get", side_effect=scoreboard):
                reply = tools._get_live_sports_score(query, now_utc=NOW)

            self.assertIn("Final — CFB", reply)
            self.assertIn("Alabama Crimson Tide 31", reply)

    def test_generic_sport_does_not_misclassify_unknown_colleges_as_pro(self):
        for query in (
            "Duke basketball score",
            "UConn basketball score",
            "Boise State football score",
        ):
            with self.subTest(query=query), patch.object(tools.requests, "get") as lookup:
                self.assertEqual([], tools._live_sports_league_rows(query, fallback=False))
                self.assertIsNone(tools.live_sports_native_reply(query, now_utc=NOW))
                lookup.assert_not_called()

    def test_known_pro_aliases_and_explicit_acronyms_still_classify(self):
        cases = (
            ("Pacers basketball score", "NBA"),
            ("Seahawks football score", "NFL"),
            ("NBA score", "NBA"),
            ("NFL score", "NFL"),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                rows = tools._live_sports_league_rows(query, fallback=False)
                self.assertEqual([expected], [row[0] for row in rows])

    def test_bare_tar_heels_fails_closed_with_sport_clarification(self):
        with patch.object(tools.requests, "get") as lookup:
            reply = tools._get_live_sports_score("did the Tar Heels win?", now_utc=NOW)

        lookup.assert_not_called()
        self.assertIn("Which Tar Heels sport", reply)
        self.assertIn("basketball, football, or baseball", reply)
        self.assertNotIn("ambiguous across", reply)

    def test_multiple_city_matches_fail_closed_instead_of_picking_nearest(self):
        lakers = _event(
            "lakers",
            date="2026-10-22T00:00:00Z",
            away="Los Angeles Lakers",
            home="Phoenix Suns",
            away_score="100",
            home_score="99",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
        )
        clippers = _event(
            "clippers",
            date="2026-10-22T01:00:00Z",
            away="Los Angeles Clippers",
            home="Portland Trail Blazers",
            away_score="101",
            home_score="98",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response([lakers, clippers] if (params or {}).get("dates") == "20261021" else [])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("NBA Los Angeles score", now_utc=NOW)

        self.assertIn("multiple teams", reply.lower())
        self.assertIn("Los Angeles Lakers", reply)
        self.assertIn("Los Angeles Clippers", reply)

    def test_postponed_game_keeps_espn_status_instead_of_claiming_not_started(self):
        postponed = _event(
            "postponed-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            state="pre",
            name="STATUS_POSTPONED",
            detail="Postponed",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response([postponed] if (params or {}).get("dates") == "20261021" else [])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("pacers score", now_utc=NOW)

        self.assertIn("Postponed — NBA", reply)
        self.assertNotIn("Not started", reply)
        self.assertNotIn("Wed Oct 21", reply)

    def test_final_competition_status_overrides_stale_event_pregame_status(self):
        contradictory = _event(
            "contradictory-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="pre",
            name="STATUS_SCHEDULED",
            detail="Scheduled",
            away_winner=True,
            home_winner=False,
        )
        contradictory["competitions"][0]["status"] = {
            "type": {
                "state": "post",
                "name": "STATUS_FINAL",
                "completed": True,
                "shortDetail": "Final",
            }
        }

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response([contradictory] if (params or {}).get("dates") == "20261021" else [])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("did the Pacers win?", now_utc=NOW)

        self.assertIn("Yes — Indiana Pacers beat New Orleans Pelicans 123-120", reply)
        self.assertIn("Final", reply)
        self.assertNotIn("Not started", reply)

    def test_duplicate_event_advances_from_pregame_to_final(self):
        stale_pregame = _event(
            "same-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
        )
        final = _event(
            "same-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([stale_pregame], headers={"Age": "100"})
            if date_key == "20261021":
                return _Response([final], headers={"Age": "0"})
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("did the Pacers win?", now_utc=NOW)

        self.assertIn("Yes — Indiana Pacers beat New Orleans Pelicans 123-120", reply)
        self.assertNotIn("Not started", reply)

    def test_phase_changing_duplicates_without_freshness_fail_closed(self):
        pregame = _event(
            "unverified-phase-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
        )
        final = _event(
            "unverified-phase-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([pregame])
            if date_key == "20261021":
                return _Response([final])
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("did the Pacers win?", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Final", reply)

    def test_phase_change_requires_strictly_newer_response(self):
        live = _event(
            "equal-freshness-phase-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )
        final = _event(
            "equal-freshness-phase-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([live], headers={"Age": "0"})
            if date_key == "20261021":
                return _Response([final], headers={"Age": "0"})
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Final", reply)

    def test_duplicate_live_event_uses_later_same_phase_snapshot(self):
        stale_second_quarter = _event(
            "same-live-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="48",
            home_score="48",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="4:39 - 2nd Quarter",
        )
        fresh_fourth_quarter = _event(
            "same-live-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([fresh_fourth_quarter], headers={"Age": "0"})
            if date_key == "20261021":
                return _Response([stale_second_quarter], headers={"Age": "100"})
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("Indiana Pacers 100", reply)
        self.assertIn("New Orleans Pelicans 98", reply)
        self.assertIn("4th Quarter", reply)
        self.assertNotIn("48", reply)

    def test_conflicting_same_phase_duplicates_without_freshness_fail_closed(self):
        second_quarter = _event(
            "ambiguous-live-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="48",
            home_score="48",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="4:39 - 2nd Quarter",
        )
        fourth_quarter = _event(
            "ambiguous-live-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([second_quarter])
            if date_key == "20261021":
                return _Response([fourth_quarter])
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Indiana Pacers 48", reply)
        self.assertNotIn("Indiana Pacers 100", reply)

    def test_stale_final_cannot_outrank_fresher_live_snapshot(self):
        stale_final = _event(
            "conflicting-phase-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        fresh_live = _event(
            "conflicting-phase-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([stale_final], headers={"Age": "100"})
            if date_key == "20261021":
                return _Response([fresh_live], headers={"Age": "0"})
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Final", reply)
        self.assertNotIn("Live", reply)

    def test_cross_phase_snapshots_with_non_comparable_headers_fail_closed(self):
        stale_final = _event(
            "partial-freshness-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        fresh_live = _event(
            "partial-freshness-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )
        now_header = NOW.strftime("%a, %d %b %Y %H:%M:%S GMT")

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([stale_final], headers={"Age": "100"})
            if date_key == "20261021":
                return _Response([fresh_live], headers={"Date": now_header})
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Final", reply)

    def test_three_snapshot_conflict_fails_closed_in_every_request_order(self):
        live = _event(
            "three-snapshot-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )
        stale_final = _event(
            "three-snapshot-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        snapshots = (
            (live, {"Age": "0"}),
            (live, {"Date": NOW.strftime("%a, %d %b %Y %H:%M:%S GMT")}),
            (stale_final, {"Age": "100"}),
        )
        date_keys = ("20261019", "20261020", "20261021")

        for ordered_snapshots in permutations(snapshots):
            responses = dict(zip(date_keys, ordered_snapshots))

            def scoreboard(url, params=None, timeout=12):
                del url, timeout
                snapshot = responses.get((params or {}).get("dates"))
                if not snapshot:
                    return _Response([])
                event, headers = snapshot
                return _Response([event], headers=headers)

            with self.subTest(order=ordered_snapshots), \
                 patch.object(tools.requests, "get", side_effect=scoreboard):
                reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

            self.assertIn("ESPN live lookup failed", reply)
            self.assertIn("won't guess", reply)
            self.assertNotIn("Final", reply)

    def test_identity_correction_is_resolved_before_query_matching(self):
        stale_pacers = _event(
            "corrected-team-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="48",
            home_score="48",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="4:39 - 2nd Quarter",
        )
        fresh_celtics = _event(
            "corrected-team-game",
            date="2026-10-22T00:00:00Z",
            away="Boston Celtics",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )
        snapshots = (
            (stale_pacers, {"Age": "100"}),
            (fresh_celtics, {"Age": "0"}),
        )

        for ordered_snapshots in (snapshots, tuple(reversed(snapshots))):
            responses = dict(zip(("20261020", "20261021"), ordered_snapshots))

            def scoreboard(url, params=None, timeout=12):
                del url, timeout
                snapshot = responses.get((params or {}).get("dates"))
                if not snapshot:
                    return _Response([])
                event, headers = snapshot
                return _Response([event], headers=headers)

            with self.subTest(order=ordered_snapshots), \
                 patch.object(tools.requests, "get", side_effect=scoreboard):
                reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

            self.assertIn("couldn't find", reply)
            self.assertIn("don't guess", reply)
            self.assertNotIn("48", reply)

    def test_conflicting_date_and_age_freshness_signals_fail_closed(self):
        stale_second_quarter = _event(
            "conflicting-freshness-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="48",
            home_score="48",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="4:39 - 2nd Quarter",
        )
        fresh_fourth_quarter = _event(
            "conflicting-freshness-pacers-game",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="100",
            home_score="98",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="1:12 - 4th Quarter",
        )
        fresh_date = (NOW - timedelta(seconds=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        stale_date = (NOW - timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response(
                    [fresh_fourth_quarter],
                    headers={"Date": fresh_date, "Age": "20"},
                )
            if date_key == "20261021":
                return _Response(
                    [stale_second_quarter],
                    headers={"Date": stale_date, "Age": "0"},
                )
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Indiana Pacers 48", reply)
        self.assertNotIn("Indiana Pacers 100", reply)

    def test_postponed_status_overrides_competition_scheduled_status(self):
        postponed = _event(
            "mixed-postponed-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            state="pre",
            name="STATUS_POSTPONED",
            detail="Postponed",
        )
        postponed["competitions"][0]["status"] = {
            "type": {
                "state": "pre",
                "name": "STATUS_SCHEDULED",
                "completed": False,
                "shortDetail": "Scheduled",
            }
        }

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response([postponed] if (params or {}).get("dates") == "20261021" else [])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("Postponed — NBA", reply)
        self.assertNotIn("Not started", reply)

    def test_pregame_after_scheduled_tip_fails_closed(self):
        stale_pregame = _event(
            "stale-pregame-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
        )

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            return _Response([stale_pregame] if (params or {}).get("dates") == "20261021" else [])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("still marks that game pregame after its scheduled start", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Not started", reply)

    def test_provider_declared_stale_responses_fail_closed_and_close(self):
        stale_headers = (
            {"Age": str(tools._LIVE_SPORTS_MAX_CACHE_AGE_SECONDS + 1)},
            {"Date": "Wed, 21 Oct 2026 00:00:00 GMT"},
            {"Warning": '110 - "Response is stale"'},
        )
        for headers in stale_headers:
            responses = []

            def scoreboard(url, params=None, timeout=12):
                del url, params, timeout
                response = _Response([], headers=headers)
                responses.append(response)
                return response

            with self.subTest(headers=headers), \
                 patch.object(tools.requests, "get", side_effect=scoreboard):
                reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

            self.assertIn("ESPN live lookup failed", reply)
            self.assertTrue(responses)
            self.assertTrue(all(response.close_calls == 1 for response in responses))

    def test_provider_failure_fails_closed_instead_of_guessing(self):
        with patch.object(tools.requests, "get", side_effect=TimeoutError("synthetic")):
            reply = tools._get_live_sports_score("did the pacers win?", now_utc=NOW)
        self.assertIn("I don't have a verified score", reply)
        self.assertIn("won't guess", reply)

    def test_partial_current_date_failure_never_reports_yesterdays_final(self):
        old_final = _event(
            "old-final-pacers",
            date="2026-10-21T00:30:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="110",
            home_score="101",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=True,
            home_winner=False,
        )
        malformed_today = _Response([{"id": "bad-today", "competitions": [None]}])

        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            date_key = (params or {}).get("dates")
            if date_key == "20261020":
                return _Response([old_final])
            if date_key == "20261021":
                return malformed_today
            return _Response([])

        for query in ("did the Pacers win?", "Pacers score today"):
            with self.subTest(query=query), \
                 patch.object(tools.requests, "get", side_effect=scoreboard):
                reply = tools._get_live_sports_score(query, now_utc=NOW)

            self.assertIn("ESPN live lookup failed", reply)
            self.assertIn("won't guess", reply)
            self.assertNotIn("Chicago Bulls", reply)
            self.assertNotIn("Yes —", reply)

    def test_no_match_with_partial_failure_is_provider_failure(self):
        def scoreboard(url, params=None, timeout=12):
            del url, timeout
            if (params or {}).get("dates") == "20261021":
                raise TimeoutError("synthetic current-date failure")
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard):
            reply = tools._get_live_sports_score("Pacers score today", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("couldn't find", reply)

    def test_malformed_provider_payload_counts_as_lookup_failure(self):
        malformed = _Response([])
        malformed.json = Mock(side_effect=ValueError("synthetic malformed JSON"))
        with patch.object(tools.requests, "get", return_value=malformed):
            reply = tools._get_live_sports_score("did the pacers win?", now_utc=NOW)
        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)

    def test_malformed_nested_event_counts_as_lookup_failure(self):
        malformed = _Response([{"id": "x", "competitions": [None]}])
        with patch.object(tools.requests, "get", return_value=malformed):
            reply = tools.live_sports_native_reply("did the pacers win?", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)

    def test_non_scalar_home_away_counts_as_lookup_failure(self):
        malformed_event = _event(
            "bad-home-away",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
        )
        malformed_event["competitions"][0]["competitors"][0]["homeAway"] = ["bad"]

        with patch.object(tools.requests, "get", return_value=_Response([malformed_event])):
            reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)

    def test_duplicate_home_away_roles_fail_closed(self):
        malformed_event = _event(
            "duplicate-home-role",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
        )
        malformed_event["competitions"][0]["competitors"][0]["homeAway"] = "home"

        with patch.object(tools.requests, "get", return_value=_Response([malformed_event])):
            reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Final — NBA", reply)

    def test_non_string_status_detail_counts_as_lookup_failure(self):
        malformed_event = _event(
            "bad-status-detail",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
        )
        malformed_event["status"]["type"]["shortDetail"] = ["bad"]

        with patch.object(tools.requests, "get", return_value=_Response([malformed_event])):
            reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)

    def test_missing_stable_event_identity_counts_as_lookup_failure(self):
        malformed_event = _event(
            "temporary-id",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
        )
        malformed_event.pop("id")

        with patch.object(tools.requests, "get", return_value=_Response([malformed_event])):
            reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)

    def test_unresolved_status_counts_as_lookup_failure(self):
        unresolved_event = _event(
            "unknown-status",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="100",
            home_score="98",
            state="unknown",
            name="STATUS_UNKNOWN",
            detail="Unknown",
        )

        with patch.object(tools.requests, "get", return_value=_Response([unresolved_event])):
            reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Not started", reply)
        self.assertNotIn("100", reply)

    def test_head_to_head_event_requires_exactly_two_competitors(self):
        malformed_event = _event(
            "one-competitor",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
        )
        malformed_event["competitions"][0]["competitors"] = [
            malformed_event["competitions"][0]["competitors"][0]
        ]

        with patch.object(tools.requests, "get", return_value=_Response([malformed_event])):
            reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Final —", reply)

    def test_populated_scores_must_be_bounded_whole_numeric_values(self):
        for bad_score in (True, float("nan"), float("inf"), "not-a-score", 1.5, 1001):
            with self.subTest(bad_score=bad_score):
                malformed_event = _event(
                    "bad-score",
                    date="2026-10-22T00:00:00Z",
                    away="Indiana Pacers",
                    home="Chicago Bulls",
                    away_score=bad_score,
                )
                with patch.object(
                    tools.requests,
                    "get",
                    return_value=_Response([malformed_event]),
                ):
                    reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

                self.assertIn("ESPN live lookup failed", reply)
                self.assertIn("won't guess", reply)
                self.assertNotIn(str(bad_score), reply)

    def test_tied_nba_final_fails_closed(self):
        tied_final = _event(
            "tied-final-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="120",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
        )
        with patch.object(tools.requests, "get", return_value=_Response([tied_final])):
            reply = tools.live_sports_native_reply("did the Pacers win?", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("Final —", reply)

    def test_final_winner_flag_cannot_contradict_scores(self):
        contradictory = _event(
            "contradictory-winner-pacers",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="New Orleans Pelicans",
            away_score="123",
            home_score="120",
            state="post",
            name="STATUS_FINAL",
            detail="Final",
            away_winner=False,
            home_winner=True,
        )
        with patch.object(tools.requests, "get", return_value=_Response([contradictory])):
            reply = tools.live_sports_native_reply("did the Pacers win?", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)
        self.assertNotIn("No —", reply)

    def test_live_and_final_events_require_both_scores(self):
        cases = (
            ("in", "STATUS_IN_PROGRESS", "4:39 - 3rd Quarter"),
            ("post", "STATUS_FINAL", "Final"),
        )
        for state, name, detail in cases:
            with self.subTest(state=state):
                malformed_event = _event(
                    f"empty-{state}-scores",
                    date="2026-10-22T00:00:00Z",
                    away="Indiana Pacers",
                    home="Chicago Bulls",
                    state=state,
                    name=name,
                    detail=detail,
                )
                with patch.object(
                    tools.requests,
                    "get",
                    return_value=_Response([malformed_event]),
                ):
                    reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

                self.assertIn("ESPN live lookup failed", reply)
                self.assertIn("won't guess", reply)
                self.assertNotIn("Indiana Pacers ?", reply)

    def test_negative_scores_fail_closed_in_every_phase(self):
        cases = (
            (-1, "pre", "STATUS_SCHEDULED", "Scheduled", ""),
            ("-1", "in", "STATUS_IN_PROGRESS", "4:39 - 3rd Quarter", "0"),
            (-0.5, "post", "STATUS_FINAL", "Final", "1"),
        )
        for bad_score, state, name, detail, other_score in cases:
            with self.subTest(bad_score=bad_score, state=state):
                malformed_event = _event(
                    f"negative-{state}-score",
                    date="2026-10-22T00:00:00Z",
                    away="Indiana Pacers",
                    home="Chicago Bulls",
                    away_score=bad_score,
                    home_score=other_score,
                    state=state,
                    name=name,
                    detail=detail,
                )
                with patch.object(
                    tools.requests,
                    "get",
                    return_value=_Response([malformed_event]),
                ):
                    reply = tools.live_sports_native_reply("Pacers score", now_utc=NOW)

                self.assertIn("ESPN live lookup failed", reply)
                self.assertIn("won't guess", reply)
                self.assertNotIn(str(bad_score), reply)

    def test_provider_processing_guard_fails_closed_if_validation_is_bypassed(self):
        malformed_event = _event(
            "guarded-home-away",
            date="2026-10-22T00:00:00Z",
            away="Indiana Pacers",
            home="Chicago Bulls",
            away_score="100",
            home_score="99",
            state="in",
            name="STATUS_IN_PROGRESS",
            detail="4:39 - 3rd Quarter",
        )
        malformed_event["competitions"][0]["competitors"][0]["homeAway"] = ["bad"]
        fetched = ([("NBA", malformed_event, 40, [0])], 5, 5)

        with patch.object(tools, "_fetch_live_sports_events", return_value=fetched):
            reply = tools._get_live_sports_score("Pacers score", now_utc=NOW)

        self.assertIn("ESPN live lookup failed", reply)
        self.assertIn("won't guess", reply)

    def test_date_window_requests_run_concurrently_with_bounded_timeouts(self):
        barrier = threading.Barrier(5)
        timeouts = []

        def scoreboard(url, params=None, timeout=12):
            del url, params
            timeouts.append(timeout)
            barrier.wait(timeout=2)
            return _Response([])

        with patch.object(tools.requests, "get", side_effect=scoreboard) as lookup:
            reply = tools._get_live_sports_score("pacers score", now_utc=NOW)

        self.assertEqual(5, lookup.call_count)
        self.assertEqual([tools._LIVE_SPORTS_HTTP_TIMEOUT_SECONDS] * 5, sorted(timeouts))
        self.assertIn("couldn't find", reply)

    def test_next_nfl_game_scans_two_weeks_with_bounded_requests_and_workers(self):
        monday = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)
        after_bye = _event(
            "seahawks-after-bye",
            date="2026-11-02T01:20:00Z",
            away="Seattle Seahawks",
            home="Los Angeles Rams",
            state="pre",
            detail="Scheduled",
        )
        date_keys = []
        timeouts = []
        worker_counts = []
        lock = threading.Lock()

        def scoreboard(url, params=None, timeout=12):
            del url
            date_key = (params or {}).get("dates")
            with lock:
                date_keys.append(date_key)
                timeouts.append(timeout)
            return _Response([after_bye] if date_key == "20261101" else [])

        def executor_factory(*args, **kwargs):
            max_workers = kwargs.get("max_workers", args[0] if args else None)
            worker_counts.append(max_workers)
            return RealThreadPoolExecutor(*args, **kwargs)

        with patch.object(tools.requests, "get", side_effect=scoreboard) as lookup, \
             patch.object(tools, "ThreadPoolExecutor", side_effect=executor_factory):
            reply = tools._get_live_sports_score(
                "when do the Seahawks play next?",
                now_utc=monday,
            )

        self.assertEqual(tools._LIVE_SPORTS_MAX_DATE_REQUESTS, lookup.call_count)
        self.assertEqual(tools._LIVE_SPORTS_MAX_DATE_REQUESTS, len(set(date_keys)))
        self.assertIn("20261101", date_keys)
        self.assertEqual([tools._LIVE_SPORTS_MAX_CONCURRENT_REQUESTS], worker_counts)
        self.assertEqual(
            [tools._LIVE_SPORTS_HTTP_TIMEOUT_SECONDS] * tools._LIVE_SPORTS_MAX_DATE_REQUESTS,
            sorted(timeouts),
        )
        self.assertIn("Not started — NFL", reply)
        self.assertIn("Seattle Seahawks @ Los Angeles Rams", reply)

class LiveSportsRoutingTests(unittest.TestCase):
    setUp = route_fixtures.NoWebRouteTests.setUp
    route = route_fixtures.NoWebRouteTests.route

    def test_incident_wording_bypasses_model_and_stale_history(self):
        verified = "Yes — Indiana Pacers beat New Orleans Pelicans 123-120. Final. ESPN checked 7:53 PM PDT."
        stale_history = [
            {"role": "assistant", "content": "They're tied at 48 with 4:39 left in the 2nd quarter."},
        ]
        with patch.object(main, "get_history", return_value=stale_history), \
             patch.object(main, "live_sports_native_reply", return_value=verified) as lookup:
            self.route("did the pacers win?", group=True)

        lookup.assert_called_once_with("did the pacers win?")
        self.model.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(route_fixtures.GROUP, self.send.call_args.args[0])
        self.assertEqual(verified, self.send.call_args.args[1])
        self.assertTrue(self.send.call_args.kwargs["is_group"])

    def test_today_score_wording_bypasses_model_and_stale_history(self):
        query = "what was the pacers score today"
        verified = "Yes — Indiana Pacers beat New Orleans Pelicans 123-120. Final. ESPN checked 8:44 PM PDT."
        stale_history = [
            {"role": "assistant", "content": "The game hasn't started yet. It's at 5:00 PM PT."},
        ]
        with patch.object(main, "get_history", return_value=stale_history), \
             patch.object(main, "live_sports_native_reply", return_value=verified) as lookup:
            self.route(query, group=True)

        lookup.assert_called_once_with(query)
        self.model.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(verified, self.send.call_args.args[1])

    def test_contextless_live_api_followup_never_reuses_stale_history(self):
        query = "tap into live api and lmk the score"
        prompt = "Which team and league should I check on ESPN? Name them so I don't guess."
        stale_history = [
            {"role": "assistant", "content": "They're tied at 48 in the second quarter."},
        ]
        with patch.object(main, "get_history", return_value=stale_history), \
             patch.object(main, "live_sports_native_reply", return_value=prompt) as lookup:
            self.route(query, group=True)

        lookup.assert_called_once_with(query)
        self.model.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(prompt, self.send.call_args.args[1])

    def test_named_unsupported_api_request_continues_to_broader_lookup(self):
        for query in (
            "tap into live api and lmk Barcelona score",
            "tap into ESPN api and lmk Barcelona result",
            "check ESPN API and tell me the Inter Miami result",
            "Barcelona result, check the ESPN API",
            "check Barcelona result in the ESPN API",
            "check the Barcelona result using ESPN API",
            "ESPN API: check the Barcelona result",
            "get the Barcelona result from ESPN API",
            "show me the Barcelona result from ESPN API",
            "tell me the Barcelona result from ESPN API",
            "fetch the Barcelona result from ESPN API",
            "pull the Barcelona result from ESPN API",
            "Barcelona result via ESPN API, look it up",
        ):
            with self.subTest(query=query):
                self.route(query, group=True)

                self.model.assert_called_once()
                self.assertTrue(self.model.call_args.kwargs["use_tools"])
                self.send.assert_called_once()

    def test_native_score_route_covers_owner_and_admin_but_not_friend_dm(self):
        verified = "Final — NBA: Indiana Pacers 123, New Orleans Pelicans 120."
        with patch.object(main, "live_sports_native_reply", return_value=verified) as lookup:
            for sender, group, native in (
                (route_fixtures.OWNER, False, True),
                (route_fixtures.ADMIN, False, True),
                (route_fixtures.FRIEND, False, False),
                (route_fixtures.FRIEND, True, True),
            ):
                with self.subTest(sender=sender, group=group):
                    lookup.reset_mock()
                    self.route("what's the pacers score?", sender=sender, group=group)
                    self.assertEqual(native, bool(lookup.called))
                    self.assertEqual(native, not bool(self.model.called))

    def test_no_search_returns_deterministic_unverified_sports_reply(self):
        with patch.object(main, "live_sports_native_reply") as lookup:
            for query in (
                "no search did the pacers win?",
                "no search did the pacers win Monday?",
                "no search check ESPN API and tell me the Inter Miami result",
                "no search Barcelona result, check the ESPN API",
                "no search ESPN API: check the Barcelona result",
            ):
                with self.subTest(query=query):
                    lookup.reset_mock()
                    self.route(query, group=True)

                    lookup.assert_not_called()
                    self.model.assert_not_called()
                    self.send.assert_called_once()
                    self.assertIn("You asked me not to search", self.send.call_args.args[1])
                    self.assertIn("have not verified it", self.send.call_args.args[1])

    def test_non_sports_finish_questions_do_not_trigger_live_info_tools(self):
        for text in (
            "did the upload finish?",
            "has the backup ended?",
            "did you finish the report?",
            "what was the result of 2 + 2?",
            "did the upload result look right?",
            "show me the result from that file",
            "the lab result was negative",
                "summarize the test results I pasted",
                "check the API result",
                "check the live API result",
                "could you check the live API result",
                "check the live API result for the deployment",
                "could you check the live API result for order 123",
                "query the live API results for customer account",
            ):
            with self.subTest(text=text):
                self.assertFalse(tools.is_explicit_live_sports_api_request(text))
                self.assertFalse(main._should_use_limited_web_tools(text, False))
                self.assertFalse(main._should_use_owner_tools(text, False))

        # Explicit "look up" still receives the existing generic web route,
        # but it must never be classified as a sports API request.
        self.assertFalse(tools.is_explicit_live_sports_api_request(
            "look up the live API result from endpoint alpha"
        ))
        self.assertTrue(main._should_use_limited_web_tools("has the game ended?", False))

    def test_generic_live_api_results_remain_tool_free(self):
        for query in (
            "check the live API result for the deployment",
            "query the live API results for customer account",
            "no search check the live API result for authentication",
        ):
            with self.subTest(query=query):
                self.route(query, group=True)

                self.model.assert_called_once()
                self.assertFalse(self.model.call_args.kwargs["use_tools"])
                self.send.assert_called_once()

    def test_public_quota_blocks_native_score_lookup_without_guessing(self):
        with patch.object(main, "get_tool_uses_today", return_value=main._FRIEND_SEARCH_LIMIT), \
             patch.object(main, "live_sports_native_reply") as lookup:
            self.route("did the pacers win?", sender=route_fixtures.ADMIN, group=True)

        lookup.assert_not_called()
        self.model.assert_not_called()
        self.assertIn("daily limit reached", self.send.call_args.args[1].lower())


if __name__ == "__main__":
    unittest.main()
