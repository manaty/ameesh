# SPDX-License-Identifier: AGPL-3.0-only
"""#7471 — `orphan_lot` se tait devant un réveil programmé à venir.

Témoin #7257 : un lot `intake` assigné, sans tour depuis le seuil, portait un
réveil au 19/11 09:00Z ; l'alerte le signalait `sans_tour` alors que le
rendez-vous est voulu et daté. Même lecture que le débit (#6423) et
`planned_waits` : rien n'est actionnable avant l'échéance. Le réveil échu ne
l'excuse plus. Les raisons qui ne tiennent pas à un tour (`arrete`, `inconnu`)
restent vraies sous un réveil, comme sous une dépendance (#2526) : sans
exécuteur vivant, il ne serait pas délivré.
"""
from __future__ import annotations

import time
import unittest

from ameesh import exploitation, registry, work

from .support import PgTestCase


class OrphanLotWakeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("dort", "claude", cwd=self.tmp)
        self.lot = work.add(self.db, title="contrôle au 19/11", assignee="dort")

    def _alerts(self):
        return [(a["lot"], a["reason"])
                for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "orphan_lot"]

    def _vieillir(self, hours=2):
        for sql in (
            "UPDATE work_items SET created_at = now() - interval '%d hours',"
            " updated_at = now() - interval '%d hours'" % (hours, hours),
            "UPDATE work_item_events SET created_at = now() - interval '%d hours'" % hours,
            "UPDATE work_item_milestones SET at = now() - interval '%d hours'" % hours,
        ):
            self.db.execute(sql)

    def _wake(self, at):
        work.schedule_wake(self.db, self.lot["id"], at, text="contrôle", actor="dort")

    def test_reveil_futur_ne_signale_pas_sans_tour(self):
        self._wake(time.time() + 3600)
        self._vieillir()
        self.assertEqual(self._alerts(), [])
        # Échéance passée : le rendez-vous n'excuse plus l'absence de tour.
        self._wake(time.time() - 60)
        self._vieillir()
        self.assertEqual(self._alerts(), [(self.lot["id"], "sans_tour")])

    def test_reveil_futur_ne_cache_pas_un_agent_arrete(self):
        self._wake(time.time() + 3600)
        self._vieillir()
        registry.set_status(self.db, "dort", "stopped", status_text="arrêté",
                            stop_reason="manuel")
        self.assertEqual(self._alerts(), [(self.lot["id"], "arrete")])


if __name__ == "__main__":
    unittest.main()
