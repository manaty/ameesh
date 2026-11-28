# SPDX-License-Identifier: AGPL-3.0-only
"""#8291 — le nocturne psql dimensionne ses parts sur le plafond du job.

Les 25 et 26/11, une part coupée à 60 min par nuit (7/16 puis 10/16, run
45738064364) : la table `psql_full` du 08/11 annonçait 16 parts égales
(~12 050 s de tests) que le run a mesurées de 2 799 à 12 113 s. Comme la
carte depuis #8261, 16 n'est plus qu'un plancher : le plan relève le nombre
de parts tant que la plus lourde ne tient pas sous la cible, et le signale
quand un module seul la dépasse. Témoins sans base : le plan et tests.yml.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import canon

REPO = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 11, 26, 2, 0, tzinfo=datetime.timezone.utc)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = _load("test_plan_8291", "test-plan.py")
parts = _load("test_parts_8291", "test-parts.py")
POPEN = subprocess.Popen  # le vrai, avant que le témoin ne le remplace


class NocturnePartsTest(unittest.TestCase):
    def setUp(self):
        self.mods = plan.parts.modules()
        self.cible = plan.psql_full_target_s()
        self.state = {
            "map": None, "sure": True, "phase2_safe": True, "lot_safe": True,
            "phase2_why": "", "unsure": [], "untested": [], "unknown_blocks": [],
            "foundation": [], "changed": 1, "always": [], "contract": [],
            "classes": {m: ["P3", ""] for m in self.mods}}

    def _table(self, poids: dict[str, float]):
        table = {"_generated": "2026-11-26T00:00:00Z", "psql": {}, "psql_full": poids}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chemin = pathlib.Path(tmp.name) / "durees.json"
        chemin.write_text(json.dumps(table), encoding="utf-8")
        patch = mock.patch.object(plan.parts, "TABLE", chemin)
        patch.start()
        self.addCleanup(patch.stop)

    def _plan(self):
        nature = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "classify", return_value=dict(self.state)), \
                mock.patch.object(plan, "nature", return_value=nature):
            return plan.make_plan("workflow_dispatch", {}, NOW, contract_policy=None,
                                  psql_full=True)

    def _coherent(self, p):
        nuit = p["parts"]["psql"]
        n = len(nuit)
        self.assertEqual([e["part"] for e in nuit], list(range(1, n + 1)))
        self.assertEqual({e["of"] for e in nuit}, {n})
        self.assertEqual(sorted(m for e in nuit for m in e["modules"]), self.mods)
        self.assertEqual(plan.outputs(p)["psql_parts"], str(n))
        self.assertEqual(json.loads(plan.outputs(p)["psql"]), list(range(1, n + 1)))
        return n, max(e["estimate_s"] for e in nuit)

    def test_les_parts_sont_relevees_sous_la_cible(self):
        # le 26/11 : 16 parts de ~12 050 s, au-delà de la cible
        total = 16 * 11_000.0
        self._table({m: total / len(self.mods) for m in self.mods})
        p = self._plan()
        n, lourde = self._coherent(p)
        self.assertGreater(n, plan.PSQL_FULL_PARTS_MIN)
        self.assertLessEqual(n, plan.PSQL_FULL_PARTS_MAX)
        self.assertLessEqual(lourde, self.cible)
        self.assertIn(f"psql night parts (#8291): {n} (floor 16)", plan.summary(p))
        self.assertFalse([a for a in plan.annotations(p) if "#8291" in a])

    def test_le_repli_releve_les_memes_parts(self):
        total = 16 * 11_000.0
        self._table({m: total / len(self.mods) for m in self.mods})
        nominal, _ = self._coherent(self._plan())
        repli = plan.fallback_plan("workflow_dispatch", "boom", psql_full=True)
        n, lourde = self._coherent(repli)
        self.assertEqual(n, nominal)
        self.assertLessEqual(lourde, self.cible)

    def test_une_suite_legere_garde_le_plancher(self):
        self._table({m: 1.0 for m in self.mods})
        n, _ = self._coherent(self._plan())
        self.assertEqual(n, plan.PSQL_FULL_PARTS_MIN)

    def test_un_module_insecable_est_signale_sans_multiplier_les_parts(self):
        lent = self.mods[0]
        poids = {m: 1.0 for m in self.mods}
        poids[lent] = self.cible * 1.2
        self._table(poids)
        p = self._plan()
        n, lourde = self._coherent(p)
        self.assertEqual(n, plan.PSQL_FULL_PARTS_MIN)
        self.assertGreater(lourde, self.cible)
        alertes = [a for a in plan.annotations(p) if "#8291" in a]
        self.assertEqual(len(alertes), 1, alertes)
        self.assertTrue(alertes[0].startswith("::warning "), alertes[0])

    def test_la_table_mesuree_tient_sous_la_cible(self):
        """La table livrée (run 45738064364) : aucune part au-delà de la cible."""
        _, lourde = self._coherent(self._plan())
        self.assertLessEqual(lourde, self.cible)

    def test_le_plafond_est_celui_du_job_psql(self):
        doc = canon.load_yaml((REPO / ".github" / "workflows" / "tests.yml")
                              .read_text(encoding="utf-8"))
        job = doc["jobs"]["psql"]
        self.assertEqual(job["timeout-minutes"], plan.PSQL_FULL_PLAFOND_MIN)
        self.assertEqual(job["env"]["AMEESH_PLAFOND_MIN"], str(plan.PSQL_FULL_PLAFOND_MIN))



class EcheanceTest(unittest.TestCase):
    """La part parallèle s'arrête à son échéance, non concluante, avant que
    le plafond du job ne l'annule."""

    MODULES = ["test_940_agregat_bash", "test_8291_nocturne_parts"]

    def _bloquant(self, *_a, **kw):
        return POPEN([sys.executable, "-c", "import time; time.sleep(60)"],
                                cwd=kw.get("cwd"))

    def test_attendre_sans_echeance_attend_la_fin(self):
        procs = [subprocess.Popen([sys.executable, "-c", "pass"])]
        self.assertFalse(parts.attendre(procs, None, pas_s=0.05))
        self.assertEqual(procs[0].returncode, 0)

    def test_attendre_arrete_a_l_echeance(self):
        procs = [self._bloquant()]
        debut = time.monotonic()
        self.assertTrue(parts.attendre(procs, time.time() + 0.3, pas_s=0.05))
        procs[0].wait(timeout=10)
        self.assertLess(time.monotonic() - debut, 10)

    SOURD = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "time.sleep(60)")

    def _sourd(self, *_a, **kw):
        return POPEN([sys.executable, "-c", self.SOURD], cwd=kw.get("cwd"))

    def test_un_enfant_sourd_a_sigterm_est_tue_apres_la_grace(self):
        """Relecture ameesh-cx2 (B/P2) : un enfant qui ignore SIGTERM ne
        retient plus la part jusqu'au plafond du job."""
        procs = [self._sourd()]
        debut = time.monotonic()
        self.assertTrue(parts.attendre(procs, time.time() + 1.0, pas_s=0.05, grace_s=0.5))
        self.assertLess(time.monotonic() - debut, 10)
        self.assertEqual(procs[0].returncode, -signal.SIGKILL)

    def test_la_part_echue_avec_un_enfant_sourd_finit_non_concluante(self):
        with mock.patch.object(parts, "GRACE_ECHEANCE_S", 0.5):
            code, fin, _ = self._couper(echeance=time.time() + 1.5, popen=self._sourd)
        self.assertEqual(code, 1)
        self.assertEqual(fin["raison"], "echeance")

    def _couper(self, echeance=None, signal_apres=None, popen=None):
        """Joue `run_parallel` sur deux processus qui ne finissent jamais :
        coupés à l'`echeance`, ou par un SIGTERM (le plafond du job)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chemin = pathlib.Path(tmp.name) / "plan.json"
        chemin.write_text(json.dumps({"parts": {"psql": [
            {"part": 1, "of": 1, "label": "psql 1/1", "modules": self.MODULES}]},
            "classes": {}}), encoding="utf-8")
        flux = pathlib.Path(tmp.name) / "flux.jsonl"
        args = argparse.Namespace(
            plan=str(chemin), measure=None, driver="psql", part=1, parts=1,
            label=None, parallel=2, flow=str(flux), output_durations=None,
            echeance=echeance)
        env = {k: v for k, v in os.environ.items()
               if k not in ("AMEESH_TEST_DSN", "AGENT_MESH_TEST_DSN", parts.PARALLEL_ENV)}
        sortie = io.StringIO()
        self.addCleanup(os.chdir, os.getcwd())
        minuterie = (threading.Timer(signal_apres, os.kill, (os.getpid(), signal.SIGTERM))
                     if signal_apres else None)
        debut = time.monotonic()
        code = None
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(parts.subprocess, "Popen",
                                  side_effect=popen or self._bloquant), \
                contextlib.redirect_stdout(sortie):
            if minuterie:
                minuterie.start()
            try:
                code = parts.run_parallel(args)
            except SystemExit as exc:
                code = exc.code
            finally:
                if minuterie:
                    minuterie.cancel()
        self.assertLess(time.monotonic() - debut, 30)
        evenements = [json.loads(l) for l in flux.read_text(encoding="utf-8").splitlines()]
        fin = [e for e in evenements if e["type"] == "fin"]
        self.assertEqual(len(fin), 1, evenements)
        self.assertEqual(fin[0]["statut"], "interrompu")
        return code, fin[0], sortie.getvalue()

    def test_la_part_echue_est_non_concluante_avec_ses_restants(self):
        code, fin, sortie = self._couper(echeance=time.time() + 1.0)
        self.assertEqual(code, 1)
        self.assertEqual(fin["raison"], "echeance")
        self.assertEqual(fin["restants"], self.MODULES)
        self.assertIn("Part non concluante (#8291)", sortie)
        self.assertIn("2 module(s) non terminé(s) sur 2, 0 test(s) en échec", sortie)

    #: l'enfant simulé : écrit un résultat terminal pour chaque test de son
    #: sous-plan retenu par `garder`, puis ne finit jamais (coupé à l'échéance)
    def _partiel(self, garder):
        def lancer(commande, **kw):
            script = (
                "import json, sys, time\n"
                "cmd, garder = sys.argv[1:-1], set(json.loads(sys.argv[-1]))\n"
                "plan = json.load(open(cmd[cmd.index('--plan') + 1], encoding='utf-8'))\n"
                "flux = cmd[cmd.index('--flow') + 1]\n"
                "tests = [t for e in plan['parts']['psql'] for t in e.get('tests', [])]\n"
                "with open(flux, 'a', encoding='utf-8') as fh:\n"
                "    for t in tests:\n"
                "        if t in garder:\n"
                "            fh.write(json.dumps({'type': 'test', 'module': t.split('.')[1],\n"
                "                                 'test': t, 'statut': 'succes'}) + '\\n')\n"
                "time.sleep(60)\n")
            return POPEN([sys.executable, "-c", script, *commande[2:], json.dumps(sorted(garder))],
                         cwd=kw.get("cwd"))
        return lancer

    def test_un_module_partiel_reste_et_un_module_termine_sort(self):
        """Relecture mty-amh-impl-11 et ameesh-dev3 (B/P2) : t1 terminé et t2
        coupé dans le même module le laissaient hors des restants."""
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(REPO)
        unites = parts._unites_de(self.MODULES)
        ids = sorted(t for v in unites.values() for t in v)
        complet = [t for t in ids if parts._module_of_test_id(t) == self.MODULES[0]]
        partiel = [t for t in ids if parts._module_of_test_id(t) == self.MODULES[1]]
        self.assertGreater(len(partiel), 1)
        garder = set(complet) | {partiel[0]}
        code, fin, sortie = self._couper(echeance=time.time() + 3.0,
                                         popen=self._partiel(garder))
        self.assertEqual(code, 1)
        self.assertEqual(fin["restants"], [self.MODULES[1]])
        self.assertIn("1 module(s) non terminé(s) sur 2", sortie)

    def test_la_part_annulee_par_le_job_laisse_sa_fin(self):
        """Le flux de psql 10/16 du 26/11 n'avait aucune `fin` (SIGTERM)."""
        code, fin, _ = self._couper(signal_apres=1.0)
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(fin["raison"], f"signal {int(signal.SIGTERM)}")
        self.assertEqual(fin["restants"], self.MODULES)

    def test_la_part_psql_porte_son_echeance(self):
        doc = canon.load_yaml((REPO / ".github" / "workflows" / "tests.yml")
                              .read_text(encoding="utf-8"))
        etape = next(s for s in doc["jobs"]["psql"]["steps"]
                     if str(s.get("name", "")).startswith("part "))
        self.assertIn('--echeance "$(( AMEESH_JOB_DEBUT + (AMEESH_PLAFOND_MIN - 3) * 60 ))"',
                      etape["run"])


if __name__ == "__main__":
    unittest.main()
