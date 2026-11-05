import json
import sqlite3
from dataclasses import dataclass

from .config import BOT_DB_PATH, normalize_handle
from .permissions import is_owner, redact_secret


IMAGE_ACCESS_POLICY_EVENT = "image_access_policy"
IMAGE_TOOL_NAMES = ("openai_image_scan", "openai_image_generation")
BASE_NON_OWNER_IMAGE_LIMIT = 5


@dataclass(frozen=True)
class ImageAccessStatus:
    sender: str
    used_today: int
    daily_limit: int | None
    revoked: bool = False
    extra_daily_limit: int = 0

    @property
    def is_uncapped(self) -> bool:
        return self.daily_limit is None

    @property
    def allowed(self) -> bool:
        if self.is_uncapped:
            return True
        if self.revoked:
            return False
        return self.used_today < int(self.daily_limit or 0)

    @property
    def remaining(self) -> int | None:
        if self.is_uncapped:
            return None
        return max(0, int(self.daily_limit or 0) - self.used_today)


def _read_policy(target: str, db_path: str = BOT_DB_PATH) -> tuple[bool, int]:
    handle = normalize_handle(target)
    revoked = False
    extra = 0
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT payload FROM bot_log WHERE event_type = ? ORDER BY id ASC",
            (IMAGE_ACCESS_POLICY_EVENT,),
        ).fetchall()
    except Exception:
        return True, 0
    finally:
        if conn is not None:
            conn.close()

    for (payload_raw,) in rows:
        try:
            payload = json.loads(payload_raw or "{}")
        except json.JSONDecodeError:
            continue
        if normalize_handle(payload.get("target", "")) != handle:
            continue
        action = (payload.get("action") or "").lower()
        if action == "revoke":
            revoked = True
        elif action == "allow":
            revoked = False
        elif action == "reset":
            revoked = False
            extra = 0
        elif action == "extend":
            revoked = False
            try:
                extra += int(payload.get("amount", 5) or 5)
            except (TypeError, ValueError):
                extra += 5
    return revoked, max(0, extra)


def image_uses_today(sender: str, db_path: str = BOT_DB_PATH) -> int:
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        placeholders = ",".join("?" for _ in IMAGE_TOOL_NAMES)
        row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM tool_usage
            WHERE sender = ?
              AND tool IN ({placeholders})
              AND date(ts) = date('now')
            """,
            (sender, *IMAGE_TOOL_NAMES),
        ).fetchone()
        return int(row[0] if row else 0)
    except Exception:
        return BASE_NON_OWNER_IMAGE_LIMIT
    finally:
        if conn is not None:
            conn.close()


def get_image_access_status(sender: str, db_path: str = BOT_DB_PATH) -> ImageAccessStatus:
    used = image_uses_today(sender, db_path=db_path)
    if is_owner(sender):
        return ImageAccessStatus(sender=sender, used_today=used, daily_limit=None)
    revoked, extra = _read_policy(sender, db_path=db_path)
    return ImageAccessStatus(
        sender=sender,
        used_today=used,
        daily_limit=BASE_NON_OWNER_IMAGE_LIMIT + extra,
        revoked=revoked,
        extra_daily_limit=extra,
    )


def image_access_denial(sender: str, db_path: str = BOT_DB_PATH) -> str | None:
    status = get_image_access_status(sender, db_path=db_path)
    if status.allowed:
        return None
    if status.revoked:
        return "Image access is turned off for you right now."
    return (
        f"Image limit reached ({status.used_today}/{status.daily_limit} today). "
        "the owner can extend you by 5 more."
    )


def record_image_access_policy(
    actor: str,
    target: str,
    action: str,
    amount: int = 0,
    db_path: str = BOT_DB_PATH,
) -> str:
    safe_action = (action or "").strip().lower()
    if safe_action not in {"revoke", "allow", "reset", "extend"}:
        raise ValueError("unknown image access action")
    target_handle = normalize_handle(target)
    if not target_handle:
        raise ValueError("missing target")
    payload = {
        "target": target_handle,
        "action": safe_action,
        "amount": int(amount or 0),
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
            (actor, IMAGE_ACCESS_POLICY_EVENT, json.dumps(payload, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()
    return target_handle


def format_image_access_status(target: str, db_path: str = BOT_DB_PATH) -> str:
    status = get_image_access_status(target, db_path=db_path)
    handle = redact_secret(normalize_handle(target) or target)
    if status.is_uncapped:
        return f"{handle}: uncapped owner image access. Used {status.used_today} today."
    state = "revoked" if status.revoked else "active"
    return (
        f"{handle}: {state}. Used {status.used_today}/{status.daily_limit} today. "
        f"Extra daily allowance: +{status.extra_daily_limit}."
    )
