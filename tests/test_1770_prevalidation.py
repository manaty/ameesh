# SPDX-License-Identifier: AGPL-3.0-only
"""#1770 : prévalidation au gel — sélection, refus nommés, preuve.

Aucune VM n'est contactée : la sélection, le flux et l'offload sont testés
par leurs entrées/sorties (le run réel se vérifie sur une VM, hors de ces
témoins bornés)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import ameesh.prevalidation  # noqa: F401  (preuve du chargement du fichier, plan L161f #1339)
from ameesh import offload, prevalidation as pv

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


def flux_complet(statut_2: str = "succes") -> str:
    """Le flux d'une exécution **complète** de `plan_sain()` : deux parts,
    trois modules (le contrat `test_cluster` compris)."""
    lignes = []
    for part, modules, statut in (("psycopg:1", ["test_a"], "succes"),
                                  ("psycopg:2", ["test_cluster", "test_b"], statut_2)):
        lignes.append(pv.PART_MARK + part)
        lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "debut",
                                  "part": part, "modules": modules}))
        for module in modules:
            lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                                      "part": part, "module": module,
                                      "test": "test_x", "statut": "succes"}))
        lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                                  "part": part, "statut": statut, "duree_s": 1.2}))
    return "\n".join(lignes)


def plan_contrat() -> dict:
    """#3800 : le plan de repli écrit quand la sélection n'est pas prouvée."""
    return {
        "schema": pv.PLAN_SCHEMA, "head": A,
        "map": {"used": False, "problem": "carte absente"},
        "selection": {"mode": "contract", "why": "sélection incertaine",
                      "modules": 2, "impacted": 2, "contract": 2,
                      "selected": 2, "skipped": 0},
        "sure": False, "phase2_safe": False, "unsure": ["carte absente"],
        "fallback": {"cause": "selection-full",
                     "causes": ["selection-full"],
                     "why": "selection unsure (deploy/x.py)",
                     "contracts": ["test_cluster"],
                     "modules": ["test_a", "test_cluster"]},
        "contract": ["test_cluster"],
        "classes": {"test_a": ["P1", "contrat"],
                    "test_cluster": ["P1", "contrat"]},
        "parts": {"psycopg": [{"part": 1, "of": 1, "modules": ["test_a", "test_cluster"],
                               "estimate_s": 10}],
                  "psql": []},
    }


def flux_contrat(statut: str = "succes") -> str:
    part = "psycopg:1"
    lignes = [pv.PART_MARK + part]
    for module in ("test_a", "test_cluster"):
        lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                                  "part": part, "module": module,
                                  "test": "test_x", "statut": "succes"}))
    lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                              "part": part, "statut": statut, "duree_s": 1.2}))
    return "\n".join(lignes)


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

    def test_plan_de_contrat_accepte_malgre_la_selection_incertaine(self):
        # #3800 : le plan de repli ne se fait pas passer pour une sélection
        # impactée ; il porte sa cause et ses modules de contrat.
        sel = pv.selection(plan_contrat(), head=A)
        self.assertTrue(sel.ok, sel.why)
        self.assertEqual(sel.fallback["cause"], "selection-full")
        self.assertEqual(sel.modules, ("test_a", "test_cluster"))

    def test_mode_contrat_sans_repli_refuse(self):
        plan = plan_contrat()
        plan["fallback"] = None
        sel = pv.selection(plan, head=A)
        self.assertIn("selection-unsafe", sel.causes)

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

    def test_carte_perimee_marquee_refusee(self):
        # #3450 : une carte périmée juge encore le diff propre d'un lot en CI,
        # mais la prévalidation au gel exige une carte fraîche ; le refus
        # garde sa cause nommée.
        plan = plan_sain()
        plan["map"] = {"commit": B, "used": True, "problem": None,
                       "age_h": 176.0, "stale": True}
        plan["phase2_safe"] = False
        plan["phase2_why"] = "map older than 24 h (176 h)"
        sel = pv.selection(plan, head=A)
        self.assertIn("card-stale", sel.causes)
        self.assertIn("périmée", sel.why)

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
        self.flow = pv.parse_flow(flux_complet())

    def test_vert_seulement_si_toutes_les_parts_et_modules(self):
        rapport = self.sel.proof(lot=7, host="vm-agents-1",
                                 run={"outcome": "green", "exit": 0, "seconds": 12.0},
                                 flow=self.flow)
        self.assertTrue(rapport["ok"], rapport)
        self.assertEqual(rapport["outcome"], "green")
        # les modules du rapport sont ceux réellement exécutés (ordre trié),
        # le plan reste dans `planned`
        self.assertEqual(rapport["modules"], ["test_a", "test_b", "test_cluster"])
        self.assertEqual(rapport["planned"], ["test_a", "test_cluster", "test_b"])
        self.assertEqual(rapport["missing"], [])
        self.assertEqual(rapport["planned_parts"], ["psycopg:1", "psycopg:2"])
        self.assertEqual(rapport["sha"], A)
        self.assertEqual(rapport["host"], "vm-agents-1")

    def test_un_repli_contractuel_dit_toujours_les_contrats_joues(self):
        # #3800 : un vert de contrat ne se lit pas comme un vert de sélection.
        sel = pv.selection(plan_contrat(), head=A)
        rapport = sel.proof(lot=7, host="vm",
                            run={"outcome": "green", "exit": 0, "seconds": 1.0},
                            flow=pv.parse_flow(flux_contrat()))
        self.assertTrue(rapport["ok"], rapport)
        self.assertEqual(rapport["selection"], "contract")
        self.assertEqual(rapport["fallback"]["cause"], "selection-full")
        self.assertIn("contrats seuls", " ".join(rapport["detail"]))
        self.assertEqual(rapport["modules"], ["test_a", "test_cluster"])

    def test_un_repli_avec_contrats_transverses_dit_les_tests_du_lot(self):
        # #3954 : le repli joue aussi les contrats transverses et les tests
        # changés du lot ; la preuve ne les cache pas sous « contrats seuls ».
        modules = ["test_a", "test_cluster", "test_l107_executor_contract",
                   "test_3845_paquet_horaire"]
        plan = plan_contrat()
        plan["fallback"] = dict(plan["fallback"],
                                modules=modules,
                                transverse=["test_l107_executor_contract"],
                                changed_tests=["test_3845_paquet_horaire"])
        plan["classes"] = {name: ["P1", "contrat"] for name in modules}
        plan["parts"] = {"psycopg": [{"part": 1, "of": 1, "modules": modules,
                                      "estimate_s": 10}], "psql": []}
        plan["selection"] = dict(plan["selection"], modules=len(modules),
                                 impacted=len(modules), selected=len(modules))
        sel = pv.selection(plan, head=A)
        lignes = [pv.PART_MARK + "psycopg:1"]
        for module in modules:
            lignes.append(json.dumps({
                "v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                "part": "psycopg:1", "module": module, "test": "test_x",
                "statut": "succes"}))
        lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                                  "part": "psycopg:1", "statut": "succes",
                                  "duree_s": 1.2}))
        rapport = sel.proof(lot=7, host="vm",
                            run={"outcome": "green", "exit": 0, "seconds": 1.0},
                            flow=pv.parse_flow("\n".join(lignes)))
        self.assertTrue(rapport["ok"], rapport)
        self.assertIn("contrats et tests du lot", " ".join(rapport["detail"]))
        self.assertEqual(rapport["fallback"]["transverse"],
                         ["test_l107_executor_contract"])

    def test_une_seule_part_ne_vaut_pas_vert(self):
        """Repro cx1 : une part verte sur deux, un module sur trois -> refus."""
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=pv.parse_flow(flux()))
        self.assertFalse(rapport["ok"])
        self.assertIn("run-incomplete", rapport["causes"])
        # jamais les modules du plan : la preuve dit ce qui a tourné
        self.assertEqual(rapport["modules"], ["test_a"])
        self.assertEqual(rapport["missing"], ["test_b", "test_cluster"])

    def test_fin_verte_sans_test_ninvente_aucun_module(self):
        """Un unique fin=succes, zéro test : incomplet, modules vides."""
        seul_fin = "\n".join([
            pv.PART_MARK + "psycopg:1",
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                        "part": "psycopg:1", "statut": "succes", "duree_s": 0.1}),
        ])
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=pv.parse_flow(seul_fin))
        self.assertFalse(rapport["ok"])
        self.assertIn("run-incomplete", rapport["causes"])
        self.assertEqual(rapport["modules"], [])
        self.assertEqual(rapport["tests"], 0)
        self.assertEqual(rapport["planned"], ["test_a", "test_cluster", "test_b"])

    def test_module_planifie_non_execute_refuse(self):
        """Deux parts vertes mais un module attendu muet : refus."""
        partiel = "\n".join(
            ligne for ligne in flux_complet().splitlines()
            if '"module": "test_b"' not in ligne)
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=pv.parse_flow(partiel))
        self.assertFalse(rapport["ok"])
        self.assertIn("run-incomplete", rapport["causes"])
        self.assertEqual(rapport["missing"], ["test_b"])

    def test_sans_aucun_flux_le_run_est_incomplet(self):
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow={"fins": (), "modules": ()})
        self.assertFalse(rapport["ok"])
        self.assertIn("run-incomplete", rapport["causes"])
        self.assertEqual(rapport["modules"], [])

    def test_fin_echec_refuse(self):
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=pv.parse_flow(flux_complet(statut_2="echec")))
        self.assertFalse(rapport["ok"])
        self.assertIn("run-red", rapport["causes"])

    def test_fin_interrompue_refuse(self):
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=pv.parse_flow(flux_complet(statut_2="interrompu")))
        self.assertFalse(rapport["ok"])
        self.assertIn("run-incomplete", rapport["causes"])

    def test_fin_statut_inconnu_refuse(self):
        """Un statut autre que `succes` n'est jamais vert (P1 cx1).

        `unknown`, `skipped` ou un statut vide ne disent pas que la part a
        réussi : la preuve les compte comme incomplets, pas comme verts."""
        for statut in ("unknown", "skipped", ""):
            with self.subTest(statut=statut):
                rapport = self.sel.proof(
                    lot=7, host="vm", run={"outcome": "green", "exit": 0},
                    flow=pv.parse_flow(flux_complet(statut_2=statut)))
                self.assertFalse(rapport["ok"], rapport)
                self.assertIn("run-incomplete", rapport["causes"])
                # la part fautive est nommée avec le statut observé
                self.assertIn("psycopg:2", " ".join(rapport["detail"]))

    def test_les_tests_en_echec_sont_nommes(self):
        """#2644 : le refus nommé porte le test en échec, pas seulement la part."""
        flow = "\n".join([
            pv.PART_MARK + "psycopg:1",
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                        "part": "psycopg:1", "module": "test_a",
                        "test": "tests.test_a.C.test_rouge", "statut": "echec"}),
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                        "part": "psycopg:1", "statut": "echec", "duree_s": 1.0}),
        ])
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=pv.parse_flow(flow))
        self.assertFalse(rapport["ok"])
        self.assertIn("run-red", rapport["causes"])
        self.assertEqual(rapport["failed"], ["tests.test_a.C.test_rouge"])
        self.assertIn("tests.test_a.C.test_rouge", " ".join(rapport["detail"]))

    def test_run_rouge_de_loffload_refuse(self):
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "run-red", "exit": 1},
                                 flow=self.flow)
        self.assertFalse(rapport["ok"])
        self.assertIn("run-red", rapport["causes"])

    def _flux_en_echec(self, *echecs, statut_2: str = "succes") -> dict:
        """#4039 : le flux complet, plus des tests en échec
        `(nom, extrait[, part])`."""
        lignes = [flux_complet(statut_2)]
        for echec in echecs:
            nom, extrait = echec[0], echec[1]
            part = echec[2] if len(echec) > 2 else "psycopg:2"
            event = {"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                     "part": part, "module": "test_b", "test": nom,
                     "statut": "erreur"}
            if extrait:
                event["extrait"] = extrait
            lignes.append(json.dumps(event))
        return pv.parse_flow("\n".join(lignes))

    def test_echecs_de_setupclass_seuls_ne_sont_pas_rouges(self):
        """#4039 : base absente du guest — chaque module à base tombe en
        setUpClass ; le run n'est pas concluant (§3), jamais un run-red."""
        echecs = (
            ("setUpClass (tests.test_b.C)", "AssertionError: Postgres injoignable"),
            ("setUpModule (tests.test_cluster)", ""),
            ("tests.test_a.D.test_x", "AssertionError: Postgres injoignable (dsn)"))
        # rouge de l'offload (exit 1), ou part rouge sous un exit 0
        for run, statut in (({"outcome": "run-red", "exit": 1}, "succes"),
                            ({"outcome": "green", "exit": 0}, "echec")):
            with self.subTest(run=run["outcome"]):
                flow = self._flux_en_echec(*echecs, statut_2=statut)
                rapport = self.sel.proof(lot=7, host="vm", run=run, flow=flow)
                self.assertFalse(rapport["ok"])
                self.assertEqual(rapport["outcome"], "run-incomplete", rapport)
                self.assertNotIn("run-red", rapport["causes"])
                self.assertIn("environnement", " ".join(rapport["detail"]))

    def test_un_echec_de_corps_de_test_reste_rouge(self):
        flow = self._flux_en_echec(
            ("setUpClass (tests.test_b.C)", "AssertionError: Postgres injoignable"),
            ("tests.test_b.C.test_y", "AssertionError: 1 != 2"))
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "run-red", "exit": 1},
                                 flow=flow)
        self.assertEqual(rapport["outcome"], "run-red")
        self.assertIn("run-red", rapport["causes"])

    def test_meme_nom_environnement_puis_assertion_reste_rouge(self):
        """#4039 (B/P1 cx3) : un même test en base injoignable sous une part
        et en assertion sous une autre est un rouge, dans les deux ordres."""
        nom = "tests.test_b.C.test_y"
        env = (nom, "AssertionError: Postgres injoignable", "psql:1")
        corps = (nom, "AssertionError: 1 != 2", "psycopg:2")
        for ordre in ((env, corps), (corps, env)):
            with self.subTest(premier=ordre[0][2]):
                flow = self._flux_en_echec(*ordre)
                self.assertEqual(flow["failed"], (nom,))
                self.assertEqual(flow["environment"], ())
                rapport = self.sel.proof(lot=7, host="vm",
                                         run={"outcome": "run-red", "exit": 1},
                                         flow=flow)
                self.assertEqual(rapport["outcome"], "run-red")
                self.assertIn("run-red", rapport["causes"])

    def test_preemption_par_la_porte_nest_pas_no_vm(self):
        """#2298 : un run coupé par la porte (exit 75) dit gate-preempted."""
        rapport = self.sel.proof(lot=7, host="vm",
                                 run={"outcome": "gate-preempted", "exit": 75,
                                      "seconds": 2103.9},
                                 flow=self.flow)
        self.assertFalse(rapport["ok"])
        self.assertEqual(rapport["outcome"], "gate-preempted")
        self.assertIn("gate-preempted", rapport["causes"])
        self.assertNotIn("no-vm", rapport["causes"])

    def test_tete_changee_invalide(self):
        rapport = self.sel.proof(lot=7, host="vm",
                                 run={"outcome": "green", "exit": 0},
                                 flow=self.flow, head_after=B)
        self.assertFalse(rapport["ok"])
        self.assertIn("head-moved", rapport["causes"])

    def test_arbre_verifie_recopie_dans_le_rapport(self):
        tree = {"head": A, "snapshot": B, "plan": "plan.json"}
        rapport = self.sel.proof(lot=7, host="vm", run={"outcome": "green", "exit": 0},
                                 flow=self.flow, tree=tree)
        self.assertEqual(rapport["tree"], tree)

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

    def test_argv_porte_le_snapshot_construit(self):
        """Le contenu envoyé est figé par son identité : pas de photographie."""
        argv = pv.offload_argv("vm-agents-1", "echo ok", snapshot=B)
        self.assertEqual(argv[argv.index("--snapshot") + 1], B)
        self.assertLess(argv.index("--snapshot"), argv.index("--"))

    def test_argv_porte_le_profil_du_guest(self):
        """Le profil du guest porte les dépendances et la base du projet."""
        argv = pv.offload_argv("vm-agents-1", "echo ok", profile="ameesh")
        self.assertEqual(argv[argv.index("--profile") + 1], "ameesh")
        # sans profil, l'offload garde le profil par défaut de l'hôte
        self.assertNotIn("--profile", pv.offload_argv("vm-agents-1", "echo ok"))

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

    def test_script_distant_utilise_le_venv_du_profil(self):
        """Le profil du guest pose `AMEESH_TEST_VENV` (contrat L159b) : les
        parts tournent avec ce venv (psycopg, dépendances), jamais avec le
        python système nu du guest."""
        sel = pv.selection(plan_sain(), head=A)
        script = pv.remote_script(sel.plan_path or "plan.json", sel.parts)
        self.assertIn('AMEESH_TEST_VENV', script)
        self.assertIn('"$py" ' + pv.DEFAULT_PARTS_SCRIPT + " run --plan", script)
        self.assertIn('[ -n "$py" ] || py=python3', script)

    def test_script_distant_interpreteur_force(self):
        """Un interpréteur explicite reste possible (tests, autre projet)."""
        sel = pv.selection(plan_sain(), head=A)
        script = pv.remote_script(sel.plan_path or "plan.json", sel.parts,
                                  python="/opt/projet/bin/python3")
        self.assertIn("/opt/projet/bin/python3 " + pv.DEFAULT_PARTS_SCRIPT + " run --plan",
                      script)
        self.assertNotIn("AMEESH_TEST_VENV", script)

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
        # #2298 : une préemption par la porte n'est pas une VM absente
        self.assertEqual(pv.classify(75, "ameesh offload: stopped on vm-agents-1: host "
                                         "stopped: maintenance; no fallback (--fallback none)"),
                         "gate-preempted")
        self.assertEqual(pv.classify(75, "ameesh offload: stopped on vm-agents-1: host "
                                         "draining: over deadline; no fallback (--fallback none)"),
                         "gate-preempted")
        # #4008 : un job refusé par la porte AVANT son départ est aussi une
        # préemption, pas une VM absente.
        self.assertEqual(pv.classify(75, "ameesh offload: refused on ameesh-agents-1: "
                                         "host stopped: app_cpu; no fallback (--fallback none)"),
                         "gate-preempted")
        self.assertEqual(pv.classify(75, "ameesh offload: refused on ameesh-agents-1: "
                                         "host draining: maintenance; no fallback (--fallback none)"),
                         "gate-preempted")
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

    def test_run_offload_duree_murale_sans_marque(self):
        """#2665 : sans durée lue, le repli est mesuré sur la même horloge
        monotone que le départ (jamais une date murale moins un instant monotone)."""
        class Fake:
            returncode = 0
            stdout = flux()
            stderr = ""

        result = pv.run_offload("vm-agents-1", "echo ok", runner=lambda *a, **k: Fake())
        self.assertGreaterEqual(result["seconds"], 0.0)
        self.assertLess(result["seconds"], 60.0)

    def test_run_offload_preempte_par_la_porte(self):
        """#2298 : la note « stopped on … host draining » sort en gate-preempted."""
        class Fake:
            returncode = 75
            stdout = "%s2103\n" % pv.SECONDS_MARK
            stderr = ("ameesh offload: stopped on vm-agents-1: host draining: over "
                      "deadline; no fallback (--fallback none)\n")

        result = pv.run_offload("vm-agents-1", "echo coupe", runner=lambda *a, **k: Fake())
        self.assertEqual(result["outcome"], "gate-preempted")
        self.assertEqual(result["exit"], 75)
        self.assertEqual(result["seconds"], 2103.0)

    def test_run_offload_preempte_sans_saut_final_stdout(self):
        """La note stderr garde sa ligne après un stdout sans saut final."""
        class Fake:
            returncode = 75
            stdout = flux()
            stderr = ("ameesh offload: stopped on vm-agents-1: host draining: over "
                      "deadline; no fallback (--fallback none)\n")

        result = pv.run_offload("vm-agents-1", "echo coupe", runner=lambda *a, **k: Fake())
        self.assertEqual(result["outcome"], "gate-preempted")
        self.assertEqual(result["flow"]["fins"], ("succes",))

    def test_run_offload_borne_l_attente_locale(self):
        # #2665/B1 cx2 : l'échéance du passage atteint le runner réel, pas
        # seulement le timeout du job distant ; le client reçoit un
        # --wait-timeout pour annuler son job avant le garde-fou local.
        seen = {}

        def runner(argv, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            seen["wait_timeout"] = argv[argv.index("--wait-timeout") + 1]
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        result = pv.run_offload("vm-agents-1", "echo", timeout=120,
                                deadline=pv.Deadline(30.0), runner=runner)
        self.assertEqual(result["outcome"], "run-incomplete")
        self.assertEqual(result["exit"], 124)
        self.assertGreater(seen["timeout"], 30.0)
        self.assertLessEqual(seen["timeout"], 30.0 + pv.OFFLOAD_LOCAL_GRACE_S)
        self.assertGreater(int(seen["wait_timeout"]), 0)

    def test_run_offload_attente_locale_reellement_tuee(self):
        # Le vrai subprocess.run est le juge : une commande qui dort plus que
        # le budget local est tuée et rend un run-incomplete, pas un blocage.
        started = time.monotonic()

        def runner(argv, **kwargs):
            return subprocess.run(["sleep", "5"], timeout=kwargs.get("timeout"),
                                  capture_output=True, text=True)

        with mock.patch.object(pv, "OFFLOAD_LOCAL_GRACE_S", 0.1):
            result = pv.run_offload("vm-agents-1", "echo", deadline=pv.Deadline(0.05),
                                    runner=runner)
        self.assertEqual(result["outcome"], "run-incomplete")
        self.assertEqual(result["exit"], 124)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIsNotNone(result["seconds"])

    def test_pinned_snapshot_budget_expired_is_run_incomplete(self):
        # #2665/B1 cx2 : la construction du snapshot (git) est bornée ; une
        # échéance épuisée est une indisponibilité, pas une sélection fausse.
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(pv.PrevalidationError) as caught:
                pv.pinned_snapshot(root, A, "plan.json", "{}",
                                   deadline=pv.Deadline(0.0))
        self.assertEqual(caught.exception.cause, "run-incomplete")


class SnapshotIdentityTest(unittest.TestCase):
    """Le contenu envoyé est **construit**, pas photographié (P1 cx1).

    Repro cx1, vrai git : un plan non suivi, un fichier non suivi, puis un
    fichier suivi marqué `assume-unchanged` et modifié — `git status` le dit
    propre alors que `offload.snapshot()` remet le flag et embarque le contenu
    modifié. Le geste fige donc l'arbre « SHA candidat + plan relu » par son
    identité (`offload.snapshot_with`) et la passe à l'offload (`--snapshot`)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self._git("init", "-q")
        self._git("config", "user.email", "tests@ameesh.invalid")
        self._git("config", "user.name", "tests")
        self._write("service.py", "VERSION = 1\n")
        self._git("add", "service.py")
        self._git("commit", "-q", "-m", "init")
        self.head = self._git("rev-parse", "HEAD")
        self.plan_rel = "plan.json"
        self.plan = '{"schema": "ameesh-test-plan/1"}\n'
        self._write(self.plan_rel, self.plan)

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _write(self, name: str, text: str) -> None:
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _snapshot_files(self, snap: str) -> list[str]:
        return self._git("ls-tree", "-r", "--name-only", snap).splitlines()

    def test_arbre_propre_meme_identite_que_la_photographie(self):
        """La formule d'identité est celle de l'offload : un arbre propre ne
        change pas d'un cheveu entre `snapshot_with` et `snapshot`."""
        construit = pv.pinned_snapshot(self.repo, self.head, self.plan_rel, self.plan)
        self.assertEqual(construit, offload.snapshot(self.repo))

    def test_contenu_envoye_est_le_sha_plus_le_plan(self):
        self._write("runtime_config.py", "X = 2\n")
        self._git("update-index", "--assume-unchanged", "service.py")
        self._write("service.py", "VERSION = 999\n")
        construit = pv.pinned_snapshot(self.repo, self.head, self.plan_rel, self.plan)
        # la photographie de l'arbre de travail, elle, embarque le fichier
        # modifié et le non-suivi : c'est exactement ce que la preuve refuse
        self.assertNotEqual(construit, offload.snapshot(self.repo))
        self.assertEqual(self._snapshot_files(construit), ["plan.json", "service.py"])
        self.assertEqual(self._git("show", "%s:service.py" % construit), "VERSION = 1")

    def test_flag_assume_unchanged_ne_trompe_pas_le_snapshot(self):
        """`skip-worktree`/`assume-unchanged` : le statut ment, l'identité non."""
        self._git("update-index", "--assume-unchanged", "service.py")
        self._write("service.py", "VERSION = 999\n")
        construit = pv.pinned_snapshot(self.repo, self.head, self.plan_rel, self.plan)
        self.assertEqual(self._git("show", "%s:service.py" % construit), "VERSION = 1")

    def test_plan_modifie_apres_lecture_refuse(self):
        """Le plan prouvé est le texte relu : si le fichier change ensuite, la
        construction porte le texte relu, pas la version du disque."""
        construit = pv.pinned_snapshot(self.repo, self.head, self.plan_rel, self.plan)
        self._write(self.plan_rel, '{"autre": true}\n')
        # `git show` rend le blanc de fin rogné par `_git` : on compare le texte
        # significatif, l'identité du blob restant celle du plan relu
        self.assertEqual(self._git("show", "%s:plan.json" % construit),
                         self.plan.strip())

    def test_arbre_du_sha_absent_refuse_sans_head_moved(self):
        """Un arbre non constructible est un refus de sélection (#1820) :
        `head-moved` ne dit que la tête qui a réellement bougé."""
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.pinned_snapshot(self.repo, B, self.plan_rel, self.plan)
        self.assertEqual(caught.exception.cause, "selection-unsafe")
        self.assertNotEqual(caught.exception.cause, "head-moved")


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

    def test_hote_local_declare_au_canon_est_une_vm(self):
        # Décision pilote 19/10 06:03 (#2644) : l'hôte local déclaré au canon
        # est une VM d'agents ; un poste local non déclaré reste refusé.
        ok, cause = pv.host_allowed("ameesh-agents-1", local="ameesh-agents-1",
                                    declared=("ameesh-agents-1",))
        self.assertTrue(ok, cause)
        ok, cause = pv.host_allowed("ameesh-agents-1", local="ameesh-agents-1",
                                    declared=("ameesh-agents-2",))
        self.assertFalse(ok)
        self.assertEqual(cause, "no-vm")


if __name__ == "__main__":
    unittest.main()
