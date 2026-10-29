# SPDX-License-Identifier: AGPL-3.0-only
"""#4173 (manaty/ameesh#3185, volet 3) : l'empreinte d'un lot et
l'ordonnancement sans chevauchement dans un dépôt.

L'empreinte d'un lot est la liste des fichiers ou modules (dossiers) qu'il
touche, relatifs à la racine de son dépôt (`app`). Elle a trois sources,
dont elle est l'union (`work_item_footprints`, 0502) :

* `intake` : estimée à la création (`ameesh work add`) depuis les chemins
  cités par le titre et le corps (`intake_neighbors.cited_paths`), résolus
  dans un clone du dépôt quand il y en a un ;
* `manual` : posée par `ameesh work footprint <lot> --set …`, elle remplace
  l'estimation (une estimation fausse se corrige, avec sa ligne au journal) ;
* `diff` : les fichiers réellement changés par la branche du lot, depuis sa
  base — rafraîchie au gel et par `ameesh work footprint <lot> --refresh`.

Un lot OCCUPE son empreinte dès qu'il est confié ou démarré, jusqu'à sa
fusion ou sa fermeture. L'ordonnanceur ne confie jamais un lot `intake` dont
l'empreinte chevauche celle d'un lot occupant du même dépôt : la prise au
repos (L165) ne le voit pas (`unassigned_lots`) et l'attribution le refuse
sous le verrou du graphe des dépendances (`_assign_locked`). Il attend la
fusion de l'autre et part donc de la base à jour. Une empreinte trop large
coûte une attente, jamais un conflit ; un lot sans empreinte connue n'est
pas retenu (rien ne dit qu'il chevauche).
"""
from __future__ import annotations

import json
import posixpath
import sys
from typing import Iterable, Optional

from . import storage, work

SOURCES = ("intake", "manual", "diff")
ESTIMATE_SOURCES = ("intake", "manual")


def normalize(path: str) -> Optional[str]:
    """Le chemin relatif à la racine, sans `./`, ni `/` en tête ou en queue ;
    None s'il est vide, absolu (racine, `~`, lettre de lecteur) ou sort du
    dépôt (`..`). Un chemin absolu n'est jamais rabattu en relatif : il
    désignerait un fichier hors du dépôt sous un nom qui existe peut-être."""
    value = str(path or "").strip().replace("\\", "/")
    if (not value or value == "~" or value.startswith("~/")
            or value.startswith("/")
            or (len(value) >= 2 and value[0].isalpha() and value[1] == ":")):
        return None
    while value.startswith("./"):
        value = value[2:]
    value = value.strip("/")
    if not value:
        return None
    value = posixpath.normpath(value)
    if value in (".", "") or value == ".." or value.startswith("../"):
        return None
    return value


def overlaps(left: str, right: str) -> bool:
    """Deux chemins normalisés se chevauchent : égaux, ou l'un est un dossier
    parent de l'autre (`src/a` et `src/a/b.py`, pas `src/a` et `src/ab.py`).
    Même règle que le SQL de la garde (`storage.postgres.work_footprints`)."""
    return (left == right or right.startswith(left + "/")
            or left.startswith(right + "/"))


def overlapping_pairs(mine: Iterable[str], theirs: Iterable[str]) -> list[tuple[str, str]]:
    """Les paires (chemin de `mine`, chemin de `theirs`) qui se chevauchent."""
    theirs = list(theirs)
    return [(a, b) for a in mine for b in theirs if overlaps(a, b)]


def _clean(paths: Iterable[str]) -> list[str]:
    return sorted({p for p in (normalize(path) for path in paths) if p})


# --------------------------------------------------------------------------
# estimation et diff
# --------------------------------------------------------------------------

def estimate(text: str, repo: Optional[str] = None, target: str = "") -> list[str]:
    """Les chemins cités par `text`, résolus dans `repo` à `target` quand un
    clone est connu (un nom de fichier seul devient son chemin s'il est
    unique). Un chemin cité absent du dépôt est gardé : c'est souvent le
    fichier à créer."""
    from . import intake_neighbors
    trees: dict = {}
    out = []
    for cited in intake_neighbors.cited_paths(text):
        path = cited["git_path"]
        if repo and target:
            path = intake_neighbors._resolve_git_path(repo, target, path, trees) or path
        out.append(path)
    return _clean(out)


def _git(repo: str, *args: str):
    from . import intake_neighbors
    return intake_neighbors._git(repo, *args)


def _ref(repo: str, name: str) -> str:
    """`origin/<name>` s'il existe dans le clone, sinon `name`, sinon ""."""
    for ref in ("origin/" + name, name):
        proc = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if proc.returncode == 0:
            return ref
    return ""


def diff_paths(repo: str, target: str, head: str) -> Optional[list[str]]:
    """Les fichiers changés par `head` depuis sa base sur `target`
    (`git diff --name-only target...head`), ou None si illisible."""
    base = _ref(repo, target) if target else ""
    tip = _ref(repo, head) if head else ""
    if not base or not tip:
        return None
    proc = _git(repo, "diff", "--name-only", "--no-renames", base + "..." + tip)
    if proc.returncode != 0:
        return None
    return _clean(proc.stdout.decode("utf-8", "replace").splitlines())


def _fresh_base(repo: str, target: str) -> tuple[str, bool]:
    """La base du diff et la preuve de sa fraîcheur (#4733).

    La ref `origin/<cible>` d'un clone partagé n'est fraîche que de son
    dernier fetch. La pointe **publiée** de la cible est donc lue à la forge
    par SHA exact (`review_authors.refreshed_target`, lecture seule, aucun ref
    déplacé — la lecture de #4673), puis l'objet exact est rapatrié. Une forge
    muette rend la ref locale, qui n'est alors **pas** prouvée fraîche :
    l'appelant le dit au lieu de la faire passer pour la cible du jour."""
    if not target:
        return "", False
    from . import plan_git
    if plan_git.full_commit_id(target):
        return target, True
    try:
        from . import review_authors
        fresh = review_authors.refreshed_target(repo, target)
    except Exception:  # noqa: BLE001 - fraîcheur au mieux, le doute se dit
        fresh = ""
    if fresh:
        return fresh, True
    return _ref(repo, target), False


def _clone_and_target(cfg, item: dict, repo: Optional[str] = None) -> tuple:
    from . import intake_neighbors
    clone = intake_neighbors.resolve_clone(cfg, item.get("app") or "", repo)
    if not clone:
        return None, ""
    target = (item.get("branch_target") or "").strip() \
        or intake_neighbors.project_target(cfg, item.get("app") or "") \
        or intake_neighbors.default_target(clone)
    if target.startswith("origin/"):
        target = target[len("origin/"):]
    return clone, target


# --------------------------------------------------------------------------
# gestes
# --------------------------------------------------------------------------

def paths_of(db, item_id: int) -> list[str]:
    return sorted({r["path"] for r in storage.of(db).work_footprints.for_items([item_id])})


def set_paths(db, item_id: int, paths: Iterable[str], *, actor: str) -> dict:
    """`--set` : l'empreinte estimée devient `paths` (source `manual`, qui
    remplace l'estimation d'intake) ; les lignes du diff restent. Un chemin
    absolu (ou vide) est refusé nommément, jamais rabattu en relatif."""
    raw = [str(path) for path in paths]
    invalid = [path for path in raw if path.strip() and normalize(path) is None]
    if invalid:
        raise work.WorkError(
            "invalid path(s), relative to the repository root: %s"
            % ", ".join(invalid))
    clean = _clean(raw)
    if not clean and raw:
        raise work.WorkError("no usable path: paths are relative to the repository root")
    result = storage.of(db).work_footprints.replace(
        item_id, "manual", clean, actor=actor, replaces=ESTIMATE_SOURCES)
    if result is None:
        raise work.WorkError("lot %s not found" % item_id)
    return result


def estimate_lot(cfg, item: dict, repo: Optional[str] = None) -> list[str]:
    """L'estimation d'intake d'un lot (créé ou à créer : `app`, `title`,
    `body`, `branch_target`), résolue dans un clone du dépôt s'il y en a un."""
    clone, target = _clone_and_target(cfg, item, repo)
    return estimate("%s\n%s" % (item.get("title") or "", item.get("body") or ""),
                    clone, _ref(clone, target) if clone and target else "")


def estimate_for_creation(cfg, *, app: str = "", title: str = "", body: str = "",
                          branch_target: str = "", repo: Optional[str] = None) -> list[str]:
    """L'estimation d'intake d'un lot à créer — le geste commun de tous les
    chemins de création (`work.add`, file d'amélioration). Un texte qui ne cite
    aucun chemin ne paie pas la résolution du clone ; un échec de la recherche
    git laisse l'empreinte inconnue et ne défait jamais la création."""
    if cfg is None:
        return []
    from . import intake_neighbors
    text = "%s\n%s" % (title or "", body or "")
    if not intake_neighbors.cited_paths(text):
        return []
    try:
        return estimate_lot(cfg, {"app": app or "", "title": title or "",
                                  "body": body or "",
                                  "branch_target": branch_target or ""}, repo)
    except Exception:  # noqa: BLE001 - la création n'attend pas l'estimation
        return []


def record_estimate(db, cfg, item: dict, *, actor: str, repo: Optional[str] = None) -> dict | None:
    """`--estimate` : l'estimation d'intake du lot, réécrite si elle trouve un
    chemin. Le manuel remplace l'estimation : s'il existe, `--estimate` ne
    ré-ajoute pas de lignes `intake` (l'union serait plus large que les deux) ;
    `--clear` puis `--estimate` réestime."""
    rows = storage.of(db).work_footprints.for_items([int(item["id"])])
    if any(str(row.get("source")) == "manual" for row in rows):
        return None
    paths = estimate_lot(cfg, item, repo)
    if not paths:
        return None
    return storage.of(db).work_footprints.replace(
        int(item["id"]), "intake", paths, actor=actor)


def refresh(db, cfg, item_id: int, *, actor: str, head: str = "",
            repo: Optional[str] = None) -> dict:
    """Le diff réel de la branche du lot (ou de `head`, la tête gelée)
    remplace les lignes `diff` de l'empreinte.

    La base est la cible du lot (`branch_target`), sinon celle que le projet
    déclare (`develop` pour Nexlink), sinon le défaut du dépôt (#4733) ; sa
    pointe publiée est relue à la forge quand elle répond, et `fresh` dit si
    la base est prouvée fraîche ou seulement la ref du dernier fetch."""
    item = work.get(db, item_id)
    if item is None:
        raise work.WorkError("lot %s not found" % item_id)
    tip = (head or "").strip() or (item.get("branch") or "").strip()
    if not tip:
        raise work.WorkError("lot %s has no branch: nothing to diff" % item_id)
    clone, target = _clone_and_target(cfg, item, repo)
    if not clone or not target:
        raise work.WorkError("lot %s: no local clone of %s with a known target"
                             % (item_id, item.get("app") or "its repository"))
    base, fresh = _fresh_base(clone, target)
    paths = diff_paths(clone, base, tip)
    if paths is None:
        raise work.WorkError("lot %s: %s...%s unreadable in %s (fetch first)"
                             % (item_id, target, tip, clone))
    result = storage.of(db).work_footprints.replace(
        item_id, "diff", paths, actor=actor, head=tip if head else None)
    result.update({"target": target, "base": base, "fresh": fresh})
    return result


def refusal(item_id: int, rows: list) -> str:
    """Le message du refus d'attribution (`FootprintOverlap`)."""
    by_lot: dict = {}
    for row in rows:
        by_lot.setdefault(int(row["id"]), row)
    parts = ["#%d (%s%s: %s ~ %s)" % (
        lot, row.get("state") or "?",
        ", %s" % row["assignee"] if row.get("assignee") else "",
        row.get("path"), row.get("other_path")) for lot, row in sorted(by_lot.items())]
    return ("lot #%s not assigned: its footprint overlaps lot(s) in progress in the same "
            "repository: %s. It waits for their merge and starts on the updated base "
            "(#3185); a wrong footprint is corrected with `ameesh work footprint %s "
            "--set PATH…`" % (item_id, "; ".join(parts), item_id))


def describe(db, item_id: int) -> dict:
    rows = storage.of(db).work_footprints.for_items([item_id])
    return {"id": int(item_id),
            "paths": sorted({r["path"] for r in rows}),
            "rows": rows,
            "overlapping": storage.of(db).work_footprints.overlapping(item_id)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_parser(sub, func):
    p = sub.add_parser(
        "footprint",
        help="show or set a lot's footprint (files/modules it touches, #3185): the "
             "scheduler never gives a lot whose footprint overlaps a lot in progress "
             "in the same repository")
    p.add_argument("id", type=int)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--set", nargs="+", metavar="PATH", dest="paths",
                      help="replace the estimated footprint (files or directories, "
                           "relative to the repository root)")
    mode.add_argument("--clear", action="store_true",
                      help="remove the estimated footprint (the diff rows stay)")
    mode.add_argument("--clear-diff", action="store_true", dest="clear_diff",
                      help="remove the diff rows (e.g. computed on a wrong base); "
                           "pilot gesture, --refresh recomputes them")
    mode.add_argument("--estimate", action="store_true",
                      help="re-estimate from the paths cited by the title and body")
    mode.add_argument("--refresh", action="store_true",
                      help="replace the diff rows from the branch's actual diff against "
                           "its target (branch target, else the project's declared "
                           "target, else the repository default)")
    p.add_argument("--repo", help="local clone of the lot's repository")
    p.add_argument("--json", action="store_true")
    p.add_argument("--actor", default="")
    p.set_defaults(func=func)
    return p


def run(cfg, db, args) -> int:
    actor = args.actor or "ameesh"
    if work.get(db, args.id) is None:
        raise work.WorkError("lot %s not found" % args.id)
    if (args.paths or args.clear or args.clear_diff) and not work.is_pilot(cfg, db, actor):
        # une empreinte posée à la main peut lever une attente : geste de
        # pilote, comme l'exception §4 (`--estimate`/`--refresh` sont
        # déterministes et restent ouverts)
        raise work.WorkError("footprint --set/--clear/--clear-diff refused: %s is neither "
                             "a human nor an orchestrator/design agent (pilot gesture)"
                             % (actor or "nobody"))
    refreshed = None
    if args.paths:
        set_paths(db, args.id, args.paths, actor=actor)
    elif args.clear:
        storage.of(db).work_footprints.replace(args.id, "manual", [], actor=actor,
                                               replaces=ESTIMATE_SOURCES)
    elif args.clear_diff:
        storage.of(db).work_footprints.replace(args.id, "diff", [], actor=actor)
    elif args.estimate:
        item = work.get(db, args.id)
        manual = any(row["source"] == "manual"
                     for row in storage.of(db).work_footprints.for_items([args.id]))
        if manual:
            # le manuel remplace l'estimation : --estimate ne ré-ajoute pas
            # des lignes `intake` sous une correction de pilote.
            print("lot #%d: manual footprint set (--set), estimate not written — "
                  "change it with --set PATH…, or --clear then --estimate" % args.id)
        elif record_estimate(db, cfg, item, actor=actor, repo=args.repo) is None:
            print("lot #%d: no path cited by its title or body" % args.id)
    elif args.refresh:
        refreshed = refresh(db, cfg, args.id, actor=actor, repo=args.repo)
    found = describe(db, args.id)
    if refreshed is not None:
        found["refresh"] = {key: refreshed.get(key) for key in ("target", "base", "fresh")}
        if not refreshed.get("fresh"):
            print("warning: lot #%d: base %s of %s was not proven fresh (the clone's "
                  "last fetch only): fetch it, or rerun when the forge answers"
                  % (args.id, refreshed.get("base") or "?",
                     refreshed.get("target") or "?"), file=sys.stderr)
        elif not args.json:
            print("lot #%d: diff base %s @%s (fresh)" % (
                args.id, refreshed["target"], refreshed["base"][:12]))
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2, default=str))
        return 0
    if not found["paths"]:
        print("lot #%d: no known footprint" % args.id)
        return 0
    sources: dict = {}
    for row in found["rows"]:
        sources.setdefault(row["path"], []).append(row["source"])
    print("lot #%d footprint:" % args.id)
    for path in found["paths"]:
        print("  %s  (%s)" % (path, ", ".join(sources.get(path, []))))
    seen = set()
    for row in found["overlapping"]:
        if int(row["id"]) in seen:
            continue
        seen.add(int(row["id"]))
        print("overlaps #%d (%s%s): %s ~ %s" % (
            int(row["id"]), row["state"],
            ", %s" % row["assignee"] if row.get("assignee") else "",
            row["path"], row["other_path"]))
    return 0
