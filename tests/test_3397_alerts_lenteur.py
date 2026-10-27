# SPDX-License-Identifier: AGPL-3.0-only
"""#3397 — `GET /api/v1/alerts` répondait en 93–102 s.

Trois causes mesurées sur la base de staging (24/10/2026) :

* `merge_queue.review_gate_for_item` lisait toute la plage gelée (Git, auteurs,
  survie des lignes) pour un lot sensible dont les `ok` étaient déjà trop peu
  nombreux : 39–62 s à chaque passe d'alertes, pour rendre un refus que le
  compte des verdicts suffit à établir ;
* `operations.listing()` balayait `turn_costs_time_idx` jusqu'au dernier tour
  de chaque agent (~2,2 Go de buffers par appel) faute de l'index
  `(agent, recorded_at, id)` ;
* la route d'alertes recalculait tout à chaque événement SSE de l'écran
  (`ReadView`), sans borne ni partage.

Ces témoins tiennent les trois corrections : la porte se ferme sans lire la
plage, l'index sert le `LIMIT 1`, et la route partage un instantané de TTL
court en une seule passe.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import alert_ack, db as db_mod, merge_queue, serve

from .support import PgTestCase


HEAD = "a" * 40


def _mark(actor: str, *, at: float = 1.0, verdict: str = "ok",
          sha: str = HEAD) -> dict:
    return {"id": int(at * 1000), "kind": "verdict", "actor": actor,
            "verdict": verdict, "sha": sha, "at_ts": at}


class _QueryDb:
    """A connection marker: the gate reads it; these tests never reach SQL."""

    def query(self, *_args, **_kwargs):
        return []


class ReviewGateShortCircuitTest(unittest.TestCase):
    """Un compte de `ok` insuffisant ferme la porte sans lire les auteurs."""

    def gate(self, marks, *, db=None, **kwargs):
        return merge_queue.review_gate(
            db, marks, HEAD,
            klass={"class": "normal",
                   "files": [{"path": "src/ameesh/merge_queue.py"}]},
            required=2, item={"assignee": "author"},
            family_of=lambda name: {"r1": "claude", "r2": "codex"}.get(name, ""),
            **kwargs)

    def test_one_ok_where_two_are_required_never_reads_the_frozen_range(self):
        with mock.patch.object(merge_queue, "_attribution_on_freeze",
                               side_effect=AssertionError("frozen range read")):
            out = self.gate([_mark("r1")], db=_QueryDb())
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 2)
        self.assertIn("no review verdict ok on the freeze", out["why"])
        # #3400 : l'ok reste lisible, mais il n'est pas *accepté* : sans la
        # lecture des auteurs, son indépendance n'est pas prouvée.
        self.assertEqual(out["accepted"], [])
        self.assertEqual([entry["actor"] for entry in out["accepted_unfiltered"]],
                         ["r1"])
        self.assertEqual(out["accepted_unfiltered"][0]["family"], "claude")

    def test_the_author_s_own_ok_is_never_accepted_on_the_short_circuit(self):
        """#3400 : l'auteur de la tête ne compte pas, même quand la porte ferme
        sur le seul compte ; l'ancien `accepted` le listait sans filtre."""
        with mock.patch.object(merge_queue, "_attribution_on_freeze",
                               side_effect=AssertionError("frozen range read")):
            out = self.gate([_mark("author")], db=_QueryDb())
        self.assertFalse(out["ok"])
        self.assertEqual(out["accepted"], [])
        self.assertEqual([entry["actor"] for entry in out["accepted_unfiltered"]],
                         ["author"])

    def test_enough_ok_verdicts_still_read_the_range(self):
        """Au compte requis, la porte garde sa lecture complète : on ne
        suppose jamais l'indépendance des auteurs."""
        with mock.patch.object(merge_queue, "_attribution_on_freeze",
                               side_effect=RuntimeError("range read")) as read:
            out = self.gate([_mark("r1", at=1), _mark("r2", at=2)], db=_QueryDb())
        self.assertTrue(read.called)
        self.assertFalse(out["ok"])
        self.assertIn("frozen range cannot be read", out["why"])
        self.assertEqual(out["accepted_unfiltered"], [])


class AlertsRouteCacheTest(unittest.TestCase):
    """Une rafale d'événements partage un calcul ; le TTL le périme."""

    def setUp(self):
        self.ctx = serve.Context(cfg=object(), human="human:owner")
        self.computations = 0
        self.now = 1_000.0

    def _batched(self, db, fn):
        self.computations += 1
        return [{"schema": "ameesh-alerts/1", "type": "stale_lot",
                 "agent": "a1", "lot": 1, "since": 1.0}]

    def _call(self):
        with mock.patch.object(serve.db_mod, "batched", side_effect=self._batched), \
                mock.patch.object(alert_ack, "annotate",
                                  side_effect=lambda cfg, db, rows, **kw: rows), \
                mock.patch.object(serve.time, "monotonic",
                                  side_effect=lambda: self.now):
            return serve._alerts(self.ctx, object(), {}, {})

    def test_a_burst_shares_one_computation_and_the_ttl_expires(self):
        first = self._call()
        second = self._call()
        self.assertEqual(first, second)
        self.assertEqual(self.computations, 1)
        self.now += serve.ALERTS_TTL_S - 0.5
        self._call()
        self.assertEqual(self.computations, 1, "still inside the TTL")
        self.now += 1.0
        self._call()
        self.assertEqual(self.computations, 2, "the snapshot is stale")

    def test_each_context_has_its_own_snapshot(self):
        self._call()
        other = serve.Context(cfg=object(), human="human:other")
        with mock.patch.object(serve.db_mod, "batched", side_effect=self._batched), \
                mock.patch.object(alert_ack, "annotate",
                                  side_effect=lambda cfg, db, rows, **kw: rows), \
                mock.patch.object(serve.time, "monotonic",
                                  side_effect=lambda: self.now):
            serve._alerts(other, object(), {}, {})
        self.assertEqual(self.computations, 2)


class TurnCostsIndexTest(PgTestCase):
    """L'index de #3397 existe et sert le dernier tour d'un agent."""

    INDEX = "turn_costs_agent_recorded_idx"

    def test_the_index_is_migrated(self):
        rows = self.db.query(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s "
            "AND tablename = 'turn_costs' AND indexname = %s",
            (self.schema, self.INDEX))
        self.assertEqual(len(rows), 1, rows)
        definition = rows[0]["indexdef"].lower()
        self.assertIn("(agent, recorded_at desc, id desc)", definition)

    def _fill_turn_costs(self):
        """#3941 : remplit la table sans le coût du trigger d'évènements.

        Chaque insertion appelait `ameesh_events_cost` → `ameesh_event` ; sous
        la charge de la CI, les 20 000 lignes dépassaient le `statement_timeout`
        du banc (`canceling statement due to statement timeout`) alors que le
        test ne lit que le plan. Le trigger est coupé le temps du remplissage
        (schéma du test seulement), puis rétabli même si l'insertion échoue.
        """
        self.db.execute(
            "ALTER TABLE turn_costs DISABLE TRIGGER ameesh_events_cost")
        try:
            self.db.execute(
                "INSERT INTO turn_costs (agent, harness, recorded_at) "
                "SELECT 'a' || mod(g, 5), 'deepseek', "
                "now() - (g || ' minutes')::interval "
                "FROM generate_series(1, 20000) g")
        finally:
            self.db.execute(
                "ALTER TABLE turn_costs ENABLE TRIGGER ameesh_events_cost")

    def test_the_fill_does_not_fire_the_cost_trigger(self):
        """#3941 : témoin — le banc n'écrit plus 20 000 `cost.recorded`."""
        self.db.execute("TRUNCATE turn_costs")
        before = self.db.query(
            "SELECT count(*) AS n FROM ameesh_events")[0]["n"]
        self._fill_turn_costs()
        after = self.db.query(
            "SELECT count(*) AS n FROM ameesh_events")[0]["n"]
        self.assertEqual(after, before)

    def test_the_last_turn_lookup_uses_it(self):
        # The psql driver wraps every read in a JSON aggregate; `EXPLAIN`
        # cannot be wrapped, so the plan is read on the psycopg driver (the
        # default CI driver). The index itself is checked above under both.
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        self.db.execute("TRUNCATE turn_costs")
        self._fill_turn_costs()
        self.db.execute("ANALYZE turn_costs")
        plan = self.db.query(
            "EXPLAIN (FORMAT JSON) SELECT id FROM turn_costs WHERE agent = %s "
            "ORDER BY recorded_at DESC, id DESC LIMIT 1", ("a0",))
        text = json.dumps(plan)
        self.assertIn(self.INDEX, text)
        self.assertNotIn("Seq Scan", text)
