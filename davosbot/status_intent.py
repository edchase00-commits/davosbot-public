"""Bounded read-only aliases for native status commands."""

import re


def is_billing_status_request(text: str) -> bool:
    clean = re.sub(r"\s+", " ", (text or "").replace("’", "'").strip()).lower()
    return bool(re.fullmatch(
        r"(?:(?:please|pls)\s+)?(?:"
        r"(?:what(?:'s| is)|show(?: me)?|check|tell me)\s+(?:my|our|the bot's|davos(?:bot)?'s)\s+"
        r"(?:gemini|api|bot)\s+(?:spend|spending|usage|bill|billing|costs?)"
        r"|how much\s+(?:have i|has davos(?:bot)?|has the bot)\s+(?:spent|used)\s+(?:on\s+)?gemini"
        r")(?:\s+(?:today|so far|overall))?\s*[.!?]*", clean,
    ))
