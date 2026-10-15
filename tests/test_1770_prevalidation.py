# SPDX-License-Identifier: AGPL-3.0-only
"""#1770 : prévalidation au gel — sélection, refus nommés, preuve.

Aucune VM n'est contactée : la sélection, le flux et l'offload sont testés
par leurs entrées/sorties (le run réel se vérifie sur une VM, hors de ces
témoins bornés)."""
from __future__ import annotations

import json
import unittest

import ameesh.prevalidation  # noqa: F401  (preuve du chargement du fichier, plan L161f #1339)
from ameesh import prevalidation as pv

A = "a" * 40
B = "b" * 40


def plan_sain() -> dict:
    return {
        "schema": pv.PLAN_SCHEMA, "head": A,
        "map": {"commit": B, "used": True, "problem": None, "age_h": 1.0},
        # forme réelle du plan L161f : `selection` ne porte que des comptes,
        # les listes de modules sont dans les parts (#1770, défaut trouvé en
        # relisant un vrai plan : le compte lu comme une liste faisait échouer)
        "selection": {"mode": "impacted", "why": "", "modules": 3, "impacted": 3,
                      "contract": 1, "selected": 3, "skipped": 0},
        "sure": True, "phase2_safe": True, "unsure": [], "phase2_why": "",
        "contract": ["test_cluster"],
        "parts": {"psycopg": [{"part": 1, "of": 2, "modules": ["test_a"], "estimate_s": 10},
                              {"part": 2, "of": 2, "modules": ["test_cluster", "test_b"],
                               "estimate_s": 20}],
                  "psql": []},
    }


def flux(statut: str = "succes", part: str = "psycopg:1", module: str = "test_a") -> str:
    return "\n".join([
        pv.PART_MARK + part,
        json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "debut",
                    "part": part, "modules": [module]}),
        json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                    "part": part, "module": module, "test": "test_x", "statut": "succes"}),
        json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                    "part": part, "statut": statut, "duree_s": 1.2}),
    ])


class SelectionTest(unittest.TestCase):
    def test_plan_sain_accepte(self):
        sel = pv.selection(plan_sain(), head=A)
        self.assertTrue(sel.ok, sel.why)
        self.assertEqual(sel.card_commit, B)
        self.assertEqual(len(sel.parts), 2)
        self.assertIn("test_cluster", sel.modules)
        # les modules viennent des parts, pas du compte `selection.modules`
        self.assertEqual(sel.modules, ("test_a", "test_cluster", "test_b"))

    def test_carte_absente_refusee(self):
        plan = plan_sain()
        plan["map"] = {"used": False, "problem": "carte absente"}
        sel = pv.selection(plan, head=A)
        self.assertEqual(sel.causes, ("card-missing",))

    def test_carte_perimee_refusee(self):
        plan = plan_sain()
        plan["map"] = {"used": False, "commit": B, "problem": "carte vieille de 50 h"}
        sel = pv.selection(plan, head=A)
        self.assertEqual(sel.causes, ("card-stale",))

    def test_repli_suite_complete_refuse(self):
        plan = plan_sain()
        plan["selection"] = {"mode": "full", "why": "fondation modifiée",
                             "modules": 0, "selected": 0}
        sel = pv.selection(plan, head=A)
        self.assertIn("selection-full", sel.causes)

    def test_selection_non_sure_refusee(self):
        plan = plan_sain()
        plan["sure"] = False
        plan["unsure"] = ["bloc inconnu dans src/ameesh/x.py"]
        sel = pv.selection(plan, head=A)
        self.assertIn("selection-unsafe", sel.causes)
        self.assertIn("bloc inconnu", sel.why)

    def test_phase2_non_sure_refusee(self):
        plan = plan_sain()
        plan["phase2_safe"] = False
        plan["phase2_why"] = "carte de plus de 24 h"
        sel = pv.selection(plan, head=A)
        self.assertIn("selection-unsafe", sel.causes)

    def test_tete_changee_refusee(self):
        plan = plan_sain()
        plan["head"] = B
        sel = pv.selection(plan, head=A)
        self.assertEqual(sel.causes, ("head-moved",))

    def test_contrat_hors_selection_refuse(self):
        plan = plan_sain()
        plan["contract"] = ["test_cluster", "test_absent"]
        sel = pv.selection(plan, head=A)
        self.assertIn("selection-unsafe", sel.causes)
        self.assertIn("test_absent", sel.why)

    def test_part_sans_modules_refusee(self):
        plan = plan_sain()
        plan["parts"]["psycopg"][0]["modules"] = None
        sel = pv.selection(plan, head=A)
        self.assertIn("selection-unsafe", sel.causes)

    def test_compte_de_modules_incoherent_refuse(self):
        plan = plan_sain()
        plan["selection"]["selected"] = 5
        sel = pv.selection(plan, head=A)
        self.assertIn("selection-unsafe", sel.causes)
        self.assertIn("incohérent", sel.why)


class ProofTest(unittest.TestCase):
    def setUp(self):
        self.sel = pv.selection(plan_sain(), head=A)
        self.flow = pv.parse_flow(flux())

    def test_vert_avec_fin_succes(self):
        rapport = self.sel.proof(lot=7, host="vm-agents-1",
                                 run={"outcome": "green", "exit": 0, "seconds": 12.0},
                                 flow=self.flow)
        self.assertTrue(rapport["ok"])
        self.assertEqual(rapport["outcome"], "green")
        self.assertEqual(rapport["modules"], ["test_a"])
        self.assertEqual(rapport["sha"], A)
        self.assertEqual(rapport["host"], "vm-agents-1")

    def test_sans_fin_le_run_est_incomplet(self):
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow={"fins": (), "modules": ("test_a",)})
        self.assertFalse(rapport["ok"])
        self.assertIn("run-incomplete", rapport["causes"])

    def test_fin_echec_refuse(self):
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=pv.parse_flow(flux(statut="echec")))
        self.assertFalse(rapport["ok"])
        self.assertIn("run-red", rapport["causes"])

    def test_tete_changee_invalide(self):
        rapport = self.sel.proof(lot=7, host="vm",
                                 run={"outcome": "green", "exit": 0},
                                 flow=self.flow, head_after=B)
        self.assertFalse(rapport["ok"])
        self.assertIn("head-moved", rapport["causes"])

    def test_selection_refusee_ne_produit_pas_de_vert(self):
        sel = pv.selection(plan_sain(), head=B)
        rapport = sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                            flow=self.flow)
        self.assertFalse(rapport["ok"])
        self.assertIn("head-moved", rapport["causes"])


class RunTest(unittest.TestCase):
    def test_argv_force_le_non_repli(self):
        argv = pv.offload_argv("vm-agents-1", "echo ok", timeout=900)
        self.assertIn("--fallback", argv)
        self.assertEqual(argv[argv.index("--fallback") + 1], "none")
        self.assertIn("--timeout", argv)
        self.assertIn("900", argv)

    def test_script_distant_par_part_et_marqueurs(self):
        sel = pv.selection(plan_sain(), head=A)
        script = pv.remote_script(sel.plan_path or "plan.json", sel.parts)
        self.assertIn("--plan plan.json", script)
        self.assertIn(pv.PART_MARK + "psycopg:1", script)
        self.assertIn(pv.PART_MARK + "psycopg:2", script)
        self.assertIn(pv.SECONDS_MARK, script)
        self.assertIn("exit $ec", script)
        self.assertNotIn("--fallback", script)
        # sans clé au bloc, la convention du plan L161f s'applique
        self.assertIn(pv.DEFAULT_PARTS_SCRIPT + " run --plan", script)

    def test_script_distant_suit_le_lanceur_du_bloc(self):
        """Le bloc peut nommer un autre lanceur de part (#1770, Nexlink)."""
        sel = pv.selection(plan_sain(), head=A)
        script = pv.remote_script(sel.plan_path or "plan.json", sel.parts,
                                  parts_script="tools/nexlink-parts.py")
        self.assertIn("tools/nexlink-parts.py run --plan", script)
        self.assertNotIn(pv.DEFAULT_PARTS_SCRIPT, script)

    def test_classify(self):
        self.assertEqual(pv.classify(0), "green")
        self.assertEqual(pv.classify(1), "run-red")
        self.assertEqual(pv.classify(75), "no-vm")
        self.assertEqual(pv.classify(75, "lost the connection to vm (ssh exit 255)"),
                         "transport-lost")
        self.assertEqual(pv.classify(124), "run-incomplete")
        self.assertEqual(pv.classify(130), "run-incomplete")
        self.assertEqual(pv.classify(70), "run-incomplete")

    def test_parse_flow(self):
        lu = pv.parse_flow(flux(part="psycopg:2", module="test_b"))
        self.assertEqual(lu["modules"], ("test_b",))
        self.assertEqual(lu["tests"], 1)
        self.assertEqual(lu["fins"], ("succes",))
        self.assertEqual(lu["parts"], ("psycopg:2",))
        self.assertEqual(lu["fins_par_part"], {"psycopg:2": "succes"})

    def test_parse_seconds(self):
        self.assertEqual(pv.parse_seconds("bruit\n%s42\n" % pv.SECONDS_MARK), 42.0)
        self.assertIsNone(pv.parse_seconds("bruit"))

    def test_run_offload_lit_sortie_et_code(self):
        class Fake:
            returncode = 0
            stdout = flux() + "\n%s37\n" % pv.SECONDS_MARK
            stderr = ""

        result = pv.run_offload("vm-agents-1", "echo ok", runner=lambda *a, **k: Fake())
        self.assertEqual(result["outcome"], "green")
        self.assertEqual(result["seconds"], 37.0)
        self.assertEqual(result["flow"]["modules"], ("test_a",))

    def test_run_offload_rouge(self):
        class Fake:
            returncode = 1
            stdout = flux(statut="echec")
            stderr = ""

        result = pv.run_offload("vm-agents-1", "echo ko", runner=lambda *a, **k: Fake())
        self.assertEqual(result["outcome"], "run-red")


class HostTest(unittest.TestCase):
    def test_hote_local_refuse(self):
        ok, cause = pv.host_allowed("pc-smichea", local="pc-smichea")
        self.assertFalse(ok)
        self.assertEqual(cause, "no-vm")

    def test_hote_vide_refuse(self):
        ok, cause = pv.host_allowed("")
        self.assertFalse(ok)
        self.assertEqual(cause, "no-vm")

    def test_deni_refuse(self):
        ok, _ = pv.host_allowed("pc-smichea", local="autre", deny=("pc-smichea",))
        self.assertFalse(ok)

    def test_vm_agents_acceptee(self):
        ok, cause = pv.host_allowed("vm-agents-1", local="pc-smichea")
        self.assertTrue(ok, cause)


if __name__ == "__main__":
    unittest.main()
