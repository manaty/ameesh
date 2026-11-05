# SPDX-License-Identifier: AGPL-3.0-only
"""#3885/#5968 — the resume cursor of the failure-fingerprint pass.

A project may declare several sources (production, staging, …); each
environment keeps its own cursor in ``fingerprint_source_state``.  The
historical table ``fingerprint_watch_state`` is left untouched and still
readable/writable by the N-1 code (#5946): a production advance/fail mirrors
its row there with the old ``ON CONFLICT (project)`` contract, and a project
without a row in the new table falls back to its old production cursor.
"""
from __future__ import annotations

from .. import interface

#: the old table's shape (one row per project) is the N-1 contract: never
#: change it, never stop mirroring a production source into it
OLD_COLUMNS = ("project, url, cursor, last_read_at, last_error, updated_at")


class FingerprintWatch(interface.FingerprintWatch):
    def state(self, project, environment="production"):
        rows = self.db.query(
            "SELECT cursor, environment, url,"
            "       extract(epoch FROM last_read_at) AS last_read_ts,"
            "       last_error, extract(epoch FROM updated_at) AS updated_ts"
            " FROM fingerprint_source_state"
            " WHERE project = %s AND environment = %s",
            (str(project or ""), str(environment or "production")))
        new_row = rows[0] if rows else None
        if str(environment or "production") != "production":
            return new_row
        # N-1 takeover: the old process writes the historical table, the new
        # one the source table.  Take the **most recent** production cursor of
        # the two (same URL), so a takeover never replays a window already
        # read and opens a false recurrence (#5968 review N1).  A different
        # URL is not the same source: the new table's row wins.
        rows = self.db.query(
            "SELECT cursor, 'production' AS environment, url,"
            "       extract(epoch FROM last_read_at) AS last_read_ts,"
            "       last_error, extract(epoch FROM updated_at) AS updated_ts"
            " FROM fingerprint_watch_state WHERE project = %s",
            (str(project or ""),))
        old_row = rows[0] if rows else None
        if new_row is None or old_row is None:
            return new_row if new_row is not None else old_row
        if new_row.get("url") != old_row.get("url"):
            return new_row

        def freshness(row):
            read = row.get("last_read_ts")
            tried = row.get("updated_ts")
            return (float(read) if read is not None else -1.0,
                    float(tried) if tried is not None else -1.0)

        return new_row if freshness(new_row) >= freshness(old_row) else old_row

    def failing(self):
        # The new sources first; the old table covers a production source the
        # new pass has not written yet (old process held the lease last).
        return self.db.query(
            "SELECT project, environment, url, last_error,"
            "       extract(epoch FROM last_read_at) AS last_read_ts,"
            "       extract(epoch FROM updated_at) AS updated_ts"
            " FROM fingerprint_source_state WHERE last_error <> ''"
            " UNION ALL"
            " SELECT w.project, 'production', w.url, w.last_error,"
            "       extract(epoch FROM w.last_read_at),"
            "       extract(epoch FROM w.updated_at)"
            " FROM fingerprint_watch_state w"
            " WHERE w.last_error <> ''"
            "   AND NOT EXISTS (SELECT 1 FROM fingerprint_source_state s"
            "                   WHERE s.project = w.project"
            "                     AND s.environment = 'production')"
            " ORDER BY project, environment")

    def advance(self, project, url, cursor, *, environment="production", now=None):
        project = str(project or "")
        url = str(url or "")
        environment = str(environment or "production")
        self.db.execute(
            "INSERT INTO fingerprint_source_state"
            "    (project, environment, url, cursor, last_read_at, last_error,"
            "     updated_at)"
            " VALUES (%s, %s, %s, %s, coalesce(to_timestamp(%s), now()), '', now())"
            " ON CONFLICT (project, environment) DO UPDATE SET"
            "    url = EXCLUDED.url,"
            "    cursor = EXCLUDED.cursor,"
            "    last_read_at = EXCLUDED.last_read_at,"
            "    last_error = '',"
            "    updated_at = now()",
            (project, environment, url, cursor, now))
        if environment == "production":
            self._mirror(project, url, cursor, "", now, success=True)

    def fail(self, project, url, error, *, environment="production", now=None):
        project = str(project or "")
        url = str(url or "")
        environment = str(environment or "production")
        error = str(error or "")[:500]
        self.db.execute(
            "INSERT INTO fingerprint_source_state"
            "    (project, environment, url, cursor, last_read_at, last_error,"
            "     updated_at)"
            " VALUES (%s, %s, %s, NULL, NULL, %s, coalesce(to_timestamp(%s), now()))"
            " ON CONFLICT (project, environment) DO UPDATE SET"
            "    url = EXCLUDED.url,"
            "    last_error = EXCLUDED.last_error,"
            "    updated_at = EXCLUDED.updated_at",
            (project, environment, url, error, now))
        if environment == "production":
            self._mirror(project, url, None, error, now, success=False)

    def _mirror(self, project, url, cursor, error, now, *, success):
        """Keep the N-1 production row current, with its own old contract.

        A success moves the old cursor and clears its error; a failure keeps
        the old cursor and records the error.  The old table's primary key is
        `project`, so its upsert is `ON CONFLICT (project)` — exactly what a
        1.39.0 process still in service expects.
        """
        if success:
            self.db.execute(
                "INSERT INTO fingerprint_watch_state (" + OLD_COLUMNS + ")"
                " VALUES (%s, %s, %s, coalesce(to_timestamp(%s), now()), '', now())"
                " ON CONFLICT (project) DO UPDATE SET"
                "    url = EXCLUDED.url,"
                "    cursor = EXCLUDED.cursor,"
                "    last_read_at = EXCLUDED.last_read_at,"
                "    last_error = '',"
                "    updated_at = now()",
                (project, url, cursor, now))
        else:
            self.db.execute(
                "INSERT INTO fingerprint_watch_state (" + OLD_COLUMNS + ")"
                " VALUES (%s, %s, NULL, NULL, %s, coalesce(to_timestamp(%s), now()))"
                " ON CONFLICT (project) DO UPDATE SET"
                "    url = EXCLUDED.url,"
                "    last_error = EXCLUDED.last_error,"
                "    updated_at = EXCLUDED.updated_at",
                (project, url, error, now))
