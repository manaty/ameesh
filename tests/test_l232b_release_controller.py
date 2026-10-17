# SPDX-License-Identifier: AGPL-3.0-only
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from tests import support

PATH = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/controleur-release.py'
SPEC = importlib.util.spec_from_file_location('release_controller', PATH)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class Authority:
    def __init__(self):
        self.denied = set()
        self.calls = []

    def validate(self, operation, digest, step):
        self.calls.append((operation, digest, step))
        return step not in self.denied


class Host:
    """Fake enrolled host; effects deduplicated before returning a receipt."""
    def __init__(self):
        self.calls = []
        self.effects = []
        self.receipts = {}
        self.fail = set()
        self.lost = set()
        self.has_fence = False
        self.before = None
        self.current = 'old-code-and-venv'
        self.config = 'old-unit-snapshot'

    def fenced(self, request):
        return self.has_fence

    def perform(self, step, request):
        self.calls.append(step)
        if self.before:
            self.before(step)
        action = request['action_id']
        if action not in self.receipts:
            ok = step not in self.fail
            if ok:
                self.effects.append(step)
                if step in ('fence', 'recovery_fence'):
                    self.has_fence = True
                if step == 'configure':
                    self.config = 'new-unit-snapshot'
                if step == 'activate':
                    self.current = request['manifest']['release']
                if step == 'rollback':
                    self.current = 'old-code-and-venv'
                    self.config = 'old-unit-snapshot'
                if step in ('unfence', 'recovery_unfence'):
                    self.has_fence = False
            self.receipts[action] = dict(copy.deepcopy(request), ok=ok)
        if step in self.lost:
            self.lost.remove(step)
            raise OSError('SYNTHETIC-SECRET must not escape')
        return copy.deepcopy(self.receipts[action])


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ameesh-controller-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.manifest = dict(host='test-host', commit='a' * 40,
                             release='a' * 40 + '-' + 'b' * 64,
                             database='mesh-test', units=['ameesh@test.service'],
                             policy='approved-test-policy', rollback_until=200)
        self.authority = Authority()
        self.host = Host()

    def run_release(self, operation='release-1', manifest=None):
        return controller.run(self.root, operation, manifest or self.manifest,
                              authority=self.authority, adapter=self.host)

    def record(self):
        return json.loads((self.root / 'release-1.json').read_text())

    def test_success_orders_backup_and_pin_before_additive_migration(self):
        self.assertEqual(self.run_release(), 'accepted')
        self.assertEqual(self.host.effects, list(controller.STEPS))
        self.assertEqual(self.host.current, self.manifest['release'])
        self.assertFalse(self.host.has_fence)
        self.assertEqual(self.run_release(), 'accepted')
        self.assertEqual(self.host.effects, list(controller.STEPS))
        self.assertEqual((self.root / 'release-1.json').stat().st_mode & 0o777, 0o600)

    def test_failed_backup_never_fences_or_migrates(self):
        self.host.fail.add('backup')
        with self.assertRaisesRegex(controller.ControllerError, 'held'):
            self.run_release()
        self.assertEqual(self.host.effects, ['prepare'])
        self.assertEqual(self.host.current, 'old-code-and-venv')

    def test_health_failure_restores_code_venv_and_configuration(self):
        self.host.fail.add('health')
        self.assertEqual(self.run_release(), 'rolled_back')
        self.assertEqual(self.host.effects[-5:], list(controller.RECOVERY))
        self.assertEqual(self.host.current, 'old-code-and-venv')
        self.assertEqual(self.host.config, 'old-unit-snapshot')
        self.assertNotIn('accept', self.host.effects)
        self.assertFalse(self.host.has_fence)

    def test_rollback_failure_keeps_fence_and_host_reservation(self):
        self.host.fail.update(('health', 'rollback'))
        with self.assertRaisesRegex(controller.ControllerError, 'held'):
            self.run_release()
        self.assertTrue(self.host.has_fence)
        self.assertTrue((self.root / 'test-host.active.json').exists())
        with self.assertRaisesRegex(controller.ControllerError, 'unresolved'):
            self.run_release('release-2')
        self.assertNotIn('recovery_unfence', self.host.effects)

    def test_lost_receipt_resumes_every_action_without_repeating_effect(self):
        for step in controller.STEPS:
            with self.subTest(step=step), tempfile.TemporaryDirectory(prefix='ameesh-controller-replay-') as directory:
                self.root = Path(directory).resolve()
                self.host = Host()
                self.host.lost.add(step)
                with self.assertRaisesRegex(controller.ControllerError, '^action outcome uncertain; reconcile intent$'):
                    self.run_release()
                self.assertEqual(self.record()['intent'], step)
                self.assertNotIn('SYNTHETIC-SECRET', (self.root / 'release-1.json').read_text())
                self.assertEqual(self.run_release(), 'accepted')
                self.assertEqual(self.host.effects, list(controller.STEPS))
                self.assertEqual(self.host.calls.count(step), 2)

    def test_changed_manifest_and_second_operation_cannot_take_reserved_host(self):
        self.host.lost.add('prepare')
        with self.assertRaises(controller.ControllerError):
            self.run_release()
        changed = dict(self.manifest, commit='c' * 40, release='c' * 40 + '-' + 'b' * 64)
        for operation, manifest in [('release-1', changed), ('release-2', self.manifest)]:
            with self.assertRaisesRegex(controller.ControllerError, 'unresolved'):
                self.run_release(operation, manifest)
        self.assertEqual(self.host.effects, ['prepare'])

    def test_changed_manifest_or_foreign_journal_leaves_no_orphan_reservation(self):
        self.assertEqual(self.run_release(), 'accepted')
        changed = dict(self.manifest, commit='c' * 40, release='c' * 40 + '-' + 'b' * 64)
        with self.assertRaisesRegex(controller.ControllerError, 'manifest changed'):
            self.run_release('release-1', changed)
        # The refusal happened before the host reservation: the next operation
        # is not blocked by a reservation nobody owns.
        self.assertFalse((self.root / 'test-host.active.json').exists())
        self.assertEqual(self.run_release('release-2'), 'accepted')

    def test_reserved_readiness_operation_never_replaces_the_service_document(self):
        readiness = self.root / 'readiness.json'
        readiness.write_text('{"ready": true}')
        with self.assertRaisesRegex(controller.ControllerError, 'reserved operation'):
            self.run_release('readiness')
        self.assertEqual(readiness.read_text(), '{"ready": true}')
        self.assertFalse((self.root / 'test-host.active.json').exists())
        self.assertEqual(self.run_release('release-2'), 'accepted')

    def test_revocation_between_actions_blocks_before_migration(self):
        self.host.before = lambda step: self.authority.denied.add('migrate') if step == 'fence' else None
        with self.assertRaisesRegex(controller.ControllerError, 'authority denied'):
            self.run_release()
        self.assertTrue(self.host.has_fence)
        self.assertNotIn('migrate', self.host.calls)

    def test_missing_live_fence_blocks_switch_even_with_recorded_fence(self):
        self.host.lost.add('activate')
        with self.assertRaises(controller.ControllerError):
            self.run_release()
        self.host.has_fence = False
        with self.assertRaisesRegex(controller.ControllerError, 'live drained'):
            self.run_release()
        self.assertEqual(self.host.calls.count('activate'), 1)

    def test_unsafe_receipt_is_not_journaled(self):
        perform = self.host.perform
        def wrong(step, request):
            return dict(perform(step, request), unexpected='SYNTHETIC-SECRET')
        self.host.perform = wrong
        with self.assertRaisesRegex(controller.ControllerError, 'receipt mismatch'):
            self.run_release()
        self.assertEqual(self.record()['receipts'], {})
        self.assertNotIn('SYNTHETIC-SECRET', (self.root / 'release-1.json').read_text())

    def test_verification_exceptions_do_not_expose_provider_text(self):
        def secret_failure(*args):
            raise RuntimeError('SYNTHETIC-SECRET')
        self.authority.validate = secret_failure
        with self.assertRaisesRegex(controller.ControllerError, '^authority or fence verification unavailable$'):
            self.run_release()
        self.authority = Authority()
        self.host.fenced = secret_failure
        with self.assertRaisesRegex(controller.ControllerError, '^authority or fence verification unavailable$'):
            self.run_release()
        self.assertNotIn('SYNTHETIC-SECRET', (self.root / 'release-1.json').read_text())

    def test_denied_enrollment_does_not_reserve_host(self):
        self.authority.denied.add('prepare')
        with self.assertRaisesRegex(controller.ControllerError, 'authority denied'):
            self.run_release()
        self.assertFalse((self.root / 'test-host.active.json').exists())
        self.assertEqual(self.host.calls, [])

    def test_recovery_lost_receipts_resume_without_second_rollback(self):
        for step in controller.RECOVERY:
            with self.subTest(step=step), tempfile.TemporaryDirectory(prefix='ameesh-recovery-replay-') as directory:
                self.root = Path(directory).resolve()
                self.host = Host()
                self.host.fail.add('health')
                self.host.lost.add(step)
                with self.assertRaisesRegex(controller.ControllerError, 'uncertain'):
                    self.run_release()
                self.assertEqual(self.run_release(), 'rolled_back')
                self.assertEqual(self.host.effects[-5:], list(controller.RECOVERY))
                self.assertEqual(self.host.effects.count('rollback'), 1)
                self.assertEqual(self.host.current, 'old-code-and-venv')

    def test_two_controllers_cannot_interleave_host_changes(self):
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        errors = []
        self.host.fail.add('backup')
        def wait(step):
            if step == 'prepare':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test deadline')
        self.host.before = wait
        def worker(operation):
            if operation == 'release-2':
                started.set()
            try:
                self.run_release(operation)
            except Exception as error:
                errors.append(str(error))
        first = threading.Thread(target=worker, args=('release-1',))
        second = threading.Thread(target=worker, args=('release-2',))
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            second.start()
            self.assertTrue(started.wait(5))
        finally:
            release.set()
            first.join(5)
            if second.ident:
                second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(self.host.calls, ['prepare', 'backup'])
        self.assertEqual(set(errors), {'action failed; release held',
                                      'host has another unresolved release'})

    def test_public_or_linked_journal_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(controller.ControllerError, 'private'):
            self.run_release()
        self.root.chmod(0o700)
        (self.root / 'release-1.json').symlink_to(self.root / 'elsewhere')
        with self.assertRaises(OSError):
            self.run_release()


if __name__ == '__main__':
    unittest.main()
