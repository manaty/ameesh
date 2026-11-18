# SPDX-License-Identifier: AGPL-3.0-only
"""#4172 (manaty/ameesh#3185, volet 2): backlog coherence pass.

A fixture backlog plants one duplicate, one already-done lot, one obsolete
path and one contradiction. Only the already-done reference is closed; the
probably-done and probably-obsolete lots, the duplicate and the contradiction
go into one digest for the PO. A second pass sends no second digest.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import backlog_coherence, config, db as db_mod, registry, storage, work

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
        self.assertIn(("probably_obsolete", self.obsolete["id"], 0), findings)
        self.assertIn(("probably_obsolete", self.obsolete_basename["id"], 0),
                      findings)
        self.assertIn(("duplicate", self.dup_b["id"], self.dup_a["id"]), findings)
        contradiction = {row for row in findings if row[0] == "contradiction"}
        self.assertEqual(len(contradiction), 1)
        # not a closure: the fixture's duplicate and contradiction stay open
        applied = backlog_coherence.apply_report(self.db, report, cfg=self.test_cfg)
        closed = {(row["kind"], row["lot"]) for row in applied["closures"]
                  if row["result"] == "closed"}
        self.assertEqual(closed, {("already_done", self.already_ref["id"])})
        for row in applied["closures"]:
            item = work.get(self.db, row["lot"])
            self.assertEqual(item["state"], "closed")
        self.assertEqual(work.get(self.db, self.already_ref["id"])["close_reason"], "done")
        for lot_id in (self.obsolete["id"], self.obsolete_basename["id"],
                       self.dup_a["id"], self.dup_b["id"], self.add["id"],
                       self.remove["id"], self.already_code["id"]):
            self.assertNotEqual(work.get(self.db, lot_id)["state"], "closed")
        notes = [event["note"] for event in
                 work.events(self.db, self.already_ref["id"])]
        self.assertTrue(any("coherence: already done" in note
                            for note in notes), notes)
        sent = self.db.query(
            "SELECT id, body FROM agent_mailbox WHERE recipient = 'human:po'")
        self.assertEqual(len(sent), 1, sent)
        body = sent[0]["body"]
        self.assertIn("Corriger le mode X", body)
        self.assertIn("Ajouter le mode Y", body)
        self.assertIn("Supprimer le mode Y", body)
        self.assertIn("Corriger la ligne deux (code)", body)
        self.assertIn("Obsolètes probables", body)
        self.assertIn("Nettoyer src/gone.py", body)
        # #4262: no deterministic closure is claimed for an obsolescence.
        self.assertIn("Les fermetures déterministes éventuelles (déjà fait) "
                      "sont justifiées", body)
        self.assertNotIn("déjà fait, obsolète", body)

    def test_restoration_after_deletion_is_a_digest_not_a_closure(self):
        # B1 cx2 (#4172): a path already gone when the lot is filed may be a
        # restoration request; its absence is a probable obsolescence, never a
        # deterministic closure.
        self.db.execute("TRUNCATE work_items CASCADE")
        self.repo = tempfile.mkdtemp(prefix="coh-4172-restore-", dir=self.tmp)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.invalid")
        _git(self.repo, "config", "user.name", "test")
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        self._write("src/gone.py", "deleted before the lot\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        os.unlink(os.path.join(self.repo, "src/gone.py"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "remove before the lot")
        lot = self.lot("Restaurer src/gone.py supprimé par erreur",
                       body="src/gone.py:1")
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg,
                                        repo=self.repo)
        finding = next(row for row in report["findings"]
                       if row["lot"] == lot["id"])
        self.assertEqual(finding["kind"], "probably_obsolete")
        self.assertFalse(finding["deterministic"])
        applied = backlog_coherence.apply_report(self.db, report,
                                                 cfg=self.test_cfg)
        self.assertFalse(any(row["lot"] == lot["id"]
                             for row in applied["closures"]))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        sent = self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'human:po'")
        self.assertEqual(len(sent), 1)
        self.assertIn("Obsolètes probables", sent[0]["body"])
        self.assertIn("Restaurer src/gone.py", sent[0]["body"])

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

    def _cite(self, *lot_ids, subject="partial step"):
        """A merged change that only references the lots: a sub-step, not proof."""
        self._write("src/x.py", "alpha\nbug fixed\ngamma\n# %s\n"
                    % ",".join(str(int(lot)) for lot in lot_ids))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qam",
             "%s\n\n%s" % (subject, "\n".join(
                 "ameesh-work: %d" % int(lot) for lot in lot_ids)))

    def _started_and_back_in_intake(self):
        """A lot whose work has started (build) and which returned to intake.

        The measured start (L157) stays on the row, and the lot carries no
        branch: exactly the operator lot a git citation must not close (#7782).
        """
        lot = self.lot("Corriger la ligne deux (démarré)")
        work.move(self.db, lot["id"], "build", actor="test")
        work.move(self.db, lot["id"], "blocked", actor="test")
        work.move(self.db, lot["id"], "intake", actor="test")
        item = work.get(self.db, lot["id"])
        self.assertIsNotNone(item["started_ts"])
        self.assertEqual(item["state"], "intake")
        self.assertFalse(item["branch"] or item["pr_ref"])
        return lot

    def test_a_started_lot_is_not_closed_by_a_git_citation(self):
        started = self._started_and_back_in_intake()
        held = self.lot("Corriger la ligne deux (retenu)")
        work.note(self.db, held["id"], "hold: smoke P6 du déplacement à livrer",
                  actor="test")
        self._cite(started["id"], held["id"])
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg,
                                        repo=self.repo)
        found = {row["lot"] for row in report["findings"]
                 if row["kind"] == "already_done"}
        self.assertIn(started["id"], found)
        self.assertIn(held["id"], found)
        applied = backlog_coherence.apply_report(self.db, report,
                                                 cfg=self.test_cfg, digest=False)
        refused = {row["lot"]: row for row in applied["closures"]
                   if row["result"] == "refused"}
        self.assertIn(started["id"], refused)
        self.assertIn("started", refused[started["id"]]["reason"])
        self.assertIn("hold", refused[held["id"]]["reason"])
        self.assertIn("test", refused[held["id"]]["reason"])
        self.assertEqual(work.get(self.db, started["id"])["state"], "intake")
        self.assertEqual(work.get(self.db, held["id"])["state"], "intake")

    def test_manual_close_of_a_started_lot_stays_allowed(self):
        """#7782 only guards the automatic pass: an operator keeps `work close`."""
        started = self._started_and_back_in_intake()
        work.close(self.db, started["id"], done=True, actor="test",
                   note="read and closed by hand")
        self.assertEqual(work.get(self.db, started["id"])["state"], "closed")

    def _apply_while_the_lot_is_locked(self, lot_id: int, report: dict, action):
        """Run `apply_report` while another connection holds the lot's row lock.

        `action(tx)` writes the concurrent start/hold inside that transaction,
        after the pass has read the lot and is waiting on the lock. The write
        is committed before the pass can proceed (#7782).
        """
        writer = db_mod.connect(self.cfg)
        result: dict = {}
        errors: list = []

        def run():
            try:
                result.update(backlog_coherence.apply_report(
                    self.db, report, cfg=self.test_cfg, digest=False))
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)

        thread = None
        try:
            with writer.transaction() as tx:
                tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE",
                         (int(lot_id),))
                thread = threading.Thread(target=run)
                thread.start()
                time.sleep(0.5)
                self.assertTrue(
                    thread.is_alive(),
                    "apply_report did not wait for the lot's row lock")
                action(tx)
            thread.join(20)
            self.assertFalse(thread.is_alive(), "apply_report did not return")
            self.assertEqual(errors, [])
            return result
        finally:
            if thread is not None:
                thread.join(20)
            writer.close()

    def test_a_start_winning_the_race_refuses_the_closure(self):
        """#7782: `started_ts` read before the lock must not close the lot."""
        lot = self.lot("Corriger la ligne deux (course au démarrage)")
        self._cite(lot["id"])
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg,
                                        repo=self.repo)
        self.assertTrue(any(row["kind"] == "already_done"
                            and row["lot"] == lot["id"]
                            for row in report["findings"]))
        applied = self._apply_while_the_lot_is_locked(
            lot["id"], report,
            lambda tx: tx.execute(
                "UPDATE work_items SET started_at = now() WHERE id = %s",
                (lot["id"],)))
        refused = [row for row in applied["closures"]
                   if row["lot"] == lot["id"] and row["result"] == "refused"]
        self.assertEqual(len(refused), 1, applied["closures"])
        self.assertIn("started", refused[0]["reason"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_a_hold_winning_the_race_refuses_the_closure(self):
        """#7782: a `hold:` committed while the pass waits on the lock holds."""
        lot = self.lot("Corriger la ligne deux (course au hold)")
        self._cite(lot["id"])
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg,
                                        repo=self.repo)
        applied = self._apply_while_the_lot_is_locked(
            lot["id"], report,
            lambda tx: work.note(tx, lot["id"],
                                 "hold: smoke P6 du déplacement à livrer",
                                 actor="test"))
        refused = [row for row in applied["closures"]
                   if row["lot"] == lot["id"] and row["result"] == "refused"]
        self.assertEqual(len(refused), 1, applied["closures"])
        self.assertIn("hold", refused[0]["reason"])
        self.assertIn("test", refused[0]["reason"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_a_concurrent_manual_closure_is_not_rectified(self):
        """#7782: a manual `superseded` closure of a review mission stays so.

        The pass may read the mission while it is still in intake; a manual
        closure then commits before `close_fresh` reloads it. `done` used to
        rewrite the review's `superseded by` its parent into `done` (the
        `_rectify_done` path) before the fresh guard ran. The automatic pass
        must refuse, not regularise a manual gesture.
        """
        parent = self.lot("Lot relu par la passe")
        registry.upsert(self.db, "relecteur", harness="claude",
                        host=self.cfg.host, cwd=self.tmp, mode="execute")
        review = work.add(
            self.db,
            title="R1 Codex #%d : reprise du suivi CI" % int(parent["id"]),
            source="mail", issue_ref="manaty/ameesh#941", app="p",
            assignee="relecteur")
        storage.of(self.db).work.add_milestone(
            parent["id"], "verdict", sha="e" * 40, actor="relecteur",
            verdict="blocked", note="")
        self._cite(review["id"])
        report = backlog_coherence.scan(self.db, "p", cfg=self.test_cfg,
                                        repo=self.repo)
        real_close_fresh = work.close_fresh

        def close_manually_then_apply(db, item_id, **kwargs):
            work.close(db, item_id, superseded_by=parent["id"], actor="qa",
                       note="clôture administrative du suivi de revue")
            return real_close_fresh(db, item_id, **kwargs)

        with mock.patch.object(work, "close_fresh",
                               side_effect=close_manually_then_apply):
            applied = backlog_coherence.apply_report(
                self.db, report, cfg=self.test_cfg, digest=False)
        refused = [row for row in applied["closures"]
                   if row["lot"] == review["id"]]
        self.assertEqual(len(refused), 1, applied["closures"])
        self.assertEqual(refused[0]["result"], "refused")
        self.assertIn("not closable", refused[0]["reason"])
        after = work.get(self.db, review["id"])
        self.assertEqual((after["state"], after["close_reason"]),
                         ("closed", "superseded"))
        self.assertEqual(int(after["superseded_by"]), int(parent["id"]))

    def _cfg_with_recipients(self, recipients):
        return dataclasses.replace(
            self.test_cfg, coherence={"recipients": recipients})

    def _sent(self):
        return self.db.query("SELECT recipient, body FROM agent_mailbox ORDER BY id")

    def test_configured_recipients_win_over_the_default_human(self):
        cfg = self._cfg_with_recipients({"p": "human:owner"})
        report = backlog_coherence.scan(self.db, "p", cfg=cfg, repo=self.repo)
        applied = backlog_coherence.apply_report(self.db, report, cfg=cfg)
        self.assertTrue(applied["digest"]["sent"], applied["digest"])
        self.assertEqual(applied["digest"]["recipient"], "human:owner")
        self.assertEqual([row["recipient"] for row in self._sent()],
                         ["human:owner"])

    def test_a_project_not_declared_keeps_the_default_human(self):
        cfg = self._cfg_with_recipients({"other": "human:other"})
        report = backlog_coherence.scan(self.db, "p", cfg=cfg, repo=self.repo)
        applied = backlog_coherence.apply_report(self.db, report, cfg=cfg)
        self.assertTrue(applied["digest"]["sent"], applied["digest"])
        self.assertEqual([row["recipient"] for row in self._sent()],
                         ["human:po"])

    def test_several_recipients_each_get_the_digest(self):
        cfg = self._cfg_with_recipients({"p": ["po", "human:po2"]})
        report = backlog_coherence.scan(self.db, "p", cfg=cfg, repo=self.repo)
        applied = backlog_coherence.apply_report(self.db, report, cfg=cfg)
        self.assertTrue(applied["digest"]["sent"], applied["digest"])
        self.assertEqual(applied["digest"]["recipients"],
                         ["human:po", "human:po2"])
        sent = self._sent()
        self.assertEqual([row["recipient"] for row in sent],
                         ["human:po", "human:po2"])
        self.assertEqual(sent[0]["body"], sent[1]["body"])
        # the idempotence key is per recipient: a second pass re-sends nothing
        again = backlog_coherence.scan(self.db, "p", cfg=cfg, repo=self.repo)
        backlog_coherence.apply_report(self.db, again, cfg=cfg)
        self.assertEqual(len(self._sent()), 2)

    def test_configured_recipients_win_over_the_responsible_package(self):
        self.db.execute(
            "INSERT INTO work_packages (id, kind, title, responsible, team,"
            " canon_ref, present) VALUES ('p-package', 'epic', 'p package',"
            " 'human:pkg', 'p', 'test', true)")
        cfg = self._cfg_with_recipients({"p": "human:owner"})
        report = backlog_coherence.scan(self.db, "p", cfg=cfg, repo=self.repo)
        applied = backlog_coherence.apply_report(self.db, report, cfg=cfg)
        self.assertEqual(applied["digest"]["recipient"], "human:owner")
        self.assertEqual([row["recipient"] for row in self._sent()],
                         ["human:owner"])

    def test_invalid_recipient_config_is_reported_never_sent(self):
        cfg = self._cfg_with_recipients({"p": "agent:robot"})
        report = backlog_coherence.scan(self.db, "p", cfg=cfg, repo=self.repo)
        applied = backlog_coherence.apply_report(self.db, report, cfg=cfg)
        self.assertFalse(applied["digest"]["sent"])
        self.assertIn("coherence.recipients.p", applied["digest"]["error"])
        self.assertIn("human:<id>", applied["digest"]["error"])
        self.assertEqual(self._sent(), [])
        # the deterministic closures do not depend on the digest's recipient
        self.assertEqual(
            work.get(self.db, self.already_ref["id"])["state"], "closed")

    def test_unknown_coherence_config_key_is_reported(self):
        cfg = dataclasses.replace(
            self.test_cfg, coherence={"recipient": {"p": "human:po"}})
        report = backlog_coherence.scan(self.db, "p", cfg=cfg, repo=self.repo)
        applied = backlog_coherence.apply_report(self.db, report, cfg=cfg)
        self.assertFalse(applied["digest"]["sent"])
        self.assertIn("coherence: unknown key", applied["digest"]["error"])
        self.assertEqual(self._sent(), [])

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
        self.assertIn("probably obsolete", result.stdout)
        self.assertEqual(work.get(self.db, self.already_ref["id"])["state"], "intake")


class CoherenceConfigTest(unittest.TestCase):
    """The recipients travel from the host configuration file to the pass."""

    def test_host_configuration_carries_the_project_recipients(self):
        with tempfile.TemporaryDirectory(prefix="coh-config-") as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"coherence": {"recipients": {
                    "p": ["human:po", "human:po2"]}}}, fh)
            cfg = config.load(env={"AMEESH_CONFIG": path},
                              register_credentials=False)
        self.assertEqual(backlog_coherence.parse_config(cfg),
                         {"p": ("human:po", "human:po2")})


if __name__ == "__main__":
    unittest.main()
