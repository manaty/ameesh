# SPDX-License-Identifier: AGPL-3.0-only
"""#5557 — une levée annoncée en prose ne laisse plus le hold actif en silence.

Le journal ne lit la levée que dans la forme exacte (`unhold:`, `hold
lifted:`…). Une note qui annonce pourtant « le hold du staging est levé »
n'arrêtait rien : le débit gardait l'attente active (#5484, deux holds restés
deux heures après la levée annoncée). Le chemin d'écriture (`work note`, et la
note de transition) refuse désormais cette annonce tant qu'un hold est actif,
en citant la commande exacte `unhold: <raison>` ; sans hold actif, la note
ordinaire passe, et une note qui dit qu'il n'est pas levé, qu'il le sera ou
demande s'il l'est n'est pas une annonce.
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
        ):
            with self.subTest(text=text):
                self.assertTrue(W.announces_lift(text))

    def test_intentions_negations_and_questions_are_not_announcements(self):
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
                "the hold is not lifted",
                "the hold will be lifted",
                "the hold has not yet been released",
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
    """Le hold actif rend l'annonce en prose actionnable, pas silencieuse."""

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

    def test_prose_lift_is_refused_while_a_hold_is_active(self):
        self._note("hold: staging hors créneau", actor="human:proprio")
        with self.assertRaises(work.WorkError) as ctx:
            self._note("Le hold du staging est levé.", actor="human:proprio")
        message = str(ctx.exception)
        self.assertIn("unhold: <reason>", message)
        self.assertIn("#5484", message)
        self.assertIn("human:proprio", message)
        self.assertNotIn("Le hold du staging est levé.", self._notes())
        self.assertEqual(set(self._active()), {"human:proprio"})

    def test_both_holds_stay_active_and_are_named(self):
        """Le cas de #5484 : deux retenues annoncées levées en prose."""
        self._note("hold: gestes owner", actor="human:proprio")
        self._note("hold: attente du paquet", actor="dev-a")
        with self.assertRaises(work.WorkError) as ctx:
            self._note("Holds périmés levés : les deux sont finis.",
                       actor="human:proprio")
        message = str(ctx.exception)
        self.assertIn("human:proprio", message)
        self.assertIn("dev-a", message)
        self.assertEqual(set(self._active()), {"human:proprio", "dev-a"})

    def test_without_a_hold_the_prose_note_is_written(self):
        self._note("Le hold du staging est levé.")
        self.assertIn("Le hold du staging est levé.", self._notes())

    def test_non_announcements_are_written_under_a_hold(self):
        self._note("hold: staging", actor="human:proprio")
        for text in ("Le hold n'est pas levé : L2 reste.",
                     "Le hold peut être levé par le pilote.",
                     "Le hold est-il levé ?",
                     "Relevé du staging : L2 reste.",
                     "hold-up: staging"):
            with self.subTest(text=text):
                self._note(text, actor="human:proprio")
                self.assertIn(text, self._notes())

    def test_a_new_hold_note_may_recall_an_old_lift(self):
        self._note("hold: gestes owner", actor="human:proprio")
        text = "hold: L2 ; l'ancien hold du staging est levé"
        self._note(text, actor="human:proprio")
        self.assertIn(text, self._notes())

    def test_exact_unhold_still_lifts(self):
        self._note("hold: staging", actor="human:proprio")
        self._note("unhold: staging attesté", actor="human:proprio")
        self.assertEqual(self._active(), {})
        self.assertIn("unhold: staging attesté", self._notes())

    def test_wake_note_is_refused_too(self):
        self._note("hold: staging", actor="human:proprio")
        with self.assertRaisesRegex(work.WorkError, "#5484"):
            self._note("Le hold est levé.", actor="human:proprio",
                       wake_ts=time.time() + 3600)
        self.assertNotIn("Le hold est levé.", self._notes())

    def test_a_state_change_still_lifts_before_a_prose_note(self):
        self._note("hold: staging", actor="human:proprio")
        work.move(self.db, self.lot["id"], "build", actor="human:proprio")
        self.assertEqual(self._active(), {})
        self._note("Le hold du staging est levé.", actor="human:proprio")
        self.assertIn("Le hold du staging est levé.", self._notes())

    def test_transition_note_is_refused_and_the_move_not_done(self):
        self._note("hold: staging", actor="human:proprio")
        with self.assertRaisesRegex(work.WorkError, "#5484"):
            work.move(self.db, self.lot["id"], "build",
                      note="Le hold est levé, on passe en build.",
                      actor="human:proprio")
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "intake")
        self.assertEqual(set(self._active()), {"human:proprio"})

    def test_cli_exits_nonzero_and_names_the_exact_command(self):
        self._note("hold: staging")
        proc = self.mesh("work", "note", str(self.lot["id"]),
                         "Le hold du staging est levé.",
                         env=self.env(AGENT_MAIL_NAME="dev-a"))
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("unhold: <reason>", proc.stderr)
        self.assertIn("#5484", proc.stderr)
        self.assertNotIn("Le hold du staging est levé.", self._notes())


if __name__ == "__main__":
    unittest.main()
