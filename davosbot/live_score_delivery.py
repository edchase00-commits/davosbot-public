"""Durable, duplicate-resistant native delivery for live-score alerts.

The score monitor owns alert timing.  This module owns only recipient routing,
one native send attempt, and reconciliation with Messages ``chat.db``.  State
is deliberately external to the repository and private to the runtime user.

An AppleScript return value cannot prove that Messages did or did not queue a
message.  Every attempt therefore records a database watermark before calling
``send_message(..., recovery_mode="none")`` and treats the matching outgoing
Messages row as the receipt.  Ambiguous attempts are never retried here.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from typing import Callable, Mapping, Sequence

from .config import normalize_handle
from .message_body import decode_attributed_body
from .text_safety import normalize_bot_text


SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 32 * 1024
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_ALERT_TEXT = 16 * 1024
MAX_ALERTS = 10_000
MAX_MESSAGE_CANDIDATES = 1_000
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
ALERT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
PHONE = re.compile(r"\+[1-9][0-9]{6,14}\Z")
EMAIL = re.compile(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+\Z")
GROUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9;:+._-]{0,511}\Z")
DELIVERY_STATES = {
    "ready",
    "waiting_route",
    "attempting",
    "sent",
    "pending",
    "failed",
    "unknown",
}


class DeliveryError(Exception):
    """A fail-closed delivery error with a non-sensitive fixed code."""


@dataclass(frozen=True)
class DeliveryConfig:
    """Validated immutable recipient routing."""

    mode: str
    expected_count: int
    participants: tuple[str, ...]
    group_guid: str | None = None
    destinations: tuple[str, ...] = ()

    def canonical_payload(self) -> dict:
        if self.mode == "direct":
            delivery = {"mode": "direct", "destinations": list(self.destinations)}
        else:
            delivery = {"mode": self.mode, "group_guid": self.group_guid}
        return {
            "schema_version": SCHEMA_VERSION,
            "expected_count": self.expected_count,
            "participants": list(self.participants),
            "delivery": delivery,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.canonical_payload())).hexdigest()


@dataclass(frozen=True)
class GroupRoute:
    """One exact-participant Messages group."""

    chat_rowid: int
    group_guid: str
    chat_identifier: str
    latest_message_rowid: int

    @property
    def send_recipient(self) -> str:
        """Identifier accepted by ``imessage.send_message(is_group=True)``."""

        if self.chat_identifier:
            return self.chat_identifier
        return self.group_guid.rsplit(";", 1)[-1]


@dataclass(frozen=True)
class RouteStatus:
    ready: bool
    mode: str
    group_guid: str | None
    destinations: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DeliveryReceipt:
    alert_id: str
    text: str
    destination: str | None
    state: str
    outcome: str
    watermark: int
    receipt_rowid: int | None
    attempted_at: float

    @property
    def confirmed(self) -> bool:
        return self.state == "sent" and self.receipt_rowid is not None

    def as_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "text": self.text,
            "destination": self.destination,
            "state": self.state,
            "outcome": self.outcome,
            "watermark": self.watermark,
            "receipt_rowid": self.receipt_rowid,
            "attempted_at": self.attempted_at,
            "confirmed": self.confirmed,
        }


@dataclass(frozen=True)
class _MessageMatch:
    state: str
    rowid: int | None


def default_state_dir() -> Path:
    """Return the private runtime directory, never a repository-relative path."""

    configured = os.getenv("DAVOS_LIVE_SCORE_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "davosbot" / "live-score-alerts"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_json(raw: bytes, maximum: int, error_code: str) -> object:
    if len(raw) > maximum:
        raise DeliveryError(error_code)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DeliveryError(error_code)
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except DeliveryError:
        raise
    except (UnicodeError, ValueError, RecursionError, TypeError):
        raise DeliveryError(error_code) from None


def _is_private_owner(info: os.stat_result, *, directory: bool) -> bool:
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(info.st_mode) or info.st_mode & 0o077:
        return False
    return not hasattr(os, "geteuid") or info.st_uid == os.geteuid()


def _ensure_private_directory(path: Path) -> None:
    if not path.is_absolute():
        raise DeliveryError("external_state_dir_required")
    try:
        if path.is_symlink():
            raise DeliveryError("unsafe_state_dir")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise DeliveryError("unsafe_state_dir")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise DeliveryError("unsafe_state_dir")
        os.chmod(path, 0o700)
        if not _is_private_owner(path.stat(follow_symlinks=False), directory=True):
            raise DeliveryError("unsafe_state_dir")
    except DeliveryError:
        raise
    except OSError:
        raise DeliveryError("unsafe_state_dir") from None


def _read_private_file(path: Path, maximum: int, error_code: str) -> bytes:
    if path.is_symlink():
        raise DeliveryError(error_code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not _is_private_owner(info, directory=False) or info.st_size > maximum:
                raise DeliveryError(error_code)
            raw = handle.read(maximum + 1)
    except FileNotFoundError:
        raise
    except DeliveryError:
        raise
    except OSError:
        raise DeliveryError(error_code) from None
    if len(raw) > maximum:
        raise DeliveryError(error_code)
    return raw


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise DeliveryError("state_write_failed") from None


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise DeliveryError("unsafe_state_path")
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        _fsync_directory(path.parent)
    except DeliveryError:
        raise
    except OSError:
        raise DeliveryError("state_write_failed") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _valid_string(value: object, maximum: int, *, allow_newline: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise DeliveryError("invalid_config")
    for character in value:
        code = ord(character)
        if code == 0 or (code < 32 and not (allow_newline and character in "\r\n\t")):
            raise DeliveryError("invalid_config")
    return value


def _destination(value: object, normalizer: Callable[[str], str]) -> str:
    raw = _valid_string(value, 254).strip()
    try:
        normalized = normalizer(raw)
    except Exception:
        raise DeliveryError("invalid_config") from None
    if not isinstance(normalized, str):
        raise DeliveryError("invalid_config")
    normalized = normalized.strip()
    if not (PHONE.fullmatch(normalized) or EMAIL.fullmatch(normalized)):
        raise DeliveryError("invalid_config")
    return normalized


def validate_delivery_config(
    payload: object,
    normalizer: Callable[[str], str] = normalize_handle,
) -> DeliveryConfig:
    """Validate a decoded routing payload with no ignored fields."""

    required = {"schema_version", "expected_count", "participants", "delivery"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise DeliveryError("invalid_config")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise DeliveryError("invalid_config")
    expected_count = payload["expected_count"]
    if type(expected_count) is not int or not 1 <= expected_count <= 20:
        raise DeliveryError("invalid_config")
    raw_participants = payload["participants"]
    if not isinstance(raw_participants, list) or len(raw_participants) != expected_count:
        raise DeliveryError("invalid_config")
    participants = tuple(_destination(value, normalizer) for value in raw_participants)
    if len(set(participants)) != expected_count:
        raise DeliveryError("duplicate_destination")

    delivery = payload["delivery"]
    if not isinstance(delivery, dict) or not isinstance(delivery.get("mode"), str):
        raise DeliveryError("invalid_config")
    mode = delivery["mode"]
    if mode == "direct":
        if set(delivery) != {"mode", "destinations"}:
            raise DeliveryError("invalid_config")
        raw_destinations = delivery["destinations"]
        if not isinstance(raw_destinations, list) or len(raw_destinations) != expected_count:
            raise DeliveryError("invalid_config")
        destinations = tuple(_destination(value, normalizer) for value in raw_destinations)
        if len(set(destinations)) != expected_count:
            raise DeliveryError("duplicate_destination")
        if set(destinations) != set(participants):
            raise DeliveryError("destination_mismatch")
        return DeliveryConfig(mode, expected_count, participants, destinations=destinations)

    if mode not in {"group", "auto_group"} or set(delivery) not in (
        {"mode"},
        {"mode", "group_guid"},
    ):
        raise DeliveryError("invalid_config")
    group_guid = delivery.get("group_guid")
    if mode == "group":
        if not isinstance(group_guid, str) or not GROUP_ID.fullmatch(group_guid):
            raise DeliveryError("invalid_config")
    elif group_guid is not None:
        # Auto mode acquires its binding from an exact participant discovery.
        raise DeliveryError("invalid_config")
    return DeliveryConfig(mode, expected_count, participants, group_guid=group_guid)


def load_delivery_config(
    path: str | os.PathLike[str],
    normalizer: Callable[[str], str] = normalize_handle,
) -> DeliveryConfig:
    config_path = Path(path).expanduser()
    _ensure_private_directory(config_path.parent)
    try:
        raw = _read_private_file(config_path, MAX_CONFIG_BYTES, "invalid_config_file")
    except FileNotFoundError:
        raise DeliveryError("config_missing") from None
    return validate_delivery_config(_parse_json(raw, MAX_CONFIG_BYTES, "invalid_config"), normalizer)


def write_delivery_config(
    path: str | os.PathLike[str],
    payload: Mapping[str, object],
    normalizer: Callable[[str], str] = normalize_handle,
) -> DeliveryConfig:
    """Validate and durably install a private external routing file."""

    config_path = Path(path).expanduser()
    _ensure_private_directory(config_path.parent)
    config = validate_delivery_config(dict(payload), normalizer)
    raw = _canonical(config.canonical_payload())
    if len(raw) > MAX_CONFIG_BYTES:
        raise DeliveryError("invalid_config")
    _atomic_write(config_path, raw)
    return config


def _readonly_db(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    try:
        path = Path(db_path).expanduser().resolve(strict=True)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except (OSError, sqlite3.Error, ValueError):
        raise DeliveryError("messages_db_unreadable") from None


def _chat_columns(connection: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute("PRAGMA table_info(chat)")}
    except sqlite3.Error:
        raise DeliveryError("messages_db_unreadable") from None


def _all_group_routes(
    db_path: str | os.PathLike[str],
    participants: Sequence[str],
    normalizer: Callable[[str], str],
) -> list[GroupRoute]:
    wanted = set(participants)
    try:
        with closing(_readonly_db(db_path)) as connection:
            columns = _chat_columns(connection)
            if "chat_identifier" not in columns:
                raise DeliveryError("messages_db_unreadable")
            guid_column = "c.guid" if "guid" in columns else "NULL"
            rows = connection.execute(
                f"""
                SELECT c.ROWID AS chat_rowid, {guid_column} AS guid,
                       c.chat_identifier, h.id AS participant
                FROM chat c
                JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
                JOIN handle h ON h.ROWID = chj.handle_id
                ORDER BY c.ROWID ASC
                """
            ).fetchall()
            grouped: dict[int, dict] = {}
            for row in rows:
                chat_rowid = int(row["chat_rowid"])
                entry = grouped.setdefault(
                    chat_rowid,
                    {
                        "guid": row["guid"],
                        "chat_identifier": row["chat_identifier"],
                        "participants": set(),
                    },
                )
                participant = row["participant"]
                if not isinstance(participant, str):
                    entry["participants"].add(None)
                    continue
                try:
                    entry["participants"].add(normalizer(participant))
                except Exception:
                    entry["participants"].add(None)

            routes = []
            for chat_rowid, entry in grouped.items():
                if entry["participants"] != wanted:
                    continue
                raw_guid = entry["guid"]
                identifier = entry["chat_identifier"]
                if not isinstance(identifier, str):
                    identifier = ""
                group_guid = raw_guid if isinstance(raw_guid, str) and raw_guid else identifier
                if not group_guid or not GROUP_ID.fullmatch(group_guid):
                    continue
                latest = connection.execute(
                    "SELECT COALESCE(MAX(message_id), 0) FROM chat_message_join WHERE chat_id = ?",
                    (chat_rowid,),
                ).fetchone()[0]
                routes.append(
                    GroupRoute(
                        chat_rowid=chat_rowid,
                        group_guid=group_guid,
                        chat_identifier=identifier,
                        latest_message_rowid=int(latest or 0),
                    )
                )
            return routes
    except DeliveryError:
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError):
        raise DeliveryError("messages_db_unreadable") from None


def discover_exact_group(
    db_path: str | os.PathLike[str],
    participants: Sequence[str],
    normalizer: Callable[[str], str] = normalize_handle,
) -> GroupRoute | None:
    """Return the newest chat whose normalized participant set is exact."""

    normalized = tuple(_destination(value, normalizer) for value in participants)
    if len(set(normalized)) != len(normalized):
        raise DeliveryError("duplicate_destination")
    routes = _all_group_routes(db_path, normalized, normalizer)
    if not routes:
        return None
    # Message ROWID is the best monotonic activity marker. Chat ROWID breaks a
    # tie for new empty/draft chats with the same exact participants.
    return max(routes, key=lambda route: (route.latest_message_rowid, route.chat_rowid))


def _resolve_bound_group(
    db_path: str | os.PathLike[str],
    participants: Sequence[str],
    group_guid: str,
    normalizer: Callable[[str], str],
) -> GroupRoute | None:
    matches = []
    for route in _all_group_routes(db_path, participants, normalizer):
        if group_guid in {route.group_guid, route.chat_identifier, route.send_recipient}:
            matches.append(route)
    if not matches:
        return None
    return max(matches, key=lambda route: (route.latest_message_rowid, route.chat_rowid))


def _watermark(db_path: str | os.PathLike[str]) -> int:
    try:
        with closing(_readonly_db(db_path)) as connection:
            row = connection.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
            return int(row[0] or 0)
    except DeliveryError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise DeliveryError("messages_db_unreadable") from None


def _direct_chat_ids(
    connection: sqlite3.Connection,
    destination: str,
    normalizer: Callable[[str], str],
) -> set[int]:
    chat_ids: set[int] = set()
    try:
        for row in connection.execute("SELECT ROWID, chat_identifier FROM chat"):
            identifier = row["chat_identifier"]
            if isinstance(identifier, str):
                try:
                    if normalizer(identifier) == destination:
                        chat_ids.add(int(row["ROWID"]))
                except Exception:
                    pass

        participant_sets: dict[int, set[str | None]] = {}
        for row in connection.execute(
            """
            SELECT chj.chat_id, h.id
            FROM chat_handle_join chj
            JOIN handle h ON h.ROWID = chj.handle_id
            """
        ):
            values = participant_sets.setdefault(int(row[0]), set())
            try:
                values.add(normalizer(row[1]) if isinstance(row[1], str) else None)
            except Exception:
                values.add(None)
        chat_ids.update(chat_id for chat_id, values in participant_sets.items() if values == {destination})
        return chat_ids
    except sqlite3.Error:
        raise DeliveryError("messages_db_unreadable") from None


def _message_match(
    db_path: str | os.PathLike[str],
    text: str,
    watermark: int,
    *,
    destination: str,
    group_route: GroupRoute | None,
    normalizer: Callable[[str], str],
) -> _MessageMatch:
    """Reconcile exact body, direction, post-watermark row, and destination."""

    try:
        with closing(_readonly_db(db_path)) as connection:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(message)")}
            if not {"text", "is_from_me", "is_sent", "error"} <= columns:
                raise DeliveryError("messages_db_unreadable")
            body = "m.attributedBody" if "attributedBody" in columns else "NULL"
            if group_route is not None:
                chat_ids = {group_route.chat_rowid}
            else:
                chat_ids = _direct_chat_ids(connection, destination, normalizer)
            if not chat_ids:
                return _MessageMatch("absent", None)
            placeholders = ",".join("?" for _ in chat_ids)
            rows = connection.execute(
                f"""
                SELECT m.ROWID, m.text, {body} AS attributed_body,
                       m.is_sent, m.error
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE m.ROWID > ?
                  AND m.is_from_me = 1
                  AND cmj.chat_id IN ({placeholders})
                  AND (m.text = ? OR (m.text IS NULL AND {body} IS NOT NULL))
                ORDER BY m.ROWID DESC
                LIMIT ?
                """,
                (watermark, *sorted(chat_ids), text, MAX_MESSAGE_CANDIDATES + 1),
            ).fetchall()
            if len(rows) > MAX_MESSAGE_CANDIDATES:
                return _MessageMatch("unknown", int(rows[0]["ROWID"]))

            exact: list[tuple[str, int]] = []
            undecodable: list[int] = []
            for row in rows:
                row_text = row["text"]
                if row_text is None:
                    row_text = decode_attributed_body(row["attributed_body"])
                    if row_text is None:
                        undecodable.append(int(row["ROWID"]))
                        continue
                if row_text != text:
                    continue
                sent = row["is_sent"]
                error = row["error"]
                state = (
                    "sent"
                    if sent == 1 and error == 0
                    else "failed"
                    if isinstance(error, int) and error != 0
                    else "pending"
                )
                exact.append((state, int(row["ROWID"])))
            for state in ("sent", "pending", "failed"):
                matching = [rowid for row_state, rowid in exact if row_state == state]
                if matching:
                    return _MessageMatch(state, max(matching))
            if undecodable:
                return _MessageMatch("unknown", max(undecodable))
            return _MessageMatch("absent", None)
    except DeliveryError:
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError):
        raise DeliveryError("messages_db_unreadable") from None


def _empty_state(config: DeliveryConfig) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "config_hash": config.digest,
        "bound_group_guid": None,
        "alerts": {},
    }


def _validate_record(record: object) -> None:
    required = {
        "destination",
        "state",
        "watermark",
        "attempted_at",
        "sender_result",
        "receipt_rowid",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise DeliveryError("state_corrupt")
    destination = record["destination"]
    if destination is not None and (
        not isinstance(destination, str) or not destination or len(destination) > 512
    ):
        raise DeliveryError("state_corrupt")
    if record["state"] not in DELIVERY_STATES:
        raise DeliveryError("state_corrupt")
    if type(record["watermark"]) is not int or record["watermark"] < 0:
        raise DeliveryError("state_corrupt")
    attempted_at = record["attempted_at"]
    if type(attempted_at) not in (int, float) or not math.isfinite(attempted_at) or attempted_at < 0:
        raise DeliveryError("state_corrupt")
    if record["sender_result"] is not None and type(record["sender_result"]) is not bool:
        raise DeliveryError("state_corrupt")
    receipt = record["receipt_rowid"]
    if receipt is not None and (type(receipt) is not int or receipt <= 0):
        raise DeliveryError("state_corrupt")
    if record["state"] == "sent" and receipt is None:
        raise DeliveryError("state_corrupt")


def _validate_state(payload: object, config: DeliveryConfig) -> dict:
    required = {"schema_version", "config_hash", "bound_group_guid", "alerts"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise DeliveryError("state_corrupt")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise DeliveryError("state_corrupt")
    if payload["config_hash"] != config.digest:
        raise DeliveryError("config_changed")
    bound = payload["bound_group_guid"]
    if bound is not None and (not isinstance(bound, str) or not GROUP_ID.fullmatch(bound)):
        raise DeliveryError("state_corrupt")
    if config.mode != "auto_group" and bound is not None:
        raise DeliveryError("state_corrupt")
    alerts = payload["alerts"]
    if not isinstance(alerts, dict) or len(alerts) > MAX_ALERTS:
        raise DeliveryError("state_corrupt")
    expected_targets = {"group"} if config.mode != "direct" else set(config.destinations)
    for alert_id, alert in alerts.items():
        if not isinstance(alert_id, str) or not ALERT_ID.fullmatch(alert_id):
            raise DeliveryError("state_corrupt")
        if not isinstance(alert, dict) or set(alert) != {"text", "targets"}:
            raise DeliveryError("state_corrupt")
        text = alert["text"]
        if not isinstance(text, str) or not text or len(text) > MAX_ALERT_TEXT or "\x00" in text:
            raise DeliveryError("state_corrupt")
        targets = alert["targets"]
        if not isinstance(targets, dict) or set(targets) != expected_targets:
            raise DeliveryError("state_corrupt")
        for key, record in targets.items():
            _validate_record(record)
            if config.mode == "direct" and record["destination"] != key:
                raise DeliveryError("state_corrupt")
    return payload


def _load_state(path: Path, config: DeliveryConfig) -> dict:
    try:
        raw = _read_private_file(path, MAX_STATE_BYTES, "state_corrupt")
    except FileNotFoundError:
        return _empty_state(config)
    return _validate_state(_parse_json(raw, MAX_STATE_BYTES, "state_corrupt"), config)


def _save_state(path: Path, state: dict, config: DeliveryConfig) -> None:
    _validate_state(state, config)
    payload = _canonical(state)
    if len(payload) > MAX_STATE_BYTES:
        raise DeliveryError("state_capacity")
    _atomic_write(path, payload)


def _new_record(destination: str | None, state: str) -> dict:
    return {
        "destination": destination,
        "state": state,
        "watermark": 0,
        "attempted_at": 0,
        "sender_result": None,
        "receipt_rowid": None,
    }


class LiveScoreDelivery:
    """Single-process durable native delivery coordinator.

    Construction takes a non-blocking lifetime ``flock``. Call ``close`` (or
    use a context manager) before another monitor process can own the state.
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None,
        db_path: str | os.PathLike[str],
        *,
        config_path: str | os.PathLike[str] | None = None,
        sender: Callable[..., bool] | None = None,
        normalizer: Callable[[str], str] = normalize_handle,
        clock: Callable[[], float] = time.time,
    ):
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        _ensure_private_directory(self.state_dir)
        self.config_path = Path(config_path).expanduser() if config_path is not None else self.state_dir / "config.json"
        self.db_path = Path(db_path).expanduser()
        self.normalizer = normalizer
        self.clock = clock
        self._lock_fd: int | None = None
        self._acquire_lock()
        try:
            self.config = load_delivery_config(self.config_path, self.normalizer)
            self._state_path = self.state_dir / "state.json"
            self._state = _load_state(self._state_path, self.config)
            if sender is None:
                from .imessage import send_message

                sender = send_message
            self.sender = sender
        except Exception:
            self.close()
            raise

    def _acquire_lock(self) -> None:
        import fcntl

        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.state_dir / "monitor.lock", flags, 0o600)
            os.fchmod(fd, 0o600)
            if not _is_private_owner(os.fstat(fd), directory=False):
                os.close(fd)
                raise DeliveryError("unsafe_lock_file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise DeliveryError("monitor_busy") from None
            self._lock_fd = fd
        except DeliveryError:
            raise
        except OSError:
            raise DeliveryError("unsafe_lock_file") from None

    def close(self) -> None:
        if self._lock_fd is None:
            return
        fd, self._lock_fd = self._lock_fd, None
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "LiveScoreDelivery":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _require_lock(self) -> None:
        if self._lock_fd is None:
            raise DeliveryError("monitor_closed")

    @property
    def bound_group_guid(self) -> str | None:
        self._require_lock()
        if self.config.mode == "group":
            return self.config.group_guid
        return self._state["bound_group_guid"]

    def _route(self) -> GroupRoute | None:
        if self.config.mode == "direct":
            return None
        if self.config.mode == "group":
            assert self.config.group_guid is not None
            return _resolve_bound_group(
                self.db_path,
                self.config.participants,
                self.config.group_guid,
                self.normalizer,
            )
        bound = self._state["bound_group_guid"]
        if bound:
            route = _resolve_bound_group(
                self.db_path,
                self.config.participants,
                bound,
                self.normalizer,
            )
            if route is not None:
                return route
            # A participant can leave a previously valid Messages thread.
            # Keep exact-participant validation fail-closed, but allow
            # auto-discovery of a replacement exact group instead of pinning
            # the monitor forever to a route that no longer qualifies.
        return discover_exact_group(self.db_path, self.config.participants, self.normalizer)

    def route_status(self) -> RouteStatus:
        """Read current route readiness without writing state or sending."""

        self._require_lock()
        if self.config.mode == "direct":
            return RouteStatus(
                ready=True,
                mode="direct",
                group_guid=None,
                destinations=self.config.destinations,
                reason="explicit_direct_destinations",
            )
        route = self._route()
        if route is None:
            return RouteStatus(
                ready=False,
                mode=self.config.mode,
                group_guid=self.bound_group_guid,
                destinations=(),
                reason="no_exact_group",
            )
        return RouteStatus(
            ready=True,
            mode=self.config.mode,
            group_guid=route.group_guid,
            destinations=(route.send_recipient,),
            reason="exact_group",
        )

    def _save(self) -> None:
        _save_state(self._state_path, self._state, self.config)

    def _bind_discovered_route(self, route: GroupRoute) -> None:
        if self.config.mode != "auto_group":
            return
        bound = self._state["bound_group_guid"]
        if bound is not None and bound != route.group_guid:
            existing = _resolve_bound_group(
                self.db_path,
                self.config.participants,
                bound,
                self.normalizer,
            )
            if existing is not None:
                raise DeliveryError("bound_group_changed")
        if bound != route.group_guid:
            self._state["bound_group_guid"] = route.group_guid
            self._save()

    @staticmethod
    def _validate_alert(alert_id: str, text: str) -> tuple[str, str]:
        if not isinstance(alert_id, str) or not ALERT_ID.fullmatch(alert_id):
            raise DeliveryError("invalid_alert_id")
        if not isinstance(text, str):
            raise DeliveryError("invalid_alert_text")
        exact = normalize_bot_text(text)
        if not exact or len(exact) > MAX_ALERT_TEXT or "\x00" in exact:
            raise DeliveryError("invalid_alert_text")
        return alert_id, exact

    def _receipt(self, alert_id: str, text: str, record: dict, *, existing: bool) -> DeliveryReceipt:
        state = record["state"]
        if state == "waiting_route":
            outcome = "no_route" if record["destination"] is None else "waiting_bootstrap_receipt"
        elif state == "sent":
            outcome = "reconciled_sent"
        elif existing:
            outcome = "reconciled"
        else:
            outcome = "attempted"
        return DeliveryReceipt(
            alert_id=alert_id,
            text=text,
            destination=record["destination"],
            state=state,
            outcome=outcome,
            watermark=record["watermark"],
            receipt_rowid=record["receipt_rowid"],
            attempted_at=float(record["attempted_at"]),
        )

    def _reconcile_record(
        self,
        record: dict,
        text: str,
        *,
        route: GroupRoute | None,
    ) -> None:
        if record["state"] in {"ready", "sent"}:
            return
        if record["destination"] is None:
            if route is None:
                return
            record["destination"] = route.group_guid
        match = _message_match(
            self.db_path,
            text,
            record["watermark"],
            destination=record["destination"],
            group_route=route,
            normalizer=self.normalizer,
        )
        if match.state != "absent":
            record["state"] = match.state
            record["receipt_rowid"] = match.rowid
            return
        if record["state"] == "attempting":
            record["state"] = "failed" if record["sender_result"] is False else "unknown"
        # Waiting bootstrap, pending, failed, and unknown records remain
        # reconcilable but are never converted into a new send attempt.

    def _attempt(self, record: dict, text: str, *, route: GroupRoute | None) -> None:
        if record["state"] != "ready":
            return
        if self.config.mode != "direct" and route is None:
            record.update(
                state="waiting_route",
                watermark=_watermark(self.db_path),
                sender_result=None,
                receipt_rowid=None,
            )
            self._save()
            return
        record.update(
            state="attempting",
            watermark=_watermark(self.db_path),
            attempted_at=float(self.clock()),
            sender_result=None,
            receipt_rowid=None,
        )
        if route is not None:
            record["destination"] = route.group_guid
        self._save()  # Durable attempting record always precedes native send.

        result: bool | None = None
        try:
            if route is not None:
                result = self.sender(
                    route.send_recipient,
                    text,
                    is_group=True,
                    recovery_mode="none",
                )
            else:
                result = self.sender(
                    record["destination"],
                    text,
                    is_group=False,
                    recovery_mode="none",
                )
        except Exception:
            result = None
        record["sender_result"] = result if type(result) is bool else None
        self._save()
        self._reconcile_record(record, text, route=route)
        self._save()

    def deliver(self, alert_id: str, text: str) -> list[DeliveryReceipt]:
        """Attempt each configured destination at most once for this alert ID."""

        self._require_lock()
        alert_id, exact = self._validate_alert(alert_id, text)
        alert = self._state["alerts"].get(alert_id)
        existing = alert is not None
        if alert is not None and alert["text"] != exact:
            raise DeliveryError("immutable_alert_changed")

        route = self._route() if self.config.mode != "direct" else None
        if route is not None:
            self._bind_discovered_route(route)
        if alert is None:
            if self.config.mode == "direct":
                targets = {
                    destination: _new_record(destination, "ready")
                    for destination in self.config.destinations
                }
            elif route is None:
                # This global watermark allows an operator to create the exact
                # group by sending this exact first alert in Messages. It also
                # prevents an older identical body from being accepted later.
                waiting = _new_record(None, "waiting_route")
                waiting["watermark"] = _watermark(self.db_path)
                targets = {"group": waiting}
            else:
                targets = {"group": _new_record(route.group_guid, "ready")}
            alert = {"text": exact, "targets": targets}
            self._state["alerts"][alert_id] = alert
            self._save()

        receipts = []
        for record in alert["targets"].values():
            before = dict(record)
            if record["state"] != "ready":
                self._reconcile_record(record, exact, route=route)
                if record != before:
                    self._save()
            if record["state"] == "ready":
                self._attempt(record, exact, route=route)
            receipts.append(self._receipt(alert_id, exact, record, existing=existing))
        return receipts

    def reconcile(self, alert_id: str | None = None) -> list[DeliveryReceipt]:
        """Re-read exact Messages receipts without initiating any send."""

        self._require_lock()
        if alert_id is not None and (
            not isinstance(alert_id, str) or not ALERT_ID.fullmatch(alert_id)
        ):
            raise DeliveryError("invalid_alert_id")
        route = self._route() if self.config.mode != "direct" else None
        if route is not None:
            self._bind_discovered_route(route)
        selected = (
            [(alert_id, self._state["alerts"][alert_id])]
            if alert_id in self._state["alerts"]
            else []
            if alert_id is not None
            else list(self._state["alerts"].items())
        )
        receipts = []
        changed = False
        for current_id, alert in selected:
            for record in alert["targets"].values():
                before = dict(record)
                self._reconcile_record(record, alert["text"], route=route)
                changed = changed or record != before
                receipts.append(
                    self._receipt(current_id, alert["text"], record, existing=True)
                )
        if changed:
            self._save()
        return receipts

    def snapshot(self) -> dict:
        """Return a JSON-safe copy for local status reporting."""

        self._require_lock()
        return json.loads(_canonical(self._state))
