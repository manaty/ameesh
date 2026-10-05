# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-approve multi-équipe (lot L27, décision 0026) — recette du contrat.

* profil strict : RP ID exactement égal à l'hôte de l'unique origine et de
  `public_url` ; zones et hôtes réservés refusés (et leurs parents) ; mode
  « compatible » explicite pour les essais locaux ;
* deux équipes A et B (deux schémas, deux configurations, deux services) :
  une passkey, une origine, un jeton, une action ou un reçu de B ne vaut
  jamais pour A ; RP ID parent refusé ; POST navigateur d'un site frère
  (`Sec-Fetch-Site: same-site`) refusé ; changement d'hébergement à hôte
  constant sans réenrôlement ; changement d'hôte → réenrôlement ;
* `GET /health` et `ameesh approve-check` : concordance de la politique ;
* `approve_url` distincte de `public_url`, API ouverte sous l'hôte public
  seulement par `api_via_public` écrit dans le JSON ;
* TLS local facultatif (passthrough) : permissions, rechargement à chaud ;
* `deploy/sql/role-approve.sql` sur un Postgres réel : le service tourne
  sous ce rôle, qui ne lit rien d'autre et n'écrit nulle part.

Noms d'hôtes et d'équipes génériques (domaine réservé `.test`, RFC 2606).
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import uuid
from urllib.parse import urlsplit

from ameesh import actions, approve_client, approve_check
from ameesh import db as db_mod
from ameesh import migrations, receipts
from ameesh.approve import config as approve_config
from ameesh.approve import server as approve_server
from ameesh.approve.service import ApproveService
from ameesh.approve.sources import DbActionSource
from ameesh.approve.tls import TlsReloader
from ameesh.connectors.shell_noop import ShellNoopConnector
from ameesh.storage.postgres.authenticators import AUTH_COLUMNS

from .support import REPO, TEST_DSN, PgTestCase, apply_authenticators, child_env
from .webauthn_soft import SoftWebAuthn

H_A = "equipe-a.approve.example.test"
H_B = "equipe-b.approve.example.test"
PARENT = "approve.example.test"
ORIGIN_A = "https://" + H_A
ORIGIN_B = "https://" + H_B
ALICE = "human:alice"
AGENT = "agent:deepseek7"
COMMIT = "c" * 40
ROLE_SQL = os.path.join(REPO, "deploy", "sql", "role-approve.sql")


def member(title: str, *entries: dict) -> dict:
    return {"title": title, "canon_ref": "members/%s.md@%s" % (title, COMMIT),
            "authenticators": list(entries)}


def write_private(path: str, data: bytes, mode: int = 0o600) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)
    return path


def new_token_file(directory: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return write_private(os.path.join(directory, "service-token"),
                         (token + "\n").encode()), token


def quiet_logs(test: unittest.TestCase) -> None:
    """Journal du service gardé hors de la sortie des tests."""
    logger = logging.getLogger("ameesh.approve")
    previous = (logger.propagate, list(logger.handlers), logger.level)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    def restore():
        logger.propagate, logger.handlers, level = previous[0], previous[1], previous[2]
        logger.setLevel(level)
    test.addCleanup(restore)


class Instance:
    """Une instance d'ameesh-approve : sa config, son état privé, son jeton,
    son serveur sur la boucle locale (port éphémère), sa base (schéma)."""

    def __init__(self, test: PgTestCase, schema: str, host: str, *, workdir: str,
                 state_dir: str | None = None, tls: TlsReloader | None = None, **config):
        self.test = test
        self.host = host
        self.origin = "https://" + host
        self.workdir = workdir
        os.makedirs(workdir, mode=0o700, exist_ok=True)
        self.token_file, self.token = new_token_file(workdir)
        self.cfg = approve_config.ApproveConfig(
            rp_id=host, origins=(self.origin,), port=0, token_file=self.token_file,
            state_dir=state_dir or os.path.join(workdir, "state"), **config).validate()
        self.db = test.connect(schema)
        self.service = ApproveService(
            self.cfg, self.db, DbActionSource(self.db), service_token=self.token)
        self.server = approve_server.make_server(self.service, tls=tls)
        self.port = self.server.server_address[1]
        self.local = "127.0.0.1:%d" % self.port
        self.thread = approve_server.serve_in_thread(self.server)
        self.tls = tls
        self.stopped = False
        test.addCleanup(self.stop)

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.db.close()

    # -- HTTP --------------------------------------------------------------
    def http(self, method: str, path: str, body=None, *, host: str | None = None,
             origin: str | None = None, auth: str | None = None,
             headers: dict | None = None, tls_name: str | None = None,
             cafile: str | None = None):
        if self.tls is not None:
            context = ssl.create_default_context(cafile=cafile)
            raw = socket.create_connection(("127.0.0.1", self.port), timeout=15)
            sock = context.wrap_socket(raw, server_hostname=tls_name or self.host)
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
            conn.sock = sock
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            sent = {"Host": host or self.host}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                sent["Content-Type"] = "application/json"
            if origin is not None:
                sent["Origin"] = origin
            if auth is not None:
                sent["Authorization"] = "Bearer " + auth
            sent.update(headers or {})
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def api(self, method: str, path: str, body=None, **kwargs):
        kwargs.setdefault("host", self.local)
        kwargs.setdefault("auth", self.token)
        return self.http(method, path, body, **kwargs)

    def request(self, action_id: str, approver: str = ALICE) -> tuple[dict, str]:
        status, payload = self.api("POST", "/requests", {
            "action_id": action_id, "approver": approver, "requested_by": AGENT})
        self.test.assertEqual(status, 201, payload)
        data = json.loads(payload)
        link = urlsplit(data["link"])
        self.test.assertEqual("%s://%s" % (link.scheme, link.netloc), self.origin)
        return data, link.path

    def page(self, path: str) -> dict:
        status, payload = self.http("GET", path)
        self.test.assertEqual(status, 200, payload)
        match = re.search(r'<main id="ameesh-approval" ([^>]*)>', payload.decode())
        import html
        return {name: html.unescape(value)
                for name, value in re.findall(r'data-([a-z-]+)="([^"]*)"', match.group(1))}

    def submit(self, path: str, body: dict, *, origin: str | None = None, **kwargs):
        status, payload = self.http("POST", path, body,
                                    origin=self.origin if origin is None else origin, **kwargs)
        return status, json.loads(payload or b"{}")

    def receipt(self, request_id: str) -> dict:
        status, payload = self.api("GET", "/receipts/" + request_id)
        self.test.assertEqual(status, 200, payload)
        return json.loads(payload)


def signed(passkey: SoftWebAuthn, page: dict, decision: str = "approve", **proof) -> dict:
    challenge = receipts.b64u_decode(page["challenge-" + decision])
    return dict(passkey.proof(challenge, **proof), decision=decision,
                credential_id=passkey.credential_id)


# --------------------------------------------------------------------------
# profil strict (unitaire)
# --------------------------------------------------------------------------

class ProfilStrictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l27-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def config(self, **values) -> approve_config.ApproveConfig:
        values.setdefault("rp_id", H_A)
        values.setdefault("origins", (ORIGIN_A,))
        values.setdefault("state_dir", os.path.join(self.tmp, "s"))
        return approve_config.ApproveConfig(**values).validate()

    def refused(self, fragment: str, **values) -> None:
        with self.assertRaises(approve_config.ApproveConfigError) as caught:
            self.config(**values)
        self.assertIn(fragment, str(caught.exception))

    def test_strict_par_defaut(self):
        cfg = self.config()
        self.assertEqual((cfg.profile, cfg.rp_id, cfg.origins), ("strict", H_A, (ORIGIN_A,)))
        self.assertEqual(self.config(public_url=ORIGIN_A + "/").base_url, ORIGIN_A)

    def test_rp_id_exactement_l_hote(self):
        # RP ID parent de l'origine : admis par WebAuthn, refusé par le profil
        self.refused("l'origine doit être exactement 'https://%s'" % PARENT,
                     rp_id=PARENT, origins=(ORIGIN_A,))
        self.refused("exactement UNE origine", origins=(ORIGIN_A, "https://m." + H_A))
        self.refused("sans port", origins=(ORIGIN_A + ":8443",))
        self.refused("pas d'hôte partagé par chemin", public_url=ORIGIN_A + "/equipe-a")
        self.refused("ni localhost", rp_id="localhost", origins=("http://localhost:8765",))
        self.refused("ni localhost", rp_id="127.0.0.1", origins=("https://127.0.0.1",))
        # formes IPv4 historiques (inet_aton, règle WHATWG du dernier segment)
        for host in ("127.1", "0x7f.1", "0177.0.0.1", "10.0.0.010", "a.b.0x10", "a.b.123",
                     "2130706433.0"):
            with self.subTest(host=host):
                with self.assertRaises(approve_config.ApproveConfigError):
                    self.config(rp_id=host, origins=("https://" + host,))
        for host in ("a1.example.test", "0x.example.test", "123.example.test"):
            self.assertEqual(self.config(rp_id=host, origins=("https://" + host,)).rp_id, host)

    def test_mode_compatible_explicite(self):
        cfg = self.config(rp_id="localhost", origins=("http://localhost:8765",),
                          profile="compatible")
        self.assertEqual(cfg.profile, "compatible")
        cfg = self.config(rp_id=PARENT, origins=(ORIGIN_A, ORIGIN_B), profile="compatible",
                          public_url=ORIGIN_A + "/x")
        self.assertEqual(cfg.base_url, ORIGIN_A + "/x")
        self.refused("profil inconnu", profile="souple")

    def test_zones_et_hotes_reserves(self):
        values = dict(reserved_zones=("pages.example.test",),
                      reserved_hosts=("approve.pages.example.test",))
        for rp_id, fragment in (("pages.example.test", "une zone réservée"),
                                ("approve.pages.example.test", "un hôte réservé"),
                                ("example.test", "parent du nom réservé")):
            for profile in ("strict", "compatible"):
                with self.subTest(rp_id=rp_id, profile=profile):
                    self.refused(fragment, rp_id=rp_id, origins=("https://" + rp_id,),
                                 profile=profile, **values)
        # un hôte d'équipe SOUS la zone ou sous l'hôte réservé reste admis
        for rp_id in ("equipe-a.pages.example.test", "equipe-a.approve.pages.example.test"):
            self.assertEqual(self.config(rp_id=rp_id, origins=("https://" + rp_id,),
                                         **values).rp_id, rp_id)
        # réservés par la décision 0026, sans configuration
        for rp_id in ("nexlink.ph", "ameesh.nexlink.ph"):
            self.refused("réservé", rp_id=rp_id, origins=("https://" + rp_id,))
        self.assertEqual(self.config(rp_id="equipe-a.nexlink.ph",
                                     origins=("https://equipe-a.nexlink.ph",)).rp_id,
                         "equipe-a.nexlink.ph")
        self.refused("nom réservé illisible", reserved_zones=("Zone Invalide",))

    def test_chargement_json_et_environnement(self):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"rp_id": "zone.example.test", "origins": ["https://zone.example.test"],
                       "reserved_zones": ["zone.example.test"]}, fh)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "zone réservée"):
            approve_config.load({}, path=path)
        env = {"AMEESH_APPROVE_RP_ID": H_A, "AMEESH_APPROVE_ORIGINS": ORIGIN_A,
               "AMEESH_APPROVE_RESERVED_ZONES": "approve.example.test, autre.test",
               "AMEESH_APPROVE_CONFIG": os.path.join(self.tmp, "absent.json")}
        cfg = approve_config.load(env)
        self.assertEqual(cfg.reserved_zones, ("approve.example.test", "autre.test"))
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "parent du nom"):
            approve_config.load(dict(env, AMEESH_APPROVE_RP_ID="example.test",
                                     AMEESH_APPROVE_ORIGINS="https://example.test"))
        cfg = approve_config.load(dict(env, AMEESH_APPROVE_PROFILE="compatible",
                                       AMEESH_APPROVE_ORIGINS=ORIGIN_A + ",https://m." + H_A))
        self.assertEqual(len(cfg.origins), 2)

    def test_api_publique_jamais_implicite(self):
        absent = os.path.join(self.tmp, "absent.json")
        env = {"AMEESH_APPROVE_RP_ID": H_A, "AMEESH_APPROVE_ORIGINS": ORIGIN_A,
               "AMEESH_APPROVE_CONFIG": absent, "AMEESH_APPROVE_API_VIA_PUBLIC": "1"}
        self.assertFalse(approve_config.load(env).api_via_public)   # pas une variable
        path = os.path.join(self.tmp, "config.json")
        for value, ok in ((True, True), (False, True), ("true", False), (1, False)):
            with self.subTest(value=value):
                with open(path, "w") as fh:
                    json.dump({"rp_id": H_A, "origins": [ORIGIN_A], "api_via_public": value}, fh)
                if ok:
                    self.assertIs(approve_config.load({}, path=path).api_via_public, value)
                else:
                    with self.assertRaisesRegex(approve_config.ApproveConfigError,
                                                "booléen JSON"):
                        approve_config.load({}, path=path)

    def test_cli_refuse_un_rp_id_parent(self):
        token = os.path.join(self.tmp, "tok")
        new_token_file(self.tmp)
        os.replace(os.path.join(self.tmp, "service-token"), token)
        proc = subprocess.run(
            [sys.executable, "-m", "ameesh.approve", "serve", "--rp-id", PARENT,
             "--origin", ORIGIN_A, "--port", "0", "--token-file", token,
             "--state-dir", os.path.join(self.tmp, "s")],
            capture_output=True, text=True, timeout=60,
            env=child_env(AMEESH_APPROVE_CONFIG=os.path.join(self.tmp, "absent.json")))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("profil strict", proc.stderr)
        self.assertIn("RP ID = hôte de l'origine", proc.stderr)
        proc = subprocess.run(
            [sys.executable, "-m", "ameesh.approve", "enroll-link", "--approver", ALICE,
             "--rp-id", "nexlink.ph", "--origin", "https://nexlink.ph",
             "--state-dir", os.path.join(self.tmp, "s")],
            capture_output=True, text=True, timeout=60,
            env=child_env(AMEESH_APPROVE_CONFIG=os.path.join(self.tmp, "absent.json")))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("zone réservée", proc.stderr)


# --------------------------------------------------------------------------
# deux équipes, deux schémas, deux services
# --------------------------------------------------------------------------

class EquipesTest(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.schema_b = cls.schema + "_b"
        cls.db_b = db_mod.connect(dataclasses.replace(cls.cfg, schema=cls.schema_b))
        migrations.migrate(cls.db_b, log=None)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.db_b.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % cls.schema_b)
            cls.db_b.close()
        finally:
            super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        quiet_logs(self)
        self.work = tempfile.mkdtemp(prefix="ameesh-l27-equipes-")
        self.addCleanup(shutil.rmtree, self.work, True)
        for db in (self.db, self.db_b):
            db.execute("TRUNCATE authenticators, standing_approvals, standing_reservations, "
                       "mesh_consumed_nonces, actions, action_attempts, action_events "
                       "RESTART IDENTITY CASCADE")
        # le même humain dans les deux équipes, une passkey par équipe (par RP ID)
        self.alice_a = SoftWebAuthn("ES256", rp_id=H_A, origin=ORIGIN_A)
        self.alice_b = SoftWebAuthn("ES256", rp_id=H_B, origin=ORIGIN_B)
        for db, passkey in ((self.db, self.alice_a), (self.db_b, self.alice_b)):
            self.assertEqual(apply_authenticators(db, [member("alice", passkey.entry())])
                             ["errors"], [])
        self.noop = ShellNoopConnector(os.path.join(self.work, "noop"), timeout=10.0)
        self.a = self.instance(self.schema, H_A, "a")
        self.b = self.instance(self.schema_b, H_B, "b")

    def instance(self, schema: str, host: str, label: str, **kwargs) -> Instance:
        return Instance(self, schema, host, workdir=os.path.join(self.work, label), **kwargs)

    def propose(self, db) -> dict:
        return actions.propose(db, self.noop, project="demo", operation="send",
                               target="client:42", args={"n": 1}, proposed_by=AGENT,
                               approvers=[ALICE])

    def approved_receipt(self, inst: Instance, passkey: SoftWebAuthn, db) -> tuple[dict, dict]:
        action = self.propose(db)
        data, path = inst.request(action["action_id"])
        status, result = inst.submit(path, signed(passkey, inst.page(path)))
        self.assertEqual(status, 200, result)
        return action, inst.receipt(data["request_id"])

    @staticmethod
    def policy(host: str) -> receipts.Policy:
        return receipts.Policy(rp_id=host, origins=("https://" + host,))

    def authenticator_count(self, db) -> int:
        return int(db.query("SELECT count(*) AS n FROM authenticators")[0]["n"])

    # -- B ne vaut jamais pour A ------------------------------------------------
    def test_passkey_de_b_ne_vaut_pas_pour_a(self):
        action = self.propose(self.db)
        _data, path = self.a.request(action["action_id"])
        page = self.a.page(path)
        self.assertEqual(page["credentials"], self.alice_a.credential_id)
        # la passkey de B, telle qu'un navigateur la produirait (RP ID de B) …
        status, result = self.a.submit(path, signed(self.alice_b, page))
        self.assertEqual((status, result["error"]), (403, receipts.UNKNOWN_AUTHENTICATOR))
        # … ou même forcée sous l'origine et le RP ID de A : inconnue du registre de A
        status, result = self.a.submit(path, signed(self.alice_b, page, rp_id=H_A,
                                                    origin=ORIGIN_A))
        self.assertEqual((status, result["error"]), (403, receipts.UNKNOWN_AUTHENTICATOR))
        # le lien n'est pas consommé par ces refus : la passkey de A passe
        self.assertEqual(self.a.submit(path, signed(self.alice_a, page))[0], 200)

    def test_origine_de_b_refusee_par_a(self):
        action = self.propose(self.db)
        _data, path = self.a.request(action["action_id"])
        page = self.a.page(path)
        # en-tête Origin de B sur un POST à A
        status, result = self.a.submit(path, signed(self.alice_a, page), origin=ORIGIN_B)
        self.assertEqual((status, result["error"]), (403, "origin"))
        # origine de B DANS l'assertion (clientDataJSON) : refusée par la politique de A
        status, result = self.a.submit(path, signed(self.alice_a, page, origin=ORIGIN_B))
        self.assertEqual((status, result["error"]), (403, receipts.ORIGIN))
        # la page de A sous l'hôte de B : Host refusé
        self.assertEqual(self.a.http("GET", path, host=H_B)[0], 421)
        self.assertEqual(self.a.submit(path, signed(self.alice_a, page))[0], 200)

    def test_jeton_de_b_refuse_par_a(self):
        action = self.propose(self.db)
        body = {"action_id": action["action_id"], "approver": ALICE, "requested_by": AGENT}
        status, payload = self.a.api("POST", "/requests", body, auth=self.b.token)
        self.assertEqual((status, json.loads(payload)["error"]), (401, "unauthorized"))
        data_b, _path = self.b.request(self.propose(self.db_b)["action_id"])
        self.assertEqual(self.a.api("GET", "/receipts/" + data_b["request_id"],
                                    auth=self.b.token)[0], 401)
        # même avec le bon jeton, une demande de B est inconnue de A (état privé)
        self.assertEqual(self.a.api("GET", "/receipts/" + data_b["request_id"])[0], 404)

    def test_action_de_b_inconnue_de_a(self):
        action_b = self.propose(self.db_b)
        status, payload = self.a.api("POST", "/requests", {
            "action_id": action_b["action_id"], "approver": ALICE, "requested_by": AGENT})
        self.assertEqual((status, json.loads(payload)["error"]), (404, "unknown_action"))

    def test_recu_de_b_ne_vaut_pas_pour_a(self):
        action_b, receipt_b = self.approved_receipt(self.b, self.alice_b, self.db_b)
        verdict = receipts.verify_receipt(self.db_b, receipt_b, self.policy(H_B),
                                          expected_digest=action_b["digest"],
                                          expected_action_id=action_b["action_id"])
        self.assertTrue(verdict.authority, verdict.reason)
        # vérificateur de A : registre de A, politique de A
        verdict = receipts.verify_receipt(self.db, receipt_b, self.policy(H_A),
                                          expected_digest=action_b["digest"],
                                          expected_action_id=action_b["action_id"],
                                          consume_by="porte-a")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, receipts.UNKNOWN_AUTHENTICATOR)
        # même registre que B mais politique de A : origine et RP ID refusés
        verdict = receipts.verify_receipt(self.db_b, receipt_b, self.policy(H_A),
                                          expected_digest=action_b["digest"],
                                          expected_action_id=action_b["action_id"])
        self.assertEqual(verdict.code, receipts.ORIGIN)
        # la porte de A ne connaît pas l'action de B
        with self.assertRaises(actions.ActionError):
            actions.approve(self.db, action_b["action_id"], receipt_b, policy=self.policy(H_A))
        # rien consommé dans A : le reçu reste utilisable par B, sous sa porte
        self.assertEqual(int(self.db.query(
            "SELECT count(*) AS n FROM mesh_consumed_nonces")[0]["n"]), 0)

    # -- RP ID parent, site frère ------------------------------------------------
    def test_rp_id_parent_refuse(self):
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "profil strict"):
            approve_config.ApproveConfig(rp_id=PARENT, origins=(ORIGIN_A,)).validate()
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "zone réservée"):
            approve_config.ApproveConfig(rp_id=PARENT, origins=(ORIGIN_A,),
                                         profile="compatible",
                                         reserved_zones=(PARENT,)).validate()
        # une passkey créée sous le RP ID parent (portée commune aux deux
        # équipes) n'est pas acceptée par A, même enrôlée dans son registre
        shared = SoftWebAuthn("ES256", rp_id=PARENT, origin=ORIGIN_A)
        self.assertEqual(apply_authenticators(
            self.db, [member("alice", self.alice_a.entry(), shared.entry())])["errors"], [])
        action = self.propose(self.db)
        _data, path = self.a.request(action["action_id"])
        page = self.a.page(path)
        status, result = self.a.submit(path, signed(shared, page))
        self.assertEqual((status, result["error"]), (403, receipts.RP_ID))

    def test_post_navigateur_d_un_site_frere_refuse(self):
        """B et A sont « same-site » (même domaine enregistrable) : un script de
        B peut viser A ; Sec-Fetch-Site: same-site est refusé, quelle que soit
        l'origine annoncée."""
        action = self.propose(self.db)
        _data, path = self.a.request(action["action_id"])
        page = self.a.page(path)
        body = signed(self.alice_a, page)
        for origin, site in ((ORIGIN_B, "same-site"), (ORIGIN_A, "same-site"),
                             (ORIGIN_A, "cross-site"), (ORIGIN_A, "none")):
            with self.subTest(origin=origin, site=site):
                status, result = self.a.submit(path, body, origin=origin,
                                               headers={"Sec-Fetch-Site": site})
                self.assertEqual((status, result["error"]), (403, "origin"))
        # l'enrôlement aussi
        link = self.a.service.create_enroll_link("human:dave")["link"]
        status, payload = self.a.http("POST", urlsplit(link).path, {"x": 1}, origin=ORIGIN_A,
                                      headers={"Sec-Fetch-Site": "same-site"})
        self.assertEqual((status, json.loads(payload)["error"]), (403, "origin"))
        # le lien a survécu aux refus
        status, _r = self.a.submit(path, body, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)

    # -- changement d'hébergement ------------------------------------------------
    def test_changement_d_hebergement_a_hote_constant_sans_reenrolement(self):
        enrolled = self.authenticator_count(self.db)
        action1 = self.propose(self.db)
        data1, path1 = self.a.request(action1["action_id"])
        page1 = self.a.page(path1)
        self.assertEqual(self.a.submit(path1, signed(self.alice_a, page1))[0], 200)
        receipt1 = self.a.receipt(data1["request_id"])
        consumed = receipts.verify_receipt(self.db, receipt1, self.policy(H_A),
                                           expected_digest=action1["digest"],
                                           expected_action_id=action1["action_id"],
                                           consume_by="porte")
        self.assertTrue(consumed.consumed, consumed.reason)
        # un lien encore ouvert sur l'ancien emplacement : il expirera avec lui
        action2 = self.propose(self.db)
        _data2, path2 = self.a.request(action2["action_id"])

        # bascule : ancien écrivain arrêté, état privé transféré avec ses
        # permissions, nouveau jeton ; même base, même registre, même H
        old_token = self.a.token
        self.a.stop()
        new_state = os.path.join(self.work, "ailleurs", "state")
        os.makedirs(os.path.dirname(new_state), mode=0o700)
        shutil.copytree(self.a.cfg.state_dir, new_state, symlinks=True)
        moved = self.instance(self.schema, H_A, "ailleurs", state_dir=new_state)
        self.assertNotEqual(moved.port, self.a.port)

        # ancien lien consommé : jamais réutilisable ; ancien jeton : refusé
        self.assertEqual(moved.http("GET", path1)[0], 410)
        self.assertEqual(moved.api("POST", "/requests", {
            "action_id": action2["action_id"], "approver": ALICE, "requested_by": AGENT},
            auth=old_token)[0], 401)
        # rejeu du reçu consommé : refusé (nonces en base, pas dans l'état)
        again = receipts.verify_receipt(self.db, receipt1, self.policy(H_A),
                                        expected_digest=action1["digest"],
                                        expected_action_id=action1["action_id"],
                                        consume_by="porte")
        self.assertEqual(again.code, receipts.REPLAY)
        # la passkey déjà enrôlée approuve au nouvel emplacement, sans enrôlement
        action3 = self.propose(self.db)
        data3, path3 = moved.request(action3["action_id"])
        self.assertEqual(moved.submit(path3, signed(self.alice_a, moved.page(path3)))[0], 200)
        receipt3 = moved.receipt(data3["request_id"])
        verdict = receipts.verify_receipt(self.db, receipt3, self.policy(H_A),
                                          expected_digest=action3["digest"],
                                          expected_action_id=action3["action_id"],
                                          consume_by="porte")
        self.assertTrue(verdict.consumed, verdict.reason)
        self.assertEqual(self.authenticator_count(self.db), enrolled)
        # le lien resté ouvert avant la bascule reste utilisable une fois, ici
        self.assertEqual(moved.submit(path2, signed(self.alice_a, moved.page(path2)))[0], 200)

    def test_changement_d_hote_exige_un_reenrolement(self):
        nouvel_hote = "approve.equipe-a.example"
        moved = self.instance(self.schema, nouvel_hote, "nouvel-hote")
        action = self.propose(self.db)
        _data, path = moved.request(action["action_id"])   # le registre est le même
        page = moved.page(path)
        self.assertEqual(page["rp-id"], nouvel_hote)
        # la passkey existante est liée à H_A : son assertion porte rpIdHash(H_A)
        status, result = moved.submit(path, signed(self.alice_a, page,
                                                   origin=moved.origin))
        self.assertEqual((status, result["error"]), (403, receipts.RP_ID))
        status, result = moved.submit(path, signed(self.alice_a, page))   # origine H_A
        self.assertEqual((status, result["error"]), (403, receipts.ORIGIN))
        # un reçu signé sous H_A ne vaut pas pour la politique du nouvel hôte
        action_a, receipt_a = self.approved_receipt(self.a, self.alice_a, self.db)
        verdict = receipts.verify_receipt(self.db, receipt_a, self.policy(nouvel_hote),
                                          expected_digest=action_a["digest"],
                                          expected_action_id=action_a["action_id"])
        self.assertEqual(verdict.code, receipts.ORIGIN)
        # réenrôlement : nouvelle cérémonie sous le nouvel hôte → proposition
        # de canon portant le nouveau RP ID (jamais une écriture en base)
        before = self.authenticator_count(self.db)
        link = moved.service.create_enroll_link(ALICE)["link"]
        view = moved.service.enroll_view(urlsplit(link).path.rsplit("/", 1)[1])
        self.assertEqual(view["rp_id"], nouvel_hote)
        self.assertIn(self.alice_a.credential_id, view["exclude"])
        from .test_approve import attestation
        fresh = SoftWebAuthn("ES256", rp_id=nouvel_hote, origin=moved.origin)
        status, payload = moved.http("POST", urlsplit(link).path, attestation(
            fresh, receipts.b64u_decode(view["challenge"]), origin=moved.origin,
            rp_id=nouvel_hote), origin=moved.origin)
        self.assertEqual(status, 200, payload)
        proposal = json.loads(payload)["proposal"]
        with open(os.path.join(moved.cfg.proposals_path, proposal), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('rp_id: "%s"' % nouvel_hote, text)
        self.assertEqual(self.authenticator_count(self.db), before)

    def test_base_indisponible_refus(self):
        """Base de l'équipe injoignable : refus (503), jamais une page ni un reçu."""
        action = self.propose(self.db)
        _data, path = self.a.request(action["action_id"])
        page = self.a.page(path)

        class Injoignable:
            def query(self, *_a, **_k):
                raise db_mod.Unavailable("base injoignable")
            execute = query

        dead = Injoignable()
        self.a.service.db = dead
        self.a.service.source = DbActionSource(dead)
        self.assertEqual(self.a.http("GET", path)[0], 503)
        status, result = self.a.submit(path, signed(self.alice_a, page))
        self.assertEqual(status, 503, result)
        status, _payload = self.a.api("POST", "/requests", {
            "action_id": self.propose(self.db)["action_id"], "approver": ALICE,
            "requested_by": AGENT})
        self.assertEqual(status, 503)

    # -- /health et approve-check -------------------------------------------------
    def test_health_publie_la_politique_sans_secret(self):
        status, payload = self.a.http("GET", "/health", host=self.a.local)
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertEqual(data, {
            "service": "ameesh-approve", "health": 1, "rp_id": H_A, "origins": [ORIGIN_A],
            "public_url": ORIGIN_A, "profile": "strict", "level": "standard",
            "api_via_public": False, "tls": False})
        text = payload.decode()
        for secret in (self.a.token, self.a.cfg.state_dir, self.a.token_file):
            self.assertNotIn(secret, text)
        # mêmes règles de Host que l'API ; aucun navigateur
        self.assertEqual(self.a.http("GET", "/health")[0], 421)
        self.assertEqual(self.a.http("GET", "/health", host=self.a.local,
                                     origin=ORIGIN_A)[0], 403)
        self.assertEqual(self.a.http("POST", "/health", {}, host=self.a.local)[0], 405)

    def check(self, inst: Instance, **env) -> subprocess.CompletedProcess:
        base = dict(AMEESH_APPROVE_URL="http://127.0.0.1:%d" % inst.port,
                    AMEESH_APPROVE_RP_ID=inst.host, AMEESH_APPROVE_ORIGINS=inst.origin)
        base.update(env)
        return self.mesh("approve-check", env=self.env(**{k: v for k, v in base.items()
                                                          if v is not None}))

    def test_approve_check_concordance(self):
        proc = self.check(self.a)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("politique concordante", proc.stdout)
        self.assertIn("boucle locale", proc.stdout)
        # écarts : RP ID, origine en trop, origine manquante, politique absente
        cases = (
            (dict(AMEESH_APPROVE_RP_ID=H_B), "RP ID : service"),
            (dict(AMEESH_APPROVE_ORIGINS=ORIGIN_A + "," + ORIGIN_B),
             "admise par le vérificateur mais pas par le service : " + ORIGIN_B),
            (dict(AMEESH_APPROVE_ORIGINS=ORIGIN_B), "origine du service refusée"),
            (dict(AMEESH_APPROVE_RP_ID=""), "AMEESH_APPROVE_RP_ID absent"),
        )
        for env, fragment in cases:
            with self.subTest(fragment=fragment):
                proc = self.check(self.a, **env)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("ÉCART", proc.stdout)
                self.assertIn(fragment, proc.stdout)
        # la politique de B ne concorde pas avec le service de A
        proc = self.check(self.a, AMEESH_APPROVE_RP_ID=H_B, AMEESH_APPROVE_ORIGINS=ORIGIN_B)
        self.assertEqual(proc.returncode, 1)
        proc = self.mesh("approve-check", "--json", env=self.env(
            AMEESH_APPROVE_URL="http://127.0.0.1:%d" % self.a.port,
            AMEESH_APPROVE_RP_ID=H_A, AMEESH_APPROVE_ORIGINS=ORIGIN_A))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])
        # injoignable ou non configuré : 2, sans rien écrire
        self.a.stop()
        self.assertEqual(self.check(self.a).returncode, 2)
        proc = self.check(self.a, AMEESH_APPROVE_URL="")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("AMEESH_APPROVE_URL", proc.stderr)

    def test_compare_unitaire(self):
        service = {"rp_id": H_A, "origins": [ORIGIN_A], "profile": "strict"}
        self.assertEqual(approve_check.compare(service, self.policy(H_A)), [])
        self.assertTrue(approve_check.compare(
            service, receipts.Policy(rp_id=H_A, origins=(ORIGIN_A, "https://m." + H_A))))

    # -- approve_url distincte de public_url --------------------------------------
    def test_api_sous_l_hote_public_seulement_si_ouverte(self):
        action = self.propose(self.db)
        body = {"action_id": action["action_id"], "approver": ALICE, "requested_by": AGENT}
        status, payload = self.a.api("POST", "/requests", body, host=H_A)
        self.assertEqual(status, 421)
        refusal = approve_client._refusal(status, payload)
        self.assertIn("api_via_public", refusal.message)
        opened = self.instance(self.schema, H_A, "ouverte", api_via_public=True)
        status, payload = opened.api("POST", "/requests", body, host=H_A)
        self.assertEqual(status, 201, payload)
        # toujours authentifiée, toujours sans navigateur
        self.assertEqual(opened.api("POST", "/requests", body, host=H_A, auth="x" * 43)[0],
                         401)
        self.assertEqual(opened.api("POST", "/requests", body, host=H_A,
                                    origin=ORIGIN_A)[0], 403)
        # le lien rendu reste celui de public_url, jamais celui de l'API
        self.assertTrue(json.loads(payload)["link"].startswith(ORIGIN_A + "/a/"))

    def test_client_approve_url_distincte(self):
        """La config du client ne déduit aucune URL : approve_url seule, et le
        jeton n'est envoyé ni à /health ni ailleurs qu'à l'API."""
        cfg = dataclasses.replace(self.cfg, approve_url="http://127.0.0.1:%d" % self.a.port,
                                  approve_token_file=self.a.token_file)
        self.assertEqual(approve_client.health(cfg)["rp_id"], H_A)
        cfg_b = dataclasses.replace(cfg, approve_url="http://127.0.0.1:%d" % self.b.port)
        self.assertEqual(approve_client.health(cfg_b)["rp_id"], H_B)
        action = self.propose(self.db)
        data = approve_client.create_request(cfg, action_id=action["action_id"],
                                             approver=ALICE, requested_by=AGENT)
        self.assertTrue(data["link"].startswith(ORIGIN_A + "/a/"))
        # le jeton de A vers l'API de B : refusé
        with self.assertRaises(approve_client.ApproveClientError) as caught:
            approve_client.create_request(cfg_b, action_id=action["action_id"],
                                          approver=ALICE, requested_by=AGENT)
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaisesRegex(approve_client.ApproveClientError, "TLS_NAME"):
            approve_client.health(dataclasses.replace(cfg, approve_tls_name=H_A))


# --------------------------------------------------------------------------
# TLS local (passthrough)
# --------------------------------------------------------------------------

def _openssl_cert(directory: str, host: str, tag: str) -> tuple[str, str]:
    cert = os.path.join(directory, "%s.crt" % tag)
    key = os.path.join(directory, "%s.key" % tag)
    proc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=%s" % host,
         "-addext", "subjectAltName=DNS:%s" % host],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise unittest.SkipTest("openssl ne sait pas créer le certificat de test : %s"
                                % proc.stderr[-200:])
    os.chmod(cert, 0o600)
    os.chmod(key, 0o600)
    return cert, key


@unittest.skipUnless(shutil.which("openssl"), "openssl absent : certificats de test impossibles")
class TlsLocalTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        quiet_logs(self)
        self.work = tempfile.mkdtemp(prefix="ameesh-l27-tls-")
        os.chmod(self.work, 0o700)
        self.addCleanup(shutil.rmtree, self.work, True)
        self.certs = os.path.join(self.work, "tls")
        os.makedirs(self.certs, mode=0o700)
        self.cert, self.key = _openssl_cert(self.certs, H_A, "h")

    def test_permissions_refusees(self):
        for mode in (0o644, 0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.key, mode)
                with self.assertRaisesRegex(approve_config.ApproveConfigError,
                                            "permissions trop ouvertes"):
                    TlsReloader(self.cert, self.key)
        os.chmod(self.key, 0o600)
        os.chmod(self.cert, 0o644)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "certificat"):
            TlsReloader(self.cert, self.key)
        os.chmod(self.cert, 0o600)
        link = os.path.join(self.certs, "lien.key")
        os.symlink(self.key, link)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "pas un lien"):
            TlsReloader(self.cert, link)
        other_cert, other_key = _openssl_cert(self.certs, H_A, "autre")
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "inutilisable"):
            TlsReloader(self.cert, other_key)
        self.assertEqual(TlsReloader(other_cert, other_key).loads, 1)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "vont ensemble"):
            approve_config.ApproveConfig(rp_id=H_A, origins=(ORIGIN_A,),
                                         tls_cert=self.cert).validate()

    def test_course_entre_controle_et_chargement(self):
        """Verdict codex2 (L27 B1) : entre le contrôle et le chargement, les
        chemins déposés sont remplacés par des liens vers une autre paire en
        0644, puis les inodes d'origine restaurés. Le service ne rouvre jamais
        le chemin déposé : il charge les octets lus sur le descripteur contrôlé."""
        alt_cert, alt_key = _openssl_cert(self.certs, H_A, "alt")
        os.chmod(alt_cert, 0o644)
        os.chmod(alt_key, 0o644)
        original = ssl.SSLContext.load_cert_chain
        chemins = []

        def course(ctx, certfile, keyfile=None, *args, **kwargs):
            chemins.append((certfile, keyfile))
            for path, alt in ((self.cert, alt_cert), (self.key, alt_key)):
                os.rename(path, path + ".saved")
                os.symlink(alt, path)
            try:
                return original(ctx, certfile, keyfile, *args, **kwargs)
            finally:
                for path in (self.cert, self.key):
                    os.unlink(path)
                    os.rename(path + ".saved", path)

        private = os.path.join(self.work, "etat")
        os.makedirs(private, mode=0o700)
        with unittest.mock.patch.object(ssl.SSLContext, "load_cert_chain", course):
            reloader = TlsReloader(self.cert, self.key, workdir=private)
            reloader.request_reload()
            inst = Instance(self, self.schema, H_A, workdir=os.path.join(self.work, "a"),
                            tls=reloader)
            served = self.peer(inst)       # rechargement forcé, sous la course
        self.assertEqual(reloader.loads, 2)
        for certfile, keyfile in chemins:
            self.assertNotIn(certfile, (self.cert, alt_cert))
            self.assertNotIn(keyfile, (self.key, alt_key))
            self.assertTrue(certfile.startswith(private + os.sep))
        self.assertEqual(os.listdir(private), [])          # copie privée effacée
        with open(self.cert, encoding="ascii") as fh:
            self.assertEqual(served, ssl.PEM_cert_to_DER_cert(fh.read()))
        with open(alt_cert, encoding="ascii") as fh:
            self.assertNotEqual(served, ssl.PEM_cert_to_DER_cert(fh.read()))
        # un lien déposé à la place de la clé : refusé, ancien certificat gardé
        os.rename(self.key, self.key + ".vrai")
        os.symlink(alt_key, self.key)
        self.assertEqual(self.peer(inst), served)
        with self.assertRaisesRegex(approve_config.ApproveConfigError, "lien"):
            TlsReloader(self.cert, self.key)

    def test_cli_refuse_une_cle_lisible_par_d_autres(self):
        token, _t = new_token_file(self.work)
        os.chmod(self.key, 0o640)
        proc = subprocess.run(
            [sys.executable, "-m", "ameesh.approve", "serve", "--rp-id", H_A,
             "--origin", ORIGIN_A, "--port", "0", "--token-file", token,
             "--state-dir", os.path.join(self.work, "s"),
             "--tls-cert", self.cert, "--tls-key", self.key],
            capture_output=True, text=True, timeout=60,
            env=child_env(AMEESH_APPROVE_CONFIG=os.path.join(self.work, "absent.json")))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("permissions trop ouvertes", proc.stderr)

    def peer(self, inst: Instance) -> bytes:
        """Le certificat servi maintenant (sans vérification : on le compare)."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", inst.port), timeout=15) as raw:
            with context.wrap_socket(raw, server_hostname=H_A) as sock:
                return sock.getpeercert(binary_form=True)

    def test_service_en_tls_et_rechargement_a_chaud(self):
        reloader = TlsReloader(self.cert, self.key)
        inst = Instance(self, self.schema, H_A, workdir=os.path.join(self.work, "a"),
                        tls=reloader)
        status, payload = inst.http("GET", "/health", host=inst.local, cafile=self.cert)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["tls"])
        # le certificat est vérifié pour H (et seulement H)
        with self.assertRaises(ssl.SSLCertVerificationError):
            inst.http("GET", "/health", host=inst.local, cafile=self.cert, tls_name=H_B)
        # un client HTTP en clair n'obtient rien
        conn = http.client.HTTPConnection("127.0.0.1", inst.port, timeout=5)
        with self.assertRaises((ConnectionError, http.client.HTTPException, OSError)):
            conn.request("GET", "/health", headers={"Host": inst.local})
            conn.getresponse().read()
        conn.close()
        first = self.peer(inst)

        # renouvellement : dépôt par renommage atomique, clé puis certificat
        new_cert, new_key = _openssl_cert(self.certs, H_A, "neuf")
        os.replace(new_key, self.key)
        self.peer(inst)          # entre les deux : clé ≠ certificat, l'ancien reste servi
        os.replace(new_cert, self.cert)
        second = self.peer(inst)
        self.assertNotEqual(first, second)
        with open(self.cert, "rb") as fh:
            self.assertEqual(second, ssl.PEM_cert_to_DER_cert(fh.read().decode()))
        loads = reloader.loads
        # un dépôt aux permissions trop ouvertes est refusé : certificat conservé
        os.chmod(self.cert, 0o644)
        self.assertEqual(self.peer(inst), second)
        os.chmod(self.cert, 0o600)
        # SIGHUP : relecture forcée, sans changement de fichier
        reloader.request_reload()
        self.peer(inst)
        self.assertEqual(reloader.loads, loads + 1)

    def test_client_ameesh_par_la_boucle_locale(self):
        """Exécuteur sur l'appareil : approve_url https://127.0.0.1:PORT, nom
        TLS vérifié = H ; l'API reste sous l'hôte local (pas d'api_via_public)."""
        inst = Instance(self, self.schema, H_A, workdir=os.path.join(self.work, "a"),
                        tls=TlsReloader(self.cert, self.key))
        env = self.env(AMEESH_APPROVE_URL="https://127.0.0.1:%d" % inst.port,
                       AMEESH_APPROVE_TLS_NAME=H_A, SSL_CERT_FILE=self.cert,
                       AMEESH_APPROVE_RP_ID=H_A, AMEESH_APPROVE_ORIGINS=ORIGIN_A)
        proc = self.mesh("approve-check", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TLS local", proc.stdout)
        proc = self.mesh("approve-check", env=dict(env, AMEESH_APPROVE_TLS_NAME=H_B))
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("injoignable", proc.stderr)


# --------------------------------------------------------------------------
# rôle Postgres d'approve
# --------------------------------------------------------------------------

def _dsn_for(user: str, password: str) -> str:
    from .test_cluster import _dsn_for as dsn_for
    return dsn_for(user, password)


def _contract() -> dict[str, set]:
    """Le contrat de role-approve.sql, lu dans le fichier."""
    with open(ROLE_SQL, encoding="utf-8") as fh:
        text = fh.read()
    block = text.split("INSERT INTO pg_temp.ameesh_contrat", 1)[1].split(";", 1)[0]
    return {rel: set(re.findall(r"'([a-z_]+)'", cols))
            for rel, cols in re.findall(r"\('([a-z_]+)', ARRAY\[(.*?)\]\)", block, re.S)}


class RoleApproveTest(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.role = "t_appr_%s" % suffix
        cls.login = "t_appr_login_%s" % suffix
        cls.password = secrets.token_hex(16)
        try:
            for _ in range(2):      # idempotent
                cls.apply_script(cls.role)
            q = db_mod.quote_ident
            cls.db.execute("CREATE ROLE %s LOGIN PASSWORD '%s' IN ROLE %s"
                           % (q(cls.login), cls.password, q(cls.role)))
            cls.db.execute("ALTER ROLE %s SET search_path = %s" % (q(cls.login), q(cls.schema)))
            cls.db.execute("ALTER ROLE %s SET default_transaction_read_only = on"
                           % q(cls.login))
        except Exception:
            cls.drop_roles()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.drop_roles()
        finally:
            super().tearDownClass()

    @classmethod
    def drop_roles(cls) -> None:
        for name in (cls.login, cls.role):
            if cls.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)):
                cls.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(name))
                cls.db.execute("DROP ROLE %s" % db_mod.quote_ident(name))

    @classmethod
    def run_script(cls, role: str, schema: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, PGOPTIONS="-c search_path=%s" % (schema or cls.schema))
        return subprocess.run(
            ["psql", TEST_DSN, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", "role=%s" % role,
             "-f", ROLE_SQL], capture_output=True, text=True, timeout=60, env=env)

    @classmethod
    def apply_script(cls, role: str) -> None:
        proc = cls.run_script(role)
        if proc.returncode != 0:
            raise AssertionError("role-approve.sql en échec : %s" % proc.stderr)

    def setUp(self) -> None:
        super().setUp()
        quiet_logs(self)
        self.db.execute("TRUNCATE authenticators, mesh_consumed_nonces, actions, "
                        "action_attempts, action_events RESTART IDENTITY CASCADE")

    def readable(self, role: str | None = None) -> dict[str, set]:
        rows = self.db.query(
            "SELECT c.relname AS rel, a.attname AS col "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm', 'p') "
            "AND has_column_privilege(%s, c.oid, a.attnum, 'SELECT')",
            (self.schema, role or self.role))
        out: dict[str, set] = {}
        for row in rows:
            out.setdefault(row["rel"], set()).add(row["col"])
        return out

    def login_db(self):
        cfg = dataclasses.replace(self.cfg, dsn=_dsn_for(self.login, self.password))
        db = db_mod.connect(cfg)
        self.addCleanup(db.close)
        return db

    def as_login(self, sql: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("PGPASSWORD", None)
        env.pop("PGOPTIONS", None)
        return subprocess.run(["psql", _dsn_for(self.login, self.password), "-X", "-At",
                               "-v", "ON_ERROR_STOP=1", "-c", sql],
                              capture_output=True, text=True, timeout=30, env=env)

    def test_lit_exactement_le_contrat(self):
        self.assertEqual(self.readable(), _contract())
        self.assertEqual(set(_contract()), {"actions", "authenticators",
                                            "mesh_consumed_nonces"})

    def test_le_contrat_suit_le_code(self):
        """Toute colonne que le service lit est au contrat, et réciproquement :
        une lecture ajoutée au code sans décision fait échouer ce test."""
        contract = _contract()
        actions_cols = {r["attname"] for r in self.db.query(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass('actions') "
            "AND attnum > 0 AND NOT attisdropped")}
        self.assertEqual(contract["actions"], set(DbActionSource.READ) & actions_cols)
        auth_cols = {re.match(r"\s*(?:extract\(epoch from )?([a-z_]+)", part).group(1)
                     for part in AUTH_COLUMNS.split(",")}
        self.assertEqual(contract["authenticators"], auth_cols)
        self.assertEqual(contract["mesh_consumed_nonces"],
                         {"approver", "nonce", "consumed_by", "consumed_at"})
        # jamais le matériel de reçu de l'action
        self.assertFalse({"auth_receipt", "auth_nonce", "auth_challenge", "replace_receipt",
                          "replace_nonce", "last_note"} & contract["actions"])

    def test_n_ecrit_nulle_part_et_ne_lit_rien_d_autre(self):
        for sql in ("SELECT count(*) FROM agent_mailbox",
                    "SELECT count(*) FROM agent_registry",
                    "SELECT auth_receipt FROM actions",
                    "SELECT challenge FROM mesh_consumed_nonces",
                    "SELECT count(*) FROM standing_approvals",
                    "SELECT * FROM actions",
                    "BEGIN READ WRITE; "
                    "INSERT INTO mesh_consumed_nonces (approver, nonce, consumed_by) "
                    "VALUES ('human:x', 'n', 'x')",
                    "BEGIN READ WRITE; "
                    "UPDATE authenticators SET level = 'eleve'",
                    "BEGIN READ WRITE; "
                    "UPDATE actions SET state = 'approved'"):
            with self.subTest(sql=sql):
                proc = self.as_login(sql)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("permission denied", proc.stderr)
        self.assertEqual(self.as_login("SELECT count(*) FROM authenticators").returncode, 0)

    def test_le_service_tourne_sous_ce_role(self):
        alice = SoftWebAuthn("ES256", rp_id=H_A, origin=ORIGIN_A)
        self.assertEqual(apply_authenticators(self.db, [member("alice", alice.entry())])
                         ["errors"], [])
        noop = ShellNoopConnector(os.path.join(self.tmp, "noop"), timeout=10.0)
        action = actions.propose(self.db, noop, project="demo", operation="send",
                                 target="client:42", args={"n": 1}, proposed_by=AGENT,
                                 approvers=[ALICE])
        workdir = tempfile.mkdtemp(prefix="ameesh-l27-role-")
        self.addCleanup(shutil.rmtree, workdir, True)
        db = self.login_db()
        db_mod.require_schema(db)
        token_file, token = new_token_file(workdir)
        cfg = approve_config.ApproveConfig(rp_id=H_A, origins=(ORIGIN_A,), port=0,
                                           token_file=token_file,
                                           state_dir=os.path.join(workdir, "s")).validate()
        source = DbActionSource(db)
        source.check()
        service = ApproveService(cfg, db, source, service_token=token)
        data = service.create_request({"action_id": action["action_id"], "approver": ALICE,
                                       "requested_by": AGENT})
        token_link = urlsplit(data["link"]).path.rsplit("/", 1)[1]
        view = service.link_view(token_link)
        challenge = receipts.challenge(dict(view["request"], decision="approve"))
        body = dict(alice.proof(challenge), decision="approve",
                    credential_id=alice.credential_id)
        self.assertEqual(service.submit(token_link, body)["decision"], "approve")
        status, raw = service.receipt(data["request_id"])
        self.assertEqual(status, 200)
        # l'exécuteur (son propre rôle, ici l'administrateur du banc) consomme
        verdict = receipts.verify_receipt(self.db, json.loads(raw),
                                          receipts.Policy(rp_id=H_A, origins=(ORIGIN_A,)),
                                          expected_digest=action["digest"],
                                          expected_action_id=action["action_id"],
                                          consume_by="porte")
        self.assertTrue(verdict.consumed, verdict.reason)

    def test_refus_transactionnel(self):
        s = db_mod.quote_ident(self.schema)
        self.db.execute("GRANT SELECT (body) ON %s.agent_mailbox TO PUBLIC" % s)
        neuf = "t_apprn_%s" % uuid.uuid4().hex[:8]
        try:
            avant = self.readable()
            for role in (neuf, self.role):
                proc = self.run_script(role)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("NON TENU", proc.stderr)
                self.assertIn("lecture hors contrat : agent_mailbox.body", proc.stderr)
            self.assertFalse(self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                           (neuf,)))
            self.assertEqual(self.readable(), avant)
        finally:
            self.db.execute("REVOKE SELECT (body) ON %s.agent_mailbox FROM PUBLIC" % s)
        parent = "t_apprp_%s" % uuid.uuid4().hex[:8]
        q = db_mod.quote_ident
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(parent))
        try:
            self.db.execute("GRANT %s TO %s" % (q(parent), q(self.role)))
            proc = self.run_script(self.role)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("appartenance interdite", proc.stderr)
        finally:
            self.db.execute("DROP OWNED BY %s" % q(parent))
            self.db.execute("DROP ROLE %s" % q(parent))
        self.apply_script(self.role)
        self.assertEqual(self.readable(), _contract())

    def test_refus_droits_sur_le_schema_d_une_autre_equipe(self):
        """Verdict codex2 (L27 B2) : le rôle de A a reçu USAGE et SELECT sur le
        schéma de B ; appliqué au schéma de A, le script REFUSE (rien changé,
        rien révoqué en silence)."""
        q = db_mod.quote_ident
        autre = self.schema + "_b"
        db = self.connect(autre)
        self.addCleanup(lambda: (db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % autre),
                                 db.close()))
        migrations.migrate(db, log=None)
        self.db.execute("GRANT USAGE ON SCHEMA %s TO %s" % (q(autre), q(self.role)))
        self.db.execute("GRANT SELECT ON %s.authenticators TO %s" % (q(autre), q(self.role)))
        try:
            avant = self.readable()
            proc = self.run_script(self.role)
            self.assertNotEqual(proc.returncode, 0, "installation acceptée à tort")
            self.assertIn("installation annulée", proc.stderr)
            self.assertIn("lecture hors du schéma %s : %s.authenticators"
                          % (self.schema, autre), proc.stderr)
            self.assertIn("droit accordé au rôle sur le schéma %s" % autre, proc.stderr)
            self.assertEqual(self.readable(), avant)
            self.assertTrue(self.db.query(
                "SELECT has_table_privilege(%s, %s, 'SELECT') AS ok",
                (self.role, "%s.authenticators" % q(autre)))[0]["ok"])
        finally:
            self.db.execute("REVOKE ALL ON %s.authenticators FROM %s" % (q(autre), q(self.role)))
            self.db.execute("REVOKE ALL ON SCHEMA %s FROM %s" % (q(autre), q(self.role)))
        base = self.db.query("SELECT current_database() AS d")[0]["d"]
        self.db.execute("GRANT TEMP ON DATABASE %s TO %s" % (q(base), q(self.role)))
        try:
            proc = self.run_script(self.role)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("droit accordé au rôle sur la base %s : TEMPORARY" % base,
                          proc.stderr)
        finally:
            self.db.execute("REVOKE TEMP ON DATABASE %s FROM %s" % (q(base), q(self.role)))
        self.apply_script(self.role)
        self.assertEqual(self.readable(), _contract())

    def test_refus_acl_par_defaut_d_un_autre_schema(self):
        """Verdict codex2 (L27) : droits FUTURS hors du schéma. Une ACL par
        défaut du schéma de B vers le rôle de A (sans aucune table ni USAGE
        aujourd'hui), ou vers PUBLIC dans un schéma utilisable, fait refuser."""
        q = db_mod.quote_ident
        autre = "t_appd_%s" % uuid.uuid4().hex[:8]
        self.db.execute("CREATE SCHEMA %s" % q(autre))
        try:
            for objets, priv, type_ in (("TABLES", "SELECT", "r"),
                                        ("SEQUENCES", "USAGE", "S"),
                                        ("FUNCTIONS", "EXECUTE", "f")):
                with self.subTest(objets=objets):
                    self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT %s ON %s TO %s"
                                    % (q(autre), priv, objets, q(self.role)))
                    try:
                        avant = self.readable()
                        proc = self.run_script(self.role)
                        self.assertNotEqual(proc.returncode, 0, "installation acceptée à tort")
                        self.assertIn("installation annulée", proc.stderr)
                        self.assertIn("ACL par défaut hors du schéma %s (%s, %s) : %s à %s"
                                      % (self.schema, type_, autre, priv, self.role),
                                      proc.stderr)
                        self.assertEqual(self.readable(), avant)
                    finally:
                        self.db.execute(
                            "ALTER DEFAULT PRIVILEGES IN SCHEMA %s REVOKE %s ON %s FROM %s"
                            % (q(autre), priv, objets, q(self.role)))
            # vers PUBLIC, dans un schéma utilisable par le rôle (USAGE à PUBLIC)
            self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT ON TABLES "
                            "TO PUBLIC" % q(autre))
            self.db.execute("GRANT USAGE ON SCHEMA %s TO PUBLIC" % q(autre))
            try:
                proc = self.run_script(self.role)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("ACL par défaut hors du schéma %s (r, %s) : SELECT à PUBLIC"
                              % (self.schema, autre), proc.stderr)
            finally:
                self.db.execute("REVOKE USAGE ON SCHEMA %s FROM PUBLIC" % q(autre))
                self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s REVOKE SELECT ON TABLES "
                                "FROM PUBLIC" % q(autre))
        finally:
            self.db.execute("DROP SCHEMA %s CASCADE" % q(autre))
        self.apply_script(self.role)
        self.assertEqual(self.readable(), _contract())

    def test_un_role_par_equipe(self):
        """Le rôle d'une équipe ne lit rien dans le schéma d'une autre."""
        other = self.schema + "_x"
        db = self.connect(other)
        self.addCleanup(lambda: (db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % other),
                                 db.close()))
        migrations.migrate(db, log=None)
        rows = self.db.query(
            "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
            "WHERE n.nspname = %s AND has_column_privilege(%s, c.oid, a.attnum, 'SELECT')",
            (other, self.role))
        self.assertEqual(int(rows[0]["n"]), 0)
        self.assertFalse(self.db.query("SELECT has_schema_privilege(%s, %s, 'USAGE') AS ok",
                                       (self.role, other))[0]["ok"])
