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
from datetime import datetime, timezone
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
    def __init__(self, root, *, base_at=None):
        self.root = root
        os.makedirs(root)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q", "-b", "main")
        self.write("README", "base\n")
        self.commit("base", at=base_at)
        self.origin = root + ".git"
        subprocess.run(["git", "init", "-q", "--bare", self.origin], check=True)
        self.git("remote", "add", "origin", self.origin)
        self.git("push", "-q", "origin", "main")

    @staticmethod
    def date_env(at):
        if at is None:
            return {}
        stamp = datetime.fromtimestamp(float(at), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}

    def git(self, *args, env=None):
        merged = dict(self.env, **(env or {}))
        proc = subprocess.run(["git", "-C", self.root, *args], capture_output=True,
                              text=True, env=merged)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as target:
            target.write(text)

    def commit(self, message, *, at=None):
        self.git("add", "-A")
        self.git("commit", "-qm", message, env=self.date_env(at))
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
        self.now = datetime(2026, 10, 18, 11, 0,
                            tzinfo=ZoneInfo("Europe/Paris")).timestamp()
        self.project_repo = ProjectRepo(os.path.join(self.tmp, "project"),
                                        base_at=self.now - 86400)
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
        # #2965 : un gel réel est antérieur au créneau qu'il alimente. Le
        # dater avant le cutoff évite qu'un plan d'ombre ne voie la porte
        # s'ouvrir « après » le créneau par simple horloge de fixture.
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE id = %s", (self.now - 30, self.mark["id"]))
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (self.item["id"],))
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

    @staticmethod
    def tail_of(stamp):
        return "paquet-h-" + stamp

    @classmethod
    def branch_of(cls, stamp):
        return "integration/" + cls.tail_of(stamp)

    def write_manifest(self, tail, lots, *, name=None):
        path = "reviews/%s.md" % (name or "integration-" + tail)
        self.project_repo.write(path, "# Paquet %s\n\n## Admissions\n\n%s\n\n"
                                % (tail, "\n".join("- #%d" % lot for lot in lots)))
        return path

    def open_packet(self, lots, stamp="20261018-1000", *, at=None, name=None):
        """A packet branch pushed but not merged: the composer is still on it."""
        branch = self.branch_of(stamp)
        self.project_repo.git("checkout", "-qb", branch, "main")
        self.write_manifest(self.tail_of(stamp), lots, name=name)
        self.project_repo.commit("composition %s" % stamp, at=at)
        self.project_repo.git("checkout", "-q", "main")
        self.project_repo.git("push", "-q", "origin", branch)
        return branch

    def enrich_packet(self, branch, lots, *, at=None, name=None):
        """The composer adds a lot to an already pushed packet branch."""
        self.project_repo.git("checkout", "-q", branch)
        self.write_manifest(branch.split("/", 1)[1], lots, name=name)
        self.project_repo.commit("composition enrichie", at=at)
        self.project_repo.git("checkout", "-q", "main")
        self.project_repo.git("push", "-q", "origin", branch)

    def manual_packet(self, lots, stamp="20261018-1100", *, branch_at=None,
                      merge_at=None):
        branch = self.branch_of(stamp)
        self.project_repo.git("checkout", "-qb", branch, "main")
        self.write_manifest(self.tail_of(stamp), lots)
        self.project_repo.commit("composition %s" % stamp, at=branch_at)
        self.project_repo.git("checkout", "-q", "main")
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/" + branch, branch,
                              env=self.project_repo.date_env(merge_at))
        self.project_repo.git("push", "-q", "origin", "main", branch)
        return branch

    def state_with_members(self, *lots, stamp="20261018-1100"):
        self.shadow.scheduled_step(self.db, self.now)
        state = self.shadow.store.load()
        plan = dict(state["plans"][-1])
        plan.update(local_stamp=stamp, cutoff=self.now,
                    members=[dict(work_item_id=lot) for lot in lots],
                    deferred=[])
        return dict(state, plans=[plan])

    def train_row(self, *, queued_at=None):
        """#2965 : une ligne enrôlée par le train, avec son reçu « packet »."""
        row = self.ops.queue_add(
            self.item["id"], target="main", frozen_sha=self.head,
            frozen_id=self.mark["id"], source=merge_queue.source_of(
                self.project_repo.root), actor="fixture", note="")
        self.assertIsNotNone(row)
        self.db.execute(
            "INSERT INTO integration_train_queue_receipts"
            " (work_item_id, generation, frozen_id, kind) VALUES (%s, %s, %s, 'packet')",
            (self.item["id"], row["generation"], self.mark["id"]))
        if queued_at is not None:
            self.db.execute("UPDATE merge_queue SET queued_at = to_timestamp(%s)"
                            " WHERE work_item_id = %s",
                            (queued_at, self.item["id"]))
        return self.ops.queue(None)[0]

    def dated_mark(self, kind, actor, *, stamp, verdict=None, sha=None, harness=None):
        mark = self.ops.add_milestone(self.item["id"], kind, sha=sha or self.head,
                                      actor=actor, verdict=verdict, note="")
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE id = %s", (stamp, mark["id"]))
        if harness is not None:
            self.db.execute("INSERT INTO turn_costs (agent, harness, session,"
                            " recorded_at) VALUES (%s, %s, %s, to_timestamp(%s))",
                            (actor, harness, "s-" + actor, stamp))
        return mark

    def numbered_packet(self, lots, name="paquet-97-nexlink", *, branch_at=None,
                        merge_at=None, merge=True, manifest=True):
        """A numbered packet (`integration/paquet-<n>-nexlink`, #2733)."""
        branch = "integration/" + name
        self.project_repo.git("checkout", "-qb", branch, "main")
        if manifest:
            self.write_manifest(name, lots)
            self.project_repo.commit("composition %s" % name, at=branch_at)
        else:
            # The numbered composer declares its units in the commit trailers.
            for lot in lots:
                self.project_repo.write("packet/%s.txt" % lot, "%s\n" % lot)
                self.project_repo.commit(
                    "composition %s #%d\n\nameesh-work: %d" % (name, lot, lot),
                    at=branch_at)
        self.project_repo.git("checkout", "-q", "main")
        if merge:
            self.project_repo.git("merge", "--no-ff", "-m",
                                  "Merge #2732 « paquet 97 (integration/%s) »" % name,
                                  branch, env=self.project_repo.date_env(merge_at))
            self.project_repo.git("push", "-q", "origin", "main")
        self.project_repo.git("push", "-q", "origin", branch)
        return branch

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

    def test_a_lot_added_to_a_packet_after_the_cutoff_stays_an_extra(self):
        """B1 dev4/cx3 : le manifeste final ne prouve pas la composition au cutoff."""
        state = self.state_with_members(11, 22)
        cutoff = self.now
        branch = self.open_packet([11], stamp="20261018-1000", at=cutoff - 3600)
        self.enrich_packet(branch, [11, 22], at=cutoff + 1200)
        self.project_repo.git("checkout", "-q", "main")
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/" + branch, branch,
                              env=self.project_repo.date_env(cutoff + 1800))
        self.project_repo.git("push", "-q", "origin", "main", branch)
        self.manual_packet([11], stamp="20261018-1100",
                           branch_at=cutoff + 1860, merge_at=cutoff + 1920)
        comparisons = self.shadow.compare(state, None)
        comparison = next(row for row in comparisons
                          if row["packet"]["stamp"] == "20261018-1100")
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})
        self.assertEqual(comparison["explanations"]["22"],
                         "train would include it; composer did not")

    def test_a_variant_manifest_is_never_a_carrier_and_the_extra_is_unknown(self):
        """B2 cx3 : pas de repli sur un manifeste voisin (ni names[-1])."""
        state = self.state_with_members(11, 22)
        cutoff = self.now
        branch = self.open_packet(
            [22], stamp="20261018-1000", at=cutoff - 3600,
            name="integration-paquet-h-20261018-1000c")
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/" + branch, branch,
                              env=self.project_repo.date_env(cutoff + 1800))
        self.project_repo.git("push", "-q", "origin", "main", branch)
        self.manual_packet([11], stamp="20261018-1100",
                           branch_at=cutoff + 1860, merge_at=cutoff + 1920)
        comparisons = self.shadow.compare(state, None)
        comparison = next(row for row in comparisons
                          if row["packet"]["stamp"] == "20261018-1100")
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})
        self.assertIn("porteur inconnu", comparison["explanations"]["22"])

    def test_a_carrier_without_commit_before_the_cutoff_keeps_the_extra_unknown(self):
        """Sans composition prouvée au cutoff, le lot reste extra/inconnu."""
        state = self.state_with_members(11, 22)
        cutoff = self.now
        branch = self.open_packet([22], stamp="20261018-1000", at=cutoff + 1200)
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/" + branch, branch,
                              env=self.project_repo.date_env(cutoff + 1800))
        self.project_repo.git("push", "-q", "origin", "main", branch)
        self.manual_packet([11], stamp="20261018-1100",
                           branch_at=cutoff + 1860, merge_at=cutoff + 1920)
        comparisons = self.shadow.compare(state, None)
        comparison = next(row for row in comparisons
                          if row["packet"]["stamp"] == "20261018-1100")
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})
        self.assertIn("porteur inconnu", comparison["explanations"]["22"])

    def test_a_lot_composed_in_an_open_packet_before_the_cutoff_is_carried(self):
        state = self.state_with_members(11, 22)
        cutoff = self.now
        self.open_packet([22], stamp="20261018-1000", at=cutoff - 900)
        self.manual_packet([11], stamp="20261018-1100",
                           branch_at=cutoff + 60, merge_at=cutoff + 120)
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

    def test_an_open_packet_created_after_the_cutoff_does_not_mask_the_extra(self):
        state = self.state_with_members(11, 22)
        cutoff = self.now
        self.open_packet([22], stamp="20261018-1000", at=cutoff + 1200)
        self.manual_packet([11], stamp="20261018-1100",
                           branch_at=cutoff + 60, merge_at=cutoff + 120)
        comparisons = self.shadow.compare(state, None)
        comparison = comparisons[0]
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})

    def test_an_abandoned_open_packet_is_not_a_carrier(self):
        """Des branches integration/** jamais fusionnées ne masquent pas un écart."""
        state = self.state_with_members(11, 22)
        cutoff = self.now
        self.open_packet([22], stamp="20261015-2310", at=cutoff - 2 * 86400)
        self.manual_packet([11], stamp="20261018-1100",
                           branch_at=cutoff + 60, merge_at=cutoff + 120)
        comparisons = self.shadow.compare(state, None)
        comparison = comparisons[0]
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})
        self.assertEqual(comparison["explanations"]["22"],
                         "train would include it; composer did not")

    def test_a_packet_merged_before_the_cutoff_is_not_a_carrier(self):
        state = self.state_with_members(11, 22)
        cutoff = self.now
        self.manual_packet([22], stamp="20261018-1000",
                           branch_at=cutoff - 3600, merge_at=cutoff - 600)
        self.manual_packet([11], stamp="20261018-1100",
                           branch_at=cutoff + 60, merge_at=cutoff + 120)
        comparisons = self.shadow.compare(state, None)
        comparison = next(row for row in comparisons
                          if row["packet"]["stamp"] == "20261018-1100")
        self.assertEqual(comparison["extra"], [22])
        self.assertEqual(comparison["carried"], {})

    def test_open_packets_lists_only_branches_not_landed(self):
        self.shadow.scheduled_step(self.db, self.now)
        branch = self.open_packet([999], stamp="20261018-1000",
                                  at=self.now - 900)
        base = self.project_repo.git("rev-parse", "main")
        opened = self.shadow.open_packets(self.shadow.clone_path, base)
        self.assertEqual([row["branch"] for row in opened], [branch])
        self.assertEqual(opened[0]["tail"], "paquet-h-20261018-1000")
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/" + branch, branch,
                              env=self.project_repo.date_env(self.now + 60))
        self.project_repo.git("push", "-q", "origin", "main")
        base = self.project_repo.git("rev-parse", "main")
        self.assertEqual(self.shadow.open_packets(self.shadow.clone_path, base), [])

    def test_a_numbered_manual_packet_is_compared_by_its_merge_date(self):
        """#2733 : `integration/paquet-<n>-nexlink` n'a pas d'heure dans son nom."""
        state = self.state_with_members(self.item["id"])
        self.numbered_packet([self.item["id"]], merge_at=self.now + 120)
        rules = types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris"))
        comparisons = self.shadow.compare(state, rules)
        self.assertEqual(len(comparisons), 1)
        comparison = comparisons[0]
        self.assertEqual(comparison["packet"]["tail"], "paquet-97-nexlink")
        self.assertEqual(comparison["packet"]["stamp"], "20261018-1102")
        self.assertEqual((comparison["missing"], comparison["extra"]), ([], []))

    def test_a_numbered_packet_without_manifest_uses_work_trailers(self):
        """#2733 : le composeur numéroté n'écrit pas de manifeste."""
        state = self.state_with_members(self.item["id"])
        self.numbered_packet([self.item["id"]], merge_at=self.now + 120,
                             manifest=False)
        rules = types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris"))
        comparisons = self.shadow.compare(state, rules)
        self.assertEqual(len(comparisons), 1)
        comparison = comparisons[0]
        self.assertEqual(comparison["packet"]["manifest"], None)
        self.assertEqual(comparison["packet"]["lots"], [self.item["id"]])
        self.assertEqual((comparison["missing"], comparison["extra"]), ([], []))

    def test_open_numbered_packet_is_read_with_its_head_date(self):
        self.shadow.scheduled_step(self.db, self.now)
        self.numbered_packet([999], merge=False, branch_at=self.now - 900)
        base = self.project_repo.git("rev-parse", "main")
        opened = self.shadow.open_packets(self.shadow.clone_path, base,
                                          ZoneInfo("Europe/Paris"))
        self.assertEqual([row["tail"] for row in opened], ["paquet-97-nexlink"])
        self.assertEqual(opened[0]["stamp"], "20261018-1045")

    def test_numbered_packet_merged_after_cutoff_is_a_carrier(self):
        state = self.state_with_members(11, 22)
        self.numbered_packet([22], name="paquet-96-nexlink",
                             branch_at=self.now - 900, merge_at=self.now + 120)
        self.numbered_packet([11], branch_at=self.now + 180,
                             merge_at=self.now + 240)
        comparisons = self.shadow.compare(state, types.SimpleNamespace(
            zone=lambda: ZoneInfo("Europe/Paris")))
        comparison = next(row for row in comparisons
                          if row["packet"]["tail"] == "paquet-97-nexlink")
        self.assertEqual(comparison["extra"], [])
        self.assertEqual(comparison["carried"],
                         {"22": "integration/paquet-96-nexlink"})

    def test_numbered_open_packet_enriched_after_cutoff_uses_prior_manifest(self):
        state = self.state_with_members(11, 22, 33)
        branch = self.numbered_packet([22], name="paquet-96-nexlink",
                                      merge=False, branch_at=self.now - 900)
        self.enrich_packet(branch, [22, 33], at=self.now + 120)
        self.numbered_packet([11], branch_at=self.now + 180,
                             merge_at=self.now + 240)
        comparison = self.shadow.compare(state, types.SimpleNamespace(
            zone=lambda: ZoneInfo("Europe/Paris")))[0]
        self.assertEqual(comparison["extra"], [33])
        self.assertEqual(comparison["carried"],
                         {"22": "integration/paquet-96-nexlink"})

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

    def test_a_train_row_admissible_before_the_cutoff_is_a_member(self):
        """#2965 (B3, parité) : une ligne enrôlée après le cutoff mais dont
        l'admission (gel + porte) est antérieure y est membre, comme en actif."""
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE id = %s", (self.now - 600, self.mark["id"]))
        row = self.train_row()
        self.assertGreater(row["queued_ts"], self.now)
        result, _ = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["result"], "shadow-plan", result)
        self.assertEqual([member["work_item_id"] for member in result["plan"]["members"]],
                         [self.item["id"]], result["plan"]["deferred"])
        self.assertEqual(result["plan"]["deferred"], [])

    def test_a_train_row_admissible_after_the_cutoff_is_late(self):
        """#2965 (B3) : un dernier avis après le cutoff rend la ligne late,
        même si sa file a été écrite avant — l'ombre lit l'instant d'admission."""
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE id = %s", (self.now - 600, self.mark["id"]))
        self.train_row(queued_at=self.now - 300)
        self.dated_mark("verdict", "r1", stamp=self.now + 60, verdict="blocked",
                        harness="claude")
        self.dated_mark("verdict", "r1", stamp=self.now + 120, verdict="ok",
                        harness="claude")
        result, _ = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["result"], "shadow-plan", result)
        self.assertEqual(result["plan"]["members"], [])
        reasons = {row["work_item_id"]: row["reason"]
                   for row in result["plan"]["deferred"]}
        self.assertIn("late admission", reasons[self.item["id"]])

    def test_a_candidate_whose_gate_opens_after_the_cutoff_is_late(self):
        """#2965 (b, ombre) : le dernier avis tombe à slot+60, la porte ne
        s'ouvre qu'à slot+120 : le candidat est late, comme dans l'actif."""
        self.dated_mark("verdict", "r1", stamp=self.now + 60, verdict="blocked",
                        harness="claude")
        self.dated_mark("verdict", "r1", stamp=self.now + 120, verdict="ok",
                        harness="claude")
        result, _ = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["result"], "shadow-plan", result)
        self.assertEqual(result["plan"]["members"], [])
        reasons = {row["work_item_id"]: row["reason"]
                   for row in result["plan"]["deferred"]}
        self.assertIn("late admission", reasons[self.item["id"]])

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


class ShadowMainSyncTest(PgTestCase):
    """#2727 : main en avance est un membre mécanique, pas un lot manquant."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")
        self.now = datetime(2026, 10, 18, 11, 0,
                            tzinfo=ZoneInfo("Europe/Paris")).timestamp()
        self.project_repo = ProjectRepo(os.path.join(self.tmp, "project"),
                                        base_at=self.now - 86400)
        self.project_repo.git("checkout", "-qb", "develop", "main")
        self.project_repo.git("push", "-q", "origin", "develop")
        self.project_repo.git("checkout", "-q", "main")
        self.project_repo.write("prod.txt", "prod\n")
        self.main_tip = self.project_repo.commit("prod")
        self.project_repo.git("push", "-q", "origin", "main")
        self.project_repo.git("checkout", "-q", "develop")
        self.base = self.project_repo.git("rev-parse", "develop")
        self.head = self.project_repo.branch()
        self.canon = _Repo(os.path.join(self.tmp, "canon"), federation_text=FEDERATION)
        self.commit = self.canon.git("rev-parse", "HEAD")
        self.item = work.add(self.db, title="shadow sync fixture", app="ameesh",
                             source="fixture", actor="fixture",
                             branch="lot/member", branch_target="develop")
        self.ops = storage.of(self.db).work
        self.mark = self.ops.add_milestone(self.item["id"], "frozen", sha=self.head,
                                           actor="fixture", verdict=None, note="")
        # #2965 : même horloge que ShadowPassageTest — le gel précède le
        # créneau pour que l'ombre lise une porte déjà ouverte.
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE id = %s", (self.now - 30, self.mark["id"]))
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (self.item["id"],))
        self.notices = []
        self.receipts = {sha: dataclasses.asdict(author(sha)) for sha in (self.base, self.head)}
        provider = types.SimpleNamespace(
            ProvenanceError=RuntimeError,
            receipt_for_commit=lambda db, sha: self.receipts.get(sha),
            receipt_for_milestone=lambda db, mid: None)
        candidate = partial(plan_git.freeze_candidate,
                            repo=self.project_repo.root, target="develop")
        self.project = radar.Project(name="ameesh", repo=self.project_repo.root,
                                     target="develop", github="o/r")
        self.shadow = train_shadow.Passage(
            types.SimpleNamespace(state_dir=os.path.join(self.tmp, "state"),
                                  host="h", runner="r"),
            self.project, policy_path=self.canon.root, policy_commit=self.commit,
            workflows=(), provider=provider, mechanics=mechanics,
            repository=self.project_repo.root, candidate=candidate,
            initial=train_initial.Boundary(self.now - 60),
            notify=self.notices.append)

    def manual_packet(self, lots, stamp="20261018-1100", *, branch_at=None,
                      merge_at=None):
        branch = "integration/paquet-h-" + stamp
        self.project_repo.git("checkout", "-qb", branch, "develop")
        self.project_repo.write("reviews/integration-paquet-h-%s.md" % stamp,
                                "# Paquet\n\n## Admissions\n\n%s\n"
                                % "\n".join("- #%d" % lot for lot in lots))
        self.project_repo.commit("composition %s" % stamp, at=branch_at)
        self.project_repo.git("checkout", "-q", "develop")
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge pull request #1 from manaty/" + branch, branch,
                              env=self.project_repo.date_env(merge_at))
        self.project_repo.git("push", "-q", "origin", "develop", branch)
        return branch

    def test_plan_carries_main_and_the_comparison_matches_the_manual_sync(self):
        result, _ = self.shadow.scheduled_step(self.db, self.now)
        self.assertEqual(result["result"], "shadow-plan", result)
        self.assertEqual(result["plan"]["sync"],
                         dict(ref="main", sha=self.main_tip))
        self.assertEqual([row["work_item_id"] for row in result["plan"]["members"]],
                         [self.item["id"]])
        # The composer syncs main with the build lot #2695, then composes the
        # packet. The train's mechanical main sha is the same one.
        self.project_repo.git("checkout", "-q", "develop")
        self.project_repo.git("merge", "--no-ff", "-m",
                              "Merge main %s dans develop — synchro #2695\n\n"
                              "ameesh-work: 2695" % self.main_tip, "main")
        self.project_repo.git("push", "-q", "origin", "develop")
        self.manual_packet([2695, self.item["id"]])
        self.shadow.scheduled_step(self.db, self.now + 3600)
        comparison = self.shadow.store.load()["comparisons"][0]
        self.assertEqual(comparison["missing"], [])
        self.assertEqual(comparison["sync"]["plan"], self.main_tip)
        self.assertEqual(comparison["sync"]["manual"], self.main_tip)
        self.assertEqual(comparison["sync"]["lots"], [2695])
        self.assertIn("synchro main", comparison["explanations"]["2695"])

    def manual_sync(self, message, *, extra=None):
        self.project_repo.git("checkout", "-q", "develop")
        self.project_repo.git("merge", "--no-ff", "--no-commit", "main")
        if extra is not None:
            self.project_repo.write(extra, "not in main\n")
        self.project_repo.commit(message % self.main_tip)
        self.project_repo.git("push", "-q", "origin", "develop")

    def test_impure_manual_sync_keeps_its_lot_missing(self):
        # cx2: merging main then adding an unreviewed file is not a sync. The
        # exact main parent and the trailer prove nothing without the tree.
        self.shadow.scheduled_step(self.db, self.now)
        self.manual_sync("Merge main %s dans develop — synchro #9999\n\n"
                         "ameesh-work: 9999", extra="unreviewed-feature")
        self.manual_packet([9999, self.item["id"]])
        self.shadow.scheduled_step(self.db, self.now + 3600)
        comparison = self.shadow.store.load()["comparisons"][0]
        self.assertIn(9999, comparison["missing"])
        self.assertEqual(comparison["sync"]["lots"], [])
        self.assertFalse(comparison["sync"]["refused"]["pure"])
        self.assertIn("tree", comparison["sync"]["refused"]["reason"])

    def test_pure_sync_with_several_trailers_exempts_nothing(self):
        # dev1: a packet merge with a main parent must not exempt every trailer.
        self.shadow.scheduled_step(self.db, self.now)
        self.manual_sync("Merge main %s dans develop\n\n"
                         "ameesh-work: 9999\nameesh-work: 9998")
        self.manual_packet([9999, 9998, self.item["id"]])
        self.shadow.scheduled_step(self.db, self.now + 3600)
        comparison = self.shadow.store.load()["comparisons"][0]
        self.assertEqual(comparison["missing"], [9998, 9999])
        self.assertEqual(comparison["sync"]["lots"], [])
        self.assertIn("trailer", comparison["sync"]["refused"]["reason"])
