#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""agent-mesh — la CLI du mesh : observabilité, clés, approbations, lots.

  agent-mesh list [--json]                     mesh list : agents, projet, hôte, bail, non lus,
                                                budget, regroupés par projet (L62) ; statut
                                                préfixé « ext/ » : agent externe (L37)
  agent-mesh show <agent> [--json]             détail d'un agent
  agent-mesh key generate --out DIR --i-am-the-owner   paire de clés (acte du propriétaire)
  agent-mesh key register <agent> --public-key FICHIER [--role owner|agent]
  agent-mesh key show <agent> | key list | key revoke <agent>
  agent-mesh approve --key FICHIER --action A --hash H [--kind K] [--expires 48h]
                                                signe une approbation liée à un artefact
  agent-mesh approvals [--action A] [--hash H] [--json]
  agent-mesh verify --action A --hash H [--consume] [--by NOM]
                                                une approbation valide existe-t-elle ?
  agent-mesh cost report | cost spent <agent|all> [s] | cost over <agent>
                                                coût des tours et jauges de forfait
  agent-mesh work add --title T [--type bug|evolution] [--app A] [--assignee N] …
  agent-mesh work list [--state S] | work show <id> | work move <id> <état> | work note <id> "…"
  agent-mesh work assign <id> <agent> [--externe] [--actor A]   réassigner (attribution gardée)
  agent-mesh import-v0 [--agents a,b] [--dry-run]     bascule : boîte fichier v0 → Postgres
  agent-mesh export-v0 [--agents a,b] [--keep]        retour arrière : Postgres → boîte v0
  agent-mesh migrate | doctor [--notify-test | --probe]
  agent-mesh canon check|show|sync [--json] [--host H]   canon OKF (spec §4)
  agent-mesh placement check [--agent A] [--json]        admissions et hôtes admissibles (C4)
  agent-mesh hosts [--json] [HÔTE] [--history N]         ressources des hôtes (L31)
  agent-mesh agent spawn <nom> --by <créateur> --ttl 2h   agent éphémère (R14)

L'autorité du propriétaire ne se déduit jamais d'un texte : elle se prouve par
une signature Ed25519 dont la clé publique est dans agent_registry, liée au
contenu et à une échéance. `key generate` est un acte du propriétaire : aucun
agent ne doit générer ni détenir sa clé privée.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time

from . import accounts as accounts_mod
from . import authority, catalog, mail, config as config_mod, cost as cost_mod, db as db_mod, identity
from . import migrations, placement, registry
from . import fil, signing, storage, work
from .config import Config
from .db import Db

USAGE_HINT = "agent-mesh: %s"


def _open(cfg: Config) -> Db:
    # L61 : la vérification de schéma part avec la première requête
    return db_mod.open_db(cfg)


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "il y a %ds" % seconds
    if seconds < 3600:
        return "il y a %dm" % (seconds // 60)
    if seconds < 86400:
        return "il y a %dh" % (seconds // 3600)
    return "il y a %dj" % (seconds // 86400)


def _fmt_span(seconds: float | None) -> str:
    """Une durée (pas un âge) : 42s, 12m, 3h05, 2j."""
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02d" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dj" % (seconds // 86400)


#: unités binaires, du plus grand au plus petit
_BYTE_UNITS = (("TiB", 1024 ** 4), ("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024))


def _fmt_bytes(value: int | None) -> str:
    """Une taille lisible : « 3.2 GiB », « 0 B », « — » si inconnue."""
    if value is None:
        return "—"
    value = int(value)
    for label, factor in _BYTE_UNITS:
        if abs(value) >= factor:
            return "%.1f %s" % (value / factor, label)
    return "%d B" % value



def _fmt_moment(epoch: float | None) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _budget(row: dict) -> str:
    budget = row.get("budget_usd")
    spent = row.get("spent_usd")
    if budget is None:
        return "—" if not spent else "%.4f/—" % float(spent)
    return "%.4f/%.2f" % (float(spent or 0), float(budget))


def _lease(row: dict) -> str:
    owner = row.get("lease_owner")
    if not owner:
        return "—"
    remaining = float(row.get("lease_expires_ts") or 0) - time.time()
    return "%s (%ds)" % (owner[:18], max(0, int(remaining)))


# --------------------------------------------------------------------------
# mesh list / show  (spec §5 point 5)
# --------------------------------------------------------------------------

def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    # L61 : agents et lots préchargés en un seul aller-retour (db.prefetch) ;
    # le code ci-dessous lit ensuite ces réponses sans retourner à la base
    db = db_mod.prefetch(db, lambda d: (registry.overview(d), work.delays(d, limit=500)))
    try:
        rows = registry.overview(db)
        # L62 : le projet de chaque agent (équipe, à défaut chantier), en
        # colonne et en clé JSON ajoutée ; le tableau est regroupé par projet
        from .projects import agent_project, project_label
        for row in rows:
            row["project"] = agent_project(row)
        if args.json:
            # C4 : verdict de placement de chaque agent (0022)
            placement.annotate(db, rows)
            # L26 : champs d'exploitation (schéma `ameesh-agent/1`,
            # docs/EXPLOITATION.md), ajoutés sans retirer les clés existantes
            from . import exploitation
            exploitation.annotate(cfg, db, rows)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("aucun agent connu")
            return 0
        now = time.time()
        # Un seul balayage des lots pour toute la vue (L36) : le pied global
        # et le compte par agent en dérivent.
        lots = work.delays(db, limit=500)
        open_by_agent: dict[str, int] = {}
        for lot in lots:
            if lot.get("assignee") and lot.get("state") not in work.TERMINAL:
                open_by_agent[lot["assignee"]] = open_by_agent.get(lot["assignee"], 0) + 1
        # regroupé par projet (tri stable : dans un projet, l'ordre de
        # `overview`, vu le plus récemment d'abord) ; les sans-projet en dernier
        rows.sort(key=lambda r: (r["project"] is None, r["project"] or ""))
        print("%-20s %-12s %-9s %-10s %-11s %-22s %-8s %-5s %-13s %-4s %s" % (
            "NOM", "PROJET", "HARNAIS", "HÔTE", "STATUT", "BAIL", "NON LUS", "LOTS", "BUDGET",
            "CLÉ", "VU"))
        for row in rows:
            status = row.get("status") or "?"
            if row.get("status_text"):
                status = "%s/%s" % (status, row["status_text"][:22])
            if row.get("mode") == "externe":
                # L37 (0030) : session humaine, jamais réveillée par ameesh
                status = "ext/" + status
            print("%-20s %-12s %-9s %-10s %-11s %-22s %-8d %-5d %-13s %-4s %s" % (
                row["name"][:20], project_label(row["project"])[:12],
                (row.get("harness") or "?")[:9], (row.get("host") or "")[:10],
                status[:11], _lease(row), int(row.get("unread") or 0),
                open_by_agent.get(row["name"], 0), _budget(row),
                ("owner" if row.get("has_owner_key")
                 else "agent" if row.get("key_ready") else "—"),
                _fmt_age(now - float(row.get("last_seen_ts") or 0)),
            ))
        # Les lots sur la même vue (0018 point 5) : le compte par phase et ce
        # qui attend le plus, pour que `ameesh list` dise où en est le travail.
        if lots:
            counts: dict[str, int] = {}
            for lot in lots:
                name, _at = work.last_milestone(lot)
                counts[name] = counts.get(name, 0) + 1
            oldest = min((float(lot["frozen_ts"]) for lot in lots
                          if lot.get("frozen_ts") and not lot.get("reviewed_ts")),
                         default=None)
            detail = ", ".join("%s %d" % (name, counts[name])
                               for name, _at_key, _duration in reversed(work.MILESTONES)
                               if name in counts)
            waiting = " — plus ancien gel : %s" % _fmt_age(now - oldest) if oldest else ""
            print("lots     : %d (%s)%s" % (len(lots), detail, waiting))
        return 0
    finally:
        db.close()


def cmd_hosts(cfg: Config, args: argparse.Namespace) -> int:
    """`ameesh hosts [--json] [HOST]` : ressources des hôtes (L31, 0028).

    Le dernier relevé de chaque hôte connu (canon et/ou base), ses seuils
    (politique de la fiche `Host`, valeurs par défaut comprises), l'état de
    pression et l'historique court. En `--json`, un objet `ameesh-host/1` par
    ligne.

    L43 (0031) : seuils et `max_agents` sont les limites PHYSIQUES — les plus
    strictes des fiches Host de tous les canons — avec la provenance de
    chacune (`limits_origin`) et les fiches de chaque canon (`fiches`).
    """
    from . import canon as canon_mod
    from . import placement as placement_mod
    from . import resources as res

    db = _open(cfg)
    try:
        # L42 (0031) : les fiches Host de tous les canons configurés (le canon
        # par défaut d'abord)
        canons = canon_mod.load_configured(cfg)
        latest_by_host = {row["host"]: row for row in res.current(db)}
        names = set(latest_by_host)
        for canon in canons:
            names |= {h.title for h in canon.hosts}
        if args.host:
            names = {args.host}
        if not names:
            print("aucun hôte connu : ni mesure publiée, ni fiche Host au canon")
            return 0
        now = time.time()
        many = len(canons) > 1
        for name in sorted(names):
            # L43 (0031) : chaque canon décrit l'hôte par sa fiche Host ; les
            # limites physiques sont les plus strictes, avec leur provenance
            fiches = canon_mod.host_fiches(canons, name)
            host = fiches[0][1] if fiches else None
            physical = res.host_limits(canons, name)
            limits, origin = physical["limits"], physical["origin"]
            admissions = {
                canon.id: sum(1 for p in canon.placements if p.agent and name in
                              placement_mod.admitted_hosts(canon, p))
                for canon, _fiche in fiches}
            latest = latest_by_host.get(name)
            verdict = res.pressure(latest, limits=limits) if latest is not None else None
            history = res.history(db, name, args.history)
            if args.json:
                print(json.dumps({
                    "schema": "ameesh-host/1",
                    "host": name,
                    "responsible": host.responsible if host is not None else None,
                    "limits": limits,
                    "max_agents": physical["max_agents"],
                    "limits_origin": origin,
                    "fiches": [{"canon": canon.id, "default": canon.is_default,
                                "responsible": fiche.responsible,
                                "max_agents": fiche.policy.max_agents,
                                "admissions": admissions.get(canon.id, 0)}
                               for canon, fiche in fiches],
                    "latest": latest,
                    "pressure": verdict,
                    "history": history,
                }, ensure_ascii=False, sort_keys=True), flush=True)
                continue
            resp = ("responsable %s" % host.responsible) if host is not None and host.responsible \
                else "hôte sans fiche Host" if host is None else "responsable inconnu"
            if latest is None:
                print("%s — %s ; aucune mesure publiée" % (name, resp))
                _print_host_fiches(fiches, physical, admissions, many=many)
                continue
            age = _fmt_age(now - float(latest.get("sampled_ts") or now))
            print("%s — %s ; mesure %s" % (name, resp, age))
            _print_host_fiches(fiches, physical, admissions, many=many)

            def tag(key: str) -> str:
                return " [%s]" % origin.get(key, res.DEFAULT_ORIGIN) if fiches else ""

            print("  seuils     mémoire ≥ %s%s ; swap ≤ %s%s ; charge ≤ %.2f%s ; disque ≥ %s%s"
                  % (_fmt_bytes(limits["min_mem_available"]), tag("min_mem_available"),
                     _fmt_bytes(limits["max_swap_used"]), tag("max_swap_used"),
                     limits["max_load"], tag("max_load"),
                     _fmt_bytes(limits["min_disk_free"]), tag("min_disk_free")))
            print("  état       mémoire %s ; swap %s ; charge %s ; disque %s ; tours %s"
                  % (_fmt_bytes(latest.get("mem_available_bytes")),
                     _fmt_bytes(latest.get("swap_used_bytes")),
                     ("%.2f" % latest["load1"]) if latest.get("load1") is not None else "—",
                     _fmt_bytes(latest.get("disk_free_bytes")),
                     latest.get("turns_in_progress")
                     if latest.get("turns_in_progress") is not None else "—"))
            if latest.get("on_ac") is not None or latest.get("battery_percent") is not None:
                # L106 : alimentation d'un portable
                print("  alimentation %s%s ; seuils batterie %s %% (plus de tour), %s %% "
                      "(arrêt)" % (
                          "secteur" if latest.get("on_ac") else
                          "batterie" if latest.get("on_ac") is False else "inconnue",
                          " %s %%" % latest["battery_percent"]
                          if latest.get("battery_percent") is not None else "",
                          limits.get("min_battery_percent"),
                          limits.get("stop_battery_percent")))

            def valeur(key, value):
                if key == "max_load":
                    return "%.2f" % value
                if key in res.POWER_KEYS:
                    return "%s %%" % value
                return _fmt_bytes(value)

            if verdict and verdict["breaches"]:
                tag = "CRITIQUE" if verdict["critical"] else "PRESSION"
                detail = " ; ".join(
                    "%s %s (seuil %s)" % (b["label"], valeur(b["key"], b["value"]),
                                          valeur(b["key"], b["limit"]))
                    for b in verdict["breaches"])
                print("  %s  %s" % (tag, detail))
            else:
                print("  pression   aucune")
            if history:
                trace = " ".join(
                    "%s→%s" % (_fmt_bytes(row.get("mem_available_bytes")),
                               ("%.1f" % row["load1"]) if row.get("load1") is not None else "—")
                    for row in history)
                print("  historique  %d relevé(s) : %s" % (len(history), trace))
        return 0
    finally:
        db.close()


def _print_host_fiches(fiches: list, physical: dict, admissions: dict, *, many: bool) -> None:
    """`ameesh hosts` (L43, 0031) : les fiches Host de l'hôte par canon, et le
    `max_agents` physique (le plus strict) avec sa provenance."""
    if not fiches:
        return
    if many or len(fiches) > 1:
        print("  fiches     %s" % " ; ".join(
            "%s (%s, max %s, %d admission(s))" % (
                canon.id, fiche.responsible or "sans responsable",
                "—" if fiche.policy.max_agents is None else fiche.policy.max_agents,
                admissions.get(canon.id, 0))
            for canon, fiche in fiches))
    total = sum(admissions.values())
    cap = physical["max_agents"]
    if cap is None:
        print("  agents     %d admission(s) ; max_agents non déclaré" % total)
        return
    print("  agents     %d admission(s) ; max_agents %d [%s]%s" % (
        total, cap, physical["origin"].get("max_agents", "?"),
        " — AU-DELÀ du maximum physique : l'exécuteur ne fait tourner que %d persona(s) "
        "à la fois" % cap if total > cap else ""))


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        # L61 : la ligne et la clé en un seul aller-retour (db.batched)
        row, info = db_mod.batched(db, lambda db: (
            registry.get(db, args.agent), authority.key_info(db, args.agent)))
        if row is None:
            print(USAGE_HINT % ("agent inconnu : %s" % args.agent), file=sys.stderr)
            return 1
        row = dict(row)
        row["key_ready"] = bool(info and info.get("public_key") and not info.get("key_revoked_ts"))
        row["public_key_fingerprint"] = (info or {}).get("public_key_fingerprint")
        row["key_updated_ts"] = (info or {}).get("key_updated_ts")
        row["key_revoked_ts"] = (info or {}).get("key_revoked_ts")
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2))
            return 0
        print("agent    : %s" % row["name"])
        print("chantier : %s" % (row.get("chantier") or "—"))
        print("harnais  : %-9s hôte : %-12s statut : %s%s" % (
            row.get("harness") or "?", row.get("host") or "—", row.get("status"),
            (" (%s)" % row["status_text"]) if row.get("status_text") else ""))
        if row.get("stop_reason"):
            print("arrêt    : %s" % row["stop_reason"])
        # L37 (0030) : mode explicite ; un agent externe exige un responsable
        if row.get("mode") == "externe":
            print("mode     : externe (session humaine, non réveillable)%s" % (
                "" if row.get("responsible")
                else " — SANS responsable humain : obligatoire pour un agent externe"))
        else:
            print("mode     : execute (mené par un exécuteur, sous bail)")
        # L39 (0033) : compte d'origine de la session, quand il est connu
        print("session  : %s%s" % (row.get("session_id") or "—",
                                   " (compte %s)" % row["session_account"]
                                   if row.get("session_id") and row.get("session_account")
                                   else ""))
        print("bail     : %s" % _lease(row))
        print("modèle   : %-9s budget : %s" % (row.get("model") or "—", _budget(row)))
        print("responsable : %s   équipe : %s%s" % (
            row.get("responsible") or "aucun (non réclamable si requis)",
            row.get("team") or "—",
            "   éphémère créé par %s, échéance %s" % (
                row.get("created_by") or "?", _fmt_moment(row.get("ephemeral_expires_ts")))
            if row.get("ephemeral") else ""))
        if row.get("canon_ref"):
            print("canon    : %s" % row["canon_ref"])
        print("clé      : %s" % (
            "%s (rôle %s%s)" % (row.get("public_key_fingerprint") or "",
                                row.get("key_role") or "agent",
                                ", révoquée" if row.get("key_revoked_ts") else "")
            if row.get("key_ready") else "aucune clé publique enregistrée"))
        print("tours    : %s   dernier tour : %s" % (
            row.get("turns"), _fmt_moment(row.get("last_turn_ts"))))
        print("erreur   : %s" % (row.get("last_error") or "—"))
        print("dossier  : %s" % (row.get("cwd") or "—"))
        print("vu       : %s" % _fmt_age(time.time() - float(row.get("last_seen_ts") or 0)))
        return 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# clés
# --------------------------------------------------------------------------

def cmd_key(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        if args.key_command == "generate":
            if not args.i_am_the_owner:
                print(
                    "refus : la clé du propriétaire ne se génère pas sans --i-am-the-owner.\n"
                    "Aucun agent ne doit générer ni détenir cette clé : la cérémonie est\n"
                    "un acte du propriétaire (docs/BASCULE.md).",
                    file=sys.stderr)
                return 1
            private_path, public_path, fingerprint = signing.write_keypair(
                os.path.expanduser(args.out), args.name)
            print("clé privée : %s (0600 — à garder hors ligne, jamais commitée)" % private_path)
            print("clé publique : %s" % public_path)
            print("empreinte : %s" % fingerprint)
            print("enregistrez-la : agent-mesh key register <agent> --public-key %s" % public_path)
            return 0
        if args.key_command == "register":
            public = signing.read_public(os.path.expanduser(args.public_key))
            result = authority.register_key(
                db, args.agent, public, role=args.role, note=args.note)
            print("clé de %s enregistrée : %s (rôle %s)" % (
                result["agent"], result["fingerprint"], result["role"]))
            if result["role"] == "owner":
                print("attention : cette clé porte l'autorité du propriétaire pour %s ; "
                      "sa clé privée ne doit exister que chez le propriétaire."
                      % result["agent"])
            return 0
        if args.key_command == "revoke":
            if not authority.revoke_key(db, args.agent):
                print(USAGE_HINT % ("aucune clé active pour %s" % args.agent), file=sys.stderr)
                return 1
            print("clé de %s révoquée (les signatures existantes ne vaudront plus rien)" % args.agent)
            return 0
        if args.key_command == "show":
            info = authority.key_info(db, args.agent)
            if not info or not info.get("public_key"):
                print("aucune clé publique enregistrée pour %s" % args.agent, file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0
            print("agent      : %s" % info["name"])
            print("empreinte  : %s" % info.get("public_key_fingerprint"))
            print("rôle       : %s%s" % (
                info.get("key_role") or "agent",
                " (autorité du propriétaire)" if info.get("key_role") == "owner"
                else " (provenance seulement)"))
            print("clé        : %s" % info.get("public_key"))
            print("enregistrée: %s" % _fmt_moment(info.get("key_updated_ts")))
            print("révoquée   : %s" % (_fmt_moment(info.get("key_revoked_ts"))
                                       if info.get("key_revoked_ts") else "non"))
            return 0
        if args.key_command == "list":
            rows = storage.of(db).keys.registered()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print("aucune clé enregistrée")
                return 0
            for row in rows:
                print("%-20s %-8s %s  %s" % (
                    row["name"], row.get("key_role") or "agent",
                    row["public_key_fingerprint"],
                    "révoquée %s" % _fmt_moment(row["key_revoked_ts"])
                    if row.get("key_revoked_ts") else "enregistrée %s"
                    % _fmt_moment(row.get("key_updated_ts"))))
            return 0
        print(USAGE_HINT % "sous-commande key inconnue", file=sys.stderr)
        return 2
    finally:
        db.close()


# --------------------------------------------------------------------------
# approbations
# --------------------------------------------------------------------------

def cmd_approve(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        seed = signing.read_private(os.path.expanduser(args.key))
        # Signer une approbation est un acte d'autorité : l'approbateur ne se
        # déduit pas du dossier. Identité liée, ou --as explicite.
        binding = identity.resolve_binding(cfg, db)
        approver = args.as_agent or (binding.name if binding.ok else "")
        if not approver:
            print("approbateur non lié : posez AGENT_MAIL_NAME (le runner le fait) "
                  "ou passez --as NOM", file=sys.stderr)
            return 2
        ttl = authority.parse_ttl(args.expires)
        meta = json.loads(args.meta) if args.meta else {}
        built = authority.create_approval(
            db, seed, approver=approver, action=args.action, artifact_kind=args.kind,
            artifact_hash=args.hash, decision=args.decision, ttl=ttl, meta=meta)
        print("approbation #%d enregistrée : %s %s %s" % (
            built["id"], built["decision"], built["action"], built["artifact_hash"][:16] + "…"))
        print("clé : %s   expire : %s" % (
            built["signature_key"][:16] + "…",
            _fmt_moment(time.time() + ttl)))
        return 0
    finally:
        db.close()


def cmd_approvals(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        rows = authority.list_approvals(
            db, action=args.action, artifact_hash=args.hash, limit=args.limit)
        for row in rows:
            verdict = authority.verify_approval(db, row)
            row["verdict"] = verdict.ok
            row["reason"] = verdict.reason
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("aucune approbation")
            return 0
        for row in rows:
            print("#%-4d %-9s %-10s %-12s %s  %s" % (
                row["id"], row["decision"], row["action"], row["artifact_kind"],
                row["artifact_hash"][:16] + "…",
                "VALIDE" if row["verdict"] else "invalide (%s)" % row["reason"]))
            print("      %s  expire %s%s" % (
                row["approver"], _fmt_moment(row.get("expires_ts")),
                "  consommée par %s" % row["consumed_by"] if row.get("consumed_ts") else ""))
        return 0
    finally:
        db.close()


def cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        row, verdict = authority.find_approval(
            db, args.action, args.hash, consume_by=args.by if args.consume else None)
        if args.json:
            print(json.dumps({"ok": verdict.ok, "reason": verdict.reason,
                              "approval": row}, ensure_ascii=False, indent=2))
        elif verdict.ok:
            print("approbation VALIDE #%s : %s %s → %s" % (
                row["id"], row["approver"], row["action"],
                "consommée par %s" % args.by if args.consume else "disponible"))
            print("artefact %s   expire %s" % (
                row["artifact_hash"], _fmt_moment(row.get("expires_ts"))))
        else:
            print("pas d'approbation valide : %s" % verdict.reason, file=sys.stderr)
        return 0 if verdict.ok else 1
    finally:
        db.close()


# --------------------------------------------------------------------------
# lots (work_items)
# --------------------------------------------------------------------------

def _delays_cell(row: dict | None, now: float) -> str:
    """La phase courante d'un lot et l'âge de son dernier jalon (R19)."""
    if not row:
        return "—"
    name, at = work.last_milestone(row)
    if at is None:
        return name
    return "%s %s" % (name, _fmt_span(now - at))


def _print_delays(row: dict) -> None:
    """La frise d'un lot : jalons datés, durées entre eux."""
    now = time.time()
    parts = []
    for name, at_key, _duration in work.MILESTONES:
        at = row.get(at_key)
        parts.append("%s %s" % (name, _fmt_age(now - float(at)) if at else "—"))
    print("jalons   : %s" % " · ".join(reversed(parts)))
    spans = " · ".join("%s %s" % (label, _fmt_span(value))
                       for label, value in work.durations(row))
    if row.get("blocked_verdicts"):
        spans += " · verdicts bloqués %d/%d" % (row["blocked_verdicts"], row["verdicts"])
    print("délais   : %s" % spans)


def cmd_review_class(cfg: Config, args: argparse.Namespace) -> int:
    """La classe de revue d'un changement (décision 0018 point 1, R19).

    La politique est lue au canon (`review_policies.classes`) ; les fichiers
    viennent des arguments ou de `--diff REF` (ref..HEAD + arbre de travail).
    Une déclaration invalide refuse de classer : jamais une classe par défaut
    rassurante sur une politique illisible.
    """
    from . import canon as canon_mod
    from . import canon_cli, review as review_mod

    canon = canon_cli.load_canon(cfg, args)
    if not canon.readable:
        print("erreur : canon illisible (%s)" % canon.root, file=sys.stderr)
        return 1
    policy, problems = review_mod.policy_from_federation(canon.federation)
    if problems:
        for code, _severity, message in problems:
            print("%s : %s" % (code, message), file=sys.stderr)
        return 1
    if args.diff and args.paths:
        print(USAGE_HINT % "review-class : --diff REF ou des fichiers, pas les deux",
              file=sys.stderr)
        return 2
    in_repo = canon_mod.git_toplevel(os.getcwd())
    if args.diff and in_repo is None:
        print(USAGE_HINT % "review-class : --diff REF demande un dépôt git", file=sys.stderr)
        return 1
    # Hors dépôt, les chemins donnés sont lus depuis le dossier courant : c'est
    # ce que l'appelant voit, et la politique n'a pas de racine à leur donner.
    repo = in_repo or os.getcwd()
    if args.diff:
        try:
            paths = canon_mod.git_changed_paths(repo, args.diff)
        except canon_mod.GitError as exc:
            # une référence illisible n'est pas « aucun fichier changé » : rendre
            # une classe partielle ferait croire à un changement anodin (B2).
            print(USAGE_HINT % ("review-class : %s" % exc), file=sys.stderr)
            return 1
        if not paths:
            print("aucun fichier changé depuis %s" % args.diff)
            return 0
    elif args.paths:
        # Les chemins sont donnés depuis le dossier courant : on les ramène à la
        # racine du dépôt, parce que la politique est écrite en chemins de dépôt.
        here = os.path.relpath(os.getcwd(), repo).replace(os.sep, "/")
        paths = [p if here == "." else "%s/%s" % (here, p) for p in args.paths]
    else:
        print(USAGE_HINT % "review-class : des fichiers ou --diff REF", file=sys.stderr)
        return 2
    try:
        result = review_mod.classify(policy, paths)
    except review_mod.ReviewError as exc:
        print(USAGE_HINT % ("review-class : %s" % exc), file=sys.stderr)
        return 1
    if args.json:
        # `policy_ref` : le commit du canon lu, pour que la classe affichée soit
        # rattachable à la politique qui l'a produite (mesh-design).
        policy_ref = canon.sources[0].commit if canon.sources else ""
        print(json.dumps(dict(result, root=canon.root, repo=repo, policy_ref=policy_ref),
                         ensure_ascii=False, indent=2))
        return 0
    print("classe : %s" % result["class_display"])
    for entry in result["files"]:
        rule = ", ".join(entry["rule"]) if entry["rule"] else "défaut"
        print("  %-52s %-9s %s" % (entry["path"], review_mod.DISPLAY[entry["class"]], rule))
    return 0


def _print_assignment(check: dict | None) -> None:
    """Ce que l'attribution gardée (L37) a à dire : session externe forcée,
    agent arrêté."""
    if not check:
        return
    if check.get("externe"):
        print("session externe : %s ne sera pas réveillé par ameesh ; le lot attend %s "
              "(responsable)" % (check["assignee"], check["responsible"]))
    if check.get("warning"):
        print("avertissement : %s" % check["warning"], file=sys.stderr)


def cmd_work(cfg: Config, args: argparse.Namespace) -> int:
    from . import plan, plan_cli, stagnation  # plan de travail (L29)
    db = _open(cfg)
    try:
        if args.work_command in plan_cli.COMMANDS:
            return plan_cli.run(db, args)
        if args.work_command == "add":
            # L37 (0030, règle 2) : attribution gardée — un assigné non
            # réveillable est refusé AVANT la création (WorkError, code 1).
            check = (work.check_assignee(db, args.assignee, externe=args.externe, cfg=cfg)
                     if args.assignee else None)
            item = work.add(
                db, title=args.title, type=args.type, source=args.source, app=args.app,
                body=args.body or "", issue_ref=args.issue_ref, workstream=args.workstream,
                assignee=args.assignee, budget_usd=args.budget, actor=args.actor,
                package=args.package, externe=args.externe, cfg=cfg)
            print("lot #%d créé en %s : %s%s" % (
                item["id"], item["state"], item["title"],
                " (plan : %s)" % item["package_id"] if item.get("package_id") else ""))
            _print_assignment(check)
            return 0
        if args.work_command == "assign":
            out = work.assign(db, args.id, args.agent, externe=args.externe,
                              actor=args.actor, cfg=cfg)
            print("lot #%d assigné à %s (avant : %s)" % (
                out["item"]["id"], out["item"]["assignee"], out["previous"] or "personne"))
            _print_assignment(out["check"])
            return 0
        if args.work_command == "delegate":
            # L40 (0030, point 5) : délégation à échéance, gardée comme `assign`
            out = work.delegate(db, args.id, args.agent, within=args.within,
                                actor=args.actor, cfg=cfg)
            item = out["item"]
            print("lot #%d délégué à %s par %s, échéance dans %s (%s)" % (
                item["id"], item["assignee"], item["delegated_by"],
                work.span(float(item["due_ts"]) - float(item["delegated_ts"])),
                _fmt_moment(item.get("due_ts"))))
            if out.get("replaced"):
                print("délégation précédente remplacée (%s, déléguée par %s)" % (
                    out["replaced"]["delegate"], out["replaced"]["delegated_by"]))
            if out.get("message_id"):
                print("événement #%d déposé pour %s" % (out["message_id"], item["assignee"]))
            if out.get("warning"):
                print("avertissement : %s" % out["warning"], file=sys.stderr)
            return 0
        if args.work_command == "expire-delegations":
            rows = work.expire_delegations(db, actor=args.actor or work.SYSTEM_SENDER,
                                           dry_run=args.dry_run)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print("aucune délégation échue")
                return 0
            for row in rows:
                issue = {
                    "rendue": "rendu à %s (aucun tour de %s)" % (row["delegated_by"],
                                                                row["delegate"]),
                    "soldee": "délégation soldée (travail de %s)" % row["delegate"],
                    "annulee": "délégation annulée (lot fermé ou réassigné)",
                }.get(row["outcome"], row["outcome"])
                print("%slot #%s : %s" % ("[dry-run] " if row.get("dry_run") else "",
                                          row["work_item_id"], issue))
            return 0
        if args.work_command == "list":
            threshold = stagnation.stale_after(args.stale_after)

            def lire(db):
                rows = work.list_items(db, state=args.state, assignee=args.assignee,
                                       limit=args.limit)
                plan.annotate(db, rows, threshold=threshold)
                # Les délais viennent d'un seul balayage, restreint aux lots
                # affichés (jamais une requête par ligne) ; le JSON les porte
                # aussi, pour la frise (L24). Le filtre porte sur les lignes
                # rendues, pas sur une fenêtre globale : un lot ancien garde sa
                # frise (B3).
                delays = {row["work_item_id"]: row for row in
                          work.delays(db, ids=[row["id"] for row in rows])}
                for row in rows:
                    row["delays"] = delays.get(row["id"])
                return rows

            # L61 : lectures regroupées — deux allers-retours en tout
            rows = db_mod.batched(db, lire)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print("aucun lot")
                return 0
            now = time.time()
            print("%-5s %-10s %-14s %-12s %-10s %-12s %-12s %s" % (
                "ID", "TYPE", "ÉTAT", "ASSIGNÉ", "MAJ", "DÉLAI", "EPIC", "TITRE"))
            for row in rows:
                print("%-5d %-10s %-14s %-12s %-10s %-12s %-12s %s" % (
                    row["id"], row["type"], row["state"], (row.get("assignee") or "—")[:12],
                    _fmt_age(now - float(row.get("updated_ts") or 0)),
                    _delays_cell(row.get("delays"), now), (row.get("epic") or "—")[:12],
                    row["title"][:60]))
                notes = []
                if row.get("stale"):
                    notes.append("STAGNANT depuis %s" % plan.describe_age(row["stale"]["idle_s"]))
                if row.get("waiting_for"):
                    notes.append("attend : %s" % row["waiting_for"]["label"])
                if row.get("delegation"):
                    notes.append(row["delegation"]["label"])
                if notes:
                    print("      %s" % " · ".join(notes))
            return 0
        if args.work_command == "show":
            item = work.get(db, args.id)
            if item is None:
                print(USAGE_HINT % ("lot %s introuvable" % args.id), file=sys.stderr)
                return 1
            item["events"] = work.events(db, args.id)
            item["milestones"] = work.milestones(db, args.id)
            item["delays"] = work.timeline(db, args.id)
            plan.annotate(db, [item])
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
                return 0
            print("lot      : #%d %s" % (item["id"], item["title"]))
            print("type     : %-10s état : %-14s assigné : %s" % (
                item["type"], item["state"], item.get("assignee") or "—"))
            print("app      : %-10s source : %-12s workstream : %s" % (
                item.get("app") or "—", item.get("source") or "—", item.get("workstream") or "—"))
            print("issue    : %-12s boucles : %-3s budget : %s" % (
                item.get("issue_ref") or "—", item.get("loops"), _budget(item)))
            if item.get("package_id") or item.get("epic"):
                print("plan     : fiche %-12s epic : %s" % (
                    item.get("package_id") or "—", item.get("epic") or "—"))
            if item.get("state") == "closed":
                print("fermé    : %s" % ("abandonné" if item.get("close_reason") == "abandoned"
                                         else "remplacé par #%s" % item.get("superseded_by")))
            if item.get("pr_ref"):
                print("PR       : %s" % item["pr_ref"])
            if item.get("waiting_for"):
                print("attend   : %s" % item["waiting_for"]["label"])
            if item.get("delegation"):
                delegation = item["delegation"]
                print("délégué  : %s (le %s%s)" % (
                    delegation["label"], _fmt_moment(delegation.get("delegated_ts")),
                    ", échéance %s" % _fmt_moment(delegation["due_ts"])
                    if delegation.get("due_ts") else ""))
            if item.get("stale"):
                print("STAGNANT : aucune activité depuis %s"
                      % plan.describe_age(item["stale"]["idle_s"]))
            if item.get("body"):
                print("corps    : %s" % item["body"])
            print("créé     : %s   maj : %s   fermé : %s" % (
                _fmt_moment(item.get("created_ts")), _fmt_moment(item.get("updated_ts")),
                _fmt_moment(item.get("closed_ts"))))
            if item["delays"]:
                _print_delays(item["delays"])
            for event in item["events"]:
                print("  %s  %-14s %-12s %s" % (
                    _fmt_moment(event.get("created_ts")), event["state"],
                    (event.get("actor") or "—")[:12], event["note"]))
            return 0
        if args.work_command == "move":
            item = work.move(db, args.id, args.state, note=args.note or "", actor=args.actor)
            print("lot #%d → %s (boucles %s)" % (item["id"], item["state"], item["loops"]))
            return 0
        if args.work_command == "note":
            work.note(db, args.id, args.text, actor=args.actor)
            print("note ajoutée au lot #%d" % args.id)
            return 0
        if args.work_command == "milestone":
            row = work.milestone(
                db, args.id, args.kind, sha=args.sha or "", actor=args.actor or "",
                verdict=args.verdict, note=args.note or "")
            print("jalon %s ajouté au lot #%d%s" % (
                row["kind"], args.id,
                " (%s)" % row["verdict"] if row.get("verdict") else ""))
            return 0
        print(USAGE_HINT % "sous-commande work inconnue", file=sys.stderr)
        return 2
    finally:
        db.close()


# --------------------------------------------------------------------------
# passage v0 → v1 : importer/exporter la boîte fichier
# --------------------------------------------------------------------------

def cmd_import_v0(cfg: Config, args: argparse.Namespace) -> int:
    """Importe les non-lus de la boîte v0 dans Postgres (bascule, étape 4).

    Les fichiers importés sont déplacés dans `inbox/<nom>/imported/`, jamais
    supprimés : le retour arrière consiste à les remettre en place.

    Chaque message importé est écrit dans son fil, sauf s'il y est déjà (déposé
    par le repli fichier de la v1, qui note le fichier v0 dans l'entrée). Le
    corps est repris tel quel : un import ne refuse pas un message déjà envoyé.
    """
    from . import fil, mail, registry
    state = os.path.expanduser(args.state or cfg.v0_state)
    inbox_root = os.path.join(state, "inbox")
    if not os.path.isdir(inbox_root):
        print("aucune boîte v0 dans %s" % inbox_root, file=sys.stderr)
        return 1
    db = _open(cfg)
    total = 0
    try:
        deja_au_fil = set() if args.dry_run else fil.v0_recorded(cfg)
        names = ([n.strip() for n in args.agents.split(",") if n.strip()]
                 if args.agents else sorted(os.listdir(inbox_root)))
        for name in names:
            directory = os.path.join(inbox_root, name)
            if not os.path.isdir(directory):
                continue
            # le registre v0 (agents/<nom>.json) donne harnais, dossier, session ;
            # un essai (--dry-run) n'écrit rien, registre compris
            agent_file = os.path.join(state, "agents", name + ".json")
            if os.path.exists(agent_file) and not args.dry_run:
                try:
                    with open(agent_file, encoding="utf-8") as fh:
                        seen = json.load(fh)
                    registry.upsert(
                        db, name, harness=seen.get("tool") or None, host=cfg.host,
                        cwd=seen.get("cwd"), session_id=seen.get("session_id"))
                except (OSError, ValueError) as exc:
                    print("  (%s : registre v0 illisible : %s)" % (name, exc), file=sys.stderr)
            fichiers = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
            importes = 0
            for filename in fichiers:
                path = os.path.join(directory, filename)
                try:
                    with open(path, encoding="utf-8") as fh:
                        message = json.load(fh)
                except (OSError, ValueError) as exc:
                    print("  (%s : %s illisible : %s)" % (name, filename, exc), file=sys.stderr)
                    continue
                ts = float(message.get("ts") or time.time())
                if args.dry_run:
                    print("%s ← %s : %s" % (name, message.get("from", "?"),
                                            (message.get("text") or "")[:60]))
                    importes += 1
                    continue
                cle_v0 = "%s/%s" % (name, filename)
                mail.send(db, message.get("from") or "inconnu", name,
                          message.get("text") or "", host=message.get("host"),
                          created_us=int(ts * 1_000_000), allow_structured=True,
                          thread=cle_v0 not in deja_au_fil,
                          thread_meta={"importe_v0": cle_v0})
                destination = os.path.join(directory, "imported")
                os.makedirs(destination, exist_ok=True)
                os.replace(path, os.path.join(destination, filename))
                importes += 1
            if importes:
                print("%s : %d message(s) %s" % (
                    name, importes, "à importer" if args.dry_run else "importé(s)"))
            total += importes
        print("%d message(s) %s" % (total, "à importer" if args.dry_run else "importé(s)"))
        return 0
    finally:
        db.close()


def cmd_export_v0(cfg: Config, args: argparse.Namespace) -> int:
    """Réécrit les non-lus d'un agent dans la boîte v0 (retour arrière).

    Par défaut c'est un déplacement : les messages sont marqués remis en base,
    pour que la v0 reprenne la main sans doublon.
    """
    from . import mail
    state = os.path.expanduser(args.state or cfg.v0_state)
    db = _open(cfg)
    try:
        names = ([n.strip() for n in args.agents.split(",") if n.strip()]
                 if args.agents else storage.of(db).mailbox.pending_recipients_sorted())
        total = 0
        for name in names:
            rows = mail.unread(db, name)
            if not rows:
                continue
            directory = os.path.join(state, "inbox", name)
            os.makedirs(directory, exist_ok=True)
            for row in rows:
                filename = "%d-%s-%d.json" % (
                    int(float(row["created_ts"]) * 1000), row["sender"], os.getpid())
                tmp = os.path.join(directory, "." + filename)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"from": row["sender"], "to": name, "ts": row["created_ts"],
                               "text": row["body"], "host": row.get("host")},
                              fh, ensure_ascii=False)
                os.replace(tmp, os.path.join(directory, filename))
            if not args.keep:
                mail.mark_delivered(db, [row["id"] for row in rows])
            print("%s : %d message(s) exporté(s)%s" % (
                name, len(rows), "" if args.keep else " (marqués remis en base)"))
            total += len(rows)
        print("%d message(s) exporté(s) vers %s" % (total, state))
        return 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# migrations / diagnostic (délégués à la CLI agent-mail)
# --------------------------------------------------------------------------

def cmd_migrate(cfg: Config, _args: argparse.Namespace) -> int:
    from . import cli
    return cli.cmd_migrate(cfg)


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from . import cli
    return cli.cmd_doctor(cfg, notify_test=args.notify_test, probe=args.probe,
                          harness=getattr(args, "harness", False))


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def _etat_chemin(cfg: Config, agent: str, cle: str) -> str:
    return os.path.join(cfg.agent_dir(agent), cle)


def _lit_etat(cfg: Config, agent: str, cle: str) -> str:
    try:
        with open(_etat_chemin(cfg, agent, cle), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _ecrit_etat(cfg: Config, agent: str, cle: str, valeur: str) -> None:
    """Écrit (ou efface) un réglage local ; jamais fatal, `set` le dit."""
    chemin = _etat_chemin(cfg, agent, cle)
    if not valeur:
        try:
            os.unlink(chemin)
        except OSError:
            pass
        return
    os.makedirs(cfg.agent_dir(agent), mode=0o700, exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write(valeur + "\n")


#: clés de `ameesh set` (L13 : model, effort ; L26 : tier, session_policy ;
#: L37 : mode, `execute` | `externe`, décision 0030 ; L60 : context_max_tokens)
SET_KEYS = ("model", "effort", "tier", "session_policy", "mode", "context_max_tokens")
#: suffixes acceptés par `context_max_tokens` (`15M`, `500k`)
_TOKEN_SUFFIXES = {"k": 1_000, "m": 1_000_000}


def parse_token_count(valeur: str) -> int:
    """`15000000`, `15M`, `1.5m`, `500k` → entier ≥ 0 ; ValueError sinon (L60)."""
    texte = valeur.strip().lower().replace("_", "")
    facteur = 1
    if texte and texte[-1] in _TOKEN_SUFFIXES:
        facteur = _TOKEN_SUFFIXES[texte[-1]]
        texte = texte[:-1]
    nombre = float(texte)
    if nombre != nombre or nombre < 0 or nombre * facteur > 2 ** 62:
        raise ValueError(valeur)
    return int(round(nombre * facteur))
#: un tier est un identifiant court (passé tel quel au harnais par son descripteur)
_TIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def cmd_set(cfg: Config, args) -> int:
    """`ameesh set <agent> model=… effort=… tier=… session_policy=… mode=…
    context_max_tokens=…` (L13, L26, L37, L60).

    Écrit l'état d'exécution : le modèle dans le registre (visible par `list`)
    et dans l'état local ; l'effort et le tier dans l'état local **et** en base
    (L26 : `list --json` les rend quel que soit l'hôte) ; la politique de
    session (`par-lot` | `taille` | `jamais`, décision 0025) en base seulement.
    Le tour suivant les lit et les applique ; une valeur vide revient au
    défaut. Le tier n'a d'effet que sur un harnais dont le descripteur le
    déclare (Codex : `service_tier`). Le mode (`execute` | `externe`, L37) est
    écrit en base ; vide = `execute`. Le plafond de contexte
    (`context_max_tokens`, L60 : `15M`, `500k`, `0` = désactivé) est écrit en
    base ; vide = défaut de l'exécuteur.
    """
    from . import adapters
    from .config import SESSION_POLICIES

    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db, defer=True)
        agent = registry.get(db, args.agent)
        if agent is None:
            print("agent inconnu : %s" % args.agent, file=sys.stderr)
            return 1
        valeurs: dict = {}
        for couple in args.values:
            cle, sep, valeur = couple.partition("=")
            valeur = valeur.strip()
            if not sep or cle not in SET_KEYS:
                print("usage : ameesh set <agent> model=… effort=… tier=… "
                      "session_policy=%s mode=%s context_max_tokens=N|15M|0"
                      % ("|".join(SESSION_POLICIES), "|".join(registry.MODES)),
                      file=sys.stderr)
                return 2
            if cle == "mode" and valeur and valeur not in registry.MODES:
                print("mode invalide : %r (%s)" % (valeur, " | ".join(registry.MODES)),
                      file=sys.stderr)
                return 2
            if cle == "session_policy" and valeur and valeur not in SESSION_POLICIES:
                print("session_policy invalide : %r (%s)"
                      % (valeur, " | ".join(SESSION_POLICIES)), file=sys.stderr)
                return 2
            if cle == "tier" and valeur and not _TIER_RE.match(valeur):
                print("tier invalide : %r (ex. fast, flex)" % valeur, file=sys.stderr)
                return 2
            if cle == "context_max_tokens" and valeur:
                try:
                    valeur = str(parse_token_count(valeur))
                except ValueError:
                    print("context_max_tokens invalide : %r (ex. 15M, 500k, 0 = "
                          "désactivé)" % valeur, file=sys.stderr)
                    return 2
            valeurs[cle] = valeur
        for cle, valeur in valeurs.items():
            if cle in ("model", "effort", "tier"):
                _ecrit_etat(cfg, args.agent, cle, valeur)
            if cle == "model":
                if valeur:
                    registry.upsert(db, args.agent, model=valeur)
                else:
                    storage.of(db).agents.clear_model(args.agent)
        if "mode" in valeurs:
            registry.set_mode(db, args.agent, valeurs["mode"] or "execute")
        reglages = {k: v for k, v in valeurs.items() if k not in ("model", "mode")}
        if reglages:
            storage.of(db).operations.set_settings(args.agent, reglages)
        agent = registry.get(db, args.agent) or {}
        modele = agent.get("model") or "défaut"
        effort = _lit_etat(cfg, args.agent, "effort") or agent.get("effort") or "défaut"
        tier = _lit_etat(cfg, args.agent, "tier") or agent.get("tier") or "défaut"
        politique = agent.get("session_policy") or "défaut (%s)" % cfg.session_policy
        from .exploitation import effective_context_max
        plafond = effective_context_max(cfg, agent)
        plafond_txt = ("désactivé" if plafond == 0 else "%d" % plafond) + (
            "" if agent.get("context_max_tokens") is not None else " (défaut)")
        print("%s : modèle=%s effort=%s tier=%s session=%s mode=%s contexte=%s (prend "
              "effet au prochain tour)" % (args.agent, modele, effort, tier, politique,
                                          agent.get("mode") or "execute", plafond_txt))
        if agent.get("mode") == "externe" and not agent.get("responsible"):
            # 0030 : un agent externe a obligatoirement un responsable humain
            print("attention : agent externe sans responsable humain : ses lots et ses "
                  "alertes n'ont personne à qui aller (désignez-le au canon)",
                  file=sys.stderr)
        if valeurs.get("tier") and not adapters.supports_tier(agent.get("harness") or ""):
            print("attention : le harnais %s ne déclare pas de tier : réglage sans effet"
                  % (agent.get("harness") or "?"), file=sys.stderr)
        return 0
    finally:
        db.close()


def _catalog_store(cfg):
    db = db_mod.connect(cfg)
    return db, storage.of(db).catalog


def cmd_models_list(cfg, args) -> int:
    db, store = _catalog_store(cfg)
    try:
        rows = store.listing(harness=getattr(args, "harness", None))
    finally:
        db.close()
    if getattr(args, "json", False):
        print(json.dumps(rows, default=str, ensure_ascii=False))
        return 0
    for row in rows:
        retired = " (retiré)" if row.get("retired_at") else ""
        harness = row.get("harness") or "-"
        print(f"{row['provider']}/{row['model_id']}  [{harness}]{retired}")
    return 0


def cmd_models_show(cfg, args) -> int:
    db, store = _catalog_store(cfg)
    try:
        rows = store.show(args.model)
    finally:
        db.close()
    if getattr(args, "json", False):
        print(json.dumps(rows, default=str, ensure_ascii=False))
        return 0
    for row in rows:
        print(f"{row['provider']}/{row['model_id']}  ctx={row.get('context_window')}  "
              f"prix={row.get('price_input')}/{row.get('price_cached')}/{row.get('price_output')}  "
              f"source={row.get('source')}  harnais={row.get('harness') or '-'}  "
              f"efforts={list(row.get('efforts') or [])}")
    return 0


#: La correspondance entre le vocabulaire de `--source` et les modules de découverte.
DISCOVERY_SOURCES = {
    "anthropic": "ameesh.discovery.anthropic",
    "openai": "ameesh.discovery.openai",
    "deepseek": "ameesh.discovery.deepseek",
    "harness": "ameesh.discovery.harness",
    "prices": "ameesh.discovery.prices",
}


def _source_module(name: str):
    import importlib

    return importlib.import_module(DISCOVERY_SOURCES[name])


def cmd_models_discover(cfg, args) -> int:
    """Découvre, enregistre, et prévient — sans jamais dépenser un token."""
    names = [args.source] if getattr(args, "source", None) else list(DISCOVERY_SOURCES)
    db, store = _catalog_store(cfg)
    failures = 0
    try:
        for name in names:
            result = _source_module(name).listing()
            if getattr(args, "dry_run", False):
                print(f"{name}: {len(result.models)} modèle(s), complet={result.complete} "
                      f"{result.detail}".rstrip())
                continue
            sent: list[dict] = []
            notify = getattr(args, "notify", None)

            def emit(body: str, payload: dict, _sent=sent, _name=name) -> None:
                # Le fil de l'équipe est DURABLE dans les deux cas (revue B4) :
                # `mail.send` dépose un message et l'écrit ; sans destinataire, on
                # écrit le fil directement, sans inventer de destinataire — mesh-design
                # a demandé que --notify reste explicite, codex3 que le fil survive.
                if notify:
                    mail.send(db, args.sender, notify, body, kind="event", payload=payload,
                              allow_structured=True)
                else:
                    # La cfg de la COMMANDE, pas `db.cfg` : un appelant de test peut
                    # fournir une base sans configuration, et le fil doit quand même
                    # s'écrire (sonde B4).
                    fil.record(
                        cfg, db, sender=args.sender, recipients=[], text=body,
                        project=getattr(args, "project", None),
                        meta={"kind": "event", "source": _name, "payload": payload},
                    )
                    print(body)
                _sent.append({"body": body, "payload": payload})

            decision = catalog.record(store, result, emit=emit)
            if not result.complete:
                failures += 1
            print(f"{name}: {len(result.models)} vu(s), {len(decision.to_retire)} retiré(s)"
                  + (f", {decision.refused_reason}" if decision.refused_reason else ""))
    finally:
        db.close()
    return 1 if failures and not getattr(args, "dry_run", False) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-mesh", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="mesh list : tous les agents")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="détail d'un agent")
    p_show.add_argument("agent")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_hosts = sub.add_parser("hosts", help="ressources des hôtes (L31)")
    p_hosts.add_argument("host", nargs="?", default=None, help="un seul hôte (sinon tous)")
    p_hosts.add_argument("--history", type=int, default=10,
                         help="nombre de relevés de l'historique court (défaut 10)")
    p_hosts.add_argument("--json", action="store_true", help="un objet JSON par hôte")
    p_hosts.set_defaults(func=cmd_hosts)

    p_models = sub.add_parser("models", help="catalogue des modèles (L14)")
    models_sub = p_models.add_subparsers(dest="models_command")

    p_models_list = models_sub.add_parser("list", help="lister le catalogue")
    p_models_list.add_argument("--harness")
    p_models_list.add_argument("--json", action="store_true")
    p_models_list.set_defaults(func=cmd_models_list)

    p_models_show = models_sub.add_parser("show", help="détail d'un modèle")
    p_models_show.add_argument("model")
    p_models_show.add_argument("--json", action="store_true")
    p_models_show.set_defaults(func=cmd_models_show)

    p_models_discover = models_sub.add_parser("discover", help="découvrir sans dépenser un token")
    p_models_discover.add_argument("--source", choices=sorted(DISCOVERY_SOURCES))
    p_models_discover.add_argument("--dry-run", action="store_true")
    p_models_discover.add_argument("--notify", help="agent à réveiller d'un événement")
    p_models_discover.add_argument("--sender", default=os.environ.get("AMEESH_AGENT") or "catalogue")
    p_models_discover.set_defaults(func=cmd_models_discover)

    p_key = sub.add_parser("key", help="clés publiques du propriétaire")
    key_sub = p_key.add_subparsers(dest="key_command")
    p_gen = key_sub.add_parser("generate")
    p_gen.add_argument("--out", required=True)
    p_gen.add_argument("--name", default="owner")
    p_gen.add_argument("--i-am-the-owner", action="store_true",
                       help="confirme que c'est bien le propriétaire qui agit")
    p_gen.set_defaults(func=cmd_key)
    p_reg = key_sub.add_parser("register")
    p_reg.add_argument("agent")
    p_reg.add_argument("--public-key", required=True)
    p_reg.add_argument("--role", choices=list(authority.KEY_ROLES), default=authority.DEFAULT_KEY_ROLE,
                       help="owner = autorité du propriétaire ; agent = provenance seulement")
    p_reg.add_argument("--note", default=None)
    p_reg.set_defaults(func=cmd_key)
    p_kshow = key_sub.add_parser("show")
    p_kshow.add_argument("agent")
    p_kshow.add_argument("--json", action="store_true")
    p_kshow.set_defaults(func=cmd_key)
    p_klist = key_sub.add_parser("list")
    p_klist.add_argument("--json", action="store_true")
    p_klist.set_defaults(func=cmd_key)
    p_krev = key_sub.add_parser("revoke")
    p_krev.add_argument("agent")
    p_krev.set_defaults(func=cmd_key)

    p_app = sub.add_parser("approve", help="signer une approbation")
    p_app.add_argument("--key", required=True, help="clé privée du propriétaire")
    p_app.add_argument("--action", required=True)
    p_app.add_argument("--hash", required=True, help="sha256 hex de l'artefact")
    p_app.add_argument("--kind", default="text")
    p_app.add_argument("--decision", default="approved", choices=["approved", "rejected"])
    p_app.add_argument("--expires", default=None, help="48h, 7d… (défaut 24h)")
    p_app.add_argument("--as", dest="as_agent", default=None)
    p_app.add_argument("--meta", default=None, help="JSON libre")
    p_app.set_defaults(func=cmd_approve)

    p_apps = sub.add_parser("approvals", help="lister les approbations")
    p_apps.add_argument("--action", default=None)
    p_apps.add_argument("--hash", default=None)
    p_apps.add_argument("--limit", type=int, default=50)
    p_apps.add_argument("--json", action="store_true")
    p_apps.set_defaults(func=cmd_approvals)

    p_ver = sub.add_parser("verify", help="vérifier qu'une approbation valide existe")
    p_ver.add_argument("--action", required=True)
    p_ver.add_argument("--hash", required=True)
    p_ver.add_argument("--consume", action="store_true", help="la consommer (usage unique)")
    p_ver.add_argument("--by", default=None)
    p_ver.add_argument("--json", action="store_true")
    p_ver.set_defaults(func=cmd_verify)

    p_work = sub.add_parser("work", help="lots (work_items)")
    work_sub = p_work.add_subparsers(dest="work_command")
    pw_add = work_sub.add_parser("add")
    pw_add.add_argument("--title", required=True)
    pw_add.add_argument("--type", default="evolution", choices=["bug", "evolution"])
    pw_add.add_argument("--source", default="")
    pw_add.add_argument("--app", default="")
    pw_add.add_argument("--body", default=None)
    pw_add.add_argument("--issue-ref", default=None)
    pw_add.add_argument("--workstream", default=None)
    pw_add.add_argument("--assignee", default=None)
    pw_add.add_argument("--budget", type=float, default=None)
    pw_add.add_argument("--actor", default="")
    pw_add.add_argument("--package", default=None, help="fiche WorkPackage du lot (L29)")
    pw_add.add_argument("--externe", action="store_true",
                        help="forcer l'attribution à un agent externe qui a un responsable "
                             "humain (L37, 0030)")
    pw_add.set_defaults(func=cmd_work)
    pw_list = work_sub.add_parser("list")
    pw_list.add_argument("--state", default=None)
    pw_list.add_argument("--assignee", default=None)
    pw_list.add_argument("--limit", type=int, default=50)
    pw_list.add_argument("--json", action="store_true")
    pw_list.add_argument("--stale-after", default=None,
                         help="lot stagnant sans activité depuis (défaut 6h, L29)")
    pw_list.set_defaults(func=cmd_work)
    pw_show = work_sub.add_parser("show")
    pw_show.add_argument("id", type=int)
    pw_show.add_argument("--json", action="store_true")
    pw_show.set_defaults(func=cmd_work)
    pw_move = work_sub.add_parser("move")
    pw_move.add_argument("id", type=int)
    pw_move.add_argument("state", choices=list(work.STATES))
    pw_move.add_argument("--note", default=None)
    pw_move.add_argument("--actor", default="")
    pw_move.set_defaults(func=cmd_work)
    pw_assign = work_sub.add_parser(
        "assign", help="réassigner un lot à un agent réveillable (L37, 0030)")
    pw_assign.add_argument("id", type=int)
    pw_assign.add_argument("agent")
    pw_assign.add_argument("--externe", action="store_true",
                           help="forcer l'attribution à un agent externe qui a un "
                                "responsable humain")
    pw_assign.add_argument("--actor", default="")
    pw_assign.set_defaults(func=cmd_work)
    pw_delegate = work_sub.add_parser(
        "delegate", help="déléguer un lot à un agent réveillable, à échéance (L40, 0030) : "
                         "sans tour du délégué à l'échéance, le lot revient au délégant")
    pw_delegate.add_argument("id", type=int)
    pw_delegate.add_argument("agent")
    pw_delegate.add_argument("--within", required=True,
                             help="échéance : 30m, 2h, 1d (ou des secondes)")
    pw_delegate.add_argument("--actor", default="",
                             help="le délégant, à qui le lot reviendra (défaut : "
                                  "l'assigné actuel ; human:… admis)")
    pw_delegate.set_defaults(func=cmd_work)
    pw_expire = work_sub.add_parser(
        "expire-delegations",
        help="traiter les délégations échues (fait aussi par l'exécuteur à chaque passe)")
    pw_expire.add_argument("--dry-run", action="store_true",
                           help="dire ce qui serait fait, sans rien écrire")
    pw_expire.add_argument("--json", action="store_true")
    pw_expire.add_argument("--actor", default="")
    pw_expire.set_defaults(func=cmd_work)

    pw_note = work_sub.add_parser("note")
    pw_note.add_argument("id", type=int)
    pw_note.add_argument("text")
    pw_note.add_argument("--actor", default="")
    pw_note.set_defaults(func=cmd_work)
    pw_ms = work_sub.add_parser(
        "milestone", help="déclarer un jalon de la frise (gel, verdict) — R19")
    pw_ms.add_argument("id", type=int)
    pw_ms.add_argument("kind", choices=list(work.MANUAL_KINDS),
                       help="frozen (branche gelée) ou verdict (revue rendue)")
    pw_ms.add_argument("--sha", default="", help="le commit gelé ou relu")
    # le verdict est un positionnel (`work milestone 1 verdict ok`), comme la
    # frise se lit ; `frozen` n'en prend pas
    pw_ms.add_argument("verdict", nargs="?", choices=list(work.VERDICTS), default=None,
                       help="pour le jalon verdict : ok ou blocked")
    pw_ms.add_argument("--note", default="")
    pw_ms.add_argument("--actor", default="")
    pw_ms.set_defaults(func=cmd_work)
    from . import plan_cli
    plan_cli.add_parsers(work_sub, cmd_work)  # plan de travail (L29)

    p_cost = sub.add_parser("cost", help="coût des tours et jauges de forfait (L12)")
    cost_sub = p_cost.add_subparsers(dest="cost_command")
    pc_rep = cost_sub.add_parser("report", help="tableau : agent, harnais, modèle, dépense, jauges")
    # Pas d'option de fenêtre : le rapport rend 1 h et 24 h, et `cost spent <agent> [s]`
    # couvre déjà les fenêtres libres. Une option qui ne change rien se retire.
    pc_rep.add_argument("--json", action="store_true")
    pc_rep.set_defaults(func=cmd_cost)
    pc_spent = cost_sub.add_parser("spent", help="dépense (USD) sur une fenêtre glissante")
    pc_spent.add_argument("agent", nargs="?", default="all")
    pc_spent.add_argument("seconds", nargs="?", type=float, default=3600.0)
    pc_spent.set_defaults(func=cmd_cost)
    pc_over = cost_sub.add_parser("over", help="code 0 si le rythme du forfait est dépassé")
    pc_over.add_argument("agent", nargs="?", default="all")
    pc_over.set_defaults(func=cmd_cost)
    # L26 : usage par tour, historique des jauges, solde du fournisseur payé au token
    pc_turns = cost_sub.add_parser("turns", help="usage par tour (in, cached, out, usd)")
    pc_turns.add_argument("--agent", default=None)
    pc_turns.add_argument("--since", default="24h", help="durée (16h, 2d) ou date ISO")
    pc_turns.add_argument("--limit", type=int, default=500)
    pc_turns.add_argument("--json", action="store_true")
    pc_turns.set_defaults(func=cmd_cost)
    pc_gauges = cost_sub.add_parser("gauges", help="historique des jauges de forfait")
    pc_gauges.add_argument("--harness", default=None)
    pc_gauges.add_argument("--since", default="7d")
    pc_gauges.add_argument("--json", action="store_true")
    pc_gauges.add_argument("--record", action="store_true",
                           help="relève d'abord les jauges des journaux locaux (écrit "
                                "l'historique) ; sans elle, lecture seule")
    pc_gauges.set_defaults(func=cmd_cost)
    pc_bal = cost_sub.add_parser(
        "balance", help="solde du fournisseur payé au token, dépense réelle par heure et jour")
    pc_bal.add_argument("--provider", default=None, help="deepseek (défaut : tous)")
    pc_bal.add_argument("--record", action="store_true",
                        help="relever le solde maintenant (lecture seule, gratuite)")
    pc_bal.add_argument("--since", default="7d")
    pc_bal.add_argument("--json", action="store_true")
    pc_bal.set_defaults(func=cmd_cost)

    # L30 (0027) : comptes multiples par fournisseur
    p_acc = sub.add_parser("accounts", help="comptes par fournisseur : actif, jauges, forçage (L30)")
    acc_sub = p_acc.add_subparsers(dest="accounts_command")
    pa_list = acc_sub.add_parser("list", help="comptes déclarés, compte actif, jauges, bascules")
    pa_list.add_argument("--json", action="store_true")
    pa_list.add_argument("--last", type=int, default=5, help="dernières bascules montrées")
    pa_list.set_defaults(func=cmd_accounts)
    pa_use = acc_sub.add_parser("use", help="forcer un compte (plus de choix automatique)")
    pa_use.add_argument("harness")
    pa_use.add_argument("account")
    pa_use.set_defaults(func=cmd_accounts)
    pa_auto = acc_sub.add_parser("auto", help="rendre la main au choix automatique (0034)")
    pa_auto.add_argument("harness", nargs="?", default=None,
                         help="harnais (défaut : tous ceux qui ont des comptes)")
    pa_auto.set_defaults(func=cmd_accounts)
    p_acc.set_defaults(func=cmd_accounts)

    # L70 : plafonds de budget du mesh, en base (`ameesh budget [set|unset]`)
    from . import budget as budget_mod
    budget_mod.add_parsers(sub)

    p_set = sub.add_parser("set", help="réglages d'un agent, effet au prochain tour (L13, L26)")
    p_set.add_argument("agent")
    p_set.add_argument("values", nargs="+", metavar="clé=valeur",
                       help="model=… effort=… tier=… session_policy=par-lot|taille|jamais "
                            "context_max_tokens=15M|0 "
                            "mode=execute|externe (valeur vide = défaut)")
    p_set.set_defaults(func=cmd_set)

    p_imp = sub.add_parser("import-v0", help="importer la boîte fichier v0 dans Postgres")
    p_imp.add_argument("--state", default=None, help="état v0 (défaut AGENT_MAIL_STATE)")
    p_imp.add_argument("--agents", default=None, help="liste de noms séparés par des virgules")
    p_imp.add_argument("--dry-run", action="store_true")
    p_imp.set_defaults(func=cmd_import_v0)
    p_exp = sub.add_parser("export-v0", help="réécrire des non-lus dans la boîte fichier v0")
    p_exp.add_argument("--state", default=None)
    p_exp.add_argument("--agents", default=None)
    p_exp.add_argument("--keep", action="store_true",
                       help="ne pas marquer remis en base (copie au lieu d'un déplacement)")
    p_exp.set_defaults(func=cmd_export_v0)

    p_mig = sub.add_parser("migrate")
    p_mig.set_defaults(func=cmd_migrate)
    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--notify-test", action="store_true")
    p_doc.add_argument("--probe", action="store_true",
                       help="sonde légère : base, schéma, migrations à jour ; rien d'autre")
    p_doc.add_argument("--harness", action="store_true",
                       help="binaires des harnais servis par l'hôte (chemin, provenance, "
                            "interpréteur) et unités d'exécuteur (L106)")
    p_doc.set_defaults(func=cmd_doctor)

    from . import canon_cli
    canon_cli.add_parsers(sub)

    from . import harness_cli
    harness_cli.add_parsers(sub)

    p_review = sub.add_parser(
        "review-class", help="classe de revue d'un changement (0018) : léger, normal, sensible")
    p_review.add_argument("paths", nargs="*", metavar="FICHIER",
                          help="fichiers à classer, relatifs au dépôt")
    p_review.add_argument("--diff", default=None, metavar="REF",
                          help="fichiers changés depuis REF (ref..HEAD + arbre de travail)")
    canon_cli._canon_options(p_review)
    p_review.set_defaults(func=cmd_review_class)
    return parser


def cmd_cost(cfg: Config, args) -> int:
    """`agent-mesh cost` — la comptabilité par tour et les jauges de forfait.

    Le module `ameesh.cost` ne lit les journaux des harnais qu'en lecture seule :
    ce qui est écrit ici, c'est la base (`turn_costs`, migration 0014).
    """
    what = getattr(args, "cost_command", None) or "report"
    db = _open(cfg)
    try:
        if what in ("turns", "gauges", "balance"):
            return _cost_l26(cfg, db, what, args)
        if what == "report":
            # L61 : le rapport lisait la dépense agent par agent (deux
            # requêtes chacun, ~67 s vers une base distante) : ses lectures
            # sont préchargées en quelques allers-retours (db.prefetch)
            db = db_mod.prefetch(db, lambda d: _cost_report_reads(cfg, d))
        # L70 : les plafonds en vigueur (base > configuration > défaut), ceux
        # que la garde des exécuteurs applique
        from . import budget as budget_mod
        limites = budget_mod.current(cfg, db)
        book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, **limites.book_kwargs())
        if what == "spent":
            seconds = float(getattr(args, "seconds", 3600.0) or 3600.0)
            print("%.4f" % book.spent(getattr(args, "agent", "all") or "all", seconds))
            return 0
        if what == "over":
            agent = getattr(args, "agent", "all") or "all"
            harness = book.tool_of(agent) if agent != "all" else ""
            items = accounts_mod.parse(cfg.accounts).get(harness) if harness else None
            if items:
                # L30 : le forfait n'est « dépassé » que si TOUS les comptes sont au seuil
                now = time.time()
                evals = [accounts_mod.evaluate(book, p, items, now) for p in items]
                reason = "" if any(e.ok for e in evals) else (
                    "tous les comptes %s au seuil — %s" % (harness, " ; ".join(
                        "%s : %s" % (e.profile.name, e.reason) for e in evals)))
                reason = reason or book.over(agent, pace=False)
            else:
                reason = book.over(agent)
            if reason:
                print(reason)
                return 0          # dépassé : c'est le code 0 de la référence
            return 1
        # L30 : jauges par compte et compte actif, pour les harnais à comptes
        try:
            # L71 : un affichage ne relève pas les jauges (lecture seule)
            comptes = accounts_mod.report(cfg, db, book, record=False)
        except accounts_mod.AccountError as exc:
            print("comptes : configuration invalide : %s" % exc, file=sys.stderr)
            comptes = []
        actifs = {}
        for ligne in comptes:
            if ligne["active"]:
                actifs[ligne["harness"]] = (ligne["account"], ligne["_gauges"])
        rows = book.report(accounts=actifs)
        for ligne in comptes:
            ligne.pop("_gauges", None)
        if comptes:
            for row in rows:
                if row["harness"] in actifs:
                    row["accounts"] = [c for c in comptes if c["harness"] == row["harness"]]
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, sort_keys=True, default=str))
        else:
            print(cost_mod.format_report(rows))
            print()
            print(budget_mod.summary_line(limites))
            for agent, caps in sorted(limites.agents.items()):
                print("  plafond de %s : %s" % (agent, " · ".join(
                    "%s %.2f $" % (budget_mod.LABELS[w], v) for w, v in sorted(caps.items()))))
            if comptes:
                print()
                print("comptes (hôte %s) :" % cfg.host)
                print(accounts_mod.format_rows(comptes))
        return 0
    finally:
        db.close()


def _cost_report_reads(cfg: Config, db) -> None:
    """Les lectures de `cost report`, jouées à blanc par `db.prefetch` (L61) :
    plafonds (L70), comptes et rapport par agent. Rien n'est affiché."""
    from . import budget as budget_mod
    limites = budget_mod.current(cfg, db)
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, **limites.book_kwargs())
    try:
        comptes = accounts_mod.report(cfg, db, book)
    except accounts_mod.AccountError:
        comptes = []
    actifs = {ligne["harness"]: (ligne["account"], ligne["_gauges"])
              for ligne in comptes if ligne["active"]}
    book.report(accounts=actifs)


def cmd_accounts(cfg: Config, args) -> int:
    """`ameesh accounts list|use <harnais> <compte>|auto [harnais]` (L30, 0027).

    Les profils viennent de la configuration de l'hôte ; seul l'état (compte
    actif, forçage, journal des bascules) est en base. Rien n'affiche de secret :
    un profil se montre par son type et son emplacement.
    """
    what = getattr(args, "accounts_command", None) or "list"
    declares = accounts_mod.parse(cfg.accounts)  # AccountError -> code 1, message
    qui = "human:%s" % (os.environ.get("USER") or getpass.getuser())
    db = _open(cfg)
    try:
        if what == "use":
            items = declares.get(args.harness)
            if not items:
                print("aucun compte déclaré pour %s (clé `accounts` de la configuration "
                      "de l'hôte)" % args.harness, file=sys.stderr)
                return 1
            accounts_mod.force(db, cfg.host, args.harness, items, args.account, by=qui)
            texte = ("Compte %s forcé pour %s sur %s par %s : plus de choix automatique "
                     "jusqu'à « ameesh accounts auto »." % (args.account, args.harness,
                                                            cfg.host, qui))
            fil.record(cfg, db, sender=qui, recipients=[], text=texte,
                       meta={"action": "compte", "type": "manuel", "harnais": args.harness,
                             "vers": args.account, "audit": "accounts"})
            print(texte)
            return 0
        if what == "auto":
            noms = [args.harness] if args.harness else sorted(declares)
            for nom in noms:
                if nom not in declares:
                    print("aucun compte déclaré pour %s" % nom, file=sys.stderr)
                    return 1
                if accounts_mod.automatic(db, cfg.host, nom, by=qui):
                    texte = ("Comptes %s sur %s : retour au choix automatique (%s)."
                             % (nom, cfg.host, qui))
                    fil.record(cfg, db, sender=qui, recipients=[], text=texte,
                               meta={"action": "compte", "type": "auto", "harnais": nom,
                                     "audit": "accounts"})
                    print(texte)
                else:
                    print("comptes %s : déjà en automatique" % nom)
            return 0
        book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db)
        # L71 : `accounts list` est un affichage — aucun relevé écrit
        rows = accounts_mod.report(cfg, db, book, record=False)
        for row in rows:
            row.pop("_gauges", None)
        bascules = storage.of(db).accounts.switches(cfg.host, None, max(0, getattr(args, "last", 5))) \
            if declares else []
        if getattr(args, "json", False):
            print(json.dumps({"schema": "ameesh-accounts/1", "host": cfg.host,
                              "accounts": rows, "switches": bascules},
                             ensure_ascii=False, indent=2, default=str))
            return 0
        print(accounts_mod.format_rows(rows))
        if bascules:
            print()
            print("dernières bascules :")
            for b in bascules:
                print("  %s  %-8s %-7s %s → %s  %s" % (
                    _fmt_moment(b["at_ts"]), b["harness"], b["kind"],
                    b["from_account"] or "—", b["to_account"], b["reason"] or ""))
        return 0
    finally:
        db.close()


def _since_seconds(text: str) -> float:
    from . import progress
    now = time.time()
    return max(0.0, now - progress.parse_since(text, now))


def _cost_l26(cfg: Config, db, what: str, args) -> int:
    """`cost turns|gauges|balance` (L26) : lectures du grand livre, de
    l'historique des jauges et des soldes ; le calcul des coûts n'est pas
    touché. Schémas : docs/EXPLOITATION.md."""
    ops = storage.of(db).operations
    if what == "turns":
        rows = ops.turns(agent=args.agent, since_s=_since_seconds(args.since),
                         limit=args.limit)
        if args.json:
            print(json.dumps({"schema": "ameesh-turns/1", "turns": rows},
                             ensure_ascii=False, indent=2))
            return 0
        print("%-19s %-16s %-9s %10s %10s %9s %10s" % (
            "QUAND", "AGENT", "HARNAIS", "ENTRÉE", "CACHE", "SORTIE", "USD"))
        for row in rows:
            print("%-19s %-16s %-9s %10d %10d %9d %10.4f" % (
                _fmt_moment(row["recorded_ts"]), row["agent"][:16], row["harness"][:9],
                row["input_tokens"], row["cached_input_tokens"], row["output_tokens"],
                row["usd"]))
        return 0
    if what == "gauges":
        if args.record:
            # `--record` : un relevé frais d'abord, depuis les journaux locaux
            # de l'hôte, comme `cost report` (une ligne seulement si la jauge
            # a bougé). L30 : un harnais à comptes déclarés est relevé compte
            # par compte. Sans l'option (L61), la commande ne fait que lire
            # l'historique : l'exécuteur relève déjà avant chaque tour.
            book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db)
            declares = accounts_mod.parse(cfg.accounts)
            accounts_mod.report(cfg, db, book,
                                harnesses=[args.harness] if args.harness else None)
            for nom in ([args.harness] if args.harness else ["claude", "codex"]):
                if nom not in declares:
                    book.gauges(nom)
        rows = ops.gauge_history(since_s=_since_seconds(args.since), harness=args.harness)
        if args.json:
            print(json.dumps({"schema": "ameesh-gauges/1", "readings": rows},
                             ensure_ascii=False, indent=2))
            return 0
        for row in rows:
            print("%s  %-7s %-16s %5.1f%%  remise %s" % (
                _fmt_moment(row["observed_ts"]),
                row["harness"] + ("/%s" % row["account"] if row.get("account") else ""),
                row["key"],
                float(row["used"]) * 100, _fmt_moment(row.get("resets_at_ts"))))
        return 0
    from . import balance as balance_mod
    names = [args.provider] if args.provider else None
    if args.provider and args.provider not in balance_mod.SOURCES:
        print(USAGE_HINT % ("fournisseur sans source de solde : %s (connus : %s)"
                            % (args.provider, ", ".join(balance_mod.SOURCES))),
              file=sys.stderr)
        return 2
    errors = []
    if args.record:
        # L30 : un relevé par compte de clé d'API déclaré
        for source, compte in balance_mod.account_sources(cfg, names=names):
            nom = source.provider + ("/%s" % compte if compte else "")
            if not source.configured():
                errors.append("%s : clé absente de l'environnement" % nom)
                continue
            try:
                balance_mod.record(db, source, compte)
            except balance_mod.BalanceError as exc:
                errors.append("%s : %s" % (nom, exc))
    rows = ops.balances(provider=args.provider, since_s=_since_seconds(args.since))
    report = dict(balance_mod.spend(rows), schema="ameesh-balance/1", errors=errors)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for row in report["latest"]:
            print("solde %s : %.4f %s (relevé %s)" % (
                row["provider"] + ("/%s" % row["account"] if row.get("account") else ""),
                row["total"], row["currency"],
                _fmt_moment(row["observed_ts"])))
        for slot in report["daily"]:
            print("jour  %s  %s %.4f %s" % (slot["start"][:10], slot["provider"],
                                            slot["spent"], slot["currency"]))
        for slot in report["hourly"][-24:]:
            print("heure %s  %s %.4f %s" % (slot["start"][:16], slot["provider"],
                                            slot["spent"], slot["currency"]))
        for error in errors:
            print("erreur : %s" % error, file=sys.stderr)
    return 1 if errors and args.record and not rows else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    cfg = config_mod.load()
    try:
        return args.func(cfg, args)
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, authority.AuthorityError, work.WorkError, ValueError,
            FileNotFoundError) as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
