# SPDX-License-Identifier: AGPL-3.0-only
"""#3945 : une part ne prend pas plus que sa part des vCPU de l'hôte.

Le run 40470243300 (main ad042a0c, 27/10) lançait 73 processus de test et
24 Postgres sur 3 hôtes de 16 vCPU (8 runners chacun) : main a été rouge cinq
runs de suite, sur un test de timing différent à chaque fois. Le plafond
`vCPU // AMEESH_CI_JOBS_PER_HOST` borne chaque job, même au-dessus du
`--parallel` demandé ; sans la variable, rien ne change.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

_spec = importlib.util.spec_from_file_location("parts_3945", SCRIPTS / "test-parts.py")
parts = importlib.util.module_from_spec(_spec)
sys.modules["parts_3945"] = parts
_spec.loader.exec_module(parts)


class PlafondTest(unittest.TestCase):

    def _cap(self, valeur, cpus=16):
        env = {k: v for k, v in os.environ.items() if k != parts.JOBS_PER_HOST_ENV}
        if valeur is not None:
            env[parts.JOBS_PER_HOST_ENV] = valeur
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(parts, "host_cpus", return_value=cpus):
            return parts.host_cap()

    def test_seize_vcpu_huit_runners_donnent_deux(self):
        self.assertEqual(self._cap("8"), 2)

    def test_un_hote_plus_large_garde_plus_de_processus(self):
        self.assertEqual(self._cap("8", cpus=32), 4)

    def test_jamais_moins_d_un_processus(self):
        self.assertEqual(self._cap("8", cpus=4), 1)

    def test_sans_variable_pas_de_plafond(self):
        self.assertIsNone(self._cap(None))
        self.assertIsNone(self._cap(""))
        self.assertIsNone(self._cap("0"))
        self.assertIsNone(self._cap("huit"))


class PartPlafonneeTest(unittest.TestCase):
    """De bout en bout : un plafond de 1 ramène `--parallel 2` à la série."""

    def test_le_plafond_passe_avant_le_plancher(self):
        modules = ["test_version", "test_site_art"]
        plan = {
            "schema": "ameesh-test-plan/1",
            "parts": {"psql": [{
                "part": 1, "of": 1, "label": "psql 1/1",
                "modules": modules, "estimate_s": 10.0, "pilot_list": True,
            }]},
            "classes": {m: ["P3", ""] for m in modules},
        }
        with tempfile.TemporaryDirectory() as tmp:
            chemin = pathlib.Path(tmp) / "plan.json"
            chemin.write_text(json.dumps(plan), encoding="utf-8")
            flux = pathlib.Path(tmp) / "flux.jsonl"
            # Plus de runners que de vCPU : le plafond vaut 1.
            env = dict(os.environ, AMEESH_REQUIRE_DB="0", AMEESH_BALANCE_INTERVAL="0",
                       **{parts.JOBS_PER_HOST_ENV: str(10 * (os.cpu_count() or 1))})
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "test-parts.py"), "run",
                 "--plan", str(chemin), "--part", "1", "--driver", "psql",
                 "--flow", str(flux), "--parallel", "2"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
            self.assertIn("ramenés à 1", proc.stderr)
            self.assertIn("exécution série", proc.stderr)


class WorkflowTest(unittest.TestCase):

    def test_les_parts_connaissent_les_runners_de_leur_hote(self):
        texte = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("  AMEESH_CI_JOBS_PER_HOST: ${{", texte)
        self.assertIn("vars.AMEESH_CI_JOBS_PER_HOST || '8'", texte)


if __name__ == "__main__":
    unittest.main()
