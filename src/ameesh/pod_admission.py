# SPDX-License-Identifier: AGPL-3.0-only
"""#7697 (P4 de #7633) : admission des pods enfants, sur une autorité externe.

Le B du gel #44541 (ameesh-cx2) : la VAP dérivait le Secret du label
`ameesh.dev/child`, mais ce label est choisi par le même ServiceAccount que
celui qui crée le pod. Un SA compromis pouvait donc prendre le label d'un
autre enfant et monter `ameesh-child-<label>`. Une VAP statique ne peut pas
lire `pod_requests` ; ce webhook le fait.

Le webhook est une autorité **indépendante du pourvoyeur** :

- il lit la demande `pod_requests` écrite par le `spawn` (état, enfant,
  parent, tenant, UID, runner, Secret, nom, expiration) ;
- il rend le pod attendu avec le template **copié dans son image**, et
  refuse tout écart de forme (image, commandes, env, volumes, montages,
  contexte de sécurité, labels/annotations) ;
- il vérifie aussi la ConfigMap de l'enfant (passwd/group/config) contre le
  plan de `tenancy.child_plan`, pour qu'un SA compromis ne puisse pas y
  glisser un proxy ou une configuration d'exfiltration.

Le webhook est `failurePolicy: Fail` : sans lui, aucun pod enfant n'est
admis. La VAP reste la seconde barrière, utile si le webhook est retiré par
erreur. Aucun secret n'est lu : le webhook compare des noms et des formes.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as config_mod
from . import db as db_mod
from . import dsn as dsn_mod
from . import pod_provisioner, storage, tenancy

ACTOR = pod_provisioner.ACTOR
PROVISIONER_LABEL = "ameesh-pod-provisioner"
PROVISIONER_SA = "ameesh-pod-provisioner"
MANAGED_BY = "ameesh.dev/managed-by"
REQUEST_ID = "ameesh.dev/request-id"
MAX_BODY_BYTES = 4 * 1024 * 1024

class AdmissionError(RuntimeError):
    """Refus d'admission : code stable, message sans secret."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


def _allow(message: str = "") -> dict:
    return {"allowed": True, "message": message}


def _deny(message: str) -> dict:
    return {"allowed": False, "message": message}


def provisioner_username(namespace: str) -> str:
    """Identité que l'API server donne au ServiceAccount du pourvoyeur.

    `userInfo.username` vaut `system:serviceaccount:<namespace>:<nom>` ; le
    nom du SA n'est pas l'acteur de base de données `ACTOR`, qui reste
    `system:ameesh-pod-provisioner` dans `pod_requests.claimed_by`.
    """
    return "system:serviceaccount:%s:%s" % (namespace, PROVISIONER_SA)


def _join(path: str, key: str) -> str:
    return key if not path else path + "." + key


def _allowed_extra(path: str, key: str, value) -> bool:
    """Défauts ajoutés par l'API Kubernetes, sans effet sur le confinement.

    Tout autre champ supplémentaire est refusé : la liste est volontairement
    fermée, pour qu'un futur champ (sidecar, hostUsers, etc.) ne passe pas
    par omission.
    """
    if path == "metadata":
        # `uid`, `creationTimestamp`, `resourceVersion`, `generation` et
        # `managedFields` sont posés par l'API server AVANT l'admission ; le
        # pourvoyeur ne les choisit pas. Les autres champs libres restent
        # fermés.
        return ((key in ("uid", "creationTimestamp", "resourceVersion",
                         "generation", "managedFields", "selfLink"))
                or (key == "generateName" and not value)
                or (key in ("deletionTimestamp", "deletionGracePeriodSeconds")
                    and value is None)
                or (key in ("finalizers", "ownerReferences") and value == []))
    if path == "spec":
        return ((key == "dnsPolicy" and value == "ClusterFirst")
                or (key == "schedulerName" and value == "default-scheduler")
                or (key == "enableServiceLinks" and value is True)
                or (key == "preemptionPolicy"
                    and value == "PreemptLowerPriority")
                or (key == "priority" and value == 0)
                or (key == "serviceAccount"
                    and value == "ameesh-child")
                or (key in ("hostNetwork", "hostPID", "hostIPC",
                            "shareProcessNamespace", "setHostnameAsFQDN")
                    and value is False)
                or (key == "hostUsers" and value in (True, False))
                or (key in ("hostname", "subdomain", "nodeName")
                    and not value)
                or (key in ("initContainers", "ephemeralContainers",
                            "imagePullSecrets", "tolerations",
                            "topologySpreadConstraints", "readinessGates",
                            "schedulingGates", "resourceClaims")
                    and value == [])
                or (key == "overhead" and value == {})
                or (key == "os" and value == {"name": "linux"}))
    if path.startswith("spec.containers[") and path.endswith("]"):
        return ((key == "terminationMessagePath"
                 and value == "/dev/termination-log")
                or (key == "terminationMessagePolicy" and value == "File")
                or (key == "imagePullPolicy" and value == "IfNotPresent")
                or (key in ("stdin", "tty", "stdinOnce") and value is False)
                or (key in ("ports", "envFrom") and value == []))
    if path.endswith(".securityContext"):
        if key == "procMount" and value == "Default":
            return True
        # `capabilities.add` vide est le défaut du serveur.
        return key == "capabilities" and value == {"add": []}
    if path.endswith(".capabilities"):
        return key == "add" and value == []
    if path.endswith(".resources"):
        return key == "claims" and value == []
    if path.endswith(".configMap"):
        return ((key == "defaultMode" and value == 420)
                or (key == "optional" and value is False))
    if path.endswith(".emptyDir"):
        return key == "medium" and value in ("", None)
    if path.endswith(".fieldRef"):
        return key == "apiVersion" and value == "v1"
    if path.endswith(".secretKeyRef"):
        return key == "optional" and value is False
    return False


def _compare(submitted, expected, path: str, problems: list[str]) -> None:
    if isinstance(expected, dict):
        if not isinstance(submitted, dict):
            problems.append(path + ": not a mapping")
            return
        for key, value in expected.items():
            child = _join(path, key)
            if key not in submitted:
                problems.append(child + ": missing")
                continue
            _compare(submitted[key], value, child, problems)
        for key, value in submitted.items():
            if key in expected:
                continue
            if _allowed_extra(path, key, value):
                continue
            problems.append(_join(path, key) + ": unexpected")
    elif isinstance(expected, list):
        if not isinstance(submitted, list):
            problems.append(path + ": not a list")
            return
        if len(submitted) != len(expected):
            problems.append(path + ": length %d != %d"
                            % (len(submitted), len(expected)))
        for index, (left, right) in enumerate(zip(submitted, expected)):
            _compare(left, right, "%s[%d]" % (path, index), problems)
    else:
        if submitted != expected:
            problems.append(path + ": differs")


def pod_projection(pod: dict) -> dict:
    """Vue de sécurité du pod : tout ce qui peut lire un Secret ou exécuter
    du code. Les défauts du serveur sont ajoutés par `_allowed_extra`."""
    metadata = dict(pod.get("metadata") or {})
    spec = dict(pod.get("spec") or {})
    spec["containers"] = [
        dict(container) if isinstance(container, dict) else container
        for container in (spec.get("containers") or [])]
    spec["volumes"] = [
        dict(volume) if isinstance(volume, dict) else volume
        for volume in (spec.get("volumes") or [])]
    return {"metadata": metadata, "spec": spec}


def compare_pod(submitted: dict, expected: dict) -> str | None:
    """Premier écart de forme, ou None. Ne renvoie jamais de valeur de Secret."""
    problems: list[str] = []
    _compare(pod_projection(submitted), pod_projection(expected), "", problems)
    return problems[0] if problems else None


def _valid_request_id(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _response(review: dict, decision: dict) -> dict:
    request = review.get("request") or {}
    response = {
        "uid": request.get("uid", ""),
        "allowed": bool(decision.get("allowed")),
    }
    if decision.get("message"):
        response["status"] = {"message": str(decision["message"])[:900]}
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": response,
    }


class PodAdmission:
    """Décision d'admission : demande en base + template local figé."""

    def __init__(self, db, template: pod_provisioner.PodTemplate, *,
                 client=None, image: str = pod_provisioner.DEFAULT_IMAGE,
                 namespace: str = pod_provisioner.DEFAULT_NAMESPACE,
                 dsn: str = "", now: float | None = None, requests=None,
                 provisioner_user: str | None = None):
        self.db = db
        self.template = template
        self.client = client
        self.image = image
        self.namespace = namespace
        self.dsn = dsn
        self._requests = requests
        self._now_value = now
        self.provisioner_user = (
            provisioner_user or provisioner_username(namespace))

    def _now_ts(self) -> float:
        return time.time() if self._now_value is None else float(self._now_value)

    def _row(self, request_id: str) -> dict | None:
        requests = self._requests or storage.of(self.db).pod_provisioning
        return requests.get_by_id(request_id)

    def _row_problem(self, row: dict, *, request_id: str) -> str | None:
        if row is None:
            return "unknown pod request"
        if row.get("template") != self.template.name:
            return "pod request uses another template"
        if row.get("namespace") != self.namespace:
            return "pod request uses another namespace"
        if row.get("state") not in ("claimed", "running"):
            return "pod request is not claimable (state %s)" % row.get("state")
        if row.get("claimed_by") != ACTOR:
            return "pod request was not claimed by the provisioner"
        if float(row.get("expires_ts") or 0) <= self._now_ts():
            return "pod request has expired"
        if row.get("pod_name") and row["pod_name"] != pod_provisioner.pod_name_for(
                request_id):
            return "pod request carries another pod name"
        return None

    def _expected(self, row: dict) -> pod_provisioner.Rendered:
        return self.template.render(
            row, image=self.image, namespace=self.namespace, dsn=self.dsn,
            now=self._now_ts())

    def _configmap_problem(self, rendered: pod_provisioner.Rendered,
                           request_id: str) -> str | None:
        if self.client is None:
            return None
        existing = self.client.get_configmap(rendered.configmap_name)
        if existing is None:
            return "child ConfigMap is missing"
        labels = (existing.get("metadata") or {}).get("labels") or {}
        if labels.get(MANAGED_BY) != PROVISIONER_LABEL:
            return "child ConfigMap is not managed by the provisioner"
        if labels.get(REQUEST_ID) != str(request_id):
            return "child ConfigMap belongs to another request"
        if (existing.get("data") or {}) != (rendered.configmap.get("data") or {}):
            return "child ConfigMap data differs from the child plan"
        return None

    def check(self, review: dict) -> dict:
        request = review.get("request") or {}
        if request.get("operation") not in ("CREATE", "UPDATE"):
            return _allow("not an admission of a child object")
        user = (request.get("userInfo") or {}).get("username") or ""
        if user != self.provisioner_user:
            # Refus par défaut : un objet étiqueté `managed-by` n'est admis
            # que pour le ServiceAccount du pourvoyeur. Le webhook ne vise
            # que ces objets (objectSelector), mais un autre auteur ne doit
            # pas pouvoir se glisser dans le périmètre. Le nom est dérivé du
            # namespace et configurable si le SA est renommé.
            return _deny("child object is not created by the pod provisioner")
        resource = (request.get("resource") or {}).get("resource", "")
        if resource == "configmaps":
            return self._check_configmap(request)
        if resource != "pods":
            return _allow("not a pod or ConfigMap")
        return self._check_pod(request)

    def _check_configmap(self, request: dict) -> dict:
        obj = request.get("object") or {}
        metadata = obj.get("metadata") or {}
        labels = metadata.get("labels") or {}
        request_id = str(labels.get(REQUEST_ID) or "")
        if not _valid_request_id(request_id):
            return _deny("child ConfigMap has no valid request-id label")
        row = self._row(request_id)
        problem = self._row_problem(row, request_id=request_id)
        if problem:
            return _deny(problem)
        if metadata.get("name") != pod_provisioner.configmap_name_for(request_id):
            return _deny("child ConfigMap has the wrong name")
        if metadata.get("namespace") != row["namespace"]:
            return _deny("child ConfigMap has the wrong namespace")
        rendered = self._expected(row)
        if labels.get(MANAGED_BY) != PROVISIONER_LABEL:
            return _deny("child ConfigMap is not managed by the provisioner")
        if labels.get("ameesh.dev/host") != row["host"]:
            return _deny("child ConfigMap has the wrong host label")
        if (obj.get("data") or {}) != (rendered.configmap.get("data") or {}):
            return _deny("child ConfigMap data differs from the child plan")
        return _allow("child ConfigMap matches its pod request")

    def _check_pod(self, request: dict) -> dict:
        obj = request.get("object") or {}
        metadata = obj.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        request_id = str(annotations.get(REQUEST_ID) or "")
        if not _valid_request_id(request_id):
            return _deny("child pod has no valid request-id annotation")
        row = self._row(request_id)
        problem = self._row_problem(row, request_id=request_id)
        if problem:
            return _deny(problem)
        if metadata.get("name") != pod_provisioner.pod_name_for(request_id):
            return _deny("child pod has the wrong name")
        if metadata.get("namespace") != row["namespace"]:
            return _deny("child pod has the wrong namespace")
        if request.get("namespace") != row["namespace"]:
            return _deny("child pod is created in the wrong namespace")
        rendered = self._expected(row)
        problem = compare_pod(obj, rendered.pod)
        if problem:
            return _deny("child pod shape: " + problem)
        problem = self._configmap_problem(rendered, request_id)
        if problem:
            return _deny(problem)
        return _allow("child pod matches its pod request")


def _read_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > MAX_BODY_BYTES:
        raise ValueError("invalid Content-Length")
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("the AdmissionReview is not a mapping")
    return data


class _Handler(BaseHTTPRequestHandler):
    server_version = "ameesh-pod-admission/1"
    sys_version = ""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - API BaseHTTPRequestHandler
        if self.path in ("/healthz", "/readyz"):
            self._send(200, b"ok\n", "text/plain")
            return
        self._send(404, b"not found\n", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - API BaseHTTPRequestHandler
        if self.path != "/validate":
            self._send(404, b"not found\n", "text/plain")
            return
        try:
            review = _read_body(self)
            decision = self.server.admission.check(review)
            payload = _response(review, decision)
        except Exception as exc:  # noqa: BLE001 - refus fail-closed
            # Ne jamais répondre 200/allowed sur une erreur interne : le
            # kubelet doit refuser. Le message reste borné et sans secret.
            review = {"request": {}}
            payload = _response(review, _deny(
                "admission error: %s" % exc.__class__.__name__))
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send(200, body, "application/json")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("ameesh-pod-admission: %s\n" % (fmt % args))


class AdmissionServer:
    def __init__(self, admission: PodAdmission, *, cert: str, key: str,
                 host: str = "0.0.0.0", port: int = 8443):
        self.httpd = ThreadingHTTPServer((host, int(port)), _Handler)
        self.httpd.admission = admission
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.httpd.socket = context.wrap_socket(self.httpd.socket,
                                                server_side=True)

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def close(self) -> None:
        self.httpd.server_close()


def set_ca(webhook_name: str, ca_file: str, *,
           client: pod_provisioner.HttpKubeClient | None = None) -> dict:
    """Pose le `caBundle` du webhook (geste d'initialisation du pod)."""
    path = Path(ca_file)
    try:
        ca = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise AdmissionError("ca-unreadable", "cannot read %s: %s" % (path, exc))
    if not ca:
        raise AdmissionError("ca-empty", "the CA bundle is empty")
    client = client or pod_provisioner.HttpKubeClient.from_env()
    current = client.get_validating_webhook_config(webhook_name)
    if current is None:
        raise AdmissionError("webhook-unknown",
                             "no ValidatingWebhookConfiguration %s" % webhook_name)
    hooks = current.get("webhooks") or []
    if not hooks:
        raise AdmissionError("webhook-empty", "the webhook has no entry")
    for hook in hooks:
        hook.setdefault("clientConfig", {})["caBundle"] = ca
    client.update_validating_webhook_config(webhook_name, current)
    return {"webhook": webhook_name, "webhooks": len(hooks)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh pod-admission")
    parser.add_argument("--template", default=os.environ.get(
        "AMEESH_POD_TEMPLATE", pod_provisioner.DEFAULT_TEMPLATE))
    parser.add_argument("--image", default=os.environ.get(
        "AMEESH_POD_IMAGE", pod_provisioner.DEFAULT_IMAGE))
    parser.add_argument("--namespace", default=os.environ.get(
        "AMEESH_POD_NAMESPACE", pod_provisioner.DEFAULT_NAMESPACE))
    parser.add_argument("--provisioner-user", default=os.environ.get(
        "AMEESH_POD_PROVISIONER_USER") or None,
        help="userInfo.username du SA pourvoyeur (défaut : "
             "system:serviceaccount:<namespace>:ameesh-pod-provisioner)")
    parser.add_argument("--dsn", default="")
    parser.add_argument("--cert", default="/tls/tls.crt")
    parser.add_argument("--key", default="/tls/tls.key")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--set-ca", action="store_true",
                        help="patch the webhook caBundle and exit")
    parser.add_argument("--webhook", default="ameesh-child-pods")
    parser.add_argument("--ca-file", default="/tls/tls.crt")
    args = parser.parse_args(argv)

    try:
        if args.set_ca:
            print(json.dumps(set_ca(args.webhook, args.ca_file), sort_keys=True))
            return 0
        # Le webhook ne fait aucun effet de bord (sideEffects: None) : pas
        # d'enregistrement de credentials au chargement de la configuration.
        cfg = config_mod.load(register_credentials=False)
        # `writer=False` : la source d'autorisation est lue, jamais écrite.
        db = db_mod.connect(cfg, writer=False)
        template = pod_provisioner.PodTemplate.load(
            args.template, name=tenancy.POD_CHILD_TEMPLATE)
        raw_dsn = args.dsn or cfg.dsn
        _password, clean_dsn = dsn_mod.split_password(raw_dsn)
        client = pod_provisioner.HttpKubeClient.from_env()
        # Les ConfigMaps enfants vivent dans le namespace des pods, pas
        # forcément dans celui du webhook.
        client.namespace = args.namespace
        admission = PodAdmission(
            db, template, client=client, image=args.image,
            namespace=args.namespace, dsn=clean_dsn,
            provisioner_user=args.provisioner_user)
        server = AdmissionServer(admission, cert=args.cert, key=args.key,
                                 host=args.listen, port=args.port)
        print("ameesh pod-admission: listening on %s:%d" % (args.listen, args.port),
              file=sys.stderr)
        server.serve_forever()
        return 0
    except (AdmissionError, pod_provisioner.ProvisionerError,
            db_mod.Unavailable) as exc:
        print("ameesh pod-admission: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
