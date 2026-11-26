import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from davosbot import commands, group_chat, permissions


OWNER = "+15550000001"
TARGET = "+15550000002"
FORMATTED_TARGET = "+1 (555) 000-0002"


class AdminAccessDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.db_path = str(Path(tmp) / "access.db")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE admins (
                    handle TEXT, granted_by TEXT, revoked_at TEXT
                );
                CREATE TABLE admin_audit (
                    action TEXT, handle TEXT, actor TEXT
                );
                """
            )
        self.stack.enter_context(patch.object(commands, "BOT_DB_PATH", self.db_path))
        self.stack.enter_context(patch.object(permissions, "BOT_DB_PATH", self.db_path))
        self.stack.enter_context(patch.object(permissions, "OWNER_ID", OWNER))
        self.stack.enter_context(patch.object(group_chat, "is_approved_user", return_value=False))
        self.approve_user = self.stack.enter_context(patch.object(group_chat, "approve_user"))

    def query(self, sql):
        # Always inspect committed state through a fresh, real connection.
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql).fetchall()

    def execute(self, sql):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(sql)

    def test_grant_persists_permission_and_audit_with_formatted_number(self):
        reply = commands._cmd_grant(f"grant {FORMATTED_TARGET}", OWNER)

        self.assertIn(f"Granted admin to {TARGET}", reply)
        self.assertTrue(permissions.is_admin(FORMATTED_TARGET))
        self.assertEqual([(TARGET, OWNER, None)], self.query("SELECT * FROM admins"))
        self.assertEqual([("grant", TARGET, OWNER)], self.query("SELECT * FROM admin_audit"))
        self.approve_user.assert_called_once_with(TARGET)

    def test_revoke_persists_permission_and_audit(self):
        commands._cmd_grant(f"grant {TARGET}", OWNER)

        reply = commands._cmd_revoke(f"revoke {FORMATTED_TARGET}", OWNER)

        self.assertIn(f"Revoked admin from {TARGET}", reply)
        self.assertFalse(permissions.is_admin(TARGET))
        self.assertEqual([(0,)], self.query("SELECT COUNT(*) FROM admins WHERE revoked_at IS NULL"))
        self.assertEqual([("grant",), ("revoke",)], self.query("SELECT action FROM admin_audit"))

    def test_duplicate_grant_is_idempotent_and_regrant_restores_access(self):
        commands._cmd_grant(f"grant {TARGET}", OWNER)
        reply = commands._cmd_grant(f"grant {FORMATTED_TARGET}", OWNER)
        self.assertIn("already an active admin", reply)
        self.assertEqual([(1,)], self.query("SELECT COUNT(*) FROM admins"))
        self.assertEqual([(1,)], self.query("SELECT COUNT(*) FROM admin_audit"))

        commands._cmd_revoke(f"revoke {TARGET}", OWNER)
        commands._cmd_grant(f"grant {TARGET}", OWNER)
        self.assertTrue(permissions.is_admin(TARGET))
        self.assertEqual([(1,)], self.query("SELECT COUNT(*) FROM admins WHERE revoked_at IS NULL"))

    def test_nonowners_cannot_grant_or_revoke_even_when_admin(self):
        commands._cmd_grant(f"grant {TARGET}", OWNER)
        before = self.query("SELECT * FROM admin_audit")
        for actor in (TARGET, "+15550000003"):
            for command in (commands._cmd_grant, commands._cmd_revoke):
                with self.subTest(actor=actor, command=command.__name__):
                    reply = command(f"grant {OWNER}", actor)
                    self.assertIn("the owner-only", reply)
        self.assertEqual(before, self.query("SELECT * FROM admin_audit"))
        self.assertTrue(permissions.is_admin(TARGET))

    def test_invalid_or_multiple_handles_never_change_access(self):
        invalid = (
            "", "+1", "Alex", "Alex +15550000002", "5550000002 please",
            "+15550000002 +15550000003", "+15550000002, +15550000003",
            "first@example.com second@example.com", "first@example.com;second@example.com",
        )
        for handle in invalid:
            for verb, command in (("grant", commands._cmd_grant), ("revoke", commands._cmd_revoke)):
                with self.subTest(handle=handle, verb=verb):
                    self.assertIn("Usage:", command(f"{verb} {handle}", OWNER))
        self.assertEqual([], self.query("SELECT * FROM admins"))
        self.assertEqual([], self.query("SELECT * FROM admin_audit"))
        self.approve_user.assert_not_called()

    def test_audit_failure_rolls_back_grant_and_revoke(self):
        self.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON admin_audit BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        self.assertIn("grant failed", commands._cmd_grant(f"grant {TARGET}", OWNER))
        self.assertFalse(permissions.is_admin(TARGET))
        self.approve_user.assert_not_called()

        self.execute(f"INSERT INTO admins(handle) VALUES ('{TARGET}')")
        self.assertIn("revoke failed", commands._cmd_revoke(f"revoke {TARGET}", OWNER))
        self.assertTrue(permissions.is_admin(TARGET))

    def test_ambiguous_international_identity_never_changes_access(self):
        for raw in ("+4930123456", "+49 (30) 123456", "+1555000002"):
            for verb, command in (("grant", commands._cmd_grant), ("revoke", commands._cmd_revoke)):
                with self.subTest(raw=raw, verb=verb):
                    self.assertIn("Usage:", command(f"{verb} {raw}", OWNER))
        self.assertEqual([], self.query("SELECT * FROM admins"))
        self.assertEqual([], self.query("SELECT * FROM admin_audit"))
        self.approve_user.assert_not_called()

    def test_group_sync_failure_does_not_undo_committed_grant(self):
        self.approve_user.side_effect = OSError("state unavailable")
        reply = commands._cmd_grant(f"grant {TARGET}", OWNER)
        self.assertIn("sync FAILED", reply)
        self.assertTrue(permissions.is_admin(TARGET))
        self.assertEqual([("grant", TARGET, OWNER)], self.query("SELECT * FROM admin_audit"))

    def test_persona_success_and_denial_audit_rows_persist(self):
        commands._log_persona_switch(OWNER, "default", success=True)
        commands._log_persona_switch(TARGET, "default", success=False)
        self.assertEqual(
            [("persona_switch", "default", OWNER), ("persona_switch_denied", "default", TARGET)],
            self.query("SELECT * FROM admin_audit"),
        )


class GroupAccessHandleTests(unittest.TestCase):
    def invoke(self, command, actor=OWNER):
        with patch.object(permissions, "OWNER_ID", OWNER):
            return commands.handle_group_command(actor, "a" * 32, command)

    def test_formatted_phone_and_email_are_one_complete_handle(self):
        handles = {
            "+15550000002": TARGET,
            "+1 (555) 000-0002": TARGET,
            "(555) 000-0002": TARGET,
            "555.000.0002": TARGET,
            "1\u00a0555\u00a0000\u00a00002": TARGET,
            "Friend@Example.COM": "friend@example.com",
            "+442079460958": "+442079460958",
        }
        for verb, target_function in (("allow", "approve_user"), ("revoke", "revoke_user")):
            for raw, expected in handles.items():
                with self.subTest(verb=verb, raw=raw), patch.object(commands, target_function) as mutate:
                    reply = self.invoke(f"@Davos {verb} {raw}")
                    mutate.assert_called_once_with(expected)
                    self.assertIn(expected, reply)

    def test_invalid_or_multiple_handles_are_rejected_before_state_write(self):
        for verb in ("allow", "revoke"):
            for raw in ("", "+1", "Alex", "Alex +15550000002", "+15550000002 +15550000003", "one@example.com two@example.com"):
                with self.subTest(verb=verb, raw=raw), patch.object(commands, "approve_user") as allow, patch.object(commands, "revoke_user") as revoke:
                    self.assertIn("Usage:", self.invoke(f"@Davos {verb} {raw}"))
                    allow.assert_not_called()
                    revoke.assert_not_called()

    def test_greetings_preserve_complete_handle_validation(self):
        for prefix in ("hey, ", "okay ", "one more thing: "):
            for verb, target_function in (("allow", "approve_user"), ("revoke", "revoke_user")):
                with self.subTest(prefix=prefix, verb=verb), patch.object(commands, target_function) as mutate:
                    self.assertIn(TARGET, self.invoke(f"{prefix}@Davos {verb} {FORMATTED_TARGET}"))
                    mutate.assert_called_once_with(TARGET)

    def test_greetings_do_not_allow_extra_handles_or_identity_rewrites(self):
        for verb in ("allow", "revoke"):
            for raw in ("+4930123456", "+15550000002 +15550000003", "+15550000002 please"):
                with self.subTest(verb=verb, raw=raw), patch.object(commands, "approve_user") as allow, patch.object(commands, "revoke_user") as revoke:
                    self.assertIn("Usage:", self.invoke(f"hey, @Davos {verb} {raw}"))
                    allow.assert_not_called()
                    revoke.assert_not_called()

    def test_nonowner_cannot_change_group_access(self):
        for verb in ("allow", "revoke"):
            with patch.object(commands, "approve_user") as allow, patch.object(commands, "revoke_user") as revoke:
                self.assertIsNone(self.invoke(f"@Davos {verb} {FORMATTED_TARGET}", actor=TARGET))
                allow.assert_not_called()
                revoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
