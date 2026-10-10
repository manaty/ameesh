# SPDX-License-Identifier: AGPL-3.0-only
"""L110 : enrôlement et identité des appareils (hôtes médiés).

Sans base : signature ES256 de l'appareil (RFC 6979), clé en 0600, formats
JOSE, occupants de la fiche Host et règle de visibilité, absence de tout
accès aux approbations. Avec base : code d'enrôlement, enrôlement, jetons
d'accès et de session, `verify_token`, révocation qui relâche les baux, CLI
`ameesh host` et enrôlement de bout en bout de l'appareil.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import re
import stat
import tempfile
import time
import unittest
from unittest import mock

from ameesh import canon, p256, visibility
from ameesh.executeur_mediee import appareil, contrat as C, identite, jose
from ameesh.executeur_mediee import interfaces as I

from . import test_canon
from .support import PgTestCase
from .test_l31_visibility import _replace

SERVER = "https://mesh.exemple"
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "ameesh")


def _key_dir() -> str:
    path = tempfile.mkdtemp(prefix="ameesh-l110-")
    os.chmod(path, 0o700)
    return os.path.join(path, "exec")


# ==========================================================================
# appareil : clé et signatures
# ==========================================================================

class SignatureTest(unittest.TestCase):
    def test_rfc6979_vecteur_p256_sha256(self):
        # RFC 6979 A.2.5, P-256, SHA-256, message "sample"
        d = int("C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721", 16)
        sig = appareil.sign_pure(d, b"sample")
        self.assertEqual(sig[:32].hex().upper(),
                         "EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716")
        self.assertEqual(sig[32:].hex().upper(),
                         "F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8")

    def test_signature_verifiee_par_p256(self):
        key = appareil.DeviceKey.generate()
        sig = key.sign(b"bonjour")
        self.assertEqual(len(sig), 64)
        self.assertTrue(jose.verify_raw(key.point, b"bonjour", sig))
        self.assertFalse(jose.verify_raw(key.point, b"bonjouR", sig))
        self.assertFalse(jose.verify_raw(key.point, b"bonjour", p256.encode_der_signature(
            int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))))

    def test_jwk_et_empreinte(self):
        key = appareil.DeviceKey.generate()
        jwk = key.public_jwk()
        self.assertEqual(jose.point_from_jwk(jwk), key.point)
        self.assertRegex(key.thumbprint(), r"^[A-Za-z0-9_-]{43}$")
        with self.assertRaises(jose.JoseError):
            jose.point_from_jwk({**jwk, "d": "AAAA"})
        with self.assertRaises(jose.JoseError):
            jose.point_from_jwk({**jwk, "y": jwk["x"]})

    def test_spki_nexlink(self):
        key = appareil.DeviceKey.generate()
        spki = jose.spki_from_point(key.point)
        self.assertEqual(len(jose.b64u(spki)), 122)  # Nexlink 05 §2
        self.assertEqual(jose.point_from_spki(spki), key.point)


class CleSurDisqueTest(unittest.TestCase):
    def test_creee_en_0600_puis_relue(self):
        directory = _key_dir()
        key, created = appareil.load_or_create_key(directory)
        self.assertTrue(created)
        path = os.path.join(directory, appareil.KEY_FILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        again, created = appareil.load_or_create_key(directory)
        self.assertFalse(created)
        self.assertEqual(again.thumbprint(), key.thumbprint())
        with open(path, encoding="ascii") as fh:
            self.assertTrue(fh.read().startswith("-----BEGIN PRIVATE KEY-----"))

    def test_droits_trop_ouverts_refuses(self):
        directory = _key_dir()
        appareil.load_or_create_key(directory)
        os.chmod(os.path.join(directory, appareil.KEY_FILE), 0o644)
        with self.assertRaises(appareil.DeviceError):
            appareil.load_key(directory)

    def test_pem_pkcs8(self):
        key = appareil.DeviceKey.generate()
        again = appareil.DeviceKey.from_pem(key.pem())
        self.assertEqual(again.point, key.point)
        with self.assertRaises(appareil.DeviceError):
            appareil.DeviceKey.from_pem("pas une clé")


class MessagesTest(unittest.TestCase):
    def test_assertion_es256(self):
        key = appareil.DeviceKey.generate()
        jws = appareil.assertion(key, executor_id="0123456789abcdef", server_url=SERVER + "/",
                                 now=1000)
        header, payload, signing_input, sig = jose.jws_parse(jws)
        self.assertEqual(header, {"alg": "ES256", "typ": "JWT", "kid": key.thumbprint()})
        self.assertEqual(payload["aud"], SERVER)
        self.assertEqual(payload["iss"], "0123456789abcdef")
        self.assertLessEqual(payload["exp"] - payload["iat"], I.ASSERTION_MAX_TTL_S)
        self.assertTrue(jose.verify_raw(key.point, signing_input, sig))

    def test_demande_d_enrolement(self):
        key = appareil.DeviceKey.generate()
        body = appareil.enroll_request(key, code="k7qf-2m9d-xw4p-8rta-j3nc-5hvb",
                                       server_url=SERVER, label="portable")
        self.assertEqual(body["schema"], C.SCHEMA_ENROLL)
        self.assertEqual(body["code"], "K7QF2M9DXW4P8RTAJ3NC5HVB")
        message = jose.enroll_proof_message(body["code"], body["public_key"], SERVER)
        self.assertTrue(jose.verify_b64(body["public_key"], message, body["proof"]))
        # même forme que le jeu doré de L107
        with open(os.path.join(os.path.dirname(__file__), "dore", "executeur_mediee",
                               "identite.json"), encoding="utf-8") as fh:
            dore = json.load(fh)
        self.assertEqual(set(body), set(dore["enroll"]["requete"]["corps"]))

    def test_defi_nexlink_ascii(self):
        key = appareil.DeviceKey.generate()
        text = appareil.attestation_challenge(key, server_url=SERVER, code="ABCD")
        lines = text.decode("ascii").split("\n")
        self.assertEqual(lines[0], jose.SCHEMA_ATTESTATION)
        self.assertEqual(lines[-1], "")
        self.assertIn("executor_thumbprint=%s" % key.thumbprint(), lines)

    def test_code_crockford(self):
        code = identite.new_code()
        self.assertRegex(code, r"^([0-9A-HJKMNP-TV-Z]{4}-){5}[0-9A-HJKMNP-TV-Z]{4}$")
        self.assertEqual(jose.normalize_code("o1l-i"), "0111")


# ==========================================================================
# occupants (0029)
# ==========================================================================

class OccupantsTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def _occupants(self, value: str) -> None:
        _replace(os.path.join(self.root, "hotes", "atelier.md"), "policy:",
                 "occupants: %s\npolicy:" % value)

    def test_sans_occupants_seuls_responsable_et_admins(self):
        loaded = canon.load(self.root, untrusted=True)
        self.assertIsNone(loaded.host("atelier").occupants)
        self.assertEqual(visibility.host_humans(loaded, "atelier"), ["human:bruno"])

    def test_occupants_comptent_dans_h_hote(self):
        self._occupants("[human:alice]")
        loaded = canon.load(self.root, untrusted=True)
        self.assertEqual(loaded.host("atelier").occupants, ["human:alice"])
        self.assertEqual(visibility.host_humans(loaded, "atelier"),
                         ["human:bruno", "human:alice"])
        self.assertEqual(loaded.to_dict()["hosts"][0]["occupants"] if
                         loaded.to_dict()["hosts"][0]["title"] == "atelier" else
                         loaded.to_dict()["hosts"][1]["occupants"], ["human:alice"])

    def test_occupant_inconnu_refuse(self):
        self._occupants("[human:enfant-inconnu]")
        loaded = canon.load(self.root, untrusted=True)
        self.assertIsNone(visibility.host_humans(loaded, "atelier"))  # fail closed
        codes = {f.code for f in canon.validate(loaded)}
        self.assertIn("host-occupant-unresolved", codes)

    def test_placement_refuse_si_un_occupant_n_a_pas_acces(self):
        from .test_l31_visibility import add_forge, add_memory, fake_gh
        add_forge(self.root, "alice", "alice-gh")
        add_forge(self.root, "bruno", "bruno-gh")
        add_memory(self.root, "orchestre")
        self._occupants("[human:alice]")
        loaded = canon.load(self.root, untrusted=True)
        ctx = visibility.decision_context(loaded, "atelier", "git@github.com:acme/m.git",
                                          visibility.Forge(mode="gh"))
        self.assertIn("human:alice", ctx["humans"])
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read", "alice-gh": "none"}))
        ok, why = forge.visible(ctx["repo"], ctx["humans"], ctx["logins"])
        self.assertFalse(ok)
        self.assertIn("alice", why)


# ==========================================================================
# 0012 : aucune route ni aucun droit vers les approbations
# ==========================================================================

AUTHORITY_TABLES = ("mesh_approvals", "approval", "nonce", "grant", "authenticator",
                    "standing", "receipt")


class JamaisDApprobationTest(unittest.TestCase):
    def test_aucune_table_d_autorite_dans_le_code_l110(self):
        files = [os.path.join(SRC, "executeur_mediee", n)
                 for n in ("identite.py", "appareil.py", "jose.py")]
        files.append(os.path.join(SRC, "migrations", "0110_identite_des_executeurs.sql"))
        for path in files:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            # seules les lignes de SQL comptent (pas les commentaires ni les
            # docstrings qui rappellent la règle)
            if path.endswith(".sql"):
                sql = [ln for ln in text.splitlines()
                       if ln.strip() and not ln.strip().startswith("--")]
            else:
                sql = [ln for ln in text.splitlines()
                       if re.search(r"\b(SELECT|FROM|JOIN|INTO|UPDATE|DELETE)\b", ln)]
            if path.endswith(("identite.py", ".sql")):
                self.assertTrue(sql, path)
            for line in sql:
                for word in AUTHORITY_TABLES:
                    with self.subTest(path=os.path.basename(path), word=word):
                        self.assertNotIn(word, line.lower(), line)

    def test_le_contrat_refuse_toute_operation_d_autorite(self):
        contract = C.load()
        admitted = set(contract.operations)
        for domain in ("approvals", "nonces", "grants", "authenticators"):
            self.assertIn(domain, contract.refused)
        for name in admitted:
            domain, method = name.split(".", 1)
            with self.subTest(op=name):
                self.assertNotIn(domain, ("approvals", "nonces", "grants", "authenticators",
                                          "action_source"))
                if domain == "actions":
                    self.assertNotIn(method, ("bind", "launch", "settle", "replace", "cancel"))

    def test_aucun_droit_d_approbation_dans_l_identite(self):
        fields = {f.name for f in dataclasses.fields(I.Principal)}
        self.assertEqual(fields, {"kind", "executor_id", "mesh", "host", "agent", "epoch",
                                  "agents_allowlist", "expires_ts"})
        public = {n for n, v in inspect.getmembers(identite.DbIdentityProvider)
                  if not n.startswith("_") and callable(v)}
        self.assertFalse({n for n in public if "approv" in n or "grant" in n})
        # deux sortes de jetons, et seulement deux
        self.assertEqual(identite._TOKEN_RE.pattern.split("\\.")[0], "^(amx1|ams1)")


# ==========================================================================
# serveur (base)
# ==========================================================================

def _post_to(provider, server_url=SERVER):
    """Faux transport : route `POST /enroll` et `/token` vers le fournisseur."""
    def post(url, headers, body, timeout):
        route = url[len(server_url) + len(appareil.API_PREFIX):]
        payload = json.loads(body)
        try:
            if route == "/enroll":
                return 201, provider.enroll(payload, server_url=server_url)
            if route == "/token":
                return 200, provider.issue_access_token(payload["assertion"],
                                                        server_url=server_url).to_json()
        except I.AuthError as exc:
            return C.ERRORS[exc.code].status, C.error_body(exc.code, str(exc))
        return 404, C.error_body("op_not_allowed", route)
    return post


class IdentiteBaseTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE executor_events, executor_assertion_jti, executor_tokens, "
                        "executors, executor_invitations RESTART IDENTITY CASCADE")
        self.provider = identite.DbIdentityProvider(self.db, mesh="mesh-exemple")

    # -- utilitaires --------------------------------------------------------
    def invite(self, host="anna-portable", agents=None, ttl=900):
        return identite.create_invitation(self.db, mesh="mesh-exemple", host=host,
                                          created_by="human:bruno", agents=agents, ttl_s=ttl)

    def enrolled(self, host="anna-portable", agents=None):
        key = appareil.DeviceKey.generate()
        code = self.invite(host, agents)["code"]
        reply = self.provider.enroll(appareil.enroll_request(key, code=code, server_url=SERVER),
                                     server_url=SERVER)
        return key, reply["executor_id"]

    def access(self, key, executor_id, now=None):
        jws = appareil.assertion(key, executor_id=executor_id, server_url=SERVER, now=now)
        return self.provider.issue_access_token(jws, server_url=SERVER)

    def lease(self, agent, executor_id, host="anna-portable", epoch=7, ttl=90):
        owner = C.owner_for(executor_id, host, 4121)
        self.db.execute(
            "INSERT INTO agent_registry (name, host, status, lease_owner, lease_epoch, "
            "lease_expires_at) VALUES (%s, %s, 'running', %s, %s, "
            "now() + make_interval(secs => %s)) ON CONFLICT (name) DO UPDATE SET "
            "host = excluded.host, status = excluded.status, lease_owner = excluded.lease_owner, "
            "lease_epoch = excluded.lease_epoch, lease_expires_at = excluded.lease_expires_at",
            (agent, host, owner, epoch, ttl))
        return C.Fence(agent, owner, epoch)

    def assertAuth(self, code, fn, *args, **kwargs):
        with self.assertRaises(I.AuthError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    # -- enrôlement ---------------------------------------------------------
    def test_enrolement(self):
        key = appareil.DeviceKey.generate()
        invitation = self.invite(agents=["inge-front"])
        self.assertNotIn(invitation["code"],
                         json.dumps(self.db.query("SELECT * FROM executor_invitations")))
        reply = self.provider.enroll(
            appareil.enroll_request(key, code=invitation["code"].lower(), server_url=SERVER),
            server_url=SERVER)
        self.assertEqual((reply["mesh"], reply["host"]), ("mesh-exemple", "anna-portable"))
        self.assertRegex(reply["executor_id"], r"^[0-9a-f]{16}$")
        row = self.db.query("SELECT thumbprint, agents_allowlist, enrolled_by FROM executors")[0]
        self.assertEqual(row["thumbprint"], key.thumbprint())
        self.assertEqual(row["enrolled_by"], "human:bruno")
        # usage unique
        other = appareil.DeviceKey.generate()
        self.assertAuth("token_invalid", self.provider.enroll,
                        appareil.enroll_request(other, code=invitation["code"], server_url=SERVER),
                        server_url=SERVER)

    def test_code_echu_faux_ou_autre_mesh(self):
        key = appareil.DeviceKey.generate()
        code = self.invite(ttl=1)["code"]
        self.db.execute("UPDATE executor_invitations SET expires_at = now() - interval '1 s', "
                        "created_at = now() - interval '1 h'")
        self.assertAuth("token_invalid", self.provider.enroll,
                        appareil.enroll_request(key, code=code, server_url=SERVER),
                        server_url=SERVER)
        self.assertAuth("token_invalid", self.provider.enroll,
                        appareil.enroll_request(key, code=identite.new_code(), server_url=SERVER),
                        server_url=SERVER)
        code = identite.create_invitation(self.db, mesh="autre-mesh", host="anna-portable",
                                          created_by="human:bruno")["code"]
        self.assertAuth("token_invalid", self.provider.enroll,
                        appareil.enroll_request(key, code=code, server_url=SERVER),
                        server_url=SERVER)

    def test_preuve_fausse_ou_autre_serveur(self):
        key = appareil.DeviceKey.generate()
        code = self.invite()["code"]
        body = appareil.enroll_request(key, code=code, server_url="https://ailleurs.exemple")
        self.assertAuth("token_invalid", self.provider.enroll, body, server_url=SERVER)
        body = appareil.enroll_request(key, code=code, server_url=SERVER)
        body["public_key"] = appareil.DeviceKey.generate().public_jwk()
        self.assertAuth("token_invalid", self.provider.enroll, body, server_url=SERVER)
        # le code n'a pas été consommé par les essais ratés
        self.provider.enroll(appareil.enroll_request(key, code=code, server_url=SERVER),
                             server_url=SERVER)

    def test_liaison_a_la_cle_d_appareil_nexlink(self):
        key = appareil.DeviceKey.generate()
        device = appareil.DeviceKey.generate()  # la clé d'appareil Nexlink, simulée
        code = self.invite()["code"]
        challenge = appareil.attestation_challenge(key, server_url=SERVER, code=code)
        attestation = {"schema": jose.SCHEMA_ATTESTATION,
                       "device_public_key": jose.b64u(jose.spki_from_point(device.point)),
                       "signature": device.sign_b64(challenge)}
        bad = dict(attestation, signature=device.sign_b64(b"autre chose"))
        self.assertAuth("token_invalid", self.provider.enroll,
                        appareil.enroll_request(key, code=code, server_url=SERVER,
                                                device_attestation=bad), server_url=SERVER)
        self.provider.enroll(appareil.enroll_request(key, code=code, server_url=SERVER,
                                                     device_attestation=attestation),
                             server_url=SERVER)
        row = self.db.query("SELECT device_key_sha256 FROM executors")[0]
        self.assertEqual(row["device_key_sha256"],
                         hashlib.sha256(jose.spki_from_point(device.point)).hexdigest())

    # -- jetons -------------------------------------------------------------
    def test_jeton_d_acces(self):
        key, ident = self.enrolled()
        issued = self.access(key, ident)
        self.assertTrue(issued.token.startswith(I.ACCESS_TOKEN_PREFIX))
        self.assertRegex(issued.token, r"^amx1\.[A-Za-z0-9_-]{43}$")
        self.assertAlmostEqual(issued.expires_ts - time.time(), I.ACCESS_TOKEN_TTL_S, delta=30)
        self.assertEqual(issued.to_json()["schema"], C.SCHEMA_TOKEN)
        principal = identite.verify_token(self.db, issued.token)
        self.assertEqual((principal.kind, principal.executor_id, principal.host, principal.mesh),
                         ("executor", ident, "anna-portable", "mesh-exemple"))
        stored = self.db.query("SELECT token_sha256 FROM executor_tokens")[0]["token_sha256"]
        self.assertEqual(stored, hashlib.sha256(issued.token.encode()).hexdigest())
        self.assertAuth("token_invalid", identite.verify_token, self.db, issued.token,
                        kind="session")
        self.assertAuth("token_invalid", identite.verify_token, self.db, "amx1." + "A" * 43)
        self.assertAuth("token_invalid", identite.verify_token, self.db, "Bearer x")

    def test_assertion_rejouee_ou_hors_regles(self):
        key, ident = self.enrolled()
        jws = appareil.assertion(key, executor_id=ident, server_url=SERVER)
        self.provider.issue_access_token(jws, server_url=SERVER)
        self.assertAuth("token_invalid", self.provider.issue_access_token, jws, server_url=SERVER)
        jws = appareil.assertion(key, executor_id=ident, server_url="https://autre.exemple")
        self.assertAuth("token_invalid", self.provider.issue_access_token, jws, server_url=SERVER)
        jws = appareil.assertion(key, executor_id=ident, server_url=SERVER, now=time.time() - 300)
        self.assertAuth("token_expired", self.provider.issue_access_token, jws, server_url=SERVER)
        # durée de plus de 60 s, signée en règle
        header = {"alg": "ES256", "typ": "JWT", "kid": key.thumbprint()}
        now = int(time.time())
        payload = {"iss": ident, "aud": SERVER, "iat": now, "exp": now + 600, "jti": "j" * 16}
        si = jose.jws_signing_input(header, payload)
        self.assertAuth("token_invalid", self.provider.issue_access_token,
                        "%s.%s" % (si.decode(), key.sign_b64(si)), server_url=SERVER)
        # signée par une autre clé
        other = appareil.DeviceKey.generate()
        self.assertAuth("token_invalid", self.provider.issue_access_token,
                        appareil.assertion(other, executor_id=ident, server_url=SERVER),
                        server_url=SERVER)

    def test_jeton_echu(self):
        key, ident = self.enrolled()
        issued = self.access(key, ident)
        self.db.execute("UPDATE executor_tokens SET expires_at = now() - interval '1 s'")
        self.assertAuth("token_expired", identite.DbIdentityProvider(self.db).verify,
                        issued.token)

    def test_jeton_de_session_lie_au_bail(self):
        key, ident = self.enrolled(agents=["inge-front"])
        principal = self.provider.verify(self.access(key, ident).token)
        fence = self.lease("inge-front", ident)
        session = self.provider.issue_session_token(principal, fence)
        self.assertRegex(session.token, r"^ams1\.[A-Za-z0-9_-]{43}$")
        self.assertEqual(session.to_json()["schema"], C.SCHEMA_SESSION_TOKEN)
        seen = identite.verify_token(self.db, session.token, kind="session")
        self.assertEqual((seen.kind, seen.agent, seen.epoch), ("session", "inge-front", 7))
        self.assertAuth("token_invalid", identite.verify_token, self.db, session.token,
                        kind="executor")
        # hors liste blanche, autre exécuteur, bail perdu : refus de portée
        self.lease("autre-agent", ident)
        with self.assertRaises(I.ScopeError):
            self.provider.issue_session_token(principal, C.Fence(
                "autre-agent", C.owner_for(ident, "anna-portable", 4121), 7))
        with self.assertRaises(I.ScopeError):
            self.provider.issue_session_token(principal, C.Fence(
                "inge-front", "exec:ffffffffffffffff:anna-portable:1", 7))
        with self.assertRaises(I.ScopeError):
            self.provider.issue_session_token(principal, dataclasses.replace(fence, epoch=6))
        with self.assertRaises(I.AuthError):
            self.provider.issue_session_token(seen, fence)  # un jeton de session n'en émet pas
        # l'epoch change : le jeton de session meurt (après le cache ≤ 5 s)
        self.db.execute("UPDATE agent_registry SET lease_epoch = 8 WHERE name = 'inge-front'")
        self.assertAuth("token_expired", identite.DbIdentityProvider(self.db).verify,
                        session.token)

    def test_cache_borne_a_cinq_secondes(self):
        key, ident = self.enrolled()
        token = self.access(key, ident).token
        provider = identite.DbIdentityProvider(self.db)
        provider.verify(token)
        self.db.execute("UPDATE executors SET revoked_at = now()")
        provider.verify(token)  # encore en cache
        with mock.patch.object(identite.time, "monotonic",
                               return_value=time.monotonic() + I.EXECUTOR_STATE_CACHE_S + 0.1):
            self.assertAuth("executor_revoked", provider.verify, token)

    # -- révocation ---------------------------------------------------------
    def test_revocation_relache_les_baux_et_refuse_les_jetons(self):
        key, ident = self.enrolled()
        token = self.access(key, ident).token
        principal = self.provider.verify(token)
        fence = self.lease("inge-front", ident)
        session = self.provider.issue_session_token(principal, fence).token
        self.lease("hors-executeur", "0000000000000000", host="autre")
        released = self.provider.revoke(ident, by="human:bruno", why="appareil rendu")
        self.assertEqual(released, 1)
        row = self.db.query("SELECT lease_owner, lease_epoch, status FROM agent_registry "
                            "WHERE name = 'inge-front'")[0]
        self.assertEqual((row["lease_owner"], row["lease_epoch"], row["status"]),
                         (None, 8, "idle"))
        other = self.db.query("SELECT lease_owner FROM agent_registry "
                              "WHERE name = 'hors-executeur'")[0]
        self.assertIsNotNone(other["lease_owner"])
        self.assertAuth("token_invalid", self.provider.verify, token)  # jetons effacés
        self.assertAuth("token_invalid", self.provider.verify, session)
        self.assertAuth("executor_revoked", self.provider.issue_access_token,
                        appareil.assertion(key, executor_id=ident, server_url=SERVER),
                        server_url=SERVER)
        kinds = [e["kind"] for e in identite.events(self.db, "anna-portable")]
        self.assertEqual(kinds, ["invitation", "enrolled", "revoked"])

    def test_revoke_host_annule_les_codes(self):
        key, ident = self.enrolled()
        self.invite()
        result = identite.revoke_host(self.db, "anna-portable", by="human:bruno", why="fin")
        self.assertEqual(result["executors"], [ident])
        self.assertEqual(result["invitations_cancelled"], 1)
        self.assertEqual(identite.pending_invitations(self.db), [])
        self.assertEqual(identite.list_executors(self.db)[0]["state"], "révoqué")

    def test_l_identite_ne_touche_pas_aux_approbations(self):
        before = self.db.query("SELECT count(*) AS n FROM mesh_approvals")[0]["n"]
        key, ident = self.enrolled()
        self.provider.revoke(ident, by="human:bruno", why="essai")
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM mesh_approvals")[0]["n"], before)
        refs = self.db.query(
            "SELECT conrelid::regclass::text AS src, confrelid::regclass::text AS dst "
            "FROM pg_constraint WHERE contype = 'f' "
            "AND starts_with(conrelid::regclass::text, 'executor')")
        self.assertTrue(refs)
        self.assertEqual({r["dst"].split(".")[-1].strip('"') for r in refs}, {"executors"})

    # -- appareil, de bout en bout -----------------------------------------
    def test_appareil_de_bout_en_bout(self):
        directory = _key_dir()
        code = self.invite()["code"]
        post = _post_to(self.provider)
        state = appareil.enroll(SERVER, code, directory=directory, label="VM", post=post)
        self.assertEqual(state["host"], "anna-portable")
        st = os.stat(os.path.join(directory, appareil.STATE_FILE))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        with self.assertRaises(appareil.DeviceError):  # pas de ré-enrôlement implicite
            appareil.enroll(SERVER, code, directory=directory, post=post)
        source = appareil.HttpTokenSource.from_device(directory, post=post)
        first = source.access_token()
        self.assertIs(source.access_token(), first)  # en cache
        second = source.access_token(refresh=True)
        self.assertNotEqual(first, second)
        self.assertEqual(identite.verify_token(self.db, second).executor_id,
                         state["executor_id"])
        self.provider.revoke(state["executor_id"], by="human:bruno", why="fin")
        with self.assertRaises(C.ExecutorRevoked):
            source.access_token(refresh=True)


class DeviceCliTest(PgTestCase):
    """La vraie commande `ameesh device enroll --code-file` : le code n'est
    ni dans la ligne de commande, ni dans l'environnement, ni dans la sortie."""

    invite = IdentiteBaseTest.invite

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE executor_events, executor_assertion_jti, executor_tokens, "
                        "executors, executor_invitations RESTART IDENTITY CASCADE")
        self.provider = identite.DbIdentityProvider(self.db, mesh="mesh-exemple")

    def _serve(self, seen):
        import http.server
        import threading
        provider = self.provider
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                pid = test.proc.pid
                for name in ("cmdline", "environ"):
                    with open("/proc/%d/%s" % (pid, name), "rb") as fh:
                        seen[name] = fh.read()
                try:
                    status, reply = 201, provider.enroll(body, server_url=test.url)
                except I.AuthError as exc:
                    status, reply = 401, C.error_body(exc.code, str(exc))
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def _enroll(self, *source, stdin=None):
        import subprocess
        import sys
        seen: dict = {}
        self._serve(seen)
        code = self.invite()["code"]
        home = _key_dir()
        argv = [sys.executable, "-m", "ameesh.main", "device", "enroll", "--server", self.url,
                "--home", home, *source]
        if source[-1] != "-":
            with open(source[-1], "w", encoding="ascii") as fh:
                fh.write(code + "\n")
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=self.env(),
                                     cwd=self.tmp)
        out, err = self.proc.communicate(code + "\n" if source[-1] == "-" else "", timeout=60)
        self.assertEqual(self.proc.returncode, 0, err)
        self.assertIn("appareil enrôlé", out)
        self.assertTrue(seen, "le serveur n'a rien reçu")
        for variant in (code, code.replace("-", ""), jose.normalize_code(code)):
            for name, raw in seen.items():
                with self.subTest(where=name):
                    self.assertNotIn(variant.encode(), raw)
            self.assertNotIn(variant, out + err)
        with open(os.path.join(home, appareil.STATE_FILE), encoding="utf-8") as fh:
            self.assertNotIn(jose.normalize_code(code), fh.read())

    def test_code_file(self):
        path = os.path.join(self.tmp, "code.txt")
        self._enroll("--code-file", path)

    def test_code_sur_l_entree_standard(self):
        self._enroll("--code-file", "-")

    def test_code_et_code_file_exclusifs(self):
        for args in ([], ["--code", "X", "--code-file", "-"]):
            proc = self.mesh("device", "enroll", "--server", SERVER, *args)
            self.assertEqual(proc.returncode, 2, proc.stderr)
        import subprocess
        import sys
        proc = subprocess.run([sys.executable, "-m", "ameesh.main", "device", "challenge",
                               "--server", SERVER, "--home", _key_dir(), "--code-file", "-"],
                              input="", capture_output=True, text=True, env=self.env(),
                              cwd=self.tmp, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr)  # entrée standard vide : refus
        self.assertIn("vide", proc.stderr)


# ==========================================================================
# CLI
# ==========================================================================

class HostCliTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE executor_events, executor_assertion_jti, executor_tokens, "
                        "executors, executor_invitations RESTART IDENTITY CASCADE")
        mixin = test_canon._TmpMixin()
        mixin.addCleanup = self.addCleanup
        mixin.skipTest = self.skipTest
        self.root = mixin.example_copy()

    def host(self, *args, **env):
        return self.mesh("host", *args, env=self.env(AMEESH_CANON=self.root,
                                                     AMEESH_CANON_UNTRUSTED="1", **env))

    def test_enroll_par_le_responsable(self):
        proc = self.host("enroll", "atelier", "--by", "human:bruno", "--mesh", "mesh-exemple",
                         "--agents", "orchestre", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertRegex(out["code"], r"^([0-9A-Z]{4}-){5}[0-9A-Z]{4}$")
        self.assertEqual((out["host"], out["mesh"], out["agents"]),
                         ("atelier", "mesh-exemple", ["orchestre"]))
        self.assertEqual(out["authorized_as"], "responsable de l'hôte")
        listing = json.loads(self.host("list", "--json").stdout)
        self.assertEqual(len(listing["invitations"]), 1)
        self.assertNotIn(out["code"], json.dumps(listing))

    def test_jamais_depuis_une_session_d_agent(self):
        for var in ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AMEESH_EXEC_TOKEN"):
            with self.subTest(var=var):
                proc = self.host("enroll", "atelier", "--by", "human:bruno",
                                 **{var: "orchestre"})
                self.assertEqual(proc.returncode, 3, proc.stdout)
                self.assertIn("session d'agent", proc.stderr)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM executor_invitations")[0]["n"], 0)

    def test_humain_non_habilite_ou_agent_non_admis(self):
        proc = self.host("enroll", "atelier", "--by", "human:alice")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("ni le responsable", proc.stderr)
        proc = self.host("enroll", "atelier", "--by", "agent:orchestre")
        self.assertEqual(proc.returncode, 3)
        proc = self.host("enroll", "nulle-part", "--by", "human:bruno")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("sans fiche Host", proc.stderr)
        proc = self.host("enroll", "atelier", "--by", "human:bruno", "--agents", "ouvrier")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("non admis", proc.stderr)
        proc = self.host("enroll", "atelier", "--by", "human:bruno", "--ttl", "2h")
        self.assertEqual(proc.returncode, 3)
        # coordinateur du mesh : admis
        _replace(os.path.join(self.root, "membres", "alice.md"),
                 "roles: [project-lead, reviewer]", "roles: [project-lead, coordinateur]")
        proc = self.host("enroll", "atelier", "--by", "human:alice", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["authorized_as"], "coordinateur du mesh")

    def test_revoke_list_show(self):
        code = json.loads(self.host("enroll", "atelier", "--by", "human:bruno", "--mesh",
                                    "mesh-exemple", "--json").stdout)["code"]
        provider = identite.DbIdentityProvider(self.db)
        key = appareil.DeviceKey.generate()
        ident = provider.enroll(appareil.enroll_request(key, code=code, server_url=SERVER),
                                server_url=SERVER)["executor_id"]
        self.db.execute(
            "INSERT INTO agent_registry (name, host, status, lease_owner, lease_epoch, "
            "lease_expires_at) VALUES ('orchestre', 'atelier', 'running', %s, 3, "
            "now() + interval '90 s')", (C.owner_for(ident, "atelier", 99),))
        show = json.loads(self.host("show", "atelier", "--json").stdout)
        self.assertEqual(show["executors"][0]["id"], ident)
        self.assertEqual([l["name"] for l in show["leases"]], ["orchestre"])
        self.assertIsNone(show["fiche"]["occupants"])
        text = self.host("show", "atelier").stdout
        self.assertIn("aucun déclaré", text)
        proc = self.host("revoke", "atelier", "--by", "human:alice", "--why", "x")
        self.assertEqual(proc.returncode, 3)
        proc = self.host("revoke", "atelier", "--by", "human:bruno", "--why", "rendu", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["executors"], out["released"]), ([ident], ["orchestre"]))
        listing = json.loads(self.host("list", "--json").stdout)
        self.assertEqual(listing["executors"][0]["state"], "révoqué")
        text = self.host("list").stdout
        self.assertIn(ident, text)


if __name__ == "__main__":
    unittest.main()
