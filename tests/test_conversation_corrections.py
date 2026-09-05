import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from davosbot import main


class ConversationCorrectionTests(unittest.TestCase):
    def run_owner(self, prompt, history, *, log_error=None):
        with ExitStack() as stack:
            for name in (
                "_handle_screenshot_issue_log", "_handle_priority_intake_command",
                "_handle_self_status_question", "_handle_model_status_question",
                "handle_private_send_confirmation", "handle_private_send_request",
                "handle_style_directive_message", "_describe_cron_from_text", "_cancel_cron_from_text",
                "_sports_recap_cron_from_text", "_schedule_cron_from_text",
                "_handle_openai_image_intent", "_handle_image_capability_status", "handle_command",
                "get_persona", "decatur_behavior_fast_reply", "match_skill", "detect_user_fact", "_market_fast_reply",
            ):
                stack.enter_context(patch.object(main, name, return_value=None))
            stack.enter_context(patch.object(main, "is_owner", return_value=True))
            stack.enter_context(patch.object(main, "classify_reminder_intent", return_value="none"))
            stack.enter_context(patch.object(main, "classify_cron_list_intent", return_value=False))
            stack.enter_context(patch.object(main, "detect_reminder_edit_intent", return_value=False))
            stack.enter_context(patch.object(main, "save_turn"))
            stack.enter_context(patch.object(main, "extract_and_update_memory"))
            stack.enter_context(patch.object(main, "build_light_chat_system_prompt", return_value="light prompt"))
            stack.enter_context(patch.object(main, "build_system_prompt", return_value="full prompt"))
            stack.enter_context(patch.dict(main._image_buffer, {}, clear=True))
            stack.enter_context(patch.dict(main._text_buffer, {}, clear=True))
            stack.enter_context(patch.object(main, "get_history", return_value=history))
            model = stack.enter_context(patch.object(main, "get_response", return_value="The margin is 25%, not 75%."))
            log = stack.enter_context(patch.object(main, "_log_owner_quality_intake_if_needed", side_effect=log_error, return_value="Logged bot-quality intake #7 [YELLOW]."))
            send = stack.enter_context(patch.object(main, "send_message", return_value=True))
            main.handle_dm("+15550000001", prompt)
            return model, log, send

    def test_quality_feedback_logs_and_still_reconsiders_answer(self):
        history = [
            {"role": "user", "content": "Revenue 100, cost 75. What is margin?"},
            {"role": "assistant", "content": "75%."},
        ]
        model, log, send = self.run_owner("that was wrong", history)
        log.assert_called_once_with("+15550000001", "that was wrong")
        model.assert_called_once()
        self.assertEqual(history, model.call_args.args[1])
        self.assertEqual("that was wrong", model.call_args.args[2])
        self.assertIn("Reconsider the previous answer", model.call_args.args[0])
        send.assert_called_once_with("+15550000001", "The margin is 25%, not 75%.")

    def test_feedback_log_failure_does_not_swallow_the_correction(self):
        model, log, send = self.run_owner("that was wrong", [], log_error=sqlite3.OperationalError("database is locked"))
        model.assert_called_once()
        self.assertNotIn("was recorded for review", model.call_args.args[0])
        send.assert_called_once_with("+15550000001", "The margin is 25%, not 75%.")

    def test_pasted_data_reaches_analysis_without_upload_loop(self):
        prompt = "analyze this spreadsheet:\nrevenue,cost\n100,75"
        model, _log, send = self.run_owner(prompt, [])
        model.assert_called_once()
        self.assertEqual(prompt, model.call_args.args[2])
        self.assertEqual("The margin is 25%, not 75%.", send.call_args.args[1])

    def test_previous_data_reaches_analysis_followup(self):
        history = [{"role": "user", "content": "Revenue 100, cost 75."}]
        model, _log, _send = self.run_owner("analyze this spreadsheet", history)
        model.assert_called_once()
        self.assertEqual(history, model.call_args.args[1])

    def test_genuinely_missing_artifact_still_asks_for_it(self):
        model, _log, send = self.run_owner("analyze this spreadsheet", [])
        model.assert_not_called()
        self.assertIn("actual file", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
