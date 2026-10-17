# SPDX-License-Identifier: AGPL-3.0-only
"""Exact packet/member Git reads; storage and impact provenance are fixtures."""
import copy
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import merge_queue, radar, train, train_diagnosis, train_incidents


class PacketFactsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(folder.cleanup)
        self.path = folder.name
        self.git("init", "-q", "-b", "main")
        for name, value in (("user.name", "fixture"), ("user.email", "fixture@example.invalid"),
                            ("core.hooksPath", os.devnull)):
            self.git("config", name, value)
        root = self.commit("root.py", "root")
        self.git("checkout", "-qb", "lot/one")
        first = self.commit("one.py", "one")
        self.git("checkout", "main")
        self.commit("base-only.py", "target movement before second member")
        self.git("checkout", "-qb", "lot/two")
        second = self.commit("two with spaces.py", "two")
        self.git("checkout", "main")
        base = self.commit("new-base.py", "target movement after both branches")
        self.git("merge", "-q", "--no-ff", "--no-edit", first, second)
        tree = self.git("rev-parse", "HEAD^{tree}")
        head = self.git("commit-tree", tree, "-p", base, "-p", first, "-p", second,
                        "-m", "packet fixture")
        self.project = radar.Project(name="p", repo=self.path, target="main", github="o/r")
        source = merge_queue.source_of(self.path)
        members = [dict(work_item_id=i, generation=1, target="main", frozen_id=i + 100,
                        frozen_sha=sha, source=source, branch="lot/%s" % i)
                   for i, sha in ((1, first), (2, second))]
        self.plan = dict(schema=train.SCHEMA, source=source, target="main", base=base,
                         head=head, tree=tree, members=members,
                         proof=dict(schema="ameesh-train-admissions/1", source=source,
                                    base=base, members=[dict(admission=list(train.identity(m)))
                                                        for m in members]))
        self.state = dict(phase="landed", plan=self.plan)
        self.ops = mock.Mock()
        self.ops.train_read.side_effect = lambda key: copy.deepcopy(self.state)
        patch = mock.patch.object(train_incidents.storage, "of",
                                  return_value=types.SimpleNamespace(work=self.ops))
        patch.start()
        self.addCleanup(patch.stop)
        self.facts = train_incidents.PacketFacts(object(), self.project, self.path)
        self.assertNotEqual(root, base)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", self.path, *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def commit(self, file, text):
        with open(os.path.join(self.path, file), "w") as output:
            output.write(text)
        self.git("add", "--", file)
        self.git("commit", "-qm", text)
        return self.git("rev-parse", "HEAD")

    def test_members_keep_their_own_delta_without_target_or_other_lot_changes(self):
        rows = self.facts.lands("o/r", self.plan["head"])
        self.assertEqual([(r["lot"], r["files"]) for r in rows],
                         [(1, ["one.py"]), (2, ["two with spaces.py"])])
        self.assertEqual([r["author"] for r in rows], ["", ""])
        self.ops.train_read.assert_called_once_with("p:main")
        # Mutable branches and checkout contents cannot change past facts.
        self.commit("one.py", "later checkout change")
        self.git("branch", "-f", "lot/one", "main")
        self.assertEqual(self.facts.lands("o/r", self.plan["head"]), rows)

    def test_missing_manifest_uses_ordinary_reader_but_wrong_repo_never_uses_packet(self):
        fallback = mock.Mock(return_value=[{"lot": 8, "files": ["direct.py"]}])
        self.facts.fallback = fallback
        self.assertEqual(self.facts.lands("o/r", self.plan["base"]), fallback.return_value)
        fallback.assert_called_once_with("o/r", self.plan["base"])
        self.assertIsNone(self.facts._plan("other/repo", self.plan["head"]))
        self.assertIsNone(self.facts._plan("o/r", self.plan["head"][:12]))

    def test_contradictory_manifest_never_falls_back_to_aggregate_files(self):
        original = copy.deepcopy(self.state)
        cases = [lambda p: p.update(source="other-source"),
                 lambda p: p.update(target="develop"),
                 lambda p: p.update(tree="f" * 40),
                 lambda p: p["members"][0].update(frozen_sha=p["base"]),
                 lambda p: p["proof"]["members"].reverse(),
                 lambda p: p["members"].append(p["members"][0])]
        self.facts.fallback = mock.Mock(side_effect=AssertionError("unsafe fallback"))
        for mutate in cases:
            self.state = copy.deepcopy(original)
            mutate(self.state["plan"])
            with self.subTest(plan=self.state["plan"]), self.assertRaises(train.TrainError):
                self.facts.lands("o/r", self.plan["head"])
        self.facts.fallback.assert_not_called()

    def test_missing_landing_intent_and_shallow_history_are_refused(self):
        self.state["phase"] = "ci"
        with self.assertRaisesRegex(train.TrainError, "landing intent"):
            self.facts.lands("o/r", self.plan["head"])
        self.state["phase"] = "landed"
        with open(os.path.join(self.path, ".git", "shallow"), "w") as output:
            output.write(self.plan["head"] + "\n")
        with self.assertRaisesRegex(train.TrainError, "shallow"):
            self.facts.lands("o/r", self.plan["head"])

    def test_packet_parent_proof_is_checked_even_when_tree_matches(self):
        self.plan["head"] = self.git("commit-tree", self.plan["tree"], "-p", self.plan["base"],
                                     "-m", "same tree, missing member parents")
        with self.assertRaisesRegex(train.TrainError, "parents"):
            self.facts.lands("o/r", self.plan["head"])

    def test_diagnosis_must_cover_every_exact_packet_field(self):
        self.assertIsNone(self.facts.diagnoses("o/r", self.plan["head"]))
        report = train_diagnosis.unresolved(self.plan,
            dict(head=self.plan["head"], tree=self.plan["tree"], status="red", complete=True))
        self.state["diagnosis"] = report
        result = self.facts.diagnoses("o/r", self.plan["head"])
        self.assertEqual(result, report)
        result["missing"].clear()
        self.assertTrue(report["missing"])
        for field in ("source", "target", "base", "head", "tree"):
            with self.subTest(field=field):
                self.state["diagnosis"] = copy.deepcopy(report)
                self.state["diagnosis"]["packet"][field] = "different"
                self.assertIsNone(self.facts.diagnoses("o/r", self.plan["head"]))

    def test_absent_foreign_and_stale_maps_supply_no_impact_links(self):
        self.assertIsNone(self.facts.links_for("o/r", self.plan["head"]))
        impact = mock.Mock()
        self.facts.impact = impact
        for carte in (None, {}, {"schema": "other", "commit": self.plan["head"]},
                      {"schema": "ameesh-carte/1", "commit": self.plan["base"]}):
            impact.return_value = carte
            self.assertIsNone(self.facts.links_for("o/r", self.plan["head"]))
        impact.reset_mock()
        self.assertIsNone(self.facts.links_for("other/repo", self.plan["head"]))
        impact.assert_not_called()
