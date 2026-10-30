# SPDX-License-Identifier: AGPL-3.0-only
"""L102 T4: vocabulary presentation leaves the API wire contract stable."""
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import serve, serve_actions, vocab


class ApiVocabularyTest(unittest.TestCase):
    def document(self, words):
        with mock.patch.object(vocab, "active", return_value=words):
            return serve.openapi()

    def test_core_and_dev_render_terms_and_grammar(self):
        from ameesh.modules.dev import VOCABULARY as dev
        for words in (vocab.CORE, dev, vocab.Vocabulary("idée", "idées")):
            with self.subTest(words=words.singular):
                doc = self.document(words)
                metadata = doc["x-ameesh-vocabulary"]
                self.assertEqual(metadata["singular"], words.singular)
                self.assertEqual(metadata["gender"], words.gender)
                self.assertEqual(metadata["of"], words.of)
                self.assertEqual(metadata["to"], words.to)
                self.assertEqual(metadata["elides"], words.elides)
                paths = doc["paths"]
                self.assertIn(words.title_plural, paths["/api/v1/lots"]["get"]["summary"])
                self.assertIn(words.of, paths["/api/v1/lots/{id}"]["get"]["summary"])
                self.assertIn(words.of, paths["/api/v1/threads/{project}/{lot}"]["post"]["summary"])
                self.assertIn(words.definite, paths["/api/v1/lots/{id}/assign"]["post"]["summary"])
                self.assertIn(words.of_plural, paths["/api/v1/lots"]["get"]["parameters"][0]["description"])

    def test_discovery_exposes_same_optional_metadata(self):
        ctx = SimpleNamespace(organisation="example", cfg=SimpleNamespace())
        with mock.patch.object(vocab, "active", return_value=vocab.CORE):
            discovery = serve.discovery(ctx)
            doc = serve.openapi()
        self.assertEqual(discovery["vocabulary"], doc["x-ameesh-vocabulary"])
        schema = doc["components"]["schemas"]["ameesh-discovery-1"]
        self.assertIn("vocabulary", schema["properties"])
        self.assertNotIn("vocabulary", schema["required"])
        self.assertEqual(discovery["schema"], "ameesh-discovery/1")

    def test_identifiers_payloads_and_authorization_are_independent_of_vocabulary(self):
        from ameesh.modules.dev import VOCABULARY as dev
        core, dev_doc = (self.document(words) for words in (vocab.CORE, dev))
        self.assertEqual(core["components"], dev_doc["components"])
        self.assertEqual(core["security"], dev_doc["security"])
        self.assertEqual(core["paths"].keys(), dev_doc["paths"].keys())
        for path, methods in core["paths"].items():
            for method, operation in methods.items():
                other = dev_doc["paths"][path][method]
                for key in ("operationId", "security", "requestBody"):
                    self.assertEqual(operation.get(key), other.get(key))
                self.assertEqual(operation["responses"].keys(), other["responses"].keys())
                for code, response in operation["responses"].items():
                    self.assertEqual(response.get("content"), other["responses"][code].get("content"))
        self.assertIn("lot", core["components"]["schemas"]["ameesh-lot-1"]["properties"])
        self.assertEqual(serve.match_action("/api/v1/lots/123/assign")[1], {"id": "123"})

    def test_action_errors_use_vocabulary_and_keep_code(self):
        for words in (vocab.CORE, vocab.Vocabulary("lot", "lots", "m")):
            with mock.patch.object(vocab, "active", return_value=words):
                with self.assertRaises(serve_actions.ActionError) as caught:
                    serve_actions._lot_label(None, True)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, "bad_body"))
            self.assertIn(words.of, str(caught.exception))
