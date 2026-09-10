"""Recognize narrow final lookup placeholders, never authorize a new search."""

import re


NO_LOOKUP_RESULT = (
    "I don't have a current lookup result to report from this turn. "
    "That reply did not start a background search or a later update."
)

_TEXT_TASK = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you)\s+)*(?:"
    r"translate|quote|explain|role[ -]?play|pretend|imagine|write|draft|compose|rewrite)\b", re.I,
)
_SEARCH_REQUEST = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you)\s+)*(?:google|search|look\s+up)\b", re.I,
)
_LOOKUP_QUESTION = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you)\s+)*(?:"
    r"check|find|tell|give|what|when|where|who|is|are|has|have|did|do\s+you\s+know)\b", re.I,
)
_CURRENT_SUBJECT = re.compile(
    r"\b(?:today|tonight|current|latest|live|tracking|delivered|deliver(?:y|ies)|"
    r"packages?|shipments?|forecast|weather|odds|scores?)\b", re.I,
)
_USER_CONDITION = re.compile(
    r"\b(?:(?:if|once|after|when)\s+you|could|would|can\s+(?:check|search|look))\b", re.I,
)
_RESULT_CUE = re.compile(
    r"https?://|\b(?:found|shows?|according|reported|arrived|delivered|"
    r"status\s+is|forecast\s+is|results?\s+(?:are|is)|no\s+results|"
    r"means|requires|isn't|is\s+not|doesn't|does\s+not|not)\b|[;:]", re.I,
)
_ACKNOWLEDGMENT = re.compile(
    r"(?:okay|ok|sure|on it|hang tight|one moment|just a moment|hold on|stand by)"
    r"[.!]*", re.I,
)
_LOOKUP_PROMISE = re.compile(
    r"(?:(?:I'll|I\s+will|let\s+me)\s+"
    r"(?:check|search|google|look\s+(?:up|into|for))"
    r"|(?:(?:I'm|I\s+am)\s+)?(?:checking|searching|looking\s+(?:up|into|for)))"
    r"(?:\s+[^.!?\n]{0,240})?[.!]*", re.I,
)


def is_terminal_lookup_placeholder(user_msg: str, reply: str | None) -> bool:
    """True only for a short result-free lookup promise in a real lookup ask.

    Call at a terminal model return, not for native progress notifications.
    Substantive reports, quoted/creative requests, and conditional future plans
    remain untouched. This intentionally does not recognize every paraphrase.
    """
    prompt = (user_msg or "").strip()
    if not reply or _TEXT_TASK.search(prompt):
        return False
    if not (_SEARCH_REQUEST.search(prompt) or (
        _LOOKUP_QUESTION.search(prompt) and _CURRENT_SUBJECT.search(prompt)
    )):
        return False
    text = reply.replace("\u2019", "'").strip()
    if len(text) > 360 or any(mark in text for mark in ('"', "\u201c", "\u201d", "`", "?")):
        return False
    if _USER_CONDITION.search(text) or _RESULT_CUE.search(text):
        return False
    sentences = [part.strip() for part in re.split(r"(?<=[.!])\s+|\n+", text) if part.strip()]
    return bool(sentences and any(_LOOKUP_PROMISE.fullmatch(part) for part in sentences)
                and all(_LOOKUP_PROMISE.fullmatch(part) or _ACKNOWLEDGMENT.fullmatch(part)
                        for part in sentences))
