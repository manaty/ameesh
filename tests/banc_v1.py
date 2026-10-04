# SPDX-License-Identifier: AGPL-3.0-only
"""Outillage de l'essai v1 de bout en bout (tests/test_bout_en_bout.py) et de la
démonstration (scripts/demo-v1.sh). Code de TEST : jamais importé par ameesh.

Il joue ce qu'ameesh v1 laisse hors de portée des agents, sans téléphone ni
navigateur (la demande d'approbation et la récupération du reçu passent par
`ameesh action request` et `ameesh action fetch-receipt`, jeton de service
compris : le banc n'en a pas) :

* le **téléphone de l'approbateur** : une passkey LOGICIELLE
  (tests/webauthn_soft.py, jamais une vraie clé) qui s'enrôle sur
  ameesh-approve (`/enroll/<jeton>`) puis signe une approbation (`/a/<jeton>`) ;
* le **report de la proposition d'enrôlement au canon** (la « PR » que le
  propriétaire fusionne) : l'entrée `authenticator` de la proposition est
  ajoutée à `authenticators` de la fiche Member. `ameesh canon sync` la
  recopie ensuite dans le registre de confiance (§8.2), sans aide du banc.

Le mandataire HTTPS (page Nexlink) est simulé : les requêtes partent en HTTP
vers 127.0.0.1 avec l'en-tête `Host` (et `Origin` pour les POST humains) de
l'origine publique.

Ligne de commande (depuis la racine du dépôt, PYTHONPATH=src) :

  python3 -m tests.banc_v1 enroll  --port P --link URL --key FICHIER
  python3 -m tests.banc_v1 canon-entry --proposal FICHIER --member FICHE.md
  python3 -m tests.banc_v1 approve --port P --link URL --key FICHIER
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import os
import re
import sys
from urllib.parse import urlsplit

from ameesh import canon as canon_mod
from ameesh import p256, receipts

from .webauthn_soft import ORIGIN, RP_ID, SoftWebAuthn, b64u, cbor

#: champs d'une entrée d'authentificateur au canon (spec §8.2)
ENTRY_FIELDS = ("facade", "credential_id", "public_key", "aaguid", "level")


class BancError(RuntimeError):
    """Réponse inattendue du service ou fichier illisible."""


# --------------------------------------------------------------------------
# HTTP : le service écoute sur la boucle locale ; Host = origine publique
# --------------------------------------------------------------------------

def http_call(port: int, method: str, path: str, body=None, *, host: str,
              origin: str | None = None, timeout: float = 15.0) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        headers = {"Host": host}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def public_host(origin: str = ORIGIN) -> str:
    return urlsplit(origin).netloc


def link_path(link: str, origin: str = ORIGIN) -> str:
    """Le chemin d'un lien public, après contrôle de son origine."""
    parts = urlsplit(link.strip())
    if "%s://%s" % (parts.scheme, parts.netloc) != origin:
        raise BancError("lien hors de l'origine attendue %s : %s" % (origin, link))
    return parts.path


def page_data(text: str, element: str) -> dict:
    """Les attributs `data-*` de `<main id="…">` d'une page du service."""
    match = re.search(r'<main id="%s" ([^>]*)>' % re.escape(element), text)
    if not match:
        raise BancError("page sans élément %s" % element)
    return {name: html.unescape(value)
            for name, value in re.findall(r'data-([a-z-]+)="([^"]*)"', match.group(1))}


# --------------------------------------------------------------------------
# la passkey logicielle (persistée pour la démonstration, 0600)
# --------------------------------------------------------------------------

def new_passkey() -> SoftWebAuthn:
    return SoftWebAuthn("ES256")


def save_passkey(soft: SoftWebAuthn, path: str) -> None:
    data = {"alg": "ES256", "private": "%064x" % soft.key.private,
            "credential_id": soft.credential_id,
            "avertissement": "clé LOGICIELLE de test (tests/webauthn_soft.py), jamais une vraie"}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def load_passkey(path: str) -> SoftWebAuthn:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    soft = SoftWebAuthn("ES256")
    soft.key.private = int(data["private"], 16)
    soft.key.point = p256.scalar_mult(soft.key.private, (p256.GX, p256.GY))
    soft.credential_id = data["credential_id"]
    return soft


def attestation(soft: SoftWebAuthn, challenge: bytes, *, origin: str = ORIGIN,
                rp_id: str = RP_ID) -> dict:
    """Réponse de `navigator.credentials.create` (format `none`), en logiciel."""
    credential = receipts.b64u_decode(soft.credential_id)
    flags = receipts.FLAG_UP | receipts.FLAG_UV | receipts.FLAG_AT
    auth = (hashlib.sha256(rp_id.encode("utf-8")).digest() + bytes([flags])
            + (0).to_bytes(4, "big") + bytes(16) + len(credential).to_bytes(2, "big")
            + credential + soft.key.cose())
    client = json.dumps({"type": "webauthn.create", "challenge": b64u(challenge),
                         "origin": origin}, separators=(",", ":")).encode("utf-8")
    return {"credential_id": soft.credential_id, "clientDataJSON": b64u(client),
            "attestationObject": b64u(cbor({"fmt": "none", "attStmt": {}, "authData": auth}))}


# --------------------------------------------------------------------------
# le téléphone : enrôlement, approbation
# --------------------------------------------------------------------------

def enroll(port: int, link: str, soft: SoftWebAuthn, *, origin: str = ORIGIN) -> dict:
    """Ouvre le lien d'enrôlement, crée la passkey ; rend la réponse du service
    (`proposal` : nom du fichier de proposition de canon)."""
    path = link_path(link, origin)
    status, payload = http_call(port, "GET", path, host=public_host(origin))
    if status != 200:
        raise BancError("page d'enrôlement : HTTP %d %s" % (status, payload[:200]))
    page = page_data(payload.decode("utf-8"), "ameesh-enroll")
    body = attestation(soft, receipts.b64u_decode(page["challenge"]), origin=origin,
                       rp_id=page["rp-id"])
    status, payload = http_call(port, "POST", path, body, host=public_host(origin),
                                origin=origin)
    if status != 200:
        raise BancError("enrôlement refusé : HTTP %d %s" % (status, payload[:300]))
    return json.loads(payload)


def approve(port: int, link: str, soft: SoftWebAuthn, *, decision: str = "approve",
            origin: str = ORIGIN) -> dict:
    """Ouvre la page d'approbation, signe le défi affiché ; rend {page, result}."""
    path = link_path(link, origin)
    status, payload = http_call(port, "GET", path, host=public_host(origin))
    if status != 200:
        raise BancError("page d'approbation : HTTP %d %s" % (status, payload[:200]))
    text = payload.decode("utf-8")
    page = page_data(text, "ameesh-approval")
    if soft.credential_id not in page.get("credentials", "").split(","):
        raise BancError("la page n'admet pas ce credential (%s)" % page.get("credentials"))
    challenge = receipts.b64u_decode(page["challenge-" + decision])
    body = dict(soft.proof(challenge), decision=decision, credential_id=soft.credential_id)
    status, payload = http_call(port, "POST", path, body, host=public_host(origin),
                                origin=origin)
    if status != 200:
        raise BancError("assertion refusée : HTTP %d %s" % (status, payload[:300]))
    summary = re.search(r'<pre class="summary">(.*?)</pre>', text, re.S)
    return {"page": page, "summary": html.unescape(summary.group(1)) if summary else "",
            "result": json.loads(payload)}


# --------------------------------------------------------------------------
# la « PR » du canon
# --------------------------------------------------------------------------

def proposal_entry(path: str) -> dict:
    """L'entrée `authenticator` d'une proposition d'ameesh-approve."""
    with open(path, encoding="utf-8") as fh:
        block = canon_mod.split_frontmatter(fh.read())
    if block is None:
        raise BancError("proposition sans frontmatter : %s" % path)
    data = canon_mod.load_yaml(block)
    entry = data.get("authenticator") if isinstance(data, dict) else None
    if not isinstance(entry, dict) or set(entry) != set(ENTRY_FIELDS):
        raise BancError("proposition illisible (authenticator) : %s" % path)
    if data.get("type") != "AuthenticatorProposal" or data.get("status") != "proposed":
        raise BancError("pas une proposition d'authentificateur : %s" % path)
    return {key: entry[key] for key in ENTRY_FIELDS}


def add_to_member(member_path: str, entry: dict) -> None:
    """Ajoute l'entrée à `authenticators` d'une fiche Member (liste vide ou absente)."""
    with open(member_path, encoding="utf-8") as fh:
        text = fh.read()
    block = ["authenticators:"]
    first = True
    for key in ENTRY_FIELDS:
        block.append("%s%s: %s" % ("  - " if first else "    ", key,
                                   json.dumps(entry[key], ensure_ascii=False)))
        first = False
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise BancError("fiche sans frontmatter : %s" % member_path)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise BancError("frontmatter non fermé : %s" % member_path)
    for i in range(1, end):
        if lines[i].startswith("authenticators:"):
            if lines[i].split(":", 1)[1].strip() not in ("", "[]"):
                raise BancError("fiche avec des authentificateurs : à fusionner à la main")
            if i + 1 < end and lines[i + 1].startswith((" ", "-")):
                raise BancError("liste d'authentificateurs non vide : à fusionner à la main")
            lines[i:i + 1] = block
            break
    else:
        lines[end:end] = block
    with open(member_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))



# --------------------------------------------------------------------------
# ligne de commande (scripts/demo-v1.sh)
# --------------------------------------------------------------------------

def _cmd_enroll(args) -> int:
    soft = new_passkey()
    result = enroll(args.port, args.link, soft)
    save_passkey(soft, args.key)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_canon_entry(args) -> int:
    entry = proposal_entry(args.proposal)
    add_to_member(args.member, entry)
    print("entrée ajoutée à %s : %s %s (niveau %s)" % (
        os.path.basename(args.member), entry["facade"], entry["credential_id"], entry["level"]))
    return 0


def _cmd_approve(args) -> int:
    done = approve(args.port, args.link, load_passkey(args.key))
    print("page affichée au téléphone :\n  %s" % done["summary"].replace("\n", "\n  "))
    print(json.dumps(done["result"], ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tests.banc_v1", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("enroll")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--link", required=True)
    p.add_argument("--key", required=True)
    p.set_defaults(func=_cmd_enroll)
    p = sub.add_parser("canon-entry")
    p.add_argument("--proposal", required=True)
    p.add_argument("--member", required=True)
    p.set_defaults(func=_cmd_canon_entry)
    p = sub.add_parser("approve")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--link", required=True)
    p.add_argument("--key", required=True)
    p.set_defaults(func=_cmd_approve)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (BancError, OSError, ValueError) as exc:
        print("banc_v1 : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
