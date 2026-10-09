"""Direct-provider replies must ignore reasoning and unexecuted tool calls."""
import ast
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import requests


class GeminiDirectReplyTests(unittest.TestCase):
    def call(self, data):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'davosbot/brain.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_call_gemini')
        response = Mock()
        response.json.return_value = data
        post = Mock(return_value=response)
        logger = Mock()
        namespace = dict(time=time, GEMINI_API_KEY='synthetic', GEMINI_MODEL='synthetic-model',
                         GEMINI_URL='https://example.invalid', check_gemini_budget=lambda source: SimpleNamespace(allowed=True),
                         _fit_history_for_model=lambda history: history, requests=SimpleNamespace(post=post, exceptions=requests.exceptions),
                         _close_response=lambda resp: resp.close(), _log_gemini_usage=Mock(),
                         _SLOW_MODEL_CALL_SECONDS=10, logger=logger)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<direct-provider>', 'exec'), namespace)
        result = namespace['_call_gemini']('system', [], 'synthetic request')
        self.assertEqual('NONE', post.call_args.kwargs['json']['toolConfig']['functionCallingConfig']['mode'])
        self.assertNotIn('tools', post.call_args.kwargs['json'])
        response.close.assert_called_once()
        return result, logger

    def test_final_text_after_tool_or_thought_part_is_returned(self):
        result, _ = self.call({'candidates': [{'content': {'parts': [
            {'functionCall': {'name': 'get_group_chat_status', 'args': {}}},
            {'thought': True, 'text': 'private reasoning'},
            {'text': 'Hello '}, {'text': 'there.'}]}}]})
        self.assertEqual('Hello there.', result)

    def test_tool_only_response_is_not_executed_or_logged(self):
        result, logger = self.call({'candidates': [{'content': {'parts': [
            {'functionCall': {'name': 'private_tool', 'args': {'secret': 'sensitive-value'}},
             'thoughtSignature': 'private-signature'}]}}]})
        self.assertIsNone(result)
        self.assertNotIn('sensitive-value', str(logger.mock_calls))
        self.assertNotIn('private-signature', str(logger.mock_calls))

    def test_malformed_response_does_not_log_provider_body(self):
        for data in ({'private': 'sensitive-value'}, {'candidates': None}, {'candidates': []}):
            result, logger = self.call(data)
            self.assertIsNone(result)
            self.assertNotIn('sensitive-value', str(logger.mock_calls))

    def test_thought_only_response_is_not_a_reply(self):
        result, _ = self.call({'candidates': [{'content': {'parts': [{'thought': True, 'text': 'private reasoning'}]}}]})
        self.assertIsNone(result)
