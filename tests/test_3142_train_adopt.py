# SPDX-License-Identifier: AGPL-3.0-only
"""#3142 : le porteur du bail train-service adopte les réservations train:<runner>
de l'ancien porteur, fencées par l'époque, au lieu d'attendre leur TTL."""
from __future__ import annotations

import threading
import time
import types

from ameesh import (land, packet_reservation, registry, storage, train,
                    train_reservation, train_runner, train_store)
from .support import PgTestCase
from .test_1467_train import MemoryPorts, row

CONTRACT = "c" * 64


class LedgerPorts(MemoryPorts):
    """In-memory remote effects; reservation and train state on real SQL."""

    def __init__(self, ledger, rows):
        super().__init__(rows)
        self.ledger = ledger

    def acquire(self, epoch):
        return self.ledger.acquire(epoch)

    def release(self, epoch):
        self.ledger.release(epoch)

    def load(self):
        return self.ledger.load()

    def save(self, state, expected, epoch):
        return self.ledger.save(state, expected, epoch)

    def publish(self, plan, epoch):
        self.ledger.before_publish(plan, epoch)
        self.effects.append(("publish", plan["head"], self.ledger.owner, epoch))
        if getattr(self, "lost", False):
            raise train.TrainError("push response lost")


class AdoptionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE integration_trains, integration_train_fires, merge_locks")
        self.ops = storage.of(self.db).work
        self.project = types.SimpleNamespace(name="ameesh")
        self.target = "origin/main"
        self.key = land.lock_key(self.project, self.target)
        self.now = [1000.0]
        self.reservation = train_reservation.Reservation(
            packet_reservation, deadline=lambda plan, now: now + 7200,
            clock=lambda: self.now[0])
        cfg = types.SimpleNamespace(runner="runner-b@host-b", host="host-b")
        self.lease = train_runner.HostLease(cfg, ttl=900)

    def ledger(self, runner, *, service=None):
        return self.reservation.bind(self.db, self.project, self.target, runner, CONTRACT,
                                     service=service)

    def service(self):
        return (self.lease.key, self.lease.owner, self.lease.epoch) \
            if self.lease.epoch is not None else None

    def engine(self, ledger):
        ports = LedgerPorts(ledger, [row(1)])
        return train.Engine(ports, source="github.com/o/r", target="main",
                            policy=train.Policy(enabled=True, build_beta=False)), ports

    def lock(self):
        held, = self.db.query("SELECT owner, epoch FROM merge_locks WHERE target = %s",
                              (self.key,))
        return held["owner"], int(held["epoch"])

    def left_publishing(self):
        """Runner A composes, reserves, then stops with its push uncertain."""
        old, ports = self.engine(self.ledger("runner-a@host-a"))
        ports.lost = True
        self.assertEqual(old.step(3600)["result"], "publication-uncertain")
        state = self.ops.train_read(self.key)
        self.assertEqual(state["phase"], "publishing")
        self.assertEqual(self.lock(), ("train:runner-a@host-a", old.epoch))
        return old, ports, state

    def test_new_holder_resumes_the_publication_and_fences_the_old_epoch(self):
        old, old_ports, state = self.left_publishing()
        before = old.epoch
        self.assertTrue(self.lease.acquire(self.db, now=1000))
        new, ports = self.engine(self.ledger("runner-b@host-b", service=self.service))
        ports.base = old_ports.base
        self.now[0] = 1100.0
        # No wait for the 1800 s TTL: the plan resumes at once, same head.
        self.assertEqual(new.step(3700)["result"], "ci")
        self.assertEqual(new.epoch, before + 1)
        self.assertEqual(ports.effects[0], ("publish", state["plan"]["head"],
                                            "train:runner-b@host-b", before + 1))
        self.assertEqual(self.lock(), ("train:runner-b@host-b", before + 1))
        saved = self.ops.train_read(self.key)
        self.assertEqual(saved["phase"], "ci")
        self.assertEqual(saved["plan"], state["plan"])
        self.assertEqual((saved["reservation"]["owner"], saved["reservation"]["epoch"]),
                         ("train:runner-b@host-b", before + 1))
        # The old holder can no longer write, renew, publish or unlock.
        self.assertIsNone(old.ports.acquire(before))
        self.assertEqual(old.step(3800)["result"], "busy")
        with self.assertRaises(train.TrainError):
            old.ports.save(saved, saved["revision"], before)
        with self.assertRaises(train.TrainError):
            old.ports.ledger.before_publish(saved["plan"], before)
        self.assertFalse(self.ops.land_unlock(self.key, "train:runner-a@host-a", before))
        self.assertEqual(self.lock(), ("train:runner-b@host-b", before + 1))

    def test_a_promotion_claim_of_the_old_epoch_is_refused_after_adoption(self):
        old, _, state = self.left_publishing()
        before = old.epoch
        ledger = old.ports.ledger
        state["phase"] = "ready"
        state = ledger.save(state, state["revision"], before)
        fire = self.ops.train_fire_give(self.key, dict(plan=state["plan"], actor="pilot"))
        state["phase"] = "promoting"
        state = ledger.save(state, state["revision"], before)
        self.assertTrue(self.lease.acquire(self.db, now=1000))
        new = self.ledger("runner-b@host-b", service=self.service)
        self.assertEqual(new.acquire(None), before + 1)
        self.assertIsNone(self.ops.train_fire_change(
            self.key, fire["id"], "promoting", actor="old",
            owner="train:runner-a@host-a", epoch=before))
        claimed = self.ops.train_fire_change(
            self.key, fire["id"], "promoting", actor="new",
            owner="train:runner-b@host-b", epoch=before + 1)
        self.assertEqual(claimed["state"], "promoting")

    def test_only_the_live_service_holder_adopts(self):
        old, _, _ = self.left_publishing()
        held = self.lock()
        self.assertIsNone(self.ledger("runner-b@host-b").acquire(None))
        self.assertTrue(self.lease.acquire(self.db, now=1000))
        stale = (self.lease.key, self.lease.owner, self.lease.epoch)
        self.assertTrue(self.lease.release(self.db))
        self.assertIsNone(self.ledger("runner-b@host-b", service=lambda: stale).acquire(None))
        self.assertEqual(self.lock(), held)

    def test_a_service_lease_expiring_during_the_lock_wait_adopts_nothing(self):
        # cx2 B1: the service check passes, the target lock waits, the
        # service lease expires meanwhile; nothing may move afterwards.
        old, _, state = self.left_publishing()
        held = self.lock()
        self.assertTrue(self.lease.acquire(self.db, now=1000))
        self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp()"
                        " + interval '1 second' WHERE target = %s", (self.lease.key,))
        adopting, blocker = self.connect(), self.connect()
        self.addCleanup(adopting.close)
        self.addCleanup(blocker.close)
        result = {}

        def adopt():
            result["adopted"] = storage.of(adopting).work.train_adopt(
                self.key, "train:runner-b@host-b", revision=state["revision"],
                previous=held, service=self.service(), ttl_s=1800)

        with blocker.transaction() as tx:
            tx.query("SELECT 1 FROM merge_locks WHERE target = %s FOR UPDATE", (self.key,))
            thread = threading.Thread(target=adopt)
            thread.start()
            deadline = time.monotonic() + 5
            while not self.db.query("SELECT 1 FROM pg_locks WHERE NOT granted") \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            tx.query("SELECT pg_sleep(1.2)")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, {"adopted": None})
        self.assertEqual(self.lock(), held)
        ticket = self.ops.train_read(self.key)["reservation"]
        self.assertEqual(("train:runner-a@host-a", old.epoch), (ticket["owner"], ticket["epoch"]))

    def test_a_moved_epoch_or_an_ordinary_reservation_is_never_adopted(self):
        old, _, state = self.left_publishing()
        self.assertTrue(self.lease.acquire(self.db, now=1000))
        # The old runner restarted on the target at a newer epoch than its
        # ticket: the ticket no longer names the row, nothing moves.
        newer = self.ops.land_lock(self.key, "train:runner-a@host-a", 1800)
        self.assertIsNone(self.ledger("runner-b@host-b", service=self.service).acquire(None))
        self.assertEqual(self.lock(), ("train:runner-a@host-a", newer))
        # An ordinary single-lot writer is never a train's to take.
        self.ops.land_unlock(self.key, "train:runner-a@host-a", newer)
        self.db.execute("DELETE FROM integration_trains")
        writer = packet_reservation.WriterLease(self.db, self.project, self.target)
        writer.acquire()
        plain = train_store.Ledger(self.db, self.project, self.target, "runner-b@host-b",
                                   service=self.service)
        self.assertIsNone(plain.acquire(None))
        self.assertEqual(self.lock(), (writer.owner, writer.epoch))

    def test_a_reservation_held_without_a_plan_is_adopted(self):
        # The old runner stopped while composing: no ticket, only the row.
        epoch = self.ops.land_lock(self.key, "train:runner-a@host-a", 1800)
        self.assertTrue(self.lease.acquire(self.db, now=1000))
        plain = train_store.Ledger(self.db, self.project, self.target, "runner-b@host-b",
                                   service=self.service)
        self.assertEqual(plain.acquire(None), epoch + 1)
        self.assertEqual(plain.adopted["previous_owner"], "train:runner-a@host-a")
        self.assertEqual(self.lock(), ("train:runner-b@host-b", epoch + 1))
        self.assertIsNone(self.ops.land_lock(self.key, "train:runner-a@host-a", 1800,
                                             epoch=epoch))

    def test_an_old_role_epoch_never_adopts_the_new_holders_reservation(self):
        """#3142 × #3014 (B cx2) : la clé de service d'une ancienne élection
        survit 900 s ; l'adoption prouve l'époque du rôle vivante, sinon
        l'ancien élu reprendrait la cible au nouvel élu."""
        role = "mesh-design"
        registry.upsert(self.db, role, chantier="ameesh", harness="claude",
                        host="host-b", cwd="/tmp", session_id=None, status="idle",
                        status_text="", model=None, budget_usd=None)
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', lease_owner = 'runner-b',"
            " lease_epoch = 2, lease_expires_at = clock_timestamp() + interval '10 minutes'"
            " WHERE name = %s", (role,))
        old_key, new_key = ("train-service:%s:1" % role, "train-service:%s:2" % role)
        old_owner, new_owner = "train-service:runner-a:old", "train-service:runner-b:new"
        self.assertEqual(self.ops.land_lock(old_key, old_owner, 900), 1)
        self.assertEqual(self.ops.land_lock(new_key, new_owner, 900), 1)
        held = self.ops.land_lock(self.key, "train:runner-a@host-a", 1800)
        # L'ancien élu (époque de rôle 1) ne reprend pas la cible du nouvel
        # élu (époque de rôle 2) : la clé de service est vivante, le rôle non.
        self.assertIsNone(self.ops.train_adopt(
            self.key, "train:runner-b@host-b", revision=0, previous=None,
            service=(old_key, old_owner, 1), ttl_s=1800))
        self.assertEqual(self.lock(), ("train:runner-a@host-a", held))
        # Le nouvel élu, lui, adopte.
        adopted = self.ops.train_adopt(
            self.key, "train:runner-b@host-b", revision=0, previous=None,
            service=(new_key, new_owner, 1), ttl_s=1800)
        self.assertEqual((adopted["previous_owner"], adopted["epoch"]),
                         ("train:runner-a@host-a", held + 1))
        self.assertEqual(self.lock(), ("train:runner-b@host-b", held + 1))
        # Le bail de rôle expiré ne prouve plus l'époque : rien ne s'adopte.
        self.db.execute("UPDATE agent_registry SET lease_expires_at = clock_timestamp()"
                        " - interval '1 second' WHERE name = %s", (role,))
        self.assertIsNone(self.ops.train_adopt(
            self.key, "train:runner-a@host-a", revision=0, previous=None,
            service=(new_key, new_owner, 1), ttl_s=1800))
        self.assertEqual(self.lock(), ("train:runner-b@host-b", held + 1))

    def test_a_role_lease_expiring_during_the_lock_wait_adopts_nothing(self):
        """B/P1 cx2 (#3142×#3014) : le rôle vivant au premier contrôle peut
        expirer pendant l'attente du verrou cible ; FOR SHARE ne suspend pas
        le temps, le recontrôle final doit refuser l'adoption."""
        role = "mesh-design"
        registry.upsert(self.db, role, chantier="ameesh", harness="claude",
                        host="host-b", cwd="/tmp", session_id=None, status="idle",
                        status_text="", model=None, budget_usd=None)
        # Le bail de rôle est vivant au premier contrôle puis expire pendant
        # l'attente du verrou cible ; le bail de service, lui, reste vivant.
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', lease_owner = 'runner-b',"
            " lease_epoch = 2, lease_expires_at = clock_timestamp() + interval '2 seconds'"
            " WHERE name = %s", (role,))
        key, owner = "train-service:%s:2" % role, "train-service:runner-b:new"
        self.assertEqual(self.ops.land_lock(key, owner, 900), 1)
        held = self.ops.land_lock(self.key, "train:runner-a@host-a", 1800)
        adopting, blocker = self.connect(), self.connect()
        self.addCleanup(adopting.close)
        self.addCleanup(blocker.close)
        result = {}

        def adopt():
            result["adopted"] = storage.of(adopting).work.train_adopt(
                self.key, "train:runner-b@host-b", revision=0, previous=None,
                service=(key, owner, 1), ttl_s=1800)

        with blocker.transaction() as tx:
            tx.query("SELECT 1 FROM merge_locks WHERE target = %s FOR UPDATE", (self.key,))
            thread = threading.Thread(target=adopt)
            thread.start()
            deadline = time.monotonic() + 5
            while not self.db.query("SELECT 1 FROM pg_locks WHERE NOT granted") \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            tx.query("SELECT pg_sleep(3.0)")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, {"adopted": None})
        self.assertEqual(self.lock(), ("train:runner-a@host-a", held))


if __name__ == "__main__":
    import unittest
    unittest.main()
