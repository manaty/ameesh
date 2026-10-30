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

    def replay_reviewed(self) -> str:
        """R' = la tête relue rejouée sur la cible : un autre réalignement de F."""
        _git(self.root, "switch", "-q", "-c", "lot/9-x-replay", self.tip)
        _git(self.root, "cherry-pick", self.reviewed)
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
        mark = work.milestone(self.db, self.lot, "verdict", sha=sha, actor=actor,
                              verdict=verdict)
        # #2928 : la porte sensible ne compte que les verdicts à reçu exact.
        full = _git(self.repo.root, "rev-parse", sha)
        harness = str((registry.get(self.db, actor) or {}).get("harness") or "")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), full, actor, "s-" + harness,
             "t", harness, self.lot))

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

    def alternate_reviewers(self) -> None:
        """Rev1/rev2 sont indisponibles ; rev3 (claude) et rev4 (codex) sont
        les seuls relecteurs indépendants disponibles. Leurs familles restent
        celles du registre : la porte comptera les verdicts reportés."""
        for name, harness in (("rev1", "claude"), ("rev2", "codex")):
            registry.upsert(self.db, name, harness=harness,
                            host=self.cfg.host, cwd=self.repo.root, status="stopped")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                            (name,))
        for name, harness in (("rev3", "claude"), ("rev4", "codex")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                            (name,))

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
            env=self.env(AMEESH_CONFIG=self.config_file(),
                         AGENT_MAIL_NAME="author"))
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
        # #4834 : la transition de quorum des avis reportés est appliquée et
        # le journal la porte — le lot ne reste pas en qa faute d'écrivain.
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.assertTrue(self.notes("qa → validated — carried review quorum"))

    def test_a_transient_gate_read_is_retried_by_the_executor_pass(self):
        # #4834 : une lecture de porte ratée au report ne doit pas laisser le
        # lot en qa pour toujours. L'échec est journalisé une fois, et le
        # passage de l'exécuteur applique la transition dès que la porte se lit.
        self._reviewed_freeze()
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        real = merge_queue.review_gate_for_item
        state = {"down": True}

        def flaky(*args, **kwargs):
            if state["down"]:
                raise RuntimeError("temporary gate read failure")
            return real(*args, **kwargs)

        with mock.patch.object(merge_queue, "review_gate_for_item",
                               side_effect=flaky):
            carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                          repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        self.assertTrue(self.notes(verdict_carry.PENDING))
        state["down"] = False
        report = verdict_carry.run_pass(self.cfg, self.db)
        mine = [r for r in report if r["lot"] == self.lot]
        self.assertEqual([r["result"] for r in mine], ["validated"], report)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.assertTrue(self.notes("qa → validated — carried review quorum"))
        # une fois validé, le passage ne réécrit rien
        self.assertEqual([r for r in verdict_carry.run_pass(self.cfg, self.db)
                          if r["lot"] == self.lot], [])

    def test_a_closed_gate_gets_no_pending_note_and_no_retry(self):
        # Une porte lisiblement fermée (un avis sur deux) est le cours normal
        # du qa : aucune marque de panne, aucun retry à chaque passage. Le
        # verdict qui manque la rouvrira par le chemin #4423.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        self.assertEqual(self.notes(verdict_carry.PENDING), [])
        self.assertEqual([r for r in verdict_carry.run_pass(self.cfg, self.db)
                          if r["lot"] == self.lot], [])

    def test_the_retry_of_a_transient_gate_read_is_bounded(self):
        # Une panne qui ne se répare pas ne relit pas la porte indéfiniment :
        # cinq essais, puis le lot reste en qa et l'état parle (l'alerte de lot
        # et l'auditeur prennent le relais).
        self._reviewed_freeze()
        self.freeze(self.repo.merge_target())

        def down(*args, **kwargs):
            raise RuntimeError("temporary gate read failure")

        with mock.patch.object(merge_queue, "review_gate_for_item",
                               side_effect=down):
            verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root)
            self.assertEqual(len(self.notes(verdict_carry.PENDING)), 1)
            for _ in range(verdict_carry.PENDING_ATTEMPTS - 1):
                verdict_carry.run_pass(self.cfg, self.db)
        self.assertEqual(len(self.notes(verdict_carry.PENDING)),
                         verdict_carry.PENDING_ATTEMPTS)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        # épuisé : le passage ne relit plus la porte pour ce lot
        with mock.patch.object(merge_queue, "review_gate_for_item",
                               side_effect=down) as gate_read:
            self.assertEqual([r for r in verdict_carry.run_pass(self.cfg, self.db)
                              if r["lot"] == self.lot], [])
            self.assertEqual(gate_read.call_count, 0)
        self.assertEqual(len(self.notes(verdict_carry.PENDING)),
                         verdict_carry.PENDING_ATTEMPTS)

    def test_a_blind_host_spends_no_attempt_and_a_repo_host_promotes(self):
        # #4834 (B de l'auditeur) : un hôte qui ne peut pas lire la porte ne
        # doit ni écrire de marque ni consommer d'essai. Un passage d'un hôte
        # qui a le dépôt fait la transition. Un `repo_for_freeze` vide ne
        # suffit pas à simuler l'hôte aveugle : la classe du lot peut encore
        # résoudre le dépôt par son cache ; la porte est donc explicitement
        # illisible dans les deux passages aveugles.
        self._reviewed_freeze()
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        with mock.patch.object(lot_class, "repo_for_freeze",
                               return_value=("", "")), \
                mock.patch.object(merge_queue, "review_gate_for_item",
                                  return_value=None):
            carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                          repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(self.notes(verdict_carry.PENDING), [])
        with mock.patch.object(lot_class, "repo_for_freeze",
                               return_value=("", "")), \
                mock.patch.object(merge_queue, "review_gate_for_item",
                                  return_value=None):
            report = verdict_carry.run_pass(self.cfg, self.db)
        self.assertEqual([r for r in report if r["lot"] == self.lot], [])
        self.assertEqual(self.notes(verdict_carry.PENDING), [])
        # l'hôte qui a le dépôt reprend sans essai préalable et promeut
        report = verdict_carry.run_pass(self.cfg, self.db)
        self.assertEqual([r["result"] for r in report if r["lot"] == self.lot],
                         ["validated"], report)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")

    def test_another_pending_verdict_does_not_starve_the_quorum_retry(self):
        # #4834 : un verdict encore reportable sur une autre tête ne doit pas
        # faire oublier la transition de quorum déjà en attente sur ce gel.
        self._reviewed_freeze()
        frozen = self.repo.merge_target()
        self.freeze(frozen)

        def down(*args, **kwargs):
            raise RuntimeError("temporary gate read failure")

        with mock.patch.object(merge_queue, "review_gate_for_item",
                               side_effect=down):
            verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root)
        self.assertTrue(self.notes(verdict_carry.PENDING))
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        # rev3 a voté sur la tête relue : `pending()` reste non vide, alors que
        # les verdicts reportés de rev1/rev2 attendent déjà leur transition.
        # L'INSERT direct évite `_sync_validation` (#4423), qui validerait le
        # lot ici et retirerait le cas que ce test couvre.
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, sha, actor, verdict, note, at)"
            " VALUES (%s, 'verdict', %s, 'rev3', 'ok', '', now())",
            (self.lot, self.repo.reviewed))
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        with mock.patch.object(verdict_carry, "carry",
                               return_value={"lot": self.lot, "result": "not-carried"}):
            with mock.patch.object(verdict_carry, "_carried_pending", return_value=False):
                report = verdict_carry.run_pass(self.cfg, self.db)
        self.assertIn("validated",
                      [r["result"] for r in report if r["lot"] == self.lot], report)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")

    def test_a_pending_reviewer_of_the_previous_freeze_stays_designated(self):
        # B/P1 de dev1 : rev2 n'a pas encore voté au gel précédent. Le gel
        # réaligné ne reporte que des verdicts ; sans garder son siège,
        # `progress` ne l'attend plus et il n'est plus relancé.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        self.assertEqual([r["name"] for r in outcome["reviewers"]], ["rev1", "rev2"])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(self.review_mail(), [])
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual([e["actor"] for e in carried["carried"]], ["rev1"])
        # rev1 est reporté, rev2 reste attendu sur la tête gelée
        gate = self.gate(frozen)
        self.assertFalse(gate["ok"], gate)
        self.assertEqual(gate["on_freeze"], 1, gate)
        self.verdict("rev2", frozen)
        self.assertTrue(self.gate(frozen)["ok"], self.gate(frozen)["why"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev1", "rev2"])

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


class LateCarryTest(_Fixture):
    """Marques du même gel : la dernière du journal fait foi (#2487)."""

    def _old_reviewers(self, second=True):
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        if second:
            self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")

    def test_a_late_carry_after_a_selection_replaces_its_reviewers(self):
        # B/P1 de cx1 : le gel a désigné rev3/rev4 faute de plan ; le report
        # tardif réussit et doit retirer ces noms, pas les ajouter.
        self._old_reviewers()
        self.alternate_reviewers()
        frozen = self.repo.merge_target()
        with mock.patch.object(act, "carried_plan", return_value=None):
            _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual([r["name"] for r in outcome["reviewers"]], ["rev3", "rev4"])
        self.assertEqual(self.designated(), ["rev3", "rev4"])
        self.db.execute("DELETE FROM agent_mailbox")
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertTrue(self.gate(frozen)["ok"], self.gate(frozen)["why"])
        self.assertEqual(self.review_mail(), [])
        # la lecture et la reprise ne rétablissent pas la sélection supplantée
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "carried")
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev1", "rev2"])
        again = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(again["state"], "carried", again)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(self.review_mail(), [])

    def test_a_late_carry_promotes_and_drops_the_selected_seat(self):
        # #4834 / #4715 : la désignation a tourné au gel (rev3/rev4, faute de
        # plan) puis le report tardif a rendu leurs verdicts à rev1/rev2. Le
        # lot doit passer en validated sans attendre le siège de rev3, et la
        # transition de quorum doit être au journal.
        self._old_reviewers()
        self.alternate_reviewers()
        frozen = self.repo.merge_target()
        with mock.patch.object(act, "carried_plan", return_value=None):
            _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual([r["name"] for r in outcome["reviewers"]], ["rev3", "rev4"])
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(sorted(self.designated()), ["rev1", "rev2"])
        self.assertNotIn("rev3", self.designated())
        self.assertTrue(self.gate(frozen)["ok"], self.gate(frozen)["why"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.assertTrue(self.notes("qa → validated — carried review quorum"))

    def test_a_force_designation_after_a_carried_mark_wins(self):
        self._old_reviewers()
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        self.alternate_reviewers()
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual([r["name"] for r in applied["reviewers"]], ["rev3", "rev4"])
        self.assertEqual(self.designated(), ["rev3", "rev4"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["rev3", "rev4"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "designated")
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev3", "rev4"])

    def test_a_carry_after_a_force_designation_takes_the_column_back(self):
        # Le report réussit après une désignation de repli : il écrit une
        # marque plus récente, la colonne revient aux relecteurs reportés.
        self._old_reviewers()
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        self.alternate_reviewers()
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(self.designated(), ["rev3", "rev4"])
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertTrue(self.gate(frozen)["ok"], self.gate(frozen)["why"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "carried")
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev1", "rev2"])


    def test_a_force_designation_after_a_late_carry_supersedes_it(self):
        # B/P1 de cx1 : un report tardif a remplacé la sélection du gel ; le
        # repli `force` doit écrire une marque PLUS RÉCENTE que le report, et
        # non rejouer l'ancienne sélection (colonne à quatre noms).
        self._old_reviewers()
        self.alternate_reviewers()
        frozen = self.repo.merge_target()
        with mock.patch.object(act, "carried_plan", return_value=None):
            _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "designated", outcome)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.db.execute("DELETE FROM agent_mailbox")
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual([r["name"] for r in applied["reviewers"]],
                         ["rev3", "rev4"])
        self.assertFalse(applied.get("replayed"))
        self.assertEqual(self.designated(), ["rev3", "rev4"])
        # #2541/#2557 : les demandes de rev3/rev4 sont déjà parties pour CE
        # gel ; le rejeu forcé écrit la marque plus récente sans les redéposer,
        # même si la boîte a été purgée (la marque du gel en est la preuve).
        self.assertEqual([m["recipient"] for m in self.review_mail()], [])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "designated")
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev3", "rev4"])

    def test_a_late_carry_keeps_the_previous_pending_seat(self):
        # B/P1 de cx1 (B2) : le report ne reporte que des verdicts ; le siège
        # du gel précédent sans verdict (rev2) reste désigné après un report
        # tardif qui a remplacé la sélection du nouveau gel.
        self._old_reviewers(second=False)
        self.alternate_reviewers()
        frozen = self.repo.merge_target()
        with mock.patch.object(act, "carried_plan", return_value=None):
            _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual([r["name"] for r in outcome["reviewers"]],
                         ["rev3", "rev4"])
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(sorted(self.designated()), ["rev1", "rev2"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "carried")
        self.assertEqual(sorted(r["name"] for r in
                                shown["designation"]["reviewers"]),
                         ["rev1", "rev2"])
        # rev2 n'a pas voté : la porte reste fermée sur les deux avis dus.
        self.assertFalse(self.gate(frozen)["ok"])

    def test_selection_carry_force_carry_follow_the_journal_order(self):
        # Cas demandé : sélection → report → force → report. Chaque marque
        # plus récente décide de la colonne et de la lecture.
        self._old_reviewers(second=False)
        self.alternate_reviewers()
        frozen = self.repo.merge_target()
        with mock.patch.object(act, "carried_plan", return_value=None):
            _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "designated", outcome)
        first = verdict_carry.carry(self.cfg, self.db, self.lot,
                                    repo=self.repo.root)
        self.assertEqual(first["result"], "carried", first)
        self.assertEqual(sorted(self.designated()), ["rev1", "rev2"])
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(sorted(self.designated()), ["rev3", "rev4"])
        # un verdict tardif du siège resté en attente, sur un autre
        # réalignement de la même tête, redonne un report après le `force`
        replayed = self.repo.replay_reviewed()
        self.verdict("rev2", replayed)
        second = verdict_carry.carry(self.cfg, self.db, self.lot,
                                     repo=self.repo.root)
        self.assertEqual(second["result"], "carried", second)
        self.assertEqual(sorted(self.designated()), ["rev1", "rev2"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "carried")
        self.assertEqual(sorted(r["name"] for r in
                                shown["designation"]["reviewers"]),
                         ["rev1", "rev2"])
        self.assertNotIn("rev3", self.designated())


class LatePendingSeatTest(_Fixture):
    """Report tardif : les sièges du gel précédent sans verdict restent (#2487)."""

    def _rev1_only(self):
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")

    def test_a_late_carry_after_a_selection_keeps_the_pending_seat(self):
        # Le gel réaligné a désigné rev1/rev2 faute de plan ; le report tardif
        # ne reporte que rev1 : rev2 reste attendu, sinon `progress` n'attend
        # personne et la porte reste fermée.
        self._rev1_only()
        frozen = self.repo.merge_target()
        with mock.patch.object(act, "carried_plan", return_value=None):
            _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "designated", outcome)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual([e["actor"] for e in carried["carried"]], ["rev1"])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev1", "rev2"])
        self.assertFalse(self.gate(frozen)["ok"])
        self.verdict("rev2", frozen)
        self.assertTrue(self.gate(frozen)["ok"], self.gate(frozen)["why"])

    def test_a_carry_after_a_force_designation_keeps_the_pending_seat(self):
        self._rev1_only()
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        self.alternate_reviewers()
        act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(self.designated(), ["rev3", "rev4"])
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual([e["actor"] for e in carried["carried"]], ["rev1"])
        self.assertEqual(self.designated(), ["rev1", "rev2"])



if __name__ == "__main__":
    import unittest
    unittest.main()
