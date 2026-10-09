"""Fail-closed owner schedule claims. No claim is ever automatically released."""
from contextlib import closing
from datetime import date, datetime, timedelta
import hashlib
import json
import os
import re
import sqlite3
from zoneinfo import ZoneInfo

_FIELDS = {'schedule_id', 'seattle_date', 'quote_date', 'render_hash',
           'render_part_hashes', 'part_index', 'part_count', 'part_hashes',
           'effective_part_hashes', 'message', 'message_hash'}
_HEX = re.compile(r'[a-f0-9]{64}\Z')


def _now():
    return datetime.now(ZoneInfo('America/Los_Angeles'))


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _hashes(value):
    return (isinstance(value, list) and 1 <= len(value) <= 16 and
            all(isinstance(x, str) and _HEX.fullmatch(x) for x in value))


def validate(args, binding=None):
    if set(args) != _FIELDS:
        raise ValueError('invalid_fields')
    if not isinstance(args['schedule_id'], str) or not 1 <= len(args['schedule_id']) <= 100:
        raise ValueError('invalid_schedule_id')
    count = args['part_count']
    if (not _hashes(args['part_hashes']) or not _hashes(args['effective_part_hashes']) or
            not _hashes(args['render_part_hashes']) or type(count) is not int or
            count != len(args['part_hashes']) or count != len(args['effective_part_hashes'])):
        raise ValueError('invalid_manifest')
    if not isinstance(args['render_hash'], str) or not _HEX.fullmatch(args['render_hash']):
        raise ValueError('invalid_render_hash')
    if type(args['part_index']) is not int or not 1 <= args['part_index'] <= count:
        raise ValueError('invalid_part_index')
    day = args['seattle_date']
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('invalid_occurrence_date')
    if args['quote_date'] != day:
        raise ValueError('quote_date_mismatch')
    start, end, clock = binding if binding else ('0001-01-01', '9999-12-31', '00:00')
    now = _now()
    if not start <= day <= end:
        raise ValueError('occurrence_outside_schedule')
    if day < now.date().isoformat():
        raise ValueError('occurrence_expired')
    if day > now.date().isoformat() or now.strftime('%H:%M') < clock:
        raise ValueError('occurrence_not_due')
    from .text_safety import normalize_bot_text
    message = args['message']
    if (not isinstance(message, str) or not message.strip() or len(message) > 2000 or
            len(json.dumps(message, ensure_ascii=True).encode()) > 5000 or
            any(ord(c) < 32 and c not in '\n\t' or 127 <= ord(c) <= 159 for c in message)):
        raise ValueError('invalid_text')
    effective = normalize_bot_text(message)
    whitespace_only = re.sub(r' *\n *', '\n', re.sub(r'[ \t]{2,}', ' ', message)).strip()
    if effective != whitespace_only:
        raise ValueError('semantic_normalization_rejected')
    index = args['part_index'] - 1
    if args['message_hash'] != _hash(message) or args['part_hashes'][index] != _hash(message):
        raise ValueError('message_hash_mismatch')
    if args['effective_part_hashes'][index] != _hash(effective):
        raise ValueError('effective_hash_mismatch')


def _manifest(args):
    return json.dumps({k: args[k] for k in sorted(_FIELDS - {'message', 'message_hash', 'part_index'})},
                      sort_keys=True, separators=(',', ':'))


def _activation(root, owner):
    from .work_image_receipts import _owner_key
    path = _store_path(root).with_name('schedule_occurrence_activation.json')
    if path.is_symlink():
        raise ValueError('unsafe_occurrence_path')
    raw = path.read_bytes()
    if len(raw) > 4096:
        raise ValueError('invalid_occurrence_activation')
    data = json.loads(raw)
    if (set(data) != {'owner', 'bindings', 'activation_id'} or data['owner'] != _owner_key(owner) or
            not isinstance(data['activation_id'], str) or len(data['activation_id']) != 36):
        raise ValueError('occurrence_activation_mismatch')
    _validate_bindings(data['bindings'])
    return _hash(raw.decode()), data['bindings']


def _validate_bindings(bindings):
    # Narrow first activation: one existing daily owner fitness schedule only.
    if (not isinstance(bindings, dict) or len(bindings) != 1 or
            any(not isinstance(k, str) or not 1 <= len(k) <= 100 or
                not isinstance(v, list) or len(v) != 3 or
                not all(isinstance(x, str) for x in v) for k, v in bindings.items())):
        raise ValueError('invalid_occurrence_binding')
    start, end, clock = next(iter(bindings.values()))
    if (date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or
            start > end or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock)):
        raise ValueError('invalid_occurrence_binding')


def _store_path(root):
    from .work_image_receipts import _path
    path = _path(root, create=False).with_name('schedule_occurrences.sqlite3')
    if path.is_symlink():
        raise ValueError('unsafe_occurrence_path')
    return path


def provision(root, owner, bindings):
    """Operator-only initial provisioning; never called by a Work operation.

    Requires separate review and explicit activation approval. NEVER use this
    to replace lost state: restore original claims from verified backup instead.
    Root is the internal bridge state directory, not a request argument.
    """
    from .work_image_receipts import _owner_key
    import uuid
    _validate_bindings(bindings)
    path = _store_path(root)
    marker = path.with_name('schedule_occurrence_activation.json')
    raw = json.dumps({'owner': _owner_key(owner), 'bindings': bindings, 'activation_id': str(uuid.uuid4())}, sort_keys=True).encode()
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=3)) as conn:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('PRAGMA user_version=2')
        conn.execute('CREATE TABLE activation (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, binding TEXT NOT NULL)')
        conn.execute('INSERT INTO activation VALUES (1,?,?)', (_owner_key(owner), _hash(raw.decode())))
        conn.execute('CREATE TABLE occurrences (owner TEXT, schedule TEXT, day TEXT, manifest TEXT NOT NULL, PRIMARY KEY(owner,schedule,day))')
        conn.execute('CREATE TABLE parts (owner TEXT, schedule TEXT, day TEXT, part INTEGER, hash TEXT NOT NULL, effective_hash TEXT NOT NULL, request TEXT NOT NULL, comment INTEGER NOT NULL, result TEXT, PRIMARY KEY(owner,schedule,day,part))')
        # Conservative migration: all past dates and the activation date may
        # have old notify.self sends. Reserve them before enabling this route.
        for schedule, (start, end, _) in bindings.items():
            day, through = date.fromisoformat(start), min(_now().date(), date.fromisoformat(end))
            while day <= through:
                conn.execute('INSERT INTO occurrences VALUES (?,?,?,?)',
                             (_owner_key(owner), schedule, day.isoformat(), 'legacy_reserved_unknown'))
                day += timedelta(days=1)
        conn.commit()


def _check_store(conn, owner, marker_hash):
    from .work_image_receipts import _owner_key
    if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise ValueError('occurrence_store_corrupt')
    if conn.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise ValueError('occurrence_schema_unknown')
    if conn.execute('SELECT id,owner,binding FROM activation').fetchall() != [(1, _owner_key(owner), marker_hash)]:
        raise ValueError('occurrence_activation_mismatch')
    expected = {
        'activation': ['id', 'owner', 'binding'],
        'occurrences': ['owner', 'schedule', 'day', 'manifest'],
        'parts': ['owner', 'schedule', 'day', 'part', 'hash', 'effective_hash', 'request', 'comment', 'result'],
    }
    for table, fields in expected.items():
        if [row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')] != fields:
            raise ValueError('occurrence_schema_unknown')


def _can_advance(result):
    evidence = result.get('evidence', {})
    return result.get('status') == 'ok' and evidence.get('message_state') == 'sent' and evidence.get('ambiguous') is not True


def _observed(result, timing, args):
    # Preserve original _notify flags/status; label, never upgrade, its heuristic.
    reply = json.loads(json.dumps(result))
    reply.setdefault('evidence', {}).update(timing, input_hash=args['message_hash'],
        effective_hash=args['effective_part_hashes'][args['part_index'] - 1],
        render_hash=args['render_hash'], attribution_state='heuristic', request_attributed=False)
    return reply


def execute(args, owner, send):
    from .work_image_receipts import _REQUEST, _owner_key
    identity = _REQUEST.get()
    if identity is None or identity.owner != owner:
        raise ValueError('authenticated_request_required')
    marker_hash, bindings = _activation(identity.root, owner)
    if args['schedule_id'] not in bindings:
        raise ValueError('schedule_not_registered')
    binding = bindings[args['schedule_id']]
    validate(args, binding)
    path = _store_path(identity.root)
    if not path.is_file():
        raise ValueError('occurrence_store_missing')
    key = (_owner_key(owner), args['schedule_id'], args['seattle_date'])
    manifest = _manifest(args)
    # mode=rw refuses missing files; no CREATE/repair/activation on this path.
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=3)) as conn:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        _check_store(conn, owner, marker_hash)
        validate(args, binding)  # Recheck time after waiting for a concurrent transaction.
        conn.execute('INSERT OR IGNORE INTO occurrences VALUES (?,?,?,?)', (*key, manifest))
        stored_manifest = conn.execute('SELECT manifest FROM occurrences WHERE owner=? AND schedule=? AND day=?', key).fetchone()[0]
        if stored_manifest == 'legacy_reserved_unknown':
            raise ValueError('legacy_occurrence_requires_reconciliation')
        if stored_manifest != manifest:
            raise ValueError('occurrence_conflict')
        part_key = (*key, args['part_index'])
        row = conn.execute('SELECT request,comment,result FROM parts WHERE owner=? AND schedule=? AND day=? AND part=?', part_key).fetchone()
        timing = {'occurrence_date': args['seattle_date'], 'late': _now().strftime('%H:%M') > binding[2]}
        if row:
            original = json.loads(row[2]) if row[2] else None
            reply = (_observed(original, timing, args) if original else
                     {'status': 'accepted', 'evidence': {**timing, 'message_state': 'unknown', 'ambiguous': True,
                                                       'attribution_state': 'heuristic', 'request_attributed': False}})
            reply['result'] = 'Occurrence already claimed; reconcile the original request without resending.'
            reply['evidence'].update(original_request_id=row[0], original_request_comment_id=row[1], saved_result=original)
            return reply
        if args['part_index'] > 1:
            previous = conn.execute('SELECT result FROM parts WHERE owner=? AND schedule=? AND day=? AND part=?', (*key, args['part_index'] - 1)).fetchone()
            if not previous or not previous[0] or not _can_advance(json.loads(previous[0])):
                raise ValueError('previous_part_unconfirmed')
        effective_hash = args['effective_part_hashes'][args['part_index'] - 1]
        conn.execute('INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,NULL)', (*part_key, args['message_hash'], effective_hash, identity.request_id, identity.comment_id))
        conn.commit()  # Must be durable before calling the existing guarded sender.
        result = send({'message': args['message']}, owner)
        conn.execute('UPDATE parts SET result=? WHERE owner=? AND schedule=? AND day=? AND part=?', (json.dumps(result), *part_key))
        conn.commit()
        return _observed(result, timing, args)
