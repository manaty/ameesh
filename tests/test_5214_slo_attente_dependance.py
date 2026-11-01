# SPDX-License-Identifier: AGPL-3.0-only
"""#5214 — le SLO « review GO → merge » ne fait pas payer l'attente d'une
dépendance bloquante.

Constat (#5169 sur #5162, 01/11 01:33Z) : #5169 est validé mais sa dépendance
bloquante #5162 est encore dans le paquet en vol. Son horloge GO → fusion
courrait depuis le GO ; à la fusion de #5162 (01:32:36Z), 2 h 44 plus tard,
elle a levé un niveau 3 vers le bureau et l'auditeur pour un lot qui ne
pouvait entrer dans aucun paquet avant.

Attendu : tant qu'une dépendance bloquante est ouverte, aucune ligne
`verdict_merge` ne court ; une fois la dépendance fusionnée ou close,
l'horloge part de cette levée si elle suit le GO. Les arêtes non bloquantes
ne retiennent rien. Une fusion déjà faite garde sa durée réelle.
"""
from __future__ import annotations

import time
import unittest

from ameesh import planned_waits, slo, storage, work

from .test_789_slo_alerts import _Base

#: Un lundi arbitraire, en secondes epoch.
T0 = 1_793_491_200.0
GO = 600.0                      # le verdict OK, dix minutes après le gel
DEP_MERGE = 2 * 3600.0          # la dépendance fusionne deux heures après
NOW = DEP_MERGE + 81.0          # 81 s après : le lot peut enfin fusionner


def _item(state: str = "qa") -> dict:
    return {"id": 5169, "state": state, "priority": 2,
            "assignee": "mty-amh-impl-05",
            "created_ts": T0 - 3600, "updated_ts": T0 + NOW}


def _jalons(*, merged=None) -> dict:
    return {"requested": T0 - 3600, "verdict": T0 + GO, "verdict_kind": "ok",
            "verdict_current": True, "merged": merged}


def _edge(blocker: int = 5162, *, blocking: bool = True, satisfied=None) -> dict:
    # Forme réelle de `work_dependencies.for_items` : une dépendance levée
    # n'est plus bloquante (`blocking` faux) et porte la date de sa levée.
    return {"blocker_id": blocker, "blocking": blocking,
            "satisfied_ts": None if satisfied is None else T0 + satisfied}


def _rows(*, dependencies=(), now=T0 + NOW, state="qa", merged=None, **kw) -> list:
    return slo.lot_steps(_item(state), _jalons(merged=merged), [],
                         now=now, dependencies=dependencies, **kw)


def _step(rows, key):
    return [row for row in rows if row["step"] == key]


class DependanceBloquanteTest(unittest.TestCase):

    def test_une_dependance_ouverte_ne_lance_pas_l_horloge(self):
        # #5169 avant la fusion de #5162 : l'alerte n'est pas imputable au
        # porteur, aucune ligne GO → fusion ne court.
        rows = _rows(dependencies=[_edge()])
        self.assertEqual(_step(rows, "verdict_merge"), [])
        self.assertEqual(slo.breaches(rows), [])

    def test_l_horloge_part_de_la_fusion_de_la_dependance(self):
        # Même lot, dépendance levée 2 h après le GO : 81 s plus tard, il est
        # dans les délais. Sans les arêtes, le même instant levait un niveau 3
        # (2 h 0x depuis le GO) — c'était le défaut de #5169.
        rows = _rows(dependencies=[_edge(blocking=False, satisfied=DEP_MERGE)])
        row = _step(rows, "verdict_merge")[0]
        self.assertEqual(row["started_ts"], T0 + DEP_MERGE)
        self.assertEqual(row["dependency_merged_ts"], T0 + DEP_MERGE)
        self.assertEqual(row["elapsed_s"], 81)
        self.assertEqual(row["level"], 0)
        self.assertEqual(slo.breaches(rows), [])

        # Témoin : les mêmes faits sans l'arête bloquante gardent l'alerte.
        without = _rows()
        old = _step(without, "verdict_merge")[0]
        self.assertEqual(old["started_ts"], T0 + GO)
        self.assertEqual(old["level"], 3)
        self.assertEqual([r["step"] for r in slo.breaches(without)],
                         ["verdict_merge"])

    def test_une_dependance_levee_avant_le_go_ne_change_rien(self):
        rows = _rows(dependencies=[_edge(blocking=False, satisfied=GO - 60)])
        row = _step(rows, "verdict_merge")[0]
        self.assertEqual(row["started_ts"], T0 + GO)
        self.assertNotIn("dependency_merged_ts", row)

    def test_une_dependance_non_bloquante_ne_retient_rien(self):
        rows = _rows(dependencies=[_edge(blocking=False)])
        row = _step(rows, "verdict_merge")[0]
        self.assertEqual(row["started_ts"], T0 + GO)
        self.assertNotIn("dependency_merged_ts", row)

    def test_une_dependance_ouverte_retient_aussi_hors_paquet(self):
        # L'exception « hors paquet » ne rend pas la fusion possible : la
        # dépendance bloquante reste la première attente, même avec une CI
        # verte sur la tête gelée.
        rows = _rows(dependencies=[_edge()], outside_packet=True,
                     ci_green_ts=T0 + 120)
        self.assertEqual(_step(rows, "verdict_merge"), [])

    def test_une_fusion_deja_faite_garde_sa_duree_reelle(self):
        merged = T0 + DEP_MERGE + 81.0
        rows = _rows(dependencies=[_edge(blocking=False, satisfied=DEP_MERGE)],
                     merged=merged, now=merged + 600)
        row = _step(rows, "verdict_merge")[0]
        self.assertEqual(row["done_ts"], merged)
        self.assertEqual(row["elapsed_s"], int(merged - (T0 + GO)))


if __name__ == "__main__":
    unittest.main()


class RejeuHorlogeUniqueTest(_Base):
    """#5214 : la grâce de recomposition (#5206) se juge à la date du rejeu.

    `read` normalise `now` pour tout le calcul d'attente ; l'appartenance de
    paquet doit recevoir la même horloge. Sinon une simulation SLO à date
    imposée juge la grâce à `time.time()` : un rejeu dans le futur voit une
    composition trop ancienne comme fraîche (et inversement), et deux lots du
    même instant ne sont plus jugés au même temps.
    """

    #: la tête de la composition enregistrée (pas encore poussée)
    HEAD = "b" * 40
    #: la tête du porteur que le balayage a déjà vue (composition précédente)
    OLD = "e" * 40

    def setUp(self):
        super().setUp()
        self.store = storage.of(self.db)
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")
        self.freeze = "d" * 40
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (self.freeze, self.lot["id"]))
        self.vehicle = self.packet()
        self.store.work.record_packet(
            int(self.vehicle["id"]), branch="integration/paquet-test",
            base_sha="a" * 40, head_sha=self.HEAD,
            units=[{"work_item_id": int(self.lot["id"]), "unit_sha": "c" * 40,
                    "source_sha": self.freeze, "frozen_sha": self.freeze}],
            note="packet composition recorded", actor="orch")
        self.t0 = time.time()
        # Composition enregistrée 60 s avant t0, tête observée 600 s avant :
        # c'est le cas #5206 d'une recomposition plus récente que le relevé.
        self.db.execute(
            "UPDATE work_packet_members SET recorded_at = to_timestamp(%s)"
            " WHERE package_id = %s", (self.t0 - 60.0, int(self.vehicle["id"])))
        self.db.execute(
            "UPDATE work_items SET branch_head = %s, branch_head_at = to_timestamp(%s)"
            " WHERE id = %s", (self.OLD, self.t0 - 600.0, int(self.vehicle["id"])))

    def waits(self, now):
        row = self.store.work.get(int(self.lot["id"]))
        return planned_waits.read(self.db, [row], now=now, packets=True)

    def test_la_frontiere_des_quinze_minutes_se_lit_a_la_date_du_rejeu(self):
        member = self.store.work.packet_members(
            package_ids=[int(self.vehicle["id"])])[0]
        recorded = float(member["recorded_ts"])
        ident = int(self.lot["id"])
        expected = {"what": "packet", "vehicle": int(self.vehicle["id"]),
                    "branch": "integration/paquet-test", "head": self.OLD}
        grace = planned_waits.RECORD_AHEAD_GRACE_S
        # Rejeu 10 min après l'enregistrement : la composition est fraîche.
        self.assertEqual(self.waits(recorded + 600.0)[ident], expected)
        # Juste avant la borne (15 min - 1 s), elle compte encore.
        self.assertEqual(self.waits(recorded + grace - 1.0)[ident], expected)
        # Juste après (15 min + 1 s), la grâce est passée : plus
        # d'appartenance, donc plus d'attente de paquet. Sans `now=now`,
        # l'horloge du processus (t0, 60 s après la composition) la gardait
        # fraîche et ce cas échouait : le rejeu ne datait pas la frontière.
        after = self.waits(recorded + grace + 1.0)
        self.assertNotIn(ident, after)
