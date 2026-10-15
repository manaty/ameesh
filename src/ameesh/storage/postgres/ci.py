# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : résultats de CI relevés en direct (`ci_runs`,
`ci_test_failures`, 0055, L161c) et garde de la CI (`ci_runner_state`,
`ci_queue_state`, 0426, #845)."""
from __future__ import annotations

import json

from .. import interface

RUN_COLUMNS = """
    repo, run_id, head_sha, branch, event, base, work_item_id, status, superseded_by, url,
    workflow,
    extract(epoch from first_seen_at)::float8 AS first_seen_ts,
    extract(epoch from concluded_at)::float8 AS concluded_ts,
    extract(epoch from created_at)::float8 AS created_ts,
    extract(epoch from started_at)::float8 AS started_ts,
    urgent_message_id, verdict_message_id
"""

FAILURE_COLUMNS = """
    repo, run_id, part, test, module, status, inherited, excerpt, replay, duration_s,
    extract(epoch from failed_at)::float8 AS failed_ts, message_id
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
                 url, workflow="", created_at=None) -> dict:
        rows = self.db.query(
            """
            INSERT INTO ci_runs (repo, run_id, head_sha, branch, event, base, work_item_id,
                                 url, workflow, created_at)
            VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s::timestamptz)
            ON CONFLICT (repo, run_id) DO UPDATE
               SET work_item_id = coalesce(ci_runs.work_item_id, excluded.work_item_id),
                   workflow = coalesce(nullif(excluded.workflow, ''), ci_runs.workflow)
            RETURNING %s
            """ % RUN_COLUMNS,
            (repo, int(run_id), head_sha, branch, event or "", base or "main",
             int(work_item_id) if work_item_id is not None else None, url,
             workflow or "", created_at or None))
        return rows[0]

    def mark_started(self, repo, run_ids) -> None:
        if not run_ids:
            return
        self.db.execute(
            "UPDATE ci_runs SET started_at = now() WHERE repo = %%s AND run_id IN (%s)"
            " AND started_at IS NULL" % _ints(run_ids), (repo,))

    def queued(self) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM ci_runs WHERE status = 'running' AND started_at IS NULL"
            " ORDER BY coalesce(created_at, first_seen_at), run_id" % RUN_COLUMNS)

    def running(self, repo) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM ci_runs WHERE repo = %%s AND status = 'running' ORDER BY run_id"
            % RUN_COLUMNS, (repo,))

    def running_on(self, branches) -> list[dict]:
        """#846 : tous dépôts confondus, les exécutions en cours d'une base."""
        if not branches:
            return []
        return self.db.query(
            """
            SELECT %s FROM ci_runs
             WHERE branch IN (SELECT jsonb_array_elements_text(%%s::jsonb))
               AND status = 'running'
             ORDER BY repo, run_id
            """ % RUN_COLUMNS, (json.dumps(sorted(branches)),))

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
        return self.db.query(
            "SELECT %s FROM ci_test_failures WHERE repo = %%s AND run_id = %%s"
            " ORDER BY failed_at NULLS LAST, part, test" % FAILURE_COLUMNS,
            (repo, int(run_id)))

    def add_failure(self, repo, run_id, *, part, test, module, status, inherited,
                    excerpt, replay, duration_s, failed_at) -> bool:
        rows = self.db.query(
            """
            INSERT INTO ci_test_failures
                (repo, run_id, part, test, module, status, inherited, excerpt, replay,
                 duration_s, failed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
            ON CONFLICT (repo, run_id, part, test) DO NOTHING
            RETURNING test
            """,
            (repo, int(run_id), part or "", test, module or "", status or "echec",
             bool(inherited), excerpt or "", replay or "", duration_s, failed_at or None))
        return bool(rows)

    def set_failure_message(self, repo, run_id, tests, message_id) -> None:
        if not tests:
            return
        for part, test in tests:
            self.db.execute(
                "UPDATE ci_test_failures SET message_id = %s"
                " WHERE repo = %s AND run_id = %s AND part = %s AND test = %s",
                (int(message_id), repo, int(run_id), part, test))

    def base_failures(self, repo, branch, *, workflow="") -> dict:
        # strictement la même suite : les lignes antérieures à la migration
        # 0433, toutes de `tests.yml`, y sont nommées (#1110, R1 P2-2)
        rows = self.db.query(
            """
            WITH last AS (
                SELECT run_id, head_sha FROM ci_runs
                 WHERE repo = %s AND branch = %s AND status IN ('success', 'failure')
                   AND workflow = %s
                 ORDER BY concluded_at DESC, run_id DESC LIMIT 1
            )
            SELECT last.run_id, last.head_sha, f.test
              FROM last LEFT JOIN ci_test_failures f
                ON f.repo = %s AND f.run_id = last.run_id
            """, (repo, branch, workflow or "", repo))
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

    def head_replaced(self, repo, branch, head_sha, run_id) -> bool:
        rows = self.db.query(
            """
            SELECT 1 FROM ci_runs
             WHERE repo = %s AND branch = %s AND head_sha <> %s AND run_id > %s
             LIMIT 1
            """, (repo, branch, head_sha or "", int(run_id)))
        return bool(rows)

    def latest_concluded(self, branches) -> list[dict]:
        if not branches:
            return []
        return self.db.query(
            """
            WITH current AS (
                SELECT DISTINCT ON (repo, branch) repo, branch, head_sha
                  FROM ci_runs
                 WHERE branch IN (SELECT jsonb_array_elements_text(%%s::jsonb))
                   AND status IN ('success', 'failure')
                 ORDER BY repo, branch, run_id DESC
            )
            SELECT DISTINCT ON (repo, branch, workflow) %s
              FROM ci_runs
             WHERE (repo, branch, head_sha) IN
                   (SELECT repo, branch, head_sha FROM current)
               AND status IN ('success', 'failure')
             ORDER BY repo, branch, workflow, concluded_at DESC, run_id DESC
            """ % RUN_COLUMNS, (json.dumps(sorted(branches)),))

    # -- #845 : garde de la CI (runners et file) ---------------------------
    def observe_runners(self, repo, runners) -> None:
        ids = []
        for r in runners:
            if r.get("id") is None:
                continue
            ids.append(int(r["id"]))
            self.db.execute(
                """
                INSERT INTO ci_runner_state AS s
                    (repo, runner_id, name, busy, job, busy_since, stuck_since)
                VALUES (%s, %s, %s, %s, %s,
                        CASE WHEN %s THEN now() END,
                        CASE WHEN %s THEN now() END)
                ON CONFLICT (repo, runner_id) DO UPDATE SET
                    name        = excluded.name,
                    busy        = excluded.busy,
                    job         = excluded.job,
                    busy_since  = CASE WHEN excluded.busy
                                       THEN coalesce(s.busy_since, now()) ELSE NULL END,
                    stuck_since = CASE WHEN excluded.stuck_since IS NOT NULL
                                       THEN coalesce(s.stuck_since, now()) ELSE NULL END,
                    seen_at     = now()
                """,
                (repo, int(r["id"]), r.get("name") or "", bool(r.get("busy")),
                 r.get("job"), bool(r.get("busy")), bool(r.get("stuck"))))
        # un runner retiré du dépôt ne doit pas laisser une alerte derrière lui
        if ids:
            marks = ", ".join(["%s"] * len(ids))
            self.db.execute(
                "DELETE FROM ci_runner_state WHERE repo = %%s AND runner_id NOT IN (%s)"
                % marks, tuple([repo] + ids))
        else:
            self.db.execute("DELETE FROM ci_runner_state WHERE repo = %s", (repo,))

    def stuck_runners(self) -> list[dict]:
        return self.db.query(
            """
            SELECT repo, runner_id, name, job, busy,
                   extract(epoch from stuck_since)::float8 AS since_ts,
                   extract(epoch from busy_since)::float8  AS busy_ts,
                   extract(epoch from seen_at)::float8     AS seen_ts
              FROM ci_runner_state
             WHERE stuck_since IS NOT NULL
             ORDER BY repo, name
            """)

    def observe_queue(self, repo, *, oldest_id, oldest_at, queued) -> None:
        self.db.execute(
            """
            INSERT INTO ci_queue_state AS q (repo, oldest_id, oldest_at, queued, seen_at)
            VALUES (%s, %s, %s::timestamptz, %s, now())
            ON CONFLICT (repo) DO UPDATE SET
                oldest_id = excluded.oldest_id,
                oldest_at = excluded.oldest_at,
                queued    = excluded.queued,
                seen_at   = now()
            """,
            (repo, int(oldest_id) if oldest_id is not None else None, oldest_at,
             int(queued or 0)))

    def queued_oldest(self) -> list[dict]:
        return self.db.query(
            """
            SELECT repo, oldest_id, queued,
                   extract(epoch from oldest_at)::float8 AS oldest_ts,
                   extract(epoch from seen_at)::float8   AS seen_ts
              FROM ci_queue_state
             ORDER BY repo
            """)

    # -- #845 (revue N/P2 2328) : fenêtre et cache du relevé ----------------
    def claim_guard_scan(self, repo, *, now, window_s) -> bool:
        """Revendique la fenêtre du relevé de `repo`, atomiquement.

        Vrai si aucun relevé n'a été revendiqué depuis `window_s` ; faux si un
        autre le tient encore. L'horloge est écrite AVANT tout appel HTTP :
        deux `notify --once` concurrents (ou deux hôtes) ne doublent pas le
        coût, et un relevé tué en vol laisse la fenêtre expirer."""
        rows = self.db.query(
            """
            INSERT INTO ci_guard_scan AS g (repo, started_at)
            VALUES (%s, to_timestamp(%s))
            ON CONFLICT (repo) DO UPDATE
               SET started_at = excluded.started_at, finished_at = NULL,
                   calls = 0, runs = 0, covered = 0, complete = NULL, reason = NULL
             WHERE g.started_at <= excluded.started_at - make_interval(secs => %s)
            RETURNING repo
            """,
            (repo, float(now), float(window_s)))
        return bool(rows)

    def finish_guard_scan(self, repo, *, now, calls, runs, covered, complete,
                          reason=None) -> None:
        """Clôt le relevé : coût réel, couverture, et raison d'un relevé
        incomplet — un relevé incomplet n'a rien publié."""
        self.db.execute(
            """
            UPDATE ci_guard_scan
               SET finished_at = to_timestamp(%s), calls = %s, runs = %s,
                   covered = %s, complete = %s, reason = %s
             WHERE repo = %s
            """,
            (float(now), int(calls), int(runs), int(covered), bool(complete),
             reason, repo))

    def guard_runs(self, repo) -> dict:
        """Le cache des relevés par exécution : `{(run_id, attempt): {...}}`."""
        rows = self.db.query(
            """
            SELECT run_id, attempt, status, queued, oldest_id, oldest_at, runners
              FROM ci_guard_run
             WHERE repo = %s
            """, (repo,))
        out: dict = {}
        for row in rows:
            runners = row.get("runners")
            if isinstance(runners, str):
                try:
                    runners = json.loads(runners or "{}")
                except ValueError:
                    runners = {}
            out[(int(row["run_id"]), int(row["attempt"]))] = dict(
                row, runners=dict(runners or {}))
        return out

    def observe_guard_run(self, repo, *, run_id, attempt, status, queued,
                          oldest_id, oldest_at, runners) -> None:
        """Écrit le relevé d'une exécution : seules les exécutions restées
        `queued` (aucun job commencé) sont réutilisées sans nouvel appel."""
        self.db.execute(
            """
            INSERT INTO ci_guard_run AS r
                (repo, run_id, attempt, status, queued, oldest_id, oldest_at,
                 runners, seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s::jsonb, now())
            ON CONFLICT (repo, run_id, attempt) DO UPDATE SET
                status    = excluded.status,
                queued    = excluded.queued,
                oldest_id = excluded.oldest_id,
                oldest_at = excluded.oldest_at,
                runners   = excluded.runners,
                seen_at   = now()
            """,
            (repo, int(run_id), int(attempt), status or "", int(queued or 0),
             int(oldest_id) if oldest_id is not None else None, oldest_at,
             json.dumps(runners or {})))

    def prune_guard_runs(self, repo, keep) -> None:
        """Retire du cache les exécutions qui ne sont plus non conclues."""
        keys = sorted({(int(r), int(a)) for r, a in keep})
        if not keys:
            self.db.execute("DELETE FROM ci_guard_run WHERE repo = %s", (repo,))
            return
        conds = " OR ".join(["(run_id = %s AND attempt = %s)"] * len(keys))
        params: list = [repo]
        for run_id, attempt in keys:
            params += [run_id, attempt]
        self.db.execute(
            "DELETE FROM ci_guard_run WHERE repo = %%s AND NOT (%s)" % conds,
            tuple(params))
