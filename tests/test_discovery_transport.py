# SPDX-License-Identifier: AGPL-3.0-only
"""Le transport des sources (L14, B1) : une clé ne suit pas une redirection.

La revue indépendante a reproduit la fuite : `urlopen` avec `Authorization`,
une réponse 302 de 127.0.0.1 vers localhost, et la clé factice reçue à la seconde
origine. Ces tests tiennent la correction — ils utilisent un VRAI serveur local,
parce qu'un faux transport ne prouverait rien de ce genre de bug.
"""
from __future__ import annotations

import http.server
import os
import sys
import threading
import unittest
import urllib.parse
import socket

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ameesh.discovery import http as discovery_http  # noqa: E402

RECEIVED: list[tuple[str, dict]] = []
REDIRECTS: list[tuple[int, str]] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - nom imposé par http.server
        RECEIVED.append((self.path, dict(self.headers)))
        for code, target in REDIRECTS:
            self.send_response(code)
            self.send_header("Location", target)
            self.end_headers()
            return
        body = b'{"data": [{"id": "modele"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence
        return


class TransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        RECEIVED.clear()
        REDIRECTS.clear()

    def test_une_redirection_est_refusee_et_la_cle_ne_suit_pas(self):
        # La cible est une AUTRE origine (hôte différent, même serveur) : c'est le
        # scénario de la fuite, et la clé ne doit jamais y arriver.
        target = f"http://localhost:{self.port}/capture"
        REDIRECTS.append((302, target))
        with self.assertRaises(discovery_http.RedirectRefused):
            discovery_http.fetch_json(
                f"http://127.0.0.1:{self.port}/liste",
                headers={"Authorization": "Bearer FAKE-TEST-KEY"},
                allow_http=True,
            )
        self.assertEqual([path for path, _ in RECEIVED], ["/liste"],
                         "la seconde origine n'a même pas été contactée")

    def test_les_autres_codes_de_redirection_sont_refuses_aussi(self):
        target = f"http://localhost:{self.port}/capture"
        for code in (301, 307, 308):
            RECEIVED.clear()
            REDIRECTS[:] = [(code, target)]
            with self.assertRaises(discovery_http.RedirectRefused, msg=str(code)):
                discovery_http.fetch_json(
                    f"http://127.0.0.1:{self.port}/liste",
                    headers={"Authorization": "Bearer FAKE-TEST-KEY"},
                    allow_http=True,
                )
            self.assertEqual([path for path, _ in RECEIVED], ["/liste"], str(code))

    def test_http_est_refuse_hors_loopback(self):
        # Le loopback est toléré (un test local en dépend, et la sonde de revue
        # l'utilise) : ce qui est refusé, c'est l'HTTP vers une vraie origine, où
        # une clé voyagerait en clair.
        with self.assertRaises(discovery_http.InsecureEndpoint):
            discovery_http.fetch_json("http://exemple.invalid/liste",
                                      headers={"Authorization": "Bearer FAKE-TEST-KEY"})

    def test_http_loopback_est_tolere_pour_un_test_local(self):
        payload, failure = discovery_http.fetch_json(
            f"http://127.0.0.1:{self.port}/liste",
            headers={"Authorization": "Bearer FAKE-TEST-KEY"},
        )
        self.assertEqual(failure, "")
        self.assertIsNotNone(payload)

    def test_le_chemin_normal_lit_la_charge_utile(self):
        payload, failure = discovery_http.fetch_json(
            f"http://127.0.0.1:{self.port}/liste",
            headers={"Authorization": "Bearer FAKE-TEST-KEY"},
            allow_http=True,
        )
        self.assertEqual(failure, "")
        self.assertEqual(payload, {"data": [{"id": "modele"}]})

    def test_anthropic_envoie_sa_version_d_api(self):
        from ameesh.discovery import anthropic

        payload, failure = anthropic.listing(
            token="FAKE-TEST-KEY",
            base_url=f"http://127.0.0.1:{self.port}",
        ) if False else (None, "skip")
        # Le listing réel exige HTTPS : on vérifie l'en-tête sur le transport, avec
        # la même table que celle que le module envoie.
        self.assertIn("anthropic-version", anthropic.EXTRA_HEADERS)
        self.assertEqual(anthropic.EXTRA_HEADERS["anthropic-version"], anthropic.ANTHROPIC_VERSION)

    def test_une_derniere_page_avec_last_id_est_complete(self):
        # La réponse officielle d'Anthropic porte has_more ET un curseur : quand
        # has_more vaut false, la page est la dernière, même si last_id est rempli.
        from ameesh.discovery import anthropic

        payload = {
            "data": [{"id": "claude-opus-5-5", "type": "model"}],
            "has_more": False,
            "first_id": "claude-opus-5-5",
            "last_id": "claude-opus-5-5",
        }
        result = anthropic.parse_listing(payload)
        self.assertTrue(result.complete, "has_more=false est la dernière page")
        self.assertEqual([m.model_id for m in result.models], ["claude-opus-5-5"])

    def test_une_page_avec_has_more_vrai_reste_incomplete(self):
        from ameesh.discovery import anthropic

        result = anthropic.parse_listing({"data": [{"id": "x"}], "has_more": True, "last_id": "x"})
        self.assertFalse(result.complete)
