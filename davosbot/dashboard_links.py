"""Recognize requests for the configured Fourth Down link, not access changes."""

import re


_TARGET = r"(?:(?:fantasy\s+)?dashboards?|fourth\s*down(?:\s+dashboard)?)"
_REQUEST = re.compile(
    rf"(?:{_TARGET}(?:\s+links?)?|"
    rf"(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    rf"(?:link\s+(?:the\s+)?{_TARGET}|"
    rf"(?:show|send|give)\s+(?:me\s+)?(?:the\s+)?(?:links?\s+(?:to|for)\s+)?{_TARGET}(?:\s+links?)?|"
    rf"(?:what(?:'s|\s+is)|where(?:'s|\s+is))\s+(?:the\s+)?(?:link\s+(?:to|for)\s+)?{_TARGET}(?:\s+link)?))"
    rf"(?:\s+please)?[.!?]*",
    re.IGNORECASE,
)


def is_dashboard_link_request(text: str) -> bool:
    clean = re.sub(r"\s+", " ", (text or "").strip()).replace("\u2019", "'")
    return bool(_REQUEST.fullmatch(clean))


_FANTASY_COMMAND = re.compile(
    r"fantasy(?:\s+(?:help|requests|request\s+list|access(?:\s+list)?|users|members|"
    r"(?:grant|promote|role)\s+#?\d+\s+(?:viewer|editor|owner)|revoke\s+#?\d+))?",
    re.IGNORECASE,
)
_LEGACY_GROUP_REQUEST = re.compile(
    r"fantasy\s+request(?:\s+[^\s@]+@[^\s@]+\.[^\s@]+)?", re.IGNORECASE,
)


def is_fantasy_command(text: str, *, is_group: bool = False) -> bool:
    """Recognize existing complete commands, not general fantasy discussion.

    This selects routing only. Existing DM/group handlers retain all access
    checks and the group sign-in guidance never changes dashboard access.
    """
    clean = re.sub(r"\s+", " ", (text or "").strip())
    return bool(_FANTASY_COMMAND.fullmatch(clean) or (
        is_group and _LEGACY_GROUP_REQUEST.fullmatch(clean)
    ))
