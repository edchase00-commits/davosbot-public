"""Deterministic fantasy roster persistence for DavosBot.

Handles: "save my roster" detection, text roster parsing,
atomic writes to roster.json, read-back validation.

Schema: {"players": ["Name", ...], ...other metadata keys preserved...}
The hourly briefing (davos-hourly/briefing.py) reads data["players"].
"""
import json
import re
import tempfile
from pathlib import Path

from .permissions import is_owner

ROSTER_PATH = Path("/Users/<mac-user>/davos-hourly/roster.json")

SAVE_PATTERNS = [
    r"save\s+(my\s+)?roster",
    r"remember\s+(my\s+)?roster",
    r"update\s+(my\s+)?roster",
    r"here(?:'s| is)\s+(my\s+)?roster",
]

CLEAR_PATTERNS = [
    r"clear\s+(my\s+)?roster",
    r"delete\s+(my\s+)?roster",
    r"reset\s+(my\s+)?roster",
]

SHOW_PATTERNS = [
    r"(show|display|view|list)\s+(my\s+)?roster",
    r"(what'?s|what is)\s+(my\s+)?roster",
    r"^(my\s+)?roster\s*[?!]?$",
]

# Leading junk on a roster line: whitespace, punctuation (colons left behind
# by "save my roster: ..."), bullets, and numbered-list prefixes like "1.".
_LEAD_JUNK = re.compile(r"^[\s:;\-–—•>\"'“”‘’()\[\]{}]*(\d+[\.\)])?[\s:;\-–—•>\"'“”‘’()\[\]{}]*")

_SKIP_WORDS = {"roster", "my", "team", "here", "players", "fantasy"}


def is_roster_save_request(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in SAVE_PATTERNS)


def is_roster_clear_request(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in CLEAR_PATTERNS)


def is_roster_show_request(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in SHOW_PATTERNS)


def parse_roster_text(text: str) -> list[str]:
    for p in SAVE_PATTERNS:
        text = re.sub(p, "", text, flags=re.IGNORECASE)
    # Strip the ":" (and friends) left behind by "save my roster: Mahomes, ..."
    text = _LEAD_JUNK.sub("", text)
    parts = re.split(r"[,;\n]+", text)
    players = []
    for part in parts:
        p = _LEAD_JUNK.sub("", part.strip()).strip()
        # Drop filler words ("my", "team", "roster") inside an entry, so
        # "save my roster: my team, Mahomes" doesn't save "my team".
        p = " ".join(w for w in p.split() if w.lower() not in _SKIP_WORDS).strip()
        p = _LEAD_JUNK.sub("", p).strip()
        if len(p) >= 2:
            players.append(p)
    return players


def load_roster() -> list[str]:
    try:
        with open(ROSTER_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    raw = data.get("players", []) if isinstance(data, dict) else []
    names = []
    for entry in raw:
        # Tolerate dict-shaped entries: {"name": "Mahomes", "status": "QUES"}
        name = entry.get("name", "") if isinstance(entry, dict) else entry
        name = str(name).strip()
        if name:
            names.append(name)
    return names


def save_roster(players: list[str]) -> tuple[bool, str]:
    if not players:
        return False, "No players found to save."
    try:
        existing: dict = {}
        try:
            with open(ROSTER_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            existing = {}
        # Preserve any existing metadata keys (e.g. "team", "statuses");
        # only the player list is replaced.
        data = dict(existing)
        data["players"] = players
        ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(ROSTER_PATH.parent),
            delete=False, suffix=".tmp", encoding="utf-8",
        ) as f:
            json.dump(data, f, indent=2)
            temp_path = f.name
        with open(temp_path, encoding="utf-8") as f:
            validated = json.load(f)
        if validated.get("players") != players:
            Path(temp_path).unlink(missing_ok=True)
            return False, "Validation failed after write."
        Path(temp_path).replace(ROSTER_PATH)
        return True, f"Saved {len(players)} players to your roster."
    except Exception as e:
        return False, f"Failed to save roster: {e}"


def clear_roster() -> tuple[bool, str]:
    try:
        if ROSTER_PATH.exists():
            ROSTER_PATH.unlink()
        return True, "Roster cleared."
    except Exception as e:
        return False, f"Failed to clear: {e}"


def handle_roster_message(sender: str, text: str) -> str | None:
    """Owner-only roster command dispatch. Returns a reply or None."""
    if not is_owner(sender):
        return None
    if is_roster_clear_request(text):
        ok, msg = clear_roster()
        return ("✅ " if ok else "⚠️ ") + msg
    if is_roster_save_request(text):
        players = parse_roster_text(text)
        if not players:
            return (
                "I couldn't find any player names in that message. "
                "Try: save my roster: Mahomes, Barkley, Jefferson"
            )
        ok, msg = save_roster(players)
        if ok:
            return f"✅ Saved {len(players)} players: {', '.join(players)}"
        return f"⚠️ {msg}"
    if is_roster_show_request(text):
        players = load_roster()
        if not players:
            return 'Your roster is empty. Send "save my roster: ..." to set it.'
        lines = [f"{i + 1}. {p}" for i, p in enumerate(players)]
        return "Your roster:\n" + "\n".join(lines)
    return None
