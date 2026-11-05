import unittest
from unittest.mock import patch

from davosbot import commands, food_order


class FoodOrderTests(unittest.TestCase):
    def setUp(self):
        self.drafts = patch.object(food_order, "_drafts", {})
        self.drafts.start()
        self.addCleanup(self.drafts.stop)
        self.checkout = patch.object(food_order, "begin_order", return_value=None)
        self.checkout.start()
        self.addCleanup(self.checkout.stop)
        self.control = patch.object(food_order, "handle_checkout_control", return_value=None)
        self.control.start()
        self.addCleanup(self.control.stop)

    def ask(self, text, sender="+15550000001", now=100):
        return food_order.handle_food_order(sender, text, now=now)

    def test_multiturn_choices_and_honest_checkout_handoff(self):
        self.assertIn("DoorDash, Uber Eats", self.ask("i want wings"))
        self.assertIn("Delivery or pickup", self.ask("uber eats"))
        self.assertIn("city, neighborhood", self.ask("pickup"))
        reply = self.ask("Seattle")
        self.assertIn("Uber Eats · pickup · Seattle", reply)
        self.assertIn("nothing has been ordered", reply)
        self.assertIn("https://www.google.com/search?", reply)
        self.assertIsNone(self.ask("yes"))

    def test_complete_request_needs_no_repeated_questions(self):
        reply = self.ask("please order wings on DoorDash for delivery in Seattle")
        self.assertIn("DoorDash · delivery · Seattle", reply)
        self.assertIn("nothing has been ordered", reply)

    def test_service_choices_and_sender_isolation(self):
        self.ask("get me some pizza")
        self.assertIsNone(self.ask("doordash", sender="+15550000002"))
        self.assertIn("Delivery or pickup", self.ask("restaurant website"))
        self.assertIn("city, neighborhood", self.ask("delivery"))
        self.assertIn("restaurant website", self.ask("98101"))
        self.ask("order tacos")
        self.assertIn("Compare services", self.ask("either"))

    def test_cancel_expiry_and_unrelated_actions(self):
        self.ask("order wings")
        self.assertIsNone(self.ask("remind me to call mom"))
        self.assertIsNone(self.ask("what time is it"))
        self.assertIn("No order was placed", self.ask("cancel food"))
        self.assertIsNone(self.ask("Uber Eats"))
        self.ask("order wings")
        self.assertIsNone(self.ask("Uber Eats", now=1301))

    def test_unrelated_text_without_draft_falls_through(self):
        for text in ("wings are delicious", "order the list alphabetically", "hi", "yes fix", "grant +15550000002"):
            self.assertIsNone(self.ask(text))

    def test_existing_commands_win_and_food_is_owner_admin_dm_only(self):
        with patch.object(commands, "is_admin", side_effect=lambda sender: sender in {"owner", "admin"}), patch.object(commands, "is_owner", side_effect=lambda sender: sender == "owner"), patch.object(commands, "handle_club_command", return_value=None), patch.object(commands, "_cmd_status", return_value="healthy"):
            self.assertIn("DoorDash", commands.handle_command("owner", "order wings"))
            self.assertEqual("healthy", commands.handle_command("owner", "status"))
            self.assertIn("DoorDash", commands.handle_command("admin", "order pizza"))
            self.assertIsNone(commands.handle_command("friend", "order wings"))

    def test_dispatcher_cancels_food_without_calling_schedule_cancellation(self):
        with patch.object(commands, "is_admin", return_value=True), patch.object(commands, "handle_club_command", return_value=None), patch.object(commands, "_cmd_cancel") as cancel:
            commands.handle_command("owner", "order wings")
            self.assertIn("draft cleared", commands.handle_command("owner", "cancel food"))
            cancel.assert_not_called()
            self.assertIsNone(food_order.handle_food_order("owner", "DoorDash"))

    def test_pending_food_yields_to_live_information_and_stock_symbols(self):
        self.ask("order wings")
        self.assertIsNone(self.ask("weather in Seattle"))
        self.assertIsNone(self.ask("make a chart"))
        self.ask("DoorDash")
        self.ask("pickup")
        self.assertIsNone(self.ask("AAPL"))
        self.assertIsNone(self.ask("forecast tomorrow"))
        self.assertIn("Seattle", self.ask("Seattle"))


if __name__ == "__main__":
    unittest.main()
