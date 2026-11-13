# SPDX-License-Identifier: AGPL-3.0-only
"""#7107 (avatar B5) : placement des enfants sur un hôte propre.

docs/design/5322-avatar-humain.md §3.6, §6 B5 ; décision Q6 (#213955,
option a : l'enfant est payé par le budget/compte du parent, hôte dédié pour
la première tranche). Le pod (B5b) est hors de ce lot.

Un avatar est une carte qui porte un bloc `avatar`. Son `avatar.children` vaut
`own-host` (défaut), `none` ou `pod` :

* `own-host` : `agent spawn --ttl` place l'enfant sur un hôte **autre** que
  ceux de sa lignée (le créateur immédiat et ses ancêtres), choisi dans
  l'ordre de la fiche `Placement` de l'avatar. L'hôte doit être partagé avec
  le `responsible` (hôte partagé multi-tenant de #7106), avoir une liaison
  `host_tenants` pour ce tenant (son runner et son UID propres) et un dossier
  de travail par enfant (`{agent}` dans la politique de l'hôte). Le verdict de
  placement de l'avatar sur cet hôte est réévalué (`placement.evaluate`) ;
  l'enfant reçoit ce verdict, son profil évalué, et `admitted_hosts` réduit à
  cet hôte.
* `none` : `spawn` refuse (`spawn-forbidden`).
* `pod` : non implémenté ici (B5b) ; `spawn` refuse au lieu de replier
  silencieusement sur un autre mode (`spawn-pod-unsupported`).

Rien n'est jamais replié sur l'hôte, l'UID ou le runner du parent : sans
placement propre, `spawn` refuse (`spawn-no-placement`) et dit l'hôte/le
tenant à provisionner. Aucun achat d'hôte n'est fait par ce lot : c'est une
décision de dépense de l'owner.

La garde d'exécution reste celle de #7106 : sur un hôte partagé, la base
(`ameesh_tenant_claim_ok`) ne donne le bail qu'au runner lié au tenant
`responsible`. Ce module choisit le placement ; il ne crée aucun runner et
n'écrit aucune ligne système.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import canon as canon_mod
from . import placement, storage, tenancy
from .db import Db

#: valeurs admises de `avatar.children`
CHILDREN_OWN_HOST = "own-host"
CHILDREN_NONE = "none"
CHILDREN_POD = "pod"
CHILDREN_POLICIES = (CHILDREN_OWN_HOST, CHILDREN_NONE, CHILDREN_POD)
CHILDREN_DEFAULT = CHILDREN_OWN_HOST

CARD_SECTION = "avatar"
CARD_KEY = "children"

#: codes stables des refus (préfixe du message de `SpawnError`)
SPAWN_FORBIDDEN = "spawn-forbidden"
SPAWN_POD_UNSUPPORTED = "spawn-pod-unsupported"
SPAWN_NO_PLACEMENT = "spawn-no-placement"
SPAWN_POLICY_INVALID = "spawn-policy-invalid"
SPAWN_CANON_UNREADABLE = "spawn-canon-unreadable"
SPAWN_LINEAGE_BROKEN = "spawn-lineage-broken"
SPAWN_CWD_REFUSED = "spawn-cwd-refused"


class PlacementError(ValueError):
    """Refus de placement d'un enfant : `code` stable, message autosuffisant."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


@dataclass(frozen=True)
class ChildPlacement:
    """Placement propre retenu pour un enfant."""

    host: str
    cwd: str
    credential_mode: str | None
    tenant: str
    unix_user: str
    uid: int
    runner_id: str
    verdict: placement.Verdict

    def describe(self) -> dict:
        return {"host": self.host, "cwd": self.cwd, "tenant": self.tenant,
                "unix_user": self.unix_user, "uid": self.uid,
                "runner": self.runner_id}


def policy_of(card: Any) -> tuple[str | None, str]:
    """`avatar.children` d'une carte Agent, ou (None, "") si ce n'est pas un avatar.

    Un bloc `avatar` présent mais illisible ne vaut pas « pas d'avatar » : il
    rend `(None, problème)` et l'appelant refuse (`spawn-policy-invalid`),
    jamais un repli sur l'hôte du parent. Un bloc présent sans `children` vaut
    le défaut `own-host` (§3.6)."""
    data = getattr(getattr(card, "fiche", None), "data", None) or {}
    if CARD_SECTION not in data:
        return None, ""
    section = data.get(CARD_SECTION)
    if not isinstance(section, dict):
        return None, "`%s` must be a mapping" % CARD_SECTION
    value = section.get(CARD_KEY)
    if value is None:
        return CHILDREN_DEFAULT, ""
    if not isinstance(value, str):
        return None, "`%s.%s` must be one of %s" % (
            CARD_SECTION, CARD_KEY, ", ".join(CHILDREN_POLICIES))
    value = value.strip()
    if value not in CHILDREN_POLICIES:
        return None, "`%s.%s`: %r is not one of %s" % (
            CARD_SECTION, CARD_KEY, value, ", ".join(CHILDREN_POLICIES))
    return value, ""


def canon_for(canons: Iterable[Any], root: str, row_canon: str | None):
    """(canon, carte) qui déclare `root`, dans le canon propriétaire de la ligne.

    `canons` est la liste chargée par l'appelant (jamais None ici). Une ligne
    du canon par défaut (`row_canon` NULL/'') n'est lue que dans le canon par
    défaut ; un canon illisible ne rend aucune carte (l'appelant refuse)."""
    for canon in canons:
        if canon is None or not getattr(canon, "readable", False):
            continue
        if not canon.owns(row_canon):
            continue
        card = canon.agent(root)
        if card is not None:
            return canon, card
    return None, None


def lineage(db: Db, creator: str) -> list[dict]:
    """Lignée de `creator` : la ligne du créateur, puis ses créateurs, racine en dernier.

    Un `created_by` absent ou inconnu arrête la marche ; l'appelant refuse si
    la racine est un éphémère dont le canon n'est pas lisible. Borné pour ne
    jamais boucler sur une donnée incohérente."""
    from . import registry

    rows: list[dict] = []
    seen: set[str] = set()
    name: str | None = creator
    while name and name not in seen and len(rows) < 32:
        seen.add(name)
        row = registry.get(db, name)
        if row is None:
            break
        rows.append(row)
        name = row.get("created_by") if row.get("ephemeral") else None
    return rows


def _tenant_of(responsible: str | None) -> str:
    """Tenant `human:<id>` du responsible, ou ""."""
    return canon_mod.tenant_key(responsible)


def _expand_child_path(path: str, tenant: str) -> str:
    """Développe `~` vers le home du tenant : le spawn lit le canon d'un autre
    hôte, le `~` du processus appelant n'y désigne pas le bon utilisateur."""
    if path == "~" or path.startswith("~/"):
        return tenancy.tenant_home(tenant) + path[1:]
    return path


def child_workdir(host_card: Any, root_card: Any,
                  child: str) -> tuple[str | None, str]:
    """Dossier de travail NEUF de l'enfant sur cet hôte, ou (None, raison).

    Un chemin n'est retenu que s'il est propre à l'enfant : `work_dirs` nommé
    pour lui, ou un gabarit `{agent}` dans `work_roots[projet]` / `work_root`.
    Un `work_root` partagé (sans `{agent}`) rendrait le worktree du parent :
    il est refusé, avec le motif à corriger dans la fiche Host."""
    policy = getattr(host_card, "policy", None)
    if policy is None:
        return None, "no Host card"
    dirs = policy.work_dirs or {}
    if child in dirs:
        path = canon_mod.fill_work_template(dirs[child], child)
        if path:
            return path, "policy.workspaces.dirs[%s]" % child
    team = getattr(root_card, "team", None)
    roots = policy.work_roots or {}
    if team and team in roots and "{agent}" in str(roots[team]):
        path = canon_mod.fill_work_template(roots[team], child)
        if path:
            return path, "policy.workspaces.roots[%s]" % team
    if policy.work_root and "{agent}" in str(policy.work_root):
        base = canon_mod.fill_work_template(policy.work_root, child)
        if base:
            return (os.path.join(base, team) if team else base), "policy.workspaces.root"
    return None, (
        "no per-child work directory: declare `policy.workspaces.roots[%s]` with "
        "`{agent}` (or `workspaces.root` with `{agent}`) on host %s"
        % (team or "?", getattr(host_card, "title", "?")))


def _inside(path: str, parent: str | None) -> bool:
    """`path` est-il `parent` ou sous `parent` ? (chemins déjà absolus)"""
    if not parent:
        return False
    parent = parent.rstrip("/")
    return path == parent or path.startswith(parent + "/")


def _refusal_reason(*, canon, host: str, db: Db, tenant: str, root_card: Any,
                   child: str, lineage_rows: list[dict]) -> str:
    """Pourquoi `host` ne peut pas recevoir l'enfant ("" = admissible)."""
    cards = [h for h in canon.hosts if h.title == host]
    if not cards:
        return "no Host card in the canon (unknown policy)"
    if len(cards) > 1:
        return "Host card declared %d times: ambiguous policy" % len(cards)
    host_card = cards[0]
    verdict = placement.evaluate(canon, root_card.title, host)
    if verdict.ok is not True:
        return "placement refused: %s" % (verdict.diagnostic or "not admitted")
    policy = storage.of(db).tenancy.policy(host)
    if not policy.get("shared"):
        return ("host is not shared with its tenant: no tenant runner and no own UID "
                "for the child (a child runner is B5b/out of lot); declare "
                "`policy.shared: true` and `policy.tenants: [%s]` on the Host, then "
                "`ameesh canon sync` on that host" % tenant)
    if tenant not in (policy.get("tenants") or []):
        return ("tenant %s is not declared by the host policy: add it to "
                "`policy.tenants` and `ameesh canon sync` on that host" % tenant)
    binding = next((row for row in storage.of(db).tenancy.bindings(host)
                    if row.get("tenant") == tenant), None)
    if binding is None:
        return ("no tenant runner for %s on host %s: provision the system user and "
                "register it (`ameesh host tenant add %s --host %s --register`)"
                % (tenant, host, tenant, host))
    if not binding.get("runner_id") or int(binding.get("uid") or 0) <= 0:
        return ("tenant %s on host %s has no verified UID/runner: register it again "
                "(`ameesh host tenant add %s --host %s --register`)"
                % (tenant, host, tenant, host))
    state = storage.of(db).canon.state(host, canon.state_key)
    if state is None or state.get("status") != "ok":
        return ("the canon has no valid sync on host %s (status: %s): sync it before "
                "placing a child there" % (host, (state or {}).get("status") or "none"))
    cwd, why = child_workdir(host_card, root_card, child)
    if not cwd:
        return why
    cwd = _expand_child_path(cwd, tenant)
    if not os.path.isabs(cwd):
        return "work directory %r is not absolute" % cwd
    for row in lineage_rows:
        if row.get("host") == host:
            return "host belongs to the child's lineage (%s)" % row.get("name")
        if _inside(cwd, row.get("cwd")):
            return ("work directory %s is inside the worktree of %s"
                    % (cwd, row.get("name")))
    if verdict.credential_mode is None:
        return "no credential mode admitted on this host"
    return ""


def _failure(root_card: Any, policy: str, admitted: list[str],
             excluded: list[str], reasons: list[tuple[str, str]]) -> PlacementError:
    """Le refus autosuffisant : hôtes examinés, ce qui manque, le geste à faire."""
    details = "; ".join("%s (%s)" % (host, why) for host, why in reasons) \
        or "no host outside the child's lineage is admitted"
    return PlacementError(SPAWN_NO_PLACEMENT, (
        "no own placement for a child of %s (avatar.children: %s): the child is never "
        "placed on the host, the UID or the runner of its lineage. Lineage hosts "
        "excluded: %s. Placement hosts: %s. Refused: %s. Add another host to the "
        "avatar's Placement (and register its tenant runner) or provision a children "
        "host; buying a host is an owner spending decision, never this lot's gesture."
        % (getattr(root_card, "title", "?"), policy,
           ", ".join(excluded) or "none", ", ".join(admitted) or "none", details)))


def place_child(db: Db, canon, root_card: Any, child: str, *,
                lineage_rows: list[dict], cwd: str | None = None) -> ChildPlacement | None:
    """Placement propre de `child` sous `root_card`, ou None (pas un avatar).

    Refuse (`PlacementError`) sur `avatar.children: none|pod`, politique
    illisible, `--cwd` explicite (le worktree doit être choisi sur l'hôte
    d'arrivée), ou absence de placement propre."""
    policy, problem = policy_of(root_card)
    if policy is None and problem:
        raise PlacementError(SPAWN_POLICY_INVALID, problem)
    if policy is None:
        return None
    if policy == CHILDREN_NONE:
        raise PlacementError(SPAWN_FORBIDDEN, (
            "avatar %s declares `avatar.children: none`: no child may be spawned from "
            "it" % root_card.title))
    if policy == CHILDREN_POD:
        raise PlacementError(SPAWN_POD_UNSUPPORTED, (
            "avatar %s declares `avatar.children: pod`: pod placement is B5b, out of "
            "this lot; refusing instead of falling back to the parent's host"
            % root_card.title))
    if cwd:
        raise PlacementError(SPAWN_CWD_REFUSED, (
            "a child's worktree is chosen on its own host; `--cwd` is refused for an "
            "avatar child (the parent's path must not be reused)"))
    tenant = _tenant_of(lineage_rows[0].get("responsible") if lineage_rows else None)
    if not tenant:
        raise PlacementError(SPAWN_NO_PLACEMENT, (
            "avatar %s has no resolved responsible tenant: no host can bind a runner "
            "for its children" % root_card.title))
    admission = placement.admission_of(canon, root_card.title)
    if admission is None:
        raise PlacementError(SPAWN_NO_PLACEMENT, (
            "avatar %s has no single Placement card: declare the hosts that may hold "
            "its children (docs/design/5322-avatar-humain.md §3.6)"
            % root_card.title))
    admitted = placement.admitted_hosts(canon, admission)
    lineage_hosts = [str(row.get("host")) for row in lineage_rows if row.get("host")]
    excluded = [host for host in admitted if host in lineage_hosts]
    candidates = [host for host in admitted if host not in lineage_hosts]
    reasons: list[tuple[str, str]] = []
    for host in candidates:
        why = _refusal_reason(canon=canon, host=host, db=db, tenant=tenant,
                              root_card=root_card, child=child,
                              lineage_rows=lineage_rows)
        if not why:
            verdict = placement.evaluate(canon, root_card.title, host)
            host_card = canon.host(host)
            workdir, _source = child_workdir(host_card, root_card, child)
            workdir = _expand_child_path(workdir or "", tenant)
            binding = next(row for row in storage.of(db).tenancy.bindings(host)
                           if row.get("tenant") == tenant)
            return ChildPlacement(
                host=host, cwd=workdir, credential_mode=verdict.credential_mode,
                tenant=tenant, unix_user=str(binding["unix_user"]),
                uid=int(binding["uid"]), runner_id=str(binding["runner_id"]),
                verdict=verdict)
        reasons.append((host, why))
    raise _failure(root_card, policy, admitted, excluded, reasons)
