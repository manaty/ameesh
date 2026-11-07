# SPDX-License-Identifier: AGPL-3.0-only
"""#6089 : un hold de pilote survit au quorum (#5758), mais il se signale.

Constat de l'auditeur, 07/11 : #5993 est validé par le quorum à 09:19Z le
06/11 sous un hold de mesh-design (« second avis indépendant manquant…
Décision owner #194905 (lot #5999) : connexion du compte codex secondaire »).
L'avis attendu était venu ; le débit rangeait pourtant l'attente parmi les
humaines (« owner … connexion »), `--check` ne sortait pas 2, et le hold n'a
été levé qu'à 21:07Z, à la main.

Témoins : le hold d'un pilote sur un lot validé est une attente
`hold_validated`, jamais humaine, comptée depuis la validation ; le quorum
réel écrit au détenteur du hold, une fois par hold.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import debit, mail, storage, work
from .support import PgTestCase
from .test_3437_debit_dependance import H, T
from .test_3498_hold_externe import event, item, run

CAUSE = ("hold: second avis indépendant manquant… Décision owner #194905 "
         "(lot #5999) : connexion du compte codex secondaire")


def pilot_hold(lot, since, note=CAUSE, by="mesh-design"):
    return {lot: {by: {"what": "hold", "by": by, "since": since,
                       "note": note, "pilot": True}}}


class DebitHoldValideTest(unittest.TestCase):
    def report(self, *, validated_at, hold_at=T - 12 * H, state="validated",
               pilot=True):
        events = [event(5993, 1, T - 13 * H, "build → qa — frozen", actor="ameesh",
                        state="qa"),
                  event(5993, 2, hold_at, CAUSE, actor="mesh-design", state="qa")]
        if state == "validated":
            events.append(event(5993, 3, validated_at,
                                "qa → validated — review quorum (#4423)",
                                actor="ameesh", state="validated"))
        holds = pilot_hold(5993, hold_at)
        holds[5993]["mesh-design"]["pilot"] = pilot
        return run(items=[item(5993)], events=events, holds=holds)

    def test_hold_de_pilote_sur_lot_valide_n_est_pas_humain(self):
        report = self.report(validated_at=T - 3 * H)
        rows = [row for row in report["slow_waits"] if row["lot"] == 5993]
        self.assertEqual([row["kind"] for row in rows], ["hold_validated"])
        self.assertFalse(rows[0]["human"])
        self.assertEqual(rows[0]["since_ts"], T - 3 * H)
        self.assertIn("mesh-design", rows[0]["gesture"])
        self.assertEqual(report["human_waits"], [])

    def test_le_seuil_part_de_la_validation(self):
        """Le hold a 12 h, la validation 1 h : rien avant le seuil."""
        report = self.report(validated_at=T - 1 * H)
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["human_waits"], [])

    def test_un_hold_repose_apres_la_validation_repart(self):
        """Le pilote a répondu en redisant sa cause : l'âge part de là."""
        report = self.report(validated_at=T - 5 * H, hold_at=T - 1 * H)
        self.assertEqual(report["slow_waits"], [])

    def test_lot_en_qa_garde_la_lecture_ordinaire(self):
        """Avant le quorum, la cause humaine reste humaine (sans changement)."""
        report = self.report(validated_at=None, state="qa")
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual([row["kind"] for row in report["human_waits"]], ["hold"])

    def test_hold_ordinaire_non_pilote_inchange(self):
        report = self.report(validated_at=T - 3 * H, pilot=False)
        self.assertEqual([row["kind"] for row in report["human_waits"]], ["hold"])
        self.assertEqual(report["slow_waits"], [])

    def test_etat_d_entree(self):
        events = [event(1, 1, 10.0, None, state="qa"),
                  event(1, 2, 20.0, None, state="validated"),
                  event(1, 3, 30.0, "note", state="validated")]
        self.assertEqual(debit._entered_state(events, "validated"), 20.0)
        events.append(event(1, 4, 40.0, None, state="qa"))
        self.assertIsNone(debit._entered_state(events, "validated"))


class QuorumNotifiesHolderTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="validation", app="demo")["id"]
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")
        self.ops = storage.of(self.db).work
        patcher = mock.patch("ameesh.merge_queue.review_gate_for_item")
        self.gate = patcher.start()
        self.addCleanup(patcher.stop)
        self.gate.return_value = {"ok": False}
        # mesh-design est un pilote déclaré (hold et autorité de levée)
        for name in ("mesh-design", "mty-amh-impl-05"):
            self.assertEqual(self.register(name, "claude", cwd=self.tmp).returncode, 0)
        env = mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": "mesh-design"})
        env.start()
        self.addCleanup(env.stop)
        self.mark("frozen")

    def mark(self, kind="verdict"):
        return self.ops.add_milestone(
            self.lot, kind, sha="a" * 40, actor="reader",
            verdict="ok" if kind == "verdict" else None, note="", proof=None)

    def notices(self, who):
        return [row for row in mail.unread(self.db, who)
                if (row.get("payload") or {}).get("validated_hold")]

    def test_quorum_ecrit_au_detenteur_du_hold(self):
        work.note(self.db, self.lot, CAUSE, actor="mesh-design")
        work.note(self.db, self.lot, "hold: le mien", actor="mty-amh-impl-05")
        self.gate.return_value = {"ok": True}
        self.mark()
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        notices = self.notices("mesh-design")
        self.assertEqual(len(notices), 1)
        self.assertIn("#%d" % self.lot, notices[0]["body"])
        self.assertIn("unhold:", notices[0]["body"])
        # #6096 : une demande de geste réveille le détenteur au repos
        self.assertEqual(mail.passive_reason(notices[0]), "")
        # le hold ordinaire, lui, tombe avec la transition (#5758) : pas d'avis
        self.assertEqual(self.notices("mty-amh-impl-05"), [])
        # #5758 : le hold tient toujours
        self.assertIn("mesh-design", work._active_holds(self.db, self.lot))

    def test_sans_hold_aucun_courrier(self):
        self.gate.return_value = {"ok": True}
        self.mark()
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")
        self.assertEqual(self.notices("mesh-design"), [])

    def test_hold_humain_note_au_lot(self):
        work.note(self.db, self.lot, "hold: pose VM", actor="human:pilote")
        self.gate.return_value = {"ok": True}
        self.mark()
        notes = [row["note"] for row in work.events(self.db, self.lot)
                 if "review quorum reached under the pilot hold" in
                 str(row.get("note") or "")]
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
