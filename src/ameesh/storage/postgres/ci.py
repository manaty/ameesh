# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : résultats de CI relevés en direct (`ci_runs`,
`ci_test_failures`, 0055, L161c)."""
from __future__ import annotations

import json

from .. import interface

RUN_COLUMNS = """
    repo, run_id, head_sha, branch, event, base, work_item_id, status, superseded_by, url,
    run_attempt,
    extract(epoch from first_seen_at)::float8 AS first_seen_ts,
    extract(epoch from concluded_at)::float8 AS concluded_ts,
    urgent_message_id, verdict_message_id
"""

FAILURE_COLUMNS = """
    repo, run_id, run_attempt, part, test, module, status, inherited, excerpt, replay,
    duration_s, extract(epoch from failed_at)::float8 AS failed_ts, message_id
"""


def _ints(values) -> str:
    # ids entiers seulement : ils entrent tels quels dans le texte SQL
    return ", ".join(str(int(v)) for v in sorted({int(v) for v in values}))


class CiResults(interface.CiResults):

    def run(self, repo, run_id) -> dict | None:
        rows = self.db.query("SELECT %s FROM ci_runs WHERE repo = %%s AND run_id = %%s"
                             % RUN_COLUMNS, (repo, int(run_id)))
        return rows[0] if rows else None

    def open_run(self, repo, run_id, *, head_sha, branch, event, base, work_item_id,
                 url, run_attempt=1) -> dict:
        rows = self.db.query(
            """
            INSERT INTO ci_runs (repo, run_id, head_sha, branch, event, base, work_item_id,
                                 url, run_attempt)
            VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s)
            ON CONFLICT (repo, run_id) DO UPDATE
               SET work_item_id = coalesce(ci_runs.work_item_id, excluded.work_item_id)
            RETURNING %s
            """ % RUN_COLUMNS,
            (repo, int(run_id), head_sha, branch, event or "", base or "main",
             int(work_item_id) if work_item_id is not None else None, url,
             int(run_attempt)))
        return rows[0]

    def reopen_run(self, repo, run_id, *, run_attempt) -> dict | None:
        # #901 : GitHub dit l'exécution de nouveau `queued`/`in_progress` avec
        # le même run_id : c'est une relance. La tête a pu bouger depuis (une
        # relance d'exécution périmée ne doit pas ressusciter une ligne
        # remplacée) : dans ce cas rien n'est rouvert. L'attempt doit avoir
        # augmenté : une liste API en retard (#828) redit en cours une
        # exécution conclue au même attempt sans relance, la rouvrir
        # renverrait son verdict à chaque passe.
        rows = self.db.query(
            """
            UPDATE ci_runs r
               SET status = 'running', concluded_at = NULL, superseded_by = NULL,
                   run_attempt = %%s, urgent_message_id = NULL, verdict_message_id = NULL
             WHERE r.repo = %%s AND r.run_id = %%s
               AND r.status <> 'superseded'
               AND r.run_attempt < %%s
               AND NOT EXISTS (
                   SELECT 1 FROM ci_runs n
                    WHERE n.repo = r.repo AND n.branch = r.branch
                      AND n.run_id > r.run_id AND n.head_sha <> r.head_sha)
            RETURNING %s
            """ % RUN_COLUMNS,
            (int(run_attempt), repo, int(run_id), int(run_attempt)))
        return rows[0] if rows else None

    def running(self, repo) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM ci_runs WHERE repo = %%s AND status = 'running' ORDER BY run_id"
            % RUN_COLUMNS, (repo,))

    def conclude(self, repo, run_id, status) -> bool:
        rows = self.db.query(
            """
            UPDATE ci_runs SET status = %s, concluded_at = now()
             WHERE repo = %s AND run_id = %s AND status = 'running'
            RETURNING run_id
            """, (status, repo, int(run_id)))
        return bool(rows)

    def supersede(self, repo, branch, head_sha, run_id) -> list[dict]:
        # une exécution annulée avant que la suivante soit vue l'est aussi :
        # ses messages non lus sont alors retirés
        return self.db.query(
            """
            UPDATE ci_runs SET status = 'superseded', superseded_by = %%s,
                               concluded_at = coalesce(concluded_at, now())
             WHERE repo = %%s AND branch = %%s AND status IN ('running', 'cancelled')
               AND head_sha <> %%s AND run_id < %%s
            RETURNING %s
            """ % RUN_COLUMNS, (head_sha, repo, branch, head_sha, int(run_id)))

    def set_message(self, repo, run_id, which, message_id) -> None:
        column = {"urgent": "urgent_message_id", "verdict": "verdict_message_id"}[which]
        self.db.execute("UPDATE ci_runs SET %s = %%s WHERE repo = %%s AND run_id = %%s"
                        % column, (int(message_id), repo, int(run_id)))

    def failures(self, repo, run_id) -> list[dict]:
        # l'attempt courant seulement : un échec de l'attempt précédent qui
        # n'a pas été re-vu à la relance n'est plus imputé à l'exécution (#901)
        return self.db.query(
            "SELECT %s FROM ci_test_failures"
            " WHERE repo = %%s AND run_id = %%s"
            "   AND run_attempt = (SELECT run_attempt FROM ci_runs"
            "                       WHERE repo = %%s AND run_id = %%s)"
            " ORDER BY failed_at NULLS LAST, part, test" % FAILURE_COLUMNS,
            (repo, int(run_id), repo, int(run_id)))

    def add_failure(self, repo, run_id, *, run_attempt, part, test, module, status, inherited,
                    excerpt, replay, duration_s, failed_at) -> bool:
        rows = self.db.query(
            """
            INSERT INTO ci_test_failures
                (repo, run_id, run_attempt, part, test, module, status, inherited, excerpt,
                 replay, duration_s, failed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
            ON CONFLICT (repo, run_id, part, test) DO UPDATE
               SET run_attempt = excluded.run_attempt, module = excluded.module,
                   status = excluded.status, inherited = excluded.inherited,
                   excerpt = excluded.excerpt, replay = excluded.replay,
                   duration_s = excluded.duration_s, failed_at = excluded.failed_at,
                   message_id = NULL, recorded_at = now()
             WHERE ci_test_failures.run_attempt <> excluded.run_attempt
            RETURNING test
            """,
            (repo, int(run_id), int(run_attempt), part or "", test, module or "",
             status or "echec", bool(inherited), excerpt or "", replay or "", duration_s,
             failed_at or None))
        return bool(rows)

    def set_failure_message(self, repo, run_id, run_attempt, tests, message_id) -> None:
        if not tests:
            return
        for part, test in tests:
            self.db.execute(
                "UPDATE ci_test_failures SET message_id = %s"
                " WHERE repo = %s AND run_id = %s AND run_attempt = %s AND part = %s"
                " AND test = %s",
                (int(message_id), repo, int(run_id), int(run_attempt), part, test))

    def base_failures(self, repo, branch) -> dict:
        rows = self.db.query(
            """
            WITH last AS (
                SELECT run_id, head_sha, run_attempt FROM ci_runs
                 WHERE repo = %s AND branch = %s AND status IN ('success', 'failure')
                 ORDER BY concluded_at DESC, run_id DESC LIMIT 1
            )
            SELECT last.run_id, last.head_sha, f.test
              FROM last LEFT JOIN ci_test_failures f
                ON f.repo = %s AND f.run_id = last.run_id
               AND f.run_attempt = last.run_attempt
            """, (repo, branch, repo))
        if not rows:
            return {}
        return {"run_id": rows[0]["run_id"], "head_sha": rows[0]["head_sha"],
                "tests": sorted({r["test"] for r in rows if r.get("test")})}

    def message_ids(self, repo, run_ids) -> list[int]:
        if not run_ids:
            return []
        ids = _ints(run_ids)
        rows = self.db.query(
            """
            SELECT urgent_message_id AS id FROM ci_runs
             WHERE repo = %%s AND run_id IN (%s) AND urgent_message_id IS NOT NULL
            UNION
            SELECT message_id FROM ci_test_failures
             WHERE repo = %%s AND run_id IN (%s) AND message_id IS NOT NULL
            """ % (ids, ids), (repo, repo))
        return sorted(int(r["id"]) for r in rows)

    def for_lot(self, work_item_id, limit) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM ci_runs WHERE work_item_id = %%s"
            " ORDER BY first_seen_at DESC, run_id DESC LIMIT %%s" % RUN_COLUMNS,
            (int(work_item_id), int(limit)))

    def latest_concluded(self, branches) -> list[dict]:
        if not branches:
            return []
        return self.db.query(
            """
            SELECT DISTINCT ON (repo, branch) %s
              FROM ci_runs
             WHERE branch IN (SELECT jsonb_array_elements_text(%%s::jsonb))
               AND status IN ('success', 'failure')
             ORDER BY repo, branch, concluded_at DESC, run_id DESC
            """ % RUN_COLUMNS, (json.dumps(sorted(branches)),))
