import unittest

from davosbot import tools


EXPECTED_TOOL_MATRIX = {
    "web_search": {"minimum_role": "public", "side_effect": "external_read"},
    "get_weather": {"minimum_role": "public", "side_effect": "external_read"},
    "read_file": {"minimum_role": "owner", "side_effect": "local_file_read"},
    "write_file": {"minimum_role": "owner", "side_effect": "local_file_write"},
    "shell_exec": {"minimum_role": "owner", "side_effect": "host_command"},
    "sqlite_query": {"minimum_role": "owner", "side_effect": "database_query"},
    "edit_persona": {"minimum_role": "owner", "side_effect": "persona_write"},
    "create_persona": {"minimum_role": "owner", "side_effect": "persona_write"},
    "generate_file": {"minimum_role": "owner", "side_effect": "file_generate_send"},
    "log_workout": {"minimum_role": "public", "side_effect": "workout_write"},
    "log_change_request": {"minimum_role": "owner", "side_effect": "change_log_write"},
    "set_reminder": {"minimum_role": "public", "side_effect": "scoped_reminder_write"},
    "list_reminders": {"minimum_role": "public", "side_effect": "scoped_reminder_read"},
    "cancel_reminder": {"minimum_role": "public", "side_effect": "scoped_reminder_write"},
    "schedule_cron": {"minimum_role": "owner", "side_effect": "cron_write"},
    "list_crons": {"minimum_role": "owner", "side_effect": "cron_read"},
    "cancel_cron": {"minimum_role": "owner", "side_effect": "cron_write"},
    "edit_cron": {"minimum_role": "owner", "side_effect": "cron_write"},
    "get_group_chat_status": {"minimum_role": "owner", "side_effect": "group_state_read"},
    "list_chats": {"minimum_role": "owner", "side_effect": "chat_history_read"},
    "clear_chat_history": {"minimum_role": "owner", "side_effect": "chat_history_write"},
    "query_workout": {"minimum_role": "public", "side_effect": "workout_read_scoped"},
    "bet_log": {"minimum_role": "public", "side_effect": "bet_write"},
    "bet_settle": {"minimum_role": "public", "side_effect": "bet_write"},
    "bet_stats": {"minimum_role": "public", "side_effect": "bet_read"},
    "workout_log": {"minimum_role": "public", "side_effect": "workout_write"},
    "create_skill": {"minimum_role": "admin_or_owner", "side_effect": "skill_write"},
    "get_inspirational_quote": {"minimum_role": "public", "side_effect": "quote_generate"},
    "send_imessage": {"minimum_role": "owner", "side_effect": "private_send_prepare"},
}


OWNER_ONLY_TOOLS = {
    name for name, meta in EXPECTED_TOOL_MATRIX.items() if meta["minimum_role"] == "owner"
}


class ToolPermissionMatrixTests(unittest.TestCase):
    def test_every_tool_definition_is_classified_once(self):
        names = [tool["name"] for tool in tools.TOOL_DEFINITIONS]

        self.assertEqual(sorted(EXPECTED_TOOL_MATRIX), sorted(names))
        self.assertEqual(len(names), len(set(names)))

    def test_owner_only_gate_matches_matrix(self):
        self.assertEqual(OWNER_ONLY_TOOLS, set(tools._OWNER_ONLY_TOOLS))

    def test_admin_or_owner_tools_are_not_owner_only_at_llm_gate(self):
        admin_or_owner = {
            name for name, meta in EXPECTED_TOOL_MATRIX.items() if meta["minimum_role"] == "admin_or_owner"
        }

        self.assertEqual({"create_skill"}, admin_or_owner)
        self.assertTrue(admin_or_owner.isdisjoint(tools._OWNER_ONLY_TOOLS))

    def test_public_tools_are_not_owner_only(self):
        public_tools = {
            name for name, meta in EXPECTED_TOOL_MATRIX.items() if meta["minimum_role"] == "public"
        }

        self.assertTrue(public_tools)
        self.assertTrue(public_tools.isdisjoint(tools._OWNER_ONLY_TOOLS))

    def test_high_risk_side_effects_stay_owner_only(self):
        high_risk = {
            "local_file_read",
            "local_file_write",
            "host_command",
            "database_query",
            "persona_write",
            "file_generate_send",
            "change_log_write",
            "cron_write",
            "cron_read",
            "group_state_read",
            "chat_history_read",
            "chat_history_write",
            "private_send_prepare",
        }

        for name, meta in EXPECTED_TOOL_MATRIX.items():
            with self.subTest(tool=name):
                if meta["side_effect"] in high_risk:
                    self.assertEqual("owner", meta["minimum_role"])
                    self.assertIn(name, tools._OWNER_ONLY_TOOLS)

    def test_matrix_uses_known_role_and_side_effect_labels(self):
        valid_roles = {"owner", "admin_or_owner", "public"}
        valid_side_effects = {
            "external_read",
            "local_file_read",
            "local_file_write",
            "host_command",
            "database_query",
            "persona_write",
            "file_generate_send",
            "workout_write",
            "change_log_write",
            "scoped_reminder_write",
            "scoped_reminder_read",
            "cron_write",
            "cron_read",
            "group_state_read",
            "chat_history_read",
            "chat_history_write",
            "workout_read_scoped",
            "bet_write",
            "bet_read",
            "skill_write",
            "quote_generate",
            "private_send_prepare",
        }

        for name, meta in EXPECTED_TOOL_MATRIX.items():
            with self.subTest(tool=name):
                self.assertIn(meta["minimum_role"], valid_roles)
                self.assertIn(meta["side_effect"], valid_side_effects)


if __name__ == "__main__":
    unittest.main()
