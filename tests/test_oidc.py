# SPDX-License-Identifier: AGPL-3.0-only
"""L86 (lot #377, décisions 0033 §4/0032 §2) : vérification des jetons
d'identité OIDC et rattachement au canon (`emails`, `oidc_subjects`)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import unittest
import urllib.request
from unittest import mock

from ameesh import canon, oidc, p256

from .test_canon import _TmpMixin, fiche, write
from .webauthn_soft import SoftES256Key

#: RS256 passe par l'extra `crypto` (optionnel) : sans lui, ces témoins ne
#: s'exécutent pas (la CI l'installe ; le refus explicite a son propre témoin)
_AVEC_CRYPTO = unittest.skipUnless("cryptography" in p256.available_backends(),
                                   "extra `crypto` absent (cryptography)")

ISS = "https://idp.exemple.org"
CLIENT = "ameesh-approve"
#: DigestInfo DER de SHA-256 (RFC 8017 §9.2) — la signature des tests reste
#: écrite à la main ; la vérification, elle, passe par l'extra `crypto` (L86).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


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
        t = _SHA256_PREFIX + hashlib.sha256(message).digest()
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


def jeton(cle, alg, _header=None, **claims) -> str:
    now = int(time.time())
    base = {"iss": ISS, "aud": CLIENT, "sub": "123", "iat": now, "exp": now + 600,
            "email": "alice@exemple.org", "email_verified": True}
    base.update(claims)
    base = {k: v for k, v in base.items() if v is not None}
    entete = {"alg": alg, "kid": cle.kid, "typ": "JWT"}
    entete.update(_header or {})
    head = b64u(json.dumps(entete).encode())
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

    @_AVEC_CRYPTO
    def test_rs256_et_es256(self):
        self.assertEqual(self.verifier(jeton(self.rsa, "RS256"))["sub"], "123")
        self.assertEqual(self.verifier(jeton(self.ec, "ES256"))["email"], "alice@exemple.org")
        self.assertEqual(self.fournisseur.appels, 2)          # découverte et JWKS, en cache

    @_AVEC_CRYPTO
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

    @_AVEC_CRYPTO
    def test_domaine_google_hd(self):
        claims = self.verifier(jeton(self.rsa, "RS256", hd="exemple.org",
                                     email="alice@gmail.com", email_verified=False))
        self.assertEqual(claims["hd"], "exemple.org")

    @_AVEC_CRYPTO
    def test_audiences_multiples_exigent_azp(self):
        with self.assertRaises(oidc.OidcError):
            self.verifier(jeton(self.rsa, "RS256", aud=[CLIENT, "x"]))
        self.verifier(jeton(self.rsa, "RS256", aud=[CLIENT, "x"], azp=CLIENT))

    @_AVEC_CRYPTO
    def test_nonce(self):
        token = jeton(self.rsa, "RS256", nonce="n1")
        self.verifier(token, nonce="n1")
        with self.assertRaises(oidc.OidcError):
            self.verifier(token, nonce="n2")

    @_AVEC_CRYPTO
    def test_rotation_des_cles(self):
        self.verifier(jeton(self.rsa, "RS256"))
        nouvelle = _CleEC(kid="ec2")
        self.fournisseur.cles.append(nouvelle)
        self.verifier(jeton(nouvelle, "ES256"))               # clé inconnue → relecture
        with self.assertRaises(oidc.OidcError):
            self.verifier(jeton(_CleEC(kid="fantome"), "ES256"))

    @_AVEC_CRYPTO
    def test_cle_rsa_trop_courte(self):
        self.assertFalse(oidc.rsa_verify(2 ** 1023 + 1, 65537, b"m", b"\x00" * 128))

    @_AVEC_CRYPTO
    def test_fournisseur_avec_chemin_supabase(self):
        """Un issuer qui porte un chemin (`/auth/v1`, Supabase) est suivi tel quel."""
        iss = "https://ref.supabase.co/auth/v1"
        urls = []

        def fetch(url):
            urls.append(url)
            if url.endswith("/.well-known/openid-configuration"):
                return {"issuer": iss, "jwks_uri": iss + "/jwks"}
            return {"keys": [self.rsa.jwk()]}

        p = oidc.Provider(iss, CLIENT, ("exemple.org",))
        claims = oidc.verify(jeton(self.rsa, "RS256", iss=iss), p,
                             cache=oidc.KeyCache(fetch=fetch))
        self.assertEqual(claims["iss"], iss)
        self.assertEqual(urls, [iss + "/.well-known/openid-configuration", iss + "/jwks"])

    def test_sans_extra_crypto_refus_explicite(self):
        """Sans l'extra `crypto`, RS256 est refusé, jamais servi par un repli maison."""
        import builtins
        reel = builtins.__import__

        def sans_crypto(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError("cryptography absent (essai)")
            return reel(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", new=sans_crypto):
            with self.assertRaises(oidc.OidcError) as ctx:
                oidc.rsa_verify(self.rsa.n, self.rsa.e, b"m", self.rsa.sign(b"m"))
        self.assertIn("crypto", str(ctx.exception))
        with mock.patch.object(builtins, "__import__", new=sans_crypto):
            with self.assertRaises(oidc.OidcError) as ctx:
                self.verifier(jeton(self.rsa, "RS256"))     # le message ne se perd pas
        self.assertIn("crypto", str(ctx.exception))

    def test_redirections_controlees(self):
        """R1 B2 : chaque saut est contrôlé avant émission ; pas de descente http."""
        handler = oidc._SafeRedirect()
        req = urllib.request.Request(ISS + "/jwks")
        with self.assertRaises(oidc.OidcError):                       # https -> http distant
            handler.redirect_request(req, None, 302, "Found", {},
                                     "http://203.0.113.7/jwks")
        self.assertIsNotNone(handler.redirect_request(req, None, 302, "Found", {},
                                                      ISS + "/jwks2"))     # https -> https
        local = urllib.request.Request("http://127.0.0.1:8080/realms/x/jwks")
        self.assertIsNotNone(handler.redirect_request(local, None, 302, "Found", {},
                                                      "http://127.0.0.1:8080/realms/x/2"))
        with self.assertRaises(oidc.OidcError):                       # boucle locale -> distant
            handler.redirect_request(local, None, 302, "Found", {},
                                     "http://203.0.113.7/jwks")

    def test_reponse_finale_hors_schema_refusee(self):
        """R1 B2 : même sans redirection suivie, l'URL finale est recontrôlée."""
        class Reponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def geturl(self):
                return "http://203.0.113.7/jwks"

            def read(self, n):
                return b"{}"

        class Ouvreur:
            def open(self, req, timeout=None):
                return Reponse()

        with mock.patch.object(oidc, "_OPENER", Ouvreur()):
            with self.assertRaises(oidc.OidcError):
                oidc._fetch_json(ISS + "/jwks")

    def test_metadonnees_de_cle_respectees(self):
        """R1 B3 : `use`, `key_ops`, `alg` déclarés sont respectés ; absents, acceptés."""
        base = self.ec.jwk()

        def verifier_avec(jwk):
            def fetch(url):
                if url.endswith("/.well-known/openid-configuration"):
                    return {"issuer": ISS, "jwks_uri": ISS + "/jwks"}
                return {"keys": [jwk]}
            return oidc.verify(jeton(self.ec, "ES256"), self.p,
                               cache=oidc.KeyCache(fetch=fetch))

        for champ, valeur in (("use", "enc"), ("key_ops", ["sign"]),
                              ("key_ops", "verify"), ("alg", "ES384"),
                              ("use", None), ("key_ops", None), ("alg", None)):
            with self.assertRaises(oidc.OidcError, msg="%s=%r" % (champ, valeur)):
                verifier_avec(dict(base, **{champ: valeur}))
        verifier_avec(base)                                            # métadonnées absentes
        verifier_avec(dict(base, use="sig", key_ops=["verify"], alg="ES256"))

    @_AVEC_CRYPTO
    def test_extension_critique_refusee(self):
        """R1 B4 : tout `crit` (même mal formé) refuse le jeton avant validation."""
        for crit in (["must_reject"], "must_reject", [], {}, 3):
            with self.assertRaises(oidc.OidcError, msg=repr(crit)):
                self.verifier(jeton(self.rsa, "RS256",
                                    _header={"crit": crit, "must_reject": True}))
        self.verifier(jeton(self.rsa, "RS256", _header={"typ": "JWT"}))   # sans `crit`

    def test_contrat_du_jeton_d_identite(self):
        """R1 B5 : sub/iat/exp obligatoires, nombres finis hors booléens."""
        cas = {
            "sub absent": jeton(self.rsa, "RS256", sub=None),
            "sub non chaîne": jeton(self.rsa, "RS256", sub=123),
            "sub vide": jeton(self.rsa, "RS256", sub=""),
            "iat absent": jeton(self.rsa, "RS256", iat=None),
            "iat non fini": jeton(self.rsa, "RS256", iat=float("inf")),
            "exp infini": jeton(self.rsa, "RS256", exp=float("inf")),
            "exp booléen": jeton(self.rsa, "RS256", exp=True),
            "aud non chaîne": jeton(self.rsa, "RS256", aud=123),
            "aud liste non chaîne": jeton(self.rsa, "RS256", aud=[CLIENT, 42]),
            "nbf non fini": jeton(self.rsa, "RS256", nbf=float("nan")),
        }
        for attendu, token in cas.items():
            with self.assertRaises(oidc.OidcError, msg=attendu):
                self.verifier(token)

    def test_decouverte_emetteur_exact(self):
        """R1 B1 : l'émetteur annoncé par la découverte est comparé sans normalisation."""
        def fetch(url):
            return {"issuer": ISS + "/", "jwks_uri": ISS + "/jwks"}

        with self.assertRaises(oidc.OidcError):
            oidc.KeyCache(fetch=fetch).keys(ISS)

    def test_fournisseur_types_exacts(self):
        """R1 B1 : types exigés, valeurs conservées telles quelles."""
        for raw in ({"issuer": 123, "client_id": CLIENT},
                    {"issuer": ISS, "client_id": 42},
                    {"issuer": "", "client_id": CLIENT},
                    {"issuer": "ftp://idp.example", "client_id": CLIENT}):
            with self.assertRaises(oidc.OidcError):
                oidc.Provider.from_dict(raw)
        # un `/` final n'est pas rabattu : la valeur reste telle quelle, le jeton
        # ne correspondra simplement pas (fail closed)
        self.assertEqual(oidc.Provider.from_dict(
            {"issuer": ISS + "/", "client_id": CLIENT}).issuer, ISS + "/")

    def test_fournisseur_mal_configure(self):
        for raw in ({"issuer": "http://idp", "client_id": "c"}, {"issuer": ISS}):
            with self.assertRaises(oidc.OidcError):
                oidc.Provider.from_dict(raw)


class RattachementTest(_TmpMixin, unittest.TestCase):
    def test_mode_de_rattachement(self):
        vide = type("Cfg", (), {})()
        self.assertEqual(oidc.binding(vide), "subject")                    # défaut
        self.assertEqual(oidc.binding(type("Cfg", (), {"identity": {}})()), "subject")
        self.assertEqual(
            oidc.binding(type("Cfg", (), {"identity": {"binding": "EMAIL"}})()), "email")
        with self.assertRaises(oidc.OidcError):
            oidc.binding(type("Cfg", (), {"identity": {"binding": "n'importe"}})())
        with self.assertRaises(oidc.OidcError):
            oidc.human_for([], {}, mode="n'importe")

    def test_membre_par_e_mail(self):
        root = self.example_copy()
        write(root, "membres/alice.md", fiche(type="Member", title="alice",
                                              emails=["Alice@exemple.org"]))
        c = canon.load(root, untrusted=True)
        claims = {"email": "alice@exemple.org", "email_verified": True}
        self.assertEqual(oidc.human_for([c], claims, mode=oidc.BINDING_EMAIL), "human:alice")
        self.assertIsNone(oidc.human_for([c], dict(claims, email_verified=False),
                                         mode="email"))
        self.assertIsNone(oidc.human_for([c], {"email": "x@exemple.org", "email_verified": True},
                                         mode="email"))
        self.assertIsNone(oidc.human_for([c], claims))          # défaut : le sujet, pas l'e-mail
        write(root, "membres/bruno.md", fiche(type="Member", title="bruno",
                                              emails=["alice@exemple.org"]))
        self.assertIsNone(oidc.human_for([canon.load(root, untrusted=True)], claims,
                                         mode="email"))                     # ambigu

    def test_membre_par_sujet(self):
        root = self.example_copy()
        write(root, "membres/alice.md", "\n".join([
            "---", "type: Member", "title: alice",
            "oidc_subjects:",
            '  - {issuer: "https://idp.exemple.org", subject: "sub-alice"}',
            "---", "", "# alice", ""]))
        c = canon.load(root, untrusted=True)
        claims = {"sub": "sub-alice", "email": "inconnu@exemple.org"}
        self.assertEqual(oidc.human_for([c], claims, issuer=ISS), "human:alice")
        self.assertIsNone(oidc.human_for([c], claims))                     # émetteur absent
        self.assertIsNone(oidc.human_for([c], claims, issuer="https://autre.example"))
        self.assertIsNone(oidc.human_for([c], {"sub": "sub-bruno"}, issuer=ISS))
        self.assertIsNone(oidc.human_for([c], claims, issuer=ISS, mode="email"))

    def test_sujet_et_emetteur_exacts(self):
        """R1 B1 : ni espace, ni type, ni `/` final ne sont rabattus."""
        root = self.example_copy()
        write(root, "membres/alice.md", "\n".join([
            "---", "type: Member", "title: alice",
            "oidc_subjects:",
            '  - {issuer: "https://idp.exemple.org", subject: "123"}',
            "---", "", "# alice", ""]))
        c = canon.load(root, untrusted=True)
        self.assertEqual(oidc.human_for([c], {"sub": "123"}, issuer=ISS), "human:alice")
        self.assertIsNone(oidc.human_for([c], {"sub": " 123 "}, issuer=ISS))
        self.assertIsNone(oidc.human_for([c], {"sub": 123}, issuer=ISS))
        self.assertIsNone(oidc.human_for([c], {"sub": "123"}, issuer=ISS + "/"))
        self.assertIsNone(oidc.human_for([c], {"sub": "123"},
                                         issuer=" https://idp.exemple.org"))

    def test_les_modes_ne_se_melangent_pas(self):
        root = self.example_copy()
        write(root, "membres/alice.md", fiche(type="Member", title="alice",
                                              emails=["alice@exemple.org"]))
        write(root, "membres/bruno.md", "\n".join([
            "---", "type: Member", "title: bruno",
            "oidc_subjects:",
            '  - {issuer: "https://idp.exemple.org", subject: "sub-bruno"}',
            "---", "", "# bruno", ""]))
        c = canon.load(root, untrusted=True)
        claims = {"email": "alice@exemple.org", "email_verified": True, "sub": "sub-bruno"}
        self.assertEqual(oidc.human_for([c], claims, issuer=ISS), "human:bruno")   # sujet
        self.assertEqual(oidc.human_for([c], claims, issuer=ISS, mode="email"),
                         "human:alice")                                            # e-mail

    def test_boucle_locale_http_acceptee_pour_essai(self):
        p = oidc.Provider.from_dict({"issuer": "http://127.0.0.1:8080/realms/manaty",
                                     "client_id": "c"})
        self.assertEqual(p.issuer, "http://127.0.0.1:8080/realms/manaty")
        with self.assertRaises(oidc.OidcError):
            oidc.Provider.from_dict({"issuer": "http://idp.exemple.org", "client_id": "c"})


if __name__ == "__main__":
    unittest.main()
