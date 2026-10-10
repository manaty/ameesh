# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : exploitation (lot L26, migration 0027).

Réglages d'agent, lot de la session, redémarrage, lectures enrichies de
`ameesh list --json` et `ameesh alerts`, usage par tour (`turn_costs`, lu
tel quel : le calcul des coûts n'est pas touché), historique des jauges de
forfait et soldes d'un fournisseur payé au token.

Les écritures sous bail suivent le motif du registre : verrou de ligne pris
d'abord (`FOR UPDATE`), échéance recontrôlée avec `clock_timestamp()` dans
l'instruction qui écrit. Les listes d'ids sont développées en marqueurs `%s`
(même SQL pour psql et psycopg).
"""
from __future__ import annotations

from typing import Any

from .. import interface

#: états de lot terminés (le reste est « ouvert »)
_CLOSED = "('merged', 'promoted', 'closed')"   # closed : abandonné ou remplacé (L29)


def _sans_compte_nul(rows: list[dict]) -> list[dict]:
    """`account` n'apparaît que s'il est renseigné (L30) : les relevés d'un hôte
    sans comptes gardent exactement la forme de L26 (`ameesh-gauges/1`,
    `ameesh-balance/1`)."""
    for row in rows:
        if row.get("account") is None:
            row.pop("account", None)
    return rows


def _marks(values) -> str:
    return ", ".join(["%s"] * len(values))


class Operations(interface.Operations):

    # -- réglages ------------------------------------------------------------
    def set_settings(self, name, values) -> bool:
        cols = [key for key in values if key in self.SETTINGS]
        if not cols:
            return bool(self.db.query("SELECT 1 AS ok FROM agent_registry WHERE name = %s",
                                      (name,)))
        sets = ", ".join("%s = nullif(%%s, '')%s" % (
            col, "::bigint" if col in self.INTEGER_SETTINGS else "") for col in cols)
        params: list[Any] = [values[col] if values[col] is not None else "" for col in cols]
        rows = self.db.query(
            "UPDATE agent_registry SET " + sets + ", updated_at = now()"
            " WHERE name = %s RETURNING name", tuple(params + [name]))
        return bool(rows)

    def set_session_work_item(self, name, owner, epoch, work_item) -> bool:
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET session_work_item = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
            RETURNING r.name
            """,
            (name, work_item, owner, int(epoch)),
        )
        return bool(rows)

    # -- lectures d'exploitation --------------------------------------------
    def listing(self) -> list[dict]:
        return self.db.query(
            """
            SELECT r.name, r.chantier, r.team, r.harness, r.host, r.model, r.effort, r.tier,
                   r.session_policy, r.context_max_tokens, r.turn_max_seconds, r.turn_mail_max,
                   r.session_id, r.session_work_item,
                   r.status, r.status_text, r.current_prompt, r.lease_owner,
                   r.mode, r.stop_reason, r.responsible,
                   r.last_error,  -- L106 : le détail de « hôte non prêt »
                   (r.pending_prompt IS NOT NULL) AS has_pending_prompt,
                   (r.lease_owner IS NOT NULL
                    AND r.lease_expires_at > clock_timestamp()) AS lease_live,
                   extract(epoch from r.lease_expires_at)::float8 AS lease_expires_ts,
                   extract(epoch from r.status_since)::float8     AS status_since_ts,
                   extract(epoch from r.last_turn_at)::float8     AS last_turn_ts,
                   extract(epoch from r.updated_at)::float8       AS updated_ts,
                   extract(epoch from r.restart_requested_at)::float8 AS restart_requested_ts,
                   extract(epoch from p.created_at)::float8       AS turn_started_ts,
                   p.turn AS turn_label,
                   coalesce(m.n, 0)::bigint AS unread,
                   extract(epoch from m.oldest)::float8 AS oldest_unread_ts,
                   sw.title AS session_lot_title, sw.state AS session_lot_state,
                   aw.id AS assigned_lot_id, aw.title AS assigned_lot_title,
                   aw.state AS assigned_lot_state,
                   -- L60 : Codex compte déjà le cache dans input_tokens
                   (CASE WHEN t.harness = 'codex'
                         THEN greatest(t.input_tokens, t.cached_input_tokens)
                         ELSE t.input_tokens + t.cached_input_tokens
                    END)::bigint AS last_turn_reread_tokens,
                   t.session AS last_turn_session,
                   extract(epoch from t.recorded_at)::float8 AS last_turn_recorded_ts
              FROM agent_registry r
              LEFT JOIN spend_pending p ON p.agent = r.name
              LEFT JOIN LATERAL (
                  SELECT count(*) AS n, min(created_at) AS oldest
                    FROM agent_mailbox mb
                   WHERE mb.recipient = r.name AND mb.delivered_at IS NULL) m ON true
              LEFT JOIN LATERAL (
                  SELECT w.title, w.state FROM work_items w
                   WHERE w.id = CASE WHEN r.session_work_item ~ '^[0-9]{1,18}$'
                                     THEN r.session_work_item::bigint END) sw ON true
              LEFT JOIN LATERAL (
                  SELECT w.id, w.title, w.state FROM work_items w
                   WHERE w.assignee = r.name AND w.state NOT IN """ + _CLOSED + """
                   ORDER BY w.updated_at DESC, w.id DESC LIMIT 1) aw ON true
              LEFT JOIN LATERAL (
                  SELECT c.harness, c.input_tokens, c.cached_input_tokens, c.session, c.recorded_at
                    FROM turn_costs c
                   WHERE c.agent = r.name
                   ORDER BY c.recorded_at DESC, c.id DESC LIMIT 1) t ON true
             ORDER BY r.name
            """)

    # -- redémarrage ---------------------------------------------------------
    def request_restart(self, name, brief) -> dict | None:
        rows = self.db.query(
            """
            UPDATE agent_registry
               SET restart_brief = %s, restart_requested_at = now(), updated_at = now()
             WHERE name = %s AND status <> 'stopped'
            RETURNING name, host, lease_owner,
                      extract(epoch from restart_requested_at)::float8 AS restart_requested_ts
            """,
            (brief, name),
        )
        return rows[0] if rows else None

    def apply_restart(self, name, owner=None, epoch=None) -> dict | None:
        if owner is None:
            fence = ("(verrou.lease_owner IS NULL"
                     " OR verrou.lease_expires_at IS NULL"
                     " OR verrou.lease_expires_at <= clock_timestamp())")
            params: tuple = (name,)
        else:
            fence = ("verrou.lease_owner = %s AND verrou.lease_epoch = %s"
                     " AND verrou.lease_expires_at > clock_timestamp()")
            params = (name, owner, int(epoch if epoch is not None else -1))
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at, session_id,
                       restart_brief, restart_requested_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET session_id = NULL,
                   session_work_item = NULL,
                   session_reset_at = clock_timestamp(),
                   -- le brief EN TÊTE, puis la consigne courante d'un tour qui
                   -- n'a pas abouti (bail expiré ou rendu), puis l'attente :
                   -- current_prompt est récupéré ici et effacé, sinon le claim
                   -- d'un remplaçant le remettrait DEVANT le brief (revue L26)
                   pending_prompt = concat_ws(E'\\n\\n', verrou.restart_brief,
                                              r.current_prompt, r.pending_prompt),
                   current_prompt = NULL,
                   status = CASE WHEN r.status = 'stopped' THEN r.status ELSE 'queued' END,
                   status_text = 'redémarrage : session neuve sur brief',
                   restart_brief = NULL,
                   restart_requested_at = NULL,
                   updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.restart_requested_at IS NOT NULL
               AND verrou.restart_brief IS NOT NULL
               AND """ + fence + """
            RETURNING r.name, verrou.session_id AS forgotten_session,
                      verrou.restart_brief AS brief,
                      extract(epoch from r.session_reset_at)::float8 AS session_reset_ts
            """,
            params,
        )
        return rows[0] if rows else None

    # -- adoption et reprise (L39, 0030) --------------------------------------
    def adopt(self, name, *, harness, host, cwd, session_id, session_account, prompt,
              status_text) -> dict | None:
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_expires_at, session_id, mode, harness
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET mode = 'execute',
                   harness = %s,
                   host = %s,
                   cwd = coalesce(%s::text, r.cwd),
                   session_id = %s,
                   session_account = %s::text,
                   session_work_item = NULL,
                   session_reset_at = clock_timestamp(),
                   pending_prompt = concat_ws(E'\\n\\n', %s::text,
                                              r.current_prompt, r.pending_prompt),
                   current_prompt = NULL,
                   restart_brief = NULL,
                   restart_requested_at = NULL,
                   status = 'queued',
                   status_text = %s::text,
                   last_seen = now(),
                   updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND (verrou.lease_owner IS NULL
                    OR verrou.lease_expires_at IS NULL
                    OR verrou.lease_expires_at <= clock_timestamp())
            RETURNING r.name, verrou.session_id AS previous_session,
                      verrou.mode AS previous_mode, verrou.harness AS previous_harness
            """,
            (name, harness, host, cwd, session_id, session_account, prompt, status_text),
        )
        return rows[0] if rows else None

    def resume(self, name, *, prompt, forget, status_text) -> dict | None:
        # paramètres positionnels : `oubli` (booléen) répété là où il sert
        oubli = bool(forget)
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, status, session_id,
                       (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                        AND lease_expires_at > clock_timestamp()) AS live
                  FROM agent_registry WHERE name = %s FOR UPDATE
            ), p AS (SELECT %s::boolean AS oubli, %s::text AS consigne)
            UPDATE agent_registry AS r
               SET session_id = CASE WHEN p.oubli THEN NULL ELSE r.session_id END,
                   session_work_item = CASE WHEN p.oubli THEN NULL
                                            ELSE r.session_work_item END,
                   session_reset_at = CASE WHEN p.oubli THEN clock_timestamp()
                                           ELSE r.session_reset_at END,
                   pending_prompt = CASE
                       WHEN verrou.live THEN concat_ws(E'\\n\\n', r.pending_prompt,
                                                       p.consigne)
                       ELSE concat_ws(E'\\n\\n', p.consigne, r.current_prompt,
                                      r.pending_prompt) END,
                   current_prompt = CASE WHEN verrou.live THEN r.current_prompt
                                         ELSE NULL END,
                   restart_brief = CASE WHEN verrou.live THEN r.restart_brief ELSE NULL END,
                   restart_requested_at = CASE WHEN verrou.live THEN r.restart_requested_at
                                               ELSE NULL END,
                   status = 'queued',
                   status_text = %s::text,
                   last_seen = now(),
                   updated_at = now()
              FROM verrou, p
             WHERE r.name = verrou.name
               AND (NOT verrou.live
                    OR (NOT p.oubli AND verrou.status NOT IN ('running', 'stopped')))
            RETURNING r.name, verrou.session_id AS previous_session,
                      verrou.status AS previous_status, verrou.live
            """,
            (name, oubli, prompt, status_text),
        )
        return rows[0] if rows else None

    # -- lots ----------------------------------------------------------------
    def message_lots(self, ids) -> list[str]:
        clean = sorted({int(i) for i in ids})
        if not clean:
            return []
        rows = self.db.query(
            "SELECT DISTINCT btrim(work_item_id) AS lot FROM agent_mailbox"
            " WHERE id IN (%s) AND coalesce(btrim(work_item_id), '') <> ''"
            " ORDER BY 1" % _marks(clean), tuple(clean))
        return [row["lot"] for row in rows]

    def assigned_open_lots(self, name) -> list[dict]:
        return self.db.query(
            "SELECT id, title, state, extract(epoch from updated_at)::float8 AS updated_ts"
            "  FROM work_items WHERE assignee = %s AND state NOT IN " + _CLOSED +
            " ORDER BY updated_at DESC, id DESC", (name,))

    def open_lots_activity(self, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT w.id, w.title, w.state, w.assignee, w.package_id, w.type,
                   extract(epoch from w.updated_at)::float8 AS updated_ts,
                   extract(epoch from w.created_at)::float8 AS created_ts,
                   extract(epoch from greatest(
                       w.updated_at, ev.at, ms.at, ac.at, mb.at))::float8 AS last_activity_ts
              FROM work_items w
              LEFT JOIN LATERAL (SELECT max(created_at) AS at FROM work_item_events e
                                  WHERE e.work_item_id = w.id) ev ON true
              LEFT JOIN LATERAL (SELECT max(at) AS at FROM work_item_milestones m
                                  WHERE m.work_item_id = w.id) ms ON true
              LEFT JOIN LATERAL (SELECT max(updated_at) AS at FROM actions a
                                  WHERE a.work_item = w.id) ac ON true
              LEFT JOIN LATERAL (SELECT max(created_at) AS at FROM agent_mailbox b
                                  WHERE b.work_item_id = w.id::text) mb ON true
             WHERE w.state NOT IN """ + _CLOSED + """
             ORDER BY last_activity_ts ASC, w.id
             LIMIT %s
            """,
            (int(limit),))

    # -- usage par tour ------------------------------------------------------
    def turns(self, *, agent, since_s, limit) -> list[dict]:
        sql = ("SELECT id, agent, harness, turn, model, session, usd::float8 AS usd,"
               " input_tokens, cached_input_tokens, output_tokens,"
               " extract(epoch from recorded_at)::float8 AS recorded_ts"
               " FROM turn_costs WHERE recorded_at >= now() - make_interval(secs => %s)"
               " AND void_reason IS NULL")
        params: list = [float(since_s)]
        if agent:
            sql += " AND agent = %s"
            params.append(agent)
        sql += " ORDER BY recorded_at DESC, id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    # -- jauges de forfait ---------------------------------------------------
    def record_gauges(self, readings) -> int:
        inserted = 0
        for g in readings:
            # L30 (0028) : une jauge de COMPTE porte son nom ; NULL = sans comptes
            account = g.get("account")
            rows = self.db.query(
                """
                INSERT INTO quota_gauge_readings (harness, gauge_key, used, resets_at, window_s,
                                                  account)
                SELECT %s, %s, %s, to_timestamp(%s), %s, %s
                 WHERE NOT EXISTS (
                     SELECT 1 FROM (
                         SELECT used, resets_at, observed_at FROM quota_gauge_readings
                          WHERE harness = %s AND gauge_key = %s
                            AND account IS NOT DISTINCT FROM %s
                          ORDER BY observed_at DESC, id DESC LIMIT 1) d
                      WHERE d.used = %s
                        AND d.resets_at IS NOT DISTINCT FROM to_timestamp(%s)
                        AND d.observed_at > now() - interval '10 minutes')
                RETURNING id
                """,
                (g["harness"], g["key"], float(g["used"]), g.get("resets_at"),
                 float(g["window_s"]), account, g["harness"], g["key"], account,
                 float(g["used"]), g.get("resets_at")),
            )
            inserted += len(rows)
        return inserted

    def gauge_history(self, *, since_s, harness, account=None) -> list[dict]:
        sql = ("SELECT harness, gauge_key AS key, used,"
               " extract(epoch from resets_at)::float8 AS resets_at_ts, window_s,"
               " extract(epoch from observed_at)::float8 AS observed_ts, account"
               " FROM quota_gauge_readings"
               " WHERE observed_at >= now() - make_interval(secs => %s)")
        params: list = [float(since_s)]
        if harness:
            sql += " AND harness = %s"
            params.append(harness)
        if account:
            sql += " AND account = %s"
            params.append(account)
        sql += " ORDER BY observed_at, id"
        return _sans_compte_nul(self.db.query(sql, tuple(params)))

    def latest_gauges(self, *, since_s) -> list[dict]:
        return self.db.query(
            """
            SELECT DISTINCT ON (harness, account, gauge_key)
                   harness, gauge_key AS key, used,
                   extract(epoch from resets_at)::float8 AS resets_at_ts, window_s,
                   extract(epoch from observed_at)::float8 AS observed_ts, account
              FROM quota_gauge_readings
             WHERE observed_at >= now() - make_interval(secs => %s)
             ORDER BY harness, account, gauge_key, observed_at DESC, id DESC
            """,
            (float(since_s),))

    def assigners(self, *, since_s) -> list[str]:
        rows = self.db.query(
            """
            SELECT DISTINCT who FROM (
                SELECT d.delegated_by AS who FROM work_item_delegations d
                 WHERE d.delegated_at >= now() - make_interval(secs => %s)
                UNION ALL
                SELECT e.actor FROM work_item_events e
                  JOIN work_items w ON w.id = e.work_item_id
                 WHERE e.created_at >= now() - make_interval(secs => %s)
                   AND e.note LIKE %s
                   AND e.actor <> ''
                   AND e.actor IS DISTINCT FROM w.assignee
            ) a WHERE coalesce(who, '') <> '' ORDER BY who
            """,
            (float(since_s), float(since_s), "assigné à %"))
        return [row["who"] for row in rows]

    # -- soldes --------------------------------------------------------------
    def record_balance(self, *, provider, currency, total, granted, topped_up,
                       available, account=None, unless_within_s=None) -> dict | None:
        if unless_within_s is not None and unless_within_s > 0:
            # L71 : une seule instruction — deux exécuteurs qui relèvent la
            # même clé à quelques secondes d'écart n'écrivent qu'une ligne.
            rows = self.db.query(
                """
                INSERT INTO provider_balances (provider, currency, total, granted,
                                               topped_up, available, account)
                SELECT %s, %s, %s, %s, %s, %s, %s
                 WHERE NOT EXISTS (
                       SELECT 1 FROM provider_balances
                        WHERE provider = %s AND currency = %s
                          AND account IS NOT DISTINCT FROM %s
                          AND observed_at > now() - make_interval(secs => %s))
                RETURNING provider, currency, total::float8 AS total,
                          granted::float8 AS granted, topped_up::float8 AS topped_up,
                          available, account,
                          extract(epoch from observed_at)::float8 AS observed_ts
                """,
                (provider, currency, float(total),
                 None if granted is None else float(granted),
                 None if topped_up is None else float(topped_up), available, account,
                 provider, currency, account, float(unless_within_s)),
            )
            return rows[0] if rows else None
        rows = self.db.query(
            """
            INSERT INTO provider_balances (provider, currency, total, granted, topped_up,
                                           available, account)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING provider, currency, total::float8 AS total, granted::float8 AS granted,
                      topped_up::float8 AS topped_up, available, account,
                      extract(epoch from observed_at)::float8 AS observed_ts
            """,
            (provider, currency, float(total),
             None if granted is None else float(granted),
             None if topped_up is None else float(topped_up), available, account),
        )
        return rows[0]

    def recent_balance(self, *, provider, account, within_s) -> bool:
        rows = self.db.query(
            """
            SELECT 1 AS found FROM provider_balances
             WHERE provider = %s AND account IS NOT DISTINCT FROM %s
               AND observed_at > now() - make_interval(secs => %s)
             LIMIT 1
            """,
            (provider, account, float(within_s)),
        )
        return bool(rows)

    def balances(self, *, provider, since_s, account=None) -> list[dict]:
        filtre = " AND provider = %s" if provider else ""
        extra = [provider] if provider else []
        if account:
            filtre += " AND account = %s"
            extra.append(account)
        params: list = [float(since_s)] + extra + [float(since_s)] + extra
        # DISTINCT ON inclut le compte (L30) : chaque clé d'API a sa série.
        return _sans_compte_nul(self.db.query(
            """
            SELECT provider, currency, total::float8 AS total, granted::float8 AS granted,
                   topped_up::float8 AS topped_up, available, account,
                   extract(epoch from observed_at)::float8 AS observed_ts
              FROM (
                SELECT * FROM provider_balances
                 WHERE observed_at >= now() - make_interval(secs => %s)""" + filtre + """
                UNION
                (SELECT DISTINCT ON (provider, currency, account) * FROM provider_balances
                  WHERE observed_at < now() - make_interval(secs => %s)""" + filtre + """
                  ORDER BY provider, currency, account, observed_at DESC, id DESC)
              ) b
             ORDER BY observed_at, id
            """,
            tuple(params),
        ))
