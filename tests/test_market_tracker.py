import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from davosbot import market


ET = ZoneInfo("America/New_York")


def _chart_fixture(
    *,
    symbol="NVDA",
    now=None,
    previous_close=100.0,
    regular_price=103.0,
    points=None,
):
    now = now or datetime(2026, 7, 29, 15, 0, tzinfo=ET)
    session_day = now.astimezone(ET).date()
    pre_start = datetime.combine(session_day, datetime.min.time(), ET).replace(hour=4)
    regular_start = pre_start.replace(hour=9, minute=30)
    regular_end = pre_start.replace(hour=16)
    post_end = pre_start.replace(hour=20)
    if points is None:
        latest = int(now.timestamp())
        points = [
            (latest - 3600, 100.0),
            (latest - 600, 103.1),
            (latest - 300, 103.2),
            (latest, 103.3),
        ]
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "currency": "USD",
                        "shortName": "NVIDIA Corporation",
                        "previousClose": previous_close,
                        "chartPreviousClose": previous_close,
                        "regularMarketPrice": regular_price,
                        "regularMarketDayHigh": 104.0,
                        "regularMarketDayLow": 99.5,
                        "regularMarketVolume": 123_000_000,
                        "fiftyTwoWeekHigh": 150.0,
                        "fiftyTwoWeekLow": 75.0,
                        "currentTradingPeriod": {
                            "pre": {
                                "start": int(pre_start.timestamp()),
                                "end": int(regular_start.timestamp()),
                            },
                            "regular": {
                                "start": int(regular_start.timestamp()),
                                "end": int(regular_end.timestamp()),
                            },
                            "post": {
                                "start": int(regular_end.timestamp()),
                                "end": int(post_end.timestamp()),
                            },
                        },
                    },
                    "timestamp": [item[0] for item in points],
                    "indicators": {
                        "quote": [{"close": [item[1] for item in points]}]
                    },
                }
            ],
            "error": None,
        }
    }


def _quote(
    *,
    symbol="NVDA",
    session="regular",
    change=3.2,
    confirmed=3.1,
    rapid=None,
    stale=False,
):
    return market.MarketQuote(
        symbol=symbol,
        name=symbol,
        currency="USD",
        price=103.2,
        regular_price=103.2,
        previous_close=100.0,
        day_change_pct=3.2,
        session=session,
        session_change_pct=change,
        confirmed_session_change_pct=confirmed,
        one_hour_change_pct=rapid,
        day_high=104.0,
        day_low=99.0,
        volume=10_000_000,
        fifty_two_week_high=150.0,
        fifty_two_week_low=75.0,
        timestamp=int(datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc).timestamp()),
        stale=stale,
    )


def _create_bot_log(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE bot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                sender TEXT,
                event_type TEXT,
                payload TEXT
            )
            """
        )
        conn.commit()


class MarketQuoteParsingTests(unittest.TestCase):
    def test_parses_regular_session_quote_and_confirms_two_ticks(self):
        now = datetime(2026, 7, 29, 15, 0, tzinfo=ET)
        quote = market.parse_yahoo_chart("NVDA", _chart_fixture(now=now), now=now)

        self.assertEqual(quote.session, "regular")
        self.assertAlmostEqual(quote.price, 103.3)
        self.assertAlmostEqual(quote.day_change_pct, 3.0)
        self.assertGreater(quote.confirmed_session_change_pct, 3.0)
        self.assertAlmostEqual(quote.one_hour_change_pct, 3.3)
        self.assertFalse(quote.stale)

    def test_parses_after_hours_against_regular_close(self):
        now = datetime(2026, 7, 29, 16, 30, tzinfo=ET)
        latest = int(now.timestamp())
        points = [
            (latest - 3600, 100.0),
            (latest - 600, 104.0),
            (latest - 300, 104.1),
            (latest, 104.2),
        ]
        quote = market.parse_yahoo_chart(
            "NVDA",
            _chart_fixture(
                now=now,
                previous_close=98.0,
                regular_price=100.0,
                points=points,
            ),
            now=now,
        )

        self.assertEqual(quote.session, "after-hours")
        self.assertAlmostEqual(quote.session_change_pct, 4.2)
        self.assertAlmostEqual(quote.day_change_pct, (100 / 98 - 1) * 100)
        self.assertGreater(quote.confirmed_session_change_pct, 4.0)

    def test_labels_stale_active_quote(self):
        now = datetime(2026, 7, 29, 16, 45, tzinfo=ET)
        latest = int((now - timedelta(minutes=30)).timestamp())
        payload = _chart_fixture(
            now=now,
            points=[(latest - 300, 103.0), (latest, 103.1)],
        )
        quote = market.parse_yahoo_chart("NVDA", payload, now=now)

        self.assertTrue(quote.stale)
        detail = market.format_quote_detail(quote)
        self.assertIn("after-hours; stale", detail)
        self.assertIn("As of", detail)

    def test_provider_failure_is_fail_soft_per_symbol(self):
        good = _quote(symbol="AAPL")
        with patch.object(
            market,
            "fetch_quote",
            side_effect=lambda symbol, **_kwargs: good if symbol == "AAPL" else (_ for _ in ()).throw(RuntimeError("offline")),
        ):
            quotes, errors = market.fetch_quotes(["AAPL", "MSFT"])

        self.assertEqual([item.symbol for item in quotes], ["AAPL"])
        self.assertIn("MSFT", errors)


class MarketQueryAndCommandTests(unittest.TestCase):
    def test_natural_nvda_question_routes_to_single_quote(self):
        with patch.object(market, "get_market_data", return_value="NVDA live") as getter:
            reply = market.handle_market_query("how's NVDA?")

        self.assertEqual(reply, "NVDA live")
        getter.assert_called_once_with(view="quote", symbols=["NVDA"])

    def test_after_hours_question_routes_to_movers(self):
        with patch.object(market, "get_market_data", return_value="movers") as getter:
            reply = market.handle_market_query("what moved after hours?")

        self.assertEqual(reply, "movers")
        getter.assert_called_once_with(view="movers")

    def test_complex_market_question_yields_to_existing_web_search_route(self):
        self.assertIsNone(market.handle_market_query("why is NVDA down today?"))

    def test_command_dispatch_covers_summary_quote_status_and_help(self):
        from davosbot import commands

        with patch.object(market, "get_market_data", return_value="summary") as getter:
            self.assertEqual(commands.handle_command("owner", "market"), "summary")
            getter.assert_called_once_with(view="snapshot")

        with patch.object(market, "get_market_data", return_value="quote") as getter:
            self.assertEqual(commands.handle_command("owner", "quote NVDA"), "quote")
            getter.assert_called_once_with(view="quote", symbols=["NVDA"])

        status = commands.handle_command("owner", "market status")
        self.assertIn("Watchlist:", status)
        help_text = commands.handle_command("owner", "market help")
        self.assertIn("quote NVDA", help_text)
        self.assertNotIn("earnings", help_text.lower())

    def test_legacy_earnings_command_explains_price_only_scope(self):
        reply = market.handle_market_command("market earnings", sender_is_owner=True)

        self.assertIn("price-only", reply)
        self.assertNotIn("EPS", reply)

    def test_alert_mutation_is_owner_only(self):
        self.assertEqual(
            market.handle_market_command("market alerts off", sender_is_owner=False),
            "Market alert controls are owner-only.",
        )
        with patch.object(market, "set_market_alerts_enabled", return_value="Market alerts are off.") as setter:
            reply = market.handle_market_command("market alerts off", sender_is_owner=True)
        self.assertEqual(reply, "Market alerts are off.")
        setter.assert_called_once_with(False)

    def test_alert_status_reports_actual_toggle(self):
        with patch.object(market, "market_alerts_enabled", return_value=False):
            reply = market.handle_market_command("market alerts status", sender_is_owner=True)

        self.assertIn("Market tracker: on", reply)
        self.assertIn("Market alerts: off.", reply)

    def test_owner_alert_control_uses_actual_command_dispatch(self):
        from davosbot import commands

        with (
            patch.object(commands, "is_owner", return_value=True),
            patch.object(market, "set_market_alerts_enabled", return_value="Market alerts are off.") as setter,
        ):
            reply = commands.handle_command("owner", "market alerts off")

        self.assertEqual(reply, "Market alerts are off.")
        setter.assert_called_once_with(False)

    def test_fantasy_and_market_dispatch_coexist_after_shared_command_merge(self):
        from davosbot import commands, config

        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=True),
            patch.object(commands, "FANTASY_DASHBOARD_URL", config.DEFAULT_FANTASY_DASHBOARD_URL),
            patch.object(market, "get_market_data", return_value="market snapshot"),
        ):
            fantasy_reply = commands.handle_command("owner", "fantasy")
            market_reply = commands.handle_command("owner", "market")

        self.assertIn("Fourth Down", fantasy_reply)
        self.assertEqual("market snapshot", market_reply)

class MarketAlertTests(unittest.TestCase):
    def test_regular_and_extended_threshold_tiers_escalate(self):
        trading_date = date(2026, 7, 29)
        regular = _quote(change=5.2, confirmed=5.1)
        extended = _quote(
            symbol="MSFT",
            session="after-hours",
            change=-4.3,
            confirmed=-4.1,
        )
        candidates = market.build_price_alerts(
            [regular, extended],
            trading_date=trading_date,
        )
        by_line = {candidate.line: candidate for candidate in candidates}
        regular_candidate = next(candidate for line, candidate in by_line.items() if "Nvidia" in line)
        extended_candidate = next(candidate for line, candidate in by_line.items() if "Microsoft" in line)

        self.assertTrue(any(key.endswith(":5") for key in regular_candidate.keys))
        self.assertFalse(any(key.endswith(":3") for key in regular_candidate.keys))
        self.assertTrue(any(key.endswith(":4") for key in extended_candidate.keys))
        self.assertFalse(any(key.endswith(":2") for key in extended_candidate.keys))
        self.assertIn("after hours", extended_candidate.line)

    def test_routine_noise_stays_below_need_to_know_thresholds(self):
        trading_date = date(2026, 7, 29)
        quotes = [
            _quote(change=4.9, confirmed=4.9, rapid=3.9),
            _quote(
                symbol="MSFT",
                session="after-hours",
                change=-3.9,
                confirmed=-3.9,
                rapid=-3.9,
            ),
            _quote(symbol="^IXIC", change=1.7, confirmed=1.7, rapid=1.2),
        ]

        self.assertEqual(
            market.build_price_alerts(quotes, trading_date=trading_date),
            [],
        )

    def test_index_proxies_alert_only_in_extended_hours_and_are_labeled(self):
        trading_date = date(2026, 7, 29)
        regular_proxy = _quote(
            symbol="QQQ",
            session="regular",
            change=2.0,
            confirmed=2.0,
        )
        after_hours_proxy = _quote(
            symbol="SPY",
            session="after-hours",
            change=-1.4,
            confirmed=-1.3,
        )
        candidates = market.build_price_alerts(
            [regular_proxy, after_hours_proxy],
            trading_date=trading_date,
        )

        self.assertFalse(any("QQQ" in candidate.line for candidate in candidates))
        self.assertTrue(any("S&P 500 proxy (SPY)" in candidate.line for candidate in candidates))

    def test_stale_quote_never_alerts(self):
        candidates = market.build_price_alerts(
            [_quote(change=12.0, confirmed=12.0, stale=True)],
            trading_date=date(2026, 7, 29),
        )
        self.assertEqual(candidates, [])

    def test_cooldown_suppresses_new_same_tier_but_allows_escalation(self):
        now = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        sent = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _create_bot_log(db_path)
            state = market.TrackerState(last_alert_at=now - timedelta(minutes=5))
            market.run_market_alert_cycle(
                sent,
                now=now,
                include_quotes=True,
                state=state,
                quote_fetcher=lambda *_args, **_kwargs: ([_quote(change=5.2, confirmed=5.1)], {}),
                db_path=db_path,
            )
            sent.assert_not_called()
            self.assertIn("2026-07-29:NVDA:day:up:5", state.known_keys)

            market.run_market_alert_cycle(
                sent,
                now=now,
                include_quotes=True,
                state=state,
                quote_fetcher=lambda *_args, **_kwargs: ([_quote(change=8.2, confirmed=8.1)], {}),
                db_path=db_path,
            )
            sent.assert_called_once()

    def test_suppressed_routine_move_is_persisted_without_resetting_last_alert(self):
        now = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _create_bot_log(db_path)
            state = market.TrackerState(last_alert_at=now - timedelta(minutes=5))
            market.run_market_alert_cycle(
                Mock(return_value=True),
                now=now,
                state=state,
                quote_fetcher=lambda *_args, **_kwargs: ([_quote(change=5.2, confirmed=5.1)], {}),
                db_path=db_path,
            )

            known_keys, last_alert_at = market._load_recent_alert_state(db_path)
            with closing(sqlite3.connect(db_path)) as conn:
                event_type = conn.execute(
                    "SELECT event_type FROM bot_log ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]

        self.assertIn("2026-07-29:NVDA:day:up:5", known_keys)
        self.assertIsNone(last_alert_at)
        self.assertEqual("market_alert_seen", event_type)

    def test_opposite_extended_moves_are_batched_into_one_need_to_know_alert(self):
        now = datetime(2026, 7, 29, 21, 0, tzinfo=timezone.utc)
        sent = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _create_bot_log(db_path)
            market.run_market_alert_cycle(
                sent,
                now=now,
                quote_fetcher=lambda *_args, **_kwargs: (
                    [
                        _quote(
                            symbol="MSFT",
                            session="after-hours",
                            change=5.4,
                            confirmed=5.2,
                        ),
                        _quote(
                            symbol="META",
                            session="after-hours",
                            change=-5.7,
                            confirmed=-5.5,
                        ),
                    ],
                    {},
                ),
                db_path=db_path,
            )

        sent.assert_called_once()
        message = sent.call_args.args[0]
        self.assertIn("Need-to-know market moves", message)
        self.assertIn("Microsoft", message)
        self.assertIn("Meta", message)

    def test_sent_alert_keys_persist_and_dedupe_after_restart(self):
        now = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _create_bot_log(db_path)
            first_send = Mock(return_value=True)
            market.run_market_alert_cycle(
                first_send,
                now=now,
                include_quotes=True,
                quote_fetcher=lambda *_args, **_kwargs: ([_quote(change=5.2, confirmed=5.1)], {}),
                db_path=db_path,
            )
            first_send.assert_called_once()

            second_send = Mock(return_value=True)
            market.run_market_alert_cycle(
                second_send,
                now=now + timedelta(minutes=20),
                include_quotes=True,
                quote_fetcher=lambda *_args, **_kwargs: ([_quote(change=5.2, confirmed=5.1)], {}),
                db_path=db_path,
            )
            second_send.assert_not_called()

    def test_only_extreme_moves_break_cooldown_without_prior_symbol_alert(self):
        routine = market.AlertCandidate(
            keys=("2026-07-29:NVDA:day:up:5",),
            line="routine",
            priority=5.2,
        )
        critical = market.AlertCandidate(
            keys=(
                "2026-07-29:NVDA:day:up:5",
                "2026-07-29:NVDA:day:up:8",
            ),
            line="critical",
            priority=8.2,
        )

        self.assertFalse(market._candidate_is_critical(routine))
        self.assertTrue(market._candidate_is_critical(critical))

    def test_alert_setting_persists_without_new_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _create_bot_log(db_path)
            with (
                patch.object(market, "MARKET_TRACKER_ENABLED", True),
                patch.object(market, "MARKET_ALERTS_ENABLED", True),
            ):
                market._ALERT_SETTING_CACHE = None
                reply = market.set_market_alerts_enabled(False, db_path=db_path)
                self.assertIn("off", reply)
                self.assertFalse(market.market_alerts_enabled(db_path=db_path, force=True))

    def test_market_hours_include_extended_hours_and_exclude_weekends(self):
        self.assertFalse(market._market_session_active(datetime(2026, 7, 29, 3, 59, tzinfo=ET)))
        self.assertTrue(market._market_session_active(datetime(2026, 7, 29, 4, 0, tzinfo=ET)))
        self.assertTrue(market._market_session_active(datetime(2026, 7, 29, 19, 59, tzinfo=ET)))
        self.assertFalse(market._market_session_active(datetime(2026, 7, 29, 20, 0, tzinfo=ET)))
        self.assertFalse(market._market_session_active(datetime(2026, 8, 1, 12, 0, tzinfo=ET)))

    def test_tracker_starts_as_daemon_thread(self):
        fake_thread = Mock()
        with (
            patch.object(market, "_TRACKER_STARTED", False),
            patch.object(market, "MARKET_TRACKER_ENABLED", True),
            patch.object(market.threading, "Thread", return_value=fake_thread) as thread_cls,
        ):
            result = market.start_market_tracker(lambda _message: True)

        self.assertIs(result, fake_thread)
        self.assertTrue(thread_cls.call_args.kwargs["daemon"])
        fake_thread.start.assert_called_once()
if __name__ == "__main__":
    unittest.main()
