"""Real Chromium, temporary profiles, synthetic merchant pages, no network.

Install requirements-checkout.txt and its Chromium to run this optional suite.
All requests are fulfilled or aborted in process, including service origins.
"""

from contextlib import ExitStack
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from davosbot import checkout_browser as checkout


OWNER = "+15550000001"
OTHER = "+15550000002"
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


@unittest.skipUnless(HAS_PLAYWRIGHT, "optional Playwright checkout dependency is not installed")
class ChromiumCheckoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # An optional Python package can be present without its separate browser.
        # The dedicated browser run must install both; base bot CI needs neither.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            if not Path(driver.chromium.executable_path).is_file():
                raise unittest.SkipTest("optional checkout Chromium is not installed in this environment")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(checkout, "profile_root", return_value=self.base / "private" / "profiles"))
        self.stack.enter_context(patch("davosbot.permissions.is_admin", return_value=True))
        self.requests = []
        self.purchases = 0
        self.adds = 0
        self.total = "25.18"
        self.empty = False
        self.checkout_text = ""
        self.extra_controls = ""
        self.receipt_mode = "confirmed"
        self.fail_after_click = False
        self.order_id = "ABCD-1234"
        self.opened_profiles = []
        test = self

        class FixtureSession(checkout._Session):
            def _launch(self):
                context = super()._launch()
                context.route("**/*", test.route)
                test.opened_profiles.append(self.directory)
                return context

        self.factory = FixtureSession

    def page_html(self):
        if self.checkout_text:
            return self.checkout_text
        contents = ("<p>Your cart is empty</p>" if self.empty else "<h1>Fixture Wings</h1><ul><li><p>Buffalo Wings</p><p>2</p><p>Ranch</p></li></ul>"
                    "<p>Delivery</p><p>123 Main St</p><p>Seattle WA 98101</p><p>Visa ending in 4242</p>"
                    f"<p>Total US${self.total}</p>")
        return ("<html><body>" + contents + self.extra_controls +
                "<button onclick=\"fetch('/add',{method:'POST'}).then(()=>location.reload())\">Add to cart</button>"
                "<button onclick=\"fetch('/place',{method:'POST'}).then(()=>location.href='/orders/ABCD-1234')\">Place order</button>"
                "</body></html>")

    def route(self, route):
        from urllib.parse import urlsplit
        url = urlsplit(route.request.url)
        self.requests.append((url.hostname, url.path, route.request.method))
        if url.hostname not in {"www.ubereats.com", "www.doordash.com"}:
            route.abort()
        elif url.path == "/place":
            self.purchases += 1
            if self.fail_after_click:
                route.abort()
            else:
                route.fulfill(status=200, content_type="application/json", body="{}")
        elif url.path == "/add":
            self.adds += 1
            self.empty = False
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif url.path.startswith("/orders/"):
            body = (f"<h1>Order confirmed</h1><p>Order # {self.order_id}</p><p>Fixture Wings</p><p>Total US${self.total}</p>"
                    if self.receipt_mode == "confirmed" else "<p>Your cart is empty</p>")
            route.fulfill(status=200, content_type="text/html", body=body)
        else:
            route.fulfill(status=200, content_type="text/html", body=self.page_html())

    @staticmethod
    def review(_request, snapshot):
        lines = snapshot["lines"]
        return {"action": "review", "evidence": {
            "merchant": lines.index("Fixture Wings"),
            "items": [{"name": lines.index("Buffalo Wings"), "quantity": lines.index("2"), "options": [lines.index("Ranch")]}],
            "fulfillment": lines.index("Delivery"), "address": [lines.index("123 Main St"), lines.index("Seattle WA 98101")],
            "payment_label": lines.index("Visa ending in 4242"),
            "total": next(i for i, line in enumerate(lines) if line.startswith("Total US$")),
        }}

    def adapter(self, planner=None):
        return checkout.CheckoutBrowser(planner=planner or self.review, session_factory=self.factory, clock=lambda: 1000)

    def ready(self, service="ubereats"):
        response = self.adapter().prepare(OWNER, service, {"goal": "wings", "details": "review existing cart"})
        self.assertEqual("ready", response["status"], response)
        return response["quote"]

    def test_prepare_real_dom_quote_never_clicks_purchase_for_both_services(self):
        for service in ("ubereats", "doordash"):
            quote = self.ready(service)
            self.assertEqual(2518, quote["total_minor"])
            self.assertEqual(2, quote["items"][0]["quantity"])
            self.assertEqual("Visa ending in 4242", quote["payment_label"])
        self.assertEqual(0, self.purchases)

    def test_planner_cannot_click_final_button_or_invent_eval_enter_or_url(self):
        def decision(action):
            def plan(_request, snapshot):
                final = next(control["id"] for control in snapshot["controls"] if control["name"] == "Place order")
                return {"action": action, "target": final, "script": "submit()", "key": "Enter", "url": "https://attacker.example/"}
            return plan
        for action in ("click", "evaluate", "press", "shell", "navigate"):
            result = self.adapter(decision(action)).prepare(OWNER, "ubereats", {"goal": "wings"})
            self.assertEqual("needs_input", result["status"])
        self.assertEqual(0, self.purchases)

    def test_changed_total_cart_fulfillment_address_or_payment_never_claims_or_clicks(self):
        quote = self.ready()
        original = self.page_html()
        changes = (("25.18", "30.18"), ("Buffalo Wings", "Pizza"), ("Delivery", "Pickup"),
                   ("123 Main St", "456 Other St"), ("4242", "1111"))
        for old, new in changes:
            self.checkout_text = original.replace(old, new)
            claim = Mock(return_value=True)
            result = self.adapter().submit(OWNER, quote, on_submit=claim)
            self.assertIn(result["status"], {"changed", "failed"})
            claim.assert_not_called()
        self.assertEqual(0, self.purchases)

    def test_checkpoint_rejection_and_exception_never_click(self):
        quote = self.ready()
        for claim in (Mock(return_value=False), Mock(side_effect=RuntimeError("synthetic DB failure"))):
            response = self.adapter().submit(OWNER, quote, on_submit=claim)
            self.assertEqual("failed", response["status"])
            claim.assert_called_once_with()
        self.assertEqual(0, self.purchases)

    def test_success_has_real_receipt_after_exactly_one_claim_and_one_click(self):
        quote = self.ready()
        observed = []
        def claim():
            observed.append(self.purchases)
            return True
        result = self.adapter().submit(OWNER, quote, on_submit=claim)
        self.assertEqual("confirmed", result["status"], result)
        self.assertEqual([0], observed)
        self.assertEqual(1, self.purchases)
        receipt = result["receipt"]
        self.assertEqual("ABCD-1234", receipt["order_id"])
        self.assertEqual(quote["quote_id"], receipt["quote_id"])
        self.assertEqual(quote["profile_id"], receipt["profile_id"])
        self.assertEqual("merchant_dom", receipt["evidence"])
        inspected = self.adapter().inspect_order(OWNER, result["reference"])
        self.assertEqual("confirmed", inspected["status"])
        self.assertEqual(1, self.purchases)

    def test_after_click_timeout_or_empty_cart_stays_unknown_without_retry(self):
        quote = self.ready()
        for failure in ("timeout", "empty"):
            self.fail_after_click = failure == "timeout"
            self.receipt_mode = "empty"
            before = self.purchases
            claim = Mock(return_value=True)
            result = self.adapter().submit(OWNER, quote, on_submit=claim)
            self.assertEqual("unknown", result["status"])
            self.assertEqual(before + 1, self.purchases)
            claim.assert_called_once_with()

    def test_unrelated_old_receipt_cannot_reconcile_unknown_or_other_order(self):
        quote = self.ready()
        reference = {**quote, "receipt_url": "https://www.ubereats.com/orders/ABCD-1234"}
        self.assertEqual("unknown", self.adapter().inspect_order(OWNER, reference)["status"])
        reference["order_id"] = "DIFFERENT-5432"
        self.assertEqual("unknown", self.adapter().inspect_order(OWNER, reference)["status"])
        self.assertEqual(0, self.purchases)

    def test_foreign_profile_origin_and_expired_quote_never_claim(self):
        quote = self.ready()
        variants = []
        other = deepcopy(quote); other["profile_id"] = checkout.profile_id(OTHER, "ubereats"); variants.append(other)
        external = deepcopy(quote); external["checkout_url"] = "https://attacker.example/"; variants.append(external)
        expired = deepcopy(quote); expired["expires_at"] = 999; variants.append(expired)
        for candidate in variants:
            claim = Mock(return_value=True)
            self.assertEqual("failed", self.adapter().submit(OWNER, candidate, on_submit=claim)["status"])
            claim.assert_not_called()
        self.assertEqual(0, self.purchases)

    def test_offdomain_navigation_is_blocked_by_browser_transport(self):
        self.checkout_text = "<html><body><script>location.href='https://attacker.example/steal'</script></body></html>"
        result = self.adapter().prepare(OWNER, "ubereats", {"goal": "wings"})
        self.assertNotEqual("ready", result["status"])
        self.assertFalse(any(host == "attacker.example" for host, _, _ in self.requests))
        self.assertEqual(0, self.purchases)

    def test_existing_cart_requires_review_and_add_retry_is_blocked(self):
        result = self.adapter().prepare(OWNER, "ubereats", {"goal": "wings"})
        self.assertEqual("existing_cart_needs_review", result["code"])
        def add(_request, snapshot):
            return {"action": "click", "target": next(control["id"] for control in snapshot["controls"] if control["name"] == "Add to cart")}
        self.assertEqual("existing_cart_needs_review", self.adapter(add).prepare(OWNER, "ubereats", {"goal": "wings"})["code"])
        self.assertEqual(0, self.adds)
        self.empty = True
        calls = 0
        def add_then_ask(request, snapshot):
            nonlocal calls
            calls += 1
            return add(request, snapshot) if calls == 1 else {"action": "ask", "question": "Which sauce?"}
        self.assertEqual("needs_input", self.adapter(add_then_ask).prepare(OWNER, "ubereats", {"goal": "wings"})["status"])
        self.assertEqual(1, self.adds)
        self.assertEqual("existing_cart_needs_review", self.adapter(add).prepare(OWNER, "ubereats", {"goal": "wings", "details": "ranch"})["code"])
        self.assertEqual(1, self.adds)

    def test_login_and_captcha_require_user_without_planner_or_credential_output(self):
        for html in ("<label for='secret'>Password</label><input id='secret' type='password' value='never-output-this'><button>Sign in</button>",
                     "<p>Verify you are human</p>"):
            self.checkout_text = html
            planner = Mock()
            response = self.adapter(planner).prepare(OWNER, "ubereats", {"goal": "wings"})
            self.assertEqual("needs_login", response["status"])
            planner.assert_not_called()
            self.assertNotIn("never-output-this", json.dumps(response))

    def test_unobserved_model_quote_text_does_not_become_a_quote(self):
        planner = Mock(return_value={"action": "review", "evidence": {"merchant": "invented", "total": "$1.00"}})
        response = self.adapter(planner).prepare(OWNER, "ubereats", {"goal": "wings", "details": "review existing cart"})
        self.assertEqual("needs_input", response["status"])
        self.assertNotIn("quote", response)

    def test_complete_checkout_review_preserves_omitted_paid_item_options_and_tip(self):
        self.extra_controls = ("<ul><li><p>Extra pizza</p><p>3</p><p>Extra cheese $2.00</p><p>$48.00</p></li></ul>"
                               "<p>Delivery fee $4.99</p><p>Tax $2.10</p><p>Tip $6.00</p>")
        quote = self.ready()
        self.assertEqual(1, len(quote["items"]))  # The planner omitted the pizza.
        for value in ("Extra pizza", "3", "Extra cheese $2.00", "$48.00", "Delivery fee $4.99", "Tax $2.10", "Tip $6.00"):
            self.assertIn(value, quote["review_text"])
        self.assertEqual(0, self.purchases)

    def test_quantity_from_another_dom_item_group_cannot_be_a_summary(self):
        self.extra_controls = "<ul><li><p>Extra pizza</p><p>3</p></li></ul>"
        def wrong_quantity(request, snapshot):
            result = self.review(request, snapshot)
            result["evidence"]["items"][0]["quantity"] = snapshot["lines"].index("3")
            return result
        result = self.adapter(wrong_quantity).prepare(OWNER, "ubereats", {"goal": "wings", "details": "review existing cart"})
        self.assertEqual("quote_item_group_mismatch", result["code"])
        self.assertNotIn("quote", result)
        self.assertEqual(0, self.purchases)

    def test_large_checkout_requires_manual_review_without_truncation(self):
        self.extra_controls = "<p>" + "Extra paid item " * 300 + "</p>"
        result = self.adapter().prepare(OWNER, "ubereats", {"goal": "wings", "details": "review existing cart"})
        self.assertEqual("checkout_review_too_large", result["code"])
        self.assertNotIn("quote", result)
        self.assertEqual(0, self.purchases)

    def test_visible_tip_input_change_invalidates_review_before_claim(self):
        self.extra_controls = "<label for='tip'>Tip</label><input id='tip' value='6.00'>"
        quote = self.ready()
        self.assertIn("Tip: 6.00", quote["review_text"])
        self.extra_controls = self.extra_controls.replace("6.00", "9.00")
        claimed = Mock(return_value=True)
        self.assertEqual("changed", self.adapter().submit(OWNER, quote, on_submit=claimed)["status"])
        claimed.assert_not_called()
        self.assertEqual(0, self.purchases)

    def test_purchase_history_is_not_a_final_control(self):
        self.checkout_text = self.page_html().replace("Place order", "Purchase history")
        result = self.adapter().prepare(OWNER, "ubereats", {"goal": "wings", "details": "review existing cart"})
        self.assertEqual("final_control_missing", result["code"])
        self.assertEqual(0, self.purchases)

    def test_manual_empty_cart_allows_new_preparation_after_interrupted_add(self):
        self.empty = True
        def add_then_ask(_request, snapshot):
            if "Your cart is empty" in snapshot["lines"]:
                return {"action": "click", "target": next(control["id"] for control in snapshot["controls"] if control["name"] == "Add to cart")}
            return {"action": "ask", "question": "Which sauce?"}
        first = self.adapter(add_then_ask).prepare(OWNER, "ubereats", {"goal": "wings"})
        self.assertEqual("needs_input", first["status"])
        self.assertEqual(1, self.adds)
        self.empty = True  # Simulated manual clear between requests.
        second = self.adapter(add_then_ask).prepare(OWNER, "ubereats", {"goal": "wings"})
        self.assertEqual("needs_input", second["status"])
        self.assertEqual(2, self.adds)
        self.assertEqual(0, self.purchases)

    def test_visible_unmasked_payment_data_never_reaches_planner_or_quote(self):
        self.checkout_text = "<p>Card number 4111 1111 1111 1111</p>"
        planner = Mock()
        result = self.adapter(planner).prepare(OWNER, "ubereats", {"goal": "wings"})
        self.assertEqual("sensitive_or_unsupported_page", result["code"])
        self.assertNotIn("4111", json.dumps(result))
        planner.assert_not_called()

    def test_same_label_link_and_button_keep_their_actual_node_role(self):
        self.extra_controls = "<a href='/menu'>Checkout</a><button>Checkout</button>"
        observed = []
        def inspect_controls(_request, snapshot):
            observed.extend(control for control in snapshot["controls"] if control["name"] == "Checkout")
            return {"action": "ask", "question": "Which restaurant?"}
        self.adapter(inspect_controls).prepare(OWNER, "ubereats", {"goal": "wings"})
        self.assertEqual({"link", "button"}, {control["role"] for control in observed})
        link = next(control for control in observed if control["role"] == "link")
        self.assertEqual("https://www.ubereats.com/menu", link["url"])
        self.assertEqual(0, self.purchases)

    def test_ambiguous_order_pay_or_confirm_control_cannot_submit_during_preparation(self):
        original = self.page_html()
        for label in ("Order", "Pay", "Confirm"):
            self.checkout_text = original.replace("Place order", label)
            def choose(_request, snapshot):
                return {"action": "click", "target": next(control["id"] for control in snapshot["controls"] if control["name"] == label)}
            result = self.adapter(choose).prepare(OWNER, "ubereats", {"goal": "wings"})
            self.assertEqual("purchase_requires_confirmation", result["code"])
        self.assertEqual(0, self.purchases)

    def test_preselected_membership_native_or_aria_checkbox_never_becomes_food_quote(self):
        for control in ("<label for='trial'>Start DashPass trial</label><input id='trial' type='checkbox' checked>",
                        "<div role='checkbox' aria-label='Join Uber One membership' aria-checked='true'>Join Uber One membership</div>"):
            self.extra_controls = control
            result = self.adapter().prepare(OWNER, "ubereats", {"goal": "wings", "details": "review existing cart"})
            self.assertEqual("membership_requires_manual_review", result["code"])
            self.assertNotIn("quote", result)
        self.assertEqual(0, self.purchases)


if __name__ == "__main__":
    unittest.main()
