"""Real executor and handler/tool-loop routing, with no generated files or sends."""

import unittest
from unittest.mock import patch

from davosbot import main, permissions, tools
import test_actor_capability_context as actor_fixture
from test_agentic_tool_permissions import _response, _tool_call


OWNER = actor_fixture.OWNER
OTHER = actor_fixture.FRIEND
GROUP = "0123456789abcdef0123456789abcdef"
OTHER_GROUP = "fedcba9876543210fedcba9876543210"
BASE = {"filename": "report.csv", "file_type": "csv", "content": "item,total\nsample,1"}
REQUEST = "Generate a CSV file named report.csv from these rows: item,total / sample,1."


class GeneratedFileExecutorTests(unittest.TestCase):
    def setUp(self):
        owner = patch.object(permissions, "OWNER_ID", OWNER)
        owner.start()
        self.addCleanup(owner.stop)
        generate = patch.object(tools, "_generate_file", return_value="Synthetic helper result")
        self.generate = generate.start()
        self.addCleanup(generate.stop)

    def execute(self, *, sender=OWNER, origin=OWNER, fields=None):
        self.generate.reset_mock()
        args = dict(BASE)
        args.update(fields or {})
        return tools.execute_tool_outcome("generate_file", args, sender=sender, originating_chat_id=origin)

    def assert_denied(self, outcome):
        self.assertEqual("denied", outcome.status)
        self.generate.assert_not_called()

    def test_schema_does_not_offer_model_destination_selectors(self):
        definition = next(tool for tool in tools.TOOL_DEFINITIONS if tool["name"] == "generate_file")
        self.assertEqual(set(BASE), set(definition["parameters"]["properties"]))
        self.assertEqual(set(BASE), set(definition["parameters"]["required"]))
        self.assertIn("current originating", definition["description"])

    def test_origin_only_dm_and_group_calls_keep_unverified_helper_result(self):
        for origin, recipient, group in ((OWNER, OWNER, False), ("(555) 000-0001", OWNER, False), (GROUP, GROUP, True), (GROUP.upper(), GROUP, True)):
            with self.subTest(origin=origin):
                result = self.execute(origin=origin)
                self.generate.assert_called_once_with(BASE["filename"], BASE["file_type"], BASE["content"], recipient, group)
                self.assertEqual("unverified", result.status)
                self.assertEqual("Synthetic helper result", result.text)

    def test_legacy_matching_phone_email_and_uppercase_group_are_compatible(self):
        for sender, origin, recipient, mode in (
            (OWNER, "+1 (555) 000-0001", "5550000001", False),
            ("5550000001", OWNER, "(555) 000-0001", False),
            (OWNER, GROUP.upper(), GROUP, True),
            (OWNER, GROUP, GROUP.upper(), True),
            ("OWNER@EXAMPLE.INVALID", "owner@example.invalid", "Owner@Example.Invalid", False),
        ):
            with self.subTest(sender=sender, origin=origin), patch.object(permissions, "OWNER_ID", "owner@example.invalid" if "@" in sender else OWNER):
                result = self.execute(sender=sender, origin=origin, fields={"recipient": recipient, "is_group": mode})
                self.assertEqual("unverified", result.status)
                self.generate.assert_called_once()
                expected = GROUP if mode else ("owner@example.invalid" if "@" in sender else OWNER)
                self.assertEqual((expected, mode), self.generate.call_args.args[3:])

    def test_missing_malformed_or_other_dm_origin_fails_before_generation(self):
        for origin in (None, "", " ", False, 1, [], {}, "Cole", "Cole5550000001", "+123", "a" * 31, "owner@example", "<owner@example.invalid>", OTHER, "other@example.invalid"):
            with self.subTest(origin=origin):
                self.assert_denied(self.execute(origin=origin))

    def test_malformed_or_different_legacy_recipient_never_redirects(self):
        for origin in (OWNER, GROUP):
            for recipient in (None, "", " ", False, 1, [], {}, "Cole", "Cole5550000001", OTHER, OTHER_GROUP, "other@example.invalid"):
                with self.subTest(origin=origin, recipient=recipient):
                    self.assert_denied(self.execute(origin=origin, fields={"recipient": recipient}))
        self.assert_denied(self.execute(origin=GROUP, fields={"recipient": OWNER}))
        self.assert_denied(self.execute(fields={"recipient": GROUP}))

    def test_legacy_mode_must_be_a_matching_real_boolean(self):
        for origin in (OWNER, GROUP):
            for mode in (None, "false", "true", 0, 1, [], {}):
                with self.subTest(origin=origin, mode=mode):
                    self.assert_denied(self.execute(origin=origin, fields={"is_group": mode}))
            self.assert_denied(self.execute(origin=origin, fields={"is_group": origin == OWNER}))

    def test_nonowner_still_denied_even_with_matching_origin_and_owner_claim(self):
        self.assert_denied(self.execute(sender=OTHER, origin=OTHER, fields={"sender": OWNER}))

    def test_legacy_string_entrypoint_uses_same_origin_constraint(self):
        reply = tools.execute_tool("generate_file", {**BASE, "recipient": OTHER}, sender=OWNER, originating_chat_id=OWNER)
        self.assertIn("Nothing was generated or sent", reply)
        self.generate.assert_not_called()


class GeneratedFileHandlerTests(unittest.TestCase):
    def setUp(self):
        self.f = actor_fixture.ActorCapabilityContextTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.generate = self.f.stack.enter_context(patch.object(tools, "_generate_file", return_value="Synthetic helper result"))

    def route(self, fields=None, *, group=False, sender=OWNER):
        self.generate.reset_mock()
        namespace, executor = self.f._use_real_agentic_loop([
            _response(_tool_call("generate_file", **{**BASE, **(fields or {})})),
            _response({"text": "Done, I sent the file."}),
        ])
        executor.TOOL_DEFINITIONS = [tool for tool in tools.TOOL_DEFINITIONS if tool["name"] in {"generate_file", "web_search", "get_weather"}]
        executor.execute_tool_outcome.side_effect = tools.execute_tool_outcome
        self.f.route(REQUEST, group=group, sender=sender)
        return namespace, executor

    def test_actual_owner_handlers_generate_only_in_the_original_chat(self):
        for group in (False, True):
            with self.subTest(group=group):
                _, executor = self.route(group=group)
                destination = GROUP if group else OWNER
                self.assertEqual(destination, executor.execute_tool_outcome.call_args.kwargs["originating_chat_id"])
                self.generate.assert_called_once_with(BASE["filename"], BASE["file_type"], BASE["content"], destination, group)
                self.f.send.assert_called_once()
                self.assertEqual(destination, self.f.send.call_args.args[0])
                self.assertIn("completion is not verified", self.f.send.call_args.args[1])
                self.assertNotIn("Done, I sent", self.f.send.call_args.args[1])

    def test_actual_handlers_deny_all_cross_chat_model_calls_without_file_creation(self):
        for group, fields in (
            (False, {"recipient": OTHER}), (False, {"recipient": GROUP, "is_group": True}),
            (True, {"recipient": OWNER}), (True, {"recipient": OTHER_GROUP, "is_group": True}),
            (True, {"recipient": GROUP, "is_group": False}),
            (False, {"recipient": OWNER, "is_group": "false"}),
        ):
            with self.subTest(group=group, fields=fields):
                self.route(fields, group=group)
                self.generate.assert_not_called()
                self.f.send.assert_called_once()
                self.assertEqual(GROUP if group else OWNER, self.f.send.call_args.args[0])
                self.assertIn("not allowed", self.f.send.call_args.args[1])
                self.assertIn("Nothing was generated or sent", self.f.send.call_args.args[1])

    def test_existing_private_send_handlers_still_precede_generation(self):
        for group in (False, True):
            for gate in ("handle_private_send_confirmation", "handle_private_send_request"):
                with self.subTest(group=group, gate=gate), patch.object(main, gate, return_value="Private flow remains separate"):
                    self.route(group=group)
                    self.generate.assert_not_called()
                    self.f.model.assert_not_called()
                    self.assertEqual("Private flow remains separate", self.f.send.call_args.args[1])

    def test_nonowner_handlers_do_not_offer_or_execute_generate_file(self):
        for actor in (actor_fixture.ADMIN, actor_fixture.FRIEND):
            for group in (False, True):
                with self.subTest(actor=actor, group=group):
                    self.route({"recipient": actor}, group=group, sender=actor)
                    self.generate.assert_not_called()
                    self.assertFalse(self.f.model.call_args.kwargs.get("use_tools", False))
                    self.assertNotIn("generate_file", self.f.model.call_args.kwargs.get("allowed_tools") or [])


if __name__ == "__main__":
    unittest.main()
