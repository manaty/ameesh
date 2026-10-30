# SPDX-License-Identifier: AGPL-3.0-only
"""#2447 — activation of the reviewer designation at the `frozen` milestone.

Real database: the freeze hook reads the canon policy at the audited commit,
the frozen diff and the exact provenance of its commits, selects the reviewers
of the real session families and writes the designation in the milestone's
transaction. A missing policy or an unknown author is an explicit wait, never
a guessed reviewer; the recovery path replays the same freeze.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import shutil
import subprocess
import threading
import time
import types
from unittest import mock

from ameesh import db as db_mod
from ameesh import freeze_gate, lot_class, mail, registry, review_authors, review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg
from ameesh import review_provenance as prov
from ameesh import storage, verdict_carry, work
from ameesh.storage.postgres import registry as pg_registry
from ameesh.storage.postgres import work as pg_work
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


def _federation(*, risk_paths=("src/ameesh/**",), rule_paths=("src/ameesh/**",),
                reviews=2):
    """FEDERATION avec d'autres portées de classe et de `train_reviews`."""
    text = FEDERATION.replace(
        '          - paths:\n              - "src/ameesh/**"\n            class: sensitive',
        "          - paths:\n"
        + "".join('              - "%s"\n' % path for path in risk_paths)
        + "            class: sensitive")
    return text.replace(
        '          - paths:\n              - "src/ameesh/**"\n            reviews: 2',
        "          - paths:\n"
        + "".join('              - "%s"\n' % path for path in rule_paths)
        + "            reviews: " + str(reviews))


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

    def git(self, *args: str, env=None) -> str:
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True, env=env or self.dry)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message: str, *, agent: str = "") -> str:
        env = None
        if agent:
            address = "%s@ameesh.invalid" % agent
            env = dict(self.dry, GIT_AUTHOR_NAME=agent, GIT_AUTHOR_EMAIL=address,
                       GIT_COMMITTER_NAME=agent, GIT_COMMITTER_EMAIL=address)
        self.git("add", "-A")
        self.git("commit", "-qm", message, env=env)
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


def register_agent(db, name, **kwargs):
    """#4779 : enregistre un agent de test avec un profil de relecteur prouvé.

    Le registre porte `capabilities` et `roles` depuis `canon sync` ; une fiche
    de test sans ces champs ne prouve pas la capacité `propose` et ne serait
    plus candidate. Le helper les pose après l'inscription, pour tous les
    agents du fichier (auteurs et pilotes compris, sans effet sur eux).
    """
    row = registry.upsert(db, name, **kwargs)
    db.execute(
        "UPDATE agent_registry SET capabilities ="
        " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
        " WHERE name = %s", (row["name"],))
    return row


class _ActivationFixture:
    """A frozen lot, a canon, a registry with two independent families."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM canon_state")
        # #2668: the gauge history is shared by the mesh; a test must not
        # inherit another test's plan pace (or the host's live readings)
        self.db.execute("DELETE FROM quota_gauge_readings")
        # #4779 : les agents de ces témoins sont des relecteurs : une ligne
        # neuve du registre de test reçoit le profil que `canon sync` écrit en
        # production (capacité `propose`, un rôle). Les témoins du refus
        # posent explicitement l'absence.
        self.db.execute(
            "ALTER TABLE agent_registry ALTER COLUMN capabilities SET DEFAULT"
            " ARRAY['read','report-drift','propose']::text[]")
        self.db.execute(
            "ALTER TABLE agent_registry ALTER COLUMN roles SET DEFAULT"
            " ARRAY['implementation']::text[]")
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        # `mail.py` reste sensible hors du périmètre critique : #1515 y a fait
        # entrer `work.py`, qui servait de chemin neutre à ces témoins.
        self.repo.write("src/ameesh/mail.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/mail.py", "lot\n")
        self.frozen = self.repo.commit("lot")
        self.canon = _Canon(os.path.join(self.tmp, "canon"))
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root, canon_ref="HEAD")
        for name, harness in (("author", "deepseek"), ("r1", "claude"),
                              ("r2", "codex"), ("pilot", "claude")):
            register_agent(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            # #4779 : les relecteurs de test portent la capacité `propose` et
            # un rôle, comme les agents du canon.
            self.db.execute(
                "UPDATE agent_registry SET team = 'ameesh', capabilities ="
                " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
                " WHERE name = %s", (name,))
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

    def turn(self, agent, harness, sha):
        """A finished turn of `agent` under `harness`, at the commit instant."""
        stamp = self.repo.git("log", "-1", "--format=%ct", sha)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES (%s, %s, %s, to_timestamp(%s))",
            (agent, harness, "s-" + harness, stamp))

    def four_families(self):
        """Un canon à quatre familles, dont une sans agent disponible.

        Les témoins de provenance (#2721/#2755/#3584) construisent une tête à
        deux familles d'auteurs et deux avis requis : depuis #3742 le gel
        refuse cette tête. La famille supplémentaire garde la tête relisible
        (deux familles éligibles) tout en laissant la désignation attendre la
        capacité, ce que ces témoins vérifient.
        """
        text = MECHANICS.replace(
            "families: [claude, codex, deepseek]",
            "families: [claude, codex, deepseek, gemini]")
        self.canon = _Canon(
            os.path.join(self.tmp, "canon-4-" + self._testMethodName),
            mechanics=text)
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root,
                                       canon_ref="HEAD")

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


class FreezeIntakeTest(_ActivationFixture, PgTestCase):
    """#3467: an author freezes an intake lot without a pilot's state moves."""

    def setUp(self):
        super().setUp()
        self.db.execute("UPDATE work_items SET state = 'intake' WHERE id = %s",
                        (self.lot,))

    def test_intake_freeze_enters_qa_and_designates_live_reviewers(self):
        register_agent(self.db, "dead-reviewer", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="dead")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("dead-reviewer",))
        _mark, out = self.freeze()
        item = work.get(self.db, self.lot)
        self.assertEqual(item["state"], "qa")
        self.assertEqual(set(designated(item)), {"r1", "r2"})
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        moves = self.db.query(
            "SELECT state, actor FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s ORDER BY id", (self.lot, "%frozen (#3467)"))
        self.assertEqual([(r["state"], r["actor"]) for r in moves],
                         [("build", "author"), ("qa", "author")])

    def test_pinned_freeze_keeps_process_state_and_designates_reviewers(self):
        for state in ("intake", "build"):
            with self.subTest(state=state):
                self.db.execute(
                    "UPDATE work_items SET state = %s, workflow_ref = %s WHERE id = %s",
                    (state, "process:delivery@revision", self.lot))
                mark, out = self.freeze()
                item = work.get(self.db, self.lot)
                self.assertEqual(item["state"], state)
                self.assertEqual(item["workflow_ref"], "process:delivery@revision")
                self.assertEqual(set(designated(item)), {"r1", "r2"})
                self.assertEqual(out["state"], rd.DESIGNATED)
                self.assertIn(mark["id"], [m["id"] for m in
                                         work.milestones(self.db, self.lot)])
        self.assertEqual({m["recipient"] for m in self.mailbox()}, {"r1", "r2"})
        self.assertEqual(self.db.query(
            "SELECT id FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s", (self.lot, "%frozen (#3467)")), [])

    def test_hook_failure_rolls_back_state_and_freeze(self):
        before = len(work.milestones(self.db, self.lot))

        def fail(tx, mark, threads):
            self.assertEqual(work.get(tx, self.lot)["state"], "qa")
            raise RuntimeError("designation failed")

        with self.assertRaisesRegex(RuntimeError, "designation failed"):
            self.freeze(hook=fail)
        self.assertEqual(work.get(self.db, self.lot)["state"], "intake")
        self.assertEqual(len(work.milestones(self.db, self.lot)), before)
        self.assertEqual(self.db.query(
            "SELECT id FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s", (self.lot, "%frozen (#3467)")), [])

    def test_refreeze_in_qa_does_not_repeat_the_transition(self):
        self.freeze()
        self.freeze()
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        rows = self.db.query(
            "SELECT id FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s", (self.lot, "%frozen (#3467)"))
        self.assertEqual(len(rows), 2)

    def test_freeze_does_not_lift_a_blocked_state(self):
        work.move(self.db, self.lot, "blocked", actor="author")
        self.freeze()
        self.assertEqual(work.get(self.db, self.lot)["state"], "blocked")


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

    def test_a_light_freeze_uses_the_declared_floor(self):
        # #2642 : un diff léger non vide demande le plancher du canon
        # (`reviews.light_reviews: 1`), pas zéro ; la désignation le lit par
        # `mechanics.reviews_for` et le passe à la sélection sans le réinventer.
        self.repo.write("docs/guide.md", "base\n")
        self.base = self.repo.commit("docs base")
        self.repo.write("docs/guide.md", "lot\n")
        self.frozen = self.repo.commit("docs lot")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.receipts()
        text = MECHANICS.replace("  distinct_families: true",
                                 "  distinct_families: true\n  light_reviews: 1")
        self.canon = _Canon(os.path.join(self.tmp, "canon-light"), mechanics=text)
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root, canon_ref="HEAD")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 1)
        self.assertEqual(len(out["reviewers"]), 1)
        self.assertNotEqual(out["reviewers"][0]["family"], "deepseek")
        self.assertEqual([m["recipient"] for m in self.mailbox()],
                         [out["reviewers"][0]["name"]])

    def test_the_freeze_prefers_the_raw_plan_pace_over_declared_multipliers(self):
        # #2668: one review due; Codex is 16 points ahead of its raw plan pace
        # and Claude is at its pace. Claude must be designated even though
        # Codex's agent is less loaded. The declared ×10 (Claude) and ×3
        # (Codex) profiles are deliberately ignored: with them Claude would
        # look 20 points behind and Codex 21.8, designating Codex, the inverse
        # of the witness (B of mesh-design on 4123b99). A Fable-only bucket
        # at 95 % is ignored for this Claude agent (no model declared), so it
        # cannot contaminate its general reading (cx3 B). With no current
        # reading, the load rule returns.
        self.repo.write("docs/guide.md", "base\n")
        self.base = self.repo.commit("docs base")
        self.repo.write("docs/guide.md", "lot\n")
        self.frozen = self.repo.commit("docs lot")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.receipts()
        text = MECHANICS.replace("  distinct_families: true",
                                 "  distinct_families: true\n  light_reviews: 1")
        self.canon = _Canon(os.path.join(self.tmp, "canon-pace"), mechanics=text)
        self.cfg = dataclasses.replace(
            self.cfg, canon=self.canon.root, canon_ref="HEAD",
            accounts={
                "claude": [{"name": "secondaire", "type": "config_dir",
                            "path": os.path.join(self.tmp, "claude-10"),
                            "pace_multiplier": 10.0}],
                "codex": [{"name": "secondaire", "type": "config_dir",
                           "path": os.path.join(self.tmp, "codex-3"),
                           "pace_multiplier": 3.0}],
            })
        for index in range(5):
            extra = work.add(self.db, title="load %d" % index, app="ameesh",
                             assignee="r1")
            work.move(self.db, extra["id"], "build")
        now = time.time()
        week = 7 * 86400.0
        self.db.execute(
            "INSERT INTO quota_gauge_readings"
            " (harness, gauge_key, used, resets_at, window_s, account, produced_at, source)"
            " VALUES ('codex', 'seven_day', 0.35, to_timestamp(%s), %s, 'secondaire',"
            "         to_timestamp(%s), 'test'),"
            "        ('claude', 'seven_day', 0.60, to_timestamp(%s), %s, 'secondaire',"
            "         to_timestamp(%s), 'test'),"
            "        ('claude', 'seven_day@fable', 0.95, to_timestamp(%s), %s,"
            "         'secondaire', to_timestamp(%s), 'test')",
            (now + 0.81 * week, week, now, now + 0.40 * week, week, now,
             now + 0.40 * week, week, now))
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 1)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"])
        # stale/absent readings are neutral: the load decides again
        self.db.execute("DELETE FROM quota_gauge_readings")
        self.repo.write("docs/guide.md", "another lot\n")
        self.frozen = self.repo.commit("another lot")
        self.receipts()
        _, again = self.freeze()
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual([r["name"] for r in again["reviewers"]], ["r2"])

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
            register_agent(self.db, name, harness="deepseek")
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
            register_agent(self.db, name, harness="deepseek")
        first_sha = self.frozen
        first, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        # capacity is back, but the head moves before the delayed recovery sync
        for name, harness in (("r1", "claude"), ("r2", "codex")):
            register_agent(self.db, name, harness=harness)
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
            register_agent(self.db, name, harness="deepseek")
        first, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        for name, harness in (("r1", "claude"), ("r2", "codex")):
            register_agent(self.db, name, harness=harness)
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
        # #2786: a freeze already in the target is refused, so the empty diff
        # is an empty commit on top of the base, not the base itself.
        lot_head = self.frozen
        branch = self.repo.git("rev-parse", "--abbrev-ref", "HEAD")
        self.repo.git("checkout", "-q", self.base)
        self.repo.git("commit", "-q", "--allow-empty", "-m", "gel vide")
        self.frozen = self.repo.git("rev-parse", "HEAD")
        # #2924 : le gel suivant relit la provenance des gels antérieurs ; ce
        # commit porte donc son reçu comme tout commit du lot, même si un gel
        # sans chemin ne lit pas ses propres auteurs.
        self.receipts(commits=[self.frozen])
        first, light = self.freeze()
        self.assertEqual(light["state"], rd.NONE, light)
        self.repo.git("checkout", "-q", branch)
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
            register_agent(self.db, name, harness="deepseek")
        _, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        for name, harness in (("r1", "claude"), ("r2", "codex")):
            register_agent(self.db, name, harness=harness)
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
            register_agent(self.db, name, harness="deepseek")
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
            register_agent(self.db, name, harness="deepseek")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["reviewers"], [])
        self.assertEqual(out["missing"], 2)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])
        self.assertIn("Designation at freeze impossible", self.mailbox()[0]["body"])
        self.assertEqual(pg.designation_of(self.marks()[0])["state"], rd.WAITING)

    def test_a_partial_designation_keeps_its_seat_on_retry_and_is_not_manual(self):
        # #3596 : deux avis requis, codex absent au gel. Claude (r1) est nommé
        # tout de suite, le manque est notifié, et le siège automatique ne
        # devient pas une désignation manuelle (il compterait deux fois).
        self.db.execute(
            "UPDATE agent_registry SET status = 'stopped' WHERE name = 'r2'")
        _mark, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"])
        self.assertEqual(out["missing"], 1)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot", "r1"])
        self.assertIn("Designation at freeze incomplete", self.mailbox()[0]["body"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])
        self.assertEqual(act._manual(self.db, work.get(self.db, self.lot)), [])
        # codex revient : la reprise du même gel ajoute codex seul, claude reste.
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle' WHERE name = 'r2'")
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual([r["name"] for r in again["reviewers"]], ["r1", "r2"])
        self.assertEqual([m["recipient"] for m in self.mailbox()],
                         ["pilot", "r1", "r2"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        self.assertEqual(act._manual(self.db, work.get(self.db, self.lot)), [])
        self.assertEqual(len(self.marks()), 2)

    def test_the_real_session_family_decides_not_the_declared_harness(self):
        # r1 declares claude but runs deepseek: it is the author's family and
        # must not be designated. r2 (codex) is the only independent reviewer,
        # and #3596 provides its seat at once while the second one waits.
        self.db.execute(
            "UPDATE agent_registry SET session_id='s-r1', session_origin='s-r1',"
            " session_harness='deepseek' WHERE name='r1'")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r2"])
        self.assertEqual(out["missing"], 1)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot", "r2"])
        # capacity comes back: the same freeze is designated by the recovery
        self.db.execute(
            "UPDATE agent_registry SET session_harness='claude' WHERE name='r1'")
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual([r["name"] for r in again["reviewers"]], ["r2", "r1"])
        self.assertEqual([m["recipient"] for m in self.mailbox()],
                         ["pilot", "r2", "r1"])

    def test_an_unknown_commit_provenance_waits_instead_of_guessing(self):
        self.db.execute("DELETE FROM provenance_receipts")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("provenance unknown", out["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])

    def test_a_rebased_commit_is_read_from_the_turn_journal(self):
        """#2721 — un rebase crée un nouvel objet sans reçu.

        Le reçu reste sur l'ancien commit ; le nouveau n'en a pas. Le committer
        (l'agent qui a réécrit) porte l'adresse d'agent et le journal des tours
        donne sa famille au commit — sans écrire de faux reçu, que le train
        continuerait de refuser."""
        self.repo.git("checkout", "-q", "-B", "topic", self.base)
        self.repo.write("src/ameesh/work.py", "lot rebased\n")
        old = self.repo.commit("lot", agent="author")
        self.receipts(commits=[old])
        # une vraie base neuve, puis un vrai rebase : la tête change d'objet
        self.repo.git("checkout", "-q", "-B", "main", self.base)
        self.repo.write("src/ameesh/other.py", "base 2\n")
        new_base = self.repo.commit("base 2")
        self.repo.git("checkout", "-q", "topic")
        address = "author@ameesh.invalid"
        rebase_env = dict(self.repo.dry, GIT_COMMITTER_NAME="author",
                          GIT_COMMITTER_EMAIL=address)
        self.repo.git("rebase", new_base, env=rebase_env)
        rebased = self.repo.git("rev-parse", "HEAD")
        self.assertNotEqual(rebased, old)
        self.assertEqual(self.repo.git("log", "-1", "--format=%ae", rebased),
                         address)
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (new_base, self.lot))
        self.frozen = rebased
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1", "r2"])
        self.assertNotIn("provenance unknown", out["why"])
        # le repli est une trace, pas un reçu : l'ancien objet garde le sien
        self.assertIsNone(prov.receipt_for_commit(self.db, rebased))
        self.assertEqual(prov.receipt_for_commit(self.db, old)["actor"], "author")
        mark = pg.designation_of(self.marks()[0])
        author = [a for a in mark["authors"] if a["name"] == "author"][0]
        self.assertEqual(author["family"], "deepseek")
        self.assertEqual(author["proof"], "turn-journal")
        self.assertEqual(author["fallback_commits"], [rebased[:12]])

    def test_a_commit_older_than_the_hooks_is_read_from_the_turn_journal(self):
        """#2721 — un commit antérieur aux hooks n'a aucun reçu.

        Le journal des tours de son committer reste la trace historique : il
        est admis au lieu de bloquer la désignation au gel."""
        self.repo.git("checkout", "-q", "-B", "prehooks", self.base)
        self.repo.write("src/ameesh/work.py", "avant les hooks\n")
        head = self.repo.commit("avant les hooks", agent="author")
        self.db.execute("DELETE FROM provenance_receipts")
        self.frozen = head
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertNotIn("provenance unknown", out["why"])
        mark = pg.designation_of(self.marks()[0])
        author = [a for a in mark["authors"] if a["name"] == "author"][0]
        self.assertEqual(author["proof"], "turn-journal")
        self.assertEqual(author["fallback_commits"], [head[:12]])

    def test_a_committer_outside_the_registry_still_waits(self):
        """#2721 — un committer distinct de la référence doit être au registre.

        Une adresse d'agent que seul Git connaît ne suffit pas : le gel
        attend, plutôt que de créditer un auteur forgé."""
        self.repo.git("checkout", "-q", "-B", "ghost", self.base)
        self.repo.write("src/ameesh/work.py", "fantôme\n")
        head = self.repo.commit("fantôme", agent="ghost")
        self.db.execute("DELETE FROM provenance_receipts")
        self.frozen = head
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("provenance unknown", out["why"])

    def test_a_corrupt_receipt_is_not_replaced_by_the_turn_journal(self):
        """#2721 — le repli ne couvre qu'une preuve ABSENTE.

        Une ligne de preuve qui contredit son sujet est une erreur du
        fournisseur (`ProvenanceError`), pas une origine inconnue : le journal
        des tours ne doit pas la remplacer, le gel attend."""
        self.repo.git("checkout", "-q", "-B", "corrupt", self.base)
        self.repo.write("src/ameesh/work.py", "preuve corrompue\n")
        head = self.repo.commit("preuve corrompue", agent="author")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO UPDATE SET sha = EXCLUDED.sha",
            ("commit:" + head, "f" * 40, "author", "s-deepseek", "t",
             "deepseek", self.lot))
        self.frozen = head
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("provenance unknown", out["why"])

    def test_an_inter_agent_amend_counts_the_committer_as_author(self):
        """#2721 P1 cx2/dev1 — un amend substantiel par un relecteur.

        `git commit --amend --author=<auteur>` conserve l'auteur Git ; seul le
        committer dit qui a réécrit. Le relecteur r2/codex devient donc auteur
        de sa correction (> 50 lignes), avec sa famille de tour : il ne peut
        pas relire son propre contenu."""
        self.four_families()
        self.repo.git("checkout", "-q", "-B", "amend", self.base)
        self.repo.write("src/ameesh/work.py", "A\n" * 10)
        self.repo.commit("A", agent="author")
        self.repo.write("src/ameesh/work.py", "B\n" * 120)
        env = dict(self.repo.dry, GIT_AUTHOR_NAME="author",
                   GIT_AUTHOR_EMAIL="author@ameesh.invalid",
                   GIT_COMMITTER_NAME="r2",
                   GIT_COMMITTER_EMAIL="r2@ameesh.invalid")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--amend", "-m", "A amendé par r2",
                      env=env)
        amended = self.repo.git("rev-parse", "HEAD")
        self.db.execute("DELETE FROM provenance_receipts")
        self.turn("r2", "codex", amended)
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r2",
                                actor="human:smichea")
        self.frozen = amended
        _, out = self.freeze()
        authors = {row["name"]: row for row in out["authors"]}
        self.assertEqual(authors["r2"]["family"], "codex")
        self.assertEqual(authors["r2"]["proof"], "turn-journal")
        self.assertEqual(authors["r2"]["fallback_commits"], [amended[:12]])
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertNotIn("provenance unknown", out["why"])
        self.assertNotIn("r2", [r["name"] for r in out["reviewers"]])

    def test_a_small_inter_agent_amend_authors_its_family(self):
        """#2721 / §2 + #2739/#2888 — un amend de relecteur sous le seuil est un auteur.

        r2/codex a écrit un commit de la tête : sa famille est exclue comme
        celle de tout auteur, et il ne couvre aucun siège, même manuel. La
        sélection attend une famille indépendante ; elle ne désigne pas r2 sur
        sa propre correction (#2739), ni un autre agent codex (#2888)."""
        self.four_families()
        self.repo.git("checkout", "-q", "-B", "small-amend", self.base)
        self.repo.write("src/ameesh/work.py", "A\n" * 5)
        self.repo.commit("A", agent="author")
        self.repo.write("src/ameesh/work.py", "A\n" * 4 + "petit\n")
        env = dict(self.repo.dry, GIT_AUTHOR_NAME="author",
                   GIT_AUTHOR_EMAIL="author@ameesh.invalid",
                   GIT_COMMITTER_NAME="r2",
                   GIT_COMMITTER_EMAIL="r2@ameesh.invalid")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--amend", "-m", "petit correctif r2",
                      env=env)
        amended = self.repo.git("rev-parse", "HEAD")
        self.db.execute("DELETE FROM provenance_receipts")
        self.turn("r2", "codex", amended)
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r2",
                                actor="human:smichea")
        self.frozen = amended
        _, out = self.freeze()
        authors = {row["name"]: row for row in out["authors"]}
        self.assertEqual(authors["r2"]["family"], "codex")
        self.assertEqual(out["state"], rd.WAITING, out)
        # #3596 : la seule famille indépendante (claude, r1) est pourvue tout
        # de suite ; le second siège attend.
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"])
        self.assertEqual(out["missing"], 1)
        # le correcteur est un auteur de la tête : son nom et sa famille
        # (codex) sont interdits, comme pour tout auteur (#2888).
        self.assertIn("r2", out["why"])
        self.assertIn("codex", out["why"])

    def test_a_borrowed_harness_uses_the_commit_turn_family(self):
        """#2721 P1 cx2 — le harnais déclaré ne prouve pas la famille.

        L'agent `author` est déclaré deepseek au registre, mais son tour au
        commit est codex : c'est la famille du tour qui compte, donc codex, et
        l'unique famille indépendante restante (claude) est pourvue tout de
        suite ; le second avis attend (#3596)."""
        self.repo.git("checkout", "-q", "-B", "borrowed", self.base)
        self.repo.write("src/ameesh/work.py", "codex\n")
        head = self.repo.commit("codex", agent="author")
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM turn_costs")
        self.turn("author", "codex", head)
        self.db.execute("UPDATE agent_registry SET harness = 'deepseek'"
                        " WHERE name = 'author'")
        self.frozen = head
        _, out = self.freeze()
        author = [row for row in out["authors"] if row["name"] == "author"][0]
        self.assertEqual(author["family"], "codex")
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertNotIn("provenance unknown", out["why"])
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"])
        self.assertEqual(out["missing"], 1)

    def test_a_freeze_family_different_from_the_commit_turn_excludes_both(self):
        """#2755 / #2721 N, #3584 : deux familles prouvées sont toutes exclues.

        Le commit précède le tour de gel : codex et deepseek sont deux preuves
        historiques distinctes, pas une divergence avec le registre courant.
        #3584 : cet ensemble n'est ni inconnu ni ambigu — chacune de ses
        familles est exclue, et la désignation partielle (#3596) pourvoit le
        siège claude disponible en attendant l'autre famille.
        """
        self.four_families()
        self.repo.git("checkout", "-q", "-B", "rotation", self.base)
        stamp = str(int(time.time()) - 120) + " +0000"
        self.repo.dry.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        self.repo.write("src/ameesh/work.py", "codex avant gel deepseek\n")
        head = self.repo.commit("avant rotation", agent="author")
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM turn_costs")
        self.turn("author", "codex", head)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('author', 'deepseek', 's-freeze', now() + interval '1 minute')")
        self.frozen = head
        milestone, out = self.freeze()
        item = work.get(self.db, self.lot)
        self.assertEqual(review_authors.reference_author(
            self.db, item, milestone, ("claude", "codex", "deepseek")),
            {"name": "author", "family": "deepseek"})
        self.assertEqual(review_authors.actor_family(
            self.db, "author", float(self.repo.git("log", "-1", "--format=%ct", head)),
            ("claude", "codex", "deepseek")), "codex")
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("forbidden author families: codex, deepseek", out["why"])
        self.assertNotIn("unknown", out["why"])
        self.assertEqual(out["reviewers"], [{"name": "r1", "family": "claude"}])
        self.assertEqual(out["missing"], 1)
        mark = pg.designation_of(self.marks()[0])
        self.assertEqual(mark["reviewers"], [{"name": "r1", "family": "claude"}])
        self.assertEqual(mark["authors"], [{
            "name": "author", "family": "",
            "families": ["codex", "deepseek"], "proof": "turn-journal",
            "fallback_commits": [head[:12]],
        }])
        # #3596 : le siège claude est pourvu dès le gel, l'autre famille attend.
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot", "r1"])

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

    def test_a_manual_reviewer_with_an_unavailable_policy_alerts_and_says_unknown(self):
        # #2637 : prepare -> Unavailable avec un manuel ne doit plus faire croire
        # que les avis requis sont couverts, ni taire l'alerte pilote.
        bad = _Canon(os.path.join(self.tmp, "canon-sans-mecanique"), mechanics=None)
        cfg = dataclasses.replace(self.cfg, canon=bad.root, canon_ref="HEAD")
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        before = len(self.mailbox())
        _, out = self.freeze(cfg=cfg)
        self.assertEqual(out["state"], "manual", out)
        self.assertIn("coverage unknown", out["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["pilot"])
        notes = [n for n in self.notes() if "coverage of the required" in n]
        self.assertEqual(len(notes), 1)
        self.assertIn("manual reviewer(s) kept (r1)", notes[0])
        self.assertIn("policy unavailable", notes[0])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])

    def test_a_manual_reviewer_with_a_prepare_exception_alerts_and_says_unknown(self):
        # #2637 : prepare lève, le manuel reste ; l'alerte interne et la
        # couverture inconnue sont explicites, aucun relecteur n'est ajouté.
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        before = len(self.mailbox())
        with mock.patch.object(act, "prepare", side_effect=RuntimeError("injected")):
            _, out = self.freeze()
        self.assertEqual(out["state"], "manual", out)
        self.assertIn("coverage unknown", out["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["pilot"])
        notes = [n for n in self.notes() if "coverage of the required" in n]
        self.assertEqual(len(notes), 1)
        self.assertIn("internal designation error: RuntimeError: injected", notes[0])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])

    def test_a_manual_reviewer_without_a_canon_notes_unknown_coverage_silently(self):
        # #2637 : source silencieuse (aucun canon) : pas de courrier, mais la
        # note ne prétend pas non plus que la couverture est acquise.
        cfg = dataclasses.replace(self.cfg, canon="")
        work.move(self.db, self.lot, "qa")
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        before = len(self.mailbox())
        _, out = self.freeze(cfg=cfg)
        self.assertEqual(out["state"], "manual", out)
        self.assertEqual(len(self.mailbox()), before)
        notes = [n for n in self.notes() if "coverage of the required" in n]
        self.assertEqual(len(notes), 1)
        self.assertIn("no canon configured", notes[0])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])

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
        register_agent(self.db, "r3", harness="claude", host=self.cfg.host,
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
        # #2627 / #3542 : le canon couvre scripts/** d'une règle `**` à un
        # avis, mais ne le nomme pas ; le plancher critique de la porte reste
        # deux avis, et `**` ne l'abaisse pas.
        self.repo.write("scripts/deploy-cutoff.sh", "lot\n")
        self.frozen = self.repo.commit("critical path")
        self.receipts()
        canon = _Canon(os.path.join(self.tmp, "canon-un-avis"),
                       federation=_federation(risk_paths=("src/ameesh/**",
                                                          "scripts/**"),
                                              rule_paths=("**",), reviews=1))
        cfg = dataclasses.replace(self.cfg, canon=canon.root, canon_ref="HEAD")
        _, out = self.freeze(cfg=cfg)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 2)
        self.assertEqual(len(out["reviewers"]), 2)

    def test_a_canon_rule_that_names_the_path_is_authoritative(self):
        # #3542 : deploy/** est nommé à un avis par le canon. La désignation
        # lit le même besoin que la porte : un avis, même si CRITICAL_SCOPE
        # reconnaît deploy/.
        self.repo.write("deploy/bascule.py", "lot\n")
        self.frozen = self.repo.commit("named deploy path")
        self.receipts()
        canon = _Canon(os.path.join(self.tmp, "canon-deploy-un-avis"),
                       federation=_federation(risk_paths=("src/ameesh/**",
                                                          "deploy/**"),
                                              rule_paths=("deploy/**",
                                                          "src/ameesh/mail.py"),
                                              reviews=1))
        cfg = dataclasses.replace(self.cfg, canon=canon.root, canon_ref="HEAD")
        _, out = self.freeze(cfg=cfg)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 1)
        self.assertEqual(len(out["reviewers"]), 1)
        self.assertEqual([m["recipient"] for m in self.mailbox()],
                         [out["reviewers"][0]["name"]])

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
        # #2561: the recovery command is versioned, and the manual path stays
        # usable on an older installation (the incident's host ran 1.11.7).
        body = self.mailbox()[0]["body"]
        self.assertIn("ameesh >= 1.12.0", body)
        self.assertIn("ameesh work reviewer %d" % self.lot, body)
        # a replay of the same freeze does not add a second note or mail
        act.designate(cfg, self.db, self.lot)
        self.assertEqual(len(self.notes(act.UNAVAILABLE_PREFIX)), 1)
        self.assertEqual(len(self.mailbox()), 1)

    def test_a_light_lot_journals_no_pre_merge_reviewer(self):
        # #2561: a light lot has no pre-merge reviewer (canon §2, full table:
        # the other family reviews after merge). The freeze must say so once,
        # instead of leaving "no reviewer" to be read as a failed designation.
        # a fresh light-only branch: resetting to the base removes the
        # sensitive src/ameesh change of the fixture's first head
        self.repo.git("reset", "-q", "--hard", self.base)
        self.repo.write("docs/README.md", "doc\n")
        self.frozen = self.repo.commit("light doc")
        self.receipts()
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.NONE, out)
        self.assertIn("no pre-merge reviewer", out["why"])
        self.assertEqual(self.mailbox(), [])
        notes = self.notes(act.NONE_PREFIX)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("light lot", notes[0])
        # the read/recovery view explains it too, without a designation mark
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], rd.NONE)
        self.assertIn("no pre-merge reviewer", shown["designation"]["why"])
        # a replay of the same freeze adds no second note
        act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(len(self.notes(act.NONE_PREFIX)), 1)

    def test_a_freeze_run_outside_the_lot_worktree_finds_it_beside_the_agent(self):
        # #2561 (#2510): the freeze command ran from the agent's session
        # directory, so none of the historical hints held the ameesh commit.
        # The lot's worktree is the agent's sibling; the exact commit and the
        # issue's repository decide.
        work_root = os.path.join(self.tmp, "work")
        session = os.path.join(work_root, "session")
        os.makedirs(session)
        sibling = os.path.join(work_root, "author-%d" % self.lot)
        shutil.copytree(self.repo.root, sibling)
        subprocess.run(["git", "-C", sibling, "remote", "add", "origin",
                        "https://github.com/manaty/ameesh.git"], check=True,
                       capture_output=True)
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("manaty/ameesh#1932", self.lot))
        self.db.execute("UPDATE agent_registry SET cwd = %s WHERE name = 'author'",
                        (session,))
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(session)
        item = work.get(self.db, self.lot)
        cfg = dataclasses.replace(self.cfg, radar={})
        found = act._repository(self.db, cfg, item, self.frozen)
        self.assertEqual(os.path.realpath(found), os.path.realpath(sibling))

    def test_a_discovered_sibling_of_another_project_is_refused(self):
        # The exact commit is not a permission: a sibling clone whose origin is
        # another project must not provide the target refs. Without the issue
        # reference there is no identity at all, so the fallback keeps the old
        # hints only.
        work_root = os.path.join(self.tmp, "work-autre")
        session = os.path.join(work_root, "session")
        os.makedirs(session)
        sibling = os.path.join(work_root, "author-%d" % self.lot)
        shutil.copytree(self.repo.root, sibling)
        subprocess.run(["git", "-C", sibling, "remote", "add", "origin",
                        "https://github.com/manaty/nexlink.git"], check=True,
                       capture_output=True)
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("manaty/ameesh#1932", self.lot))
        self.db.execute("UPDATE agent_registry SET cwd = %s WHERE name = 'author'",
                        (session,))
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(session)
        item = work.get(self.db, self.lot)
        cfg = dataclasses.replace(self.cfg, radar={})
        self.assertEqual(act._repository(self.db, cfg, item, self.frozen), "")

    def test_a_frozen_head_only_on_origin_is_fetched(self):
        # #2561 (#2517): the head was pushed but no local clone held it. A clone
        # of the lot's repository (validated by the issue's origin) fetches the
        # exact head from the origin, bounded; the freeze then reads its diff.
        origin = os.path.join(self.tmp, "origin-fetch.git")
        subprocess.run(["git", "init", "--bare", "-q", origin], check=True,
                       capture_output=True)
        self.repo.git("push", "-q", origin,
                      "%s:refs/heads/lot/2561" % self.frozen)
        work_root = os.path.join(self.tmp, "work-fetch")
        session = os.path.join(work_root, "session")
        os.makedirs(session)
        local = _Repo(os.path.join(work_root, "author-%d" % self.lot))
        local.write("base.txt", "base\n")
        local.commit("base")
        local.git("remote", "add", "origin",
                  "https://github.com/manaty/ameesh.git")
        local.git("config", "url.%s.insteadOf" % origin,
                  "https://github.com/manaty/ameesh.git")
        self.db.execute("UPDATE work_items SET issue_ref = %s, branch = %s"
                        " WHERE id = %s",
                        ("manaty/ameesh#1932", "lot/2561", self.lot))
        self.db.execute("UPDATE agent_registry SET cwd = %s WHERE name = 'author'",
                        (session,))
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(session)
        item = work.get(self.db, self.lot)
        cfg = dataclasses.replace(self.cfg, radar={})
        # the freeze transaction never reaches the network: no fetch there
        self.assertEqual(act._repository(self.db, cfg, item, self.frozen,
                                         fetch=False), "")
        found = act._repository(self.db, cfg, item, self.frozen)
        self.assertEqual(os.path.realpath(found), os.path.realpath(local.root))
        self.assertEqual(local.git("cat-file", "-t", self.frozen), "commit")

    def test_a_canon_policy_arriving_later_is_retried_on_the_same_freeze(self):
        # #2561 (#2522/#1854): the canon publication arrived after the freeze.
        # The unavailable note is the retry marker; the same frozen head is
        # designated once the audited policy exists.
        bad = _Canon(os.path.join(self.tmp, "canon-tardif"), mechanics=None)
        cfg = dataclasses.replace(self.cfg, canon=bad.root, canon_ref="HEAD")
        self.freeze(cfg=cfg)
        self.assertEqual(self.marks(), [])
        self.assertEqual(len(self.notes(act.UNAVAILABLE_PREFIX)), 1)
        bad.repo.write("processus/mecanique.yaml", MECHANICS)
        bad.commit = bad.repo.commit("mechanics published")
        storage.of(self.db).canon.record_state(
            self.cfg.host, "ok", root=bad.root,
            source="home @ %s" % bad.commit[:12], commit=bad.commit, good=True,
            diagnostic="", canon="")
        report = act.retry_pending(cfg, self.db)
        self.assertEqual(report["attempted"], 1, report)
        self.assertEqual(report["designated"], 1, report)
        shown = act.reviewers(cfg, self.db, self.lot)
        self.assertEqual(shown["freeze"]["sha"], self.frozen)
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["r1", "r2"])
        before = len(self.mailbox())
        again = act.retry_pending(cfg, self.db)
        self.assertEqual(again["attempted"], 0, again)
        self.assertEqual(len(self.mailbox()), before)

    def test_the_retry_adds_the_missing_seat_beside_a_manual_one(self):
        # #3596 / #140401 : une note `unavailable` et un siège manuel claude ne
        # doivent pas faire sauter le lot. Quand le canon arrive, la reprise
        # ajoute codex seul, sans redemander claude.
        bad = _Canon(os.path.join(self.tmp, "canon-manuel"), mechanics=None)
        cfg = dataclasses.replace(self.cfg, canon=bad.root, canon_ref="HEAD")
        self.freeze(cfg=cfg)
        self.assertEqual(self.marks(), [])
        self.assertEqual(len(self.notes(act.UNAVAILABLE_PREFIX)), 1)
        # the freeze already moved the lot to qa: add the manual claude seat
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1",
                                actor="human:smichea")
        before = len(self.mailbox())
        bad.repo.write("processus/mecanique.yaml", MECHANICS)
        bad.commit = bad.repo.commit("mechanics published")
        storage.of(self.db).canon.record_state(
            self.cfg.host, "ok", root=bad.root,
            source="home @ %s" % bad.commit[:12], commit=bad.commit, good=True,
            diagnostic="", canon="")
        report = act.retry_pending(cfg, self.db)
        self.assertEqual(report["attempted"], 1, report)
        self.assertEqual(report["designated"], 1, report)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["r2"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        mark = pg.designation_of(self.marks()[-1])
        self.assertEqual(mark["state"], rd.DESIGNATED)
        self.assertEqual(mark["reviewers"], [{"name": "r2", "family": "codex"}])
        # désignation finale : la passe suivante saute le lot, sans courrier
        before = len(self.mailbox())
        again = act.retry_pending(cfg, self.db)
        self.assertEqual(again["attempted"], 0, again)
        self.assertEqual(len(self.mailbox()), before)

    def test_a_covered_manual_designation_is_journaled_and_not_retried(self):
        # #3596 : deux manuels couvrent les deux avis ; la reprise le constate
        # une fois, écrit le marqueur de couverture, puis saute le lot.
        bad = _Canon(os.path.join(self.tmp, "canon-couvert"), mechanics=None)
        cfg = dataclasses.replace(self.cfg, canon=bad.root, canon_ref="HEAD")
        self.freeze(cfg=cfg)
        self.assertEqual(len(self.notes(act.UNAVAILABLE_PREFIX)), 1)
        for name in ("r1", "r2"):
            work.designate_reviewer(self.cfg, self.db, self.lot, name,
                                    actor="human:smichea")
        bad.repo.write("processus/mecanique.yaml", MECHANICS)
        bad.commit = bad.repo.commit("mechanics published")
        storage.of(self.db).canon.record_state(
            self.cfg.host, "ok", root=bad.root,
            source="home @ %s" % bad.commit[:12], commit=bad.commit, good=True,
            diagnostic="", canon="")
        before = len(self.mailbox())
        report = act.retry_pending(cfg, self.db)
        self.assertEqual(report["attempted"], 1, report)
        self.assertEqual(report["manual"], 1, report)
        self.assertEqual(len(self.mailbox()), before)
        self.assertTrue(self.notes(act.MANUAL_PREFIX))
        again = act.retry_pending(cfg, self.db)
        self.assertEqual(again["attempted"], 0, again)

    # -- retry queue fairness (cx1 B1/P2 of #2561) -------------------------

    def _retry_candidate(self, index: int, *, reviewer: str = "") -> int:
        """An open lot with one frozen milestone and one unavailable note."""
        item = work.add(self.db, title="retry %d" % index, app="ameesh",
                        assignee="author")
        lot = int(item["id"])
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (lot,))
        row = self.db.query(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor)"
            " VALUES (%s, 'frozen', %s, 'author') RETURNING id",
            (lot, ("%040x" % lot)))[0]
        if reviewer:
            # #3596: a manual seat is final only when its coverage is journaled
            # (MANUAL_PREFIX); without that marker the retry must attempt it.
            self.db.execute(
                "UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s",
                (reviewer, lot))
            marker = "%s%d:%d: manual coverage witness" % (
                act.MANUAL_PREFIX, lot, int(row["id"]))
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, note, actor)"
                " VALUES (%s, 'qa', %s, 'ameesh')", (lot, marker))
        note = "%s%d:%d: retry witness" % (act.UNAVAILABLE_PREFIX, lot,
                                           int(row["id"]))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'qa', %s, 'ameesh')", (lot, note))
        return lot

    @staticmethod
    def _waiting_designation(calls: list):
        def fake(cfg, db, lot, *, freeze=None):
            calls.append(int(lot))
            return {"state": "waiting", "reviewers": [], "freeze": freeze,
                    "why": "no capacity"}
        return fake

    def test_a_resolved_lot_does_not_consume_the_retry_budget(self):
        # 21 unavailable freezes, the 20 oldest resolved but still in qa.
        # Counting them in `seen` before `_retry_resolved` spent the whole
        # budget on them and the 21st was never retried (cx1 B1/P2).
        resolved = [self._retry_candidate(i, reviewer="r1")
                    for i in range(1, 21)]
        pending = self._retry_candidate(21)
        self.assertEqual(len(resolved), 20)
        calls: list = []
        with mock.patch.object(act, "designate",
                               side_effect=self._waiting_designation(calls)):
            report = act.retry_pending(self.cfg, self.db, limit=20)
        self.assertEqual(calls, [pending], (calls, report))
        self.assertEqual(report["attempted"], 1, report)
        self.assertEqual(report["skipped"], 20, report)

    def test_a_starved_lot_is_reached_on_the_next_turn_of_the_cursor(self):
        # 21 unresolved freezes, 20 retried per pass: the 21st must not wait
        # forever behind the same 20 notes.
        lots = [self._retry_candidate(i) for i in range(1, 22)]
        calls: list = []
        with mock.patch.object(act, "designate",
                               side_effect=self._waiting_designation(calls)):
            first = act.retry_pending(self.cfg, self.db, limit=20)
            second = act.retry_pending(self.cfg, self.db, limit=20)
        self.assertEqual(calls[:20], lots[:20], (calls, first))
        self.assertEqual(calls[20:], [lots[20]], (calls, second))
        self.assertEqual(first["attempted"], 20, first)
        self.assertEqual(second["attempted"], 1, second)

    def test_the_retry_walks_past_the_scan_window(self):
        # RETRY_SCAN + 1 notes: only the oldest is unresolved, the 200 newer
        # ones are resolved. The old DESC scan read the 200 newest and never
        # reached the oldest; the cursor scan starts there.
        first = self._retry_candidate(1)
        for index in range(2, act.RETRY_SCAN + 2):
            self._retry_candidate(index, reviewer="r1")
        calls: list = []
        with mock.patch.object(act, "designate",
                               side_effect=self._waiting_designation(calls)):
            report = act.retry_pending(self.cfg, self.db, limit=20)
        self.assertEqual(calls, [first], (calls, report))
        self.assertEqual(report["attempted"], 1, report)

    def test_the_runner_retries_after_a_successful_canon_sync(self):
        # The runner's periodic pass calls the bounded retry and the #3328
        # reassignment; a failure there never fails the canon sync.
        from ameesh import runner as runner_mod

        fake = types.SimpleNamespace(cfg=self.cfg, mediated=False,
                                     _designation_retry_ready=True)
        fake_db = mock.MagicMock()
        report = {"attempted": 1, "designated": 1,
                  "summary": "1 attempted: 1 designated"}
        moved = {"examined": 1, "reassigned": 1,
                 "summary": "1 examined: 1 reassigned"}
        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=fake_db), \
                mock.patch.object(act, "retry_pending", return_value=report) as retry, \
                mock.patch.object(act, "reassign_unreachable",
                                  return_value=moved) as reassign, \
                mock.patch.object(runner_mod, "log_async") as log:
            runner_mod.Runner.retry_review_designations(fake)
        retry.assert_called_once_with(self.cfg, fake_db)
        reassign.assert_called_once_with(self.cfg, fake_db)
        fake_db.close.assert_called_once()
        self.assertTrue(any("review designation retry" in str(call)
                            for call in log.call_args_list))
        self.assertTrue(any("review designation reassign" in str(call)
                            for call in log.call_args_list))


class UnreachableDesignationTest(_ActivationFixture, PgTestCase):
    """#3328 : un hôte perdu n'est ni désigné, ni laissé en siège mort.

    Le constat : pc-smichea `lost`, agent-mail dit « claims closed », mais
    `is_available` ne lisait que le registre et désignait ses agents. Les
    témoins couvrent la désignation neuve (exclusion, repli de la même famille,
    attente si la famille n'a que cet agent), le retour de l'hôte, et la
    reprise d'une désignation déjà écrite par le passage du runner.
    """

    LOST = "pc-smichea"

    def _agent(self, name, harness):
        register_agent(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        (name,))

    def _lose_host(self, host=None):
        host = host or self.LOST
        self.db.execute(
            "INSERT INTO host_lifecycle(host, mode, actor) VALUES (%s, 'lost', 'test')"
            " ON CONFLICT (host) DO UPDATE SET mode = 'lost'", (host,))
        self.addCleanup(self.db.execute,
                        "DELETE FROM host_lifecycle WHERE host = %s", (host,))

    def _move_to_lost(self, name, host=None):
        self.db.execute("UPDATE agent_registry SET host = %s WHERE name = %s",
                        (host or self.LOST, name))

    def _designated(self, lot=None):
        shown = act.reviewers(self.cfg, self.db, lot or self.lot)
        return shown["designation"]

    def test_a_lost_host_is_excluded_and_the_family_falls_back(self):
        # r1 (claude) est sur l'hôte perdu ; r3, même famille, est sain :
        # la désignation prend r3, jamais r1.
        self._agent("r3", "claude")
        self._move_to_lost("r1")
        self._lose_host()
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r2", "r3"])
        self.assertNotIn("r1", [r["name"] for r in out["reviewers"]])
        self.assertEqual(sorted(m["recipient"] for m in self.mailbox()), ["r2", "r3"])

    def test_a_lost_host_alone_in_its_family_waits_and_alerts(self):
        # Aucun autre claude : la demande n'est pas déposée chez un mort ; la
        # famille codex disponible est pourvue et le second avis attend
        # (#3596, aucune marque ne porte r1).
        self._move_to_lost("r1")
        self._lose_host()
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r2"])
        self.assertEqual(out["missing"], 1)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot", "r2"])
        noted = pg.designation_of(self.marks()[0])
        self.assertEqual(noted["state"], rd.WAITING)
        self.assertEqual(noted["reviewers"], [{"name": "r2", "family": "codex"}])
        self.assertNotIn("r1", json.dumps(noted))

    def test_an_undrained_host_is_designated_on_the_next_pass(self):
        # Témoin (3) : l'hôte rouvert rend r1 candidat ; la marque d'attente du
        # même gel est rejouée et désigne r1 avec r2.
        self._move_to_lost("r1")
        self._lose_host()
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.db.execute("UPDATE host_lifecycle SET mode = 'available' WHERE host = %s",
                        (self.LOST,))
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual(sorted(r["name"] for r in again["reviewers"]), ["r1", "r2"])

    def test_an_existing_designation_is_reassigned_when_its_host_is_lost(self):
        # Une désignation écrite avant la perte est reprise au passage du
        # runner : r1 est remplacé par r3 (même famille), la colonne et le
        # journal suivent, et une seconde passe ne redésigne rien.
        self._agent("r3", "claude")
        _, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r1", "r2"])
        self._move_to_lost("r1")
        self._lose_host()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 1, report)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        self.assertEqual(noted["state"], rd.DESIGNATED, noted)
        self.assertEqual(sorted(r["name"] for r in noted["reviewers"]), ["r2", "r3"])
        column = self.db.query(
            "SELECT reviewers FROM work_items WHERE id = %s", (self.lot,))[0]["reviewers"]
        self.assertEqual(sorted(column), ["r2", "r3"])
        self.assertTrue(self.notes(act.REASSIGN_PREFIX))
        self.assertIn("r1 (host %s: claims closed)" % self.LOST,
                      self.notes(act.REASSIGN_PREFIX)[-1])
        mails = len(self.mailbox())
        again = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(again["reassigned"], 0, again)
        self.assertEqual(again["examined"], 0, again)
        self.assertEqual(len(self.mailbox()), mails)

    def test_a_seat_the_pilot_already_removed_is_not_moved_again(self):
        # Le pilote a retiré le relecteur mort de la colonne avant la passe
        # (#3311/#3312) : la marque reste son histoire, mais la reprise ne
        # rouvre pas le siège et n'envoie rien.
        self._agent("r3", "claude")
        _, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r1", "r2"])
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s",
            ("r2", self.lot))
        self._move_to_lost("r1")
        self._lose_host()
        mails = len(self.mailbox())
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 0, report)
        self.assertEqual(len(self.mailbox()), mails)
        self.assertFalse(self.notes(act.REASSIGN_PREFIX))

    def test_a_reassignment_without_a_replacement_waits_once(self):
        # Seule la famille claude est disponible et son agent est mort : la
        # reprise alerte le pilote, une seule fois, au lieu de réécrire le
        # même siège mort à chaque passe. #3367 : la désignation reste telle
        # quelle — une marque d'attente retirait aussi r2, vivant, de la
        # colonne ; r1 garde sa demande et relira au retour de son hôte.
        _, out = self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["waiting"], 1, report)
        noted = self._designated()
        self.assertEqual(noted["state"], rd.DESIGNATED, noted)
        self.assertEqual(sorted(r["name"] for r in noted["reviewers"]), ["r1", "r2"])
        self.assertEqual(sorted(self._column()), ["r1", "r2"])
        self.assertIn("pilot", [m["recipient"] for m in self.mailbox()])
        self.assertIn("designation unchanged", self.notes(act.REASSIGN_PREFIX)[-1])
        mails = len(self.mailbox())
        again = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(again["examined"], 0, again)
        self.assertEqual(len(self.mailbox()), mails)

    # -- #3367 : seuls les sièges morts bougent -----------------------------
    def _column(self):
        return list(self.db.query(
            "SELECT reviewers FROM work_items WHERE id = %s",
            (self.lot,))[0]["reviewers"] or [])

    def _verdict(self, actor, sha=None, verdict="ok"):
        return work.milestone(self.db, self.lot, "verdict", sha=sha or self.frozen,
                              actor=actor, verdict=verdict, repo=self.repo.root,
                              after_commit=[])

    def _verdict_receipt(self, mark, actor, harness):
        """The exact #1465 receipt the sensitive gate requires (#3369).

        The tests run outside a managed turn, where `work.milestone` writes the
        fact without a proof; a witness of the sensitive class adds it here.
        """
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)",
            ("milestone:" + str(int(mark["id"])), self.frozen, actor,
             "s-" + harness, harness, self.lot))

    def _requests(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and m["kind"] == rd.REQUEST_KIND]

    def test_a_lost_seat_whose_verdict_is_acquired_on_the_head_is_kept(self):
        # Témoin du lot : r1 a rendu son verdict sur la tête gelée exacte (même
        # abrégé), puis son hôte se ferme. Le siège n'est pas perdu : aucune
        # redésignation, aucun courrier, la marque et la colonne sont gardées.
        self._agent("r3", "claude")
        _, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r1", "r2"])
        self._verdict_receipt(self._verdict("r1", sha=self.frozen[:12]), "r1", "claude")
        self._verdict_receipt(self._verdict("r2"), "r2", "codex")
        self._move_to_lost("r1")
        self._lose_host()
        marks, mails = len(self.marks()), len(self.mailbox())
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 0, report)
        self.assertEqual(report["reassigned"], 0, report)
        self.assertEqual(len(self.marks()), marks)
        self.assertEqual(len(self.mailbox()), mails)
        self.assertEqual(sorted(self._column()), ["r1", "r2"])
        self.assertFalse(self.notes(act.REASSIGN_PREFIX))

    def test_a_blocking_verdict_on_the_head_is_acquired_too(self):
        # Un `blocked` sur la tête gelée est aussi un avis rendu : un
        # remplaçant ne lèverait pas le blocage, il faut une nouvelle tête.
        self._agent("r3", "claude")
        self.freeze()
        self._verdict_receipt(self._verdict("r1", verdict="blocked"), "r1", "claude")
        self._move_to_lost("r1")
        self._lose_host()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 0, report)
        self.assertEqual(sorted(self._column()), ["r1", "r2"])

    def test_an_unproven_verdict_does_not_keep_a_sensitive_seat(self):
        # #3369 : la porte sensible écarte un verdict sans son reçu exact
        # (#1465). Garder le siège sur ce verdict laisserait un siège mort que
        # la porte ne compterait jamais : le remplaçant doit être désigné.
        self._agent("r3", "claude")
        self.freeze()
        klass = lot_class.of_lot(self.db, self.lot, cfg=self.cfg,
                                 head=self.frozen)
        self.assertEqual(klass["class"], lot_class.SENSITIVE, klass)
        self._verdict("r1")  # pas de reçu : un avis que la porte écarte
        self._move_to_lost("r1")
        self._lose_host()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        self.assertEqual(sorted(self._column()), ["r2", "r3"])
        self.assertIn("replaced by r3", self.notes(act.REASSIGN_PREFIX)[-1])

    def test_two_concurrent_waiting_passes_alert_once(self):
        # #3369 : le chemin d'attente n'écrit aucune marque de désignation,
        # donc sa note de journal est sa seule déduplication. Deux hôtes qui
        # passent en même temps doivent produire une seule alerte pilote et
        # une seule note : la note est écrite sous le verrou de la ligne.
        self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        barrier = threading.Barrier(2)
        original = act._selection

        def together(inputs, port, **kwargs):
            barrier.wait(timeout=20)
            return original(inputs, port, **kwargs)

        def run():
            db = self.connect()
            try:
                return act.reassign_unreachable(self.cfg, db)
            finally:
                db.close()

        with mock.patch.object(act, "_selection", side_effect=together):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                tasks = [pool.submit(run) for _ in range(2)]
                reports = [task.result(timeout=40) for task in tasks]
        self.assertEqual(sum(int(r["waiting"]) for r in reports), 1, reports)
        self.assertEqual(len([m for m in self.mailbox() if m["recipient"] == "pilot"]), 1)
        self.assertEqual(len(self.notes(act.REASSIGN_PREFIX)), 1)

    def test_a_verdict_on_an_earlier_head_does_not_keep_the_seat(self):
        # Le vrai trou (#3340) : r1 a relu une autre tête, pas celle du gel.
        # Son siège est remplacé, r2 garde le sien.
        self._agent("r3", "claude")
        self.repo.write("docs/earlier.md", "earlier head\n")
        earlier = self.repo.commit("earlier head")
        self.freeze()
        self._verdict("r1", sha=earlier)
        self._move_to_lost("r1")
        self._lose_host()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        self.assertEqual(sorted(self._column()), ["r2", "r3"])

    def test_a_live_seat_is_never_replaced(self):
        # Le cas #3341 : r2 (codex) est vivant, r4 (codex) est moins chargé.
        # L'ancien rejeu complet (force=True) reprenait r4 à la place de r2 ;
        # seul le siège mort (r1) est remplacé, et seul r3 reçoit un courrier.
        self._agent("r3", "claude")
        self._agent("r4", "codex")
        _, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r1", "r2"])
        self._move_to_lost("r1")
        self._lose_host()
        before = len(self._requests("r2"))
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        self.assertEqual([r["name"] for r in noted["reviewers"]], ["r2", "r3"], noted)
        self.assertEqual(sorted(self._column()), ["r2", "r3"])
        self.assertEqual(len(self._requests("r2")), before)
        self.assertEqual(len(self._requests("r3")), 1)
        self.assertFalse(self._requests("r4"))
        note = self.notes(act.REASSIGN_PREFIX)[-1]
        self.assertIn("replaced by r3", note)
        self.assertIn("kept: r2", note)

    def test_kept_seats_that_cover_the_opinions_drop_the_dead_one(self):
        # Trois sièges dont un mort : les deux vivants couvrent les deux
        # familles requises. Le mort est retiré, personne n'est désigné ni
        # prévenu.
        self._agent("r5", "claude")
        row, _out = self.freeze()
        note = pg._mark_note(
            self.lot, int(row["id"]), state=rd.DESIGNATED, required=2,
            reviewers=[{"name": "r1", "family": "claude"},
                       {"name": "r2", "family": "codex"},
                       {"name": "r5", "family": "claude"}],
            missing=0, why="test: three seats", authors=[], policy_ref="",
            sha=self.frozen)
        storage.of(self.db).work.note(self.lot, "build", note, "test")
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r1', 'r2', 'r5'] WHERE id = %s",
            (self.lot,))
        self._move_to_lost("r1")
        self._lose_host()
        mails = len(self.mailbox())
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        self.assertEqual([r["name"] for r in noted["reviewers"]], ["r2", "r5"], noted)
        self.assertEqual(sorted(self._column()), ["r2", "r5"])
        self.assertEqual(len(self.mailbox()), mails)
        self.assertIn(act.COVERED_WHY, self.notes(act.REASSIGN_PREFIX)[-1])

    def test_an_unreadable_head_never_clears_the_seats(self):
        # #3341 à 15:56Z : le rejeu n'a trouvé aucun dépôt tenant la tête
        # gelée, et le chemin « indisponible » a vidé la colonne (cx1, dev7).
        # La reprise n'écrit plus rien quand elle ne peut pas conclure.
        # #3685 : l'échec transitoire ne consomme plus la marque — il est
        # journalisé sous son propre préfixe et retenté à la passe suivante.
        self._agent("r3", "claude")
        self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        marks, mails = len(self.marks()), len(self.mailbox())
        unreadable = act.Unavailable(
            "no repository holds the frozen head: cannot read the frozen diff")
        with mock.patch.object(act, "prepare", return_value=unreadable):
            report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 0, report)
        self.assertEqual(report["retried"], 1, report)
        self.assertEqual(sorted(self._column()), ["r1", "r2"])
        self.assertEqual(len(self.marks()), marks)
        self.assertEqual(len(self.mailbox()), mails)
        self.assertFalse(self.notes(act.UNAVAILABLE_PREFIX))
        self.assertFalse(self.notes(act.REASSIGN_PREFIX))
        retries = self.notes(act.REASSIGN_RETRY_PREFIX)
        self.assertEqual(len(retries), 1, retries)
        self.assertIn("attempt 1/%d" % act.REASSIGN_RETRY_MAX, retries[0])
        self.assertIn("no repository holds", retries[0])

    def test_a_transient_unreadable_head_is_retried_on_the_next_pass(self):
        # #3685, témoin du lot : passe 1 sans tête lisible (dépôt sans fetch),
        # passe 2 avec la tête revenue → le siège mort est remplacé. Sans le
        # correctif, la marque du 1er échec faisait sauter la passe 2.
        self._agent("r3", "claude")
        _, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]),
                         ["r1", "r2"])
        self._move_to_lost("r1")
        self._lose_host()
        unreadable = act.Unavailable(
            "no repository holds the frozen head: cannot read the frozen diff")
        with mock.patch.object(act, "prepare", return_value=unreadable):
            first = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(first["examined"], 1, first)
        self.assertEqual(first["retried"], 1, first)
        self.assertEqual(first["reassigned"], 0, first)
        self.assertEqual(sorted(self._column()), ["r1", "r2"])
        self.assertFalse(self.notes(act.REASSIGN_PREFIX))
        self.assertTrue(self.notes(act.REASSIGN_RETRY_PREFIX))
        # the head is readable again: the next pass moves the dead seat
        second = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(second["reassigned"], 1, second)
        noted = self._designated()
        self.assertEqual([r["name"] for r in noted["reviewers"]], ["r2", "r3"],
                         noted)
        self.assertEqual(sorted(self._column()), ["r2", "r3"])
        self.assertEqual(len(self.notes(act.REASSIGN_PREFIX)), 1,
                         self.notes(act.REASSIGN_PREFIX))

    def test_a_transient_failure_is_capped_and_left_to_the_pilot(self):
        # #3685 : un échec qui dure ne boucle pas. Après le plafond, la marque
        # est consommée par une note explicite qui laisse le siège au pilote ;
        # les passes suivantes ne réessaient plus (la reprise manuelle
        # `work reviewers --apply` reste disponible).
        self._agent("r3", "claude")
        self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        unreadable = act.Unavailable("no repository holds the frozen head")
        with mock.patch.object(act, "prepare", return_value=unreadable):
            reports = [act.reassign_unreachable(self.cfg, self.db)
                       for _ in range(act.REASSIGN_RETRY_MAX)]
            self.assertTrue(all(r["retried"] == 1 for r in reports), reports)
            self.assertFalse(self.notes(act.REASSIGN_PREFIX))
            self.assertEqual(len(self.notes(act.REASSIGN_RETRY_PREFIX)),
                             act.REASSIGN_RETRY_MAX)
            final = act.reassign_unreachable(self.cfg, self.db)
            again = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(final["exhausted"], 1, final)
        self.assertEqual(final["retried"], 0, final)
        notes = self.notes(act.REASSIGN_PREFIX)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("retry cap %d reached" % act.REASSIGN_RETRY_MAX, notes[0])
        self.assertIn("left to the pilot", notes[0])
        self.assertEqual(again["examined"], 0, again)
        self.assertEqual(len(self.notes(act.REASSIGN_PREFIX)), 1,
                         self.notes(act.REASSIGN_PREFIX))
        self.assertEqual(sorted(self._column()), ["r1", "r2"])

    def test_the_recovery_does_not_echo_a_seat_the_pilot_removed(self):
        # #3685, second piège : le pilote a retiré r1 de la colonne, la marque
        # le nomme encore. `--apply` ne doit pas afficher « designated: r1 » :
        # la désignation vivante est complétée par une autre famille.
        self._agent("r3", "claude")
        self.freeze()
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r2'] WHERE id = %s",
            (self.lot,))
        before_mail, before_marks = len(self.mailbox()), len(self.marks())
        sight = act.reviewers(self.cfg, self.db, self.lot, apply=True)
        applied = sight["applied"]
        self.assertEqual(applied["state"], rd.DESIGNATED, applied)
        names = [r["name"] for r in applied["reviewers"]]
        self.assertNotIn("r1", names, applied)
        self.assertEqual(sorted(names), ["r2", "r3"])
        self.assertIn("removed by the pilot", sight["historical"])
        self.assertIn("r1", sight["historical"])
        self.assertEqual(sorted(self._column()), ["r2", "r3"])
        self.assertEqual(len(self.marks()), before_marks + 1)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before_mail:]],
                         ["r3"])

    def test_the_recovery_moves_a_seat_whose_host_is_lost(self):
        # #3685 : la marque désigne r1, dont l'hôte est perdu. `--apply` doit
        # déplacer ce siège (mêmes preuves que la passe du runner), conserver
        # r2 vivant, et dire que la marque enregistrée est historique.
        self._agent("r3", "claude")
        self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        before_mail = len(self.mailbox())
        sight = act.reviewers(self.cfg, self.db, self.lot, apply=True)
        applied = sight["applied"]
        self.assertEqual(applied["state"], rd.DESIGNATED, applied)
        self.assertEqual(sorted(r["name"] for r in applied["reviewers"]),
                         ["r2", "r3"])
        self.assertIn("unreachable reviewer(s)", sight["historical"])
        self.assertIn("r1", sight["historical"])
        self.assertEqual(sorted(self._column()), ["r2", "r3"])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before_mail:]],
                         ["r3"])

    def test_a_removed_seat_stays_out_when_another_seat_is_dead(self):
        # #3685 B1 (cx3) : retrait + siège mort dans la même marque. r1
        # (claude) a été retiré par le pilote, r2 (codex) est mort. Sans
        # l'exclusion, la branche `reseat` reprend r1 — le claude le moins
        # chargé — et annule le retrait ; avec elle, r3 (claude, déjà chargé)
        # et r4 (codex, libre) sont les vrais remplaçants.
        self._agent("r3", "claude")
        self._agent("r4", "codex")
        self.db.execute(
            "INSERT INTO work_items (title, app, state, assignee)"
            " VALUES ('charge r3', 'ameesh', 'qa', 'r3')")
        self.freeze()
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r2'] WHERE id = %s",
            (self.lot,))
        self._move_to_lost("r2")
        self._lose_host()
        before_mail = len(self.mailbox())
        sight = act.reviewers(self.cfg, self.db, self.lot, apply=True)
        applied = sight["applied"]
        self.assertEqual(applied["state"], rd.DESIGNATED, applied)
        names = sorted(r["name"] for r in applied["reviewers"])
        self.assertNotIn("r1", names, applied)
        self.assertEqual(names, ["r3", "r4"], applied)
        self.assertIn("unreachable reviewer(s)", sight["historical"])
        self.assertIn("r2", sight["historical"])
        self.assertIn("removed by the pilot", sight["historical"])
        self.assertIn("r1", sight["historical"])
        self.assertEqual(sorted(self._column()), ["r3", "r4"])
        mails = [m["recipient"] for m in self.mailbox()[before_mail:]]
        self.assertNotIn("r1", mails)
        self.assertEqual(sorted(mails), ["r3", "r4"])

    def test_the_recovery_is_unavailable_when_the_verdict_read_fails(self):
        # #3685 B2 (cx3) : r2 est mort, mais la lecture des verdicts de la
        # tête échoue (statement timeout). La reprise ne doit pas afficher la
        # marque [r1, r2] comme vivante : elle rend unavailable, sans mutation
        # ni siège affirmé.
        self.freeze()
        self._move_to_lost("r2")
        self._lose_host()
        marks, mails = len(self.marks()), len(self.mailbox())
        column = sorted(self._column())
        failure = db_mod.DbError(
            "canceling statement due to statement timeout",
            sqlstate=db_mod.QUERY_CANCELED,
            sql="SELECT kind, sha, actor FROM work_item_milestones",
            elapsed_ms=30012.0)
        with mock.patch.object(act, "_head_voters", side_effect=failure):
            sight = act.reviewers(self.cfg, self.db, self.lot, apply=True)
        applied = sight["applied"]
        self.assertEqual(applied["state"], "unavailable", applied)
        self.assertEqual(list(applied["reviewers"]), [])
        self.assertIn("reviewer evidence unreadable", applied["why"])
        self.assertIn("statement timeout", applied["why"])
        self.assertEqual(len(self.marks()), marks)
        self.assertEqual(len(self.mailbox()), mails)
        self.assertEqual(sorted(self._column()), column)

    def test_the_recovery_of_a_dead_seat_without_replacement_says_waiting(self):
        # #3685 : sans famille de remplacement, `--apply` ne réaffiche pas le
        # siège mort comme désigné : il dit l'attente, et la marque enregistrée
        # est annoncée historique.
        self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        sight = act.reviewers(self.cfg, self.db, self.lot, apply=True)
        self.assertEqual(sight["applied"]["state"], rd.WAITING, sight)
        self.assertIn("unreachable reviewer(s)", sight["historical"])
        self.assertEqual(sorted(self._column()), ["r1", "r2"])

    def test_an_unreadable_input_names_the_read_and_the_query(self):
        # #3631 : 639 passes perdues en 24 h ne disaient que « inputs
        # unreadable ». La passe nomme maintenant la lecture fautive et, sur
        # un statement_timeout, l'instruction que le pilote a mesurée.
        self.freeze()
        failure = db_mod.DbError(
            "canceling statement due to statement timeout",
            sqlstate=db_mod.QUERY_CANCELED,
            sql="SELECT name, aliases, retired_ts FROM agent_registry",
            elapsed_ms=30012.0)
        with mock.patch.object(pg_registry.Agents, "reachability",
                               side_effect=failure):
            report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["summary"],
                         "no reassignment: inputs unreadable")
        self.assertEqual(len(report["errors"]), 1, report)
        self.assertIn("agents.reachability", report["errors"][0])
        self.assertIn("statement timeout", report["errors"][0])
        self.assertIn("after 30.0s", report["errors"][0])
        self.assertIn("SELECT name, aliases", report["errors"][0])

    def test_a_pilot_removal_during_the_pass_is_not_written_back(self):
        # Relecture de impl-02 (#3367) : le pilote retire r2 pendant que la
        # passe lit le dépôt (`prepare`) ; `work reviewer --remove` n'écrit
        # aucune marque, `mark_id` est inchangé. Sous le verrou, la colonne
        # ne nomme plus un siège examiné : rien n'est écrit, r2 ne revient pas.
        self._agent("r3", "claude")
        self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        freeze = act._latest_freeze(self.db, self.lot)
        mark = act._effective_mark(self.db, self.lot, int(freeze["id"]))
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['r1'] WHERE id = %s",
                        (self.lot,))
        marks, mails = len(self.marks()), len(self.mailbox())
        out = act.reseat(self.cfg, self.db, self.lot, freeze=freeze,
                         kept=[{"name": "r2", "family": "codex"}],
                         dead=[("r1", "host pc-smichea: claims closed")],
                         mark_id=int(mark[0]))
        self.assertEqual(out["state"], "stale", out)
        self.assertNotIn("r2", self._column(), out)
        self.assertEqual(len(self.marks()), marks)
        self.assertEqual(len(self.mailbox()), mails)

    def test_a_mark_moved_by_another_pass_writes_nothing(self):
        # Deux hôtes passent en même temps : celui qui arrive second lit une
        # marque qui n'est plus la marque effective, et n'écrit rien.
        self._agent("r3", "claude")
        row, _out = self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        freeze = act._latest_freeze(self.db, self.lot)
        stale_mark = act._effective_mark(self.db, self.lot, int(freeze["id"]))
        storage.of(self.db).work.note(self.lot, "build", self.marks()[-1], "test")
        marks, mails = len(self.marks()), len(self.mailbox())
        out = act.reseat(self.cfg, self.db, self.lot, freeze=freeze,
                         kept=[{"name": "r2", "family": "codex"}],
                         dead=[("r1", "host lost")], mark_id=int(stale_mark[0]))
        self.assertEqual(out["state"], "stale", out)
        self.assertEqual(len(self.marks()), marks)
        self.assertEqual(len(self.mailbox()), mails)
        self.assertEqual(sorted(self._column()), ["r1", "r2"])

    def test_two_hundred_older_healthy_lots_do_not_hide_the_lost_seat(self):
        # Relecture #132625 (1) : la fenêtre bornée lisait les 200 lots
        # ouverts les plus anciens, sains, et ne voyait jamais le 201e dont
        # le siège est perdu. Le filtre des injoignables passe avant la
        # fenêtre : les lots sains ne sont plus des candidats.
        self._agent("r3", "claude")
        _, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r1", "r2"])
        for index in range(act.REASSIGN_SCAN):
            self.db.execute(
                "INSERT INTO work_items (title, app, state, assignee, reviewers,"
                " updated_at) VALUES (%s, 'ameesh', 'qa', 'author',"
                " ARRAY['r2'], now() - interval '2 days')",
                ("healthy %d" % index,))
        self.db.execute("UPDATE work_items SET updated_at = now() WHERE id = %s",
                        (self.lot,))
        self._move_to_lost("r1")
        self._lose_host()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 1, report)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        self.assertEqual(sorted(r["name"] for r in noted["reviewers"]), ["r2", "r3"])

    def test_a_missing_reviewer_beside_a_live_one_is_reassigned(self):
        # Relecture #132625 (2) : la sonde joignait la colonne au registre.
        # Avec un nom vivant et un nom absent, la jointure ne rendait que le
        # vivant et l'absence restait invisible. Chaque nom est désormais
        # déroulé : `ghost` est vu à côté de `r2`, qui est sain.
        self._agent("r3", "claude")
        row, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r1", "r2"])
        freeze_id = int(row["id"])
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r2', 'ghost'] WHERE id = %s",
            (self.lot,))
        note = pg._mark_note(
            self.lot, freeze_id, state=rd.DESIGNATED, required=2,
            reviewers=[{"name": "r2", "family": "codex"},
                       {"name": "ghost", "family": "claude"}],
            missing=0, why="test: missing beside live", authors=[],
            policy_ref="", sha=self.frozen)
        storage.of(self.db).work.note(self.lot, "build", note, "test")
        # `r1` revient au registre comme candidat sain : on le perd aussi pour
        # que le remplacement soit `r3`, et non le nom absent retiré de la
        # colonne.
        self._move_to_lost("r1")
        self._lose_host()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 1, report)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        names = [r["name"] for r in noted["reviewers"]]
        self.assertNotIn("ghost", names, noted)
        self.assertEqual(sorted(names), ["r2", "r3"], noted)
        self.assertIn("ghost (absent from the registry)",
                      self.notes(act.REASSIGN_PREFIX)[-1])

    def test_a_window_of_non_actionable_seats_wraps_to_the_lost_one(self):
        # Relecture #132625 (1) : un curseur fait avancer une passe dont les
        # candidats ne demandent rien (ici deux sièges sans gel), puis se
        # replie au bout. Sans lui, la même fenêtre pouvait être relue à
        # chaque passe et le siège perdu, plus ancien, ne jamais revenir.
        self._agent("r3", "claude")
        self.freeze()
        self._move_to_lost("r1")
        self._lose_host()
        for index in range(2):
            self.db.execute(
                "INSERT INTO work_items (title, app, state, assignee, reviewers)"
                " VALUES (%s, 'ameesh', 'qa', 'author', ARRAY['r1'])",
                ("stale %d" % index,))
        act._write_cursor(self.cfg, act.REASSIGN_CURSOR, self.lot)
        with mock.patch.object(act, "REASSIGN_SCAN", 2):
            first = act.reassign_unreachable(self.cfg, self.db)
            self.assertEqual(first["reassigned"], 0, first)
            self.assertEqual(act._read_cursor(self.cfg, act.REASSIGN_CURSOR),
                             self.lot + 2, first)
            second = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(second["examined"], 1, second)
        self.assertEqual(second["reassigned"], 1, second)
        self.assertEqual(sorted(r["name"] for r in self._designated()["reviewers"]),
                         ["r2", "r3"])


class PendingLoadTest(_ActivationFixture, PgTestCase):
    """#3004: the load of a reviewer counts the reviews they still owe.

    The witness of the lot: a reviewer with no assigned lot but five pending
    reviews looked free, so every freeze of their family picked them again.
    The charge is read in one query, a verdict on the frozen head discharges
    it, a closed lot no longer counts, and an unreadable read is zero.
    """

    def pending_lot(self, reviewer: str) -> tuple[int, str]:
        """An open lot whose last freeze designates `reviewer`, no verdict yet.

        The freeze hook writes the journal mark and the `reviewers` column in
        the same transaction; the benchmark writes the same state directly.
        #2989 : a freeze must name a head resolvable in the author's
        repository, so each pending review gets a real commit in the fixture
        repository (a synthetic SHA no longer passes the gate).
        """
        item = work.add(self.db, title="pending %s" % reviewer, app="ameesh",
                        assignee="author")
        lot = int(item["id"])
        work.move(self.db, lot, "build")
        work.move(self.db, lot, "qa")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, lot))
        self.repo.write("docs/pending-%d.md" % lot, "pending %d\n" % lot)
        sha = self.repo.commit("pending %d" % lot)
        row = work.milestone(self.db, lot, "frozen", sha=sha, actor="author",
                             repo=self.repo.root,
                             on_frozen=lambda tx, milestone, threads: None,
                             after_commit=[])
        note = pg._mark_note(lot, int(row["id"]), state=rd.DESIGNATED,
                             required=1,
                             reviewers=[{"name": reviewer, "family": "claude"}],
                             missing=0, why="test: pending review",
                             authors=[], policy_ref="", sha=sha)
        storage.of(self.db).work.note(lot, "qa", note, "test")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s",
                        (reviewer, lot))
        return lot, sha

    def test_pending_designations_are_the_reviewer_load(self):
        pairs = [self.pending_lot("r1") for _ in range(5)]
        lots = [lot for lot, _ in pairs]
        shas = [sha for _, sha in pairs]
        loads = act._loads(self.db)
        # témoin (a), first half: five pending reviews, no assigned lot
        self.assertEqual(loads.get("r1"), 5, loads)
        self.assertNotIn("r2", loads)
        # témoin (b): a verdict naming the frozen head discharges its review,
        # even abbreviated (the same `_same_freeze` rule as the designation)
        work.milestone(self.db, lots[0], "verdict", sha=shas[0], actor="r1",
                       verdict="ok", repo=self.repo.root, after_commit=[])
        self.assertEqual(act._loads(self.db).get("r1"), 4)
        work.milestone(self.db, lots[1], "verdict", sha=shas[1][:12], actor="r1",
                       verdict="blocked", repo=self.repo.root, after_commit=[])
        self.assertEqual(act._loads(self.db).get("r1"), 3)
        # a verdict on another head, or from another agent, discharges nothing
        self.repo.write("docs/other-head.md", "other head\n")
        other = self.repo.commit("other head")
        work.milestone(self.db, lots[2], "verdict", sha=other, actor="r1",
                       verdict="ok", repo=self.repo.root, after_commit=[])
        work.milestone(self.db, lots[3], "verdict", sha=shas[3], actor="r2",
                       verdict="ok", repo=self.repo.root, after_commit=[])
        self.assertEqual(act._loads(self.db).get("r1"), 3)
        # témoin (c): a closed lot is no longer a charge
        work.close(self.db, lots[4], abandoned=True, actor="pilot")
        self.assertEqual(act._loads(self.db).get("r1"), 2)

    def test_only_the_designation_of_the_last_freeze_counts(self):
        # A new freeze is another head: the designation of the previous one is
        # not a pending review until the new head is designated. The journal
        # mark carries its freeze, the `reviewers` column does not (it is
        # synced after the selection), so the mark is the source.
        lot, _ = self.pending_lot("r1")
        self.assertEqual(act._loads(self.db).get("r1"), 1)
        self.repo.write("docs/new-head.md", "new head\n")
        sha = self.repo.commit("new head")
        row = work.milestone(self.db, lot, "frozen", sha=sha, actor="author",
                             repo=self.repo.root,
                             on_frozen=lambda tx, milestone, threads: None,
                             after_commit=[])
        self.assertNotIn("r1", act._loads(self.db))
        note = pg._mark_note(lot, int(row["id"]), state=rd.DESIGNATED, required=1,
                             reviewers=[{"name": "r1", "family": "claude"}],
                             missing=0, why="test: new head", authors=[],
                             policy_ref="", sha=sha)
        storage.of(self.db).work.note(lot, "qa", note, "test")
        self.assertEqual(act._loads(self.db).get("r1"), 1)

    def test_an_unreadable_load_is_zero_without_an_exception(self):
        # témoin (d): the read fails, the selection keeps its other criteria
        with mock.patch.object(self.db, "query", side_effect=RuntimeError("boom")):
            self.assertEqual(act._loads(self.db), {})

    def test_the_freeze_prefers_the_reviewer_with_the_smaller_real_queue(self):
        # témoin (a), end to end: r1 has five pending reviews and no assigned
        # lot, r1b has two assigned lots and no pending review. Same family
        # (claude), no plan pace reading: r1b must be designated, and the
        # journal mark names the retained load.
        for _ in range(5):
            self.pending_lot("r1")
        register_agent(self.db, "r1b", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = 'r1b'")
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = 'r2'")
        for index in range(2):
            extra = work.add(self.db, title="assigned %d" % index, app="ameesh",
                             assignee="r1b")
            work.move(self.db, extra["id"], "build")
        # a light lot (one opinion due) whose only eligible family is claude
        self.repo.write("docs/guide.md", "base\n")
        self.base = self.repo.commit("docs base")
        self.repo.write("docs/guide.md", "lot\n")
        self.frozen = self.repo.commit("docs lot")
        item = work.add(self.db, title="light load witness", app="ameesh",
                        assignee="author")
        lot = int(item["id"])
        work.move(self.db, lot, "build")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, lot))
        self.receipts()
        text = MECHANICS.replace("  distinct_families: true",
                                 "  distinct_families: true\n  light_reviews: 1")
        self.canon = _Canon(os.path.join(self.tmp, "canon-light-load"), mechanics=text)
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root, canon_ref="HEAD")
        outcome: dict = {}
        threads: list = []
        work.milestone(self.db, lot, "frozen", sha=self.frozen, actor="author",
                       on_frozen=act.hook(self.cfg, outcome), after_commit=threads)
        for record in threads:
            record(self.db.cfg, self.db)
        self.assertEqual(outcome["state"], rd.DESIGNATED, outcome)
        self.assertEqual([r["name"] for r in outcome["reviewers"]], ["r1b"])
        self.assertEqual([r["family"] for r in outcome["reviewers"]], ["claude"])
        self.assertIn("load: r1b 2", outcome["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1b"])
        mark = act._marks(self.db, lot)[-1][2]
        self.assertEqual(mark["state"], rd.DESIGNATED, mark)
        self.assertIn("load: r1b 2", mark["why"])


class UnreadableMarkTest(_ActivationFixture, PgTestCase):
    """#3047: an unreadable designation mark can never cancel a freeze.

    A note with the mark prefix and invalid JSON used to reach a `::jsonb`
    cast inside the freeze transaction: PostgreSQL aborted the transaction and
    every freeze of the mesh failed while the lot stayed open. The mark is now
    read as text and parsed in Python; an unreadable one charges nobody and is
    journaled with its lot and note id, and the freeze continues.
    """

    def head(self, label: str) -> str:
        """A real commit for a benchmark lot: #2989 resolves every frozen head."""
        self.repo.git("commit", "--allow-empty", "-qm", "pending %s" % label)
        return self.repo.git("rev-parse", "HEAD")

    def pending_lot(self, reviewer: str, label: str) -> int:
        """An open lot whose last freeze designates `reviewer`, no verdict yet.

        The same direct state as #3004's `PendingLoadTest`: the freeze hook
        writes the journal mark and the `reviewers` column in one transaction,
        the benchmark writes them without the hook. The head is a real commit:
        since #2989 a freeze refuses a SHA its repository cannot resolve.
        """
        sha = self.head(label)
        item = work.add(self.db, title="pending %s" % label, app="ameesh",
                        assignee="author")
        lot = int(item["id"])
        work.move(self.db, lot, "build")
        work.move(self.db, lot, "qa")
        # #2989: the head must not be an ancestor of the target; the fixture's
        # base commit is the target of the benchmark lot, its pending head is
        # a real commit on top of it.
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, lot))
        row = work.milestone(self.db, lot, "frozen", sha=sha, actor="author",
                             repo=self.repo.root,
                             on_frozen=lambda tx, milestone, threads: None,
                             after_commit=[])
        note = pg._mark_note(lot, int(row["id"]), state=rd.DESIGNATED,
                             required=1,
                             reviewers=[{"name": reviewer, "family": "claude"}],
                             missing=0, why="test: pending review",
                             authors=[], policy_ref="", sha=sha)
        storage.of(self.db).work.note(lot, "qa", note, "test")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s",
                        (reviewer, lot))
        return lot

    def latest_freeze(self, lot: int) -> int:
        return int(self.db.query(
            "SELECT id FROM work_item_milestones WHERE work_item_id = %s"
            " AND kind = 'frozen' ORDER BY at DESC, id DESC LIMIT 1",
            (int(lot),))[0]["id"])

    def frozen_sha(self, lot: int) -> str:
        return str(self.db.query(
            "SELECT sha FROM work_item_milestones WHERE work_item_id = %s"
            " AND kind = 'frozen' ORDER BY at DESC, id DESC LIMIT 1",
            (int(lot),))[0]["sha"])

    def unreadable_mark(self, lot: int, freeze_id: int) -> int:
        """Write a mark with a valid prefix and invalid JSON, return its id."""
        text = "review-designation:%d:%d: {not json" % (int(lot), int(freeze_id))
        storage.of(self.db).work.note(int(lot), "qa", text, "test")
        return int(self.db.query(
            "SELECT max(id) AS id FROM work_item_events WHERE work_item_id = %s",
            (int(lot),))[0]["id"])

    def notes(self) -> list:
        return [str(row["note"]) for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s",
            (self.lot,))]

    def test_an_unreadable_mark_on_another_lot_does_not_block_the_freeze(self):
        # the oldest failure of the lot: `prepare` runs in the freeze
        # transaction, so the cast error left it aborted and NO freeze of the
        # mesh could be written while this lot stayed open.
        other = self.pending_lot("r1", "21")
        note_id = self.unreadable_mark(other, self.latest_freeze(other))
        row, out = self.freeze()
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        # the unreadable mark charges nobody: r1 is only the frozen lot's own
        # (valid) designation, not the other lot's ignored mark
        loads = act._loads(self.db)
        self.assertEqual(loads.get("r1"), 1, loads)
        self.assertEqual(loads.get("r2"), 1, loads)
        marker = "review-designation-unreadable:%d:%d:" % (other, note_id)
        self.assertEqual(sum(marker in note for note in self.notes()), 1,
                         self.notes())
        # a replay of the same read does not journal the same note twice
        act.prepare(self.cfg, self.db, self.lot, fetch=False)
        self.assertEqual(sum(marker in note for note in self.notes()), 1,
                         self.notes())

    def test_a_valid_mark_is_still_read_next_to_an_unreadable_one(self):
        self.pending_lot("r1", "31")
        bad = self.pending_lot("r2", "32")
        self.unreadable_mark(bad, self.latest_freeze(bad))
        loads = act._loads(self.db)
        # the valid mark still counts, the invalid one counts for nobody
        self.assertEqual(loads.get("r1"), 1, loads)
        self.assertNotIn("r2", loads)

    def test_a_carried_designation_is_still_read(self):
        # the Python read keeps both prefixes of #3004: a report copied by
        # `verdict_carry` is as authoritative as a direct selection
        lot = self.pending_lot("r1", "41")
        sha = self.frozen_sha(lot)
        carried = verdict_carry.carried_mark_note(
            lot, {"id": self.latest_freeze(lot), "sha": sha},
            {"carried": [{"actor": "r1"}]})
        storage.of(self.db).work.note(lot, "qa", carried, "test")
        self.assertEqual(act._loads(self.db).get("r1"), 1)

    def test_an_unreadable_mark_on_the_frozen_lot_is_ignored_and_journaled(self):
        row, _ = self.freeze()
        note_id = self.unreadable_mark(self.lot, int(row["id"]))
        # the frozen lot's own unreadable mark is ignored (chosen over a named
        # refusal: the freeze must continue, the trace explains the undercount)
        inputs = act.prepare(self.cfg, self.db, self.lot, fetch=False)
        self.assertIsInstance(inputs, act.Inputs)
        self.assertNotIn("r1", act._loads(self.db))
        marker = "review-designation-unreadable:%d:%d:" % (self.lot, note_id)
        self.assertEqual(sum(marker in note for note in self.notes()), 1,
                         self.notes())


class ActivationCliTest(_ActivationFixture, PgTestCase):
    """The production caller: `ameesh work milestone <lot> frozen`."""

    def test_the_cli_freezes_and_designates(self):
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", self.frozen,
            "--actor", "author",
            env=self.env(AMEESH_CANON=self.canon.root, AMEESH_CANON_REF="HEAD",
                         AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("reviewers: r1 (claude), r2 (codex)", proc.stdout)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        self.assertEqual(len(self.marks()), 1)

    def test_the_cli_explains_a_light_lot(self):
        # #2561: the freeze must not print nothing for a light lot; the
        # no-pre-merge-review note is the visible outcome.
        self.repo.git("reset", "-q", "--hard", self.base)
        self.repo.write("docs/README.md", "doc\n")
        self.frozen = self.repo.commit("light doc")
        self.receipts()
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", self.frozen,
            "--actor", "author",
            env=self.env(AMEESH_CANON=self.canon.root, AMEESH_CANON_REF="HEAD",
                         AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("review: none", proc.stdout)
        self.assertIn("no pre-merge reviewer", proc.stdout)
        self.assertEqual(self.mailbox(), [])
        shown = self.mesh("work", "reviewers", str(self.lot),
                          env=self.env(AMEESH_CANON=self.canon.root,
                                       AMEESH_CANON_REF="HEAD"))
        self.assertIn("designation: none", shown.stdout)
        self.assertIn("no pre-merge reviewer", shown.stdout)

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

    def test_the_cli_apply_does_not_echo_a_removed_seat(self):
        # #3685 : le pilote a retiré r1 ; `--apply` affichait « designated:
        # r1 », repris de la marque. La sortie nomme la marque historique et la
        # désignation vivante, jamais le siège retiré comme redésigné.
        register_agent(self.db, "r3", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("r3",))
        self.freeze()
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r2'] WHERE id = %s",
            (self.lot,))
        proc = self.mesh("work", "reviewers", str(self.lot), "--apply",
                         env=self.env(AMEESH_CANON=self.canon.root,
                                      AMEESH_CANON_REF="HEAD"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = [line for line in proc.stdout.splitlines()
                 if line.startswith("designated:")]
        self.assertEqual(len(lines), 1, proc.stdout)
        self.assertNotIn("r1", lines[0])
        self.assertIn("r3", lines[0])
        self.assertIn("mark       : historical", proc.stdout)
        self.assertIn("removed by the pilot", proc.stdout)


class FreezeTransactionNetworkTest(_ActivationFixture, PgTestCase):
    """#3836 — le réseau ne se lit jamais dans la transaction du gel.

    Incident du 27/10 : le gel de #3700 lisait un gel antérieur absent ; la
    forge était demandée par SHA exact dans la transaction, qui tenait le
    verrou du dépôt de migration et celui du projet. La session est restée
    `idle in transaction` sept minutes et les écritures du mesh ont attendu.
    """

    def _prior_ghost(self) -> int:
        ghost = "a" * 40
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, at)"
            " VALUES (%s, 'frozen', %s, 'author', now() - interval '1 hour')",
            (self.lot, ghost))
        return int(self.db.query(
            "SELECT id FROM work_item_milestones"
            " WHERE work_item_id = %s AND sha = %s", (self.lot, ghost))[0]["id"])

    def test_the_freeze_reading_never_fetches_inside_the_transaction(self):
        prior = self._prior_ghost()
        calls = []

        def forge(repo, sha, *, timeout=None):
            calls.append((sha, freeze_gate._in_transaction(self.db)))
            return False

        threads: list = []
        outcome: dict = {}
        with mock.patch.object(review_authors, "_forge_fetch", side_effect=forge):
            work.milestone(
                self.db, self.lot, "frozen", sha=self.frozen, actor="author",
                supersedes=prior, on_frozen=act.hook(self.cfg, outcome),
                after_commit=threads)
        for record in threads:
            record(self.db.cfg, self.db)
        self.assertEqual(calls, [],
                         "la lecture du gel a demandé la forge dans sa transaction")

    def test_the_gate_prefetches_the_prior_object_before_the_transaction(self):
        prior = self._prior_ghost()
        calls = []

        def prefetch(db, item, target, head):
            calls.append(("prefetch", freeze_gate._in_transaction(db)))
            return []

        def forge(repo, sha, *, timeout=None):
            calls.append(("forge", freeze_gate._in_transaction(self.db)))
            return False

        self.db.cfg = self.cfg
        with mock.patch.object(act, "prefetch_freeze_objects", side_effect=prefetch), \
                mock.patch.object(review_authors, "_forge_fetch", side_effect=forge):
            work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                           actor="author", supersedes=prior)
        self.assertIn(("prefetch", False), calls)
        self.assertNotIn(("forge", True), calls)


class FreezeReadingOutsideTransactionTest(_ActivationFixture, PgTestCase):
    """#3948 — la lecture des gels antérieurs ne tient pas la transaction.

    Incident #3889 : `prepare` lit les auteurs de tous les gels antérieurs
    (`_carried_from_previous`) ; sur une branche rebasée aux nombreux gels,
    cette lecture Git prend plus de 120 s. Faite dans la transaction du jalon,
    elle laissait la session `idle in transaction` au-delà de la garde de
    180 s (#3836), qui tuait la connexion : « connection lost during a
    transaction ». La lecture est désormais faite avant le BEGIN ; le hook ne
    reçoit que le résultat.
    """

    def setUp(self):
        # The default hook reads the canon from `db.cfg`; restore the fixture's
        # canon-less cfg after the test so a later `work.add` in this class's
        # `setUp` does not inherit the human-owner requirement (#3948).
        previous = self.db.cfg
        super().setUp()
        self.db.cfg = self.cfg
        self.addCleanup(setattr, self.db, "cfg", previous)

    def _prior_freeze(self) -> int:
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, at)"
            " VALUES (%s, 'frozen', %s, 'author', now() - interval '1 hour')",
            (self.lot, self.base))
        return int(self.db.query(
            "SELECT id FROM work_item_milestones"
            " WHERE work_item_id = %s AND sha = %s ORDER BY id DESC",
            (self.lot, self.base))[0]["id"])

    def test_the_reading_is_prepared_before_the_transaction(self):
        self._prior_freeze()
        calls: list = []
        original = review_authors._carried_from_previous

        def carried(*args, **kwargs):
            calls.append(("carried", freeze_gate._in_transaction(self.db)))
            return original(*args, **kwargs)

        original_prepare = act.prepare

        def prepare(*args, **kwargs):
            calls.append(("prepare", freeze_gate._in_transaction(self.db)))
            return original_prepare(*args, **kwargs)

        with mock.patch.object(review_authors, "_carried_from_previous",
                               side_effect=carried), \
                mock.patch.object(act, "prepare", side_effect=prepare):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        self.assertIn(("carried", False), calls,
                      "la lecture des gels antérieurs n'a pas été faite hors transaction")
        self.assertIn(("prepare", False), calls)
        self.assertFalse(any(in_transaction for _name, in_transaction in calls),
                         "une lecture de désignation a tourné dans la transaction du gel")
        self.assertEqual(set(designated(work.get(self.db, self.lot))), {"r1", "r2"})

    def test_a_prepare_failure_is_journaled_and_keeps_the_freeze(self):
        with mock.patch.object(act, "prepare", side_effect=RuntimeError("boom")):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        notes = self.notes("review-designation-unavailable:")
        self.assertTrue(any("internal designation error: RuntimeError: boom" in note
                            for note in notes), notes)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])

    def _prepare_then(self, change):
        original = act.prepare

        def prepare(*args, **kwargs):
            inputs = original(*args, **kwargs)
            change()
            return inputs

        return mock.patch.object(act, "prepare", side_effect=prepare)

    def test_a_target_change_during_the_reading_does_not_designate(self):
        # #3948 / relecture cx2 : la cible peut changer pendant les ~140 s de
        # lecture. Le hook doit refuser la préparation, pas désigner sur
        # l'ancienne cible.
        def change():
            self.db.execute(
                "UPDATE work_items SET branch_target = %s WHERE id = %s",
                ("origin/release", self.lot))

        with self._prepare_then(change):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        notes = self.notes("review-designation-unavailable:")
        self.assertTrue(any("target changed" in note for note in notes), notes)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])

    def test_a_freeze_written_during_the_reading_does_not_designate(self):
        # #3948 / relecture cx2 : un gel concurrent arrive après la lecture des
        # gels antérieurs ; ses auteurs ne sont pas dans la préparation.
        def change():
            self.db.execute(
                "INSERT INTO work_item_milestones"
                " (work_item_id, kind, sha, actor, at)"
                " VALUES (%s, 'frozen', %s, 'author', now())",
                (self.lot, "b" * 40))

        with self._prepare_then(change):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        notes = self.notes("review-designation-unavailable:")
        self.assertTrue(any("a freeze was written" in note for note in notes),
                        notes)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])

    def test_an_assignee_change_during_the_reading_does_not_designate(self):
        # #3948 / relecture cx2 : l'assignation porte l'auteur de référence.
        def change():
            self.db.execute(
                "UPDATE work_items SET assignee = %s WHERE id = %s",
                ("r1", self.lot))

        with self._prepare_then(change):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        notes = self.notes("review-designation-unavailable:")
        self.assertTrue(any("assignee changed" in note for note in notes), notes)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])

    def test_a_freeze_written_just_before_the_marker_is_still_detected(self):
        # #3948 / relecture cx2, course fine : un gel concurrent écrit APRÈS
        # l'id provisoire mais AVANT la capture de la marque. La marque de
        # préparation doit être bornée comme la lecture (`id < gel`), et se
        # lire par id : `at` est l'heure de début de transaction, qu'un gel
        # concurrent plus ancien peut précéder. Sinon la marque contient déjà
        # le concurrent et le hook le croit lu, alors que ses auteurs n'ont pas
        # été lus.
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, sha, actor, at)"
            " VALUES (%s, 'frozen', %s, 'author', now())",
            (self.lot, self.base))
        original = act._latest_freeze_marker
        inserted = []

        def marker(db, lot, *, before_id=None):
            if not inserted:
                inserted.append(True)
                self.db.execute(
                    "INSERT INTO work_item_milestones"
                    " (work_item_id, kind, sha, actor, at)"
                    " VALUES (%s, 'frozen', %s, 'author',"
                    "         now() - interval '1 hour')",
                    (self.lot, "d" * 40))
            return original(db, lot, before_id=before_id)

        with mock.patch.object(act, "_latest_freeze_marker",
                               side_effect=marker):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        notes = self.notes("review-designation-unavailable:")
        self.assertTrue(any("a freeze was written" in note for note in notes),
                        notes)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])

    def test_a_review_floor_raised_during_the_reading_does_not_designate(self):
        # #3948 / #3557 : un plancher posé pendant la lecture ne doit pas
        # laisser la préparation désigner avec l'ancien compte d'avis. Canon à
        # un avis : le plancher 2 dépasse le compte préparé.
        from ameesh import review_floor

        self.canon = _Canon(
            os.path.join(self.tmp, "canon-un-avis-" + self._testMethodName),
            federation=_federation(reviews=1))
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root,
                                       canon_ref="HEAD")
        self.db.cfg = self.cfg

        def change():
            review_floor.set_floor(self.cfg, self.db, self.lot, 2,
                                   reason="plancher pendant la lecture",
                                   actor="human:smichea", complete=False)

        with self._prepare_then(change):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        notes = self.notes("review-designation-unavailable:")
        self.assertTrue(any("review floor was raised" in note for note in notes),
                        notes)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])

    def test_a_carried_freeze_defers_the_superseded_notification(self):
        # #3948 B cx3 : les chemins `carried` et `waiver` sortent avant de
        # renseigner la lecture d'exclusion. La notification des anciens
        # blocages doit être différée après le COMMIT : jamais
        # `_excluded_on_freeze` (lecture de tous les gels antérieurs) sous le
        # verrou du gel.
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, sha, actor, at)"
            " VALUES (%s, 'frozen', %s, 'author', now() - interval '1 hour')",
            (self.lot, self.base))
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, sha, actor, verdict, at)"
            " VALUES (%s, 'verdict', %s, 'r1', 'blocked',"
            "         now() - interval '1 hour')",
            (self.lot, self.base))
        calls = []

        def excluded(cfg, db, item, freeze, **kwargs):
            calls.append(freeze_gate._in_transaction(self.db))
            return set()

        shape = {"state": "carried", "reviewers": [], "freeze": {}}
        with mock.patch.object(act, "carried_plan",
                               return_value={"result": "would-carry"}), \
                mock.patch.object(act, "_carried_in_transaction",
                                  return_value=shape), \
                mock.patch.object(act, "_excluded_on_freeze",
                                  side_effect=excluded):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(calls, [False],
                         "la notification a relu les auteurs sous le verrou")
        recipients = [row["recipient"] for row in self.db.query(
            "SELECT recipient FROM agent_mailbox ORDER BY id")]
        self.assertIn("r1", recipients)


class FreezeProvisionalProofTest(_ActivationFixture, PgTestCase):
    """#3948 B2 — le reçu du jalon provisoire n'est jamais lu.

    Un gel hors tour n'a pas de preuve (`proof is None`). L'id provisoire du
    jalon, `max(id du lot) + 1`, est un bigserial **global** : il désigne en
    général le jalon d'un autre lot, dont le reçu prouverait à tort la famille
    du gel. La preuve du gel doit être vide (`{}`), pas absente (`None`).
    """

    def setUp(self):
        previous = self.db.cfg
        super().setUp()
        self.db.cfg = self.cfg
        self.addCleanup(setattr, self.db, "cfg", previous)

    def test_an_out_of_turn_freeze_reads_no_foreign_receipt(self):
        reads = []

        def receipt(provider, db, milestone_id, *, strict):
            reads.append(int(milestone_id))
            return {"subject": "milestone:%d" % int(milestone_id),
                    "sha": self.frozen, "actor": "author", "session": "s",
                    "turn": "t", "harness": "claude", "record": "r"}

        with mock.patch.object(review_authors, "_milestone_receipt",
                               side_effect=receipt), \
                mock.patch.object(prov, "proof_for_current_turn",
                                  return_value=None):
            row = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                 actor="author")
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(reads, [],
                         "le gel hors tour a lu le reçu d'un id provisoire")
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])


class FreezeLockWaitTest(_ActivationFixture, PgTestCase):
    """#3836 — un gel n'attend pas un verrou en tenant ceux du mesh.

    La récidive #3820 a attendu 174 s le lookup de provenance de son gel en
    tenant le verrou du dépôt de migration et celui du projet ; la transaction
    est restée ouverte. La transaction du jalon borne ses attentes à 15 s.

    N3 revue #3836 : `SET LOCAL` ne borne que les prises qui le suivent ; un
    gel appelé dans une transaction déjà ouverte est couvert pour ses propres
    prises (avec la borne de l'appelant si elle est plus courte), pas pour les
    verrous que l'appelant tenait déjà.
    """

    def test_a_locked_lot_fails_the_freeze_and_releases_its_locks(self):
        pid = int(self.db.query("SELECT pg_backend_pid() AS pid")[0]["pid"])
        other = db_mod.connect(self.cfg)
        try:
            with other.transaction() as tx:
                tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE",
                         (self.lot,))
                with mock.patch.object(pg_work, "FREEZE_LOCK_TIMEOUT_MS", 200):
                    with self.assertRaises(db_mod.DbError) as caught:
                        work.milestone(self.db, self.lot, "frozen",
                                       sha=self.frozen, actor="author")
                self.assertIn("lock timeout", str(caught.exception).lower())
        finally:
            other.close()
        leftover = self.db.query(
            "SELECT count(*)::int AS n FROM pg_locks"
            " WHERE pid = %s AND locktype = 'advisory'", (pid,))[0]["n"]
        self.assertEqual(leftover, 0,
                         "le jalon annulé a laissé ses verrous consultatifs")

    def _nested_freeze_lock_timeouts(self, *, caller_bound=None):
        """Gèle dans une transaction déjà ouverte et rend la borne de verrou
        que le jalon voit avant ses propres prises (#3843, N3 de #3836)."""
        seen = []
        real = pg_work._lock_migration_writer

        def spy(tx, item_id, proposed=None):
            seen.append(float(tx.query(
                "SELECT extract(epoch FROM current_setting('lock_timeout')"
                "::interval) AS seconds")[0]["seconds"] or 0))
            return real(tx, item_id, proposed=proposed)

        with self.db.transaction() as outer:
            if caller_bound is not None:
                outer.execute("SET LOCAL lock_timeout = '%s'" % caller_bound)
            with mock.patch.object(pg_work, "_lock_migration_writer",
                                   side_effect=spy):
                # `outer` est la transaction déjà ouverte : sous psql c'est le
                # seul objet qui la porte (le pilote en ouvrirait une neuve).
                work.milestone(outer, self.lot, "frozen",
                               sha=self.frozen, actor="author")
        return seen

    def test_a_nested_freeze_bounds_its_own_lock_waits(self):
        seen = self._nested_freeze_lock_timeouts()
        self.assertTrue(seen, "le jalon n'a pris aucun verrou")
        self.assertTrue(all(value == 15.0 for value in seen), seen)

    def test_a_nested_freeze_keeps_the_callers_shorter_bound(self):
        seen = self._nested_freeze_lock_timeouts(caller_bound="200ms")
        self.assertTrue(seen, "le jalon n'a pris aucun verrou")
        self.assertTrue(all(abs(value - 0.2) < 1e-9 for value in seen), seen)
