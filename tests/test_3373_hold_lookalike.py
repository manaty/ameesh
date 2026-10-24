# SPDX-License-Identifier: AGPL-3.0-only
"""#3373 — une retenue apparente ne passe plus en silence.

`hold: <raison>` est le préfixe exact (`planned_waits.is_hold_note`). Les
graphies voisines — `hold :`, `hold (pilote) :`, `Hold —` — ressemblent à une
retenue mais n'en sont pas : rien ne s'arrête et l'auteur croit le lot retenu.
Le chemin d'écriture (`work.check_wait_note`, partagé par `work note` et les
notes de transition) refuse ces variantes avec la forme exacte. Les vraies
notes de retenue et de levée, et les notes ordinaires (`hold-up:`,
`holding pattern`), ne changent pas.
"""
from __future__ import annotations

import unittest

from ameesh import planned_waits as W, work

from .support import PgTestCase


class HoldLookalikePatternTest(unittest.TestCase):
    """Le motif lit les graphies voisines, jamais les formes exactes."""

    def test_lookalikes_are_recognised(self):
        for text in ("hold : fenêtre",
                     "hold  : fenêtre",
                     "hold\t: fenêtre",
                     "hold\u00a0: fenêtre",
                     "hold (pilote) : fenêtre",
                     "Hold — fenêtre",
                     "hold (qa): fenêtre"):
            with self.subTest(text=text):
                self.assertTrue(W.is_hold_lookalike(text))
                self.assertFalse(W.is_hold_note(text))
                self.assertFalse(W.is_lift_note(text))

    def test_exact_forms_and_ordinary_notes_are_not_lookalikes(self):
        for text in ("hold: fenêtre", "HOLD: fenêtre", "unhold: feu",
                     "hold lifted: feu", "hold   lifted : feu",
                     "hold-up: staging", "holding pattern", "threshold reached",
                     "", None):
            with self.subTest(text=text):
                self.assertFalse(W.is_hold_lookalike(text))


class HoldLookalikeWriteTest(PgTestCase):
    """`work note` / les transitions refusent, sans écrire de note."""

    def setUp(self):
        super().setUp()
        self.register("dev-a", "claude")
        self.lot = work.add(self.db, title="hold lookalike", assignee="dev-a")

    def _notes(self):
        return [event.get("note") or "" for event in work.events(self.db, self.lot["id"])]

    def test_work_note_refuses_lookalikes_with_the_exact_form(self):
        for text in ("hold : fenêtre",
                     "hold (pilote) : fenêtre",
                     "Hold — fenêtre"):
            with self.subTest(text=text):
                with self.assertRaises(work.WorkError) as ctx:
                    work.note(self.db, self.lot["id"], text, actor="dev-a")
                message = str(ctx.exception)
                self.assertIn("hold: <reason>", message)
                self.assertIn("unhold: <reason>", message)
                self.assertIn("#3373", message)
        self.assertNotIn("hold : fenêtre", self._notes())
        self.assertNotIn("hold (pilote) : fenêtre", self._notes())
        self.assertNotIn("Hold — fenêtre", self._notes())

    def test_exact_hold_then_lift_still_work(self):
        self.assertTrue(work.note(self.db, self.lot["id"], "hold: fenêtre",
                                  actor="dev-a"))
        # la levée est un geste de pilote (ou de l'auteur lié, #3219) : la
        # forme exacte n'est jamais visée par le refus des graphies voisines.
        self.assertTrue(work.note(self.db, self.lot["id"], "unhold: feu reçu",
                                  actor="human:proprio"))
        notes = self._notes()
        self.assertIn("hold: fenêtre", notes)
        self.assertIn("unhold: feu reçu", notes)

    def test_ordinary_note_is_still_written(self):
        self.assertTrue(work.note(self.db, self.lot["id"], "hold-up: staging",
                                  actor="dev-a"))
        self.assertIn("hold-up: staging", self._notes())

    def test_transition_note_is_refused_too(self):
        with self.assertRaises(work.WorkError):
            work.move(self.db, self.lot["id"], "build",
                      note="hold (pilote) : fenêtre", actor="dev-a")
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "intake")

    def test_cli_exits_nonzero_and_names_the_exact_form(self):
        proc = self.mesh("work", "note", str(self.lot["id"]), "Hold — fenêtre",
                         env=self.env(AGENT_MAIL_NAME="dev-a"))
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hold: <reason>", proc.stderr)
        self.assertIn("#3373", proc.stderr)
        self.assertNotIn("Hold — fenêtre", self._notes())


if __name__ == "__main__":
    unittest.main()
