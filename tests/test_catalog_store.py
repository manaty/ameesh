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

from ameesh import catalog, storage
from ameesh.discovery.base import ModelSeen, SourceResult

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

    def test_upsert_models_preserves_the_source_owner(self):
        """#4312 (N de l'auditeur sur #4291) : une autre source qui revoit le
        même modèle met à jour ses mesures, jamais la propriété de la ligne.
        Le retrait d'une source ne voit que ses propres lignes : changer
        `source` ferait échapper le modèle à celui qui l'a créé."""
        self.store.upsert_models([{"provider": "anthropic", "model_id": "modele-4312",
                                   "source": "provider-A", "price_input": 1.0}])
        self.store.upsert_models([{"provider": "anthropic", "model_id": "modele-4312",
                                   "source": "provider-B", "price_output": 5.0}])
        rows = self.store.show("modele-4312")
        self.assertEqual(rows[0]["source"], "provider-A",
                         "le second passage ne vole pas la source")
        self.assertEqual(float(rows[0]["price_input"]), 1.0)
        self.assertEqual(float(rows[0]["price_output"]), 5.0)
        self.assertEqual(self.store.active("provider-B"), [],
                         "provider-B ne peut pas retirer une ligne de provider-A")

    def test_update_prices_then_presence_then_withdrawal(self):
        """#4312 (B de l'auditeur du 29/10) : une ligne née du barème (source
        locale) doit être reprise par la source de présence qui la revoit ;
        sinon `active(source)` ne la voit jamais et le retrait du fournisseur
        la laisse active. Le prix du barème survit à la reprise."""
        self.store.update_prices([{"provider": "anthropic",
                                   "model_id": "modele-4312-prices",
                                   "price_input": 3.0, "source": "prices"}])
        self.assertEqual([row["source"] for row in self.store.active("prices")],
                         ["prices"])
        self.assertEqual(self.store.active("provider-A"), [],
                         "une source de présence ne voit pas encore la ligne")

        seen = SourceResult(source="provider-A", complete=True, models=(
            ModelSeen(provider="anthropic", model_id="modele-4312-prices",
                      source="provider-A", price_output=7.0),
            ModelSeen(provider="anthropic", model_id="modele-4312-keep",
                      source="provider-A"),
        ))
        catalog.record(self.store, seen)
        active = {row["model_id"]: row for row in self.store.active("provider-A")}
        self.assertEqual(set(active), {"modele-4312-prices", "modele-4312-keep"})
        self.assertEqual(active["modele-4312-prices"]["source"], "provider-A")
        self.assertEqual(float(active["modele-4312-prices"]["price_input"]), 3.0)
        self.assertEqual(float(active["modele-4312-prices"]["price_output"]), 7.0)

        gone = SourceResult(source="provider-A", complete=True, models=(
            ModelSeen(provider="anthropic", model_id="modele-4312-keep",
                      source="provider-A"),
        ))
        decision = catalog.record(self.store, gone)
        self.assertEqual([row.model_id for row in decision.to_retire],
                         ["modele-4312-prices"])
        self.assertEqual([row["model_id"] for row in self.store.active("provider-A")],
                         ["modele-4312-keep"])
        self.assertIsNotNone(self.store.show("modele-4312-prices")[0]["retired_at"])

    def test_upsert_models_takes_an_empty_source_line(self):
        """#4312 : une ligne sans propriétaire est reprise par la présence."""
        self.store.upsert_models([{"provider": "anthropic",
                                   "model_id": "modele-4312-vide", "source": ""}])
        self.store.upsert_models([{"provider": "anthropic",
                                   "model_id": "modele-4312-vide",
                                   "source": "provider-4312-empty"}])
        self.assertEqual([row["source"] for row in
                          self.store.active("provider-4312-empty")],
                         ["provider-4312-empty"])

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
