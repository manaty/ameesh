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

#: #4818 : le marqueur d'empreinte inconnue — le dépôt entier, qui chevauche
#: tout chemin du même dépôt (`overlaps`, et le SQL de la garde).
WHOLE_REPOSITORY = "."
UNKNOWN_POLICIES = ("repository", "open")
#: sans politique posée, une empreinte inconnue n'occupe rien (règle d'avant
#: #4818) : la politique prudente se pose par dépôt (`footprint-policy`)
DEFAULT_UNKNOWN_POLICY = "open"
#: une analyse inconnue est retentée par l'exécuteur après ce délai
RETRY_AFTER_S = 1800
#: au plus tant de lots par passe de l'exécuteur, par geste (analyse, diff)
PASS_LIMIT = 5
#: au plus tant de candidats au diff inspectés par passe (curseur, B2 de cx1)
PASS_SCAN = 100


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
            or left.startswith(right + "/")
            or WHOLE_REPOSITORY in (left, right))


def is_exempt(row: dict) -> bool:
    """#4818 : un lot qui n'est pas du code — véhicule de paquet
    (`integration/**`), question au pilote (`decision`), lot de découpage
    (`file-decoupage:`) — n'entre ni dans le plafond de lots en vol ni dans
    le marqueur d'empreinte inconnue. Un lot de la file d'amélioration est du
    code : il suit la règle commune. Même règle que le SQL de la garde
    (`_EXEMPT`)."""
    from . import packet_guard
    source = str(row.get("source") or "")
    return (packet_guard.is_packet_branch(row.get("branch") or "")
            or source == "decision" or source.startswith("file-decoupage:"))


def is_shared(path: str, shared: Iterable[str]) -> bool:
    """#4818 : `path` est un chemin partagé du dépôt (fusion triviale) ou sous
    un dossier partagé — ignoré pour le chevauchement. Le marqueur `.` ne
    l'est jamais. Même règle que le SQL de la garde (`_SHARED`)."""
    return any(path == item or path.startswith(item + "/") for item in shared or ())


def held_pairs(mine: Iterable[str], theirs: Iterable[str],
               shared: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Les paires qui retiennent un lot : chevauchantes, aucun des deux
    chemins partagé."""
    shared = list(shared or ())
    return [(a, b) for a, b in overlapping_pairs(mine, theirs)
            if not is_shared(a, shared) and not is_shared(b, shared)]


def overlapping_pairs(mine: Iterable[str], theirs: Iterable[str]) -> list[tuple[str, str]]:
    """Les paires (chemin de `mine`, chemin de `theirs`) qui se chevauchent."""
    theirs = list(theirs)
    return [(a, b) for a in mine for b in theirs if overlaps(a, b)]


def _clean(paths: Iterable[str]) -> list[str]:
    return sorted({p for p in (normalize(path) for path in paths) if p})


# --------------------------------------------------------------------------
# politique du dépôt (#4818)
# --------------------------------------------------------------------------

def default_policy(app: str) -> dict:
    return {"app": app, "unknown_policy": DEFAULT_UNKNOWN_POLICY,
            "max_in_flight": None, "shared_paths": [], "updated_by": None,
            "default": True}


def policy_of(db, app: str) -> dict:
    """La politique d'empreinte du dépôt `app`, ou le défaut sans ligne :
    empreinte inconnue ouverte (`open`), aucun plafond, aucun chemin partagé. Une base sans la table (avant 0519) rend le défaut."""
    app = str(app or "").strip()
    try:
        found = storage.of(db).work_footprints.policies([app]).get(app)
    except Exception:  # noqa: BLE001 - la politique se lit au mieux
        found = None
    return dict(found, default=False) if found else default_policy(app)


def policies_of(db, apps: Iterable[str]) -> dict:
    apps = sorted({str(a or "").strip() for a in apps if str(a or "").strip()})
    try:
        found = storage.of(db).work_footprints.policies(apps)
    except Exception:  # noqa: BLE001
        found = {}
    return {app: (dict(found[app], default=False) if app in found else default_policy(app))
            for app in apps}


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
    # #4818 : `.` (le dépôt entier) est un geste de pilote explicite
    whole = any(path.strip() in (WHOLE_REPOSITORY, "./") for path in raw)
    raw = [path for path in raw if path.strip() not in (WHOLE_REPOSITORY, "./")]
    invalid = [path for path in raw if path.strip() and normalize(path) is None]
    if invalid:
        raise work.WorkError(
            "invalid path(s), relative to the repository root: %s"
            % ", ".join(invalid))
    clean = _clean(raw)
    if not clean and raw and not whole:
        raise work.WorkError("no usable path: paths are relative to the repository root")
    if whole:
        clean = [WHOLE_REPOSITORY]
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


def _clone_ref(cfg, item: dict, repo: Optional[str] = None) -> tuple:
    """Le clone du dépôt du lot et la ref de sa cible, ou `(None, "")`."""
    if cfg is None:
        return None, ""
    clone, target = _clone_and_target(cfg, item, repo)
    return clone, (_ref(clone, target) if clone and target else "")


def analyze_lot(cfg, db, item: dict, repo: Optional[str] = None, **budget) -> dict:
    """#4818 : l'analyse d'impact d'un lot (créé ou à créer : `app`, `title`,
    `body`, `branch_target`), en lecture seule et bornée
    (`impact_analysis.analyze`). Rend l'analyse avec `footprint` (sa surface
    connue, vide si inconnue), `unknown`, `policy` et `note`, la ligne de
    journal. La politique lue ici n'est qu'indicative : le marqueur `.` d'une
    analyse inconnue est décidé là où elle s'écrit, sous le verrou de la
    garde, sur la politique du moment (`settle`, B3 de cx1). Ne lève jamais :
    un échec est une analyse inconnue."""
    from . import impact_analysis
    text = "%s\n%s" % (item.get("title") or "", item.get("body") or "")
    try:
        clone, ref = _clone_ref(cfg, item, repo)
    except Exception:  # noqa: BLE001 - sans clone, l'analyse reste textuelle
        clone, ref = None, ""
    try:
        analysis = impact_analysis.analyze(text, repo=clone, ref=ref,
                                           title=item.get("title") or "", **budget)
    except Exception as exc:  # noqa: BLE001 - jamais d'échec de l'appelant
        analysis = {"status": "unknown", "reason": "analysis failed: %s" % exc,
                    "paths": [], "weak": [], "elapsed_ms": 0, "git_calls": 0}
    policy = (policy_of(db, item.get("app") or "")["unknown_policy"]
              if db is not None else DEFAULT_UNKNOWN_POLICY)
    analysis["policy"] = policy
    analysis["unknown"] = analysis["status"] != "known"
    analysis["footprint"] = sorted({p["path"] for p in analysis["paths"]})
    analysis["note"] = impact_analysis.note(analysis)
    return analysis


def settle(analysis: dict | None, policy: str | None) -> None:
    """L'analyse prend la politique APPLIQUÉE à sa persistance (lue sous le
    verrou de la garde par le stockage) : le marqueur `.` d'une inconnue sous
    `repository`, et sa ligne de journal recalculée."""
    from . import impact_analysis
    if not analysis or not policy:
        return
    analysis["policy"] = policy
    if analysis.get("unknown"):
        analysis["footprint"] = [WHOLE_REPOSITORY] if policy == "repository" else []
    analysis["note"] = impact_analysis.note(analysis)


def analyze_for_creation(cfg, db, *, app: str = "", title: str = "", body: str = "",
                         branch_target: str = "", repo: Optional[str] = None) -> dict | None:
    """L'analyse d'impact d'un lot à créer — le geste commun de tous les
    chemins de création (`work.add`, file d'amélioration, CLI). None sans
    projet (`app`) — un lot hors dépôt n'a pas d'empreinte — ou sans `cfg`
    (#4186 : aucun projet connu, rien n'est deviné ni journalisé)."""
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    if not str(app or "").strip() or cfg is None:
        return None
    return analyze_lot(cfg, db,
                       {"app": app or "", "title": title or "", "body": body or "",
                        "branch_target": branch_target or ""}, repo)


def estimate_for_creation(cfg, *, app: str = "", title: str = "", body: str = "",
                          branch_target: str = "", repo: Optional[str] = None,
                          db=None) -> list[str]:
    """L'empreinte d'intake d'un lot à créer (`analyze_for_creation`), sans
    l'analyse ; vide sans projet. Ne défait jamais la création."""
    analysis = analyze_for_creation(cfg, db, app=app, title=title, body=body,
                                    branch_target=branch_target, repo=repo)
    return list(analysis["footprint"]) if analysis else []


def record_analysis(db, item_id: int, analysis: dict | None, *, actor: str) -> None:
    """La dernière analyse du lot et sa ligne de journal ; jamais d'échec de
    l'appelant (une base sans 0519 n'enregistre rien)."""
    if not analysis:
        return
    try:
        storage.of(db).work_footprints.record_analysis(int(item_id), analysis,
                                                       actor=actor or "ameesh")
    except Exception as exc:  # noqa: BLE001
        print("warning: impact analysis of lot #%s not recorded: %s" % (item_id, exc),
              file=sys.stderr)


def _basket_held_unknown(db, item: dict) -> bool:
    """Le lot est un panier dont l'analyse est inconnue."""
    from . import backlog
    if not backlog.is_basket(item):
        return False
    known = storage.of(db).work_footprints.analyses([int(item["id"])])
    return (known.get(int(item["id"])) or {}).get("status") == "unknown"


def record_estimate(db, cfg, item: dict, *, actor: str, repo: Optional[str] = None) -> dict | None:
    """`--estimate` : l'analyse d'impact du lot, réécrite (lignes `intake`)
    et journalisée. Le manuel remplace l'estimation : s'il existe, rien n'est
    écrit (l'union serait plus large que les deux) ; `--clear` puis
    `--estimate` réestime. Un panier dont une ligne est inconnue ne se
    réanalyse pas non plus : son corps unit ses lignes, une ligne connue y
    masquerait l'inconnue d'une autre (B de dev3 sur 055b48b). Rend l'analyse,
    ou None si rien n'est écrit."""
    rows = storage.of(db).work_footprints.for_items([int(item["id"])])
    if any(str(row.get("source")) == "manual" for row in rows):
        return None
    if _basket_held_unknown(db, item):
        return None
    analysis = analyze_lot(cfg, db, item, repo)
    # le calcul (git) se fait hors verrou ; la politique se relit à l'écriture
    written = storage.of(db).work_footprints.replace(
        int(item["id"]), "intake", analysis["footprint"], actor=actor,
        unknown=bool(analysis.get("unknown")), unless_manual=True)
    if (written or {}).get("skipped"):
        # B4 de cx1 : un `--set` est arrivé pendant le calcul ; il prime, la
        # réestimation caduque n'est ni écrite ni journalisée
        return None
    settle(analysis, (written or {}).get("policy"))
    record_analysis(db, int(item["id"]), analysis, actor=actor)
    return analysis


def refresh(db, cfg, item_id: int, *, actor: str, head: str = "",
            repo: Optional[str] = None) -> dict:
    """Le diff réel de la branche du lot (ou de `head`, la tête gelée)
    remplace les lignes `diff` de l'empreinte.

    La base est la cible du lot (`branch_target`), sinon celle que le projet
    déclare (`develop` pour Nexlink), sinon le défaut du dépôt (#4733) ; sa
    pointe publiée est relue à la forge quand elle répond, et `fresh` dit si
    la base est prouvée fraîche ou seulement la ref du dernier fetch. Un diff
    qui trouve des fichiers fait tomber le marqueur d'empreinte inconnue (`.`)
    de l'analyse (#4818)."""
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


def _path_label(path: str) -> str:
    return ("(unknown footprint: whole repository)" if path == WHOLE_REPOSITORY
            else str(path))


def refusal(item_id: int, rows: list, *, cap: int | None = None,
            in_flight: int | None = None) -> str:
    """Le message du refus d'attribution (`FootprintOverlap`), ou du plafond
    de lots en vol du dépôt (`RepositoryCapReached`, #4818)."""
    if cap is not None:
        return ("lot #%s not assigned: its repository reached its in-flight cap (%s/%s "
                "lots in progress, #4818). It waits for a merge; the cap is the "
                "repository's policy (`ameesh work footprint-policy`)"
                % (item_id, in_flight if in_flight is not None else "?", cap))
    by_lot: dict = {}
    for row in rows:
        by_lot.setdefault(int(row["id"]), row)
    parts = ["#%d (%s%s: %s ~ %s)" % (
        lot, row.get("state") or "?",
        ", %s" % row["assignee"] if row.get("assignee") else "",
        _path_label(row.get("path")), _path_label(row.get("other_path")))
        for lot, row in sorted(by_lot.items())]
    return ("lot #%s not assigned: its footprint overlaps lot(s) in progress in the same "
            "repository: %s. It waits for their merge and starts on the updated base "
            "(#3185); a wrong footprint is corrected with `ameesh work footprint %s "
            "--set PATH…`" % (item_id, "; ".join(parts), item_id))


# --------------------------------------------------------------------------
# passe de l'exécuteur (#4818) : analyses inconnues retentées, diff au push
# --------------------------------------------------------------------------

def _published_tip(clone: str, branch: str) -> str:
    """Le SHA de `origin/<branche>` dans le clone, ou "" si le clone ne la
    connaît pas : le repli de `_published_tips` quand la forge ne répond pas."""
    from . import plan_git
    name = plan_git.short_branch(branch) or branch
    proc = _git(clone, "rev-parse", "--verify", "--quiet",
                "refs/remotes/origin/%s^{commit}" % name)
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def _published_tips(clone: str, branches: Iterable[str]) -> dict:
    """`{branche: SHA}` des têtes publiées, lues à la forge en UN `ls-remote`
    borné : le clone de l'hôte n'est pas celui où l'agent a poussé, sa ref
    `origin/<branche>` n'en sait rien (B de cx2 sur 055b48b). Une branche
    absente de la réponse n'est pas publiée. Forge injoignable : les refs
    locales du clone, au mieux."""
    from . import plan_git, review_authors
    names = {branch: plan_git.short_branch(branch) or branch for branch in branches}
    try:
        listing = plan_git._git(
            clone, "ls-remote", "--refs", "origin",
            *sorted("refs/heads/%s" % name for name in set(names.values())),
            timeout=review_authors.TARGET_REFRESH_TIMEOUT)
        failed = listing.returncode != 0
    except Exception:  # noqa: BLE001 - forge coupée : lecture locale
        failed = True
    if failed:
        return {branch: _published_tip(clone, branch) for branch in names}
    heads = {}
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].startswith("refs/heads/"):
            heads[fields[1][len("refs/heads/"):]] = fields[0]
    return {branch: heads.get(name, "") for branch, name in names.items()}


def _ensure_commit(clone: str, sha: str) -> None:
    """L'objet publié dans le clone, demandé par SHA et sans écrire de ref
    (même geste que `review_authors._published_target`). Lève si la forge ne
    le donne pas : la passe suivante réessaie."""
    from . import plan_git, review_authors
    if plan_git.local_commit(clone, sha):
        return
    proc = plan_git._git(clone, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                         "--", "origin", sha,
                         timeout=review_authors.TARGET_REFRESH_TIMEOUT)
    if proc.returncode != 0 or not plan_git.local_commit(clone, sha):
        raise work.WorkError("published head %s not fetched in %s" % (sha[:12], clone))


def run_pass(cfg, db, *, actor: str, agents=None, limit: int = PASS_LIMIT,
             retry_after_s: float = RETRY_AFTER_S, cursor: int = 0,
             scan: int = PASS_SCAN) -> dict:
    """Une passe bornée, jamais fatale :

    * **refresh au premier push** : un lot occupant dont la branche est
      publiée (tête lue à la forge, un `ls-remote` par clone, `_published_tips`)
      et dont le diff n'a jamais été lu, ou l'a été sur une autre tête, voit
      ses lignes `diff` réécrites, l'objet étant d'abord demandé par SHA. Avec
      `agents`, seuls les lots de ces assignés (ceux de l'hôte, pour qu'un
      seul exécuteur s'en charge). La lecture avance par curseur
      (`cursor` → `out["cursor"]`) : au plus `scan` candidats par passe, le
      suivant reprend après le dernier inspecté et revient au début en fin de
      liste — un premier push n'attend jamais derrière des lots à jour (B2
      de cx1) ;
    * **analyse inconnue retentée** : au plus `limit` lots `intake` dont la
      dernière analyse est inconnue depuis `retry_after_s` sont réanalysés
      (prise exclusive : deux exécuteurs ne le font pas ensemble) ; une
      empreinte manuelle n'est jamais réécrite.

    Rend `{refreshed: [...], analyzed: [...], errors: [...], cursor}`."""
    out: dict = {"refreshed": [], "analyzed": [], "errors": [], "cursor": 0}
    store = storage.of(db).work_footprints
    try:
        candidates = store.refresh_candidates(
            limit=scan, after_id=cursor,
            assignees=sorted(agents) if agents is not None else None)
    except Exception as exc:  # noqa: BLE001 - base sans 0519 : rien à faire
        out["errors"].append("refresh candidates: %s" % exc)
        candidates = []
    # une page incomplète est la fin de la liste : la passe suivante reprend
    # au début ; sinon après le dernier candidat inspecté
    out["cursor"] = int(candidates[-1]["id"]) if len(candidates) >= scan else 0
    done = 0
    clones: dict = {}
    for row in candidates:
        try:
            clone = _clone_and_target(cfg, row)[0]
            if clone:
                clones.setdefault(clone, set()).add(row["branch"])
        except Exception as exc:  # noqa: BLE001 - un lot illisible n'arrête pas la passe
            out["errors"].append("lot #%s: refresh: %s" % (row.get("id"), exc))
    tips = {clone: _published_tips(clone, branches) for clone, branches in clones.items()}
    for row in candidates:
        if done >= limit:
            out["cursor"] = int(row["id"]) - 1
            break
        try:
            clone, _target = _clone_and_target(cfg, row)
            if not clone:
                continue
            tip = tips.get(clone, {}).get(row["branch"]) or ""
            if not tip or (row.get("has_diff") and (row.get("diff_head") or "") == tip):
                continue
            done += 1
            _ensure_commit(clone, tip)
            result = refresh(db, cfg, int(row["id"]), actor=actor, head=tip)
            out["refreshed"].append({"lot": int(row["id"]), "head": tip,
                                     "paths": result.get("paths"),
                                     "changed": result.get("changed")})
        except Exception as exc:  # noqa: BLE001 - un lot illisible n'arrête pas la passe
            out["errors"].append("lot #%s: refresh: %s" % (row.get("id"), exc))
    try:
        # seuls les dépôts dont cet hôte a un clone : un hôte sans clone
        # repousserait l'essai d'un autre de `retry_after_s` sans rien savoir
        from . import intake_neighbors
        apps = [app for app in store.unknown_apps()
                if intake_neighbors.resolve_clone(cfg, app)]
        claimed = (store.claim_unknown(retry_after_s=retry_after_s, limit=limit, apps=apps)
                   if apps else [])
    except Exception as exc:  # noqa: BLE001
        out["errors"].append("unknown analyses: %s" % exc)
        claimed = []
    for row in claimed:
        lot = int(row["work_item_id"])
        try:
            item = work.get(db, lot)
            if item is None:
                continue
            analysis = record_estimate(db, cfg, item, actor=actor)
            if analysis is not None:
                out["analyzed"].append({"lot": lot, "status": analysis["status"],
                                        "footprint": analysis["footprint"]})
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("lot #%s: analysis: %s" % (lot, exc))
    return out


def describe(db, item_id: int) -> dict:
    store = storage.of(db).work_footprints
    rows = store.for_items([item_id])
    try:
        analysis = store.analyses([item_id]).get(int(item_id))
    except Exception:  # noqa: BLE001 - base sans 0519
        analysis = None
    try:
        cap = store.cap_reached(item_id)
    except Exception:  # noqa: BLE001
        cap = None
    item = work.get(db, item_id) or {}
    return {"id": int(item_id),
            "paths": sorted({r["path"] for r in rows}),
            "rows": rows,
            "analysis": analysis,
            "policy": policy_of(db, item.get("app") or ""),
            "cap": cap,
            "overlapping": store.overlapping(item_id)}


def policy_line(policy: dict) -> str:
    return ("unknown footprint: %s; in-flight cap: %s; shared paths: %s%s" % (
        policy.get("unknown_policy"),
        policy.get("max_in_flight") or "none",
        ", ".join(policy.get("shared_paths") or ()) or "none",
        " (default)" if policy.get("default") else
        " (set by %s)" % policy.get("updated_by") if policy.get("updated_by") else ""))


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
                           "relative to the repository root; `.` is the whole "
                           "repository)")
    mode.add_argument("--clear", action="store_true",
                      help="remove the estimated footprint (the diff rows stay)")
    mode.add_argument("--clear-diff", action="store_true", dest="clear_diff",
                      help="remove the diff rows (e.g. computed on a wrong base); "
                           "pilot gesture, --refresh recomputes them")
    mode.add_argument("--estimate", action="store_true",
                      help="rerun the impact analysis (cited paths, identifiers found "
                           "in the repository; read-only, bounded budget, #4818)")
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
        else:
            analysis = record_estimate(db, cfg, item, actor=actor, repo=args.repo)
            if analysis is not None and not args.json:
                print("lot #%d: %s" % (args.id, analysis["note"]))
            elif analysis is None and not args.json:
                print("lot #%d: estimate not written: a manual footprint or a basket with an "
                      "unknown line — the branch diff or --set PATH… settles it" % args.id)
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
        _print_analysis(found)
        return 0
    sources: dict = {}
    for row in found["rows"]:
        sources.setdefault(row["path"], []).append(row["source"])
    confidence = {p["path"]: p.get("confidence")
                  for p in ((found.get("analysis") or {}).get("paths") or ())}
    print("lot #%d footprint:" % args.id)
    for path in found["paths"]:
        print("  %s  (%s%s)" % (_path_label(path), ", ".join(sources.get(path, [])),
                                ", %s" % confidence[path] if path in confidence else ""))
    _print_analysis(found)
    seen = set()
    for row in found["overlapping"]:
        if int(row["id"]) in seen:
            continue
        seen.add(int(row["id"]))
        print("overlaps #%d (%s%s): %s ~ %s" % (
            int(row["id"]), row["state"],
            ", %s" % row["assignee"] if row.get("assignee") else "",
            _path_label(row["path"]), _path_label(row["other_path"])))
    if found.get("cap"):
        print("repository in-flight cap reached: %s/%s" % (
            found["cap"]["in_flight"], found["cap"]["cap"]))
    return 0


def _print_analysis(found: dict) -> None:
    analysis = found.get("analysis")
    if analysis:
        print("impact analysis: %s%s, %s ms, %s git call(s), attempt %s, by %s" % (
            analysis.get("status"),
            " (%s)" % analysis["reason"] if analysis.get("reason") else "",
            analysis.get("elapsed_ms"), analysis.get("git_calls"),
            analysis.get("attempts"), analysis.get("analyzed_by")))
        weak = analysis.get("weak") or ()
        if weak:
            print("  weak (not held): %s" % ", ".join(w["path"] for w in weak))
    print("repository policy: %s" % policy_line(found.get("policy") or {}))


# --------------------------------------------------------------------------
# politique du dépôt : CLI (#4818)
# --------------------------------------------------------------------------

def add_policy_parser(sub, func):
    p = sub.add_parser(
        "footprint-policy",
        help="#4818: show or set a repository's footprint policy — unknown footprint "
             "(open, or repository: held as the whole repository), in-flight cap, "
             "shared paths ignored for overlap (pilot gesture)")
    p.add_argument("app", nargs="?", help="the repository (lot `app`); all policies without")
    p.add_argument("--unknown", choices=UNKNOWN_POLICIES, dest="unknown_policy")
    cap = p.add_mutually_exclusive_group()
    cap.add_argument("--max-in-flight", type=int, dest="max_in_flight", metavar="N",
                     help="at most N lots in progress in the repository (packet "
                          "vehicles not counted)")
    cap.add_argument("--no-cap", action="store_true", dest="no_cap")
    shared = p.add_mutually_exclusive_group()
    shared.add_argument("--shared", nargs="+", metavar="PATH", dest="shared_paths",
                        help="replace the shared paths (trivial merge: changelog, "
                             "locale index…), ignored for overlap")
    shared.add_argument("--no-shared", action="store_true", dest="no_shared")
    p.add_argument("--json", action="store_true")
    p.add_argument("--actor", default="")
    p.set_defaults(func=func)
    return p


def run_policy(cfg, db, args) -> int:
    actor = args.actor or "ameesh"
    changing = (args.unknown_policy or args.max_in_flight is not None or args.no_cap
                or args.shared_paths or args.no_shared)
    if changing:
        if not args.app:
            raise work.WorkError("footprint-policy: name the repository (app) to change")
        if not work.is_pilot(cfg, db, actor):
            raise work.WorkError("footprint-policy refused: %s is neither a human nor an "
                                 "orchestrator/design agent (pilot gesture)"
                                 % (actor or "nobody"))
        if args.max_in_flight is not None and args.max_in_flight < 1:
            raise work.WorkError("--max-in-flight: at least 1 (or --no-cap)")
        shared = None
        if args.no_shared:
            shared = []
        elif args.shared_paths:
            bad = [p for p in args.shared_paths if normalize(p) is None]
            if bad:
                raise work.WorkError("invalid shared path(s), relative to the repository "
                                     "root: %s" % ", ".join(bad))
            shared = _clean(args.shared_paths)
        result = storage.of(db).work_footprints.set_policy(
            args.app.strip(), actor=actor, unknown_policy=args.unknown_policy,
            max_in_flight=args.max_in_flight, clear_cap=args.no_cap, shared_paths=shared)
        if result.get("lots") and not args.json:
            print("%s: unknown footprint %s on %d open lot(s): %s" % (
                args.app.strip(),
                "held as the whole repository" if result["after"]["unknown_policy"]
                == "repository" else "released",
                len(result["lots"]), ", ".join("#%d" % i for i in result["lots"][:20])))
        if result["changed"]:
            from . import events
            try:
                events.record(db, "footprint.policy", args.app.strip()[:300],
                              project=args.app.strip(), actor=actor,
                              before=result["before"], after=result["after"])
            except Exception as exc:  # noqa: BLE001 - la politique est posée
                print("warning: policy change not journaled in events: %s" % exc,
                      file=sys.stderr)
    if args.app:
        found = {args.app.strip(): policy_of(db, args.app)}
    else:
        found = {app: dict(row, default=False) for app, row in
                 storage.of(db).work_footprints.policies().items()}
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2, default=str))
        return 0
    if not found:
        print("no repository footprint policy set (default: %s)"
              % policy_line(default_policy("")))
    for app, policy in sorted(found.items()):
        print("%s: %s" % (app, policy_line(policy)))
    return 0
