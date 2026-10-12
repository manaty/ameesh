# SPDX-License-Identifier: AGPL-3.0-only
"""L148 (stage 2b) : les scripts de dev parlent anglais.

Stage 2b of the L148a plan (`docs/design/l148-plan.md`, §4) renames the
French identifiers and file names of the four dev scripts:
`scripts/carte.py` -> `scripts/map.py`, `scripts/carte_mesure.py` ->
`scripts/map_measure.py`, `scripts/ci-flux.py` -> `scripts/ci-flow.py`
(`scripts/test-parts.py` keeps its already-English name). The subcommands,
options and positionals become English too; every caller (workflows, tests,
`test-plan.py`, `test-parallele.sh`, docs) is updated in the same change.

`scripts/carte_mesure.py` stays as a compatibility alias of `map_measure.py`
for the `ameesh_carte.pth` files installed before the rename; it is deleted
at stage 7 with the other shims.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


map_mod = _load("map_l148_stage2b", "map.py")
flow = _load("ci_flow_l148_stage2b", "ci-flow.py")


class FileNamesTest(unittest.TestCase):

    def test_canonical_names_exist(self):
        for name in ("map.py", "map_measure.py", "ci-flow.py", "test-parts.py"):
            with self.subTest(name=name):
                self.assertTrue((SCRIPTS / name).is_file())

    def test_old_cli_paths_are_gone(self):
        for name in ("carte.py", "ci-flux.py"):
            with self.subTest(name=name):
                self.assertFalse((SCRIPTS / name).exists())

    def test_measurement_shim_keeps_old_pth_working(self):
        shim = _load("carte_mesure_l148_stage2b", "carte_mesure.py")
        import map_measure  # the module the shim imports, same object
        self.assertIs(shim.Logbook, map_measure.Logbook)
        self.assertIs(shim.Releve, map_measure.Logbook)
        self.assertIs(shim.repository_path, map_measure.repository_path)
        self.assertIs(shim.chemin_du_depot, map_measure.repository_path)
        self.assertIs(shim.start, map_measure.start)
        self.assertIs(shim.demarrer, map_measure.start)
        self.assertEqual(shim.ENV_DOSSIER, map_measure.ENV_MEASURE_DIR)
        self.assertEqual(shim.ENV_CONTEXTE, map_measure.ENV_MEASURE_CONTEXT)


class CommandLineTest(unittest.TestCase):

    def run_script(self, name: str, *args: str):
        return subprocess.run([sys.executable, str(SCRIPTS / name), *args],
                              capture_output=True, text=True, cwd=REPO)

    def test_map_speaks_english(self):
        done = self.run_script("map.py", "--help")
        self.assertEqual(done.returncode, 0)
        for word in ("install", "merge", "show", "blocks"):
            self.assertIn(word, done.stdout)
        self.assertNotIn("installer", done.stdout)
        self.assertNotIn("fusionner", done.stdout)

    def test_map_blocks_runs(self):
        done = self.run_script("map.py", "blocks", "scripts/map.py")
        self.assertEqual(done.returncode, 0)
        self.assertIn("<module>", json.loads(done.stdout))

    def test_test_parts_speaks_english(self):
        done = self.run_script("test-parts.py", "--help")
        self.assertEqual(done.returncode, 0)
        for word in ("split", "run", "durations"):
            self.assertIn(word, done.stdout)
        self.assertNotIn("lancer", done.stdout)
        done = self.run_script("test-parts.py", "run", "--help")
        for word in ("--driver", "--list", "--since", "--flow", "--measure",
                     "--output-durations", "--map"):
            self.assertIn(word, done.stdout)

    def test_ci_flow_speaks_english(self):
        done = self.run_script("ci-flow.py", "--help")
        self.assertEqual(done.returncode, 0)
        for word in ("summary", "publish", "close"):
            self.assertIn(word, done.stdout)
        for word in ("publier", "fermer"):
            self.assertNotIn(word, done.stdout)

    def test_old_subcommand_is_rejected(self):
        done = self.run_script("ci-flow.py", "publier")
        self.assertEqual(done.returncode, 2)


class CallersTest(unittest.TestCase):

    def test_workflows_call_the_new_paths(self):
        for name in ("carte.yml", "tests.yml"):
            text = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertNotIn("scripts/carte.py", text)
                self.assertNotIn("scripts/carte_mesure.py", text)
                self.assertNotIn("scripts/ci-flux.py", text)
                self.assertNotIn("test-parts.py lancer", text)
                self.assertNotIn("test-parts.py durees", text)
        carte = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/map.py install", carte)
        self.assertIn("scripts/map.py merge", carte)

    def test_flow_module_keeps_the_json_contract(self):
        # The stored keys are out of stage 2b (§2.7): the renamed functions
        # still read and write the documented French keys.
        self.assertEqual(flow.CONCLUSIONS["echec"], "failure")
        out = flow.check_output([{"v": 1, "type": "test", "test": "tests.t.T.t",
                                  "statut": "echec", "duree_s": 1.0}])
        self.assertIn("1 échec(s)", out["title"])
        self.assertIn('"statut": "echec"', out["text"])


if __name__ == "__main__":
    unittest.main()
