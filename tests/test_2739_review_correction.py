# SPDX-License-Identifier: AGPL-3.0-only
"""#2739 — the corrector of a small reviewer fix does not review its own fix.

Canon §2 lets a designated reviewer fix a blocking defect in the first round.
#2638 keeps that small correction out of the frozen authors, so the reviewer's
**family** stays eligible for the next review. The agent itself did author the
correction: the automatic designation must not pick it again (#2739), and the
merge gate must not count its verdict on that head.

The witnesses below also fix the observation of 19/10: the provenance is read
on the **final** SHA of the correction (after a refused push rewrote it), not
on the first pushed SHA; and the family stays eligible through another agent.
"""
from __future__ import annotations

import os
from unittest import TestCase, mock

from ameesh import merge_queue, registry, work
from ameesh import review_designation as rd

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")


class SelectFixerTest(TestCase):
    """The selection: the corrector's name is excluded, its family is not."""

    def test_a_fixer_name_is_excluded_but_its_family_stays_eligible(self):
        candidates = [
            rd.Candidate("cx2", "codex", True, 0),
            rd.Candidate("cx3", "codex", True, 0),
            rd.Candidate("dev1", "claude", True, 5),
        ]
        out = rd.select(required=1,
                        authors=[{"name": "author", "family": "deepseek"}],
                        candidates=candidates, families=FAMILIES,
                        fixers=[{"name": "cx2", "family": "codex"}])
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        # cx3 is less loaded, but the rule is first the exact agent: cx2 can
        # never be designated, whatever its family or its load.
        self.assertEqual([row["name"] for row in out["reviewers"]], ["cx3"])
        self.assertEqual(out["reviewers"][0]["family"], "codex")
        self.assertEqual(out["fixers"], ["cx2"])

    def test_a_fixer_never_covers_a_manual_seat(self):
        out = rd.select(required=1,
                        authors=[{"name": "author", "family": "deepseek"}],
                        candidates=[rd.Candidate("cx2", "codex", True, 0)],
                        families=FAMILIES,
                        already=[{"name": "cx2", "family": "codex"}],
                        fixers=["cx2"])
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["manual"], [])

    def test_a_fixer_does_not_exclude_another_agent_of_its_family(self):
        out = rd.select(required=2,
                        authors=[{"name": "author", "family": "deepseek"}],
                        candidates=[rd.Candidate("cx2", "codex", True, 0),
                                    rd.Candidate("cx3", "codex", True, 0),
                                    rd.Candidate("dev1", "claude", True, 0)],
                        families=FAMILIES,
                        fixers=["cx2"])
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([row["family"] for row in out["reviewers"]],
                         ["codex", "claude"])


class GateCorrectorTest(PgTestCase):
    """The merge gate refuses the corrector's verdict on the frozen head."""

    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/work.py", "lot\n")
        self.author_commit = self.repo.commit("author: lot")
        for name, harness in (("author", "deepseek"), ("cx2", "codex"),
                              ("cx3", "codex"), ("dev1", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                            (name,))
        self.item = work.add(self.db, title="Désignation au gel", app="ameesh",
                             assignee="author")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        # the previous freeze designated cx2; the column is the trace of it
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['cx2'] WHERE id = %s",
                        (self.lot,))
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('author', 'deepseek', 's-deepseek', now() - interval '1 minute')")
        self.receipt("author", "deepseek", [self.author_commit])
        # The reviewer's small correction is rewritten by a refused push: only
        # the FINAL object has a receipt, exactly as in the 19/10 observation.
        self.repo.write("src/ameesh/work.py", "lot\ncx2 small fix\n")
        self.repo.commit("cx2: fix")
        self.first_fix = self.repo.git("rev-parse", "HEAD")
        self.repo.git("commit", "--amend", "-qm", "cx2: fix (reintegrated)")
        self.frozen = self.repo.git("rev-parse", "HEAD")
        self.receipt("cx2", "codex", [self.frozen])
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="author")
        for actor in ("cx2", "cx3", "dev1"):
            work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                           actor=actor, verdict="ok")

    def gate(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/merge_queue.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot))

    def vehicle_gate(self):
        """#2766 B cx3: the proper-delta class comes from `class_of_paths` and
        carries no provenance base; the gate must keep the lot's, or #2739's
        corrector exclusion is skipped on the vehicle's own delta."""
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/merge_queue.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        delta = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/merge_queue.py"]}]}
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            vehicle={"ok": None, "required": 1, "klass": delta})

    def test_2766_a_vehicle_delta_keeps_the_provenance_base(self):
        out = self.vehicle_gate()
        self.assertFalse(out.get("error"), out)
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertTrue(out["ok"], out["why"])
        # cx2 (the corrector) is out; cx3, same codex family, stays eligible.
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["cx3", "dev1"])
        self.assertNotIn("cx2", [entry["actor"] for entry in out["accepted"]])

    def test_the_corrector_verdict_does_not_count_and_its_family_does(self):
        out = self.gate()
        self.assertFalse(out.get("error"), out)
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertTrue(out["ok"], out["why"])
        # cx3 (codex) proves the family stays eligible; cx2 is not accepted.
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["cx3", "dev1"])
        self.assertNotIn("cx2", [entry["actor"] for entry in out["accepted"]])

    def test_the_correction_is_read_on_the_final_sha(self):
        # The first object of the correction has no receipt; the frozen head
        # does. A reader keyed to the first pushed SHA would find no corrector.
        commits = self.repo.git("rev-list", "--no-merges",
                                "%s..%s" % (self.base, self.frozen)).split()
        self.assertEqual(set(commits), {self.author_commit, self.frozen})
        self.assertNotEqual(self.first_fix, self.frozen)
        first = self.db.query(
            "SELECT actor FROM provenance_receipts WHERE subject = %s",
            ("commit:" + self.first_fix,))
        self.assertEqual(first, [])
        receipt = self.db.query(
            "SELECT actor FROM provenance_receipts WHERE subject = %s",
            ("commit:" + self.frozen,))[0]
        self.assertEqual(receipt["actor"], "cx2")
        out = self.gate()
        self.assertEqual(out["corrections"], ["cx2"])

    def test_unreadable_correction_provenance_refuses_the_gate(self):
        self.db.execute("DELETE FROM provenance_receipts WHERE subject = %s",
                        ("commit:" + self.frozen,))
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn(self.frozen[:12], out["error"])

    def test_a_gate_without_a_storage_fails_closed(self):
        # #2739 / h-2000: a db without `query` (a test fixture, here) cannot
        # read the frozen range; the absence of a corrector is not proven, so
        # the gate refuses instead of admitting every verdict.
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/merge_queue.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        families = {"author": "deepseek", "cx2": "codex", "cx3": "codex",
                    "dev1": "claude"}
        out = merge_queue.review_gate(
            object(), work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            family_of=lambda name: families.get(str(name or "").strip(), ""))
        self.assertFalse(out["ok"], out)
        self.assertIn("cannot be excluded", out["why"])

    def _designation_note(self, actor):
        # The pilot retired the manual seat after the correction (#2076); the
        # journal line is the only trace left.
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY[]::text[], reviewer = ''"
            " WHERE id = %s", (self.lot,))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor,"
            " created_at) VALUES (%s, 'qa', 'reviewer cx2 designated',"
            " %s, now() - interval '10 minutes')", (self.lot, actor))

    def test_a_manual_corrector_removed_from_the_column_still_does_not_count(self):
        # #2739: the pilot retired the manual seat after the correction
        # (#2076). The journal keeps the designation, so the corrector stays
        # recognizable and the gate still refuses its verdict.
        self._designation_note("mesh-design")
        with mock.patch("ameesh.assignments.is_orchestrator",
                        side_effect=lambda cfg, db, name, **kw: name == "mesh-design"):
            out = self.gate()
        self.assertFalse(out.get("error"), out)
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["cx3", "dev1"])

    def test_a_free_note_is_not_a_designation(self):
        # #2739: the line is only a text. A free note by an ordinary agent is
        # identical to a designation; only the system, a human or a declared
        # orchestrator/design agent can make a reviewer.
        from ameesh.review_designation_activation import _journal_reviewers

        self._designation_note("cx3")
        with mock.patch("ameesh.assignments.is_orchestrator", return_value=False):
            self.assertEqual(_journal_reviewers(self.db, self.lot), [])
        self.db.execute("DELETE FROM work_item_events WHERE note = %s",
                        ("reviewer cx2 designated",))
        self._designation_note("mesh-design")
        with mock.patch("ameesh.assignments.is_orchestrator",
                        side_effect=lambda cfg, db, name, **kw: name == "mesh-design"):
            self.assertEqual(_journal_reviewers(self.db, self.lot), ["cx2"])
