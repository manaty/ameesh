# SPDX-License-Identifier: AGPL-3.0-only
"""Radar de conflits entre lots, en observation (lot L160b, étude L160).

    ameesh conflicts [--project P] [--all] [--json]

Sur l'hôte désigné — celui dont la configuration déclare `radar.projects` —,
l'exécuteur fait un passage du radar juste après chaque relevé des branches
(L118, toutes les 5 min). Pour chaque projet déclaré :

1. les lots ouverts dont la branche est dans le dépôt du projet (locale, sinon
   `origin/<branche>`) et en avance sur leur cible ; leurs fichiers touchés
   depuis la base commune avec la cible ;
2. chaque paire de lots de même cible qui touchent un même fichier : une
   fusion d'essai `git merge-tree --write-tree` (état `conflict` ou
   `overlap`) ; chaque lot contre sa cible (`conflict` ou `clean`) ;
3. la sonde des migrations (si le projet déclare `migrations`) : un même
   numéro `NNNN_` ajouté par deux lots, ou par un lot alors que la cible l'a
   déjà sous un autre nom. Seules les migrations du **delta propre** du lot
   comptent (celles que la cible ne porte pas déjà) : une migration de la
   cible ramenée par une fusion dans la branche n'est à personne (#1333).

Les fusions d'essai n'écrivent rien dans le dépôt des agents : leurs objets
vont dans un dossier jetable (`GIT_OBJECT_DIRECTORY`, objets du dépôt en
alternates), aucun worktree, aucun `fetch`. Une paire n'est recalculée que si
l'une de ses deux pointes a bougé (cache en mémoire de l'exécuteur).

Une cible qui nomme une branche — `develop`, `origin/develop`,
`refs/remotes/origin/develop`, `refs/heads/develop`, `release/1.x`, ou
`origin/HEAD` — est lue sur la ref publiée de son remote quand elle existe :
dans un clone partagé, la branche locale reste figée alors que les agents
poussent (#1333). Un SHA ou un tag est une cible figée, lue telle quelle. La
sonde des migrations ne parle que d'une cible **certifiée** : `git ls-remote`
(lecture seule, rien n'est écrit dans le dépôt) vérifie que la ref lue est
bien la pointe publiée ; sinon la sonde se tait et le rapport dit pourquoi.

Le résultat est un journal de l'hôte, pas une table : le dernier passage dans
`<state_dir>/radar/<projet>.json` (lu par `ameesh conflicts`), et une ligne par
passage dans `<projet>.jsonl` avec les changements d'état, pour calibrer.
Pendant cette phase d'observation, le radar n'envoie AUCUN courrier (L160c).

L160d: each pair in conflict gets its resolver, and a pair whose resolution
was prepared (`ameesh work resolve`, `ameesh.resolutions`) is replayed in the
radar's private clone (`<projet>.clone`): `prepared` if `rerere` resolves
every conflicted path, `stale` otherwise.

Configuration de l'hôte (clé `radar`) :

    "radar": {"projects": {"ameesh": {"repo": "~/work/ameesh",
                                      "migrations": "src/ameesh/migrations",
                                      "github": "manaty/ameesh"}}}

`github` (facultatif, sinon déduit de l'URL `origin`) : le dépôt où lire l'état
de la CI des PR, pour savoir si un conflit bloque.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from . import plan_git
from .canon import GIT_TIMEOUT, GitError, _git, _git_env

SCHEMA = "ameesh-radar/1"
SETTINGS_KEYS = ("projects",)
PROJECT_KEYS = ("repo", "migrations", "target", "github", "land", "gates", "merge_method",
                "lease_ref", "generated", "ci_required", "protected", "full_suite", "ci_parts")
#: L1651 : clés de la déclaration `ci_parts` (les espaces réservés d'`argv`
#: sont ceux du contrat, `ci_parts.PLACEHOLDERS`)
CI_PARTS_KEYS = ("selector", "rev", "argv", "schema", "due")
#: #194 r6: where the lease of a target lives on the remote (local mode)
LEASE_REF_DEFAULT = "refs/ameesh/land/"
#: #194 r8: the only namespaces a lease ref may live in — never a branch that
#: ameesh lands on
LEASE_NAMESPACES = ("refs/ameesh/", "refs/heads/ameesh-land/")
#: L160e2: who merges the head of the queue — nobody (`off`: the agent or the
#: orchestrator), ameesh by a local merge pushed to the target, or ameesh
#: through the PR (target merged into the lot branch, CI, PR merged by the API)
LAND_MODES = ("off", "local", "pr")
MERGE_METHODS = ("merge", "squash", "rebase")
#: une migration numérotée : `NNNN_nom.sql`
MIGRATION_RE = re.compile(r"^([0-9]{3,4})_[^/]+\.sql$")
#: paires gardées au plus dans le cache d'un exécuteur
MAX_CACHE = 20000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GITHUB_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: L756 (#566): where GitHub reads workflows — a declared full suite is a file
#: of this directory, never a display name (which renames).
WORKFLOW_DIR = ".github/workflows/"
#: URL `origin` d'un dépôt GitHub (https ou ssh)
_ORIGIN_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
#: conclusions de check qui ne bloquent pas une PR verte
CI_OK = frozenset(("SUCCESS", "NEUTRAL", "SKIPPED"))
#: #1333: lecture seule de la pointe publiée d'une cible (`git ls-remote`)
LS_REMOTE_TIMEOUT = 30.0
#: L1651 (#1650) : conclusions d'un run ou d'un check qui prouvent une part
#: due — `skipped` dit que la part n'a pas tourné, c'est le trou mesuré
CI_DUE_OK = frozenset(("success", "neutral"))
#: L1651 : états possibles d'un contexte sur la tête gelée, du plus faible au
#: plus fort ; plusieurs sources qui portent le même libellé gardent le plus fort
CI_CONTEXT_RANK = {"success": 0, "pending": 1, "failed": 2}


class SettingsError(ValueError):
    """Clé `radar` invalide : message actionnable."""


@dataclass(frozen=True)
class Generated:
    """L160g (#473): a file produced by a script. A merge never resolves it
    (no rerere): it is regenerated on the merged tree by `regen`, then
    `check` must pass."""
    path: str
    regen: str
    check: str = ""


@dataclass(frozen=True)
class Gate:
    """L160g (#473): a gate run on the merged tree only when the merge
    touches one of `paths` (globs, as the paths filters of a workflow)."""
    run: str
    paths: tuple = ()


@dataclass(frozen=True)
class CiParts:
    """L1651 (#1650) : les parts qu'un change set rend dues, recalculées par
    le sélecteur versionné du dépôt (`ci_parts.selector`, lu au commit de
    `rev`, la cible par défaut) et non déduites des statuts présents.

    `argv` est lancé dans l'arbre extrait du commit épinglé, sans shell ;
    `{selector}`, `{root}`, `{files}` (les noms changés, séparés par NUL) et
    `{out}` (le plan JSON) y sont substitués. `schema` (facultatif) fixe le
    schéma attendu du plan, `due` le champ qui porte la liste des parts."""
    selector: str
    argv: tuple
    rev: str = ""
    schema: str = ""
    due: str = "due"


@dataclass(frozen=True)
class Project:
    name: str
    repo: str
    migrations: str = ""
    target: str = ""
    github: str = ""
    #: L160e2: `off` (default), `local` or `pr`; the commands run on the merged
    #: tree before it is pushed; how a PR is merged
    land: str = "off"
    gates: tuple = ()
    merge_method: str = "merge"
    #: #194 r6: prefix of the remote lease ref (`refs/ameesh/land/` by default;
    #: a branch prefix such as `ameesh-land/` when the remote refuses custom
    #: namespaces) — the lease of target T is `<prefix>T`
    lease_ref: str = LEASE_REF_DEFAULT
    #: L160g (#473): files regenerated, never resolved (`Generated`); gates
    #: chosen by the paths the merge touches (`Gate`); GitHub CI green on the
    #: frozen commit before entering the queue; targets (globs) the queue and
    #: `land` refuse (main, release branches, tags: an orchestrator's gesture)
    generated: tuple = ()
    path_gates: tuple = ()
    ci_required: bool = False
    protected: tuple = ()
    #: L756 (#566): the workflow files that make up the project's **full
    #: suite** (`.github/workflows/…`), declared by path — the file is the
    #: identity; a display name is free text and renames. A release lot
    #: certifies its head with a green run of each, jobs included.
    full_suite: tuple = ()
    #: L756 B3 (#566): per workflow, the **required jobs** — `((path, (job,
    #: …)), …)`. A declared entry `{path = …, jobs = […]}` names them; a bare
    #: path entry requires *all* the jobs of the run (fail-closed). A required
    #: job must exist and be green (every matrix instance); the other jobs must
    #: be `success` or `skipped` — a job skipped by design (`if:` on the event
    #: or the paths) never blocks a declared suite. A name ends with " *" for a
    #: matrix family (`psycopg *`), otherwise it matches exactly.
    full_suite_required: tuple = ()
    #: L756 B3 (#566): per workflow, the **tolerated jobs** — `((path, (job,
    #: …)), …)`; their conclusion neither certifies nor blocks (the `bilan des
    #: impactés` job is `continue-on-error: true` and "never required",
    #: EXPLOITATION §L161). Only meaningful with a declared `jobs` list.
    full_suite_tolerated: tuple = ()
    #: L1651 (#1650): les parts dues d'un change set gelé, recalculées par le
    #: sélecteur versionné du dépôt (`CiParts`) ; None : aucune preuve de
    #: complétude n'est demandée
    ci_parts: CiParts | None = None


@dataclass
class Settings:
    projects: dict = field(default_factory=dict)


def _suite_jobs(declared, where: str, path, kind: str, empty_ok: bool = False) -> tuple:
    """L756 B3 (#566): les jobs d'une entrée `full_suite` — un nom d'affichage,
    ou « nom * » pour une famille de matrice (le préfixe est suivi d'un
    espace : `psycopg *` couvre `psycopg 1/4`, et `tests` n'attrape pas les
    check runs `tests (flux) …`). « * » seul ou un `*` non final est refusé."""
    if declared is None:
        declared = ()
    if isinstance(declared, str):
        declared = (declared,)
    if not isinstance(declared, (list, tuple)) or (not declared and not empty_ok):
        raise SettingsError("%s.full_suite: %r: a non-empty list of %s job names expected"
                            % (where, path, kind))
    out = []
    for raw in declared:
        if not isinstance(raw, str) or not raw.strip():
            raise SettingsError("%s.full_suite: %r: %s job names expected" % (where, path, kind))
        name = raw.strip()
        if name == "*" or ("*" in name and not name.endswith(" *")):
            raise SettingsError("%s.full_suite: %r: %s job %r: a name, or \"name *\" for a "
                                "matrix family, expected" % (where, path, kind, name))
        if name in out:
            raise SettingsError("%s.full_suite: %r: duplicate %s job %s"
                                % (where, path, kind, name))
        out.append(name)
    return tuple(out)


def parse_ci_parts(raw, where: str) -> CiParts:
    """L1651 (#1650) : la déclaration `ci_parts` d'un projet. Le contrat est
    refusé à la moindre ambiguïté (clé inconnue, sélecteur hors du dépôt,
    `argv` sans `{files}` ni `{out}`, espace réservé inconnu) : une porte qui
    ne sait pas recalculer les parts dues refuse, elle ne devine pas."""
    from . import ci_parts
    if not isinstance(raw, dict):
        raise SettingsError("%s.ci_parts: object {\"selector\": …, \"argv\": […]} expected"
                            % where)
    extra = sorted(set(map(str, raw)) - set(CI_PARTS_KEYS))
    if extra:
        raise SettingsError("%s.ci_parts: unknown key(s) %s (allowed: %s)"
                            % (where, ", ".join(extra), ", ".join(CI_PARTS_KEYS)))
    selector = raw.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise SettingsError("%s.ci_parts.selector: repository-relative path expected" % where)
    selector = selector.strip()
    if selector.startswith("/") or any(part in ("", ".", "..") for part in selector.split("/")):
        raise SettingsError("%s.ci_parts.selector: %r is not a path inside the repository"
                            % (where, selector))
    argv = raw.get("argv")
    if isinstance(argv, str):
        argv = (argv,)
    if not isinstance(argv, (list, tuple)) or not argv \
            or not all(isinstance(part, str) and part.strip() for part in argv):
        raise SettingsError("%s.ci_parts.argv: a non-empty list of command arguments expected "
                            "(no shell)" % where)
    argv = tuple(part.strip() for part in argv)
    placeholders = []
    for part in argv:
        placeholders.extend(ci_parts.PLACEHOLDER_RE.findall(part))
        if ci_parts.PLACEHOLDER_RE.sub("", part).count("{") or \
                ci_parts.PLACEHOLDER_RE.sub("", part).count("}"):
            raise SettingsError("%s.ci_parts.argv: stray brace in %r (placeholders: %s)"
                                % (where, part,
                                   ", ".join("{%s}" % p for p in ci_parts.PLACEHOLDERS)))
    unknown = sorted(set(placeholders) - set(ci_parts.PLACEHOLDERS))
    if unknown:
        raise SettingsError("%s.ci_parts.argv: unknown placeholder(s) %s (allowed: %s)"
                            % (where, ", ".join("{%s}" % p for p in unknown),
                               ", ".join("{%s}" % p for p in ci_parts.PLACEHOLDERS)))
    for required in ("files", "out"):
        if required not in placeholders:
            raise SettingsError("%s.ci_parts.argv: {%s} is required (the change set fed to the "
                                "selector, and the plan it writes)" % (where, required))
    rev = (raw.get("rev") or "").strip()
    if rev:
        try:
            plan_git.check_ref(rev, "ci_parts.rev")
        except ValueError as exc:
            raise SettingsError("%s.ci_parts.rev: %s" % (where, exc)) from exc
    texts = {}
    for key, default in (("schema", ""), ("due", "due")):
        text = raw.get(key, default)
        if not isinstance(text, str) or not text.strip():
            raise SettingsError("%s.ci_parts.%s: non-empty text expected" % (where, key))
        texts[key] = text.strip()
    return CiParts(selector=selector, argv=argv, rev=rev, schema=texts["schema"],
                   due=texts["due"])


def parse_settings(raw) -> Settings:
    """Valide la clé `radar` de la configuration de l'hôte."""
    if raw is None or raw == {}:
        return Settings()
    if not isinstance(raw, dict):
        raise SettingsError("`radar`: JSON object expected")
    unknown = sorted(set(map(str, raw)) - set(SETTINGS_KEYS))
    if unknown:
        raise SettingsError("`radar`: unknown key(s) %s (allowed: %s)"
                            % (", ".join(unknown), ", ".join(SETTINGS_KEYS)))
    declared = raw.get("projects") or {}
    if not isinstance(declared, dict):
        raise SettingsError("`radar.projects`: object {\"project\": {\"repo\": …}} expected")
    projects: dict = {}
    for name, value in declared.items():
        name = str(name).strip()
        where = "`radar.projects.%s`" % name
        if not _NAME_RE.match(name):
            raise SettingsError("%s: invalid project name (letters, digits, . _ -)"
                                % where)
        if isinstance(value, str):
            value = {"repo": value}
        if not isinstance(value, dict):
            raise SettingsError("%s: object {\"repo\": path} expected" % where)
        extra = sorted(set(map(str, value)) - set(PROJECT_KEYS))
        if extra:
            raise SettingsError("%s: unknown key(s) %s (allowed: %s)"
                                % (where, ", ".join(extra), ", ".join(PROJECT_KEYS)))
        repo = value.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise SettingsError("%s.repo: repository path expected" % where)
        texts = {}
        for key in ("migrations", "target", "github"):
            text = value.get(key) or ""
            if not isinstance(text, str):
                raise SettingsError("%s.%s: text expected" % (where, key))
            texts[key] = text.strip().strip("/")
        if texts["target"]:
            plan_git.check_ref(texts["target"], "cible")
        if texts["github"] and not _GITHUB_RE.match(texts["github"]):
            raise SettingsError("%s.github: \"owner/repo\" repository expected" % where)
        land = value.get("land") or "off"
        if land not in LAND_MODES:
            raise SettingsError("%s.land: %s expected" % (where, " | ".join(LAND_MODES)))
        method = value.get("merge_method") or "merge"
        if method not in MERGE_METHODS:
            raise SettingsError("%s.merge_method: %s expected"
                                % (where, " | ".join(MERGE_METHODS)))
        gates = value.get("gates") or ()
        if isinstance(gates, str):
            gates = (gates,)
        if not isinstance(gates, (list, tuple)):
            raise SettingsError("%s.gates: a list of commands expected" % where)
        always, by_path = [], []
        for gate in gates:
            if isinstance(gate, str) and gate.strip():
                always.append(gate.strip())
                continue
            run = gate.get("run") if isinstance(gate, dict) else None
            paths = gate.get("paths", ()) if isinstance(gate, dict) else None
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(run, str) or not run.strip() \
                    or set(gate) - {"run", "paths"} or not isinstance(paths, (list, tuple)) \
                    or not all(isinstance(p, str) and p.strip() for p in paths):
                raise SettingsError("%s.gates: a command, or {\"run\": command, \"paths\": "
                                    "[globs]} expected" % where)
            if paths:
                by_path.append(Gate(run.strip(), tuple(p.strip() for p in paths)))
            else:
                always.append(run.strip())
        generated = []
        declared_generated = value.get("generated", ())
        if not isinstance(declared_generated, (list, tuple)):
            raise SettingsError("%s.generated: a list of generated files expected" % where)
        for item in declared_generated:
            if not isinstance(item, dict) or set(item) - {"path", "regen", "check"} \
                    or not all(isinstance(item.get(k), str) and item[k].strip()
                               for k in ("path", "regen", "check")) \
                    or not isinstance(item.get("check", ""), str):
                raise SettingsError("%s.generated: [{\"path\": file, \"regen\": command, "
                                    "\"check\": command}] expected" % where)
            generated_path = item["path"].strip()
            if generated_path.startswith("/") or any(
                    part in ("", ".", "..") for part in generated_path.split("/")):
                raise SettingsError("%s.generated: path must be relative to the repository" % where)
            if any(g.path == generated_path for g in generated):
                raise SettingsError("%s.generated: duplicate path %s" % (where, item["path"]))
            generated.append(Generated(generated_path, item["regen"].strip(),
                                       (item.get("check") or "").strip()))
        ci_required = value.get("ci_required", False)
        if not isinstance(ci_required, bool):
            raise SettingsError("%s.ci_required: true or false expected" % where)
        ci_parts = None
        if value.get("ci_parts") is not None:
            ci_parts = parse_ci_parts(value["ci_parts"], where)
            if not ci_required:
                # L1651 : la preuve de complétude n'a de sens que dans la
                # lecture de la CI du commit gelé ; une déclaration inerte
                # serait un faux sentiment de porte
                raise SettingsError("%s.ci_parts: ci_required must be true for the due parts of "
                                    "the frozen commit to be proven" % where)
        protected = value.get("protected") or ()
        if isinstance(protected, str):
            protected = (protected,)
        if not isinstance(protected, (list, tuple)) or not all(
                isinstance(p, str) and p.strip() for p in protected):
            raise SettingsError("%s.protected: a list of target globs expected" % where)
        declared_suite = value.get("full_suite") or ()
        if isinstance(declared_suite, (str, dict)):
            declared_suite = (declared_suite,)
        if not isinstance(declared_suite, (list, tuple)):
            raise SettingsError("%s.full_suite: a list of workflow files expected "
                                "(e.g. .github/workflows/backend-suite.yml)" % where)
        full_suite = []
        required_jobs = []
        tolerated_jobs = []
        for item in declared_suite:
            jobs = ()
            tolerated = ()
            if isinstance(item, str):
                path = item
            elif isinstance(item, dict) and not set(item) - {"path", "jobs", "tolerate"}:
                path = item.get("path")
                jobs = _suite_jobs(item.get("jobs"), where, path, "required")
                tolerated = _suite_jobs(item.get("tolerate", ()), where, path, "tolerated",
                                        empty_ok=True)
                both = sorted(set(jobs) & set(tolerated))
                if both:
                    raise SettingsError("%s.full_suite: %r: %s is both required and tolerated"
                                        % (where, path, both[0]))
            else:
                raise SettingsError("%s.full_suite: a workflow file, or "
                                    "{\"path\": file, \"jobs\": [names], \"tolerate\": [names]} "
                                    "expected" % where)
            if not isinstance(path, str) or not path.strip():
                raise SettingsError("%s.full_suite: a workflow file path expected" % where)
            path = path.strip()
            if (not path.startswith(WORKFLOW_DIR) or not path.endswith((".yml", ".yaml"))
                    or any(part in ("", ".", "..") for part in path.split("/"))):
                raise SettingsError("%s.full_suite: %r is not a %s*.yml workflow file"
                                    % (where, path, WORKFLOW_DIR))
            if path in full_suite:
                raise SettingsError("%s.full_suite: duplicate workflow %s" % (where, path))
            full_suite.append(path)
            if jobs:
                required_jobs.append((path, jobs))
            if tolerated:
                tolerated_jobs.append((path, tolerated))
        gates = always
        if land != "off" and not (gates or by_path):
            raise SettingsError("%s.gates: a project that ameesh lands (land %s) declares "
                                "its gates, run on the merged tree" % (where, land))
        lease_ref = value.get("lease_ref") or LEASE_REF_DEFAULT
        if not isinstance(lease_ref, str) or not lease_ref.strip().endswith("/"):
            raise SettingsError("%s.lease_ref: a ref prefix ending with / expected "
                                "(refs/ameesh/land/, or a branch prefix such as ameesh-land/)"
                                % where)
        lease_ref = lease_ref.strip()
        if not lease_ref.startswith("refs/"):
            lease_ref = "refs/heads/" + lease_ref
        if not lease_ref.startswith(LEASE_NAMESPACES):
            raise SettingsError("%s.lease_ref: %s is not a lease namespace (%s, or a prefix "
                                "under them)" % (where, lease_ref, " or ".join(LEASE_NAMESPACES)))
        if texts["target"] and lease_collides(lease_ref, texts["target"]):
            raise SettingsError("%s.lease_ref: %s collides with the target %s"
                                % (where, lease_ref, texts["target"]))
        try:
            plan_git.check_ref(lease_ref + "main", "lease_ref")
        except ValueError as exc:
            raise SettingsError("%s.lease_ref: %s" % (where, exc)) from exc
        projects[name] = Project(name=name, repo=os.path.expanduser(repo.strip()),
                                 migrations=texts["migrations"], target=texts["target"],
                                 github=texts["github"], land=land,
                                 gates=tuple(gates), merge_method=method,
                                 lease_ref=lease_ref,
                                 generated=tuple(generated), path_gates=tuple(by_path),
                                 ci_required=ci_required,
                                 protected=tuple(p.strip() for p in protected),
                                 full_suite=tuple(full_suite),
                                 full_suite_required=tuple(required_jobs),
                                 full_suite_tolerated=tuple(tolerated_jobs),
                                 ci_parts=ci_parts)
    # Lease namespaces are reserved across the whole configuration. Checkout
    # paths and remote aliases must not let one project's lease overwrite
    # another project's target, including projects whose landing is off.
    for holder in projects.values():
        if holder.land != "local":
            continue
        for other in projects.values():
            if other.target and lease_collides(holder.lease_ref, other.target):
                raise SettingsError("`radar.projects.%s.lease_ref`: %s collides with the target %s of %s"
                                    % (holder.name, holder.lease_ref, other.target, other.name))

    return Settings(projects=projects)


def lease_collides(prefix: str, target: str) -> bool:
    """#194 r8: the lease ref of `target` under `prefix` would be the target
    branch itself, or one holds the other (a target inside the lease
    namespace, a lease ref under the target's name)."""
    branch = target
    for head in ("origin/", "refs/heads/"):
        branch = branch[len(head):] if branch.startswith(head) else branch
    target_ref, lease = "refs/heads/" + branch, prefix + branch
    return (lease == target_ref or target_ref.startswith(prefix)
            or prefix.startswith(target_ref + "/") or lease.startswith(target_ref + "/")
            or target_ref.startswith(lease + "/"))


def protected_target(project, target: str) -> str | None:
    """L160g (#473): the `protected` glob that `target` matches, or None.
    Matched against the target as written, its branch (`origin/develop` →
    `develop`) and its full ref (`refs/heads/…`, or `refs/tags/…` as is)."""
    import fnmatch
    if not target or not getattr(project, "protected", ()):
        return None
    branch = target.split("/", 1)[1] if target.startswith("origin/") else target
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/"):]
    names = {target, branch, branch if branch.startswith("refs/") else "refs/heads/" + branch}
    for glob in project.protected:
        if any(fnmatch.fnmatchcase(name, glob) for name in names):
            return glob
    return None


def settings_of(cfg) -> Settings:
    return parse_settings(getattr(cfg, "radar", None) or {})


def journal_dir(cfg) -> str:
    return os.path.join(cfg.state_dir, "radar")


# --------------------------------------------------------------------------
# git : lecture seule, objets des fusions d'essai dans un dossier jetable
# --------------------------------------------------------------------------

def _scratch_git(repo: str, objects: str, scratch: str, *args: str):
    env = _git_env()
    env.update(GIT_OBJECT_DIRECTORY=scratch, GIT_ALTERNATE_OBJECT_DIRECTORIES=objects)
    cmd = ["git", "--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=false",
           "-c", "core.hooksPath=%s" % os.devnull, *args]
    try:
        return subprocess.run(cmd, cwd=repo, capture_output=True, env=env,
                              timeout=GIT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise GitError("git %s: timed out" % args[0]) from exc
    except OSError as exc:
        raise GitError("git %s: %s" % (args[0], exc.strerror or exc)) from exc


def _lines(proc) -> list[str]:
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def trial_merge(repo: str, objects: str, scratch: str, a: str, b: str, *,
                attributes_tree: str | None = None) -> list[str] | None:
    """Fusion d'essai de deux commits : la liste des fichiers en conflit (vide :
    fusion propre), None si git n'a pas su la calculer.

    #1517 : les attributs de fusion (`.gitattributes`, `merge=union` des
    listes centrales) sont lus dans l'arbre de la cible, et non dans ce que
    la copie locale a d'extrait. Pour une paire de lots, `attributes_tree`
    nomme cette cible même si aucun des lots n'en porte les attributs (#1610).
    Par défaut, `a` est la cible de la fusion."""
    proc = _scratch_git(repo, objects, scratch, "-c",
                        "attr.tree=%s" % (attributes_tree or a),
                        "merge-tree", "--write-tree", "-z",
                        "--name-only", "--no-messages", a, b)
    if proc.returncode not in (0, 1):
        return None
    out = _lines(proc)[1:]
    return sorted(set(out)) if proc.returncode == 1 else []


def _changed(repo: str, base: str, tip: str) -> tuple[list[str], list[str]]:
    """(fichiers touchés, fichiers ajoutés) entre `base` et `tip`, renommages
    comptés pour leurs deux chemins."""
    proc = _git(repo, "diff", "--no-renames", "--name-status", "-z", base, tip)
    if proc.returncode != 0:
        raise GitError("git diff %s..%s : %s" % (base[:12], tip[:12],
                                                 proc.stderr.decode("utf-8", "replace").strip()))
    parts = _lines(proc)
    touched, added = [], []
    for status, path in zip(parts[0::2], parts[1::2]):
        touched.append(path)
        if status.startswith("A"):
            added.append(path)
    return sorted(touched), sorted(added)


def _numbers_in(repo: str, commit: str, directory: str) -> dict[str, str]:
    """{numéro: chemin} des migrations de `directory` au commit `commit`."""
    proc = _git(repo, "ls-tree", "-z", "--name-only", commit, directory + "/")
    out: dict[str, str] = {}
    for path in _lines(proc) if proc.returncode == 0 else ():
        match = MIGRATION_RE.match(os.path.basename(path))
        if match:
            out.setdefault(match.group(1), path)
    return out


_REMOTE_REFS = "refs/remotes/"
_HEADS_REFS = "refs/heads/"


def _remotes(repo: str) -> tuple[str, ...]:
    """Les remotes déclarés du dépôt (`git remote`), `origin` par défaut."""
    names = tuple((plan_git._out(repo, "remote") or "").split())
    return names or ("origin",)


def _branch_target(target: str, remotes: tuple[str, ...]) -> tuple[str, str] | None:
    """(remote, branche) d'une cible qui nomme une branche, None sinon.

    Toutes les écritures d'une branche sont reconnues — `develop`,
    `origin/develop`, `refs/remotes/origin/develop`, `refs/heads/develop` — y
    compris un nom qui porte un slash (`release/1.x`, #1333) : le premier
    segment n'est un remote que s'il est déclaré. Un SHA est un cas distinct
    (cible figée) : None, lu tel quel."""
    target = (target or "").strip()
    if not target or plan_git._SHA_RE.match(target):
        return None
    if target == "HEAD":
        return "origin", "HEAD"
    if target.startswith(_REMOTE_REFS):
        remote, _, branch = target[len(_REMOTE_REFS):].partition("/")
        return (remote, branch) if remote and branch else None
    if target.startswith(_HEADS_REFS):
        branch = target[len(_HEADS_REFS):]
        return ("origin", branch) if branch else None
    if "/" in target:
        head, _, branch = target.partition("/")
        if branch and head in remotes:
            return head, branch
    return "origin", target


def _symbolic_head(repo: str, remote: str) -> str | None:
    """La branche que désigne `refs/remotes/<remote>/HEAD`, ou None."""
    resolved = plan_git._out(repo, "symbolic-ref", "--quiet",
                             _REMOTE_REFS + remote + "/HEAD")
    prefix = _REMOTE_REFS + remote + "/"
    if resolved and resolved.startswith(prefix) and len(resolved) > len(prefix):
        return resolved[len(prefix):]
    return None


def target_commit(repo: str, target: str,
                  remotes: tuple[str, ...] | None = None
                  ) -> tuple[str | None, str, str | None, str | None]:
    """(commit, ref lue, branche publiée, remote publié) de la cible.

    Une cible qui nomme une branche — quelle qu'en soit l'écriture — est lue
    sur `refs/remotes/<remote>/<branche>` quand cette ref existe : dans un
    clone partagé, la branche locale reste figée pendant que les agents
    poussent (#1333). La branche locale ne sert que de repli ; la branche et
    le remote rendus permettent à l'appelant de certifier la pointe publiée
    (`published_tip`). `origin/HEAD` est résolu sur la branche qu'il désigne.
    Un SHA, un tag ou une ref qui n'est pas une branche sont rendus tels quels,
    sans branche publiée : cas distinct, sans certification."""
    remotes = _remotes(repo) if remotes is None else remotes
    split = _branch_target(target, remotes)
    if split is None:
        return plan_git._commit(repo, target), target, None, None
    remote, branch = split
    if branch == "HEAD":
        resolved = _symbolic_head(repo, remote)
        if resolved:
            branch = resolved
        else:
            return (plan_git._commit(repo, _REMOTE_REFS + remote + "/HEAD"),
                    remote + "/HEAD", "HEAD", remote)
    tracking = _REMOTE_REFS + remote + "/" + branch
    found = plan_git._commit(repo, tracking)
    if found:
        return found, remote + "/" + branch, branch, remote
    local = plan_git._commit(repo, _HEADS_REFS + branch)
    if local:
        return local, target, branch, remote
    return plan_git._commit(repo, target), target, None, None


def published_tip(repo: str, remote: str, branch: str) -> str | None:
    """La pointe publiée de `branch` sur `remote`, lue par `git ls-remote` :
    lecture seule, rien n'est écrit dans `.git`, aucun `fetch`. None si elle
    est illisible (réseau, droits, branche absente) : la cible n'est alors pas
    certifiée et la sonde des migrations se tait (#1333). `branch == "HEAD"`
    certifie la pointe par défaut du remote."""
    spec = "HEAD" if branch == "HEAD" else _HEADS_REFS + branch
    try:
        proc = _git(repo, "ls-remote", "--exit-code", remote, spec,
                    timeout=LS_REMOTE_TIMEOUT)
    except GitError:
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.decode("utf-8", "replace").split()
    return parts[0] if parts and plan_git._SHA_RE.match(parts[0]) else None


# --------------------------------------------------------------------------
# un passage
# --------------------------------------------------------------------------

class Cache:
    """Résultats déjà calculés, par commits : une paire est refaite si l'une
    de ses pointes ou la cible qui fournit ses attributs a bougé."""

    def __init__(self) -> None:
        self.merges: dict = {}
        self.diffs: dict = {}
        # L160d: change sizes for the resolver, replays of prepared resolutions
        self.sizes: dict = {}
        self.replays: dict = {}
        # L160e: stacked replays of the merge queues; merge leases held (L160e2)
        self.stacks: dict = {}
        self.locks: dict = {}

    def trim(self) -> None:
        for table in (self.merges, self.diffs, self.sizes, self.replays, self.stacks):
            while len(table) > MAX_CACHE:
                table.pop(next(iter(table)))


def scan(repo: str, lots: list[dict], *, default_target: str | None = None,
         migrations: str = "", cache: Cache | None = None,
         verify=None, reservations=None) -> dict:
    """Le radar sur un dépôt. `lots` : `{id, branch, branch_target?, title?,
    assignee?}`. Rend le rapport (sans horodatage ni durée).

    `verify` (facultatif) : `(dépôt, remote, branche) -> pointe publiée | None`,
    appelée pour une cible qui nomme une branche afin de certifier la ref
    lue avant la sonde des migrations (`run_pass` passe `published_tip`,
    lecture seule). Une certification qui échoue ou constate une pointe non
    récupérée laisse la cible non certifiée : la sonde se tait. Sans
    `verify`, la ref de la branche est crue telle quelle.

    Un dépôt superficiel suspend la sonde des migrations : `merge-base` peut
    y rendre une frontière fausse, et le delta d'une branche contiendrait
    alors les migrations de la cible (#1333).

    `reservations` : `branches -> liste`, appelé sur les cibles certifiées
    quand les entrées portent encore `added` (les migrations ajoutées par un lot) ; seul son résultat
    entre au rapport, `added` ne sort jamais dans le journal (#1335)."""
    cache = cache if cache is not None else Cache()
    objects = plan_git._out(repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "objects")
    if not objects:
        raise GitError("%s is not a readable git repository" % repo)
    if default_target is None:
        default_target = plan_git.default_target(repo)
    # #1333: dans un clone superficiel, `merge-base` peut rendre une frontière
    # fausse (ou rien) : le delta d'une branche contiendrait alors les
    # migrations de la cible. Conservateur : la sonde se tait.
    shallow = plan_git._out(repo, "rev-parse", "--is-shallow-repository") == "true"
    remotes = _remotes(repo)
    branches: list[dict] = []
    skipped: list[dict] = []
    verified: dict = {}
    for row in lots:
        branch = row.get("branch")
        if not branch:
            continue
        entry = {"work_item": int(row["id"]), "branch": branch,
                 "title": row.get("title") or "", "assignee": row.get("assignee") or None}
        tip = plan_git._tip(repo, branch)
        if tip is None:
            continue                      # branche d'un autre dépôt, ou pas encore poussée
        target = row.get("branch_target") or default_target
        head, head_ref, published_branch, published_remote = (
            target_commit(repo, target, remotes) if target
            else (None, target or "", None, None))
        if head is None:
            skipped.append(dict(entry, reason="target %s not found" % (target or "—")))
            continue
        certified, reason = True, ""
        if migrations and shallow:
            certified = False
            reason = "shallow repository (history is incomplete)"
        elif published_branch and verify is not None:
            published_ref = "%s/%s" % (published_remote, published_branch)
            key = (published_remote, published_branch)
            if key not in verified:
                try:
                    verified[key] = verify(repo, published_remote, published_branch)
                except Exception:  # noqa: BLE001 - une sonde ne fait jamais échouer le passage
                    verified[key] = None
            published = verified[key]
            if not published:
                certified = False
                reason = "%s is unreadable (network, rights or no fetch)" \
                    % published_ref
            elif published != head:
                found = plan_git._commit(repo, published)
                if found is None:
                    certified = False
                    reason = ("%s is at %s, %s publishes %s (not fetched)"
                              % (published_ref, head[:12], published_remote,
                                 published[:12]))
                else:
                    head, head_ref = found, published
        base = plan_git._out(repo, "merge-base", tip, head)
        if not base:
            skipped.append(dict(entry, reason="no common base with %s" % target))
            continue
        if base == tip:
            continue                      # aucun travail propre : rien à fusionner
        key = (base, tip)
        if key not in cache.diffs:
            cache.diffs[key] = _changed(repo, base, tip)
        touched, added = cache.diffs[key]
        one = dict(entry, target=target, target_head=head, target_ref=head_ref,
                   target_certified=certified, tip=tip, base=base,
                   files=touched, added=added)
        if not certified:
            one["target_reason"] = reason
        branches.append(one)

    pairs: list[dict] = []
    against: list[dict] = []
    checked = 0
    scratch = tempfile.mkdtemp(prefix="ameesh-radar-")
    try:
        def merge(a: str, b: str, attributes_tree: str):
            key = (attributes_tree, *sorted((a, b)))
            if key not in cache.merges:
                cache.merges[key] = trial_merge(
                    repo, objects, scratch, a, b, attributes_tree=attributes_tree)
            return cache.merges[key]

        for one in branches:
            conflicts = merge(one["target_head"], one["tip"], one["target_head"])
            against.append({"work_item": one["work_item"], "branch": one["branch"],
                            "target": one["target"], "tip": one["tip"],
                            "target_head": one["target_head"],
                            "state": "error" if conflicts is None
                            else "conflict" if conflicts else "clean",
                            "files": conflicts or []})
        for i, one in enumerate(branches):
            for other in branches[i + 1:]:
                if one["target"] != other["target"]:
                    continue
                checked += 1
                shared = sorted(set(one["files"]) & set(other["files"]))
                if not shared:
                    continue
                conflicts = merge(one["tip"], other["tip"], one["target_head"])
                pairs.append({"lots": [one["work_item"], other["work_item"]],
                              "branches": [one["branch"], other["branch"]],
                              "tips": [one["tip"], other["tip"]], "target": one["target"],
                              "state": "error" if conflicts is None
                              else "conflict" if conflicts else "overlap",
                              "shared": shared, "files": conflicts or []})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    cache.trim()
    uncertified: dict = {}
    for one in branches:
        if not one.get("target_certified", True):
            uncertified.setdefault((one["target"], one["target_ref"]),
                                   one.get("target_reason") or "")
    report = {"repo": repo, "branches": [{k: v for k, v in b.items() if k != "added"}
                                         for b in branches],
              "skipped": skipped, "pairs_checked": checked, "pairs": pairs,
              "against_target": against,
              "uncertified_targets": [{"target": target, "ref": ref, "reason": why}
                                      for (target, ref), why in sorted(uncertified.items())],
              "migrations": migration_probe(repo, branches, migrations) if migrations else []}
    if migrations and shallow:
        report["migration_probe"] = "suspended: shallow repository (history is incomplete)"
    if reservations is not None:
        report["unreserved_migrations"] = reservations(
            [b for b in branches if b.get("target_certified", True)])
    return report


def migration_probe(repo: str, branches: list[dict], directory: str) -> list[dict]:
    """Numéros de migration en double : ajoutés par deux lots de même cible,
    ou par un lot alors que sa cible a déjà ce numéro sous un autre nom. Un
    lot dont la cible n'est pas certifiée est ignoré : son delta peut
    contenir les migrations de la cible (#1333)."""
    by_target: dict = {}
    in_target: dict = {}
    for one in branches:
        if not one.get("target_certified", True):
            continue
        if one["target_head"] not in in_target:
            in_target[one["target_head"]] = _numbers_in(repo, one["target_head"], directory)
        for path in one.get("added") or ():
            if os.path.dirname(path) != directory:
                continue
            match = MIGRATION_RE.match(os.path.basename(path))
            if match:
                by_target.setdefault((one["target"], one["target_head"]), {}).setdefault(
                    match.group(1), []).append((one["work_item"], one["branch"], path))
    found = []
    for (target, head), numbers in sorted(by_target.items()):
        for number, holders in sorted(numbers.items()):
            existing = in_target[head].get(number)
            existing = existing if existing and existing not in {h[2] for h in holders} \
                else None
            if len(holders) < 2 and not existing:
                continue
            found.append({"number": number, "target": target, "in_target": existing,
                          "lots": [{"work_item": lot, "branch": branch, "path": path}
                                   for lot, branch, path in holders]})
    return found


def _states(report: dict) -> dict:
    """{clé: état} d'un rapport, pour comparer deux passages."""
    out = {}
    for pair in report.get("pairs") or ():
        out["pair:%d-%d" % tuple(pair["lots"])] = pair["state"]
    for one in report.get("against_target") or ():
        out["target:%d" % one["work_item"]] = one["state"]
    for dup in report.get("migrations") or ():
        out["migration:%s:%s" % (dup["target"], dup["number"])] = "duplicate"
    for one in report.get("unreserved_migrations") or ():
        out["unreserved:%s:%s" % (one["work_item"], one["path"])] = "unreserved"
    return out


def changes(before: dict | None, after: dict) -> list[dict]:
    """Les changements d'état d'un passage au suivant (`clean` : disparu)."""
    old, new = _states(before or {}), _states(after)
    return [{"key": key, "from": old.get(key, "clean"), "to": new.get(key, "clean")}
            for key in sorted(set(old) | set(new))
            if old.get(key, "clean") != new.get(key, "clean")]


def summary(report: dict) -> dict:
    pairs = report.get("pairs") or ()
    return {"branches": len(report.get("branches") or ()),
            "pairs_checked": report.get("pairs_checked", 0),
            "overlap": sum(1 for p in pairs if p["state"] == "overlap"),
            "conflict": sum(1 for p in pairs if p["state"] == "conflict"),
            "prepared": sum(1 for p in pairs if p["state"] == "prepared"),
            "stale": sum(1 for p in pairs if p["state"] == "stale"),
            "target_conflict": sum(1 for a in report.get("against_target") or ()
                                   if a["state"] == "conflict"),
            "migration_duplicates": len(report.get("migrations") or ()),
            "unreserved_migrations": len(report.get("unreserved_migrations") or ()),
            "uncertified_targets": len(report.get("uncertified_targets") or ())}


def read_snapshot(cfg, project: str) -> dict | None:
    try:
        with open(os.path.join(journal_dir(cfg), project + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_journal(cfg, project: str, report: dict) -> dict:
    """Écrit le dernier passage (atomique) et ajoute sa ligne au journal ;
    rend la ligne."""
    directory = journal_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    line = {"ts": report["ts"], "duration_s": report["duration_s"],
            "summary": summary(report), "error": report.get("error"),
            "changes": changes(read_snapshot(cfg, project), report)}
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".%s." % project)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(directory, project + ".json"))
    with open(os.path.join(directory, project + ".jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def run_pass(cfg, db, *, cache: Cache | None = None, settings: Settings | None = None,
             now=time.time, ci="github", owner: str | None = None) -> list[dict]:
    """Un passage du radar sur les projets déclarés par l'hôte ; rend la ligne
    de journal de chacun, avec ses envois (`mail`, L160c). `ci` : la fonction
    `branche -> {number, green} | None`, par défaut lue sur GitHub."""
    from . import storage
    settings = settings if settings is not None else settings_of(cfg)
    if not settings.projects:
        return []
    lots = [r for r in storage.of(db).work.open_for_sweep(plan_git.MAX_BRANCH_LOTS)
            if r.get("branch")]
    lines = []
    for project in settings.projects.values():
        started = now()
        try:
            from .migration_reservations import probe
            missing_repo = bool(project.migrations) and not github_slug(project)
            report = scan(
                project.repo, lots, default_target=project.target or None,
                migrations=project.migrations, cache=cache,
                verify=published_tip if project.migrations else None,
                reservations=None if missing_repo
                else (lambda raw: probe(db, project, raw)))
            if missing_repo:
                report["reservation_error"] = (
                    "migration reservations need a GitHub repository identity")
            error = None
        except (GitError, plan_git.MergeProbeError, OSError) as exc:
            report, error = {"repo": project.repo}, "%s : %s" % (type(exc).__name__, exc)
        if not error:
            annotate_resolutions(cfg, db, project, report, lots, cache)
            report["queue"] = run_queue(cfg, db, project, report, cache)
            if project.land != "off":
                report["land"] = run_land(cfg, db, project, report, cache, owner)
        report.update(schema=SCHEMA, project=project.name, ts=started,
                      duration_s=round(now() - started, 3), error=error)
        line = write_journal(cfg, project.name, report)
        sent = [] if error else send_mail(cfg, db, project, report, ci=ci)
        lines.append(dict(line, project=project.name, mail=sent, land=report.get("land") or []))
    return lines


def clone_dir(cfg, project: str) -> str:
    """The radar's private clone of a project (L160d)."""
    return os.path.join(journal_dir(cfg), project + ".clone")


def annotate_resolutions(cfg, db, project: Project, report: dict, lots: list[dict],
                         cache: Cache | None) -> None:
    """L160d: resolver of each pair in conflict, replay of the prepared
    resolutions (`ameesh.resolutions.annotate`). Never raises: an error is
    noted in the report and the pairs stay `conflict`."""
    from . import resolutions, storage
    conflicted = sorted({n for p in report.get("pairs") or () if p["state"] == "conflict"
                         for n in p["lots"]})
    if not conflicted:
        return
    try:
        rows = storage.of(db).work.resolutions(conflicted)
    except Exception as exc:  # noqa: BLE001 - e.g. database not yet migrated (0057)
        rows = []
        report["resolution_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        resolutions.annotate(report, rows, {int(r["id"]): r for r in lots},
                             replayer=resolutions.Replayer(project.repo,
                                                           clone_dir(cfg, project.name)),
                             cache=cache,
                             generated=frozenset(g.path for g in project.generated))
    except Exception as exc:  # noqa: BLE001 - never fatal for the pass
        report["resolution_error"] = "%s: %s" % (type(exc).__name__, exc)


def run_queue(cfg, db, project: Project, report: dict, cache: Cache | None) -> dict:
    """L160e: order and stacked replay of the merge queues of the project
    (`ameesh.merge_queue.run_pass`). Never raises."""
    from . import merge_queue
    try:
        return merge_queue.run_pass(cfg, db, project, report, cache=cache)
    except Exception as exc:  # noqa: BLE001 - never fatal for the pass
        return {"targets": {}, "mail": [], "error": "%s: %s" % (type(exc).__name__, exc)}


def run_land(cfg, db, project: Project, report: dict, cache: Cache | None,
             owner: str | None) -> list[dict]:
    """L160e2: ameesh merges the head of each queue (`ameesh.land.run_pass`).
    Never raises."""
    from . import land
    owner = owner or "%s:%d" % (socket.gethostname(), os.getpid())
    try:
        return land.run_pass(cfg, db, project, report, owner=owner,
                             locks=cache.locks if cache is not None else None)
    except Exception as exc:  # noqa: BLE001 - never fatal for the pass
        return [{"result": "error", "error": "%s: %s" % (type(exc).__name__, exc)}]


# --------------------------------------------------------------------------
# courrier (L160c) : passif sur conflit nouveau, réveil seulement s'il bloque
# --------------------------------------------------------------------------

def github_slug(project: Project) -> str | None:
    """Le dépôt GitHub du projet : `github`, sinon l'URL `origin` du clone."""
    if project.github:
        return project.github
    url = plan_git._out(project.repo, "remote", "get-url", "origin") or ""
    match = _ORIGIN_RE.search(url.strip())
    return match.group(1) if match else None


def ci_green(rollup) -> bool:
    """`statusCheckRollup` d'une PR : vert si au moins un check, et tous
    terminés sans échec."""
    if not rollup:
        return False
    for check in rollup:
        if check.get("__typename") == "StatusContext" or "state" in check \
                and "conclusion" not in check:
            if (check.get("state") or "").upper() != "SUCCESS":
                return False
        elif (check.get("status") or "").upper() != "COMPLETED" \
                or (check.get("conclusion") or "").upper() not in CI_OK:
            return False
    return True


def github_ci(project: Project, gh=None, *, strict: bool = False):
    """La fonction `branche -> {number, url, head, green, draft} | None` du
    projet (PR ouverte de la branche, `head` : son commit de tête), lue avec
    `gh` ; None si le dépôt GitHub est inconnu. Une erreur de `gh` vaut « pas
    verte » : le courrier reste passif. `strict` (entrée en file, L160e) :
    l'erreur est levée (`plan_github.GithubError`), jamais prise pour « pas
    de PR ». `draft` (L539) : un lot léger ne part que d'une PR non brouillon."""
    slug = github_slug(project)
    if not slug:
        return None
    from . import plan_github

    def status(branch: str):
        nonlocal gh
        try:
            gh = gh or plan_github.Gh()
            rows = plan_github._json(gh, ["pr", "list", "--repo", slug, "--head", branch,
                                          "--state", "open", "--json",
                                          "number,url,headRefOid,statusCheckRollup,isDraft"]) or []
        except plan_github.GithubError:
            if strict:
                raise
            return None
        if not rows:
            return None
        return {"number": rows[0].get("number"), "url": rows[0].get("url"),
                "head": rows[0].get("headRefOid"),
                "green": ci_green(rows[0].get("statusCheckRollup")),
                "draft": bool(rows[0].get("isDraft"))}
    return status


#: L160g (#473): conclusions of a workflow run that do not block
CI_PASSING = ("success", "skipped", "neutral")


def commit_ci(project: Project, sha: str, gh=None) -> dict:
    """L160g (#473, `ci_required`): GitHub's CI on the commit `sha` itself
    (Actions, external app checks and commit statuses), the latest activity
    of each workflow database ID: `{runs,
    green, pending: [workflows], failed: [workflows], contexts: {label:
    success|pending|failed}}`; green needs at least one check, all concluded
    and passing. Raises `plan_github.GithubError` when it cannot be read
    (never taken for « no CI »).

    L1651 (#1650): `contexts` carries the same reading label by label, so the
    gate can prove that a part the change set makes due **exists** on the
    frozen commit; a `skipped` run or check is `success` for the rollup (it
    blocks nothing) but `failed` here — a due part that did not run is not a
    green one."""
    from datetime import datetime
    from . import plan_github
    slug = github_slug(project)
    if not slug:
        raise plan_github.GithubError("project %s declares no GitHub repository" % project.name)
    gh = gh or plan_github.Gh()
    rows = plan_github._json(gh, ["run", "list", "--repo", slug, "--commit", sha, "--limit",
                                  "100", "--json",
                                  "workflowDatabaseId,workflowName,name,status,conclusion,createdAt,startedAt,updatedAt,headSha"])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise plan_github.GithubError("gh run list: unexpected output")
    if len(rows) >= 100:
        raise plan_github.GithubError("gh run list: result may be truncated; cannot certify frozen CI")
    if any(r.get("headSha") != sha for r in rows):
        raise plan_github.GithubError("gh run list: a run does not match the frozen commit")
    latest: dict = {}
    times: dict = {}
    for run in rows:
        identity = run.get("workflowDatabaseId")
        if type(identity) is not int or identity <= 0:
            raise plan_github.GithubError("gh run list: missing or invalid workflow identity")
        try:
            stamps = [datetime.fromisoformat(run[field].replace("Z", "+00:00"))
                      for field in ("createdAt", "startedAt", "updatedAt")]
            if any(t.utcoffset() is None for t in stamps):
                raise ValueError("timestamp lacks timezone")
            at = max(stamps)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise plan_github.GithubError("gh run list: missing or invalid run timestamp") from exc
        if identity in times and at == times[identity] and run != latest[identity]:
            raise plan_github.GithubError("gh run list: ambiguous latest run for workflow %s" % identity)
        if identity not in times or at > times[identity]:
            times[identity], latest[identity] = at, run

    def label(identity, run):
        return run.get("workflowName") or run.get("name") or str(identity)

    pending = sorted(label(n, r) for n, r in latest.items() if (r.get("status") or "") != "completed")
    failed = sorted(label(n, r) for n, r in latest.items() if (r.get("status") or "") == "completed"
                    and (r.get("conclusion") or "").lower() not in CI_PASSING)
    # L1651 (#1650): what the frozen commit carries, label by label. A due
    # part is proven by a context that exists here and is terminal-success; a
    # part absent from this map was never dispatched, which is exactly the
    # hole a non-empty rollup of green statuses used to hide.
    contexts: dict = {}

    def note(name: str, state: str) -> None:
        """Le plus fort des états d'un même libellé l'emporte : un contexte
        vert quelque part ne rachète pas le même libellé rouge ailleurs."""
        if CI_CONTEXT_RANK[state] > CI_CONTEXT_RANK.get(contexts.get(name), -1):
            contexts[name] = state

    for identity, run in latest.items():
        note(label(identity, run), "pending" if (run.get("status") or "") != "completed"
             else ("success" if (run.get("conclusion") or "").lower() in CI_DUE_OK else "failed"))
    # Actions alone cannot certify a commit: GitHub Apps and classic commit
    # statuses can also block it. Read the same exact SHA, never the PR head.
    checks = plan_github._json(gh, ["api", "repos/%s/commits/%s/check-runs?filter=all&per_page=100"
                                   % (slug, sha)])
    if (not isinstance(checks, dict) or type(checks.get("total_count")) is not int
            or not isinstance(checks.get("check_runs"), list)
            or checks["total_count"] != len(checks["check_runs"])
            or checks["total_count"] >= 100):
        raise plan_github.GithubError("commit checks: malformed or truncated response")
    external = {}
    activity = {}
    for check in checks["check_runs"]:
        if (not isinstance(check, dict) or check.get("head_sha") != sha
                or not isinstance(check.get("app"), dict)
                or type(check["app"].get("id")) is not int
                or type(check.get("id")) is not int or not check.get("name")):
            raise plan_github.GithubError("commit checks: invalid identity or frozen SHA")
        if check["app"].get("slug") == "github-actions":
            continue  # workflow attempts above account for all their jobs
        try:
            stamps = [datetime.fromisoformat(check["started_at"].replace("Z", "+00:00"))]
            if check.get("status") == "completed" or check.get("completed_at") is not None:
                stamps.append(datetime.fromisoformat(check["completed_at"].replace("Z", "+00:00")))
            if any(t.utcoffset() is None for t in stamps):
                raise ValueError("timestamp lacks timezone")
            if len(stamps) == 2 and stamps[1] < stamps[0]:
                raise ValueError("completion precedes start")
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise plan_github.GithubError("commit checks: missing or invalid activity timestamp") from exc
        key = (check["app"]["id"], check["name"])
        # An unfinished attempt must not be hidden by any completed check.
        at = (check.get("status") != "completed", max(stamps))
        if key in activity and at == activity[key] and check != external[key]:
            raise plan_github.GithubError("commit checks: ambiguous latest activity for %s/%s" % key)
        if key not in activity or at > activity[key]:
            activity[key], external[key] = at, check
    for check in external.values():
        label = "%s/%s" % (check["app"]["id"], check["name"])
        if check.get("status") != "completed":
            pending.append(label)
            note(label, "pending")
        elif (check.get("conclusion") or "").lower() not in CI_PASSING:
            failed.append(label)
            note(label, "failed")
        else:
            note(label, "success" if (check.get("conclusion") or "").lower() in CI_DUE_OK
                 else "failed")
    statuses = plan_github._json(gh, ["api", "repos/%s/commits/%s/status?per_page=100"
                                     % (slug, sha)])
    if (not isinstance(statuses, dict) or statuses.get("sha") != sha
            or type(statuses.get("total_count")) is not int
            or not isinstance(statuses.get("statuses"), list)
            or statuses["total_count"] != len(statuses["statuses"])
            or statuses["total_count"] >= 100):
        raise plan_github.GithubError("commit statuses: malformed, truncated or wrong SHA response")
    for status in statuses["statuses"]:
        if not isinstance(status, dict) or not status.get("context"):
            raise plan_github.GithubError("commit statuses: invalid context")
        if status.get("state") == "pending":
            pending.append(status["context"])
            note(status["context"], "pending")
        elif status.get("state") != "success":
            failed.append(status["context"])
            note(status["context"], "failed")
        else:
            note(status["context"], "success")
    count = len(latest) + len(external) + len(statuses["statuses"])
    return {"runs": count, "green": bool(count) and not pending and not failed,
            "pending": sorted(pending), "failed": sorted(failed), "contexts": contexts}


#: L756 (#566): les déclencheurs d'une exécution de la suite **complète** —
#: une poussée (main, release/**, selon le workflow du projet) ou un lancement
#: manuel. Une PR, elle, peut n'avoir exécuté que le plan impacté, ou rien du
#: tout quand elle ne change qu'une version : elle ne certifie jamais une
#: release.
FULL_SUITE_EVENTS = ("push", "workflow_dispatch")


def _full_suite_ids(gh, slug: str, declared) -> dict:
    """L756 (#566): les identités (`id` de workflow) de la suite déclarée.

    Le dépôt est lu une fois (`repos/<slug>/actions/workflows`) : un chemin
    déclaré absent, désactivé ou dupliqué lève `GithubError` — jamais « pas de
    suite ». Un nom d'affichage n'est pas une identité (il se renomme) ; le
    fichier l'est, résolu ici en identité stable."""
    from . import plan_github
    data = plan_github._json(gh, ["api", "repos/%s/actions/workflows?per_page=100" % slug])
    if (not isinstance(data, dict) or type(data.get("total_count")) is not int
            or not isinstance(data.get("workflows"), list)
            or data["total_count"] != len(data["workflows"])
            or data["total_count"] >= 100):
        raise plan_github.GithubError("workflows: malformed or truncated response")
    by_path: dict = {}
    for workflow in data["workflows"]:
        if (not isinstance(workflow, dict) or type(workflow.get("id")) is not int
                or workflow["id"] <= 0 or not isinstance(workflow.get("path"), str)
                or not workflow["path"]):
            raise plan_github.GithubError("workflows: invalid workflow identity")
        by_path[workflow["path"]] = workflow
    out: dict = {}
    for path in declared:
        workflow = by_path.get(path)
        if workflow is None:
            raise plan_github.GithubError("workflows: the declared full suite %s is not a "
                                          "workflow of %s" % (path, slug))
        if (workflow.get("state") or "") != "active":
            raise plan_github.GithubError("workflows: the declared full suite %s is %s"
                                          % (path, workflow.get("state") or "unknown"))
        out[path] = workflow["id"]
    return out


def _run_jobs(gh, run: dict, slug: str) -> list:
    """L756 (#566): les jobs d'une exécution, pour refuser un vert sans tests.

    Rend `[(nom, statut, conclusion)]`. Une lecture impossible, vide ou
    tronquée lève `GithubError` : un job qu'on ne peut pas lire ne vaut jamais
    vert. Le dépôt est nommé (`--repo`), jamais déduit du répertoire courant :
    `Gh.run` ne fixe ni `cwd` ni `GH_REPO`, et `gh run view` sans `--repo`
    échoue hors d'un clone (« failed to determine base repo ») — R2 de
    mty-amh-qa-01 sur PR manaty/ameesh#633."""
    from . import plan_github
    run_id = run.get("databaseId")
    if type(run_id) is not int or run_id <= 0:
        raise plan_github.GithubError("gh run view: missing or invalid run id")
    data = plan_github._json(gh, ["run", "view", str(run_id), "--repo", slug, "--json", "jobs"])
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or not data["jobs"]:
        raise plan_github.GithubError("gh run view: no readable job for run %s" % run_id)
    if len(data["jobs"]) >= 100:
        raise plan_github.GithubError("gh run view: result may be truncated; cannot certify "
                                      "the full suite")
    out = []
    for job in data["jobs"]:
        if (not isinstance(job, dict) or not isinstance(job.get("name"), str)
                or not job["name"] or not isinstance(job.get("status"), str)):
            raise plan_github.GithubError("gh run view: invalid job")
        out.append((job["name"], job["status"], (job.get("conclusion") or "").lower()))
    return out


def _job_matches(required: str, job_name: str) -> bool:
    """L756 B3 (#566): a declared job name matches exactly; a trailing " *"
    declares a matrix family — the name, a space, then the rendered values
    (`psycopg *` covers `psycopg 1/4`). No other wildcard, so `tests` never
    catches the flow check runs `tests (flux) …`."""
    if required.endswith(" *"):
        prefix = required[:-2]
        return job_name == prefix or job_name.startswith(prefix + " ")
    return job_name == required


def _suite_jobs_of(project, path: str, attribute: str):
    """The jobs a declared suite entry names for `path`, or `None` — a bare
    path entry declares no required jobs (all of them are required) and none
    tolerated."""
    for declared_path, jobs in getattr(project, attribute, ()) or ():
        if declared_path == path:
            return tuple(jobs)
    return None


def full_suite_ci(project: Project, sha: str, gh=None) -> dict:
    """L756 (#566): une exécution de la suite complète **déclarée** par le
    projet, verte, sur `sha`, jobs compris.

    La suite complète n'est pas « un workflow quelconque devenu vert » : le
    projet la déclare par ses fichiers (`radar.projects.<p>.full_suite`),
    résolus en identités de workflow. Chacune doit avoir, sur le commit exact,
    une exécution `push` ou `workflow_dispatch` terminée, de conclusion
    `success`. Une entrée `{path = …, jobs = […], tolerate = […]}` nomme les
    **jobs requis** : chacun doit exister et être vert, toutes ses instances de
    matrice comprises (`psycopg *` couvre `psycopg 1/4`, `psycopg 2/4`, … ;
    sans `*` le nom est exact) ; les autres jobs doivent être `success` ou
    `skipped` — un job sauté par conception (`if:` sur l'événement ou les
    chemins) ne bloque jamais, et un job **toléré** (`tolerate`, le `bilan des
    impactés` en `continue-on-error`) ne certifie ni ne bloque. Une entrée
    réduite au chemin exige **tous** les jobs (`skipped`, `neutral` ou
    illisible ne certifient rien). Une suite déclarée qui n'a pas tourné n'est
    jamais verte
    (le plan impacté d'une PR, lui, ne compte jamais). Rend `{runs, suites,
    green, pending, failed, missing}` ; une lecture impossible lève
    `plan_github.GithubError`, jamais « pas de suite complète »."""
    from datetime import datetime
    from . import plan_github
    slug = github_slug(project)
    if not slug:
        raise plan_github.GithubError("project %s declares no GitHub repository" % project.name)
    declared = tuple(getattr(project, "full_suite", ()) or ())
    if not declared:
        raise plan_github.GithubError("project %s declares no full suite "
                                      "(radar.projects.%s.full_suite)"
                                      % (project.name, project.name))
    gh = gh or plan_github.Gh()
    suites = _full_suite_ids(gh, slug, declared)
    rows = plan_github._json(gh, ["run", "list", "--repo", slug, "--commit", sha, "--limit",
                                  "100", "--json",
                                  "databaseId,workflowDatabaseId,workflowName,name,status,"
                                  "conclusion,event,headSha,createdAt,startedAt,updatedAt"])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise plan_github.GithubError("gh run list: unexpected output")
    if len(rows) >= 100:
        raise plan_github.GithubError("gh run list: result may be truncated; cannot certify "
                                      "the full suite")
    latest: dict = {}
    times: dict = {}
    for run in rows:
        if run.get("headSha") != sha:
            raise plan_github.GithubError("gh run list: a run does not match the frozen commit")
        if run.get("event") not in FULL_SUITE_EVENTS:
            continue
        identity = run.get("workflowDatabaseId")
        if type(identity) is not int or identity <= 0:
            raise plan_github.GithubError("gh run list: missing or invalid workflow identity")
        if identity not in suites.values():
            continue  # une suite non déclarée ne certifie rien (dispatch docs-only)
        try:
            stamps = [datetime.fromisoformat(run[field].replace("Z", "+00:00"))
                      for field in ("createdAt", "startedAt", "updatedAt")]
            if any(t.utcoffset() is None for t in stamps):
                raise ValueError("timestamp lacks timezone")
            at = max(stamps)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise plan_github.GithubError("gh run list: missing or invalid run timestamp") from exc
        if identity in times and at == times[identity] and run != latest[identity]:
            raise plan_github.GithubError("gh run list: ambiguous latest run for workflow %s"
                                          % identity)
        if identity not in times or at > times[identity]:
            times[identity], latest[identity] = at, run

    pending: list = []
    failed: list = []
    missing: list = []
    for path, identity in suites.items():
        run = latest.get(identity)
        if run is None:
            missing.append(path)
            continue
        name = run.get("workflowName") or run.get("name") or path
        if (run.get("status") or "") != "completed":
            pending.append(name)
            continue
        if (run.get("conclusion") or "").lower() != "success":
            failed.append(name)
            continue
        required = _suite_jobs_of(project, path, "full_suite_required")
        tolerated = () if required is None else (
            _suite_jobs_of(project, path, "full_suite_tolerated") or ())
        jobs = _run_jobs(gh, run, slug)
        if required is None:
            # entrée sans jobs déclarés : tous les jobs de l'exécution sont requis
            for job_name, status, conclusion in jobs:
                if status != "completed":
                    pending.append("%s/%s" % (name, job_name))
                elif conclusion != "success":
                    failed.append("%s/%s" % (name, job_name))
            continue
        seen = dict.fromkeys(required, 0)
        for job_name, status, conclusion in jobs:
            hit = next((req for req in required if _job_matches(req, job_name)), None)
            if hit is None:
                if any(_job_matches(tol, job_name) for tol in tolerated):
                    # toléré : sa conclusion ne certifie ni ne bloque
                    if status != "completed":
                        pending.append("%s/%s" % (name, job_name))
                    continue
                # job non requis : vert ou sauté par conception, jamais neutral
                if status != "completed":
                    pending.append("%s/%s" % (name, job_name))
                elif conclusion not in ("success", "skipped"):
                    failed.append("%s/%s" % (name, job_name))
                continue
            seen[hit] += 1
            if status != "completed":
                pending.append("%s/%s" % (name, job_name))
            elif conclusion != "success":
                failed.append("%s/%s" % (name, job_name))
        for req, count in seen.items():
            if count == 0:
                failed.append("%s/%s (required job missing)" % (name, req))
    return {"runs": len(latest), "suites": len(suites),
            "green": bool(latest) and len(latest) == len(suites) and not pending
            and not failed and not missing,
            "pending": sorted(pending), "failed": sorted(failed), "missing": sorted(missing)}


def gates_for(project, files) -> tuple:
    """L160g (#473): the gates of a merge that touches `files`: the
    unconditional ones, then each path gate one of whose globs matches one
    of them (`fnmatch`: `*` also crosses `/`, so a gate runs at least
    whenever a workflow `paths` filter would). `files` None (unknown): every
    path gate runs."""
    import fnmatch
    out = list(project.gates)
    for gate in getattr(project, "path_gates", ()) or ():
        if files is None or any(fnmatch.fnmatchcase(f, glob)
                                for f in files for glob in gate.paths):
            out.append(gate.run)
    return tuple(out)


def conflicts_of(report: dict) -> dict:
    """{clé: conflit} d'un rapport : paires en conflit, lots en conflit avec
    leur cible, numéros de migration en double (mêmes clés que `_states`)."""
    out = {}
    for pair in report.get("pairs") or ():
        if pair["state"] == "conflict":
            out["pair:%d-%d" % tuple(pair["lots"])] = {
                "kind": "pair", "lots": list(pair["lots"]), "files": pair["files"],
                "target": pair["target"], "resolver": pair.get("resolver")}
        elif pair["state"] == "stale":
            # L160d: only the lot that prepared the resolution is told
            resolution = pair["resolution"]
            out["stale:%d-%d" % tuple(pair["lots"])] = {
                "kind": "stale", "lots": [resolution["by"]], "pair": list(pair["lots"]),
                "files": resolution.get("remaining") or pair["files"],
                "target": pair["target"], "commit": resolution["commit"]}
    for one in report.get("against_target") or ():
        if one["state"] == "conflict":
            out["target:%d" % one["work_item"]] = {
                "kind": "target", "lots": [one["work_item"]], "files": one["files"],
                "target": one["target"]}
    for dup in report.get("migrations") or ():
        out["migration:%s:%s" % (dup["target"], dup["number"])] = {
            "kind": "migration", "lots": sorted({h["work_item"] for h in dup["lots"]}),
            "files": [h["path"] for h in dup["lots"]], "target": dup["target"],
            "number": dup["number"], "in_target": dup["in_target"]}
    for one in report.get("unreserved_migrations") or ():
        out["unreserved:%s:%s" % (one["work_item"], one["path"])] = {
            "kind": "unreserved", "lots": [one["work_item"]], "files": [one["path"]],
            "target": one["target"], "number": one["number"], "repo": one["repo"],
            "holder": one.get("holder")}
    return out


def _files(files) -> str:
    files = list(files or ())
    return ", ".join(files[:6]) + (" …" if len(files) > 6 else "") if files else "—"


def _who(branches: dict, lot: int) -> str:
    one = branches.get(lot) or {}
    return "#%d \"%s\" (%s, %s)" % (lot, one.get("title") or "", one.get("branch") or "?",
                                     one.get("assignee") or "unassigned")


def message_text(conflict: dict, lot: int, branches: dict, *, ci=None,
                 merged=()) -> str:
    """Le courrier du radar pour l'assigné de `lot`."""
    mine = branches.get(lot) or {}
    head = "Conflict radar, lot #%d \"%s\"" % (lot, mine.get("title") or "")
    if ci is not None:
        return ("%s: your PR #%s is green but cannot merge: your branch %s "
                "conflicts with %s on %s%s. Merge %s into your branch and resolve the "
                "conflict." % (head, ci.get("number"), mine.get("branch") or "?",
                              conflict["target"], _files(conflict["files"]),
                              " (%s landed there)" % ", ".join("#%d" % n for n in merged)
                              if merged else "", conflict["target"]))
    if conflict["kind"] == "pair":
        other = [n for n in conflict["lots"] if n != lot][0]
        resolver = conflict.get("resolver") or {}
        if resolver.get("lot") == lot:
            todo = ("You prepare the resolution (%s), as you go: "
                    "`ameesh work resolve %d --with %d`; your branch does not change."
                    % (RESOLVER_REASONS.get(resolver.get("reason"), resolver.get("reason")),
                       lot, other))
        elif resolver.get("lot") == other:
            todo = ("Lot #%d prepares the resolution (%s): nothing to do for you."
                    % (other, RESOLVER_REASONS.get(resolver.get("reason"),
                                                   resolver.get("reason"))))
        else:
            todo = ("Whichever of the two lands in %s second will have to resolve."
                    % conflict["target"])
        return ("%s: your branch and the one of lot %s conflict on %s when merged. "
                "Nothing is blocked yet. %s You will be woken up if the other lot lands in "
                "%s before you and your PR is green."
                % (head, _who(branches, other), _files(conflict["files"]), todo,
                   conflict["target"]))
    if conflict["kind"] == "stale":
        other = [n for n in conflict["pair"] if n != lot][0]
        return ("%s: your resolution prepared with lot %s (%s) no longer covers the "
                "conflict, which changed on %s. Redo it: `ameesh work resolve %d --with %d` "
                "(unchanged hunks come back resolved)."
                % (head, _who(branches, other), conflict["commit"][:12],
                   _files(conflict["files"]), lot, other))
    if conflict["kind"] == "target":
        return ("%s: your branch %s no longer merges cleanly into %s (conflict on %s). "
                "Merge %s into your branch before your merge request; you will be woken up "
                "if your PR is green and still blocked."
                % (head, mine.get("branch") or "?", conflict["target"],
                   _files(conflict["files"]), conflict["target"]))
    if conflict["kind"] == "unreserved":
        return ("%s: SQL migration %s has no reservation for this lot in %s. "
                "Reserve it with `ameesh work migration %d %s` before freezing. %s"
                % (head, _files(conflict["files"]), conflict["repo"], lot,
                   conflict["number"], "Already held by lot #%s; choose a new number."
                   % conflict["holder"] if conflict.get("holder") else ""))
    others = [_who(branches, n) for n in conflict["lots"] if n != lot]
    return ("%s: migration number %s is duplicated (%s)%s%s. Take the first free "
            "number of %s and tell the other lot."
            % (head, conflict["number"], _files(conflict["files"]),
               "; also taken by lot %s" % ", ".join(others) if others else "",
               "; already in %s: %s" % (conflict["target"], conflict["in_target"])
               if conflict.get("in_target") else "", conflict["target"]))


#: why a lot is the resolver of a pair (L160d), for the mail
RESOLVER_REASONS = {
    "priority": "the other lot has a higher priority and will merge first",
    "state": "the other lot is further along and will merge first",
    "smaller change": "your change is the smallest in the conflicting files",
    "newer lot": "your lot is the most recent",
}


def _notified_path(cfg, project: str) -> str:
    return os.path.join(journal_dir(cfg), project + ".notified")


def read_notified(cfg, project: str) -> dict:
    """{clé: {lot: niveau}} des conflits déjà signalés (`passive`, `blocking`) ;
    pour un conflit avec la cible, `merged` : les lots fusionnés d'où il vient."""
    try:
        with open(_notified_path(cfg, project), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data.get("keys") or {} if isinstance(data, dict) else {}


def _write_notified(cfg, project: str, keys: dict) -> None:
    directory = journal_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".%s.notified." % project)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"schema": SCHEMA, "keys": keys}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _notified_path(cfg, project))


def _merged_partners(db, lot: int, keys, present) -> list[int]:
    """Les lots déjà signalés en conflit avec `lot` qui ont quitté le radar
    parce qu'ils sont fusionnés."""
    from . import work
    partners = set()
    for key in keys:
        if key.startswith("pair:"):
            pair = [int(n) for n in key[5:].split("-")]
            if lot in pair:
                partners.update(n for n in pair if n != lot and n not in present)
    merged = []
    for other in sorted(partners):
        row = work.get(db, other)
        if row and row.get("state") in work.MERGED_STATES:
            merged.append(other)
    return merged


def send_mail(cfg, db, project: Project, report: dict, *, ci="github") -> list[dict]:
    """Le courrier d'un passage : un message par lot concerné et par conflit
    nouveau (passif), puis un réveil quand un conflit avec la cible bloque
    une PR verte. Rend les envois `{key, lot, to, level, message_id}`."""
    from . import mail, work
    if ci == "github":
        ci = github_ci(project)
    before = read_notified(cfg, project.name)
    current = conflicts_of(report)
    branches = {b["work_item"]: b for b in report.get("branches") or ()}
    keys: dict = {}
    sent: list[dict] = []
    for key, conflict in sorted(current.items()):
        done = dict(before.get(key) or {})
        for lot in conflict["lots"]:
            one = branches.get(lot) or {}
            to = one.get("assignee")
            if not to or to.startswith(work.HUMAN_PREFIX):
                continue
            level, status = "passive", None
            if conflict["kind"] == "target" and done.get(str(lot)) != "blocking" and ci:
                status = ci(one["branch"])
                if status and status.get("green"):
                    level = "blocking"
            if done.get(str(lot)) in (level, "blocking"):
                continue
            if conflict["kind"] == "target" and "merged" not in done:
                # les lots fusionnés d'où vient le conflit, notés à sa première
                # vue : leurs paires quittent le radar avec eux
                done["merged"] = _merged_partners(db, lot, before, branches)
            data = {"project": project.name, "key": key, "kind": conflict["kind"],
                    "lots": conflict["lots"], "files": conflict["files"],
                    "target": conflict["target"]}
            if level == "blocking":
                data.update(pr=status.get("number"), merged=done.get("merged") or [])
            text = message_text(conflict, lot, branches,
                                ci=status if level == "blocking" else None,
                                merged=done.get("merged") or ())
            try:
                message_id = mail.send(
                    db, work.SYSTEM_SENDER, to, text, kind="event", work_item_id=str(lot),
                    payload={"radar_blocking" if level == "blocking" else "radar": data})
            except Exception:  # noqa: BLE001 - réessayé au passage suivant
                continue
            done[str(lot)] = level
            sent.append({"key": key, "lot": lot, "to": to, "level": level,
                         "message_id": message_id})
        if done:
            keys[key] = done
    if keys != before:
        _write_notified(cfg, project.name, keys)
    return sent


# --------------------------------------------------------------------------
# ameesh conflicts
# --------------------------------------------------------------------------

def _lot(n) -> str:
    return "#%d" % n


def render(report: dict, *, everything: bool = False) -> list[str]:
    s = summary(report)
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(report.get("ts") or 0))
    out = ["%s: pass of %s UTC (%.1f s), %d branch(es), %d pair(s) with the same target, "
           "%d overlapping, %d in conflict (%d prepared, %d stale), "
           "%d lot(s) in conflict with their target, %d duplicate migration number(s)"
           % (report.get("project"), when, report.get("duration_s") or 0, s["branches"],
              s["pairs_checked"], s["overlap"] + s["conflict"] + s["prepared"] + s["stale"],
              s["conflict"] + s["prepared"] + s["stale"], s["prepared"], s["stale"],
              s["target_conflict"], s["migration_duplicates"])]
    if report.get("error"):
        out.append("  error: %s" % report["error"])
    if report.get("resolution_error"):
        out.append("  resolutions: %s" % report["resolution_error"])
    if report.get("reservation_error"):
        out.append("  reservations: %s" % report["reservation_error"])
    if report.get("migration_probe"):
        out.append("  sonde des migrations %s" % report["migration_probe"])
    for one in report.get("uncertified_targets") or ():
        out.append("  cible non certifiée %s (%s) : %s — sonde des migrations suspendue"
                   % (one["target"], one["ref"], one["reason"]))
    for pair in report.get("pairs") or ():
        if pair["state"] == "overlap" and not everything:
            continue
        files = pair["shared"] if pair["state"] == "overlap" else pair["files"]
        resolver = pair.get("resolver") or {}
        resolution = pair.get("resolution") or {}
        out.append("  %-8s %s %s  %s%s%s" % (
            pair["state"], _lot(pair["lots"][0]), _lot(pair["lots"][1]),
            ", ".join(files[:6]) + (" …" if len(files) > 6 else ""),
            "  [resolver %s: %s]" % (_lot(resolver["lot"]), resolver["reason"])
            if resolver else "",
            "  [resolution %s by %s%s]" % (
                resolution["commit"][:12], _lot(resolution["by"]),
                ", remaining: %s" % ", ".join(resolution["remaining"][:6])
                if resolution.get("remaining") else
                ", replay %s" % resolution["replay"]
                if resolution.get("replay") not in ("prepared", "stale") else "")
            if resolution else ""))
    for one in report.get("against_target") or ():
        if one["state"] == "clean" and not everything:
            continue
        out.append("  %-8s %s against %s  %s" % (one["state"], _lot(one["work_item"]),
                                                one["target"], ", ".join(one["files"][:6])))
    for dup in report.get("migrations") or ():
        out.append("  migration %s: %s%s" % (
            dup["number"], ", ".join("%s (%s)" % (_lot(h["work_item"]), h["path"])
                                     for h in dup["lots"]),
            "; already in %s: %s" % (dup["target"], dup["in_target"])
            if dup["in_target"] else ""))
    for one in report.get("unreserved_migrations") or ():
        out.append("  unreserved migration %s: %s (%s, %s)%s" % (
            one["number"], _lot(one["work_item"]), one["repo"], one["path"],
            "; held by #%s" % one["holder"] if one.get("holder") else ""))
    queue = report.get("queue") or {}
    if queue.get("error"):
        out.append("  file : %s" % queue["error"])
    for target, listed in sorted((queue.get("targets") or {}).items()):
        out.append("  file vers %s : %s" % (target, ", ".join(
            "%d. %s%s" % (one["rank"], _lot(one["lot"]),
                          " (éjecté)" if one["state"] == "ejected" else
                          " (rejouée)" if (one.get("stack") or {}).get("state") == "replayed"
                          else "") for one in listed) or "vide"))
    for one in report.get("land") or ():
        out.append("  fusion   %s : %s%s" % (_lot(one["lot"]) if one.get("lot") else "—",
                                             one["result"], " (%s)" % one["error"]
                                             if one.get("error") else ""))
    for one in report.get("skipped") or ():
        out.append("  skipped  %s %s: %s" % (_lot(one["work_item"]), one["branch"],
                                             one["reason"]))
    return out


def cmd_conflicts(cfg, args) -> int:
    directory = journal_dir(cfg)
    try:
        names = sorted(n[:-5] for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        names = []
    if args.project:
        names = [n for n in names if n == args.project]
    reports = [r for r in (read_snapshot(cfg, n) for n in names) if r]
    if args.json:
        print(json.dumps({"schema": SCHEMA, "reports": reports}, ensure_ascii=False,
                         indent=1))
        return 0
    if not reports:
        print("no radar pass on this host (%s): the designated host declares "
              "`radar.projects` in its configuration%s"
              % (directory, ", project %s" % args.project if args.project else ""))
        return 0
    for report in reports:
        print("\n".join(render(report, everything=args.all)))
    return 0
