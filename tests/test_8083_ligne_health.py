# SPDX-License-Identifier: AGPL-3.0-only
"""#8083: the health line is a closed vocabulary, read under a byte bound.

The review of #8072 (ef4e5e62) found two non-blocking defects in the worker:
`HEALTH_LINE` accepted any short `key=value` pair, not only the fields
`Commande.said` emits, and `run_probe` captured the whole stdout before
rejecting a line over 4096 bytes. Both are hardened here: the worker knows
exactly `REPORT`'s keys and values (one table, checked against the deployed
command), and reads at most 4097 bytes of a health probe's output.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest

from ameesh import vm_adapter as vm

from tests import test_6252_vm_commandes as cycle


class HealthVocabularyTest(unittest.TestCase):
    def test_the_worker_and_the_command_share_one_table(self):
        self.assertEqual(vm.HEALTH_REPORT, cycle.cmd.REPORT)

    def test_the_lines_the_command_prints_are_accepted(self):
        for line in (
                'health host=burst stage=control',
                'health host=ameesh-agents-3 nonce=abb7d867 stage=launched',
                'health host=burst nonce=abb7d867 stage=receipt answer=ok '
                'verdict=pending delivered=true acknowledged=false turn_ended=false',
                'health host=ameesh-agents-3 nonce=abb7d867 stage=checks answer=ok '
                'verdict=ok delivered=true acknowledged=true turn_ended=true '
                'checks=failed step=3',
                'health host=burst nonce=abb7d867 stage=previous-boot previous_boot=ok',
                'health host=burst stage=done'):
            with self.subTest(line=line):
                self.assertEqual(vm.health_line(line.encode()), line)

    def test_unknown_duplicated_or_missing_fields_are_refused(self):
        for hostile in (
                'health host=burst stage=control token=abc',
                'health host=burst stage=control answer=maybe',
                'health host=burst stage=control step=0',
                'health host=burst stage=control step=100',
                'health host=burst stage=control step=03',
                'health host=burst stage=control stage=done',
                'health host=burst',
                'health host=burst answer=ok',
                'health host=burst nonce=ABCDEF12 stage=control',
                'health host=burst nonce=abc stage=control',
                'health host=burst stage=previous_boot',
                'health host=' + 'a' * 65 + ' stage=control',
                'health host=burst stage=control ' + 'x' * 5000):
            with self.subTest(hostile=hostile):
                self.assertIsNone(vm.health_line(hostile.encode()))

    def test_an_output_over_4096_bytes_is_refused(self):
        self.assertIsNone(vm.health_line(b'x' * (vm.HEALTH_OUTPUT_MAX + 1)))
        self.assertIsNone(vm.health_line(None))
        self.assertIsNone(vm.health_line('health host=burst stage=control'))


class BoundedProbeTest(unittest.TestCase):
    """The worker reads the child's stdout under the bound (#8083)."""

    @staticmethod
    def script(source):
        return [sys.executable, '-c', source]

    def test_the_read_stops_one_byte_past_the_line(self):
        code, raw = vm._probe_bounded(
            self.script('import sys; sys.stdout.write("x" * 1000000)'), 10)
        self.assertIsInstance(code, int)
        self.assertEqual(len(raw), vm.HEALTH_OUTPUT_MAX + 1)

    def test_an_oversized_output_is_dropped_and_the_child_stopped(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = vm.run_probe(
                self.script('import sys; sys.stdout.write("x" * 1000000)'), 10,
                pending=True)
        self.assertIs(result, False)
        self.assertRegex(out.getvalue(),
                         r'^health probe: no valid line \(exit -?\d+\)\n$')

    def test_a_real_health_line_is_journalled(self):
        line = 'health host=burst stage=checks verdict=ok checks=failed step=2'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = vm.run_probe(self.script('print(%r); raise SystemExit(1)' % line),
                                  10, pending=True)
        self.assertIs(result, False)
        self.assertEqual(out.getvalue(), line + ' (exit 1)\n')

    def test_pending_is_read_from_the_exit_code(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = vm.run_probe(self.script('print("health host=burst stage=launched");'
                                              ' raise SystemExit(75)'), 10, pending=True)
        self.assertEqual(result, 'pending')
        self.assertEqual(out.getvalue(), 'health host=burst stage=launched (exit 75)\n')

    def test_a_bound_timeout_is_a_wait(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = vm.run_probe(self.script('import time; time.sleep(5)'), 0.2,
                                  pending=True)
        self.assertIs(result, False)
        self.assertEqual(out.getvalue(),
                         'health probe: not run or not ended within its bound\n')
