# SPDX-License-Identifier: AGPL-3.0-only
"""#2739/#2888 — the corrector of a reviewer fix does not review its own fix.

Canon §2 lets a designated reviewer fix a blocking defect in the first round.
That correction is an authored commit of the head whatever its size (#2888):
the reviewer's **family** is no longer eligible for the opinions on that head,
and the exact agent is never designated again (#2739). The witnesses below
also fix the observation of 19/10: the provenance is read on the **final** SHA
of the correction (after a refused push rewrote it), not on the first pushed
SHA; and no other agent of the corrector's family can cover the seat.
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
    """The selection: a corrector's name and family are excluded as authors."""

    def test_a_fixer_name_and_family_are_excluded(self):
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
        # #2888: the corrector is an author of the head; its family is
        # forbidden, so neither cx2 nor cx3 (same codex family) can review it.
        self.assertEqual([row["name"] for row in out["reviewers"]], ["dev1"])
        self.assertEqual(out["reviewers"][0]["family"], "claude")
        self.assertEqual(out["fixers"], ["cx2"])

    def test_a_fixer_never_covers_a_manual_seat(self):
        out = rd.select(required=1,
                        authors=[{"name": "author", "family": "deepseek"},
                                 {"name": "cx2", "family": "codex"}],
                        candidates=[rd.Candidate("cx2", "codex", True, 0)],
                        families=FAMILIES,
                        already=[{"name": "cx2", "family": "codex"}],
                        fixers=["cx2"])
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["manual"], [])

    def test_a_fixer_family_cannot_cover_the_second_opinion(self):
        out = rd.select(required=2,
                        authors=[{"name": "author", "family": "deepseek"}],
                        candidates=[rd.Candidate("cx2", "codex", True, 0),
                                    rd.Candidate("cx3", "codex", True, 0),
                                    rd.Candidate("dev1", "claude", True, 0)],
                        families=FAMILIES,
                        fixers=[{"name": "cx2", "family": "codex"}])
        # #2888: codex is the corrector's family, an author's family; only
        # claude remains and cannot cover two distinct-family opinions. #3596
        # provides the available seat at once and waits for the missing one.
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual([row["name"] for row in out["reviewers"]], ["dev1"])
        self.assertEqual(out["missing"], 1)
        self.assertIn("1 independent review missing", out["why"])
        self.assertIn("forbidden author families: codex, deepseek", out["why"])


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

    def verdict_receipt(self, mark, harness):
        """#2928 : la porte sensible ne compte un verdict que sur son reçu exact."""
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), self.frozen, mark["actor"],
             "s-" + harness, "t", harness, self.lot))

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
        harnesses = {"cx2": "codex", "cx3": "codex", "dev1": "claude"}
        for actor in ("cx2", "cx3", "dev1"):
            mark = work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harnesses[actor])

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
        # #2888: cx2's correction authors the codex family on the delta; cx3
        # is excluded with that family. Only dev1 (claude) remains.
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"])
        self.assertNotIn("cx2", [entry["actor"] for entry in out["accepted"]])

    def test_the_corrector_verdict_does_not_count_and_neither_does_its_family(self):
        out = self.gate()
        self.assertFalse(out.get("error"), out)
        self.assertEqual(out["corrections"], ["cx2"])
        # #2888: cx2/Codex authored the head. cx3 (same codex family) is out
        # and dev1 is the only other family, so one independent opinion remains.
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"])
        self.assertNotIn("cx2", [entry["actor"] for entry in out["accepted"]])
        self.assertNotIn("cx3", [entry["actor"] for entry in out["accepted"]])

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

    def test_the_gate_reads_the_coauthor_it_was_not_given(self):
        # #2726 : la porte lit elle-même les auteurs de la plage gelée quand
        # l'appelant ne les passe pas (le feu, la file, le paquet). Un tiers
        # avec reçu exact est un auteur : ni son avis ni sa famille ne comptent.
        self.repo.write("src/ameesh/work.py", "lot\ncx2 small fix\nthird work\n")
        head = self.repo.commit("third: coauthor")
        self.receipt("third", "claude", [head])
        registry.upsert(self.db, "third", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        work.milestone(self.db, self.lot, "frozen", sha=head, actor="author")
        for actor in ("third", "cx3"):
            work.milestone(self.db, self.lot, "verdict", sha=head,
                           actor=actor, verdict="ok")
        self.frozen = head
        klass = {"class": "normal", "doubt": False,
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            required=1)
        self.assertFalse(out.get("error"), out)
        self.assertIn("third", {entry["name"] for entry in out["authors"]})
        self.assertNotIn("third", [entry["actor"] for entry in out["accepted"]])
        # The gate stays historical for a normal class with one review: the
        # co-author's own verdict is out, an independent verdict still counts.
        self.assertEqual([entry["actor"] for entry in out["accepted"]], ["cx3"])

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
        # #2888: cx2's correction authors its family; only dev1 remains.
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"])

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
