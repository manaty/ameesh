# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-approve (spec §9, C8) : le service réel, de bout en bout.

Le serveur tourne dans un thread sur 127.0.0.1, port éphémère ; le client est
`http.client` ; la passkey est l'authentificateur LOGICIEL de
tests/webauthn_soft.py ; les actions viennent d'une source en mémoire (la
table `actions` du lot L5 est testée à part). Aucune autre interface que la
boucle locale, aucun accès réseau externe.
"""
from __future__ import annotations

import hashlib
import html
import http.client
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ameesh import actions, approve_client, fil, jcs, receipts
from ameesh.connectors.shell_noop import ShellNoopConnector
from ameesh.approve import config as approve_config
from ameesh.approve import duplicate
from ameesh.approve import render
from ameesh.approve import server as approve_server
from ameesh.approve.service import ApproveError, ApproveService
from ameesh.approve.sources import (
    REQUESTABLE_STATES, ActionSourceError, DbActionSource, MemoryActionSource, normalize_action,
)
from ameesh.approve.store import Store

from .support import PgTestCase, apply_authenticators, child_env
from .webauthn_soft import ORIGIN, RP_ID, SoftWebAuthn, b64u, cbor, new_action_id

ALICE = "human:alice"
BOB = "human:bob"
DAVE = "human:dave"
AGENT = "agent:deepseek7"
COMMIT = "c" * 40
POLICY = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,))
EXPECTED_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def member(title: str, *entries: dict) -> dict:
    return {"title": title, "canon_ref": "members/%s.md@%s" % (title, COMMIT),
            "authenticators": list(entries)}


def make_action(**overrides) -> dict:
    action = {
        "action_id": new_action_id(), "project": "ameesh", "work_item": "w-1",
        "proposed_by": AGENT, "connector": "git-merge", "operation": "merge",
        "target": "exemple/depot#42", "args": {"pr": 42, "method": "merge"},
        "amount": None, "currency": None, "policy_version": 3,
        "class": "irreversible", "state": "proposed",
    }
    action.update(overrides)
    return action


def digest_of(action: dict) -> str:
    return receipts.action_digest({k: action[k] for k in receipts.ACTION_DIGEST_FIELDS})


def attestation(authenticator: SoftWebAuthn, challenge: bytes, *, origin: str = ORIGIN,
                rp_id: str = RP_ID, uv: bool = True, kind: str = "webauthn.create",
                raw_id: bytes | None = None, fmt: str = "none") -> dict:
    """Réponse de navigator.credentials.create, fabriquée en logiciel."""
    credential = receipts.b64u_decode(authenticator.credential_id)
    flags = (receipts.FLAG_UP | receipts.FLAG_AT | (receipts.FLAG_UV if uv else 0)
             | (receipts.FLAG_BE if authenticator.backup_eligible else 0)
             | (receipts.FLAG_BS if authenticator.backup_state else 0))
    auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + (0).to_bytes(4, "big")
            + bytes(16) + len(credential).to_bytes(2, "big") + credential
            + authenticator.key.cose())
    client = json.dumps({"type": kind, "challenge": b64u(challenge), "origin": origin},
                        separators=(",", ":")).encode()
    return {"credential_id": b64u(raw_id if raw_id is not None else credential),
            "clientDataJSON": b64u(client),
            "attestationObject": b64u(cbor({"fmt": fmt, "attStmt": {}, "authData": auth}))}


class Clock:
    """Horloge du service, avançable par les tests."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def write_token_file(directory: str, mode: int = 0o600, token: str | None = None) -> tuple:
    token = token or secrets.token_urlsafe(32)
    path = os.path.join(directory, "service-token")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    os.chmod(path, mode)
    return path, token


# --------------------------------------------------------------------------
# socle : un service réel par test
# --------------------------------------------------------------------------

class ApproveBase(PgTestCase):
    CONFIG: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "TRUNCATE authenticators, standing_approvals, standing_reservations, "
            "mesh_consumed_nonces RESTART IDENTITY CASCADE")
        self.alice = SoftWebAuthn("ES256")
        self.bob = SoftWebAuthn("ES256")
        result = apply_authenticators(
            self.db, [member("alice", self.alice.entry()), member("bob", self.bob.entry())])
        self.assertEqual(result["errors"], [])
        self.workdir = tempfile.mkdtemp(prefix="ameesh-approve-test-")
        self.token_file, self.service_token = write_token_file(self.workdir)
        self.acfg = approve_config.ApproveConfig(
            rp_id=RP_ID, origins=(ORIGIN,), port=0, token_file=self.token_file,
            state_dir=os.path.join(self.workdir, "state"), **self.CONFIG).validate()
        self.source = MemoryActionSource()
        self.clock = Clock()
        self.server_db = self.connect()
        self.service = ApproveService(
            self.acfg, self.server_db, self.source,
            service_token=approve_config.read_service_token(self.token_file), clock=self.clock)
        self.server = approve_server.make_server(self.service)
        self.port = self.server.server_address[1]
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.local = "127.0.0.1:%d" % self.port
        self.thread = approve_server.serve_in_thread(self.server)
        # journal du service capturé (et pas répandu dans la sortie des tests)
        self.log = io.StringIO()
        logger = logging.getLogger("ameesh.approve")
        handler = logging.StreamHandler(self.log)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        previous = (logger.level, logger.propagate)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        def restore():
            logger.removeHandler(handler)
            logger.setLevel(previous[0])
            logger.propagate = previous[1]
        self.addCleanup(restore)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.server_db.close()
        shutil.rmtree(self.workdir, ignore_errors=True)
        super().tearDown()

    # -- client HTTP ---------------------------------------------------------
    def http(self, method: str, path: str, body=None, *, host: str | None = RP_ID,
             origin: str | None = None, auth: str | None = None, headers: dict | None = None,
             raw: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            sent = {}
            if host is not None:
                sent["Host"] = host
            data = raw
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                sent["Content-Type"] = "application/json"
            if origin is not None:
                sent["Origin"] = origin
            if auth is not None:
                sent["Authorization"] = "Bearer " + auth
            sent.update(headers or {})
            if host is None:
                conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                for name, value in sent.items():
                    conn.putheader(name, value)
                if data is not None:
                    conn.putheader("Content-Length", str(len(data)))
                conn.endheaders(data)
            else:
                conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            payload = response.read()
            return (response.status, {k.lower(): v for k, v in response.getheaders()}, payload)
        finally:
            conn.close()

    def api(self, method: str, path: str, body=None, **kwargs):
        kwargs.setdefault("host", self.local)
        kwargs.setdefault("auth", self.service_token)
        return self.http(method, path, body, **kwargs)

    def create(self, action: dict | None = None, *, approver: str = ALICE,
               requested_by: str = AGENT, extra: dict | None = None):
        action = action or make_action()
        self.source.put(action)
        body = {"action_id": action["action_id"], "approver": approver,
                "requested_by": requested_by}
        body.update(extra or {})
        status, _headers, payload = self.api("POST", "/requests", body)
        self.assertEqual(status, 201, payload)
        data = json.loads(payload)
        link = urlsplit(data["link"])
        self.assertEqual("%s://%s" % (link.scheme, link.netloc), ORIGIN)
        return action, data, link.path

    def page(self, path: str, **kwargs):
        status, headers, payload = self.http("GET", path, **kwargs)
        return status, headers, payload.decode("utf-8")

    @staticmethod
    def page_data(text: str, element: str = "ameesh-approval") -> dict:
        match = re.search(r'<main id="%s" ([^>]*)>' % element, text)
        assert match, "élément %s absent" % element
        return {name: html.unescape(value)
                for name, value in re.findall(r'data-([a-z-]+)="([^"]*)"', match.group(1))}

    @staticmethod
    def summary_of(text: str) -> str:
        match = re.search(r'<pre class="summary">(.*?)</pre>', text, re.S)
        return html.unescape(match.group(1))

    @staticmethod
    def sign(authenticator: SoftWebAuthn, data: dict, decision: str = "approve",
             challenge_of: str | None = None, **proof) -> dict:
        challenge = receipts.b64u_decode(data["challenge-" + (challenge_of or decision)])
        return dict(authenticator.proof(challenge, **proof), decision=decision,
                    credential_id=authenticator.credential_id)

    def submit(self, path: str, body: dict, *, origin: str | None = ORIGIN, **kwargs):
        status, _headers, payload = self.http("POST", path, body, origin=origin, **kwargs)
        return status, json.loads(payload or b"{}")

    def fetch_receipt(self, request_id: str):
        status, _headers, payload = self.api("GET", "/receipts/" + request_id)
        return status, json.loads(payload)

    def assertSecurityHeaders(self, headers: dict) -> None:
        self.assertEqual(headers.get("content-security-policy"), EXPECTED_CSP)
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("x-frame-options"), "DENY")


# --------------------------------------------------------------------------
# cycle complet
# --------------------------------------------------------------------------

class ApproveFlowTest(ApproveBase):
    def test_cycle_complet_approuver(self):
        action, data, path = self.create()
        self.assertEqual(data["digest"], digest_of(action))
        self.assertRegex(data["request_id"], r"^req_[0-9a-z]{26}$")

        status, pending = self.fetch_receipt(data["request_id"])
        self.assertEqual((status, pending["status"]), (202, "pending"))

        status, headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertSecurityHeaders(headers)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertEqual(headers["server"], "ameesh-approve")
        self.assertIn("SPDX-License-Identifier: AGPL-3.0-only", text)
        # aucun script ni style en ligne : un seul <script>, externe
        self.assertEqual(re.findall(r"<script[^>]*>", text),
                         ['<script src="../static/approve.js" defer>'])
        self.assertNotIn("style=", text)
        self.assertIn("exemple/depot#42", text)
        self.assertIn(render.short_digest(data["digest"]), text)
        page = self.page_data(text)
        self.assertEqual(page["rp-id"], RP_ID)
        self.assertEqual(page["credentials"], self.alice.credential_id)

        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {"request_id": data["request_id"], "decision": "approve"})

        status, receipt = self.fetch_receipt(data["request_id"])
        self.assertEqual(status, 200)
        self.assertEqual(receipt["v"], "ameesh-receipt/1")
        self.assertEqual(receipt["facade"], "webauthn")
        request = receipt["request"]
        self.assertEqual((request["approver"], request["action_id"], request["requested_by"]),
                         (ALICE, action["action_id"], AGENT))
        self.assertLessEqual(request["exp"] - request["iat"], 900)
        self.assertEqual(len(receipts.b64u_decode(request["nonce"])), 16)
        self.assertEqual(b64u(receipts.challenge(request)), page["challenge-approve"])
        # le résumé affiché est celui que signe summary_digest, et c'est celui
        # du service, recalculé depuis l'action
        summary = self.summary_of(text)
        self.assertEqual(render.summary_digest(summary), request["summary_digest"])
        self.assertEqual(summary, render.render_summary(normalize_action(action), ALICE, AGENT))
        self.assertEqual(request["summary_digest"], data["summary_digest"])

        # ameesh vérifie lui-même (receipts.py), puis consomme à l'exécution
        verdict = receipts.verify_receipt(self.db, receipt, POLICY,
                                          expected_digest=digest_of(action),
                                          expected_action_id=action["action_id"])
        self.assertTrue(verdict.authority, verdict.reason)
        self.assertFalse(verdict.consumed)
        consumed = receipts.verify_receipt(self.db, receipt, POLICY, consume_by="porte",
                                           expected_digest=digest_of(action),
                                           expected_action_id=action["action_id"])
        self.assertTrue(consumed.ok and consumed.consumed, consumed.reason)
        again = receipts.verify_receipt(self.db, receipt, POLICY, consume_by="porte",
                                        expected_digest=digest_of(action),
                                        expected_action_id=action["action_id"])
        self.assertEqual(again.code, receipts.REPLAY)

    def test_cycle_complet_refuser(self):
        action, data, path = self.create()
        _status, _headers, text = self.page(path)
        status, result = self.submit(path, self.sign(self.alice, self.page_data(text), "deny"))
        self.assertEqual((status, result["decision"]), (200, "deny"), result)
        status, receipt = self.fetch_receipt(data["request_id"])
        self.assertEqual(status, 200)
        self.assertEqual(receipt["request"]["decision"], "deny")
        verdict = receipts.verify_receipt(self.db, receipt, POLICY, expect_decision="deny",
                                          expected_digest=digest_of(action),
                                          expected_action_id=action["action_id"])
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertFalse(verdict.authority)
        # un refus n'autorise rien : vérifié comme approbation, il est refusé
        self.assertEqual(receipts.verify_receipt(self.db, receipt, POLICY).code, receipts.DECISION)
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410)

    def test_jeton_reutilise_refuse(self):
        _action, data, path = self.create()
        _status, _headers, text = self.page(path)
        page = self.page_data(text)
        self.assertEqual(self.submit(path, self.sign(self.alice, page))[0], 200)
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (410, "used"))
        status, result = self.submit(path, self.sign(self.alice, page, "deny"))
        self.assertEqual(status, 410)
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410)
        self.assertIn("déjà servi", text)
        status, receipt = self.fetch_receipt(data["request_id"])
        self.assertEqual(receipt["request"]["decision"], "approve")

    def test_deux_soumissions_simultanees_un_seul_recu(self):
        _action, data, path = self.create()
        _status, _headers, text = self.page(path)
        page = self.page_data(text)
        bodies = [self.sign(self.alice, page), self.sign(self.alice, page, "deny")]
        barrier = threading.Barrier(2)
        statuses = []

        def post(body):
            barrier.wait()
            statuses.append(self.submit(path, body)[0])
        threads = [threading.Thread(target=post, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(statuses), [200, 410])
        self.assertEqual(self.fetch_receipt(data["request_id"])[0], 200)

    def test_jeton_expire(self):
        _action, data, path = self.create()
        _status, _headers, text = self.page(path)
        page = self.page_data(text)
        self.clock.offset = self.acfg.link_ttl + 1
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410)
        self.assertIn("expiré", text)
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (410, "expired"))
        status, result = self.fetch_receipt(data["request_id"])
        self.assertEqual((status, result["status"]), (410, "expired"))
        # le lien expire avant la demande : ameesh garde le temps de consommer
        self.assertLess(data["link_exp"], data["exp"])

    def test_lien_inconnu(self):
        self.create()
        status, _headers, text = self.page("/a/" + secrets.token_urlsafe(32))
        self.assertEqual(status, 404)
        self.assertEqual(self.http("GET", "/a/court")[0], 404)
        self.assertEqual(self.fetch_receipt("req_" + "0" * 26)[0], 404)

    def test_assertion_invalide_n_use_pas_le_lien(self):
        _action, data, path = self.create()
        _status, _headers, text = self.page(path)
        page = self.page_data(text)
        cases = {
            "foreign_authenticator": self.sign(self.bob, page),
            "user_verification": self.sign(self.alice, page, uv=False),
            "challenge_mismatch": self.sign(self.alice, page, "deny", challenge_of="approve"),
            "origin": self.sign(self.alice, page, origin="https://evil.example"),
            "rp_id_mismatch": self.sign(self.alice, page, rp_id="evil.example"),
        }
        for code, body in cases.items():
            with self.subTest(code):
                status, result = self.submit(path, body)
                self.assertEqual((status, result.get("error")), (403, code), result)
        self.assertEqual(self.fetch_receipt(data["request_id"])[0], 202)
        self.assertEqual(self.submit(path, self.sign(self.alice, page))[0], 200)

    def test_corps_d_assertion_malforme(self):
        _action, _data, path = self.create()
        _status, _headers, text = self.page(path)
        good = self.sign(self.alice, self.page_data(text))
        for label, body in {
            "champ en trop": dict(good, extra="x"),
            "champ manquant": {k: v for k, v in good.items() if k != "signature"},
            "décision": dict(good, decision="approved"),
            "base64 standard": dict(good, signature="ab+/"),
        }.items():
            with self.subTest(label):
                self.assertEqual(self.submit(path, body)[0], 400)
        status, _headers, _payload = self.http(
            "POST", path, origin=ORIGIN, raw=b'{"decision":"approve","decision":"deny"}',
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_action_modifiee_apres_la_demande(self):
        action, _data, path = self.create()
        _status, _headers, text = self.page(path)
        page = self.page_data(text)
        self.source.put(dict(action, args={"pr": 43, "method": "merge"}))
        status, _headers, text = self.page(path)
        self.assertEqual(status, 409)
        self.assertIn("changé", text)
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (409, "action_changed"))
        # action annulée entre la demande et la signature : plus rien à approuver
        self.source.put(dict(action, state="cancelled"))
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (409, "action_state"))
        self.assertEqual(self.page(path)[0], 409)

    def test_empreinte_stockee_incoherente(self):
        action = make_action(digest="sha256:" + "0" * 64)
        self.source.put(action)
        status, _h, payload = self.api("POST", "/requests", {
            "action_id": action["action_id"], "approver": ALICE, "requested_by": AGENT})
        self.assertEqual((status, json.loads(payload)["error"]), (409, "digest_mismatch"))
        ok = make_action()
        ok["digest"] = digest_of(ok)
        self.create(ok)

    def test_validation_de_la_demande(self):
        known = make_action()
        self.source.put(known)
        done = make_action(state="confirmed")
        self.source.put(done)
        cases = [
            ({"action_id": new_action_id(), "approver": ALICE, "requested_by": AGENT},
             404, "unknown_action"),
            ({"action_id": done["action_id"], "approver": ALICE, "requested_by": AGENT},
             409, "action_state"),
            ({"action_id": known["action_id"], "approver": "human:carol", "requested_by": AGENT},
             409, "no_authenticator"),
            ({"action_id": known["action_id"], "approver": "agent:deepseek7",
              "requested_by": AGENT}, 400, "format"),
            ({"action_id": "act_court", "approver": ALICE, "requested_by": AGENT},
             400, "format"),
            ({"action_id": known["action_id"], "approver": ALICE}, 400, "format"),
            ({"action_id": known["action_id"], "approver": ALICE + "\n",
              "requested_by": AGENT}, 400, "format"),
        ]
        for body, status, code in cases:
            with self.subTest(body=body):
                got, _h, payload = self.api("POST", "/requests", body)
                self.assertEqual((got, json.loads(payload)["error"]), (status, code))

    def test_jeton_de_service(self):
        action = make_action()
        self.source.put(action)
        body = {"action_id": action["action_id"], "approver": ALICE, "requested_by": AGENT}
        for label, auth in {"absent": None, "faux": "x" * 43,
                            "préfixe": self.service_token[:-1],
                            "vide": ""}.items():
            with self.subTest(label):
                status, _h, payload = self.api("POST", "/requests", body, auth=auth)
                self.assertEqual((status, json.loads(payload)["error"]), (401, "unauthorized"))
        status, _h, _p = self.api("POST", "/requests", body, auth=None,
                                  headers={"Authorization": "Basic " + self.service_token})
        self.assertEqual(status, 401)
        self.assertEqual(self.api("GET", "/receipts/req_" + "0" * 26, auth=None)[0], 401)
        self.assertEqual(self.api("GET", "/receipts/req_" + "0" * 26, auth="faux" * 10)[0], 401)
        # un navigateur n'a rien à faire sur l'API (Origin présent : refus)
        status, _h, payload = self.api("POST", "/requests", body, origin=ORIGIN)
        self.assertEqual((status, json.loads(payload)["error"]), (403, "origin"))
        self.assertEqual(self.api("POST", "/requests", body)[0], 201)

    def test_host_invalide(self):
        _action, _data, path = self.create()
        self.assertEqual(self.page(path, host="evil.example")[0], 421)
        self.assertEqual(self.page(path, host=self.local)[0], 421)   # page humaine : hôte public
        self.assertEqual(self.page(path, host=None)[0], 421)          # pas d'en-tête Host
        self.assertEqual(self.page(path, host=RP_ID + ":443")[0], 200)
        self.assertEqual(self.page(path, host=RP_ID.upper())[0], 200)
        status, _h, _p = self.http("GET", path, host=RP_ID, headers={"host": "evil.example"})
        self.assertEqual(status, 421)                                 # deux en-têtes Host
        # API : hôte local seulement (anti-rebinding), pas l'hôte public
        action = make_action()
        self.source.put(action)
        body = {"action_id": action["action_id"], "approver": ALICE, "requested_by": AGENT}
        self.assertEqual(self.api("POST", "/requests", body, host=RP_ID)[0], 421)
        self.assertEqual(self.api("POST", "/requests", body, host="evil.example")[0], 421)
        self.assertEqual(self.api("POST", "/requests", body,
                                  host="localhost:%d" % self.port)[0], 201)

    def test_origine_des_post_humains(self):
        _action, _data, path = self.create()
        _status, _headers, text = self.page(path)
        page = self.page_data(text)
        body = self.sign(self.alice, page)
        self.assertEqual(self.submit(path, body, origin="https://evil.example")[0], 403)
        self.assertEqual(self.submit(path, body, origin=None)[0], 403)
        self.assertEqual(self.submit(path, body, origin="null")[0], 403)
        self.assertEqual(self.submit(path, body, headers={"Sec-Fetch-Site": "cross-site"})[0],
                         403)
        self.assertEqual(self.submit(path, body, headers={"Sec-Fetch-Site": "same-origin"})[0],
                         200)

    def _oversized(self, path: str, host: str, extra: dict) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(50 * 1024 * 1024))
            for name, value in extra.items():
                conn.putheader(name, value)
            conn.endheaders()               # on n'envoie PAS le corps annoncé
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def test_corps_trop_gros(self):
        _action, _data, path = self.create()
        self.assertEqual(self._oversized(
            "/requests", self.local, {"Authorization": "Bearer " + self.service_token}), 413)
        self.assertEqual(self._oversized(path, RP_ID, {"Origin": ORIGIN}), 413)
        # juste au-dessus de la limite, corps réellement envoyé
        status, _h, _p = self.api("POST", "/requests",
                                  raw=b"{" + b" " * self.acfg.max_body + b"}",
                                  headers={"Content-Type": "application/json"})
        self.assertEqual(status, 413)
        status, _h, _p = self.api("POST", "/requests", raw=b"{}",
                                  headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _h, _p = self.api("POST", "/requests", raw=b"{}",
                                  headers={"Content-Type": "application/json",
                                           "Transfer-Encoding": "chunked"})
        self.assertEqual(status, 411)

    def test_resume_du_demandeur_ignore(self):
        fake = "sha256:" + "0" * 64
        action, data, path = self.create(extra={
            "summary": "Approuver 1 EUR <b>urgent</b>, rien d'autre",
            "summary_digest": fake, "amount": 1, "target": "autre/depot#1",
        })
        self.assertNotEqual(data["summary_digest"], fake)
        expected = render.render_summary(normalize_action(action), ALICE, AGENT)
        self.assertEqual(data["summary_digest"], render.summary_digest(expected))
        _status, _headers, text = self.page(path)
        for planted in ("Approuver 1 EUR", "urgent", "autre/depot#1", fake):
            self.assertNotIn(planted, text)
        self.assertIn("exemple/depot#42", text)
        self.assertEqual(self.summary_of(text), expected)
        self.assertIn("champs ignorés", self.log.getvalue())
        self.assertNotIn("Approuver 1 EUR", self.log.getvalue())

    def test_echappement_html_et_caracteres_trompeurs(self):
        hostile = make_action(
            project='p"><svg onload=alert(1)>',
            operation="merge</code><script>alert(2)</script>",
            target="<script>alert(1)</script>\nmontant : 0 EUR",
            args={"note": "<img src=x onerror=alert(3)>", "rtl": "fichier\u202egnp.exe",
                  "zw": "a\u200bb", "q": "\" ' & </pre>"},
            amount=12345, currency="EUR")
        _action, data, path = self.create(hostile)
        status, _headers, text = self.page(path)
        self.assertEqual(status, 200)
        self.assertEqual(re.findall(r"<script[^>]*>", text),
                         ['<script src="../static/approve.js" defer>'])
        for raw in ("<script>alert", "<img", "<svg", "onerror=alert(3)>", "\u202e", "\u200b",
                    "</code><script>"):
            self.assertNotIn(raw, text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertIn("\\u{202e}", text)
        self.assertIn("\\u{200b}", text)
        self.assertIn("123.45 EUR", text)
        # une valeur ne s'étale jamais sur plusieurs lignes du résumé
        summary = self.summary_of(text)
        self.assertEqual(len(summary.splitlines()), 13)
        self.assertIn("cible : <script>alert(1)</script>\\u{000a}montant : 0 EUR", summary)
        self.assertEqual(render.summary_digest(summary), data["summary_digest"])
        # sosie : cible avec un « о » cyrillique signalée
        _a, _d, path = self.create(make_action(target="exemple/dep\u043et#42"))
        self.assertIn("non ASCII", self.page(path)[2])

    def test_journal_sans_secret(self):
        _action, data, path = self.create()
        token = path.rsplit("/", 1)[1]
        _status, _headers, text = self.page(path)
        self.submit(path, self.sign(self.alice, self.page_data(text)))
        self.api("POST", "/requests", {"x": 1}, auth="mauvais-jeton-" + "z" * 40)
        journal = self.log.getvalue()
        self.assertIn("/a/<jeton>", journal)
        self.assertIn(data["request_id"], journal)
        for secret in (token, self.service_token, "mauvais-jeton-" + "z" * 40):
            self.assertNotIn(secret, journal)
        # l'état du service ne garde pas le jeton de lien en clair
        state = self.acfg.state_dir
        for directory, _dirs, files in os.walk(state):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700, directory)
            for name in files:
                full = os.path.join(directory, name)
                self.assertEqual(stat.S_IMODE(os.stat(full).st_mode), 0o600, full)
                with open(full, "rb") as fh:
                    self.assertNotIn(token.encode(), fh.read())
                self.assertNotIn(token, name)

    def test_fichiers_statiques_et_methodes(self):
        status, headers, payload = self.http("GET", "/static/approve.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/javascript"))
        self.assertTrue(payload.startswith(b"// SPDX-License-Identifier: AGPL-3.0-only"))
        self.assertSecurityHeaders(headers)
        # le texte ne passe que par textContent : aucun puits HTML ni eval
        self.assertNotRegex(payload, rb"\.(innerHTML|outerHTML)\b|insertAdjacentHTML|"
                                     rb"document\.write|\beval\(|new Function")
        self.assertIn(b'userVerification: "required"', payload)
        status, headers, payload = self.http("GET", "/static/approve.css")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/css"))
        for bad in ("/static/autre.js", "/static/../service.py", "/static/%2e%2e/cli.py",
                    "/inconnu"):
            with self.subTest(bad):
                status, headers, _p = self.http("GET", bad)
                self.assertEqual(status, 404)
                self.assertSecurityHeaders(headers)
        for method in ("PUT", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method):
                status, headers, _p = self.http(method, "/static/approve.js")
                self.assertEqual(status, 405)
                self.assertSecurityHeaders(headers)
        self.assertEqual(self.http("POST", "/static/approve.js", {})[0], 405)
        self.assertEqual(self.api("GET", "/requests")[0], 405)


# --------------------------------------------------------------------------
# alignement avec le lot L5 : nouvelle tentative, décision « assumer le doublon »
# --------------------------------------------------------------------------

class ApproveRetryTest(ApproveBase):
    """Nouvelle tentative (`actions.retry`) : nouveau reçu, MÊME action_id."""

    def request_status(self, action: dict, **extra):
        self.source.put(action)
        body = {"action_id": action["action_id"], "approver": ALICE, "requested_by": AGENT}
        body.update(extra)
        status, _h, payload = self.api("POST", "/requests", body)
        return status, json.loads(payload)

    def test_nouvelle_tentative_apres_echec_certain(self):
        self.assertIn("failed", REQUESTABLE_STATES)
        action = make_action(state="failed", dedupe="none")
        _action, data, path = self.create(action)
        # même action, même empreinte : le reçu vaut pour `retry` de L5
        self.assertEqual((data["action_id"], data["digest"], data["assume_duplicate"]),
                         (action["action_id"], digest_of(action), False))
        status, _headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertIn("Nouvelle tentative de la MÊME action", text)
        self.assertIn("échoué de façon certaine", text)
        self.assertNotIn("DOUBLON", text)
        page = self.page_data(text)
        self.assertEqual(page["kind"], "approve")
        self.assertEqual(self.submit(path, self.sign(self.alice, page))[0], 200)
        _status, receipt = self.fetch_receipt(data["request_id"])
        verdict = receipts.verify_receipt(self.db, receipt, POLICY, kind="action",
                                          expected_digest=digest_of(action),
                                          expected_action_id=action["action_id"],
                                          expect_decision="approve")
        self.assertTrue(verdict.authority, verdict.reason)

    def test_issue_inconnue_selon_la_deduplication(self):
        guaranteed = make_action(state="unknown", dedupe="guaranteed")
        _a, data, path = self.create(guaranteed)
        self.assertEqual(data["digest"], digest_of(guaranteed))
        self.assertIn("garantit la", self.page(path)[2])
        # source sans l'information : L5 tranchera à la liaison (retry)
        self.create(make_action(state="unknown"))
        # sans déduplication garantie : aucune nouvelle tentative, seulement
        # la décision qui assume le doublon
        status, result = self.request_status(make_action(state="unknown", dedupe="none"))
        self.assertEqual((status, result["error"]), (409, "dedupe"))
        self.assertIn("assume_duplicate", result["message"])

    def test_action_remplacee_ou_close(self):
        for state in ("launched", "confirmed", "cancelled"):
            with self.subTest(state):
                status, result = self.request_status(make_action(state=state))
                self.assertEqual((status, result["error"]), (409, "action_state"))
        replaced = make_action(state="unknown", dedupe="none", replaced_by=new_action_id())
        for extra in ({}, {"assume_duplicate": True}):
            with self.subTest(extra=extra):
                status, result = self.request_status(replaced, **extra)
                self.assertEqual((status, result["error"]), (409, "action_replaced"))

    def test_echec_entre_la_demande_et_la_signature(self):
        action, _data, path = self.create(make_action(state="approved"))
        page = self.page_data(self.page(path)[2])
        self.source.put(dict(action, state="launched"))
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (409, "action_state"))
        # échec certain : la même demande (même empreinte) vaut nouvelle tentative
        self.source.put(dict(action, state="failed"))
        self.assertEqual(self.submit(path, self.sign(self.alice, page))[0], 200)


class ApproveAssumeDuplicateTest(ApproveBase):
    """Décision « assumer le doublon » (`actions.replace` du lot L5)."""

    def unknown_action(self, **overrides) -> dict:
        values = dict(state="unknown", dedupe="none", amount=12345, currency="EUR",
                      target="exemple/depot#42")
        values.update(overrides)
        return make_action(**values)

    def test_cycle_complet_assumer_le_doublon(self):
        action = self.unknown_action()
        expected = duplicate.assume_duplicate_digest(action)
        self.assertNotEqual(expected, digest_of(action))
        _a, data, path = self.create(action, extra={"assume_duplicate": True})
        self.assertEqual((data["assume_duplicate"], data["digest"], data["action_digest"]),
                         (True, expected, digest_of(action)))

        status, _headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertSecurityHeaders(_headers)
        # EN CLAIR : une décision d'assomption de doublon, pas une approbation
        self.assertIn("<title>Doublon à assumer — ameesh</title>", text)
        self.assertEqual(html.unescape(re.search(r"<h1>(.*?)</h1>", text).group(1)),
                         "Assumer le risque d'un DOUBLON")
        self.assertNotIn("Approbation demandée", text)
        alert = re.search(r'<section class="duplicate" role="alert">(.*?)</section>', text, re.S)
        self.assertIsNotNone(alert)
        alert = alert.group(1)
        self.assertIn("PAS une approbation ordinaire", alert)
        self.assertIn("assumez le risque d'un DOUBLON de cette action", alert)
        self.assertIn("INCONNUE", alert)
        for shown in (action["action_id"], "exemple/depot#42", "123.45 EUR", "git-merge / merge"):
            self.assertIn(shown, alert)
        self.assertIn('class="approve">Assumer le doublon</button>', text)
        self.assertNotIn(">Approuver</button>", text)
        self.assertIn("Action remplacée", text)
        self.assertIn(render.short_digest(expected), text)
        self.assertIn(digest_of(action), text)
        page = self.page_data(text)
        self.assertEqual(page["kind"], "assume-duplicate")

        # le résumé signé le dit aussi, dès sa première ligne de contenu
        summary = self.summary_of(text)
        lines = summary.splitlines()
        self.assertEqual(lines[0], "ameesh-summary/1")
        self.assertTrue(lines[1].startswith("décision : ASSUMER LE RISQUE D'UN DOUBLON"), lines)
        self.assertEqual(lines[2], "action remplacée : %s" % action["action_id"])
        self.assertIn("empreinte de l'action : %s" % digest_of(action), lines)
        self.assertIn("empreinte de la décision : %s" % expected, lines)
        self.assertEqual(render.summary_digest(summary), data["summary_digest"])
        self.assertEqual(summary, render.render_summary(normalize_action(action), ALICE, AGENT,
                                                        assume_duplicate=True))
        self.assertNotEqual(summary, render.render_summary(normalize_action(action), ALICE,
                                                           AGENT))

        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["decision"]), (200, "approve"), result)
        status, receipt = self.fetch_receipt(data["request_id"])
        self.assertEqual(status, 200)
        self.assertEqual((receipt["request"]["action_id"], receipt["request"]["digest"]),
                         (action["action_id"], expected))
        verdict = receipts.verify_receipt(self.db, receipt, POLICY, kind="action",
                                          expected_digest=expected,
                                          expected_action_id=action["action_id"],
                                          expect_decision="approve")
        self.assertTrue(verdict.authority, verdict.reason)
        # domaine distinct : ce reçu n'approuve pas une nouvelle tentative
        mismatch = receipts.verify_receipt(self.db, receipt, POLICY, kind="action",
                                           expected_digest=digest_of(action),
                                           expected_action_id=action["action_id"])
        self.assertEqual(mismatch.code, receipts.DIGEST_MISMATCH)
        self.assertIn("doublon assumé", self.log.getvalue())

    def test_approbation_ordinaire_ne_vaut_pas_assomption(self):
        action = self.unknown_action(dedupe="guaranteed")
        _a, ordinary, _p = self.create(action)
        _a, assume, path = self.create(action, extra={"assume_duplicate": True})
        self.assertNotEqual(ordinary["digest"], assume["digest"])
        self.assertNotEqual(ordinary["summary_digest"], assume["summary_digest"])
        page = self.page_data(self.page(path)[2])
        self.assertNotEqual(page["challenge-approve"], self.page_data(
            self.page(urlsplit(ordinary["link"]).path)[2])["challenge-approve"])

    def test_assomption_refusee_hors_issue_inconnue(self):
        for state in ("proposed", "approved", "failed", "launched", "confirmed", "cancelled"):
            with self.subTest(state):
                action = self.unknown_action(state=state)
                self.source.put(action)
                status, _h, payload = self.api("POST", "/requests", {
                    "action_id": action["action_id"], "approver": ALICE,
                    "requested_by": AGENT, "assume_duplicate": True})
                self.assertEqual((status, json.loads(payload)["error"]), (409, "action_state"))
        action = self.unknown_action()
        self.source.put(action)
        for value in ("true", 1, None, [True]):
            with self.subTest(value=value):
                status, _h, payload = self.api("POST", "/requests", {
                    "action_id": action["action_id"], "approver": ALICE,
                    "requested_by": AGENT, "assume_duplicate": value})
                self.assertEqual((status, json.loads(payload)["error"]), (400, "format"))
        # `assume_duplicate: false` : approbation ordinaire (refusée ici : dedupe none)
        status, _h, payload = self.api("POST", "/requests", {
            "action_id": action["action_id"], "approver": ALICE, "requested_by": AGENT,
            "assume_duplicate": False})
        self.assertEqual((status, json.loads(payload)["error"]), (409, "dedupe"))

    def test_action_reconciliee_ou_remplacee_apres_la_demande(self):
        action, _data, path = self.create(self.unknown_action(),
                                          extra={"assume_duplicate": True})
        page = self.page_data(self.page(path)[2])
        self.source.put(dict(action, args={"pr": 43, "method": "merge"}))
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (409, "action_changed"))
        self.source.put(dict(action, state="confirmed"))      # réconciliée entre-temps
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (409, "action_state"))
        self.source.put(dict(action, replaced_by=new_action_id()))
        status, result = self.submit(path, self.sign(self.alice, page))
        self.assertEqual((status, result["error"]), (409, "action_replaced"))
        self.assertEqual(self.page(path)[0], 409)
        self.source.put(action)
        status, result = self.submit(path, self.sign(self.alice, page, "deny"))
        self.assertEqual((status, result["decision"]), (200, "deny"))

    def test_echappement_dans_l_encadre(self):
        hostile = self.unknown_action(target="<b>x</b>‮\nmontant : 0 EUR",
                                      operation="merge<script>")
        _a, _d, path = self.create(hostile, extra={"assume_duplicate": True})
        status, _headers, text = self.page(path)
        self.assertEqual(status, 200)
        alert = re.search(r'<section class="duplicate" role="alert">(.*?)</section>',
                          text, re.S).group(1)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;\\u{202e}\\u{000a}montant : 0 EUR", alert)
        self.assertNotIn("<script>", alert)
        self.assertNotIn("‮", text)


class ApproveDuplicateDigestTest(unittest.TestCase):
    """Non-régression : duplicate.py est une réplique EXACTE de
    `actions.assume_duplicate_digest` (lot L5)."""

    REFERENCE = {
        "action_id": "act_01J9Z3Y4X5W6V7T8S9R0Q1P2N3", "project": "ameesh",
        "connector": "git-merge", "operation": "merge", "target": "exemple/depot#42",
        "args": {"pr": 42, "method": "merge", "note": "é"}, "amount": 12345,
        "currency": "EUR", "policy_version": "3", "class": "irreversible", "state": "unknown",
    }
    #: calculés par `ameesh.actions` (origin/lot/L5-actions, 9cf68a3)
    REFERENCE_ACTION_DIGEST = ("sha256:4a8958feb125d34d34ab17458d1f7066"
                               "ab9a1f9ecb2cb67407dd8073b7e1b387")
    REFERENCE_ASSUME_DIGEST = ("sha256:6fc4fef9ba10c58357106c40c7666356"
                               "e1b6a0a9d60cf9283508944b34c663c5")

    def samples(self) -> list[dict]:
        return [
            dict(self.REFERENCE),
            make_action(state="unknown"),
            make_action(state="unknown", amount=0, currency="JPY", policy_version=7,
                        args={"z": [1, None, True], "a": {"é": " "}}),
            make_action(state="unknown", args={}, target="t"),
        ]

    def test_vecteur_de_reference(self):
        self.assertEqual(digest_of(self.REFERENCE), self.REFERENCE_ACTION_DIGEST)
        self.assertEqual(duplicate.assume_duplicate_digest(self.REFERENCE),
                         self.REFERENCE_ASSUME_DIGEST)
        payload = {"action_digest": self.REFERENCE_ACTION_DIGEST,
                   "decision": "assume-duplicate", "replaces": self.REFERENCE["action_id"]}
        self.assertEqual(self.REFERENCE_ASSUME_DIGEST, "sha256:" + hashlib.sha256(
            b"ameesh-assume-duplicate/1\x00" + jcs.canonicalize(payload)).hexdigest())
        # l'action normalisée par la source donne la même empreinte
        normalized = normalize_action(dict(self.REFERENCE))
        self.assertEqual(duplicate.assume_duplicate_digest(normalized),
                         self.REFERENCE_ASSUME_DIGEST)
        other = dict(self.REFERENCE, action_id="act_" + "0" * 26)
        self.assertNotEqual(duplicate.assume_duplicate_digest(other),
                            self.REFERENCE_ASSUME_DIGEST)

    def test_identique_a_actions_py(self):
        try:
            from ameesh import actions
        except ImportError:
            self.skipTest("ameesh.actions (lot L5) non fusionné : vecteur de référence seul")
        self.assertEqual(actions.ASSUME_DUPLICATE_DOMAIN, duplicate.ASSUME_DUPLICATE_DOMAIN)
        for action in self.samples():
            with self.subTest(action=action["action_id"]):
                self.assertEqual(actions.assume_duplicate_digest(action),
                                 duplicate.assume_duplicate_digest(action))
                self.assertEqual(actions.digest(action), digest_of(action))
                normalized = normalize_action(dict(action))
                self.assertEqual(actions.assume_duplicate_digest(normalized),
                                 duplicate.assume_duplicate_digest(normalized))
                request = actions.approval_request(normalized, ALICE, assume_duplicate=True,
                                                   requested_by=AGENT)
                self.assertEqual(request["digest"], duplicate.assume_duplicate_digest(action))


class ApproveRateLimitTest(ApproveBase):
    CONFIG = {"rate_ip_per_minute": 8, "rate_token_per_minute": 3}

    def test_limitation_par_jeton_puis_par_ip(self):
        _action, _data, path = self.create()                   # 1 requête (IP)
        statuses = [self.page(path)[0] for _ in range(4)]      # 4 requêtes (IP), jeton : 3 max
        self.assertEqual(statuses, [200, 200, 200, 429])
        other = "/a/" + secrets.token_urlsafe(32)
        self.assertEqual(self.page(other)[0], 404)             # autre jeton : pas limité
        statuses = [self.http("GET", "/static/approve.css")[0] for _ in range(4)]
        self.assertIn(429, statuses)                           # IP : 8 par minute
        status, headers, _p = self.http("GET", "/static/approve.css")
        self.assertEqual((status, headers.get("retry-after")), (429, "60"))


# --------------------------------------------------------------------------
# enrôlement
# --------------------------------------------------------------------------

class ApproveEnrollTest(ApproveBase):
    def enroll_link(self, approver: str = DAVE) -> str:
        """Le lien, créé par la commande locale `ameesh-approve enroll-link`."""
        proc = subprocess.run(
            [sys.executable, "-m", "ameesh.approve", "enroll-link", "--approver", approver,
             "--rp-id", RP_ID, "--origin", ORIGIN, "--state-dir", self.acfg.state_dir],
            capture_output=True, text=True, timeout=60,
            env=child_env(AMEESH_APPROVE_CONFIG=os.path.join(self.workdir, "absent.json")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        link = urlsplit(proc.stdout.strip())
        self.assertEqual("%s://%s" % (link.scheme, link.netloc), ORIGIN)
        self.assertIn("TÉLÉPHONE", proc.stderr)
        return link.path

    def authenticators(self) -> int:
        return int(self.db.query("SELECT count(*) AS n FROM authenticators")[0]["n"])

    def proposals(self) -> list:
        directory = self.acfg.proposals_path
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def test_enrolement_produit_une_proposition_sans_ecrire_en_base(self):
        before = self.authenticators()
        path = self.enroll_link()
        status, headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertSecurityHeaders(headers)
        page = self.page_data(text, "ameesh-enroll")
        self.assertEqual((page["rp-id"], page["user-name"]), (RP_ID, DAVE))
        self.assertEqual(page["exclude"], "")
        dave = SoftWebAuthn("ES256", backup_eligible=True, backup_state=True)
        status, result = self.submit(path, attestation(
            dave, receipts.b64u_decode(page["challenge"])))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["credential_id"], dave.credential_id)
        self.assertEqual(result["level"], "standard")
        self.assertTrue(result["backup_eligible"])
        # rien en base : le registre de confiance ne change que par PR du canon
        self.assertEqual(self.authenticators(), before)
        self.assertEqual(self.proposals(), [result["proposal"]])
        proposal_path = os.path.join(self.acfg.proposals_path, result["proposal"])
        self.assertEqual(stat.S_IMODE(os.stat(proposal_path).st_mode), 0o600)
        with open(proposal_path, encoding="utf-8") as fh:
            proposal = fh.read()
        self.assertIn('member: "human:dave"', proposal)
        self.assertIn("status: proposed", proposal)
        self.assertIn("attestation_verified: false", proposal)
        # usage unique
        self.assertEqual(self.page(path)[0], 410)
        status, _r = self.submit(path, attestation(dave, receipts.b64u_decode(page["challenge"])))
        self.assertEqual(status, 410)

        # la proposition, une fois reportée au canon (PR fusionnée), fait autorité
        entry = dict(re.findall(r'^  (facade|credential_id|public_key|aaguid|level): (.*)$',
                                proposal, re.M))
        entry = {key: json.loads(value) for key, value in entry.items()}
        self.assertEqual(entry["public_key"], dave.public_key())
        synced = apply_authenticators(self.db, [
            member("alice", self.alice.entry()), member("bob", self.bob.entry()),
            member("dave", entry)])
        self.assertEqual((synced["errors"], synced["added"]),
                         ([], ["human:dave/webauthn/%s" % dave.credential_id]))
        action, data, link = self.create(approver=DAVE)
        _status, _headers, text = self.page(link)
        self.assertEqual(self.submit(link, self.sign(dave, self.page_data(text)))[0], 200)
        receipt = self.fetch_receipt(data["request_id"])[1]
        verdict = receipts.verify_receipt(self.db, receipt, POLICY,
                                          expected_digest=digest_of(action),
                                          expected_action_id=action["action_id"])
        self.assertTrue(verdict.authority, verdict.reason)

    def test_enrolement_refuse_les_ceremonies_falsifiees(self):
        before = self.authenticators()
        path = self.enroll_link()
        page = self.page_data(self.page(path)[2], "ameesh-enroll")
        challenge = receipts.b64u_decode(page["challenge"])
        dave = SoftWebAuthn("ES256")
        cases = {
            "user_verification": attestation(dave, challenge, uv=False),
            "rp_id_mismatch": attestation(dave, challenge, rp_id="evil.example"),
            "client_data_type": attestation(dave, challenge, kind="webauthn.get"),
            "challenge_mismatch": attestation(dave, os.urandom(32)),
            "origin": attestation(dave, challenge, origin="https://evil.example"),
            "credential_id": attestation(dave, challenge, raw_id=os.urandom(16)),
        }
        for code, body in cases.items():
            with self.subTest(code):
                status, result = self.submit(path, body)
                self.assertEqual((status, result.get("error")), (403, code), result)
        self.assertEqual(self.proposals(), [])
        self.assertEqual(self.submit(path, attestation(dave, challenge),
                                     origin="https://evil.example")[0], 403)
        # le lien reste utilisable par la vraie cérémonie
        self.assertEqual(self.submit(path, attestation(dave, challenge))[0], 200)
        self.assertEqual(len(self.proposals()), 1)
        self.assertEqual(self.authenticators(), before)

    def test_enrolement_exclut_les_credentials_connus(self):
        path = self.enroll_link(ALICE)
        page = self.page_data(self.page(path)[2], "ameesh-enroll")
        self.assertEqual(page["exclude"], self.alice.credential_id)


# --------------------------------------------------------------------------
# configuration, jeton de service, permissions (sans base)
# --------------------------------------------------------------------------

class ApproveConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-approve-conf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def config(self, **overrides) -> approve_config.ApproveConfig:
        values = dict(rp_id=RP_ID, origins=(ORIGIN,), state_dir=os.path.join(self.tmp, "s"))
        values.update(overrides)
        return approve_config.ApproveConfig(**values).validate()

    def test_permissions_du_fichier_de_jeton(self):
        path, token = write_token_file(self.tmp)
        self.assertEqual(approve_config.read_service_token(path), token)
        os.chmod(path, 0o400)
        self.assertEqual(approve_config.read_service_token(path), token)
        for mode in (0o644, 0o640, 0o604, 0o660):
            with self.subTest(mode=oct(mode)):
                os.chmod(path, mode)
                with self.assertRaisesRegex(approve_config.ApproveConfigError,
                                            "permissions trop ouvertes"):
                    approve_config.read_service_token(path)
        os.chmod(path, 0o600)
        link = os.path.join(self.tmp, "lien")
        os.symlink(path, link)
        with self.assertRaises(approve_config.ApproveConfigError):
            approve_config.read_service_token(link)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "introuvable"):
            approve_config.read_service_token(os.path.join(self.tmp, "absent"))
        short, _t = write_token_file(os.path.join(self.tmp), token="court")
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "au moins"):
            approve_config.read_service_token(short)

    def test_gen_token(self):
        path = os.path.join(self.tmp, "conf", "service-token")
        proc = subprocess.run([sys.executable, "-m", "ameesh.approve", "gen-token",
                               "--token-file", path], capture_output=True, text=True,
                              timeout=60, env=child_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertGreaterEqual(len(approve_config.read_service_token(path)), 43)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "existe déjà"):
            approve_config.write_service_token(path)

    def test_le_service_refuse_un_jeton_trop_ouvert(self):
        path, _token = write_token_file(self.tmp, mode=0o644)
        proc = subprocess.run(
            [sys.executable, "-m", "ameesh.approve", "serve", "--rp-id", RP_ID,
             "--origin", ORIGIN, "--port", "0", "--token-file", path,
             "--state-dir", os.path.join(self.tmp, "s")],
            capture_output=True, text=True, timeout=60,
            env=child_env(AMEESH_APPROVE_CONFIG=os.path.join(self.tmp, "absent.json")))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("permissions trop ouvertes", proc.stderr)

    def test_dossier_d_etat_prive(self):
        open_dir = os.path.join(self.tmp, "ouvert")
        os.makedirs(open_dir)
        os.chmod(open_dir, 0o755)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "trop ouvertes"):
            Store(open_dir)
        store = Store(os.path.join(self.tmp, "neuf"))
        self.assertEqual(stat.S_IMODE(os.stat(store.root).st_mode), 0o700)

    def test_configuration_refusee(self):
        self.config()
        cases = {
            "écoute publique": dict(bind="0.0.0.0"),
            "écoute réseau": dict(bind="192.0.2.10"),
            "http hors localhost": dict(origins=("http://approve.example.test",)),
            "origine hors RP ID": dict(origins=("https://evil.example",)),
            "origine avec chemin": dict(origins=(ORIGIN + "/x",)),
            "origine port par défaut": dict(origins=(ORIGIN + ":443",)),
            "origine majuscules": dict(origins=("https://Approve.example.test",)),
            "sans origine": dict(origins=()),
            "RP ID vide": dict(rp_id=""),
            "exp trop long": dict(request_ttl=3600),
            "lien plus long que la demande": dict(request_ttl=300, link_ttl=600),
            "niveau": dict(level="max"),
            "public_url étrangère": dict(public_url="https://evil.example/approve"),
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(approve_config.ApproveConfigError):
                    self.config(**overrides)
        self.assertEqual(self.config(bind="::1").bind, "::1")
        cfg = self.config(origins=("https://approve.example.test",
                                   "https://m.approve.example.test:8443"))
        self.assertEqual(cfg.public_hosts, frozenset({
            "approve.example.test", "approve.example.test:443",
            "m.approve.example.test:8443"}))
        self.assertEqual(self.config(public_url=ORIGIN + "/approve/").base_url,
                         ORIGIN + "/approve")

    def test_resume_stable_et_visible(self):
        action = normalize_action(make_action(target="a\u202eb\nc", args={"k": "\u2028"}))
        summary = render.render_summary(action, ALICE, AGENT)
        self.assertEqual(summary, render.render_summary(dict(action), ALICE, AGENT))
        self.assertTrue(summary.startswith("ameesh-summary/1\n"))
        self.assertIn("cible : a\\u{202e}b\\u{000a}c\n", summary)
        self.assertIn('arguments : {"k":"\\u{2028}"}\n', summary)
        self.assertEqual(render.visible("a\\u{0041}"), "a\\\\u{0041}")
        self.assertEqual(render.format_amount(5, "JPY"), "5 JPY (5 en unités mineures)")
        self.assertEqual(render.format_amount(1234, "KWD"), "1.234 KWD (1234 en unités mineures)")

    def test_redaction_du_journal(self):
        token = secrets.token_urlsafe(32)
        self.assertEqual(approve_server.redact("GET /a/%s HTTP/1.1" % token),
                         "GET /a/<jeton> HTTP/1.1")
        self.assertNotIn(token, approve_server.redact("Bearer %s" % token))


# --------------------------------------------------------------------------
# source par défaut : la table `actions` (lot L5)
# --------------------------------------------------------------------------

class ApproveDbSourceTest(PgTestCase):
    COLUMNS = ("action_id text primary key, project text, work_item text, proposed_by text, "
               "connector text, operation text, target text, args jsonb, class text, "
               "amount bigint, currency text, state text, policy_version integer")

    def test_table_absente_erreur_claire(self):
        source = DbActionSource(self.db, "t_l7_absente")
        with self.assertRaisesRegex(ActionSourceError, "L5"):
            source.get_action(new_action_id())
        workdir = tempfile.mkdtemp(prefix="ameesh-approve-src-")
        self.addCleanup(shutil.rmtree, workdir, True)
        cfg = approve_config.ApproveConfig(rp_id=RP_ID, origins=(ORIGIN,),
                                           state_dir=os.path.join(workdir, "s")).validate()
        service = ApproveService(cfg, self.db, source, service_token="t" * 40)
        with self.assertRaises(ApproveError) as caught, \
                self.assertLogs("ameesh.approve", level="WARNING"):
            service.create_request({"action_id": new_action_id(), "approver": ALICE,
                                    "requested_by": AGENT})
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "action_source"))
        self.assertIn("L5", caught.exception.message)

    def test_lecture_d_une_action(self):
        self.db.execute("DROP TABLE IF EXISTS t_l7_actions")
        self.db.execute("CREATE TABLE t_l7_actions (%s)" % self.COLUMNS)
        action = make_action(amount=1500, currency="EUR", args={"pr": 7, "note": "é"})
        self.db.query(
            "INSERT INTO t_l7_actions (action_id, project, work_item, proposed_by, connector, "
            "operation, target, args, class, amount, currency, state, policy_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s) "
            "RETURNING action_id",
            (action["action_id"], action["project"], action["work_item"], action["proposed_by"],
             action["connector"], action["operation"], action["target"],
             json.dumps(action["args"]), action["class"], action["amount"], action["currency"],
             action["state"], action["policy_version"]))
        source = DbActionSource(self.db, "t_l7_actions")
        read = source.get_action(action["action_id"])
        self.assertEqual(read["computed_digest"], digest_of(action))
        self.assertEqual(read["args"], action["args"])
        self.assertIsNone(source.get_action(new_action_id()))
        self.assertIsNone(source.get_action("pas-une-action"))

    def test_deduplication_et_remplacement_lus(self):
        """Colonnes `dedupe` et `replaced_by` de la table de L5 : lues, pas exigées."""
        self.db.execute("DROP TABLE IF EXISTS t_l7_dedupe")
        self.db.execute("CREATE TABLE t_l7_dedupe (%s, dedupe text, replaced_by text)"
                        % self.COLUMNS)
        action = make_action(state="unknown")
        successor = new_action_id()
        self.db.query(
            "INSERT INTO t_l7_dedupe (action_id, project, connector, operation, target, args, "
            "class, state, policy_version, dedupe, replaced_by) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, 'none', %s) RETURNING action_id",
            (action["action_id"], action["project"], action["connector"], action["operation"],
             action["target"], json.dumps(action["args"]), action["class"], action["state"],
             action["policy_version"], successor))
        read = DbActionSource(self.db, "t_l7_dedupe").get_action(action["action_id"])
        self.assertEqual((read["dedupe"], read["replaced_by"]), ("none", successor))
        self.assertEqual(duplicate.assume_duplicate_digest(read),
                         duplicate.assume_duplicate_digest(action))

    def test_colonne_d_empreinte_manquante(self):
        self.db.execute("DROP TABLE IF EXISTS t_l7_incomplete")
        self.db.execute("CREATE TABLE t_l7_incomplete (%s)"
                        % self.COLUMNS.replace(", policy_version integer", ""))
        with self.assertRaisesRegex(ActionSourceError, "policy_version"):
            DbActionSource(self.db, "t_l7_incomplete").get_action(new_action_id())


# --------------------------------------------------------------------------
# ameesh, client du service : action request / fetch-receipt (L9b)
# --------------------------------------------------------------------------

class _FakeApprove:
    """Un faux ameesh-approve sur 127.0.0.1 : réponses imposées, requêtes notées."""

    def __init__(self, test: unittest.TestCase, responder):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                seen.append((self.command, self.path, dict(self.headers.items()), body))
                status, headers, payload = responder(self.command, self.path)
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _reply

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def close() -> None:
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)
        test.addCleanup(close)


class ApproveClientTest(ApproveBase):
    """`ameesh action request` dépose la demande (jeton de service, fichier 0600) et
    affiche le lien ; `ameesh action fetch-receipt` récupère le reçu, le VÉRIFIE,
    puis l'attache à l'action. Le jeton ne sort jamais de l'en-tête Authorization."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE actions, action_attempts, action_events "
                        "RESTART IDENTITY CASCADE")
        self.service.source = DbActionSource(self.server_db)
        self.noop = ShellNoopConnector(os.path.join(self.tmp, "noop"), timeout=10.0)
        self.action = actions.propose(
            self.db, self.noop, project="demo", operation="send", target="client:42",
            args={"n": 1}, proposed_by=AGENT, approvers=[ALICE])
        self.action_id = self.action["action_id"]

    def cli_env(self, **extra) -> dict:
        base = dict(AMEESH_APPROVE_URL="http://127.0.0.1:%d" % self.port,
                    AMEESH_APPROVE_TOKEN_FILE=self.token_file, AMEESH_APPROVE_RP_ID=RP_ID,
                    AMEESH_APPROVE_ORIGINS=ORIGIN, AGENT_MAIL_NAME="deepseek7",
                    # un mandataire configuré n'est jamais utilisé
                    http_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
                    https_proxy="http://127.0.0.1:9", no_proxy="", NO_PROXY="")
        base.update(extra)
        return self.env(**base)

    def cli(self, *args, rc: int = 0, **extra):
        proc = self.mesh("action", *args, env=self.cli_env(**extra))
        self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)
        self.assertNotIn(self.service_token, proc.stdout + proc.stderr)
        return proc

    def thread_text(self) -> str:
        path = fil.transport_for(self.cfg).location(fil.ThreadRef("demo"))
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_url_controlee(self):
        for url in ("", "http://approve.example.test", "ftp://127.0.0.1", "https://u:p@h.test",
                    "https://h.test/?q=1", "https://h.test/#f", "http://10.0.0.1:8765",
                    "https://h.test:99999"):
            with self.subTest(url=url):
                with self.assertRaises(approve_client.ApproveClientError) as caught:
                    approve_client.endpoint(url)
                self.assertEqual(caught.exception.code, "config")
        for url, expected in (("http://127.0.0.1:8765/", "http://127.0.0.1:8765"),
                              ("http://[::1]:8765", "http://[::1]:8765"),
                              ("http://localhost:8765", "http://localhost:8765"),
                              ("https://approve.example.test/api", "https://approve.example.test/api")):
            self.assertEqual(approve_client.endpoint(url), expected)

    def test_demande_puis_recu_par_la_cli(self):
        proc = self.cli("request", self.action_id, "--approver", ALICE)
        link = re.search(r"https://\S+/a/[A-Za-z0-9_-]{43}", proc.stdout).group(0)
        self.assertTrue(link.startswith(ORIGIN + "/a/"))
        request_id = re.search(r"req_[0-9a-z]{26}", proc.stdout).group(0)
        self.assertIn("ameesh action fetch-receipt %s" % self.action_id, proc.stdout)
        # le lien est dans le fil (pas le jeton de service) ; la demande au journal
        thread = self.thread_text()
        self.assertIn(link, thread)
        self.assertIn("Demande d'approbation de l'action %s" % self.action_id, thread)
        self.assertNotIn(self.service_token, thread)
        self.assertEqual(actions.last_approval_request(self.db, self.action_id), request_id)
        notes = [e["note"] for e in actions.events(self.db, self.action_id)
                 if e["event"] == actions.APPROVAL_REQUESTED]
        self.assertEqual(len(notes), 1)
        self.assertNotIn(link.rsplit("/", 1)[1], notes[0])      # le lien, pas au journal

        proc = self.cli("fetch-receipt", self.action_id, rc=5)
        self.assertIn("pas encore signée", proc.stdout)
        self.assertEqual(actions.get(self.db, self.action_id)["state"], "proposed")

        path = urlsplit(link).path
        _status, _headers, text = self.page(path)
        status, result = self.submit(path, self.sign(self.alice, self.page_data(text)))
        self.assertEqual(status, 200, result)
        out = os.path.join(self.tmp, "recu.json")
        proc = self.cli("fetch-receipt", self.action_id, "--out", out, "--by", ALICE)
        self.assertIn("vérifié et attaché", proc.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        row = actions.get(self.db, self.action_id)
        self.assertEqual((row["state"], row["auth_approver"]), ("approved", ALICE))
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["request"]["action_id"], self.action_id)
        result = actions.execute(self.db, self.action_id, self.noop, policy=POLICY, by="porte")
        self.assertEqual(result["state"], "confirmed")
        # une fois liée, la même demande ne se rattache plus (reçu consommé)
        self.cli("fetch-receipt", self.action_id, rc=1)

    def test_session_d_agent_et_refus_du_service(self):
        proc = self.cli("request", self.action_id, "--approver", ALICE, "--requested-by",
                        "human:alice", rc=1)
        self.assertIn("n'est pas son identité", proc.stderr)
        proc = self.cli("request", self.action_id, "--approver", DAVE, rc=1)
        self.assertIn("[no_authenticator]", proc.stderr)
        other, _token = write_token_file(tempfile.mkdtemp(dir=self.workdir))
        proc = self.cli("request", self.action_id, "--approver", ALICE, rc=1,
                        AMEESH_APPROVE_TOKEN_FILE=other)
        self.assertIn("[unauthorized]", proc.stderr)
        self.assertEqual(actions.last_approval_request(self.db, self.action_id), None)

    def test_jeton_trop_ouvert_ou_absent_rien_n_est_envoye(self):
        os.chmod(self.token_file, 0o644)
        proc = self.cli("request", self.action_id, "--approver", ALICE, rc=1)
        self.assertIn("[config]", proc.stderr)
        self.assertIn("permissions trop ouvertes", proc.stderr)
        proc = self.cli("request", self.action_id, "--approver", ALICE, rc=1,
                        AMEESH_APPROVE_TOKEN_FILE="")
        self.assertIn("AMEESH_APPROVE_TOKEN_FILE", proc.stderr)
        proc = self.cli("request", self.action_id, "--approver", ALICE, rc=1,
                        AMEESH_APPROVE_URL="http://approve.example.test")
        self.assertIn("boucle locale", proc.stderr)
        self.assertNotIn("demande req_", self.log.getvalue())
        # sans service : la demande brute reste disponible (--local)
        proc = self.cli("request", self.action_id, "--approver", ALICE, "--local")
        self.assertEqual(json.loads(proc.stdout)["digest"], self.action["digest"])

    def test_aucune_redirection_suivie(self):
        target = _FakeApprove(self, lambda method, path: (201, (), b"{}"))
        redirect = _FakeApprove(self, lambda method, path: (
            307, (("Location", target.url + path),), b""))
        proc = self.cli("request", self.action_id, "--approver", ALICE, rc=1,
                        AMEESH_APPROVE_URL=redirect.url)
        self.assertIn("[redirect]", proc.stderr)
        self.assertEqual(len(redirect.seen), 1)
        self.assertEqual(target.seen, [])           # le jeton n'est allé nulle part ailleurs

    def test_recu_verifie_avant_d_etre_attache(self):
        request = actions.approval_request(self.action, ALICE)
        forged = {
            "credential d'un autre": self.bob.receipt(request),
            "autre action": self.alice.receipt(actions.approval_request(
                actions.propose(self.db, self.noop, project="demo", operation="send",
                                target="client:43", proposed_by=AGENT), ALICE)),
            "signature fausse": dict(self.alice.receipt(request), credential_id=(
                self.alice.credential_id)),
        }
        forged["signature fausse"]["proof"] = dict(
            forged["signature fausse"]["proof"],
            signature=self.alice.receipt(dict(request, nonce=request["nonce"][::-1]))[
                "proof"]["signature"])
        request_id = "req_" + "0" * 26
        for label, receipt in forged.items():
            with self.subTest(label):
                fake = _FakeApprove(self, lambda method, path, r=receipt: (
                    200, (("Content-Type", "application/json"),), json.dumps(r).encode()))
                proc = self.cli("fetch-receipt", self.action_id, "--request-id", request_id,
                                rc=1, AMEESH_APPROVE_URL=fake.url)
                self.assertIn("reçu refusé", proc.stderr)
                self.assertEqual(fake.seen[0][1], "/receipts/" + request_id)
                self.assertEqual(actions.get(self.db, self.action_id)["state"], "proposed")


if __name__ == "__main__":
    unittest.main()
