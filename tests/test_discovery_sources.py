# SPDX-License-Identifier: AGPL-3.0-only
"""Les sources de découverte de L14, sur réponses ENREGISTRÉES (aucun réseau).

Ce que ces tests protègent, dans l'ordre d'importance :

1. une réponse **partielle** (pagination non suivie) ou **en erreur** ne donne
   jamais une liste complète — c'est ce qui empêche un retrait par accident ;
2. une source qui ne liste pas de modèles (le barème, les harnais) n'en retire
   jamais : elle n'a pas vu de liste, elle n'a rien à conclure ;
3. l'analyseur lit ce que les fournisseurs publient vraiment (`data`, un `id`,
   une fenêtre de contexte quand elle est là), sans exiger de champ absent.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ameesh.discovery import anthropic, deepseek, harness, openai, prices  # noqa: E402

ANTHROPIC_RESPONSE = {
    "data": [
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "created_at": "2026-09-01T00:00:00Z"},
        {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "created_at": "2026-08-01T00:00:00Z"},
    ],
    "has_more": False,
}
ANTHROPIC_PARTIAL = {"data": [{"id": "claude-opus-5-5"}], "has_more": True, "last_id": "claude-opus-5-5"}
OPENAI_RESPONSE = {"object": "list", "data": [{"id": "gpt-6.1-sol", "created": 1, "owned_by": "openai"}]}
DEEPSEEK_RESPONSE = {
    "object": "list",
    "data": [
        {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-pro", "object": "model", "owned_by": "deepseek"},
    ],
}


class ProviderListingTest(unittest.TestCase):
    def test_anthropic_reads_the_models_it_publishes(self):
        result = anthropic.parse_listing(ANTHROPIC_RESPONSE)
        self.assertTrue(result.complete)
        self.assertEqual(
            [m.model_id for m in result.models], ["claude-opus-5-5", "claude-sonnet-5"]
        )
        self.assertEqual({m.provider for m in result.models}, {"anthropic"})
        self.assertTrue(result.raw_digest)

    def test_a_first_page_is_not_a_complete_list(self):
        result = anthropic.parse_listing(ANTHROPIC_PARTIAL)
        self.assertFalse(result.complete, "pagination non suivie = liste partielle")
        self.assertIn("partielle", result.detail)
        # Les modèles vus sont quand même rapportés : ils seront rafraîchis, pas retirés.
        self.assertEqual([m.model_id for m in result.models], ["claude-opus-5-5"])

    def test_an_error_is_not_an_empty_list(self):
        for module in (anthropic, openai, deepseek):
            result = module.parse_listing(None, status_ok=False)
            self.assertFalse(result.complete, module.PROVIDER)
            self.assertEqual(result.models, (), "une erreur ne rapporte aucun modèle")
            self.assertNotIn("vide", result.detail)

    def test_a_broken_body_reports_nothing_rather_than_a_list(self):
        result = openai.parse_listing("pas un objet", status_ok=True)
        self.assertTrue(result.complete, "un corps illisible n'est pas une erreur de source")
        self.assertEqual(result.models, ())

    def test_openai_and_deepseek_read_their_lists(self):
        self.assertEqual([m.model_id for m in openai.parse_listing(OPENAI_RESPONSE).models], ["gpt-6.1-sol"])
        self.assertEqual(
            [m.model_id for m in deepseek.parse_listing(DEEPSEEK_RESPONSE).models],
            ["deepseek-flash", "deepseek-pro"],
        )

    def test_no_key_means_no_claim_and_no_withdrawal(self):
        # Sans clé, la source ne peut rien dire : elle ne prétend pas avoir vu une liste vide.
        saved = {name: os.environ.pop(name, None) for name in
                 ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")}
        try:
            for module in (anthropic, openai, deepseek):
                result = module.listing()
                self.assertFalse(result.complete, module.PROVIDER)
                self.assertEqual(result.models, ())
                self.assertIn("clé", result.detail)
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value


class LocalSourcesTest(unittest.TestCase):
    def test_prices_never_retire_anything_and_name_a_provider_when_the_prefix_says_it(self):
        result = prices.listing()
        self.assertTrue(result.complete)
        self.assertTrue(result.models)
        self.assertEqual(prices.provider_for("claude-opus-5-5"), "anthropic")
        self.assertEqual(prices.provider_for("gpt-6.1-sol"), "openai")
        self.assertEqual(prices.provider_for("deepseek-flash"), "deepseek")
        self.assertEqual(prices.provider_for("un-modele-inconnu"), "local")
        for model in result.models:
            self.assertIsNotNone(model.price_input)
            self.assertIsNotNone(model.price_output)

    def test_the_harness_source_lists_no_model_at_all(self):
        result = harness.listing()
        self.assertTrue(result.complete)
        self.assertEqual(result.models, (), "les harnais ne publient pas de liste de modèles")
        self.assertIn("rien à retirer", result.detail)
