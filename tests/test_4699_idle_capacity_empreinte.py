# SPDX-License-Identifier: AGPL-3.0-only
"""#4699 — `idle_capacity` applique la garde d'empreinte (#3185) comme le débit.

Constat du 29/10 : l'alerte proposait « 2 startable now » (#4521, #4667) que
`ameesh work assign` refusait ensuite (« its footprint overlaps lot(s) in
progress »). Le débit filtre déjà ces lots (#4561) ; l'alerte ne lisait que
les attentes planifiées (`split_startable`). Témoin : un lot d'intake dont
l'empreinte chevauche un lot en cours est absent des démarrables, présent
dans `blocked_lots` avec `what: footprint` et son bloqueur, et le brief ne
propose pas de l'attribuer.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import storage, sous_utilisation, work
from ameesh.storage.postgres.operations import Operations

from .test_l94_underuse import _Base


def _footprint(db, item, *paths):
    storage.of(db).work_footprints.replace(int(item["id"]), "manual",
                                           list(paths), actor="test")


class SplitStartableTest(unittest.TestCase):
    """La classification pure : l'empreinte se range comme une dépendance."""

    def test_empreinte_range_le_lot_dans_les_bloques(self):
        lots = [{"id": 1}, {"id": 2}, {"id": 3}]
        waits = {1: {"what": "dependency", "lots": [9]}}
        footprint = {2: [{"id": 7, "state": "qa", "assignee": "dev1"}]}
        startable, blocked = sous_utilisation.split_startable(lots, waits,
                                                              footprint)
        self.assertEqual([lot["id"] for lot in startable], [3])
        self.assertEqual(blocked[1], waits[1])
        self.assertEqual(blocked[2], {"what": "footprint", "lots": [7]})
        self.assertIn("#2 waits on the footprint of #7",
                      sous_utilisation.waiting_text(blocked))


class IdleCapacityEmpreinteTest(_Base):
    """Le témoin de bout en bout sur l'alerte."""

    def alerte(self, orchestrators=(), **kw):
        out = sous_utilisation.idle_capacity(
            self.db, self.listing(), self.now, list(orchestrators), paid=(), **kw)
        self.assertLessEqual(len(out), 1)
        return out[0] if out else None

    def decor(self):
        """Un lot en cours sur `x.py`, un lot d'intake recouvert, un libre."""
        self.agent("dev1")
        self.agent("dev2")
        occupe = work.add(self.db, title="occupe", app="ameesh", assignee="dev2")
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (occupe["id"],))
        recouvert = work.add(self.db, title="recouvert", app="ameesh")
        libre = work.add(self.db, title="libre", app="ameesh")
        _footprint(self.db, occupe, "src/ameesh/x.py")
        _footprint(self.db, recouvert, "src/ameesh/x.py")
        return occupe, recouvert, libre

    def test_un_lot_recouvert_n_est_pas_demarrable(self):
        occupe, recouvert, libre = self.decor()
        alerte = self.alerte()
        self.assertIsNotNone(alerte)
        self.assertEqual(alerte["startable_lots"], [libre["id"]])
        self.assertNotIn(recouvert["id"], alerte["startable_lots"])
        self.assertEqual(alerte["blocked_lots"][str(recouvert["id"])],
                         {"what": "footprint", "lots": [occupe["id"]]})
        self.assertIn("1 startable now: #%d" % libre["id"], alerte["detail"])
        self.assertIn("#%d waits on the footprint of #%d"
                      % (recouvert["id"], occupe["id"]), alerte["detail"])

    def test_sans_lot_demarrable_l_alerte_ne_se_leve_pas(self):
        """#2337 B1 : un lot recouvert seul n'est pas du travail à confier."""
        self.agent("dev1")
        self.agent("dev2")
        occupe = work.add(self.db, title="occupe", app="ameesh", assignee="dev2")
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (occupe["id"],))
        recouvert = work.add(self.db, title="recouvert", app="ameesh")
        _footprint(self.db, occupe, "src/ameesh/x.py")
        _footprint(self.db, recouvert, "src/ameesh/x.py")
        self.assertIsNone(self.alerte())

    def test_le_brief_ne_propose_pas_le_lot_recouvert(self):
        occupe, recouvert, libre = self.decor()
        self.agent("orch")
        alerte = self.alerte(orchestrators=["orch"])
        brief = sous_utilisation.orchestrator_briefs(
            alerte, self.listing(),
            {libre["id"]: "libre", recouvert["id"]: "recouvert"})["orch"]
        self.assertIn('Open lots without an agent: #%d "libre"' % libre["id"],
                      brief)
        self.assertNotIn('"recouvert"', brief)
        self.assertIn("Waiting for a prerequisite: #%d waits on the footprint of #%d"
                      % (recouvert["id"], occupe["id"]), brief)
        self.assertIn("Give them work tied to a lot", brief)

    def test_un_lot_apres_les_500_premiers_est_vu(self):
        # #4717 : l'alerte lit tous les lots ouverts, comme le débit
        # (`open_lots_activity(None)`) ; un plafond à 500 laissait l'occupant
        # d'un lot démarrable au-delà invisible (#3185).
        self.agent("dev1")
        self.agent("dev2")
        lots = [{"id": i, "state": "intake", "app": "ameesh",
                 "type": "evolution", "assignee": "dev2"}
                for i in range(1, 501)]
        lots.append({"id": 501, "state": "intake", "app": "ameesh",
                     "type": "evolution", "assignee": None})
        seen = {}
        original = Operations.open_lots_activity

        def read(operations, limit):
            seen["limit"] = limit
            return original(operations, limit) if limit is not None else lots

        with mock.patch.object(Operations, "open_lots_activity", read):
            alerte = self.alerte()
        self.assertIsNone(seen["limit"])
        self.assertEqual(alerte["startable_lots"], [501])


if __name__ == "__main__":
    unittest.main()
