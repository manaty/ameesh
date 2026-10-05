# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh approve-check — concordance de la politique WebAuthn (lot L27).

  ameesh approve-check [--url U] [--json]

Le service ameesh-approve signe sous SA politique (RP ID, origines : son
JSON) ; ameesh vérifie et consomme les reçus sous celle des vérificateurs
(`actions.policy_from_env` : AMEESH_APPROVE_RP_ID, AMEESH_APPROVE_ORIGINS),
lue indépendamment. Si elles divergent, des reçus valides sont refusés — ou,
pire, la confiance d'un poste est plus large que celle du service. Cette
commande lit la politique publiée par le service (`GET /health`, sans jeton
ni secret) à `approve_url` (ou `--url`) et la compare à celle de ce poste.

Codes : 0 concordance ; 1 écart (liste sur la sortie) ; 2 configuration
absente ou service injoignable. Rien n'est écrit.

Modifier `approve_url` ne modifie pas la confiance : seule la politique des
vérificateurs le fait, et cette commande vérifie qu'elle reste celle du
service.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlsplit

from . import actions, approve_client, config as config_mod


def compare(service: dict, policy) -> list[str]:
    """Écarts entre la politique publiée par le service et celle d'un
    vérificateur (receipts.Policy) ; liste vide = concordance."""
    ecarts = []
    if not policy.rp_id:
        ecarts.append("vérificateur : AMEESH_APPROVE_RP_ID absent (aucun reçu WebAuthn ne "
                      "sera accepté)")
    elif policy.rp_id != service.get("rp_id"):
        ecarts.append("RP ID : service %r, vérificateur %r" % (service.get("rp_id"),
                                                               policy.rp_id))
    theirs = set(service.get("origins") or ())
    ours = set(policy.origins)
    if not ours:
        ecarts.append("vérificateur : AMEESH_APPROVE_ORIGINS absent")
    else:
        for origin in sorted(ours - theirs):
            ecarts.append("origine admise par le vérificateur mais pas par le service : %s"
                          % origin)
        for origin in sorted(theirs - ours):
            ecarts.append("origine du service refusée par le vérificateur : %s" % origin)
    if service.get("profile") == "strict" and len(ours) > 1:
        ecarts.append("vérificateur : plusieurs origines alors que le service est en profil "
                      "strict (une seule, https://<RP ID>)")
    return ecarts


def _route(url: str, service: dict) -> str:
    """Par où passe l'API : boucle locale, ou hôte public ouvert explicitement."""
    host = (urlsplit(url).hostname or "").lower()
    public = (urlsplit(service.get("public_url") or "").hostname or "").lower()
    if public and host == public:
        return "hôte public (api_via_public)"
    if approve_client._loopback(host):
        return "boucle locale"
    return "accès privé (%s)" % host


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh approve-check", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None,
                        help="URL de l'API du service (défaut : AMEESH_APPROVE_URL)")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    url = args.url if args.url is not None else cfg.approve_url
    policy = actions.policy_from_env(os.environ)
    try:
        service = approve_client.health(cfg, url=url)
    except approve_client.ApproveClientError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": exc.code, "message": exc.message},
                             ensure_ascii=False, sort_keys=True))
        else:
            print("approve-check : %s" % exc.message, file=sys.stderr)
        return 2
    ecarts = compare(service, policy)
    base = approve_client.endpoint(url)
    if args.json:
        print(json.dumps({
            "ok": not ecarts, "ecarts": ecarts, "service": service,
            "verificateur": {"rp_id": policy.rp_id, "origins": list(policy.origins)},
            "api": _route(base, service)}, ensure_ascii=False, sort_keys=True))
        return 1 if ecarts else 0
    print("service      : RP ID %s, origines %s, profil %s, niveau %s%s"
          % (service.get("rp_id"), ", ".join(service.get("origins") or ()),
             service.get("profile"), service.get("level"),
             ", TLS local" if service.get("tls") else ""))
    print("vérificateur : RP ID %s, origines %s"
          % (policy.rp_id or "(absent)", ", ".join(policy.origins) or "(absentes)"))
    print("API          : %s par %s" % (base, _route(base, service)))
    if ecarts:
        print("ÉCART de politique (%d) :" % len(ecarts))
        for ecart in ecarts:
            print("  - %s" % ecart)
        print("Corriger la configuration du service OU des vérificateurs (le provisionneur "
              "pose les deux) ; rien n'a été modifié.")
        return 1
    print("politique concordante")
    return 0


if __name__ == "__main__":
    sys.exit(main())
