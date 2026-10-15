# SPDX-License-Identifier: AGPL-3.0-only
"""Identité des humains par OIDC (L86, lot #377 ; décisions 0033 §4, 0032 §2).

ameesh accepte un fournisseur d'identité OIDC **générique**, choisi par
l'organisation (Google restreint au domaine de l'organisation, Keycloak,
Supabase Auth…). Le fournisseur dit *qui* est l'humain ; le canon dit *ce
qu'il est* dans l'organisation. Ce module :

* vérifie un **jeton d'identité** (ID token, JWT signé) : signature RS256 ou
  ES256 contre les clés publiées par le fournisseur (découverte OIDC, puis
  JWKS, en cache), émetteur, audience (`azp` si plusieurs audiences),
  échéances (`exp`, `nbf`, `iat`, avec une tolérance), `nonce` s'il est
  attendu, et la **restriction de domaine** de l'organisation (`hd` de Google,
  sinon domaine de l'e-mail, avec `email_verified`) ;
* rattache l'identité vérifiée à un **membre du canon** (`human:<id>`), par le
  couple exact `{issuer, subject}` (`oidc_subjects:` de la fiche) par défaut,
  ou par l'e-mail vérifié (`emails:`, mode `email`) si l'owner l'active.

Le rattachement est une décision de l'owner (proposition 0033 phase 2,
manaty/home#97, lot #377) : la relecture recommande `{issuer, subject}` —
OIDC Core §5.7, un e-mail peut être réattribué et n'est pas un identifiant
unique. `identity.binding` vaut `subject` par défaut ; `email` ne s'active que
par choix explicite. Rien n'est activé sans décision.

Le contrat (étude `ui-integrable` §2.4, fiche L86) : **RS256 par l'extra
`crypto`** (`cryptography`, PKCS#1 v1.5, clé ≥ 2048 bits), **ES256 par
`ameesh.p256`**. L'extra reste optionnel à l'installation ; sans lui, RS256 est
refusé par une erreur explicite (fail closed), jamais par un repli maison. Un
jeton n'est jamais journalisé.

Configuration de l'hôte (jamais de secret ; un client OIDC public suffit pour
vérifier) :

    "identity": {
        "binding": "subject",             # subject (défaut) | email
        "providers": [
            {"issuer": "https://accounts.google.com", "client_id": "…",
             "domains": ["exemple.org"]}
        ]}

Un `issuer` en `http://` n'est accepté que sur la boucle locale (essai
Keycloak d'un poste) ; partout ailleurs, `https://` est exigé.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ALLOWED_ALGS = ("RS256", "ES256")
DEFAULT_LEEWAY_S = 60
JWKS_TTL_S = 3600
RSA_MIN_BITS = 2048


class OidcError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    issuer: str
    client_id: str
    domains: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> "Provider":
        issuer = str(raw.get("issuer") or "").rstrip("/")
        client_id = str(raw.get("client_id") or "")
        if not _issuer_ok(issuer) or not client_id:
            raise OidcError("fournisseur OIDC : `issuer` en https:// (ou http:// sur la "
                            "boucle locale) et `client_id` requis")
        domains = raw.get("domains") or []
        if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
            raise OidcError("fournisseur OIDC : `domains` doit être une liste")
        return cls(issuer=issuer, client_id=client_id,
                   domains=tuple(d.lower().lstrip("@") for d in domains))


def providers(cfg) -> list[Provider]:
    raw = (getattr(cfg, "identity", None) or {}).get("providers") or []
    return [Provider.from_dict(p) for p in raw]


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------

def b64url_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def split(token: str) -> tuple[dict, dict, bytes, bytes]:
    """(en-tête, revendications, entrée signée, signature) ; aucune vérification."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise OidcError("jeton illisible : trois parties attendues")
    try:
        header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
        signature = b64url_decode(parts[2])
    except (ValueError, json.JSONDecodeError) as exc:
        raise OidcError("jeton illisible : %s" % exc) from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("jeton illisible : objets JSON attendus")
    return header, claims, (parts[0] + "." + parts[1]).encode("ascii"), signature


def _int(b64: str) -> int:
    return int.from_bytes(b64url_decode(b64), "big")


def _crypto():
    """L'extra `crypto` (`cryptography>=41`), requis pour RS256 (fiche L86).

    L'extra reste optionnel à l'installation : sans lui, RS256 n'a pas de
    vérificateur et le jeton est refusé par une erreur explicite (fail closed),
    jamais par un repli écrit à la main."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:
        raise OidcError("RS256 exige l'extra `crypto` : `pip install 'ameesh[crypto]'`"
                        " (cryptography>=41)") from exc
    return rsa, padding, hashes, InvalidSignature


def rsa_verify(n: int, e: int, message: bytes, signature: bytes) -> bool:
    """RS256 (RSASSA-PKCS1-v1_5, SHA-256) par l'extra `crypto`."""
    rsa, padding, hashes, invalid = _crypto()
    try:
        public = rsa.RSAPublicNumbers(e, n).public_key()
    except (ValueError, TypeError):
        return False
    if public.key_size < RSA_MIN_BITS:
        return False
    try:
        public.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except invalid:
        return False
    return True


def es256_verify(jwk: dict, message: bytes, signature: bytes) -> bool:
    from . import p256
    if jwk.get("crv") != "P-256" or len(signature) != 64:
        return False
    point = (_int(jwk["x"]), _int(jwk["y"]))
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    return p256.verify(point, message, p256.encode_der_signature(r, s))


def verify_signature(header: dict, jwk: dict, signing_input: bytes, signature: bytes) -> bool:
    """RS256 par l'extra `crypto` (sans lui, `OidcError` : fail closed), ES256 par `p256`."""
    alg = header.get("alg")
    if alg == "RS256" and jwk.get("kty") == "RSA":
        return rsa_verify(_int(jwk["n"]), _int(jwk["e"]), signing_input, signature)
    if alg == "ES256" and jwk.get("kty") == "EC":
        return es256_verify(jwk, signing_input, signature)
    return False


# --------------------------------------------------------------------------
# clés du fournisseur (découverte, JWKS) en cache
# --------------------------------------------------------------------------

def _issuer_ok(url: str) -> bool:
    """`https://` partout ; `http://` seulement sur la boucle locale (essai local)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.netloc)
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


def _fetch_json(url: str, timeout: float = 10.0) -> dict:
    if not _issuer_ok(url):
        raise OidcError("URL hors https (ou boucle locale) refusée : %s" % url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — schéma vérifié
        return json.loads(resp.read(1_000_000))


@dataclass
class KeyCache:
    fetch: object = None                # url → dict (injectable pour les essais)
    ttl_s: float = JWKS_TTL_S
    _keys: dict = field(default_factory=dict)    # issuer → (expire_at, {kid: jwk})

    def keys(self, issuer: str, *, refresh: bool = False, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        cached = self._keys.get(issuer)
        if cached and cached[0] > now and not refresh:
            return cached[1]
        fetch = self.fetch or _fetch_json
        discovery = fetch(issuer + "/.well-known/openid-configuration")
        if discovery.get("issuer", "").rstrip("/") != issuer:
            raise OidcError("découverte OIDC : émetteur annoncé différent de %s" % issuer)
        jwks = fetch(discovery["jwks_uri"])
        found = {k.get("kid", ""): k for k in jwks.get("keys", []) if isinstance(k, dict)}
        self._keys[issuer] = (now + self.ttl_s, found)
        return found


_DEFAULT_CACHE = KeyCache()


# --------------------------------------------------------------------------
# vérification
# --------------------------------------------------------------------------

def verify(token: str, provider: Provider, *, cache: KeyCache | None = None,
           now: float | None = None, nonce: str | None = None,
           leeway: float = DEFAULT_LEEWAY_S) -> dict:
    """Les revendications d'un jeton d'identité VÉRIFIÉ, sinon `OidcError`."""
    cache = cache or _DEFAULT_CACHE
    now = time.time() if now is None else now
    header, claims, signing_input, signature = split(token)
    if header.get("alg") not in ALLOWED_ALGS:
        raise OidcError("algorithme refusé : %r (%s)" % (header.get("alg"), ", ".join(ALLOWED_ALGS)))
    kid = header.get("kid", "")
    keys = cache.keys(provider.issuer, now=now)
    if kid not in keys:                         # rotation des clés du fournisseur
        keys = cache.keys(provider.issuer, refresh=True, now=now)
    jwk = keys.get(kid)
    if jwk is None:
        raise OidcError("clé %r inconnue du fournisseur" % kid)
    if not verify_signature(header, jwk, signing_input, signature):
        raise OidcError("signature invalide")
    if str(claims.get("iss", "")).rstrip("/") != provider.issuer:
        raise OidcError("émetteur inattendu : %r" % claims.get("iss"))
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if provider.client_id not in audiences:
        raise OidcError("jeton destiné à une autre application (aud)")
    if len(audiences) > 1 and claims.get("azp") != provider.client_id:
        raise OidcError("plusieurs audiences sans azp égal au client")
    for key, test, label in (("exp", lambda v: v > now - leeway, "expiré"),
                             ("nbf", lambda v: v <= now + leeway, "pas encore valide"),
                             ("iat", lambda v: v <= now + leeway, "émis dans le futur")):
        value = claims.get(key)
        if value is None:
            if key == "exp":
                raise OidcError("jeton sans échéance (exp)")
            continue
        if not isinstance(value, (int, float)) or not test(value):
            raise OidcError("jeton %s" % label)
    if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("nonce inattendu")
    if provider.domains:
        domain = str(claims.get("hd") or "").lower()
        email = str(claims.get("email") or "").lower()
        if not domain and "@" in email:
            if claims.get("email_verified") is not True:
                raise OidcError("e-mail non vérifié par le fournisseur")
            domain = email.rsplit("@", 1)[1]
        if domain not in provider.domains:
            raise OidcError("domaine %r hors de l'organisation" % domain)
    return claims


def _member_emails(member) -> set:
    """E-mails déclarés par une fiche `Member` (`emails:`, ou `email:`)."""
    raw = member.fiche.data.get("emails") or member.fiche.data.get("email") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(e).strip().lower() for e in raw if str(e).strip()}


def _member_subjects(member, issuer: str) -> set:
    """Couples `{issuer, subject}` déclarés par une fiche (`oidc_subjects:`).

    Forme lue : `oidc_subjects: [{issuer: …, subject: …}, …]`, les deux clés
    requises ; une entrée d'une autre forme est ignorée (fail closed)."""
    raw = member.fiche.data.get("oidc_subjects") or []
    if not isinstance(raw, list):
        return set()
    out = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        declared = str(item.get("issuer") or "").strip().rstrip("/")
        subject = str(item.get("subject") or "").strip()
        if declared and subject and declared == issuer.rstrip("/"):
            out.add(subject)
    return out


#: rattachement au canon : `subject` (défaut, recommandé) exige le couple exact
#: `{issuer, subject}` de la fiche ; `email` (à activer par décision) accepte
#: l'e-mail vérifié. OIDC Core §5.7 : un e-mail peut être réattribué, il n'est
#: pas un identifiant unique — d'où le défaut sans e-mail.
BINDING_SUBJECT = "subject"
BINDING_EMAIL = "email"
BINDING_MODES = (BINDING_SUBJECT, BINDING_EMAIL)
DEFAULT_BINDING = BINDING_SUBJECT


def binding(cfg) -> str:
    """Mode de rattachement de l'hôte (`identity.binding`, `subject` par défaut).

    Un mode inconnu est refusé (fail closed) : mieux vaut ne rien rattacher que
    rattacher par un chemin que l'owner n'a pas choisi."""
    raw = (getattr(cfg, "identity", None) or {}).get("binding")
    mode = str(raw or DEFAULT_BINDING).strip().lower()
    if mode not in BINDING_MODES:
        raise OidcError("`identity.binding` inconnu : %r (%s)"
                        % (raw, ", ".join(BINDING_MODES)))
    return mode


def human_for(canons, claims: dict, *, issuer: str = "",
              mode: str = DEFAULT_BINDING) -> str | None:
    """`human:<id>` du membre que le jeton vérifié désigne, sinon None.

    `mode` vaut `subject` (défaut) ou `email` ; il vient de la décision de
    l'owner (`identity.binding`), jamais du jeton :

    * `subject` : le couple `{issuer, subject}` doit figurer dans les
      `oidc_subjects:` d'une fiche (option B de la proposition home#97, la
      recommandation) ; l'e-mail n'est pas regardé ;
    * `email` : l'e-mail vérifié doit figurer dans les `emails:` d'une fiche
      (option A) ; le `sub` n'est pas regardé. OIDC Core §5.7 : l'e-mail peut
      être réattribué, ce mode reste un choix explicite.

    Strict dans les deux cas : **un seul** membre correspondant dans l'ensemble
    des canons, sinon None (fail closed)."""
    if mode not in BINDING_MODES:
        raise OidcError("mode de rattachement inconnu : %r" % (mode,))
    candidates = set()
    if mode == BINDING_EMAIL:
        email = str(claims.get("email") or "").strip().lower()
        if email and claims.get("email_verified") is True:
            for c in canons:
                for m in getattr(c, "members", []):
                    if email in _member_emails(m):
                        candidates.add("human:%s" % m.title)
    else:
        subject = str(claims.get("sub") or "").strip()
        if issuer and subject:
            for c in canons:
                for m in getattr(c, "members", []):
                    if subject in _member_subjects(m, issuer):
                        candidates.add("human:%s" % m.title)
    return candidates.pop() if len(candidates) == 1 else None


def main(argv) -> int:
    """`ameesh identity verify [FICHIER|-] [--issuer U --client-id C] [--domain D]`."""
    import argparse
    import sys
    from .config import load as load_config
    p = argparse.ArgumentParser(prog="ameesh identity",
                                description="Vérifie un jeton d'identité OIDC et le rattache "
                                            "au canon (L86, lot #377).")
    p.add_argument("sub", choices=["verify"])
    p.add_argument("token_file", nargs="?", default="-")
    p.add_argument("--issuer")
    p.add_argument("--client-id")
    p.add_argument("--domain", action="append", default=[])
    p.add_argument("--binding", choices=list(BINDING_MODES), default=None,
                   help="rattachement au canon : `subject` (défaut, recommandé) ou "
                        "`email` (à activer par décision)")
    args = p.parse_args(argv)
    token = (sys.stdin.read() if args.token_file == "-"
             else open(args.token_file, encoding="utf-8").read()).strip()
    cfg = load_config()
    try:
        mode = args.binding or binding(cfg)
        if args.issuer:
            choix = [Provider.from_dict({"issuer": args.issuer, "client_id": args.client_id,
                                         "domains": args.domain})]
        else:
            choix = providers(cfg)
        if not choix:
            raise OidcError("aucun fournisseur (identity.providers de l'hôte, ou --issuer)")
        iss = str(split(token)[1].get("iss", "")).rstrip("/")
        provider = next((x for x in choix if x.issuer == iss), None)
        if provider is None:
            raise OidcError("émetteur %r non configuré" % iss)
        claims = verify(token, provider)
    except (OidcError, OSError, KeyError) as exc:
        print("ameesh identity : refusé — %s" % exc, file=sys.stderr)
        return 1
    from . import canon as canon_mod
    human = human_for(canon_mod.load_configured(cfg), claims, issuer=provider.issuer,
                      mode=mode)
    print(json.dumps({"sub": claims.get("sub"), "email": claims.get("email"),
                      "issuer": provider.issuer, "binding": mode, "human": human},
                     ensure_ascii=False))
    return 0 if human else 3
