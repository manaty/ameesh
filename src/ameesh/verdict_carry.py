# SPDX-License-Identifier: AGPL-3.0-only
"""#2442 : un réalignement mécanique sur la cible ne redemande pas de verdict.

Cas du 18/10 (paquet 67 Nexlink, #2431) : deux avis favorables sur la tête
gelée, puis la branche est réalignée sur la cible (fusion de `develop`), gelée
à nouveau — et la porte (`merge_queue.review_gate`, qui ne compte que les
verdicts rendus **sur la tête gelée**) redemande deux verdicts, sans rien de
nouveau à relire. Le canon le dit déjà (§11 bis : « verdicts acquis : ni gel
répété, ni second avis sur delta ») ; ce module le fait sans jugement (§7).

**Critère** — déterministe et vérifiable, deux preuves sur la pointe
**fraîche** de la cible `T` :

1. *même atterrissage* : la fusion d'essai de la tête relue `R` et celle de la
   nouvelle tête gelée `F` sont toutes deux propres et donnent **le même
   arbre** — atterrir `F`, c'est atterrir `R` aujourd'hui ;
2. *même delta de lot* (R1 cx1, P1-2) : le diff de chaque tête depuis sa base
   de fusion avec `T` a **le même patch-id verbatim** (`plan_git.
   rebase_identity`, #1905, la preuve des candidats de paquet). C'est la preuve
   structurelle qui rend le report durable : un contenu que l'auteur ajoute à
   l'identique de la cible, sans en descendre, est masqué par `T` dans la
   preuve 1 mais change le delta — et réapparaîtrait si la cible le retirait.
   N1 (#2486) : un commit relu que la cible porte **déjà** — sa fusion dans `T`
   ne change pas l'arbre de `T` — peut manquer à `F`, un rebase l'ayant
   abandonné comme déjà appliqué, sans que le delta du lot change. Il est alors
   ignoré ; tout commit relu ni rejoué ni déjà atterri refuse le report.

Tout autre écart laisse la porte exiger ses verdicts : un commit de l'auteur,
une fusion « maligne », le contenu d'une branche qui n'est pas dans la cible,
une résolution de conflit (la tête relue ne fusionne plus proprement : la
résolution est un jugement, que seule la dispense du pilote couvre, #1979) —
sauf (#4053, canon §2 « conflit mécanique : aucun avis ») celle que git
calcule en prenant le côté de la cible (`-X ours`), sans ligne nouvelle —, et
par prudence un réalignement dont les lignes de contexte ont bougé près des
morceaux du lot. Une cible illisible, un commit absent ou une fusion d'essai
ratée ne prouvent rien : pas de report. Un **clone superficiel** ne prouve rien
non plus (N2 #2486) : `mechanical_history` dépend de l'histoire complète, comme
`train_history` (`rev-parse --is-shallow-repository`).

**Report** — pour chaque relecteur, son **dernier** verdict, s'il porte sur une
tête réalignée, est recopié en jalon `verdict` sur la tête gelée : au nom du
relecteur (sa famille compte à la porte, #1374), sans preuve de tour (#1465 :
ce n'est pas un tour du relecteur), **daté exactement de son original**.
Tout geste réel ultérieur du même relecteur reste plus récent, même si sa
transaction a commencé avant l'écriture du report (`now()` date le début de
transaction). L'identifiant départage la copie de l'original à date égale.
La copie porte la note `verdict carried: alignment …`, avec une
ligne au journal du lot. Un **blocage se reporte** comme un avis
favorable : un réalignement ne lave jamais un blocage. Un relecteur qui a déjà
un verdict sur la tête gelée n'en reçoit pas ; le dernier verdict d'un
relecteur rendu sur une autre tête (non réalignée) n'est pas remplacé par un
plus ancien.

Le report se fait au gel (`ameesh work milestone <lot> frozen`, `carry`) et à
chaque relevé des branches de l'exécuteur (`run_pass`). Les calculs git
(récupération de la pointe, fusions d'essai, patch-ids) sont **en lecture
seule** — clone du radar et objets jetables (L230) — et peuvent donc se
chevaucher entre passages et hôtes : le bail `fire:<lot>` ne les couvre pas.
L'écriture (R1 cx1, P1-1) est sérialisée : `carry` prend ce bail juste avant
d'écrire, et la transaction tient **le verrou de ligne du lot** — celui que
prend `add_milestone` pour tout gel et tout verdict — dans une seule
transaction : gel et verdicts y sont relus, et un gel ou un verdict concurrent
passe entièrement avant ou après.
Une issue définitive (reporté ou non) est journalisée une fois par
couple (tête relue, tête gelée) ; le passage suivant ne la recalcule pas.

**Désignation au gel (#2487)** — la désignation automatique (#2447) tourne
dans la transaction du gel, donc **avant** le report. Quand le report couvre la
tête, elle s'abstient : `would_carry` donne le plan du report avant le gel,
`work.milestone` le passe au hook, qui garde les relecteurs reportés dans la
colonne `work_items.reviewers` et n'envoie aucun courrier. Les sièges du gel
précédent qui n'ont pas encore voté restent désignés : le report ne reporte que
des verdicts, pas une attente (B/P1 de dev1). Par gel, **la dernière marque du
journal fait foi** (`review-designation:` pour une sélection,
`review-designation-carried:` pour un report) : un report tardif qui aboutit
retire la sélection qu'il supplante, une désignation de repli (`force`) plus
récente reprend la main, et un report après elle réécrit sa marque. Les marques
reportées sont des désignations automatiques pour `_previous_reviewers` : un
vrai gel ultérieur les remplace.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from typing import Mapping, Optional

from . import merge_queue, plan_git, storage, work

#: préfixe de la note du jalon reporté et de sa ligne de journal
CARRIED = "verdict carried: alignment"
#: préfixe de la ligne de journal d'un réalignement qui n'en est pas un
NOT_CARRIED = "verdict not carried:"
#: préfixe de la marque de désignation gardée par un report : la désignation
#: au gel s'abstient et laisse la colonne `reviewers` aux relecteurs reportés
CARRIED_MARK_PREFIX = "review-designation-carried:"
#: résultats qu'un passage de l'exécuteur dit dans son journal
LOUD = ("carried", "not-carried", "error", "validated")
#: lots examinés par passage
MAX_LOTS = 1000


def carried_mark_prefix(lot: int) -> str:
    """Le préfixe des marques reportées d'un lot (le gel filtre ensuite)."""
    return "%s%d:" % (CARRIED_MARK_PREFIX, int(lot))


def carried_mark_note(lot: int, freeze: Mapping, plan: Mapping,
                      extra_reviewers=()) -> str:
    """La marque du gel dont les relecteurs sont gardés par un report.

    Elle a la forme des marques de désignation (lot + gel dans le préfixe, JSON
    après) pour que `_previous_reviewers` la lise comme une désignation
    automatique : un vrai gel ultérieur retire ces noms de la colonne.

    `extra_reviewers` (#2487, B/P1 de dev1) : les sièges du gel précédent qui
    n'ont pas encore voté restent désignés par le gel réaligné ; le report ne
    reporte que des verdicts, pas une attente.
    """
    freeze = dict(freeze or {})
    plan = dict(plan or {})
    reviewers = []
    for entry in plan.get("carried") or ():
        name = str((entry or {}).get("actor") or "").strip()
        if name and name not in [r["name"] for r in reviewers]:
            reviewers.append({"name": name, "family": ""})
    for entry in extra_reviewers or ():
        name = str((entry or {}).get("name") or "").strip()
        if name and name not in [r["name"] for r in reviewers]:
            reviewers.append({"name": name,
                              "family": str((entry or {}).get("family") or "")})
    payload = {
        "state": "carried",
        "reviewers": reviewers,
        "freeze": {"id": int(freeze.get("id") or 0),
                   "sha": _norm(freeze.get("sha"))},
        "carried": [dict(entry or {}) for entry in plan.get("carried") or ()],
        "why": str(plan.get("detail") or ""),
    }
    return "%s%d:%d: %s" % (CARRIED_MARK_PREFIX, int(lot),
                            int(payload["freeze"]["id"]),
                            json.dumps(payload, ensure_ascii=False, sort_keys=True))


def carried_mark_of(note: str) -> Optional[dict]:
    """La marque reportée portée par une ligne de journal, ou None.

    Une marque illisible n'est jamais interprétée : None, la colonne et la
    reprise restent à la désignation normale.
    """
    text = str(note or "")
    if not text.startswith(CARRIED_MARK_PREFIX):
        return None
    _, separator, payload = text.partition(": ")
    if not separator:
        return None
    try:
        found = json.loads(payload)
    except ValueError:
        return None
    if (not isinstance(found, dict) or found.get("state") != "carried"
            or not isinstance(found.get("freeze"), dict)):
        return None
    return found


def _norm(sha) -> str:
    return str(sha or "").strip().lower()


def _same(a, b) -> bool:
    """Même commit sans dépôt : égaux, ou l'un abrégé de l'autre."""
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _key(actor) -> str:
    return str(actor or "").strip().lower() or "?"


def pending(marks, frozen) -> dict:
    """{relecteur: dernier verdict} des relecteurs dont le dernier verdict porte
    sur une **autre** tête que `frozen` — les candidats au report. Un relecteur
    qui a déjà un verdict sur la tête gelée n'en est pas un : le sien fait foi.
    Sans dépôt : les têtes se comparent à l'égalité ou au préfixe."""
    frozen = _norm(frozen)
    if not frozen:
        return {}
    latest: dict = {}
    current: set = set()
    for mark in marks or ():
        if mark.get("kind") != "verdict" or not _norm(mark.get("sha")):
            continue
        key = _key(mark.get("actor"))
        if _same(mark.get("sha"), frozen):
            current.add(key)
        order = merge_queue._mark_order
        if key not in latest or order(mark) > order(latest[key]):
            latest[key] = mark
    return {key: mark for key, mark in latest.items()
            if key not in current and not _same(mark.get("sha"), frozen)}


def merged_tree(repo: str, target: str, head: str, *, base: str | None = None,
                attributes: str | None = None, favor: str | None = None) -> str | None:
    """L'arbre de la fusion d'essai **propre** de `head` dans `target`, ou None
    (conflit, commit inconnu, git en échec). `base` impose la base de fusion
    (un cherry-pick : `base` = parent du commit repris). Les objets écrits vont
    dans un dossier jetable (le dépôt en alternates) : rien n'entre dans `.git`
    (L230). Les attributs de fusion se lisent dans l'arbre de `attributes`, la
    cible par défaut (#1517). `favor` (#4053, `ours` = `target`, `theirs` =
    `head`) résout les morceaux en conflit de contenu de ce côté (`-X`) ; un
    conflit d'une autre nature (suppression, renommage) reste un échec."""
    from . import radar
    objects = plan_git._out(repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "objects")
    if not objects:
        return None
    extra = ["--merge-base", base] if base else []
    if favor:
        if favor not in ("ours", "theirs"):
            raise ValueError("favor: ours or theirs")
        extra += ["-X", favor]
    scratch = tempfile.mkdtemp(prefix="ameesh-carry-")
    try:
        proc = radar._scratch_git(repo, objects, scratch,
                                  "-c", "attr.tree=%s" % (attributes or target),
                                  "merge-tree", "--write-tree", "-z", "--name-only",
                                  "--no-messages", *extra, target, head)
    except radar.GitError:
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0:
        return None
    lines = radar._lines(proc)
    return lines[0].strip() if lines else None


def conflict_hunks(repo: str, target: str, head: str) -> int | None:
    """#4053 : le nombre de morceaux en conflit de la fusion d'essai de `head`
    dans `target` (marques `<<<<<<<` de l'arbre écrit par git), ou None si
    rien n'est lisible. Sert à la raison tracée, jamais à la preuve."""
    from . import radar
    objects = plan_git._out(repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "objects")
    if not objects:
        return None
    scratch = tempfile.mkdtemp(prefix="ameesh-carry-")
    try:
        proc = radar._scratch_git(repo, objects, scratch,
                                  "-c", "attr.tree=%s" % target,
                                  "merge-tree", "--write-tree", "-z", "--name-only",
                                  "--no-messages", target, head)
        if proc.returncode != 1:
            return None
        lines = radar._lines(proc)
        if len(lines) < 2:
            return None
        hunks = 0
        for path in dict.fromkeys(lines[1:]):
            blob = radar._scratch_git(repo, objects, scratch, "cat-file", "blob",
                                      "%s:%s" % (lines[0].strip(), path))
            if blob.returncode != 0:
                return None
            hunks += sum(1 for line in blob.stdout.splitlines()
                         if line.startswith(b"<<<<<<< "))
        return hunks
    except radar.GitError:
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


#: commits nouveaux examinés au plus pour une preuve d'historique (borne de coût)
MAX_NEW_COMMITS = 200
#: commits relus essayés au plus pour un commit rejoué dont le conflit a été
#: résolu côté cible (#4053) : son patch-id ne nomme plus le commit repris
MAX_REPLAY_CANDIDATES = 20


def _lines_of(repo: str, *args: str) -> list[str] | None:
    proc = plan_git._git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").split()


def _complete_history(repo: str) -> bool:
    """L'histoire complète est-elle lisible ? Un clone superficiel ne prouve
    rien (N2 qa-01 #2486) : ses `rev-list` omettent des commits sans le dire.
    Même garde que `train_history` (`rev-parse --is-shallow-repository`)."""
    return plan_git._out(repo, "rev-parse", "--is-shallow-repository") == "false"


#: commits relus abandonnés examinés au plus pour la preuve « déjà atterri »
MAX_DROPPED_COMMITS = 200


def _own_delta(repo: str, head: str, tip: str) -> dict | None:
    """{patch-id verbatim: commit} des commits non-fusions propres à `head`
    depuis sa base de fusion avec `tip`, ou None si le delta est illisible."""
    base = plan_git._out(repo, "merge-base", head, tip)
    if not base:
        return None
    try:
        return plan_git._patch_ids(repo, "%s..%s" % (base, head))
    except Exception:  # noqa: BLE001 - patch-id illisible : rien de prouvé
        return None


def _landed(repo: str, tip: str, commit: str) -> bool | None:
    """Le commit `commit` est-il **déjà atterri** dans la cible `tip` ? Vrai si
    sa fusion dans `tip` laisse l'arbre de `tip` intact, None si la preuve est
    illisible (conflit, objet absent). Un commit de même patch-id dans
    l'historique ne suffit pas : si la cible l'a annulé depuis, la fusion le
    réintroduit et la réponse est non (N1 #2486)."""
    tree = plan_git._out(repo, "rev-parse", tip + "^{tree}")
    if not tree:
        return None
    merged = merged_tree(repo, tip, commit)
    if merged is None:
        return None
    return merged == tree


def _same_delta_with_landed(repo: str, reviewed: str, frozen: str,
                            tip: str) -> tuple:
    """N1 qa-01 #2486 : le delta propre du lot est constant si chaque commit
    relu est **rejoué** dans `frozen` (même patch-id verbatim) ou **déjà
    atterri** dans la cible (sa fusion ne change pas l'arbre de `tip`). Un
    rebase abandonne alors un commit déjà appliqué sans changer l'atterrissage.
    Rend `(oui, raison)` ; aucun report sur un doute ni sur un commit relu
    disparu sans preuve d'atterrissage."""
    old = _own_delta(repo, reviewed, tip)
    new = _own_delta(repo, frozen, tip)
    if old is None or new is None:
        return False, "the lot's own delta cannot be read"
    if not old:
        return False, "the lot has no commit of its own"
    added = [pid for pid in new if pid not in old]
    if added:
        return False, ("the new head brings %d commit(s) outside the reviewed delta"
                       % len(added))
    dropped = [commit for pid, commit in old.items() if pid not in new]
    if not dropped:
        return False, "no reviewed commit is missing from the new head"
    if len(dropped) > MAX_DROPPED_COMMITS:
        return False, "too many dropped commits to prove (%d)" % len(dropped)
    for commit in sorted(dropped):
        landed = _landed(repo, tip, commit)
        if landed is None:
            return False, ("the merge of reviewed commit %s into the target cannot be "
                           "read" % commit[:12])
        if not landed:
            return False, ("reviewed commit %s is neither replayed nor already landed "
                           "in the target" % commit[:12])
    return True, "%d reviewed commit(s) already landed in the target ignored" % len(dropped)


def mechanical_history(repo: str, reviewed: str, frozen: str, tip: str, *,
                       target_side: bool = False) -> tuple:
    """R1 cx1/qa-01 : chaque commit atteignable depuis `frozen` mais ni depuis
    `reviewed` ni depuis la cible `tip` est **mécanique** — rien de ce que la
    nouvelle tête apporte à l'historique n'est un contenu neuf :

    * une fusion à deux parents dont l'arbre est **exactement** la fusion
      d'essai propre de ses parents (ni conflit résolu, ni retouche) ;
    * un commit à un parent qui reprend proprement (cherry-pick, rebase) un
      commit de l'historique relu de même patch-id : son arbre est la fusion
      d'essai du commit repris sur son parent, avec pour base le parent repris.

    `target_side` (#4053) admet en plus la résolution **côté cible** calculée
    par git (`-X`), sans aucune ligne de l'auteur : une fusion dont l'arbre est
    celle de ses parents, conflits résolus du côté du parent ancêtre de `tip` ;
    un commit rejoué dont l'arbre est la reprise d'un commit relu, conflits
    résolus du côté de son parent (son patch-id a changé avec le conflit : les
    commits relus sont essayés, au plus `MAX_REPLAY_CANDIDATES`).

    Une copie à la main d'un fichier de la cible, un ajout suivi d'un retrait
    (un secret resterait dans l'historique), une fusion « maligne » ou le
    contenu d'une autre branche sont refusés. Un clone superficiel l'est aussi
    (N2 #2486) : l'histoire manquante ferait passer un commit pour neuf. Rend
    `(oui, raison)`."""
    if not _complete_history(repo):
        return False, "complete Git history required; shallow clone refused"
    new = _lines_of(repo, "rev-list", "--topo-order", "--reverse", frozen, "--not",
                   reviewed, tip)
    if new is None:
        return False, "the new commits cannot be listed"
    if len(new) > MAX_NEW_COMMITS:
        return False, "too many new commits to prove (%d)" % len(new)
    sources = None
    resolved = 0
    for commit in new:
        parents = _lines_of(repo, "rev-list", "--parents", "-n", "1", commit)
        tree = plan_git._out(repo, "rev-parse", commit + "^{tree}")
        if not parents or not tree:
            return False, "commit %s cannot be read" % commit[:12]
        parents = parents[1:]
        if len(parents) == 2:
            if merged_tree(repo, parents[0], parents[1], attributes=parents[0]) == tree:
                continue
            favor = _target_parent(repo, parents, tip) if target_side else None
            if not favor or merged_tree(repo, parents[0], parents[1], attributes=parents[0],
                                        favor=favor) != tree:
                return False, ("merge %s is not the clean, unmodified merge of its parents "
                               "(conflict resolved or content added)" % commit[:12])
            resolved += 1
            continue
        if len(parents) != 1:
            return False, "commit %s has %d parents" % (commit[:12], len(parents))
        if sources is None:
            try:
                sources = plan_git._patch_ids(repo, reviewed, "--not", tip)
            except Exception:  # noqa: BLE001 - patch-id illisible : rien de prouvé
                return False, "the reviewed commits' patch-ids cannot be read"
        try:
            pid = plan_git._diff_patch_id(repo, parents[0], commit)
        except Exception:  # noqa: BLE001 - idem
            pid = None
        source = sources.get(pid) if pid else None
        if source:
            if _replays(repo, parents[0], source, tree):
                continue
            if target_side and _replays(repo, parents[0], source, tree, favor="ours"):
                resolved += 1
                continue
            return False, ("commit %s is not the clean replay of reviewed commit %s"
                           % (commit[:12], source[:12]))
        if target_side and any(
                _replays(repo, parents[0], candidate, tree, favor="ours")
                for candidate in list(sources.values())[:MAX_REPLAY_CANDIDATES]):
            resolved += 1
            continue
        return False, ("commit %s is not a replay of a reviewed commit: new content "
                       "in the history" % commit[:12])
    if resolved:
        return True, ("%d mechanical commit(s), %d resolved on the target side"
                      % (len(new), resolved))
    return True, "%d mechanical commit(s)" % len(new)


def _replays(repo: str, parent: str, source: str, tree: str, *,
             favor: str | None = None) -> bool:
    """L'arbre `tree` est-il la reprise du commit relu `source` sur `parent`
    (base : le parent de `source`) ? Un commit de fusion ne se rejoue pas."""
    origin = _lines_of(repo, "rev-list", "--parents", "-n", "1", source)
    if not origin or len(origin) != 2:
        return False
    return merged_tree(repo, parent, source, base=origin[1], attributes=parent,
                       favor=favor) == tree


def _target_parent(repo: str, parents: list, tip: str) -> str | None:
    """#4053 : le côté cible d'une fusion à deux parents, au sens de `-X` —
    `ours` si seul le premier parent est ancêtre de `tip`, `theirs` si seul le
    second l'est ; None sinon (aucun côté cible prouvé)."""
    inside = [plan_git._git(repo, "merge-base", "--is-ancestor", p, tip).returncode == 0
              for p in parents]
    if inside == [True, False]:
        return "ours"
    if inside == [False, True]:
        return "theirs"
    return None


def realigned(repo: str, reviewed: str, frozen: str, tip: str) -> tuple:
    """`frozen` est-elle un réalignement mécanique de `reviewed` sur la cible
    de pointe `tip` ? Trois preuves : même atterrissage, historique mécanique,
    même delta de lot. Rend `(oui, raison)` ; la raison dit pourquoi non. Un
    clone superficiel est refusé d'emblée (N2 #2486).

    #4053 — conflit résolu **côté cible** (canon §2 : « conflit mécanique :
    aucun avis ») : quand la tête relue ne fusionne plus proprement, la
    résolution reste mécanique si la fusion d'essai de `reviewed` dans `tip`,
    morceaux en conflit pris du côté de la cible (`-X ours`), donne l'arbre de
    la fusion propre de `frozen` : aucune ligne nouvelle. L'historique doit
    alors être mécanique au même sens (`target_side`) ; il tient lieu de preuve
    du delta, que le conflit résolu change par construction. Toute autre
    résolution (une ligne ajoutée, le côté du lot) reste un jugement."""
    if not _complete_history(repo):
        return False, "complete Git history required; shallow clone refused"
    if _same(reviewed, frozen):
        return False, "same head"
    theirs = merged_tree(repo, tip, frozen)
    if theirs is None:
        return False, "the frozen head does not merge cleanly into the target"
    ours = merged_tree(repo, tip, reviewed)
    if ours is None:
        if merged_tree(repo, tip, reviewed, favor="ours") != theirs:
            return False, ("the reviewed head no longer merges cleanly into the target: "
                           "the new head resolves a conflict (a judgment, not a "
                           "realignment)")
        history, why = mechanical_history(repo, reviewed, frozen, tip, target_side=True)
        if not history:
            return False, ("conflict resolved like the target, but the history is not "
                           "mechanical: %s" % why)
        hunks = conflict_hunks(repo, tip, reviewed)
        return True, ("conflict resolved on the target side: %s hunk(s) already carried "
                      "by the target, no new line (%s)"
                      % ("?" if hunks is None else hunks, why))
    if ours != theirs:
        return False, ("landing the frozen head would not land the reviewed content: "
                       "the merge results differ")
    history, why = mechanical_history(repo, reviewed, frozen, tip)
    if not history:
        return False, why
    # R1 cx1, P1-2 : le delta propre du lot est le même des deux côtés
    # (`plan_git.rebase_identity`, #1905 : patch-id verbatim du diff écrasé
    # depuis la base de chaque tête avec la cible).
    same = plan_git.rebase_identity(repo, reviewed, frozen, tip)
    reason = "same merge result, mechanical history and same lot delta"
    if not same.get("mechanical"):
        # N1 qa-01 #2486 : un rebase peut abandonner un commit relu déjà
        # atterri dans la cible ; il ne compte alors pas dans le delta.
        ok, why = _same_delta_with_landed(repo, reviewed, frozen, tip)
        if not ok:
            return False, ("the lot's own delta differs from the reviewed one (%s): %s"
                           % (same.get("detail") or "?", why))
        reason += " (%s)" % why
    return True, reason


def _outcomes(db, lots) -> dict:
    """{lot: notes de journal des issues déjà rendues}, en une lecture."""
    out: dict = {int(lot): [] for lot in lots}
    try:
        rows = storage.of(db).work.events_noted("verdict ", 0.0, ids=sorted(out))
    except Exception:  # noqa: BLE001 - journal illisible : on recalcule
        return out
    for r in rows:
        note = str(r.get("note") or "")
        if note.startswith((CARRIED, NOT_CARRIED)) and int(r["work_item_id"]) in out:
            out[int(r["work_item_id"])].append(note)
    return out


def _arrow(reviewed: str, frozen: str) -> str:
    return "%s → %s" % (_norm(reviewed)[:12], _norm(frozen)[:12])


_ARROW_RE = re.compile(r"\b([0-9a-f]{4,64}) → ([0-9a-f]{4,64})\b")


def _decided(notes, reviewed: str, frozen: str) -> bool:
    """Une issue est-elle déjà journalisée pour ce couple de têtes ? Les têtes
    se comparent au préfixe (N1 qa-01) : un verdict qui cite un SHA abrégé
    retrouve l'issue écrite avec le SHA résolu, sans nouveau calcul."""
    for note in notes or ():
        for left, right in _ARROW_RE.findall(note):
            if _same(left, reviewed) and _same(right, frozen):
                return True
    return False


def _clone(cfg, row: dict, repo: str):
    """Le clone radar où lire la cible et la tête gelée.

    #3331 : le clone suit le dépôt lu, pas l'`app` — un lot du canon se lit
    dans un clone du canon, jamais dans celui du projet (`radar.clone_dir`
    nomme le clone d'après le dépôt quand il diffère de celui du projet)."""
    from . import plan_git, radar, resolutions
    name = str(row.get("app") or "").strip() or os.path.basename(repo.rstrip("/"))
    project = None
    try:
        project = radar.settings_of(cfg).projects.get(name)
    except Exception:  # noqa: BLE001 - réglages radar illisibles : cible par défaut
        project = None
    slug = plan_git._repo_slug(repo)
    if slug:
        repo_name = slug.rsplit("/", 1)[-1]
        if repo_name.casefold() != name.casefold():
            name = repo_name
    return resolutions.Replayer(repo, radar.clone_dir(cfg, name),
                                target=getattr(project, "target", "") or "")


def _passive(db, to, lot: int, text: str, data: dict) -> None:
    """Le mot au fil du lot, sans réveiller personne."""
    from . import mail
    if not to or str(to).startswith(work.HUMAN_PREFIX):
        return
    try:
        mail.send(db, work.SYSTEM_SENDER, to, text, kind="event", work_item_id=str(lot),
                  payload={"radar": dict(data, kind="verdict-carry")})
    except Exception:  # noqa: BLE001 - un mot n'est jamais fatal
        pass


def would_carry(cfg, db, lot: int, frozen_sha: str, *, repo: str | None = None,
                clone=None, notes=None, fetched: dict | None = None) -> dict:
    """Lecture seule : `carry` copierait-il des verdicts sur `frozen_sha` ?

    Le gel n'a pas besoin d'exister encore : `frozen_sha` est la tête qu'on
    s'apprête à geler. Sert à la désignation au gel (#2487) pour s'abstenir
    quand le report couvre la tête — aucun nouveau relecteur, aucun courrier.
    Même critère et mêmes lectures que `carry` (`realigned`), sans écriture ni
    bail. Rend la forme de `carry`, `result` : `would-carry`, `not-carried`,
    `idle`, `no-repository`, `error`. Ne lève jamais.
    """
    lot = int(lot)
    out: dict = {"lot": lot, "result": "idle", "frozen": "", "target": "", "tip": "",
                 "carried": [], "refused": [], "detail": ""}
    try:
        out, plan, refused, _where = _plan_for(
            cfg, db, lot, out, _norm(frozen_sha), repo=repo, clone=clone,
            notes=notes, fetched={} if fetched is None else fetched)
    except Exception as exc:  # noqa: BLE001 - la désignation garde son cours
        out.update(result="error", detail="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return out
    if out["result"] in ("no-repository", "error"):
        return out
    out["result"] = "would-carry" if plan else "not-carried" if refused else "idle"
    return out


def carry(cfg, db, lot: int, *, repo: str | None = None, clone=None,
          dry_run: bool = False, lock: bool = True, notes=None,
          fetched: dict | None = None) -> dict:
    """Reporte sur la tête gelée du lot les verdicts rendus sur une tête dont
    elle n'est qu'un réalignement mécanique (critère du module).

    Rend `{lot, result, frozen, target, tip, carried, refused, detail}` ;
    `result` : `idle` (rien à reporter), `carried`, `not-carried`,
    `no-repository`, `busy` (bail tenu ailleurs), `moved` (gel changé
    pendant le calcul), `error` (cible illisible…). `would-carry` en essai.
    `notes` : les issues déjà journalisées du lot, lues par l'appelant ;
    `fetched` : les pointes de cible déjà récupérées pendant ce passage, par
    `(dépôt, cible)` (N2 qa-01 : une récupération par projet, pas par lot) ;
    `lock` : prend le bail `fire:<lot>` pour l'écriture **seule** — les calculs
    git sont des lectures concurrentes (N3 qa-01 #2486).
    Ne lève jamais."""
    lot = int(lot)
    out: dict = {"lot": lot, "result": "idle", "frozen": "", "target": "", "tip": "",
                 "carried": [], "refused": [], "detail": ""}
    try:
        return _carry(cfg, db, lot, out, repo=repo, clone=clone, dry_run=dry_run, lock=lock,
                      notes=notes, fetched={} if fetched is None else fetched)
    except Exception as exc:  # noqa: BLE001 - jamais fatal : retenté au passage suivant
        out.update(result="error", detail="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return out


def _carry(cfg, db, lot: int, out: dict, *, repo, clone, dry_run: bool, lock: bool,
           notes, fetched: dict) -> dict:
    from . import fire
    row = work.get(db, lot)
    if row is None or row.get("state") in tuple(work.MERGED_STATES) + ("closed",):
        out["detail"] = "lot not open"
        return out
    freeze = plan_git.latest_freeze(work.milestones(db, lot))
    declared = _norm((freeze or {}).get("sha"))
    if not declared:
        out["detail"] = "not frozen"
        return out
    out, plan, refused, where = _plan_for(
        cfg, db, lot, out, declared, repo=repo, clone=clone, notes=notes,
        fetched=fetched)
    if out["result"] in ("no-repository", "error"):
        return out
    if not plan and not refused:
        out["result"] = "idle"
        return out
    if dry_run:
        out["result"] = "would-carry" if plan else "not-carried"
        return out
    handle = fire._lock(db, lot) if lock else None
    if lock and handle is None:
        out.update(result="busy", detail="another host holds the lease of lot #%d" % lot)
        return out
    try:
        return _write(db, lot, out, declared, plan, refused, where)
    finally:
        if handle is not None:
            fire._unlock(handle)


def _plan_for(cfg, db, lot: int, out: dict, declared: str, *, repo, clone, notes,
              fetched: dict) -> tuple:
    """Le plan du report pour `declared` : `(out, plan, refused, where)`.

    Lecture seule, partagée par `carry` (écrit) et `would_carry` (désignation au
    gel, avant même que le jalon `frozen` existe). `out["result"]` ne porte que
    les refus terminaux (`no-repository`, `error`) ; l'appelant décide
    `idle`/`would-carry`/`not-carried`."""
    from . import lot_class
    row = work.get(db, lot)
    if row is None or row.get("state") in tuple(work.MERGED_STATES) + ("closed",):
        out["detail"] = "lot not open"
        return out, [], [], ""
    if not declared:
        out["detail"] = "not frozen"
        return out, [], [], ""
    out["frozen"] = declared
    marks = work.milestones(db, lot)
    candidates = pending(marks, declared)
    if not candidates:
        out["detail"] = "no verdict to carry"
        return out, [], [], ""
    if notes is None:
        notes = _outcomes(db, [lot])[lot]
    if all(_decided(notes, m.get("sha"), declared) for m in candidates.values()):
        out["detail"] = "already decided for this freeze"
        return out, [], [], ""
    repo = repo or lot_class.repository(cfg, row)
    if not repo:
        out.update(result="no-repository", detail="no repository known for the lot on this "
                   "host: the executor's branch sweep retries")
        return out, [], [], ""
    target = lot_class.default_base(cfg, row, repo)
    if not target:
        out.update(result="error", detail="no target branch known for the lot")
        return out, [], [], ""
    out["target"] = merge_queue._remote_branch(target)
    clone = clone or _clone(cfg, row, repo)
    key = (repo, out["target"])
    if key not in fetched:
        tips, where = merge_queue.fetch_fresh(repo, [target], clone)
        fetched[key] = (tips.get(target), where)
    tip, where = fetched[key]
    if not tip:
        out.update(result="error", detail="target %s not found on origin" % out["target"])
        return out, [], [], ""
    out["tip"] = tip
    if merge_queue.pinned(where, declared) is None and row.get("branch"):
        # objets de la tête gelée, bornés par la cible
        merge_queue.fetch_fresh(repo, [row["branch"]], clone, base=out["target"])
    frozen = merge_queue.pinned(where, declared)
    if not frozen:
        out.update(result="error", detail="the freeze %s is not a commit of %s"
                   % (declared[:12], where))
        return out, [], [], ""
    out["frozen"] = frozen
    verdicts: dict = {}          # tête relue complète -> (oui, raison)
    plan: list = []              # (relecteur, verdict reporté)
    refused: list = []           # (tête relue, raison)
    for key, mark in sorted(candidates.items()):
        if _decided(notes, mark.get("sha"), frozen):
            continue
        reviewed = merge_queue.pinned(where, mark.get("sha"))
        if not reviewed:
            out["detail"] = "reviewed head %s is not a commit of %s" % (
                _norm(mark.get("sha"))[:12], where)
            continue                  # objet absent : rien de prouvé, retenté
        if reviewed not in verdicts:
            verdicts[reviewed] = realigned(where, reviewed, frozen, tip)
        ok, why = verdicts[reviewed]
        if ok:
            plan.append((mark, reviewed))
        elif (reviewed, why) not in refused:
            refused.append((reviewed, why))
    if not plan and not refused:
        if not out["detail"]:
            out["detail"] = "nothing decidable"
        return out, [], [], where
    out["carried"] = [_entry(m, r) for m, r in plan]
    out["refused"] = [{"reviewed": r, "why": w} for r, w in refused]
    return out, plan, refused, where


def _entry(mark: dict, reviewed: str) -> dict:
    return {"actor": str(mark.get("actor") or ""), "verdict": mark.get("verdict") or "ok",
            "from": reviewed}


_MARKS_SQL = ("SELECT id, kind, sha, actor, verdict,"
              "       extract(epoch from at)::float8 AS at_ts"
              "  FROM work_item_milestones"
              " WHERE work_item_id = %s AND kind IN ('frozen', 'verdict')")

_COPY_SQL = ("INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, verdict, note, at)"
             " SELECT work_item_id, 'verdict', %s, actor, verdict, %s,"
             "        at"
             "   FROM work_item_milestones"
             "  WHERE id = %s AND work_item_id = %s AND kind = 'verdict'"
             " RETURNING id")

#: #2928 : la preuve du verdict d'origine suit le jalon reporté. Le reçu
#: nomme le nouveau jalon et sa tête (la même que celle du jalon copié), avec
#: l'acteur, la session, le tour et le harnais du relecteur d'origine : c'est
#: son geste qui reste la preuve, le report n'étant que mécanique. Sans cette
#: copie, un verdict reporté sur un lot sensible perdait son reçu et la porte
#: le refusait, alors que le report dit « pas de nouvelle relecture due ».
_COPY_RECEIPT_SQL = (
    "INSERT INTO provenance_receipts"
    "       (subject, sha, actor, session, turn, harness, work_item_id)"
    " SELECT 'milestone:' || %s, %s, actor, session, turn, harness, work_item_id"
    "   FROM provenance_receipts"
    "  WHERE subject = 'milestone:' || %s"
    " ON CONFLICT (subject) DO NOTHING")


def _write(db, lot: int, out: dict, declared: str, plan: list, refused: list,
           where: str) -> dict:
    """Écrit les jalons reportés dans **une** transaction qui tient le verrou de
    ligne du lot — celui de `add_milestone` (R1 cx1, P1-1) : gel et verdicts y
    sont relus, un gel plus récent annule tout (`moved`), et un relecteur dont
    le dernier verdict a changé entre-temps garde le sien. Un gel ou un verdict
    concurrent attend le COMMIT, ou est vu s'il l'a précédé. Les lignes de
    journal et les mots suivent le COMMIT.

    Quand le report couvre la tête (aucun refus), la colonne `reviewers` est
    gardée sur les relecteurs reportés et la marque `review-designation-carried`
    est écrite dans la même transaction (#2487) : la désignation au gel s'est
    abstenue, et un relevé d'exécuteur tardif ne laisse pas une autre
    désignation fermer la porte."""
    from . import train_events
    frozen, tip, target = out["frozen"], out["tip"], out["target"]
    carried = []
    with db.transaction() as tx:
        rows = tx.query("SELECT id, state, title, app, assignee, branch_target"
                        "  FROM work_items WHERE id = %s FOR NO KEY UPDATE", (int(lot),))
        marks = tx.query(_MARKS_SQL, (int(lot),)) if rows else []
        freeze = plan_git.latest_freeze(marks)
        if not rows or not freeze or not merge_queue._same_sha(freeze.get("sha"), frozen, where):
            out.update(result="moved", detail="the freeze changed meanwhile: retried next pass")
            return out
        row = rows[0]
        still = pending(marks, declared)
        for mark, reviewed in plan:
            now = still.get(_key(mark.get("actor")))
            if now is None or int(now.get("id") or 0) != int(mark.get("id") or 0):
                continue              # verdict rendu ou reporté entre-temps : le sien vaut
            note = ("%s %s on %s@%s, same merge result and same lot delta as the reviewed "
                    "head (#2442)" % (CARRIED, _arrow(reviewed, frozen), target, tip[:12]))
            copied = tx.query(_COPY_SQL, (frozen, note, int(mark["id"]), int(lot)))
            if copied:
                # #2928 : le reçu du verdict d'origine suit le jalon reporté
                # (même acteur/session/tour, tête du nouveau jalon).
                tx.execute(_COPY_RECEIPT_SQL,
                           (int(copied[0]["id"]), frozen, int(mark["id"])))
                carried.append(_entry(mark, reviewed))
        if carried:
            try:
                train_events.emit(tx, kind="verdict", project=row.get("app") or "",
                                  target=merge_queue._remote_branch(row.get("branch_target") or ""))
            except ValueError:
                pass                  # lot sans sujet de train : le jalon reste valide
        if carried and not refused:
            _keep_reviewers(tx, lot, row, freeze, plan, out)
    st = storage.of(db).work
    state = row.get("state") or ""
    by_head: dict = {}
    for entry in carried:
        by_head.setdefault(entry["from"], []).append(entry)
    for reviewed, entries in by_head.items():
        st.note(lot, state, "%s %s on %s@%s: %s — no re-review due (#2442)"
                % (CARRIED, _arrow(reviewed, frozen), target, tip[:12],
                   ", ".join("%s %s" % (e["verdict"], e["actor"]) for e in entries)),
                work.SYSTEM_SENDER, once=True)
    for reviewed, why in refused:
        st.note(lot, state, "%s %s on %s@%s: %s — verdicts are due on the frozen head (#2442)"
                % (NOT_CARRIED, _arrow(reviewed, frozen), target, tip[:12], why),
                work.SYSTEM_SENDER, once=True)
    out.update(carried=carried, refused=[{"reviewed": r, "why": w} for r, w in refused],
               result="carried" if carried else "not-carried" if refused else "idle")
    if carried:
        text = ("Lot #%d « %s »: the new freeze %s is a mechanical realignment on %s "
                "(same merge result and same lot delta as the reviewed head); verdicts "
                "carried: %s. No re-review is due (#2442)."
                % (lot, row.get("title") or "", frozen[:12], target,
                   ", ".join("%s %s from %s" % (e["verdict"], e["actor"], e["from"][:12])
                             for e in carried)))
        data = {"project": row.get("app") or "", "target": target, "sha": frozen}
        for to in [row.get("assignee")] + [e["actor"] for e in carried]:
            if to:
                _passive(db, to, lot, text, data)
    if carried:
        from .freeze_gate import _in_transaction
        # Nested carries are reconciled by the outer freeze writer. Ordinary
        # carries apply the same guarded quorum transition as a rendered
        # verdict (#4423), after committing their copied verdicts and
        # provenance; a failed read is journaled and retried by `run_pass`
        # (#4834), never dropped.
        if not _in_transaction(db):
            out["validation"] = promote_carried(db, lot, freeze=freeze)
    return out


#: la ligne de journal d'une transition de quorum reportée qui n'a pas abouti
PENDING = "carried quorum pending:"
#: une panne de lecture se retente ce nombre de fois, puis le lot reste qa
#: (l'alerte de lot en qa et l'auditeur prennent le relais, #4834)
PENDING_ATTEMPTS = 5


def _carried_ok_on_freeze(marks, freeze) -> list[dict]:
    """Les verdicts `ok` **reportés** sur la tête gelée, écrits après le gel.

    Le report copie le verdict d'origine avec son horodatage et un identifiant
    neuf : c'est ce `id > freeze` que `_sync_validation` (#4423) exige pour
    qu'un écrivain puisse promouvoir le lot. Un verdict bloqué n'ouvre jamais
    la porte ; la porte est de toute façon relue avant l'écriture."""
    frozen = _norm((freeze or {}).get("sha"))
    if not frozen:
        return []
    freeze_id = int((freeze or {}).get("id") or 0)
    found = []
    for mark in marks or ():
        if mark.get("kind") != "verdict" or (mark.get("verdict") or "") != "ok":
            continue
        if int(mark.get("id") or 0) <= freeze_id:
            continue
        if not _same(mark.get("sha"), frozen):
            continue
        if str(mark.get("note") or "").startswith(CARRIED):
            found.append(mark)
    return found


def _carried_blocked_on_freeze(marks, freeze) -> bool:
    """Un verdict bloqué sur la tête gelée ferme la porte : rien à retenter."""
    frozen = _norm((freeze or {}).get("sha"))
    if not frozen:
        return False
    return bool(merge_queue.blocked_verdicts(marks or (), frozen))


def _host(db) -> str:
    """L'hôte du passage, pour compter les essais de reprise par hôte (#4834)."""
    return str(getattr(getattr(db, "cfg", None), "host", "") or "").strip()


def promote_carried(db, lot: int, *, freeze=None) -> dict:
    """La transition de quorum d'un verdict reporté, comme pour un avis rendu.

    Après la copie des verdicts — un INSERT direct, qui ne passe donc pas par
    `add_milestone` ni par `_sync_validation` (#4423) — le lot doit passer en
    `validated` dès que la porte est ouverte. L'écriture reste celle d'une
    validation ordinaire (`work.move`, garde de quorum relue et sérialisée).

    Un échec n'est **pas** silencieux (#4715, #4834). Une porte **illisible**
    (lecture en échec, provenance non concluante) est une panne passagère :
    une ligne `carried quorum pending:` la dit à chaque essai (5 au plus), et
    `run_pass` retente. Une porte **lisible et fermée** (avis manquant,
    bloquant) est le cours normal du `qa` : aucun retry, aucun bruit — le
    verdict suivant la rouvrira. Sans cette distinction, une lecture ratée
    laissait le lot en `qa` pour toujours : le report avait déjà marqué ses
    verdicts « décidés », aucun écrivain ne revenait. Rend
    `{result: validated|pending|closed|exhausted|idle, detail}` ; ne lève
    jamais."""
    row = work.get(db, lot)
    if not row or str(row.get("state") or "") != "qa":
        return {"result": "idle", "detail": "lot not in qa"}
    if str(row.get("workflow_ref") or "").strip():
        return {"result": "idle", "detail": "pinned lot"}
    marks = work.milestones(db, lot)
    latest = plan_git.latest_freeze(marks)
    freeze = freeze or latest
    if not _carried_ok_on_freeze(marks, freeze):
        return {"result": "idle", "detail": "no carried ok verdict on the frozen head"}
    # #4834 : un hôte sans dépôt local ne peut pas conclure la porte. Il ne
    # doit ni écrire de marque ni consommer d'essai (le budget est par hôte) :
    # le prochain passage d'un hôte qui résout le gel fera la transition.
    from . import lot_class
    try:
        repo, _resolved = lot_class.repo_for_freeze(
            db, getattr(db, "cfg", None), row, _norm(freeze.get("sha")))
    except Exception:  # noqa: BLE001 - une sonde ratée ne conclut pas
        repo = ""
    if not repo:
        return {"result": "no-repository",
                "detail": "the frozen head is unreadable from this host"}

    def _attempt():
        work.move(db, lot, "validated", actor=work.SYSTEM_SENDER,
                  note="qa → validated — carried review quorum (#4423)")

    exc = None
    try:
        _attempt()
        return {"result": "validated"}
    except Exception as caught:  # noqa: BLE001 - classée ci-dessous
        exc = caught
    # La lecture qui a échoué est refaite une fois, hors du geste d'écriture :
    # elle dit si la porte est fermée pour une raison durable, lisible et
    # normale, ou si la lecture elle-même n'a pas conclu (panne passagère).
    try:
        gate = merge_queue.review_gate_for_item(
            db, getattr(db, "cfg", None), row, marks)
    except Exception:  # noqa: BLE001 - une lecture ratée ne conclut pas
        gate = None
    if isinstance(gate, dict) and gate.get("ok") and not gate.get("error"):
        # La porte est ouverte : la première tentative a échoué sur autre
        # chose (course, transaction). Un second essai borné, puis la reprise.
        try:
            _attempt()
            return {"result": "validated"}
        except Exception as caught:  # noqa: BLE001 - reprise au passage suivant
            exc = caught
    elif isinstance(gate, dict) and not gate.get("error"):
        # Porte lisible et fermée : avis manquant ou bloquant. Le lot reste en
        # `qa` par la règle ordinaire ; le prochain verdict le promouvra.
        return {"result": "closed", "detail": gate.get("why") or ""}
    why = ("%s: %s" % (type(exc).__name__, exc) if not isinstance(exc, work.WorkError)
           else str(exc))
    freeze_id = int((freeze or {}).get("id") or 0)
    host = _host(db)
    try:
        # Le compte est celui de **ce** gel et de **cet** hôte : une panne
        # réparée ailleurs ne doit pas épuiser le budget local.
        seen = storage.of(db).work.events_noted(
            "%s freeze #%d host %s " % (PENDING, freeze_id, host),
            0.0, ids=[int(lot)])
        attempts = len(seen)
    except Exception:  # noqa: BLE001 - lecture impossible : on s'arrête
        attempts = PENDING_ATTEMPTS
    if attempts >= PENDING_ATTEMPTS:
        return {"result": "exhausted", "detail": why}
    try:
        retry = ("retried by the executor pass" if attempts + 1 < PENDING_ATTEMPTS
                 else "no further retry")
        storage.of(db).work.note(
            lot, "qa",
            "%s freeze #%d host %s — %s — attempt %d/%d, %s (#4834)"
            % (PENDING, freeze_id, host, why[:200], attempts + 1,
               PENDING_ATTEMPTS, retry),
            work.SYSTEM_SENDER)
    except Exception:  # noqa: BLE001 - une note ne retient jamais la passe
        pass
    return {"result": "pending", "detail": why}


def _keep_reviewers(tx, lot: int, row: dict, freeze: dict, plan: list, out: dict) -> None:
    """Garde les relecteurs reportés dans la colonne lue par la porte.

    La désignation au gel s'abstient déjà quand le plan est connu avant le gel
    (#2487) ; ce chemin couvre le report tardif (relevé de l'exécuteur, ou gel
    fait par un autre appelant) : sans lui, une désignation du nouveau gel
    resterait dans la colonne et `progress` attendrait un verdict qui n'a pas
    lieu d'être.

    La marque porte aussi les **sièges du gel précédent sans verdict** (B/P1 de
    dev1) : le report ne reporte que des verdicts, pas une attente. Le hook les
    filtre sur la colonne d'avant le gel ; ici, la sélection du nouveau gel a
    déjà pu retirer ces noms, donc on les reprend de la marque effective du gel
    précédent (un siège retiré à la main n'est pas distinguable — la marque est
    la source automatique). Une marque `review-designation-carried` est écrite
    quand la dernière marque du gel n'est pas un report qui porte déjà ces noms
    (report après une désignation `force`, ou report tardif qui complète des
    sièges) : elle devient l'effective et la colonne la suit. La marque rend la
    reprise et la lecture (`ameesh work reviewers`) idempotentes."""
    from . import review_designation_activation as activation
    freeze_id = int(freeze.get("id") or 0)
    names = []
    for mark, _reviewed in plan:
        name = str(mark.get("actor") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return
    payload = {"state": "carried", "carried": out.get("carried") or []}
    extra = activation._previous_designees(tx, int(lot), freeze_id)
    note = carried_mark_note(int(lot), freeze, payload, extra_reviewers=extra)
    wanted = carried_mark_of(note) or {}
    wanted_names = [str((reviewer or {}).get("name") or "").strip()
                    for reviewer in wanted.get("reviewers") or ()]
    wanted_names = [name for name in wanted_names if name]
    effective = activation._effective_mark(
        tx, int(lot), freeze_id, since_ts=float(freeze.get("at_ts") or 0.0))
    current_names = []
    if effective is not None and effective[1] == "carried":
        current_names = [str((reviewer or {}).get("name") or "").strip()
                         for reviewer in (effective[2].get("reviewers") or ())]
        current_names = [name for name in current_names if name]
    if effective is None or effective[1] != "carried" or current_names != wanted_names:
        # #2487 : un report tardif qui aboutit après une vraie désignation
        # (repli `force`) écrit une marque plus récente ; la colonne et la
        # lecture suivent alors le report, pas la sélection supplantée. Les
        # sièges du gel précédent sans verdict restent désignés, comme au gel ;
        # la colonne a déjà suivi la sélection, elle ne filtre donc pas.
        storage.of(tx).work.note(int(lot), str(row.get("state") or ""), note,
                                 work.SYSTEM_SENDER)
    activation._sync_reviewers(
        tx, dict(row), freeze, wanted_names,
        text="reviewers kept from carried verdicts at freeze #%d: %s (#2442/#2487)"
             % (freeze_id, ", ".join(wanted_names)))


def describe(result: dict) -> str:
    """Une ligne lisible du résultat de `carry` (CLI, journal de l'exécuteur)."""
    kind = result.get("result")
    if kind in ("carried", "would-carry"):
        return "verdicts carried (alignment on %s@%s): %s — no re-review due" % (
            result.get("target"), _norm(result.get("tip"))[:12],
            ", ".join("%s %s from %s" % (e["verdict"], e["actor"], e["from"][:12])
                      for e in result.get("carried") or ()))
    if kind == "not-carried":
        return "verdicts not carried: %s" % "; ".join(
            "%s: %s" % (e["reviewed"][:12], e["why"]) for e in result.get("refused") or ())
    if kind in ("validated", "would-validate"):
        return "carried review quorum validated on the frozen head (#4423)"
    return "verdicts not carried (%s): %s" % (kind, result.get("detail") or "")


def _carried_pending(db, lot: int, freeze: dict) -> bool:
    """Un report planifié au gel n'a pas eu lieu : faut-il désigner ?

    La désignation au gel s'est abstenue sur le plan du report (#2487) et a
    laissé la marque `review-designation-carried` ; si le report refuse ensuite
    (cible déplacée, dépôt absent), personne n'est désigné. Vrai quand cette
    marque est **effective** (la dernière du gel, aucune sélection réelle ne la
    remplace), que ses verdicts ne sont pas déjà sur la tête gelée, et qu'aucun
    échec de désignation n'a déjà été constaté pour ce gel (sinon la passe
    suivante répéterait la même lecture sans rien changer)."""
    from . import review_designation_activation as activation
    lot = int(lot)
    freeze_id = int(freeze.get("id") or 0)
    try:
        effective = activation._effective_mark(
            db, lot, freeze_id, since_ts=float(freeze.get("at_ts") or 0.0))
        if effective is None or effective[1] != "carried":
            return False
        carried = effective[2].get("carried") or ()
        if carried:
            on_head = {_key(mark.get("actor")) for mark in work.milestones(db, lot, None)
                       if mark.get("kind") == "verdict"
                       and _same(mark.get("sha"), freeze.get("sha"))}
            if all(_key(entry.get("actor")) in on_head for entry in carried):
                return False
        prefix = "%s%d:%d:" % (activation.UNAVAILABLE_PREFIX, lot, freeze_id)
        if storage.of(db).work.events_noted(prefix, 0.0, ids=[lot]):
            return False
        return True
    except Exception:  # noqa: BLE001 - lecture impossible : rien à réparer
        return False


def run_pass(cfg, db, *, dry_run: bool = False, limit: int = MAX_LOTS) -> list[dict]:
    """Un passage sur les lots ouverts dont la tête gelée attend un verdict
    qu'une tête précédente porte (`pending`). Le tri sans git écarte tous les
    autres : seuls ces lots coûtent une récupération et deux fusions d'essai.

    #4834 : le même passage **retente** la transition de quorum d'un report déjà
    écrit mais non appliqué (porte illisible un instant, lot resté en `qa`).
    Le report a marqué ses verdicts « décidés » ; sans cette reprise, plus aucun
    écrivain ne revient et le lot ne passe jamais en `validated`. Le tri est
    encore sans git (marques en base) : seuls les lots qui portent un `ok`
    reporté sur leur dernière tête gelée coûtent une lecture de porte.
    Ne lève jamais pour un lot."""
    st = storage.of(db)
    rows = [r for r in st.work.open_for_sweep(limit)
            if r.get("state") not in tuple(work.MERGED_STATES) + ("closed",)]
    if not rows:
        return []
    marks: dict = {}
    for mark in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        marks.setdefault(int(mark["work_item_id"]), []).append(mark)
    # #4834 : seuls les lots dont une transition de quorum reportée a déjà
    # échoué faute de lecture sont retentés (marque `carried quorum pending:`),
    # dans la limite d'essais de **leur** gel. Une porte lisiblement fermée
    # n'est pas une panne : elle ne se retente pas, et une panne qui dure laisse
    # le lot en `qa` (visible par l'état) plutôt que de relire la porte à
    # chaque passage.
    pending_notes: dict[int, list[str]] = {}
    for entry in st.work.events_noted(PENDING, 0.0, ids=[int(r["id"]) for r in rows]):
        pending_notes.setdefault(int(entry["work_item_id"]), []).append(
            str(entry.get("note") or ""))
    wanted = []
    pending_quorum = []
    host = _host(db)
    for row in rows:
        lot = int(row["id"])
        lot_marks = marks.get(lot, [])
        freeze = plan_git.latest_freeze(lot_marks)
        if not freeze:
            continue
        if pending(lot_marks, freeze.get("sha")):
            wanted.append(lot)
        label = "%s freeze #%d host %s " % (
            PENDING, int(freeze.get("id") or 0), host)
        attempts = sum(1 for note in pending_notes.get(lot, ())
                       if note.startswith(label))
        if (attempts < PENDING_ATTEMPTS
                and str(row.get("state") or "") == "qa"
                and _carried_ok_on_freeze(lot_marks, freeze)
                and not _carried_blocked_on_freeze(lot_marks, freeze)):
            pending_quorum.append((lot, freeze))
    if not wanted and not pending_quorum:
        return []
    out = []
    handled: set = set()
    if wanted:
        notes = _outcomes(db, wanted)
        fetched: dict = {}
        for lot in wanted:
            result = carry(cfg, db, lot, dry_run=dry_run, notes=notes.get(lot), fetched=fetched)
            if result.get("result") != "idle":
                out.append(result)
            if result.get("result") in ("carried", "busy", "moved"):
                # `carried` : `_write` a déjà tenté la transition de quorum ;
                # `busy`/`moved` : un autre hôte écrit ou le gel a bougé, la
                # reprise attend le passage suivant au lieu de courir après.
                handled.add(lot)
            if dry_run or result.get("result") not in ("not-carried", "error",
                                                       "no-repository", "idle"):
                continue
            # #2487 : le gel s'est abstenu sur un plan de report qui n'a pas abouti.
            # Personne n'est désigné ; on rejoue la désignation réelle du gel, une
            # fois, sans toucher au reste. Le gel n'est pas re-déclaré.
            freeze = plan_git.latest_freeze(marks.get(lot, []))
            if not freeze or not _carried_pending(db, lot, freeze):
                continue
            try:
                from . import review_designation_activation as activation
                fixed = activation.designate(cfg, db, lot, force=True)
                result["designation"] = str((fixed or {}).get("state") or "")
            except Exception as exc:  # noqa: BLE001 - la passe suivante retente
                result["designation"] = "error: %s" % (type(exc).__name__,)
    for lot, freeze in pending_quorum:
        if lot in handled:
            continue
        if dry_run:
            out.append({"lot": lot, "result": "would-validate",
                        "detail": "carried review quorum on the frozen head (#4423)"})
            continue
        result = promote_carried(db, lot, freeze=freeze)
        if result["result"] == "validated":
            out.append({"lot": lot, "result": "validated",
                        "detail": "carried review quorum on the frozen head (#4423)"})
        # `pending` est déjà dit une fois au journal du lot par
        # `promote_carried` ; le passage suivant le retente sans bruit.
    return out
