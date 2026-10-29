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

* état `qa` ou `validated` (aucun filtre d'assigné : un lot assigné est un candidat comme un
  autre) ;
* aucun prérequis explicite encore bloquant (`work_dependencies`, #4144),
  même si le parent est hors `qa` ; un parent fusionné ne bloque plus ;
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

#3375 : un lot dont un **hold de pilote** est actif (`planned_waits`, la même
lecture que la stagnation) n'est pas candidat, même gelé, avisé et sans
conflit : il porte le refus `pilot-hold`, dont le détail commence par « hold
pilote » — le motif que le composeur écrit dans sa table « Écartés ». Le
composeur peut passer outre explicitement (`hold_overrides`), jamais par
défaut : la surcharge ne lève que le hold, pas les autres refus (gel, avis,
conflit). Le journal de la surcharge est écrit par l'appelant, sur le lot.
L'autorité de pilote est lue **en strict** : un registre ou un canon illisible
rend la liste incomplète (`CandidateError`), jamais un lot réputé libre.

#4429 : un lot prêt qui **ajoute** une migration numérotée (répertoire
`migrations` du projet) n'est plus prêt si ce numéro est déjà sur la base du
paquet (`migration-collision`, cas « cible »), ou si un autre lot prêt ajoute
le même numéro (cas « candidats ») : un paquet qui porte les deux est rouge à
`migrate` (« duplicate migration version »). Entre candidats, le lot qui
détient la réservation L122 active du numéro reste ; sans détenteur parmi
eux, le premier dans l'ordre des prêts (priorité, âge, id). Aucune
renumérotation : le correctif reste un geste d'auteur (réserver, renommer,
regeler). Un projet sans `migrations` déclaré n'est pas lu. La couverture
par une réservation active du lot (#4430, `_check_migration_coverage`) passe
avant : un lot non couvert ne départage personne.

Un lot dont la préparation n'a pas pu être lue (branche, cible, classe, porte)
n'est ni prêt ni écarté : il porte `error` et il est nommé dans `errors` et
`errors_by_lot`. L'appelant peut alors composer les autres lots, mais il doit
écarter explicitement chaque lot en erreur : l'omettre en silence reste
interdit (#3813). Seule une erreur globale (base, cible, base de données,
inventaire) lève `CandidateError` et rend toute la liste inutilisable.

#4572 : `candidate_refusal` rend le **premier refus côté auteur** d'un seul
lot (`freeze-not-head`, `conflict`, …), avec le geste attendu, pour les
lecteurs qui ne composent pas — escalade SLO « review GO → merge » et
`ameesh debit`. Mêmes codes que `_candidate`, lecture locale des refs du
dépôt (aucun fetch) ; `ready=True` seulement quand le gel est la tête publiée
et que la fusion avec la cible est propre.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import os
import re
import subprocess
import time

from . import (lot_class, merge_queue, migrations, packet_guard, plan_git,
               resolutions, storage, work, work_dependencies)
from .canon import FETCH_TIMEOUT

SCHEMA = "ameesh-packet-candidates/1"

#: un identifiant de commit complet
_SHA = re.compile(r"[0-9a-f]{40}")
#: #2496 : où les remises lues de `origin` sont copiées dans le clone du radar
REMISE_LOCAL = "refs/ameesh/candidates/remise/"
#: #2496 : l'attribut qui rejoue une union mécanique (`merge=union`)
_UNION_ATTRIBUTES = b"* merge=union\n"


#: #4429 : un fichier de migration numéroté (toute largeur : Nexlink 3, ameesh 4)
_MIGRATION = re.compile(r"^(\d+)_.+\.sql$")


class CandidateError(RuntimeError):
    """La liste ne peut pas être calculée : cible, clone ou lecture Git."""


class HoldOverrideError(CandidateError):
    """#3375 : la surcharge explicite d'un hold de pilote est invalide ou sans objet."""


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


def changed_files(repo: str, base: str, source: str) -> list[str]:
    """Les fichiers modifiés par `source` depuis sa base de fusion avec `base`.

    C'est la lecture du partage en paquets parallèles (#3256 C) : deux lots
    qui nomment un même fichier ne doivent pas partir dans deux paquets. Un
    diff illisible est une erreur (`CandidateError`), jamais une liste vide
    supposée : le composeur ne partagerait alors pas.
    """
    if not base or not source:
        raise CandidateError("changed files: base and source are required")
    proc = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", "--no-renames", "-z",
         "%s...%s" % (base, source)],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise CandidateError(
            "git diff %s...%s: %s"
            % (base[:12], source[:12],
               proc.stderr.decode("utf-8", "replace").strip()[:300]))
    return sorted({name for name in proc.stdout.decode("utf-8", "replace").split("\0")
                   if name})


def _check_migration_coverage(db, entry, *, repo, origin_repo, base, target, cfg=None):
    """#4430: added migration files require this lot's active reservation."""
    from . import migration_reservations, migrations
    slug = migrations.origin_repo(origin_repo)
    layout = migrations.layout_for(slug, cfg) if slug else None
    if layout is None:
        return
    paths = [path for path in entry['files']
             if os.path.dirname(path) == layout.directory.rstrip('/')
             and migration_reservations._NUMBER.fullmatch(os.path.basename(path))]
    if not paths:
        return
    proc = subprocess.run(
        ['git', '-C', repo, 'diff', '--name-only', '--no-renames', '--diff-filter=A',
         '-z', '%s...%s' % (base, entry['source']), '--', layout.directory + '/'],
        capture_output=True)
    if proc.returncode:
        raise CandidateError('cannot read added migration files: ' +
                             proc.stderr.decode('utf-8', 'replace')[:300])
    added = set(proc.stdout.decode('utf-8', 'replace').split('\0'))
    numbers = {int(migration_reservations._NUMBER.fullmatch(os.path.basename(path)).group(1))
               for path in paths if path in added}
    if not numbers:
        return
    try:
        missing = migration_reservations.uncovered(
            db, entry['work_item'], numbers, repository=slug, target_branch=target)
    except Exception as exc:
        raise CandidateError('cannot read active migration reservations: %s' % exc) from exc
    if missing:
        entry['causes'].append(_cause(
            'migration-collision', 'migration number(s) %s not covered by an active reservation of lot #%s'
            % (', '.join(layout.label(number) for number in missing), entry['work_item']),
            numbers=missing))
        entry['ready'] = False


def added_migrations(repo: str, base: str, source: str,
                     directory: str) -> dict[int, list[str]]:
    """#4429 : numéro → migrations que `source` **ajoute** sous `directory`
    depuis sa base de fusion avec `base`, et qui ne sont pas déjà, au même
    chemin, dans `base`. Un diff illisible est une erreur, jamais « aucune ».
    """
    proc = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", "--no-renames", "-z",
         "--diff-filter=A", "%s...%s" % (base, source), "--", directory + "/"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise CandidateError(
            "git diff %s...%s: %s"
            % (base[:12], source[:12],
               proc.stderr.decode("utf-8", "replace").strip()[:300]))
    landed = set(target_migrations(repo, base, directory).get("paths", ()))
    found: dict[int, list[str]] = {}
    for name in proc.stdout.decode("utf-8", "replace").split("\0"):
        match = _MIGRATION.match(os.path.basename(name)) if name else None
        if (match and os.path.dirname(name) == directory and name not in landed):
            found.setdefault(int(match.group(1)), []).append(name)
    return found


def target_migrations(repo: str, base: str, directory: str) -> dict:
    """#4429 : les migrations numérotées de `base` : `{numbers: {n: [chemins]},
    paths: [chemins]}`. Une lecture impossible lève `CandidateError`."""
    proc = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-z", "--name-only", base, "--", directory + "/"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise CandidateError(
            "git ls-tree %s %s: %s"
            % (base[:12], directory,
               proc.stderr.decode("utf-8", "replace").strip()[:300]))
    numbers: dict[int, list[str]] = {}
    paths = []
    for name in proc.stdout.decode("utf-8", "replace").split("\0"):
        match = _MIGRATION.match(os.path.basename(name)) if name else None
        if match:
            numbers.setdefault(int(match.group(1)), []).append(name)
            paths.append(name)
    return {"numbers": numbers, "paths": paths}


def _migration_directory(project) -> str:
    return (getattr(project, "migrations", "") or "").strip().strip("/")


def _migration_repo(project, repo: str) -> str:
    """Le dépôt `owner/repo` des réservations L122 du projet, ou ""."""
    slug = getattr(project, "github", "") or ""
    if not slug:
        try:
            slug = migrations.origin_repo(repo) or ""
        except Exception:  # noqa: BLE001 - sans dépôt, aucun détenteur lu
            slug = ""
    return migrations.repo_key(slug)


def _exclude(entry: dict, cause: dict) -> None:
    entry["causes"].append(cause)
    entry["ready"] = False


def _migration_collisions(db, candidates: list[dict], *, where: str,
                          package_base: str, directory: str, slug: str,
                          target_branch: str) -> None:
    """#4429 : écarte les lots prêts dont une migration ajoutée entre en
    collision (numéro sur la base du paquet, ou partagé entre lots prêts).

    Lecture seule. Une lecture Git impossible met le lot en `error` (à
    écarter explicitement, #3813) ; un registre illisible pour départager
    deux candidats met **chacun** d'eux en erreur, jamais l'un réputé prioritaire.
    """
    ready = sorted((e for e in candidates if e["ready"] and not e["error"]),
                   key=_ready_order)
    if not ready:
        return
    try:
        on_target = target_migrations(where, package_base, directory)["numbers"]
    except CandidateError as exc:
        for entry in ready:
            entry["error"] = "cannot read the migrations of %s: %s" % (target_branch, exc)
            entry["ready"] = False
        return
    added: dict[int, dict[int, list[str]]] = {}
    for entry in ready:
        try:
            mine = added_migrations(where, package_base, entry["source"], directory)
        except CandidateError as exc:
            entry["error"] = "cannot read the added migrations: %s" % exc
            entry["ready"] = False
            continue
        entry["migrations"] = {str(n): paths for n, paths in sorted(mine.items())}
        for number, paths in sorted(mine.items()):
            if number in on_target:
                _exclude(entry, _cause(
                    "migration-collision",
                    "migration %s: number already on %s (%s): renumber, reserve, freeze again"
                    % (", ".join(paths), target_branch, ", ".join(on_target[number])),
                    number=number, paths=paths, case="target",
                    with_paths=on_target[number]))
            elif len(paths) > 1:
                _exclude(entry, _cause(
                    "migration-collision",
                    "migrations %s share number %d in the same lot"
                    % (", ".join(paths), number),
                    number=number, paths=paths, case="lot"))
        if entry["ready"]:
            added[entry["work_item"]] = mine
    by_number: dict[int, list[dict]] = {}
    for entry in ready:
        if entry["work_item"] in added:
            for number in added[entry["work_item"]]:
                by_number.setdefault(number, []).append(entry)
    registry = None
    for number, shared in sorted(by_number.items()):
        if len(shared) < 2:
            continue
        holder_id = None
        try:
            if registry is None:
                registry = storage.of(db).work
            holder = registry.migration_holder(number, slug) if slug else None
            holder_id = int(holder["id"]) if holder and holder.get("id") is not None else None
        except Exception as exc:  # noqa: BLE001 - départage impossible
            for entry in shared:
                entry["error"] = ("cannot read the reservation of migration %d "
                                  "shared with %s: %s" % (
                                      number, ", ".join("#%d" % e["work_item"]
                                                        for e in shared
                                                        if e is not entry), exc))
                entry["ready"] = False
            continue
        keeper = next((e for e in shared if e["work_item"] == holder_id), shared[0])
        why = ("holds the active reservation" if keeper["work_item"] == holder_id
               else "first in ready order, no candidate holds the reservation")
        for entry in shared:
            if entry is keeper:
                continue
            _exclude(entry, _cause(
                "migration-collision",
                "migration %s: number %d also added by #%d (%s), which stays (%s): "
                "renumber, reserve, freeze again"
                % (", ".join(added[entry["work_item"]][number]), number,
                   keeper["work_item"],
                   ", ".join(added[keeper["work_item"]][number]), why),
                number=number, paths=added[entry["work_item"]][number],
                case="candidates", with_lot=keeper["work_item"],
                with_paths=added[keeper["work_item"]][number]))


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
    # #3041 : l'URL est rejouée dans `where` ; un chemin relatif se lit
    # depuis `repo`.
    url = resolutions._local_from(repo, plan_git._out(repo, "remote", "get-url", "origin") or "")
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
                                       item=_without_waiver(row), cfg=cfg,
                                       target=row.get("branch_target") or None)
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
    """Tous les lots `qa` et `validated` du projet, sans filtre d'assigné ni borne de page.

    Un filtre `assignee` ou une limite d'interface cacherait précisément les
    lots assignés que #2404 a vus omis (le composeur doit voir tous les
    candidats, pas ceux qui restent à prendre).
    """
    rows = [row for state in lifecycle.TO_VALIDATE
            for row in storage.of(db).work.items(state=state, assignee=None, limit=None)]
    return [row for row in rows if (row.get("app") or "") == app]


def _marks(db, ids) -> dict:
    """Les jalons de tous les lots en une lecture (`progress.lot_milestones`)."""
    out: dict = {}
    for mark in storage.of(db).progress.lot_milestones(list(ids)):
        out.setdefault(int(mark["work_item_id"]), []).append(mark)
    return out


def _pilot_holds(db, ids, cfg=None) -> dict[int, dict]:
    """#3375 : `{lot: hold}` des holds de **pilote** actifs, avec leur motif.

    Même lecture que la stagnation : le flux ciblé du stockage
    (`Progress.lot_hold_events`, transitions + notes hold/levée) et
    `planned_waits.active_holds`, dont l'autorisation de pilote est relue à
    l'instant (`human:*`, orchestrateur/conception). Un lot peut porter
    plusieurs holds ; on retient le plus récent hold de pilote actif, celui
    qui porte le motif à citer.

    Lecture **stricte** (`strict=True`, #3375) : une autorité de pilote
    illisible (registre, canon absent/partiel, alias) est une erreur de liste,
    jamais un acteur réputé non pilote. Ignorer le hold ferait composer un lot
    réservé — c'est exactement le défaut B1 relevé en relecture.
    """
    from . import planned_waits        # import tardif : évite le cycle work/plan

    lot_ids = [int(lot) for lot in ids]
    if not lot_ids:
        return {}
    try:
        events = storage.of(db).progress.lot_hold_events(
            lot_ids, planned_waits.HOLD_NOTE_PATTERNS)
        active = planned_waits.active_holds(db, events, cfg=cfg, strict=True)
    except Exception as exc:  # noqa: BLE001 - lecture impossible : liste incomplète
        raise CandidateError("cannot read the active pilot holds: %s" % exc) from exc
    out: dict[int, dict] = {}
    for lot, holds in active.items():
        pilots = [hold for hold in holds.values() if hold.get("pilot")]
        if not pilots:
            continue
        chosen = max(pilots, key=lambda hold: float(hold.get("since") or 0.0))
        out[int(lot)] = {"by": chosen.get("by") or "",
                         "since": chosen.get("since"),
                         "note": chosen.get("note") or ""}
    return out


def _hold_moment(since) -> str:
    """Le `since` d'un hold, lisible ; une date absente n'invente rien."""
    try:
        return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(since)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "date inconnue"


def _hold_overrides(raw) -> dict[int, str]:
    """#3375 : valide `{lot: raison}` du geste explicite du composeur.

    Une raison vide, un identifiant illisible ou un doublon contradictoire
    sont des entrées invalides : une surcharge muette ne serait pas
    auditable, et deux raisons pour un lot cacheraient laquelle a été
    journalisée.
    """
    out: dict[int, str] = {}
    for lot, reason in (raw or {}).items():
        try:
            ident = int(lot)
        except (TypeError, ValueError) as exc:
            raise HoldOverrideError("hold override: invalid lot %r" % (lot,)) from exc
        if ident <= 0:
            raise HoldOverrideError("hold override: invalid lot %r" % (lot,))
        text = " ".join(str(reason or "").split())
        if not text:
            raise HoldOverrideError("hold override: lot #%d needs a reason" % ident)
        if ident in out and out[ident] != text:
            raise HoldOverrideError("hold override: lot #%d given two reasons" % ident)
        out[ident] = text
    return out


def inventory(db, project, *, repo: str, clone, target: str | None = None,
              app: str | None = None, base: str | None = None,
              cfg=None, hold_overrides: dict[int, str] | None = None) -> dict:
    """La liste calculée des candidats d'un paquet pour `project`.

    `project` porte le nom et la cible déclarée (`radar.Project`) ; `clone` est
    un `resolutions.Replayer` (clone privé du radar ou clone jetable) où les
    branches et la cible sont lues **fraîches** de `origin`. `target` et `app`
    remplacent ceux du projet ; `base` force le commit de base de l'essai de
    fusion (reproduction d'un manifeste) au lieu de la tête fraîche de la
    cible. La cible est résolue par `resolve_target` : un `origin/HEAD`
    `release/*` sans cible déclarée lève `CandidateError` (#2464).

    #3375 : un hold de pilote actif ajoute le refus `pilot-hold` (détail
    « hold pilote : … »). `hold_overrides` (`{lot: raison}`) est le geste
    explicite du composeur : il ne lève que ce refus, et seulement sur un lot
    qui porte effectivement un hold actif — une surcharge inutile, vide ou
    visant un lot hors liste est une erreur d'entrée, jamais une omission
    silencieuse. Le module reste en lecture seule : c'est l'appelant qui
    journalise la raison sur le lot.

    Rend le schéma `ameesh-packet-candidates/1` : `candidates` (tous les lots
    `qa` du projet, prêts ou refusés avec leurs codes), `ready` (ids, ordre de
    priorité), `errors` (lectures impossibles propres à un lot, à écarter) et
    `errors_by_lot` (`[{work_item, error}]`, même information attribuée au lot
    pour les consommateurs — #3813). Une erreur globale lève `CandidateError`
    et ne rend aucune liste. Aucune écriture, aucun effet hors du clone
    `clone`.
    """
    app = (app or getattr(project, "name", "") or default_app(repo)).strip()
    target_branch, target_source = resolve_target(project, repo, target)
    rows = _items(db, app)
    ids = [int(row["id"]) for row in rows]
    marks = _marks(db, ids)
    overrides = _hold_overrides(hold_overrides)
    holds = _pilot_holds(db, ids, cfg=cfg)
    # #4144 : même vérité que `work show`, y compris les prérequis hors qa.
    # Une panne de lecture ne doit jamais transformer un lot bloqué en prêt.
    try:
        dependencies = work_dependencies.grouped(db, ids)
        links = work_dependencies.links(db, ids)
    except Exception as exc:  # noqa: BLE001 - inventaire incomplet
        raise CandidateError("cannot read blocking dependencies: %s" % exc) from exc
    unknown = sorted(set(overrides) - set(ids))
    if unknown:
        raise HoldOverrideError(
            "hold override: lot(s) %s not among the %d qa lot(s) of project %s"
            % (", ".join("#%d" % lot for lot in unknown), len(ids), app or "?"))
    unheld = sorted(set(overrides) - set(holds))
    if unheld:
        raise HoldOverrideError(
            "hold override: lot(s) %s carry no active pilot hold (the override "
            "lifts a pilot hold, nothing else)" % ", ".join("#%d" % lot for lot in unheld))
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
    errors_by_lot: list[dict] = []
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
            cfg=cfg, canons=canons, pilot_hold=holds.get(lot),
            hold_override=overrides.get(lot, "")))
    _apply_remises(db, candidates, {int(row["id"]): row for row in rows}, marks,
                   remises, remise_error, where=where, package_base=package_base,
                   cfg=cfg, canons=canons)
    from . import migrations
    slug = migrations.origin_repo(repo)
    layout = migrations.layout_for(slug, cfg) if slug else None
    for entry in candidates:
        entry["conflicts_with"] = sorted({
            int(link["lot"]) for link in links.get(entry["work_item"], ())
            if link.get("kind") == "conflicts_with"})
        # Après les remises : ni une résolution de conflit ni une surcharge
        # de hold ne peuvent lever un prérequis encore bloquant.
        entry["dependencies"] = sorted({
            int(edge["blocker_id"])
            for edge in dependencies.get(entry["work_item"], [])
            if edge.get("blocker_id") is not None})
        for edge in dependencies.get(entry["work_item"], []):
            if edge["blocking"]:
                entry["causes"].append(_cause(
                    "dependency-blocking", "#%d %s" % (
                        edge["blocker_id"], edge["blocker_state"]),
                    blocker_id=edge["blocker_id"],
                    blocker_state=edge["blocker_state"]))
                entry["ready"] = False
        if entry["ready"] and not entry["source"]:
            entry["source"] = entry["published_head"]
    for entry in candidates:
        # #3256 C : les fichiers modifiés d'un lot prêt, pour ne jamais
        # partager deux lots qui se croisent entre deux paquets parallèles.
        if entry["ready"]:
            try:
                entry["files"] = changed_files(where, package_base, entry["source"])
                entry["migration_files"] = [
                    path for path in entry["files"] if (layout is not None
                         and path.startswith(layout.directory.rstrip("/") + "/"))
                    or (path.startswith("deploy/sql/") and path.endswith(".sql"))]
                _check_migration_coverage(db, entry, repo=where, origin_repo=repo,
                                          base=package_base, target=target_branch, cfg=cfg)
            except CandidateError as exc:
                entry["error"] = "cannot read the changed files: %s" % exc
                entry["ready"] = False
    # #4429 : après les prérequis et la couverture des réservations (#4430) :
    # un lot bloqué ou non couvert ne départage personne. Une collision de
    # migration (numéro sur la cible ou partagé) retire le lot des prêts.
    directory = _migration_directory(project)
    if directory:
        _migration_collisions(db, candidates, where=where, package_base=package_base,
                              directory=directory, slug=_migration_repo(project, repo),
                              target_branch=target_branch)
    for entry in candidates:
        if entry["error"]:
            errors.append("#%d : %s" % (entry["work_item"], entry["error"]))
            # #3813 : la même erreur, attribuée au lot. L'export lisible peut
            # alors rester utilisable et nommer les lots à écarter.
            errors_by_lot.append({"work_item": int(entry["work_item"]),
                                  "error": entry["error"]})
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
        "errors_by_lot": errors_by_lot,
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
               canons=None, pilot_hold: dict | None = None,
               hold_override: str = "") -> dict:
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
        # #3256 C : les fichiers modifiés (partage en paquets disjoints) et
        # les prérequis explicites du lot, même non bloquants.
        "files": [],
        "dependencies": [],
        # #4429 : numéro → migrations ajoutées (lot prêt, projet à migrations)
        "migrations": {},
        # #3375 : le hold de pilote actif, et la surcharge explicite éventuelle
        "pilot_hold": None,
        "hold_override": "",
    }
    if pilot_hold:
        entry["pilot_hold"] = dict(pilot_hold)
        if hold_override:
            entry["hold_override"] = hold_override
        else:
            entry["causes"].append(_cause(
                "pilot-hold",
                "hold pilote (par %s, depuis %s) : %s"
                % (pilot_hold.get("by") or "auteur inconnu",
                   _hold_moment(pilot_hold.get("since")),
                   pilot_hold.get("note") or "motif non consigné")))
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
                                           klass=klass, item=row,
                                           cfg=cfg, target=target)
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


# --------------------------------------------------------------------------
# #4572 : la cause côté auteur, pour l'escalade SLO et le débit
# --------------------------------------------------------------------------

#: #4572 : refus qu'un **auteur** peut lever lui-même, avec le geste attendu.
#: Le composeur du paquet ne peut rien y faire : il ne compose que `ready`.
#: Les codes sont ceux de `_candidate` (`inventory`), jamais un vocabulaire
#: parallèle. Un refus absent de cette table (avis, hold, dépendance, release,
#: cible) n'est pas une cause d'auteur et n'est pas touché ici.
AUTHOR_CAUSES = {
    "freeze-not-head":
        "freeze again on the published head, then have it reviewed",
    "freeze-required":
        "declare the freeze of the published head, then have it reviewed",
    "freeze-unresolved":
        "freeze again on a commit of the repository, then have it reviewed",
    "missing-branch":
        "give the lot its branch, push it, then freeze",
    "conflict":
        "resolve the conflict with the target, then freeze and have it reviewed; "
        "if a remise on the target is refused, fix it and have the remise reviewed",
}


def author_gesture(code: str) -> str:
    """#4572 : le geste de l'auteur pour un refus, vide si ce n'est pas le sien."""
    return AUTHOR_CAUSES.get(str(code or ""), "")


def _author_refusal(code: str, detail: str, **extra) -> dict:
    """Un refus d'auteur, dans la forme que lisent l'escalade et le débit."""
    out = {"code": code, "detail": detail, "owner": "author",
           "gesture": author_gesture(code), "ready": False, "unknown": False}
    out.update(extra)
    return out


def refusal_of(row: dict, *, frozen: str = "", raw_freeze: str = "",
               published: str = "", published_source: str = "", repo: str = "",
               target: str = "", base: str = "", remises=()) -> dict:
    """#4572 : le premier refus **côté auteur** d'un lot, ou son absence.

    Le calcul reste celui de `inventory` (`_candidate`) : mêmes codes, même
    ordre de lecture (branche, gel, tête publiée, essai de fusion contre la
    cible). Il ne fait **aucune I/O réseau** et ne compose rien : il lit les
    refs locales que `candidate_refusal` a résolues. `published` est la tête
    publiée de la branche (`origin/<branche>`) ; `branch_head` n'est qu'une
    observation et ne doit pas la remplacer (voir `plan_git.freeze_candidate`).
    Un `published` vide laisse la tête inconnue : si aucun conflit n'est
    trouvé, le résultat est `unknown` — on ne dit jamais « prêt » sans preuve.

    `ready=True` seulement quand le gel est la tête publiée et que l'essai de
    fusion est propre. `unknown=True` quand la lecture n'a pas pu conclure
    (dépôt absent, gel non résolu, refs manquantes, essai illisible). Aucun
    code n'est inventé : un refus non nommé ici (avis, hold, release…) laisse
    `code` vide, `unknown` inchangé.

    `remises` (#2496/#4572) : les remises du lot sur la cible, déjà évaluées
    (`evaluate_remise`) ou marquées `unreadable` quand leur commit n'est pas
    local. Une remise propre et couverte rend le lot **prêt** (le composeur
    peut le porter) ; des remises toutes refusées gardent `conflict` avec
    leurs motifs ; une remise non lisible ici rend `unknown` — on ne dit
    jamais « l'auteur doit résoudre » quand une remise peut le porter."""
    row = row or {}
    branch = str(row.get("branch") or "").strip()
    raw = str(raw_freeze or frozen or "").strip()
    out = {"code": "", "detail": "", "owner": "", "gesture": "",
           "ready": False, "unknown": False,
           "published": str(published or ""),
           "published_source": str(published_source or "")}
    if not branch:
        return _author_refusal("missing-branch",
                               "the lot has no branch to package")
    if not raw:
        return _author_refusal("freeze-required",
                               "the lot declares no freeze")
    if not plan_git.commit_id(raw):
        return _author_refusal("freeze-unresolved",
                               "freeze %r is not a commit id" % raw)
    if not frozen:
        out["unknown"] = True
        return out
    observed = str(published_source or "") == "observed"
    if published:
        if not plan_git.same_commit(frozen, published):
            return _author_refusal(
                "freeze-not-head",
                "freeze %s is not the %s head %s"
                % (frozen[:12], "observed" if observed else "published",
                   published[:12]))
        # Une tête publiée égale au gel autorise l'essai de fusion ; une simple
        # observation (`branch_head`) ne prouve pas la tête publiée et ne
        # permettra jamais de conclure « prêt ».
    if not repo or not base:
        out["unknown"] = True
        return out
    try:
        merged = merge_tree(repo, base, frozen)
    except Exception:  # noqa: BLE001 - essai illisible : on ne conclut pas
        out["unknown"] = True
        return out
    if merged.get("state") == "error":
        out["unknown"] = True
        return out
    if merged.get("state") == "conflict":
        detail = "conflict with %s: %s" % (
            target or "the target",
            ", ".join(merged.get("files") or ()) or "files unreadable")
        files = list(merged.get("files") or ())
        known = [rem for rem in (remises or ()) if isinstance(rem, dict)]
        good = [rem for rem in known if rem.get("ok")]
        if good:
            # #2496 : une remise propre et couverte est la source du lot ;
            # le composeur la porte, l'auteur n'a rien à débloquer.
            chosen = good[0]
            out["ready"] = True
            out["source"] = str(chosen.get("sha") or "")
            out["remise"] = chosen
            return out
        unreadable = [rem for rem in known if rem.get("unreadable")]
        if unreadable:
            out["unknown"] = True
            out["detail"] = detail + "; remise(s) unreadable here: " + "; ".join(
                "%s: %s" % (str(rem.get("ref") or "?").rsplit("/", 1)[-1],
                            rem.get("unreadable")) for rem in unreadable)
            return out
        if known:
            detail += "; remise(s) refused: " + "; ".join(
                "%s: %s" % (str(rem.get("ref") or "?").rsplit("/", 1)[-1],
                            rem.get("why") or "no reason") for rem in known)
        return _author_refusal("conflict", detail, files=files)
    if not published or observed:
        out["unknown"] = True
        return out
    out["ready"] = True
    return out


#: #4572 : un SHA complet dans une note de remise.
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def announced_remises(events, lot: int, target: str) -> list[dict]:
    """Les remises du lot sur `target` annoncées au journal (#4572).

    Un résolveur pousse sa remise et la note : « Remise sur main … poussée :
    <sha>, ref refs/ameesh/resolutions/<lot>/main-… ». Le journal est la
    seule trace locale quand le commit n'est pas dans ce dépôt ; on en tire
    la ref et le SHA complet qui la précède, jamais une conclusion —
    `evaluate_remise` tranche ensuite si l'objet est là. Lecture seule."""
    prefix = "%s%d/" % (resolutions.REF_PREFIX, int(lot))
    found: dict = {}
    for event in events or ():
        note = str(event.get("note") or "")
        for word in note.replace(",", " ").split():
            ref = word.strip().rstrip(".;:)")
            if not ref.startswith(prefix):
                continue
            tail = ref[len(prefix):]
            if not (tail.startswith(target + "-")
                    or tail.startswith("union-%s-" % target)):
                continue
            before = note[:note.find(ref)]
            shas = _FULL_SHA.findall(before)
            found.setdefault(ref, shas[-1] if shas else "")
    return [{"ref": ref, "sha": sha} for ref, sha in sorted(found.items())]


def local_remises(repo: str, lot: int, target: str) -> list[dict]:
    """Les refs de remise du lot **déjà présentes** dans `repo` (#4572).

    Aucun fetch, aucun réseau : un clone d'auteur ne suit pas
    `refs/ameesh/resolutions/*` ; le journal (`announced_remises`) complète.
    """
    proc = _git_out(repo, "for-each-ref", "--format=%(objectname)\t%(refname)",
                    resolutions.REF_PREFIX)
    if proc.returncode:
        return []
    found: dict = {}
    for line in _text(proc).splitlines():
        sha, _, ref = line.partition("\t")
        sha, ref = sha.strip().lower(), ref.strip()
        if _SHA.fullmatch(sha) and is_remise(ref, lot, target):
            found[ref] = sha
    return [{"ref": ref, "sha": sha} for ref, sha in sorted(found.items())]


def evaluate_known_remises(db, cfg, row: dict, marks, *, repo: str, target: str,
                           base: str, frozen: str, remises) -> list[dict]:
    """#4572 : évalue les remises connues du lot, comme `_apply_remises`.

    Une remise dont le commit n'est pas local, ou dont la lecture échoue, est
    rendue `unreadable` — l'appelant ne conclut alors pas. Une remise évaluée
    porte `ok`/`why`/`cover`, la forme d'`evaluate_remise` (#2496). Aucune
    écriture, aucun fetch."""
    known = [dict(rem) for rem in remises or () if isinstance(rem, dict)]
    if not known:
        return []
    klass = None
    canons = None
    try:
        canons = work.configured_canons(cfg) if cfg is not None else None
        klass = lot_class.of_lot(db, int(row["id"]), cfg=cfg,
                                 item=_class_row(row, target), repo=repo,
                                 base=base, head=frozen, milestones=list(marks or ()),
                                 canons=canons)
    except Exception:  # noqa: BLE001 - classe illisible : remises non concluantes
        klass = None
    out: list[dict] = []
    for rem in known:
        sha = plan_git.local_commit(repo, rem.get("sha") or rem.get("ref") or "")
        if not sha:
            out.append({"ref": rem.get("ref") or "", "sha": rem.get("sha") or "",
                        "ok": False, "unreadable": "commit not available locally"})
            continue
        if klass is None:
            out.append({"ref": rem.get("ref") or "", "sha": sha, "ok": False,
                        "unreadable": "review class unreadable"})
            continue
        try:
            result = evaluate_remise(
                db, _class_row(row, target), list(marks or ()), repo=repo,
                package_base=base, frozen=frozen, ref=rem.get("ref") or "",
                sha=sha, klass=klass, cfg=cfg, canons=canons)
        except Exception as exc:  # noqa: BLE001 - lecture impossible : on ne conclut pas
            result = {"ok": False, "unreadable": "%s: %s" % (type(exc).__name__, exc)}
        result["ref"] = rem.get("ref") or ""
        result["sha"] = sha
        out.append(result)
    return out


def candidate_refusal(db, cfg, row: dict, marks=None, *, overview=None,
                      repo: str | None = None, events=None) -> dict:
    """#4572 : `refusal_of` avec la résolution des refs locales du lot.

    `marks` : les jalons du lot (relus de la base s'ils manquent). `repo` peut
    être fourni par un appelant qui l'a déjà résolu. Le dépôt est retrouvé par
    le commit gelé comme dans `merge_queue.review_gate_for_item` (#2559) ;
    `overview` réutilise les lignes du registre déjà lues par la passe.
    `published` est la ref `origin/<branche>` du dépôt, jamais `branch_head` ;
    à défaut de dépôt, `branch_head` ne sert qu'à **nommer** la tête dans le
    détail (`published_source="observed"`), la conclusion restant `unknown`
    sans essai de fusion. Lecture seule, aucun fetch, aucune écriture."""
    row = row or {}
    if marks is None:
        marks = storage.of(db).work.milestones(int(row["id"]), None)
    freeze = plan_git.latest_freeze(list(marks or ()))
    raw = str((freeze or {}).get("sha") or "").strip()
    frozen = ""
    target_name = str(row.get("branch_target") or "").strip()
    base = ""
    published = ""
    source = ""
    if plan_git.commit_id(raw):
        if repo is None:
            try:
                repo, frozen = lot_class.repo_for_freeze(db, cfg, row, raw,
                                                         overview=overview)
            except Exception:  # noqa: BLE001 - dépôt illisible : on ne conclut pas
                repo, frozen = "", ""
        else:
            try:
                frozen = plan_git.local_commit(repo, raw) or ""
            except Exception:  # noqa: BLE001 - commit illisible : on ne conclut pas
                frozen = ""
    if repo:
        branch = str(row.get("branch") or "").strip()
        if branch:
            try:
                published = plan_git.local_commit(
                    repo, "origin/" + merge_queue._remote_branch(branch)) or ""
            except Exception:  # noqa: BLE001 - ref illisible : tête inconnue
                published = ""
            if published:
                source = "origin"
        try:
            target_name = lot_class.default_base(cfg, row, repo) or target_name
        except Exception:  # noqa: BLE001 - cible illisible : essai sans cible
            target_name = str(row.get("branch_target") or "").strip()
        target_name = merge_queue._remote_branch(target_name) or target_name
        if target_name:
            try:
                ref = merge_queue._class_base(repo, target_name)
                base = (plan_git._commit(repo, ref) or "") if ref else ""
            except Exception:  # noqa: BLE001 - cible illisible : essai sans base
                base = ""
    if not published:
        observed = str(row.get("branch_head") or "").strip()
        if observed:
            published, source = observed, "observed"
    result = refusal_of(row, frozen=frozen, raw_freeze=raw,
                        published=published, published_source=source,
                        repo=repo or "", target=target_name, base=base)
    if (result.get("code") == "conflict" and repo and frozen
            and target_name and base):
        # #2496/#4572 : un conflit n'est pas forcément côté auteur — une
        # remise propre et couverte sur la cible fait du lot une unité que le
        # composeur porte. On lit les remises locales et celles annoncées au
        # journal, puis on les évalue comme `_apply_remises`. Sans objet local
        # ni raison lisible, `refusal_of` rend `unknown`, jamais « l'auteur
        # doit résoudre ».
        known: dict = {rem["ref"]: rem for rem in
                       local_remises(repo, int(row["id"]), target_name)}
        if events is None and db is not None:
            try:
                events = storage.of(db).progress.lot_events([int(row["id"])])
            except Exception:  # noqa: BLE001 - journal illisible : pas d'indice
                events = ()
        for rem in announced_remises(events, int(row["id"]), target_name):
            known.setdefault(rem["ref"], rem)
        if known:
            evaluated = evaluate_known_remises(
                db, cfg, row, marks, repo=repo, target=target_name, base=base,
                frozen=frozen, remises=list(known.values()))
            if evaluated:
                result = refusal_of(
                    row, frozen=frozen, raw_freeze=raw, published=published,
                    published_source=source, repo=repo, target=target_name,
                    base=base, remises=evaluated)
    return result
