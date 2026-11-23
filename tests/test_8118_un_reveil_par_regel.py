# SPDX-License-Identifier: AGPL-3.0-only
"""#8118 — un seul réveil par relecteur et par nouvelle tête.

Témoin du lot : trois regels successifs avec les mêmes relecteurs donnent
trois réveils par relecteur (un par tête gelée), pas six à neuf. La demande de
désignation du siège est le réveil ; l'annonce de gel que l'auteur envoie
devant elle devient passive (lue au tour suivant) et ne double plus ce réveil.
Une demande déjà livrée n'est plus un réveil en attente (B #231383) : la
réponse de l'auteur réveille, et une question qui n'annonce pas le gel aussi.
La livraison ne peut pas se glisser entre la lecture et le dépôt (B #231419) :
le dépôt relit sous le verrou du destinataire.

La porte ne change pas (#2442) : la demande exacte reste déposée sur la tête
gelée et le verdict reste dû.
"""
from __future__ import annotations

import json
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import mail, merge_queue, storage, work
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg
from ameesh.storage.postgres import mailbox as _pg
from ameesh.work_reviewers import designated

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture


class OneWakePerRegelTest(_ActivationFixture, PgTestCase):
    # -- helpers ----------------------------------------------------------
    def _verdict(self, actor, harness, sha, verdict="ok"):
        mark = work.milestone(self.db, self.lot, "verdict", sha=sha,
                              actor=actor, verdict=verdict, repo=self.repo.root,
                              after_commit=[])
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)",
            ("milestone:" + str(int(mark["id"])), sha, actor,
             "s-" + harness, harness, self.lot))

    def _advance(self, text):
        self.repo.write("src/ameesh/mail.py", text + "\n")
        self.frozen = self.repo.commit(text)
        self.receipts(commits=[self.frozen])

    def _requests(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and m["kind"] == "request"]

    def _notifies(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and m["kind"] == "notify"]

    def _wakes(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and not mail.passive_reason(m)]

    def _author_note(self, name, **kwargs):
        return mail.send(self.db, "author", name,
                         "#%d : nouveau gel %s" % (self.lot, self.frozen),
                         kind="notify", work_item_id=str(self.lot), **kwargs)

    # -- witnesses --------------------------------------------------------
    def test_three_regels_wake_each_reviewer_once_per_head(self):
        for index in range(3):
            previous = self.frozen
            if index:
                self._advance("head%d" % (index + 1))
            _mark, out = self.freeze()
            self.assertEqual(out["state"], "designated", out)
            self.assertEqual(set(designated(work.get(self.db, self.lot))),
                             {"r1", "r2"})
            # B2 #45616 (ameesh-cx2) : un regel à relecteurs inchangés n'écrit
            # pas de nouvelle désignation ; il garde les sièges et ne dépose
            # qu'une demande de delta par relecteur (marque de continuation).
            self.assertEqual(len(self.marks()), 1,
                             [note[:80] for note in self.marks()])
            continuations = [note for note in self.notes()
                             if note.startswith("review-designation-continued:")]
            self.assertEqual(len(continuations), index)
            if index:
                payload = json.loads(continuations[-1].split(": ", 1)[1])
                self.assertEqual(payload["freeze"]["sha"], self.frozen)
                self.assertEqual(payload["previous"]["sha"], previous)
                self.assertEqual({row["name"] for row in payload["reviewers"]},
                                 {"r1", "r2"})
                self.assertEqual(len(payload["sent"]), 2, payload)
            # L'auteur prévient les relecteurs après le gel, comme dans
            # l'incident (#8087, #8096) : l'avis ne doit pas rouvrir un tour.
            for name in ("r1", "r2"):
                self._author_note(name)
            for name in ("r1", "r2"):
                notes = self._notifies(name)
                self.assertEqual(len(notes), index + 1, notes)
                self.assertTrue(all(mail.passive_reason(m) for m in notes),
                                [(m["kind"], mail.passive_reason(m)) for m in notes])
                requests = self._requests(name)
                self.assertEqual(len(requests), index + 1)
                # Le courrier du regel donne bien la nouvelle tête et, dès le
                # deuxième regel, le delta depuis la tête du verdict précédent.
                newest = requests[-1]
                self.assertIn("--sha " + self.frozen, newest["body"])
                if index:
                    self.assertIn("git diff %s..%s"
                                  % (previous[:12], self.frozen[:12]),
                                  newest["body"])
                    self.assertIn("Review continued at re-freeze",
                                  newest["body"])
            # Le siège est réveillé (demande lue) puis rend son verdict : la
            # tête suivante aura sa propre demande.
            self.db.execute(
                "UPDATE agent_mailbox SET delivered_at = now()"
                " WHERE work_item_id = %s AND kind = 'request'",
                (str(self.lot),))
            for name, harness in (("r1", "claude"), ("r2", "codex")):
                self._verdict(name, harness, self.frozen)
        for name in ("r1", "r2"):
            wakes = self._wakes(name)
            self.assertEqual(len(wakes), 3,
                             [(m["kind"], m["body"][:50]) for m in wakes])
            self.assertEqual([m["kind"] for m in wakes], ["request"] * 3)

    def test_a_continued_regel_keeps_the_proven_base_and_the_due_review(self):
        # B/P2 #231515 (auditeur) : un regel à relecteurs inchangés n'écrit
        # pas de désignation ; sa marque de continuation doit porter la même
        # preuve de base que le port (#6365), sinon la porte retombe sur un
        # origin/<cible> en retard. Les sièges gardés restent aussi des avis
        # dus sur la nouvelle tête (file de l'agent et charge de la sélection).
        self.freeze()
        self._advance("head2")
        _mark, out = self.freeze()
        self.assertTrue(out.get("continued"), out)
        rows = self.db.query(
            "SELECT note, evidence FROM work_item_events"
            " WHERE work_item_id = %s AND note LIKE %s ORDER BY id",
            (self.lot, pg.continued_mark_prefix(self.lot) + "%"))
        self.assertEqual(len(rows), 1, rows)
        payload = pg.continued_mark_of(rows[0]["note"])
        self.assertEqual(payload["base"], self.base)
        self.assertEqual(rows[0]["evidence"],
                         pg.base_evidence(self.lot,
                                          int(payload["freeze"]["id"]),
                                          self.frozen, self.base))
        self.assertEqual(merge_queue._designated_base(
            self.db, work.get(self.db, self.lot),
            {"id": int(payload["freeze"]["id"])}, self.repo.root, self.frozen),
            self.base)
        for name in ("r1", "r2"):
            due = act.pending_reviews(self.db, name)
            self.assertIn((self.lot, self.frozen),
                          [(row["lot"], row["sha"]) for row in due], due)

    def test_a_human_or_system_notice_always_wakes(self):
        self.freeze()
        before = len(self._wakes("r1"))
        mail.send(self.db, "human:alice", "r1", "#%d : question" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        mail.send(self.db, "ameesh", "r1", "#%d : rappel système" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        self.assertEqual(len(self._wakes("r1")), before + 2)

    def test_an_urgent_notice_is_never_passive(self):
        self.freeze()
        before = len(self._wakes("r1"))
        self._author_note("r1", payload={"urgent": True})
        self.assertEqual(len(self._wakes("r1")), before + 1)

    def test_a_notice_outside_the_review_lot_or_state_wakes(self):
        self.freeze()
        reviews_before = len(self._wakes("r1"))
        pilot_before = len(self._wakes("pilot"))
        # Destinataire hors siège : le pilote n'est pas relecteur désigné.
        mail.send(self.db, "author", "pilot", "#%d : pour information" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        # Lot remis en construction : plus de verdict dû sur une tête gelée.
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (self.lot,))
        self._author_note("r1")
        self.assertEqual(len(self._wakes("pilot")), pilot_before + 1)
        self.assertEqual(len(self._wakes("r1")), reviews_before + 1)

    def test_after_an_ok_verdict_the_authors_notice_wakes_again(self):
        # Frontière de la règle : le réveil unique couvre le temps où le
        # verdict est dû. Une fois le verdict `ok` rendu sur la tête gelée, un
        # avis ordinaire de l'auteur réveille de nouveau.
        self.freeze()
        self._verdict("r1", "claude", self.frozen)
        before = len(self._wakes("r1"))
        self._author_note("r1")
        self.assertEqual(len(self._wakes("r1")), before + 1)

    def test_after_a_blocked_verdict_the_authors_question_wakes(self):
        # B P2 auditeur : entre un B et le regel, la désignation ne réveille
        # personne ; c'est le moment où l'auteur questionne le bloqueur sur
        # son B. Le verdict déjà rendu sur la tête garde ce dialogue actif.
        self.freeze()
        self._verdict("r1", "claude", self.frozen, verdict="blocked")
        before = len(self._wakes("r1"))
        self._author_note("r1")
        self.assertEqual(len(self._wakes("r1")), before + 1)
        self.assertEqual(mail.passive_reason(self._notifies("r1")[0]), "")

    def test_a_consigne_from_another_agent_always_wakes(self):
        # B P2 auditeur : une consigne du pilote ou de mesh-design à un
        # relecteur (retrait, priorité avant une coupure) n'est pas l'avis de
        # l'auteur du contenu gelé : elle réveille.
        self.freeze()
        before = len(self._wakes("r1"))
        mail.send(self.db, "pilot", "r1", "#%d : priorité avant la coupure" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        mail.send(self.db, "mesh-design", "r1", "#%d : B à reconsidérer" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        self.assertEqual(len(self._wakes("r1")), before + 2)

    def test_after_the_request_is_delivered_the_authors_reply_wakes(self):
        # B #231383 : une demande livrée n'est plus un réveil en attente. La
        # réponse de l'auteur au relecteur ouvre son tour, même si elle
        # rappelle le gel et la tête (le corps, seul, la ferait passer pour
        # une annonce).
        self.freeze()
        self.db.execute(
            "UPDATE agent_mailbox SET delivered_at = now()"
            " WHERE work_item_id = %s AND kind = 'request'",
            (str(self.lot),))
        before = len(self._wakes("r1"))
        mail.send(self.db, "author", "r1",
                  "#%d : sur le gel %s, l'option A te va-t-elle ?"
                  % (self.lot, self.frozen),
                  kind="notify", work_item_id=str(self.lot))
        self.assertEqual(len(self._wakes("r1")), before + 1)

    def test_a_question_before_delivery_wakes(self):
        # B #231383 : seul un avis qui annonce le gel de cette tête double la
        # demande en attente ; une question de l'auteur réveille.
        self.freeze()
        before = len(self._wakes("r1"))
        mail.send(self.db, "author", "r1",
                  "#%d : l'option A te va-t-elle ?" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        self.assertEqual(len(self._wakes("r1")), before + 1)

    def test_a_reserved_request_still_counts_as_waiting(self):
        # B #231450 (auditeur) : le runner a réservé la demande pour un tour
        # (`meta.consigne`, sous FOR UPDATE du registre) mais ne l'a pas encore
        # livrée : le tour déjà réservé ne contient pas l'avis déposé
        # maintenant. L'avis reste actif ; une réservation relâchée redevient
        # « en attente » et l'avis passif redevient permis.
        self.freeze()
        request_id = self.db.query(
            "SELECT id FROM agent_mailbox"
            " WHERE recipient = 'r1' AND kind = 'request'"
            "   AND work_item_id = %s ORDER BY id DESC LIMIT 1",
            (str(self.lot),))[0]["id"]
        mailbox = storage.of(self.db).mailbox
        mailbox.reserve("r1", None, None, "token", ids=[request_id],
                        porteur="test", ttl_seconds=60)
        self._author_note("r1")
        self.assertEqual(mail.passive_reason(self._notifies("r1")[0]), "")
        mailbox.release("r1", "token", [request_id])
        self._author_note("r1")
        self.assertEqual(len(self._notifies("r1")), 2)
        self.assertTrue(mail.passive_reason(self._notifies("r1")[1]))

    def test_a_question_citing_gel_and_head_always_wakes(self):
        # B1 #45616 (ameesh-cx2) : citer « gel » et les 12 caractères de la
        # tête ne fait pas une annonce ; une question réveille, même avant la
        # livraison de la demande.
        self.freeze()
        before = len(self._wakes("r1"))
        mail.send(self.db, "author", "r1",
                  "#%d : sur le gel %s, quelle option choisir ?"
                  % (self.lot, self.frozen),
                  kind="notify", work_item_id=str(self.lot))
        self.assertEqual(len(self._wakes("r1")), before + 1)
        self.assertEqual(mail.passive_reason(self._notifies("r1")[-1]), "")

    def test_delivery_between_the_read_and_the_deposit_keeps_the_wake(self):
        # B #231419 : la livraison gagne entre la lecture de la demande et le
        # dépôt. Le dépôt relit sous le verrou du destinataire : l'avis reste
        # actif, le relecteur ne termine pas son tour sans le voir.
        self.freeze()
        real = _pg.Mailbox.review_notice_head
        calls = []

        def deliver_between(box, sender, recipient, lot):
            head = real(box, sender, recipient, lot)
            calls.append(head)
            if len(calls) == 1:
                box.db.execute(
                    "UPDATE agent_mailbox SET delivered_at = now()"
                    " WHERE work_item_id = %s AND kind = 'request'",
                    (str(self.lot),))
            return head

        with mock.patch.object(_pg.Mailbox, "review_notice_head",
                               deliver_between):
            mail.send(self.db, "author", "r1",
                      "#%d : nouveau gel %s" % (self.lot, self.frozen),
                      kind="notify", work_item_id=str(self.lot))
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(mail.passive_reason(self._notifies("r1")[0]), "")

    def test_the_deposit_waits_for_a_delivery_in_flight(self):
        # B #231419 : une livraison en cours tient le verrou exclusif du
        # destinataire ; le dépôt attend son COMMIT, relit la demande livrée
        # et reste actif. Sans ce verrou, le dépôt aurait lu « non livrée »
        # et rendu passif un message que le relecteur ne verrait pas.
        self.freeze()
        other = self.connect()
        self.addCleanup(other.close)
        locked = threading.Event()
        release = threading.Event()
        committed = threading.Event()
        attempted = threading.Event()
        errors = []
        real_lock = _pg.Mailbox.lock_recipient

        def note_lock(box, recipient):
            attempted.set()
            return real_lock(box, recipient)

        def deliver():
            try:
                with other.transaction() as tx:
                    tx.query("SELECT 1 FROM agent_registry"
                             " WHERE name = %s FOR UPDATE", ("r1",))
                    locked.set()
                    if not release.wait(30):
                        raise RuntimeError("dépôt non libéré")
                    tx.execute(
                        "UPDATE agent_mailbox SET delivered_at = now()"
                        " WHERE work_item_id = %s AND kind = 'request'",
                        (str(self.lot),))
                committed.set()
            except Exception as exc:  # noqa: BLE001 - remonté au témoin
                errors.append(exc)
                locked.set()
                committed.set()

        def send():
            try:
                mail.send(self.db, "author", "r1",
                          "#%d : nouveau gel %s" % (self.lot, self.frozen),
                          kind="notify", work_item_id=str(self.lot))
            except Exception as exc:  # noqa: BLE001 - remonté au témoin
                errors.append(exc)

        deliver_thread = threading.Thread(target=deliver, daemon=True)
        deliver_thread.start()
        self.assertTrue(locked.wait(10), "livraison non verrouillée")
        with mock.patch.object(_pg.Mailbox, "lock_recipient", note_lock):
            send_thread = threading.Thread(target=send, daemon=True)
            send_thread.start()
            self.assertTrue(attempted.wait(10), "dépôt sans verrou")
            time.sleep(0.2)
            self.assertTrue(send_thread.is_alive(),
                            "le dépôt n'a pas attendu la livraison")
            release.set()
            send_thread.join(30)
            self.assertFalse(send_thread.is_alive(), "dépôt bloqué")
            deliver_thread.join(30)
        self.assertEqual(errors, [])
        self.assertTrue(committed.is_set())
        self.assertEqual(mail.passive_reason(self._notifies("r1")[0]), "")


class ContinuedSeatsCorrectorTest(unittest.TestCase):
    """#8118 : un correcteur exclu ne garde pas son siège par continuation.

    Le garde comparait la colonne à `str(corrector)` — la représentation d'un
    dictionnaire — au lieu du nom : un correcteur exclu par un doute de
    provenance (famille vide) traversait la continuation et restait relecteur.
    """

    def test_an_excluded_corrector_breaks_the_continuation(self):
        inputs = types.SimpleNamespace(
            manual=[], waiver=False, unknown_authors=[], doubts=[],
            policy=types.SimpleNamespace(required=2,
                                         families=["claude", "codex"]),
            answered=(), authors=[{"name": "author", "family": "deepseek"}],
            correctors=[{"name": "cx3", "family": "",
                         "unresolved": "afd35c55"}],
            candidates=[types.SimpleNamespace(name="cx3", family="codex",
                                              available=True),
                        types.SimpleNamespace(name="r1", family="claude",
                                              available=True)])
        context = {"freeze": {"id": 11},
                   "reviewers": [{"name": "cx3", "family": "codex"},
                                 {"name": "r1", "family": "claude"}]}
        with mock.patch.object(act.pg, "PgPort") as port:
            port.return_value.previous_review.return_value = context
            seats = act._continued_seats(
                None, {"id": 7, "reviewers": ["cx3", "r1"]},
                inputs, {"id": 12})
        self.assertEqual(seats, [])

    def test_a_changed_family_breaks_the_continuation(self):
        """#2447 : r1 et r2 réenregistrés en deepseek ne sont plus indépendants.

        La continuation lisait la famille dans la désignation précédente et
        gardait deux sièges devenus de la famille de l'auteur, au lieu de
        laisser la désignation complète constater le quorum perdu (#8118).
        """
        inputs = types.SimpleNamespace(
            manual=[], waiver=False, unknown_authors=[], doubts=[],
            policy=types.SimpleNamespace(required=2,
                                         families=["claude", "codex"]),
            answered=(), authors=[{"name": "author", "family": "deepseek"}],
            correctors=[],
            candidates=[types.SimpleNamespace(name="r1", family="deepseek",
                                              available=True),
                        types.SimpleNamespace(name="r2", family="deepseek",
                                              available=True)])
        context = {"freeze": {"id": 11},
                   "reviewers": [{"name": "r1", "family": "claude"},
                                 {"name": "r2", "family": "codex"}]}
        with mock.patch.object(act.pg, "PgPort") as port:
            port.return_value.previous_review.return_value = context
            seats = act._continued_seats(
                None, {"id": 7, "reviewers": ["r1", "r2"]},
                inputs, {"id": 12})
        self.assertEqual(seats, [])

    def test_a_swapped_family_breaks_the_continuation_even_with_quorum(self):
        """Même quorum de familles, mais les sièges ont changé de famille."""
        inputs = types.SimpleNamespace(
            manual=[], waiver=False, unknown_authors=[], doubts=[],
            policy=types.SimpleNamespace(required=2,
                                         families=["claude", "codex"]),
            answered=(), authors=[{"name": "author", "family": "deepseek"}],
            correctors=[],
            candidates=[types.SimpleNamespace(name="r1", family="codex",
                                              available=True),
                        types.SimpleNamespace(name="r2", family="claude",
                                              available=True)])
        context = {"freeze": {"id": 11},
                   "reviewers": [{"name": "r1", "family": "claude"},
                                 {"name": "r2", "family": "codex"}]}
        with mock.patch.object(act.pg, "PgPort") as port:
            port.return_value.previous_review.return_value = context
            seats = act._continued_seats(
                None, {"id": 7, "reviewers": ["r1", "r2"]},
                inputs, {"id": 12})
        self.assertEqual(seats, [])


if __name__ == "__main__":
    unittest.main()
