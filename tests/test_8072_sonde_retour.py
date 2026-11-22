# SPDX-License-Identifier: AGPL-3.0-only
"""#8072: the return probe of R1 (22/11), read from the worker log alone.

Relevé of 22/11: the probe `mty-amh-impl-15` DID play its turns. The journal
of `runner-ameesh-dev4@ameesh-agents-3` shows four probe turns (05:18, 05:21,
05:24, 05:27Z), each one acknowledging its nonce with delivery confirmed. Each
was followed by a refused health (`health.py`, `inventory:ValueError`, fixed
by #8069): `refuse_probe` sets the host back to `draining`, which closes the
probe's claims. The runner then logs « host claims closed (draining or
returning) » and gives up its lease, the next admission names it again, and
a new nonce is launched. Nothing in the claim path was refusing it (#7874
holds). The record files of the VM (`/var/lib/ameesh-vm-remote/<nonce>.json`)
only hold the launch by design: the receipt is computed on demand.

The cause of the misreading is therefore at the source: the worker discarded
the output of `health`, so neither the receipt nor the failing check were in
its log. (c) Health now prints one bounded line, and the worker journals it
after validation. (b) The queue splitter never sleeps the probe of an open
host window, and lifts its automatic sleep.
"""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace
import time
import unittest
from unittest import mock

from ameesh import agent_pause, file_decoupage as fd, registry, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue

from tests.support import PgTestCase
from tests import test_4610_sleep_work_mail as sleep_fixtures
from tests import test_6252_vm_commandes as cycle

FRESH, cmd = cycle.FRESH, cycle.cmd


class HealthLineTest(cycle.CloudCycleTest):
    """(c) The real command, the real worker tick, a simulated guest."""

    def said(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = self.tick()
        lines = [l for l in out.getvalue().splitlines() if l.startswith('health ')]
        self.assertEqual(len(lines), 1, out.getvalue())
        self.assertIsNotNone(vm.health_line(lines[0].encode()), lines[0])
        return result, lines[0]

    def fields(self, line):
        return dict(part.split('=', 1) for part in line.split()[1:])

    def test_the_22_11_return_reads_from_the_line(self):
        # The probe turn ends with a complete receipt; a remote check is red.
        self.returning()
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            result, line = self.said()
            self.assertEqual(result, 'health pending')
            nonce = self.vm.launches[-1][-1]
            self.assertEqual(self.fields(line), {'host': 'burst', 'nonce': nonce[:8],
                                                 'stage': 'launched'})
            result, line = self.said()
            self.assertEqual(self.fields(line)['answer'], 'failed')  # no receipt yet
            self.assertEqual(self.fields(line)['verdict'], 'pending')
            self.complete(acknowledged=False, turn_ended=False, verdict='pending')
            result, line = self.said()
            self.assertEqual(result, 'health pending')
            self.assertEqual(self.fields(line), {
                'host': 'burst', 'nonce': nonce[:8], 'stage': 'receipt', 'answer': 'ok',
                'verdict': 'pending', 'delivered': 'true', 'acknowledged': 'false',
                'turn_ended': 'false'})
            self.vm.failing.add('doctor')
            self.complete()
            result, line = self.said()
        self.assertEqual(result, 'health failed')
        fields = self.fields(line)
        self.assertEqual((fields['verdict'], fields['delivered'], fields['acknowledged'],
                          fields['turn_ended'], fields['stage'], fields['checks']),
                         ('ok', 'true', 'true', 'true', 'checks', 'failed'))
        rank = [a[0].rsplit('/', 1)[-1] for a in self.cible['remote']['health']].index('doctor')
        self.assertEqual(fields['step'], str(rank + 1))
        # Never the full nonce, a path, a key or a remote byte.
        self.assertNotIn(nonce, line)
        self.assertNotIn('/', line)
        self.assertEqual(self.control(), ('draining', False, None))

    def test_a_foreign_receipt_says_absent(self):
        self.returning()
        with FRESH:
            self.tick()
            self.said()
            self.complete(nonce='0' * 32)
            _result, line = self.said()
        self.assertEqual(self.fields(line)['verdict'], 'pending')
        self.assertEqual(self.fields(line)['delivered'], 'absent')

    def test_a_refused_control_still_prints_its_stage(self):
        # Not returning: the control refuses, the line says where it stopped.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd.main(['health', str(self.path)], natif=cmd.Natif.of(self.db),
                            runner=self.vm.run)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().strip(), 'health host=burst stage=control')


class RunProbeLineTest(unittest.TestCase):
    """(c) The worker journals the validated line, and nothing else."""

    def run_probe(self, stdout, code=75, pending=True):
        out = io.StringIO()
        done = SimpleNamespace(returncode=code, stdout=stdout)
        with mock.patch('ameesh.vm_adapter.subprocess.run', return_value=done) as run, \
                contextlib.redirect_stdout(out):
            result = vm.run_probe(['/health'], 10, pending=pending)
        return result, out.getvalue(), run.call_args.kwargs

    def test_valid_line_is_journalled_with_the_exit_code(self):
        line = (b'health host=ameesh-agents-3 nonce=abb7d867 stage=checks answer=ok '
                b'verdict=ok delivered=true acknowledged=true turn_ended=true '
                b'checks=failed step=3\n')
        result, out, kwargs = self.run_probe(line, code=1)
        self.assertIs(result, False)
        self.assertEqual(out, line.decode().strip() + ' (exit 1)\n')
        self.assertEqual(kwargs['stderr'], vm.subprocess.DEVNULL)

    def test_anything_else_is_dropped_unread(self):
        for hostile in (b'postgresql://user:secret@db/ameesh\n',
                        b'health host=burst stage=/etc/shadow\n',
                        b'health host=burst token=' + b'a' * 40 + b'\n',
                        b'health host=burst\n',
                        b'x' * 5000):
            result, out, _ = self.run_probe(hostile)
            self.assertEqual(result, 'pending')
            self.assertEqual(out, 'health probe: no valid line (exit 75)\n')

    def test_other_probes_stay_silent_and_discarded(self):
        result, out, kwargs = self.run_probe(None, code=0, pending=False)
        self.assertIs(result, True)
        self.assertEqual(out, '')
        self.assertEqual(kwargs['stdout'], vm.subprocess.DEVNULL)


class RefusedReturnClosesTheProbeTest(PgTestCase):
    """(a) What the runner's « host claims closed » meant on 22/11."""

    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('DELETE FROM host_lifecycle')
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,hook_state) "
                        "VALUES ('burst','draining','tester',false,'draining','draining')")
        registry.upsert(self.db, 'burst-probe', harness='claude', host='burst')
        self.q = Queue(self.db)
        control = self.q.control('burst')
        # The busy request of the resync (#7913) precedes the return.
        self.q.request('burst', control['generation'], control['release_epoch'], 'busy')
        self.request = self.q.pending('burst')[0]

    def open_for_probe(self):
        return storage.of(self.db).host_lifecycle.claims_open('burst', 'burst-probe')

    def test_refused_health_closes_the_probe_and_readmission_reopens_it(self):
        self.assertFalse(self.open_for_probe())
        self.assertTrue(self.q.admit_probe(self.request, 'burst-probe'))
        self.assertTrue(self.open_for_probe())
        self.assertEqual([a['name'] for a in registry.claimable(self.db, 'burst')],
                         ['burst-probe'])
        lease = registry.claim(self.db, 'burst-probe', 'runner-ag3', ttl_seconds=300)
        self.assertTrue(registry.begin_turn(self.db, 'burst-probe', 'runner-ag3',
                                            lease['lease_epoch'], 'probe turn'))
        # A refused health after the turn: the runner's next check between
        # turns sees the probe closed and releases its lease (#7799).
        self.assertTrue(self.q.refuse_probe(self.request))
        self.assertFalse(self.open_for_probe())
        self.assertTrue(self.q.admit_probe(self.request, 'burst-probe'))
        self.assertTrue(self.open_for_probe())


def _window(db, host, probe):
    db.execute('DELETE FROM host_lifecycle WHERE host=%s', (host,))
    db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,"
               "hook_state,return_probe) VALUES (%s,'returning','tester',false,"
               "'draining','draining',%s)", (host, probe))


class WindowProbeSleepTest(PgTestCase):
    """(b) The queue splitter never sleeps the probe of an open host window."""

    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM host_lifecycle')
        for name in ("pilote", "dev0", "dev1", "dev2"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    listing = sleep_fixtures.SleepWorkMailTest.listing
    run_pass = sleep_fixtures.SleepWorkMailTest.run_pass
    split_lot = sleep_fixtures.SleepWorkMailTest.split_lot
    make_split = sleep_fixtures.SleepWorkMailTest.make_split
    notes = sleep_fixtures.SleepWorkMailTest.notes
    sleep_all = sleep_fixtures.SleepWorkMailTest.sleep_all
    lift_event = sleep_fixtures.SleepWorkMailTest.lift_event

    def test_window_probes_reads_closed_returns_and_live_fences(self):
        self.assertEqual(fd.window_probes(self.db), (set(), None))
        _window(self.db, 'burst', 'dev1')
        self.assertEqual(fd.window_probes(self.db), ({'dev1'}, None))
        self.db.execute("UPDATE host_lifecycle SET claims_open=true WHERE host='burst'")
        self.assertEqual(fd.window_probes(self.db), (set(), None))
        self.db.execute("INSERT INTO host_release_operations(operation,host,manifest_digest) "
                        "VALUES ('op-8072','burst',repeat('a',64))")
        self.db.execute("INSERT INTO host_release_fences(host,operation,probe) "
                        "VALUES ('burst','op-8072','dev2')")
        try:
            self.assertEqual(fd.window_probes(self.db), ({'dev2'}, None))
        finally:
            self.db.execute("DELETE FROM host_release_fences WHERE host='burst'")
            self.db.execute("DELETE FROM host_release_operations WHERE operation='op-8072'")

    def test_probe_of_a_closed_return_is_spared(self):
        split = self.make_split()
        from ameesh import work
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        _window(self.db, 'burst', 'dev1')
        out = self.run_pass(1900, self.listing("dev0", "dev1"))
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertIn("#8072", by_agent["dev1"]["why"])
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(any(n.startswith("sleep exempted: dev1:") and "#8072" in n
                            for n in self.notes(split)))

    def test_unreadable_probes_sleep_nobody(self):
        split = self.make_split()
        from ameesh import work
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        with mock.patch.object(fd, 'window_probes', return_value=(set(), 'Boom: x')):
            out = self.run_pass(1900, self.listing("dev0", "dev1"))
        sleep = next(g for g in out if g["kind"] == "sleep")
        self.assertFalse(any(row["slept"] for row in sleep["agents"]))
        self.assertFalse(registry.get(self.db, "dev0")["operator_paused"])

    def test_automatic_sleep_is_lifted_when_the_window_names_it(self):
        self.sleep_all()
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])
        _window(self.db, 'burst', 'dev1')
        out = self.run_pass(2000)
        self.assertEqual([g["agents"] for g in out if g["kind"] == "wake"], [["dev1"]])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        self.assertIn("#8072", self.lift_event("dev1")["reason"])

    def test_human_pause_of_the_probe_is_preserved(self):
        agent_pause.change(self.db, "dev1", paused=True, reason="maintenance",
                           actor="human:alice", principal="human:alice")
        _window(self.db, 'burst', 'dev1')
        self.run_pass(2000)
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])


def load_tests(loader, tests, pattern):
    # Only this module's classes: the imported CloudCycleTest runs in 6252.
    suite = unittest.TestSuite()
    for cls in (HealthLineTest, RunProbeLineTest, RefusedReturnClosesTheProbeTest,
                WindowProbeSleepTest):
        for name in sorted(vars(cls)):
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite


if __name__ == '__main__':
    unittest.main()
