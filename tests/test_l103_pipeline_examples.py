# SPDX-License-Identifier: AGPL-3.0-only
"""L103 T4 (#4398): the two shipped Pipeline examples, end to end.

The acme card is the development pipeline converted from the Process witness
(`examples/processus/acme-work-delivery.md`, left untouched). The support card
is a non-development, after-sales pipeline. Both are plain canon data:
`canon check` compiles them, the module registry exposes them as declarative
modules of source ``canon``, and `ameesh module <name>` adopts and drives a
lot on PostgreSQL. The issue projection is the pure
`process_projection.project_issue`, never `plan_github`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.support import PgTestCase
from ameesh import canon, modules, pipeline_cli, process_card, process_projection
from ameesh import process_registry, process_workflow, work
from ameesh.modules import registry
from ameesh.modules.storage import ModuleStore
from ameesh.db import DbError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CANON = ROOT / "examples" / "canon"
EXAMPLES = ROOT / "examples" / "pipelines"
PROCESS_WITNESS = ROOT / "examples" / "processus" / "acme-work-delivery.md"
ACME = "acme-work-delivery"
SUPPORT = "support-work-delivery"


def copie_canon(destination: Path) -> Path:
    """The example canon plus the shipped Pipeline cards, ready to read."""
    shutil.copytree(EXAMPLE_CANON, destination)
    shutil.copytree(EXAMPLES, destination / "pipelines")
    return destination


#: The activation a reader writes (guide, section 5): each card for its
#: project, and the built-in `work-types` module for support's non-code types.
FEDERATION = """\
members: []
extensions:
  ameesh:
    projects:
      acme:
        modules: [acme-work-delivery]
      support:
        modules: [support-work-delivery, work-types]
"""


def sans_bloc(texte: str, cle: str = "pipeline") -> str:
    """The card without its top-level block (its indented lines included)."""
    lines, out, dropping = texte.split("\n"), [], False
    for line in lines:
        if line == cle + ":":
            dropping = True
            continue
        if dropping and (line.startswith("  ") or not line.strip()):
            continue
        dropping = False
        out.append(line)
    return "\n".join(out)


class PipelineExamplesCanonTest(unittest.TestCase):
    """The shipped cards are valid canon data with the expected shape."""

    def charge(self) -> tuple[canon.Canon, list]:
        with tempfile.TemporaryDirectory() as tmp:
            root = copie_canon(Path(tmp) / "acme")
            loaded = canon.load(str(root), untrusted=True)
            return loaded, canon.validate(loaded)

    def test_the_two_cards_compile_with_the_expected_shape(self):
        loaded, found = self.charge()
        self.assertEqual(canon.errors(found), [])
        self.assertEqual([p.name for p in loaded.pipelines], [ACME, SUPPORT])
        acme = loaded.pipeline(ACME)
        self.assertTrue(acme.valid)
        self.assertEqual((len(acme.artifact.stages), len(acme.artifact.transitions),
                          len(acme.artifact.types), acme.artifact.initial),
                         (8, 20, 7, "draft"))
        self.assertEqual(("intake", "build", "qa"),
                         acme.artifact.projection.issue_open)
        support = loaded.pipeline(SUPPORT)
        self.assertTrue(support.valid)
        self.assertEqual((len(support.artifact.stages), len(support.artifact.transitions),
                          len(support.artifact.types), support.artifact.initial),
                         (6, 9, 4, "new"))
        self.assertEqual(("docs", "investigation", "governance-tracking",
                          "human_operation"), support.artifact.types)
        self.assertEqual(("intake", "build", "qa"),
                         support.artifact.projection.issue_open)
        self.assertEqual(("promoted",), support.artifact.projection.issue_completed)
        self.assertEqual(("closed",), support.artifact.projection.issue_not_planned)
        # `canon show --json` lists the cards, never as working rules.
        data = loaded.to_dict()
        self.assertEqual([item["name"] for item in data["pipelines"]], [ACME, SUPPORT])
        self.assertEqual(data["processes"], [])

    def test_the_cards_are_modules_of_source_canon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copie_canon(Path(tmp) / "acme")
            loaded = canon.load(str(root), untrusted=True)
            discovered = registry.discover(canon=loaded)
            implementations = modules.installed(loaded)
        for name in (ACME, SUPPORT):
            module = discovered.get(name)
            self.assertEqual((module.source, module.storage, module.capabilities,
                              module.cli), ("canon", (), ("lifecycle", "cli"),
                                             "ameesh module %s" % name))
            self.assertTrue(module.card_ref.startswith(
                "canon:pipelines/%s.md@" % name))
            self.assertEqual(implementations[name].descriptor({}),
                             {"types": [], "schemas": {}, "rules": []})

    def test_a_card_without_pipeline_block_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copie_canon(Path(tmp) / "acme")
            card = root / "pipelines" / "sans-bloc.md"
            texte = (EXAMPLES / (SUPPORT + ".md")).read_text(encoding="utf-8")
            texte = texte.replace("name: " + SUPPORT, "name: sans-bloc", 1)
            card.write_text(sans_bloc(texte), encoding="utf-8")
            loaded = canon.load(str(root), untrusted=True)
            found = canon.validate(loaded)
            self.assertIn("pipeline-invalid", {f.code for f in canon.errors(found)})
            self.assertFalse(loaded.pipeline("sans-bloc").valid)

    def test_the_process_witness_is_still_a_process_card(self):
        result = process_card.check_page(str(PROCESS_WITNESS))
        self.assertTrue(result.has_workflow)
        self.assertEqual((len(result.artifact.stages), len(result.artifact.transitions),
                          len(result.artifact.types)), (8, 20, 7))
        self.assertIn("\ntype: Process\n", PROCESS_WITNESS.read_text(encoding="utf-8"))
        self.assertIn("\nworkflow:\n", PROCESS_WITNESS.read_text(encoding="utf-8"))


class WorkTypesModuleTest(unittest.TestCase):
    """#5001: the built-in module of the non-code types, inactive by default."""

    def test_it_declares_the_installable_types_beyond_bug_and_evolution(self):
        from ameesh.modules import work_types
        self.assertEqual(work_types.TYPES, tuple(
            kind for kind in process_workflow.SUPPORTED_TYPES
            if kind not in ("bug", "evolution")))
        self.assertEqual(work_types.descriptor(),
                         {"types": list(work_types.TYPES), "schemas": {}, "rules": []})
        with self.assertRaisesRegex(ValueError, "unknown work-types module option"):
            work_types.descriptor({"strict": True})

    def test_it_is_built_in_but_active_only_where_selected(self):
        found = registry.discover()
        self.assertEqual(found.get("work-types").source, "builtin")
        self.assertNotIn("work-types", modules.always_active())
        loaded = canon.Canon(root=".", readable=True, id="test", federation={
            "extensions": {"ameesh": {"projects": {
                "support": {"modules": ["work-types"]},
                "acme": {"modules": ["it"]}}}}})
        self.assertEqual(sorted((row["project"], row["module"])
                                for row in modules.declarations(loaded.federation, loaded)),
                         [("acme", "it"), ("support", "work-types")])


class PipelineExamplesEndToEndTest(PgTestCase):
    """Adopt, drive and project both shipped cards on a real PostgreSQL."""

    def setUp(self):
        super().setUp()
        installed = dict(process_registry.INSTALLED_VERIFIERS)

        def restore_verifiers():
            process_registry.INSTALLED_VERIFIERS.clear()
            process_registry.INSTALLED_VERIFIERS.update(installed)

        self.addCleanup(restore_verifiers)
        self.db.execute("TRUNCATE project_modules CASCADE")
        self.repo = Path(self.tmp) / ("exemples-%s" % os.urandom(4).hex())
        copie_canon(self.repo)
        (self.repo / "federation.yaml").write_text(FEDERATION, encoding="utf-8")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.org",
                 "commit", "-qm", "two pipeline examples")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.loaded = canon.load(str(self.repo), ref=self.sha)
        self.assertEqual(canon.errors(canon.validate(self.loaded)), [])
        patch = mock.patch.object(canon, "load_configured",
                                  return_value=[self.loaded])
        patch.start()
        self.addCleanup(patch.stop)
        found = modules.installed(self.loaded)
        self.acme = found[ACME]
        self.support = found[SUPPORT]
        self.db.execute("INSERT INTO agent_registry (name, status) "
                        "VALUES ('author', 'idle')")
        # A declarative module stores no work item type: support's four types
        # come from the built-in `work-types` module (#5001), activated with
        # the card in the canon and applied by the normal `canon sync` step.
        modules.sync(self.db, self.loaded)
        self.acme_lot = self.lot("acme delivery", "evolution", "acme")
        self.support_lot = self.lot("customer request", "docs", "support")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True)

    def lot(self, title: str, type_: str, app: str) -> int:
        return int(self.db.query(
            "INSERT INTO work_items (title, type, app, state, assignee) "
            "VALUES (%s, %s, %s, 'intake', 'author') RETURNING id",
            (title, type_, app))[0]["id"])

    def run_cli(self, module, *args, expected=0):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = pipeline_cli.main(self.db, list(map(str, args)), actor="author",
                                     module=module, cfg=self.cfg)
        self.assertEqual(code, expected, errors.getvalue())
        return json.loads(output.getvalue()) if code == 0 else errors.getvalue()

    def projection(self, module, lot: int):
        state = process_registry.registry_state(self.db, lot)
        name, spec, encoding = pipeline_cli.pipeline_at(
            state.workflow_ref, cfg=self.cfg, version=state.workflow_version)
        self.assertEqual(name, module.NAME)
        mapping = process_projection.ProcessMapping(
            spec.projection.project, state.workflow_ref, state.workflow_version,
            spec, encoding.stage_labels)
        return process_projection.project_issue(work.get(self.db, lot), mapping)

    def test_acme_card_advances_holds_and_releases(self):
        adopted = self.run_cli(self.acme, "adopt", self.acme_lot)
        self.assertEqual(adopted["workflow_stage"], "draft")
        self.run_cli(self.acme, "advance", self.acme_lot,
                     "--to", "backlog", "--guard", "planning")
        projected = self.projection(self.acme, self.acme_lot)
        self.assertEqual((projected.state, projected.label, projected.core),
                         ("open", "Backlog", "intake"))
        self.run_cli(self.acme, "hold", self.acme_lot, "--hold", "blocked")
        projected = self.projection(self.acme, self.acme_lot)
        self.assertEqual((projected.state, projected.label),
                         ("open", "Backlog"))
        self.run_cli(self.acme, "release", self.acme_lot)
        self.run_cli(self.acme, "advance", self.acme_lot,
                     "--to", "sprint", "--guard", "planning")
        projected = self.projection(self.acme, self.acme_lot)
        self.assertEqual((projected.state, projected.label, projected.core),
                         ("open", "Sprint", "intake"))
        self.assertEqual(process_registry.registry_state(
            self.db, self.acme_lot).receipts, 4)

    def test_support_card_completes_a_non_development_request(self):
        self.run_cli(self.support, "adopt", self.support_lot)
        self.run_cli(self.support, "advance", self.support_lot,
                     "--to", "triage", "--guard", "planning")
        self.run_cli(self.support, "advance", self.support_lot,
                     "--to", "draft", "--guard", "start_work")
        self.run_cli(self.support, "hold", self.support_lot,
                     "--hold", "blocked")
        projected = self.projection(self.support, self.support_lot)
        self.assertEqual((projected.state, projected.label, projected.core),
                         ("open", "Draft", "build"))
        self.run_cli(self.support, "release", self.support_lot)
        # A non-code request needs a review and its own outcome, never a
        # software delivery.
        sha = "a" * 40
        self.db.execute("UPDATE work_items SET reviewer='relecteur' WHERE id=%s",
                        (self.support_lot,))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor) "
            "VALUES (%s, 'frozen', %s, 'author')", (self.support_lot, sha))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, verdict) "
            "VALUES (%s, 'verdict', %s, 'relecteur', 'ok')", (self.support_lot, sha))
        self.run_cli(self.support, "advance", self.support_lot,
                     "--to", "approval", "--guard", "review_requested",
                     "--ref", "head=" + sha)
        projected = self.projection(self.support, self.support_lot)
        self.assertEqual((projected.state, projected.label, projected.core),
                         ("open", "Approval", "qa"))
        self.run_cli(self.support, "advance", self.support_lot,
                     "--to", "sent", "--guard", "work_completed",
                     "--ref", "outcome=reply sent to the customer")
        projected = self.projection(self.support, self.support_lot)
        self.assertEqual((projected.state, projected.state_reason,
                          projected.label, projected.core),
                         ("closed", "completed", "Sent", "promoted"))
        self.assertEqual(process_registry.registry_state(
            self.db, self.support_lot).receipts, 6)

    def test_the_canon_activates_work_types_for_support_only(self):
        stored = {(row["project"], row["module"]): row["types"]
                  for row in ModuleStore(self.db).stored()}
        self.assertEqual(sorted(stored), [("acme", ACME), ("support", SUPPORT),
                                          ("support", "work-types")])
        self.assertEqual(stored[("support", "work-types")],
                         ["docs", "investigation", "governance-tracking",
                          "promotion", "human_operation"])
        lot = work.add(self.db, title="answer a customer", type="docs",
                       app="support", actor="author")
        self.assertEqual(work.get(self.db, lot["id"])["type"], "docs")

    def test_a_project_without_work_types_refuses_a_docs_lot(self):
        with self.assertRaisesRegex(work.WorkError, "unknown type 'docs' for "
                                                    "project 'acme'"):
            work.add(self.db, title="write the manual", type="docs", app="acme",
                     actor="author")
        # the SQL guard is unchanged: the same refusal below the API
        with self.assertRaisesRegex(DbError, "AMEESH_MODULE_RULE: work item type "
                                             "docs has no active module in project acme"):
            self.lot("write the manual", "docs", "acme")

    def test_adoption_refuses_a_module_that_the_project_did_not_activate(self):
        self.db.execute("DELETE FROM project_modules WHERE project = 'acme'")
        self.assertIn("pipeline-module-inactive",
                      self.run_cli(self.acme, "adopt", self.acme_lot, expected=1))
        self.assertFalse(work.get(self.db, self.acme_lot)["workflow_ref"])
