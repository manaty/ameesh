# SPDX-License-Identifier: AGPL-3.0-only
"""#7697 (P4 de #7633) : le pourvoyeur de pods enfants.

Design `docs/design/7633-hote-avatars-pod.md` §8.2, §8.4, §8.5. Un seul
composant du mesh parle à l'API Kubernetes : `system:ameesh-pod-provisioner`
(#6091). Il lit `pod_requests`, crée un pod à partir d'un template FIGÉ
(`pod_templates`), puis écrit `pod_hosts`, `host_tenancy` et `host_tenants`
dans une transaction. À l'échéance il supprime le pod et les liaisons ; il ne
supprime jamais un pod d'un autre parent (labels vérifiés).

Le pod enfant a deux conteneurs : un superviseur (état machine, capacité
déclarée, bail #7695) et le runner de l'enfant (UID neuf, `emptyDir`, aucun
PVC du parent, aucun `hostPath`). La clé d'API d'organisation est lue dans un
Secret par enfant, que le pourvoyeur ne lit jamais ; le relais de modèle est
une tranche ultérieure (D6, pilote 20/11).

Ce module ne crée aucun pod sans template validé, et n'accepte que les
placeholders connus. Les objets créés sont des `Pod` nus : `ttlSecondsAfterFinished`
est un champ de Job, pas de Pod ; le TTL est tenu par `activeDeadlineSeconds`
et par le nettoyage du pourvoyeur.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'image d'exécution
    yaml = None

from . import config as config_mod
from . import db as db_mod
from . import dsn as dsn_mod
from . import storage, tenancy

ACTOR = "system:ameesh-pod-provisioner"
DEFAULT_TEMPLATE = "/etc/ameesh/pod-provisioner/template.yaml"
DEFAULT_IMAGE = "ameesh-runner"
DEFAULT_NAMESPACE = "ameesh"
#: marge de l'`activeDeadlineSeconds` au-delà du TTL de la demande.
DEADLINE_GRACE_S = 300
MAX_DEADLINE_S = 7 * 86400
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: placeholders que le template peut porter ; tout autre est refusé.
ALLOWED_PLACEHOLDERS = frozenset({
    "POD_NAME", "NAMESPACE", "HOST", "PARENT", "CHILD", "TENANT", "TEMPLATE",
    "REQUEST_ID", "UID", "UNIX_USER", "HOME", "CWD", "RUNNER_ID", "CONFIGMAP",
    "CREDENTIAL_SECRET", "CREDENTIAL_ENV", "IMAGE", "ACTIVE_DEADLINE",
    "CPU_REQUEST", "CPU_LIMIT", "MEMORY_REQUEST", "MEMORY_LIMIT",
    "MEMORY_BYTES", "CPU_CORES", "DSN", "EXPIRES_AT",
})
#: placeholders sans lesquels le pod ne peut pas être un hôte enfant sûr.
REQUIRED_PLACEHOLDERS = frozenset({
    "POD_NAME", "NAMESPACE", "HOST", "PARENT", "CHILD", "TENANT", "TEMPLATE",
    "REQUEST_ID", "UID", "HOME", "CWD", "RUNNER_ID", "CONFIGMAP",
    "CREDENTIAL_SECRET", "CREDENTIAL_ENV", "IMAGE", "ACTIVE_DEADLINE",
})


class ProvisionerError(RuntimeError):
    """Refus du pourvoyeur : code stable, message sans secret."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


class KubeError(ProvisionerError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message)


class KubeConflict(KubeError):
    pass


def _short_error(exc: BaseException) -> str:
    """Message borné pour `pod_requests.error` : jamais un corps de réponse."""
    text = str(exc or "").strip().replace("\n", " ")
    return text[:900] or exc.__class__.__name__


def _protocol_slug(value: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return (slug[:limit].strip("-") or "x")


def pod_name_for(request_id: str) -> str:
    """Nom du Pod : dérivé de l'UUID de la demande (jamais du parent)."""
    token = re.sub(r"[^a-f0-9]", "", str(request_id or "").lower())[:12]
    if not token:
        raise ProvisionerError("request-invalid", "a request id is required")
    return "ameesh-child-" + token


def configmap_name_for(request_id: str) -> str:
    return pod_name_for(request_id) + "-cfg"


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPE]i?|)", str(value).strip(),
                         re.IGNORECASE)
    if not match:
        raise ProvisionerError("template-invalid", "unreadable memory %r" % (value,))
    factors = {"": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3,
               "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6}
    suffix = match.group(2).upper()
    if suffix.endswith("I"):
        factors = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
                   "T": 1024 ** 4, "P": 1024 ** 5, "E": 1024 ** 6}
        suffix = suffix[:-1]
    return int(float(match.group(1)) * factors[suffix])


def _cpu_cores(value: str) -> float:
    text = str(value).strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(m?)", text)
    if not match:
        raise ProvisionerError("template-invalid", "unreadable cpu %r" % (value,))
    number = float(match.group(1))
    return number / 1000.0 if match.group(2) == "m" else number


def _walk(value, visit):
    if isinstance(value, dict):
        for key, item in value.items():
            _walk(item, visit)
    elif isinstance(value, list):
        for item in value:
            _walk(item, visit)
    else:
        visit(value)


def _strings(value):
    out = []
    _walk(value, lambda item: out.append(item) if isinstance(item, str) else None)
    return out


def _replace(value, mapping):
    if isinstance(value, dict):
        return {key: _replace(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, mapping) for item in value]
    if isinstance(value, str):
        # Une valeur qui n'est QUE un placeholder garde le type de la source
        # (`runAsUser` et `activeDeadlineSeconds` sont des entiers) ; un
        # placeholder noyé dans du texte est interpolé en chaîne.
        exact = re.fullmatch(r"\{\{([A-Z0-9_]+)\}\}", value)
        if exact:
            key = exact.group(1)
            if key not in mapping:
                raise ProvisionerError("template-invalid",
                                       "unknown placeholder {{%s}}" % key)
            return mapping[key]

        def one(match):
            key = match.group(1)
            if key not in mapping:
                raise ProvisionerError("template-invalid",
                                       "unknown placeholder {{%s}}" % key)
            return str(mapping[key])
        rendered = re.sub(r"\{\{([A-Z0-9_]+)\}\}", one, value)
        if "{{" in rendered or "}}" in rendered:
            raise ProvisionerError("template-invalid",
                                   "unresolved placeholder in %r" % value)
        return rendered
    return value


@dataclass(frozen=True)
class Rendered:
    pod: dict
    configmap: dict
    pod_name: str
    configmap_name: str


@dataclass(frozen=True)
class PodTemplate:
    name: str
    path: str
    raw: dict
    fingerprint: str

    @classmethod
    def load(cls, path: str, *, name: str = tenancy.POD_CHILD_TEMPLATE):
        if yaml is None:
            raise ProvisionerError("template-unreadable",
                                   "PyYAML is required to read the pod template")
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProvisionerError("template-unreadable",
                                   "cannot read template %s: %s" % (path, exc)) from exc
        try:
            raw = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - YAML illisible : refus
            raise ProvisionerError("template-unreadable",
                                   "cannot parse template %s: %s" % (path, exc)) from exc
        if not isinstance(raw, dict):
            raise ProvisionerError("template-invalid", "the template is not a mapping")
        digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")).hexdigest()
        template = cls(name=str(name), path=str(path), raw=raw, fingerprint=digest)
        template.validate()
        return template

    def placeholders(self) -> set[str]:
        found = set()
        for text in _strings(self.raw):
            found.update(re.findall(r"\{\{([A-Z0-9_]+)\}\}", text))
        return found

    def validate(self) -> None:
        raw = self.raw
        if raw.get("apiVersion") != "v1" or raw.get("kind") != "Pod":
            raise ProvisionerError("template-invalid",
                                   "the template must be a v1 Pod")
        found = self.placeholders()
        unknown = found - ALLOWED_PLACEHOLDERS
        if unknown:
            raise ProvisionerError("template-invalid",
                                   "unknown placeholders: %s" % ", ".join(sorted(unknown)))
        missing = REQUIRED_PLACEHOLDERS - found
        if missing:
            raise ProvisionerError("template-invalid",
                                   "missing placeholders: %s" % ", ".join(sorted(missing)))
        metadata = raw.get("metadata") or {}
        if metadata.get("name") != "{{POD_NAME}}":
            raise ProvisionerError("template-invalid",
                                   "metadata.name must be {{POD_NAME}}")
        if metadata.get("namespace") != "{{NAMESPACE}}":
            raise ProvisionerError("template-invalid",
                                   "metadata.namespace must be {{NAMESPACE}}")
        spec = raw.get("spec") or {}
        if spec.get("restartPolicy") != "Never":
            raise ProvisionerError("template-invalid", "spec.restartPolicy must be Never")
        if spec.get("automountServiceAccountToken") is not False:
            raise ProvisionerError("template-invalid",
                                   "spec.automountServiceAccountToken must be false")
        if spec.get("serviceAccountName") != "ameesh-child":
            raise ProvisionerError("template-invalid",
                                   "spec.serviceAccountName must be ameesh-child")
        for forbidden in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(forbidden) is True:
                raise ProvisionerError("template-invalid",
                                       "spec.%s is forbidden" % forbidden)
        pod_security = spec.get("securityContext") or {}
        if pod_security.get("runAsNonRoot") is not True:
            raise ProvisionerError("template-invalid",
                                   "spec.securityContext.runAsNonRoot must be true")
        if (pod_security.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
            raise ProvisionerError("template-invalid",
                                   "spec.securityContext.seccompProfile must be RuntimeDefault")
        volumes = spec.get("volumes") or []
        if not isinstance(volumes, list):
            raise ProvisionerError("template-invalid", "spec.volumes must be a list")
        for volume in volumes:
            if not isinstance(volume, dict):
                raise ProvisionerError("template-invalid", "a volume is not a mapping")
            if "hostPath" in volume or "persistentVolumeClaim" in volume:
                raise ProvisionerError(
                    "template-invalid",
                    "a child pod never mounts a hostPath or a parent PVC")
            allowed = {"name", "emptyDir", "configMap", "secret"}
            if set(volume) - allowed:
                raise ProvisionerError(
                    "template-invalid",
                    "volume %r has a forbidden source: %s"
                    % (volume.get("name"), ", ".join(sorted(set(volume) - allowed))))
        containers = spec.get("containers") or []
        if not isinstance(containers, list) or not containers:
            raise ProvisionerError("template-invalid", "spec.containers is empty")
        names = {container.get("name") for container in containers
                 if isinstance(container, dict)}
        if names != {"supervisor", "child"}:
            raise ProvisionerError(
                "template-invalid",
                "spec.containers must be exactly supervisor and child (got %s)"
                % ", ".join(sorted(str(name) for name in names)))
        for container in containers:
            if not isinstance(container, dict):
                raise ProvisionerError("template-invalid", "a container is not a mapping")
            if container.get("image") != "{{IMAGE}}":
                raise ProvisionerError(
                    "template-invalid",
                    "container %s must use the frozen {{IMAGE}}" % container.get("name"))
            security = container.get("securityContext") or {}
            if security.get("runAsNonRoot") is not True:
                raise ProvisionerError("template-invalid",
                                       "%s.runAsNonRoot must be true" % container["name"])
            if security.get("allowPrivilegeEscalation") is not False:
                raise ProvisionerError("template-invalid",
                                       "%s.allowPrivilegeEscalation must be false"
                                       % container["name"])
            if security.get("privileged") is True:
                raise ProvisionerError("template-invalid",
                                       "%s.privileged is forbidden" % container["name"])
            if security.get("readOnlyRootFilesystem") is not True:
                raise ProvisionerError("template-invalid",
                                       "%s.readOnlyRootFilesystem must be true"
                                       % container["name"])
            if "ALL" not in ((security.get("capabilities") or {}).get("drop") or []):
                raise ProvisionerError("template-invalid",
                                       "%s must drop ALL capabilities" % container["name"])
            resources = (container.get("resources") or {}).get("limits") or {}
            if not resources.get("cpu") or not resources.get("memory"):
                raise ProvisionerError("template-invalid",
                                       "%s needs cpu and memory limits"
                                       % container["name"])
        child = next(c for c in containers if c.get("name") == "child")
        if (child.get("securityContext") or {}).get("runAsUser") != "{{UID}}":
            raise ProvisionerError("template-invalid",
                                   "child.runAsUser must be {{UID}}")
        envs = {entry.get("name"): entry for entry in (child.get("env") or [])
                if isinstance(entry, dict)}
        uid_env = envs.get("AMEESH_POD_UID") or {}
        if ((uid_env.get("valueFrom") or {}).get("fieldRef") or {}).get("fieldPath") \
                != "metadata.uid":
            raise ProvisionerError(
                "template-invalid",
                "child needs AMEESH_POD_UID from metadata.uid (pod-host-unknown)")
        credential = envs.get("{{CREDENTIAL_ENV}}") or {}
        secret_ref = (credential.get("valueFrom") or {}).get("secretKeyRef") or {}
        if secret_ref.get("name") != "{{CREDENTIAL_SECRET}}" or secret_ref.get("key") != "api-key":
            raise ProvisionerError(
                "template-invalid",
                "child credential env must read key api-key of {{CREDENTIAL_SECRET}}")

    def render(self, request: dict, *, image: str = DEFAULT_IMAGE,
               namespace: str = DEFAULT_NAMESPACE, dsn: str = "",
               cpu_request: str = "250m", cpu_limit: str = "2",
               memory_request: str = "512Mi", memory_limit: str = "2Gi",
               now: float | None = None) -> Rendered:
        now = time.time() if now is None else float(now)
        plan = tenancy.child_plan(
            request["host"], request["child"], request["tenant"],
            parent=request["parent"], harness=request["harness"],
            uid=request["uid"], image=image)
        pod_name = pod_name_for(request["id"])
        configmap = configmap_name_for(request["id"])
        left = max(0.0, float(request["expires_ts"]) - now)
        deadline = min(MAX_DEADLINE_S, max(60, int(math.ceil(left)) + DEADLINE_GRACE_S))
        mapping = {
            "POD_NAME": pod_name,
            "NAMESPACE": namespace,
            "HOST": request["host"],
            "PARENT": request["parent"],
            "CHILD": request["child"],
            "TENANT": request["tenant"],
            "TEMPLATE": request["template"],
            "REQUEST_ID": str(request["id"]),
            "UID": int(request["uid"]),
            "UNIX_USER": plan["unix_user"],
            "HOME": plan["home"],
            "CWD": plan["cwd"],
            "RUNNER_ID": plan["runner_id"],
            "CONFIGMAP": configmap,
            "CREDENTIAL_SECRET": request["credential_secret"],
            "CREDENTIAL_ENV": plan["credential_env"],
            "IMAGE": image,
            "ACTIVE_DEADLINE": deadline,
            "CPU_REQUEST": cpu_request,
            "CPU_LIMIT": cpu_limit,
            "MEMORY_REQUEST": memory_request,
            "MEMORY_LIMIT": memory_limit,
            "MEMORY_BYTES": _memory_bytes(memory_limit),
            "CPU_CORES": _cpu_cores(cpu_limit),
            "DSN": dsn,
            "EXPIRES_AT": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(float(request["expires_ts"]))),
        }
        pod = _replace(copy.deepcopy(self.raw), mapping)
        pod["metadata"]["name"] = pod_name
        pod["metadata"]["namespace"] = namespace
        labels = pod["metadata"].setdefault("labels", {})
        labels.update({
            "ameesh.dev/managed-by": "ameesh-pod-provisioner",
            "ameesh.dev/host": request["host"],
            "ameesh.dev/parent": request["parent"],
            "ameesh.dev/child": request["child"],
            "ameesh.dev/tenant": request["tenant"],
            "ameesh.dev/template": request["template"],
        })
        annotations = pod["metadata"].setdefault("annotations", {})
        annotations.update({
            "ameesh.dev/request-id": str(request["id"]),
            "ameesh.dev/fingerprint": request["fingerprint"],
            "ameesh.dev/expires-at": mapping["EXPIRES_AT"],
        })
        pod["spec"]["activeDeadlineSeconds"] = deadline
        config_body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": configmap,
                "namespace": namespace,
                "labels": {
                    "ameesh.dev/managed-by": "ameesh-pod-provisioner",
                    "ameesh.dev/host": request["host"],
                    "ameesh.dev/request-id": str(request["id"]),
                },
            },
            "data": {
                "passwd": plan["passwd"],
                "group": plan["group"],
                "config.json": json.dumps(plan["config"], sort_keys=True),
            },
        }
        self._validate_rendered(pod, mapping, request)
        return Rendered(pod=pod, configmap=config_body, pod_name=pod_name,
                        configmap_name=configmap)

    def _validate_rendered(self, pod: dict, mapping: dict, request: dict) -> None:
        for text in _strings(pod):
            if "{{" in text or "}}" in text:
                raise ProvisionerError("template-invalid",
                                       "unresolved placeholder in rendered pod")
        labels = (pod.get("metadata") or {}).get("labels") or {}
        if labels.get("ameesh.dev/managed-by") != "ameesh-pod-provisioner":
            raise ProvisionerError("template-invalid", "rendered pod has no manager label")
        if labels.get("ameesh.dev/host") != request["host"]:
            raise ProvisionerError("template-invalid", "rendered pod has the wrong host label")
        if (pod.get("metadata") or {}).get("name") != mapping["POD_NAME"]:
            raise ProvisionerError("template-invalid", "rendered pod has the wrong name")
        volumes = (pod.get("spec") or {}).get("volumes") or []
        for volume in volumes:
            if "hostPath" in volume or "persistentVolumeClaim" in volume:
                raise ProvisionerError("template-invalid",
                                       "rendered pod mounts a forbidden volume")


class HttpKubeClient:
    """Client HTTP minimal de l'API Kubernetes, sans dépendance externe.

    En cluster : le jeton du ServiceAccount et son CA. Jamais de redirection,
    jamais de TLS non vérifié, jamais le corps d'une erreur dans un message.
    """

    def __init__(self, server: str, token: str, ca_file: str | None,
                 namespace: str, *, timeout: float = 15.0):
        parsed = urllib.parse.urlsplit(server)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise KubeError("kube-config", "the Kubernetes server must be an HTTPS origin")
        if not token or "\n" in token or "\r" in token:
            raise KubeError("kube-config", "a Kubernetes bearer token is required")
        self.server = server.rstrip("/")
        self.token = token
        self.ca_file = ca_file
        self.namespace = namespace
        self.timeout = float(timeout)
        self.context = (ssl.create_default_context(cafile=ca_file)
                        if ca_file else ssl.create_default_context())

    @classmethod
    def from_env(cls) -> "HttpKubeClient":
        service = "/var/run/secrets/kubernetes.io/serviceaccount"
        token_file = os.environ.get("AMEESH_KUBE_TOKEN_FILE", service + "/token")
        ca_file = os.environ.get("AMEESH_KUBE_CA_FILE", service + "/ca.crt")
        namespace = (os.environ.get("AMEESH_POD_NAMESPACE")
                     or _read_text(service + "/namespace")
                     or DEFAULT_NAMESPACE).strip()
        server = os.environ.get("AMEESH_KUBE_SERVER", "https://kubernetes.default.svc")
        token = _read_text(token_file)
        if not token:
            raise KubeError("kube-config",
                            "no Kubernetes service account token at %s" % token_file)
        if not os.path.exists(ca_file):
            ca_file = None
        return cls(server, token.strip(), ca_file, namespace)

    def _request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.server + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self.context) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise KubeError("kube-response",
                                    "the Kubernetes response exceeds the limit")
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code == 409:
                raise KubeConflict("kube-conflict", "the Kubernetes object already exists") from None
            raise KubeError("kube-http-%d" % exc.code,
                            "Kubernetes refused the request (%s)" % exc.reason) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise KubeError("kube-unavailable",
                            "Kubernetes is unreachable (%s)" % exc.__class__.__name__) from None

    def _base(self) -> str:
        return "/api/v1/namespaces/%s" % urllib.parse.quote(self.namespace, safe="")

    def create_configmap(self, body: dict) -> dict:
        return self._request("POST", self._base() + "/configmaps", body) or {}

    def get_configmap(self, name: str) -> dict | None:
        return self._request(
            "GET", self._base() + "/configmaps/" + urllib.parse.quote(name, safe=""))

    def delete_configmap(self, name: str) -> bool:
        return self._request(
            "DELETE", self._base() + "/configmaps/" + urllib.parse.quote(name, safe="")) is not None

    def create_pod(self, body: dict) -> dict:
        return self._request("POST", self._base() + "/pods", body) or {}

    def get_pod(self, name: str) -> dict | None:
        return self._request(
            "GET", self._base() + "/pods/" + urllib.parse.quote(name, safe=""))

    def delete_pod(self, name: str) -> bool:
        return self._request(
            "DELETE", self._base() + "/pods/" + urllib.parse.quote(name, safe=""),
            {"propagationPolicy": "Background"}) is not None

    def list_pods(self, label_selector: str = "") -> list[dict]:
        path = self._base() + "/pods"
        if label_selector:
            path += "?" + urllib.parse.urlencode({"labelSelector": label_selector})
        body = self._request("GET", path) or {}
        return list(body.get("items") or [])


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except OSError:
        return ""


class Provisioner:
    """Un pourvoyeur : heartbeat du template, démarrage et nettoyage bornés."""

    def __init__(self, db, client, template: PodTemplate, *, image: str = DEFAULT_IMAGE,
                 dsn: str = "", actor: str = ACTOR, poll: float = 5.0):
        self.db = db
        self.client = client
        self.template = template
        self.image = image
        self.dsn = dsn
        self.actor = actor
        self.poll = max(1.0, float(poll))

    def heartbeat(self) -> dict:
        return storage.of(self.db).pod_provisioning.register_template(
            self.template.name, namespace=self.client.namespace, image=self.image,
            fingerprint=self.template.fingerprint, spec=self.template.raw,
            actor=self.actor)

    def start_once(self, *, now: float | None = None) -> dict | None:
        requests = storage.of(self.db).pod_provisioning
        request = requests.claim(self.actor)
        if request is None:
            return None
        rendered = None
        try:
            rendered = self.template.render(
                request, image=self.image, namespace=self.client.namespace,
                dsn=self.dsn, now=now)
            self._ensure_configmap(rendered)
            pod = self._ensure_pod(rendered, request)
            uid = ((pod.get("metadata") or {}).get("uid") or "").strip()
            if not uid:
                raise ProvisionerError("kube-response",
                                       "the created pod has no metadata.uid")
            started = requests.running(
                request["id"], pod_uid=uid, pod_name=rendered.pod_name,
                actor=self.actor)
            if started is None:
                self._delete_pod(rendered.pod_name)
                self._delete_configmap(rendered.configmap_name)
                requests.fail(request["id"],
                              "the child row disappeared or the request was no "
                              "longer claimable before the pod started")
                return None
            return started
        except Exception as exc:  # noqa: BLE001 - une demande ne bloque pas la boucle
            if rendered is not None:
                self._delete_pod(rendered.pod_name)
                self._delete_configmap(rendered.configmap_name)
            requests.fail(request["id"], _short_error(exc))
            return None

    def _ensure_configmap(self, rendered: Rendered) -> None:
        try:
            self.client.create_configmap(rendered.configmap)
            return
        except KubeConflict:
            existing = self.client.get_configmap(rendered.configmap_name)
            if existing is not None:
                if (existing.get("data") or {}) == ((rendered.configmap.get("data")) or {}):
                    return
                self.client.delete_configmap(rendered.configmap_name)
        self.client.create_configmap(rendered.configmap)

    def _ensure_pod(self, rendered: Rendered, request: dict) -> dict:
        try:
            pod = self.client.create_pod(rendered.pod)
        except KubeConflict:
            existing = self.client.get_pod(rendered.pod_name)
            if existing is None:
                raise
            labels = (existing.get("metadata") or {}).get("labels") or {}
            if (labels.get("ameesh.dev/host") != request["host"]
                    or labels.get("ameesh.dev/parent") != request["parent"]
                    or labels.get("ameesh.dev/request-id") != str(request["id"])):
                raise ProvisionerError(
                    "pod-name-conflict",
                    "a pod of another parent already uses %s" % rendered.pod_name)
            return existing
        return pod or {}

    def _delete_pod(self, name: str) -> None:
        try:
            self.client.delete_pod(name)
        except KubeError:
            pass

    def _delete_configmap(self, name: str) -> None:
        try:
            self.client.delete_configmap(name)
        except KubeError:
            pass

    def cleanup_once(self, *, limit: int = 32) -> list[dict]:
        requests = storage.of(self.db).pod_provisioning
        removed = []
        for request in requests.cleanup_candidates(limit=limit):
            pod_name = request.get("pod_name") or pod_name_for(request["id"])
            pod = self.client.get_pod(pod_name)
            if pod is not None:
                labels = (pod.get("metadata") or {}).get("labels") or {}
                if (labels.get("ameesh.dev/host") != request["host"]
                        or labels.get("ameesh.dev/parent") != request["parent"]):
                    requests.fail(
                        request["id"],
                        "pod-owner-mismatch: refusing to delete a pod of another parent")
                    continue
                self.client.delete_pod(pod_name)
            self.client.delete_configmap(configmap_name_for(request["id"]))
            expired = (request.get("state") == "expired"
                       or float(request.get("expires_ts") or 0) <= time.time())
            requests.cleanup(request["host"])
            removed.append({"child": request["child"], "host": request["host"],
                            "state": "expired" if expired else "deleted"})
        return removed

    def run_once(self) -> dict:
        template = self.heartbeat()
        reclaimed = storage.of(self.db).pod_provisioning.reclaim_stale()
        started = self.start_once()
        removed = self.cleanup_once()
        return {"template": template.get("name"), "reclaimed": reclaimed,
                "started": (started or {}).get("child"),
                "removed": removed}

    def run_forever(self, *, stop=None) -> None:
        stopping = False

        def _stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        while not stopping:
            self.run_once()
            deadline = time.monotonic() + self.poll
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh pod-provisioner")
    parser.add_argument("--once", action="store_true",
                        help="one heartbeat/start/cleanup pass, then exit")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--template", default=os.environ.get(
        "AMEESH_POD_TEMPLATE", DEFAULT_TEMPLATE))
    parser.add_argument("--image", default=os.environ.get("AMEESH_POD_IMAGE", DEFAULT_IMAGE))
    parser.add_argument("--namespace", default="")
    parser.add_argument("--actor", default=ACTOR)
    parser.add_argument("--dsn", default="")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--templates", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load()
        db = db_mod.connect(cfg)
        if args.templates:
            rows = db.query(
                "SELECT name, namespace, image, fingerprint, active,"
                " extract(epoch from last_seen_at)::float8 AS seen_ts"
                " FROM pod_templates ORDER BY name")
            print(json.dumps([dict(row) for row in rows], sort_keys=True))
            return 0
        client = HttpKubeClient.from_env()
        if args.namespace:
            client.namespace = args.namespace
        template = PodTemplate.load(args.template, name=tenancy.POD_CHILD_TEMPLATE)
        raw_dsn = args.dsn or cfg.dsn
        _password, clean_dsn = dsn_mod.split_password(raw_dsn)
        provisioner = Provisioner(
            db, client, template, image=args.image, dsn=clean_dsn,
            actor=args.actor, poll=args.poll)
        if args.cleanup_only:
            print(json.dumps({"removed": provisioner.cleanup_once()}, sort_keys=True))
            return 0
        if args.once:
            print(json.dumps(provisioner.run_once(), sort_keys=True))
            return 0
        provisioner.run_forever()
        return 0
    except (ProvisionerError, db_mod.Unavailable) as exc:
        print("ameesh pod-provisioner: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
