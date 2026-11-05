import hashlib
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
from davosbot import work_actions, work_occurrences as core
from davosbot.work_image_receipts import request_scope

class OccurrenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = patch.object(core, '_now', return_value=datetime(2026,10,8,9,tzinfo=ZoneInfo('America/Los_Angeles')))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with patch.object(core, '_now', return_value=datetime(2026,10,7,9,tzinfo=ZoneInfo('America/Los_Angeles'))):
            core.provision(self.temp.name, 'synthetic-owner', {'synthetic-schedule': ['2026-10-08','2027-01-31','08:00']})
        self.args = dict(schedule_id='synthetic-schedule', seattle_date='2026-10-08', part_index=1, part_count=1, message='synthetic', message_hash=hashlib.sha256(b'synthetic').hexdigest())
        digest = self.args['message_hash']
        self.args.update(quote_date='2026-10-08', render_hash=digest, render_part_hashes=[digest,digest], part_hashes=[digest], effective_part_hashes=[digest])
        self.calls = []
    def send(self, args, owner):
        self.calls.append(args)
        return {'status':'ok', 'result':'synthetic receipt', 'evidence':{'message_state':'sent'}}
    def run_action(self, args=None, send=None, root=None):
        with request_scope(str(uuid.uuid4()), 123, 'synthetic-owner', root or self.temp.name):
            with patch.object(work_actions, '_notify', send or self.send):
                work_actions.validate_action('notify.scheduled_self', args or self.args)
                return work_actions._execute('notify.scheduled_self', args or self.args, 'synthetic-owner')
    def test_concurrent_adapter_and_restart(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.run_action(), range(6)))
        self.assertEqual(len(self.calls), 1)
        repeated = self.run_action()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(repeated['evidence']['saved_result']['evidence']['message_state'], 'sent')
        self.assertIn('original_request_id', repeated['evidence'])
    def test_conflicting_body_and_manifest(self):
        self.run_action()
        with self.assertRaisesRegex(ValueError, 'occurrence_conflict'):
            self.run_action(dict(self.args, message='changed', message_hash=hashlib.sha256(b'changed').hexdigest(), part_hashes=[hashlib.sha256(b'changed').hexdigest()], effective_part_hashes=[hashlib.sha256(b'changed').hexdigest()]))
        with self.assertRaisesRegex(ValueError, 'occurrence_conflict'):
            self.run_action(self.multipart())
        self.assertEqual(len(self.calls), 1)
    def test_crash_after_claim_never_replays(self):
        def crash(*args):
            raise RuntimeError('synthetic crash before or after submission')
        with self.assertRaises(RuntimeError):
            self.run_action(self.multipart(),send=crash)
        self.assertTrue(self.run_action(self.multipart())['evidence']['ambiguous'])
        with self.assertRaisesRegex(ValueError, 'previous_part_unconfirmed'):
            self.run_action(dict(self.multipart(),part_index=2))
        self.assertEqual(self.calls, [])
    def test_pending_blocks_later_part(self):
        self.run_action(self.multipart(),send=lambda *a: {'status':'accepted','evidence':{'message_state':'unknown','ambiguous':True}})
        with self.assertRaisesRegex(ValueError, 'previous_part_unconfirmed'):
            self.run_action(dict(self.multipart(),part_index=2))
    def test_invalid_expired_unregistered_and_storage_failure(self):
        for args in (dict(self.args, seattle_date='2000-01-01'), dict(self.args, schedule_id='arbitrary'), dict(self.args, message_hash='0'*64), dict(self.args, part_index=True)):
            with self.assertRaises(ValueError):
                self.run_action(args)
        with patch.object(core.sqlite3, 'connect', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.run_action()
        self.assertEqual(self.calls, [])
    def test_scope_required_and_generic_unchanged(self):
        with self.assertRaisesRegex(ValueError, 'authenticated_request_required'):
            core.execute(self.args, 'synthetic-owner', self.send)
        with patch.object(work_actions, '_notify', self.send):
            for _ in range(2):
                work_actions._execute('notify.self', {'message':'synthetic'}, 'synthetic-owner')
        self.assertEqual(len(self.calls), 2)
    def test_failed_result_checkpoint_keeps_claim(self):
        real_connect = core.sqlite3.connect
        class BrokenCheckpoint:
            def __init__(self, conn): self.conn = conn
            def execute(self, sql, parameters=()):
                if sql.startswith('UPDATE parts'):
                    raise OSError('synthetic result storage failure')
                return self.conn.execute(sql, parameters)
            def commit(self): return self.conn.commit()
            def close(self): return self.conn.close()
        with patch.object(core.sqlite3, 'connect', side_effect=lambda *a, **kw: BrokenCheckpoint(real_connect(*a, **kw))):
            with self.assertRaises(OSError): self.run_action()
        self.assertTrue(self.run_action()['evidence']['ambiguous'])
        self.assertEqual(len(self.calls), 1)

    def test_process_restart_does_not_replay(self):
        import json
        import subprocess
        import sys
        self.run_action()
        code = '''import json,sys
from davosbot import work_occurrences as core
from davosbot.work_image_receipts import request_scope
from datetime import datetime
from zoneinfo import ZoneInfo
core._now=lambda:datetime(2026,10,8,9,tzinfo=ZoneInfo('America/Los_Angeles'))
with request_scope(sys.argv[3],123,'synthetic-owner',sys.argv[1]):
 def forbidden(*a): raise AssertionError('must not send')
 result=core.execute(json.loads(sys.argv[2]),'synthetic-owner',forbidden)
 assert result['evidence']['saved_result']['evidence']['message_state']=='sent'
'''
        result = subprocess.run([sys.executable, '-c', code, self.temp.name, json.dumps(self.args), str(uuid.uuid4())], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_adapter_owner_gate(self):
        with patch('davosbot.config.OWNER_ID', 'synthetic-owner'), patch('davosbot.config.normalize_handle', side_effect=lambda x:x), patch('davosbot.permissions.is_owner', side_effect=lambda x:x=='synthetic-owner'):
            result = work_actions.execute_action('notify.scheduled_self', self.args, owner='someone-else')
            self.assertEqual(result['evidence']['code'], 'owner_required')
            with request_scope(str(uuid.uuid4()), 123, 'synthetic-owner', self.temp.name):
                with patch.object(work_actions, '_notify', self.send):
                    result = work_actions.execute_action('notify.scheduled_self', self.args, owner='synthetic-owner')
                    self.assertEqual(result['evidence']['message_state'], 'sent')
        self.assertEqual(len(self.calls), 1)

    def test_lost_or_damaged_store_never_recreates_history(self):
        import json
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        self.run_action()
        db = Path(self.temp.name) / 'schedule_occurrences.sqlite3'
        marker = db.with_name('schedule_occurrence_activation.json')
        original, activation = db.read_bytes(), marker.read_bytes()
        for damage in ('missing','empty','missing_table','wrong_schema','wrong_owner','wrong_binding','corrupt','missing_marker'):
            with self.subTest(damage=damage):
                db.write_bytes(original)
                marker.write_bytes(activation)
                if damage == 'missing': db.unlink()
                elif damage == 'empty': db.write_bytes(b'')
                elif damage == 'corrupt': db.write_bytes(b'not sqlite')
                elif damage == 'missing_marker': marker.unlink()
                else:
                    with closing(sqlite3.connect(db)) as conn, conn:
                        if damage == 'missing_table': conn.execute('DROP TABLE parts')
                        elif damage == 'wrong_schema': conn.execute('PRAGMA user_version=99')
                        elif damage == 'wrong_owner': conn.execute("UPDATE activation SET owner='wrong'")
                        else: conn.execute("UPDATE activation SET binding='wrong'")
                with self.assertRaises((ValueError, OSError, sqlite3.Error)):
                    self.run_action()
                self.assertEqual(len(self.calls), 1)
                if damage == 'missing': self.assertFalse(db.exists())

    def test_full_manifest_render_part_conflict(self):
        self.run_action()
        changed = dict(self.args, render_part_hashes=[self.args['message_hash'], 'a'*64])
        with self.assertRaisesRegex(ValueError, 'occurrence_conflict'): self.run_action(changed)
        self.assertEqual(len(self.calls), 1)

    def test_whitespace_exact_input_and_effective_identity(self):
        from davosbot.text_safety import normalize_bot_text
        message = '  synthetic  \n routine '
        input_hash, effective_hash = core._hash(message), core._hash(normalize_bot_text(message))
        args = dict(self.args, message=message, message_hash=input_hash, part_hashes=[input_hash], effective_part_hashes=[effective_hash])
        result = self.run_action(args)
        self.assertEqual(self.calls[0]['message'], message)
        self.assertEqual(result['evidence']['input_hash'], input_hash)
        self.assertEqual(result['evidence']['effective_hash'], effective_hash)
        self.assertNotEqual(input_hash, effective_hash)
        with self.assertRaisesRegex(ValueError, 'semantic_normalization_rejected'):
            self.run_action(dict(args,message='my g'))

    def test_due_dates_and_horizon(self):
        for now, code in ((datetime(2026,10,8,7,59,tzinfo=ZoneInfo('America/Los_Angeles')), 'occurrence_not_due'), (datetime(2026,10,9,0,1,tzinfo=ZoneInfo('America/Los_Angeles')), 'occurrence_expired')):
            with patch.object(core,'_now',return_value=now):
                with self.assertRaisesRegex(ValueError,code): self.run_action()
        with self.assertRaisesRegex(ValueError,'invalid_manifest'):
            self.run_action(dict(self.args,render_part_hashes=['a'*64]*17))
        self.assertEqual(self.calls, [])
        self.assertTrue(self.run_action()['evidence']['late'])

    def test_activation_reserves_legacy_day(self):
        import pathlib
        root = pathlib.Path(self.temp.name) / 'migration'
        root.mkdir()
        core.provision(root, 'synthetic-owner', {'synthetic-schedule':['2026-10-08','2027-01-31','08:00']})
        with request_scope(str(uuid.uuid4()),123,'synthetic-owner',root):
            with self.assertRaisesRegex(ValueError,'legacy_occurrence_requires_reconciliation'):
                core.execute(self.args,'synthetic-owner',self.send)
        self.assertEqual(self.calls, [])

    def test_actual_bridge_and_receipt_preserve_baseline_unknown(self):
        import json
        from pathlib import Path
        from davosbot import work_bridge as bridge
        from test_work_bridge import FakeTransport, NOW, comment, request
        root = Path(self.temp.name)
        state_root = root / '.work_bridge'
        state_root.mkdir()
        with patch.object(core,'_now',return_value=datetime(2026,10,7,9,tzinfo=ZoneInfo('America/Los_Angeles'))):
            core.provision(state_root,'synthetic-owner',{'synthetic-schedule':['2026-10-08','2027-01-31','08:00']})
        ids = [str(uuid.uuid4()),str(uuid.uuid4())]
        comments = [comment(request(request_id=rid,action='notify.scheduled_self',args=self.multipart()),comment_id=5354900000+i) for i,rid in enumerate(ids)]
        transport = FakeTransport(comments)
        calls = []
        def weak_send(*a):
            calls.append(a)
            return {'status':'accepted','result':'unknown baseline','evidence':{'message_state':'unknown','ambiguous':True}}
        with patch('davosbot.config.OWNER_ID','synthetic-owner'), patch('davosbot.config.normalize_handle',side_effect=lambda x:x), patch('davosbot.permissions.is_owner',return_value=True), patch('davosbot.config.PROJECT_ROOT',root), patch.object(work_actions,'_notify',weak_send):
            worker = bridge.WorkBridge(root,'synthetic-owner',transport=transport,validate_action=work_actions.validate_action,execute_action=work_actions.execute_action,clock=lambda:NOW,revision='a'*40)
            worker.poll()
            state = bridge._load(state_root / 'state.json')
            for index,rid in enumerate(ids):
                response = state['records'][rid]['response']
                self.assertEqual(response['state'],'ambiguous')
                self.assertTrue(response['result']['evidence']['ambiguous'])
                receipt = work_actions.execute_action('requests.receipt',{'request_id':rid,'request_comment_id':5354900000+index},owner='synthetic-owner')
                self.assertEqual(receipt['evidence']['request_state'],'ambiguous')
                self.assertTrue(receipt['evidence']['saved_confirmation']['ambiguous'])
                self.assertEqual(receipt['evidence']['saved_confirmation']['message_state'],'unknown')
                self.assertEqual(receipt['evidence']['saved_confirmation']['attribution_state'],'heuristic')
                self.assertIs(receipt['evidence']['saved_confirmation']['request_attributed'],False)
            self.assertEqual(len(calls),1)
            next_id = str(uuid.uuid4())
            transport.rows.append(comment(request(request_id=next_id,action='notify.scheduled_self',args=dict(self.multipart(),part_index=2)),comment_id=5354900050))
            worker.poll()
            blocked = bridge._load(state_root/'state.json')['records'][next_id]['response']
            self.assertEqual(blocked['result']['evidence']['code'],'previous_part_unconfirmed')
            self.assertEqual(len(calls),1)
            with request_scope(str(uuid.uuid4()),123,'synthetic-owner',state_root):
                with self.assertRaisesRegex(ValueError,'previous_part_unconfirmed'):
                    core.execute(dict(self.multipart(),part_index=2),'synthetic-owner',weak_send)
            self.assertEqual(len(calls),1)

    def test_twelve_process_claims_one_synthetic_effect(self):
        import json
        import subprocess
        import sys
        from pathlib import Path
        code = '''import json,sys
from datetime import datetime
from zoneinfo import ZoneInfo
from davosbot import work_occurrences as core
from davosbot.work_image_receipts import request_scope
core._now=lambda:datetime(2026,10,8,9,tzinfo=ZoneInfo('America/Los_Angeles'))
with request_scope(sys.argv[3],123,'synthetic-owner',sys.argv[1]):
 def synthetic(*a):
  with open(sys.argv[4],'a') as f: f.write('effect\\n')
  return {'status':'ok','evidence':{'message_state':'sent'}}
 core.execute(json.loads(sys.argv[2]),'synthetic-owner',synthetic)
'''
        effect = Path(self.temp.name) / 'synthetic-effects.txt'
        def process(_):
            result = subprocess.run([sys.executable,'-c',code,self.temp.name,json.dumps(self.args),str(uuid.uuid4()),str(effect)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
        with ThreadPoolExecutor(max_workers=12) as pool: list(pool.map(process,range(12)))
        self.assertEqual(effect.read_text().splitlines(), ['effect'])

    def multipart(self):
        return dict(self.args,part_count=2,part_index=1,part_hashes=[self.args['message_hash']]*2,effective_part_hashes=[self.args['message_hash']]*2)

    def test_multipart_success_and_unknown_stops_continuation(self):
        args = self.multipart()
        self.run_action(args)
        self.run_action(dict(args,part_index=2))
        self.run_action(dict(args,part_index=2))
        self.assertEqual(len(self.calls),2)

    def test_sunday_single_final_manifest_separate_from_render(self):
        self.run_action()
        with self.assertRaisesRegex(ValueError,'occurrence_conflict'):
            self.run_action(dict(self.args,render_hash='b'*64))
        self.assertEqual(len(self.calls),1)

    def test_actual_bridge_multipart_normal_success_and_receipts(self):
        from contextlib import closing
        import sqlite3
        from pathlib import Path
        from davosbot import work_bridge as bridge
        from test_work_bridge import FakeTransport, NOW, comment, request
        root = Path(self.temp.name) / 'normal-bridge'
        state_root = root / '.work_bridge'
        state_root.mkdir(parents=True)
        with patch.object(core,'_now',return_value=datetime(2026,10,7,9,tzinfo=ZoneInfo('America/Los_Angeles'))):
            core.provision(state_root,'synthetic-owner',{'synthetic-schedule':['2026-10-08','2027-01-31','08:00']})
        messages = root / 'synthetic-messages.sqlite'
        with closing(sqlite3.connect(messages)) as conn, conn:
            conn.executescript('CREATE TABLE message(id INTEGER PRIMARY KEY,text TEXT,is_sent INTEGER,error INTEGER,is_from_me INTEGER); CREATE TABLE chat(id INTEGER PRIMARY KEY,chat_identifier TEXT); CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);')
            conn.execute('INSERT INTO chat VALUES(1,?)',('synthetic-owner',))
        native_calls = []
        def native_send(owner,text,**kwargs):
            self.assertEqual(kwargs,{'is_group':False,'recovery_mode':'none'})
            native_calls.append(text)
            with closing(sqlite3.connect(messages)) as conn, conn:
                row = conn.execute('INSERT INTO message(text,is_sent,error,is_from_me) VALUES(?,1,0,1)',(text,)).lastrowid
                conn.execute('INSERT INTO chat_message_join VALUES(1,?)',(row,))
            return True
        indexes = [1,2,1,2]
        ids = [str(uuid.uuid4()) for _ in indexes]
        transport = FakeTransport([comment(request(request_id=rid,action='notify.scheduled_self',args=dict(self.multipart(),part_index=index)),comment_id=5354900100+i) for i,(rid,index) in enumerate(zip(ids,indexes))])
        with patch('davosbot.config.OWNER_ID','synthetic-owner'), patch('davosbot.config.normalize_handle',side_effect=lambda x:x), patch('davosbot.permissions.is_owner',return_value=True), patch('davosbot.config.PROJECT_ROOT',root), patch('davosbot.config.DB_PATH',str(messages)), patch('davosbot.imessage.send_message',native_send):
            bridge.WorkBridge(root,'synthetic-owner',transport=transport,validate_action=work_actions.validate_action,execute_action=work_actions.execute_action,clock=lambda:NOW,revision='a'*40).poll()
            state = bridge._load(state_root / 'state.json')
            for i,rid in enumerate(ids):
                response = state['records'][rid]['response']
                self.assertEqual(response['state'],'completed')
                self.assertEqual(response['result']['status'],'ok')
                evidence = response['result']['evidence']
                self.assertEqual(evidence['message_state'],'sent')
                self.assertIsNot(evidence.get('ambiguous'),True)
                self.assertEqual(evidence['attribution_state'],'heuristic')
                self.assertIs(evidence['request_attributed'],False)
                receipt = work_actions.execute_action('requests.receipt',{'request_id':rid,'request_comment_id':5354900100+i},owner='synthetic-owner')
                self.assertEqual(receipt['evidence']['saved_confirmation']['attribution_state'],'heuristic')
                self.assertIs(receipt['evidence']['saved_confirmation']['request_attributed'],False)
        self.assertEqual(native_calls,['synthetic','synthetic'])

    def test_crash_before_and_after_each_multipart_effect_never_replays(self):
        from pathlib import Path
        for crash_index in (1,2,3):
            for after_effect in (False,True):
                with self.subTest(part=crash_index,after_effect=after_effect):
                    root = Path(self.temp.name) / f'crash-{crash_index}-{after_effect}'
                    root.mkdir()
                    with patch.object(core,'_now',return_value=datetime(2026,10,7,9,tzinfo=ZoneInfo('America/Los_Angeles'))):
                        core.provision(root,'synthetic-owner',{'synthetic-schedule':['2026-10-08','2027-01-31','08:00']})
                    args = dict(self.multipart(),part_count=3,part_hashes=[self.args['message_hash']]*3,effective_part_hashes=[self.args['message_hash']]*3)
                    effects = []
                    def success(*a):
                        effects.append(a)
                        return {'status':'ok','evidence':{'message_state':'sent'}}
                    def crash(*a):
                        if after_effect: effects.append(a)
                        raise RuntimeError('synthetic interruption')
                    for index in range(1,crash_index): self.run_action(dict(args,part_index=index),send=success,root=root)
                    with self.assertRaises(RuntimeError): self.run_action(dict(args,part_index=crash_index),send=crash,root=root)
                    reply = self.run_action(dict(args,part_index=crash_index),send=success,root=root)
                    self.assertTrue(reply['evidence']['ambiguous'])
                    self.assertIsNone(reply['evidence']['saved_result'])
                    self.assertEqual(len(effects),crash_index-1+int(after_effect))
                    if crash_index < 3:
                        with self.assertRaisesRegex(ValueError,'previous_part_unconfirmed'):
                            self.run_action(dict(args,part_index=crash_index+1),send=success,root=root)

    def test_sequencing_preserves_original_outcomes(self):
        from pathlib import Path
        outcomes = [
            {'status':'accepted','evidence':{'message_state':'unknown','ambiguous':True}},
            {'status':'accepted','evidence':{'message_state':'pending','ambiguous':False}},
            {'status':'error','evidence':{'message_state':'failed','ambiguous':False}},
            {'status':'ok','evidence':{'message_state':'sent','ambiguous':True}},
        ]
        for index,original in enumerate(outcomes):
            with self.subTest(original=original):
                root = Path(self.temp.name) / f'outcome-{index}'
                root.mkdir()
                with patch.object(core,'_now',return_value=datetime(2026,10,7,9,tzinfo=ZoneInfo('America/Los_Angeles'))):
                    core.provision(root,'synthetic-owner',{'synthetic-schedule':['2026-10-08','2027-01-31','08:00']})
                result = self.run_action(self.multipart(),send=lambda *a:original,root=root)
                repeated = self.run_action(self.multipart(),root=root)
                self.assertEqual(result['status'],original['status'])
                self.assertEqual(repeated['status'],original['status'])
                self.assertEqual(repeated['evidence']['saved_result'],original)
                self.assertEqual(repeated['evidence']['ambiguous'],original['evidence']['ambiguous'])
                with self.assertRaisesRegex(ValueError,'previous_part_unconfirmed'):
                    self.run_action(dict(self.multipart(),part_index=2),root=root)

    def test_actual_bridge_crash_before_after_each_part_and_new_uuid_replay(self):
        from pathlib import Path
        from davosbot import work_bridge as bridge
        from test_work_bridge import FakeTransport, NOW, comment, request
        for crash_index in (1,2,3):
            for after_effect in (False,True):
                with self.subTest(part=crash_index,after_effect=after_effect):
                    root = Path(self.temp.name)/f'bridge-crash-{crash_index}-{after_effect}'
                    state_root = root/'.work_bridge'
                    state_root.mkdir(parents=True)
                    with patch.object(core,'_now',return_value=datetime(2026,10,7,9,tzinfo=ZoneInfo('America/Los_Angeles'))):
                        core.provision(state_root,'synthetic-owner',{'synthetic-schedule':['2026-10-08','2027-01-31','08:00']})
                    args = dict(self.multipart(),part_count=3,part_hashes=[self.args['message_hash']]*3,effective_part_hashes=[self.args['message_hash']]*3)
                    indexes = [1,2,3,crash_index]
                    ids = [str(uuid.uuid4()) for _ in indexes]
                    transport = FakeTransport([comment(request(request_id=rid,action='notify.scheduled_self',args=dict(args,part_index=index)),comment_id=5354900200+i) for i,(rid,index) in enumerate(zip(ids,indexes))])
                    attempts, effects = [], []
                    def synthetic(*a):
                        attempts.append(a)
                        if len(attempts)==crash_index:
                            if after_effect: effects.append(a)
                            raise RuntimeError('synthetic crash around submission')
                        effects.append(a)
                        return {'status':'ok','evidence':{'message_state':'sent'}}
                    with patch('davosbot.config.OWNER_ID','synthetic-owner'), patch('davosbot.config.normalize_handle',side_effect=lambda x:x), patch('davosbot.permissions.is_owner',return_value=True), patch('davosbot.config.PROJECT_ROOT',root), patch.object(work_actions,'_notify',synthetic):
                        bridge.WorkBridge(root,'synthetic-owner',transport=transport,validate_action=work_actions.validate_action,execute_action=work_actions.execute_action,clock=lambda:NOW,revision='a'*40).poll()
                        state = bridge._load(state_root/'state.json')
                        for rid in (ids[crash_index-1],ids[-1]):
                            response = state['records'][rid]['response']
                            self.assertEqual(response['state'],'ambiguous')
                            self.assertTrue(response['result']['evidence']['ambiguous'])
                        receipt = work_actions.execute_action('requests.receipt',{'request_id':ids[-1],'request_comment_id':5354900203},owner='synthetic-owner')
                        self.assertEqual(receipt['evidence']['request_state'],'ambiguous')
                        self.assertTrue(receipt['evidence']['saved_confirmation']['ambiguous'])
                    self.assertEqual(len(attempts),crash_index)
                    self.assertEqual(len(effects),crash_index-1+int(after_effect))
