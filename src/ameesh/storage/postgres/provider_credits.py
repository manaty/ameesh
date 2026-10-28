# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : crédits du compte fournisseur (#4164, migration 0500).

Une ligne par compte logique (`harness`, `account`). Les transitions sont
des écritures conditionnelles sous le verrou de ligne : entre deux runners
qui voient le même épuisement (ou la même reprise), un seul obtient la
transition — c'est lui qui écrit la note de fil. L'alerte, elle, se déduit de
la ligne (`since`), jamais de l'écrivain : une par transition.
"""
from __future__ import annotations

from .. import interface

_ROW = ("select harness, account, state, reason, detail, source, agent, host,"
        " extract(epoch from since)::float8 as since_ts,"
        " extract(epoch from checked_at)::float8 as checked_ts,"
        " extract(epoch from next_probe_at)::float8 as next_probe_ts"
        " from provider_account_credits")


class ProviderCredits(interface.ProviderCredits):

    def get(self, harness, account) -> dict | None:
        rows = self.db.query(_ROW + " where harness = %s and account = %s",
                             (harness, account or ""))
        return rows[0] if rows else None

    def listing(self) -> list[dict]:
        return self.db.query(_ROW + " order by harness, account")

    def mark_exhausted(self, harness, account, *, reason, detail, source, agent,
                       host, probe_after_s) -> bool:
        params = (reason, detail, source, agent, host, float(probe_after_s))
        inserted = self.db.query(
            "insert into provider_account_credits"
            " (harness, account, state, reason, detail, source, agent, host,"
            "  next_probe_at)"
            " values (%s, %s, 'exhausted', %s, %s, %s, %s, %s,"
            "         now() + make_interval(secs => %s))"
            " on conflict (harness, account) do nothing returning state",
            (harness, account or "") + params)
        if inserted:
            return True
        flipped = self.db.query(
            "update provider_account_credits set state = 'exhausted', since = now(),"
            " checked_at = now(), reason = %s, detail = %s, source = %s, agent = %s,"
            " host = %s, next_probe_at = now() + make_interval(secs => %s)"
            " where harness = %s and account = %s and state <> 'exhausted'"
            " returning state",
            params + (harness, account or ""))
        if flipped:
            return True
        # déjà épuisé : contrôle actualisé, `since` (clé d'alerte) inchangé
        self.db.execute(
            "update provider_account_credits set checked_at = now(), reason = %s,"
            " detail = %s, source = %s, agent = %s, host = %s,"
            " next_probe_at = greatest(next_probe_at, now() + make_interval(secs => %s))"
            " where harness = %s and account = %s",
            params + (harness, account or ""))
        return False

    def mark_ok(self, harness, account, *, source, agent=None, host=None) -> bool:
        rows = self.db.query(
            "update provider_account_credits set state = 'ok', since = now(),"
            " checked_at = now(), source = %s, agent = %s, host = %s,"
            " next_probe_at = null, reason = null, detail = null"
            " where harness = %s and account = %s and state <> 'ok'"
            " returning state",
            (source, agent, host, harness, account or ""))
        if rows:
            return True
        self.db.execute(
            "update provider_account_credits set checked_at = now()"
            " where harness = %s and account = %s",
            (harness, account or ""))
        return False

    def claim_probe(self, harness, account, interval_s) -> bool:
        rows = self.db.query(
            "update provider_account_credits"
            " set next_probe_at = now() + make_interval(secs => %s), checked_at = now()"
            " where harness = %s and account = %s and state = 'exhausted'"
            "   and (next_probe_at is null or next_probe_at <= now())"
            " returning state",
            (float(interval_s), harness, account or ""))
        return bool(rows)
