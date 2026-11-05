import re
from .config import OWNER_ID, BOT_DB_PATH, normalize_handle, ADMIN_PASSWORD
from .db import connect_bot_db

_PW_PATTERNS = re.compile(
    r"(?:password|pw)\s*[:=]\s*(\S+)|^(\S+)$",
    re.IGNORECASE,
)


def check_admin_password(text: str) -> bool:
    """Return True if text contains the correct ADMIN_PASSWORD.

    Accepts three forms:
      "password: hunter2"   "pw: hunter2"   or just "hunter2" (bare)
    Case-insensitive, strips whitespace. Always False if ADMIN_PASSWORD not set.
    """
    if not ADMIN_PASSWORD:
        return False
    needle = ADMIN_PASSWORD.lower()
    haystack = (text or "").strip().lower()
    # Exact bare match
    if haystack == needle:
        return True
    # "password: X" or "pw: X" anywhere in the message
    for m in _PW_PATTERNS.finditer(haystack):
        candidate = (m.group(1) or m.group(2) or "").strip()
        if candidate == needle:
            return True
    # Plain substring match — password appears verbatim anywhere in message
    return needle in haystack.split()


def strip_password(text: str) -> str:
    """Remove the password token from a message so it never reaches the LLM."""
    if not ADMIN_PASSWORD:
        return text
    pw = ADMIN_PASSWORD
    # Remove "password: X", "pw: X", or bare password word
    cleaned = re.sub(
        r"\b(?:password|pw)\s*[:=]\s*" + re.escape(pw) + r"\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b" + re.escape(pw) + r"\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def redact_secret(text: str) -> str:
    """Replace ADMIN_PASSWORD anywhere in text with [redacted]. Use BEFORE logging
    or storing user text so the password never lands in PM2 logs / DB / MEMORY.md."""
    if not text:
        return ""
    redacted = text
    if ADMIN_PASSWORD:
        redacted = re.sub(re.escape(ADMIN_PASSWORD), "[redacted]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"([?&](?:key|api_key|token|access_token)=)[^&\s)]+", r"\1[redacted]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[redacted-api-key]", redacted)
    redacted = re.sub(r"github_pat_[0-9A-Za-z_]+", "[redacted-github-token]", redacted)
    redacted = re.sub(r"gh[opusr]_[0-9A-Za-z_]+", "[redacted-github-token]", redacted)
    redacted = re.sub(r"sk-[0-9A-Za-z_-]{20,}", "[redacted-openai-key]", redacted)
    redacted = re.sub(r"https://[^/\s:@]+:[^@\s]+@", "https://[redacted]@", redacted)
    return redacted

# Actions only the owner (the owner) can perform. Admins are explicitly blocked.
OWNER_ONLY_ACTIONS = [
    # Existing operational gates
    "modify_soul",
    "change_personality",
    "schedule_cron",
    "grant_admin",
    "revoke_admin",
    "view_audit_log",
    # Bot management commands
    "deploy",           # pull + restart
    "view_logs",        # pm2 logs / pm2 status
    "view_billing",     # Gemini usage + cost
    "manage_memory",    # memory wipe / add / clear
    "view_chats",       # list enabled group chats
    "view_changelog",   # change log (log command)
    "view_session",     # !status / !uptime (DB session info)
    "view_personalities",  # !personalities
    "view_backups",     # !backups
    "manage_ratelimit", # !ratelimit
    "manage_image_access",
    "manage_owner_alerts",
]

# Actions that require at least admin. Regular friends are blocked from these.
ADMIN_ALLOWED_ACTIONS = [
    "create_skill",
    "list_bets",
    "settle_bet",
    "view_missing_capabilities",
    "send_contact_card",
]


def is_owner(sender: str) -> bool:
    if not OWNER_ID:
        return False
    return normalize_handle(sender) == OWNER_ID


def is_admin(sender: str) -> bool:
    """True if sender is the owner OR has an active (non-revoked) admin record."""
    if is_owner(sender):
        return True
    handle = normalize_handle(sender)
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            row = conn.execute(
                "SELECT 1 FROM admins WHERE handle = ? AND revoked_at IS NULL",
                (handle,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def can_user_do(sender: str, action: str) -> bool:
    """Three-tier permission check: owner > admin > friend.

    Owner   ? everything.
    Admin   ? everything except OWNER_ONLY_ACTIONS.
    Friend  ? everything except OWNER_ONLY_ACTIONS and ADMIN_ALLOWED_ACTIONS.
    """
    if is_owner(sender):
        return True
    if action in OWNER_ONLY_ACTIONS:
        return False
    if is_admin(sender):
        return True
    return action not in ADMIN_ALLOWED_ACTIONS
