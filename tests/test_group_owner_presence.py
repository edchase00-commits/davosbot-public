"""Owner membership normalization through the actual group dispatch gate."""

import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from davosbot import commands, config, image_conversation, imessage, main, permissions

OWNER = "+15550000001"
GROUP = "a" * 32
OTHER_GROUP = "b" * 32
OTHER = "+15550000002"


def message_db(members):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE chat(chat_identifier TEXT);
        CREATE TABLE handle(id TEXT);
        CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
    """)
    for chat, handle in members:
        chat_id = conn.execute("INSERT INTO chat VALUES (?)", (chat,)).lastrowid
        handle_id = conn.execute("INSERT INTO handle VALUES (?)", (handle,)).lastrowid
        conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (chat_id, handle_id))
    return conn


class GroupOwnerPresenceTests(unittest.TestCase):
    def dispatch(self, members, *, owner=OWNER, sender=None, db_error=None, enabled=True):
        sender = sender if sender is not None else owner
        with ExitStack() as stack:
            stack.enter_context(patch.object(permissions, "OWNER_ID", config.normalize_handle(owner) if isinstance(owner, str) else ""))
            for module in (main, commands):
                stack.enter_context(patch.object(module, "is_owner", wraps=permissions.is_owner))
            stack.enter_context(patch.object(main, "OWNER_ID", owner))
            stack.enter_context(patch.object(main, "is_owner_in_chat", wraps=imessage.is_owner_in_chat))
            stack.enter_context(patch.object(main, "is_gc_enabled", return_value=enabled))
            stack.enter_context(patch.object(main, "is_approved_user", return_value=False))
            stack.enter_context(patch.object(main, "handle_group_persona_editor_command", return_value=None))
            stack.enter_context(patch.object(commands, "is_approved_user", return_value=False))
            stack.enter_context(patch.object(commands, "_is_persona_catalog_request", return_value=False))
            stack.enter_context(patch.object(image_conversation, "begin_message", return_value=False))
            stack.enter_context(patch.object(main, "get_response", side_effect=AssertionError("Native ping must not call a model")))
            stack.enter_context(patch.object(main, "save_turn", side_effect=AssertionError("Ping has no history side effect")))
            send = stack.enter_context(patch.object(main, "send_message", return_value=True))
            db = stack.enter_context(patch.object(imessage, "_get_db", side_effect=db_error or (lambda: message_db(members))))
            main.handle_group_message(sender, GROUP, "@Davos ping")
            return send, db

    def test_canonical_formatted_and_ten_digit_membership_admit_verified_owner(self):
        for member in (OWNER, "5550000001", "1 555 000 0001", "+1 (555) 000-0001"):
            for sender in (OWNER, "+1 (555) 000-0001"):
                with self.subTest(member=member, sender=sender):
                    send, db = self.dispatch([(GROUP, member)], sender=sender)
                    send.assert_called_once_with(GROUP, "pong — routing confirmed", is_group=True)
                    db.assert_called_once()

    def test_email_case_and_configured_phone_spelling_use_existing_normalization(self):
        for owner, member in (("Owner@Example.Invalid", "OWNER@example.INVALID"),
                              ("(555) 000-0001", OWNER)):
            with self.subTest(owner=owner):
                send, _ = self.dispatch([(GROUP, member)], owner=owner)
                send.assert_called_once_with(GROUP, "pong — routing confirmed", is_group=True)

    def test_owner_in_other_chat_and_absent_owner_do_not_open_requested_chat(self):
        for members in ([(OTHER_GROUP, OWNER), (GROUP, OTHER)], [(GROUP, OTHER)], [], [(GROUP, None)]):
            with self.subTest(members=members):
                send, _ = self.dispatch(members)
                send.assert_not_called()

    def test_invalid_owner_configuration_fails_before_opening_database(self):
        for owner in ("", "   ", None, "owner", "12345", "not-an-email@", "two@@example.invalid"):
            with self.subTest(owner=owner):
                send, db = self.dispatch([(GROUP, owner)], owner=owner, sender=OWNER)
                send.assert_not_called()
                db.assert_not_called()

    def test_database_errors_remain_fail_closed_without_logging_identifiers(self):
        with patch.object(imessage, "logger") as logger:
            send, _ = self.dispatch([], db_error=sqlite3.OperationalError("synthetic private detail"))
        send.assert_not_called()
        logger.error.assert_called_once_with("is_owner_in_chat error (%s)", "OperationalError")

    def test_matching_owner_membership_does_not_authorize_another_sender(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                send, _ = self.dispatch([(GROUP, OWNER), (GROUP, OTHER)], sender=OTHER, enabled=enabled)
                send.assert_not_called()

    def test_missing_or_invalid_chat_identifier_never_queries_database(self):
        for chat in (None, "", 7):
            with self.subTest(chat=chat), patch.object(imessage, "_get_db") as db:
                self.assertFalse(imessage.is_owner_in_chat(chat, OWNER))
                db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
