"""Optional real-Chromium proof across the merchant adapter and durable ledger.

The sibling adapter fixture intercepts every request with synthetic pages. These
tests belong in the dedicated browser environment, not the network-blocking
unit-test runner. A base-install skip is not evidence of working checkout.
"""

import importlib.util
import unittest
from unittest.mock import patch

from davosbot import food_checkout


class FoodBrowserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if (importlib.util.find_spec("playwright") is None
                or importlib.util.find_spec("davosbot.checkout_browser") is None):
            raise unittest.SkipTest("Optional checkout adapter and Chromium environment required")
        import test_checkout_browser_integration as fixtures
        fixtures.ChromiumCheckoutTests.setUpClass()
        cls.fixtures = fixtures

    def setUp(self):
        self.browser = self.fixtures.ChromiumCheckoutTests(methodName="runTest")
        self.browser.setUp()
        self.addCleanup(self.browser.doCleanups)
        self.access = patch.object(food_checkout, "is_admin", return_value=True)
        self.access.start()
        self.addCleanup(self.access.stop)
        self.actor = self.fixtures.OWNER
        self.store = food_checkout.CheckoutStore(self.browser.base / "ledger" / "state.sqlite3")
        self.adapter = self.browser.adapter()

    def prepare(self, service="Uber Eats"):
        return food_checkout.begin_order(self.actor, service, {
            "goal": "two buffalo wings with ranch", "fulfillment": "delivery", "area": "Seattle",
            "details": "review existing cart",
        }, store=self.store, adapter=self.adapter, clock=lambda: 1000)

    def control(self, text):
        return food_checkout.handle_checkout_control(self.actor, text, store=self.store,
                                                     adapter=self.adapter, clock=lambda: 1000)

    def test_real_quote_approval_receipt_then_duplicate_never_repeats_purchase(self):
        reply = self.prepare()
        row = self.store.latest(self.actor)
        self.assertEqual("quoted", row["state"], reply)
        self.assertIn(row["quote"]["review_text"], reply)
        self.assertEqual(0, self.browser.purchases)
        command = "food confirm " + row["token"]
        self.assertIn("Merchant confirmed order ABCD-1234", self.control(command))
        self.assertIn("Merchant confirmed order ABCD-1234", self.control(command))
        self.assertEqual("confirmed", self.store.latest(self.actor)["state"])
        self.assertEqual(1, self.browser.purchases)

    def test_real_changed_total_does_not_reserve_or_purchase(self):
        self.prepare("DoorDash")
        row = self.store.latest(self.actor)
        self.browser.total = "30.18"
        self.assertIn("did not submit", self.control("food confirm " + row["token"]))
        self.assertEqual("needs_input", self.store.latest(self.actor)["state"])
        self.assertEqual(0, self.browser.purchases)
        self.assertIn("USD 30.18", self.control("food resume"))
        self.assertNotEqual(row["token"], self.store.latest(self.actor)["token"])

    def test_real_missing_receipt_persists_unknown_and_blocks_repeat(self):
        self.prepare()
        row = self.store.latest(self.actor)
        self.browser.receipt_mode = "missing"
        command = "food confirm " + row["token"]
        self.assertIn("unresolved", self.control(command))
        self.assertIn("unresolved", self.control("food status"))
        self.assertIn("unresolved", self.control(command))
        self.assertEqual("unknown", self.store.latest(self.actor)["state"])
        self.assertEqual(1, self.browser.purchases)


if __name__ == "__main__":
    unittest.main()
