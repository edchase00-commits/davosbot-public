"""Bounded public standings reads. No model tools, sends, or durable state."""
from datetime import datetime, timedelta, timezone
import math
import re
import threading
from zoneinfo import ZoneInfo
import requests

URL = 'https://site.api.espn.com/apis/v2/sports/basketball/nba/standings'
PT = ZoneInfo('America/Los_Angeles')
_lock = threading.Lock()
_context = {}
TTL = timedelta(minutes=5)
FOLLOWUP = re.compile(r"(?:so+|try again|check again|use (?:the )?espn api(?:\..*)?|pull (?:the )?real data)[.!? ]*", re.I)


def recognized(text):
    return bool(re.search(r'\bstandings?\b', text or '', re.I) and
                re.search(r'\b(?:nba|basketball)\b', text or '', re.I))


def _latest(sender, history, group):
    for turn in reversed(list(history)[-8:]):
        if turn.get('role') != 'user':
            continue
        text = str(turn.get('content', ''))
        if group:
            if not text.startswith(sender + ': '):
                continue
            text = text[len(sender) + 2:]
        return text


def query(text, *, sender, recipient, history=(), is_group=False, now=None):
    if recognized(text):
        return text
    if not FOLLOWUP.fullmatch((text or '').strip()):
        return None
    now = now or datetime.now(timezone.utc)
    with _lock:
        previous = _context.get((sender, recipient))
    if previous is None:
        return None
    request, last_request, stamp = previous
    if not timedelta(0) <= now - stamp <= TTL or _latest(sender, history, is_group) != last_request:
        return None
    return request


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value != int(value):
        raise ValueError('standings_number')
    return int(value)


def _format(data, now):
    season = data.get('season')
    if not isinstance(season, dict):
        raise ValueError('season_missing')
    start = datetime.fromisoformat(season['startDate'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(season['endDate'].replace('Z', '+00:00'))
    if not start <= now <= end:
        raise ValueError('season_stale')
    label = season.get('displayName', '')
    if not re.fullmatch(r'20\d{2}-\d{2}', label):
        raise ValueError('season_label')
    year = int(label[:4])
    if start.year != year or end.year != year + 1 or label[-2:] != str(year + 1)[-2:] or not timedelta(0) < end - start <= timedelta(days=366):
        raise ValueError('season_interval')
    children = data.get('children')
    if not isinstance(children, list) or len(children) != 2:
        raise ValueError('conferences')
    groups = {}; identities = set()
    for group in children:
        name = group.get('name')
        if name not in {'Eastern Conference', 'Western Conference'} or name in groups:
            raise ValueError('conference_identity')
        entries = group.get('standings', {}).get('entries')
        if not isinstance(entries, list) or len(entries) != 15:
            raise ValueError('team_count')
        rows = []
        for entry in entries:
            team = entry['team']; identity = str(team['id']); abbreviation = team['abbreviation']
            if not identity.isdigit() or identity in identities or not re.fullmatch(r'[A-Z]{2,4}', abbreviation):
                raise ValueError('team_identity')
            identities.add(identity)
            stats = entry['stats']
            if not isinstance(stats, list) or len({x['name'] for x in stats}) != len(stats):
                raise ValueError('stats_shape')
            stats = {x['name']: x.get('value') for x in stats}
            wins, losses = _integer(stats['wins']), _integer(stats['losses'])
            rows.append((-(wins / (wins + losses) if wins + losses else 0), -wins, abbreviation, wins, losses))
        groups[name] = sorted(rows)
    lines = [f'NBA standings — {label}. Records sorted by win percentage; tied records are not official tiebreak ranks.']
    for name in ('Eastern Conference', 'Western Conference'):
        lines.append(name + ': ' + '; '.join(f'{r[2]} {r[3]}–{r[4]}' for r in groups[name]))
    lines.append('ESPN checked ' + now.astimezone(PT).strftime('%b %d, %I:%M %p %Z') + '. https://www.espn.com/nba/standings')
    return '\n'.join(lines)


def reply(text, *, sender, recipient, now=None, allow_lookup=True, get=None, on_lookup=None, last_request=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return 'I could not verify the current clock for NBA standings.'
    now = now.astimezone(timezone.utc)
    from .tools import _has_unsupported_live_sports_date, _validate_live_sports_response_freshness
    if _has_unsupported_live_sports_date(text):
        return 'This lookup supports current NBA standings only; I have not verified historical standings.'
    if not allow_lookup:
        with _lock:
            _context.pop((sender, recipient), None)
        return 'A fresh ESPN check is needed for NBA standings. You asked me not to search, so I have not verified them.'
    with _lock:
        for key, value in list(_context.items()):
            if now - value[2] > TTL:
                _context.pop(key, None)
        if len(_context) >= 256:
            _context.pop(next(iter(_context)))
        _context[(sender, recipient)] = (text, last_request or text, now)
    try:
        if on_lookup:
            on_lookup()
        response = (get or requests.get)(URL, timeout=5)
        try:
            response.raise_for_status()
            _validate_live_sports_response_freshness(response, now)
            return _format(response.json(), now)
        finally:
            response.close()
    except Exception:
        return 'ESPN NBA standings lookup failed right now. I do not have verified standings, so I will not guess.'
