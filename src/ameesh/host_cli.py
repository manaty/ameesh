# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh host | device — enrôlement des appareils prêtés (lot L110).

Sur le serveur du mesh (humain habilité seulement, jamais une session
d'agent) :

  ameesh host enroll <hôte> --by human:ID [--mesh M] [--agents a,b] [--ttl 15m] [--json]
        émet un code d'enrôlement à usage unique, de courte durée (15 min par
        défaut, 1 h au plus), lié au mesh, à l'hôte et, si donnée, à la liste
        d'agents (chacun doit être admis sur l'hôte par le canon) ;
  ameesh host revoke <hôte> --by human:ID --why TEXTE [--executor ID] [--json]
        révoque les exécuteurs de l'hôte (ou un seul) : jetons refusés, baux
        relâchés, codes en attente annulés ;
  ameesh host list [--json]             exécuteurs enrôlés et codes en attente ;
  ameesh host show <hôte> [--json]      fiche, occupants, exécuteurs, baux, journal.

Habilité : le responsable de la fiche `Host` visée, ou un membre de rôle
coordinateur (`coordinator`, `coordinateur`, `coordinatrice`) du canon qui
déclare l'hôte. La fiche `Host` doit exister au canon.

Dans la VM de l'appareil :

  ameesh device enroll --server URL (--code-file FICHIER|- | --code CODE)
                       [--label L] [--attestation FICHIER]
        génère la clé P-256 (0600, volume persistant), s'enrôle, garde l'état ;
  ameesh device challenge --server URL (--code-file FICHIER|- | --code CODE)
        le défi à faire signer par la clé d'appareil Nexlink (facultatif) ;
  ameesh device show [--json]           état de l'appareil (sans secret).

Le code d'enrôlement se passe de préférence par `--code-file` (ou `-` pour
l'entrée standard) : `--code` le laisse dans /proc/<pid>/cmdline.

`AMEESH_EXEC_HOME` (défaut /var/lib/ameesh-exec) : dossier de la clé et de
l'état de l'appareil.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import canon as canon_mod
from . import config as config_mod
from . import db as db_mod
from .config import NAME_RE, Config

COORDINATOR_ROLES = frozenset({"coordinator", "coordinateur", "coordinatrice"})
#: variables qu'un exécuteur pose pour le harnais d'un agent : leur présence
#: signe une session d'agent
AGENT_SESSION_VARS = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                      "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH", "AMEESH_EXEC_TOKEN")


class Refused(Exception):
    """Commande refusée (habilitation, canon) ; code de sortie 3."""


def _moment(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "—"


def agent_session(cfg: Config, db) -> str | None:
    """Pourquoi cette commande tourne dans une session d'agent, ou None."""
    for var in AGENT_SESSION_VARS:
        if os.environ.get(var):
            return "variable %s posée (session d'agent)" % var
    try:
        from . import session_bindings
        row = session_bindings.by_ancestry(cfg, db)
    except Exception:  # table absente, base en panne : la variable suffit
        row = None
    if row:
        return "session liée à l'agent %s (ameesh mail bind)" % row.get("agent")
    return None


def _host_canon(cfg: Config, host: str, mesh: str | None):
    """(canon, fiche Host) : le canon qui déclare l'hôte (celui du mesh si
    `mesh` nomme un canon configuré)."""
    try:
        canons = canon_mod.load_configured(cfg)
    except canon_mod.CanonError as exc:
        raise Refused("canon illisible : %s" % exc)
    if not canons:
        raise Refused("aucun canon configuré : la fiche Host de %s ne peut être lue" % host)
    found = canon_mod.host_fiches(canons, host)
    if mesh:
        named = [(c, h) for c, h in found if c.id == mesh]
        found = named or found
    if not found:
        raise Refused("hôte %s sans fiche Host au canon : déclarez-la d'abord" % host)
    return found[0]


def authorize(canon, host_fiche, by: str) -> str:
    """L'humain `by` peut-il enrôler ou révoquer cet hôte ? Rend la raison
    d'admission ; lève `Refused`."""
    if not by or not by.startswith("human:"):
        raise Refused("--by human:<id> requis : seul un humain enrôle ou révoque un hôte")
    resolved = canon.resolve_human(by)
    if resolved is None:
        raise Refused("%s ne résout pas vers un Member humain du canon" % by)
    if resolved == canon.resolve_human(host_fiche.responsible):
        return "responsable de l'hôte"
    ident = resolved.split(":", 1)[1]
    member = next((m for m in canon.members if m.title == ident), None)
    roles = {str(r).strip().lower() for r in (member.roles or [])} if member else set()
    if roles & COORDINATOR_ROLES:
        return "coordinateur du mesh"
    raise Refused("%s n'est ni le responsable de l'hôte %s (%s) ni coordinateur du mesh"
                  % (by, host_fiche.title, host_fiche.responsible or "—"))


def admitted_agents(canon, host_fiche) -> set:
    """Agents que le canon admet sur cet hôte (nommé ou par étiquette)."""
    tags = set(host_fiche.tags or [])
    return {p.agent for p in canon.placements
            if p.agent and (host_fiche.title in p.host_names() or tags & set(p.tags()))}


def _guard(cfg: Config, db, args) -> tuple:
    why = agent_session(cfg, db)
    if why:
        raise Refused("refusé : %s ; seul un humain habilité enrôle ou révoque un hôte" % why)
    canon, fiche = _host_canon(cfg, args.host, getattr(args, "mesh", None))
    reason = authorize(canon, fiche, args.by)
    return canon, fiche, reason


# --------------------------------------------------------------------------
# serveur
# --------------------------------------------------------------------------

def cmd_enroll(cfg: Config, db, args) -> int:
    from . import authority
    from .mediated_executor import identity
    canon, fiche, reason = _guard(cfg, db, args)
    agents = None
    if args.agents:
        agents = sorted({a.strip() for a in args.agents.split(",") if a.strip()})
        admitted = admitted_agents(canon, fiche)
        outside = [a for a in agents if a not in admitted]
        if outside:
            raise Refused("agent(s) non admis sur %s par le canon : %s"
                          % (args.host, ", ".join(outside)))
    mesh = args.mesh or canon.id
    try:
        ttl = authority.parse_ttl(args.ttl, identity.INVITATION_TTL_DEFAULT_S)
    except authority.AuthorityError as exc:
        raise Refused(str(exc))
    if ttl > identity.INVITATION_TTL_MAX_S:
        raise Refused("code d'enrôlement : %d min au plus" % (identity.INVITATION_TTL_MAX_S // 60))
    invitation = identity.create_invitation(db, mesh=mesh, host=args.host, created_by=args.by,
                                            agents=agents, ttl_s=ttl)
    invitation["authorized_as"] = reason
    if args.json:
        print(json.dumps({"schema": "ameesh-host-enroll/1", **invitation},
                         ensure_ascii=False, indent=2))
        return 0
    print("code d'enrôlement de %s (mesh %s) : %s" % (args.host, mesh, invitation["code"]))
    print("  à usage unique, valable jusqu'à %s ; émis par %s (%s)"
          % (_moment(invitation["expires_ts"]), args.by, reason))
    print("  agents : %s" % (", ".join(agents) if agents else "ceux que le canon admet"))
    print("  dans la VM : ameesh device enroll --server <URL du serveur> --code-file -"
          " (code sur l'entrée standard : jamais dans la ligne de commande)")
    return 0


def cmd_revoke(cfg: Config, db, args) -> int:
    from .mediated_executor import identity
    _guard(cfg, db, args)
    if not args.why or not args.why.strip():
        raise Refused("--why TEXTE requis (journal de la révocation)")
    result = identity.revoke_host(db, args.host, by=args.by, why=args.why.strip(),
                                  executor_id=args.executor)
    if args.json:
        print(json.dumps({"schema": "ameesh-host-revoke/1", **result},
                         ensure_ascii=False, indent=2))
        return 0
    if not result["executors"] and not result["invitations_cancelled"]:
        print("rien à révoquer sur %s" % args.host)
        return 0
    print("hôte %s : %d exécuteur(s) révoqué(s) %s, %d bail(aux) relâché(s)%s, "
          "%d code(s) annulé(s)" % (
              args.host, len(result["executors"]), ", ".join(result["executors"]),
              len(result["released"]),
              " (%s)" % ", ".join(result["released"]) if result["released"] else "",
              result["invitations_cancelled"]))
    print("  rappel : retirez aussi la fiche Host ou l'admission au canon, sinon l'hôte "
          "reste admis pour un prochain enrôlement")
    return 0


def cmd_list(cfg: Config, db, args) -> int:
    from .mediated_executor import identity
    executors = identity.list_executors(db)
    pending = identity.pending_invitations(db)
    if args.json:
        print(json.dumps({"schema": "ameesh-host-list/1", "executors": executors,
                          "invitations": pending}, ensure_ascii=False, indent=2))
        return 0
    if not executors and not pending:
        print("aucun exécuteur enrôlé, aucun code en attente")
        return 0
    if executors:
        print("%-16s %-16s %-10s %-22s %-16s %s" % (
            "EXÉCUTEUR", "HÔTE", "ÉTAT", "ENRÔLÉ PAR", "DERNIER CONTACT", "AGENTS"))
        for row in executors:
            print("%-16s %-16s %-10s %-22s %-16s %s" % (
                row["id"], row["host"][:16], row["state"], (row["enrolled_by"] or "")[:22],
                _moment(row.get("last_seen_ts")),
                ", ".join(row["agents_allowlist"]) if row["agents_allowlist"] else "canon"))
    if pending:
        print("codes en attente :")
        for row in pending:
            print("  %-16s par %s, échéance %s" % (row["host"], row["created_by"],
                                                   _moment(row["expires_ts"])))
    return 0


def cmd_show(cfg: Config, db, args) -> int:
    from .mediated_executor import identity
    fiche_info = None
    try:
        canons = canon_mod.load_configured(cfg)
        found = canon_mod.host_fiches(canons, args.host)
    except canon_mod.CanonError:
        found = []
    if found:
        canon, fiche = found[0]
        fiche_info = {"canon": canon.id, "responsible": fiche.responsible,
                      "admins": list(fiche.admins or []),
                      "occupants": list(fiche.occupants) if fiche.occupants is not None else None,
                      "admitted_agents": sorted(admitted_agents(canon, fiche))}
    executors = identity.list_executors(db, args.host)
    leases = identity.host_leases(db, [e["id"] for e in executors if not e.get("revoked_ts")])
    pending = identity.pending_invitations(db, args.host)
    journal = identity.events(db, args.host)
    if args.json:
        print(json.dumps({"schema": "ameesh-host-show/1", "host": args.host, "fiche": fiche_info,
                          "executors": executors, "leases": leases, "invitations": pending,
                          "events": journal}, ensure_ascii=False, indent=2))
        return 0
    print("hôte %s" % args.host)
    if fiche_info is None:
        print("  fiche Host : absente du canon")
    else:
        print("  responsable : %s ; admins : %s" % (
            fiche_info["responsible"] or "—", ", ".join(fiche_info["admins"]) or "—"))
        print("  occupants   : %s" % (
            ", ".join(fiche_info["occupants"]) if fiche_info["occupants"]
            else "aucun déclaré (seules les personas lisibles par le responsable et les "
                 "admins partent ici)"))
        print("  admis par le canon : %s" % (", ".join(fiche_info["admitted_agents"]) or "—"))
    if not executors:
        print("  aucun exécuteur enrôlé")
    for row in executors:
        print("  exécuteur %s : %s, enrôlé %s par %s, dernier contact %s%s" % (
            row["id"], row["state"], _moment(row["enrolled_ts"]), row["enrolled_by"],
            _moment(row.get("last_seen_ts")),
            ", appareil Nexlink lié" if row.get("device_key_sha256") else ""))
        if row.get("revoked_ts"):
            print("    révoqué %s par %s : %s" % (_moment(row["revoked_ts"]), row["revoked_by"],
                                                  row.get("revoked_why") or ""))
    for lease in leases:
        print("  bail : %s (epoch %s, %s, échéance %s)" % (
            lease["name"], lease["lease_epoch"], lease["status"],
            _moment(lease.get("lease_expires_ts"))))
    for row in pending:
        print("  code en attente, par %s, échéance %s" % (row["created_by"],
                                                          _moment(row["expires_ts"])))
    for ev in journal:
        print("  %s %-22s %s %s" % (_moment(ev["ts"]), ev["kind"], ev["actor"],
                                    ev.get("executor_id") or ""))
    return 0


# --------------------------------------------------------------------------
# appareil
# --------------------------------------------------------------------------

def _read_code(args) -> str:
    """Le code d'enrôlement : `--code`, sinon le fichier `--code-file` (ou
    stdin pour `-`). Jamais recopié dans un journal ni dans l'environnement."""
    if args.code_file is None:
        return args.code
    if args.code_file == "-":
        text = sys.stdin.readline()
    else:
        with open(args.code_file, encoding="ascii") as fh:
            text = fh.read(256)
    code = "".join(text.split())
    if not code:
        raise ValueError("code d'enrôlement vide (%s)" % args.code_file)
    return code


def cmd_device(args) -> int:
    from .mediated_executor import device
    directory = args.home
    if args.device_command in ("challenge", "enroll"):
        args.code = _read_code(args)
    if args.device_command == "challenge":
        key, created = device.load_or_create_key(directory)
        challenge = device.attestation_challenge(key, server_url=args.server, code=args.code)
        sys.stdout.write(challenge.decode("ascii"))
        if created:
            print("(clé de l'exécuteur créée : %s)" % key.path, file=sys.stderr)
        return 0
    if args.device_command == "enroll":
        attestation = None
        if args.attestation:
            with open(args.attestation, encoding="utf-8") as fh:
                attestation = json.load(fh)
        state = device.enroll(args.server, args.code, directory=directory, label=args.label,
                                device_attestation=attestation)
        print("appareil enrôlé : exécuteur %s, hôte %s, mesh %s" % (
            state["executor_id"], state["host"], state["mesh"]))
        return 0
    # show
    state = device.load_state(directory)
    key_path = os.path.join(device.home(directory), device.KEY_FILE)
    info = {"home": device.home(directory), "enrolled": state is not None,
            "state": state, "key": os.path.exists(key_path)}
    if info["key"]:
        key = device.load_key(directory)
        info["thumbprint"] = key.thumbprint()
        info["signer"] = key.signer
    if args.json:
        print(json.dumps({"schema": "ameesh-device-show/1", **info}, ensure_ascii=False, indent=2))
        return 0
    if state is None:
        print("appareil non enrôlé (%s)" % info["home"])
    else:
        print("exécuteur %s, hôte %s, mesh %s, serveur %s" % (
            state["executor_id"], state["host"], state["mesh"], state["server_url"]))
    if info["key"]:
        print("clé : %s (empreinte %s, signature %s)" % (key_path, info["thumbprint"],
                                                          info["signer"]))
    return 0


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ameesh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    top = p.add_subparsers(dest="group", required=True)
    host = top.add_parser("host", help="enrôlement des hôtes médiés (serveur)")
    sub = host.add_subparsers(dest="command", required=True)
    e = sub.add_parser("enroll", help="émettre un code d'enrôlement")
    e.add_argument("host")
    e.add_argument("--by", required=True, help="human:<id> habilité")
    e.add_argument("--mesh", default=None, help="mesh (défaut : identifiant du canon)")
    e.add_argument("--agents", default=None, help="liste blanche d'agents, séparés par des virgules")
    e.add_argument("--ttl", default=None, help="durée du code (défaut 15m, 1h au plus)")
    e.add_argument("--json", action="store_true")
    r = sub.add_parser("revoke", help="révoquer les exécuteurs d'un hôte")
    r.add_argument("host")
    r.add_argument("--by", required=True)
    r.add_argument("--why", required=True)
    r.add_argument("--executor", default=None)
    r.add_argument("--mesh", default=None)
    r.add_argument("--json", action="store_true")
    ls = sub.add_parser("list", help="exécuteurs enrôlés")
    ls.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="un hôte")
    s.add_argument("host")
    s.add_argument("--json", action="store_true")
    dev = top.add_parser("device", help="l'appareil (dans la VM)")
    dsub = dev.add_subparsers(dest="device_command", required=True)
    for name in ("enroll", "challenge"):
        d = dsub.add_parser(name)
        d.add_argument("--server", required=True)
        source = d.add_mutually_exclusive_group(required=True)
        source.add_argument("--code", default=None,
                            help="code d'enrôlement (visible dans la ligne de commande : "
                                 "préférez --code-file)")
        source.add_argument("--code-file", default=None,
                            help="fichier qui contient le code, ou - pour l'entrée standard")
        d.add_argument("--home", default=None)
        if name == "enroll":
            d.add_argument("--label", default="")
            d.add_argument("--attestation", default=None,
                           help="fichier JSON ameesh-device-attestation/1 (Nexlink)")
    d = dsub.add_parser("show")
    d.add_argument("--home", default=None)
    d.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.group == "device":
        from .db import DbError
        from .mediated_executor.device import DeviceError
        try:
            return cmd_device(args)
        except (DeviceError, DbError, OSError, ValueError) as exc:
            print("erreur : %s" % exc, file=sys.stderr)
            return 1
    if getattr(args, "host", None) and not NAME_RE.match(args.host):
        print("erreur : nom d'hôte invalide %r" % args.host, file=sys.stderr)
        return 2
    cfg = config_mod.load()
    db = db_mod.open_db(cfg)
    try:
        handler = {"enroll": cmd_enroll, "revoke": cmd_revoke, "list": cmd_list,
                   "show": cmd_show}[args.command]
        return handler(cfg, db, args)
    except Refused as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 3
    except (ValueError, LookupError) as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
