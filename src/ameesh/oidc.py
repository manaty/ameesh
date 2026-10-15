# SPDX-License-Identifier: AGPL-3.0-only
"""Identité des humains par OIDC (L86, lot #377 ; décisions 0033 §4, 0032 §2).

ameesh accepte un fournisseur d'identité OIDC **générique**, choisi par
l'organisation (Google restreint au domaine de l'organisation, Keycloak,
Supabase Auth…). Le fournisseur dit *qui* est l'humain ; le canon dit *ce
qu'il est* dans l'organisation. Ce module :

* vérifie un **jeton d'identité** (ID token, JWT signé) : signature RS256 ou
  ES256 contre les clés publiées par le fournisseur (découverte OIDC, puis
  JWKS, en cache), contrat ID token d'OIDC Core §2 (`iss` exact, `aud`, `sub`,
  `iat`, `exp` — nombres finis hors booléens — `nbf` s'il est présent),
  audience (`azp` si plusieurs audiences), `nonce` s'il est attendu, et la
  **restriction de domaine** de l'organisation (`hd` de Google, sinon domaine de
  l'e-mail, avec `email_verified`) ;
* rattache l'identité vérifiée à un **membre du canon** (`human:<id>`), par le
  couple exact `{issuer, subject}` (`oidc_subjects:` de la fiche) par défaut,
  ou par l'e-mail vérifié (`emails:`, mode `email`) si l'owner l'active.

Stricteté (relecture R1 Codex, 15/10) : `iss`, `sub` et les valeurs du canon
sont des **chaînes exactes**, jamais rognées ni converties (OIDC Core §2 — une
espace ou un `/` final désigne une autre identité) ; les **extensions critiques
JWS** (`crit`, RFC 7515 §4.1.11) sont refusées faute d'implémentation ; les
métadonnées de clé déclarées (`use`, `key_ops`, `alg`, RFC 7517 §4.2-4.4) sont
respectées ; les **redirections HTTP sont contrôlées saut par saut** (une
origine `https://` ne redescend jamais en `http://`).

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
import math
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
        """Un fournisseur déclaré, **sans normalisation** : `issuer` et
        `client_id` sont conservés tels quels (OIDC Core §2 : l'`issuer` est une
        chaîne exacte ; un `/` final, une espace ou un autre type ne sont pas
        rabattus — le jeton ne correspondra simplement pas)."""
        if not isinstance(raw, dict):
            raise OidcError("fournisseur OIDC : objet attendu")
        issuer = raw.get("issuer")
        client_id = raw.get("client_id")
        if not isinstance(issuer, str) or not issuer or not _issuer_ok(issuer):
            raise OidcError("fournisseur OIDC : `issuer` (chaîne exacte, https:// ou "
                            "http:// sur la boucle locale) requis")
        if not isinstance(client_id, str) or not client_id:
            raise OidcError("fournisseur OIDC : `client_id` (chaîne exacte) requis")
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


def jwk_allows(jwk: dict, alg: str) -> bool:
    """Métadonnées de la JWK (RFC 7517 §4.2-4.4), quand elles sont déclarées.

    `use` vaut `sig`, `key_ops` contient `verify`, `alg` vaut l'algorithme de
    l'en-tête ; une déclaration contraire refuse la clé. Une métadonnée absente
    reste acceptée (contrat : les fournisseurs ne les publient pas tous)."""
    use = jwk.get("use")
    if "use" in jwk and use != "sig":
        return False
    ops = jwk.get("key_ops")
    if "key_ops" in jwk:
        if not isinstance(ops, list) or not all(isinstance(op, str) for op in ops):
            return False
        if "verify" not in ops:
            return False
    declared = jwk.get("alg")
    if "alg" in jwk and declared != alg:
        return False
    return True


def verify_signature(header: dict, jwk: dict, signing_input: bytes, signature: bytes) -> bool:
    """RS256 par l'extra `crypto` (sans lui, `OidcError` : fail closed), ES256 par `p256`."""
    alg = header.get("alg")
    if alg not in ALLOWED_ALGS or not jwk_allows(jwk, alg):
        return False
    try:
        if alg == "RS256" and jwk.get("kty") == "RSA":
            return rsa_verify(_int(jwk["n"]), _int(jwk["e"]), signing_input, signature)
        if alg == "ES256" and jwk.get("kty") == "EC":
            return es256_verify(jwk, signing_input, signature)
    except OidcError:
        raise                               # ex. extra `crypto` absent : erreur explicite
    except (KeyError, TypeError, ValueError):
        return False                      # clé publiée mal formée : refus, jamais un crash
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


def _redirect_ok(origin: str, target: str) -> bool:
    """Chaque saut d'une redirection est contrôlé **avant émission**.

    Une origine `https://` ne redescend jamais en `http://` (les clés JWKS
    pourraient être substituées sur le segment clair) ; l'exception `http://`
    de la boucle locale reste bornée à la boucle locale."""
    if not _issuer_ok(target):
        return False
    if urllib.parse.urlsplit(origin).scheme == "https":
        return urllib.parse.urlsplit(target).scheme == "https"
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Redirections contrôlées saut par saut (RFC 9110 §15.4) ; au-delà : refus."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _redirect_ok(req.full_url, newurl):
            raise OidcError("redirection refusée (hors https ou hors boucle locale) : %s"
                            % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: ouvreur unique : le contrôle des redirections ne dépend pas de l'appelant
_OPENER = urllib.request.build_opener(_SafeRedirect)


def _fetch_json(url: str, timeout: float = 10.0) -> dict:
    if not _issuer_ok(url):
        raise OidcError("URL hors https (ou boucle locale) refusée : %s" % url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _OPENER.open(req, timeout=timeout) as resp:   # noqa: S310 — schémas contrôlés
        final = resp.geturl()
        if not _issuer_ok(final) or not _redirect_ok(url, final):
            raise OidcError("réponse obtenue hors du schéma autorisé : %s" % final)
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
        if not isinstance(discovery, dict) or discovery.get("issuer") != issuer:
            raise OidcError("découverte OIDC : émetteur annoncé différent de %s" % issuer)
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise OidcError("découverte OIDC : `jwks_uri` absent ou de forme invalide")
        jwks = fetch(jwks_uri)
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise OidcError("JWKS : `keys` doit être une liste")
        found = {k.get("kid", ""): k for k in jwks["keys"] if isinstance(k, dict)}
        self._keys[issuer] = (now + self.ttl_s, found)
        return found


_DEFAULT_CACHE = KeyCache()


# --------------------------------------------------------------------------
# vérification
# --------------------------------------------------------------------------

def verify(token: str, provider: Provider, *, cache: KeyCache | None = None,
           now: float | None = None, nonce: str | None = None,
           leeway: float = DEFAULT_LEEWAY_S) -> dict:
    """Les revendications d'un jeton d'identité VÉRIFIÉ, sinon `OidcError`.

    Le contrat ID token (OIDC Core §2) est exigé : `iss` exact, `aud`, `sub`
    (chaîne non vide), `iat` et `exp` (nombres finis, hors booléens), `nbf`
    contrôlé s'il est présent. Les extensions critiques JWS (`crit`, RFC 7515
    §4.1.11) ne sont pas implémentées : leur présence refuse le jeton."""
    cache = cache or _DEFAULT_CACHE
    now = time.time() if now is None else now
    header, claims, signing_input, signature = split(token)
    if "crit" in header:
        raise OidcError("extension critique JWS non prise en charge : %r" % (header.get("crit"),))
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
    iss = claims.get("iss")
    if not isinstance(iss, str) or iss != provider.issuer:
        raise OidcError("émetteur inattendu : %r" % (iss,))
    aud = claims.get("aud")
    if isinstance(aud, str):
        audiences = [aud]
    elif isinstance(aud, list) and all(isinstance(a, str) for a in aud):
        audiences = aud
    else:
        raise OidcError("audience absente ou de forme invalide (aud)")
    if provider.client_id not in audiences:
        raise OidcError("jeton destiné à une autre application (aud)")
    if len(audiences) > 1 and claims.get("azp") != provider.client_id:
        raise OidcError("plusieurs audiences sans azp égal au client")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise OidcError("sujet absent ou de forme invalide (sub)")
    for key, label, obligatoire in (("exp", "échéance (exp)", True),
                                    ("iat", "date d'émission (iat)", True),
                                    ("nbf", "pas encore valide", False)):
        value = claims.get(key)
        if value is None:
            if obligatoire:
                raise OidcError("jeton sans %s" % label)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise OidcError("jeton : %s de forme invalide" % label)
        if key == "exp" and not value > now - leeway:
            raise OidcError("jeton expiré")
        if key == "nbf" and not value <= now + leeway:
            raise OidcError("jeton pas encore valide")
        if key == "iat" and not value <= now + leeway:
            raise OidcError("jeton émis dans le futur")
    if nonce is not None:
        claim_nonce = claims.get("nonce")
        if not isinstance(claim_nonce, str) or not hmac.compare_digest(claim_nonce, nonce):
            raise OidcError("nonce inattendu")
    if provider.domains:
        hd = claims.get("hd")
        email = claims.get("email")
        if hd is not None and not isinstance(hd, str):
            raise OidcError("revendication `hd` de forme invalide")
        if email is not None and not isinstance(email, str):
            raise OidcError("revendication `email` de forme invalide")
        domain = (hd or "").lower()
        email = (email or "").lower()
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
    return {e.strip().lower() for e in raw if isinstance(e, str) and e.strip()}


def _member_subjects(member, issuer: str) -> set:
    """Couples `{issuer, subject}` déclarés par une fiche (`oidc_subjects:`).

    Forme lue : `oidc_subjects: [{issuer: …, subject: …}, …]`, deux **chaînes**
    requises, comparées **sans normalisation** (OIDC Core §2 : une espace ou un
    `/` final change l'identité ; le canon doit porter la valeur exacte du
    jeton). Une entrée d'une autre forme est ignorée (fail closed)."""
    raw = member.fiche.data.get("oidc_subjects") or []
    if not isinstance(raw, list):
        return set()
    out = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        declared = item.get("issuer")
        subject = item.get("subject")
        if isinstance(declared, str) and isinstance(subject, str) \
                and declared and subject and declared == issuer:
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

    Un mode inconnu ou d'un autre type est refusé (fail closed) : mieux vaut ne
    rien rattacher que rattacher par un chemin que l'owner n'a pas choisi."""
    raw = (getattr(cfg, "identity", None) or {}).get("binding")
    if raw is None:
        return DEFAULT_BINDING
    if not isinstance(raw, str) or raw.lower() not in BINDING_MODES:
        raise OidcError("`identity.binding` inconnu : %r (%s)"
                        % (raw, ", ".join(BINDING_MODES)))
    return raw.lower()


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
    des canons, sinon None (fail closed). Le `sub` et l'`issuer` sont des
    **chaînes exactes** (OIDC Core §2) : ni espace, ni `/`, ni type ne sont
    rabattus."""
    if mode not in BINDING_MODES:
        raise OidcError("mode de rattachement inconnu : %r" % (mode,))
    candidates = set()
    if mode == BINDING_EMAIL:
        email = claims.get("email")
        if isinstance(email, str) and email and claims.get("email_verified") is True:
            email = email.strip().lower()
            for c in canons:
                for m in getattr(c, "members", []):
                    if email in _member_emails(m):
                        candidates.add("human:%s" % m.title)
    else:
        subject = claims.get("sub")
        if isinstance(issuer, str) and issuer and isinstance(subject, str) and subject:
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
        iss = split(token)[1].get("iss")
        provider = next((x for x in choix if x.issuer == iss), None)
        if provider is None:
            raise OidcError("émetteur %r non configuré" % (iss,))
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
