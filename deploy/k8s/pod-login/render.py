#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7911 : pod de connexion du siège d'un tenant pod (design 7633 §7.3).

Le pod partagé ne reçoit **jamais** `pods/exec` : `kubectl exec` choisit le
conteneur dans la requête, pas dans l'objet RBAC, donc un `resourceNames` sur
le pod partagé ouvrirait tous ses conteneurs, ceux des autres tenants compris.

Ce rendu produit, pour **une** personne et **une** session :

* un pod d'un seul conteneur qui monte **seulement** le PVC de la personne,
  sous son UID/GID, sans superviseur, sans jeton de compte de service,
  `readOnlyRootFilesystem: true`, `/tmp` en `emptyDir`, épinglé au même nœud
  que le pod partagé (le PVC `ReadWriteOnce` est monté par les deux pods).
  Son `activeDeadlineSeconds` est dérivé de l'échéance de la fenêtre `login` :
  le pod meurt 30 s **avant** que la base ne rende la main aux prises du
  tenant, jamais après (design §7.3) ;
* le processus principal du conteneur tient un **verrou** et écrit un
  **battement** dans `.ameesh/seat-login/` sur le PVC, une fois par seconde.
  Le runner du tenant, qui monte le même volume, n'enregistre la fin du pod
  qu'après verrou libre **et** battement silencieux depuis 30 s : la reprise
  est conditionnée à la fin **observée** du pod, pas à la seule échéance SQL
  (B/P2 #46731). Fichier absent ou verrou tenu = pas de fin observée ;
* un Role `get` + `pods/exec` borné par `resourceNames` à **ce seul pod**
  (`kubectl exec` lit d'abord le pod, puis demande l'exec) ;
* si `--subject` est donné, le RoleBinding de la personne (`User:<nom>` ou
  `Group:<nom>`). Sans sujet, le Role est rendu seul : le bureau crée le
  binding avec l'identité Kubernetes réelle de la personne.

Aucun secret, aucune clé, aucun identifiant n'est rendu ni lu : la personne
lance `claude /login` ou `codex login` elle-même dans le pod. La fenêtre
`login` (preuve D2, TTL) est ouverte en base par `ameesh host tenant login`,
qui rend aussi le `pod_name` à passer ici.

    python3 deploy/k8s/pod-login/render.py --namespace ameesh \\
      --host pod-alice --tenant human:alice --uid 2519 --pvc pvc-alice \\
      --pod ameesh-login-alice-1a2b3c4d --node node-1 \\
      --image registry.example/ameesh-runner@sha256:... \\
      --expires-at 1795742400 --subject User:alice
"""
from __future__ import annotations

import argparse
import re
import sys
import time

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

#: borne du design §7.3 : `activeDeadlineSeconds` <= 30 min.
TTL_MAX_S = 1800
TTL_MIN_S = 30
#: #7911 : le pod de connexion doit mourir **avant** la fenêtre `login` de la
#: base, qui rend la main aux prises du tenant à `expires_at`. La marge couvre
#: la terminaison du pod (grâce 5 s) et la reprise du runner : le pod est
#: éteint au plus tard à `expires_at - 30 s`, jamais après la garde SQL.
TERMINATION_MARGIN_S = 30
#: fsGroup neutre du chart pod-shared (jamais un fsGroup par tenant).
DEFAULT_FS_GROUP = 10000
#: préfixe du pod de connexion ; un nom de pod partagé est refusé.
LOGIN_PREFIX = "ameesh-login-"
SHARED_PREFIX = "ameesh-pod-"
#: #7911 : répertoire du verrou et du battement, à la racine du PVC du tenant.
#: `tenancy.LOGIN_BEAT_DIR` porte la même valeur (le runner lit ce chemin).
BEAT_DIR = ".ameesh/seat-login"
DNS_1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
TENANT_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._@-]*$")


def beat_command(pod: str, expires_at: float) -> list[str]:
    """PID 1 du pod de connexion : verrou + battement, sortie à l'échéance.

    Le verrou est tenu jusqu'à la mort du processus (le noyau le rend, même
    après SIGKILL) et le battement est réécrit chaque seconde. Le runner du
    tenant, qui monte le même PVC sous le même UID, ne reprend qu'après verrou
    libre **et** battement silencieux depuis `LOGIN_BEAT_STALE_S` : la fin du
    pod est observée là où l'écriture concurrente aurait lieu, sans jeton
    Kubernetes dans le pod (B/P2 #46731).

    Le processus sort de lui-même à `expires_at - TERMINATION_MARGIN_S` : la
    borne est **absolue**, calculée sur l'horloge de la fenêtre, pas sur celle
    du démarrage du conteneur. Un pod créé tard après le rendu ne peut donc
    pas survivre à l'échéance SQL ; `activeDeadlineSeconds` n'est que le
    filet de sécurité du manifeste.
    """
    deadline = float(expires_at) - TERMINATION_MARGIN_S
    script = (
        "import fcntl, os, time\n"
        + "deadline = %.0f\n" % deadline
        + "base = os.path.join(os.environ['HOME'], %r)\n" % BEAT_DIR
        + "os.makedirs(base, mode=0o700, exist_ok=True)\n"
        "lock = open(os.path.join(base, %r), 'w')\n" % (pod + ".lock")
        + "fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "beat = os.path.join(base, %r)\n" % (pod + ".beat")
        + "while time.time() < deadline:\n"
        "    with open(beat, 'w') as stream:\n"
        "        stream.write('%.0f' % time.time())\n"
        "    time.sleep(1)\n"
    )
    return ["python3", "-c", script]


class RenderError(ValueError):
    """Entrée refusée (nom hors chart, échéance hors borne, image non épinglée)."""


def deadline_ttl(expires_at: float, *, now: float | None = None) -> int:
    """`activeDeadlineSeconds` d'un pod de connexion, dérivé de la fenêtre.

    `expires_at` est l'échéance de la fenêtre `tenant_logins` (epoch, le
    `expires_ts` rendu par `host tenant login`) ; le pod meurt
    `TERMINATION_MARGIN_S` avant, pour que la garde `ameesh_tenant_login_active`
    ne rende pas la main aux prises du tenant pendant que le pod monte encore
    le PVC. Refuse une échéance déjà trop proche ou hors de la borne de 30 min.
    """
    now = time.time() if now is None else float(now)
    try:
        expires = float(expires_at)
    except (TypeError, ValueError):
        raise RenderError("expires-at must be an epoch number, got %r"
                          % (expires_at,)) from None
    remaining = expires - now
    if remaining > TTL_MAX_S + TERMINATION_MARGIN_S:
        raise RenderError(
            "expires-at is more than 30 min away (%ss): not a seat-login "
            "window (design 7633 §7.3)" % int(remaining))
    ttl = int(remaining - TERMINATION_MARGIN_S)
    if ttl < TTL_MIN_S:
        raise RenderError(
            "the seat-login window ends in %ss, less than the %ss margin plus "
            "the %ss minimum pod TTL: reopen the window"
            % (int(remaining), TERMINATION_MARGIN_S, TTL_MIN_S))
    return min(ttl, TTL_MAX_S)



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
          pod: str, node: str, image: str, expires_at: float,
          now: float | None = None,
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
    ttl_s = deadline_ttl(expires_at, now=now)
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
                # #7911 : verrou + battement sur le PVC, pour que le runner
                # observe la fin du pod avant de reprendre (B/P2 #46731).
                "command": beat_command(pod, expires_at),
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
        "rules": [
            {
                # `kubectl exec` lit d'abord le pod (`Pods.Get`) pour résoudre
                # la phase et le conteneur : sans ce `get`, l'exec échoue avant
                # la sous-ressource. Borné au même pod, jamais au partagé.
                "apiGroups": [""],
                "resources": ["pods"],
                "resourceNames": [pod],
                "verbs": ["get"],
            },
            {
                "apiGroups": [""],
                "resources": ["pods/exec"],
                # Le pod de connexion n'a qu'un conteneur : la limite par pod
                # suffit. Le pod partagé n'est jamais nommé ici.
                "resourceNames": [pod],
                "verbs": ["create"],
            },
        ],
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
    parser.add_argument(
        "--expires-at", required=True, type=float,
        help="epoch seconds of the login window expiry (`expires_ts` from "
             "`ameesh host tenant login`); the pod dies 30 s before it")
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
                     expires_at=args.expires_at, fs_group=args.fs_group,
                     subject=args.subject)
    except RenderError as exc:
        print("pod-login-render: %s" % exc, file=sys.stderr)
        return 1
    print(yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False),
          end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
