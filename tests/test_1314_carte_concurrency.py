# SPDX-License-Identifier: AGPL-3.0-only
"""#1314 — la carte d'impact de main n'est jamais annulée par la poussée suivante.

Règle du bureau (14/10/2026, complément #1314) : le workflow `carte` annulait
chaque mesure de main à la poussée suivante (`cancel-in-progress: true`), si
bien qu'avec une fusion toutes les quelques minutes presque aucune carte n'était
publiée (runs 38558057012, 38560201871, 38570320372 annulés) et que les PR de
lot perdaient la sélection. GitHub garde un run en cours et un seul en attente,
remplacé par le suivant : la carte publiée reste celle de la main la plus
récente. C'est la règle déjà appliquée à tests.yml (#824,
`tests/test_l161a_ci_sans_code.py`).

Sans base : lecture du workflow.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")
GROUP = re.compile(r"^\s*group:\s*(carte-.*)$", re.M)


class CarteConcurrencyTest(unittest.TestCase):

    def test_une_carte_de_main_n_est_pas_annulee(self):
        # la mesure de main ne doit jamais être annulée par la poussée suivante
        settings = [ln.strip() for ln in WORKFLOW.splitlines()
                    if not ln.lstrip().startswith("#")
                    and ln.strip().startswith("cancel-in-progress:")]
        self.assertEqual(
            settings, ["cancel-in-progress: ${{ github.event_name == 'pull_request' }}"])

    def test_le_groupe_de_main_est_stable(self):
        # même groupe pour toutes les poussées de main : le run en cours
        # n'est pas remplacé, le suivant attend (un seul en attente, tests.yml).
        # Le dispatch, lui, est isolé par une identité par run (#1316) : voir
        # tests/test_1316_carte_dispatch.py.
        m = GROUP.search(WORKFLOW)
        self.assertIsNotNone(m, "groupe de concurrence introuvable")
        group = m.group(1)
        self.assertIn("|| 'main'", group)

    def test_une_pr_garde_son_groupe_annulable(self):
        # une PR qui touche la mesure garde son groupe : une mesure dépassée
        # n'a plus d'objet.
        m = GROUP.search(WORKFLOW)
        self.assertIsNotNone(m, "groupe de concurrence introuvable")
        self.assertIn("format('pr-{0}', github.event.pull_request.number)", m.group(1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
