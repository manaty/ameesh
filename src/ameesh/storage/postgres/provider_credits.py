# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : crédits du compte fournisseur (#4164, migration 0500 ;
prépayé et seuils bas #4166, migration 0504).

Une ligne par compte logique (`harness`, `account`). Les transitions sont
des écritures conditionnelles sous le verrou de ligne : entre deux runners
qui voient le même épuisement (ou la même reprise), un seul obtient la
transition — c'est lui qui écrit la note de fil. L'alerte, elle, se déduit de
la ligne (`since`), jamais de l'écrivain : une par transition.

#4166 : `low` ajoute un montant de référence prépayé (`prepaid_usd`,
`prepaid_at`) et le dernier relevé (`balance_usd`, `ratio`, `runway_at`). La
déclaration du prépayé est humaine (`ameesh budget prepaid set`) et ne touche
jamais `state` : un compte `exhausted` ne repasse par la sonde (#4164), et
l'évaluation qui suit la déclaration recalcule `low`/`ok`.
"""
from __future__ import annotations

from .. import interface

#: les colonnes accordées au rôle superviseur (role-superviseur.sql) : pas
#: `detail`, texte libre du fournisseur — `listing` nourrit les alertes
_COLUMNS = ("select harness, account, state, reason, source, agent, host,"
            " extract(epoch from since)::float8 as since_ts,"
            " extract(epoch from checked_at)::float8 as checked_ts,"
            " extract(epoch from next_probe_at)::float8 as next_probe_ts,"
            " prepaid_usd::float8 as prepaid_usd,"
            " extract(epoch from prepaid_at)::float8 as prepaid_ts, prepaid_by,"
            " balance_usd::float8 as balance_usd, ratio::float8 as ratio,"
            " extract(epoch from runway_at)::float8 as runway_ts")
_LISTING = _COLUMNS + " from provider_account_credits"
#: et le détail, pour l'exécuteur et la lecture d'un compte (rôle complet)
_ROW = _COLUMNS + ", detail from provider_account_credits"

#: `%s::float8` null → null (un `runway_at` inconnu n'écrase pas l'ancien)
_RUNWAY = "case when %s::float8 is null then null else to_timestamp(%s::float8) end"
#: idem, mais une valeur absente garde l'ancienne (mises à jour)
_RUNWAY_KEEP = ("case when %s::float8 is null then runway_at"
                " else to_timestamp(%s::float8) end")


class ProviderCredits(interface.ProviderCredits):

    def get(self, harness, account) -> dict | None:
        rows = self.db.query(_ROW + " where harness = %s and account = %s",
                             (harness, account or ""))
        return rows[0] if rows else None

    def listing(self) -> list[dict]:
        return self.db.query(_LISTING + " order by harness, account")

    def mark_exhausted(self, harness, account, *, reason, detail, source, agent,
                       host, probe_after_s, balance_usd=None, ratio=None,
                       runway_ts=None) -> bool:
        releve = (None if balance_usd is None else float(balance_usd),
                  None if ratio is None else float(ratio),
                  None if runway_ts is None else float(runway_ts))
        params = (reason, detail, source, agent, host, float(probe_after_s),
                  releve[0], releve[1], releve[2], releve[2])
        inserted = self.db.query(
            "insert into provider_account_credits"
            " (harness, account, state, reason, detail, source, agent, host,"
            "  next_probe_at, balance_usd, ratio, runway_at)"
            " values (%s, %s, 'exhausted', %s, %s, %s, %s, %s,"
            "         now() + make_interval(secs => %s), %s, %s, "
            + _RUNWAY + ")"
            " on conflict (harness, account) do nothing returning state",
            (harness, account or "") + params)
        if inserted:
            return True
        flipped = self.db.query(
            "update provider_account_credits set state = 'exhausted', since = now(),"
            " checked_at = now(), reason = %s, detail = %s, source = %s, agent = %s,"
            " host = %s, next_probe_at = now() + make_interval(secs => %s),"
            " balance_usd = coalesce(%s, balance_usd),"
            " ratio = coalesce(%s, ratio), runway_at = " + _RUNWAY_KEEP +
            " where harness = %s and account = %s and state <> 'exhausted'"
            " returning state",
            params + (harness, account or ""))
        if flipped:
            return True
        # déjà épuisé : contrôle actualisé, `since` (clé d'alerte) et échéance
        # de sonde inchangés — seule `claim_probe` avance l'échéance ; un échec
        # hors sonde ne doit pas la repousser sans fin (B mty-amh-docs-01)
        self.db.execute(
            "update provider_account_credits set checked_at = now(), reason = %s,"
            " detail = %s, source = %s, agent = %s, host = %s,"
            " balance_usd = coalesce(%s, balance_usd),"
            " ratio = coalesce(%s, ratio), runway_at = " + _RUNWAY_KEEP +
            " where harness = %s and account = %s",
            (reason, detail, source, agent, host, releve[0], releve[1],
             releve[2], releve[2], harness, account or ""))
        return False

    def mark_low(self, harness, account, *, reason, detail, source, agent, host,
                 balance_usd, ratio, runway_ts, allow_from_exhausted=True) -> bool:
        releve = (None if balance_usd is None else float(balance_usd),
                  None if ratio is None else float(ratio),
                  None if runway_ts is None else float(runway_ts))
        # #4166 (B1 cx2/auditeur) : l'évaluation périodique ne rouvre jamais un
        # compte `exhausted` — seule une sonde ou un relevé de solde réel le
        # fait ; un mark_exhausted concurrent ne doit pas être écrasé.
        guard = "" if allow_from_exhausted else " and state <> 'exhausted'"
        inserted = self.db.query(
            "insert into provider_account_credits"
            " (harness, account, state, reason, detail, source, agent, host,"
            "  balance_usd, ratio, runway_at)"
            " values (%s, %s, 'low', %s, %s, %s, %s, %s, %s, %s, "
            + _RUNWAY + ")"
            " on conflict (harness, account) do nothing returning state",
            (harness, account or "", reason, detail, source, agent, host,
             releve[0], releve[1], releve[2], releve[2]))
        if inserted:
            return True
        # transition depuis `ok` (le compte baisse) ou `exhausted` (un relevé
        # de solde montre des crédits revenus, mais bas) : nouvel épisode
        flipped = self.db.query(
            "update provider_account_credits set state = 'low', since = now(),"
            " checked_at = now(), reason = %s, detail = %s, source = %s,"
            " agent = %s, host = %s, balance_usd = %s, ratio = %s,"
            " runway_at = " + _RUNWAY + ", next_probe_at = null"
            " where harness = %s and account = %s and state <> 'low'" + guard
            + " returning state",
            (reason, detail, source, agent, host, releve[0], releve[1],
             releve[2], releve[2], harness, account or ""))
        if flipped:
            return True
        # déjà `low` : le relevé bouge, pas l'épisode (une alerte par `since`)
        self.db.execute(
            "update provider_account_credits set checked_at = now(), reason = %s,"
            " detail = %s, source = %s, agent = %s, host = %s, balance_usd = %s,"
            " ratio = %s, runway_at = " + _RUNWAY +
            " where harness = %s and account = %s" + guard,
            (reason, detail, source, agent, host, releve[0], releve[1],
             releve[2], releve[2], harness, account or ""))
        return False

    def mark_ok(self, harness, account, *, source, agent=None, host=None,
                balance_usd=None, ratio=None, runway_ts=None,
                allow_from_exhausted=True) -> bool:
        releve = (None if balance_usd is None else float(balance_usd),
                  None if ratio is None else float(ratio),
                  None if runway_ts is None else float(runway_ts))
        guard = "" if allow_from_exhausted else " and state <> 'exhausted'"
        rows = self.db.query(
            "update provider_account_credits set state = 'ok', since = now(),"
            " checked_at = now(), source = %s, agent = %s, host = %s,"
            " next_probe_at = null, reason = null, detail = null,"
            " balance_usd = coalesce(%s, balance_usd),"
            " ratio = coalesce(%s, ratio), runway_at = " + _RUNWAY +
            " where harness = %s and account = %s and state <> 'ok'" + guard
            + " returning state",
            (source, agent, host, releve[0], releve[1], releve[2], releve[2],
             harness, account or ""))
        if rows:
            return True
        # déjà `ok` : pas de transition, mais le relevé affiché est à jour
        self.db.execute(
            "update provider_account_credits set checked_at = now(),"
            " balance_usd = coalesce(%s, balance_usd),"
            " ratio = coalesce(%s, ratio), runway_at = " + _RUNWAY +
            " where harness = %s and account = %s" + guard,
            (releve[0], releve[1], releve[2], releve[2], harness, account or ""))
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

    # -- #4166 : le prépayé déclaré par un humain ----------------------------
    def declare_prepaid(self, harness, account, *, usd, actor) -> tuple:
        with self.db.transaction() as tx:
            rows = tx.query(
                "select prepaid_usd::float8 as prepaid_usd"
                " from provider_account_credits"
                " where harness = %s and account = %s for update",
                (harness, account or ""))
            old = rows[0]["prepaid_usd"] if rows else None
            if not rows:
                # un compte jamais vu : la déclaration le crée en `ok` ; c'est
                # l'évaluation qui suit (assess) qui pose `low`/`exhausted`
                tx.execute(
                    "insert into provider_account_credits"
                    " (harness, account, state, prepaid_usd, prepaid_at, prepaid_by,"
                    "  checked_at, balance_usd, ratio)"
                    " values (%s, %s, 'ok', %s, now(), %s, now(), %s, 1.0)",
                    (harness, account or "", float(usd), actor, float(usd)))
                return True, old
            tx.execute(
                "update provider_account_credits"
                " set prepaid_usd = %s, prepaid_at = now(), prepaid_by = %s,"
                "     checked_at = now(), balance_usd = %s, ratio = 1.0,"
                "     runway_at = null"
                " where harness = %s and account = %s",
                (float(usd), actor, float(usd), harness, account or ""))
            return True, old

    def clear_prepaid(self, harness, account, *, actor) -> tuple:
        with self.db.transaction() as tx:
            rows = tx.query(
                "select prepaid_usd::float8 as prepaid_usd"
                " from provider_account_credits"
                " where harness = %s and account = %s for update",
                (harness, account or ""))
            if not rows or rows[0]["prepaid_usd"] is None:
                return False, None
            old = rows[0]["prepaid_usd"]
            tx.execute(
                "update provider_account_credits"
                " set prepaid_usd = null, prepaid_at = null, prepaid_by = null,"
                "     checked_at = now(), balance_usd = null, ratio = null,"
                "     runway_at = null"
                " where harness = %s and account = %s",
                (harness, account or ""))
            return True, old
