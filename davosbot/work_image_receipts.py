"""Durable metadata for authenticated Work image jobs, without automatic replay.

Only the bridge supplies request identity. Records contain no prompts, image
paths, message bodies or credentials. Reading a receipt never starts a job.
"""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

_REQUEST = ContextVar("davos_work_image_request", default=None)
_PROCESS_ID = str(uuid.uuid4())
_LIVE = set()
_LOCK = threading.RLock()
_STATES = {"queued", "generating", "sending", "sent", "failed", "unknown"}
_TERMINAL = {"sent", "failed", "unknown"}
_REASONS = {"", "generation_failed", "start_failed", "send_unverified", "execution_interrupted"}
_PROVIDERS = {"local", "gemini", "openai", "unknown"}
_MAX_RECORDS = 10000


def valid_request_id(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _owner_key(owner):
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner_required")
    return hashlib.sha256(owner.encode()).hexdigest()


@dataclass(frozen=True)
class _Identity:
    request_id: str
    comment_id: int
    owner: str
    root: Path
    revision: str


@contextmanager
def request_scope(request_id, comment_id, owner, root, revision="unknown"):
    """Internal bridge scope, installed only after transport authentication."""
    if not valid_request_id(request_id) or type(comment_id) is not int or comment_id <= 0:
        raise ValueError("invalid_request_identity")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{7,40}|unknown", revision):
        revision = "unknown"
    _owner_key(owner)
    token = _REQUEST.set(_Identity(request_id, comment_id, owner, Path(root), revision))
    try:
        yield
    finally:
        _REQUEST.reset(token)


def _path(root, create=False):
    root = Path(root)
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("unsafe_image_receipt_path")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "image_receipts.sqlite3"
    if path.is_symlink():
        raise ValueError("unsafe_image_receipt_path")
    return path


@contextmanager
def _database(root, write=False):
    path = _path(root, create=write)
    if write:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
    with closing(sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                uri=True, timeout=3)) as conn:
        conn.row_factory = sqlite3.Row
        if write:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("PRAGMA query_only=ON")
        try:
            if write:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ValueError("image_receipt_schema_unknown")
                conn.execute("""CREATE TABLE IF NOT EXISTS image_receipts (
                    request_id TEXT PRIMARY KEY, comment_id INTEGER NOT NULL,
                    owner_key TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE,
                    process_id TEXT NOT NULL, provider TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    runtime_revision TEXT NOT NULL)""")
                conn.execute("PRAGMA user_version=1")
            elif conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("image_receipt_schema_unknown")
            yield conn
            if write:
                conn.commit()
                if os.name != "nt":
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
        except BaseException:
            if write:
                conn.rollback()
            raise


def _now():
    now = time.time()
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise ValueError("invalid_receipt_clock")
    return now


class ImageTracker:
    def __init__(self, owner):
        self.identity = _REQUEST.get()
        if self.identity is None or self.identity.owner != owner:
            raise ValueError("authenticated_image_request_required")
        self.job_id = None

    def prepare(self, job_id, sender, recipient, is_group, provider):
        identity = self.identity
        if (self.job_id is not None or sender != identity.owner or recipient != identity.owner
                or is_group is not False or not re.fullmatch(r"[0-9]{10,16}-[0-9]{4}", job_id)):
            raise ValueError("invalid_image_job_binding")
        now = _now()
        provider = provider if provider in _PROVIDERS else "unknown"
        with _LOCK, _database(identity.root, write=True) as conn:
            if conn.execute("SELECT count(*) FROM image_receipts").fetchone()[0] >= _MAX_RECORDS:
                raise ValueError("image_receipt_capacity")
            conn.execute("INSERT INTO image_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                identity.request_id, identity.comment_id, _owner_key(identity.owner), job_id,
                _PROCESS_ID, provider, "queued", "", now, now, identity.revision))
        self.job_id = job_id
        with _LOCK:
            _LIVE.add(job_id)

    def mark(self, state, *, reason="", provider=None):
        transitions = {"generating": {"queued"}, "sending": {"generating"},
                       "sent": {"sending"}, "failed": {"queued", "generating"},
                       "unknown": {"queued", "generating", "sending"}}
        if self.job_id is None or state not in transitions or reason not in _REASONS:
            raise ValueError("invalid_image_receipt_transition")
        identity = self.identity
        with _LOCK, _database(identity.root, write=True) as conn:
            row = conn.execute("SELECT * FROM image_receipts WHERE request_id=?", (identity.request_id,)).fetchone()
            if (row is None or row["comment_id"] != identity.comment_id or row["job_id"] != self.job_id
                    or row["owner_key"] != _owner_key(identity.owner) or row["process_id"] != _PROCESS_ID
                    or row["state"] not in transitions[state]):
                raise ValueError("image_receipt_transition_rejected")
            actual_provider = provider if provider in _PROVIDERS else row["provider"]
            conn.execute("UPDATE image_receipts SET state=?,reason=?,provider=?,updated_at=? WHERE request_id=?",
                         (state, reason, actual_provider, _now(), identity.request_id))

    def close(self):
        with _LOCK:
            _LIVE.discard(self.job_id)


def receipt(request_id, comment_id, owner, *, root=None):
    """Owner adapter calls this after its gate; unknown never authorizes retry."""
    if not valid_request_id(request_id) or type(comment_id) is not int or comment_id <= 0:
        raise ValueError("invalid_image_receipt_lookup")
    if root is None:
        from .config import PROJECT_ROOT
        root = PROJECT_ROOT / ".work_bridge"
    result = {"request_id": request_id, "request_comment_id": comment_id,
              "receipt_state": "unknown", "job_state": "unknown",
              "send_verified": False, "delivery_state": "unknown", "retry_safe": False}
    try:
        with _LOCK, _database(root) as conn:
            row = conn.execute("SELECT * FROM image_receipts WHERE request_id=? AND comment_id=? AND owner_key=?",
                               (request_id, comment_id, _owner_key(owner))).fetchone()
        if row is None:
            return result
        if (not re.fullmatch(r"[0-9]{10,16}-[0-9]{4}", row["job_id"])
                or row["state"] not in _STATES or row["reason"] not in _REASONS
                or row["provider"] not in _PROVIDERS
                or not re.fullmatch(r"[0-9a-f]{7,40}|unknown", row["runtime_revision"])
                or any(type(row[key]) not in (int, float) or not math.isfinite(row[key]) or row[key] <= 0
                       for key in ("created_at", "updated_at"))):
            return result
        state, reason = row["state"], row["reason"]
        with _LOCK:
            if state not in _TERMINAL and (row["process_id"] != _PROCESS_ID or row["job_id"] not in _LIVE):
                state, reason = "unknown", "execution_interrupted"
        result.update(receipt_state="recorded", job_id=row["job_id"], job_state=state,
                      reason=reason, provider=row["provider"], send_verified=state == "sent",
                      created_at=row["created_at"], updated_at=row["updated_at"],
                      runtime_revision=row["runtime_revision"])
        if state == "sent":
            result["send_evidence"] = "native_attachment_is_sent_without_error"
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return result
