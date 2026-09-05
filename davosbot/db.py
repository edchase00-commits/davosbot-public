import json
import logging
import re
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .config import BOT_DB_PATH

logger = logging.getLogger(__name__)

_DB_PATH = Path(BOT_DB_PATH)
_BACKUPS_DIR = _DB_PATH.parent / "backups"
_BACKUP_KEEP_DAYS = 30
_BACKUP_KEEP_MIN = 5
_CREATE_IF_NOT_EXISTS_RE = re.compile(
    r"^\s*CREATE\s+(TABLE|(?:UNIQUE\s+)?INDEX)\s+IF\s+NOT\s+EXISTS\s+"
    r"[`\"\[]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


@contextmanager
def connect_bot_db(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a bot DB transaction and always close the SQLite connection."""
    conn = sqlite3.connect(str(path or BOT_DB_PATH))
    try:
        with conn:
            yield conn
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()


def backup_database() -> str:
    """Copy davosbot.db to backups/davosbot_YYYYMMDD_HHMMSS.db.

    Deduplicates within the same second: if a file with the current timestamp
    already exists (e.g. called multiple times during one startup), returns its
    path without copying again. This ensures one backup per startup rather than
    one per migration statement.
    """
    _BACKUPS_DIR.mkdir(exist_ok=True)
    if not _DB_PATH.exists():
        logger.info("DB backup skipped: %s does not exist yet", _DB_PATH.name)
        return ""

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = _BACKUPS_DIR / f"davosbot_{ts}.db"
    if dest.exists():
        return str(dest)
    shutil.copy2(_DB_PATH, dest)
    logger.info("DB backed up ? %s", dest.name)
    return str(dest)


def _create_object_already_exists(sql: str) -> bool:
    """Return True when an idempotent CREATE targets an existing object."""
    match = _CREATE_IF_NOT_EXISTS_RE.match(sql or "")
    if not match or not Path(BOT_DB_PATH).exists():
        return False

    object_kind = match.group(1).upper()
    object_type = "index" if "INDEX" in object_kind else "table"
    object_name = match.group(2)
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ? LIMIT 1",
                (object_type, object_name),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def run_migration(sql: str, description: str) -> None:
    """Back up davosbot.db, execute one DDL statement, and log the event.

    Raises on SQL failure — never silently swallows schema errors.
    The backup is deduplicated so multiple calls within the same second
    share the same backup file.
    """
    if _create_object_already_exists(sql):
        logger.debug("Migration already applied - '%s'", description)
        return

    backup_path = backup_database()

    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        conn.execute(sql)
        conn.commit()
    except Exception as exc:
        logger.error("Migration FAILED — '%s': %s", description, exc)
        raise
    finally:
        conn.close()

    logger.debug("Migration OK — '%s'", description)

    # Write to bot_log if the table exists yet (early migrations may run before it's created).
    conn = sqlite3.connect(BOT_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
            (
                "system",
                "migration",
                json.dumps({"description": description, "backup": backup_path}),
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def cleanup_old_backups() -> None:
    """Delete backups older than 30 days, always keeping the 5 most recent."""
    if not _BACKUPS_DIR.exists():
        return

    backups = sorted(
        _BACKUPS_DIR.glob("davosbot_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # newest first
    )

    cutoff = datetime.now() - timedelta(days=_BACKUP_KEEP_DAYS)
    removed = 0
    for i, path in enumerate(backups):
        if i < _BACKUP_KEEP_MIN:
            continue  # always keep the most recent _BACKUP_KEEP_MIN files
        if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            path.unlink()
            removed += 1

    if removed:
        logger.info(
            "Backup cleanup: removed %d file(s) older than %d days",
            removed,
            _BACKUP_KEEP_DAYS,
        )
