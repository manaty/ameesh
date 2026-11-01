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

from ameesh import work_lifecycle as lifecycle

from typing import Any

import json

from ...persona_sessions import track_change
from .. import interface
from .mailbox import PASSIVE_SQL
from .work import REQUEUED_PARAMS, has_reviewer, requeued_sql
from .work_dependencies import (current_verdict as _current_verdict,
                                owed_by_assignee, start_defers, wake_defers)

#: états de lot terminés (le reste est « ouvert »)
_CLOSED = f"('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"   # closed : abandonné ou remplacé (L29)
#: L165b : un lot dans ces états attend quelqu'un d'autre que son assigné
_WAITING_ON_OTHERS = f"('{lifecycle.QA}', '{lifecycle.VALIDATED}', '{lifecycle.WAITING_HUMAN}', '{lifecycle.BLOCKED}')"
#: L125 : un message passif de la boîte `mb`
_PASSIVE = PASSIVE_SQL.format(t="mb")


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

    def publish_peek(self, name, owner, epoch, payload) -> bool:
        from ...agent_peek import sanitise
        # Lock first, then check real time in a fresh statement: expired
        # runners cannot publish after waiting behind a lease replacement.
        with self.db.transaction() as tx:
            tx.query("SELECT name FROM agent_registry WHERE name = %s FOR UPDATE", (name,))
            rows = tx.query("""
                INSERT INTO agent_peek (agent, host, lease_epoch, payload)
                SELECT name, host, lease_epoch, %s::jsonb FROM agent_registry
                WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
                  AND lease_expires_at > clock_timestamp()
                ON CONFLICT (agent) DO UPDATE SET host = EXCLUDED.host,
                  lease_epoch = EXCLUDED.lease_epoch, captured_at = clock_timestamp(),
                  payload = EXCLUDED.payload RETURNING agent
                """, (json.dumps(sanitise(payload), ensure_ascii=False), name, owner, int(epoch)))
        return bool(rows)

    def read_peek(self, name, human):
        # An unavailable snapshot is explicit; metadata still describes the
        # authorised agent. Old host/lease observations are never reused.
        # An empty declared responsible never authorises an empty reader:
        # the two empty strings must not match (N4, #1398).
        rows = self.db.query(f"""
            SELECT r.host, r.status, r.status_text,
                   w.id AS lot_id, w.title AS lot_title, w.state AS lot_state,
                   extract(epoch FROM p.captured_at)::float8 AS captured_ts, p.payload
            FROM agent_registry r
            LEFT JOIN agent_peek p ON p.agent = r.name AND p.host = r.host
                                     AND p.lease_epoch = r.lease_epoch
            LEFT JOIN LATERAL (
                SELECT id, title, state FROM work_items
                WHERE assignee = r.name AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}','{lifecycle.PROMOTED}','{lifecycle.CLOSED}')
                ORDER BY (id::text = r.session_work_item) DESC NULLS LAST,
                         (state = '{lifecycle.IN_PROGRESS}') DESC, updated_at DESC, id DESC LIMIT 1
            ) w ON true
            WHERE r.name = %s AND r.responsible <> '' AND r.responsible = %s
            """, (name, human))
        return rows[0] if rows else None

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

    def write_context_pilot(self, name, settings, state, *, opening) -> bool:
        cols = [key for key in settings if key in self.INTEGER_SETTINGS]
        sets = "".join("%s = nullif(%%s, '')::bigint, " % col for col in cols)
        params: list[Any] = [
            "" if settings[col] is None else str(settings[col]) for col in cols]
        ouvert = "context_pilot IS NOT NULL AND context_pilot->>'returned_ts' IS NULL"
        garde = "NOT (coalesce(%s, false))" % ouvert if opening else ouvert
        return bool(self.db.query(
            "UPDATE agent_registry SET " + sets + "context_pilot = %s::jsonb,"
            " updated_at = now() WHERE name = %s AND " + garde + " RETURNING name",
            tuple(params + [json.dumps(state), name])))

    def set_dormant(self, name, owner, epoch, status_text) -> bool:
        return bool(self.db.query(
            "UPDATE agent_registry SET status_text = %s, updated_at = now()"
            " WHERE name = %s AND status = 'idle' AND lease_owner = %s"
            "   AND lease_epoch = %s AND lease_expires_at > clock_timestamp()"
            " RETURNING name", (status_text, name, owner, int(epoch))))

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
    def listing(self, *, today: str | None = None) -> list[dict]:
        import datetime as _dt
        today = today or _dt.date.today().isoformat()
        # #1726 : une relecture désignée (#705) sans verdict courant occupe son
        # relecteur — sans quoi, sans lot à lui, il passerait pour « au repos »
        # et la prise lui donnerait un lot ou une autre relecture. La colonne
        # `reviewer` n'existe qu'après la migration 0421.
        review_due = ""
        if has_reviewer(self.db):
            review_due = (f"""
                     + (SELECT count(*) FROM work_items w
                         WHERE w.state = '{lifecycle.QA}' AND w.reviewer = r.name
                           AND """ + _current_verdict("w") + """ IS NULL)""")
        return self.db.query(
            """
            SELECT r.name, r.chantier, r.team, r.projects, r.harness, r.host, r.model,
                   -- #4779 : le profil de relecture (capacité `propose`, rôles),
                   -- lu par `file_decoupage._reviewer_spares`
                   r.capabilities, r.roles,
                   r.ephemeral,  -- #3411 : la prise automatique ne sert pas un éphémère
                   r.effort, r.tier,
                   r.cwd,  -- L73b : dossiers de travail protégés par le ménage
                   r.session_policy, r.lot_wake, r.context_max_tokens, r.turn_max_seconds, r.turn_mail_max,
                   r.turn_grace_seconds,
                   r.session_max_tokens, r.turn_reread_budget, r.session_reread_budget,
                   r.session_id, r.session_work_item,
                   r.status, r.status_text, r.current_prompt, r.lease_owner,
                   r.mode, r.stop_reason, r.responsible,
                   -- #2200 : la pause opérateur, portée par la lecture partagée
                   -- des alertes (état « paused », jamais « idle »)
                   r.operator_paused, r.operator_pause_reason, r.operator_pause_by,
                   r.operator_pause_for,
                   extract(epoch from r.operator_pause_at)::float8 AS operator_pause_ts,
                   extract(epoch from r.operator_pause_until)::float8 AS operator_pause_until_ts,
                   r.canon,  -- L242c : le canon propriétaire (rôles de sa fiche)
                   -- #3460 : un agent retiré ne prend rien et n'alerte plus
                   extract(epoch from r.retired_at)::float8 AS retired_ts,
                   extract(epoch from r.lease_wait_since)::float8 AS lease_wait_since_ts,
                   r.admitted_hosts,  -- #254 : hôtes en pause
                   r.last_error,  -- L106 : le détail de « hôte non prêt »
                   (r.pending_prompt IS NOT NULL) AS has_pending_prompt,
                   (r.lease_owner IS NOT NULL
                    AND r.lease_expires_at > clock_timestamp()) AS lease_live,
                   extract(epoch from r.lease_expires_at)::float8 AS lease_expires_ts,
                   extract(epoch from r.status_since)::float8     AS status_since_ts,
                   -- #1521 : le début du motif affiché (status_text), pas du statut
                   extract(epoch from r.status_reason_since)::float8 AS status_reason_since_ts,
                   extract(epoch from r.last_turn_at)::float8     AS last_turn_ts,
                   extract(epoch from r.updated_at)::float8       AS updated_ts,
                   extract(epoch from r.restart_requested_at)::float8 AS restart_requested_ts,
                   extract(epoch from p.created_at)::float8       AS turn_started_ts,
                   p.turn AS turn_label,
                   coalesce(m.n, 0)::bigint AS unread,
                   extract(epoch from m.oldest)::float8 AS oldest_unread_ts,
                   coalesce(m.n_passive, 0)::bigint AS passive_unread,
                   sw.title AS session_lot_title, sw.state AS session_lot_state,
                   (coalesce(""" + start_defers("sw", "j.today") + """, false)
                       OR coalesce(sw.wake_at > clock_timestamp(), false)) AS session_lot_later,
                   aw.id AS assigned_lot_id, aw.title AS assigned_lot_title,
                   aw.state AS assigned_lot_state,
                   coalesce(al.n, 0)::bigint AS active_lots,
                   -- #1677 : les lots qui demandent encore un geste de l'agent
                   coalesce(ac.n, 0)::bigint AS actionable_lots,
                   -- #4619 : le lot actionnable à citer au journal (le lot
                   -- assigné le plus récent peut ne pas l'être)
                   ac.lot_id AS actionable_lot_id,
                   -- L60 : Codex compte déjà le cache dans input_tokens
                   (CASE WHEN t.harness = 'codex'
                         THEN greatest(t.input_tokens, t.cached_input_tokens)
                         ELSE t.input_tokens + t.cached_input_tokens
                    END)::bigint AS last_turn_reread_tokens,
                   t.session AS last_turn_session,
                   extract(epoch from t.recorded_at)::float8 AS last_turn_recorded_ts
              FROM agent_registry r
              -- #318 r2 : le jour local de l'hôte, pas celui du fuseau de la base
              CROSS JOIN (SELECT %s::date AS today) j
              LEFT JOIN spend_pending p ON p.agent = r.name
              LEFT JOIN LATERAL (
                  -- L125 : `unread` = le courrier qui ouvrira un tour ; le
                  -- courrier passif (accusé, copie, diffusion) est compté à part.
                  -- #3923 : deux index partiels disjoints (actif 0495, passif
                  -- 0489) au lieu d'un parcours du tas de tous les non-lus —
                  -- les 5 074 non-lus de `bureau` coûtaient 443 ms à eux seuls.
                  SELECT actif.n, actif.oldest, passif.n_passive
                    FROM (SELECT count(*) AS n,
                                 min(mb.created_at) AS oldest
                            FROM agent_mailbox mb
                           WHERE mb.recipient = r.name AND mb.delivered_at IS NULL
                             AND NOT """ + _PASSIVE + """) actif
                   CROSS JOIN (SELECT count(*) AS n_passive
                                 FROM agent_mailbox mb
                                WHERE mb.recipient = r.name AND mb.delivered_at IS NULL
                                  AND """ + _PASSIVE + """) passif) m ON true
              LEFT JOIN LATERAL (
                  -- #5161 : la ligne entière, `start_defers` lit l'état et
                  -- les verdicts du lot (correction due)
                  SELECT w.* FROM work_items w
                   WHERE w.id = CASE WHEN r.session_work_item ~ '^[0-9]{1,18}$'
                                     THEN r.session_work_item::bigint END) sw ON true
              LEFT JOIN LATERAL (
                  SELECT w.id, w.title, w.state FROM work_items w
                   WHERE w.assignee = r.name AND w.state NOT IN """ + _CLOSED + """
                   ORDER BY w.updated_at DESC, w.id DESC LIMIT 1) aw ON true
              LEFT JOIN LATERAL (
                  -- L165b : les lots qui occupent l'agent (ni en attente d'un
                  -- autre, ni prévus pour plus tard) ; #5161 : un lot en qa qui
                  -- attend son geste (gel, correction due) l'occupe
                  SELECT count(*) AS n FROM work_items w
                   WHERE w.assignee = r.name AND w.state NOT IN """ + _CLOSED + """
                     AND (w.state NOT IN """ + _WAITING_ON_OTHERS + """
                          OR """ + owed_by_assignee("w") + """)
                     AND NOT """ + start_defers("w", "j.today") + """
                     AND NOT """ + wake_defers("w") + """) al ON true
              LEFT JOIN LATERAL (
                  -- #1677 : ce que l'agent doit encore faire — ses lots qui
                  -- l'occupent, plus la correction d'un lot en qa dont le
                  -- verdict COURANT est bloquant (B1 : un `blocked` d'une tête
                  -- regelée depuis n'attend plus de correction). Aucun lot
                  -- actionnable = au repos, quel que soit le temps depuis le
                  -- dernier tour.
                  -- #1726 : une relecture désignée sans verdict courant
                  -- (relecteur #705, lot en qa) compte aussi : l'agent doit
                  -- son verdict avant de prendre autre chose.
                  -- #4619 : `lot_id` nomme le lot actionnable que le journal
                  -- peut citer ; `assigned_lot_id` reste le lot assigné le plus
                  -- récemment touché, qui peut être prévu plus tard ou en
                  -- attente d'un autre.
                  -- #5037 : un réveil futur ne l'éteint pas quand ce geste est
                  -- dû (gel manquant ou verdict courant bloquant).
                  SELECT count(*)""" + review_due + """ AS n, min(w.id) AS lot_id
                    FROM work_items w
                   WHERE w.assignee = r.name AND w.state NOT IN """ + _CLOSED + """
                     AND (w.state NOT IN """ + _WAITING_ON_OTHERS + """
                          OR """ + owed_by_assignee("w") + """)
                     AND NOT """ + start_defers("w", "j.today") + """
                     AND NOT """ + wake_defers("w") + """) ac ON true
              LEFT JOIN LATERAL (
                  SELECT c.harness, c.input_tokens, c.cached_input_tokens, c.session, c.recorded_at
                    FROM turn_costs c
                   WHERE c.agent = r.name
                   ORDER BY c.recorded_at DESC, c.id DESC LIMIT 1) t ON true
             ORDER BY r.name
            """, (today,))

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

    @track_change("restart")
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
                   -- L165 (EP3) : le brief de relève décrivait l'ancienne
                   -- session ; il part avec elle.
                   election_brief = NULL,
                   session_reset_at = clock_timestamp(),
                   -- le brief EN TÊTE, puis la consigne courante d'un tour qui
                   -- n'a pas abouti (bail expiré ou rendu), puis l'attente :
                   -- current_prompt est récupéré ici et effacé, sinon le claim
                   -- d'un remplaçant le remettrait DEVANT le brief (revue L26)
                   pending_prompt = concat_ws(E'\\n\\n', verrou.restart_brief,
                                              r.current_prompt, r.pending_prompt),
                   current_prompt = NULL,
                   status = CASE WHEN r.status = 'stopped' THEN r.status ELSE 'queued' END,
                   status_text = 'restart: fresh session from brief',
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
    @track_change("adopt")
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
                   -- #1037 : la session adoptée est connue AVANT tout tour ;
                   -- son ouverture est celle du harnais adopté (le déclencheur
                   -- de 0384 garde les trois tant que origin = session).
                   session_harness = %s,
                   session_cwd = coalesce(%s::text, r.cwd),
                   session_origin = %s,
                   session_work_item = NULL,
                   -- L165 (EP3) : une session adoptée remplace tout brief de
                   -- relève en attente.
                   election_brief = NULL,
                   -- #1037 : un changement de harnais DÉCLARÉ emporte les
                   -- réglages du harnais précédent : les garder ferait lancer
                   -- le nouveau harnais avec le modèle, l'effort ou le tier de
                   -- l'ancien (r.harness = la valeur d'avant l'UPDATE).
                   model = CASE WHEN r.harness IS DISTINCT FROM %s::text
                                THEN NULL ELSE r.model END,
                   effort = CASE WHEN r.harness IS DISTINCT FROM %s::text
                                 THEN NULL ELSE r.effort END,
                   tier = CASE WHEN r.harness IS DISTINCT FROM %s::text
                               THEN NULL ELSE r.tier END,
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
            (name, harness, host, cwd, session_id, session_account, harness, cwd,
             session_id, harness, harness, harness, prompt, status_text),
        )
        return rows[0] if rows else None

    @track_change("fresh resume")
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
                   -- L165 (EP3) : oublier la session oublie le brief de relève
                   -- qui la décrivait ; sans oubli, il reste en attente.
                   election_brief = CASE WHEN p.oubli THEN NULL
                                         ELSE r.election_brief END,
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

    #: corps commun de `open_lots_activity` / `state_lots_activity` : la
    #: dernière activité d'un lot (ligne, transitions, jalons, actions,
    #: messages). `__WHERE__` reçoit le filtre de l'appelant (qui peut porter
    #: un `%s`) ; le fragment §4 `__RETURNED__` porte ses propres paramètres
    #: (`REQUEUED_PARAMS`), d'où la substitution, jamais un `%` de formatage.
    #: #1922/N13 : `workflow_ref` (0452) est porté par la lecture, comme
    #: `work.get` le fait par `to_jsonb` — '' sur une base d'avant la
    #: migration. `released_by` en écarte les dépendants pinnés, dont la
    #: retenue ne tombe que par une commande `release` du registre.
    _LOTS_ACTIVITY = """
            SELECT w.id, w.title, w.state, to_jsonb(w)->>'legacy_state' AS legacy_state, w.assignee, w.package_id, w.type,
                   w.priority, w.app, w.workstream, w.branch_target, __REVIEWER__,
                   -- #4818 : un lot de décision ou de découpage n'entre pas
                   -- dans le plafond de lots en vol (`work_footprints.is_exempt`)
                   w.source,
                   to_jsonb(w)->>'external_executor' AS external_executor,
                   -- #2335 : l'appartenance à un paquet se lit sur le porteur
                   -- (`integration/**`), son manifeste (corps) et la tête gelée
                   -- du lot, sans requête par lot. Le corps n'est transféré que
                   -- pour un porteur : pas pour tous les lots ouverts (N2 de
                   -- la relecture dev7).
                   w.branch, w.branch_head,
                   -- #5206 : l'instant du relevé de la tête, pour dater une
                   -- composition enregistrée après lui (recomposition en cours)
                   extract(epoch from w.branch_head_at)::float8 AS branch_head_ts,
                   extract(epoch from w.wake_at)::float8 AS wake_ts,
                   to_char(w.planned_start, 'YYYY-MM-DD') AS planned_start,
                   CASE WHEN left(btrim(w.branch), 12) = 'integration/' THEN w.body END AS body,
                   to_jsonb(w)->'reviewers' AS reviewers,
                   coalesce(to_jsonb(w)->>'workflow_ref', '') AS workflow_ref,
                   __RETURNED__
                   coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),
                            nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,
                   extract(epoch from w.updated_at)::float8 AS updated_ts,
                   extract(epoch from w.created_at)::float8 AS created_ts,
                   extract(epoch from greatest(
                       w.updated_at, ev.at, ms.at, ac.at, mb.at))::float8 AS last_activity_ts
              FROM work_items w
              LEFT JOIN work_packages k ON k.id = w.package_id
              LEFT JOIN work_packages kp ON kp.id = w.package_parent
              LEFT JOIN LATERAL (SELECT max(created_at) AS at FROM work_item_events e
                                  WHERE e.work_item_id = w.id) ev ON true
              LEFT JOIN LATERAL (SELECT max(at) AS at FROM work_item_milestones m
                                  WHERE m.work_item_id = w.id) ms ON true
              LEFT JOIN LATERAL (SELECT max(updated_at) AS at FROM actions a
                                  WHERE a.work_item = w.id) ac ON true
              LEFT JOIN LATERAL (SELECT max(created_at) AS at FROM agent_mailbox b
                                  WHERE b.work_item_id = w.id::text) mb ON true
             WHERE __WHERE__
             ORDER BY last_activity_ts ASC, w.id
             LIMIT %s
            """

    def _lots_activity(self, where, params) -> list[dict]:
        # #705 : `w.reviewer` n'existe qu'après la migration 0421 ; avant elle,
        # la lecture rend '' (l'alerte ne nomme alors personne, comme hier).
        reviewer = ("w.reviewer" if has_reviewer(self.db)
                    else "''::text AS reviewer")
        # §4 : `returned_to_queue` distingue un lot `build` RENDU à la file
        # (limite d'en-cours) d'un lot en cours sans assigné (prérequis,
        # travail confié par courrier) — voir `work.requeued_sql`.
        sql = (self._LOTS_ACTIVITY
               .replace("__REVIEWER__", reviewer)
               .replace("__RETURNED__", f"(w.state = '{lifecycle.IN_PROGRESS}' AND "
                        + requeued_sql("w") + ") AS returned_to_queue,")
               .replace("__WHERE__", where))
        return self.db.query(sql, REQUEUED_PARAMS + tuple(params))

    def open_lots_activity(self, limit) -> list[dict]:
        return self._lots_activity(
            "w.state NOT IN " + _CLOSED,
            (int(limit) if limit is not None else None,))

    def state_lots_activity(self, state, limit) -> list[dict]:
        predicate = ("coalesce(to_jsonb(w)->>'legacy_state', w.state) = %s"
                     if state in ("merged", "promoted") else "w.state = %s")
        return self._lots_activity(
            predicate,
            (state, int(limit) if limit is not None else None))

    def build_lots_activity(self, limit) -> list[dict]:
        """§4 : les lots en `build` assignés à un agent, avec leur dernière
        activité (même calcul qu'`open_lots_activity`) — la lecture dédiée du
        balayage de la limite d'en-cours, sans le plafond global des lots
        ouverts."""
        return self.db.query(
            f"""
            SELECT w.id, w.title, w.state, to_jsonb(w)->>'legacy_state' AS legacy_state, w.assignee, w.package_id, w.type,
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
             WHERE w.state = '{lifecycle.IN_PROGRESS}' AND w.assignee IS NOT NULL
             ORDER BY last_activity_ts ASC, w.id
             LIMIT %s
            """,
            (int(limit) if limit is not None else None,))

    # -- usage par tour ------------------------------------------------------
    def turns(self, *, agent, since_s, limit, message=None) -> list[dict]:
        sql = ("SELECT id, agent, harness, turn, model, session, usd::float8 AS usd,"
               " input_tokens, cached_input_tokens, output_tokens, work_item_id,"
               " inputs,"
               " extract(epoch from recorded_at)::float8 AS recorded_ts,"
               " extract(epoch from started_at)::float8 AS started_ts"
               " FROM turn_costs WHERE recorded_at >= now() - make_interval(secs => %s)"
               " AND void_reason IS NULL")
        params: list = [float(since_s)]
        if agent:
            sql += " AND agent = %s"
            params.append(agent)
        if message is not None:
            # L80b : les tours qui ont servi ce message (l'index GIN porte le
            # contenant). Le coût rendu est celui du tour entier, jamais divisé.
            sql += " AND inputs @> %s::jsonb"
            params.append(json.dumps([str(message)]))
        sql += " ORDER BY recorded_at DESC, id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    # -- jauges de forfait ---------------------------------------------------
    def record_gauges(self, readings) -> int:
        inserted = 0
        for g in readings:
            # L30 (0028) : une jauge de COMPTE porte son nom ; NULL = sans comptes.
            # #1132 (0434) : `produced_at` (instant de production, NULL quand le
            # flux ne le date pas) et `source` (journal d'origine) : la provenance.
            # La dédup ne regarde plus la SEULE dernière insertion, ni n'importe
            # quelle ligne identique (delta auditeur, tête a66546be) : une
            # production STRICTEMENT plus fraîche de la même valeur neutralise une
            # relecture plus ancienne, mais à production ÉGALE (NULL inclus) c'est
            # la DERNIÈRE valeur de ce rang qui décide, dans l'ordre de
            # `gauge_latest` (production, puis insertion). Une oscillation
            # 87 -> 67 -> 87 sous le même marqueur reste donc lisible, une
            # production plus récente à valeur égale est conservée, et un relevé
            # périmé réinséré ne devient jamais « le dernier ».
            account = g.get("account")
            produced = g.get("produced_at")
            rows = self.db.query(
                """
                WITH candidat AS (
                    SELECT %s::text AS harness, %s::text AS gauge_key,
                           %s::float8 AS used, to_timestamp(%s::float8) AS resets_at,
                           %s::float8 AS window_s, %s::text AS account,
                           to_timestamp(%s::float8) AS produced_at, %s::text AS source,
                           %s::boolean AS overage_capable,
                           %s::boolean AS overage_in_use
                )
                INSERT INTO quota_gauge_readings (harness, gauge_key, used, resets_at, window_s,
                                                  account, produced_at, source,
                                                  overage_capable, overage_in_use)
                SELECT harness, gauge_key, used, resets_at, window_s, account,
                       produced_at, source, overage_capable, overage_in_use
                  FROM candidat c
                 WHERE NOT EXISTS (
                     SELECT 1 FROM quota_gauge_readings r
                      WHERE r.harness = c.harness AND r.gauge_key = c.gauge_key
                        AND r.account IS NOT DISTINCT FROM c.account
                        AND r.used = c.used
                        AND r.resets_at IS NOT DISTINCT FROM c.resets_at
                        AND r.overage_capable IS NOT DISTINCT FROM c.overage_capable
                        AND r.overage_in_use IS NOT DISTINCT FROM c.overage_in_use
                        AND r.observed_at > now() - interval '10 minutes'
                        AND (
                            (r.produced_at IS NOT NULL
                             AND (c.produced_at IS NULL
                                  OR r.produced_at > c.produced_at))
                            OR
                            (r.produced_at IS NOT DISTINCT FROM c.produced_at
                             AND NOT EXISTS (
                                 SELECT 1 FROM quota_gauge_readings n
                                  WHERE n.harness = r.harness
                                    AND n.gauge_key = r.gauge_key
                                    AND n.account IS NOT DISTINCT FROM r.account
                                    AND n.produced_at IS NOT DISTINCT FROM r.produced_at
                                    AND (n.observed_at > r.observed_at
                                         OR (n.observed_at = r.observed_at
                                             AND n.id > r.id))))
                        )
                )
                RETURNING id
                """,
                (g["harness"], g["key"], float(g["used"]), g.get("resets_at"),
                 float(g["window_s"]), account, produced, g.get("source"),
                 g.get("overage_capable"), g.get("overage_in_use")),
            )
            inserted += len(rows)
        return inserted

    def gauge_history(self, *, since_s, harness, account=None) -> list[dict]:
        sql = ("SELECT harness, gauge_key AS key, used,"
               " extract(epoch from resets_at)::float8 AS resets_at_ts, window_s,"
               " extract(epoch from observed_at)::float8 AS observed_ts,"
               " extract(epoch from produced_at)::float8 AS produced_ts, source, account,"
               " overage_capable, overage_in_use"
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

    def latest_gauges(self, *, since_s, by_source=False) -> list[dict]:
        """L94 : le DERNIER relevé de chaque jauge, par instant de PRODUCTION
        (#1132) : un relevé périmé réinséré ne remplace plus le frais.

        `by_source` garde une ligne par source de publication (agent) : deux
        hôtes homonymes ne s'effacent plus dans la sélection (#3755, avis
        cx1). Les lecteurs de la vue unifiée gardent le défaut."""
        return self._gauge_heads(since_s=since_s, by_source=by_source)

    def gauge_latest(self, *, harness, account) -> list[dict]:
        """Latest known production per gauge, without an age limit (#1132)."""
        if harness is None or account is None:
            return []
        return self._gauge_heads(harness=harness, account=account)

    def _gauge_heads(self, *, since_s=None, harness=None, account=None, by_source=False):
        """#3922: seek each winning reading, skipping its entire group.

        DISTINCT ON walks every historical reading even with an ordered index.
        The recursive walk jumps past each entire group and filters the age
        inside each indexed seek, including for entirely expired groups. Account's
        null flag keeps NULL distinct from an empty named account and makes the
        tuple comparable (SQL row comparisons containing NULL cannot seek).
        The additive gauge_seek index matches these expressions exactly.
        """
        key = "harness, (account IS NULL), coalesce(account, ''), gauge_key"
        extra = ", (account IS NULL) AS unnamed, coalesce(account, '') AS account_key"
        previous = "k.harness, k.unnamed, k.account_key, k.gauge_key"
        source_order = ""
        if by_source:
            key += ", (source IS NULL), coalesce(source, '')"
            extra += ", (source IS NULL) AS anonymous, coalesce(source, '') AS source_key"
            previous += ", k.anonymous, k.source_key"
            source_order = ", r.source"
        params = []
        bounds = ""
        age = "true"
        if since_s is not None:
            bounds = "bounds AS (SELECT now() - make_interval(secs => %s) AS cutoff), "
            params.append(float(since_s))
            age = "observed_at >= (SELECT cutoff FROM bounds)"
        scope = ""
        if harness is not None:
            scope = ("harness = %s AND (account IS NULL) IS FALSE"
                     " AND coalesce(account, '') = %s AND ")
            params.extend((harness, account))
        order = key + ", produced_at DESC NULLS LAST, observed_at DESC, id DESC"
        return self.db.query(
            "WITH RECURSIVE " + bounds + "heads AS ("
            " (SELECT q.*" + extra + " FROM quota_gauge_readings q WHERE " + scope +
            age + " ORDER BY " + order + " LIMIT 1)"
            " UNION ALL"
            " SELECT n.* FROM heads k CROSS JOIN LATERAL ("
            " SELECT q.*" + extra + " FROM quota_gauge_readings q WHERE (" + key + ")"
            " > (" + previous + ") AND " + age +
            " ORDER BY " + order + " LIMIT 1) n"
            # A scoped walk stops at the end of its account.
            + (" WHERE n.harness = k.harness AND n.unnamed = k.unnamed"
               " AND n.account_key = k.account_key" if harness is not None else "") +
            ") SELECT r.harness, r.gauge_key AS key, r.used,"
            " extract(epoch from r.resets_at)::float8 AS resets_at_ts, r.window_s,"
            " extract(epoch from r.observed_at)::float8 AS observed_ts,"
            " extract(epoch from r.produced_at)::float8 AS produced_ts, r.source, r.account,"
            " r.overage_capable, r.overage_in_use FROM heads r"
            " ORDER BY r.harness, r.account, r.gauge_key" + source_order, tuple(params))

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
                   AND (e.note LIKE %s OR e.note LIKE %s)
                   AND e.actor <> ''
                   AND e.actor IS DISTINCT FROM w.assignee
            ) a WHERE coalesce(who, '') <> '' ORDER BY who
            """,
            (float(since_s), float(since_s), "assigné à %", "assigned to %"))
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
        # #3923 : plus de DISTINCT ON sur tout l'historique (un tri de ~3 000
        # lignes à chaque lecture, 211 ms mesurés) ; la fenêtre se lit
        # directement, puis une sonde par série rend le dernier relevé
        # antérieur. Mêmes lignes, même ordre (`observed_at`, `id`).
        params: list = [float(since_s)] + extra + extra + [float(since_s)]
        return _sans_compte_nul(self.db.query(
            """
            SELECT provider, currency, total::float8 AS total, granted::float8 AS granted,
                   topped_up::float8 AS topped_up, available, account,
                   extract(epoch from observed_at)::float8 AS observed_ts
              FROM (
                SELECT provider, currency, total, granted, topped_up, available,
                       account, observed_at, id
                  FROM provider_balances
                 WHERE observed_at >= now() - make_interval(secs => %s)""" + filtre + """
                UNION ALL
                SELECT last.provider, last.currency, last.total, last.granted,
                       last.topped_up, last.available, last.account,
                       last.observed_at, last.id
                  FROM (SELECT DISTINCT provider, currency, account
                          FROM provider_balances
                         WHERE true""" + filtre + """) s
                  CROSS JOIN LATERAL (
                    SELECT pb.* FROM provider_balances pb
                     WHERE pb.provider = s.provider
                       AND pb.currency = s.currency
                       AND pb.account IS NOT DISTINCT FROM s.account
                       AND pb.observed_at < now() - make_interval(secs => %s)
                     ORDER BY pb.observed_at DESC, pb.id DESC
                     LIMIT 1) last
              ) u
             ORDER BY observed_at, id
            """,
            tuple(params),
        ))
