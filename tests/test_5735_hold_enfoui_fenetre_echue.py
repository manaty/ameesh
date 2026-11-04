# SPDX-License-Identifier: AGPL-3.0-only
"""#5735 — un hold enfoui est refusé ; une fenêtre échue ne tait plus le SLO.

Rejeu de nexlink #5684 : la note de 22:49 portait « hold: attente externe »
après une correction de lien. Le journal ne lit que la tête d'une note : rien
n'a retenu le lot, et « prêt → démarré » a crié au niveau 3 au réveil de 07:15.
(a) Le chemin d'écriture refuse désormais une marque `hold:`/`unhold:` qui
n'est pas en tête, en disant de la mettre en tête ou de la citer.
(c) Une fenêtre d'observation de pilote (#4235) échue ne tait plus le délai
intake ; l'horloge repart de l'échéance, lue comme le débit.
"""
from __future__ import annotations

import time
import unittest

from ameesh import planned_waits as W, work

from .test_789_slo_alerts import HEURE, _Base

#: la note de #5684 du 03/11 22:49, telle quelle
NOTE_5684 = ("Correction: the proof link is https://github.com/manaty/nexlink/"
             "issues/3270#issuecomment-6616971182 (not #3269). hold: attente "
             "externe : première route device réelle créée en prod (aucune à "
             "22:50Z). [réveil programmé: 2026-11-04 07:15 UTC]")


class BuriedMarkerPatternTest(unittest.TestCase):

    def test_note_5684_is_buried(self):
        self.assertFalse(W.is_hold_note(NOTE_5684))
        self.assertTrue(W.buried_hold_marker(NOTE_5684).startswith(
            "hold: attente externe"))

    def test_buried_after_sentence_or_line(self):
        for text in ("relevé fait.\nunhold: feu", "fin ; HOLD: x",
                     "v1.2 posée. hold lifted: feu", "ok ! hold: fenêtre"):
            with self.subTest(text=text):
                self.assertIsNotNone(W.buried_hold_marker(text))

    def test_citations_head_markers_and_prose_pass(self):
        for text in ("le préfixe hold: est lu en tête",
                     "voir « hold: x ». fin", "écrire `hold: x`.",
                     'écrire "hold: x".', "ok. Hold on, attente",
                     "3.5 h. holding pattern", "hold: x. unhold: quand y",
                     "unhold: feu. hold: plus tard", "", None):
            with self.subTest(text=text):
                self.assertIsNone(W.buried_hold_marker(text))


class BuriedMarkerWriteTest(_Base):

    def _notes(self):
        return [e.get("note") or "" for e in work.events(self.db, self.lot["id"])]

    def test_note_5684_is_refused_with_the_correction(self):
        with self.assertRaises(work.WorkError) as ctx:
            work.note(self.db, self.lot["id"], NOTE_5684, actor="orch")
        message = str(ctx.exception)
        self.assertIn("Put it first", message)
        self.assertIn("quote it", message)
        self.assertIn("#5735", message)
        self.assertNotIn(NOTE_5684, self._notes())

    def test_transition_note_is_refused_too(self):
        with self.assertRaises(work.WorkError):
            work.move(self.db, self.lot["id"], "build", note=NOTE_5684,
                      actor="dev1")
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "intake")

    def test_quoted_citation_and_head_marker_are_written(self):
        for text in ("Correction du lien. Le préfixe `hold:` retient en tête.",
                     "hold: attente externe : route réelle. Puis lecture."):
            with self.subTest(text=text):
                self.assertTrue(work.note(self.db, self.lot["id"], text,
                                          actor="human:pilote"))
                self.assertIn(text, self._notes())


def _utc(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


class ExpiredWindowSloTest(_Base):
    """#5735 (c) : la fenêtre échue rend le délai intake, depuis l'échéance."""

    def window(self, due, actor="human:pilote", ago=9 * HEURE):
        note = "hold: fenêtre : sondes de staging (due %s)" % _utc(due)
        work.note(self.db, self.lot["id"], note, actor=actor)
        self.db.execute(
            "UPDATE work_item_events SET created_at = now() - make_interval(secs => %s)"
            " WHERE work_item_id = %s AND note = %s", (ago, self.lot["id"], note))

    def intake(self):
        return [a["level"] for a in self.slo() if a["step"] == "intake_started"]

    def test_window_not_due_stays_silent(self):
        self.age(10 * HEURE)
        self.window(time.time() + HEURE)
        self.now = time.time()
        self.assertEqual(self.intake(), [])

    def test_expired_window_resumes_from_its_deadline(self):
        self.age(10 * HEURE)
        due = time.time() - 30 * 60
        self.window(due)
        for offset, levels in ((0, []), (60 * 60, [1])):
            with self.subTest(offset=offset):
                self.now = time.time() + offset
                # mesuré depuis l'échéance, jamais depuis la création (10 h)
                self.assertEqual(self.intake(), levels)

    def test_ordinary_pilot_hold_still_holds_after_the_window(self):
        self.age(10 * HEURE)
        self.window(time.time() - 3 * HEURE)
        work.note(self.db, self.lot["id"], "hold: attente des gestes owner",
                  actor="human:autre")
        self.now = time.time()
        self.assertEqual(self.intake(), [])

    def test_expired_window_helper_reads_like_the_debit(self):
        now = time.time()
        past = {"p": {"pilot": True,
                      "note": "hold: fenêtre : x (due %s)" % _utc(now - 60)}}
        self.assertAlmostEqual(W.expired_window(past, now), now - 60, delta=60)
        future = {"p": {"pilot": True,
                        "note": "hold: fenêtre : x (due %s)" % _utc(now + 600)}}
        self.assertIsNone(W.expired_window(future, now))
        unreadable = {"p": {"pilot": True, "note": "hold: fenêtre : x (due bientôt)"}}
        self.assertIsNone(W.expired_window(unreadable, now))
        self.assertIsNone(W.expired_window({"a": dict(past["p"], pilot=False)}, now))


if __name__ == "__main__":
    unittest.main()
