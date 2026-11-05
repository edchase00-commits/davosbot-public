"""Quiet, fixed-source Platinum Emerald release watch; never downloads game data."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile

import requests

HOME_URL = "https://platinumemerald.com/"
MANIFEST_URL = HOME_URL + "patch/patch.json"
_LEVEL = r"(?:full release|stable release|version\s+1\.0)"
_SUBJECT = rf"(?:(?:the|our|Platinum Emerald(?:'s)?)\s+)?{_LEVEL}(?:\s+of\s+Platinum Emerald)?"
_UNCERTAIN = re.compile(r"\b(?:not|never|no|unconfirmed|tentative|hopefully|hope|aim|target|expected|delayed|cancelled|canceled|if|whether|could|might|would|candidate|beta|alpha|demo)\b|\b\w+n['’]t\b", re.I)
_DATE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})\b", re.I)
_ANNOUNCEMENT = re.compile(rf"^{_SUBJECT}\s+(?:launches|releases|arrives|comes out|will (?:be released|launch|arrive)|is (?:scheduled|set) for|date\s*(?:is|:))\s+(?:on\s+)?({_DATE.pattern})[.!]?$", re.I)
_AVAILABLE = re.compile(rf"^(?:status\s*:\s*{_LEVEL}|{_SUBJECT}\s+(?:is (?:now )?available|is out(?: now)?|is (?:now )?released|has (?:now )?launched|has been released)(?: now)?)[.!]?$", re.I)


class WatchError(RuntimeError):
    """Bounded diagnostic, without network bodies, credentials or owner identity."""


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.active = []
        self.hidden = 0
        self.hidden_tags = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = (self.hidden or tag in {"script", "style", "template", "noscript"}
                  or "hidden" in attributes or attributes.get("aria-hidden", "").lower() == "true"
                  or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attributes.get("style", ""), re.I))
        if hidden and tag not in {"meta", "link", "br", "img", "input", "hr", "source", "wbr"}:
            self.hidden_tags.append(tag)
            self.hidden = len(self.hidden_tags)
        if not self.hidden and tag in {"p", "h1", "h2", "h3", "li", "title"}:
            self.active.append((tag, []))

    def handle_data(self, data):
        if not self.hidden:
            for _, parts in self.active:
                parts.append(data)

    def handle_endtag(self, tag):
        if tag in self.hidden_tags:
            index = len(self.hidden_tags) - 1 - self.hidden_tags[::-1].index(tag)
            del self.hidden_tags[index:]
            self.hidden = len(self.hidden_tags)
        for index in range(len(self.active) - 1, -1, -1):
            if self.active[index][0] == tag:
                _, parts = self.active.pop(index)
                self.blocks.append(" ".join("".join(parts).split()))
                break


def fetch_public(url: str, limit: int) -> str:
    if url not in {HOME_URL, MANIFEST_URL}:
        raise WatchError("unsupported_source")
    try:
        with requests.get(url, timeout=(5, 20), allow_redirects=False, stream=True,
                          headers={"User-Agent": "Davos-Platinum-Emerald-Watch/1.0"}) as response:
            if response.status_code != 200:
                raise WatchError("source_http_error")
            chunks, size = [], 0
            for chunk in response.iter_content(16384):
                size += len(chunk)
                if size > limit:
                    raise WatchError("source_too_large")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")
    except (requests.RequestException, UnicodeError) as exc:
        raise WatchError("source_unavailable") from exc


def inspect_release(html: str, manifest: dict, today: date) -> dict:
    """Require explicit official prose; a full4_release build name means nothing."""
    if not isinstance(manifest, dict):
        raise WatchError("invalid_manifest")
    output = manifest.get("output", {}) if isinstance(manifest, dict) else {}
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if (not isinstance(output, dict) or manifest.get("game") != "Platinum Emerald"
            or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version)
            or type(output.get("bytes")) is not int or not 0 < output["bytes"] <= 33554432
            or not isinstance(output.get("sha1"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{40}", output["sha1"])):
        raise WatchError("invalid_manifest")
    page = _Page()
    page.feed(html)
    if not any("platinum emerald" in line.lower() for line in page.blocks):
        raise WatchError("unrecognized_homepage")
    # Current disclaimers veto availability, but historical "preview has ended" does not.
    preview = any(re.search(
        r"(?:status\s*:\s*|now\s+(?:in\s+)?|still\s+(?:in\s+)?)(?:a\s+)?(?:development preview|early access|alpha|beta)\b"
        r"|^development preview$|^platinum emerald\s*[:\-]\s*development preview", line, re.I)
        for line in page.blocks)
    events = []
    release_claim = False
    for line in page.blocks:
        # Whole paragraphs retain negation and qualifiers across punctuation.
        if len(line) > 1000 or not re.search(_LEVEL, line, re.I) or _UNCERTAIN.search(line):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            announced = _ANNOUNCEMENT.fullmatch(sentence)
            if not announced:
                if _AVAILABLE.fullmatch(sentence):
                    release_claim = True
                    if not preview:
                        events.append({"key": "full-release", "kind": "release", "evidence": sentence})
                continue
            parsed = None
            for fmt in ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y"):
                try:
                    parsed = datetime.strptime(announced.group(1).title(), fmt).date()
                    break
                except ValueError:
                    pass
            if parsed and today <= parsed <= today + timedelta(days=730):
                events.append({"key": "date:" + parsed.isoformat(), "kind": "date",
                               "date": parsed.isoformat(), "evidence": sentence})
    events = list({event["key"]: event for event in events}.values())
    return {"status": "conflict" if preview and release_claim
            else "released" if any(e["kind"] == "release" for e in events)
            else "preview" if preview else "unconfirmed", "version": version,
            "output_sha1": output["sha1"].lower(), "events": events}


def observe(today: date) -> dict:
    html = fetch_public(HOME_URL, 1048576)
    try:
        manifest = json.loads(fetch_public(MANIFEST_URL, 262144))
    except ValueError as exc:
        raise WatchError("invalid_manifest_json") from exc
    return inspect_release(html, manifest, today)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema": 1, "events": {}}
    try:
        if path.is_symlink() or path.stat().st_size > 262144:
            raise ValueError
        state = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("schema") != 1
                or not isinstance(state.get("events"), dict) or len(state["events"]) > 200):
            raise ValueError
        for key, entry in state["events"].items():
            if (not isinstance(key, str) or not isinstance(entry, dict)
                    or entry.get("state") not in {"claimed", "queued", "ambiguous"}
                    or not isinstance(entry.get("message"), str)):
                raise ValueError
        return state
    except (OSError, ValueError) as exc:
        raise WatchError("state_needs_review") from exc


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def watch_lock(path: Path):
    # This standalone service runs on the Mini, never on Windows.
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WatchError("already_running") from exc
        yield


def process_observation(path: Path, observation: dict, now: datetime, queue, pending) -> str:
    """Called under the service lock; a persisted claim never authorizes a resend."""
    state = load_state(path)
    state["last_check"] = now.isoformat()
    state["observation"] = observation
    result = "needs_review" if observation["status"] in {"unconfirmed", "conflict"} else "unchanged"
    # Keep unresolved outcomes visible even if the website later removes the event.
    for previous in state["events"].values():
        if previous["state"] != "queued":
            if pending(previous["message"]):
                previous["state"] = "queued"
            else:
                result = "needs_review"
    for event in observation["events"]:
        key = event["key"]
        previous = state["events"].get(key)
        if previous:
            continue
        if len(state["events"]) >= 200:
            raise WatchError("state_needs_review")
        if event["kind"] == "date":
            message = f"Platinum Emerald announced a full-release date: {event['date']}. Official announcement: {HOME_URL}"
        else:
            message = f"Platinum Emerald's full release is available. Official version: {observation['version']}. {HOME_URL}"
        due = (now + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        entry = {"state": "claimed", "message": message, "due_utc": due,
                 "claimed_at": now.isoformat(), "evidence": event["evidence"]}
        state["events"][key] = entry
        save_state(path, state)  # Durable before the first possible side effect.
        try:
            if not pending(message):
                queue(message, due)
            entry["state"] = "queued" if pending(message) else "ambiguous"
        except Exception:
            entry["state"] = "ambiguous"
        if entry["state"] != "queued":
            result = "needs_review"
        elif result != "needs_review":
            result = "queued"
        save_state(path, state)
    save_state(path, state)
    return result


def native_reminders():
    from .config import OWNER_ID
    from .reminder_tools import _list_reminders, _set_reminder
    if not OWNER_ID:
        raise WatchError("owner_not_configured")
    owner = OWNER_ID
    def pending(message):
        for line in _list_reminders(owner).splitlines():
            if re.match(r"\d+\. ", line):
                for separator in (" \u2014 ", " \\u2014 "):
                    if separator in line and line.partition(separator)[2] == message:
                        return True
        return False
    return (lambda message, due: _set_reminder(message, due, originating_chat_id=owner),
            pending)
