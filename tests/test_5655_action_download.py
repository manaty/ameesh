# SPDX-License-Identifier: AGPL-3.0-only
import copy
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import types
import unittest
from unittest import mock

from ameesh.ci import Watcher
from ameesh.ci_action_download import failed_action_download

ROOT = Path(__file__).resolve().parents[1]
JOB = {'id': 42, 'name': 'psql 7/16', 'conclusion': 'failure', 'steps': [
    {'name': 'Set up job', 'status': 'completed', 'conclusion': 'failure'}]}
ROW = {'run_id': 8, 'run_attempt': 1, 'head_sha': 'abc', 'branch': 'main'}


class DownloadFailureTest(unittest.TestCase):
    def test_real_logs_are_infrastructure(self):
        for file in (ROOT / 'tests/fixtures/5655').glob('*.log'):
            with self.subTest(file=file.name):
                self.assertTrue(failed_action_download(JOB, file.read_text()))

    def test_a_real_failure_or_a_recovered_download_stays_negative(self):
        text = (ROOT / 'tests/fixtures/5655/429.log').read_text()
        self.assertFalse(failed_action_download(JOB, text + '\nFAIL: test_real\n'))
        job = copy.deepcopy(JOB)
        job['steps'].append({'name': 'Tests', 'status': 'completed', 'conclusion': 'failure'})
        self.assertFalse(failed_action_download(job, text))
        job['steps'][0]['conclusion'] = 'success'
        self.assertFalse(failed_action_download(job, text))
        self.assertFalse(failed_action_download(JOB, 'HTTP 429 Too Many Requests'))

    def test_retry_is_only_for_listed_jobs_and_survives_watcher_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = types.SimpleNamespace(state_dir=tmp)
            api = mock.Mock(return_value={'status': 'completed', 'head_sha': 'abc', 'run_attempt': 1})
            rerun = mock.Mock()
            jobs = [JOB, dict(JOB, id=43)]
            for _ in range(2):
                watcher = Watcher(cfg, api=api, rerun_job=rerun)
                watcher._retry_download_jobs('org/repo', ROW, jobs)
            self.assertEqual(rerun.call_args_list, [mock.call('org/repo', 42), mock.call('org/repo', 43)])
            watcher._retry_download_jobs('org/repo', dict(ROW, run_attempt=2), [dict(JOB, id=44)])
            self.assertEqual(rerun.call_count, 2)

    def test_changed_head_or_running_attempt_is_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            rerun = mock.Mock()
            watcher = Watcher(types.SimpleNamespace(state_dir=tmp),
                api=lambda _: {'status': 'in_progress', 'head_sha': 'other', 'run_attempt': 2},
                rerun_job=rerun)
            watcher._retry_download_jobs('org/repo', ROW, [JOB])
            rerun.assert_not_called()

    def test_base_record_skips_infrastructure_but_keeps_real_failed_job(self):
        bad = dict(JOB, id=43, name='real failure', steps=[{'name': 'Tests', 'conclusion': 'failure'}])
        text = (ROOT / 'tests/fixtures/5655/429.log').read_text()
        watcher = Watcher(types.SimpleNamespace(), api=lambda _: {'jobs': [JOB, bad]}, logs=lambda *_: text)
        store = mock.Mock()
        watcher._record_failed_jobs(store, 'org/repo', ROW)
        self.assertEqual(store.ci.add_failure.call_count, 1)
        self.assertEqual(store.ci.add_failure.call_args.kwargs['test'], 'real failure')

    def test_terminal_infrastructure_is_not_a_red_test_verdict(self):
        text = (ROOT / 'tests/fixtures/5655/429.log').read_text()
        api = lambda _: {'jobs': [JOB]}
        watcher = Watcher(types.SimpleNamespace(), api=api, logs=lambda *_: text)
        store = mock.Mock()
        store.ci.failures.return_value = []
        store.ci.head_replaced.return_value = False
        store.ci.conclude.return_value = True
        run = {'id': 8, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        with mock.patch('ameesh.ci.storage.of', return_value=store), \
                mock.patch.object(watcher, '_retry_download_jobs') as retry, \
                mock.patch.object(watcher, '_verdict', return_value=['nonconclusive']) as verdict:
            self.assertEqual(watcher._conclude(None, 'org/repo', ROW, run=run), ['nonconclusive'])
        store.ci.conclude.assert_called_once_with('org/repo', 8, 'cancelled')
        self.assertEqual(verdict.call_args.args[-1], 'inconclusive')
        retry.assert_called_once()

    def test_incomplete_job_listing_cannot_hide_a_negative_on_another_page(self):
        text = (ROOT / 'tests/fixtures/5655/429.log').read_text()
        watcher = Watcher(types.SimpleNamespace(),
            api=lambda _: {'jobs': [JOB], 'total_count': 101}, logs=lambda *_: text)
        store = mock.Mock()
        store.ci.failures.return_value = []
        store.ci.head_replaced.return_value = True
        run = {'id': 8, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        with mock.patch('ameesh.ci.storage.of', return_value=store), \
                mock.patch.object(watcher, '_verdict'):
            watcher._conclude(None, 'org/repo', ROW, run=run)
        store.ci.conclude.assert_called_once_with('org/repo', 8, 'failure')

    def test_negative_evidence_prevents_nonconclusive_run(self):
        text = (ROOT / 'tests/fixtures/5655/429.log').read_text()
        watcher = Watcher(types.SimpleNamespace(), api=lambda _: {'jobs': [JOB]}, logs=lambda *_: text)
        store = mock.Mock()
        store.ci.failures.return_value = [{'test': 'test_real'}]
        store.ci.head_replaced.return_value = True
        run = {'id': 8, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        with mock.patch('ameesh.ci.storage.of', return_value=store), \
                mock.patch.object(watcher, '_verdict') as verdict:
            watcher._conclude(None, 'org/repo', ROW, run=run)
        store.ci.conclude.assert_called_once_with('org/repo', 8, 'failure')
        self.assertEqual(verdict.call_args.args[-1], 'failure')


class ArchiveCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('action_archive', ROOT / 'deploy/guest/action-archive-cache.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_all_workflow_actions_are_pinned(self):
        actions = self.module.actions(ROOT / '.github/workflows')
        self.assertIn(('actions/checkout', '11d5960a326750d5838078e36cf38b85af677262'), actions)

    def test_prefill_is_idempotent_and_repairs_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / 'workflows'
            workflows.mkdir()
            sha = 'a' * 40
            (workflows / 'test.yml').write_text(f'- uses: org/action/sub@{sha}\n')
            def safe(path):
                path = Path(path)
                path.mkdir(parents=True, exist_ok=True)
                return path
            def download(command, **_):
                output = command[command.index('--output') + 1]
                with tarfile.open(output, 'w:gz') as archive:
                    member = tarfile.TarInfo('action-root/action.yml')
                    member.size = 4
                    archive.addfile(member, io.BytesIO(b'test'))
            with mock.patch.object(self.module, 'safe_directory', side_effect=safe), \
                    mock.patch.object(self.module.subprocess, 'run', side_effect=download) as run:
                self.module.prefill(workflows, root / 'cache')
                target = root / 'cache/org_action' / f'{sha}.tar.gz'
                self.assertTrue(target.is_file())
                self.module.prefill(workflows, root / 'cache')
                self.assertEqual(run.call_count, 1)
                target.write_bytes(b'broken')
                self.module.prefill(workflows, root / 'cache')
                self.assertEqual(run.call_count, 2)

    def test_mutable_tag_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'test.yml').write_text('- uses: org/action@v4\n')
            with self.assertRaises(ValueError):
                self.module.actions(tmp)
