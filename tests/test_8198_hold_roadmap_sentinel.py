# SPDX-License-Identifier: AGPL-3.0-only
"""#8198 — une sentinelle roadmap écrite dans la raison d'un hold est lue.

Le pilote note sa raison sous `hold:` (et sa levée sous `unhold:`) :
`hold: roadmap: épuisée nexlink — …`. Le préfixe de retenue ne doit pas
cacher la sentinelle. Témoins : la fonction partagée
`file_decoupage.roadmap_state`, le verdict du débit `_roadmap_verdicts`, la
lecture du journal (`work.events_noted` avec `wrapped`, donc
`roadmap_notes`) et la requête `_ROADMAP_SQL` du débit. Le quatrième lecteur,
la règle 1 (`lot_sessions.actionable`, #4901), est couvert par
`tests/test_l165c_sessions_par_lot.py`.

Une note dont le corps ne commence pas par `roadmap:` reste ignorée : le
préfixe de retenue est facultatif, il ne déplace pas la sentinelle.
"""
from __future__ import annotations

import unittest

from ameesh import debit, file_decoupage as fd, work

from .support import PgTestCase
from .test_3256_debit_capacity import H, T, split_lot

SPLIT_NEXLINK = "file-decoupage:nexlink"

#: la forme réelle de la note du 24/11 00:25Z sur #8082.
HOLD_NOTE = ("hold: roadmap: épuisée nexlink — relecture après paquets 23h "
             "le 24/11 00:3xZ, demandée par auditeur #232617.")


def note(text, ident=8082, source=SPLIT_NEXLINK, at=T - H, eid=1):
    return {"id": eid, "work_item_id": ident, "source": source,
            "note": text, "at_ts": at}


class RoadmapStateTest(unittest.TestCase):
    def test_a_hold_sentinel_is_read_as_exhausted(self):
        notes = [{"id": 1, "note": HOLD_NOTE}]
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.EXHAUSTED_STATE)
        self.assertTrue(fd.exhausted(notes, "nexlink"))
        self.assertEqual(fd.exhausted_since(notes, "nexlink"), 0.0)

    def test_an_unhold_reopening_is_read_as_reopened(self):
        notes = [{"id": 1, "note": "unhold: roadmap: rouverte nexlink — #8147 "
                                    "et #8148 (décision owner)"}]
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.REOPENED_STATE)
        self.assertFalse(fd.exhausted(notes, "nexlink"))

    def test_the_wrapper_is_optional_and_its_spacing_is_free(self):
        for text in ("roadmap: épuisée nexlink",
                     "hold: roadmap: épuisée nexlink",
                     "hold:roadmap:épuisée nexlink",
                     "  Hold:  ROADMAP : ÉPUISÉE nexlink"):
            with self.subTest(text=text):
                self.assertEqual(
                    fd.roadmap_state([{"id": 1, "note": text}], "nexlink"),
                    fd.EXHAUSTED_STATE)

    def test_a_sentinel_after_the_wrapper_must_start_the_reason(self):
        for text in ("hold: roadmap toujours épuisée nexlink",
                     "hold: point de 06:00 — roadmap: épuisée nexlink",
                     "hold: autre sujet"):
            with self.subTest(text=text):
                self.assertIsNone(fd.roadmap_state([{"id": 1, "note": text}],
                                                   "nexlink"))

    def test_the_last_note_still_decides(self):
        notes = [{"id": 1, "note": HOLD_NOTE},
                 {"id": 2, "note": "roadmap: rouverte nexlink"}]
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.REOPENED_STATE)
        notes = [{"id": 1, "note": "unhold: roadmap: rouverte nexlink"},
                 {"id": 2, "note": "hold: roadmap: épuisée nexlink"}]
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.EXHAUSTED_STATE)


class DebitVerdictTest(unittest.TestCase):
    def test_the_debit_verdict_reads_the_hold_sentinel(self):
        notes = [note(HOLD_NOTE)]
        self.assertEqual(
            debit._roadmap_verdicts(notes, [split_lot(8082, "nexlink")]),
            {"nexlink": {"lot": 8082, "at_ts": T - H}})

    def test_a_hold_note_without_a_sentinel_is_not_a_verdict(self):
        notes = [note("hold: roadmap toujours épuisée nexlink")]
        self.assertEqual(
            debit._roadmap_verdicts(notes, [split_lot(8082, "nexlink")]), {})


class JournalReaderTest(PgTestCase):
    """Les deux lecteurs de la base : le journal du lot et la requête du débit."""

    def split(self):
        row = work.add(self.db, title="Découper la feuille de route de nexlink",
                       app="nexlink", source=SPLIT_NEXLINK, actor="pilote")
        return int(row["id"])

    def test_roadmap_notes_reads_the_hold_sentinel(self):
        split = self.split()
        work.note(self.db, split, HOLD_NOTE, actor="pilote")
        notes = fd.roadmap_notes(self.db, split)
        self.assertEqual([str(row["note"]) for row in notes], [HOLD_NOTE])
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.EXHAUSTED_STATE)

    def test_a_hold_note_that_is_not_a_sentinel_stays_ordinary(self):
        split = self.split()
        work.note(self.db, split,
                  "hold: roadmap toujours épuisée nexlink — point de 06:00",
                  actor="pilote")
        self.assertEqual(fd.roadmap_notes(self.db, split), [])

    def test_the_debit_sql_selects_the_hold_sentinel(self):
        split = self.split()
        work.note(self.db, split, HOLD_NOTE, actor="pilote")
        rows = debit._rows(self.db, debit._ROADMAP_SQL, (0.0,))
        self.assertEqual([str(row["note"]) for row in rows], [HOLD_NOTE])
        self.assertEqual(
            debit._roadmap_verdicts(rows, [split_lot(split, "nexlink")]),
            {"nexlink": {"lot": split, "at_ts": rows[0]["at_ts"]}})


if __name__ == "__main__":
    unittest.main()
