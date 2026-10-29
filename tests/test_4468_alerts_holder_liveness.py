# SPDX-License-Identifier: AGPL-3.0-only
"""#4468: the alerts pass follows liveness on every routed candidate.

`election.alerts_delegate` used to keep the pass on the lease holder as soon
as its standing was fresh and `notify_ok` was true. Both prove that the host
*could* push; neither proves that its `ameesh-role-alerts` pass runs. On
2026-10-29 the alerts pass of the holder (agents-1) was killed by its systemd
timeout (#4469) and the shared `notify_liveness` beat went stale while every
other candidate stood by "the holder can push": nothing was pushed to
human:smichea for ten minutes (#161260).

The same liveness rule now applies to **every** routed candidate, the holder
included. A host keeps the pass only while its own shared beat is fresh; for
the holder the beat must also belong to its **current tenure**
(`agent_registry.lease_since`), otherwise a beat written before the claim
would keep the new holder after its grace. Two bounded graces remain:

* a freshly claimed holder that has not beaten yet gets the first pass
  (`ALERTS_GRACE_S`): its route, when it returns, must take
  precedence (#3568/#3575), but not forever;
* the host the pass was last entrusted to (`alerts_delegations`, written by
  `role_run` under the shared execution lease) keeps it while it has not
  beaten yet and its grace runs, so a child that never starts cannot keep the
  pass for good.

When nobody proves a live pass, the routed candidate that has waited longest
gets it, never the one that just exhausted its grace without beating: the
pass leaves a delegate that stopped beating and comes back to a recovered
holder instead of staying on the oldest or the newest dead beat.

Witnesses: a beating holder keeps it; a holder that stopped beating hands it
over; the bounded holder grace and its expiry; a fresh beat from a previous
tenure does not keep the holder (review cx3); a delegate that stops beating
returns the pass to the holder (review auditeur); the memory grace and its
expiry; the execution-lease fence on the memory; the tenure trigger; the
`role status` and `role run` reads agreeing.
"""
from __future__ import annotations

import dataclasses
import time
import unittest
from unittest import mock

from ameesh import (alerts_delegation_pg, election, notify_gap, registry,
                    role_run, role_timers, storage)

from .support import PgTestCase

NAME = "pilote-4468"
OWNER = "runner-test"


class _HolderCase(PgTestCase):
    """Two routed candidates (`banc` holds the lease, `atelier` stands by)."""

    HOST = "banc"

    def setUp(self) -> None:
        super().setUp()
        self.cfg = dataclasses.replace(self.cfg, host=self.HOST)
        for table in ("role_standings", "role_handover", "agent_candidates",
                      "canon_state", "host_lifecycle", "host_holds",
                      "credential_health", "host_resources", "merge_locks",
                      "notify_liveness", "alerts_delegations"):
            self.db.execute("DELETE FROM " + table)
        # the role's harness need not exist on the bench (same fixture as the
        # election tests: the witness is about the route, not the harness)
        adapter = mock.patch("ameesh.adapters.adapter_for", return_value=object())
        adapter.start()
        self.addCleanup(adapter.stop)
        env = mock.patch.object(role_timers, "unit_env", return_value={})
        env.start()
        self.addCleanup(env.stop)

    def role(self, *, releve="election", hosts=("banc", "atelier"),
             responsible="human:owner"):
        registry.upsert(self.db, NAME, harness="claude", host=hosts[0], cwd="/work")
        self.db.execute(
            "UPDATE agent_registry SET releve = %s, candidats = %s::text[],"
            " responsible = %s WHERE name = %s",
            (releve, "{%s}" % ",".join(hosts), responsible, NAME))
        return registry.get(self.db, NAME)

    def standing(self, host: str, *, notify_ok: bool, score: float = 90.0,
                 ready: bool = True, evaluated_ts: float | None = None,
                 diagnostic: str = "") -> None:
        self.db.execute(
            "INSERT INTO role_standings"
            " (name, host, evaluated_at, availability, capacity, score, ready,"
            "  diagnostic, notify_ok, notify_diagnostic)"
            " VALUES (%s, %s, coalesce(to_timestamp(%s), clock_timestamp()),"
            "         %s, 0.5, %s, %s, %s, %s, %s)",
            (NAME, host, evaluated_ts, score, score + 0.5, ready, diagnostic,
             notify_ok,
             "" if notify_ok else "human:owner has no route on this host"))

    def hold(self, host: str = "banc", *, epoch: int = 7,
             expires_in: float = 300.0) -> dict:
        self.db.execute(
            "UPDATE agent_registry SET host = %s, lease_owner = %s,"
            " lease_epoch = %s, lease_expires_at = clock_timestamp()"
            " + make_interval(secs => %s) WHERE name = %s",
            (host, OWNER, epoch, expires_in, NAME))
        return registry.get(self.db, NAME)

    def tenure(self, *, age: float) -> None:
        """Move the current tenure's start `age` seconds into the past.

        `lease_since` is written by the trigger of lease_owner/epoch; updating
        the column alone does not fire it, so this builds an old tenure
        without pretending a claim.
        """
        self.db.execute(
            "UPDATE agent_registry SET lease_since = clock_timestamp()"
            " - make_interval(secs => %s) WHERE name = %s",
            (float(age), NAME))

    def beat(self, host: str, *, age: float = 0.0,
             human: str = "human:owner") -> None:
        """The shared liveness beat the alerts pass writes on its host."""
        storage.of(self.db).notify_liveness.beat(
            host, [human], now=time.time() - float(age))

    def memory(self, host: str, *, age: float = 0.0) -> None:
        """The handover memory `role_run` writes under the execution lease."""
        self.db.execute(
            "INSERT INTO alerts_delegations (role, host, entrusted_at)"
            " VALUES (%s, %s, clock_timestamp() - make_interval(secs => %s))"
            " ON CONFLICT (role) DO UPDATE SET host = excluded.host,"
            " entrusted_at = excluded.entrusted_at",
            (NAME, host, float(age)))

    def decide(self, *, holder: str = "banc") -> dict:
        return election.alerts_delegate(
            self.db, registry.get(self.db, NAME), holder=holder)


class HolderLivenessTest(_HolderCase):
    def setUp(self) -> None:
        super().setUp()
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=True)
        self.standing("atelier", notify_ok=True)

    def test_a_beating_holder_keeps_the_pass(self):
        self.tenure(age=60.0)
        self.beat("banc", age=5.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertTrue(decision["holder_notify_ok"])
        self.assertEqual(decision["last_executor"], "banc")
        self.assertIn("alerts pass beats", decision["reason"])

    def test_a_holder_that_stopped_beating_leaves_the_pass(self):
        # The beat is from the current tenure (after the claim) but older
        # than the deliverability window: the pass has stopped.
        self.tenure(age=notify_gap.ALIVE_S + 120.0)
        self.beat("banc", age=notify_gap.ALIVE_S + 60.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "atelier")
        self.assertFalse(decision["holder_notify_ok"])
        self.assertIn("banc stopped beating", decision["reason"])
        self.assertIn("atelier has waited longest", decision["reason"])

    def test_a_holder_that_has_not_beaten_yet_gets_a_bounded_grace(self):
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertTrue(decision["holder_notify_ok"])
        self.assertIn("holder can push", decision["reason"])
        self.assertIn("grace left", decision["reason"])
        # Past the grace with no beat in this tenure: the first routed
        # candidate in the election order takes the pass.
        self.tenure(age=election.ALERTS_GRACE_S + 30.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "atelier")
        self.assertIn("has not run the alerts pass in its current tenure",
                      decision["reason"])

    def test_a_stale_beat_from_a_previous_tenure_does_not_count(self):
        # The holder has beaten, but before it took its current lease: that
        # is a fresh-tenure grace, not proof the current pass runs.
        self.beat("banc", age=notify_gap.ALIVE_S + 120.0)
        self.tenure(age=10.0)
        self.assertEqual(self.decide()["delegate"], "banc")
        self.tenure(age=election.ALERTS_GRACE_S + 30.0)
        self.assertEqual(self.decide()["delegate"], "atelier")

    def test_a_fresh_beat_from_a_previous_tenure_does_not_keep_the_holder(self):
        # Review cx3: the beat is still inside ALIVE_S but older than the
        # lease. It proves nothing about this tenure, and the grace is spent.
        self.tenure(age=150.0)
        self.beat("banc", age=200.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "atelier")
        self.assertIn("has not run the alerts pass in its current tenure",
                      decision["reason"])
        # Inside the grace the same pre-tenure beat is harmless: the holder
        # still gets its first pass.
        self.tenure(age=10.0)
        self.assertEqual(self.decide()["delegate"], "banc")

    def test_the_holder_takes_the_pass_back_when_its_beat_returns(self):
        self.tenure(age=notify_gap.ALIVE_S + 120.0)
        self.beat("banc", age=notify_gap.ALIVE_S + 60.0)
        self.assertEqual(self.decide()["delegate"], "atelier")
        self.beat("banc", age=1.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertTrue(decision["holder_notify_ok"])
        self.assertIn("alerts pass beats", decision["reason"])

    def test_a_delegate_that_stops_beating_returns_the_pass(self):
        # Review auditeur: the pass must not stay on the newest dead beat
        # either. `banc` stopped first, `atelier` then runs and stops; the
        # candidate that has waited longest, `banc`, gets the pass back.
        self.tenure(age=notify_gap.ALIVE_S + 120.0)
        self.beat("banc", age=notify_gap.ALIVE_S + 60.0)
        self.assertEqual(self.decide()["delegate"], "atelier")
        self.beat("atelier", age=notify_gap.ALIVE_S + 30.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertIn("the holder takes it back", decision["reason"])

    def test_the_entrusted_delegate_gets_a_bounded_grace(self):
        self.tenure(age=notify_gap.ALIVE_S + 120.0)
        self.beat("banc", age=notify_gap.ALIVE_S + 60.0)
        self.assertEqual(self.decide()["delegate"], "atelier")
        # The wrapper of atelier just took the pass and has not beaten yet.
        self.memory("atelier", age=30.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "atelier")
        self.assertIn("just entrusted", decision["reason"])
        # Its grace is spent without a beat: another candidate takes over,
        # never the same dead hand.
        self.memory("atelier", age=election.ALERTS_GRACE_S + 30.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertIn("the holder takes it back", decision["reason"])

    def test_status_and_role_run_read_the_same_tenure(self):
        self.tenure(age=election.ALERTS_GRACE_S + 30.0)
        report = role_run.live(self.db, NAME)
        self.assertIsNotNone(report["lease_since_ts"])
        decision = role_run._alerts_delegation(self.db, report, host="banc")
        self.assertEqual(decision["delegate"], "atelier")
        status = election.status(self.db, NAME)
        self.assertEqual(status["alerts_delegate"]["delegate"], "atelier")


class OnlyRouteTest(_HolderCase):
    """No other candidate can push: the holder must not stand by."""

    def setUp(self) -> None:
        super().setUp()
        self.role(hosts=("banc",))
        self.hold("banc")
        self.standing("banc", notify_ok=True)

    def test_the_only_route_keeps_the_pass_without_a_beat(self):
        self.tenure(age=election.ALERTS_GRACE_S + 30.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertTrue(decision["holder_notify_ok"])
        self.assertIn("no other candidate proves a live alerts pass",
                      decision["reason"])

    def test_a_stale_beat_does_not_silence_the_only_route(self):
        self.tenure(age=election.ALERTS_GRACE_S + 30.0)
        self.beat("banc", age=notify_gap.ALIVE_S + 60.0)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertIn("no other candidate proves a live alerts pass",
                      decision["reason"])


class DelegationMemoryTest(_HolderCase):
    """`alerts_delegations` is written under the shared execution lease."""

    def setUp(self) -> None:
        super().setUp()
        self.role()

    def test_the_memory_is_fenced_by_the_execution_lease(self):
        lease = role_run._AlertsLease(self.db, NAME, "banc", ttl=60.0)
        self.assertIsNotNone(lease.acquire())
        owner, epoch = lease.owner, lease.epoch
        self.assertTrue(alerts_delegation_pg.entrust(
            self.db, NAME, "banc", lock_key=lease.key, owner=owner, epoch=epoch))
        self.assertEqual(alerts_delegation_pg.latest(self.db, NAME)["host"], "banc")
        lease.release()
        self.assertFalse(alerts_delegation_pg.entrust(
            self.db, NAME, "atelier", lock_key=lease.key, owner=owner, epoch=epoch))
        self.assertEqual(alerts_delegation_pg.latest(self.db, NAME)["host"], "banc")

    def test_a_second_handover_replaces_the_first(self):
        lease = role_run._AlertsLease(self.db, NAME, "banc", ttl=60.0)
        self.assertIsNotNone(lease.acquire())
        self.addCleanup(lease.release)
        self.assertTrue(alerts_delegation_pg.entrust(
            self.db, NAME, "banc", lock_key=lease.key, owner=lease.owner,
            epoch=lease.epoch))
        self.assertTrue(alerts_delegation_pg.entrust(
            self.db, NAME, "atelier", lock_key=lease.key, owner=lease.owner,
            epoch=lease.epoch))
        row = alerts_delegation_pg.latest(self.db, NAME)
        self.assertEqual(row["host"], "atelier")
        self.assertIsNotNone(row["entrusted_ts"])


class LeaseSinceTriggerTest(_HolderCase):
    """`lease_since` measures the current tenure for every lease writer."""

    def setUp(self) -> None:
        super().setUp()
        self.role()

    def since(self) -> float | None:
        return registry.get(self.db, NAME)["lease_since_ts"]

    def test_a_claim_starts_the_tenure(self):
        self.hold("banc")
        self.assertIsNotNone(self.since())

    def test_a_renewal_keeps_the_tenure(self):
        self.hold("banc")
        before = self.since()
        self.assertIsNotNone(
            registry.renew(self.db, NAME, OWNER, 7, 300.0))
        self.assertEqual(self.since(), before)

    def test_a_new_epoch_restarts_the_tenure(self):
        self.hold("banc")
        before = self.since()
        time.sleep(0.01)
        self.db.execute(
            "UPDATE agent_registry SET lease_epoch = lease_epoch + 1"
            " WHERE name = %s", (NAME,))
        after = self.since()
        self.assertIsNotNone(after)
        self.assertGreater(after, before)

    def test_a_release_clears_the_tenure(self):
        self.hold("banc")
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = NULL,"
            " lease_epoch = lease_epoch + 1 WHERE name = %s", (NAME,))
        self.assertIsNone(self.since())


class GraceAnchorTest(unittest.TestCase):
    """The grace is anchored on the database clocks, not the host clock."""

    def test_the_grace_covers_at_least_four_alerts_ticks(self):
        # The timer runs every 30 s (role_timers.ALERTS_INTERVAL_S): four
        # ticks cover a fresh executor, its first pass and a slow database.
        self.assertGreaterEqual(election.ALERTS_GRACE_S,
                                4 * role_timers.ALERTS_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
