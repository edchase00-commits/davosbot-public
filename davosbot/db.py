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


_LEGACY_CRON_ACTIONS_SQL = "('morning_message','drift_check','sports_recap')"
_CRON_SQL_NOW = "strftime('%Y-%m-%d %H:%M:%f','now')"


def init_legacy_cron_schema(db_path: str, migrate) -> None:
    """Use the caller's normal backed-up migration path; never rewrite cron rows."""
    migrate("""CREATE TABLE IF NOT EXISTS cron_schedule_state (
        job_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL,
        effective_at TEXT NOT NULL
    )""", "cron schedule effective boundaries")
    migrate("""CREATE TABLE IF NOT EXISTS cron_occurrences (
        job_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        occurrence_key TEXT NOT NULL, due_at TEXT NOT NULL,
        status TEXT NOT NULL, attempt_id TEXT NOT NULL,
        attempts INTEGER NOT NULL, updated_at TEXT NOT NULL,
        error_code TEXT,
        PRIMARY KEY (job_id, revision, occurrence_key)
    )""", "legacy cron occurrence claims")
    # Every existing writer, including Work edits, participates in the same
    # committed boundary. Research payload.run and successful last_run writes
    # must not count as schedule edits.
    triggers = {
        "cron_schedule_insert": f"""AFTER INSERT ON cron_jobs
            WHEN NEW.action_type IN {_LEGACY_CRON_ACTIONS_SQL}
            BEGIN
                INSERT INTO cron_schedule_state VALUES (NEW.id,1,{_CRON_SQL_NOW})
                ON CONFLICT(job_id) DO UPDATE SET
                    revision=revision+1,effective_at={_CRON_SQL_NOW};
            END""",
        "cron_schedule_update": f"""AFTER UPDATE OF cron_expression,action_type,action_payload,enabled ON cron_jobs
            WHEN (OLD.action_type IN {_LEGACY_CRON_ACTIONS_SQL} OR NEW.action_type IN {_LEGACY_CRON_ACTIONS_SQL})
                AND (OLD.cron_expression IS NOT NEW.cron_expression
                    OR OLD.action_type IS NOT NEW.action_type
                    OR OLD.action_payload IS NOT NEW.action_payload
                    OR OLD.enabled IS NOT NEW.enabled)
            BEGIN
                INSERT INTO cron_schedule_state VALUES (NEW.id,1,{_CRON_SQL_NOW})
                ON CONFLICT(job_id) DO UPDATE SET
                    revision=revision+1,effective_at={_CRON_SQL_NOW};
            END""",
        "cron_schedule_delete": f"""AFTER DELETE ON cron_jobs
            WHEN OLD.action_type IN {_LEGACY_CRON_ACTIONS_SQL}
            BEGIN
                UPDATE cron_schedule_state SET revision=revision+1,
                    effective_at={_CRON_SQL_NOW} WHERE job_id=OLD.id;
            END""",
    }
    with connect_bot_db(db_path) as conn:
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for name, definition in triggers.items():
        if name not in existing:
            migrate(f"CREATE TRIGGER {name} {definition}", f"{name} effective boundary")
    with connect_bot_db(db_path) as conn:
        missing = conn.execute(f"""SELECT 1 FROM cron_jobs j
            WHERE j.action_type IN {_LEGACY_CRON_ACTIONS_SQL}
            AND NOT EXISTS (SELECT 1 FROM cron_schedule_state s WHERE s.job_id=j.id)
            LIMIT 1""").fetchone()
    if missing:
        # First deployment does not replay occurrences predating activation.
        migrate(f"""INSERT INTO cron_schedule_state(job_id,revision,effective_at)
            SELECT j.id,1,{_CRON_SQL_NOW} FROM cron_jobs j
            WHERE j.action_type IN {_LEGACY_CRON_ACTIONS_SQL}
            AND NOT EXISTS (SELECT 1 FROM cron_schedule_state s WHERE s.job_id=j.id)""",
            "initialize legacy cron effective boundaries")


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
