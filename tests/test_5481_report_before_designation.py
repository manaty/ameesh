# SPDX-License-Identifier: AGPL-3.0-only
"""#5481 : le report des verdicts passe avant toute désignation au gel.

Constat nexlink #5434 (02/11 09:31Z) : un regel d'adaptation de test a
désigné et écrit aux relecteurs *avant* d'évaluer le report ; le report a
réussi 69 s plus tard, mais les courriers étaient partis. Un regel de la même
tête a redésigné encore, et trois hôtes ont brûlé cinq tentatives de reprise
#4834 sur un lot déjà `validated`.

Les trois défauts sont couverts ici :
1. le gel diffère la désignation, le report passe d'abord (avec le `--repo`
   du gel, ce qui évite le `no-repository` d'un hôte sans réglage radar) ;
2. un regel du même SHA garde la validation et les relecteurs, sans courrier ;
3. une reprise de quorum périmée ou déjà validée ne consomme aucun essai.
"""
from __future__ import annotations

import json
import os
from unittest import mock

from ameesh import merge_queue, plan_git, review_designation_activation as act
from ameesh import storage, verdict_carry, work

from .test_2487_designation_carry import _Fixture


class _Reviewed(_Fixture):
    """Un gel relu, deux avis favorables, boîte aux lettres vidée."""

    def reviewed_freeze(self) -> None:
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")

    def config_without_radar(self) -> str:
        """Un hôte qui n'a aucun dépôt radar : le gel fournit `--repo`."""
        path = os.path.join(self.tmp, "config-5481-no-radar.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"canon": self.canon.root, "canon_ref": "HEAD",
                       "radar": {"projects": {}}}, handle)
        return path

    def freeze_latest(self) -> dict:
        return plan_git.latest_freeze(work.milestones(self.db, self.lot))


class ReportBeforeDesignationTest(_Reviewed):
    """Le report est évalué avant que quiconque soit relancé."""

    def test_a_test_adaptation_carries_before_any_review_mail(self):
        # Le scénario du constat : la tête relue a ses deux avis, puis une
        # adaptation de test ajoute un fichier e2e. La CLI doit reporter
        # immédiatement, sans déposer de demande de relecture.
        self.reviewed_freeze()
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", frozen,
            "--actor", "author", "--repo", self.repo.root,
            env=self.env(AMEESH_CONFIG=self.config_without_radar(),
                         AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.assertTrue(self.notes(verdict_carry.CARRIED))
        self.assertIn("verdicts carried", proc.stdout)

    def test_the_cli_designates_after_the_report_refuses(self):
        # Un vrai changement d'auteur n'est ni un réalignement ni une
        # adaptation : le report refuse, et c'est seulement là que la CLI
        # désigne et écrit aux relecteurs (#5481).
        self.reviewed_freeze()
        changed = self.repo.author_commit("vraie correction\n")
        self.receipt(changed)
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", changed,
            "--actor", "author", "--repo", self.repo.root,
            env=self.env(AMEESH_CONFIG=self.config_without_radar(),
                         AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])
        self.assertIn("review designation after the carry refused", proc.stdout)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")

    def test_a_report_that_carries_writes_no_review_designation_mark(self):
        # Le gel différé n'écrit aucune sélection ; le report post-gel réussit
        # et garde les relecteurs sans courrier.
        self.reviewed_freeze()
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        outcome: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=frozen,
                       actor="author", outcome=outcome, repo=self.repo.root,
                       defer_designation=True)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual((carried.get("validation") or {}).get("result"),
                         "validated", carried)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")

    def test_a_freeze_reads_the_report_plan_in_its_repository(self):
        # Défaut 4 du constat : `no-repository` sur une branche `review/...`
        # publiée. Le gel lit `--repo` ; le plan du report doit lire le même
        # dépôt, s'abstenir de désigner, puis le report écrit la marque gardée.
        self.reviewed_freeze()
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        outcome: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=frozen, actor="author",
                       outcome=outcome, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(outcome.get("state"), "carried", outcome)
        self.assertEqual(self.review_mail(), [])
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)


class SameHeadTest(_Reviewed):
    """Un regel du même SHA ne redésigne pas et ne rouvre pas la revue."""

    def _validated_head(self) -> str:
        """Un gel réaligné, ses verdicts reportés, lot validé."""
        self.reviewed_freeze()
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.db.execute("DELETE FROM agent_mailbox")
        return frozen

    def test_a_same_head_refreeze_keeps_validation_without_mail(self):
        frozen = self._validated_head()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.assertTrue(self.notes("same head re-freeze"))
        self.assertTrue(self.notes(verdict_carry.CARRIED_MARK_PREFIX))
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "carried")
        self.assertEqual([r["name"] for r in shown["designation"]["reviewers"]],
                         ["rev1", "rev2"])

    def test_a_same_head_refreeze_through_the_cli_does_not_mail(self):
        frozen = self._validated_head()
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen",
            "--sha", frozen, "--actor", "author",
            "--repo", self.repo.root,
            env=self.env(AMEESH_CONFIG=self.config_without_radar(),
                         AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")


class SameHeadRaceTest(_Reviewed):
    """La vérification du même SHA se refait sous le verrou du gel."""

    def test_a_head_change_under_the_lock_refuses_the_shortcut(self):
        self.freeze(self.repo.reviewed)
        other = self.repo.author_commit("autre tête\n")
        self.receipt(other)
        self.freeze(other)
        before = [m["id"] for m in work.milestones(self.db, self.lot)
                  if m["kind"] == "frozen"]
        outcome: dict = {}
        with self.assertRaises(work.WorkError):
            storage.of(self.db).work.add_milestone(
                self.lot, "frozen", sha=self.repo.reviewed, actor="author",
                verdict=None, note="", on_frozen=act.same_head_hook(outcome))
        after = [m["id"] for m in work.milestones(self.db, self.lot)
                 if m["kind"] == "frozen"]
        self.assertEqual(after, before)
        self.assertEqual(outcome, {})


class DeferredRecoveryTest(_Reviewed):
    """Un gel différé interrompu est repris une fois par l'exécuteur (#5481)."""

    def _deferred_freeze(self, sha: str) -> dict:
        self.receipt(sha)
        outcome: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=sha, actor="author",
                       outcome=outcome, repo=self.repo.root,
                       defer_designation=True)
        return outcome

    def _run_recovery(self) -> dict:
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            return act.finish_deferred(self.cfg, self.db)

    def test_an_interrupted_deferred_freeze_is_designated_once(self):
        self.reviewed_freeze()
        changed = self.repo.author_commit("vraie correction\n")
        self._deferred_freeze(changed)
        self.assertTrue(self.notes(act.DEFERRED_PREFIX))
        self.db.execute("DELETE FROM agent_mailbox")
        first = self._run_recovery()
        mine = [row for row in first["results"] if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, first)
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        # La marque et l'issue finale sont écrites : la passe suivante ne
        # redésigne pas et ne remet aucun courrier.
        second = self._run_recovery()
        self.assertEqual([row for row in second["results"] if row["lot"] == self.lot], [])
        self.assertEqual(len(self.review_mail()), 2)

    def test_a_deferred_freeze_whose_report_carries_mails_nobody(self):
        self.reviewed_freeze()
        frozen = self.repo.adapt_test()
        # Le plan lu avant le gel échoue (par exemple sans réglage radar) :
        # le gel diffère. Le report de la passe suivante réussit et garde les
        # avis, sans courrier.
        with mock.patch.object(act, "carried_plan", return_value=None):
            self._deferred_freeze(frozen)
        self.assertTrue(self.notes(act.DEFERRED_PREFIX))
        self.db.execute("DELETE FROM agent_mailbox")
        first = self._run_recovery()
        mine = [row for row in first["results"] if row["lot"] == self.lot]
        self.assertEqual([row["result"] for row in mine], ["carried"], first)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.assertTrue(self.notes(verdict_carry.CARRIED))


class QuorumRetryTest(_Reviewed):
    """#4834 : une reprise périmée ou déjà validée ne consomme aucun essai."""

    def test_a_stale_freeze_never_validates_a_newer_head(self):
        self.reviewed_freeze()
        old = self.freeze_latest()
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        # Un nouveau gel d'une AUTRE tête : la reprise de l'ancien gel ne doit
        # pas valider cette tête, même si ses propres verdicts reportés sont
        # encore dans la frise.
        newer = self.repo.author_commit("autre contenu\n")
        self.receipt(newer)
        self.freeze(newer)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        result = verdict_carry.promote_carried(self.db, self.lot, freeze=old)
        self.assertIn(result["result"], ("stale", "idle"), result)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        self.assertEqual(self.notes(verdict_carry.PENDING), [])

    def test_an_already_validated_race_writes_no_pending_attempt(self):
        # Le veilleur de la course : `promote_carried` lit `qa`, un autre
        # hôte valide, `work.move` lève « already in validated ». C'est le
        # résultat attendu, pas une panne : aucune ligne pending, aucun essai.
        self.reviewed_freeze()
        freeze = self.freeze_latest()
        qa_row = dict(work.get(self.db, self.lot), state="qa")
        real_get = work.get
        calls = {"n": 0}

        def fake_get(db, item_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return qa_row
            return real_get(db, item_id)

        with mock.patch.object(verdict_carry.work, "get", side_effect=fake_get), \
                mock.patch.object(verdict_carry, "_carried_ok_on_freeze",
                                  return_value=[{"id": 1}]), \
                mock.patch.object(merge_queue, "review_gate_for_item",
                                  return_value={"ok": True, "why": ""}):
            result = verdict_carry.promote_carried(self.db, self.lot,
                                                   freeze=freeze)
        self.assertEqual(result["result"], "validated", result)
        self.assertEqual(self.notes(verdict_carry.PENDING), [])
        self.assertEqual([row for row in verdict_carry.run_pass(self.cfg, self.db)
                          if row["lot"] == self.lot], [])
