import json
import logging
import re

from .config import BOT_DB_PATH
from .db import connect_bot_db

logger = logging.getLogger("davosbot.tools")

_ROTATING_MORNING_INTROS = [
    "Morning boys.",
    "Good morning fellas.",
    "Morning crew.",
    "Alright boys, new day.",
    "Fresh slate today.",
    "Let's have a day.",
    "A little momentum early.",
    "Morning fellas.",
]

_FALLBACK_QUOTES = [
    "Keep it simple today: show up, do the next right thing, and let momentum find you.",
    "Tiny wins count. Stack a few early and the day starts working with you.",
    "You do not need a perfect plan. You need one clean next move.",
    "Quiet consistency beats dramatic effort. Put one good rep on the board.",
    "Start with the thing you have been avoiding. The day gets lighter after that.",
    "Make the first hour honest and the rest of the day has something to build on.",
    "Do the useful thing before the urgent thing starts yelling.",
    "A little discipline early buys a lot of peace later.",
    "Set the tone before the day sets it for you.",
    "No need to force magic. Just make the next good decision.",
]
_ZENQUOTES_TODAY_URL = "https://zenquotes.io/api/today"
_ZENQUOTES_ATTRIBUTION = "Source: https://zenquotes.io/"

_MORNING_WEEKDAY_RE = re.compile(
    r"\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
    re.IGNORECASE,
)
_MORNING_DATE_RE = re.compile(
    r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
    r"(?:\d{1,2}(?:st|nd|rd|th)?(?:,?\s+20\d{2})?|20\d{2})\b",
    re.IGNORECASE,
)


def _select_morning_intro(payload: dict, date_key: str) -> str:
    if (payload or {}).get("intro_mode") == "rotate":
        import hashlib as _hashlib
        seed = f"{payload.get('recipient', '')}:{date_key}:morning_intro"
        idx = int(_hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(_ROTATING_MORNING_INTROS)
        return _ROTATING_MORNING_INTROS[idx]
    return ((payload or {}).get("intro") or "").strip()


_MORNING_GREETING_PREFIX_RE = re.compile(
    r"^\s*(?:good\s+morning|morning)(?:\s+(?:boys|fellas|crew|team|everyone|y'all|you\s+all))?[,.!:\-;\s]+",
    re.IGNORECASE,
)


def _strip_duplicate_morning_greeting(intro: str, quote: str) -> str:
    """Drop quote-side greetings when the cron intro already opened the message."""
    intro_norm = (intro or "").strip().lower()
    quote = (quote or "").strip()
    if not intro_norm or not quote:
        return quote
    if not re.match(r"^(?:good\s+morning|morning)\b", intro_norm):
        return quote
    cleaned = _MORNING_GREETING_PREFIX_RE.sub("", quote, count=1).strip()
    if not cleaned:
        return quote
    return cleaned[:1].upper() + cleaned[1:]


def _render_morning_message_body(payload: dict, quote: str, now_pt=None) -> str:
    if now_pt is None:
        from datetime import datetime as _dt, timezone as _tz
        from zoneinfo import ZoneInfo as _ZoneInfo
        now_pt = _dt.now(_tz.utc).astimezone(_ZoneInfo("America/Los_Angeles"))
    date_key = now_pt.strftime("%Y-%m-%d")
    intro = _select_morning_intro(payload or {}, date_key)
    quote = _strip_duplicate_morning_greeting(intro, quote)
    return f"{intro}\n\n{quote}" if intro else quote


def _morning_quote_mentions_date(text: str) -> bool:
    return bool(_MORNING_WEEKDAY_RE.search(text or "") or _MORNING_DATE_RE.search(text or ""))


def _fetch_zenquotes_quote(*, request_get=None, timeout: float = 8.0) -> str:
    if request_get is None:
        import requests

        request_get = requests.get

    response = request_get(_ZENQUOTES_TODAY_URL, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("ZenQuotes returned an unexpected response")

    quote = re.sub(r"\s+", " ", str(data[0].get("q") or "")).strip()
    author = re.sub(r"\s+", " ", str(data[0].get("a") or "")).strip()
    if not quote or len(quote) > 500:
        raise ValueError("ZenQuotes returned an invalid quote")
    if not author or len(author) > 120:
        author = "Unknown"
    return f"{quote}\n- {author}\n\n{_ZENQUOTES_ATTRIBUTION}"


def _get_inspirational_quote(
    *,
    gemini_api_key: str,
    rewrite_fn,
    recent_hashes_fn,
    log_choice_fn,
    logger_obj=None,
    zenquotes_fn=None,
) -> str:
    """Fetch the daily ZenQuote, with Gemini and local fail-soft fallbacks."""
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo as _ZoneInfo
    import hashlib as _hashlib

    log = logger_obj or logger
    date_key = _dt.now(_tz.utc).astimezone(_ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

    def _fallback(source: str) -> str:
        start_idx = int(_hashlib.sha256(date_key.encode("utf-8")).hexdigest(), 16) % len(_FALLBACK_QUOTES)
        recent_hashes = recent_hashes_fn(date_key)
        quote = _FALLBACK_QUOTES[start_idx]
        for offset in range(len(_FALLBACK_QUOTES)):
            candidate = _FALLBACK_QUOTES[(start_idx + offset) % len(_FALLBACK_QUOTES)]
            if _quote_hash(candidate) not in recent_hashes:
                quote = candidate
                break
        log_choice_fn(date_key, source, quote)
        return quote

    fetch_zenquote = zenquotes_fn or _fetch_zenquotes_quote
    try:
        quote = (fetch_zenquote() or "").strip()
        if not quote:
            raise ValueError("ZenQuotes returned an empty quote")
        if _morning_quote_mentions_date(quote):
            raise ValueError("ZenQuotes quote mentioned a date")
        if _quote_hash(quote) in recent_hashes_fn(date_key):
            raise ValueError("ZenQuotes repeated a recent quote")
        log_choice_fn(date_key, "zenquotes", quote)
        return quote
    except Exception as exc:
        log.warning("ZenQuotes morning quote failed: %s", exc)

    if not gemini_api_key:
        return _fallback("fallback:zenquotes_and_no_gemini_key")
    try:
        quote = rewrite_fn(
            f"Rotation seed: {date_key}. Write one short original morning line for a group chat. "
            "Tone: casual, warm, quietly inspirational, like a friend giving a useful nudge. "
            "Do not mention the date, weekday, month, year, or the seed. "
            "Do not start with 'good morning', 'morning', 'hey', or any greeting. "
            "No attribution, no preamble, no hashtags, no quotes around it. Max 22 words."
        )
        quote = (quote or "").strip()
        if not quote:
            return _fallback("fallback:empty_gemini")
        if _morning_quote_mentions_date(quote):
            log.warning("Gemini morning quote mentioned a date; using deterministic fallback")
            return _fallback("fallback:gemini_date_leak")
        if _quote_hash(quote) in recent_hashes_fn(date_key):
            log.warning("Gemini repeated a recent morning quote; using deterministic fallback")
            return _fallback("fallback:gemini_repeat")
        log_choice_fn(date_key, "gemini", quote)
        return quote
    except Exception as e:
        log.warning("get_inspirational_quote Gemini failed: %s", e)
        return _fallback("fallback:gemini_error")


def _log_quote_choice(
    date_key: str,
    source: str,
    quote: str,
    *,
    db_path: str = BOT_DB_PATH,
    connect_fn=connect_bot_db,
    logger_obj=None,
) -> None:
    log = logger_obj or logger
    try:
        quote_hash = _quote_hash(quote)
        with connect_fn(db_path) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (
                    "system",
                    "morning_quote_selected",
                    json.dumps({"date": date_key, "source": source, "quote_hash": quote_hash}),
                ),
            )
    except Exception as e:
        log.warning("quote choice log failed: %s", e)


def _quote_hash(quote: str) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256((quote or "").encode("utf-8")).hexdigest()[:12]


def _recent_quote_hashes(
    date_key: str,
    *,
    db_path: str = BOT_DB_PATH,
    connect_fn=connect_bot_db,
    logger_obj=None,
) -> set[str]:
    log = logger_obj or logger
    hashes: set[str] = set()
    try:
        with connect_fn(db_path) as conn:
            rows = conn.execute(
                "SELECT payload FROM bot_log WHERE event_type = 'morning_quote_selected' ORDER BY id DESC LIMIT 60"
            ).fetchall()
        for (payload,) in rows:
            try:
                data = json.loads(payload or "{}")
            except Exception:
                continue
            quote_hash = data.get("quote_hash")
            if data.get("date") != date_key and isinstance(quote_hash, str):
                hashes.add(quote_hash)
    except Exception as e:
        log.warning("quote repeat check failed: %s", e)
    return hashes


def _quote_seen_recently(date_key: str, quote: str) -> bool:
    return _quote_hash(quote) in _recent_quote_hashes(date_key)
