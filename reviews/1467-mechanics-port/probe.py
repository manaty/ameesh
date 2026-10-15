# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-branch #1467/#1616 probe; SQL/Git real, CI/provenance fixtures.

PYTHONPATH=src ameesh heavy -- .venv/bin/python reviews/1467-mechanics-port/probe.py \
    --supplier . --commit 488681e0dbb00f53a39f8244819b3faf362b74c1

Only the supplier's exact mechanics.py object is loaded. No branch merge,
canon checkout, production connection, runner installation or remote API.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

from ameesh import train
from tests.test_1467_train_runner import TrainRunnerTest

MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
projects: {}
"""


def stamp(hour):
    return datetime.fromisoformat("2026-10-15T%s:00:00+02:00" % hour).timestamp()


class RealMechanicsTest(TrainRunnerTest):
    def test_real_next_cutoff_and_engine_use_the_changed_canon(self):
        self.queued_at_ten()
        commit = self.revise(MECHANICS.replace('"11:00"', '"12:00"'))
        passage = self.passage(policy_commit=commit)
        result, deadline = passage.scheduled_step(self.db, stamp("11"))
        self.assertEqual(result["result"], "idle", result)
        self.assertEqual(deadline, stamp("12"))
        self.assertEqual(self.api_calls, [])
        result, deadline = passage.scheduled_step(self.db, stamp("12"))
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(deadline, stamp("23"))

    def write(self, name, content):
        super().write(name, content)
        if name == "federation.yaml":
            super().write("processus/mecanique.yaml", MECHANICS)

    def passage(self, **kw):
        kw.setdefault("mechanics", supplier)
        return super().passage(**kw)

    def queued_at_ten(self):
        self.db.execute("UPDATE merge_queue SET queued_at=to_timestamp(%s)", [stamp("10")])

    def revise(self, document):
        self.write("processus/mecanique.yaml", document)
        return self.commit("different canon mechanics")

    def test_real_calendar_changes_when_only_pinned_canon_changes(self):
        self.queued_at_ten()
        before = self.passage(policy=train.Policy(enabled=True)).runtime(self.db)
        self.assertEqual(before.policy.cutoff(stamp("11")), stamp("11"))
        commit = self.revise(MECHANICS.replace('"11:00"', '"12:00"'))
        passage = self.passage(policy=train.Policy(enabled=True), policy_commit=commit)
        self.assertEqual(passage.step(self.db, stamp("11"))["result"], "idle")
        self.assertEqual(self.api_calls, [])
        result = passage.step(self.db, stamp("12"))
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(result["plan"]["proof"]["members"][0]["policy"]["policy_commit"], commit)

    def test_real_timezone_changes_engine_cutoff(self):
        self.queued_at_ten()
        commit = self.revise(MECHANICS.replace("Europe/Paris", "UTC"))
        passage = self.passage(policy=train.Policy(enabled=True), policy_commit=commit)
        self.assertEqual(passage.step(self.db, stamp("11"))["result"], "idle")
        self.assertEqual(passage.step(self.db, stamp("13"))["result"], "ci")

    def test_absent_mechanics_never_uses_old_versioned_policy_fallback(self):
        os.unlink(os.path.join(self.publisher, "processus/mecanique.yaml"))
        commit = self.commit("canon without mechanics")
        with self.assertRaisesRegex(train.TrainError, "absent"):
            self.passage(policy_commit=commit).step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])
        self.assertEqual(self.api_calls, [])

    def test_real_family_declaration_rejects_an_excluded_family(self):
        commit = self.revise(MECHANICS.replace("claude, codex, deepseek", "claude, codex"))
        result = self.passage(policy_commit=commit).step(self.db, self.now)
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("missing independent", result["error"])
        self.assertEqual(self.api_calls, [])

    def test_real_review_counts_come_from_same_canon_commit(self):
        del self.receipts["milestone:3"]  # Second review is not known.
        result = self.passage().step(self.db, self.now)
        self.assertEqual(result["result"], "refused", result)
        with open(os.path.join(self.publisher, "federation.yaml")) as fh:
            federation = json.load(fh)
        federation["review_policies"]["train_reviews"]["projects"][self.project.name]["rules"][0]["reviews"] = 1
        self.write("federation.yaml", json.dumps(federation))
        commit = self.commit("canon explicitly requiring one review")
        result = self.passage(policy_commit=commit).step(self.db, stamp("23"))
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(result["plan"]["proof"]["members"][0]["policy"]["reviews"], 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--supplier", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--test", action="append", help="Run only the named consumer witness")
    args = parser.parse_args()
    resolved = subprocess.check_output(["git", "-C", args.supplier, "rev-parse", "--verify",
                                        args.commit + "^{commit}"], text=True).strip()
    if args.commit != resolved:
        parser.error("full exact supplier commit required")
    content = subprocess.check_output(["git", "-C", args.supplier, "show",
                                       resolved + ":src/ameesh/mechanics.py"])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mechanics.py")
        with open(path, "wb") as fh:
            fh.write(content)
        spec = importlib.util.spec_from_file_location("ameesh.mechanics", path)
        supplier = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = supplier
        spec.loader.exec_module(supplier)
        names = [name for name in RealMechanicsTest.__dict__ if name.startswith("test_")]
        names += ["test_new_clone_reads_real_history_then_lands_exact_ci_head_across_connections",
                  "test_competing_runner_cannot_touch_the_reserved_train_or_share_its_clone",
                  "test_missing_provenance_cannot_publish_or_be_replaced_by_current_registry"]
        if args.test:
            if not set(args.test) <= set(names):
                parser.error("unknown consumer witness")
            names = args.test
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(
            RealMechanicsTest(name) for name in names))
        sys.exit(not result.wasSuccessful())
