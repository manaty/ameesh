# SPDX-License-Identifier: AGPL-3.0-only
"""#8051 : le préfiltre de tête du journal ne change aucun résultat.

`lot_hold_events` lit les notes hold/levée par lot. Le préfiltre
`note_head LIKE 'hold%'` (index 0606) ne fait que réduire les lignes lues :
le motif ARE ancré reste le seul juge. Ces témoins vérifient, sur les quatre
familles de motifs qui passent leurs têtes (`HoldPatterns`), que la lecture
préfiltrée rend exactement la lecture exacte — y compris les formes que le
préfiltre sur-approxime (blancs de tête, casse, `hold-up:` ordinaire).
"""
from __future__ import annotations

import json
from pathlib import Path

from ameesh import packet_guard, packet_unit_red, planned_waits, prod_gate, storage, work
from ameesh.storage.postgres.progress import NOTE_HEAD_SQL

from .support import PgTestCase

NOTES = (
    "hold: pilote",
    "Hold: casse",
    " \thold: blancs de tête",
    "unhold: levée",
    "hold lifted: levée",
    "prod-gate: attente de porte",
    "prod-gate lifted: porte levée",
    "packet-exception incident sha=" + "a" * 40,
    "packet-unit-red: source retirée",
    "hold-up: note ordinaire",
    "basket line: pas une retenue",
    "",
)


class NoteHeadIndexTest(PgTestCase):
    def setUp(self):
        self.item = int(work.add(self.db, title="note head", app="acme")["id"])
        for note in NOTES:
            self.db.execute(
                "INSERT INTO work_item_events(work_item_id, state, note, actor)"
                " VALUES (%s, 'build', %s, 'test')", (self.item, note))

    def test_prefilter_keeps_the_exact_result(self):
        st = storage.of(self.db)
        for patterns in (planned_waits.HOLD_NOTE_PATTERNS,
                         prod_gate.NOTE_PATTERNS,
                         packet_guard.NOTE_PATTERNS,
                         packet_unit_red.NOTE_PATTERNS):
            with self.subTest(patterns=patterns):
                filtered = st.progress.lot_hold_events([self.item], patterns)
                exact = st.progress.lot_hold_events([self.item], tuple(patterns))
                self.assertTrue(filtered)
                self.assertEqual([e["id"] for e in filtered],
                                 [e["id"] for e in exact])

    def test_index_present_and_migration_matches_the_query_expression(self):
        names = {row["indexname"] for row in self.db.query(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = current_schema()"
            "   AND tablename = 'work_item_events'")}
        self.assertIn("work_item_events_item_note_head_idx", names)
        # L'expression du préfiltre et celle de l'index doivent rester le même
        # arbre SQL : sinon l'index cesse d'être servi (repli exact, jamais un
        # faux négatif). Un changement de HOLD_NOTE_WHITESPACE rougit ici.
        migration = (Path(__file__).resolve().parents[1]
                     / "src/ameesh/migrations/0606_work_events_note_head_idx.sql")
        text = " ".join(migration.read_text(encoding="utf-8").split())
        self.assertIn(" ".join(NOTE_HEAD_SQL.split()), text)

    def test_expression_is_indexable_and_used_by_the_planner(self):
        # Même arbre que l'index 0606, sur une table temporaire SANS autre
        # index : si l'expression n'était pas indexable à l'identique, le plan
        # ne pourrait pas nommer cet index (seqscan coupé).
        with self.db.transaction() as tx:
            tx.execute("CREATE TEMP TABLE note_head_probe(note text) ON COMMIT DROP")
            tx.execute("INSERT INTO note_head_probe(note)"
                       " VALUES ('hold: x'), ('basket line: y')")
            tx.execute("CREATE INDEX note_head_probe_idx ON note_head_probe ("
                       + NOTE_HEAD_SQL + " text_pattern_ops)")
            tx.execute("SET LOCAL enable_seqscan = off")
            plan = tx.query(
                "EXPLAIN (FORMAT JSON) SELECT 1 FROM note_head_probe"
                " WHERE " + NOTE_HEAD_SQL + " LIKE 'hold%'")
        self.assertIn("note_head_probe_idx", json.dumps(plan, default=str))
