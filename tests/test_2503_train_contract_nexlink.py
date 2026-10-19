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
import sys
import tempfile
import types
import unittest
from unittest import mock

from ameesh import ci_parts, radar, train, train_ci, train_contracts


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

    def test_resolve_covers_the_head_declarations(self):
        """B1 de la relecture #2503 : une suite ajoutée par la tête est due."""
        calls = []

        def due(_project, _head, _base, _repo, *, declarations_sha=None, cache=None):
            calls.append(declarations_sha)
            names = (("lot/alpha",) if declarations_sha is None
                     else ("lot/alpha", "lot/beta"))
            return {"schema": "nexlink-lot-plan/1", "due": names}

        with self.audit_base(), self.declared(), mock.patch.object(
                train_contracts.ci_parts, "due", side_effect=due):
            found = self.contract.resolve(self.plan, repo="/nonexistent")
        self.assertEqual([(workflow.context, workflow.path) for workflow in found], [
            ("lot/alpha", ".github/workflows/alpha.yml"),
            ("lot/beta", ".github/workflows/beta.yml"),
        ])
        self.assertEqual(calls, [None, self.plan["head"]])

    def test_resolve_preserves_a_base_due_part_the_head_no_longer_lists(self):
        """B1 : la tête ne peut pas retirer une part due à la base."""
        def due(_project, _head, _base, _repo, *, declarations_sha=None, cache=None):
            names = ("lot/alpha",) if declarations_sha is None else ()
            return {"schema": "nexlink-lot-plan/1", "due": names}

        with self.audit_base(), self.declared(), mock.patch.object(
                train_contracts.ci_parts, "due", side_effect=due):
            found = self.contract.resolve(self.plan, repo="/nonexistent")
        self.assertEqual([workflow.context for workflow in found], ["lot/alpha"])

    def test_due_part_is_declared_at_the_head(self):
        """B1 : la déclaration est lue à la tête dispatchée, pas à la base."""
        def run(_repo, *args):
            if args and args[0] == "show":
                if args[1].startswith(self.plan["head"] + ":"):
                    return types.SimpleNamespace(returncode=1, stdout=b"")
                return types.SimpleNamespace(
                    returncode=0, stdout=b"      context: lot/alpha\n")
            return types.SimpleNamespace(returncode=0, stdout=b"")

        with self.audit_base(), self.due("lot/alpha"), \
                mock.patch.object(train_contracts.resolutions, "_run", side_effect=run):
            with self.assertRaisesRegex(train.TrainError, "at the train head"):
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


#: Un faux `bwrap` : le contrat refuse de prouver sans bac à sable, et le
#: témoin B1 exerce la vraie plomberie `ci_parts` (extraction, sélecteur,
#: plan). Il exécute la commande demandée sans isoler, comme le faux de
#: #1651 ; le bac à sable réel a ses propres témoins.
FAKE_BWRAP = '''\
#!%s
import os
import sys

args = sys.argv[1:]
env = {}
cmd = []
index = 0
while index < len(args):
    arg = args[index]
    if arg == "--":
        cmd = args[index + 1:]
        break
    if arg == "--clearenv":
        env = {}
        index += 1
    elif arg in ("--die-with-parent", "--unshare-all", "--unshare-net"):
        index += 1
    elif arg in ("--ro-bind", "--bind"):
        index += 3
    elif arg == "--setenv":
        env[args[index + 1]] = args[index + 2]
        index += 3
    elif arg in ("--proc", "--dev", "--tmpfs", "--chdir"):
        if arg == "--chdir":
            os.chdir(args[index + 1])
        index += 2
    elif arg.startswith("--"):
        raise SystemExit("fake bwrap: unknown option %%r" %% arg)
    else:
        cmd = args[index:]
        break
if not cmd:
    raise SystemExit("fake bwrap: no command")
os.execvpe(cmd[0], cmd, env)
'''

#: Un sélecteur minimal qui lit les déclarations de workflows de l'arbre
#: extrait, comme `lot-plan.mjs` : la preuve B1 porte sur l'arbre lu, pas sur
#: le langage du sélecteur.
NEXLINK_SELECTOR = r'''\
import fnmatch
import json
import os
import sys

options = {}
args = sys.argv[1:]
for index, arg in enumerate(args):
    if arg.startswith("--"):
        options[arg[2:]] = args[index + 1]
root = os.environ["GITHUB_WORKSPACE"]
with open(options["files"], "rb") as handle:
    files = [name for name in handle.read().decode("utf-8").split("\0") if name]
due = []
for name in sorted(os.listdir(os.path.join(root, ".github", "workflows"))):
    if not name.endswith(".yml"):
        continue
    with open(os.path.join(root, ".github", "workflows", name), encoding="utf-8") as handle:
        text = handle.read()
    if "\n  lot-pending:\n" not in text:
        continue
    context = [line.split("context: ", 1)[1].strip()
               for line in text.splitlines() if line.strip().startswith("context: lot/")][0]
    patterns = []
    block = text.split("\n  push:\n", 1)
    if len(block) == 2:
        for line in block[1].splitlines()[1:]:
            if line.startswith("  ") and not line.startswith("    "):
                break
            if line.startswith("      - "):
                patterns.append(line[8:].strip())
    if patterns and not any(fnmatch.fnmatchcase(changed, pattern)
                            for changed in files for pattern in patterns):
        continue
    due.append(context)
with open(options["out"], "w", encoding="utf-8") as handle:
    json.dump({"schema": "nexlink-lot-plan/1", "due": due}, handle)
'''


class HeadDeclarationsTest(unittest.TestCase):
    """B1 de la relecture #2503, sur un dépôt réel : la base garde son plan et
    les ajouts de la tête entrent dans les parts dues."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "test")
        self.write("scripts/ci/lot-plan.py", NEXLINK_SELECTOR)
        self.write(".github/workflows/lot-plan.yml", "name: plan\n")
        self.write(".github/workflows/lot-status.yml", "name: status\n")
        self.write(".github/workflows/alpha.yml", self.workflow("alpha", "**"))
        self.write(".github/workflows/epsilon.yml", self.workflow("epsilon", "docs/**"))
        self.write("backend/file.txt", "base\n")
        self.base = self.commit("base")
        self.write(".github/workflows/alpha.yml", self.workflow("alpha", "docs/**"))
        self.write(".github/workflows/beta.yml", self.workflow("beta", "**"))
        self.write(".github/workflows/epsilon.yml", self.workflow("epsilon", "**"))
        self.write("backend/file.txt", "head\n")
        self.head = self.commit("head")
        sandbox = os.path.join(self.tmp.name, "sandbox-bin")
        os.makedirs(sandbox)
        path = os.path.join(sandbox, "bwrap")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_BWRAP % sys.executable)
        os.chmod(path, 0o755)
        patcher = mock.patch.dict(
            os.environ, {"PATH": sandbox + os.pathsep + os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.contract = train_contracts.nexlink()
        self.contract.ci_parts = radar.CiParts(
            selector="scripts/ci/lot-plan.py",
            argv=(sys.executable, "{selector}", "--files", "{files}", "--out", "{out}"),
            schema="nexlink-lot-plan/1", due="due")
        self.plan = dict(head=self.head, base=self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def workflow(self, name, *paths):
        filters = ""
        if paths:
            filters = "  push:\n    paths:\n" + "".join(
                "      - %s\n" % path for path in paths)
        return "name: %s\non:\n%sjobs:\n  lot-pending:\n    context: lot/%s\n" % (
            name, filters, name)

    def write(self, path, text):
        target = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def due(self, sha=None, *, declarations=None):
        return ci_parts.due(self.contract._project(), sha or self.head, self.base,
                            self.repo, declarations_sha=declarations)

    def test_the_base_plan_and_the_head_declarations_are_two_proofs(self):
        """The head adds beta and widens epsilon; alpha is narrowed there."""
        base = self.due()
        self.assertEqual(base["due"], ("lot/alpha",))
        self.assertEqual(base["declarations"], self.base)
        head = self.due(declarations=self.head)
        self.assertEqual(head["due"], ("lot/beta", "lot/epsilon"))
        self.assertEqual(head["declarations"], self.head)

    def test_base_and_head_proofs_do_not_share_a_cache_entry(self):
        cache = os.path.join(self.tmp.name, "cache")
        base = ci_parts.due(self.contract._project(), self.head, self.base, self.repo,
                            cache=cache)
        head = ci_parts.due(self.contract._project(), self.head, self.base, self.repo,
                            cache=cache, declarations_sha=self.head)
        self.assertEqual((base["due"], head["due"]),
                         (("lot/alpha",), ("lot/beta", "lot/epsilon")))
        again = ci_parts.due(self.contract._project(), self.head, self.base, self.repo,
                             cache=cache)
        self.assertEqual(again["due"], ("lot/alpha",))
        again = ci_parts.due(self.contract._project(), self.head, self.base, self.repo,
                             cache=cache, declarations_sha=self.head)
        self.assertEqual(again["due"], ("lot/beta", "lot/epsilon"))

    def test_resolve_joins_the_base_plan_and_the_head_declarations(self):
        with mock.patch.object(train_contracts.Nexlink, "_audit_base", return_value=None):
            found = self.contract.resolve(self.plan, repo=self.repo)
        self.assertEqual([(workflow.context, workflow.path) for workflow in found], [
            ("lot/alpha", ".github/workflows/alpha.yml"),
            ("lot/beta", ".github/workflows/beta.yml"),
            ("lot/epsilon", ".github/workflows/epsilon.yml"),
        ])

    def test_a_head_selector_change_is_refused_by_the_declarations_proof(self):
        self.write("scripts/ci/lot-plan.py", NEXLINK_SELECTOR + "\n# changed\n")
        changed = self.commit("selector change")
        with self.assertRaisesRegex(ci_parts.PartsError, "differs"):
            self.due(changed, declarations=changed)

    def test_a_head_removed_suite_cannot_silence_the_base_plan(self):
        os.remove(os.path.join(self.repo, ".github/workflows/alpha.yml"))
        removed = self.commit("remove alpha")
        with mock.patch.object(train_contracts.Nexlink, "_audit_base", return_value=None):
            with self.assertRaisesRegex(train.TrainError, "at the train head"):
                self.contract.resolve(dict(head=removed, base=self.base), repo=self.repo)


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
        self.plan = dict(head=self.head, tree=self.tree, base="c" * 40,
                         branch="integration/train-test")
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
        """B2 de la relecture #2503 : l'exception est celle de la suite."""
        self.jobs.append(dict(id=20, name="changed-paths", status="completed",
                              conclusion="skipped"))
        self.assertEqual(self.ci().read(self.plan)["status"], "green")

    def test_a_lot_named_test_job_is_not_plumbing(self):
        """B2 : un job de test `lot-*` sauté reste exigé.

        L'ancien préfixe global `lot-` écartait `lot-regression` et un run
        backend vert se lisait complet.
        """
        self.jobs.append(dict(id=20, name="lot-regression", status="completed",
                              conclusion="skipped"))
        with self.assertRaisesRegex(train.TrainError, "skipped or interrupted"):
            self.ci().read(self.plan)

    def test_identified_plumbing_jobs_are_tolerated(self):
        self.jobs.extend([
            dict(id=20, name="lot-plan / Due parts of the change set",
                 status="completed", conclusion="success"),
            dict(id=21, name="lot-plan / Announce the due parts still missing a status",
                 status="completed", conclusion="success"),
            dict(id=22, name="lot-plan-retry", status="completed", conclusion="skipped"),
            dict(id=23, name="lot-pending / Lot status (lot/backend-suite)",
                 status="completed", conclusion="success"),
            dict(id=24, name="lot-status / Lot status (lot/backend-suite)",
                 status="completed", conclusion="success"),
        ])
        self.assertEqual(self.ci().read(self.plan)["status"], "green")

    def test_a_plumbing_caller_prefix_does_not_tolerate_a_test_job(self):
        """B2 : seul le nom exact d'un job de plomberie est écarté."""
        self.jobs.append(dict(id=20, name="lot-plan / regression", status="completed",
                              conclusion="skipped"))
        with self.assertRaisesRegex(train.TrainError, "skipped or interrupted"):
            self.ci().read(self.plan)

    def test_lot_status_is_tolerated_only_for_this_suite_context(self):
        self.jobs.append(dict(id=20, name="lot-status / Lot status (lot/other-suite)",
                              status="completed", conclusion="skipped"))
        with self.assertRaisesRegex(train.TrainError, "skipped or interrupted"):
            self.ci().read(self.plan)

    def test_a_suite_tolerance_does_not_leak_to_another_suite(self):
        """B2 : `changed-paths` n'est toléré que dans la suite qui le déclare."""
        self.due = train_ci.DueWorkflow(".github/workflows/compute.yml", "lot/compute")
        self.contract.due = (self.due,)
        self.runs[0]["path"] = self.due.path
        self.jobs.append(dict(id=20, name="changed-paths", status="completed",
                              conclusion="skipped"))
        with self.assertRaisesRegex(train.TrainError, "skipped or interrupted"):
            self.ci().read(self.plan)

    def test_the_desktop_matrix_is_tolerated_only_in_its_suite(self):
        self.due = train_ci.DueWorkflow(".github/workflows/desktop-suite.yml",
                                        "lot/desktop-suite")
        self.contract.due = (self.due,)
        self.runs[0]["path"] = self.due.path
        self.jobs = [
            dict(id=19, name="desktop", status="completed", conclusion="success"),
            # a whole skipped matrix keeps the unexpanded name in the API
            dict(id=20, name="Desktop suite (${{ matrix.os }})", status="completed",
                 conclusion="skipped"),
            dict(id=21, name="Desktop suite (windows-latest)", status="completed",
                 conclusion="skipped"),
            dict(id=22, name="Desktop suite (macos-latest)", status="completed",
                 conclusion="skipped"),
        ]
        self.assertEqual(self.ci().read(self.plan)["status"], "green")
        self.due = train_ci.DueWorkflow(".github/workflows/backend-suite.yml",
                                        "lot/backend-suite")
        self.contract.due = (self.due,)
        self.runs[0]["path"] = self.due.path
        with self.assertRaisesRegex(train.TrainError, "skipped or interrupted"):
            self.ci().read(self.plan)

    def test_a_tolerated_job_that_ran_and_failed_is_red(self):
        """N2 de la relecture #2503 : la suite fautive reste nommable."""
        self.due = train_ci.DueWorkflow(".github/workflows/desktop-suite.yml",
                                        "lot/desktop-suite")
        self.contract.due = (self.due,)
        self.runs[0].update(path=self.due.path, conclusion="failure")
        self.jobs = [
            dict(id=19, name="desktop", status="completed", conclusion="success"),
            dict(id=20, name="Desktop suite (windows-latest)", status="completed",
                 conclusion="failure"),
        ]
        result = self.ci().read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("red", True))
        self.assertEqual([item["workflow"] for item in result["workflows"]],
                         [self.due.path])

    def test_a_failed_plumbing_job_with_green_tests_names_no_lot(self):
        """#2550 : un publieur de statut en échec n'est pas un test rouge."""
        self.runs[0].update(conclusion="failure")
        self.jobs = [
            dict(id=19, name="backend", status="completed", conclusion="success"),
            dict(id=20, name="lot-status / Lot status (%s)" % self.due.context,
                 status="completed", conclusion="failure"),
        ]
        with self.assertRaisesRegex(train.TrainError, "does not prove"):
            self.ci().read(self.plan)

    def test_remote_source_check_runs_once_per_head_and_base(self):
        """N1 de la relecture #2503 : la preuve dépend aussi de la base."""
        ci = self.ci()
        ci.read(self.plan)
        self.calls.clear()
        ci.read(self.plan)
        self.assertFalse(any("/contents/" in call[-1] for call in self.calls))
        ci.read(dict(self.plan, base="d" * 40))
        self.assertTrue(any("/contents/" in call[-1] for call in self.calls))

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
