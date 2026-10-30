# SPDX-License-Identifier: AGPL-3.0-only
"""Private disposable documents and real owner proofs; host effects simulated."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import support
from tests import test_l232b_bootstrap_files as filesystem
from ameesh import authority, decisions, registry, signing, work

SPEC = importlib.util.spec_from_file_location('controller_enrollment',
    Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/enroler-controleur.py')
enroll = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(enroll)


class RealTransportKeyTest(unittest.TestCase):
    def test_real_generated_key_roundtrips_without_root_or_comment(self):
        # Only the trust of this disposable test directory is substituted.
        # Both production methods execute the real fixed ssh-keygen binary;
        # read() still checks ownership, mode, file type and link count.
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'identity'
            system=enroll.System()
            with mock.patch.object(enroll.runtime,'trusted'):
                system.generate_key(path)
                public=system.public_key(path)
            self.assertEqual(len(public.split()),2)
            self.assertTrue(public.startswith('ssh-ed25519 '))
            self.assertEqual(path.with_name('identity.pub').read_text().strip(),public)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(path.with_name('identity.pub').stat().st_mode),0o600)


class ProbeIsolationTest(unittest.TestCase):
    def test_runuser_failure_cannot_prove_unreadability(self):
        import subprocess
        system = enroll.System()
        with mock.patch.object(enroll.runtime, 'trusted'), mock.patch('subprocess.run') as run:
            for code, out, err in ((0, b'', b''), (1, b'', b'runuser: user missing'),
                                   (2, b'', b''), (1, b'unexpected', b'')):
                run.return_value = subprocess.CompletedProcess([], code, out, err)
                with self.subTest(code=code, err=err), self.assertRaises(enroll.EnrollmentError):
                    system.probe_key(Path('/private/identity'), 'missing-executor')
            run.return_value = subprocess.CompletedProcess([], 1, b'', b'')
            system.probe_key(Path('/private/identity'), 'executor')


class System:
    def __init__(self):
        self.active = False
        self.invocation = 'b' * 32
        self.calls = []
        self.admission = True
        self.lost_start = False
        self.probes = []

    def generate_key(self, path):
        path.write_text('synthetic-private-key')
        path.chmod(0o600)
        path.with_name(path.name + '.pub').write_text(self.public_key(path))
        path.with_name(path.name + '.pub').chmod(0o600)

    def public_key(self, path):
        enroll.read(path)
        return 'ssh-ed25519 synthetic-public-key'

    def probe_key(self, path, account):
        self.probes.append((path, account))
        if path.exists() and (path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077):
            raise enroll.EnrollmentError('transport key isolation probe refused')

    def state(self):
        return dict(LoadState='loaded', ActiveState='active' if self.active else 'inactive',
                    MainPID='123' if self.active else '0', InvocationID=self.invocation)

    def command(self, action):
        self.calls.append(action)
        if action == 'start':
            self.active = True
            if self.lost_start:
                self.lost_start = False
                raise OSError('synthetic lost start reply')
        elif action == 'stop':
            self.active = False

    def admit(self):
        if not self.admission:
            raise ValueError('synthetic admission failure')

    def ready(self, bundle):
        return self.active


class EnrollmentTest(unittest.TestCase):
    def setUp(self):
        base = filesystem.BootstrapFilesTest()
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.root = base.root
        self.operation = 'fixture-enrollment'
        self.stage = self.root / enroll.STAGE.lstrip('/') / self.operation
        self.stage.mkdir(mode=0o700, parents=True)
        self.policy = dict(host='fixture-host', units=['ameesh-runner@probe.service'],
                           manager={'kind': 'system'}, restart_seconds=30,
                           controller_unit='ameesh-release-controller.service')
        self.pins = {}
        for name, field in (('runtime', 'runtime_tree_sha256'), ('lib', 'source_tree_sha256')):
            path = self.root / 'opt/ameesh-release' / name
            path.mkdir(parents=True)
            (path / 'fixture').write_text('synthetic immutable closure')
            (path / 'fixture').chmod(0o444)
            path.chmod(0o555)
            self.pins[field] = enroll.runtime.inventory(path)['digest']
        def writable():
            for path in self.root.rglob('*'):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
        self.addCleanup(writable)
        self.path(enroll.UNIT).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path(enroll.UNIT).write_text(enroll.unit.UNIT)
        self.path(enroll.UNIT).chmod(0o644)
        self.path(enroll.CONFIG).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path('/var/lib/ameesh-release').mkdir(mode=0o700, parents=True, exist_ok=True)
        self.write(self.path(enroll.CONFIG + '/turn.json'), dict(agent='probe',
            unit='ameesh-runner@probe.service', account='executor', home='/home/executor', seconds=30))
        self.path(enroll.LOCK).write_text('')
        self.path(enroll.LOCK).chmod(0o600)
        self.lock_inode = self.path(enroll.LOCK).stat().st_ino
        (self.stage / 'transport').mkdir(mode=0o700)
        secret = self.stage / 'transport/identity'
        secret.write_text('synthetic-private-key')
        secret.chmod(0o600)
        known = self.stage / 'known_hosts'
        known.write_text('synthetic-known-host')
        known.chmod(0o600)
        self.data = dict(
            enrollment=dict(host='fixture-host', machine_id='a' * 32, policy=self.policy,
                            database='fixture-db'),
            database=dict(backend='postgres', schema='public',
                          dsn='postgresql://controller:synthetic-secret@localhost/fixture-db'),
            service=self.pins,
            card=dict(units=self.policy['units'], properties={}, guard=None, seconds=30),
            destinations=dict(config='/etc/ameesh/fixture.json'),
            transport=dict(host='fixture-host', address='192.0.2.1', port=22,
                           user='ameesh-release', host_key_alias='fixture-host',
                           known_hosts=str(known), known_hosts_sha256=enroll.sha(known.read_bytes()),
                           identity_file=str(secret), seconds=30),
            backup=dict(dsn='postgresql://backup@localhost/fixture-db',
                        restore_dsn='postgresql://restore@localhost/fixture-restore',
                        retention_seconds=3600, restore_capacity='/etc/ameesh-release/capacity.json',
                        remote=None))
        self.write(self.stage / 'input.json', dict(version=1, expires_at=int(time.time()) + 600))
        for name, value in self.data.items():
            self.write(self.stage / (name + '.json'), value)
        def typed(operation):
            if enroll.re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', operation) is None:
                raise ValueError('typed operation required')
        self.backend = SimpleNamespace(path=self.path, stage=typed, db=None,
            enrollment=dict(host='fixture-host'), machine=lambda: 'a' * 32,
            host_key=lambda: 'f' * 64, gestes=SimpleNamespace(code='e' * 64))
        self.system = System()
        public = self.system.public_key(secret)
        self.write(self.stage / 'keygen.json', dict(status='complete', public_key=public,
                   public_key_sha256=enroll.sha(public.encode())))
        self.engine = enroll.Enrollment(self.backend, system=self.system)
        # The libpq parser has its own integration tests. Filesystem tests can
        # run under psql too, with deterministic synthetic connection identities.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(enroll.backup, 'database_target',
                          side_effect=lambda dsn: dsn.rsplit('/', 1)[-1] if isinstance(dsn, str) else None).start()
        self.allow = mock.Mock()
        self.allow.validate.return_value = True
        mock.patch.object(enroll, 'SignedEnrollmentAuthority', return_value=self.allow).start()
        mock.patch.object(enroll, 'SignedStopAuthority', return_value=self.allow).start()
        mock.patch.object(enroll, 'SignedKeyAuthority', return_value=self.allow).start()
        mock.patch.object(enroll.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1001)).start()

    def path(self, logical):
        return self.root / logical.lstrip('/')

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def prepare(self):
        return self.engine.prepare(self.operation)

    def apply(self, **kwargs):
        return self.engine.apply(self.operation, 123, **kwargs)

    def test_exact_private_publication_real_modes_and_separate_stop(self):
        prepared = self.prepare()
        self.assertNotIn('synthetic-secret', json.dumps(prepared))
        self.assertFalse(self.system.active)
        self.assertEqual(self.apply()['outcome'], 'ready')
        for name in enroll.FILES:
            path = self.path(enroll.CONFIG + '/' + name + '.json')
            self.assertEqual(json.loads(path.read_bytes()), self.data[name])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(self.path('/var/lib/ameesh-release/controller/readiness.json').exists())
        # Only the real service may write readiness; fake readiness is a test
        # system response, never fabricated on disk by enrollment.
        self.assertEqual(self.system.calls, ['daemon-reload', 'start'])
        self.assertEqual(self.apply()['outcome'], 'ready')
        self.assertEqual(self.system.calls.count('start'), 1)
        self.assertEqual(self.apply(stop=True)['outcome'], 'stopped')
        self.assertTrue(self.path(enroll.CONFIG + '/database.json').exists())
        self.assertEqual(self.path(enroll.LOCK).stat().st_ino, self.lock_inode)

    def test_signing_options_and_named_oversized_question(self):
        prepared = self.prepare()
        self.assertEqual(prepared['options'], enroll.OPTIONS)
        oversized = dict(prepared['bundle'], padding='x' * 4000)
        with self.assertRaisesRegex(enroll.EnrollmentError, 'decision limit'):
            enroll.question(self.operation, oversized, oversized['expires_at'])

    def test_stop_uses_a_new_window_without_renewing_enrollment(self):
        prepared = self.prepare()
        self.apply()
        old_expiry = prepared['bundle']['expires_at']
        self.engine.clock = lambda: old_expiry + 1
        with self.assertRaisesRegex(enroll.EnrollmentError, 'unexpired'):
            self.apply()
        fresh = old_expiry + 600
        text = enroll.question(self.operation, prepared['bundle'], fresh, stop=True)
        scope = json.loads(text[len(enroll.STOP_PREFIX):])
        self.assertEqual(scope['enrollment_expires_at'], old_expiry)
        self.assertEqual(scope['expires_at'], fresh)
        self.assertEqual(self.apply(stop=True)['outcome'], 'stopped')
        self.assertEqual(json.loads((self.stage / 'bundle.json').read_text()), prepared['bundle'])

    def test_missing_signature_has_no_effect_and_drift_refuses_before_start(self):
        self.prepare()
        self.allow.validate.return_value = False
        with self.assertRaisesRegex(enroll.EnrollmentError, 'authority'):
            self.apply()
        self.assertEqual(self.system.calls, [])
        self.assertFalse(self.path(enroll.CONFIG + '/enrollment.json').exists())
        self.allow.validate.return_value = True
        self.write(self.stage / 'card.json', dict(self.data['card'], seconds=31))
        with self.assertRaisesRegex(enroll.EnrollmentError, 'frozen'):
            self.apply()
        self.assertEqual(self.system.calls, [])

    def test_existing_documents_symlinks_and_hardlinks_are_refused(self):
        target = self.path(enroll.CONFIG + '/database.json')
        self.write(target, self.data['database'])
        with self.assertRaisesRegex(enroll.EnrollmentError, 'overwritten'):
            self.prepare()
        target.unlink()
        source = self.stage / 'database.json'
        original = source.read_bytes()
        source.unlink()
        source.symlink_to(self.stage / 'card.json')
        with self.assertRaises(ValueError):
            self.prepare()
        source.unlink()
        source.write_bytes(original)
        source.chmod(0o600)
        os.link(source, self.stage / 'extra')
        with self.assertRaisesRegex(enroll.EnrollmentError, 'regular'):
            self.prepare()

    def test_lost_publication_reply_is_reconciled_without_rewriting_file(self):
        self.prepare()
        write = enroll.admin.Backend.write
        lost = []
        def interrupted(path, text):
            write(path, text)
            if path.name == 'database.json' and not lost:
                lost.append(path.stat().st_ino)
                raise OSError('synthetic lost publication reply')
        with mock.patch.object(enroll.admin.Backend, 'write', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.apply()['outcome'], 'ready')
        self.assertEqual(self.path(enroll.CONFIG + '/database.json').stat().st_ino, lost[0])

    def test_lost_start_reply_is_observed_without_restarting(self):
        self.prepare()
        self.system.lost_start = True
        with self.assertRaises(OSError):
            self.apply()
        self.assertEqual(self.apply()['outcome'], 'ready')
        self.assertEqual(self.system.calls.count('start'), 1)

    def test_incomplete_factory_and_queue_never_start_service(self):
        self.prepare()
        self.system.admission = False
        with self.assertRaisesRegex(ValueError, 'admission'):
            self.apply()
        self.assertEqual(self.system.calls, [])
        self.system.admission = True
        queued = self.path(enroll.CONFIG + '/operations/unexpected.json')
        self.write(queued, {})
        with self.assertRaisesRegex(enroll.EnrollmentError, 'queue'):
            self.apply()
        self.assertEqual(self.system.calls, [])

    def test_completed_receipt_never_blesses_private_drift_or_another_invocation(self):
        self.prepare()
        self.apply()
        self.system.invocation = 'c' * 32
        with self.assertRaisesRegex(enroll.EnrollmentError, 'instance'):
            self.apply(stop=True)
        self.system.invocation = 'b' * 32
        self.write(self.path(enroll.CONFIG + '/database.json'), dict(self.data['database'], schema='other'))
        with self.assertRaisesRegex(enroll.EnrollmentError, 'differs'):
            self.apply()
        self.assertNotIn('stop', self.system.calls)

    def test_wrong_host_and_probe_policy_are_refused(self):
        self.write(self.stage / 'enrollment.json', dict(self.data['enrollment'], machine_id='c' * 32))
        with self.assertRaisesRegex(enroll.EnrollmentError, 'binding'):
            self.prepare()
        changed = dict(self.policy, units=['ameesh-runner@other.service'])
        self.write(self.stage / 'enrollment.json', dict(self.data['enrollment'], policy=changed))
        self.write(self.stage / 'card.json', dict(self.data['card'], units=changed['units']))
        with self.assertRaisesRegex(enroll.EnrollmentError, 'probe'):
            self.prepare()

    def test_private_errors_and_invalid_cli_never_expose_documents(self):
        with mock.patch.object(enroll.os, 'geteuid', return_value=0), \
                mock.patch.object(enroll.sys, 'stderr') as output:
            self.assertEqual(enroll.main(['enroll', '../unsafe', '--decision', 'bad']), 2)
        text = ''.join(call.args[0] for call in output.write.call_args_list if call.args)
        self.assertNotIn('synthetic-secret', text)
        self.assertIn('OPERATION [--decision N] required', text)

    def test_separately_signed_fresh_key_is_never_regenerated_or_exported(self):
        path = self.stage / 'transport/identity'
        path.unlink()
        path.parent.rmdir()
        (self.stage / 'keygen.json').unlink()
        self.allow.validate.return_value = False
        with self.assertRaisesRegex(enroll.EnrollmentError, 'authority'):
            self.engine.keygen(self.operation, 122)
        self.assertFalse(path.exists())
        self.allow.validate.return_value = True
        result = self.engine.keygen(self.operation, 122)
        self.assertNotIn('synthetic-private-key', json.dumps(result))
        inode = path.stat().st_ino
        self.assertEqual(self.engine.keygen(self.operation, 122), result)
        self.assertEqual(path.stat().st_ino, inode)
        self.assertTrue(self.system.probes)
        self.assertEqual(self.prepare()['bundle']['transport_key'], result)

    def test_failed_keygen_probes_isolation_and_never_retries_uncertain_artifacts(self):
        path = self.stage / 'transport/identity'
        path.unlink()
        path.parent.rmdir()
        (self.stage / 'keygen.json').unlink()
        generate = self.system.generate_key
        def lost(path):
            generate(path)
            raise OSError('synthetic lost key generation reply')
        with mock.patch.object(self.system, 'generate_key', side_effect=lost):
            with self.assertRaises(OSError):
                self.engine.keygen(self.operation, 122)
        self.assertTrue(self.system.probes)
        with self.assertRaisesRegex(enroll.EnrollmentError, 'uncertain'):
            self.engine.keygen(self.operation, 122)
        self.assertTrue(path.exists())

    def test_native_permission_probe_requires_an_actual_readability_refusal(self):
        import subprocess
        for code in (0, 1, 2):
            with self.subTest(code=code), \
                    mock.patch.object(enroll.runtime, 'trusted'), \
                    mock.patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=code, stdout=b'', stderr=b'')) as run:
                if code == 1:
                    enroll.System().probe_key(Path('/private/identity'), 'executor')
                else:
                    with self.assertRaises(enroll.EnrollmentError):
                        enroll.System().probe_key(Path('/private/identity'), 'executor')
                self.assertEqual(run.call_args.args[0], [
                    '/usr/sbin/runuser', '--user', 'executor', '--',
                    '/usr/bin/test', '-r', '/private/identity'])


class SignedAuthorityTest(support.PgTestCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.bundle = dict(operation='fixture-enrollment', expires_at=int(self.now) + 600,
                           host='fixture-host', files={'/etc/ameesh-release/database.json': 'a' * 64})
        self.seed = b'\x42' * 32
        registry.upsert(self.db, 'fixture-owner', harness='other', host=self.cfg.host)
        authority.register_key(self.db, 'fixture-owner', signing.public_from_seed(self.seed), role='owner')
        registry.upsert(self.db, 'fixture-controller', harness='other', host=self.cfg.host)
        self.lot = work.add(self.db, title='Synthetic independent controller enrollment',
                            assignee='fixture-controller')

    def ask(self, stop=False):
        return decisions.ask(self.cfg, self.db, requester='fixture-controller', lot=str(self.lot['id']),
            question=enroll.question('fixture-enrollment', self.bundle, self.bundle['expires_at'], stop=stop),
            options=['%s=%s' % (value['key'], value['label'])
                     for value in (enroll.STOP_OPTIONS if stop else enroll.OPTIONS)], needs_signature=True)

    def sign(self, view, text):
        digest = decisions.answer_digest(view, text, decisions.match_option(view, text), 'human:fixture-owner')
        authority.create_approval(self.db, self.seed, approver='fixture-owner',
            action=decisions.APPROVAL_ACTION, artifact_kind=decisions.APPROVAL_KIND,
            artifact_hash=digest, ttl=self.now + 900 - time.time())
        decisions.answer(self.cfg, self.db, view['id'], text,
                         human='human:fixture-owner', channel='cli', signed=True)

    def adapter(self, view, stop=False, bundle=None):
        cls = enroll.SignedStopAuthority if stop else enroll.SignedEnrollmentAuthority
        return cls(self.db, view['id'], 'fixture-enrollment', bundle or self.bundle, clock=lambda: self.now)

    def test_live_signature_exact_payload_revocation_and_separate_stop(self):
        view = self.ask()
        adapter = self.adapter(view)
        digest = enroll.protocol.identity(self.bundle)
        self.assertFalse(adapter.validate('fixture-enrollment', digest, 'start'))

        self.sign(view, 'enroll')
        self.assertTrue(adapter.validate('fixture-enrollment', digest, 'start'))
        self.assertFalse(adapter.validate('fixture-enrollment', digest, 'stop'))
        self.assertFalse(self.adapter(view, stop=True).validate('fixture-enrollment', digest, 'stop'))
        changed = dict(self.bundle, host='another-host')
        self.assertFalse(self.adapter(view, bundle=changed).validate(
            'fixture-enrollment', enroll.protocol.identity(changed), 'start'))
        stop = self.ask(stop=True)
        self.sign(stop, 'stop')
        self.assertTrue(self.adapter(stop, stop=True).validate('fixture-enrollment', digest, 'stop'))
        self.now += 601
        self.assertFalse(adapter.validate('fixture-enrollment', digest, 'start'))
        self.now -= 601
        authority.revoke_key(self.db, 'fixture-owner')
        self.assertFalse(adapter.validate('fixture-enrollment', digest, 'start'))

    def test_fresh_signed_stop_after_original_enrollment_expired(self):
        self.bundle['expires_at'] = int(self.now) - 1
        fresh = int(self.now) + 300
        view = decisions.ask(self.cfg, self.db, requester='fixture-controller', lot=str(self.lot['id']),
            question=enroll.question('fixture-enrollment', self.bundle, fresh, stop=True),
            options=['%s=%s' % (value['key'], value['label']) for value in enroll.STOP_OPTIONS],
            needs_signature=True)
        self.sign(view, 'stop')
        digest = enroll.protocol.identity(self.bundle)
        adapter = self.adapter(view, stop=True)
        self.assertTrue(adapter.validate('fixture-enrollment', digest, 'stop'))
        self.assertFalse(self.adapter(view).validate('fixture-enrollment', digest, 'start'))
        self.now = fresh + 1
        self.assertFalse(adapter.validate('fixture-enrollment', digest, 'stop'))

    def test_key_generation_signature_does_not_authorize_enrollment(self):
        view = decisions.ask(self.cfg, self.db, requester='fixture-controller', lot=str(self.lot['id']),
            question=enroll.KEY_PREFIX + enroll.canonical(self.bundle),
            options=['%s=%s' % (value['key'], value['label']) for value in enroll.KEY_OPTIONS],
            needs_signature=True)
        self.sign(view, 'keygen')
        adapter = enroll.SignedKeyAuthority(self.db, view['id'], 'fixture-enrollment',
                                          self.bundle, clock=lambda: self.now)
        digest = enroll.protocol.identity(self.bundle)
        self.assertTrue(adapter.validate('fixture-enrollment', digest, 'keygen'))
        self.assertFalse(self.adapter(view).validate('fixture-enrollment', digest, 'start'))
