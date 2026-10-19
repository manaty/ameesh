# SPDX-License-Identifier: AGPL-3.0-only
"""#2541 : un rejeu forcé ne renvoie pas les courriels déjà partis du gel.

Le rejeu `force` (#2487) écrit une marque plus récente que le report, mais il
ne doit pas redéposer les demandes de relecture ni l'avis d'attente du **même**
gel : un second réveil du même relecteur et une seconde alerte de capacité ne
sont pas des faits nouveaux. Le témoin inverse compte autant : une marque
`carried` nomme des relecteurs déjà sollicités pour un **autre** gel, et le
rejeu qui les désigne après un report refusé doit bien leur écrire pour ce gel.
"""
from __future__ import annotations

from ameesh import registry
from ameesh import review_designation_activation as act

from .test_2487_designation_carry import _Fixture


class ForceReplayMailTest(_Fixture):
    """La boîte de réception est la preuve d'envoi, par gel."""

    def _freeze_reviewed(self):
        row, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        return row, outcome

    def _mails_for(self, freeze_id: int) -> list:
        return [row for row in self.review_mail()
                if int((row["payload"]["review_designation"] or {}).get("freeze") or 0)
                == int(freeze_id)]

    def _unavailable(self, name: str, harness: str) -> None:
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status="stopped")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        (name,))

    def test_a_force_replay_does_not_mail_a_designation_twice(self):
        # Même gel, mêmes relecteurs : la marque est réécrite, pas les courriels.
        _, outcome = self._freeze_reviewed()
        freeze_id = int(outcome["freeze"]["id"])
        self.assertEqual([m["recipient"] for m in self._mails_for(freeze_id)],
                         ["rev1", "rev2"])
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertFalse(applied.get("replayed"), applied)
        self.assertEqual(sorted(r["name"] for r in applied["reviewers"]),
                         ["rev1", "rev2"])
        self.assertEqual([m["recipient"] for m in self._mails_for(freeze_id)],
                         ["rev1", "rev2"])

    def test_a_force_replay_does_not_repeat_the_waiting_notification(self):
        # Aucune capacité indépendante : un seul avis d'attente pour le gel.
        self._unavailable("rev1", "claude")
        self._unavailable("rev2", "codex")
        row, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "waiting", outcome)
        freeze_id = int(row["id"])
        first = self._mails_for(freeze_id)
        self.assertEqual([m["recipient"] for m in first], ["pilot"])
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "waiting", applied)
        self.assertEqual([m["recipient"] for m in self._mails_for(freeze_id)],
                         ["pilot"])

    def test_a_force_replay_mails_only_the_new_reviewers(self):
        # Le rejeu garde rev1 (demande déjà partie) et ajoute rev4 : seule la
        # nouvelle demande part, l'ancienne ne repart pas.
        self._freeze_reviewed()
        self._unavailable("rev2", "codex")
        for name, harness in (("rev3", "claude"), ("rev4", "codex")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute(
                "UPDATE agent_registry SET team = 'ameesh' WHERE name = %s", (name,))
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(sorted(r["name"] for r in applied["reviewers"]),
                         ["rev1", "rev4"])
        self.assertEqual([m["recipient"] for m in self.review_mail()],
                         ["rev1", "rev2", "rev4"])

    def test_a_forced_designation_after_a_carried_mark_mails_this_freeze(self):
        # La marque `carried` nomme rev1/rev2, mais leur demande date du gel
        # précédent : le rejeu forcé de CE gel doit les réveiller.
        _, outcome = self._freeze_reviewed()
        self.assertNotIn("replayed", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()
        row, carried = self.freeze(frozen)
        self.assertEqual(carried["state"], "carried", carried)
        carried_id = int(row["id"])
        self.assertEqual(self._mails_for(carried_id), [])
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(sorted(r["name"] for r in applied["reviewers"]),
                         ["rev1", "rev2"])
        self.assertEqual([m["recipient"] for m in self._mails_for(carried_id)],
                         ["rev1", "rev2"])
