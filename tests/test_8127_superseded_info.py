# SPDX-License-Identifier: AGPL-3.0-only
"""#8127 — la notification #2681 est une information, pas un réveil.

Un blocage posé sur une tête antérieure ne bloque plus la tête gelée ; le
relecteur l'apprend par un courrier qui n'appelle aucun geste (« si vous
voulez re-relire »). Déposé actif, ce courrier ouvrait un tour pour rien : au
R1 de #6002, c'est lui qui a réveillé le seul relecteur disponible, onze
secondes avant la demande de retour de l'adaptateur.

Le réveil reste celui de la demande de désignation : quand le bloqueur est
redésigné sur la nouvelle tête, la demande porte déjà son tour et le courrier
#2681 ne double pas.
"""
from __future__ import annotations

from ameesh import mail, storage, work

from .support import PgTestCase
from .test_2447_review_designation_activation import (
    _ActivationFixture, register_agent)


def superseded(mailbox):
    return [row for row in mailbox
            if (row["payload"] or {}).get("review_block_superseded")]


class SupersededNotificationTest(_ActivationFixture, PgTestCase):
    def setUp(self):
        super().setUp()
        self.ops = storage.of(self.db).work

    def block(self, actor, sha):
        self.ops.add_milestone(self.lot, "verdict", sha=sha, actor=actor,
                               verdict="blocked", note="")

    def requests(self):
        return [row for row in self.mailbox() if row["kind"] == "request"]

    def next_head(self):
        self.repo.write("src/ameesh/mail.py", "lot\nnext\n")
        self.frozen = self.repo.commit("next head")
        self.receipts()

    def test_an_old_block_is_notified_as_passive_information(self):
        # H1 : r1 porte une charge, le siège claude revient donc à r3, qui
        # bloque H1. H2 : la charge passe à r3 et le pilote redésigne r1 à la
        # main ; le bloqueur r3 n'est pas repris et reçoit le courrier #2681.
        register_agent(self.db, "r3", harness="claude", host=self.cfg.host,
                       cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = 'r3'")
        charge = work.add(self.db, title="charge de r1", app="ameesh",
                          assignee="r1")
        _first, h1 = self.freeze()
        self.assertEqual(sorted(r["name"] for r in h1["reviewers"]),
                         ["r2", "r3"], h1)
        self.block("r3", self.frozen)

        self.db.execute("UPDATE work_items SET assignee = 'r3' WHERE id = %s",
                        (int(charge["id"]),))
        # H2 : le pilote désigne r1 à la main pour la famille claude ; le
        # siège r3 du gel précédent n'est pas repris.
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['r1']"
                        " WHERE id = %s", (self.lot,))
        self.next_head()
        _second, h2 = self.freeze()
        self.assertEqual([r["name"] for r in h2["reviewers"]], ["r2"], h2)
        # Le siège manuel r1 est gardé, le bloqueur r3 n'est pas repris.
        self.assertEqual(sorted(work.get(self.db, self.lot)["reviewers"]),
                         ["r1", "r2"])

        notice = superseded(self.mailbox())
        self.assertEqual([row["recipient"] for row in notice], ["r3"])
        self.assertEqual(notice[0]["kind"], "notify")
        self.assertTrue(notice[0]["payload"]["info"])
        self.assertEqual(
            notice[0]["payload"]["review_block_superseded"]["sha"], self.frozen)
        # Aucun réveil : le courrier est lu au tour suivant du destinataire.
        self.assertEqual(mail.passive_reason(notice[0]), "info")
        self.assertEqual(mail.passive_label(notice[0]),
                         "information, no action expected")
        self.assertFalse(mail.mid_turn(notice[0]))
        for row in self.requests():
            self.assertEqual(mail.passive_reason(row), "", row)

    def test_a_redesignated_blocker_gets_only_the_designation_request(self):
        # Le bloqueur de H1 garde son siège sur H2 : la demande de désignation
        # est son seul réveil, le courrier #2681 ne double pas.
        _first, h1 = self.freeze()
        self.assertEqual(sorted(r["name"] for r in h1["reviewers"]),
                         ["r1", "r2"], h1)
        self.block("r1", self.frozen)

        self.next_head()
        _second, h2 = self.freeze()
        self.assertEqual(sorted(r["name"] for r in h2["reviewers"]),
                         ["r1", "r2"], h2)

        self.assertEqual(superseded(self.mailbox()), [])
        requests = self.requests()
        self.assertEqual([row["recipient"] for row in requests], ["r1", "r2"])
        for row in requests:
            self.assertEqual(mail.passive_reason(row), "", row)
