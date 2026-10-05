# SPDX-License-Identifier: AGPL-3.0-only
"""Les événements du catalogue (L14) : ce qui est dit, et à quelles conditions.

Aucune base, aucune messagerie : `events_for` rend le texte et la charge utile,
`record` reçoit un faux domaine et un faux émetteur. C'est délibéré — un
« retrait massif refusé » doit être lisible par un humain qui n'a pas lu le code,
et cela se relit dans un test.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ameesh.catalog import ActiveModel, events_for, reconcile, record  # noqa: E402
from ameesh.discovery.base import ModelSeen, SourceResult  # noqa: E402


def result(*ids: str, source: str = "openai", complete: bool = True, raw: bytes = b"{}") -> SourceResult:
    return SourceResult(
        source=source,
        complete=complete,
        models=tuple(ModelSeen(provider="openai", model_id=model, source=source) for model in ids),
        raw=raw,
    )


class FakeStore:
    """Le domaine du stockage, en mémoire : ce que `record` lui demande, il le note."""

    def __init__(self, active: list[ActiveModel]):
        self._active = list(active)
        self._known: list[dict] = [
            {"provider": row.provider, "model_id": row.model_id, "source": row.source}
            for row in active
        ]
        self.upserted: list[dict] = []
        self.retired: list[dict] = []
        self.priced: list[dict] = []

    def active(self, source: str) -> list[dict]:
        return [
            {"provider": row.provider, "model_id": row.model_id, "source": row.source}
            for row in self._active
            if row.source == source
        ]

    def upsert_models(self, rows):
        self.upserted.extend(rows)
        return len(rows)

    def upsert_harnesses(self, rows):  # pragma: no cover - pas d'effort dans ces cas
        return len(rows)

    def known(self, keys):
        return [
            row for row in self._known if (row["provider"], row["model_id"]) in set(keys)
        ]

    def update_prices(self, rows):
        self.priced.extend(rows)
        return len(rows)

    def retire(self, rows):
        self.retired.extend(rows)
        for row in rows:
            self._active = [r for r in self._active if not (r.provider == row["provider"] and r.model_id == row["model_id"])]
        return len(rows)


class EventsTest(unittest.TestCase):
    def test_un_retrait_ordinaire_se_dit(self):
        decision = reconcile([ActiveModel("openai", "a", "openai"), ActiveModel("openai", "b", "openai")], result("a"))
        events = events_for(decision, result("a"))
        self.assertEqual(len(events), 1)
        self.assertIn("1 modèle(s) retiré(s)", events[0].body)
        self.assertEqual(events[0].payload["retired"], ["b"])

    def test_un_retrait_massif_refuse_se_dit_et_se_marque_urgent(self):
        state = [ActiveModel("openai", m, "openai") for m in "abcd"]
        decision = reconcile(state, result("a"))
        events = events_for(decision, result("a"))
        self.assertEqual(len(events), 1)
        self.assertIn("retrait massif REFUSÉ", events[0].body)
        self.assertIn("un humain doit trancher", events[0].body)
        self.assertTrue(events[0].payload["urgent"])
        self.assertEqual(len(events[0].payload["refused"]), 3)

    def test_aucun_evenement_quand_rien_ne_change(self):
        state = [ActiveModel("openai", "a", "openai")]
        decision = reconcile(state, result("a"))
        self.assertEqual(events_for(decision, result("a")), [])

    def test_un_nouveau_modele_se_dit(self):
        decision = reconcile([ActiveModel("openai", "a", "openai")], result("a", "b"))
        events = events_for(decision, result("a", "b"), appeared=("b",))
        self.assertEqual(len(events), 1)
        self.assertIn("nouveau(x) modèle(s)", events[0].body)
        self.assertEqual(events[0].payload["new"], ["b"])


class RecordTest(unittest.TestCase):
    def test_record_enregistre_puis_emet_le_refus_massif(self):
        store = FakeStore([ActiveModel("openai", m, "openai") for m in "abcd"])
        sent: list[tuple[str, dict]] = []
        decision = record(store, result("a"), emit=lambda body, payload: sent.append((body, payload)))
        self.assertEqual(decision.refused_reason, "mass-withdrawal")
        self.assertEqual(store.retired, [], "un refus massif ne retire rien")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1]["urgent"])
        # Les quatre modèles ont quand même été rafraîchis : ils ont été vus.
        self.assertEqual(len(store.upserted), 1)

    def test_record_retire_et_emet_quand_la_source_a_raison(self):
        store = FakeStore([ActiveModel("openai", "a", "openai"), ActiveModel("openai", "b", "openai")])
        sent: list[tuple[str, dict]] = []
        decision = record(store, result("a"), emit=lambda body, payload: sent.append((body, payload)))
        self.assertEqual([row["model_id"] for row in store.retired], ["b"])
        self.assertEqual(decision.refused_reason, "")
        self.assertEqual(len(sent), 1)
        self.assertIn("retiré", sent[0][0])

    def test_un_bareme_ne_ressuscite_pas_un_modele_retire_ni_ne_vole_la_source(self):
        # B2 : une source LOCALE met à jour les prix, elle ne rend pas un modèle
        # actif et ne prend pas la propriété de la ligne.
        class LocalStore(FakeStore):
            def upsert_models(self, rows):  # ne doit PAS être appelé par une source locale
                raise AssertionError("une source locale ne doit pas enregistrer une présence")

        store = LocalStore([])
        local = SourceResult(
            source="prices",
            complete=True,
            kind="local",
            models=(ModelSeen(provider="openai", model_id="a", price_input=2.0, source="prices"),),
        )
        decision = record(store, local, emit=lambda body, payload: None)
        self.assertEqual(decision.refused_reason, "local-source")
        self.assertEqual(store.retired, [])
        self.assertEqual(len(store.priced), 1)
        self.assertEqual(store.priced[0]["price_input"], 2.0)

    def test_une_source_locale_ne_retire_jamais_meme_complete(self):
        store = FakeStore([ActiveModel("openai", "a", "prices"), ActiveModel("openai", "b", "prices")])
        local = SourceResult(source="prices", complete=True, kind="local",
                             models=(ModelSeen(provider="openai", model_id="a", source="prices"),))
        decision = record(store, local, emit=lambda body, payload: None)
        self.assertEqual(store.retired, [])
        self.assertEqual(decision.refused_reason, "local-source")

    def test_le_premier_modele_d_un_catalogue_vide_est_un_evenement(self):
        # B4 : catalogue vide + premier modèle => un événement, pas un silence.
        store = FakeStore([])
        sent: list[tuple[str, dict]] = []
        record(store, result("a"), emit=lambda body, payload: sent.append((body, payload)))
        self.assertEqual(len(sent), 1, "une première découverte doit se dire")
        self.assertEqual(sent[0][1]["new"], ["a"])

    def test_un_changement_de_prix_est_un_evenement(self):
        # B4 : un prix qui change est persisté ET annoncé.
        store = FakeStore([ActiveModel("openai", "a", "openai")])
        store._known = [{"provider": "openai", "model_id": "a", "source": "openai",
                         "price_input": 1.0, "price_cached": 0.5, "price_output": 2.0}]
        priced = SourceResult(
            source="openai", complete=True,
            models=(ModelSeen(provider="openai", model_id="a", source="openai",
                              price_input=3.0, price_cached=0.5, price_output=2.0),),
        )
        sent: list[tuple[str, dict]] = []
        record(store, priced, emit=lambda body, payload: sent.append((body, payload)))
        self.assertEqual(len(sent), 1)
        self.assertIn("prix modifié", sent[0][0])
        self.assertEqual(sent[0][1]["repriced"], ["a"])

    def test_un_prix_en_cache_qui_change_est_un_evenement(self):
        # Le triplet COMPLET : un prix en cache qui bouge compte (sonde croisée).
        store = FakeStore([ActiveModel("openai", "a", "prices")])
        store._known = [{"provider": "openai", "model_id": "a", "source": "prices",
                         "price_input": 2.0, "price_cached": 1.0, "price_output": 5.0}]
        local = SourceResult(source="prices", complete=True, kind="local",
                             models=(ModelSeen(provider="openai", model_id="a", source="prices",
                                               price_input=2.0, price_cached=4.0, price_output=5.0),))
        sent: list[tuple[str, dict]] = []
        record(store, local, emit=lambda body, payload: sent.append((body, payload)))
        self.assertEqual(sent[0][1]["repriced"], ["a"])

    def test_un_bareme_qui_change_le_prix_d_une_ligne_du_fournisseur_dit_repriced(self):
        # Cas croisé : la ligne appartient à openai, `prices` en change le prix. Ce
        # n'est PAS une nouveauté, et la source du fournisseur reste la sienne.
        store = FakeStore([ActiveModel("openai", "a", "openai")])
        store._known = [{"provider": "openai", "model_id": "a", "source": "openai",
                         "price_input": 2.0, "price_cached": 1.0, "price_output": 5.0}]
        local = SourceResult(source="prices", complete=True, kind="local",
                             models=(ModelSeen(provider="openai", model_id="a", source="prices",
                                               price_input=3.0, price_cached=1.0, price_output=5.0),))
        sent: list[tuple[str, dict]] = []
        record(store, local, emit=lambda body, payload: sent.append((body, payload)))
        self.assertEqual([payload.get("repriced") for _, payload in sent], [["a"]], str(sent))
        self.assertIsNone(sent[0][1].get("new"), "un prix change n'est pas une nouveauté")

    def test_record_ne_retire_rien_sur_une_source_incomplete(self):
        store = FakeStore([ActiveModel("openai", m, "openai") for m in "abcd"])
        sent: list[tuple[str, dict]] = []
        decision = record(store, result("a", complete=False), emit=lambda body, payload: sent.append((body, payload)))
        self.assertEqual(store.retired, [])
        self.assertEqual(sent, [], "une source incomplète ne dit pas de retrait")
        self.assertEqual(decision.refused_reason, "incomplete")
