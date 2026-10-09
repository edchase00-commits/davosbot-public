"""Keyless market quotes and low-noise owner price alerts.

Yahoo Finance's chart feed supplies current and extended-hours price data.
The feed is treated as best-effort and may be delayed. This module never places
trades or makes buy/sell recommendations.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

import requests

from .config import (
    BOT_DB_PATH,
    MARKET_ALERT_COOLDOWN_MINUTES,
    MARKET_ALERTS_ENABLED,
    MARKET_DATA_TIMEOUT,
    MARKET_POLL_MINUTES,
    MARKET_TRACKER_ENABLED,
)

logger = logging.getLogger(__name__)

MARKET_DATA_TOOL = "get_market_data"
MAG7_SYMBOLS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA")
INDEX_SYMBOLS = ("^IXIC", "^GSPC")
INDEX_PROXY_SYMBOLS = ("QQQ", "SPY")
TRACKED_SYMBOLS = INDEX_SYMBOLS + MAG7_SYMBOLS
MONITORED_SYMBOLS = TRACKED_SYMBOLS + INDEX_PROXY_SYMBOLS

_DISPLAY_NAMES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "META": "Meta",
    "TSLA": "Tesla",
    "^IXIC": "Nasdaq",
    "^GSPC": "S&P 500",
    "QQQ": "Nasdaq-100 proxy (QQQ)",
    "SPY": "S&P 500 proxy (SPY)",
}
_ALIAS_PATTERNS = (
    (re.compile(r"\b(?:mag\s*7|magnificent\s+seven)\b", re.IGNORECASE), MAG7_SYMBOLS),
    (re.compile(r"\b(?:nasdaq(?:\s+composite)?|ixic)\b", re.IGNORECASE), ("^IXIC",)),
    (re.compile(r"\b(?:s\s*&\s*p\s*500|s\s+and\s+p\s*500|spx|sp500)\b", re.IGNORECASE), ("^GSPC",)),
    (re.compile(r"\bqqq\b", re.IGNORECASE), ("QQQ",)),
    (re.compile(r"\bspy\b", re.IGNORECASE), ("SPY",)),
    (re.compile(r"\b(?:apple|aapl)\b", re.IGNORECASE), ("AAPL",)),
    (re.compile(r"\b(?:microsoft|msft)\b", re.IGNORECASE), ("MSFT",)),
    (re.compile(r"\b(?:nvidia|nvda)\b", re.IGNORECASE), ("NVDA",)),
    (re.compile(r"\b(?:amazon|amzn)\b", re.IGNORECASE), ("AMZN",)),
    (re.compile(r"\b(?:alphabet|google|googl)\b", re.IGNORECASE), ("GOOGL",)),
    (re.compile(r"\b(?:meta|meta platforms)\b", re.IGNORECASE), ("META",)),
    (re.compile(r"\b(?:tesla|tsla)\b", re.IGNORECASE), ("TSLA",)),
)
_MARKET_QUERY_RE = re.compile(
    r"\b(?:stocks?|shares?|market|ticker|quote|price|trading|premarket|pre-market|"
    r"after\s*hours|postmarket|post-market|mag\s*7|magnificent\s+seven|"
    r"nasdaq|ixic|qqq|s\s*&\s*p|spx|sp500|spy|wall\s+street)\b",
    re.IGNORECASE,
)
_COMPLEX_QUERY_RE = re.compile(
    r"\b(?:why|reason|news|headline|happened|should\s+i|buy|sell|hold|"
    r"price\s+target|forecast|outlook|recommend|explain)\b",
    re.IGNORECASE,
)
_DIRECT_TICKER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:AAPL|MSFT|NVDA|AMZN|GOOGL|META|TSLA|IXIC|SPX|QQQ|SPY)(?![A-Za-z0-9])"
)
_VALID_SYMBOL_RE = re.compile(r"^[A-Z0-9^.=/-]{1,15}$")

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_YAHOO_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 DavosBot/1.0 (personal market monitor)",
}

_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")
_ACTIVE_SESSIONS = frozenset({"premarket", "regular", "after-hours"})
_QUOTE_CACHE_SECONDS = 30.0
_QUOTE_CACHE: dict[str, tuple[float, "MarketQuote"]] = {}
_QUOTE_CACHE_LOCK = threading.Lock()
_TRACKER_START_LOCK = threading.Lock()
_TRACKER_STARTED = False
_ALERT_SETTING_LOCK = threading.Lock()
_ALERT_SETTING_CACHE: tuple[float, str, bool] | None = None


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    name: str
    currency: str
    price: float
    regular_price: float
    previous_close: float
    day_change_pct: float
    session: str
    session_change_pct: float
    confirmed_session_change_pct: float | None
    one_hour_change_pct: float | None
    day_high: float | None
    day_low: float | None
    volume: float | None
    fifty_two_week_high: float | None
    fifty_two_week_low: float | None
    timestamp: int
    stale: bool


@dataclass(frozen=True)
class AlertCandidate:
    keys: tuple[str, ...]
    line: str
    priority: float


@dataclass
class TrackerState:
    known_keys: set[str] = field(default_factory=set)
    last_quote_poll: float = 0.0
    last_alert_at: datetime | None = None


def _finite_number(value: object, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _change_pct(price: float | None, reference: float | None) -> float:
    if price is None or reference is None or reference == 0:
        return 0.0
    return ((price / reference) - 1.0) * 100.0


def _period_contains(period: object, timestamp: float) -> bool:
    if not isinstance(period, dict):
        return False
    start = _finite_number(period.get("start"))
    end = _finite_number(period.get("end"))
    return bool(start is not None and end is not None and start <= timestamp < end)


def _session_at(periods: dict, timestamp: float) -> str:
    if _period_contains(periods.get("pre"), timestamp):
        return "premarket"
    if _period_contains(periods.get("regular"), timestamp):
        return "regular"
    if _period_contains(periods.get("post"), timestamp):
        return "after-hours"
    return "closed"


def _one_hour_change(points: list[tuple[int, float]]) -> float | None:
    if len(points) < 2:
        return None
    latest_ts, latest_price = points[-1]
    target = latest_ts - 60 * 60
    prior = min(points[:-1], key=lambda point: abs(point[0] - target))
    if abs(prior[0] - target) > 12 * 60:
        return None
    return _change_pct(latest_price, prior[1])


def parse_yahoo_chart(
    symbol: str,
    payload: dict,
    *,
    now: datetime | None = None,
) -> MarketQuote:
    """Parse one Yahoo chart response into a stable, alert-friendly quote."""
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if not results:
        error = chart.get("error") if isinstance(chart, dict) else None
        raise ValueError(f"no Yahoo chart result for {symbol}: {error or 'empty response'}")

    result = results[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or [{}]
    closes = quotes[0].get("close") or []
    points: list[tuple[int, float]] = []
    for raw_ts, raw_price in zip(timestamps, closes):
        ts = _finite_number(raw_ts)
        price = _finite_number(raw_price)
        if ts is not None and price is not None and price > 0:
            points.append((int(ts), float(price)))
    if not points:
        raise ValueError(f"no usable Yahoo prices for {symbol}")

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_ts = now_dt.timestamp()
    periods = meta.get("currentTradingPeriod") or {}
    session = _session_at(periods, now_ts)
    latest_ts, latest_price = points[-1]

    previous_close = _finite_number(meta.get("previousClose"))
    if previous_close is None:
        previous_close = _finite_number(meta.get("chartPreviousClose"), latest_price)
    regular_price = _finite_number(meta.get("regularMarketPrice"), latest_price)
    if regular_price is None or previous_close is None:
        raise ValueError(f"Yahoo response missing reference prices for {symbol}")

    reference = regular_price if session == "after-hours" else previous_close
    if session == "closed":
        reference = previous_close
    session_price = latest_price if session in _ACTIVE_SESSIONS else regular_price
    session_change = _change_pct(session_price, reference)

    active_points = [
        point for point in points
        if _session_at(periods, point[0]) == session
    ]
    confirmed: float | None = None
    if session in _ACTIVE_SESSIONS and len(active_points) >= 2:
        recent = active_points[-2:]
        if recent[-1][0] - recent[-2][0] <= 15 * 60:
            changes = [_change_pct(point[1], reference) for point in recent]
            if changes[0] == 0 or changes[1] == 0 or (changes[0] > 0) == (changes[1] > 0):
                sign = 1.0 if changes[-1] >= 0 else -1.0
                confirmed = sign * min(abs(value) for value in changes)

    stale = bool(
        session in _ACTIVE_SESSIONS
        and (now_ts - latest_ts > 12 * 60 or latest_ts - now_ts > 5 * 60)
    )
    resolved_symbol = str(meta.get("symbol") or symbol).upper()
    return MarketQuote(
        symbol=resolved_symbol,
        name=str(meta.get("shortName") or meta.get("longName") or _DISPLAY_NAMES.get(resolved_symbol, resolved_symbol)),
        currency=str(meta.get("currency") or "USD"),
        price=float(session_price),
        regular_price=float(regular_price),
        previous_close=float(previous_close),
        day_change_pct=_change_pct(regular_price, previous_close),
        session=session,
        session_change_pct=session_change,
        confirmed_session_change_pct=confirmed,
        one_hour_change_pct=_one_hour_change(points),
        day_high=_finite_number(meta.get("regularMarketDayHigh")),
        day_low=_finite_number(meta.get("regularMarketDayLow")),
        volume=_finite_number(meta.get("regularMarketVolume")),
        fifty_two_week_high=_finite_number(meta.get("fiftyTwoWeekHigh")),
        fifty_two_week_low=_finite_number(meta.get("fiftyTwoWeekLow")),
        timestamp=latest_ts,
        stale=stale,
    )


def _fetch_quote_uncached(symbol: str, now: datetime | None = None) -> MarketQuote:
    encoded = url_quote(symbol, safe="")
    response = requests.get(
        _YAHOO_CHART_URL.format(symbol=encoded),
        params={
            "interval": "5m",
            "range": "1d",
            "includePrePost": "true",
        },
        headers=_YAHOO_HEADERS,
        timeout=MARKET_DATA_TIMEOUT,
    )
    response.raise_for_status()
    return parse_yahoo_chart(symbol, response.json(), now=now)


def _normalize_symbol(symbol: object) -> str | None:
    raw = str(symbol or "").strip().upper()
    aliases = {
        "NASDAQ": "^IXIC",
        "IXIC": "^IXIC",
        "SPX": "^GSPC",
        "SP500": "^GSPC",
        "S&P": "^GSPC",
        "S&P500": "^GSPC",
        "GOOG": "GOOGL",
    }
    raw = aliases.get(raw, raw)
    return raw if _VALID_SYMBOL_RE.fullmatch(raw) else None


def fetch_quote(
    symbol: str,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> MarketQuote:
    normalized = _normalize_symbol(symbol)
    if not normalized:
        raise ValueError(f"invalid market symbol: {symbol!r}")
    if not force:
        with _QUOTE_CACHE_LOCK:
            cached = _QUOTE_CACHE.get(normalized)
        if cached and time.monotonic() - cached[0] <= _QUOTE_CACHE_SECONDS:
            return cached[1]

    market_quote = _fetch_quote_uncached(normalized, now=now)
    with _QUOTE_CACHE_LOCK:
        _QUOTE_CACHE[normalized] = (time.monotonic(), market_quote)
    return market_quote


def fetch_quotes(
    symbols: Iterable[str],
    *,
    force: bool = False,
    now: datetime | None = None,
) -> tuple[list[MarketQuote], dict[str, str]]:
    normalized: list[str] = []
    for symbol in symbols:
        clean = _normalize_symbol(symbol)
        if clean and clean not in normalized:
            normalized.append(clean)
    normalized = normalized[:12]
    if not normalized:
        return [], {}

    found: dict[str, MarketQuote] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(normalized))) as pool:
        futures = {
            pool.submit(fetch_quote, symbol, force=force, now=now): symbol
            for symbol in normalized
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                found[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = str(exc)[:160]
    return [found[symbol] for symbol in normalized if symbol in found], errors


def extract_market_symbols(text: str) -> list[str]:
    raw = text or ""
    symbols: list[str] = []
    for pattern, matches in _ALIAS_PATTERNS:
        if pattern.search(raw):
            for symbol in matches:
                if symbol not in symbols:
                    symbols.append(symbol)
    for ticker in _DIRECT_TICKER_RE.findall(raw):
        clean = _normalize_symbol(ticker)
        if clean and clean not in symbols:
            symbols.append(clean)
    return symbols


def is_market_query(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _MARKET_QUERY_RE.search(raw):
        return True
    if _DIRECT_TICKER_RE.search(raw):
        return True
    return bool(re.fullmatch(r"(?:AAPL|MSFT|NVDA|AMZN|GOOGL|META|TSLA|IXIC|SPX|QQQ|SPY)\??", raw))


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def _format_price(market_quote: MarketQuote) -> str:
    if market_quote.symbol.startswith("^"):
        return f"{market_quote.price:,.2f}"
    prefix = "$" if market_quote.currency == "USD" else f"{market_quote.currency} "
    return f"{prefix}{market_quote.price:,.2f}"


def _format_volume(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def _format_pt_clock(dt: datetime) -> str:
    return f"{dt.strftime('%I:%M %p').lstrip('0')} PT"


def _as_of_line(quotes: Sequence[MarketQuote]) -> str:
    if not quotes:
        return ""
    timestamp = max(market_quote.timestamp for market_quote in quotes)
    dt = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(_PT)
    return _format_pt_clock(dt)


def _quote_line(market_quote: MarketQuote) -> str:
    name = _DISPLAY_NAMES.get(market_quote.symbol, market_quote.symbol)
    stale = " | STALE" if market_quote.stale else ""
    if market_quote.session == "after-hours":
        movement = f"{_format_pct(market_quote.session_change_pct)} after hours"
        regular = f"{_format_pct(market_quote.day_change_pct)} regular"
        return f"{name}: {_format_price(market_quote)} | {movement} | {regular}{stale}"
    if market_quote.session == "premarket":
        return f"{name}: {_format_price(market_quote)} | {_format_pct(market_quote.session_change_pct)} premarket{stale}"
    if market_quote.session == "regular":
        return f"{name}: {_format_price(market_quote)} | {_format_pct(market_quote.day_change_pct)} regular session{stale}"
    return f"{name}: {_format_price(market_quote)} | {_format_pct(market_quote.day_change_pct)} last regular session{stale}"


def format_market_snapshot(quotes: Sequence[MarketQuote], *, movers_only: bool = False) -> str:
    if not quotes:
        return "Market data is temporarily unavailable. Try again in a minute."
    by_symbol = {market_quote.symbol: market_quote for market_quote in quotes}
    stale_count = sum(1 for market_quote in quotes if market_quote.stale)
    lines = [f"Market snapshot • {_as_of_line(quotes)}"]
    indices = [by_symbol[symbol] for symbol in INDEX_SYMBOLS if symbol in by_symbol]
    stocks = [by_symbol[symbol] for symbol in MAG7_SYMBOLS if symbol in by_symbol]
    proxies = [
        by_symbol[symbol] for symbol in INDEX_PROXY_SYMBOLS
        if symbol in by_symbol and by_symbol[symbol].session in {"premarket", "after-hours"}
    ]
    if movers_only:
        ranked = sorted(stocks, key=lambda item: abs(item.session_change_pct if item.session != "closed" else item.day_change_pct), reverse=True)
        lines.append("Biggest Mag 7 moves:")
        lines.extend(f"- {_quote_line(item)}" for item in ranked[:5])
    else:
        if indices:
            lines.append("Indices")
            lines.extend(f"- {_quote_line(item)}" for item in indices)
        if stocks:
            lines.append("Mag 7")
            lines.extend(f"- {_quote_line(item)}" for item in stocks)
        if proxies:
            lines.append("Extended-hours index proxies")
            lines.extend(f"- {_quote_line(item)}" for item in proxies)
    footer = "Yahoo Finance data; quotes can be delayed."
    if stale_count:
        footer = f"{footer} {stale_count} quote(s) look stale."
    lines.append(footer)
    return "\n".join(lines)


def format_quote_detail(market_quote: MarketQuote) -> str:
    lines = [_quote_line(market_quote)]
    if market_quote.one_hour_change_pct is not None:
        lines.append(f"Last hour: {_format_pct(market_quote.one_hour_change_pct)}")
    if market_quote.day_low is not None and market_quote.day_high is not None:
        lines.append(f"Day range: {market_quote.day_low:,.2f}-{market_quote.day_high:,.2f}")
    lines.append(f"Volume: {_format_volume(market_quote.volume)}")
    quote_time = datetime.fromtimestamp(market_quote.timestamp, timezone.utc).astimezone(_PT)
    freshness = "stale" if market_quote.stale else "fresh"
    lines.append(f"As of {_format_pt_clock(quote_time)} ({market_quote.session}; {freshness}).")
    lines.append("Yahoo Finance data; quotes can be delayed.")
    return "\n".join(lines)


def market_status_summary() -> str:
    tracker_state = "on" if MARKET_TRACKER_ENABLED else "off"
    alert_state = "on" if market_alerts_enabled() else "off"
    return "\n".join(
        [
            f"Market tracker: {tracker_state}, polling every {MARKET_POLL_MINUTES} minutes from 4am-8pm ET on weekdays.",
            "Watchlist: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA; ^IXIC Nasdaq and ^GSPC S&P 500 during regular hours; QQQ/SPY as labeled extended-hours proxies.",
            f"Market alerts: {alert_state}. Need-to-know only: unusually large single-name moves, sharp index moves, fast breaks, or broad Mag 7 moves.",
            f"Two confirmed ticks are required. Routine follow-ups wait {MARKET_ALERT_COOLDOWN_MINUTES} minutes and are remembered instead of arriving late; only a much larger escalation can break the cooldown.",
            "Quotes: Yahoo Finance. Data can be delayed.",
        ]
    )


def market_help() -> str:
    return "\n".join(
        [
            "Market commands:",
            "- `market` or `stocks` - Mag 7, Nasdaq, and S&P 500 snapshot",
            "- `market movers` - biggest Mag 7 moves",
            "- `quote NVDA` or `market quote NVDA` - one-symbol detail",
            "- `market status` - watchlist, sessions, sources, and alert policy",
            "- `market alerts on|off|status` - owner-only alert control",
            "Natural questions work too: `how's NVDA?`, `how's Mag 7?`, `what moved after hours?`",
        ]
    )


def get_market_data(
    *,
    view: str = "snapshot",
    symbols: Sequence[str] | str | None = None,
) -> str:
    clean_view = str(view or "snapshot").strip().lower()
    if clean_view in {"status", "alerts"}:
        return market_status_summary()

    if isinstance(symbols, str):
        requested = [part for part in re.split(r"[\s,]+", symbols) if part]
    else:
        requested = list(symbols or [])
    if not requested:
        requested = list(MONITORED_SYMBOLS)
    quotes, _errors = fetch_quotes(requested)
    if clean_view in {"movers", "move"}:
        return format_market_snapshot(quotes, movers_only=True)
    if clean_view in {"quote", "quotes", "detail"} and len(quotes) == 1:
        return format_quote_detail(quotes[0])
    return format_market_snapshot(quotes)


def handle_market_command(text: str, *, sender_is_owner: bool, allow_live_lookup: bool = True) -> str | None:
    """Dispatch explicit deterministic market commands."""
    raw = re.sub(r"^\s*/", "", text or "").strip()
    lower = raw.lower()
    lower = re.sub(r"^(?:market|stocks?)\b", "", lower).strip()

    alert_match = re.fullmatch(r"alerts?(?:\s+(on|off|status))?", lower)
    if alert_match:
        action = alert_match.group(1) or "status"
        if action in {"on", "off"}:
            if not sender_is_owner:
                return "Market alert controls are owner-only."
            return set_market_alerts_enabled(action == "on")
        return market_status_summary()
    if lower in {"help", "commands", "?"}:
        return market_help()
    if lower in {"status", "watchlist", "alerts status"}:
        return market_status_summary()
    if lower in {"earnings", "calendar", "earnings calendar"}:
        return "Market tracking is price-only. Try `market`, `market movers`, or `quote NVDA`."
    if lower in {"movers", "moves", "what moved", "after hours", "after-hours"}:
        return get_market_data(view="movers") if allow_live_lookup else None

    quote_match = re.fullmatch(r"(?:quote|stock|ticker)(?:\s+quote)?\s+([A-Za-z0-9^.=/-]{1,15})", lower)
    if not quote_match:
        quote_match = re.fullmatch(r"(?:quote\s+)?([A-Za-z0-9^.=/-]{1,15})", lower)
    if quote_match and lower:
        symbol = _normalize_symbol(quote_match.group(1))
        if not symbol:
            return "Use a valid ticker, for example `quote NVDA`."
        return get_market_data(view="quote", symbols=[symbol]) if allow_live_lookup else None
    if lower in {"", "summary", "watch", "check"}:
        return get_market_data(view="snapshot") if allow_live_lookup else None
    return market_help()


def handle_market_query(text: str, *, allow_live_lookup: bool = True) -> str | None:
    """Handle simple market questions without paying for an LLM round trip."""
    if not is_market_query(text) or _COMPLEX_QUERY_RE.search(text or ""):
        return None
    lower = (text or "").lower()
    if re.search(r"\b(?:status|alerts?|tracking|watchlist|thresholds?)\b", lower):
        return market_status_summary()
    if not allow_live_lookup:
        return None
    if re.search(r"\b(?:movers?|moved|moving|biggest\s+moves?|what(?:'s|\s+is)\s+moving|what\s+moved)\b", lower):
        return get_market_data(view="movers")
    symbols = extract_market_symbols(text)
    if len(symbols) == 1:
        return get_market_data(view="quote", symbols=symbols)
    if symbols and set(symbols) != set(MAG7_SYMBOLS):
        return get_market_data(view="snapshot", symbols=symbols)
    return get_market_data(view="snapshot")


_STOCK_REGULAR_THRESHOLDS = (5.0, 8.0, 12.0)
_STOCK_EXTENDED_THRESHOLDS = (4.0, 7.0, 10.0)
_STOCK_RAPID_THRESHOLDS = (4.0, 7.0)
_INDEX_REGULAR_THRESHOLDS = (1.75, 3.0, 5.0)
_INDEX_EXTENDED_THRESHOLDS = (1.25, 2.0, 3.0)
_INDEX_RAPID_THRESHOLDS = (1.25, 2.0, 3.0)


def _crossed_tier_keys(
    *,
    category: str,
    symbol: str,
    trading_date: date,
    value: float,
    thresholds: Sequence[float],
) -> list[str]:
    direction = "up" if value >= 0 else "down"
    return [
        f"{trading_date}:{symbol}:{category}:{direction}:{threshold:g}"
        for threshold in thresholds
        if abs(value) >= threshold
    ]


def build_price_alerts(
    quotes: Sequence[MarketQuote],
    *,
    trading_date: date,
) -> list[AlertCandidate]:
    candidates: list[AlertCandidate] = []
    by_symbol = {market_quote.symbol: market_quote for market_quote in quotes}

    for market_quote in quotes:
        if (
            market_quote.symbol not in MONITORED_SYMBOLS
            or market_quote.session not in _ACTIVE_SESSIONS
            or market_quote.stale
            or market_quote.confirmed_session_change_pct is None
        ):
            continue
        is_proxy = market_quote.symbol in INDEX_PROXY_SYMBOLS
        if is_proxy and market_quote.session == "regular":
            continue
        is_index = market_quote.symbol in INDEX_SYMBOLS or is_proxy
        extended = market_quote.session != "regular"
        movement = market_quote.confirmed_session_change_pct
        display_movement = market_quote.session_change_pct
        if is_index:
            thresholds = _INDEX_EXTENDED_THRESHOLDS if extended else _INDEX_REGULAR_THRESHOLDS
            rapid_thresholds = _INDEX_RAPID_THRESHOLDS
        else:
            thresholds = _STOCK_EXTENDED_THRESHOLDS if extended else _STOCK_REGULAR_THRESHOLDS
            rapid_thresholds = _STOCK_RAPID_THRESHOLDS
        category = "extended" if extended else "day"
        keys = _crossed_tier_keys(
            category=category,
            symbol=market_quote.symbol,
            trading_date=trading_date,
            value=movement,
            thresholds=thresholds,
        )
        rapid = market_quote.one_hour_change_pct
        rapid_keys: list[str] = []
        if rapid is not None:
            rapid_keys = _crossed_tier_keys(
                category="one_hour",
                symbol=market_quote.symbol,
                trading_date=trading_date,
                value=rapid,
                thresholds=rapid_thresholds,
            )
        if not keys and not rapid_keys:
            continue

        label = _DISPLAY_NAMES.get(market_quote.symbol, market_quote.symbol)
        session_label = (
            "after hours" if market_quote.session == "after-hours"
            else "premarket" if market_quote.session == "premarket"
            else "today"
        )
        line = f"{label} {display_movement:+.2f}% {session_label} to {_format_price(market_quote)}"
        if rapid_keys and rapid is not None:
            line += f" ({rapid:+.2f}% in the last hour)"
        candidates.append(
            AlertCandidate(
                keys=tuple(dict.fromkeys(keys + rapid_keys)),
                line=line,
                priority=max(abs(display_movement), abs(rapid or 0.0)),
            )
        )

    mag7 = [
        by_symbol[symbol] for symbol in MAG7_SYMBOLS
        if symbol in by_symbol
        and by_symbol[symbol].session == "regular"
        and not by_symbol[symbol].stale
    ]
    if len(mag7) >= 5:
        up = [item for item in mag7 if item.day_change_pct > 0]
        down = [item for item in mag7 if item.day_change_pct < 0]
        dominant = up if len(up) >= len(down) else down
        if len(dominant) >= 5:
            median_move = statistics.median(item.day_change_pct for item in dominant)
            broad_keys = _crossed_tier_keys(
                category=f"broad_{len(dominant)}of7",
                symbol="MAG7",
                trading_date=trading_date,
                value=median_move,
                thresholds=(3.0, 5.0),
            )
            if broad_keys:
                direction = "rally" if median_move > 0 else "selloff"
                index_context = by_symbol.get("^IXIC")
                context = (
                    f"; Nasdaq {index_context.day_change_pct:+.2f}%"
                    if index_context and not index_context.stale
                    else ""
                )
                candidates.append(
                    AlertCandidate(
                        keys=tuple(broad_keys),
                        line=f"Broad Mag 7 {direction}: {len(dominant)}/7 moving together, median {median_move:+.2f}%{context}",
                        priority=abs(median_move) + 0.25,
                    )
                )
    return candidates


def format_market_alert(
    candidates: Sequence[AlertCandidate],
    *,
    now: datetime,
) -> str:
    local_now = now.astimezone(_PT)
    ordered = sorted(candidates, key=lambda item: item.priority, reverse=True)
    noun = "move" if len(ordered) == 1 else "moves"
    lines = [f"Need-to-know market {noun} • {_format_pt_clock(local_now)}"]
    lines.extend(f"- {candidate.line}" for candidate in ordered[:8])
    if len(ordered) > 8:
        lines.append(f"- Plus {len(ordered) - 8} more threshold crossing(s).")
    lines.append("Yahoo Finance data can be delayed; verify before trading.")
    return "\n".join(lines)


def _load_recent_alert_state(db_path: str = BOT_DB_PATH) -> tuple[set[str], datetime | None]:
    keys: set[str] = set()
    last_alert_at: datetime | None = None
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT timestamp, event_type, payload
                FROM bot_log
                WHERE event_type IN ('market_alert', 'market_alert_seen')
                  AND timestamp >= datetime('now', '-14 days')
                ORDER BY id DESC
                LIMIT 1000
                """
            ).fetchall()
        for raw_timestamp, event_type, raw_payload in rows:
            if event_type == "market_alert" and last_alert_at is None and raw_timestamp:
                try:
                    last_alert_at = datetime.fromisoformat(str(raw_timestamp)).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            try:
                payload = json.loads(raw_payload or "{}")
            except (TypeError, ValueError):
                continue
            for key in payload.get("event_keys") or []:
                if isinstance(key, str):
                    keys.add(key)
    except Exception as exc:
        logger.warning("Could not restore market alert dedupe state: %s", exc)
    return keys, last_alert_at


def _load_recent_alert_keys(db_path: str = BOT_DB_PATH) -> set[str]:
    return _load_recent_alert_state(db_path)[0]


def market_alerts_enabled(*, db_path: str = BOT_DB_PATH, force: bool = False) -> bool:
    """Return the persisted alert toggle, bounded by hard env switches."""
    global _ALERT_SETTING_CACHE
    if not MARKET_TRACKER_ENABLED or not MARKET_ALERTS_ENABLED:
        return False
    with _ALERT_SETTING_LOCK:
        cached = _ALERT_SETTING_CACHE
        if not force and cached and cached[1] == db_path and time.monotonic() - cached[0] < 15:
            return cached[2]
    enabled = True
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                """
                SELECT payload
                FROM bot_log
                WHERE event_type = 'market_alert_setting'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row:
            payload = json.loads(row[0] or "{}")
            enabled = bool(payload.get("enabled", True))
    except Exception as exc:
        logger.warning("Could not read market alert setting; using env default: %s", exc)
    with _ALERT_SETTING_LOCK:
        _ALERT_SETTING_CACHE = (time.monotonic(), db_path, enabled)
    return enabled


def set_market_alerts_enabled(enabled: bool, *, db_path: str = BOT_DB_PATH) -> str:
    """Persist the owner alert toggle in bot_log without a schema change."""
    global _ALERT_SETTING_CACHE
    if enabled and (not MARKET_TRACKER_ENABLED or not MARKET_ALERTS_ENABLED):
        return "Market alerts are hard-disabled in the Mini environment. Enable the market tracker env settings and restart first."
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (
                    "owner",
                    "market_alert_setting",
                    json.dumps({"enabled": bool(enabled)}),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not persist market alert setting: %s", exc)
        return "I couldn't save the market alert setting. Nothing changed."
    with _ALERT_SETTING_LOCK:
        _ALERT_SETTING_CACHE = (time.monotonic(), db_path, bool(enabled))
    state = "on" if enabled else "off"
    return f"Market alerts are {state}. On-demand quotes still work."


def _record_market_event(
    event_keys: Sequence[str],
    *,
    event_type: str,
    db_path: str = BOT_DB_PATH,
) -> None:
    if event_type not in {"market_alert", "market_alert_seen"}:
        raise ValueError("Unsupported market event type")
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (
                    "system",
                    event_type,
                    json.dumps({"event_keys": sorted(set(event_keys)), "source": "market_tracker"}),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not persist market alert dedupe state: %s", exc)


def _record_market_alert(
    event_keys: Sequence[str],
    *,
    db_path: str = BOT_DB_PATH,
) -> None:
    _record_market_event(event_keys, event_type="market_alert", db_path=db_path)


def _record_market_seen(
    event_keys: Sequence[str],
    *,
    db_path: str = BOT_DB_PATH,
) -> None:
    _record_market_event(event_keys, event_type="market_alert_seen", db_path=db_path)


def run_market_alert_cycle(
    send_alert: Callable[[str], bool],
    *,
    now: datetime | None = None,
    include_quotes: bool = True,
    state: TrackerState | None = None,
    quote_fetcher: Callable[..., tuple[list[MarketQuote], dict[str, str]]] = fetch_quotes,
    db_path: str = BOT_DB_PATH,
) -> str | None:
    """Run one alert evaluation and return the sent message, if any."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    et_now = current.astimezone(_ET)
    if state is None:
        known_keys, last_alert_at = _load_recent_alert_state(db_path)
        runtime = TrackerState(known_keys=known_keys, last_alert_at=last_alert_at)
    else:
        runtime = state

    candidates: list[AlertCandidate] = []
    if include_quotes:
        quotes, errors = quote_fetcher(MONITORED_SYMBOLS, force=True, now=current)
        if errors:
            logger.warning(
                "Market tracker quote failures: %s",
                ", ".join(sorted(errors)),
            )
        candidates.extend(
            build_price_alerts(
                quotes,
                trading_date=et_now.date(),
            )
        )

    pending = [
        candidate for candidate in candidates
        if any(key not in runtime.known_keys for key in candidate.keys)
    ]
    if not pending:
        return None
    cooldown = timedelta(minutes=MARKET_ALERT_COOLDOWN_MINUTES)
    within_cooldown = bool(
        runtime.last_alert_at
        and current - runtime.last_alert_at < cooldown
    )
    escalation = any(
        _candidate_is_escalation(candidate, runtime.known_keys)
        for candidate in pending
    )
    critical = any(_candidate_is_critical(candidate) for candidate in pending)
    if within_cooldown and not escalation and not critical:
        seen_keys = sorted({key for candidate in pending for key in candidate.keys})
        runtime.known_keys.update(seen_keys)
        _record_market_seen(seen_keys, db_path=db_path)
        logger.info(
            "Suppressed %d routine market threshold key(s) during cooldown",
            len(seen_keys),
        )
        return None
    message = format_market_alert(pending, now=current)
    try:
        sent = bool(send_alert(message))
    except Exception as exc:
        logger.warning("Market alert send failed: %s", exc)
        return None
    if not sent:
        logger.warning("Market alert send returned false")
        return None

    sent_keys = sorted({key for candidate in pending for key in candidate.keys})
    runtime.known_keys.update(sent_keys)
    runtime.last_alert_at = current
    _record_market_alert(sent_keys, db_path=db_path)
    logger.info("Market alert sent with %d event key(s)", len(sent_keys))
    return message


def _market_session_active(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    minute = now_et.hour * 60 + now_et.minute
    return 4 * 60 <= minute < 20 * 60


def _candidate_is_escalation(candidate: AlertCandidate, known_keys: set[str]) -> bool:
    for key in candidate.keys:
        prefix, separator, raw_threshold = key.rpartition(":")
        if not separator:
            continue
        try:
            threshold = float(raw_threshold)
        except ValueError:
            continue
        for known in known_keys:
            known_prefix, known_separator, known_threshold = known.rpartition(":")
            if known_separator and known_prefix == prefix:
                try:
                    if float(known_threshold) < threshold:
                        return True
                except ValueError:
                    continue
    return False


def _candidate_is_critical(candidate: AlertCandidate) -> bool:
    """Allow a genuinely extreme move to break the global cooldown."""
    for key in candidate.keys:
        parts = key.split(":")
        if len(parts) != 5:
            continue
        _trading_date, symbol, category, _direction, raw_threshold = parts
        try:
            threshold = float(raw_threshold)
        except ValueError:
            continue
        if symbol in INDEX_SYMBOLS or symbol in INDEX_PROXY_SYMBOLS:
            if threshold >= 3.0:
                return True
        elif symbol == "MAG7":
            if threshold >= 5.0:
                return True
        elif category == "extended":
            if threshold >= 7.0:
                return True
        elif category == "one_hour":
            if threshold >= 7.0:
                return True
        elif threshold >= 8.0:
            return True
    return False


def _tracker_loop(send_alert: Callable[[str], bool]) -> None:
    known_keys, last_alert_at = _load_recent_alert_state()
    state = TrackerState(known_keys=known_keys, last_alert_at=last_alert_at)
    logger.info(
        "Market tracker started: %d symbols, %dm polling, alerts=%s",
        len(MONITORED_SYMBOLS),
        MARKET_POLL_MINUTES,
        MARKET_ALERTS_ENABLED,
    )
    while True:
        monotonic_now = time.monotonic()
        current = datetime.now(timezone.utc)
        et_now = current.astimezone(_ET)
        quote_due = bool(
            market_alerts_enabled()
            and _market_session_active(et_now)
            and monotonic_now - state.last_quote_poll >= MARKET_POLL_MINUTES * 60
        )
        if quote_due:
            state.last_quote_poll = monotonic_now
            try:
                run_market_alert_cycle(
                    send_alert,
                    now=current,
                    state=state,
                )
            except Exception:
                logger.exception("Market tracker cycle failed")
        time.sleep(30)


def start_market_tracker(send_alert: Callable[[str], bool]) -> threading.Thread | None:
    """Start the single daemon market-monitor thread."""
    global _TRACKER_STARTED
    if not MARKET_TRACKER_ENABLED:
        logger.info("Market tracker disabled")
        return None
    with _TRACKER_START_LOCK:
        if _TRACKER_STARTED:
            return None
        _TRACKER_STARTED = True
        thread = threading.Thread(
            target=_tracker_loop,
            args=(send_alert,),
            name="davos-market-tracker",
            daemon=True,
        )
        thread.start()
        return thread
