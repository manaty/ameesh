# SPDX-License-Identifier: AGPL-3.0-only
"""#3749 — la suite complète est psycopg ; la suite psql entière est nocturne.

Règle owner du 26/10 (canon home#184) : une suite complète en 10 min de mur
au plus. Hors lot (paquet, main, release), le plan ne donne plus à psql que sa
liste pilote et tout le contrat ; `--psql-full` (le nocturne,
`.github/workflows/psql-nocturne.yml`) donne à psql tous les modules et à
psycopg aucune part, et l'agrégat n'admet une famille psycopg vide que là.
Témoins sans base : le plan, l'agrégat et les deux workflows.
"""
from __future__ import annotations

import datetime
import importlib.util
import io
import json
import pathlib
import contextlib
import tempfile
import unittest
from unittest import mock

from ameesh import canon

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
NOW = datetime.datetime(2026, 10, 26, 14, 0, tzinfo=datetime.timezone.utc)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = _load("test_plan_3749", "test-plan.py")
agregat = _load("ci_agregat_3749", "ci-agregat.py")


def _workflow(name: str) -> dict:
    return canon.load_yaml((WORKFLOWS / name).read_text(encoding="utf-8")) or {}


def _on(doc: dict) -> dict:
    # PyYAML lit la clé `on:` comme le booléen True
    return doc.get("on", doc.get(True)) or {}


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.mods = plan.parts.modules()
        self.contract = plan.read_list(plan.ALWAYS_FILE, self.mods)
        self.pilot = plan.read_list(plan.PILOT_FILE, self.mods, required=True)
        self.state = {
            "map": {"commit": "c" * 40, "generated": NOW.isoformat(), "age_h": 0.0,
                    "used": True, "problem": "", "stale": False},
            "sure": True, "phase2_safe": True, "lot_safe": True, "phase2_why": "",
            "unsure": [], "untested": [], "unknown_blocks": [], "foundation": [],
            "changed": 1, "always": [], "contract": list(self.contract),
            "classes": {m: ["P3", ""] for m in self.mods}}

    def _plan(self, event, **kw):
        nature = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "classify", return_value=dict(self.state)), \
                mock.patch.object(plan, "nature", return_value=nature):
            return plan.make_plan(event, {}, NOW, **kw)

    @staticmethod
    def _ran(p, driver):
        return sorted(m for ps in p["parts"][driver] for m in ps["modules"])

    def test_paquet_main_et_release_passent_psql_pilote_et_contrat(self):
        expected = sorted(set(self.pilot) | set(self.contract))
        self.assertLess(len(expected), len(self.mods))
        cases = {
            "poussée de main": dict(event="push", ref="refs/heads/main"),
            "portier de paquet": dict(event="push", ref="refs/heads/integration/h-1"),
            "PR de paquet": dict(event="pull_request", mode="full", base_ref="main",
                                 head_ref="integration/paquet-h-1"),
            "PR de release": dict(event="pull_request", mode="full", base_ref="main",
                                  head_ref="release/1.2.0"),
            "à la main": dict(event="workflow_dispatch"),
        }
        for why, kw in cases.items():
            with self.subTest(why):
                p = self._plan(**kw)
                self.assertEqual(self._ran(p, "psycopg"), self.mods)
                self.assertEqual(self._ran(p, "psql"), expected)
                self.assertFalse(p["psql_full"])
                self.assertIn("pilot list + contract", plan.summary(p))

    def test_le_nocturne_passe_toute_la_suite_sous_psql_et_rien_sous_psycopg(self):
        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event):
                p = self._plan(event, psql_full=True)
                self.assertEqual(p["parts"]["psycopg"], [])
                self.assertEqual(self._ran(p, "psql"), self.mods)
                self.assertEqual({ps["of"] for ps in p["parts"]["psql"]},
                                 {plan.PARTS["psql"][1]})
                out = plan.outputs(p)
                self.assertEqual(out["psycopg"], "[]")
                self.assertEqual(out["psycopg_parts"], "0")
                self.assertEqual(out["code"], "true")
                self.assertIn("psql night run", plan.summary(p))

    def test_le_nocturne_ne_prend_jamais_la_selection_d_un_lot(self):
        p = self._plan("pull_request", mode="lot", base_ref="main",
                       head_ref="lot/x", psql_full=True)
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertEqual(self._ran(p, "psql"), self.mods)
        self.assertEqual(p["parts"]["psycopg"], [])

    def test_le_lot_garde_sa_selection_psql(self):
        p = self._plan("pull_request", base_ref="main", head_ref="lot/x")
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertEqual(self._ran(p, "psql"),
                         sorted(set(self.pilot) & set(self.contract)))

    @staticmethod
    def _consumer_modules(p, driver):
        """Les modules que scripts/test-parts.py `run --plan` passe vraiment,
        part par part : la liste explicite du plan, sinon (repli L116) la
        liste pilote seule si le plan la demande, sinon toute la suite."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "plan.json"
            path.write_text(json.dumps(p), encoding="utf-8")
            seen = set()
            for entry in p["parts"][driver]:
                got = plan.parts.read_plan(path, driver, entry["part"])
                if got is not None:
                    seen.update(got["modules"])
                elif driver == "psql" and plan.parts._driver_plan(path):
                    seen.update(plan.parts.read_list(plan.PILOT_FILE, plan.parts.modules()))
                else:
                    seen.update(plan.parts.modules())
            return sorted(seen)

    def test_repli(self):
        # R1 cx3 (B/P2) : le repli hors nocturne passait `pilot_list` sans
        # module ; test-parts.py ne lisait alors que pilote-psql.txt et sept
        # contrats sautaient psql. Le témoin lit ce que le consommateur
        # exécute, pas seulement le drapeau.
        expected = sorted(set(self.pilot) | set(self.contract))
        cases = {
            "push main": plan.fallback_plan("push", "boom"),
            "PR de release": plan.fallback_plan("pull_request", "boom", base_ref="main",
                                                head_ref="release/1.2.0"),
            "gel de lot": plan.fallback_plan("pull_request", "boom", mode="lot",
                                             base_ref="main", head_ref="lot/x"),
        }
        for why, p in cases.items():
            with self.subTest(why):
                self.assertEqual(self._consumer_modules(p, "psql"), expected)
                self.assertEqual({ps["of"] for ps in p["parts"]["psql"]},
                                 {plan.PARTS["psql"][1]})
                self.assertEqual(self._consumer_modules(p, "psycopg"), self.mods)
        night = plan.fallback_plan("schedule", "boom", psql_full=True)
        self.assertEqual(night["parts"]["psycopg"], [])
        self.assertEqual(self._consumer_modules(night, "psql"), self.mods)
        self.assertEqual(len(night["parts"]["psql"]), plan.PARTS["psql"][1])
        self.assertEqual(plan.outputs(night)["psycopg"], "[]")

    def test_repli_sur_listes_illisibles_rend_la_suite_psql_entiere(self):
        for failure in (ValueError("stale entry"), FileNotFoundError("absent"), []):
            with self.subTest(failure=failure):
                effect = failure if isinstance(failure, Exception) else None
                value = failure if effect is None else None
                with mock.patch.object(plan, "read_list", side_effect=effect,
                                       return_value=value):
                    p = plan.fallback_plan("push", "boom")
                self.assertEqual(self._consumer_modules(p, "psql"), self.mods)
                self.assertFalse(any(ps["pilot_list"] for ps in p["parts"]["psql"]))
                self.assertIn("whole suite under psql", " ".join(p["unsure"]))

    def test_cli_psql_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []

            def fake(event, card, **kw):
                seen.append(kw["psql_full"])
                return plan.fallback_plan(event, "test", psql_full=kw["psql_full"])

            out = pathlib.Path(tmp) / "plan.json"
            with mock.patch.object(plan, "make_plan", side_effect=fake), \
                    contextlib.redirect_stdout(io.StringIO()):
                for value in ("true", "false", ""):
                    args = ["plan", "--out", str(out), "--event", "schedule"]
                    plan.main(args + ["--psql-full", value])
                plan.main(["plan", "--out", str(out), "--event", "push"])
            self.assertEqual(seen, [True, False, False, False])
            self.assertNotEqual(json.loads(out.read_text())["parts"]["psycopg"], [])


class AgregatTest(unittest.TestCase):
    def _classify(self, **over):
        values = dict(event="schedule", plan="success", version="skipped",
                      version_only="false", docs_only="false", doc_readers="skipped",
                      psycopg="skipped", psycopg_parts="[]",
                      psql="success", psql_parts=json.dumps(list(range(1, 17))),
                      platform_macos="success", fs_class="skipped")
        values.update(over)
        return agregat.classify(**values)

    def test_le_nocturne_admet_psycopg_sans_part(self):
        result = self._classify(psql_full="true")
        self.assertEqual(result["state"], agregat.GREEN, result)

    def test_hors_nocturne_psycopg_sans_part_reste_rouge(self):
        result = self._classify()
        self.assertEqual(result["state"], agregat.RED)
        self.assertIn("psycopg (skipped, parts [])", result["red"])

    def test_le_nocturne_n_excuse_pas_une_part_psycopg_planifiee(self):
        result = self._classify(psql_full="true", psycopg_parts="[1, 2]")
        self.assertEqual(result["state"], agregat.RED)

    def test_le_nocturne_rougit_sur_un_echec_psql(self):
        result = self._classify(psql_full="true", psql="failure")
        self.assertEqual(result["state"], agregat.RED)

    def test_cli(self):
        argv = ["--event", "schedule", "--plan", "success", "--version", "skipped",
                "--psycopg", "skipped", "--psycopg-parts", "[]",
                "--psql", "success", "--psql-parts", "[1]",
                "--plateforme-macos", "success", "--autre-fs", "skipped"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agregat.main(argv + ["--psql-full", "true"], jobs=[]), 0)
            self.assertEqual(agregat.main(argv, jobs=[]), 1)


class WorkflowTest(unittest.TestCase):
    def test_le_nocturne_appelle_tests_avec_psql_full(self):
        doc = _workflow("psql-nocturne.yml")
        on = _on(doc)
        self.assertIn("schedule", on)
        self.assertTrue(on["schedule"][0]["cron"])
        self.assertIn("workflow_dispatch", on)
        jobs = doc["jobs"]
        self.assertEqual(list(jobs), ["suite"])
        self.assertEqual(jobs["suite"]["uses"], "./.github/workflows/tests.yml")
        self.assertIs(jobs["suite"]["with"]["psql-full"], True)
        self.assertFalse(doc["concurrency"]["cancel-in-progress"])

    def test_tests_yml_porte_l_entree_et_la_passe_au_plan_et_a_l_agregat(self):
        doc = _workflow("tests.yml")
        entree = _on(doc)["workflow_call"]["inputs"]["psql-full"]
        self.assertEqual((entree["type"], entree["default"]), ("boolean", False))
        text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("PSQL_FULL: ${{ inputs.psql-full }}", text)
        self.assertIn('--psql-full "$PSQL_FULL"', text)
        self.assertIn('--psql-full "${{ inputs.psql-full }}"', text)

    def test_autre_systeme_de_fichiers_sans_part_psycopg_est_saute(self):
        cond = _workflow("tests.yml")["jobs"]["fs-class"]["if"]
        self.assertIn("needs.plan.outputs.psycopg != '[]'", cond)

    def test_chaque_job_appele_garde_son_plafond(self):
        jobs = _workflow("tests.yml")["jobs"]
        for name, job in jobs.items():
            with self.subTest(name):
                self.assertIsInstance(job.get("timeout-minutes"), int)


if __name__ == "__main__":
    unittest.main()
