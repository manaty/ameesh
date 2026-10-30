# SPDX-License-Identifier: AGPL-3.0-only
"""Real Git objects and disposable payloads; no accounts, root or services."""
from tests import support
import base64
import binascii
import builtins
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare_handoff', ROOT / 'deploy/mise-a-jour/preparer-handoff.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class Registry:
    def __init__(self):
        self.rows = [dict(name='canary', host='fixture-host')]

    def query(self, sql, params):
        if params != ('canary',):
            raise AssertionError('unexpected registry query')
        return self.rows


class PreparationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Keep all file/uid/mode/link checks. Test roots may have a shared
        # writable ancestor; production retains runtime.trusted in full.
        patch = mock.patch.object(prepare.runtime, 'trusted', side_effect=lambda path: Path(path))
        patch.start()
        self.addCleanup(patch.stop)
        # Tests never execute a root operation, including on root-run CI.
        if os.getuid() == 0:
            self.skipTest('non-root file ownership fixture required')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.lib = self.repo / 'deploy/mise-a-jour'
        self.lib.mkdir(parents=True)
        for path in (ROOT / 'deploy/mise-a-jour').glob('*.py'):
            (self.lib / path.name).write_bytes(path.read_bytes())
        (self.lib / 'systemd').mkdir()
        for path in (ROOT / 'deploy/mise-a-jour/systemd').iterdir():
            (self.lib / 'systemd' / path.name).write_bytes(path.read_bytes())
        self.run_git('init', '-q')
        self.run_git('add', '.')
        self.run_git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.sha = self.run_git('rev-parse', 'HEAD').strip()
        self.runtime = self.root / 'runtime'
        (self.runtime / 'bin').mkdir(parents=True)
        (self.runtime / 'bin/python').write_bytes(b'not executed')
        (self.runtime / 'bin/python').chmod(0o755)
        (self.runtime / 'pyvenv.cfg').write_text('home = /usr/bin\ninclude-system-site-packages = false\n')
        controllers = {}
        for index, name in enumerate(prepare.NAMES):
            wire = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + bytes([index + 1]) * 32
            path = self.root / (name + '.pub')
            path.write_text('ssh-ed25519 ' + base64.b64encode(wire).decode())
            path.chmod(0o600)
            controllers[name] = dict(source='192.0.2.1/32', public_key_file=str(path))
        self.request = dict(version=1, host='fixture-host', agent='canary', sha=self.sha,
            operation='fixture', expires_at=2000000100,
            observation=dict(host='fixture-host', machine_id='a'*32, host_key_sha256='b'*64,
                account='executor', home='/home/executor', uid=1001, gid=1001,
                unit='ameesh-runner@canary.service', users=[dict(name='executor', uid=1001)],
                groups=[dict(name='executor', gid=1001)], source='administrator fixture', observed_at=2000000000),
            choices=dict(account_ids={name: dict(uid=980 + index, gid=980 + index)
                                      for index, name in enumerate(prepare.NAMES)}, controllers=controllers,
                         restart_seconds=30, probe_seconds=30, manager=dict(kind='system'),
                         execution=dict(state='/home/executor/state', config='/home/executor/config.json',
                             release_store='/opt/ameesh-release/store', roots=['/home/executor/work'],
                             tools=['git'], secrets=[], scope='system')))
        self.db = Registry()
        self.output = self.root / 'result'

    def run_git(self, *args):
        return subprocess.run(['/usr/bin/git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def assemble(self, request=None):
        return prepare.assemble(request or self.request, self.repo, self.runtime, self.output,
                                db=self.db, now=2000000001)

    def test_exact_consumer_plans_measured_bytes_and_no_live_commands(self):
        with mock.patch.object(prepare.backend.installer.LinuxHost, 'command',
                               side_effect=AssertionError('host effect forbidden')):
            manifest = self.assemble()
        release_dir = self.output / 'payload/fixture-release'
        value = json.loads((release_dir / 'plan.json').read_text())
        prepare.release.validate(value['plan'])
        self.assertEqual(value['plan']['runtime_tree_sha256'], prepare.runtime.inventory(release_dir / 'runtime')['digest'])
        self.assertEqual(value['plan']['policy']['manager'], {'kind': 'system'})
        self.assertEqual(value['plan']['source_tree_sha256'], prepare.runtime.inventory(release_dir / 'sources')['digest'])
        prepare.seal.validate(value['runtime_seal'])
        documents = self.output / 'documents'
        prepare.release.enrollment_documents({destination: (documents / Path(destination).name).read_text()
            for destination in prepare.release.ENROLLMENT_DOCUMENTS})
        execution = prepare.host_executor.validate_execution(json.loads((documents / 'execution.json').read_text()))
        with mock.patch.object(prepare.local_dispatch, 'protected', side_effect=lambda path, **kw: Path(path)):
            local_rules = prepare.local_dispatch.policy(documents / 'local.json')
        prepare.local_dispatch.consistent_policy(local_rules, execution)
        self.assertEqual(local_rules, dict(uid=1001, host='fixture-host', agents=['canary']))
        self.assertEqual(execution['accounts']['canary']['unit'], 'ameesh-runner@canary.service')
        self.assertTrue((release_dir / 'sources/systemd/ameesh-handoff-local.socket').exists())
        self.assertEqual(value['runtime_seal']['trees']['store']['files'], {})
        other = json.loads((self.output / 'payload/fixture-canaux/plan.json').read_text())
        prepare.channels.validate(other['plan'])
        prepare.channels.validate_account_ids(other['account_ids'])
        self.assertEqual(manifest['enrollment']['code_sha256'], prepare.backend.code_digest(release_dir / 'sources'))
        self.assertEqual(set(manifest['destinations']), {'release', 'canaux'})
        self.assertEqual(manifest['sha'], self.sha)
        stage = self.output / 'stage-administrateur.py'
        compile(stage.read_text(), str(stage), 'exec')
        self.assertEqual(manifest['staging']['argv'], ['/usr/bin/python3', str(stage)])
        result = subprocess.run(['/usr/bin/python3', str(stage)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('root, no arguments required', result.stderr)
        self.assertTrue(all(item['host'] == 'fixture-host' and item['account'] == 'root'
                            for item in manifest['commands']))
        self.assertEqual(manifest['digest'], prepare.seal.identity({k: v for k, v in manifest.items() if k != 'digest'}))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((release_dir / 'plan.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.assemble()

    def test_unknown_nested_fields_and_incorrect_host_refuse_before_output(self):
        for path in ((), ('observation',), ('choices',), ('choices', 'controllers', 'release'),
                     ('choices', 'account_ids', 'release'), ('choices', 'execution')):
            changed = copy.deepcopy(self.request)
            value = changed
            for key in path:
                value = value[key]
            value['unknown'] = 'refused'
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.assemble(changed)
            self.assertFalse(self.output.exists())
        self.db.rows[0]['host'] = 'another-host'
        with self.assertRaises(ValueError):
            self.assemble()
        self.assertFalse(self.output.exists())

    def test_backend_reads_both_generated_plans_and_exact_inventories(self):
        manifest = self.assemble()
        host = self.root / 'host'
        stage = host / prepare.backend.STAGE.lstrip('/')
        stage.mkdir(parents=True)
        for path in (self.output / 'payload').iterdir():
            shutil.copytree(path, stage / path.name)

        class System:
            def machine(self):
                return 'a' * 32

        with mock.patch.object(prepare.backend.files_mod.runtime, 'trusted', side_effect=lambda path: Path(path)):
            backend = prepare.backend.Backend(system=System(), enrollment=manifest['enrollment'],
                                              root=host, code=manifest['enrollment']['code_sha256'])
        for kind in ('release', 'canaux'):
            inventory = backend.inventory('fixture-' + kind)
            self.assertEqual(set(inventory['files']), set(manifest['destinations'][kind]))
            self.assertTrue(all(item is None for item in inventory['files'].values()))
            self.assertEqual(inventory['host'], 'fixture-host')
            self.assertEqual(inventory['machine_id'], 'a' * 32)
        self.assertFalse((stage / '.admin').exists())

    def test_generated_staging_executes_both_operations_once_and_keeps_exact_bytes(self):
        manifest = self.assemble()
        self.assertEqual([item['operation'] for item in manifest['commands']],
                         ['fixture-release', 'fixture-canaux'])
        # Exercise the defensive deduplication too: operation lists can contain
        # more than one command record without replaying mkdir on the stage.
        manifest['commands'] *= 2
        host = self.root / 'staging-host'
        stage = host / 'var/lib/ameesh-release-bootstrap'
        stage.mkdir(parents=True, mode=0o700)
        (host / 'etc/ssh').mkdir(parents=True)
        (host / 'etc/machine-id').write_text('a' * 32)
        public_key = (self.root / 'release.pub').read_text()
        (host / 'etc/ssh/ssh_host_ed25519_key.pub').write_text(public_key)
        manifest['observation']['host_key_sha256'] = prepare.backend.hashlib.sha256(
            base64.b64decode(public_key.split()[1])).hexdigest()
        source = prepare.staging_script(manifest, self.output)
        original_path = Path
        original_lstat = Path.lstat

        def mapped_path(*parts):
            value = original_path(*parts)
            if str(value) in ('/var/lib/ameesh-release-bootstrap', '/etc/machine-id',
                              '/etc/ssh/ssh_host_ed25519_key.pub'):
                return host / str(value).lstrip('/')
            return value

        def owned_lstat(value, *args, **kwargs):
            info = original_lstat(value, *args, **kwargs)
            fields = list(info)
            fields[4] = 0  # Only fixture ownership substitutes for root.
            if original_path(value) in host.parents:
                fields[0] &= ~0o022  # Shared test ancestors, not host dirs.
            return os.stat_result(fields)

        original_import = builtins.__import__

        def mapped_import(name, *args, **kwargs):
            if name == 'pathlib':
                return SimpleNamespace(Path=mapped_path)
            return original_import(name, *args, **kwargs)

        # Execute the actual generated source, with only fixed system paths,
        # root identity and fixture ownership remapped. File opens, copying,
        # modes, hashes, fsync and exclusive creation remain real.
        with mock.patch('builtins.__import__', side_effect=mapped_import), \
                mock.patch.object(Path, 'lstat', owned_lstat), mock.patch('os.geteuid', return_value=0), \
                mock.patch.object(sys, 'argv', ['stage-administrateur.py']), mock.patch('builtins.print'):
            namespace = {}
            exec(compile(source, '<generated staging>', 'exec'), namespace)
            self.assertEqual(namespace['PLAN']['operations'], ['fixture-release', 'fixture-canaux'])
            with self.assertRaises(FileExistsError):
                exec(compile(source, '<generated staging>', 'exec'), {})
        for name, expected in manifest['payload_inventory'].items():
            target = stage / name
            self.assertEqual(target.read_bytes(), (self.output / 'payload' / name).read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, expected['mode'])
        self.assertTrue((stage / 'fixture-release/plan.json').exists())
        self.assertTrue((stage / 'fixture-canaux/plan.json').exists())

    def test_same_public_key_blob_with_alternate_base64_is_not_distinct(self):
        key = (self.root / 'release.pub').read_text()
        alternate = key + '='
        self.assertNotEqual(key, alternate)
        # Python 3.12.15+ and 3.14 refuse excess padding under validate=True
        # (#4795): the alternate spelling is then rejected outright, which is
        # equally safe; older versions decode it to the same blob.
        try:
            decoded = base64.b64decode(alternate.split()[1], validate=True)
        except binascii.Error:
            expected = 'invalid delegation enrollment'
        else:
            self.assertEqual(base64.b64decode(key.split()[1], validate=True), decoded)
            expected = 'distinct controller'
        (self.root / 'handoff.pub').write_text(alternate)
        with self.assertRaisesRegex(ValueError, expected):
            self.assemble()
        self.assertFalse(self.output.exists())

    def test_occupied_ids_and_names_duplicate_keys_and_public_key_protection(self):
        variants = []
        for field in ('uid', 'gid'):
            changed = copy.deepcopy(self.request)
            changed['choices']['account_ids']['release'][field] = 1001
            variants.append(changed)
        changed = copy.deepcopy(self.request)
        changed['choices']['account_ids']['release'] = changed['choices']['account_ids']['handoff']
        variants.append(changed)
        changed = copy.deepcopy(self.request)
        changed['choices']['controllers']['handoff'] = changed['choices']['controllers']['release']
        variants.append(changed)
        changed = copy.deepcopy(self.request)
        changed['observation']['users'].append(dict(name='ameesh-release', uid=500))
        variants.append(changed)
        for request in variants:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.assemble(request)
        path = self.root / 'release.pub'
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.assemble()
        path.chmod(0o600)
        path.write_text('-----BEGIN OPENSSH PRIVATE KEY-----')
        with self.assertRaises(ValueError):
            self.assemble()
        with self.assertRaises(ValueError):
            json.loads('{"host":1,"host":2}', object_pairs_hook=prepare.unique)

    def test_source_revision_mismatch_refuses_and_checkout_changes_are_ignored(self):
        (self.lib / 'backend-administrateur.py').write_text('not the committed content')
        self.assemble()  # Git objects, not dirty checkout bytes.
        self.assertNotEqual((self.output / 'payload/fixture-release/sources/backend-administrateur.py').read_text(),
                            'not the committed content')
        other_output = self.root / 'other'
        self.output = other_output
        self.run_git('add', '.')
        self.run_git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'changed')
        self.request['sha'] = self.run_git('rev-parse', 'HEAD').strip()
        with self.assertRaisesRegex(ValueError, 'pinned source'):
            self.assemble()
        self.assertFalse(other_output.exists())

    def test_link_and_pth_runtime_refused_with_no_partial_output(self):
        for kind in ('symlink', 'pth'):
            path = self.runtime / ('extra.pth' if kind == 'pth' else 'extra')
            if kind == 'symlink':
                path.symlink_to(self.runtime / 'bin/python')
            else:
                path.write_text('import unsafe')
            try:
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    self.assemble()
                self.assertFalse(self.output.exists())
            finally:
                path.unlink()

    def test_execution_scope_must_match_observed_unit(self):
        self.request['choices']['execution']['scope'] = 'user'
        with self.assertRaises(ValueError):
            self.assemble()
        self.assertFalse(self.output.exists())

    def test_real_user_unit_uses_shared_consumer_validator(self):
        self.request['observation']['unit'] = 'ameesh-runner-agent@canary.service'
        self.request['choices']['execution']['scope'] = 'user'
        self.request['choices']['manager'] = dict(kind='user', account='executor')
        # Before #4765 this must refuse rather than invent an alias. When that
        # dependency is integrated the same test verifies the full output.
        try:
            prepare.entry.pilot.validate_enrollment(dict(agent='canary', unit=self.request['observation']['unit'],
                account='executor', home='/home/executor', seconds=30))
        except ValueError:
            with self.assertRaises(ValueError):
                self.assemble()
            return
        manifest = self.assemble()
        execution = json.loads((self.output / 'documents/execution.json').read_text())
        self.assertEqual(execution['accounts']['canary']['unit'], 'ameesh-runner-agent@canary.service')
        self.assertEqual(manifest['observation']['unit'], execution['accounts']['canary']['unit'])

    def test_manager_is_explicit_closed_and_matches_observed_executor(self):
        for manager in (None, {}, {'kind': 'user'}, {'kind': 'system', 'account': 'executor'},
                        {'kind': 'user', 'account': 'other'}, {'kind': 'user', 'account': 'executor'},
                        {'kind': 'invented'}):
            changed = copy.deepcopy(self.request)
            changed['choices']['manager'] = manager
            with self.subTest(manager=manager), self.assertRaises(ValueError):
                self.assemble(changed)
            self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
