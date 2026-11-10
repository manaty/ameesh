# SPDX-License-Identifier: AGPL-3.0-only
"""#6720 : un refus de report sur une tête déjà remplacée ne redésigne pas.

Constat nexlink #6699 (10/11 13:45Z) : rev-02 bloque 812d130, l'auteur corrige,
le gel ab80b4 reçoit les avis favorables de claude1 et codex3, puis un rebasage
regèle 77c74bc. Le report garde les deux avis (adaptation de test), mais le
dernier verdict de rev-02 porte encore sur 812d130 : son report est refusé, le
report est lu « partiel », et la désignation différée (#5481) redésigne
rev-02 et claude1 à 13:54Z, avec un battement validated → qa → validated au
verdict de rev-02. Ce blocage ne comptait déjà plus à la porte du gel
précédent : il est journalisé, mais ne demande aucun siège.
"""
from __future__ import annotations

from ameesh import registry, verdict_carry, work

from .test_5481_report_before_designation import _Reviewed


class SupersededRefusalTest(_Reviewed):
    """Le blocage d'une tête dépassée par le gel précédent ne rouvre rien."""

    def _blocked_then_fixed(self) -> str:
        """X bloqué par rev3, R = correction relue par rev1 et rev2 ; rend R."""
        registry.upsert(self.db, "rev3", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("rev3",))
        self.freeze(self.repo.reviewed)
        self.verdict("rev3", self.repo.reviewed, "blocked")
        # rev3 s'arrête : le gel corrigé désigne rev1 et rev2 (nexlink #6699 :
        # claude1 et codex3 relisent ab80b4, rev-02 n'y siège pas)
        registry.upsert(self.db, "rev3", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="stopped")
        fixed = self.repo.author_commit("lot, corrigé après le blocage\n")
        self.receipt(fixed)
        self.freeze(fixed)
        self.verdict("rev1", fixed)
        self.verdict("rev2", fixed)
        self.db.execute("DELETE FROM agent_mailbox")
        return fixed

    def test_a_stale_block_does_not_make_the_cli_designate(self):
        self._blocked_then_fixed()
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", frozen,
            "--actor", "author", "--repo", self.repo.root,
            env=self.env(AMEESH_CONFIG=self.config_without_radar(),
                         AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("review designation after the carry refused", proc.stdout)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        stale = [note for note in self.notes(verdict_carry.NOT_CARRIED)
                 if verdict_carry.SUPERSEDED in note]
        self.assertEqual(len(stale), 1, self.notes())
        self.assertFalse(self.notes("validated → qa"), self.notes())

    def test_the_deferred_recovery_does_not_designate_either(self):
        self._blocked_then_fixed()
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        work.milestone(self.db, self.lot, "frozen", sha=frozen, actor="author",
                       outcome={}, repo=self.repo.root, defer_designation=True)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)
        self.assertEqual(carried["refused"], [], carried)
        self.assertEqual(len(carried["superseded"]), 1, carried)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")

    def test_a_refusal_on_the_previous_freeze_still_designates(self):
        # Le témoin inverse : le blocage porte sur la tête du gel précédent,
        # le report refuse et la désignation reprend (#5481 inchangé).
        self._blocked_then_fixed()
        previous = self.freeze_latest()["sha"]
        self.verdict("rev3", previous, "blocked")
        frozen = self.repo.adapt_test()
        self.receipt(frozen)
        work.milestone(self.db, self.lot, "frozen", sha=frozen, actor="author",
                       outcome={}, repo=self.repo.root, defer_designation=True)
        carried = verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root)
        self.assertTrue(carried["refused"], carried)
        self.assertEqual(carried["superseded"], [], carried)
