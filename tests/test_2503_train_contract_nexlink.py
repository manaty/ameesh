# SPDX-License-Identifier: AGPL-3.0-only
"""#2503 : le contrat CI Nexlink est dynamique — seules les parts dues comptent.

Un paquet Nexlink n'est pas un run unique des 23 suites et le contrat ne les
gèle pas : `lot-plan.mjs` calcule les parts que le change set intégré rend
dues, le fournisseur dispatche ces suites avec `expected_sha`, puis exige
leurs jobs exécutables (hors plomberie et jobs tolérés sauté). Le sélecteur et
les workflows de plan/statut partagés sont audités ; une suite due doit
exister à la tête, une liste vide est refusée.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import train, train_ci, train_contracts


class NexlinkContractTest(unittest.TestCase):
    def setUp(self):
        self.contract = train_contracts.nexlink()
        self.plan = dict(head="a" * 40, base="b" * 40)

    def audit_base(self):
        """Skip the local selector check in the due-selection witnesses."""
        return mock.patch.object(train_contracts.Nexlink, "_audit_base",
                                 return_value=None)

    def due(self, *contexts):
        return mock.patch.object(
            train_contracts.ci_parts, "due",
            return_value={"schema": "nexlink-lot-plan/1", "due": contexts})

    def declared(self):
        """The target workflow files declare the same contexts as the selector."""
        def run(_repo, *args):
            if args and args[0] == "show":
                path = args[1].split(":", 1)[1]
                context = "lot/" + path.rsplit("/", 1)[-1][:-4]
                return types.SimpleNamespace(
                    returncode=0, stdout=("      context: %s\n" % context).encode())
            return types.SimpleNamespace(returncode=0, stdout=b"")

        return mock.patch.object(train_contracts.resolutions, "_run", side_effect=run)

    def test_contract_does_not_freeze_the_23_suites(self):
        self.assertFalse(hasattr(self.contract, "__iter__"))
        with self.audit_base(), self.declared(), self.due("lot/backend-suite"):
            due = self.contract.resolve(self.plan, repo="/nonexistent")
        self.assertEqual([workflow.path for workflow in due],
                         [".github/workflows/backend-suite.yml"])
        with self.audit_base(), self.declared(), self.due("lot/backend-suite"):
            sources = self.contract.sources_for(self.plan, repo="/nonexistent")
        self.assertEqual(set(sources), {
            ".github/workflows/lot-plan.yml",
            ".github/workflows/lot-status.yml",
            "scripts/ci/lot-plan.mjs",
        })
        self.assertNotIn(".github/workflows/backend-suite.yml", sources)

    def test_resolve_maps_every_due_context_to_its_workflow(self):
        with self.audit_base(), self.declared(), self.due(
                "lot/backend-suite", "lot/host-rust"):
            due = self.contract.resolve(self.plan, repo="/nonexistent")
        self.assertEqual([(workflow.context, workflow.path) for workflow in due], [
            ("lot/backend-suite", ".github/workflows/backend-suite.yml"),
            ("lot/host-rust", ".github/workflows/host-rust.yml"),
        ])
        self.assertTrue(all(isinstance(workflow, train_ci.DueWorkflow) for workflow in due))

    def test_due_part_must_be_declared_by_its_workflow(self):
        def wrong(_repo, *args):
            return types.SimpleNamespace(returncode=0,
                                         stdout=b"      context: lot/something-else\n")

        with self.audit_base(), self.due("lot/backend-suite"), \
                mock.patch.object(train_contracts.resolutions, "_run", side_effect=wrong):
            with self.assertRaisesRegex(train.TrainError, "not declared"):
                self.contract.resolve(self.plan, repo="/nonexistent")

    def test_resolve_refuses_a_malformed_due_context(self):
        with self.audit_base(), self.due("not-a-lot"):
            with self.assertRaisesRegex(train.TrainError, "malformed due part"):
                self.contract.resolve(self.plan, repo="/nonexistent")

    def test_resolve_refuses_an_empty_due_list(self):
        with self.audit_base(), self.due():
            with self.assertRaisesRegex(train.TrainError, "no due part"):
                self.contract.resolve(self.plan, repo="/nonexistent")

    def test_resolve_names_the_selector_refusal(self):
        def refuse(*_args, **_kwargs):
            raise train_contracts.ci_parts.PartsError("bwrap is not installed")

        with self.audit_base(), mock.patch.object(
                train_contracts.ci_parts, "due", side_effect=refuse):
            with self.assertRaisesRegex(train.TrainError, "bwrap is not installed"):
                self.contract.resolve(self.plan, repo="/nonexistent")

    def test_base_selector_must_be_the_audited_one(self):
        path, blob = train_contracts._NEXLINK_LOT_SELECTOR
        text = "100644 blob %s\t%s" % (blob, path)
        proc = types.SimpleNamespace(returncode=0)
        with mock.patch.object(train_contracts.resolutions, "_run", return_value=proc), \
                mock.patch.object(train_contracts.resolutions, "_text", return_value=text):
            self.contract._audit_base("/tmp/repo", "b" * 40)
        broken = "100644 blob %s\t%s" % ("f" * 40, path)
        with mock.patch.object(train_contracts.resolutions, "_run", return_value=proc), \
                mock.patch.object(train_contracts.resolutions, "_text", return_value=broken):
            with self.assertRaisesRegex(train.TrainError, "selector differs"):
                self.contract._audit_base("/tmp/repo", "b" * 40)

    def test_digest_is_the_shared_contract_not_one_due_set(self):
        other = train_contracts.nexlink()
        self.assertEqual(self.contract.digest(), other.digest())
        self.assertRegex(self.contract.digest(), r"^[0-9a-f]{64}$")
        self.assertEqual(train_ci.contract_digest(self.contract), self.contract.digest())

    def test_sources_for_still_refuses_an_empty_plan(self):
        with self.audit_base(), self.due():
            with self.assertRaisesRegex(train.TrainError, "no due part"):
                self.contract.sources_for(self.plan, repo="/nonexistent")


class FakeContract:
    """A dynamic contract with a fixed due set and shared sources."""

    def __init__(self, due, sources):
        self.due = tuple(due)
        self.sources = dict(sources)
        self.verified_heads = set()

    def digest(self):
        return "e" * 64

    def resolve(self, plan, *, repo, cache=None):
        return self.due

    def due_for(self, plan, *, repo, cache=None):
        return self.due

    def sources_for(self, plan, *, repo, cache=None):
        return dict(self.sources)


class DynamicGitHubTest(unittest.TestCase):
    """The adapter dispatches and reads only the due suites; the rest is inert."""

    def setUp(self):
        self.head, self.tree = "a" * 40, "b" * 40
        self.plan = dict(head=self.head, tree=self.tree, branch="integration/train-test")
        self.due = train_ci.DueWorkflow(
            ".github/workflows/backend-suite.yml", "lot/backend-suite")
        self.sources = {
            ".github/workflows/lot-plan.yml": "2" * 40,
            ".github/workflows/lot-status.yml": "3" * 40,
            "scripts/ci/lot-plan.mjs": "4" * 40,
        }
        self.contract = FakeContract((self.due,), self.sources)
        self.calls = []
        self.dispatch = []
        self.runs = [dict(
            id=9, run_attempt=1, status="completed", conclusion="success",
            head_sha=self.head, head_branch=self.plan["branch"],
            path=self.due.path, event="workflow_dispatch")]
        self.jobs = [dict(id=19, name="backend", status="completed",
                          conclusion="success")]

    def gh(self, args):
        self.calls.append(args)
        if args[0] == "workflow":
            self.dispatch.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        path = args[-1]
        if "/contents/" in path:
            source = path.split("/contents/", 1)[1].split("?", 1)[0]
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                [dict(type="file", sha=self.sources.get(source, "d" * 40))]))
        if "/jobs?" in path:
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                [dict(jobs=self.jobs)]))
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            [dict(workflow_runs=self.runs)]))

    def ci(self):
        return train_ci.GitHub("o/r", self.contract, gh=self.gh, repo="/tmp/clone")

    def test_start_dispatches_only_the_due_suite(self):
        self.ci().start(self.plan)
        self.assertEqual([call[2] for call in self.dispatch], [self.due.path])

    def test_read_requires_every_non_tolerated_job_and_is_green(self):
        self.jobs.extend([
            dict(id=20, name="changed-paths", status="completed", conclusion="skipped"),
            dict(id=21, name="lot-plan / Due parts of the change set",
                 status="completed", conclusion="success"),
            dict(id=22, name="lot-status / Lot status (lot/backend-suite)",
                 status="completed", conclusion="success"),
        ])
        result = self.ci().read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("green", True))
        self.assertEqual([item["workflow"] for item in result["workflows"]],
                         [self.due.path])

    def test_a_new_required_job_failure_is_red(self):
        self.runs[0]["conclusion"] = "failure"
        self.jobs.append(dict(id=20, name="new-regression", status="completed",
                              conclusion="failure"))
        result = self.ci().read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("red", True))

    def test_missing_due_run_is_absent_not_green(self):
        self.runs = []
        result = self.ci().read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("absent", False))

    def test_red_due_suite_is_complete_red(self):
        self.runs[0]["conclusion"] = "failure"
        self.jobs[0]["conclusion"] = "failure"
        result = self.ci().read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("red", True))

    def test_skipped_required_job_is_not_full_ci(self):
        self.jobs[0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(train.TrainError, "skipped"):
            self.ci().read(self.plan)

    def test_tolerated_skipped_job_does_not_break_the_suite(self):
        self.jobs.append(dict(id=20, name="desktop-platform", status="completed",
                              conclusion="skipped"))
        self.assertEqual(self.ci().read(self.plan)["status"], "green")

    def test_remote_source_check_runs_once_per_head(self):
        ci = self.ci()
        ci.read(self.plan)
        self.calls.clear()
        ci.read(self.plan)
        self.assertFalse(any("/contents/" in call[-1] for call in self.calls))

    def test_remote_source_check_refuses_a_changed_shared_blob(self):
        original = self.gh

        def changed(args):
            if "/contents/scripts/ci/lot-plan.mjs" in args[-1]:
                self.calls.append(args)
                return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                    [dict(type="file", sha="f" * 40)]))
            return original(args)

        ci = train_ci.GitHub("o/r", self.contract, gh=changed, repo="/tmp/clone")
        with self.assertRaisesRegex(train.TrainError, "source differs"):
            ci.read(self.plan)
        self.assertFalse(self.dispatch)

    def test_due_workflow_must_exist_at_the_head(self):
        def missing(args):
            if "/contents/" + self.due.path in args[-1]:
                self.calls.append(args)
                return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                    [dict(type="dir")]))
            return self.gh(args)

        ci = train_ci.GitHub("o/r", self.contract, gh=missing, repo="/tmp/clone")
        with self.assertRaisesRegex(train.TrainError, "missing at the train head"):
            ci.read(self.plan)
        self.assertFalse(self.dispatch)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class VerifyLocalTest(unittest.TestCase):
    """Before publication, only the shared blobs and the due suite existence are checked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "test")
        os.makedirs(os.path.join(self.repo, ".github/workflows"))
        os.makedirs(os.path.join(self.repo, "scripts/ci"))
        self.plan = dict(head=None)
        self.write(".github/workflows/lot-plan.yml", "plan\n")
        self.write(".github/workflows/lot-status.yml", "status\n")
        self.write("scripts/ci/lot-plan.mjs", "selector\n")
        self.write(".github/workflows/backend-suite.yml", "backend\n")
        self.commit()
        self.sources = {path: self.blob(path) for path in (
            ".github/workflows/lot-plan.yml",
            ".github/workflows/lot-status.yml",
            "scripts/ci/lot-plan.mjs")}
        self.due = train_ci.DueWorkflow(
            ".github/workflows/backend-suite.yml", "lot/backend-suite")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        with open(os.path.join(self.repo, path), "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "fixture")
        self.plan["head"] = git(self.repo, "rev-parse", "HEAD")

    def blob(self, path):
        return git(self.repo, "rev-parse", "HEAD:" + path)

    def test_shared_sources_must_match_and_the_due_workflow_must_exist(self):
        contract = FakeContract((self.due,), self.sources)
        train_ci.verify_local(self.repo, self.plan, contract)

    def test_changed_shared_source_refuses_publication(self):
        contract = FakeContract((self.due,), dict(self.sources,
            **{"scripts/ci/lot-plan.mjs": "e" * 40}))
        with self.assertRaisesRegex(train.TrainError, "source differs"):
            train_ci.verify_local(self.repo, self.plan, contract)

    def test_missing_due_workflow_refuses_publication(self):
        contract = FakeContract((self.due,), self.sources)
        os.remove(os.path.join(self.repo, ".github/workflows/backend-suite.yml"))
        self.commit()
        with self.assertRaisesRegex(train.TrainError, "due suite workflow missing"):
            train_ci.verify_local(self.repo, self.plan, contract)

    def test_an_undue_workflow_is_not_part_of_this_packet_proof(self):
        contract = FakeContract((self.due,), self.sources)
        self.write(".github/workflows/website-suite.yml", "website\n")
        self.commit()
        train_ci.verify_local(self.repo, self.plan, contract)


class AssemblyTest(unittest.TestCase):
    def test_assembly_exposes_the_nexlink_contract(self):
        from ameesh import train_assembly
        self.assertIs(train_assembly.CONTRACTS["nexlink"], train_contracts.nexlink)


if __name__ == "__main__":
    unittest.main()
