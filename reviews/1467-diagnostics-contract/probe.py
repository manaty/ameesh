# SPDX-License-Identifier: AGPL-3.0-only
"""Consumer-only #1467/#1681 probe: real Git, fixture storage/map provenance."""
import argparse
import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import ameesh
from ameesh import ci, train_diagnosis, train_incidents
from tests.test_1467_train_incidents import PacketFactsTest


class IncidentConsumerTest(PacketFactsTest):
    def map(self, commit):
        return dict(schema="ameesh-carte/1", commit=commit,
                    blocs={"one.py": {"changed": {"modules": ["test_one"]}}},
                    donnees={}, tables={}, migrations={})

    def test_exact_map_and_per_member_diffs_select_only_the_matching_lot(self):
        self.facts.impact = lambda repo, head: self.map(head)
        links = self.facts.links_for("o/r", self.plan["head"])
        result = supplier.attribute(["test_one"], self.facts.lands("o/r", self.plan["head"]), links)
        self.assertEqual((result["status"], result["lot"]), ("unique", 1))
        self.assertEqual(result["author"], "")
        self.assertEqual([r["lot"] for r in result["candidates"]], [1])

    def test_absent_or_stale_map_preserves_unknown_in_the_real_consumer(self):
        rows = self.facts.lands("o/r", self.plan["head"])
        for provider in (None, lambda repo, head: self.map(self.plan["base"])):
            self.facts.impact = provider
            links = self.facts.links_for("o/r", self.plan["head"])
            result = supplier.attribute(["test_one"], rows, links)
            self.assertEqual(result["status"], "unknown")
            self.assertIsNone(result["lot"])

    def test_shared_report_preserves_unknown_and_missing_facts(self):
        report = train_diagnosis.unresolved(self.plan, dict(head=self.plan["head"],
            tree=self.plan["tree"], status="red", complete=True,
            workflows=[dict(workflow="tests.yml", run=123, attempt=2,
                            jobs=[dict(id=456, name="suite", conclusion="failure")])]))
        self.state["diagnosis"] = report
        result = supplier.diagnosis_of(self.facts.diagnoses("o/r", self.plan["head"]),
                                       head_sha=self.plan["head"])
        self.assertEqual(result["attribution"]["status"], "unknown")
        self.assertEqual(result["environment"]["status"], "unknown")
        self.assertEqual(result["ci"][0]["attempt"], 2)
        self.assertEqual(result["missing"], report["missing"])

    def test_consume_routes_exact_map_and_manifest_without_cross_repository_assignment(self):
        self.routing(through_runner=False)

    def test_exact_runner_entry_routes_packet_and_uncovered_repository(self):
        self.routing(through_runner=True)

    def routing(self, *, through_runner):
        impact = mock.Mock(side_effect=lambda repo, head: self.map(head))
        self.facts.impact = impact
        self.ops.by_issue_ref.return_value = []
        self.ops.open_by_ref.return_value = []
        observed = [dict(repo=repo, branch="main", run_id=number, status="failure",
                         head_sha=head, workflow="tests", url="")
                    for repo, number, head in (("o/r", 123, self.plan["head"]),
                                               ("other/r", 456, "a" * 40))]
        watcher = mock.Mock()
        watcher.latest_concluded.return_value = observed
        watcher.head_replaced.return_value = False
        watcher.failures.return_value = [dict(module="test_one", test="test_one.test_changed")]
        db = object()
        passage = types.SimpleNamespace(policy=types.SimpleNamespace(enabled=True),
            incident_facts=mock.Mock(return_value=self.facts))
        fallback = mock.Mock(return_value=[dict(sha="a" * 40, lot=2, files=["one.py"])])
        result = []
        consume = supplier.consume

        def observed_consume(*args, **kwargs):
            lines = consume(*args, **kwargs)
            result.extend(lines)
            return lines

        with mock.patch.object(supplier, "INTEGRATION",
                               {"o/r": ("main", "p"), "other/r": ("main", "other")}), \
                mock.patch.object(supplier.storage, "of",
                                  return_value=types.SimpleNamespace(work=self.ops, ci=watcher)), \
                mock.patch.object(ci, "landed_commits", fallback, create=True), \
                mock.patch.object(supplier.work, "get", return_value={"assignee": "agent-one"}), \
                mock.patch.object(supplier.work, "add", side_effect=[{"id": 901}, {"id": 902}]), \
                mock.patch.object(supplier.work, "assign") as assigned, \
                mock.patch.object(supplier, "consume", side_effect=observed_consume) as called:
            if through_runner:
                runner_log.reset_mock()
                runner_entry(types.SimpleNamespace(train_passages=(passage,)), db)
                messages = [call.args[0] for call in runner_log.call_args_list]
                self.assertEqual(len(messages), 2)
                self.assertTrue(all("lot #" in message and "ouvert" in message
                                    for message in messages), messages)
            else:
                train_incidents.consume(db, [passage], consumer=supplier)
            called.assert_called_once()
        by_repo = {row["repo"]: row for row in result}
        self.assertEqual((by_repo["o/r"]["status"], by_repo["o/r"]["lot"]), ("unique", 1))
        self.assertEqual((by_repo["other/r"]["status"], by_repo["other/r"]["lot"]),
                         ("unknown", None))
        assigned.assert_called_once_with(db, 901, "agent-one", actor=supplier.work.SYSTEM_SENDER)
        impact.assert_called_once_with("o/r", self.plan["head"])
        fallback.assert_called_once_with("other/r", "a" * 40)
        passage.incident_facts.assert_called_once_with(db, fallback=fallback)

    def test_no_enabled_passage_keeps_the_ordinary_consumer_call(self):
        consumer = mock.Mock()
        db = object()
        passage = types.SimpleNamespace(policy=types.SimpleNamespace(enabled=False))
        result = train_incidents.consume(db, [passage], consumer=consumer)
        self.assertIs(result, consumer.consume.return_value)
        consumer.consume.assert_called_once_with(db)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--supplier", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    resolved = subprocess.check_output(["git", "-C", args.supplier, "rev-parse", "--verify",
                                        args.commit + "^{commit}"], text=True).strip()
    if args.commit != resolved:
        parser.error("full exact supplier commit required")
    source = subprocess.check_output(["git", "-C", args.supplier, "show",
                                      resolved + ":src/ameesh/incident.py"])
    runner_source = subprocess.check_output(["git", "-C", args.supplier, "show",
                                             resolved + ":src/ameesh/runner.py"])
    runner_class = next(node for node in ast.parse(runner_source).body
                        if isinstance(node, ast.ClassDef) and node.name == "Runner")
    entry = next(node for node in runner_class.body
                 if isinstance(node, ast.FunctionDef) and node.name == "incident_once")
    runner_log = mock.Mock()
    namespace = dict(__package__="ameesh", log_async=runner_log)
    exec(compile(ast.Module(body=[entry], type_ignores=[]),
                 "<exact supplier Runner.incident_once>", "exec"), namespace)
    runner_entry = namespace["incident_once"]
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
        path = os.path.join(directory, "incident.py")
        with open(path, "wb") as output:
            output.write(source)
        spec = importlib.util.spec_from_file_location("ameesh.incident", path)
        supplier = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = supplier
        spec.loader.exec_module(supplier)
        ameesh.incident = supplier
        names = [name for name in IncidentConsumerTest.__dict__ if name.startswith("test_")]
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(
            IncidentConsumerTest(name) for name in names))
    sys.exit(not result.wasSuccessful())
