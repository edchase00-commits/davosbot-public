import os
import re
import shutil
import sqlite3
import subprocess
import threading
import logging
import time
import uuid
from pathlib import Path
from .config import DB_PATH, OWNER_ID, normalize_handle
from .text_safety import is_imessage_reaction, normalize_bot_text
from .message_body import decode_attributed_body

logger = logging.getLogger(__name__)

_last_rowid: int | None = None
_message_columns_cache: set[str] | None = None
_last_messages_restart_at: float = 0.0
_last_messages_recovery_retry_at: float = 0.0
_last_osascript_started_at: float = 0.0
_osascript_send_lock = threading.Lock()
_MESSAGES_RESTART_COOLDOWN_SECONDS = 120.0
_APPLESCRIPT_SEND_TIMEOUT_SECONDS = 15
_APPLESCRIPT_MESSAGE_TIMEOUT_SECONDS = 5
_SLOW_APPLESCRIPT_SEND_SECONDS = 3.0
_MESSAGES_RECOVERY_PRE_OPEN_SLEEP_SECONDS = 4
_MESSAGES_RECOVERY_POST_OPEN_SLEEP_SECONDS = 4
_MESSAGES_BACKGROUND_RETRY_COOLDOWN_SECONDS = 15.0
_MESSAGE_SEND_VERIFY_TIMEOUT_SECONDS = 1.0
_MESSAGES_RECOVERY_KILL_COMMANDS = (
    (["killall", "Messages"], "killall Messages"),
    (["pkill", "-f", "Messages Assistant Extension"], "pkill Messages Assistant Extension"),
    (["pkill", "-f", "MessagesBlastDoorService"], "pkill MessagesBlastDoorService"),
    (["pkill", "-f", "IMDPersistenceAgent"], "pkill IMDPersistenceAgent"),
    (["pkill", "-f", "IMDMessageServicesAgent"], "pkill IMDMessageServicesAgent"),
    (["pkill", "-f", "imagent"], "pkill imagent"),
    (["pkill", "-f", "identityservicesd"], "pkill identityservicesd"),
)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


_APPLESCRIPT_MIN_SEND_INTERVAL_SECONDS = max(
    0.0,
    _float_env("APPLESCRIPT_MIN_SEND_INTERVAL_SECONDS", 0.25),
)


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_watermark(conn: sqlite3.Connection) -> None:
    global _last_rowid
    row = conn.execute("SELECT MAX(ROWID) as max_id FROM message").fetchone()
    _last_rowid = row["max_id"] or 0
    logger.info("Watermark initialized to ROWID %d", _last_rowid)


def _message_columns(conn: sqlite3.Connection) -> set[str]:
    global _message_columns_cache
    if _message_columns_cache is None:
        rows = conn.execute("PRAGMA table_info(message)").fetchall()
        _message_columns_cache = {str(row[1]) for row in rows}
    return _message_columns_cache


def _optional_message_column(conn: sqlite3.Connection, name: str, default: str) -> str:
    return f"m.{name}" if name in _message_columns(conn) else default


def _image_attachment_predicate(alias: str = "a") -> str:
    lower_name = f"lower({alias}.filename)"
    return (
        f"({alias}.mime_type LIKE 'image/%' OR "
        f"{lower_name} LIKE '%.png' OR {lower_name} LIKE '%.jpg' OR "
        f"{lower_name} LIKE '%.jpeg' OR {lower_name} LIKE '%.webp' OR "
        f"{lower_name} LIKE '%.heic' OR {lower_name} LIKE '%.heif')"
    )


def poll_new_messages() -> list[dict]:
    """Return new inbound messages since last poll."""
    global _last_rowid
    try:
        conn = _get_db()
        try:
            if _last_rowid is None:
                _init_watermark(conn)
                return []
            assoc_type = _optional_message_column(conn, "associated_message_type", "0")
            assoc_guid = _optional_message_column(conn, "associated_message_guid", "NULL")
            attributed_body = _optional_message_column(conn, "attributedBody", "NULL")
            image_predicate = _image_attachment_predicate("a")
            image_predicate_2 = _image_attachment_predicate("a2")

            rows = conn.execute(
                f"""
                SELECT
                    m.ROWID,
                    m.text,
                    {attributed_body} AS attributed_body,
                    m.is_from_me,
                    m.date,
                    h.id AS sender,
                    c.chat_identifier,
                    {assoc_type} AS associated_message_type,
                    {assoc_guid} AS associated_message_guid,
                    (SELECT a.filename FROM message_attachment_join maj
                     JOIN attachment a ON a.ROWID = maj.attachment_id
                     WHERE maj.message_id = m.ROWID
                       AND {image_predicate}
                     LIMIT 1) as image_path,
                    (SELECT a.mime_type FROM message_attachment_join maj
                     JOIN attachment a ON a.ROWID = maj.attachment_id
                     WHERE maj.message_id = m.ROWID
                       AND {image_predicate}
                     LIMIT 1) as image_mime
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE m.ROWID > ?
                  AND m.is_from_me = 0
                  AND (m.text IS NOT NULL OR {attributed_body} IS NOT NULL OR EXISTS (
                      SELECT 1 FROM message_attachment_join maj2
                      JOIN attachment a2 ON a2.ROWID = maj2.attachment_id
                      WHERE maj2.message_id = m.ROWID AND {image_predicate_2}
                  ))
                ORDER BY m.ROWID ASC
                """,
                (_last_rowid,),
            ).fetchall()

            if rows:
                _last_rowid = rows[-1]["ROWID"]

            results = []
            for r in rows:
                d = dict(r)
                body = d.pop("attributed_body", None)
                if not d.get("text") and body:
                    d["text"] = decode_attributed_body(body)
                    if d["text"] is None:
                        logger.warning("Unable to decode attributed text for message row %s", d.get("ROWID"))
                if is_imessage_reaction(
                    d.get("text"),
                    d.get("associated_message_type"),
                    d.get("associated_message_guid"),
                ):
                    logger.info("Ignoring iMessage reaction row %s", d.get("ROWID"))
                    continue
                if not d.get("text") and not d.get("image_path"):
                    continue
                if d.get("image_path"):
                    d["image_path"] = os.path.expanduser(d["image_path"])
                results.append(d)
        finally:
            conn.close()

        return results
    except Exception as e:
        logger.error("poll_new_messages error: %s", e)
        return []


def find_recent_image_attachment(chat_identifier: str, sender: str | None = None, limit: int = 12) -> str | None:
    """Return the latest readable inbound image in a chat, optionally scoped to one sender."""
    if not chat_identifier:
        return None
    try:
        conn = _get_db()
        try:
            image_predicate = _image_attachment_predicate("a")
            params: list[object] = [chat_identifier]
            sender_clause = ""
            if sender:
                normalized = normalize_handle(sender)
                sender_values = [sender]
                if normalized and normalized != sender:
                    sender_values.append(normalized)
                placeholders = ",".join("?" for _ in sender_values)
                sender_clause = f"AND h.id IN ({placeholders})"
                params.extend(sender_values)
            params.append(max(1, int(limit)))
            rows = conn.execute(
                f"""
                SELECT a.filename
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                JOIN message_attachment_join maj ON maj.message_id = m.ROWID
                JOIN attachment a ON a.ROWID = maj.attachment_id
                WHERE c.chat_identifier = ?
                  AND m.is_from_me = 0
                  AND {image_predicate}
                  {sender_clause}
                ORDER BY m.ROWID DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            filename = row["filename"] if isinstance(row, sqlite3.Row) else row[0]
            if not filename:
                continue
            path = os.path.expanduser(str(filename))
            if os.path.exists(path):
                return path
        return None
    except Exception as e:
        logger.warning("find_recent_image_attachment failed: %s", e)
        return None


def is_owner_in_chat(chat_identifier: str, owner_id: str) -> bool:
    try:
        conn = _get_db()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM chat_handle_join chj
                JOIN chat c ON c.ROWID = chj.chat_id
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE c.chat_identifier = ? AND h.id = ?
                """,
                (chat_identifier, normalize_handle(owner_id)),
            ).fetchone()
        finally:
            conn.close()
        return row[0] > 0
    except Exception as e:
        logger.error("is_owner_in_chat error: %s", e)
        return False


def _applescript_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _looks_like_messages_bridge_failure(stderr: str) -> bool:
    lower = (stderr or "").lower()
    return any(
        marker in lower
        for marker in (
            "appleevent timed out",
            "appleevent handler failed",
            "event timed out",
            "connection invalid",
            "hiservices-xpcservice",
            "(-1712)",
            "(-10000)",
        )
    )


def _run_osascript(script: str, timeout_seconds: float) -> subprocess.CompletedProcess:
    """Serialize Messages AppleScript sends so macOS can release bridge resources."""
    global _last_osascript_started_at
    with _osascript_send_lock:
        now = time.monotonic()
        wait_seconds = _APPLESCRIPT_MIN_SEND_INTERVAL_SECONDS - (now - _last_osascript_started_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _last_osascript_started_at = time.monotonic()
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )


def _hard_relaunch_messages(reason: str) -> bool:
    global _last_messages_restart_at
    now = time.time()
    if now - _last_messages_restart_at < _MESSAGES_RESTART_COOLDOWN_SECONDS:
        logger.warning("Messages relaunch skipped during cooldown after %s", reason)
        return False
    _last_messages_restart_at = now
    logger.warning("Hard relaunching Messages after AppleScript failure: %s", reason)
    ok = True
    for command, label in _MESSAGES_RECOVERY_KILL_COMMANDS:
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=5)
        except Exception as exc:
            ok = False
            logger.error("%s failed during recovery: %s", label, exc)
    time.sleep(_MESSAGES_RECOVERY_PRE_OPEN_SLEEP_SECONDS)
    try:
        result = subprocess.run(["open", "-a", "Messages"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            ok = False
            logger.error("open -a Messages failed during recovery: %s", result.stderr.strip())
    except Exception as exc:
        ok = False
        logger.error("open -a Messages failed during recovery: %s", exc)
    time.sleep(_MESSAGES_RECOVERY_POST_OPEN_SLEEP_SECONDS)
    return ok


def _applescript_recovery_retry_worker(
    script: str,
    *,
    label: str,
    timeout_seconds: float,
    success_probe: object = None,
) -> None:
    if callable(success_probe) and success_probe():
        logger.warning("AppleScript send for %s verified before async recovery", label)
        return

    _hard_relaunch_messages(f"{label} async recovery")
    if callable(success_probe) and success_probe():
        logger.warning("AppleScript send for %s verified after async recovery", label)
        return

    started = time.monotonic()
    try:
        result = _run_osascript(script, timeout_seconds)
    except subprocess.TimeoutExpired:
        logger.error("Background AppleScript retry timed out for %s after %.2fs", label, time.monotonic() - started)
        return
    except Exception as exc:
        logger.error("Background AppleScript retry error for %s: %s", label, exc)
        return

    elapsed = time.monotonic() - started
    if result.returncode != 0:
        logger.error("Background AppleScript retry failed for %s: %s", label, result.stderr.strip())
        return
    if elapsed >= _SLOW_APPLESCRIPT_SEND_SECONDS:
        logger.warning("Background AppleScript retry slow for %s: %.2fs", label, elapsed)
    if callable(success_probe) and not success_probe():
        logger.error("Background AppleScript retry did not verify as sent for %s", label)


def _schedule_applescript_recovery_retry(
    script: str,
    *,
    label: str,
    timeout_seconds: float,
    success_probe: object = None,
) -> bool:
    global _last_messages_recovery_retry_at
    now = time.time()
    if now - _last_messages_recovery_retry_at < _MESSAGES_BACKGROUND_RETRY_COOLDOWN_SECONDS:
        logger.warning("Messages async recovery retry skipped during cooldown after %s", label)
        return False
    _last_messages_recovery_retry_at = now
    thread = threading.Thread(
        target=_applescript_recovery_retry_worker,
        kwargs={
            "script": script,
            "label": label,
            "timeout_seconds": timeout_seconds,
            "success_probe": success_probe,
        },
        name="davosbot-imessage-recovery",
        daemon=True,
    )
    thread.start()
    logger.warning("Scheduled async Messages recovery retry for %s", label)
    return True


def _run_applescript_with_recovery(
    script: str,
    *,
    label: str,
    timeout_seconds: float = _APPLESCRIPT_SEND_TIMEOUT_SECONDS,
    success_probe: object = None,
    recovery_mode: str = "inline",
) -> bool:
    """Run AppleScript with optional recovery.

    `inline` blocks for relaunch+retry and is used when callers need a truthful
    boolean. `background` keeps interactive replies from wedging the poll loop.
    """
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            result = _run_osascript(script, timeout_seconds)
        except subprocess.TimeoutExpired:
            logger.error("AppleScript timed out for %s after %.2fs", label, time.monotonic() - started)
            if callable(success_probe) and success_probe():
                logger.warning("AppleScript timed out for %s, but Messages DB verified the send", label)
                return True
            if recovery_mode == "background" and attempt == 1:
                _schedule_applescript_recovery_retry(
                    script,
                    label=label,
                    timeout_seconds=timeout_seconds,
                    success_probe=success_probe,
                )
                return False
            if recovery_mode == "none":
                return False
            if attempt == 1 and _hard_relaunch_messages(f"{label} timeout"):
                continue
            return False
        except Exception as exc:
            logger.error("AppleScript send error for %s: %s", label, exc)
            return False

        if result.returncode == 0:
            elapsed = time.monotonic() - started
            if elapsed >= _SLOW_APPLESCRIPT_SEND_SECONDS:
                logger.warning("AppleScript send slow for %s: %.2fs attempt=%d", label, elapsed, attempt)
            return True

        stderr = result.stderr.strip()
        logger.error("AppleScript error for %s: %s", label, stderr)
        if callable(success_probe) and _looks_like_messages_bridge_failure(stderr) and success_probe():
            logger.warning("AppleScript errored for %s, but Messages DB verified the send", label)
            return True
        if recovery_mode == "background" and attempt == 1 and _looks_like_messages_bridge_failure(stderr):
            _schedule_applescript_recovery_retry(
                script,
                label=label,
                timeout_seconds=timeout_seconds,
                success_probe=success_probe,
            )
            return False
        if recovery_mode == "none":
            return False
        if attempt == 1 and _looks_like_messages_bridge_failure(stderr) and _hard_relaunch_messages(label):
            continue
        return False
    return False


def _attachment_cache_root() -> Path:
    return Path.home() / "Library" / "Messages" / "Attachments" / "DavosBotSendCache"


def _safe_attachment_name(path: Path) -> str:
    name = path.name or "davosbot_attachment"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "davosbot_attachment"
    if path.suffix and not name.lower().endswith(path.suffix.lower()):
        name = f"{name}{path.suffix}"
    return name


def _stage_outbound_attachment(file_path: str) -> Path:
    source = Path(os.path.expanduser(file_path)).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))

    target_dir = _attachment_cache_root() / uuid.uuid4().hex
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_attachment_name(source)
    shutil.copy2(source, target)
    return target


def _latest_message_rowid() -> int:
    conn = _get_db()
    try:
        row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


def _display_attachment_path(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _verify_file_send(after_rowid: int, staged_path: Path, timeout_seconds: float = 12.0) -> bool:
    absolute = str(staged_path)
    display = _display_attachment_path(staged_path)
    deadline = time.time() + timeout_seconds
    last_rows: list[sqlite3.Row] = []

    while time.time() < deadline:
        try:
            conn = _get_db()
            try:
                rows = conn.execute(
                    """
                    SELECT
                        m.ROWID,
                        m.is_sent,
                        m.is_delivered,
                        m.error,
                        a.transfer_state,
                        a.filename
                    FROM message m
                    JOIN message_attachment_join maj ON maj.message_id = m.ROWID
                    JOIN attachment a ON a.ROWID = maj.attachment_id
                    WHERE m.ROWID > ?
                      AND m.is_from_me = 1
                      AND (a.filename = ? OR a.filename = ?)
                    ORDER BY m.ROWID DESC
                    LIMIT 3
                    """,
                    (after_rowid, absolute, display),
                ).fetchall()
            finally:
                conn.close()
            if rows:
                last_rows = rows
                for row in rows:
                    if int(row["error"] or 0) == 0 and int(row["is_sent"] or 0) == 1:
                        return True
                    if int(row["error"] or 0) != 0 or int(row["transfer_state"] or 0) == 6:
                        break
        except Exception as e:
            logger.error("verify file send error: %s", e)
            return False
        time.sleep(0.5)

    if last_rows:
        states = [
            {
                "rowid": row["ROWID"],
                "is_sent": row["is_sent"],
                "is_delivered": row["is_delivered"],
                "error": row["error"],
                "transfer_state": row["transfer_state"],
            }
            for row in last_rows
        ]
        logger.error("AppleScript file send did not verify as sent: %s", states)
    else:
        logger.error("AppleScript file send produced no attachment row for staged file")
    return False


def _verify_message_send(after_rowid: int, recipient: str, is_group: bool = False, timeout_seconds: float = 2.0) -> bool:
    """Best-effort confirmation for text sends after AppleScript bridge hangs."""
    if after_rowid <= 0:
        return False
    raw_recipient = recipient or ""
    normalized_recipient = normalize_handle(raw_recipient)
    deadline = time.time() + timeout_seconds
    last_rows: list[sqlite3.Row] = []

    while time.time() < deadline:
        try:
            conn = _get_db()
            try:
                if is_group:
                    rows = conn.execute(
                        """
                        SELECT m.ROWID, m.is_sent, m.error, c.chat_identifier
                        FROM message m
                        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                        JOIN chat c ON c.ROWID = cmj.chat_id
                        WHERE m.ROWID > ?
                          AND m.is_from_me = 1
                          AND (c.chat_identifier = ? OR c.chat_identifier LIKE ?)
                        ORDER BY m.ROWID DESC
                        LIMIT 3
                        """,
                        (after_rowid, raw_recipient, f"%{raw_recipient}"),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT m.ROWID, m.is_sent, m.error, c.chat_identifier
                        FROM message m
                        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                        JOIN chat c ON c.ROWID = cmj.chat_id
                        WHERE m.ROWID > ?
                          AND m.is_from_me = 1
                          AND c.chat_identifier IN (?, ?)
                        ORDER BY m.ROWID DESC
                        LIMIT 3
                        """,
                        (after_rowid, raw_recipient, normalized_recipient),
                    ).fetchall()
            finally:
                conn.close()
            if rows:
                last_rows = rows
                for row in rows:
                    if int(row["error"] or 0) == 0 and int(row["is_sent"] or 0) == 1:
                        return True
                    if int(row["error"] or 0) != 0:
                        break
        except Exception as e:
            logger.error("verify message send error: %s", e)
            return False
        time.sleep(0.25)

    if last_rows:
        states = [
            {"rowid": row["ROWID"], "is_sent": row["is_sent"], "error": row["error"]}
            for row in last_rows
        ]
        logger.error("AppleScript message send did not verify as sent: %s", states)
    return False


def send_file(recipient: str, file_path: str, is_group: bool = False, *, recovery_mode: str = "inline") -> bool:
    """Send a file via iMessage using AppleScript."""
    if recovery_mode not in {"inline", "none"}:
        return False
    if not recipient or not isinstance(recipient, str):
        logger.error("send_file: invalid recipient %r", recipient)
        return False
    try:
        staged_path = _stage_outbound_attachment(file_path)
    except Exception as e:
        logger.error("send_file: could not stage attachment: %s", e)
        return False

    abs_path = _applescript_quote(str(staged_path))
    before_rowid = _latest_message_rowid()
    safe_recipient = _applescript_quote(recipient)
    if is_group:
        script = f"""
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set foundChat to missing value
    repeat with theChat in (every chat of targetService)
        if (id of theChat) ends with "{safe_recipient}" then
            set foundChat to theChat
            exit repeat
        end if
    end repeat
    if foundChat is missing value then error "no chat with id ending in {safe_recipient}"
    set imageFile to POSIX file "{abs_path}" as alias
    send imageFile to foundChat
end tell
"""
    else:
        script = f"""
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{safe_recipient}" of targetService
    set imageFile to POSIX file "{abs_path}" as alias
    send imageFile to targetBuddy
end tell
"""
    verified = False
    def success_probe():
        nonlocal verified
        verified = _verify_file_send(before_rowid, staged_path)
        return verified
    # Work receipts own retry policy. A timed-out submission may have happened:
    # inspect its staged attachment, but never run the AppleScript a second time.
    recovery = ({"recovery_mode": "none", "success_probe": success_probe}
                if recovery_mode == "none" else {})
    if not _run_applescript_with_recovery(script, label=f"file send to {recipient}", **recovery):
        return False
    if verified:
        return True
    return _verify_file_send(before_rowid, staged_path)


def send_message(recipient: str, text: str, is_group: bool = False, recovery_mode: str = "background") -> bool:
    """Send an iMessage via AppleScript. Use is_group=True for group chats.

    For GCs, AppleScript's chat `id` is `iMessage;+;chat<32hex>` — match by `ends with`
    on the bare GUID so we don't accidentally fire to a different chat that happens to
    contain the substring. If no chat matches, AppleScript raises an error so we return
    False instead of silently succeeding.
    """
    if not recipient or not isinstance(recipient, str):
        logger.error("send_message: invalid recipient %r", recipient)
        return False
    try:
        before_rowid = _latest_message_rowid()
    except Exception as exc:
        before_rowid = 0
        logger.warning("send_message: could not read Messages DB before send: %s", exc)
    safe_outbound = normalize_bot_text(text or "")
    safe_text = safe_outbound.replace("\\", "\\\\").replace('"', '\\"')
    if is_group:
        script = f"""
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set foundChat to missing value
    repeat with theChat in (every chat of targetService)
        if (id of theChat) ends with "{recipient}" then
            set foundChat to theChat
            exit repeat
        end if
    end repeat
    if foundChat is missing value then error "no chat with id ending in {recipient}"
    send "{safe_text}" to foundChat
end tell
"""
    else:
        script = f"""
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{recipient}" of targetService
    send "{safe_text}" to targetBuddy
end tell
"""
    return _run_applescript_with_recovery(
        script,
        label=f"message send to {recipient} (group={is_group})",
        timeout_seconds=_APPLESCRIPT_MESSAGE_TIMEOUT_SECONDS,
        success_probe=lambda: _verify_message_send(
            before_rowid,
            recipient,
            is_group=is_group,
            timeout_seconds=_MESSAGE_SEND_VERIFY_TIMEOUT_SECONDS,
        ),
        recovery_mode=recovery_mode,
    )
