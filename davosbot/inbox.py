"""Durable intake identities with at-most-once handler claims.

This module has no configuration, startup, send, model, or permission imports.
Apple's database stays read-only; message content is never copied to the ledger.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from .message_body import decode_attributed_body
from .inbox_ownership import initializing_inbox
from .text_safety import is_imessage_reaction

RECOVERY_MAX_AGE = 15 * 60
READINESS_MAX_AGE = 2 * 60
FUTURE_TOLERANCE = 2 * 60
SCAN_LIMIT = 500
DISPATCH_LIMIT = 20
MAX_PENDING_MESSAGES = 1000
APPLE_EPOCH = 978307200
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}

_SCHEMA = (
    ("""CREATE TABLE IF NOT EXISTS inbound_source (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        source_identity TEXT,
        cursor_rowid INTEGER CHECK (cursor_rowid >= 0),
        anchor_guid TEXT,
        initialized_at REAL,
        runtime_session_id INTEGER,
        session_error TEXT,
        last_poll_at REAL,
        anchor_missing INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )""", "Add durable iMessage source cursor"),
    ("""CREATE TABLE IF NOT EXISTS inbound_messages (
        message_guid TEXT PRIMARY KEY,
        source_rowid INTEGER NOT NULL UNIQUE,
        source_date INTEGER,
        source_handle_id INTEGER,
        recovered INTEGER NOT NULL DEFAULT 0,
        sender TEXT,
        sender_key TEXT,
        chat_identifier TEXT,
        observed_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        claimed_at REAL,
        confirmation_cutoff_rowid INTEGER,
        state TEXT NOT NULL CHECK (state IN
            ('pending', 'processing', 'handler_returned', 'uncertain', 'ignored', 'held')),
        reason TEXT
    )""", "Add durable iMessage intake ledger"),
    ("""CREATE INDEX IF NOT EXISTS idx_inbound_pending
        ON inbound_messages (state, source_rowid)""", "Index pending iMessage identities"),
    ("""CREATE INDEX IF NOT EXISTS idx_inbound_sender_claims
        ON inbound_messages (sender_key, source_rowid)""", "Index sender-scoped iMessage claims"),
)


class InboxSourceError(RuntimeError):
    """A fixed, non-content-bearing source failure suitable for health metadata."""


def initialize_schema(db_path, migrate=None):
    if migrate is not None:
        for sql, description in _SCHEMA:
            migrate(sql, description)
        return
    with closing(sqlite3.connect(db_path)) as conn, conn:
        for sql, _description in _SCHEMA:
            conn.execute(sql)


def inbox_health(db_path, *, now=None):
    """Read aggregate intake health without initializing or mutating any DB."""
    now = time.time() if now is None else now
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return {"available": False, "reason": "database_missing"}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            source = conn.execute("SELECT initialized_at,last_poll_at,anchor_missing,last_error,session_error FROM inbound_source WHERE id=1").fetchone()
            counts = dict(conn.execute("SELECT state,COUNT(*) FROM inbound_messages GROUP BY state").fetchall())
            oldest = conn.execute("SELECT MIN(observed_at) FROM inbound_messages WHERE state='pending'").fetchone()[0]
            processing = conn.execute("SELECT MIN(claimed_at) FROM inbound_messages WHERE state='processing'").fetchone()[0]
            reasons = dict(conn.execute("SELECT reason,COUNT(*) FROM inbound_messages WHERE state='held' GROUP BY reason").fetchall())
    except sqlite3.Error:
        return {"available": False, "reason": "schema_or_database_unavailable"}
    return {
        "available": True,
        "initialized": bool(source and source["initialized_at"] is not None),
        "counts": counts,
        "held_reasons": reasons,
        "oldest_pending_age_seconds": max(0, now - oldest) if oldest is not None else None,
        "oldest_processing_age_seconds": max(0, now - processing) if processing is not None else None,
        "last_poll_age_seconds": max(0, now - source["last_poll_at"]) if source and source["last_poll_at"] is not None else None,
        "anchor_missing": bool(source and source["anchor_missing"]),
        "source_error": (source["session_error"] or source["last_error"] or
                         ("source_not_initialized" if source["initialized_at"] is None else None)) if source else "source_not_initialized",
    }


class MessageInbox:
    def __init__(self, source_path, db_path, *, now=time.time, migrate=None,
                 confirmation_guard=None, session_id=None, normalize_sender=None):
        self.source_path = Path(source_path).expanduser().resolve()
        self.db_path = Path(db_path)
        self.now = now
        self.confirmation_guard = confirmation_guard
        self.session_id = session_id
        self.normalize_sender = normalize_sender or (lambda value: value)
        self._session_cutover = None
        with initializing_inbox(self.db_path):
            initialize_schema(self.db_path, migrate)
            # A previous process claim may have reached an external effect.
            # A still-running in-process worker must never be "recovered".
            with self._transaction() as conn:
                conn.execute("""UPDATE inbound_messages SET state='uncertain',reason='interrupted',updated_at=?
                                WHERE state='processing'""", (self.now(),))
                conn.execute("UPDATE inbound_messages SET recovered=1 WHERE state='pending'")
                self._attach_session(conn)

    def _attach_session(self, conn):
        checkpoint = conn.execute("SELECT * FROM inbound_source WHERE id=1").fetchone()
        previous = checkpoint["runtime_session_id"] if checkpoint else None
        if self.session_id is None and previous is None:
            return  # Standalone synthetic consumers need no bot_sessions table.
        if checkpoint is None:
            conn.execute("INSERT INTO inbound_source (id) VALUES (1)")
        if checkpoint and checkpoint["session_error"] in {"untracked_runtime_session", "runtime_session_not_new"}:
            return  # An operator-reviewed cutover is required, never auto-rebase.
        valid = type(self.session_id) is int and self.session_id > 0
        if valid:
            try:
                latest = conn.execute("SELECT MAX(id) FROM bot_sessions").fetchone()[0]
                sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='bot_sessions'").fetchone()
                valid = latest == self.session_id and sequence is not None and sequence[0] == self.session_id
            except sqlite3.Error:
                valid = False
        error = None
        if not valid:
            error = "runtime_session_not_committed"
        elif previous is not None and self.session_id <= previous:
            error = "runtime_session_not_new"
        elif previous is not None and self.session_id != previous + 1:
            error = "untracked_runtime_session"
        if error:
            conn.execute("UPDATE inbound_source SET session_error=? WHERE id=1", (error,))
        else:
            conn.execute("UPDATE inbound_source SET runtime_session_id=?,session_error=NULL WHERE id=1", (self.session_id,))

    def _check_session(self, checkpoint):
        if checkpoint["session_error"]:
            raise InboxSourceError(checkpoint["session_error"])
        if checkpoint["runtime_session_id"] != self.session_id:
            raise InboxSourceError("runtime_session_changed")

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn

    @contextmanager
    def _source(self):
        with closing(sqlite3.connect(self.source_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn

    def _source_identity(self):
        info = self.source_path.stat()
        value = f"{self.source_path}\0{info.st_dev}\0{info.st_ino}"
        return hashlib.sha256(value.encode()).hexdigest()

    def _check_source(self, source, checkpoint):
        self._check_session(checkpoint)
        if self._source_identity() != checkpoint["source_identity"]:
            raise InboxSourceError("source_identity_changed")
        if not checkpoint["cursor_rowid"]:
            return False
        anchor = source.execute("SELECT guid FROM message WHERE ROWID=?", (checkpoint["cursor_rowid"],)).fetchone()
        if anchor is not None and anchor["guid"] != checkpoint["anchor_guid"]:
            raise InboxSourceError("cursor_anchor_conflict")
        # Deleting a message is not proof of a replaced source. Keep the durable
        # floor, report the missing anchor, and only accept strictly newer rows.
        return anchor is None

    def _record_error(self, error):
        reason = str(error) if isinstance(error, InboxSourceError) else "source_or_ledger_unavailable"
        try:
            with self._transaction() as conn:
                conn.execute("UPDATE inbound_source SET last_error=? WHERE id=1", (reason,))
        except sqlite3.Error:
            pass

    def poll(self):
        """Commit a bounded identity window and cursor together, before dispatch."""
        try:
            with self._transaction() as ledger, self._source() as source:
                now = self.now()
                if self._session_cutover is None:
                    self._session_cutover = source.execute("SELECT COALESCE(MAX(ROWID),0) FROM message").fetchone()[0]
                checkpoint = ledger.execute("SELECT * FROM inbound_source WHERE id=1").fetchone()
                if checkpoint is not None:
                    self._check_session(checkpoint)
                if checkpoint is None or checkpoint["initialized_at"] is None:
                    anchor = source.execute("SELECT ROWID,guid FROM message ORDER BY ROWID DESC LIMIT 1").fetchone()
                    if anchor and not anchor["guid"]:
                        raise InboxSourceError("missing_message_identity")
                    ledger.execute("INSERT OR IGNORE INTO inbound_source (id) VALUES (1)")
                    ledger.execute("""UPDATE inbound_source SET source_identity=?,cursor_rowid=?,anchor_guid=?,
                        initialized_at=?,last_poll_at=? WHERE id=1""", (self._source_identity(), anchor["ROWID"] if anchor else 0,
                                                                       anchor["guid"] if anchor else None, now, now))
                    return 0
                missing = self._check_source(source, checkpoint)
                waiting = ledger.execute("SELECT COUNT(*) FROM inbound_messages WHERE state IN ('pending','processing')").fetchone()[0]
                capacity = max(0, MAX_PENDING_MESSAGES - waiting)
                if not capacity:
                    ledger.execute("UPDATE inbound_source SET last_poll_at=?,anchor_missing=?,last_error='intake_backpressure' WHERE id=1",
                                   (now, int(missing)))
                    return 0
                rows = source.execute("""SELECT m.ROWID,m.guid,m.date,m.is_from_me,m.handle_id,h.id AS sender
                    FROM message m LEFT JOIN handle h ON h.ROWID=m.handle_id
                    WHERE m.ROWID>? ORDER BY m.ROWID LIMIT ?""", (checkpoint["cursor_rowid"], min(SCAN_LIMIT, capacity))).fetchall()
                for row in rows:
                    if not isinstance(row["guid"], str) or not row["guid"].strip():
                        raise InboxSourceError("missing_message_identity")
                    duplicate = ledger.execute("SELECT source_rowid FROM inbound_messages WHERE message_guid=?", (row["guid"],)).fetchone()
                    if duplicate and duplicate[0] != row["ROWID"]:
                        raise InboxSourceError("duplicate_message_identity")
                    chats = self._chats(source, row["ROWID"])
                    ledger.execute("""INSERT OR IGNORE INTO inbound_messages
                        (message_guid,source_rowid,source_date,source_handle_id,recovered,sender,sender_key,chat_identifier,
                         observed_at,updated_at,state,reason)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (row["guid"], row["ROWID"], row["date"], row["handle_id"],
                                                    int(row["ROWID"] <= self._session_cutover),
                                                    row["sender"], self.normalize_sender(row["sender"]) if row["sender"] else None,
                                                    next(iter(chats)) if len(chats) == 1 else None, now, now,
                                                    "ignored" if row["is_from_me"] else "pending",
                                                    "outbound" if row["is_from_me"] else None))
                if rows:
                    ledger.execute("UPDATE inbound_source SET cursor_rowid=?,anchor_guid=? WHERE id=1",
                                   (rows[-1]["ROWID"], rows[-1]["guid"]))
                    missing = False
                ledger.execute("UPDATE inbound_source SET last_poll_at=?,anchor_missing=?,last_error=NULL WHERE id=1", (now, int(missing)))
                return len(rows)
        except (OSError, sqlite3.Error, InboxSourceError) as error:
            self._record_error(error)
            raise

    @staticmethod
    def _set_state(ledger, guid, state, reason, now):
        ledger.execute("UPDATE inbound_messages SET state=?,reason=?,updated_at=? WHERE message_guid=?",
                       (state, reason, now, guid))

    @staticmethod
    def _chats(source, rowid):
        return {r[0] for r in source.execute("""SELECT c.chat_identifier FROM chat_message_join j
            JOIN chat c ON c.ROWID=j.chat_id WHERE j.message_id=?""", (rowid,)).fetchall()}

    def _inspect_pending(self, source, ledger, item, now):
        guid = item["message_guid"]
        row = source.execute("""SELECT m.*,h.id AS sender FROM message m
            LEFT JOIN handle h ON h.ROWID=m.handle_id WHERE m.ROWID=?""", (item["source_rowid"],)).fetchone()
        if row is None:
            return "held", "source_message_missing", None
        data = dict(row)
        if data.get("guid") != guid:
            return "held", "message_identity_changed", None
        copies = source.execute("SELECT ROWID FROM message WHERE guid=? LIMIT 2", (guid,)).fetchall()
        if len(copies) != 1:
            return "held", "ambiguous_message_identity", None
        if data.get("is_from_me") != 0:
            return "ignored", "outbound", None
        if data.get("date") != item["source_date"]:
            return "held", "message_date_changed", None
        if data.get("handle_id") != item["source_handle_id"]:
            return "held", "origin_changed", None
        text = data.get("text")
        if not text and data.get("attributedBody"):
            text = decode_attributed_body(data["attributedBody"])
        if is_imessage_reaction(text, data.get("associated_message_type"), data.get("associated_message_guid")):
            return "ignored", "reaction", None
        raw_date = data.get("date")
        if not isinstance(raw_date, (int, float)) or raw_date <= 0:
            return "held", "invalid_timestamp", None
        sent_at = APPLE_EPOCH + (raw_date / 1_000_000_000 if raw_date >= 1_000_000_000_000 else raw_date)
        if now - sent_at > RECOVERY_MAX_AGE:
            return "held", "stale_message", None
        if sent_at - now > FUTURE_TOLERANCE:
            return "held", "future_timestamp", None

        chats = self._chats(source, item["source_rowid"])
        if len(chats) > 1:
            return "held", "ambiguous_origin", None
        sender = data.get("sender")
        chat = next(iter(chats), None)
        if ((item["sender"] and sender != item["sender"])
                or (item["chat_identifier"] and chat != item["chat_identifier"])):
            return "held", "origin_changed", None
        sender_key = self.normalize_sender(sender) if sender else None
        if item["sender_key"] and sender_key != item["sender_key"]:
            return "held", "origin_changed", None
        # Bind independently verified partial metadata before waiting. A late
        # handle can reserve its sender even while the chat join is absent,
        # and a late chat can reserve its chat while the handle is absent.
        ledger.execute("UPDATE inbound_messages SET sender=?,sender_key=?,chat_identifier=? WHERE message_guid=?", (sender, sender_key, chat, guid))
        if not sender or not chat:
            return self._waiting(item, now, "origin_not_ready")
        newer = ledger.execute("""SELECT 1 FROM inbound_messages
            WHERE (chat_identifier=? OR sender_key=?)
            AND source_rowid>? AND claimed_at IS NOT NULL LIMIT 1""",
            (chat, sender_key, item["source_rowid"])).fetchone()
        if newer:
            return "held", "origin_arrived_after_newer_work", None
        attachments = source.execute("""SELECT a.filename,a.mime_type FROM message_attachment_join j
            JOIN attachment a ON a.ROWID=j.attachment_id WHERE j.message_id=? ORDER BY a.ROWID""",
                                     (item["source_rowid"],)).fetchall()
        images = [a for a in attachments if str(a["mime_type"] or "").lower().startswith("image/")
                  or Path(a["filename"] or "").suffix.lower() in _IMAGE_SUFFIXES]
        if data.get("cache_has_attachments") and not attachments:
            return self._waiting(item, now, "attachment_not_ready")
        image_path = None
        if images:
            image_path = os.path.expanduser(images[0]["filename"] or "")
            if not image_path or not Path(image_path).is_file() or not os.access(image_path, os.R_OK):
                return self._waiting(item, now, "image_not_readable")
        if not text and not image_path:
            return self._waiting(item, now, "content_not_ready")
        message = {"ROWID": item["source_rowid"], "guid": guid, "text": text,
            "is_from_me": 0, "date": raw_date, "sender": sender, "chat_identifier": chat,
            "associated_message_type": data.get("associated_message_type"),
            "associated_message_guid": data.get("associated_message_guid"),
            "image_path": image_path, "image_mime": images[0]["mime_type"] if images else None}
        if self.confirmation_guard and self.confirmation_guard(message):
            cutoff = ledger.execute("""SELECT MAX(confirmation_cutoff_rowid) FROM inbound_messages
                WHERE sender_key=? AND recovered=1""", (sender_key,)).fetchone()[0]
            if item["recovered"] or (cutoff is not None and item["source_rowid"] <= cutoff):
                return "held", "fresh_confirmation_required", None
        return "ready", None, message

    @staticmethod
    def _waiting(item, now, reason):
        return ("held" if now - item["observed_at"] >= READINESS_MAX_AGE else "pending", reason, None)

    def claim_next(self, *, admission=None):
        """Resolve source data and durably claim one identity before returning it."""
        try:
            with self._transaction() as ledger, self._source() as source:
                checkpoint = ledger.execute("SELECT * FROM inbound_source WHERE id=1").fetchone()
                if checkpoint is None or checkpoint["initialized_at"] is None:
                    if checkpoint:
                        self._check_session(checkpoint)
                    return None
                self._check_source(source, checkpoint)
                # A failed acknowledgement leaves an uncertain live claim. Do
                # not let later messages authorize its partial pending state.
                blocked_chats = {row[0] for row in ledger.execute("""SELECT DISTINCT chat_identifier
                    FROM inbound_messages WHERE state='processing' AND chat_identifier IS NOT NULL""")}
                blocked_senders = {row[0] for row in ledger.execute("""SELECT DISTINCT sender_key
                    FROM inbound_messages WHERE state='processing' AND sender_key IS NOT NULL""")}
                items = ledger.execute("SELECT * FROM inbound_messages WHERE state='pending' ORDER BY source_rowid LIMIT ?", (MAX_PENDING_MESSAGES,)).fetchall()
                now = self.now()
                for item in items:
                    state, reason, message = self._inspect_pending(source, ledger, item, now)
                    current = ledger.execute("SELECT chat_identifier,sender_key FROM inbound_messages WHERE message_guid=?", (item["message_guid"],)).fetchone()
                    chat, sender_key = current
                    if state != "ready":
                        self._set_state(ledger, item["message_guid"], state, reason, now)
                        if state == "pending":
                            if chat:
                                blocked_chats.add(chat)
                            if sender_key:
                                blocked_senders.add(sender_key)
                        continue
                    if chat in blocked_chats or sender_key in blocked_senders:
                        # Carry both reservations forward through overlapping
                        # chat/sender chains; later messages cannot bypass this one.
                        blocked_chats.add(chat)
                        blocked_senders.add(sender_key)
                        continue
                    # Pool stop and claim admission share a narrow lock. Source
                    # resolution, transaction acquisition and execution do not.
                    with admission() if admission else nullcontext(True) as allowed:
                        if not allowed:
                            return None
                        ledger.execute("""UPDATE inbound_messages SET state='processing',reason=NULL,
                            claimed_at=?,updated_at=? WHERE message_guid=? AND state='pending'""", (now, now, item["message_guid"]))
                    return message
                return None
        except (OSError, sqlite3.Error, InboxSourceError) as error:
            self._record_error(error)
            raise

    def finish(self, guid, *, uncertain=False):
        with self._transaction() as conn:
            item = conn.execute("SELECT recovered FROM inbound_messages WHERE message_guid=? AND state='processing'", (guid,)).fetchone()
            cutoff = None
            if item and item["recovered"]:
                with self._source() as source:
                    checkpoint = conn.execute("SELECT * FROM inbound_source WHERE id=1").fetchone()
                    self._check_source(source, checkpoint)
                    cutoff = source.execute("SELECT COALESCE(MAX(ROWID),0) FROM message").fetchone()[0]
                # ROWID is an arrival fence, independent of timestamp precision
                # or clock changes. If this read fails the claim stays processing.
            conn.execute("""UPDATE inbound_messages SET state=?,reason=?,updated_at=?,confirmation_cutoff_rowid=?
                WHERE message_guid=? AND state='processing'""",
                ("uncertain" if uncertain else "handler_returned", "dispatch_exception" if uncertain else None,
                 self.now(), cutoff, guid))

    def dispatch_ready(self, handler, *, limit=DISPATCH_LIMIT, admission=None):
        count = 0
        for _ in range(limit):
            message = self.claim_next(admission=admission) if admission else self.claim_next()
            if message is None:
                break
            try:
                handler(message)
            except BaseException:
                self.finish(message["guid"], uncertain=True)
                raise
            self.finish(message["guid"])
            count += 1
        return count
