# SPDX-License-Identifier: AGPL-3.0-only
"""#5191 — tout fichier suivi de classe non légère a un nombre d'avis déclaré.

#5125 et #5144 ont ajouté une classe sensible au canon sans la règle
`train_reviews` correspondante ; rien ne l'a vu avant le gel suivant d'un lot
touchant ces chemins (#5163, #5188). `ameesh canon check` passe donc les
fichiers suivis du dépôt de chaque projet par `Mechanics.reviews_for`, la
fonction même de la désignation au gel.

Ce contrôle reste hors de `canon.validate`, que `canon sync` lit aussi : une
règle manquante refuse le check, elle ne retire pas le canon aux hôtes. Il ne
lit que des objets git locaux, sans fetch ; un dépôt de projet absent de l'hôte
est un « non mesuré » (info), jamais un vert ni un refus.

#5216 : le dépôt du canon n'a pas de CI ; `for_freeze` lance ce contrôle au gel
d'un lot du canon qui touche sa politique. Seul ce que le lot introduit refuse :
un manque déjà là à sa base, ou né d'un fichier ajouté au projet, ne retient pas
un lot étranger. Une mesure impossible ne refuse pas et se dit dans la note.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from . import canon as canon_mod
from . import canon_lowering, mechanics, plan_git
from .deadline import Deadline

CODE_MISSING = "review-count-missing"
CODE_NO_FILE = "review-rule-no-file"
CODE_UNCLASSIFIABLE = "review-path-unclassifiable"
CODE_UNMEASURED = "review-count-unmeasured"


def _github_slug(cfg, project: str) -> str:
    """`owner/repo` du projet dans la clé `github` de la configuration, ou ""."""
    github = getattr(cfg, "github", None)
    projects = github.get("projects") if isinstance(github, dict) else None
    entry = projects.get(project) if isinstance(projects, dict) else None
    if isinstance(entry, dict):
        entry = entry.get("repo")
    return entry.strip().lower() if isinstance(entry, str) else ""


def _checkout(path: str):
    """`(racine, cible)` d'un clone, ou `(None, raison)`."""
    root = canon_mod.git_toplevel(path) if os.path.isdir(path) else None
    if not root:
        return None, "not a git checkout: %s" % path
    return (root, plan_git.default_target(root) or ""), ""


def _repository(cfg, project: str, explicit: Mapping[str, str]):
    """`--project-repo`, sinon `radar.projects`, sinon le clone courant s'il est
    celui du projet : `(racine, cible)`, ou `(None, raison)`."""
    if project in explicit:
        return _checkout(os.path.expanduser(explicit[project]))
    declared, reason = canon_lowering._project_repository(cfg, project)
    if declared:
        return declared, ""
    slug = _github_slug(cfg, project)
    here = canon_mod.git_toplevel(os.getcwd())
    if slug and here and plan_git._repo_slug(here) == slug:
        return _checkout(here)
    return None, reason


def _scan(policy: mechanics.Mechanics, paths, add) -> None:
    """Un constat par fichier sans nombre d'avis, par règle sans fichier."""
    used: set[int] = set()
    project = policy.project
    for path in paths:
        try:
            policy.reviews_for([path])
        except mechanics.ReviewCountUnknown:
            add(CODE_MISSING, canon_mod.ERROR,
                "project %s: %s has class %s but no train_reviews rule gives its "
                "review count; the review designation refuses it at freeze"
                % (project, path, policy.review_policy.class_of(path)[0]))
        except mechanics.MechanicsError as exc:
            add(CODE_UNCLASSIFIABLE, canon_mod.WARNING,
                "project %s: %s" % (project, exc))
            continue
        for index, rule in enumerate(policy.review_policy.rules):
            if index not in used and any(regex.match(path) for regex in rule.regexes):
                used.add(index)
    for index, rule in enumerate(policy.review_policy.rules):
        if index not in used:
            add(CODE_NO_FILE, canon_mod.WARNING,
                "project %s: class %s rule matches no tracked file: %s"
                % (project, rule.cls, ", ".join(rule.paths)))


def findings(cfg, canon: canon_mod.Canon,
             project_repos: Mapping[str, str] | None = None) -> list[canon_mod.Finding]:
    """Les constats du contrôle pour ce canon (aucun sans politique de revue)."""
    explicit = dict(project_repos or {})
    out: list[canon_mod.Finding] = []
    root = canon.sources[0] if canon.sources else None
    where = {"member": getattr(root, "member", ""), "path": "federation.yaml"}

    def add(code: str, severity: str, message: str) -> None:
        out.append(canon_mod.Finding(code, severity, message, **where))

    def refuse_explicit() -> list[canon_mod.Finding]:
        # un dépôt demandé et non mesuré ne rend jamais un vert muet
        for project in sorted(explicit):
            add(CODE_UNMEASURED, canon_mod.ERROR,
                "project %s: --project-repo given but nothing was measured" % project)
        return out

    if getattr(root, "mode", "") != "git" or not root.commit or root.prefix:
        # la politique se compile au commit exact, à la racine d'un dépôt
        return refuse_explicit()
    try:
        projects = mechanics.declared_projects(root.repo, root.commit)
    except mechanics.MechanicsAbsent:
        return refuse_explicit()
    except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
        # un bloc présent mais mal formé n'est pas « aucune politique »
        add(CODE_UNMEASURED, canon_mod.ERROR, "review policy unreadable: %s" % exc)
        return out
    for project in sorted(set(explicit) - set(projects)):
        add(CODE_UNMEASURED, canon_mod.ERROR,
            "--project-repo names %s, which risk_classes does not declare" % project)
    for project in projects:
        severity = canon_mod.ERROR if project in explicit else canon_mod.INFO
        try:
            repository, reason = _repository(cfg, project, explicit)
        except (canon_mod.CanonError, OSError) as exc:
            add(CODE_UNMEASURED, severity, "project %s not measured: %s" % (project, exc))
            continue
        if not repository:
            add(CODE_UNMEASURED, severity,
                "project %s not measured: %s" % (project, reason))
            continue
        try:
            policy = mechanics.load(root.repo, root.commit, project)
        except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
            # des fichiers à mesurer et aucune politique : le gel refuserait aussi
            add(CODE_UNMEASURED, canon_mod.ERROR,
                "project %s: review policy does not compile: %s" % (project, exc))
            continue
        try:
            _ref, commit = canon_lowering._revision(*repository)
            paths = canon_lowering._tracked(repository[0], commit) if commit else ()
        except (canon_mod.CanonError, OSError) as exc:
            add(CODE_UNMEASURED, severity, "project %s not measured: %s" % (project, exc))
            continue
        if not commit:
            add(CODE_UNMEASURED, severity,
                "project %s not measured: no revision in %s" % (project, repository[0]))
            continue
        _scan(policy, paths, add)
    return out


def _missing(policy: mechanics.Mechanics, paths, deadline: Deadline) -> list[str]:
    """Les fichiers de classe non légère sans nombre d'avis pour cette politique."""
    out: list[str] = []
    for index, path in enumerate(paths):
        if index % canon_lowering.CHECK_EVERY == 0:
            canon_lowering._check(deadline, "the files of %s" % policy.project)
        try:
            policy.reviews_for([path])
        except mechanics.ReviewCountUnknown:
            out.append(path)
        except mechanics.MechanicsError:
            continue  # chemin inclassable : un avertissement de `canon check`
    return out


def _declared(repo: str, commit: str, deadline: Deadline):
    """`(projets, erreur)` au commit ; un canon sans politique n'en déclare aucun."""
    try:
        return mechanics.declared_projects(repo, commit, deadline=deadline), None
    except mechanics.MechanicsAbsent:
        return (), None
    except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
        canon_lowering._check(deadline, "federation.yaml")
        return (), exc


def _measure_project(cfg, repo: str, base: str, head: str, project: str,
                     base_projects, deadline: Deadline, refused: list,
                     unmeasured: list) -> None:
    """Le constat d'un projet déclaré à la tête du lot."""
    before, _error = (canon_lowering._load(repo, base, project, deadline)
                      if project in base_projects else (None, None))
    after, error = canon_lowering._load(repo, head, project, deadline)
    if after is None and before is not None:
        refused.append("project %s: review policy does not compile: %s"
                       % (project, str(error)[:300]))
        return
    if after is None and project in base_projects:
        return  # la base ne compilait pas non plus : rien d'introduit
    found, reason = canon_lowering._project_repository(cfg, project, deadline)
    if not found:
        unmeasured.append("%s (%s)" % (project, reason))
        return
    if after is None:
        refused.append("project %s: review policy does not compile: %s"
                       % (project, str(error)[:300]))
        return
    _ref, commit = canon_lowering._revision(found[0], found[1], deadline)
    if not commit:
        unmeasured.append("%s (no revision in %s)" % (project, found[0]))
        return
    paths = canon_lowering._tracked(found[0], commit, deadline)
    known = set(_missing(before, paths, deadline)) if before is not None else set()
    fresh = [path for path in _missing(after, paths, deadline) if path not in known]
    if fresh:
        listed = fresh[:canon_lowering.MAX_LISTED]
        tail = len(fresh) - len(listed)
        refused.append("project %s: no train_reviews rule gives the review count of "
                       "%s%s" % (project, ", ".join(listed),
                                 " (+%d more)" % tail if tail else ""))


def for_freeze(cfg, repo: str, target: str, head: str,
               *, deadline: Optional[Deadline] = None) -> tuple[list, list]:
    """`(refus, non mesurés)` du contrôle pour la tête gelée d'un lot du canon.

    Rien à dire (`[], []`) quand le diff ne touche pas la politique ou que le
    dépôt n'en porte aucune. Ne lève jamais : le gel n'attend pas une sonde lente.
    """
    if deadline is None:
        deadline = Deadline(canon_lowering.BUDGET_S)
    refused: list = []
    unmeasured: list = []
    try:
        if not target:
            target = plan_git.default_target(repo, deadline=deadline) or ""
        _ref, tip = canon_lowering._revision(repo, target, deadline)
        if not tip:
            return [], ["no readable target in %s" % repo]
        base = canon_lowering._merge_base(repo, tip, head, deadline)
        paths = canon_mod.git_range_paths(repo, base, head)
        if not canon_lowering.touches(paths):
            return [], []
        base_projects, base_error = _declared(repo, base, deadline)
        projects, error = _declared(repo, head, deadline)
        if error is not None:
            if base_error is None:
                refused.append("review policy unreadable: %s" % str(error)[:300])
            return refused, []
        for project in projects:
            _measure_project(cfg, repo, base, head, project, base_projects,
                             deadline, refused, unmeasured)
    except (canon_lowering._Expired, canon_mod.CanonError, canon_mod.GitError,
            canon_mod.GitDeadline, mechanics.MechanicsError, OSError) as exc:
        unmeasured.append("measure interrupted: %s" % str(exc)[:200])
    return refused, unmeasured
