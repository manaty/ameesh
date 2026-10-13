# SPDX-License-Identifier: AGPL-3.0-only
"""Commandes du canon (spec §4) et des agents éphémères.

  ameesh canon check [--host H] [--json] validation (§4.3) ; code 1 si erreur ;
                                         état du canon pour l'hôte (ok | invalid |
                                         unreadable), état enregistré au registre,
                                         placements de l'hôte admis ou refusés (C4)
  ameesh canon show [--json]             fiches lues, et d'où (commit)
  ameesh canon sync [--host H] [--json] [--bootstrap-ref BRANCHE]
                                         canon → registre de l'hôte (§4.4) ; enregistre
                                         l'état du canon (canon_state), même illisible,
                                         et le verdict de placement de chaque agent ;
                                         recopie Member.authenticators dans le registre
                                         de confiance (§8.2) ; code 1 si erreur
  ameesh placement check [--agent A] [--json]
                                         admissions actuelles (hôtes admis, verdicts
                                         de placement) et hôtes admissibles (hôtes,
                                         modes d'identifiants) ; lecture seule, code 1
                                         si une admission est refusée
  ameesh hosts [--json] [HÔTE] [--history N]
                                         ressources des hôtes (L31) : dernier relevé,
                                         seuils, pression et historique court
  ameesh agent spawn [nom] --by <créateur> --ttl <durée> [--role R] [--project P …]
                                         [--cwd DIR] [--prompt T] ; sans nom, il est
                                         engendré par `extensions.ameesh.naming`
                                         (L243, #468)
  ameesh agent name --role R [--project P …] [--json]
                                         prochain nom libre <org>-<repo>-<role>-<seq>
                                         (L243, #468) : fiches, registre et rôles du
                                         nommage du canon
  ameesh canon agent spawn|name …        mêmes commandes sous `canon` (L243, #468) ;
                                         la place canonique du nommage

Options communes du canon : `--canon DOSSIER` (sinon AMEESH_CANON), `--ref REV`
(sinon AMEESH_CANON_REF, le `ref` du manifeste, origin/main), `--fetch`.

Plusieurs canons (L42, décision 0031) : `canon check`, `show` et `sync`
traitent par défaut TOUS les canons configurés (`canons` / AMEESH_CANONS), le
canon par défaut d'abord ; `--canon <id>` en choisit un par l'identifiant de
sa fédération (un dossier reste accepté, lu comme canon par défaut). Avec un
seul canon, la sortie est inchangée. `--ref` ne vise que le canon par défaut.
L43 : `placement check` aussi ; le bloc d'un canon ne montre que SES agents
(verdicts évalués et verdicts enregistrés au registre), et `canon show` son
périmètre ameesh (`ameesh: {scope: …}` de federation.yaml).

Registre des authentificateurs (§8.2) : `--ref` choisit ce qui est LU, jamais
ce qui est canonique. La branche canonique de confiance est celle de la
configuration de l'hôte (AMEESH_CANON_REF / `canon_ref`), sinon celle du
manifeste du dernier commit appliqué (journal en base), sinon — premier
amorçage — `--bootstrap-ref BRANCHE` (journalisé) ; sans rien de cela, refus.
L44 (0031) : chaque canon a son registre et son journal ; la branche de
confiance configurée d'un canon qui n'est pas le canon par défaut est la `ref`
de SON entrée `canons` (AMEESH_CANONS n'en donne pas : amorçage par
`canon sync --canon <id> --bootstrap-ref BRANCHE`). `--bootstrap-ref` ne sert
qu'aux canons dont le registre n'est pas encore amorcé.
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
from . import placement as placement_mod
from .config import Config

UNTRUSTED_TAG = "[NOT APPROVED] "

#: L156d : libellés anglais des actions de `canon_sync` à l'affichage ; le
#: JSON (`--json`, `SyncAction.to_dict`) garde les valeurs stockées
ACTION_LABELS = {
    "créé": "created", "mis à jour": "updated", "inchangé": "unchanged",
    "conflit": "conflict", "laissé": "left", "déplacé": "moved",
    "réintégré": "revived", "arrêté": "stopped", "arrêt demandé": "stop requested",
    "hors canon": "off canon",
}


def _load(cfg: Config, args: argparse.Namespace) -> canon_mod.Canon:
    if getattr(args, "ref", None):
        cfg = dataclasses.replace(cfg, canon_ref=args.ref)
    wanted = getattr(args, "canon", None)
    if wanted and len(canon_mod.configured(cfg)) > 1 and not os.path.isdir(wanted):
        return _load_all(cfg, args)[0]
    return canon_mod.from_config(cfg, root=wanted,
                                 fetch=bool(getattr(args, "fetch", False)))


def _load_all(cfg: Config, args: argparse.Namespace) -> list[canon_mod.Canon]:
    """Les canons visés (L42, 0031) : tous les canons configurés, ou celui de
    `--canon <id>` ; un `--canon DOSSIER` (forme historique) est lu comme
    canon par défaut. Un seul canon configuré : exactement `_load`."""
    if getattr(args, "ref", None):
        cfg = dataclasses.replace(cfg, canon_ref=args.ref)
    fetch = bool(getattr(args, "fetch", False))
    wanted = getattr(args, "canon", None)
    entries = canon_mod.configured(cfg)
    if wanted and (os.path.isdir(wanted) or not entries):
        return [canon_mod.from_config(cfg, root=wanted, fetch=fetch)]
    if len(entries) <= 1 and not wanted:
        return [canon_mod.from_config(cfg, fetch=fetch)]
    canons = canon_mod.from_config_all(cfg, fetch=fetch)
    if not wanted:
        return canons
    chosen = [c for c in canons if c.id == wanted]
    if chosen:
        return chosen
    # ni un identifiant configuré, ni un dossier : la forme historique (racine
    # absente → canon illisible, « canon-missing », diagnostiqué comme avant)
    return [canon_mod.from_config(cfg, root=wanted, fetch=fetch)]


def _header(canon: canon_mod.Canon, many: bool) -> None:
    if many:
        print("== canon %s ==" % canon.label())


def load_canon(cfg: Config, args: argparse.Namespace) -> canon_mod.Canon:
    """Charge le canon selon les options communes (`--canon`, `--ref`, `--fetch`)."""
    return _load(cfg, args)


def _print_findings(findings: list[canon_mod.Finding]) -> None:
    for f in findings:
        print("%s%-8s %-30s %s — %s" % (
            UNTRUSTED_TAG if f.untrusted else "",
            "ERROR" if f.severity == canon_mod.ERROR
            else "info." if f.severity == canon_mod.INFO else "warn.",
            f.code, f.where(), f.message))


def _summary(findings: list[canon_mod.Finding]) -> str:
    errors = len(canon_mod.errors(findings))
    infos = sum(1 for f in findings if f.severity == canon_mod.INFO)
    return "%d error(s), %d warning(s)%s" % (
        errors, len(findings) - errors - infos,
        ", %d info(s)" % infos if infos else "")


def _moment(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else "—"


def _status_line(host: str, status: str, diagnostic: str, *, recorded: bool) -> str:
    """Le statut du canon pour un hôte, et ce qu'il implique pour la réclamation."""
    if status == canon_sync.CANON_OK:
        return "canon state for %s: ok%s" % (host, " — %s" % diagnostic if diagnostic else "")
    return ("canon state for %s: %s — %s\n  → %scanon agents (and their ephemerals) "
            "NOT claimable on %s; leases, sessions and running turns left intact"
            % (host, status, diagnostic or "?",
               "" if recorded else "after ameesh canon sync: ", host))


def _recorded(cfg: Config, host: str, canon: canon_mod.Canon | None = None) -> dict | None:
    """{"state": état enregistré ou None, "placements": verdicts écrits au
    registre pour l'hôte (`placement.recorded`), ou None si illisibles}, ou
    None sans base (check marche sans base)."""
    try:
        db = db_mod.connect(cfg)
    except db_mod.DbError:
        return None
    try:
        out = {"state": canon_sync.state(db, host, canon), "placements": None,
               "projects": None}
        try:
            out["projects"] = canon_sync.known_projects(db)
        except db_mod.DbError:
            pass                        # base pas encore migrée
        try:
            rows = placement_mod.recorded(db, host)
            # L43 (0031) : dans le bloc d'un canon, seuls SES agents (colonne
            # `canon` du registre, NULL = canon par défaut ; les éphémères
            # portent le canon de leur créateur)
            out["placements"] = rows if canon is None else {
                name: row for name, row in rows.items() if canon.owns(row.get("canon"))}
        except db_mod.DbError:
            pass                        # base pas encore migrée (0022)
        return out
    except db_mod.DbError:
        return None
    finally:
        db.close()


def _print_recorded_placements(recorded: dict | None) -> None:
    """Verdicts du registre qui ne valent plus : profil divergé depuis l'évaluation."""
    rows = (recorded or {}).get("placements") or {}
    for name in sorted(rows):
        if rows[name].get("placement_profile_ok") is False:
            print("registry  : %-16s %s" % (name, rows[name]["placement_diagnostic"]))


def _recorded_line(host: str, recorded: dict | None) -> str:
    if recorded is None:
        return "registry  : recorded state unknown (database unreachable or not migrated)"
    st = recorded["state"]
    if st is None:
        return ("registry  : no canon state for %s (ameesh canon sync): canon agents "
                "not claimable" % host)
    line = "registry  : %s for %s (checked %s), last valid commit %s (%s)" % (
        st["status"], host, _moment(st.get("checked_ts")),
        (st.get("last_good_commit") or "—")[:12], _moment(st.get("last_good_ts")))
    if st["status"] != canon_sync.CANON_OK:
        line += (" — %s\n  → canon agents not claimable on %s until an "
                 "\"ameesh canon sync\" reads a valid canon" % (st.get("diagnostic") or "?",
                                                                    host))
    if st.get("auth_status"):
        line += "\nauthenticators : %s%s (checked %s)%s" % (
            "IN ERROR — " if st["auth_status"] == canon_sync.AUTH_STATE_ERROR else "",
            st["auth_status"], _moment(st.get("auth_checked_ts")),
            " — %s" % st["auth_diagnostic"] if st.get("auth_diagnostic") else "")
    return line


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    canons = _load_all(cfg, args)
    if len(canons) == 1:
        return _check_one(cfg, args, canons[0])
    # L42 (0031) : un bloc par canon ; en JSON, {"canons": [...]}
    code, out = 0, []
    for canon in canons:
        if args.json:
            code = max(code, _check_one(cfg, args, canon, sink=out))
        else:
            _header(canon, True)
            code = max(code, _check_one(cfg, args, canon))
    if args.json:
        print(json.dumps({"ok": code == 0, "canons": out}, ensure_ascii=False, indent=2))
    return code


def _check_one(cfg: Config, args: argparse.Namespace, canon: canon_mod.Canon,
               sink: list | None = None) -> int:
    host = getattr(args, "host", None) or cfg.host
    recorded = _recorded(cfg, host, canon)
    # L243 : les projets inconnus ne se vérifient qu'avec la base (lots)
    findings = canon_mod.validate(canon, (recorded or {}).get("projects"))
    errors = canon_mod.errors(findings)
    status, diagnostic = canon_sync.assess(canon, host, findings)
    placements = placement_mod.verdicts(canon) if canon.readable else []
    if args.json:
        data = {
            "ok": not errors, "root": canon.root, "untrusted": canon.untrusted,
            "readable": canon.readable, "host": host,
            "canon_status": status, "diagnostic": diagnostic,
            "registry_state": recorded["state"] if recorded else None,
            # C4 : verdicts écrits au registre pour l'hôte, confrontés au
            # profil courant (placement_profile_ok faux : profil divergé)
            "registry_placements": [
                dict(row, agent=name)
                for name, row in sorted(((recorded or {}).get("placements") or {}).items())],
            "sources": [s.to_dict() for s in canon.sources],
            "errors": len(errors),
            "warnings": sum(1 for f in findings if f.severity == canon_mod.WARNING),
            "infos": sum(1 for f in findings if f.severity == canon_mod.INFO),
            "findings": [f.to_dict() for f in findings],
            "placements": [v.to_dict() for v in placements],
            "canon_id": canon.id, "default": canon.is_default,
        }
        if sink is not None:
            sink.append(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    print("canon : %s" % canon.root)
    print("source : %s%s" % (UNTRUSTED_TAG if canon.untrusted else "", canon.source_label()))
    _print_findings(findings)
    print(("canon valid — " if not errors
           else "canon UNREADABLE — " if not canon.readable
           else "canon INVALID — ") + _summary(findings))
    print(_status_line(host, status, diagnostic, recorded=False))
    print(_recorded_line(host, recorded))
    if canon.readable:
        _print_placements(host, [v for v in placements if v.host == host])
    _print_recorded_placements(recorded)
    return 1 if errors else 0


def _print_placements(host: str, verdicts: list[placement_mod.Verdict]) -> None:
    """Placements de l'hôte (C4) : admis, ou refusés et pourquoi."""
    if not verdicts:
        print("placements on %s: none" % host)
        return
    refused = [v for v in verdicts if not v.ok]
    print("placements on %s: %d admitted, %d refused%s" % (
        host, len(verdicts) - len(refused), len(refused),
        " (not claimable; ameesh placement check --agent A for admissible hosts)"
        if refused else ""))
    for verdict in verdicts:
        print("  %-16s %s" % (verdict.agent, verdict.describe()))


def _join(values) -> str:
    return ",".join(values) if values else "—"


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    canons = _load_all(cfg, args)
    if len(canons) > 1 and args.json:
        out = []
        for canon in canons:
            data = canon.to_dict()
            data["findings"] = [f.to_dict() for f in canon_mod.validate(canon)]
            out.append(data)
        print(json.dumps({"canons": out}, ensure_ascii=False, indent=2))
        return 0
    for canon in canons:
        _header(canon, len(canons) > 1)
        _show_one(canon, args)
    return 0


def _show_one(canon: canon_mod.Canon, args: argparse.Namespace) -> int:
    findings = canon_mod.validate(canon)
    if args.json:
        data = canon.to_dict()
        data["findings"] = [f.to_dict() for f in findings]
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print("canon      : %s" % canon.root)
    print("source     : %s%s" % (UNTRUSTED_TAG if canon.untrusted else "", canon.source_label()))
    if canon.scopes or canon.scope_unread:
        # L43 (0031) : périmètre ameesh d'un canon partagé
        print("scope      : %s" % "; ".join(
            ["%s → %s%s" % (member, ", ".join(s or "." for s in dirs),
                            " (%d typed card(s) outside the scope ignored)"
                            % canon.out_of_scope[member]
                            if canon.out_of_scope.get(member) else "")
             for member, dirs in sorted(canon.scopes.items())]
            + ["%s → NOT READ (scope invalid or missing)" % member
               for member in sorted(canon.scope_unread)]))
    print("members    : %s" % (", ".join(
        "%s (%s)" % (m.title, _join(m.roles)) for m in canon.members) or "—"))
    print("hosts      :")
    for h in canon.hosts:
        p = h.policy
        print("  %-14s %-18s harnesses %s; providers %s; modes %s; max %s%s" % (
            h.title, h.responsible or "NO RESPONSIBLE",
            "all" if p.harnesses is None else _join(p.harnesses),
            "all" if p.providers is None else _join(p.providers),
            "all" if p.credential_modes is None else _join(p.credential_modes),
            "—" if p.max_agents is None else p.max_agents,
            "; tags %s" % _join(h.tags) if h.tags else ""))
    print("agents     :")
    for a in canon.agents:
        hosts = placement_mod.admitted_hosts(canon, placement_mod.admission_of(canon, a.title))
        print("  %-14s %-18s %-12s %s/%s/%s [%s] → %s" % (
            a.title, a.responsible or "NO RESPONSIBLE", a.team or "—",
            a.harness or "?", a.provider or "?", a.credential_mode or "?",
            _join(a.capabilities), _join(hosts)))
    print("admissions :")
    for pl in canon.placements:
        cibles = "%s%s" % (
            _join(pl.host_names()),
            " +tags %s" % _join(pl.tags()) if pl.tags() else "")
        print("  %-14s → %-24s %-13s%s" % (
            pl.agent or "?", cibles or "—", pl.credential_mode or "—",
            " (cwd ignored: %s)" % pl.cwd if pl.cwd else ""))
    print("findings   : %s (details: ameesh canon check)" % _summary(findings))
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    host = args.host or cfg.host
    canons = _load_all(cfg, args)
    many = len(canons) > 1
    db = db_mod.connect(cfg)
    code, out = 0, []
    try:
        db_mod.require_schema(db)
        if many and not getattr(args, "canon", None):
            try:
                canon_sync.adopt_default(db, host, canons)
            except canon_sync.DefaultCanonError as exc:
                # L46 : aucun canon n'est synchronisé — le nouveau canon par
                # défaut prendrait les lignes de l'ancien pour les siennes
                print("canon sync refused: %s" % exc, file=sys.stderr)
                return 1
        for canon in canons:
            if not args.json:
                _header(canon, many)
            code = max(code, _sync_one(db, cfg, args, canon, host,
                                       sink=out if (many and args.json) else None))
    finally:
        db.close()
    if many and args.json:
        print(json.dumps({"host": host, "canons": out}, ensure_ascii=False, indent=2))
    return code


def _sync_one(db, cfg: Config, args: argparse.Namespace, canon: canon_mod.Canon,
              host: str, sink: list | None = None) -> int:
    try:
        # registre des authentificateurs : la branche canonique de confiance
        # vient de la configuration de l'hôte POUR CE CANON (L44 : cfg.canon_ref
        # pour le canon par défaut, la `ref` de son entrée `canons` sinon ;
        # jamais --ref), du dernier commit appliqué de ce canon, ou de
        # --bootstrap-ref au premier amorçage
        report = canon_sync.sync(db, canon, host,
                                 trusted_ref=canon_sync.configured_ref_for(cfg, canon),
                                 bootstrap_ref=args.bootstrap_ref or "")
    except canon_sync.CanonUnreadable as exc:
        return _print_unreadable(args, canon, exc, sink=sink)
    auth_errors = report.authenticators.errors if report.authenticators else []
    if args.json:
        data = report.to_dict()
        if sink is not None:
            data.update(canon_id=canon.id, default=canon.is_default)
            sink.append(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 1 if report.errors or auth_errors else 0
    tag = UNTRUSTED_TAG if report.untrusted else ""
    print("%scanon sync on %s — %s" % (tag, host, report.source))
    if not report.actions:
        print("  no agent placed on %s" % host)
    for action in report.actions:
        print("%s  %-16s %-14s %s" % (tag, action.agent,
                                      ACTION_LABELS.get(action.action, action.action),
                                      action.detail))
        for reason in action.blocked:
            print("%s  %-16s   blocked by %s" % (tag, "", reason))
    refused = [a.agent for a in report.actions
               if a.action in ("created", "updated", "unchanged",
                               "créé", "mis à jour", "inchangé")  # canon_sync, either language
               and a.placement is not None and not a.placement.ok]
    if refused:
        print("%splacement refused on %s: %s — not claimable; admissible hosts: "
              "ameesh placement check --agent A" % (tag, host, ", ".join(refused)))
    print(_status_line(host, report.status, report.diagnostic, recorded=True))
    _print_packages(report.packages)
    _print_authenticators(report.authenticators)
    if report.errors:
        print("canon INVALID — %s: the affected agents are not claimable "
              "(ameesh canon check)" % _summary(report.findings))
        return 1
    return 1 if auth_errors else 0


def _print_packages(done) -> None:
    """Le plan de travail (L29) recopié par la synchronisation."""
    if done is None:
        return
    parts = ["%d %s" % (len(ids), label) for ids, label in (
        (done.created, "created"), (done.updated, "updated"),
        (done.unchanged, "unchanged"), (done.retired, "retired"),
        (done.skipped, "in error, not written"),
        (getattr(done, "conflicts", []), "declared by another canon, not written"))
        if ids]
    if not parts:
        return
    print("plan (WorkPackage) : %s%s" % (
        ", ".join(parts),
        "; %d lot(s) follow the new parent of their card" % done.relinked
        if done.relinked else ""))
    if done.skipped:
        print("  in error: %s (ameesh canon check)" % ", ".join(done.skipped))


def _print_authenticators(done: canon_sync.AuthenticatorSync | None) -> None:
    """Le registre des authentificateurs (§8.2) après la synchronisation."""
    if done is None:
        return
    if done.branch:
        print("authenticators : trusted canonical branch %s (%s)" % (
            done.branch, canon_sync.TRUST_LABELS.get(done.trust, done.trust)))
    for note in done.notes:
        print("authenticators : %s" % note)
    if done.status != canon_sync.AUTH_SYNCED:
        print("authenticators : %s%s" % (
            "REFUSED — " if done.status == canon_sync.AUTH_REFUSED else "", done.reason))
        return
    data = done.to_dict()
    print("authenticators : added %s; updated %s; revoked %s; unchanged %d%s" % (
        _join(data["added"]), _join(data["updated"]), _join(data["revoked"]),
        data["unchanged"], "; FROZEN %s" % _join(data["frozen"]) if data["frozen"] else ""))
    if done.partial:
        print("authenticators : canon partly read (%s): no revocation for absence"
              % "; ".join(done.partial))
    for error in done.errors:
        print("authenticators : ERROR %s" % error)


def _modes(modes) -> str:
    return "all" if modes is None else (_join(modes) if modes else "none")


def cmd_placement_check(cfg: Config, args: argparse.Namespace) -> int:
    """C4 : placements actuels (admis ou refusés) et placements admissibles.

    Lecture seule : ni le canon ni le registre ne sont modifiés, aucun agent
    n'est déplacé. Code 1 si le canon est illisible, si l'agent demandé n'a
    pas de fiche, ou si un placement actuel est refusé.

    L43 (0031) : plusieurs canons configurés — un bloc par canon (en JSON,
    `{"canons": [...]}`), chacun avec SES agents jugés par SA fiche Host ;
    `--canon <id>` en choisit un ; `--agent A` ne garde que le(s) canon(s) qui
    le déclarent. Un seul canon : sortie inchangée.
    """
    canons = _load_all(cfg, args)
    if getattr(args, "agent", None) and len(canons) > 1:
        # L42 (0031) : l'agent est cherché dans tous les canons configurés
        found = [c for c in canons if c.agent(args.agent) is not None]
        canons = found or canons[:1]
    if len(canons) == 1:
        return _placement_one(args, canons[0])
    code, out = 0, []
    for canon in canons:
        if not args.json:
            _header(canon, True)
        code = max(code, _placement_one(args, canon, sink=out if args.json else None))
    if args.json:
        print(json.dumps({"canons": out}, ensure_ascii=False, indent=2))
    return code


def _placement_one(args: argparse.Namespace, canon: canon_mod.Canon,
                   sink: list | None = None) -> int:
    """`placement check` pour UN canon (ses agents seulement)."""
    findings = canon_mod.validate(canon)
    errors = canon_mod.errors(findings)

    def emit(data: dict) -> None:
        if sink is not None:
            data.update(canon_id=canon.id, default=canon.is_default)
            sink.append(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))

    if not canon.readable:
        if args.json:
            emit({"root": canon.root, "readable": False,
                  "untrusted": canon.untrusted, "agents": [],
                  "findings": [f.to_dict() for f in findings]})
            return 1
        print("canon UNREADABLE (%s): no placement can be evaluated" % canon.root)
        _print_findings(findings)
        return 1
    entries = placement_mod.report(canon, args.agent, findings)
    unknown = [e["agent"] for e in entries if not e["known"]]
    refused = [(e["agent"], p) for e in entries for p in e["placements"]
               if not p["placement_ok"]]
    code = 1 if unknown or refused else 0
    if args.json:
        emit({
            "root": canon.root, "readable": True, "untrusted": canon.untrusted,
            "source": canon.source_label(), "errors": len(errors),
            "refused": len(refused), "agents": entries,
        })
        return code
    tag = UNTRUSTED_TAG if canon.untrusted else ""
    print("canon : %s" % canon.root)
    print("source : %s%s" % (tag, canon.source_label()))
    if errors:
        print("canon INVALID — %s: the affected agents are not claimable "
              "(ameesh canon check)" % _summary(findings))
    for entry in entries:
        name = entry["agent"]
        if not entry["known"]:
            print("%sagent %s: no Agent card in the canon" % (tag, name))
            continue
        print("%sagent %s — harness %s, provider %s, model %s, mode %s" % (
            tag, name, entry["harness"] or "?", entry["provider"] or "?",
            entry["model"] or "—", entry["credential_mode"] or "?"))
        if not entry["placements"]:
            print("  placement: none — not claimable (agent without placement)")
        for current in entry["placements"]:
            verdict = placement_mod.Verdict(
                name, current["host"], current["placement_ok"],
                current["placement_diagnostic"], current["placement_ref"],
                current["credential_mode"])
            print("  placement %-12s %s%s" % (
                current["host"], verdict.describe(),
                "" if verdict.ok else " → not claimable on %s" % current["host"]))
        admissible = entry["admissible"]
        print("  admissible:%s" % ("" if admissible else " no canon host"))
        for choice in admissible:
            print("    %-14s mode %s (admitted: %s)%s" % (
                choice["host"], choice["credential_mode"] or "to be set",
                _modes(choice["credential_modes"]),
                " — current placement" if choice["current"] else ""))
        if entry["refused"]:
            print("  refused:")
        for choice in entry["refused"]:
            print("    %-14s %s" % (choice["host"], "; ".join(choice["reasons"])))
    if refused:
        print("%d placement(s) refused: ameesh moves no agent — change the "
              "Placement card (or the host policy) by PR on the canon, then "
              "ameesh canon sync" % len(refused))
    return code


def _print_unreadable(args: argparse.Namespace, canon: canon_mod.Canon,
                      exc: canon_sync.CanonUnreadable, sink: list | None = None) -> int:
    """Canon illisible : seul l'état `unreadable` a été enregistré."""
    if args.json:
        data = {
            "host": exc.host, "source": "", "untrusted": canon.untrusted,
            "canon_status": canon_sync.CANON_UNREADABLE, "diagnostic": exc.diagnostic,
            "actions": [], "errors": len(canon_mod.errors(exc.findings)),
            "findings": [f.to_dict() for f in exc.findings],
            "authenticators": canon_sync.AuthenticatorSync(
                canon_sync.AUTH_SKIPPED, "canon unreadable: registry unchanged").to_dict(),
        }
        if sink is not None:
            data.update(canon_id=canon.id, default=canon.is_default)
            sink.append(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 1
    print("canon sync on %s — canon UNREADABLE (%s): registry unchanged (agents and "
          "authenticators)" % (exc.host, canon.root))
    _print_findings(exc.findings)
    print(_status_line(exc.host, canon_sync.CANON_UNREADABLE, exc.diagnostic, recorded=True))
    return 1


def _canon_read_key(canon: canon_mod.Canon) -> tuple:
    """(dossier réel, révision lue) : la clé de déduplication d'un canon (L243, #468).

    Deux lectures du MÊME dossier à deux révisions ne sont pas le même canon :
    `--canon DOSSIER` lit la révision par défaut, mais un canon configuré sur
    `origin/stable` réserve des noms que `origin/main` ignore. Dédupliquer sur
    le seul dossier perdait ces réservations (codex r2, #452)."""
    source = canon.sources[0] if canon.sources else None
    return (os.path.realpath(canon.root),
            (source.commit or source.rev) if source is not None else "")


def _configured_canons(cfg: Config, args: argparse.Namespace) -> list[canon_mod.Canon]:
    """Tous les canons configurés, le canon par défaut d'abord.

    L243 (#468) : un nom d'agent est unique pour TOUS les canons, donc les
    noms déjà pris se lisent partout ; `--canon` ne choisit que le nommage
    qui s'applique (voir `_naming_canon`). Un `--canon DOSSIER` n'est donc pas
    le SEUL canon lu : les canons configurés restent lus pour leurs
    réservations (sinon le générateur rendrait un nom déjà déclaré ailleurs).
    La déduplication se fait sur (dossier, révision) : un dossier configuré à
    deux révisions est lu deux fois, chacune pour ses réservations."""
    if getattr(args, "ref", None):
        cfg = dataclasses.replace(cfg, canon_ref=args.ref)
    fetch = bool(getattr(args, "fetch", False))
    entries = canon_mod.configured(cfg)
    wanted = getattr(args, "canon", None)
    if wanted and (os.path.isdir(wanted) or not entries):
        chosen = canon_mod.from_config(cfg, root=wanted, fetch=fetch)
        if not entries:
            return [chosen]
        merged = [chosen]
        seen = {_canon_read_key(chosen)}
        for canon in canon_mod.from_config_all(cfg, fetch=fetch):
            key = _canon_read_key(canon)
            if key in seen:                 # même dossier, même révision que `--canon`
                continue
            seen.add(key)
            merged.append(canon)
        return merged
    if len(entries) <= 1:
        return [canon_mod.from_config(cfg, fetch=fetch)]
    return canon_mod.from_config_all(cfg, fetch=fetch)


def _naming_canon(canons: list, wanted: str | None) -> canon_mod.Canon:
    """Le canon dont le nommage s'applique : `--canon <id>`, sinon le défaut."""
    if wanted and not os.path.isdir(wanted):
        chosen = next((c for c in canons if c.id == wanted), None)
        if chosen is None:
            raise canon_mod.CanonError("canon %r is not configured: %s"
                                       % (wanted, ", ".join(c.id for c in canons)))
        return chosen
    return canons[0]


def _taken_agent_names(canons: list, db) -> set:
    """Noms déjà pris : fiches de tous les canons, puis registre entier.

    Les agents ARRÊTÉS restent dans le registre : leur nom est réservé pour
    toujours (courrier, fils, lots et sessions continuent de le citer). Les
    alias de #423 s'ajouteront ici quand ils existeront."""
    names = {agent.title for canon in canons if canon.readable for agent in canon.agents}
    names |= {row["name"] for row in registry.overview(db) if row.get("name")}
    return names


def _require_readable(canons: list) -> None:
    """Toutes les réservations sont-elles lisibles ? (codex r3, #452)

    Un canon configuré mais ILLISIBLE (révision jamais récupérée, dépôt cassé)
    ne dit pas ses noms : proposer un nom supposerait qu'il est libre. On
    refuse, en nommant le canon et sa ref — que le canon illisible soit celui
    qui nomme ou un autre, ses réservations manqueraient dans les deux cas."""
    for canon in canons:
        if canon.readable:
            continue
        reasons = [f.message for f in canon.load_findings
                   if f.severity == canon_mod.ERROR]
        raise canon_mod.NamingError(
            "unreadable canon %s (%s) at ref %s: %s — its agent names are unknown, "
            "so no free name can be guaranteed (fetch or repair it first)"
            % (canon.id or "?", canon.root, canon.rev or "?",
               reasons[0] if reasons else "no source read"))


def _generated_name(naming, role: str | None, projects, canons: list, db) -> str:
    """Nom par défaut engendré par le nommage, ou refus explicite (L243, #468)."""
    _require_readable(canons)
    if naming is None:
        raise canon_mod.NamingError(
            "no `extensions.ameesh.naming` in the canon: declare org, projects and "
            "roles in its federation.yaml")
    if not role:
        raise canon_mod.NamingError("--role is required to generate a name")
    known = canon_mod.known_projects(canons, projects or [], naming)
    return naming.next_name(role, projects or [], _taken_agent_names(canons, db), known)


def cmd_agent_name(cfg: Config, args: argparse.Namespace) -> int:
    canons = _configured_canons(cfg, args)
    chosen = _naming_canon(canons, getattr(args, "canon", None))
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        name = _generated_name(chosen.naming, args.role, args.project, canons, db)
    finally:
        db.close()
    if args.json:
        print(json.dumps({"name": name, "org": chosen.naming.org, "role": args.role,
                          "projects": list(args.project or [])}, ensure_ascii=False))
    else:
        print(name)
    return 0


def cmd_spawn(cfg: Config, args: argparse.Namespace) -> int:
    if args.name and (args.role or args.project):
        print("refused: give a name OR --role/--project (the name is then generated "
              "from the canon's naming)", file=sys.stderr)
        return 1
    # Une session d'agent ne crée qu'en son propre nom : son identité est celle
    # que le runner a posée (et, sous bail, vérifiée), jamais celle qu'elle dit.
    if os.environ.get("AGENT_MAIL_NAME"):
        db = db_mod.connect(cfg)
        try:
            binding = identity.resolve_binding(cfg, db)
        finally:
            db.close()
        if not binding.ok or binding.name != args.by:
            print("refused: this session is %s; --by %s is not its identity"
                  % (binding.describe(), args.by), file=sys.stderr)
            return 1
    ttl = canon_sync.parse_ttl(args.ttl)
    # L42 (0031) : le nom de l'éphémère ne doit être déclaré par aucun canon
    canon: list = []
    for index, entry in enumerate(canon_mod.configured(cfg)):
        try:
            canon.append(canon_mod.from_config(cfg) if index == 0
                         else canon_mod.from_config(cfg, entry=entry))
        except canon_mod.CanonError:
            pass
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        name = args.name or _generated_name(
            canon[0].naming if canon else None, args.role, args.project, canon, db)
        row = canon_sync.spawn(db, name, args.by, ttl, cwd=args.cwd, canon=canon)
        if args.prompt:
            registry.set_pending_prompt(db, name, args.prompt)
    finally:
        db.close()
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0
    print("ephemeral agent %s created by %s: responsible %s, capabilities %s, host %s, "
          "expires %s" % (
              row["name"], row["created_by"], row["responsible"],
              _join(row.get("capabilities")), row.get("host") or "—",
              time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ephemeral_expires_ts"]))))
    if row.get("placement_ok") is False:
        print("placement refused (not claimable): %s"
              % (row.get("placement_diagnostic") or "?"))
    return 0


def _canon_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--canon", default=None,
                        help="id of a configured canon (L42; otherwise all for "
                             "check/show/sync, the default canon elsewhere), or root "
                             "of a canon (otherwise AMEESH_CANON)")
    parser.add_argument("--ref", default=None,
                        help="canonical revision (otherwise AMEESH_CANON_REF, the manifest "
                             "ref, origin/main)")
    parser.add_argument("--fetch", action="store_true", help="git fetch before reading")
    parser.add_argument("--json", action="store_true")


def _add_agent_parsers(parent) -> None:
    """`agent spawn|name` (L243, #468), branché aux DEUX endroits.

    `ameesh canon agent …` est la place canonique (le nommage est une
    propriété du canon) ; `ameesh agent …` reste branché pour les scripts et
    la documentation existants. Codex r1 (#452) : le premier chemin manquait."""
    p_agent = parent.add_parser("agent", help="ephemeral agents, and names (L243)")
    agent_sub = p_agent.add_subparsers(dest="agent_command")
    p_spawn = agent_sub.add_parser(
        "spawn", help="create an ephemeral (responsible inherited from the creator, read/propose)")
    p_spawn.add_argument("name", nargs="?", default=None,
                         help="name; omitted, generated from `extensions.ameesh.naming`")
    p_spawn.add_argument("--by", required=True, help="creating agent")
    p_spawn.add_argument("--ttl", required=True, help="expiry: 30m, 2h, 1d (max 7d)")
    p_spawn.add_argument("--role", default=None,
                         help="role of a generated name (L243, #468)")
    p_spawn.add_argument("--project", action="append", default=None,
                         help="project of a generated name; repeat for several (L243)")
    p_spawn.add_argument("--cwd", default=None, help="directory (otherwise the creator's)")
    p_spawn.add_argument("--prompt", default=None, help="first prompt")
    p_spawn.add_argument("--json", action="store_true")
    p_spawn.set_defaults(func=cmd_spawn)

    p_name = agent_sub.add_parser(
        "name", help="next free agent name <org>-<repo>-<role>-<seq> (L243, #468)")
    _canon_options(p_name)
    p_name.add_argument("--role", required=True, help="role declared in naming.roles")
    p_name.add_argument("--project", action="append", default=None,
                        help="project the agent works on; repeat for a multi-project agent")
    p_name.set_defaults(func=cmd_agent_name)


def add_parsers(sub) -> None:
    """Branche `canon …` (dont `canon agent …`), `placement …` et `agent …`."""
    p_canon = sub.add_parser("canon", help="OKF canon: check, show, sync")
    canon_sub = p_canon.add_subparsers(dest="canon_command")
    p_check = canon_sub.add_parser("check", help="validate the canon (§4.3)")
    _canon_options(p_check)
    p_check.add_argument("--host", default=None,
                         help="host whose state is diagnosed (otherwise AMEESH_HOST / this "
                              "machine)")
    p_check.set_defaults(func=cmd_check)
    p_cshow = canon_sub.add_parser("show", help="cards read and their source")
    _canon_options(p_cshow)
    p_cshow.set_defaults(func=cmd_show)
    p_sync = canon_sub.add_parser("sync", help="canon → host registry (§4.4)")
    _canon_options(p_sync)
    p_sync.add_argument("--host", default=None, help="host (otherwise AMEESH_HOST / this machine)")
    p_sync.add_argument("--bootstrap-ref", default=None, metavar="BRANCH",
                        help="first bootstrap of the authenticator registry only "
                             "(no AMEESH_CANON_REF, no commit applied yet): trusted "
                             "canonical branch (origin/<BRANCH>), logged; "
                             "ignored afterwards")
    p_sync.set_defaults(func=cmd_sync)
    _add_agent_parsers(canon_sub)

    p_place = sub.add_parser("placement", help="governed placement (C4): check")
    place_sub = p_place.add_subparsers(dest="placement_command")
    p_pcheck = place_sub.add_parser(
        "check", help="admitted or refused placements, and admissible placements (read-only)")
    _canon_options(p_pcheck)
    p_pcheck.add_argument("--agent", default=None, help="a single agent (otherwise all)")
    p_pcheck.set_defaults(func=cmd_placement_check)

    _add_agent_parsers(sub)
