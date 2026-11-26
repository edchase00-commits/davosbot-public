import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from .runtime_locks import personality_file_locked
from .config import SOUL_PATH, BOT_DB_PATH
from .db import connect_bot_db
from .permissions import is_owner

logger = logging.getLogger(__name__)

_SOUL_PATH = Path(SOUL_PATH)
_BACKUPS_DIR = _SOUL_PATH.parent / "backups"
_VERSION_RE = re.compile(r"<!-- v(\d+) \|")
_VERSION_FULL_RE = re.compile(r"<!-- (v\d+ \| .+?) -->")


@personality_file_locked
def read_soul() -> str:
    """Read SOUL.md and return its full contents.

    Raises FileNotFoundError with a clear message if the file is missing.
    """
    if not _SOUL_PATH.exists():
        raise FileNotFoundError(f"SOUL.md not found at {SOUL_PATH}")
    return _SOUL_PATH.read_text(encoding="utf-8")


@personality_file_locked
def _next_version(new_content: str) -> int:
    """Return the next version number, guaranteed monotonically increasing.

    Scans both the new content and the current SOUL.md (if it exists) to
    ensure restoring from an older backup never reuses a version number.
    """
    existing = ""
    if _SOUL_PATH.exists():
        try:
            existing = _SOUL_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
    versions = [int(m.group(1)) for m in _VERSION_RE.finditer(existing + "\n" + new_content)]
    return (max(versions) + 1) if versions else 1


def _log_soul_write(sender: str, reason: str, backup_path: str) -> None:
    try:
        with connect_bot_db(BOT_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO bot_log (sender, event_type, payload) VALUES (?, ?, ?)",
                (sender, "soul_write", json.dumps({"reason": reason, "backup": backup_path})),
            )
    except Exception as e:
        logger.warning("Failed to log soul_write to bot_log: %s", e)


@personality_file_locked
def write_soul(new_content: str, reason: str, sender: str) -> str:
    """Write new content to SOUL.md with validation, backup, versioning, and audit log.

    Steps:
      1. Validate sender is owner — raises PermissionError if not.
      2. Back up the current SOUL.md to backups/SOUL_YYYYMMDD_HHMMSS.md.
      3. Append a version comment to new_content.
      4. Write to SOUL.md.
      5. Log event_type='soul_write' to bot_log.

    Returns the backup filepath.
    """
    if not is_owner(sender):
        raise PermissionError(f"write_soul: {sender!r} is not the owner")

    _BACKUPS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = _BACKUPS_DIR / f"SOUL_{ts}.md"

    if _SOUL_PATH.exists():
        shutil.copy2(_SOUL_PATH, backup_path)
        logger.info("SOUL.md backed up ? %s", backup_path.name)

    version = _next_version(new_content)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    version_comment = f"<!-- v{version} | {timestamp} | {sender} | {reason} -->"
    final_content = new_content.rstrip() + "\n" + version_comment + "\n"

    _SOUL_PATH.write_text(final_content, encoding="utf-8")
    logger.info("SOUL.md written: v%d, reason=%r", version, reason)

    _log_soul_write(sender=sender, reason=reason, backup_path=str(backup_path))
    return str(backup_path)


@personality_file_locked
def restore_soul_from_backup(backup_name: str, sender: str) -> str:
    """Copy a named SOUL backup file back to SOUL.md.

    Validates ownership, uses write_soul so the restore itself is versioned and audited.
    Returns a status message.
    """
    backup_path = _BACKUPS_DIR / backup_name
    if not backup_path.exists():
        # Accept bare name with or without directory prefix
        matches = list(_BACKUPS_DIR.glob(f"*{backup_name}*"))
        if matches:
            backup_path = matches[0]
        else:
            return f"Backup not found: {backup_name}"

    content = backup_path.read_text(encoding="utf-8")
    write_soul(content, f"restored from {backup_path.name}", sender=sender)
    return f"Restored SOUL.md from {backup_path.name}."


@personality_file_locked
def restore_soul_from_latest_backup() -> str | None:
    """Emergency startup restore: copy the most recent SOUL_*.md backup to SOUL.md.

    Bypasses owner validation — only called at startup when SOUL.md is
    empty or missing. Returns the backup filename used, or None if no backup exists.
    """
    if not _BACKUPS_DIR.exists():
        return None

    soul_backups = sorted(
        _BACKUPS_DIR.glob("SOUL_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not soul_backups:
        return None

    latest = soul_backups[0]
    shutil.copy2(latest, _SOUL_PATH)
    logger.info("Emergency SOUL.md restore from %s", latest.name)
    return latest.name
