# SPDX-License-Identifier: AGPL-3.0-only
"""#2526 — `orphan_lot` d'un lot qui attend une dépendance bloquante ouverte.

Un lot `intake`/`build` qu'une dépendance explicite retient n'est pas orphelin
`sans_tour` : la file de l'exécuteur l'exclut déjà (`lot_sessions.actionable`),
et son détenteur n'a rien à y faire. Les raisons `inconnu` et `arrete` restent
vraies malgré la dépendance. Un bloqueur ne satisfait son dépendant qu'une
fois fusionné (#2252).
"""
from __future__ import annotations

import unittest

from ameesh import exploitation, registry, storage, work

from .support import PgTestCase


class OrphanLotDependanceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("dort", "claude", cwd=self.tmp)

    def _alerts(self, **kw):
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] == "orphan_lot"]

    def _vieillir(self):
        for sql in (
            "UPDATE work_items SET created_at = now() - interval '2 hours',"
            " updated_at = now() - interval '2 hours'",
            "UPDATE work_item_events SET created_at = now() - interval '2 hours'",
            "UPDATE work_item_milestones SET at = now() - interval '2 hours'",
        ):
            self.db.execute(sql)

    def test_dependance_bloquante_ouverte_puis_fermee(self):
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        bloque = work.add(self.db, title="attend l'amont", assignee="dort")
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")
        self._vieillir()
        # Dépendance ouverte : le lot attend, il n'est pas orphelin.
        self.assertEqual(self._alerts(), [])
        # Même en qa, une dépendance reste bloquante tant qu'elle n'est pas
        # fusionnée (#2252) : toujours pas d'alerte.
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (amont["id"],))
        self.assertEqual(self._alerts(), [])
        # Bloqueur fusionné : l'attente tombe. L'inactivité repart de la levée
        # (#2623) : le lot n'est pas orphelin à la seconde de la fusion.
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (amont["id"],))
        self.assertEqual(self._alerts(), [])
        # Levée vieillie au-delà du seuil : le lot redevient orphelin
        # `sans_tour`, l'agent n'a toujours pas tourné.
        self.db.execute("UPDATE work_item_milestones SET at = now() - interval '2 hours'"
                        " WHERE work_item_id = %s AND kind = 'merged'", (amont["id"],))
        alertes = self._alerts()
        self.assertEqual([(a["lot"], a["reason"]) for a in alertes],
                         [(bloque["id"], "sans_tour")])

    def test_une_dependance_ne_cache_pas_inconnu_ni_arrete(self):
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        bloque = work.add(self.db, title="bloqué", assignee="dort")
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")
        self._vieillir()
        # Agent arrêté : `arrete` est vrai malgré la dépendance.
        registry.set_status(self.db, "dort", "stopped", status_text="arrêté",
                            stop_reason="manuel")
        alertes = self._alerts()
        self.assertEqual([(a["lot"], a["reason"]) for a in alertes],
                         [(bloque["id"], "arrete")])
        # Agent sorti du registre : `inconnu` aussi.
        self.db.execute("DELETE FROM agent_registry WHERE name = 'dort'")
        alertes = self._alerts()
        self.assertEqual([(a["lot"], a["reason"]) for a in alertes],
                         [(bloque["id"], "inconnu")])


if __name__ == "__main__":
    unittest.main()
