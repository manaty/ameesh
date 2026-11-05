# SPDX-License-Identifier: AGPL-3.0-only
"""#5968 — multi-source fingerprints and the N-1 cursor barrier (#5946).

The new pass reads one source per environment and writes
``fingerprint_source_state`` keyed by ``(project, environment)``.  The
historical ``fingerprint_watch_state`` (one row per project, primary key
``project``) is **not modified**: an ameesh 1.39.0 process still in service
during the deployment keeps its exact upsert and stays the production cursor.
These witnesses replay the old process's SQL, then the new pass, in both
orders of a takeover.
"""
from __future__ import annotations

from ameesh import storage

from .support import PgTestCase


class SourceStateTest(PgTestCase):
    def setUp(self):
        self.db.execute("DELETE FROM fingerprint_source_state")
        self.db.execute("DELETE FROM fingerprint_watch_state")
        self.api = storage.of(self.db).fingerprints

    # -- the 1.39.0 code, verbatim: old table, ON CONFLICT (project) ---------

    def old_advance(self, project, url, cursor):
        self.db.execute(
            "INSERT INTO fingerprint_watch_state"
            "    (project, url, cursor, last_read_at, last_error, updated_at)"
            " VALUES (%s, %s, %s, now(), '', now())"
            " ON CONFLICT (project) DO UPDATE SET"
            "    url = EXCLUDED.url,"
            "    cursor = EXCLUDED.cursor,"
            "    last_read_at = EXCLUDED.last_read_at,"
            "    last_error = '',"
            "    updated_at = now()",
            (project, url, cursor))

    def old_fail(self, project, url, error):
        self.db.execute(
            "INSERT INTO fingerprint_watch_state"
            "    (project, url, cursor, last_read_at, last_error, updated_at)"
            " VALUES (%s, %s, NULL, NULL, %s, now())"
            " ON CONFLICT (project) DO UPDATE SET"
            "    url = EXCLUDED.url,"
            "    last_error = EXCLUDED.last_error,"
            "    updated_at = EXCLUDED.updated_at",
            (project, url, error))

    def old_state(self, project):
        rows = self.db.query(
            "SELECT cursor, last_error, url, last_read_at"
            " FROM fingerprint_watch_state WHERE project = %s", (project,))
        return rows[0] if rows else None

    # -- the old contract survives the migration ----------------------------

    def test_the_old_table_keeps_its_project_primary_key(self):
        row = self.db.query(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint"
            " WHERE conrelid = 'fingerprint_watch_state'::regclass"
            "   AND contype = 'p'")
        self.assertEqual(len(row), 1)
        self.assertIn("(project)", row[0]["definition"])
        self.assertNotIn("environment", row[0]["definition"])

    def test_the_old_process_still_writes_after_the_migration(self):
        # Exactly what a 1.39.0 host does when it holds the lease after 0549.
        self.old_advance("acme", "https://prod.acme.test/f", "old-cursor")
        self.old_advance("acme", "https://prod.acme.test/f", "old-cursor-2")
        self.assertEqual(self.old_state("acme")["cursor"], "old-cursor-2")

    def test_a_takeover_from_the_old_cursor_falls_back_then_mirrors(self):
        # 1.39.0 read the production journal: the new pass starts from its
        # cursor instead of replaying the whole journal.
        self.old_advance("acme", "https://prod.acme.test/f", "old-cursor")
        state = self.api.state("acme", "production")
        self.assertEqual(state["cursor"], "old-cursor")
        self.assertEqual(state["environment"], "production")
        self.assertEqual(state["url"], "https://prod.acme.test/f")
        # The new pass advances: both tables carry the same new cursor, so an
        # old process taking the lease back does not replay the window.
        self.api.advance("acme", "https://prod.acme.test/f", "new-cursor", now=1000)
        self.assertEqual(self.api.state("acme", "production")["cursor"], "new-cursor")
        self.assertEqual(self.old_state("acme")["cursor"], "new-cursor")

    def test_the_new_production_row_does_not_need_the_old_one(self):
        self.api.advance("acme", "https://prod.acme.test/f", "new-cursor", now=1000)
        self.api.fail("acme", "https://prod.acme.test/f", "HTTP 503", now=1061)
        state = self.api.state("acme", "production")
        self.assertEqual(state["cursor"], "new-cursor")
        self.assertIn("HTTP 503", state["last_error"])
        # the mirror keeps the old process's failure visible too
        self.assertIn("HTTP 503", self.old_state("acme")["last_error"])

    def test_staging_never_writes_the_old_production_row(self):
        self.api.advance("acme", "https://prod.acme.test/f", "prod-cursor", now=1000)
        self.api.advance("acme", "https://staging.acme.test/f", "staging-cursor",
                         environment="staging", now=1000)
        state = self.api.state("acme", "staging")
        self.assertEqual(state["cursor"], "staging-cursor")
        self.assertEqual(state["environment"], "staging")
        # the old table still carries only the production row
        self.assertEqual(self.old_state("acme")["cursor"], "prod-cursor")
        self.assertNotIn("staging", self.db.query(
            "SELECT url FROM fingerprint_watch_state WHERE project = 'acme'")[0]["url"])

    def test_an_old_failure_is_read_by_the_new_alerts_until_the_new_write(self):
        self.old_fail("acme", "https://prod.acme.test/f", "HTTP 401")
        failing = self.api.failing()
        self.assertEqual([row["url"] for row in failing],
                         ["https://prod.acme.test/f"])
        self.assertEqual(failing[0]["environment"], "production")
        self.api.advance("acme", "https://prod.acme.test/f", "cursor", now=1000)
        self.assertEqual(self.api.failing(), [])

    def test_a_staging_failure_is_not_mirrored_to_the_old_table(self):
        self.api.fail("acme", "https://staging.acme.test/f", "HTTP 401",
                      environment="staging", now=1000)
        self.assertIsNone(self.old_state("acme"))
        self.assertEqual(len(self.api.failing()), 1)

    def test_a_url_change_drops_the_foreign_cursor_on_the_mirror_too(self):
        # #5968 review P2: a failure after the endpoint changed must not leave
        # the old journal's cursor under the new URL — the next read would send
        # cursor-A to journal B and could skip entries.  The N-1 mirror follows
        # the same rule, so a 1.39.0 takeover does not inherit it either.
        self.api.advance("acme", "https://a.acme.test/f", "cursor-a", now=1000)
        self.api.fail("acme", "https://b.acme.test/f", "HTTP 503", now=1061)
        state = self.api.state("acme", "production")
        self.assertEqual(state["url"], "https://b.acme.test/f")
        self.assertIsNone(state["cursor"])
        self.assertIsNone(state["last_read_ts"])
        old = self.old_state("acme")
        self.assertEqual(old["url"], "https://b.acme.test/f")
        self.assertIsNone(old["cursor"])
        self.assertIsNone(old["last_read_at"])

    def test_a_same_url_failure_keeps_the_cursor(self):
        # The reset is tied to the URL change, never to a transient failure.
        self.api.advance("acme", "https://prod.acme.test/f", "cursor", now=1000)
        self.api.fail("acme", "https://prod.acme.test/f", "HTTP 503", now=1061)
        state = self.api.state("acme", "production")
        self.assertEqual(state["cursor"], "cursor")
        self.assertIsNotNone(state["last_read_ts"])
        old = self.old_state("acme")
        self.assertEqual(old["cursor"], "cursor")
        self.assertIsNotNone(old["last_read_at"])

    def test_an_old_failure_after_a_healthy_new_read_is_alerted(self):
        # #5968 review P2: the migration seeds production, then a 1.39.0
        # process holding the lease fails.  That failure is the most recent
        # state of production and must reach the alerts, even though a healthy
        # new-table row exists.
        self.api.advance("acme", "https://prod.acme.test/f", "new", now=1000)
        self.old_fail("acme", "https://prod.acme.test/f", "HTTP 401")
        failing = self.api.failing()
        self.assertEqual(len(failing), 1)
        self.assertEqual(failing[0]["url"], "https://prod.acme.test/f")
        self.assertEqual(failing[0]["environment"], "production")
        self.assertIn("HTTP 401", failing[0]["last_error"])
        # A later successful production read clears it.
        self.api.advance("acme", "https://prod.acme.test/f", "newer", now=2000)
        self.assertEqual(self.api.failing(), [])

    def test_a_stale_new_failure_is_cleared_by_a_newer_old_read(self):
        # The reverse takeover: the old process read later than the new one
        # failed; the old healthy row is the current production state.
        self.api.fail("acme", "https://prod.acme.test/f", "HTTP 401", now=1000)
        self.old_advance("acme", "https://prod.acme.test/f", "old-later")
        self.assertEqual(self.api.failing(), [])

    def test_state_takes_the_most_recent_production_cursor(self):
        # New code advances, then an old process takes the lease back and
        # advances only the historical table: the new pass must resume from
        # the old, more recent cursor, not replay and risk a false recurrence.
        self.api.advance("acme", "https://prod.acme.test/f", "new", now=1000)
        self.old_advance("acme", "https://prod.acme.test/f", "old-later")
        self.assertEqual(self.api.state("acme", "production")["cursor"],
                         "old-later")
        # And the reverse: a newer new-table read wins.
        self.api.advance("acme", "https://prod.acme.test/f", "newer",
                         now=9999999999)
        self.assertEqual(self.api.state("acme", "production")["cursor"], "newer")

    def test_state_prefers_the_new_source_when_the_url_differs(self):
        # A different URL is a different journal: its cursor must not be
        # borrowed from the historical row.
        self.api.advance("acme", "https://new.acme.test/f", "new", now=1000)
        self.old_advance("acme", "https://old.acme.test/f", "old")
        state = self.api.state("acme", "production")
        self.assertEqual(state["cursor"], "new")
        self.assertEqual(state["url"], "https://new.acme.test/f")
