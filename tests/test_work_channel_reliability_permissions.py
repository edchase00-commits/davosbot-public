"""Synthetic rollover and gh-control readback regressions; no live transport."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from davosbot import work_bridge as bridge, work_actions as actions
from test_work_bridge import FakeTransport, NOW, OWNER, comment, request


def gh_sanitized_body(body):
    """Emulate decoded nested-body changes documented by go-gh v2.16.0.

    Its JSON sanitizer sees even double-escaped Unicode controls; the remaining
    slash makes a receipt's embedded JSON invalid. This is a regression fixture,
    not permission to accept changed bytes or a claim about the Mini's version.
    """
    def replace(match):
        code = int(match.group(1), 16)
        if code in (9, 10, 11, 13) or not (code < 32 or 128 <= code <= 159):
            return match.group(0)
        return '\\^' + chr((code % 128) + 64)
    return re.sub(r'\\u00([0-9a-f]{2})', replace, body, flags=re.IGNORECASE)


class SanitizingTransport(FakeTransport):
    def __init__(self, rows):
        super().__init__(rows)
        self.lose_ack = False

    def publish(self, result):
        number = super().publish(result)
        stored = self.rows[-1]['body']
        if self.lose_ack or gh_sanitized_body(stored) != bridge._json_bytes(result).decode():
            raise bridge.BridgeError('publication_unconfirmed')
        return number

    def comment_pages(self, cursor):
        for rows, complete in super().comment_pages(cursor):
            for row in rows:
                row['body'] = gh_sanitized_body(row['body'])
            yield rows, complete


class ChannelReliabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.journal_root = self.root / '.work_bridge'
        self.now = NOW
        self.req = request()
        self.transport = SanitizingTransport([comment(self.req)])
        self.calls = []
        self.result = {'status': 'ok', 'result': '\x1b[32mPM2 online\x1b[0m; literal \\u001b; \x9b1mC1\x9b0m'}
        if sys.platform == 'win32':
            lock = patch.object(bridge, '_lock', side_effect=self.fixture_lock)
            sync = patch.object(bridge, '_sync_state_directory')
            lock.start()
            sync.start()
            self.addCleanup(lock.stop)
            self.addCleanup(sync.stop)

    @staticmethod
    @contextmanager
    def fixture_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        yield

    def execute(self, action, args, *, owner):
        self.calls.append(action)
        return copy.deepcopy(self.result)

    def worker(self):
        return bridge.WorkBridge(self.root, OWNER, transport=self.transport,
                                 validate_action=lambda a, b: None, execute_action=self.execute,
                                 clock=lambda: self.now, revision='a' * 40)

    def state(self):
        return bridge._load(self.journal_root / 'state.json')

    def record(self):
        return self.state()['records'][self.req['request_id']]

    def test_documented_sanitizer_would_break_old_nested_ansi_json(self):
        unsafe = bridge._json_bytes({'result': '\x1b[32mOnline\x1b[0m'}).decode()
        changed = gh_sanitized_body(unsafe)
        self.assertNotEqual(unsafe, changed)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(changed)

    def test_new_ansi_result_is_saved_once_and_acknowledged_once(self):
        self.assertEqual('active', self.worker().poll()['state'])
        response = self.record()['response']
        encoded = bridge._json_bytes(response).decode()
        self.assertEqual(encoded, gh_sanitized_body(encoded))
        self.assertNotIn('\\u001b', encoded)
        self.assertNotIn('[32m', response['result']['result'])
        self.assertIsNotNone(self.record()['published_comment_id'])
        for elapsed in (121, 500, 20000):
            self.now += elapsed
            self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_lost_post_ack_reconciles_after_delay_and_restart_without_repost(self):
        self.transport.lose_ack = True
        self.worker().poll()
        self.assertIsNone(self.record()['published_comment_id'])
        self.now += 121
        self.worker().poll()
        self.assertIsNotNone(self.record()['published_comment_id'])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_missing_or_tampered_receipt_never_allows_a_second_post(self):
        self.transport.lose_ack = True
        self.worker().poll()
        row = self.transport.rows[-1]
        data = json.loads(row['body'])
        data['result']['result'] = 'tampered'
        row['body'] = bridge._json_bytes(data).decode()
        self.now += 1000
        self.worker().poll()
        self.assertIsNone(self.record()['published_comment_id'])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_unsafe_keys_reject_without_normalization_or_collision(self):
        for key in ['x\x1by', '\\u001b', '\x1b[32mname\x1b[0m', '\x80key']:
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.safe_result({key: 'value', 'name': 'other'})

    def test_all_controls_and_literal_controls_are_transport_stable(self):
        controls = ''.join(chr(i) for i in list(range(32)) + list(range(127, 160)))
        literals = ' '.join('\\u%04x' % i for i in list(range(32)) + list(range(127, 160)))
        result = bridge.safe_result({'actual': controls, 'literal': literals, 'unicode': 'Hello café 🎉\nnext\tcolumn'})
        encoded = bridge._json_bytes(result).decode()
        self.assertEqual(encoded, gh_sanitized_body(encoded))
        self.assertEqual('Hello café 🎉\nnext\tcolumn', result['unicode'])
        self.assertEqual(result, bridge.safe_result(result))

    def test_formatting_cannot_reveal_split_tokens_or_private_keys(self):
        esc = '\x1b[31m'
        cases = ['gh' + esc + 'p_' + 'a' * 20,
                 'sk-' + esc + 'a' * 20,
                 '-----BE' + esc + 'GIN PRIVATE KEY-----\nprivate payload']
        for text in cases:
            with self.subTest(text=text):
                result = bridge.safe_result({'result': text})['result']
                self.assertEqual('[redacted]', result)
        value = 'synthetic-' + esc + 'secret-value'
        result = bridge.safe_result({'result': value}, redactor=lambda text: text.replace('synthetic-secret-value', '[redacted]'))
        self.assertEqual('[redacted]', result['result'])

    def seed_legacy(self):
        self.journal_root.mkdir()
        rows = {}
        for number, phase, attempted, published in [(1, 'finished', NOW-500, None),
                                                     (2, 'finished', 0, None),
                                                     (3, 'started', 0, None),
                                                     (4, 'finished', NOW-500, 5550000111)]:
            reqid = str(uuid.uuid4())
            response = {'schema_version': 1, 'kind': 'davos_result', 'request_id': reqid,
                        'request_comment_id': 5550000000 + number, 'state': 'completed',
                        'result': {'status': 'ok', 'result': '\x1b[32mOld raw result\x1b[0m'},
                        'runtime_revision': 'b' * 40, 'completed_at': bridge._iso(NOW-900000)}
            rows[reqid] = {'comment_id': 5550000000+number, 'body_sha256': 'c'*64,
                           'created_at': NOW-900000, 'started_at': NOW-900000, 'phase': phase,
                           'response': response if phase == 'finished' else None,
                           'published_comment_id': published, 'publication_attempt_at': attempted}
        state = {'schema_version': 1, 'scanned_at': NOW-100, 'records': rows}
        bridge._save(self.journal_root/'state.json', state)
        old_cursor = self.journal_root/'scan.json'
        old_cursor.write_bytes(bridge._json_bytes({'schema_version': 1, 'cursor': '999999999999'}))
        return copy.deepcopy(rows), old_cursor.read_bytes()

    def test_rollover_holds_every_legacy_record_unchanged_and_uses_new_cursor(self):
        legacy, cursor = self.seed_legacy()
        self.worker().poll()
        self.now += 8*86400
        self.worker().poll()
        for reqid, record in legacy.items():
            self.assertEqual(record, self.state()['records'][reqid])
        self.assertEqual(cursor, (self.journal_root/'scan.json').read_bytes())
        binding = bridge._load_channel_binding(self.journal_root)
        self.assertEqual(bridge.ISSUE_ID, binding['issue_id'])
        self.assertEqual(bridge.PREVIOUS_ISSUE_ID, binding['previous_issue_id'])
        self.assertEqual(set(legacy), set(binding['held_request_ids']))
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))
        self.assertIsNotNone(bridge._load_cursor(bridge._channel_cursor_path(self.journal_root)))

    def test_legacy_request_uuid_cannot_reexecute_on_new_channel(self):
        legacy, _ = self.seed_legacy()
        self.transport.rows = [comment(request(request_id=next(iter(legacy))), created=self.now)]
        self.worker().poll()
        self.assertEqual([], self.calls)
        self.assertEqual([], self.transport.publish_calls)

    def test_missing_or_corrupt_binding_fails_closed_without_journal_mutation(self):
        self.worker().poll()
        before = (self.journal_root/'state.json').read_bytes()
        binding = self.journal_root/'channel.json'
        binding.unlink()
        self.assertEqual('channel_binding_missing', self.worker().poll()['error'])
        self.assertEqual(before, (self.journal_root/'state.json').read_bytes())
        binding.write_text('{}')
        self.assertEqual('channel_binding_corrupt', self.worker().poll()['error'])
        self.assertEqual(before, (self.journal_root/'state.json').read_bytes())
        self.assertEqual(1, len(self.calls))

    def test_wrong_channel_binding_cannot_be_reused(self):
        self.worker().poll()
        path = self.journal_root/'channel.json'
        data = json.loads(path.read_bytes())
        data['issue_id'] += 1
        path.write_bytes(bridge._json_bytes(data))
        self.assertEqual('channel_binding_corrupt', self.worker().poll()['error'])
        self.assertEqual(1, len(self.calls))

    def test_crash_before_publication_attempt_commit_can_submit_once_later(self):
        original = bridge._save
        def fail(path, state):
            rec = state['records'].get(self.req['request_id'], {})
            if rec.get('publication_attempt_at'):
                raise OSError('before attempt commit')
            original(path, state)
        with patch.object(bridge, '_save', side_effect=fail):
            self.worker().poll()
        self.assertEqual([], self.transport.publish_calls)
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_crash_after_attempt_commit_before_post_is_never_resent(self):
        original = bridge._save
        def fail(path, state):
            original(path, state)
            rec = state['records'].get(self.req['request_id'], {})
            if rec.get('publication_attempt_at') and rec.get('published_comment_id') is None:
                raise OSError('after attempt commit')
        with patch.object(bridge, '_save', side_effect=fail):
            self.worker().poll()
        self.assertEqual([], self.transport.publish_calls)
        self.now += 10000
        self.worker().poll()
        self.assertEqual([], self.transport.publish_calls)
        self.assertEqual(1, len(self.calls))

    def test_crash_after_post_before_ack_save_reconciles_once(self):
        original = bridge._save
        def fail(path, state):
            rec = state['records'].get(self.req['request_id'], {})
            if rec.get('published_comment_id') is not None:
                raise OSError('before ack save')
            original(path, state)
        with patch.object(bridge, '_save', side_effect=fail):
            self.worker().poll()
        self.assertIsNone(self.record()['published_comment_id'])
        self.worker().poll()
        self.assertIsNotNone(self.record()['published_comment_id'])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_legacy_receipt_lookup_reports_hold_without_rewriting_result(self):
        legacy, _ = self.seed_legacy()
        reqid = next(iter(legacy))
        self.worker().poll()
        before = (self.journal_root/'state.json').read_bytes()
        with patch('davosbot.config.PROJECT_ROOT', self.root), patch('davosbot.config.OWNER_ID', OWNER), patch('davosbot.permissions.OWNER_ID', OWNER):
            reply = actions.execute_action('requests.receipt', {'request_id': reqid, 'request_comment_id': legacy[reqid]['comment_id']}, owner=OWNER)
        evidence = reply['evidence']
        self.assertEqual('held_previous_channel', evidence['publication_state'])
        self.assertEqual(64, evidence['publication_issue_number'])
        self.assertEqual('completed', evidence['request_state'])
        self.assertEqual('b'*40, evidence['runtime_revision'])
        self.assertEqual(before, (self.journal_root/'state.json').read_bytes())
        self.assertNotIn('Old raw result', json.dumps(reply))

    def test_closed_channel_cannot_post_new_results(self):
        self.transport.open = False
        self.worker().poll()
        self.assertEqual([], self.calls)
        self.assertEqual([], self.transport.publish_calls)
        native = bridge.GitHubTransport('.')
        with patch.object(native, 'assert_channel', return_value=False), patch.object(native, '_call') as call:
            with self.assertRaisesRegex(bridge.BridgeError, 'channel_closed'):
                native.publish({'kind': 'davos_result'})
            call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
