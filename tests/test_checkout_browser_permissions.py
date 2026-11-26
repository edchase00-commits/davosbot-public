"""Pure permission, quote and account-boundary checks without browser setup."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from davosbot import checkout_browser as checkout


OWNER = "+15550000001"
OTHER = "+15550000002"


class CheckoutBoundaryTests(unittest.TestCase):
    def test_profiles_are_canonical_sender_and_service_scoped(self):
        self.assertEqual(checkout.profile_id(OWNER, "Uber Eats"), checkout.profile_id("+1 (555) 000-0001", "ubereats"))
        self.assertNotEqual(checkout.profile_id(OWNER, "ubereats"), checkout.profile_id(OTHER, "ubereats"))
        self.assertNotEqual(checkout.profile_id(OWNER, "ubereats"), checkout.profile_id(OWNER, "doordash"))
        self.assertEqual(64, len(checkout.profile_id(OWNER, "ubereats")))

    def test_url_allowlist_rejects_credentials_lookalikes_ports_and_external_origins(self):
        for url in ("http://www.ubereats.com/", "https://www.ubereats.com.attacker.example/",
                    "https://attacker.example/ubereats.com", "https://owner:secret@www.ubereats.com/",
                    "https://www.ubereats.com:444/", "file:///tmp/cart", "javascript:alert(1)",
                    "https://www.ubereats.com/?access_token=synthetic"):
            with self.subTest(url=url), self.assertRaises(checkout.CheckoutError):
                checkout._url(url, "ubereats")
        self.assertEqual("https://auth.uber.com/", checkout._url("https://auth.uber.com/", "ubereats"))
        with self.assertRaises(checkout.CheckoutError):
            checkout._url("https://www.doordash.com/", "ubereats")

    def test_unauthorized_calls_never_open_profile_or_browser(self):
        factory = Mock()
        adapter = checkout.CheckoutBrowser(session_factory=factory)
        with patch("davosbot.permissions.is_admin", return_value=False), patch.object(checkout, "_profile_lock") as lock:
            self.assertEqual("authorization_required", adapter.prepare(OTHER, "ubereats", {"goal": "wings"})["code"])
            self.assertEqual("authorization_required", adapter.submit(OTHER, {}, on_submit=Mock())["code"])
            self.assertEqual("authorization_required", adapter.inspect_order(OTHER, {})["code"])
            lock.assert_not_called()
        factory.assert_not_called()

    def test_foreign_quote_and_missing_order_reference_never_launch_browser(self):
        factory, claimed = Mock(), Mock()
        adapter = checkout.CheckoutBrowser(session_factory=factory)
        reference = {"service": "ubereats", "profile_id": checkout.profile_id(OWNER, "ubereats")}
        with patch("davosbot.permissions.is_admin", return_value=True):
            self.assertEqual("quote_account_mismatch", adapter.submit(OTHER, reference, on_submit=claimed)["code"])
            self.assertEqual("quote_account_mismatch", adapter.inspect_order(OTHER, reference)["code"])
            self.assertEqual("order_reference_missing", adapter.inspect_order(OWNER, reference)["code"])
        factory.assert_not_called()
        claimed.assert_not_called()

    def test_profile_leaf_symlink_never_launches_or_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(checkout, "profile_root", return_value=Path(temporary) / "checkout" / "profiles"):
            target = checkout.profile_root() / checkout.profile_id(OWNER, "ubereats")
            with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == target):
                with self.assertRaisesRegex(checkout.CheckoutError, "profile_unavailable"):
                    with checkout._profile_lock(OWNER, "ubereats"):
                        self.fail("symlinked profile entered")

    def test_capability_missing_optional_dependency_is_setup_needed(self):
        with patch("davosbot.permissions.is_admin", return_value=True), patch.object(checkout.importlib.util, "find_spec", return_value=None):
            result = checkout.capability(OWNER, "doordash")
        self.assertEqual("setup_needed", result["code"])
        self.assertFalse(result["purchase_ready"])
        self.assertEqual("unknown", result["authenticated"])

    def test_planner_uses_budgeted_existing_gemini_helper_and_no_tool_inventory(self):
        with patch("davosbot.brain._call_gemini", return_value='{"action":"ask","question":"Which sauce?"}') as model:
            self.assertEqual("ask", checkout._planner({"goal": "wings"}, {"lines": []})["action"])
        self.assertEqual("checkout_browser", model.call_args.kwargs["source"])
        self.assertEqual([], model.call_args.args[1])

    def test_order_receipt_requires_exact_order_merchant_total_and_confirmation(self):
        reference = {"merchant": "Fixture Wings", "currency": "USD", "total_minor": 2518,
                     "quote_id": "synthetic-quote", "profile_id": "a" * 64, "order_id": "ABCD-1234"}
        snapshot = {"url": "https://www.ubereats.com/orders/ABCD-1234", "lines": [
            "Order confirmed", "Order # ABCD-1234", "Fixture Wings", "Total US$25.18"]}
        receipt = checkout._receipt(snapshot, reference, 100)
        self.assertEqual("ABCD-1234", receipt["order_id"])
        self.assertEqual("merchant_dom", receipt["evidence"])
        for index, value in ((0, "Your cart is empty"), (1, "Order # OLDER-4321"),
                             (2, "Other Restaurant"), (3, "Total US$30.00")):
            changed = deepcopy(snapshot)
            changed["lines"][index] = value
            self.assertIsNone(checkout._receipt(changed, reference, 100))


if __name__ == "__main__":
    unittest.main()
