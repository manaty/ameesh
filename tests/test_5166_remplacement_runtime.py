# SPDX-License-Identifier: AGPL-3.0-only
"""Replacing a sealed runtime (#5166): real files, signatures on a disposable schema.

Only ancestor ownership and the reparenting of read-only directories (which
needs root) are adapted to the unprivileged test user; services stay simulated.
"""
import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest import mock

from tests import support
from tests import test_4323_backend_administrateur as fixture
from ameesh import decisions, authority, registry, signing, work

backend_mod = fixture.backend_mod
bootstrap = fixture.bootstrap
swap = backend_mod.module('remplacement-runtime')
continuation = backend_mod.module('continuation-bootstrap')
seal = swap.seal
HELD = 'held-bootstrap'


def rename(source, target, _real=os.rename):
    mode = stat.S_IMODE(os.lstat(source).st_mode)
    os.chmod(source, mode | 0o700)
    _real(source, target)
    os.chmod(target, mode)


def fill(tree, marker):
    (tree / 'bin').mkdir(parents=True, mode=0o755)
    program = tree / 'bin/python'
    program.write_text('synthetic ' + marker)
    program.chmod(0o755)
    home = Path(sys._base_executable).resolve().parent
    (tree / 'pyvenv.cfg').write_text('home = %s\ninclude-system-site-packages = false\n' % home)
    (tree / 'pyvenv.cfg').chmod(0o644)
    tree.chmod(0o755)
    (tree / 'bin').chmod(0o755)


def sources(tree, marker):
    tree.mkdir(mode=0o755)
    (tree / 'hote-release.py').write_text('# ' + marker + '\n')
    (tree / 'hote-release.py').chmod(0o644)


def freeze(tree):
    for directory, dirs, names in os.walk(tree, topdown=False):
        for name in names:
            path = Path(directory) / name
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        Path(directory).chmod(0o555)


def thaw(path):
    for directory, dirs, names in os.walk(path):
        Path(directory).chmod(0o700)


class Harness(fixture.Fixture, support.PgTestCase):
    def sign(self, view, text):
        option = decisions.match_option(view, text)
        digest = decisions.answer_digest(view, text, option, 'human:fixture-owner')
        authority.create_approval(self.db, self.seed, approver='fixture-owner', action=decisions.APPROVAL_ACTION,
            artifact_kind=decisions.APPROVAL_KIND, artifact_hash=digest, ttl=(self.now+900)-time.time())
        decisions.answer(self.cfg, self.db, view['id'], text, human='human:fixture-owner', channel='cli', signed=True)

    def ask(self, question, options):
        return decisions.ask(self.cfg, self.db, requester='fixture-controller', lot=str(self.lot['id']),
            question=question, options=['%s=%s' % (item['key'], item['label']) for item in options],
            needs_signature=True)

    def setUp(self):
        support.PgTestCase.setUp(self)
        fixture.Fixture.setUp(self)
        self.now = time.time(); self.seed = b'\x51'*32
        registry.upsert(self.db, 'fixture-owner', harness='other', host=self.cfg.host)
        authority.register_key(self.db, 'fixture-owner', signing.public_from_seed(self.seed), role='owner')
        registry.upsert(self.db, 'fixture-controller', harness='other', host=self.cfg.host)
        self.lot = work.add(self.db, title='Synthetic runtime replacement', assignee='fixture-controller')
        self.backend.db = self.db
        self.addCleanup(lambda: thaw(self.root))
        for patcher in (mock.patch.object(seal, 'protected', side_effect=lambda path, **kw: Path(path)),
                        mock.patch.object(os, 'rename', rename),
                        mock.patch.object(backend_mod, 'module', self.loader())):
            patcher.start(); self.addCleanup(patcher.stop)
        self.system.handoff_stopped = lambda: True
        self.seed_sealed_host()
        self.stage_replacement()
        self.item = self.backend.replacement('swap-1')

    def loader(self):
        original = backend_mod.module
        return lambda name: swap if name == 'remplacement-runtime' else original(name)

    def seed_sealed_host(self):
        self.base = self.root / 'opt/ameesh-release'
        self.base.mkdir(parents=True, exist_ok=True); self.base.chmod(0o755)
        (self.root / 'opt').chmod(0o755)
        fill(self.base / 'runtime', 'old')
        sources(self.base / 'lib', 'old')
        store = self.base / 'store'
        store.mkdir(exist_ok=True); store.chmod(0o711)
        freeze(self.base / 'runtime'); freeze(self.base / 'lib')
        trees = dict(runtime=swap.runtime.inventory(self.base/'runtime'), lib=swap.runtime.inventory(self.base/'lib'),
                     store=self.empty_store())
        self.old_pin = dict(version=1, sha='a'*40, machine_id=self.bundle['machine_id'], trees=trees)
        etc = self.root / 'etc/ameesh-release'
        etc.mkdir(parents=True, exist_ok=True)
        self.pin = etc / 'runtime-seal.json'
        self.pin.write_bytes(seal.canonical(self.old_pin)); self.pin.chmod(0o600)
        self.lock = etc / 'runtime-seal.lock'
        self.lock.write_text(''); self.lock.chmod(0o600)
        self.lock_stat = self.lock.stat()
        entry = self.root / 'usr/local/libexec/ameesh-runtime-seal.py'
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text('# enrolled verifier\n'); entry.chmod(0o555)
        key = self.root / 'etc/ssh/ssh_host_ed25519_key.pub'
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text('ssh-ed25519 AAAA\n'); key.chmod(0o644)

    @staticmethod
    def empty_store():
        body = dict(version=1, dirs=['.'], files={}, bytes=0)
        return dict(body, digest=seal.identity(body))

    def stage_replacement(self, held=None, sha='b'*40):
        stage = self.root / 'var/lib/ameesh-release-bootstrap/swap-1'
        stage.mkdir(parents=True, mode=0o700)
        fill(stage / 'runtime', 'new')
        sources(stage / 'sources', 'new')
        (stage / 'runtime').chmod(0o700)
        value = dict(version=1, sha=sha, expires_at=int(self.now)+600, held_operation=held)
        (stage / 'replacement.json').write_text(json.dumps(value))
        (stage / 'replacement.json').chmod(0o600)
        self.swap_stage = stage
        stage.parent.chmod(0o700)

    def prepared_view(self):
        self.out = self.item.prepare()
        return self.ask(self.out['question'], swap.OPTIONS)

    def replace(self):
        view = self.prepared_view()
        self.sign(view, 'replace')
        return view, self.item.run(view['id'])

    def snapshot_old(self):
        return dict(pin=self.pin.read_bytes(), runtime=swap.runtime.inventory(self.base/'runtime')['digest'],
                    lib=swap.runtime.inventory(self.base/'lib')['digest'])


class ReplacementTest(Harness):
    def test_replacement_keeps_lock_old_pin_and_old_trees_and_leaves_a_measured_host(self):
        old = self.snapshot_old()
        view, outcome = self.replace()
        self.assertEqual(outcome['outcome'], 'replaced')
        new = json.loads(self.pin.read_bytes())
        self.assertEqual(new['sha'], 'b'*40)
        seal.verify(new, self.base, uid=os.geteuid())
        kept = self.root / 'opt/ameesh-release-retained/swap-1'
        self.assertEqual((kept/'runtime-seal.json').read_bytes(), old['pin'])
        self.assertEqual(stat.S_IMODE((kept/'runtime-seal.json').stat().st_mode), 0o600)
        self.assertEqual(swap.runtime.inventory(kept/'runtime')['digest'], old['runtime'])
        self.assertEqual(swap.runtime.inventory(kept/'lib')['digest'], old['lib'])
        now = self.lock.stat()
        self.assertEqual((now.st_ino, now.st_size), (self.lock_stat.st_ino, 0))
        self.assertEqual(new['trees']['store'], self.old_pin['trees']['store'])
        # A lost reply replays without any second effect.
        before = fixture.tree_state(self.base)
        self.assertEqual(self.item.run(view['id'])['outcome'], 'replaced')
        self.assertEqual(fixture.tree_state(self.base), before)

    def test_unsigned_or_other_scope_moves_nothing(self):
        old = self.snapshot_old()
        view = self.prepared_view()
        with self.assertRaises(ValueError):
            self.item.run(view['id'])
        other = self.ask('Approve something else', swap.OPTIONS)
        self.sign(other, 'replace')
        with self.assertRaises(ValueError):
            self.item.run(other['id'])
        self.assertEqual(self.snapshot_old(), old)
        self.assertFalse((self.root/'opt/ameesh-release-retained').exists())

    def test_running_handoff_refuses_before_any_move(self):
        old = self.snapshot_old()
        view = self.prepared_view()
        self.sign(view, 'replace')
        self.system.handoff_stopped = lambda: False
        with self.assertRaisesRegex(ValueError, 'stopped'):
            self.item.run(view['id'])
        self.assertEqual(self.snapshot_old(), old)

    def test_interpreter_inside_the_moved_tree_refuses(self):
        view = self.prepared_view()
        self.sign(view, 'replace')
        with mock.patch.object(swap.sys, 'prefix', str(self.base/'runtime')):
            with self.assertRaisesRegex(ValueError, 'staged runtime'):
                self.item.run(view['id'])

    def test_interruption_fails_closed_for_h8_then_resumes(self):
        old = self.snapshot_old()
        view = self.prepared_view()
        self.sign(view, 'replace')
        with mock.patch.object(swap.runtime, 'publish', side_effect=OSError('lost')):
            with self.assertRaises(OSError):
                self.item.run(view['id'])
        self.assertEqual(self.pin.read_bytes(), old['pin'])
        with self.assertRaises((seal.Refused, OSError)):
            seal.verify(self.old_pin, self.base, uid=os.geteuid())
        self.assertEqual(self.item.run(view['id'])['outcome'], 'replaced')
        seal.verify(json.loads(self.pin.read_bytes()), self.base, uid=os.geteuid())

    def test_drift_of_old_tree_refuses_at_preflight(self):
        view = self.prepared_view()
        self.sign(view, 'replace')
        self.pin.write_bytes(seal.canonical(dict(self.old_pin, sha='c'*40)))
        with self.assertRaisesRegex(ValueError, 'reviewed previous pin'):
            self.item.run(view['id'])

    def test_rollback_is_separately_signed_and_restores_exactly(self):
        old = self.snapshot_old()
        view, _ = self.replace()
        with self.assertRaises(ValueError):
            self.item.rollback(view['id'])
        text = self.item.rollback_text(int(self.now)+600)['question']
        other = self.ask(text, swap.ROLLBACK_OPTIONS)
        self.sign(other, 'rollback')
        self.assertEqual(self.item.rollback(other['id'])['outcome'], 'rolled-back')
        self.assertEqual(self.snapshot_old(), old)
        seal.verify(self.old_pin, self.base, uid=os.geteuid())
        self.assertEqual(self.lock.stat().st_ino, self.lock_stat.st_ino)
        self.assertTrue((self.root/'opt/ameesh-release-retained/swap-1/replaced-runtime').is_dir())
        with self.assertRaisesRegex(ValueError, 'rolled back'):
            self.item.run(view['id'])
        with self.assertRaises(ValueError):
            self.item.run(other['id'])

    def test_altered_bundle_and_unrelated_reservation_refuse(self):
        self.item.prepare()
        path = self.item.bundle_path
        value = json.loads(path.read_bytes()); value['retained'] = '/tmp/elsewhere'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'bundle changed|unavailable'):
            self.item.prepared()
        path.unlink()
        reservation = self.backend.journal()/'fixture-host.active.json'
        reservation.parent.mkdir(parents=True, exist_ok=True)
        reservation.write_text(json.dumps(dict(operation='unrelated', digest='d'*64))); reservation.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.item.prepare()

    def test_same_commit_refuses_and_question_stays_within_the_signed_limit(self):
        self.assertLessEqual(len(self.item.prepare()['question'].encode()), decisions.MAX_QUESTION)
        value = json.loads((self.swap_stage/'replacement.json').read_bytes())
        value['sha'] = self.old_pin['sha']
        (self.swap_stage/'replacement.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'sealed commit'):
            self.item.prepare()

    def test_bundle_binds_the_code_that_runs_it_and_only_the_staged_code_may_run(self):
        self.assertEqual(self.item.prepare()['bundle_digest'], json.loads(self.item.bundle_path.read_bytes())['digest'])
        self.backend.gestes.code = 'f'*64
        with self.assertRaisesRegex(ValueError, 'other code'):
            self.item.prepared()
        self.assertEqual(self.item.prepared(same_code=False)[0]['code_sha256'], 'e'*64)
        with self.assertRaisesRegex(ValueError, 'staged runtime and sources'):
            backend_mod.replacement_enrollment(self.enrollment, 'swap-1', 'f'*64)

    def test_every_signed_command_measures_the_stage_and_refuses_a_change(self):
        view = self.prepared_view()
        self.sign(view, 'replace')
        old = self.snapshot_old()
        extra = self.swap_stage/'sources/planted.py'
        extra.write_text('# planted after signature\n'); extra.chmod(0o644)
        for command in (lambda: self.item.plan(view['id']), lambda: self.item.run(view['id']),
                        lambda: self.item.rollback(view['id']),
                        lambda: self.item.rollback_text(int(self.now)+600), self.item.prepare):
            with self.assertRaisesRegex(ValueError, 'differ from the signed'):
                command()
        self.assertEqual(self.snapshot_old(), old)
        extra.unlink()
        self.assertEqual(self.item.run(view['id'])['outcome'], 'replaced')

    def test_a_stage_others_can_reach_is_refused(self):
        self.swap_stage.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'private root stage'):
            self.item.prepare()
        self.swap_stage.chmod(0o700)
        target = self.swap_stage/'sources/hote-release.py'
        target.chmod(0o664)
        with self.assertRaises(ValueError):
            self.item.prepare()

    def test_code_digest_loads_under_a_python_without_ameesh_and_matches_the_backend(self):
        # Ancestor ownership is real in a child process, so this stops at the
        # companions; the real build and comparison ran by hand (lot note).
        directory = self.swap_stage/'sources'
        code = Path(backend_mod.__file__).with_name('verifier-remplacement.py')
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
            'import importlib.util as u, sys\n'
            's = u.spec_from_file_location("v", sys.argv[1]); m = u.module_from_spec(s)\n'
            's.loader.exec_module(m); print(m.module("runtime-release").code_digest(sys.argv[2]))\n'
            'assert "ameesh" not in sys.modules',
            str(code), str(directory)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), backend_mod.code_digest(directory))

    def test_foreign_decision_leaves_no_trace_and_replay_reverifies(self):
        view, _ = self.replace()
        before = self.backend.gestes.entries()
        other = self.ask('Approve something else', swap.OPTIONS)
        self.sign(other, 'replace')
        with self.assertRaises(ValueError):
            self.item.run(other['id'])
        self.assertEqual(self.backend.gestes.entries(), before)
        extra = self.base/'runtime/bin'
        extra.chmod(0o755)
        with self.assertRaises(ValueError):
            (extra/'planted').write_text('x')
            self.item.run(view['id'])

    def test_missing_host_key_and_unsafe_held_name_refuse(self):
        with mock.patch.object(self.backend, 'host_key', return_value=None):
            with self.assertRaisesRegex(ValueError, 'host key'):
                self.item.prepare()
        value = json.loads((self.swap_stage/'replacement.json').read_bytes())
        value['held_operation'] = '../elsewhere'
        (self.swap_stage/'replacement.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'exact staged'):
            self.item.prepare()

    def test_handoff_stopped_reads_states_and_instances(self):
        installer = backend_mod.installer
        with mock.patch.object(installer.account.os, 'geteuid', return_value=0):
            host = installer.LinuxHost()
        for states, instances, expected in (
                ('ActiveState=inactive\n\nActiveState=failed\n', '', True),
                ('ActiveState=active\n\nActiveState=inactive\n', '', False),
                ('ActiveState=inactive\n', '', False),
                ('ActiveState=inactive\n\nActiveState=inactive\n', 'ameesh-handoff-local@1.service loaded\n', False)):
            with mock.patch.object(host, 'command', side_effect=[states, instances]):
                self.assertIs(host.handoff_stopped(), expected)

    def test_independent_check_compares_the_signed_values(self):
        check = backend_mod.module('verifier-remplacement')
        question = self.item.prepare()['question']
        bundle = json.loads(self.item.bundle_path.read_bytes())
        measured = dict(runtime=bundle['next']['runtime'], lib=bundle['next']['lib'],
                        code_sha256=bundle['code_sha256'])
        self.assertEqual(check.signed(question)['sha'], bundle['next']['sha'])
        self.assertEqual(check.differences(question, measured), [])
        self.assertEqual(check.differences(question, dict(measured, lib='0'*64)), ['lib'])

    def test_cli_parses_only_the_typed_commands(self):
        self.assertEqual(backend_mod.parse(['replace', 'swap-1', '--decision', '4']), ('replace', 'swap-1', 4))
        self.assertEqual(backend_mod.parse(['replacement-prepare', 'swap-1']), ('replacement-prepare', 'swap-1', None))
        self.assertEqual(backend_mod.parse(['replacement-rollback-question', 'swap-1', '--expires', '9']),
                         ('replacement-rollback-question', 'swap-1', 9))
        with self.assertRaises(backend_mod.BackendError):
            backend_mod.parse(['replace', 'swap-1'])
        with self.assertRaises(backend_mod.BackendError):
            backend_mod.parse(['replacement-prepare', 'swap-1', '--root', '/'])


class HeldContinuationTest(Harness):
    """A bootstrap held before enable keeps its journal; continuation needs the proof."""

    def setUp(self):
        super().setUp()
        self.old_stage = self.root/('var/lib/ameesh-release-bootstrap/'+HELD)
        self.old_stage.mkdir(mode=0o700)
        digests = dict(runtime_tree_sha256=self.old_pin['trees']['runtime']['digest'],
                       source_tree_sha256=self.old_pin['trees']['lib']['digest'])
        document = seal.canonical(self.old_pin).decode()
        self.old_bundle = bootstrap.delegation.render(dict(self.bundle['plan'], **digests),
            account_ids=dict(uid=987, gid=987), enrollment={bootstrap.delegation.SEAL_DOCUMENT: document})
        self.old_question = bootstrap.question(HELD, self.old_bundle, expires_at=int(self.now)+600)
        self.write(self.old_stage, self.old_bundle, self.old_question)
        self.old_view = self.ask(self.old_question, bootstrap.OPTIONS)
        self.sign(self.old_view, 'install')
        scopes = bootstrap.scopes(HELD, self.old_bundle)
        receipts = {step: dict(operation=HELD, step=step, action_id=HELD+':'+step, scope_digest=scopes[step],
                               bundle=self.old_bundle, ok=True) for step in bootstrap.STEPS[:6]}
        journal = self.backend.journal(); journal.mkdir(mode=0o700, parents=True, exist_ok=True)
        journal.parent.chmod(0o700); journal.chmod(0o700)
        bootstrap.protocol.save(journal/(HELD+'.json'),
            dict(bundle=self.old_bundle, intent='validate', receipts=receipts, complete=False))
        self.reservation = journal/'fixture-host.active.json'
        bootstrap.protocol.save(self.reservation, dict(operation=HELD, digest=self.old_bundle['digest']))
        self.backend.gestes.append(command='install', phase='start', operation=HELD,
                                   bundle_digest=self.old_bundle['digest'], decision=self.old_view['id'])
        self.old_bytes = (journal/(HELD+'.json')).read_bytes()
        value = json.loads((self.swap_stage/'replacement.json').read_bytes())
        value['held_operation'] = HELD
        (self.swap_stage/'replacement.json').write_text(json.dumps(value))

    @staticmethod
    def write(stage, bundle, question):
        for name, text in (('bundle.json', json.dumps(bundle, sort_keys=True)), ('question.txt', question)):
            (stage/name).write_text(text); (stage/name).chmod(0o600)

    def new_bundle(self):
        pin = self.pin.read_text()
        document = json.loads(pin)
        plan = dict(self.old_bundle['plan'], runtime_tree_sha256=document['trees']['runtime']['digest'],
                    source_tree_sha256=document['trees']['lib']['digest'],
                    local_transport=dict(controller_source='127.0.0.1/32', controller_key='ssh-ed25519 '
                        + base64.b64encode(base64.b64decode(
                            self.old_bundle['plan']['controller_key'].split()[1])[:-32]+b'z'*32).decode()))
        bundle = bootstrap.delegation.render(plan, account_ids=dict(uid=987, gid=987),
                                             enrollment={bootstrap.delegation.SEAL_DOCUMENT: pin})
        stage = self.root/'var/lib/ameesh-release-bootstrap/continued'
        stage.mkdir(mode=0o700, exist_ok=True)
        self.write(stage, bundle, bootstrap.question('continued', bundle, expires_at=int(self.now)+600))
        return bundle

    def reference(self):
        return continuation.reference(self.backend, 'continued', HELD, self.old_view['id'])

    def test_held_bootstrap_is_untouched_and_continuation_requires_the_signed_proof(self):
        reservation = self.reservation.read_bytes()
        view, _ = self.replace()
        self.assertEqual((self.backend.journal()/(HELD+'.json')).read_bytes(), self.old_bytes)
        self.assertEqual(self.reservation.read_bytes(), reservation)
        self.new_bundle()
        ref = self.reference()
        self.assertEqual(ref['replacement']['decision'], view['id'])
        self.assertEqual(ref['replacement']['operation'], 'swap-1')
        text = self.item.rollback_text(int(self.now)+600)['question']
        back = self.ask(text, swap.ROLLBACK_OPTIONS)
        self.sign(back, 'rollback')
        self.item.rollback(back['id'])
        with self.assertRaisesRegex(ValueError, 'completed signed replacement'):
            self.reference()

    def test_tree_change_without_replacement_refuses(self):
        self.pin.write_bytes(seal.canonical(dict(self.old_pin, trees=dict(self.old_pin['trees'],
            runtime=self.inventory_new()))))
        self.new_bundle()
        with self.assertRaisesRegex(ValueError, 'completed signed replacement'):
            self.reference()

    def inventory_new(self):
        return swap.runtime.inventory(self.swap_stage/'runtime')

    def test_other_seal_than_the_signed_replacement_refuses(self):
        self.replace()
        document = json.loads(self.pin.read_text())
        self.pin.write_bytes(seal.canonical(dict(document, sha='e'*40)))
        self.new_bundle()
        with self.assertRaisesRegex(ValueError, 'signed replacement'):
            self.reference()
