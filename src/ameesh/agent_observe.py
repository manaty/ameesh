# SPDX-License-Identifier: AGPL-3.0-only
"""Commande `ameesh agent observe` (contrat public E1b, #7636).

  ameesh agent observe <agent> [--account-profile NAME] [--json]
        le profil admis, le bail, les bornes d'exécution et l'enveloppe de
        facturation expurgée d'un agent (schéma `ameesh-agent-observation/1`).

Le même constructeur sert `GET /api/v1/agents/{agent}/observation` : le
document `--json` et le corps HTTP ne peuvent pas diverger.

La session est en lecture seule et le constructeur ouvre sa propre
transaction `REPEATABLE READ, READ ONLY` : aucun bail n'est pris, renouvelé
ou rendu, aucune réservation n'est posée, aucun canon n'est synchronisé.
Seul `--json` fait contrat ; le rendu texte peut changer.

Sorties : `0` le document ; `3` agent inconnu (rien sur stdout qu'on puisse
prendre pour une observation) ; `2` usage ; `1` base injoignable.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .config import NAME_RE

#: code de sortie d'une cible inconnue (comme `turn observe`)
EXIT_UNKNOWN = 3


def _agent_name(value: str) -> str:
    if not NAME_RE.match(value or ""):
        raise argparse.ArgumentTypeError("invalid agent name")
    return value


def _account_name(value: str) -> str:
    if not NAME_RE.match(value or ""):
        raise argparse.ArgumentTypeError("invalid account profile name")
    return value


def _age(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh" % (seconds // 3600)


def _text(document: dict) -> str:
    """Le rendu humain ; les états machine sont dans `--json`."""
    agent = document["agent"]
    profile = document["profile"]
    admission = document["admission"]
    lease = document["lease"]
    limits = document["limits"]
    budget = document["budget"]
    lines = [
        "agent      %s: %s (%s)" % (agent["name"], agent["status"],
                                    agent["mode"]),
        "profile    %s/%s on %s%s" % (
            profile["harness"] or "?", profile["model"] or "?",
            profile["host"] or "?",
            (", responsible " + profile["responsible"])
            if profile["responsible"] else ""),
        "admission  %s%s" % (
            ("unknown" if admission["admitted"] is None else
             "admitted" if admission["admitted"] else "not admitted"),
            (" (%s)" % ", ".join(admission["reasons"]))
            if admission["reasons"] else ""),
    ]
    if lease["live"]:
        lines.append("lease      live, owner %s epoch %s, expires in %s" % (
            lease["owner"] or "?", lease["epoch"], _age(
                None if lease["expires_ts"] is None else
                lease["expires_ts"] - document["observed_ts"])))
    else:
        lines.append("lease      not live")
    if limits["available"] and limits["effective"]:
        effective = limits["effective"]
        lines.append(
            "limits     context %s, turn %ss, mail %s, grace %ss (%s)" % (
                effective["context_max_tokens"], effective["turn_max_seconds"],
                effective["turn_mail_max"], effective["turn_grace_seconds"],
                effective["session_policy"]))
    else:
        lines.append("limits     unavailable from this host")
    if budget["available"]:
        lines.append(
            "budget     %s: %.2f/%.2f $ (%s), %s" % (
                budget["account_profile"] or "?",
                budget["used_usd"] or 0.0, budget["limit_usd"] or 0.0,
                budget["period"] or "?", "admitted"
                if budget["admitted"] else budget["reason"] or "refused"))
    else:
        lines.append("budget     unavailable (%s)" % (budget["reason"] or "?"))
    turn = document["native"]["turn"]
    sandbox = document["native"]["sandbox"]
    lines.append("turn       %s" % (turn["turn_id"] if turn else "none"))
    lines.append("sandbox    %s" % (sandbox["turn_id"] if sandbox else "none"))
    if document["missing"]:
        lines.append("missing    %s" % ", ".join(document["missing"]))
    return "\n".join(lines)


def cmd_observe(cfg, args) -> int:
    from . import agent_observation
    from .mesh_cli import _open

    db = _open(dataclasses.replace(cfg, read_only=True))
    try:
        document = agent_observation.observe(
            db, args.agent, cfg=cfg, account_profile=args.account_profile)
    except agent_observation.Unknown as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_UNKNOWN
    finally:
        db.close()
    print(json.dumps(document, ensure_ascii=False) if args.json
          else _text(document))
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Ajoute les options de la commande au parseur `agent observe`."""
    parser.add_argument("agent", type=_agent_name, help="agent to observe")
    parser.add_argument(
        "--account-profile", type=_account_name, default=None,
        metavar="NAME",
        help="declared account profile whose envelope is reported; otherwise "
             "the session's recorded account, then the single declared profile")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_observe)
