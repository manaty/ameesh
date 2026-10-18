# SPDX-License-Identifier: AGPL-3.0-only
"""#2397 — le découpage de la CI pèse les trous de la carte à son échelle.

`weights()` mélangeait deux sources non comparables : la carte, mesurée sous
couverture (plus lente), et `tests/parts/durees.json`, mesurée sans couverture.
La table statique comble toujours les trous de la carte (#1476) et la carte
reste prioritaire quand elle connaît une durée (L148 stage 6), mais à l'échelle
de la carte : le `surcout` qu'elle relève convertit la table avant de la
mélanger.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = _load("test_plan_2397", "test-plan.py")


def _card(modules: dict, surcout: dict | None = None) -> dict:
    return {
        "schema": "ameesh-carte/1",
        "commit": "abc",
        "genere": "2026-10-18T04:00:00+00:00",
        "outils": {"python": "3.12", "coverage": "7"},
        "measure": {"parts": 1, "processus": 2, "processus_sans_contexte": 0,
                    "sous_processus_sans_mesure": {}, "surcout": surcout or {}},
        "modules": modules,
        "blocs": {}, "donnees": {}, "dossiers": {}, "tables": {},
        "migrations": {}, "sans_test": [],
    }


class WeightsScaleTest(unittest.TestCase):
    """Une table statique courte et connue, substituée à `tests/parts/durees.json`."""

    def setUp(self):
        self._dossier = tempfile.TemporaryDirectory()
        self.chemin = pathlib.Path(self._dossier.name) / "durees.json"
        self.chemin.write_text(json.dumps({
            "_source": "test",
            "psql": {"test_l116_parts": 10.0, "test_l161e_map": 20.0},
            "psycopg": {"test_l116_parts": 5.0, "test_l161e_map": 8.0},
        }), encoding="utf-8")
        self._table = plan.parts.TABLE
        plan.parts.TABLE = self.chemin

    def tearDown(self):
        plan.parts.TABLE = self._table
        self._dossier.cleanup()

    def test_l161e_map_a_l_echelle_de_la_carte(self):
        # carte sous couverture ×2 : le trou vaut 20 × 2, la mesure de la
        # carte (30) garde sa valeur brute.
        card = _card({"test_l116_parts": {"durations": {"psql": 30.0}}},
                     surcout={"psql": 2.0})
        self.assertEqual(plan.weights(["test_l116_parts", "test_l161e_map"], "psql", card),
                         {"test_l116_parts": 30.0, "test_l161e_map": 40.0})

    def test_surcout_par_pilote(self):
        card = _card({"test_l116_parts": {"durations": {"psql": 30.0, "psycopg": 10.0}}},
                     surcout={"psycopg": 1.5})
        self.assertEqual(plan.weights(["test_l116_parts", "test_l161e_map"], "psycopg", card),
                         {"test_l116_parts": 10.0, "test_l161e_map": 12.0})
        # le surcoût de psycopg ne convertit pas la table psql
        self.assertEqual(plan.weights(["test_l116_parts", "test_l161e_map"], "psql", card),
                         {"test_l116_parts": 30.0, "test_l161e_map": 20.0})

    def test_sans_surcout_table_brute(self):
        # carte antérieure à `measure` : comportement d'avant #2397 conservé
        card = _card({"test_l116_parts": {"durations": {"psql": 30.0}}})
        self.assertEqual(plan.weights(["test_l116_parts", "test_l161e_map"], "psql", card),
                         {"test_l116_parts": 30.0, "test_l161e_map": 20.0})

    def test_surcout_nul_ignore(self):
        card = _card({"test_l116_parts": {"durations": {"psql": 30.0}}},
                     surcout={"psql": 0})
        self.assertEqual(plan.weights(["test_l116_parts", "test_l161e_map"], "psql", card),
                         {"test_l116_parts": 30.0, "test_l161e_map": 20.0})

    def test_sans_carte_table_brute(self):
        self.assertEqual(plan.weights(["test_l116_parts", "test_l161e_map"], "psql", None),
                         {"test_l116_parts": 10.0, "test_l161e_map": 20.0})


if __name__ == "__main__":
    unittest.main()
