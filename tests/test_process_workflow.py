# SPDX-License-Identifier: AGPL-3.0-only
"""#1918 : compilateur de workflows Process (#1915 tranche 1) — refus et graphes.

Aucune base, aucun état natif : le module testé est une API pure. La fixture
générique a 14 étapes et une organisation fictive (« acme ») ; les gardes
viennent du registre fini du contrat consommateur (#1379).
"""
from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from ameesh.process_workflow import (CORE_STATES, SUPPORTED_SCHEMA, SUPPORTED_TYPES,
                                     TERMINAL_CORES, VERIFIER_NAMES, VERIFIERS,
                                     CompiledWorkflow, WorkflowRefusal,
                                     compile_project_workflow, compile_workflow,
                                     parse_workflow)

REF = "canon@f12867b7c7bb:processus/workflows/acme-14-etapes.json"


def fixture() -> dict:
    """L'exemple public à 14 étapes : statut Process -> cœur natif."""
    stages = [
        ("draft", "intake"), ("backlog", "intake"), ("sprint", "intake"),
        ("implementation", "build"),
        ("review", "qa"), ("tests", "qa"),
        ("staging", "merged"), ("staging_passed", "merged"),
        ("preprod", "merged"), ("preprod_passed", "merged"),
        ("staging_failed", "build"), ("preprod_failed", "build"),
        ("done", "promoted"), ("stopped", "closed"),
    ]
    transitions = [
        ("draft", "backlog", "planning"),
        ("backlog", "sprint", "planning"),
        ("sprint", "implementation", "start_work"),
        ("implementation", "review", "review_requested"),
        ("review", "tests", "qa_started"),
        ("tests", "staging", "merged_and_staging_deployed"),
        ("tests", "implementation", "qa_repair"),
        ("staging", "staging_passed", "staging_pass"),
        ("staging", "staging_failed", "staging_failure"),
        ("staging_passed", "preprod", "preprod_deployed"),
        ("preprod", "preprod_passed", "preprod_pass"),
        ("preprod", "preprod_failed", "preprod_failure"),
        ("preprod_passed", "done", "work_completed"),
        ("staging_failed", "implementation", "repair_started"),
        ("preprod_failed", "implementation", "repair_started"),
    ]
    for status, _core in stages:
        if status not in ("done", "stopped"):
            transitions.append((status, "stopped", "stop_work"))
    return {
        "schema": SUPPORTED_SCHEMA,
        "initial": "draft",
        "types": list(SUPPORTED_TYPES),
        "stages": [{"status": s, "core": c} for s, c in stages],
        "transitions": [{"from": a, "to": b, "guard": g} for a, b, g in transitions],
        "projection": {
            "project": "acme",
            "status_field": "Stage",
            "product_field": "Product",
            "issue_open": ["intake", "build", "qa", "merged"],
            "issue_completed": ["promoted"],
            "issue_not_planned": ["closed"],
        },
    }


class WorkflowTestCase(unittest.TestCase):
    def compile(self, document=None, *, ref=REF, version=1) -> CompiledWorkflow:
        if document is None:
            document = fixture()
        return compile_workflow(document, ref=ref, version=version)

    def assertRefusal(self, code, func, *args, **kwargs) -> WorkflowRefusal:
        with self.assertRaises(WorkflowRefusal) as caught:
            func(*args, **kwargs)
        self.assertEqual(code, caught.exception.code, str(caught.exception))
        return caught.exception


class FixtureTest(WorkflowTestCase):
    def test_exemple_14_etapes_accepte(self):
        artifact = self.compile()
        self.assertEqual(SUPPORTED_SCHEMA, artifact.schema)
        self.assertEqual("draft", artifact.initial)
        self.assertEqual(tuple(SUPPORTED_TYPES), artifact.types)
        self.assertEqual(14, len(artifact.stages))
        self.assertEqual(27, len(artifact.transitions))
        self.assertEqual("acme", artifact.projection.project)
        self.assertEqual(("intake", "build", "qa", "merged"),
                         artifact.projection.issue_open)
        self.assertEqual(("promoted",), artifact.projection.issue_completed)
        self.assertEqual(("closed",), artifact.projection.issue_not_planned)
        self.assertEqual(REF, artifact.workflow_ref)
        self.assertEqual(1, artifact.workflow_version)
        self.assertEqual({"intake", "build", "qa", "merged", "promoted", "closed"},
                         {stage.core for stage in artifact.stages})

    def test_registre_fini_des_verificateurs(self):
        self.assertEqual(14, len(VERIFIER_NAMES))
        self.assertEqual(frozenset(VERIFIER_NAMES), VERIFIERS)
        self.assertEqual(set(VERIFIER_NAMES), set(self.compile().guards()))
        self.assertEqual(14, len(self.compile().guards()))

    def test_exemple_en_texte_json_egal_a_l_objet(self):
        text = json.dumps(fixture(), ensure_ascii=False)
        self.assertEqual(self.compile(), self.compile(text))

    def test_exemple_immuable(self):
        document = fixture()
        before = json.dumps(document, sort_keys=True)
        first = self.compile(document)
        second = self.compile(document)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(before, json.dumps(document, sort_keys=True))
        self.assertIsInstance(first.stages, tuple)
        self.assertIsInstance(first.transitions, tuple)
        self.assertIsInstance(first.projection.issue_open, tuple)
        with self.assertRaises(FrozenInstanceError):
            first.initial = "done"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            first.stages[0].core = "build"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            first.projection.issue_open = ()  # type: ignore[misc]

    def test_revision_conservee_et_distincte(self):
        other = self.compile(ref=REF + "#2", version=2)
        self.assertNotEqual(self.compile(), other)
        self.assertEqual(REF + "#2", other.workflow_ref)
        self.assertEqual(2, other.workflow_version)

    def test_helpers_de_graphe(self):
        artifact = self.compile()
        self.assertEqual("build", artifact.core_of("staging_failed"))
        self.assertEqual("promoted", artifact.core_of("done"))
        self.assertEqual({"preprod_pass", "preprod_failure", "stop_work"},
                         {t.guard for t in artifact.transitions_from("preprod")})
        self.assertEqual(("done", "stopped"), artifact.statuses()[-2:])
        with self.assertRaises(KeyError):
            artifact.core_of("absent")

    def test_workflow_minimal_sans_arete_accepte(self):
        document = {
            "schema": SUPPORTED_SCHEMA,
            "initial": "draft",
            "types": ["docs"],
            "stages": [{"status": "draft", "core": "intake"}],
            "transitions": [],
            "projection": {"project": "acme", "status_field": "Stage",
                           "product_field": "Product", "issue_open": ["intake"],
                           "issue_completed": [], "issue_not_planned": []},
        }
        artifact = self.compile(document)
        self.assertEqual(1, len(artifact.stages))
        self.assertEqual((), artifact.transitions)

    def test_core_states_et_terminaux(self):
        self.assertEqual(("intake", "build", "qa", "merged", "promoted", "closed"),
                         CORE_STATES)
        self.assertEqual({"promoted": "completed", "closed": "not_planned"},
                         TERMINAL_CORES)


class LectureTest(WorkflowTestCase):
    def test_cle_dupliquee_au_premier_niveau_refusee(self):
        text = ('{"schema": "process-workflow/1",'
                ' "schema": "process-workflow/1"}')
        self.assertRefusal("key-duplicate", parse_workflow, text)

    def test_cle_dupliquee_imbriquee_refusee(self):
        text = '{"stages": [{"status": "draft", "core": "intake", "core": "build"}]}'
        self.assertRefusal("key-duplicate", parse_workflow, text)
        self.assertRefusal("key-duplicate", compile_workflow, text, ref=REF)

    def test_json_illisible_refuse(self):
        self.assertRefusal("json-invalid", parse_workflow, "{pas du json")
        self.assertRefusal("json-invalid", compile_workflow, "[", ref=REF)

    def test_texte_non_json_refuse(self):
        self.assertRefusal("value-type", parse_workflow, 42)
        self.assertRefusal("value-type", compile_workflow, 42, ref=REF)


class FormeTest(WorkflowTestCase):
    def test_cle_inconnue_refusee_a_chaque_niveau(self):
        cases = {
            "$.extra": lambda d: d.update({"extra": 1}),
            "$.stages[0].couleur": lambda d: d["stages"][0].update({"couleur": "rouge"}),
            "$.transitions[0].weight": lambda d: d["transitions"][0].update({"weight": 1}),
            "$.projection.epic": lambda d: d["projection"].update({"epic": "x"}),
        }
        for path, mutate in cases.items():
            with self.subTest(path=path):
                document = fixture()
                mutate(document)
                refusal = self.assertRefusal("key-unknown", self.compile, document)
                self.assertEqual(path, refusal.path)

    def test_cle_manquante_refusee(self):
        cases = {
            "schema": lambda d: d.pop("schema"),
            "initial": lambda d: d.pop("initial"),
            "stages[0].core": lambda d: d["stages"][0].pop("core"),
            "transitions[0].guard": lambda d: d["transitions"][0].pop("guard"),
            "projection.issue_open": lambda d: d["projection"].pop("issue_open"),
        }
        for path, mutate in cases.items():
            with self.subTest(path=path):
                document = fixture()
                mutate(document)
                refusal = self.assertRefusal("key-missing", self.compile, document)
                self.assertEqual("$." + path, refusal.path)

    def test_valeur_de_mauvais_type_refusee(self):
        cases = {
            "$": lambda d: 42,
            "$.stages": lambda d: d.update({"stages": {"draft": "intake"}}),
            "$.stages[0].status": lambda d: d["stages"][0].update({"status": 1}),
            "$.transitions[0]": lambda d: d.update({"transitions": ["arête"]}),
            "$.projection": lambda d: d.update({"projection": []}),
            "$.types": lambda d: d.update({"types": "bug"}),
        }
        for path, make in cases.items():
            with self.subTest(path=path):
                document = fixture()
                value = make(document)
                document = document if value is None else value
                refusal = self.assertRefusal("value-type", self.compile, document)
                self.assertEqual(path, refusal.path)

    def test_liste_vide_refusee(self):
        for path, mutate in (("$.stages", lambda d: d.update({"stages": []})),
                             ("$.types", lambda d: d.update({"types": []}))):
            with self.subTest(path=path):
                document = fixture()
                mutate(document)
                refusal = self.assertRefusal("value-empty", self.compile, document)
                self.assertEqual(path, refusal.path)

    def test_schema_non_pris_en_charge(self):
        document = fixture()
        document["schema"] = "process-workflow/2"
        self.assertRefusal("schema-unsupported", self.compile, document)

    def test_type_non_pris_en_charge_ou_duplique(self):
        document = fixture()
        document["types"] = ["bug", "inconnu"]
        self.assertRefusal("type-unsupported", self.compile, document)
        document = fixture()
        document["types"] = ["bug", "bug"]
        self.assertRefusal("type-duplicate", self.compile, document)

    def test_ref_et_version_invalides(self):
        for ref in ("", "   ", None, 12):
            with self.subTest(ref=ref):
                self.assertRefusal("ref-invalid", compile_workflow, fixture(), ref=ref)
        for version in (-1, True, 1.5, "1", None):
            with self.subTest(version=version):
                self.assertRefusal("version-invalid", compile_workflow, fixture(),
                                   ref=REF, version=version)


class GrapheTest(WorkflowTestCase):
    def test_etape_dupliquee_refusee(self):
        document = fixture()
        document["stages"].append({"status": "draft", "core": "build"})
        self.assertRefusal("stage-duplicate", self.compile, document)

    def test_etape_avec_espaces_en_bord_refusee(self):
        for status in (" draft", "draft ", "\tdraft"):
            with self.subTest(status=status):
                document = fixture()
                document["stages"][0]["status"] = status
                self.assertRefusal("stage-padded", self.compile, document)

    def test_etape_dupliquee_a_la_casse_pres_refusee(self):
        for status in ("Draft", "DRAFT", "dRaFt"):
            with self.subTest(status=status):
                document = fixture()
                document["stages"].append({"status": status, "core": "build"})
                self.assertRefusal("stage-case-duplicate", self.compile, document)

    def test_core_inconnu_refuse(self):
        for core in ("blocked", "waiting_human", "inconnu"):
            with self.subTest(core=core):
                document = fixture()
                document["stages"][0]["core"] = core
                self.assertRefusal("core-unknown", self.compile, document)

    def test_initial_non_declare(self):
        document = fixture()
        document["initial"] = "absent"
        self.assertRefusal("initial-undeclared", self.compile, document)

    def test_arete_dupliquee_refusee(self):
        document = fixture()
        document["transitions"].append({"from": "draft", "to": "backlog",
                                        "guard": "planning"})
        self.assertRefusal("edge-duplicate", self.compile, document)

    def test_arete_ambigue_refusee(self):
        document = fixture()
        document["transitions"].append({"from": "draft", "to": "sprint",
                                        "guard": "planning"})
        self.assertRefusal("edge-ambiguous", self.compile, document)

    def test_arete_vers_etape_non_declaree_refusee(self):
        document = fixture()
        document["transitions"].append({"from": "draft", "to": "fantome",
                                        "guard": "planning"})
        self.assertRefusal("edge-undeclared", self.compile, document)
        document = fixture()
        document["transitions"].append({"from": "fantome", "to": "draft",
                                        "guard": "planning"})
        self.assertRefusal("edge-undeclared", self.compile, document)

    def test_garde_hors_registre_refusee(self):
        document = fixture()
        document["transitions"].append({"from": "draft", "to": "sprint",
                                        "guard": "approve_everything"})
        self.assertRefusal("guard-unknown", self.compile, document)

    def test_etape_inaccessible_refusee(self):
        document = fixture()
        document["stages"].append({"status": "orpheline", "core": "qa"})
        refusal = self.assertRefusal("stage-unreachable", self.compile, document)
        self.assertIn("orpheline", refusal.message)

    def test_reouverture_terminale_refusee(self):
        for terminal in ("done", "stopped"):
            with self.subTest(terminal=terminal):
                document = fixture()
                document["transitions"].append({"from": terminal, "to": "implementation",
                                                "guard": "start_work"})
                self.assertRefusal("terminal-reopening", self.compile, document)


class ProjectionTest(WorkflowTestCase):
    def test_classification_recouvrante_refusee(self):
        document = fixture()
        document["projection"]["issue_completed"] = ["promoted", "closed"]
        self.assertRefusal("classification-overlap", self.compile, document)
        document = fixture()
        document["projection"]["issue_open"] = ["intake", "build", "qa", "merged",
                                                "promoted"]
        self.assertRefusal("classification-overlap", self.compile, document)

    def test_classification_incomplete_refusee(self):
        document = fixture()
        document["projection"]["issue_open"] = ["intake", "build", "qa"]
        refusal = self.assertRefusal("classification-incomplete", self.compile, document)
        self.assertIn("merged", refusal.message)

    def test_classification_incoherente_refusee(self):
        document = fixture()
        document["projection"]["issue_open"] = ["intake", "build", "qa"]
        document["projection"]["issue_completed"] = ["merged"]
        self.assertRefusal("classification-mismatch", self.compile, document)
        document = fixture()
        document["projection"]["issue_completed"] = ["promoted", "closed"]
        document["projection"]["issue_not_planned"] = []
        document["projection"]["issue_open"] = ["intake", "build", "qa", "merged"]
        self.assertRefusal("classification-mismatch", self.compile, document)

    def test_coeur_classe_sans_etape_refuse(self):
        document = fixture()
        document["stages"] = [{"status": "draft", "core": "intake"}]
        document["transitions"] = []
        document["projection"] = {"project": "acme", "status_field": "Stage",
                                  "product_field": "Product",
                                  "issue_open": ["intake"],
                                  "issue_completed": ["promoted"],
                                  "issue_not_planned": []}
        refusal = self.assertRefusal("classification-mismatch", self.compile, document)
        self.assertIn("promoted", refusal.message)

    def test_champs_de_projection_confondus_refuses(self):
        document = fixture()
        document["projection"]["product_field"] = document["projection"]["status_field"]
        self.assertRefusal("projection-conflict", self.compile, document)


class OptInTest(WorkflowTestCase):
    def test_projet_sans_workflow_inchange(self):
        self.assertIsNone(compile_project_workflow(None, ref=REF))
        self.assertIsNone(compile_project_workflow("# Process en prose seule", ref=REF))
        self.assertIsNone(compile_project_workflow({"titre": "acme"}, ref=REF))
        self.assertIsNone(compile_project_workflow({"workflow": "prose"}, ref=REF))

    def test_projet_avec_workflow_compile(self):
        project = {"titre": "acme", "workflow": fixture()}
        before = json.dumps(project, sort_keys=True)
        artifact = compile_project_workflow(project, ref=REF, version=3)
        self.assertIsInstance(artifact, CompiledWorkflow)
        self.assertEqual(14, len(artifact.stages))
        self.assertEqual(3, artifact.workflow_version)
        self.assertEqual(before, json.dumps(project, sort_keys=True))

    def test_projet_avec_workflow_invalide_refuse(self):
        project = {"workflow": {"schema": SUPPORTED_SCHEMA}}
        self.assertRefusal("key-missing", compile_project_workflow, project, ref=REF)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
