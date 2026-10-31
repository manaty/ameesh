# SPDX-License-Identifier: AGPL-3.0-only
"""Elected release tick: no standby effects, one execution across candidates."""
import argparse
import os
import types
import unittest
from unittest import mock

from ameesh import packet_watch, release, release_cutoff, role_run, pilot_gesture, registry
from tests.support import PgTestCase


def report(host='a', epoch=7):
    return dict(role='pilot', host=host, epoch=epoch, held=True,
                stopped=False, owner='runner', left_s=180)


class ElectionTest(unittest.TestCase):
    def args(self):
        return argparse.Namespace(release_command='tick', project='example',
                                  now=None, version=None, force=False,
                                  no_staging=True, json=True)

    def cfg(self):
        return types.SimpleNamespace(host='a', release=dict(role='pilot'),
                                     packet_watch=dict(role='pilot'))

    def test_release_pass_is_exposed_by_the_cli(self):
        args = role_run.build_parser().parse_args(['pilot', '--pass', 'release'])
        self.assertEqual(args.pass_name, 'release')

    def test_standby_skips_both_release_and_packet_watch(self):
        with mock.patch.object(role_run, 'run', return_value=0) as wrapper, \
                mock.patch.object(release, '_release_tick') as cut, \
                mock.patch.object(release, '_packet_watch') as watch, \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(release.run(self.cfg(), self.args()), 0)
        cut.assert_not_called()
        watch.assert_not_called()
        args = wrapper.call_args.args[1]
        self.assertEqual((args.role, args.pass_name), ('pilot', 'release'))
        self.assertIn('--no-staging', args.command)
        self.assertIn('--project', args.command)

    def test_child_requires_live_epoch_gate_before_any_tick(self):
        cfg = self.cfg()
        with mock.patch.dict(os.environ, {role_run.ENV_ROLE: 'pilot',
                                         role_run.ENV_PASS: 'release'}), \
                mock.patch('ameesh.db.open_db') as opened, \
                mock.patch('ameesh.pilot_gesture.gate', side_effect=ValueError('stale')), \
                mock.patch.object(release, '_release_tick') as cut:
            with self.assertRaisesRegex(ValueError, 'stale'):
                release.run(cfg, self.args())
        opened.return_value.close.assert_called_once()
        cut.assert_not_called()

    def test_fenced_child_keeps_packet_watch_after_release_tick(self):
        with mock.patch.dict(os.environ, {role_run.ENV_ROLE: 'pilot',
                                         role_run.ENV_PASS: 'release'}), \
                mock.patch('ameesh.db.open_db'), \
                mock.patch('ameesh.pilot_gesture.gate') as gate, \
                mock.patch.object(release_cutoff, 'projects', return_value={'example': object()}), \
                mock.patch.object(release, '_release_tick', return_value=0) as cut, \
                mock.patch.object(release, '_packet_watch', return_value=0) as watch:
            self.assertEqual(release.run(self.cfg(), self.args()), 0)
        gate.return_value.hold.assert_not_called()
        cut.assert_called_once()
        watch.assert_called_once()

    def test_canonical_child_of_alias_never_acquires_another_pass(self):
        cfg = self.cfg()
        cfg.release['role'] = cfg.packet_watch['role'] = 'pilot-alias'
        for child, accepted in (('pilot', True), ('foreign-pilot', False)):
            with self.subTest(child=child), \
                    mock.patch.object(release_cutoff, 'projects', return_value={'example': object()}), \
                    mock.patch.dict(os.environ, {role_run.ENV_ROLE: child,
                                                 role_run.ENV_PASS: 'release'}, clear=True), \
                    mock.patch('ameesh.db.open_db') as opened, \
                    mock.patch.object(role_run, 'live', return_value=report()) as live, \
                    mock.patch.object(role_run, 'run') as wrapper, \
                    mock.patch.object(pilot_gesture, 'gate') as gate, \
                    mock.patch.object(release, '_release_tick', return_value=0) as cut, \
                    mock.patch.object(release, '_packet_watch', return_value=0):
                if accepted:
                    self.assertEqual(release.run(cfg, self.args()), 0)
                    gate.assert_called_once()
                    cut.assert_called_once()
                else:
                    with self.assertRaises(pilot_gesture.GestureRefused):
                        release.run(cfg, self.args())
                    gate.assert_not_called()
                    cut.assert_not_called()
                live.assert_called_once_with(opened.return_value, 'pilot-alias')
                wrapper.assert_not_called()
                opened.return_value.close.assert_called_once()

    def test_manual_watch_dry_run_is_preserved_by_wrapper(self):
        args = self.args()
        args.release_command, args.dry_run = 'packet-watch', True
        with mock.patch.object(role_run, 'run', return_value=0) as wrapper, \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(release.run(self.cfg(), args), 0)
        self.assertIn('--dry-run', wrapper.call_args.args[1].command)

    def test_direct_cutoff_and_staging_apply_require_the_elected_wrapper(self):
        for command in ('cutoff', 'staging-needed'):
            with self.subTest(command=command):
                args = self.args()
                args.release_command = command
                if command == 'staging-needed':
                    args.apply, args.actor = True, 'pilot'
                with mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(role_run, 'run', return_value=0) as wrapper, \
                        mock.patch.object(release, '_release_tick') as cut, \
                        mock.patch('ameesh.staging_pending.apply') as stage:
                    self.assertEqual(release.run(self.cfg(), args), 0)
                cut.assert_not_called()
                stage.assert_not_called()
                self.assertIn(command, wrapper.call_args.args[1].command)
                if command == 'staging-needed':
                    self.assertIn('--apply', wrapper.call_args.args[1].command)
                    self.assertIn('--actor', wrapper.call_args.args[1].command)

    def test_role_projects_replace_legacy_host_selection(self):
        cfg = self.cfg()
        cfg.release.update(host='retired', projects={'example': {}})
        cfg.packet_watch = dict(host='retired', projects={'example': {}})
        with mock.patch.object(release_cutoff.ProjectRelease, 'from_config', return_value='cut'):
            self.assertEqual(release_cutoff.projects(cfg), {'example': 'cut'})
        with mock.patch.object(packet_watch.ProjectWatch, 'from_config', return_value='watch'):
            self.assertEqual(packet_watch.projects(cfg), {'example': 'watch'})

    def test_elected_synchronous_staging_is_refused_before_wrapper(self):
        cfg = self.cfg()
        cfg.release['projects'] = {'example': {'repo': '/fixture/repo'}}
        for command in ('tick', 'packet-watch', 'cutoff', 'staging-needed'):
            with self.subTest(command=command):
                args = self.args()
                args.release_command, args.apply = command, True
                with mock.patch.object(role_run, 'run') as wrapper, \
                        mock.patch('ameesh.db.open_db') as opened, \
                        mock.patch.object(release_cutoff, 'run_staging') as stage:
                    with self.assertRaisesRegex(release_cutoff.ReleaseError, 'staging_executor'):
                        release.run(cfg, args)
                wrapper.assert_not_called()
                opened.assert_not_called()
                stage.assert_not_called()

    def test_elected_staging_uses_independent_executor_configuration(self):
        cfg = self.cfg()
        cfg.release['projects'] = {'example': {'repo': '/fixture/repo',
            'staging_executor': '/fixture/executor.json'}}
        project = release_cutoff.projects(cfg)['example']
        self.assertEqual(project.staging_executor, '/fixture/executor.json')
        cfg.release.pop('role')
        cfg.packet_watch = {}
        cfg.release['projects']['example'].pop('staging_executor')
        self.assertEqual(release_cutoff.projects(cfg)['example'].staging_executor, '')

    def test_elected_nexlink_keeps_its_workflow_staging_route(self):
        cfg = self.cfg()
        cfg.release['projects'] = {'nexlink': {'repo': '/fixture/repo',
            'target': 'develop', 'model': 'nexlink'}}
        self.assertEqual(release_cutoff.projects(cfg)['nexlink'].model, 'nexlink')

    def test_watch_role_also_requires_independent_script_staging(self):
        cfg = self.cfg()
        cfg.release.pop('role')
        for executor in (None, '', '   ', True):
            cfg.release['projects'] = {'example': {'repo': '/fixture/repo',
                'staging_executor': executor}}
            with self.subTest(executor=executor), \
                    self.assertRaisesRegex(release_cutoff.ReleaseError, 'staging_executor'):
                release_cutoff.projects(cfg)

    def test_changed_role_stops_execution_renewal(self):
        lease = role_run._ReleaseLease(object(), 'pilot', 'a', 7)
        for current in (report('b', 8), report('a', 8),
                        dict(report(), held=False), dict(report(), stopped=True)):
            with mock.patch.object(role_run, 'live', return_value=current), \
                    mock.patch.object(role_run._AlertsLease, 'renew') as renew:
                self.assertFalse(lease.renew())
                renew.assert_not_called()

    def test_epoch_and_role_reach_the_watched_child(self):
        args = argparse.Namespace(role='pilot', pass_name='release', command=['tick'],
                                  min_left=0, dry_run=False, json=False)
        with mock.patch.object(role_run.db_mod, 'connect'), \
                mock.patch.object(role_run, 'live', return_value=report()), \
                mock.patch.object(role_run, '_ReleaseLease') as lease, \
                mock.patch.object(role_run, '_AlertsExecution') as execution, \
                mock.patch.object(role_run, '_run_under_lease', return_value=(0, False)) as child:
            lease.return_value.acquire.return_value = 1
            lease.return_value.remaining.return_value = 30
            execution.return_value.lost.is_set.return_value = False
            self.assertEqual(role_run.run(self.cfg(), args), 0)
        env = child.call_args.args[1]
        self.assertEqual(env[role_run.ENV_EPOCH], '7')
        self.assertEqual(env[role_run.ENV_ROLE], 'pilot')
        self.assertNotIn('AGENT_MAIL_NAME', env)
        lease.return_value.release.assert_called_once()

    def test_rollback_files_are_not_migrations_in_release_notes(self):
        with mock.patch.object(release_cutoff, '_out', return_value=(
                'src/ameesh/migrations/0518_forward.sql\n'
                'src/ameesh/migrations/rollback/0518_forward.sql\n')):
            self.assertEqual(release_cutoff.migrations_between('repo', 'a', 'b'),
                             ['0518_forward.sql'])


class ExecutionLeaseTest(PgTestCase):
    def test_real_aliased_release_gate_never_renews_the_runner_lease(self):
        name, owner = 'test-release-pilot', 'test-release-runner'
        registry.upsert(self.db, name, harness='other', host=self.cfg.host, status='idle')
        self.db.execute("UPDATE agent_registry SET releve='election', candidats=ARRAY[%s],"
                        " aliases=ARRAY['test-release-alias'] WHERE name=%s",
                        (self.cfg.host, name))
        lease = registry.claim(self.db, name, owner, 60, host=self.cfg.host)
        cfg = types.SimpleNamespace(host=self.cfg.host, release=dict(role='test-release-alias'),
                                    packet_watch={}, canon=[])
        env = {role_run.ENV_ROLE:name, role_run.ENV_PASS:'release',
               role_run.ENV_EPOCH:str(lease['lease_epoch']), role_run.ENV_OWNER:owner}
        def snapshot():
            return self.db.query("SELECT last_seen, lease_expires_at FROM agent_registry"
                                 " WHERE name=%s", (name,))[0]
        before = snapshot()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch('ameesh.db.open_db', return_value=self.db), \
                mock.patch.object(self.db, 'close'), \
                mock.patch.object(pilot_gesture, 'effective_ttl', return_value=(60, False)):
            self.assertIsNone(release._elected_tick(cfg, ElectionTest().args()))
            self.assertEqual(snapshot(), before)
            self.db.execute("UPDATE agent_registry SET lease_expires_at=clock_timestamp()"
                            " - interval '1 second' WHERE name=%s", (name,))
            expired = snapshot()
            for command in ('tick', 'packet-watch', 'cutoff', 'staging-needed'):
                args = ElectionTest().args()
                args.release_command, args.apply = command, True
                with self.subTest(command=command), \
                        mock.patch.object(release, '_release_tick') as cut, \
                        mock.patch.object(release, '_packet_watch') as watch, \
                        mock.patch('ameesh.staging_pending.apply') as stage:
                    with self.assertRaises(pilot_gesture.GestureRefused):
                        release.run(cfg, args)
                    cut.assert_not_called()
                    watch.assert_not_called()
                    stage.assert_not_called()
                    self.assertEqual(snapshot(), expired)

    def test_shared_lease_excludes_second_host_and_allows_successor(self):
        first = role_run._ReleaseLease(self.db, 'test-5015', 'a', 1)
        second = role_run._ReleaseLease(self.db, 'test-5015', 'b', 2)
        try:
            self.assertIsNotNone(first.acquire())
            self.assertIsNone(second.acquire())
            self.assertTrue(first.release())
            self.assertIsNotNone(second.acquire())
        finally:
            first.release()
            second.release()
