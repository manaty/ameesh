# SPDX-License-Identifier: AGPL-3.0-only
"""L165b (#408) : sessions mises de côté par (agent, lot) et réveils sur lot
actionnable, sur Postgres (`agent_lot_sessions`, migration 0384)."""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import datetime as _dt
import time

from .. import interface
from ..interface import HOLD_NOTE_WHITESPACE
from .work import has_wake_at
from .work_dependencies import (current_verdict as _current_verdict, freeze_due,
                                start_defers, wake_defers)

_CLOSED = f"('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"

_COLUMNS = """
    l.agent, l.work_item_id, l.session_id, l.harness, l.account, l.cwd, l.wakes,
    extract(epoch from l.parked_at)::float8        AS parked_ts,
    extract(epoch from l.last_wake_at)::float8     AS last_wake_ts,
    extract(epoch from l.wake_activity_at)::float8 AS wake_activity_ts,
    extract(epoch from l.created_at)::float8       AS created_ts
"""


def _moment(ts) -> str:
    """Un instant lisible dans le journal, fuseau du poste : ISO avec offset."""
    return _dt.datetime.fromtimestamp(float(ts)).astimezone().isoformat(
        timespec="seconds")


class LotSessions(interface.LotSessions):

    def switch(self, name, owner, epoch, *, to_lot, harness, account, cwd, current=None):
        """`to_lot=None` : mise de côté de la session active seulement (#2055)."""
        with self.db.transaction() as tx:
            reg = tx.query(
                "SELECT session_work_item,"
                "       (session_id = %s AND session_origin = session_id) AS origine,"
                "       session_account, session_harness, session_cwd,"
                "       (lease_owner = %s AND lease_epoch = %s"
                "        AND lease_expires_at > clock_timestamp()) AS ours"
                "  FROM agent_registry WHERE name = %s FOR UPDATE",
                (current, owner, int(epoch), name))
            if not reg or not reg[0]["ours"]:
                return None
            r = reg[0]
            # l'origine notée à l'ouverture de la session (inconnue si le
            # registre ne la décrit pas : elle ne sera pas reprise)
            if r.get("origine"):
                origine = (r.get("session_harness"), r.get("session_account"),
                           r.get("session_cwd"))
            else:
                origine = (None, None, None)
            # `to_lot=None` (ou vide) : aucune session active au sortir, la
            # session courante est seulement mise de côté pour son lot.
            cible = str(to_lot).strip() if to_lot is not None else ""
            parked = None
            courant = str(r.get("session_work_item") or "").strip()
            if current and courant.isdigit() and courant != cible:
                # la session active est gardée pour son lot s'il est encore
                # ouvert et assigné à l'agent (sinon la purge l'effacerait)
                rows = tx.query(
                    "INSERT INTO agent_lot_sessions AS l"
                    "       (agent, work_item_id, session_id, harness, account, cwd, parked_at)"
                    " SELECT %s, w.id, %s, %s, %s, %s, clock_timestamp()"
                    "   FROM work_items w"
                    "  WHERE w.id = %s AND w.assignee = %s"
                    "    AND w.state NOT IN " + _CLOSED +
                    " ON CONFLICT (agent, work_item_id) DO UPDATE"
                    "    SET session_id = excluded.session_id, harness = excluded.harness,"
                    "        account = excluded.account, cwd = excluded.cwd,"
                    "        parked_at = excluded.parked_at"
                    " RETURNING l.session_id",
                    (name, current, *origine, int(courant), name))
                parked = rows[0]["session_id"] if rows else None
            resumed = discarded = raison = None
            if cible.isdigit():
                rows = tx.query(
                    "SELECT l.session_id, l.harness, l.account, l.cwd,"
                    "       (w.assignee = l.agent AND w.state NOT IN " + _CLOSED + ") AS ouvert"
                    "  FROM agent_lot_sessions l JOIN work_items w ON w.id = l.work_item_id"
                    " WHERE l.agent = %s AND l.work_item_id = %s FOR UPDATE OF l",
                    (name, int(cible)))
                row = rows[0] if rows else None
                if row and row.get("session_id"):
                    if not row.get("ouvert"):
                        discarded, raison = row["session_id"], "lot"
                    elif row.get("harness") is not None and row.get("cwd") is not None \
                            and (row.get("harness"), row.get("account"), row.get("cwd")) \
                            == (harness, account, cwd):
                        resumed = row["session_id"]
                    else:
                        discarded, raison = row["session_id"], "origin"
                    tx.execute(
                        "UPDATE agent_lot_sessions SET session_id = NULL, parked_at = NULL"
                        " WHERE agent = %s AND work_item_id = %s", (name, int(cible)))
            tx.execute(
                "UPDATE agent_registry SET session_id = %s, session_account = %s,"
                "       session_harness = %s, session_cwd = %s, session_origin = %s,"
                "       session_work_item = %s, session_reset_at = clock_timestamp(),"
                "       updated_at = now()"
                " WHERE name = %s",
                (resumed, account if resumed else None, harness if resumed else None,
                 cwd if resumed else None, resumed, cible or None, name))
            return {"resumed": resumed, "parked": parked, "discarded": discarded,
                    "discard_reason": raison}

    def listing(self, name=None):
        sql = "SELECT " + _COLUMNS + " FROM agent_lot_sessions l"
        params: tuple = ()
        if name is not None:
            sql += " WHERE l.agent = %s"
            params = (name,)
        return self.db.query(sql + " ORDER BY l.agent, l.work_item_id", params)

    def actionable(self, name, *, today=None):
        from ...planned_waits import HOLD_NOTE_PATTERNS   # tardif : pas de cycle
        today = today or _dt.date.today().isoformat()
        lift = HOLD_NOTE_PATTERNS[1]
        rows = self.db.query(
            f"""
            SELECT w.id, w.title, w.state, w.priority, w.source AS roadmap_source,
                   extract(epoch from greatest(w.updated_at, w.branch_head_at, ev.at, mb.at)
                           )::float8 AS activity_ts,
                   coalesce(l.wakes, 0)::bigint AS wakes,
                   extract(epoch from l.last_wake_at)::float8     AS last_wake_ts,
                   extract(epoch from l.wake_activity_at)::float8 AS wake_activity_ts
              FROM work_items w
              LEFT JOIN agent_lot_sessions l ON l.agent = w.assignee AND l.work_item_id = w.id
              LEFT JOIN LATERAL (
                  SELECT max(e.created_at) AS at FROM work_item_events e
                   WHERE e.work_item_id = w.id) ev ON true
              LEFT JOIN LATERAL (
                  SELECT max(b.created_at) AS at FROM agent_mailbox b
                   WHERE btrim(b.work_item_id) = w.id::text) mb ON true
             WHERE w.assignee = %s
               AND (w.state IN ('{lifecycle.REQUESTED}', '{lifecycle.IN_PROGRESS}')
                    OR (w.state = '{lifecycle.QA}' AND (""" + freeze_due("w") + """
                        OR """ + _current_verdict("w") + """ = 'blocked')))
               AND NOT """ + start_defers("w", "%s::date") + """
               AND NOT """ + wake_defers("w") + """
               AND NOT EXISTS (SELECT 1 FROM agent_mailbox b
                                WHERE b.delivered_at IS NULL AND b.kind = 'request'
                                  AND (b.payload -> 'decision') IS NOT NULL
                                  AND btrim(b.work_item_id) = w.id::text)
               AND NOT EXISTS (SELECT 1 FROM commitments c
                                WHERE c.work_item_id = w.id AND c.kind = 'decision'
                                  AND c.status IN ('proposed', 'open'))
               AND NOT EXISTS (SELECT 1 FROM work_item_dependencies d
                                 JOIN work_items b ON b.id = d.blocker_id
                                WHERE d.work_item_id = w.id AND b.state NOT IN """ + _CLOSED + """)
               AND NOT EXISTS (
                   SELECT 1 FROM (
                       SELECT e.id, e.created_at, e.state FROM work_item_events e
                        WHERE e.work_item_id = w.id
                          AND starts_with(lower(btrim(e.note)), %s)
                          AND (starts_with(e.actor, 'human:')
                               OR EXISTS (SELECT 1 FROM agent_registry r
                                           WHERE r.name = e.actor
                                              OR 'agent:' || r.name = e.actor))
                        ORDER BY e.created_at DESC, e.id DESC LIMIT 1) h
                    WHERE NOT EXISTS (
                          SELECT 1 FROM work_item_events e
                           WHERE e.work_item_id = w.id
                             AND (e.created_at, e.id) > (h.created_at, h.id)
                             AND (e.state IS DISTINCT FROM h.state
                                  OR (coalesce(btrim(e.note), '') <> ''
                                      AND NOT starts_with(lower(btrim(e.note)), %s)
                                      AND (e.actor IN (w.assignee, 'agent:' || w.assignee)
                                           OR starts_with(e.actor, 'human:')))
                                  OR btrim(e.note, %s) ~* %s)))
             ORDER BY w.priority ASC NULLS LAST, w.created_at ASC, w.id ASC
            """,
            (name, today, self.HOLD_PREFIX, self.WAKE_PREFIX,
             HOLD_NOTE_WHITESPACE, lift))
        # #4901 : la note du pilote peut lever le hold SQL ci-dessus tout en
        # déclarant la feuille de route épuisée. Le lot reste ouvert, mais ne
        # doit plus réveiller le pilote par la règle 1. Même lecture que le
        # sommeil : dernière note du projet, dans l'ordre du journal.
        from ... import file_decoupage
        from .work import WorkItems
        projects = {}
        for row in rows:
            source = row.pop("roadmap_source", None) or ""
            if source.startswith(file_decoupage.SOURCE_PREFIX):
                projects[row["id"]] = source[len(file_decoupage.SOURCE_PREFIX):]
        if not projects:
            return rows
        notes = {item_id: [] for item_id in projects}
        for event in WorkItems(self.db).events_noted(
                file_decoupage.ROADMAP_PREFIX, 0, ids=list(projects)):
            notes[event["work_item_id"]].append(event)
        return [row for row in rows if row["id"] not in projects or
                not file_decoupage.exhausted(notes[row["id"]], projects[row["id"]])]

    def note_wake(self, name, lot, activity_ts):
        rows = self.db.query(
            "INSERT INTO agent_lot_sessions AS l"
            "       (agent, work_item_id, wakes, last_wake_at, wake_activity_at)"
            " VALUES (%s, %s, 1, clock_timestamp(), to_timestamp(%s::float8))"
            " ON CONFLICT (agent, work_item_id) DO UPDATE"
            "    SET wakes = CASE WHEN l.wake_activity_at IS NULL"
            "                       OR excluded.wake_activity_at"
            "                          > l.wake_activity_at + interval '1 millisecond'"
            "                     THEN 1 ELSE l.wakes + 1 END,"
            "        last_wake_at = excluded.last_wake_at,"
            "        wake_activity_at = greatest(l.wake_activity_at, excluded.wake_activity_at)"
            " RETURNING " + _COLUMNS,
            (name, int(lot), None if activity_ts is None else float(activity_ts)))
        return rows[0] if rows else {}

    def due_wakes(self, name, now, claim_ttl_s=120.0):
        if not has_wake_at(self.db):
            return []
        return self.db.query(
            """
            SELECT w.id, w.title, w.state, w.priority,
                   extract(epoch from w.wake_at)::float8 AS wake_ts
              FROM work_items w
             WHERE w.assignee = %s
               AND w.state NOT IN """ + _CLOSED + """
               AND w.wake_at IS NOT NULL
               AND w.wake_at <= to_timestamp(%s)
               AND (w.wake_claim_at IS NULL
                    OR w.wake_claim_at < clock_timestamp() - make_interval(secs => %s))
             ORDER BY w.wake_at ASC, w.priority ASC NULLS LAST, w.id ASC
            """,
            (name, float(now), float(claim_ttl_s)))

    def claim_wake(self, name, lot, now, claim_ttl_s=120.0):
        """#2466 : bail de livraison du réveil ; l'échéance reste armée.

        #2614 : le claim n'écrit plus rien au journal — un claim non consommé
        (drainage, report) ne laisse pas de ligne `wake:` ; c'est
        `consume_wake` qui l'écrit. `claim_ttl_s=0` renouvelle le bail du
        détenteur (battement du tour, `Worker._renew_wake_claim`)."""
        if not has_wake_at(self.db):
            return None
        rows = self.db.query(
            "UPDATE work_items SET wake_claim_at = clock_timestamp()"
            " WHERE id = %s AND assignee = %s"
            "   AND state NOT IN " + _CLOSED +
            "   AND wake_at IS NOT NULL AND wake_at <= to_timestamp(%s)"
            "   AND (wake_claim_at IS NULL"
            "        OR wake_claim_at < clock_timestamp() - make_interval(secs => %s))"
            " RETURNING id, state, title,"
            "           extract(epoch from wake_at)::float8 AS wake_ts",
            (int(lot), name, float(now), float(claim_ttl_s)))
        if not rows:
            return None
        row = rows[0]
        return {"id": row["id"], "state": row["state"], "title": row["title"],
                "wake_ts": float(row["wake_ts"])}

    def consume_wake(self, name, lot, wake_ts):
        """#2466 : le harnais du tour est lancé — l'échéance réclamée est
        consommée ; une échéance reposée entre-temps est conservée.

        #2614 : la ligne `wake: scheduled wake delivered` est écrite ici, dans
        la même transaction, une seule fois par réveil réellement délivré."""
        if not has_wake_at(self.db):
            return False
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET wake_at = NULL, wake_claim_at = NULL,"
                "       updated_at = now()"
                " WHERE id = %s AND assignee = %s"
                "   AND abs(extract(epoch from wake_at) - %s::float8) < 0.001"
                "   AND wake_claim_at IS NOT NULL"
                " RETURNING id, CASE WHEN state = 'delivered' THEN"
                " coalesce(to_jsonb(work_items)->>'legacy_state', state)"
                " ELSE state END AS state", (int(lot), name, float(wake_ts)))
            if not rows:
                return False
            tx.execute(
                "INSERT INTO work_item_events (work_item_id, state, note, actor)"
                " VALUES (%s, %s, %s, %s)",
                (int(lot), rows[0]["state"],
                 "wake: scheduled wake delivered at %s (due %s)"
                 % (_moment(time.time()), _moment(wake_ts)),
                 "ameesh"))
            return True

    def purge(self, name=None):
        """Supprime les lignes des lots fusionnés, livrés ou fermés, ou qui ne
        sont plus assignés à leur agent ; rend les lignes supprimées.

        Ne touche JAMAIS à `agent_registry` (#794, revue Claude B1 du
        2026-10-14) : ce balayage n'a pas le bail de l'agent et peut tomber en
        plein tour. La session ACTIVE d'un lot fini est fermée par l'agent qui
        la détient, à son tour suivant, sous son bail
        (`AgentWorker._close_finished_lot_session`) ; un lot seulement
        réassigné (relecture, pilotage, audit) ne ferme jamais la session de
        celui qui travaille dessus.
        """
        sql = ("DELETE FROM agent_lot_sessions l USING work_items w"
               " WHERE w.id = l.work_item_id"
               "   AND (w.state IN " + _CLOSED + " OR w.assignee IS DISTINCT FROM l.agent)")
        params: tuple = ()
        if name is not None:
            sql += " AND l.agent = %s"
            params = (name,)
        with self.db.transaction() as tx:
            return tx.query(sql + " RETURNING " + _COLUMNS, params)
