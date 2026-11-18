# SPDX-License-Identifier: AGPL-3.0-only
"""#7671 — contrat public 1.2, E2a : courrier typé, idempotent et borné.

Le banc vérifie la surface ``ameesh.public.mail`` telle que le contrat écrit
la décrit (`docs/api/module-contract-v1.md`) : soumission typée, clé
d'idempotence portée par ``(acteur, destinataire)``, identifiant durable,
historique à curseur borné au périmètre de l'appelant, ``submit_many`` borné,
et refus déterministes. Postgres réel.

Les cas de concurrence ouvrent une connexion par fil : le même processus ne
prouverait rien, le verrou client sérialiserait les deux transactions.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from unittest import mock

from ameesh import mail, registry
from ameesh.public import mail as public_mail
from ameesh.public.mail import (
    BatchResult, HistoryEntry, HistoryPage, IdempotencyConflict,
    InvalidSubmission, Receipt, RecipientRefused, ScopeRefused, Submission,
    SubmissionResult, UnsupportedStorage,
)

from .support import PgTestCase


class PublicMailBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("alpha", "beta", "gamma"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host)

    def submission(self, recipient: str = "beta", body: str = "hello",
                   **overrides) -> Submission:
        data = {"recipient": recipient, "body": body}
        data.update(overrides)
        return Submission(**data)

    def rows(self, recipient: str | None = None) -> list[dict]:
        if recipient is None:
            return self.db.query(
                "SELECT id, sender, recipient, body, kind, payload, work_item_id,"
                " delivered_at FROM agent_mailbox ORDER BY id")
        return self.db.query(
            "SELECT id, sender, recipient, body, kind, payload, work_item_id,"
            " delivered_at FROM agent_mailbox WHERE recipient = %s ORDER BY id",
            (recipient,))


class SubmitTest(PublicMailBase):
    def test_submit_returns_a_durable_typed_receipt(self):
        receipt = public_mail.submit(
            self.db, "alpha",
            self.submission(kind="request", payload={"module": {"doc": 42}},
                            work_item_id=7671,
                            idempotency_key="alpha:beta:1"))
        self.assertIsInstance(receipt, Receipt)
        self.assertGreater(receipt.id, 0)
        self.assertEqual(receipt.request_id, receipt.id)
        self.assertEqual((receipt.actor, receipt.recipient, receipt.kind),
                         ("alpha", "beta", "request"))
        self.assertFalse(receipt.reused)
        self.assertGreater(receipt.created_ts, 0)
        [row] = self.rows("beta")
        self.assertEqual(row["sender"], "alpha")
        self.assertEqual(row["body"], "hello")
        self.assertEqual(row["kind"], "request")
        self.assertEqual(row["payload"]["module"], {"doc": 42})
        self.assertEqual(row["payload"]["idempotency_key"], "alpha:beta:1")
        self.assertEqual(row["work_item_id"], "7671")
        self.assertEqual(receipt.as_dict()["request_id"], row["id"])

    def test_non_request_receipt_has_no_request_id(self):
        receipt = public_mail.submit(self.db, "alpha", self.submission())
        self.assertIsNone(receipt.request_id)
        self.assertEqual(receipt.kind, "notify")

    def test_body_and_payload_are_trimmed_and_round_trip(self):
        receipt = public_mail.submit(
            self.db, "alpha",
            self.submission(body="  ligne un\nligne deux  ",
                            payload={"module": {"n": 1, "t": [True, None, "é"]}}))
        [row] = self.rows("beta")
        self.assertEqual(row["body"], "ligne un\nligne deux")
        self.assertEqual(row["payload"]["module"],
                         {"n": 1, "t": [True, None, "é"]})
        self.assertEqual(public_mail.find(
            self.db, "alpha", "beta", "absent"), None)

    def test_thread_false_keeps_private_mail_out_of_the_thread(self):
        public_mail.submit(self.db, "alpha", self.submission(
            body="private document context", thread=False))
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM thread_index")[0]["n"], 0)
        self.assertEqual(len(self.rows("beta")), 1)
        public_mail.submit(self.db, "alpha", self.submission(
            body="readable announcement", thread=True))
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM thread_index")[0]["n"], 1)

    def test_stopped_sender_is_refused(self):
        self.db.execute(
            "UPDATE agent_registry SET status = 'stopped' WHERE name = 'alpha'")
        with self.assertRaises(ScopeRefused):
            public_mail.submit(self.db, "alpha", self.submission())
        self.assertEqual(self.rows(), [])

    def test_unknown_and_retired_recipient_are_refused(self):
        with self.assertRaises(RecipientRefused):
            public_mail.submit(
                self.db, "alpha", self.submission(recipient="nobody"))
        self.db.execute(
            "UPDATE agent_registry SET retired_at = clock_timestamp()"
            " WHERE name = 'beta'")
        with self.assertRaises(RecipientRefused):
            public_mail.submit(self.db, "alpha", self.submission())
        self.assertEqual(self.rows(), [])

    def test_invalid_actor_and_submission_are_refused_before_deposit(self):
        for actor in ("", "all", "human:smichea", "bad name", "x" * 65):
            with self.subTest(actor=actor):
                with self.assertRaises(InvalidSubmission):
                    public_mail.submit(self.db, actor, self.submission())
        for candidate in (
                self.submission(body=""),
                self.submission(body="   "),
                self.submission(body='{"only": "json"}'),
                self.submission(kind="shout"),
                self.submission(work_item_id="lot-42"),
                self.submission(work_item_id=0),
                self.submission(thread="yes"),
                self.submission(idempotency_key="bad key"),
                self.submission(payload={"urgent": True}),
                self.submission(payload={"report": "state"}),
                self.submission(payload={"n": float("nan")}),
                self.submission(payload={"n": object()}),
                self.submission(payload={"x": "a" * public_mail.MAX_PAYLOAD_BYTES}),
                self.submission(body="a" * (public_mail.MAX_BODY_BYTES + 1)),
                "not-a-submission",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(InvalidSubmission):
                    public_mail.submit(self.db, "alpha", candidate)
        self.assertEqual(self.rows(), [])

    def test_attribution_is_validated_and_normalized(self):
        receipt = public_mail.submit(self.db, "alpha", self.submission(
            payload={"attribution": {"source": " ima ", "run_id": "r-1"}}))
        [row] = self.rows("beta")
        self.assertEqual(row["payload"]["attribution"],
                         {"source": "ima", "run_id": "r-1"})
        with self.assertRaises(InvalidSubmission):
            public_mail.submit(self.db, "alpha", self.submission(
                idempotency_key="attr-bad",
                payload={"attribution": {"unknown": "x"}}))
        self.assertEqual(len(self.rows()), 1)

    def test_declared_process_identity_must_match_the_actor(self):
        from ameesh import identity
        binding = identity.Binding(name="alpha", source="runner", ok=True)
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "alpha"}), \
                mock.patch("ameesh.identity.resolve_binding",
                           return_value=binding):
            receipt = public_mail.submit(self.db, "alpha", self.submission())
            self.assertGreater(receipt.id, 0)
            with self.assertRaises(ScopeRefused):
                public_mail.submit(self.db, "beta", self.submission())
        self.assertEqual(len(self.rows("beta")), 1)

    def test_a_declared_but_unverified_identity_is_refused(self):
        from ameesh import identity
        binding = identity.Binding(
            name="alpha", source="runner", ok=False, reason="lease lost")
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "alpha"}), \
                mock.patch("ameesh.identity.resolve_binding",
                           return_value=binding):
            with self.assertRaises(ScopeRefused):
                public_mail.submit(self.db, "alpha", self.submission())
        self.assertEqual(self.rows(), [])

    def test_mediated_driver_is_not_served(self):
        with mock.patch.object(public_mail.storage, "is_remote",
                               return_value=True):
            for call in (
                    lambda: public_mail.submit(self.db, "alpha", self.submission()),
                    lambda: public_mail.find(self.db, "alpha", "beta", "k"),
                    lambda: public_mail.history(self.db, "alpha"),
                    lambda: public_mail.submit_many(self.db, "alpha", []),
            ):
                with self.assertRaises(UnsupportedStorage):
                    call()
        self.assertEqual(self.rows(), [])


class IdempotencyTest(PublicMailBase):
    def test_same_key_same_content_reuses_the_id(self):
        first = public_mail.submit(
            self.db, "alpha", self.submission(
                kind="request", payload={"module": {"doc": 1}},
                idempotency_key="same-key"))
        second = public_mail.submit(
            self.db, "alpha", self.submission(
                kind="request", payload={"module": {"doc": 1}},
                idempotency_key="same-key"))
        self.assertEqual(second.id, first.id)
        self.assertTrue(second.reused)
        self.assertEqual(len(self.rows("beta")), 1)
        self.assertEqual(len(mail.unread(self.db, "beta")), 1)

    def test_key_is_scoped_by_recipient(self):
        first = public_mail.submit(
            self.db, "alpha", self.submission(recipient="beta",
                                              idempotency_key="shared"))
        second = public_mail.submit(
            self.db, "alpha", self.submission(recipient="gamma",
                                              idempotency_key="shared"))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(self.rows()), 2)

    def test_same_key_with_another_content_refuses(self):
        first = public_mail.submit(
            self.db, "alpha", self.submission(
                kind="request", payload={"module": {"doc": 1}},
                idempotency_key="conflict"))
        for changed in (
                self.submission(body="another text",
                                idempotency_key="conflict"),
                self.submission(kind="notify",
                                payload={"module": {"doc": 1}},
                                idempotency_key="conflict"),
                self.submission(payload={"module": {"doc": 2}},
                                idempotency_key="conflict"),
                self.submission(work_item_id=12,
                                payload={"module": {"doc": 1}},
                                idempotency_key="conflict"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(IdempotencyConflict):
                    public_mail.submit(self.db, "alpha", changed)
        [row] = self.rows("beta")
        self.assertEqual(row["id"], first.id)
        self.assertEqual(row["body"], "hello")

    def test_find_returns_the_durable_receipt_and_stays_in_scope(self):
        receipt = public_mail.submit(
            self.db, "alpha", self.submission(
                kind="request", idempotency_key="lookup-me"))
        found = public_mail.find(self.db, "alpha", "beta", "lookup-me")
        self.assertEqual(found.id, receipt.id)
        self.assertTrue(found.reused)
        self.assertIsNone(public_mail.find(
            self.db, "gamma", "beta", "lookup-me"))
        self.assertIsNone(public_mail.find(
            self.db, "alpha", "gamma", "lookup-me"))
        with self.assertRaises(InvalidSubmission):
            public_mail.find(self.db, "alpha", "beta", "")

    def test_concurrent_identical_retries_make_one_durable_row(self):
        results, errors = self._concurrent(
            same=True, thread=False)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(sorted(result["reused"] for result in results),
                         [False, True])
        self.assertEqual(len(self.rows("beta")), 1)

    def test_concurrent_conflicting_retries_leave_one_row(self):
        results, errors = self._concurrent(same=False, thread=False)
        self.assertEqual(len(results) + len(errors), 2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], IdempotencyConflict)
        self.assertEqual(len(results), 1)
        [row] = self.rows("beta")
        self.assertEqual(row["body"], results[0]["body"])

    def test_unique_violation_fallback_reuses_a_late_row(self):
        """The 0497 conflict path re-reads and reuses, without the pre-check."""
        first = public_mail.submit(
            self.db, "alpha", self.submission(
                kind="request", payload={"module": {"doc": 1}},
                idempotency_key="late-identical"))
        miss, calls = self._miss_first_precheck()
        with mock.patch.object(public_mail, "_existing", side_effect=miss):
            second = public_mail.submit(
                self.db, "alpha", self.submission(
                    kind="request", payload={"module": {"doc": 1}},
                    idempotency_key="late-identical"))
        self.assertEqual(calls, ["late-identical", "late-identical"])
        self.assertEqual(second.id, first.id)
        self.assertTrue(second.reused)
        self.assertEqual(len(self.rows("beta")), 1)

    def test_unique_violation_fallback_refuses_another_content(self):
        """A late conflicting row is a deterministic refusal, never a reuse."""
        first = public_mail.submit(
            self.db, "alpha", self.submission(
                kind="request", idempotency_key="late-conflict"))
        miss, calls = self._miss_first_precheck()
        with mock.patch.object(public_mail, "_existing", side_effect=miss):
            with self.assertRaises(IdempotencyConflict):
                public_mail.submit(
                    self.db, "alpha", self.submission(
                        body="another text", kind="request",
                        idempotency_key="late-conflict"))
        self.assertEqual(calls, ["late-conflict", "late-conflict"])
        [row] = self.rows("beta")
        self.assertEqual(row["id"], first.id)
        self.assertEqual(row["body"], "hello")

    def _miss_first_precheck(self):
        """Return an ``_existing`` that misses once, then behaves normally.

        This forces the insert to meet the unique index of migration 0497, so
        the fallback is exercised deterministically instead of depending on
        thread interleaving.
        """
        real = public_mail._existing
        calls: list[str | None] = []

        def miss_the_precheck(db, prepared):
            calls.append(prepared.idempotency_key)
            if len(calls) == 1:
                return None
            return real(db, prepared)

        return miss_the_precheck, calls

    def _concurrent(self, *, same: bool, thread: bool):
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(body: str) -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=10)
                receipt = public_mail.submit(
                    db, "alpha",
                    self.submission(body=body, thread=thread,
                                    idempotency_key="concurrent"))
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
                   for body in (("same" if same else "conflict one",
                                 "same" if same else "conflict two"))]
        for thread_ in threads:
            thread_.start()
        for thread_ in threads:
            thread_.join(timeout=30)
            self.assertFalse(thread_.is_alive(), "a concurrent worker is stuck")
        return results, errors


class BatchTest(PublicMailBase):
    def test_submit_many_keeps_order_and_isolates_refusals(self):
        batch = public_mail.submit_many(self.db, "alpha", (
            self.submission(recipient="beta", body="one",
                            idempotency_key="batch-1"),
            self.submission(recipient="nobody", body="two",
                            idempotency_key="batch-2"),
            self.submission(recipient="gamma", body="three",
                            idempotency_key="batch-3"),
        ))
        self.assertIsInstance(batch, BatchResult)
        self.assertFalse(batch.ok)
        self.assertEqual([result.index for result in batch.results], [0, 1, 2])
        self.assertTrue(batch.results[0].ok)
        self.assertEqual(batch.results[1].error, "refused")
        self.assertTrue(batch.results[2].ok)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(batch.as_dict()["results"][1]["error"], "refused")

    def test_a_batch_is_bounded_before_any_deposit(self):
        items = [self.submission(body="body %d" % index,
                                 idempotency_key="bound-%d" % index)
                 for index in range(public_mail.MAX_BATCH + 1)]
        with self.assertRaises(InvalidSubmission):
            public_mail.submit_many(self.db, "alpha", items)
        self.assertEqual(self.rows(), [])

    def test_duplicate_inside_one_batch_reuses_the_first_id(self):
        batch = public_mail.submit_many(self.db, "alpha", (
            self.submission(idempotency_key="inside"),
            self.submission(idempotency_key="inside"),
        ))
        first, second = batch.results
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(first.receipt.id, second.receipt.id)
        self.assertTrue(second.receipt.reused)
        self.assertEqual(len(self.rows("beta")), 1)

    def test_empty_batch_is_an_empty_result(self):
        batch = public_mail.submit_many(self.db, "alpha", [])
        self.assertTrue(batch.ok)
        self.assertEqual(batch.results, ())


class ControlPayloadTest(PublicMailBase):
    """Review B/P1 (#7671): a module payload never selects a core control path.

    The mailbox rewrites a pending review request when it sees
    ``payload.review_designation`` on a request linked to a lot
    (``Mailbox.send`` → ``_send_review(automatic=True)``).  A module payload is
    data, so the public surface must refuse that key before any deposit and
    leave the core request intact.
    """

    LOT = 7671

    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state)"
            " VALUES (%s, 'evolution', 'mesh-design', 'lot de relecture',"
            " 'ameesh', 'qa')", (self.LOT,))
        self.pending = mail.send(
            self.db, "beta", "alpha", "relecture du lot",
            kind="request", work_item_id=str(self.LOT),
            payload={"review_request": {"freeze": {"sha": "a" * 40}}})

    def mailbox_rows(self) -> list[dict]:
        return self.db.query(
            "SELECT id, sender, recipient, body, payload, work_item_id,"
            " delivered_at FROM agent_mailbox ORDER BY id")

    def replacement_notes(self) -> list[dict]:
        return self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s", (self.LOT, "review-designation-replaced%"))

    def test_every_reserved_key_is_refused_before_deposit(self):
        before = self.mailbox_rows()
        for key in sorted(public_mail.RESERVED_PAYLOAD_KEYS):
            with self.subTest(key=key):
                with self.assertRaises(InvalidSubmission):
                    public_mail.submit(
                        self.db, "gamma", self.submission(
                            recipient="alpha", body="marker " + key,
                            payload={key: {"sha": "b" * 40}}))
        self.assertEqual(self.mailbox_rows(), before)

    def test_a_mapping_cannot_hide_a_reserved_key(self):
        class Hidden(dict):
            def __iter__(self):
                return (key for key in super().__iter__()
                        if key not in ("review_designation", "review_request"))

        before = self.mailbox_rows()
        for key in ("review_designation", "review_request"):
            with self.subTest(key=key):
                with self.assertRaises(InvalidSubmission):
                    public_mail.submit(
                        self.db, "gamma", self.submission(
                            recipient="alpha", body="hidden " + key,
                            kind="request", work_item_id=self.LOT,
                            payload=Hidden({key: {"sha": "b" * 40}})))
        self.assertEqual(self.mailbox_rows(), before)

    def test_a_module_cannot_replace_a_pending_review_request(self):
        before = self.mailbox_rows()
        with self.assertRaises(InvalidSubmission):
            public_mail.submit(
                self.db, "gamma", self.submission(
                    recipient="alpha", body="take over", kind="request",
                    work_item_id=self.LOT,
                    payload={"review_designation": {"sha": "b" * 40}}))
        self.assertEqual(self.mailbox_rows(), before)
        self.assertEqual(self.replacement_notes(), [])

    def test_a_batch_reports_a_reserved_key_as_invalid(self):
        before = self.mailbox_rows()
        batch = public_mail.submit_many(self.db, "gamma", (
            self.submission(recipient="alpha", body="fine"),
            self.submission(
                recipient="alpha", body="take over", kind="request",
                work_item_id=self.LOT,
                payload={"review_request": {"freeze": {"sha": "c" * 40}}}),
        ))
        self.assertEqual([result.ok for result in batch.results], [True, False])
        self.assertEqual(batch.results[1].error, "invalid")
        rows = self.mailbox_rows()
        self.assertEqual(len(rows), len(before) + 1)
        self.assertEqual([row for row in rows if row["id"] == self.pending],
                         [row for row in before if row["id"] == self.pending])


class HistoryTest(PublicMailBase):
    def setUp(self) -> None:
        super().setUp()
        self.welcome = public_mail.submit(
            self.db, "alpha", self.submission(
                recipient="beta", body="alpha to beta", kind="request",
                payload={"module": {"n": 1}}, idempotency_key="h1"))
        self.reply = public_mail.submit(
            self.db, "beta", self.submission(
                recipient="alpha", body="beta replies", kind="reply",
                payload={"module": {"n": 2}}, idempotency_key="h2"))
        self.other = public_mail.submit(
            self.db, "alpha", self.submission(
                recipient="gamma", body="alpha to gamma", kind="event",
                idempotency_key="h3"))
        public_mail.submit(
            self.db, "gamma", self.submission(
                recipient="beta", body="not alpha's business"))

    def test_history_is_limited_to_the_actor_scope(self):
        page = public_mail.history(self.db, "alpha")
        self.assertIsInstance(page, HistoryPage)
        self.assertEqual([entry.body for entry in page.entries],
                         ["alpha to beta", "beta replies", "alpha to gamma"])
        self.assertEqual([entry.id for entry in page.entries],
                         [self.welcome.id, self.reply.id, self.other.id])
        self.assertFalse(page.has_more)
        self.assertEqual(page.next_cursor, self.other.id)

    def test_peer_restricts_to_the_two_party_conversation(self):
        page = public_mail.history(self.db, "alpha", peer="beta")
        self.assertEqual([entry.body for entry in page.entries],
                         ["alpha to beta", "beta replies"])
        page = public_mail.history(self.db, "alpha", peer="gamma")
        self.assertEqual([entry.body for entry in page.entries],
                         ["alpha to gamma"])

    def test_kind_filter_and_payload_round_trip(self):
        page = public_mail.history(self.db, "alpha", kind="reply")
        self.assertEqual([entry.body for entry in page.entries],
                         ["beta replies"])
        self.assertEqual(page.entries[0].payload["module"], {"n": 2})
        self.assertIsInstance(page.entries[0], HistoryEntry)

    def test_cursor_is_bounded_and_stable_under_new_mail(self):
        first = public_mail.history(self.db, "alpha", limit=2)
        self.assertEqual(len(first.entries), 2)
        self.assertTrue(first.has_more)
        self.assertEqual(first.next_cursor, first.entries[-1].id)
        # New mail after the first page must not shift the cursor.
        public_mail.submit(
            self.db, "alpha", self.submission(
                recipient="beta", body="later mail",
                idempotency_key="h-later"))
        second = public_mail.history(
            self.db, "alpha", after_id=first.next_cursor, limit=2)
        self.assertEqual([entry.body for entry in second.entries],
                         ["alpha to gamma", "later mail"])
        self.assertFalse(second.has_more)
        self.assertEqual(second.next_cursor, second.entries[-1].id)
        empty = public_mail.history(
            self.db, "alpha", after_id=second.next_cursor)
        self.assertEqual(empty.entries, ())
        self.assertEqual(empty.next_cursor, second.next_cursor)

    def test_time_bounds_and_delivery_observation(self):
        all_entries = public_mail.history(self.db, "alpha").entries
        stamps = [entry.created_ts for entry in all_entries]
        floor, ceiling = min(stamps), max(stamps)
        self.assertEqual(
            len(public_mail.history(
                self.db, "alpha", since_ts=floor - 1,
                until_ts=ceiling + 1).entries), 3)
        self.assertEqual(
            public_mail.history(
                self.db, "alpha", until_ts=floor - 1).entries, ())
        self.assertEqual(
            public_mail.history(
                self.db, "alpha", since_ts=ceiling + 1).entries, ())
        [entry] = public_mail.history(self.db, "beta", peer="alpha",
                                      kind="request").entries
        self.assertIsNone(entry.delivered_ts)
        mail.mark_delivered(self.db, [entry.id])
        [entry] = public_mail.history(self.db, "beta", peer="alpha",
                                      kind="request").entries
        self.assertIsNotNone(entry.delivered_ts)

    def test_history_refuses_invalid_bounds(self):
        for kwargs in ({"limit": 0}, {"limit": public_mail.MAX_HISTORY + 1},
                       {"after_id": -1}, {"since_ts": "now"},
                       {"until_ts": -2}, {"since_ts": 10, "until_ts": 5},
                       {"kind": "shout"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidSubmission):
                    public_mail.history(self.db, "alpha", **kwargs)


class ContractSurfaceTest(unittest.TestCase):
    def test_version_and_exported_surface(self):
        # Contract 1.3 (E2b, #7672) is additive over this E2a surface: the
        # same elements stay exported, the revision only moves forward.
        self.assertEqual(public_mail.CONTRACT_VERSION, "1.3")
        self.assertEqual(public_mail.MAX_BATCH, 50)
        self.assertEqual(public_mail.MAX_HISTORY, 200)
        for name in ("Submission", "Receipt", "HistoryEntry", "HistoryPage",
                     "SubmissionResult", "BatchResult", "submit", "submit_many",
                     "find", "history", "PublicMailError", "InvalidSubmission",
                     "IdempotencyConflict", "ScopeRefused", "RecipientRefused",
                     "UnsupportedStorage"):
            self.assertIn(name, public_mail.__all__)
            self.assertTrue(hasattr(public_mail, name))

    def test_receipt_and_page_are_json_ready(self):
        receipt = Receipt(id=7, actor="a", recipient="b", kind="request",
                          created_ts=1.5, reused=True)
        self.assertEqual(json.loads(json.dumps(receipt.as_dict()))["request_id"], 7)
        page = HistoryPage(entries=(), next_cursor=None, has_more=False)
        self.assertEqual(json.loads(json.dumps(page.as_dict())),
                         {"entries": [], "next_cursor": None, "has_more": False})
        result = SubmissionResult(index=0, error="invalid", detail="nope")
        self.assertEqual(result.as_dict()["ok"], False)
        self.assertEqual(BatchResult((result,)).as_dict()["results"][0]["error"],
                         "invalid")
