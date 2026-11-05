"""Read-only replies to narrow identity/access questions, never authentication."""

import re

from .request_context import verified_actor

_IDENTITY = re.compile(
    r"(?:whoami|who am i|(?:do you know|can you tell me) who i am|do you recognize me"
    r"|do you know my name|am i (?:owner|(?:the |your )?owner))", re.I,
)
_ACCESS = re.compile(
    r"(?:mypermissions|what(?:'s| is) my (?:access|permission)(?: level)?"
    r"|what (?:access|permissions) do i have|am i (?:an? )?(?:admin|approved friend))", re.I,
)
_CRON_CAPABILITY = re.compile(
    r"(?:can i|am i (?:allowed|able) to|do i have permission to) "
    r"(?:create|schedule|set up) (?:(?:a |an? new |new )?)"
    r"(?:crons?|cron jobs?|recurring jobs?)", re.I,
)


def query_kind(text: str) -> str | None:
    """Whole-query only: actions, quotes, roleplay and image asks stay elsewhere."""
    if not isinstance(text, str) or len(text) > 180:
        return None
    clean = re.sub(r"\s+", " ", text.replace("\u2019", "'").strip())
    clean = re.sub(r"[?!.]+$", "", clean).strip()
    for kind, pattern in (("identity", _IDENTITY), ("access", _ACCESS), ("cron", _CRON_CAPABILITY)):
        if pattern.fullmatch(clean):
            return kind
    return None


def reply(text: str, sender: str) -> str | None:
    kind = query_kind(text)
    if kind is None:
        return None
    role = verified_actor(sender)
    if role == "owner (the owner)":
        identity = "I recognize you as the owner, the configured owner. Your owner access is active."
        capability = "You can create supported recurring jobs. Send what you want delivered and when."
    elif role == "admin":
        identity = "You're recognized as an admin, not the configured owner."
        capability = (
            "You can create or update a sports-recap cron in the current chat. "
            "Other recurring jobs require the owner's owner access."
        )
    elif role == "approved friend":
        identity = "You're recognized as an approved friend, not the configured owner."
        capability = "Your current access does not allow creating recurring jobs. the owner has owner access."
    else:
        return None
    if kind == "access" and role != "owner (the owner)":
        # Preserve the existing informational command's concrete action list.
        from .commands import _cmd_mypermissions
        return _cmd_mypermissions(sender)
    if kind == "cron":
        return identity + " " + capability + " This status check has not created a job."
    return identity
