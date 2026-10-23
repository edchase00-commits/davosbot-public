import copy
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch
from davosbot import nba_standings as n,main
import test_no_web_tool_permissions as routes
NOW=datetime(2026,10,22,16,45,tzinfo=timezone.utc)
QUERY='can we get an nba standings update? Remember to pull the real data'
def payload():
 return {'season':{'startDate':'2026-09-30T07:00Z','endDate':'2027-06-26T06:59Z','displayName':'2026-27'},'children':[{'name':name,'standings':{'entries':[{'team':{'id':str(offset+i),'abbreviation':chr(65+i)+'BC'},'stats':[{'name':'wins','value':i%3},{'name':'losses','value':1}]} for i in range(15)]}} for name,offset in [('Eastern Conference',1),('Western Conference',16)]]}
def response(data=None,headers=None):
 r=Mock();r.json.return_value=data or payload();r.headers=headers or {};return r
class StandingsTests(unittest.TestCase):
 def setUp(self):n._context.clear()
 def test_fresh(self):
  r=response();out=n.reply(QUERY,sender='one',recipient='chat',now=NOW,get=Mock(return_value=r))
  for part in ['Eastern Conference','Western Conference','not official tiebreak ranks','09:45 AM PDT']:self.assertIn(part,out)
  r.close.assert_called_once()
 def test_fail_closed(self):
  cases=[]
  for mutation in ['season','label','interval','count','duplicate','stat']:
   d=copy.deepcopy(payload())
   if mutation=='season':d['season']['endDate']='2026-01-01T00:00Z'
   if mutation=='label':d['season']['displayName']='2025-26'
   if mutation=='interval':d['season']['startDate']='2025-09-30T07:00Z'
   if mutation=='count':d['children'][0]['standings']['entries'].pop()
   if mutation=='duplicate':d['children'][1]['standings']['entries'][0]['team']['id']='1'
   if mutation=='stat':d['children'][0]['standings']['entries'][0]['stats'][0]['value']='bad'
   cases.append(response(d))
  cases.append(response(headers={'Age':'99999'}))
  for r in cases:
   self.assertIn('will not guess',n.reply(QUERY,sender='one',recipient='chat',now=NOW,get=Mock(return_value=r)));r.close.assert_called_once()
 def test_followups(self):
  n.reply(QUERY,sender='one',recipient='chat',now=NOW,get=Mock(return_value=response()))
  h=[{'role':'user','content':'one: '+QUERY}];o=dict(sender='one',recipient='chat',history=h,is_group=True,now=NOW)
  self.assertEqual(QUERY,n.query('sooooo',**o))
  for change in [dict(sender='two'),dict(recipient='other'),dict(now=NOW+timedelta(minutes=6)),dict(history=h+[{'role':'user','content':'one: unrelated'}])]:self.assertIsNone(n.query('sooooo',**dict(o,**change)))
  n.reply(QUERY,sender='one',recipient='chat',last_request='sooooo',now=NOW,get=Mock(return_value=response()))
  self.assertEqual(QUERY,n.query('use the espn api. Use the GitHub reference for what parameters to fill.',**dict(o,history=[{'role':'user','content':'one: sooooo'}])))
 def test_no_search_history_unrelated(self):
  n.reply(QUERY,sender='one',recipient='chat',now=NOW,get=Mock(return_value=response()))
  get=Mock();self.assertIn('not verified',n.reply(QUERY,sender='one',recipient='chat',now=NOW,allow_lookup=False,get=get))
  self.assertIn('historical',n.reply('nba standings last year',sender='one',recipient='chat',now=NOW,get=get));get.assert_not_called()
  self.assertIsNone(n.query('try again',sender='one',recipient='chat',history=[{'role':'user','content':QUERY}],now=NOW))
  self.assertFalse(n.recognized('NFL standings'));self.assertIsNone(n.query('sooooo',sender='new',recipient='chat'))
class StandingsRouteTests(routes.NoWebRouteTests):
 def test_route(self):
  for sender in (routes.OWNER,routes.FRIEND):
   for group in ((False,True) if sender==routes.OWNER else (True,)):
    with self.subTest(sender=sender,group=group),patch.object(n,'reply',return_value='fixture ESPN standings') as lookup:
     self.route(QUERY,sender=sender,group=group);lookup.assert_called_once();self.model.assert_not_called();self.send.assert_called_once()
 def test_gates(self):
  with patch.object(main,'get_tool_uses_today',return_value=100),patch.object(n,'reply') as lookup:
   self.route(QUERY,sender=routes.FRIEND,group=True);lookup.assert_not_called();self.assertIn('limit',self.send.call_args.args[1])
  with patch.object(n,'requests') as request:
   self.route('no search '+QUERY,group=True);request.get.assert_not_called();self.assertIn('not verified',self.send.call_args.args[1])
 def test_same_actor_group_followup_fetches_again(self):
  n._context.clear()
  with patch.object(n.requests,'get',side_effect=lambda *a,**k: response()) as get:
   self.route(QUERY,sender=routes.FRIEND,group=True)
   h=[{'role':'user','content':routes.FRIEND+': '+QUERY}]
   with patch.object(main,'get_history',return_value=h):self.route('sooooo',sender=routes.FRIEND,group=True)
   self.assertEqual(2,get.call_count);self.model.assert_not_called()
   h=[{'role':'user','content':routes.FRIEND+': sooooo'}]
   with patch.object(main,'get_history',return_value=h):self.route('use the espn api. Use the GitHub reference for what parameters to fill.',sender=routes.FRIEND,group=True)
   self.assertEqual(3,get.call_count);self.model.assert_not_called()

 def test_no_search_clears_context_even_at_quota(self):
  n.reply(QUERY,sender=routes.FRIEND,recipient=routes.GROUP,get=Mock(return_value=response()))
  with patch.object(main,'get_tool_uses_today',return_value=100):
   self.route('no search '+QUERY,sender=routes.FRIEND,group=True)
  self.assertNotIn((routes.FRIEND,routes.GROUP),n._context)
