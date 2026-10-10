#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Signature de release de l'image `ameesh-executor` (L114, docs/IMAGE-EXECUTEUR.md §8).

Lancé par le PROPRIÉTAIRE, sur son poste, avec la clé privée de release
gardée hors du dépôt. Rien ici ne lit ni n'écrit de clé ailleurs que là où on
le lui dit.

  signer-release.py public  --key release-key.pem
        la clé publique, SPKI DER en base64url (valeur de AMEESH_RELEASE_PUBLIC_KEY)
  signer-release.py sign    --key release-key.pem  <registre>/ameesh-executor@sha256:<64 hex>
        la signature (ES256, r‖s, base64url, 86 caractères)
  signer-release.py verify  --pub <base64url>  <image>  <signature>

Octets signés (identiques à `agentImageSignedBytes` côté Nexlink) :
`JSON.stringify(["ameesh/release/v1/image", image])`, c'est-à-dire du JSON
compact, sans espace. Demande l'extra `cryptography`.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import re
import sys

DOMAIN = "ameesh/release/v1/image"
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.\-:/]*/ameesh-executor@sha256:[0-9a-f]{64}$")


def signed_bytes(image: str) -> bytes:
    if not IMAGE_RE.match(image):
        raise SystemExit("image : <registre>/ameesh-executor@sha256:<64 hex minuscules> attendu")
    return json.dumps([DOMAIN, image], separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _private(path: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    with open(path, "rb") as fh:
        pem = fh.read()
    password = None
    if b"ENCRYPTED" in pem:
        password = getpass.getpass("phrase de la clé de release : ").encode()
    key = serialization.load_pem_private_key(pem, password=password)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise SystemExit("clé de release : P-256 attendue")
    return key


def public_b64u(key) -> str:
    from cryptography.hazmat.primitives import serialization
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return b64u(der)


def sign(key, image: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    r, s = decode_dss_signature(key.sign(signed_bytes(image), ec.ECDSA(hashes.SHA256())))
    return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def verify(pub: str, image: str, signature: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    if not re.fullmatch(r"[A-Za-z0-9_-]{86}", signature):
        return False
    raw = unb64u(signature)
    key = serialization.load_der_public_key(unb64u(pub))
    try:
        key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                        int.from_bytes(raw[32:], "big")),
                   signed_bytes(image), ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("public")
    a.add_argument("--key", required=True)
    s = sub.add_parser("sign")
    s.add_argument("--key", required=True)
    s.add_argument("image")
    v = sub.add_parser("verify")
    v.add_argument("--pub", required=True)
    v.add_argument("image")
    v.add_argument("signature")
    args = p.parse_args(argv)
    if args.cmd == "public":
        print(public_b64u(_private(args.key)))
        return 0
    if args.cmd == "sign":
        key = _private(args.key)
        signature = sign(key, args.image)
        print(json.dumps({"schema": "ameesh-release-signature/1", "image": args.image,
                          "image_signature": signature, "public_key": public_b64u(key)},
                         indent=2))
        return 0
    ok = verify(args.pub, args.image, args.signature)
    print("signature valide" if ok else "signature FAUSSE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
