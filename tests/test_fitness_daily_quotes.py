"""Synthetic-only daily quote cache, isolation, clock and consumer transport tests."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from davosbot import fitness_context as fitness, fitness_quotes as quotes
from davosbot import morning_quotes, permissions, tools, work_actions, work_actions_extra as extra
import test_fitness_context as fixtures

OWNER, OTHER = fixtures.OWNER, fixtures.OTHER
NOW = datetime(2026, 10, 8, 18, tzinfo=timezone.utc)
PUBLIC = 'Keep moving.\n- Synthetic author\n\nSource: https://zenquotes.io/'


def choice(day, text=PUBLIC, source='zenquotes'):
    return {'date': day, 'text': text, 'source': source, 'hash': morning_quotes._quote_hash(text)}


class DailyQuoteTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(quotes.config, 'PROJECT_ROOT', self.directory))
        self.stack.enter_context(patch.object(permissions, 'OWNER_ID', OWNER))
        self.stack.enter_context(patch.object(quotes.config, 'OWNER_ID', OWNER))
        self.path = self.directory / '.fitness_quotes' / 'owner.json'

    def select(self, day, recent):
        return choice(day, f'Synthetic step for {day}.'), {'provider_called': True, 'model_called': False}

    def install(self, now=NOW):
        with patch.object(quotes, '_select', side_effect=self.select):
            return quotes.get_daily_quote(OWNER, now=now, generate=True)

    def test_cache_only_read_creates_nothing_and_calls_no_provider(self):
        with patch.object(quotes, '_select', side_effect=AssertionError('provider')):
            self.assertIsNone(quotes.get_daily_quote(OWNER, now=NOW))
        self.assertFalse(self.path.parent.exists())

    def test_owner_gate_precedes_path_or_provider(self):
        with patch.object(quotes, '_cache_path', side_effect=AssertionError('private path')):
            for owner in (OTHER, '', None):
                with self.assertRaisesRegex(ValueError, 'owner_required'):
                    quotes.get_daily_quote(owner, now=NOW, generate=True)

    def test_day_cache_is_persistent_and_shared_without_repeated_calls(self):
        with patch.object(quotes, '_select', side_effect=self.select) as select:
            first = quotes.get_daily_quote(OWNER, now=NOW, generate=True)
            second = quotes.get_daily_quote(OWNER, now=NOW + timedelta(hours=3), generate=True)
            third = quotes.get_daily_quote(OWNER, now=NOW)
        self.assertEqual(select.call_count, 1)
        self.assertFalse(first['cache_hit'])
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['text'], third['text'])
        self.assertFalse(second['provider_called'])
        self.assertFalse(second['model_called'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_midnight_and_dst_use_actual_pacific_date(self):
        clocks = [(datetime(2026, 10, 8, 6, 59, tzinfo=timezone.utc), '2026-10-07'),
                  (datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc), '2026-10-08'),
                  (datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc), '2026-11-01'),
                  (datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc), '2026-11-01')]
        with patch.object(quotes, '_select', side_effect=self.select) as select:
            for now, expected in clocks:
                self.assertEqual(quotes.get_daily_quote(OWNER, now=now, generate=True)['date'], expected)
        self.assertEqual(select.call_count, 3)
        with self.assertRaisesRegex(ValueError, 'timezone_required'):
            quotes.get_daily_quote(OWNER, now=NOW.replace(tzinfo=None), generate=True)

    def test_next_day_rejects_old_cache_without_changing_it_on_preview(self):
        self.install()
        before = self.path.read_bytes()
        self.assertIsNone(quotes.get_daily_quote(OWNER, now=NOW + timedelta(days=1)))
        self.assertEqual(before, self.path.read_bytes())
        with patch.object(quotes, '_select', side_effect=self.select) as select:
            quotes.get_daily_quote(OWNER, now=NOW + timedelta(days=1), generate=True)
            recent = select.call_args.args[1]
        self.assertIn(choice('2026-10-08', 'Synthetic step for 2026-10-08.')['hash'], recent)
        state = json.loads(self.path.read_text())
        self.assertEqual(set(state['history'][0]), {'date', 'hash'})

    def test_history_bounded_and_no_previous_quote_text_retained(self):
        for offset in range(70):
            self.install(NOW + timedelta(days=offset))
        state = json.loads(self.path.read_text())
        self.assertEqual(len(state['history']), 60)
        self.assertLess(self.path.stat().st_size, quotes.MAX_CACHE_BYTES)
        self.assertNotIn('Synthetic step for 2026-10-08.', self.path.read_text())

    def test_exhausted_local_fallbacks_leave_oldest_available_not_yesterday(self):
        lines = morning_quotes._FALLBACK_QUOTES
        with patch.object(quotes, '_select', side_effect=lambda d, r: (choice(d, lines[(datetime.fromisoformat(d) - datetime(2026, 10, 8)).days % 10], 'fallback:gemini_error'), {'provider_called': False, 'model_called': False})):
            for offset in range(10):
                quotes.get_daily_quote(OWNER, now=NOW + timedelta(days=offset), generate=True)
        with patch.object(quotes, '_select', side_effect=self.select) as select:
            quotes.get_daily_quote(OWNER, now=NOW + timedelta(days=10), generate=True)
        recent = select.call_args.args[1]
        self.assertNotIn(morning_quotes._quote_hash(lines[0]), recent)
        self.assertIn(morning_quotes._quote_hash(lines[-1]), recent)

    def test_corruption_owner_mismatch_and_symlinks_fail_without_provider(self):
        self.install()
        original = self.path.read_bytes()
        state = json.loads(original)
        bads = [b'{', b'x' * (quotes.MAX_CACHE_BYTES + 1), json.dumps({**state, 'owner': 'different'}).encode(),
                json.dumps({**state, 'current': {**state['current'], 'hash': 'bad'}}).encode()]
        with patch.object(quotes, '_select', side_effect=AssertionError('provider')):
            for bad in bads:
                self.path.write_bytes(bad)
                with self.assertRaisesRegex(ValueError, 'invalid_quote_cache'):
                    quotes.get_daily_quote(OWNER, now=NOW, generate=True)
            self.path.unlink()
            target = self.directory / 'other.json'; target.write_bytes(original)
            self.path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'invalid_quote_cache'):
                quotes.get_daily_quote(OWNER, now=NOW, generate=True)

    def test_cache_lock_prevents_simultaneous_provider_churn(self):
        with quotes._lock(self.path), patch.object(quotes, '_select', side_effect=AssertionError('provider')):
            with self.assertRaisesRegex(ValueError, 'quote_preparing'):
                quotes.get_daily_quote(OWNER, now=NOW, generate=True)
        self.assertFalse(self.path.exists())

    def test_cache_and_directory_must_remain_private_regular_files(self):
        self.install()
        for target, bad_mode, good_mode in ((self.path,0o644,0o600),(self.path.parent,0o777,0o700)):
            target.chmod(bad_mode)
            with self.assertRaisesRegex(ValueError,'invalid_quote_cache'):
                quotes.get_daily_quote(OWNER,now=NOW,generate=True)
            target.chmod(good_mode)
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        with self.assertRaises((ValueError,OSError)):
            quotes.get_daily_quote(OWNER,now=NOW,generate=True)

    def test_clock_reversal_and_mismatched_choice_do_not_write(self):
        self.install()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'clock_reversed'):
            quotes.get_daily_quote(OWNER, now=NOW - timedelta(days=1), generate=True)
        with patch.object(quotes, '_select', return_value=(choice('2099-01-01'), {'provider_called': True, 'model_called': False})):
            with self.assertRaisesRegex(ValueError, 'invalid_quote'):
                quotes.get_daily_quote(OWNER, now=NOW + timedelta(days=1), generate=True)
        self.assertEqual(before, self.path.read_bytes())

    def test_primary_pipeline_retains_attribution_and_never_touches_group_log(self):
        day = quotes._day(datetime.now(timezone.utc))
        with patch.object(morning_quotes, '_fetch_zenquotes_quote', return_value=PUBLIC), \
             patch.object(tools, '_gemini_rewrite', side_effect=AssertionError('model')), \
             patch.object(morning_quotes, '_log_quote_choice', side_effect=AssertionError('group log')), \
             patch.object(morning_quotes, '_recent_quote_hashes', side_effect=AssertionError('group read')):
            result, calls = quotes._select(day, set())
        self.assertEqual(result['text'], PUBLIC)
        self.assertEqual(result['source'], 'zenquotes')
        self.assertTrue(calls['provider_called']); self.assertFalse(calls['model_called'])

    def test_existing_gemini_and_local_fallbacks_have_no_invented_author(self):
        day = quotes._day(datetime.now(timezone.utc))
        with patch.object(morning_quotes, '_fetch_zenquotes_quote', side_effect=RuntimeError('offline')), \
             patch.object(tools, 'GEMINI_API_KEY', 'synthetic-key'), \
             patch.object(quotes, '_rewrite_line', side_effect=lambda prompt, **kwargs: (kwargs['before_request'](), 'One useful step matters.')[1]) as rewrite:
            result, calls = quotes._select(day, set())
        self.assertEqual(result['text'], 'One useful step matters.')
        self.assertEqual(result['source'], 'gemini')
        self.assertTrue(calls['model_called'])
        self.assertNotIn('fitness', rewrite.call_args.args[0])
        self.assertNotIn(OWNER, rewrite.call_args.args[0])
        with patch.object(morning_quotes, '_fetch_zenquotes_quote', side_effect=RuntimeError('offline')), \
             patch.object(tools, 'GEMINI_API_KEY', ''), \
             patch.object(tools, '_gemini_rewrite', side_effect=AssertionError('model')):
            result, calls = quotes._select(day, set())
        self.assertIn(result['text'], morning_quotes._FALLBACK_QUOTES)
        self.assertFalse(calls['model_called'])
        self.assertNotIn('\n- ', result['text'])

    def test_duplicate_and_malformed_provider_text_reach_existing_fallback(self):
        day = quotes._day(datetime.now(timezone.utc))
        for public in (PUBLIC, 'x' * 701, 'bad\x00quote'):
            with patch.object(morning_quotes, '_fetch_zenquotes_quote', return_value=public), \
                 patch.object(tools, 'GEMINI_API_KEY', ''):
                result, _ = quotes._select(day, {morning_quotes._quote_hash(PUBLIC)})
            self.assertIn(result['text'], morning_quotes._FALLBACK_QUOTES)


class DailyConsumerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(quotes.config, 'PROJECT_ROOT', self.directory))
        self.stack.enter_context(patch.object(permissions, 'OWNER_ID', OWNER))
        self.stack.enter_context(patch.object(quotes.config, 'OWNER_ID', OWNER))
        self.plan = fixtures.fixture(); self.plan['consumer_quote_policy'] = 'cole_daily'
        self.stack.enter_context(patch.object(fitness, 'load_current', return_value=(self.plan, 'synthetic')))

    def reply(self, requested='today', **kw):
        return fitness.plan_command_reply("What's my workout for " + requested, sender=OWNER, chat_id=OWNER, is_group=False, now=NOW, **kw)

    def test_narrow_policy_excludes_static_and_unknown_modes(self):
        self.assertEqual(fitness.parse_plan(json.dumps(self.plan)), self.plan)
        for updates in ({'consumer_quote':'Static'}, {'consumer_quote_policy':'evil'}, {'consumer_quote_policy':None}, {'consumer_quote_policy':[]}):
            with self.assertRaises(ValueError):
                fitness.parse_plan(json.dumps({**self.plan, **updates}))

    def test_future_query_caches_current_day_and_keeps_full_body(self):
        pick = lambda day, recent: (choice(day), {'provider_called':True, 'model_called':False})
        with patch.object(quotes, '_select', side_effect=pick) as select:
            future = self.reply('tomorrow')
            today = self.reply()
        self.assertEqual(select.call_count, 1)
        self.assertEqual(select.call_args.args[0], '2026-10-08')
        self.assertTrue(future.startswith('Workout date: Friday, October 9, 2026.'))
        self.assertTrue(today.startswith("Today's date is Thursday, October 8, 2026."))
        self.assertIn("Today's motivation:\n" + PUBLIC, future)
        self.assertIn(PUBLIC, today)
        self.assertIn(fitness._consumer_typography(self.plan['weekly_schedule']['Friday']['details']), future)
        self.assertIn(self.plan['daily_habits'][0], future)

    def test_group_nonowner_or_unavailable_plan_never_gets_quote(self):
        with patch.object(quotes, 'get_daily_quote', side_effect=AssertionError('quote call')):
            for sender, chat, group in ((OTHER, OTHER, False), (OWNER, OTHER, False), (OWNER, 'a'*32, True)):
                self.assertIsNone(fitness.plan_command_reply('workout plan', sender=sender, chat_id=chat, is_group=group))
            self.plan['status']='paused'
            self.assertIn('paused', self.reply())

    def test_preview_calls_no_provider_then_matches_cached_native_reply(self):
        original = fitness.plan_command_reply
        with patch.object(fitness, 'plan_command_reply', side_effect=lambda text, **kw: original(text, now=NOW, **kw)), \
             patch.object(quotes, '_select', side_effect=AssertionError('provider')):
            preview = extra.execute_extra_action('fitness.plan.preview', {'query': "What's my workout today"}, OWNER)['result']
        self.assertFalse(preview['model_called']); self.assertFalse(preview['sent'])
        self.assertIn("isn't available", preview['plan_reply'])
        self.assertFalse((self.directory/'.fitness_quotes').exists())
        with patch.object(quotes, '_select', return_value=(choice('2026-10-08'), {'provider_called':True,'model_called':False})):
            native = self.reply()
        with patch.object(fitness, 'plan_command_reply', side_effect=lambda text, **kw: original(text, now=NOW, **kw)):
            preview = extra.execute_extra_action('fitness.plan.preview', {'query': "What's my workout today"}, OWNER)['result']
        self.assertEqual(preview['plan_reply'], native)

    def test_render_action_reports_actual_calls_and_matches_native_exactly(self):
        original = fitness.plan_command_reply
        with patch.object(fitness, 'plan_command_reply', side_effect=lambda text, **kw: original(text, now=NOW, **kw)), \
             patch.object(quotes, '_select', return_value=(choice('2026-10-08'), {'provider_called':True, 'model_called':False})):
            result = extra.execute_extra_action('fitness.plan.render', {'query': "What's my workout today"}, OWNER)['result']
        self.assertTrue(result['provider_called']); self.assertFalse(result['model_called']); self.assertFalse(result['sent'])
        self.assertEqual(''.join(result['message_parts']), self.reply())
        self.assertEqual(result['quote']['date'], '2026-10-08')
        with self.assertRaisesRegex(ValueError, 'owner_required'):
            extra.execute_extra_action('fitness.plan.render', {'query':'workout plan'}, OTHER)
        for field in ('recipient','date','url','db_path','provider','sender','now'):
            with self.assertRaises(ValueError):
                extra.validate_extra_action('fitness.plan.render', {'query':'workout plan',field:'arbitrary'})

    def test_failure_does_not_falsely_claim_no_provider_call(self):
        original = fitness.plan_command_reply
        with patch.object(fitness, 'plan_command_reply', side_effect=lambda text, **kw: original(text, now=NOW, **kw)), \
             patch.object(quotes, 'get_daily_quote', side_effect=OSError('write failed')):
            result = extra.execute_extra_action('fitness.plan.render', {'query':'workout plan'}, OWNER)['result']
        self.assertIsNone(result['provider_called']); self.assertIsNone(result['model_called'])
        self.assertIn("isn't available", ''.join(result['message_parts']))

    def test_transport_parts_are_lossless_bounded_and_pass_notify_schema(self):
        texts = ['x'*2001, ('word '*550)+'tail', ('first\n'*700)+'last', self.reply(prepare_quote=False)]
        for text in texts:
            parts = fitness.consumer_message_parts(text)
            self.assertEqual(''.join(parts),text)
            for part in parts:
                self.assertLessEqual(len(part), 2000)
                work_actions.validate_action('notify.self', {'message':part})

    def test_grounding_and_metadata_exclude_quote_policy_and_provider_calls(self):
        plain=deepcopy(self.plan); del plain['consumer_quote_policy']
        with patch.object(quotes,'get_daily_quote',side_effect=AssertionError('quote call')):
            self.assertEqual(fitness.render_context(self.plan, 'tomorrow workout', now=NOW), fitness.render_context(plain,'tomorrow workout',now=NOW))
            self.assertNotIn('cole_daily', json.dumps(fitness.metadata(self.plan,'synthetic')))


if __name__ == '__main__':
    unittest.main()

class FullBridgeTransportTests(DailyConsumerTests):
    def test_full_action_unicode_quote_and_plan_pages_preserve_every_character(self):
        text = 'é' * 500 + '\n- ' + 'é' * 120 + '\n\nSource: https://zenquotes.io/'
        self.plan['daily_habits']=['é'*170 for _ in range(8)]
        original=fitness.plan_command_reply
        with patch.object(fitness,'plan_command_reply',side_effect=lambda q,**kw:original(q,now=NOW,**kw)), \
             patch.object(quotes,'_select',return_value=(choice('2026-10-08',text),{'provider_called':True,'model_called':False})):
            response=work_actions.execute_action('fitness.plan.render',{'query':'workout plan'},OWNER)
        self.assertEqual(response['status'],'ok',response)
        result=response['result']; assembled=result['message_parts'][:]
        self.assertNotIn('text',result['quote'])
        digest=result['render_hash']; count=result['part_count']
        self.assertGreater(count,1)
        while result['next_part_index'] is not None:
            args={'query':'workout plan','part_index':result['next_part_index'],'expected_render_hash':digest}
            with patch.object(fitness,'plan_command_reply',side_effect=lambda q,**kw:original(q,now=NOW,**kw)):
                response=work_actions.execute_action('fitness.plan.render',args,OWNER)
            self.assertEqual(response['status'],'ok',response)
            self.assertLessEqual(len(json.dumps(response,ensure_ascii=True).encode()),7800)
            result=response['result']; assembled+=result['message_parts']
            self.assertEqual(result['render_hash'],digest)
        self.assertEqual(len(assembled),count)
        self.assertEqual(''.join(assembled),self.reply())
        for part in assembled:
            work_actions.validate_action('notify.self',{'message':part})

    def test_existing_sanitizers_fail_closed_instead_of_claiming_exact_body(self):
        for text in ('The secret: consistency is simple.', 'A token: synthetic phrase.', 'sk-'+'x'*20, 'x'*1984+' secret: example phrase.'):
            with patch.object(fitness,'plan_command_reply',return_value=text):
                result=work_actions.execute_action('fitness.plan.render',{'query':'workout plan'},OWNER)
            self.assertEqual(result['status'],'error')
            self.assertEqual(result['evidence']['code'],'fitness_render_redacted')
            self.assertNotIn(text,json.dumps(result))

    def test_stale_or_missing_page_hash_fails_without_truncation(self):
        with self.assertRaises(ValueError):
            extra.validate_extra_action('fitness.plan.render',{'query':'workout plan','part_index':1})
        with patch.object(fitness,'plan_command_reply',return_value='A whole unchanged routine.'):
            result=work_actions.execute_action('fitness.plan.render',{'query':'workout plan','expected_render_hash':'0'*64},OWNER)
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['evidence']['code'],'stale_fitness_render')

class FitnessBudgetIsolationTests(unittest.TestCase):
    def test_blocked_budget_reports_no_model_and_sends_no_alert(self):
        from davosbot import billing
        day=quotes._day(datetime.now(timezone.utc))
        summary=billing.GeminiUsageSummary(estimated_cost_usd=10)
        with patch.object(morning_quotes,'_fetch_zenquotes_quote',side_effect=RuntimeError('offline')), \
             patch.object(tools,'GEMINI_API_KEY','synthetic-key'), \
             patch.object(billing,'GEMINI_ENABLED',True), patch.object(billing,'GEMINI_DAILY_BUDGET_USD',1), \
             patch.object(billing,'get_gemini_usage_summary',return_value=summary), \
             patch.object(billing,'_maybe_send_gemini_budget_alert',side_effect=AssertionError('alert')) as alert, \
             patch('requests.post',side_effect=AssertionError('HTTP')) as post:
            selected,calls=quotes._select(day,set())
        self.assertIn(selected['text'],morning_quotes._FALLBACK_QUOTES)
        self.assertFalse(calls['model_called']); alert.assert_not_called(); post.assert_not_called()

    def test_allowed_warning_budget_keeps_usage_accounting_without_alert(self):
        from davosbot import billing
        response=Mock();response.json.return_value={'candidates':[{'content':{'parts':[{'text':'One useful next step.'}]}}],
                                                    'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':5,'totalTokenCount':15}}
        before=Mock()
        with patch.object(billing,'GEMINI_ENABLED',True), patch.object(billing,'GEMINI_DAILY_BUDGET_USD',2), \
             patch.object(billing,'GEMINI_DAILY_ALERT_USD',0.25), \
             patch.object(billing,'get_gemini_usage_summary',return_value=billing.GeminiUsageSummary(estimated_cost_usd=0.5)), \
             patch.object(billing,'_maybe_send_gemini_budget_alert',side_effect=AssertionError('alert')) as alert, \
             patch.object(billing,'log_gemini_usage') as usage, patch('requests.post',return_value=response) as post:
            result=quotes._rewrite_line('generic prompt',api_key='synthetic-key',url=tools._GEMINI_URL,before_request=before)
        self.assertEqual(result,'One useful next step.');before.assert_called_once();post.assert_called_once();alert.assert_not_called()
        usage.assert_called_once_with(10,5,15,'fitness_quote')

    def test_existing_default_budget_alert_behavior_is_unchanged(self):
        from davosbot import billing
        with patch.object(billing,'GEMINI_ENABLED',True), patch.object(billing,'GEMINI_DAILY_BUDGET_USD',1), \
             patch.object(billing,'get_gemini_usage_summary',return_value=billing.GeminiUsageSummary(estimated_cost_usd=2)), \
             patch.object(billing,'_maybe_send_gemini_budget_alert') as alert:
            result=billing.check_gemini_budget('tool_rewrite')
        self.assertFalse(result.allowed);alert.assert_called_once()
