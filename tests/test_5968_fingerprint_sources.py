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
            "SELECT cursor, last_error FROM fingerprint_watch_state"
            " WHERE project = %s", (project,))
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
