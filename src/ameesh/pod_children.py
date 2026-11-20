# SPDX-License-Identifier: AGPL-3.0-only
"""#7697 (P4 de #7633) : demande et démarrage borné d'un enfant porté par un pod.

Design `docs/design/7633-hote-avatars-pod.md` §8 : le parent ne parle jamais à
l'API Kubernetes. Il écrit `pod_requests` (dans la transaction de
`Ephemerals.create`), le pourvoyeur crée le pod, puis le `spawn` attend le
premier battement de l'enfant OU un état d'échec, pendant une borne (120 s).
Au-delà, la demande est annulée et l'enfant nettoyé : le parent ne bloque pas
sur le travail de l'enfant au-delà de ce démarrage.

Aucun secret ici : le nom du Secret d'organisation par enfant est écrit dans
la demande ; le pourvoyeur le monte sans le lire.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import storage, tenancy

#: borne du démarrage : premier battement de l'enfant ou état d'échec.
WAIT_S = 120.0
#: intervalle de lecture de la demande pendant l'attente.
POLL_S = 0.5

SPAWN_POD_UNSUPPORTED = "spawn-pod-unsupported"
SPAWN_POD_CREDENTIAL = "spawn-pod-credential"
SPAWN_POD_START_TIMEOUT = "spawn-pod-start-timeout"
SPAWN_POD_FAILED = "spawn-pod-failed"
SPAWN_POD_REQUEST_MISSING = "spawn-pod-request-missing"


class PodError(RuntimeError):
    """Refus d'un enfant pod : `code` stable, message autosuffisant."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


def ready(db):
    """(template, "") si un pourvoyeur bat avec un template actif, sinon
    (None, raison). C'est la condition qui fait disparaître
    `spawn-pod-unsupported` (design §8.5)."""
    row = storage.of(db).pod_provisioning.ready()
    if row is None:
        return None, (
            "no live pod provisioner/template: deploy deploy/k8s/pod-provisioner "
            "and check its heartbeat (pod_templates.last_seen_at)")
    return row, ""


def fingerprint(request: dict) -> str:
    """Empreinte de la demande : ce que le pourvoyeur peut re-vérifier."""
    payload = {key: request.get(key) for key in (
        "child", "parent", "tenant", "host", "template", "namespace", "harness",
        "credential_secret", "uid", "unix_user", "runner_id", "profile",
        "requested_by", "expires_ts")}
    # `timestamptz` garde la microseconde : l'aller-retour base arrondit le
    # `time.time()` d'origine, l'empreinte doit être stable des deux côtés.
    if payload.get("expires_ts") is not None:
        payload["expires_ts"] = round(float(payload["expires_ts"]), 6)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def request_fields(placement, *, ttl_seconds: float, tools=None, mcp=None,
                   context=None) -> dict:
    """Champs de `pod_requests` pour un `ChildPlacement` pod.

    `placement` porte le template, le namespace, l'hôte, l'UID, l'utilisateur,
    le runner et le nom du Secret ; le profil ne contient que des noms et un
    digest de contexte (jamais le texte)."""
    if getattr(placement, "runtime", "") != "pod":
        raise PodError(SPAWN_POD_UNSUPPORTED,
                       "placement is not a pod child placement")
    profile = {
        "harness": placement.evaluated_profile.get("harness"),
        "provider": placement.evaluated_profile.get("provider"),
        "model": placement.evaluated_profile.get("model"),
        "tools": list(tools or ()),
        "mcp": list(mcp or ()),
        "context_digest": (context or {}).get("digest"),
    }
    fields = {
        "child": getattr(placement, "child", None),
        "parent": getattr(placement, "parent", None),
        "tenant": placement.tenant,
        "host": placement.host,
        "template": placement.template,
        "namespace": placement.namespace,
        "harness": placement.evaluated_profile.get("harness"),
        "credential_secret": placement.credential_secret,
        "uid": int(placement.uid),
        "unix_user": placement.unix_user,
        "runner_id": placement.runner_id,
        "profile": profile,
        "requested_by": getattr(placement, "parent", None),
        "expires_ts": time.time() + float(ttl_seconds),
    }
    fields["fingerprint"] = fingerprint(fields)
    return fields


def wait_started(db, child: str, *, timeout_s: float | None = None):
    """Attend le premier battement de l'enfant ou un état d'échec.

    Rend la demande `running` (avec `pod_uid`) quand `runner_presence` porte le
    UID du pod provisionné. Sur échec ou dépassement, annule la demande,
    nettoie l'enfant et lève `PodError` (`spawn-pod-failed` /
    `spawn-pod-start-timeout`)."""
    limit = WAIT_S if timeout_s is None else max(0.0, float(timeout_s))
    if limit <= 0:
        return None
    deadline = time.monotonic() + limit
    while True:
        request = storage.of(db).pod_provisioning.get(child)
        if request is None:
            raise PodError(
                SPAWN_POD_REQUEST_MISSING,
                "pod request for %s disappeared before it could start" % child)
        state = request.get("state")
        if state in ("failed", "cancelled", "expired", "deleted"):
            cancel(db, child, "request is %s" % state)
            raise PodError(
                SPAWN_POD_FAILED,
                "child %s did not start: request is %s (%s)"
                % (child, state, (request.get("error") or "no detail")))
        if state == "running":
            host_row = storage.of(db).pod_hosts.get(request["host"])
            presence = storage.of(db).tenancy.presence(
                request["host"], request["tenant"])
            if (host_row is not None and presence is not None
                    and host_row.get("pod_uid") == presence.get("pod_uid")
                    and float(presence.get("seen_ts") or 0)
                    >= float(request.get("requested_ts") or 0)):
                started = dict(request)
                started["pod_uid"] = host_row["pod_uid"]
                return started
        left = deadline - time.monotonic()
        if left <= 0:
            cancel(db, child, "child did not beat within %.0fs" % limit)
            raise PodError(
                SPAWN_POD_START_TIMEOUT,
                "child %s did not start within %.0fs: request cancelled and the "
                "provisioner will delete the pod" % (child, limit))
        time.sleep(min(POLL_S, max(0.01, left)))


def cancel(db, child: str, reason: str) -> dict:
    """Annule la demande et nettoie la ligne de l'enfant (idempotent)."""
    cancelled = storage.of(db).pod_provisioning.cancel(child, reason)
    discarded = storage.of(db).ephemerals.discard(child)
    return {"cancelled": bool(cancelled), "child": discarded}
