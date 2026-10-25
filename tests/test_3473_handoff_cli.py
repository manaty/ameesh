# SPDX-License-Identifier: AGPL-3.0-only
"""H6: operator authorization, single-step dispatch and private output."""
import contextlib
import functools
import io
import json
from pathlib import Path
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
        # Keep the actual pinned source and loader; only redirect its enrollment
        # documents to an empty private directory, without touching this host.
        module = handoff_factory._module(handoff_factory.source_path())
        with tempfile.TemporaryDirectory() as directory:
            module.HandoffFactory = functools.partial(
                module.HandoffFactory, root=Path(directory))
            with mock.patch.object(work, 'corrector'), \
                    mock.patch.object(storage, 'of', return_value=self.api), \
                    mock.patch.object(handoff_factory, '_module', return_value=module), \
                    mock.patch.object(handoffs, 'advance') as advance, \
                    mock.patch.object(handoffs, 'cancel') as cancel, \
                    mock.patch.object(handoffs, 'retry') as retry:
                for command in ('advance', 'cancel', 'retry'):
                    with self.subTest(command=command):
                        with self.assertRaisesRegex(ValueError, '^handoff step refused;') as caught:
                            self.invoke(command)
                        self.assertNotIn(directory, str(caught.exception))
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
        with mock.patch.object(work, 'corrector'), \
                mock.patch.object(storage, 'of', return_value=self.api), \
                mock.patch.object(ameesh, 'handoff_factory', self.loader, create=True), \
                mock.patch.object(host_lifecycle, '_ReadyAdapter'), \
                mock.patch.object(handoffs, 'retry', side_effect=ValueError('SECRET')):
            with self.assertRaisesRegex(ValueError, '^handoff step refused;') as raised:
                self.invoke('retry')
        self.assertNotIn('SECRET', str(raised.exception))
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
