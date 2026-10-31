# SPDX-License-Identifier: AGPL-3.0-only
"""Commandes `ameesh veille` (#3264 A, #4291) : veille des versions.

  ameesh veille scan [--dry-run] [--json] [--no-probe] [--harness H …]
        lit les versions installées de cet hôte, les versions publiées (npm ou
        releases), les modèles servis et facturés, puis entretient un lot
        d'évaluation par harnais. `--dry-run` montre sans écrire.

La minuterie utilisateur `ameesh-veille.timer` appelle `scan` une fois par
jour. `veille.host` peut désigner l'hôte qui réconcilie les lots ; les autres
hôtes ne font que relever leurs versions installées.
"""
from __future__ import annotations

import json
import sys


def _fmt_decision(report: dict, harness: str) -> dict | None:
    for action in report.get("actions") or ():
        if action.get("harness") == harness:
            return action
    return None


def _line(entry: dict, action: dict | None) -> str:
    harness = entry["harness"]
    if entry.get("error"):
        return "%s: %s" % (harness, entry["error"])
    decision = entry.get("decision") or {}
    installed = (entry.get("installed") or {}).get("newest") or "inconnue"
    candidate = decision.get("candidate") or {}
    if candidate:
        published = "%s (%s)" % (candidate.get("version") or "?",
                                 candidate.get("date") or "date inconnue")
    elif (entry.get("published") or {}).get("error"):
        published = "illisible"
    else:
        published = "aucune plus récente"
    if action is None:
        result = "rien"
    elif action["action"] in ("created", "would-create"):
        result = "lot %s%s (P%s)" % (
            "#%s " % action.get("lot") if action.get("lot") else "",
            "à créer" if action["action"] == "would-create" else "créé",
            decision.get("priority") or 2)
    elif action["action"] in ("updated", "would-update"):
        result = "lot #%s %s (P%s)" % (
            action.get("lot"), "à mettre à jour"
            if action["action"] == "would-update" else "mis à jour",
            decision.get("priority") or 2)
    elif action["action"] == "unchanged":
        result = "lot #%s inchangé" % action.get("lot")
    elif action["action"] == "already-evaluated":
        result = "déjà évalué, rien à ouvrir"
    elif action["action"] == "none":
        result = "aucun lot (%s)" % (action.get("reason") or "à jour")
    else:
        result = action["action"]
    return "%s: installé %s ; publié %s ; %s" % (harness, installed, published, result)


def cmd_veille(cfg, args) -> int:
    from .platform.child_environment import managed_entry
    if not managed_entry():
        return 1
    from . import veille
    from .mesh_cli import _open

    if getattr(args, "veille_command", None) != "scan":
        print("usage: ameesh veille scan [--dry-run] [--json] [--no-probe] "
              "[--harness H …]", file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        report = veille.scan(
            cfg, db, sources=getattr(args, "harness", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            probe=not bool(getattr(args, "no_probe", False)))
    finally:
        db.close()
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1 if report.get("errors") else 0
    if not report.get("enabled"):
        print("veille: off (no `veille.sources` in the configuration of this host)")
        return 0
    if not report.get("reconciled"):
        print("veille: probe only (%s)" % (report.get("reason") or "not reconciling"))
    for entry in report.get("sources") or ():
        line = _line(entry, _fmt_decision(report, entry["harness"]))
        if entry.get("error"):
            print(line, file=sys.stderr)
        else:
            print(line)
    for error in report.get("errors") or ():
        if not any(entry.get("error") and entry["error"] in error
                   for entry in report.get("sources") or ()):
            print("veille: %s" % error, file=sys.stderr)
    # une erreur persistante (document npm trop gros, source illisible) ne
    # doit pas rester invisible : la minuterie sort en échec et le journal dit
    # pourquoi ; le prochain passage réessaie (N de l'auditeur sur #4291).
    return 1 if report.get("errors") else 0


def add_parsers(sub) -> None:
    """Branche `veille …` sur le parseur de `mesh_cli`."""
    parser = sub.add_parser(
        "veille", help="#3264 A: daily harness and model version watch")
    commands = parser.add_subparsers(dest="veille_command")
    scan = commands.add_parser(
        "scan", help="read published/installed versions and reconcile evaluation lots")
    scan.add_argument("--dry-run", action="store_true",
                      help="read and decide, write nothing")
    scan.add_argument("--json", action="store_true",
                      help="schema ameesh-veille/1 report")
    scan.add_argument("--no-probe", action="store_true",
                      help="do not read this host's installed versions again")
    scan.add_argument("--harness", action="append", default=None, metavar="ID",
                      help="only this harness (repeatable)")
    scan.set_defaults(func=cmd_veille)
    parser.set_defaults(func=cmd_veille)
