# SPDX-License-Identifier: AGPL-3.0-only
"""#2487 : un gel réaligné dont les verdicts sont reportés garde ses relecteurs.

La désignation automatique (#2447) tourne dans la transaction du gel, donc
avant le report (#2442). Sur un gel réaligné qualifié par le report, elle doit
s'abstenir : aucun nouveau relecteur, aucun courrier, la colonne `reviewers`
garde ceux dont le verdict est reporté, et la porte reste ouverte. Un gel qui
n'est pas un réalignement désigne normalement ; un plan de report qui
n'aboutit pas est réparé par la désignation de repli.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
from unittest import mock

from ameesh import db as db_mod
from ameesh import lot_class, merge_queue, registry, verdict_carry, work
from ameesh import review_designation_activation as act
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

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def _git(root, *args) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


class _Repo:
    """main B0 → T1 ; branche lot/9-x = B0 + un fichier du lot (tête relue)."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ameesh-2487-")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True, env=ENV)
        self.b0 = self.commit("shared.txt", "un\ndeux\ntrois\n", "b0")
        _git(self.root, "switch", "-q", "-c", "lot/9-x")
        self.reviewed = self.commit("src/ameesh/lot.py", "lot\n", "lot")
        _git(self.root, "switch", "-q", "main")
        self.tip = self.commit("src/ameesh/target.py", "target\n", "t1")

    def commit(self, path: str, text: str, message: str) -> str:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
        _git(self.root, "add", path)
        _git(self.root, "commit", "-q", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")

    def merge_target(self) -> str:
        """F = la tête relue réalignée par fusion de la cible."""
        _git(self.root, "switch", "-q", "lot/9-x")
        _git(self.root, "merge", "-q", "--no-edit", "main")
        return _git(self.root, "rev-parse", "HEAD")

    def author_commit(self, text: str = "lot, corrigé\n") -> str:
        """Une vraie tête d'auteur : ce n'est pas un réalignement."""
        _git(self.root, "switch", "-q", "lot/9-x")
        return self.commit("src/ameesh/lot.py", text, "fix")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class _Canon:
    """Un canon minimal : mécanique du train, politique d'avis et pilote."""

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", root], check=True, env=ENV)
        for path, text in (("index.md", "# test canon\n"),
                           ("federation.yaml", FEDERATION),
                           ("processus/mecanique.yaml", MECHANICS),
                           ("agents/pilot.md", PILOT)):
            self.commit(path, text)

    def commit(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
        _git(self.root, "add", path)
        _git(self.root, "commit", "-q", "-m", "canon")


class _Fixture(PgTestCase):
    """Un lot ameesh gelé, un canon actif et deux relecteurs indépendants."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM canon_state")
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)
        self.canon = _Canon(os.path.join(self.tmp, "canon"))
        # Le lot est créé sans canon (le contrôle d'assignation exigerait un
        # responsable humain), puis la connexion relit la canon pour le gel.
        setup = self.db
        for name, harness in (("author", "deepseek"), ("rev1", "claude"),
                              ("rev2", "codex"), ("pilot", "claude")):
            registry.upsert(setup, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            setup.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                          (name,))
        self.item = work.add(setup, title="Désignation au gel", app="ameesh",
                             assignee="author", branch="lot/9-x",
                             branch_target="main")
        self.lot = int(self.item["id"])
        work.move(setup, self.lot, "build")
        setup.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('author', 'deepseek', 's-deepseek', now() - interval '1 minute')")
        self.cfg = dataclasses.replace(
            self.cfg, canon=self.canon.root, canon_ref="HEAD",
            radar={"projects": {"ameesh": {"repo": self.repo.root,
                                           "target": "main"}}})
        self.db = db_mod.connect(self.cfg)
        self.addCleanup(self.db.close)
        self.receipt(self.repo.reviewed)

    # -- helpers ----------------------------------------------------------
    def receipt(self, sha: str) -> None:
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, 'author', 's-deepseek', 't', 'deepseek', %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, self.lot))

    def freeze(self, sha: str):
        outcome: dict = {}
        row = work.milestone(self.db, self.lot, "frozen", sha=sha,
                             actor="author", outcome=outcome)
        return row, outcome

    def verdict(self, actor: str, sha: str, verdict: str = "ok") -> None:
        work.milestone(self.db, self.lot, "verdict", sha=sha, actor=actor,
                       verdict=verdict)

    def mailbox(self):
        return self.db.query(
            "SELECT recipient, body, payload FROM agent_mailbox ORDER BY id")

    def review_mail(self) -> list:
        """Les courriers de désignation (demande de relecture), pas les mots
        passifs du report qui disent seulement « pas de re-relecture due »."""
        return [row for row in self.mailbox()
                if isinstance(row.get("payload"), dict)
                and row["payload"].get("review_designation")]

    def notes(self, prefix: str = "") -> list:
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,)) if str(row["note"]).startswith(prefix)]

    def gate(self, sha: str) -> dict:
        klass = {"class": lot_class.SENSITIVE, "doubt": False,
                 "files": [{"path": "src/ameesh/verdict_carry.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/verdict_carry.py"]}]}
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), sha,
            repo=self.repo.root, klass=klass,
            item=dict(work.get(self.db, self.lot), assignee="author"))

    def designated(self) -> list:
        return designated(work.get(self.db, self.lot))

    def config_file(self) -> str:
        path = os.path.join(self.tmp, "config-2487.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"canon": self.canon.root, "canon_ref": "HEAD",
                       "radar": {"projects": {"ameesh": {
                           "repo": self.repo.root, "target": "main"}}}}, handle)
        return path


class CarriedFreezeTest(_Fixture):
    """Le témoin : gel réaligné, désignation active, aucun nouveau relecteur."""

    def _reviewed_freeze(self):
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["rev1", "rev2"])
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")

    def test_the_carry_plan_covers_the_realigned_head_before_the_freeze(self):
        # Lecture seule avant tout gel : le plan reconnaît le réalignement et
        # ne désigne personne. Il ne doit rien écrire avant le gel.
        self._reviewed_freeze()
        frozen = self.repo.merge_target()
        before = (len(self.mailbox()), len(self.notes()))
        plan = verdict_carry.would_carry(self.cfg, self.db, self.lot, frozen)
        self.assertEqual(plan["result"], "would-carry", plan)
        self.assertEqual([e["actor"] for e in plan["carried"]], ["rev1", "rev2"])
        self.assertEqual((len(self.mailbox()), len(self.notes())), before)

    def test_a_realigned_freeze_keeps_the_reported_reviewers_without_mail(self):
        self._reviewed_freeze()
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        self.assertEqual([r["name"] for r in outcome["reviewers"]], ["rev1", "rev2"])
        # aucun nouveau relecteur, aucun courrier : la désignation s'abstient
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertTrue(self.notes(verdict_carry.CARRIED_MARK_PREFIX))
        # le report recopie les verdicts et la porte passe sur la tête gelée
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(len([m for m in work.milestones(self.db, self.lot)
                              if m["kind"] == "verdict" and m["sha"] == frozen]), 2)
        gate = self.gate(frozen)
        self.assertTrue(gate["ok"], gate["why"])
        self.assertEqual(self.review_mail(), [])
        # la lecture de reprise montre la désignation reportée, pas une absence
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "carried")
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev1", "rev2"])
        # rejouer la reprise ne remplace pas les relecteurs reportés
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], "carried", again)
        self.assertEqual(self.review_mail(), [])

    def test_the_cli_freeze_keeps_the_reported_reviewers(self):
        self._reviewed_freeze()
        frozen = self.repo.merge_target()
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", frozen,
            "--actor", "author",
            env=self.env(AMEESH_CONFIG=self.config_file()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("reviewers: kept from carried verdicts (rev1, rev2)",
                      proc.stdout)
        self.assertIn("verdicts carried (alignment on main@", proc.stdout)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertTrue(self.gate(frozen)["ok"], self.gate(frozen)["why"])
        shown = self.mesh("work", "reviewers", str(self.lot),
                          env=self.env(AMEESH_CONFIG=self.config_file()))
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertIn("designation: kept from carried verdicts (rev1, rev2)",
                      shown.stdout)

    def test_a_carried_mark_is_replaced_by_a_later_real_designation(self):
        # Un vrai gel ultérieur re-désigne : les noms gardés par le report sont
        # des noms automatiques, donc retirés avant la sélection suivante.
        self._reviewed_freeze()
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        for name in ("rev1", "rev2"):
            registry.upsert(self.db, name, harness="deepseek",
                            host=self.cfg.host, cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                            (name,))
        changed = self.repo.author_commit()
        self.receipt(changed)
        self.db.execute("DELETE FROM agent_mailbox")
        _, outcome = self.freeze(changed)
        self.assertEqual(outcome["state"], "waiting", outcome)
        self.assertEqual(self.designated(), [])


class NotCarriedTest(_Fixture):
    """Ce qui n'est pas un réalignement garde la désignation normale."""

    def test_a_new_author_head_still_designates_and_mails(self):
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        changed = self.repo.author_commit()
        self.receipt(changed)
        _, outcome = self.freeze(changed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["rev1", "rev2"])
        self.assertFalse(self.notes(verdict_carry.CARRIED_MARK_PREFIX))
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "not-carried", carried)

    def test_the_executor_repairs_a_plan_whose_carry_refused(self):
        # Le gel s'est abstenu sur le plan ; si le report n'aboutit pas, la
        # passe de l'exécuteur rejoue la désignation réelle du même gel, une
        # fois, au lieu de laisser le lot sans relecteur.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        failure = {"lot": self.lot, "result": "not-carried", "frozen": frozen,
                   "target": "main", "tip": self.repo.tip,
                   "carried": [], "refused": [{"reviewed": self.repo.reviewed,
                                               "why": "injected"}], "detail": ""}
        with mock.patch.object(verdict_carry, "carry", return_value=failure):
            report = verdict_carry.run_pass(self.cfg, self.db)
        mine = [row for row in report if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, report)
        self.assertEqual(mine[0].get("designation"), "designated", mine[0])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["rev1", "rev2"])
        self.assertEqual(self.designated(), ["rev1", "rev2"])

    def test_a_real_designation_after_a_failed_carry_removes_the_kept_names(self):
        # Le repli de la CLI : le report a refusé, `force` désigne réellement
        # et la colonne ne garde pas les noms du plan abandonné.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["rev1", "rev2"])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        # la marque réelle est prioritaire à la lecture
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "designated")


if __name__ == "__main__":
    import unittest
    unittest.main()
