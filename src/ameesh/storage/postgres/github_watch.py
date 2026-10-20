# SPDX-License-Identifier: AGPL-3.0-only
"""Durable intake cursor; use lock/remember/advance in one transaction."""
from .. import interface


class GithubWatch(interface.GithubWatch):
    def cursor(self, project, repo):
        rows = self.db.query(
            "SELECT scanned_through FROM github_watch_cursors WHERE project=%s AND repo=%s",
            (project, repo))
        return float(rows[0]["scanned_through"]) if rows else None

    def lock(self, project, repo, initial):
        self.db.execute(
            "INSERT INTO github_watch_cursors VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (project, repo, initial))
        rows = self.db.query(
            "SELECT scanned_through FROM github_watch_cursors WHERE project=%s AND repo=%s"
            " FOR UPDATE", (project, repo))
        return float(rows[0]["scanned_through"])

    def remember(self, project, repo, kind, object_id, digest):
        return bool(self.db.query(
            "INSERT INTO github_watch_events AS e VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (project,repo,kind,object_id) DO UPDATE SET digest=EXCLUDED.digest"
            " WHERE e.digest <> EXCLUDED.digest RETURNING object_id",
            (project, repo, kind, object_id, digest)))

    def advance(self, project, repo, until):
        self.db.execute(
            "UPDATE github_watch_cursors SET scanned_through=greatest(scanned_through,%s)"
            " WHERE project=%s AND repo=%s", (until, project, repo))
