# SPDX-License-Identifier: AGPL-3.0-only
"""#3921 : la disponibilité d'élection sur `host_presence`, bornée.

Constat (pg_stat_statements, 27/10) : `role_standings_pg.samples` rendait
~8 400 lignes par appel (7 jours de relevés d'un hôte, ~140 000) pour que
`election.availability` somme la couverture en Python. La couverture est
désormais sommée par la base sur les tranches de 120 s de `host_presence`
(migration 0496, tenue par déclencheurs) : même résultat que la somme relevé
par relevé, et le nombre de lignes de `host_resources` lues ne dépend plus de
la fenêtre.
"""
from __future__ import annotations

import unittest

from ameesh import election, host_lifecycle, role_standings_pg as pg

from .support import PgTestCase

NOW = 1_700_000_000.0


def reference(times: list[float], now: float, cover: float) -> float:
    """The coverage as `election.availability` summed it before #3921."""
    covered = 0.0
    for index, ts in enumerate(times):
        end = min(now, ts + cover)
        if index + 1 < len(times):
            end = min(end, times[index + 1])
        if end > ts:
            covered += end - ts
    return covered


class HostPresenceTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        self.db.execute("ALTER TABLE host_resources SET (autovacuum_enabled = off)")
        self.addCleanup(self.db.execute,
                        "ALTER TABLE host_resources RESET (autovacuum_enabled)")

    def series(self, host: str, start: float, count: int, step: float) -> None:
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at)"
            " SELECT %s, to_timestamp(%s + g * %s) FROM generate_series(0, %s) g",
            (host, start, step, count - 1))

    def times(self, host: str, since: float) -> list[float]:
        return pg.samples(self.db, host, since)

    def assert_matches(self, host: str, *, now: float = NOW, cover: float = 180.0) -> dict:
        since = now - pg.WINDOW_S
        times = self.times(host, since)
        got = pg.coverage(self.db, host, since, now=now, cover_s=cover)
        self.assertEqual(got["samples"], len(times))
        self.assertAlmostEqual(got["covered_s"], reference(times, now, cover), delta=1e-3)
        if times:
            self.assertAlmostEqual(got["first_ts"], times[0], delta=1e-6)
            self.assertAlmostEqual(got["last_ts"], times[-1], delta=1e-6)
        else:
            self.assertIsNone(got["last_ts"])
        return got

    def measured(self, call) -> tuple[int, int]:
        """(rows returned to the client, rows of `host_resources` read from
        the heap) by `call`, the latter from the transaction's own
        statistics. Autovacuum is off on the bench table (`setUp`): no page
        is all-visible, so an index-only scan still counts its rows."""
        sql = ("SELECT (seq_tup_read + coalesce(idx_tup_fetch, 0))::bigint AS n"
               "  FROM pg_stat_xact_user_tables WHERE relid = 'host_resources'::regclass")
        returned = []

        class Counting:
            def __init__(self, tx):
                self.tx = tx

            def query(self, *args, **kwargs):
                rows = self.tx.query(*args, **kwargs)
                returned.append(len(rows))
                return rows

            def __getattr__(self, name):
                return getattr(self.tx, name)

        with self.db.transaction() as tx:
            before = int(tx.query(sql)[0]["n"])
            call(Counting(tx))
            return sum(returned), int(tx.query(sql)[0]["n"]) - before

    # -- the bucket width ---------------------------------------------------
    def test_bucket_never_wider_than_the_freshness_floor(self):
        # the coverage is exact on buckets only for a cover at least this wide
        self.assertLessEqual(pg.PRESENCE_BUCKET_S, host_lifecycle.MIN_STALENESS_S)

    # -- same result as the per-reading sum --------------------------------
    def test_dense_week_with_a_hole_and_partial_ends(self):
        # start off the bucket grid; a 47-minute hole; a cadence of 37 s
        self.series("vm", NOW - pg.WINDOW_S - 53.0, 9000, 37.0)
        self.series("vm", NOW - 4 * 86400.0 + 11.0, 4000, 37.0)
        self.series("vm", NOW - 86400.0 - 2820.0, 2000, 41.0)
        self.series("vm", NOW - 9.0, 1, 1.0)
        got = self.assert_matches("vm")
        self.assertGreater(got["samples"], 10000)

    def test_cover_narrower_than_a_bucket_falls_back_to_readings(self):
        self.series("vm", NOW - 7200.0, 120, 61.0)
        self.assert_matches("vm", cover=30.0)
        self.assert_matches("vm", cover=600.0)

    def test_evaluation_instant_before_the_last_readings(self):
        # `now` given by the caller may precede readings still <= now()
        self.series("vm", NOW - 3600.0, 200, 30.0)
        self.assert_matches("vm", now=NOW - 1800.0 + 7.0)

    def test_duplicates_and_future_readings(self):
        self.series("vm", NOW - 600.0, 10, 20.0)
        self.series("vm", NOW - 600.0, 10, 20.0)
        self.db.execute("INSERT INTO host_resources (host, sampled_at)"
                        " VALUES ('vm', now() + interval '1 hour')")
        got = self.assert_matches("vm")
        self.assertEqual(got["samples"], 20)

    def test_no_reading(self):
        got = self.assert_matches("vide")
        self.assertEqual((got["samples"], got["covered_s"]), (0, 0.0))

    # -- the triggers keep the buckets equal to the history ----------------
    def test_buckets_follow_updates_and_deletes(self):
        self.series("vm", NOW - 86400.0, 2000, 30.0)
        self.db.execute("DELETE FROM host_resources WHERE host = 'vm'"
                        " AND sampled_at BETWEEN to_timestamp(%s) AND to_timestamp(%s)",
                        (NOW - 50000.0, NOW - 40000.0))
        self.assert_matches("vm")
        self.db.execute("UPDATE host_resources SET sampled_at = sampled_at - interval '7 minutes'"
                        " WHERE host = 'vm' AND sampled_at > to_timestamp(%s)", (NOW - 9000.0,))
        self.assert_matches("vm")
        rows = self.db.query(
            "SELECT count(*)::int AS n FROM host_presence p WHERE host = 'vm'"
            " AND readings <> (SELECT count(*) FROM host_resources r WHERE r.host = p.host"
            "                  AND r.sampled_at >= p.bucket_start"
            "                  AND r.sampled_at < p.bucket_start + interval '120 seconds')")
        self.assertEqual(rows[0]["n"], 0)
        self.db.execute("DELETE FROM host_resources WHERE host = 'vm'")
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM host_presence WHERE host = 'vm'")[0]["n"], 0)

    # -- bounded ------------------------------------------------------------
    def test_availability_reads_a_bounded_number_of_readings(self):
        # a week at one reading every 10 s: ~60 000 readings in the window.
        # Before #3921, `availability` brought them all back (`samples`).
        now = pg.now_ts(self.db)
        self.series("vm", now - pg.WINDOW_S + 5.0, 60480, 10.0)
        returned, read = self.measured(lambda tx: election.availability(tx, "vm", now=now))
        # one row per query, not one per reading
        self.assertLess(returned, 10)
        # the partial buckets at both ends and the hour of the cadence (360)
        self.assertLess(read, 1000)
        av = election.availability(self.db, "vm", now=now)
        self.assertEqual(av["samples"], 60480)
        self.assertGreater(av["availability"], 99.9)


if __name__ == "__main__":
    unittest.main()
