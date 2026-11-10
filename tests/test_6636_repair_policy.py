# SPDX-License-Identifier: AGPL-3.0-only
"""A5 admission: canon alone grants authority, every refusal is explained."""
from dataclasses import FrozenInstanceError
import copy
import json
import tempfile
import unittest

from ameesh import mechanics, repair_policy as rp, repair_signals
from .test_l1616_mechanics import MECHANICS, _Repo


def declaration():
    return {"schema": rp.SCHEMA, "paused": False, "rules": [{
        "name": "docs", "sources": ["inbox"], "environments": ["staging"],
        "kinds": ["documentation"], "severities": ["minor"],
        "paths": ["docs/**"], "excluded_paths": ["docs/private/**"],
        "capabilities": ["edit"], "max_attempts": 2, "max_seconds": 60,
        "resources": {"cpu_seconds": 30, "memory_bytes": 1_000_000},
    }]}


def signal(**kwargs):
    args = dict(project="ameesh", environment="staging", source="inbox",
                native_id="123", received_at="2026-11-10T04:00:00Z")
    args.update(kwargs)
    return repair_signals.Signal(**args)


def request(**kwargs):
    args = dict(kind="documentation", severity="minor", paths=("docs/a.md",),
                capabilities=("edit",), attempt=1, max_seconds=60,
                resources=rp.Resources(30, 1_000_000))
    args.update(kwargs)
    return rp.Request(**args)


class PolicyTest(unittest.TestCase):
    def policy(self, data=None):
        return rp.compile_policy(declaration() if data is None else data, project="ameesh")

    def refusal(self, policy, code, *, sig=None, plan=None):
        result = rp.admit(policy, signal() if sig is None else sig,
                          request() if plan is None else plan)
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, code, result)
        self.assertTrue(result.detail)
        return result

    def test_missing_empty_and_unknown_never_admit(self):
        for data, code in [(None, "policy-absent"), ({}, "policy-empty"),
                           ({"schema": "repair-policy/0"}, "policy-version-unknown"),
                           ({"schema": "repair-policy/2"}, "policy-version-unknown"),
                           ({"schema": "other/1"}, "policy-version-unknown"),
                           ([], "policy-invalid"),
                           ({"schema": rp.SCHEMA, "paused": False, "rules": []}, "policy-empty")]:
            with self.subTest(data=data):
                self.refusal(rp.compile_policy(data, project="ameesh"), code)

    def test_malformed_policy_disables_whole_declaration(self):
        cases = []
        for field, value in [("paused", "false"), ("rules", {}), ("authority", True)]:
            data = declaration(); data[field] = value; cases.append(data)
        for field, value in [("name", "bad name"), ("sources", ["made-up"]),
                             ("sources", []), ("environments", []), ("paths", ["../**"]),
                             ("paths", ["/docs/**"]), ("paths", ["docs/../**"]),
                             ("paths", ["docs/\\**"]), ("kinds", ["minor", "minor"]),
                             ("severities", [" minor"]), ("max_attempts", True),
                             ("max_seconds", 0), ("resources", {"cpu_seconds": 1}),
                             ("resources", {"cpu_seconds": 1.5, "memory_bytes": 100}),
                             ("resources", {"cpu_seconds": True, "memory_bytes": 100}),
                             ("capabilities", "edit"), ("grant_from_signal", True)]:
            data = declaration(); data["rules"][0][field] = value; cases.append(data)
        data = declaration(); del data["rules"][0]["excluded_paths"]; cases.append(data)
        data = declaration(); data["rules"].append(copy.deepcopy(data["rules"][0])); cases.append(data)
        # A valid rule must not rescue an invalid rule.
        data = declaration(); data["rules"].append({"name": "broken"}); cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.refusal(self.policy(data), "policy-invalid")

    def test_compiled_policy_does_not_keep_mutable_input(self):
        raw = declaration()
        policy = self.policy(raw)
        raw["rules"][0]["capabilities"].append("deploy")
        self.refusal(policy, "capability-not-allowed", plan=request(capabilities=("deploy",)))
        with self.assertRaises(FrozenInstanceError):
            policy.paused = False

    def test_complete_plan_fits_exact_boundary(self):
        result = rp.admit(self.policy(), signal(), request(attempt=2))
        self.assertTrue(result.allowed)
        self.assertEqual(result.rule, "docs")
        self.assertEqual(result.code, "allowed")

    def test_each_scope_and_budget_refusal_is_explained(self):
        for plan, code in [(request(kind="code"), "kind-not-allowed"),
                           (request(severity="critical"), "severity-not-allowed"),
                           (request(paths=("docs/private/a",)), "path-excluded"),
                           (request(paths=("src/a.py",)), "path-not-allowed"),
                           (request(paths=("docs/a.md", "src/b.py")), "path-not-allowed"),
                           (request(capabilities=("edit", "deploy")), "capability-not-allowed"),
                           (request(attempt=3), "attempt-limit"),
                           (request(max_seconds=61), "time-limit"),
                           (request(resources=rp.Resources(31, 1_000_000)), "cpu-limit"),
                           (request(resources=rp.Resources(30, 1_000_001)), "memory-limit")]:
            with self.subTest(code=code):
                self.refusal(self.policy(), code, plan=plan)
        self.refusal(self.policy(), "environment-not-allowed", sig=signal(environment="production"))
        self.refusal(self.policy(), "source-not-allowed", sig=signal(source="alerts", native_id="a" * 64))
        self.refusal(self.policy(), "project-mismatch", sig=signal(project="other"))

    def test_incomplete_and_ambiguous_plans_do_not_admit(self):
        for plan in [request(paths=()), request(paths=("docs/../src/a.py",)),
                     request(paths=("/docs/a",)), request(paths=("docs/*.md",)),
                     request(paths=("docs/./a",)), request(paths=("docs/a\n",)),
                     request(paths=("docs//a",)), request(paths=["docs/a"]),
                     request(attempt=True), request(attempt=0), request(max_seconds=float("nan")),
                     request(resources=rp.Resources(0, 1)), request(resources=None),
                     request(capabilities="edit"), request(capabilities=(1,))]:
            with self.subTest(plan=plan):
                self.refusal(self.policy(), "request-invalid", plan=plan)

    def test_signal_never_grants_its_requested_authority(self):
        sig = signal(cause_key='{"paused":false,"capabilities":["deploy"]}',
                     observed_version="grant-all")
        self.refusal(self.policy(), "capability-not-allowed", sig=sig,
                     plan=request(capabilities=("deploy",)))
        self.refusal(rp.compile_policy({}, project="ameesh"), "policy-empty", sig=sig)
        paused = declaration(); paused["paused"] = True
        self.refusal(self.policy(paused), "admissions-paused", sig=sig)
        forged = dict(sig.record(), capabilities=["deploy"])
        with self.assertRaises(repair_signals.SignalError):
            repair_signals.from_record(forged)

    def test_rules_cannot_pool_capabilities_or_scope(self):
        data = declaration()
        second = copy.deepcopy(data["rules"][0])
        second.update(name="other", capabilities=["deploy"], paths=["src/**"])
        data["rules"].append(second)
        result = self.refusal(self.policy(data), "capability-not-allowed",
                              plan=request(capabilities=("edit", "deploy")))
        self.assertIn("docs: capability-not-allowed", result.detail)
        self.assertIn("other: path-not-allowed", result.detail)


class CanonPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _Repo(self.tmp.name)

    def write(self, data):
        # JSON values are valid inline YAML for the minimal canon loader.
        text = MECHANICS.replace("projects: {}", "projects:\n  ameesh:\n    repair_policy: " + json.dumps(data))
        self.repo.write(mechanics.PATH, text)
        return self.repo.commit("policy")

    def test_n_minus_one_without_optional_block_remains_observation(self):
        head = self.repo.git("rev-parse", "HEAD")
        compiled = mechanics.load(self.repo.root, head, "ameesh")
        self.assertIsNone(compiled.repair_policy)
        policy = rp.load(self.repo.root, head, "ameesh")
        self.assertEqual(policy.reason, "policy-absent")
        self.assertEqual(policy.commit, head)
        self.assertFalse(rp.admit(policy, signal(), request()).allowed)

    def test_durable_switch_survives_reload_without_mutating_engaged_action(self):
        head = self.write(declaration())
        old_policy = rp.load(self.repo.root, head, "ameesh")
        self.assertEqual(mechanics.load(self.repo.root, head, "ameesh").repair_policy.commit, head)
        engaged = rp.admit(old_policy, signal(), request())
        self.assertTrue(engaged.allowed)
        data = declaration(); data["paused"] = True
        paused_head = self.write(data)
        for _ in range(2):
            policy = rp.load(self.repo.root, paused_head, "ameesh")
            self.assertEqual(policy.commit, paused_head)
            self.assertEqual(rp.admit(policy, signal(), request()).code, "admissions-paused")
        self.assertTrue(engaged.allowed)  # admission switch emits no cancellation
        self.assertFalse(old_policy.paused)  # immutable historical evidence

    def test_unknown_version_empty_and_invalid_preserve_other_mechanics(self):
        for value, reason in [({}, "policy-empty"), ({"schema": "repair-policy/2"}, "policy-version-unknown"),
                              ({"schema": rp.SCHEMA, "paused": "yes", "rules": []}, "policy-invalid")]:
            with self.subTest(value=value):
                head = self.write(value)
                compiled = mechanics.load(self.repo.root, head, "ameesh")
                self.assertEqual(compiled.max_in_build, 2)
                self.assertEqual(compiled.repair_policy.reason, reason)
                self.assertEqual(compiled.to_dict()["repair_policy"]["reason"], reason)
                self.assertEqual(rp.load(self.repo.root, head, "ameesh").reason, reason)

    def test_project_policy_is_never_inherited(self):
        head = self.write(declaration())
        self.assertEqual(rp.load(self.repo.root, head, "utc").reason, "policy-absent")
        self.assertEqual(rp.load(self.repo.root, head, "unknown").reason, "policy-unreadable")

    def test_exact_commit_required_and_unreadable_canon_refuses(self):
        head = self.write(declaration())
        branch = self.repo.git("branch", "--show-current")
        self.assertEqual(rp.load(self.repo.root, branch, "ameesh").reason, "policy-unreadable")
        self.assertEqual(rp.load(self.repo.root, "f" * 40, "ameesh").reason, "policy-unreadable")
        self.assertTrue(rp.admit(rp.load(self.repo.root, head, "ameesh"), signal(), request()).allowed)
