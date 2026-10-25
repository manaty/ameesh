# SPDX-License-Identifier: AGPL-3.0-only
"""H6: operator authorization, single-step dispatch and private output."""
import contextlib
import functools
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import handoff_factory, handoffs, host_lifecycle, mesh_cli, storage, work


class OperatorTest(unittest.TestCase):
    def setUp(self):
        self.db = mock.Mock()
        self.api = mock.Mock()
        self.api.handoffs.get.return_value = {'id': 'op', 'plan': {'lifecycle_host': 'source'}}
        self.binding = SimpleNamespace(adapter=object(), requirements_for=object())
        self.loader = SimpleNamespace(load=mock.Mock(return_value=self.binding))
        self.result = {'id': 'op', 'state': 'held', 'evidence': {'token': 'SECRET'},
                       'plan': {'secret_refs': ['SECRET']}, 'error': 'SECRET'}

    def invoke(self, command, *options):
        args = mesh_cli.build_parser().parse_args(['handoff', command, 'op', *options])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mesh_cli._handoff_operator(object(), self.db, args)
        return code, out.getvalue()

    @contextlib.contextmanager
    def controller(self):
        """Run the real controller and contract with storage and host doubles."""
        operation = {'id': 'op', 'agent': 'dev', 'source': 'source', 'target': 'target',
                     'state': 'held', 'reserved_until_ts': 0,
                     'plan': {'repository': 'git@forge:org/app.git',
                              'owner': 'runner', 'epoch': 1},
                     'evidence': {'held_at': 'prepared', 'controller_epoch': 1}}
        self.api.handoffs.get.return_value = operation
        self.api.handoffs.db = self.db
        self.api.handoffs.controller.return_value = contextlib.nullcontext(self.db)
        self.api.handoffs.cancellation_guard.return_value = contextlib.nullcontext(
            self.api.handoffs)
        self.api.handoffs.begin_effect.return_value = operation
        self.api.handoffs.promote_loss.return_value = None
        self.api.host_lifecycle.get.return_value = None
        self.binding.adapter = mock.Mock()
        self.binding.adapter.revalidate.return_value = True
        with mock.patch.object(work, 'corrector'), \
                mock.patch.object(storage, 'of', return_value=self.api), \
                mock.patch.object(handoff_factory, 'load', return_value=self.binding):
            yield operation

    def test_expired_retry_names_explicit_renewal_without_resuming(self):
        with self.controller():
            with self.assertRaisesRegex(ValueError, 'renewal_required:.*--renew-ttl'):
                self.invoke('retry')
        self.binding.adapter.revalidate.assert_called_once()
        self.api.handoffs.renew.assert_not_called()
        self.api.handoffs.transition.assert_not_called()

    def test_cancel_after_switch_or_source_loss_names_refusal_without_restoring(self):
        for state, reason, control, category in (
                ('switched', None, None, 'destination_switched'),
                ('prepared', 'host_loss', None, 'source_lost'),
                ('prepared', None, {'mode': 'lost'}, 'source_lost')):
            with self.subTest(category=category, control=control), self.controller() as op:
                op['evidence']['held_at'] = state
                op['plan']['reason'] = reason
                self.api.host_lifecycle.get.return_value = control
                with self.assertRaisesRegex(ValueError, category + ':'):
                    self.invoke('cancel')
                self.binding.adapter.restore_source.assert_not_called()
                self.api.handoffs.unfence.assert_not_called()

    def test_helper_cannot_publish_a_controller_refusal(self):
        with self.controller():
            self.binding.adapter.revalidate.side_effect = handoffs.HandoffRefusal(
                handoffs.Refusal.RENEWAL_REQUIRED)
            with self.assertRaisesRegex(ValueError, '^handoff step refused; inspect') as caught:
                self.invoke('retry')
        self.binding.adapter.revalidate.assert_called_once()
        self.assertNotIn('renewal_required', str(caught.exception))
        self.api.handoffs.transition.assert_not_called()

    def test_public_category_does_not_render_exception_text(self):
        error = handoffs.HandoffRefusal(handoffs.Refusal.RENEWAL_REQUIRED)
        error.args = ('SECRET',)
        with self.controller(), mock.patch.object(handoffs, 'retry', side_effect=error):
            with self.assertRaisesRegex(ValueError, 'renewal_required:.*--renew-ttl') as caught:
                self.invoke('retry')
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_unknown_category_is_filtered(self):
        error = handoffs.HandoffRefusal(handoffs.Refusal.RENEWAL_REQUIRED)
        error.reason = SimpleNamespace(name='SECRET', value='SECRET')
        with self.controller(), mock.patch.object(handoffs, 'retry', side_effect=error):
            with self.assertRaisesRegex(ValueError, '^handoff step refused; inspect') as caught:
                self.invoke('retry')
        self.assertNotIn('SECRET', str(caught.exception))

    def test_dispatch_one_step_with_shared_binding_and_filter_output(self):
        import ameesh
        for command in ('advance', 'cancel', 'retry'):
            with self.subTest(command=command), \
                    mock.patch.object(work, 'corrector') as authorize, \
                    mock.patch.object(storage, 'of', return_value=self.api), \
                    mock.patch.object(ameesh, 'handoff_factory', self.loader, create=True), \
                    mock.patch.object(host_lifecycle, '_ReadyAdapter') as wrapper, \
                    mock.patch.object(handoffs, 'advance', return_value=self.result) as advance, \
                    mock.patch.object(handoffs, 'cancel', return_value=self.result) as cancel, \
                    mock.patch.object(handoffs, 'retry', return_value=self.result) as retry:
                code, out = self.invoke(command, '--actor', 'human:op')
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), {'operation': {'id': 'op', 'state': 'held'}})
                self.assertNotIn('SECRET', out)
                authorize.assert_called_once()
                self.assertFalse(authorize.call_args.kwargs['history'])
                wrapper.assert_called_once_with(self.db, self.binding.adapter,
                                                self.binding.requirements_for)
                calls = [advance.call_count, cancel.call_count, retry.call_count]
                self.assertEqual(sum(calls), 1)
                if command == 'retry':
                    self.assertIsNone(retry.call_args.kwargs['renew_ttl'])

    def test_manual_plan_uses_enrolled_adapter_without_lifecycle_requirement(self):
        import ameesh
        self.api.handoffs.get.return_value = {'id': 'op', 'plan': {}}
        with mock.patch.object(work, 'corrector'), \
                mock.patch.object(storage, 'of', return_value=self.api), \
                mock.patch.object(ameesh, 'handoff_factory', self.loader, create=True), \
                mock.patch.object(host_lifecycle, '_ReadyAdapter') as wrapper, \
                mock.patch.object(handoffs, 'advance', return_value=self.result) as advance:
            self.invoke('advance')
        wrapper.assert_not_called()
        advance.assert_called_once_with(self.db, 'op', self.binding.adapter)

    def test_unknown_operation_never_loads_factory(self):
        self.api.handoffs.get.return_value = None
        with mock.patch.object(work, 'corrector'), \
                mock.patch.object(storage, 'of', return_value=self.api):
            with self.assertRaisesRegex(ValueError, 'unknown handoff'):
                self.invoke('advance')
        self.loader.load.assert_not_called()

    def test_real_factory_without_enrollment_refuses_before_controller_step(self):
        # Exercise enrollment even when the checkout is group-writable. The
        # copy retains the actual source and passes the real loader trust check.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fabrique-handoff.py'
            shutil.copyfile(handoff_factory.source_path(), source)
            source.chmod(0o644)
            enrollment = Path(directory) / 'enrollment'
            enrollment.mkdir(mode=0o700)
            module = handoff_factory._module(source)
            module.HandoffFactory = functools.partial(
                module.HandoffFactory, root=enrollment)
            with mock.patch.object(work, 'corrector'), \
                    mock.patch.object(storage, 'of', return_value=self.api), \
                    mock.patch.object(handoff_factory, 'source_path', return_value=source), \
                    mock.patch.object(handoff_factory, '_module', return_value=module) as load, \
                    mock.patch.object(handoffs, 'advance') as advance, \
                    mock.patch.object(handoffs, 'cancel') as cancel, \
                    mock.patch.object(handoffs, 'retry') as retry:
                for command in ('advance', 'cancel', 'retry'):
                    with self.subTest(command=command):
                        with self.assertRaisesRegex(ValueError, '^handoff step refused;') as caught:
                            self.invoke(command)
                        self.assertNotIn(directory, str(caught.exception))
                        failure = caught.exception.__context__.__context__
                        self.assertIsInstance(failure, module.FactoryError)
                        self.assertEqual(str(failure), 'document:enrollment')
                self.assertEqual(load.call_args_list, [mock.call(source)] * 3)
                advance.assert_not_called()
                cancel.assert_not_called()
                retry.assert_not_called()

    def test_unauthorized_never_loads_factory_or_reads_operation(self):
        with mock.patch.object(work, 'corrector', side_effect=work.WorkError('refused')), \
                mock.patch.object(storage, 'of') as api:
            with self.assertRaises(work.WorkError):
                self.invoke('advance')
        api.assert_not_called()
        self.loader.load.assert_not_called()

    def test_private_helper_exception_is_filtered(self):
        import ameesh
        for command in ('advance', 'cancel', 'retry'):
            for error in (ValueError('SECRET'), handoffs.HandoffError('SECRET'),
                          handoffs.HandoffError(handoffs.Refusal.RENEWAL_REQUIRED.value)):
                with self.subTest(command=command, error=type(error)), \
                        mock.patch.object(work, 'corrector'), \
                        mock.patch.object(storage, 'of', return_value=self.api), \
                        mock.patch.object(ameesh, 'handoff_factory', self.loader, create=True), \
                        mock.patch.object(host_lifecycle, '_ReadyAdapter'), \
                        mock.patch.object(handoffs, command, side_effect=error):
                    with self.assertRaisesRegex(ValueError, '^handoff step refused; inspect') as raised:
                        self.invoke(command)
                self.assertNotIn('SECRET', str(raised.exception))
                self.assertNotIn('renewal_required', str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_explicit_renewal_forwarded_without_advancing(self):
        import ameesh
        with mock.patch.object(work, 'corrector'), \
                mock.patch.object(storage, 'of', return_value=self.api), \
                mock.patch.object(ameesh, 'handoff_factory', self.loader, create=True), \
                mock.patch.object(host_lifecycle, '_ReadyAdapter'), \
                mock.patch.object(handoffs, 'retry', return_value=self.result) as retry, \
                mock.patch.object(handoffs, 'advance') as advance:
            self.invoke('retry', '--renew-ttl', '300')
        self.assertEqual(retry.call_args.kwargs['renew_ttl'], 300)
        advance.assert_not_called()

    def test_command_closes_its_connection_after_refusal(self):
        args = mesh_cli.build_parser().parse_args(['handoff', 'advance', 'op'])
        with mock.patch.object(mesh_cli, '_open', return_value=self.db) as opened, \
                mock.patch.object(mesh_cli, '_handoff_operator',
                                  side_effect=ValueError('refused')):
            with self.assertRaisesRegex(ValueError, 'refused'):
                mesh_cli.cmd_handoff(object(), args)
        opened.assert_called_once()
        self.db.close.assert_called_once()

    def test_ttl_bounds_rejected_before_database(self):
        for ttl in ('29', '7201', 'true', '1.5'):
            with self.subTest(ttl=ttl), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    mesh_cli.build_parser().parse_args(
                        ['handoff', 'retry', 'op', '--renew-ttl', ttl])
