# SPDX-License-Identifier: AGPL-3.0-only
"""R3 boundaries and publication on a fake /opt; no root, sudo or network."""
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / 'deploy/vm-adapter/remote'
SPEC = importlib.util.spec_from_file_location('r3', REMOTE / 'adapter-reinstall.py')
r3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r3)
TAG = 'v1.58.0'
SHA = '02dff619c640' + 'a' * 28
NOW = dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)


def report():
    hosts = {host: {'actif': SHA[:12]} for host in
             ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3')}
    return {'schema': 'ameesh-release-cutoff/1', 'project': 'ameesh',
            'result': 'released', 'tag': TAG, 'release_sha': SHA,
            'staging': {'exit': 0, 'attestation': {
                'schema': 'ameesh-staging-actif/1', 'sha': SHA, 'hotes': hosts,
                'reportes': {host: {'raison': 'offline'} for host in
                             ('pc-smichea', 'manaty-mesh-1')}}}}


class BoundariesTest(unittest.TestCase):
    def test_one_strict_tag_and_expiry(self):
        self.assertEqual(r3.request([TAG], NOW), TAG)
        for args in ([], [TAG, 'extra'], ['1.58.0'], ['v01.58.0'],
                     ['v1.58.0\n'], ['v1.58.0;id'], ['v1.58.0 --help'],
                     ['--help'], ['../../root'], ['v1.58.0-rc1'], ['v1.58.0$(id)']):
            with self.subTest(args=args), self.assertRaises(r3.Refused):
                r3.request(args, NOW)
        self.assertEqual(r3.request([TAG], r3.EXPIRES - dt.timedelta(seconds=1)), TAG)
        with self.assertRaisesRegex(r3.Refused, 'expired'):
            r3.request([TAG], r3.EXPIRES)

    def test_native_attestation_covers_all_agent_vms_and_exact_commit(self):
        good = report()
        self.assertEqual(r3.attested(good, TAG), SHA)
        for mutate in (
                lambda p: p.update(tag='v1.57.0'),
                lambda p: p.update(project='foreign'),
                lambda p: p.update(result='kept-active'),
                lambda p: p.update(release_sha=SHA[:12]),
                lambda p: p['staging'].update(exit=1),
                lambda p: p['staging'].update(exit=False),
                lambda p: p['staging']['attestation'].update(sha='b' * 40),
                lambda p: p['staging']['attestation']['hotes'].pop('ameesh-agents-3'),
                lambda p: p['staging']['attestation']['hotes']['ameesh-agents-1'].update(actif=''),
                lambda p: p['staging']['attestation']['hotes']['ameesh-agents-1'].update(actif='b' * 12),
                lambda p: p['staging']['attestation']['reportes'].update(**{'ameesh-agents-3': {'raison': 'absent'}}),
                lambda p: p['staging']['attestation']['reportes']['pc-smichea'].update(raison='')):
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.subTest(report=bad), self.assertRaises(r3.Refused):
                r3.attested(bad, TAG)

    def test_timer_active_or_unknown_refuses_even_with_inactive_service(self):
        for state in (b'active\n', b'activating\n', b'failed\n', b'\n'):
            with mock.patch.object(r3, 'run', return_value=state), \
                    self.subTest(state=state), self.assertRaises(r3.Refused):
                r3.idle()
        with mock.patch.object(r3, 'run', side_effect=[b'inactive\n', b'active\n']), \
                self.assertRaises(r3.Refused):
            r3.idle()
        with mock.patch.object(r3, 'run', side_effect=[b'inactive\n', b'failed\n']):
            r3.idle()

    def test_commands_scrub_environment_stdin_cwd_and_hide_child_stderr(self):
        child = mock.Mock(return_value=subprocess.CompletedProcess([], 0, b'inactive\n', b''))
        with mock.patch.dict(os.environ, {'PATH': '/hostile', 'PYTHONPATH': '/hostile',
                                         'GIT_CONFIG_COUNT': '9', 'PIP_INDEX_URL': 'secret'}), \
                mock.patch.object(r3.subprocess, 'run', child):
            r3.run(['/usr/bin/systemctl', 'show'])
        kwargs = child.call_args.kwargs
        self.assertEqual(kwargs['env'], r3.ENV)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['cwd'], r3.BASE)
        self.assertNotIn('shell', kwargs)
        child.return_value = subprocess.CompletedProcess([], 1, b'', b'SECRET')
        with mock.patch.object(r3.subprocess, 'run', child), \
                self.assertRaises(r3.Refused) as error:
            r3.run(['/usr/bin/git'])
        self.assertNotIn('SECRET', str(error.exception))

    def test_real_root_ownership_and_parent_checks(self):
        # No root mutation: /etc is a real root-controlled directory.
        r3.trusted('/etc', directory=True)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            path = Path(directory) / 'file'
            path.write_text('not root enrolled')
            with self.assertRaises(r3.Refused):
                r3.trusted(path)
            link = Path(directory) / 'link'
            link.symlink_to('/etc/passwd')
            with self.assertRaises(r3.Refused):
                r3.trusted(link)


class FakeInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'opt'
        self.base.mkdir()
        self.proofs = Path(self.temp.name) / 'proofs'
        self.proofs.mkdir()
        (self.proofs / (TAG + '.json')).write_text(json.dumps(report()))
        self.old = self.base / ('venv-' + 'b' * 12)
        self.old.mkdir()
        (self.base / 'venv').symlink_to(self.old.name)
        # Only trust ownership is substituted; all paths, files, locks and
        # atomic replaces below are real in this fake /opt.
        for name, value in [('BASE', self.base), ('PROOFS', self.proofs),
                            ('trusted', mock.Mock()), ('EXPIRES', dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc))]:
            patch = mock.patch.object(r3, name, value)
            patch.start()
            self.addCleanup(patch.stop)

        def read_file(path, limit=r3.MAX_JSON):
            return Path(path).read_bytes()

        patch = mock.patch.object(r3, 'read_file', side_effect=read_file)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def fetch(tag, sha, source, work):
        (source / 'vm_adapter.py').write_text('reviewed source')

    @staticmethod
    def build(source, venv):
        venv.mkdir()
        (venv / 'installed').write_text('from ' + str(source))

    def install(self, **overrides):
        defaults = {'fetch': self.fetch, 'build': self.build,
                    'check_unit': mock.Mock(), 'idle': mock.Mock()}
        defaults.update(overrides)
        with mock.patch.multiple(r3, **defaults), mock.patch('sys.stdout', new_callable=io.StringIO):
            r3.install(TAG)

    def test_nominal_build_then_atomic_publish_and_preserve_rollback(self):
        self.install()
        new = self.base / ('venv-' + SHA[:12])
        self.assertEqual((self.base / 'venv').resolve(), new)
        self.assertTrue((new / 'installed').is_file())
        self.assertEqual((self.base / 'venv-previous').resolve(), self.old)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(list(self.base.glob('.r3-*')))

    def test_missing_or_foreign_proof_refuses_before_fetch_or_build(self):
        (self.proofs / (TAG + '.json')).unlink()
        fetch = mock.Mock()
        with self.assertRaises(FileNotFoundError):
            self.install(fetch=fetch)
        fetch.assert_not_called()
        bad = report()
        bad['tag'] = 'v1.0.0'
        (self.proofs / (TAG + '.json')).write_text(json.dumps(bad))
        with self.assertRaises(r3.Refused):
            self.install(fetch=fetch)
        fetch.assert_not_called()

    def test_active_timer_refuses_before_fetch(self):
        fetch = mock.Mock()
        with self.assertRaises(r3.Refused):
            self.install(idle=mock.Mock(side_effect=r3.Refused('timer active')), fetch=fetch)
        fetch.assert_not_called()
        self.assertEqual((self.base / 'venv').resolve(), self.old)

    def test_failure_before_switch_keeps_old_venv_and_cleans_partial_build(self):
        for stage in ('fetch', 'check_unit', 'build'):
            with self.subTest(stage=stage), self.assertRaises(r3.Refused):
                self.install(**{stage: mock.Mock(side_effect=r3.Refused('failed'))})
            self.assertEqual((self.base / 'venv').resolve(), self.old)
            self.assertFalse((self.base / ('src-' + SHA[:12])).exists())
            self.assertFalse((self.base / ('venv-' + SHA[:12])).exists())

    def test_timer_reactivated_during_build_refuses_publication(self):
        with self.assertRaises(r3.Refused):
            self.install(idle=mock.Mock(side_effect=[None, r3.Refused('timer active')]))
        self.assertEqual((self.base / 'venv').resolve(), self.old)
        self.assertFalse((self.base / 'venv-previous').exists())

    def test_existing_destination_is_never_reused_or_removed(self):
        target = self.base / ('venv-' + SHA[:12])
        target.mkdir()
        marker = target / 'existing'
        marker.write_text('keep')
        with self.assertRaises(r3.Refused):
            self.install()
        self.assertEqual(marker.read_text(), 'keep')
        self.assertEqual((self.base / 'venv').resolve(), self.old)

    def test_installed_unit_must_match_except_the_one_user_directive(self):
        source = self.base / 'source'
        path = source / 'deploy/vm-adapter/ameesh-vm-adapter.service'
        path.parent.mkdir(parents=True)
        template = (ROOT / 'deploy/vm-adapter/ameesh-vm-adapter.service').read_bytes()
        path.write_bytes(template)
        installed = self.base / 'installed.service'
        installed.write_bytes(template.replace(b'User=REPLACE_WITH_EXISTING_OPERATOR_ACCOUNT', b'User=agent'))
        with mock.patch.object(r3, 'UNIT', installed), mock.patch.object(r3, 'run') as run:
            r3.check_unit(source)
            run.assert_called_once()
            installed.write_bytes(installed.read_bytes() + b'ExecStartPost=/bin/sh\n')
            run.reset_mock()
            with self.assertRaises(r3.Refused):
                r3.check_unit(source)
            run.assert_not_called()

    def test_fetched_tag_commit_mismatch_refuses_before_archive(self):
        child = mock.Mock(side_effect=[b'', b'', b'c' * 40 + b'\n'])
        with mock.patch.object(r3, 'run', child), self.assertRaises(r3.Refused):
            r3.fetch(TAG, SHA, self.base / 'source', self.base)
        self.assertEqual(child.call_count, 3)

    def test_archive_rejects_traversal_symlinks_hardlinks_and_duplicate_files(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE)):
            archive = self.base / 'test.tar'
            with tarfile.open(archive, 'w') as stream:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.linkname = '/etc/passwd'
                stream.addfile(item)
            with self.subTest(name=name), self.assertRaises(r3.Refused):
                r3.unpack(archive, self.base / 'source')


if __name__ == '__main__':
    unittest.main()
