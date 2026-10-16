# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-branch #1467/#1616/#1757 probe; Git/SQL real, CI/provenance fixtures.

PYTHONPATH=src ameesh heavy -- .venv/bin/python reviews/1467-mechanics-port/probe.py \
    --supplier . --commit <exact-1757-supplier-commit>

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
from datetime import date, datetime
from unittest import mock

from ameesh import db as db_mod, train, train_ci, train_policy
from tests.test_1467_train_runner import TrainRunnerTest

MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
packets:
  cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
merge:
  named_fire: true
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
projects: {}
"""


def stamp(hour):
    return datetime.fromisoformat("2026-10-15T%s:00:00+02:00" % hour).timestamp()


def verify_pinned_canon_calendar(rules):
    """Check the pinned canon through the consumer's scheduling port."""
    check = unittest.TestCase()
    check.assertEqual(rules.timezone, "Europe/Paris")
    check.assertEqual(rules.cutoffs, ("11:00", "23:00"))
    check.assertEqual(rules.packet_cutoffs, tuple("%02d:00" % hour for hour in range(24)))
    policy = train.Policy(enabled=True, schedule=rules)
    counts = {}
    for day, count, repeated in (("2026-10-15", 24, 1),
                                 ("2026-03-29", 23, 0),
                                 ("2026-10-25", 25, 2)):
        packets = rules.packets_on(date.fromisoformat(day))
        epochs = [at for at, _ in packets]
        check.assertEqual(len(packets), count)
        check.assertEqual(epochs, sorted(set(epochs)))
        check.assertEqual(sum(label == "02:00" for _, label in packets), repeated)
        for index, at in enumerate(epochs):
            check.assertEqual(policy.cutoff(at), at)
            check.assertEqual(policy.cutoff(at + 5), at)
            check.assertEqual(rules.next_packet(at - 1), at)
            if index + 1 < len(epochs):
                check.assertEqual(rules.next_packet(at), epochs[index + 1])
        counts[day] = count
    for first, second in (("2026-10-15T23:00:00+02:00", "2026-10-16T00:00:00+02:00"),
                          ("2026-03-29T01:00:00+01:00", "2026-03-29T03:00:00+02:00"),
                          ("2026-10-25T02:00:00+02:00", "2026-10-25T02:00:00+01:00")):
        before, after = [datetime.fromisoformat(at).timestamp() for at in (first, second)]
        check.assertEqual(rules.next_packet(before), after)
        check.assertEqual(policy.cutoff(after - 1), before)
        check.assertEqual(policy.cutoff(after), after)
    check.assertEqual(rules.cutoff(stamp("12")), stamp("11"))
    check.assertEqual(policy.cutoff(stamp("12")), stamp("12"))
    check.assertEqual(rules.next_packet(stamp("12")), stamp("13"))
    return counts


class RealMechanicsTest(TrainRunnerTest):
    def dst_passage(self, epochs):
        # The shared SQL/Git assertions must use the exact supplier's calendar.
        if not hasattr(self, "dst_canon"):
            self.dst_canon = self.hourly_canon()
        return self.passage(policy_commit=self.dst_canon)

    def hourly_canon(self):
        hours = json.dumps(["%02d:00" % hour for hour in range(24)])
        return self.revise(MECHANICS.replace('  cutoffs: ["11:00", "23:00"]',
                                            '  cutoffs: ' + hours)
                           .replace("named_fire: true", "named_fire: false"))

    def test_real_hourly_calendar_keeps_pending_packet_across_restart(self):
        commit = self.hourly_canon()
        passage = self.passage(policy_commit=commit)
        rules = passage.runtime(self.db).policy.schedule
        self.assertEqual(rules.cutoff(stamp("12")), stamp("11"))
        self.assertEqual(rules.last_packet(stamp("12")), stamp("12"))
        first, deadline = passage.scheduled_step(self.db, stamp("12"))
        self.assertEqual(first["result"], "ci", first)
        self.assertEqual(deadline, stamp("13"))
        plan = first["plan"]
        other_db = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other_db.close)
        resumed = self.passage(policy_commit=commit)
        pending = dict(head=plan["head"], tree=plan["tree"],
                       status="pending", complete=False)
        with mock.patch.object(train_ci.GitHub, "read", return_value=pending):
            result, deadline = resumed.scheduled_step(other_db, stamp("13"))
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(train.digest(result["plan"]), train.digest(plan))
        self.assertEqual(deadline, stamp("14"))
        self.assertEqual(len(self.ops.train_read(self.key)["attempts"]), 1)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/").splitlines(),
                         ["refs/heads/" + plan["branch"]])
        result, _ = resumed.scheduled_step(other_db, stamp("13") + 1)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
        self.assertEqual(len(self.ops.train_read(self.key)["attempts"]), 1)

    def test_sparse_calendar_resumes_after_a_day_without_a_packet(self):
        commit = self.revise(MECHANICS.replace('  cutoffs: ["11:00", "23:00"]',
                                              '  cutoffs: ["02:00"]')
                             .replace("named_fire: true", "named_fire: false"))
        queued, before, due = [datetime.fromisoformat(value).timestamp() for value in (
            "2026-03-28T03:00:00+01:00", "2026-03-30T01:59:59+02:00",
            "2026-03-30T02:00:00+02:00")]
        self.db.execute("UPDATE merge_queue SET queued_at=to_timestamp(%s)", [queued])
        passage = self.passage(policy_commit=commit)
        result, deadline = passage.scheduled_step(self.db, queued)
        self.assertEqual(result["result"], "idle", result)
        self.assertEqual(deadline, due)
        other_db = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other_db.close)
        resumed = self.passage(policy_commit=commit)
        rules = resumed.runtime(other_db).policy.schedule
        previous = datetime.fromisoformat("2026-03-28T02:00:00+01:00").timestamp()
        self.assertEqual(train.Policy(schedule=rules).cutoff(before), previous)
        result, deadline = resumed.scheduled_step(other_db, before)
        self.assertEqual(result["result"], "idle", result)
        self.assertEqual(deadline, due)
        self.assertEqual(self.api_calls, [])
        first, deadline = resumed.scheduled_step(other_db, due)
        self.assertEqual(first["result"], "ci", first)
        self.assertEqual(deadline,
                         datetime.fromisoformat("2026-03-31T02:00:00+02:00").timestamp())
        result, _ = self.passage(policy_commit=commit).scheduled_step(self.db, due + 1)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), first["plan"]["head"])
        self.assertEqual(len(self.ops.train_read(self.key)["attempts"]), 1)

    def test_real_withdrawal_keeps_ci_barrier_on_fresh_connection(self):
        commit = self.hourly_canon()
        passage = self.passage(policy_commit=commit)
        first, _ = passage.scheduled_step(self.db, stamp("12"))
        self.assertEqual(first["result"], "ci", first)
        plan = first["plan"]
        self.ops.queue_remove(plan["members"][0]["work_item_id"],
                              note="fixture withdrawal", actor="fixture")
        result, _ = passage.scheduled_step(self.db, stamp("12") + 1)
        self.assertEqual(result["result"], "refused", result)
        self.assertEqual(self.ops.train_read(self.key)["phase"], "draining")
        other_db = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other_db.close)
        resumed = self.passage(policy_commit=commit)
        pending = dict(head=plan["head"], tree=plan["tree"],
                       status="pending", complete=False)
        with mock.patch.object(train_ci.GitHub, "read", return_value=pending):
            result, deadline = resumed.scheduled_step(other_db, stamp("13"))
        self.assertEqual(result["result"], "draining", result)
        self.assertEqual(deadline, stamp("14"))
        self.assertEqual(len(self.ops.train_read(self.key)["attempts"]), 1)
        result, _ = resumed.scheduled_step(other_db, stamp("13") + 1)
        self.assertEqual(result["result"], "idle", result)
        self.assertEqual(self.ops.train_read(self.key)["phase"], "invalidated")
        self.assertIsNone(resumed.epoch)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_real_canon_merge_flag_lands_without_a_named_fire(self):
        commit = self.revise(MECHANICS.replace("named_fire: true", "named_fire: false"))
        passage = self.passage(policy_commit=commit)
        first = passage.step(self.db, self.now)
        self.assertEqual(first["result"], "ci", first)
        result = passage.step(self.db, self.now + 1)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), first["plan"]["head"])
        self.assertIsNone(self.ops.train_fire_read(self.key, first["plan"]["id"]))
        receipt = self.ops.train_read(self.key)["fire"]
        self.assertEqual(receipt["policy_commit"], commit)
        self.assertFalse(receipt["merge"]["named_fire"])

    def test_absent_merge_clause_is_not_an_implicit_named_fire(self):
        start = MECHANICS.index("merge:\n")
        end = MECHANICS.index("reviews:\n", start)
        commit = self.revise(MECHANICS[:start] + MECHANICS[end:])
        with self.assertRaises(train.TrainError):
            self.passage(policy_commit=commit).step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])
        self.assertEqual(self.api_calls, [])

    def test_absent_packet_calendar_never_uses_release_cutoffs(self):
        document = MECHANICS.replace('packets:\n  cutoffs: ["11:00", "23:00"]\n', "")
        commit = self.revise(document)
        with self.assertRaisesRegex(train.TrainError, "explicit compiled packet calendar"):
            self.passage(policy_commit=commit).step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])
        self.assertEqual(self.api_calls, [])

    def test_real_next_cutoff_and_engine_use_the_changed_canon(self):
        self.queued_at_ten()
        commit = self.revise(MECHANICS.replace('  cutoffs: ["11:00", "23:00"]',
                                              '  cutoffs: ["12:00", "23:00"]'))
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
        commit = self.revise(MECHANICS.replace('  cutoffs: ["11:00", "23:00"]',
                                              '  cutoffs: ["12:00", "23:00"]'))
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
    parser.add_argument("--canon", help="Also read the candidate canon objects, never its checkout")
    parser.add_argument("--canon-commit", help="Full exact candidate canon commit")
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
        if bool(args.canon) != bool(args.canon_commit):
            parser.error("--canon and --canon-commit must be supplied together")
        if args.canon:
            examples = {
                "ameesh": {".github/workflows/tests.yml": 1, "src/ameesh/identity.py": 2,
                           "src/ameesh/migrations/0001.sql": 2},
                "nexlink": {".github/workflows/backend-suite.yml": 1,
                            "infra/compute-build/Dockerfile": 1,
                            "scripts/deploy-staging.sh": 1, "scripts/deploy-prod.sh": 2,
                            "supabase/migrations/0001.sql": 2,
                            "backend/src/modules/payments/payments.service.ts": 2,
                            "backend/src/modules/messaging/messaging.service.ts": 1},
            }
            for project, paths in examples.items():
                rules = supplier.load(args.canon, args.canon_commit, project)
                compiled = train_policy.Compiled(rules)
                actual = {path: compiled._need([path])[1] for path in paths}
                if actual != paths or rules.merge_named_fire is not False \
                        or rules.merge_repeat_freeze is not False \
                        or rules.merge_on_green_packet is not True:
                    raise AssertionError((project, actual, rules.to_dict()))
                calendar = verify_pinned_canon_calendar(rules)
                print(json.dumps(dict(project=project, commit=rules.commit, reviews=actual,
                                      merge=rules.to_dict()["merge"], packets=calendar)), flush=True)
        names = [name for name in RealMechanicsTest.__dict__ if name.startswith("test_")]
        names += ["test_new_clone_reads_real_history_then_lands_exact_ci_head_across_connections",
                  "test_competing_runner_cannot_touch_the_reserved_train_or_share_its_clone",
                  "test_missing_provenance_cannot_publish_or_be_replaced_by_current_registry",
                  "test_pending_packet_survives_repeated_hour_on_fresh_connection",
                  "test_pending_packet_survives_spring_gap_on_fresh_connection"]
        if args.test:
            if not set(args.test) <= set(names):
                parser.error("unknown consumer witness")
            names = args.test
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(
            RealMechanicsTest(name) for name in names))
        sys.exit(not result.wasSuccessful())
