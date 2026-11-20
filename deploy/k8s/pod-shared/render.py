#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7695 (P2 de #7633) : rend le chart d'un hôte pod partagé à N tenants.

Le fichier `host.exemple.yaml` (ou celui passé en `--config`) est la source :
il nomme l'hôte logique, l'image, le canon et la liste des tenants. Le rendu
produit les objets Kubernetes d'un pod partagé :

* UN superviseur (uid 10001, sans volume de tenant) ;
* N conteneurs de tenant, chacun sous l'UID de `tenancy.pod_plan()` et
  montant UNIQUEMENT son PVC ;
* N PVC `ReadWriteOnce`, un `fsGroup` neutre unique pour le pod (jamais un
  `fsGroup` par tenant, N de #7659) ;
* la ConfigMap `passwd`/`group` dérivée des mêmes plans, une ConfigMap de
  configuration par tenant (noms de comptes seulement) et celle du
  superviseur, la ConfigMap du canon ;
* les init/sidecar du canon, les `securityContext` du design §6.

Le rendu est un document d'opérateur : `render.py --check` compare les
fichiers de `rendered/` au rendu courant, `--write` les écrit, `--stdout`
les imprime. Aucun objet n'est appliqué par ce script et il ne lit aucun
secret. Le plan d'un tenant (`ameesh host tenant add … --pod`) reste la
source de l'UID ; le chart recopie ses valeurs et le battement les vérifie.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - l'opérateur installe PyYAML
    sys.exit("render.py: PyYAML est requis (pip install pyyaml)")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ameesh import tenancy  # noqa: E402  (le chemin du dépôt d'abord)

#: Fichiers du rendu, dans l'ordre de kustomization.yaml.
RENDERED = ("configmaps.yaml", "pvcs.yaml", "statefulset.yaml")
HEADER = (
    "# SPDX-License-Identifier: AGPL-3.0-only\n"
    "# GÉNÉRÉ par deploy/k8s/pod-shared/render.py — ne pas editer a la main.\n"
    "# Source : %s ; `render.py --check` verifie la fraicheur.\n"
)
SA_NAME = "ameesh-pod"
CANON_PATH = tenancy.POD_CANON
CANON_REPO = tenancy.POD_CANON_REPO


class RenderError(ValueError):
    """Configuration ou plan incohérent ; le rendu est refusé."""


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    return slug or "tenant"


def _memory_bytes(value) -> int:
    """« 2Gi », « 512Mi », « 1000M », un entier : des octets."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPE]i?|)", text, re.IGNORECASE)
    if not match:
        raise RenderError("mémoire illisible : %r" % (value,))
    number = float(match.group(1))
    suffix = match.group(2)
    factors = {
        "": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3,
        "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6,
        "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3,
        "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    }
    return int(number * factors[suffix])


def _cpu_cores(value) -> float:
    """« 2 », « 250m », « 1.5 » : des cœurs (au moins 0.001)."""
    if isinstance(value, (int, float)):
        return max(0.001, float(value))
    text = str(value or "").strip()
    try:
        if text.endswith("m"):
            return max(0.001, float(text[:-1]) / 1000.0)
        return max(0.001, float(text))
    except ValueError:
        raise RenderError("CPU illisible : %r" % (value,))


def load_config(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except OSError as exc:
        raise RenderError("configuration illisible : %s" % exc) from exc
    if not isinstance(config, dict):
        raise RenderError("la configuration doit être un mapping YAML")
    for key in ("host", "tenants"):
        if not config.get(key):
            raise RenderError("`%s` est requis" % key)
    tenants = config["tenants"]
    if not isinstance(tenants, list) or not tenants:
        raise RenderError("`tenants` doit être une liste non vide")
    seen_slugs, seen_uids = set(), set()
    normalised = []
    for item in tenants:
        entry = {"tenant": item} if isinstance(item, str) else dict(item or {})
        tenant = tenancy.check_tenant(str(entry.get("tenant") or ""))
        plan = tenancy.pod_plan(
            str(config["host"]), tenant,
            uid=entry.get("uid"),
            image=str(config.get("image") or "ameesh-runner"),
            storage_size=str((entry.get("storage_size")
                              or (config.get("tenant_defaults") or {}).get("storage_size")
                              or "20Gi")))
        slug = _slug(tenant.split(":", 1)[1])
        if slug in seen_slugs:
            raise RenderError("deux tenants partagent le nom court %r" % slug)
        if plan["uid"] in seen_uids:
            raise RenderError("deux tenants partagent l'UID %d (donner un --uid "
                              "explicite à l'un d'eux)" % plan["uid"])
        seen_slugs.add(slug)
        seen_uids.add(plan["uid"])
        entry["plan"] = plan
        normalised.append(entry)
    config = dict(config)
    config["tenants"] = normalised
    if not config.get("statefulset"):
        config["statefulset"] = "ameesh-pod-" + _slug(config["host"])
    return config


def _pod_plan_env(env) -> list[dict]:
    """Environnement d'un conteneur de tenant : celui du plan, plus le mode pod.

    Le plan porte déjà HOME/USER/LOGNAME/TMPDIR/AMEESH_* et la paire
    `safe.directory` bornée au dépôt du canon ; on n'ajoute aucun secret.
    `AMEESH_POD=1` rend le mode pod explicite au runner (les profils hors du
    home sont alors refusés à la publication, design §4.2)."""
    out = copy.deepcopy(env)
    if not any(item.get("name") == "AMEESH_POD" for item in out):
        out.append({"name": "AMEESH_POD", "value": "1"})
    return out


def _tenant_config(entry, config) -> dict:
    tenant = entry["plan"]["tenant"]
    ident = tenant.split(":", 1)[1]
    defaults = dict(config.get("tenant_defaults") or {})
    accounts = entry.get("accounts", defaults.get("accounts", {})) or {}
    canon = dict(config.get("canon") or {})
    return {
        "dsn": str(config.get("dsn") or ""),
        "schema": str(config.get("schema") or "public"),
        "project": str(config.get("project") or ""),
        "host": entry["plan"]["host"],
        "tenant": tenant,
        "runner_id": entry["plan"]["runner_id"],
        "humans": [ident],
        "pod": True,
        "canon": CANON_REPO,
        "canon_ref": "origin/%s" % (canon.get("branch") or "main"),
        "canon_sync_interval": int(defaults.get("canon_sync_interval", 0) or 0),
        "lease_ttl": int(defaults.get("lease_ttl", 120) or 120),
        "poll": int(defaults.get("poll", 5) or 5),
        "accounts": copy.deepcopy(accounts),
    }


def _supervisor_config(config) -> dict:
    supervisor = dict(config.get("supervisor") or {})
    canon = dict(config.get("canon") or {})
    hosts = [entry["plan"]["tenant"].split(":", 1)[1] for entry in config["tenants"]]
    return {
        "dsn": str(config.get("dsn") or ""),
        "schema": str(config.get("schema") or "public"),
        "project": str(config.get("project") or ""),
        "host": str(config["host"]),
        "humans": hosts,
        "pod": True,
        "canon": CANON_REPO,
        "canon_ref": "origin/%s" % (canon.get("branch") or "main"),
        "canon_sync_interval": int(supervisor.get("canon_sync_interval", 300) or 300),
        "lease_ttl": int(supervisor.get("lease_ttl", 120) or 120),
        "poll": int(supervisor.get("poll", 5) or 5),
    }


def _security_context(uid: int) -> dict:
    return {
        "runAsUser": uid,
        "runAsGroup": tenancy.POD_FS_GROUP,
        "runAsNonRoot": True,
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _args(poll: int) -> list[str]:
    return ["sh", "-ec", "umask 077\nexec ameesh run --poll %d" % int(poll)]


def _readiness() -> dict:
    return {
        "exec": {"command": ["ameesh", "doctor", "--probe"]},
        "initialDelaySeconds": 5,
        "periodSeconds": 30,
        "timeoutSeconds": 10,
        "failureThreshold": 3,
    }


def _labels(config, component="pod-host") -> dict:
    return {
        "app.kubernetes.io/name": "ameesh",
        "app.kubernetes.io/component": component,
        "app.kubernetes.io/part-of": "ameesh",
    }


def _canon_init(config) -> dict:
    canon = dict(config.get("canon") or {})
    image = str(config.get("image") or "ameesh-runner")
    return {
        "name": "canon",
        "image": image,
        "imagePullPolicy": str(config.get("image_pull_policy") or "IfNotPresent"),
        "args": ["sh", "-ec", (
            "install -m 600 /etc/ameesh/canon-key/ssh-privatekey /tmp/canon-key\n"
            "export GIT_SSH_COMMAND=\"ssh -i /tmp/canon-key -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ameesh/canon-key/known_hosts\"\n"
            "repo=/var/lib/ameesh/canon/repo\n"
            "if [ ! -d \"$repo/.git\" ]; then\n"
            "  git clone --quiet --branch \"$CANON_BRANCH\" -- \"$CANON_URL\" \"$repo\"\n"
            "fi\n"
            "ameesh canon sync --fetch || echo \"canon sync en échec : réclamation "
            "fermée jusqu'au prochain sync\" >&2\n"
            "rm -f /tmp/canon-key\n")],
        "env": [
            {"name": "HOME", "value": "/tmp"},
            {"name": "AMEESH_HOST", "value": str(config["host"])},
            {"name": "AMEESH_CONFIG", "value": "/etc/ameesh/supervisor/config.json"},
            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
                "name": "ameesh-db", "key": "password"}}},
            {"name": "CANON_URL", "valueFrom": {"configMapKeyRef": {
                "name": "ameesh-pod-canon", "key": "url"}}},
            {"name": "CANON_BRANCH", "valueFrom": {"configMapKeyRef": {
                "name": "ameesh-pod-canon", "key": "branch"}}},
        ],
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                      "limits": {"cpu": "250m", "memory": "256Mi"}},
        "securityContext": {
            "runAsUser": tenancy.POD_SUPERVISOR_UID,
            "runAsGroup": tenancy.POD_FS_GROUP,
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "privileged": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumeMounts": [
            {"name": "supervisor-config", "mountPath": "/etc/ameesh/supervisor",
             "readOnly": True},
            {"name": "canon", "mountPath": CANON_PATH},
            {"name": "canon-key", "mountPath": "/etc/ameesh/canon-key",
             "readOnly": True},
            {"name": "canon-tmp", "mountPath": "/tmp"},
        ],
    }


def _canon_fetch(config) -> dict:
    image = str(config.get("image") or "ameesh-runner")
    return {
        "name": "canon-fetch",
        "image": image,
        "imagePullPolicy": str(config.get("image_pull_policy") or "IfNotPresent"),
        "args": ["sh", "-ec", (
            "install -m 600 /etc/ameesh/canon-key/ssh-privatekey /tmp/canon-key\n"
            "export GIT_SSH_COMMAND=\"ssh -i /tmp/canon-key -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ameesh/canon-key/known_hosts\"\n"
            "while :; do\n"
            "  git -C /var/lib/ameesh/canon/repo fetch --quiet --no-tags origin \\\n"
            "    || echo \"git fetch du canon en échec : dernière révision connue "
            "conservée\" >&2\n"
            "  sleep \"$CANON_FETCH_INTERVAL\" & wait $!\n"
            "done\n")],
        "env": [
            {"name": "HOME", "value": "/tmp"},
            {"name": "CANON_FETCH_INTERVAL", "valueFrom": {"configMapKeyRef": {
                "name": "ameesh-pod-canon", "key": "fetch-interval"}}},
        ],
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                      "limits": {"cpu": "100m", "memory": "128Mi"}},
        "securityContext": _security_context(tenancy.POD_SUPERVISOR_UID),
        "volumeMounts": [
            {"name": "canon", "mountPath": CANON_PATH},
            {"name": "canon-key", "mountPath": "/etc/ameesh/canon-key",
             "readOnly": True},
            {"name": "canon-tmp", "mountPath": "/tmp"},
        ],
    }


def _supervisor(config) -> dict:
    supervisor = dict(config.get("supervisor") or {})
    poll = int(supervisor.get("poll", 5) or 5)
    limits = [(entry["plan"]["container"]["resources"]["limits"])
              for entry in config["tenants"]]
    memory = sum(_memory_bytes(limit.get("memory")) for limit in limits)
    cpu = sum(_cpu_cores(limit.get("cpu")) for limit in limits)
    return {
        "name": "supervisor",
        "image": str(config.get("image") or "ameesh-runner"),
        "imagePullPolicy": str(config.get("image_pull_policy") or "IfNotPresent"),
        "args": _args(poll),
        "workingDir": "/home/ameesh",
        "env": [
            {"name": "HOME", "value": "/home/ameesh"},
            {"name": "AMEESH_HOST", "value": str(config["host"])},
            {"name": "AMEESH_CONFIG", "value": "/etc/ameesh/supervisor/config.json"},
            # La capacité DÉCLARÉE du pod, pas les mesures du nœud : load1
            # n'est jamais mesuré (design §3.2). Elle suit la somme des limites
            # des tenants, mise à jour avec le chart.
            {"name": "AMEESH_POD", "value": "1"},
            {"name": "AMEESH_POD_MEMORY_BYTES", "value": str(memory)},
            {"name": "AMEESH_POD_CPU_CORES", "value": ("%g" % cpu)},
            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
                "name": "ameesh-db", "key": "password"}}},
        ],
        "readinessProbe": _readiness(),
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                      "limits": {"cpu": "500m", "memory": "512Mi"}},
        "securityContext": _security_context(tenancy.POD_SUPERVISOR_UID),
        "volumeMounts": [
            {"name": "supervisor-home", "mountPath": "/home/ameesh"},
            {"name": "supervisor-config", "mountPath": "/etc/ameesh/supervisor",
             "readOnly": True},
            {"name": "canon", "mountPath": CANON_PATH, "readOnly": True},
            {"name": "supervisor-tmp", "mountPath": "/tmp"},
        ],
    }


def _tenant_container(entry, config) -> dict:
    plan = entry["plan"]
    defaults = dict(config.get("tenant_defaults") or {})
    poll = int(defaults.get("poll", 5) or 5)
    slug = _slug(plan["tenant"].split(":", 1)[1])
    container = copy.deepcopy(plan["container"])
    container["args"] = _args(poll)
    container["workingDir"] = plan["home"]
    container["imagePullPolicy"] = str(
        config.get("image_pull_policy") or "IfNotPresent")
    container["env"] = _pod_plan_env(plan["container"]["env"])
    # Le DSN du ConfigMap ne porte aucun mot de passe (design §6.3) : sans
    # cette variable, le runner du tenant ne peut pas ouvrir la base (pas de
    # battement, donc pas de réclamation). Même secret que le superviseur ;
    # `envFrom` reste réservé aux clés d'API du tenant.
    container["env"].append({"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
        "name": "ameesh-db", "key": "password"}}})
    container["readinessProbe"] = _readiness()
    container["envFrom"] = [{"secretRef": {
        "name": str(entry.get("secret") or "ameesh-harness-" + slug),
        "optional": True}}]
    mounts = []
    for mount in container["volumeMounts"]:
        mount = dict(mount)
        if mount["name"] == "tenant-tmp":
            mount["name"] = "tenant-tmp-" + slug
        elif mount["name"] == "tenant-config":
            mount["name"] = "tenant-config-" + slug
        mounts.append(mount)
    container["volumeMounts"] = mounts
    return container


def _statefulset(config) -> dict:
    plans = [entry["plan"] for entry in config["tenants"]]
    volumes = [
        {"name": "supervisor-home", "emptyDir": {"sizeLimit": "1Gi"}},
    ]
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = _slug(plan["tenant"].split(":", 1)[1])
        volumes.append({"name": "home-" + slug, "persistentVolumeClaim": {
            "claimName": "pvc-" + slug}})
    volumes += [
        {"name": "canon", "emptyDir": {}},
        {"name": "canon-key", "secret": {
            "secretName": "ameesh-canon-deploy-key", "defaultMode": 0o440}},
        {"name": "canon-tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
        {"name": "supervisor-tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        {"name": "supervisor-config", "configMap": {"name": "ameesh-pod-supervisor"}},
        {"name": "passwd", "configMap": {
            "name": "ameesh-pod-passwd",
            "items": [{"key": "passwd", "path": "passwd"}]}},
        {"name": "group", "configMap": {
            "name": "ameesh-pod-passwd",
            "items": [{"key": "group", "path": "group"}]}},
    ]
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = _slug(plan["tenant"].split(":", 1)[1])
        volumes.append({"name": "tenant-config-" + slug, "configMap": {
            "name": plan["config_maps"]["tenant"]["name"],
            "items": [{"key": "config.json", "path": "config.json"}]}})
        volumes.append({"name": "tenant-tmp-" + slug, "emptyDir": {
            "sizeLimit": plan["volumes"]["tmp"]["emptyDir"]["sizeLimit"]}})

    containers = [_supervisor(config)]
    containers += [_tenant_container(entry, config) for entry in config["tenants"]]
    containers.append(_canon_fetch(config))

    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": str(config["statefulset"]),
            "labels": _labels(config),
        },
        "spec": {
            # Un seul superviseur : deux répliques seraient deux écrivains.
            # Le bail SQL (0599) est l'arbitre, ceci la politesse du chart.
            "replicas": 1,
            "serviceName": "ameesh-pod",
            "podManagementPolicy": "OrderedReady",
            "updateStrategy": {"type": "RollingUpdate"},
            "selector": {"matchLabels": _labels(config)},
            "template": {
                "metadata": {"labels": _labels(config)},
                "spec": {
                    "serviceAccountName": SA_NAME,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "hostNetwork": False,
                    "hostPID": False,
                    "hostIPC": False,
                    "shareProcessNamespace": False,
                    "terminationGracePeriodSeconds": 120,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": tenancy.POD_SUPERVISOR_UID,
                        "runAsGroup": tenancy.POD_FS_GROUP,
                        "fsGroup": tenancy.POD_FS_GROUP,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": [_canon_init(config)],
                    "containers": containers,
                    "volumes": volumes,
                },
            },
        },
    }


def _configmaps(config) -> list[dict]:
    plans = [entry["plan"] for entry in config["tenants"]]
    # Le fichier passwd est la réunion des plans : root + superviseur, une
    # ligne par tenant (ses UID), nobody. Le group est commun (GID neutre).
    root = plans[0]["passwd"].splitlines()[0]
    supervisor = plans[0]["passwd"].splitlines()[1]
    nobody = plans[0]["passwd"].splitlines()[3]
    tenant_lines = [plan["passwd"].splitlines()[2] for plan in plans]
    passwd = "\n".join([root, supervisor, *tenant_lines, nobody]) + "\n"
    group = plans[0]["group"].rstrip("\n") + "\n"
    out = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ameesh-pod-passwd", "labels": _labels(config)},
            "data": {"passwd": passwd, "group": group},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ameesh-pod-supervisor", "labels": _labels(config)},
            "data": {"config.json": json.dumps(_supervisor_config(config), indent=2,
                                               ensure_ascii=False) + "\n"},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ameesh-pod-canon", "labels": _labels(config)},
            "data": {
                "url": str((config.get("canon") or {}).get("url") or ""),
                "branch": str((config.get("canon") or {}).get("branch") or "main"),
                "fetch-interval": str(int((config.get("canon") or {}).get(
                    "fetch_interval", 300) or 300)),
            },
        },
    ]
    for entry in config["tenants"]:
        plan = entry["plan"]
        out.append({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": plan["config_maps"]["tenant"]["name"],
                "labels": _labels(config),
            },
            "data": {"config.json": json.dumps(_tenant_config(entry, config),
                                               indent=2, ensure_ascii=False) + "\n"},
        })
    return out


def _pvcs(config) -> list[dict]:
    out = []
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = _slug(plan["tenant"].split(":", 1)[1])
        out.append({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": "pvc-" + slug,
                "labels": dict(_labels(config),
                               **{"ameesh.kubernetes.io/tenant": slug}),
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {
                    "storage": plan["volumes"]["home"]["size"]}},
            },
        })
    return out


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _represent_str)


def _text(documents, source: str) -> str:
    body = yaml.dump_all(documents, Dumper=_Dumper, sort_keys=False,
                         default_flow_style=False, allow_unicode=True,
                         width=1000)
    return HEADER % source + body


def render(config: dict, source: str = "host.exemple.yaml") -> dict[str, str]:
    return {
        "configmaps.yaml": _text(_configmaps(config), source),
        "pvcs.yaml": _text(_pvcs(config), source),
        "statefulset.yaml": _text([_statefulset(config)], source),
    }


def render_all(config: dict) -> list[dict]:
    """Le rendu en objets, pour les tests et les outils (kubectl apply -f -)."""
    return _configmaps(config) + _pvcs(config) + [_statefulset(config)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="render.py",
        description="Rend le chart d'un hôte pod partagé à N tenants (#7695).")
    parser.add_argument("--config", default=str(HERE / "host.exemple.yaml"))
    parser.add_argument("--out", default=str(HERE / "rendered"))
    parser.add_argument("--write", action="store_true",
                        help="écrit les fichiers rendus")
    parser.add_argument("--check", action="store_true",
                        help="compare les fichiers rendus au rendu courant")
    parser.add_argument("--stdout", action="store_true",
                        help="imprime le rendu (sans écrire)")
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
        source = os.path.relpath(config_path, ROOT)
        rendered = render(config, source)
    except (RenderError, tenancy.TenancyError) as exc:
        print("rendu refusé : %s" % exc, file=sys.stderr)
        return 2
    if args.stdout:
        for name in RENDERED:
            sys.stdout.write(rendered[name])
        return 0
    out = Path(args.out).resolve()
    if args.check or not args.write:
        ecarts = 0
        for name in RENDERED:
            path = out / name
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current == rendered[name]:
                continue
            ecarts += 1
            print("écart : %s" % path, file=sys.stderr)
            for line in list(difflib.unified_diff(
                    current.splitlines(), rendered[name].splitlines(),
                    fromfile=str(path), tofile="rendu courant", lineterm=""))[:40]:
                print(line, file=sys.stderr)
        if ecarts:
            print("rendu périmé : lancer `render.py --write` et relire le diff",
                  file=sys.stderr)
            return 1
        print("rendu à jour : %d fichier(s)" % len(RENDERED))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for name in RENDERED:
        (out / name).write_text(rendered[name], encoding="utf-8")
        print("écrit : %s" % (out / name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
