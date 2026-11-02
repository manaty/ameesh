# SPDX-License-Identifier: AGPL-3.0-only
"""Durable intake cursor; use lock/remember/advance in one transaction."""
import json

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

    def scan(self, project, repo):
        rows = self.db.query(
            "SELECT progress FROM github_watch_scans WHERE project=%s AND repo=%s", (project, repo))
        if not rows:
            return None
        value = rows[0]["progress"]
        return json.loads(value) if isinstance(value, str) else value

    def checkpoint(self, project, repo, progress):
        if progress is None:
            self.db.execute("DELETE FROM github_watch_scans WHERE project=%s AND repo=%s",
                            (project, repo))
        else:
            self.db.execute(
                "INSERT INTO github_watch_scans VALUES (%s,%s,%s::jsonb)"
                " ON CONFLICT (project,repo) DO UPDATE SET progress=EXCLUDED.progress",
                (project, repo, json.dumps(progress)))

    # -- #5506: follow-up of delivered inputs ---------------------------------
    def track(self, project, repo, agent, entries, now):
        # Caller holds the cursor lock: the follow-up commits with the digest.
        bound = self.db.query("SELECT coalesce(max(id), 0) AS id FROM work_item_events")
        events_after = int(bound[0]["id"])
        for entry in entries:
            self.db.execute(
                "INSERT INTO github_watch_pending (project, repo, kind, object_id,"
                " issue_ref, url, agent, delivered_at, events_after)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (project, repo, kind, object_id) DO UPDATE SET"
                " issue_ref=EXCLUDED.issue_ref, url=EXCLUDED.url, agent=EXCLUDED.agent,"
                " delivered_at=EXCLUDED.delivered_at, events_after=EXCLUDED.events_after,"
                " reminded_at=NULL, escalated_at=NULL, resolved_at=NULL, proof=NULL,"
                " checked_at=NULL",
                (project, repo, entry["kind"], int(entry["object_id"]),
                 entry["issue_ref"], entry["url"], agent, float(now), events_after))

    def open_entries(self, limit, *, checked_before=None):
        if checked_before is None:
            return self.db.query(
                "SELECT * FROM github_watch_pending WHERE resolved_at IS NULL"
                " ORDER BY delivered_at, project, repo, kind, object_id LIMIT %s",
                (int(limit),))
        return self.db.query(
            "SELECT * FROM github_watch_pending WHERE resolved_at IS NULL"
            " AND (checked_at IS NULL OR checked_at <= %s)"
            " ORDER BY checked_at NULLS FIRST, delivered_at, project, repo, kind, object_id"
            " LIMIT %s", (float(checked_before), int(limit)))

    def local_proof(self, entry):
        # An input on an issue is covered by any lot carrying it; a comment
        # only by a lot created after its delivery.
        rows = self.db.query(
            "SELECT id FROM work_items WHERE lower(issue_ref) = lower(%s)"
            " AND (%s = 'issue' OR created_at >= to_timestamp(%s))"
            " ORDER BY id LIMIT 1",
            (entry["issue_ref"], entry["kind"], float(entry["delivered_at"])))
        if rows:
            return "lot #%s" % rows[0]["id"]
        rows = self.db.query(
            "SELECT work_item_id FROM work_item_events WHERE id > %s"
            " AND strpos(lower(note), lower(%s)) > 0 AND actor <> 'ameesh'"
            " ORDER BY id LIMIT 1", (int(entry["events_after"]), entry["url"]))
        if rows:
            return "note on lot #%s" % rows[0]["work_item_id"]
        return None

    def resolve(self, entry, proof, now):
        self.db.execute(
            "UPDATE github_watch_pending SET resolved_at=%s, proof=%s"
            " WHERE project=%s AND repo=%s AND kind=%s AND object_id=%s"
            " AND resolved_at IS NULL AND delivered_at=%s",
            (float(now), proof, entry["project"], entry["repo"], entry["kind"],
             int(entry["object_id"]), float(entry["delivered_at"])))

    def mark(self, entries, column, now):
        if column not in ("reminded_at", "escalated_at", "checked_at"):
            raise ValueError("github_watch.mark: unknown column %r" % column)
        for entry in entries:
            self.db.execute(
                "UPDATE github_watch_pending SET " + column + "=%s"
                " WHERE project=%s AND repo=%s AND kind=%s AND object_id=%s"
                " AND delivered_at=%s",
                (float(now), entry["project"], entry["repo"], entry["kind"],
                 int(entry["object_id"]), float(entry["delivered_at"])))
