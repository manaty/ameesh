# SPDX-License-Identifier: AGPL-3.0-only
"""#7698 (P5 de #7633) : préflight pod d'un déplacement d'agent.

Design `docs/design/7633-hote-avatars-pod.md` §9.2–§9.3. Un hôte pod est une
cible normale de `ameesh agent move`, avec des gardes qui n'ont de sens que
pour lui :

* la cible n'est pas un hôte enfant éphémère (`pod_hosts`) : un enfant a un
  UID neuf et un TTL, jamais une cible de déplacement ;
* sa carte Host est un `runtime.kind: pod` (jamais un `pod-template`) et son
  `account_isolation` est connu — la carte est la preuve statique du modèle ;
* le conteneur du tenant est prêt : liaison `host_tenants` au passwd du plan,
  UID de pod acceptable, présence fraîche (le `candidate_state` de la cible
  vérifie le couple superviseur + battement du tenant, #7698) et profil de
  compte publié (gardes de #7108, dans `relocation`) ;
* le PVC n'est ré-attachable que si la même personne garde le même UID sur
  les deux hôtes pod (D8) ; sinon le repli est le re-login sur un PVC neuf
  (D7). Le plan le dit (`pvc: reattach|relogin|none`) et l'événement
  `agent.moved` le conserve.

Aucun fichier de session ni d'identifiant n'est lu, copié ou déplacé ici : ce
module ne lit que des noms, des UID et des références de carte.
"""
from __future__ import annotations

from . import canon as canon_mod
from . import storage, tenancy

#: la cible est un pod enfant éphémère (`pod_hosts`), jamais une cible.
MOVE_POD_CHILD_TARGET = "move-pod-child-target"
#: la cible déclare `runtime.kind: pod-template` (elle provisionne des enfants).
MOVE_POD_TEMPLATE = "move-pod-template"
#: le runtime de la cible est illisible, absent alors que l'hôte est un pod, ou
#: son `account_isolation` n'est pas connu.
MOVE_POD_RUNTIME = "move-pod-runtime"
MOVE_POD_ISOLATION = "move-pod-isolation"
#: la liaison du tenant manque, son passwd ne suit pas le plan, ou son UID
#: n'est pas un UID de pod acceptable.
MOVE_TENANT_MISSING = "move-tenant-missing"
MOVE_POD_PASSWD = "move-pod-passwd"
MOVE_POD_UID = "move-pod-uid"

#: valeurs de `account_isolation` admises pour une cible de déplacement.
ACCOUNT_ISOLATION = ("per-tenant-volume", "per-pod-empty-dir")
#: état du PVC dans le plan : ré-attachable, re-login requis, ou aucun volume
#: de pod à déplacer (la source n'est pas un pod).
PVC_REATTACH = "reattach"
PVC_RELOGIN = "relogin"
PVC_NONE = "none"


class PodMoveError(ValueError):
    """Refus pod d'un déplacement : `code` stable, message actionnable."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


def _runtime(cfg, host: str):
    try:
        return tenancy.pod_runtime(cfg, host)
    except tenancy.TenancyError as exc:
        raise PodMoveError(MOVE_POD_RUNTIME, str(exc)) from exc


def _binding(db, host: str, tenant: str):
    for bound in storage.of(db).tenancy.bindings(host):
        if bound.get("tenant") == tenant:
            return bound
    return None


def _pod_identity(cfg, db, host: str, tenant: str):
    """`(kind, uid)` de la personne sur `host` : ("pod", uid) si un hôte pod,
    ("host", None) sinon. Ne lève pas : une source dont la carte est illisible
    ne bloque pas le préflight de la cible."""
    if not host:
        return "host", None
    child = storage.of(db).pod_hosts.get(host)
    if child is not None:
        return "pod", int(child.get("uid") or 0)
    try:
        runtime = tenancy.pod_runtime(cfg, host)
    except tenancy.TenancyError:
        return "unknown", None
    if runtime is None or runtime.kind != "pod":
        return "host", None
    bound = _binding(db, host, tenant)
    return "pod", int(bound["uid"]) if bound else None


def preflight(cfg, db, row, target: str) -> dict:
    """Plan pod du déplacement de `row` vers `target`, ou `{}` si la cible
    n'est pas un pod (les gardes de #7108 restent seules).

    Lève `PodMoveError` (code stable) avant tout effet. Ne vérifie ni la
    présence, ni le profil de compte, ni le tour en cours : ces gardes sont
    celles de `relocation` et restent valables pour tout hôte."""
    child = storage.of(db).pod_hosts.get(target)
    if child is not None:
        raise PodMoveError(
            MOVE_POD_CHILD_TARGET,
            "%s is an ephemeral child pod host (%s, parent %s, tenant %s): a "
            "child host is never a move target (design 7633 §9.2)"
            % (target, child.get("state") or "?", child.get("parent") or "?",
               child.get("tenant") or "?"))
    runtime = _runtime(cfg, target)
    if runtime is None:
        # Un hôte qui a déjà tenu un bail de superviseur est un pod dont la
        # carte a disparu : ne pas le traiter comme un hôte classique.
        if storage.of(db).pod_supervisor.lease(target) is not None:
            raise PodMoveError(
                MOVE_POD_RUNTIME,
                "%s has a supervisor lease history but no Host card in the "
                "canon: sync its Host card (runtime.kind: pod) before moving "
                "an agent onto it" % target)
        return {}
    if runtime.kind != "pod":
        raise PodMoveError(
            MOVE_POD_TEMPLATE,
            "%s declares runtime.kind=%r, not `pod`: a pod template only "
            "provisions ephemeral child pods and is never a move target"
            % (target, runtime.kind))
    isolation = str(getattr(runtime, "account_isolation", "") or "")
    if isolation not in ACCOUNT_ISOLATION:
        raise PodMoveError(
            MOVE_POD_ISOLATION,
            "%s declares account_isolation=%r (known: %s): the target does "
            "not prove how tenant accounts are isolated"
            % (target, isolation, ", ".join(ACCOUNT_ISOLATION)))
    tenant = canon_mod.tenant_key(row.get("responsible"))
    if not tenant:
        raise PodMoveError(
            MOVE_TENANT_MISSING,
            "%s has no human responsible: an avatar of a pod host needs a "
            "tenant (human:<id>)" % (row.get("name") or "?"))
    policy = tenancy.host_policy(db, target)
    if not policy.get("shared") or tenant not in (policy.get("tenants") or ()):
        raise PodMoveError(
            MOVE_TENANT_MISSING,
            "tenant %s is not declared on pod host %s (declared: %s): sync the "
            "Host card and the tenant list before moving"
            % (tenant, target, ", ".join(policy.get("tenants") or ()) or "none"))
    bound = _binding(db, target, tenant)
    if bound is None:
        raise PodMoveError(
            MOVE_TENANT_MISSING,
            "tenant %s is not bound on pod host %s: the supervisor registers "
            "the tenant with the chart UID ('ameesh host tenant add --pod')"
            % (tenant, target))
    expected_user = tenancy.unix_user(tenant)
    if bound.get("unix_user") != expected_user:
        raise PodMoveError(
            MOVE_POD_PASSWD,
            "tenant %s of pod host %s is bound to unix user %r, not %r: the "
            "container passwd does not follow the chart plan"
            % (tenant, target, bound.get("unix_user") or "", expected_user))
    try:
        uid = tenancy.check_pod_uid(bound.get("uid"))
    except tenancy.TenancyError as exc:
        raise PodMoveError(MOVE_POD_UID, str(exc)) from exc
    presence = storage.of(db).tenancy.presence(target, tenant)
    if presence is not None and presence.get("uid") is not None:
        if int(presence["uid"]) != int(uid):
            raise PodMoveError(
                MOVE_POD_UID,
                "tenant %s of pod host %s beats under UID %s but is bound to "
                "%d: the container does not run under the chart UID"
                % (tenant, target, presence["uid"], uid))
    source = row.get("host") or ""
    source_kind, source_uid = _pod_identity(cfg, db, source, tenant)
    if source_kind == "pod" and source_uid is not None and int(source_uid) == int(uid):
        pvc = PVC_REATTACH
    elif source_kind == "pod":
        pvc = PVC_RELOGIN
    else:
        pvc = PVC_NONE
    return {
        "kind": "pod",
        "tenant": tenant,
        "uid": uid,
        "account_isolation": isolation,
        "isolation_ref": str(getattr(runtime, "isolation_ref", "") or ""),
        "namespace": str(getattr(runtime, "namespace", "") or ""),
        "workload": str(getattr(runtime, "workload", "") or ""),
        "source_kind": source_kind,
        "source_uid": source_uid,
        "pvc": pvc,
    }
