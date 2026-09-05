"""Evaluator contract tests, with synthetic histories and mocked local inference."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from scripts import eval_conversation_style as evaluator


def response(content="Synthetic answer", reason="stop", **message_fields):
    result = Mock()
    result.json.return_value = {"message": {"content": content, **message_fields},
                                "done_reason": reason, "eval_count": 12}
    return result


class ConversationEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.post.return_value = response()
        self.builder = Mock(return_value="Synthetic prompt; no tools or memory.")
        self.namespace = {"build_system_prompt": self.builder, "build_light_chat_system_prompt": self.builder}

    def evaluate(self, case, **options):
        return evaluator.evaluate_case(self.session, self.namespace, case, variant="after",
                                       model="fixture-model", timeout=5, think=options.get("think", "off"))

    def test_current_prompt_definitions_load_without_runtime_imports_or_file_reads(self):
        source = (evaluator.ROOT / "davosbot" / "personality.py").read_text(encoding="utf-8")
        # The source's dependency imports and decorated file readers must not
        # execute. Building both prompts then has no reason to read any file.
        source = "import forbidden_evaluation_dependency\n" + source
        with patch.object(Path, "read_text", side_effect=AssertionError("unexpected private read")):
            namespace = evaluator.prompt_namespace(source)
            for name in ("build_system_prompt", "build_light_chat_system_prompt"):
                prompt = namespace[name](user_text="what do you think?")
                self.assertIn(evaluator.SYNTHETIC_IDENTITY, prompt)
                self.assertNotIn("unused-synthetic-memory", prompt)

    def test_real_prior_outputs_feed_followups_without_mutating_case(self):
        case = copy.deepcopy(evaluator.EXTENDED_CASES[0])
        original = copy.deepcopy(case)
        self.session.post.side_effect = [response("First real reply"), response("Second real reply"), response("Third real reply")]
        results = self.evaluate(case)
        self.assertEqual([1, 2, 3], [row["turn"] for row in results])
        second = self.session.post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual({"role": "assistant", "content": "First real reply"}, second[-2])
        self.assertEqual(case["followups"][0]["text"], second[-1]["content"])
        third = self.session.post.call_args_list[2].kwargs["json"]["messages"]
        self.assertEqual("Second real reply", third[-2]["content"])
        self.assertEqual(case["followups"][1]["criterion"], results[-1]["criterion"])
        self.assertEqual(original, case)
        self.assertTrue(all(row["semantic_assessment"] == "manual_review_required" for row in results))

    def test_timeout_halts_dependent_turns_and_does_not_record_exception_payload(self):
        self.session.post.side_effect = requests.Timeout("secret-error-canary")
        results = self.evaluate(evaluator.EXTENDED_CASES[0])
        self.assertEqual(["error", "skipped", "skipped"], [row["status"] for row in results])
        self.assertEqual("Timeout", results[0]["error"])
        self.assertEqual(1, self.session.post.call_count)
        self.assertNotIn("secret-error-canary", json.dumps(results))

    def test_empty_truncated_and_malformed_responses_are_not_successful_turns(self):
        for first, expected in ((response(""), "empty"), (response("Partial answer", "length"), "truncated")):
            with self.subTest(status=expected):
                self.session.reset_mock()
                self.session.post.return_value = first
                rows = self.evaluate(evaluator.EXTENDED_CASES[0])
                self.assertEqual(expected, rows[0]["status"])
                self.assertEqual(["skipped", "skipped"], [row["status"] for row in rows[1:]])
                self.assertEqual(1, self.session.post.call_count)
        self.session.post.return_value.json.return_value = {"message": []}
        self.assertEqual("error", self.evaluate(evaluator.CASES[0])[0]["status"])

    def test_thinking_text_never_enters_report_or_next_turn(self):
        self.session.post.return_value = response(thinking="reasoning-canary")
        rows = self.evaluate(evaluator.EXTENDED_CASES[0])
        self.assertEqual(len("reasoning-canary"), rows[0]["thinking_char_count"])
        self.assertNotIn("reasoning-canary", json.dumps(rows))
        for call in self.session.post.call_args_list:
            self.assertNotIn("reasoning-canary", json.dumps(call.kwargs["json"]))
            self.assertEqual("http://127.0.0.1:11434/api/chat", call.args[0])
            self.assertFalse(call.kwargs["json"]["think"])
            self.assertEqual(180, call.kwargs["json"]["options"]["num_predict"])

    def test_think_default_is_omitted_and_case_histories_stay_independent(self):
        first = self.evaluate(evaluator.CASES[0], think="default")
        self.assertNotIn("think", self.session.post.call_args.kwargs["json"])
        second = self.evaluate(evaluator.CASES[1])
        self.assertEqual([], second[0]["history"])
        self.assertNotEqual(first[0]["history"], second[0]["history"])

    def test_selected_case_sets_are_bounded_unique_and_keep_all_original_cases(self):
        self.assertEqual(8, len(evaluator.selected_cases(suite="core")))
        self.assertEqual(10, len(evaluator.selected_cases(suite="extended")))
        self.assertEqual(18, len({case["id"] for case in evaluator.CASES}))
        selected = evaluator.selected_cases(["draft_reference_typo", "draft_not_sent"])
        self.assertEqual({"draft_reference_typo", "draft_not_sent"}, {case["id"] for case in selected})
        self.assertEqual([], evaluator.selected_cases(["draft_reference_typo"], "core"))
        self.assertEqual(13, sum(1 + len(case.get("followups", [])) for case in evaluator.EXTENDED_CASES))

    def test_cli_evaluates_requested_extended_case_on_both_prompts_without_semantic_pass_claim(self):
        source = (evaluator.ROOT / "davosbot" / "personality.py").read_text(encoding="utf-8")
        self.session.get.return_value.json.return_value = {"models": [{"name": "fixture-model", "digest": "fixture-digest"}]}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            with patch.object(evaluator, "_git_output", side_effect=["a" * 40, source, "b" * 40]), patch.object(evaluator.requests, "Session", return_value=self.session):
                status = evaluator.main(["--baseline-ref", "HEAD", "--output", str(output), "--model", "fixture-model",
                                         "--case", "draft_reference_typo"])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(0, status)
        self.assertFalse(self.session.trust_env)
        self.assertEqual(["before", "after"], [row["variant"] for row in report["results"]])
        self.assertEqual("b" * 40, report["after_ref"])
        self.assertEqual("fixture-digest", report["model_digest"])
        self.assertEqual("manual_review_required", report["semantic_assessment"])
        self.assertNotIn("pass", report)

    def test_cli_rejects_empty_selection_before_any_model_request(self):
        with patch.object(evaluator.requests, "Session") as session, self.assertRaises(SystemExit) as raised:
            evaluator.main(["--baseline-ref", "HEAD", "--output", "unused.json", "--suite", "core", "--case", "draft_reference_typo"])
        self.assertEqual(2, raised.exception.code)
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
