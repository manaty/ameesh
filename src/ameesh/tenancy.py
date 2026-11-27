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
import time
import uuid
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
#: #7697 : plage d'UID des enfants portés par un pod (jamais l'UID du parent) ;
#: un enfant a un UID neuf, stable pour la vie de son pod.
POD_CHILD_UID_BASE = 30000
POD_CHILD_UID_SPAN = 20000
#: préfixe du nom du Secret d'une clé d'API d'organisation par enfant (D6).
POD_CHILD_SECRET_PREFIX = "ameesh-child-"
#: nom du template de pod enfant enregistré par le pourvoyeur.
POD_CHILD_TEMPLATE = "ameesh-child"
#: #7911 (design 7633 §7.3) : une fenêtre de connexion du siège dure au plus
#: 30 min — la même borne est dans `tenant_logins` (0610), et le pod de
#: connexion porte `activeDeadlineSeconds` sous cette borne.
LOGIN_TTL_MAX_S = 1800.0
LOGIN_TTL_DEFAULT_S = 1800.0
#: #7911 : préfixe du pod de connexion dédié (le pod partagé n'est jamais une
#: cible `pods/exec`). Le nom est rendu par `host tenant login` AVANT la
#: création : le runner sait quel battement observer.
LOGIN_POD_PREFIX = "ameesh-login-"
LOGIN_POD_RE = re.compile(r"^ameesh-login-[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
#: #7911 : répertoire du battement, à la RACINE du PVC du tenant (le pod de
#: connexion et le runner le montent au même endroit logique). Le pod y écrit
#: un battement par seconde ; le runner n'enregistre la fin qu'après silence.
LOGIN_BEAT_DIR = ".ameesh/seat-login"
#: #7911 : silence du battement au-delà duquel le pod est tenu pour terminé.
#: Plus long que `terminationGracePeriodSeconds` (5 s) : les processus d'un
#: conteneur qui s'arrête ont le temps de disparaître avant la reprise.
LOGIN_BEAT_STALE_S = 30.0
#: Une preuve D2 citable : décision/mail/lot (`#233251`), référence
#: `owner/repo#123`, ou `card@commit`. Une phrase libre va dans la note du
#: lot, jamais dans la colonne : la preuve est une référence, pas un récit.
LOGIN_TERMS_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._/-]*[:#@][A-Za-z0-9._/#@-]+|#?[0-9]+)\Z")


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


# --------------------------------------------------------------------------
# #7697 : identité d'un enfant porté par un pod (design 7633 §8.3–§8.4)
# --------------------------------------------------------------------------
def _dns_slug(value: str, limit: int = 40) -> str:
    """Nom DNS-1123 dérivé d'un identifiant libre (agent, enfant)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return (slug[:limit].strip("-") or "x")


def child_uid(tenant: str, child: str) -> int:
    """UID d'un enfant porté par un pod : plage 30000+, stable pour ce couple.

    L'UID ne dépend jamais du parent : un enfant ne peut pas hériter de son
    UID, et deux enfants du même parent ont des UID distincts. Une collision
    entre deux enfants est possible (20 000 valeurs) ; le template porte
    ensuite l'UID comme source de vérité et `host_tenants` le vérifie.
    """
    tenant = check_tenant(tenant)
    digest = hashlib.sha256(
        ("ameesh-pod-child-uid:" + tenant + ":" + str(child)).encode("utf-8")).digest()
    return POD_CHILD_UID_BASE + int.from_bytes(digest[:8], "big") % POD_CHILD_UID_SPAN


def check_child_uid(uid: int) -> int:
    """UID acceptable pour un enfant pod : 30000..60000, jamais un UID réservé."""
    try:
        value = int(uid)
    except (TypeError, ValueError):
        raise TenancyError("child-uid-invalid", "UID %r refused" % (uid,)) from None
    if not (POD_CHILD_UID_BASE <= value <= 60000) or value in (
            POD_FS_GROUP, POD_SUPERVISOR_UID):
        raise TenancyError(
            "child-uid-invalid",
            "UID %d refused: a pod child needs an unprivileged UID in "
            "%d..60000, never the parent's UID" % (value, POD_CHILD_UID_BASE))
    return value


def child_unix_user(tenant: str, child: str) -> str:
    """Entrée `passwd` propre à l'enfant (jamais le nom du parent)."""
    tenant = check_tenant(tenant)
    digest = hashlib.sha256(
        ("ameesh-pod-child-user:" + tenant + ":" + str(child)).encode("utf-8"))
    return "child-" + digest.hexdigest()[:10]


def child_runner_id(host: str, child: str) -> str:
    """Identifiant d'exécuteur de l'enfant sur son hôte (stable, sans secret)."""
    return "%s/%s" % (host, _dns_slug(child, 40))


def child_slug(child: str) -> str:
    """Forme DNS de l'enfant, celle du label `ameesh.dev/child` et du Secret.

    Le nom du Secret est **dérivé du label**, sans annotation libre : la VAP
    vérifie en CEL que le Secret monté est exactement
    `ameesh-child-<label>`. Le suffixe hexadécimal distingue deux noms bruts
    qui tombent sur la même forme DNS (`Enfant.1` et `enfant-1`), sans quoi
    deux enfants partageraient le même Secret (la contrainte `unique` sur
    `pod_requests.credential_secret` refuserait le second)."""
    text = str(child or "")
    digest = hashlib.sha256(
        ("ameesh-pod-child-slug:" + text).encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (_dns_slug(text, 20), digest)


def child_credential_secret(child: str) -> str:
    """Nom du Secret d'API d'organisation par enfant (le secret n'est pas ici).

    Le pourvoyeur ne lit jamais ce Secret : il le monte dans le conteneur de
    l'enfant, qui est le seul à pouvoir le lire. Le nom est déterministe pour
    que l'opérateur puisse le créer avant le premier `spawn`."""
    return POD_CHILD_SECRET_PREFIX + child_slug(child)


def child_host(parent: str, child: str, token: str) -> str:
    """Nom logique stable de l'hôte enfant, jamais le nom du pod.

    `pod-<parent>-<enfant>-<8 hex>` : le jeton distingue deux incarnations du
    même enfant et tient dans les 63 caractères d'un label DNS."""
    token = re.sub(r"[^a-z0-9]", "", str(token or "").lower())
    if len(token) < 4:
        raise TenancyError("child-host-invalid", "a host token of 4+ chars is required")
    host = "pod-%s-%s-%s" % (_dns_slug(parent, 18), _dns_slug(child, 18), token[:12])
    host = host[:63].rstrip("-")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", host):
        raise TenancyError("child-host-invalid", "invalid child host %r" % (host,))
    return host


def child_plan(host: str, child: str, tenant: str, *, parent: str,
               harness: str, uid: int | None = None, provider: str | None = None,
               model: str | None = None, image: str = "ameesh-runner") -> dict:
    """Plan de données de l'enfant : UID, passwd/group, ConfigMap, `emptyDir`.

    Aucun PVC ni volume du parent : le home et le dossier de travail sont des
    `emptyDir` propres à ce pod (design 7633 §8.4). Le plan ne crée rien ;
    le template du pourvoyeur le rend et `host_tenants` enregistre l'UID que
    le battement vérifiera. L'enfant est en `api-key` : pas de copie du
    dossier d'abonnement du parent, un Secret d'organisation par enfant."""
    from . import accounts as accounts_mod

    tenant = check_tenant(tenant)
    host = (host or "").strip()
    if not host:
        raise TenancyError("child-host-invalid", "a child pod host is required")
    chosen = check_child_uid(child_uid(tenant, child) if uid is None else uid)
    user = child_unix_user(tenant, child)
    home = "/home/" + user
    cwd = home + "/work"
    runner = child_runner_id(host, child)
    key_env = accounts_mod.default_key_env_of(harness)
    if not key_env:
        raise TenancyError(
            "child-harness-key-env",
            "harness %r has no api-key environment: a pod child cannot use a "
            "subscription credential (design 7633 §8.4)" % (harness,))
    secret = child_credential_secret(child)
    passwd = "\n".join([
        "root:x:0:0:root:/root:/bin/sh",
        "ameesh:x:%d:%d:ameesh pod supervisor:/home/ameesh:/bin/sh"
        % (POD_SUPERVISOR_UID, POD_FS_GROUP),
        "%s:x:%d:%d:child %s:%s:/bin/sh"
        % (user, chosen, POD_FS_GROUP, _dns_slug(child, 20), home),
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin",
    ])
    group = "\n".join([
        "root:x:0:",
        "ameesh:x:%d:" % POD_SUPERVISOR_UID,
        "ameesh-pod:x:%d:" % POD_FS_GROUP,
        "nogroup:x:65534:",
    ])
    config = {
        "host": host,
        "tenant": tenant,
        "runner_id": runner,
        "accounts": {
            harness: [{"name": "enfant", "type": "api_key_env",
                       "key_env": key_env}],
        },
    }
    env = [
        {"name": "HOME", "value": home},
        {"name": "USER", "value": user},
        {"name": "LOGNAME", "value": user},
        {"name": "TMPDIR", "value": "/tmp"},
        {"name": "AMEESH_HOST", "value": host},
        {"name": "AMEESH_TENANT", "value": tenant},
        {"name": "AMEESH_RUNNER_ID", "value": runner},
        {"name": "AMEESH_CONFIG", "value": "/etc/ameesh/child/config.json"},
        {"name": "AMEESH_CANON_SYNC_INTERVAL", "value": "0"},
        {"name": "AMEESH_POD", "value": "1"},
        {"name": "AMEESH_POD_UID", "valueFrom": {
            "fieldRef": {"fieldPath": "metadata.uid"}}},
        {"name": key_env, "valueFrom": {
            "secretKeyRef": {"name": secret, "key": "api-key"}}},
    ]
    volumes = {
        "home": {"name": "child-home", "emptyDir": {"sizeLimit": "2Gi"}},
        "tmp": {"name": "child-tmp", "emptyDir": {"sizeLimit": "512Mi"}},
        "config": {"name": "child-config",
                   "configMap": {"name": "child-config-" + _dns_slug(child, 30)}},
    }
    return {
        "host": host,
        "child": child,
        "parent": parent,
        "tenant": tenant,
        "harness": harness,
        "provider": provider,
        "model": model,
        "image": image,
        "uid": chosen,
        "fs_group": POD_FS_GROUP,
        "unix_user": user,
        "home": home,
        "cwd": cwd,
        "runner_id": runner,
        "credential_secret": secret,
        "credential_env": key_env,
        "passwd": passwd,
        "group": group,
        "config": config,
        "env": env,
        "volumes": volumes,
        "container": {
            "name": "child",
            "image": image,
            "env": env,
            "volumeMounts": [
                {"name": "child-home", "mountPath": home},
                {"name": "child-config", "mountPath": "/etc/ameesh/child",
                 "readOnly": True},
                {"name": "child-tmp", "mountPath": "/tmp"},
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
        },
    }


def pod_host_problem(db, host: str, pod_uid: str | None, *, now: float | None = None) -> str:
    """Pourquoi `host` n'est pas un hôte enfant vivant et connu (`""` sinon).

    Fail closed : une ligne absente pour un hôte qui n'est pas un pod n'est
    pas un problème (les hôtes ordinaires n'ont pas de ligne), mais un hôte
    qui en a une doit être `running`, non expiré et porter le `pod_uid`
    observé. Le code stable est `pod-host-unknown`."""
    row = storage.of(db).pod_hosts.get(host)
    if row is None:
        return ""
    now = time.time() if now is None else float(now)
    if row.get("state") != "running":
        return ("pod-host-unknown: pod host %s is %s, not running"
                % (host, row.get("state") or "unknown"))
    if float(row.get("expires_ts") or 0) <= now:
        return ("pod-host-unknown: pod host %s expired at %s"
                % (host, row.get("expires_ts")))
    if not pod_uid or str(row.get("pod_uid") or "") != str(pod_uid):
        return ("pod-host-unknown: pod UID observed by the runner (%s) is not "
                "the provisioned pod UID (%s)" % (pod_uid or "none", row.get("pod_uid")))
    return ""


def pod_runtime(cfg, host: str):
    """`policy.runtime` de `host` dans le canon configuré, ou None.

    Sert au geste `host tenant add --pod` : le plan pod n'a de sens que pour
    une fiche Host qui déclare `runtime.kind: pod`. Une erreur de lecture ne
    rend jamais un hôte pod par défaut (fail closed)."""
    try:
        for canon in canon_mod.load_configured(cfg):
            # Même garde que runner.refresh_host_limits : un faux canon peut
            # porter `host=None`, attribut présent mais non appelable.
            lookup = getattr(canon, "host", None)
            fiche = lookup(host) if callable(lookup) else None
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


def pod_profile_problem(cfg, profile, *, home: str | None = None) -> str:
    """#7695 (design 7633 §4.2) : sur un hôte pod, un dossier de compte ou un
    fichier de clé hors du home du tenant est refusé (`""` si conforme).

    Le PVC du tenant est la seule frontière de son état : un `config_dir` ou
    un `key_file` absolu hors de son home monterait (ou lirait) un volume
    d'un autre, ou la racine de l'image en lecture seule. Les comptes sans
    dossier (`~/.claude`, `~/.codex`, `~/.dsh`) restent sous le home, donc
    conformes. Hors hôte pod, rien ne change : `""`."""
    if not getattr(cfg, "pod", False):
        return ""
    if home is None:
        home = os.environ.get("HOME") or os.path.expanduser("~")
    root = os.path.realpath(os.path.expanduser(home))
    declared = getattr(profile, "path", "") or ""
    if declared and getattr(profile, "kind", "") == "config_dir":
        target = os.path.realpath(os.path.expanduser(declared))
        if target != root and not target.startswith(root + os.sep):
            return ("account %s/%s: config_dir %s is outside the tenant home %s "
                    "(a pod tenant only mounts its own volume)"
                    % (getattr(profile, "harness", "?"), getattr(profile, "name", "?"),
                       declared, home))
    key_file = getattr(profile, "key_file", "") or ""
    if key_file:
        target = os.path.realpath(os.path.expanduser(key_file))
        if target != root and not target.startswith(root + os.sep):
            return ("account %s/%s: key_file %s is outside the tenant home %s "
                    "(a pod tenant only mounts its own volume)"
                    % (getattr(profile, "harness", "?"), getattr(profile, "name", "?"),
                       key_file, home))
    return ""


def published_profiles(cfg) -> list[str]:
    """#7108 : profils de compte que CE runner peut offrir, `harnais/compte`.

    Le runner d'un tenant lit sa propre configuration (`accounts`), sous son
    home 0700 : le superviseur ne peut pas la lire. Seuls des noms passent au
    battement, jamais un chemin ni un secret. Une configuration illisible ne
    publie rien (fail closed : le déplacement vers ce tenant est refusé).

    #7695 : sur un hôte pod, un profil `config_dir` (ou un `key_file`) hors du
    home du tenant n'est pas publié non plus : son volume est la seule
    frontière de son état (design 7633 §4.2, §5.2)."""
    from . import accounts as accounts_mod
    try:
        declared = accounts_mod.parse(getattr(cfg, "accounts", None) or {})
    except accounts_mod.AccountError:
        return []
    out = {"%s/%s" % (harness, profile.name)
           for harness, profiles in declared.items() for profile in profiles
           if not pod_profile_problem(cfg, profile)}
    return sorted(out)


def beat(cfg, db, *, version: str | None = None,
         login_ack: bool = False) -> bool:
    """Battement du runner d'un tenant (`runner_presence`) ; faux sans tenant
    configuré ou sans liaison qui nomme ce runner (le superviseur n'a pas
    encore enregistré le tenant, ou l'identifiant diffère). Le battement
    publie les profils de compte (#7108) avec la présence.

    #7697 : sur un hôte enfant, le battement publie aussi le UID Kubernetes
    observé et refuse (`pod-host-unknown`, fail closed) si la ligne
    `pod_hosts` manque, est expirée ou ne porte pas ce UID. Un hôte ordinaire
    n'a pas de ligne et garde le comportement de #7108.
    #7911 : `login_ack` n'est posé par l'exécuteur qu'après avoir arrêté ses
    tours et rendu ses baux ; sans lui, un battement pendant la fenêtre laisse
    `state=running` et le bureau n'ouvre pas le pod de connexion."""
    tenant = runner_tenant(cfg)
    if not tenant:
        return False
    pod_uid = str(getattr(cfg, "pod_uid", "") or "").strip() or None
    if storage.of(db).pod_hosts.get(cfg.host) is not None:
        problem = pod_host_problem(db, cfg.host, pod_uid)
        if problem:
            return False
    if version is None:
        from . import version as package_version
        version = package_version()
    return storage.of(db).tenancy.beat(
        cfg.host, tenant, cfg.runner, uid=os.getuid(),
        version=str(version or "") or None, profiles=published_profiles(cfg),
        pod_uid=pod_uid, login_ack=login_ack)


def login_active(db, host: str, tenant: str) -> bool:
    """#7911 : la garde de connexion du siège est active pour ce tenant (la
    fonction SQL `ameesh_tenant_login_active`, 0610) : fenêtre ouverte tant
    que la fin du pod de connexion désigné n'a pas été observée. Une fenêtre
    sans pod reste bornée par son échéance ; une fenêtre avec pod ne se
    rouvre pas à l'expiration seule (B/P2 #46731)."""
    return storage.of(db).tenancy.login_active(host, check_tenant(tenant))


def login_window(db, host: str, tenant: str) -> dict | None:
    """#7911 : la fenêtre `login` ouverte du tenant, ou None. Le runner lit
    `pod_name` et `pod_ended_ts` pour observer la fin du pod sur le volume
    partagé avant de reprendre ses tours."""
    return storage.of(db).tenancy.login_window(host, check_tenant(tenant))


def login_pod_ended(db, host: str, tenant: str, *, pod_name: str,
                    by: str) -> bool:
    """#7911 : enregistre la fin observée du pod de connexion (battement
    silencieux du volume, ou suppression constatée). Idempotent."""
    return storage.of(db).tenancy.login_pod_ended(
        host, check_tenant(tenant), pod_name=pod_name, by=by)


def login_pod_name(tenant: str) -> str:
    """#7911 : nom unique du pod de connexion d'une fenêtre.

    Rendu par `host tenant login` : le bureau le passe à `render.py --pod` et
    le runner observe le battement de CE pod. Le suffixe aléatoire interdit
    qu'une fenêtre précédente (ou une autre personne) partage le même
    battement sur le volume."""
    ident = re.sub(r"[^a-z0-9-]", "-",
                   check_tenant(tenant)[len("human:"):].lower()).strip("-")
    ident = (ident or "tenant")[:24].strip("-") or "tenant"
    return LOGIN_POD_PREFIX + ident + "-" + uuid.uuid4().hex[:8]


def login_pod_beat_stale(cfg, pod_name: str, *, now: float | None = None) -> bool:
    """#7911 : la fin du pod de connexion est-elle **observée** sur le volume ?

    Le pod de connexion tient un verrou et écrit un battement par seconde dans
    `LOGIN_BEAT_DIR`, à la racine du PVC du tenant (design 7633 §7.3). Le
    runner est le seul autre processus qui monte ce volume : il considère la
    fin acquise quand le verrou est libre **et** que le battement est
    silencieux depuis `LOGIN_BEAT_STALE_S`. Le verrou prouve que le porteur
    n'est plus vivant (le noyau le rend à sa mort, même SIGKILL), le silence
    couvre la grâce de terminaison : aucun processus du pod ne peut plus
    écrire quand la reprise a lieu. Fichier absent, verrou tenu ou lecture
    impossible : pas de fin observée (fail closed), la fenêtre reste fermée et
    `logout` est le seul chemin après suppression constatée."""
    tenant = runner_tenant(cfg)
    name = str(pod_name or "").strip()
    if not tenant or not name:
        return False
    base = os.path.join(tenant_home(tenant), LOGIN_BEAT_DIR)
    beat = os.path.join(base, name + ".beat")
    lock = os.path.join(base, name + ".lock")
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        return False
    try:
        import fcntl
        try:
            # Verrou exclusif non bloquant : il n'est libre que si le porteur
            # du pod est mort (le noyau le libère à la disparition du process).
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    try:
        beat_mtime = os.stat(beat).st_mtime
    except OSError:
        return False
    moment = time.time() if now is None else float(now)
    return (moment - beat_mtime) >= LOGIN_BEAT_STALE_S


def login_open(db, host: str, tenant: str, *, terms_ref: str, actor: str,
               ttl_s: float = LOGIN_TTL_DEFAULT_S,
               pod_name: str | None = None) -> dict:
    """Ouvre la fenêtre `login` d'un tenant pod (geste humain/avatar).

    `terms_ref` est la preuve citable de la vérification D2 (conditions du
    fournisseur avec la personne, décision owner #233251) : sans elle, aucune
    connexion de siège. Le nom du pod de connexion est rendu ici, AVANT sa
    création : le runner observe son battement sur le volume pour ne reprendre
    qu'après sa fin observée (B/P2 #46731). Le runner du tenant rend ses baux
    et son battement publie `login` ; le bureau attend cet acquittement
    (`host tenant list`) avant de créer le pod. Refuse un tenant non lié, un
    nom de pod invalide et une fenêtre encore ouverte ; la durée est bornée à
    30 min (design §7.3)."""
    tenant = check_tenant(tenant)
    ref = str(terms_ref or "").strip()
    if not ref or len(ref) > 300 or not LOGIN_TERMS_RE.match(ref):
        raise TenancyError(
            "tenant-login-terms-invalid",
            "a citable proof of the supplier-terms verification is required "
            "(D2, decision #233251): e.g. `#233251`, `owner/repo#123` or "
            "`mail:234666`; the narrative goes in the lot note")
    try:
        ttl = float(ttl_s)
    except (TypeError, ValueError):
        ttl = 0.0
    if not (30.0 <= ttl <= LOGIN_TTL_MAX_S):
        raise TenancyError(
            "tenant-login-ttl-invalid",
            "a seat-login window lasts between 30 s and %d s (design 7633 §7.3)"
            % int(LOGIN_TTL_MAX_S))
    pod = str(pod_name or "").strip() or login_pod_name(tenant)
    if len(pod) > 63 or not LOGIN_POD_RE.match(pod):
        raise TenancyError(
            "tenant-login-pod-invalid",
            "the connection pod must be a DNS-1123 name starting with `%s`, "
            "got %r" % (LOGIN_POD_PREFIX, pod))
    bound = {row.get("tenant") for row in
             storage.of(db).tenancy.bindings(host)}
    if tenant not in bound:
        raise TenancyError(
            "tenant-not-bound",
            "host %s: %s has no binding (run `host tenant add --pod "
            "--register` first)" % (host, tenant))
    result = storage.of(db).tenancy.login_open(
        host, tenant, terms_ref=ref, actor=actor, ttl_s=ttl, pod_name=pod)
    if not result.get("opened"):
        current = result.get("current") or {}
        raise TenancyError(
            "tenant-login-already-open",
            "host %s: %s already has a seat-login window (until %s, by %s, "
            "pod %s); delete that pod, observe its deletion, then run "
            "`host tenant logout`"
            % (host, tenant, _iso(current.get("expires_ts")),
               current.get("opened_by") or "?",
               current.get("pod_name") or "?"))
    return result


def login_close(db, host: str, tenant: str, *, actor: str) -> dict:
    """Ferme la fenêtre `login` du tenant (idempotent : `closed: False` s'il
    n'y en a pas). Le pod de connexion est supprimé avant, par le bureau."""
    return storage.of(db).tenancy.login_close(
        host, check_tenant(tenant), actor=actor)


def _iso(ts) -> str:
    """Horodatage UTC lisible (secondes) pour les messages et `host list`."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "?"


def presence_reason(db, role: Mapping, host: str, *, now: float,
                    limit_s: float) -> str:
    """§3.2 point 5 : sur un hôte partagé, un runner du tenant de l'agent est
    vivant (battement frais au seuil de l'hôte). "" si présent ou si l'hôte
    n'est pas partagé ; sinon la raison.

    #7698 : le battement publie l'UID observé (`runner_presence.uid`) et, sur
    un hôte enfant, le UID Kubernetes du pod. Une ligne de présence dont l'UID
    ne correspond plus à `host_tenants.uid` (re-liaison, chart changé) ne rend
    pas l'hôte présent, et un pod recréé (UID neuf) non plus : c'est le même
    fail closed que la garde de réclamation `ameesh_tenant_claim_ok`."""
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
    observed, bound = row.get("uid"), row.get("binding_uid")
    if observed is None or bound is None or int(observed) != int(bound):
        return ("runner of tenant %s on shared host %s beats under UID %s, "
                "binding UID %s: tenant UID not verified"
                % (tenant, host, observed if observed is not None else "none",
                   bound if bound is not None else "none"))
    child = storage.of(db).pod_hosts.get(host)
    if child is not None:
        problem = pod_host_problem(db, host, row.get("pod_uid"), now=now)
        if problem:
            return problem
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
    """Lignes de `ameesh host tenant list`.

    #7695 : les profils de compte publiés par le battement (`harnais/compte`,
    jamais un chemin ni un secret) sont affichés, pour voir d'un coup d'œil
    quels harnais un tenant peut offrir au déplacement.
    #7911 : la fenêtre de connexion du siège est montrée (preuve D2 citée,
    auteur, échéance) ; `runner=login` est l'acquittement du runner — le
    bureau attend cet état avant de créer le pod de connexion."""
    out = []
    for row in rows:
        seen = row.get("seen_ts")
        age = ("never" if seen is None
               else "%ds ago" % int(max(0.0, float(row.get("now_ts") or 0) - float(seen))))
        profiles = "  profiles=%s" % ",".join(row.get("profiles") or ()) \
            if row.get("profiles") else ""
        login = ""
        if row.get("login_until_ts") is not None:
            login = "  login until %s terms=%s by=%s pod=%s" % (
                _iso(row.get("login_until_ts")), row.get("login_terms") or "?",
                row.get("login_opened_by") or "?",
                row.get("login_pod") or "?")
            if row.get("login_pod_ended_ts") is not None:
                login += " ended=%s" % _iso(row.get("login_pod_ended_ts"))
        state = "  state=%s" % row["state"] if row.get("state") else ""
        out.append("%s  %s  user=%s uid=%s runner=%s  seen %s%s%s%s%s" % (
            row["host"], row["tenant"], row["unix_user"], row["uid"], row["runner_id"],
            age, "  (%s)" % row["version"] if row.get("version") else "", profiles,
            state, login))
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
    # #7911 : connexion du siège. Le geste est humain/avatar, jamais un agent
    # ni le runner du tenant ; `--terms` cite la preuve D2 (conditions du
    # fournisseur vérifiées avec la personne, décision owner #233251).
    login = commands.add_parser(
        "login", help="open a seat-connection window for a pod tenant (D2 proof)")
    login.add_argument("tenant", help="human:<id>")
    login.add_argument("--host", help="pod host (default: this host)")
    login.add_argument("--terms", required=True,
                       help="citable proof of the supplier-terms verification "
                            "(D2): e.g. `#233251`, `owner/repo#123`, `mail:234666`")
    login.add_argument("--ttl", type=int, default=int(LOGIN_TTL_DEFAULT_S),
                       help="window in seconds (30..%d, default %d); the "
                            "returned pod_name and expires_ts drive render.py"
                            % (int(LOGIN_TTL_MAX_S), int(LOGIN_TTL_DEFAULT_S)))
    login.add_argument("--actor",
                       help="who opens (human:<id> or avatar) from an unbound terminal")
    logout = commands.add_parser(
        "logout", help="close the seat-connection window of a tenant")
    logout.add_argument("tenant", help="human:<id>")
    logout.add_argument("--host", help="pod host (default: this host)")
    logout.add_argument("--actor",
                        help="who closes (human:<id> or avatar) from an unbound terminal")
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
        if args.command in ("login", "logout"):
            if args.command == "login":
                runtime = pod_runtime(cfg, host)
                if runtime is None or getattr(runtime, "kind", "") != "pod":
                    raise TenancyError(
                        "tenant-not-pod",
                        "host %s does not declare `policy.runtime.kind: pod` "
                        "in the canon (design 7633 §7.1); a seat connection "
                        "runs in a dedicated pod" % host)
                result = login_open(db, host, args.tenant, terms_ref=args.terms,
                                    actor=actor, ttl_s=args.ttl)
            else:
                result = login_close(db, host, args.tenant, actor=actor)
            print(json.dumps(result, ensure_ascii=False))
            return 0
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
