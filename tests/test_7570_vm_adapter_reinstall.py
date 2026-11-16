# SPDX-License-Identifier: AGPL-3.0-only
"""R3 boundaries and publication on a fake /opt; no root, sudo or network."""
import datetime as dt
import importlib.util
import io
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
        self.old = self.base / ('venv-' + 'b' * 12)
        self.old.mkdir()
        (self.base / 'venv').symlink_to(self.old.name)
        # Only trust ownership is substituted; all paths, files, locks and
        # atomic replaces below are real in this fake /opt.
        for name, value in [('BASE', self.base),
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
    def extract(sha, git, source, work):
        (source / 'vm_adapter.py').write_text('reviewed source')

    @staticmethod
    def build(source, venv):
        venv.mkdir()
        (venv / 'installed').write_text('from ' + str(source))

    def install(self, **overrides):
        defaults = {'fetch': mock.Mock(return_value=(SHA, [])), 'extract': self.extract, 'build': self.build,
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

    def test_active_timer_refuses_before_fetch(self):
        fetch = mock.Mock()
        with self.assertRaises(r3.Refused):
            self.install(idle=mock.Mock(side_effect=r3.Refused('timer active')), fetch=fetch)
        fetch.assert_not_called()
        self.assertEqual((self.base / 'venv').resolve(), self.old)

    def test_failure_before_switch_keeps_old_venv_and_cleans_partial_build(self):
        for stage in ('extract', 'check_unit', 'build'):
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

    def test_forge_requires_main_release_marker_version_and_notes(self):
        responses = [b'', b'', (SHA + '\n').encode(), b'',
                     b'release commit\n\nameesh-release: 1.58.0\n',
                     b'[project]\nname = "ameesh"\nversion = "1.58.0"\n', b'']
        with mock.patch.object(r3, 'run', side_effect=responses) as child:
            self.assertEqual(r3.fetch(TAG, self.base)[0], SHA)
            self.assertIn('refs/heads/main:refs/remotes/origin/main', child.call_args_list[1].args[0])
            self.assertIn('merge-base', child.call_args_list[3].args[0])
        for index, value in ((2, b'invalid'), (4, b'ordinary commit'),
                             (5, b'[project]\nversion = "1.57.0"\n')):
            bad = list(responses)
            bad[index] = value
            with self.subTest(index=index), mock.patch.object(r3, 'run', side_effect=bad), self.assertRaises(r3.Refused):
                r3.fetch(TAG, self.base)
        for index in (1, 3, 6):
            bad = list(responses)
            bad[index] = r3.Refused('tag, ancestry or notes missing')
            with self.subTest(index=index), mock.patch.object(r3, 'run', side_effect=bad), self.assertRaises(r3.Refused):
                r3.fetch(TAG, self.base)

    def test_real_git_fetch_accepts_release_and_refuses_tag_outside_main(self):
        repo = self.base / 'forge'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['/usr/bin/git', '-C', str(repo),
                                            '-c', 'user.name=Test',
                                            '-c', 'user.email=test@example.invalid',
                                            *args], stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-b', 'main')
        (repo / 'pyproject.toml').write_text('[project]\nversion = "1.58.0"\n')
        notes = repo / 'docs/versions/1.58.0.md'
        notes.parent.mkdir(parents=True)
        notes.write_text('Release notes')
        git('add', '.')
        git('commit', '-m', 'release: 1.58.0\n\nameesh-release: 1.58.0')
        git('tag', TAG)
        expected = git('rev-parse', 'HEAD')
        work = self.base / 'fetch-work'
        work.mkdir()
        with mock.patch.object(r3, 'ORIGIN', str(repo)):
            sha, command = r3.fetch(TAG, work)
            self.assertEqual(sha, expected)
            source = self.base / 'extracted'
            source.mkdir()
            r3.extract(sha, command, source, work)
            self.assertEqual((source / 'pyproject.toml').read_bytes(),
                             (repo / 'pyproject.toml').read_bytes())
            git('checkout', '-b', 'outside-main')
            (repo / 'pyproject.toml').write_text('[project]\nversion = "1.59.0"\n')
            git('add', '.')
            git('commit', '-m', 'release: 1.59.0\n\nameesh-release: 1.59.0')
            git('tag', 'v1.59.0')
            other = self.base / 'other-fetch'
            other.mkdir()
            with self.assertRaises(r3.Refused):
                r3.fetch('v1.59.0', other)
            missing = self.base / 'missing-fetch'
            missing.mkdir()
            with self.assertRaises(r3.Refused):
                r3.fetch('v9.9.9', missing)

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
