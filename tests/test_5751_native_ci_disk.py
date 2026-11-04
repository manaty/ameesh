# SPDX-License-Identifier: AGPL-3.0-only
"""#5751 : le relevé natif survit au silence GitHub ; chaque refus reste dû."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import ci, ci_disk, plan_github, storage
from .test_5325_ci_disque import disque
from .test_5742_ci_test_space import espace, statvfs
from .support import PgTestCase

REPO = 'acme/widget'
HOST = 'ci-a'
NOW = 1793700000


def refusal(**over):
    return dict(dict(schema='ameesh-ci-disk-refusal/1', id='a' * 32, repo=REPO,
                     host=HOST, run_id='42', attempt='1', job='psql', runner='r1',
                     sampled_ts=NOW, reason='4 GiB libres'), **over)


def snapshot(**over):
    return dict(dict(schema=ci_disk.SCHEMA, host=HOST, volume='/', free_bytes=1024**3,
                     total_bytes=60 * 1024**3, sampled_ts=NOW), **over)


class NativeTest(unittest.TestCase):
    def test_settings_bound_to_declared_repo_and_no_ssh_options(self):
        raw = dict(projects={'widget': REPO}, ci_disk_native={REPO: {HOST: 'runner@vm'}})
        self.assertEqual(plan_github.parse_settings(raw).ci_disk_native[REPO][HOST], 'runner@vm')
        for value in ({'other/repo': {HOST: 'vm'}}, {REPO: {HOST: '-oProxyCommand=bad'}},
                      {REPO: {HOST: 'vm; id'}}, {REPO: {}}, [],
                      {REPO: {'bad/host': 'vm'}}):
            with self.subTest(value=value), self.assertRaises(plan_github.SettingsError):
                plan_github.parse_settings(dict(raw, ci_disk_native=value))

    def test_remote_command_is_fixed_and_bounded(self):
        with mock.patch.object(ci_disk.subprocess, 'run', return_value=SimpleNamespace(
                stdout='{"snapshot":null,"refusals":[]}')) as run:
            ci_disk.read_native('runner@vm')
        self.assertEqual(run.call_args.args[0][-2:], ['runner@vm', ci_disk.NATIVE_COMMAND])
        self.assertEqual(run.call_args.kwargs['timeout'], 15)
        self.assertIn('StrictHostKeyChecking=yes', run.call_args.args[0])

    def test_collect_keeps_original_time_and_expectation_when_vm_fails(self):
        st = mock.Mock()
        def read(target):
            if target == 'broken':
                raise TimeoutError()
            return {'snapshot': snapshot(sampled_ts=NOW - 8000), 'refusals': [refusal()]}
        ci_disk.collect_native(REPO, {HOST: 'vm', 'ci-b': 'broken'}, st, now=NOW, read=read)
        self.assertEqual(st.expect_disk.call_count, 2)
        self.assertEqual(st.observe_disk.call_args.kwargs['sampled_ts'], NOW - 8000)
        self.assertIsNone(st.observe_disk.call_args.kwargs['run_id'])
        st.observe_disk_refusal.assert_called_once()
        st.forget_disks.assert_called_once()

    def test_invalid_snapshot_cannot_resolve_low_and_refusals_still_collected(self):
        for over in (dict(host='wrong'), dict(sampled_ts=NOW + 301), dict(free_bytes=True),
                     dict(sampled_ts=float('nan')), dict(sampled_ts='yesterday')):
            with self.subTest(over=over):
                st = mock.Mock()
                ci_disk.collect_native(REPO, {HOST: 'vm'}, st, now=NOW, read=lambda _: {
                    'snapshot': snapshot(**over), 'refusals': [refusal()]})
                st.observe_disk.assert_not_called()
                st.observe_disk_refusal.assert_called_once()

    def test_refusal_validation_rejects_other_repo_age_schema_and_identity(self):
        st = mock.Mock()
        for over in (dict(repo='evil/repo'), dict(id='../../escape'), dict(schema='other'),
                     dict(sampled_ts=NOW - 7 * 86400 - 1), dict(sampled_ts=NOW + 301),
                     dict(run_id='not-a-run'), dict(attempt='1.2')):
            ci_disk.observe_refusal(REPO, HOST, refusal(**over), st, now=NOW)
        st.observe_disk_refusal.assert_not_called()

    def test_guard_notice_survives_spool_enospc_without_launching_test(self):
        output = io.StringIO()
        with mock.patch.object(espace, 'verifier', side_effect=OSError('full')), \
                mock.patch.object(espace.tempfile, 'NamedTemporaryFile', side_effect=OSError(28, 'full')), \
                mock.patch.object(espace.subprocess, 'Popen') as launch, \
                contextlib.redirect_stdout(output):
            self.assertEqual(espace.executer(['never']), 75)
        launch.assert_not_called()
        line, = [s for s in output.getvalue().splitlines() if 'title=ameesh-ci-disk-refusal/1' in s]
        self.assertEqual(json.loads(line.split('::')[-1])['schema'], 'ameesh-ci-disk-refusal/1')

    def test_snapshot_and_refusal_real_files_are_read_without_resampling(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(disque, 'NATIVE_DIR', Path(tmp)), \
                mock.patch.object(espace, 'NATIVE_DIR', Path(tmp)), \
                mock.patch.object(disque, 'espace', return_value=dict(
                    chemin='/', libre=1, total=100, utilise_pct=99)), \
                mock.patch.dict(espace.os.environ, {'GITHUB_REPOSITORY': REPO, 'GITHUB_RUN_ID': '42'}), \
                contextlib.redirect_stdout(io.StringIO()):
            document = disque.mesure(HOST, snapshot=True)
            espace.signaler_refus(OSError('full'))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                disque.natif()
            data = json.loads(output.getvalue())
            self.assertEqual(data['snapshot'], document)
            self.assertEqual(data['refusals'][0]['repo'], REPO)
            self.assertEqual(len(list(Path(tmp).glob('refus-*.json'))), 1)

    def test_native_reader_does_not_follow_symlinks_or_wait_on_fifo(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data.json').write_text('{"secret":true}')
            (root / 'link.json').symlink_to(root / 'data.json')
            os.mkfifo(root / 'pipe.json')
            for name in ('link.json', 'pipe.json'):
                with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                    disque._document_natif(root / name)

    def test_usage_reports_minimum_during_part_not_only_endpoints(self):
        usage = espace.Consommation()
        with mock.patch.object(espace.os, 'statvfs', side_effect=[statvfs(9), statvfs(6), statvfs(8)]):
            for _ in range(3):
                usage.relever()
        self.assertEqual((usage.before, usage.minimum), (9 * 1024**3, 6 * 1024**3))

    def test_native_runs_before_any_github_call_and_without_workflow_discovery(self):
        watcher = ci.Watcher(SimpleNamespace(), api=mock.Mock(side_effect=AssertionError('GitHub')),
                             clock=lambda: NOW)
        settings = plan_github.Settings(projects={'widget': REPO}, ci_disk_native={REPO: {HOST: 'vm'}})
        order = []
        with mock.patch.object(watcher, 'repos', return_value=[REPO]), \
                mock.patch.object(watcher, 'settings', return_value=settings), \
                mock.patch.object(watcher, '_native_disks', side_effect=lambda *a: order.append('native') or []), \
                mock.patch.object(watcher, 'watch', side_effect=lambda *a: order.append('github') or []), \
                mock.patch.object(ci.storage, 'of'), mock.patch.object(plan_github, 'flush_reads'):
            watcher.tick(object())
        self.assertEqual(order, ['native', 'github'])
        with mock.patch.object(watcher, 'settings', return_value=settings), \
                mock.patch.object(watcher, 'discovered_workflows') as discover:
            watcher._disks(object(), REPO)
            discover.assert_not_called()


class NativeStorageTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.st = storage.of(self.db).ci
        self.db.execute('TRUNCATE ci_disk_state, ci_disk_refusals')

    def test_missing_vm_becomes_unreadable_and_never_falsely_healthy(self):
        ci_disk.collect_native(REPO, {HOST: 'vm'}, self.st, now=NOW,
                               read=mock.Mock(side_effect=TimeoutError))
        row, = self.st.disks()
        rows = ci_disk.findings([row], [], row['first_seen_ts'] + ci_disk.MAX_AGE + 1)
        self.assertEqual([r['type'] for r in rows], ['ci_disk_unreadable'])

    def test_native_replay_cannot_overwrite_newer_low(self):
        self.st.observe_disk(REPO, host=HOST, volume='/', run_id=99,
                             sampled_ts=NOW, free_bytes=1, total_bytes=100)
        ci_disk.collect_native(REPO, {HOST: 'vm'}, self.st, now=NOW,
            read=lambda _: {'snapshot': snapshot(sampled_ts=NOW - 1), 'refusals': []})
        self.assertEqual(self.st.disks()[0]['free_bytes'], 1)

    def watcher(self):
        watcher = ci.Watcher(self.cfg, clock=lambda: NOW)
        watcher._project_pilot = lambda *a: 'pilot'
        return watcher

    def test_two_transports_and_watcher_restart_send_once_per_refusal(self):
        for host in (HOST, 'hostname-alias'):
            ci_disk.observe_refusal(REPO, host, refusal(), self.st, now=NOW)
        with mock.patch.object(ci, '_send', return_value=123) as send:
            self.assertEqual(len(self.watcher()._disk_refusal_messages(self.db, REPO)), 1)
            self.assertEqual(self.watcher()._disk_refusal_messages(self.db, REPO), [])
            ci_disk.observe_refusal(REPO, HOST, refusal(id='b' * 32), self.st, now=NOW)
            self.assertEqual(len(self.watcher()._disk_refusal_messages(self.db, REPO)), 1)
        self.assertEqual(send.call_count, 2)
        self.assertFalse(send.call_args.kwargs['urgent'])
        self.assertEqual(self.st.failures(REPO, 42), [])

    def test_real_mail_and_ack_are_committed_together(self):
        from ameesh import registry
        registry.upsert(self.db, 'pilot', harness='claude', host=self.cfg.host)
        ci_disk.observe_refusal(REPO, HOST, refusal(), self.st, now=NOW)
        result, = self.watcher()._disk_refusal_messages(self.db, REPO)
        mail, = self.db.query("SELECT id, body, recipient FROM agent_mailbox")
        self.assertEqual(mail['recipient'], 'pilot')
        self.assertEqual(mail['id'], result['message_id'])
        self.assertIn('Aucun test lancé', mail['body'])
        row, = self.db.query("SELECT message_id FROM ci_disk_refusals")
        self.assertEqual(row['message_id'], mail['id'])
        self.assertEqual(self.watcher()._disk_refusal_messages(self.db, REPO), [])

    def test_no_pilot_keeps_refusal_pending(self):
        ci_disk.observe_refusal(REPO, HOST, refusal(), self.st, now=NOW)
        watcher = self.watcher()
        watcher._project_pilot = lambda *a: None
        self.assertEqual(watcher._disk_refusal_messages(self.db, REPO), [])
        self.assertIsNone(self.db.query("SELECT message_id FROM ci_disk_refusals")[0]['message_id'])

    def test_concurrent_reader_cannot_send_locked_refusal(self):
        ci_disk.observe_refusal(REPO, HOST, refusal(), self.st, now=NOW)
        other = self.connect()
        try:
            with self.db.transaction() as tx:
                self.assertEqual(len(storage.of(tx).ci.disk_refusals(REPO)), 1)
                with other.transaction() as tx_other:
                    self.assertEqual(storage.of(tx_other).ci.disk_refusals(REPO), [])
        finally:
            other.close()

    def test_failed_send_rolls_back_ack_and_is_retried(self):
        ci_disk.observe_refusal(REPO, HOST, refusal(), self.st, now=NOW)
        with mock.patch.object(ci, '_send', side_effect=RuntimeError('mail unavailable')):
            with self.assertRaises(RuntimeError):
                self.watcher()._disk_refusal_messages(self.db, REPO)
        with mock.patch.object(ci, '_send', return_value=456):
            self.assertEqual(len(self.watcher()._disk_refusal_messages(self.db, REPO)), 1)

    def test_pending_annotation_refusal_retried_without_native_configuration(self):
        ci_disk.observe_refusal(REPO, HOST, refusal(), self.st, now=NOW)
        watcher = self.watcher()
        watcher.settings = lambda: plan_github.Settings(projects={'widget': REPO})
        with mock.patch.object(ci, '_send', return_value=1001) as send:
            self.assertEqual(len(watcher._native_disks(self.db, REPO)), 1)
            send.assert_called_once()

    def test_notice_fallback_binds_run_attempt_and_check_url(self):
        rows = [{'title': 'ameesh-ci-disk-refusal/1', 'message': json.dumps(refusal(attempt='2'))},
                {'title': 'ameesh-ci-disk-refusal/1', 'message': json.dumps(refusal())}]
        def api(path):
            if '/jobs?' in path:
                return {'jobs': [dict(id=7, conclusion='failure', check_run_url=
                    'https://api.github.com/repos/acme/widget/check-runs/70'),
                    dict(id=8, conclusion='failure', check_run_url='https://evil/check-runs/80')]}
            return rows
        watcher = self.watcher()
        watcher.api = api
        with mock.patch.object(ci, '_send', return_value=789) as send:
            out = watcher._github_disk_refusals(self.db, REPO, dict(run_id=42, run_attempt=1))
            self.assertEqual(len(out), 1)
            self.assertEqual(self.watcher()._disk_refusal_messages(self.db, REPO), [])
            send.assert_called_once()
