# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : approbations permanentes bornées (§8.3 ; 0011).

SQL déplacé tel quel depuis `ameesh.receipts` (lot L1). Toute écriture qui
accorde une autorité est une fonction PL/pgSQL, UNE transaction qui prend
ses verrous puis recontrôle les échéances (exp, iat futur, until) à l'heure
réelle de la base (`clock_timestamp()`), APRÈS le dernier verrou et
immédiatement avant d'écrire — inventaire en tête de
`migrations/0011_receipts.sql` :

* `ameesh_standing_register_spend` : consommation du nonce du reçu
  `standing` et insertion du grant (avec la dimension dépense, #7865),
  après le verrou partagé de l'authentificateur ;
* `ameesh_standing_reserve` : verrou du grant d'abord, puis révocation,
  authentificateur (verrou partagé), couverture, échéance, plafonds de
  dépense et ledger (#7865), cumul ;
* `ameesh_standing_release` : une seule libération par réservation ;
* `ameesh_spend_request_stop`, `ameesh_spend_confirm`, `ameesh_spend_state` :
  arrêt par une action du connecteur, confirmation humaine de revue et
  projection des ressources récurrentes (#7865 B1/B2).

Une échéance dépassée pendant l'attente d'un verrou annule la transaction
entière (`ameesh_echeance [expired|iat_future]`, levée en DbError) ; un
plafond de dépense la refuse par `ameesh_spend [code]` (#7865).
"""
from __future__ import annotations

from .. import interface


class Grants(interface.Grants):

    def register(self, *, approver, nonce, challenge, authenticator_id, receipt_json,
                 connector, operations_json, action_class, max_amount, currency, exp, iat,
                 until, clock_skew, registered_by, beneficiary_kind="none", beneficiary="",
                 beneficiary_clones=False, targets_json="[]", charter_line="",
                 charter_version="", max_per_month=0, max_per_day=0,
                 spend_providers_json="[]", spend_purposes_json="[]",
                 review_horizon_hours=24) -> int | None:
        rows = self.db.query(
            "SELECT ameesh_standing_register_spend(%s::text, %s::text, %s::text, %s::bigint, "
            "%s::text, %s::text, %s::jsonb, %s::text, %s::bigint, %s::text, %s::bigint, "
            "%s::bigint, %s::bigint, %s::integer, %s::text, %s::text, %s::text, "
            "%s::boolean, %s::jsonb, %s::text, %s::text, %s::bigint, %s::bigint, "
            "%s::jsonb, %s::jsonb, %s::integer) AS id",
            (approver, nonce, challenge, authenticator_id, receipt_json, connector,
             operations_json, action_class, max_amount, currency, exp, iat, until,
             int(clock_skew), registered_by, beneficiary_kind, beneficiary,
             bool(beneficiary_clones), targets_json, charter_line, charter_version,
             int(max_per_month), int(max_per_day), spend_providers_json,
             spend_purposes_json, int(review_horizon_hours)),
        )
        return rows[0].get("id") if rows else None

    def reserve(self, grant_id, action_id, amount, *, reserved_by, connector, operation,
                action_class, currency) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM ameesh_standing_reserve(%s::bigint, %s::text, %s::bigint, %s::text, "
            "%s::text, %s::text, %s::text, %s::text)",
            (int(grant_id), action_id, amount, reserved_by, connector, operation, action_class,
             currency),
        )
        return rows[0] if rows else None

    def release(self, reservation_id, reason) -> dict | None:
        rows = self.db.query("SELECT * FROM ameesh_standing_release(%s::bigint, %s::text)",
                             (int(reservation_id), reason or "failed"))
        return rows[0] if rows else None

    def live_reservations(self, action_id) -> list[dict]:
        return self.db.query(
            "SELECT id, grant_id, action_id, amount, connector, operation, action_class, "
            "currency, reserved_by, extract(epoch from reserved_at)::float8 AS reserved_ts "
            "FROM standing_reservations WHERE action_id = %s "
            "AND released_at IS NULL ORDER BY id", (action_id,))

    def candidates(self, connector, operation, action_class, amount, currency,
                   canon="", proposed_by="", target="", action_id="") -> list[dict]:
        """Simple PRÉ-FILTRE, sans verrou : la décision (révocation, échéance
        à l'heure réelle après le dernier verrou, bénéficiaire, cible, cumul,
        plafonds de dépense) est refaite par `reserve` pour chaque candidat.
        L44 (0031) : seuls les grants dont l'authentificateur est déclaré par
        `canon` (celui de l'action). #7629 : le bénéficiaire et la cible sont
        pré-filtrés ici. #7865 : un grant de dépense exige une action de
        dépense dont le fournisseur et l'objet sont dans son périmètre."""
        return self.db.query(
            """
            SELECT s.id FROM standing_approvals s
             WHERE s.connector = %s AND s.operations @> jsonb_build_array(%s::text)
               AND s.action_class = %s AND s.revoked_at IS NULL
               AND s.valid_until > clock_timestamp()
               AND s.consumed_amount + %s::bigint <= s.max_amount
               AND (%s::bigint = 0 OR s.currency = %s)
               AND ameesh_standing_beneficiary_ok(s.beneficiary_kind, s.beneficiary,
                                                  %s::text)
               AND ameesh_standing_target_ok(s.targets, %s::text)
               AND (s.max_per_month = 0 AND s.max_per_day = 0
                    OR EXISTS (SELECT 1 FROM actions a
                                WHERE a.action_id = %s::text
                                  AND a.recurrence IN ('once', 'daily')
                                  AND a.spend_provider <> '' AND a.spend_purpose <> ''
                                  AND s.spend_providers @> jsonb_build_array(a.spend_provider)
                                  AND s.spend_purposes @> jsonb_build_array(a.spend_purpose)))
               AND EXISTS (SELECT 1 FROM authenticators a
                            WHERE a.id = s.authenticator_id AND a.canon = %s)
             ORDER BY s.valid_until, s.id
            """,
            (connector, operation, action_class, amount, amount, currency, proposed_by,
             target, action_id, canon),
        )

    def revoke(self, grant_id, by) -> bool:
        rows = self.db.query(
            "UPDATE standing_approvals SET revoked_at = now(), revoked_by = %s "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id", (by, int(grant_id)))
        return bool(rows)

    def listed(self, *, include_inactive=False) -> list[dict]:
        sql = """
            SELECT s.id, s.approver, s.connector, s.operations, s.action_class,
                   s.max_amount, s.currency, s.consumed_amount, s.uses,
                   s.receipt AS receipt_json,
                   a.facade,
                   s.beneficiary_kind, s.beneficiary, s.beneficiary_clones,
                   s.targets, s.charter_line, s.charter_version,
                   s.max_per_month, s.max_per_day, s.spend_providers, s.spend_purposes,
                   s.review_horizon_hours,
                   extract(epoch from s.valid_until)::float8 AS until_ts,
                   extract(epoch from s.revoked_at)::float8  AS revoked_ts, s.revoked_by
              FROM standing_approvals s
              LEFT JOIN authenticators a ON a.id = s.authenticator_id
        """
        if not include_inactive:
            sql += (" WHERE s.revoked_at IS NULL"
                    " AND s.valid_until > clock_timestamp()")
        return self.db.query(sql + " ORDER BY s.valid_until, s.id")

    def get(self, grant_id) -> dict | None:
        rows = self.db.query(
            """
            SELECT id, approver, nonce, authenticator_id, connector, operations, action_class,
                   max_amount, currency, consumed_amount, uses,
                   beneficiary_kind, beneficiary, beneficiary_clones, targets,
                   charter_line, charter_version,
                   max_per_month, max_per_day, spend_providers, spend_purposes,
                   review_horizon_hours,
                   extract(epoch from valid_until)::float8 AS until_ts,
                   extract(epoch from revoked_at)::float8  AS revoked_ts, revoked_by
              FROM standing_approvals WHERE id = %s
            """, (int(grant_id),))
        return rows[0] if rows else None

    # -- ledger des dépenses (#7865) --------------------------------------
    #: colonnes d'une ligne du ledger avec sa ligne d'action, l'horizon de
    #: revue de son grant et l'état de la demande d'arrêt (B1/B2)
    _LEDGER_SELECT = """
        SELECT l.id, l.action_id, l.grant_id, l.reservation_id, l.approver,
               l.decided_by, l.provider, l.purpose, l.amount, l.currency,
               l.recurrence, l.state, l.started_on, l.stopped_on,
               l.stopped_by, l.stop_reason, l.confirmed_at, l.confirmed_by,
               l.confirmed_decision_id,
               l.stop_action_id, l.stop_requested_at, l.stop_requested_on,
               l.stop_requested_by,
               extract(epoch from l.started_at)::float8 AS started_ts,
               extract(epoch from l.stopped_at)::float8 AS stopped_ts,
               extract(epoch from l.confirmed_at)::float8 AS confirmed_ts,
               extract(epoch from l.created_at)::float8 AS created_ts,
               a.connector, a.target, a.project, a.work_item,
               g.review_horizon_hours, g.max_per_month, g.max_per_day,
               g.max_amount, g.currency AS grant_currency,
               s.state AS stop_action_state
          FROM spend_ledger l
          JOIN actions a ON a.action_id = l.action_id
          JOIN standing_approvals g ON g.id = l.grant_id
          LEFT JOIN actions s ON s.action_id = l.stop_action_id
    """

    def spend_state(self, grant_id) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM ameesh_spend_state(%s::bigint)", (int(grant_id),))
        return rows[0] if rows else None

    def spend_totals_at(self, grant_id, day) -> dict | None:
        rows = self.db.query(
            """
            SELECT ameesh_spend_day_spent(%s::bigint, %s::date) AS day_spent,
                   ameesh_spend_month_spent(%s::bigint, %s::date) AS month_spent,
                   ameesh_spend_total_accrued(%s::bigint, %s::date) AS total_accrued
            """, (int(grant_id), day, int(grant_id), day, int(grant_id), day))
        return rows[0] if rows else None

    def spend_ongoing(self, *, decided_by=None) -> list[dict]:
        sql = self._LEDGER_SELECT + " WHERE l.recurrence = 'daily' AND l.state = 'ongoing'"
        params: list = []
        if decided_by:
            sql += " AND l.decided_by = %s"
            params.append(decided_by)
        sql += " ORDER BY l.started_on, l.id"
        return self.db.query(sql, tuple(params))

    def spend_get(self, ledger_id) -> dict | None:
        rows = self.db.query(self._LEDGER_SELECT + " WHERE l.id = %s",
                             (int(ledger_id),))
        return rows[0] if rows else None

    def spend_for_action(self, action_id) -> dict | None:
        rows = self.db.query(self._LEDGER_SELECT + " WHERE l.action_id = %s",
                             (action_id,))
        return rows[0] if rows else None

    def spend_request_stop(self, ledger_id, action_id, *, by, on=None,
                           reason=None) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM ameesh_spend_request_stop(%s::bigint, %s::text, "
            "%s::text, %s::date, %s::text)",
            (int(ledger_id), action_id, by or "", on, reason or ""))
        return rows[0] if rows else None

    def spend_confirmation(self, decision_id) -> dict | None:
        rows = self.db.query(
            "SELECT decision_id, ledger_id, signed_at, confirmed_at, confirmed_by "
            "FROM spend_review_confirmations WHERE decision_id = %s",
            (int(decision_id),))
        return rows[0] if rows else None

    def spend_confirm(self, ledger_id, *, by, decision,
                      signed_at) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM ameesh_spend_confirm(%s::bigint, %s::text, %s::bigint, "
            "to_timestamp(%s))",
            (int(ledger_id), by or "", int(decision), float(signed_at)))
        return rows[0] if rows else None
