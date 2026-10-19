# SPDX-License-Identifier: AGPL-3.0-only
"""#2447 — activation of the reviewer designation at the `frozen` milestone.

Real database: the freeze hook reads the canon policy at the audited commit,
the frozen diff and the exact provenance of its commits, selects the reviewers
of the real session families and writes the designation in the milestone's
transaction. A missing policy or an unknown author is an explicit wait, never
a guessed reviewer; the recovery path replays the same freeze.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
from unittest import mock

from ameesh import mail, registry, review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg
from ameesh import storage, work
from ameesh.work_reviewers import designated

from .support import PgTestCase

MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""

FEDERATION = """federation: "0.1"
id: manaty-test
root: home
members:
  - id: home
    ref: main
    bundle: .
    entrypoint: index.md
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
        default: light
        rules:
          - paths:
              - "src/ameesh/**"
            class: sensitive
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      ameesh:
        rules:
          - paths:
              - "src/ameesh/**"
            reviews: 2
"""

PILOT = """---
type: Agent
title: pilot
team: ameesh
roles: [orchestrateur]
harness: claude
---
"""


class _Repo:
    """A minimal git repository for the frozen lot or the canon."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.dry = dict(os.environ,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@example.invalid")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True, env=self.dry)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


class _Canon:
    """A canon repository: mechanics + federation + an orchestrator card."""

    def __init__(self, root: str, *, mechanics=MECHANICS, federation=FEDERATION):
        self.root = root
        self.repo = _Repo(root)
        self.repo.write("index.md", "# test canon\n")
        self.repo.write("federation.yaml", federation)
        if mechanics is not None:
            self.repo.write("processus/mecanique.yaml", mechanics)
        self.repo.write("agents/pilot.md", PILOT)
        self.commit = self.repo.commit("canon")

    def update(self, path: str, text: str) -> str:
        self.repo.write(path, text)
        self.commit = self.repo.commit("canon update")
        return self.commit


class _ActivationFixture:
    """A frozen lot, a canon, a registry with two independent families."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM canon_state")
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/work.py", "lot\n")
        self.frozen = self.repo.commit("lot")
        self.canon = _Canon(os.path.join(self.tmp, "canon"))
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root, canon_ref="HEAD")
        for name, harness in (("author", "deepseek"), ("r1", "claude"),
                              ("r2", "codex"), ("pilot", "claude")):
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
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('author', 'deepseek', 's-deepseek', now() - interval '1 minute')")
        self.receipts()

    # -- helpers ----------------------------------------------------------
    def receipts(self, *, actor="author", harness="deepseek", commits=None):
        for sha in (commits if commits is not None
                    else self.repo.git("rev-list", "--no-merges",
                                       "%s..%s" % (self.base, self.frozen)).split()):
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness, self.lot))

    def freeze(self, *, cfg=None, hook=None):
        threads: list = []
        outcome: dict = {}
        hook = hook if hook is not None else act.hook(cfg or self.cfg, outcome)
        row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                             actor="author", on_frozen=hook, after_commit=threads)
        for record in threads:
            record(self.db.cfg, self.db)
        return row, outcome

    def mailbox(self):
        return self.db.query(
            "SELECT recipient, body, payload, kind, work_item_id"
            " FROM agent_mailbox ORDER BY id")

    def marks(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,)) if str(row["note"]).startswith(pg.MARK_PREFIX)]

    def notes(self, prefix=""):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,)) if str(row["note"]).startswith(prefix)]


class ActivationTest(_ActivationFixture, PgTestCase):
    """The freeze hook against a real database and a real frozen repository."""

    def test_work_milestone_installs_the_designation_by_default(self):
        # no explicit hook, no after_commit: `work.milestone` itself activates
        # the designation and drains its readable thread after the freeze commits
        db = self.connect(canon=self.canon.root, canon_ref="HEAD")
        try:
            row = work.milestone(db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        finally:
            db.close()
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        self.assertEqual(len(self.marks()), 1)

    def test_the_freeze_designates_two_real_families_without_an_agent(self):
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 2)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1", "r2"])
        self.assertEqual([r["family"] for r in out["reviewers"]], ["claude", "codex"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        # #2478: the designation is a request linked to the lot, active mail
        # for the reviewer's runner.
        self.assertEqual([m["kind"] for m in self.mailbox()], ["request", "request"])
        self.assertEqual([m["work_item_id"] for m in self.mailbox()],
                         [str(self.lot), str(self.lot)])
        self.assertIn(self.frozen, self.mailbox()[0]["body"])
        self.assertEqual(len(self.marks()), 1)
        self.assertEqual(pg.designation_of(self.marks()[0])["state"], rd.DESIGNATED)
        # B1 of the Codex review cx2: the gate and progress read the column,
        # not the journal mark, so the designated reviewers must be there too
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        # the CLI reads the same thing through the recovery view
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["r1", "r2"])

    def test_a_new_freeze_clears_the_previous_automatic_designation(self):
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        # a new head, no independent reviewer left: the old names must go
        self.repo.write("src/ameesh/work.py", "another lot\n")
        self.frozen = self.repo.commit("another lot")
        self.receipts()
        for name in ("r1", "r2"):
            registry.upsert(self.db, name, harness="deepseek")
        _, again = self.freeze()
        self.assertEqual(again["state"], rd.WAITING, again)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        self.assertTrue([n for n in self.notes() if "designation of freeze" in n
                         and "cleared" in n])

    def test_a_stale_selection_recovery_does_not_clear_the_new_freeze(self):
        # B/P1 of the Codex review cx2: a recovery of F1 selected r1,r2 and
        # committed them, then F2 (no capacity) was frozen and cleared the
        # column; F1's delayed sync re-registered r1,r2 for the head of F2.
        # A replay of a freeze that is no longer the latest writes nothing.
        for name in ("r1", "r2"):
            registry.upsert(self.db, name, harness="deepseek")
        first_sha = self.frozen
        first, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        # capacity is back, but the head moves before the delayed recovery sync
        for name, harness in (("r1", "claude"), ("r2", "codex")):
            registry.upsert(self.db, name, harness=harness)
        self.repo.write("src/ameesh/work.py", "new head\n")
        self.frozen = self.repo.commit("new head")
        self.receipts()
        _, frozen_again = self.freeze()
        self.assertEqual(frozen_again["state"], rd.DESIGNATED, frozen_again)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        before_mail = len(self.mailbox())
        before_note = len(self.notes())
        replay = act.designate(
            self.cfg, self.db, self.lot,
            freeze={"id": int(first["id"]), "sha": first_sha})
        self.assertEqual(replay["state"], "stale", replay)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.notes()), before_note)

    def test_a_stale_unavailable_freeze_does_not_clear_the_new_freeze(self):
        # The paths that erase the column are gated too: an old freeze whose
        # evidence is now unavailable must not clear the new head's reviewers.
        for name in ("r1", "r2"):
            registry.upsert(self.db, name, harness="deepseek")
        first, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        for name, harness in (("r1", "claude"), ("r2", "codex")):
            registry.upsert(self.db, name, harness=harness)
        self.repo.write("src/ameesh/work.py", "new head\n")
        self.frozen = self.repo.commit("new head")
        self.receipts()
        _, frozen_again = self.freeze()
        self.assertEqual(frozen_again["state"], rd.DESIGNATED, frozen_again)
        before_mail = len(self.mailbox())
        before_note = len(self.notes())
        replay = act.designate(
            self.cfg, self.db, self.lot,
            freeze={"id": int(first["id"]), "sha": "f" * 40})
        self.assertEqual(replay["state"], "stale", replay)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.notes()), before_note)

    def test_a_stale_light_freeze_does_not_clear_the_new_freeze(self):
        # Same guard on the light/no-path branch, which also writes the column.
        lot_head = self.frozen
        self.frozen = self.base
        first, light = self.freeze()
        self.assertEqual(light["state"], rd.NONE, light)
        self.frozen = lot_head
        self.repo.write("src/ameesh/work.py", "new head\n")
        self.frozen = self.repo.commit("new head")
        self.receipts()
        _, frozen_again = self.freeze()
        self.assertEqual(frozen_again["state"], rd.DESIGNATED, frozen_again)
        before_mail = len(self.mailbox())
        before_note = len(self.notes())
        replay = act.designate(
            self.cfg, self.db, self.lot,
            freeze={"id": int(first["id"]), "sha": self.base})
        self.assertEqual(replay["state"], "stale", replay)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.notes()), before_note)

    def test_the_selection_and_the_reviewer_column_commit_together(self):
        # The mark, the mails and the column are one transaction: a failure in
        # the column write cannot leave a designation that is only half visible.
        for name in ("r1", "r2"):
            registry.upsert(self.db, name, harness="deepseek")
        _, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        for name, harness in (("r1", "claude"), ("r2", "codex")):
            registry.upsert(self.db, name, harness=harness)
        marked = len(self.marks())
        mailed = len(self.mailbox())
        with mock.patch.object(act, "_sync_reviewers",
                               side_effect=RuntimeError("injected sync failure")):
            with self.assertRaises(RuntimeError):
                act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(len(self.marks()), marked)
        self.assertEqual([pg.designation_of(mark)["state"] for mark in self.marks()],
                         [rd.WAITING])
        self.assertEqual(len(self.mailbox()), mailed)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        # the clean replay finalizes the same freeze, mailing each reviewer once
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual([message["recipient"] for message in self.mailbox()],
                         ["pilot", "r1", "r2"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])

    def test_a_recovery_without_a_pilot_still_refuses_the_wait(self):
        # Outside the hook an undeliverable capacity alert is still a refusal
        # (`at_freeze=False`), as before the recovery moved into one
        # transaction: only the freeze hook turns it into a waiting mark.
        for name in ("r1", "r2"):
            registry.upsert(self.db, name, harness="deepseek")
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="author",
                       on_frozen=lambda tx, milestone, threads: None,
                       after_commit=[])
        self.db.execute("DELETE FROM agent_registry WHERE name = 'pilot'")
        marked = len(self.marks())
        mailed = len(self.mailbox())
        noted = len(self.notes())
        out = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(out["state"], "unavailable", out)
        self.assertIn("recipient", out["why"])
        self.assertEqual(len(self.marks()), marked)
        self.assertEqual(len(self.mailbox()), mailed)
        self.assertEqual(len(self.notes()), noted + 1)

    def test_the_last_good_commit_wins_when_the_state_is_invalid(self):
        # B2 of the Codex review cx2: a failed current pass (status invalid)
        # must not fall back to the non-audited ref. The policy was two
        # reviews at the audited commit, one at HEAD.
        audited = self.canon.commit
        fresh = self.canon.update(
            "federation.yaml",
            FEDERATION.replace("            reviews: 2", "            reviews: 1"))
        storage.of(self.db).canon.record_state(
            self.cfg.host, "ok", root=self.canon.root,
            source="home @ %s" % audited[:12], commit=audited, good=True,
            diagnostic="", canon="")
        storage.of(self.db).canon.record_state(
            self.cfg.host, "invalid", root=self.canon.root,
            source="home @ %s" % fresh[:12], commit=fresh, good=False,
            diagnostic="probe: current pass invalid", canon="")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 2)
        self.assertEqual(len(out["reviewers"]), 2)

    def test_an_exception_during_the_selection_leaves_no_partial_mail(self):
        # B3 of the Codex review cx2: the first mail must not survive without
        # its mark. A savepoint rolls the attempt back; only the waiting note
        # and the pilot alert remain, and a replay mails each reviewer once.
        real = mail.send
        calls = []

        def failing(db, sender, recipient, body, *args, **kwargs):
            calls.append(recipient)
            if len(calls) == 2:
                raise RuntimeError("injected failure before the second mail")
            return real(db, sender, recipient, body, *args, **kwargs)

        frozen = self.frozen
        with mock.patch.object(mail, "send", side_effect=failing):
            row, out = self.freeze()
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(out["state"], "unavailable", out)
        self.assertIn("internal designation error", out["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])
        self.assertEqual(self.marks(), [])
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        self.assertEqual(frozen, self.frozen)
        # a replay designates the same head, without a duplicate r1 mail
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual([m["recipient"] for m in self.mailbox()],
                         ["pilot", "r1", "r2"])
        self.assertEqual(len(self.marks()), 1)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])

    def test_the_policy_is_read_at_the_audited_commit_not_the_configured_ref(self):
        # HEAD says one review; the audited commit says two. The activation
        # must obey the audited commit, not the fresh ref.
        audited = self.canon.commit
        self.canon.update(
            "federation.yaml",
            FEDERATION.replace("            reviews: 2", "            reviews: 1"))
        storage.of(self.db).canon.record_state(
            self.cfg.host, "ok", root=self.canon.root,
            source="home @ %s" % audited[:12], commit=audited, good=True,
            diagnostic="", canon="")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 2)
        self.assertEqual(len(out["reviewers"]), 2)

    def test_no_reviewer_free_is_an_explicit_wait_and_a_capacity_alert(self):
        for name in ("r1", "r2"):
            registry.upsert(self.db, name, harness="deepseek")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["reviewers"], [])
        self.assertEqual(out["missing"], 2)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])
        self.assertIn("Designation at freeze impossible", self.mailbox()[0]["body"])
        self.assertEqual(pg.designation_of(self.marks()[0])["state"], rd.WAITING)

    def test_the_real_session_family_decides_not_the_declared_harness(self):
        # r1 declares claude but runs deepseek: it is the author's family and
        # must not be designated. r2 (codex) is the only independent reviewer.
        self.db.execute(
            "UPDATE agent_registry SET session_id='s-r1', session_origin='s-r1',"
            " session_harness='deepseek' WHERE name='r1'")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["missing"], 1)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])
        # capacity comes back: the same freeze is designated by the recovery
        self.db.execute(
            "UPDATE agent_registry SET session_harness='claude' WHERE name='r1'")
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual([r["name"] for r in again["reviewers"]], ["r1", "r2"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot", "r1", "r2"])

    def test_an_unknown_commit_provenance_waits_instead_of_guessing(self):
        self.db.execute("DELETE FROM provenance_receipts")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("provenance unknown", out["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])

    def test_an_unknown_author_family_waits(self):
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("UPDATE provenance_receipts SET harness = 'other'")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("author family unknown", out["why"])
        self.assertEqual(out["reviewers"], [])

    def test_a_manual_reviewer_does_not_hide_an_unknown_author_family(self):
        # Deux manuels de familles distinctes ne prouvent rien si la famille
        # de l'auteur est inconnue : la designation attend et alerte, elle ne
        # se declare pas suffisante (#2627).
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r2",
                                actor="human:smichea")
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("UPDATE provenance_receipts SET harness = 'other'")
        before = len(self.mailbox())
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("author family unknown", out["why"])
        self.assertEqual(out["reviewers"], [])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["pilot"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])

    def test_a_manual_reviewer_keeps_the_journaled_exception(self):
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r2",
                                actor="human:smichea")
        before = len(self.mailbox())
        _, out = self.freeze()
        self.assertEqual(out["state"], "manual", out)
        self.assertEqual(len(self.mailbox()), before)
        self.assertEqual(self.marks(), [])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        self.assertTrue([n for n in self.notes() if "manual reviewer(s)" in n])

    def test_an_insufficient_manual_designation_gets_the_missing_reviewer(self):
        # #2627 / constat #2551 : deux avis requis, un seul relecteur manuel
        # (claude). Le manque est désigné, le manuel reste en place.
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        before = len(self.mailbox())
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 2)
        self.assertEqual(out["missing"], 0)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r2"])
        self.assertEqual([r["family"] for r in out["reviewers"]], ["codex"])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["r2"])
        self.assertEqual(self.mailbox()[before]["kind"], "request")
        self.assertIn("review 2/2", self.mailbox()[before]["body"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        mark = pg.designation_of(self.marks()[0])
        self.assertEqual(mark["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in mark["reviewers"]], ["r2"])
        self.assertEqual(mark["required"], 2)
        self.assertEqual(mark["missing"], 0)

    def test_the_recovery_completes_an_insufficient_manual_designation(self):
        # Le gel a eu lieu sans designation (hook explicite) : la reprise
        # `work reviewers --apply` complète le manuel avec la même sélection.
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                       actor="author",
                       on_frozen=lambda tx, milestone, threads: None,
                       after_commit=[])
        before = len(self.mailbox())
        out = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r2"])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["r2"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])

    def test_a_manual_reviewer_of_the_author_family_does_not_count(self):
        # Le manuel r1 tourne en realite sous deepseek, la famille de l'auteur :
        # il ne couvre aucun siege, les deux avis automatiques partent.
        registry.upsert(self.db, "r3", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = 'r3'")
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        self.db.execute(
            "UPDATE agent_registry SET session_id='s-r1', session_origin='s-r1',"
            " session_harness='deepseek' WHERE name='r1'")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 2)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r2", "r3"])
        self.assertEqual([r["family"] for r in out["reviewers"]], ["codex", "claude"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2", "r3"])

    def test_a_manual_waiver_is_never_overridden(self):
        # #1979 : la dispense `-none` est un geste d'autorite. Un gel renouvele
        # de la MEME tete la garde (`_lapse_waiver`), et la designation
        # n'ajoute ni relecteur, ni courrier, ni marque.
        work.move(self.db, self.lot, "qa")
        self.freeze()
        work.waive_review(self.cfg, self.db, self.lot, actor="human:smichea")
        before_mail = len(self.mailbox())
        before_marks = len(self.marks())
        _, out = self.freeze()
        self.assertEqual(out["state"], "manual", out)
        self.assertIn("-none", out["why"])
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)
        raw = self.db.query("SELECT reviewers FROM work_items WHERE id = %s",
                            (self.lot,))[0]["reviewers"]
        self.assertEqual(list(raw), ["-none"])

    def test_the_critical_floor_of_the_merge_gate_is_the_requirement(self):
        # #2627 : le canon peut ne declarer qu'un avis sur exploitation.py ;
        # la porte de fusion en exige deux (chemin critique). La designation
        # doit lire le meme besoin que le feu, sinon le lot reste court.
        self.repo.write("src/ameesh/exploitation.py", "lot\n")
        self.frozen = self.repo.commit("critical path")
        self.receipts()
        canon = _Canon(os.path.join(self.tmp, "canon-un-avis"),
                       federation=FEDERATION.replace("            reviews: 2",
                                                     "            reviews: 1"))
        cfg = dataclasses.replace(self.cfg, canon=canon.root, canon_ref="HEAD")
        _, out = self.freeze(cfg=cfg)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 2)
        self.assertEqual(len(out["reviewers"]), 2)

    def test_no_canon_is_silent_and_keeps_the_freeze(self):
        cfg = dataclasses.replace(self.cfg, canon="")
        row, out = self.freeze(cfg=cfg)
        self.assertEqual(out["state"], "unavailable", out)
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.marks(), [])
        self.assertEqual(self.notes(act.UNAVAILABLE_PREFIX), [])
        frozen = [m for m in work.milestones(self.db, self.lot)
                  if m["kind"] == "frozen"]
        self.assertEqual([int(m["id"]) for m in frozen], [int(row["id"])])

    def test_a_canon_without_mechanics_is_reported_once_and_mailed(self):
        bad = _Canon(os.path.join(self.tmp, "canon-sans-mecanique"), mechanics=None)
        cfg = dataclasses.replace(self.cfg, canon=bad.root, canon_ref="HEAD")
        _, out = self.freeze(cfg=cfg)
        self.assertEqual(out["state"], "unavailable", out)
        self.assertIn("policy unavailable", out["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])
        self.assertEqual(len(self.notes(act.UNAVAILABLE_PREFIX)), 1)
        # a replay of the same freeze does not add a second note or mail
        act.designate(cfg, self.db, self.lot)
        self.assertEqual(len(self.notes(act.UNAVAILABLE_PREFIX)), 1)
        self.assertEqual(len(self.mailbox()), 1)


class ActivationCliTest(_ActivationFixture, PgTestCase):
    """The production caller: `ameesh work milestone <lot> frozen`."""

    def test_the_cli_freezes_and_designates(self):
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", self.frozen,
            "--actor", "author",
            env=self.env(AMEESH_CANON=self.canon.root, AMEESH_CANON_REF="HEAD"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("reviewers: r1 (claude), r2 (codex)", proc.stdout)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        self.assertEqual(len(self.marks()), 1)

    def test_the_cli_recovery_retries_the_same_freeze(self):
        self.freeze()  # first freeze: designated
        self.db.execute("DELETE FROM agent_mailbox")
        proc = self.mesh("work", "reviewers", str(self.lot), "--apply", "--json",
                         env=self.env(AMEESH_CANON=self.canon.root,
                                      AMEESH_CANON_REF="HEAD"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('"state": "designated"', proc.stdout)
        # a designating replay does not mail a second time
        self.assertEqual(self.mailbox(), [])
