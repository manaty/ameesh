# SPDX-License-Identifier: AGPL-3.0-only
"""#6034: a canon pin edited by hand, without a train-canon.jsonl entry, is refused."""
import json
import os
import types
import unittest
from unittest import mock

from ameesh import config, train_assembly, train_requalify
from . import test_3483_train_canon_auto as _auto


class UnjournaledPinTest(unittest.TestCase):
    # the real Git canon and host/profile files of #3483, without its tests
    setUp = _auto.AutomaticCanonTest.setUp
    host = _auto.AutomaticCanonTest.host
    read = _auto.AutomaticCanonTest.read

    def passage(self, pilot=None):
        return types.SimpleNamespace(project=types.SimpleNamespace(name='ameesh'),
            policy_path=self.repo.root, policy_commit=self.pin,
            canon=types.SimpleNamespace(pin=self.pin, pilot=pilot))

    def set_pin(self, paths, pin):
        for path in paths:
            raw = self.read(path)
            raw['train']['projects']['ameesh']['canon_commit'] = pin
            with open(path, 'w') as out:
                json.dump(raw, out)

    def reload(self, cfg, root, passage, report, alerted):
        with mock.patch.object(config, '_config_dir_default',
                               return_value=os.path.join(root, 'runner-profiles')), \
             mock.patch('ameesh.scoped_db.ScopedDb'), \
             mock.patch('ameesh.mail.send') as send:
            changes = train_assembly.reload((passage,),
                expected=train_assembly.Settings.parse(cfg.train),
                config_path=cfg.config_path, host=cfg.host, report=report,
                alerted=alerted)
        return changes, send

    def test_hand_edited_pin_is_refused_kept_and_alerted(self):
        cfg, root, base, profile = self.host('manual')
        self.set_pin((base, profile), self.gate)
        passage, report = self.passage(pilot=lambda db: 'pilote'), mock.Mock()
        changes, send = self.reload(cfg, root, passage, report, set())
        self.assertEqual(changes, [])
        self.assertEqual((passage.policy_commit, passage.canon.pin), (self.pin, self.pin))
        message = report.call_args.args[0]
        self.assertIn('refused for ameesh', message)
        self.assertIn('%s -> %s' % (self.pin[:12], self.gate[:12]), message)
        self.assertIn(base, message)
        self.assertIn(profile, message)
        send.assert_called_once()
        payload = send.call_args.kwargs['payload']['train_canon_unjournaled']
        self.assertEqual((payload['pin'], payload['new_pin']), (self.pin, self.gate))
        self.assertFalse(payload['startup'])

    def test_two_unjournaled_values_alert_once_each(self):
        cfg, root, base, profile = self.host('twice')
        self.repo.write('plan/other.md', 'other\n')
        other = self.repo.commit('second manual pin')
        passage, report, alerted = self.passage(), mock.Mock(), set()
        for pin in (self.gate, self.gate, other, other):
            self.set_pin((base, profile), pin)
            self.reload(cfg, root, passage, report, alerted)
        self.assertEqual(passage.policy_commit, self.pin)
        self.assertEqual(report.call_count, 2)
        self.assertIn(self.gate[:12], report.call_args_list[0].args[0])
        self.assertIn(other[:12], report.call_args_list[1].args[0])

    def test_mechanical_requalification_is_followed(self):
        cfg, root, *_ = self.host('mechanical')
        with mock.patch.object(config, '_config_dir_default',
                               return_value=os.path.join(root, 'runner-profiles')), \
             mock.patch.object(train_requalify.work, 'get', return_value={'id': 6034}), \
             mock.patch.object(train_requalify.work, 'note') as note:
            result = train_requalify.apply(cfg, object(), 'ameesh', lot=6034,
                                           actor='pilote',
                                           gate_reader=lambda *args: (self.gate, ''))
        self.assertEqual(result['state'], 'requalified')
        note.assert_called_once()
        passage, report = self.passage(), mock.Mock()
        changes, _send = self.reload(cfg, root, passage, report, set())
        self.assertEqual(passage.policy_commit, self.gate)
        self.assertEqual(changes[0]['pin'], self.gate)
        report.assert_not_called()

    def test_failed_lot_trace_leaves_no_journal_entry(self):
        cfg, root, base, profile = self.host('trace-failure')
        with mock.patch.object(config, '_config_dir_default',
                               return_value=os.path.join(root, 'runner-profiles')), \
             mock.patch.object(train_requalify.work, 'get', return_value={'id': 6034}), \
             mock.patch.object(train_requalify.work, 'note', side_effect=OSError('db')):
            with self.assertRaisesRegex(OSError, 'db'):
                train_requalify.apply(cfg, object(), 'ameesh', lot=6034, actor='pilote',
                                      gate_reader=lambda *args: (self.gate, ''))
        self.assertEqual(train_assembly.journaled(
            os.path.join(root, 'train-canon.jsonl'), 'ameesh'), set())
        self.assertEqual(self.read(base)['train']['projects']['ameesh']['canon_commit'],
                         self.pin)

    def test_startup_without_journal_assembles_without_alert(self):
        cfg, root, *_ = self.host('boot')
        report, alerted = mock.Mock(), set()
        train_assembly._startup_pins(cfg, (self.passage(),), report, alerted)
        report.assert_not_called()

    def test_startup_pin_unknown_to_an_existing_journal_is_alerted_not_refused(self):
        cfg, root, *_ = self.host('boot-journal')
        with train_requalify.journal(cfg, {'project': 'ameesh', 'pin': self.pin,
                                           'gate': self.gate}, actor='train'):
            pass
        report, passage = mock.Mock(), self.passage()
        with mock.patch.object(config, '_config_dir_default',
                               return_value=os.path.join(root, 'runner-profiles')):
            train_assembly._startup_pins(cfg, (passage,), report, set())
        self.assertEqual(passage.policy_commit, self.pin)
        report.assert_called_once()
        self.assertIn('at startup', report.call_args.args[0])
        # the journaled gate itself boots quietly
        report.reset_mock()
        passage.policy_commit = self.gate
        train_assembly._startup_pins(cfg, (passage,), report, set())
        report.assert_not_called()


if __name__ == '__main__':
    unittest.main()
