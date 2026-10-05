# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : lectures de la vue d'avancement (`ameesh progress`, L24).

Lectures seules sur les tables existantes (`work_items`, `work_item_events`,
`work_item_milestones` (L10),
`actions`, `agent_registry`, `spend_pending`, `agent_mailbox`, `turn_costs`) :
le lot L24 n'a pas de migration. Les listes de noms ou d'ids sont développées
en marqueurs `%s` (aucun tableau lié : même SQL pour psql et psycopg) ; un
filtre absent n'est jamais un paramètre NULL typé par le serveur.
"""
from __future__ import annotations

from .. import interface
from .work import ITEM_COLUMNS

#: états d'action qui attendent encore quelque chose (non terminaux)
_OPEN_ACTION_STATES = ("proposed", "approved", "launched", "unknown")


_ACTION_COLUMNS = """
    action_id, project, work_item, proposed_by, connector, operation, target, class,
    state, attempts, last_actor, auth_approver, approvers,
    extract(epoch from created_at)::float8  AS created_ts,
    extract(epoch from updated_at)::float8  AS updated_ts,
    extract(epoch from approved_at)::float8 AS approved_ts,
    extract(epoch from launched_at)::float8 AS launched_ts,
    extract(epoch from finished_at)::float8 AS finished_ts
"""


def _marks(values) -> str:
    return ", ".join(["%s"] * len(values))


class Progress(interface.Progress):

    def lots(self, *, since_ts, project, limit) -> list[dict]:
        sql = ("SELECT %s, count(*) OVER ()::bigint AS total FROM work_items"
               " WHERE (state NOT IN ('merged', 'promoted', 'closed')"
               "        OR updated_at >= to_timestamp(%%s))" % ITEM_COLUMNS)
        params: list = [float(since_ts)]
        if project:
            sql += " AND (app = %s OR workstream = %s)"
            params += [project, project]
        # borne de rendu : les ouverts d'abord, puis les plus récents
        sql += (" ORDER BY (state IN ('merged', 'promoted', 'closed')), updated_at DESC, id DESC"
                " LIMIT %s")
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def lot_events(self, item_ids) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        return self.db.query(
            "SELECT id, work_item_id, state, note, actor,"
            "       extract(epoch from created_at)::float8 AS created_ts"
            "  FROM work_item_events WHERE work_item_id IN (%s)"
            " ORDER BY id" % _marks(ids), tuple(ids))

    def lot_milestones(self, item_ids) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        return self.db.query(
            "SELECT id, work_item_id, kind, actor, verdict, sha,"
            "       extract(epoch from at)::float8 AS at_ts"
            "  FROM work_item_milestones WHERE work_item_id IN (%s)"
            " ORDER BY at, id" % _marks(ids), tuple(ids))

    def packages(self) -> list[dict]:
        return self.db.query(
            "SELECT id, kind, title, parent, responsible, team, status, canon_ref"
            "  FROM work_packages WHERE present ORDER BY id")

    def package_items(self) -> list[dict]:
        return self.db.query(
            "SELECT id, package_id, state, close_reason,"
            "       extract(epoch from updated_at)::float8 AS updated_ts"
            "  FROM work_items WHERE package_id IS NOT NULL ORDER BY id")

    def lot_messages(self, item_ids) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        return self.db.query(
            "SELECT work_item_id::bigint AS work_item_id,"
            "       extract(epoch from max(created_at))::float8 AS last_ts"
            "  FROM agent_mailbox WHERE work_item_id IN (%s)"
            " GROUP BY work_item_id" % _marks(ids), tuple(str(i) for i in ids))

    def lot_actions(self, item_ids) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        return self.db.query(
            "SELECT %s FROM actions WHERE work_item IN (%s)"
            " ORDER BY created_at, action_id" % (_ACTION_COLUMNS, _marks(ids)), tuple(ids))

    def actions(self, *, since_ts, project, limit) -> list[dict]:
        open_marks = _marks(_OPEN_ACTION_STATES)
        scope = "(updated_at >= to_timestamp(%%s) OR state IN (%s))" % open_marks
        params: list = [float(since_ts), *_OPEN_ACTION_STATES]
        if project:
            scope += " AND project = %s"
            params.append(project)
        params += [*_OPEN_ACTION_STATES, int(limit)]
        return self.db.query(
            "SELECT %s, count(*) OVER ()::bigint AS total FROM actions WHERE %s"
            " ORDER BY (state NOT IN (%s)), updated_at DESC, action_id DESC LIMIT %%s"
            % (_ACTION_COLUMNS, scope, open_marks), tuple(params))

    def agents(self, project) -> list[dict]:
        sql = """
            SELECT r.name, r.chantier, r.team, r.harness, r.host, r.model, r.provider,
                   r.credential_mode, r.status, r.status_text, r.current_prompt,
                   (r.pending_prompt IS NOT NULL) AS has_pending_prompt,
                   r.turns, r.lease_owner,
                   (r.lease_owner IS NOT NULL AND r.lease_expires_at > now()) AS lease_live,
                   extract(epoch from r.last_turn_at)::float8 AS last_turn_ts,
                   extract(epoch from r.last_seen)::float8    AS last_seen_ts,
                   extract(epoch from r.updated_at)::float8   AS updated_ts,
                   extract(epoch from p.created_at)::float8   AS turn_started_ts,
                   p.turn  AS turn_label,
                   p.model AS turn_model,
                   (SELECT count(*) FROM agent_mailbox m
                     WHERE m.recipient = r.name AND m.delivered_at IS NULL)::bigint AS unread
              FROM agent_registry r
              LEFT JOIN spend_pending p ON p.agent = r.name
        """
        params: tuple = ()
        if project:
            sql += " WHERE r.chantier = %s OR r.team = %s"
            params = (project, project)
        sql += " ORDER BY r.name"
        return self.db.query(sql, params)

    def costs(self, *, since_ts, agents) -> list[dict]:
        clauses = ["recorded_at >= least(to_timestamp(%s), now() - interval '24 hours')"]
        params: list = [float(since_ts)]
        if agents is not None:
            names = list(agents)
            if not names:
                return []
            clauses.append("agent IN (%s)" % _marks(names))
            params += names
        win = "recorded_at >= to_timestamp(%s)"
        return self.db.query(
            """
            SELECT agent, harness, coalesce(model, '') AS model,
                   coalesce(sum(usd) FILTER (WHERE %s), 0)::float8 AS usd_window,
                   coalesce(sum(usd) FILTER (WHERE recorded_at >= now() - interval '1 hour'),
                            0)::float8 AS usd_1h,
                   coalesce(sum(usd) FILTER (WHERE recorded_at >= now() - interval '24 hours'),
                            0)::float8 AS usd_24h,
                   count(*) FILTER (WHERE %s)::bigint AS turns,
                   coalesce(sum(input_tokens) FILTER (WHERE %s), 0)::bigint AS input_tokens,
                   coalesce(sum(cached_input_tokens) FILTER (WHERE %s), 0)::bigint
                       AS cached_input_tokens,
                   coalesce(sum(output_tokens) FILTER (WHERE %s), 0)::bigint AS output_tokens,
                   extract(epoch from max(recorded_at))::float8 AS last_ts
              FROM turn_costs
             WHERE %s
             GROUP BY agent, harness, coalesce(model, '')
             ORDER BY agent, harness, 3
            """ % (win, win, win, win, win, " AND ".join(clauses)),
            tuple([float(since_ts)] * 5 + params))
