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

**Adaptation (#5042)** — canon §2 : « conflit mécanique, adaptation de test :
aucun avis ». Quand le critère ci-dessus refuse, `adapted` prouve une seconde
forme : la fusion de la nouvelle tête et la fusion d'essai de la tête relue ne
diffèrent que par des fichiers de test modifiés ou ajoutés (la reconnaissance du
véhicule, `packet_reviews` : le plan de CI `tests/parts/` n'en est pas), et par
des morceaux en conflit résolus en **appliquant chaque côté à sa place** sur la
base, sans aucune ligne nouvelle et chaque bloc contigu : deux ajouts juxtaposés,
ou un ajout voisin d'une ligne modifiée par l'autre côté. Deux côtés qui changent
la même ligne de la base, ou deux blocs entrelacés, restent un jugement. Chaque commit nouveau reste prouvé : fusion de même nature,
commit limité aux tests, ou reprise propre d'un commit relu. Un lot fait de
tests seuls n'en profite pas : ses tests sont son contenu. **Seul un avis
favorable se reporte ainsi** : une adaptation peut répondre au blocage, son
auteur juge donc la nouvelle tête, redésigné normalement.

**Sorties générées (#7323)** — une liste fermée versionnée sur la cible
(`.ameesh/generated-files.json`) nomme les sorties et leurs contrôles.
Un delta linéaire limité à ces sorties conserve les avis favorables si chaque
contrôle réussit sur un checkout jetable de la tête gelée. Les sources et les
générateurs restent relus ; contrôle rouge ou non concluant, code mêlé,
changement transitoire ou mode spécial : refus. Voir `docs/VERDICTS-GENERES.md`.

**Renumérotation (#7000)** — constat nexlink #6953 : le pilote échange deux
réservations, l'auteur renomme sa migration 784 en 783 et retouche les renvois
au numéro. C'est une troisième adaptation ; l'invariant prouvé, pour la
renumérotation `M` {ancien n° : nouveau n°} :

- I1 (portée) chaque fichier renommé F (ancien → nouveau) est un couple
  suppression/ajout d'un même répertoire, de même nom après le préfixe
  numérique, sous le répertoire de migrations déclaré, un compagnon déclaré
  (`radar.projects.<p>.migrations_companions`) ou un test adaptable ; F a été
  ajouté ou modifié par la tête relue depuis sa base avec la cible ;
- I2 (réservation) au moins une migration est renommée, et chaque nouveau
  numéro est réservé par le lot (`ameesh work migration`, L122) ;
- I3 (fonction) `M` donne un seul nouveau numéro par ancien, et deux anciens
  ne vont jamais au même nouveau ; les numéros se comparent en entiers ;
- I4 (contenu) une migration ou un compagnon renommé reste un fichier
  ordinaire de même mode, et son contenu est l'ancien où seules les
  occurrences en jeton entier de **son propre** ancien numéro deviennent son
  nouveau numéro (`_substitute`, `_own_mapping`) ;
- I5 (reste) hors des couples, ne changent que des tests adaptables (règle des
  tests) et des compagnons de la tête relue, fichiers ordinaires de même mode,
  retouchés de leur seul numéro — ou de `M` entier pour un registre sans
  numéro ; un fichier numéroté hors de `M` ne change pas ;
- I6 (historique) chaque commit nouveau respecte I1, I4 et I5 pas à pas (un
  contenu retouché puis rétabli reste refusé) ;
- I7 (arrivée) chaque nouveau numéro nomme une seule migration de l'arbre
  d'atterrissage.

Le report note « adaptation : renumérotation N→M ».

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
plus ancien. Un refus sur une tête que le **gel précédent** avait déjà
remplacée (#6720 : verdict antérieur à ce gel, sur une autre tête) est
journalisé mais ne rend pas le report partiel : la porte du gel précédent ne
le lisait déjà plus, il ne demande aucun siège.

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

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from typing import Mapping, Optional
from pathlib import Path, PurePosixPath

from . import merge_queue, plan_git, storage, work

#: préfixe de la note du jalon reporté et de sa ligne de journal
CARRIED = "verdict carried: alignment"
#: préfixe de la ligne de journal d'un réalignement qui n'en est pas un
NOT_CARRIED = "verdict not carried:"
#: préfixe de la marque de désignation gardée par un report : la désignation
#: au gel s'abstient et laisse la colonne `reviewers` aux relecteurs reportés
CARRIED_MARK_PREFIX = "review-designation-carried:"
#: #5042 : une adaptation peut répondre au blocage, son auteur juge la nouvelle tête
NOT_OK_ADAPTATION = "a blocking verdict is not carried across an adaptation"
#: #6720 : un refus sur une tête que le gel précédent avait déjà remplacée
SUPERSEDED = "superseded by the previous freeze"
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
        # #5481 : la marque écrite avant le report est une provision ; le
        # report accompli en écrit une autre, sans ce drapeau.
        "provisional": bool(plan.get("provisional")),
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


def _repo_path(path) -> str:
    """Un dépôt local fourni en chemin relatif (`--repo .`), résolu tôt.

    Le report lit ensuite ce chemin depuis le clone radar : un `"."` y
    désignerait le clone, pas le dépôt du gel. Les URL et chemins absolus sont
    rendus tels quels."""
    text = str(path or "").strip()
    if not text:
        return ""
    if os.path.isabs(text) or "://" in text or text.startswith("git@"):
        return text
    return os.path.abspath(text)


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


#: morceau en conflit : ajouts des deux côtés au même endroit essayés au plus
#: dans les deux ordres (borne de coût : 2 rendus par juxtaposition)
MAX_JUXTAPOSITIONS = 6
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


def _adaptable_test(path: str) -> bool:
    """Un fichier de test qu'on adapte sans avis : la reconnaissance du véhicule
    (#3518), hors plan de CI, qui décide quels modules tournent (#2792)."""
    from . import packet_reviews
    return (bool(packet_reviews._TEST.search(path))
            and not packet_reviews._CI_PLAN.search(path))


def _conflict_segments(blob: bytes) -> list | None:
    """Le fichier écrit par une fusion en conflit (style `diff3`), découpé : une
    ligne (`bytes`) hors conflit, `(côté gauche, base, côté droit)` pour un morceau
    en conflit. None si les marques ne se lisent pas exactement (marque d'une
    autre taille, morceau non fermé, base elle-même en conflit) : rien n'est alors
    prouvé."""
    segments: list = []
    left: list = []
    origin: list = []
    right: list = []
    side = None
    for line in blob.splitlines(keepends=True):
        bare = line.rstrip(b"\r\n")
        if side is None:
            if line.startswith(b"<<<<<<< "):
                left, origin, right = [], [], []
                side = left
            else:
                segments.append(line)
        elif line.startswith(b"<<<<<<< "):
            return None
        elif side is left and line.startswith(b"||||||| "):
            side = origin
        elif side is origin and bare == b"=======":
            side = right
        elif side is right and line.startswith(b">>>>>>> "):
            segments.append((left, origin, right))
            side = None
        else:
            side.append(line)
    return None if side is not None else segments


def _edits(origin: list, side: list) -> list:
    """Les changements d'un côté sur la base : `(début, fin, lignes rendues)`, en
    positions de la base (début = fin : un ajout devant cette ligne)."""
    matcher = difflib.SequenceMatcher(None, origin, side, autojunk=False)
    return [(start, end, side[first:last])
            for tag, start, end, first, last in matcher.get_opcodes() if tag != "equal"]


def _hunk_renderings(left: list, origin: list, right: list) -> list | None:
    """Les rendus mécaniques d'un morceau en conflit : la base, les changements des
    deux côtés appliqués chacun à sa place, chaque bloc contigu. Deux ajouts au même
    endroit se juxtaposent dans un ordre ou dans l'autre ; un ajout voisin d'une
    ligne modifiée par l'autre côté garde les deux (cas #4975). None si les côtés
    touchent une même ligne de la base (un vrai désaccord, un jugement), ou si les
    ordres à essayer dépassent `MAX_JUXTAPOSITIONS`."""
    edits_left, edits_right = _edits(origin, left), _edits(origin, right)
    for start, end, _lines in edits_left:
        for other, stop, _rendered in edits_right:
            if (max(start, other) < min(end, stop)
                    or (start == end and other < start < stop)
                    or (other == stop and start < other < end)):
                return None
    ties = {start for start, end, _lines in edits_left if start == end} & {
        start for start, end, _lines in edits_right if start == end}
    if len(ties) > MAX_JUXTAPOSITIONS:
        return None
    renderings: list = [[]]
    cursor = 0
    # Un ajout (fin = début) passe devant la modification qui commence au même
    # endroit : il est écrit devant cette ligne de la base.
    edits = sorted(edits_left + edits_right, key=lambda edit: edit[:2])
    index = 0
    while index < len(edits):
        start, end, lines = edits[index]
        kept = origin[cursor:start]
        if start in ties and start == end:
            again = edits[index + 1][2]
            index += 1
            renderings = [done + kept + order for done in renderings
                          for order in (lines + again, again + lines)]
        else:
            renderings = [done + kept + lines for done in renderings]
        cursor = max(cursor, end)
        index += 1
    return [done + origin[cursor:] for done in renderings]


def composed_hunks(conflicted: bytes, resolved: bytes) -> int | None:
    """Le nombre de morceaux en conflit de `conflicted`, si `resolved` en est la
    résolution mécanique : tout le reste à l'octet près, et chaque morceau égal à
    l'un de ses rendus (`_hunk_renderings`). None sinon : une ligne ajoutée, retirée,
    retouchée, ou deux blocs entrelacés (cx2 #172928 : l'ordre peut changer le
    comportement) sont un jugement."""
    segments = _conflict_segments(conflicted)
    if not segments:
        return None
    expected = resolved.splitlines(keepends=True)
    position = hunks = 0
    for segment in segments:
        if not isinstance(segment, tuple):
            if expected[position:position + 1] != [segment]:
                return None
            position += 1
            continue
        renderings = _hunk_renderings(*segment)
        if not renderings:
            return None
        size = len(renderings[0])
        if expected[position:position + size] not in renderings:
            return None
        position += size
        hunks += 1
    return hunks if hunks and position == len(expected) else None


@dataclass(frozen=True)
class Renumbering:
    """#7000 : ce qu'une renumérotation de migration peut toucher sans avis — le
    répertoire de migrations déclaré du dépôt, ses compagnons déclarés
    (`radar.projects.<p>.migrations_companions` : rollback, registre) — et les
    numéros que le lot réserve (`ameesh work migration`, L122)."""

    directory: str
    reserved: frozenset
    companions: tuple = ()
    #: les chemins que la tête relue ajoute ou modifie depuis sa base avec la
    #: cible (`adapted` les fixe) : seuls ceux-là se renumérotent — une migration
    #: de la cible, jamais relue, n'est pas au lot (B1 qa-01 #213399). None :
    #: aucun chemin prouvé, aucune renumérotation
    owned: frozenset | None = None

    def zone(self, path: str) -> str:
        """`migration`, `companion`, `test` ou "" : où vit `path`."""
        if os.path.dirname(path) == self.directory:
            return "migration"
        if any(path == c or path.startswith(c + "/") for c in self.companions):
            return "companion"
        return "test" if _adaptable_test(path) else ""


#: un fichier numéroté : le numéro littéral, puis le reste du nom (`_x.sql`)
_NUMBERED = re.compile(r"^(\d+)(_.+)$")


def renumbering_of(cfg, row: Mapping, repo: str) -> Renumbering | None:
    """La renumérotation admissible pour le lot `row` dans le clone `repo`, ou
    None : dépôt sans répertoire de migrations déclaré, ou aucun numéro réservé
    dans ce dépôt — rien n'est alors une renumérotation (fail-closed)."""
    from . import migrations, radar
    slug = migrations.repo_key(plan_git._repo_slug(repo) or row.get("migration_repo") or "")
    if not slug:
        return None
    layout = migrations.layout_for(slug, cfg)
    if layout is None or not layout.directory:
        return None
    reserved = frozenset(
        int(r["number"]) for r in work.reserved_migrations(dict(row))
        if migrations.repo_key(r["repo"] or row.get("migration_repo") or "") == slug)
    if not reserved:
        return None
    companions: tuple = ()
    try:
        for project in radar.settings_of(cfg).projects.values():
            # B2 qa-01 #213399 : `github` est facultatif, déduit de l'origine
            if migrations.repo_key(radar.github_slug(project) or "") == slug:
                companions = tuple(project.migrations_companions)
    except Exception:  # noqa: BLE001 - réglages illisibles : aucun compagnon
        companions = ()
    return Renumbering(layout.directory.strip("/"), reserved, companions)


def _substitute(blob: bytes, mapping: Mapping) -> bytes:
    """`blob`, chaque ancien numéro de `mapping` remplacé par le nouveau en jeton
    entier (ni lettre ni chiffre de part et d'autre), en une passe : un numéro
    rendu n'est jamais remplacé à nouveau."""
    if not mapping:
        return blob
    olds = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(rb"(?<![0-9A-Za-z])(" + b"|".join(re.escape(o.encode()) for o in olds)
                         + rb")(?![0-9A-Za-z])")
    return pattern.sub(lambda m: mapping[m.group(1).decode()].encode(), blob)


def _renamed_pairs(changes: Mapping, renumber: Renumbering) -> list:
    """Les couples suppression/ajout d'un même répertoire dont seul le numéro du
    nom change : `[(ancien chemin, nouveau chemin, ancien n°, nouveau n°, zone)]`.
    Un chemin supprimé qui a zéro ou plusieurs ajouts possibles n'en forme aucun."""
    added: dict = {}
    for path, status in changes.items():
        match = _NUMBERED.match(os.path.basename(path)) if status == "A" else None
        if match:
            added.setdefault((os.path.dirname(path), match.group(2)), []).append(
                (path, match.group(1)))
    pairs = []
    for path, status in sorted(changes.items()):
        match = _NUMBERED.match(os.path.basename(path)) if status == "D" else None
        zone = renumber.zone(path) if match else ""
        if not zone:
            continue
        found = [(new, number) for new, number in
                 added.get((os.path.dirname(path), match.group(2)), ())
                 if number != match.group(1)]
        if len(found) == 1:
            pairs.append((path, found[0][0], match.group(1), found[0][1], zone))
    return pairs


def _renumbered(changes: Mapping, renumber: Renumbering | None, before, after) -> dict:
    """#7000 : la part de `changes` (base → arbre) qui n'est qu'une renumérotation.

    Rend `{mapping, paths, why}` : `mapping` {ancien n° : nouveau n°} (vide si aucune),
    `paths` les chemins couverts, `why` un refus ; `before`/`after` lisent un
    chemin dans la base et dans l'arbre (`_regular` : `(mode, contenu)` d'un
    fichier ordinaire, None sinon — un lien symbolique ne se renumérote pas,
    et le mode ne change pas, B cx2 #213476). Conditions, toutes requises :
    au moins un fichier du répertoire de migrations renommé du seul préfixe
    numérique ; chaque nouveau numéro réservé par le lot ; un seul nouveau numéro
    par ancien ; le contenu de chaque migration ou compagnon renommé (ou compagnon
    retouché) **égal octet pour octet** à l'ancien, numéros remplacés en jetons
    entiers (`_substitute`). Un test renommé suit la règle des tests (adaptable)."""
    out = {"mapping": {}, "paths": set(), "why": ""}
    if renumber is None or renumber.owned is None:
        return out
    pairs = [pair for pair in _renamed_pairs(changes, renumber) if pair[0] in renumber.owned]
    mapping: dict = {}
    news: dict = {}               # I3 : ancien → nouveau, en entiers (0784 = 784)
    for _old, _new, number, renamed, zone in pairs:
        if zone != "migration":
            continue
        if int(renamed) not in renumber.reserved:
            out["why"] = ("renumbering %s→%s: %s is not the lot's reservation (%s)"
                          % (number, renamed, renamed,
                             ", ".join(str(n) for n in sorted(renumber.reserved))))
            return out
        if (mapping.setdefault(number, renamed) != renamed
                or news.setdefault(int(number), int(renamed)) != int(renamed)):
            out["why"] = "renumbering %d to two numbers" % int(number)
            return out
    # B2 dev3 #213435 : deux anciens numéros vers un même nouveau laisseraient
    # deux migrations de ce numéro à l'arrivée
    taken: dict = {}
    for _old, new, number, renamed, zone in pairs:
        if zone == "migration" and mapping.get(number) == renamed:
            # B dev01 #213475 : `0783` et `783` sont le même numéro
            if taken.setdefault(int(renamed), number) != number:
                out["why"] = ("renumbering %s and %s to the same number %s"
                              % (taken[int(renamed)], number, renamed))
                return out
    if not mapping:
        return out
    paths: set = set()
    for old, new, number, renamed, zone in pairs:
        if mapping.get(number) != renamed:
            continue                  # couple hors de la renumérotation : jugé ailleurs
        if zone != "test":
            old_blob, new_blob = before(old), after(new)
            if (old_blob is None or new_blob is None or old_blob[0] != new_blob[0]
                    or _substitute(old_blob[1], {number: renamed}) != new_blob[1]):
                out["why"] = ("%s is not %s with only %s→%s changed"
                              % (new, old, number, renamed))
                return out
        paths.update((old, new))
    for path, status in sorted(changes.items()):
        if (status == "M" and path not in paths and path in renumber.owned
                and renumber.zone(path) == "companion"):
            old_blob, new_blob = before(path), after(path)
            if (old_blob is None or new_blob is None or old_blob[0] != new_blob[0]
                    or _substitute(old_blob[1], _own_mapping(path, mapping)) != new_blob[1]):
                out["why"] = "%s is changed beyond the renumbering" % path
                return out
            paths.add(path)
    out.update(mapping=mapping, paths=paths)
    return out


#: les modes d'un fichier ordinaire dans un arbre git
_REGULAR_MODES = ("100644", "100755")


def _regular(run, rev: str, path: str) -> tuple | None:
    """`(mode, contenu)` de `path` dans `rev` si c'est un fichier ordinaire, None
    sinon (absent, lien symbolique, sous-module, illisible). `run(*args)` lance
    git et rend le processus."""
    listed = run("ls-tree", "-z", rev, "--", path)
    if listed.returncode != 0:
        return None
    entry = listed.stdout.split(b"\0", 1)[0]
    meta, _tab, name = entry.partition(b"\t")
    fields = meta.split()
    if len(fields) != 3 or name.decode("utf-8", "surrogateescape") != path:
        return None
    mode, kind, sha = (field.decode() for field in fields)
    if kind != "blob" or mode not in _REGULAR_MODES:
        return None
    proc = run("cat-file", "blob", sha)
    return (mode, proc.stdout) if proc.returncode == 0 else None


def _own_mapping(path: str, mapping: Mapping) -> dict:
    """B dev3 #213504 : le seul remplacement permis dans `path`. Un fichier
    numéroté ne change que son propre numéro (ancien ou déjà nouveau) : le
    `SELECT 784` d'une migration 785 reste 784 ; un fichier numéroté hors de la
    renumérotation ne change pas. Un fichier sans numéro (registre déclaré)
    suit le remplacement entier."""
    match = _NUMBERED.match(os.path.basename(path))
    if not match:
        return dict(mapping)
    number = match.group(1)
    if number in mapping:
        return {number: mapping[number]}
    olds = [old for old, new in mapping.items() if new == number]
    # B qa-01 #213550 : un fichier numéroté hors de la renumérotation (une autre
    # migration du lot) ne change pas, même retouché puis rétabli
    return {olds[0]: number} if len(olds) == 1 else {}


def _mapping_text(mapping: Mapping) -> str:
    return ", ".join("%s→%s" % item for item in sorted(mapping.items()))



def _merge_adaptation(repo: str, left: str, right: str, *, head: str | None = None,
                      tree: str | None = None, renumber: Renumbering | None = None) -> dict:
    """La fusion `left` + `right` comparée à son résultat attendu : l'arbre `tree`
    (une fusion de l'historique), ou la fusion propre de `head` dans `left`.
    Rend `{ok, why, tests, hunks, renumbered}` : seuls des fichiers de test
    modifiés ou ajoutés, des conflits résolus mécaniquement et une renumérotation
    de migration réservée (`_renumbered`, #7000) peuvent différer."""
    from . import radar
    refused = {"ok": False, "tests": 0, "hunks": 0, "renumbered": {}}
    objects = plan_git._out(repo, "rev-parse", "--path-format=absolute",
                            "--git-path", "objects")
    if not objects:
        return dict(refused, why="the object directory cannot be read")
    scratch = tempfile.mkdtemp(prefix="ameesh-carry-")

    def merge(one: str, other: str):
        return radar._scratch_git(repo, objects, scratch,
                                  "-c", "attr.tree=%s" % one,
                                  "-c", "merge.conflictStyle=diff3",
                                  "merge-tree", "--write-tree", "-z", "--name-only",
                                  "--no-messages", one, other)

    def blob(root: str, path: str) -> bytes | None:
        proc = radar._scratch_git(repo, objects, scratch, "cat-file", "blob",
                                  "%s:%s" % (root, path))
        return proc.stdout if proc.returncode == 0 else None

    try:
        trial = merge(left, right)
        lines = radar._lines(trial)
        if trial.returncode not in (0, 1) or not lines:
            return dict(refused, why="the trial merge cannot be computed")
        base = lines[0].strip()
        conflicts = set(lines[1:]) if trial.returncode == 1 else set()
        if tree is None:
            landed = merge(left, head)
            lines = radar._lines(landed)
            if landed.returncode != 0 or not lines:
                return dict(refused, why="the frozen head does not merge cleanly")
            tree = lines[0].strip()
        diff = radar._scratch_git(repo, objects, scratch, "diff-tree", "-r", "--no-renames",
                                  "--name-status", "-z", base, tree)
        if diff.returncode != 0:
            return dict(refused, why="the two merges cannot be compared")
        fields = radar._lines(diff)
        changes = dict(zip(fields[1::2], fields[0::2]))
        unresolved = sorted(path for path in conflicts if path not in changes)
        if unresolved:
            return dict(refused, why="conflict left as is in %s" % unresolved[0])
        def regular(root: str, path: str):
            return _regular(lambda *args: radar._scratch_git(repo, objects, scratch, *args),
                            root, path)

        renamed = _renumbered(changes, renumber, lambda path: regular(base, path),
                              lambda path: regular(tree, path))
        if renamed["why"]:
            return dict(refused, why=renamed["why"])
        if renamed["mapping"]:
            # I8 : chaque nouveau numéro nomme une seule migration de l'arbre
            # d'atterrissage (un fichier de la cible au même numéro en ferait deux)
            listed = radar._scratch_git(repo, objects, scratch, "ls-tree", "-z", "--name-only",
                                        tree, "--", renumber.directory + "/")
            if listed.returncode != 0:
                return dict(refused, why="the landed migrations cannot be listed")
            counts: dict = {}
            for name in listed.stdout.decode("utf-8", "surrogateescape").split("\0"):
                match = _NUMBERED.match(os.path.basename(name)) if name else None
                if match:
                    counts[int(match.group(1))] = counts.get(int(match.group(1)), 0) + 1
            for new in sorted({int(value) for value in renamed["mapping"].values()}):
                if counts.get(new, 0) != 1:
                    return dict(refused, why="migration number %d names %d files once landed"
                                % (new, counts.get(new, 0)))
        tests = hunks = 0
        for path, status in sorted(changes.items()):
            if path in renamed["paths"]:
                continue
            if status in ("M", "A") and _adaptable_test(path):
                tests += 1
                continue
            if status != "M" or path not in conflicts:
                return dict(refused, why="%s differs outside a test or a conflict" % path)
            before, after = blob(base, path), blob(tree, path)
            found = (composed_hunks(before, after)
                     if before is not None and after is not None else None)
            if not found:
                return dict(refused, why="conflict in %s is not resolved by keeping both "
                            "sides' changes and nothing else" % path)
            hunks += found
        return {"ok": True, "why": "", "tests": tests, "hunks": hunks,
                "renumbered": renamed["mapping"]}
    except radar.GitError as exc:
        return dict(refused, why=str(exc)[:200])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _adapted_history(repo: str, reviewed: str, frozen: str, tip: str, *,
                     renumber: Renumbering | None = None, mapping: Mapping | None = None
                     ) -> tuple:
    """Chaque commit que `frozen` apporte, hors tête relue et cible, est une
    adaptation : fusion dont l'arbre est celle de ses parents à une adaptation
    près, commit limité à des fichiers de test modifiés ou ajoutés, commit de la
    renumérotation `mapping` (`_renumber_commit`, #7000), ou reprise
    propre d'un commit relu. Tout autre contenu est refusé, même retiré ensuite :
    il resterait dans l'historique (R1 cx1). Rend `(oui, raison)`."""
    new = _lines_of(repo, "rev-list", "--topo-order", "--reverse", frozen, "--not",
                    reviewed, tip)
    if new is None:
        return False, "the new commits cannot be listed"
    if len(new) > MAX_NEW_COMMITS:
        return False, "too many new commits to prove (%d)" % len(new)
    sources = None
    for commit in new:
        parents = _lines_of(repo, "rev-list", "--parents", "-n", "1", commit)
        tree = plan_git._out(repo, "rev-parse", commit + "^{tree}")
        if not parents or not tree:
            return False, "commit %s cannot be read" % commit[:12]
        parents = parents[1:]
        if len(parents) == 2:
            merge = _merge_adaptation(repo, parents[0], parents[1], tree=tree,
                                      renumber=renumber)
            if not merge["ok"]:
                return False, "merge %s: %s" % (commit[:12], merge["why"])
            continue
        if len(parents) != 1:
            return False, "commit %s has %d parents" % (commit[:12], len(parents))
        fields = _name_status(repo, parents[0], commit)
        if fields is None:
            return False, "commit %s cannot be read" % commit[:12]
        if fields and all(status in ("M", "A") and _adaptable_test(path)
                          for path, status in fields.items()):
            continue
        if mapping and _renumber_commit(repo, parents[0], commit, fields, renumber, mapping):
            continue
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
        if not source or not _replays(repo, parents[0], source, tree):
            return False, ("commit %s is neither a test adaptation nor the clean replay of "
                           "a reviewed commit" % commit[:12])
    return True, "%d commit(s) proved" % len(new)


def _renumber_commit(repo: str, parent: str, commit: str, fields: Mapping,
                     renumber: Renumbering, mapping: Mapping) -> bool:
    """Un commit de l'historique n'est-il qu'un pas de la renumérotation `mapping`
    (prouvée de bout en bout par `_renumbered`) ? Chaque chemin est un test
    adaptable, un couple renommé du seul numéro, le fichier renuméroté ou un
    compagnon, et son contenu ne diffère du précédent **que par ces numéros**
    (remplacés des deux côtés, l'égalité stricte reste celle du bout en bout) :
    renommer puis retoucher les renvois en deux commits reste une adaptation."""
    def run(*args):
        return plan_git._git(repo, *args)

    def same(old: str, new: str) -> bool:
        before, after = _regular(run, parent, old), _regular(run, commit, new)
        own = _own_mapping(old, mapping)
        return (before is not None and after is not None and before[0] == after[0]
                and _substitute(before[1], own) == _substitute(after[1], own))

    # B1 dev3 #213435 : seuls un chemin de la tête relue et le nouveau chemin
    # attendu d'un tel chemin se retouchent ; un fichier de la cible qui porte
    # le nouveau numéro n'est pas au lot, même retouché puis rétabli
    owned = renumber.owned or frozenset()
    expected = set(owned)
    for path in owned:
        match = _NUMBERED.match(os.path.basename(path))
        if match and match.group(1) in mapping:
            expected.add(os.path.join(os.path.dirname(path),
                                      mapping[match.group(1)] + match.group(2)))
    pairs = {}
    for old, new, number, renamed, zone in _renamed_pairs(fields, renumber):
        if mapping.get(number) == renamed and old in owned:
            pairs[old] = pairs[new] = (old, new, zone)
    for path, status in fields.items():
        zone = renumber.zone(path)
        if path in pairs:
            old, new, zone = pairs[path]
            if path == new and zone != "test" and not same(old, new):
                return False
            continue
        if status in ("M", "A") and zone == "test":
            continue
        if status == "M" and zone in ("migration", "companion") and path in expected:
            if not same(path, path):
                return False
            continue
        return False
    return bool(fields)


def _name_status(repo: str, old: str, new: str) -> dict | None:
    """{chemin: statut} entre deux commits, renommages dépliés ; None si illisible."""
    proc = plan_git._git(repo, "diff-tree", "-r", "--no-renames", "--name-status", "-z",
                         old, new)
    if proc.returncode != 0:
        return None
    fields = [f for f in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if f]
    return dict(zip(fields[1::2], fields[0::2]))


def adapted(repo: str, reviewed: str, frozen: str, tip: str, *,
            renumber: Renumbering | None = None) -> tuple:
    """#5042 : `frozen` n'est-elle qu'une adaptation de `reviewed` au sens du
    canon §2 (« conflit mécanique, adaptation de test : aucun avis ») ? Rend
    `(oui, raison)` ; le critère est dans la doc du module. Un clone superficiel
    ne prouve rien (N2 #2486)."""
    if not _complete_history(repo):
        return False, "complete Git history required; shallow clone refused"
    if _same(reviewed, frozen):
        return False, "same head"
    if renumber is not None:
        base = plan_git._out(repo, "merge-base", reviewed, tip)
        own = _name_status(repo, base, reviewed) if base else None
        renumber = replace(renumber, owned=frozenset(
            path for path, status in (own or {}).items() if status in ("A", "M"))
            if own is not None else None)
    merge = _merge_adaptation(repo, tip, reviewed, head=frozen, renumber=renumber)
    if not merge["ok"]:
        return False, merge["why"]
    if not merge["tests"] and not merge["hunks"] and not merge["renumbered"]:
        return False, "no test adaptation and no mechanical conflict"
    if merge["tests"]:
        base = plan_git._out(repo, "merge-base", reviewed, tip)
        own = _name_status(repo, base, reviewed) if base else None
        if own is None:
            return False, "the reviewed delta cannot be read"
        if all(_adaptable_test(path) for path in own):
            return False, "the lot is made of tests: they are its reviewed content"
    history, why = _adapted_history(repo, reviewed, frozen, tip, renumber=renumber,
                                    mapping=merge["renumbered"])
    if not history:
        return False, why
    if merge["renumbered"]:
        return True, ("adaptation : renumérotation %s (migration number reserved by the lot, "
                      "canon §2): renamed files differ only by that number, %d test file(s) "
                      "changed, %d conflict hunk(s) keeping both sides; %s"
                      % (_mapping_text(merge["renumbered"]), merge["tests"], merge["hunks"],
                         why))
    return True, ("test or mechanical adaptation (canon §2): %d test file(s) changed, "
                  "%d conflict hunk(s) keeping both sides, no other line; %s"
                  % (merge["tests"], merge["hunks"], why))


# #7323: the declaration is read from the integrated target, never the caller's
# checkout or an unreviewed candidate. Inputs and checkers stay unchanged R→F.
GENERATED_FILES = ".ameesh/generated-files.json"
GENERATED_CHECK_TIMEOUT = 60


def _generated_path(value) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value
            and not PurePosixPath(value).is_absolute()
            and all(part not in ("", ".", "..", ".git") for part in value.split("/")))


def generated_adapted(repo: str, reviewed: str, frozen: str, *,
                      declaration_ref: str | None = None, runtime_repo: str | None = None) -> tuple:
    """Only declared generated outputs, unchanged inputs, successful frozen check.

    This deliberately requires descent from R and checks every intervening
    commit. Mixed code, transient code edits, merges, modes and declarations
    changed in the same delta cannot acquire inherited opinions.
    """
    if not _complete_history(repo):
        return False, "generated proof requires complete history"
    if plan_git._git(repo, "merge-base", "--is-ancestor", reviewed, frozen).returncode:
        return False, "generated proof requires descent from the reviewed head"
    raw = plan_git._out(repo, "show", (declaration_ref or reviewed) + ":" + GENERATED_FILES)
    try:
        manifest = json.loads(raw or "null")
        files = manifest["files"]
        if manifest["version"] != 1 or not isinstance(files, dict) or not files:
            raise ValueError("invalid declaration")
        for path, rule in files.items():
            if (not _generated_path(path) or path == GENERATED_FILES
                    or not isinstance(rule, dict)
                    or not isinstance(rule.get("check"), list) or not rule["check"]
                    or not all(isinstance(arg, str) and arg for arg in rule["check"])):
                raise ValueError("invalid rule")
            if "runtime" in rule and (not _generated_path(rule["runtime"])
                    or not isinstance(rule.get("locks"), list) or not rule["locks"]
                    or not all(_generated_path(lock) for lock in rule["locks"])):
                raise ValueError("invalid runtime declaration")
    except (ValueError, TypeError, KeyError):
        return False, "generated declaration absent or invalid on trusted head"
    runtime_repo = runtime_repo or repo
    changed = _name_status(repo, reviewed, frozen)
    if not changed or any(path not in files or status not in ("A", "M")
                          for path, status in changed.items()):
        return False, "delta is not limited to declared generated files"
    commits = _lines_of(repo, "rev-list", "--reverse", reviewed + ".." + frozen)
    if not commits or len(commits) > MAX_NEW_COMMITS:
        return False, "generated history unavailable or too large"
    for commit in commits:
        parents = _lines_of(repo, "rev-list", "--parents", "-n", "1", commit)
        if not parents or len(parents) != 2:
            return False, "generated history must be linear"
        delta = _name_status(repo, parents[1], commit)
        if delta is None or any(path not in files or status not in ("A", "M")
                                for path, status in delta.items()):
            return False, "generated history contains another change"
    for path in files:
        before = plan_git._out(repo, "ls-tree", reviewed, "--", path)
        after = plan_git._out(repo, "ls-tree", frozen, "--", path)
        if (not after or after.split()[0] != "100644"
                or (before and before.split()[0] != "100644")):
            return False, "generated output must be a regular non-executable file"
    try:
        with tempfile.TemporaryDirectory(prefix="ameesh-generated-") as tmp:
            tree = str(Path(tmp) / "tree")
            if plan_git._git(repo, "clone", "--shared", "--no-checkout", "--", repo, tree).returncode:
                return False, "generated check checkout unavailable"
            if plan_git._git(tree, "checkout", "--detach", frozen).returncode:
                return False, "generated check checkout unavailable"
            for path in sorted(files):
                rule = files[path]
                runtime = rule.get("runtime")
                if runtime and not (Path(tree) / runtime).exists():
                    # Reuse installed tools only with exactly the frozen lock
                    # and package declaration. Missing tools are inconclusive.
                    for lock in rule["locks"]:
                        blob = plan_git._git(repo, "show", frozen + ":" + lock)
                        if blob.returncode or (Path(runtime_repo) / lock).read_bytes() != blob.stdout:
                            return False, "generated runtime lock differs from frozen head"
                    source = Path(runtime_repo) / runtime
                    if not source.is_dir():
                        return False, "generated runtime unavailable"
                    (Path(tree) / runtime).symlink_to(source.resolve(), target_is_directory=True)
                # Isolate ambient configuration, while trusting the host's
                # executables and installed runtime bytes (not a sandbox).
                env = {"PATH": os.defpath, "HOME": tmp, "TMPDIR": tmp,
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "PYTHONPATH": str(Path(tree) / "src"),
                       "PYTHONNOUSERSITE": "1"}
                command = list(rule["check"])
                if "/" not in command[0]:
                    # The host runtime is trusted, including tools outside the
                    # system PATH. Resolve only the executable; do not pass the
                    # host PATH or injection variables into the checker.
                    executable = shutil.which(command[0])
                    if executable is None:
                        return False, "generated executable unavailable for " + path
                    command[0] = str(Path(executable).resolve())
                result = subprocess.run(command, cwd=tree,
                                        env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=GENERATED_CHECK_TIMEOUT)
                if result.returncode:
                    return False, "generated check failed or inconclusive for " + path
                # A checker must validate the committed bytes, not fix them.
                blob = plan_git._git(repo, "show", frozen + ":" + path)
                if blob.returncode or (Path(tree) / path).read_bytes() != blob.stdout:
                    return False, "generated check modified its output"
    except (OSError, subprocess.SubprocessError):
        return False, "generated check unavailable or timed out"
    return True, "generated adaptation: frozen generation checks passed for " + ", ".join(sorted(changed))


def _carriable(repo: str, reviewed: str, frozen: str, tip: str, *,
               renumber: Renumbering | None = None, runtime_repo: str | None = None) -> tuple:
    """`(oui, raison, adaptation)` : réalignement (tout verdict se reporte) ou, à
    défaut, adaptation (l'avis favorable seul se reporte, #5042, #7000)."""
    ok, why = realigned(repo, reviewed, frozen, tip)
    if ok:
        return True, why, False
    fits, how = adapted(repo, reviewed, frozen, tip, renumber=renumber)
    if fits:
        return True, how, True
    generated, proof = generated_adapted(repo, reviewed, frozen, declaration_ref=tip,
                                          runtime_repo=runtime_repo)
    if generated:
        return True, proof, True
    return False, "%s; not an adaptation either: %s; generated check #7323: %s" % (why, how, proof), False


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
        # Old refusals of generated files never ran this new proof. Revisit
        # those once on the same freeze, preserving acquired verdicts.
        if (str(note).startswith(NOT_CARRIED)
                and "differs outside a test or a conflict" in note
                and "generated check #7323:" not in note):
            continue
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
                 "carried": [], "refused": [], "superseded": [], "detail": ""}
    try:
        out, plan, refused, _where = _plan_for(
            cfg, db, lot, out, _norm(frozen_sha), repo=repo, clone=clone,
            notes=notes, fetched={} if fetched is None else fetched)
    except Exception as exc:  # noqa: BLE001 - la désignation garde son cours
        out.update(result="error", detail="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return out
    if out["result"] in ("no-repository", "error"):
        return out
    out["result"] = ("would-carry" if plan else
                     "not-carried" if refused or out.get("superseded") else "idle")
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
                 "carried": [], "refused": [], "superseded": [], "detail": ""}
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
    if not plan and not refused and not out.get("superseded"):
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
    from .agent_review_hold import carried_reviewers
    eligible = set(carried_reviewers(db, lot, [m.get("actor") for m in candidates.values()]))
    candidates = {key: mark for key, mark in candidates.items() if mark.get("actor") in eligible}
    if not candidates:
        out["detail"] = "no verdict to carry"
        return out, [], [], ""
    if notes is None:
        notes = _outcomes(db, [lot])[lot]
    if all(_decided(notes, m.get("sha"), declared) for m in candidates.values()):
        out["detail"] = "already decided for this freeze"
        return out, [], [], ""
    repo = _repo_path(repo or lot_class.repository(cfg, row))
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
    try:
        renumber = renumbering_of(cfg, row, repo)
    except Exception:  # noqa: BLE001 - réservation illisible : aucune renumérotation
        renumber = None
    verdicts: dict = {}          # tête relue complète -> (oui, raison)
    plan: list = []              # (relecteur, verdict reporté)
    refused: list = []           # (tête relue, raison)
    superseded: list = []        # (tête relue, raison), hors du gel précédent
    previous = plan_git.latest_freeze(
        [m for m in marks if m.get("kind") == "frozen" and not _same(m.get("sha"), declared)])
    for key, mark in sorted(candidates.items()):
        if _decided(notes, mark.get("sha"), frozen):
            continue
        reviewed = merge_queue.pinned(where, mark.get("sha"))
        if not reviewed:
            out["detail"] = "reviewed head %s is not a commit of %s" % (
                _norm(mark.get("sha"))[:12], where)
            continue                  # objet absent : rien de prouvé, retenté
        if reviewed not in verdicts:
            verdicts[reviewed] = _carriable(where, reviewed, frozen, tip, renumber=renumber,
                                               runtime_repo=repo)
        ok, why, adaptation = verdicts[reviewed]
        if ok and adaptation and (mark.get("verdict") or "ok") != "ok":
            ok, why = False, NOT_OK_ADAPTATION
        if ok:
            plan.append((mark, reviewed))
            if adaptation:
                out.setdefault("adaptations", {})[reviewed] = why
        elif _stale(mark, previous):
            if (reviewed, why) not in superseded:
                superseded.append((reviewed, why))
        elif (reviewed, why) not in refused:
            refused.append((reviewed, why))
    if not plan and not refused and not superseded:
        if not out["detail"]:
            out["detail"] = "nothing decidable"
        return out, [], [], where
    out["carried"] = [_entry(m, r) for m, r in plan]
    out["refused"] = [{"reviewed": r, "why": w} for r, w in refused]
    out["superseded"] = [{"reviewed": r, "why": w} for r, w in superseded]
    return out, plan, refused, where


def _stale(mark: Mapping, previous: Optional[Mapping]) -> bool:
    """#6720 : le verdict précède le gel précédent et porte sur une autre tête.

    Constat nexlink #6699 (10/11) : rev-02 bloque 812d130, l'auteur corrige,
    le gel ab80b4 reçoit deux avis favorables d'autres relecteurs, puis un
    rebasage regèle 77c74bc. Le dernier verdict de rev-02 porte encore sur
    812d130 : son report est refusé, mais ce blocage ne comptait déjà plus à
    la porte du gel précédent (elle ne lit que la tête gelée). Ce refus ne
    rend donc pas le report partiel : il ne demande aucun siège et ne relance
    aucune désignation. Les sièges du gel précédent sans verdict restent
    désignés par la marque du report (`_previous_designees`)."""
    if not previous:
        return False
    return (int(mark.get("id") or 0) < int(previous.get("id") or 0)
            and not _same(mark.get("sha"), previous.get("sha")))


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
    adaptations = out.get("adaptations") or {}
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
        from .agent_review_hold import carried_reviewers
        eligible = set(carried_reviewers(tx, lot, [m.get("actor") for m, _ in plan]))
        for mark, reviewed in plan:
            if mark.get("actor") not in eligible:
                continue
            now = still.get(_key(mark.get("actor")))
            if now is None or int(now.get("id") or 0) != int(mark.get("id") or 0):
                continue              # verdict rendu ou reporté entre-temps : le sien vaut
            note = ("%s %s on %s@%s, %s" % (
                CARRIED, _arrow(reviewed, frozen), target, tip[:12],
                adaptations.get(reviewed) or "same merge result and same lot delta as "
                "the reviewed head (#2442)"))
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
        st.note(lot, state, "%s %s on %s@%s: %s — no re-review due (%s)"
                % (CARRIED, _arrow(reviewed, frozen), target, tip[:12],
                   ", ".join("%s %s" % (e["verdict"], e["actor"]) for e in entries),
                   adaptations.get(reviewed) or "#2442"),
                work.SYSTEM_SENDER, once=True)
    for reviewed, why in refused:
        st.note(lot, state, "%s %s on %s@%s: %s — verdicts are due on the frozen head (#2442)"
                % (NOT_CARRIED, _arrow(reviewed, frozen), target, tip[:12], why),
                work.SYSTEM_SENDER, once=True)
    superseded = [(e["reviewed"], e["why"]) for e in out.get("superseded") or ()]
    for reviewed, why in superseded:
        st.note(lot, state, "%s %s on %s@%s: %s — %s: no seat is due for it (#6720)"
                % (NOT_CARRIED, _arrow(reviewed, frozen), target, tip[:12], why,
                   SUPERSEDED), work.SYSTEM_SENDER, once=True)
    out.update(carried=carried, refused=[{"reviewed": r, "why": w} for r, w in refused],
               result="carried" if carried else
               "not-carried" if refused or superseded else "idle")
    if carried:
        how = ("a test or mechanical adaptation of the reviewed head (canon §2)"
               if any(e["from"] in adaptations for e in carried) else
               "a mechanical realignment (same merge result and same lot delta as the "
               "reviewed head)")
        text = ("Lot #%d « %s »: the new freeze %s on %s is %s; verdicts "
                "carried: %s. No re-review is due (#2442)."
                % (lot, row.get("title") or "", frozen[:12], target, how,
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
#: une porte lue fermée n'est relue qu'après ce délai, sauf si le lot bouge
#: (jalon, ligne) : le canon peut changer le plancher sans toucher au lot
CLOSED_RECHECK_S = 1800
#: #4849 : lot → (empreinte, instant) de la dernière porte lue fermée par ce
#: processus. Sans essai ni note : une porte fermée n'est pas une panne.
_closed_gates: dict[int, tuple] = {}


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


def _notify_held(db, lot: int) -> None:
    """#6089 : le quorum reporté valide le lot ; un hold de pilote y survit
    (#5758) et son détenteur l'apprend. L'avis ne défait jamais la
    validation."""
    from . import validated_hold_notice
    try:
        validated_hold_notice.notify(db, int(lot))
    except Exception:  # noqa: BLE001 - l'avis est une suite, pas une condition
        pass


def promote_carried(db, lot: int, *, freeze=None) -> dict:
    """La transition de quorum d'un verdict reporté, comme pour un avis rendu.

    La porte est lue **avant** le geste d'écriture, avec la même lecture que la
    garde de `work.move` : une porte ouverte appelle la validation ordinaire ;
    une porte lisiblement fermée est le `qa` normal ; une porte illisible ne
    conclut pas et ne consomme aucun essai.

    Après la copie des verdicts — un INSERT direct, qui ne passe donc pas par
    `add_milestone` ni par `_sync_validation` (#4423) — le lot doit passer en
    `validated` dès que la porte est ouverte. L'écriture reste celle d'une
    validation ordinaire (`work.move`, garde de quorum relue et sérialisée).

    Un échec n'est **pas** silencieux (#4715, #4834). Une porte **illisible**
    (lecture en échec, provenance non concluante) est une panne passagère :
    une ligne `carried quorum pending:` la dit à chaque essai (5 au plus), et
    `run_pass` retente. Une porte **lisible et fermée** (avis manquant,
    bloquant) est le cours normal du `qa` : ni note ni essai — le verdict
    suivant la rouvrira, et `run_pass` ne la relit que si le lot bouge
    (#4849). Sans cette distinction, une lecture ratée laissait le lot en `qa`
    pour toujours : le report avait déjà marqué ses verdicts « décidés »,
    aucun écrivain ne revenait. Rend
    `{result: validated|pending|closed|exhausted|stale|idle, detail}` ; ne
    lève jamais."""
    row = work.get(db, lot)
    if not row or str(row.get("state") or "") != "qa":
        return {"result": "idle", "detail": "lot not in qa"}
    if str(row.get("workflow_ref") or "").strip():
        return {"result": "idle", "detail": "pinned lot"}
    marks = work.milestones(db, lot)
    latest = plan_git.latest_freeze(marks)
    freeze = freeze or latest
    # #5481 : un passage peut porter un gel qui n'est plus celui du lot (gel
    # concurrent, regel de la même tête). Le quorum ne s'applique qu'à la tête
    # courante : une tête changée n'est jamais validée par un vieux report.
    # Un regel de la même tête garde la même preuve (les verdicts portent sur
    # le SHA, pas sur l'identifiant du gel).
    if (not freeze or not latest
            or not plan_git.same_commit(freeze.get("sha"), latest.get("sha"))):
        return {"result": "stale",
                "detail": "the carried freeze is no longer the lot's head"}
    if not _carried_ok_on_freeze(marks, freeze):
        return {"result": "idle", "detail": "no carried ok verdict on the frozen head"}
    # #4834 : la porte est lue avec la même lecture que la garde de
    # `work.move`, **avant** le geste d'écriture. Un hôte sans dépôt local ne
    # peut pas conclure la porte : il ne doit ni écrire de marque ni consommer
    # d'essai (le budget est par hôte) ; le prochain passage d'un hôte qui
    # résout le gel fera la transition.
    from . import lot_class
    frozen = _norm(freeze.get("sha"))

    def _read_gate():
        try:
            found = merge_queue.review_gate_for_item(
                db, getattr(db, "cfg", None), row, marks)
        except Exception as caught:  # noqa: BLE001 - une lecture ratée ne conclut pas
            return None, caught
        return found, None

    def _attempt():
        work.move(db, lot, "validated", actor=work.SYSTEM_SENDER,
                  note="qa → validated — carried review quorum (#4423)")

    gate, gate_exc = _read_gate()
    gate2 = None
    if isinstance(gate, dict) and not gate.get("error"):
        if not gate.get("ok"):
            # Porte lisible et fermée : avis manquant ou bloquant. Le lot
            # reste en `qa` par la règle ordinaire ; le prochain verdict le
            # promouvra, sans bruit ni reprise.
            return {"result": "closed", "detail": gate.get("why") or ""}
        exc = None
        try:
            _attempt()
        except Exception as caught:  # noqa: BLE001 - classée ci-dessous
            exc = caught
        else:
            _notify_held(db, lot)
            return {"result": "validated"}
        # La porte est ouverte mais l'écriture a échoué (course, transaction).
        # Une seconde lecture bornée dit si elle l'est encore : un second
        # essai, sinon la reprise au passage suivant.
        gate2, gate2_exc = _read_gate()
        if isinstance(gate2, dict) and gate2.get("ok") and not gate2.get("error"):
            try:
                _attempt()
            except Exception as caught:  # noqa: BLE001 - reprise au passage suivant
                exc = caught
            else:
                _notify_held(db, lot)
                return {"result": "validated"}
        elif isinstance(gate2, dict) and not gate2.get("error"):
            return {"result": "closed", "detail": gate2.get("why") or ""}
        elif gate2_exc is not None:
            exc = gate2_exc
    else:
        exc = gate_exc
    # #5880 : une porte rendue avec `error` a un diagnostic (provenance
    # inconnue, registre ou canon illisible) : la note le dit au lieu de
    # « the review gate is unreadable ».
    gate_why = ""
    for found in (gate2, gate):
        if isinstance(found, dict) and found.get("error"):
            gate_why = str(found.get("error") or found.get("why") or "")
            break
    # #5481 : la course entre hôtes n'est pas une panne. Entre la lecture de
    # l'état et le geste, un autre passage a pu valider le lot (`work.move`
    # lève alors « already in validated ») ou le faire sortir de `qa`. On
    # relit l'état : validé est le résultat attendu, et rien n'est compté
    # comme essai. Sans cette relecture, trois hôtes brûlaient chacun cinq
    # tentatives sur un lot déjà validé (nexlink #5434).
    try:
        now = work.get(db, lot)
    except Exception:  # noqa: BLE001 - une relecture ratée ne conclut pas
        now = None
    if now is not None:
        now_state = str(now.get("state") or "")
        if now_state == "validated":
            return {"result": "validated",
                    "detail": "another writer already validated the carried quorum"}
        if now_state != "qa":
            return {"result": "idle",
                    "detail": "the lot left qa during the carried quorum"}
    # La porte n'a pas conclu : sans dépôt local, cet hôte ne peut rien
    # affirmer et ne doit pas consommer le budget d'un autre hôte.
    try:
        repo, _resolved = lot_class.repo_for_freeze(
            db, getattr(db, "cfg", None), row, frozen)
    except Exception:  # noqa: BLE001 - une sonde ratée ne conclut pas
        repo = ""
    if not repo:
        return {"result": "no-repository",
                "detail": "the frozen head is unreadable from this host"}
    why = ("%s: %s" % (type(exc).__name__, exc) if not isinstance(exc, work.WorkError)
           else str(exc)) if exc is not None else (
               gate_why or "the review gate is unreadable")
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
    if (effective is None or effective[1] != "carried"
            or current_names != wanted_names
            or bool((effective[2] if effective else {}).get("provisional"))):
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
            ["%s: %s" % (e["reviewed"][:12], e["why"]) for e in result.get("refused") or ()]
            + ["%s: %s (%s)" % (e["reviewed"][:12], e["why"], SUPERSEDED)
               for e in result.get("superseded") or ()])
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


def _gate_print(row: Mapping, lot_marks, freeze: Mapping) -> tuple:
    """Ce qui peut rouvrir une porte fermée sans passer par `_sync_validation` :
    un jalon (verdict, gel), la ligne du lot (relecteurs, état)."""
    last = max((int(mark.get("id") or 0) for mark in lot_marks or ()), default=0)
    return (int(freeze.get("id") or 0), last, row.get("updated_ts"),
            json.dumps(row.get("reviewers"), sort_keys=True, default=str))


def _recently_closed(lot: int, print_: tuple, now: float) -> bool:
    seen = _closed_gates.get(lot)
    return bool(seen and seen[0] == print_ and now - seen[1] < CLOSED_RECHECK_S)


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
    #4849 : une porte lue fermée (`closed`) n'est pas relue à chaque passage :
    ce processus s'en souvient, sans essai ni note, tant que le lot ne bouge
    pas et au plus `CLOSED_RECHECK_S`. Ne lève jamais pour un lot."""
    st = storage.of(db)
    rows = [r for r in st.work.open_for_sweep(limit)
            if r.get("state") not in tuple(work.MERGED_STATES) + ("closed",)]
    if not rows:
        return []
    marks: dict = {}
    for mark in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        marks.setdefault(int(mark["work_item_id"]), []).append(mark)
    # #4834 : un lot en `qa` qui porte un `ok` reporté sur son gel est repris,
    # même sans essai écrit. Une panne de lecture (`carried quorum pending:`)
    # se retente dans la limite d'essais de **son** gel et de cet hôte, puis
    # le lot reste en `qa` (visible par l'état). #4849 : une porte lisiblement
    # fermée n'est pas une panne ; elle n'est relue que si le lot a bougé.
    pending_notes: dict[int, list[str]] = {}
    for entry in st.work.events_noted(PENDING, 0.0, ids=[int(r["id"]) for r in rows]):
        pending_notes.setdefault(int(entry["work_item_id"]), []).append(
            str(entry.get("note") or ""))
    wanted = []
    pending_quorum = []
    host = _host(db)
    now = time.time()
    eligible: set = set()
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
            eligible.add(lot)
            print_ = _gate_print(row, lot_marks, freeze)
            if not _recently_closed(lot, print_, now):
                pending_quorum.append((lot, freeze, print_))
    for lot in set(_closed_gates) - eligible:
        _closed_gates.pop(lot, None)
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
    for lot, freeze, print_ in pending_quorum:
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
        if result["result"] == "closed":
            _closed_gates[lot] = (print_, now)
        else:
            _closed_gates.pop(lot, None)
        # `pending` est déjà dit une fois au journal du lot par
        # `promote_carried` ; le passage suivant le retente sans bruit.
    return out
