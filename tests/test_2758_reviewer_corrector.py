# SPDX-License-Identifier: AGPL-3.0-only
"""#2758 — the corrector of a small reviewer fix never reviews its own fix.

Canon §2 lets a designated reviewer fix a blocking defect in the first round.
#2638 keeps that small correction out of the frozen authors, so the reviewer's
**family** stays eligible for the next review. The agent itself did author the
correction: the automatic designation must not pick it again, and the merge
gate must not count its verdict on that head — another agent of the same family
takes the seat (#2680/#2692).

The gate witness also reads the provenance on the **final** SHA of the
correction (after a refused push rewrote it), and a manual corrector retired
from the column stays recognizable through the lot journal.
"""
from __future__ import annotations

import os
from unittest import TestCase

from ameesh import merge_queue, registry, review_designation as rd, storage, work

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
        # the exact agent is excluded whatever its family or its load
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

    def test_no_other_agent_of_the_family_waits_with_the_corrector_named(self):
        out = rd.select(required=1,
                        authors=[{"name": "author", "family": "deepseek"}],
                        candidates=[rd.Candidate("cx2", "codex", True, 0)],
                        families=FAMILIES, fixers=["cx2"])
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["reviewers"], [])
        self.assertIn("corrector(s) excluded: cx2", out["why"])


class MailWordingTest(TestCase):
    def test_the_mail_says_the_corrector_is_not_designated_again(self):
        body = rd.reviewer_message(
            {"id": 1, "title": "test"}, freeze={"id": 2, "sha": "a" * 40},
            required=1, rank=1, reviewer={"family": "codex"})
        self.assertIn("not designated again on a head carrying your correction", body)
        self.assertIn("another agent of your family takes that seat", body)


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
        self.ops = storage.of(self.db).work
        self.ops.add_milestone(self.lot, "frozen", sha=self.frozen, actor="author",
                               verdict=None, note="")
        for actor in ("cx2", "cx3", "dev1"):
            self.ops.add_milestone(self.lot, "verdict", sha=self.frozen,
                                   actor=actor, verdict="ok", note="")

    def gate(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/merge_queue.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            families=FAMILIES)

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

    def test_a_manual_corrector_removed_from_the_column_still_does_not_count(self):
        # #2758: the pilot retired the manual seat after the correction
        # (#2076). The journal keeps the designation, so the corrector stays
        # recognizable and the gate still refuses its verdict.
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY[]::text[], reviewer = ''"
            " WHERE id = %s", (self.lot,))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor,"
            " created_at) VALUES (%s, 'qa', 'reviewer cx2 designated',"
            " 'mesh-design', now() - interval '10 minutes')", (self.lot,))
        out = self.gate()
        self.assertFalse(out.get("error"), out)
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["cx3", "dev1"])
