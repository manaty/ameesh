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
   `overlap`), puis attribution par fusions témoins si les bases diffèrent
   (`target-conflict`, `superseded` ou `unclassified` pour les conflits
   hérités ou incertains, #2104) ; chaque lot contre sa cible (`conflict` ou `clean`) ;
3. la sonde des migrations (si le projet déclare `migrations`) : un même
   numéro `NNNN_` ajouté par deux lots, ou par un lot alors que la cible l'a
   déjà sous un autre nom. Seules les migrations du **delta propre** du lot
   comptent (celles que la cible ne porte pas déjà) : une migration de la
   cible ramenée par une fusion dans la branche n'est à personne (#1333).
   Une copie identique héritée d'un autre lot suivi reste attribuée à ce
   constituant, sans doublon ni réservation supplémentaire pour le paquet (#2279).
   Un lot empilé sur une base qui porte déjà la migration (paquet, lot parent)
   ne l'ajoute pas, même quand la pointe du parent a avancé depuis
   l'empilement : la preuve est le merge-base des deux branches (#2388).

Les fusions d'essai n'écrivent rien dans le dépôt des agents : leurs objets
vont dans un dossier jetable (`GIT_OBJECT_DIRECTORY`, objets du dépôt en
alternates), aucun worktree, aucun `fetch`. Une paire n'est recalculée que si
l'une de ses deux pointes ou la cible a bougé (cache en mémoire de l'exécuteur).

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

`migrations_width` (facultatif) : chiffres des numéros de migration
(`014_x.sql` : 3) ; sinon celui d'un dépôt connu, sinon 4.

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
from .canon import GIT_TIMEOUT, NO_LAZY_FETCH, GitError, _git, _git_env

SCHEMA = "ameesh-radar/1"
SETTINGS_KEYS = ("projects",)
PROJECT_KEYS = ("repo", "migrations", "migrations_width", "target", "github", "land", "gates", "merge_method",
                "lease_ref", "generated", "ci_required", "protected", "full_suite", "ci_parts",
                "required_statuses", "required_checks", "production_branch")
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
    #: #4750 : chiffres des numéros de migration (`014_x.sql` : 3) ; 0 : celui
    #: d'un dépôt connu (`migrations.LAYOUTS`), sinon 4
    migrations_width: int = 0
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
    #: #1748 (a): `((branch glob, context prefix), …)` — a PR head (or frozen
    #: commit) of a matching branch is green only with at least one commit
    #: status of that prefix in `success` (Nexlink: `integration/**` → `lot/`)
    required_statuses: tuple = ()
    #: #2271 : les contextes de check requis, par nom exact. La protection de
    #: la branche cible est lue à la forge ; cette déclaration la complète et
    #: la remplace quand elle ne peut pas être lue (jamais pour l'affaiblir).
    required_checks: tuple = ()
    #: #3428 : la clé `required_checks` était **déclarée**, même vide. Une
    #: déclaration vide dit « aucune protection forge » et ne couvre qu'un 403
    #: de plan prouvé (`Upgrade to GitHub Pro…`) ; l'absence de clé garde le
    #: refus fail-closed. Ce drapeau distingue `[]` de l'absence.
    required_checks_declared: bool = False
    #: #3533: published ancestry excluded from packet author attribution.
    production_branch: str = ""


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
        for key in ("migrations", "target", "github", "production_branch"):
            text = value.get(key) or ""
            if not isinstance(text, str):
                raise SettingsError("%s.%s: text expected" % (where, key))
            texts[key] = text.strip().strip("/")
        width = value.get("migrations_width")
        if width is None:
            width = 0
        if isinstance(width, bool) or not isinstance(width, int) or not 0 <= width <= 9:
            raise SettingsError("%s.migrations_width: digits of a migration number (1-9) "
                                "expected" % where)
        if texts["target"]:
            plan_git.check_ref(texts["target"], "cible")
        if texts["production_branch"]:
            plan_git.check_ref(texts["production_branch"], "production_branch")
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
        required_checks = value.get("required_checks", ())
        if required_checks is None:
            required_checks = ()
        if isinstance(required_checks, str):
            required_checks = (required_checks,)
        if not isinstance(required_checks, (list, tuple)) or not all(
                isinstance(name, str) and name.strip() for name in required_checks):
            raise SettingsError("%s.required_checks: a list of check names expected" % where)
        # Un nom de check est un contexte exact : ne pas rogner ses espaces
        # significatifs, `tests ` n'est pas `tests` pour la forge.
        required_checks = tuple(required_checks)
        if len(set(required_checks)) != len(required_checks):
            raise SettingsError("%s.required_checks: duplicate check name" % where)
        required_checks_declared = ("required_checks" in value
                                    and value["required_checks"] is not None)
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
        required = value.get("required_statuses", {})
        if not isinstance(required, dict) or not all(
                isinstance(g, str) and g.strip() and isinstance(p, str) and p.strip()
                for g, p in required.items()):
            raise SettingsError("%s.required_statuses: {\"branch glob\": \"context prefix\"} "
                                "expected" % where)
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
                                 migrations=texts["migrations"], migrations_width=width,
                                 target=texts["target"],
                                 github=texts["github"], land=land,
                                 production_branch=texts["production_branch"],
                                 gates=tuple(gates), merge_method=method,
                                 lease_ref=lease_ref,
                                 generated=tuple(generated), path_gates=tuple(by_path),
                                 ci_required=ci_required,
                                 protected=tuple(p.strip() for p in protected),
                                 full_suite=tuple(full_suite),
                                 full_suite_required=tuple(required_jobs),
                                 full_suite_tolerated=tuple(tolerated_jobs),
                                 ci_parts=ci_parts,
                                 required_statuses=tuple((g.strip(), p.strip())
                                                         for g, p in required.items()),
                                 required_checks=required_checks,
                                 required_checks_declared=required_checks_declared)

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


def pass_lock(cfg, project: str):
    """Le verrou d'un passage radar sur un projet, pour l'hôte (L160b, #2254) :
    un descripteur ouvert et verrouillé, ou None quand un autre exécutant de
    l'hôte fait déjà ce passage.

    Un passage partage des ressources mutables — le clone privé du projet
    (`scan` y écrit, `Replayer` y fait ses fusions), le dernier rapport, le
    journal et `.notified` — et les écrit sans autre exclusion. Plusieurs
    exécuteurs d'un même hôte déclarent `radar.projects` : sans ce verrou,
    deux passages se marchent dessus (une fusion d'essai de l'un s'exécute
    pendant le `checkout`/`reset` de l'autre, le rejeu rend un `error`
    passager) et leurs écritures se succèdent sans ordre. Le noyau libère le
    verrou à la mort du processus : un passage tué ne bloque pas le suivant.

    `flock` non bloquant : le perdant s'abstient, le gagnant écrit le journal
    du passage ; le prochain tour reprendra le verrou. Le fichier n'est jamais
    supprimé (deux processus sur deux inodes différents ne s'excluraient pas).
    """
    import errno
    import fcntl
    directory = journal_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, project + ".pass.lock")
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return None
    return fh


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
        self.attributions: dict = {}
        # L160d: change sizes for the resolver, replays of prepared resolutions
        self.sizes: dict = {}
        self.replays: dict = {}
        # L160e: stacked replays of the merge queues; merge leases held (L160e2)
        self.stacks: dict = {}
        self.locks: dict = {}

    def trim(self) -> None:
        for table in (self.merges, self.diffs, self.attributions,
                      self.sizes, self.replays, self.stacks):
            while len(table) > MAX_CACHE:
                table.pop(next(iter(table)))


def scan(repo: str, lots: list[dict], *, default_target: str | None = None,
         migrations: str = "", cache: Cache | None = None,
         verify=None, reservations=None, reservation_holder=None) -> dict:
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
    entre au rapport, `added` ne sort jamais dans le journal (#1335).

    `reservation_holder` (facultatif) : `numéro -> id du lot détenteur | None`,
    lu sur le registre des réservations. #2396 : quand plusieurs copies d'une
    migration héritée divergent, le détenteur de la réservation est préféré
    comme survivant, pour que la sonde des réservations ne signale pas la copie
    d'un autre détenteur comme « unreserved ». Sans lui, le rang par date décide."""
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

        def attribution(one: dict, other: dict, raw_files: list[str]):
            from . import radar_attribution
            if one["target_head"] != other["target_head"]:
                return {"native": [], "inherited": [], "uncertain": raw_files}
            if one["base"] == other["base"]:
                # Both sides diverge from the same target base: no differing
                # target history can be responsible for their raw conflict.
                return {"native": raw_files, "inherited": [], "uncertain": []}
            head = one["target_head"]
            key = (head, one["tip"], one["base"], other["tip"], other["base"])
            if key not in cache.attributions:
                def probe(a, b):
                    return radar_attribution.read_merge(_scratch_git(
                        repo, objects, scratch, "-c", "attr.tree=%s" % head,
                        "merge-tree", "--write-tree", "-z", "--messages", a, b))
                cache.attributions[key] = radar_attribution.classify(one, other, probe)
            return cache.attributions[key]

        for one in branches:
            conflicts = merge(one["target_head"], one["tip"], one["target_head"])
            against.append({"work_item": one["work_item"], "branch": one["branch"],
                            "target": one["target"], "tip": one["tip"],
                            "target_head": one["target_head"],
                            "state": "error" if conflicts is None
                            else "conflict" if conflicts else "clean",
                            "files": conflicts or []})
        target_states = {item["work_item"]: item["state"] for item in against}
        for i, one in enumerate(branches):
            for other in branches[i + 1:]:
                if one["target"] != other["target"]:
                    continue
                checked += 1
                shared = sorted(set(one["files"]) & set(other["files"]))
                if not shared:
                    continue
                conflicts = merge(one["tip"], other["tip"], one["target_head"])
                pair = {"lots": [one["work_item"], other["work_item"]],
                        "branches": [one["branch"], other["branch"]],
                        "tips": [one["tip"], other["tip"]], "target": one["target"],
                        "state": "error" if conflicts is None
                        else "conflict" if conflicts else "overlap",
                        "shared": shared, "files": conflicts or []}
                if conflicts:
                    evidence = attribution(one, other, conflicts)
                    uncertain = ((set(conflicts) - set(evidence["native"])
                                  - set(evidence["inherited"]))
                                 | set(evidence["uncertain"] or ()))
                    pair.update(raw_files=conflicts, target_head=one["target_head"],
                                bases=[one["base"], other["base"]],
                                inherited_files=evidence["inherited"],
                                uncertain_files=sorted(uncertain))
                    pair["files"] = evidence["native"]
                    pair["attribution_pending"] = bool(pair["inherited_files"]
                                                       or pair["uncertain_files"])
                    if not pair["files"]:
                        pair["state"] = ("unclassified" if pair["uncertain_files"]
                                         else "target-conflict")
                        if (pair["state"] == "target-conflict"
                                and all(target_states[n] == "clean" for n in pair["lots"])):
                            pair["state"] = "superseded"
                pairs.append(pair)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    cache.trim()
    uncertified: dict = {}
    for one in branches:
        if not one.get("target_certified", True):
            uncertified.setdefault((one["target"], one["target_ref"]),
                                   one.get("target_reason") or "")
    migration_branches = (_native_migration_additions(
        repo, branches, migrations, reservation_holder=reservation_holder)
        if migrations else branches)
    report = {"repo": repo, "branches": [{k: v for k, v in b.items() if k != "added"}
                                         for b in branches],
              "skipped": skipped, "pairs_checked": checked, "pairs": pairs,
              "against_target": against,
              "uncertified_targets": [{"target": target, "ref": ref, "reason": why}
                                      for (target, ref), why in sorted(uncertified.items())],
              "migrations": migration_probe(repo, migration_branches, migrations)
              if migrations else []}
    if migrations and shallow:
        report["migration_probe"] = "suspended: shallow repository (history is incomplete)"
    if reservations is not None:
        report["unreserved_migrations"] = reservations(
            [b for b in migration_branches if b.get("target_certified", True)])
    return report


def _native_migration_additions(repo: str, branches: list[dict], directory: str,
                                *, reservation_holder=None) -> list[dict]:
    """#2279/#2388: attribute unchanged inherited migrations to their base.

    Inheritance is proved by history, never by a branch name: either a strict
    ancestor with the same path and blob (#2279), or — when neither holder
    descends from the other — a common merge-base that already carries the
    same path and blob (#2388: a lot stacked on a package whose tip has moved
    since the fork). One survivor is kept for the whole diverged group, not
    pair by pair: a pairwise choice can cycle between ancestry and date and
    hide every copy. The survivor keeps the attribution, so a third holder,
    the duplicate probe and the reservation probe still see the number. The
    reservation holder is preferred when it is in the group (#2396): the
    surviving copy is then the one the registry names, so the reservation
    probe does not report another holder's copy as unreserved. Otherwise the
    tip that advanced last wins, then the oldest lot. Equal tips, independent
    additions, modified SQL and unreadable evidence keep their warnings. Both
    probes use this view; branch diffs in the report keep their full scope.
    """
    groups: dict = {}
    for one in branches:
        if not one.get("target_certified", True):
            continue
        for path in one.get("added") or ():
            if (os.path.dirname(path) == directory
                    and MIGRATION_RE.fullmatch(os.path.basename(path))):
                groups.setdefault((one["target"], one["target_head"], path), []).append(one)
    inherited, ancestors, bases, blobs, first_parents = {}, {}, {}, {}, {}

    def ancestor(a: str, b: str) -> bool | None:
        """True/False, or None when Git could not answer (warning kept)."""
        pair = (a, b)
        if pair not in ancestors:
            proc = _git(repo, "merge-base", "--is-ancestor", a, b,
                        env_extra=NO_LAZY_FETCH)
            ancestors[pair] = (True if proc.returncode == 0
                               else False if proc.returncode == 1 else None)
        return ancestors[pair]

    def common_base(a: str, b: str) -> str | None:
        pair = tuple(sorted((a, b)))
        if pair not in bases:
            proc = _git(repo, "merge-base", pair[0], pair[1],
                        env_extra=NO_LAZY_FETCH)
            bases[pair] = (proc.stdout.strip().decode("ascii", "replace")
                           if proc.returncode == 0 else None)
        return bases[pair]

    def on_first_parent(base: str, tip: str) -> bool:
        """`base` is on the first-parent chain of `tip`: the branch was born
        on it (stacked), rather than merging it in from the target (#1333
        stays visible for a merge: the inherited migration is not attributed
        without a certified target)."""
        key = (base, tip)
        if key not in first_parents:
            count = _git(repo, "rev-list", "--count", base + ".." + tip,
                         env_extra=NO_LAZY_FETCH)
            try:
                steps = int(count.stdout.strip() or b"0")
            except ValueError:
                steps = 0
            if steps <= 0:
                first_parents[key] = False
            else:
                chain = _git(repo, "rev-list", "--first-parent",
                             "--max-count=%d" % (steps + 1), tip,
                             env_extra=NO_LAZY_FETCH)
                first_parents[key] = (chain.returncode == 0
                                      and base.encode("ascii") in chain.stdout.split())
        return first_parents[key]

    def blob(commit: str, path: str) -> str | None:
        key = (commit, path)
        if key not in blobs:
            proc = _git(repo, "rev-parse", "--verify", commit + ":" + path,
                        env_extra=NO_LAZY_FETCH)
            blobs[key] = proc.stdout.strip() if proc.returncode == 0 else None
        return blobs[key]

    tip_dates: dict = {}

    def tip_date(tip: str) -> int:
        if tip not in tip_dates:
            proc = _git(repo, "log", "-1", "--format=%ct", tip,
                        env_extra=NO_LAZY_FETCH)
            try:
                tip_dates[tip] = int(proc.stdout.strip() or b"0")
            except ValueError:
                tip_dates[tip] = 0
        return tip_dates[tip]

    def rank(one: dict, holder=None):
        """Pick the survivor of a diverged group: the reservation holder when
        it is in the group (#2396), else the tip that advanced last, then the
        oldest work item. Git cannot tell which of several diverged branches
        was the base (they carry the same file at their merge-base); the
        ranking only keeps one attribution, so a third holder and the
        reservation probe still see the number (#2388 review)."""
        return (1 if holder is not None and int(one["work_item"]) == holder else 0,
                tip_date(one["tip"]), -int(one["work_item"]), one["branch"])

    for (_, _, path), holders in groups.items():
        if len(holders) < 2:
            continue
        inherited_indices, diverged = set(), []
        for index, one in enumerate(holders):
            for other_index in range(index + 1, len(holders)):
                other = holders[other_index]
                a, b = one["tip"], other["tip"]
                if a == b:
                    continue
                ab = ancestor(a, b)
                ba = ancestor(b, a)
                if ab is None or ba is None:
                    continue              # unreadable history: keep the warning
                if ab or ba:
                    # One tip descends from the other: the descendant's copy
                    # is inherited when the file is identical (#2279). A
                    # holder explained by an ancestor holder is not a root of
                    # the lineage and never needs its own representative.
                    parent, child = (a, b) if ab else (b, a)
                    child_index = other_index if ab else index
                    if blob(parent, path) and blob(parent, path) == blob(child, path):
                        inherited_indices.add(child_index)
                    continue
                # #2388: diverged holders (a lot stacked on a base that moved
                # since the fork). A common ancestor carrying the identical
                # file proves that neither of them added it; each is an
                # inherited copy of the same lineage. Keep an edge, the
                # survivor is chosen once for the whole connected group.
                base = common_base(a, b)
                value = blob(base, path) if base else None
                if (value and value == blob(a, path) == blob(b, path)
                        and on_first_parent(base, a)
                        and on_first_parent(base, b)):
                    diverged.append((index, other_index))
        # Relecture #2388: a pairwise choice cycles (A ancestor B, A/C and B/C
        # diverged): each pair drops one copy, and A, B, C all lose the
        # migration. Ancestry is settled first; then each connected group of
        # diverged roots keeps exactly one survivor, so the duplicate and
        # reservation probes always see the number (#2388).
        roots = {index: index for index in range(len(holders))
                 if index not in inherited_indices}

        def find(index):
            while roots[index] != index:
                roots[index] = roots[roots[index]]
                index = roots[index]
            return index

        for index, other_index in diverged:
            if index not in roots or other_index not in roots:
                continue
            left, right = find(index), find(other_index)
            if left != right:
                roots[right] = left
        groups_by_root: dict = {}
        for index in list(roots):
            groups_by_root.setdefault(find(index), []).append(index)
        holder = None
        if (reservation_holder is not None
                and any(len(members) > 1 for members in groups_by_root.values())):
            number = int(MIGRATION_RE.fullmatch(os.path.basename(path)).group(1))
            try:
                found = reservation_holder(number)
            except Exception as exc:
                raise GitError(
                    "migration reservation registry unreadable: %s" % exc) from exc
            holder = int(found) if found is not None else None
        for members in groups_by_root.values():
            if len(members) < 2:
                continue
            keep = max(members, key=lambda index: rank(holders[index], holder))
            inherited_indices.update(index for index in members if index != keep)
        for index in inherited_indices:
            inherited.setdefault(holders[index]["work_item"], set()).add(path)
    return [dict(one, added=[path for path in one.get("added") or ()
                            if path not in inherited.get(one["work_item"], ())])
            for one in branches]


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
            "inherited": sum(1 for p in pairs if p["state"] == "target-conflict"),
            "superseded": sum(1 for p in pairs if p["state"] == "superseded"),
            "unclassified": sum(1 for p in pairs if p["state"] == "unclassified"),
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
    de journal de chacun des projets effectivement passés, avec ses envois
    (`mail`, L160c). Un projet dont un autre exécutant de l'hôte tient déjà le
    passage est ignoré (#2254, `pass_lock`) : son journal est écrit par ce
    passage-là. `ci` : la fonction `branche -> {number, green} | None`, par
    défaut lue sur GitHub."""
    from . import storage
    settings = settings if settings is not None else settings_of(cfg)
    if not settings.projects:
        return []
    lots = [r for r in storage.of(db).work.open_for_sweep(plan_git.MAX_BRANCH_LOTS)
            if r.get("branch")]
    lines = []
    for project in settings.projects.values():
        lock = pass_lock(cfg, project.name)
        if lock is None:
            continue                       # un autre exécutant de l'hôte le passe
        try:
            started = now()
            try:
                from .migration_reservations import probe
                from .migrations import repo_key
                github = github_slug(project)
                missing_repo = bool(project.migrations) and not github
                slug = repo_key(github) if project.migrations and github else ""
                holders: dict = {}

                def reservation_holder(number):
                    """#2396: prefer the registry's holder as the survivor of a
                    diverged inherited group, so the reservation probe does not
                    report another holder's copy as unreserved."""
                    if not slug:
                        return None
                    if number not in holders:
                        try:
                            row = storage.of(db).work.migration_holder(number, slug)
                        except Exception as exc:
                            raise GitError(
                                "migration reservation registry unreadable: %s" % exc) from exc
                        holders[number] = int(row["id"]) if row else None
                    return holders[number]

                report = scan(
                    project.repo, lots, default_target=project.target or None,
                    migrations=project.migrations, cache=cache,
                    verify=published_tip if project.migrations else None,
                    reservations=None if missing_repo
                    else (lambda raw: probe(db, project, raw)),
                    reservation_holder=reservation_holder if slug else None)
                if missing_repo:
                    report["reservation_error"] = (
                        "migration reservations need a GitHub repository identity")
                error = None
            except (GitError, plan_git.MergeProbeError, OSError) as exc:
                report, error = ({"repo": project.repo},
                                 "%s : %s" % (type(exc).__name__, exc))
            if not error:
                annotate_resolutions(cfg, db, project, report, lots, cache)
                report["queue"] = run_queue(cfg, db, project, report, cache)
                if project.land != "off":
                    report["land"] = run_land(cfg, db, project, report, cache, owner)
            report.update(schema=SCHEMA, project=project.name, ts=started,
                          duration_s=round(now() - started, 3), error=error)
            line = write_journal(cfg, project.name, report)
            sent = [] if error else send_mail(cfg, db, project, report, ci=ci)
            lines.append(dict(line, project=project.name, mail=sent,
                              land=report.get("land") or []))
        finally:
            lock.close()
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
    conflicted = sorted({n for p in report.get("pairs") or ()
                         if p["state"] in ("conflict", "target-conflict", "unclassified", "superseded")
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
                             replayer=resolutions.Replayer(
                                 project.repo, clone_dir(cfg, project.name),
                                 target=project.target),
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


def required_statuses(project, branch: str | None) -> tuple:
    """#1748 (a): the commit status prefixes `branch` must carry in `success`
    (`required_statuses` of the project). An unknown branch gets every
    declared prefix: a doubt never lowers the gate."""
    import fnmatch
    declared = getattr(project, "required_statuses", ()) or ()
    name = (branch or "").strip()
    # Match the same branch aliases that merge_queue.fetch_fresh resolves.
    for ref_prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(ref_prefix):
            name = name[len(ref_prefix):]
            break
    if not name:
        return tuple(sorted({prefix for _, prefix in declared}))
    return tuple(sorted({prefix for glob, prefix in declared
                         if fnmatch.fnmatchcase(name, glob)}))


def _status_success(contexts, required) -> list:
    """The prefixes of `required` that no `success` context of `contexts`
    (`(context, state)` pairs) carries."""
    won = [c for c, state in contexts if (state or "").upper() == "SUCCESS"]
    return [p for p in required if not any((c or "").startswith(p) for c in won)]


#: #4549 : le job de planification de la suite `tests`. Son échec saute
#: toutes les parts qui en dépendent : la suite n'exécute alors aucun test.
PLAN_CHECK = "plan"
#: #4549 : préfixe des checks de flux d'une part (`ci.CHECK_PREFIX`). Leur
#: existence prouve qu'une part a démarré, donc qu'un test a pu s'exécuter.
#: Recopié ici pour rester lisible sans import circulaire ; `ci.py` reste la
#: source du nom.
TEST_FLOW_PREFIX = "tests (flux)"


def _entry_name(entry) -> str:
    """Le nom d'une entrée de rollup : `name` d'un check run, `context` d'un
    statut de commit."""
    return str(entry.get("name") or entry.get("context") or "")


def testless_failure(checks, name, required=()) -> bool:
    """#4549 : la suite a-t-elle échoué au plan **sans exécuter de test** ?

    La preuve est fermée par défaut : un job `plan` en `FAILURE`, aucun check
    de flux de part (`tests (flux) …`) — une part qui démarre publie le sien —,
    aucun autre négatif que le plan et les checks requis, et aucun check
    encore en cours. Un test rouge, une part lancée ou une suite encore
    active ne sont donc jamais masqués. `required` nomme les autres checks
    requis : leur échec, dans une suite au plan rouge, ne prouve pas un test
    exécuté (toutes les parts dépendent du plan)."""
    rows = [entry for entry in checks or () if isinstance(entry, dict)]
    plan = [entry for entry in rows if _entry_name(entry) == PLAN_CHECK]
    if not plan or not any(check_result(entry) == CI_NEGATIVE_RESULT
                           for entry in plan):
        return False
    required = set(required or ())
    for entry in rows:
        label = _entry_name(entry)
        if label.startswith(TEST_FLOW_PREFIX):
            return False
        if check_result(entry) == CI_WAITING_RESULT:
            return False
        if label in (PLAN_CHECK, name) or label in required:
            continue
        if check_result(entry) == CI_NEGATIVE_RESULT:
            return False
    return True


def latest_rollup(rollup) -> list:
    """#2524 : les derniers checks par (workflow, nom), statuts par contexte.
    #3008 : une suite connue (`checkSuiteId`, rollup construit depuis les
    check runs de la tête) entre dans la clé : deux workflows homonymes ne se
    masquent pas.
    #4549 (B2 de cx2) : aucune suite n'est masquée ici sur le seul nom de
    workflow ni sur une date : une suite verdit sur ses preuves (workflow,
    plan, base attestée) dans la porte (`fire._required_gate`), jamais par un
    filtrage du rollup.

    Seule une date strictement plus récente remplace une entrée. Dates
    absentes, invalides ou ex aequo : garder les entrées ambiguës afin de ne
    jamais masquer un échec ou un check en cours sur la foi de l'ordre reçu.
    """
    from datetime import datetime

    rows = list(rollup or ())
    groups = {}
    for index, entry in enumerate(rows):
        context = (entry.get("__typename") == "StatusContext"
                   or "state" in entry and "conclusion" not in entry)
        name = entry.get("context" if context else "name")
        key = (("context", name) if context else
               ("check", entry.get("workflowName"), entry.get("checkSuiteId"), name)
               ) if name else ("unknown", index)
        # `gh` exporte la date d'un StatusContext sous `startedAt` (son
        # `createdAt` GraphQL) ; `createdAt` reste lu pour un rollup GraphQL brut.
        raw = (entry.get("startedAt") or entry.get("createdAt")) if context else entry.get("startedAt")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            stamp = stamp.timestamp() if stamp.tzinfo is not None else None
        except (AttributeError, TypeError, ValueError, OverflowError):
            stamp = None
        groups.setdefault(key, []).append((entry, stamp))
    latest = []
    for entries in groups.values():
        if any(stamp is None for _, stamp in entries):
            latest.extend(entry for entry, _ in entries)
        else:
            newest = max(stamp for _, stamp in entries)
            latest.extend(entry for entry, stamp in entries if stamp == newest)
    return latest


def merge_rollup(rollup, *, slug: str, head: str, branch: str, gh) -> list:
    """#2876 : remplacer un portier seulement sur preuve de son run annulé.

    Lire le run PR et TOUS les runs push paquet de la composition : le plus
    récent peut être actif sans aucun check visible dans le rollup. Sans
    preuve API fraîche, aucun remplacement et aucun vert d'intégration.
    `ci_green` reste un prédicat pur ; les lecteurs lui passent ce rollup.
    """
    from datetime import datetime
    from urllib.parse import urlencode

    original = list(rollup or ())
    rows = latest_rollup(original)
    if not branch.startswith("integration/"):
        return rows
    aggregate = [c for c in rows if c.get("__typename") == "CheckRun"
                 and c.get("workflowName") == "tests" and c.get("name") == "tests"]
    if len(aggregate) != 1 or aggregate[0].get("conclusion") != "SUCCESS":
        return rows

    #: #3494 : les checks publiés par les étapes du workflow appelé (flux des
    #: parts `tests (flux) …`, détail de l'agrégat `tests (bilan des parts)`)
    #: sont rattachés par la forge à la suite `paquet` de la tête ; leur
    #: `detailsUrl` (`/runs/<check-id>`) ne désigne aucun run Actions. Ils ne
    #: prouvent pas le portier : les compter comme tels rendait sa preuve
    #: illisible et la sentinelle `paquet (run proof unavailable)` bloquait la
    #: fusion du paquet (h-15, run 40158149836). Ils restent dans le rollup,
    #: avec leurs portes propres (agrégat, flux).
    def custom(check):
        name = str(check.get("name") or check.get("context") or "")
        return name == AGGREGATE_DETAIL_CHECK or name.startswith("tests (flux)")

    all_packet = [c for c in original if c.get("__typename") == "CheckRun"
                  and c.get("workflowName") == "paquet" and not custom(c)]
    packet = [c for c in all_packet
              if (c.get("name") in ("couverture (PR ouverte sur cette tête)",
                                    "suite complète du paquet")
                  or (c.get("name") or "").startswith("suite complète du paquet / "))]

    def blocked():
        return rows + [{"__typename": "CheckRun", "name": "paquet (run proof unavailable)",
                        "status": "COMPLETED", "conclusion": "FAILURE"}]

    def read(path):
        if hasattr(gh, "run"):
            code, out, _ = gh.run(["api", path])
        else:
            proc = gh(["api", path])
            code, out = proc.returncode, proc.stdout
        if code:
            raise ValueError("packet run proof unavailable")
        return json.loads(out)

    def run_id(check):
        url = check.get("detailsUrl") or ""
        if not isinstance(url, str):
            return None
        match = re.fullmatch(r"https://github[.]com/" + re.escape(slug)
                             + r"/actions/runs/([1-9][0-9]*)/job/[1-9][0-9]*", url)
        return int(match.group(1)) if match else None

    def finished(check):
        if check.get("status") != "COMPLETED":
            return None
        try:
            stamp = datetime.fromisoformat(check["completedAt"].replace("Z", "+00:00"))
            return stamp.timestamp() if stamp.tzinfo is not None else None
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            return None

    try:
        runs, page = [], 1
        while True:
            query = urlencode({"head_sha": head, "branch": branch, "event": "push",
                               "per_page": 100, "page": page})
            # La liste générale fonctionne aussi dans un dépôt sans portier
            # (pas de 404 ambigu sur un workflow absent).
            data = read("repos/%s/actions/runs?%s" % (slug, query))
            batch = data["workflow_runs"]
            total = data["total_count"]
            if not isinstance(batch, list) or type(total) is not int or total < 0:
                return blocked()
            runs.extend(batch)
            if len(runs) >= total:
                if len(runs) != total:
                    return blocked()
                break
            if not batch:
                return blocked()
            page += 1
        if any(type(r.get("id")) is not int or r["id"] <= 0
               or r.get("head_sha") != head or r.get("head_branch") != branch
               or r.get("event") != "push" or not isinstance(r.get("path"), str)
               for r in runs):
            return blocked()
        if len({r["id"] for r in runs}) != len(runs):
            return blocked()  # pagination déplacée pendant la lecture
        runs = [r for r in runs if r.get("path") == ".github/workflows/paquet.yml"]
        if not runs:
            # Aucun portier créé : la PR porte toute la validation.
            return rows if not all_packet else blocked()
        if any(r.get("status") != "completed" for r in runs):
            return blocked()  # y compris une nouvelle tentative d'un ancien run
        latest = max(runs, key=lambda r: r["id"])
        if latest.get("conclusion") == "success":
            return rows  # aucun échec de portier n'est effacé sans annulation
        if latest.get("conclusion") != "cancelled":
            return blocked()
        replacement = aggregate[0]
        end, replacement_id = finished(replacement), run_id(replacement)
        if end is None or replacement_id is None or gh is None:
            return blocked()
        pr_run = read("repos/%s/actions/runs/%d" % (slug, replacement_id))
        if (pr_run.get("id") != replacement_id or pr_run.get("head_sha") != head
                or pr_run.get("head_branch") != branch or pr_run.get("event") != "pull_request"
                or pr_run.get("path") != ".github/workflows/tests.yml"
                or pr_run.get("status") != "completed" or pr_run.get("conclusion") != "success"):
            return blocked()
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError):
        return blocked()

    # Le run annulé le plus récent doit être visible : un CANCELLED d'un
    # ancien run ne permet jamais de retirer les FAILURE d'un nouveau run.
    ids = [run_id(c) for c in all_packet]
    if (not packet or None in ids or any(i not in {r["id"] for r in runs} for i in ids)
            or not any(run_id(c) == latest["id"] and c.get("conclusion") == "CANCELLED"
                       for c in packet)):
        return blocked()
    ends = [finished(c) for c in all_packet]
    if any(t is None for t in ends) or end <= max(ends):
        return blocked()
    return [c for c in rows if c not in packet]


def ci_green(rollup, required=()) -> bool:
    """`statusCheckRollup` d'une PR : vert si au moins un check, et tous
    terminés sans échec, après sélection des derniers checks (#2524).
    Les lecteurs valident le remplacement éventuel du portier via merge_rollup (#2876).
    #1748 (a) : chaque préfixe de `required`
    (`required_statuses`) doit en outre être porté par un statut de commit
    (`StatusContext`, jamais un check run, dont le nom est libre) en
    `success` — des checks `skipped` ou `success` seuls ne suffisent plus."""
    rollup = latest_rollup(rollup)
    if not rollup:
        return False
    statuses = [(c.get("context"), c.get("state")) for c in rollup
                if c.get("__typename") == "StatusContext"
                or "state" in c and "conclusion" not in c]
    if _status_success(statuses, required):
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


#: #3085 (règle owner du 23/10, canon home §3) : un check ne bloque une porte
#: que s'il est **négatif** — un test exécuté qui démontre un défaut. Tout
#: autre résultat conclu est *non concluant* (sauté, neutralisé, annulé,
#: expiré, démarrage raté, statut en erreur) : il se consigne, il n'arrête
#: rien. Un check encore en cours n'est pas un résultat : il attend.
CI_GREEN_RESULT = "green"
CI_NEGATIVE_RESULT = "negative"
CI_INCONCLUSIVE_RESULT = "inconclusive"
CI_WAITING_RESULT = "waiting"
#: conclusions (check run) d'un rouge prouvé ; la casse est libre
CI_NEGATIVE = frozenset(("FAILURE",))
#: conclusions (check run) qui ne prouvent rien
CI_INCONCLUSIVE = frozenset(("SKIPPED", "NEUTRAL", "CANCELLED", "TIMED_OUT",
                             "STALE", "STARTUP_FAILURE", "ACTION_REQUIRED"))
#: états (statut de commit) qui ne prouvent rien
CI_INCONCLUSIVE_STATES = frozenset(("ERROR",))
#: #3186 (décision owner #128512 du 23/10) : les conclusions d'un check qui
#: n'a pas conclu **lui-même** — exécution annulée, expirée, démarrage raté,
#: abandonnée par GitHub (`STALE` : restée incomplète quatorze jours). Sur un
#: check requis (l'agrégat `tests`, #876) ou sur un run entier lu par la
#: release, ce n'est pas un test non concluant : aucune preuve n'existe, la
#: porte bloque et relance le run une fois (`rerun_unconcluded`). Une part ou
#: un check facultatif ainsi conclu reste non concluant (#3085).
CI_NEVER_CONCLUDED = frozenset(("CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "STALE"))


def never_concluded(state) -> bool:
    """#3186 : la conclusion `state` (check ou run, casse libre) dit que le
    check n'a pas conclu lui-même — ni vert, ni rouge, ni sauté."""
    return str(state or "").strip().upper() in CI_NEVER_CONCLUDED


#: #3221 : les états qu'une porte écrit entre parenthèses après le nom consigné
#: #3530 : `ci_reading` écrit aussi `(état; URL)` et l'état d'un job encore en
#: cours (`in_progress`, `queued`) — la casse reste libre.
CI_CONSIGNED_STATES = (CI_INCONCLUSIVE | CI_INCONCLUSIVE_STATES | CI_NEGATIVE
                       | frozenset(("SUCCESS", "PENDING", "EXPECTED", "?",
                                    "IN_PROGRESS", "QUEUED")))

#: #3530 : l'URL du run que `release_cutoff.ci_reading` met après l'état dans
#: le suffixe consigné. Absente ou vide (`(cancelled; )`) : rien à garder.
_CONSIGNED_URL = re.compile(r"^https?://\S*$", re.IGNORECASE)

#: #3251 : check de détail publié par l'étape « bilan des parts » de
#: `.github/workflows/tests.yml` (scripts/ci-agregat.py), à chaque exécution.
#: `success` quand toutes les parts ont conclu, `neutral` quand au moins une
#: part interne a été annulée (non concluant, canon §3), `failure` sur un
#: négatif. Il porte la distinction que le run ne porte pas : le check requis
#: `tests` reste vert quand l'agrégat a conclu malgré une annulation interne.
AGGREGATE_DETAIL_CHECK = "tests (bilan des parts)"


def aggregate_detail_entries(rollup) -> list:
    """#3251 : les derniers checks de détail de l'agrégat requis.

    Une liste vide signifie « aucun signal » (projet sans cet agrégat, ou tête
    antérieure à #3251) : la porte ne consigne rien et ne bloque rien de plus.

    Le même nom peut venir de deux workflows sur une même tête (`tests` du run
    de la PR, `paquet` du portier qui a appelé la suite) : le regroupement de
    `latest_rollup` par (workflow, suite) les garderait tous les deux. On
    normalise la clé pour ne garder que le plus récent des deux — sinon un
    `neutral` d'un essai antérieur consignerait une tête redevenue verte.
    Dates absentes ou ex aequo : toutes les entrées restent (jamais masquer un
    négatif)."""
    rows = []
    for entry in rollup or ():
        if str(entry.get("name") or entry.get("context") or "")                 != AGGREGATE_DETAIL_CHECK:
            continue
        row = dict(entry)
        row.pop("workflowName", None)
        row.pop("checkSuiteId", None)
        rows.append(row)
    return latest_rollup(rows)


def _consigned_suffix(group: str) -> bool:
    """#3530 : le suffixe d'occurrence écrit par `release_cutoff.ci_reading` —
    `ÉTAT[, ÉTAT]` puis, éventuellement, `; URL` — ne contient que des états
    connus. Une parenthèse du nom (`linux`, `linux; arm64`) ne l'est pas."""
    chunks = [chunk.strip() for chunk in str(group or "").split(";")]
    states = [state.strip().upper() for state in chunks[0].split(",")]
    if not states or not all(states) \
            or any(state not in CI_CONSIGNED_STATES for state in states):
        return False
    return all(not chunk or _CONSIGNED_URL.match(chunk) for chunk in chunks[1:])


def nonconclusive_source(app: str, entry: str) -> str:
    """#3209 : la clé du lot de correction d'un check non concluant — l'app
    et le **nom du check**, sans l'état consigné (`tests (CANCELLED)` →
    `tests`) ni la tête : le même check au paquet suivant ou à la release
    retrouve le lot encore ouvert (`work.add_or_note`).
    #3221 : seul un suffixe fait d'états connus est retiré ; une parenthèse du
    nom (`tests (linux)`, nom brut de la release) reste dans la clé.
    #3530 : `ci_reading` écrit `(état; URL)` (et l'état d'un job encore en
    cours, `in_progress` / `queued`) ; l'URL et l'état ne font pas partie de
    la clé, sinon chaque occurrence rouvre un lot."""
    text = str(entry or "").strip()
    match = re.match(r"^(.*?)\s*\(([^()]*)\)$", text)
    if match and match.group(1) and _consigned_suffix(match.group(2)):
        text = match.group(1)
    return "nonconclusive:%s:%s" % (app, text)


def conclusion_result(status, conclusion) -> str:
    """#3085 : classe un run ou un check brut (`status`, `conclusion`, casse
    libre) en `green` / `negative` / `inconclusive` / `waiting`.

    Seule `FAILURE` est un défaut prouvé. Un job annulé (quota), sauté,
    expiré ou qui n'a pas démarré n'a rien démontré : non concluant, jamais
    négatif. Un run qui n'est pas terminé attend."""
    if str(status or "").strip().lower() != "completed":
        return CI_WAITING_RESULT
    value = str(conclusion or "").strip().upper()
    if value == "SUCCESS":
        return CI_GREEN_RESULT
    if value in CI_NEGATIVE:
        return CI_NEGATIVE_RESULT
    if value in CI_INCONCLUSIVE:
        return CI_INCONCLUSIVE_RESULT
    # une conclusion absente ou inconnue n'est pas un résultat : elle attend,
    # elle ne verdit jamais la porte.
    return CI_WAITING_RESULT


def state_result(state) -> str:
    """#3085 : classe un statut de commit (`SUCCESS`, `FAILURE`, `PENDING`,
    `ERROR`) ; `ERROR` dit que le check n'a pas conclu, pas qu'il a échoué."""
    value = str(state or "").strip().upper()
    if value == "SUCCESS":
        return CI_GREEN_RESULT
    if value == "FAILURE":
        return CI_NEGATIVE_RESULT
    if value in CI_INCONCLUSIVE_STATES:
        return CI_INCONCLUSIVE_RESULT
    return CI_WAITING_RESULT


def check_result(entry) -> str:
    """#3085 : classe une entrée de `statusCheckRollup`."""
    if not isinstance(entry, dict):
        return CI_WAITING_RESULT
    if entry.get("__typename") == "StatusContext" \
            or "state" in entry and "conclusion" not in entry:
        return state_result(entry.get("state"))
    return conclusion_result(entry.get("status"), entry.get("conclusion"))


def rollup_state_result(state) -> str:
    """#3085 : classe un état déjà normalisé par `_rollup_state` (majuscules),
    qui mêle les états d'un statut et les conclusions d'un check."""
    value = str(state or "").strip().upper()
    if value == "SUCCESS":
        return CI_GREEN_RESULT
    if value == "FAILURE":
        return CI_NEGATIVE_RESULT
    if value == "PENDING" or not value:
        return CI_WAITING_RESULT
    if value in CI_INCONCLUSIVE or value in CI_INCONCLUSIVE_STATES:
        return CI_INCONCLUSIVE_RESULT
    return CI_WAITING_RESULT


def ci_result(rollup) -> dict:
    """#3085 : `{state, green, negative, inconclusive, waiting}` d'un rollup.

    `state` vaut `red` dès un négatif, `pending` si un check attend (ou si le
    rollup est vide : rien ne se suppose vert), `green` sinon — un non
    concluant n'empêche pas le vert, il est listé pour être consigné. Les
    noms sont triés, sans doublon."""
    rows = latest_rollup(rollup)
    found = {CI_GREEN_RESULT: [], CI_NEGATIVE_RESULT: [],
             CI_INCONCLUSIVE_RESULT: [], CI_WAITING_RESULT: []}
    for entry in rows:
        found[check_result(entry)].append(str(
            entry.get("context") if entry.get("__typename") == "StatusContext"
            else entry.get("name") or entry.get("context") or ""))
    # le classement se fait sur les entrées, pas sur les noms : un check sans
    # nom reste un résultat et ne disparaît pas du verdict.
    state = ("red" if found[CI_NEGATIVE_RESULT]
             else "pending" if not rows or found[CI_WAITING_RESULT]
             else "green")
    out = {key: sorted({name for name in names if name}) for key, names in found.items()}
    out["state"] = state
    return out


def packet_proof_entries(rollup) -> list:
    """Les checks du portier de suite complète d'un paquet : le workflow
    `paquet` et la sentinelle `paquet (run proof unavailable)` que
    `merge_rollup` ajoute quand la preuve manque (#2876/#3066)."""
    out = []
    for entry in latest_rollup(rollup) or ():
        name = str(entry.get("name") or entry.get("context") or "")
        # #3251 : le détail de l'agrégat `tests` n'est pas une preuve du
        # portier, même quand `tests.yml` est appelé par `paquet` (le check est
        # alors associé au workflow appelant) : il a sa propre porte.
        if name == AGGREGATE_DETAIL_CHECK:
            continue
        if str(entry.get("workflowName") or "") == "paquet" \
                or name == "paquet (run proof unavailable)":
            out.append(entry)
    return out


#: #3209 : les jobs du portier `paquet` (.github/workflows/paquet.yml)
PACKET_COVERAGE = "couverture (PR ouverte sur cette tête)"
PACKET_SUITE = "suite complète du paquet"


def packet_suite_covered(entry, entries) -> bool:
    """#3209 : `entry` est la suite complète que le portier `paquet` a
    sautée **par construction** — une PR ouverte porte déjà la tête, sa suite
    tourne dans le run `pull_request` (check requis `tests`, lu à part).

    La preuve est le job `couverture` vert du même run (même `checkSuiteId`
    quand les deux le portent) : c'est lui qui décide du saut. Sans cette
    preuve, ou si la suite est annulée, expirée ou rouge, rien ne change :
    le non concluant se consigne (#3085) et le rouge bloque."""
    name = str(entry.get("name") or "")
    if (str(entry.get("conclusion") or "").upper() != "SKIPPED"
            or not (name == PACKET_SUITE or name.startswith(PACKET_SUITE + " / "))):
        return False
    suite = entry.get("checkSuiteId")
    return any(str(other.get("name") or "") == PACKET_COVERAGE
               and str(other.get("workflowName") or "") == "paquet"
               and str(other.get("status") or "").upper() == "COMPLETED"
               and str(other.get("conclusion") or "").upper() == "SUCCESS"
               and (suite is None or other.get("checkSuiteId") is None
                    or other.get("checkSuiteId") == suite)
               for other in entries)


def packet_portier_replaced(entry, entries, rollup) -> bool:
    """#4812 : `entry` est un job du portier `paquet` qu'ameesh a annulé
    **par construction** — `ameesh ci run --full` remplace le portier de la
    tête par le run `pull_request` étiqueté de la PR (#2876) : `couverture`
    finit CANCELLED, `suite complète du paquet` SKIPPED.

    La preuve est double : le job `couverture` annulé du même run (même
    `checkSuiteId` quand les deux le portent), et l'agrégat `tests` du
    workflow `tests` vert dans le rollup de la même tête — la suite complète
    a bien tourné, ailleurs. Sans elle, ou pour une suite annulée, expirée ou
    rouge, rien ne change : le non concluant se consigne (#3085)."""
    name = str(entry.get("name") or "")
    state = str(entry.get("conclusion") or "").upper()
    if not ((name == PACKET_COVERAGE and state == "CANCELLED")
            or ((name == PACKET_SUITE or name.startswith(PACKET_SUITE + " / "))
                and state == "SKIPPED")):
        return False
    suite = entry.get("checkSuiteId")
    cancelled = any(str(other.get("name") or "") == PACKET_COVERAGE
                    and str(other.get("workflowName") or "") == "paquet"
                    and str(other.get("conclusion") or "").upper() == "CANCELLED"
                    and (suite is None or other.get("checkSuiteId") is None
                         or other.get("checkSuiteId") == suite)
                    for other in entries)
    return cancelled and any(
        str(other.get("name") or "") == "tests"
        and str(other.get("workflowName") or "") == "tests"
        and check_result(other) == CI_GREEN_RESULT
        for other in latest_rollup(rollup) or ())


def ci_state(rollup) -> str:
    """#3085 : l'état de la CI d'un rollup sous la règle du 23/10 — un non
    concluant ne bloque pas (voir `ci_result`).

    Les checks du portier `paquet` portent la preuve que la suite complète a
    tourné (ou a été remplacée) : un `FAILURE` exécuté et une sentinelle
    `paquet (run proof unavailable)` restent `red`, un check en cours
    `pending`. Un check `paquet` **non concluant** (sauté, annulé pour
    quota, expiré) ne prouve aucun défaut : il est consigné par le
    classifieur global, qui le laisse vert."""
    entries = packet_proof_entries(rollup)
    unfinished = any(entry.get("__typename") != "StatusContext"
                     and str(entry.get("status") or "").upper() != "COMPLETED"
                     for entry in entries)
    if unfinished:
        return "pending"
    if any(check_result(entry) == CI_NEGATIVE_RESULT for entry in entries):
        return "red"
    return ci_result(rollup)["state"]


def actions_run_id(slug: str, entry) -> int | None:
    """#3186 : le run GitHub Actions d'un check — `detailsUrl` d'une entrée de
    rollup, `details_url` d'un check run REST (`…/<slug>/actions/runs/<id>/
    job/<id>`), ou `databaseId` d'une ligne de `gh run list`. None pour un
    statut de commit, un check d'une autre application ou d'un autre dépôt :
    rien à relancer."""
    if not isinstance(entry, dict):
        return None
    identity = entry.get("databaseId")
    if type(identity) is int and identity > 0:
        return identity
    url = entry.get("detailsUrl") or entry.get("details_url") or ""
    if not isinstance(url, str) or not slug:
        return None
    match = re.fullmatch(r"https://github[.]com/" + re.escape(slug)
                         + r"/actions/runs/([1-9][0-9]*)/job/[1-9][0-9]*", url)
    return int(match.group(1)) if match else None


def _gh_call(gh, argv: list) -> tuple:
    """`(code, out, err)` d'un `gh` des deux formes en usage : `plan_github.Gh`
    (`run(argv)` → tuple) ou l'appelable de `land`/`fire` (→ CompletedProcess)."""
    if hasattr(gh, "run"):
        code, out, err = gh.run(list(argv))
        return code, out or "", err or ""
    proc = gh(list(argv))
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def rerun_unconcluded(slug: str, head: str, runs, *, gh, dry_run: bool = False,
                      guard=None) -> list:
    """#3186 (décision owner #128512 du 23/10) : « agrégat requis jamais conclu
    = preuve absente, donc bloquant : on relance le seul run (coût : une
    relance) ».

    `runs` : des paires `(libellé, run_id | None)`. Chaque run est relu
    (`actions/runs/<id>`) puis relancé en entier — `gh run rerun <id>` :
    `--failed` ne rejoue que les jobs FAILURE, pas un job annulé, expiré ou
    jamais démarré (B2 d'impl-04) — seulement s'il porte la tête `head`, s'il
    est terminé et à son premier essai. Un run déjà
    relancé (essai ≥ 2, par ameesh ou par `relance hosted`) ne l'est plus :
    la relance a coûté sa fois, la preuve manque toujours et le dire suffit.
    Aucune issue ne rouvre la porte : seule une conclusion la rouvre.
    `guard()` (bail du passage, garde du pilote élu) est relu juste avant
    chaque relance : un détenteur périmé ne relance rien (`busy`).

    Rend `[{check, run, result, detail}]`, `result` parmi `rerun`,
    `would-rerun`, `running` (déjà relancé, en cours), `spent` (relance déjà
    faite), `busy` (garde perdue), `unknown` (aucun run Actions à relancer),
    `refused` (run d'une autre tête) et `error` (lecture ou relance
    impossible)."""
    out, seen = [], set()
    for label, run_id in runs or ():
        if run_id in seen and run_id is not None:
            continue
        seen.add(run_id)
        row = {"check": label, "run": run_id}
        if run_id is None:
            out.append(dict(row, result="unknown",
                            detail="no GitHub Actions run to rerun"))
            continue
        try:
            code, text, err = _gh_call(gh, ["api", "repos/%s/actions/runs/%d" % (slug, run_id)])
            data = json.loads(text) if not code else None
        except (OSError, ValueError, TypeError) as exc:
            code, data, err = 1, None, str(exc)
        if code or not isinstance(data, dict) or data.get("id") != run_id:
            out.append(dict(row, result="error",
                            detail="run %d unreadable: %s" % (run_id, (err or "?").strip()[:200])))
            continue
        attempt = data.get("run_attempt")
        attempt = attempt if type(attempt) is int and attempt > 0 else 1
        if str(data.get("head_sha") or "").lower() != str(head or "").lower():
            out.append(dict(row, result="refused", detail="run %d is on %s, not %s"
                            % (run_id, str(data.get("head_sha") or "?")[:12], head[:12])))
        elif data.get("status") != "completed":
            out.append(dict(row, result="running", attempt=attempt,
                            detail="run %d is %s (attempt %d)"
                            % (run_id, data.get("status") or "?", attempt)))
        elif attempt > 1:
            out.append(dict(row, result="spent", attempt=attempt,
                            detail="run %d already rerun once (attempt %d, %s): the "
                                   "proof is still missing" % (
                                       run_id, attempt, data.get("conclusion") or "?")))
        elif dry_run:
            out.append(dict(row, result="would-rerun", attempt=attempt,
                            detail="run %d would be rerun" % run_id))
        elif guard is not None and not guard():
            out.append(dict(row, result="busy", attempt=attempt,
                            detail="lease lost before the rerun of run %d: nothing rerun"
                            % run_id))
        else:
            try:
                code, _text, err = _gh_call(gh, ["run", "rerun", str(run_id),
                                                 "--repo", slug])
            except (OSError, ValueError, TypeError) as exc:
                code, err = 1, str(exc)
            if code:
                out.append(dict(row, result="error", attempt=attempt,
                                detail="rerun of run %d refused: %s"
                                % (run_id, (err or "?").strip()[:200])))
            else:
                out.append(dict(row, result="rerun", attempt=attempt,
                                detail="run %d rerun (once)" % run_id))
    return out


def describe_reruns(reruns) -> str:
    """#3186 : une ligne lisible des issues de `rerun_unconcluded`."""
    return "; ".join("%s: %s" % (row.get("check") or "?", row.get("detail") or row["result"])
                     for row in reruns or ())


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
        rollup = merge_rollup(rows[0].get("statusCheckRollup"), slug=slug,
                              head=rows[0].get("headRefOid") or "", branch=branch, gh=gh)
        return {"number": rows[0].get("number"), "url": rows[0].get("url"),
                "head": rows[0].get("headRefOid"),
                "green": ci_green(rollup, required_statuses(project, branch)),
                "draft": bool(rows[0].get("isDraft"))}
    return status


#: L160g (#473): conclusions of a workflow run that do not block
CI_PASSING = ("success", "skipped", "neutral")


def run_result(run) -> str:
    """#3209 : classe un run de `gh run list` comme `conclusion_result`, sauf
    la garde déclenchée par `workflow_run` (`relance hosted`, `flux
    orphelins`) et sautée : sa condition `if:` dit que le run observé n'a pas
    échoué, elle n'avait rien à faire. Ce n'est pas un test qui n'a pas
    conclu — rien à consigner ; il passe, comme `CI_PASSING` le lisait déjà.
    Annulée, expirée ou rouge, elle garde sa classe."""
    if (str(run.get("event") or "") == "workflow_run"
            and str(run.get("status") or "").lower() == "completed"
            and str(run.get("conclusion") or "").lower() == "skipped"):
        return CI_GREEN_RESULT
    return conclusion_result(run.get("status"), run.get("conclusion"))


#: #3639 : l'API rend au plus 1 000 check-runs par commit (10 pages de 100).
CHECK_RUNS_PAGE = 100
CHECK_RUNS_MAX_PAGES = 10


def commit_check_runs(gh, slug: str, sha: str) -> list:
    """Tous les check-runs du commit `sha`, page par page (#3639).

    Une tête qui porte la suite complète dépasse 100 check-runs (141 sur
    fb8b7403, 145 sur 47d72915). Une seule page la rendait illisible, et la
    coupe ne lisait plus la CI de main. Les gardes restent celles d'une page :
    chaque réponse est bien formée, le total ne change pas d'une page à
    l'autre, et le nombre lu à la fin est égal au total. Sinon la lecture lève
    `GithubError` et n'est jamais prise pour « pas de CI ». La première page
    garde l'URL d'avant ; seules les suivantes portent `&page=`."""
    from . import plan_github
    path = "repos/%s/commits/%s/check-runs?filter=all&per_page=%d" % (
        slug, sha, CHECK_RUNS_PAGE)
    runs: list = []
    total = None
    for page in range(1, CHECK_RUNS_MAX_PAGES + 1):
        data = plan_github._json(gh, ["api", path if page == 1 else "%s&page=%d" % (path, page)])
        if (not isinstance(data, dict) or type(data.get("total_count")) is not int
                or not isinstance(data.get("check_runs"), list)
                or (total is not None and data["total_count"] != total)):
            raise plan_github.GithubError("commit checks: malformed or truncated response")
        total = data["total_count"]
        runs.extend(data["check_runs"])
        if len(runs) >= total or len(data["check_runs"]) < CHECK_RUNS_PAGE:
            break
    if total is None or len(runs) != total:
        raise plan_github.GithubError("commit checks: malformed or truncated response")
    return runs


def commit_ci(project: Project, sha: str, gh=None, *, branch: str | None = None,
              required=()) -> dict:
    """L160g (#473, `ci_required`): GitHub's CI on the commit `sha` itself
    (Actions, external app checks and commit statuses), the latest activity
    of each workflow database ID: `{runs,
    green, pending: [workflows], failed: [workflows], contexts: {label:
    success|pending|failed}, statuses: {context: success|pending|failed}}`;
    green needs at least one check, all concluded and passing. Raises
    `plan_github.GithubError` when it cannot be read (never taken for
    « no CI »).

    L1651 (#1650): `contexts` carries the same reading label by label, so the
    gate can prove that a part the change set makes due **exists** on the
    frozen commit; a `skipped` run or check is `success` for the rollup (it
    blocks nothing) but `failed` here — a due part that did not run is not a
    green one.

    N1 de #1666 : `contexts` mêle les libellés des trois espaces (runs,
    checks, statuts). Les parts dues d'un projet (`ci_parts`) sont des
    **statuts de commit** : `statuses` les rend seuls, pour qu'un run ou un
    check portant le nom d'une part ne puisse pas en tenir lieu.

    #3085/#3186 : `acceptable` (aucun négatif, rien en cours ni absent) porte
    la règle du 23/10 ; `inconclusive` liste les non concluants consignés.
    `required` (paires `(nom, app_id | None)` ou noms : les checks requis de
    la cible) désigne les agrégats requis, lus par leur nom sur les check
    runs du commit, Actions comprises : un requis `FAILURE` est `negative`,
    un requis qui n'a pas conclu lui-même est dans `unconcluded`
    (`[{check, databaseId}]`, le run à relancer) — preuve absente, jamais
    `acceptable` (décision owner #128512). Un run ou un check hors des requis
    qui n'a pas conclu reste non concluant (§3). `required_negative` nomme les
    requis rouges : la release ne les reclasse jamais (B3 d'impl-04)."""
    from datetime import datetime
    from . import plan_github
    slug = github_slug(project)
    if not slug:
        raise plan_github.GithubError("project %s declares no GitHub repository" % project.name)
    gh = gh or plan_github.Gh()
    rows = plan_github._json(gh, ["run", "list", "--repo", slug, "--commit", sha, "--limit",
                                  "100", "--json",
                                  "workflowDatabaseId,workflowName,name,status,conclusion,createdAt,startedAt,updatedAt,headSha,event"])
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
    #: #3085 : le classement négatif / non concluant / vert de chaque source,
    #: pour que la porte de release ne prenne pas un job annulé ou sauté pour
    #: un défaut prouvé.
    results: list[tuple[str, str]] = [
        (label(identity, run), run_result(run))  # #3209 : garde workflow_run sautée
        for identity, run in latest.items()]
    #: #3186 (décision owner #128512) : les agrégats requis de la cible, lus
    #: sur les check runs du commit (les derniers par application et nom).
    wanted = []
    for value in (required,) if isinstance(required, str) else tuple(required or ()):
        name, app_id = (value, None) if isinstance(value, str) else tuple(value)
        if str(name or "").strip():
            wanted.append((str(name), app_id))
    required_latest: dict = {}
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
    external = {}
    activity = {}
    for check in commit_check_runs(gh, slug, sha):
        if (not isinstance(check, dict) or check.get("head_sha") != sha
                or not isinstance(check.get("app"), dict)
                or type(check["app"].get("id")) is not int
                or type(check.get("id")) is not int or not check.get("name")):
            raise plan_github.GithubError("commit checks: invalid identity or frozen SHA")
        if any(check["name"] == name and app_id in (None, check["app"]["id"])
               for name, app_id in wanted):
            # le dernier essai l'emporte ; un essai inachevé n'est jamais masqué.
            # R3 cx2 : la suite (un run de workflow) entre dans la clé, comme au
            # rollup du paquet (#3008) — un workflow vert ne masque pas
            # l'agrégat homonyme jamais conclu d'un autre workflow ; seuls les
            # essais d'une même suite se remplacent.
            suite = check.get("check_suite")
            suite = suite.get("id") if isinstance(suite, dict) else None
            key = (check["app"]["id"], suite if type(suite) is int else -1, check["name"])
            rank = (check.get("status") != "completed", check["id"])
            if key not in required_latest or rank > required_latest[key][0]:
                required_latest[key] = (rank, check)
        # #3251 : le check de détail de l'agrégat est le seul check Actions qui
        # porte une information absente du run (une part interne annulée alors
        # que l'agrégat a conclu) ; il passe par la lecture commune ci-dessous.
        # Les autres checks Actions sont couverts par leur exécution (les jobs
        # du run), pas par une seconde lecture.
        if (check["app"].get("slug") == "github-actions"
                and check["name"] != AGGREGATE_DETAIL_CHECK):
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
        # Le check de détail garde son nom seul : la clé du lot de correction
        # (#3209, `nonconclusive_source`) doit être la même au paquet et à la
        # release, sans identifiant d'application devant.
        label = (check["name"] if check["name"] == AGGREGATE_DETAIL_CHECK
                 else "%s/%s" % (check["app"]["id"], check["name"]))
        results.append((label, conclusion_result(check.get("status"),
                                                 check.get("conclusion"))))
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
    #: N1 de #1666 : les parts dues d'un projet (`ci_parts`) sont des statuts
    #: de commit ; ce dictionnaire ne retient que cet espace-là, là où
    #: `contexts` mêle aussi les libellés de runs et de checks.
    status_contexts: dict = {}

    def note_status(name: str, state: str) -> None:
        """Même règle que `note` : le plus fort des états d'un même contexte
        l'emporte — un contexte vert ne rachète pas le même contexte rouge."""
        if CI_CONTEXT_RANK[state] > CI_CONTEXT_RANK.get(status_contexts.get(name), -1):
            status_contexts[name] = state

    for status in statuses["statuses"]:
        if not isinstance(status, dict) or not status.get("context"):
            raise plan_github.GithubError("commit statuses: invalid context")
        results.append((status["context"], state_result(status.get("state"))))
        if status.get("state") == "pending":
            pending.append(status["context"])
            note(status["context"], "pending")
            note_status(status["context"], "pending")
        elif status.get("state") != "success":
            failed.append(status["context"])
            note(status["context"], "failed")
            note_status(status["context"], "failed")
        else:
            note(status["context"], "success")
            note_status(status["context"], "success")
    count = len(latest) + len(external) + len(statuses["statuses"])
    # #1748 (a): the declared prefixes of `branch` need a `success` status
    missing = _status_success([(s["context"], s.get("state")) for s in statuses["statuses"]],
                              required_statuses(project, branch))
    # #3085 : un résultat non concluant (job sauté, annulé pour quota, panne
    # de démarrage) ne rend pas la tête rouge ; il est listé pour être
    # consigné. `green` garde son sens strict, `acceptable` porte la règle.
    negative = sorted({name for name, result in results
                       if result == CI_NEGATIVE_RESULT})
    unconcluded, required_negative = [], []
    for name, app_id in wanted:
        rows = [check for (app, _suite, check_name), (_rank, check)
                in sorted(required_latest.items())
                if check_name == name and app_id in (None, app)]
        if any(conclusion_result(c.get("status"), c.get("conclusion")) == CI_NEGATIVE_RESULT
               for c in rows):
            negative = sorted(set(negative) | {name})
            required_negative.append(name)
            # B1 cx2 (#3186) : le requis rouge d'une suite homonyme est un
            # échec, même quand chaque workflow se dit vert ; `failed` le
            # nomme, et `green` tombe avec lui.
            if name not in failed:
                failed.append(name)
        elif any(c.get("status") == "completed" and never_concluded(c.get("conclusion"))
                 for c in rows):
            unconcluded.extend(
                {"check": name, "databaseId": actions_run_id(slug, c)} for c in rows
                if c.get("status") == "completed" and never_concluded(c.get("conclusion")))
    absent = {row["check"] for row in unconcluded}
    inconclusive = sorted({name for name, result in results
                           if result == CI_INCONCLUSIVE_RESULT} - absent)
    acceptable = bool(count) and not pending and not negative and not missing \
        and not unconcluded
    # un agrégat requis jamais conclu n'est jamais vert, même dans un run
    # qui se dit vert (R3 cx2) ; sans `required`, `green` est inchangé.
    return {"runs": count, "green": bool(count) and not pending and not failed and not missing
            and not unconcluded,
            "acceptable": acceptable, "negative": negative, "inconclusive": inconclusive,
            "unconcluded": unconcluded, "required_negative": sorted(set(required_negative)),
            "pending": sorted(pending), "failed": sorted(failed), "missing": missing,
            "contexts": contexts, "statuses": status_contexts}


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


def _run_jobs(gh, run: dict, slug: str, *, empty_ok: bool = False) -> list:
    """L756 (#566): les jobs d'une exécution, pour refuser un vert sans tests.

    Rend `[(nom, statut, conclusion)]`. Une lecture impossible, vide ou
    tronquée lève `GithubError` : un job qu'on ne peut pas lire ne vaut jamais
    vert. `empty_ok` distingue la lecture de tous les runs d'un commit
    (#2692), où une exécution sautée sans job ne prouve rien, sans être une
    lecture ratée. Le dépôt est nommé (`--repo`), jamais déduit du répertoire
    courant : `Gh.run` ne fixe ni `cwd` ni `GH_REPO`, et `gh run view` sans
    `--repo` échoue hors d'un clone (« failed to determine base repo ») — R2 de
    mty-amh-qa-01 sur PR manaty/ameesh#633."""
    from . import plan_github
    run_id = run.get("databaseId")
    if type(run_id) is not int or run_id <= 0:
        raise plan_github.GithubError("gh run view: missing or invalid run id")
    data = plan_github._json(gh, ["run", "view", str(run_id), "--repo", slug, "--json", "jobs"])
    if (not isinstance(data, dict) or not isinstance(data.get("jobs"), list)
            or (not data["jobs"] and not empty_ok)):
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


def commit_suite(project: Project, sha: str, gh=None) -> dict:
    """#2692: les exécutions et leurs jobs sur le commit exact `sha`.

    Une suite complète de paquet peut être répartie sur plusieurs exécutions
    (le run `pull_request` de la PR porte la matrice, le portier `paquet` les
    flux) : l'appelant compare les **jobs**, jamais un nom de workflow ni un
    seul run vert. Rend `{head, runs: [{id, url, workflow, event, conclusion,
    jobs: [{name, status, conclusion}]}]}` ; une lecture impossible ou
    tronquée lève `GithubError`, une exécution sans job est gardée vide (elle
    ne prouve aucun job)."""
    from . import plan_github
    slug = github_slug(project)
    if not slug:
        raise plan_github.GithubError("project %s declares no GitHub repository"
                                      % project.name)
    gh = gh or plan_github.Gh()
    rows = plan_github._json(gh, ["run", "list", "--repo", slug, "--commit", sha,
                                  "--limit", "100", "--json",
                                  "databaseId,workflowName,name,event,status,conclusion,"
                                  "headSha,url"])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise plan_github.GithubError("gh run list: unexpected output")
    if len(rows) >= 100:
        raise plan_github.GithubError("gh run list: result may be truncated; cannot certify "
                                      "the full suite")
    if any(r.get("headSha") != sha for r in rows):
        raise plan_github.GithubError("gh run list: a run does not match the frozen commit")
    runs = []
    for run in rows:
        run_id = run.get("databaseId")
        if type(run_id) is not int or run_id <= 0:
            raise plan_github.GithubError("gh run list: missing or invalid run id")
        run_conclusion = (run.get("conclusion") or "").lower()
        # A skipped workflow executed no job: reading its (empty) job list
        # would spend an API call to prove nothing.
        jobs = [] if run_conclusion == "skipped" else _run_jobs(
            gh, run, slug, empty_ok=True)
        runs.append({
            "id": run_id,
            "url": run.get("url") or "",
            "workflow": run.get("workflowName") or run.get("name") or "",
            "event": run.get("event") or "",
            "conclusion": run_conclusion,
            "jobs": [dict(name=name, status=status, conclusion=conclusion)
                     for name, status, conclusion in jobs],
        })
    return {"head": sha, "runs": runs}


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
    leur cible, numéros de migration en double (mêmes clés que `_states`).

    #2238: a pair, a stale resolution and a conflict with the target carry
    their `fingerprint` — the lots' heads, their target's head and the
    resolution commit, never the replay state (which can vary from one pass
    to the next while nothing changes); `send_mail` does not resend a notice
    whose inputs have not moved."""
    heads = {b["work_item"]: b for b in report.get("branches") or ()}

    def fingerprint(lots, tips=(), resolution=None) -> str:
        tips = list(tips or ())
        parts = []
        for i, lot in enumerate(lots):
            one = heads.get(lot) or {}
            tip = (tips[i] if i < len(tips) else None) or one.get("tip") or ""
            parts.append("%d:%s@%s" % (lot, tip, one.get("target_head") or ""))
        if resolution:
            parts.append("resolution:%s" % resolution)
        return "|".join(parts)

    out = {}
    for pair in report.get("pairs") or ():
        if pair["state"] in ("conflict", "unclassified"):
            out["pair:%d-%d" % tuple(pair["lots"])] = {
                "kind": "pair", "lots": list(pair["lots"]),
                "files": pair["files"] or pair.get("uncertain_files") or [],
                "target": pair["target"], "resolver": pair.get("resolver"),
                "attribution_pending": pair.get("attribution_pending", False),
                "fingerprint": fingerprint(pair["lots"], pair.get("tips"),
                                           (pair.get("resolution") or {}).get("commit"))}
        elif pair["state"] == "stale":
            # L160d: only the lot that prepared the resolution is told
            resolution = pair["resolution"]
            out["stale:%d-%d" % tuple(pair["lots"])] = {
                "kind": "stale", "lots": [resolution["by"]], "pair": list(pair["lots"]),
                "files": resolution.get("remaining") or pair["files"],
                "target": pair["target"], "commit": resolution["commit"],
                "fingerprint": fingerprint(pair["lots"], pair.get("tips"),
                                           resolution["commit"])}
    for one in report.get("against_target") or ():
        if one["state"] == "conflict":
            out["target:%d" % one["work_item"]] = {
                "kind": "target", "lots": [one["work_item"]], "files": one["files"],
                "target": one["target"],
                "fingerprint": fingerprint([one["work_item"]], [one.get("tip")])}
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
    for conflict in out.values():
        if "fingerprint" not in conflict:
            conflict["fingerprint"] = json.dumps(
                [[(lot, (heads.get(lot) or {}).get("tip")) for lot in conflict["lots"]],
                 conflict["files"], conflict.get("holder")], sort_keys=True)
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
        if conflict.get("attribution_pending"):
            todo = ("The raw merge also contains inherited or unclassified conflicts. "
                    "No resolver is assigned. Consult each branch's target diagnostics; "
                    "pair attribution will be retried when the branches or target change.")
        elif resolver.get("lot") == lot:
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


def _key_lots(key: str) -> list[int] | None:
    """The lots a conflict key names (`pair:a-b`, `stale:a-b`, `target:n`),
    or None for a key without readable lots (migrations)."""
    kind, _, rest = key.partition(":")
    try:
        if kind in ("pair", "stale"):
            return [int(n) for n in rest.split("-")]
        if kind == "target":
            return [int(rest)]
    except ValueError:
        return None
    return None


def _lot_inputs(fingerprint):
    """A `conflicts_of` fingerprint without the target's head (`lot:tip@head`
    becomes `lot:tip`); JSON fingerprints carry no target head (#2602)."""
    if not fingerprint or fingerprint.startswith("["):
        return fingerprint
    return re.sub(r"@[^|]*", "", fingerprint)


def send_mail(cfg, db, project: Project, report: dict, *, ci="github") -> list[dict]:
    """Le courrier d'un passage : un message par lot concerné et par conflit
    nouveau (passif), puis un réveil quand un conflit avec la cible bloque
    une PR verte. Rend les envois `{key, lot, to, level, message_id}`.

    #2238: a conflict that leaves a pass while all its lots stay on the radar
    (the resolution replay read in one pass, not in the next) is kept
    "dormant" with its `fingerprint`; if it comes back on the same inputs
    (heads, target, resolution), no notice is resent. It is new again only
    when its inputs changed, or when one of its lots left the radar (merged,
    closed)."""
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
        fingerprint = conflict.get("fingerprint")
        dormant = done.pop("dormant", False)
        known = done.get("fp")
        if (dormant and known != fingerprint) or (
                known is not None
                and _lot_inputs(known) != _lot_inputs(fingerprint)):
            # #2238: back on other inputs than the notice already sent.
            # #2602: changed lot heads are new inputs, even without an absent
            # pass; the target's head alone is not (it moves every packet).
            # #2640: a state written before the JSON fingerprints of
            # migration/unreserved conflicts has no `fp`; upgrading the code
            # must not resend a notice already delivered.
            done = {}
        for lot in conflict["lots"]:
            one = branches.get(lot) or {}
            to = one.get("assignee")
            if not to or to.startswith(work.HUMAN_PREFIX):
                continue
            recipient_key = "%s:%s" % (lot, to)
            # Upgrade the old per-lot ledger without resending existing mail.
            previous = done.get(recipient_key, done.get(str(lot)))
            level, status = "passive", None
            if conflict["kind"] == "target" and previous != "blocking" and ci:
                status = ci(one["branch"])
                if status and status.get("green"):
                    level = "blocking"
            if previous in (level, "blocking"):
                done[recipient_key] = previous
                done.pop(str(lot), None)
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
            done[recipient_key] = level
            done.pop(str(lot), None)
            sent.append({"key": key, "lot": lot, "to": to, "level": level,
                         "message_id": message_id})
        if done:
            if fingerprint is not None:
                done["fp"] = fingerprint
            done["lots"] = conflict["lots"]
            keys[key] = done
    # #2238: a conflict absent from this pass whose lots all stay on the
    # radar keeps its notice, dormant, instead of being forgotten.
    for key, done in before.items():
        if key in current or "fp" not in done:
            continue
        lots = done.get("lots") or _key_lots(key)
        if lots and all(n in branches for n in lots):
            keys[key] = dict(done, dormant=True)
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
              s["pairs_checked"], len(report.get("pairs") or ()),
              s["conflict"] + s["prepared"] + s["stale"], s["prepared"], s["stale"],
              s["target_conflict"], s["migration_duplicates"])]
    if s["inherited"] or s["unclassified"] or s["superseded"]:
        out.append("  pair attribution: %d inherited target conflict(s), %d unclassified pair(s), "
                   "%d superseded raw conflict(s)"
                   % (s["inherited"], s["unclassified"], s["superseded"]))
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
        files = (pair["shared"] if pair["state"] == "overlap" else
                 pair["files"] or pair.get("uncertain_files") or pair.get("inherited_files") or [])
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
        if pair.get("attribution_pending"):
            out.append("    attribution pending; inherited: %s; unclassified: %s"
                       % (_files(pair.get("inherited_files")),
                          _files(pair.get("uncertain_files"))))
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
