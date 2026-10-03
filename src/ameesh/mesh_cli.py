#!/usr/bin/env python3
"""agent-mesh — la CLI du mesh : observabilité, clés, approbations, lots.

  agent-mesh list [--json]                     mesh list : agents, hôte, bail, non lus, budget
  agent-mesh show <agent> [--json]             détail d'un agent
  agent-mesh key generate --out DIR --i-am-the-owner   paire de clés (acte du propriétaire)
  agent-mesh key register <agent> --public-key FICHIER [--role owner|agent]
  agent-mesh key show <agent> | key list | key revoke <agent>
  agent-mesh approve --key FICHIER --action A --hash H [--kind K] [--expires 48h]
                                                signe une approbation liée à un artefact
  agent-mesh approvals [--action A] [--hash H] [--json]
  agent-mesh verify --action A --hash H [--consume] [--by NOM]
                                                une approbation valide existe-t-elle ?
  agent-mesh work add --title T [--type bug|evolution] [--app A] [--assignee N] …
  agent-mesh work list [--state S] | work show <id> | work move <id> <état> | work note <id> "…"
  agent-mesh import-v0 [--agents a,b] [--dry-run]     bascule : boîte fichier v0 → Postgres
  agent-mesh export-v0 [--agents a,b] [--keep]        retour arrière : Postgres → boîte v0
  agent-mesh migrate | doctor [--notify-test]

L'autorité du propriétaire ne se déduit jamais d'un texte : elle se prouve par
une signature Ed25519 dont la clé publique est dans agent_registry, liée au
contenu et à une échéance. `key generate` est un acte du propriétaire : aucun
agent ne doit générer ni détenir sa clé privée.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import authority, config as config_mod, db as db_mod, identity, migrations, registry
from . import signing, work
from .config import Config
from .db import Db

USAGE_HINT = "agent-mesh: %s"


def _open(cfg: Config) -> Db:
    db = db_mod.connect(cfg)
    db_mod.require_schema(db)
    return db


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "il y a %ds" % seconds
    if seconds < 3600:
        return "il y a %dm" % (seconds // 60)
    if seconds < 86400:
        return "il y a %dh" % (seconds // 3600)
    return "il y a %dj" % (seconds // 86400)


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
    try:
        rows = registry.overview(db)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("aucun agent connu")
            return 0
        now = time.time()
        print("%-20s %-9s %-10s %-11s %-22s %-8s %-13s %-4s %s" % (
            "NOM", "HARNAIS", "HÔTE", "STATUT", "BAIL", "NON LUS", "BUDGET", "CLÉ", "VU"))
        for row in rows:
            status = row.get("status") or "?"
            if row.get("status_text"):
                status = "%s/%s" % (status, row["status_text"][:22])
            print("%-20s %-9s %-10s %-11s %-22s %-8d %-13s %-4s %s" % (
                row["name"][:20], (row.get("harness") or "?")[:9], (row.get("host") or "")[:10],
                status[:11], _lease(row), int(row.get("unread") or 0), _budget(row),
                ("owner" if row.get("has_owner_key")
                 else "agent" if row.get("key_ready") else "—"),
                _fmt_age(now - float(row.get("last_seen_ts") or 0)),
            ))
        return 0
    finally:
        db.close()


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        row = registry.get(db, args.agent)
        if row is None:
            print(USAGE_HINT % ("agent inconnu : %s" % args.agent), file=sys.stderr)
            return 1
        info = authority.key_info(db, args.agent)
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
        print("session  : %s" % (row.get("session_id") or "—"))
        print("bail     : %s" % _lease(row))
        print("modèle   : %-9s budget : %s" % (row.get("model") or "—", _budget(row)))
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
            rows = db.query(
                """
                SELECT name, public_key_fingerprint, key_role,
                       extract(epoch from key_updated_at)::float8 as key_updated_ts,
                       extract(epoch from key_revoked_at)::float8 as key_revoked_ts
                  FROM agent_registry
                 WHERE public_key IS NOT NULL
                 ORDER BY name
                """
            )
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

def cmd_work(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        if args.work_command == "add":
            item = work.add(
                db, title=args.title, type=args.type, source=args.source, app=args.app,
                body=args.body or "", issue_ref=args.issue_ref, workstream=args.workstream,
                assignee=args.assignee, budget_usd=args.budget, actor=args.actor)
            print("lot #%d créé en %s : %s" % (item["id"], item["state"], item["title"]))
            return 0
        if args.work_command == "list":
            rows = work.list_items(db, state=args.state, assignee=args.assignee, limit=args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print("aucun lot")
                return 0
            print("%-5s %-10s %-14s %-12s %-10s %s" % (
                "ID", "TYPE", "ÉTAT", "ASSIGNÉ", "MAJ", "TITRE"))
            now = time.time()
            for row in rows:
                print("%-5d %-10s %-14s %-12s %-10s %s" % (
                    row["id"], row["type"], row["state"], (row.get("assignee") or "—")[:12],
                    _fmt_age(now - float(row.get("updated_ts") or 0)),
                    row["title"][:60]))
            return 0
        if args.work_command == "show":
            item = work.get(db, args.id)
            if item is None:
                print(USAGE_HINT % ("lot %s introuvable" % args.id), file=sys.stderr)
                return 1
            item["events"] = work.events(db, args.id)
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
            if item.get("body"):
                print("corps    : %s" % item["body"])
            print("créé     : %s   maj : %s   fermé : %s" % (
                _fmt_moment(item.get("created_ts")), _fmt_moment(item.get("updated_ts")),
                _fmt_moment(item.get("closed_ts"))))
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
            # le registre v0 (agents/<nom>.json) donne harnais, dossier, session
            agent_file = os.path.join(state, "agents", name + ".json")
            if os.path.exists(agent_file):
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
                 if args.agents else [row["recipient"] for row in db.query(
                     "SELECT DISTINCT recipient FROM agent_mailbox "
                     "WHERE delivered_at IS NULL ORDER BY recipient")])
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
    return cli.cmd_doctor(cfg, notify_test=args.notify_test)


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

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
    pw_add.set_defaults(func=cmd_work)
    pw_list = work_sub.add_parser("list")
    pw_list.add_argument("--state", default=None)
    pw_list.add_argument("--assignee", default=None)
    pw_list.add_argument("--limit", type=int, default=50)
    pw_list.add_argument("--json", action="store_true")
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
    pw_note = work_sub.add_parser("note")
    pw_note.add_argument("id", type=int)
    pw_note.add_argument("text")
    pw_note.add_argument("--actor", default="")
    pw_note.set_defaults(func=cmd_work)

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
    p_doc.set_defaults(func=cmd_doctor)
    return parser


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
