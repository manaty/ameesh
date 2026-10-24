# SPDX-License-Identifier: AGPL-3.0-only
"""#3343 — la carte de mesure repart et l'assemblage a la marge du plafond.

#2116 avait suspendu les mesures automatiques de `carte.yml` en attendant la
correction de #1632 ; la réactivation n'a jamais été faite. Depuis, la carte
du 17/10 (996f7180) vieillissait : le plan de chaque CI de lot retombait sur la
suite complète (`map … old: not used`), en 3 parts psql, et une part dépassait
le plafond de 60 min (run 40089991398, psql 1/3 annulée à 61 min).

Ce module tient les déclencheurs retenus par #3343 (mesure quotidienne à
03:30Z, PR qui touche la mesure, dispatch manuel), l'absence de poussée sur
main (décision pilote du 24/10 : dix jobs de mesure contre la ci-full du paquet
suivant) et le plafond du job d'assemblage, porté à la borne de 60 de #755 :
le 19/10 (run 39223265159), l'assemblage tournait encore à 30 min alors que les
dix parts étaient vertes.

Sans base : lecture du workflow.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")

#: les fichiers dont la mesure dépend : une PR qui les touche doit produire
#: une carte de contrôle (artefact seulement, pas de cache).
PATHS_MESURE = (
    "scripts/map.py",
    "scripts/map_measure.py",
    "scripts/test-parts.py",
    "tests/support.py",
    ".github/workflows/carte.yml",
)


def _on_block() -> str:
    """Le bloc `on:` du workflow (jusqu'à la clé de premier niveau suivante)."""
    m = re.search(r"(?ms)^on:\n(.*?)(?=^[a-z])", WORKFLOW)
    if m is None:
        raise AssertionError("bloc on: introuvable dans carte.yml")
    return m.group(1)


class ReactivationTest(unittest.TestCase):

    def test_pas_de_poussee_sur_main(self):
        # Décision pilote (24/10) : chaque fusion lancerait dix jobs de mesure
        # pendant 40 à 60 min contre la ci-full du paquet suivant (contention
        # 13:07Z). La carte est rafraîchie par un run quotidien à 03:30Z.
        block = _on_block()
        self.assertNotRegex(block, r"(?m)^  push:")
        self.assertRegex(block, r'(?m)^  schedule:\n    - cron: "30 3 \* \* \*"$')
        self.assertIn("  workflow_dispatch:", block)

    def test_une_pr_qui_touche_la_mesure_est_mesuree(self):
        # même liste qu'avant #2116 : la PR produit une carte de contrôle.
        block = _on_block()
        self.assertRegex(block, r"(?m)^  pull_request:\n    paths:\n")
        for path in PATHS_MESURE:
            with self.subTest(path=path):
                self.assertIn(f"      - {path}", block)

    def test_le_dispatch_manuel_reste_disponible(self):
        self.assertIn("  workflow_dispatch:", _on_block())


class AssemblageSousPlafondTest(unittest.TestCase):

    def test_le_job_carte_a_la_marge_des_60_minutes(self):
        # #3343 : le 19/10, l'assemblage (15m30 le 17/10) était encore en
        # cours à 30 min et a été annulé ; #755 borne un job à 60 min.
        m = re.search(r"(?ms)^  carte:\n(.*?)(?=^  [a-z][a-z0-9-]*:|\Z)", WORKFLOW)
        self.assertIsNotNone(m, "job carte introuvable")
        timeout = re.search(r"(?m)^    timeout-minutes:\s*(\d+)$", m.group(1))
        self.assertIsNotNone(timeout, "timeout-minutes du job carte introuvable")
        self.assertEqual(int(timeout.group(1)), 60)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
