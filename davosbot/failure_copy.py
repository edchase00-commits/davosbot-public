"""User-facing fallback copy for model/backend misses."""

import re


IMAGE_PROCESSING_FAILURE_REPLY = "I need another pass on that image. Send it again or ask me one specific thing."
TOOL_CHAT_FAILURE_REPLY = "That got tangled. Try again in a sec, or add 'no web search' if you want pure chat."
DIRECT_CHAT_FAILURE_REPLY = "I blanked for a second. Try again and I'll catch it."
UNEXPECTED_FAILURE_REPLY = "Something went sideways, but I'm still here. Try again in a sec."
IMAGE_SCAN_FAILURE_REPLY = "I couldn't read that image cleanly. Send it again or ask me one specific thing."
IMAGE_SCAN_MISSING_REPLY = (
    "I can read images, but I need an attached or recently buffered image in this chat. "
    "Send an image with `what's in this screenshot?` or `gpt scan image [ask]`."
)
ROAST_FALLBACK_REPLY = "Lmao. Swing harder, champ."


_TRANSIENT_ERROR_PREFIX = "__transient_error__:"
_BOT_DIRECTED_RIBBING_RE = re.compile(
    r"\b(?:you'?re|you are|ur|u r)\s+(?:a\s+)?(?:pussy|bitch|coward|soft|bum|trash)\b",
    re.IGNORECASE,
)


def harmless_roast_fallback(user_msg: str) -> str | None:
    """Tiny local fallback for bot-directed ribbing when model text comes back empty."""
    if _BOT_DIRECTED_RIBBING_RE.search(user_msg or ""):
        return ROAST_FALLBACK_REPLY
    return None


def humanize_transient_error(reply: str | None) -> str | None:
    """Convert transient backend sentinels into provider-neutral chat copy."""
    if not reply or not isinstance(reply, str) or not reply.startswith(_TRANSIENT_ERROR_PREFIX):
        return reply
    detail = reply.split(":", 1)[1].strip()
    if "503" in detail or "502" in detail or "504" in detail:
        return "The reply path hit traffic for a second. Try again in 30 seconds; your message wasn't lost."
    if "429" in detail:
        return "I hit a temporary throttle. Give it a minute and try again."
    if "Timeout" in detail or "Connection" in detail:
        return "That took too long, so I cut it off. Try again; should be quick."
    return "The reply path got weird for a second. Try again in a sec."


def image_scan_success_reply(message: str) -> str:
    return (message or "").strip() or "I can see the image, but I didn't get useful detail from it."


def image_scan_failure_reply(_reason: str | None = None) -> str:
    return IMAGE_SCAN_FAILURE_REPLY
