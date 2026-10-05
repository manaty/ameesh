# SPDX-License-Identifier: AGPL-3.0-only
"""Le catalogue en base (L14) : les opérations du domaine, sur Postgres réel.

La classe part de `PgTestCase` : un **schéma jetable** créé pour la classe,
migré par `migrations.migrate`, puis `DROP SCHEMA ... CASCADE`. Ces tests passent
donc depuis une base NEUVE sans supposer un poste déjà migré — la première
version se connectait à la main et ne passait que sur ma machine, ce que la revue
a refusé à juste titre.

`scripts/test.sh` fait deux passes (psql puis psycopg) : c'est le décompte par
pilote que la revue demande, et c'est par lui qu'on a vu qu'une liste Python en
paramètre passe avec psycopg et casse avec psql.
"""
from __future__ import annotations

import unittest

from ameesh import storage

from .support import PgTestCase

SOURCE = "test-source-L14"


class CatalogStoreTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = storage.of(self.db).catalog

    def test_upsert_puis_retrait_puis_reapparition(self):
        seen = {"provider": "anthropic", "model_id": "modele-L14", "source": SOURCE,
                "context_window": 200000, "price_input": 1.0, "price_cached": 0.1,
                "price_output": 5.0, "raw_digest": "abc"}
        self.assertEqual(self.store.upsert_models([seen]), 1)
        rows = self.store.active(SOURCE)
        self.assertEqual([(r["provider"], r["model_id"], r["source"]) for r in rows],
                         [("anthropic", "modele-L14", SOURCE)])

        self.assertEqual(self.store.retire([{"provider": "anthropic", "model_id": "modele-L14"}]), 1)
        self.assertEqual(self.store.active(SOURCE), [], "un retiré n'est plus actif")

        # Il réapparaît : le retrait s'efface, et un prix absent ne remplace pas l'ancien.
        self.store.upsert_models([{"provider": "anthropic", "model_id": "modele-L14", "source": SOURCE}])
        self.assertEqual(len(self.store.active(SOURCE)), 1, "un modèle revu redevient actif")
        shown = self.store.show("modele-L14")
        self.assertTrue(shown)
        self.assertEqual(float(shown[0]["price_input"]), 1.0, "le prix connu est conservé")

    def test_update_prices_ne_touche_ni_la_source_ni_le_retrait(self):
        # B2, la partie base : un barème rafraîchit les prix, il ne rend pas un modèle
        # actif et ne prend pas la propriété de la ligne.
        self.store.upsert_models([{"provider": "anthropic", "model_id": "modele-L14",
                                   "source": SOURCE, "price_input": 1.0}])
        self.store.retire([{"provider": "anthropic", "model_id": "modele-L14"}])
        # Tarifer une ligne retirée ne la réactive pas : le prix peut se mettre à jour,
        # `retired_at` reste, et la source du fournisseur reste la sienne.
        self.store.update_prices([{"provider": "anthropic", "model_id": "modele-L14",
                                   "price_input": 9.0, "source": "prices"}])
        self.assertEqual(self.store.active(SOURCE), [], "un barème ne réactive pas une ligne retirée")
        self.assertEqual(self.store.active(SOURCE), [])
        self.store.upsert_models([{"provider": "anthropic", "model_id": "modele-L14", "source": SOURCE}])
        self.assertEqual(self.store.update_prices([{"provider": "anthropic", "model_id": "modele-L14",
                                                    "price_input": 9.0}]), 1)
        rows = self.store.show("modele-L14")
        self.assertEqual(rows[0]["source"], SOURCE, "un barème ne vole pas la source")
        self.assertEqual(float(rows[0]["price_input"]), 9.0)

    def test_harnais_et_listing(self):
        self.store.upsert_models([{"provider": "anthropic", "model_id": "modele-L14", "source": SOURCE}])
        self.store.upsert_harnesses([{"provider": "anthropic", "model_id": "modele-L14",
                                      "harness": "claude", "efforts": ["low", "high"], "source": "harness"}])
        rows = [r for r in self.store.listing(harness="claude") if r["model_id"] == "modele-L14"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["harness"], "claude")
        self.assertEqual(list(rows[0]["efforts"]), ["low", "high"])
        self.assertEqual(self.store.listing(harness="codex"), [], "un autre harnais ne voit pas cette ligne")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
