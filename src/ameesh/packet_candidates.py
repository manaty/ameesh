# SPDX-License-Identifier: AGPL-3.0-only
"""Lots prêts pour un paquet horaire, calculés et non demandés par courrier.

Constat #2404 (auditeur, 18/10) : des lots aux verdicts complets, gelés sur la
tête publiée de leur branche et sans conflit avec la cible ont attendu un
paquet de 1 h à 20 h, parce que le composeur partait des lots cités dans les
courriers (orchestrateur, pilote, auteurs) au lieu d'interroger l'état des
lots. Une admission perdue en courrier ne laissait alors aucune trace : le lot
n'était ni composé ni « écarté » au manifeste.

Ce module rend la liste **calculée** que la composition doit lire avant tout
courrier :

* état `qa` (aucun filtre d'assigné : un lot assigné est un candidat comme un
  autre) ;
* gel effectif (`plan_git.freeze_candidate`, #1756) sans refus, résolu en
  commit **égal à la tête publiée** de la branche (`origin`, lecture fraîche :
  une tête locale n'est pas la forge) ;
* verdicts requis acquis **sur cette tête**, par la même porte que la fusion
  (`merge_queue.review_gate`, #1374) : comptés par classe, indépendants, sans
  blocage courant ; la dispense `-none` du pilote (#1979) couvre la tête
  gelée, jamais une remise (#2496/#2681) ;
* `git merge-tree` propre contre la cible (le commit de base du paquet) ;
  #2496 : sinon, une **remise** propre sur la cible
  (`refs/ameesh/resolutions/<lot>/<cible>-*` ou `union-<cible>-*`) rend le
  lot prêt comme unité, la remise servant de source (`source`), si ses
  parents sont le gel relu et un commit de la cible, si elle fusionne
  proprement dans la base, et si un verdict la couvre (même porte que le
  gel, classe la plus stricte du gel et du contenu composé ; la dispense
  du gel ne la couvre pas) ou si c'est une union mécanique — ajouts purs
  des deux côtés dans chaque conflit, arbre égal à la fusion `merge=union`
  (§2 : conflit mécanique, aucun avis). Un blocage sur la remise la refuse
  toujours ; un verdict du gel n'est jamais écarté par une ref de remise.
  Sinon le lot reste `conflict`, avec le motif du refus de chaque remise.

Chaque refus porte un **code** stable (`freeze-not-head`, `verdicts-missing`,
`conflict`, …) et un détail lisible : la composition peut donc écrire une
ligne « écarté » motivée, et un contrôle d'inclusion peut exiger que tout lot
prêt y figure. Aucune écriture : ce module lit la base, les jalons et Git.

Un lot dont la préparation n'a pas pu être lue (branche, cible, classe, porte)
n'est ni prêt ni écarté : il porte `error`, et l'appelant doit refuser de
composer sur une liste incomplète plutôt que de l'omettre en silence.
"""
from __future__ import annotations

import os
import re
import subprocess

from . import lot_class, merge_queue, packet_guard, plan_git, resolutions, storage, work
from .canon import FETCH_TIMEOUT

SCHEMA = "ameesh-packet-candidates/1"

#: un identifiant de commit complet
_SHA = re.compile(r"[0-9a-f]{40}")
#: #2496 : où les remises lues de `origin` sont copiées dans le clone du radar
REMISE_LOCAL = "refs/ameesh/candidates/remise/"
#: #2496 : l'attribut qui rejoue une union mécanique (`merge=union`)
_UNION_ATTRIBUTES = b"* merge=union\n"


class CandidateError(RuntimeError):
    """La liste ne peut pas être calculée : cible, clone ou lecture Git."""


def _cause(code: str, detail: str = "", **extra) -> dict:
    """Un refus nommé, stable et lisible."""
    out = {"code": code, "detail": detail}
    out.update(extra)
    return out


def merge_tree(repo: str, base: str, tip: str) -> dict:
    """L'essai de fusion de `tip` dans `base` (`git merge-tree --write-tree`).

    Rend `{state: clean|conflict|error, files, detail}`. `conflict` n'est pas
    une erreur de lecture : c'est une exclusion nommée (résolution à préparer,
    ou paquet suivant). Un code de sortie autre que 0/1 est une erreur : on ne
    conclut pas.
    """
    proc = subprocess.run(
        ["git", "-C", repo, "merge-tree", "--write-tree", "--name-only", base, tip],
        capture_output=True,
    )
    text = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace").strip()
    lines = text.splitlines()
    tree = lines[0].strip() if lines else ""
    files: list[str] = []
    if proc.returncode == 1:
        # arbre écrit, puis un nom de fichier par ligne, puis un blanc et les
        # messages de conflit. On ne garde que la liste des fichiers.
        for line in lines[1:]:
            if not line.strip():
                break
            files.append(line)
        return {"state": "conflict", "files": files, "tree": tree, "detail": ""}
    if proc.returncode != 0:
        return {"state": "error", "files": [],
                "detail": (err or "git merge-tree rc=%d" % proc.returncode)[:300]}
    if not _SHA.fullmatch(tree):
        return {"state": "error", "files": [],
                "detail": "git merge-tree rendered no tree: %s" % (err or text[:300])}
    return {"state": "clean", "files": [], "tree": tree, "detail": ""}


def _ancestor(repo: str, tip: str, base: str) -> bool:
    """Le contenu du lot est-il déjà dans la cible (tête ancêtre) ?"""
    proc = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", tip, base],
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        raise CandidateError("git merge-base --is-ancestor rc=%d: %s"
                             % (proc.returncode,
                                proc.stderr.decode("utf-8", "replace").strip()[:300]))
    return proc.returncode == 0


def _git_out(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True)


def _text(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def is_remise(ref: str, lot: int, target: str) -> bool:
    """#2496 : `ref` est-elle une remise du lot `lot` sur la cible `target`
    (`refs/ameesh/resolutions/<lot>/<cible>-…` ou `…/union-<cible>-…`) ? Une
    résolution avec un autre lot (`…/<lot>/<autre>`) n'en est pas une."""
    prefix = "%s%d/" % (resolutions.REF_PREFIX, int(lot))
    if not ref.startswith(prefix):
        return False
    tail = ref[len(prefix):]
    return tail.startswith(target + "-") or tail.startswith("union-%s-" % target)


def list_remises(repo: str, where: str, lots, target: str) -> tuple[dict, str]:
    """({lot: [(ref, commit)]}, erreur) des remises de `lots` sur `target`,
    lues **fraîches** de `origin` et copiées sous `REMISE_LOCAL` dans `where`
    (le clone du radar ; jamais le dépôt des agents). Sans `origin`, les refs
    de `repo` font foi. Une lecture impossible rend l'erreur : aucune remise,
    le lot reste `conflict` avec ce motif."""
    lots = {int(lot) for lot in lots}
    if not lots:
        return {}, ""
    url = plan_git._out(repo, "remote", "get-url", "origin")
    if url:
        try:
            listing = resolutions._run(where, "ls-remote", url, resolutions.REF_PREFIX + "*",
                                       timeout=FETCH_TIMEOUT)
        except (resolutions.GitError, OSError) as exc:
            return {}, "cannot list the remises on origin: %s" % exc
        if listing.returncode:
            return {}, "cannot list the remises on origin: %s" % resolutions._err(listing)[:300]
        text = resolutions._text(listing)
    else:
        proc = _git_out(repo, "for-each-ref", "--format=%(objectname)\t%(refname)",
                        resolutions.REF_PREFIX)
        if proc.returncode:
            return {}, "cannot list the remises: %s" % proc.stderr.decode(
                "utf-8", "replace").strip()[:300]
        text = _text(proc)
    found: dict = {}
    for line in text.splitlines():
        sha, _tab, ref = line.partition("\t")
        sha, ref = sha.strip().lower(), ref.strip()
        if not _SHA.fullmatch(sha):
            continue
        for lot in lots:
            if is_remise(ref, lot, target):
                found.setdefault(lot, []).append((ref, sha))
    if not url or not found:
        return found, ""
    specs = ["+%s:%s%s" % (ref, REMISE_LOCAL, ref[len(resolutions.REF_PREFIX):])
             for refs in found.values() for ref, _sha in refs]
    try:
        proc = resolutions._run(where, "fetch", "--quiet", url, *specs, timeout=FETCH_TIMEOUT)
    except (resolutions.GitError, OSError) as exc:
        return {}, "cannot fetch the remises from origin: %s" % exc
    if proc.returncode:
        return {}, "cannot fetch the remises from origin: %s" % resolutions._err(proc)[:300]
    return found, ""


def mechanical_union(repo: str, ours: str, theirs: str, remise: str) -> str:
    """#2496 : pourquoi la remise `remise` (parents `ours`, `theirs`) n'est
    pas une remise mécanique ; vide si elle l'est.

    Mécanique veut dire sans jugement, vérifiable par tous : soit la remise
    est exactement la fusion propre de ses parents, soit chaque conflit est
    un **ajout pur des deux côtés** (section de base vide en diff3, même
    chemin des trois côtés, une seule base de fusion) et l'arbre de la
    remise est exactement celui de la fusion `merge=union`. Deux
    modifications d'une même ligne ne sont jamais une union mécanique."""
    tree = _text(_git_out(repo, "rev-parse", remise + "^{tree}"))
    if not _SHA.fullmatch(tree):
        return "tree of %s unreadable" % remise[:12]
    plain = merge_tree(repo, ours, theirs)
    if plain["state"] == "error":
        return "merge of the parents unreadable: %s" % plain["detail"]
    if plain["state"] == "clean":
        return "" if plain["tree"] == tree else \
            "content beyond the clean merge of its parents"
    bases = _text(_git_out(repo, "merge-base", "--all", ours, theirs)).split()
    if len(bases) != 1:
        return "%d merge bases between the parents" % len(bases)
    for path in plain["files"]:
        blobs = []
        for commit in (ours, bases[0], theirs):
            blob = _text(_git_out(repo, "rev-parse", "--verify", "-q",
                                  "%s:%s" % (commit, path)))
            if not _SHA.fullmatch(blob):
                return "%s: not on the three sides (added, deleted or renamed)" % path
            blobs.append(blob)
        proc = _git_out(repo, "merge-file", "-p", "--diff3", "--object-id",
                        "-L", "ours", "-L", "base", "-L", "theirs", *blobs)
        if proc.returncode < 0 or proc.returncode > 127:
            return "%s: git merge-file failed (binary file?)" % path
        why = _base_sections_empty(proc.stdout.decode("utf-8", "replace"))
        if why:
            return "%s: %s" % (path, why)
    blob = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"],
                          input=_UNION_ATTRIBUTES, capture_output=True)
    attr_blob = _text(blob)
    if blob.returncode or not _SHA.fullmatch(attr_blob):
        return "union attributes cannot be written"
    made = subprocess.run(["git", "-C", repo, "mktree"],
                          input=("100644 blob %s\t.gitattributes\n" % attr_blob).encode(),
                          capture_output=True)
    attr_tree = _text(made)
    if made.returncode or not _SHA.fullmatch(attr_tree):
        return "union attributes cannot be written"
    union = _git_out(repo, "-c", "attr.tree=%s" % attr_tree, "merge-tree", "--write-tree",
                     ours, theirs)
    lines = _text(union).splitlines()
    if union.returncode != 0 or not lines:
        return "the union merge of the parents does not resolve every conflict"
    if lines[0].strip() != tree:
        return "content beyond the union of both sides"
    return ""


def _base_sections_empty(text: str) -> str:
    """Les conflits diff3 de `text` n'ont-ils que des sections de base vides ?
    Vide si oui ; la raison sinon (marqueurs incohérents compris)."""
    section = None
    base_lines = 0
    conflicts = 0
    for line in text.splitlines():
        if line == "<<<<<<< ours":
            if section is not None:
                return "conflict markers unreadable"
            section, base_lines = "ours", 0
            conflicts += 1
        elif line == "||||||| base":
            if section != "ours":
                return "conflict markers unreadable"
            section = "base"
        elif line == "=======" and section is not None:
            if section != "base":
                return "conflict markers unreadable"
            if base_lines:
                return "both sides change the same lines (not a pure addition)"
            section = "theirs"
        elif line == ">>>>>>> theirs":
            if section != "theirs":
                return "conflict markers unreadable"
            section = None
        elif section == "base":
            base_lines += 1
    if section is not None or not conflicts:
        return "conflict markers unreadable"
    return ""


def _stricter(*classes) -> dict:
    """#2496 : la classe la plus exigeante — avis requis, puis rang, puis
    doute (un doute vaut sensible). Jamais moins que la plus stricte."""
    def key(klass: dict) -> tuple:
        rank = lot_class.RANK.get(str(klass.get("class") or lot_class.SENSITIVE), 2)
        if klass.get("doubt"):
            rank = max(rank, lot_class.RANK[lot_class.SENSITIVE])
        return (merge_queue.required_verdicts(klass), rank, bool(klass.get("doubt")))
    return max((k or {} for k in classes), key=key)


def _without_waiver(row: dict) -> dict:
    """#2496 (B2 cx2) : la dispense `-none` du pilote couvre la tête gelée,
    jamais une remise : son contenu (la résolution) n'a pas été vu."""
    return dict(row, reviewer="" if str(row.get("reviewer") or "").strip()
                == work.REVIEWER_NONE else row.get("reviewer"),
                reviewers=[r for r in (row.get("reviewers") or ())
                           if r != work.REVIEWER_NONE])


def evaluate_remise(db, row: dict, marks, *, repo: str, package_base: str,
                    frozen: str, ref: str, sha: str, klass, cfg=None,
                    canons=None) -> dict:
    """#2496 : une remise du lot peut-elle être sa source dans le paquet ?

    Rend `{ref, sha, ok, why, cover, target_parent, review_class}` ; `cover`
    dit ce qui couvre la remise : `verdict` (porte des avis sur le SHA de la
    remise) ou `mechanical` (`mechanical_union`, §2 : aucun avis). La porte
    des avis applique la classe **la plus stricte** du gel (`klass`) et du
    contenu réellement composé (la remise contre `package_base`) ; la
    dispense du pilote, liée à la tête gelée, ne la couvre jamais. `row` est
    la ligne de classe, cible du paquet comprise (`_class_row`) : la remise se
    lit sur la même cible que la tête gelée, pas sur `origin/HEAD`. Ne lève
    jamais : un doute refuse."""
    out = {"ref": ref, "sha": sha, "ok": False, "why": "", "cover": "",
           "target_parent": "", "review_class": None}
    parents = _text(_git_out(repo, "rev-parse", sha + "^@")).split()
    if len(parents) != 2 or parents.count(frozen) != 1:
        out["why"] = ("parents are not the reviewed freeze %s and one target commit"
                      % frozen[:12])
        return out
    other = parents[0] if parents[1] == frozen else parents[1]
    out["target_parent"] = other
    try:
        if not _ancestor(repo, other, package_base):
            out["why"] = "parent %s is not a commit of the target" % other[:12]
            return out
    except CandidateError as exc:
        out["why"] = str(exc)
        return out
    merged = merge_tree(repo, package_base, sha)
    if merged["state"] != "clean":
        out["why"] = ("conflict with the base: %s" % ", ".join(merged["files"])
                      if merged["state"] == "conflict" else merged["detail"]
                      or "merge with the base unreadable")
        return out
    # B2 (cx1, cx2) : la classe du contenu composé, jamais moins que le gel
    try:
        composed = lot_class.of_lot(db, int(row["id"]), cfg=cfg, item=row, repo=repo,
                                    base=package_base, head=sha, milestones=marks,
                                    canons=canons)
    except Exception as exc:  # noqa: BLE001 - classe illisible : on refuse
        out["why"] = "review class of the remise unreadable: %s" % exc
        return out
    effective = _stricter(klass, composed)
    out["review_class"] = effective
    if effective.get("class") == lot_class.RELEASE:
        out["why"] = "class release: the release procedure, not a package, carries it"
        return out
    # une résolution jugée demande au moins un avis indépendant, même pour un
    # lot léger : une porte à zéro avis ne prouve rien sur la résolution
    gate_class = effective if merge_queue.required_verdicts(effective) else \
        dict(effective, **{"class": lot_class.NORMAL})
    try:
        gate = merge_queue.review_gate(db, marks, sha, repo=repo, klass=gate_class,
                                       item=_without_waiver(row))
    except Exception as exc:  # noqa: BLE001 - porte illisible : on refuse
        out["why"] = "review gate of the remise unreadable: %s" % exc
        return out
    if gate.get("blocked"):
        # un blocage sur la remise n'est jamais levé par l'union mécanique
        out["why"] = gate.get("why") or "blocked verdict on the remise"
        return out
    if gate.get("ok") and gate.get("accepted"):
        out["ok"], out["cover"] = True, "verdict"
        return out
    union = mechanical_union(repo, parents[0], parents[1], sha)
    if not union:
        out["ok"], out["cover"] = True, "mechanical"
        return out
    out["why"] = "%s; not mechanical: %s" % (
        gate.get("why") if not gate.get("ok") and gate.get("why")
        else "no verdict on the remise", union)
    return out


def _row_target(row: dict, target: str) -> str:
    return merge_queue._remote_branch(row.get("branch_target") or target)


def _class_row(row: dict, target: str) -> dict:
    """La ligne de classe : la cible **résolue du paquet** quand le lot n'en
    déclare pas (#2464, #2574).

    Sans elle, `of_lot` relirait `origin/HEAD` du clone radar — resté sur une
    `release/*` sur pc-smichea — et surclasserait le lot. La tête gelée et ses
    remises doivent lire la même cible, jamais la ligne brute du lot."""
    return row if row.get("branch_target") else dict(row, branch_target=target)


def _release_target(branch: str) -> bool:
    value = lot_class._normalized_target(branch)
    return value == lot_class.RELEASE or value.startswith(lot_class.RELEASE + "/")


def resolve_target(project, repo: str, target: str | None = None) -> tuple[str, str]:
    """La cible du paquet et sa provenance (#2464).

    L'option `--target` prime, puis la cible déclarée du projet
    (`radar.projects.<nom>.target`), puis `origin/HEAD` du clone. Un
    `origin/HEAD` qui nomme `release/*` **sans cible déclarée** est une erreur
    de lecture, pas une classe : sur le clone radar de pc-smichea il pointait
    `release/1.11.0` (périmé depuis le 15/10), et `lot_class` rendait
    « release » chaque lot sans `branch_target` — donc tous écartés en silence.
    Le bureau remet `origin/HEAD` à jour ; ce calcul, lui, refuse de conclure
    sur cette ref.

    Rend `(branche, provenance)` avec une provenance `option`, `project` ou
    `origin/HEAD`.
    """
    declared = str(target or getattr(project, "target", "") or "").strip()
    if declared:
        return merge_queue._remote_branch(declared), ("option" if target else "project")
    try:
        fallback = plan_git.default_target(repo)
    except plan_git.MergeProbeError as exc:
        # Cible déclarée introuvable (`ameesh.target`) : erreur de lecture,
        # pas un repli silencieux sur main (#2464).
        raise CandidateError(str(exc)) from exc
    if not fallback:
        raise CandidateError(
            "no target branch: pass --target or declare the target of project %r"
            % (getattr(project, "name", "") or "?"))
    branch = merge_queue._remote_branch(fallback)
    if _release_target(branch):
        raise CandidateError(
            "origin/HEAD of %s resolves to %s, a release branch, and no target is "
            "declared (radar.projects.%s.target or --target): a clone's stale "
            "origin/HEAD is not a package target"
            % (repo, branch, getattr(project, "name", "") or "?"))
    return branch, "origin/HEAD"


def default_app(repo: str) -> str:
    """Le projet d'un dépôt sans `radar.projects` : le nom du dépôt d'origine."""
    try:
        from . import migrations
        origin = migrations.origin_repo(repo)
        if origin:
            return origin.rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001 - repli sur le nom du dossier
        pass
    return os.path.basename(os.path.realpath(repo))


def _items(db, app: str) -> list[dict]:
    """Tous les lots `qa` du projet, sans filtre d'assigné ni borne de page.

    Un filtre `assignee` ou une limite d'interface cacherait précisément les
    lots assignés que #2404 a vus omis (le composeur doit voir tous les
    candidats, pas ceux qui restent à prendre).
    """
    rows = storage.of(db).work.items(state="qa", assignee=None, limit=None)
    return [row for row in rows if (row.get("app") or "") == app]


def _marks(db, ids) -> dict:
    """Les jalons de tous les lots en une lecture (`progress.lot_milestones`)."""
    out: dict = {}
    for mark in storage.of(db).progress.lot_milestones(list(ids)):
        out.setdefault(int(mark["work_item_id"]), []).append(mark)
    return out


def inventory(db, project, *, repo: str, clone, target: str | None = None,
              app: str | None = None, base: str | None = None,
              cfg=None) -> dict:
    """La liste calculée des candidats d'un paquet pour `project`.

    `project` porte le nom et la cible déclarée (`radar.Project`) ; `clone` est
    un `resolutions.Replayer` (clone privé du radar ou clone jetable) où les
    branches et la cible sont lues **fraîches** de `origin`. `target` et `app`
    remplacent ceux du projet ; `base` force le commit de base de l'essai de
    fusion (reproduction d'un manifeste) au lieu de la tête fraîche de la
    cible. La cible est résolue par `resolve_target` : un `origin/HEAD`
    `release/*` sans cible déclarée lève `CandidateError` (#2464).

    Rend le schéma `ameesh-packet-candidates/1` : `candidates` (tous les lots
    `qa` du projet, prêts ou refusés avec leurs codes), `ready` (ids, ordre de
    priorité) et `errors` (lectures impossibles, à corriger avant de
    composer). Aucune écriture, aucun effet hors du clone `clone`.
    """
    app = (app or getattr(project, "name", "") or default_app(repo)).strip()
    target_branch, target_source = resolve_target(project, repo, target)
    rows = _items(db, app)
    ids = [int(row["id"]) for row in rows]
    marks = _marks(db, ids)
    names = [target_branch]
    names += [str(row.get("branch") or "") for row in rows if row.get("branch")]
    try:
        tips, where = merge_queue.fetch_fresh(repo, names, clone,
                                              base=target_branch)
    except merge_queue.QueueError as exc:
        raise CandidateError(str(exc)) from exc
    fetched_target = tips.get(target_branch)
    if not fetched_target:
        raise CandidateError("target %s is not on origin: the package base cannot be read"
                             % target_branch)
    if base:
        resolved = plan_git._commit(where, base)
        if not resolved:
            raise CandidateError("base %s is not a commit of %s" % (base, where))
        package_base, base_source = resolved, "option"
    else:
        package_base, base_source = fetched_target, "target"
    canons = work.configured_canons(cfg)
    candidates: list[dict] = []
    errors: list[str] = []
    # #2496 : les remises sur la cible, lues avant les gels — un verdict rendu
    # sur une remise couvre la remise, il n'est pas un nouveau gel de la
    # branche (sinon `freeze_candidate` y verrait un rebase de la tête).
    found, remise_error = list_remises(repo, where, ids, target_branch)
    remises = {lot: [(ref, listed, plan_git._commit(
                   where, ref if where == repo
                   else REMISE_LOCAL + ref[len(resolutions.REF_PREFIX):]) or "")
                   for ref, listed in sorted(refs)]
               for lot, refs in found.items()}
    for row in rows:
        lot = int(row["id"])
        shas = _remise_shas(where, remises.get(lot, []), marks.get(lot, []),
                            tips.get(str(row.get("branch") or ""), ""))
        candidates.append(_candidate(
            db, row, target_branch=target_branch, tips=tips, where=where,
            package_base=package_base,
            marks=[m for m in marks.get(lot, []) if not _names_remise(m, shas)],
            cfg=cfg, canons=canons))
    _apply_remises(db, candidates, {int(row["id"]): row for row in rows}, marks,
                   remises, remise_error, where=where, package_base=package_base,
                   cfg=cfg, canons=canons)
    for entry in candidates:
        if entry["ready"] and not entry["source"]:
            entry["source"] = entry["published_head"]
        if entry["error"]:
            errors.append("#%d : %s" % (entry["work_item"], entry["error"]))
    by_id = {entry["work_item"]: entry for entry in candidates}
    ready = sorted((entry["work_item"] for entry in candidates if entry["ready"]),
                   key=lambda lot: _ready_order(by_id[lot]))
    return {
        "schema": SCHEMA,
        "project": app,
        "repo": repo,
        "clone": where,
        "target": target_branch,
        "target_source": target_source,
        "target_head": fetched_target,
        "base": package_base,
        "base_source": base_source,
        "candidates": candidates,
        "ready": ready,
        "waiting": sorted(entry["work_item"] for entry in candidates
                          if not entry["ready"] and not entry["error"]),
        "errors": errors,
    }


def _remise_shas(repo: str, remises: list, marks: list, tip: str) -> set:
    """#2496 (B1 cx1) : les SHA de remise dont les verdicts sont mis de côté
    pour lire le gel. Seule une **fusion à deux parents** peut être une
    remise ; jamais la tête publiée de la branche ni un SHA gelé du lot :
    une ref de remise qui pointe sur le gel (ou un alias) ne fait jamais
    disparaître un verdict du gel, un blocage surtout."""
    def same(a: str, b: str) -> bool:
        a, b = a.strip().lower(), b.strip().lower()
        return len(min(a, b, key=len)) >= 7 and (a.startswith(b) or b.startswith(a))

    frozen = [str(m.get("sha") or "") for m in marks if m.get("kind") == "frozen"]
    out = set()
    for _ref, _listed, sha in remises:
        if not sha or (tip and same(sha, tip)) or any(same(sha, f) for f in frozen):
            continue
        if len(_text(_git_out(repo, "rev-parse", sha + "^@")).split()) == 2:
            out.add(sha)
    return out


def _names_remise(mark: dict, shas: set) -> bool:
    """Un verdict qui nomme une remise du lot (SHA complet ou abrégé)."""
    sha = str(mark.get("sha") or "").strip().lower()
    return (mark.get("kind") == "verdict" and len(sha) >= 7
            and any(full.startswith(sha) for full in shas))


def _apply_remises(db, candidates: list[dict], rows: dict, marks: dict, remises: dict,
                   error: str, *, where: str, package_base: str, cfg=None,
                   canons=None) -> None:
    """#2496 : un lot dont `conflict` est le seul motif devient prêt comme
    unité quand une de ses remises sur la cible passe `evaluate_remise` ; la
    remise est alors sa `source`. Sinon le motif `conflict` garde la raison du
    refus de chaque remise."""
    waiting = [entry for entry in candidates if not entry["error"]
               and [cause["code"] for cause in entry["causes"]] == ["conflict"]
               and entry["frozen_sha"] and entry["frozen_sha"] == entry["published_head"]]
    for entry in waiting:
        lot = entry["work_item"]
        cause = entry["causes"][0]
        if error:
            cause["detail"] += " (remises unreadable: %s)" % error
            continue
        evaluated = []
        for ref, listed, sha in remises.get(lot, []):
            if sha != listed:
                evaluated.append({"ref": ref, "sha": listed, "ok": False, "cover": "",
                                  "target_parent": "",
                                  "why": "remise not fetched as listed on origin"})
                continue
            evaluated.append(evaluate_remise(
                db, _class_row(rows[lot], entry["target"]), marks.get(lot, []),
                repo=where, package_base=package_base,
                frozen=entry["frozen_sha"], ref=ref, sha=sha, klass=entry["review_class"],
                cfg=cfg, canons=canons))
        entry["remises"] = evaluated
        good = [e for e in evaluated if e["ok"]]
        if not good:
            if evaluated:
                cause["detail"] += "; remise(s) refused: " + "; ".join(
                    "%s: %s" % (e["ref"].rsplit("/", 1)[-1], e["why"]) for e in evaluated)
            continue
        # la remise la plus récente sur la cible : parent de cible le plus
        # proche de la base, puis le nom de la ref (un ordre, jamais un tirage)
        def freshness(e: dict) -> tuple:
            stamp = _text(_git_out(where, "log", "-1", "--format=%ct", e["target_parent"]))
            return (int(stamp) if stamp.isdigit() else 0, e["ref"])
        chosen = max(good, key=freshness)
        entry["remise"] = chosen
        entry["source"] = chosen["sha"]
        entry["causes"] = []
        entry["ready"] = True


def _ready_order(entry: dict) -> tuple:
    """Priorité, puis âge de création, puis id : un ordre stable, jamais un tirage."""
    return (entry.get("priority") if entry.get("priority") is not None else 2,
            entry.get("created_ts") or 0.0, entry["work_item"])


def _candidate(db, row: dict, *, target_branch: str, tips: dict, where: str,
               package_base: str, marks: list[dict], cfg=None,
               canons=None) -> dict:
    """Un lot : tête publiée, gel, classe, porte des avis, essai de fusion."""
    lot = int(row["id"])
    branch = str(row.get("branch") or "").strip()
    target = _row_target(row, target_branch)
    entry: dict = {
        "work_item": lot,
        "title": row.get("title") or "",
        "assignee": row.get("assignee") or "",
        "priority": row.get("priority"),
        "created_ts": row.get("created_ts"),
        "branch": branch,
        "branch_target": row.get("branch_target") or "",
        "target": target,
        "published_head": "",
        "freeze": None,
        "frozen_sha": "",
        "review_class": None,
        "gate": None,
        "merge_tree": None,
        "ready": False,
        "causes": [],
        "error": "",
        # #2496 : le commit à composer (tête publiée, ou remise sur la cible)
        "source": "",
        "remise": None,
        "remises": [],
    }
    if not branch:
        entry["causes"].append(_cause("missing-branch",
                                      "no branch: the lot has nothing to compose"))
        return entry
    if packet_guard.is_packet_branch(branch):
        # Le lot porteur d'un paquet (`integration/**`) est le véhicule, pas
        # un lot à composer dans un autre paquet.
        entry["causes"].append(_cause(
            "packet-vehicle", "branch %s is itself a package, never a unit" % branch))
        return entry
    if target != target_branch:
        entry["causes"].append(_cause(
            "target-mismatch",
            "lot targets %s, package targets %s" % (target, target_branch)))
    tip = tips.get(branch)
    if not tip:
        entry["causes"].append(_cause(
            "branch-not-published",
            "branch %s is not on origin: a local head is not the forge" % branch))
        return entry
    entry["published_head"] = tip
    try:
        freeze = plan_git.freeze_candidate(
            marks, repo=where, target=package_base,
            waived=merge_queue.is_waived(row))
    except Exception as exc:  # noqa: BLE001 - lecture du gel impossible
        entry["error"] = "cannot read the effective freeze: %s" % exc
        return entry
    entry["freeze"] = freeze
    frozen = merge_queue.pinned(where, freeze.get("sha"))
    entry["frozen_sha"] = frozen or ""
    for cause in freeze.get("causes") or ():
        entry["causes"].append(_cause(cause, "effective freeze refused: %s" % cause))
    if freeze.get("sha") and not frozen:
        entry["causes"].append(_cause(
            "freeze-unresolved",
            "freeze %r is not a commit of %s" % (freeze.get("sha"), where)))
    if frozen and frozen != tip:
        entry["causes"].append(_cause(
            "freeze-not-head",
            "freeze %s is not the published head %s: freeze again, then review"
            % (frozen[:12], tip[:12])))
    try:
        # La classe se lit sur la cible **résolue du paquet** (#2464), comme la
        # file de fusion : sans `branch_target` sur la ligne du lot, `of_lot`
        # relirait `origin/HEAD` du clone radar et surclasserait le lot. Les
        # remises de ce lot lisent la même ligne (`_class_row`, #2574).
        klass = lot_class.of_lot(db, lot, cfg=cfg, item=_class_row(row, target),
                                 repo=where, base=package_base, head=tip,
                                 milestones=marks, canons=canons)
    except Exception as exc:  # noqa: BLE001 - classification impossible
        entry["error"] = "cannot read the review class: %s" % exc
        return entry
    entry["review_class"] = klass
    if klass.get("class") == lot_class.RELEASE:
        entry["causes"].append(_cause(
            "release",
            "class release: the release procedure, not a package, carries it"))
    try:
        if frozen:
            gate = merge_queue.review_gate(db, marks, frozen, repo=where,
                                           klass=klass, item=row)
            entry["gate"] = gate
            if gate.get("error"):
                # #2521 : une lecture impossible n'est pas un verdict manquant ;
                # la liste est incomplète et ne doit pas servir de base.
                entry["error"] = "cannot read the review gate: %s" % gate["error"]
                return entry
            if not gate.get("ok"):
                entry["causes"].append(_cause(
                    "verdicts-missing", gate.get("why") or "required verdicts missing"))
    except Exception as exc:  # noqa: BLE001 - porte illisible
        entry["error"] = "cannot read the review gate: %s" % exc
        return entry
    try:
        merged = merge_tree(where, package_base, tip)
    except Exception as exc:  # noqa: BLE001 - essai de fusion impossible
        entry["error"] = "cannot probe the merge with the target: %s" % exc
        return entry
    entry["merge_tree"] = merged
    if merged["state"] == "error":
        entry["error"] = merged["detail"] or "the merge with the target is unreadable"
        return entry
    if merged["state"] == "conflict":
        entry["causes"].append(_cause(
            "conflict", "conflict with %s: %s"
            % (target_branch, ", ".join(merged["files"]) or "files unreadable"),
            files=merged["files"]))
    try:
        if _ancestor(where, tip, package_base):
            entry["causes"].append(_cause(
                "already-in-target",
                "the frozen head is an ancestor of %s: sync-merges should close it"
                % target_branch))
    except CandidateError as exc:
        entry["error"] = str(exc)
        return entry
    entry["ready"] = not entry["causes"] and not entry["error"]
    return entry
