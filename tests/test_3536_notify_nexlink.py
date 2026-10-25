# SPDX-License-Identifier: AGPL-3.0-only
"""#3536 : canal `nexlink` d'`ameesh notify` (alerte du mesh -> app de l'owner).

Spec owner du 25/10/2026, décision #138440 option a. Aucun envoi réel : un
faux serveur HTTP sur la boucle locale tient les trois routes (`GET
/mesh/alerts/keys`, `POST /mesh/alerts`, `GET /mesh/alerts/{id}`). Les
vecteurs de crypto sont fixés : le wrap X25519 est celui de Nexlink
(`shared/message-crypto/src/x25519-wrap.ts`), revérifié contre `node:crypto`
(crypto native, mêmes octets) ; l'AAD et la signature HMAC sont figées avec
le lot jumeau #3537.
"""
from __future__ import annotations

import base64
import hmac
import http.server
import json
import os
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

from ameesh import nexlink_alerts as nx
from ameesh import notify, role_timers

ACCOUNT = "b1e3c612-4825-4716-abe9-194ab3acde43"
SECRET = "s3cr3t-mesh"
#: l'API #3537 valide `alert_id` comme un UUID (colonne `uuid`)
ALERT_ID = "6f1c2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b"
ALERT_ID_2 = "9a8b7c6d-5e4f-4a3b-9c8d-7e6f5a4b3c2d"
RECIPIENT_PRIVATE = bytes(range(1, 33))
RECIPIENT_PUBLIC = bytes.fromhex(
    "07a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c")
MESH_URL = "https://api-staging.nexlink.ph"


def _b64d(text):
    return base64.b64decode(text)


def _public_key(private: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)


# ==========================================================================
# format et cryptographie (sans réseau)
# ==========================================================================

class CryptoTest(unittest.TestCase):
    def test_vecteur_wrap_nexlink(self):
        """Le vecteur du wrap est identique à celui produit par node:crypto.

        Clés fixes : privée destinataire 01..20, éphémère 21..40, nonce
        65..70, CEK 41..60. Le même calcul en JS (X25519, HKDF-SHA-256,
        AES-256-GCM, AAD de `x25519WrapAad`) donne ces octets, et Node
        déchiffre le wrap produit ici : la compatibilité avec #3537 est
        vérifiée, pas supposée.
        """
        wrapped = nx.wrap_to_x25519(
            RECIPIENT_PUBLIC, nx.WrapContext(ACCOUNT, 1),
            bytes(range(65, 97)),
            ephemeral_private=bytes(range(33, 65)),
            nonce=bytes(range(101, 113)))
        self.assertEqual(len(wrapped), 93)
        self.assertEqual(wrapped.hex(),
                         "015869aff450549732cbaaed5e5df9b30a6da31cb0e5742bad5ad4a1"
                         "a768f1a67b65666768696a6b6c6d6e6f701878826273fdb2d0ca23bc63b11"
                         "adf44afe426af47d37dd81b65d4d0ccb35d5db1b4cbb2b8ddd452474f53108a7e"
                         "22db")
        self.assertEqual(
            nx.unwrap_from_x25519(RECIPIENT_PRIVATE, nx.WrapContext(ACCOUNT, 1),
                                  wrapped),
            bytes(range(65, 97)))

    def test_wrap_contexte_lie_au_destinataire(self):
        wrapped = nx.wrap_to_x25519(
            _public_key(RECIPIENT_PRIVATE), nx.WrapContext(ACCOUNT, 1),
            b"payload")
        for context in (nx.WrapContext(ACCOUNT, 2),          # autre génération
                        nx.WrapContext("b1e3c612-4825-4716-abe9-194ab3acde44", 1)):
            with self.subTest(context=context), self.assertRaises(Exception):
                nx.unwrap_from_x25519(RECIPIENT_PRIVATE, context, wrapped)

    def test_contenu_aes_gcm_et_aad(self):
        cek = bytes(range(32))
        aad = nx.content_aad("a" * 64, "2026-10-25T11:41:27Z")
        nonce, sealed = nx.seal_content(cek, b'{"title":"x"}', aad)
        self.assertEqual(len(nonce), 12)
        self.assertEqual(nx.open_content(cek, nonce, sealed, aad), b'{"title":"x"}')
        with self.assertRaises(Exception):
            nx.open_content(cek, nonce, sealed,
                            nx.content_aad("b" * 64, "2026-10-25T11:41:27Z"))

    def test_plaintext_json_sans_detail_brut(self):
        raw = nx.content_plaintext("titre", "corps", kind="dead_runner",
                                   event="resolved", context={"lot": 3536,
                                                              "since": 1000.0})
        payload = json.loads(raw)
        self.assertEqual(payload, {"title": "titre", "body": "corps",
                                   "kind": "dead_runner", "event": "resolved",
                                   "lot": 3536, "since": 1000.0})

    def test_signature_hmac(self):
        # vecteur vérifié avec `node:crypto` (createHmac sha256)
        self.assertEqual(
            nx.mesh_signature("secret", "ameesh", "1700000000", b'{"x":1}'),
            "v1=ad3f7e602840789e7203dd52783effefe226dd4fd61bebb80b92ca4be40453f0")
        self.assertNotEqual(
            nx.mesh_signature("secret", "ameesh", "1700000000", b'{"x":2}'),
            nx.mesh_signature("secret", "ameesh", "1700000000", b'{"x":1}'))
        headers = nx.mesh_headers("secret", b"", timestamp=1700000000)
        self.assertEqual(set(headers), {"X-Mesh-Key-Id", "X-Mesh-Timestamp",
                                        "X-Mesh-Signature"})

    def test_identites(self):
        # l'API #3537 valide l'alert_id comme un UUID (colonne `uuid`)
        first = nx.alert_id_for("x")
        self.assertEqual(first, nx.alert_id_for("x"))
        self.assertEqual(str(uuid.UUID(first)), first)
        self.assertNotEqual(first, nx.alert_id_for("y"))
        self.assertEqual(nx.delivery_key("raised", first), first)
        self.assertEqual(nx.delivery_key("resolved", first), first)
        with self.assertRaises(ValueError):
            nx.uuid_bytes("00000000-0000-0000-0000-000000000000")


# ==========================================================================
# configuration
# ==========================================================================

def _nx_config(**overrides):
    channel = {"type": "nexlink", "url": MESH_URL, "token_env": "MESH_SECRET",
               "fallback": "ntfy"}
    channel.update(overrides.pop("channel", {}))
    raw = {"routes": {"human:owner": ["nx"]},
           "channels": {"nx": channel,
                        "ntfy": {"type": "ntfy", "url": "https://ntfy.example",
                                 "topic": "ameesh-owner"}}}
    raw.update(overrides)
    return notify.parse_config(raw)


class ConfigTest(unittest.TestCase):
    def test_canal_valide(self):
        ncfg = _nx_config()
        channel = ncfg.channels_for("human:owner")[0]
        self.assertEqual(channel.kind, "nexlink")
        self.assertEqual(channel.options["token_env"], "MESH_SECRET")
        self.assertEqual(channel.options["fallback"], "ntfy")

    def test_secret_en_clair_refuse(self):
        for key in ("token", "secret", "password", "api_key"):
            with self.subTest(key=key), self.assertRaises(notify.NotifyConfigError):
                _nx_config(channel={key: "clair"})

    def test_secret_nomme_obligatoire(self):
        with self.assertRaises(notify.NotifyConfigError) as ctx:
            _nx_config(channel={"token_env": None})
        self.assertIn("token_env", str(ctx.exception))

    def test_url_obligatoire_et_https(self):
        with self.assertRaises(notify.NotifyConfigError):
            _nx_config(channel={"url": None})
        with self.assertRaises(notify.NotifyConfigError):
            _nx_config(channel={"url": "http://api.nexlink.ph"})

    def test_option_inconnue_refusee(self):
        with self.assertRaises(notify.NotifyConfigError):
            _nx_config(channel={"topic": "t"})
        for url in (MESH_URL + "?x=1", MESH_URL + "#f"):
            with self.subTest(url=url), self.assertRaises(notify.NotifyConfigError):
                _nx_config(channel={"url": url})

    def test_repli_declare_remote_et_pas_lui_meme(self):
        with self.assertRaises(notify.NotifyConfigError):
            _nx_config(channel={"fallback": "absent"})
        with self.assertRaises(notify.NotifyConfigError):
            _nx_config(channel={"fallback": "nx"})
        # le repli ne double pas un canal déjà routé vers le même humain
        with self.assertRaises(notify.NotifyConfigError) as ctx:
            _nx_config(routes={"human:owner": ["nx", "ntfy"]})
        self.assertIn("double send", str(ctx.exception))
        raw = {"routes": {"human:owner": ["nx"]},
               "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                   "token_env": "S", "fallback": "app"}}}
        with self.assertRaises(notify.NotifyConfigError):
            notify.parse_config(raw)

    def test_account_overage_urgent(self):
        self.assertIn("account_overage", notify.URGENT_TYPES)
        message = notify.render({"type": "account_overage", "harness": "claude",
                                 "account": "x", "since": 1000.0,
                                 "detail": "au-delà du plan"}, "raised", 2000.0)
        self.assertTrue(message.urgent)

    def test_remote_push_problem_et_repli(self):
        channel = _nx_config().channels["nx"]
        self.assertEqual(notify.remote_push_problem(
            channel, {"MESH_SECRET": SECRET}, ncfg=_nx_config()), "")
        # sans le module de crypto, le repli déclaré couvre le canal
        with mock.patch.object(nx, "crypto_available", return_value=False):
            ncfg = _nx_config()
            self.assertEqual(notify.remote_push_problem(
                ncfg.channels["nx"], {"MESH_SECRET": SECRET}, ncfg=ncfg), "")
            # sans repli, le manque est nommé et ne cite jamais le secret
            ncfg2 = _nx_config(channel={"fallback": None})
            problem = notify.remote_push_problem(ncfg2.channels["nx"],
                                                 {"MESH_SECRET": SECRET}, ncfg=ncfg2)
            self.assertIn("cryptography", problem)
        # secret absent : le repli couvre ; sans repli, la variable est nommée
        ncfg = _nx_config()
        self.assertEqual(notify.remote_push_problem(ncfg.channels["nx"], {}, ncfg=ncfg), "")
        ncfg2 = _nx_config(channel={"fallback": None})
        problem = notify.remote_push_problem(ncfg2.channels["nx"], {}, ncfg=ncfg2)
        self.assertIn("MESH_SECRET", problem)
        self.assertNotIn(SECRET, problem)

    def test_fallback_readiness_pour_le_test(self):
        ncfg = _nx_config()
        self.assertEqual(notify.fallback_readiness(
            ncfg, ncfg.channels["nx"], {"MESH_SECRET": SECRET}),
            {"channel": "ntfy", "ready": True})
        raw = {"routes": {"human:owner": ["nx"]},
               "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                   "token_env": "S", "fallback": "s"},
                            "s": {"type": "slack"}}}
        ncfg = notify.parse_config(raw)
        out = notify.fallback_readiness(ncfg, ncfg.channels["nx"], {})
        self.assertFalse(out["ready"])
        self.assertIn("AMEESH_SLACK_WEBHOOK", out["problem"])
        self.assertIsNone(notify.fallback_readiness(
            ncfg, ncfg.channels["s"], {}))

    def test_role_timers_accepte_le_repli(self):
        cfg = types.SimpleNamespace(notify={
            "routes": {"human:owner": ["nx"]},
            "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                "token_env": "MESH_SECRET", "fallback": "ntfy"},
                         "ntfy": {"type": "ntfy", "url": "https://ntfy.example",
                                  "topic": "t"}}})
        with mock.patch.object(nx, "crypto_available", return_value=False):
            humans, error = role_timers.notify_reach(
                cfg, responsible="human:owner", env={"MESH_SECRET": SECRET})
            self.assertEqual((humans, error), (["human:owner"], ""))
        cfg.notify["channels"]["nx"]["fallback"] = None
        with mock.patch.object(nx, "crypto_available", return_value=False):
            _, error = role_timers.notify_reach(
                cfg, responsible="human:owner", env={"MESH_SECRET": SECRET})
        self.assertIn("nexlink", error)


# ==========================================================================
# faux serveur Nexlink
# ==========================================================================

class FakeMesh:
    """Les routes du contrat #3537 : clés, dépôt signé, accusé de lecture."""

    def __init__(self, *, keys=None, responses=None, receipt=None):
        self.keys = list(keys) if keys is not None else [{
            "key_id": "wrap-1", "gen": 1, "alg": nx.KEY_ALG,
            "public_key": RECIPIENT_PUBLIC.hex(), "active": True}]
        self.responses = list(responses or [])
        self.receipt = receipt or {"accepted_at": "2026-10-25T11:41:30Z",
                                   "pushed": True, "read_at": "2026-10-25T11:42:00Z"}
        self.requests: list = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _record(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size) if size else b""
                timestamp = self.headers.get("X-Mesh-Timestamp") or ""
                key_id = self.headers.get("X-Mesh-Key-Id") or ""
                expected = nx.mesh_signature(SECRET, key_id, timestamp, body)
                owner.requests.append({
                    "method": self.command, "path": self.path,
                    "headers": dict(self.headers), "body": body,
                    "signature_ok": hmac.compare_digest(
                        expected, self.headers.get("X-Mesh-Signature") or "")})

            def _json(self, status, payload):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802 - API de http.server
                self._record()
                if self.path == "/mesh/alerts/keys":
                    self._json(200, {"account_id": ACCOUNT, "keys": owner.keys})
                elif self.path.startswith("/mesh/alerts/"):
                    self._json(200, owner.receipt)
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802 - API de http.server
                self._record()
                if self.path != "/mesh/alerts":
                    self._json(404, {"error": "not_found"})
                    return
                if owner.responses:
                    status, payload = owner.responses.pop(0)
                else:
                    status, payload = 202, {"accepted": True, "duplicate": False,
                                            "id": "row-1"}
                self._json(status, payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def by_path(self, path, method=None):
        return [r for r in self.requests
                if r["path"] == path and (method is None or r["method"] == method)]


class _MeshCase(unittest.TestCase):
    def mesh(self, **kw):
        server = FakeMesh(**kw)
        self.addCleanup(server.close)
        return server

    def config(self, server, **overrides):
        raw = {"routes": {"human:owner": ["nx"]},
               "channels": {"nx": {"type": "nexlink", "url": server.url,
                                   "token_env": "MESH_SECRET", "fallback": "ntfy"},
                            "ntfy": {"type": "ntfy", "url": "https://ntfy.example",
                                     "topic": "t"}}}
        raw.update(overrides)
        ncfg = notify.parse_config(raw)
        return ncfg, ncfg.channels["nx"]

    def sender(self, env=None):
        return notify.Sender(env={"MESH_SECRET": SECRET} if env is None else env)

    @staticmethod
    def message(**over):
        base = {"title": "ameesh: agent stopped with mail",
                "body": "[stopped_with_mail] agent ouvrier\narrêté avec 2 messages",
                "urgent": True, "alert_id": ALERT_ID, "event": "raised",
                "occurred_at": "2026-10-25T11:41:27Z", "kind": "stopped_with_mail",
                "context": {"agent": "ouvrier"}}
        base.update(over)
        return notify.Message(**base)

    def post_body(self, server):
        (request,) = server.by_path("/mesh/alerts", "POST")
        return json.loads(request["body"])


class SenderTest(_MeshCase):
    def test_envoi_signe_chiffre_et_wrap(self):
        server = self.mesh()
        _, channel = self.config(server)
        sender = self.sender()
        sender.send(channel, self.message())
        (request,) = server.by_path("/mesh/alerts", "POST")
        self.assertTrue(request["signature_ok"])
        self.assertEqual(request["headers"]["X-Mesh-Key-Id"], "ameesh-v1")
        self.assertNotIn("ouvrier", request["body"].decode("utf-8"))
        body = json.loads(request["body"])
        self.assertEqual(set(body), {"v", "alert_id", "severity", "occurred_at",
                                     "ciphertext", "nonce", "wraps"})
        self.assertEqual((body["v"], body["alert_id"], body["severity"],
                          body["occurred_at"]),
                         (1, ALERT_ID, "urgent", "2026-10-25T11:41:27Z"))
        self.assertEqual(str(uuid.UUID(body["alert_id"])), ALERT_ID)
        self.assertEqual(request["headers"]["X-Mesh-Idempotency-Key"], ALERT_ID)
        # l'enveloppe se déchiffre avec la clé privée du compte (celle de l'app)
        (wrap,) = body["wraps"]
        self.assertEqual((wrap["key_id"], wrap["gen"]), ("wrap-1", 1))
        cek = nx.unwrap_from_x25519(RECIPIENT_PRIVATE,
                                    nx.WrapContext(ACCOUNT, 1),
                                    _b64d(wrap["wrap"]))
        plaintext = nx.open_content(
            cek, _b64d(body["nonce"]), _b64d(body["ciphertext"]),
            nx.content_aad(body["alert_id"], body["occurred_at"]))
        content = json.loads(plaintext)
        self.assertEqual(content["title"], "ameesh: agent stopped with mail")
        self.assertEqual(content["kind"], "stopped_with_mail")
        self.assertEqual(content["agent"], "ouvrier")
        # le dernier envoi retenu pour l'accusé de `--test`
        self.assertEqual(sender.last[channel.name]["alert_id"], ALERT_ID)
        self.assertTrue(sender.last[channel.name]["accepted"])

    def test_key_id_surchargeable(self):
        server = self.mesh()
        _, channel = self.config(server)
        sender = self.sender({"MESH_SECRET": SECRET,
                              "AMEESH_NEXLINK_KEY_ID": "ameesh-test"})
        sender.send(channel, self.message())
        (request,) = server.by_path("/mesh/alerts", "POST")
        self.assertTrue(request["signature_ok"])
        self.assertEqual(request["headers"]["X-Mesh-Key-Id"], "ameesh-test")
        body = json.loads(request["body"])
        self.assertNotIn("event", body)
        self.assertEqual(request["headers"]["X-Mesh-Idempotency-Key"], ALERT_ID)

    def test_resolution_non_poussee(self):
        """#3537 v1 ne stocke que l'alerte levée : une résolution ne part pas."""
        server = self.mesh()
        _, channel = self.config(server)
        sender = self.sender()
        sender.send(channel, self.message(event="resolved"))
        self.assertEqual(server.requests, [])
        self.assertEqual(sender.last[channel.name]["skipped"], "resolved")

    def test_test_avec_accuse(self):
        server = self.mesh()
        ncfg, channel = self.config(server)
        sender = self.sender()
        self.assertEqual(notify.send_test(ncfg, "human:owner", "hote",
                                          sender=sender), [("nx", None)])
        (request,) = server.by_path("/mesh/alerts", "POST")
        body = json.loads(request["body"])
        self.assertNotIn("kind", body)     # le type reste dans le chiffré
        self.assertEqual(sender.last["nx"]["alert_id"],
                         notify.test_alert_id("human:owner", channel, "hote"))
        receipt = sender.receipt(channel, sender.last["nx"]["alert_id"])
        self.assertTrue(receipt["pushed"])

    def test_key_id_invalide_refuse(self):
        server = self.mesh()
        _, channel = self.config(server)
        with self.assertRaises(notify.ChannelError) as ctx:
            self.sender({"MESH_SECRET": SECRET,
                         "AMEESH_NEXLINK_KEY_ID": "a\nb"}).send(channel, self.message())
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(server.requests, [])

    def test_content_free_sans_cle(self):
        server = self.mesh(keys=[])
        _, channel = self.config(server)
        self.sender().send(channel, self.message())
        body = self.post_body(server)
        self.assertTrue(body["content_free"])
        self.assertNotIn("ciphertext", body)
        self.assertNotIn("wraps", body)
        raw = server.by_path("/mesh/alerts", "POST")[0]["body"].decode("utf-8")
        self.assertNotIn("ouvrier", raw)

    def test_plusieurs_cles_actives(self):
        second = _public_key(bytes(range(9, 41)))
        server = self.mesh(keys=[
            {"key_id": "old", "gen": 1, "public_key": RECIPIENT_PUBLIC.hex(),
             "active": False},
            {"key_id": "wrap-1", "gen": 1, "public_key": RECIPIENT_PUBLIC.hex(),
             "active": True},
            {"key_id": "device-2", "gen": 2, "public_key": second.hex(),
             "active": True}])
        _, channel = self.config(server)
        self.sender().send(channel, self.message())
        wraps = self.post_body(server)["wraps"]
        self.assertEqual([(w["key_id"], w["gen"]) for w in wraps],
                         [("wrap-1", 1), ("device-2", 2)])

    def test_cle_inconnue_409_relit_et_reessaie(self):
        server = self.mesh(responses=[(409, {"error": "key_unknown"}),
                                      (202, {"accepted": True, "duplicate": False})])
        _, channel = self.config(server)
        self.sender().send(channel, self.message())
        self.assertEqual(len(server.by_path("/mesh/alerts/keys", "GET")), 2)
        self.assertEqual(len(server.by_path("/mesh/alerts", "POST")), 2)
        self.assertTrue(all(r["signature_ok"] for r in server.requests))

    def test_409_apres_relecture_est_permanent(self):
        server = self.mesh(responses=[(409, {"error": "key_unknown"}),
                                      (409, {"error": "key_unknown"})])
        _, channel = self.config(server)
        with self.assertRaises(notify.ChannelError) as ctx:
            self.sender().send(channel, self.message())
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(ctx.exception.status, 409)

    def test_401_permanent_429_non(self):
        server = self.mesh(responses=[(401, {"error": "unauthorized"})])
        _, channel = self.config(server)
        with self.assertRaises(notify.ChannelError) as ctx:
            self.sender().send(channel, self.message())
        self.assertTrue(ctx.exception.permanent)
        server2 = self.mesh(responses=[(429, {"error": "rate_limited"})])
        _, channel2 = self.config(server2)
        with self.assertRaises(notify.ChannelError) as ctx:
            self.sender().send(channel2, self.message())
        self.assertFalse(ctx.exception.permanent)

    def test_cache_des_cles(self):
        server = self.mesh()
        _, channel = self.config(server)
        sender = self.sender()
        sender.send(channel, self.message())
        sender.send(channel, self.message(alert_id=ALERT_ID_2))
        self.assertEqual(len(server.by_path("/mesh/alerts/keys", "GET")), 1)
        self.assertEqual(len(server.by_path("/mesh/alerts", "POST")), 2)

    def test_accuse_de_lecture_signe(self):
        server = self.mesh()
        ncfg, channel = self.config(server)
        receipt = self.sender().receipt(channel, ALERT_ID)
        self.assertEqual(receipt["read_at"], "2026-10-25T11:42:00Z")
        (request,) = server.by_path("/mesh/alerts/" + ALERT_ID, "GET")
        self.assertTrue(request["signature_ok"])

    def test_secret_fichier_0600(self):
        server = self.mesh()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mesh.secret")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(SECRET + "\n")
            os.chmod(path, 0o600)
            ncfg = notify.parse_config({
                "routes": {"human:owner": ["nx"]},
                "channels": {"nx": {"type": "nexlink", "url": server.url,
                                    "token_file": path}}})
            notify.Sender(env={}).send(ncfg.channels["nx"], self.message())
            self.assertTrue(server.by_path("/mesh/alerts", "POST")[0]["signature_ok"])
            os.chmod(path, 0o644)
            with self.assertRaises(notify.ChannelError) as ctx:
                notify.Sender(env={}).send(ncfg.channels["nx"], self.message())
            self.assertTrue(ctx.exception.permanent)
            self.assertIn("readable by others", str(ctx.exception))


# ==========================================================================
# notifier : identité et repli
# ==========================================================================

class FakeSender:
    """Un `Sender` sans réseau : `outcomes[canal]` est une file de résultats
    (`None` = succès, une `ChannelError` = échec)."""

    def __init__(self, outcomes=None):
        self.outcomes = {name: list(queue) for name, queue in (outcomes or {}).items()}
        self.sent: list = []

    def send(self, channel, message):
        self.sent.append((channel.name, message))
        queue = self.outcomes.get(channel.name) or []
        result = queue.pop(0) if queue else None
        if isinstance(result, Exception):
            raise result


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.logs: list = []
        self.cfg = types.SimpleNamespace(state_dir=tempfile.mkdtemp(), host="hote")

    def ncfg(self):
        return notify.parse_config({
            "routes": {"human:owner": ["nx"]},
            "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                "token_env": "MESH_SECRET", "fallback": "ntfy"},
                         "ntfy": {"type": "ntfy", "url": "https://ntfy.example",
                                  "topic": "t"}}})

    def entry(self, **over):
        entry = {"alert": {"type": "dead_runner", "agent": "ouvrier", "since": 900.0,
                           "detail": "exécuteur mort"},
                 "status": "envoi", "human": "human:owner", "source": "agent",
                 "channels": {"nx": {"ok": False, "attempts": 0, "error": None}}}
        entry.update(over)
        return entry

    def notifier(self, sender, state=None):
        return notify.Notifier(self.cfg, self.ncfg(), state or notify.empty_state(),
                               sender=sender, log=self.logs.append,
                               clock=lambda: 1000.0)

    def test_identite_stable_et_instant_fige(self):
        ncfg = notify.parse_config({
            "routes": {"human:owner": ["nx"]},
            "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                "token_env": "MESH_SECRET"}}})
        sender = FakeSender({"nx": [notify.ChannelError("boom") for _ in range(2)]})
        notifier = notify.Notifier(self.cfg, ncfg, notify.empty_state(),
                                   sender=sender, log=self.logs.append,
                                   clock=lambda: 1000.0)
        entry = self.entry()
        notifier._attempt(entry, "raised", 1000.0)
        notifier._attempt(entry, "raised", 1100.0)
        first, second = [m for _, m in sender.sent]
        self.assertEqual(first.alert_id, second.alert_id)
        self.assertEqual((first.occurred_at, second.occurred_at),
                         ("1970-01-01T00:16:40Z", "1970-01-01T00:16:40Z"))
        self.assertEqual(first.alert_id,
                         nx.alert_id_for(notify._notification_id(
                             notify.key_of(entry["alert"]),
                             notify._entry_severity(entry), "human:owner",
                             notify._since_of(entry["alert"]))))
        self.assertEqual(str(uuid.UUID(first.alert_id)), first.alert_id)
        # une autre alerte (autre `since`) n'a pas la même identité
        other = self.entry(alert={"type": "dead_runner", "agent": "ouvrier",
                                  "since": 950.0, "detail": "x"})
        notifier._attempt(other, "raised", 1200.0)
        self.assertNotEqual(sender.sent[-1][1].alert_id, first.alert_id)
        self.assertIn("alert_id", notifier._records[-1])

    def test_repli_au_deuxieme_echec_et_une_seule_fois(self):
        sender = FakeSender({"nx": [notify.ChannelError("boom") for _ in range(4)]})
        notifier = self.notifier(sender)
        entry = self.entry()
        notifier._attempt(entry, "raised", 1000.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx"])
        self.assertEqual(notifier.state["fallback"], {})
        notifier._attempt(entry, "raised", 1030.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "nx", "ntfy"])
        fallback = sender.sent[-1][1]
        self.assertEqual(fallback.title[:8], "[repli] ")
        self.assertTrue(fallback.body.startswith("repli nexlink — "))
        self.assertEqual(len(notifier.state["fallback"]), 1)
        self.assertEqual(notifier._records[-1]["fallback"], {"ntfy": "ok"})
        notifier._attempt(entry, "raised", 1060.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "nx", "ntfy", "nx"])
        # le repli ne vaut pas livraison du primaire : le primaire continue
        self.assertEqual(notifier.state["notified"], {})
        self.assertFalse(entry["channels"]["nx"]["ok"])

    def test_erreur_permanente_repli_immediat(self):
        sender = FakeSender({"nx": [notify.ChannelError("crypto absente",
                                                        permanent=True)]})
        notifier = self.notifier(sender)
        entry = self.entry()
        notifier._attempt(entry, "raised", 1000.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "ntfy"])
        self.assertEqual(entry["channels"]["nx"]["attempts"], notifier.ncfg.max_attempts)
        self.assertEqual(len(notifier.state["fallback"]), 1)

    def test_repli_avec_un_seul_essai_primaire(self):
        """Cas 3 de dev4 : avec `max_attempts=1`, un échec transitoire du
        primaire doit ouvrir le repli dans le même passage."""
        ncfg = notify.parse_config({
            "routes": {"human:owner": ["nx"]},
            "max_attempts": 1,
            "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                "token_env": "MESH_SECRET", "fallback": "ntfy"},
                         "ntfy": {"type": "ntfy", "url": "https://ntfy.example",
                                  "topic": "t"}}})
        sender = FakeSender({"nx": [notify.ChannelError("timeout")]})
        notifier = notify.Notifier(self.cfg, ncfg, notify.empty_state(),
                                   sender=sender, log=self.logs.append,
                                   clock=lambda: 1000.0)
        entry = self.entry()
        notifier._attempt(entry, "raised", 1000.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "ntfy"])
        self.assertTrue(entry["channels"]["nx→ntfy"]["ok"])
        self.assertEqual(len(notifier.state["fallback"]), 1)
        self.assertEqual(entry["channels"]["nx"]["attempts"], 1)

    def test_repli_retente_meme_si_le_primaire_est_epuise(self):
        """B/P1 de la relecture cx1 : un repli temporairement en échec doit
        être retenté alors que le primaire est déjà à `max_attempts`."""
        sender = FakeSender({
            "nx": [notify.ChannelError("secret absent", permanent=True)],
            "ntfy": [notify.ChannelError("timeout"), None]})
        notifier = self.notifier(sender)
        entry = self.entry()
        notifier._attempt(entry, "raised", 1000.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "ntfy"])
        self.assertEqual(entry["channels"]["nx"]["attempts"],
                         notifier.ncfg.max_attempts)
        self.assertEqual(notifier._pending(entry), ["nx→ntfy"])
        # le chemin réel du passage suivant : `_retry` ne voit plus que le repli
        notifier._retry(entry, "raised", 1030.0)
        self.assertEqual([name for name, _ in sender.sent],
                         ["nx", "ntfy", "ntfy"])
        self.assertTrue(entry["channels"]["nx→ntfy"]["ok"])
        self.assertFalse(entry["channels"]["nx"]["ok"])
        self.assertEqual(len(notifier.state["fallback"]), 1)
        self.assertEqual(notifier.state["notified"], {})

    def test_succes_primaire_annule_le_repli_en_attente(self):
        sender = FakeSender({
            "nx": [notify.ChannelError("boom"), notify.ChannelError("boom"), None],
            "ntfy": [notify.ChannelError("timeout")]})
        notifier = self.notifier(sender)
        entry = self.entry()
        notifier._attempt(entry, "raised", 1000.0)
        notifier._attempt(entry, "raised", 1030.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "nx", "ntfy"])
        self.assertFalse(entry["channels"]["nx→ntfy"]["ok"])
        self.assertEqual(entry["channels"]["nx→ntfy"]["attempts"], 1)
        notifier._attempt(entry, "raised", 1060.0)
        self.assertEqual([name for name, _ in sender.sent],
                         ["nx", "nx", "ntfy", "nx"])
        self.assertTrue(entry["channels"]["nx"]["ok"])
        self.assertTrue(entry["channels"]["nx→ntfy"]["ok"])
        self.assertTrue(entry["channels"]["nx→ntfy"].get("cancelled"))
        self.assertEqual(notifier.state["fallback"], {})
        self.assertEqual(len(notifier.state["notified"]), 1)

    def test_succes_primaire_pas_de_repli_et_registre(self):
        sender = FakeSender()
        notifier = self.notifier(sender)
        entry = self.entry()
        notifier._attempt(entry, "raised", 1000.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx"])
        self.assertTrue(entry["channels"]["nx"]["ok"])
        self.assertEqual(len(notifier.state["notified"]), 1)
        self.assertNotIn("fallback", notifier._records[-1])

    def test_repli_absent(self):
        ncfg = notify.parse_config({
            "routes": {"human:owner": ["nx"]},
            "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                "token_env": "MESH_SECRET"}}})
        sender = FakeSender({"nx": [notify.ChannelError("boom") for _ in range(4)]})
        notifier = notify.Notifier(self.cfg, ncfg, notify.empty_state(),
                                   sender=sender, log=self.logs.append,
                                   clock=lambda: 1000.0)
        entry = self.entry()
        for pass_no in range(3):
            notifier._attempt(entry, "raised", 1000.0 + 30 * pass_no)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "nx", "nx"])

    def test_episode_sans_since_a_une_identite_distincte(self):
        """B/P2 cx3 : deux occurrences d'un type sans `since` (account_overage)
        doivent avoir deux `alert_id`, et les retries le même."""
        ncfg = notify.parse_config({
            "routes": {"human:owner": ["nx"]},
            "channels": {"nx": {"type": "nexlink", "url": MESH_URL,
                                "token_env": "MESH_SECRET"}}})
        sender = FakeSender({"nx": [notify.ChannelError("boom"),
                                    notify.ChannelError("boom"), None]})
        notifier = notify.Notifier(self.cfg, ncfg, notify.empty_state(),
                                   sender=sender, log=self.logs.append,
                                   clock=lambda: 1000.0)
        alert = {"type": "account_overage", "harness": "claude", "account": "x"}
        first = self.entry(alert=alert, raised_ts=1000.0)
        notifier._attempt(first, "raised", 1000.0)
        notifier._attempt(first, "raised", 1030.0)
        second = self.entry(alert=alert, raised_ts=2000.0)
        notifier._attempt(second, "raised", 2000.0)
        ids = [message.alert_id for _, message in sender.sent]
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0], ids[2])
        self.assertEqual(str(uuid.UUID(ids[0])), ids[0])

    def test_membre_resume_sans_since_par_episode(self):
        """B/P2 cx3 : le `raised_ts` de secours distingue les membres de
        résumé d'account_overage, et il survit à la normalisation/reprise."""
        notifier = self.notifier(FakeSender())
        alert = {"type": "account_overage", "harness": "claude", "account": "x"}
        first = self.entry(alert=alert, raised_ts=900.0)
        second = self.entry(alert=alert, raised_ts=1001.0)
        one = notifier._summary_member(first, "human:owner")
        other = notifier._summary_member(second, "human:owner")
        self.assertNotEqual(notifier._summary_member_id(one),
                            notifier._summary_member_id(other))
        raised = {notifier._summary_member_id(one): one,
                  notifier._summary_member_id(other): other}
        restored = notifier._normalize_summary_raised(
            "human:owner", json.loads(json.dumps(raised)))
        self.assertEqual(set(restored), set(raised))

    def test_resume_sans_since_ne_perd_pas_le_second_episode(self):
        """B/P2 cx3 : le second account_overage arrive pendant le gel du
        premier ; il survit à la clôture et part avec un nouvel `alert_id`."""
        sender = FakeSender({"nx": [notify.ChannelError("boom"),
                                    notify.ChannelError("boom"), None]})
        notifier = self.notifier(sender)
        alert = {"type": "account_overage", "harness": "claude", "account": "x"}
        bucket = notifier._bucket("human:owner")
        notifier._summary_add(bucket, "raised",
                              self.entry(alert=alert, raised_ts=900.0,
                                         status="resumee"))
        notifier._summaries(1000.0)              # premier envoi, réponse perdue
        notifier._summary_add(
            bucket, "resolved",
            {"alert": alert, "raised_ts": 900.0, "human": "human:owner",
             "status": "envoi"})
        notifier._summary_add(bucket, "raised",
                              self.entry(alert=alert, raised_ts=1001.0,
                                         status="resumee"))
        self.assertEqual(len(bucket["raised"]), 2)
        self.assertEqual(len(bucket["resolved"]), 1)
        notifier._summaries(1060.0)              # retry du premier épisode
        notifier._summaries(1120.0)              # succès du premier épisode
        self.assertEqual(len(bucket["raised"]), 1)
        notifier._summaries(1180.0)              # second épisode
        summaries = [message for _, message in sender.sent
                     if message.kind == "summary"]
        self.assertGreaterEqual(len(summaries), 4)
        self.assertNotEqual(summaries[0].alert_id, summaries[-1].alert_id)
        self.assertEqual(str(uuid.UUID(summaries[-1].alert_id)),
                         summaries[-1].alert_id)

    def test_resume_identite_propre_a_l_episode(self):
        """B/P2 cx3 : deux résumés successifs au texte identique sont deux
        épisodes distincts ; un retry garde l'identité de son épisode."""
        notifier = self.notifier(FakeSender())
        first, second = {}, {}
        one = notifier._summary_alert_id("human:owner", first)
        self.assertEqual(one, notifier._summary_alert_id("human:owner", first))
        # l'identité survit à un rechargement de l'état (retry après restart)
        self.assertEqual(one, notifier._summary_alert_id(
            "human:owner", json.loads(json.dumps(first))))
        other = notifier._summary_alert_id("human:owner", second)
        self.assertNotEqual(one, other)
        self.assertEqual(str(uuid.UUID(one)), one)
        self.assertEqual(str(uuid.UUID(other)), other)

    def test_resume_passe_par_le_repli_nexlink(self):
        """B/P1 cx3 : un résumé de débit atteint le repli quand le primaire
        est en panne, et le repli est retenté indépendamment du primaire."""
        sender = FakeSender({
            "nx": [notify.ChannelError("boom") for _ in range(5)],
            "ntfy": [notify.ChannelError("timeout"), None]})
        notifier = self.notifier(sender)
        notifier._summary_add(notifier._bucket("human:owner"), "raised",
                              self.entry())
        for pass_no in range(5):
            notifier._summaries(1000.0 + 60 * pass_no)
        self.assertEqual([name for name, _ in sender.sent],
                         ["nx", "nx", "ntfy", "nx", "ntfy", "nx", "nx"])
        sent = [message for _, message in sender.sent]
        self.assertTrue(sent[4].title.startswith("[repli] "))
        self.assertTrue(sent[4].body.startswith("repli nexlink — "))
        self.assertEqual({message.alert_id for message in sent}, {sent[0].alert_id})
        self.assertEqual(len(notifier.state["fallback"]), 1)
        self.assertNotIn("human:owner", notifier.state["summary"])
        # un repli réussi ne marque pas les membres livrés côté primaire
        self.assertEqual(notifier.state["notified"], {})

    def test_resume_erreur_permanente_repli_immediat(self):
        """Le cas de la relecture cx3 B/P1 : erreur permanente du primaire,
        le résumé part sur ntfy dans le même passage, pas cinq fois sur nx."""
        sender = FakeSender({"nx": [notify.ChannelError("401", permanent=True)]})
        notifier = self.notifier(sender)
        notifier._summary_add(notifier._bucket("human:owner"), "raised",
                              self.entry())
        notifier._summaries(1000.0)
        self.assertEqual([name for name, _ in sender.sent], ["nx", "ntfy"])
        self.assertTrue(sender.sent[-1][1].body.startswith("repli nexlink — "))
        self.assertEqual(len(notifier.state["fallback"]), 1)
        self.assertNotIn("human:owner", notifier.state["summary"])
        self.assertEqual(notifier.state["notified"], {})

    def test_resume_fige_ses_membres_avant_l_envoi(self):
        """B/P2 cx1 : un membre arrivé après le premier envoi ne doit pas
        changer le contenu sous le même `alert_id` — le hub dédupliquerait le
        retry et le membre ajouté serait perdu."""
        sender = FakeSender({"nx": [notify.ChannelError("response lost"),
                                    None, None]})
        notifier = self.notifier(sender)
        one = self.entry(alert={"type": "dead_runner", "agent": "one",
                                "since": 900.0, "detail": "x"})
        notifier._summary_add(notifier._bucket("human:owner"), "raised", one)
        notifier._summaries(1000.0)
        # le second membre arrive alors que le premier envoi n'a pas été
        # acquitté (réponse perdue) : il doit attendre l'épisode suivant.
        two = self.entry(alert={"type": "dead_runner", "agent": "two",
                                "since": 1001.0, "detail": "y"})
        notifier._summary_add(notifier._bucket("human:owner"), "raised", two)
        notifier._summaries(2000.0)
        self.assertEqual(len(sender.sent), 2)
        first, retry = sender.sent[0][1], sender.sent[1][1]
        self.assertEqual(first.alert_id, retry.alert_id)
        self.assertEqual((retry.title, retry.body), (first.title, first.body))
        # seul le membre figé est marqué ; l'autre reste pour son épisode
        self.assertEqual(len(notifier.state["notified"]), 1)
        self.assertIn("human:owner", notifier.state["summary"])
        notifier._summaries(2100.0)
        third = sender.sent[-1][1]
        self.assertNotEqual(third.alert_id, first.alert_id)
        self.assertNotIn("human:owner", notifier.state["summary"])
        self.assertEqual(len(notifier.state["notified"]), 2)

    def test_message_sans_identite(self):
        message = notify.render_summary({"raised": {"dead_runner": 2}}, "hote")
        first = notify.message_alert_id(message)
        self.assertEqual(first, notify.message_alert_id(
            notify.render_summary({"raised": {"dead_runner": 2}}, "hote")))
        self.assertNotEqual(first, notify.message_alert_id(
            notify.render_summary({"raised": {"dead_runner": 3}}, "hote")))

    def test_test_alert_id_stable(self):
        ncfg = self.ncfg()
        channel = ncfg.channels["nx"]
        self.assertEqual(notify.test_alert_id("human:owner", channel, "hote"),
                         notify.test_alert_id("human:owner", channel, "hote"))
        message = notify.render_test("human:owner", channel, "hote")
        self.assertEqual(message.alert_id,
                         notify.test_alert_id("human:owner", channel, "hote"))
        self.assertEqual(message.kind, "test")


if __name__ == "__main__":
    unittest.main()
