# SPDX-License-Identifier: AGPL-3.0-only
"""#4574 — la garde des littéraux client fait partie du contrat d'un lot.

`test_4259_client_literals` lit `src/ameesh` comme du texte (`os.walk`,
`open`) et n'exécute aucun code du paquet : la carte mesurée test → code
(L161f) ne la relie à aucun fichier, et la sélection d'un lot (impactés +
contrat) ne la choisissait jamais. Le 29/10, le paquet h-06 (#4548) a été
recomposé deux fois pour elle, en ci-full seulement (#4425, puis #4513).
Comme `test_2449_garde_mecanique` et `test_2457_garde_non_python`, elle est
dans la liste « toujours », que la sélection d'un lot et la prévalidation
(`prevalidation.contracts`) exécutent sur chaque branche de lot.

Même angle mort pour `test_l161f_plan`, qui fige le contenu de
`tests/parts/toujours.txt` : #4256 y ajoutait un module, la sélection de son
lot n'a pas lancé ce test, et h-06 a été recomposé une fois de plus
(ci-full 40883252255). Il devient un contrat transverse de `tests/parts/`.

Témoins : la liste lue par le sélecteur lui-même contient la garde ; un
changement sous `tests/parts/` rend `test_l161f_plan` dû.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

from ameesh import transverse_contracts

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_plan():
    spec = importlib.util.spec_from_file_location(
        "test_plan_4574", REPO / "scripts" / "test-plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GardeLitterauxContratTest(unittest.TestCase):

    def test_la_garde_des_litteraux_client_est_dans_la_liste_toujours(self):
        plan = _load_plan()
        connus = sorted(p.stem for p in (REPO / "tests").glob("test*.py"))
        toujours = plan.read_list(plan.ALWAYS_FILE, connus, required=True)
        self.assertIn("test_4259_client_literals", toujours)

    def test_un_changement_des_listes_de_parts_rend_le_plan_du(self):
        for path in ("tests/parts/toujours.txt", "tests/parts/pilote-psql.txt"):
            with self.subTest(path=path):
                dus = transverse_contracts.hits([("M", path)])
                self.assertEqual(dus.get("test_l161f_plan"), path)
        self.assertNotIn("test_l161f_plan",
                         transverse_contracts.hits([("M", "tests/test_cluster.py")]))


if __name__ == "__main__":
    unittest.main()
