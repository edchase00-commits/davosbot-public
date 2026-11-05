"""Diagnostic metadata is bounded and contains no provider content; no live calls."""
import json
import unittest
from unittest.mock import Mock, patch
from davosbot import research_reports as reports, research_cron as cron
from test_research_cron import REQUEST, GROUP, NOW, _sources, _waivers

class WaiverDiagnosticTests(unittest.TestCase):
    def setUp(self):
        _,self.payload=cron.parse_creation(REQUEST,GROUP,NOW)
    def failure(self,data):
        with self.assertRaises(reports.ReportFailure) as raised:
            reports.render_report(self.payload,_sources(),json.dumps(data),NOW)
        return raised.exception
    def test_missing_nonlist_and_wrong_count_are_distinct(self):
        for data,reason,count in [({},'missing_players',None),({'players':None},'players_not_list',None),
                                  ({'players':'private provider text'},'players_not_list',None),
                                  ({'players':[]},'wrong_target_count',0),
                                  ({'players':[{}]*1000},'wrong_target_count',100)]:
            with self.subTest(reason=reason):
                error=self.failure(data)
                self.assertEqual(str(error),'insufficient_waiver_targets')
                self.assertEqual(error.diagnostic['reason'],reason)
                self.assertEqual(error.diagnostic['expected_count'],10)
                self.assertEqual(error.diagnostic['source_count'],1)
                self.assertEqual(error.diagnostic.get('actual_count'),count)
                self.assertNotIn('private provider text',json.dumps(error.diagnostic))
    def test_unsupported_ungrounded_duplicate_and_bad_citation_are_distinct(self):
        cases=[]
        cases.append(({'error':'secret provider explanation'},'unsupported_targets'))
        for field,value,reason in [('name','Private Unsupported Player','ungrounded_target'),
                                    ('name','Player 1','duplicate_target'),('source_ids',[999],'invalid_target_citations')]:
            data=json.loads(_waivers());data['players'][2][field]=value;cases.append((data,reason))
        for data,reason in cases:
            with self.subTest(reason=reason):
                error=self.failure(data);self.assertEqual(error.diagnostic['reason'],reason)
                encoded=json.dumps(error.diagnostic)
                for sensitive in ['secret provider explanation','Private Unsupported Player','https://',GROUP]:self.assertNotIn(sensitive,encoded)
    def test_valid_ten_grounded_targets_still_render(self):
        self.assertIn('NFL fantasy waiver top 10',reports.render_report(self.payload,_sources(),_waivers(),NOW))
    def test_failure_worker_logs_only_counts_and_fixed_reason_without_report_send(self):
        payload=dict(self.payload);payload['run']={'id':'fixture','date':NOW.date().isoformat()}
        error=self.failure({'players':[]});send=Mock(return_value=True)
        with patch.object(reports,'_current',return_value=('00:59 wed',payload)),patch.object(reports,'_finish',return_value=True),patch.object(reports,'fetch_sources',return_value=_sources()),patch.object(reports,'synthesize',side_effect=error),patch.object(reports,'_slots'),self.assertLogs(reports.logger,level='WARNING') as logs:
            reports._work('fixture.sqlite',15,'fixture','00:59 wed',payload,NOW,send)
        self.assertEqual(send.call_count,1) # Existing failure notice only, no retry/report.
        self.assertNotIn('NFL fantasy waiver top 10',send.call_args.args[1])
        diagnostic=[line for line in logs.output if 'validation diagnostic' in line]
        self.assertEqual(len(diagnostic),1);self.assertIn('wrong_target_count',diagnostic[0]);self.assertIn('"actual_count": 0',diagnostic[0])
        self.assertNotIn(GROUP,' '.join(logs.output))
