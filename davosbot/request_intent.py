"""Small request distinctions shared by deterministic command entry points."""

import re


def is_log_analysis_request(text: str) -> bool:
    """Distinguish a log being read from an instruction to log a bot repair."""
    # Inspect the request header, not arbitrary contents of the supplied log.
    header = re.split(r"[\n:]", text or "", maxsplit=1)[0].strip()
    if not re.match(
        r"^(?:(?:please|pls|can you|could you|would you|davos)\s+)*"
        r"(?:analy[sz]e|read|review|check|inspect|scan|look\s+at)\b",
        header, re.I,
    ):
        return False
    if re.search(r"\b(?:and|then)\s+(?:please\s+)?(?:log|record|capture|fix|repair|debug|ship)\b", header, re.I):
        return False
    return bool(re.search(r"\blogs?\b", header, re.I))
