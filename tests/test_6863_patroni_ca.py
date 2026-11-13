# SPDX-License-Identifier: AGPL-3.0-only
"""EP8.6 (#6863) : `patroni_ca` fait vérifier l'API REST de la grappe.

L'API Patroni écoute en HTTPS avec un certificat signé par l'AC privée de la
grappe : sans `patroni_ca`, la lecture échoue sur la vérification (et le dit
dans `errors`) ; avec, elle aboutit, sans toucher au magasin du système.
"""
from __future__ import annotations

import http.server
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path

from ameesh import config as config_mod
from ameesh import db_status

REPONSES = {
    "/patroni": {"patroni": {"name": "ameesh-agents-1", "scope": "ameesh", "version": "4.1.5"},
                 "role": "primary", "state": "running", "timeline": 4},
    "/cluster": {"members": [{"name": "ameesh-agents-1", "role": "leader", "state": "running"}]},
}


class _Gestionnaire(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (API de http.server)
        corps = json.dumps(REPONSES.get(self.path, {})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *args):
        pass


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


@unittest.skipUnless(shutil.which("openssl"), "openssl absent")
class PatroniCaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=ca-grappe-test", "-keyout", "ca.key", "-out", "ca.crt", cwd=d)
        _openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1",
                 "-keyout", "rest.key", "-out", "rest.csr", cwd=d)
        (d / "san.cnf").write_text("subjectAltName=IP:127.0.0.1\n", encoding="utf-8")
        _openssl("x509", "-req", "-in", "rest.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                 "-CAcreateserial", "-days", "1", "-extfile", "san.cnf", "-out", "rest.crt", cwd=d)
        cls.ca = str(d / "ca.crt")
        contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        contexte.load_cert_chain(d / "rest.crt", d / "rest.key")
        cls.serveur = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gestionnaire)
        cls.serveur.socket = contexte.wrap_socket(cls.serveur.socket, server_side=True)
        cls.fil = threading.Thread(target=cls.serveur.serve_forever, daemon=True)
        cls.fil.start()
        cls.url = "https://127.0.0.1:%d" % cls.serveur.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.serveur.shutdown()
        cls.serveur.server_close()
        cls._tmp.cleanup()

    def test_sans_ca_la_verification_echoue_et_le_dit(self):
        lu = db_status.patroni(types.SimpleNamespace(patroni_url=self.url, patroni_ca=""))
        self.assertIsNone(lu["local"])
        self.assertIn("CERTIFICATE_VERIFY_FAILED", lu["errors"]["local"] or "")

    def test_avec_ca_la_lecture_aboutit(self):
        lu = db_status.patroni(types.SimpleNamespace(patroni_url=self.url, patroni_ca=self.ca))
        self.assertEqual(lu["errors"], {"local": None, "cluster": None})
        self.assertEqual(lu["name"], "ameesh-agents-1")
        self.assertEqual(lu["local"]["role"], "primary")

    def test_un_lecteur_fourni_n_est_pas_remplace(self):
        appels = []

        def lecteur(url, chemin):
            appels.append(chemin)
            return REPONSES[chemin]

        db_status.patroni(types.SimpleNamespace(patroni_url=self.url, patroni_ca=self.ca),
                          reader=lecteur)
        self.assertEqual(appels, ["/patroni", "/cluster"])

    def test_variable_d_environnement(self):
        with tempfile.TemporaryDirectory() as d:
            vide = Path(d, "config.json")
            vide.write_text("{}", encoding="utf-8")
            cfg = config_mod.load(env={"AMEESH_CONFIG": str(vide),
                                       "AMEESH_PATRONI_CA": " /etc/ameesh/tls/ca.crt "})
        self.assertEqual(cfg.patroni_ca, "/etc/ameesh/tls/ca.crt")
        self.assertEqual(config_mod.Config().patroni_ca, "")


if __name__ == "__main__":
    unittest.main()
