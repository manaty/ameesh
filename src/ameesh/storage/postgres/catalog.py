# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : le catalogue des modèles (L14, migration 0015).

Deux tables, et une règle qui n'est pas ici : **ce module ne décide jamais d'un
retrait**, il exécute celui que `ameesh.catalog.reconcile` a décidé. Les trois
gardes (liste incomplète, liste vide, retrait massif) vivent là-bas, parce que ce
sont des décisions, et une décision se teste sans base.

Un modèle qui réapparaît voit son `retired_at` effacé : le retrait est un état,
pas une lápide.
"""
from __future__ import annotations

from typing import Any, Sequence

from .. import interface

def _text_array(values) -> str:
    """Un littéral de tableau Postgres, sûr pour les DEUX pilotes.

    Une liste Python traverse `%s` sans difficulté avec psycopg, et donne
    `malformed array literal` avec le pilote psql (qui échappe les paramètres
    lui-même). On construit donc le littéral une fois, pour les deux : c'est le
    genre d'écart qui ne se voit que parce que `scripts/test.sh` fait deux passes.
    """
    parts = []
    for value in values or ():
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{text}"')
    return "{" + ",".join(parts) + "}"


class Catalog(interface.Catalog):

    def upsert_models(self, models: Sequence[dict]) -> int:
        count = 0
        for model in models:
            self.db.execute(
                """
                INSERT INTO model_catalog
                    (provider, model_id, context_window, price_input, price_cached,
                     price_output, source, raw_digest)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider, model_id) DO UPDATE SET
                    context_window = coalesce(excluded.context_window, model_catalog.context_window),
                    price_input  = coalesce(excluded.price_input,  model_catalog.price_input),
                    price_cached = coalesce(excluded.price_cached, model_catalog.price_cached),
                    price_output = coalesce(excluded.price_output, model_catalog.price_output),
                    source       = excluded.source,
                    raw_digest   = coalesce(excluded.raw_digest, model_catalog.raw_digest),
                    last_seen    = now(),
                    retired_at   = NULL
                """,
                (
                    model["provider"], model["model_id"], model.get("context_window"),
                    model.get("price_input"), model.get("price_cached"),
                    model.get("price_output"), model.get("source", ""),
                    model.get("raw_digest") or None,
                ),
            )
            count += 1
        return count

    def upsert_harnesses(self, harnesses: Sequence[dict]) -> int:
        count = 0
        for item in harnesses:
            self.db.execute(
                """
                INSERT INTO model_harness
                    (provider, model_id, harness, efforts, source)
                VALUES (%s, %s, %s, %s::text[], %s)
                ON CONFLICT (provider, model_id, harness) DO UPDATE SET
                    efforts    = excluded.efforts,
                    source     = excluded.source,
                    last_seen  = now(),
                    retired_at = NULL
                """,
                (
                    item["provider"], item["model_id"], item["harness"],
                    _text_array(item.get("efforts")), item.get("source", "harness"),
                ),
            )
            count += 1
        return count

    def update_prices(self, models: Sequence[dict]) -> int:
        count = 0
        for model in models:
            # Un barème peut nommer un modèle qu'aucune source de présence n'a
            # encore vu : la ligne naît alors avec la source locale, et une ligne
            # qui existe garde SA source et son état de retrait (revue B2).
            rows = self.db.query(
                """
                INSERT INTO model_catalog
                    (provider, model_id, price_input, price_cached, price_output,
                     context_window, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider, model_id) DO UPDATE SET
                    price_input  = coalesce(excluded.price_input,  model_catalog.price_input),
                    price_cached = coalesce(excluded.price_cached, model_catalog.price_cached),
                    price_output = coalesce(excluded.price_output, model_catalog.price_output),
                    context_window = coalesce(excluded.context_window, model_catalog.context_window),
                    last_seen = now()
             RETURNING model_id
                """,
                (
                    model["provider"], model["model_id"],
                    model.get("price_input"), model.get("price_cached"),
                    model.get("price_output"), model.get("context_window"),
                    model.get("source", "prices"),
                ),
            )
            count += len(rows)
        return count

    def known(self, keys: Sequence[tuple[str, str]]) -> list[dict]:
        pairs = [f"{provider}/{model_id}" for provider, model_id in keys]
        if not pairs:
            return []
        return self.db.query(
            """
            SELECT provider, model_id, source, price_input, price_cached,
                   price_output, context_window, retired_at
              FROM model_catalog
             WHERE (provider || '/' || model_id) = ANY(%s::text[])
            """,
            (_text_array(pairs),),
        )

    def active(self, source: str) -> list[dict]:
        return self.db.query(
            """
            SELECT provider, model_id, source,
                   price_input, price_cached, price_output, context_window
              FROM model_catalog
             WHERE source = %s AND retired_at IS NULL
             ORDER BY provider, model_id
            """,
            (source,),
        )

    def retire(self, models: Sequence[dict]) -> int:
        count = 0
        for model in models:
            rows = self.db.query(
                """
                UPDATE model_catalog
                   SET retired_at = now()
                 WHERE provider = %s AND model_id = %s AND retired_at IS NULL
             RETURNING model_id
                """,
                (model["provider"], model["model_id"]),
            )
            count += len(rows)
        return count

    def listing(self, *, harness: str | None = None) -> list[dict]:
        return self.db.query(
            """
            SELECT c.provider, c.model_id, c.context_window, c.price_input,
                   c.price_cached, c.price_output, c.first_seen, c.last_seen,
                   c.retired_at, c.source,
                   h.harness, h.efforts
              FROM model_catalog c
              LEFT JOIN model_harness h
                     ON h.provider = c.provider AND h.model_id = c.model_id
             WHERE (%s::text IS NULL OR h.harness = %s)
             ORDER BY c.provider, c.model_id, h.harness
            """,
            (harness, harness),
        )

    def show(self, needle: str) -> list[dict]:
        return self.db.query(
            """
            SELECT c.provider, c.model_id, c.context_window, c.price_input,
                   c.price_cached, c.price_output, c.first_seen, c.last_seen,
                   c.retired_at, c.source, h.harness, h.efforts
              FROM model_catalog c
              LEFT JOIN model_harness h
                     ON h.provider = c.provider AND h.model_id = c.model_id
             WHERE c.model_id = %s OR c.model_id ILIKE %s
             ORDER BY c.provider, c.model_id, h.harness
            """,
            (needle, f"%{needle}%"),
        )
