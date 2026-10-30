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

    def last_before(self, agent, ts) -> dict | None:
        rows = self.db.query(
            "select harness, session, extract(epoch from recorded_at)::float8 as recorded_ts"
            " from turn_costs where agent = %s and recorded_at <= to_timestamp(%s)"
            " order by recorded_at desc, id desc limit 1", (agent, float(ts)))
        return rows[0] if rows else None

    def first_after(self, agent, ts) -> dict | None:
        rows = self.db.query(
            "select harness, session, extract(epoch from recorded_at)::float8 as recorded_ts"
            " from turn_costs where agent = %s and recorded_at >= to_timestamp(%s)"
            " order by recorded_at asc, id asc limit 1", (agent, float(ts)))
        return rows[0] if rows else None

    def work_item_of_turn(self, turn_id) -> int | None:
        """#4679 : le lot attribué au tour, ou None.

        Le hook post-commit écrit un reçu de commit sans lot ; la ligne du
        grand livre porte le lot servi par le tour. Une attribution ambiguë
        (plusieurs lots distincts pour le même identifiant) n'est pas devinée
        et rend None."""
        text = str(turn_id or "").strip()
        if not text:
            return None
        rows = self.db.query(
            "select distinct work_item_id from turn_costs"
            " where turn_id = %s and work_item_id is not null", (text,))
        if len(rows) == 1:
            try:
                return int(rows[0]["work_item_id"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def session_total(self, agent, session) -> dict | None:
        """#4448 : la mesure de rotation d'une session, relue du grand livre.

        Même somme que les compteurs en mémoire de l'exécuteur : l'entrée et
        la sortie de chaque tour (`input_tokens + output_tokens`). Le cache
        n'est donc inclus que là où `input_tokens` le contient déjà : Codex
        compte son entrée en cache dans `input_tokens`, Claude et DeepSeek la
        publient à part (`cached_input_tokens`) et le cache écrit n'est pas
        inclus — la convention est hétérogène selon le harnais (§5.3 du
        contrat `ameesh-context/1`). `turns` compte **tous** les tours
        inscrits, réussis ou non : c'est la définition du compteur vivant
        (`session_turns`), pas les seuls succès — sinon l'alerte et la
        rotation divergent (B cx3). Le harnais et l'instant sont ceux du
        dernier tour de la session, pour choisir la surcharge compilée du bon
        harnais. Aucun tour : None (pas une session à zéro par défaut).
        """
        rows = self.db.query(
            "select coalesce(sum(input_tokens + output_tokens), 0)::bigint as tokens,"
            " count(*)::bigint as turns,"
            " (array_agg(harness order by recorded_at desc, id desc))[1] as harness,"
            " extract(epoch from max(recorded_at))::float8 as last_ts"
            " from turn_costs where agent = %s and session = %s",
            (agent, session))
        if not rows or int(rows[0].get("turns") or 0) == 0:
            return None
        return rows[0]

    def insert(self, *, agent, harness, turn, model, session, usd, input_tokens,
               cached_input_tokens, output_tokens, cum_usd, cum_input_tokens,
               cum_cached_input_tokens, cum_output_tokens, account=None,
               spend_key=None, source=None, executor=None, lease_owner=None,
               lease_epoch=None, void_reason=None, price_fallback=None,
               cache_write_tokens=None, work_item_id=None, started_at=None,
               turn_id=None, inputs=None) -> bool:
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
        # L111 (migration 0064) : provenance et bail, écrits seulement quand
        # ils sont donnés (relais de modèle, déclaration d'un appareil médié).
        for column, value in (("source", source), ("executor", executor),
                              ("lease_owner", lease_owner), ("lease_epoch", lease_epoch)):
            if value is not None:
                cols.append(column)
                values.append(value)
        if void_reason is not None:
            # L225 : tour sans tarif connu ni majorant sûr, écarté dès l'écriture
            cols.append("void_reason")
            values.append(void_reason)
        if price_fallback is not None:
            # L225 (migration 0059) : tour facturé au repli prudent, gardé
            cols.append("price_fallback")
            values.append(price_fallback)
        if cache_write_tokens is not None:
            # L225 (0059) : écritures de cache Claude, pour `cost correct`
            cols.append("cache_write_tokens")
            values.append(int(cache_write_tokens))
        if work_item_id is not None:
            # #796 (0423) : le lot servi par le tour
            cols.append("work_item_id")
            values.append(int(work_item_id))
        if started_at is not None:
            # #796 (0423) : début du tour, epoch de l'horloge de la base
            cols.append("started_at")
            values.append(float(started_at))
        if turn_id is not None:
            # #796 (0423) : l'identifiant du tour, pour le relier à ses
            # ressources (`turn_resources.turn_id`)
            cols.append("turn_id")
            values.append(str(turn_id))
        if inputs is not None:
            # L80b (0472) : les messages servis par ce tour, en JSON array.
            cols.append("inputs")
            values.append(json.dumps([str(i) for i in inputs], ensure_ascii=False))
        suffix = ""
        if spend_key is not None:
            # L60 (migration 0041) : un marqueur comptable n'écrit qu'une ligne,
            # même rejoué après une écriture dont l'effacement n'a pas suivi.
            cols.append("spend_key")
            values.append(spend_key)
            suffix = " on conflict (spend_key) do nothing"
        sql = ("insert into turn_costs (" + ", ".join(cols) + ") values ("
               + ", ".join("to_timestamp(%s)" if c == "started_at"
                           else "%s::jsonb" if c == "inputs" else "%s"
                           for c in cols) + ")")
        if not suffix:
            self.db.execute(sql, tuple(values))
            return True
        return bool(self.db.query(sql + suffix + " returning id", tuple(values)))

    def spent(self, seconds, *, agent, harnesses, account=None) -> float:
        # L95 (0043) : une ligne écartée par `cost correct` ne compte plus ;
        # L111 : la déclaration d'un appareil médié ne compte pas, le relais
        # de modèle a mesuré la même dépense (`source = 'relay'`).
        clauses = ["recorded_at >= now() - make_interval(secs => %s)",
                   "void_reason IS NULL", "source <> 'device'"]
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
            " and void_reason is null and source <> 'device' and harness in ("
            + ", ".join(["%s"] * len(names)) + ")",
            tuple([float(from_ts), float(to_ts)] + names))
        return float(rows[0]["usd"]) if rows else 0.0

    def paid_lines(self, from_ts, *, harnesses) -> list[dict]:
        names = list(harnesses)
        if not names:
            return []
        return self.db.query(
            "select agent, harness, account, extract(epoch from recorded_at)::float8 as ts,"
            " usd::float8 as usd from turn_costs"
            " where recorded_at > to_timestamp(%s) and void_reason is null"
            " and source <> 'device'"
            " and harness in (" + ", ".join(["%s"] * len(names)) + ")",
            tuple([float(from_ts)] + names))

    def models_used(self, harness, from_ts, *, limit=50) -> list[dict]:
        return self.db.query(
            "SELECT coalesce(model, '') AS model, count(*) AS turns,"
            " extract(epoch from min(recorded_at))::float8 AS first_ts,"
            " extract(epoch from max(recorded_at))::float8 AS last_ts"
            " FROM turn_costs WHERE harness = %s AND recorded_at >= to_timestamp(%s)"
            " AND void_reason IS NULL AND source <> 'device'"
            " GROUP BY coalesce(model, '')"
            " ORDER BY max(recorded_at) DESC LIMIT %s",
            (str(harness), float(from_ts), int(limit)))

    def unpriced(self, since_ts, prefix) -> list[dict]:
        return self.db.query(
            "SELECT agent, harness, coalesce(model, '') AS model, count(*) AS turns,"
            " count(*) FILTER (WHERE void_reason IS NOT NULL) AS voided,"
            " coalesce(sum(usd) FILTER (WHERE void_reason IS NULL), 0)::float8 AS usd,"
            " sum(input_tokens)::bigint AS input_tokens,"
            " sum(cached_input_tokens)::bigint AS cached_input_tokens,"
            " sum(output_tokens)::bigint AS output_tokens,"
            " extract(epoch from min(recorded_at))::float8 AS first_ts,"
            " extract(epoch from max(recorded_at))::float8 AS last_ts"
            " FROM turn_costs WHERE recorded_at >= to_timestamp(%s)"
            " AND (price_fallback IS NOT NULL OR starts_with(void_reason, %s))"
            " GROUP BY agent, harness, coalesce(model, '') ORDER BY agent, harness, model",
            (float(since_ts), prefix))

    # -- corrections tracées (L95, migration 0043) ---------------------------
    def ledger(self, *, since_ts=None) -> list[dict]:
        # `to_jsonb(t)->>'void_reason'` : la colonne n'existe qu'après 0043,
        # et l'essai doit pouvoir lire une base qui ne l'a pas encore
        sql = ("SELECT t.id, t.agent, t.harness, t.turn, t.model, t.session, t.account,"
               " t.usd::float8 AS usd, t.input_tokens, t.cached_input_tokens,"
               " t.output_tokens, t.cum_usd::float8 AS cum_usd, t.cum_input_tokens,"
               " t.cum_cached_input_tokens, t.cum_output_tokens,"
               " to_jsonb(t)->>'void_reason' AS void_reason,"
               " (to_jsonb(t)->>'cache_write_tokens')::bigint AS cache_write_tokens,"
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
