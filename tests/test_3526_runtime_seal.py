# SPDX-License-Identifier: AGPL-3.0-only
"""Complete tree measurements and real flock races on disposable files only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seal = load('scellement-runtime')
runtime = load('runtime-release')


def empty_tree():
    body = dict(version=1, dirs=['.'], files={}, bytes=0)
    return dict(body, digest=seal.identity(body))


class SealTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'bundle'
        self.base.mkdir(mode=0o755)
        (self.base / 'runtime/bin').mkdir(parents=True)
        (self.base / 'lib').mkdir()
        (self.base / 'store').mkdir(mode=0o711)
        (self.base / 'runtime/bin/python').write_bytes(b'concrete interpreter')
        (self.base / 'runtime/pyvenv.cfg').write_text(f'home = {Path(sys._base_executable).resolve().parent}\ninclude-system-site-packages = false\n')
        (self.base / 'lib/dispatch-local-handoff.py').write_bytes(b'raise RuntimeError("MUST NOT IMPORT")')
        for path in self.base.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o644)
        (self.base / 'store').chmod(0o711)
        patch=mock.patch.object(runtime, 'trusted', side_effect=lambda path: Path(path))
        patch.start(); self.addCleanup(patch.stop)
        self.document = dict(version=1, sha='a'*40, machine_id='b'*32,
            trees={key: runtime.inventory(self.base / key) if key != 'store' else empty_tree()
                   for key in ('runtime', 'lib', 'store')})
        for prefix in ('runtime', 'lib'):
            for path in (self.base / prefix).rglob('*'):
                path.chmod(0o555 if path.is_dir() else 0o444)
            (self.base / prefix).chmod(0o555)
        def writable():
            for directory, children, files in os.walk(self.base):
                Path(directory).chmod(0o755)
        self.addCleanup(writable)
        # Preserve real type/mode/uid/inode/content checks. Only ancestor trust
        # is bypassed: test roots live under the shared temporary directory.
        self.protected = mock.patch.object(seal, 'protected', side_effect=lambda path, **kw: Path(path))
        self.protected.start()
        self.addCleanup(self.protected.stop)

    def verify(self, document=None, **kw):
        return seal.verify(document or self.document, self.base, uid=os.getuid(), **kw)

    def test_full_tree_no_import_and_no_cache(self):
        self.assertGreaterEqual(self.verify(), 0)
        self.assertGreaterEqual(self.verify(), 0)
        path = self.base / 'lib/dispatch-local-handoff.py'
        path.chmod(0o644)
        path.write_bytes(b'other root-owned bytes')
        path.chmod(0o444)
        with self.assertRaises(seal.Refused):
            self.verify()

    def test_added_missing_substituted_file_and_old_pin(self):
        path = self.base / 'lib/extra.py'
        (self.base / 'lib').chmod(0o755)
        path.write_bytes(b'extra')
        path.chmod(0o444)
        with self.assertRaises(seal.Refused): self.verify()
        path.unlink()
        original = self.base / 'lib/dispatch-local-handoff.py'
        data = original.read_bytes()
        original.unlink()
        with self.assertRaises(seal.Refused): self.verify()
        original.write_bytes(data + b'new release')
        original.chmod(0o444)
        (self.base / 'lib').chmod(0o555)
        with self.assertRaises(seal.Refused): self.verify()  # prior pin
        # Bounded rollback to the exact previously authorized bytes.
        original.chmod(0o644); original.write_bytes(data); original.chmod(0o444)
        self.verify()

    def test_link_pth_hardlink_and_special_file_refused(self):
        parent = self.base / 'lib'
        parent.chmod(0o755)
        original = parent / 'dispatch-local-handoff.py'
        for kind in ('symlink', 'dirlink', 'pth', 'hardlink', 'fifo'):
            path = parent / ('evil.pth' if kind == 'pth' else 'extra')
            with self.subTest(kind=kind):
                if kind == 'symlink': path.symlink_to(original)
                elif kind == 'dirlink': path.symlink_to(self.base / 'runtime', target_is_directory=True)
                elif kind == 'hardlink': os.link(original, path)
                elif kind == 'fifo': os.mkfifo(path)
                else: path.write_text('import evil')
                try:
                    with self.assertRaises(seal.Refused): self.verify()
                finally: path.unlink()

    def test_read_detects_substitution_after_open(self):
        path = self.base / 'lib/dispatch-local-handoff.py'
        original_lstat = Path.lstat
        observed = 0
        def substitute(value, *args, **kw):
            nonlocal observed
            info = original_lstat(value, *args, **kw)
            if value == path:
                observed += 1
                if observed == 2:
                    (self.base / 'lib').chmod(0o755)
                    path.unlink(); path.write_bytes(b'substituted'); path.chmod(0o444)
                    return original_lstat(value)
            return info
        with mock.patch.object(Path, 'lstat', substitute):
            with self.assertRaises(seal.Refused): self.verify()

    def test_deadline_and_wrong_modes_refuse(self):
        with self.assertRaisesRegex(seal.Refused, 'deadline'):
            self.verify(clock=mock.Mock(side_effect=[0, seal.SECONDS+1]))
        (self.base / 'runtime/bin/python').chmod(0o666)
        with self.assertRaises(seal.Refused): self.verify()

    def test_even_a_measured_venv_cannot_expand_the_system_trust_boundary(self):
        config=self.base/'runtime/pyvenv.cfg'
        for text in (f'home = {Path(sys._base_executable).resolve().parent}\ninclude-system-site-packages = true\n',
                     'home = /outside/manifest\ninclude-system-site-packages = false\n'):
            config.chmod(0o644); config.write_text(text); config.chmod(0o444)
            doc=copy.deepcopy(self.document)
            doc['trees']['runtime']=runtime.inventory(self.base/'runtime')
            with self.assertRaisesRegex(seal.Refused,'system packages'):
                self.verify(doc)

    def test_manifest_digest_duplicate_and_traversal_refused(self):
        for change in ('digest', 'path', 'pth'):
            doc = copy.deepcopy(self.document)
            tree = doc['trees']['lib']
            if change == 'digest': tree['digest'] = '0'*64
            else:
                entry = tree['files'].pop('dispatch-local-handoff.py')
                tree['files']['../escape' if change == 'path' else 'evil.pth'] = entry
                tree['digest'] = seal.identity({k:v for k,v in tree.items() if k != 'digest'})
            with self.assertRaises(seal.Refused): self.verify(doc)
        with self.assertRaises(seal.Refused): json.loads('{"v":1,"v":2}', object_pairs_hook=seal.unique)

    def test_install_waits_until_execution_releases_shared_lock(self):
        lock = Path(self.temp.name) / 'lock'
        lock.write_text('')
        lock.chmod(0o600)
        entered, finished = threading.Event(), threading.Event()
        def install():
            entered.set()
            with seal.locked(lock, exclusive=True, uid=os.getuid()):
                finished.set()
        with seal.locked(lock, uid=os.getuid()):
            thread = threading.Thread(target=install)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.05))
            self.verify()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())

    def test_missing_or_linked_lock_refuses(self):
        lock = Path(self.temp.name) / 'lock'
        with self.assertRaises(OSError):
            with seal.locked(lock, uid=os.getuid()): self.fail('admitted')
        lock.symlink_to(self.base / 'runtime/bin/python')
        with self.assertRaises(OSError):
            with seal.locked(lock, uid=os.getuid()): self.fail('admitted')

    def test_bootstrap_binds_pin_and_verifier_to_existing_signed_scope(self):
        from tests.test_l232b_bootstrap import bundle
        bootstrap = load('bootstrap-release')
        original = bundle()
        plan = dict(original['plan'], runtime_tree_sha256=self.document['trees']['runtime']['digest'],
                    source_tree_sha256=self.document['trees']['lib']['digest'])
        doc = dict(self.document, machine_id=plan['machine_id'])
        rendered = bootstrap.delegation.render(plan, account_ids={'uid':987,'gid':987},
            enrollment={bootstrap.delegation.SEAL_DOCUMENT:seal.canonical(doc).decode()})
        text = bootstrap.question('seal-test', rendered, expires_at=2000000000)
        question = json.loads(text[len(bootstrap.PREFIX):])
        self.assertIn(bootstrap.delegation.SEAL_DOCUMENT, question['file_sha256'])
        self.assertIn(bootstrap.delegation.SEAL_ENTRY, question['file_sha256'])
        changed = copy.deepcopy(rendered)
        changed['files'][bootstrap.delegation.SEAL_DOCUMENT]['content'] = '{}'
        with self.assertRaises(ValueError): bootstrap.scopes('seal-test', changed)


class LaunchTest(unittest.TestCase):
    setUp = SealTest.setUp
    def invoke(self, pin):
        import contextlib
        import types
        with mock.patch.object(seal.os, 'geteuid', return_value=0), \
             mock.patch.object(seal.sys, 'flags', types.SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=1)), \
             mock.patch.object(seal, 'locked', return_value=contextlib.nullcontext()), \
             mock.patch.object(seal, 'PIN', pin), \
             mock.patch.object(seal, 'BASE', self.base), \
             mock.patch.object(seal, 'read', wraps=seal.read) as reader, \
             mock.patch.object(seal, 'verify', side_effect=lambda doc, base: verify_original(doc,base,uid=os.getuid())), \
             mock.patch.object(seal.subprocess, 'run') as child:
            # Metadata remains real, with the fixture uid passed to reads.
            reader.side_effect=lambda path, limit, **kw: read_original(path,limit,uid=os.getuid())
            with self.assertRaises((ValueError, OSError)):
                seal.main([])
            child.assert_not_called()

    def test_absent_and_old_pin_execute_nothing(self):
        pin=Path(self.temp.name)/'pin'
        self.invoke(pin)
        doc=copy.deepcopy(self.document)
        doc['machine_id']=Path('/etc/machine-id').read_text().strip()
        # A valid previous manifest cannot attest changed current bytes.
        (self.base/'runtime/bin/python').chmod(0o644)
        (self.base/'runtime/bin/python').write_bytes(b'new runtime')
        (self.base/'runtime/bin/python').chmod(0o444)
        pin.write_bytes(seal.canonical(doc)); pin.chmod(0o600)
        self.invoke(pin)


read_original=seal.read
verify_original=seal.verify

class LargePinTest(unittest.TestCase):
    def test_pin_snapshot_accepts_real_manifest_size_but_other_files_stay_bounded(self):
        import hashlib
        module=load('fichiers-bootstrap')
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(module.runtime,'trusted',side_effect=lambda path:Path(path)):
            engine=module.BootstrapFiles(Path(temp)/'journal',authority=object(),root=temp)
            pin=engine.path(module.delegation.SEAL_DOCUMENT)
            pin.parent.mkdir(parents=True)
            data=b'x'*70000
            pin.write_bytes(data); pin.chmod(0o600)
            self.assertEqual(engine.snapshot(pin)['sha256'],hashlib.sha256(data).hexdigest())
            other=pin.with_name('other.json'); other.write_bytes(data); other.chmod(0o600)
            with self.assertRaises(module.FileError): engine.snapshot(other)

class BootstrapLockTest(unittest.TestCase):
    setUp=SealTest.setUp

    def test_signed_bootstrap_keeps_exclusive_lock_through_every_effect(self):
        import fcntl
        from types import SimpleNamespace
        from tests.test_l232b_bootstrap import bundle, Authority, Host
        bootstrap=load('bootstrap-release')
        original=bundle()
        doc=dict(self.document,machine_id=original['machine_id'])
        plan=dict(original['plan'],runtime_tree_sha256=doc['trees']['runtime']['digest'],
                  source_tree_sha256=doc['trees']['lib']['digest'])
        review=bootstrap.delegation.render(plan,account_ids=dict(uid=987,gid=987),
            enrollment={bootstrap.delegation.SEAL_DOCUMENT:seal.canonical(doc).decode()})
        lock=Path(self.temp.name)/'lock'; lock.write_text(''); lock.chmod(0o600)
        host=Host()
        def assert_reserved(step):
            fd=os.open(lock,os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
            finally: os.close(fd)
        host.before=assert_reserved
        authority=Authority(); authority.denied=set()
        adapter=SimpleNamespace(locked=lambda **kw:seal.locked(lock,uid=os.getuid(),**kw))
        with mock.patch.object(bootstrap,'module',return_value=adapter):
            self.assertEqual(bootstrap.run(Path(self.temp.name)/'journal','seal-test',review,
                             authority=authority,adapter=host),'installed')
        self.assertEqual(host.effects,list(bootstrap.STEPS))
        with seal.locked(lock,uid=os.getuid()): pass
