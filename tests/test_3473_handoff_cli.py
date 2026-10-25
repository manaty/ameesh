# SPDX-License-Identifier: AGPL-3.0-only
"""H6: operator authorization, single-step dispatch and private output."""
import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import handoffs, host_lifecycle, mesh_cli, storage, work


class OperatorTest(unittest.TestCase):
    def setUp(self):
        self.db = mock.Mock()
        self.api = mock.Mock()
        self.api.handoffs.get.return_value = {'id': 'op'}
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

    def test_ttl_bounds_rejected_before_database(self):
        for ttl in ('29', '7201', 'true', '1.5'):
            with self.subTest(ttl=ttl), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    mesh_cli.build_parser().parse_args(
                        ['handoff', 'retry', 'op', '--renew-ttl', ttl])
