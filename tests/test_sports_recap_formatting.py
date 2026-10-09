import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from davosbot import tools
class _FakeResponse:
    def __init__(self, events):
        self._events = events

    def raise_for_status(self):
        return None

    def json(self):
        return {"events": self._events}


def _event(
    name,
    away,
    home,
    status,
    *,
    league_slug="regular-season",
    away_score="",
    home_score="",
    away_abbrev="AWAY",
    home_abbrev="HOME",
    notes=None,
    away_rank=None,
    home_rank=None,
):
    away_team = {
        "shortDisplayName": away,
        "displayName": away,
        "abbreviation": away_abbrev,
    }
    home_team = {
        "shortDisplayName": home,
        "displayName": home,
        "abbreviation": home_abbrev,
    }
    if away_rank:
        away_team["curatedRank"] = {"current": away_rank}
    if home_rank:
        home_team["curatedRank"] = {"current": home_rank}
    return {
        "name": name,
        "shortName": name,
        "season": {"slug": league_slug},
        "notes": [{"headline": note} for note in (notes or [])],
        "status": {"type": {"shortDetail": status}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "away",
                        "score": away_score,
                        "team": away_team,
                    },
                    {
                        "homeAway": "home",
                        "score": home_score,
                        "team": home_team,
                    },
                ]
            }
        ],
    }


class SportsRecapFormattingTests(unittest.TestCase):
    def test_playoffs_rank_before_seattle_and_bias_footer_is_removed(self):
        def fake_get(url, params=None, timeout=12):
            if "/basketball/nba/" in url:
                return _FakeResponse([
                    _event(
                        "Pacers vs Knicks",
                        "Pacers",
                        "Knicks",
                        "Final",
                        league_slug="postseason",
                        away_score="118",
                        home_score="112",
                    )
                ])
            if "/baseball/mlb/" in url:
                return _FakeResponse([
                    _event(
                        "Mariners vs Astros",
                        "Mariners",
                        "Astros",
                        "7:10 PM",
                        away_score="",
                        home_score="",
                    )
                ])
            if "/hockey/nhl/" in url:
                return _FakeResponse([
                    _event(
                        "Canadiens vs Hurricanes",
                        "Canadiens",
                        "Hurricanes",
                        "8:00 PM",
                        league_slug="post-season",
                    )
                ])
            if "/baseball/college-baseball/" in url:
                return _FakeResponse([
                    _event(
                        "North Carolina Tar Heels at Duke Blue Devils",
                        "North Carolina Tar Heels",
                        "Duke Blue Devils",
                        "Final",
                        away_score="5",
                        home_score="4",
                        away_abbrev="UNC",
                    )
                ])
            if "/basketball/mens-college-basketball/" in url:
                return _FakeResponse([
                    _event(
                        "Kansas Jayhawks at Houston Cougars",
                        "Kansas Jayhawks",
                        "Houston Cougars",
                        "9:00 PM",
                        away_rank=8,
                        home_rank=3,
                    )
                ])
            return _FakeResponse([])

        now = datetime(2026, 5, 20, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        with patch.object(tools.requests, "get", side_effect=fake_get):
            recap = tools._get_sports_recap(now_pt=now)

        lines = recap.splitlines()
        nba_idx = next(i for i, line in enumerate(lines) if "NBA:" in line)
        nhl_idx = next(i for i, line in enumerate(lines) if "NHL:" in line)
        unc_idx = next(i for i, line in enumerate(lines) if "NCAA Baseball:" in line)
        mlb_idx = next(i for i, line in enumerate(lines) if "MLB:" in line)
        college_idx = next(i for i, line in enumerate(lines) if "NCAAM:" in line)
        self.assertLess(nba_idx, mlb_idx)
        self.assertLess(nhl_idx, mlb_idx)
        self.assertLess(unc_idx, mlb_idx)
        self.assertLess(mlb_idx, college_idx)
        self.assertIn("Finished", recap)
        self.assertIn("Scheduled", recap)
        self.assertLess(lines.index("Finished"), lines.index("Scheduled"))
        self.assertNotIn("[Playoff]", recap)
        self.assertNotIn("[Seattle]", recap)
        self.assertNotIn("Seattle bias", recap)
        self.assertNotIn("Playoff bias", recap)
        self.assertNotIn("copypasta", recap.lower())
        self.assertNotIn("??", recap)
        self.assertNotIn("?", recap)

    def test_looks_ahead_for_nba_playoffs_before_today_seattle_games(self):
        def fake_get(url, params=None, timeout=12):
            date_key = (params or {}).get("dates")
            if "/basketball/nba/" in url and date_key == "20260521":
                return _FakeResponse([
                    _event(
                        "Pacers vs Knicks",
                        "Pacers",
                        "Knicks",
                        "Thu 5:00 PM",
                        league_slug="post-season",
                    )
                ])
            if "/baseball/mlb/" in url and date_key == "20260520":
                return _FakeResponse([
                    _event(
                        "Mariners vs Astros",
                        "Mariners",
                        "Astros",
                        "Wed 7:10 PM",
                    )
                ])
            if "/baseball/college-baseball/" in url and date_key == "20260520":
                return _FakeResponse([
                    _event(
                        "NC State Wolfpack at Clemson Tigers",
                        "NC State Wolfpack",
                        "Clemson Tigers",
                        "Final",
                    )
                ])
            if "/baseball/college-baseball/" in url and date_key == "20260521":
                return _FakeResponse([
                    _event(
                        "UNC Wilmington Seahawks at Campbell Camels",
                        "UNC Wilmington Seahawks",
                        "Campbell Camels",
                        "Final",
                        away_abbrev="UNCW",
                        notes=["Conference Tournament"],
                    )
                ])
            return _FakeResponse([])

        now = datetime(2026, 5, 20, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        with patch.object(tools.requests, "get", side_effect=fake_get):
            recap = tools._get_sports_recap(now_pt=now)

        lines = recap.splitlines()
        nba_idx = next(i for i, line in enumerate(lines) if "NBA:" in line)
        mlb_idx = next(i for i, line in enumerate(lines) if "MLB:" in line)
        self.assertLess(nba_idx, mlb_idx)
        self.assertIn("Pacers", lines[nba_idx])
        self.assertIn("Pacers @ Knicks", lines[nba_idx])
        self.assertIn("Scheduled", recap)
        self.assertNotIn("NC State", recap)
        self.assertNotIn("UNC Wilmington", recap)
        self.assertNotIn("bias", recap.lower())

    def test_mlb_is_today_only_and_ordered_live_finished_scheduled(self):
        def fake_get(url, params=None, timeout=12):
            date_key = (params or {}).get("dates")
            if "/baseball/mlb/" in url and date_key == "20260520":
                return _FakeResponse([
                    _event(
                        "Dodgers at Cubs",
                        "Dodgers",
                        "Cubs",
                        "7:10 PM",
                    ),
                    _event(
                        "Red Sox at Orioles",
                        "Red Sox",
                        "Orioles",
                        "Final",
                        away_score="4",
                        home_score="2",
                    ),
                    _event(
                        "Yankees at Rays",
                        "Yankees",
                        "Rays",
                        "Top 4th",
                        away_score="2",
                        home_score="1",
                    ),
                ])
            if "/baseball/mlb/" in url and date_key == "20260521":
                return _FakeResponse([
                    _event(
                        "Mets at Braves",
                        "Mets",
                        "Braves",
                        "Thu 4:10 PM",
                    )
                ])
            return _FakeResponse([])

        now = datetime(2026, 5, 20, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        with patch.object(tools.requests, "get", side_effect=fake_get):
            recap = tools._get_sports_recap(now_pt=now)

        lines = recap.splitlines()
        live_idx = lines.index("Live")
        finished_idx = lines.index("Finished")
        scheduled_idx = lines.index("Scheduled")
        yankees_idx = next(i for i, line in enumerate(lines) if "Yankees" in line)
        red_sox_idx = next(i for i, line in enumerate(lines) if "Red Sox" in line)
        dodgers_idx = next(i for i, line in enumerate(lines) if "Dodgers" in line)

        self.assertLess(live_idx, finished_idx)
        self.assertLess(finished_idx, scheduled_idx)
        self.assertLess(yankees_idx, red_sox_idx)
        self.assertLess(red_sox_idx, dodgers_idx)
        self.assertNotIn("Mets", recap)

    def test_unc_watch_excludes_scheduled_futures(self):
        def fake_get(url, params=None, timeout=12):
            date_key = (params or {}).get("dates")
            if "/baseball/college-baseball/" in url and date_key == "20260519":
                return _FakeResponse([
                    _event(
                        "North Carolina Tar Heels at NC State Wolfpack",
                        "North Carolina Tar Heels",
                        "NC State Wolfpack",
                        "Final",
                        away_score="6",
                        home_score="3",
                        away_abbrev="UNC",
                    )
                ])
            if "/baseball/college-baseball/" in url and date_key == "20260521":
                return _FakeResponse([
                    _event(
                        "North Carolina Tar Heels at Duke Blue Devils",
                        "North Carolina Tar Heels",
                        "Duke Blue Devils",
                        "Thu 6:00 PM",
                        away_abbrev="UNC",
                    )
                ])
            return _FakeResponse([])

        now = datetime(2026, 5, 20, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        with patch.object(tools.requests, "get", side_effect=fake_get):
            recap = tools._get_sports_recap(now_pt=now)

        self.assertIn("North Carolina Tar Heels 6 @ NC State Wolfpack 3", recap)
        self.assertNotIn("Duke Blue Devils", recap)


if __name__ == "__main__":
    unittest.main()
