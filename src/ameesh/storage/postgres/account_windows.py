# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : fenêtre courte du fournisseur, par compte (#3779).

Les journaux `account_rate_events` (429) et `account_turn_starts` (débuts de
tour) de la migration 0491 ne portent que des noms, des hôtes et le texte
d'erreur borné du fournisseur — aucun secret, aucune invite. Les instants sont
ceux de la base (`now()`), communs aux hôtes : une passe d'alertes ou un
exécuteur d'un autre fuseau ne décale ni le cooldown ni l'écart entre débuts.
"""
from __future__ import annotations

from .. import interface


class AccountWindows(interface.AccountWindows):

    def record(self, harness, account, host, agent, kind, detail) -> None:
        self.db.execute(
            "insert into account_rate_events"
            " (harness, account, host, agent, kind, detail)"
            " values (%s, %s, %s, %s, %s, %s)",
            (harness, account, host or "", agent or "", kind, detail),
        )

    def recent(self, harness, account, since_s) -> dict:
        rows = self.db.query(
            """
            SELECT count(*)::bigint AS count,
                   extract(epoch FROM max(at))::float8 AS last_at_ts,
                   (array_agg(agent ORDER BY at DESC))[1] AS last_agent,
                   (array_agg(host ORDER BY at DESC))[1] AS last_host,
                   (array_agg(detail ORDER BY at DESC))[1] AS last_detail
              FROM account_rate_events
             WHERE harness = %s AND account = %s
               AND at >= now() - make_interval(secs => %s)
            """,
            (harness, account, float(since_s)),
        )
        return rows[0] if rows else {
            "count": 0, "last_at_ts": None, "last_agent": None,
            "last_host": None, "last_detail": None,
        }

    def active(self, harness, account) -> list[dict]:
        # #3779 (avis cx2) : la place est celle du TOUR (harnais et compte
        # effectifs), pas la ligne `agent_registry.harness` de l'agent : un
        # agent Claude/DeepSeek qui emprunte Codex pose une place codex et doit
        # compter dans le plafond Codex. Le bail vivant reste lu au registre.
        return self.db.query(
            """
            SELECT s.agent AS name, s.host,
                   extract(epoch FROM s.at)::float8 AS status_since_ts
              FROM account_turn_slots s
              JOIN agent_registry r ON r.name = s.agent
             WHERE s.harness = %s AND s.account = %s
               AND r.status = 'running'
               AND r.lease_owner IS NOT NULL
               AND r.lease_expires_at > clock_timestamp()
             ORDER BY s.at, s.agent
            """,
            (harness, account),
        )

    def start(self, harness, account, host, agent) -> None:
        self.db.execute(
            "insert into account_turn_starts (harness, account, host, agent)"
            " values (%s, %s, %s, %s)",
            (harness, account, host or "", agent or ""),
        )
        # La place active porte le harnais et le compte EFFECTIFS du tour ;
        # elle est écrite dans la transaction d'admission, avec `begin_turn`.
        # Un agent n'a qu'un tour actif : la clé agent remplace la place d'un
        # tour précédent (rotation, emprunt, retour au harnais propre).
        self.db.execute(
            "insert into account_turn_slots (agent, harness, account, host, at)"
            " values (%s, %s, %s, %s, now())"
            " on conflict (agent) do update set"
            " harness = excluded.harness, account = excluded.account,"
            " host = excluded.host, at = excluded.at",
            (agent or "", harness, account, host or ""),
        )

    def last_start(self, harness, account) -> dict:
        rows = self.db.query(
            """
            SELECT extract(epoch FROM max(at))::float8 AS last_at_ts,
                   (array_agg(agent ORDER BY at DESC))[1] AS last_agent,
                   (array_agg(host ORDER BY at DESC))[1] AS last_host
              FROM account_turn_starts
             WHERE harness = %s AND account = %s
            """,
            (harness, account),
        )
        return rows[0] if rows else {
            "last_at_ts": None, "last_agent": None, "last_host": None,
        }
