"""Bounded catch-up and durable send claims for the three legacy cron actions."""

from datetime import datetime, timedelta, timezone
import json
import logging
import re
import uuid
from zoneinfo import ZoneInfo

from .db import connect_bot_db


logger = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")
LEGACY_ACTIONS = frozenset({"morning_message", "drift_check", "sports_recap"})
GRACE = timedelta(minutes=15)
PREPARATION_LEASE = timedelta(minutes=3)
RETRY_DELAY = timedelta(seconds=60)
MAX_PREPARATION_ATTEMPTS = 3
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def due_occurrence(expression: str, now: datetime, effective_at: str):
    """Latest valid Pacific occurrence, first DST fold only, within the grace."""
    match = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?:\s+([a-z]+))?", (expression or "").strip().lower())
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    day = match[3]
    if hour > 23 or minute > 59:
        return None
    if day and day not in {*_DAYS, *(name[:3] for name in _DAYS)}:
        return None
    effective = _parse_stamp(effective_at)
    current = now.astimezone(timezone.utc)
    local_date = current.astimezone(PACIFIC).date()
    for date in (local_date, local_date - timedelta(days=1)):
        if day and date.weekday() != next(i for i, name in enumerate(_DAYS) if name.startswith(day)):
            continue
        naive = datetime(date.year, date.month, date.day, hour, minute)
        due = naive.replace(tzinfo=PACIFIC, fold=0).astimezone(timezone.utc)
        # Spring-forward wall times that never occur must not shift silently.
        if due.astimezone(PACIFIC).replace(tzinfo=None) != naive:
            continue
        if effective <= due <= current and current - due <= GRACE:
            return f"{date.isoformat()} {hour:02d}:{minute:02d}", due
    return None


def _job(conn, job_id):
    return conn.execute("""SELECT j.cron_expression,j.action_type,j.action_payload,
        j.enabled,j.last_run,s.revision,s.effective_at
        FROM cron_jobs j JOIN cron_schedule_state s ON s.job_id=j.id WHERE j.id=?""",
        (job_id,)).fetchone()


def claim_due(db_path: str, job_id: int, now: datetime):
    with connect_bot_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _job(conn, job_id)
        if not row or not row[3] or row[1] not in LEGACY_ACTIONS:
            return None
        occurrence = due_occurrence(row[0], now, row[6])
        if occurrence is None:
            return None
        key, due = occurrence
        # Also respect successes from an older binary during a rollback.
        if row[4] and _parse_stamp(row[4]) >= due:
            return None
        identity = (job_id, row[5], key)
        previous = conn.execute("""SELECT status,attempts,updated_at FROM cron_occurrences
            WHERE job_id=? AND revision=? AND occurrence_key=?""", identity).fetchone()
        if previous:
            status, attempts, updated = previous
            if status not in {"preparing", "preparation_failed"} or attempts >= MAX_PREPARATION_ATTEMPTS:
                return None
            delay = PREPARATION_LEASE if status == "preparing" else RETRY_DELAY
            if now.astimezone(timezone.utc) - _parse_stamp(updated) < delay:
                return None
        else:
            attempts = 0
        token = uuid.uuid4().hex
        conn.execute("""INSERT INTO cron_occurrences
            (job_id,revision,occurrence_key,due_at,status,attempt_id,attempts,updated_at)
            VALUES (?,?,?,?,'preparing',?,?,?)
            ON CONFLICT(job_id,revision,occurrence_key) DO UPDATE SET
                status='preparing',attempt_id=excluded.attempt_id,
                attempts=excluded.attempts,updated_at=excluded.updated_at,error_code=NULL""",
            (*identity, _stamp(due), token, attempts + 1, _stamp(now)))
        return {"identity": identity, "token": token, "row": row,
                "expression": row[0], "action": row[1], "raw_payload": row[2]}


def _transition(db_path, claim, status, now, *, expected_status, error_code=None, verify_job=False, completed=False):
    with connect_bot_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if verify_job:
            current = _job(conn, claim["identity"][0])
            # last_run may only change through another completion; all other
            # fields and the revision must still be the originally claimed row.
            if current != claim["row"] or not current[3]:
                return False
        cursor = conn.execute("""UPDATE cron_occurrences SET status=?,updated_at=?,error_code=?
            WHERE job_id=? AND revision=? AND occurrence_key=? AND attempt_id=? AND status=?""",
            (status, _stamp(now), error_code, *claim["identity"], claim["token"], expected_status))
        if cursor.rowcount != 1:
            return False
        if completed:
            conn.execute("UPDATE cron_jobs SET last_run=? WHERE id=?", (_stamp(now), claim["identity"][0]))
        return True


def _prepare(claim, now_pt, owner_id):
    payload = json.loads(claim["raw_payload"] or "{}")
    recipient = payload.get("recipient") if isinstance(payload, dict) else None
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("invalid_recipient")
    if claim["action"] == "morning_message":
        from .tools import _get_inspirational_quote, _render_morning_message_body
        body = _render_morning_message_body(payload, _get_inspirational_quote(), now_pt=now_pt)
    elif claim["action"] == "sports_recap":
        from .tools import _get_sports_recap
        body = _get_sports_recap(now_pt=now_pt)
    else:
        from .commands import _cmd_drift
        body = "Weekly drift check\n\n" + _cmd_drift(owner_id)
    if not isinstance(body, str) or not body.strip():
        raise ValueError("empty_body")
    return recipient, body


def run_due(db_path: str, job_id: int, now: datetime, send, on_submitted, *, owner_id: str, clock=None) -> bool:
    """At most one outbound attempt per occurrence; uncertain sends stay claimed."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    claim = None
    sending = False
    try:
        claim = claim_due(db_path, job_id, now)
        if claim is None:
            return False
        recipient, body = _prepare(claim, now.astimezone(PACIFIC), owner_id)
        if not _transition(db_path, claim, "sending", clock(), expected_status="preparing", verify_job=True):
            return False
        # A persisted sending claim is intentionally never retried after a
        # process crash or ambiguous AppleScript result.
        sending = True
        accepted = send(recipient, body, is_group=bool(re.fullmatch(r"[0-9a-fA-F]{32}", recipient)), recovery_mode="none")
        if accepted is not True:
            _transition(db_path, claim, "uncertain", clock(), expected_status="sending", error_code="send_unverified")
            logger.warning("Cron #%d delivery unverified; occurrence will not be retried", job_id)
            return False
        if not _transition(db_path, claim, "completed", clock(), expected_status="sending", verify_job=True, completed=True):
            logger.warning("Cron #%d accepted but completion checkpoint unverified", job_id)
            return False
        if on_submitted is not None:
            try:
                on_submitted(job_id, recipient, body)
            except Exception:
                logger.warning("Cron #%d accepted but history save failed", job_id)
        return True
    except Exception:
        if claim is not None:
            try:
                _transition(db_path, claim, "uncertain" if sending else "preparation_failed", clock(),
                            expected_status="sending" if sending else "preparing",
                            error_code="send_or_checkpoint_unverified" if sending else "preparation_failed")
            except Exception:
                pass  # The committed sending claim still prevents a duplicate.
        logger.warning("Cron #%d %s", job_id, "delivery or checkpoint unverified" if sending else "preparation failed")
        return False
