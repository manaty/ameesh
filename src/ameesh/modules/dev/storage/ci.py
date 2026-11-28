# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : résultats de CI relevés en direct (`ci_runs`,
`ci_test_failures`, 0055, L161c) et garde de la CI (`ci_runner_state`,
`ci_queue_state`, 0426, #845)."""
from __future__ import annotations

import json

from ....storage import interface
from .schema import DevDomain

RUN_COLUMNS = """
    repo, run_id, head_sha, branch, event, base, work_item_id, status, superseded_by, url,
    workflow,
    run_attempt,
    extract(epoch from first_seen_at)::float8 AS first_seen_ts,
    extract(epoch from concluded_at)::float8 AS concluded_ts,
    extract(epoch from created_at)::float8 AS created_ts,
    extract(epoch from started_at)::float8 AS started_ts,
    urgent_message_id, verdict_message_id
"""

#: #3686 : sur une base, un `workflow_dispatch` d'un workflow qui n'y tourne
#: jamais de lui-même (aucune exécution push, planifiée… sur la même branche)
#: est un geste d'opérateur sur une suite qui ne suit pas cette base : refusé
#: par sa garde, il ne dit rien de l'état fusionné (manaty/nexlink main, 26/10
#: 05:46Z, six suites refusées et six `ci_main_red` qu'aucun vert ne pouvait
#: fermer). Une exécution `pull_request*` dont la tête est la base (PR de
#: synchronisation main → develop) ne prouve pas non plus que la suite la
#: suit. Un dispatch d'une suite qui tourne aussi sur la base compte comme
#: avant. Alias `r` pour la ligne examinée.
FOLLOWS_BASE = """
    (r.event IS DISTINCT FROM 'workflow_dispatch'
     OR EXISTS (SELECT 1 FROM ci_runs o
                 WHERE o.repo = r.repo AND o.branch = r.branch
                   AND o.workflow IS NOT DISTINCT FROM r.workflow
                   AND o.event IS DISTINCT FROM 'workflow_dispatch'
                   AND coalesce(o.event, '') NOT LIKE 'pull_request%%'))
"""

FAILURE_COLUMNS = """
    repo, run_id, run_attempt, part, test, module, status, inherited, excerpt, replay,
    duration_s, extract(epoch from failed_at)::float8 AS failed_ts, message_id
"""


def _ints(values) -> str:
    # ids entiers seulement : ils entrent tels quels dans le texte SQL
    return ", ".join(str(int(v)) for v in sorted({int(v) for v in values}))


class CiResults(DevDomain, interface.CiResults):

    def run(self, repo, run_id) -> dict | None:
        rows = self.db.query("SELECT %s FROM ci_runs WHERE repo = %%s AND run_id = %%s"
                             % RUN_COLUMNS, (repo, int(run_id)))
        return rows[0] if rows else None

    def suite(self, *, since_ts, limit) -> list[dict]:
        # #1476 : seuls les contextes qui GARANTISSENT la suite complète
        # comptent — poussée ou PR `release/**`|`integration/**` (source ou
        # cible) et exécution manuelle (`workflow_dispatch`, toujours
        # complète). Une poussée sur `main` est écartée : une plage poussée
        # entièrement documentaire y prend la voie rapide L161a (make_plan
        # laisse les deux matrices vides) et `ci_runs` ne garde ni le mode du
        # plan ni les jobs, donc la compter tirerait la médiane vers le bas.
        # Une exécution annulée ou remplacée n'a pas de durée de suite. La
        # borne est PAR DÉPÔT : un dépôt très actif ne chasse pas les autres
        # de la lecture (row_number, pas un LIMIT global).
        return self.db.query(
            """
            SELECT repo, run_id, branch, base, event, duration_s, concluded_ts FROM (
                SELECT repo, run_id, branch, base, event,
                       extract(epoch from (concluded_at - first_seen_at))::float8
                           AS duration_s,
                       extract(epoch from concluded_at)::float8 AS concluded_ts,
                       row_number() OVER (PARTITION BY repo
                                          ORDER BY concluded_at DESC, run_id DESC) AS rn
                  FROM ci_runs
                 WHERE status IN ('success', 'failure') AND concluded_at IS NOT NULL
                   AND concluded_at >= to_timestamp(%s)
                   AND (branch LIKE 'release/%%' OR branch LIKE 'integration/%%'
                        OR base LIKE 'release/%%' OR base LIKE 'integration/%%'
                        OR event = 'workflow_dispatch')
            ) t
             WHERE rn <= %s
             ORDER BY concluded_ts DESC, run_id DESC
            """, (float(since_ts), int(limit)))

    def open_run(self, repo, run_id, *, head_sha, branch, event, base, work_item_id,
                 url, workflow="", created_at=None, run_attempt=1) -> dict:
        rows = self.db.query(
            """
            INSERT INTO ci_runs (repo, run_id, head_sha, branch, event, base, work_item_id,
                                 url, workflow, created_at, run_attempt)
            VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s::timestamptz, %%s)
            ON CONFLICT (repo, run_id) DO UPDATE
               SET work_item_id = coalesce(ci_runs.work_item_id, excluded.work_item_id),
                   workflow = coalesce(nullif(excluded.workflow, ''), ci_runs.workflow)
            RETURNING %s
            """ % RUN_COLUMNS,
            (repo, int(run_id), head_sha, branch, event or "", base or "main",
             int(work_item_id) if work_item_id is not None else None, url,
             workflow or "", created_at or None, int(run_attempt)))
        return rows[0]

    def gate_state(self) -> dict:
        """#7650 : le témoin monotone des exécutions de CI (`auditor_pass`)."""
        row = self.db.query(
            """
            SELECT (SELECT coalesce(max(extract(epoch from first_seen_at)), 0)::float8
                      FROM ci_runs) AS ci_seen,
                   (SELECT coalesce(max(extract(epoch from concluded_at)), 0)::float8
                      FROM ci_runs) AS ci_concluded,
                   (SELECT count(*) FROM ci_runs WHERE status = 'running')::bigint
                       AS ci_running
            """)[0]
        return {"ci_seen": float(row.get("ci_seen") or 0.0),
                "ci_concluded": float(row.get("ci_concluded") or 0.0),
                "ci_running": int(row.get("ci_running") or 0)}

    def recent_runs(self, now_ts, since_ts, limit) -> list[dict]:
        """#3256 : les exécutions récentes pour la mesure du débit (#7650)."""
        return self.db.query(
            """
            SELECT repo, run_id, run_attempt, workflow, status, branch, work_item_id, url,
                   extract(epoch FROM
                       coalesce(rc.started_at, rc.created_at, rc.first_seen_at))::float8 AS started_ts,
                   extract(epoch FROM rc.concluded_at)::float8 AS concluded_ts
              FROM ci_runs rc
             WHERE coalesce(rc.concluded_at, to_timestamp(%s)) > to_timestamp(%s)
               AND coalesce(rc.started_at, rc.created_at, rc.first_seen_at) > to_timestamp(%s)
               AND coalesce(rc.started_at, rc.created_at, rc.first_seen_at) IS NOT NULL
             ORDER BY coalesce(rc.concluded_at, to_timestamp(%s)) DESC
             LIMIT %s
            """, (float(now_ts), float(since_ts), float(since_ts),
                  float(now_ts), int(limit)))

    def mark_started(self, repo, run_ids) -> None:
        if not run_ids:
            return
        self.db.execute(
            "UPDATE ci_runs SET started_at = now() WHERE repo = %%s AND run_id IN (%s)"
            " AND started_at IS NULL" % _ints(run_ids), (repo,))

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

    def running_on(self, branches) -> list[dict]:
        """#846 : tous dépôts confondus, les exécutions en cours d'une base
        (#3686 : hors dispatch d'une suite qui ne suit pas la base)."""
        if not branches:
            return []
        return self.db.query(
            """
            SELECT %s FROM ci_runs r
             WHERE branch IN (SELECT jsonb_array_elements_text(%%s::jsonb))
               AND status = 'running'
               AND %s
             ORDER BY repo, run_id
            """ % (RUN_COLUMNS, FOLLOWS_BASE), (json.dumps(sorted(branches)),))

    def conclude(self, repo, run_id, status) -> bool:
        from .... import merge_queue, train_events
        with self.db.transaction() as tx:
            rows = tx.query(
                """
                UPDATE ci_runs SET status = %s, concluded_at = now()
                 WHERE repo = %s AND run_id = %s AND status = 'running'
                RETURNING run_id, head_sha, branch
                """, (status, repo, int(run_id)))
            if rows and status in ("success", "failure", "cancelled"):
                # The GitHub integration branch does not encode its target.
                # Route only through the persisted exact source/head/branch;
                # neither a lot recipient nor GitHub's default PR base is used.
                subjects = tx.query(
                    "SELECT target_key, document->'plan'->>'target' AS target"
                    " FROM integration_trains"
                    " WHERE document->'plan'->>'source' = %s"
                    " AND document->'plan'->>'head' = %s"
                    " AND document->'plan'->>'branch' = %s",
                    (merge_queue.normalize_source("github.com/" + repo),
                     rows[0]["head_sha"], rows[0]["branch"]))
                for subject in subjects:
                    project, _, target = subject["target_key"].partition(":")
                    if target != subject["target"]:
                        continue
                    try:
                        train_events.emit(tx, kind="ci-terminal", project=project, target=target)
                    except ValueError:
                        # A legacy/malformed namespace is not a train subject.
                        pass
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
        # `run_attempt < excluded` : un événement d'un attempt antérieur (le
        # check run de l'attempt précédent, pas encore republié par la relance)
        # ne réécrit jamais une ligne de l'attempt courant — le témoin ne
        # recule pas (#901).
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
             WHERE ci_test_failures.run_attempt < excluded.run_attempt
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

    def base_failures(self, repo, branch, *, workflow="") -> dict:
        # strictement la même suite : les lignes antérieures à la migration
        # 0433, toutes de `tests.yml`, y sont nommées (#1110, R1 P2-2)
        # #1683 : la référence est la tête la plus récemment poussée, pas
        # l'exécution conclue le plus récemment — une exécution lente d'une
        # tête plus ancienne ne redevient pas la base. Pour chaque tête, sa
        # première observation, annulations comprises (un réessai ne la
        # rajeunit pas), mais le résultat de sa dernière exécution conclue.
        # #2064 : `created_at` de GitHub est à la seconde ; à observation
        # égale, le premier run_id de chaque tête départage les poussées.
        # Une nouvelle exécution d'une ancienne tête ne la rajeunit pas.
        rows = self.db.query(
            """
            WITH runs AS (
                SELECT run_id, head_sha, run_attempt, status, concluded_at,
                       min(coalesce(created_at, first_seen_at))
                           OVER (PARTITION BY head_sha) AS seen_at,
                       min(run_id) OVER (PARTITION BY head_sha) AS head_run
                  FROM ci_runs
                 WHERE repo = %s AND branch = %s AND workflow = %s
            ), last AS (
                SELECT run_id, head_sha, run_attempt FROM runs
                 WHERE status IN ('success', 'failure')
                 ORDER BY seen_at DESC, head_run DESC,
                          concluded_at DESC NULLS LAST, run_id DESC LIMIT 1
            )
            SELECT last.run_id, last.head_sha, f.test
              FROM last LEFT JOIN ci_test_failures f
                ON f.repo = %s AND f.run_id = last.run_id
               AND f.run_attempt = last.run_attempt
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
        # #3686 : la tête courante et la dernière exécution de chaque workflow
        # se lisent sans les dispatchs d'une suite qui ne suit pas la base
        return self.db.query(
            """
            WITH current AS (
                SELECT DISTINCT ON (repo, branch) repo, branch, head_sha
                  FROM ci_runs r
                 WHERE branch IN (SELECT jsonb_array_elements_text(%%s::jsonb))
                   AND status IN ('success', 'failure')
                   AND %s
                 ORDER BY repo, branch, run_id DESC
            )
            SELECT DISTINCT ON (repo, branch, workflow) %s
              FROM ci_runs r
             WHERE (repo, branch, head_sha) IN
                   (SELECT repo, branch, head_sha FROM current)
               AND status IN ('success', 'failure')
               AND %s
             ORDER BY repo, branch, workflow, concluded_at DESC, run_id DESC
            """ % (FOLLOWS_BASE, RUN_COLUMNS, FOLLOWS_BASE), (json.dumps(sorted(branches)),))

    # -- #845 : garde de la CI (runners et file) ---------------------------
    def observe_runners(self, repo, runners) -> None:
        ids = []
        for r in runners:
            if r.get("id") is None:
                continue
            ids.append(int(r["id"]))
            labels = r.get("labels")
            # #8031 : les étiquettes du runner, en ligne ou hors ligne, sont la
            # correspondance hôte → file qu'il peut servir. NULL = non relevé.
            # #8422 : `online` distingue un runner libre d'un runner d'une VM
            # arrêtée (un runner hors ligne n'est jamais busy) ; `last_busy_at`
            # garde le dernier relevé busy, que la fin du job efface de
            # `busy_since`. `state_seen_at` date ces deux faits, dans la même
            # écriture que `seen_at` : un veilleur N-1 rafraîchit `seen_at`
            # sans les nommer, et le lecteur voit alors l'écart (doute), jamais
            # une preuve de repos ou de retour.
            self.db.execute(
                """
                INSERT INTO ci_runner_state AS s
                    (repo, runner_id, name, busy, job, busy_since, stuck_since,
                     labels, online, last_busy_at, state_seen_at)
                VALUES (%s, %s, %s, %s, %s,
                        CASE WHEN %s THEN now() END,
                        CASE WHEN %s THEN now() END,
                        %s::jsonb, %s,
                        CASE WHEN %s THEN now() END, now())
                ON CONFLICT (repo, runner_id) DO UPDATE SET
                    name        = excluded.name,
                    busy        = excluded.busy,
                    job         = excluded.job,
                    busy_since  = CASE WHEN excluded.busy
                                       THEN coalesce(s.busy_since, now()) ELSE NULL END,
                    -- A job may have run and ended between the last current
                    -- scan and an N-1 write: the gap is not a measured rest.
                    -- The first current scan after it dates the rest from its
                    -- own instant; a steady current writer keeps the instant.
                    last_busy_at = CASE
                        WHEN excluded.busy THEN now()
                        WHEN s.seen_at IS DISTINCT FROM s.state_seen_at THEN now()
                        ELSE s.last_busy_at END,
                    stuck_since = CASE WHEN excluded.stuck_since IS NOT NULL
                                       THEN coalesce(s.stuck_since, now()) ELSE NULL END,
                    labels      = excluded.labels,
                    online      = excluded.online,
                    state_seen_at = now(),
                    seen_at     = now()
                """,
                (repo, int(r["id"]), r.get("name") or "", bool(r.get("busy")),
                 r.get("job"), bool(r.get("busy")), bool(r.get("stuck")),
                 json.dumps(sorted(labels)) if labels is not None else None,
                 bool(r.get("online")), bool(r.get("busy"))))
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

    def all_runners(self) -> list[dict]:
        """#8031 : tous les runners relevés, avec leurs étiquettes.

        En ligne ou hors ligne : GitHub les rend encore quand la VM est
        arrêtée, et c'est cette inscription qui nomme les étiquettes que
        l'hôte peut servir au réveil. #8422 : `online` dit si le runner est
        actuellement connecté, `last_busy_ts` date le dernier relevé où il
        était occupé (NULL s'il n'a jamais été vu busy), et `state_seen_ts`
        date l'écriture de ces deux faits (NULL ou différent de `seen_ts` :
        relevé d'un veilleur N-1, donc doute)."""
        return self.db.query(
            """
            SELECT repo, runner_id, name, busy, labels, online,
                   extract(epoch from seen_at)::float8       AS seen_ts,
                   extract(epoch from state_seen_at)::float8 AS state_seen_ts,
                   extract(epoch from last_busy_at)::float8  AS last_busy_ts
              FROM ci_runner_state
             ORDER BY repo, name
            """)

    def watch_failure(self, repo):
        """Count a failed repository pass, without fabricating a fresh queue scan."""
        rows = self.db.query("""
            INSERT INTO ci_queue_state AS q
                (repo, seen_at, watch_failures, watch_failure_since)
            VALUES (%s, 'epoch'::timestamptz, 1, now())
            ON CONFLICT (repo) DO UPDATE SET
                watch_failures = q.watch_failures + 1,
                watch_failure_since = coalesce(q.watch_failure_since, now())
            RETURNING watch_failures, (extract(epoch from watch_failure_since) * 1000000)::bigint AS since
        """, (repo,))
        return rows[0]

    def watch_success(self, repo):
        self.db.execute("""
            UPDATE ci_queue_state SET watch_failures = 0, watch_failure_since = NULL
            WHERE repo = %s
        """, (repo,))

    def observe_queue(self, repo, *, oldest_id, oldest_at, queued,
                      wait_published=False) -> None:
        """#8031 : `wait_published` estampille `wait_seen_at` dans la même
        écriture que `seen_at`, preuve que `ci_queue_wait` vient de cette
        passe. Sans lui, un veilleur N-1 rafraîchit la file de capacité sans
        publier les groupes : `wait_seen_at` reste NULL ou antérieur, et le
        lecteur doute au lieu de lire une file de réveil vide."""
        self.db.execute(
            """
            INSERT INTO ci_queue_state AS q
                (repo, oldest_id, oldest_at, queued, seen_at, wait_seen_at)
            VALUES (%s, %s, %s::timestamptz, %s, now(),
                    CASE WHEN %s THEN now() END)
            ON CONFLICT (repo) DO UPDATE SET
                oldest_id    = excluded.oldest_id,
                oldest_at    = excluded.oldest_at,
                queued       = excluded.queued,
                seen_at      = excluded.seen_at,
                wait_seen_at = CASE WHEN %s THEN excluded.wait_seen_at
                                    ELSE q.wait_seen_at END
            """,
            (repo, int(oldest_id) if oldest_id is not None else None, oldest_at,
             int(queued or 0), bool(wait_published), bool(wait_published)))

    def queued_oldest(self) -> list[dict]:
        return self.db.query(
            """
            SELECT q.repo, q.oldest_id, q.queued,
                   extract(epoch from q.oldest_at)::float8    AS oldest_ts,
                   extract(epoch from q.seen_at)::float8      AS seen_ts,
                   extract(epoch from q.wait_seen_at)::float8 AS wait_seen_ts,
                   extract(epoch from s.seen_at)::float8      AS scan_seen_ts
              FROM ci_queue_state AS q
              LEFT JOIN ci_queue_wait_scan AS s ON s.repo = q.repo
             ORDER BY q.repo
            """)

    def queue_wait(self) -> list[dict]:
        """#8031 : la file du réveil, groupée par jeu d'étiquettes demandées."""
        return self.db.query(
            """
            SELECT repo, labels, oldest_id, queued,
                   extract(epoch from oldest_at)::float8 AS oldest_ts,
                   extract(epoch from seen_at)::float8   AS seen_ts
              FROM ci_queue_wait
             ORDER BY repo, labels
            """)

    def wake_queue(self) -> list[dict]:
        """#8031 : file de capacité, stamp de scan et groupes en UNE lecture.

        Les trois tables sont jointes dans la même requête (même instantané) :
        un `observe_queue_wait` concurrent ne peut pas fournir des timestamps
        d'une génération avec les groupes d'une autre. Une ligne par groupe ;
        une file vide rend une ligne au `labels` NULL, avec le stamp du scan."""
        return self.db.query(
            """
            SELECT q.repo,
                   extract(epoch from q.oldest_at)::float8     AS oldest_ts,
                   q.queued                                    AS queue_queued,
                   extract(epoch from q.seen_at)::float8       AS seen_ts,
                   extract(epoch from q.wait_seen_at)::float8  AS wait_seen_ts,
                   extract(epoch from s.seen_at)::float8       AS scan_seen_ts,
                   w.labels,
                   extract(epoch from w.oldest_at)::float8     AS wait_oldest_ts,
                   w.queued                                    AS wait_queued,
                   extract(epoch from w.seen_at)::float8       AS group_seen_ts
              FROM ci_queue_state AS q
              LEFT JOIN ci_queue_wait_scan AS s ON s.repo = q.repo
              LEFT JOIN ci_queue_wait AS w ON w.repo = q.repo
             ORDER BY q.repo, w.labels
            """)

    def observe_queue_wait(self, repo, groups) -> None:
        """#8031 : remplace la file du réveil du dépôt par `groups`.

        Le veilleur publie ces groupes AVANT `ci_queue_state`, dont
        `wait_published=True` estampille `wait_seen_at`. Les groupes et
        `ci_queue_wait_scan` sont écrits dans le MÊME ordre SQL : le stamp
        existe même quand la file est vide. Une publication interrompue laisse
        le stamp de la file antérieur (groupes ou scan plus récents) ou NULL
        (veilleur N-1), donc le lecteur doute au lieu de lire une file
        fraîche aux groupes disparus. La suppression des groupes disparus et
        l'UPSERT des groupes présents ne laissent aucun état intermédiaire
        visible, et un groupe qui demeure est mis à jour sans violer sa clé."""
        payload = json.dumps([
            {"labels": sorted({str(label).strip().casefold()
                               for label in group.get("labels") or ()
                               if str(label).strip()}),
             "oldest_id": (int(group["oldest_id"])
                           if group.get("oldest_id") is not None else None),
             "oldest_at": group.get("oldest_at") or None,
             "queued": int(group.get("queued") or 0)}
            for group in groups])
        self.db.execute(
            """
            WITH fresh AS (
                SELECT labels, oldest_id, oldest_at, queued
                  FROM jsonb_to_recordset(%s::jsonb)
                       AS x(labels jsonb, oldest_id bigint, oldest_at timestamptz,
                            queued integer)
            ), gone AS (
                DELETE FROM ci_queue_wait AS old
                 WHERE old.repo = %s
                   AND NOT EXISTS (SELECT 1 FROM fresh
                                    WHERE fresh.labels = old.labels)
            ), scan AS (
                INSERT INTO ci_queue_wait_scan(repo, seen_at)
                VALUES (%s, now())
                ON CONFLICT (repo) DO UPDATE SET seen_at = excluded.seen_at
                RETURNING seen_at
            )
            INSERT INTO ci_queue_wait(repo, labels, oldest_id, oldest_at, queued, seen_at)
            SELECT %s, labels, oldest_id, oldest_at, queued, scan.seen_at
              FROM fresh, scan
            ON CONFLICT (repo, labels) DO UPDATE SET
                oldest_id = excluded.oldest_id,
                oldest_at = excluded.oldest_at,
                queued    = excluded.queued,
                seen_at   = excluded.seen_at
            """, (payload, repo, repo, repo))

    def prune_queue(self, repos) -> None:
        """#2957 : les relevés des dépôts hors du périmètre actif sont retirés.

        Sans cette purge, un dépôt sorti de `github.projects` garde sa ligne
        `ci_queue_state` : le repli de `github_observation` sur un hôte sans
        configuration continue de le surveiller et alerte à jamais de son
        silence. #8031 : la file groupée par étiquettes suit la même purge,
        sinon un réveil nommerait un dépôt sorti du périmètre. Sans périmètre,
        rien n'est retiré (on ne sait pas si les dépôts ont été retirés ou si
        la configuration locale est seulement absente)."""
        keys = sorted({str(repo).strip().casefold() for repo in repos
                       if str(repo).strip()})
        if not keys:
            return
        marks = ", ".join(["%s"] * len(keys))
        self.db.execute(
            "DELETE FROM ci_queue_wait WHERE lower(repo) NOT IN (%s)" % marks,
            tuple(keys))
        self.db.execute(
            "DELETE FROM ci_queue_wait_scan WHERE lower(repo) NOT IN (%s)" % marks,
            tuple(keys))
        self.db.execute(
            "DELETE FROM ci_queue_state WHERE lower(repo) NOT IN (%s)" % marks,
            tuple(keys))

    # -- #3344 : âge de la carte de mesure ----------------------------------
    def observe_map(self, repo, *, branch, run_id, head_sha, published_at) -> None:
        self.db.execute(
            """
            INSERT INTO ci_map_state AS m (repo, branch, run_id, head_sha,
                                           published_at, seen_at)
            VALUES (%s, %s, %s, %s, %s::timestamptz, now())
            ON CONFLICT (repo) DO UPDATE SET
                branch       = excluded.branch,
                run_id       = excluded.run_id,
                head_sha     = excluded.head_sha,
                published_at = excluded.published_at,
                seen_at      = now()
            """,
            (repo, branch, int(run_id) if run_id is not None else None,
             head_sha or None, published_at or None))

    def maps(self) -> list[dict]:
        return self.db.query(
            """
            SELECT repo, branch, run_id, head_sha,
                   extract(epoch from published_at)::float8 AS published_ts,
                   extract(epoch from seen_at)::float8      AS seen_ts
              FROM ci_map_state
             ORDER BY repo
            """)

    def forget_map(self, repo) -> None:
        self.db.execute("DELETE FROM ci_map_state WHERE lower(repo) = lower(%s)", (repo,))

    def prune_maps(self, repos) -> None:
        """Comme `prune_queue` (#2957) : un dépôt sorti du périmètre actif
        ne garde pas une carte qui alerterait à jamais ; sans périmètre, rien
        n'est retiré."""
        keys = sorted({str(repo).strip().casefold() for repo in repos
                       if str(repo).strip()})
        if not keys:
            return
        self.db.execute(
            "DELETE FROM ci_map_state WHERE lower(repo) NOT IN (%s)"
            % ", ".join(["%s"] * len(keys)), tuple(keys))

    def expect_disk(self, repo, *, host, volume) -> None:
        # #5633 : `seen_at` date la dernière apparition de la VM dans les jobs
        # du workflow, pas la dernière mesure : un job en attente ou une sonde
        # en échec la repousse. `first_seen_at` est posé à l'insertion et ne
        # bouge plus : la grâce de première mesure ne se renouvelle pas à
        # chaque collecte. Une lecture ratée n'appelle pas cette méthode.
        self.db.execute(
            "INSERT INTO ci_disk_state (repo, host, volume) VALUES (%s, %s, %s) "
            "ON CONFLICT (repo, host, volume) DO UPDATE SET seen_at = now()",
            (repo, host, volume))

    def observe_disk(self, repo, *, host, volume, run_id, sampled_ts,
                     free_bytes, total_bytes) -> None:
        self.db.execute(
            """
            INSERT INTO ci_disk_state AS d
                (repo, host, volume, run_id, sampled_at, free_bytes, total_bytes)
            VALUES (%s, %s, %s, %s, to_timestamp(%s), %s, %s)
            ON CONFLICT (repo, host, volume) DO UPDATE SET
                run_id=excluded.run_id, sampled_at=excluded.sampled_at,
                free_bytes=excluded.free_bytes, total_bytes=excluded.total_bytes,
                seen_at=now()
            WHERE d.sampled_at IS NULL OR excluded.sampled_at > d.sampled_at
            """, (repo, host, volume, run_id, sampled_ts, free_bytes, total_bytes))

    def disks(self) -> list[dict]:
        return self.db.query(
            "SELECT repo, host, volume, run_id, free_bytes, total_bytes, "
            "extract(epoch from sampled_at)::float8 AS sampled_ts, "
            "extract(epoch from first_seen_at)::float8 AS first_seen_ts, "
            "extract(epoch from seen_at)::float8 AS seen_ts "
            "FROM ci_disk_state ORDER BY repo, host, volume")

    def forget_disks(self, repo, hosts, *, older_than_s) -> None:
        # #5633 : ne garde que les VM encore présentes dans les jobs du
        # workflow ; l'âge sur `seen_at` protège les pannes de lecture (qui ne
        # rafraîchissent pas la date) et une VM encore attendue (job en file).
        keep = sorted({str(host) for host in hosts if str(host).strip()})
        if not keep:
            return
        self.db.execute(
            "DELETE FROM ci_disk_state WHERE lower(repo) = lower(%s) "
            "AND host <> ALL(%s) "
            "AND seen_at < now() - (%s * interval '1 second')",
            (repo, keep, float(older_than_s)))

    def prune_disks(self, repos) -> None:
        keys = sorted({str(repo).strip().casefold() for repo in repos if str(repo).strip()})
        if keys:
            self.db.execute("DELETE FROM ci_disk_state WHERE lower(repo) NOT IN (%s)"
                            % ", ".join(["%s"] * len(keys)), tuple(keys))

    def observe_disk_refusal(self, repo, *, host, event_id, payload) -> None:
        self.db.execute(
            "INSERT INTO ci_disk_refusals (repo, host, event_id, payload) "
            "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
            (repo, host, event_id, json.dumps(payload)))

    def disk_refusals(self, repo) -> list[dict]:
        # #5755 : un signal de fork n'appelle aucun courrier ; il ne fait
        # que lever l'alerte, et part avec l'âge comme un signal acquitté.
        self.db.execute("DELETE FROM ci_disk_refusals WHERE (message_id IS NOT NULL "
                        "OR payload->>'fork' = 'true') "
                        "AND seen_at < now() - interval '8 days'")
        return self.db.query(
            "SELECT repo, host, event_id, payload FROM ci_disk_refusals "
            "WHERE repo = %s AND message_id IS NULL "
            "AND payload->>'fork' IS DISTINCT FROM 'true' ORDER BY seen_at, event_id "
            "LIMIT 100 FOR UPDATE SKIP LOCKED", (repo,))

    def set_disk_refusal_message(self, repo, *, host, event_id, message_id) -> None:
        self.db.execute(
            "UPDATE ci_disk_refusals SET message_id = %s "
            "WHERE repo = %s AND host = %s AND event_id = %s",
            (message_id, repo, host, event_id))

    def dismiss_disk_refusal(self, repo, *, host, event_id) -> None:
        # #5754 : 0 = consommée sans courrier (aucun identifiant de message
        # n'est nul) ; la purge des acquittés après 8 jours s'applique comme
        # pour un courrier réel.
        self.db.execute(
            "UPDATE ci_disk_refusals SET message_id = 0 "
            "WHERE repo = %s AND host = %s AND event_id = %s",
            (repo, host, event_id))

    def claim_disk_dispatch(self, repo, *, now, interval_s) -> bool:
        return bool(self.db.query(
            "INSERT INTO ci_disk_dispatch AS d (repo, requested_at) "
            "VALUES (%s, to_timestamp(%s)) ON CONFLICT (repo) DO UPDATE "
            "SET requested_at = excluded.requested_at "
            "WHERE d.requested_at <= excluded.requested_at - (%s * interval '1 second') "
            "RETURNING repo", (repo, float(now), float(interval_s))))

    def disk_signals(self) -> list[dict]:
        rows = self.db.query("SELECT repo, host, payload FROM ci_disk_refusals "
                             "WHERE seen_at >= now() - interval '7 days'")
        return [dict(row['payload'], repo=row['repo'], host=row['host'],
                     refusal=not row['payload'].get('signal')) for row in rows]

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

    def observe_guard_run(self, repo, *, run_id, attempt, status, queued,
                          oldest_id, oldest_at, runners) -> None:
        """Écrit le relevé d'une exécution (#845) : trace de la fenêtre en
        cours, sans relecture — depuis le retrait du lecteur (B1), chaque
        relevé relit toutes les exécutions au lieu de réutiliser ce cache."""
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
