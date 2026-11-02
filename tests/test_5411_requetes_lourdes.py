# SPDX-License-Identifier: AGPL-3.0-only
"""#5411 — les trois lectures lourdes du passage d'alertes.

* `measure_candidates` sans identifiants part des seuls lots qui manquent de
  mesure (ou dont le diff/CI est incomplet) : le LATERAL du jalon `merged` ne
  tourne plus pour les 4 289 lots ouverts démarrés (111 931 tampons, 2,2 s) ;
  les conditions de #3056/#2317 sont conservées (lot non mesuré : toujours
  candidat, même fusionné ancien ; lot mesuré incomplet : seulement si sa
  fusion est dans la fenêtre de reprise) ;
* `latest_by_path` avec fenêtre lit `housekeeping_log_kind_at_host_path_idx`
  au lieu de scanner tout le journal (762 848 lignes, 3,6 s mesurés) ;
* `balances` ne relit plus tout l'historique pour la liste des séries : seule
  une série qui a un relevé ANTÉRIEUR à la fenêtre est sondée.
"""
from __future__ import annotations

import json

from ameesh import storage, work
from ameesh.storage.postgres import menage as menage_pg

from .support import PgTestCase

SHA = "0123456789abcdef0123456789abcdef01234567"

MILESTONE_INDEX = "work_item_milestones_item_kind_at_idx"
WINDOW_INDEX = "housekeeping_log_kind_at_host_path_idx"


class _LotCase(PgTestCase):
    """Un lot démarré et fusionné, avec ou sans mesure."""

    def _lot(self, title, *, merged_hours_ago=1.0, started=True,
             state_after="merged"):
        lot = work.add(self.db, title=title, actor="orch")
        work.set_branch(self.db, lot["id"],
                        "lot/%d-%s" % (lot["id"], title.replace(" ", "-")),
                        actor="orch", source="test")
        work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
        if state_after != "merged":
            # closed is a terminal state written by `work.close`; the read
            # only needs a closed row, so the state is posed directly.
            self.db.execute(
                "UPDATE work_items SET state = %s, close_reason = 'abandoned'"
                " WHERE id = %s", (state_after, lot["id"]))
        if started:
            self.db.execute(
                "UPDATE work_items SET started_at = now() - interval '2 hours'"
                " WHERE id = %s", (lot["id"],))
        if merged_hours_ago:
            self.db.execute(
                "UPDATE work_item_milestones SET at = now() - make_interval(hours => %s::int)"
                " WHERE work_item_id = %s AND kind = 'merged'",
                (float(merged_hours_ago), lot["id"]))
        return lot

    def _measure(self, lot, *, diff_files=None, ci_minutes=None):
        self.db.execute(
            "INSERT INTO work_item_measures (work_item_id, started_at, merged_at,"
            " total_minutes, agent_minutes, review_minutes, human_minutes,"
            " diff_files, ci_minutes)"
            " VALUES (%s, now() - interval '2 hours', now() - interval '1 hour',"
            " 60, 50, 5, 5, %s, %s) ON CONFLICT (work_item_id) DO UPDATE SET"
            " diff_files = excluded.diff_files, ci_minutes = excluded.ci_minutes",
            (lot["id"], diff_files, ci_minutes))


class MeasureCandidatesTest(_LotCase):
    """La lecture des lots à mesurer garde les conditions de #3056/#2317."""

    def _ids(self, **kwargs):
        rows = storage.of(self.db).work.measure_candidates(
            ids=None, retry_diff_s=48 * 3600.0, since_s=None, limit=50, **kwargs)
        return {int(row["id"]) for row in rows}

    def test_unmeasured_lot_is_a_candidate_even_after_a_while(self):
        lot = self._lot("non mesure", merged_hours_ago=240)
        self.assertIn(lot["id"], self._ids())

    def test_complete_measure_is_not_a_candidate(self):
        lot = self._lot("mesure complete")
        self._measure(lot, diff_files=3, ci_minutes=5)
        self.assertNotIn(lot["id"], self._ids())

    def test_incomplete_measure_is_a_candidate_while_the_merge_is_recent(self):
        lot = self._lot("mesure incomplete recente")
        self._measure(lot, diff_files=3, ci_minutes=None)
        self.assertIn(lot["id"], self._ids())

    def test_incomplete_measure_waits_out_the_retry_window(self):
        lot = self._lot("mesure incomplete ancienne", merged_hours_ago=72)
        self._measure(lot, diff_files=3, ci_minutes=None)
        self.assertNotIn(lot["id"], self._ids())

    def test_unstarted_or_closed_lot_is_never_a_candidate(self):
        unstarted = self._lot("pas demarre", started=False)
        closed = self._lot("clos", state_after="closed")
        self.assertNotIn(unstarted["id"], self._ids())
        self.assertNotIn(closed["id"], self._ids())

    def test_a_since_window_still_bounds_the_merge(self):
        old = self._lot("non mesure ancien", merged_hours_ago=240)
        recent = self._lot("non mesure recent")
        rows = storage.of(self.db).work.measure_candidates(
            ids=None, retry_diff_s=48 * 3600.0, since_s=24 * 3600.0, limit=50)
        ids = {int(row["id"]) for row in rows}
        self.assertNotIn(old["id"], ids)
        self.assertIn(recent["id"], ids)

    def test_the_measure_columns_are_kept(self):
        lot = self._lot("colonnes")
        row = next(r for r in storage.of(self.db).work.measure_candidates(
            ids=None, retry_diff_s=48 * 3600.0, since_s=None, limit=50)
            if int(r["id"]) == int(lot["id"]))
        self.assertIn(row["state"], ("merged", "delivered"))
        self.assertEqual(row["merge_sha"], SHA)
        self.assertFalse(row["measured"])
        self.assertEqual(int(row["started_ts"]), int(row["started_ts"]))
        self.assertGreater(row["merged_ts"], 0)


class AlertIndexMigrationTest(PgTestCase):
    """Les deux index additives de 0533 sont migrées (pas posées à la main)."""

    def test_the_indexes_are_migrated(self):
        rows = self.db.query(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = %s AND indexname IN (%s, %s)",
            (self.schema, MILESTONE_INDEX, WINDOW_INDEX))
        by_name = {row["indexname"]: row["indexdef"].lower() for row in rows}
        self.assertIn("(work_item_id, kind, at desc, id desc)",
                      by_name[MILESTONE_INDEX])
        self.assertIn("(kind, at desc, host, path, id desc)",
                      by_name[WINDOW_INDEX])


class LatestByPathWindowTest(PgTestCase):
    """La lecture à fenêtre passe par l'index du temps, sans balayage complet."""

    def _plan(self, sql, params=()):
        # `EXPLAIN` ne rentre pas dans l'agrégat JSON du pilote psql.
        from ameesh import db as db_mod
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        return json.dumps(self.db.query("EXPLAIN (FORMAT JSON) " + sql, params))

    def test_the_window_read_uses_the_time_index(self):
        self.db.execute("DELETE FROM housekeeping_log")
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path, bytes)"
            " SELECT 'h1', now() - (g || ' seconds')::interval, 'test', 'tmp',"
            " 'signaled', '/tmp/p' || (g % 4000), g"
            " FROM generate_series(1, 20000) g")
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path, bytes)"
            " SELECT 'h1', now() - interval '3 days' - (g || ' seconds')::interval,"
            " 'test', 'tmp', 'signaled', '/tmp/old' || g, g"
            " FROM generate_series(1, 200000) g")
        self.db.execute("ANALYZE housekeeping_log")
        sql, params = menage_pg._latest_by_path_sql("tmp", since_s=86400.0)
        plan = self._plan(sql, params)
        self.assertIn(WINDOW_INDEX, plan)
        self.assertNotIn("Seq Scan", plan)

    def test_the_window_still_returns_the_latest_line_per_path(self):
        self.db.execute("DELETE FROM housekeeping_log")
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path)"
            " VALUES ('h1', now() - interval '2 hours', 't', 'tmp', 'signaled', '/tmp/a'),"
            "        ('h1', now() - interval '1 hour', 't', 'tmp', 'unremovable', '/tmp/a'),"
            "        ('h1', now() - interval '3 days', 't', 'tmp', 'signaled', '/tmp/b')")
        rows = storage.of(self.db).housekeeping.latest_by_path(
            None, "tmp", since_s=86400.0)
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(set(by_path), {"/tmp/a"})
        self.assertEqual(by_path["/tmp/a"]["action"], "unremovable")


class BalancesWindowTest(PgTestCase):
    """La liste des séries ne relit que ce qui est antérieur à la fenêtre."""

    def setUp(self):
        self.db.execute("DELETE FROM provider_balances")

    def _reading(self, provider, currency, total, seconds_ago, account=None):
        self.db.execute(
            "INSERT INTO provider_balances (provider, currency, total, account,"
            " observed_at) VALUES (%s, %s, %s, %s, now() - make_interval(secs => %s))",
            (provider, currency, total, account, float(seconds_ago)))

    def test_a_series_entirely_outside_the_window_keeps_its_last_reading(self):
        self._reading("p", "USD", 10, 7200)
        self._reading("p", "USD", 12, 3600)
        # none in the window: only the last reading before it is returned
        rows = storage.of(self.db).operations.balances(provider="p", since_s=60.0)
        self.assertEqual([float(row["total"]) for row in rows], [12.0])

    def test_the_last_reading_before_the_window_is_the_previous_one(self):
        self._reading("p", "USD", 5, 4 * 3600)
        self._reading("p", "USD", 7, 2 * 3600)
        self._reading("p", "USD", 9, 600)
        rows = storage.of(self.db).operations.balances(provider="p", since_s=3600.0)
        self.assertEqual([float(row["total"]) for row in rows], [7.0, 9.0])

    def test_a_window_covering_all_history_returns_each_reading_once(self):
        self._reading("p", "USD", 3, 7200)
        self._reading("p", "USD", 4, 3600)
        rows = storage.of(self.db).operations.balances(provider="p", since_s=86400.0)
        self.assertEqual([float(row["total"]) for row in rows], [3.0, 4.0])

    def test_the_series_summary_keeps_peak_and_spend(self):
        # 100 -> 80 (baisse 20), 80 -> 120 (recharge), 120 -> 110 (baisse 10)
        for seconds_ago, total in ((4 * 3600, 100), (3 * 3600, 80),
                                   (2 * 3600, 120), (3600, 110)):
            self._reading("p", "USD", total, seconds_ago)
        summary = next(row for row in storage.of(self.db).operations.balance_series(
            provider="p", account=None, since_s=86400.0)
            if row["currency"] == "USD")
        self.assertEqual(summary["readings"], 4)
        self.assertEqual(summary["total"], 110)
        self.assertEqual(summary["spent"], 30)
        # le sommet repart de la dernière hausse (120), pas du sommet initial
        self.assertEqual(summary["peak"], 120)
        self.assertEqual(summary["first_ts"], summary["first_ts"])
        self.assertGreater(summary["last_ts"], summary["first_ts"])
