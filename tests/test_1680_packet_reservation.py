# SPDX-License-Identifier: AGPL-3.0-only
"""Packet CI and ordinary target writers exclude each other on real SQL."""
import json
import types
import unittest
from unittest import mock

from ameesh import land, packet_reservation as reservation, storage
from .support import PgTestCase


class ReservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        # PgTestCase's lot truncation does not clear the independent lease
        # table. Each scenario owns a fresh target lease in this test schema.
        self.db.execute("TRUNCATE merge_locks")
        self.ops = storage.of(self.db).work
        self.project = types.SimpleNamespace(name="nexlink")
        self.target = "origin/develop"
        self.key = land.lock_key(self.project, self.target)
        self.owner = "train:test-runner"
        self.epoch = self.ops.land_lock(self.key, self.owner, 1800)

    def ticket(self, **over):
        values = dict(owner=self.owner, epoch=self.epoch, branch="integration/packet-6",
                      base="a" * 40, head="b" * 40, ci_contract="c" * 64,
                      deadline=10000, now=100, ttl_s=1800)
        values.update(over)
        return reservation.begin(self.db, self.project, self.target, **values)

    def expire(self):
        self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp() - interval '1 second'"
                        " WHERE target = %s", (self.key,))

    @staticmethod
    def observation(ticket, state, **over):
        data = {k: getattr(ticket, k) for k in ("branch", "base", "head", "ci_contract")}
        data.update(kind=state, complete=True)
        data.update(over)
        return lambda _: data

    def test_begin_preserves_the_train_owner_epoch_and_json_identity(self):
        ticket = self.ticket()
        self.assertEqual((ticket.key, ticket.owner, ticket.epoch),
                         (self.key, self.owner, self.epoch))
        saved = json.loads(json.dumps(ticket.to_json()))
        self.assertEqual(reservation.Ticket.from_json(saved), ticket)
        self.assertEqual(reservation.next_renew(saved), 700)
        self.assertIsNone(self.ops.land_lock(self.key, "ordinary-writer", 1800))

    def test_maintenance_keeps_a_long_ci_exclusive_without_any_ci_read(self):
        ticket = self.ticket()
        # Only SQL expires_at moves. The reservation clock and all ordering
        # are explicit; no wall-clock delay or GitHub response is involved.
        for now in (700, 1300, 1900, 2500, 3100):
            self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp() + interval '60 seconds'"
                            " WHERE target = %s", (self.key,))
            with mock.patch.object(reservation, "observe", side_effect=AssertionError("CI polled")):
                ticket = reservation.renew(self.db, ticket, now=now)
            self.assertEqual(ticket.epoch, self.epoch)
            with self.assertRaisesRegex(reservation.ReservationError, "reserved"):
                reservation.WriterLease(self.db, self.project, self.target).acquire()

    def test_explicit_expired_epoch_is_not_revived_but_owner_restart_is_distinct(self):
        self.expire()
        with self.assertRaisesRegex(reservation.ReservationError, "expired"):
            self.ticket()
        row, = self.db.query("SELECT expires_at < clock_timestamp() AS expired FROM merge_locks"
                             " WHERE target = %s", (self.key,))
        self.assertTrue(row["expired"])
        newer = self.ops.land_lock(self.key, self.owner, 1800)
        self.assertGreater(newer, self.epoch)
        with self.assertRaisesRegex(reservation.ReservationError, "replaced"):
            self.ticket()
        self.assertEqual(self.ticket(epoch=newer).epoch, newer)

    def test_old_maintenance_and_terminal_event_cannot_release_a_new_holder(self):
        ticket = self.ticket()
        self.expire()
        newer = self.ops.land_lock(self.key, "other-runner", 1800)
        self.assertGreater(newer, self.epoch)
        read = mock.Mock()
        with self.assertRaises(reservation.ReservationError):
            reservation.observe(self.db, ticket, now=700, read=read)
        read.assert_not_called()
        self.assertFalse(self.ops.land_unlock(self.key, ticket.owner, ticket.epoch))
        self.assertIsNone(self.ops.land_lock(self.key, "third-runner", 1800))

    def test_deadline_expires_uncertain_dispatch_and_does_not_rearm(self):
        ticket = self.ticket(deadline=1000)
        ticket = reservation.renew(self.db, ticket, now=999)
        self.assertEqual(reservation.next_renew(ticket), 1000)
        with self.assertRaisesRegex(reservation.ReservationError, "absolute deadline"):
            reservation.renew(self.db, ticket, now=1000)
        self.assertIsNotNone(self.ops.land_lock(self.key, "next-packet", 1800))
        with self.assertRaises(reservation.ReservationError):
            reservation.renew(self.db, ticket, now=1001)
        self.assertIsNone(self.ops.land_lock(self.key, "third", 1800))

    def test_success_keeps_the_target_until_exact_landing(self):
        ticket = self.ticket()
        out = reservation.observe(self.db, ticket, now=200,
                                  read=self.observation(ticket, "passed"))
        self.assertEqual(out["state"], "retained")
        self.assertIsNone(self.ops.land_lock(self.key, "ordinary-writer", 1800))
        out = reservation.observe(self.db, out["ticket"], now=300,
                                  read=self.observation(ticket, "landed"))
        self.assertEqual(out, {"state": "released", "reason": "landed"})
        self.assertIsNotNone(self.ops.land_lock(self.key, "ordinary-writer", 1800))

    def test_qualified_failure_releases_but_stale_or_partial_run_does_not(self):
        ticket = self.ticket()
        for over in ({"head": "d" * 40}, {"base": "e" * 40},
                     {"ci_contract": "f" * 64}, {"branch": "integration/old"},
                     {"complete": False}, {"kind": "unknown"}):
            with self.subTest(over=over), self.assertRaises(reservation.ReservationError):
                reservation.observe(self.db, ticket, now=200,
                                    read=self.observation(ticket, "failed", **over))
            self.assertIsNone(self.ops.land_lock(self.key, "ordinary-writer", 1800))
        self.assertEqual(reservation.observe(self.db, ticket, now=200,
                         read=self.observation(ticket, "failed"))["state"], "released")
        self.assertIsNotNone(self.ops.land_lock(self.key, "ordinary-writer", 1800))

    def test_writer_excludes_packet_and_cannot_push_after_epoch_loss(self):
        self.ops.land_unlock(self.key, self.owner, self.epoch)
        writer = reservation.WriterLease(self.db, self.project, self.target)
        writer.acquire()
        self.assertIsNone(self.ops.land_lock(self.key, self.owner, 1800))
        self.expire()
        newer = self.ops.land_lock(self.key, self.owner, 1800)
        with self.assertRaisesRegex(reservation.ReservationError, "lost before push"):
            writer.refresh()
        writer.release()
        self.assertEqual(self.ops.land_lock(self.key, self.owner, 1800, epoch=newer), newer)

    def test_new_namespace_is_not_silently_substituted_for_the_held_key(self):
        # #1374 normalizes target aliases in `land.lock_key`: the lease held
        # here stays in the legacy namespace (raw target) that deployment must
        # reconcile, while the installed function returns the canonical form.
        # The port must not silently substitute that new key for the held one.
        legacy = "%s:%s" % (self.project.name, self.target)
        self.assertNotEqual(legacy, self.key)
        self.assertTrue(self.ops.land_unlock(self.key, self.owner, self.epoch))
        self.epoch = self.ops.land_lock(legacy, self.owner, 1800)
        with mock.patch.object(land, "lock_key", return_value=self.key):
            with self.assertRaisesRegex(reservation.ReservationError, "missing"):
                self.ticket()
        self.assertIsNone(self.ops.land_lock(legacy, "ordinary-writer", 1800))

    def test_invalid_ticket_or_time_never_writes_a_lease(self):
        for over in ({"epoch": None}, {"epoch": True}, {"branch": "agent/lot"},
                     {"base": "short"}, {"deadline": float("inf")},
                     {"deadline": 100}, {"ttl_s": 0}, {"ci_contract": "unknown"}):
            with self.subTest(over=over), mock.patch.object(storage, "of") as connect:
                with self.assertRaises(reservation.ReservationError):
                    self.ticket(**over)
                connect.assert_not_called()
        ticket = self.ticket()
        with mock.patch.object(storage, "of") as connect:
            with self.assertRaisesRegex(reservation.ReservationError, "backwards"):
                reservation.renew(self.db, ticket, now=99)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
