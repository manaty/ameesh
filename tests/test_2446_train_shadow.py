# SPDX-License-Identifier: AGPL-3.0-only
"""#2446 : le mode ombre publie le plan calculé et le compare au composeur.

Le plan est calculé en lecture seule (inventaire, préparations, preuves
historiques) ; il ne réserve rien, ne pousse rien, ne lance aucune CI et ne
fusionne rien. La comparaison explique chaque lot manquant ou en trop.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import tempfile
import types
import unittest
from datetime import datetime
from functools import partial
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import (mechanics, merge_queue, plan_git, radar, storage,
                    train_incidents, train_initial, train_shadow, work)
from .support import PgTestCase
from .test_1467_train_review import author
from .test_2446_train_assembly import MECHANICS, _Repo

FEDERATION = """federation: "0.1"
id: manaty
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
        default: light
        rules: []
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      ameesh:
        rules:
          - paths:
              - "src/**"
              - "**/*.sql"
            reviews: 2
"""


class ProjectRepo:
    def __init__(self, root):
        self.root = root
        os.makedirs(root)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q", "-b", "main")
        self.write("README", "base\n")
        self.commit("base")
        self.origin = root + ".git"
        subprocess.run(["git", "init", "-q", "--bare", self.origin], check=True)
        self.git("remote", "add", "origin", self.origin)
        self.git("push", "-q", "origin", "main")

    def git(self, *args):
        proc = subprocess.run(["git", "-C", self.root, *args], capture_output=True,
                              text=True, env=self.env)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as target:
            target.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def branch(self):
        """A reviewed member branch, frozen and pushed."""
        self.git("checkout", "-qb", "lot/member")
        self.write("content", "member\n")
        head = self.commit("member")
        self.git("push", "-q", "origin", "lot/member")
        self.git("checkout", "-q", "main")
        return head


class ManifestTest(unittest.TestCase):
    def test_admissions_section_is_authoritative(self):
        text = """# Paquet

## Admissions (2)

- #2225
- #800 **+ #2084**

## Composition

| unité | lot |
|---|---|
| 1 | #1 |
"""
        self.assertEqual(train_shadow.parse_manifest(text), [2225, 800])

    def test_table_is_read_when_admissions_is_absent(self):
        text = """## Composition (2 unités)

| unité | lot | source |
|---|---|---|
| 1 | #2225 **+ #800** | x |
| 2 | #2242 | y |
"""
        self.assertEqual(train_shadow.parse_manifest(text), [2225, 800, 2242])

    def test_empty_or_foreign_text_names_no_lot(self):
        for text in ("", "# Paquet sans admissions\n", None):
            self.assertEqual(train_shadow.parse_manifest(text), [])

    def test_store_is_atomic_and_deduplicates_packets(self):
        with tempfile.TemporaryDirectory() as root:
            store = train_shadow.Store(os.path.join(root, "shadow.json"))
            state = store.load()
            plan = dict(cutoff=100.0, local_stamp="20261018-1100", members=[])
            state = store.record_plan(state, plan)
            state = store.record_plan(state, dict(plan, cutoff=101.0,
                                                  local_stamp="20261018-1200"))
            state = store.record_comparisons(state, [dict(
                packet=dict(sha="a" * 40, stamp="20261018-1100"), missing=[])])
            state = store.record_comparisons(state, [dict(
                packet=dict(sha="a" * 40, stamp="20261018-1100"), missing=[1])])
            self.assertEqual(len(state["plans"]), 2)
            self.assertEqual(len(state["comparisons"]), 1)
            self.assertEqual(state["comparisons"][0]["missing"], [])
            with open(store.path, encoding="utf-8") as source:
                self.assertEqual(json.load(source), state)


class ShadowPassageTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")
        self.project_repo = ProjectRepo(os.path.join(self.tmp, "project"))
        self.base = self.project_repo.git("rev-parse", "main")
        self.head = self.project_repo.branch()
        self.canon = _Repo(os.path.join(self.tmp, "canon"), federation_text=FEDERATION)
        self.commit = self.canon.git("rev-parse", "HEAD")
        self.item = work.add(self.db, title="shadow fixture", app="ameesh",
                             source="fixture", actor="fixture",
                             branch="lot/member", branch_target="main")
        self.ops = storage.of(self.db).work
        self.mark = self.ops.add_milestone(self.item["id"], "frozen", sha=self.head,
                                           actor="fixture", verdict=None, note="")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (self.item["id"],))
        self.now = datetime(2026, 10, 18, 11, 0,
                            tzinfo=ZoneInfo("Europe/Paris")).timestamp()
        self.notices = []
        self.receipts = {sha: dataclasses.asdict(author(sha)) for sha in (self.base, self.head)}
        provider = types.SimpleNamespace(
            ProvenanceError=RuntimeError,
            receipt_for_commit=lambda db, sha: self.receipts.get(sha),
            receipt_for_milestone=lambda db, mid: None)
        candidate = partial(plan_git.freeze_candidate,
                            repo=self.project_repo.root, target="main")
        self.project = radar.Project(name="ameesh", repo=self.project_repo.root,
                                     target="main", github="o/r")
        self.shadow = train_shadow.Passage(
            types.SimpleNamespace(state_dir=os.path.join(self.tmp, "state"),
                                  host="h", runner="r"),
            self.project, policy_path=self.canon.root, policy_commit=self.commit,
            workflows=(), provider=provider, mechanics=mechanics,
            repository=self.project_repo.root, candidate=candidate,
            initial=train_initial.Boundary(self.now - 60),
            notify=self.notices.append)

    def manual_packet(self, lots, stamp="20261018-1100"):
        self.project_repo.git("checkout", "-qb",
                              "integration/paquet-h-" + stamp, "main")
        self.project_repo.write("reviews/integration-paquet-h-%s.md" % stamp,
                                "# Paquet %s\n\n## Admissions\n\n%s\n\n"
                                % (stamp, "\n".join("- #%d" % lot for lot in lots)))
        self.project_repo.commit("composition %s" % stamp)
        self.project_repo.git("checkout", "-q", "main")
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/integration/paquet-h-" + stamp,
                              "integration/paquet-h-" + stamp)
        self.project_repo.git("push", "-q", "origin", "main",
                              "integration/paquet-h-" + stamp)

    def open_packet(self, lots, stamp="20261018-1000"):
        """A packet branch pushed but not merged: the composer is still on it."""
        self.project_repo.git("checkout", "-qb",
                              "integration/paquet-h-" + stamp, "main")
        self.project_repo.write("reviews/integration-paquet-h-%s.md" % stamp,
                                "# Paquet %s\n\n## Admissions\n\n%s\n\n"
                                % (stamp, "\n".join("- #%d" % lot for lot in lots)))
        self.project_repo.commit("composition %s" % stamp)
        self.project_repo.git("push", "-q", "origin",
                              "integration/paquet-h-" + stamp)
        self.project_repo.git("checkout", "-q", "main")

    def state_with_members(self, *lots, stamp="20261018-1100"):
        self.shadow.scheduled_step(self.db, self.now)
        state = self.shadow.store.load()
        plan = dict(state["plans"][-1])
        plan.update(local_stamp=stamp, cutoff=self.now,
                    members=[dict(work_item_id=lot) for lot in lots],
                    deferred=[])
        return dict(state, plans=[plan])

    @staticmethod
    def fake_packet(lots, stamp, created_ts, sha):
        return dict(sha=sha, stamp=stamp, manifest="reviews/paquet.md",
                    created_ts=created_ts, lots=list(lots))

    def test_shadow_incident_facts_are_empty_and_never_raise(self):
        """#2682 : le consommateur d'incidents interroge l'ombre sans lever."""
        calls = []

        class Consumer:
            INTEGRATION = {"o/r": "main"}

            def consume(self, db, *, lands, diagnoses, links_for):
                calls.append(db)
                return []

        fallback = lambda repo, head: [{"sha": head}]  # noqa: E731
        reader = self.shadow.incident_facts(self.db, fallback=fallback)
        self.assertIsInstance(reader, train_shadow.NoTrainFacts)
        self.assertEqual((reader.repo, reader.target), ("o/r", "main"))
        self.assertEqual(reader.lands("o/r", "a" * 40), [{"sha": "a" * 40}])
        self.assertIsNone(reader.diagnoses("o/r", "a" * 40))
        self.assertIsNone(reader.links_for("o/r", "a" * 40))
        self.assertEqual(train_incidents.consume(
            self.db, (self.shadow,), consumer=Consumer()), [])
        self.assertEqual(calls, [self.db])

    def test_extra_merged_after_the_cutoff_is_a_carrier_not_a_disagreement(self):
        carrier_sha = "c" * 40
        state = self.state_with_members(11, 22)
        state["comparisons"] = [dict(packet=dict(sha=carrier_sha))]
        packet = self.fake_packet([11], "20261018-1100", self.now + 60, "b" * 40)
        carrier = self.fake_packet([22], "20261018-1000", self.now + 30, carrier_sha)
        with mock.patch.object(self.shadow, "manual_packets",
                               return_value=[carrier, packet]), \
                mock.patch.object(self.shadow, "open_packets", return_value=[]):
            comparisons = self.shadow.compare(state, None)
        self.assertEqual(len(comparisons), 1)
        comparison = comparisons[0]
        self.assertEqual(comparison["extra"], [])
        self.assertEqual(comparison["carried"],
                         {"22": "integration/paquet-h-20261018-1000"})
        self.assertEqual(comparison["explanations"]["22"],
                         "porté par integration/paquet-h-20261018-1000 en vol")

    def test_extra_composed_in_an_open_packet_is_a_carrier_not_a_disagreement(self):
        state = self.state_with_members(11, 22)
        packet = self.fake_packet([11], "20261018-1100", self.now + 60, "b" * 40)
        carrier = dict(self.fake_packet([22], "20261018-1000", 0, "d" * 40),
                       branch="integration/paquet-h-20261018-1000")
        with mock.patch.object(self.shadow, "manual_packets", return_value=[packet]), \
                mock.patch.object(self.shadow, "open_packets", return_value=[carrier]):
            comparisons = self.shadow.compare(state, None)
        comparison = comparisons[0]
        self.assertEqual(comparison["extra"], [])
        self.assertEqual(comparison["carried"],
                         {"22": "integration/paquet-h-20261018-1000"})
        self.assertEqual(comparison["explanations"]["22"],
                         "porté par integration/paquet-h-20261018-1000 en vol")

    def test_a_later_open_packet_does_not_hide_a_real_extra(self):
        state = self.state_with_members(11, 22)
        packet = self.fake_packet([11], "20261018-1100", self.now + 60, "b" * 40)
        later = dict(self.fake_packet([22], "20261018-1200", 0, "d" * 40),
                     branch="integration/paquet-h-20261018-1200")
        with mock.patch.object(self.shadow, "manual_packets", return_value=[packet]), \
                mock.patch.object(self.shadow, "open_packets", return_value=[later]):
            comparisons = self.shadow.compare(state, None)
        comparison = comparisons[0]
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})
        self.assertEqual(comparison["explanations"]["22"],
                         "train would include it; composer did not")

    def test_a_packet_merged_before_the_cutoff_is_not_a_carrier(self):
        state = self.state_with_members(11, 22)
        packet = self.fake_packet([11], "20261018-1100", self.now + 60, "b" * 40)
        old = self.fake_packet([22], "20261018-1000", self.now - 60, "c" * 40)
        with mock.patch.object(self.shadow, "manual_packets",
                               return_value=[old, packet]), \
                mock.patch.object(self.shadow, "open_packets", return_value=[]):
            comparisons = self.shadow.compare(state, None)
        comparison = comparisons[0]
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})

    def test_open_packets_reads_the_branch_manifest_until_it_lands(self):
        self.shadow.scheduled_step(self.db, self.now)
        self.open_packet([999], stamp="20261018-1000")
        base = self.project_repo.git("rev-parse", "main")
        opened = self.shadow.open_packets(self.shadow.clone_path, base)
        self.assertEqual([(row["branch"], row["lots"]) for row in opened],
                         [("integration/paquet-h-20261018-1000", [999])])
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/integration/"
                              "paquet-h-20261018-1000",
                              "integration/paquet-h-20261018-1000")
        self.project_repo.git("push", "-q", "origin", "main")
        base = self.project_repo.git("rev-parse", "main")
        self.assertEqual(self.shadow.open_packets(self.shadow.clone_path, base), [])

    def test_comparison_excludes_a_lot_composed_in_an_open_packet(self):
        state = self.state_with_members(11, 22)
        self.open_packet([22], stamp="20261018-1000")
        self.manual_packet([11], stamp="20261018-1100")
        comparisons = self.shadow.compare(state, None)
        self.assertEqual(len(comparisons), 1)
        comparison = comparisons[0]
        self.assertEqual(comparison["packet"]["stamp"], "20261018-1100")
        self.assertEqual(comparison["missing"], [])
        self.assertEqual(comparison["extra"], [])
        self.assertEqual(comparison["carried"],
                         {"22": "integration/paquet-h-20261018-1000"})
        self.assertEqual(comparison["explanations"]["22"],
                         "porté par integration/paquet-h-20261018-1000 en vol")

    def test_shadow_plan_is_read_only_and_compares_equal_to_the_composer(self):
        result, deadline = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["result"], "shadow-plan", result)
        self.assertEqual(deadline, datetime(2026, 10, 18, 12, 0,
                                           tzinfo=ZoneInfo("Europe/Paris")).timestamp())
        self.assertEqual([row["work_item_id"] for row in result["plan"]["members"]],
                         [self.item["id"]])
        self.assertEqual(result["plan"]["deferred"], [])
        self.assertEqual(self.ops.queue(None), [])
        self.assertEqual(self.project_repo.git(
            "for-each-ref", "--format=%(refname)", "refs/heads/integration/"), "")
        # Two hours later the composer's packet for 11:00 is on main.
        self.manual_packet([self.item["id"]])
        result, _ = self.shadow.scheduled_step(self.db, self.now + 3600)
        self.assertEqual(result["result"], "shadow-plan", result)
        state = self.shadow.store.load()
        self.assertEqual(len(state["comparisons"]), 1)
        comparison = state["comparisons"][0]
        self.assertEqual(comparison["packet"]["stamp"], "20261018-1100")
        self.assertEqual(comparison["plan"]["matched"], "stamp")
        self.assertEqual((comparison["missing"], comparison["extra"]), ([], []))
        self.assertEqual(self.ops.queue(None), [])

    def test_missing_lot_is_named_and_explained(self):
        self.shadow.scheduled_step(self.db, self.now)
        self.manual_packet([self.item["id"], 999])
        self.shadow.scheduled_step(self.db, self.now + 3600)
        comparison = self.shadow.store.load()["comparisons"][0]
        self.assertEqual(comparison["missing"], [999])
        self.assertEqual(comparison["extra"], [])
        self.assertIn("not a candidate", comparison["explanations"]["999"])

    def test_withdrawal_receipt_is_the_same_refusal_as_active_enrollment(self):
        self.db.execute(
            "INSERT INTO integration_train_queue_receipts"
            " (work_item_id, generation, frozen_id, kind) VALUES (%s, 1, %s, 'removed')",
            (self.item["id"], self.mark["id"]))
        result, _ = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["plan"]["members"], [])
        reasons = {row["work_item_id"]: row["reason"]
                   for row in result["plan"]["deferred"]}
        self.assertIn("active enrollment would refuse", reasons[self.item["id"]])
        self.assertEqual(self.ops.queue(None), [])

    def test_an_existing_queue_row_without_allocation_receipt_is_not_a_member(self):
        source = merge_queue.source_of(self.project_repo.root)
        self.ops.queue_add(self.item["id"], target="main", frozen_sha=self.head,
                           frozen_id=self.mark["id"], source=source, actor="fixture",
                           note="")
        self.ops.queue_update(self.item["id"], state="ejected", actor="fixture",
                              note="legacy ejection without packet receipt")
        result, _ = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["plan"]["members"], [])
        self.assertTrue(any(row["work_item_id"] == self.item["id"]
                            and "active enrollment would refuse" in row["reason"]
                            for row in result["plan"]["deferred"]))

    def test_freeze_before_the_activation_boundary_is_not_a_member(self):
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE id = %s",
                        (self.now - 7200, self.mark["id"]))
        result, _ = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["plan"]["members"], [])
        self.assertTrue(any(row["work_item_id"] == self.item["id"]
                            and "active enrollment would refuse" in row["reason"]
                            for row in result["plan"]["deferred"]))

    def test_packets_merged_before_the_first_plan_are_not_compared(self):
        self.shadow.scheduled_step(self.db, self.now)
        state = self.shadow.store.load()
        old = dict(sha="a" * 40, stamp="20261017-1100", manifest="old.md",
                   created_ts=state["plans"][0]["cutoff"] - 3600, lots=[999])
        # A packet named for an earlier hour but merged after the shadow began
        # is not matched to the first plan by proximity either.
        late = dict(sha="b" * 40, stamp="20261017-1200", manifest="old.md",
                    created_ts=state["plans"][0]["cutoff"] + 60, lots=[998])
        with mock.patch.object(self.shadow, "manual_packets", return_value=[old, late]):
            self.assertEqual(self.shadow.compare(state, None), [])

    def test_same_cutoff_is_published_once(self):
        first, _ = self.shadow.scheduled_step(self.db, self.now)
        second, _ = self.shadow.scheduled_step(self.db, self.now + 10)
        self.assertEqual(first["result"], "shadow-plan")
        self.assertEqual(second["result"], "shadow-idle")
        state = self.shadow.store.load()
        self.assertEqual(len(state["plans"]), 1)
        self.assertEqual(state["plans"][0]["cutoff"], self.now)


if __name__ == "__main__":
    unittest.main()
