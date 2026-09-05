"""Offline purchase-boundary tests. No merchant, live account, or bot state."""

import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from davosbot import food_checkout as checkout


ACTOR = "+15550000001"
REQUEST = {"goal": "six buffalo wings", "fulfillment": "pickup", "area": "Seattle", "details": ""}


def quote(actor=ACTOR, now=1000):
    return {"version": 1, "service": "doordash", "profile_id": checkout._profile_id(actor, "doordash"),
            "quote_id": "test-quote", "merchant": "Fixture Wings", "items": [
                {"name": "Six wings", "quantity": 1, "options": ["Buffalo"]}],
            "fulfillment": "pickup", "address": "Fixture restaurant, Seattle", "payment_label": "Visa ending 4242",
            "currency": "USD", "total_minor": 1999, "checkout_url": "https://www.doordash.com/checkout",
            "observed_at": now, "expires_at": now + 300, "evidence_hash": "a" * 64,
            "review_text": "Fixture Wings\nSix wings\nQuantity: 1\nBuffalo\nPickup\nVisa ending 4242\nTotal USD $19.99"}


def receipt(q, now=1000):
    return {"order_id": "fixture-order-1", "status": "placed", "url": "https://www.doordash.com/orders/fixture-order-1",
            "merchant": q["merchant"], "currency": q["currency"], "total_minor": q["total_minor"],
            "profile_id": q["profile_id"], "quote_id": q["quote_id"], "evidence": "merchant_dom", "observed_at": now}


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = checkout.CheckoutStore(Path(self.temp.name) / "checkout" / "state.sqlite3")
        self.now = 1000
        self.access = patch.object(checkout, "is_admin", return_value=True)
        self.allowed = self.access.start()
        self.addCleanup(self.access.stop)
        self.adapter = Mock()
        self.adapter.prepare.side_effect = lambda actor, service, request: {"status": "ready", "quote": quote(actor, self.now)}
        self.adapter.inspect_order.return_value = {"status": "unknown"}

    def begin(self, actor=ACTOR):
        return checkout.begin_order(actor, "DoorDash", REQUEST, store=self.store, adapter=self.adapter, clock=lambda: self.now)

    def control(self, text, actor=ACTOR, store=None):
        return checkout.handle_checkout_control(actor, text, store=store or self.store,
                                                adapter=self.adapter, clock=lambda: self.now)

    def confirm(self, actor=ACTOR):
        return self.control("food confirm " + self.store.latest(actor)["token"], actor)

    def successful_submit(self, actor, q, *, on_submit):
        self.assertTrue(on_submit())
        return {"status": "confirmed", "receipt": receipt(q, self.now)}

    def test_quote_requires_explicit_code_then_verified_receipt(self):
        response = self.begin()
        self.assertIn("USD 19.99", response)
        self.assertIn("Visa ending 4242", response)
        self.assertIn("Nothing has been ordered", response)
        self.adapter.submit.assert_not_called()
        self.assertIsNone(self.control("yes"))
        self.assertIn("exact food confirm code", self.control("food confirm wrong"))
        self.adapter.submit.side_effect = self.successful_submit
        response = self.confirm()
        self.assertIn("Merchant confirmed order fixture-order-1", response)
        self.assertEqual("confirmed", self.store.latest(ACTOR)["state"])
        self.control("food confirm repeated")
        self.assertEqual(1, self.adapter.submit.call_count)

    def test_complete_merchant_checkout_is_shown_even_if_summary_omits_item(self):
        q = quote()
        q["review_text"] += "\nExtra paid item: dessert\nService fee: USD $2.00"
        self.adapter.prepare.side_effect = None
        self.adapter.prepare.return_value = {"status": "ready", "quote": q}
        self.assertIn(q["review_text"], self.begin())
        q["review_text"] = "x" * 3501
        self.assertFalse(checkout.validate_quote(q, ACTOR, "doordash", self.now))

    def test_missing_optional_adapter_is_honest_handoff_without_state(self):
        with patch.object(checkout, "_adapter", side_effect=ImportError):
            self.assertIsNone(checkout.begin_order(ACTOR, "DoorDash", REQUEST, store=self.store))
        self.assertFalse(self.store.path.exists())

    def test_actor_alias_shares_order_and_other_admin_cannot_confirm_it(self):
        self.begin()
        self.assertIn("Ready for your review", self.begin("+1 (555) 000-0001"))
        self.assertEqual(1, self.adapter.prepare.call_count)
        self.assertIn("No food checkout", self.control("food confirm " + self.store.latest(ACTOR)["token"], "+15550000002"))
        self.adapter.submit.assert_not_called()

    def test_revocation_checked_before_read_and_again_at_click(self):
        self.allowed.return_value = False
        self.assertIn("requires active", self.begin())
        self.assertFalse(self.store.path.exists())
        self.allowed.return_value = True
        self.begin()

        def revoked(actor, q, *, on_submit):
            self.allowed.return_value = False
            self.assertFalse(on_submit())
            return {"status": "failed"}

        self.adapter.submit.side_effect = revoked
        self.assertIn("did not submit", self.confirm())
        self.assertEqual("needs_input", self.store.latest(ACTOR)["state"])

    def test_timeout_after_reservation_stays_unknown_across_store_restart(self):
        self.begin()
        token = self.store.latest(ACTOR)["token"]

        def timeout(actor, q, *, on_submit):
            self.assertTrue(on_submit())
            raise TimeoutError("synthetic checkout timeout")

        self.adapter.submit.side_effect = timeout
        self.assertIn("unresolved", self.confirm())
        restarted = checkout.CheckoutStore(self.store.path)
        self.assertIn("unresolved", self.control("food confirm " + token, store=restarted))
        self.assertIn("unresolved", self.begin())
        self.assertIn("can't cancel", self.control("food cancel"))
        self.assertEqual(1, self.adapter.submit.call_count)
        self.assertEqual(1, self.adapter.prepare.call_count)

    def test_restart_between_reservation_and_result_cannot_replay(self):
        self.begin()
        row = self.store.latest(ACTOR)
        self.assertTrue(self.store.transition(ACTOR, row["id"], ("quoted",), "submitting", self.now, token=None))
        self.assertIn("unresolved", self.control("food confirm " + row["token"]))
        self.assertIn("unresolved", self.control("food resume"))
        self.adapter.submit.assert_not_called()

    def test_cancel_new_planning_draft_preserves_older_purchase_uncertainty(self):
        from davosbot import food_order
        self.begin()
        row = self.store.latest(ACTOR)
        self.store.transition(ACTOR, row["id"], ("quoted",), "unknown", self.now, token=None)
        real_control = checkout.handle_checkout_control
        with patch.object(food_order, "_drafts", {}), patch.object(food_order, "handle_checkout_control",
                side_effect=lambda sender, text: real_control(sender, text, store=self.store, adapter=self.adapter, clock=lambda: self.now)):
            for phrase in ("cancel food", "never mind", "stop"):
                with self.subTest(phrase=phrase):
                    self.assertIn("DoorDash", food_order.handle_food_order(ACTOR, "order wings"))
                    response = food_order.handle_food_order(ACTOR, phrase)
                    self.assertIn("draft cleared", response)
                    self.assertIn("unresolved purchase", response)
                    self.assertNotIn("No order was placed", response)
        self.assertEqual("unknown", self.store.latest(ACTOR)["state"])

    def test_changed_cart_needs_new_quote_and_old_code_cannot_buy(self):
        self.begin()
        old_token = self.store.latest(ACTOR)["token"]
        self.adapter.submit.return_value = {"status": "changed"}
        self.assertIn("did not submit", self.confirm())
        self.assertIn("Ready for your review", self.control("food resume"))
        self.assertNotEqual(old_token, self.store.latest(ACTOR)["token"])
        self.assertIn("exact food confirm code", self.control("food confirm " + old_token))

    def test_cross_process_requote_invalidates_inflight_old_callback(self):
        self.begin()

        def raced(actor, q, *, on_submit):
            other = checkout.CheckoutStore(self.store.path)
            row = other.latest(actor)
            self.assertTrue(other.transition(actor, row["id"], ("quoted",), "quoted", self.now,
                                             expected_token=row["token"], token="newtoken", quote=quote()))
            self.assertFalse(on_submit())
            return {"status": "failed"}

        self.adapter.submit.side_effect = raced
        self.confirm()
        self.assertEqual("newtoken", self.store.latest(ACTOR)["token"])
        self.assertEqual("quoted", self.store.latest(ACTOR)["state"])

    def test_quote_expiry_during_checkout_prevents_final_click(self):
        self.begin()

        def slow(actor, q, *, on_submit):
            self.now += 301
            self.assertFalse(on_submit())
            return {"status": "changed"}

        self.adapter.submit.side_effect = slow
        self.assertIn("did not submit", self.confirm())

    def test_malformed_receipts_and_success_without_callback_are_rejected(self):
        self.begin()
        self.adapter.submit.return_value = {"status": "confirmed", "receipt": receipt(quote())}
        self.assertIn("did not submit", self.confirm())
        self.control("food resume")

        def wrong_total(actor, q, *, on_submit):
            self.assertTrue(on_submit())
            r = receipt(q)
            r["total_minor"] += 100
            return {"status": "confirmed", "receipt": r}

        self.adapter.submit.side_effect = wrong_total
        self.assertIn("unresolved", self.confirm())

    def test_unknown_status_can_only_reconcile_verified_matching_receipt(self):
        self.begin()

        def uncertain(actor, q, *, on_submit):
            self.assertTrue(on_submit())
            return {"status": "unknown", "reference": {
                "service": q["service"], "profile_id": q["profile_id"], "quote_id": q["quote_id"],
                "order_id": "fixture-order-1", "receipt_url": "https://www.doordash.com/orders/fixture-order-1"}}

        self.adapter.submit.side_effect = uncertain
        self.confirm()
        self.assertIn("unresolved", self.control("food status"))
        reference = self.adapter.inspect_order.call_args.args[1]
        self.assertEqual("fixture-order-1", reference["order_id"])
        self.adapter.inspect_order.return_value = {"status": "confirmed", "receipt": receipt(quote())}
        self.assertIn("Merchant confirmed", self.control("food status"))
        self.assertEqual(1, self.adapter.submit.call_count)

    def test_clock_rollback_cannot_hide_current_checkout(self):
        self.begin()
        first = self.store.latest(ACTOR)["id"]
        self.control("food cancel")
        self.now -= 100
        self.assertIn("Ready for your review", self.begin())
        second = self.store.latest(ACTOR)
        self.assertNotEqual(first, second["id"])
        self.assertIn(second["token"], self.control("food status"))
        self.assertIn("No purchase was submitted", self.control("food cancel"))
        self.assertEqual(second["id"], self.store.latest(ACTOR)["id"])

    def test_foreign_reference_cannot_be_saved_or_retarget_known_order(self):
        self.begin()
        row = self.store.latest(ACTOR)
        self.store.transition(ACTOR, row["id"], ("quoted",), "unknown", self.now, token=None)
        ref = {**row["quote"], "order_id": "fixture-order-1", "receipt_url": "https://www.doordash.com/orders/fixture-order-1"}
        for field in ("service", "profile_id", "quote_id"):
            with self.subTest(field=field):
                candidate = dict(ref)
                candidate.pop(field)
                self.adapter.inspect_order.return_value = {"status": "unknown", "reference": candidate}
                self.control("food status")
                self.assertNotIn("order_id", self.store.latest(ACTOR)["reference"])
                candidate[field] = "wrong-account-or-quote"
                self.control("food status")
                self.assertNotIn("order_id", self.store.latest(ACTOR)["reference"])
        self.store.transition(ACTOR, row["id"], ("unknown",), "unknown", self.now, reference=ref)
        other = receipt(quote())
        other["order_id"] = "different-order"
        self.adapter.inspect_order.return_value = {"status": "confirmed", "receipt": other,
                                                   "reference": {**ref, "order_id": "different-order"}}
        self.assertIn("unresolved", self.control("food status"))
        self.assertEqual("fixture-order-1", self.store.latest(ACTOR)["reference"]["order_id"])

    def test_invalid_quote_shapes_origins_accounts_and_expiry_fail_closed(self):
        variants = {"profile_id": "other-admin", "service": "ubereats", "checkout_url": "https://doordash.com.evil.example/checkout",
                    "payment_label": "4242424242424242", "total_minor": True, "expires_at": 999,
                    "observed_at": 1001, "currency": "JPY", "items": [{"name": "wings", "quantity": 0}]}
        for field, value in variants.items():
            with self.subTest(field=field):
                q = quote()
                q[field] = value
                self.assertFalse(checkout.validate_quote(q, ACTOR, "doordash", self.now))

    def test_login_details_and_cancel_do_not_claim_purchase_or_store_secrets(self):
        self.adapter.prepare.return_value = {"status": "needs_login"}
        self.adapter.prepare.side_effect = None
        self.assertIn("Sign in", self.begin())
        before = self.store.latest(ACTOR)["request"]
        self.assertIn("directly on the merchant", self.control("food details password secret-canary"))
        self.assertEqual(before, self.store.latest(ACTOR)["request"])
        self.adapter.prepare.return_value = {"status": "needs_input", "question": "Which sauce?"}
        self.assertIn("Which sauce?", self.control("food details buffalo"))
        self.assertEqual("buffalo", self.adapter.prepare.call_args.args[2]["details"])
        self.assertIn("No purchase was submitted", self.control("food cancel"))
        self.adapter.submit.assert_not_called()

    def test_one_final_callback_and_concurrent_duplicate_confirm(self):
        self.begin()
        command = "food confirm " + self.store.latest(ACTOR)["token"]
        entered, release = threading.Event(), threading.Event()
        results = []

        def blocked(actor, q, *, on_submit):
            self.assertTrue(on_submit())
            self.assertFalse(on_submit())
            entered.set()
            self.assertTrue(release.wait(3))
            return {"status": "confirmed", "receipt": receipt(q)}

        self.adapter.submit.side_effect = blocked
        workers = [threading.Thread(target=lambda: results.append(self.control(command))) for _ in range(2)]
        for worker in workers:
            worker.start()
        self.assertTrue(entered.wait(3))
        release.set()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(1, self.adapter.submit.call_count)
        self.assertEqual(2, len(results))
        self.assertTrue(all("Merchant confirmed" in result for result in results))

    def test_other_actor_preparation_progresses_while_one_browser_blocks(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()

        def prepare(actor, service, request):
            if actor == ACTOR:
                entered.set()
                self.assertTrue(release.wait(3))
            else:
                completed.set()
            return {"status": "ready", "quote": quote(actor)}

        self.adapter.prepare.side_effect = prepare
        worker = threading.Thread(target=self.begin)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.begin("+15550000002")
            self.assertTrue(completed.is_set())
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_state_symlink_rejected_without_writing_target(self):
        target = Path(self.temp.name) / "unrelated"
        target.mkdir()
        link = Path(self.temp.name) / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation unavailable on this host")
        with self.assertRaises(ValueError):
            checkout.CheckoutStore(link / "state.sqlite3").begin(ACTOR, "doordash", REQUEST, self.now)
        self.assertEqual([], list(target.iterdir()))


if __name__ == "__main__":
    unittest.main()
