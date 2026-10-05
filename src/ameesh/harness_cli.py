# SPDX-License-Identifier: AGPL-3.0-only
"""Commandes `ameesh harness` (L16, R22) : descripteurs de harnais.

  ameesh harness list [--json]        descripteurs connus (paquet + hôte) ;
                                      signale sur stderr ceux qui sont ignorés
  ameesh harness show <id|fichier> [--json]
                                      le descripteur, ses clés et ses dérivés
  ameesh harness check <fichier> [--json]
                                      validation du manifeste ACP étendu ;
                                      code 1 si erreur bloquante

Aucune base n'est nécessaire : ce sont des fichiers. Le dossier de l'hôte est
`$AMEESH_HARNESSES_DIR` (sinon `~/.config/ameesh/harnesses/`) ; il prime sur les
descripteurs livrés dans le paquet pour un même `id`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import harnesses
from .harnesses import ERROR, Finding


def _print_findings(findings: list[Finding]) -> None:
    for f in findings:
        print("%s%-32s %s" % ("ERREUR " if f.severity == ERROR else "avert. ",
                              f.code, f.message))


def _summary(findings: list[Finding]) -> str:
    errors = len(harnesses.errors(findings))
    return "%d erreur(s), %d avertissement(s)" % (errors, len(findings) - errors)


def cmd_list(cfg, args: argparse.Namespace) -> int:
    known, findings = harnesses.scan()
    if args.json:
        print(json.dumps([
            {"id": d.id, "name": d.name, "version": d.version,
             "schema_version": d.schema_version, "description": d.description,
             "protocol": d.protocol, "stream": d.stream, "binary": d.binary,
             "source": d.source, "path": d.path,
             "paid_per_token": d.paid_per_token, "attach": d.attach}
            for d in known.values()
        ], ensure_ascii=False))
    else:
        for ident in sorted(known):
            d = known[ident]
            print("%-14s %-10s %-20s %-8s %s" % (
                d.id, d.protocol, d.stream or "-", d.source, d.path))
    for f in findings:
        prefix = "descripteur ignoré : " if f.severity == ERROR else "avertissement : "
        print("%s%s" % (prefix, f), file=sys.stderr)
    return 1 if harnesses.errors(findings) else 0


def _show_json(descriptor: harnesses.HarnessDescriptor) -> None:
    """JSON stable : le document, sa provenance et son empreinte (audit)."""
    print(json.dumps({
        "descriptor": descriptor.as_json(),
        "source": descriptor.source,
        "path": descriptor.path,
        "sha256": descriptor.sha256,
    }, ensure_ascii=False, indent=2))


def _show_text(descriptor: harnesses.HarnessDescriptor) -> None:
    print("%s — %s" % (descriptor.id, descriptor.name))
    print("version        : %s (schéma %s)" % (descriptor.version,
                                               descriptor.schema_version))
    print("source         : %s (%s)" % (descriptor.source, descriptor.path))
    print("empreinte      : sha256 %s" % (descriptor.sha256 or "?"))
    print("description    : %s" % descriptor.description)
    if descriptor.repository:
        print("dépôt          : %s" % descriptor.repository)
    if descriptor.license:
        print("licence        : %s" % descriptor.license)
    print("protocole      : %s" % descriptor.protocol)
    print("binaire        : %s%s" % (
        descriptor.binary,
        " (env : %s)" % ", ".join(descriptor.binary_env) if descriptor.binary_env else ""))
    print("commande       : %s" % (" ".join(descriptor.command) or "—"))
    if descriptor.attach:
        print("interactif     : %s" % (" ".join(descriptor.interactive) or "—"))
    else:
        print("interactif     : non déclaré (attach indisponible)")
    print("session        : %s" % (" ".join(descriptor.session_flag) or "— (ACP)"))
    print("flux           : %s" % descriptor.stream)
    for key in ("model", "effort", "tier"):
        setting = descriptor.settings()[key]
        if setting.flag:
            print("%-14s : %s" % (key, " ".join(setting.flag)))
        if setting.file:
            print("%-14s : fichier gabarit (drapeau %s)" % (key, " ".join(setting.flag)))
        if setting.default:
            print("%-14s : défaut %s" % (key, setting.default))
    if descriptor.env:
        print("environnement  : %s" % ", ".join(
            "%s=%s" % (k, v) for k, v in sorted(descriptor.env.items())))
    if descriptor.capabilities:
        print("capacités ACP  : %s" % ", ".join(sorted(descriptor.capabilities)))
    if descriptor.authentication:
        print("authentification : %s" % json.dumps(descriptor.authentication, ensure_ascii=False))
    if descriptor.hooks:
        print("hooks          : %s" % ", ".join(sorted(descriptor.hooks)))
    if descriptor.cost:
        print("coût           : %s" % json.dumps(descriptor.cost, ensure_ascii=False))
    if descriptor.permissions:
        print("permissions    : %s" % json.dumps(descriptor.permissions, ensure_ascii=False))
    if descriptor.distribution:
        print("distribution   : %s" % ", ".join(sorted(descriptor.distribution)))
    else:
        print("distribution   : non décrite (installation par l'hôte, L17)")


def cmd_show(cfg, args: argparse.Namespace) -> int:
    target = args.identifiant
    if os.path.sep in target or target.endswith(".json"):
        descriptor, findings = harnesses.check_path(os.path.abspath(os.path.expanduser(target)))
        if descriptor is None:
            _print_findings(findings)
            return 1
    else:
        descriptor = harnesses.get(target)
        if descriptor is None:
            print("descripteur inconnu : %r (connus : %s)"
                  % (target, ", ".join(harnesses.known_ids()) or "aucun"), file=sys.stderr)
            return 1
        findings = harnesses.validate(descriptor.document, path=descriptor.path)
    if args.json:
        _show_json(descriptor)
        for f in findings:
            print("avert. %-32s %s" % (f.code, f.message), file=sys.stderr)
    else:
        _show_text(descriptor)
        for f in findings:
            print("avert. %-32s %s" % (f.code, f.message))
    return 0


def cmd_check(cfg, args: argparse.Namespace) -> int:
    path = os.path.abspath(os.path.expanduser(args.fichier))
    descriptor, findings = harnesses.check_path(path)
    if args.json:
        print(json.dumps({
            "path": path,
            "valid": descriptor is not None,
            "id": descriptor.id if descriptor else None,
            "findings": [{"code": f.code, "severity": f.severity, "message": f.message}
                         for f in findings],
        }, ensure_ascii=False))
    else:
        if descriptor is not None:
            print("%s : descripteur valide (%s, protocole %s, flux %s)"
                  % (path, descriptor.id, descriptor.protocol, descriptor.stream))
        _print_findings(findings)
        print(_summary(findings))
    return 1 if descriptor is None else 0


def add_parsers(sub) -> None:
    """Branche `harness …` sur le parseur de mesh_cli."""
    p_harness = sub.add_parser("harness", help="descripteurs de harnais (L16)")
    harness_sub = p_harness.add_subparsers(dest="harness_command")

    p_list = harness_sub.add_parser("list", help="descripteurs connus (paquet + hôte)")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = harness_sub.add_parser("show", help="un descripteur, par identifiant ou fichier")
    p_show.add_argument("identifiant")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_check = harness_sub.add_parser("check", help="valider un fichier de descripteur")
    p_check.add_argument("fichier")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)
