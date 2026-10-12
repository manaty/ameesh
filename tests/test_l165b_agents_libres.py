# SPDX-License-Identifier: AGPL-3.0-only
"""L165b : un agent au repos dont tous les lots attendent un autre que lui
(`qa`, `waiting_human`, `blocked`, ou début prévu après aujourd'hui) est libre
pour un lot de l'équipe et compte dans `idle_capacity`, qui part aussi à
l'agent de conception ; l'auditeur reçoit le geste de niveau 1."""
from __future__ import annotations

import time
import unittest

from ameesh import alert_briefs, backlog, mail, storage, sous_utilisation, work

from .support import hold_for_owner
from .test_l119_backlog import HEURE
from .test_l165_prise_lots import _Lots


class _Libres(_Lots):
    def tenu(self, agent, state, title=None):
        """Un lot ouvert assigné à `agent`, amené dans `state`."""
        item = self.lot(title or "lot de %s en %s" % (agent, state))
        work.assign(self.db, item["id"], agent)
        if state == "waiting_human":
            hold_for_owner(self.db, item["id"])
        elif state != "intake":
            work.move(self.db, item["id"], "build")
            if state != "build":
                work.move(self.db, item["id"], state)
        return item

    def listing(self):
        return storage.of(self.db).operations.listing()


class PriseTest(_Libres):
    def test_lots_qui_attendent_un_autre_ne_l_occupent_pas(self):
        for state in ("qa", "blocked", "waiting_human"):
            with self.subTest(state=state):
                self.db.execute("DELETE FROM agent_mailbox")
                self.db.execute("DELETE FROM work_items")
                self.agent("dev7", team="nexlink")
                self.tenu("dev7", state)
                lot = self.lot("à prendre")
                row = {r["name"]: r for r in self.listing()}["dev7"]
                self.assertEqual(row["active_lots"], 0)
                out = self.take()
                self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                                 [("dev7", lot["id"])])

    def test_lot_en_cours_occupe_et_lot_pris_aussi(self):
        self.agent("dev7", team="nexlink")
        en_cours = self.tenu("dev7", "build")
        self.lot("à prendre")
        self.assertEqual(self.take(), [])
        work.move(self.db, en_cours["id"], "qa")
        self.assertEqual(len(self.take()), 1)
        # le lot pris (intake, assigné) l'occupe : pas de deuxième prise
        self.lot("un autre")
        self.assertEqual(self.take(), [])

    def test_debut_prevu_apres_aujourd_hui(self):
        self.agent("dev7", team="nexlink")
        plus_tard = self.tenu("dev7", "intake")
        self.db.execute("UPDATE work_items SET planned_start = current_date + 2"
                        " WHERE id = %s", (plus_tard["id"],))
        lot = self.lot("à prendre")
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])

    def test_courrier_en_attente_ou_lot_de_session_en_cours(self):
        self.agent("dev7", team="nexlink")
        self.tenu("dev7", "qa")
        self.lot("à prendre")
        mail.send(self.db, "orchestrateur", "dev7", "relis ceci")
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()")
        self.agent("claude2", team="autre")
        session = self.tenu("claude2", "build", title="lot de session d'un autre")
        self.db.execute("UPDATE agent_registry SET session_work_item = %s"
                        " WHERE name = 'dev7'", (str(session["id"]),))
        self.assertEqual(self.take(), [])
        work.move(self.db, session["id"], "qa")
        self.assertEqual(len(self.take()), 1)

    def test_la_file_d_amelioration_reste_stricte(self):
        self.agent("dev7", team="nexlink")
        self.tenu("dev7", "qa")
        element = self.element("dette", 90, team="nexlink")
        self.assertEqual(self.take(idle_s=60, lot_idle_s=0), [])
        self.assertIsNone(work.get(self.db, element["id"])["assignee"])


class IdleCapacityTest(_Libres):
    def test_compte_les_agents_en_attente_et_previent_la_conception(self):
        self.agent("orchestrateur", team="nexlink", status="running")
        self.agent("mesh-design", team="nexlink", status="running")
        self.agent("dev7", team="nexlink")
        self.agent("dev4", team="nexlink")
        self.tenu("dev7", "qa")
        self.tenu("dev4", "build")
        lot = self.lot("sans agent")
        alerts = sous_utilisation.idle_capacity(
            self.db, self.listing(), time.time(), ["orchestrateur"],
            designers=["mesh-design", "orchestrateur"])
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["agents"], ["dev7"])
        self.assertEqual(alert["lots"], [lot["id"]])
        self.assertEqual(alert["designers"], ["mesh-design"])
        self.assertIn("without a lot in progress", alert["detail"])
        briefs = alert_briefs.briefs(alert, self.listing(), {lot["id"]: "sans agent"},
                                     auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["auditeur", "mesh-design", "orchestrateur"])
        self.assertIn("#%d \"sans agent\"" % lot["id"], briefs["mesh-design"])
        self.assertIn("ameesh work assign <lot> <agent>", briefs["auditeur"])
        self.assertIn("level 1", briefs["auditeur"])

    def test_sans_lot_en_attente_l_auditeur_n_a_pas_de_geste(self):
        text = alert_briefs._for_auditor({"detail": "x", "lots": []})
        self.assertNotIn("level 1", text)

    def test_roles_de_conception(self):
        self.assertEqual(sous_utilisation.DESIGN_ROLES, ("conception", "design"))
        self.assertTrue(set(sous_utilisation.DESIGN_ROLES) <= set(work.CORRECTOR_ROLES))


if __name__ == "__main__":
    unittest.main()
