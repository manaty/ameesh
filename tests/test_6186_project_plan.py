# SPDX-License-Identifier: AGPL-3.0-only
"""#6186: a second project declares its plan adapter and contract registry.

The engine carries neither the project name nor its paths: the project's canon
declares `prevalidation.plan_script` and
`projects.<project>.contract_selection.registry`, and the plan adapter supplies
the candidate inventory (Jest files here, not Python `tests/test_*.py`).
Activation and rollback are observed through `meets_canon`, which reads the
policy from the exact canon source that served the project.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ameesh import contract_plan as cp, contract_selection as cs
from ameesh import mechanics, packet_contract_proof, prevalidation as pv
from ameesh import prevalidation_calls as calls
from tests.test_l1616_mechanics import FEDERATION, MECHANICS

PROJECT = "nexlink"
MODULE = "backend/test/payments.e2e-spec.ts"
REGISTRY = "tests/parts/contrats-nexlink.json"
CONTRACT = "tests/parts/contrats-nexlink.txt"
PLAN_SCRIPT = "scripts/plan-nexlink.py"
PARTS_SCRIPT = "scripts/parts-nexlink.py"


def second_mechanics() -> str:
    return MECHANICS.replace(
        "schema: ameesh-mechanics/1", "schema: ameesh-mechanics/2"
    ).replace(
        "projects: {}",
        "projects:\n"
        "  nexlink:\n"
        "    contract_selection:\n"
        "      registry: %s\n"
        "      publish: contracts-first\n"
        "prevalidation:\n"
        "  classes: [P0, P1]\n"
        "  contracts: [%s]\n"
        "  hosts: [vm1]\n"
        "  timeout_seconds: 3600\n"
        "  plan_script: %s\n"
        "  parts_script: %s\n"
        % (REGISTRY, CONTRACT, PLAN_SCRIPT, PARTS_SCRIPT),
    )


def second_federation() -> str:
    # The federation declares which projects this canon serves (and therefore
    # which canon `parameters` selects); add nexlink beside the fixture's utc.
    text = FEDERATION.replace(
        "      utc:\n        default: light\n        rules: []\n",
        "      nexlink:\n        default: normal\n        rules: []\n"
        "      utc:\n        default: light\n        rules: []\n",
        1,
    )
    return text.replace(
        "      utc:\n        rules:\n",
        "      nexlink:\n        rules:\n"
        "          - paths:\n"
        "              - \"**\"\n"
        "            reviews: 1\n"
        "      utc:\n        rules:\n",
        1,
    )


def without_policy(text: str) -> str:
    return text.replace(
        "projects:\n"
        "  nexlink:\n"
        "    contract_selection:\n"
        "      registry: %s\n"
        "      publish: contracts-first\n" % REGISTRY,
        "projects: {}\n",
    )


class Repo:
    """A minimal candidate repository plus its canon at the same commits."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = self.tmp.name
        self.env = dict(
            os.environ,
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.write("backend/src/payments.ts", "export const pay = 1\n")
        self.write("backend/test/payments.e2e-spec.ts", "test('pay', () => {})\n")
        self.write(REGISTRY, json.dumps({"schema": cs.SCHEMA, "rules": [{
            "id": "backend-tests",
            "paths": ["backend/**"],
            "operations": list(cs.OPERATIONS),
            "modules": [MODULE],
        }]}, indent=1) + "\n")
        self.write(CONTRACT, MODULE + "  # the second project's declared contract\n")
        self.write(PARTS_SCRIPT, "#!/usr/bin/env python3\n")
        self.digest = cs.compile_registry(self.path(REGISTRY).read_text()).digest
        self.write(PLAN_SCRIPT, self._adapter())
        self.write("processus/mecanique.yaml", second_mechanics())
        self.write("federation.yaml", second_federation())
        self.write("tests/parts/plan-nexlink.json", json.dumps(self._template(), indent=1) + "\n")
        self.base = self.commit("base")
        self.write("backend/src/payments.ts", "export const pay = 2\n")
        self.head = self.commit("candidate")
        self.policy = mechanics.load(self.root, self.base, PROJECT).contract_selection

    def path(self, rel: str) -> Path:
        return Path(self.root, rel)

    def write(self, rel: str, text: str) -> None:
        path = self.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True, env=self.env)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def _adapter(self) -> str:
        return """#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
plan = sub.add_parser("plan")
plan.add_argument("--out", required=True)
plan.add_argument("--map")
plan.add_argument("--event")
plan.add_argument("--ref")
plan.add_argument("--base")
plan.add_argument("--mode")
plan.add_argument("--local-map", action="store_true")
args = parser.parse_args()
head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
data = json.loads(Path("tests/parts/plan-nexlink.json").read_text())
data["head"] = data["tested"] = head
data["contract_plan"]["commit"] = head
data["contract_plan"]["base"] = args.base
data["project"] = os.environ.get("AMEESH_PROJECT", "")
Path(args.out).write_text(json.dumps(data))
"""

    def _template(self) -> dict:
        shared = {
            "schema": cp.SCHEMA,
            "commit": "set by the adapter",
            "base": "set by the adapter",
            "inventory": [MODULE],
            "registry_digest": self.digest,
            "active": True,
            "contracts": [MODULE],
            "impacted": [],
            "modules": [MODULE],
            "evidence": {MODULE: {"reasons": ["rule:backend-tests"],
                                  "paths": ["backend/src/payments.ts"]}},
            "fallback_reasons": [],
            "full_suite_required": False,
        }
        return {
            "schema": "ameesh-test-plan/1",
            "event": "workflow_dispatch",
            "head": "set by the adapter",
            "tested": "set by the adapter",
            "mode": "lot",
            "classes": {MODULE: ["P3", "declared contract"]},
            "parts": {"jest": [{"part": 1, "of": 1, "label": "jest",
                                "modules": [MODULE], "estimate_s": 0.0}]},
            "contract": [MODULE],
            "contract_plan": shared,
        }

    def close(self) -> None:
        self.tmp.cleanup()


class SecondProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def test_registry_accepts_the_second_projects_module_identities(self):
        # A path-like identity is valid; a name with a separator is not.
        self.assertEqual(cs.compile_registry(
            self.repo.path(REGISTRY).read_text()).digest, self.repo.digest)
        for name in ("bad;module", "../escape", "/absolute", "a b"):
            with self.subTest(name=name), self.assertRaises(cs.RegistryError):
                cs.compile_registry(json.dumps({"schema": cs.SCHEMA, "rules": [{
                    "id": "bad", "paths": [], "operations": list(cs.OPERATIONS),
                    "modules": [name]}]}))

    def test_tooling_and_prepare_use_the_declared_plan_adapter(self):
        document = mechanics.load(self.repo.root, self.repo.base, PROJECT)
        block = document.prevalidation
        params = calls.Parameters(params=block, repo=self.repo.root,
                                  commit=self.repo.base, project=PROJECT)
        # No scripts/test-plan.py exists in this repository: only the canon
        # declaration makes the adapter visible.
        calls.tooling(self.repo.root, self.repo.head, params)

        prepared = calls.prepare(
            self.repo.root, self.repo.head, self.repo.base,
            plan_script=block.plan_script, project=PROJECT)
        self.addCleanup(prepared.close)
        plan = prepared.plan
        self.assertEqual(plan["project"], PROJECT)
        self.assertEqual(plan["contract_plan"]["inventory"], [MODULE])
        self.assertEqual(plan["contract_plan"]["commit"], self.repo.head)
        self.assertEqual(plan["contract_plan"]["base"], self.repo.base)

        # Removing the declaration rolls back to the convention (which this
        # repository does not carry): the refusal is named, never a silent
        # "can't open file" read as a bad selection.
        self.repo.write("processus/mecanique.yaml", second_mechanics().replace(
            "  plan_script: %s\n" % PLAN_SCRIPT, ""))
        convention = mechanics.load(
            self.repo.root, self.repo.commit("no plan"), PROJECT).prevalidation
        with self.assertRaisesRegex(pv.PrevalidationError, "tooling-missing"):
            calls.tooling(self.repo.root, self.repo.head, calls.Parameters(
                params=convention, repo=self.repo.root,
                commit=self.repo.base, project=PROJECT))

    def test_active_plan_is_verified_from_the_projects_own_canon(self):
        document = mechanics.load(self.repo.root, self.repo.base, PROJECT)
        block = document.prevalidation
        plan = self.repo._template()
        plan["head"] = plan["tested"] = self.repo.head
        plan["contract_plan"]["commit"] = self.repo.head
        plan["contract_plan"]["base"] = self.repo.base
        ok, why = pv.meets_canon(
            plan, block, self.repo.root, head=self.repo.head,
            project=PROJECT, canon_repo=self.repo.root,
            canon_commit=self.repo.base)
        self.assertEqual((ok, why), (True, ""))

    def test_a_plan_cannot_under_declare_the_conservative_fallback(self):
        document = mechanics.load(self.repo.root, self.repo.base, PROJECT)
        plan = self.repo._template()
        plan["head"] = plan["tested"] = self.repo.head
        plan["contract_plan"]["commit"] = self.repo.head
        plan["contract_plan"]["base"] = self.repo.base
        plan["contract_plan"]["full_suite_required"] = True
        ok, why = pv.meets_canon(
            plan, document.prevalidation, self.repo.root, head=self.repo.head,
            project=PROJECT, canon_repo=self.repo.root,
            canon_commit=self.repo.base)
        self.assertFalse(ok)
        self.assertIn("contract plan fallback differs", why)

    def test_rollback_drops_the_active_plan_and_returns_to_legacy(self):
        document = mechanics.load(self.repo.root, self.repo.base, PROJECT)
        block = document.prevalidation
        active = self.repo._template()
        active["head"] = active["tested"] = self.repo.head
        active["contract_plan"]["commit"] = self.repo.head
        active["contract_plan"]["base"] = self.repo.base

        self.repo.write("processus/mecanique.yaml",
                        without_policy(second_mechanics()))
        rolled = self.repo.commit("rollback")
        ok, why = pv.meets_canon(
            active, block, self.repo.root, head=self.repo.head,
            project=PROJECT, canon_repo=self.repo.root, canon_commit=rolled)
        self.assertFalse(ok)
        self.assertIn("contract plan policy unavailable", why)

        legacy = cp.build(self.repo.root, self.repo.head, self.repo.base,
                          [MODULE], [MODULE], (), None)
        self.assertFalse(legacy["active"])
        plan = self.repo._template()
        plan["head"] = plan["tested"] = self.repo.head
        plan["contract_plan"] = legacy
        ok, why = pv.meets_canon(
            plan, block, self.repo.root, head=self.repo.head,
            project=PROJECT, canon_repo=self.repo.root, canon_commit=rolled)
        self.assertEqual((ok, why), (True, ""))

    def test_publish_legacy_is_a_declared_rollback_of_the_packet_order(self):
        text = second_mechanics().replace(
            "      publish: contracts-first\n", "      publish: legacy\n")
        self.repo.write("processus/mecanique.yaml", text)
        rolled = self.repo.commit("publish legacy")
        loaded = mechanics.load_global(self.repo.root, rolled)
        self.assertEqual(packet_contract_proof.from_document(loaded, PROJECT), "legacy")
        self.assertEqual(mechanics.load(self.repo.root, rolled, PROJECT)
                         .contract_selection.publish, "legacy")


class NoClientLiteralTest(unittest.TestCase):
    """The generic engine holds no other project's name or path (#6186)."""

    def test_the_contract_engine_has_no_second_project_data(self):
        root = Path(__file__).resolve().parent.parent
        engine = ("contract_plan.py", "contract_selection.py", "prevalidation.py",
                  "prevalidation_calls.py", "packet_contract_proof.py")
        for name in engine:
            text = (root / "src" / "ameesh" / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("nexlink", text.lower())
                self.assertNotIn("backend/test", text)
                self.assertNotIn(PLAN_SCRIPT, text)


if __name__ == "__main__":
    unittest.main()
