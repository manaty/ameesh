# SPDX-License-Identifier: AGPL-3.0-only
"""#5557 — une levée annoncée en prose ne laisse plus le hold actif en silence.

Le journal ne lit la levée que dans la forme exacte (`unhold:`, `hold
lifted:`…). Une note qui annonce pourtant « le hold du staging est levé »
n'arrêtait rien : le débit gardait l'attente active (#5484, deux holds restés
deux heures après la levée annoncée). La note est écrite (c'est une note
ordinaire, le lot demandait un avertissement), mais la CLI cite alors la
commande exacte `unhold: <raison>` et les auteurs des retenues actives ; sans
hold actif, pas d'avertissement. Une note qui dit que le hold n'est pas levé,
qu'il le sera, qu'il est maintenu ou qu'une levée est demandée n'annonce rien.
"""
from __future__ import annotations

import time
import unittest

from ameesh import planned_waits as W, storage, work

from .support import PgTestCase


class ProseLiftPatternTest(unittest.TestCase):
    """`announces_lift` lit une annonce acquise, jamais une intention."""

    def test_prose_lifts_are_recognised(self):
        for text in (
                "Le hold du staging est levé.",
                "Les holds sont levés.",
                "Le hold a été levé à 22:04Z.",
                "Le hold vient d'être levé.",
                "Holds périmés levés : impl-18 depuis 11:17Z, et pilote depuis 20:40Z.",
                "Levée du hold : staging attesté.",
                "Le pilote a levé le hold.",
                "The hold is lifted.",
                "The holds have been released.",
                "hold released",
                "Holds levés.",
                "Le hold est levé jusqu'à demain.",
        ):
            with self.subTest(text=text):
                self.assertTrue(W.announces_lift(text))

    def test_intentions_negations_requests_and_questions_are_not_announcements(self):
        for text in (
                "Le hold peut être levé par le pilote.",
                "Le hold n'est pas levé.",
                "Le hold n'a pas encore été levé.",
                "Le hold n'est plus levé.",
                "Aucun hold n'est levé.",
                "Le hold sera levé après le staging.",
                "Le hold doit être levé avant 22 h.",
                "Il faut que le hold soit levé.",
                "Une fois le hold levé, on repart.",
                "Le hold reste à lever.",
                "Le hold est-il levé ?",
                "Je ne sais pas si le hold est levé.",
                # B auditeur du 03/11 : conservation et demande, pas une
                # levée acquise.
                "hold maintenu jusqu'à la levée du gel",
                "levée du hold demandée au pilote",
                "La levée du hold est demandée au pilote.",
                "levée du hold requise",
                "the hold is not lifted",
                "the hold will be lifted",
                "the hold has not yet been released",
                "the hold is maintained until the freeze is lifted",
                "lift of the hold requested",
                "le staging est levé",
                "Relevé du staging : L2 reste.",
        ):
            with self.subTest(text=text):
                self.assertFalse(W.announces_lift(text))

    def test_exact_forms_are_not_prose(self):
        for text in ("hold: fenêtre", "unhold: feu reçu",
                     "hold lifted: feu reçu", "hold-up: staging", "", None):
            with self.subTest(text=text):
                self.assertFalse(W.announces_lift(text))


class ProseLiftWriteTest(PgTestCase):
    """La note est écrite ; l'avertissement rend la levée actionnable."""

    def setUp(self):
        super().setUp()
        self.register("dev-a", "claude")
        self.register("dev-b", "claude")
        self.lot = work.add(self.db, title="hold levé en prose", assignee="dev-a")

    def _note(self, text: str, actor: str = "dev-a", **kwargs):
        return work.note(self.db, self.lot["id"], text, actor=actor, **kwargs)

    def _notes(self) -> list[str]:
        return [event.get("note") or ""
                for event in work.events(self.db, self.lot["id"])]

    def _active(self) -> dict[str, dict]:
        events = storage.of(self.db).progress.lot_hold_events(
            [self.lot["id"]], W.HOLD_NOTE_PATTERNS)
        return W.active_holds(self.db, events).get(self.lot["id"], {})

    def _warning(self, text: str) -> str:
        return work.hold_lift_warning(self.db, self.lot["id"], text)

    def test_prose_lift_is_written_with_a_warning_and_the_hold_stays(self):
        self._note("hold: staging hors créneau", actor="human:proprio")
        text = "Le hold du staging est levé."
        self.assertTrue(self._note(text, actor="human:proprio"))
        self.assertIn(text, self._notes())
        warning = self._warning(text)
        self.assertIn("unhold: <reason>", warning)
        self.assertIn("#5484", warning)
        self.assertIn("human:proprio", warning)
        self.assertEqual(set(self._active()), {"human:proprio"})

    def test_both_holds_are_named_and_stay_active(self):
        """Le cas de #5484 : deux retenues annoncées levées en prose."""
        self._note("hold: gestes owner", actor="human:proprio")
        self._note("hold: attente du paquet", actor="dev-a")
        text = "Holds périmés levés : les deux sont finis."
        self.assertTrue(self._note(text, actor="human:proprio"))
        warning = self._warning(text)
        self.assertIn("human:proprio", warning)
        self.assertIn("dev-a", warning)
        self.assertEqual(set(self._active()), {"human:proprio", "dev-a"})

    def test_without_a_hold_there_is_no_warning(self):
        text = "Le hold du staging est levé."
        self._note(text)
        self.assertIn(text, self._notes())
        self.assertEqual(self._warning(text), "")

    def test_non_announcements_have_no_warning(self):
        self._note("hold: staging", actor="human:proprio")
        for text in ("Le hold n'est pas levé : L2 reste.",
                     "Le hold peut être levé par le pilote.",
                     "Le hold est maintenu jusqu'à la levée du gel",
                     "levée du hold demandée au pilote",
                     "Le hold est-il levé ?",
                     "Relevé du staging : L2 reste.",
                     "hold-up: staging"):
            with self.subTest(text=text):
                self._note(text, actor="human:proprio")
                self.assertIn(text, self._notes())
                self.assertEqual(self._warning(text), "")

    def test_a_new_hold_note_may_recall_an_old_lift(self):
        self._note("hold: gestes owner", actor="human:proprio")
        text = "hold: L2 ; l'ancien hold du staging est levé"
        self._note(text, actor="human:proprio")
        self.assertIn(text, self._notes())
        self.assertEqual(self._warning(text), "")

    def test_exact_unhold_still_lifts_without_a_warning(self):
        self._note("hold: staging", actor="human:proprio")
        text = "unhold: staging attesté"
        self._note(text, actor="human:proprio")
        self.assertEqual(self._active(), {})
        self.assertIn(text, self._notes())
        self.assertEqual(self._warning(text), "")

    def test_wake_note_is_written_and_warned(self):
        self._note("hold: staging", actor="human:proprio")
        text = "Le hold est levé."
        self._note(text, actor="human:proprio", wake_ts=time.time() + 3600)
        self.assertTrue(any(note.startswith(text) and "[réveil programmé:" in note
                            for note in self._notes()))
        self.assertIn("#5484", self._warning(text))

    def test_a_state_change_lifts_before_the_warning(self):
        self._note("hold: staging", actor="human:proprio")
        work.move(self.db, self.lot["id"], "build", actor="human:proprio")
        self.assertEqual(self._active(), {})
        text = "Le hold du staging est levé."
        self._note(text, actor="human:proprio")
        self.assertIn(text, self._notes())
        self.assertEqual(self._warning(text), "")

    def test_a_transition_note_is_written_and_the_state_lifts(self):
        """La transition lève les holds : la note de move n'a rien à lever."""
        self._note("hold: staging", actor="human:proprio")
        text = "Le hold est levé, on passe en build."
        item = work.move(self.db, self.lot["id"], "build", note=text,
                         actor="human:proprio")
        self.assertEqual(item["state"], "build")
        self.assertIn(text, self._notes())
        self.assertEqual(self._active(), {})

    def test_cli_returns_zero_writes_the_note_and_warns(self):
        self._note("hold: staging")
        text = "Le hold du staging est levé."
        proc = self.mesh("work", "note", str(self.lot["id"]), text,
                         env=self.env(AGENT_MAIL_NAME="dev-a"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("note added", proc.stdout)
        self.assertIn("unhold: <reason>", proc.stderr)
        self.assertIn("#5484", proc.stderr)
        self.assertIn(text, self._notes())


if __name__ == "__main__":
    unittest.main()
