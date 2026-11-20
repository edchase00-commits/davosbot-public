"""Durable orchestration for the fixed November 20-21 ESPN parlay watch.

This module deliberately knows nothing about league scoring or Messages.app.
``espn_parlay`` owns provider truth, grading, and text formatting;
``live_score_delivery`` owns the native route and send receipts.  Keeping those
boundaries injectable makes the timing and restart rules testable without
network or Messages access.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
LIVE_ALERT_INTERVAL_SECONDS = 600.0
LIVE_POLL_SECONDS = 15.0
RECEIPT_POLL_SECONDS = 15.0
TERMINAL_FIELD_GRACE_SECONDS = 120.0
ROUTE_BOOTSTRAP_TTL_SECONDS = 600.0
MAX_LOOP_SLEEP_SECONDS = 60.0
_SENT_STATES = {"sent", "confirmed"}
_UNRESOLVED_STATES = {
    "unsubmitted", "attempting", "pending", "unknown", "waiting_route",
    "failed", "error",
}


class MonitorError(RuntimeError):
    """A bounded operational error safe to place in status output."""


@dataclass(frozen=True)
class CycleResult:
    status: dict[str, Any]
    complete: bool
    next_poll_at: float


def _event_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _event_key(spec: Any) -> str:
    key = _event_value(spec, "key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", key):
        raise MonitorError("invalid_event_key")
    return key


def _safe_code(error: BaseException) -> str:
    value = getattr(error, "code", None) or str(error)
    if isinstance(value, str):
        value = value.strip().lower().replace(" ", "_")
        if re.fullmatch(r"[a-z0-9_.:-]{1,120}", value):
            return value
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    return name[:120] or "provider_error"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _as_epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    elif hasattr(value, "_asdict"):
        value = value._asdict()
    elif not isinstance(value, (dict, list, tuple, str, int, float, bool, type(None))):
        value = getattr(value, "__dict__", str(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class JsonMonitorState:
    """Small atomic state file; delivery receipts remain authoritative."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self, event_keys: list[str], now: float) -> dict[str, Any]:
        if not self.path.exists():
            return self._new(event_keys, now)
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 2_000_000:
                raise ValueError
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise MonitorError("monitor_state_needs_review") from exc
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != SCHEMA_VERSION
            or state.get("event_keys") != event_keys
            or not isinstance(state.get("events"), dict)
        ):
            raise MonitorError("monitor_state_mismatch")
        for key in event_keys:
            event = state["events"].get(key)
            if not isinstance(event, dict) or not isinstance(event.get("alerts", {}), dict):
                raise MonitorError("monitor_state_needs_review")
        return state

    @staticmethod
    def _new(event_keys: list[str], now: float) -> dict[str, Any]:
        events = {}
        for key in event_keys:
            events[key] = {
                "settled": False,
                "provider_state": "unknown",
                "last_polled_at": None,
                "next_provider_poll_at": 0.0,
                "last_live_alert_at": None,
                "live_sequence": 0,
                "final_alert_id": None,
                "terminal_pending_since": None,
                "forced_incomplete": False,
                "unsupported_fields": [],
                "last_error": None,
                "alerts": {},
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "event_keys": event_keys,
            "created_at": now,
            "updated_at": now,
            "last_poll_at": None,
            "events": events,
        }

    def save(self, state: dict[str, Any]) -> None:
        _atomic_json(self.path, state)


class LiveScoreMonitor:
    """Poll provider state, claim due alerts durably, and reconcile delivery."""

    def __init__(
        self,
        event_specs: Iterable[Any],
        fetch_snapshot: Callable[[Any], Any],
        format_alert: Callable[[Any, Any], str],
        is_settled: Callable[[Any, Any], bool],
        delivery: Any,
        state_path: Path,
        status_path: Path,
        *,
        is_terminal_boundary: Callable[[Any, Any], bool] | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
        live_alert_interval: float = LIVE_ALERT_INTERVAL_SECONDS,
        expected_event_count: int | None = None,
        expected_pick_count: int | None = None,
    ):
        self.specs = tuple(event_specs)
        self.keys = [_event_key(spec) for spec in self.specs]
        if len(set(self.keys)) != len(self.keys):
            raise MonitorError("duplicate_event_key")
        if expected_event_count is not None and len(self.keys) != expected_event_count:
            raise MonitorError("unexpected_event_count")
        self.pick_count = sum(len(_event_value(spec, "picks", ()) or ()) for spec in self.specs)
        if expected_pick_count is not None and self.pick_count != expected_pick_count:
            raise MonitorError("unexpected_pick_count")
        self.fetch_snapshot = fetch_snapshot
        self.format_alert = format_alert
        self.is_settled = is_settled
        self.is_terminal_boundary = is_terminal_boundary or is_settled
        self.delivery = delivery
        self.state_store = JsonMonitorState(Path(state_path))
        self.status_path = Path(status_path)
        self.clock = clock
        self.sleeper = sleeper
        self.logger = logger or logging.getLogger(__name__)
        self.live_alert_interval = float(live_alert_interval)
        if self.live_alert_interval <= 0:
            raise MonitorError("invalid_live_alert_interval")

    def _route_status(self) -> dict[str, Any]:
        if self.delivery is None:
            return {"ready": False, "mode": "none", "reason": "delivery_disabled"}
        try:
            status = self.delivery.route_status()
            result = _json_value(status)
            if not isinstance(result, dict):
                raise TypeError
            result["ready"] = bool(result.get("ready"))
            return result
        except AttributeError:
            guid = getattr(self.delivery, "bound_group_guid", None)
            return {
                "ready": bool(guid),
                "mode": "group" if guid else "unknown",
                "group_guid": guid,
                "reason": None if guid else "route_status_unavailable",
            }
        except Exception as exc:
            return {"ready": False, "mode": "unknown", "reason": _safe_code(exc)}

    @staticmethod
    def _receipt(receipt: Any) -> dict[str, Any]:
        raw = _json_value(receipt)
        if not isinstance(raw, dict):
            return {"state": "unknown", "outcome": "invalid_receipt"}
        output = {}
        for name in (
            "alert_id", "destination", "state", "outcome", "watermark",
            "receipt_rowid", "attempted_at",
        ):
            if name in raw:
                output[name] = raw[name]
        output["state"] = str(output.get("state", "unknown")).lower()
        return output

    @staticmethod
    def _receipt_state(alert: dict[str, Any]) -> str:
        if alert.get("expired_no_backfill"):
            return "expired_no_backfill"
        receipts = alert.get("receipts") or []
        if not receipts:
            return str(alert.get("receipt_state") or "unsubmitted")
        states = {str(item.get("state", "unknown")).lower() for item in receipts}
        if states and states <= _SENT_STATES:
            return "sent"
        if "failed" in states:
            return "failed"
        for candidate in ("waiting_route", "unknown", "pending", "attempting"):
            if candidate in states:
                return candidate
        return sorted(states)[0] if states else "unknown"

    def _save(self, state: dict[str, Any], now: float) -> None:
        state["updated_at"] = now
        self.state_store.save(state)

    @staticmethod
    def _delivery_anchor(alert: dict[str, Any]) -> float | None:
        attempts = []
        for receipt in alert.get("receipts") or []:
            attempted = _as_epoch(receipt.get("attempted_at"))
            if attempted is not None and attempted > 0:
                attempts.append(attempted)
        if attempts:
            return max(attempts)
        sent_at = _as_epoch(alert.get("sent_at"))
        return sent_at if sent_at is not None and sent_at > 0 else None

    def _record_delivery_timing(self, alert: dict[str, Any], now: float) -> None:
        if self._receipt_state(alert) == "sent" and _as_epoch(alert.get("sent_at")) is None:
            # Some native/bootstrap receipts have no attempted_at. The first
            # monitor observation of their confirmed row becomes the cadence
            # anchor instead of the cycle that originally claimed the alert.
            alert["sent_at"] = now
        anchor = self._delivery_anchor(alert)
        if anchor is not None:
            alert["cadence_anchor_at"] = anchor

    def _deliver_alert(self, state: dict[str, Any], alert: dict[str, Any], now: float) -> None:
        if alert.get("expired_no_backfill"):
            return
        current = self._receipt_state(alert)
        if current == "sent":
            before = alert.get("sent_at")
            self._record_delivery_timing(alert, now)
            if alert.get("sent_at") != before:
                self._save(state, now)
            return
        # A live score that waited for a route is stale.  Never flush it after
        # approval; the next provider poll will create one fresh current alert.
        if alert.get("kind") == "live" and current == "waiting_route" and not alert.get("route_bootstrap"):
            alert["expired_no_backfill"] = True
            alert["receipt_state"] = "expired_no_backfill"
            self._save(state, now)
            return
        try:
            receipts = self.delivery.deliver(alert["alert_id"], alert["text"])
            converted = [self._receipt(item) for item in (receipts or [])]
            if converted:
                alert["receipts"] = converted
            alert["receipt_state"] = self._receipt_state(alert)
            self._record_delivery_timing(alert, now)
            alert["last_delivery_check_at"] = now
            alert["delivery_error"] = None
        except Exception as exc:
            alert["receipt_state"] = "unknown"
            alert["delivery_error"] = _safe_code(exc)
            alert["last_delivery_check_at"] = now
        self._save(state, now)

    def _reconcile(self, state: dict[str, Any], route: dict[str, Any], now: float) -> None:
        for key in self.keys:
            event = state["events"][key]
            for alert in event["alerts"].values():
                current = self._receipt_state(alert)
                if current not in _UNRESOLVED_STATES:
                    continue
                if alert.get("kind") == "live" and not route.get("ready") and not alert.get("route_bootstrap"):
                    continue
                self._deliver_alert(state, alert, now)

    @staticmethod
    def _provider_state(snapshot: Any) -> str:
        state = str(_event_value(snapshot, "state", "unknown") or "unknown").lower()
        if state in {"in", "live", "in_progress", "in-progress"}:
            return "in"
        if state in {"post", "final", "complete", "completed"}:
            return "post"
        if state in {"pre", "scheduled"}:
            return "pre"
        return state[:80]

    @staticmethod
    def _is_live(snapshot: Any) -> bool:
        explicit = _event_value(snapshot, "is_live", None)
        if isinstance(explicit, bool):
            return explicit
        return LiveScoreMonitor._provider_state(snapshot) == "in"

    @staticmethod
    def _unsupported(snapshot: Any) -> list[str]:
        values = (
            _event_value(snapshot, "unsupported_fields", None)
            or _event_value(snapshot, "diagnostics", ())
            or ()
        )
        if isinstance(values, str):
            values = [values]
        return sorted({str(value)[:120] for value in values if value})

    def _poll_interval(self, snapshot: Any, now: float, live_due_at: float | None = None) -> float:
        if self._is_live(snapshot):
            interval = LIVE_POLL_SECONDS
            if live_due_at is not None:
                interval = min(interval, max(1.0, live_due_at - now))
            return interval
        scheduled = _as_epoch(
            _event_value(snapshot, "scheduled_at", None)
            or _event_value(snapshot, "start_time", None)
            or _event_value(snapshot, "date", None)
        )
        if scheduled is None:
            return 300.0
        remaining = scheduled - now
        if remaining > 43_200:
            return 1_800.0
        if remaining > 7_200:
            return 600.0
        if remaining > 1_800:
            return 120.0
        if remaining > 300:
            return 30.0
        # Scheduled time only changes polling frequency.  It never marks the
        # event live, creates an alert, or settles a pick.
        return 15.0 if remaining > -7_200 else 60.0

    def _claim_alert(
        self,
        state: dict[str, Any],
        event: dict[str, Any],
        key: str,
        kind: str,
        text: str,
        now: float,
        *,
        route_bootstrap: bool = False,
    ) -> dict[str, Any]:
        if kind == "final":
            alert_id = f"{key}:final"
            event["final_alert_id"] = alert_id
        else:
            event["live_sequence"] = int(event.get("live_sequence") or 0) + 1
            alert_id = f"{key}:live:{event['live_sequence']}"
        existing = event["alerts"].get(alert_id)
        if existing:
            if existing.get("text") != text:
                raise MonitorError("claimed_alert_text_changed")
            return existing
        alert = {
            "alert_id": alert_id,
            "kind": kind,
            "text": text,
            "claimed_at": now,
            "receipt_state": "unsubmitted",
            "receipts": [],
        }
        if route_bootstrap:
            alert["route_bootstrap"] = True
        event["alerts"][alert_id] = alert
        self._save(state, now)  # claim before any possible native side effect
        return alert

    def _pending_route_bootstrap(self, state: dict[str, Any]) -> dict[str, Any] | None:
        for key in self.keys:
            for alert in state["events"][key]["alerts"].values():
                if (
                    alert.get("route_bootstrap")
                    and not alert.get("expired_no_backfill")
                    and self._receipt_state(alert) != "sent"
                ):
                    return alert
        return None

    def _expire_bootstrap(
        self,
        state: dict[str, Any],
        alert: dict[str, Any],
        now: float,
        reason: str,
    ) -> None:
        if (
            not alert.get("route_bootstrap")
            or alert.get("expired_no_backfill")
            or self._receipt_state(alert) == "sent"
        ):
            return
        alert["expired_no_backfill"] = True
        alert["expired_at"] = now
        alert["expiry_reason"] = reason
        alert["receipt_state"] = "expired_no_backfill"
        self._save(state, now)

    def _expire_stale_bootstraps(self, state: dict[str, Any], now: float) -> None:
        for key in self.keys:
            for alert in state["events"][key]["alerts"].values():
                claimed = _as_epoch(alert.get("claimed_at"))
                if (
                    alert.get("route_bootstrap")
                    and claimed is not None
                    and now >= claimed + ROUTE_BOOTSTRAP_TTL_SECONDS
                ):
                    self._expire_bootstrap(state, alert, now, "approval_timeout")

    def _expire_event_bootstrap(self, state: dict[str, Any], event: dict[str, Any], now: float) -> None:
        for alert in event["alerts"].values():
            self._expire_bootstrap(state, alert, now, "provider_no_longer_live")

    def _live_alert_due(
        self,
        event: dict[str, Any],
        route_ready: bool,
        now: float,
        *,
        bootstrap_pending: bool,
    ) -> bool:
        live_alerts = [
            alert for alert in event["alerts"].values()
            if alert.get("kind") == "live" and not alert.get("expired_no_backfill")
        ]
        # Exactly one first real-live observation is allowed to reach delivery
        # without a route.  Its waiting receipt/watermark is the native UI
        # approval bootstrap.  Later live updates wait for a ready route.
        if not live_alerts:
            return route_ready or not bootstrap_pending
        if not route_ready:
            return False
        if live_alerts:
            latest = max(live_alerts, key=lambda item: float(item.get("claimed_at") or 0))
            if latest.get("expired_no_backfill") or self._receipt_state(latest) == "waiting_route":
                return True
        anchor = self._latest_live_delivery_anchor(event)
        # A claimed alert with no attempt/receipt anchor stays the one durable
        # in-flight alert. Reconciliation, not another alert ID, resolves it.
        return anchor is not None and now >= anchor + self.live_alert_interval

    def _latest_live_delivery_anchor(self, event: dict[str, Any]) -> float | None:
        anchors = [
            self._delivery_anchor(alert)
            for alert in event["alerts"].values()
            if alert.get("kind") == "live" and not alert.get("expired_no_backfill")
        ]
        present = [anchor for anchor in anchors if anchor is not None]
        return max(present) if present else None

    def _poll_event(
        self,
        state: dict[str, Any],
        spec: Any,
        route: dict[str, Any],
        now: float,
        *,
        no_send: bool,
        previews: dict[str, str],
    ) -> None:
        key = _event_key(spec)
        event = state["events"][key]
        try:
            snapshot = self.fetch_snapshot(spec)
            event["last_error"] = None
            event["provider_state"] = self._provider_state(snapshot)
            event["unsupported_fields"] = self._unsupported(snapshot)
            event["last_status_name"] = str(_event_value(snapshot, "status_name", ""))[:120]
            event["last_status_detail"] = str(_event_value(snapshot, "status_detail", ""))[:160]
            event["last_polled_at"] = now
            text = self.format_alert(spec, snapshot)
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise MonitorError("invalid_formatted_alert")
            previews[key] = text
            settled = bool(self.is_settled(spec, snapshot))
            terminal_boundary = bool(self.is_terminal_boundary(spec, snapshot))
            if not no_send and (not self._is_live(snapshot) or terminal_boundary):
                self._expire_event_bootstrap(state, event, now)
            if (
                not no_send
                and event.get("terminal_pending_since") is not None
                and not terminal_boundary
            ):
                event["terminal_pending_since"] = None
                event["terminal_pending_reset_at"] = now
                self._save(state, now)
            if no_send:
                event["preflight_settled"] = settled
                event["preflight_terminal_boundary"] = terminal_boundary
                if terminal_boundary and not settled:
                    event["next_provider_poll_at"] = now + RECEIPT_POLL_SECONDS
                    return
            elif settled:
                # A final result is never used as an approval bootstrap and is
                # never handed to delivery without a route. Keep polling it so
                # the first post-approval send is one fresh final result.
                if not route.get("ready"):
                    event["next_provider_poll_at"] = now + RECEIPT_POLL_SECONDS
                    self._save(state, now)
                    return
                alert = self._claim_alert(state, event, key, "final", text, now)
                event["settled"] = True
                event["settled_at"] = now
                event["forced_incomplete"] = False
                if event.get("terminal_pending_since") is not None:
                    event["terminal_pending_resolved_at"] = now
                event["next_provider_poll_at"] = None
                self._save(state, now)
                self._deliver_alert(state, alert, now)
                return
            elif terminal_boundary:
                if event.get("terminal_pending_since") is None:
                    event["terminal_pending_since"] = now
                    self._save(state, now)
                pending_since = float(event["terminal_pending_since"])
                if now < pending_since + TERMINAL_FIELD_GRACE_SECONDS:
                    event["next_provider_poll_at"] = now + RECEIPT_POLL_SECONDS
                    self._save(state, now)
                    return
                if not route.get("ready"):
                    event["next_provider_poll_at"] = now + RECEIPT_POLL_SECONDS
                    self._save(state, now)
                    return
                alert = self._claim_alert(state, event, key, "final", text, now)
                alert["forced_incomplete"] = True
                event["settled"] = True
                event["settled_at"] = now
                event["forced_incomplete"] = True
                event["forced_incomplete_at"] = now
                event["next_provider_poll_at"] = None
                self._save(state, now)
                self._deliver_alert(state, alert, now)
                return
            elif self._is_live(snapshot) and self._live_alert_due(
                event,
                bool(route.get("ready")),
                now,
                bootstrap_pending=self._pending_route_bootstrap(state) is not None,
            ):
                no_prior_live = not any(
                    item.get("kind") == "live" and not item.get("expired_no_backfill")
                    for item in event["alerts"].values()
                )
                alert = self._claim_alert(
                    state,
                    event,
                    key,
                    "live",
                    text,
                    now,
                    route_bootstrap=no_prior_live and not bool(route.get("ready")),
                )
                self._deliver_alert(state, alert, now)
            live_anchor = self._latest_live_delivery_anchor(event)
            event["last_live_alert_at"] = live_anchor
            live_due = live_anchor + self.live_alert_interval if live_anchor is not None else None
            event["next_provider_poll_at"] = now + self._poll_interval(snapshot, now, live_due)
        except Exception as exc:
            code = _safe_code(exc)
            event["last_error"] = code
            event["last_polled_at"] = now
            event["next_provider_poll_at"] = now + 60.0
            if (
                "unmatched" in code
                or "not_found" in code
                or "not-found" in code
                or code in {"identity_mismatch", "unknown_event"}
            ):
                event["provider_state"] = "unmatched"
        if not no_send:
            self._save(state, now)

    def _complete(self, state: dict[str, Any]) -> bool:
        for key in self.keys:
            event = state["events"][key]
            if not event.get("settled"):
                return False
            alert_id = event.get("final_alert_id")
            alert = event["alerts"].get(alert_id) if alert_id else None
            if not alert or self._receipt_state(alert) != "sent":
                return False
        return True

    def _next_poll(self, state: dict[str, Any], now: float, complete: bool) -> float:
        if complete:
            return now
        due = []
        for key in self.keys:
            event = state["events"][key]
            if not event.get("settled"):
                value = event.get("next_provider_poll_at")
                due.append(float(value) if isinstance(value, (int, float)) else now + 60.0)
                continue
            alert_id = event.get("final_alert_id")
            alert = event["alerts"].get(alert_id) if alert_id else None
            if not alert or self._receipt_state(alert) != "sent":
                due.append(now + RECEIPT_POLL_SECONDS)
        return min(due) if due else now + MAX_LOOP_SLEEP_SECONDS

    def _status(
        self,
        state: dict[str, Any],
        route: dict[str, Any],
        now: float,
        next_poll: float,
        complete: bool,
        no_send: bool,
        previews: dict[str, str],
    ) -> dict[str, Any]:
        events = {}
        unmatched = []
        unsupported = {}
        receipt_states = {}
        for key in self.keys:
            event = state["events"][key]
            if event.get("provider_state") == "unmatched":
                unmatched.append(key)
            if event.get("unsupported_fields"):
                unsupported[key] = list(event["unsupported_fields"])
            alerts = {alert_id: self._receipt_state(alert) for alert_id, alert in event["alerts"].items()}
            receipt_states.update(alerts)
            events[key] = {
                "provider_state": event.get("provider_state"),
                "settled": bool(event.get("settled")),
                "last_error": event.get("last_error"),
                "last_polled_at": _iso(event.get("last_polled_at")),
                "next_provider_poll_at": _iso(event.get("next_provider_poll_at")),
                "last_status_name": event.get("last_status_name"),
                "last_status_detail": event.get("last_status_detail"),
                "terminal_pending": bool(
                    event.get("terminal_pending_since") is not None
                    and not event.get("settled")
                ),
                "terminal_pending_since": _iso(event.get("terminal_pending_since")),
                "forced_incomplete": bool(event.get("forced_incomplete")),
                "alerts": alerts,
            }
            if no_send and key in previews:
                events[key]["sample_alert"] = previews[key]
        failed_final = [
            key for key in self.keys
            if state["events"][key].get("final_alert_id")
            and self._receipt_state(state["events"][key]["alerts"][state["events"][key]["final_alert_id"]]) == "failed"
        ]
        bootstrap_alerts = []
        for key in self.keys:
            for alert in state["events"][key]["alerts"].values():
                if (
                    not alert.get("route_bootstrap")
                    or alert.get("expired_no_backfill")
                    or self._receipt_state(alert) == "sent"
                ):
                    continue
                bootstrap_alerts.append({
                    "event_key": key,
                    "alert_id": alert.get("alert_id"),
                    "text": alert.get("text"),
                    "receipt_state": self._receipt_state(alert),
                    "watermarks": [
                        item.get("watermark") for item in alert.get("receipts", [])
                        if item.get("watermark") is not None
                    ],
                })
        forced_incomplete = [
            key for key in self.keys if state["events"][key].get("forced_incomplete")
        ]
        phase = "preflight" if no_send else "complete" if complete else "delivery_blocked" if failed_final else "running"
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": phase,
            "updated_at": _iso(now),
            "last_poll_at": _iso(state.get("last_poll_at")),
            "next_poll_at": _iso(next_poll),
            "event_count": len(self.keys),
            "pick_count": self.pick_count,
            "route": route,
            "unmatched_events": unmatched,
            "unsupported_fields": unsupported,
            "receipt_states": receipt_states,
            "failed_final_deliveries": failed_final,
            "forced_incomplete_settlements": forced_incomplete,
            "pending_route_bootstrap": bootstrap_alerts,
            "complete": complete,
            "no_send": no_send,
            "events": events,
        }

    def run_once(self, *, no_send: bool = False, force_poll: bool = False) -> CycleResult:
        now = float(self.clock())
        if not math.isfinite(now):
            raise MonitorError("invalid_clock")
        state = self.state_store.load(self.keys, now)
        route = self._route_status()
        if not no_send:
            self._reconcile(state, route, now)
            # Reconciliation can bind a newly-created exact group. Refresh so
            # other live events can send current alerts in this same cycle and
            # status reflects the route that delivery now owns.
            route = self._route_status()
            # Give an exact native UI send at the 600-second edge one final
            # receipt reconciliation before expiring/replacing its bootstrap.
            self._expire_stale_bootstraps(state, now)
        previews: dict[str, str] = {}
        polled = False
        for spec in self.specs:
            key = _event_key(spec)
            event = state["events"][key]
            if event.get("settled") and not no_send:
                continue
            next_due = event.get("next_provider_poll_at")
            if force_poll or next_due is None or float(next_due) <= now:
                polled = True
                self._poll_event(state, spec, route, now, no_send=no_send, previews=previews)
        if polled:
            state["last_poll_at"] = now
        if not no_send:
            route = self._route_status()
        complete = False if no_send else self._complete(state)
        next_poll = self._next_poll(state, now, complete)
        status = self._status(state, route, now, next_poll, complete, no_send, previews)
        if not no_send:
            self._save(state, now)
            _atomic_json(self.status_path, status)
        self.logger.info(
            "live-score cycle phase=%s events=%d unmatched=%d next_poll=%s route_ready=%s",
            status["phase"], len(self.keys), len(status["unmatched_events"]),
            status["next_poll_at"], bool(route.get("ready")),
        )
        return CycleResult(status=status, complete=complete, next_poll_at=next_poll)

    def run_forever(self) -> CycleResult:
        first = True
        while True:
            result = self.run_once(force_poll=first)
            first = False
            if result.complete:
                return result
            delay = max(1.0, min(MAX_LOOP_SLEEP_SECONDS, result.next_poll_at - float(self.clock())))
            self.sleeper(delay)
