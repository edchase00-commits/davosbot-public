"""Bounded public NBA boxscore reads; no model tools, sender or durable state."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import threading
from zoneinfo import ZoneInfo
import requests

PT = ZoneInfo('America/Los_Angeles')
STATS = {'points':('PTS','points'),'assists':('AST','assists'),'rebounds':('REB','rebounds'),
         'steals':('STL','steals'),'blocks':('BLK','blocks'),'turnovers':('TO','turnovers')}
QUERY = re.compile(r"^how many (?P<stat>points?|assists?|rebounds?|steals?|blocks?|turnovers?) did (?P<player>[a-z .’'-]{1,80}?) (?:have|score|get)(?P<scope>.*)$",re.I)
_lock=threading.Lock()
_context={}
_games={}
TTL=timedelta(minutes=5)

@dataclass(frozen=True)
class Identity:
    event_id:str
    athlete_id:str
    player:str
    day:str
    query:str
    checked_at:datetime
    last_request:str


def parse(text):
    match=QUERY.fullmatch((text or '').strip().rstrip('!? .'))
    if not match:return None
    stat=match['stat'].lower()
    if not stat.endswith('s'):stat+='s'
    return stat,match['player'].strip(),match['scope'].strip()


def _norm(value):return re.sub(r'[^a-z0-9]+',' ',str(value).lower()).strip()


def _latest_user(sender,history,is_group):
    for turn in reversed(list(history)[-8:]):
        if turn.get('role')!='user':continue
        text=str(turn.get('content',''))
        if is_group:
            if not text.startswith(sender+': '):continue
            text=text[len(sender)+2:]
        return text
    return None


def record_game_query(sender,recipient,text,verified_reply,now=None):
    """Only called with a newly grounded native response, never model history."""
    from .tools import _live_sports_league_rows
    rows=_live_sports_league_rows(text,fallback=False)
    if len(rows)!=1 or rows[0][0]!='NBA' or 'ESPN checked ' not in verified_reply:
        return
    if not (verified_reply.startswith(('Live — NBA:','Final — NBA:')) or
            (verified_reply.startswith(('Yes —','No —')) and '. Final.' in verified_reply)):
        return
    now=now or datetime.now(timezone.utc)
    with _lock:
        for key,value in list(_games.items()):
            if now-value[1]>TTL:_games.pop(key,None)
        if len(_games)>=256:_games.pop(next(iter(_games)))
        _games[(sender,recipient)]=(text,now)


def _previous_game(sender,recipient,history,is_group,now):
    with _lock:game=_games.get((sender,recipient))
    if game is None:return None
    query,stamp=game
    if (not timedelta(0)<=now-stamp<=TTL or stamp.astimezone(PT).date()!=now.astimezone(PT).date()
            or _norm(_latest_user(sender,history,is_group))!=_norm(query)):
        return None
    return query


def _remember(key,identity):
    with _lock:
        for k,v in list(_context.items()):
            if identity.checked_at-v.checked_at>TTL:_context.pop(k,None)
        if len(_context)>=256:_context.pop(next(iter(_context)))
        _context[key]=identity


def _saved(key,now):
    with _lock:
        value=_context.get(key)
        if value is None:return None
        if not timedelta(0)<=now-value.checked_at<=TTL or value.day!=now.astimezone(PT).date().isoformat():
            _context.pop(key,None);return None
        return value


def _get_summary(event_id,now,get):
    from .tools import _validate_live_sports_response_freshness
    if not re.fullmatch(r'[0-9]{1,15}',event_id):raise ValueError('event_identity')
    response=get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary',params={'event':event_id},timeout=5)
    try:
        response.raise_for_status();_validate_live_sports_response_freshness(response,now)
        data=response.json()
        if not isinstance(data,dict) or str((data.get('header') or {}).get('id'))!=event_id:raise ValueError('summary_identity')
        return data
    finally:response.close()


def reply(text,*,sender,recipient,history=(),is_group=True,now=None,allow_lookup=True,get=None,on_lookup=None):
    parsed=parse(text)
    if parsed is None:return None
    stat,requested,scope=parsed
    from .tools import (_has_unsupported_live_sports_date,_fetch_live_sports_events,
                        _event_start_datetime,_live_sports_resolved_phase)
    explicit_team=re.fullmatch(r'for (?:the )?([a-z .-]{1,60})',scope,re.I)
    if _has_unsupported_live_sports_date(text) or (scope not in ('','today','tonight') and explicit_team is None):
        return 'Please name the NBA player and current game; I have not verified a boxscore for that date or period.'
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:return 'I could not verify the current clock for that boxscore.'
    now=now.astimezone(timezone.utc);day=now.astimezone(PT).date().isoformat();key=(sender,recipient)
    pronoun=_norm(requested) in {'he','him','his','they','them'}
    saved=_saved(key,now)
    if saved is not None and _norm(_latest_user(sender,history,is_group))!=_norm(saved.last_request):
        saved=None
    if pronoun:
        if explicit_team is not None:
            return 'Please give the full player name for that explicit team/game; I will not carry a pronoun into a possibly different event.'
        if saved is None:return 'Which NBA player and game do you mean? I do not have a recent verified player for your follow-up.'
        requested=saved.player;query=saved.query
    else:
        query=_previous_game(sender,recipient,history,is_group,now)
        if explicit_team is not None:
            from .tools import _live_sports_league_rows
            rows=_live_sports_league_rows(explicit_team[1],fallback=False)
            if len(rows)==1 and rows[0][0]=='NBA':query=explicit_team[1]+' score today'
            else:return 'Please name one NBA team/game explicitly; I have not substituted the previous game.'
        # A verified prior player read can retain the same game for this actor.
        if query is None and saved is not None and _norm(requested) in {_norm(saved.player),_norm(saved.player).split()[-1]}:query=saved.query
        if query is None:return f'Which NBA team/game do you mean for {requested}? Please name it so I can check the right boxscore.'
    if not allow_lookup:return 'A fresh ESPN boxscore check is needed. I have not looked it up because this request forbids searching.'
    try:
        if on_lookup is not None:on_lookup()
        matches,ok,total=_fetch_live_sports_events(query,league_hint='NBA',now_utc=now)
        if not total or ok!=total:raise ValueError('incomplete_scoreboard')
        events={}
        for league,event,phase,ranks in matches:
            comps=event.get('competitions') or []
            when=_event_start_datetime(event,comps[0] if comps else {})
            if league=='NBA' and when is not None and when.astimezone(PT).date().isoformat()==day:
                events[str(event['id'])]=event
        if len(events)!=1:return 'Which NBA game do you mean? I could not identify exactly one current-day game for that player request.'
        event_id,event=next(iter(events.items()))
        if pronoun and event_id!=saved.event_id:raise ValueError('followup_event_changed')
        comp=event['competitions'][0];phase=_live_sports_resolved_phase(event,comp)
        if phase not in (0,1):return 'ESPN has no verified live or final player boxscore for that game yet.'
        data=_get_summary(event_id,now,get or requests.get)
        header=data['header'];hc=header['competitions']
        if not isinstance(hc,list) or len(hc)!=1:raise ValueError('summary_competition')
        hc=hc[0]
        if str(hc.get('id'))!=event_id or _event_start_datetime({},hc)!=_event_start_datetime(event,comp):raise ValueError('summary_event_date')
        if _live_sports_resolved_phase({},hc)!=phase:raise ValueError('summary_state')
        team_ids={str(x['team']['id']) for x in comp['competitors']}
        summary_ids={str(x['team']['id']) for x in hc['competitors']}
        if len(team_ids)!=2 or team_ids!=summary_ids:raise ValueError('summary_teams')
        found=[]
        for team in data['boxscore']['players']:
            if str(team['team']['id']) not in team_ids:raise ValueError('player_team')
            for table in team['statistics']:
                for athlete in table['athletes']:
                    person=athlete['athlete'];name=person['displayName'];identity=str(person['id'])
                    if not re.fullmatch(r'[0-9]{1,15}',identity):raise ValueError('player_identity')
                    tokens=_norm(name).split();asked=_norm(requested)
                    if asked==_norm(name) or asked==tokens[-1]:found.append((name,identity,athlete,table))
        if len(found)!=1:return 'I could not identify one matching NBA player in that event boxscore. Please give the full player name.'
        name,athlete_id,row,table=found[0]
        if pronoun and athlete_id!=saved.athlete_id:raise ValueError('followup_player_changed')
        if type(row.get('didNotPlay')) is not bool:raise ValueError('participation_unverified')
        if row.get('didNotPlay') is True:
            return f'{name} is marked did not play in that ESPN boxscore; I will not invent a {stat} total.'
        labels,keys,values=table['labels'],table['keys'],row['stats'];label,stat_key=STATS[stat]
        if not all(isinstance(v,list) for v in (labels,keys,values)) or len(labels)!=len(keys) or len(labels)!=len(values):raise ValueError('stat_columns')
        if labels.count(label)!=1 or keys.count(stat_key)!=1 or labels.index(label)!=keys.index(stat_key):raise ValueError('stat_label')
        value=values[labels.index(label)]
        if not isinstance(value,str) or not re.fullmatch(r'\d{1,3}',value):raise ValueError('stat_unavailable')
        _remember(key,Identity(event_id,athlete_id,name,day,query,now,text))
        state='Final' if phase==1 else 'Live'
        return f"{state} — {name}: {int(value)} {stat}. ESPN checked {now.astimezone(PT).strftime('%-I:%M %p %Z')}. https://www.espn.com/nba/boxscore/_/gameId/{event_id}"
    except Exception:
        return 'I could not verify a fresh ESPN player boxscore for that exact game. I will not guess or reuse an earlier answer.'
