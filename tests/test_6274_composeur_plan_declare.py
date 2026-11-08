# SPDX-License-Identifier: AGPL-3.0-only
"""#6274: the hourly composer consumes the project's declared plan and registry.

Witness: the #6186 second-project repository (Jest identities, its own plan
adapter, no `tests/parts/toujours.txt`, no `src/ameesh`) passes the composer's
contract pass end to end: the declared adapter plans, the identities stay
opaque (`backend/test/payments.e2e-spec.ts`, never `tests.`-prefixed), and the
declared part launcher runs them, never `unittest`. ameesh's own convention
(`scripts/test-parts.py`) keeps the native path unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from ameesh import mechanics, prevalidation_calls as calls
from tests.test_4304_paquet_poussee_precoce import paquet_auto as auto
from tests.test_6186_project_plan import (CONTRACT, MODULE, PARTS_SCRIPT,
                                          PLAN_SCRIPT, PROJECT, Repo)

#: a declared launcher speaking the part protocol, not Python unittest
LAUNCHER = """#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
run = sub.add_parser("run")
for name in ("--plan", "--part", "--driver", "--flow"):
    run.add_argument(name, required=True)
args = parser.parse_args()
part = json.loads(Path(args.plan).read_text())["parts"][args.driver][int(args.part) - 1]
failed = False
with open(args.flow, "w") as flow:
    def event(**fields):
        flow.write(json.dumps(dict(schema="ameesh-test-event/1", **fields)) + "\\n")
    event(type="debut", modules=part["modules"])
    for module in part["modules"]:
        if "SKIP" in Path(module).read_text():
            continue
        bad = "FAIL" in Path(module).read_text()
        failed = failed or bad
        event(type="test", module=module, test=module + " > pays",
              statut="echec" if bad else "succes")
    event(type="fin", statut="echec" if failed else "succes")
sys.exit(1 if failed else 0)
"""

#: `ameesh heavy … -- cmd`: run cmd at once (no host slot in a test)
HEAVY = '#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n'

_STATE = None


def setUpModule():
    global _STATE
    _STATE = tempfile.TemporaryDirectory(prefix="paquet-6274-state-",
                                         dir=os.environ.get("TMPDIR"))
    unittest.addModuleCleanup(_STATE.cleanup)
    for patch in (mock.patch.object(auto, "_state_dir", return_value=_STATE.name),
                  mock.patch.object(auto, "contract_attestation_context",
                                    side_effect=ValueError("isolated test: no canon"))):
        patch.start()
        unittest.addModuleCleanup(patch.stop)


class DeclaredComposerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.close)
        self.repo.write(PARTS_SCRIPT, LAUNCHER)
        self.repo.head = self.repo.commit("declared launcher")
        self.worktree = Path(self.repo.root)
        block = mechanics.load(self.repo.root, self.repo.base, PROJECT).prevalidation
        self.block = block
        with mock.patch.object(calls, "parameters", return_value=calls.Parameters(
                params=block, repo=self.repo.root, commit=self.repo.base,
                project=PROJECT)):
            self.tooling = auto.resolve_tooling(PROJECT, log=lambda _line: None)
        bins = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(bins.cleanup)
        bin_dir = Path(bins.name)
        heavy = bin_dir / "ameesh"
        heavy.write_text(HEAVY)
        heavy.chmod(heavy.stat().st_mode | stat.S_IEXEC)
        path = mock.patch.dict(os.environ, {"PATH": "%s%s%s" % (
            bin_dir, os.pathsep, os.environ.get("PATH", ""))})
        path.start()
        self.addCleanup(path.stop)

    def test_the_project_block_names_the_tools(self):
        self.assertTrue(self.tooling.declared)
        self.assertEqual((self.tooling.plan_script, self.tooling.parts_script,
                          self.tooling.contracts, self.tooling.project),
                         (PLAN_SCRIPT, PARTS_SCRIPT, (CONTRACT,), PROJECT))
        # Without a project, or with ameesh's own launcher, nothing changes.
        self.assertIs(auto.resolve_tooling(""), auto.LEGACY_TOOLING)
        self.assertFalse(auto.Tooling(parts_script=auto.DEFAULT_PARTS_SCRIPT).declared)
        with mock.patch.object(calls, "parameters", side_effect=RuntimeError("no block")):
            self.assertIs(auto.resolve_tooling(PROJECT, log=lambda _line: None),
                          auto.LEGACY_TOOLING)

    def test_identities_are_preserved_and_no_ameesh_convention_is_required(self):
        self.assertFalse((self.worktree / auto.CONTRACT_ALWAYS).exists())
        self.assertFalse((self.worktree / "src/ameesh/contract_plan.py").exists())
        modules = auto.contract_modules(self.worktree, base=self.repo.base,
                                        tooling=self.tooling)
        self.assertEqual(modules, [MODULE])
        # The N-1 convention on the same tree is the cx1 finding (#6186).
        with self.assertRaisesRegex(auto.PaquetAutoError, "toujours.txt"):
            auto.contract_modules(self.worktree, base=self.repo.base,
                                  tooling=auto.LEGACY_TOOLING)

    def test_an_identity_outside_the_tree_and_the_inventory_is_refused(self):
        self.repo.write(CONTRACT, MODULE + "\nbackend/test/gone.spec.ts\n")
        self.repo.commit("unknown contract")
        with self.assertRaisesRegex(auto.PaquetAutoError, "gone.spec.ts"):
            auto.contract_modules(self.worktree, base=self.repo.base,
                                  tooling=self.tooling)

    def passed(self):
        with mock.patch.object(auto, "_TOOLING", self.tooling):
            return auto.prepublication_contracts(self.worktree, base=self.repo.base,
                                                 budget=120, project=PROJECT)

    def test_the_declared_launcher_runs_the_contracts_green(self):
        report = self.passed()
        run = report.run
        self.assertIsInstance(run, auto.FlowTestRun)
        self.assertEqual(run.modules, [MODULE])
        self.assertTrue(run.green, run.output)
        self.assertFalse(run.inconclusive)
        self.assertEqual(run.tests, 1)
        self.assertEqual(report.head, self.repo.git("rev-parse", "HEAD"))

    def test_a_failing_spec_is_a_named_negative(self):
        self.repo.write(MODULE, "test('pay', () => { FAIL })\n")
        self.repo.commit("red spec")
        run = self.passed().run
        self.assertFalse(run.green)
        self.assertTrue(run.real_failure)
        self.assertEqual(run.failed_tests, [MODULE + " > pays"])

    def test_a_missing_launcher_is_inconclusive_never_green(self):
        self.repo.git("rm", "-q", PARTS_SCRIPT)
        self.repo.commit("no launcher")
        run = self.passed().run
        self.assertFalse(run.green)
        self.assertFalse(run.real_failure)
        self.assertTrue(run.inconclusive)

    def test_an_end_without_any_test_is_inconclusive_never_green(self):
        # B/P2 cx2: a launcher that only writes `fin succes` proves nothing.
        self.repo.write(MODULE, "test('pay', () => {}) // SKIP\n")
        self.repo.commit("silent launcher")
        run = self.passed().run
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.flow["fins"], ("succes",))
        self.assertFalse(run.green)
        self.assertFalse(run.real_failure)
        self.assertTrue(run.inconclusive)
        self.assertIn(MODULE, run.motive())

    def test_a_module_without_a_test_event_is_never_attested(self):
        # B/P2 cx2: A and B requested, only A played: not green, B not passed.
        other = "backend/test/refunds.e2e-spec.ts"
        self.repo.write(other, "test('refund', () => {}) // SKIP\n")
        self.repo.write(CONTRACT, MODULE + "\n" + other + "\n")
        self.repo.commit("two contracts, one silent")
        scope = {
            "plan": {"modules": [MODULE, other]}, "selection_digest": "1" * 64,
            "tests": [MODULE, other], "canon_digest": "2" * 64,
            "registry_digest": "3" * 64, "runner_digest": "4" * 64,
            "profile": "local-heavy", "drivers": {"driver": "contrats"},
            "database_schema": "v1", "fixtures": {}, "exclusions": {},
        }
        path = Path(_STATE.name, "attestation-6274.json")
        with mock.patch.object(auto, "contract_attestation_context",
                               return_value=(scope, path, [], None)):
            report = self.passed()
        run = report.run
        self.assertEqual(run.modules, [MODULE, other])
        self.assertEqual(run.executed, {MODULE})
        self.assertEqual(run.unexecuted, [other])
        self.assertFalse(run.green)
        self.assertTrue(run.inconclusive)
        self.assertIsNotNone(report.evidence)
        self.assertFalse(report.evidence["ok"])
        self.assertIn(other, report.evidence["missing"])
        self.assertNotIn(other, report.evidence["modules"])
        # Defence in depth: even a run read as green attests only what ran.
        with mock.patch.object(auto.FlowTestRun, "green", new=True), \
                mock.patch.object(auto, "contract_attestation_context",
                                  return_value=(scope, path, [], None)), \
                mock.patch.object(auto, "run_declared_contracts",
                                  return_value=run):
            forced = self.passed()
        self.assertIn(other, forced.evidence["missing"])
        self.assertIn(MODULE, forced.evidence["modules"])


if __name__ == "__main__":
    unittest.main()
