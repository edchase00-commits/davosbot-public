"""Shared, side-effect-free distinction between banter and literal requests."""

import re


_ROAST_REQUEST_RE = re.compile(
    r"\b(?:roast|cook|flame|drag|clown|shit\s*talk|talk\s+shit|make\s+fun\s+of)\b",
    re.IGNORECASE,
)
_ROAST_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|no|without|stop|avoid)\s+(?:(?:want|need)\s+)?(?:(?:a|the|any)\s+)?"
    r"(?:roast(?:ing)?|cook(?:ing)?|flam(?:e|ing)|drag(?:ging)?|clown(?:ing)?|"
    r"insults?|jokes?|shit\s*talk(?:ing)?|talk(?:ing)?\s+shit|mak(?:e|ing)\s+fun\s+of)\b",
    re.IGNORECASE,
)
_LITERAL_USE_RE = re.compile(
    r"(?:roast|cook)\s+(?:(?:a|an|the|some|these|those|my)\s+)?"
    r"(?:(?:fresh|frozen|whole|raw|chopped|sliced|seasoned|boneless|skinless)\s+){0,3}"
    r"(?:chicken|turkey|beef|pork|lamb|duck|ham|vegetables?|potatoes?|wings?|"
    r"dinner|lunch|breakfast|meals?|pasta|rice|eggs?|steak|food|fish|salmon|"
    r"trout|cod|tuna|shrimp|tofu|broccoli|cauliflower|asparagus|brussels\s+sprouts|"
    r"carrots?|squash|garlic)\b"
    r"|cook\s+(?:me|us|him|her|them|(?:my|our|the)\s+(?:friend|family|guests?))\s+"
    r"(?:(?:a|some|the)\s+)?(?:dinner|lunch|breakfast|meals?)\b"
    r"|drag\s+(?:(?:a|an|the|this|that|my)\s+)?(?:file|folder|icon|window|slider)\b"
    r"|drag\s+and\s+drop\b"
    r"|drag\s+\S+\.(?:csv|txt|pdf|png|jpg|zip)\b"
    r"|flame\s+(?:on|from|of|is|keeps|went|goes)\b",
    re.IGNORECASE,
)


def is_roast_request(text: str) -> bool:
    """Require a nonliteral roast phrase and respect an explicit declined style.

    Check literal uses at each match, so cooking in an explanation does not
    cancel a separate request to roast a friend. Normalize phone apostrophes
    for detection only; callers retain the user's original prompt.
    """
    normalized = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    if _ROAST_NEGATION_RE.search(normalized):
        return False
    return any(
        not _LITERAL_USE_RE.match(normalized, match.start())
        for match in _ROAST_REQUEST_RE.finditer(normalized)
    )
