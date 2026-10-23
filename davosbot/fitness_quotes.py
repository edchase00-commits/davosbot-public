"""Owner-only daily motivation. Reuses selection, never group quote/send state.

The only stored text is today's public/original line. Prior days retain hashes
only. Preview reads are provider-free; generation uses existing quote providers.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from zoneinfo import ZoneInfo

from . import config, morning_quotes, permissions

PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_CACHE_BYTES = 16384
MAX_QUOTE_CHARS = 700
MAX_HISTORY = 60
SOURCES = frozenset({"zenquotes", "gemini", "fallback:zenquotes_and_no_gemini_key",
                     "fallback:empty_gemini", "fallback:gemini_date_leak",
                     "fallback:gemini_repeat", "fallback:gemini_error"})


class QuoteUnavailable(ValueError):
    pass


def _day(now):
    if now.tzinfo is None:
        raise QuoteUnavailable("timezone_required")
    return now.astimezone(PACIFIC).date().isoformat()


def _text(value):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > MAX_QUOTE_CHARS
            or any((ord(c) < 32 and c != "\n") or 127 <= ord(c) <= 159 for c in value)):
        raise QuoteUnavailable("invalid_quote")
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise QuoteUnavailable("invalid_quote_cache")
    date.fromisoformat(value)
    return value


def _cache_path():
    # Fixed local path, not accepted by any action or message API.
    return Path(config.PROJECT_ROOT) / ".fitness_quotes" / "owner.json"


def _owner(owner):
    if not owner or not permissions.is_owner(owner):
        raise ValueError("owner_required")
    return hashlib.sha256(config.normalize_handle(owner).encode()).hexdigest()


def _private_stat(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())):
        raise QuoteUnavailable("invalid_quote_cache")


def _read(path, owner_key):
    if path.is_symlink() or path.parent.is_symlink():
        raise QuoteUnavailable("invalid_quote_cache")
    try:
        _private_stat(path.parent.stat(follow_symlinks=False), directory=True)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            _private_stat(os.fstat(handle.fileno()))
            raw = handle.read(MAX_CACHE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_CACHE_BYTES:
        raise QuoteUnavailable("invalid_quote_cache")
    try:
        state = json.loads(raw)
        if (not isinstance(state, dict) or set(state) != {"version", "owner", "current", "history"}
                or type(state["version"]) is not int or state["version"] != 1
                or state["owner"] != owner_key):
            raise ValueError
        current = state["current"]
        if not isinstance(current, dict) or set(current) != {"date", "text", "source", "hash"}:
            raise ValueError
        _date(current["date"])
        _text(current["text"])
        if current["source"] not in SOURCES or current["hash"] != morning_quotes._quote_hash(current["text"]):
            raise ValueError
        history = state["history"]
        if not isinstance(history, list) or len(history) > MAX_HISTORY:
            raise ValueError
        previous = ""
        for entry in history:
            if (not isinstance(entry, dict) or set(entry) != {"date", "hash"}
                    or not isinstance(entry["hash"], str) or not re.fullmatch(r"[0-9a-f]{12}", entry["hash"])
                    or not previous < _date(entry["date"]) < current["date"]):
                raise ValueError
            previous = entry["date"]
        return state
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise QuoteUnavailable("invalid_quote_cache") from None


@contextmanager
def _lock(path):
    # Runtime is macOS; fail closed rather than generate unlocked elsewhere.
    import fcntl
    if path.parent.is_symlink():
        raise QuoteUnavailable("invalid_quote_cache")
    path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    _private_stat(path.parent.stat(follow_symlinks=False), directory=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.parent / "owner.lock", flags, 0o600)
    try:
        _private_stat(os.fstat(fd))
        # A simultaneous native/Work request never starts another provider call.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QuoteUnavailable("quote_preparing") from None
        yield
    finally:
        os.close(fd)


def _write(path, state):
    raw = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(raw) > MAX_CACHE_BYTES:
        raise QuoteUnavailable("invalid_quote_cache")
    fd, temporary = tempfile.mkstemp(prefix="owner-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _rewrite_line(prompt, *, api_key, url, before_request):
    """Same configured Gemini route/accounting, with no budget-alert sends."""
    import requests
    from . import billing
    budget = billing.check_gemini_budget("fitness_quote", notify=False)
    if not budget.allowed:
        raise QuoteUnavailable("gemini_budget_unavailable")
    before_request()
    try:
        response = requests.post(url, params={"key": api_key},
                                 json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                                 timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception:
        # Provider exception URLs may contain the existing query-string key.
        raise QuoteUnavailable("gemini_request_failed") from None
    usage = data.get("usageMetadata", {})
    billing.log_gemini_usage(usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0),
                            usage.get("totalTokenCount", 0), "fitness_quote")
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _select(day_key, recent):
    # Imports existing configured provider only after the owner/cache gates.
    from . import tools
    calls = {"provider_called": False, "model_called": False}
    choice = {}

    def zenquote():
        calls["provider_called"] = True
        return _text(morning_quotes._fetch_zenquotes_quote())

    def before_model_request():
        calls["provider_called"] = calls["model_called"] = True

    def rewrite(prompt):
        return _text(_rewrite_line(prompt, api_key=tools.GEMINI_API_KEY,
                                  url=tools._GEMINI_URL, before_request=before_model_request))

    def record(selected_day, source, text):
        # A provider response crossing midnight must never be stored under the
        # following day's key, or under a requested future workout date.
        if selected_day != day_key:
            raise QuoteUnavailable("quote_date_changed")
        choice.update(date=selected_day, source=source, text=_text(text),
                      hash=morning_quotes._quote_hash(text))

    morning_quotes._get_inspirational_quote(
        gemini_api_key=tools.GEMINI_API_KEY, rewrite_fn=rewrite,
        recent_hashes_fn=lambda _day: recent, log_choice_fn=record,
        zenquotes_fn=zenquote,
    )
    if not choice:
        raise QuoteUnavailable("quote_unavailable")
    return choice, calls


def get_daily_quote(owner, *, now=None, generate=False):
    """Return today's cached line; only generate=True may call providers/write.

    `now` is internal testing/clock input, never a caller-supplied action field.
    Workout target dates are deliberately absent from this interface.
    """
    owner_key = _owner(owner)
    clock = now or datetime.now(timezone.utc)
    day_key = _day(clock)
    path = _cache_path()
    state = _read(path, owner_key)
    empty_calls = {"provider_called": False, "model_called": False}
    if state and state["current"]["date"] == day_key:
        return {**state["current"], **empty_calls, "cache_hit": True}
    if not generate:
        return None
    with _lock(path):
        state = _read(path, owner_key)
        if state and state["current"]["date"] == day_key:
            return {**state["current"], **empty_calls, "cache_hit": True}
        if state and state["current"]["date"] > day_key:
            raise QuoteUnavailable("quote_clock_reversed")
        history = list(state["history"]) if state else []
        if state:
            history.append({key: state["current"][key] for key in ("date", "hash")})
        history = history[-MAX_HISTORY:]
        recent = {entry["hash"] for entry in history}
        # Ten local fallbacks cannot avoid sixty days forever. Keep one eligible
        # oldest local fallback, so fallback exhaustion never repeats yesterday.
        local_hashes = [morning_quotes._quote_hash(q) for q in morning_quotes._FALLBACK_QUOTES]
        if set(local_hashes) <= recent:
            last_seen = {entry["hash"]: entry["date"] for entry in history}
            recent.remove(min(local_hashes, key=lambda h: last_seen[h]))
        choice, calls = _select(day_key, recent)
        if choice["date"] != day_key or choice["source"] not in SOURCES:
            raise QuoteUnavailable("invalid_quote")
        _text(choice["text"])
        if choice["hash"] != morning_quotes._quote_hash(choice["text"]):
            raise QuoteUnavailable("invalid_quote")
        if now is None and _day(datetime.now(timezone.utc)) != day_key:
            raise QuoteUnavailable("quote_date_changed")
        _write(path, {"version": 1, "owner": owner_key, "current": choice, "history": history})
        return {**choice, **calls, "cache_hit": False}
