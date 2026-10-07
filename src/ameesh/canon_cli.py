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
  ameesh agent spawn <nom> --by <créateur> --ttl <durée> [--cwd DIR] [--prompt T]

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

UNTRUSTED_TAG = "[NON APPROUVÉ] "


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
            "ERREUR" if f.severity == canon_mod.ERROR
            else "info." if f.severity == canon_mod.INFO else "avert.",
            f.code, f.where(), f.message))


def _summary(findings: list[canon_mod.Finding]) -> str:
    errors = len(canon_mod.errors(findings))
    infos = sum(1 for f in findings if f.severity == canon_mod.INFO)
    return "%d erreur(s), %d avertissement(s)%s" % (
        errors, len(findings) - errors - infos,
        ", %d information(s)" % infos if infos else "")


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


def _recorded(cfg: Config, host: str, canon: canon_mod.Canon | None = None) -> dict | None:
    """{"state": état enregistré ou None, "placements": verdicts écrits au
    registre pour l'hôte (`placement.recorded`), ou None si illisibles}, ou
    None sans base (check marche sans base)."""
    try:
        db = db_mod.connect(cfg)
    except db_mod.DbError:
        return None
    try:
        out = {"state": canon_sync.state(db, host, canon), "placements": None}
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
            print("registre  : %-16s %s" % (name, rows[name]["placement_diagnostic"]))


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
    if st.get("auth_status"):
        line += "\nauthentificateurs : %s%s (contrôle du %s)%s" % (
            "EN ERREUR — " if st["auth_status"] == canon_sync.AUTH_STATE_ERROR else "",
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
    findings = canon_mod.validate(canon)
    errors = canon_mod.errors(findings)
    status, diagnostic = canon_sync.assess(canon, host, findings)
    recorded = _recorded(cfg, host, canon)
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
    print(("canon valide — " if not errors
           else "canon ILLISIBLE — " if not canon.readable
           else "canon INVALIDE — ") + _summary(findings))
    print(_status_line(host, status, diagnostic, recorded=False))
    print(_recorded_line(host, recorded))
    if canon.readable:
        _print_placements(host, [v for v in placements if v.host == host])
    _print_recorded_placements(recorded)
    return 1 if errors else 0


def _print_placements(host: str, verdicts: list[placement_mod.Verdict]) -> None:
    """Placements de l'hôte (C4) : admis, ou refusés et pourquoi."""
    if not verdicts:
        print("placements sur %s : aucun" % host)
        return
    refused = [v for v in verdicts if not v.ok]
    print("placements sur %s : %d admis, %d refusé(s)%s" % (
        host, len(verdicts) - len(refused), len(refused),
        " (non réclamables ; ameesh placement check --agent A pour les hôtes admissibles)"
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
        print("périmètre  : %s" % " ; ".join(
            ["%s → %s%s" % (member, ", ".join(s or "." for s in dirs),
                            " (%d fiche(s) typée(s) hors périmètre ignorée(s))"
                            % canon.out_of_scope[member]
                            if canon.out_of_scope.get(member) else "")
             for member, dirs in sorted(canon.scopes.items())]
            + ["%s → NON LU (périmètre invalide ou absent)" % member
               for member in sorted(canon.scope_unread)]))
    print("membres    : %s" % (", ".join(
        "%s (%s)" % (m.title, _join(m.roles)) for m in canon.members) or "—"))
    print("hôtes      :")
    for h in canon.hosts:
        p = h.policy
        print("  %-14s %-18s harnais %s ; fournisseurs %s ; modes %s ; max %s%s" % (
            h.title, h.responsible or "SANS RESPONSABLE",
            "tous" if p.harnesses is None else _join(p.harnesses),
            "tous" if p.providers is None else _join(p.providers),
            "tous" if p.credential_modes is None else _join(p.credential_modes),
            "—" if p.max_agents is None else p.max_agents,
            " ; étiquettes %s" % _join(h.tags) if h.tags else ""))
    print("agents     :")
    for a in canon.agents:
        hosts = placement_mod.admitted_hosts(canon, placement_mod.admission_of(canon, a.title))
        print("  %-14s %-18s %-12s %s/%s/%s [%s] → %s" % (
            a.title, a.responsible or "SANS RESPONSABLE", a.team or "—",
            a.harness or "?", a.provider or "?", a.credential_mode or "?",
            _join(a.capabilities), _join(hosts)))
    print("admissions :")
    for pl in canon.placements:
        cibles = "%s%s" % (
            _join(pl.host_names()),
            " +étiquettes %s" % _join(pl.tags()) if pl.tags() else "")
        print("  %-14s → %-24s %-13s%s" % (
            pl.agent or "?", cibles or "—", pl.credential_mode or "—",
            " (cwd ignoré : %s)" % pl.cwd if pl.cwd else ""))
    print("constats   : %s (détail : ameesh canon check)" % _summary(findings))
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
                print("canon sync refusé : %s" % exc, file=sys.stderr)
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
    print("%scanon sync sur %s — %s" % (tag, host, report.source))
    if not report.actions:
        print("  aucun agent placé sur %s" % host)
    for action in report.actions:
        print("%s  %-16s %-14s %s" % (tag, action.agent, action.action, action.detail))
        for reason in action.blocked:
            print("%s  %-16s   bloqué par %s" % (tag, "", reason))
    refused = [a.agent for a in report.actions
               if a.action in ("créé", "mis à jour", "inchangé")
               and a.placement is not None and not a.placement.ok]
    if refused:
        print("%splacement refusé sur %s : %s — non réclamable(s) ; hôtes admissibles : "
              "ameesh placement check --agent A" % (tag, host, ", ".join(refused)))
    print(_status_line(host, report.status, report.diagnostic, recorded=True))
    _print_packages(report.packages)
    _print_authenticators(report.authenticators)
    if report.errors:
        print("canon INVALIDE — %s : les agents concernés ne sont pas réclamables "
              "(ameesh canon check)" % _summary(report.findings))
        return 1
    return 1 if auth_errors else 0


def _print_packages(done) -> None:
    """Le plan de travail (L29) recopié par la synchronisation."""
    if done is None:
        return
    parts = ["%d %s" % (len(ids), label) for ids, label in (
        (done.created, "créée(s)"), (done.updated, "mise(s) à jour"),
        (done.unchanged, "inchangée(s)"), (done.retired, "retirée(s)"),
        (done.skipped, "en erreur, non écrite(s)"),
        (getattr(done, "conflicts", []), "déclarée(s) par un autre canon, non écrite(s)"))
        if ids]
    if not parts:
        return
    print("plan (WorkPackage) : %s%s" % (
        ", ".join(parts),
        " ; %d lot(s) suivent le nouveau parent de leur fiche" % done.relinked
        if done.relinked else ""))
    if done.skipped:
        print("  en erreur : %s (ameesh canon check)" % ", ".join(done.skipped))


def _print_authenticators(done: canon_sync.AuthenticatorSync | None) -> None:
    """Le registre des authentificateurs (§8.2) après la synchronisation."""
    if done is None:
        return
    if done.branch:
        print("authentificateurs : branche canonique de confiance %s (%s)" % (
            done.branch, canon_sync.TRUST_LABELS.get(done.trust, done.trust)))
    for note in done.notes:
        print("authentificateurs : %s" % note)
    if done.status != canon_sync.AUTH_SYNCED:
        print("authentificateurs : %s%s" % (
            "REFUSÉ — " if done.status == canon_sync.AUTH_REFUSED else "", done.reason))
        return
    data = done.to_dict()
    print("authentificateurs : ajoutés %s ; mis à jour %s ; révoqués %s ; inchangés %d%s" % (
        _join(data["added"]), _join(data["updated"]), _join(data["revoked"]),
        data["unchanged"], " ; GELÉS %s" % _join(data["frozen"]) if data["frozen"] else ""))
    if done.partial:
        print("authentificateurs : canon lu en partie (%s) : aucune révocation pour absence"
              % "; ".join(done.partial))
    for error in done.errors:
        print("authentificateurs : ERREUR %s" % error)


def _modes(modes) -> str:
    return "tous" if modes is None else (_join(modes) if modes else "aucun")


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
        print("canon ILLISIBLE (%s) : aucun placement évaluable" % canon.root)
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
        print("canon INVALIDE — %s : les agents concernés ne sont pas réclamables "
              "(ameesh canon check)" % _summary(findings))
    for entry in entries:
        name = entry["agent"]
        if not entry["known"]:
            print("%sagent %s : aucune fiche Agent dans le canon" % (tag, name))
            continue
        print("%sagent %s — harnais %s, fournisseur %s, modèle %s, mode %s" % (
            tag, name, entry["harness"] or "?", entry["provider"] or "?",
            entry["model"] or "—", entry["credential_mode"] or "?"))
        if not entry["placements"]:
            print("  placement : aucun — non réclamable (agent sans placement)")
        for current in entry["placements"]:
            verdict = placement_mod.Verdict(
                name, current["host"], current["placement_ok"],
                current["placement_diagnostic"], current["placement_ref"],
                current["credential_mode"])
            print("  placement %-12s %s%s" % (
                current["host"], verdict.describe(),
                "" if verdict.ok else " → non réclamable sur %s" % current["host"]))
        admissible = entry["admissible"]
        print("  admissibles :%s" % ("" if admissible else " aucun hôte du canon"))
        for choice in admissible:
            print("    %-14s mode %s (admis : %s)%s" % (
                choice["host"], choice["credential_mode"] or "à fixer",
                _modes(choice["credential_modes"]),
                " — placement actuel" if choice["current"] else ""))
        if entry["refused"]:
            print("  refusés :")
        for choice in entry["refused"]:
            print("    %-14s %s" % (choice["host"], " ; ".join(choice["reasons"])))
    if refused:
        print("%d placement(s) refusé(s) : ameesh ne déplace aucun agent — changez la "
              "fiche Placement (ou la politique de l'hôte) par PR sur le canon, puis "
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
                canon_sync.AUTH_SKIPPED, "canon illisible : registre inchangé").to_dict(),
        }
        if sink is not None:
            data.update(canon_id=canon.id, default=canon.is_default)
            sink.append(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 1
    print("canon sync sur %s — canon ILLISIBLE (%s) : registre inchangé (agents et "
          "authentificateurs)" % (exc.host, canon.root))
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
    if row.get("placement_ok") is False:
        print("placement refusé (non réclamable) : %s"
              % (row.get("placement_diagnostic") or "?"))
    return 0


def _canon_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--canon", default=None,
                        help="identifiant d'un canon configuré (L42 ; sinon tous pour "
                             "check/show/sync, le canon par défaut ailleurs), ou racine "
                             "d'un canon (sinon AMEESH_CANON)")
    parser.add_argument("--ref", default=None,
                        help="révision canonique (sinon AMEESH_CANON_REF, le ref du "
                             "manifeste, origin/main)")
    parser.add_argument("--fetch", action="store_true", help="git fetch avant la lecture")
    parser.add_argument("--json", action="store_true")


def add_parsers(sub) -> None:
    """Branche `canon …`, `placement …` et `agent …` sur le parseur de mesh_cli."""
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
    p_sync.add_argument("--bootstrap-ref", default=None, metavar="BRANCHE",
                        help="premier amorçage du registre des authentificateurs seulement "
                             "(ni AMEESH_CANON_REF, ni commit déjà appliqué) : branche "
                             "canonique de confiance (origin/<BRANCHE>), journalisée ; "
                             "ignorée ensuite")
    p_sync.set_defaults(func=cmd_sync)

    p_place = sub.add_parser("placement", help="placement gouverné (C4) : check")
    place_sub = p_place.add_subparsers(dest="placement_command")
    p_pcheck = place_sub.add_parser(
        "check", help="placements admis ou refusés, et placements admissibles (lecture seule)")
    _canon_options(p_pcheck)
    p_pcheck.add_argument("--agent", default=None, help="un seul agent (sinon tous)")
    p_pcheck.set_defaults(func=cmd_placement_check)

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
