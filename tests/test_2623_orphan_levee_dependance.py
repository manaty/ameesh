# SPDX-License-Identifier: AGPL-3.0-only
"""#2623 — `orphan_lot` compte l'inactivité depuis la levée de la dépendance.

Un lot resté bloqué plusieurs jours ne devient pas orphelin `sans_tour` à la
seconde où son dernier bloqueur est fusionné : son détenteur n'a pas encore pu
reprendre. L'inactivité court depuis la levée du DERNIER bloqueur (même helper
que #2551, `_prerequisite_satisfied_ts`), comme elle court depuis la dernière
activité du lot quand celle-ci est plus récente. Une dépendance encore ouverte
continue de l'exclure (#2526), et les raisons `inconnu`/`arrete` restent vraies.
"""
from __future__ import annotations

import unittest

from ameesh import exploitation, registry, storage, work

from .support import PgTestCase


class OrphanLotLeveeDependanceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("dort", "claude", cwd=self.tmp)

    def _alerts(self, **kw):
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] == "orphan_lot"]

    def _vieillir(self, hours=2):
        for sql in (
            "UPDATE work_items SET created_at = now() - interval '%d hours',"
            " updated_at = now() - interval '%d hours'" % (hours, hours),
            "UPDATE work_item_events SET created_at = now() - interval '%d hours'" % hours,
            "UPDATE work_item_milestones SET at = now() - interval '%d hours'" % hours,
        ):
            self.db.execute(sql)

    def _fusionner(self, lot, *, hours=0):
        """Fusion par l'état (le trigger 0013 pose le jalon `merged`), datée."""
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (lot["id"],))
        if hours:
            self.db.execute(
                "UPDATE work_item_milestones SET at = now() - make_interval(hours => %s)"
                " WHERE work_item_id = %s AND kind = 'merged'", (hours, lot["id"]))

    def test_bloqueur_fusionne_a_l_instant_ne_rend_pas_orphelin(self):
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        bloque = work.add(self.db, title="attend l'amont", assignee="dort")
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")
        self._vieillir()
        # Dépendance ouverte : le lot attend, il n'est pas orphelin (#2526).
        self.assertEqual(self._alerts(), [])
        # Bloqueur fusionné à l'instant : l'inactivité ne court pas encore.
        self._fusionner(amont)
        self.assertEqual(self._alerts(), [])
        # La levée elle-même est ancienne : le lot redevient orphelin.
        self._fusionner(amont, hours=2)
        alertes = self._alerts()
        self.assertEqual([(a["lot"], a["reason"]) for a in alertes],
                         [(bloque["id"], "sans_tour")])
        self.assertGreaterEqual(alertes[0]["value"], 1800)

    def test_le_dernier_bloqueur_leve_gouverne(self):
        amont_a = work.add(self.db, title="amont a", assignee="human:proprio")
        amont_b = work.add(self.db, title="amont b", assignee="human:proprio")
        bloque = work.add(self.db, title="attend a puis b", assignee="dort")
        for amont in (amont_a, amont_b):
            storage.of(self.db).work_dependencies.add(
                bloque["id"], amont["id"], actor="test")
        self._vieillir()
        # a est fusionné depuis 3 h, b vient de l'être : c'est b qui gouverne.
        self._fusionner(amont_a, hours=3)
        self.assertEqual(self._alerts(), [])
        self._fusionner(amont_b)
        self.assertEqual(self._alerts(), [])
        # b vieillit au-delà du seuil : les deux levées sont anciennes.
        self._fusionner(amont_b, hours=2)
        self.assertEqual([(a["lot"], a["reason"]) for a in self._alerts()],
                         [(bloque["id"], "sans_tour")])

    def test_une_dependance_encore_ouverte_ecarte_toujours(self):
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        autre = work.add(self.db, title="autre amont", assignee="human:proprio")
        bloque = work.add(self.db, title="attend les deux", assignee="dort")
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")
        storage.of(self.db).work_dependencies.add(bloque["id"], autre["id"], actor="test")
        self._vieillir()
        # Une levée récente ne suffit pas tant qu'une arête reste ouverte.
        self._fusionner(amont)
        self.assertEqual(self._alerts(), [])

    def test_arrete_reste_vrai_malgre_une_levee_recente(self):
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        bloque = work.add(self.db, title="attend l'amont", assignee="dort")
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")
        self._vieillir()
        self._fusionner(amont)
        registry.set_status(self.db, "dort", "stopped", status_text="arrêté",
                            stop_reason="manuel")
        self.assertEqual([(a["lot"], a["reason"]) for a in self._alerts()],
                         [(bloque["id"], "arrete")])


if __name__ == "__main__":
    unittest.main()
