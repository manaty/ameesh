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
