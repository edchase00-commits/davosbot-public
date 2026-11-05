import unittest
from unittest.mock import patch

from davosbot import permissions
class PermissionSafetyTests(unittest.TestCase):
    def test_owner_admin_friend_tiers_preserve_owner_only_actions(self):
        with patch.object(permissions, "is_owner", lambda sender: sender == "owner"), patch.object(
            permissions, "is_admin", lambda sender: sender == "admin"
        ):
            self.assertTrue(permissions.can_user_do("owner", "modify_soul"))
            self.assertTrue(permissions.can_user_do("owner", "manage_image_access"))
            self.assertTrue(permissions.can_user_do("owner", "manage_owner_alerts"))
            self.assertTrue(permissions.can_user_do("owner", "schedule_cron"))
            self.assertFalse(permissions.can_user_do("admin", "modify_soul"))
            self.assertFalse(permissions.can_user_do("admin", "manage_image_access"))
            self.assertFalse(permissions.can_user_do("admin", "manage_owner_alerts"))
            self.assertFalse(permissions.can_user_do("admin", "change_personality"))
            self.assertFalse(permissions.can_user_do("admin", "schedule_cron"))
            self.assertTrue(permissions.can_user_do("admin", "send_contact_card"))
            self.assertFalse(permissions.can_user_do("friend", "send_contact_card"))
            self.assertTrue(permissions.can_user_do("friend", "casual_chat"))

    def test_admin_password_check_strip_and_redact(self):
        with patch.object(permissions, "ADMIN_PASSWORD", "swordfish"):
            self.assertTrue(permissions.check_admin_password("swordfish"))
            self.assertTrue(permissions.check_admin_password("password: swordfish"))
            self.assertTrue(permissions.check_admin_password("pw=swordfish"))
            self.assertFalse(permissions.check_admin_password("swordfishing"))

            self.assertEqual("pull", permissions.strip_password("password: swordfish pull"))
            self.assertEqual("please pull", permissions.strip_password("please swordfish pull"))
            self.assertEqual("token [redacted] here", permissions.redact_secret("token swordfish here"))

    def test_redact_secret_covers_common_api_token_formats(self):
        legacy_github = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
        fine_grained_github = "github_pat_" + "11ABCDEFG0abcdefghijklmnopqrstuvwxyz1234567890"
        openai_key = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz1234567890ABCDE"
        text = (
            f"github {legacy_github} "
            f"fine {fine_grained_github} "
            f"openai {openai_key} "
            "url https://example.test/path?token=supersecret&api_key=anothersecret"
        )

        redacted = permissions.redact_secret(text)

        self.assertNotIn(legacy_github, redacted)
        self.assertNotIn(fine_grained_github, redacted)
        self.assertNotIn(openai_key, redacted)
        self.assertNotIn("supersecret", redacted)
        self.assertNotIn("anothersecret", redacted)
        self.assertIn("[redacted-github-token]", redacted)
        self.assertIn("[redacted-openai-key]", redacted)
        self.assertIn("token=[redacted]", redacted)
        self.assertIn("api_key=[redacted]", redacted)

    def test_empty_admin_password_never_passes(self):
        with patch.object(permissions, "ADMIN_PASSWORD", ""):
            self.assertFalse(permissions.check_admin_password(""))
            self.assertFalse(permissions.check_admin_password("password: anything"))


if __name__ == "__main__":
    unittest.main()
