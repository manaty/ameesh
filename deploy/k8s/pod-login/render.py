#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7911 : pod de connexion du siège d'un tenant pod (design 7633 §7.3).

Le pod partagé ne reçoit **jamais** `pods/exec` : `kubectl exec` choisit le
conteneur dans la requête, pas dans l'objet RBAC, donc un `resourceNames` sur
le pod partagé ouvrirait tous ses conteneurs, ceux des autres tenants compris.

Ce rendu produit, pour **une** personne et **une** session :

* un pod d'un seul conteneur qui monte **seulement** le PVC de la personne,
  sous son UID/GID, sans superviseur, sans jeton de compte de service,
  `readOnlyRootFilesystem: true`, `/tmp` en `emptyDir`, TTL <= 30 min, épinglé
  au même nœud que le pod partagé (le PVC `ReadWriteOnce` est monté par les
  deux pods) ;
* un Role `pods/exec` borné par `resourceNames` à **ce seul pod** ;
* si `--subject` est donné, le RoleBinding de la personne (`User:<nom>` ou
  `Group:<nom>`). Sans sujet, le Role est rendu seul : le bureau crée le
  binding avec l'identité Kubernetes réelle de la personne.

Aucun secret, aucune clé, aucun identifiant n'est rendu ni lu : la personne
lance `claude /login` ou `codex login` elle-même dans le pod. La fenêtre
`login` (preuve D2, TTL) est ouverte en base par `ameesh host tenant login`.

    python3 deploy/k8s/pod-login/render.py --namespace ameesh \\
      --host pod-alice --tenant human:alice --uid 2519 --pvc pvc-alice \\
      --pod ameesh-login-alice --node node-1 \\
      --image registry.example/ameesh-runner@sha256:... --subject User:alice
"""
from __future__ import annotations

import argparse
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

#: borne du design §7.3 : `activeDeadlineSeconds` <= 30 min.
TTL_MAX_S = 1800
TTL_MIN_S = 30
#: fsGroup neutre du chart pod-shared (jamais un fsGroup par tenant).
DEFAULT_FS_GROUP = 10000
#: préfixe du pod de connexion ; un nom de pod partagé est refusé.
LOGIN_PREFIX = "ameesh-login-"
SHARED_PREFIX = "ameesh-pod-"
DNS_1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
TENANT_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._@-]*$")


class RenderError(ValueError):
    """Entrée refusée (nom hors chart, TTL hors borne, image non épinglée)."""


def slug(tenant: str) -> str:
    ident = str(tenant).strip()
    if not TENANT_RE.match(ident):
        raise RenderError("tenant must be `human:<id>`, got %r" % tenant)
    text = re.sub(r"[^a-z0-9-]+", "-", ident[len("human:"):].lower())
    text = text.strip("-")[:40].strip("-")
    return text or "tenant"


def _name(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or not DNS_1123.match(text) or len(text) > 63:
        raise RenderError("%s must be a DNS-1123 name (<= 63), got %r"
                          % (label, value))
    return text


def _pinned_image(image: str) -> str:
    text = str(image or "").strip()
    if "@sha256:" not in text:
        raise RenderError("image must be pinned by digest (`...@sha256:...`), "
                          "got %r" % image)
    return text


def build(*, namespace: str, host: str, tenant: str, uid: int, pvc: str,
          pod: str, node: str, image: str, ttl_s: int = TTL_MAX_S,
          fs_group: int = DEFAULT_FS_GROUP,
          subject: str | None = None) -> list[dict]:
    """Les objets Kubernetes du pod de connexion (Pod, Role, RoleBinding)."""
    namespace = _name(namespace, "namespace")
    host = _name(host, "host")
    node = _name(node, "node")
    pvc = _name(pvc, "pvc")
    pod = _name(pod, "pod")
    if pod.startswith(SHARED_PREFIX):
        raise RenderError("refusing a shared pod name %r: no pods/exec on the "
                          "shared pod (design 7633 §7.3)" % pod)
    if not pod.startswith(LOGIN_PREFIX):
        raise RenderError("the connection pod must be named `%s…`, got %r "
                          "(the shared pod is never an exec target)"
                          % (LOGIN_PREFIX, pod))
    image = _pinned_image(image)
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        raise RenderError("uid must be a positive integer") from None
    if uid <= 0:
        raise RenderError("uid must be a positive integer")
    try:
        fs_group = int(fs_group)
    except (TypeError, ValueError):
        raise RenderError("fs-group must be a positive integer") from None
    if fs_group <= 0:
        raise RenderError("fs-group must be a positive integer")
    try:
        ttl_s = int(ttl_s)
    except (TypeError, ValueError):
        raise RenderError("ttl must be an integer number of seconds") from None
    if not (TTL_MIN_S <= ttl_s <= TTL_MAX_S):
        raise RenderError("ttl must be between %d and %d seconds (design 7633 "
                          "§7.3), got %s" % (TTL_MIN_S, TTL_MAX_S, ttl_s))
    ident = slug(tenant)
    user = "ameesh-" + ident
    home = "/home/" + user
    labels = {
        "app.kubernetes.io/name": "ameesh",
        "app.kubernetes.io/component": "pod-login",
        "ameesh.dev/host": host,
        "ameesh.dev/tenant": ident,
    }
    pod_doc = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod,
            "namespace": namespace,
            "labels": dict(labels),
        },
        "spec": {
            # Même nœud que le pod partagé : le PVC ReadWriteOnce est monté
            # par les deux pods sans détachement (design §7.3, étape 2).
            "nodeName": node,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": ttl_s,
            "terminationGracePeriodSeconds": 5,
            # Aucun jeton de compte de service : le shell de connexion ne
            # parle pas à l'API Kubernetes.
            "automountServiceAccountToken": False,
            "serviceAccountName": "ameesh-login",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": uid,
                "runAsGroup": fs_group,
                "fsGroup": fs_group,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [{
                "name": "login",
                "image": image,
                "command": ["sleep", "infinity"],
                "workingDir": home,
                "env": [
                    {"name": "HOME", "value": home},
                    {"name": "USER", "value": user},
                    {"name": "LOGNAME", "value": user},
                    {"name": "TERM", "value": "xterm-256color"},
                    {"name": "TMPDIR", "value": "/tmp"},
                ],
                "volumeMounts": [
                    {"name": "home", "mountPath": home},
                    {"name": "passwd", "mountPath": "/etc/passwd",
                     "subPath": "passwd", "readOnly": True},
                    {"name": "group", "mountPath": "/etc/group",
                     "subPath": "group", "readOnly": True},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": uid,
                    "runAsGroup": fs_group,
                    "allowPrivilegeEscalation": False,
                    "privileged": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
            }],
            "volumes": [
                # Le SEUL volume de la personne : son PVC, jamais celui d'un
                # pair ni le dossier du superviseur.
                {"name": "home",
                 "persistentVolumeClaim": {"claimName": pvc}},
                # ConfigMap passwd/group du chart pod-shared : l'UID du chart
                # se résout dans le shell (design §6.3).
                {"name": "passwd",
                 "configMap": {"name": "ameesh-pod-passwd",
                               "items": [{"key": "passwd", "path": "passwd"}]}},
                {"name": "group",
                 "configMap": {"name": "ameesh-pod-passwd",
                               "items": [{"key": "group", "path": "group"}]}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
            ],
        },
    }
    service_account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "ameesh-login",
            "namespace": namespace,
            "labels": dict(labels),
        },
        # Aucun jeton monté, et aucun RoleBinding ne nomme ce compte.
        "automountServiceAccountToken": False,
    }
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": pod,
            "namespace": namespace,
            "labels": dict(labels),
        },
        "rules": [{
            "apiGroups": [""],
            "resources": ["pods/exec"],
            # Le pod de connexion n'a qu'un conteneur : la limite par pod
            # suffit. Le pod partagé n'est jamais nommé ici.
            "resourceNames": [pod],
            "verbs": ["create"],
        }],
    }
    docs = [service_account, pod_doc, role]
    if subject:
        kind, _, name = str(subject).partition(":")
        kind, name = kind.strip(), name.strip()
        if kind not in ("User", "Group") or not name:
            raise RenderError("subject must be `User:<name>` or `Group:<name>`, "
                              "got %r" % subject)
        docs.append({
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": pod,
                "namespace": namespace,
                "labels": dict(labels),
            },
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
                        "kind": "Role", "name": pod},
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io",
                          "kind": kind, "name": name}],
        })
    return docs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pod-login-render",
        description="#7911: dedicated seat-connection pod, exec Role and binding")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--host", required=True, help="logical pod host")
    parser.add_argument("--tenant", required=True, help="human:<id>")
    parser.add_argument("--uid", required=True, type=int,
                        help="tenant UID from the chart (host_tenants.uid)")
    parser.add_argument("--pvc", required=True, help="the tenant's PVC name")
    parser.add_argument("--pod", required=True,
                        help="connection pod name (ameesh-login-…)")
    parser.add_argument("--node", required=True,
                        help="spec.nodeName of the shared pod (same node)")
    parser.add_argument("--image", required=True,
                        help="harness image pinned by digest")
    parser.add_argument("--ttl", type=int, default=TTL_MAX_S,
                        help="activeDeadlineSeconds (30..1800, default 1800)")
    parser.add_argument("--fs-group", type=int, default=DEFAULT_FS_GROUP)
    parser.add_argument("--subject",
                        help="RoleBinding subject: User:<name> or Group:<name>")
    args = parser.parse_args(argv)
    if yaml is None:
        print("pod-login-render: PyYAML absent", file=sys.stderr)
        return 2
    try:
        docs = build(namespace=args.namespace, host=args.host,
                     tenant=args.tenant, uid=args.uid, pvc=args.pvc,
                     pod=args.pod, node=args.node, image=args.image,
                     ttl_s=args.ttl, fs_group=args.fs_group,
                     subject=args.subject)
    except RenderError as exc:
        print("pod-login-render: %s" % exc, file=sys.stderr)
        return 1
    print(yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False),
          end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
