# SPDX-License-Identifier: AGPL-3.0-only
"""#3922: indexed group walks preserve DISTINCT ON's history semantics."""
from ameesh import storage
from .test_1132_jauges_perimees import _JaugesBase


class GaugeSeeksTest(_JaugesBase):
    def test_matches_distinct_on_with_null_empty_and_expired_groups(self):
        # Several harnesses/keys, NULL versus empty accounts, old groups,
        # replayed production, NULL production and exact timestamp ties.
        self.db.execute("""
            INSERT INTO quota_gauge_readings
                (harness, account, gauge_key, used, window_s,
                 observed_at, produced_at, source)
            SELECT h, a, g, n::float8, 3600,
                   now() - make_interval(secs => CASE WHEN n=1 THEN 7200 ELSE 0 END),
                   CASE WHEN n=3 THEN NULL ELSE now() - make_interval(secs => n*60) END,
                   src
            FROM unnest(ARRAY['claude','codex']) h
            CROSS JOIN unnest(ARRAY[NULL::text,'','a','z']) a
            CROSS JOIN unnest(ARRAY['five_hour','seven_day']) g
            CROSS JOIN generate_series(1,4) n
            CROSS JOIN unnest(ARRAY[NULL::text,'','source-a','source-z']) src
        """)
        self.db.execute("""
            INSERT INTO quota_gauge_readings
                (harness, account, gauge_key, used, window_s, observed_at, source)
            VALUES ('ancient', 'old-account', 'old-key', .9, 3600,
                    now() - interval '30 days', 'old-source')
        """)
        ops = storage.of(self.db).operations
        fields = ("harness, gauge_key AS key, used, "
                  "extract(epoch from resets_at)::float8 AS resets_at_ts, window_s, "
                  "extract(epoch from observed_at)::float8 AS observed_ts, "
                  "extract(epoch from produced_at)::float8 AS produced_ts, "
                  "source, account, overage_capable, overage_in_use")
        for by_source in (False, True):
            groups = "harness, account, gauge_key" + (", source" if by_source else "")
            for since in (3600, 86400, -1):
                with self.subTest(by_source=by_source, since=since):
                    expected = self.db.query(
                        "SELECT DISTINCT ON (" + groups + ") " + fields +
                        " FROM quota_gauge_readings WHERE observed_at >= now() - make_interval(secs => %s)"
                        " ORDER BY " + groups + ", produced_at DESC NULLS LAST, observed_at DESC, id DESC",
                        (float(since),))
                    self.assertEqual(ops.latest_gauges(since_s=since, by_source=by_source), expected)
        for harness, account in [('claude',''), ('codex','a'), ('claude','missing'), ('missing','a')]:
            expected = self.db.query(
                "SELECT DISTINCT ON (gauge_key) " + fields +
                " FROM quota_gauge_readings WHERE harness=%s AND account=%s"
                " ORDER BY gauge_key, produced_at DESC NULLS LAST, observed_at DESC, id DESC",
                (harness, account))
            self.assertEqual(ops.gauge_latest(harness=harness, account=account), expected)
