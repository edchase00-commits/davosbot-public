import copy
from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch,Mock
from davosbot import player_boxscores as box,tools,main
import test_no_web_tool_permissions as routes
from test_live_sports_answers import FINAL_PACERS

NOW=datetime(2026,10,22,5,11,tzinfo=timezone.utc)
OWNER,FRIEND,GROUP=routes.OWNER,routes.FRIEND,routes.GROUP
class Frozen(datetime):
    @classmethod
    def now(cls,tz=None):return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

def fixture():
    event=copy.deepcopy(FINAL_PACERS);event['id']='401909839'
    comp=event['competitions'][0]
    for c,identity in zip(comp['competitors'],['11','3']):c['team']['id']=identity
    hc=copy.deepcopy(comp);hc.update(id=event['id'],date=event['date'],status=event['status'])
    data={'header':{'id':event['id'],'competitions':[hc]},'boxscore':{'players':[{
        'team':{'id':'11'},'statistics':[{'labels':['PTS','AST'],'keys':['points','assists'],
        'athletes':[{'athlete':{'id':'4396993','displayName':'Tyrese Haliburton'},'didNotPlay':False,'stats':['23','12']}]}]}]}}
    return event,data
class Response:
    def __init__(self,data,headers=None):self.data=data;self.headers=headers or {}
    def json(self):return self.data
    def raise_for_status(self):pass
    def close(self):pass

class PlayerBoxscoreTests(unittest.TestCase):
    def setUp(self):
        box._context.clear();box._games.clear();self.event,self.data=fixture()
        self.history=[{'role':'user','content':OWNER+': pacers score today'}]
        box.record_game_query(OWNER,GROUP,'pacers score today','Final — NBA: Pacers. ESPN checked 10:11 PM PDT.',now=NOW)
        self.fetch=patch.object(tools,'_fetch_live_sports_events',return_value=([('NBA',self.event,1,[0])],1,1));self.fetcher=self.fetch.start();self.addCleanup(self.fetch.stop)
        self.get=Mock(side_effect=lambda *a,**k:Response(self.data))
    def ask(self,text,**kw):
        return box.reply(text,sender=kw.pop('sender',OWNER),recipient=kw.pop('recipient',GROUP),history=kw.pop('history',self.history),now=kw.pop('now',NOW),get=self.get,**kw)
    def test_points_then_same_actor_pronoun_assists_are_fresh_reads(self):
        q='how many points did Haliburton have?';self.assertIn('23 points',self.ask(q))
        self.history.append({'role':'user','content':OWNER+': '+q})
        self.assertIn('12 assists',self.ask('how many assists did he have!'))
        self.assertEqual(self.get.call_count,2);self.assertEqual(self.get.call_args.kwargs['params'],{'event':'401909839'})
    def test_pronoun_with_explicit_team_never_silently_reuses_old_event(self):
        q='how many points did Haliburton have?';self.ask(q);self.history.append({'role':'user','content':OWNER+': '+q})
        self.get.reset_mock();self.fetcher.reset_mock()
        self.assertIn('full player name',self.ask('how many assists did he have for Lakers?'))
        self.get.assert_not_called();self.fetcher.assert_not_called()
    def test_unresolved_explicit_team_never_falls_back_to_saved_game(self):
        q='how many points did Haliburton have?';self.ask(q);self.history.append({'role':'user','content':OWNER+': '+q})
        for team in ['Seahawks','Unknown Team']:
            self.get.reset_mock();self.fetcher.reset_mock()
            self.assertIn('one NBA team',self.ask('how many assists did Haliburton have for '+team+'?'))
            self.get.assert_not_called();self.fetcher.assert_not_called()
    def test_stat_labels_not_positions(self):
        table=self.data['boxscore']['players'][0]['statistics'][0];table['labels'].reverse();table['keys'].reverse();table['athletes'][0]['stats'].reverse()
        self.assertIn('23 points',self.ask('how many points did Haliburton have?'))
    def test_cross_actor_chat_or_raw_model_history_cannot_supply_context(self):
        for kwargs in [{'sender':FRIEND},{'recipient':'other-chat'},{'history':[{'role':'assistant','content':'Haliburton23'}]},{'history':[{'role':'user','content':OWNER+': other topic'}]}]:
            self.assertIn('Which NBA',self.ask('how many points did Haliburton have?',**kwargs))
        self.get.assert_not_called()
    def test_pronoun_cannot_cross_actor_chat_time_day_or_new_topic(self):
        q='how many points did Haliburton have?';self.ask(q);self.history.append({'role':'user','content':OWNER+': '+q})
        for kwargs in [{'sender':FRIEND},{'recipient':'other-chat'},{'now':NOW+timedelta(minutes=6)},{'now':NOW+timedelta(days=1)},{'history':[{'role':'user','content':OWNER+': tell me a joke'}]}]:
            self.assertIn('Which NBA player',self.ask('how many assists did he have!',**kwargs))
    def test_dnp_and_missing_values_are_not_zero(self):
        row=self.data['boxscore']['players'][0]['statistics'][0]['athletes'][0];row['didNotPlay']=True;row['stats']=[]
        self.assertIn('did not play',self.ask('how many points did Haliburton have?'))
        row['didNotPlay']=False
        self.assertIn('could not verify',self.ask('how many points did Haliburton have?'))
    def test_missing_labels_duplicate_labels_and_unknown_values_fail_closed(self):
        for change in ('missing','duplicate','unknown','mismatched_key'):
            event,self.data=fixture();t=self.data['boxscore']['players'][0]['statistics'][0]
            if change=='missing':t['labels']=[]
            elif change=='duplicate':t['labels']=['PTS','PTS']
            elif change=='unknown':t['athletes'][0]['stats'][0]='--'
            else:t['keys'].reverse()
            self.assertIn('could not verify',self.ask('how many points did Haliburton have?'))
    def test_duplicate_surname_needs_full_name(self):
        athletes=self.data['boxscore']['players'][0]['statistics'][0]['athletes'];other=copy.deepcopy(athletes[0]);other['athlete']={'id':'123','displayName':'Other Haliburton'};athletes.append(other)
        self.assertIn('full player name',self.ask('how many points did Haliburton have?'))
        self.assertIn('23 points',self.ask('how many points did Tyrese Haliburton have?'))
    def test_event_date_team_and_summary_state_must_match(self):
        for change in ('event','date','team','phase'):
            _,self.data=fixture();hc=self.data['header']['competitions'][0]
            if change=='event':self.data['header']['id']='123'
            elif change=='date':hc['date']='2026-10-21T00:00Z'
            elif change=='team':hc['competitors'][0]['team']['id']='999'
            else:hc['status']={'type':{'state':'pre'}}
            self.assertIn('could not verify',self.ask('how many points did Haliburton have?'))
    def test_stale_summary_partial_scoreboard_and_no_search_never_guess(self):
        self.get.side_effect=lambda *a,**k:Response(self.data,{'Age':'900'})
        self.assertIn('could not verify',self.ask('how many points did Haliburton have?'))
        self.fetcher.return_value=([('NBA',self.event,1,[0])],1,2)
        self.assertIn('could not verify',self.ask('how many points did Haliburton have?'))
        self.get.reset_mock();self.assertIn('forbids searching',self.ask('how many points did Haliburton have?',allow_lookup=False));self.get.assert_not_called()
    def test_multiple_games_and_changed_followup_identity_are_held(self):
        other=copy.deepcopy(self.event);other['id']='555'
        self.fetcher.return_value=([('NBA',self.event,1,[0]),('NBA',other,1,[0])],1,1)
        self.assertIn('exactly one',self.ask('how many points did Haliburton have?'))
        self.fetcher.return_value=([('NBA',self.event,1,[0])],1,1)
        q='how many points did Haliburton have?';self.ask(q);self.history.append({'role':'user','content':OWNER+': '+q})
        self.data['boxscore']['players'][0]['statistics'][0]['athletes'][0]['athlete']['id']='888'
        self.assertIn('could not verify',self.ask('how many assists did he have!'))
    def test_expired_game_context_and_untyped_participation_are_held(self):
        self.assertIn('Which NBA team',self.ask('how many points did Haliburton have?',now=NOW+timedelta(minutes=6)))
        self.get.assert_not_called()
        self.data['boxscore']['players'][0]['statistics'][0]['athletes'][0]['didNotPlay']='false'
        self.assertIn('could not verify',self.ask('how many points did Haliburton have?'))
    def test_explicit_team_and_unsupported_dates(self):
        self.assertIn('23 points',self.ask('how many points did Haliburton have for Pacers?',history=[]))
        for scope in ['yesterday','last week','in 2024','tomorrow','on October 21']:
            self.get.reset_mock();self.assertIn('date or period',self.ask('how many points did Haliburton have '+scope+'?'));self.get.assert_not_called()

class PlayerBoxscoreRoutingTests(unittest.TestCase):
    setUp=routes.NoWebRouteTests.setUp
    route=routes.NoWebRouteTests.route
    def test_exact_owner_and_approved_participant_sequence_ignores_model_stats(self):
        event,data=fixture()
        for sender in (OWNER,FRIEND):
            box._context.clear();box._games.clear();history=[{'role':'assistant','content':'Haliburton had22points and12assists'}]
            with patch.object(box,'datetime',Frozen),patch.object(main,'get_history',side_effect=lambda chat,n:history[-n:]),patch.object(main,'save_turn',side_effect=lambda chat,role,body:history.append({'role':role,'content':body})),patch.object(main,'live_sports_native_reply',side_effect=tools.live_sports_native_reply),patch.object(tools,'_fetch_live_sports_events',return_value=([('NBA',event,1,[0])],1,1)),patch.object(box.requests,'get',return_value=Response(data)):
                for query,expected in [('what was the pacers score?','Final'),('how many points did Haliburton have?','23 points'),('how many assists did he have!','12 assists')]:
                    self.route(query,sender=sender,group=True);self.model.assert_not_called();self.send.assert_called_once();self.assertIn(expected,self.send.call_args.args[1])
    def test_quota_and_unapproved_participant_never_fetch(self):
        for approved,quota in [(False,0),(True,99)]:
            with patch.object(main,'is_approved_user',return_value=approved),patch.object(main,'get_tool_uses_today',return_value=quota),patch.object(tools,'_fetch_live_sports_events') as fetch:
                self.route('how many points did Haliburton have?',sender=FRIEND,group=True);fetch.assert_not_called();self.model.assert_not_called()
