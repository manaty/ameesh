# SPDX-License-Identifier: AGPL-3.0-only
"""#7106 (avatar B3) : hôte partagé multi-tenant.

docs/design/5322-avatar-humain.md §3.4. Un hôte dont la fiche Host déclare
`policy.shared` héberge plusieurs **tenants** (un par humain) :

* chaque tenant a un utilisateur système propre (`ameesh-<id>`, home 0700,
  `linger`), un runner propre (`ameesh-runner-tenant@<id>.service`) dont
  `tenant` vaut `human:<id>`, et ses dossiers de comptes sous son home ;
  sur l'hôte partagé, des clés d'organisation seulement (décision Q2,
  #213954) ;
* le **superviseur d'hôte** (le runner de l'utilisateur `ameesh`, sans
  `tenant`) garde l'état machine : relevés, cycle de vie, portes, ménage ;
  il ne réclame aucun agent ;
* un runner de tenant ne publie pas `host_resources` : il bat
  `runner_presence` et prend les baux de ses agents.

La garde est en base (`ameesh_tenant_claim_ok`, migration 0575) : le runner
lié au tenant `responsible` de l'agent, et lui seul, prend le bail ; le
filtre de ce module n'est qu'une optimisation et un diagnostic.

Limite honnête (§3.4) : l'identité de l'exécuteur reste celle de sa
configuration ; l'isolation SQL par rôle est un travail distinct (0033/L155).
"""
from __future__ import annotations

import os
import pwd
import re
import shlex
from typing import Any, Mapping

from . import canon as canon_mod
from . import storage

#: identifiant d'un tenant : `human:<id>` (même grammaire que la contrainte
#: `host_tenants_tenant_valid` de 0575)
TENANT_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._@-]*$")
#: unité systemd du runner d'un tenant (instance = identifiant du membre)
TENANT_UNIT = "ameesh-runner-tenant@%s.service"
#: préfixe de l'utilisateur système d'un tenant
USER_PREFIX = "ameesh-"


class TenancyError(ValueError):
    """Refus d'un geste de tenant (code stable, message lisible)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check_tenant(tenant: str) -> str:
    """`human:<id>` validé et normalisé, sinon TenancyError."""
    key = canon_mod.tenant_key(tenant)
    if not key or not TENANT_RE.match(key):
        raise TenancyError("tenant-invalid", "tenant %r: human:<id> expected" % tenant)
    return key


def unix_user(tenant: str) -> str:
    """Utilisateur système dérivé du tenant, non secret (`ameesh-<id>`).

    Minuscules, caractères hors `[a-z0-9_-]` remplacés par `-`, borné à 32
    caractères (limite de `useradd`)."""
    ident = check_tenant(tenant)[len("human:"):].lower()
    ident = re.sub(r"[^a-z0-9_-]", "-", ident).strip("-") or "tenant"
    return (USER_PREFIX + ident)[:32].rstrip("-")


def tenant_home(tenant: str) -> str:
    """Home de l'utilisateur système du tenant (celui que crée `add_plan`)."""
    return "/home/%s" % unix_user(tenant)


def runner_id(host: str, tenant: str) -> str:
    """Identifiant d'exécuteur du runner d'un tenant (stable, sans secret)."""
    return "%s/%s" % (host, check_tenant(tenant)[len("human:"):])


# --------------------------------------------------------------------------
# rôle de l'exécuteur sur cet hôte
# --------------------------------------------------------------------------
def runner_tenant(cfg) -> str:
    """Le tenant de cet exécuteur (`tenant` / `AMEESH_TENANT`), ou ""."""
    return canon_mod.tenant_key(getattr(cfg, "tenant", "") or "")


def host_policy(db, host: str) -> dict:
    """`{shared, tenants}` de l'hôte selon le canon synchronisé."""
    return storage.of(db).tenancy.policy(host)


def admits(cfg, policy: Mapping, agent: Mapping) -> bool:
    """Cet exécuteur peut-il réclamer `agent` sur un hôte de cette politique ?

    Hôte non partagé : oui (règles inchangées). Hôte partagé : seulement le
    runner d'un tenant, pour les agents dont il est le `responsible` ; le
    superviseur (sans tenant) ne réclame rien. Optimisation : la base refuse
    de toute façon (`ameesh_tenant_claim_ok`)."""
    if not policy.get("shared"):
        return True
    tenant = runner_tenant(cfg)
    return bool(tenant) and canon_mod.tenant_key(agent.get("responsible")) == tenant


def beat(cfg, db, *, version: str | None = None) -> bool:
    """Battement du runner d'un tenant (`runner_presence`) ; faux sans tenant
    configuré ou sans liaison qui nomme ce runner (le superviseur n'a pas
    encore enregistré le tenant, ou l'identifiant diffère)."""
    tenant = runner_tenant(cfg)
    if not tenant:
        return False
    if version is None:
        from . import version as package_version
        version = package_version()
    return storage.of(db).tenancy.beat(cfg.host, tenant, cfg.runner, uid=os.getuid(),
                                       version=str(version or "") or None)


def presence_reason(db, role: Mapping, host: str, *, now: float,
                    limit_s: float) -> str:
    """§3.2 point 5 : sur un hôte partagé, un runner du tenant de l'agent est
    vivant (battement frais au seuil de l'hôte). "" si présent ou si l'hôte
    n'est pas partagé ; sinon la raison."""
    policy = host_policy(db, host)
    if not policy.get("shared"):
        return ""
    tenant = canon_mod.tenant_key(role.get("responsible"))
    if not tenant or tenant not in policy.get("tenants", []):
        return "responsible %s is not a tenant of shared host %s" % (
            tenant or "undeclared", host)
    row = storage.of(db).tenancy.presence(host, tenant)
    if row is None or row.get("seen_ts") is None:
        return "no runner of tenant %s on shared host %s" % (tenant, host)
    clock = max(float(now), float(row.get("now_ts") or 0.0))
    age = clock - float(row["seen_ts"])
    if age > limit_s:
        return "runner of tenant %s silent for %ds (limit %ds)" % (
            tenant, int(age), int(limit_s))
    return ""


# --------------------------------------------------------------------------
# isolation par UID vérifié (persona_memory.require_isolation)
# --------------------------------------------------------------------------
def isolation_problem(db, host: str, agent: Mapping, peers: list[Mapping], *,
                      uid: int | None = None) -> str:
    """Sur un hôte partagé : "" si `agent` et chaque pair actif tournent sous
    des UID vérifiés **distincts** ; sinon la raison du refus.

    L'UID d'un agent est celui de la liaison de son tenant (`host_tenants`,
    écrite par le superviseur), jamais une déclaration de l'agent. Le
    processus courant doit tourner sous l'UID du tenant de l'agent. Deux
    personas du même tenant, ou un pair sans UID vérifié, restent refusés."""
    uid = os.getuid() if uid is None else int(uid)
    bound = {row["tenant"]: row for row in storage.of(db).tenancy.bindings(host)}
    mine = bound.get(canon_mod.tenant_key(agent.get("responsible")))
    if mine is None:
        return "agent %s has no verified tenant UID on shared host %s" % (
            agent.get("name"), host)
    if int(mine["uid"]) != uid:
        return ("process UID %d is not the UID %d of tenant %s"
                % (uid, int(mine["uid"]), mine["tenant"]))
    for peer in peers:
        other = bound.get(canon_mod.tenant_key(peer.get("responsible")))
        if other is None:
            return "active persona %s has no verified tenant UID" % peer.get("name")
        if int(other["uid"]) == int(mine["uid"]):
            return "active persona %s shares UID %d" % (peer.get("name"), int(mine["uid"]))
    return ""


# --------------------------------------------------------------------------
# provisionnement (source only : `ameesh host tenant add|list|remove`)
# --------------------------------------------------------------------------
def unit_text(host: str, tenant: str, *, ameesh_bin: str = "/usr/local/bin/ameesh") -> str:
    """Unité utilisateur du runner d'un tenant (installée sous son home).

    `AMEESH_RUNNER_ID` est l'identifiant que `register` lie (`runner_id`) :
    sans lui, l'exécuteur vaudrait `<hôte>:<pid>`, ni battement ni bail."""
    ident = check_tenant(tenant)[len("human:"):]
    return """[Unit]
Description=ameesh runner of tenant human:%(ident)s (shared host, #7106)
After=network-online.target

[Service]
Type=simple
Environment=AMEESH_TENANT=human:%(ident)s
Environment=AMEESH_RUNNER_ID=%(runner)s
Environment=AMEESH_CONFIG=%%h/.config/ameesh/config.json
ExecStart=%(bin)s run
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=default.target
""" % {"ident": ident, "runner": runner_id(host, tenant), "bin": ameesh_bin}


def add_plan(host: str, tenant: str) -> list[list[str]]:
    """Gestes système d'un tenant neuf, dans l'ordre ; rien n'est exécuté ici.

    Utilisateur sans groupe partagé (groupe personnel), home 0700, `linger`
    pour ses unités utilisateur, dossier de comptes 0700 sous son home ;
    les secrets (clés d'organisation, Q2) sont installés ensuite par
    l'humain ou son tenant, jamais par ce plan ni par le canon."""
    user = unix_user(tenant)
    home = tenant_home(tenant)
    return [
        ["useradd", "--create-home", "--home-dir", home, "--user-group",
         "--shell", "/usr/sbin/nologin", user],
        ["chmod", "0700", home],
        ["install", "-d", "-m", "0700", "-o", user, "-g", user,
         home + "/.config/ameesh", home + "/.config/ameesh/accounts",
         home + "/.config/systemd/user"],
        ["loginctl", "enable-linger", user],
    ]


def register(db, host: str, tenant: str, *, actor: str,
             uid: int | None = None, user: str | None = None) -> dict:
    """Liaison tenant → utilisateur système, UID et runner (`host_tenants`).

    Refusée si le canon ne déclare pas l'hôte partagé avec ce tenant
    (`tenant-not-declared`) ou si l'utilisateur système n'existe pas
    (`tenant-user-missing`) : l'UID enregistré est celui que le système
    rend, jamais une valeur saisie."""
    tenant = check_tenant(tenant)
    policy = host_policy(db, host)
    if not policy.get("shared") or tenant not in policy.get("tenants", []):
        raise TenancyError("tenant-not-declared",
                           "host %s: %s is not a tenant declared by the canon "
                           "(policy.shared/tenants; run \"ameesh canon sync\" first)"
                           % (host, tenant))
    user = user or unix_user(tenant)
    if uid is None:
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            raise TenancyError("tenant-user-missing",
                               "system user %s does not exist on %s (see the "
                               "`add` plan)" % (user, host)) from None
    if int(uid) <= 0:
        raise TenancyError("tenant-uid-invalid", "UID %r refused" % uid)
    return storage.of(db).tenancy.bind(host, tenant, unix_user=user, uid=int(uid),
                                       runner_id=runner_id(host, tenant), actor=actor)


def remove(db, host: str, tenant: str) -> dict:
    """Retire la liaison ; refusé tant qu'un agent du tenant reste sur l'hôte."""
    tenant = check_tenant(tenant)
    result = storage.of(db).tenancy.unbind(host, tenant)
    if result["blocking"]:
        raise TenancyError("tenant-busy",
                           "tenant %s still has agents on %s (lease, turn or not "
                           "moved): %s" % (tenant, host, ", ".join(result["blocking"])))
    return result


def shell_plan(host: str, tenant: str) -> str:
    """Le plan lisible de `add` (commandes citées, unité à installer)."""
    user = unix_user(tenant)
    lines = ["# #7106: tenant %s on shared host %s (run as root by the bureau)"
             % (check_tenant(tenant), host)]
    lines += [" ".join(shlex.quote(part) for part in step) for step in add_plan(host, tenant)]
    lines.append("# unit %s, installed as %s:"
                 % (TENANT_UNIT % tenant[len("human:"):],
                    "/home/%s/.config/systemd/user/ameesh-runner-tenant@.service" % user))
    lines += ["#   " + line for line in unit_text(host, tenant).splitlines()]
    lines.append("# %s: the tenant's configuration (database, no secret in this plan),"
                 " installed by the bureau like the organization keys"
                 % ("/home/%s/.config/ameesh/config.json" % user))
    lines.append("# then (as the host supervisor): ameesh host tenant add %s --host %s"
                 " --register --actor <human:id of the registrant>"
                 % (shlex.quote(check_tenant(tenant)), shlex.quote(host)))
    return "\n".join(lines)


def describe(rows: list[Mapping[str, Any]]) -> list[str]:
    """Lignes de `ameesh host tenant list`."""
    out = []
    for row in rows:
        seen = row.get("seen_ts")
        age = ("never" if seen is None
               else "%ds ago" % int(max(0.0, float(row.get("now_ts") or 0) - float(seen))))
        out.append("%s  %s  user=%s uid=%s runner=%s  seen %s%s" % (
            row["host"], row["tenant"], row["unix_user"], row["uid"], row["runner_id"],
            age, "  (%s)" % row["version"] if row.get("version") else ""))
    return out


# --------------------------------------------------------------------------
# `ameesh host tenant add|list|remove`
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    """`ameesh host tenant …` : geste d'infrastructure (bureau ou humain,
    jamais un agent ni le runner d'un tenant).

    `add` sans `--register` imprime le plan système (utilisateur, home 0700,
    linger, dossier de comptes, unité) et n'exécute rien ; `--register`
    écrit la liaison en base, une fois l'utilisateur créé. `remove` est
    refusé tant qu'un agent du tenant reste sur l'hôte."""
    import argparse
    import json

    from . import config
    from . import db as db_mod
    from . import work

    parser = argparse.ArgumentParser(prog="ameesh host tenant")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="plan (and register) a tenant of a shared host")
    add.add_argument("tenant", help="human:<id>")
    add.add_argument("--host", help="shared host (default: this host)")
    add.add_argument("--register", action="store_true",
                     help="write the binding (system user, UID, runner) in the database")
    add.add_argument("--actor", help="who registers (human:<id>) from an unbound terminal")
    listing = commands.add_parser("list", help="tenants and runner presence")
    listing.add_argument("--host")
    listing.add_argument("--json", action="store_true")
    drop = commands.add_parser("remove", help="remove a tenant binding (no agent left)")
    drop.add_argument("tenant")
    drop.add_argument("--host")
    drop.add_argument("--actor", help="who removes (human:<id>) from an unbound terminal")
    args = parser.parse_args(argv)
    cfg = config.load()
    host = args.host or cfg.host
    if args.command == "add" and not args.register:
        try:
            print(shell_plan(host, args.tenant))
        except TenancyError as exc:
            print("tenant refused (%s): %s" % (exc.code, exc))
            return 1
        return 0
    db = db_mod.connect(cfg)
    try:
        if args.command == "list":
            rows = storage.of(db).tenancy.bindings(args.host)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False))
            else:
                print("\n".join(describe(rows)) or "no tenant")
            return 0
        if runner_tenant(cfg):
            raise TenancyError("tenant-runner-refused",
                               "a tenant runner configuration (%s) cannot write "
                               "tenant bindings: the host supervisor does"
                               % runner_tenant(cfg))
        actor = work.corrector(cfg, db, args.actor, what="tenant registration",
                               history=False)
        if args.command == "add":
            result = register(db, host, args.tenant, actor=actor)
        else:
            result = remove(db, host, args.tenant)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except TenancyError as exc:
        print("tenant refused (%s): %s" % (exc.code, exc))
        return 1
    except (work.WorkError, db_mod.DbError) as exc:
        print("tenant refused: %s" % exc)
        return 1
    finally:
        db.close()
