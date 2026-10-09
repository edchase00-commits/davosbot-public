"""Owner-only delivery receipts from a private, data-only GitHub inbox.

There is no remote command or recipient field. Failed/ambiguous Messages sends
are reconciled against exact text and recipient metadata before any retry.
The durable attempt record reduces duplicates; it cannot promise exactly once.
"""

from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from .message_body import decode_attributed_body
from .text_safety import normalize_bot_text

REPOSITORY = "example/davosbot"
DATA_BRANCH = "davos/package-delivery-data"
INBOX_PATH = "package-delivery/inbox.json"
STATUS_PATH = "package-delivery/status.json"
POLL_SECONDS = 120
HEARTBEAT_SECONDS = 900
RETRY_SECONDS = 900
MAX_SEND_ATTEMPTS = 3
SENT_RETENTION_SECONDS = 180 * 86400
MAX_INBOX_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 2_097_152
MAX_STATE_BYTES = 4_194_304
HEX = re.compile(r"[0-9a-f]{64}\Z")
EVENT_FIELDS = {"event_id", "shipment_key", "status", "merchant", "item",
                "tracking_number", "carrier", "delivered_at", "delivery_location", "source"}
SOURCE_FIELDS = {"provider", "message_id", "sender", "confirmation"}
logger = logging.getLogger(__name__)
_start_lock = threading.Lock()
_thread: threading.Thread | None = None


class DeliveryError(Exception):
    """Only a fixed error code crosses logging/heartbeat boundaries."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise DeliveryError("invalid_timestamp") from None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _string(value, maximum, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or
            (not empty and not value.strip()) or
            any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise DeliveryError("invalid_string")
    return value


def _json(raw, maximum):
    if len(raw) > maximum:
        raise DeliveryError("input_too_large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DeliveryError("duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise DeliveryError("invalid_json") from None


def validate_inbox(raw, now):
    inbox = _json(raw, MAX_INBOX_BYTES)
    required = {"schema_version", "enabled", "activated_at", "watchlist", "events"}
    if (not isinstance(inbox, dict) or not required <= set(inbox) or
            set(inbox) - required - {"last_mail_scan_at"} or
            type(inbox["schema_version"]) is not int or inbox["schema_version"] != 1 or
            type(inbox["enabled"]) is not bool):
        raise DeliveryError("invalid_inbox_schema")
    activated = _timestamp(inbox["activated_at"])
    if activated > now + 300:
        raise DeliveryError("future_activation")
    if "last_mail_scan_at" in inbox and _timestamp(inbox["last_mail_scan_at"]) > now + 300:
        raise DeliveryError("future_mail_scan")
    for name in ("watchlist", "events"):
        if not isinstance(inbox[name], list) or len(inbox[name]) > 500:
            raise DeliveryError("invalid_list")
    if any(not isinstance(item, dict) for item in inbox["watchlist"]):
        raise DeliveryError("invalid_watchlist")
    seen = set()
    for event in inbox["events"]:
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise DeliveryError("invalid_event_schema")
        key = _string(event["shipment_key"], 250)
        tracking = _string(event["tracking_number"], 50, empty=True)
        if key.startswith("tracking:"):
            if not re.fullmatch(r"[A-Z0-9]{1,50}", tracking) or key != "tracking:" + tracking:
                raise DeliveryError("invalid_shipment_key")
        elif not re.fullmatch(r"order:[a-z0-9][a-z0-9.-]{0,99}:[A-Za-z0-9_-]{1,60}:[A-Za-z0-9_-]{1,60}", key) or tracking:
            raise DeliveryError("invalid_shipment_key")
        event_id = _string(event["event_id"], 64)
        if not HEX.fullmatch(event_id) or event_id != hashlib.sha256(key.encode()).hexdigest():
            raise DeliveryError("invalid_event_hash")
        if event_id in seen:
            raise DeliveryError("duplicate_event")
        seen.add(event_id)
        if event["status"] != "delivered":
            raise DeliveryError("not_delivered")
        delivered = _timestamp(event["delivered_at"])
        if not activated <= delivered <= now + 300:
            raise DeliveryError("delivery_outside_window")
        for name, maximum in (("merchant", 100), ("item", 200), ("carrier", 50), ("delivery_location", 200)):
            _string(event[name], maximum, empty=name == "delivery_location")
        source = event["source"]
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS or source["provider"] != "gmail":
            raise DeliveryError("invalid_source")
        if not re.fullmatch(r"[0-9a-f]{1,64}", _string(source["message_id"], 64)):
            raise DeliveryError("invalid_message_id")
        if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", _string(source["sender"], 254)):
            raise DeliveryError("invalid_sender")
        confirmation = _string(source["confirmation"], 300).replace("\u2019", "'")
        if (not re.search(r"\bdelivered\b", confirmation, re.I) or
                re.search(r"\b(out for delivery|to be delivered|can be delivered|not|no|never|"
                          r"cannot|can't|couldn't|wasn't|hasn't|haven't|isn't|won't|wouldn't|didn't|"
                          r"will|would|should|could|may|might|probably|possibly|if|unless|whether|"
                          r"expected|estimated|expect|scheduled|anticipated|attempt|attempted|"
                          r"pending|awaiting|delay|delayed|undeliverable|unable|failed)\b", confirmation, re.I)):
            raise DeliveryError("unconfirmed_delivery")
    return inbox


def alert_text(event):
    text = f"Delivered: {event['item']} ({event['merchant']})."
    if event["delivery_location"]:
        text += f" Left at: {event['delivery_location']}."
    if event["tracking_number"]:
        identifier = f"{event['carrier']}: {event['tracking_number']}"
    else:
        _, _, order, shipment = event["shipment_key"].split(":")
        identifier = f"Order {order}, shipment {shipment}"
    # Package identity and delivery time make the exact text deterministic.
    return normalize_bot_text(text + f"\n{identifier}\nConfirmed: {event['delivered_at']}")


class GitHubBridge:
    """Fixed endpoints and branch; authenticated gh, never a shell command."""

    def __init__(self, root):
        self.root = Path(root)
        self.executable = shutil.which("gh")
        if not self.executable:
            self.executable = next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh") if Path(p).is_file()), None)

    def _call(self, arguments, payload=None):
        if not self.executable:
            raise DeliveryError("gh_unavailable")
        command = [self.executable, "api", "--hostname", "github.com", *arguments]
        with tempfile.TemporaryFile() as input_file:
            if payload is not None:
                input_file.write(_canonical(payload))
                input_file.seek(0)
                command += ["--input", "-"]
            try:
                with subprocess.Popen(command, cwd=self.root, stdin=input_file,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      shell=False) as process:
                    deadline, output = time.monotonic() + 30, bytearray()
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            while selector.get_map():
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise DeliveryError("gh_timeout")
                                for key, _ in selector.select(min(remaining, 1)):
                                    chunk = os.read(key.fd, 65536)
                                    if not chunk:
                                        selector.unregister(key.fileobj)
                                    output.extend(chunk)
                                    if len(output) > MAX_OUTPUT_BYTES:
                                        raise DeliveryError("gh_output_too_large")
                        if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                            raise DeliveryError("gh_request_failed")
                        return bytes(output)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
            except subprocess.TimeoutExpired:
                raise DeliveryError("gh_timeout") from None
            except OSError:
                raise DeliveryError("gh_unavailable") from None

    def assert_private(self):
        metadata = _json(self._call([f"repos/{REPOSITORY}", "--method", "GET"]), MAX_OUTPUT_BYTES)
        if not isinstance(metadata, dict) or metadata.get("private") is not True or metadata.get("full_name", "").lower() != REPOSITORY.lower():
            raise DeliveryError("repository_not_private")

    def read_inbox(self):
        self.assert_private()
        return self._call([f"repos/{REPOSITORY}/contents/{INBOX_PATH}?ref={DATA_BRANCH}",
                           "--method", "GET", "-H", "Accept: application/vnd.github.raw+json"])

    def publish_status(self, status):
        self.assert_private()
        endpoint = f"repos/{REPOSITORY}/contents/{STATUS_PATH}"
        current = _json(self._call([endpoint + f"?ref={DATA_BRANCH}", "--method", "GET"]), MAX_OUTPUT_BYTES)
        sha = current.get("sha") if isinstance(current, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise DeliveryError("status_sha_missing")
        self._call([endpoint, "--method", "PUT"], {"message": "Update delivery monitor status",
                   "branch": DATA_BRANCH, "sha": sha,
                   "content": base64.b64encode(_canonical(status)).decode()})


def messages_status(db_path, owner, text, normalize, after_rowid=None):
    """Return sent/pending/failed/absent/unknown plus the current DB watermark."""
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            watermark = db.execute("SELECT COALESCE(MAX(ROWID),0) FROM message").fetchone()[0]
            chats = [row[0] for row in db.execute("SELECT ROWID,chat_identifier FROM chat")
                     if normalize(row[1] or "") == normalize(owner)]
            if not chats:
                return "absent", watermark
            columns = {row[1] for row in db.execute("PRAGMA table_info(message)")}
            body = "m.attributedBody" if "attributedBody" in columns else "NULL"
            placeholders = ",".join("?" for _ in chats)
            rows = db.execute(
                f"SELECT m.text,{body},m.is_sent,m.error FROM message m "
                "JOIN chat_message_join cmj ON cmj.message_id=m.ROWID "
                f"WHERE cmj.chat_id IN ({placeholders}) AND m.is_from_me=1 "
                f"AND (m.text=? OR (m.ROWID>? AND m.text IS NULL AND {body} IS NOT NULL)) "
                "ORDER BY m.ROWID DESC LIMIT 1001",
                [*chats, text, watermark if after_rowid is None else after_rowid]).fetchall()
            if len(rows) > 1000:
                return "unknown", watermark
            states = []
            for row_text, attributed, sent, error in rows:
                if row_text is None:
                    row_text = decode_attributed_body(attributed)
                    if row_text is None:
                        states.append("unknown")
                if row_text != text:
                    continue
                states.append("sent" if sent == 1 and error == 0 else
                              "failed" if isinstance(error, int) and error != 0 else "pending")
            for state in ("sent", "pending", "unknown", "failed"):
                if state in states:
                    return state, watermark
            return "absent", watermark
    except (sqlite3.Error, OSError, ValueError, TypeError):
        raise DeliveryError("messages_db_unreadable") from None


@contextmanager
def _state_lock(root):
    import fcntl
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise DeliveryError("unsafe_state_path")
    fd = os.open(root / "lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeliveryError("monitor_busy") from None
        yield
    finally:
        os.close(fd)


def _load_state(path):
    if path.is_symlink():
        raise DeliveryError("unsafe_state_path")
    if not path.exists():
        return {"schema_version": 1, "events": {}}
    try:
        with path.open("rb") as handle:
            state = _json(handle.read(MAX_STATE_BYTES + 1), MAX_STATE_BYTES)
        if (not isinstance(state, dict) or set(state) != {"schema_version", "events"} or
                type(state["schema_version"]) is not int or state["schema_version"] != 1 or
                not isinstance(state["events"], dict) or len(state["events"]) > 10000):
            raise ValueError
        required = {"payload_hash", "state", "attempt_at", "attempts", "watermark", "sent_at"}
        for event_id, record in state["events"].items():
            if (not HEX.fullmatch(event_id) or not isinstance(record, dict) or set(record) != required or
                    not isinstance(record["payload_hash"], str) or not HEX.fullmatch(record["payload_hash"]) or
                    record["state"] not in {"ready", "attempting", "pending", "failed", "unknown", "review", "sent"} or
                    any(type(record[k]) is not int or record[k] < 0 for k in ("attempts", "watermark")) or
                    any(type(record[k]) not in (int, float) or not math.isfinite(record[k]) or record[k] < 0 for k in ("attempt_at", "sent_at")) or
                    (record["state"] == "sent" and not record["sent_at"])):
                raise ValueError
        return state
    except (ValueError, TypeError, OSError, DeliveryError):
        raise DeliveryError("state_corrupt") from None


def _sync_state_directory(path):
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _save_state(path, state):
    payload = _canonical(state)
    if len(payload) > MAX_STATE_BYTES or len(state["events"]) > 10000:
        raise DeliveryError("state_capacity")
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_state_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class DeliveryMonitor:
    def __init__(self, root, owner, db_path, sender, normalize, *, bridge=None, clock=time.time, revision="unknown"):
        self.root, self.owner, self.db_path = Path(root) / ".package_delivery", normalize(owner), db_path
        self.sender, self.normalize, self.clock = sender, normalize, clock
        self.bridge = bridge if bridge is not None else GitHubBridge(root)
        self.revision = revision if re.fullmatch(r"[0-9a-f]{40,64}", revision) else "unknown"
        self.last_heartbeat, self.last_successful_poll = 0, 0

    def poll(self):
        now, errors, state, changed = self.clock(), [], {"events": {}}, False
        status = "error"
        try:
            with _state_lock(self.root):
                state = _load_state(self.root / "state.json")
                inbox = validate_inbox(self.bridge.read_inbox(), now)
                inbox_ids = {event["event_id"] for event in inbox["events"]}
                expired = [key for key, rec in state["events"].items()
                           if key not in inbox_ids and rec["state"] == "sent" and
                           now - rec["sent_at"] > SENT_RETENTION_SECONDS]
                for key in expired:
                    del state["events"][key]
                if expired:
                    _save_state(self.root / "state.json", state)
                    changed = True
                self.last_successful_poll = now
                status = "active" if inbox["enabled"] else "disabled"
                if inbox["enabled"] and not self.owner:
                    raise DeliveryError("owner_unconfigured")
                if inbox["enabled"]:
                    scan = inbox.get("last_mail_scan_at")
                    if not scan:
                        status = "awaiting_mail_scan"
                    elif now - _timestamp(scan) > 10800:
                        status = "source_stale"
                    sent_this_poll = 0
                    for event in inbox["events"]:
                        event_id = event["event_id"]
                        digest = hashlib.sha256(_canonical(event)).hexdigest()
                        record = state["events"].get(event_id)
                        previous_record = dict(record) if record is not None else None
                        if record and record["payload_hash"] != digest:
                            errors.append("immutable_event_changed")
                            continue
                        if record and record["state"] == "sent":
                            continue
                        if sent_this_poll >= 10:
                            continue
                        if record is None:
                            record = {"payload_hash": digest, "state": "ready", "attempt_at": 0,
                                      "attempts": 0, "watermark": 0, "sent_at": 0}
                            state["events"][event_id] = record
                        text = alert_text(event)
                        found, watermark = messages_status(self.db_path, self.owner, text, self.normalize,
                                                           record["watermark"] if record["attempts"] else None)
                        if found == "sent":
                            record.update(state="sent", sent_at=now)
                        elif found in {"pending", "unknown"}:
                            record["state"] = found
                        elif record["attempts"] >= MAX_SEND_ATTEMPTS:
                            record["state"] = "review"
                            errors.append("send_attempts_exhausted")
                        elif record["attempts"] and now - record["attempt_at"] < RETRY_SECONDS:
                            continue
                        elif record["state"] == "unknown" and found == "absent":
                            # A successful/raised send with no row remains ambiguous.
                            continue
                        else:
                            record.update(state="attempting", attempt_at=now, attempts=record["attempts"] + 1)
                            if record["attempts"] == 1:
                                record["watermark"] = watermark
                            _save_state(self.root / "state.json", state)
                            sent_this_poll += 1
                            result = None
                            try:
                                result = self.sender(self.owner, text, is_group=False, recovery_mode="none")
                            except Exception:
                                pass  # An exception may still have queued a message.
                            found, _ = messages_status(self.db_path, self.owner, text, self.normalize, record["watermark"])
                            if found == "sent":
                                record.update(state="sent", sent_at=now)
                            elif found == "absent":
                                record["state"] = "failed" if result is False else "unknown"
                            else:
                                record["state"] = found
                            if record["state"] == "failed" and record["attempts"] >= MAX_SEND_ATTEMPTS:
                                record["state"] = "review"
                                errors.append("send_attempts_exhausted")
                        if record != previous_record:
                            changed = True
                            _save_state(self.root / "state.json", state)
                if errors:
                    status = "error"
        except DeliveryError as exc:
            errors.append(str(exc))
        except Exception:
            errors.append("monitor_internal_error")
        records = state["events"]
        heartbeat = {"schema_version": 1, "runtime_revision": self.revision, "checked_at": _iso(now),
                     "last_successful_poll": _iso(self.last_successful_poll) if self.last_successful_poll else None,
                     "state": "error" if errors else status, "errors": sorted(set(errors)),
                     "counts": {"tracked": len(records), "sent": sum(r["state"] == "sent" for r in records.values())},
                     "events": [{"event_id": key, "state": rec["state"], "sent_at": _iso(rec["sent_at"]) if rec["sent_at"] else None}
                                for key, rec in list(records.items())[-500:]]}
        if errors == ["monitor_busy"]:
            return heartbeat  # Another process owns both the attempt and its receipt.
        if changed or not self.last_heartbeat or now - self.last_heartbeat >= HEARTBEAT_SECONDS:
            try:
                self.bridge.publish_status(heartbeat)
                self.last_heartbeat = now
            except Exception:
                logger.warning("Package delivery monitor: heartbeat_publish_failed")
        if errors:
            logger.warning("Package delivery monitor: %s", ",".join(sorted(set(errors))))
        return heartbeat


def start_package_delivery_monitor():
    """Start once on the live Mac; imports do not load credentials in tests."""
    global _thread
    if sys.platform != "darwin":
        return None
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        from .config import DB_PATH, OWNER_ID, PROJECT_ROOT, normalize_handle
        from .imessage import send_message
        if not OWNER_ID:
            logger.warning("Package delivery monitor: owner_unconfigured")
            return None
        revision = "unknown"
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                                    text=True, timeout=5, shell=False, check=True)
            revision = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        monitor = DeliveryMonitor(PROJECT_ROOT, OWNER_ID, DB_PATH, send_message, normalize_handle, revision=revision)
        def run():
            while True:
                monitor.poll()
                time.sleep(POLL_SECONDS)
        _thread = threading.Thread(target=run, name="package-delivery", daemon=True)
        _thread.start()
        return _thread
