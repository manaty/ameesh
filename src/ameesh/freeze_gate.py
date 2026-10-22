# SPDX-License-Identifier: AGPL-3.0-only
"""La porte du gel et du verdict — les jalons qui prouvent une tête relue (#2873).

Le gel (`frozen`) et le verdict (`verdict`) décident qu'une tête a été relue :
leur écriture, leur lecture et la résolution des têtes vivaient dans
`work.py`, la machine à états touchée par presque tous les lots. Elles vivent
ici pour que toute modification de la porte passe par les deux avis déclarés
au canon (§8, arbitrage mesh-design du 20/10/2026, #2872/#2873), sans
surclasser tout `work.py`.

`work.py` réexporte ces noms : les appelants (`ameesh work milestone`, `fire`,
`merge_queue`, la CLI…) ne changent pas. Le déplacement est sans changement
de comportement : mêmes messages, mêmes refus, mêmes traces.
"""
from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Sequence

from . import storage

if TYPE_CHECKING:  # pragma: no cover - annotations seulement
    from .db import Db
    from .work import WorkError

#: les jalons d'un lot : `requested`, `merged`, `closed` et `delivered` sont
#: automatiques (0013, 0026, fermeture #988) ; `frozen`, `verdict`, `fire` et
#: `unfire` se déclarent par la CLI — le feu (`fire`/`unfire`, #1374) par
#: `ameesh work fire` seulement, qui vérifie qu'il porte sur le gel courant
#: (`ameesh.fire`).
MILESTONE_KINDS = ("requested", "frozen", "verdict", "fire", "unfire", "merged", "closed", "delivered")
MANUAL_KINDS = ("frozen", "verdict", "fire", "unfire")
VERDICTS = ("ok", "blocked")

#: #2674 : un verdict ne compte à la porte (`merge_queue.review_gate`) que
#: s'il nomme la tête relue — un commit complet (40 ou 64 hexadécimaux), ou
#: une abréviation d'au moins 12 caractères **résolue** dans un dépôt (ou,
#: sans dépôt, dans la tête gelée courante). Un avis sans tête ne pouvait
#: jamais compter et laissait le lot des heures en « verdicts-missing ».
REVIEWED_HEAD_MIN = 12
_FULL_HEAD = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REVIEWED_HEAD = re.compile(r"[0-9a-f]{%d,64}" % REVIEWED_HEAD_MIN)

#: #2786 : un gel écrit nomme un commit complet (40 ou 64 hexadécimaux) —
#: une abréviation hexadécimale d'au moins [`FROZEN_HEAD_MIN`] caractères est
#: résolue **dans le dépôt de l'auteur** et écrite complète. Une ref
#: symbolique (`HEAD`, une branche) est refusée : chaque lecteur la résolvait
#: dans SON clone, et le balayage d'un hôte a passé un lot en `merged` sur le
#: `HEAD` d'un autre arbre que celui gelé (#2712).
FROZEN_HEAD_MIN = REVIEWED_HEAD_MIN
#: #2786 (5) : la ligne machine de la note d'un gel qui dit dans quelles
#: branches d'intégration il n'était pas encore (le balayage y croit la
#: livraison par le contenu, même après une avance rapide).
FREEZE_ABSENT_MARK = "ameesh-freeze-absent:"
#: #2786 : la marque est une preuve machine, jamais une note d'appelant : une
#: ligne fournie par `--note` est retirée avant que le gel n'écrive la sienne
#: (sinon `--note "ameesh-freeze-absent: main"` contournerait le garde #2784).
FREEZE_ABSENT_LINE = re.compile(r"(?m)^[ \t]*ameesh-freeze-absent:[^\n]*(?:\n|$)")
#: #2971 : toute marque machine de la note de gel (`ameesh-freeze-absent`,
#: `ameesh-freeze-unverified`, `ameesh-freeze-unpublished`,
#: `ameesh-freeze-supersedes`, ...) est une preuve du gate, jamais une note
#: d'appelant. Le motif est générique pour couvrir aussi les marques des
#: branches #2962/#2989 : une ligne fournie par `--note` est retirée avant
#: l'écriture, comme `FREEZE_ABSENT_LINE` le fait pour `absent`.
#: Le motif juge une ligne déjà strippée : `add_milestone` strippe la note
#: finale, donc un blanc de tête (`\v`, `\f`, espace insécable) ne doit pas
#: cacher la marque (#2971, revue impl-05 — même classe que #2989 B).
FREEZE_MACHINE_LINE = re.compile(r"ameesh-freeze-[a-z0-9-]+:")
#: #2989 : la ligne machine d'un gel qui déclare un gel antérieur **remplacé**
#: (typiquement un gel fantôme dont le SHA n'existe nulle part). Elle n'est
#: écrite que par `work milestone frozen --supersedes <id>`, jamais recopiée
#: d'une note d'appelant : la porte ignore alors le gel remplacé, avec la note
#: du nouveau gel comme trace. Sans elle, un gel antérieur absent bloque la
#: provenance (refus fermé de #2924 B5).
#: (Résolution de conflit #3009/#2989 : union des deux gardes ; le filtre
#: générique de #2971 couvre déjà `supersedes`, les constantes de #2989
#: restent pour sa lecture des auteurs.)
FREEZE_SUPERSEDES_MARK = "ameesh-freeze-supersedes:"
FREEZE_SUPERSEDES_LINE = re.compile(
    r"(?m)^[ \t]*ameesh-freeze-supersedes:[^\n]*(?:\n|$)")
FREEZE_SUPERSEDES_RE = re.compile(
    r"(?m)^ameesh-freeze-supersedes:\s*(\d+)\s*$")

#: #2962 : un gel résolu localement mais absent de `origin/<branche>` après
#: rafraîchissement reste accepté (geler puis pousser dans le même tour est
#: permis), mais l'avertissement est explicite dans la sortie et dans la note.
FREEZE_UNPUBLISHED_MARK = "ameesh-freeze-unpublished:"
#: #2962 (exception du gel distant) : aucun dépôt de l'auteur n'est lisible
#: depuis l'hôte. La tête complète est écrite **avec une note**, jamais tenue
#: pour vérifiée : on ne devine pas un commit qu'aucun dépôt ne peut lire.
FREEZE_UNVERIFIED_MARK = "ameesh-freeze-unverified:"


def milestones(db: Db, item_id: int, limit: int = 100) -> list[dict]:
    """Les jalons d'un lot, du plus récent au plus ancien."""
    return storage.of(db).work.milestones(item_id, limit)


def _in_transaction(db: Db) -> bool:
    """True on an open transaction object (psql) or inside a psycopg block."""
    depth = getattr(db, "_depth", None)
    if depth is not None:
        return bool(depth)
    return int(getattr(db, "_tx_depth", 0) or 0) > 0


def frozen_head(db: Db, item_id: int) -> str:
    """Le SHA du dernier gel du lot, en minuscules ("" s'il n'est pas gelé)."""
    from . import plan_git
    freeze = plan_git.latest_freeze(milestones(db, item_id))
    return str((freeze or {}).get("sha") or "").strip().lower()


#: l'ancien nom privé de `work.py` (#2873) : la porte le garde pour ses
#: appelants et les tests qui lisaient la tête gelée.
_frozen_head = frozen_head


def _head_repos(db: Db, item: dict) -> list[str]:
    """Les dépôts où résoudre l'abréviation d'un verdict, sans réseau.

    Le répertoire courant est celui où `ameesh` est lancé (le plus souvent
    l'arbre du relecteur) ; le dépôt du projet vient du radar ; le dossier de
    travail de l'assigné, du registre. Aucun n'est autoritaire (seul `--repo`
    l'est) : une abréviation résolue dans l'un suffit, et l'absence de dépôt
    est dite par le refus."""
    candidates: list = [os.getcwd()]
    try:
        from . import lot_class
        candidates.append(lot_class.repository(getattr(db, "cfg", None), item))
    except Exception:  # noqa: BLE001 - réglages illisibles : pas de dépôt
        pass
    try:
        from . import fire
        candidates.append(fire._repo_of(db, item))
    except Exception:  # noqa: BLE001 - registre illisible : pas de dépôt
        pass
    # #2928 : une tête relue peut vivre dans le dépôt du canon (le cas des
    # lots `canon/**`), pas dans le dépôt du projet. Elle s'y résout, et la
    # preuve #1465 s'y écrit — sans cela le verdict s'écrivait sans reçu et
    # la porte sensible ne pouvait plus le compter.
    try:
        from . import canon as canon_mod
        for entry in canon_mod.configured(getattr(db, "cfg", None)):
            candidates.append(getattr(entry, "path", ""))
    except Exception:  # noqa: BLE001 - canon illisible : pas de dépôt
        pass
    seen: list[str] = []
    for path in candidates:
        path = str(path or "").strip()
        if path and path not in seen and os.path.isdir(path):
            seen.append(path)
    return seen


def _author_repos(db: Db, item: dict) -> list[str]:
    """Les dépôts **de l'auteur** lisibles depuis l'hôte (#2962).

    Seuls comptent le dépôt du projet (radar) et le dossier de travail de
    l'assigné (registre), pas le répertoire courant : `_head_repos` accepte le
    cwd comme lieu de résolution commode, mais il n'est pas la preuve que
    l'auteur peut lire sa propre tête. Un dossier qui existe sans être un
    checkout git n'est pas un dépôt lisible (`fire._checkout`)."""
    candidates: list = []
    try:
        from . import fire
    except Exception:  # noqa: BLE001 - pas de validateur : pas de dépôt
        return []
    try:
        from . import lot_class
        candidates.append(lot_class.repository(getattr(db, "cfg", None), item))
    except Exception:  # noqa: BLE001 - réglages illisibles : pas de dépôt
        pass
    try:
        candidates.append(fire._repo_of(db, item))
    except Exception:  # noqa: BLE001 - registre illisible : pas de dépôt
        pass
    seen: list[str] = []
    for path in candidates:
        where = fire._checkout(str(path or "").strip())
        if where and where not in seen:
            seen.append(where)
    return seen


def _proof_repo(db: Db, item: dict, sha: str,
                repo: str | None = None) -> str | None:
    """Le dépôt où `sha` se résout, pour la preuve exacte du jalon (#1465/#2928).

    Le répertoire courant d'un relecteur n'est pas celui de la tête relue
    quand elle vit dans un autre dépôt — le dépôt canon, par exemple. Sans ce
    dépôt, `proof_for_current_turn` ne résout pas le commit, n'écrit aucun
    reçu, et la porte sensible refuse désormais ce verdict : la preuve se
    résout donc là où le commit existe. Rend None quand aucun dépôt ne le
    connaît : le jalon s'écrit alors sans preuve, comme avant."""
    text = str(sha or "").strip().lower()
    if not text:
        return None
    from . import freeze_checks
    for where in ([repo] if repo else []) + _head_repos(db, item):
        if freeze_checks.commit_in(where, text):
            return where
    return None


def _proposed_head(db: Db, item: dict, frozen: str, repo: str | None = None) -> str:
    """La tête gelée proposée dans un refus, en abréviation valide (12+).

    Un gel ancien peut porter moins de 12 caractères : on le complète dans un
    dépôt quand l'un le connaît, sinon la proposition est vide (le refus dit
    alors de geler de nouveau avec la tête complète)."""
    head = frozen
    if head and len(head) < REVIEWED_HEAD_MIN:
        from . import freeze_checks
        for where in ([repo] if repo else []) + _head_repos(db, item):
            head = freeze_checks.commit_in(where, head) or head
            if len(head) >= REVIEWED_HEAD_MIN:
                break
    return head[:12] if len(head) >= REVIEWED_HEAD_MIN else ""


def _head_refusal(db: Db, item: dict, why: str, *,
                  repo: str | None = None) -> "WorkError":
    """Le refus nommé d'un verdict sans tête, avec la tête gelée proposée."""
    from .work import WorkError
    frozen = frozen_head(db, item["id"])
    proposed = _proposed_head(db, item, frozen, repo)
    if proposed:
        hint = ("the current frozen head is %s: pass it with `--sha %s`"
                % (proposed, proposed))
    elif frozen:
        hint = ("the current frozen head %s is too short to name a commit: freeze "
                "again with the full head" % frozen)
    else:
        hint = ("lot #%d has no frozen head yet: freeze it first "
                "(`ameesh work milestone %d frozen --sha <commit>`)"
                % (item["id"], item["id"]))
    return WorkError("%s; %s" % (why, hint))


def reviewed_head(db: Db, item: dict, sha: str, *, repo: str | None = None) -> str:
    """Le `sha` d'un verdict, vérifié et normalisé (#2674).

    Un avis ne compte à la porte (`merge_queue.review_gate`) que s'il nomme la
    tête relue : un commit **résolu** — dans `repo` (nommé par `--repo`, alors
    autoritaire), sinon dans le répertoire courant, le dépôt du projet ou celui
    de l'assigné — ou la tête gelée courante. #2962 : un texte de 40
    hexadécimaux n'est plus cru sur parole ; il doit se résoudre comme une
    abréviation, ou nommer exactement la tête gelée (le relecteur peut ne pas
    avoir le dépôt). Un `sha` vide, trop court, non hexadécimal ou non résolu
    est refusé avec la tête gelée proposée : le jalon n'est pas écrit, plutôt
    qu'un avis qui ne pourrait jamais franchir la porte."""
    text = str(sha or "").strip().lower()
    if not text:
        raise _head_refusal(
            db, item,
            "verdict refused (head-missing): a verdict must name the reviewed head — "
            "the full commit id, or an abbreviation of at least %d hex characters "
            "resolved in a repository (`--sha`)" % REVIEWED_HEAD_MIN)
    if not _REVIEWED_HEAD.fullmatch(text):
        raise _head_refusal(
            db, item,
            "verdict refused (head-missing): --sha %r is not a commit id: give the full "
            "head, or an abbreviation of at least %d hex characters (`--sha`)"
            % (sha, REVIEWED_HEAD_MIN), repo=repo)
    from . import freeze_checks
    # #2962 : une tête **complète** n'est plus crue sur parole. Elle vaut
    # quand elle nomme exactement la tête gelée courante (même si aucun dépôt
    # local ne la porte : la tête gelée fait foi, et `--repo` ne peut pas
    # l'effacer), sinon il faut un dépôt qui la résolve.
    full = bool(_FULL_HEAD.fullmatch(text))
    frozen = frozen_head(db, item["id"]) if full else ""
    if full and frozen == text:
        return text
    if repo:
        if freeze_checks.commit_in(repo, text):
            return text
        raise _head_refusal(
            db, item,
            "verdict refused (head-unresolved): %s does not name a commit of %s"
            % (text, repo), repo=repo)
    for where in _head_repos(db, item):
        if freeze_checks.commit_in(where, text):
            return text
    if not full:
        frozen = frozen_head(db, item["id"])
    if frozen and (frozen == text if full else frozen.startswith(text)):
        return text
    raise _head_refusal(
        db, item,
        "verdict refused (head-unresolved): %s does not resolve to a commit here and "
        "does not name the current frozen head" % text, repo=repo)


def _freeze_refusal_target(repo: str, item: dict, refreshed: dict | None = None) -> str:
    """La cible du lot lue dans `repo` (`branch_target`, puis le dépôt), ou ''.

    #2786 : le clone est rafraîchi avant de lire la cible publiée — un
    `origin/<branche>` périmé ferait accepter un gel déjà livré (avis cx1)."""
    from . import plan_git
    cache = refreshed if refreshed is not None else {}
    try:
        # aucun remote : rien à rafraîchir, la cible locale fait foi. Le
        # premier fetch (origin) précède la résolution : une cible qui n'existe
        # qu'à la forge (`origin/HEAD`, `origin/main`) doit être lisible.
        if plan_git._out(repo, "remote"):
            plan_git.refresh_clone(repo, "", cache)
    except Exception:  # noqa: BLE001 - forge illisible : la cible reste lisible
        pass
    target = str((item or {}).get("branch_target") or "").strip()
    if not target:
        try:
            target = str(plan_git.declared_target(repo) or "").strip()
        except Exception:  # noqa: BLE001 - réglage illisible : cible inconnue
            target = ""
    if not target:
        try:
            target = str(plan_git.default_target(repo) or "").strip()
        except Exception:  # noqa: BLE001 - cible par défaut illisible
            target = ""
    if not target:
        return ""
    try:
        if plan_git._out(repo, "remote"):
            # la cible peut nommer un autre remote : rafraîchi à son tour
            plan_git.refresh_clone(repo, target, cache)
    except Exception:  # noqa: BLE001 - forge illisible : la cible reste lisible
        pass
    try:
        # la ref publiée (origin/<branche>), rafraîchie, fait foi
        return str(plan_git.published_target(repo, target) or target)
    except Exception:  # noqa: BLE001 - pas de remote : la cible reste lisible
        return target


def _refuse_freeze_already_in_target(repo: str, item: dict, full: str,
                                     refreshed: dict | None = None) -> None:
    """#2786 (4) : refuser un gel qui n'a aucun commit propre.

    Un gel déjà ancêtre de la cible au moment où il est posé ne prouvera
    jamais une livraison (cas #2784 : un ancien commit d'un autre lot, déjà
    dans `main`, gelé sans branche ni commit propre). La cible illisible ne
    conclut pas : le balayage la traitera en doute."""
    from . import plan_git
    from .work import WorkError
    target = _freeze_refusal_target(repo, item, refreshed)
    if not target:
        return
    try:
        if plan_git._commit(repo, target) is None:
            return
        if plan_git._is_ancestor(repo, full, target):
            raise WorkError(
                "freeze refused (already-in-target): %s is already an ancestor of %s "
                "in %s — the freeze names no own commit; push the lot's commit and "
                "freeze its head (#2784)" % (full[:12], target, repo))
    except WorkError:
        raise
    except Exception:  # noqa: BLE001 - cible illisible : le balayage tranchera
        return


def _freeze_absent_marker(db: Db, item: dict, full: str, repo: str | None,
                          refreshed: dict | None = None) -> str:
    """La ligne machine des branches où `full` n'était pas au gel (#2786 (5)).

    Le balayage y lira une preuve de livraison même après une avance rapide ;
    sans elle (gel antérieur), il retombe sur l'instant du jalon. N'est écrite
    que quand un dépôt de l'auteur porte le commit et que la cible **publiée a
    été lue après rafraîchissement** ; une forge illisible ou un fetch refusé
    n'écrit aucune marque d'absence (avis cx1), le balayage doutera plutôt
    que de deviner."""
    from . import freeze_checks, plan_git
    where = ""
    for candidate in ([repo] if repo else []) + _head_repos(db, item):
        if candidate and freeze_checks.commit_in(candidate, full):
            where = candidate
            break
    if not where:
        return ""
    try:
        tips = plan_git.integration_tips(where, refreshed=refreshed)
        if tips is None:            # forge non lue : aucune preuve d'absence
            return ""
        absent = sorted(name for name, tip in tips.items()
                        if not plan_git._is_ancestor(where, full, tip))
    except Exception:  # noqa: BLE001 - refs illisibles : pas de marque
        return ""
    if not absent:
        return ""
    return "ameesh-freeze-absent: " + " ".join(absent[:8])


def _freeze_unpublished(db: Db, item: dict, full: str, repo: str | None,
                        refreshed: dict | None = None, *, where: str = "") -> str:
    """La ligne d'avertissement d'un gel absent de `origin/<branche>` (#2962).

    Le gel **n'est pas refusé** : geler puis pousser dans le même tour reste
    permis. Mais la tête n'est lisible par les autres hôtes qu'une fois la
    branche poussée ; le gel de #2914 sur 66a06cf0, jamais poussé, a envoyé
    une désignation sur une tête que personne ne pouvait lire. Le clone est
    rafraîchi avant la lecture (même cache `refreshed` que la marque #2786) ;
    une forge illisible ou un fetch refusé n'écrit rien, on ne devine pas.
    `where` est le dépôt qui a résolu la tête (rendu par `freeze_head`)."""
    from . import freeze_checks, plan_git
    branch = str((item or {}).get("branch") or "").strip()
    if not branch or not full:
        return ""
    holder = str(where or "").strip()
    if not holder:
        for candidate in ([repo] if repo else []) + _head_repos(db, item):
            if candidate and freeze_checks.commit_in(candidate, full):
                holder = candidate
                break
    if not holder:
        return ""
    try:
        if plan_git._out(holder, "remote", "get-url", "origin") is None:
            return ""                      # rien de publié à comparer
        cache = refreshed if refreshed is not None else {}
        if plan_git.refresh_clone(holder, "", cache).get("origin"):
            return ""                      # forge illisible : pas de devinette
        if plan_git._commit(holder, "refs/remotes/origin/" + branch) is None:
            detail = "branch not published"
        elif not plan_git._is_ancestor(holder, full, "refs/remotes/origin/" + branch):
            detail = "not in the published branch"
        else:
            return ""                      # la tête est publiée : rien à dire
    except Exception:  # noqa: BLE001 - refs illisibles : pas d'avertissement
        return ""
    return ("%s %s is not in origin/%s after refresh (%s); push the lot branch "
            "(freezing before pushing in the same turn stays allowed)"
            % (FREEZE_UNPUBLISHED_MARK, full[:12], branch, detail))


def _freeze_unresolved(text: str, repo: str | None = None) -> str:
    """Le refus nommé d'une tête de gel qu'aucun dépôt ne résout (#2962).

    Même message pour une abréviation et pour un commit complet : la tête
    complète n'est pas crue sur parole, elle doit être lisible quelque part.
    Le `repo` nommé est autoritaire ; hors `--repo`, le refus nomme les lieux
    où l'auteur peut geler."""
    if repo:
        return ("freeze refused (head-unresolved): %s does not name a commit of %s; "
                "freeze from the lot's worktree or pass --repo <the repository "
                "holding the commit>" % (text, repo))
    return ("freeze refused (head-unresolved): %s does not resolve to a commit "
            "in the author's repository (current directory, project repository, "
            "assignee's worktree); freeze from the lot's worktree or pass --repo "
            "<the repository holding the commit>" % text)


def freeze_head(db: Db, item: dict, sha: str, *, repo: str | None = None,
                refreshed: dict | None = None,
                outcome: dict | None = None) -> str:
    """La tête d'un gel, écrite en commit complet (#2786, #2962).

    Un gel est une tête **immuable** : il ne peut nommer qu'un commit
    complet (40 ou 64 hexadécimaux), ou une abréviation hexadécimale d'au
    moins [`FROZEN_HEAD_MIN`] caractères, résolue ici — dans `repo` quand
    `--repo` le nomme (alors autoritaire), sinon dans le répertoire courant,
    le dépôt du projet ou le dossier de travail de l'assigné — et rendue
    complète pour être écrite. Tout le reste est refusé : une ref symbolique
    (`HEAD`, une branche) n'est jamais résolue, parce que chaque lecteur la
    résoudrait dans SON clone et que le balayage d'un autre arbre passerait
    le lot en `merged` sur un commit que personne n'a relu (#2712).

    #2962 : un SHA complet doit lui aussi se résoudre dans `repo` ou dans
    [`_head_repos`], pas seulement avoir 40 caractères hexadécimaux. Une
    tête que personne ne peut lire n'est pas un gel : elle a déjà envoyé une
    désignation sur un commit introuvable (#2914, 66a06cf0). **Décision pour
    le gel distant** — quand *aucun dépôt de l'auteur* n'est lisible depuis
    l'hôte (ni le dépôt du projet, ni le dossier de l'assigné), on **accepte**
    la tête complète **avec la note `ameesh-freeze-unverified`** : refuser
    ferait dépendre un gel d'un clone que l'hôte n'a pas, et la note dit
    exactement ce qui n'a pas été vérifié. C'est la seule exception ; dès
    qu'un dépôt de l'auteur est lisible, un SHA complet absent de tous les
    dépôts est refusé `head-unresolved`. Le répertoire courant ne fait jamais
    cette preuve (#2989 N2) : un clone de relecteur non fetché refuserait une
    tête valide ; seuls le dépôt du projet et le dossier de l'assigné
    (`_author_repos`) comptent.

    (4) : quand le dépôt de l'auteur est lisible, un commit déjà ancêtre de
    la cible **publiée rafraîchie** est refusé — il n'a aucun commit propre
    et ne prouverait aucune livraison (#2784). `refreshed` est le cache de
    fetch partagé avec [`_freeze_absent_marker`] et [`_freeze_unpublished`]
    pour un même gel. `outcome` (facultatif) reçoit le dépôt qui a résolu la
    tête ou la marque `unverified` : l'appelant en tire la note, sans
    redemander au disque. Le `sha` rendu est le commit complet à stocker."""
    from .work import WorkError
    text = str(sha or "").strip().lower()
    from . import freeze_checks
    if not text:
        raise WorkError(
            "freeze refused (head-missing): a freeze must name the frozen commit — "
            "the full 40-hex commit id, or an abbreviation of at least %d hex "
            "characters resolved in the author's repository (`--sha`)"
            % FROZEN_HEAD_MIN)
    if not _REVIEWED_HEAD.fullmatch(text):
        raise WorkError(
            "freeze refused (head-missing): --sha %r is not a commit id — a symbolic "
            "ref (HEAD, a branch) is never resolved for a freeze: resolve it first "
            "(`git rev-parse <ref>`) and pass the full commit id, or an abbreviation "
            "of at least %d hex characters (`--sha`)" % (sha, FROZEN_HEAD_MIN))
    if repo:
        resolved = freeze_checks.commit_in(repo, text)
        if not resolved or not resolved.lower().startswith(text):
            raise WorkError(_freeze_unresolved(text, repo))
        full = resolved.lower()
        if outcome is not None:
            outcome["repo"] = repo
        _refuse_freeze_already_in_target(repo, item, full, refreshed)
        return full
    candidates = _head_repos(db, item)
    for where in candidates:
        resolved = freeze_checks.commit_in(where, text)
        if resolved and resolved.lower().startswith(text):
            full = resolved.lower()
            if outcome is not None:
                outcome["repo"] = where
            _refuse_freeze_already_in_target(where, item, full, refreshed)
            return full
    if _FULL_HEAD.fullmatch(text):
        # #2962, exception du gel distant : aucun dépôt de l'auteur n'est
        # lisible, on ne peut pas prouver l'absence. La note `unverified` est
        # écrite par `milestone` ; on ne prétend pas que la tête est vérifiée.
        # #2989 N2 (revue dev2) : seuls le dépôt du projet et le dossier de
        # l'assigné font preuve d'absence — le cwd d'un relecteur peut être un
        # clone non fetché, il ne prouve rien.
        authors = _author_repos(db, item)
        if not authors:
            if outcome is not None:
                outcome["unverified"] = True
            return text
    raise WorkError(_freeze_unresolved(text))


def milestone(
    db: Db,
    item_id: int,
    kind: str,
    *,
    sha: str = "",
    actor: str = "",
    verdict: str | None = None,
    note: str = "",
    on_frozen=None,
    after_commit: list | None = None,
    outcome: dict | None = None,
    repo: str | None = None,
    supersedes: Sequence[int] | int | None = None,
) -> dict:
    """Déclare un jalon que la machine à états ne peut pas déduire.

    `frozen` (la branche est gelée pour la revue), `verdict` (`ok` ou
    `blocked`, avec le `sha` relu), et `fire`/`unfire` (le feu du pilote et son
    retrait, #1374) sont les seuls déclarés : `requested`, `merged`, `closed`
    et `delivered` sont écrits automatiquement (transitions, fermeture et
    livraison hors cible : 0013, 0026, #988), les déclarer à la main serait un
    mensonge sur la frise.

    `verdict` (#2674, #2962) exige une tête relue : le `sha` est vérifié par
    [`reviewed_head`] avant toute écriture — un commit **résolu** dans un
    dépôt (ou `repo`), ou la tête gelée courante. Un texte complet de 40
    hexadécimaux n'est plus accepté sur parole. Sans résolution, le jalon est
    refusé avec la tête gelée courante proposée ; un avis qui ne peut pas
    compter à la porte n'est plus accepté en silence. `repo` (facultatif) est
    le dépôt nommé par `--repo`, où la tête doit alors se résoudre.

    `frozen` (#2786, #2962) exige une tête **immuable** : le `sha` est vérifié
    par [`freeze_head`] avant toute écriture — commit complet **résolu** dans
    un dépôt (ou `repo`), ou abréviation hexadécimale d'au moins 12 caractères
    résolue dans le dépôt de l'auteur et alors écrite complète ; une ref
    symbolique (`HEAD`, une branche) est refusée, parce que chaque lecteur la
    résoudrait dans son propre clone. Un gel distant, sans aucun dépôt de
    l'auteur lisible, écrit la tête complète avec la note
    [`FREEZE_UNVERIFIED_MARK`]. Un gel résolu mais absent de
    `origin/<branche>` après rafraîchissement est accepté avec la note
    [`FREEZE_UNPUBLISHED_MARK`] (geler puis pousser dans le même tour).

    `supersedes` (#2989) nomme un ou des gels antérieurs du même lot que ce
    gel **remplace** ; la note reçoit alors la ligne machine
    [`FREEZE_SUPERSEDES_MARK`]. La lecture des auteurs (`review_authors`) ignore
    un gel remplacé quand son SHA est absent du dépôt : c'est la réparation
    d'un gel fantôme (un SHA erroné écrit avant la validation de la CLI). Le
    remplacement est refusé si le SHA nommé se résout encore dans un dépôt
    connu : la marque ne vaut que pour un fantôme prouvé, et un gel réellement
    lisible ne s'efface pas. Une ligne de note fournie par l'appelant est
    retirée : la marque ne se contourne pas. Seul `frozen` accepte `supersedes`.

    `on_frozen` (facultatif, #1617) : le callback du gel, appelé dans la
    transaction du jalon `frozen` avec `(tx, milestone, after_commit)`. Il
    laisse `after_commit` recevoir les écritures de fil différées
    (`mail.send(..., after_commit=…)`) ; c'est **l'appelant** qui vide la liste
    après le COMMIT qu'il tient (celui du `with` interne sur une connexion
    racine, celui de sa propre transaction si l'appel est imbriqué : un `with`
    imbriqué ne commite pas).

    Absent (le défaut), le gel **active la désignation automatique des
    relecteurs** (#2447) quand l'hôte lit un canon et que l'appel tient sa
    transaction : le hook de `review_designation_activation` est installé, et
    la liste `after_commit` créée ici est vidée après le COMMIT interne. Sur
    une transaction déjà ouverte, l'appelant garde la main : il fournit son
    hook et sa liste, comme le contrat #1617 l'exige. Un callback explicite
    l'emporte toujours (témoins, reprise hors hook).

    `outcome` (facultatif) reçoit la forme de la désignation du gel par défaut,
    pour le message de la CLI. Avant d'installer ce hook, un gel dont la tête
    est couverte par un report de verdicts (#2442) est reconnu par
    `verdict_carry.would_carry` : la désignation s'abstient alors et garde les
    relecteurs reportés, sans courrier (#2487). Le plan est lu **avant** la
    transaction du jalon : la transaction ne fait aucune lecture réseau.

    Le feu ne s'écrit pas ici sans contrôle : `ameesh.fire.give` vérifie qu'il
    porte sur le gel courant, et `ameesh.fire.withdraw` qu'un feu est actif.
    """
    from .work import WorkError, get
    if kind not in MILESTONE_KINDS:
        raise WorkError("unknown milestone: %r (%s)" % (kind, ", ".join(MILESTONE_KINDS)))
    if kind not in MANUAL_KINDS:
        raise WorkError(
            "milestone %s is automatic (lot transition or closing): only %s "
            "are declared by hand" % (kind, ", ".join(MANUAL_KINDS)))
    if kind == "verdict":
        if verdict not in VERDICTS:
            raise WorkError("missing or unknown verdict: %r (%s)"
                            % (verdict, ", ".join(VERDICTS)))
    elif verdict is not None:
        raise WorkError("a verdict only makes sense for the `verdict` milestone")
    if on_frozen is not None and kind != "frozen":
        raise WorkError("a freeze callback only makes sense for the `frozen` milestone")
    replaced: list[int] = []
    if supersedes is not None:
        if kind != "frozen":
            raise WorkError("--supersedes only makes sense for the `frozen` milestone")
        values = [supersedes] if isinstance(supersedes, int) else list(supersedes)
        for value in values:
            try:
                freeze_id = int(value)
            except (TypeError, ValueError):
                raise WorkError("--supersedes expects a freeze milestone id, got %r"
                                % (value,))
            if freeze_id <= 0:
                raise WorkError("--supersedes expects a freeze milestone id, got %r"
                                % (value,))
            if freeze_id not in replaced:
                replaced.append(freeze_id)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    # #2971 : `--note` ne fournit aucune preuve machine de gel. Le retrait est
    # fait ici, avant les marques que le gate ajoute lui-même, pour que
    # `unverified`/`unpublished` (#2962) et `supersedes` (#2989) restent
    # écrites par le gate et ne soient jamais effacées par un appelant.
    # #2971 (revue impl-05) : `add_milestone` strippe la note finale. Un filtre
    # ligne à ligne sur `^[ \t]*` laissait donc une ligne machine précédée d'un
    # blanc Unicode (`\v`, `\f`, espace insécable) devenir valide après ce
    # strip ; retirer une autre ligne pouvait aussi l'exposer. On juge chaque
    # ligne sur sa forme strippée — exactement celle que la lecture verra — puis
    # on strippe l'ensemble : aucune marque fournie par `--note` ne survit.
    note = "\n".join(
        line for line in str(note or "").splitlines()
        if not FREEZE_MACHINE_LINE.match(line.strip())).strip()
    if kind == "verdict":
        # #2674 : refuser à l'écriture un avis qui ne nomme pas la tête relue.
        sha = reviewed_head(db, item, sha, repo=repo)
    elif kind == "frozen":
        # #2786 : écrire une tête immuable, jamais une ref que chaque lecteur
        # résoudrait dans son clone. Le cache de fetch est partagé avec la
        # marque (5) : la cible publiée n'est rafraîchie qu'une fois.
        refreshed: dict = {}
        freeze_info: dict = {}
        sha = freeze_head(db, item, sha, repo=repo, refreshed=refreshed,
                          outcome=freeze_info)
        # (5) : la marque des branches où le gel était absent, pour que le
        # balayage ne confonde pas une livraison (avance rapide) avec un gel
        # déjà dans la cible. C'est une preuve machine : une ligne fournie par
        # l'appelant (`--note`) est ignorée et refaite ici (#2786, avis dev4).
        # #2989 (B des revues cx2 et dev1) : `milestone` fait un `strip()`
        # final sur la note. Un filtre ligne à ligne sur `^[ \t]*` laissait
        # passer une ligne machine précédée d'un blanc Unicode (`\v`, `\f`,
        # espace insécable) ; retirer une autre ligne pouvait aussi l'exposer.
        # On juge donc chaque ligne sur sa forme strippée — exactement celle
        # que la lecture verra — puis on strippe l'ensemble : aucune ligne
        # machine (absence, remplacement) fournie par `--note` ne survit,
        # quel que soit le blanc.
        note = "\n".join(
            line for line in str(note or "").splitlines()
            if not line.strip().startswith(
                (FREEZE_ABSENT_MARK, FREEZE_SUPERSEDES_MARK))).strip()
        if replaced:
            # #2989 : la déclaration ne vaut que pour un gel antérieur du même
            # lot (la ligne machine est écrite ici, jamais recopiée).
            prior = {int(row.get("id") or 0): row
                     for row in milestones(db, item_id, None)}
            from . import freeze_checks
            known = ([repo] if repo else []) + _head_repos(db, item)
            for freeze_id in replaced:
                row = prior.get(freeze_id)
                if row is None or row.get("kind") != "frozen":
                    raise WorkError(
                        "--supersedes %s: not a prior `frozen` milestone of lot #%d"
                        % (freeze_id, item_id))
                replaced_sha = str(row.get("sha") or "").strip()
                # #2989 (N1 de la revue dev2) : la marque ne vaut que pour un
                # fantôme **prouvé**. Un gel encore lisible dans un dépôt connu
                # ne se remplace pas : la lecture le relit de toute façon, mais
                # si sa récupération échouait un jour (objets élagués, source
                # injoignable), la marque le ferait disparaître au lieu du
                # refus fermé B5. On refuse donc à l'écriture : la preuve est
                # positive (le commit existe), pas une absence devinée.
                if not replaced_sha:
                    continue
                for where in known:
                    if where and freeze_checks.commit_in(where, replaced_sha):
                        raise WorkError(
                            "--supersedes %s: freeze %s is readable in %s — only a "
                            "freeze absent from every known repository (a proven "
                            "ghost) can be replaced (#2989)"
                            % (freeze_id, replaced_sha[:12], where))
            note = (note + "\n" if note else "") + "\n".join(
                "%s %d" % (FREEZE_SUPERSEDES_MARK, freeze_id)
                for freeze_id in replaced)
        # #2962 : un gel distant sans dépôt d'auteur lisible est écrit avec sa
        # note ; un gel résolu mais pas encore poussé n'est pas refusé, mais
        # l'avertissement `freeze-unpublished` est explicite (note et sortie).
        if freeze_info.get("unverified"):
            note = ((note + "\n" if note else "")
                    + "%s no repository of the author is readable from this host; "
                      "%s is written unverified (freeze from the lot's worktree or "
                      "pass --repo)" % (FREEZE_UNVERIFIED_MARK, sha[:12]))
        else:
            warning = _freeze_unpublished(
                db, item, sha, repo, refreshed,
                where=str(freeze_info.get("repo") or ""))
            if warning:
                note = (note + "\n" if note else "") + warning
        marker = _freeze_absent_marker(db, item, sha, repo, refreshed)
        if marker:
            note = (note + "\n" if note else "") + marker
    drain: list | None = None
    if kind == "frozen" and on_frozen is None:
        from . import review_designation_activation as activation
        cfg = getattr(db, "cfg", None)
        if activation.enabled(cfg) and not _in_transaction(db):
            # #2487 : un gel réaligné dont les verdicts vont être reportés
            # n'appelle pas de nouvelle désignation. Le plan est lu avant la
            # transaction du jalon (lectures git/réseau hors verrou de ligne).
            carried = activation.carried_plan(cfg, db, item_id, sha)
            on_frozen = activation.hook(cfg, outcome, carried=carried)
            if after_commit is None:
                after_commit = []
                drain = after_commit
    # #1465 : un gel ou un verdict déclaré dans un tour managé emporte la
    # preuve exacte de son origine (acteur, session, tour, harnais, sha
    # complet). Hors tour (orchestrateur, humain), le jalon s'écrit sans
    # preuve : l'origine reste inconnue, jamais devinée par proximité.
    # #2928 : la preuve se résout dans le dépôt qui porte la tête (le canon
    # pour un lot `canon/**`), pas seulement dans le répertoire courant.
    from . import review_provenance
    proof = review_provenance.proof_for_current_turn(
        db, actor=actor, sha=sha, cwd=_proof_repo(db, item, sha, repo))
    if proof and not actor:
        actor = proof["actor"]
    row = storage.of(db).work.add_milestone(
        item_id, kind, sha=sha.strip(), actor=actor, verdict=verdict, note=note.strip(),
        on_frozen=on_frozen, after_commit=after_commit, proof=proof)
    if drain is not None:
        # the internal `with` has committed: the readable thread can leave now
        for record in drain:
            record(db.cfg, db)
    return row
