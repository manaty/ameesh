# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : comptes multiples par fournisseur (L30, migration 0028).

Seuls des NOMS de comptes passent ici : les profils (dossiers de
configuration, clés d'API) sont des secrets d'hôte, lus dans la configuration
de l'hôte par `ameesh.accounts`.

`switch` est UNE transaction : verrou de la ligne `account_active`,
comparaison avec le compte vu par l'appelant, changement, puis écriture du
journal. Deux workers qui voient le même compte au seuil n'écrivent qu'une
bascule ; le second relit le compte en place.
"""
from __future__ import annotations

from .. import interface

_ACTIVE = ("select account, forced, extract(epoch from updated_at)::float8 as updated_ts"
           " from account_active where host = %s and harness = %s")


class Accounts(interface.Accounts):

    def active(self, host, harness) -> dict | None:
        rows = self.db.query(_ACTIVE, (host, harness))
        return rows[0] if rows else None

    def init_active(self, host, harness, account) -> dict:
        self.db.execute(
            "insert into account_active (host, harness, account) values (%s, %s, %s)"
            " on conflict (host, harness) do nothing", (host, harness, account))
        return self.active(host, harness) or {"account": account, "forced": False}

    def switch(self, host, harness, *, expected, to, kind, reason, agent,
               forced=False) -> bool:
        with self.db.transaction() as tx:
            rows = tx.query(
                "select account, forced from account_active"
                " where host = %s and harness = %s for update", (host, harness))
            if rows:
                ligne = rows[0]
                if expected is not None and ligne["account"] != expected:
                    return False
                if not forced and kind != "config" and ligne["forced"]:
                    return False  # un forçage manuel n'est jamais défait par l'automate
                if ligne["account"] == to and bool(ligne["forced"]) == bool(forced):
                    return False
                tx.execute(
                    "update account_active set account = %s, forced = %s, updated_at = now()"
                    " where host = %s and harness = %s", (to, bool(forced), host, harness))
                depart = ligne["account"]
            else:
                if expected is not None:
                    return False
                tx.execute(
                    "insert into account_active (host, harness, account, forced)"
                    " values (%s, %s, %s, %s)", (host, harness, to, bool(forced)))
                depart = None
            tx.execute(
                "insert into account_switches (host, harness, from_account, to_account,"
                " kind, reason, agent) values (%s, %s, %s, %s, %s, %s, %s)",
                (host, harness, depart, to, kind, reason, agent))
        return True

    def set_auto(self, host, harness, *, reason) -> bool:
        with self.db.transaction() as tx:
            rows = tx.query(
                "select account, forced from account_active"
                " where host = %s and harness = %s for update", (host, harness))
            if not rows or not rows[0]["forced"]:
                return False
            tx.execute(
                "update account_active set forced = false, updated_at = now()"
                " where host = %s and harness = %s", (host, harness))
            tx.execute(
                "insert into account_switches (host, harness, from_account, to_account,"
                " kind, reason) values (%s, %s, %s, %s, 'auto', %s)",
                (host, harness, rows[0]["account"], rows[0]["account"], reason))
        return True

    def holds(self, host, harness) -> dict:
        rows = self.db.query(
            "select account, until_ts, reason from account_holds"
            " where host = %s and harness = %s", (host, harness))
        return {r["account"]: {"until_ts": r["until_ts"], "reason": r["reason"]} for r in rows}

    def hold(self, host, harness, account, until_ts, reason) -> None:
        self.db.execute(
            "insert into account_holds (host, harness, account, until_ts, reason)"
            " values (%s, %s, %s, %s, %s)"
            " on conflict (host, harness, account) do update"
            " set until_ts = excluded.until_ts, reason = excluded.reason, created_at = now()",
            (host, harness, account, until_ts, reason))

    def release(self, host, harness, account) -> None:
        self.db.execute(
            "delete from account_holds where host = %s and harness = %s and account = %s",
            (host, harness, account))

    def switches(self, host, harness, limit) -> list:
        clauses = ["host = %s"]
        params: list = [host]
        if harness:
            clauses.append("harness = %s")
            params.append(harness)
        params.append(int(limit))
        return self.db.query(
            "select id, harness, from_account, to_account, kind, reason, agent,"
            " extract(epoch from at)::float8 as at_ts from account_switches where "
            + " and ".join(clauses) + " order by at desc, id desc limit %s", tuple(params))
