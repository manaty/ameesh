# SPDX-License-Identifier: AGPL-3.0-only
"""#2225 : vérification d'une carte Process (`processus/` -> workflow natif).

Le module testé est un pont de données : lecture du frontmatter par le
lecteur du canon, encodage de la forme canon (étapes en table, transitions
groupées, libellés, alias de types) vers `process-workflow/1`, puis
compilation stricte. Aucune base, aucun état natif, aucune exécution.
"""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import contextlib
import io
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from ameesh.process_card import (CheckResult, TYPE_ALIASES, artifact_document,
                                 check_page, encode_canon_workflow, main,
                                 result_dict, workflow_of_page)
from ameesh.process_workflow import (SUPPORTED_SCHEMA, WorkflowRefusal,
                                     compile_workflow)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "processus" / "acme-work-delivery.md"

BASE = """
schema: acme-workflow/v1
initial: draft
types: [bug, evolution, docs]
stages:
  draft: {core: intake, status: Draft}
  implementation: {core: build, status: In Progress}
  review: {core: qa, status: Dev review}
  done: {core: promoted, status: Done}
  stopped: {core: closed, status: Stopped}
transitions:
  - {from: [draft], to: implementation, guard: start_work}
  - {from: [implementation], to: review, guard: review_requested}
  - {from: [review], to: done, guard: work_completed}
  - {from: [draft, implementation, review], to: stopped, guard: stop_work}
projection:
  project: acme
  status_field: Stage
  product_field: Product
  issue_open: [draft, implementation, review]
  issue_completed: [done]
  issue_not_planned: [stopped]
"""


def page(workflow: str = BASE, *, type_: str = "Process") -> str:
    """Une carte Markdown : frontmatter YAML + corps de prose."""
    return ("---\ntype: %s\ntitle: acme-work-delivery\n"
            "responsible: human:alice\nupdated: 2026-10-21\n"
            "workflow:\n%s\n---\n\n# acme work delivery\n\nRègles en prose.\n"
            % (type_, textwrap.indent(workflow.strip("\n"), "  ")))


class PageCase(unittest.TestCase):
    def check(self, text: str, **kwargs) -> CheckResult:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "acme-work-delivery.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return check_page(path, **kwargs)

    def refusal(self, code: str, text: str, **kwargs) -> WorkflowRefusal:
        with self.assertRaises(WorkflowRefusal) as caught:
            self.check(text, **kwargs)
        self.assertEqual(code, caught.exception.code, str(caught.exception))
        return caught.exception


class ExampleTest(PageCase):
    def test_l_exemple_du_canon_compile(self):
        result = check_page(str(EXAMPLE))
        self.assertTrue(result.has_workflow)
        artifact = result.artifact
        self.assertEqual(8, len(artifact.stages))
        self.assertEqual(20, len(artifact.transitions))
        self.assertEqual(7, len(artifact.types))
        self.assertEqual("draft", artifact.initial)
        self.assertEqual(("intake", "build", "qa"),
                         artifact.projection.issue_open)
        self.assertEqual(("promoted",), artifact.projection.issue_completed)
        self.assertEqual(("closed",), artifact.projection.issue_not_planned)
        self.assertEqual("Draft", result.encoding.stage_labels["draft"])
        self.assertEqual({"governance": "governance-tracking"},
                         result.encoding.type_aliases)
        self.assertIn("governance-tracking", artifact.types)

    def test_deux_choix_planning_conserves(self):
        artifact = check_page(str(EXAMPLE)).artifact
        planning = [transition for transition in artifact.transitions
                   if transition.source == "draft"
                   and transition.guard == "planning"]
        self.assertEqual({"backlog", "sprint"},
                         {transition.target for transition in planning})

    def test_l_artefact_est_le_document_encode(self):
        result = check_page(str(EXAMPLE))
        self.assertEqual(artifact_document(result.artifact),
                         result.encoding.document)


class EncodeTest(PageCase):
    def test_transitions_groupees_developpees(self):
        result = self.check(page())
        encoded = result.encoding.document["transitions"]
        self.assertIn({"from": "draft", "to": "stopped", "guard": "stop_work"},
                      encoded)
        self.assertIn({"from": "review", "to": "stopped", "guard": "stop_work"},
                      encoded)
        self.assertEqual(6, len(encoded))

    def test_deux_cibles_sous_la_meme_garde(self):
        workflow = BASE.replace(
            "  - {from: [draft], to: implementation, guard: start_work}\n",
            "  - {from: [draft], to: implementation, guard: planning}\n"
            "  - {from: [draft], to: review, guard: planning}\n")
        result = self.check(page(workflow))
        targets = {transition.target for transition in result.artifact.transitions
                   if transition.source == "draft" and transition.guard == "planning"}
        self.assertEqual({"implementation", "review"}, targets)

    def test_doublon_exact_refuse(self):
        workflow = BASE.replace(
            "  - {from: [implementation], to: review, guard: review_requested}\n",
            "  - {from: [implementation], to: review, guard: review_requested}\n"
            "  - {from: [implementation], to: review, guard: review_requested}\n")
        self.refusal("edge-duplicate", page(workflow))

    def test_libelle_duplique_refuse(self):
        workflow = BASE.replace(
            "implementation: {core: build, status: In Progress}",
            "implementation: {core: build, status: Draft}")
        self.refusal("stage-label-duplicate", page(workflow))

    def test_alias_de_type_en_collision_refuse(self):
        workflow = BASE.replace(
            "types: [bug, evolution, docs]",
            "types: [governance, governance-tracking]")
        self.refusal("type-duplicate", page(workflow))

    def test_alias_de_type_applique(self):
        workflow = BASE.replace("types: [bug, evolution, docs]",
                                "types: [bug, governance, human_operation]")
        result = self.check(page(workflow))
        self.assertEqual(("bug", "governance-tracking", "human_operation"),
                         result.artifact.types)

    def test_etape_inconnue_dans_la_projection_refusee(self):
        workflow = BASE.replace("issue_open: [draft, implementation, review]",
                                "issue_open: [draft, implementation, review, ghost]")
        self.refusal("classification-mismatch", page(workflow))

    def test_etape_non_classee_refusee(self):
        workflow = BASE.replace("issue_open: [draft, implementation, review]",
                                "issue_open: [draft, implementation]")
        self.refusal("classification-incomplete", page(workflow))

    def test_core_dans_deux_groupes_refuse(self):
        workflow = BASE.replace(
            "  review: {core: qa, status: Dev review}\n",
            "  review: {core: build, status: Dev review}\n").replace(
            "issue_open: [draft, implementation, review]",
            "issue_open: [draft, implementation]").replace(
            "issue_completed: [done]",
            "issue_completed: [done, review]")
        self.refusal("classification-core-conflict", page(workflow))

    def test_cle_inconnue_refusee(self):
        workflow = BASE.replace("types: [bug, evolution, docs]",
                                "types: [bug, evolution, docs]\nunexpected: 1")
        self.refusal("key-unknown", page(workflow))

    def test_cle_manquante_refusee(self):
        workflow = BASE.replace(
            "  project: acme\n", "")
        self.refusal("key-missing", page(workflow))

    def test_schema_vide_refuse(self):
        workflow = BASE.replace("schema: acme-workflow/v1", "schema: ''")
        self.refusal("value-empty", page(workflow))

    def test_type_hors_registre_refuse(self):
        workflow = BASE.replace("types: [bug, evolution, docs]",
                                "types: [bug, evolution, improvement]")
        self.refusal("type-unsupported", page(workflow))

    def test_garde_inconnue_refusee(self):
        workflow = BASE.replace("guard: start_work", "guard: invented")
        self.refusal("guard-unknown", page(workflow))

    def test_etape_inconnue_dans_une_arete_refusee(self):
        workflow = BASE.replace("to: implementation", "to: ghost")
        self.refusal("edge-undeclared", page(workflow))


class PageTest(PageCase):
    def test_carte_en_prose_sans_workflow(self):
        text = ("---\ntype: Process\ntitle: prose\n"
                "responsible: human:alice\nupdated: 2026-10-21\n---\n\n"
                "# prose\n\nAucune donnée exécutable.\n")
        result = self.check(text)
        self.assertFalse(result.has_workflow)
        self.assertIsNone(result.artifact)

    def test_workflow_en_prose_ignore(self):
        text = ("---\ntype: Process\ntitle: prose\n"
                "responsible: human:alice\nupdated: 2026-10-21\n"
                "workflow: \"draft -> done\"\n---\n\n# prose\n")
        self.assertIsNone(workflow_of_page(text))

    def test_frontmatter_absent_refuse(self):
        self.refusal("frontmatter-missing", "# prose seule\n")

    def test_type_non_process_refuse(self):
        self.refusal("value-type", page(type_="Decision"))

    def test_workflow_non_objet_refuse(self):
        text = ("---\ntype: Process\ntitle: x\n"
                "responsible: human:alice\nupdated: 2026-10-21\n"
                "workflow: [draft, done]\n---\n")
        self.refusal("value-type", text)

    def test_yaml_invalide_refuse(self):
        text = ("---\ntype: Process\ntitle: x\n"
                "responsible: human:alice\nupdated: 2026-10-21\n"
                "workflow: {schema: [unclosed\n---\n")
        self.refusal("frontmatter-invalid", text)

    def test_forme_native_acceptee(self):
        native = """
schema: process-workflow/1
initial: draft
types: [bug]
stages:
  - {status: draft, core: intake}
  - {status: done, core: promoted}
  - {status: stopped, core: closed}
transitions:
  - {from: draft, to: done, guard: work_completed}
  - {from: draft, to: stopped, guard: stop_work}
projection:
  project: acme
  status_field: Stage
  product_field: Product
  issue_open: [intake]
  issue_completed: [promoted]
  issue_not_planned: [closed]
"""
        result = self.check(page(native))
        self.assertTrue(result.has_workflow)
        self.assertIsNone(result.encoding)
        self.assertEqual(3, len(result.artifact.stages))

    def test_version_invalide_refusee(self):
        with self.assertRaises(WorkflowRefusal) as caught:
            self.check(page(), version=-1)
        self.assertEqual("version-invalid", caught.exception.code)

    def test_ref_vide_refuse(self):
        with self.assertRaises(WorkflowRefusal) as caught:
            self.check(page(), ref="")
        self.assertEqual("ref-invalid", caught.exception.code)


class CliTest(PageCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_route_ameesh_process(self):
        from ameesh.main import main as ameesh_main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = isolated_entrypoint(ameesh_main, ["process", "check", str(EXAMPLE)])
        self.assertEqual(0, code, err.getvalue())
        self.assertIn("OK — 8 stages", out.getvalue())

    def test_check_reussi(self):
        code, out, err = self.run_main(["check", str(EXAMPLE)])
        self.assertEqual(0, code, err)
        self.assertIn("OK — 8 stages, 20 transitions, 7 types", out)
        self.assertIn('draft/intake "Draft"', out)

    def test_check_json_contient_le_document_natif(self):
        code, out, err = self.run_main(["check", str(EXAMPLE), "--json"])
        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertEqual("process-workflow/1", payload["document"]["schema"])
        self.assertEqual("governance-tracking", payload["document"]["types"][4])
        self.assertEqual({"draft": "Draft"}, {"draft": payload["labels"]["draft"]})

    def test_carte_en_prose_sort_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prose.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("---\ntype: Process\ntitle: prose\n"
                             "responsible: human:alice\nupdated: 2026-10-21\n---\n")
            code, out, _ = self.run_main(["check", path])
        self.assertEqual(0, code)
        self.assertIn("nothing to compile", out)

    def test_refus_sort_un(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(page(BASE.replace("initial: draft", "initial: ghost")))
            code, _, err = self.run_main(["check", path])
        self.assertEqual(1, code)
        self.assertIn("initial-undeclared", err)

    def test_fichier_absent_sort_deux(self):
        code, _, err = self.run_main(["check", os.path.join(ROOT, "absent-2225.md")])
        self.assertEqual(2, code)
        self.assertIn("cannot read", err)


class ReferenceShapeTest(unittest.TestCase):
    def test_les_groupes_equivalents_a_la_forme_native(self):
        """La carte de référence #1379 (14 étapes) se groupe sans perte."""
        from .test_process_workflow import fixture

        native = fixture()
        grouped = dict(native)
        grouped["stages"] = {
            stage["status"]: {"core": stage["core"], "status": stage["status"]}
            for stage in native["stages"]}
        sources = {}
        for transition in native["transitions"]:
            sources.setdefault((transition["to"], transition["guard"]), []).append(
                transition["from"])
        grouped["transitions"] = [
            {"from": values, "to": target, "guard": guard}
            for (target, guard), values in sources.items()]
        by_core = {}
        for stage in native["stages"]:
            by_core.setdefault(stage["core"], []).append(stage["status"])
        grouped["projection"] = dict(native["projection"])
        for group in ("issue_open", "issue_completed", "issue_not_planned"):
            grouped["projection"][group] = [
                stage for core in native["projection"][group]
                for stage in by_core[core]]

        encoded = encode_canon_workflow(grouped)
        self.assertEqual(SUPPORTED_SCHEMA, encoded.document["schema"])
        self.assertEqual(native["initial"], encoded.document["initial"])
        self.assertEqual(native["types"], encoded.document["types"])
        self.assertEqual(native["stages"], encoded.document["stages"])
        self.assertEqual(
            sorted((edge["from"], edge["to"], edge["guard"])
                   for edge in native["transitions"]),
            sorted((edge["from"], edge["to"], edge["guard"])
                   for edge in encoded.document["transitions"]))
        self.assertEqual(native["projection"], encoded.document["projection"])


class ConstantsTest(unittest.TestCase):
    def test_alias_documente(self):
        self.assertEqual("governance-tracking", TYPE_ALIASES["governance"])

    def test_compilation_directe_du_document_encode(self):
        encoded = encode_canon_workflow({
            "schema": "acme-workflow/v1",
            "initial": "draft",
            "types": ["bug"],
            "stages": {"draft": {"core": "intake", "status": "Draft"},
                       "done": {"core": "promoted", "status": "Done"},
                       "stopped": {"core": "closed", "status": "Stopped"}},
            "transitions": [{"from": ["draft"], "to": "done",
                             "guard": "work_completed"},
                            {"from": ["draft"], "to": "stopped",
                             "guard": "stop_work"}],
            "projection": {"project": "acme", "status_field": "Stage",
                           "product_field": "Product",
                           "issue_open": ["draft"], "issue_completed": ["done"],
                           "issue_not_planned": ["stopped"]},
        })
        artifact = compile_workflow(encoded.document, ref="acme@abc:processus/x.md",
                                    version=3)
        self.assertEqual("acme@abc:processus/x.md", artifact.workflow_ref)
        self.assertEqual(3, artifact.workflow_version)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
