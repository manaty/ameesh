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

from ameesh import work_lifecycle as lifecycle

from .. import interface
from .work import item_columns

#: états d'action qui attendent encore quelque chose (non terminaux)
_OPEN_ACTION_STATES = ("proposed", "approved", "launched", "unknown")


_ACTION_COLUMNS = """
    action_id, project, work_item, proposed_by, connector, operation, target, class,
    state, attempts, last_actor, auth_kind, auth_approver, approvers,
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
        # L61 : lecture de `reviewer` sans sonde, cette commande est bornée
        sql = ("SELECT %s, count(*) OVER ()::bigint AS total FROM work_items"
               f" WHERE (state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
               "        OR updated_at >= to_timestamp(%%s))"
               % item_columns(self.db, "work_items"))
        params: list = [float(since_ts)]
        if project:
            sql += " AND (app = %s OR workstream = %s)"
            params += [project, project]
        # borne de rendu : les ouverts d'abord, puis les plus récents
        sql += (f" ORDER BY (state IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')), updated_at DESC, id DESC"
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

    def lot_transitions(self, item_ids) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        # Seules les lignes où l'état change sortent : le texte des notes
        # (des mégaoctets sur une liste de lots) n'est ni lu ni transféré.
        return self.db.query(
            "SELECT id, work_item_id, state, actor,"
            "       extract(epoch from created_at)::float8 AS created_ts"
            "  FROM (SELECT id, work_item_id, state, actor, created_at,"
            "               lag(state) OVER (PARTITION BY work_item_id"
            "                               ORDER BY id) AS prev"
            "          FROM work_item_events WHERE work_item_id IN (%s)) marked"
            " WHERE prev IS DISTINCT FROM state"
            " ORDER BY id" % _marks(ids), tuple(ids))

    def lot_event_last(self, item_ids) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        return self.db.query(
            "SELECT work_item_id,"
            "       extract(epoch from max(created_at))::float8 AS last_ts"
            "  FROM work_item_events WHERE work_item_id IN (%s)"
            " GROUP BY work_item_id" % _marks(ids), tuple(ids))

    def packet_exits(self, *, since_ts) -> list[dict]:
        from ...slo import PACKET_COMPOSITION_NOTE, VEHICLE_EXIT_MARK

        return self.db.query(
            "SELECT id, work_item_id, note,"
            "       extract(epoch from created_at)::float8 AS created_ts"
            "  FROM work_item_events"
            " WHERE created_at >= to_timestamp(%s) AND note LIKE %s"
            " ORDER BY id",
            (float(since_ts), PACKET_COMPOSITION_NOTE + "%" + VEHICLE_EXIT_MARK + "%"))

    def lot_hold_events(self, item_ids, patterns) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        wanted = [str(pattern) for pattern in patterns]
        if any(not pattern for pattern in wanted):
            raise ValueError("empty hold pattern")
        merged: dict[tuple[int, int], dict] = {}
        for ev in self.lot_transitions(ids):
            merged[(int(ev["work_item_id"]), int(ev["id"]))] = ev
        if wanted:
            # #1664 : seules les notes hold/levée sortent (motifs de
            # planned_waits, ARE) : le texte des notes ordinaires (des
            # mégaoctets sur une liste de lots) n'est ni lu ni transféré. La
            # ligne à note l'emporte sur la transition de même id.
            where = " OR ".join(["btrim(note, %s) ~* %s"] * len(wanted))
            for ev in self.db.query(
                    "SELECT id, work_item_id, state, note, actor,"
                    "       extract(epoch from created_at)::float8 AS created_ts"
                    "  FROM work_item_events"
                    " WHERE work_item_id IN (%s) AND note IS NOT NULL AND (%s)"
                    " ORDER BY id" % (_marks(ids), where),
                    tuple(ids) + tuple(value for pattern in wanted
                                       for value in (interface.HOLD_NOTE_WHITESPACE, pattern))):
                merged[(int(ev["work_item_id"]), int(ev["id"]))] = ev
        return sorted(merged.values(), key=lambda e: int(e["id"]))

    def lot_milestones(self, item_ids) -> list[dict]:
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        return self.db.query(
            "SELECT id, work_item_id, kind, actor, verdict, sha, note,"
            "       extract(epoch from at)::float8 AS at_ts"
            "  FROM work_item_milestones WHERE work_item_id IN (%s)"
            " ORDER BY at, id" % _marks(ids), tuple(ids))

    def packages(self) -> list[dict]:
        return self.db.query(
            "SELECT id, kind, title, parent, responsible, team, status, canon_ref,"
            # L96 : dates du canon et dates posées dans ameesh (jours ISO)
            "       to_char(start_on, 'YYYY-MM-DD') AS start_on,"
            "       to_char(end_on, 'YYYY-MM-DD') AS end_on,"
            "       to_char(delivery_on, 'YYYY-MM-DD') AS delivery_on,"
            "       to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
            "       to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
            "       to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery,"
            "       planned_source, planned_by"
            "  FROM work_packages WHERE present ORDER BY id")

    def package_items(self) -> list[dict]:
        return self.db.query(
            "SELECT id, package_id, state, close_reason, app, workstream, external_executor,"
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
        sql = f"""
            SELECT r.name, r.workstream, r.workstream AS chantier,
                   r.team, r.harness, r.host, r.model, r.provider,
                   r.credential_mode, r.status, r.status_text, r.current_prompt,
                   r.operator_paused, r.operator_pause_reason, r.operator_pause_by,
                   r.operator_pause_for,
                   extract(epoch from r.operator_pause_at)::float8 AS operator_pause_ts,
                   extract(epoch from r.operator_pause_until)::float8 AS operator_pause_until_ts,
                   (r.pending_prompt IS NOT NULL) AS has_pending_prompt,
                   r.turns, r.lease_owner, r.mode, r.stop_reason, r.responsible,
                   (r.lease_owner IS NOT NULL AND r.lease_expires_at > now()) AS lease_live,
                   extract(epoch from r.last_turn_at)::float8 AS last_turn_ts,
                   extract(epoch from r.last_seen)::float8    AS last_seen_ts,
                   extract(epoch from r.updated_at)::float8   AS updated_ts,
                   extract(epoch from p.created_at)::float8   AS turn_started_ts,
                   p.turn  AS turn_label,
                   p.model AS turn_model,
                   (SELECT count(*) FROM agent_mailbox m
                     WHERE m.recipient = r.name AND m.delivered_at IS NULL)::bigint AS unread,
                   -- §4 (owner 14/10/2026) : l'en-cours de l'agent, tous
                   -- projets confondus (le plafond est global)
                   (SELECT count(*) FROM work_items w
                     WHERE w.assignee = r.name AND w.state = '{lifecycle.IN_PROGRESS}')::int AS builds
              FROM agent_registry r
              LEFT JOIN spend_pending p ON p.agent = r.name
        """
        params: tuple = ()
        if project:
            # L243 : l'agent appartient à tous ses projets — `projects` déclaré
            # (0388), sinon son équipe, sinon son `workstream`
            sql += (" WHERE %s = any(coalesce(r.projects,"
                    " case when coalesce(btrim(r.team), '') <> '' then array[btrim(r.team)]"
                    "      when coalesce(btrim(r.workstream), '') <> ''"
                    "           then array[btrim(r.workstream)]"
                    "      else array[]::text[] end))")
            params = (project,)
        sql += " ORDER BY r.name"
        return self.db.query(sql, params)

    def costs(self, *, since_ts, agents) -> list[dict]:
        # L95 (0043) : les lignes écartées par `cost correct` ne comptent pas
        clauses = ["recorded_at >= least(to_timestamp(%s), now() - interval '24 hours')",
                   "void_reason IS NULL"]
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
                   count(*) FILTER (WHERE %s AND usd = 0 AND input_tokens = 0
                                     AND cached_input_tokens = 0
                                     AND output_tokens = 0)::bigint AS failed_turns,
                   coalesce(sum(input_tokens) FILTER (WHERE %s), 0)::bigint AS input_tokens,
                   coalesce(sum(cached_input_tokens) FILTER (WHERE %s), 0)::bigint
                       AS cached_input_tokens,
                   coalesce(sum(output_tokens) FILTER (WHERE %s), 0)::bigint AS output_tokens,
                   extract(epoch from max(recorded_at))::float8 AS last_ts
              FROM turn_costs
             WHERE %s
             GROUP BY agent, harness, coalesce(model, '')
             ORDER BY agent, harness, 3
            """ % (win, win, win, win, win, win, " AND ".join(clauses)),
            tuple([float(since_ts)] * 6 + params))

    def ci_suite(self, *, since_ts, limit) -> list[dict]:
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
