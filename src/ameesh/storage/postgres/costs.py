# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : grand livre des coûts (`turn_costs`, migration 0014).

SQL déplacé tel quel depuis `ameesh.cost` (lot L1, phase 2). Rappel des
garanties, inchangées :

* `insert` est UNE instruction (`execute`) : la ligne porte le coût du tour
  et le cumul brut qui sert de repère au tour suivant ; si elle échoue, rien
  n'est écrit et le repère n'a pas bougé (sonde codex2 B1) ;
* `last_reading` ne prend que les lignes qui portent un relevé (cumul en
  dollars ou en jetons) : une ligne sans résultat n'efface pas le repère
  (sonde codex2 B4) ;
* `spent` lit la fenêtre à l'heure de la base (`now()`).

Aucune transaction ici : le marqueur comptable (`spend_pending`) qui
suspend les tours est un domaine à part, posé et effacé par l'exécuteur.
"""
from __future__ import annotations

import json

from .. import interface

#: colonnes qu'une correction du grand livre peut écrire (L95)
_CORRECTABLE = ("usd", "model", "input_tokens", "cached_input_tokens", "output_tokens",
                "void_reason")


class TurnCosts(interface.TurnCosts):

    def last_reading(self, agent, harness, session=None) -> dict | None:
        columns = ("session, cum_usd, cum_input_tokens, cum_cached_input_tokens,"
                   " cum_output_tokens")
        where = ("where harness = %s"
                 " and (cum_usd is not null or cum_input_tokens is not null)")
        params: list = [harness]
        if agent is not None:
            # L71 : `agent=None` cherche le relevé de la session chez tout
            # agent (session reprise sous un autre nom)
            where += " and agent = %s"
            params.append(agent)
        if session is not None:
            where += " and session = %s"
            params.append(session)
        rows = self.db.query(
            "select " + columns + " from turn_costs " + where +
            " order by recorded_at desc, id desc limit 1", tuple(params))
        return rows[0] if rows else None

    def insert(self, *, agent, harness, turn, model, session, usd, input_tokens,
               cached_input_tokens, output_tokens, cum_usd, cum_input_tokens,
               cum_cached_input_tokens, cum_output_tokens, account=None,
               spend_key=None) -> bool:
        cols = ["agent", "harness", "turn", "model", "session", "usd",
                "input_tokens", "cached_input_tokens", "output_tokens",
                "cum_usd", "cum_input_tokens", "cum_cached_input_tokens",
                "cum_output_tokens"]
        values: list = [agent, harness, turn, model, session, usd, input_tokens,
                        cached_input_tokens, output_tokens, cum_usd, cum_input_tokens,
                        cum_cached_input_tokens, cum_output_tokens]
        if account is not None:
            # L30 (migration 0028) : la colonne n'est écrite que si un compte est
            # nommé — un hôte sans comptes déclarés n'en dépend pas.
            cols.append("account")
            values.append(account)
        suffix = ""
        if spend_key is not None:
            # L60 (migration 0041) : un marqueur comptable n'écrit qu'une ligne,
            # même rejoué après une écriture dont l'effacement n'a pas suivi.
            cols.append("spend_key")
            values.append(spend_key)
            suffix = " on conflict (spend_key) do nothing"
        sql = ("insert into turn_costs (" + ", ".join(cols) + ") values ("
               + ", ".join(["%s"] * len(cols)) + ")")
        if not suffix:
            self.db.execute(sql, tuple(values))
            return True
        return bool(self.db.query(sql + suffix + " returning id", tuple(values)))

    def spent(self, seconds, *, agent, harnesses, account=None) -> float:
        # L95 (0043) : une ligne écartée par `cost correct` ne compte plus
        clauses = ["recorded_at >= now() - make_interval(secs => %s)",
                   "void_reason IS NULL"]
        params: list = [float(seconds)]
        if agent != "all":
            clauses.append("agent = %s")
            params.append(agent)
        if account is not None:
            clauses.append("account = %s")
            params.append(account)
        if harnesses:
            clauses.append("harness in (%s)" % ", ".join(["%s"] * len(harnesses)))
            params.extend(harnesses)
        rows = self.db.query(
            "select coalesce(sum(usd), 0) as usd from turn_costs where "
            + " and ".join(clauses), tuple(params))
        return float(rows[0]["usd"]) if rows else 0.0

    def spent_between(self, from_ts, to_ts, *, harnesses) -> float:
        names = list(harnesses)
        if not names:
            return 0.0
        rows = self.db.query(
            "select coalesce(sum(usd), 0)::float8 as usd from turn_costs"
            " where recorded_at > to_timestamp(%s) and recorded_at <= to_timestamp(%s)"
            " and void_reason is null and harness in (" + ", ".join(["%s"] * len(names)) + ")",
            tuple([float(from_ts), float(to_ts)] + names))
        return float(rows[0]["usd"]) if rows else 0.0

    # -- corrections tracées (L95, migration 0043) ---------------------------
    def ledger(self, *, since_ts=None) -> list[dict]:
        # `to_jsonb(t)->>'void_reason'` : la colonne n'existe qu'après 0043,
        # et l'essai doit pouvoir lire une base qui ne l'a pas encore
        sql = ("SELECT t.id, t.agent, t.harness, t.turn, t.model, t.session, t.account,"
               " t.usd::float8 AS usd, t.input_tokens, t.cached_input_tokens,"
               " t.output_tokens, t.cum_usd::float8 AS cum_usd, t.cum_input_tokens,"
               " t.cum_cached_input_tokens, t.cum_output_tokens,"
               " to_jsonb(t)->>'void_reason' AS void_reason,"
               " extract(epoch from t.recorded_at)::float8 AS recorded_ts"
               " FROM turn_costs t")
        params: tuple = ()
        if since_ts is not None:
            sql += " WHERE t.recorded_at >= to_timestamp(%s)"
            params = (float(since_ts),)
        return self.db.query(sql + " ORDER BY t.recorded_at, t.id", params)

    def correct(self, *, run_id, actor, corrections) -> int:
        done = 0
        with self.db.transaction() as tx:
            for item in corrections:
                values = {k: v for k, v in (item.get("set") or {}).items()
                          if k in _CORRECTABLE}
                if not values:
                    continue
                logged = tx.query(
                    "INSERT INTO turn_cost_corrections (run_id, turn_cost_id, kind, reason,"
                    " old_row, new_values, actor)"
                    " SELECT %s, t.id, %s, %s, to_jsonb(t), %s::jsonb, %s"
                    " FROM turn_costs t WHERE t.id = %s AND t.void_reason IS NULL"
                    " RETURNING id",
                    (run_id, item["kind"], item["reason"], json.dumps(values, sort_keys=True),
                     actor, int(item["id"])))
                if not logged:
                    continue
                cols = sorted(values)
                tx.execute(
                    "UPDATE turn_costs SET " + ", ".join("%s = %%s" % c for c in cols)
                    + " WHERE id = %s",
                    tuple(values[c] for c in cols) + (int(item["id"]),))
                done += 1
        return done
