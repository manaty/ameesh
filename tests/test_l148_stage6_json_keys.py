# SPDX-License-Identifier: AGPL-3.0-only
"""L148 (stage 6, scripts) : les clés JSON des scripts parlent anglais.

Stage 6 of the L148a plan (`docs/design/l148-plan.md`, §4) emits the English
key **beside** the French one for one release: `durations` beside `durees`
and `measure` beside `mesure` in the test → code card (`scripts/map.py`),
read back by `scripts/test-parts.py --map` and `scripts/test-plan.py`.
`_Result.flux` becomes `_Result.flow` (the `--flow` option already exists).
The French keys leave at stage 7, with the next major version.
"""
from __future__ import annotations

import gzip
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


map_mod = _load("map_l148_stage6", "map.py")
parts = _load("test_parts_l148_stage6", "test-parts.py")
plan = _load("test_plan_l148_stage6", "test-plan.py")


def _card(modules: dict, mesure_key: str = "measure") -> dict:
    """Une carte minimale, lisible par `summary` et `map_durations`."""
    return {
        "schema": "ameesh-carte/1",
        "commit": "abc",
        "genere": "2026-10-13T04:00:00+00:00",
        "outils": {"python": "3.12", "coverage": "7"},
        mesure_key: {"parts": 1, "processus": 2, "processus_sans_contexte": 0,
                     "sous_processus_sans_mesure": {}, "surcout": {}},
        "modules": modules,
        "blocs": {}, "donnees": {}, "dossiers": {}, "tables": {},
        "migrations": {}, "sans_test": [],
    }


def _write(chemin: pathlib.Path, carte: dict) -> pathlib.Path:
    with gzip.open(chemin, "wt", encoding="utf-8") as f:
        json.dump(carte, f)
    return chemin


class CardKeysTest(unittest.TestCase):

    def test_module_carries_both_spellings(self):
        out = map_mod._module_out({"durations": {"psycopg": 1.0}, "echecs": []})
        self.assertEqual(out["durations"], {"psycopg": 1.0})
        self.assertEqual(out["durees"], {"psycopg": 1.0})
        self.assertEqual(out["echecs"], [])

    def test_summary_reads_the_english_measure(self):
        texte = map_mod.summary(_card({}))
        self.assertIn("2 processus mesurés en 1 parts", texte)

    def test_summary_still_reads_the_old_measure(self):
        texte = map_mod.summary(_card({}, mesure_key="mesure"))
        self.assertIn("2 processus mesurés en 1 parts", texte)

    def test_map_durations_reads_the_english_key(self):
        chemin = _write(pathlib.Path(tempfile.mkdtemp()) / "carte.json.gz",
                        _card({"test_l1": {"durations": {"psycopg": 3.0}, "echecs": []}}))
        self.assertEqual(parts.map_durations(chemin), {"psycopg": {"test_l1": 3.0}})

    def test_map_durations_still_reads_the_old_key(self):
        chemin = _write(pathlib.Path(tempfile.mkdtemp()) / "carte.json.gz",
                        _card({"test_l1": {"durees": {"psycopg": 4.0}, "echecs": []}}))
        self.assertEqual(parts.map_durations(chemin), {"psycopg": {"test_l1": 4.0}})

    def test_map_durations_prefers_the_english_key(self):
        chemin = _write(pathlib.Path(tempfile.mkdtemp()) / "carte.json.gz",
                        _card({"test_l1": {"durations": {"psycopg": 3.0},
                                          "durees": {"psycopg": 9.0}, "echecs": []}}))
        self.assertEqual(parts.map_durations(chemin), {"psycopg": {"test_l1": 3.0}})

    def test_plan_weights_read_the_english_key(self):
        # un module de test réel : `weight` compte ses `def test` pour le repli
        card = _card({"test_l161e_map": {"durations": {"psycopg": 3.0}, "echecs": []}})
        self.assertEqual(plan.weights(["test_l161e_map"], "psycopg", card),
                         {"test_l161e_map": 3.0})


class ResultFlowTest(unittest.TestCase):

    def test_result_class_exposes_flow(self):
        marqueur = object()
        classe = parts._result_class(marqueur, {}, None, None)
        self.assertIs(classe.flow, marqueur)

    def test_result_defaults_to_no_flow(self):
        self.assertIsNone(parts._Result.flow)
