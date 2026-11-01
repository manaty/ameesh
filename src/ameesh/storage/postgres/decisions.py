# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : demandes de décision au propriétaire (lot L124).

Aucune table neuve : une demande est une ligne de `agent_mailbox`
(`kind = 'request'`, objet `payload.decision`, destinataire `human:<id>`).
Elle reste « non remise » (`delivered_at IS NULL`) tant qu'elle attend : la
lecture des demandes en attente passe par l'index partiel des non-lus. La
clôture (réponse ou retrait) est UNE écriture conditionnelle : un seul
gagnant entre deux réponses simultanées.

`register_chat` inscrit l'agent de conversation du propriétaire (L123) dans
`agent_registry`, en mode `externe` : aucun exécuteur ne le réclame jamais.
"""
from __future__ import annotations

import json

from .. import interface
from .mailbox import PROJECT_SQL
from .registry import NAME_LOCK_CTE, name_lock_key

#: une ligne de `agent_mailbox` qui porte une demande de décision
_IS_DECISION = "kind = 'request' AND (payload -> 'decision') IS NOT NULL"

_COLUMNS = """
    id, sender, recipient, body, payload, work_item_id, status, host,
    extract(epoch from created_at)::float8   AS created_ts,
    extract(epoch from delivered_at)::float8 AS closed_ts,
    (__PROJECT__) AS sender_project
""".replace("__PROJECT__", PROJECT_SQL.replace("%s", "agent_mailbox.sender"))

#: L233: a mail that may carry a decision written in prose
_MAIL_COLUMNS = """
    m.id, m.sender, m.recipient, m.body, m.kind, m.work_item_id,
    extract(epoch from m.created_at)::float8 AS created_ts
"""

#: les demandes en attente pour la vue par projet (`projects.board`, L124) :
#: un sous-SELECT, inséré dans la requête unique de la vue
PENDING_BOARD_SQL = """
    SELECT m.id, m.recipient AS owner, m.sender AS requester,
           extract(epoch from m.created_at)::float8 AS created_ts,
           coalesce((m.payload -> 'decision' ->> 'urgent')::boolean, false) AS urgent,
           (m.payload -> 'decision' ->> 'due_ts')::float8 AS due_ts
      FROM agent_mailbox m
     WHERE m.delivered_at IS NULL AND m.kind = 'request'
       AND (m.payload -> 'decision') IS NOT NULL
     ORDER BY m.id
     LIMIT 500
"""


def _same_forwarded_decision(original_payload, copy_payload, *, withdrawn_copy) -> bool:
    """La copie porte la demande de l'original, au retrait près (#4576)."""
    def content(payload):
        payload = dict(payload or {})
        decision = dict(payload.get("decision") or {})
        if withdrawn_copy:
            # A withdrawal only adds these two keys; every other field — the
            # question, the options, the deadline, the signature requirement —
            # must still be the original's.
            decision.pop("state", None)
            decision.pop("withdrawn", None)
        payload["decision"] = decision
        return payload
    return content(original_payload) == content(copy_payload)


class Decisions(interface.Decisions):

    def create(self, *, sender, recipient, body, payload, work_item_id, host) -> dict:
        rows = self.db.query(
            """
            INSERT INTO agent_mailbox
                (sender, recipient, body, host, kind, payload, work_item_id, status)
            VALUES (%s, %s, %s, %s, 'request', %s::jsonb, %s, 'pending')
            RETURNING id, extract(epoch from created_at)::float8 AS created_ts,
                      (__PROJECT__) AS sender_project
            """.replace("__PROJECT__", PROJECT_SQL),
            (sender, recipient, body, host,
             json.dumps(payload or {}, ensure_ascii=False), work_item_id, sender),
        )
        return rows[0]

    def get(self, decision_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM agent_mailbox WHERE id = %%s AND %s" % (_COLUMNS, _IS_DECISION),
            (int(decision_id),))
        return rows[0] if rows else None

    def repair_forward(self, decision_id, *, actor):
        # #4571/#4576: no approval or answer is created. Row locks serialize this
        # repair with close/forward; ambiguous or answered copies stay put.
        with self.db.transaction() as tx:
            rows = tx.query("SELECT id, recipient, kind, payload, meta, status, delivered_at"
                            " FROM agent_mailbox WHERE id = %s FOR UPDATE",
                            (int(decision_id),))
            if not rows:
                raise ValueError("decision not found")
            original = rows[0]
            decision = (original["payload"] or {}).get("decision")
            forwarded = (original["meta"] or {}).get("forwarded") or {}
            copy_id = forwarded.get("message_id")
            if (original["kind"] != "request" or not isinstance(decision, dict)
                    or not original["recipient"].startswith("human:")
                    or not copy_id or original["delivered_at"] is None):
                raise ValueError("not an accidentally forwarded human decision")
            if original["status"] != "delivered":
                raise ValueError("decision was already answered or withdrawn")
            copies = tx.query("SELECT id, recipient, kind, payload, meta, status"
                              " FROM agent_mailbox WHERE id = %s FOR UPDATE", (copy_id,))
            if not copies:
                raise ValueError("forwarded decision copy not found")
            copy = copies[0]
            origin = (copy["meta"] or {}).get("forwarded_from") or {}
            original_content = (original["payload"] or {}).get("decision") or {}
            copy_content = (copy["payload"] or {}).get("decision") or {}
            # The original was closed by the forward, never answered or withdrawn:
            # reopening one of those is out of scope.
            if ((original_content.get("state") or "pending") != "pending"
                    or original_content.get("answer") or original_content.get("withdrawn")):
                raise ValueError("decision was already answered or withdrawn")
            copy_state = copy_content.get("state") or "pending"
            # A copy the requester already withdrew is repairable (#4576): its
            # withdrawal record is both the proof that it cannot answer and the
            # audit this command must keep. An answered copy still refuses.
            if (copy_state not in ("pending", "withdrawn")
                    or copy_content.get("answer") or copy["status"] == "answered"
                    or (copy_state == "withdrawn" and not copy_content.get("withdrawn"))):
                raise ValueError("decision or copy was already answered or withdrawn")
            if (copy["kind"] != "request"
                    or not _same_forwarded_decision(original["payload"], copy["payload"],
                                                    withdrawn_copy=copy_state == "withdrawn")
                    or origin.get("message_id") != int(decision_id)
                    or origin.get("agent") != original["recipient"]
                    or forwarded.get("to") != copy["recipient"]
                    or (copy["meta"] or {}).get("forwarded")):
                raise ValueError("forward chain or changed copy: repair refused")
            already_withdrawn = copy_state == "withdrawn"
            if not already_withdrawn:
                record = json.dumps({"state": "withdrawn", "withdrawn": {
                    "by": actor, "why": "accidental forward repaired; original #%d restored"
                    % int(decision_id)}})
                tx.execute("UPDATE agent_mailbox SET status = 'delivered', delivered_at = now(),"
                           " payload = jsonb_set(payload, '{decision}',"
                           " (payload->'decision') || %s::jsonb),"
                           " meta = meta - 'consigne' WHERE id = %s", (record, copy_id))
            tx.execute("UPDATE agent_mailbox SET status = 'pending', delivered_at = NULL,"
                       " meta = (meta - 'forwarded') || jsonb_build_object("
                       " 'forward_repaired', jsonb_build_object('copy', %s::bigint,"
                       " 'by', %s::text, 'ts', extract(epoch from now()))) WHERE id = %s",
                       (copy_id, actor, int(decision_id)))
            result = {"decision": int(decision_id), "withdrawn_copy": int(copy_id)}
            if already_withdrawn:
                result["copy_already_withdrawn"] = True
            return result

    def pending(self, *, limit) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM agent_mailbox WHERE delivered_at IS NULL AND %s"
            " ORDER BY id LIMIT %%s" % (_COLUMNS, _IS_DECISION), (int(limit),))

    def history(self, *, limit) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM agent_mailbox WHERE delivered_at IS NOT NULL AND %s"
            " ORDER BY id DESC LIMIT %%s" % (_COLUMNS, _IS_DECISION), (int(limit),))

    def pending_for_lot(self, lot) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM agent_mailbox WHERE delivered_at IS NULL AND %s"
            " AND btrim(work_item_id) = %%s ORDER BY id" % (_COLUMNS, _IS_DECISION),
            (str(lot).strip(),))

    def supersession(self, decision_id):
        # #4995: append-only evidence, read by the authority gate; the
        # mutable payload marker is only a cache.
        rows = self.db.query(
            "SELECT supersedes, superseded_by, person, occurred_at, proof"
            " FROM decision_supersessions WHERE supersedes = %s", (int(decision_id),))
        return rows[0] if rows else None

    def close(self, decision_id, *, status, record) -> dict | None:
        from ... import decisions
        if set(record or {}) - {"state", "answer", "withdrawn"}:
            raise decisions.DecisionError("decision closure cannot change the signed request")
        # #4991: close and replace are one transaction, including on a
        # mediated executor. Lock both rows in id order; two replacements of
        # one authority cannot both win. Existing answers remain immutable.
        request = self.get(decision_id)
        target = ((request or {}).get("payload") or {}).get("decision", {}).get("supersedes")
        if status != "answered" or target is None:
            return self._close(decision_id, status=status, record=record)
        if type(target) is not int or not 0 < target < int(decision_id):
            raise decisions.DecisionError("supersedes must name an earlier answered decision")
        with self.db.transaction() as tx:
            tx.query("SELECT id FROM agent_mailbox WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
                     (target, int(decision_id)))
            repository = type(self)(tx)
            current = repository.get(decision_id)
            if not current or current.get("closed_ts") is not None:
                return None
            if (current["payload"]["decision"].get("supersedes") != target
                    or record.get("state") != "answered"):
                raise decisions.DecisionError("supersession request changed")
            closed = repository._close(decision_id, status=status, record=record)
            view = decisions.view_of(closed)
            if ((view.get("answer") or {}).get("channel") != "signed"
                    or view.get("needs_signature") is not True
                    or not decisions.verify_proof(tx, view, live=True)[0]):
                raise decisions.DecisionError("supersession requires a valid signed answer")
            if not decisions.supersession_approved(view):
                return closed
            old = repository.get(target)
            old_view = decisions.view_of(old) if old else None
            if not old_view or old_view["state"] != "answered":
                raise decisions.DecisionError("supersedes requires an answered decision; pending uses withdraw")
            decisions.require_current(tx, old_view)
            answer = view["answer"]
            # Journal first: the #4995 marker trigger only accepts a marker
            # whose logged replacement is already visible in this transaction.
            tx.execute("INSERT INTO decision_supersessions"
                       " (supersedes, superseded_by, person, occurred_at, proof)"
                       " VALUES (%s, %s, %s, to_timestamp(%s), %s::jsonb)",
                       (target, int(decision_id), answer["by"], answer["ts"],
                        json.dumps(answer["proof"])))
            tx.execute("UPDATE agent_mailbox SET payload = jsonb_set(payload,"
                       " '{decision,superseded_by}', to_jsonb(%s::bigint)) WHERE id = %s",
                       (int(decision_id), target))
            return closed

    def _close(self, decision_id, *, status, record):
        # Une seule instruction : la condition (encore en attente) et
        # l'écriture ne se séparent pas — deux réponses, un seul gagnant.
        rows = self.db.query(
            """
            UPDATE agent_mailbox
               SET status = %s, delivered_at = now(),
                   payload = jsonb_set(payload, '{decision}',
                                       (payload -> 'decision') || %s::jsonb)
             WHERE id = %s AND delivered_at IS NULL AND __IS_DECISION__
            RETURNING __COLUMNS__
            """.replace("__IS_DECISION__", _IS_DECISION).replace("__COLUMNS__", _COLUMNS),
            (status, json.dumps(record or {}, ensure_ascii=False), int(decision_id)),
        )
        return rows[0] if rows else None

    def reply_for(self, decision_id, *, sender=None) -> dict | None:
        # #3892 : le courrier déjà déposé pour cette réponse. Borné à
        # l'expéditeur (index `sender`) : le répondeur est l'humain qui a
        # gardé la réponse, jamais un homonyme. Le rejeu ne double pas le
        # courrier quand il est là, le renvoie quand il manque.
        sql = ("SELECT %s FROM agent_mailbox"
               " WHERE kind = 'reply'"
               " AND payload->'decision_answer'->>'decision' = %%s" % _COLUMNS)
        params: tuple = (str(int(decision_id)),)
        if sender is not None:
            sql += " AND sender = %s"
            params += (sender,)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT 1", params)
        return rows[0] if rows else None

    def source_mail(self, message_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM agent_mailbox m WHERE m.id = %%s" % _MAIL_COLUMNS,
            (int(message_id),))
        return rows[0] if rows else None

    def prose_candidates(self, *, recipients, since_ts, limit) -> list[dict]:
        # The cue words are matched by the caller (decisions.prose_cues): here
        # only the shape — mail written by an agent to bureau or a human, not
        # a request, reply, alert or receipt, not yet converted into a request.
        return self.db.query(
            """
            SELECT __COLUMNS__ FROM agent_mailbox m
             WHERE m.created_at >= to_timestamp(%s)
               AND (m.recipient = ANY(ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)))
                    OR m.recipient LIKE 'human:%%')
               AND coalesce(m.kind, 'notify') NOT IN ('request', 'reply', 'event', 'receipt')
               AND m.sender <> 'ameesh' AND m.sender NOT LIKE 'human:%%'
               AND m.sender NOT LIKE 'chat-%%'
               AND NOT EXISTS (
                   SELECT 1 FROM agent_mailbox r
                    WHERE r.kind = 'request' AND (r.payload -> 'decision') IS NOT NULL
                      AND r.payload -> 'decision' ->> 'source_mail' = m.id::text)
             ORDER BY m.id DESC
             LIMIT %s
            """.replace("__COLUMNS__", _MAIL_COLUMNS),
            (float(since_ts), json.dumps(list(recipients)), int(limit)))[::-1]

    def validation_register(self, decision_id, *, routing):
        self.db.execute("INSERT INTO decision_validations(decision_id, routing)"
                        " VALUES (%s, %s::jsonb)",
                        (int(decision_id), json.dumps(routing)))

    def validation_route(self, decision_id):
        rows = self.db.query("SELECT routing FROM decision_validations WHERE decision_id = %s",
                             (int(decision_id),))
        return rows[0]["routing"] if rows else None

    def validation_due(self, *, now):
        return self.db.query(
            "SELECT m.id FROM agent_mailbox m JOIN decision_validations v ON v.decision_id = m.id"
            " WHERE m.delivered_at IS NULL AND v.escalated_at IS NULL"
            " AND CASE WHEN jsonb_typeof(m.payload->'decision'->'due_ts') = 'number'"
            " THEN (m.payload->'decision'->>'due_ts')::numeric <= %s ELSE true END"
            " ORDER BY m.id", (float(now),))

    def validation_lock(self, decision_id):
        rows = self.db.query(
            "SELECT %s FROM agent_mailbox WHERE id = %%s AND %s FOR UPDATE"
            % (_COLUMNS, _IS_DECISION), (int(decision_id),))
        return rows[0] if rows else None

    def validation_escalate(self, decision_id, *, recipient, decision, body, now):
        self.db.execute(
            "UPDATE agent_mailbox SET recipient = %s, body = %s,"
            " payload = jsonb_set(payload, '{decision}', %s::jsonb) WHERE id = %s",
            (recipient, body, json.dumps(decision), int(decision_id)))
        self.db.execute(
            "UPDATE decision_validations SET escalated_at = to_timestamp(%s),"
            " routing = routing || %s::jsonb WHERE decision_id = %s",
            (float(now), json.dumps({"escalated_to": recipient}), int(decision_id)))

    def validation_event(self, decision_id, *, person, kind, now, facts, event_key):
        self.db.execute(
            "INSERT INTO validation_events(decision_id, person, kind, occurred_at, facts, event_key)"
            " VALUES (%s, %s, %s, to_timestamp(%s), %s::jsonb, %s)"
            " ON CONFLICT(decision_id, event_key) DO NOTHING",
            (int(decision_id), person, kind, float(now), json.dumps(facts), event_key))

    def validation_history(self, *, person):
        # Deliberately absent from the mediated executor's closed contract.
        return self.db.query(
            "SELECT e.decision_id, person, kind, extract(epoch from occurred_at)::float8 AS ts,"
            " facts, routing->>'domain' AS domain FROM validation_events e"
            " JOIN decision_validations v ON v.decision_id = e.decision_id"
            " WHERE person = %s ORDER BY e.id", (person,))

    def validation_followup(self, decision_id, *, followup_id, response_key):
        self.db.execute(
            "INSERT INTO validation_decision_links(source_id, target_id, relation, response_key)"
            " VALUES (%s, %s, 'contradicts', %s)",
            (int(decision_id), int(followup_id), response_key))

    def validation_followup_id(self, decision_id, *, response_key):
        rows = self.db.query(
            "SELECT target_id FROM validation_decision_links WHERE source_id = %s AND response_key = %s",
            (int(decision_id), response_key))
        return rows[0]["target_id"] if rows else None

    def register_chat(self, name, *, human, host, harness, cwd, status_text) -> dict | None:
        # L123 : un agent `execute` du même nom, ou un bail vivant, ne se
        # transforment jamais en conversation (rien n'est écrit : la ligne
        # n'est pas rendue). La session est oubliée : le hook de la nouvelle
        # session l'inscrit (`upsert_unleased` n'écrit qu'une session vide).
        rows = self.db.query(
            """
            __LOCK__
            INSERT INTO agent_registry
                (name, workstream, harness, host, cwd, session_id, status, status_text,
                 mode, responsible, created_by, last_seen, updated_at)
            SELECT %s, '', %s, %s, %s, NULL, 'idle', %s, 'externe', %s, %s, now(), now()
              FROM l
            ON CONFLICT (name) DO UPDATE SET
                harness = excluded.harness,
                host = excluded.host,
                cwd = excluded.cwd,
                session_id = NULL,
                session_account = NULL,
                status = CASE WHEN agent_registry.status IN ('stopped', 'dead')
                              THEN 'idle' ELSE agent_registry.status END,
                status_text = excluded.status_text,
                responsible = excluded.responsible,
                created_by = excluded.created_by,
                last_seen = now(),
                updated_at = now()
             WHERE agent_registry.mode = 'externe'
               AND (agent_registry.lease_owner IS NULL
                    OR agent_registry.lease_expires_at IS NULL
                    OR agent_registry.lease_expires_at <= clock_timestamp())
            RETURNING name, mode, responsible, harness, host
            """.replace("__LOCK__", NAME_LOCK_CTE),
            (name_lock_key(name), name, harness, host, cwd, status_text, human, human),
        )
        return rows[0] if rows else None
