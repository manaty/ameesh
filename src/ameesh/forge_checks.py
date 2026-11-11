# SPDX-License-Identifier: AGPL-3.0-only
"""#6832 — la déclaration des checks requis, versionnée avec le code.

`radar.projects.<p>.required_checks` vit dans `~/.config/ameesh/config.json` :
une déclaration posée sur un hôte ne suit pas la relève (§11, bascule en moins
de cinq minutes). Le 11/11, le pilote a échoué d'AG2 vers AG1 et `merge-pr` a
refusé Nexlink : AG1 n'avait pas la déclaration (#211196, #5152).

Ce module porte la même déclaration dans le paquet, donc versionnée avec le
code et installée sur chaque hôte par le chemin de staging (données de paquet,
`pip install`) : la garde de fusion la lit quel que soit l'hôte qui a pris le
bail, sans geste d'hôte ni dérive de profil. Le fichier
`forge_checks.json` est petit et strict : schéma inconnu, dépôt en double,
clé inconnue ou nom de check vide refusent la lecture — une déclaration
illisible ne s'ignore pas.

Une déclaration **vide** garde le sens de #3428 : elle ne couvre que le 403 de
**plan** GitHub (protection et rulesets indisponibles sur un dépôt privé hors
Pro), jamais un 403 de droits.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

SCHEMA = "ameesh-forge-checks/1"
#: le fichier livré avec le paquet ; `path` reste injectable pour les tests
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forge_checks.json")

_TOP_KEYS = frozenset(("schema", "projects"))
_DECL_KEYS = frozenset(("github", "required_checks", "why"))
#: `owner/repo`, casse significative seulement pour l'affichage : GitHub ne
#: la distingue pas, la clé de lecture est en minuscules (`_repo_key`)
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ORIGIN_PREFIX = re.compile(r"^(?:https?://|git@)?(?:www\.)?github\.com[/:]")

#: cache par chemin : `(mtime_ns, taille)` → déclarations validées
_CACHE: dict = {}


class InventoryError(ValueError):
    """La déclaration versionnée est illisible ou invalide : elle ne s'ignore pas."""


@dataclass(frozen=True)
class Declaration:
    """La déclaration d'un projet : son dépôt et ses checks requis.

    `required_checks` vide est une **déclaration**, pas une absence : elle
    couvre le seul 403 de plan GitHub (#3428)."""

    project: str
    github: str
    required_checks: tuple
    why: str = ""


def _repo_key(value) -> str:
    """`owner/repo` normalisé (minuscules, sans URL ni suffixe `.git`)."""
    text = str(value or "").strip().lower()
    text = _ORIGIN_PREFIX.sub("", text)
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text


def _pairs(pairs):
    """`object_pairs_hook` : un doublon JSON est une erreur, pas un écrasement."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise InventoryError("duplicate key %r" % (key,))
        out[key] = value
    return out


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryError("%s: non-empty text expected" % where)
    return value


def _parse(raw, path: str) -> tuple:
    if not isinstance(raw, dict):
        raise InventoryError("%s: JSON object expected" % path)
    unknown = sorted(set(map(str, raw)) - _TOP_KEYS)
    if unknown:
        raise InventoryError("%s: unknown top-level key(s) %s (allowed: %s)"
                             % (path, ", ".join(unknown), ", ".join(sorted(_TOP_KEYS))))
    if raw.get("schema") != SCHEMA:
        raise InventoryError("%s: schema must be %r, not %r"
                             % (path, SCHEMA, raw.get("schema")))
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        raise InventoryError("%s.projects: object expected" % path)
    out = []
    seen: dict = {}
    for name, value in projects.items():
        name = str(name).strip()
        where = "%s.projects.%s" % (path, name)
        if not name:
            raise InventoryError("%s.projects: empty project name" % path)
        if not isinstance(value, dict):
            raise InventoryError("%s: object expected" % where)
        extra = sorted(set(map(str, value)) - _DECL_KEYS)
        if extra:
            raise InventoryError("%s: unknown key(s) %s (allowed: %s)"
                                 % (where, ", ".join(extra), ", ".join(sorted(_DECL_KEYS))))
        github = _repo_key(_text(value.get("github"), where + ".github"))
        if not _REPO_RE.match(github):
            raise InventoryError("%s.github: owner/repo expected, not %r"
                                 % (where, value.get("github")))
        if github in seen:
            raise InventoryError("%s.github: %s is already declared by %s"
                                 % (where, github, seen[github]))
        seen[github] = name
        checks = value.get("required_checks")
        if not isinstance(checks, list):
            raise InventoryError("%s.required_checks: list of check names expected" % where)
        for check in checks:
            if not isinstance(check, str) or not check.strip():
                raise InventoryError("%s.required_checks: non-empty check name expected"
                                     % where)
        if len(set(checks)) != len(checks):
            raise InventoryError("%s.required_checks: duplicate check name" % where)
        why = value.get("why", "")
        if why is not None:
            why = _text(why, where + ".why")
        out.append(Declaration(name, github, tuple(checks), why or ""))
    return tuple(out)


def inventory(path: str | None = None) -> tuple | None:
    """Les déclarations versionnées, ou None si le fichier n'est pas installé.

    Un fichier absent n'est pas une erreur : l'hôte peut tourner une version
    antérieure à #6832 (le doctor le dit, la garde refuse alors un 403 de plan
    sans déclaration). Un fichier présent mais invalide lève `InventoryError`."""
    path = os.path.abspath(path or PATH)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InventoryError("%s: unreadable (%s)" % (path, exc)) from exc
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream, object_pairs_hook=_pairs)
    except InventoryError:
        raise
    except (OSError, ValueError) as exc:
        raise InventoryError("%s: unreadable (%s)" % (path, exc)) from exc
    parsed = _parse(raw, path)
    _CACHE[path] = (key, parsed)
    return parsed


def declarations(path: str | None = None) -> dict:
    """Les déclarations indexées par `owner/repo` (minuscules)."""
    return {item.github: item for item in (inventory(path) or ())}


def declared(repo, path: str | None = None):
    """La déclaration versionnée de `repo` : un tuple (vide possible), ou None
    quand aucune déclaration n'est installée. Un fichier invalide lève
    `InventoryError` : un appelant qui garde une fusion doit refuser."""
    key = _repo_key(repo)
    if not key:
        return None
    found = declarations(path).get(key)
    return None if found is None else found.required_checks


def _one_line(exc) -> str:
    return " ".join(str(exc).split())[:160]


def _merged_projects(cfg) -> dict:
    """`(owner/repo, cible)` → nom de projet, pour tout ce que l'hôte fusionne.

    `github.projects` (la cible `base`, `main` par défaut), `release.projects`
    (son `github` et son `target`) et les projets radar déclarés avec un
    `github` explicite. Un projet sans dépôt GitHub nommable (déduit d'un
    `origin`) n'est pas sondable ici : il n'est pas inventé."""
    out: dict = {}

    def add(name, github, targets) -> None:
        repo = _repo_key(github)
        if not repo or not _REPO_RE.match(repo):
            return
        if isinstance(targets, str):
            targets = (targets,)
        for target in targets or ():
            target = str(target or "").strip()
            if target:
                out.setdefault((repo, target), str(name))

    github = getattr(cfg, "github", None) or {}
    for name, value in (github.get("projects") or {}).items():
        if isinstance(value, str):
            add(name, value, ("main",))
        elif isinstance(value, dict):
            add(name, value.get("repo"), value.get("base") or ("main",))
    release = getattr(cfg, "release", None) or {}
    for name, value in (release.get("projects") or {}).items():
        if isinstance(value, dict):
            add(name, value.get("github"), (value.get("target") or "main",))
    radar = getattr(cfg, "radar", None) or {}
    for name, value in (radar.get("projects") or {}).items():
        if isinstance(value, dict):
            add(name, value.get("github"), (value.get("target") or "main",))
    return out


def _host_declared(cfg, repo: str) -> tuple:
    """`(déclarée, noms)` de la déclaration `radar.projects` de CET hôte.

    Elle reste lue par la garde (elle complète la versionnée, jamais l'inverse) ;
    le doctor la reconnaît pour ne pas condamner un hôte qui garde la porte
    fermée, tout en signalant qu'elle ne suit pas la relève."""
    from . import radar as radar_mod
    try:
        settings = radar_mod.settings_of(cfg)
    except radar_mod.SettingsError:
        return False, ()
    for project in settings.projects.values():
        if _repo_key(project.github) == repo and project.required_checks_declared:
            return True, tuple(project.required_checks)
    return False, ()


def _plan_refusal(slug: str, target: str, gh) -> tuple:
    """`(403 de plan prouvé, autres lectures)` pour la protection et les rulesets."""
    from . import fire, land
    plan = False
    others: list = []
    for read in (fire._protection_required, fire._ruleset_required):
        try:
            read(slug, target, gh=gh)
        except land.ForbiddenLandError as exc:
            if fire._plan_unprotected(str(exc)):
                plan = True
            else:
                others.append("rights 403")
        except land.LandError as exc:
            others.append(_one_line(exc))
    return plan, others


def doctor_lines(cfg, db, *, gh=None, path: str | None = None) -> tuple:
    """#6832 : le doctor de l'hôte, face aux déclarations des projets qu'il
    fusionne. Rend `(ko, lignes)`.

    Un hôte **candidat d'un rôle élu** (§11) doit pouvoir prouver la garde de
    chaque projet qu'il fusionne : quand la forge répond 403 de **plan** sur
    une cible et qu'aucune déclaration (versionnée ou `radar.projects` de cet
    hôte) ne la couvre, c'est un KO qui nomme le projet et la cible — c'est
    exactement le refus de `merge-pr` après une relève (#211196). Un 403 de
    droits, une lecture passagère ou un `gh` sans jeton restent des
    non-concluants : ils n'affaiblissent aucune source, ils ne bloquent pas
    (§3). Hors candidat d'un rôle élu, rien n'est vérifié : cet hôte ne
    fusionne pas au nom du mesh."""
    from . import role_standings_pg
    lines: list = []
    try:
        roles = role_standings_pg.elected_roles(db, cfg.host)
    except Exception as exc:  # noqa: BLE001 - une lecture ne casse pas le doctor
        return False, ["not checked (elected roles unreadable: %s)" % _one_line(exc)]
    if not roles:
        return False, ["not a candidate host of an elected role; declarations not checked"]
    projects = _merged_projects(cfg)
    if not projects:
        return False, ["candidate host (%s) but no GitHub project declared; nothing to check"
                       % ", ".join(sorted(str(role.get("name")) for role in roles))]
    try:
        installed = inventory(path)
    except InventoryError as exc:
        return True, ["KO — the versioned declaration inventory is unreadable: %s"
                      % _one_line(exc)]
    known = {item.github: item for item in (installed or ())}
    if installed is None:
        lines.append("no versioned declaration inventory installed (%s)" % (path or PATH))
    if gh is None:
        try:
            from . import packet_automerge
            from .modules.dev import plan_github
            gh = packet_automerge.gh_of(plan_github.Gh())
        except Exception as exc:  # noqa: BLE001 - sans gh, rien n'est prouvé
            lines.append("not checked (no GitHub reader: %s)" % _one_line(exc))
            return False, lines
    ko = False
    for (repo, target), name in sorted(projects.items()):
        versioned = known.get(repo)
        if versioned is not None:
            lines.append("%s (%s, %s): versioned declaration installed (%d check(s))"
                         % (name, repo, target, len(versioned.required_checks)))
            continue
        host_declared, host_names = _host_declared(cfg, repo)
        if host_declared:
            lines.append("%s (%s, %s): WARNING host-only radar declaration (%d check(s)); "
                         "it does not follow the failover — move it to "
                         "src/ameesh/forge_checks.json"
                         % (name, repo, target, len(host_names)))
            continue
        plan, others = _plan_refusal(repo, target, gh)
        if plan:
            ko = True
            lines.append("KO — %s (%s, %s): the forge proves a plan 403 and no declaration "
                         "covers it; merge-pr would refuse after a failover (add it to "
                         "src/ameesh/forge_checks.json)" % (name, repo, target))
        elif others:
            lines.append("%s (%s, %s): not conclusive (%s); no declaration needed"
                         % (name, repo, target, ", ".join(others)))
        else:
            lines.append("%s (%s, %s): protection readable; no declaration needed"
                         % (name, repo, target))
    return ko, lines
