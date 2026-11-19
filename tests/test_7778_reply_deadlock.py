# SPDX-License-Identifier: AGPL-3.0-only
"""#7778 — panier de #7672 : réponses croisées et fil écrit après le commit.

1. Deux agents qui se répondent en même temps tenaient chacun leur ligne du
   registre (`fence`, FOR UPDATE) puis attendaient celle de l'autre
   (`_insert`, FOR SHARE) : le serveur annulait l'une des deux (40P01). Les
   deux lignes se prennent désormais dans l'ordre des noms : aucun
   interblocage, aucune reprise ; la reprise unique reste un filet.
2. Une réponse annulée après son dépôt (lecture de la ligne, commit) ne laisse
   aucune entrée dans le fil : l'écriture du fil attend le commit.

Postgres réel, une connexion par fil pour l'interblocage.
"""
from __future__ import annotations

import threading
from unittest import mock

from ameesh import fil, mail, registry
from ameesh.public import mail as public_mail
from ameesh.public.mail import Lease, PublicMailError, Reply, Submission

from .support import PgTestCase

OWNER = "runner-7778"
TURN = "turn-7778"


class CrossedReplyTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM spend_pending")
        self.leases: dict[str, Lease] = {}
        for name in ("alpha", "beta"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host)
            lease = registry.claim(self.db, name, OWNER, 600)
            self.assertIsNotNone(lease)
            self.leases[name] = Lease(OWNER, int(lease["lease_epoch"]))

    def held_request(self, sender: str, recipient: str) -> int:
        """A request from ``sender``, held as ``recipient``'s current prompt."""
        receipt = public_mail.submit(self.db, sender, Submission(
            recipient=recipient, body="question from %s" % sender,
            kind="request"))
        epoch = self.leases[recipient].epoch
        token = mail.new_token()
        mail.reserve(self.db, recipient, OWNER, epoch, token,
                     ids=[receipt.id], porteur="consigne", turn_id=TURN)
        self.assertIsNotNone(registry.pending_spend_begin(
            self.db, recipient, 0, "consigne", "model-7778", turn_id=TURN))
        self.assertEqual(
            mail.deliver(self.db, recipient, OWNER, epoch, token, [receipt.id]),
            [receipt.id])
        return receipt.id

    def test_crossed_replies_both_land_without_a_raw_deadlock(self):
        requests = {"beta": self.held_request("alpha", "beta"),
                    "alpha": self.held_request("beta", "alpha")}
        # Before #7778 each worker held its own registry row here, then the
        # inserts crossed.  In name order the second worker waits inside its
        # fence instead: the barrier breaks after a short wait, nothing more.
        barrier = threading.Barrier(2)
        fenced = registry.lease_agent_locked
        first = threading.local()

        def fence_then_wait(tx, name, owner, epoch, **kwargs):
            result = fenced(tx, name, owner, epoch, **kwargs)
            if not getattr(first, "done", False):
                first.done = True
                try:
                    barrier.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            return result

        results: dict[str, int] = {}
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(actor: str) -> None:
            db = self.connect()
            try:
                receipt = public_mail.reply(
                    db, actor, requests[actor], Reply(body="answer of %s" % actor),
                    lease=self.leases[actor])
            except BaseException as exc:  # noqa: BLE001 — the test inspects it
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    results[actor] = receipt.in_reply_to
            finally:
                db.close()

        once = mock.Mock(wraps=public_mail._reply_once)
        with mock.patch.object(registry, "lease_agent_locked", fence_then_wait), \
                mock.patch.object(public_mail, "_reply_once", once):
            threads = [threading.Thread(target=worker, args=(actor,))
                       for actor in ("alpha", "beta")]
            for thread_ in threads:
                thread_.start()
            for thread_ in threads:
                thread_.join(timeout=30)
                self.assertFalse(thread_.is_alive(), "a crossed reply is stuck")
        self.assertEqual(errors, [])
        self.assertEqual(results, requests)
        # No deadlock victim: each reply committed on its first transaction.
        self.assertEqual(once.call_count, 2)
        replies = self.db.query(
            "SELECT in_reply_to FROM agent_mailbox WHERE kind = 'reply'"
            " ORDER BY in_reply_to")
        self.assertEqual([row["in_reply_to"] for row in replies],
                         sorted(requests.values()))

    def test_a_second_deadlock_is_not_hidden(self):
        request_id = self.held_request("alpha", "beta")
        deadlock = public_mail.DbError("deadlock detected", sqlstate="40P01")
        with mock.patch.object(public_mail, "_reply_once",
                               side_effect=deadlock) as once:
            with self.assertRaises(public_mail.DbError):
                public_mail.reply(self.db, "beta", request_id,
                                  Reply(body="answer"),
                                  lease=self.leases["beta"])
        self.assertEqual(once.call_count, public_mail._REPLY_ATTEMPTS)

    def test_a_rolled_back_reply_leaves_no_thread_entry(self):
        request_id = self.held_request("alpha", "beta")
        written = []
        record = fil.record

        def spy(cfg, db, **kwargs):
            written.append(list(kwargs.get("mailbox_ids") or ()))
            return record(cfg, db, **kwargs)

        with mock.patch.object(fil, "record", spy):
            with mock.patch.object(public_mail, "_row_of",
                                   side_effect=PublicMailError("read failed")):
                with self.assertRaises(PublicMailError):
                    public_mail.reply(self.db, "beta", request_id,
                                      Reply(body="answer"),
                                      lease=self.leases["beta"])
            self.assertEqual(written, [])
            self.assertEqual(self.db.query(
                "SELECT count(*) AS n FROM agent_mailbox WHERE kind = 'reply'"
            )[0]["n"], 0)
            # The committed retry reaches the thread once, after its commit.
            receipt = public_mail.reply(
                self.db, "beta", request_id, Reply(body="answer"),
                lease=self.leases["beta"])
        self.assertEqual(written, [[receipt.id]])
