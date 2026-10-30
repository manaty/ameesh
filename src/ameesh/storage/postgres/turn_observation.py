# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : les faits d'un tour ou d'une requête (#2167, lot #4831).

Le lecteur de l'observation (`ameesh.turn_observation`) ne devine rien : il
reçoit les lignes qui font foi (`turn_resources`, `spend_pending`,
`turn_costs`, `agent_registry`) et une requête de la boîte
(`agent_mailbox`). Aucune colonne de contenu n'est sélectionnée : ni corps, ni
`payload`, ni signature. Le document contractuel est bâti par le module
métier, jamais ici.
"""
from __future__ import annotations

from .. import interface

#: une requête de la boîte : les métadonnées seules. `meta.consigne.turn`
#: (#2894) est le tour de la réservation qui a remis le message ; il n'est
#: retenu que pour un message effectivement remis (`delivered_at` non nul).
REQUEST_SQL = """
    SELECT id, recipient, status,
           extract(epoch from created_at)::float8 AS created_ts,
           extract(epoch from delivered_at)::float8 AS delivered_ts,
           meta->'consigne'->>'turn' AS delivered_turn_id,
           coalesce((meta ? 'retire'), false) AS withdrawn
      FROM agent_mailbox WHERE id = %s
"""

#: la ressource du tour (`running`, `done`, `orphan`), absente d'un tour qui
#: n'a pas encore pris la main.
RESOURCE_SQL = """
    SELECT turn_id, agent, host, status,
           extract(epoch from started_at)::float8 AS started_ts,
           extract(epoch from ended_at)::float8 AS ended_ts
      FROM turn_resources WHERE turn_id = %s
"""

#: le marqueur comptable encore en attente. Il est lu par `turn_id` : un
#: marqueur d'un autre tour n'est pas un fait de celui-ci (et un marqueur
#: périmé après règlement reste visible, c'est la précédence du grand livre).
MARKER_SQL = """
    SELECT agent, turn_id, turn, start_index, spend_key, model, work_item_id,
           extract(epoch from created_at)::float8 AS created_ts
      FROM spend_pending WHERE turn_id = %s
"""

#: la ligne du grand livre, par `turn_id` d'abord, sinon par la clé du
#: marqueur (ligne d'avant 0423, ou réparation). La plus récente gagne.
LEDGER_SQL = """
    SELECT turn, turn_id, agent, work_item_id, billing_account,
           usd::float8 AS usd, model, source, spend_key,
           extract(epoch from started_at)::float8 AS started_ts,
           extract(epoch from recorded_at)::float8 AS recorded_ts
      FROM turn_costs
     WHERE turn_id = %s OR (spend_key IS NOT NULL AND spend_key = %s)
     ORDER BY id DESC LIMIT 1
"""

#: l'agent observé, ses compteurs et le bail qui décide de `turn_in_progress`.
#: `last_turn_at` et `status_since` sont les instants échus que le contrat
#: expose (`*_ts` epoch).
AGENT_SQL = """
    SELECT name, status, status_text, harness, model, session_id,
           extract(epoch from status_since)::float8 AS status_since_ts,
           turns, budget_usd::float8 AS budget_usd, spent_usd::float8 AS spent_usd,
           extract(epoch from last_turn_at)::float8 AS last_turn_ts,
           lease_owner, lease_epoch,
           extract(epoch from lease_expires_at)::float8 AS lease_expires_ts,
           (lease_owner IS NOT NULL AND lease_expires_at > clock_timestamp())
               AS lease_live
      FROM agent_registry WHERE name = %s
"""


class TurnObservation(interface.TurnObservation):
    """Les faits, sur la connexion (ou la transaction) courante."""

    def read(self, *, turn: str | None = None, request: int | None = None) -> dict | None:
        if request is not None:
            rows = self.db.query(REQUEST_SQL, (int(request),))
            if not rows:
                return None
            message = rows[0]
            agent_name = message["recipient"]
            turn_id = message.get("delivered_turn_id") or None
        else:
            message = None
            turn_id = str(turn)
            agent_name = None
        resource = marker = ledger = None
        if turn_id:
            rows = self.db.query(RESOURCE_SQL, (turn_id,))
            resource = rows[0] if rows else None
            rows = self.db.query(MARKER_SQL, (turn_id,))
            marker = rows[0] if rows else None
            rows = self.db.query(LEDGER_SQL, (turn_id,
                                              (marker or {}).get("spend_key")))
            ledger = rows[0] if rows else None
            if agent_name is None:
                agent_name = (resource or ledger or marker or {}).get("agent")
        if agent_name is None:
            return None
        rows = self.db.query(AGENT_SQL, (agent_name,))
        if not rows:
            return None
        return {
            "request": message,
            "turn": {"id": turn_id, "resource": resource,
                     "marker": marker, "ledger": ledger},
            "agent": rows[0],
        }
