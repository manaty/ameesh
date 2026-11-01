# SPDX-License-Identifier: AGPL-3.0-only
"""#4900: retirement leaves idle/status/lease behind; health must ignore it."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support  # isolate the host's agent environment
from ameesh import hostcheck


class RetiredMailHealthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/sante-courrier.py'
        spec = importlib.util.spec_from_file_location('sante_courrier_retired', path)
        cls.health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.health)

    def rows(self):
        return [dict(name='retired', host='vm', mode='execute', status='idle',
                     retired_ts=900.0, lease_owner='old-runner', lease_expires_ts=1100.0),
                dict(name='active', host='vm', mode='execute', status='idle',
                     retired_ts=None, lease_owner='runner', lease_expires_ts=1100.0)]

    def test_retired_never_targeted_locally_or_cross_host(self):
        h = self.health
        with mock.patch.object(h.registry, 'overview', return_value=self.rows()):
            for host in ('vm', None):
                self.assertEqual([r['name'] for r in h.personas(None, host)], ['active'])
        self.assertFalse(h.driven(self.rows()[0], 1000.0))

    def test_retired_mail_does_not_fail_stuck_or_missing_executor(self):
        h = self.health
        for lease in (None, 'old-runner'):
            row = dict(self.rows()[0], lease_owner=lease)
            with mock.patch.object(h.mail, 'unread_active') as unread:
                self.assertEqual(h.stuck(None, [row], 10, 1000.0), [])
                self.assertEqual(h.sans_executeur(None, [row], 10, 1000.0), [])
                unread.assert_not_called()

    def test_restored_agent_is_checked_again_and_still_blocks_on_old_mail(self):
        h = self.health
        row = dict(self.rows()[0], retired_ts=None, lease_owner=None)
        with mock.patch.object(h.registry, 'overview', return_value=[row]), \
             mock.patch.object(h.mail, 'unread_active',
                               return_value=[{'id': 1, 'created_ts': 1}]), \
             mock.patch.object(h.mail, 'maybe_redelivered', return_value=False):
            self.assertEqual(h.personas(None, 'vm'), [row])
            self.assertEqual(h.sans_executeur(None, [row], 10, 1000)[0]['destinataire'],
                             'retired')

    def test_retirement_after_snapshot_prevents_probe(self):
        h = self.health
        active = self.rows()[1]
        with mock.patch.object(h.registry, 'get',
                               return_value=dict(active, retired_ts=999.0)), \
             mock.patch.object(h.time, 'time', return_value=1000.0):
            self.assertIsNone(h.revalider(None, [active])[0])

    def run_main(self, get, probe=None, unread=None):
        h = self.health
        output = io.StringIO()
        rows = [self.rows()[1]]
        with mock.patch.object(h.config, 'load', return_value=object()), \
             mock.patch.object(h.db_mod, 'connect', return_value=mock.Mock()), \
             mock.patch.object(h.registry, 'overview', return_value=rows), \
             mock.patch.object(h.registry, 'get', side_effect=get), \
             mock.patch.object(h.mail, 'unread_active', return_value=unread or []), \
             mock.patch.object(h.mail, 'maybe_redelivered', return_value=False), \
             mock.patch.object(h, 'dead_letters', return_value=[]), \
             mock.patch.object(h, 'canal_humain', return_value=[]), \
             mock.patch.object(h.time, 'time', return_value=1000.0), \
             mock.patch.object(h.mail, 'send', side_effect=probe), \
             contextlib.redirect_stdout(output):
            code = h.main(['--hote', 'vm', '--json', '--souffrance', '10',
                           '--attente', '0'])
        return code, json.loads(output.getvalue())

    def test_retirement_between_snapshot_and_checks_is_not_a_ko(self):
        # #4902: the snapshot counts the agent, the re-read sees it retired.
        active = self.rows()[1]
        retired = dict(active, retired_ts=999.0)
        code, report = self.run_main(lambda _db, _name: retired,
                                     unread=[{'id': 1, 'created_ts': 1}])
        self.assertEqual(report['souffrance'], [])
        self.assertEqual(report['verdict'], 'sans-sonde')
        self.assertEqual(code, 0)

    def test_agent_still_active_at_reread_keeps_the_ko(self):
        active = self.rows()[1]
        code, report = self.run_main(
            lambda _db, _name: active, unread=[{'id': 1, 'created_ts': 1}],
            probe=self.health.storage.RecipientRetired('retired'))
        self.assertEqual(report['verdict'], 'ko')
        self.assertEqual(report['souffrance'][0]['destinataire'], 'active')
        self.assertEqual(code, 1)

    def test_retirement_between_revalidation_and_send_is_not_a_ko(self):
        # #4902: mail.send refuses under lock; nothing was measured on the host.
        active = self.rows()[1]
        code, report = self.run_main(
            lambda _db, _name: active,
            probe=self.health.storage.RecipientRetired('retired'))
        self.assertEqual(report['verdict'], 'non-concluant')
        self.assertEqual(code, 3)
        self.assertNotIn('erreur', report)
        self.assertIsNone(report['sonde']['message'])
        self.assertIn('retiree', report['sonde']['tour'])

    def test_retirement_during_the_wait_is_not_a_ko(self):
        h = self.health
        active = self.rows()[1]
        with mock.patch.object(h.mail, 'send', return_value=7), \
             mock.patch.object(h.mail, 'get', return_value={'id': 7}), \
             mock.patch.object(h.registry, 'get',
                               return_value=dict(active, retired_ts=999.0)):
            out = h.probe(None, active, 'abc', 60.0)
        self.assertFalse(out['concluant'])
        self.assertIsNone(out['delai_s'])
        self.assertEqual(out['message'], 7)
        self.assertIn('retiree', out['tour'])

    def test_stopped_retired_mailbox_is_not_a_dead_letter(self):
        h = self.health
        db = mock.Mock()
        letters = [{'recipient': 'retired', 'n': 2}, {'recipient': 'stopped', 'n': 1}]
        rows = [dict(self.rows()[0], status='stopped'),
                dict(self.rows()[1], name='stopped', status='stopped')]
        with mock.patch.object(h.storage, 'of') as of:
            of.return_value.mailbox.dead_letters.return_value = letters
            self.assertEqual(h.dead_letters(db, 'vm', rows),
                             [{'destinataire': 'stopped', 'n': 1}])

    def test_retired_external_profile_is_not_a_human_channel(self):
        h = self.health
        with mock.patch.object(h.mail, 'unread_active') as unread:
            self.assertEqual(h.canal_humain(None, 'vm', 1000,
                             [dict(self.rows()[0], mode='externe')]), [])
            unread.assert_not_called()

    def test_doctor_does_not_require_a_runner_for_retired_agent(self):
        with mock.patch.object(hostcheck.registry, 'overview', return_value=self.rows()):
            self.assertEqual([r['name'] for r in hostcheck.served_agents(
                SimpleNamespace(host='vm'), None)], ['active'])

    def test_old_unread_retired_mail_keeps_health_green(self):
        h = self.health
        db = mock.Mock()
        output = io.StringIO()
        # Keep the real filters and selection. Only transport and clock are fake.
        def unread(_db, name):
            return [{'id': 1, 'created_ts': 1}, {'id': 2, 'created_ts': 2}] if name == 'retired' else []
        with mock.patch.object(h.config, 'load', return_value=object()), \
             mock.patch.object(h.db_mod, 'connect', return_value=db), \
             mock.patch.object(h.registry, 'overview', return_value=self.rows()), \
             mock.patch.object(h.registry, 'get', return_value=self.rows()[1]), \
             mock.patch.object(h.mail, 'unread_active', side_effect=unread), \
             mock.patch.object(h, 'dead_letters', return_value=[]), \
             mock.patch.object(h.time, 'time', return_value=1000.0), \
             mock.patch.object(h, 'probe', return_value={'delai_s': 0.1}) as probe, \
             contextlib.redirect_stdout(output):
            self.assertEqual(h.main(['--hote', 'vm', '--json', '--souffrance', '10']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['verdict'], 'ok')
        self.assertEqual(report['personas'], 1)
        self.assertEqual(report['souffrance'], [])
        self.assertEqual(report['sans_executeur'], [])
        self.assertEqual(probe.call_args.args[1]['name'], 'active')
