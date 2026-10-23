import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from davosbot import work_actions_extra as extra


OWNER = "+15550000001"
OTHER = "+15550000002"
GROUP = "a" * 32


class ExtraActionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.modules = {
            "config": SimpleNamespace(OWNER_ID=OWNER),
            "permissions": SimpleNamespace(is_owner=lambda value: value == OWNER),
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.lookup = self.stack.enter_context(patch.object(extra, "_module", side_effect=lambda name: self.modules[name]))

    def execute(self, action, args=None, owner=OWNER):
        return extra.execute_extra_action(action, args or {}, owner)

    def snapshot(self, value):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "gc_state.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.modules["config"].PROJECT_ROOT = root
        return path

    def test_schema_catalogue_is_serializable_without_runtime_imports(self):
        json.dumps(extra.EXTRA_ACTIONS, allow_nan=False)
        self.lookup.assert_not_called()

    def test_rejects_unknown_gateway_names_before_runtime_access(self):
        for action in ("shell", "execute_tool", "handle_command", "private_send.confirm", "memory.dump", "files.read"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "unsupported_action"):
                self.execute(action)
        self.lookup.assert_not_called()

    def test_rejects_caller_controlled_context_or_paths_for_every_action(self):
        for action in extra.EXTRA_ACTIONS:
            for key in ("sender", "owner", "db_path", "path", "command", "password"):
                with self.subTest(action=action, key=key), self.assertRaises(ValueError):
                    self.execute(action, {key: "untrusted"})
        self.lookup.assert_not_called()

    def test_owner_must_match_config_exactly_even_if_permission_mock_accepts(self):
        self.modules["permissions"].is_owner = lambda value: True
        for caller in (OTHER, "", None, OWNER[1:]):
            with self.subTest(caller=caller), self.assertRaisesRegex(ValueError, "owner_required"):
                self.execute("skills.list", owner=caller)

    def test_configured_owner_still_passes_native_owner_gate(self):
        self.modules["permissions"].is_owner = lambda value: False
        with self.assertRaisesRegex(ValueError, "owner_required"):
            self.execute("skills.list")

    def test_persona_switch_requires_explicit_history_acknowledgement(self):
        for ack in (False, 1, "true", None):
            with self.subTest(ack=ack), self.assertRaises(ValueError):
                self.execute("personas.set", {"name": "gruden", "clear_history": ack})
        self.lookup.assert_not_called()

    def test_persona_switch_never_calls_native_shared_state_helper(self):
        helper = Mock(return_value="Switched to gruden.")
        self.modules["commands"] = SimpleNamespace(_cmd_persona=helper)
        result = self.execute("personas.set", {"name": "gruden", "clear_history": True})
        self.assertEqual(result["status"], "native_confirmation_required")
        self.assertFalse(result["evidence"]["changed"])
        helper.assert_not_called()
        self.assertEqual([call.args[0] for call in self.lookup.call_args_list], ["config", "permissions"])

    def test_native_mutation_refusal_is_not_reported_as_success(self):
        self.modules["commands"] = SimpleNamespace(create_skill=Mock(return_value="Permission denied."))
        result = self.execute("skills.create", {"name": "hello", "trigger_phrase": "hi", "response_template": "Hello"})
        self.assertEqual(result["status"], "error")

    def test_workout_set_bounds_and_unknown_nested_fields(self):
        invalid_sets = [[], [{"weight": -1, "reps": 5}], [{"weight": float("nan"), "reps": 5}],
                        [{"weight": float("inf"), "reps": 5}], [{"weight": 50, "reps": True}],
                        [{"weight": 50, "reps": 0}], [{"weight": 50, "reps": 5, "sender": OTHER}],
                        [{"weight": 50, "reps": 5}] * 31]
        for sets in invalid_sets:
            with self.subTest(sets=sets), self.assertRaises(ValueError):
                self.execute("workouts.log", {"exercise_name": "Bench", "sets": sets})
        self.lookup.assert_not_called()

    def test_workout_native_input_mutation_cannot_change_request(self):
        def helper(args, sender):
            self.assertEqual(sender, OWNER)
            args["sets"][0]["weight"] = 999
            return "Logged — Bench"
        self.modules["tools"] = SimpleNamespace(_workout_log_tool=helper)
        args = {"exercise_name": "Bench", "sets": [{"weight": 100, "reps": 5}]}
        self.assertEqual(self.execute("workouts.log", args)["status"], "ok")
        self.assertEqual(args["sets"][0]["weight"], 100)

    def test_exercise_query_requires_an_exercise(self):
        with self.assertRaises(ValueError):
            self.execute("workouts.query", {"query_type": "exercise"})

    def test_native_only_private_send_never_imports_or_changes_pending_state(self):
        result = self.execute("private_send.prepare", {"recipient": OTHER, "message": "Hello"})
        self.assertEqual(result["status"], "native_confirmation_required")
        self.assertEqual(result["evidence"], {"staged": False, "sent": False})
        self.assertEqual([call.args[0] for call in self.lookup.call_args_list], ["config", "permissions"])

    def test_private_send_rejects_contact_alias_and_password_payload(self):
        for args in ({"recipient": "friend", "message": "Hello"},
                     {"recipient": OTHER, "message": "Hello", "password": "anything"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.execute("private_send.prepare", args)
        self.lookup.assert_not_called()

    def test_skill_content_update_does_not_import_mutators(self):
        result = self.execute("skills.update", {"name": "hello", "response_template": "new"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["evidence"]["code"], "unsupported")
        self.assertTrue(all(call.args[0] in {"config", "permissions"} for call in self.lookup.call_args_list))

    def test_access_changes_require_explicit_acknowledgement(self):
        for action in ("access.grant_admin", "access.revoke_admin", "access.set_approved"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.execute(action, {"handle": OTHER})

    def test_access_grant_cannot_partially_commit_before_group_sync(self):
        helper = Mock(return_value="Granted admin to " + OTHER)
        self.modules["commands"] = SimpleNamespace(_parse_access_handle=lambda value: value, _cmd_grant=helper)
        result = self.execute("access.grant_admin", {"handle": OTHER, "acknowledge_access_change": True})
        self.assertEqual(result["status"], "native_confirmation_required")
        self.assertFalse(result["evidence"]["changed"])
        helper.assert_not_called()

    def test_handle_that_native_normalizes_differently_is_refused(self):
        helper = Mock()
        self.modules["commands"] = SimpleNamespace(_parse_access_handle=lambda value: value.lower(), _cmd_revoke=helper)
        with self.assertRaisesRegex(ValueError, "exact_handle_required"):
            self.execute("access.revoke_admin", {"handle": "Friend@example.test", "acknowledge_access_change": True})
        helper.assert_not_called()

    def test_group_status_requires_owner_membership_in_exact_group(self):
        self.snapshot({"enabled_chats": [GROUP], "personas": {}})
        self.modules["imessage"] = SimpleNamespace(is_owner_in_chat=lambda group, owner: False)
        with self.assertRaisesRegex(ValueError, "unknown_or_unowned_group"):
            self.execute("groups.status", {"chat_id": GROUP})
        self.assertNotIn("group_chat", [call.args[0] for call in self.lookup.call_args_list])

    def test_group_read_uses_independent_snapshot_without_resetting_live_state(self):
        path = self.snapshot({"enabled_chats": [GROUP], "personas": {GROUP: "gruden"}})
        raw_before = path.read_bytes()
        live_state = {"enabled_chats": ["b" * 32], "personas": {"dm": "other"}}
        gc = SimpleNamespace(_state=live_state, _load=Mock(side_effect=AssertionError("would reset live state")))
        self.modules["group_chat"] = gc  # A regression must not call even its reads.
        self.modules["imessage"] = SimpleNamespace(is_owner_in_chat=lambda group, owner: group == GROUP and owner == OWNER)
        result = self.execute("groups.status", {"chat_id": GROUP})
        self.assertEqual(result["result"], {"chat_id": GROUP, "enabled": True, "persona": "gruden"})
        self.assertEqual(result["evidence"], {"source": "independent_disk_snapshot"})
        self.assertIs(gc._state, live_state)
        gc._load.assert_not_called()
        self.assertEqual(raw_before, path.read_bytes())

    def test_group_mutations_and_approval_use_native_only_without_imports(self):
        for action, args in (
            ("groups.set_enabled", {"chat_id": GROUP, "enabled": False}),
            ("access.set_approved", {"handle": OTHER, "approved": True, "acknowledge_access_change": True}),
        ):
            with self.subTest(action=action):
                result = self.execute(action, args)
                self.assertEqual(result["status"], "native_confirmation_required")
                self.assertFalse(result["evidence"]["changed"])
        self.assertTrue(all(call.args[0] in {"config", "permissions"} for call in self.lookup.call_args_list))

    def test_group_list_excludes_nonowner_groups_and_dm_context(self):
        self.snapshot({"enabled_chats": [GROUP, "b" * 32], "personas": {"dm": "gruden", GROUP: "gruden"}})
        self.modules["imessage"] = SimpleNamespace(is_owner_in_chat=lambda group, owner: group == GROUP)
        result = self.execute("groups.list")
        self.assertEqual(result["result"], [{"chat_id": GROUP, "enabled": True, "persona": "gruden"}])
        self.assertNotIn("group_chat", [call.args[0] for call in self.lookup.call_args_list])

    def test_persona_status_reads_snapshot_and_preserves_hidden_persona_boundary(self):
        self.snapshot({"enabled_chats": [], "personas": {"dm": "secret persona"}})
        self.modules["personality"] = SimpleNamespace(list_personas=lambda: ["gruden"])
        result = self.execute("personas.status")
        self.assertEqual(result["result"], {"current": "hidden persona", "available": ["gruden"]})
        self.assertNotIn("group_chat", [call.args[0] for call in self.lookup.call_args_list])
        self.assertNotIn("commands", [call.args[0] for call in self.lookup.call_args_list])

    def test_corrupt_or_missing_snapshot_never_falls_back_to_native_state(self):
        path = self.snapshot({})
        for content in ("{", '[]', '{"enabled_chats":null}', '{"personas":{"dm":[]}}'):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "group_snapshot_invalid"):
                self.execute("groups.list")
        path.unlink()
        with self.assertRaisesRegex(ValueError, "group_snapshot_unavailable"):
            self.execute("groups.list")
        self.assertNotIn("group_chat", [call.args[0] for call in self.lookup.call_args_list])

    def test_group_snapshot_is_bounded_before_json_decode(self):
        path = self.snapshot({})
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "group_snapshot_invalid"):
            self.execute("groups.list")

    def test_change_done_requires_acknowledgement_of_native_deletion(self):
        with self.assertRaises(ValueError):
            self.execute("changes.done", {"change_id": 7})
        helper = Mock(return_value="Log #7 removed.")
        self.modules["commands"] = SimpleNamespace(_cmd_log=helper)
        self.assertEqual(self.execute("changes.done", {"change_id": 7, "acknowledge_removal": True})["status"], "ok")
        helper.assert_called_once_with("log done 7", sender=OWNER)

    def test_memory_search_cannot_become_full_dump_using_sql_wildcards(self):
        for query in ("%", "___", "a", "abc%", "abc_"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.execute("memory.search", {"query": query})
        self.lookup.assert_not_called()

    def test_memory_note_uses_only_native_structured_note_helper(self):
        helper = Mock(return_value=19)
        self.modules["memory"] = SimpleNamespace(add_owner_memory_item=helper)
        result = self.execute("memory.note", {"text": "I prefer brief answers"})
        self.assertEqual(result["evidence"], {"note_id": 19})
        helper.assert_called_once_with("I prefer brief answers", source="work_chat_owner_note")

    def test_bet_ambiguous_round_trip_does_not_log_record(self):
        logger = Mock()
        self.modules["commands"] = SimpleNamespace(_parse_bet_input=lambda value: {"event": "different"}, _cmd_bet_log=logger)
        result = self.execute("bets.log", {"event": "Game", "odds": -110, "stake_units": 2})
        self.assertEqual(result["status"], "error")
        logger.assert_not_called()

    def test_images_keep_quota_route_and_do_not_claim_delivery(self):
        import re
        never = re.compile(r"(?!)")
        native = Mock(return_value="On it, generating image. Estimate: 1 minute.")
        self.modules["main"] = SimpleNamespace(_IMAGE_QUEUE_STATUS_RE=never, _IMAGE_QUEUE_SEND_RE=never,
                                               _LAST_GENERATED_IMAGE_RE=never, _handle_openai_image_intent=native)
        self.modules["image_conversation"] = SimpleNamespace(is_image_followup=lambda text: False)
        self.modules["openai_images"] = SimpleNamespace(parse_openai_image_intent=lambda text, has_image: SimpleNamespace(kind="generate"))
        tracker = SimpleNamespace(job_id="1788629000000-1234")
        with patch("davosbot.work_image_receipts.ImageTracker", return_value=tracker):
            result = self.execute("images.generate", {"prompt": "A red fox in snow"})
        native.assert_called_once_with(OWNER, "image generate A red fox in snow", None, OWNER, is_group=False, tracking=tracker)
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["evidence"]["delivery_confirmed"])
        self.assertEqual(tracker.job_id, result["evidence"]["job_id"])

    def test_images_cannot_read_native_buffered_attachment_via_followup(self):
        native = Mock()
        self.modules["main"] = SimpleNamespace(_handle_openai_image_intent=native)
        self.modules["image_conversation"] = SimpleNamespace(is_image_followup=lambda text: True)
        result = self.execute("images.generate", {"prompt": "Recreate that image"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["evidence"]["code"], "unsupported")
        native.assert_not_called()


class NativeWorkoutIntegrationTests(unittest.TestCase):
    def test_workout_round_trip_in_temporary_database_is_owner_scoped(self):
        from davosbot import tools
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "journal.sqlite")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE workout_entries (id INTEGER PRIMARY KEY, date TEXT DEFAULT '2026-01-01', sender TEXT, muscle_group TEXT, exercise_name TEXT, sets_json TEXT, notes TEXT)")
                db.execute("INSERT INTO workout_entries (sender, exercise_name, sets_json, notes) VALUES (?, 'Other private exercise', '[]', '')", (OTHER,))
            lookup = extra._module
            def module(name):
                if name == "config":
                    return SimpleNamespace(OWNER_ID=OWNER)
                if name == "permissions":
                    return SimpleNamespace(is_owner=lambda value: value == OWNER)
                return lookup(name)
            with patch.object(extra, "_module", side_effect=module), patch.object(tools, "BOT_DB_PATH", path):
                saved = extra.execute_extra_action("workouts.log", {
                    "exercise_name": "Bench", "muscle_group": "chest", "sets": [{"weight": 185, "reps": 5}],
                }, OWNER)
                self.assertEqual(saved["status"], "ok")
                result = extra.execute_extra_action("workouts.query", {"query_type": "recent"}, OWNER)
                self.assertIn("Bench", result["result"])
                self.assertNotIn("Other private exercise", result["result"])
            with closing(sqlite3.connect(path)) as db, db:
                row = db.execute("SELECT sender, sets_json FROM workout_entries WHERE exercise_name='Bench'").fetchone()
            self.assertEqual(row[0], OWNER)
            self.assertEqual(json.loads(row[1]), [{"weight": 185.0, "reps": 5}])


if __name__ == "__main__":
    unittest.main()
