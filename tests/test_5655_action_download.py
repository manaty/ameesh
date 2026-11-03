# SPDX-License-Identifier: AGPL-3.0-only
import copy
import importlib.util
import io
import os
import subprocess
import sys
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
    def test_log_texts_are_released_between_ticks_even_on_error(self):
        logs = mock.Mock(return_value='large log')
        watcher = Watcher(types.SimpleNamespace(), api=mock.Mock(), logs=logs)

        def observe(db, repo):
            self.assertEqual(watcher._job_log_texts, {})
            watcher._job_log(repo, ROW, 42)
            watcher._job_log(repo, ROW, 42)
            raise RuntimeError('interrupted observation')

        with mock.patch.object(watcher, 'repos', return_value=['org/repo']), \
                mock.patch.object(watcher, '_watch', side_effect=observe), \
                mock.patch('ameesh.ci.storage.of'), \
                mock.patch('ameesh.plan_github.flush_reads'):
            for _ in range(2):
                watcher.tick(None)
                self.assertEqual(watcher._job_log_texts, {})
        self.assertEqual(logs.call_count, 2)

    def test_log_cache_is_scoped_to_repo_run_attempt_and_job(self):
        logs = mock.Mock(side_effect=['first', 'retry', 'other repo'])
        watcher = Watcher(types.SimpleNamespace(), logs=logs)
        self.assertEqual(watcher._job_log('org/repo', ROW, 42), 'first')
        self.assertEqual(watcher._job_log('org/repo', ROW, '42'), 'first')
        self.assertEqual(watcher._job_log('org/repo', dict(ROW, run_attempt=2), 42), 'retry')
        self.assertEqual(watcher._job_log('other/repo', ROW, 42), 'other repo')
        self.assertEqual(logs.call_count, 3)

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

    def test_retry_is_once_per_run_and_survives_watcher_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = types.SimpleNamespace(state_dir=tmp)
            api = mock.Mock(return_value={'status': 'completed', 'head_sha': 'abc', 'run_attempt': 1})
            rerun = mock.Mock()
            jobs = [JOB, dict(JOB, id=43)]
            for _ in range(2):
                watcher = Watcher(cfg, api=api, rerun_failed_jobs=rerun)
                watcher._retry_download_jobs('org/repo', ROW, jobs)
            self.assertEqual(rerun.call_args_list, [mock.call('org/repo', 8)])
            watcher._retry_download_jobs('org/repo', dict(ROW, run_attempt=2), [dict(JOB, id=44)])
            self.assertEqual(rerun.call_count, 1)

    def test_retry_posts_failed_jobs_endpoint_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            watcher = Watcher(types.SimpleNamespace(state_dir=tmp),
                api=lambda _: {'status': 'completed', 'head_sha': 'abc', 'run_attempt': 1})
            with mock.patch('ameesh.plan_github._json') as post:
                watcher._retry_download_jobs('org/repo', ROW, [JOB, dict(JOB, id=43)])
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[1],
                ['api', '-X', 'POST', 'repos/org/repo/actions/runs/8/rerun-failed-jobs'])

    def test_forbidden_retry_consumes_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            rerun = mock.Mock(side_effect=RuntimeError('HTTP 403 Forbidden'))
            for _ in range(2):
                watcher = Watcher(types.SimpleNamespace(state_dir=tmp),
                    api=lambda _: {'status': 'completed', 'head_sha': 'abc', 'run_attempt': 1},
                    rerun_failed_jobs=rerun)
                watcher._retry_download_jobs('org/repo', ROW, [JOB, dict(JOB, id=43)])
            rerun.assert_called_once_with('org/repo', 8)

    def test_mixed_or_incomplete_run_is_never_retried(self):
        text = (ROOT / 'tests/fixtures/5655/429.log').read_text()
        bad = dict(JOB, id=43, steps=[{'name': 'Tests', 'conclusion': 'failure'}])
        for listing in [{'jobs': [JOB, bad]}, {'jobs': [JOB], 'total_count': 101}]:
            watcher = Watcher(types.SimpleNamespace(), api=lambda _: listing, logs=lambda *_: text)
            store = mock.Mock()
            store.ci.failures.return_value = []
            store.ci.head_replaced.return_value = False
            run = {'id': 8, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
            with mock.patch('ameesh.ci.storage.of', return_value=store), \
                    mock.patch.object(watcher, '_retry_download_jobs') as retry, \
                    mock.patch.object(watcher, '_verdict'):
                watcher._conclude(None, 'org/repo', ROW, run=run)
            retry.assert_not_called()
            store.ci.conclude.assert_called_once_with('org/repo', 8, 'failure')

    def test_changed_head_or_running_attempt_is_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            rerun = mock.Mock()
            watcher = Watcher(types.SimpleNamespace(state_dir=tmp),
                api=lambda _: {'status': 'in_progress', 'head_sha': 'other', 'run_attempt': 2},
                rerun_failed_jobs=rerun)
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


class NativeInstallationTest(unittest.TestCase):
    def test_install_check_refresh_and_remove_native_units_without_touching_guest(self):
        installer = ROOT / 'deploy/ci-flotte/install-action-cache.sh'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = root / 'etc/systemd/system'
            units.mkdir(parents=True)
            native = units / 'actions.runner.manaty-ameesh.ameesh-ci-3-1.service'
            native.write_text('[Service]\nUser=runner\n')
            guest = units / 'ameesh-guest-ci@.service'
            guest.write_text('guest untouched\n')
            foreign = units / 'actions.runner.other-project.service'
            foreign.write_text('foreign untouched\n')
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo('root/action.yml')
                member.size = 4
                tar.addfile(member, io.BytesIO(b'test'))
            binary = root / 'bin'
            binary.mkdir()
            curl = binary / 'curl'
            curl.write_text('#!' + sys.executable + "\nimport os,shutil,sys\nshutil.copyfile(os.environ['FAKE_ARCHIVE'], sys.argv[sys.argv.index('--output')+1])\n")
            curl.chmod(0o755)
            # The installer's filesystem seam is under TMPDIR, whose outer
            # shared directories rightly fail the production root guard.
            # Keep real downloads/publication/checks; replace only that guard
            # in this unprivileged subprocess, never in deployed code.
            python = binary / 'python3'
            python.write_text('#!' + sys.executable + '\n' + """
import pathlib, sys
source = sys.argv[2]
ns = {'__name__': 'test_helper'}
exec(compile(pathlib.Path(source).read_text(), source, 'exec'), ns)
def private_directory(path):
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path
ns['safe_directory'] = private_directory
args = sys.argv[3:]
ns['check' if '--check' in args else 'prefill'](
    args[args.index('--workflows')+1], args[args.index('--cache')+1])
""")
            python.chmod(0o755)
            env = dict(os.environ, CI_FLOTTE_TEST_ROOT=tmp, FAKE_ARCHIVE=str(archive),
                       PATH=str(binary) + os.pathsep + os.environ['PATH'])
            def run(mode):
                return subprocess.run(['bash', str(installer), mode], env=env, text=True, capture_output=True)
            self.assertNotEqual(run('--check').returncode, 0)
            result = run('--install')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            dropin = Path(str(native) + '.d/action-cache.conf')
            self.assertIn('ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=/usr/local/lib/ameesh-ci/action-archives', dropin.read_text())
            self.assertEqual(run('--check').returncode, 0)
            cached = next((root / 'usr/local/lib/ameesh-ci/action-archives').glob('*/*.tar.gz'))
            cached.write_bytes(b'corrupt')
            self.assertNotEqual(run('--check').returncode, 0)
            result = run('--install')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(run('--check').returncode, 0)
            self.assertEqual(run('--remove').returncode, 0)
            self.assertFalse(dropin.exists())
            self.assertTrue(cached.exists())
            self.assertEqual(guest.read_text(), 'guest untouched\n')
            self.assertEqual(foreign.read_text(), 'foreign untouched\n')
            self.assertEqual(native.read_text(), '[Service]\nUser=runner\n')
