# SPDX-License-Identifier: AGPL-3.0-only
"""#7672 — contrat public 1.3, E2b : demande en attente et réponse corrélée.

Le banc vérifie la surface ``ameesh.public.mail`` telle que le contrat écrit
la décrit (`docs/api/module-contract-v1.md`) : une demande n'est « en attente »
que si elle est la consigne du tour natif courant, sous un bail vivant et son
marqueur comptable ; une réponse est corrélée à la demande, adressée à son
expéditeur, unique, et un rejeu identique rend le même identifiant.

Postgres réel. Les cas de concurrence ouvrent une connexion par fil : le même
processus ne prouverait rien, le verrou de ligne du registre sérialiserait les
deux transactions avant même l'insert.
"""
from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from ameesh import db as db_mod, mail, registry, storage
from ameesh.public import mail as public_mail
from ameesh.public.mail import (
    InvalidSubmission, Lease, PendingPage, PendingRequest, Receipt,
    RecipientRefused, Reply, ReplyConflict, ScopeRefused, Submission,
    UnsupportedStorage,
)

from .support import PgTestCase

OWNER = "runner-e2b"
TURN = "turn-e2b-1"
LOT = 7672


class PendingReplyBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        # PgTestCase truncates the mailbox and the registry but not the
        # accounting marker; this class creates one in most methods.
        self.db.execute("DELETE FROM spend_pending")
        for name in ("alpha", "beta", "gamma"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host)
        lease = registry.claim(self.db, "beta", OWNER, 600)
        self.assertIsNotNone(lease)
        self.epoch = int(lease["lease_epoch"])
        self.lease = Lease(OWNER, self.epoch)

    def submission(self, sender: str = "alpha", body: str = "question",
                   **overrides) -> Submission:
        data = {"recipient": "beta", "body": body, "kind": "request"}
        data.update(overrides)
        return Submission(**data)

    def request(self, sender: str = "alpha", body: str = "question",
                **overrides) -> Receipt:
        return public_mail.submit(self.db, sender, self.submission(
            sender=sender, body=body, **overrides))

    def hold(self, request_id: int, *, turn: str = TURN,
             deliver: bool = True, marker: bool = True) -> str:
        """Make one request the prompt of the native turn (reserve + marker)."""
        token = mail.new_token()
        reserved = mail.reserve(
            self.db, "beta", OWNER, self.epoch, token,
            ids=[request_id], porteur="consigne", turn_id=turn)
        self.assertEqual([int(row["id"]) for row in reserved], [request_id])
        if marker:
            self.assertIsNotNone(registry.pending_spend_begin(
                self.db, "beta", 0, "consigne", "model-e2b", turn_id=turn))
        if deliver:
            delivered = mail.deliver(
                self.db, "beta", OWNER, self.epoch, token, [request_id])
            self.assertEqual(delivered, [request_id])
        return token

    def rows(self) -> list[dict]:
        return self.db.query(
            "SELECT id, sender, recipient, body, kind, payload, work_item_id,"
            " in_reply_to, delivered_at FROM agent_mailbox ORDER BY id")

    def reply_row(self, request_id: int) -> dict:
        rows = self.db.query(
            "SELECT id, sender, recipient, body, kind, payload, work_item_id,"
            " in_reply_to FROM agent_mailbox WHERE in_reply_to = %s",
            (request_id,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def bodies(self, page: PendingPage) -> list[str]:
        return [entry.body for entry in page.entries]


class PendingTest(PendingReplyBase):
    def test_pending_lists_the_current_turn_request(self):
        request = self.request(body="the question")
        self.hold(request.id)
        page = public_mail.pending(self.db, "beta", self.lease)
        self.assertIsInstance(page, PendingPage)
        self.assertFalse(page.has_more)
        [entry] = page.entries
        self.assertIsInstance(entry, PendingRequest)
        self.assertEqual(entry.id, request.id)
        self.assertEqual((entry.sender, entry.recipient), ("alpha", "beta"))
        self.assertEqual(entry.body, "the question")
        self.assertEqual(entry.work_item_id, "")
        self.assertEqual(entry.turn_id, TURN)
        self.assertIsNotNone(entry.delivered_ts)
        self.assertEqual(json.loads(json.dumps(page.as_dict()))["entries"][0]["id"],
                         request.id)

    def test_pending_includes_a_reserved_but_not_delivered_request(self):
        request = self.request(body="not shown yet")
        self.hold(request.id, deliver=False)
        [entry] = public_mail.pending(self.db, "beta", self.lease).entries
        self.assertEqual(entry.id, request.id)
        self.assertIsNone(entry.delivered_ts)

    def test_pending_is_fenced_by_owner_epoch_and_expiry(self):
        request = self.request()
        self.hold(request.id)
        for lease in (Lease("another-runner", self.epoch),
                      Lease(OWNER, self.epoch + 1)):
            with self.subTest(lease=lease), self.assertRaises(ScopeRefused):
                public_mail.pending(self.db, "beta", lease)
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second'"
            " WHERE name = 'beta'")
        with self.assertRaises(ScopeRefused):
            public_mail.pending(self.db, "beta", self.lease)

    def test_a_marker_of_another_turn_hides_the_old_request(self):
        request = self.request(body="turn one")
        self.hold(request.id, turn="turn-one")
        # A new turn starts: the marker names turn-two, the old consigne does
        # not. The old request is not the prompt of this turn anymore.
        registry.pending_spend_begin(
            self.db, "beta", 1, "consigne", "model-e2b", turn_id="turn-two")
        self.assertEqual(public_mail.pending(self.db, "beta", self.lease).entries,
                         ())

    def test_a_request_outside_the_turn_prompt_is_not_pending(self):
        self.hold(self.request(body="the prompt").id)
        unread = self.request(body="ordinary unread")
        public_mail.pending(self.db, "beta", self.lease)
        ids = [entry.id for entry in
               public_mail.pending(self.db, "beta", self.lease).entries]
        self.assertNotIn(unread.id, ids)

    def test_no_current_marker_yields_an_empty_page(self):
        request = self.request()
        self.hold(request.id, marker=False)
        page = public_mail.pending(self.db, "beta", self.lease)
        self.assertEqual(page.entries, ())
        self.assertFalse(page.has_more)

    def test_an_answered_request_leaves_pending(self):
        request = self.request()
        self.hold(request.id)
        public_mail.reply(self.db, "beta", request.id,
                          Reply(body="answer"), lease=self.lease)
        self.assertEqual(public_mail.pending(self.db, "beta", self.lease).entries,
                         ())

    def test_a_withdrawn_request_is_not_pending_and_cannot_be_answered(self):
        request = self.request()
        self.hold(request.id)
        self.db.execute(
            "UPDATE agent_mailbox SET meta = meta || %s::jsonb WHERE id = %s",
            (json.dumps({"retire": {"reason": "test"}}), request.id))
        self.assertEqual(public_mail.pending(self.db, "beta", self.lease).entries,
                         ())
        with self.assertRaises(ScopeRefused):
            public_mail.reply(self.db, "beta", request.id,
                              Reply(body="too late"), lease=self.lease)

    def test_sender_filter_and_ordering(self):
        first = self.request(sender="alpha", body="one")
        second = self.request(sender="gamma", body="two")
        third = self.request(sender="alpha", body="three")
        token = mail.new_token()
        reserved = mail.reserve(
            self.db, "beta", OWNER, self.epoch, token,
            ids=[first.id, second.id, third.id], porteur="consigne",
            turn_id=TURN)
        self.assertEqual(len(reserved), 3)
        registry.pending_spend_begin(
            self.db, "beta", 0, "consigne", "model-e2b", turn_id=TURN)
        page = public_mail.pending(self.db, "beta", self.lease, sender="alpha")
        self.assertEqual(self.bodies(page), ["one", "three"])
        self.assertTrue(all(entry.sender == "alpha" for entry in page.entries))

    def test_page_is_bounded_and_reports_more(self):
        requests = [self.request(body="q%d" % index) for index in range(3)]
        token = mail.new_token()
        mail.reserve(
            self.db, "beta", OWNER, self.epoch, token,
            ids=[request.id for request in requests], porteur="consigne",
            turn_id=TURN)
        registry.pending_spend_begin(
            self.db, "beta", 0, "consigne", "model-e2b", turn_id=TURN)
        page = public_mail.pending(self.db, "beta", self.lease, limit=2)
        self.assertEqual(self.bodies(page), ["q0", "q1"])
        self.assertTrue(page.has_more)
        with self.assertRaises(InvalidSubmission):
            public_mail.pending(self.db, "beta", self.lease,
                                limit=public_mail.MAX_PENDING + 1)
        for bad in (0, -1, True, "2"):
            with self.subTest(limit=bad), self.assertRaises(InvalidSubmission):
                public_mail.pending(self.db, "beta", self.lease, limit=bad)

    def test_pending_validates_the_lease_and_sender(self):
        for bad in (None, "lease", {"owner": OWNER, "epoch": self.epoch},
                    Lease("", self.epoch), Lease("two words", self.epoch),
                    Lease(OWNER, 0), Lease(OWNER, True), Lease(OWNER, "1")):
            with self.subTest(lease=bad), self.assertRaises(InvalidSubmission):
                public_mail.pending(self.db, "beta", bad)
        with self.assertRaises(InvalidSubmission):
            public_mail.pending(self.db, "beta", self.lease, sender="bad name")

    def test_pending_is_read_only(self):
        request = self.request()
        self.hold(request.id)
        before = self.rows()
        public_mail.pending(self.db, "beta", self.lease)
        self.assertEqual(self.rows(), before)

    def test_mediated_driver_is_not_served(self):
        with mock.patch.object(public_mail.storage, "is_remote",
                               return_value=True):
            with self.assertRaises(UnsupportedStorage):
                public_mail.pending(self.db, "beta", self.lease)
            with self.assertRaises(UnsupportedStorage):
                public_mail.reply(self.db, "beta", 1, Reply(body="x"),
                                  lease=self.lease)


class ReplyTest(PendingReplyBase):
    def test_reply_is_correlated_addressed_and_inherits_the_lot(self):
        request = self.request(body="please answer", payload={"module": {"doc": 7}},
                               work_item_id=LOT)
        self.hold(request.id)
        receipt = public_mail.reply(
            self.db, "beta", request.id,
            Reply(body="the answer", payload={"module": {"result": "ok"}}),
            lease=self.lease)
        self.assertIsInstance(receipt, Receipt)
        self.assertEqual(receipt.kind, "reply")
        self.assertEqual((receipt.actor, receipt.recipient), ("beta", "alpha"))
        self.assertEqual(receipt.in_reply_to, request.id)
        self.assertIsNone(receipt.request_id)
        self.assertFalse(receipt.reused)
        row = self.reply_row(request.id)
        self.assertEqual(row["id"], receipt.id)
        self.assertEqual((row["sender"], row["recipient"]), ("beta", "alpha"))
        self.assertEqual(row["body"], "the answer")
        self.assertEqual(row["kind"], "reply")
        self.assertEqual(row["in_reply_to"], request.id)
        self.assertEqual(row["work_item_id"], str(LOT))
        self.assertEqual(row["payload"]["module"], {"result": "ok"})
        self.assertEqual(json.loads(json.dumps(receipt.as_dict()))["in_reply_to"],
                         request.id)

    def test_identical_retry_reuses_the_durable_reply(self):
        request = self.request()
        self.hold(request.id)
        first = public_mail.reply(
            self.db, "beta", request.id,
            Reply(body="same", payload={"module": {"n": 1}}), lease=self.lease)
        second = public_mail.reply(
            self.db, "beta", request.id,
            Reply(body="same", payload={"module": {"n": 1}}), lease=self.lease)
        self.assertEqual(second.id, first.id)
        self.assertTrue(second.reused)
        self.assertEqual(len(self.rows()), 2)  # the request and its reply

    def test_another_body_or_payload_is_a_deterministic_conflict(self):
        request = self.request()
        self.hold(request.id)
        public_mail.reply(self.db, "beta", request.id,
                          Reply(body="original", payload={"module": {"n": 1}}),
                          lease=self.lease)
        for changed in (Reply(body="other", payload={"module": {"n": 1}}),
                        Reply(body="original", payload={"module": {"n": 2}})):
            with self.subTest(reply=changed), self.assertRaises(ReplyConflict):
                public_mail.reply(self.db, "beta", request.id, changed,
                                  lease=self.lease)
        self.assertEqual(self.reply_row(request.id)["body"], "original")

    def test_a_stale_lease_is_refused_before_reading_the_request(self):
        request = self.request()
        self.hold(request.id)
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-new',"
            " lease_epoch = lease_epoch + 1,"
            " lease_expires_at = now() + interval '1 hour' WHERE name = 'beta'")
        for lease in (self.lease, Lease("runner-new", self.epoch)):
            with self.subTest(lease=lease), self.assertRaises(ScopeRefused):
                public_mail.reply(self.db, "beta", request.id,
                                  Reply(body="stale"), lease=lease)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE kind = 'reply'"),
            [{"n": 0}])

    def test_expired_lease_is_refused(self):
        request = self.request()
        self.hold(request.id)
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second'"
            " WHERE name = 'beta'")
        with self.assertRaises(ScopeRefused):
            public_mail.reply(self.db, "beta", request.id, Reply(body="late"),
                              lease=self.lease)

    def test_a_request_of_another_agent_is_refused(self):
        request = self.request()
        self.hold(request.id)
        other = registry.claim(self.db, "alpha", "runner-alpha", 600)
        self.assertIsNotNone(other)
        with self.assertRaises(ScopeRefused):
            public_mail.reply(
                self.db, "alpha", request.id, Reply(body="not mine"),
                lease=Lease("runner-alpha", int(other["lease_epoch"])))
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE kind = 'reply'"),
            [{"n": 0}])

    def test_a_request_outside_the_current_turn_is_refused(self):
        request = self.request()
        self.hold(request.id, turn=TURN)
        # A later turn replaces the marker; the old prompt is no longer held.
        registry.pending_spend_begin(
            self.db, "beta", 2, "consigne", "model-e2b", turn_id="turn-later")
        with self.assertRaises(ScopeRefused):
            public_mail.reply(self.db, "beta", request.id, Reply(body="late"),
                              lease=self.lease)

    def test_an_unreserved_request_is_refused(self):
        request = self.request()
        registry.pending_spend_begin(
            self.db, "beta", 0, "consigne", "model-e2b", turn_id=TURN)
        with self.assertRaises(ScopeRefused):
            public_mail.reply(self.db, "beta", request.id, Reply(body="early"),
                              lease=self.lease)

    def test_unknown_and_non_request_messages_are_refused(self):
        with self.assertRaises(ScopeRefused):
            public_mail.reply(self.db, "beta", 999999, Reply(body="ghost"),
                              lease=self.lease)
        notify = public_mail.submit(
            self.db, "alpha", Submission(recipient="beta", body="note",
                                         kind="notify"))
        token = mail.new_token()
        mail.reserve(self.db, "beta", OWNER, self.epoch, token,
                     ids=[notify.id], porteur="consigne", turn_id=TURN)
        registry.pending_spend_begin(
            self.db, "beta", 0, "consigne", "model-e2b", turn_id=TURN)
        with self.assertRaises(ScopeRefused):
            public_mail.reply(self.db, "beta", notify.id, Reply(body="nope"),
                              lease=self.lease)

    def test_reply_validates_its_inputs_before_any_write(self):
        request = self.request()
        self.hold(request.id)
        for bad in (None, "reply", Submission(recipient="alpha", body="x")):
            with self.subTest(reply=bad), self.assertRaises(InvalidSubmission):
                public_mail.reply(self.db, "beta", request.id, bad,
                                  lease=self.lease)
        for bad in (0, -1, True, "1", None):
            with self.subTest(request_id=bad), self.assertRaises(InvalidSubmission):
                public_mail.reply(self.db, "beta", bad, Reply(body="x"),
                                  lease=self.lease)
        for bad in (Reply(body=""), Reply(body="   "),
                    Reply(body='{"only": "json"}'),
                    Reply(body="x" * (public_mail.MAX_BODY_BYTES + 1)),
                    Reply(body="x", payload={"urgent": True}),
                    Reply(body="x", payload={"idempotency_key": "k"}),
                    Reply(body="x", payload={"n": float("nan")}),
                    Reply(body="x", thread="yes")):
            with self.subTest(reply=bad), self.assertRaises(InvalidSubmission):
                public_mail.reply(self.db, "beta", request.id, bad,
                                  lease=self.lease)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE kind = 'reply'"),
            [{"n": 0}])

    def test_reply_to_a_retired_sender_is_refused(self):
        request = self.request()
        self.hold(request.id)
        self.db.execute(
            "UPDATE agent_registry SET retired_at = clock_timestamp()"
            " WHERE name = 'alpha'")
        with self.assertRaises(RecipientRefused):
            public_mail.reply(self.db, "beta", request.id, Reply(body="lost"),
                              lease=self.lease)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE kind = 'reply'"),
            [{"n": 0}])

    def test_thread_false_keeps_the_reply_out_of_the_thread(self):
        request = self.request()
        self.hold(request.id)
        before = self.db.query("SELECT count(*) AS n FROM thread_index")[0]["n"]
        public_mail.reply(self.db, "beta", request.id,
                          Reply(body="private answer", thread=False),
                          lease=self.lease)
        after = self.db.query("SELECT count(*) AS n FROM thread_index")[0]["n"]
        self.assertEqual(after, before)
        self.reply_row(request.id)

    def test_history_exposes_the_correlation_on_both_sides(self):
        request = self.request(body="question")
        self.hold(request.id)
        reply = public_mail.reply(self.db, "beta", request.id,
                                  Reply(body="answer"), lease=self.lease)
        for actor in ("alpha", "beta"):
            [entry] = [row for row in public_mail.history(
                self.db, actor, peer="beta" if actor == "alpha" else "alpha",
                kind="reply").entries]
            self.assertEqual(entry.id, reply.id)
            self.assertEqual(entry.in_reply_to, request.id)
            self.assertEqual(entry.as_dict()["in_reply_to"], request.id)
        request_entry = [row for row in public_mail.history(
            self.db, "alpha", kind="request").entries][-1]
        self.assertIsNone(request_entry.in_reply_to)

    def test_an_old_writer_row_reads_as_uncorrelated(self):
        """N-1 : l'écrivain d'avant 0594 insère sans `in_reply_to` ; le
        nouveau lecteur tient sur sa ligne, et une réponse non corrélée ne
        répond pas à la demande."""
        request = self.request()
        self.hold(request.id)
        self.previous_insert(
            "agent_mailbox",
            {"sender": "beta", "recipient": "alpha", "body": "old reply",
             "kind": "reply", "work_item_id": str(LOT)},
            added=("in_reply_to",))
        [entry] = public_mail.history(
            self.db, "alpha", peer="beta", kind="reply").entries
        self.assertEqual(entry.body, "old reply")
        self.assertIsNone(entry.in_reply_to)
        self.assertEqual(
            [pending.id for pending in
             public_mail.pending(self.db, "beta", self.lease).entries],
            [request.id])


class ConcurrencyTest(PendingReplyBase):
    def _concurrent(self, bodies: tuple[str, str]):
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(body: str) -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=10)
                receipt = public_mail.reply(
                    db, "beta", self.request_id,
                    Reply(body=body, payload={"module": {"n": 1}}),
                    lease=self.lease)
            except BaseException as exc:  # noqa: BLE001 — the test inspects it
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    results.append({"id": receipt.id,
                                    "reused": receipt.reused,
                                    "body": body})
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(body,))
                   for body in bodies]
        for thread_ in threads:
            thread_.start()
        for thread_ in threads:
            thread_.join(timeout=30)
            self.assertFalse(thread_.is_alive(), "a concurrent worker is stuck")
        return results, errors

    def setUp(self) -> None:
        super().setUp()
        request = self.request()
        self.hold(request.id)
        self.request_id = request.id

    def test_reply_target_fences_the_marker_until_commit(self):
        """B #7672 : le marqueur lu pour autoriser la réponse est tenu
        `FOR SHARE` jusqu'au commit. Un effacement concurrent
        (`pending_spend_clear`, une autre connexion) attend et échoue sous
        `lock_timeout` ; après le commit de la réponse, le marqueur est intact
        et l'effacement passe."""
        holder, other = self.connect(), self.connect()
        self.addCleanup(holder.close)
        self.addCleanup(other.close)
        other.execute("SET lock_timeout = '200ms'")
        with holder.transaction() as tx:
            target = storage.of(tx).mailbox.reply_target(self.request_id)
            self.assertTrue(target["request"]["marker_present"])
            self.assertEqual(target["request"]["marker_turn_id"], TURN)
            with self.assertRaises(db_mod.DbError):
                other.execute("DELETE FROM spend_pending WHERE agent = 'beta'")
        self.assertEqual(
            registry.pending_spend_get(self.db, "beta")["turn_id"], TURN)
        self.assertTrue(registry.pending_spend_clear(self.db, "beta"))

    def test_concurrent_identical_replies_make_one_durable_row(self):
        results, errors = self._concurrent(("same", "same"))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(sorted(result["reused"] for result in results),
                         [False, True])
        self.assertEqual(len(self.rows()), 2)

    def test_concurrent_conflicting_replies_leave_one_row(self):
        results, errors = self._concurrent(("one", "two"))
        self.assertEqual(len(results) + len(errors), 2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReplyConflict)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.reply_row(self.request_id)["body"],
                         results[0]["body"])


class ContractSurfaceTest(unittest.TestCase):
    def test_version_and_exported_surface(self):
        self.assertEqual(public_mail.CONTRACT_VERSION, "1.3")
        self.assertEqual(public_mail.MAX_PENDING, 200)
        self.assertEqual(public_mail.DEFAULT_PENDING, 50)
        for name in ("Lease", "Reply", "PendingRequest", "PendingPage",
                     "pending", "reply", "ReplyConflict", "MAX_PENDING",
                     "DEFAULT_PENDING"):
            self.assertIn(name, public_mail.__all__)
            self.assertTrue(hasattr(public_mail, name))

    def test_values_are_json_ready(self):
        lease = Lease(OWNER, 3)
        self.assertEqual(json.loads(json.dumps(lease.as_dict())),
                         {"owner": OWNER, "epoch": 3})
        entry = PendingRequest(
            id=1, sender="a", recipient="b", body="q", payload={"m": 1},
            work_item_id="2", created_ts=1.5, delivered_ts=None, turn_id="t")
        page = PendingPage(entries=(entry,), has_more=False)
        self.assertEqual(json.loads(json.dumps(page.as_dict()))["entries"][0]["id"], 1)
        receipt = Receipt(id=9, actor="b", recipient="a", kind="reply",
                          created_ts=1.0, reused=True, in_reply_to=1)
        self.assertEqual(json.loads(json.dumps(receipt.as_dict())),
                         {"id": 9, "request_id": None, "in_reply_to": 1,
                          "actor": "b", "recipient": "a", "kind": "reply",
                          "created_ts": 1.0, "reused": True})
        self.assertEqual(ReplyConflict.code, "conflict")
        self.assertEqual(ScopeRefused.code, "refused")
        self.assertEqual(InvalidSubmission.code, "invalid")


if __name__ == "__main__":
    unittest.main()
