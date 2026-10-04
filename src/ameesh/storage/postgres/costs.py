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

from .. import interface


class TurnCosts(interface.TurnCosts):

    def last_reading(self, agent, harness, session=None) -> dict | None:
        columns = ("session, cum_usd, cum_input_tokens, cum_cached_input_tokens,"
                   " cum_output_tokens")
        where = ("where agent = %s and harness = %s"
                 " and (cum_usd is not null or cum_input_tokens is not null)")
        if session is None:
            rows = self.db.query(
                "select " + columns + " from turn_costs " + where +
                " order by recorded_at desc, id desc limit 1", (agent, harness))
        else:
            rows = self.db.query(
                "select " + columns + " from turn_costs " + where + " and session = %s"
                " order by recorded_at desc, id desc limit 1", (agent, harness, session))
        return rows[0] if rows else None

    def insert(self, *, agent, harness, turn, model, session, usd, input_tokens,
               cached_input_tokens, output_tokens, cum_usd, cum_input_tokens,
               cum_cached_input_tokens, cum_output_tokens) -> None:
        self.db.execute(
            "insert into turn_costs (agent, harness, turn, model, session, usd,"
            " input_tokens, cached_input_tokens, output_tokens,"
            " cum_usd, cum_input_tokens, cum_cached_input_tokens, cum_output_tokens)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (agent, harness, turn, model, session,
             usd, input_tokens, cached_input_tokens,
             output_tokens, cum_usd, cum_input_tokens, cum_cached_input_tokens,
             cum_output_tokens))

    def spent(self, seconds, *, agent, harnesses) -> float:
        clauses = ["recorded_at >= now() - make_interval(secs => %s)"]
        params: list = [float(seconds)]
        if agent != "all":
            clauses.append("agent = %s")
            params.append(agent)
        if harnesses:
            clauses.append("harness in (%s)" % ", ".join(["%s"] * len(harnesses)))
            params.extend(harnesses)
        rows = self.db.query(
            "select coalesce(sum(usd), 0) as usd from turn_costs where "
            + " and ".join(clauses), tuple(params))
        return float(rows[0]["usd"]) if rows else 0.0
