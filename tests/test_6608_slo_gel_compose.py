# SPDX-License-Identifier: AGPL-3.0-only
"""#6608 : pas de `freeze-not-head` quand le gel est déjà composé ou en composition.

Le 09/11 à 23:13Z, nexlink #6511 a levé une brèche SLO « GO → fusion »
(`freeze-not-head`) alors que le pilote composait le paquet 515 depuis son gel
(porteur #6593 enregistré à 23:20:21). `recorded_packet_attachments` détachait
le membre dès que la tête de branche différait de la source composée, et la
passe SLO ignorait le bail de composition (#5941/#6563) : escalade et regel
demandés à tort (auditeur 23:16Z, retirés à 23:30Z).

(a) Membre composé depuis son dernier gel, tête bougée après : gardé, attente
`head_moved_after_composition`, avis léger à l'auteur, aucune escalade.
(b) Bail de composition tenu, porteur pas encore enregistré : attente, pas de
brèche.
"""
from __future__ import annotations

from unittest import mock

from ameesh import exploitation, packet_composition, planned_waits, stagnation, storage

from . import test_5206_recomposition_membership as base
from .test_789_slo_alerts import _seuils


class GelComposeTest(base.RecompositionTest):
    #: S2 : un commit poussé par l'auteur après le gel composé
    MOVED = "f" * 40

    def move_head(self):
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (self.MOVED, int(self.lot["id"])))

    def planned_wait(self):
        rows = storage.of(self.db).operations.open_lots_activity(None)
        described = stagnation.describe(self.db, rows, now=self.now, open_items=rows)
        return (described.get(int(self.lot["id"])) or {}).get("planned_wait") or {}

    def delivery_pass(self):
        exploitation.alerts(self.cfg, self.db, now=self.now, record_footprint=True,
                            **_seuils(orchestrators_declared=["orch"]))

    def notes(self):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s", (int(self.lot["id"]),
                                  exploitation.HEAD_MOVED_NOTE + "%"))]

    def mails(self):
        return self.db.query(
            "SELECT body, payload FROM agent_mailbox WHERE recipient = 'dev1'"
            " AND body LIKE %s", ("%%packet #%d%%" % int(self.vehicle["id"]),))

    # -- (a) témoin : gel S1, membre à S1, tête S2 ----------------------------
    def test_tete_bougee_apres_le_gel_compose_ne_fait_pas_de_breche(self):
        self.recorded(36)
        self.move_head()
        self.assertEqual(self.verdict_merge(), [])

    def test_l_attente_nomme_la_tete_bougee(self):
        self.recorded(36)
        self.move_head()
        wait = self.planned_wait()
        self.assertEqual(wait.get("what"), "packet")
        self.assertEqual(wait["head_moved_after_composition"],
                         {"frozen": self.freeze, "head": self.MOVED})

    def test_avis_leger_a_l_auteur_une_seule_fois(self):
        self.recorded(36)
        self.move_head()
        self.delivery_pass()
        self.delivery_pass()
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("packet #%d" % int(self.vehicle["id"]), notes[0])
        self.assertIn(self.MOVED[:12], notes[0])
        mails = self.mails()
        self.assertEqual(len(mails), 1)
        self.assertIn("not in packet", mails[0]["body"])
        self.assertTrue((mails[0]["payload"] or {}).get("info"))

    def test_une_lecture_n_ecrit_rien(self):
        self.recorded(36)
        self.move_head()
        self.verdict_merge()
        self.assertEqual(self.notes(), [])

    def test_tete_a_la_source_sans_avis(self):
        self.recorded(36)
        self.assertNotIn("head_moved_after_composition", self.planned_wait())
        self.delivery_pass()
        self.assertEqual(self.notes(), [])

    # -- inchangé : un regel après la composition détache --------------------
    def test_un_regel_apres_la_composition_detache_toujours(self):
        self.recorded(36)
        self.move_head()
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now(), %s, 'dev1')", (self.lot["id"], self.MOVED))
        self.assertNotEqual(self.planned_wait().get("what"), "packet")

    def test_la_regle_sans_stockage(self):
        carrier = dict(id=90, state="qa", branch="integration/paquet-test",
                       branch_head=self.HEAD)
        unit = dict(id=1, state="qa", branch_head=self.MOVED)
        store = mock.Mock()
        store.work.packet_members.return_value = [dict(
            package_id=90, work_item_id=1, head_sha=self.HEAD,
            source_sha=self.freeze)]
        store.progress.lot_milestones.return_value = [dict(
            work_item_id=1, kind="frozen", sha=self.freeze, at_ts=1.0, id=1)]
        moved = {}
        attach = planned_waits.recorded_packet_attachments
        self.assertEqual(attach(store, [carrier], [unit], now=10.0, moved=moved),
                         {90: {1}})
        self.assertEqual(moved, {1: {"vehicle": 90, "frozen": self.freeze,
                                     "head": self.MOVED}})
        # sans gel connu, la tête bougée ne prouve rien : détaché
        store.progress.lot_milestones.return_value = []
        self.assertEqual(attach(store, [carrier], [unit], now=10.0), {})


class BailSansPorteurTest(base.RecompositionTest):
    """(b) Le composeur tient le bail, le porteur n'est pas encore enregistré."""

    def setUp(self):
        super().setUp()
        # aucun porteur : le paquet n'est pas encore enregistré
        self.db.execute("DELETE FROM work_packet_members")
        self.db.execute("UPDATE work_items SET branch = NULL WHERE id = %s",
                        (int(self.vehicle["id"]),))

    def hold(self):
        app = str(self.lot.get("app") or "")
        lease = packet_composition.Lease(self.db, app, owner="pilote:test:1:x")
        self.assertTrue(lease.acquire())
        self.addCleanup(lease.release)
        return lease

    def test_sans_bail_la_breche_part(self):
        self.assertEqual([a["step"] for a in self.verdict_merge()], ["verdict_merge"])

    def test_bail_tenu_sans_porteur_pas_de_breche(self):
        self.hold()
        self.assertEqual(self.verdict_merge(), [])

    def test_bail_rendu_la_breche_revient(self):
        self.hold().release()
        self.assertEqual([a["step"] for a in self.verdict_merge()], ["verdict_merge"])

    def test_bail_illisible_ne_tait_rien(self):
        with mock.patch.object(packet_composition, "holder",
                               side_effect=RuntimeError("down")):
            self.assertEqual([a["step"] for a in self.verdict_merge()],
                             ["verdict_merge"])


# les témoins de #5206 restent à leur module : pas de double exécution ici
for _name in [n for n in vars(base.RecompositionTest) if n.startswith("test_")]:
    for _cls in (GelComposeTest, BailSansPorteurTest):
        if _name not in vars(_cls):
            setattr(_cls, _name, None)
