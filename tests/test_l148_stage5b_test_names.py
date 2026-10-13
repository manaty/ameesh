# SPDX-License-Identifier: AGPL-3.0-only
"""L148 (stage 5b, batch 1) : les fichiers de test portent un nom anglais.

Stage 5b of the L148a plan (`docs/design/l148-plan.md`, §4 and §5) renames
the French **file** names of the test suite; the `test_<sentence>` names
inside stay French for now (mesh-design decision: option (b) now, option (a)
once the open-PR queue has drained).

Batch 1 covers the 17 modules no open PR touches or references. Five
modules keep their French name until the PRs that reference them merge
(batch 2):
`test_l125_courrier_regroupe` (tests/support.py), `test_l130_demandes_sans_lot`
and `test_l165_prise_lots` (tests/parts/pilote-psql.txt),
`test_l39_adoption_reprise` (tests/test_l63_platform.py) and
`test_l43_perimetre_hote` (tests/test_l159a_capacity_guard.py,
tests/test_l112_host_gate.py).

`tests/parts/durees.json` is keyed by module name: a renamed module whose key
is not renamed would silently fall back to the test-count estimate. The keys
are checked here against the files that exist.
"""
from __future__ import annotations

import json
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

#: French name -> English name, batch 1 (2026-10-12)
RENAMES = {
    "test_courrier_consigne": "test_mail_instruction",
    "test_fil": "test_thread_log",
    "test_l106_coupure_hote": "test_l106_host_cutoff",
    "test_l160c_radar_courrier": "test_l160c_radar_mail",
    "test_l161b_flux": "test_l161b_flow",
    "test_l161c_echecs_temps_reel": "test_l161c_failures_realtime",
    "test_l161e_carte": "test_l161e_map",
    "test_l166_session_fille": "test_l166_child_session",
    "test_l219_menage_tmp": "test_l219_housekeeping_tmp",
    "test_l50_group_orphan_lots": "test_l50_group_orphan_work_items",
    "test_l41_liaisons": "test_l41_bindings",
    "test_l48_echecs_rapides": "test_l48_fast_failures",
    "test_l62_projets": "test_l62_projects",
    "test_l73_menage": "test_l73_housekeeping",
    "test_l94_sous_utilisation": "test_l94_underuse",
    "test_l96_feuille_de_route": "test_l96_roadmap",
    "test_suivi_lots_fusionnes": "test_merged_work_item_tracking",
}


class FileNamesTest(unittest.TestCase):

    def test_english_names_exist(self):
        for new in RENAMES.values():
            with self.subTest(name=new):
                self.assertTrue((TESTS / (new + ".py")).is_file())

    def test_french_names_are_gone(self):
        for old in RENAMES:
            with self.subTest(name=old):
                self.assertFalse((TESTS / (old + ".py")).exists())

    def test_no_module_is_renamed_twice(self):
        self.assertEqual(len(set(RENAMES.values())), len(RENAMES))

    def test_durees_keys_name_existing_modules(self):
        table = json.loads((TESTS / "parts" / "durees.json").read_text())
        for pilote in ("psql", "psycopg"):
            for module in table[pilote]:
                with self.subTest(pilote=pilote, module=module):
                    self.assertTrue((TESTS / (module + ".py")).is_file())

    def test_durees_uses_the_new_keys(self):
        table = json.loads((TESTS / "parts" / "durees.json").read_text())
        for old, new in RENAMES.items():
            for pilote in ("psql", "psycopg"):
                if old in table[pilote] or new in table[pilote]:
                    with self.subTest(pilote=pilote, module=new):
                        self.assertIn(new, table[pilote])
                        self.assertNotIn(old, table[pilote])


if __name__ == "__main__":
    unittest.main()
