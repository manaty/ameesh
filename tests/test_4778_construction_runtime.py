# SPDX-License-Identifier: AGPL-3.0-only
"""Disposable tests for the reproducible bootstrap runtime builder (#4778).

No network, no root, no real pip install and no host effect: external commands
are mocked except Git on a disposable repository, and the one real interpreter
copy is the test interpreter itself.
"""
from tests import support  # noqa: F401 — environnement isolé du poste

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PREAMBLE = ('#!/bin/sh\n'
            "'''exec' \"$(dirname \"$(readlink -f \"$(command -v \"$0\" || printf '%s' \"$0\")\")\")/python\" \"$0\" \"$@\"\n"
            "' '''\n")


def load():
    spec = importlib.util.spec_from_file_location(
        'construction_runtime',
        ROOT / 'deploy/mise-a-jour/construction-runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load()


def make_repo(root):
    repo = Path(root) / 'repo'
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid')
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, env=env).stdout.decode().strip()
    git('init', '-q')
    (repo / 'src').mkdir()
    (repo / 'src' / 'value.txt').write_text('committed bytes\n')
    (repo / 'deploy' / 'mise-a-jour').mkdir(parents=True)
    (repo / 'deploy' / 'mise-a-jour' / 'runtime-pins.txt').write_bytes(
        (ROOT / 'deploy/mise-a-jour/runtime-pins.txt').read_bytes())
    (repo / 'deploy' / 'mise-a-jour' / 'runtime-build-pins.txt').write_bytes(
        (ROOT / 'deploy/mise-a-jour/runtime-build-pins.txt').read_bytes())
    git('add', '.')
    git('commit', '-q', '-m', 'fixture')
    return repo, git


class PinTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, text, name='pins.txt'):
        path = self.root / name
        path.write_text(text)
        return path

    def test_repository_pins_are_exact_and_hashed(self):
        for name, kind, required in (
                ('runtime-pins.txt', 'runtime', {'psycopg', 'psycopg-binary', 'psutil'}),
                ('runtime-build-pins.txt', 'build', {'setuptools'})):
            path = ROOT / 'deploy/mise-a-jour' / name
            pins = build.read_pins(path, kind=kind)
            self.assertEqual(pins['digest'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(required - set(pins['records']), set())
            for record in pins['records'].values():
                self.assertTrue(record['hashes'])
                self.assertTrue(all(len(value) == 64 for value in record['hashes']))
                self.assertTrue(record['version'])

    def test_unpinned_url_option_marker_duplicate_and_binary_are_refused(self):
        cases = {
            'psycopg==3.3.6\n': 'hash',
            'psycopg==3.3.6 --hash=sha256:' + 'a' * 64 + '\npsycopg==3.3.6 --hash=sha256:' + 'b' * 64 + '\n': 'duplicate',
            '-r other.txt\n': 'unhashed',
            'git+https://example.invalid/x@main\n': 'unhashed',
            'psycopg==3.3.6; python_version < "3.13" --hash=sha256:' + 'a' * 64 + '\n': 'exact',
            'psycopg>=3.3.6 --hash=sha256:' + 'a' * 64 + '\n': 'exact',
            'psycopg==3.3.6 --hash=sha512:' + 'a' * 64 + '\n': 'only',
            'psycopg==3.3.6 --hash=sha256:' + 'a' * 64 + ' \\\n': 'continuation',
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(build.BuildError, message):
                    build.read_pins(self.write(text), kind='runtime')

    def test_required_closure_and_regular_file(self):
        text = ('psycopg[binary]==3.3.6 --hash=sha256:' + 'a' * 64 + '\n'
                'psycopg-binary==3.3.6 --hash=sha256:' + 'b' * 64 + '\n')
        with self.assertRaisesRegex(build.BuildError, 'psutil'):
            build.read_pins(self.write(text), kind='runtime')
        path = self.write('setuptools==1.0 --hash=sha256:' + 'a' * 64 + '\n')
        link = self.root / 'link.txt'
        link.symlink_to(path)
        with self.assertRaises(build.BuildError):
            build.read_pins(link, kind='build')


class CloneTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.git = make_repo(self.root)
        self.sha = self.git('rev-parse', 'HEAD')

    def test_clean_clone_at_the_exact_sha(self):
        self.assertIsInstance(build.verify_clone(self.repo, self.sha), int)
        with self.assertRaisesRegex(build.BuildError, 'HEAD'):
            build.verify_clone(self.repo, 'f' * 40)
        with self.assertRaisesRegex(build.BuildError, '40-hex'):
            build.verify_clone(self.repo, self.sha[:12])

    def test_dirty_and_untracked_clone_refused(self):
        (self.repo / 'src' / 'value.txt').write_text('changed\n')
        with self.assertRaisesRegex(build.BuildError, 'clean'):
            build.verify_clone(self.repo, self.sha)
        (self.repo / 'src' / 'value.txt').write_text('committed bytes\n')
        (self.repo / 'untracked').write_text('x')
        with self.assertRaisesRegex(build.BuildError, 'clean'):
            build.verify_clone(self.repo, self.sha)

    def test_subdirectory_is_not_the_clone_root(self):
        with self.assertRaisesRegex(build.BuildError, 'clone root'):
            build.verify_clone(self.repo / 'src', self.sha)

    def test_archive_uses_commit_bytes_not_checkout_bytes(self):
        destination = self.root / 'source'
        (self.repo / 'src' / 'value.txt').write_text('checkout drift\n')
        build.extract_source(self.repo, self.sha, destination,
                             env=build._git_env(self.root))
        self.assertEqual((destination / 'src' / 'value.txt').read_text(),
                         'committed bytes\n')
        self.assertTrue((destination / 'deploy/mise-a-jour/runtime-pins.txt').is_file())

    def test_archive_links_and_traversal_refused(self):
        archive = self.root / 'evil.tar'
        with tarfile.open(archive, 'w') as tar:
            info = tarfile.TarInfo('../escape')
            info.size = 1
            tar.addfile(info, io.BytesIO(b'x'))
        with self.assertRaisesRegex(build.BuildError, 'unsafe'):
            build.extract_archive(archive, self.root / 'out')
        self.assertFalse((self.root / 'escape').exists())

        archive2 = self.root / 'link.tar'
        with tarfile.open(archive2, 'w') as tar:
            info = tarfile.TarInfo('link')
            info.type = tarfile.SYMTYPE
            info.linkname = 'target'
            tar.addfile(info)
        with self.assertRaisesRegex(build.BuildError, 'links'):
            build.extract_archive(archive2, self.root / 'out2')


class ScriptTest(unittest.TestCase):
    def test_entry_point_shebangs_are_rebased(self):
        simple = b'#!/tmp/build/runtime/bin/python\nprint("x")\n'
        self.assertEqual(build.rebase_script(simple), PREAMBLE.encode() + b'print("x")\n')
        long = (b'#!/bin/sh\n'
                b"'''exec' /a/very/long/build/runtime/bin/python \"$0\" \"$@\"\n"
                b"' '''\n"
                b'print("x")\n')
        self.assertEqual(build.rebase_script(long), PREAMBLE.encode() + b'print("x")\n')
        self.assertEqual(build.rebase_script(PREAMBLE.encode() + b'print("x")\n'),
                         PREAMBLE.encode() + b'print("x")\n')
        with self.assertRaises(build.BuildError):
            build.rebase_script(b'print("no shebang")\n')
        with self.assertRaisesRegex(build.BuildError, 'preamble'):
            build.rebase_script(b'#!/bin/sh\necho no\n')

    def test_preamble_runs_the_interpreter_beside_the_script(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime_dir = Path(temp.name) / 'runtime'
        (runtime_dir / 'bin').mkdir(parents=True)
        shutil.copyfile(Path(sys.executable).resolve(), runtime_dir / 'bin' / 'python')
        (runtime_dir / 'bin' / 'python').chmod(0o755)
        script = runtime_dir / 'bin' / 'demo'
        script.write_bytes(build.rebase_script(
            b'#!/tmp/build/python\nimport sys\nprint("ok", sys.argv[1])\n'))
        script.chmod(0o755)
        path = str(runtime_dir / 'bin') + ':/usr/bin:/bin'
        observed = subprocess.run([str(script), 'direct'], check=True,
                                  capture_output=True, text=True,
                                  env={'PATH': path}).stdout.strip()
        self.assertEqual(observed, 'ok direct')
        observed = subprocess.run(['demo', 'path'], check=True, capture_output=True,
                                  text=True, env={'PATH': path}).stdout.strip()
        self.assertEqual(observed, 'ok path')
        # Un lien hors du runtime trouve encore le python à côté du script réel.
        links = Path(temp.name) / 'links'
        links.mkdir()
        (links / 'demo').symlink_to(script)
        observed = subprocess.run([str(links / 'demo'), 'link'], check=True,
                                  capture_output=True, text=True,
                                  env={'PATH': '/usr/bin:/bin'}).stdout.strip()
        self.assertEqual(observed, 'ok link')
        observed = subprocess.run(['demo', 'linked-path'], check=True,
                                  capture_output=True, text=True,
                                  env={'PATH': str(links) + ':/usr/bin:/bin'}).stdout.strip()
        self.assertEqual(observed, 'ok linked-path')

    def test_record_hash_is_updated_after_rebase(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime_dir = Path(temp.name) / 'runtime'
        (runtime_dir / 'bin').mkdir(parents=True)
        (runtime_dir / 'bin' / 'python').write_bytes(b'not executed')
        script = runtime_dir / 'bin' / 'ameesh'
        script.write_bytes(b'#!/tmp/build/python\nprint(1)\n')
        script.chmod(0o755)
        dist = runtime_dir / 'lib/python3.12/site-packages/ameesh-9.9.dist-info'
        dist.mkdir(parents=True)
        (dist / 'RECORD').write_text(
            '../../../bin/ameesh,sha256=stale,1\n'
            'direct_url.json,sha256=stale,1\n')
        (dist / 'direct_url.json').write_text('{}')
        scripts = build.rewrite_scripts(runtime_dir)
        self.assertEqual(scripts, {'ameesh'})
        self.assertEqual(script.read_text(), PREAMBLE + 'print(1)\n')
        rows = (dist / 'RECORD').read_text().splitlines()
        final = PREAMBLE.encode() + b'print(1)\n'
        self.assertEqual(rows, ['../../../bin/ameesh,sha256=' + build._record_digest(final)
                                + ',' + str(len(final))])
        self.assertFalse((dist / 'direct_url.json').exists())

    def test_unrecorded_executable_is_refused(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime_dir = Path(temp.name) / 'runtime'
        (runtime_dir / 'bin').mkdir(parents=True)
        (runtime_dir / 'bin' / 'python').write_bytes(b'not executed')
        (runtime_dir / 'bin' / 'extra').write_text('#!/bin/sh\n')
        dist = runtime_dir / 'lib/python3.12/site-packages/ameesh-9.9.dist-info'
        dist.mkdir(parents=True)
        (dist / 'RECORD').write_text('')
        with self.assertRaisesRegex(build.BuildError, 'unrecorded'):
            build.rewrite_scripts(runtime_dir)


class MaterializeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        (self.runtime / 'bin').mkdir(parents=True)
        (self.runtime / 'lib/python3.12/site-packages').mkdir(parents=True)
        (self.runtime / 'include/python3.12').mkdir(parents=True)
        (self.runtime / 'lib64').symlink_to('lib')
        base = Path(sys.executable).resolve()
        (self.runtime / 'bin' / 'python').symlink_to(sys.executable)
        (self.runtime / 'bin' / 'python3').symlink_to(sys.executable)
        (self.runtime / 'bin' / 'python3.12').symlink_to(sys.executable)
        (self.runtime / 'bin' / 'activate').write_text('activate\n')
        (self.runtime / 'pyvenv.cfg').write_text(
            'home = %s\ninclude-system-site-packages = false\nversion = 3.12.3\n'
            'command = %s -m venv %s\n' % (base.parent, base, self.runtime))

    def test_materialized_interpreter_and_stable_config(self):
        version = build.materialize(self.runtime, python=sys.executable,
                                    env=build._pip_env(self.root, 0))
        self.assertRegex(version, r'^[0-9]+\.[0-9]+\.[0-9]+')
        self.assertTrue((self.runtime / 'bin' / 'python').is_file())
        self.assertFalse((self.runtime / 'bin' / 'python').is_symlink())
        for name in ('python3', 'python3.12', 'activate'):
            self.assertFalse((self.runtime / 'bin' / name).exists())
        self.assertFalse((self.runtime / 'lib64').exists())
        self.assertFalse((self.runtime / 'include').exists())
        config = (self.runtime / 'pyvenv.cfg').read_text()
        self.assertEqual(config, 'home = %s\ninclude-system-site-packages = false\nversion = %s\n'
                         % (Path(sys.executable).resolve().parent, version))
        observed = subprocess.run(
            [str(self.runtime / 'bin' / 'python'), '-I', '-B', '-c',
             'import sys; print(sys.prefix)'], check=True, capture_output=True,
            text=True, env=build._pip_env(self.root, 0)).stdout.strip()
        self.assertEqual(Path(observed).resolve(), self.runtime.resolve())


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        (self.runtime / 'bin').mkdir(parents=True)
        (self.runtime / 'lib').mkdir()
        (self.runtime / 'bin' / 'python').write_bytes(b'not executed')
        (self.runtime / 'lib' / 'module.py').write_bytes(b'x = 1\n')
        (self.runtime / 'pyvenv.cfg').write_text('home = /usr/bin\n')
        self.patch = mock.patch.object(build.runtime, 'trusted',
                                       side_effect=lambda path: Path(path))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_receipt_binds_the_measured_tree(self):
        build.seal_tree(self.runtime)
        pins = {'digest': '1' * 64}
        build_pins = {'digest': '2' * 64}
        wheel = {'name': 'ameesh-1.0-py3-none-any.whl', 'sha256': '3' * 64}
        document = build.receipt(self.runtime, sha='4' * 40, pins=pins,
                                 build_pins=build_pins, wheel=wheel,
                                 python_version='3.12.3', output=self.root)
        inventory = build.runtime.inventory(self.runtime)
        self.assertEqual(document['tree_sha256'], inventory['digest'])
        self.assertEqual(document['tree_files'], 3)
        self.assertEqual(document['tree_bytes'], inventory['bytes'])
        self.assertEqual(build.validate_receipt(document), document)
        path = self.root / 'receipt.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
        self.assertEqual(json.loads(path.read_text()), document)
        for change in ('extra', 'sha', 'python', 'pins_sha256', 'tree_files', 'wheel'):
            changed = dict(document)
            if change == 'extra':
                changed['unexpected'] = 1
            elif change == 'wheel':
                changed['wheel'] = dict(document['wheel'], name='other-1.0.whl')
            else:
                changed[change] = 'bad'
            with self.assertRaises(build.BuildError):
                build.validate_receipt(changed)

    def test_sanitize_removes_artifacts_refuses_pth_and_links(self):
        (self.runtime / 'lib' / '__pycache__').mkdir()
        (self.runtime / 'lib' / '__pycache__' / 'x.pyc').write_bytes(b'x')
        dist = self.runtime / 'lib/python3.12/site-packages/ameesh-9.9.dist-info'
        dist.mkdir(parents=True)
        (dist / 'RECORD').write_text('')
        (self.runtime / 'bin' / 'ameesh').write_bytes(b'#!/tmp/python\nprint(1)\n')
        (self.runtime / 'bin' / 'ameesh').chmod(0o755)
        # Record the entry point so rewrite_scripts accepts the bin inventory.
        (dist / 'RECORD').write_text('../../../bin/ameesh,sha256=stale,1\n')
        (self.runtime / 'lib' / 'inject.pth').write_text('/tmp')
        with self.assertRaisesRegex(build.BuildError, 'path injection'):
            build.sanitize(self.runtime)
        (self.runtime / 'lib' / 'inject.pth').unlink()
        build.sanitize(self.runtime)
        self.assertFalse((self.runtime / 'lib' / '__pycache__').exists())
        self.assertEqual((self.runtime / 'bin' / 'ameesh').read_text(),
                         PREAMBLE + 'print(1)\n')

        (self.runtime / 'lib' / 'link').symlink_to(self.runtime / 'lib' / 'module.py')
        with self.assertRaisesRegex(build.BuildError, 'symlink'):
            build.sanitize(self.runtime)

    def test_sanitize_refuses_hardlinks(self):
        os.link(self.runtime / 'lib' / 'module.py', self.runtime / 'lib' / 'other.py')
        with self.assertRaisesRegex(build.BuildError, 'hardlink'):
            build.sanitize(self.runtime)

    def test_sanitize_refuses_a_file_from_another_uid(self):
        # mesh-design #167612: the runtime tree must belong entirely to the
        # operator running the assembler; no root or other-account file.
        with mock.patch.object(build.os, 'geteuid', return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(build.BuildError, 'owned by the builder'):
                build.sanitize(self.runtime)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.output = self.root / 'out'

    def test_root_sha_and_existing_output_refused_before_any_effect(self):
        with mock.patch.object(build.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(build.BuildError, 'outside root'):
                build.build(self.repo, 'a' * 40, self.output)
        with self.assertRaisesRegex(build.BuildError, '40-hex'):
            build.build(self.repo, 'abc', self.output)
        self.output.mkdir()
        with self.assertRaisesRegex(build.BuildError, 'new output'):
            build.build(self.repo, 'a' * 40, self.output)

    def test_untrusted_parent_refused(self):
        shared = self.root / 'shared'
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        with self.assertRaisesRegex(build.BuildError, 'group/world'):
            build.build(self.repo, 'a' * 40, shared / 'out')

    def test_clone_overlap_refused(self):
        with mock.patch.object(build.runtime, 'trusted',
                               side_effect=lambda path: Path(path)):
            with self.assertRaisesRegex(build.BuildError, 'separate'):
                build.build(self.repo, 'a' * 40, self.repo / 'out')

    def test_build_wires_the_measured_pipeline(self):
        sha = 'a' * 40
        wheel = {'path': self.root / 'wheels/ameesh-1.0-py3-none-any.whl',
                 'name': 'ameesh-1.0-py3-none-any.whl', 'version': '1.0',
                 'sha256': 'b' * 64}
        document = {'tree_sha256': 'c' * 64}

        def source(repo, sha_value, destination, **kwargs):
            target = Path(destination) / 'deploy/mise-a-jour'
            target.mkdir(parents=True)
            for name in ('runtime-pins.txt', 'runtime-build-pins.txt'):
                shutil.copyfile(ROOT / 'deploy/mise-a-jour' / name, target / name)
            return Path(destination)

        def venv(path, *, python, with_pip, env):
            path = Path(path)
            (path / 'bin').mkdir(parents=True)
            (path / 'bin' / 'python').write_bytes(b'not executed')

        def materialize(path, *, python, env):
            path = Path(path)
            (path / 'bin').mkdir(parents=True, exist_ok=True)
            (path / 'bin' / 'python').write_bytes(b'not executed')
            (path / 'pyvenv.cfg').write_text('home = /usr/bin\n')
            return '3.12.3'

        patches = [
            mock.patch.object(build.runtime, 'trusted', side_effect=lambda path: Path(path)),
            mock.patch.object(build, 'verify_clone', return_value=123),
            mock.patch.object(build, 'extract_source', side_effect=source),
            mock.patch.object(build, 'create_venv', side_effect=venv),
            mock.patch.object(build, 'install_build_pins'),
            mock.patch.object(build, 'build_wheel', return_value=wheel),
            mock.patch.object(build, 'install_runtime_pins'),
            mock.patch.object(build, 'install_wheel'),
            mock.patch.object(build, 'materialize', side_effect=materialize),
            mock.patch.object(build, 'sanitize'),
            mock.patch.object(build, 'check_runtime'),
            mock.patch.object(build, 'seal_tree'),
            mock.patch.object(build, 'receipt', return_value=document),
        ]
        started = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        self.assertIs(build.build(self.repo, sha, self.output), document)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o755)
        self.assertFalse((self.output / '.build').exists())
        self.assertEqual(started[2].call_count, 1)
        self.assertEqual(started[3].call_count, 2)  # tools + runtime venvs
        for patched in started[4:]:
            self.assertTrue(patched.called)


if __name__ == '__main__':
    unittest.main()
