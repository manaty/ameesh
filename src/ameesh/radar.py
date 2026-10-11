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
   déjà sous un autre nom.

Les fusions d'essai n'écrivent rien dans le dépôt des agents : leurs objets
vont dans un dossier jetable (`GIT_OBJECT_DIRECTORY`, objets du dépôt en
alternates), aucun worktree, aucun `fetch`. Une paire n'est recalculée que si
l'une de ses deux pointes a bougé (cache en mémoire de l'exécuteur).

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
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from . import plan_git
from .canon import GIT_TIMEOUT, GitError, _git, _git_env

SCHEMA = "ameesh-radar/1"
SETTINGS_KEYS = ("projects",)
PROJECT_KEYS = ("repo", "migrations", "target", "github")
#: une migration numérotée : `NNNN_nom.sql`
MIGRATION_RE = re.compile(r"^([0-9]{4})_[^/]+$")
#: paires gardées au plus dans le cache d'un exécuteur
MAX_CACHE = 20000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GITHUB_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: URL `origin` d'un dépôt GitHub (https ou ssh)
_ORIGIN_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
#: conclusions de check qui ne bloquent pas une PR verte
CI_OK = frozenset(("SUCCESS", "NEUTRAL", "SKIPPED"))


class SettingsError(ValueError):
    """Clé `radar` invalide : message actionnable."""


@dataclass(frozen=True)
class Project:
    name: str
    repo: str
    migrations: str = ""
    target: str = ""
    github: str = ""


@dataclass
class Settings:
    projects: dict = field(default_factory=dict)


def parse_settings(raw) -> Settings:
    """Valide la clé `radar` de la configuration de l'hôte."""
    if raw is None or raw == {}:
        return Settings()
    if not isinstance(raw, dict):
        raise SettingsError("`radar` : objet JSON attendu")
    unknown = sorted(set(map(str, raw)) - set(SETTINGS_KEYS))
    if unknown:
        raise SettingsError("`radar` : clé(s) inconnue(s) %s (admise : %s)"
                            % (", ".join(unknown), ", ".join(SETTINGS_KEYS)))
    declared = raw.get("projects") or {}
    if not isinstance(declared, dict):
        raise SettingsError("`radar.projects` : objet {\"projet\": {\"repo\": …}} attendu")
    projects: dict = {}
    for name, value in declared.items():
        name = str(name).strip()
        where = "`radar.projects.%s`" % name
        if not _NAME_RE.match(name):
            raise SettingsError("%s : nom de projet invalide (lettres, chiffres, . _ -)"
                                % where)
        if isinstance(value, str):
            value = {"repo": value}
        if not isinstance(value, dict):
            raise SettingsError("%s : objet {\"repo\": chemin} attendu" % where)
        extra = sorted(set(map(str, value)) - set(PROJECT_KEYS))
        if extra:
            raise SettingsError("%s : clé(s) inconnue(s) %s (admises : %s)"
                                % (where, ", ".join(extra), ", ".join(PROJECT_KEYS)))
        repo = value.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise SettingsError("%s.repo : chemin du dépôt attendu" % where)
        texts = {}
        for key in ("migrations", "target", "github"):
            text = value.get(key) or ""
            if not isinstance(text, str):
                raise SettingsError("%s.%s : texte attendu" % (where, key))
            texts[key] = text.strip().strip("/")
        if texts["target"]:
            plan_git.check_ref(texts["target"], "cible")
        if texts["github"] and not _GITHUB_RE.match(texts["github"]):
            raise SettingsError("%s.github : dépôt « owner/repo » attendu" % where)
        projects[name] = Project(name=name, repo=os.path.expanduser(repo.strip()),
                                 migrations=texts["migrations"], target=texts["target"],
                                 github=texts["github"])
    return Settings(projects=projects)


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
        raise GitError("git %s : délai dépassé" % args[0]) from exc
    except OSError as exc:
        raise GitError("git %s : %s" % (args[0], exc.strerror or exc)) from exc


def _lines(proc) -> list[str]:
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def trial_merge(repo: str, objects: str, scratch: str, a: str, b: str) -> list[str] | None:
    """Fusion d'essai de deux commits : la liste des fichiers en conflit (vide :
    fusion propre), None si git n'a pas su la calculer."""
    proc = _scratch_git(repo, objects, scratch, "merge-tree", "--write-tree", "-z",
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


# --------------------------------------------------------------------------
# un passage
# --------------------------------------------------------------------------

class Cache:
    """Résultats déjà calculés, par commits : une paire n'est refaite que si
    l'une de ses pointes a bougé."""

    def __init__(self) -> None:
        self.merges: dict = {}
        self.diffs: dict = {}
        # L160d: change sizes for the resolver, replays of prepared resolutions
        self.sizes: dict = {}
        self.replays: dict = {}

    def trim(self) -> None:
        for table in (self.merges, self.diffs, self.sizes, self.replays):
            while len(table) > MAX_CACHE:
                table.pop(next(iter(table)))


def scan(repo: str, lots: list[dict], *, default_target: str | None = None,
         migrations: str = "", cache: Cache | None = None) -> dict:
    """Le radar sur un dépôt. `lots` : `{id, branch, branch_target?, title?,
    assignee?}`. Rend le rapport (sans horodatage ni durée)."""
    cache = cache if cache is not None else Cache()
    objects = plan_git._out(repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "objects")
    if not objects:
        raise GitError("%s n'est pas un dépôt git lisible" % repo)
    if default_target is None:
        default_target = plan_git.default_target(repo)
    branches: list[dict] = []
    skipped: list[dict] = []
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
        head = plan_git._commit(repo, target) if target else None
        if head is None:
            skipped.append(dict(entry, reason="cible %s introuvable" % (target or "—")))
            continue
        base = plan_git._out(repo, "merge-base", tip, head)
        if not base:
            skipped.append(dict(entry, reason="aucune base commune avec %s" % target))
            continue
        if base == tip:
            continue                      # aucun travail propre : rien à fusionner
        key = (base, tip)
        if key not in cache.diffs:
            cache.diffs[key] = _changed(repo, base, tip)
        touched, added = cache.diffs[key]
        branches.append(dict(entry, target=target, target_head=head, tip=tip, base=base,
                             files=touched, added=added))

    pairs: list[dict] = []
    against: list[dict] = []
    checked = 0
    scratch = tempfile.mkdtemp(prefix="ameesh-radar-")
    try:
        def merge(a: str, b: str):
            key = (a, b) if a < b else (b, a)
            if key not in cache.merges:
                cache.merges[key] = trial_merge(repo, objects, scratch, a, b)
            return cache.merges[key]

        for one in branches:
            conflicts = merge(one["target_head"], one["tip"])
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
                conflicts = merge(one["tip"], other["tip"])
                pairs.append({"lots": [one["work_item"], other["work_item"]],
                              "branches": [one["branch"], other["branch"]],
                              "tips": [one["tip"], other["tip"]], "target": one["target"],
                              "state": "error" if conflicts is None
                              else "conflict" if conflicts else "overlap",
                              "shared": shared, "files": conflicts or []})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    cache.trim()
    return {"repo": repo, "branches": [{k: v for k, v in b.items() if k != "added"}
                                       for b in branches],
            "skipped": skipped, "pairs_checked": checked, "pairs": pairs,
            "against_target": against,
            "migrations": migration_probe(repo, branches, migrations) if migrations else []}


def migration_probe(repo: str, branches: list[dict], directory: str) -> list[dict]:
    """Numéros de migration en double : ajoutés par deux lots de même cible,
    ou par un lot alors que sa cible a déjà ce numéro sous un autre nom."""
    by_target: dict = {}
    in_target: dict = {}
    for one in branches:
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
            "migration_duplicates": len(report.get("migrations") or ())}


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
             now=time.time, ci="github") -> list[dict]:
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
            report = scan(project.repo, lots, default_target=project.target or None,
                          migrations=project.migrations, cache=cache)
            error = None
        except (GitError, plan_git.MergeProbeError, OSError) as exc:
            report, error = {"repo": project.repo}, "%s : %s" % (type(exc).__name__, exc)
        if not error:
            annotate_resolutions(cfg, db, project, report, lots, cache)
        report.update(schema=SCHEMA, project=project.name, ts=started,
                      duration_s=round(now() - started, 3), error=error)
        line = write_journal(cfg, project.name, report)
        sent = [] if error else send_mail(cfg, db, project, report, ci=ci)
        lines.append(dict(line, project=project.name, mail=sent))
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
                             cache=cache)
    except Exception as exc:  # noqa: BLE001 - never fatal for the pass
        report["resolution_error"] = "%s: %s" % (type(exc).__name__, exc)


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


def github_ci(project: Project, gh=None):
    """La fonction `branche -> {number, url, green} | None` du projet (PR
    ouverte de la branche), lue avec `gh` ; None si le dépôt GitHub est inconnu.
    Une erreur de `gh` vaut « pas verte » : le courrier reste passif."""
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
                                          "number,url,statusCheckRollup"]) or []
        except plan_github.GithubError:
            return None
        if not rows:
            return None
        return {"number": rows[0].get("number"), "url": rows[0].get("url"),
                "green": ci_green(rows[0].get("statusCheckRollup"))}
    return status


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
    return out


def _files(files) -> str:
    files = list(files or ())
    return ", ".join(files[:6]) + (" …" if len(files) > 6 else "") if files else "—"


def _who(branches: dict, lot: int) -> str:
    one = branches.get(lot) or {}
    return "#%d « %s » (%s, %s)" % (lot, one.get("title") or "", one.get("branch") or "?",
                                    one.get("assignee") or "non assigné")


def message_text(conflict: dict, lot: int, branches: dict, *, ci=None,
                 merged=()) -> str:
    """Le courrier du radar pour l'assigné de `lot`."""
    mine = branches.get(lot) or {}
    head = "Radar de conflits, lot #%d « %s »" % (lot, mine.get("title") or "")
    if ci is not None:
        return ("%s : ta PR #%s est verte mais ne peut pas fusionner, ta branche %s est "
                "en conflit avec %s sur %s%s. Intègre %s dans ta branche et résous le "
                "conflit." % (head, ci.get("number"), mine.get("branch") or "?",
                              conflict["target"], _files(conflict["files"]),
                              " (%s y est entré)" % ", ".join("#%d" % n for n in merged)
                              if merged else "", conflict["target"]))
    if conflict["kind"] == "pair":
        other = [n for n in conflict["lots"] if n != lot][0]
        resolver = conflict.get("resolver") or {}
        if resolver.get("lot") == lot:
            todo = ("C'est à toi de préparer la résolution (%s), au fil du travail : "
                    "`ameesh work resolve %d --with %d` ; ta branche ne change pas."
                    % (RESOLVER_REASONS.get(resolver.get("reason"), resolver.get("reason")),
                       lot, other))
        elif resolver.get("lot") == other:
            todo = ("Le lot #%d prépare la résolution (%s) : rien à faire pour toi."
                    % (other, RESOLVER_REASONS.get(resolver.get("reason"),
                                                   resolver.get("reason"))))
        else:
            todo = ("Le second des deux à entrer dans %s devra résoudre."
                    % conflict["target"])
        return ("%s : ta branche et celle du lot %s se fusionnent en conflit sur %s. Rien "
                "ne bloque encore. %s Tu seras réveillé si l'autre lot entre dans %s avant "
                "toi et que ta PR est verte."
                % (head, _who(branches, other), _files(conflict["files"]), todo,
                   conflict["target"]))
    if conflict["kind"] == "stale":
        other = [n for n in conflict["pair"] if n != lot][0]
        return ("%s : ta résolution préparée avec le lot %s (%s) ne couvre plus le conflit, "
                "qui a changé sur %s. Refais-la : `ameesh work resolve %d --with %d` (les "
                "blocs inchangés reviennent résolus)."
                % (head, _who(branches, other), conflict["commit"][:12],
                   _files(conflict["files"]), lot, other))
    if conflict["kind"] == "target":
        return ("%s : ta branche %s ne fusionne plus proprement dans %s (conflit sur %s). "
                "Intègre %s dans ta branche avant ta demande de fusion ; tu seras réveillé "
                "si ta PR est verte et toujours bloquée."
                % (head, mine.get("branch") or "?", conflict["target"],
                   _files(conflict["files"]), conflict["target"]))
    others = [_who(branches, n) for n in conflict["lots"] if n != lot]
    return ("%s : le numéro de migration %s est en double (%s)%s%s. Prends le premier "
            "numéro libre de %s et signale-le à l'autre lot."
            % (head, conflict["number"], _files(conflict["files"]),
               " ; aussi pris par le lot %s" % ", ".join(others) if others else "",
               " ; déjà dans %s : %s" % (conflict["target"], conflict["in_target"])
               if conflict.get("in_target") else "", conflict["target"]))


#: why a lot is the resolver of a pair (L160d), for the mail
RESOLVER_REASONS = {
    "priority": "l'autre lot est plus prioritaire et fusionnera d'abord",
    "state": "l'autre lot est plus avancé et fusionnera d'abord",
    "smaller change": "ton changement est le plus petit dans les fichiers en conflit",
    "newer lot": "ton lot est le plus récent",
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
    out = ["%s : passage du %s UTC (%.1f s), %d branche(s), %d paire(s) de même cible, "
           "%d qui se recouvrent, %d en conflit (%d préparée(s), %d périmée(s)), "
           "%d lot(s) en conflit avec leur cible, %d numéro(s) de migration en double"
           % (report.get("project"), when, report.get("duration_s") or 0, s["branches"],
              s["pairs_checked"], s["overlap"] + s["conflict"] + s["prepared"] + s["stale"],
              s["conflict"] + s["prepared"] + s["stale"], s["prepared"], s["stale"],
              s["target_conflict"], s["migration_duplicates"])]
    if report.get("error"):
        out.append("  erreur : %s" % report["error"])
    if report.get("resolution_error"):
        out.append("  résolutions : %s" % report["resolution_error"])
    for pair in report.get("pairs") or ():
        if pair["state"] == "overlap" and not everything:
            continue
        files = pair["shared"] if pair["state"] == "overlap" else pair["files"]
        resolver = pair.get("resolver") or {}
        resolution = pair.get("resolution") or {}
        out.append("  %-8s %s %s  %s%s%s" % (
            pair["state"], _lot(pair["lots"][0]), _lot(pair["lots"][1]),
            ", ".join(files[:6]) + (" …" if len(files) > 6 else ""),
            "  [résolveur %s : %s]" % (_lot(resolver["lot"]), resolver["reason"])
            if resolver else "",
            "  [résolution %s par %s%s]" % (
                resolution["commit"][:12], _lot(resolution["by"]),
                ", reste : %s" % ", ".join(resolution["remaining"][:6])
                if resolution.get("remaining") else
                ", rejeu %s" % resolution["replay"]
                if resolution.get("replay") not in ("prepared", "stale") else "")
            if resolution else ""))
    for one in report.get("against_target") or ():
        if one["state"] == "clean" and not everything:
            continue
        out.append("  %-8s %s contre %s  %s" % (one["state"], _lot(one["work_item"]),
                                                one["target"], ", ".join(one["files"][:6])))
    for dup in report.get("migrations") or ():
        out.append("  migration %s : %s%s" % (
            dup["number"], ", ".join("%s (%s)" % (_lot(h["work_item"]), h["path"])
                                     for h in dup["lots"]),
            " ; déjà dans %s : %s" % (dup["target"], dup["in_target"])
            if dup["in_target"] else ""))
    for one in report.get("skipped") or ():
        out.append("  ignoré   %s %s : %s" % (_lot(one["work_item"]), one["branch"],
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
        print("aucun passage du radar sur cet hôte (%s) : l'hôte désigné déclare "
              "`radar.projects` dans sa configuration%s"
              % (directory, ", projet %s" % args.project if args.project else ""))
        return 0
    for report in reports:
        print("\n".join(render(report, everything=args.all)))
    return 0
