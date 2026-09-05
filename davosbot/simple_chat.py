import re

from .failure_copy import DIRECT_CHAT_FAILURE_REPLY


SHORT_CHAT_ONLY_RE = re.compile(
    r"^\s*(?:"
    r"hi|hey|yo|sup|what'?s\s+up|wyd|what\s+(?:are|r)\s+(?:you|u)\s+doing|"
    r"ping|are\s+you\s+alive|you\s+there|still\s+there|you\s+good|"
    r"lol|lmao|haha|thanks|thank\s+you|thx|ok|okay|"
    r"nice|cool|bet|gm|gn|good\s+morning|good\s+night|how\s+are\s+you|you\s+up"
    r"|welcome\s+back"
    r")\s*[.!?]*\s*$",
    re.IGNORECASE,
)
FAST_CHAT_REPLY_RE = re.compile(
    r"^\s*ping\s*[.!?]*\s*$",
    re.IGNORECASE,
)
FAST_LOCAL_CHAT_RE = re.compile(
    r"^\s*(?:"
    r"hi|hey|yo+|sup|what'?s\s+up|what'?s\s+good|wass+up|wyd|what\s+(?:are|r)\s+(?:you|u)\s+doing|"
    r"are\s+you\s+alive|you\s+there|still\s+there|you\s+good|"
    r"we\s+missed\s+you|i\s+missed\s+you|miss\s+you|welcome\s+back|"
    r"thanks|thank\s+you|thx|ok|okay|nice|cool|bet|gm|gn|good\s+morning|good\s+night|"
    r"how\s+(?:is|was)\s+(?:ur|your)\s+day(?:\s+(?:going|today|bro|dude|man|davos|lc)){0,3}|"
    r"how\s+(?:are\s+you|you)\s+doing(?:\s+(?:now|today|bro|dude|man|davos|lc)){0,3}|"
    r"(?:are\s+)?you\s+ready\s+for\s+(?:tmw|tomorrow)|lol|lmao|haha"
    r")\s*[.!?]*\s*$",
    re.IGNORECASE,
)
FAST_LOOSE_CASUAL_RE = re.compile(
    r"\b(?:wass+up|what'?s\s+(?:up|good))\b",
    re.IGNORECASE,
)
_GREETING_FILLER_RE = re.compile(
    r"^(?:(?:yo+|hey|hi|bro|dude|man|davos|lc)[\s,.!?]*)*$", re.IGNORECASE,
)
_TONE_ONLY_RE = re.compile(
    r"^(?:davos\s+)?(?:why\s+(?:are\s+)?you\s+(?:so\s+)?(?:weird|robotic|bland)(?:\s+all\s+of\s+a\s+sudden)?|"
    r"(?:can\s+you\s+)?(?:go\s+back\s+to\s+being\s+a\s+chiller|be\s+(?:less\s+(?:weird|robotic)|more\s+chill)))\s*[.!?]*$",
    re.IGNORECASE,
)
FAST_CHAT_TASK_RE = re.compile(
    r"\b(?:remind(?:er|ers)?|cron|schedule|weather|model|routing|fallback|github|"
    r"expense|report|ticket|seatgeek|search|look\s+up|send|text|dm|message|email|"
    r"image|photo|screenshot|workout|log|memory|website|app|build|fix)\b",
    re.IGNORECASE,
)
WHATS_GOOD_REPLIES = (
    "Yo. What's up?",
    "What's good?",
    "I'm here. What's up?",
)
WEIRD_REPLIES = (
    "Fair. I'll keep it simple.",
    "Got it. Less weird.",
)
CHILLER_REPLIES = (
    "Got you. I'll keep it loose.",
    "Say less. What's up?",
)
FAST_DAY_REPLIES = (
    "Good. You?",
    "Solid. What about you?",
    "All good. You?",
)
FAST_READY_REPLIES = (
    "Yeah. What's on deck?",
    "Ready. What do we need?",
    "Yep. What's next?",
)
SIMPLE_CHAT_DEFAULT_REPLIES = (
    "Yo. What's up?",
    "I'm here. What's up?",
    "What's up?",
    "All good. What's the move?",
)
BLAND_SIMPLE_CHAT_RE = re.compile(
    r"^\s*(?:"
    r"i'?m\s+(?:here|alive|on|back)|"
    r"still\s+here|"
    r"here|"
    r"alive|"
    r"present"
    r")\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _normalize_chat_text(text: str) -> str:
    normalized = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", normalized.strip())


def stable_reply_choice(text: str, replies: tuple[str, ...]) -> str:
    return replies[sum(ord(ch) for ch in (text or "")) % len(replies)]


def looks_like_plain_chat(
    text: str,
    *,
    live_info_re: re.Pattern | None = None,
    side_effect_re: re.Pattern | None = None,
) -> bool:
    """True for short conversational texts that do not need the tool loop."""
    s = (text or "").strip()
    if not s:
        return True
    if SHORT_CHAT_ONLY_RE.match(s):
        return True
    if (live_info_re and live_info_re.search(s)) or (side_effect_re and side_effect_re.search(s)):
        return False
    words = re.findall(r"[A-Za-z0-9']+", s)
    if len(s) <= 80 and 0 < len(words) <= 8:
        return True
    return False


def fast_chat_reply(text: str) -> str | None:
    """Instant replies for trivial chat that should not pay model latency."""
    clean = _normalize_chat_text(text)
    lower = clean.lower().strip(".!? ")
    if not clean:
        return None
    if lower == "ping":
        return "pong."
    if FAST_CHAT_TASK_RE.search(clean):
        return None
    if FAST_LOCAL_CHAT_RE.match(clean):
        return simple_chat_personality_fallback(clean)
    words = re.findall(r"[A-Za-z0-9']+", clean)
    # A greeting followed by an actual subject needs the conversation/model.
    # "what's up with my invoice" is not equivalent to "what's up bro".
    remainder = FAST_LOOSE_CASUAL_RE.sub("", clean).strip()
    if len(words) <= 8 and FAST_LOOSE_CASUAL_RE.search(clean) and _GREETING_FILLER_RE.fullmatch(remainder):
        return simple_chat_personality_fallback(clean)
    return None


def simple_chat_personality_fallback(user_msg: str) -> str:
    """In-character fallback for tiny chat when the fast local model blanks."""
    clean = _normalize_chat_text(user_msg)
    lower = clean.lower()
    if re.search(r"\b(?:wass?up|what'?s\s+(?:up|good))\b", lower):
        return stable_reply_choice(lower, WHATS_GOOD_REPLIES)
    if re.search(r"\bhow\s+(?:is|was)\s+(?:ur|your)\s+day\b|\bhow\s+(?:are\s+you|you)\s+doing\b", lower):
        return stable_reply_choice(lower, FAST_DAY_REPLIES)
    if re.search(r"\b(?:are\s+)?you\s+ready\s+for\s+(?:tmw|tomorrow)\b", lower):
        return stable_reply_choice(lower, FAST_READY_REPLIES)
    if re.search(r"\bchiller\b", lower):
        return stable_reply_choice(lower, CHILLER_REPLIES)
    if re.search(r"\b(?:weird|robotic|lame|bland|personality)\b", lower):
        return stable_reply_choice(lower, WEIRD_REPLIES)
    if re.search(r"\b(?:wyd|what\s+(?:are|r)\s+(?:you|u)\s+doing)\b", lower):
        return "Here. What's up?"
    if re.search(r"\bwelcome\s+back\b|\bback\s+(?:online|on)\b", lower):
        return "There we go. What did I miss?"
    if re.search(r"\b(?:we\s+)?missed\s+you\b|\bi\s+missed\s+you\b|\bmiss\s+you\b", lower):
        return "Missed you too. What's up?"
    if re.search(r"\b(?:are\s+you\s+alive|you\s+there|still\s+there|you\s+good)\b", lower):
        return "Alive. What's up?"
    if re.search(r"\b(?:thanks|thank\s+you|thx)\b", lower):
        return "Got you."
    if re.search(r"\b(?:slow|lag|latency|taking\s+forever)\b", lower):
        return "Here. What's up?"
    if re.search(r"\b(?:lol|lmao|haha)\b", lower) and len(clean) <= 80:
        return "Lmao."
    return stable_reply_choice(lower, SIMPLE_CHAT_DEFAULT_REPLIES)


def simple_chat_empty_fallback(user_msg: str) -> str:
    clean = re.sub(r"\s+", " ", (user_msg or "").strip())
    exact_word = re.search(r"\bexactly\s+one\s+word\s*:\s*([A-Za-z0-9_-]+)", clean, re.IGNORECASE)
    if exact_word:
        return exact_word.group(1)
    casual = re.sub(r"^(?:lol|lmao|haha)\s+", "", clean, flags=re.IGNORECASE)
    if fast_chat_reply(casual) is not None or _TONE_ONLY_RE.fullmatch(clean):
        return simple_chat_personality_fallback(casual)
    return DIRECT_CHAT_FAILURE_REPLY


def polish_simple_chat_reply(user_msg: str, reply: str | None) -> str | None:
    if reply and BLAND_SIMPLE_CHAT_RE.match(reply):
        casual = fast_chat_reply(user_msg)
        # Never replace a substantive answer with an unrelated greeting.
        return casual if casual is not None else reply
    return reply


def history_limit(user_msg: str, plain_chat_limit: int = 2) -> int:
    """Keep enough turns for short followups; small talk retains its fast budget."""
    casual = re.sub(r"^(?:lol|lmao|haha)\s+", "", _normalize_chat_text(user_msg), flags=re.IGNORECASE)
    return plain_chat_limit if fast_chat_reply(casual) is not None else max(plain_chat_limit, 12)
