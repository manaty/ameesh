# SPDX-License-Identifier: AGPL-3.0-only
"""#2129 (N3 de la relecture Claude de #1901) : une ligne d'hôte malformée
(`body` non objet) ne fait pas tomber la passe d'alertes.

Avant : `availability.alerts` faisait `row["body"].get(...)` ; un `body` non
objet levait `AttributeError`, hors de l'`except (OSError, ValueError)` de
`exploitation._gate_alerts` — toute la passe d'alertes échouait. Après : la
ligne n'est pas une observation, elle est sautée et nommée, les alertes des
autres hôtes restent rendues, et la famille de la porte est dite non lue
(une ligne non observée ne résout rien, #1901/#1473).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from ameesh import exploitation
from ameesh.mediated_executor import availability


def registre(hosts=None):
    return json.dumps({"schema": availability.REGISTRY_SCHEMA,
                       "hosts": hosts if hosts is not None else {}})


def indisponible(host=None, reason="unknown"):
    ligne = {"body": {"state": "stopped", "reason": reason, "seq": 1,
                      "available": False, "until_ts": None, "caps": {}},
             "since_ts": 10.0}
    return ligne


class LigneMalformeeTest(unittest.TestCase):
    """`alerts` saute la ligne et nomme l'anomalie sans tomber."""

    def test_ligne_malformee_ne_fait_pas_tomber_la_passe(self):
        erreurs = []
        out = availability.alerts(
            [{"host": "h1", "body": "stopped", "since_ts": 0.0},
             dict(indisponible(), host="h2")],
            now=1000.0, held={"h1": ["a"]}, errors=erreurs)
        self.assertEqual([a["host"] for a in out], ["h2"])
        self.assertEqual([a["type"] for a in out], ["host_unavailable"])
        self.assertEqual(len(erreurs), 1)
        self.assertIn("h1", erreurs[0])
        self.assertIn("str", erreurs[0])

    def test_body_absent_reste_ignore_sans_erreur(self):
        erreurs = []
        self.assertEqual(availability.alerts([{"host": "h1", "body": None}],
                                             now=1000.0, errors=erreurs), [])
        self.assertEqual(erreurs, [])

    def test_body_liste_vide_est_malformee(self):
        # `[]` était avalé par `or {}` avant le correctif : une ligne vide
        # n'est pas une observation davantage qu'une ligne pleine.
        erreurs = []
        self.assertEqual(availability.alerts([{"host": "h1", "body": []}],
                                             now=1000.0, errors=erreurs), [])
        self.assertEqual(len(erreurs), 1)
        self.assertIn("list", erreurs[0])

    def test_sans_liste_d_erreurs_la_passe_rend_quand_meme(self):
        out = availability.alerts(
            [{"host": "h1", "body": {"state": "n-importe-quoi"}},
             dict(indisponible(), host="h2")], now=1000.0)
        self.assertEqual([a["host"] for a in out], ["h2"])


class RegistreLocalTest(unittest.TestCase):
    """Le registre de fichier : la famille est dite non lue, pas résolue."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-2129-")
        self.cfg = SimpleNamespace(state_dir=self.dir, host="h1")
        self.path = availability.default_path(self.cfg)

    def ecrire(self, hosts):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(registre(hosts))

    def test_ligne_malformee_nomme_la_famille_et_garde_les_autres(self):
        self.ecrire({"h1": {"body": 42, "since_ts": 0.0},
                     "h2": indisponible()})
        non_lues, erreurs = [], []
        out = availability.local_alerts(self.cfg, [], 1000.0,
                                        unreadable=non_lues, errors=erreurs)
        self.assertEqual([a["host"] for a in out], ["h2"])
        self.assertEqual(non_lues, [availability.GATE_FAMILY])
        self.assertEqual(len(erreurs), 1)
        self.assertIn("h1", erreurs[0])

    def test_gate_alerts_rend_la_porte_non_lue_et_l_autre_alerte(self):
        self.ecrire({"h1": {"body": "stopped"}, "h2": indisponible()})
        non_lues = []
        out = exploitation._gate_alerts(self.cfg, [], 1000.0,
                                        unreadable=non_lues)
        self.assertEqual(sorted(a["type"] for a in out),
                         ["host_gate_unreadable", "host_unavailable"])
        self.assertEqual(non_lues, [availability.GATE_FAMILY])

    def test_registre_sain_n_est_pas_signe(self):
        self.ecrire({"h1": indisponible()})
        non_lues, erreurs = [], []
        out = availability.local_alerts(self.cfg, [], 1000.0,
                                        unreadable=non_lues, errors=erreurs)
        self.assertEqual([a["type"] for a in out], ["host_unavailable"])
        self.assertEqual(non_lues, [])
        self.assertEqual(erreurs, [])


if __name__ == "__main__":
    unittest.main()
