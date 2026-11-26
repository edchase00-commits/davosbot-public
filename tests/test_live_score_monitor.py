from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import unittest

from davosbot.live_score_monitor import LiveScoreMonitor, MonitorError


@dataclass(frozen=True)
class Spec:
    key: str


@dataclass
class Snapshot:
    state: str
    is_live: bool = False
    scheduled_at: float | None = None
    status_name: str = "STATUS_SCHEDULED"
    status_detail: str = "Scheduled"
    unsupported_fields: list[str] = field(default_factory=list)
    settled: bool = False
    terminal: bool = False
    text: str = "snapshot"


@dataclass(frozen=True)
class Route:
    ready: bool
    mode: str = "group"
    group_guid: str | None = "synthetic-guid"
    destinations: tuple[str, ...] = ("owner", "friend-a", "friend-b")
    reason: str | None = None


@dataclass(frozen=True)
class Receipt:
    alert_id: str
    destination: str
    state: str
    outcome: str = "ok"
    watermark: int | None = None
    receipt_rowid: int | None = None
    attempted_at: float | None = None


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeDelivery:
    def __init__(self, ready=True, states=None, attempted_at=None, bind_on_sent=False):
        self.ready = ready
        self.states = list(states or ["sent"])
        self.attempted_at = list(attempted_at or [])
        self.bind_on_sent = bind_on_sent
        self.calls = []
        self.records = {}

    def route_status(self):
        return Route(self.ready, group_guid="synthetic-guid" if self.ready else None,
                     reason=None if self.ready else "no_exact_group")

    def deliver(self, alert_id, text):
        self.calls.append((alert_id, text))
        state = self.states.pop(0) if self.states else self.records.get(alert_id, "sent")
        self.records[alert_id] = state
        if state == "sent" and self.bind_on_sent:
            self.ready = True
        attempted = self.attempted_at.pop(0) if self.attempted_at else None
        return [Receipt(alert_id, "synthetic-guid", state, attempted_at=attempted)]


class Harness:
    def __init__(self, test, snapshots, delivery=None, settled=None, terminal=None, specs=None):
        self.test = test
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.specs = tuple(specs or [Spec("event-one")])
        self.snapshots = snapshots
        self.fetch_count = 0
        self.delivery = delivery or FakeDelivery()

        def fetch(spec):
            self.fetch_count += 1
            value = self.snapshots[spec.key]
            if isinstance(value, list):
                item = value.pop(0) if len(value) > 1 else value[0]
            else:
                item = value
            if isinstance(item, BaseException):
                raise item
            return item

        self.fetch = fetch
        self.format = lambda spec, snapshot: f"{spec.key} | {snapshot.text}"
        self.settled = settled or (lambda spec, snapshot: snapshot.settled)
        self.terminal = terminal or (lambda spec, snapshot: snapshot.terminal or snapshot.settled)

    def monitor(self):
        return LiveScoreMonitor(
            self.specs,
            self.fetch,
            self.format,
            self.settled,
            self.delivery,
            self.root / "state.json",
            self.root / "status.json",
            is_terminal_boundary=self.terminal,
            clock=self.clock,
        )


class LiveScoreMonitorTests(unittest.TestCase):
    def test_scheduled_time_changes_poll_frequency_but_never_infers_live(self):
        harness = Harness(self, {"event-one": Snapshot(
            "pre", scheduled_at=1_799_999_000, text="Scheduled by ESPN")})
        result = harness.monitor().run_once(force_poll=True)
        self.assertEqual(harness.delivery.calls, [])
        self.assertFalse(result.status["events"]["event-one"]["settled"])
        self.assertEqual(result.status["events"]["event-one"]["provider_state"], "pre")

    def test_live_alert_is_immediate_then_uses_real_world_600_second_cadence(self):
        snapshot = Snapshot("in", is_live=True, status_name="STATUS_IN_PROGRESS",
                            status_detail="Q2 3:10", text="Q2 3:10 | Away 20, Home 21")
        harness = Harness(self, {"event-one": snapshot})
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)
        self.assertEqual([call[0] for call in harness.delivery.calls], ["event-one:live:1"])
        harness.clock.advance(599)
        monitor.run_once(force_poll=True)
        self.assertEqual(len(harness.delivery.calls), 1)
        harness.clock.advance(1)
        monitor.run_once(force_poll=True)
        self.assertEqual([call[0] for call in harness.delivery.calls],
                         ["event-one:live:1", "event-one:live:2"])

    def test_restart_does_not_duplicate_live_alert(self):
        harness = Harness(self, {"event-one": Snapshot("in", is_live=True, text="Q1 9:00")})
        harness.monitor().run_once(force_poll=True)
        harness.monitor().run_once(force_poll=True)
        self.assertEqual([call[0] for call in harness.delivery.calls], ["event-one:live:1"])

    def test_route_wait_claims_exactly_one_bootstrap_then_reconciles_native_receipt(self):
        delivery = FakeDelivery(ready=False, states=["waiting_route", "waiting_route", "sent"])
        snapshot = Snapshot("in", is_live=True, text="Q1 8:00")
        harness = Harness(self, {"event-one": snapshot}, delivery=delivery)
        monitor = harness.monitor()
        first = monitor.run_once(force_poll=True)
        self.assertEqual(delivery.calls, [("event-one:live:1", "event-one | Q1 8:00")])
        self.assertEqual(first.status["pending_route_bootstrap"][0]["alert_id"], "event-one:live:1")
        self.assertEqual(first.status["pending_route_bootstrap"][0]["text"], "event-one | Q1 8:00")
        harness.clock.advance(60)
        snapshot.text = "Q2 10:00"
        monitor.run_once(force_poll=True)
        self.assertEqual(len(delivery.calls), 2)  # reconcile only; no second alert
        delivery.ready = True
        monitor.run_once(force_poll=True)
        self.assertEqual([call[0] for call in delivery.calls],
                         ["event-one:live:1", "event-one:live:1", "event-one:live:1"])

    def test_waiting_route_race_expires_old_live_text_instead_of_backfilling(self):
        delivery = FakeDelivery(ready=True, states=["waiting_route", "sent"])
        snapshot = Snapshot("in", is_live=True, text="Q1 old")
        harness = Harness(self, {"event-one": snapshot}, delivery=delivery)
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)
        harness.clock.advance(10)
        snapshot.text = "Q1 current"
        monitor.run_once(force_poll=True)
        self.assertEqual(delivery.calls[-1], ("event-one:live:2", "event-one | Q1 current"))
        self.assertEqual(len(delivery.calls), 2)

    def test_only_one_global_bootstrap_is_claimed_for_simultaneous_live_events(self):
        specs = (Spec("first-live"), Spec("second-live"))
        delivery = FakeDelivery(ready=False, states=["waiting_route", "sent", "sent"])
        harness = Harness(
            self,
            {
                "first-live": Snapshot("in", is_live=True, text="Q1 first"),
                "second-live": Snapshot("in", is_live=True, text="Q1 second"),
            },
            delivery=delivery,
            specs=specs,
        )
        monitor = harness.monitor()
        first = monitor.run_once(force_poll=True)
        self.assertEqual(delivery.calls, [("first-live:live:1", "first-live | Q1 first")])
        self.assertEqual(len(first.status["pending_route_bootstrap"]), 1)

        delivery.ready = True
        second = monitor.run_once(force_poll=True)
        self.assertEqual([call[0] for call in delivery.calls], [
            "first-live:live:1",  # approval watermark/bootstrap
            "first-live:live:1",  # reconcile exact native receipt
            "second-live:live:1",  # one fresh current alert after route bind
        ])
        self.assertEqual(second.status["pending_route_bootstrap"], [])

    def test_final_waits_for_route_then_sends_fresh_without_waiting_route_deadlock(self):
        delivery = FakeDelivery(ready=False, states=["sent"])
        snapshot = Snapshot("post", settled=True, text="Final | won")
        harness = Harness(self, {"event-one": snapshot}, delivery=delivery)
        monitor = harness.monitor()
        waiting = monitor.run_once(force_poll=True)
        self.assertEqual(delivery.calls, [])
        self.assertFalse(waiting.status["events"]["event-one"]["settled"])
        self.assertEqual(waiting.status["pending_route_bootstrap"], [])

        delivery.ready = True
        sent = monitor.run_once(force_poll=True)
        self.assertEqual(delivery.calls, [("event-one:final", "event-one | Final | won")])
        self.assertTrue(sent.complete)

    def test_settlement_creates_final_not_live_and_waits_for_confirmed_receipt(self):
        delivery = FakeDelivery(states=["pending", "sent"])
        snapshot = Snapshot("in", is_live=True, settled=True, text="Halftime | City 1, Fulham 0 | won")
        harness = Harness(self, {"event-one": snapshot}, delivery=delivery)
        monitor = harness.monitor()
        first = monitor.run_once(force_poll=True)
        self.assertFalse(first.complete)
        self.assertEqual(delivery.calls[0][0], "event-one:final")
        self.assertNotIn("event-one:live:1", first.status["receipt_states"])
        second = monitor.run_once()
        self.assertTrue(second.complete)
        self.assertEqual(delivery.calls, [
            ("event-one:final", "event-one | Halftime | City 1, Fulham 0 | won"),
            ("event-one:final", "event-one | Halftime | City 1, Fulham 0 | won"),
        ])

    def test_failed_final_receipt_is_visible_and_does_not_report_complete(self):
        delivery = FakeDelivery(states=["failed"])
        harness = Harness(self, {"event-one": Snapshot("post", settled=True, text="Final | lost")},
                          delivery=delivery)
        result = harness.monitor().run_once(force_poll=True)
        self.assertFalse(result.complete)
        self.assertEqual(result.status["phase"], "delivery_blocked")
        self.assertEqual(result.status["failed_final_deliveries"], ["event-one"])

    def test_failed_final_receipt_is_reconciled_without_new_alert_or_resend_id(self):
        delivery = FakeDelivery(states=["failed", "sent"])
        harness = Harness(self, {"event-one": Snapshot("post", settled=True, text="Final | won")},
                          delivery=delivery)
        monitor = harness.monitor()
        first = monitor.run_once(force_poll=True)
        self.assertFalse(first.complete)
        self.assertAlmostEqual(first.next_poll_at - harness.clock(), 15.0)
        second = monitor.run_once()
        self.assertTrue(second.complete)
        self.assertEqual([call[0] for call in delivery.calls],
                         ["event-one:final", "event-one:final"])

    def test_terminal_boundary_waits_for_fields_then_sends_normal_final(self):
        missing = Snapshot("post", terminal=True, settled=False, text="Final | grading unavailable")
        complete = Snapshot("post", terminal=True, settled=True, text="Final | won")
        harness = Harness(self, {"event-one": [missing, complete]})
        monitor = harness.monitor()
        first = monitor.run_once(force_poll=True)
        self.assertEqual(harness.delivery.calls, [])
        self.assertTrue(first.status["events"]["event-one"]["terminal_pending"])
        harness.clock.advance(30)
        second = monitor.run_once(force_poll=True)
        self.assertTrue(second.complete)
        self.assertEqual(harness.delivery.calls,
                         [("event-one:final", "event-one | Final | won")])
        self.assertEqual(second.status["forced_incomplete_settlements"], [])

    def test_terminal_boundary_forces_review_final_after_field_grace(self):
        snapshot = Snapshot("post", terminal=True, settled=False,
                            text="Final | grading unavailable; review")
        harness = Harness(self, {"event-one": snapshot})
        monitor = harness.monitor()
        first = monitor.run_once(force_poll=True)
        self.assertEqual(harness.delivery.calls, [])
        harness.clock.advance(119)
        monitor.run_once(force_poll=True)
        self.assertEqual(harness.delivery.calls, [])
        harness.clock.advance(1)
        forced = monitor.run_once(force_poll=True)
        self.assertTrue(forced.complete)
        self.assertEqual(harness.delivery.calls,
                         [("event-one:final", "event-one | Final | grading unavailable; review")])
        self.assertEqual(forced.status["forced_incomplete_settlements"], ["event-one"])
        self.assertTrue(forced.status["events"]["event-one"]["forced_incomplete"])

    def test_terminal_pending_suppresses_live_updates_during_grace(self):
        snapshot = Snapshot("in", is_live=True, terminal=True, settled=False,
                            text="HT | score field unavailable")
        harness = Harness(self, {"event-one": snapshot})
        result = harness.monitor().run_once(force_poll=True)
        self.assertEqual(harness.delivery.calls, [])
        self.assertTrue(result.status["events"]["event-one"]["terminal_pending"])

    def test_terminal_pending_resets_if_provider_regresses_before_grace_expires(self):
        terminal = Snapshot("post", terminal=True, settled=False,
                            text="Final | grading unavailable")
        regressed = Snapshot("pre", terminal=False, settled=False,
                             text="ESPN reverted to scheduled")
        harness = Harness(self, {"event-one": [terminal, regressed]})
        monitor = harness.monitor()
        first = monitor.run_once(force_poll=True)
        self.assertTrue(first.status["events"]["event-one"]["terminal_pending"])

        harness.clock.advance(121)
        result = monitor.run_once(force_poll=True)
        self.assertEqual(harness.delivery.calls, [])
        self.assertFalse(result.status["events"]["event-one"]["terminal_pending"])
        self.assertIsNone(result.status["events"]["event-one"]["terminal_pending_since"])
        self.assertEqual(result.status["forced_incomplete_settlements"], [])

    def test_route_bootstrap_expires_at_600_seconds_and_is_replaced(self):
        delivery = FakeDelivery(
            ready=False,
            states=["waiting_route", "waiting_route", "waiting_route"],
        )
        snapshot = Snapshot("in", is_live=True, text="Q1 current")
        harness = Harness(self, {"event-one": snapshot}, delivery=delivery)
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)
        harness.clock.advance(600)
        snapshot.text = "Q3 current"
        result = monitor.run_once(force_poll=True)
        self.assertEqual([call[0] for call in delivery.calls],
                         ["event-one:live:1", "event-one:live:1", "event-one:live:2"])
        self.assertEqual([item["alert_id"] for item in result.status["pending_route_bootstrap"]],
                         ["event-one:live:2"])
        self.assertEqual(result.status["receipt_states"]["event-one:live:1"],
                         "expired_no_backfill")

    def test_bootstrap_receipt_reconciles_before_expiry_at_600_second_edge(self):
        delivery = FakeDelivery(
            ready=False,
            states=["waiting_route", "sent"],
            bind_on_sent=True,
        )
        snapshot = Snapshot("in", is_live=True, text="Q1 current")
        harness = Harness(self, {"event-one": snapshot}, delivery=delivery)
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)

        harness.clock.advance(600)
        result = monitor.run_once(force_poll=True)
        self.assertEqual([call[0] for call in delivery.calls],
                         ["event-one:live:1", "event-one:live:1"])
        self.assertEqual(result.status["receipt_states"]["event-one:live:1"], "sent")
        self.assertEqual(result.status["pending_route_bootstrap"], [])
        self.assertTrue(result.status["route"]["ready"])

    def test_bootstrap_expires_early_when_provider_is_no_longer_live(self):
        delivery = FakeDelivery(ready=False, states=["waiting_route"])
        live = Snapshot("in", is_live=True, text="Q1")
        pre = Snapshot("pre", is_live=False, text="ESPN reverted to pre")
        harness = Harness(self, {"event-one": [live, pre]}, delivery=delivery)
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)
        result = monitor.run_once(force_poll=True)
        self.assertEqual(result.status["pending_route_bootstrap"], [])
        self.assertEqual(result.status["receipt_states"]["event-one:live:1"],
                         "expired_no_backfill")

    def test_bootstrap_expires_at_terminal_boundary_even_if_provider_still_says_live(self):
        delivery = FakeDelivery(ready=False, states=["waiting_route", "waiting_route"])
        live = Snapshot("in", is_live=True, text="First half")
        halftime = Snapshot("in", is_live=True, terminal=True, settled=False,
                            text="HT | score field unavailable")
        harness = Harness(self, {"event-one": [live, halftime]}, delivery=delivery)
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)
        result = monitor.run_once(force_poll=True)
        self.assertEqual(result.status["pending_route_bootstrap"], [])
        self.assertEqual(result.status["receipt_states"]["event-one:live:1"],
                         "expired_no_backfill")
        self.assertTrue(result.status["events"]["event-one"]["terminal_pending"])

    def test_live_cadence_uses_delivery_attempt_time_not_cycle_start(self):
        delivery = FakeDelivery(states=["sent", "sent"], attempted_at=[1_800_000_007.0, None])
        snapshot = Snapshot("in", is_live=True, text="Q1")
        harness = Harness(self, {"event-one": snapshot}, delivery=delivery)
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)
        harness.clock.advance(600)
        monitor.run_once(force_poll=True)
        self.assertEqual(len(delivery.calls), 1)
        harness.clock.advance(7)
        monitor.run_once(force_poll=True)
        self.assertEqual([call[0] for call in delivery.calls],
                         ["event-one:live:1", "event-one:live:2"])

    def test_route_is_refreshed_after_reconcile_for_same_cycle_live_alerts(self):
        specs = (Spec("bootstrap"), Spec("other"))
        delivery = FakeDelivery(
            ready=False,
            states=["waiting_route", "sent", "sent"],
            bind_on_sent=True,
        )
        harness = Harness(
            self,
            {
                "bootstrap": Snapshot("in", is_live=True, text="Q1 bootstrap"),
                "other": Snapshot("in", is_live=True, text="Q1 other"),
            },
            delivery=delivery,
            specs=specs,
        )
        monitor = harness.monitor()
        monitor.run_once(force_poll=True)
        result = monitor.run_once(force_poll=True)
        self.assertTrue(result.status["route"]["ready"])
        self.assertEqual([call[0] for call in delivery.calls], [
            "bootstrap:live:1", "bootstrap:live:1", "other:live:1",
        ])

    def test_no_send_preflight_formats_but_does_not_write_or_call_delivery(self):
        harness = Harness(self, {"event-one": Snapshot("in", is_live=True, text="Q3 sample")})
        result = harness.monitor().run_once(no_send=True, force_poll=True)
        self.assertEqual(harness.delivery.calls, [])
        self.assertFalse((harness.root / "state.json").exists())
        self.assertFalse((harness.root / "status.json").exists())
        self.assertEqual(result.status["phase"], "preflight")
        self.assertEqual(result.status["events"]["event-one"]["sample_alert"],
                         "event-one | Q3 sample")

    def test_unmatched_and_unsupported_provider_fields_are_explicit(self):
        error = RuntimeError("event_not_found")
        specs = (Spec("missing"), Spec("limited"))
        snapshots = {
            "missing": error,
            "limited": Snapshot("in", is_live=True, text="R2 clock unavailable",
                                unsupported_fields=["ufc_live_clock"]),
        }
        harness = Harness(self, snapshots, specs=specs)
        result = harness.monitor().run_once(no_send=True, force_poll=True)
        self.assertEqual(result.status["unmatched_events"], ["missing"])
        self.assertEqual(result.status["unsupported_fields"], {"limited": ["ufc_live_clock"]})
        self.assertEqual(result.status["events"]["missing"]["last_error"], "event_not_found")

    def test_expected_event_count_and_duplicate_keys_fail_closed(self):
        harness = Harness(self, {"event-one": Snapshot("pre")})
        with self.assertRaisesRegex(MonitorError, "unexpected_event_count"):
            LiveScoreMonitor(
                harness.specs, harness.fetch, harness.format, harness.settled,
                harness.delivery, harness.root / "state", harness.root / "status",
                expected_event_count=15,
            )
        with self.assertRaisesRegex(MonitorError, "duplicate_event_key"):
            LiveScoreMonitor(
                [Spec("same"), Spec("same")], harness.fetch, harness.format, harness.settled,
                harness.delivery, harness.root / "state", harness.root / "status",
            )


if __name__ == "__main__":
    unittest.main()
