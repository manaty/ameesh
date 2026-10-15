# SPDX-License-Identifier: AGPL-3.0-only
"""#1770 : commande explicite `ameesh work prevalidate` et contrôles du canon.

Aucune VM n'est contactée : l'offload et la lecture de la tête publiée sont
testés par leurs entrées/sorties. La commande refuse un canon sans bloc
(`block-missing`) et n'écrit aucun jalon."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from ameesh import db as db_mod
from ameesh import mesh_cli, prevalidation as pv

A = "a" * 40
B = "b" * 40
C = "c" * 40


def plan_canon(head: str = A) -> dict:
    return {
        "schema": pv.PLAN_SCHEMA, "head": head,
        "map": {"commit": B, "used": True, "problem": None, "age_h": 1.0},
        # forme réelle : les comptes dans `selection`, les listes dans `parts`
        "selection": {"mode": "impacted", "why": "", "modules": 2, "impacted": 2,
                      "contract": 1, "selected": 2, "skipped": 0},
        "sure": True, "phase2_safe": True, "unsure": [], "phase2_why": "",
        "contract": ["test_cluster"],
        "classes": {"test_a": ["P0", "modifié"], "test_cluster": ["P1", "contrat"],
                    "test_autre": ["P2", "rouge 7 j"]},
        "parts": {"psycopg": [{"part": 1, "of": 1,
                               "modules": ["test_a", "test_cluster"],
                               "estimate_s": 10}]},
    }


def params(**over) -> SimpleNamespace:
    values = {"classes": ("P0", "P1"), "contracts": (), "hosts": ("vm1",),
              "timeout_seconds": 60}
    values.update(over)
    return SimpleNamespace(**values)


def flux(statut: str = "succes", module: str = "test_a") -> str:
    return "\n".join([
        pv.PART_MARK + "psycopg:1",
        json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                    "part": "psycopg:1", "module": module, "test": "test_x",
                    "statut": "succes"}),
        json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                    "part": "psycopg:1", "statut": statut, "duree_s": 1.2}),
    ]) + "\n" + pv.SECONDS_MARK + "42\n"


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def runner_for(returncode: int = 0, stdout: str = ""):
    def run(argv, **kwargs):
        return _Proc(returncode, stdout)
    return run


class MeetsCanonTest(unittest.TestCase):
    def test_selection_exacte_acceptee(self):
        ok, why = pv.meets_canon(plan_canon(), params())
        self.assertTrue(ok, why)

    def test_module_hors_classes_refuse(self):
        plan = plan_canon()
        plan["parts"]["psycopg"][0]["modules"].append("test_autre")
        plan["selection"]["selected"] = 3
        ok, why = pv.meets_canon(plan, params())
        self.assertFalse(ok)
        self.assertIn("hors des classes", why)

    def test_contrat_absent_de_la_selection_refuse(self):
        plan = plan_canon()
        plan["parts"]["psycopg"][0]["modules"] = ["test_a"]
        plan["selection"]["selected"] = 1
        ok, why = pv.meets_canon(plan, params())
        self.assertFalse(ok)
        self.assertIn("absents", why)

    def test_plan_sans_classes_refuse(self):
        plan = plan_canon()
        plan.pop("classes")
        ok, why = pv.meets_canon(plan, params())
        self.assertFalse(ok)

    def test_canon_sans_classes_refuse(self):
        ok, _ = pv.meets_canon(plan_canon(), params(classes=()))
        self.assertFalse(ok)


class PublishedHeadTest(unittest.TestCase):
    def test_tete_lue_au_distant(self):
        got = pv.published_head("lot/x", runner=runner_for(
            0, "%s\trefs/heads/lot/x\n" % C))
        self.assertEqual(got, C)

    def test_reference_introuvable_refuse(self):
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.published_head("lot/x", runner=runner_for(0, ""))
        self.assertEqual(caught.exception.cause, "head-moved")

    def test_lecture_impossible_refuse(self):
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.published_head("lot/x", runner=runner_for(1, ""))
        self.assertEqual(caught.exception.cause, "head-moved")


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = os.path.join(self.tmp.name, "plan.json")
        with open(self.plan, "w", encoding="utf-8") as fh:
            json.dump(plan_canon(), fh)

    def test_vert_avec_hote_et_delai_du_canon(self):
        report = pv.validate(lot=1770, plan_path=self.plan, head=A, host="vm1",
                             params=params(), repo=self.tmp.name,
                             runner=runner_for(0, flux()))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["causes"], [])
        self.assertEqual(report["host"], "vm1")
        self.assertEqual(report["seconds"], 42.0)
        self.assertIn("test_a", report["modules"])

    def test_lanceur_de_part_du_bloc_dans_la_commande(self):
        """Le bloc nomme le lanceur de part : il part tel quel sur la VM."""
        seen: list = []

        def capture(argv, **kwargs):
            seen.append(argv)
            return _Proc(0, flux())

        report = pv.validate(lot=1770, plan_path=self.plan, head=A, host="vm1",
                             params=params(parts_script="tools/parts.py"),
                             repo=self.tmp.name, runner=capture)
        self.assertTrue(report["ok"], report)
        command = " ".join(seen[0])
        self.assertIn("tools/parts.py run --plan", command)
        self.assertNotIn(pv.DEFAULT_PARTS_SCRIPT, command)

    def test_lanceur_de_part_par_defaut_sans_cle(self):
        """Sans clé au bloc, la convention du plan L161f s'applique."""
        seen: list = []

        def capture(argv, **kwargs):
            seen.append(argv)
            return _Proc(0, flux())

        report = pv.validate(lot=1770, plan_path=self.plan, head=A, host="vm1",
                             params=params(), repo=self.tmp.name, runner=capture)
        self.assertTrue(report["ok"], report)
        self.assertIn(pv.DEFAULT_PARTS_SCRIPT, " ".join(seen[0]))

    def test_hote_hors_canon_refuse(self):
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.validate(lot=1770, plan_path=self.plan, head=A, host="pc-smichea",
                        params=params(), repo=self.tmp.name,
                        runner=runner_for(0, flux()))
        self.assertEqual(caught.exception.cause, "no-vm")

    def test_contrat_declare_introuvable_refuse(self):
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.validate(lot=1770, plan_path=self.plan, head=A, host="vm1",
                        params=params(contracts=("tests/parts/toujours.txt",)),
                        repo=self.tmp.name, runner=runner_for(0, flux()))
        self.assertEqual(caught.exception.cause, "selection-unsafe")

    def test_rouge_ne_produit_pas_de_vert(self):
        report = pv.validate(lot=1770, plan_path=self.plan, head=A, host="vm1",
                             params=params(), repo=self.tmp.name,
                             runner=runner_for(1, flux("echec")))
        self.assertFalse(report["ok"])
        self.assertIn("run-red", report["causes"])

    def test_tete_publiee_changee_invalide(self):
        report = pv.validate(lot=1770, plan_path=self.plan, head=A, host="vm1",
                             params=params(), repo=self.tmp.name,
                             published_ref="lot/x",
                             runner=runner_for(0, flux()),
                             git_runner=runner_for(0, "%s\trefs/heads/lot/x\n" % C))
        self.assertFalse(report["ok"])
        self.assertIn("head-moved", report["causes"])

    def test_tete_publiee_identique_valide(self):
        report = pv.validate(lot=1770, plan_path=self.plan, head=A, host="vm1",
                             params=params(), repo=self.tmp.name,
                             published_ref="lot/x",
                             runner=runner_for(0, flux()),
                             git_runner=runner_for(0, "%s\trefs/heads/lot/x\n" % A))
        self.assertTrue(report["ok"], report)


class CommandTest(unittest.TestCase):
    def _args(self, **over):
        values = {"lot": 1770, "sha": A, "plan": "plan.json", "host": "vm1",
                  "base": "", "repo": "", "canon_repo": "", "canon_commit": "",
                  "project": "ameesh", "published_ref": "", "remote": "origin",
                  "json": False}
        values.update(over)
        return SimpleNamespace(**values)

    def test_commande_enregistree(self):
        self.assertIn("prevalidate", pv.COMMANDS)
        parser = mesh_cli.build_parser()
        args = parser.parse_args(["work", "prevalidate", "1770", "--sha", A,
                                  "--plan", "plan.json", "--host", "vm1"])
        self.assertEqual(args.work_command, "prevalidate")
        self.assertEqual(args.lot, 1770)

    def test_canon_absent_refuse_block_missing(self):
        out = StringIO()
        with redirect_stdout(out):
            code = pv.run(None, None, self._args())
        self.assertEqual(code, 1)
        self.assertIn("block-missing", out.getvalue())

    def test_canon_absent_refuse_en_json(self):
        out = StringIO()
        with redirect_stdout(out):
            code = pv.run(None, None, self._args(json=True))
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["causes"], ["block-missing"])
        self.assertEqual(payload["schema"], pv.REPORT_SCHEMA)


class PersistenceTest(unittest.TestCase):
    """Le rapport s'imprime avant l'écriture : un run réel ne se perd jamais.

    La table 0450 est livrée avec le lot : avant son déploiement, l'écriture
    échoue en `SchemaMissing` et l'avertissement nomme `ameesh migrate` ; le
    sort du run, lui, ne change pas."""

    def _args(self):
        return SimpleNamespace(lot=1770, sha=A, plan="plan.json", host="vm1",
                               base="", repo="", canon_repo="", canon_commit="c" * 40,
                               project="ameesh", published_ref="", remote="origin",
                               json=True)

    def _green(self) -> dict:
        return {"schema": pv.REPORT_SCHEMA, "lot": 1770, "sha": A, "base": "",
                "ok": True, "outcome": "green", "causes": [], "host": "vm1",
                "modules": ["test_a"], "seconds": 42.0}

    def test_rapport_imprime_malgre_la_table_absente(self):
        out, err = StringIO(), StringIO()
        missing = db_mod.SchemaMissing(
            'relation "prevalidation_reports" does not exist')
        with mock.patch.object(pv, "load_parameters",
                               return_value=(None, params())), \
                mock.patch.object(pv, "validate", return_value=self._green()), \
                mock.patch.object(pv, "record", side_effect=missing), \
                redirect_stdout(out), redirect_stderr(err):
            code = pv.run(None, object(), self._args())
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertIn("non persisté", err.getvalue())
        self.assertIn("migrate", err.getvalue())

    def test_rapport_persiste_sans_avertissement(self):
        out, err = StringIO(), StringIO()
        with mock.patch.object(pv, "load_parameters",
                               return_value=(None, params())), \
                mock.patch.object(pv, "validate", return_value=self._green()), \
                mock.patch.object(pv, "record") as recorded, \
                redirect_stdout(out), redirect_stderr(err):
            code = pv.run(None, object(), self._args())
        self.assertEqual(code, 0)
        recorded.assert_called_once()
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
