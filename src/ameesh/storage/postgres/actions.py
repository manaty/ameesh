# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : actions, tentatives et journal (`actions`,
`action_attempts`, `action_events` ; 0010).

SQL déplacé tel quel depuis `ameesh.actions` (lot L1). Les transitions qui
accordent ou consomment une autorité sont des fonctions PL/pgSQL, chacune UNE
transaction qui prend ses verrous puis recontrôle les échéances à l'heure
réelle de la base, après le dernier verrou (règle de 0011_receipts.sql) :

* `ameesh_action_launch` : approved → launched, consommation du nonce
  (`ameesh_receipt_consume`) ou réservation sur le grant
  (`ameesh_standing_reserve`), création de la tentative ;
* `ameesh_action_settle` : issue d'une tentative, transition conditionnelle ;
* `ameesh_action_replace` : consommation du nonce de la décision « assumer
  le doublon », liaison et création de l'action qui remplace.

Le journal (`action_events`) est écrit par déclencheur dans la transaction
de chaque transition ; `log_event` n'écrit que les événements informatifs.
"""
from __future__ import annotations

from .. import interface

ACTION_COLUMNS = """
    action_id, project, work_item, proposed_by, connector, operation, target, args,
    class, amount, currency, policy_version, digest, dedupe, requires_receipt, approvers,
    canon, state, replaces, replaced_by, replace_approver, attempts, auth_kind, auth_approver,
    auth_nonce, auth_challenge, auth_authenticator_id, auth_grant_id, auth_by,
    external_ref, last_error, last_actor, last_note,
    extract(epoch from auth_expires_at)::float8 AS auth_expires_ts,
    extract(epoch from created_at)::float8      AS created_ts,
    extract(epoch from updated_at)::float8      AS updated_ts,
    extract(epoch from approved_at)::float8     AS approved_ts,
    extract(epoch from launched_at)::float8     AS launched_ts,
    extract(epoch from launch_deadline)::float8 AS launch_deadline_ts,
    extract(epoch from finished_at)::float8     AS finished_ts,
    (state = 'launched' AND launch_deadline < now()) AS stale
"""


class Actions(interface.Actions):

    # -- lecture -----------------------------------------------------------
    def get(self, action_id, *, with_receipts) -> dict | None:
        columns = ACTION_COLUMNS + (", auth_receipt, replace_receipt" if with_receipts else "")
        rows = self.db.query("SELECT %s FROM actions WHERE action_id = %%s" % columns,
                             (action_id,))
        return rows[0] if rows else None

    def recent(self, *, state, project, limit) -> list[dict]:
        sql = "SELECT %s FROM actions WHERE true" % ACTION_COLUMNS
        params: list = []
        if state:
            sql += " AND state = %s"
            params.append(state)
        if project:
            sql += " AND project = %s"
            params.append(project)
        sql += " ORDER BY created_at DESC, action_id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def attempts(self, action_id) -> list[dict]:
        return self.db.query(
            """
            SELECT action_id, attempt_no, auth_kind, receipt_id, approver, nonce, grant_id,
                   reservation_id, state, external_ref, error, launched_by, settled_by,
                   extract(epoch from launched_at)::float8 AS launched_ts,
                   extract(epoch from deadline_at)::float8 AS deadline_ts,
                   extract(epoch from finished_at)::float8 AS finished_ts
              FROM action_attempts WHERE action_id = %s ORDER BY attempt_no
            """, (action_id,))

    def events(self, action_id) -> list[dict]:
        return self.db.query(
            """
            SELECT id, action_id, attempt_no, event, from_state, to_state, actor, note,
                   extract(epoch from created_at)::float8 AS created_ts
              FROM action_events WHERE action_id = %s ORDER BY id
            """, (action_id,))

    def log_event(self, action_id, attempt_no, event, from_state, to_state, actor,
                  note) -> None:
        self.db.query(
            "INSERT INTO action_events (action_id, attempt_no, event, from_state, to_state, "
            "actor, note) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (action_id, attempt_no, event, from_state, to_state, actor, note))

    def last_event_note(self, action_id, event) -> str | None:
        rows = self.db.query(
            "SELECT note FROM action_events WHERE action_id = %s AND event = %s "
            "ORDER BY id DESC LIMIT 1", (action_id, event))
        return (rows[0]["note"] or "") if rows else None

    def decision_queues(self) -> tuple[list[dict], list[dict], list[dict]]:
        approvals = self.db.query(
            "SELECT %s FROM actions WHERE state = 'proposed' AND requires_receipt "
            "ORDER BY created_at" % ACTION_COLUMNS)
        unknown = self.db.query(
            "SELECT %s FROM actions WHERE state = 'unknown' AND replaced_by IS NULL "
            "ORDER BY updated_at" % ACTION_COLUMNS)
        interrupted = self.db.query(
            "SELECT %s FROM actions WHERE state = 'launched' AND launch_deadline < now() "
            "ORDER BY launched_at" % ACTION_COLUMNS)
        return approvals, unknown, interrupted

    def covering_grants(self, action_id, amount, connector, operation, action_class,
                        currency, canon="") -> list[dict]:
        """Lecture seule, sans verrou : simple pré-filtre ; la réservation se
        fait au lancement, atomiquement, et y recontrôle tout sous verrou.
        L44 (0031) : seuls les grants dont l'authentificateur est déclaré par
        `canon` (celui de l'action) la couvrent."""
        return self.db.query(
            """
            SELECT s.id, s.approver,
                   EXISTS (SELECT 1 FROM standing_reservations r
                            WHERE r.grant_id = s.id AND r.action_id = %s
                              AND r.released_at IS NULL AND r.amount = %s::bigint) AS live
              FROM standing_approvals s
             WHERE s.connector = %s AND s.operations @> jsonb_build_array(%s::text)
               AND s.action_class = %s AND s.revoked_at IS NULL
               AND s.valid_until > clock_timestamp()
               AND (%s::bigint = 0 OR s.currency = %s)
               AND EXISTS (SELECT 1 FROM authenticators a
                            WHERE a.id = s.authenticator_id AND a.revoked_at IS NULL
                              AND a.canon = %s)
             ORDER BY s.valid_until, s.id
            """,
            (action_id, amount, connector, operation, action_class, amount, currency, canon),
        )

    def launched(self, *, action_id, grace, force) -> list[dict]:
        sql = ("SELECT action_id, attempts FROM actions WHERE state = 'launched'")
        params: list = []
        if action_id is not None:
            sql += " AND action_id = %s"
            params.append(action_id)
        if not force:
            sql += " AND launch_deadline + make_interval(secs => %s::double precision) < now()"
            params.append(float(grace))
        return self.db.query(sql, tuple(params))

    # -- transitions -------------------------------------------------------
    def propose(self, *, action_id, project, work_item, proposed_by, connector, operation,
                target, args_json, action_class, amount, currency, policy_version, digest,
                dedupe, requires_receipt, approvers_json, note) -> None:
        self.db.query(
            """
            INSERT INTO actions
                (action_id, project, work_item, proposed_by, connector, operation, target, args,
                 class, amount, currency, policy_version, digest, dedupe, requires_receipt,
                 approvers, last_actor, last_note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s)
            RETURNING action_id
            """,
            (action_id, project, work_item, proposed_by, connector, operation, target,
             args_json, action_class, amount, currency, policy_version, digest,
             dedupe, requires_receipt, approvers_json, proposed_by, note),
        )

    def bind(self, action_id, *, from_states, digest, auth, by) -> bool:
        """`→ approved` si l'action est encore dans `from_states`, non
        remplacée, et que son empreinte est `digest` ; faux sinon."""
        placeholders = ", ".join(["%s"] * len(from_states))
        rows = self.db.query(
            """
            UPDATE actions
               SET state = 'approved', auth_kind = %s, auth_receipt = %s, auth_approver = %s,
                   auth_nonce = %s, auth_challenge = %s, auth_authenticator_id = %s::bigint,
                   auth_expires_at = to_timestamp(%s::double precision), auth_grant_id = %s::bigint,
                   auth_by = %s, approved_at = now(), last_actor = %s, last_note = %s
             WHERE action_id = %s AND state IN (__FROM__) AND replaced_by IS NULL AND digest = %s
            RETURNING action_id
            """.replace("__FROM__", placeholders),
            (auth["auth_kind"], auth["auth_receipt"], auth["auth_approver"], auth["auth_nonce"],
             auth["auth_challenge"], auth["auth_authenticator_id"], auth["auth_expires"],
             auth["auth_grant_id"], by or "", by or "", auth["note"], action_id,
             *from_states, digest),
        )
        return bool(rows)

    def launch(self, action_id, *, digest, auth_kind, auth_nonce, auth_grant_id, by,
               timeout, clock_skew) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM ameesh_action_launch(%s::text, %s::text, %s::text, %s::text, "
            "%s::bigint, %s::text, %s::double precision, %s::integer)",
            (action_id, digest, auth_kind, auth_nonce, auth_grant_id, by or "", timeout,
             int(clock_skew)),
        )
        return rows[0] if rows else None

    def settle(self, action_id, attempt, *, from_state, state, external_ref, error, by,
               note, settled_by, stale_after) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM ameesh_action_settle(%s::text, %s::integer, %s::text, %s::text, "
            "%s::text, %s::text, %s::text, %s::text, %s::text, %s::double precision)",
            (action_id, int(attempt), from_state, state, external_ref, error,
             by or "", note, settled_by, stale_after),
        )
        return rows[0] if rows else None

    def replace(self, action_id, *, digest, new_id, new_digest, receipt_json, approver,
                nonce, challenge, authenticator_id, by, clock_skew) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM ameesh_action_replace(%s::text, %s::text, %s::text, %s::text, "
            "%s::text, %s::text, %s::text, %s::text, %s::bigint, %s::text, %s::integer)",
            (action_id, digest, new_id, new_digest, receipt_json, approver, nonce, challenge,
             authenticator_id, by or "", int(clock_skew)),
        )
        return rows[0] if rows else None

    def cancel(self, action_id, *, by, note) -> bool:
        rows = self.db.query(
            """
            UPDATE actions SET state = 'cancelled', finished_at = now(), last_actor = %s,
                   last_note = %s
             WHERE action_id = %s AND state IN ('proposed', 'approved', 'failed')
            RETURNING action_id
            """, (by or "", note or "annulée", action_id))
        return bool(rows)
