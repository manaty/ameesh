# SPDX-License-Identifier: AGPL-3.0-only
"""#8112 — le courrier du jour et les actifs passent devant le stock FIFO.

Constat (auditeur, 23/11 01:12) : `agent_mailbox` lit les 200 plus anciens
non-lus, et `mail.prompt_for` remplit son budget dans cet ordre. Un stock de
passifs anciens retarde donc le courrier du jour lié à un lot et un actif
au-delà de la page : seul le courrier porteur de `meta.consigne` passait
devant (critère 2 de #8040 tenu seulement parce que la file était vide).

Désormais, page pleine, l'exécuteur lit à part le courrier du jour lié à un
lot et le courrier actif (`mail.unread_priority`) et les met devant le stock.
Postgres réel : les témoins passent par `_pick` et `prompt_for`.
"""
from __future__ import annotations

import time
import unittest

from ameesh import mail

from .test_l125_courrier_regroupe import _Base


class DayStartTest(unittest.TestCase):
    def test_minuit_utc(self):
        # 2026-11-23 01:04:05 UTC -> même jour, 00:00:00 UTC
        self.assertEqual(mail.day_start(1795395845.0), 1795392000.0)

    def test_priority_first_ordre_et_doublons(self):
        priorite = [dict(id=3), dict(id=9)]
        page = [dict(id=1), dict(id=3), dict(id=2)]
        self.assertEqual([m["id"] for m in mail.priority_first(priorite, page)],
                         [3, 9, 1, 2])

    def test_priority_order_stable(self):
        maintenant = time.time()

        def vieux(ident):
            return dict(id=ident, created_ts=maintenant - 5 * 86400,
                        payload={"info": True})

        actif = dict(id=5, created_ts=maintenant - 9 * 86400, payload={})
        jour = dict(id=9, created_ts=maintenant, payload={"info": True},
                    work_item_id="8112")
        rows = [vieux(1), actif, jour, vieux(2)]
        self.assertEqual(
            [m["id"] for m in mail.priority_order(rows, mail.day_start())],
            [5, 9, 1, 2])


class UnreadPriorityTest(_Base):
    def _stock(self, recipient: str, nombre: int, *, actif: bool = False) -> None:
        """`nombre` messages de 5 jours, passifs (ou actifs), ids croissants."""
        self.fill(
            "INSERT INTO agent_mailbox"
            " (sender, recipient, body, kind, payload, created_at)"
            " SELECT 'stock', %s, 'vieux compte rendu ' || g, 'notify',"
            "        %s::jsonb, now() - make_interval(days => 5)"
            "   FROM generate_series(1, %s) AS g",
            (recipient, '{}' if actif else '{"info": true}', int(nombre)))

    def test_la_page_pleine_ne_cache_ni_le_jour_ni_l_actif(self):
        _runner, worker = self._worker("orch-p")
        self._stock("orch-p", 300)                    # stock passif, 300 plus anciens
        actif = mail.send(self.db, "dev1", "orch-p", "actif ancien")
        self._vieillir("orch-p", 400)
        jour = mail.send(self.db, "dev2", "orch-p",
                         "avis du jour sur le lot 8112", work_item_id="8112",
                         payload={"info": True})

        rows = mail.unread_priority(self.db, "orch-p", limit=200)
        self.assertEqual([row["id"] for row in rows], [actif, jour])

        spec = worker.pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail")
        # le témoin du lot : le courrier du jour entre dans le premier prompt
        self.assertIn(jour, spec["ids"])
        self.assertIn("avis du jour sur le lot 8112", spec["prompt"])

    def test_un_actif_du_jour_derriere_un_actif_ancien_est_servi(self):
        _runner, worker = self._worker("orch-a")
        self._stock("orch-a", 199)
        mail.send(self.db, "dev1", "orch-a", "actif ancien")  # dans la page FIFO
        self._stock("orch-a", 101)
        self._vieillir("orch-a", 400)
        jour = mail.send(self.db, "dev2", "orch-a", "actif du jour 8112")

        spec = worker.pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail")
        self.assertIn(jour, spec["ids"])
        self.assertIn("actif du jour 8112", spec["prompt"])

    def test_un_passif_du_jour_sans_lot_reste_dans_le_stock(self):
        _runner, worker = self._worker("orch-s")
        self._stock("orch-s", 300)
        mail.send(self.db, "dev1", "orch-s", "actif ancien")
        self._vieillir("orch-s", 400)
        sans_lot = mail.send(self.db, "dev2", "orch-s", "info du jour sans lot",
                             payload={"info": True})

        rows = mail.unread_priority(self.db, "orch-s", limit=200)
        self.assertNotIn(sans_lot, [row["id"] for row in rows])
        self.assertGreaterEqual(time.time(), mail.day_start())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
