# SPDX-License-Identifier: AGPL-3.0-only
"""L232d (#366) : le chemin complet sur le service réel, sans doublure.

Un CLI factice (codex épinglé) imprime une URL et un code à usage unique ; le
superviseur les remet au service `ameesh-approve` **réel** (HTTP sur la boucle
locale, base migrée, jeton de service), l'approbateur ouvre le lien, signe une
assertion WebAuthn logicielle, lit l'URL et le code dans la vue privée, puis
signe `done`. Côté ameesh : le reçu est récupéré par le client réel, vérifié
et le nonce consommé, le groupe de processus est tué, la santé du compte est
sondée et écrite dans `credential_health`.

Seules doublures : le CLI du harnais (aucun fournisseur n'est contacté) et
l'assertion WebAuthn (authentificateur logiciel de `tests/webauthn_soft.py`).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from ameesh import accounts as accounts_mod
from ameesh import approve_client
from ameesh import login
from ameesh import login_cli
from ameesh import receipts
from ameesh import storage

from .test_approve import AGENT, ALICE, ApproveBase

URL = "https://chatgpt.com/device"
CODE = "WXYZ-9876"


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class LoginEndToEndTest(ApproveBase):
    HOST = "ameesh-agents-1"
    #: l'API des défis est ouverte sur ce banc, avec son jeton DÉDIÉ (B1 dev4)
    LOGIN_API = True

    def setUp(self) -> None:
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="l232d-e2e-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.home = os.path.join(self.dir, "codex-home")
        os.makedirs(self.home, mode=0o700)
        self.spawned: list = []
        self.addCleanup(self.reap)
        self.cfg = SimpleNamespace(
            host=self.HOST,
            accounts={"codex": [{"name": "default", "path": self.home}]},
            approve_url="http://127.0.0.1:%d" % self.port,
            approve_token_file=self.token_file,
            login_token_file=self.login_token_file,
            approve_tls_name="",
        )
        self.environ = {"PATH": self.dir + ":" + os.environ.get("PATH", ""),
                        "HOME": self.dir}
        self.cli_path = os.path.join(self.dir, "codex")
        with open(self.cli_path, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                "mkdir -p \"$CODEX_HOME\"\n"
                "printf '{\"tokens\": {\"refresh_token\": \"rt\"}}' > \"$CODEX_HOME/auth.json\"\n"
                "printf 'Open this URL: %s\\nEnter this code: %s\\n'\n"
                "sleep 30\n" % (URL, CODE))
        os.chmod(self.cli_path, stat.S_IRWXU)

    def reap(self) -> None:
        for proc in self.spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def supervisor(self):
        def spawn(argv, **kwargs):
            proc = subprocess.Popen(argv, **kwargs)
            self.spawned.append(proc)
            return proc

        profile = accounts_mod.parse(self.cfg.accounts)["codex"][0]
        return login.Supervisor(
            spawn=spawn,
            publish=login.publisher(approve_client, self.cfg, approver=ALICE,
                                    requested_by=AGENT),
            health=lambda request: login_cli.health_report(
                self.server_db, self.cfg, profile, request, environ=self.environ))

    def test_defi_reel_jusqu_a_la_sante(self):
        policy = receipts.Policy(rp_id=self.acfg.rp_id, origins=tuple(self.acfg.origins))
        supervisor = self.supervisor()
        request = supervisor.begin(host=self.HOST, harness="codex", account="default",
                                   home=self.home, version="0.159.3", approver=ALICE,
                                   requested_by=AGENT, environ=self.environ)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING),
                        "state=%s category=%s" % (request.state, request.category))
        self.assertRegex(request.login_id, r"^lgn_[0-9a-z]{26}$")
        self.assertNotIn(URL, str(request.describe()) + repr(request))
        self.assertNotIn(CODE, str(request.describe()) + repr(request))

        # -- l'approbateur : lien muet, assertion, vue privée, done ---------
        path = urlsplit(request.link).path
        status, _headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertNotIn(URL, text)
        self.assertNotIn(CODE, text)
        page = self.page_data(text, "ameesh-login")
        status, opened = self.submit(path, self.sign(self.alice, page, decision="view"))
        self.assertEqual(status, 200, opened)
        self.assertEqual(opened["url"], URL)
        self.assertEqual(opened["code"], CODE)
        status, signed = self.submit(path, self.sign(
            self.alice, {"challenge-done": opened["challenge_done"]}, decision="done"))
        self.assertEqual(status, 200, signed)

        # -- ameesh : reçu réel, vérification, consommation du nonce --------
        fetched: dict = {}

        def fetch(login_id):
            status, payload = approve_client.fetch_login_receipt(self.cfg, login_id)
            if isinstance(payload, (bytes, bytearray)):
                payload = json.loads(payload)
            fetched["document"] = payload
            return status, payload

        def verify(document):
            return login.receipt_decision(self.server_db, request, document,
                                          policy=policy, consume_by=AGENT)

        result = login.wait_for_decision(supervisor, request, fetch=fetch, verify=verify,
                                         timeout=5.0, interval=0.05)
        self.assertEqual(result.state, login.DONE)
        self.assertEqual(result.category, "healthy")     # la sonde a parlé
        self.assertTrue(wait_for(lambda: self.spawned and self.spawned[0].poll() is not None))
        self.assertEqual(supervisor.registry.live(), [])
        # le code privé est effacé dès que le reçu existe
        self.assertIsNone(self.service.store.login_private(request.login_id))
        self.assertNotIn(URL, json.dumps(fetched["document"]))
        self.assertNotIn(CODE, json.dumps(fetched["document"]))

        # -- le nonce est à usage unique ------------------------------------
        with self.assertRaises(login.LoginError) as caught:
            verify(fetched["document"])
        self.assertEqual(caught.exception.category, receipts.REPLAY)

        # -- la santé est écrite pour la prochaine sonde/porte --------------
        row = (storage.of(self.server_db).credentials.health(self.HOST, "codex")
               or {}).get("default") or {}
        self.assertEqual(row.get("state"), "healthy")
        # même source que la passe à la demande : la contrainte n'admet pas « login »
        self.assertEqual(row.get("source"), "sentinel")
        self.assertIn("refresh token present", row.get("reason") or "")


if __name__ == "__main__":
    unittest.main()
