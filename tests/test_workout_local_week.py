"""Synthetic in-memory workout reads; never import runtime or open its database."""
import ast
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


class FrozenDateTime(datetime):
    instant = datetime(2026, 10, 8, 6, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz)


def load_commands(connection):
    source = ROOT / 'davosbot' / 'commands.py'
    tree = ast.parse(source.read_text())
    names = {'_workout_local_window', '_workout_display_text', '_workout_display_sets',
             '_cmd_workout_summary', '_cmd_workout'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = {'datetime': FrozenDateTime, 'timezone': timezone, 'ZoneInfo': ZoneInfo,
                 'json': json, 'closing': closing, 'BOT_DB_PATH': 'synthetic-only',
                 'sqlite3': Mock()}
    namespace['sqlite3'].connect.return_value = connection
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


class WorkoutLocalWeekTests(unittest.TestCase):
    def setUp(self):
        FrozenDateTime.instant = datetime(2026, 10, 8, 6, 30, tzinfo=timezone.utc)
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
            CREATE TABLE workout_entries(id INTEGER PRIMARY KEY, sender TEXT,
              date TEXT, created_at TEXT, muscle_group TEXT, exercise_name TEXT,
              sets_json TEXT, notes TEXT);
            CREATE TABLE workouts(id INTEGER PRIMARY KEY, ts TEXT, exercise TEXT,
              sets INTEGER, reps INTEGER, weight_lbs REAL, notes TEXT);
        ''')
        # Mock close lets both source reads share this explicitly synthetic DB.
        self.connection = Mock(wraps=self.db)
        self.connection.close = Mock()
        self.ns = load_commands(self.connection)

    def tearDown(self):
        self.db.close()

    def add(self, timestamp, exercise='bench', sender='synthetic-owner', sets=None, notes=''):
        self.db.execute('INSERT INTO workout_entries(sender, date, created_at, muscle_group, '
                        'exercise_name, sets_json, notes) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (sender, timestamp[:10], timestamp, 'chest', exercise,
                         json.dumps(sets if sets is not None else [{'weight': 100, 'reps': 8}]), notes))

    def summary(self):
        return self.ns['_cmd_workout_summary']('synthetic-owner')

    def today(self):
        return self.ns['_cmd_workout']('synthetic-owner')

    def test_week_starts_seattle_monday_and_excludes_future_other_sender(self):
        self.add('2026-10-05 06:59:59', 'previous-sunday')
        self.add('2026-10-05 07:00:00', 'monday')
        self.add('2026-10-08 06:30:01', 'future')
        self.add('2026-10-05 07:00:00', 'other-person', sender='someone-else')
        result = self.summary()
        self.assertIn('Week of 2026-10-05', result)
        self.assertIn('monday:', result)
        for excluded in ('previous-sunday:', 'future:', 'other-person:'):
            self.assertNotIn(excluded, result)
        self.assertIn('1 logged day(s)', result)

    def test_today_uses_seattle_midnight_for_both_sources(self):
        self.add('2026-10-07 06:59:59', 'yesterday')
        self.add('2026-10-07 07:00:00', 'local-midnight')
        self.add('2026-10-08 06:29:00', 'local-evening')
        self.add('2026-10-08 06:31:00', 'future')
        self.db.executemany('INSERT INTO workouts(ts, exercise, sets, reps, weight_lbs) VALUES (?, ?, 1, 8, 100)',
                            [('2026-10-07 06:59:59', 'old-yesterday'),
                             ('2026-10-07 07:00:00', 'old-midnight')])
        result = self.today()
        self.assertIn('local-midnight:', result)
        self.assertIn('local-evening:', result)
        self.assertIn('[legacy] old-midnight', result)
        for excluded in ('yesterday', 'future'):
            self.assertNotIn(excluded, result)

    def test_sunday_utc_monday_still_previous_seattle_week(self):
        FrozenDateTime.instant = datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc)
        first, start, _ = self.ns['_workout_local_window'](True)
        self.assertEqual(str(first), '2026-09-28')
        self.assertEqual(start, '2026-09-28T07:00:00+00:00')

    def test_dst_boundaries_use_offset_of_midnight_not_current_offset(self):
        for instant, expected in [
            (datetime(2026, 3, 8, 12, tzinfo=timezone.utc), '2026-03-08T08:00:00+00:00'),
            (datetime(2026, 11, 1, 12, tzinfo=timezone.utc), '2026-11-01T07:00:00+00:00'),
        ]:
            FrozenDateTime.instant = instant
            self.assertEqual(self.ns['_workout_local_window']()[1], expected)

    def test_offset_and_z_timestamps_are_understood(self):
        self.add('2026-10-07T22:30:00-07:00', 'offset')
        self.add('2026-10-08T06:00:00Z', 'utc-z')
        result = self.summary()
        self.assertIn('1 logged day(s)', result)
        self.assertIn('offset:', result)
        self.assertIn('utc-z:', result)

    def test_heaviest_set_keeps_actual_reps_and_effort(self):
        self.add('2026-10-08 06:00:00', sets=[{'weight': 80, 'reps': 12}, {'weight': 100, 'reps': 5}],
                 notes='RIR 2; per hand')
        result = self.summary()
        self.assertIn('100lbs x 5 (RIR 2; per hand)', result)
        self.assertNotIn('100lbs x 12', result)
        self.assertNotIn('105lbs', result)

    def test_invalid_saved_sets_are_partial_not_invented(self):
        self.add('2026-10-08 06:00:00', sets=[None, {'weight': -5, 'reps': 2},
                 {'weight': float('nan'), 'reps': 2}, {'weight': 100, 'reps': 2.5},
                 {'weight': True, 'reps': 8}, {'weight': 80, 'reps': 8}])
        result = self.summary()
        self.assertIn('chest: 1 sets', result)
        self.assertIn('Partial summary:', result)
        self.assertIn('80lbs x 8', result)
        self.assertIn('[partial sets]', self.today())

    def test_read_failure_never_claims_no_logs(self):
        self.connection.execute.side_effect = sqlite3.OperationalError('synthetic-error')
        self.assertIn('unavailable', self.summary())
        result = self.today()
        self.assertIn('coverage is incomplete', result)
        self.assertNotIn('No saved workout', result)
        self.assertNotIn('synthetic-error', result)

    def test_single_source_failure_preserves_other_source_and_warning(self):
        self.add('2026-10-08 06:00:00')
        self.db.execute('DROP TABLE workouts')
        result = self.today()
        self.assertIn('bench:', result)
        self.assertIn('Legacy workout entries unavailable.', result)

    def test_empty_coverage_is_honest(self):
        self.assertIn('legacy logs and unlogged activity are not included', self.summary())
        self.assertIn('No saved workout entries found today (Seattle time)', self.today())

    def test_large_result_and_notes_are_bounded_and_flagged(self):
        for i in range(205):
            self.add('2026-10-08 06:00:00', exercise='x' * 300 + str(i), notes='RIR 2 ' * 300)
        summary, today = self.summary(), self.today()
        self.assertIn('Partial summary:', summary)
        self.assertIn('[truncated]', summary)
        self.assertIn('Saved entries capped at 20.', today)
        self.assertLess(len(summary), 3000)
        self.assertLess(len(today), 9000)
        sql = [str(c.args[0]) for c in self.connection.execute.call_args_list]
        self.assertTrue(any('LIMIT 201' in q for q in sql))
        self.assertTrue(any('LIMIT 21' in q for q in sql))

    def test_queries_only_read_and_preserve_sender_scope(self):
        self.summary()
        self.today()
        calls = self.connection.execute.call_args_list
        self.assertTrue(all(c.args[0].startswith('SELECT ') for c in calls))
        entry_calls = [c for c in calls if 'FROM workout_entries' in c.args[0]]
        self.assertTrue(all('sender = ?' in c.args[0] and c.args[1][0] == 'synthetic-owner' for c in entry_calls))
        self.ns['sqlite3'].connect.assert_called_with('synthetic-only')


if __name__ == '__main__':
    unittest.main()
