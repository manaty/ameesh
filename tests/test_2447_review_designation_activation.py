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

from ameesh import registry, review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg
from ameesh import storage, work

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
        self.repo.write("src/ameesh/db.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/db.py", "lot\n")
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
            "SELECT recipient, body, payload FROM agent_mailbox ORDER BY id")

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
        self.assertIn(self.frozen, self.mailbox()[0]["body"])
        self.assertEqual(len(self.marks()), 1)
        self.assertEqual(pg.designation_of(self.marks()[0])["state"], rd.DESIGNATED)
        # the CLI reads the same thing through the recovery view
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["r1", "r2"])

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

    def test_a_manual_reviewer_keeps_the_journaled_exception(self):
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        before = len(self.mailbox())
        _, out = self.freeze()
        self.assertEqual(out["state"], "manual", out)
        self.assertEqual(len(self.mailbox()), before)
        self.assertEqual(self.marks(), [])
        self.assertTrue([n for n in self.notes() if "manual reviewer(s)" in n])

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
