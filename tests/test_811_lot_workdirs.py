# SPDX-License-Identifier: AGPL-3.0-only
"""L243-4: project directories and per-lot resume on real Postgres."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, lot_workdir, registry, storage, work
from ameesh.runner import AgentWorker

from . import test_l165c_sessions_par_lot as sessions
from . import test_810_multi_project_consumers as consumers


def card(policy):
    return SimpleNamespace(owns=lambda value: value is None,
                           host=lambda name: SimpleNamespace(policy=policy))


class DestinationTest(unittest.TestCase):
    def setUp(self):
        self.agent = dict(name='worker', projects=['alpha', 'beta'], cwd='/primary', host='banc')
        self.cfg = SimpleNamespace(host='banc')

    def path(self, policy, project='beta'):
        return lot_workdir.destination(self.cfg, self.agent, project, canons=[card(policy)])

    def test_primary_keeps_registry_cwd(self):
        self.assertEqual(self.path(canon.HostPolicy(), 'alpha'), '/primary')

    def test_secondary_uses_project_root_not_agent_override(self):
        policy = canon.HostPolicy(work_dirs={'worker': '/primary'},
                                  work_roots={'beta': '/secondary/{agent}'})
        self.assertEqual(self.path(policy), '/secondary/worker')

    def test_work_root_and_template(self):
        self.assertEqual(self.path(canon.HostPolicy(work_root='/roots/{agent}')),
                         '/roots/worker/beta')

    def test_missing_root_and_undeclared_project_refused(self):
        with self.assertRaises(lot_workdir.WorkdirError):
            self.path(canon.HostPolicy())
        with self.assertRaises(lot_workdir.WorkdirError):
            self.path(canon.HostPolicy(work_root='/roots'), 'gamma')

    def test_primary_directory_cannot_serve_secondary(self):
        with self.assertRaises(lot_workdir.WorkdirError):
            self.path(canon.HostPolicy(work_roots={'beta': '/primary'}))

    def test_conflicting_host_roots_refused(self):
        with self.assertRaises(lot_workdir.WorkdirError):
            lot_workdir.destination(self.cfg, self.agent, 'beta', canons=[
                card(canon.HostPolicy(work_root='/one')),
                card(canon.HostPolicy(work_root='/two'))])


class RoutingTest(sessions._Base):
    _env = sessions.RoutageTest._env
    tour = sessions.RoutageTest.tour

    def setUp(self):
        super().setUp()
        runner, lease = self.bail('worker')
        self.primary = self._cwd('worker')
        self.secondary = self._cwd('beta-worker')
        self.db.execute("UPDATE agent_registry SET team='alpha', projects='{alpha,beta}'"
                        " WHERE name='worker'")
        self.worker = AgentWorker(runner, registry.get(self.db, 'worker'), lease)
        self.addCleanup(self.worker.watchdog_stop.set)
        self.policy = canon.HostPolicy(work_dirs={'worker': self.primary},
                                        work_roots={'beta': self.secondary})
        patcher = mock.patch('ameesh.canon.load_configured', return_value=[card(self.policy)])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a = work.add(self.db, title='A', app='alpha', assignee='worker')['id']
        self.b = work.add(self.db, title='B', app='beta', assignee='worker')['id']

    def test_switch_and_resume_use_destination_cwd(self):
        self.tour(self.worker, self.a, 'start A', session='session-A')
        self.tour(self.worker, self.b, 'start B', session='session-B')
        row = registry.get(self.db, 'worker')
        self.assertEqual(row['cwd'], self.primary)
        self.assertEqual(row['session_cwd'], self.secondary)
        parked = storage.of(self.db).lot_sessions.listing('worker')[0]
        self.assertEqual(parked['cwd'], self.primary)
        argv = self.tour(self.worker, self.a, 'return A')
        self.assertEqual(argv[argv.index('--resume') + 1], 'session-A')
        parked = {r['work_item_id']: r for r in storage.of(self.db).lot_sessions.listing('worker')}
        self.assertEqual(parked[self.b]['cwd'], self.secondary)
        argv = self.tour(self.worker, self.b, 'return B')
        self.assertEqual(argv[argv.index('--resume') + 1], 'session-B')
        self.assertEqual(registry.get(self.db, 'worker')['cwd'], self.primary)

    def test_first_secondary_turn_without_previous_lot(self):
        self.tour(self.worker, self.b, 'start B', session='session-B')
        self.assertEqual(registry.get(self.db, 'worker')['session_cwd'], self.secondary)
        self.assertEqual(registry.get(self.db, 'worker')['cwd'], self.primary)

    def test_missing_secondary_does_not_switch_or_resume_primary(self):
        self.tour(self.worker, self.a, 'start A', session='session-A')
        os.rmdir(self.secondary)
        spec = self.worker.rotate_for_lot({'kind': 'event', 'prompt': 'B', 'lot': str(self.b)})
        self.assertTrue(spec['lot_switch_failed'])
        self.assertFalse(self.worker.run_turn(spec))
        self.assertEqual(registry.get(self.db, 'worker')['session_id'], 'session-A')
        self.assertEqual(registry.get(self.db, 'worker')['session_work_item'], str(self.a))
        self.assertFalse(self.worker.workdir_ready())
        os.mkdir(self.secondary)
        self.assertTrue(self.worker.workdir_ready())
        self.tour(self.worker, self.b, 'retry B', session='session-B')
        self.assertEqual(registry.get(self.db, 'worker')['session_cwd'], self.secondary)

    def test_mediated_secondary_cannot_use_primary_depot(self):
        self.worker.runner.mediated = True
        self.assertIsNone(self.worker._resolve_workdir(lot=str(self.b)))
        self.assertIn('no per-project depot', self.worker._lot_workdir_error)

    def test_multi_project_switch_also_rotates_a_continuous_session(self):
        self.db.execute("UPDATE agent_registry SET session_policy='jamais' WHERE name='worker'")
        self.tour(self.worker, self.a, 'start A', session='session-A')
        argv = self.tour(self.worker, self.b, 'start B', session='session-B')
        self.assertNotIn('--resume', argv)
        self.assertEqual(registry.get(self.db, 'worker')['session_cwd'], self.secondary)

    def test_same_lot_changed_secondary_root_starts_a_new_session(self):
        self.tour(self.worker, self.b, 'start B', session='session-B')
        self.policy.work_roots['beta'] = self._cwd('beta-new')
        argv = self.tour(self.worker, self.b, 'B at new root', session='session-B2')
        self.assertNotIn('--resume', argv)
        self.assertEqual(registry.get(self.db, 'worker')['session_cwd'], self._cwd('beta-new'))


class PickupTest(consumers.PriseMultiProjetsTest):
    """Existing primary-only tests still cover hosts without secondary roots."""
    def test_secondary_ready_lot_wins_existing_priority_order(self):
        self.multi()
        primary = self.lot('primary', app='ameesh', priority=2)
        secondary = self.lot('secondary', app='nexlink', priority=1)
        with mock.patch('ameesh.lot_workdir.destination', return_value=self.tmp):
            result = self.take()
        self.assertEqual([r['item']['id'] for r in result], [secondary['id']])
        self.assertIsNone(work.get(self.db, primary['id'])['assignee'])

    def test_ready_secondary_backlog_is_taken(self):
        self.multi()
        item = self.element('secondary improvement', 90, team='nexlink')
        with mock.patch('ameesh.lot_workdir.destination', return_value=self.tmp):
            result = self.take()
        self.assertEqual([r['item']['id'] for r in result], [item['id']])

    def test_missing_policy_leaves_lot_unassigned_with_one_reason(self):
        self.multi()
        item = self.lot('secondary', app='nexlink')
        self.assertEqual(self.take(), [])
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, item['id'])['assignee'])
        reasons = [e['note'] for e in work.events(self.db, item['id'])
                   if (e.get('note') or '').startswith('L243-4:')]
        self.assertEqual(len(reasons), 1)
        self.assertIn('Host work root', reasons[0])

    def test_dry_run_missing_policy_does_not_write_reason(self):
        self.multi()
        item = self.lot('secondary', app='nexlink')
        self.assertEqual(self.take(dry_run=True), [])
        self.assertFalse(any((e.get('note') or '').startswith('L243-4:')
                             for e in work.events(self.db, item['id'])))
