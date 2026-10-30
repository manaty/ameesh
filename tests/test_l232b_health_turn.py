# SPDX-License-Identifier: AGPL-3.0-only
"""The controlled authenticated turn: real disposable release tree, simulated backend.

No root, SSH, systemd, PostgreSQL migration or production effect is executed:
the enrolled backend is simulated and the fixed system command is only built,
never run.
"""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'release_health_turn',
    Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/pilote-sante.py')
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


class Database:
    """Registry and turn resources, with the fixes the module may read."""

    def __init__(self):
        self.now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        self.registry = dict(name='one', host='fixture-host', status='idle', lease_owner=None,
                             lease_expires_at=None, lease_epoch=4, turns=2,
                             last_turn_at=self.now - timedelta(hours=1))
        self.turns = []
        self.running = 0
        self.unreachable = False

    def query(self, sql, params=()):
        if self.unreachable:
            raise OSError('fixture database unavailable')
        if 'clock_timestamp' in sql:
            return [{'now': self.now}]
        if 'agent_registry' in sql:
            if self.registry is None:
                return []
            # Model the projection: a column the module forgets to SELECT is
            # absent from the row, exactly like a real cursor description.
            columns = sql.split('FROM', 1)[0].split('SELECT', 1)[1]
            return [{name.strip(): self.registry.get(name.strip()) for name in columns.split(',')}]
        if 'count(*) AS running' in sql:
            return [{'running': self.running}]
        if 'turn_resources' in sql:
            return [dict(row) for row in self.turns if row['started_at'] >= params[2]]
        raise AssertionError(sql)


class Backend:
    """Simulated one-turn backend: mutates the evidence like a real --once run."""

    def __init__(self, db, *, code=0, effect=True, outcome='idle', error=None):
        self.db, self.code, self.effect, self.calls = db, code, effect, []
        self.outcome, self.error = outcome, error

    def one_turn(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.code, Exception):
            raise self.code
        if self.code == 0 and self.effect:
            self.db.registry['turns'] += 1
            self.db.registry['lease_epoch'] += 2
            self.db.registry['lease_owner'] = None
            self.db.registry['lease_expires_at'] = None
            self.db.registry['status'] = self.outcome
            self.db.registry['last_error'] = self.error
            self.db.registry['last_turn_at'] = self.db.now
            self.db.turns.append({'turn_id': 't1', 'status': 'done',
                                  'started_at': self.db.now, 'ended_at': self.db.now})
        return self.code


class HealthTurnTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ameesh-release-turn-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'release'
        (self.root / 'venv/bin').mkdir(parents=True, mode=0o700)
        self.runner = self.root / 'venv/bin/agent-runner'
        self.runner.write_text('#!/bin/sh\nexit 0\n')
        self.runner.chmod(0o700)
        self.root.chmod(0o700)
        self.db = Database()
        self.backend = Backend(self.db)
        self.enrollment = dict(agent='one', unit='ameesh-runner@one.service', account='ameesh',
                               home='/home/ameesh', seconds=30)
        self.turn = pilot.HealthTurn(self.db, enrollment=self.enrollment, host='fixture-host',
                                     system=self.backend)

    def call(self):
        return self.turn.one_turn(operation='release-fixture', step='health',
                                  release='a' * 40, root=self.root)

    def test_both_runner_unit_templates_of_the_same_agent_are_accepted(self):
        # The deployed user template is `ameesh-runner-agent@`; the system
        # template `ameesh-runner@` remains valid on an L55 server. Both name
        # this agent's own unit; neither is an alias for another agent.
        for unit in ('ameesh-runner@one.service', 'ameesh-runner-agent@one.service'):
            with self.subTest(unit=unit):
                database = Database()
                backend = Backend(database)
                turn = pilot.HealthTurn(database, enrollment=dict(self.enrollment, unit=unit),
                                        host='fixture-host', system=backend)
                self.assertEqual(turn.unit, unit)
                self.assertTrue(turn.one_turn(operation='release-fixture', step='health',
                                              release='a' * 40, root=self.root))
                self.assertEqual(backend.calls[0]['unit'], unit)
                self.assertEqual(backend.calls[0]['agent'], 'one')

    def test_a_runner_unit_of_another_agent_or_another_name_is_refused(self):
        for unit in ('ameesh-runner@other.service',
                     'ameesh-runner-agent@other.service',
                     'ameesh-runner@one.service.extra',
                     'ameesh-runner-agent@one',
                     'ameesh-runner@One.service',
                     'ameesh-runner-agent@one.service ',
                     'runner-agent@one.service',
                     None):
            with self.subTest(unit=unit):
                with self.assertRaises(pilot.TurnError):
                    pilot.HealthTurn(self.db, enrollment=dict(self.enrollment, unit=unit),
                                     host='fixture-host', system=self.backend)

    def test_one_completed_turn_is_accepted_with_the_enrolled_identity(self):
        self.assertTrue(self.call())
        self.assertEqual(len(self.backend.calls), 1)
        call = self.backend.calls[0]
        self.assertEqual(call['unit'], 'ameesh-runner@one.service')
        self.assertEqual(call['agent'], 'one')
        self.assertEqual(call['host'], 'fixture-host')
        self.assertEqual(call['account'], 'ameesh')
        self.assertEqual(call['home'], '/home/ameesh')
        self.assertEqual(call['seconds'], 30)
        self.assertEqual(call['executable'], str(self.runner))

    def test_a_blocked_turn_outcome_is_refused(self):
        # A blocked turn (an authentication failure, a host refusal) still
        # increments `turns` and closes its resource; the outcome is evidence.
        self.backend = Backend(self.db, outcome='blocked',
                               error='auth_failure: account not authenticated')
        self.turn = pilot.HealthTurn(self.db, enrollment=self.enrollment, host='fixture-host',
                                     system=self.backend)
        self.assertFalse(self.call())
        self.assertEqual(len(self.backend.calls), 1)

    def test_a_recorded_turn_error_is_refused_even_when_the_status_is_idle(self):
        # B3 : l'erreur enregistrée est lue dans le registre ; un tour clos
        # `idle` mais marqué en erreur n'est pas une preuve de santé.
        self.backend = Backend(self.db, outcome='idle',
                               error='probe: authentication failed')
        self.turn = pilot.HealthTurn(self.db, enrollment=self.enrollment, host='fixture-host',
                                     system=self.backend)
        self.assertFalse(self.call())
        self.assertEqual(len(self.backend.calls), 1)

    def test_a_live_lease_or_running_turn_is_never_disturbed(self):
        for change in ({'status': 'running'},
                       {'lease_owner': 'runner-1',
                        'lease_expires_at': self.db.now + timedelta(minutes=5)}):
            self.db.registry.update(change)
            self.assertFalse(self.call(), change)
            self.assertEqual(self.backend.calls, [])
            self.db.registry.update({'status': 'idle', 'lease_owner': None,
                                     'lease_expires_at': None})

    def test_an_expired_lease_is_free(self):
        self.db.registry.update({'lease_owner': 'runner-1',
                                 'lease_expires_at': self.db.now - timedelta(minutes=5)})
        self.assertTrue(self.call())

    def test_no_turn_a_failure_or_wrong_evidence_is_refused(self):
        self.backend.code = 3
        self.assertFalse(self.call())
        self.backend.code = 1
        self.assertFalse(self.call())
        # A zero exit without the completed-turn evidence never claims health.
        self.backend.effect = False
        self.backend.code = 0
        self.assertFalse(self.call())
        # A still-running turn resource, or more than one, is not drained.
        self.db.turns = [{'turn_id': 't1', 'status': 'running',
                          'started_at': self.db.now, 'ended_at': None}]
        self.assertFalse(self.call())
        self.db.turns = [{'turn_id': 't0', 'status': 'done', 'started_at': self.db.now,
                          'ended_at': self.db.now}]
        self.backend.effect = True
        self.assertFalse(self.call())

    def test_a_held_lease_a_missing_increment_or_a_live_turn_is_refused(self):
        original = self.backend.one_turn
        snapshot = dict(self.db.registry)

        def reset():
            self.db.registry.clear()
            self.db.registry.update(snapshot)
            self.db.turns = []

        def held(**kwargs):
            code = original(**kwargs)
            self.db.registry['lease_owner'] = 'runner-1'
            self.db.registry['lease_expires_at'] = self.db.now + timedelta(minutes=5)
            return code

        self.backend.one_turn = held
        self.assertFalse(self.call())

        def twice(**kwargs):
            code = original(**kwargs)
            self.db.registry['turns'] += 1
            return code

        reset()
        self.backend.one_turn = twice
        self.assertFalse(self.call())

        def stale(**kwargs):
            code = original(**kwargs)
            self.db.registry['lease_epoch'] = snapshot['lease_epoch']
            return code

        reset()
        self.backend.one_turn = stale
        self.assertFalse(self.call())
        self.backend.one_turn = original

    def test_an_ambiguous_result_or_evidence_raises(self):
        for code in (True, '0', None):
            self.backend.code = code
            with self.assertRaises(pilot.TurnError):
                self.call()
        self.backend.code = 0
        self.backend.one_turn = mock.Mock(side_effect=pilot.TurnError('controlled turn timed out'))
        with self.assertRaises(pilot.TurnError):
            self.call()
        self.db.unreachable = True
        self.backend.one_turn = Backend(self.db).one_turn
        with self.assertRaises(pilot.TurnError):
            self.call()
        self.db.unreachable = False
        count = self.db.running

        def ambiguous_running(sql, params=()):
            if 'count(*) AS running' in sql:
                return []
            return Database.query(self.db, sql, params)

        self.db.query = ambiguous_running
        with self.assertRaises(pilot.TurnError):
            self.call()
        self.assertEqual(count, self.db.running)

    def test_a_missing_runner_or_an_invalid_enrollment_is_refused(self):
        self.runner.unlink()
        self.assertFalse(self.call())
        self.assertEqual(self.backend.calls, [])
        self.runner.write_text('#!/bin/sh\nexit 0\n')
        self.runner.chmod(0o700)
        for broken in (dict(self.enrollment, unit='ameesh-runner@other.service'),
                       dict(self.enrollment, agent='bad agent'),
                       dict(self.enrollment, account='Root'),
                       dict(self.enrollment, home='relative'),
                       dict(self.enrollment, home='/home/../etc'),
                       dict(self.enrollment, seconds=4),
                       dict(self.enrollment, seconds=True),
                       dict(self.enrollment, extra='x')):
            with self.assertRaises(pilot.TurnError, msg=broken):
                pilot.HealthTurn(self.db, enrollment=broken, host='fixture-host',
                                 system=self.backend)
        with self.assertRaises(pilot.TurnError):
            pilot.HealthTurn(self.db, enrollment=self.enrollment, host='bad host',
                             system=self.backend)
        with self.assertRaises(pilot.TurnError):
            pilot.HealthTurn(self.db, enrollment=self.enrollment, host='fixture-host',
                             system=object())
        with self.assertRaises(pilot.TurnError):
            pilot.HealthTurn(object(), enrollment=self.enrollment, host='fixture-host',
                             system=self.backend)

    def test_the_fixed_command_runs_one_bounded_turn_as_the_enrolled_account(self):
        runner = pilot.TurnRunner(runuser='/usr/sbin/runuser')
        result = mock.Mock(returncode=0, stdout=b'', stderr=b'')
        with mock.patch.object(pilot.subprocess, 'run', return_value=result) as started:
            self.assertEqual(runner.one_turn(unit='ameesh-runner@one.service', agent='one',
                                             host='fixture-host', account='ameesh',
                                             home='/home/ameesh', seconds=30,
                                             executable=str(self.runner)), 0)
        args, kwargs = started.call_args
        self.assertEqual(args[0], ['/usr/sbin/runuser', '-u', 'ameesh', '--', str(self.runner),
                                   '--host', 'fixture-host', '--agents', 'one', '--once',
                                   '--wait', '30'])
        self.assertEqual(kwargs['timeout'], 60)
        self.assertEqual(kwargs['check'], False)
        self.assertEqual(kwargs['env']['HOME'], '/home/ameesh')
        self.assertEqual(kwargs['env']['PATH'], '/usr/bin:/bin')
        self.assertNotIn('CANARY', json.dumps(kwargs['env']))

    def test_a_timed_out_or_unavailable_turn_command_raises(self):
        runner = pilot.TurnRunner()
        with mock.patch.object(pilot.subprocess, 'run',
                               side_effect=pilot.subprocess.TimeoutExpired('turn', 60)):
            with self.assertRaises(pilot.TurnError):
                runner.one_turn(unit='u', agent='one', host='h', account='ameesh',
                                home='/home/ameesh', seconds=30, executable='/bin/true')
        with mock.patch.object(pilot.subprocess, 'run', side_effect=OSError('unavailable')):
            with self.assertRaises(pilot.TurnError):
                runner.one_turn(unit='u', agent='one', host='h', account='ameesh',
                                home='/home/ameesh', seconds=30, executable='/bin/true')
        with mock.patch.object(pilot.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout=b'x' * 65537, stderr=b'')):
            with self.assertRaises(pilot.TurnError):
                runner.one_turn(unit='u', agent='one', host='h', account='ameesh',
                                home='/home/ameesh', seconds=30, executable='/bin/true')


if __name__ == '__main__':
    unittest.main()
