# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : ressources des hôtes, orphelins et visibilité (lot L31,
décision 0028, migration 0029).

Tout le SQL de L31 est ici : relevés `host_resources`, rattachement des
ressources d'un tour (`turn_resources`) et cache court de la règle de
visibilité (`visibility_checks`). Les modules métier (`ameesh.resources`,
`ameesh.orphans`, `ameesh.visibility`) ne portent que les décisions ; le SQL
reste dans l'interface de stockage (spec §10).

Les instants sont rendus en secondes epoch (`*_ts`), l'heure étant celle de
la base (`now()`) sauf quand un test la fournit.
"""
from __future__ import annotations

import json
from typing import Any

from .. import interface

#: une liste de textes passée en paramètre : `NULL` si vide/None, sinon un
#: JSON que l'instruction convertit en `text[]`
_ARRAY_SQL = "ARRAY(SELECT jsonb_array_elements_text(%s::jsonb))"


def _array(values) -> str | None:
    if values is None:
        return None
    return json.dumps(list(values))

#: colonnes d'un relevé de ressources, dans l'ordre de lecture
READING_COLUMNS = (
    "id, host, extract(epoch from sampled_at)::float8 AS sampled_ts, "
    "mem_available_bytes, swap_used_bytes, load1, cpu_count, disk_free_bytes, "
    "disk_path, turns_in_progress, tmp_path, tmp_fstype, tmp_size_bytes, tmp_used_bytes, "
    "on_ac, battery_percent"
)

#: colonnes d'une ressource de tour
TURN_COLUMNS = (
    "id, turn_id, agent, host, pgid, label, containers, "
    "extract(epoch from started_at)::float8 AS started_ts, "
    "extract(epoch from ended_at)::float8 AS ended_ts, status"
)


class Hosts(interface.HostResources):

    def record(self, reading) -> dict:
        rows = self.db.query(
            """
            INSERT INTO host_resources
                (host, mem_available_bytes, swap_used_bytes, load1, cpu_count,
                 disk_free_bytes, disk_path, turns_in_progress,
                 tmp_path, tmp_fstype, tmp_size_bytes, tmp_used_bytes,
                 on_ac, battery_percent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING """ + READING_COLUMNS,
            (reading.get("host") or "", reading.get("mem_available_bytes"),
             reading.get("swap_used_bytes"), reading.get("load1"),
             reading.get("cpu_count"), reading.get("disk_free_bytes"),
             reading.get("disk_path"), reading.get("turns_in_progress"),
             reading.get("tmp_path"), reading.get("tmp_fstype"),
             reading.get("tmp_size_bytes"), reading.get("tmp_used_bytes"),
             reading.get("on_ac"), reading.get("battery_percent")),
        )
        # Historique court : au-delà de sept jours, la ligne n'a plus d'usage
        # et la table ne doit pas grandir sans fin.
        self.db.execute(
            "DELETE FROM host_resources"
            " WHERE sampled_at < now() - interval '7 days'", ())
        return rows[0]

    def latest(self, host) -> dict | None:
        rows = self.db.query(
            "SELECT " + READING_COLUMNS + " FROM host_resources WHERE host = %s"
            " ORDER BY sampled_at DESC, id DESC LIMIT 1", (host,))
        return rows[0] if rows else None

    def history(self, host, limit) -> list[dict]:
        rows = self.db.query(
            "SELECT " + READING_COLUMNS + " FROM host_resources WHERE host = %s"
            " ORDER BY sampled_at DESC, id DESC LIMIT %s", (host, int(limit)))
        return list(reversed(rows))

    def current(self, host) -> list[dict]:
        params: list[Any] = []
        where = ""
        if host:
            where = " WHERE host = %s"
            params.append(host)
        rows = self.db.query(
            "SELECT DISTINCT ON (host) " + READING_COLUMNS + " FROM host_resources"
            + where + " ORDER BY host, sampled_at DESC, id DESC", tuple(params))
        return rows

    def usage(self, host, since_s) -> dict:
        rows = self.db.query(
            """
            SELECT count(*)::int AS samples,
                   extract(epoch from min(sampled_at))::float8 AS first_ts,
                   extract(epoch from max(sampled_at))::float8 AS last_ts,
                   max(load1 / nullif(cpu_count, 0))::float8 AS max_load_per_cpu,
                   avg(load1 / nullif(cpu_count, 0))::float8 AS avg_load_per_cpu,
                   max(turns_in_progress)::int AS max_turns,
                   avg(turns_in_progress)::float8 AS avg_turns
              FROM host_resources
             WHERE host = %s AND sampled_at >= now() - make_interval(secs => %s)
            """, (host, float(since_s)))
        return rows[0] if rows else {"samples": 0}

    def turns_in_progress(self, host) -> int:
        rows = self.db.query(
            "SELECT count(*)::int AS n FROM agent_registry"
            " WHERE host = %s AND status = 'running'"
            "   AND lease_expires_at > clock_timestamp()", (host,))
        return int(rows[0]["n"]) if rows else 0


class TurnResources(interface.TurnResources):

    def open_turn(self, turn_id, agent, host, *, pgid, label, containers=None) -> None:
        self.db.query(
            """
            INSERT INTO turn_resources (turn_id, agent, host, pgid, label, containers)
            VALUES (%s, %s, %s, %s, %s, """ + _ARRAY_SQL + """)
            ON CONFLICT (turn_id) DO UPDATE SET
                agent = excluded.agent, host = excluded.host,
                pgid = excluded.pgid, label = excluded.label,
                containers = coalesce(excluded.containers, turn_resources.containers)
            RETURNING id
            """,
            (turn_id, agent, host, None if pgid is None else int(pgid), label,
             _array(containers)),
        )

    def close_turn(self, turn_id, *, orphan, containers=None) -> None:
        self.db.query(
            "UPDATE turn_resources SET ended_at = now(),"
            " status = CASE WHEN %s THEN 'orphan' ELSE 'done' END,"
            " containers = coalesce(nullif(" + _ARRAY_SQL + ", '{}'), containers)"
            " WHERE turn_id = %s AND status = 'running' RETURNING id",
            (bool(orphan), _array(containers), turn_id),
        )

    def mark_orphan(self, turn_id, containers=None) -> None:
        self.db.query(
            "UPDATE turn_resources SET status = 'orphan',"
            " ended_at = coalesce(ended_at, now()),"
            " containers = coalesce(nullif(" + _ARRAY_SQL + ", '{}'), containers)"
            " WHERE turn_id = %s AND status <> 'done' RETURNING id",
            (_array(containers), turn_id),
        )

    def get(self, turn_id) -> dict | None:
        rows = self.db.query(
            "SELECT " + TURN_COLUMNS + " FROM turn_resources WHERE turn_id = %s",
            (turn_id,))
        return rows[0] if rows else None

    def open_by_agent(self, agent) -> list[dict]:
        return self.db.query(
            "SELECT " + TURN_COLUMNS + " FROM turn_resources"
            " WHERE agent = %s AND status = 'running'"
            " ORDER BY started_at DESC, id DESC", (agent,))

    def orphans(self, host=None, limit=50) -> list[dict]:
        where, params = "", []
        if host:
            where = " WHERE host = %s"
            params.append(host)
        params.append(int(limit))
        return self.db.query(
            "SELECT " + TURN_COLUMNS + " FROM turn_resources" + where +
            (" AND" if where else " WHERE") + " status = 'orphan'"
            " ORDER BY started_at DESC, id DESC LIMIT %s", tuple(params))

    def stale_running(self, older_than_s, host=None) -> list[dict]:
        where, params = "", [float(older_than_s)]
        if host:
            where = " AND host = %s"
            params.append(host)
        return self.db.query(
            "SELECT " + TURN_COLUMNS + " FROM turn_resources"
            " WHERE status = 'running'"
            "   AND started_at < now() - make_interval(secs => %s)" + where +
            " ORDER BY started_at, id", tuple(params))


class Visibility(interface.Visibility):

    def cached(self, persona, host, now_ts, context) -> dict | None:
        """Un verdict n'est réutilisé que pour le MÊME contexte (dépôt+forge,
        humains requis, comptes) et s'il n'est pas expiré."""
        rows = self.db.query(
            "SELECT persona, host, ok, diagnostic, repository, context,"
            " extract(epoch from checked_at)::float8 AS checked_ts,"
            " extract(epoch from expires_at)::float8 AS expires_ts"
            " FROM visibility_checks"
            " WHERE persona = %s AND host = %s AND context = %s"
            "   AND expires_at > to_timestamp(%s)",
            (persona, host, context or "", float(now_ts)))
        return rows[0] if rows else None

    def put(self, persona, host, *, ok, diagnostic, repository, context, ttl_s) -> dict:
        rows = self.db.query(
            """
            INSERT INTO visibility_checks
                (persona, host, ok, diagnostic, repository, context, checked_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, now(), now() + make_interval(secs => %s))
            ON CONFLICT (persona, host) DO UPDATE SET
                ok = excluded.ok, diagnostic = excluded.diagnostic,
                repository = excluded.repository, context = excluded.context,
                checked_at = excluded.checked_at, expires_at = excluded.expires_at
            RETURNING persona, host, ok, diagnostic, repository, context,
                      extract(epoch from checked_at)::float8 AS checked_ts,
                      extract(epoch from expires_at)::float8 AS expires_ts
            """,
            (persona, host, bool(ok), diagnostic or "", repository, context or "",
             float(ttl_s)))
        return rows[0]

    def purge(self, now_ts) -> int:
        rows = self.db.query(
            "DELETE FROM visibility_checks WHERE expires_at <= to_timestamp(%s)"
            " RETURNING persona", (float(now_ts),))
        return len(rows)
