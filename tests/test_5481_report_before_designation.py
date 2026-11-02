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

from ameesh import merge_queue, plan_git, registry, review_floor
from ameesh import review_designation_activation as act
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

    def test_a_relative_repository_is_resolved_for_the_report(self):
        # `--repo .` : le report le lit depuis le clone radar, pas depuis le
        # répertoire courant du processus ; le chemin doit être résolu avant.
        with mock.patch("os.getcwd", return_value=self.repo.root):
            self.assertEqual(verdict_carry._repo_path("."), self.repo.root)
        self.assertEqual(verdict_carry._repo_path("https://example.invalid/x.git"),
                         "https://example.invalid/x.git")

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

    def test_a_same_head_refreeze_through_the_cli_asks_only_the_missing_seat(self):
        # #4582 : rev1 a voté sur la tête, rev2 non. Le regel du même SHA ne
        # peut pas garder le quorum (il manque un avis) : la désignation passe
        # après le report (idle ici) et ne relance que rev2.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen",
            "--sha", self.repo.reviewed, "--actor", "author",
            "--repo", self.repo.root,
            env=self.env(AMEESH_CONFIG=self.config_without_radar(),
                         AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev2"])
        self.assertEqual(self.designated(), ["rev1", "rev2"])


class LiveColumnCoverageTest(_Reviewed):
    """Un siège retiré par le pilote n'est plus couvert par la marque (#5481)."""

    def _requests_after(self, before: int) -> list[str]:
        return [str(row["recipient"]) for row in self.db.query(
                    "SELECT id, recipient, payload FROM agent_mailbox"
                    " WHERE id > %s ORDER BY id", (before,))
                if isinstance(row.get("payload"), dict)
                and row["payload"].get("review_designation")]

    def test_a_removed_seat_disables_the_same_head_shortcut(self):
        freeze, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        # Le pilote retire le siège codex de la colonne vivante ; le siège ne
        # revient pas (un autre codex est disponible).
        work.designate_reviewer(self.cfg, self.db, self.lot, "rev2",
                                actor="human:smichea", remove=True)
        self.assertEqual(self.designated(), ["rev1"])
        registry.upsert(self.db, "rev4", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = 'rev4'")
        self.db.execute("UPDATE agent_registry SET status = 'stopped'"
                        " WHERE name = 'rev2'")
        before = int(self.db.query(
            "SELECT coalesce(max(id), 0) AS id FROM agent_mailbox")[0]["id"])
        # La marque nomme encore rev2, la colonne vivante non : le raccourci
        # « même tête » figerait une désignation incomplète.
        self.assertFalse(act.acquired_on_head(self.db, self.cfg, self.lot, freeze))
        second: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=self.repo.reviewed,
                       actor="author", outcome=second, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(second.get("state"), "deferred", second)
        self.assertTrue(self.notes(act.DEFERRED_PREFIX))
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            report = act.finish_deferred(self.cfg, self.db)
        mine = [row for row in report["results"] if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, report)
        self.assertEqual(sorted(self.designated()), ["rev1", "rev4"])
        # rev1 nommait déjà cette tête ; le siège codex remplacé reçoit sa
        # demande une seule fois, puis l'issue finale ne la rejoue pas.
        self.assertEqual(self._requests_after(before), ["rev4"])
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            again = act.finish_deferred(self.cfg, self.db)
        self.assertEqual([row for row in again["results"]
                          if row["lot"] == self.lot], [])
        self.assertEqual(self._requests_after(before), ["rev4"])


class CarriedFloorCoverageTest(_Reviewed):
    """Une marque carried complète reste acquise au plancher (#5481, B2)."""

    def _carried_head(self) -> str:
        self.reviewed_freeze()
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot,
                                      repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        return frozen

    def test_a_complete_carried_mark_stays_acquired_at_floor_two(self):
        frozen = self._carried_head()
        # Le plancher courant exige deux familles ; la marque carried n'écrit
        # ni `required` ni les familles des acteurs reportés.
        review_floor.set_floor(self.cfg, self.db, self.lot, 2,
                               reason="deux familles", actor="human:smichea",
                               complete=False)
        freeze = self.freeze_latest()
        mark = act._effective_mark(self.db, self.lot, int(freeze["id"]))
        self.assertEqual(mark[1], "carried")
        self.assertNotIn("required", mark[2])
        self.assertTrue(act.acquired_on_head(self.db, self.cfg, self.lot, freeze))
        self.db.execute("DELETE FROM agent_mailbox")
        # Deux regels identiques du même SHA : le premier écrit la marque
        # carried, le suivant doit rester acquis sans nouvelle désignation.
        for _ in range(2):
            outcome: dict = {}
            work.milestone(self.db, self.lot, "frozen", sha=frozen,
                           actor="author", outcome=outcome, repo=self.repo.root,
                           defer_designation=True)
            self.assertEqual(outcome.get("state"), "carried", outcome)
            self.assertEqual(self.review_mail(), [])
            self.assertFalse(self.notes(act.DEFERRED_PREFIX))
        self.assertEqual(self.designated(), ["rev1", "rev2"])


class SameHeadWithoutSeatsTest(_Fixture):
    """Un premier gel interrompu puis un regel du même SHA reste repris."""

    def test_a_same_head_refreeze_without_seats_keeps_the_deferred_path(self):
        first: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=self.repo.reviewed,
                       actor="author", outcome=first, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(first.get("state"), "deferred", first)
        self.assertEqual(self.designated(), [])
        self.assertTrue(self.notes(act.DEFERRED_PREFIX))
        # La CLI est interrompue ; un regel du même SHA ne doit pas prendre le
        # raccourci « rien à garder » et laisser le lot sans relecteur.
        second: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=self.repo.reviewed,
                       actor="author", outcome=second, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(second.get("state"), "deferred", second)
        self.assertEqual(self.designated(), [])
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            report = act.finish_deferred(self.cfg, self.db)
        mine = [row for row in report["results"] if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, report)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            again = act.finish_deferred(self.cfg, self.db)
        self.assertEqual([row for row in again["results"] if row["lot"] == self.lot], [])
        self.assertEqual(len(self.review_mail()), 2)


class InheritedSeatsTest(_Reviewed):
    """Des sièges hérités d'une autre tête ne prouvent pas une désignation."""

    def test_a_single_verdict_without_mark_does_not_take_the_shortcut(self):
        # #5481 (B/P1 cx2) : un avis isolé sur B, sans aucune marque de
        # désignation de B, ne prouve pas la couverture. Le raccourci « même
        # tête » écrirait carried avec rev1 seul, sans DEFERRED : le siège
        # manquant serait perdu.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        changed = self.repo.author_commit("vraie tête B\n")
        self.receipt(changed)
        first: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=changed, actor="author",
                       outcome=first, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(first.get("state"), "deferred", first)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.verdict("rev1", changed)
        self.db.execute("DELETE FROM agent_mailbox")
        second: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=changed, actor="author",
                       outcome=second, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(second.get("state"), "deferred", second)
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            report = act.finish_deferred(self.cfg, self.db)
        mine = [row for row in report["results"] if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, report)
        self.assertEqual(sorted(self.designated()), ["rev1", "rev2"])
        # rev1 a déjà rendu son avis sur B : seule la seconde famille est
        # relancée, une fois.
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev2"])
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            again = act.finish_deferred(self.cfg, self.db)
        self.assertEqual([row for row in again["results"] if row["lot"] == self.lot], [])
        self.assertEqual(len(self.review_mail()), 1)

    def test_inherited_seats_do_not_take_the_same_head_shortcut(self):
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        changed = self.repo.author_commit("vraie tête B\n")
        self.receipt(changed)
        # Gel B différé, CLI interrompue : la colonne garde les sièges de A.
        first: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=changed, actor="author",
                       outcome=first, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(first.get("state"), "deferred", first)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.db.execute("DELETE FROM agent_mailbox")
        # Regel du même SHA B : sans marque effective sur B, le raccourci ne
        # doit pas écrire une marque non provisoire ni sauter la désignation.
        second: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=changed, actor="author",
                       outcome=second, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(second.get("state"), "deferred", second)
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            report = act.finish_deferred(self.cfg, self.db)
        mine = [row for row in report["results"] if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, report)
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])


class WaitingDesignationTest(_Fixture):
    """Une attente n'est pas une désignation acquise : elle reste à reprendre."""

    def test_a_waiting_mark_is_completed_on_same_head_recovery(self):
        freeze, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        # La désignation effective devient une attente (aucun candidat).
        payload = {"state": "waiting", "required": 2, "reviewers": [],
                   "freeze": {"id": freeze["id"], "sha": freeze["sha"]},
                   "why": "no candidate"}
        storage.of(self.db).work.note(
            self.lot, "qa",
            act.pg.mark_prefix(self.lot, freeze["id"]) + " " + json.dumps(payload),
            "ameesh")
        self.assertFalse(act.acquired_on_head(self.db, self.cfg, self.lot, freeze))
        self.db.execute("DELETE FROM agent_mailbox")
        second: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=self.repo.reviewed,
                       actor="author", outcome=second, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(second.get("state"), "deferred", second)
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            report = act.finish_deferred(self.cfg, self.db)
        mine = [row for row in report["results"] if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, report)
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "designated")

    def test_one_verdict_does_not_neutralize_a_waiting_seat(self):
        # #5481 (B cx2) : deux avis requis, une seule famille disponible.
        # L'attente partielle désigne rev1 ; rev1 rend son avis. Au regel du
        # même SHA, un avis isolé ne prouve pas la couverture : le siège
        # manquant reste à reprendre, et seule la seconde famille est relancée.
        self.db.execute(
            "UPDATE agent_registry SET status = 'stopped' WHERE name = 'rev2'")
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "waiting", outcome)
        self.assertEqual([row["name"] for row in outcome["reviewers"]], ["rev1"])
        # L'attente envoie aussi son avis de capacité au pilote : seules les
        # demandes de relecture comptent ici.
        self.assertEqual([row["recipient"] for row in self.review_mail()
                          if row["recipient"] != "pilot"], ["rev1"])
        # Le verdict arrive sur la tête gelée, mais le quorum (2) n'est pas
        # atteint.
        self.verdict("rev1", self.repo.reviewed)
        # La capacité revient.
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle' WHERE name = 'rev2'")
        second: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=self.repo.reviewed,
                       actor="author", outcome=second, repo=self.repo.root,
                       defer_designation=True)
        self.assertEqual(second.get("state"), "deferred", second)
        with mock.patch.object(act, "DEFERRED_GRACE_S", 0.0):
            report = act.finish_deferred(self.cfg, self.db)
        mine = [row for row in report["results"] if row["lot"] == self.lot]
        self.assertEqual(len(mine), 1, report)
        self.assertEqual(sorted(self.designated()), ["rev1", "rev2"])
        # La demande de rev1 nommait déjà cette tête : aucun second courrier ;
        # seule la famille manquante (rev2) est notifiée par la reprise.
        self.assertEqual([row["recipient"] for row in self.review_mail()
                          if row["recipient"] != "pilot"], ["rev1", "rev2"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "designated")


class DesignateDeferredForceTest(_Reviewed):
    """Un report planifié mais refusé ne neutralise pas la sélection réelle."""

    def test_force_bypasses_the_provisional_carried_mark(self):
        self.reviewed_freeze()
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        self.freeze(frozen)
        freeze = self.freeze_latest()
        self.assertEqual(act.reviewers(self.cfg, self.db, self.lot)
                         ["designation"]["state"], "carried")
        self.db.execute("DELETE FROM agent_mailbox")
        outcome = act.designate_deferred(self.cfg, self.db, self.lot,
                                         freeze, force=True)
        self.assertEqual(outcome.get("state"), "designated", outcome)
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])
        shown = act.reviewers(self.cfg, self.db, self.lot)
        self.assertEqual(shown["designation"]["state"], "designated")
        # Le deuxième appel (CLI/reprise) rend la marque écrite, sans nouveau
        # courrier.
        again = act.designate_deferred(self.cfg, self.db, self.lot,
                                       freeze, force=True)
        self.assertEqual(again.get("state"), "designated", again)
        self.assertEqual(len(self.review_mail()), 2)


class ProvisionalCarriedShortcutTest(_Reviewed):
    """Une marque carried provisoire ne justifie pas le raccourci (#5481).

    Le gel écrit la marque du report avant de l'avoir accompli : elle est
    provisoire. La prendre pour une désignation acquise ferait écrire au regel
    de la même tête une marque définitive, sans qu'aucun siège ait été désigné
    sur cette tête ; le refus du report ne pourrait plus la contourner.
    """

    def _freeze_deferred(self, sha: str) -> dict:
        outcome: dict = {}
        work.milestone(self.db, self.lot, "frozen", sha=sha, actor="author",
                       outcome=outcome, repo=self.repo.root,
                       defer_designation=True)
        return outcome

    def test_a_provisional_mark_keeps_the_report_refusal_recovery_open(self):
        self.reviewed_freeze()
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        first = self._freeze_deferred(frozen)
        self.assertEqual(first.get("state"), "carried", first)
        first_freeze = self.freeze_latest()
        mark = act._effective_mark(self.db, self.lot, int(first_freeze["id"]))
        self.assertEqual(mark[1], "carried")
        self.assertTrue(mark[2].get("provisional"), mark[2])
        # La CLI est interrompue avant le report ; un regel du même SHA ne
        # doit pas convertir la provision en désignation définitive.
        self.assertFalse(act.acquired_on_head(self.db, self.cfg, self.lot,
                                              first_freeze))
        second = self._freeze_deferred(frozen)
        self.assertEqual(second.get("state"), "carried", second)
        self.assertFalse(second.get("same_head"))
        second_freeze = self.freeze_latest()
        again = act._effective_mark(self.db, self.lot, int(second_freeze["id"]))
        self.assertEqual(again[1], "carried")
        self.assertTrue(again[2].get("provisional"), again[2])
        self.assertEqual(self.review_mail(), [])
        # Le report refuse ensuite : la sélection réelle contourne la
        # provision et ne relance les deux sièges qu'une fois.
        self.db.execute("DELETE FROM agent_mailbox")
        outcome = act.designate_deferred(self.cfg, self.db, self.lot,
                                         second_freeze, force=True)
        self.assertEqual(outcome.get("state"), "designated", outcome)
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])
        replay = act.designate_deferred(self.cfg, self.db, self.lot,
                                        second_freeze, force=True)
        self.assertEqual(replay.get("state"), "designated", replay)
        self.assertEqual([row["recipient"] for row in self.review_mail()],
                         ["rev1", "rev2"])


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
