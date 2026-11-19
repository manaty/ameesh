# SPDX-License-Identifier: AGPL-3.0-only
"""#7874: the closed return's probe is claimable, and the R1 entry guard.

R1 of #6002 (19/11): the adapter admitted `return_probe`, but the host's
runner only claims among `registry.claimable`, which kept the strict closed
predicate. The probe never played its turn; the return looped on `health
deadline passed`. The second half is `host release-check`, the guard that
names the open lots of agents placed on the host before a drain.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from ameesh import host_release, registry, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue
from tests.test_5925_vm_adapter import policy


class ClaimableReturnProbeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('DELETE FROM vm_adapter_observations')
        self.db.execute('TRUNCATE host_release_operations CASCADE')
        self.db.execute('DELETE FROM host_lifecycle')
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,hook_state) "
                        "VALUES ('burst','draining','tester',false,'draining','draining')")
        registry.upsert(self.db, 'burst-probe', harness='claude', host='burst')
        registry.upsert(self.db, 'burst-other', harness='codex', host='burst')
        self.q = Queue(self.db)

    def names(self):
        return sorted(a['name'] for a in registry.claimable(self.db, 'burst'))

    def returning(self, probe='burst-probe'):
        self.db.execute("UPDATE host_lifecycle SET mode='returning',return_probe=%s "
                        "WHERE host='burst'", (probe,))

    def test_only_the_named_probe_of_a_closed_return_is_claimable(self):
        self.assertEqual(self.names(), [])  # draining: closed to everyone
        self.returning()
        self.assertEqual(self.names(), ['burst-probe'])
        lease = registry.claim(self.db, 'burst-probe', 'runner-ag3', ttl_seconds=300)
        self.assertIsNotNone(lease)
        self.assertTrue(registry.begin_turn(self.db, 'burst-probe', 'runner-ag3',
                                            lease['lease_epoch'], 'probe turn'))
        self.assertIsNone(registry.claim(self.db, 'burst-other', 'runner-ag3', ttl_seconds=300))

    def test_closed_return_without_probe_lost_and_live_fence_stay_strict(self):
        self.returning(probe=None)
        self.assertEqual(self.names(), [])
        self.returning()
        self.db.execute("INSERT INTO host_release_operations(operation,host,manifest_digest) "
                        "VALUES ('op-7874','burst',repeat('a',64))")
        self.db.execute("INSERT INTO host_release_fences(host,operation,probe) "
                        "VALUES ('burst','op-7874','burst-other')")
        try:
            # L232b unchanged: a live fence offers nobody to scheduling.
            self.assertEqual(self.names(), [])
        finally:
            self.db.execute("DELETE FROM host_release_fences WHERE host='burst'")
            self.db.execute("DELETE FROM host_release_operations WHERE operation='op-7874'")
        self.assertEqual(self.names(), ['burst-probe'])
        self.db.execute("UPDATE host_lifecycle SET mode='lost' WHERE host='burst'")
        self.assertEqual(self.names(), [])

    def test_open_return_offers_every_agent_again(self):
        self.returning()
        self.db.execute("UPDATE host_lifecycle SET claims_open=true WHERE host='burst'")
        self.assertEqual(self.names(), ['burst-other', 'burst-probe'])

    def test_witness_a_real_return_reaches_healthy_through_the_probe_claim(self):
        """Adapter admits the probe, the runner finds and claims it, its turn
        starts, and the next tick acknowledges `busy` (ready)."""
        provider = mock.Mock()
        provider.state.return_value = 'running'
        operations = mock.Mock()
        operations.listing.return_value = []
        operations.open_lots_activity.return_value = []
        hosts = mock.Mock()
        hosts.holds.return_value = []
        store = SimpleNamespace(host_lifecycle=storage.of(self.db).host_lifecycle,
                                operations=operations, hosts=hosts)
        played = []

        def health(argv, timeout, pending=False):
            if argv == ['/health']:
                # The real health command waits for the receipt of a turn.
                return True if played else 'pending'
            return True

        control = self.q.control('burst')
        self.q.request('burst', control['generation'], control['release_epoch'], 'busy')
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours',"
                        "off_since=clock_timestamp()-interval '2 hours'")
        cfg = replace(self.cfg, idle_after_s=1800)

        def tick():
            with mock.patch('ameesh.vm_adapter.storage.of', return_value=store), \
                    mock.patch('ameesh.vm_adapter.host_release._fresh_reading',
                               return_value={'sampled_ts': 1}):
                return vm.tick_host(cfg, self.db, policy(), 'burst', provider, probe=health,
                                    evidence=lambda *a, **k: {})

        self.assertEqual(tick(), 'probe admitted')
        self.assertEqual(self.names(), ['burst-probe'])
        lease = registry.claim(self.db, 'burst-probe', 'runner-ag3', ttl_seconds=300)
        self.assertTrue(registry.begin_turn(self.db, 'burst-probe', 'runner-ag3',
                                            lease['lease_epoch'], 'probe turn'))
        played.append(True)
        self.assertEqual(tick(), 'ready')


AG3 = 'ameesh-agents-3'


def agent(name, admitted, **over):
    return dict(dict(name=name, host=AG3, admitted_hosts=admitted, mode='execute',
                     status='idle', actionable_lots=0, unread=0), **over)


class ReleaseCheckTest(unittest.TestCase):
    def setUp(self):
        # The R1 of 19/11, reduced: impl-12 and impl-15 admitted on AG3 only,
        # impl-13 also on AG1 (open), a pilot elected elsewhere.
        self.listing = [agent('impl-12', [AG3], actionable_lots=1),
                        agent('impl-15', [AG3], actionable_lots=1),
                        agent('impl-13', [AG3, 'ameesh-agents-1'], actionable_lots=1),
                        agent('pilote', [AG3, 'ameesh-agents-1'], releve='election'),
                        dict(agent('ailleurs', ['ameesh-agents-1']), host='ameesh-agents-1')]
        self.lots = [dict(id=7873, state='qa', assignee='impl-12', title='a'),
                     dict(id=7871, state='validated', assignee='impl-15', title='b'),
                     dict(id=7860, state='build', assignee='impl-13', title='c'),
                     dict(id=7000, state='closed', assignee='impl-13', title='d'),
                     dict(id=7001, state='build', assignee='pilote', title='e'),
                     dict(id=7002, state='build', assignee='ailleurs', title='f')]
        self.controls = {'ameesh-agents-1': dict(host='ameesh-agents-1', mode='available',
                                                 claims_open=True)}

    def test_names_every_open_lot_of_placed_agents_and_the_wake_branch(self):
        out = host_release.release_check(AG3, self.listing, self.lots,
                                         controls=self.controls, holds={})
        self.assertFalse(out['ok'])
        self.assertEqual([l['id'] for l in out['blockers']], [7873, 7860, 7871])
        # impl-13 can run on AG1: it would not wake AG3 (relocation path).
        self.assertEqual(out['return_targets'], ['impl-12', 'impl-15'])

    def test_same_branch_decides_wake_due_on_a_draining_host(self):
        control = dict(host=AG3, mode='draining', claims_open=False)
        controls = dict(self.controls, **{AG3: control})
        self.assertTrue(vm.wake_due(AG3, control, self.listing, [], controls, {},
                                    now=0, horizon=900, target={}))
        quiet = [dict(a, actionable_lots=0) for a in self.listing]
        self.assertFalse(vm.wake_due(AG3, control, quiet, [], controls, {},
                                     now=0, horizon=900, target={}))

    def test_clear_once_the_lots_are_finished_or_moved(self):
        lots = [dict(l, assignee='ailleurs') if l['id'] in (7873, 7871, 7860) else l
                for l in self.lots]
        listing = [dict(a, actionable_lots=0) for a in self.listing]
        out = host_release.release_check(AG3, listing, lots, controls=self.controls, holds={})
        self.assertEqual(out, {'host': AG3, 'ok': True, 'blockers': [], 'return_targets': []})



class ListingReleveTest(PgTestCase):
    """B of mty-amh-docs-01 (freeze #44304): the real `Operations.listing()`
    did not select `releve`, so an elected role was read as placed."""

    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM host_lifecycle')
        registry.upsert(self.db, 'placed', harness='claude', host=AG3)
        registry.upsert(self.db, 'pilot', harness='claude', host=AG3)
        self.db.execute("UPDATE agent_registry SET releve='election', candidats=%s, "
                        "admitted_hosts=%s WHERE name='pilot'",
                        ([AG3, 'ameesh-agents-1'], [AG3]))
        self.db.execute("UPDATE agent_registry SET admitted_hosts=%s WHERE name='placed'", ([AG3],))

    def test_listing_carries_releve_and_the_guard_skips_elected_roles(self):
        listing = storage.of(self.db).operations.listing()
        rows = {r['name']: r for r in listing if r['name'] in ('placed', 'pilot')}
        self.assertEqual(rows['pilot']['releve'], 'election')
        lots = [dict(id=1, state='build', assignee='placed', title='a'),
                dict(id=2, state='build', assignee='pilot', title='b')]
        listing = [dict(r, actionable_lots=1) if r['name'] in rows else r for r in listing]
        out = host_release.release_check(AG3, listing, lots, controls={}, holds={})
        self.assertEqual([l['id'] for l in out['blockers']], [1])
        self.assertEqual(out['return_targets'], ['placed'])


if __name__ == '__main__':
    unittest.main()
