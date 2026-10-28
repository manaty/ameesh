# SPDX-License-Identifier: AGPL-3.0-only
"""#4172 (manaty/ameesh#3185, volet 2): backlog coherence pass.

A fixture backlog plants one duplicate, one already-done lot, one obsolete
path and one contradiction. Only the deterministic findings are closed; the
duplicate and the contradiction go into one digest for the PO. A second pass
sends no second digest.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import time
import unittest

from ameesh import backlog_coherence, work

from .support import PgTestCase


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True,
        timeout=30).stdout


class CoherenceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_items CASCADE")
        self.test_cfg = dataclasses.replace(
            self.cfg, notify={"default_human": "human:po"})
        self.repo = tempfile.mkdtemp(prefix="coh-4172-", dir=self.tmp)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.invalid")
        _git(self.repo, "config", "user.name", "test")
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        self._write("src/x.py", "alpha\nbug line\ngamma\n")
        self._write("src/gone.py", "to be removed\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.already_ref = self.lot("Corriger la ligne deux (référence)",
                                    body="src/x.py:2")
        self.already_code = self.lot("Corriger la ligne deux (code)",
                                     body="src/x.py:2")
        self.obsolete = self.lot("Nettoyer src/gone.py", body="src/gone.py:1")
        self.obsolete_basename = self.lot("Nettoyer gone.py", body="gone.py:1")
        self.dup_a = self.lot("Corriger le mode X")
        self.dup_b = self.lot("Corriger le mode X")
        self.add = self.lot("Ajouter le mode Y dans src/x.py", body="src/x.py:3")
        self.remove = self.lot("Supprimer le mode Y dans src/x.py",
                               body="src/x.py:3")
        # the lots are one second older than the merged changes below: the
        # code-touch proof is strictly posterior to the lot (a same-second
        # base commit is not a fix)
        time.sleep(1.1)
        # a merged change that references one lot and touches the cited line
        # (the other lot sees the same merged change through the code itself)
        self._write("src/x.py", "alpha\nbug fixed\ngamma\n")
        _git(self.repo, "commit", "-qam",
             "fix line two\n\nameesh-work: %d" % self.already_ref["id"])
        # a merged change that removes the cited path
        os.unlink(os.path.join(self.repo, "src/gone.py"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "remove src/gone.py")

    def _write(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def lot(self, title, *, body=""):
        return work.add(self.db, title=title, body=body, app="p")

    def _findings(self, report):
        return {(row["kind"], int(row["lot"]), int(row.get("other") or 0))
                for row in report["findings"]}

    def test_fixture_finds_the_four_families_and_closes_only_deterministic(self):
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg, repo=self.repo)
        self.assertEqual(report["git"], "vérifié")
        self.assertEqual(report["base"], "main")
        self.assertEqual(report["open_lots"], 8)
        findings = self._findings(report)
        self.assertIn(("already_done", self.already_ref["id"], 0), findings)
        # a touch of the cited line is a hint for the PO, never a closure
        self.assertIn(("probably_done", self.already_code["id"], 0), findings)
        self.assertIn(("obsolete", self.obsolete["id"], 0), findings)
        self.assertIn(("obsolete", self.obsolete_basename["id"], 0), findings)
        self.assertIn(("duplicate", self.dup_b["id"], self.dup_a["id"]), findings)
        contradiction = {row for row in findings if row[0] == "contradiction"}
        self.assertEqual(len(contradiction), 1)
        # not a closure: the fixture's duplicate and contradiction stay open
        applied = backlog_coherence.apply_report(self.db, report, cfg=self.test_cfg)
        closed = {(row["kind"], row["lot"]) for row in applied["closures"]
                  if row["result"] == "closed"}
        self.assertEqual(closed, {("already_done", self.already_ref["id"]),
                                  ("obsolete", self.obsolete["id"]),
                                  ("obsolete", self.obsolete_basename["id"])})
        for row in applied["closures"]:
            item = work.get(self.db, row["lot"])
            self.assertEqual(item["state"], "closed")
        self.assertEqual(work.get(self.db, self.already_ref["id"])["close_reason"], "done")
        self.assertEqual(work.get(self.db, self.obsolete["id"])["close_reason"],
                         "abandoned")
        self.assertEqual(work.get(self.db, self.obsolete_basename["id"])["close_reason"],
                         "abandoned")
        for lot_id in (self.dup_a["id"], self.dup_b["id"], self.add["id"],
                       self.remove["id"], self.already_code["id"]):
            self.assertNotEqual(work.get(self.db, lot_id)["state"], "closed")
        notes = [event["note"] for event in
                 work.events(self.db, self.already_ref["id"])]
        self.assertTrue(any("coherence: already done" in note
                            for note in notes), notes)
        notes = [event["note"] for event in work.events(self.db, self.obsolete["id"])]
        self.assertTrue(any("coherence: obsolete" in note
                            for note in notes), notes)
        sent = self.db.query(
            "SELECT id, body FROM agent_mailbox WHERE recipient = 'human:po'")
        self.assertEqual(len(sent), 1, sent)
        body = sent[0]["body"]
        self.assertIn("Corriger le mode X", body)
        self.assertIn("Ajouter le mode Y", body)
        self.assertIn("Supprimer le mode Y", body)
        self.assertIn("Corriger la ligne deux (code)", body)

    def test_second_pass_sends_no_second_digest(self):
        first = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg, repo=self.repo)
        backlog_coherence.apply_report(self.db, first, cfg=self.test_cfg)
        before = self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE recipient = 'human:po'")[0]["n"]
        again = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg, repo=self.repo)
        self.assertFalse(any(row.get("deterministic") for row in again["findings"]))
        backlog_coherence.apply_report(self.db, again, cfg=self.test_cfg)
        after = self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE recipient = 'human:po'")[0]["n"]
        self.assertEqual(int(after), int(before))

    def test_dry_scan_writes_nothing(self):
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg, repo=self.repo)
        self.assertFalse(any(row.get("deterministic")
                             and work.get(self.db, row["lot"])["state"] == "closed"
                             for row in report["findings"]))
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox")[0]["n"], 0)
        self.assertEqual(work.get(self.db, self.already_ref["id"])["state"], "intake")

    def test_a_lot_with_a_branch_is_not_closed(self):
        work.note(self.db, self.already_ref["id"], "branch attached",
                  actor="test")
        self.db.execute("UPDATE work_items SET branch = 'lot/4172-test'"
                        " WHERE id = %s", (self.already_ref["id"],))
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg, repo=self.repo)
        applied = backlog_coherence.apply_report(self.db, report, cfg=self.test_cfg)
        refused = [row for row in applied["closures"]
                   if row["lot"] == self.already_ref["id"]]
        self.assertEqual(refused[0]["result"], "refused")
        self.assertIn("branch", refused[0]["reason"])
        self.assertEqual(work.get(self.db, self.already_ref["id"])["state"], "intake")

    def test_cli_reads(self):
        result = self.mesh("work", "coherence", "--project", "p", "--repo", self.repo,
                          "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"schema": "ameesh-backlog-coherence/1"', result.stdout)
        # the CLI builds its own config: a PO is needed to write the digest, so
        # this invocation stays read-only and proves the parser/dispatch.
        result = self.mesh("work", "coherence", "--project", "p", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already done", result.stdout)
        self.assertIn("probably done", result.stdout)
        self.assertEqual(work.get(self.db, self.already_ref["id"])["state"], "intake")


if __name__ == "__main__":
    unittest.main()
