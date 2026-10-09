"""Real message/model/tool paths, synthetic providers/executor/sends only."""

import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from davosbot import commands, file_continuation as files, main, permissions, tool_outcomes
import test_owner_action_routing as owner_fixture
from test_agentic_tool_permissions import _load_agentic_loop, _response, _tool_call
from test_model_routing import _load_routing_helpers
import test_group_cron_creation_permissions as group_fixture


REQUEST = "Save these rows as a CSV file: name,total / Example,42"
QUESTION = "What filename should I use?"
OWNER = owner_fixture.OWNER


class FileContinuationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owner_fixture.OwnerActionRoutingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.stack = self.fixture.stack
        self.stack.enter_context(patch.object(files, "_pending", {}))
        self.clock = self.stack.enter_context(patch.object(files.time, "monotonic", return_value=100))
        self.stack.enter_context(patch.object(files, "PROJECT_ROOT", Path.cwd()))
        self.exists = self.stack.enter_context(patch.object(files.os.path, "lexists", return_value=False))
        self.save = self.stack.enter_context(patch.object(main, "save_turn"))

    def start(self, question=QUESTION, request=REQUEST, *, attempted=()):
        def answer(*args, **kwargs):
            for name in attempted:
                kwargs["on_tool_call"](name)
            return question
        self.fixture.model.side_effect = answer
        self.fixture.route(request)
        self.fixture.model.side_effect = None
        self.save.reset_mock()

    def run_followup(self, text, responses=None, outcome=None):
        if responses is None:
            responses = [_response(_tool_call("write_file", path="totals.csv", content="name,total\nExample,42")), _response({"text": "Done."})]
        namespace, module = _load_agentic_loop(responses)
        module.execute_tool_outcome.return_value = outcome or tool_outcomes.ToolOutcome("unverified", "Synthetic write result")
        routes, local, direct, events = _load_routing_helpers(ollama_reply="Done.", gemini_reply="Done.")
        routes["_call_gemini_agentic"] = namespace["_call_gemini_agentic"]
        self.fixture.model.side_effect = routes["get_response"]
        with patch.dict("sys.modules", {"agentic_boundary_test.tools": module, "agentic_boundary_test.tool_outcomes": tool_outcomes}):
            self.fixture.route(text, [{"role": "user", "content": REQUEST}, {"role": "assistant", "content": QUESTION}])
        return namespace, module

    def test_filename_and_short_confirmation_reach_restricted_executor(self):
        for question, answer in ((QUESTION, "totals.csv"), ("Use totals.csv for the filename?", "Yes, do it."), ("Use totals.csv for the filename?", "Okay.")):
            with self.subTest(answer=answer):
                self.start(question)
                namespace, module = self.run_followup(answer)
                module.execute_tool_outcome.assert_called_once_with("write_file", {"path": "totals.csv", "content": "name,total\nExample,42"}, sender=OWNER, originating_chat_id=OWNER)
                self.fixture.send.assert_called_once()
                self.assertIn("completion is not verified", self.fixture.send.call_args.args[1])
                self.assertFalse(self.fixture.model.call_args.kwargs["use_tools"])
                self.assertEqual(["write_file"], self.fixture.model.call_args.kwargs["allowed_tools"])
                self.assertIn(REQUEST, self.fixture.model.call_args.args[2])
                payload = namespace["requests"].post.call_args.kwargs["json"]
                self.assertEqual(["write_file"], [entry["name"] for entry in payload["tools"][0]["functionDeclarations"]])

    def test_yes_without_proposed_name_asks_again_without_tools(self):
        self.start()
        self.fixture.route("Okay.")
        self.fixture.model.assert_not_called()
        self.fixture.send.assert_called_once_with(OWNER, files.ASK_NAME)
        _, module = self.run_followup("totals.csv")
        module.execute_tool_outcome.assert_called_once()

    def test_no_write_provider_prose_cannot_claim_done(self):
        self.start()
        _, module = self.run_followup("totals.csv", [_response({"text": "Done."})])
        module.execute_tool_outcome.assert_not_called()
        self.fixture.send.assert_called_once_with(OWNER, files.NO_WRITE)

    def test_foreign_tools_changed_paths_and_extra_arguments_are_denied(self):
        for call in (_tool_call("shell_exec", command="echo synthetic"), _tool_call("web_search", query="unrequested"),
                     _tool_call("write_file", path="../other.csv", content="x"),
                     _tool_call("write_file", path="other.csv", content="x"),
                     _tool_call("write_file", path="totals.csv", content="x", overwrite=True)):
            with self.subTest(call=call):
                self.start()
                _, module = self.run_followup("totals.csv", [_response(call), _response({"text": "Done."})])
                module.execute_tool_outcome.assert_not_called()
                self.fixture.send.assert_called_once_with(OWNER, files.NO_WRITE)

    def test_duplicate_and_modified_retries_cannot_write_twice(self):
        self.start()
        first = _tool_call("write_file", path="totals.csv", content="one")
        changed = _tool_call("write_file", path="totals.csv", content="two")
        _, module = self.run_followup("totals.csv", [_response(first, first, changed), _response({"text": "Done."})])
        module.execute_tool_outcome.assert_called_once()
        self.assertIn("not run again", self.fixture.send.call_args.args[1])

    def test_existing_file_or_wrong_cwd_cannot_be_written(self):
        for scenario in ("existing", "cwd"):
            with self.subTest(scenario=scenario):
                self.start()
                with patch.object(files.os.path, "lexists", return_value=scenario == "existing"), patch.object(files, "PROJECT_ROOT", Path.cwd() if scenario == "existing" else Path.cwd() / "different"):
                    _, module = self.run_followup("totals.csv")
                module.execute_tool_outcome.assert_not_called()

    def test_failed_clarification_or_attempted_mutation_creates_no_pending_state(self):
        self.fixture.send.return_value = False
        self.start()
        self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))
        self.fixture.send.return_value = True
        for name in ("write_file", "shell_exec", "invented_mutation"):
            self.start(attempted=[name])
            self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))

    def test_unrelated_native_identity_and_history_clear_invalidate_pending(self):
        for text in ("Who am I?", "ping", "Never mind", "How do I export CSV?", "don't save it"):
            self.start()
            self.fixture.route(text)
            self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))
        self.start()
        files.clear_chat(OWNER)
        self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))

    def test_expiry_actor_origin_and_normalized_identity_boundaries(self):
        self.start()
        self.assertIsNone(files.consume("+15550000002", OWNER, "totals.csv"))
        self.assertIsNone(files.consume(OWNER, group_fixture.GROUP, "totals.csv"))
        self.assertIsInstance(files.consume("(555) 000-0001", OWNER, "totals.csv"), files.Selection)
        self.start()
        self.clock.return_value = 401
        self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))

    def test_failed_send_consumes_selection_without_false_success_history_or_replay(self):
        self.start()
        self.fixture.send.return_value = False
        _, module = self.run_followup("totals.csv")
        module.execute_tool_outcome.assert_called_once()
        self.save.assert_called_once()
        self.assertEqual("user", self.save.call_args.args[1])
        self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))

    def test_raised_send_does_not_retry_and_history_clear_hooks_remove_drafts(self):
        from davosbot import memory
        self.start()
        self.fixture.send.side_effect = RuntimeError("synthetic failure")
        _, module = self.run_followup("totals.csv")
        module.execute_tool_outcome.assert_called_once()
        self.fixture.send.assert_called_once()
        self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))
        self.fixture.send.side_effect = None
        for clear, args in ((memory.clear_history, (OWNER,)), (memory.clear_history_count, (OWNER, 1)), (memory.clear_history_minutes, (OWNER, 1))):
            self.start()
            with patch.object(memory, "connect_bot_db"):
                clear(*args)
            self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))

    def test_owner_revocation_clears_pending_even_for_filename_answer(self):
        self.start()
        with patch.object(main, "is_owner", return_value=False), patch.object(main, "is_admin", return_value=False), patch.object(main, "is_approved_user", return_value=False):
            self.fixture.route("totals.csv")
        self.assertIsNone(files.consume(OWNER, OWNER, "totals.csv"))

    def test_private_send_and_acute_safety_still_take_precedence(self):
        for gate in ("handle_private_send_confirmation", "handle_private_send_request", "_handle_acute_safety"):
            self.start("Use totals.csv for the filename?")
            with patch.object(main, gate, return_value=True if gate == "_handle_acute_safety" else "Private flow"):
                self.fixture.route("Okay.")
            self.fixture.model.assert_not_called()

    def test_meta_unsafe_and_completion_claims_cannot_start_drafts(self):
        for request in ("How do I save CSV files?", "Pretend to export the table as a CSV file", '"Save rows as a CSV file"', "Don't save rows as a CSV file", "Save rows to /private/report.csv", "Overwrite rows in a CSV file"):
            self.assertFalse(files.remember(OWNER, OWNER, request, QUESTION, delivered=True, attempted_tools=[]))
        for reply in ("Done. What filename should I use?", "Should I run shell_exec?", "Use MEMORY.md for the filename?", "Use ../totals.csv for the filename?", "Use totals.json for the filename?"):
            self.assertFalse(files.remember(OWNER, OWNER, REQUEST, reply, delivered=True, attempted_tools=[]))
        self.start()
        self.fixture.route("totals.json")
        self.fixture.model.assert_not_called()
        self.assertIn("doesn't match", self.fixture.send.call_args.args[1])

    def test_withheld_execution_and_code_examples_cannot_arm_filename_approval(self):
        for request in (
            "Save these rows as a CSV file, but do not write anything yet; just show the filename options.",
            "Write code showing how to save a CSV file.",
            "Save these rows as a CSV file tomorrow.",
            "Save these rows as a CSV file only after I approve the final contents.",
        ):
            with self.subTest(request=request):
                self.start("Use totals.csv for the filename?", request=request)
                self.assertFalse(files._pending)
                _, module = self.run_followup("Okay.")
                module.execute_tool_outcome.assert_not_called()

    def test_argument_guard_exception_fails_closed_before_callback(self):
        namespace, module = _load_agentic_loop([_response(_tool_call("write_file", path="totals.csv", content="x")), _response({"text": "Done."})])
        callback = Mock()
        with patch.dict("sys.modules", {"agentic_boundary_test.tools": module, "agentic_boundary_test.tool_outcomes": tool_outcomes}):
            result = namespace["_call_gemini_agentic"]("system", [], REQUEST, allowed_tools=["write_file"], on_tool_call=callback, tool_argument_guard=Mock(side_effect=ValueError("synthetic")))
        module.execute_tool_outcome.assert_not_called()
        callback.assert_not_called()
        self.assertIn("not allowed", result)


class FileGroupBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = group_fixture.GroupCronCreationPermissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(patch.stopall)
        patch.object(files, "_pending", {}).start()
        patch.object(main, "_remember_unmentioned_group_text").start()
        patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None).start()

    def test_group_requires_original_owner_current_access_and_a_mention(self):
        self.fixture.model.return_value = QUESTION
        self.fixture.ask("@Davos " + REQUEST)
        for sender, chat in ((group_fixture.FRIEND, group_fixture.GROUP), (OWNER, group_fixture.OTHER_GROUP)):
            self.fixture.ask("@Davos totals.csv", sender, chat)
            if self.fixture.model.called:
                self.assertNotIn("tool_argument_guard", self.fixture.model.call_args.kwargs)
        self.assertIsNone(self.fixture.ask("totals.csv", OWNER))
        for gate in (self.fixture.enabled, self.fixture.owner_present):
            gate.return_value = False
            self.fixture.ask("@Davos totals.csv", OWNER)
            self.fixture.model.assert_not_called()
            gate.return_value = True
        self.fixture.ask("@Davos totals.csv", OWNER)
        self.assertEqual(["write_file"], self.fixture.model.call_args.kwargs["allowed_tools"])
        self.assertEqual(group_fixture.GROUP, self.fixture.model.call_args.kwargs["originating_chat_id"])
        self.assertEqual(files.NO_WRITE, self.fixture.send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
