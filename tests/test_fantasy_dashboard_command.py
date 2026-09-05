import unittest
from unittest.mock import patch

from davosbot import commands, config


class FantasyDashboardCommandTests(unittest.TestCase):
    def test_owner_handle_command_gets_default_dashboard_link(self):
        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=True),
            patch.object(
                commands,
                "FANTASY_DASHBOARD_URL",
                config.DEFAULT_FANTASY_DASHBOARD_URL,
            ),
        ):
            reply = commands.handle_command("owner", "fantasy")

        self.assertIn("Fourth Down", reply)
        self.assertIn(config.DEFAULT_FANTASY_DASHBOARD_URL, reply)

    def test_configured_override_is_returned(self):
        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=True),
            patch.object(
                commands,
                "FANTASY_DASHBOARD_URL",
                "https://fantasy.example.test",
            ),
        ):
            reply = commands.handle_command("owner", "fantasy")

        self.assertIn("https://fantasy.example.test", reply)
        self.assertNotIn(config.DEFAULT_FANTASY_DASHBOARD_URL, reply)

    def test_non_owner_handle_command_is_denied_without_url(self):
        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=False),
            patch.object(
                commands,
                "FANTASY_DASHBOARD_URL",
                config.DEFAULT_FANTASY_DASHBOARD_URL,
            ),
        ):
            reply = commands.handle_command("admin", "fantasy")

        self.assertEqual("The fantasy dashboard is owner-only.", reply)
        self.assertNotIn("https://", reply)

    def test_invalid_url_fails_closed(self):
        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=True),
            patch.object(commands, "FANTASY_DASHBOARD_URL", ""),
        ):
            reply = commands.handle_command("owner", "fantasy")

        self.assertIn("FANTASY_DASHBOARD_URL", reply)
        self.assertNotIn("https://", reply)

    def test_owner_help_lists_fantasy_command(self):
        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=True),
        ):
            reply = commands.handle_command("owner", "help")

        self.assertIn("- fantasy", reply)
        self.assertIn("Fourth Down", reply)

    def test_owner_can_list_pending_requests(self):
        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=True),
            patch.object(
                commands.fantasy_access,
                "list_access",
                return_value={
                    "ok": True,
                    "members": [
                        {
                            "id": 7,
                            "handle": "+15550000002",
                            "displayName": "Pat Manager",
                            "emailHint": "te****@example.com",
                            "status": "pending",
                            "role": "viewer",
                        }
                    ],
                },
            ) as list_access,
        ):
            reply = commands.handle_command("owner", "fantasy requests")

        list_access.assert_called_once_with(pending_only=True)
        self.assertIn("#7 Pat Manager", reply)
        self.assertIn("te****@example.com", reply)
        self.assertIn("fantasy grant #ID", reply)

    def test_owner_can_grant_promote_and_revoke(self):
        member = {
            "id": 7,
            "handle": "+15550000002",
            "status": "active",
            "role": "editor",
        }
        with (
            patch.object(commands, "is_admin", return_value=True),
            patch.object(commands, "is_owner", return_value=True),
            patch.object(
                commands.fantasy_access,
                "grant_access",
                return_value={"ok": True, "member": member},
            ) as grant,
            patch.object(
                commands.fantasy_access,
                "set_access_role",
                return_value={"ok": True, "member": {**member, "role": "owner"}},
            ) as promote,
            patch.object(
                commands.fantasy_access,
                "revoke_access",
                return_value={
                    "ok": True,
                    "member": {**member, "status": "pending", "role": "viewer"},
                },
            ) as revoke,
        ):
            grant_reply = commands.handle_command(
                "owner", "fantasy grant #7 editor"
            )
            promote_reply = commands.handle_command(
                "owner", "fantasy promote 7 owner"
            )
            revoke_reply = commands.handle_command("owner", "fantasy revoke #7")

        grant.assert_called_once_with(7, "editor")
        promote.assert_called_once_with(7, "owner")
        revoke.assert_called_once_with(7)
        self.assertIn("now editor", grant_reply)
        self.assertIn("now owner", promote_reply)
        self.assertIn("returned to pending", revoke_reply)
        self.assertIn("request meme", revoke_reply)

    def test_group_fantasy_returns_link_without_access_api_call(self):
        with (
            patch.object(commands, "is_owner", return_value=False),
            patch.object(commands.fantasy_access, "get_access_status") as get_status,
            patch.object(commands.fantasy_access, "request_access") as request_access,
            patch.object(
                commands,
                "FANTASY_DASHBOARD_URL",
                config.DEFAULT_FANTASY_DASHBOARD_URL,
            ),
        ):
            reply = commands.handle_group_command(
                "+15550000002",
                "group-chat-guid",
                "@Davos fantasy",
            )

        self.assertIn(config.DEFAULT_FANTASY_DASHBOARD_URL, reply)
        get_status.assert_not_called()
        request_access.assert_not_called()

    def test_legacy_group_request_command_explains_signin_flow(self):
        with (
            patch.object(commands, "is_owner", return_value=False),
            patch.object(commands.fantasy_access, "request_access") as request_access,
            patch.object(
                commands,
                "FANTASY_DASHBOARD_URL",
                config.DEFAULT_FANTASY_DASHBOARD_URL,
            ),
        ):
            reply = commands.handle_group_command(
                "+15550000002",
                "group-chat-guid",
                "@Davos fantasy request tester@example.com",
            )

        self.assertIn(config.DEFAULT_FANTASY_DASHBOARD_URL, reply)
        self.assertIn("first sign-in creates the request", reply)
        self.assertNotIn("tester@example.com", reply)
        request_access.assert_not_called()

    def test_group_participant_gets_fantasy_link_without_broader_bot_access(self):
        with (
            patch.object(commands, "is_owner", return_value=False),
            patch.object(commands, "is_approved_user", return_value=False),
            patch.object(commands.fantasy_access, "request_access") as request_access,
            patch.object(
                commands,
                "FANTASY_DASHBOARD_URL",
                config.DEFAULT_FANTASY_DASHBOARD_URL,
            ),
        ):
            fantasy_reply = commands.handle_group_command(
                "+15550000003",
                "group-chat-guid",
                "@Davos fantasy",
            )
            unrelated_reply = commands.handle_group_command(
                "+15550000003", "group-chat-guid", "@Davos logs"
            )

        self.assertIn(config.DEFAULT_FANTASY_DASHBOARD_URL, fantasy_reply)
        request_access.assert_not_called()
        self.assertIsNone(unrelated_reply)

    def test_enabled_group_routes_fantasy_before_general_user_gate(self):
        from davosbot import main

        with (
            patch.object(main, "is_owner_in_chat", return_value=True),
            patch.object(main, "is_at_mentioned", return_value=True),
            patch.object(main, "normalize_group_mention_command", return_value="@Davos fantasy"),
            patch.object(main, "is_owner", return_value=False),
            patch.object(main, "is_gc_enabled", return_value=True),
            patch.object(main, "is_approved_user", return_value=False),
            patch.object(
                main,
                "handle_group_command",
                return_value=f"Fourth Down\n{config.DEFAULT_FANTASY_DASHBOARD_URL}",
            ) as handle_group_command,
            patch.object(main, "send_message") as send_message,
        ):
            main.handle_group_message(
                "+15550000003", "group-chat-guid", "@Davos fantasy"
            )

        handle_group_command.assert_called_once_with(
            "+15550000003", "group-chat-guid", "@Davos fantasy"
        )
        send_message.assert_called_once_with(
            "group-chat-guid",
            f"Fourth Down\n{config.DEFAULT_FANTASY_DASHBOARD_URL}",
            is_group=True,
        )


if __name__ == "__main__":
    unittest.main()
