# SPDX-License-Identifier: AGPL-3.0-only
"""#3885 — the resume cursor of the failure-fingerprint pass (one per project)."""
from __future__ import annotations

from .. import interface


class FingerprintWatch(interface.FingerprintWatch):
    def state(self, project):
        rows = self.db.query(
            "SELECT cursor, extract(epoch FROM last_read_at) AS last_read_ts,"
            "       last_error FROM fingerprint_watch_state"
            " WHERE project = %s", (str(project or ""),))
        return rows[0] if rows else None

    def advance(self, project, url, cursor, *, now=None):
        self.db.execute(
            "INSERT INTO fingerprint_watch_state"
            "    (project, url, cursor, last_read_at, last_error, updated_at)"
            " VALUES (%s, %s, %s, coalesce(to_timestamp(%s), now()), '', now())"
            " ON CONFLICT (project) DO UPDATE SET"
            "    url = EXCLUDED.url,"
            "    cursor = EXCLUDED.cursor,"
            "    last_read_at = EXCLUDED.last_read_at,"
            "    last_error = '',"
            "    updated_at = now()",
            (str(project or ""), str(url or ""), cursor, now))

    def fail(self, project, url, error, *, now=None):
        self.db.execute(
            "INSERT INTO fingerprint_watch_state"
            "    (project, url, cursor, last_read_at, last_error, updated_at)"
            " VALUES (%s, %s, NULL, NULL, %s, now())"
            " ON CONFLICT (project) DO UPDATE SET"
            "    url = EXCLUDED.url,"
            "    last_error = EXCLUDED.last_error,"
            "    updated_at = now()",
            (str(project or ""), str(url or ""), str(error or "")[:500]))
