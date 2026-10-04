# SPDX-License-Identifier: AGPL-3.0-only
"""Commandes du canon (spec §4) et des agents éphémères.

  ameesh canon check [--host H] [--json] validation (§4.3) ; code 1 si erreur ;
                                         état du canon pour l'hôte (ok | invalid |
                                         unreadable) et état enregistré au registre
  ameesh canon show [--json]             fiches lues, et d'où (commit)
  ameesh canon sync [--host H] [--json]  canon → registre de l'hôte (§4.4) ; enregistre
                                         l'état du canon (canon_state), même illisible
  ameesh agent spawn <nom> --by <créateur> --ttl <durée> [--cwd DIR] [--prompt T]

Options communes du canon : `--canon DOSSIER` (sinon AMEESH_CANON), `--ref REV`
(sinon AMEESH_CANON_REF, le `ref` du manifeste, origin/main), `--fetch`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time

from . import canon as canon_mod
from . import canon_sync, db as db_mod, identity, registry
from .config import Config

UNTRUSTED_TAG = "[NON APPROUVÉ] "


def _load(cfg: Config, args: argparse.Namespace) -> canon_mod.Canon:
    if getattr(args, "ref", None):
        cfg = dataclasses.replace(cfg, canon_ref=args.ref)
    return canon_mod.from_config(cfg, root=getattr(args, "canon", None),
                                 fetch=bool(getattr(args, "fetch", False)))


def _print_findings(findings: list[canon_mod.Finding]) -> None:
    for f in findings:
        print("%s%-8s %-30s %s — %s" % (
            UNTRUSTED_TAG if f.untrusted else "",
            "ERREUR" if f.severity == canon_mod.ERROR else "avert.",
            f.code, f.where(), f.message))


def _summary(findings: list[canon_mod.Finding]) -> str:
    errors = len(canon_mod.errors(findings))
    return "%d erreur(s), %d avertissement(s)" % (errors, len(findings) - errors)


def _moment(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else "—"


def _status_line(host: str, status: str, diagnostic: str, *, recorded: bool) -> str:
    """Le statut du canon pour un hôte, et ce qu'il implique pour la réclamation."""
    if status == canon_sync.CANON_OK:
        return "état du canon pour %s : ok%s" % (host, " — %s" % diagnostic if diagnostic else "")
    return ("état du canon pour %s : %s — %s\n  → %sagents du canon (et leurs éphémères) "
            "NON réclamables sur %s ; baux, sessions et tours en cours intacts"
            % (host, status, diagnostic or "?",
               "" if recorded else "après ameesh canon sync : ", host))


def _recorded(cfg: Config, host: str) -> dict | None:
    """{"state": état enregistré ou None}, ou None sans base (check marche sans base)."""
    try:
        db = db_mod.connect(cfg)
    except db_mod.DbError:
        return None
    try:
        return {"state": canon_sync.state(db, host)}
    except db_mod.DbError:
        return None
    finally:
        db.close()


def _recorded_line(host: str, recorded: dict | None) -> str:
    if recorded is None:
        return "registre  : état enregistré inconnu (base injoignable ou non migrée)"
    st = recorded["state"]
    if st is None:
        return ("registre  : aucun état du canon pour %s (ameesh canon sync) : agents du "
                "canon non réclamables" % host)
    line = "registre  : %s pour %s (contrôle du %s), dernier commit valide %s (%s)" % (
        st["status"], host, _moment(st.get("checked_ts")),
        (st.get("last_good_commit") or "—")[:12], _moment(st.get("last_good_ts")))
    if st["status"] != canon_sync.CANON_OK:
        line += (" — %s\n  → agents du canon non réclamables sur %s jusqu'à un "
                 "« ameesh canon sync » qui lit un canon valide" % (st.get("diagnostic") or "?",
                                                                    host))
    return line


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    canon = _load(cfg, args)
    host = getattr(args, "host", None) or cfg.host
    findings = canon_mod.validate(canon)
    errors = canon_mod.errors(findings)
    status, diagnostic = canon_sync.assess(canon, host, findings)
    recorded = _recorded(cfg, host)
    if args.json:
        print(json.dumps({
            "ok": not errors, "root": canon.root, "untrusted": canon.untrusted,
            "readable": canon.readable, "host": host,
            "canon_status": status, "diagnostic": diagnostic,
            "registry_state": recorded["state"] if recorded else None,
            "sources": [s.to_dict() for s in canon.sources],
            "errors": len(errors), "warnings": len(findings) - len(errors),
            "findings": [f.to_dict() for f in findings],
        }, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    print("canon : %s" % canon.root)
    print("source : %s%s" % (UNTRUSTED_TAG if canon.untrusted else "", canon.source_label()))
    _print_findings(findings)
    print(("canon valide — " if not errors
           else "canon ILLISIBLE — " if not canon.readable
           else "canon INVALIDE — ") + _summary(findings))
    print(_status_line(host, status, diagnostic, recorded=False))
    print(_recorded_line(host, recorded))
    return 1 if errors else 0


def _join(values) -> str:
    return ",".join(values) if values else "—"


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    canon = _load(cfg, args)
    findings = canon_mod.validate(canon)
    if args.json:
        data = canon.to_dict()
        data["findings"] = [f.to_dict() for f in findings]
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print("canon      : %s" % canon.root)
    print("source     : %s%s" % (UNTRUSTED_TAG if canon.untrusted else "", canon.source_label()))
    print("membres    : %s" % (", ".join(
        "%s (%s)" % (m.title, _join(m.roles)) for m in canon.members) or "—"))
    print("hôtes      :")
    for h in canon.hosts:
        p = h.policy
        print("  %-14s %-18s harnais %s ; fournisseurs %s ; modes %s ; max %s" % (
            h.title, h.responsible or "SANS RESPONSABLE",
            "tous" if p.harnesses is None else _join(p.harnesses),
            "tous" if p.providers is None else _join(p.providers),
            "tous" if p.credential_modes is None else _join(p.credential_modes),
            "—" if p.max_agents is None else p.max_agents))
    print("agents     :")
    for a in canon.agents:
        hosts = [pl.host or "?" for pl in canon.placements_of(a.title)]
        print("  %-14s %-18s %-12s %s/%s/%s [%s] → %s" % (
            a.title, a.responsible or "SANS RESPONSABLE", a.team or "—",
            a.harness or "?", a.provider or "?", a.credential_mode or "?",
            _join(a.capabilities), _join(hosts)))
    print("placements :")
    for pl in canon.placements:
        print("  %-14s → %-12s %-13s %s" % (
            pl.agent or "?", pl.host or "?", pl.credential_mode or "—", pl.cwd or "—"))
    print("constats   : %s (détail : ameesh canon check)" % _summary(findings))
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    host = args.host or cfg.host
    canon = _load(cfg, args)
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        try:
            report = canon_sync.sync(db, canon, host)
        except canon_sync.CanonUnreadable as exc:
            return _print_unreadable(args, canon, exc)
    finally:
        db.close()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 1 if report.errors else 0
    tag = UNTRUSTED_TAG if report.untrusted else ""
    print("%scanon sync sur %s — %s" % (tag, host, report.source))
    if not report.actions:
        print("  aucun agent placé sur %s" % host)
    for action in report.actions:
        print("%s  %-16s %-14s %s" % (tag, action.agent, action.action, action.detail))
        for reason in action.blocked:
            print("%s  %-16s   bloqué par %s" % (tag, "", reason))
    print(_status_line(host, report.status, report.diagnostic, recorded=True))
    if report.errors:
        print("canon INVALIDE — %s : les agents concernés ne sont pas réclamables "
              "(ameesh canon check)" % _summary(report.findings))
        return 1
    return 0


def _print_unreadable(args: argparse.Namespace, canon: canon_mod.Canon,
                      exc: canon_sync.CanonUnreadable) -> int:
    """Canon illisible : seul l'état `unreadable` a été enregistré."""
    if args.json:
        print(json.dumps({
            "host": exc.host, "source": "", "untrusted": canon.untrusted,
            "canon_status": canon_sync.CANON_UNREADABLE, "diagnostic": exc.diagnostic,
            "actions": [], "errors": len(canon_mod.errors(exc.findings)),
            "findings": [f.to_dict() for f in exc.findings],
        }, ensure_ascii=False, indent=2))
        return 1
    print("canon sync sur %s — canon ILLISIBLE (%s) : registre inchangé"
          % (exc.host, canon.root))
    _print_findings(exc.findings)
    print(_status_line(exc.host, canon_sync.CANON_UNREADABLE, exc.diagnostic, recorded=True))
    return 1


def cmd_spawn(cfg: Config, args: argparse.Namespace) -> int:
    # Une session d'agent ne crée qu'en son propre nom : son identité est celle
    # que le runner a posée (et, sous bail, vérifiée), jamais celle qu'elle dit.
    if os.environ.get("AGENT_MAIL_NAME"):
        db = db_mod.connect(cfg)
        try:
            binding = identity.resolve_binding(cfg, db)
        finally:
            db.close()
        if not binding.ok or binding.name != args.by:
            print("refus : cette session est %s ; --by %s n'est pas son identité"
                  % (binding.describe(), args.by), file=sys.stderr)
            return 1
    ttl = canon_sync.parse_ttl(args.ttl)
    canon = None
    if cfg.canon:
        try:
            canon = canon_mod.from_config(cfg)
        except canon_mod.CanonError:
            canon = None
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        row = canon_sync.spawn(db, args.name, args.by, ttl, cwd=args.cwd, canon=canon)
        if args.prompt:
            registry.set_pending_prompt(db, args.name, args.prompt)
    finally:
        db.close()
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0
    print("agent éphémère %s créé par %s : responsable %s, capacités %s, hôte %s, "
          "échéance %s" % (
              row["name"], row["created_by"], row["responsible"],
              _join(row.get("capabilities")), row.get("host") or "—",
              time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ephemeral_expires_ts"]))))
    return 0


def _canon_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--canon", default=None, help="racine du canon (sinon AMEESH_CANON)")
    parser.add_argument("--ref", default=None,
                        help="révision canonique (sinon AMEESH_CANON_REF, le ref du "
                             "manifeste, origin/main)")
    parser.add_argument("--fetch", action="store_true", help="git fetch avant la lecture")
    parser.add_argument("--json", action="store_true")


def add_parsers(sub) -> None:
    """Branche `canon …` et `agent …` sur le parseur de mesh_cli."""
    p_canon = sub.add_parser("canon", help="canon OKF : check, show, sync")
    canon_sub = p_canon.add_subparsers(dest="canon_command")
    p_check = canon_sub.add_parser("check", help="valider le canon (§4.3)")
    _canon_options(p_check)
    p_check.add_argument("--host", default=None,
                         help="hôte dont l'état est diagnostiqué (sinon AMEESH_HOST / cette "
                              "machine)")
    p_check.set_defaults(func=cmd_check)
    p_cshow = canon_sub.add_parser("show", help="fiches lues et leur source")
    _canon_options(p_cshow)
    p_cshow.set_defaults(func=cmd_show)
    p_sync = canon_sub.add_parser("sync", help="canon → registre de l'hôte (§4.4)")
    _canon_options(p_sync)
    p_sync.add_argument("--host", default=None, help="hôte (sinon AMEESH_HOST / cette machine)")
    p_sync.set_defaults(func=cmd_sync)

    p_agent = sub.add_parser("agent", help="agents éphémères")
    agent_sub = p_agent.add_subparsers(dest="agent_command")
    p_spawn = agent_sub.add_parser(
        "spawn", help="créer un éphémère (responsable hérité du créateur, read/propose)")
    p_spawn.add_argument("name")
    p_spawn.add_argument("--by", required=True, help="agent créateur")
    p_spawn.add_argument("--ttl", required=True, help="échéance : 30m, 2h, 1d (max 7d)")
    p_spawn.add_argument("--cwd", default=None, help="dossier (sinon celui du créateur)")
    p_spawn.add_argument("--prompt", default=None, help="première consigne")
    p_spawn.add_argument("--json", action="store_true")
    p_spawn.set_defaults(func=cmd_spawn)
