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
"""
from __future__ import annotations

import os
from typing import Mapping

from . import canon as canon_mod
from . import canon_lowering, mechanics, plan_git

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
        add(CODE_UNMEASURED, canon_mod.WARNING, "review policy unreadable: %s" % exc)
        return refuse_explicit()
    for project in sorted(set(explicit) - set(projects)):
        add(CODE_UNMEASURED, canon_mod.ERROR,
            "--project-repo names %s, which risk_classes does not declare" % project)
    for project in projects:
        severity = canon_mod.ERROR if project in explicit else canon_mod.INFO
        try:
            policy = mechanics.load(root.repo, root.commit, project)
        except mechanics.MechanicsAbsent:
            return refuse_explicit()
        except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
            # politique incomplète (`omarchy`) : `load` le refuse déjà au gel
            add(CODE_UNMEASURED, _worse(severity, canon_mod.WARNING),
                "project %s not measured: %s" % (project, exc))
            continue
        try:
            repository, reason = _repository(cfg, project, explicit)
            if not repository:
                add(CODE_UNMEASURED, severity,
                    "project %s not measured: %s" % (project, reason))
                continue
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


def _worse(first: str, second: str) -> str:
    return canon_mod.ERROR if canon_mod.ERROR in (first, second) else canon_mod.WARNING
