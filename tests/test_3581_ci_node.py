# SPDX-License-Identifier: AGPL-3.0-only
"""Lot #3581: the suite jobs carry the JS bridge's node and refuse a runner
without it.

The bridge witness of #3529 (`test_3529_nexlink_release.GitTests.
test_ci_planner_bridge_uses_frozen_script_without_dispatch`) calls `node`.
Some pool runners did not have it in their PATH, so 6fdfefdde made the witness
skip when `node` is absent; on those runners the bridge is then no longer
verified at all (a silent non-conclusive result, canon §3). The three jobs
that run the suite therefore provision node 22 — the major of the Nexlink
workflows that own that bridge, and of ui.yml — and verify it at job startup,
before the test part: a runner that cannot provide it fails the part instead
of skipping the witness.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import canon

from .support import REPO

WORKFLOW = os.path.join(REPO, ".github", "workflows", "tests.yml")
SUITE_JOBS = ("psycopg", "psql", "fs-class")
NODE_MAJOR = "22"
SETUP_NODE = "actions/setup-node"
CHECKOUT = "actions/checkout@"
PART_PREFIX = "part "


def load_workflow():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return canon.load_yaml(fh.read()) or {}


class NodeForTheJsBridgeTest(unittest.TestCase):
    def setUp(self):
        self.jobs = load_workflow()["jobs"]

    def steps(self, job):
        return self.jobs[job].get("steps") or []

    def test_each_suite_job_provisions_the_bridge_major(self):
        for job in SUITE_JOBS:
            with self.subTest(job=job):
                setups = [step for step in self.steps(job)
                          if str(step.get("uses", "")).startswith(SETUP_NODE + "@")]
                self.assertEqual(len(setups), 1,
                                 "%s: exactly one setup-node step" % job)
                ref = str(setups[0]["uses"]).split("@", 1)[1]
                self.assertRegex(ref, r"^[0-9a-f]{40}$",
                                 "%s: setup-node must be pinned by commit" % job)
                self.assertEqual(
                    str((setups[0].get("with") or {}).get("node-version")),
                    NODE_MAJOR,
                    "%s: the bridge major is %s" % (job, NODE_MAJOR))

    def test_the_startup_check_fails_closed_before_the_part(self):
        for job in SUITE_JOBS:
            with self.subTest(job=job):
                steps = self.steps(job)
                names = [str(step.get("name", "")) for step in steps]
                checkouts = [i for i, step in enumerate(steps)
                             if str(step.get("uses", "")).startswith(CHECKOUT)]
                witnesses = [i for i, step in enumerate(steps)
                             if "node_path" in str(step.get("run", ""))]
                parts = [i for i, name in enumerate(names)
                         if name.startswith(PART_PREFIX)]
                self.assertTrue(checkouts, "%s: no checkout" % job)
                self.assertEqual(len(witnesses), 1,
                                 "%s: exactly one node startup check" % job)
                self.assertTrue(parts, "%s: no test part" % job)
                index = witnesses[0]
                self.assertGreater(index, checkouts[0], "%s: check before checkout" % job)
                self.assertLess(index, parts[0], "%s: check after the test part" % job)
                witness = steps[index]
                # a startup check that can be skipped is not a check
                self.assertNotIn("if", witness, "%s: conditional node check" % job)
                self.assertNotIn("continue-on-error", witness,
                                 "%s: non-blocking node check" % job)
                body = str(witness["run"])
                self.assertIn("v22.", body, "%s: major not checked" % job)
                self.assertIn("::error::", body, "%s: no explicit error" % job)
                self.assertIn("exit 1", body, "%s: check does not fail closed" % job)

    def test_the_check_follows_the_provisioning_in_every_job(self):
        # The check must read the node the job has just provisioned, not a
        # host binary that happens to be on the runner: after a later edit a
        # check placed before setup-node would pass on one runner and fail on
        # the others, which is exactly the flakiness of #3581.
        for job in SUITE_JOBS:
            with self.subTest(job=job):
                steps = self.steps(job)
                setups = [i for i, step in enumerate(steps)
                          if str(step.get("uses", "")).startswith(SETUP_NODE + "@")]
                witnesses = [i for i, step in enumerate(steps)
                             if "node_path" in str(step.get("run", ""))]
                self.assertEqual(len(setups), 1, "%s: no setup-node" % job)
                self.assertEqual(len(witnesses), 1, "%s: no node check" % job)
                self.assertLess(setups[0], witnesses[0],
                                "%s: node checked before it is provisioned" % job)
                self.assertEqual(witnesses[0], setups[0] + 1,
                                 "%s: the startup check must directly follow setup-node" % job)

    def witness_body(self):
        step = [step for step in self.steps("psycopg")
                if "node_path" in str(step.get("run", ""))][0]
        return str(step["run"])

    def run_witness(self, body, path):
        return subprocess.run([shutil.which("bash") or "/bin/bash", "-c", body],
                              env=dict(os.environ, PATH=path), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    @staticmethod
    def write_node(folder, version):
        path = os.path.join(folder, "node")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho %s\n" % version)
        os.chmod(path, 0o755)

    def test_the_witness_refuses_an_absent_or_wrong_node(self):
        body = self.witness_body()
        absent_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, absent_dir)
        absent = self.run_witness(body, absent_dir)
        self.assertNotEqual(absent.returncode, 0, absent.stdout)
        self.assertIn("node 22 requis", absent.stdout)

        wrong_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, wrong_dir)
        self.write_node(wrong_dir, "v18.20.4")
        wrong = self.run_witness(body, wrong_dir)
        self.assertNotEqual(wrong.returncode, 0, wrong.stdout)
        self.assertIn("node 22 requis", wrong.stdout)

        right_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, right_dir)
        self.write_node(right_dir, "v22.22.0")
        right = self.run_witness(body, right_dir)
        self.assertEqual(right.returncode, 0, right.stdout)
        self.assertIn("pont JS disponible", right.stdout)


if __name__ == "__main__":
    unittest.main()
