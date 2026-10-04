# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : approbations permanentes bornées (§8.3 ; 0011).

SQL déplacé tel quel depuis `ameesh.receipts` (lot L1). Toute écriture qui
accorde une autorité est une fonction PL/pgSQL, UNE transaction qui prend
ses verrous puis recontrôle les échéances (exp, iat futur, until) à l'heure
réelle de la base (`clock_timestamp()`), APRÈS le dernier verrou et
immédiatement avant d'écrire — inventaire en tête de
`migrations/0011_receipts.sql` :

* `ameesh_standing_register` : consommation du nonce du reçu `standing` et
  insertion du grant, après le verrou partagé de l'authentificateur ;
* `ameesh_standing_reserve` : verrou du grant d'abord, puis révocation,
  authentificateur (verrou partagé), couverture, échéance, cumul ;
* `ameesh_standing_release` : une seule libération par réservation.

Une échéance dépassée pendant l'attente d'un verrou annule la transaction
entière (`ameesh_echeance [expired|iat_future]`, levée en DbError).
"""
from __future__ import annotations

from .. import interface


class Grants(interface.Grants):

    def register(self, *, approver, nonce, challenge, authenticator_id, receipt_json,
                 connector, operations_json, action_class, max_amount, currency, exp, iat,
                 until, clock_skew, registered_by) -> int | None:
        rows = self.db.query(
            "SELECT ameesh_standing_register(%s::text, %s::text, %s::text, %s::bigint, "
            "%s::text, %s::text, %s::jsonb, %s::text, %s::bigint, %s::text, %s::bigint, "
            "%s::bigint, %s::bigint, %s::integer, %s::text) AS id",
            (approver, nonce, challenge, authenticator_id, receipt_json, connector,
             operations_json, action_class, max_amount, currency, exp, iat, until,
             int(clock_skew), registered_by),
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
            "SELECT grant_id FROM standing_reservations WHERE action_id = %s "
            "AND released_at IS NULL ORDER BY id", (action_id,))

    def candidates(self, connector, operation, action_class, amount, currency) -> list[dict]:
        """Simple PRÉ-FILTRE, sans verrou : la décision (révocation, échéance
        à l'heure réelle après le dernier verrou, cumul) est refaite par
        `reserve` pour chaque candidat."""
        return self.db.query(
            """
            SELECT id FROM standing_approvals
             WHERE connector = %s AND operations @> jsonb_build_array(%s::text)
               AND action_class = %s AND revoked_at IS NULL AND valid_until > clock_timestamp()
               AND consumed_amount + %s::bigint <= max_amount
               AND (%s::bigint = 0 OR currency = %s)
             ORDER BY valid_until, id
            """,
            (connector, operation, action_class, amount, amount, currency),
        )

    def revoke(self, grant_id, by) -> bool:
        rows = self.db.query(
            "UPDATE standing_approvals SET revoked_at = now(), revoked_by = %s "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id", (by, int(grant_id)))
        return bool(rows)

    def get(self, grant_id) -> dict | None:
        rows = self.db.query(
            """
            SELECT id, approver, nonce, authenticator_id, connector, operations, action_class,
                   max_amount, currency, consumed_amount, uses,
                   extract(epoch from valid_until)::float8 AS until_ts,
                   extract(epoch from revoked_at)::float8  AS revoked_ts, revoked_by
              FROM standing_approvals WHERE id = %s
            """, (int(grant_id),))
        return rows[0] if rows else None
