"""Incident multi-turn routes, owner and approved participant; sends/providers mocked."""
from datetime import datetime,timezone
from unittest.mock import patch
import unittest
from davosbot import main,tools,time_queries
import test_no_web_tool_permissions as routes
OWNER,FRIEND,GROUP=routes.OWNER,routes.FRIEND,routes.GROUP
from test_live_sports_answers import FINAL_PACERS,_Response

NOW=datetime(2026,10,22,4,47,tzinfo=timezone.utc)
class GroundedShortFollowupTests(unittest.TestCase):
    setUp=routes.NoWebRouteTests.setUp
    route=routes.NoWebRouteTests.route
    def test_exact_sequence_for_owner_and_existing_approved_participant(self):
        real_clock=time_queries.current_time_reply
        real_sports=tools.live_sports_native_reply
        for sender in (OWNER,FRIEND):
            history=[{'role':'assistant','content':'Pacers 72-84 Q3; it is 6:55PMPT.'}]
            def save(chat,role,body):history.append({'role':role,'content':body})
            with patch.object(main,'get_history',side_effect=lambda chat,n:history[-n:]),patch.object(main,'save_turn',side_effect=save),patch.object(main,'current_time_reply',side_effect=lambda q:real_clock(q,now=NOW)),patch.object(main,'live_sports_native_reply',side_effect=lambda q:real_sports(q,now_utc=NOW)),patch.object(tools.requests,'get',return_value=_Response([FINAL_PACERS])) as get:
                for query,expected in [('what time is it','9:47 PM PDT'),('that is incorrect, try again','9:47 PM PDT'),('pacers game','Final')]:
                    with self.subTest(sender=sender,query=query):
                        self.route(query,sender=sender,group=True)
                        self.model.assert_not_called();self.send.assert_called_once();reply=self.send.call_args.args[1]
                        self.assertIn(expected,reply);self.assertNotIn('Q3',reply);self.assertNotIn('72-84',reply)
                self.assertIn('123',reply);self.assertGreater(get.call_count,0)
    def test_correction_never_reopens_a_prior_no_search_sports_request(self):
        for sender in (OWNER,FRIEND):
            history=[]
            with patch.object(main,'get_history',side_effect=lambda chat,n:history[-n:]),patch.object(main,'save_turn',side_effect=lambda chat,role,body:history.append({'role':role,'content':body})),patch.object(main,'live_sports_native_reply',side_effect=tools.live_sports_native_reply),patch.object(tools.requests,'get') as get:
                self.route('no search pacers game',sender=sender,group=True)
                self.model.assert_not_called();get.assert_not_called()
                self.route('try again',sender=sender,group=True)
                self.model.assert_not_called();get.assert_not_called();self.send.assert_called_once()
                self.assertIn('Which team',self.send.call_args.args[1])

    def test_short_alias_shapes_and_social_false_positive(self):
        for text in ['pacers game','the Pacers game?','how about the Memphis Grizzlies game','Chicago Bulls game tonight']:
            self.assertTrue(tools.supports_live_sports_native_request(text),text)
        for text in ['game night invite','Pacers game is fun','my video game','my game']:
            self.assertFalse(tools.supports_live_sports_native_request(text),text)
    def test_correction_context_never_uses_other_member_or_assistant(self):
        with patch.object(main,'get_history',return_value=[{'role':'user','content':OWNER+': pacers game'},{'role':'assistant','content':'what time is it'}]):
            self.assertIsNone(main._grounded_followup_query(FRIEND,'try again',GROUP,is_group=True))
        with patch.object(main,'get_history',return_value=[{'role':'user','content':FRIEND+': what time is it'},{'role':'user','content':FRIEND+': tell me a joke'}]):
            self.assertIsNone(main._grounded_followup_query(FRIEND,'try again',GROUP,is_group=True))
    def test_short_sports_no_search_and_quota_still_fail_closed(self):
        for limited in (False,True):
            with patch.object(main,'get_tool_uses_today',return_value=99 if limited else 0),patch.object(main,'live_sports_native_reply') as lookup:
                self.route('pacers game' if limited else 'no search pacers game',sender=FRIEND,group=True)
                self.model.assert_not_called();lookup.assert_not_called();self.send.assert_called_once()
    def test_unapproved_group_participant_cannot_reach_lookup(self):
        with patch.object(main,'is_approved_user',return_value=False),patch.object(main,'live_sports_native_reply') as lookup:
            self.route('pacers game',sender=FRIEND,group=True)
            lookup.assert_not_called();self.model.assert_not_called()
    def test_bare_clock_variants_and_dst(self):
        for query in ['what time is it',"what's the time?",'what is the time now']:
            answer=time_queries.current_time_reply(query,now=NOW);self.assertIn('9:47 PM PDT',answer);self.assertIn('12:47 AM EDT',answer)
        winter=datetime(2026,11,2,5,47,tzinfo=timezone.utc)
        self.assertIn('PST',time_queries.current_time_reply('what time is it',now=winter))
