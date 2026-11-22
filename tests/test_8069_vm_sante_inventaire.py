# SPDX-License-Identifier: AGPL-3.0-only
"""#8069: VM health of AG3, red at each R1 of #6002 (22/11).

Defect 1: the producer compared the active runner units with the `units` of
a root card posed once (8/11); an agent added or retired made the receipt
red (`inventory:ValueError`). The units now come from the user manager.
Defect 2: readiness required a lease from every runner, while a `returning`
return with claims closed lets only its probe agent claim (#7874).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from ameesh import host_lifecycle, registry

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'vm_health_8069', ROOT / 'deploy/vm-adapter/remote/checks/health.py')
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)

PROBE, OTHER = 'ameesh-runner-agent@probe.service', 'ameesh-runner-agent@other.service'
IDS = {PROBE: 'runner-probe@vm', OTHER: 'runner-other@vm'}


def show(states):
    return '\n\n'.join('Id=%s\nActiveState=%s\nUnitFileState=%s' % (unit, active, enabled)
                       for unit, (active, enabled) in states.items()) + '\n'


class Producer:
    """produce() against a fake user manager, registry and host control."""

    def __init__(self, root, *, units, leases, control=None, card_units=None, unloaded=()):
        self.receipt = Path(root) / 'health.json'
        self.boot = Path(root) / 'boot'
        self.boot.write_text('boot\n')
        self.card = dict(venv='/venv', verifier='/verifier.py', environment_file='/env',
                         receipt=str(self.receipt))
        if card_units is not None:
            self.card['units'] = card_units
        self.units, self.leases, self.control = units, leases, control
        # Enabled by a default.target.wants link, absent from list-units.
        self.unloaded = set(unloaded)
        self.calls = []

    def command(self, argv, end, **kw):
        self.calls.append(argv)
        if 'list-units' in argv:
            return json.dumps([dict(unit=u, active=a) for u, (a, _e) in self.units.items()
                               if u not in self.unloaded])
        if 'default.target' in argv:
            return ' '.join(['docker.service'] + [u for u, (_a, e) in self.units.items()
                                                  if e == 'enabled']) + '\n'
        if 'show' in argv:
            return show({u: self.units[u] for u in argv[argv.index('--') + 1:]})
        if '-c' in argv:
            return json.dumps(dict(version='1.2.3', source={'url': 'file:///src-' + 'a' * 40}))
        if 'list' in argv:
            return json.dumps([dict(name=name, host='vm', lease_owner=owner,
                                    runner_seen_owner=seen, status='idle',
                                    lease_expires_ts=time.time() + 60 if owner else None)
                               for name, owner, seen in self.leases])
        if 'status' in argv:
            return json.dumps(self.control)
        return ''

    def state(self, unit, *args):
        if self.units[unit][0] != 'active':
            raise ValueError('runner inactive or configuration stale')
        return 100 + sorted(self.units).index(unit), 'execute', IDS[unit]

    def run(self):
        verifier = SimpleNamespace(venv_installe=mock.Mock(return_value=Path('/live')))
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=mock.Mock()))
        with mock.patch.object(health.os, 'geteuid', return_value=1000), \
                mock.patch.object(health.importlib.util, 'spec_from_file_location', return_value=spec), \
                mock.patch.object(health.importlib.util, 'module_from_spec', return_value=verifier), \
                mock.patch.object(health, 'runner_state', side_effect=self.state), \
                mock.patch.object(health, 'command', side_effect=self.command), \
                contextlib.redirect_stderr(io.StringIO()):
            code = health.produce(boot=self.boot, reader=lambda *a: self.card)
        return code, json.loads(self.receipt.read_text())


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.leases = [('probe', IDS[PROBE], IDS[PROBE]), ('other', IDS[OTHER], IDS[OTHER])]

    def test_card_and_host_inventory_diverging_stays_green(self):
        # AG3 on 22/11: the card still named codex2 (gone), the host ran others.
        retired = 'ameesh-runner-agent@codex2.service'
        units = {PROBE: ('active', 'enabled'), OTHER: ('active', 'enabled'),
                 'ameesh-runner-agent@avatar-essai.service': ('inactive', 'disabled')}
        producer = Producer(self.temp.name, units=units, leases=self.leases,
                            card_units=[PROBE, retired])
        code, receipt = producer.run()
        self.assertEqual(code, 0, receipt)
        self.assertTrue(receipt['version_mode'] and receipt['doctor'])
        self.assertEqual(len(receipt['pids']), 2)  # avatar-essai, disabled and stopped, is out

    def test_card_without_units_is_valid(self):
        producer = Producer(self.temp.name, units={PROBE: ('active', 'enabled'),
                                                   OTHER: ('active', 'enabled')},
                            leases=self.leases)
        self.assertEqual(producer.run()[0], 0)

    def test_enabled_runner_stopped_or_failed_is_expected_and_red(self):
        for state in ('inactive', 'failed'):
            with self.subTest(state=state):
                producer = Producer(self.temp.name, units={
                    PROBE: ('active', 'enabled'), OTHER: (state, 'enabled')}, leases=self.leases)
                code, receipt = producer.run()
                self.assertEqual((code, receipt['reason']), (1, 'runners-before:ValueError'))

    def test_enabled_instance_absent_from_list_units_is_expected_and_red(self):
        # B of mty-amh-impl-11 (freeze caf3812f9a99): an instance enabled by its
        # default.target.wants link but not loaded was invisible to list-units.
        units = {PROBE: ('active', 'enabled'), OTHER: ('inactive', 'enabled')}
        producer = Producer(self.temp.name, units=units, leases=self.leases, unloaded=[OTHER])
        with mock.patch.object(health, 'command', side_effect=producer.command):
            self.assertEqual(health.runner_units(time.monotonic() + 5), sorted(units))
        code, receipt = producer.run()
        self.assertEqual((code, receipt['reason']), (1, 'runners-before:ValueError'))

    def test_disabled_active_runner_is_still_proven(self):
        units = {PROBE: ('active', 'enabled'), OTHER: ('active', 'disabled')}
        with mock.patch.object(health, 'command', side_effect=Producer(
                self.temp.name, units=units, leases=[]).command):
            self.assertEqual(health.runner_units(time.monotonic() + 5), sorted(units))

    def test_unreadable_or_foreign_inventory_refuses(self):
        cases = [
            ([], ''),
            ([dict(unit='ameesh-runner-agent@x y.service', active='active')], ''),
            ([dict(unit=PROBE, active='active')], 'Id=%s\nActiveState=active\n' % OTHER),
            ([dict(unit=PROBE, active='inactive')], show({PROBE: ('inactive', 'disabled')})),
        ]
        for listing, props in cases:
            def command(argv, end, **kw):
                return json.dumps(listing) if 'list-units' in argv else props
            with self.subTest(listing=listing), \
                    mock.patch.object(health, 'command', side_effect=command), \
                    self.assertRaises(ValueError):
                health.runner_units(time.monotonic() + 5)


class ClosedReturnReadinessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.units = {PROBE: ('active', 'enabled'), OTHER: ('active', 'enabled')}

    def control(self, mode='returning', claims_open=False, probe='probe'):
        return [dict(host='vm', mode=mode, claims_open=claims_open, return_probe=probe)]

    def test_closed_return_with_only_the_probe_leased_is_green(self):
        producer = Producer(self.temp.name, units=self.units, control=self.control(),
                            leases=[('probe', IDS[PROBE], IDS[PROBE]), ('other', None, IDS[OTHER])])
        code, receipt = producer.run()
        self.assertEqual(code, 0, receipt)
        status = [argv for argv in producer.calls if 'status' in argv]
        self.assertEqual([argv[-3:] for argv in status], [['host', 'status', 'vm']])
        self.assertIn('--property=EnvironmentFile=/env', status[0])

    def test_closed_return_probe_without_lease_is_red(self):
        producer = Producer(self.temp.name, units=self.units, control=self.control(),
                            leases=[('probe', None, IDS[PROBE]), ('other', None, IDS[OTHER])])
        code, receipt = producer.run()
        self.assertEqual((code, receipt['reason']), (1, 'readiness:ValueError'))
        self.assertTrue(receipt['version_mode'])

    def test_probe_leased_by_a_foreign_runner_is_red(self):
        with self.assertRaises(ValueError):
            health.readiness([dict(name='probe', lease_owner='runner-probe@elsewhere',
                                   lease_expires_ts=time.time() + 60)],
                             [IDS[PROBE], IDS[OTHER]], time.time(), 'probe')

    def test_probe_expired_or_host_blocked_is_red(self):
        for row in (dict(lease_expires_ts=time.time() - 1),
                    dict(lease_expires_ts=time.time() + 60, status='blocked',
                         status_text='hôte non prêt')):
            with self.subTest(row=row), self.assertRaises(ValueError):
                health.readiness([dict(row, name='probe', lease_owner=IDS[PROBE])],
                                 [IDS[PROBE], IDS[OTHER]], time.time(), 'probe')

    def test_open_or_draining_host_still_requires_every_lease(self):
        for control in (self.control(claims_open=True), self.control(mode='available', probe=None),
                        self.control(mode='draining', probe=None)):
            with self.subTest(control=control[0]):
                producer = Producer(self.temp.name, units=self.units, control=control,
                                    leases=[('probe', IDS[PROBE], IDS[PROBE]),
                                            ('other', None, IDS[OTHER])])
                code, receipt = producer.run()
                self.assertEqual((code, receipt['reason']), (1, 'readiness:ValueError'))

    def test_unreadable_control_refuses(self):
        for control in (None, [], [dict(host='other-vm', mode='returning', claims_open=False,
                                        return_probe='probe')],
                        self.control(probe=None), self.control(probe='')):
            with self.subTest(control=control), self.assertRaises(ValueError):
                health.closed_return_probe(control, 'vm')

    def test_all_leased_never_reads_the_control(self):
        producer = Producer(self.temp.name, units=self.units, control=None,
                            leases=[('probe', IDS[PROBE], IDS[PROBE]),
                                    ('other', IDS[OTHER], IDS[OTHER])])
        self.assertEqual(producer.run()[0], 0)
        self.assertFalse([argv for argv in producer.calls if 'status' in argv])

    def test_runner_host_must_be_one(self):
        rows = [dict(host='vm', runner_seen_owner='a'), dict(host='vm2', lease_owner='b')]
        with self.assertRaises(ValueError):
            health.runner_host(rows, ['a', 'b'])
        with self.assertRaises(ValueError):
            health.runner_host([], ['a'])
        self.assertEqual(health.runner_host(rows, ['a']), 'vm')


class HostStatusReturnProbeTest(PgTestCase):
    """The host control the producer reads, and the claim rule it mirrors."""

    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('TRUNCATE host_release_operations CASCADE')
        self.db.execute('DELETE FROM host_lifecycle')
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,"
                        "hook_state,return_probe) VALUES ('burst','returning','tester',false,"
                        "'draining','draining','burst-probe')")
        registry.upsert(self.db, 'burst-probe', harness='claude', host='burst')
        registry.upsert(self.db, 'burst-other', harness='codex', host='burst')

    def status(self):
        out = io.StringIO()
        with mock.patch.object(host_lifecycle.config, 'load'), \
                mock.patch.object(host_lifecycle.db_mod, 'connect', return_value=self.db), \
                mock.patch.object(self.db, 'close'), contextlib.redirect_stdout(out):
            self.assertEqual(host_lifecycle.main(['status', 'burst']), 0)
        return json.loads(out.getvalue())

    def test_status_names_the_closed_return_probe(self):
        row = self.status()[0]
        self.assertEqual((row['mode'], row['claims_open'], row['return_probe']),
                         ('returning', False, 'burst-probe'))
        self.assertEqual(health.closed_return_probe(self.status(), 'burst'), 'burst-probe')

    def test_closed_return_lets_only_the_probe_claim(self):
        # Defect 2 confirmed on the claim rule: the other runner can hold no lease.
        names = sorted(a['name'] for a in registry.claimable(self.db, 'burst'))
        self.assertEqual(names, ['burst-probe'])


if __name__ == '__main__':
    unittest.main()
