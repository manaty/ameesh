# SPDX-License-Identifier: AGPL-3.0-only
"""#8283 — la sentinelle roadmap derrière un préfixe de fenêtre, et
l'exclusion du débit par plages.

Sur #8082 le 25/11, le pilote notait `hold: fenêtre : roadmap: épuisée
nexlink …` (événements 207244, 207392) : la raison du hold est une fenêtre
d'observation (`planned_waits`, #4235). Les lecteurs n'acceptaient que
`[hold:] roadmap: épuisée`, la note était invisible, la dernière note lue
restait la réouverture de 12:03Z : sommeil jamais réarmé, 333 h-agent
comptées. Les trois formes (directe, sous `hold:`, sous `hold: fenêtre :`)
valent dans les deux lecteurs (parseurs du journal et SQL du débit).

Second défaut : le débit ne lisait que le verdict de l'instant ; une
réouverture ponctuelle annulait l'exclusion sur toute la fenêtre. Les heures
se calculent maintenant hors des plages où la route était épuisée.
"""
from __future__ import annotations

import unittest

from ameesh import debit, file_decoupage as fd, work

from .support import PgTestCase
from .test_3256_debit_capacity import H, T, agent, measure, split_lot

#: la forme réelle de l'événement 207244 (25/11 18:00Z) sur #8082.
WINDOW_NOTE = ("hold: fenêtre : roadmap: épuisée nexlink — rien à découper "
               "avant la décision owner [réveil programmé: 2026-11-26T00:00Z]")
FORMS = ("roadmap: épuisée nexlink",
         "hold: roadmap: épuisée nexlink — relecture après paquets",
         WINDOW_NOTE)


class SentinelFormsTest(unittest.TestCase):
    def test_the_three_forms_are_read_as_exhausted(self):
        for text in FORMS + ("Hold:  Fenêtre d'observation :  ROADMAP : épuisée nexlink",
                             "hold: window: roadmap: épuisée nexlink",
                             "hold: observation window : roadmap: épuisée nexlink"):
            with self.subTest(text=text):
                notes = [{"id": 2, "note": "roadmap: rouverte nexlink"},
                         {"id": 3, "note": text}]
                self.assertTrue(fd.ROADMAP_NOTE_RE.match(text))
                self.assertEqual(fd.roadmap_state(notes, "nexlink"),
                                 fd.EXHAUSTED_STATE)

    def test_a_window_reopening_is_read(self):
        notes = [{"id": 1, "note": WINDOW_NOTE},
                 {"id": 2, "note": "unhold: fenêtre : roadmap: rouverte nexlink"}]
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.REOPENED_STATE)

    def test_another_cause_before_the_sentinel_stays_ordinary(self):
        for text in ("hold: fenêtre : relecture, roadmap: épuisée nexlink",
                     "hold: fenêtre : roadmap toujours épuisée nexlink",
                     "fenêtre roadmap: épuisée nexlink"):
            with self.subTest(text=text):
                self.assertIsNone(fd.ROADMAP_NOTE_RE.match(text))
                self.assertIsNone(fd.roadmap_state([{"note": text}], "nexlink"))

    def test_the_debit_sql_filter_is_the_parser_filter(self):
        self.assertEqual(debit._SENTINEL_SQL_RE.replace("''", "'"),
                         "^" + fd.SENTINEL_WRAPPER + r"roadmap\s*:")

    def test_the_split_lot_body_documents_the_window_form(self):
        item = fd.Measure(project="nexlink", wakeable=["a", "b"], idle=["a"])
        body = fd.body_of("nexlink", item, T - H, T, [])
        self.assertIn("hold: fenêtre : roadmap: épuisée <projet>", body)


class TransitionsTest(unittest.TestCase):
    def test_repeated_notes_are_not_transitions(self):
        notes = [{"id": 1, "note": "roadmap: épuisée nexlink", "at_ts": 10.0},
                 {"id": 2, "note": WINDOW_NOTE, "at_ts": 20.0},
                 {"id": 3, "note": "roadmap: rouverte nexlink", "at_ts": 30.0},
                 {"id": 4, "note": WINDOW_NOTE, "at_ts": 40.0},
                 {"id": 5, "note": "roadmap: épuisée ameesh", "at_ts": 50.0}]
        self.assertEqual(fd.roadmap_transitions(notes, "nexlink"),
                         [(fd.EXHAUSTED_STATE, 10.0), (fd.REOPENED_STATE, 30.0),
                          (fd.EXHAUSTED_STATE, 40.0)])


def note(text, at, eid):
    return {"id": eid, "work_item_id": 90, "note": text, "at_ts": at}


class IntervalCapacityTest(unittest.TestCase):
    """Fenêtre de 6 h, un agent au repos sans tour : 6 h imputables sans
    épuisement ; les plages d'épuisement en sont retirées."""

    def run_notes(self, notes, **extra):
        cap = measure([agent("worker")], roadmap_notes=notes,
                      open_lots=[split_lot()], **extra)
        return cap, cap["projects"][0]

    def test_a_window_sentinel_after_a_reopening_excludes_its_range(self):
        # épuisée (avant la fenêtre), rouverte à T−4 h, épuisée sous fenêtre à
        # T−1 h : seules les 3 h de route ouverte sont imputées.
        notes = [note("roadmap: épuisée ameesh", T - 10 * H, 1),
                 note("unhold: roadmap: rouverte ameesh", T - 4 * H, 2),
                 note("hold: fenêtre : roadmap: épuisée ameesh", T - H, 3)]
        cap, row = self.run_notes(notes)
        self.assertEqual(row["roadmap_exhausted"]["lot"], 90)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 3.0)
        self.assertEqual(row["idle_agent_hours"], 3.0)
        self.assertEqual(row["roadmap_exhausted_s"], 3 * H)
        self.assertNotIn("idle_agent_hours_excluded", row)

    def test_a_punctual_reopening_no_longer_lifts_the_whole_window(self):
        # avant #8283 : verdict « rouverte » à l'instant, 6 h imputées.
        notes = [note("roadmap: épuisée ameesh", T - 10 * H, 1),
                 note("roadmap: rouverte ameesh", T - H, 2)]
        cap, row = self.run_notes(notes)
        self.assertIsNone(row["roadmap_exhausted"])
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 1.0)
        self.assertEqual(row["roadmap_exhausted_s"], 5 * H)

    def test_an_exhaustion_alone_keeps_the_whole_window_excluded(self):
        # #7249/#7849 : une note d'épuisement seule exclut toute la fenêtre.
        cap, row = self.run_notes([note(WINDOW_NOTE.replace("nexlink", "ameesh"),
                                        T - H, 1)])
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 0)
        self.assertEqual(row["idle_agent_hours_excluded"], "feuille de route épuisée")

    def test_a_dated_startable_lot_still_ends_the_last_range(self):
        notes = [note("roadmap: épuisée ameesh", T - 10 * H, 1),
                 note("roadmap: rouverte ameesh", T - 5 * H, 2),
                 note("hold: fenêtre : roadmap: épuisée ameesh", T - 3 * H, 3)]
        cap, _row = self.run_notes(
            notes, startable=[{"id": 12, "app": "ameesh", "created_ts": T - H}])
        # 2 h rouvertes (T−5→T−3) + 1 h depuis le lot démarrable
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 3.0)

    def test_a_turn_across_a_bound_is_cut_there(self):
        notes = [note("roadmap: épuisée ameesh", T - 10 * H, 1),
                 note("roadmap: rouverte ameesh", T - 4 * H, 2),
                 note(WINDOW_NOTE.replace("nexlink", "ameesh"), T - 2 * H, 3)]
        turns = [{"agent": "worker", "work_item_id": 7, "at_ts": T - 3 * H,
                  "end_ts": T - 1.5 * H}]
        cap, _row = self.run_notes(notes, turns=turns)
        # route ouverte T−4→T−2, tour dès T−3 : 1 h au repos.
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 1.0)


class WindowSentinelPgTest(PgTestCase):
    def split(self):
        return int(work.add(self.db, title="Découper la feuille de route de nexlink",
                            app="nexlink", source="file-decoupage:nexlink",
                            actor="pilote")["id"])

    def test_the_journal_and_the_debit_sql_read_the_window_sentinel(self):
        split = self.split()
        texts = ["roadmap: rouverte nexlink", WINDOW_NOTE,
                 "hold: fenêtre : relecture, roadmap: épuisée nexlink"]
        for text in texts:
            work.note(self.db, split, text, actor="pilote")
        notes = fd.roadmap_notes(self.db, split)
        sql = [str(row["note"]) for row in debit._rows(
            self.db, debit._ROADMAP_SQL, (0.0,))]
        self.assertEqual([str(row["note"]) for row in notes], texts[:2])
        self.assertEqual(sql, texts[:2])
        # le sommeil se réarme : la dernière note lue est l'épuisement
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.EXHAUSTED_STATE)

    def test_a_window_sentinel_is_a_split_move(self):
        split = self.split()
        self.db.execute("UPDATE work_items SET assignee = 'pilote' WHERE id = %s",
                        (split,))
        work.note(self.db, split, WINDOW_NOTE, actor="pilote")
        rows = debit._rows(self.db, debit._SPLIT_MOVES_SQL, (str(split),))
        self.assertIsNotNone(rows[0]["moved_ts"])


if __name__ == "__main__":
    unittest.main()
