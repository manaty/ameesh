# SPDX-License-Identifier: AGPL-3.0-only
"""Durable intake cursor; use lock/remember/advance in one transaction."""
import json

from ....storage import interface
from .schema import DevDomain


class GithubWatch(DevDomain, interface.GithubWatch):
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

    def pending_page(self, project, after, batch):
        """#7650 : une page des entrées en attente, après le curseur `after`.

        Rend, pour chaque entrée `github_watch_pending`, le lot qui la couvre
        (même `app` et même `issue_ref` ; une remarque seulement par un lot
        créé après sa livraison), ou None. L'ordre du curseur est
        (repo, kind, object_id)."""
        return self.db.query(
            "SELECT p.repo,p.kind,p.object_id,p.issue_ref,p.delivered_at,"
            " (SELECT w.id FROM work_items w"
            " WHERE w.app=p.project AND lower(w.issue_ref)=lower(p.issue_ref)"
            " AND (p.kind='issue' OR w.created_at>=to_timestamp(p.delivered_at))"
            " ORDER BY w.id LIMIT 1) AS lot"
            " FROM github_watch_pending p WHERE p.project=%s"
            " AND (p.repo,p.kind,p.object_id)>(%s,%s,%s)"
            " ORDER BY p.repo,p.kind,p.object_id LIMIT %s",
            (project, *after, batch))

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

    # -- #7761: pre-act audit storage, ported from the core shim --------------
    def audit_activate(self, project, repo, workflow, now):
        self.db.execute(
            "INSERT INTO pre_act_audit_cursors VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING", (project, repo, workflow, now, now))
        return self.db.query(
            "SELECT activated_at, scanned_through FROM pre_act_audit_cursors"
            " WHERE project=%s AND repo=%s AND workflow=%s",
            (project, repo, workflow))[0]

    def audit_lock(self):
        # One common lock: receipts are single-use across all subscriptions.
        self.db.query("SELECT pg_advisory_xact_lock(7761, 1)")

    def audit_receipts(self, ids):
        return [row["evidence"] for row in self.db.query(
            "SELECT evidence FROM work_item_events WHERE evidence->>'type'='pre-act/1'"
            " AND evidence->>'pregeste_id'=ANY(%s::text[])", (list(ids),))]

    def audit_known(self, repo, runs):
        return {(int(r["run_id"]), int(r["attempt"])) for r in self.db.query(
            "SELECT run_id, attempt FROM pre_act_audit_runs WHERE repo=%s"
            " AND run_id=ANY(%s::bigint[])", (repo, [int(r["id"]) for r in runs]))}

    def audit_consumed(self, ids):
        return {row["pregeste_id"] for row in self.db.query(
            "SELECT pregeste_id FROM pre_act_audit_runs WHERE reason=''"
            " AND pregeste_id=ANY(%s::text[])", (list(ids),))}

    def audit_record(self, repo, workflow, run, pregeste_id, reason):
        self.db.execute(
            "INSERT INTO pre_act_audit_runs VALUES (%s,%s,%s,%s,%s,%s)",
            (repo, int(run["id"]), int(run.get("run_attempt", 1)), workflow,
             pregeste_id, reason))

    def audit_advance(self, project, repo, workflow, until):
        self.db.execute(
            "UPDATE pre_act_audit_cursors SET scanned_through=GREATEST(scanned_through,%s)"
            " WHERE project=%s AND repo=%s AND workflow=%s",
            (until, project, repo, workflow))
