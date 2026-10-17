# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh approve-check: WebAuthn policy agreement (lot L27).

  ameesh approve-check [--url U] [--json]

The ameesh-approve service signs under ITS policy (RP ID, origins: its
JSON); ameesh verifies and consumes receipts under the verifiers' policy
(`actions.policy_from_env`: AMEESH_APPROVE_RP_ID, AMEESH_APPROVE_ORIGINS),
read independently. If they diverge, valid receipts are refused, or worse,
a machine trusts more than the service does. This command reads the policy
published by the service (`GET /health`, no token or secret) at
`approve_url` (or `--url`) and compares it with this machine's.

Exit codes: 0 agreement; 1 mismatch (listed on output); 2 configuration
missing or service unreachable. Nothing is written.

Changing `approve_url` does not change trust: only the verifiers' policy
does, and this command checks that it still matches the service's.
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
        ecarts.append("verifier: AMEESH_APPROVE_RP_ID missing (no WebAuthn receipt will "
                      "be accepted)")
    elif policy.rp_id != service.get("rp_id"):
        ecarts.append("RP ID: service %r, verifier %r" % (service.get("rp_id"),
                                                               policy.rp_id))
    theirs = set(service.get("origins") or ())
    ours = set(policy.origins)
    if not ours:
        ecarts.append("verifier: AMEESH_APPROVE_ORIGINS missing")
    else:
        for origin in sorted(ours - theirs):
            ecarts.append("origin allowed by the verifier but not by the service: %s"
                          % origin)
        for origin in sorted(theirs - ours):
            ecarts.append("service origin refused by the verifier: %s" % origin)
    if service.get("profile") == "strict" and len(ours) > 1:
        ecarts.append("verifier: several origins while the service uses the strict "
                      "profile (only one, https://<RP ID>)")
    return ecarts


def _route(url: str, service: dict) -> str:
    """Par où passe l'API : boucle locale, ou hôte public ouvert explicitement."""
    host = (urlsplit(url).hostname or "").lower()
    public = (urlsplit(service.get("public_url") or "").hostname or "").lower()
    if public and host == public:
        return "public host (api_via_public)"
    if approve_client._loopback(host):
        return "loopback"
    return "private access (%s)" % host


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh approve-check", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None,
                        help="service API URL (default: AMEESH_APPROVE_URL)")
    parser.add_argument("--json", action="store_true", help="JSON output")
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
            print("approve-check: %s" % exc.message, file=sys.stderr)
        return 2
    ecarts = compare(service, policy)
    base = approve_client.endpoint(url)
    if args.json:
        print(json.dumps({
            "ok": not ecarts, "ecarts": ecarts, "service": service,
            "verificateur": {"rp_id": policy.rp_id, "origins": list(policy.origins)},
            "api": _route(base, service)}, ensure_ascii=False, sort_keys=True))
        return 1 if ecarts else 0
    print("service  : RP ID %s, origins %s, profile %s, level %s%s"
          % (service.get("rp_id"), ", ".join(service.get("origins") or ()),
             service.get("profile"), service.get("level"),
             ", local TLS" if service.get("tls") else ""))
    print("verifier : RP ID %s, origins %s"
          % (policy.rp_id or "(missing)", ", ".join(policy.origins) or "(missing)"))
    print("API      : %s via %s" % (base, _route(base, service)))
    print("login API: %s (connection challenges, dedicated token)"
          % (service.get("login_api") or "unknown"))
    if ecarts:
        print("Policy MISMATCH (%d):" % len(ecarts))
        for ecart in ecarts:
            print("  - %s" % ecart)
        print("Fix the configuration of the service OR of the verifiers (the provisioner "
              "sets both); nothing was changed.")
        return 1
    print("policies match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
