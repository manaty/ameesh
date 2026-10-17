# SPDX-License-Identifier: AGPL-3.0-only
"""#2282 : le garde cgroup DeepSeek est un chemin de sécurité (deux avis).

Le garde a deux moitiés : le préchargement Node (``deepseek_cgroup.cjs``), qui
intercepte ``child_process`` dans le processus lancé, et son module Python
(``deepseek_cgroup.py``), qui compose ``NODE_OPTIONS`` et vérifie le cgroup.
Une modification de l'un ou de l'autre touche un contrôle de sécurité : le
canon (règle « droits, identité, clés et secrets ») doit les classer sensibles
et la porte de fusion (``merge_queue.CRITICAL_SCOPE``) doit en compter deux.

Témoin rouge→vert : le premier test suit la chaîne classement du canon →
nombre d'avis ; les suivants couvrent le motif de secours de la porte, y
compris un faux voisin qui ne doit pas hériter des deux avis.
"""
from __future__ import annotations

import types
import unittest

from ameesh import lot_class, merge_queue

#: les deux moitiés du garde cgroup DeepSeek
CGROUP = ("src/ameesh/deepseek_cgroup.cjs",
          "src/ameesh/deepseek_cgroup.py")


def _canon(paths):
    """Un canon minimal qui porte la règle de sécurité attendue pour le garde."""
    return types.SimpleNamespace(
        root="canon", sources=[],
        federation={"review_policies": {"risk_classes": {"projects": {"ameesh": {
            "default": "light",
            "rules": [{"paths": list(paths), "class": "sensitive"}]}}}}})


def _classified(path):
    """Un diff classé sensible par une règle large qui ne cite pas le chemin."""
    return {"class": "sensitive", "doubt": False,
            "files": [{"path": path, "class": "sensitive",
                       "rule": ["src/ameesh/**"]}]}


class CgroupGuardSafetyScopeTest(unittest.TestCase):
    """Le garde cgroup compte deux avis, comme SQL, la sécurité et la prod."""

    def test_le_canon_classe_le_garde_sensible_et_la_porte_compte_deux_avis(self):
        # #2282 : le chemin doit être dans la règle de sécurité du canon
        # (federation.yaml) ET dans CRITICAL_SCOPE ; le premier sans le second
        # ne donne qu'un avis, le second sans le premier aucun (classe light).
        canon = _canon(CGROUP)
        for path in CGROUP:
            with self.subTest(path=path):
                classified = lot_class.class_of_paths([canon], "ameesh", [path])
                self.assertEqual(classified["class"], "sensitive", path)
                self.assertFalse(classified["doubt"], path)
                self.assertEqual(merge_queue.required_verdicts(classified), 2, path)

    def test_le_garde_compte_deux_avis_meme_si_le_canon_ne_cite_pas_le_chemin(self):
        # Le motif de secours de la porte ne dépend pas de la règle du canon :
        # un chemin classé sensible par une règle large reste critique.
        for path in CGROUP:
            with self.subTest(path=path):
                classified = _classified(path)
                self.assertTrue(merge_queue.critical_scope(classified), path)
                self.assertEqual(merge_queue.required_verdicts(classified), 2, path)

    def test_un_faux_voisin_ne_compte_pas(self):
        # Le motif est ancré sur le nom exact : un autre fichier de src/ameesh
        # classé sensible ne devient pas critique par simple préfixe.
        classified = _classified("src/ameesh/deepseek_cgroup_extra.cjs")
        self.assertFalse(merge_queue.critical_scope(classified))
        self.assertEqual(merge_queue.required_verdicts(classified), 1)


if __name__ == "__main__":
    unittest.main()
