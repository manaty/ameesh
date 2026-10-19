# SPDX-License-Identifier: AGPL-3.0-only
"""#2557 : le dédoublonnage du rejeu forcé survit à un alias et à une boîte purgée.

#2541 filtrait les courriels déjà partis du gel sur `agent_mailbox`, avec la clé
lot+gel. La boîte n'est qu'une file de distribution : une purge (scission,
rétention) en efface la preuve, et un destinataire renommé — l'ancien nom devenu
alias — ne s'y compare pas au nom primaire que la sélection redonne. La marque
du gel porte désormais les courriels réellement déposés, en noms primaires ; le
rejeu forcé les relit au journal, qui n'est pas purgé, et compare des primaires.
"""
from __future__ import annotations

import json

from ameesh import registry
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg

from .test_2487_designation_carry import _Fixture


class ReplayDedupDurableTest(_Fixture):
    """La marque du gel est la preuve durable ; la boîte n'est qu'une file."""

    def _freeze_reviewed(self):
        row, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        return row, outcome

    def _mails_for(self, freeze_id: int) -> list:
        return [row for row in self.review_mail()
                if int((row["payload"]["review_designation"] or {}).get("freeze") or 0)
                == int(freeze_id)]

    def _marks(self, freeze_id: int) -> list:
        prefix = pg.mark_prefix(self.lot, freeze_id)
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,)) if str(row["note"]).startswith(prefix)]

    def _unavailable(self, name: str, harness: str) -> None:
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status="stopped")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        (name,))

    def _rename_with_alias(self, old: str, new: str) -> None:
        """Renomme un agent en gardant son ancien nom comme alias."""
        self.db.execute(
            "UPDATE agent_registry SET name = %s, aliases = ARRAY[%s] WHERE name = %s",
            (new, old, old))
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        (new,))

    def test_the_mark_records_the_deposited_mails(self):
        # Le socle : la marque du gel nomme les courriels partis, en primaires.
        _, outcome = self._freeze_reviewed()
        mark = pg.designation_of(self._marks(int(outcome["freeze"]["id"]))[-1])
        self.assertEqual([(m["to"], m["kind"]) for m in mark["sent"]],
                         [("rev1", "request"), ("rev2", "request")])

    def test_a_purged_mailbox_does_not_resend_the_requests(self):
        # La boîte est effacée après le gel : le rejeu forcé ne redépose rien.
        _, outcome = self._freeze_reviewed()
        freeze_id = int(outcome["freeze"]["id"])
        self.db.execute("DELETE FROM agent_mailbox")
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(sorted(r["name"] for r in applied["reviewers"]),
                         ["rev1", "rev2"])
        self.assertEqual(self.mailbox(), [])

    def test_a_purged_mailbox_does_not_repeat_the_waiting_notice(self):
        # Même preuve pour l'avis d'attente : la marque le nomme, la purge ne
        # le fait pas repartir.
        self._unavailable("rev1", "claude")
        self._unavailable("rev2", "codex")
        row, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "waiting", outcome)
        freeze_id = int(row["id"])
        self.assertEqual([m["recipient"] for m in self._mails_for(freeze_id)],
                         ["pilot"])
        mark = pg.designation_of(self._marks(freeze_id)[-1])
        self.assertEqual([(m["to"], m["capacity"]) for m in mark["sent"]],
                         [("pilot", True)])
        self.db.execute("DELETE FROM agent_mailbox")
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "waiting", applied)
        self.assertEqual(self.mailbox(), [])

    def test_a_renamed_recipient_alias_does_not_resend_the_request(self):
        # rev1 devient rev1x, rev1 restant son alias : la demande déjà partie
        # pour rev1 (nom primaire d'alors) ne repart pas pour rev1x.
        _, outcome = self._freeze_reviewed()
        freeze_id = int(outcome["freeze"]["id"])
        self._rename_with_alias("rev1", "rev1x")
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(sorted(r["name"] for r in applied["reviewers"]),
                         ["rev1x", "rev2"])
        self.assertEqual([m["recipient"] for m in self._mails_for(freeze_id)],
                         ["rev1", "rev2"])

    def test_a_purged_mailbox_and_an_alias_keep_only_the_delta(self):
        # Boîte purgée + destinataire renommé : seule la demande du nouveau
        # relecteur (rev4) part au rejeu.
        self._freeze_reviewed()
        self._rename_with_alias("rev1", "rev1x")
        self._unavailable("rev2", "codex")
        for name, harness in (("rev3", "claude"), ("rev4", "codex")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute(
                "UPDATE agent_registry SET team = 'ameesh' WHERE name = %s", (name,))
        self.db.execute("DELETE FROM agent_mailbox")
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(sorted(r["name"] for r in applied["reviewers"]),
                         ["rev1x", "rev4"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["rev4"])

    def test_a_legacy_mark_without_sent_still_proves_its_requests(self):
        # Migration : une marque d'avant #2557 (sans liste `sent`) prouve ses
        # demandes par son état `designated` ; la boîte peut avoir été purgée.
        _, outcome = self._freeze_reviewed()
        freeze_id = int(outcome["freeze"]["id"])
        note = self._marks(freeze_id)[-1]
        mark = pg.designation_of(note)
        mark.pop("sent", None)
        legacy = "%s %s" % (pg.mark_prefix(self.lot, freeze_id),
                            json.dumps(mark, ensure_ascii=False, sort_keys=True))
        self.db.execute("UPDATE work_item_events SET note = %s"
                        " WHERE work_item_id = %s AND note = %s",
                        (legacy, self.lot, note))
        self.db.execute("DELETE FROM agent_mailbox")
        applied = act.designate(self.cfg, self.db, self.lot, force=True)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual(self.mailbox(), [])
