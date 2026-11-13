# SPDX-License-Identifier: AGPL-3.0-only
"""#6832 — la déclaration des checks requis, versionnée dans le canon.

`radar.projects.<p>.required_checks` vit dans la configuration d'un hôte : une
déclaration posée sur un hôte ne suit pas la relève (§11, bascule en moins de
cinq minutes). Le 11/11, le pilote a échoué d'AG2 vers AG1 et `merge-pr` a
refusé Nexlink : AG1 n'avait pas la déclaration (#211196, #5152).

La déclaration vit désormais dans le **canon du mesh**
(`federation.yaml`, `extensions.ameesh.forge_checks`), pas dans le paquet :
ameesh reste générique, ses valeurs par défaut et son schéma ne nomment aucun
client (§7). Le bloc se lit au commit exact du canon appliqué
(`canon.load_configured` + `canon.git_read`), comme les paramètres mécaniques
(#2451), et il suit le déploiement staging qui synchronise le canon sur
chaque hôte — aucun geste d'hôte. Le lecteur est strict : schéma inconnu,
dépôt en double, clé inconnue ou nom de check vide refusent la lecture — une
déclaration illisible ne s'ignore pas.

Une déclaration **vide** garde le sens de #3428 : elle ne couvre que le 403 de
**plan** GitHub (protection et rulesets indisponibles sur un dépôt privé hors
Pro), jamais un 403 de droits.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import canon

SCHEMA = "ameesh-forge-checks/1"
#: le manifeste du canon qui porte le bloc `extensions.ameesh.forge_checks`
MANIFEST = "federation.yaml"

_TOP_KEYS = frozenset(("schema", "projects"))
_DECL_KEYS = frozenset(("github", "required_checks", "why"))
#: `owner/repo`, casse significative seulement pour l'affichage : GitHub ne
#: la distingue pas, la clé de lecture est en minuscules (`_repo_key`)
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ORIGIN_PREFIX = re.compile(r"^(?:https?://|git@)?(?:www\.)?github\.com[/:]")

#: déclarations déjà compilées, par `(dépôt git, commit exact)` : un commit
#: ne change pas, une passe ne relit donc pas le manifeste par appel
_CACHE: dict = {}
#: canons déjà chargés par `(path, ref, untrusted)` et leur date de lecture :
#: une rafale de gardes (file, feu) ne recharge pas tout le canon à chaque lot
_LOADED: dict = {}
_LOADED_TTL_S = 30.0


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


@dataclass(frozen=True)
class Evidence:
    """Ce que le canon appliqué déclare, ou pourquoi il ne déclare rien.

    `declarations is None` : aucune source (canon absent ou bloc absent) ;
    `declarations == ()` : le bloc existe et ne déclare aucun projet."""

    declarations: tuple | None
    label: str = ""
    reason: str = ""


def _repo_key(value) -> str:
    """`owner/repo` normalisé (minuscules, sans URL ni suffixe `.git`)."""
    text = str(value or "").strip().lower()
    text = _ORIGIN_PREFIX.sub("", text)
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryError("%s: non-empty text expected" % where)
    return value


def _parse(raw, where: str) -> tuple:
    if not isinstance(raw, dict):
        raise InventoryError("%s: mapping expected" % where)
    unknown = sorted(set(map(str, raw)) - _TOP_KEYS)
    if unknown:
        raise InventoryError("%s: unknown top-level key(s) %s (allowed: %s)"
                             % (where, ", ".join(unknown), ", ".join(sorted(_TOP_KEYS))))
    if raw.get("schema") != SCHEMA:
        raise InventoryError("%s: schema must be %r, not %r"
                             % (where, SCHEMA, raw.get("schema")))
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        raise InventoryError("%s.projects: mapping expected" % where)
    out = []
    seen: dict = {}
    for name, value in projects.items():
        name = str(name).strip()
        scope = "%s.projects.%s" % (where, name)
        if not name:
            raise InventoryError("%s.projects: empty project name" % where)
        if not isinstance(value, dict):
            raise InventoryError("%s: mapping expected" % scope)
        extra = sorted(set(map(str, value)) - _DECL_KEYS)
        if extra:
            raise InventoryError("%s: unknown key(s) %s (allowed: %s)"
                                 % (scope, ", ".join(extra), ", ".join(sorted(_DECL_KEYS))))
        github = _repo_key(_text(value.get("github"), scope + ".github"))
        if not _REPO_RE.match(github):
            raise InventoryError("%s.github: owner/repo expected, not %r"
                                 % (scope, value.get("github")))
        if github in seen:
            raise InventoryError("%s.github: %s is already declared by %s"
                                 % (scope, github, seen[github]))
        seen[github] = name
        checks = value.get("required_checks")
        if not isinstance(checks, list):
            raise InventoryError("%s.required_checks: list of check names expected" % scope)
        for check in checks:
            if not isinstance(check, str) or not check.strip():
                raise InventoryError("%s.required_checks: non-empty check name expected"
                                     % scope)
        if len(set(checks)) != len(checks):
            raise InventoryError("%s.required_checks: duplicate check name" % scope)
        why = value.get("why", "")
        if why is not None:
            why = _text(why, scope + ".why")
        out.append(Declaration(name, github, tuple(checks), why or ""))
    return tuple(out)


def _block(manifest, where: str):
    """Le bloc `extensions.ameesh.forge_checks` du manifeste, ou None.

    Un bloc absent n'est pas une erreur : un canon qui ne déclare rien garde
    le comportement d'avant #6832 (un 403 de plan sans déclaration refuse).
    Un bloc présent mais mal formé refuse : il ne s'ignore pas."""
    if not isinstance(manifest, dict):
        raise InventoryError("%s: mapping expected" % where)
    extensions = manifest.get("extensions")
    if extensions is None:
        return None
    if not isinstance(extensions, dict):
        raise InventoryError("%s.extensions: mapping expected" % where)
    ameesh = extensions.get("ameesh")
    if ameesh is None:
        return None
    if not isinstance(ameesh, dict):
        raise InventoryError("%s.extensions.ameesh: mapping expected" % where)
    block = ameesh.get("forge_checks")
    if block is None:
        return None
    return block


def _configured_canons(cfg) -> list:
    """Les canons configurés, relus au plus une fois par fenêtre courte.

    Le chargement du canon (git, fiches) est coûteux ; la garde de fusion le
    demande à chaque geste. Un commit ne change pas, la compilation des
    déclarations est de toute façon mémoïsée par `(dépôt, commit)`."""
    if cfg is None:
        return []
    try:
        entries = canon.configured(cfg)
    except Exception:  # noqa: BLE001 - une configuration illisible se dira plus bas
        entries = ()
    key = tuple((getattr(entry, "path", ""), getattr(entry, "ref", ""),
                 bool(getattr(entry, "untrusted", False))) for entry in entries)
    now = time.monotonic()
    hit = _LOADED.get(key)
    if hit is not None and now - hit[0] < _LOADED_TTL_S:
        return hit[1]
    try:
        loaded = canon.load_configured(cfg)
    except Exception:  # noqa: BLE001 - l'appelant dira « canon illisible »
        loaded = []
    _LOADED[key] = (now, loaded)
    return loaded


def _manifest_at(canon_obj) -> tuple:
    """`(texte, provenance)` du manifeste au commit exact, ou `(None, raison)`."""
    sources = getattr(canon_obj, "sources", None) or []
    if not sources:
        return None, "no member source"
    source = sources[0]
    if getattr(source, "mode", "") != "git" or not getattr(source, "repo", "") \
            or not getattr(source, "commit", ""):
        return None, "no exact git commit"
    if getattr(source, "prefix", ""):
        return None, "canon under a sub-directory (%s)" % source.prefix
    raw = canon.git_read(source.repo, source.commit, MANIFEST)
    if raw is None:
        return None, "%s unreadable at %s" % (MANIFEST, source.commit[:12])
    try:
        return raw.decode("utf-8"), "%s@%s" % (source.repo, source.commit[:12])
    except UnicodeDecodeError:
        return None, "%s is not UTF-8 at %s" % (MANIFEST, source.commit[:12])


def _exact_source(canon_obj):
    """La première source Git au commit exact de ce canon, ou None.

    Un canon en fichiers de travail (`untrusted`) ou sous un sous-dossier n'a
    pas de commit appliqué : il ne peut pas porter une déclaration versionnée,
    ce n'est pas une lecture en erreur (la garde reste forge-seule)."""
    sources = getattr(canon_obj, "sources", None) or []
    if not sources:
        return None
    source = sources[0]
    if getattr(source, "mode", "") != "git" or not getattr(source, "repo", "") \
            or not getattr(source, "commit", "") or getattr(source, "prefix", ""):
        return None
    return source


def _canon_block(canon_obj) -> tuple:
    """`(déclarations, provenance)` du bloc de ce canon, ou None.

    Mémoïsé par `(dépôt, commit)` : le contenu d'un commit est immuable."""
    source = _exact_source(canon_obj)
    if source is None:
        return None
    key = (source.repo, source.commit)
    if key in _CACHE:
        return _CACHE[key]
    text, label = _manifest_at(canon_obj)
    if text is None:
        raise InventoryError("the canon manifest cannot be read: %s" % label)
    try:
        manifest = canon.load_yaml(text)
    except ValueError as exc:
        raise InventoryError("%s: unreadable YAML (%s)" % (label, " ".join(str(exc).split())))
    block = _block(manifest, label)
    if block is None:
        result = None
    else:
        result = (_parse(block, "%s:extensions.ameesh.forge_checks" % label), label)
    _CACHE[key] = result
    return result


def configured(cfg=None) -> Evidence:
    """La déclaration versionnée du canon appliqué, ou la raison de son absence.

    Lit les canons lisibles au commit exact de leur première source Git.
    Aucun canon configuré, un canon illisible ou un manifeste absent ne sont
    pas des erreurs de lecture : la garde refuse alors un 403 de plan comme
    avant #6832. Un bloc **présent** mais invalide lève `InventoryError`."""
    canons = _configured_canons(cfg)
    if not canons:
        return Evidence(None, "", "no readable canon configured")
    declarations: list = []
    labels: list = []
    unreadable: list = []
    unavailable: list = []
    seen: dict = {}
    for canon_obj in canons:
        if not getattr(canon_obj, "readable", False):
            unreadable.append(getattr(canon_obj, "id", "") or "?")
            continue
        if _exact_source(canon_obj) is None:
            unavailable.append(getattr(canon_obj, "id", "") or "?")
            continue
        found = _canon_block(canon_obj)
        if found is None:
            continue
        found_declarations, label = found
        for item in found_declarations:
            if item.github in seen:
                raise InventoryError("%s: %s is already declared by %s"
                                     % (label, item.github, seen[item.github]))
            seen[item.github] = label
        declarations.extend(found_declarations)
        labels.append(label)
    if not labels:
        if unreadable:
            return Evidence(None, "", "no readable canon (unreadable: %s)"
                            % ", ".join(sorted(unreadable)))
        if unavailable:
            return Evidence(None, "", "no exact canon commit (unavailable: %s)"
                            % ", ".join(sorted(unavailable)))
        return Evidence(None, "", "no extensions.ameesh.forge_checks in the canon")
    return Evidence(tuple(declarations), ", ".join(labels))


def declared(repo, cfg=None):
    """La déclaration versionnée de `repo` : un tuple (vide possible), ou None
    quand le canon n'en porte pas. Un bloc invalide lève `InventoryError` :
    un appelant qui garde une fusion doit refuser."""
    key = _repo_key(repo)
    if not key:
        return None
    for item in configured(cfg).declarations or ():
        if item.github == key:
            return item.required_checks
    return None


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


def doctor_lines(cfg, db, *, gh=None) -> tuple:
    """#6832 : le doctor de l'hôte, face aux déclarations des projets qu'il
    fusionne. Rend `(ko, lignes)`.

    Un hôte **candidat d'un rôle élu** (§11) doit pouvoir prouver la garde de
    chaque projet qu'il fusionne : quand la forge répond 403 de **plan** sur
    une cible et qu'aucune déclaration (canon ou `radar.projects` de cet
    hôte) ne la couvre, c'est un KO qui nomme le projet et la cible — c'est
    exactement le refus de `merge-pr` après une relève (#211196). Un 403 de
    droits, une lecture passagère ou un `gh` sans jeton restent des
    non-concluants : ils n'affaiblissent aucune source, ils ne bloquent pas
    (§3). Hors candidat d'un rôle élu, rien n'est vérifié : cet hôte ne
    fusionne pas au nom du mesh. #7059 : un rôle `releve: preference` (avatar)
    n'en est pas un, ses hôtes candidats ne sont pas tenus à cette garde."""
    from . import role_standings_pg
    lines: list = []
    try:
        roles = role_standings_pg.election_roles(db, cfg.host)
    except Exception as exc:  # noqa: BLE001 - une lecture ne casse pas le doctor
        return False, ["not checked (elected roles unreadable: %s)" % _one_line(exc)]
    if not roles:
        return False, ["not a candidate host of an elected role; declarations not checked"]
    projects = _merged_projects(cfg)
    if not projects:
        return False, ["candidate host (%s) but no GitHub project declared; nothing to check"
                       % ", ".join(sorted(str(role.get("name")) for role in roles))]
    try:
        evidence = configured(cfg)
    except InventoryError as exc:
        return True, ["KO — the versioned declaration is unreadable: %s" % _one_line(exc)]
    if evidence.declarations is None:
        if "no readable canon" in evidence.reason or "no exact canon commit" in evidence.reason:
            return True, ["KO — the canon declarations cannot be proven: %s"
                          % _one_line(evidence.reason)]
        lines.append(evidence.reason)
    known = {item.github: item for item in (evidence.declarations or ())}
    if not known:
        lines.append("the canon declares no project (%s)" % (evidence.label or "no block"))
    if gh is None:
        try:
            # `plan_github` est la façade de compatibilité du cœur (#4384),
            # comme dans `fire`/`merge_pr` ; le cœur n'importe jamais
            # l'implémentation d'un module (L97, test_generic_core).
            from . import packet_automerge, plan_github
            gh = packet_automerge.gh_of(plan_github.Gh())
        except Exception as exc:  # noqa: BLE001 - sans gh, rien n'est prouvé
            lines.append("not checked (no GitHub reader: %s)" % _one_line(exc))
            return False, lines
    ko = False
    for (repo, target), name in sorted(projects.items()):
        versioned = known.get(repo)
        if versioned is not None:
            lines.append("%s (%s, %s): versioned declaration installed (%d check(s), %s)"
                         % (name, repo, target, len(versioned.required_checks),
                            evidence.label))
            continue
        host_declared, host_names = _host_declared(cfg, repo)
        if host_declared:
            lines.append("%s (%s, %s): WARNING host-only radar declaration (%d check(s)); "
                         "it does not follow the failover — move it to the canon "
                         "(extensions.ameesh.forge_checks)"
                         % (name, repo, target, len(host_names)))
            continue
        plan, others = _plan_refusal(repo, target, gh)
        if plan:
            ko = True
            lines.append("KO — %s (%s, %s): the forge proves a plan 403 and no declaration "
                         "covers it; merge-pr would refuse after a failover (add it to the "
                         "canon, extensions.ameesh.forge_checks)" % (name, repo, target))
        elif others:
            lines.append("%s (%s, %s): not conclusive (%s); no declaration needed"
                         % (name, repo, target, ", ".join(others)))
        else:
            lines.append("%s (%s, %s): protection readable; no declaration needed"
                         % (name, repo, target))
    return ko, lines


def clear_cache() -> None:
    """Vide les mémoïsations (tests, ou changement de canon dans un processus)."""
    _CACHE.clear()
    _LOADED.clear()
