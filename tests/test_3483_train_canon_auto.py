# SPDX-License-Identifier: AGPL-3.0-only
"""Real Git/config witnesses for automatic, host-local canon qualification."""
import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import config, train_assembly, train_requalify, train_runner
from .test_3366_train_canon_requalify import _Repo


class AutomaticCanonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _Repo(os.path.join(self.tmp.name, 'canon'))
        self.pin = self.repo.git('rev-parse', 'HEAD')
        self.repo.write('plan/roadmap.md', 'next\n')
        self.gate = self.repo.commit('plan only')

    def host(self, name):
        root = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.join(root, 'runner-profiles'))
        path = os.path.join(root, 'config.json')
        profile = os.path.join(root, 'runner-profiles', 'critiques.json')
        project = dict(repo=root, target='main', github='o/r', canon=self.repo.root,
                       canon_commit=self.pin, ci_contract='ameesh',
                       provenance='review_provenance', reservation='packet_reservation',
                       deadline_minutes=120, candidate=False, initial_since=None)
        raw = dict(host=name, train=dict(schema='ameesh-train-host/1', host=name,
                   role='train', mode='active', projects={'ameesh': project}))
        with open(path, 'w') as out:
            json.dump(raw, out)
        with open(profile, 'w') as out:
            json.dump({'inherit': path, 'train': {'projects': {
                'ameesh': {'canon_commit': self.pin}}}}, out)
        cfg = config.load({'AMEESH_CONFIG': profile})
        return cfg, root, path, profile

    def automatic(self, cfg, root, gate=None):
        # Profile discovery uses this host's configured profile directory.
        with mock.patch.object(config, '_config_dir_default',
                               return_value=os.path.join(root, 'runner-profiles')):
            return train_requalify.automatic(cfg, object(), 'ameesh',
                gate_reader=lambda *args: (self.gate if gate is None else gate, ''))

    def read(self, path):
        with open(path) as source:
            return json.load(source)

    def test_plan_merge_follows_on_two_hosts_and_all_profiles(self):
        for name in ('host-a', 'host-b'):
            cfg, root, base, profile = self.host(name)
            result = self.automatic(cfg, root)
            self.assertEqual(result['changed'], ['plan/roadmap.md'])
            for path in (base, profile):
                self.assertEqual(self.read(path)['train']['projects']['ameesh']['canon_commit'],
                                 self.gate)
            row = self.read(os.path.join(root, 'train-canon.jsonl'))
            self.assertEqual((row['pin'], row['gate'], row['host']),
                             (self.pin, self.gate, name))
            self.assertEqual(row['guard'], list(train_assembly.REQUALIFY_GUARD))
            fresh = config.load({'AMEESH_CONFIG': profile})
            self.assertEqual(self.automatic(fresh, root)['state'], 'aligned')
            with open(os.path.join(root, 'train-canon.jsonl')) as source:
                self.assertEqual(len(source.readlines()), 1)

    def test_rule_changes_refuse_without_configuration_changes(self):
        for rule in train_assembly.REQUALIFY_GUARD:
            with self.subTest(rule=rule):
                cfg, root, base, profile = self.host(rule.replace('/', '-'))
                self.repo.write(rule, 'changed\n')
                gate = self.repo.commit('rule changed')
                with self.assertRaisesRegex(train_requalify.RequalifyError,
                                            'reviewed requalification required'):
                    self.automatic(cfg, root, gate)
                for path in (base, profile):
                    self.assertEqual(self.read(path)['train']['projects']['ameesh']['canon_commit'],
                                     self.pin)
                self.assertFalse(os.path.exists(os.path.join(root, 'train-canon.jsonl')))

    def test_backward_gate_refuses(self):
        cfg, root, base, profile = self.host('backward')
        self.automatic(cfg, root)
        fresh = config.load({'AMEESH_CONFIG': profile})
        with self.assertRaisesRegex(train_requalify.RequalifyError, 'not a descendant'):
            self.automatic(fresh, root, self.pin)

    def test_renaming_a_rule_is_not_hidden_by_git_rename_detection(self):
        cfg, root, *_ = self.host('rename')
        self.repo.git('mv', 'processus/mecanique.yaml', 'processus/old.yaml')
        # Keep a valid rule file at the destination commit, with new content.
        self.repo.write('processus/mecanique.yaml', 'schema: changed\n')
        gate = self.repo.commit('rename rule and replace')
        with self.assertRaisesRegex(train_requalify.RequalifyError, 'rules change'):
            self.automatic(cfg, root, gate)

    def test_unreadable_gate_refuses(self):
        cfg, root, *_ = self.host('unknown')
        with self.assertRaisesRegex(train_requalify.RequalifyError, 'no gate canon'):
            self.automatic(cfg, root, '')

    def test_journal_failure_restores_every_pin(self):
        cfg, root, base, profile = self.host('journal-failure')
        with mock.patch.object(train_requalify.os, 'fsync', side_effect=OSError('disk')):
            with self.assertRaisesRegex(OSError, 'disk'):
                self.automatic(cfg, root)
        for path in (base, profile):
            self.assertEqual(self.read(path)['train']['projects']['ameesh']['canon_commit'],
                             self.pin)
        with open(os.path.join(root, 'train-canon.jsonl')) as journal:
            self.assertEqual(journal.read(), '')

    def test_refresh_automatically_qualifies_then_reloads_effective_pin(self):
        cfg, root, *_ = self.host('refresh')
        passage = types.SimpleNamespace(project=types.SimpleNamespace(name='ameesh'),
            policy_path=self.repo.root, policy_commit=self.pin,
            canon=types.SimpleNamespace(pin=self.pin))
        with mock.patch.object(config, '_config_dir_default',
                return_value=os.path.join(root, 'runner-profiles')), \
             mock.patch('ameesh.scoped_db.ScopedDb') as db, \
             mock.patch.object(train_requalify.train_canon, 'gate_commit',
                               return_value=(self.gate, '')):
            changes = train_assembly.reload((passage,),
                expected=train_assembly.Settings.parse(cfg.train),
                config_path=cfg.config_path, host=cfg.host, automatic=True)
            db.return_value.close.assert_called_once()
        self.assertEqual(passage.policy_commit, self.gate)
        self.assertEqual(passage.canon.pin, self.gate)
        self.assertEqual(changes[0]['previous'], self.pin)

    def test_refusals_keep_passages_running_and_do_not_block_other_projects(self):
        for index, failure in enumerate((
                train_requalify.RequalifyError('rules change'),
                train_requalify.RequalifyError('no gate canon commit'),
                train_requalify.RequalifyError('not a descendant'),
                OSError('journal failed'), RuntimeError('database unavailable'))):
            with self.subTest(failure=str(failure)):
                cfg, root, base, profile = self.host('refusal-%s' % index)
                raw = self.read(base)
                raw['train']['projects']['nexlink'] = dict(
                    raw['train']['projects']['ameesh'], ci_contract='nexlink')
                with open(base, 'w') as out:
                    json.dump(raw, out)
                cfg = config.load({'AMEESH_CONFIG': profile})
                passages = [types.SimpleNamespace(
                    project=types.SimpleNamespace(name=name),
                    policy_path=self.repo.root, policy_commit=self.pin,
                    canon=types.SimpleNamespace(pin=self.pin),
                    scheduled_step=mock.Mock(return_value=({'result': 'ci'}, 200)))
                    for name in ('ameesh', 'nexlink')]
                report, refusals = mock.Mock(), {}
                original = train_requalify.automatic
                def qualify(fresh, db, project):
                    if project == 'ameesh':
                        raise failure
                    return original(fresh, db, project,
                                    gate_reader=lambda *args: (self.gate, ''))
                def refresh():
                    return train_assembly.reload(passages,
                        expected=train_assembly.Settings.parse(cfg.train),
                        config_path=cfg.config_path, automatic=True,
                        refusals=refusals, report=report)
                service = train_runner.Service(cfg, passages, stop=threading.Event(),
                                               report=mock.Mock(), refresh=refresh)
                with mock.patch.object(config, '_config_dir_default',
                        return_value=os.path.join(root, 'runner-profiles')), \
                     mock.patch('ameesh.scoped_db.ScopedDb'), \
                     mock.patch.object(train_requalify, 'automatic', side_effect=qualify):
                    for _ in range(2):
                        self.assertEqual(service.pass_once(0, 100), 200)
                        self.assertEqual(service.pass_once(1, 100), 200)
                self.assertEqual(passages[0].policy_commit, self.pin)
                self.assertEqual(passages[1].policy_commit, self.gate)
                self.assertEqual(passages[0].scheduled_step.call_count, 2)
                self.assertEqual(passages[1].scheduled_step.call_count, 2)
                report.assert_called_once()
                self.assertIn('ameesh', report.call_args.args[0])
                self.assertIn(str(failure), report.call_args.args[0])

    def test_manual_and_automatic_writers_take_the_same_lock(self):
        cfg, root, *_ = self.host('locking')
        with mock.patch.object(train_requalify.fcntl, 'flock') as lock, \
             mock.patch.object(train_requalify, '_apply', return_value={}) as apply:
            train_requalify.apply(cfg, object(), 'ameesh', lot=3483, actor='pilot')
            self.automatic(cfg, root)
        self.assertEqual(lock.call_count, 2)
        self.assertEqual(apply.call_count, 2)
