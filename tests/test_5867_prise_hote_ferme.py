# SPDX-License-Identifier: AGPL-3.0-only
"""#5867 : un hôte qui ne peut pas ouvrir de tour (drainage, porte fermée,
hôte perdu, pause de l'owner) n'est ni un preneur de lot de l'équipe (L165)
ni une capacité disponible (`idle_capacity`, découpage #5122).

Constat : #5864 a été « pris de lui-même par ameesh-dev5, au repos » alors que
son hôte ameesh-agents-1 était en drainage depuis 00:57Z ; aucun tour ne
s'ouvrait sur AG1 avant l'undrain, le lot serait resté immobile plus de 7 h.
L'élection lisait déjà la porte (`ameesh role status` : « host gate closed
(draining) ») ; la prise ne la lisait pas.
"""
from __future__ import annotations

from ameesh import debit, file_decoupage, host_lifecycle, storage, sous_utilisation, work

from .test_l119_backlog import HEURE, _Base


class _Hotes(_Base):
    """Banc de la prise : les tables d'hôte ne sont pas truncatées par le banc."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle, host_resources, host_holds CASCADE")

    def lot(self, title, **kw):
        item = work.add(self.db, title=title, app="nexlink", **kw)
        # created_at recule : la prise laisse passer un lot tout juste créé
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (HEURE, item["id"]))
        return item

    def take(self, **kw):
        kw.setdefault("orchestras", ["orchestrateur"])
        return super().take(**kw)

    def liste(self):
        return storage.of(self.db).operations.listing()

    def drain(self, host):
        return storage.of(self.db).host_lifecycle.drain(
            host, actor="human:proprio", duration=3600)


class PriseHoteFermeTest(_Hotes):
    def test_prise_ecarte_un_hote_en_drainage(self):
        # a-draine passe avant b-sain (même repos, ordre du nom) : sans la
        # garde, la prise lui donnerait le lot et il dormirait jusqu'à
        # l'undrain.
        self.agent("a-draine", team="nexlink", host="h-draine")
        self.agent("b-sain", team="nexlink", host="h-sain")
        self.drain("h-draine")
        lot = self.lot("lot de l'equipe")

        taken = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in taken],
                         [("b-sain", lot["id"])])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "b-sain")

    def test_prise_ecarte_un_hote_a_la_porte_fermee(self):
        self.agent("a-porte", team="nexlink", host="h-porte")
        storage.of(self.db).hosts.record({"host": "h-porte", "gate_state": "stopped"})
        lot = self.lot("lot pour un hote ferme")

        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

        # Témoin positif : la porte se rouvre, le même agent prend le lot.
        storage.of(self.db).hosts.record({"host": "h-porte", "gate_state": "available"})
        self.assertEqual([t["agent"] for t in self.take()], ["a-porte"])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "a-porte")


class CapaciteHoteFermeTest(_Hotes):
    def test_closed_hosts_lit_les_trois_sources(self):
        self.drain("h-drain")
        storage.of(self.db).hosts.record({"host": "h-gate", "gate_state": "draining"})
        storage.of(self.db).hosts.hold("h-hold", reason="maintenance",
                                       author="human:proprio", seconds=600)
        self.assertEqual(host_lifecycle.closed_hosts(self.db), {
            "h-drain": "host gate closed (draining)",
            "h-gate": "host gate draining",
            "h-hold": "host held by human:proprio: maintenance",
        })

    def test_idle_capacity_ne_compte_pas_un_hote_en_drainage(self):
        self.agent("a-draine", team="nexlink", host="h-draine")
        self.drain("h-draine")
        lot = self.lot("lot qui attend un preneur")
        listing = self.liste()

        # L'agent est toujours au repos au sens du registre ; c'est la
        # capacité qui l'écarte.
        self.assertEqual([row["name"] for row in sous_utilisation.idle_agents(
            listing, self.now, 0.0, actionable_idle=True)], ["a-draine"])
        self.assertEqual(sous_utilisation.idle_capacity(
            self.db, listing, self.now, [], idle_s=0.1, cfg=self.cfg), [])

        # Témoin positif : l'hôte revenu, la même capacité alerte.
        self.db.execute("UPDATE host_lifecycle SET mode = 'available' WHERE host = 'h-draine'")
        alerts = sous_utilisation.idle_capacity(
            self.db, listing, self.now, [], idle_s=0.1, cfg=self.cfg)
        self.assertEqual(len(alerts), 1)
        self.assertIn("a-draine", alerts[0]["agents"])
        self.assertIn(lot["id"], alerts[0]["startable_lots"])

    def test_decoupage_ne_compte_pas_un_hote_en_drainage(self):
        self.agent("a-draine", team="nexlink", host="h-draine")
        self.drain("h-draine")
        self.lot("lot qui attend un preneur")
        lots = storage.of(self.db).operations.open_lots_activity(None)
        listing = self.liste()

        # Sans la garde, la capacité fantôme ferait ouvrir un lot de découpage.
        closed = host_lifecycle.closed_hosts(self.db)
        self.assertEqual(file_decoupage.measure(
            listing, lots, {}, self.now, [], configured=False,
            closed_hosts=closed), {})

        # Témoin positif : le même banc, hôte rouvert, mesure la capacité.
        self.db.execute("UPDATE host_lifecycle SET mode = 'available' WHERE host = 'h-draine'")
        measures = file_decoupage.measure(
            listing, lots, {}, self.now, [], configured=False,
            closed_hosts=host_lifecycle.closed_hosts(self.db))
        self.assertEqual(measures["nexlink"].wakeable, ["a-draine"])
        self.assertEqual(measures["nexlink"].idle, ["a-draine"])

    def test_le_debit_ne_compte_pas_un_hote_en_drainage(self):
        self.agent("a-draine", team="nexlink", host="h-draine")
        self.drain("h-draine")
        unreadable: list = []
        cap = debit.read_capacity(self.db, cfg=self.cfg, now=self.now, hours=6,
                                  turns=[], canons=[], unreadable=unreadable)
        self.assertEqual(cap["mesh"]["idle"], 0)
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(unreadable, [])
