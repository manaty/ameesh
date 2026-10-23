# SPDX-License-Identifier: AGPL-3.0-only
"""#3142 : le porteur du bail train-service adopte les réservations train:<runner>
de l'ancien porteur, fencées par l'époque, au lieu d'attendre leur TTL."""
from __future__ import annotations

import types

from ameesh import (land, packet_reservation, storage, train, train_reservation,
                    train_runner, train_store)
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


if __name__ == "__main__":
    import unittest
    unittest.main()
