# SPDX-License-Identifier: AGPL-3.0-only
"""L539 — la classe de revue d'un LOT : light, normal, sensitive ou release.

Le canon (processus/lots.md §8, `review_policies.risk_classes`) déclare les
portées par projet ; ce module rend la classe du **lot**, c'est-à-dire du diff
qu'il emporte depuis sa cible — `merge-base(target, frozen_sha)..frozen_sha` —
et non d'un ensemble de fichiers donné par l'appelant.

Deux axes, dans cet ordre :

* **release** : la cible est `release/*`, ou le lot est de type `release` ; un
  lot release ne se fusionne jamais tout seul (vol. B) ;
* le diff gelé, classé par la politique du projet du lot (`row["app"]`), tous
  les canons configurés lus — un chemin sensible pour l'un l'emporte, et un
  canon qui ne déclare pas ce projet ne dit rien de lui.

Tout doute rend `sensitive`, jamais `light` : lot non gelé, dépôt ou politique
illisibles, diff impossible à lire. La classe n'est qu'une **lecture** : elle
n'autorise rien à elle seule — la file de fusion garde le gel, la CI verte sur
la tête gelée, les portes et le bail.
"""
from __future__ import annotations

from typing import Optional

from . import canon as canon_mod
from . import plan_git, review as review_mod, work

LIGHT = "light"
NORMAL = "normal"
SENSITIVE = "sensitive"
RELEASE = "release"
#: la plus haute l'emporte entre deux lectures (un chemin sensible pour l'un)
RANK = {LIGHT: 0, NORMAL: 1, SENSITIVE: 2, RELEASE: 3}
#: classe rendue dans le doute : jamais une classe rassurante par défaut.
#: Le drapeau `doubt` dit si la classe est un doute ou une lecture : deux
#: lectures de la même tête se comparent par le maximum, jamais en rétrogradant
#: une classe calculée (revue #544 r1).
DOUBT = SENSITIVE


def _normalized_target(ref) -> str:
    """Une cible ramenée à sa branche : `origin/x`, `refs/heads/x` et
    `refs/remotes/origin/x` valent `x` (revue #544 r1)."""
    text = str(ref or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def release_reason(row: dict, *targets) -> str:
    """Pourquoi ce lot est « release » (chaîne vide sinon).

    L'axe release ne dépend ni du dépôt ni du canon : une cible `release/*` —
    celle portée par le lot **ou la cible résolue à l'atterrissage**, écrite
    `origin/release/1.10` ou `refs/heads/release/1.10` — ou un lot de type
    `release` se lit sur la ligne du lot seule.
    """
    for candidate in tuple(targets) + (row.get("branch_target"),):
        value = _normalized_target(candidate)
        if value == RELEASE or value.startswith(RELEASE + "/"):
            return "target %s" % value
    if str(row.get("type") or "").strip().lower() == RELEASE:
        return "lot type release"
    return ""


def _ref(canon) -> str:
    sources = getattr(canon, "sources", None) or []
    return str(getattr(sources[0], "commit", "") or "") if sources else ""


def repository(cfg, item: dict) -> Optional[str]:
    """Le chemin du dépôt du lot, par son projet (`radar.projects`), ou None.

    Jamais d'exception : un hôte sans réglages radar n'a pas de dépôt à
    proposer, et l'appelant rend un doute.
    """
    from . import radar
    name = str(item.get("app") or "").strip()
    if cfg is None or not name:
        return None
    try:
        project = radar.settings_of(cfg).projects.get(name)
    except Exception:  # noqa: BLE001 - réglages radar illisibles : pas de dépôt
        return None
    return getattr(project, "repo", None) or None


def project_target(cfg, item: dict) -> str:
    """La cible déclarée du projet du lot (`radar.projects.<app>.target`), ou "".

    Le projet est celui du lot (`row["app"]`) ; jamais d'exception : des
    réglages radar illisibles ou un projet que ces réglages ne déclarent pas
    ne disent rien de la cible (même repli que `repository`). Sert à
    `default_base` et à `plan_git.sync_branches`, qui doivent résoudre la
    cible de la même façon (#752)."""
    name = str(item.get("app") or "").strip()
    if cfg is None or not name:
        return ""
    from . import radar
    try:
        project = radar.settings_of(cfg).projects.get(name)
    except Exception:  # noqa: BLE001 - réglages radar illisibles : pas de cible
        return ""
    return str(getattr(project, "target", "") or "").strip() if project is not None else ""


def default_base(cfg, item: dict, repo: str) -> Optional[str]:
    """La cible du lot : `branch_target`, puis la cible du projet
    (`project_target`), puis celle du dépôt (`plan_git.default_target`,
    `origin/HEAD` ou main/master)."""
    explicit = str(item.get("branch_target") or "").strip()
    if explicit:
        return explicit
    target = project_target(cfg, item)
    if target:
        return target
    try:
        return plan_git.default_target(repo)
    except Exception:  # noqa: BLE001 - dépôt illisible : l'appelant doutera
        return None


def class_of_paths(canons, project: Optional[str], paths) -> dict:
    """La classe des `paths` pour `project`, lue dans tous les canons.

    Rend `{class, why, files, policy_ref, doubt}` ; une politique illisible, un
    projet que **aucun** canon ne déclare (sans repli déclaré) ou des chemins
    inclassables rendent `sensitive` avec `doubt` — jamais une classe rassurante
    par défaut. Un canon qui déclare d'autres projets sans repli ne dit rien de
    celui-ci : il est ignoré, pas compté comme un doute.
    """
    worst, why, files, ref = LIGHT, "", [], ""
    covered = False
    for canon in canons or ():
        root = str(getattr(canon, "root", "") or "")
        federation = getattr(canon, "federation", None)
        policy, problems = review_mod.policy_from_federation(federation, project)
        errors = [p for p in problems if p[1] == "error"]
        if errors and all(p[0] == review_mod.UNKNOWN_PROJECT for p in errors):
            # Ce canon ne couvre pas ce projet : sa politique n'en parle pas.
            continue
        if errors:
            return {"class": SENSITIVE, "files": [], "policy_ref": _ref(canon),
                    "doubt": True,
                    "why": "review policy unreadable (%s): %s"
                           % (root or "canon", errors[0][2])}
        covered = True
        try:
            result = review_mod.classify(policy, paths)
        except review_mod.ReviewError as exc:
            return {"class": SENSITIVE, "files": [], "policy_ref": _ref(canon),
                    "doubt": True, "why": "cannot classify the change: %s" % exc}
        if not ref:
            ref = _ref(canon)
        if RANK[result["class"]] > RANK[worst]:
            worst, files = result["class"], result["files"]
            why = "policy of %s" % (root or "canon")
    if not covered:
        return {"class": SENSITIVE, "files": [], "policy_ref": "", "doubt": True,
                "why": "project %r is not declared by any canon (no fallback policy)"
                       % (project,)}
    return {"class": worst, "why": why, "files": files, "policy_ref": ref,
            "doubt": False}


def of_lot(db, lot: int, *, cfg=None, item: Optional[dict] = None,
           repo: Optional[str] = None, base: Optional[str] = None,
           head: Optional[str] = None) -> dict:
    """La classe du lot `lot` ; ne lève jamais (tout doute rend `sensitive`).

    `item` évite une lecture de la base ; `repo`, `base` et `head` sont ceux
    que l'appelant a déjà résolus (la file de fusion connaît la tête gelée et
    le dépôt du clone). Rend aussi `files`, `policy_ref`, `target`, `frozen_sha`,
    `why` et `doubt` (vrai quand la classe vient d'une lecture impossible, pas
    d'une politique) pour la trace.
    """
    out: dict = {"lot": int(lot), "class": SENSITIVE, "why": "", "files": [],
                 "policy_ref": "", "repo": repo or "", "target": base or "",
                 "frozen_sha": head or "", "doubt": True}
    row = item if item is not None else work.get(db, lot)
    if row is None:
        out["why"] = "lot #%d not found" % lot
        return out
    reason = release_reason(row)
    if reason:
        out.update({"class": RELEASE, "why": reason, "doubt": False})
        return out
    if not repo:
        repo = repository(cfg, row)
    out["repo"] = repo or ""
    if not repo:
        out["why"] = "no repository known for the lot"
        return out
    if not head:
        freeze = plan_git.latest_freeze(work.milestones(db, lot))
        if not freeze or not str(freeze.get("sha") or "").strip():
            out["why"] = "lot #%d is not frozen" % lot
            return out
        frozen = str(freeze["sha"]).strip()
        try:
            head = plan_git._commit(repo, frozen)
        except canon_mod.GitError as exc:
            out["why"] = "cannot read the freeze: %s" % exc
            return out
        if not head:
            out["why"] = "the freeze %s is not a commit of %s" % (frozen[:12], repo)
            return out
    out["frozen_sha"] = head
    # La cible résolue à l'atterrissage (celle du projet, ou `origin/HEAD`) peut
    # être `release/*` sans que la ligne du lot le dise : l'axe release se lit
    # sur elle aussi, normalisée (revue #544 r1).
    landing = default_base(cfg, row, repo)
    if not base:
        base = landing
    out["target"] = base or ""
    reason = release_reason(row, landing, base)
    if reason:
        out.update({"class": RELEASE, "why": reason, "doubt": False})
        return out
    if not base:
        out["why"] = "no target branch: freeze the lot or name its target"
        return out
    try:
        paths = canon_mod.git_range_paths(repo, base, head)
    except canon_mod.GitError as exc:
        out["why"] = "cannot read the diff %s..%s: %s" % (base, head[:12], exc)
        return out
    if not paths:
        out.update({"class": LIGHT, "why": "no file changed since %s" % base,
                    "doubt": False})
        return out
    canons = work.configured_canons(cfg)
    if not canons:
        out["why"] = "no readable canon: review policy unknown"
        return out
    result = class_of_paths(canons, row.get("app"), paths)
    out.update(result)
    out["why"] = "%s; %d file(s) since %s" % (result.get("why") or "policy",
                                              len(paths), base)
    return out
