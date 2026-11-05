"""Small text-normalization helpers for outbound bot copy and inbound noise."""

from __future__ import annotations

import re


_BANNED_PHRASE_REPLACEMENTS = (
    (re.compile(r"\bmy\s+g\b", re.IGNORECASE), "boss"),
)

_TAPBACK_TEXT_RE = re.compile(
    r"^\s*(?:"
    r"Liked|Disliked|Loved|Laughed\s+at|Emphasized|Questioned|"
    r"Removed\s+(?:a\s+)?(?:like|dislike|love|laugh|emphasis|question)|"
    r"Reacted\s+.+?\s+(?:to|from)"
    r")\s+(?:an?\s+)?(?:image|photo|picture|attachment|message|link|video|audio|"
    r"[\u201c\"].*?[\u201d\"])\s*\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def normalize_bot_text(text: str) -> str:
    """Normalize bot output without changing normal prose."""
    if not isinstance(text, str) or not text:
        return text

    normalized = text
    for pattern, replacement in _BANNED_PHRASE_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    return normalized.strip()


def is_imessage_reaction_text(text: str | None) -> bool:
    """Return True for iMessage Tapback/reaction text rows."""
    if not text:
        return False
    return bool(_TAPBACK_TEXT_RE.match(text.strip()))


def is_imessage_reaction(
    text: str | None,
    associated_message_type: object = None,
    associated_message_guid: object = None,
) -> bool:
    """Detect reaction rows from either schema metadata or rendered text."""
    try:
        assoc_type = int(associated_message_type or 0)
    except (TypeError, ValueError):
        assoc_type = 0
    if associated_message_guid and 2000 <= assoc_type < 4000:
        return True
    if associated_message_guid and is_imessage_reaction_text(text):
        return True
    return is_imessage_reaction_text(text)
