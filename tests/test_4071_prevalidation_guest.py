# SPDX-License-Identifier: AGPL-3.0-only
"""#4071 — le guest ne joue pas les tests d'hôte, et le dit.

Le run réel #4044 (hôte agents-3, profil ameesh, 2 486 tests) a rendu 91
échecs, tous dans des modules que le guest ne peut pas exécuter (unités
systemd, HOME/UID du poste, canaux Unix courts, recette libpq, snapshot sans
histoire). Ces témoins bornés vérifient la liste déclarative, le filtrage du
plan envoyé, la preuve qui distingue un échec exclu d'un rouge réel, et le
fait que la suite complète n'est jamais filtrée.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import prevalidation as pv

from . import test_1770_prevalidation_cli as cli

A, B, C = cli.A, cli.B, cli.C
FAKE = "test_hote_factice"
PROFILE = "ameesh-test-4071"
REASON = "hôte factice : unité systemd et HOME du poste"


def plan_with_host() -> dict:
    """Le plan canonique, plus un module d'hôte factice dans la même part."""
    plan = cli.plan_canon()
    plan["selection"] = {"mode": "impacted", "why": "", "modules": 3,
                         "impacted": 3, "contract": 1, "selected": 3,
                         "skipped": 0}
    plan["classes"][FAKE] = ["P0", "test d'hôte factice"]
    plan["parts"]["psycopg"][0]["modules"].append(FAKE)
    return plan


def exclusions() -> tuple[dict, ...]:
    return ({"module": FAKE, "reason": REASON},)


def params(contracts=()) -> SimpleNamespace:
    return SimpleNamespace(classes=("P0", "P1"), contracts=tuple(contracts),
                           hosts=("vm1",), timeout_seconds=60)


def flux_with(failures=(), successes=("test_a", "test_cluster")) -> str:
    """Le flux d'une part : les succès nommés, puis les échecs nommés."""
    part = "psycopg:1"
    lignes = [pv.PART_MARK + part]
    for module in (*successes, *failures):
        statut = "echec" if module in failures else "succes"
        lignes.append(json.dumps({
            "v": 1, "schema": pv.FLOW_SCHEMA, "type": "test", "part": part,
            "module": module, "test": "tests.%s.C.test_x" % module,
            "statut": statut}))
    lignes.append(json.dumps({
        "v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin", "part": part,
        "statut": "echec" if failures else "succes", "duree_s": 1.0}))
    return "\n".join(lignes)


class GuestListTest(unittest.TestCase):
    def test_ameesh_profile_declares_the_host_modules_with_a_reason(self):
        """La liste est déclarative : module exact + raison, jamais un motif."""
        listed = pv.GUEST_EXCLUSIONS["ameesh"]
        for module, reason in listed.items():
            with self.subTest(module=module):
                self.assertRegex(module, r"^test_[a-z0-9_]+$")
                self.assertTrue(reason.strip(), module)
        # les modules du run réel #4044 qui ne peuvent pas tourner au guest
        self.assertTrue({
            "test_503_agent_host", "test_3114_host_wide_drain",
            "test_l93_device_state", "test_l112_host_gate",
            "test_approve", "test_653_db_connections",
            "test_l161f_plan", "test_1323_actif",
        } <= set(listed))

    def test_unknown_profile_has_no_exclusion(self):
        self.assertEqual(pv.guest_excluded("default"), ())
        self.assertEqual(pv.guest_excluded(""), ())

    def test_test_module_name_is_read_at_the_module_grain(self):
        self.assertEqual(pv.test_module("tests.test_a.C.test_x"), "test_a")
        self.assertEqual(pv.test_module("setUpClass (tests.test_a.C)"), "test_a")
        self.assertEqual(pv.test_module("tests.test_a"), "test_a")
        self.assertEqual(pv.test_module("autre.chose"), "")
        self.assertEqual(pv.test_module(""), "")


class FilterTest(unittest.TestCase):
    def test_host_module_is_removed_from_the_guest_plan_only(self):
        plan = plan_with_host()
        filtered, applied = pv.filter_guest_plan(plan, exclusions())
        self.assertEqual([item["module"] for item in applied], [FAKE])
        self.assertEqual(applied[0]["reason"], REASON)
        # retiré des parts, des classes et du bloc de sélection
        self.assertNotIn(FAKE, pv.planned_modules(filtered))
        self.assertNotIn(FAKE, filtered["classes"])
        self.assertEqual(filtered["selection"]["selected"], 2)
        self.assertEqual(filtered["selection"]["modules"], 2)
        self.assertEqual(filtered["guest_excluded"],
                         [{"module": FAKE, "reason": REASON}])
        # le plan d'entrée n'est jamais modifié : la CI le garde tel quel
        self.assertIn(FAKE, pv.planned_modules(plan))
        self.assertIn(FAKE, plan["classes"])

    def test_a_declared_module_absent_from_the_plan_is_not_reported(self):
        plan = cli.plan_canon()
        filtered, applied = pv.filter_guest_plan(plan, exclusions())
        self.assertEqual(applied, ())
        self.assertEqual(filtered, plan)

    def test_an_excluded_contract_is_not_required_by_the_canon_check(self):
        plan = plan_with_host()
        plan["contract"] = ["test_cluster", FAKE]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "contrats.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("test_cluster  # rôles du cluster\n%s  # hôte\n" % FAKE)
            filtered, applied = pv.filter_guest_plan(plan, exclusions())
            self.assertEqual([item["module"] for item in applied], [FAKE])
            self.assertEqual(filtered["contract"], ["test_cluster"])
            self.assertTrue(pv.meets_canon(filtered, params((path,)),
                                           excluded=exclusions())[0])
            # sans la déclaration d'exclusion, le contrat manque : refus
            ok, why = pv.meets_canon(filtered, params((path,)))
            self.assertFalse(ok)
            self.assertIn(FAKE, why)


class ProofTest(unittest.TestCase):
    def sel(self):
        return pv.selection(plan_with_host(), head=A, excluded=exclusions())

    def test_an_excluded_failure_is_not_a_real_red(self):
        report = self.sel().proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=pv.parse_flow(flux_with(failures=(FAKE,))))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["outcome"], "green")
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["excluded_failed"],
                         ["tests.%s.C.test_x" % FAKE])
        self.assertIn("exclu", " ".join(report["detail"]))
        self.assertEqual([item["module"] for item in report["excluded"]], [FAKE])

    def test_a_real_failure_next_to_an_excluded_one_stays_red(self):
        report = self.sel().proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=pv.parse_flow(flux_with(failures=(FAKE, "test_b"))))
        self.assertFalse(report["ok"])
        self.assertIn("run-red", report["causes"])
        self.assertEqual(report["failed"], ["tests.test_b.C.test_x"])
        self.assertEqual(report["excluded_failed"],
                         ["tests.%s.C.test_x" % FAKE])

    def test_a_red_without_any_named_test_is_not_hidden(self):
        report = self.sel().proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=pv.parse_flow(flux_with(successes=("test_a", "test_cluster"))))
        self.assertFalse(report["ok"])
        self.assertIn("run-red", report["causes"])

    def test_a_red_part_without_named_failure_stays_red_next_to_an_excluded_one(self):
        # part 1 : échec d'un module exclu ; part 2 : fin rouge sans test nommé
        # (crash, import) — elle ne se lit jamais comme verte.
        plan = plan_with_host()
        plan["parts"]["psycopg"][0]["of"] = 2
        plan["parts"]["psycopg"].append(
            {"part": 2, "of": 2, "modules": ["test_b"], "estimate_s": 10})
        plan["classes"]["test_b"] = ["P0", "b"]
        plan["selection"].update(selected=4, modules=4, impacted=4)
        chosen = pv.selection(plan, head=A, excluded=exclusions())
        self.assertTrue(chosen.ok, chosen.causes)
        part = "psycopg:2"
        flux = "\n".join((
            flux_with(failures=(FAKE,)), pv.PART_MARK + part,
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                        "part": part, "module": "test_b",
                        "test": "tests.test_b.C.test_y", "statut": "succes"}),
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                        "part": part, "statut": "echec", "duree_s": 1.0})))
        report = chosen.proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=pv.parse_flow(flux))
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["outcome"], "run-red")
        self.assertIn("parts rouges: psycopg:2", report["detail"])

    def multi_selection(self) -> pv.Selection:
        """Deux parts : les échecs exclus ne doivent jamais verdir l'autre."""
        return pv.Selection(
            head=A, modules=("test_real", "test_autre"),
            parts=(
                {"driver": "psycopg", "part": 1, "of": 2,
                 "modules": ("test_real",)},
                {"driver": "psycopg", "part": 2, "of": 2,
                 "modules": ("test_autre",)},
            ),
            excluded=exclusions())

    def test_all_parts_with_only_excluded_failures_are_green(self):
        lignes: list[str] = []
        for part, module in (("psycopg:1", "test_real"),
                             ("psycopg:2", "test_autre")):
            lignes += [pv.PART_MARK + part,
                       json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA,
                                   "type": "test", "part": part,
                                   "module": module,
                                   "test": "tests.%s.C.test_x" % module,
                                   "statut": "succes"}),
                       json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA,
                                   "type": "test", "part": part,
                                   "module": FAKE,
                                   "test": "tests.%s.C.test_%s"
                                           % (FAKE, part.replace(":", "_")),
                                   "statut": "echec"}),
                       json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA,
                                   "type": "fin", "part": part,
                                   "statut": "echec", "duree_s": 1.0})]
        report = self.multi_selection().proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=pv.parse_flow("\n".join(lignes)))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["outcome"], "green")
        self.assertEqual(len(report["excluded_failed"]), 2)

    def test_an_old_flow_without_failed_by_part_still_neutralises_an_excluded_failure(self):
        # compatibilité de la preuve : un flux construit sans failed_by_part
        # (bancs anciens) garde le repli global quand tous les échecs nommés
        # sont exclus.
        selection = pv.Selection(
            head=A, modules=("test_real",),
            parts=({"driver": "psycopg", "part": 1, "of": 1,
                    "modules": ("test_real",)},),
            excluded=exclusions())
        flow = {"modules": ("test_real", FAKE),
                "failed": ("tests.%s.C.test_x" % FAKE,),
                "fins_par_part": {"psycopg:1": "echec"},
                "parts": ("psycopg:1",), "tests": 1}
        report = selection.proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=flow)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["outcome"], "green")

    def test_a_missing_excluded_module_is_not_run_incomplete(self):
        # le plan n'a pas été filtré (appel direct), le module exclu est muet :
        # son absence n'est pas un module planifié non exécuté.
        report = self.sel().proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "green", "exit": 0, "seconds": 1.0},
            flow=pv.parse_flow(flux_with(successes=("test_a", "test_cluster"))))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["missing"], [])


    def test_an_excluded_failure_does_not_hide_a_real_environment_failure(self):
        # #4039 × #4071 : l'échec exclu ne compte pas dans la qualification
        # d'environnement, mais un vrai échec d'environnement reste non
        # concluant (run-incomplete), jamais vert.
        selection = pv.Selection(
            head=A, modules=("test_real",),
            parts=({"driver": "psycopg", "part": 1, "of": 1,
                    "modules": ("test_real",)},),
            excluded=exclusions())
        flux = "\n".join([
            pv.PART_MARK + "psycopg:1",
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                        "part": "psycopg:1", "module": "test_real",
                        "test": "tests.test_real.C.test_x", "statut": "echec",
                        "extrait": "Postgres injoignable (banc)"}),
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                        "part": "psycopg:1", "module": FAKE,
                        "test": "tests.%s.C.test_x" % FAKE,
                        "statut": "echec"}),
            json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                        "part": "psycopg:1", "statut": "echec",
                        "duree_s": 1.0}),
        ])
        report = selection.proof(
            lot=4071, host="ameesh-agents-3",
            run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=pv.parse_flow(flux))
        self.assertFalse(report["ok"], report)
        self.assertIn("run-incomplete", report["causes"])
        self.assertNotIn("run-red", report["causes"])
        self.assertEqual(report["failed"], ["tests.test_real.C.test_x"])
        self.assertEqual(report["excluded_failed"],
                         ["tests.%s.C.test_x" % FAKE])


class ValidateTest(unittest.TestCase):
    def test_validate_sends_the_filtered_plan_and_reports_the_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "plan.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(plan_with_host(), fh)
            seen = {}

            def snapshot(root, head, plan_rel, plan):
                seen["plan"] = plan
                return C

            with mock.patch.dict(pv.GUEST_EXCLUSIONS,
                                 {PROFILE: {FAKE: REASON}}):
                report = pv.validate(
                    lot=4071, plan_path=path, head=A, host="vm1",
                    params=params(), repo=directory, profile=PROFILE,
                    runner=cli.runner_for(0, cli.flux()),
                    snapshotter=snapshot,
                    git_runner=cli.git_runner_for())
        self.assertTrue(report["ok"], report)
        sent = json.loads(seen["plan"])
        self.assertNotIn(FAKE, pv.planned_modules(sent))
        self.assertNotIn(FAKE, sent["classes"])
        self.assertIn("test_a", pv.planned_modules(sent))
        self.assertEqual(sent["guest_excluded"],
                         [{"module": FAKE, "reason": REASON}])
        self.assertEqual([item["module"] for item in report["excluded"]], [FAKE])
        self.assertEqual(report["outcome"], "green")

    def test_another_profile_keeps_the_whole_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "plan.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(plan_with_host(), fh)
            seen = {}

            def snapshot(root, head, plan_rel, plan):
                seen["plan"] = plan
                return C

            report = pv.validate(
                lot=4071, plan_path=path, head=A, host="vm1",
                params=params(), repo=directory, profile="default",
                runner=cli.runner_for(
                    0, flux_with(successes=("test_a", "test_cluster", FAKE))),
                snapshotter=snapshot, git_runner=cli.git_runner_for())
        self.assertTrue(report["ok"], report)
        self.assertIn(FAKE, seen["plan"])
        self.assertEqual(report["excluded"], [])
