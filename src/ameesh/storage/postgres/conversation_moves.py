# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: conversation moves (#7050, migration 0572).

Each transition takes the agent's registry row lock FIRST, then the
attempt's, and rechecks lease deadlines with `clock_timestamp()` after the
wait: the same order as `claim` and `begin_turn`, so a switch and a source
turn admission serialize on the registry row.
"""
from __future__ import annotations

import json

from ..conversation_moves import (AFTER_SWITCH, BEFORE_SWITCH, HANDOVER_TTL_S,
                                  OPEN, PLAN_TTL_S, ConversationMoves as Contract)

_COLUMNS = """move_id::text AS move_id, agent, epoch_source, state, reason,
              requested_by, source_host, source_cwd, source_owner,
              source_session, source_cache, target_host, target_cwd,
              target_owner, target_epoch, target_session, target_turn_started,
              checks, from_seq, to_seq, digest, error,
              extract(epoch FROM created_at)::float8 AS created_ts,
              extract(epoch FROM updated_at)::float8 AS updated_ts,
              extract(epoch FROM verified_at)::float8 AS verified_ts,
              extract(epoch FROM failed_at)::float8 AS failed_ts,
              (updated_at <= clock_timestamp() - make_interval(secs => %d))
                  AS stale""" % PLAN_TTL_S

_OPEN_SQL = "(%s)" % ", ".join("'%s'" % s for s in OPEN)
_BEFORE_SQL = "(%s)" % ", ".join("'%s'" % s for s in BEFORE_SWITCH)
_AFTER_SQL = "(%s)" % ", ".join("'%s'" % s for s in AFTER_SWITCH)

#: notice left to the source when the target's first turn had been launched
FAILED_AFTER_LAUNCH = (
    "Conversation move to host %s failed after its first turn was launched there "
    "(%s): part of that turn may have run. Your own session resumes here; read "
    "the shared conversation journal and the lot state before acting.")

#: #7052: the source's in-progress instruction when its host vanished mid-turn.
#: The claim replays `current_prompt` once on the target; the notice opens it.
INTERRUPTED_TURN = (
    "The previous turn on host %s was interrupted (the host went away) and may "
    "have partially run. Its instruction is replayed once: read the shared "
    "conversation journal and the lot state before acting.")


def _row(rows):
    if not rows:
        return None
    row = dict(rows[0])
    if isinstance(row.get("checks"), str):
        row["checks"] = json.loads(row["checks"])
    return row


class ConversationMoves(Contract):
    def __init__(self, db):
        self.db = db

    def _locked(self, tx, move_id):
        """Registry row, then attempt, both locked; (attempt, registry)."""
        rows = tx.query("SELECT agent FROM conversation_moves WHERE move_id = %s::uuid",
                        (move_id,))
        if not rows:
            return None, None
        registry = tx.query(
            """SELECT name, host, cwd, mode, status, lease_owner, lease_epoch, session_id,
                      current_prompt,
                      (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                       AND lease_expires_at > clock_timestamp()) AS lease_live
                 FROM agent_registry WHERE name = %s FOR UPDATE""", (rows[0]["agent"],))
        move = _row(tx.query("SELECT " + _COLUMNS + " FROM conversation_moves"
                             " WHERE move_id = %s::uuid FOR UPDATE", (move_id,)))
        return move, (registry[0] if registry else None)

    def _set(self, tx, move_id, assignments, params=()):
        return _row(tx.query(
            "UPDATE conversation_moves SET " + assignments + ", updated_at = clock_timestamp()"
            " WHERE move_id = %s::uuid RETURNING " + _COLUMNS,
            tuple(params) + (move_id,)))

    def plan(self, *, move_id, agent, target_host, reason, requested_by):
        with self.db.transaction() as tx:
            # #7093 (relecture dev3) : un alias se résout comme à l'écriture
            # (déclencheur ameesh_agent_spelling) ; sans cela, un plan nommé
            # par un alias ne relisait ni la ligne de registre ni la tentative
            # ouverte, et ouvrait une seconde ligne sous un autre nom.
            resolved = tx.query(
                "SELECT coalesce(ameesh_agent_primary_locked(%s), %s) AS name",
                (agent, agent))
            agent = resolved[0]["name"] if resolved else agent
            rows = tx.query(
                """SELECT name, host, cwd, mode, lease_owner, lease_epoch, session_id
                     FROM agent_registry WHERE name = %s FOR UPDATE""", (agent,))
            if not rows or rows[0]["mode"] != "execute" or rows[0]["host"] == target_host:
                return None
            r = rows[0]
            # B (relecture 3) : une tentative d'avant la bascule plus vieille
            # que PLAN_TTL_S ne retient plus rien — échouée ici, une nouvelle
            # peut s'ouvrir, même sur le même epoch (clé partielle, 0572)
            tx.execute(
                "UPDATE conversation_moves SET state = 'failed', failed_at = clock_timestamp(),"
                " error = 'expired before the switch', updated_at = clock_timestamp()"
                " WHERE agent = %s AND state IN " + _BEFORE_SQL + " AND updated_at <="
                " clock_timestamp() - make_interval(secs => %s)", (agent, PLAN_TTL_S))
            # #7052 : une bascule d'avatar que personne n'a réclamée (cible et
            # source disparues) ne bloque pas indéfiniment le prochain plan :
            # au-delà de HANDOVER_TTL_S, une nouvelle tentative peut s'ouvrir.
            # Un détenteur VIVANT n'est jamais périmé : son premier tour peut
            # durer (`rehydrating`), la tentative lui appartient.
            tx.execute(
                "UPDATE conversation_moves SET state = 'failed', failed_at = clock_timestamp(),"
                " error = 'handover expired before the target claimed',"
                " updated_at = clock_timestamp()"
                " WHERE agent = %s AND reason = 'preference' AND state IN "
                + _AFTER_SQL + " AND switched_at <= clock_timestamp()"
                " - make_interval(secs => %s)"
                " AND NOT EXISTS (SELECT 1 FROM agent_registry r"
                "                  WHERE r.name = %s AND r.lease_owner IS NOT NULL"
                "                    AND r.lease_expires_at > clock_timestamp())",
                (agent, HANDOVER_TTL_S, agent))
            # ON CONFLICT without a target: the (agent, epoch_source) key AND the
            # one-open-attempt index both reread instead of raising
            created = tx.query(
                """INSERT INTO conversation_moves
                       (move_id, agent, epoch_source, reason, requested_by,
                        source_host, source_cwd, source_owner, source_session,
                        target_host)
                   VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING RETURNING move_id""",
                (move_id, agent, int(r["lease_epoch"]), reason, requested_by,
                 r["host"], r["cwd"], r["lease_owner"], r["session_id"], target_host))
            row = _row(tx.query(
                "SELECT " + _COLUMNS + " FROM conversation_moves WHERE move_id = %s::uuid"
                " OR (agent = %s AND (epoch_source = %s OR state IN " + _OPEN_SQL + "))"
                " ORDER BY (move_id = %s::uuid) DESC, (state IN " + _OPEN_SQL + ") DESC,"
                " created_at DESC LIMIT 1",
                (move_id, agent, int(r["lease_epoch"]), move_id)))
            if row is not None:
                row["created"] = bool(created)
            return row

    def get(self, move_id):
        return _row(self.db.query("SELECT " + _COLUMNS + " FROM conversation_moves"
                                  " WHERE move_id = %s::uuid", (move_id,)))

    def open_for(self, agent):
        return _row(self.db.query("SELECT " + _COLUMNS + " FROM conversation_moves"
                                  " WHERE agent = %s AND state IN " + _OPEN_SQL,
                                  (agent,)))

    def release_refused(self, agent, *, owner, epoch, host):
        return bool(self.db.query(
            """UPDATE agent_registry
                  SET status = CASE WHEN pending_prompt IS NOT NULL THEN 'queued'
                                    ELSE 'idle' END,
                      status_text = 'conversation move in progress',
                      last_seen = now(), updated_at = now()
                WHERE name = %s AND status = 'running' AND host = %s
                  AND lease_owner = %s AND lease_epoch = %s
            RETURNING name""", (agent, host, owner, int(epoch))))

    def holds(self, agent):
        return bool(self.db.query(
            "SELECT 1 FROM conversation_moves WHERE agent = %s AND (state IN "
            + _AFTER_SQL + " OR (state IN " + _BEFORE_SQL + " AND updated_at >"
            " clock_timestamp() - make_interval(secs => %s))) LIMIT 1",
            (agent, PLAN_TTL_S)))

    def for_target(self, host):
        rows = self.db.query("SELECT " + _COLUMNS + " FROM conversation_moves"
                             " WHERE target_host = %s AND state IN " + _OPEN_SQL
                             + " ORDER BY created_at, move_id", (host,))
        return [_row([r]) for r in rows]

    def open_handover(self, agent):
        """#7052: the newest post-switch attempt of this `preference` role.

        A target that claimed holds a live lease: the attempt is serving and
        must not be reclaimed, so it is excluded here (the same registry row
        the switch locked, without the lock: this is only read).
        """
        return _row(self.db.query(
            """SELECT m.move_id::text AS move_id, m.agent, m.source_host,
                      m.target_host, m.state,
                      extract(epoch FROM m.switched_at)::float8 AS switched_ts,
                      extract(epoch FROM clock_timestamp())::float8 AS now_ts
                 FROM conversation_moves m
                WHERE m.agent = %s
                  AND m.reason = 'preference'
                  AND m.state IN ('switched', 'recovered', 'rehydrating')
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_registry r
                       WHERE r.name = m.agent
                         AND r.lease_owner IS NOT NULL
                         AND r.lease_expires_at > clock_timestamp())
                ORDER BY m.switched_at DESC NULLS LAST, m.created_at DESC
                LIMIT 1""", (agent,)))

    def prepare(self, move_id, *, target_host, target_cwd, checks):
        if any(not isinstance(v, bool) for v in checks.values()):
            raise ValueError("conversation move checks are booleans only")
        with self.db.transaction() as tx:
            move, registry = self._locked(tx, move_id)
            if move is None or move["target_host"] != target_host:
                return None
            if move["state"] != "planned":
                return move if move["state"] == "prepared" else None
            if move["stale"] or registry is None or registry["host"] != move["source_host"]:
                return None
            return self._set(tx, move_id, "state = 'prepared', target_cwd = %s,"
                             " checks = %s::jsonb, prepared_at = clock_timestamp()",
                             (target_cwd, json.dumps(checks, sort_keys=True)))

    def switch(self, move_id, *, target_host):
        with self.db.transaction() as tx:
            move, r = self._locked(tx, move_id)
            if move is None or r is None or move["target_host"] != target_host:
                return None
            if move["state"] in ("switched", "recovered"):
                return move
            # #7093 (relecture dev3) : un tour source plus long que
            # PLAN_TTL_S retarde la bascule, il ne l'abandonne pas — le
            # `stale` ne vaut que pour une tentative encore `planned`.
            if move["state"] != "prepared":
                return None
            if r["mode"] != "execute" or r["host"] != move["source_host"]:
                return None
            if int(r["lease_epoch"]) != int(move["epoch_source"]):
                # #7093 (relecture 1/2) : le bail a été rendu et repris depuis
                # le plan ; le détenteur est peut-être le même nom, mais ce
                # n'est plus le bail que le plan a vu.
                return self._set(tx, move_id,
                                 "state = 'failed', error = %s, failed_at = clock_timestamp()",
                                 ("switch: source lease changed since the plan",))
            if (r["session_id"] or None) != (move["source_session"] or None):
                # B4 : la session native a changé depuis le plan (rotation,
                # bascule de lot) — l'échec ne pourrait plus la rendre
                return self._set(tx, move_id,
                                 "state = 'failed', error = %s, failed_at = clock_timestamp()",
                                 ("switch: source session changed since the plan",))
            owner = r["lease_owner"] or ""
            if r["lease_live"]:
                # the turn boundary: never during a source turn, never from an
                # interactive session, never from a holder the plan did not see
                if (r["status"] == "running" or owner.startswith("attach:")
                        or owner != (move["source_owner"] or "")):
                    return None
                state = "switched"
            else:
                state = "recovered" if owner else "switched"
            # #7052 : le tour en cours sur l'hôte disparu est rejoué une fois
            # sur la cible ; la consigne humaine garde son identité
            # (`pending_prompt_id`), la mention ouvre le texte rejoué.
            notice = (INTERRUPTED_TURN % move["source_host"]
                      if state == "recovered" and r.get("current_prompt") else None)
            moved = tx.query(
                """UPDATE agent_registry
                      SET lease_owner = NULL, lease_expires_at = NULL,
                          lease_epoch = lease_epoch + 1,
                          host = %s, cwd = coalesce(%s, cwd),
                          current_prompt = CASE WHEN %s::text IS NULL
                                                THEN current_prompt
                                                ELSE %s::text || E'\\n\\n'
                                                     || current_prompt END,
                          status = CASE WHEN status IN ('running', 'dead') THEN 'idle'
                                        ELSE status END,
                          updated_at = now()
                    WHERE name = %s AND host = %s AND lease_epoch = %s
                RETURNING lease_epoch""",
                (move["target_host"], move["target_cwd"], notice, notice,
                 move["agent"], move["source_host"], r["lease_epoch"]))
            if not moved:
                return None
            return self._set(tx, move_id, "state = %s, switched_at = clock_timestamp()",
                             (state,))

    def bind(self, move_id, *, owner, epoch, host, from_seq, to_seq, digest):
        with self.db.transaction() as tx:
            move, r = self._locked(tx, move_id)
            if move is None or r is None or move["target_host"] != host:
                return None
            fenced = (r["lease_live"] and r["lease_owner"] == owner
                      and int(r["lease_epoch"]) == int(epoch) and r["host"] == host)
            if not fenced:
                return None
            if move["state"] == "rehydrating":
                same = (move["target_owner"] == owner
                        and int(move["target_epoch"]) == int(epoch))
                return move if same else None
            if move["state"] not in ("switched", "recovered"):
                return None
            return self._set(tx, move_id,
                             "state = 'rehydrating', target_owner = %s, target_epoch = %s,"
                             " from_seq = %s, to_seq = %s, digest = %s",
                             (owner, int(epoch), from_seq, to_seq, digest))

    def note_turn(self, move_id, *, owner, epoch, session):
        rows = self.db.query(
            "UPDATE conversation_moves SET target_turn_started = true,"
            " target_session = coalesce(%s, target_session), updated_at = clock_timestamp()"
            " WHERE move_id = %s::uuid AND state = 'rehydrating'"
            " AND target_owner = %s AND target_epoch = %s RETURNING move_id",
            (session, move_id, owner, int(epoch)))
        return bool(rows)

    def verify(self, move_id, *, owner, epoch, session, account, harness, cwd):
        with self.db.transaction() as tx:
            move, r = self._locked(tx, move_id)
            if move is None or r is None:
                return None
            if move["state"] == "verified":
                return move if move["target_session"] == session else None
            if (move["state"] != "rehydrating" or move["target_owner"] != owner
                    or int(move["target_epoch"]) != int(epoch)):
                return None
            if not (r["lease_live"] and r["lease_owner"] == owner
                    and int(r["lease_epoch"]) == int(epoch)
                    and r["host"] == move["target_host"]):
                return None
            # the provisional session ends here, and only here (§2.3)
            tx.execute(
                """UPDATE agent_registry
                      SET session_id = %s, session_account = %s,
                          session_harness = %s, session_cwd = %s, session_origin = %s,
                          election_brief = NULL, updated_at = now()
                    WHERE name = %s""",
                (session, account, harness, cwd, session, move["agent"]))
            return self._set(tx, move_id,
                             "state = 'verified', target_session = %s,"
                             " source_cache = 'stale-cache', verified_at = clock_timestamp()",
                             (session,))

    def fail(self, move_id, *, error, owner=None, epoch=None, only=None):
        error = (error or "failed")[:2000]
        with self.db.transaction() as tx:
            move, r = self._locked(tx, move_id)
            if move is None or move["state"] not in OPEN:
                return None
            if only is not None and move["state"] not in only:
                # #7093 (relecture dev3) : la passe cible ne défait pas une
                # tentative qu'une autre passe vient de préparer ou de basculer.
                return None
            if move["state"] in BEFORE_SWITCH or r is None:
                return self._set(tx, move_id, "state = 'failed', error = %s,"
                                 " failed_at = clock_timestamp()", (error,))
            if r["lease_live"] and not (r["lease_owner"] == owner and epoch is not None
                                        and int(r["lease_epoch"]) == int(epoch)):
                return None
            if r["host"] == move["target_host"]:
                notice = (FAILED_AFTER_LAUNCH % (move["target_host"], error)
                          if move["target_turn_started"] else None)
                tx.execute(
                    """UPDATE agent_registry
                          SET lease_owner = NULL, lease_expires_at = NULL,
                              lease_epoch = lease_epoch + 1,
                              host = %s, cwd = coalesce(%s, cwd),
                              status = CASE WHEN %s::text IS NOT NULL
                                             AND status NOT IN ('stopped', 'blocked')
                                            THEN 'queued'
                                            WHEN status IN ('running', 'dead') THEN 'idle'
                                            ELSE status END,
                              pending_prompt = CASE WHEN %s::text IS NULL THEN pending_prompt
                                                    ELSE concat_ws(E'\\n\\n', %s::text,
                                                                   pending_prompt) END,
                              -- #6487 : une consigne neuve est une occurrence neuve
                              pending_prompt_id = CASE WHEN %s::text IS NULL
                                                       THEN pending_prompt_id
                                                       ELSE gen_random_uuid() END,
                              updated_at = now()
                        WHERE name = %s""",
                    (move["source_host"], move["source_cwd"], notice, notice, notice,
                     notice, move["agent"]))
            return self._set(tx, move_id, "state = 'failed', error = %s,"
                             " failed_at = clock_timestamp()", (error,))
