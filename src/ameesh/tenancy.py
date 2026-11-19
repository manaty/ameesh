# SPDX-License-Identifier: AGPL-3.0-only
"""#7106 (avatar B3) : hôte partagé multi-tenant.

docs/design/5322-avatar-humain.md §3.4. Un hôte dont la fiche Host déclare
`policy.shared` héberge plusieurs **tenants** (un par humain) :

* chaque tenant a un utilisateur système propre (`ameesh-<id>`, home 0700,
  `linger`), un runner propre (`ameesh-runner-tenant@<id>.service`) dont
  `tenant` vaut `human:<id>`, et ses dossiers de comptes sous son home ;
  sur l'hôte partagé, l'abonnement personnel exige l'opt-in et l'isolation
  (décision Q2 amendée #224052, manaty/ameesh#5901) ;
* le **superviseur d'hôte** (le runner de l'utilisateur `ameesh`, sans
  `tenant`) garde l'état machine : relevés, cycle de vie, portes, ménage ;
  il ne réclame aucun agent ;
* un runner de tenant ne publie pas `host_resources` : il bat
  `runner_presence` et prend les baux de ses agents.

La garde est en base (`ameesh_tenant_claim_ok`, migration 0575) : le runner
lié au tenant `responsible` de l'agent, et lui seul, prend le bail ; le
filtre de ce module n'est qu'une optimisation et un diagnostic.

Le provisionnement est un geste de l'avatar ou d'un humain, jamais d'un agent
(§3.4) ; `add --register` se lance sur l'hôte cible, dont le passwd donne
l'UID du tenant.

Limite honnête (§3.4) : l'identité de l'exécuteur reste celle de sa
configuration ; l'isolation SQL par rôle est un travail distinct (0033/L155).
"""
from __future__ import annotations

import hashlib
import json
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
#: #7694 (design 7633 §6.2, N de #7659) : un seul GID neutre par volume et
#: pour tous les pods qui le montent — jamais un `fsGroup` par tenant, dont
#: le passage récursif de kubelet élargirait les modes 0700/0600 au groupe.
POD_FS_GROUP = 10000
#: UID du conteneur superviseur (profil cluster existant) ; jamais un tenant.
POD_SUPERVISOR_UID = 10001
#: plage d'allocation automatique des UID de tenant (stable par personne).
POD_UID_BASE = 2000
POD_UID_SPAN = 1000
#: #7694 (B1 de #44265) : montage du canon dans le pod (lecture seule) et
#: dépôt cloné par l'init du superviseur, lu par le tenant sous SON UID.
POD_CANON = "/var/lib/ameesh/canon"
POD_CANON_REPO = POD_CANON + "/repo"


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
# #7694 : hôte pod — plan de tenant (source only, aucun geste root)
# --------------------------------------------------------------------------
def pod_uid(tenant: str) -> int:
    """UID par défaut d'un tenant de pod : stable pour une personne (D8).

    Dérivé du seul identifiant `human:<id>`, donc le même sur tous les hôtes
    pod et indépendant de l'ordre des tenants. Une collision entre deux
    personnes est possible (1000 valeurs) : le bureau la tranche avec
    `--uid`, que le chart porte ensuite comme source de vérité.
    """
    digest = hashlib.sha256(("ameesh-pod-uid:" + check_tenant(tenant)).encode("utf-8"))
    return POD_UID_BASE + int.from_bytes(digest.digest()[:8], "big") % POD_UID_SPAN


def check_pod_uid(uid: int) -> int:
    """UID de tenant acceptable pour un pod : non privilégié et non réservé."""
    try:
        value = int(uid)
    except (TypeError, ValueError):
        raise TenancyError("tenant-uid-invalid", "UID %r refused" % (uid,)) from None
    if value < 1000 or value > 60000 or value in (POD_FS_GROUP, POD_SUPERVISOR_UID):
        raise TenancyError(
            "tenant-uid-invalid",
            "UID %d refused: a tenant of a pod needs an unprivileged UID in "
            "1000..60000, distinct from the neutral fsGroup %d and the "
            "supervisor %d" % (value, POD_FS_GROUP, POD_SUPERVISOR_UID))
    return value


def _pod_slug(tenant: str) -> str:
    """Nom DNS-1123 dérivé du tenant (conteneur et PVC)."""
    ident = check_tenant(tenant)[len("human:"):].lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", ident).strip("-")[:40].strip("-")
    return slug or "tenant"


def pod_plan(host: str, tenant: str, *, uid: int | None = None,
             image: str = "ameesh-runner",
             storage_size: str = "20Gi") -> dict:
    """#7694 (design 7633 §3.1, §6.2) : fragment de chart d'un tenant de pod.

    Rend un plan de **données** — conteneur, volume, ConfigMap `passwd`/`group`,
    UID, variables — et n'exécute rien. Le bureau applique le plan dans le
    chart puis enregistre la liaison avec `--register --uid` ; aucun `useradd`,
    `linger` ni geste root n'est imprimé.

    Le GID est **le même pour tous les tenants** (`POD_FS_GROUP`, mode A) : un
    `fsGroup` par tenant est refusé par construction (N de #7659), et le plan
    ne monte jamais le volume d'un autre tenant.
    """
    tenant = check_tenant(tenant)
    host = (host or "").strip()
    if not host:
        raise TenancyError("tenant-host-missing", "a pod host name is required")
    chosen = check_pod_uid(pod_uid(tenant) if uid is None else uid)
    user = unix_user(tenant)
    home = tenant_home(tenant)
    slug = _pod_slug(tenant)
    container = "tenant-" + slug
    claim = "pvc-" + slug
    mount_home = "home-" + slug
    runner = runner_id(host, tenant)
    #: Fichier `passwd` complet du pod : root, superviseur, CE tenant, nobody.
    #: Le chart le projette tel quel ; toute divergence avec `host_tenants.uid`
    #: est refusée par le battement (design 7633 §3.1, §6.3).
    passwd = "\n".join([
        "root:x:0:0:root:/root:/bin/sh",
        "ameesh:x:%d:%d:ameesh pod supervisor:/home/ameesh:/bin/sh"
        % (POD_SUPERVISOR_UID, POD_FS_GROUP),
        "%s:x:%d:%d:tenant %s:%s:/bin/sh"
        % (user, chosen, POD_FS_GROUP, slug, home),
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin",
    ])
    group = "\n".join([
        "root:x:0:",
        "ameesh:x:%d:" % POD_SUPERVISOR_UID,
        "ameesh-pod:x:%d:" % POD_FS_GROUP,
        "nogroup:x:65534:",
    ])
    passwd_map = "ameesh-pod-passwd"
    config_map = "ameesh-pod-tenant-" + slug
    #: le fichier de configuration du tenant est une ConfigMap **non secrète**
    #: (hôte, tenant, runner, comptes sans clé) montée en lecture seule ; le
    #: mot de passe de la base reste un Secret (design 7633 §3, tableau).
    config_path = "/etc/ameesh/tenant/config.json"
    env = [
        {"name": "HOME", "value": home},
        {"name": "USER", "value": user},
        {"name": "LOGNAME", "value": user},
        {"name": "TMPDIR", "value": "/tmp"},
        {"name": "AMEESH_HOST", "value": host},
        {"name": "AMEESH_TENANT", "value": tenant},
        {"name": "AMEESH_RUNNER_ID", "value": runner},
        {"name": "AMEESH_CONFIG", "value": config_path},
        {"name": "AMEESH_CANON", "value": POD_CANON_REPO},
        # Le canon est monté en lecture seule par le superviseur : le runner
        # du tenant ne le synchronise jamais (design 7633 §3.1).
        {"name": "AMEESH_CANON_SYNC_INTERVAL", "value": "0"},
        # B1 de #44265 : le dépôt est cloné par l'init du superviseur (10001)
        # puis lu par ce tenant (UID du plan). Git refuse « dubious ownership »
        # sans `safe.directory` ; la confiance est déclarée par la configuration
        # d'environnement de Git, bornée à CE dépôt (jamais `*`) et sans
        # fichier global. Le chart porte les mêmes variables ; le témoin kind
        # (`deploy/k8s/pod-witness/temoin-canon.yaml`) prouve la lecture sous
        # un UID différent de celui du clone.
        {"name": "GIT_CONFIG_COUNT", "value": "1"},
        {"name": "GIT_CONFIG_KEY_0", "value": "safe.directory"},
        {"name": "GIT_CONFIG_VALUE_0", "value": POD_CANON_REPO},
    ]
    return {
        "host": host,
        "tenant": tenant,
        "unix_user": user,
        "uid": chosen,
        "fs_group": POD_FS_GROUP,
        "home": home,
        "runner_id": runner,
        "passwd": passwd,
        "group": group,
        "container": {
            "name": container,
            "image": image,
            "env": env,
            "volumeMounts": [
                {"name": mount_home, "mountPath": home},
                {"name": "passwd", "mountPath": "/etc/passwd",
                 "subPath": "passwd", "readOnly": True},
                {"name": "group", "mountPath": "/etc/group",
                 "subPath": "group", "readOnly": True},
                {"name": "tenant-config", "mountPath": "/etc/ameesh/tenant",
                 "readOnly": True},
                # Canon alimenté par l'init/`canon-fetch` du superviseur ; le
                # runner du tenant le lit, ne l'écrit jamais.
                {"name": "canon", "mountPath": POD_CANON,
                 "readOnly": True},
                {"name": "tenant-tmp", "mountPath": "/tmp"},
            ],
            "securityContext": {
                "runAsUser": chosen,
                "runAsGroup": POD_FS_GROUP,
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
                "privileged": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "resources": {
                "requests": {"cpu": "250m", "memory": "512Mi"},
                "limits": {"cpu": "2", "memory": "2Gi"},
            },
        },
        "volumes": {
            "home": {
                "name": mount_home,
                "persistentVolumeClaim": {"claimName": claim},
                "accessModes": ["ReadWriteOnce"],
                "size": storage_size,
                "mountPath": home,
            },
            "tmp": {"name": "tenant-tmp", "emptyDir": {"sizeLimit": "1Gi"}},
            "passwd": {
                "name": "passwd",
                "configMap": {"name": passwd_map,
                              "items": [{"key": "passwd", "path": "passwd"}]},
            },
            "group": {
                "name": "group",
                "configMap": {"name": passwd_map,
                              "items": [{"key": "group", "path": "group"}]},
            },
            "tenant_config": {
                "name": "tenant-config",
                "configMap": {"name": config_map,
                              "items": [{"key": "config.json",
                                         "path": "config.json"}]},
            },
            "canon": {"name": "canon", "emptyDir": {}},
        },
        "pod_security_context": {
            "runAsNonRoot": True,
            # Le superviseur et les init tournent sous 10001 ; le conteneur du
            # tenant remplace `runAsUser` par l'UID du plan (ci-dessus).
            "runAsUser": POD_SUPERVISOR_UID,
            "runAsGroup": POD_FS_GROUP,
            "fsGroup": POD_FS_GROUP,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "config_maps": {
            "passwd_group": {"name": passwd_map,
                             "passwd": passwd, "group": group},
            "tenant": {"name": config_map, "key": "config.json",
                       "mountPath": "/etc/ameesh/tenant"},
        },
        # Valeurs de liaison : ce que le bureau passe au `--register` après
        # avoir appliqué le chart (l'UID du chart est la source de vérité).
        "register_argv": ["ameesh", "host", "tenant", "add", tenant,
                          "--host", host, "--pod", "--register",
                          "--uid", str(chosen)],
    }


def pod_plan_text(plan: dict) -> str:
    """Le fragment lisible de `host tenant add --pod` (JSON = YAML valide)."""
    lines = [
        "# #7694: tenant %s on pod host %s — apply this fragment in the chart"
        % (plan["tenant"], plan["host"]),
        "# one neutral fsGroup %d for every pod that mounts this PVC; never a "
        "per-tenant fsGroup (N of #7659)" % plan["fs_group"],
        "# no useradd, no linger, no root gesture: the UID comes from the chart",
        json.dumps({k: v for k, v in plan.items() if k != "register_argv"},
                   ensure_ascii=False, indent=2, sort_keys=True),
        "# then, on the host supervisor: %s"
        % " ".join(shlex.quote(part) for part in plan["register_argv"]),
    ]
    return "\n".join(lines)


def pod_runtime(cfg, host: str):
    """`policy.runtime` de `host` dans le canon configuré, ou None.

    Sert au geste `host tenant add --pod` : le plan pod n'a de sens que pour
    une fiche Host qui déclare `runtime.kind: pod`. Une erreur de lecture ne
    rend jamais un hôte pod par défaut (fail closed)."""
    try:
        for canon in canon_mod.load_configured(cfg):
            fiche = canon.host(host) if hasattr(canon, "host") else None
            runtime = getattr(getattr(fiche, "policy", None), "runtime", None)
            if runtime is not None:
                return runtime
    except Exception as exc:  # noqa: BLE001 - autorité illisible : refus
        raise TenancyError("tenant-runtime-unreadable",
                           "cannot read the runtime of %s: %s" % (host, exc)) from exc
    return None


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


def published_profiles(cfg) -> list[str]:
    """#7108 : profils de compte que CE runner peut offrir, `harnais/compte`.

    Le runner d'un tenant lit sa propre configuration (`accounts`), sous son
    home 0700 : le superviseur ne peut pas la lire. Seuls des noms passent au
    battement, jamais un chemin ni un secret. Une configuration illisible ne
    publie rien (fail closed : le déplacement vers ce tenant est refusé)."""
    from . import accounts as accounts_mod
    try:
        declared = accounts_mod.parse(getattr(cfg, "accounts", None) or {})
    except accounts_mod.AccountError:
        return []
    out = {"%s/%s" % (harness, profile.name)
           for harness, profiles in declared.items() for profile in profiles}
    return sorted(out)


def beat(cfg, db, *, version: str | None = None) -> bool:
    """Battement du runner d'un tenant (`runner_presence`) ; faux sans tenant
    configuré ou sans liaison qui nomme ce runner (le superviseur n'a pas
    encore enregistré le tenant, ou l'identifiant diffère). Le battement
    publie les profils de compte (#7108) avec la présence."""
    tenant = runner_tenant(cfg)
    if not tenant:
        return False
    if version is None:
        from . import version as package_version
        version = package_version()
    return storage.of(db).tenancy.beat(
        cfg.host, tenant, cfg.runner, uid=os.getuid(),
        version=str(version or "") or None, profiles=published_profiles(cfg))


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
    les secrets (clés ou abonnements selon Q2 #224052) sont installés ensuite par
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


def register(db, host: str, tenant: str, *, actor: str, local_host: str,
             uid: int | None = None, user: str | None = None,
             pod: bool = False) -> dict:
    """Liaison tenant → utilisateur système, UID et runner (`host_tenants`).

    Refusée si le canon ne déclare pas l'hôte partagé avec ce tenant
    (`tenant-not-declared`) ou si l'utilisateur système n'existe pas
    (`tenant-user-missing`) : l'UID enregistré est celui que le système de
    l'hôte cible rend, jamais une valeur saisie. `local_host` est l'hôte de
    l'appelant (`cfg.host`) : quand il diffère de `host`, le passwd local
    n'est pas celui de la cible, donc l'UID ne peut pas être lu et la
    liaison est refusée (`tenant-uid-unverified`) sauf `uid` fourni, lu sur
    la cible par l'appelant.

    #7694 : sur un hôte **pod** (`pod=True`), il n'y a pas de `useradd` ni de
    passwd local dans le superviseur ; l'UID vient du **chart** et est exigé
    (`tenant-uid-required`). C'est le battement qui prouve ensuite que le
    processus tourne sous cet UID (design 7633 §3.1)."""
    tenant = check_tenant(tenant)
    policy = host_policy(db, host)
    if not policy.get("shared") or tenant not in policy.get("tenants", []):
        raise TenancyError("tenant-not-declared",
                           "host %s: %s is not a tenant declared by the canon "
                           "(policy.shared/tenants; run \"ameesh canon sync\" first)"
                           % (host, tenant))
    user = user or unix_user(tenant)
    if pod:
        if uid is None:
            raise TenancyError("tenant-uid-required",
                               "host %s is a pod: the tenant UID comes from the "
                               "chart and must be passed with `--uid` (see `host "
                               "tenant add --pod`), never read from a local passwd"
                               % host)
        uid = check_pod_uid(uid)
    elif uid is None:
        if host != local_host:
            raise TenancyError("tenant-uid-unverified",
                               "host %s is not this host (%s): the tenant UID is "
                               "read from the target host's passwd, never from the "
                               "caller's; run the registration on %s"
                               % (host, local_host, host))
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
    lines = ["# #7106: tenant %s on shared host %s (run as root by the avatar)"
             % (check_tenant(tenant), host)]
    lines += [" ".join(shlex.quote(part) for part in step) for step in add_plan(host, tenant)]
    lines.append("# unit %s, installed as %s:"
                 % (TENANT_UNIT % tenant[len("human:"):],
                    "/home/%s/.config/systemd/user/ameesh-runner-tenant@.service" % user))
    lines += ["#   " + line for line in unit_text(host, tenant).splitlines()]
    lines.append("# %s: the tenant's configuration (database, no secret in this plan),"
                 " installed by the avatar like the organization keys"
                 % ("/home/%s/.config/ameesh/config.json" % user))
    lines.append("# then, on the target host (as the host supervisor): ameesh host tenant"
                 " add %s --host %s --register --actor <human:<id>|avatar>"
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


def operator(cfg, db, explicit: str | None = None) -> str:
    """Qui écrit ou retire une liaison de tenant : un humain ou son avatar.

    La conception réserve ce geste d'infrastructure à l'avatar de l'humain,
    « jamais un agent » (5322 §3.4) ; `work.corrector` est plus large
    (orchestrateurs et agents de conception) et ne convient donc pas ici. Une
    session liée agit en son nom seul ; un terminal humain dit qui il est
    (`--actor`)."""
    from . import agent_names, avatars, canon, identity, work

    text = (explicit or "").strip()
    text = agent_names.resolve(text, db=db, operation="tenancy.operator")
    binding = identity.resolve_binding(cfg, db)
    # A rejected runner/session identity must never fall back to the human
    # terminal path, including a malformed AGENT_MAIL_NAME (no returned name).
    if not binding.ok and (binding.name or binding.source != "none"
                           or os.environ.get("AGENT_MAIL_NAME")):
        raise TenancyError("tenant-identity-invalid",
                           "invalid session identity (%s)"
                           % (binding.reason or "unbound identity"))
    if binding.name:
        if text and text != binding.name:
            raise TenancyError("tenant-actor-mismatch",
                               "this session belongs to %s, it does not act on "
                               "behalf of %s" % (binding.name, text))
        name = binding.name
    else:
        if not text:
            raise TenancyError("tenant-actor-missing",
                               "say who does it (--actor human:<id> or --actor avatar)")
        name = text
    if name.startswith(work.HUMAN_PREFIX):
        return name
    human = work.known_human(db, name, cfg)
    if human:
        return human
    try:
        # La fiche Agent du canon, pas la liste d'alerte
        # (`AMEESH_ALERT_ORCHESTRATORS` nomme des orchestrateurs, jamais un
        # avatar) : un agent ne signe pas une liaison de tenant.
        canons = canon.load_configured(cfg) if cfg is not None else []
        if avatars.is_avatar_in(canons, name):
            return name
    except Exception as exc:  # noqa: BLE001 - autorité illisible : refus
        raise TenancyError("tenant-authority-unreadable",
                           "cannot read the avatar of %s: %s" % (name, exc)) from exc
    raise TenancyError("tenant-avatar-only",
                       "%s is neither a human nor an avatar: a tenant binding is "
                       "an infrastructure gesture, \"l'avatar, jamais un agent\" "
                       "(design 5322 §3.4)" % name)


# --------------------------------------------------------------------------
# `ameesh host tenant add|list|remove`
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    """`ameesh host tenant …` : geste d'infrastructure (avatar ou humain,
    jamais un agent ni le runner d'un tenant).

    `add` sans `--register` imprime le plan système (utilisateur, home 0700,
    linger, dossier de comptes, unité) et n'exécute rien ; `--register`
    écrit la liaison en base, une fois l'utilisateur créé, et se lance sur
    l'hôte cible (l'UID est lu dans son passwd, pas celui de l'appelant).
    #7694 : `add --pod` imprime le fragment de chart d'un tenant de pod
    (conteneur, PVC, ConfigMap passwd/group, UID, `fsGroup` neutre), sans
    aucun geste root ; `--register --pod --uid` écrit la liaison avec l'UID
    du chart. `remove` est refusé tant qu'un agent du tenant reste sur
    l'hôte."""
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
    add.add_argument("--pod", action="store_true",
                     help="hôte pod : plan de chart/ConfigMap, aucun geste root")
    add.add_argument("--uid", type=int,
                     help="UID du tenant sur un hôte pod (source : le chart)")
    add.add_argument("--actor",
                     help="who registers (human:<id> or avatar) from an unbound terminal")
    listing = commands.add_parser("list", help="tenants and runner presence")
    listing.add_argument("--host")
    listing.add_argument("--json", action="store_true")
    drop = commands.add_parser("remove", help="remove a tenant binding (no agent left)")
    drop.add_argument("tenant")
    drop.add_argument("--host")
    drop.add_argument("--actor",
                      help="who removes (human:<id> or avatar) from an unbound terminal")
    args = parser.parse_args(argv)
    cfg = config.load()
    host = args.host or cfg.host
    if args.command == "add":
        if args.uid is not None and not args.pod:
            print("tenant refused (tenant-pod-flag-required): --uid is the UID "
                  "of a pod chart; pass --pod")
            return 1
        if args.pod:
            try:
                runtime = pod_runtime(cfg, host)
            except TenancyError as exc:
                print("tenant refused (%s): %s" % (exc.code, exc))
                return 1
            if runtime is None or getattr(runtime, "kind", "") != "pod":
                print("tenant refused (tenant-not-pod): host %s does not declare "
                      "`policy.runtime.kind: pod` in the canon (design 7633 §7.1)"
                      % host)
                return 1
            if not args.register:
                try:
                    print(pod_plan_text(pod_plan(host, args.tenant, uid=args.uid)))
                except TenancyError as exc:
                    print("tenant refused (%s): %s" % (exc.code, exc))
                    return 1
                return 0
        elif not args.register:
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
        actor = operator(cfg, db, args.actor)
        if args.command == "add":
            if args.pod:
                # Le plan a déjà vérifié la fiche Host ; l'UID vient du chart
                # et le battement prouvera ensuite l'UID du processus.
                pod_runtime(cfg, host)
            result = register(db, host, args.tenant, actor=actor,
                              local_host=cfg.host, pod=bool(args.pod),
                              uid=args.uid)
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
