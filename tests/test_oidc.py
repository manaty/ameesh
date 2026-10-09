# SPDX-License-Identifier: AGPL-3.0-only
"""Étude v2 D1 (0033 §4) : vérification des jetons d'identité OIDC."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import unittest

from ameesh import canon, oidc, p256

from .test_canon import _TmpMixin, fiche, write
from .webauthn_soft import SoftES256Key

ISS = "https://idp.exemple.org"
CLIENT = "ameesh-approve"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _premier(bits: int) -> int:
    def probable(n: int) -> bool:
        if n % 2 == 0:
            return False
        d, r = n - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(24):
            x = pow(secrets.randbelow(n - 3) + 2, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                return False
        return True
    while True:
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1   # deux bits hauts : p*q fait bien 2*bits
        if all(c % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and probable(c):
            return c


class _CleRSA:
    """Clé RSA 2048 jetable, signature RS256 (tests seulement)."""
    def __init__(self, kid="rsa1"):
        e = 65537
        while True:
            p, q = _premier(1024), _premier(1024)
            phi = (p - 1) * (q - 1)
            if p != q and phi % e:
                break
        self.n, self.e, self.d, self.kid = p * q, e, pow(e, -1, phi), kid

    def jwk(self):
        k = (self.n.bit_length() + 7) // 8
        return {"kty": "RSA", "kid": self.kid, "alg": "RS256",
                "n": b64u(self.n.to_bytes(k, "big")), "e": b64u(self.e.to_bytes(3, "big"))}

    def sign(self, message: bytes) -> bytes:
        k = (self.n.bit_length() + 7) // 8
        t = oidc._SHA256_PREFIX + hashlib.sha256(message).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")


class _CleEC:
    def __init__(self, kid="ec1"):
        self.key, self.kid = SoftES256Key(), kid

    def jwk(self):
        x, y = self.key.point
        return {"kty": "EC", "crv": "P-256", "kid": self.kid,
                "x": b64u(x.to_bytes(32, "big")), "y": b64u(y.to_bytes(32, "big"))}

    def sign(self, message: bytes) -> bytes:
        r, s = p256.decode_der_signature(self.key.sign(message))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def jeton(cle, alg, **claims) -> str:
    now = int(time.time())
    base = {"iss": ISS, "aud": CLIENT, "sub": "123", "iat": now, "exp": now + 600,
            "email": "alice@exemple.org", "email_verified": True}
    base.update(claims)
    base = {k: v for k, v in base.items() if v is not None}
    head = b64u(json.dumps({"alg": alg, "kid": cle.kid, "typ": "JWT"}).encode())
    body = b64u(json.dumps(base).encode())
    sig = cle.sign((head + "." + body).encode())
    return head + "." + body + "." + b64u(sig)


class _Fournisseur:
    def __init__(self, *cles):
        self.cles = list(cles)
        self.appels = 0

    def __call__(self, url):
        self.appels += 1
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISS, "jwks_uri": ISS + "/jwks"}
        return {"keys": [c.jwk() for c in self.cles]}


class VerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rsa = _CleRSA()
        cls.ec = _CleEC()

    def setUp(self):
        self.fournisseur = _Fournisseur(self.rsa, self.ec)
        self.cache = oidc.KeyCache(fetch=self.fournisseur)
        self.p = oidc.Provider(ISS, CLIENT, ("exemple.org",))

    def verifier(self, token, **kw):
        return oidc.verify(token, self.p, cache=self.cache, **kw)

    def test_rs256_et_es256(self):
        self.assertEqual(self.verifier(jeton(self.rsa, "RS256"))["sub"], "123")
        self.assertEqual(self.verifier(jeton(self.ec, "ES256"))["email"], "alice@exemple.org")
        self.assertEqual(self.fournisseur.appels, 2)          # découverte et JWKS, en cache

    def test_refus(self):
        cas = {
            "signature invalide": jeton(self.rsa, "RS256")[:-6] + "AAAAAA",
            "expiré": jeton(self.rsa, "RS256", exp=int(time.time()) - 3600),
            "émetteur inattendu": jeton(self.rsa, "RS256", iss="https://autre.example"),
            "autre application": jeton(self.rsa, "RS256", aud="autre-client"),
            "hors de l'organisation": jeton(self.rsa, "RS256", email="eve@ailleurs.example"),
            "non vérifié": jeton(self.rsa, "RS256", email_verified=False),
            "sans échéance": jeton(self.rsa, "RS256", exp=None),
        }
        for attendu, token in cas.items():
            with self.assertRaises(oidc.OidcError, msg=attendu) as ctx:
                self.verifier(token)
            self.assertIn(attendu, str(ctx.exception), attendu)

    def test_algorithme_none_ou_hs256_refuse(self):
        for alg in ("none", "HS256"):
            head = b64u(json.dumps({"alg": alg, "kid": "rsa1"}).encode())
            body = b64u(json.dumps({"iss": ISS, "aud": CLIENT, "exp": time.time() + 60}).encode())
            with self.assertRaises(oidc.OidcError):
                self.verifier(head + "." + body + "." + b64u(b"x"))

    def test_domaine_google_hd(self):
        claims = self.verifier(jeton(self.rsa, "RS256", hd="exemple.org",
                                     email="alice@gmail.com", email_verified=False))
        self.assertEqual(claims["hd"], "exemple.org")

    def test_audiences_multiples_exigent_azp(self):
        with self.assertRaises(oidc.OidcError):
            self.verifier(jeton(self.rsa, "RS256", aud=[CLIENT, "x"]))
        self.verifier(jeton(self.rsa, "RS256", aud=[CLIENT, "x"], azp=CLIENT))

    def test_nonce(self):
        token = jeton(self.rsa, "RS256", nonce="n1")
        self.verifier(token, nonce="n1")
        with self.assertRaises(oidc.OidcError):
            self.verifier(token, nonce="n2")

    def test_rotation_des_cles(self):
        self.verifier(jeton(self.rsa, "RS256"))
        nouvelle = _CleEC(kid="ec2")
        self.fournisseur.cles.append(nouvelle)
        self.verifier(jeton(nouvelle, "ES256"))               # clé inconnue → relecture
        with self.assertRaises(oidc.OidcError):
            self.verifier(jeton(_CleEC(kid="fantome"), "ES256"))

    def test_cle_rsa_trop_courte(self):
        self.assertFalse(oidc.rsa_verify(2 ** 1023 + 1, 65537, b"m", b"\x00" * 128))

    def test_fournisseur_mal_configure(self):
        for raw in ({"issuer": "http://idp", "client_id": "c"}, {"issuer": ISS}):
            with self.assertRaises(oidc.OidcError):
                oidc.Provider.from_dict(raw)


class RattachementTest(_TmpMixin, unittest.TestCase):
    def test_membre_par_e_mail(self):
        root = self.example_copy()
        write(root, "membres/alice.md", fiche(type="Member", title="alice",
                                              emails=["Alice@exemple.org"]))
        c = canon.load(root, untrusted=True)
        claims = {"email": "alice@exemple.org", "email_verified": True}
        self.assertEqual(oidc.human_for([c], claims), "human:alice")
        self.assertIsNone(oidc.human_for([c], dict(claims, email_verified=False)))
        self.assertIsNone(oidc.human_for([c], {"email": "x@exemple.org", "email_verified": True}))
        write(root, "membres/bruno.md", fiche(type="Member", title="bruno",
                                              emails=["alice@exemple.org"]))
        self.assertIsNone(oidc.human_for([canon.load(root, untrusted=True)], claims))   # ambigu


if __name__ == "__main__":
    unittest.main()
