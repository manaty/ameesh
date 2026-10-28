# SPDX-License-Identifier: AGPL-3.0-only
"""#3589 — refuser le gel d'un lot empilé qui recopie la tête gelée de son parent.

Un lot empilé se pose sur la **tête gelée** de son parent (`git rebase --onto
<parent gelé>`), pas sur la cible : sa plage de revue contient alors les
commits du parent, et la porte des membres (#3513) les écarte comme les
commits de la cible. Quand l'agent rebase à la main « sur main », deux
commits qui portent le même patch ont des SHA différents : les copies
redeviennent des commits tiers, de provenance inconnue ou ambiguë, et la
désignation des relecteurs échoue (H6→H8/#3506, 25/10).

Au gel d'un lot dont une dépendance — `depends` ouvert ou membre du même
paquet (#2443) — est un lot ouvert, la plage du gel est comparée aux commits
de la tête gelée du parent : si elle contient des commits de même patch-id que
ceux du parent **sans contenir cette tête**, le gel est refusé. Le message
donne la commande de ré-empilement, `git rebase --onto <tête gelée du parent>
<dernière copie> <branche>`. Le refus n'est donné que si la dernière copie
reproduit exactement l'arbre de la tête gelée du parent : le rebase proposé
est alors **à arbre identique**, et la recette le prouve.

La lecture est celle des autres preuves du dépôt : `git rev-list`, `git diff`
et `git patch-id --verbatim` (aucune normalisation d'espaces, comme
`plan_git`). Un patch-id indisponible, un dépôt superficiel, un parent dont la
tête n'est pas lisible localement ou une cible illisible **n'ajoutent pas de
refus** : le doute ne bloque pas un gel. Un lot sans dépendance ouverte n'est
même pas comparé — rien ne change pour un lot non empilé.

Le parent comparé est le plus proche : une tête dont une autre tête candidate
est l'ancêtre est écartée, et quand plusieurs blocs copiés correspondent, la
« dernière copie » la plus avancée l'emporte. Une chaîne `H6 → H7 → H8` ne
fait donc refuser H8 que sur H7, jamais sur H6 déjà porté par H7. Une fusion
dans les commits propres arrête aussi le refus : le rebase par défaut
l'aplatirait, l'arbre ne serait plus prouvé identique.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from . import plan_git, work

#: un lot terminé ne peut plus être un parent d'empilement
DONE_STATES = ("merged", "promoted", "closed")

#: cause du refus, dans la même forme que les autres refus de la porte du gel
STACKED_COPIES = "stacked-copies"


def _text(value) -> str:
    return str(value or "").strip()


def _sha(value) -> str:
    return _text(value).lower()


def _done(row) -> bool:
    return _text((row or {}).get("state")) in DONE_STATES


def _freeze_sha(db, lot: int) -> str:
    """Le SHA nommé par le dernier gel du lot (`""` s'il n'y en a pas)."""
    try:
        rows = work.milestones(db, int(lot))
    except Exception:  # noqa: BLE001 - jalons illisibles : pas de parent prouvé
        return ""
    freeze = plan_git.latest_freeze(rows)
    return _sha((freeze or {}).get("sha"))


def candidates(db, lot: int) -> list[dict]:
    """Les lots ouverts dont la tête gelée peut être le parent de `lot`.

    Rend `[{lot, source}]`, `source` valant `depends` (arête explicite) ou
    `packet` (membre du même paquet). Une base ou un graphe illisible ne
    fabrique aucun parent : le doute n'ajoute pas de refus.
    """
    try:
        lot = int(lot)
    except (TypeError, ValueError):
        return []
    found: dict[int, str] = {}
    try:
        from . import work_dependencies
        edges = work_dependencies.grouped(db, [lot]).get(lot, ())
    except Exception:  # noqa: BLE001 - graphe illisible : aucun parent
        edges = ()
    for edge in edges or ():
        try:
            parent = int((edge or {}).get("blocker_id") or 0)
        except (TypeError, ValueError):
            continue
        if parent and parent != lot and (edge or {}).get("blocking"):
            found.setdefault(parent, "depends")
    try:
        from . import packet_link
        memberships = packet_link.for_item(db, lot)
    except Exception:  # noqa: BLE001 - paquets illisibles : aucun parent
        memberships = ()
    for membership in memberships or ():
        if _text((membership or {}).get("package_state")) in DONE_STATES:
            continue
        try:
            package = int((membership or {}).get("package_id") or 0)
        except (TypeError, ValueError):
            continue
        if not package:
            continue
        try:
            members = packet_link.memberships(db, package_ids=[package])
        except Exception:  # noqa: BLE001 - adhésions illisibles : aucun parent
            continue
        for member in members or ():
            try:
                other = int((member or {}).get("work_item_id") or 0)
            except (TypeError, ValueError):
                continue
            if other and other != lot:
                found.setdefault(other, "packet")
    return [{"lot": parent, "source": found[parent]}
            for parent in sorted(found)]


def _target_commit(repo: str, target: str) -> str:
    """Le commit que nomme la cible, ou `""` : lecture locale, sans fetch."""
    text = _text(target)
    if not text:
        try:
            text = _text(plan_git.default_target(repo))
        except Exception:  # noqa: BLE001 - cible par défaut illisible
            text = ""
    if not text:
        return ""
    try:
        from . import review_authors
        return review_authors.target_commit(repo, text)
    except Exception:  # noqa: BLE001 - cible illisible : pas de base
        return ""


def _is_ancestor(repo: str, before: str, after: str) -> bool:
    proc = plan_git._git(repo, "merge-base", "--is-ancestor", before, after)
    return proc.returncode == 0


def _merge_base(repo: str, left: str, right: str) -> str:
    return plan_git._out(repo, "merge-base", left, right) or ""


def _tree(repo: str, commit: str) -> str:
    return plan_git._out(repo, "rev-parse", commit + "^{tree}") or ""


def _shallow(repo: str) -> bool:
    """Un clone superficiel ne prouve pas une plage : on ne conclut pas."""
    try:
        return plan_git._out(repo, "rev-parse", "--is-shallow-repository") == "true"
    except Exception:  # noqa: BLE001 - lecture impossible : la suite tranchera
        return False


def _commits_between(repo: str, base: str, head: str) -> list[str] | None:
    """Les commits de `base..head`, du plus ancien au plus récent."""
    text = plan_git._out(repo, "rev-list", "--topo-order", "--reverse",
                         head, "--not", base)
    if text is None:
        return None
    return [line.strip() for line in text.splitlines() if line.strip()]


def _single_parent(repo: str, commit: str) -> str:
    """Le parent unique de `commit`, ou `""` (fusion, racine, illisible)."""
    text = plan_git._out(repo, "rev-list", "--parents", "-n", "1", commit)
    if not text:
        return ""
    fields = text.split()
    return fields[1] if len(fields) == 2 else ""


def copy_prefix(repo: str, commits: Sequence[str],
                patch_ids: Mapping[str, str]) -> list[str]:
    """Le préfixe (du plus ancien au plus récent) de commits recopiés.

    Un commit est une copie quand son patch-id est celui d'un commit de la
    tête du parent. Le préfixe s'arrête au premier commit qui n'en est pas
    une : c'est le bloc hérité que `git rebase --onto` enlèvera, la « dernière
    copie » étant son dernier commit. Une fusion ou un patch-id illisible
    arrête la lecture : le doute ne fabrique pas de copie."""
    if not repo or not commits or not patch_ids:
        return []
    copies: list[str] = []
    for commit in commits:
        parent = _single_parent(repo, commit)
        if not parent:
            break
        try:
            patch = plan_git._diff_patch_id(repo, parent, commit)
        except Exception:  # noqa: BLE001 - patch-id illisible : pas de copie
            break
        if not patch or patch not in patch_ids:
            break
        copies.append(commit)
    return copies


def parent_heads(repo: str, parents: Iterable[Mapping]) -> list[dict]:
    """Les parents lisibles, réduits aux têtes les plus proches.

    `parents` : `[{lot, frozen, source, target}]`. Une tête dont une autre
    tête candidate est l'ancêtre est écartée : ce n'est pas la base immédiate
    de l'empilement (H6 derrière H7). Les têtes identiques sont dédoublonnées
    sur le plus petit numéro. Une tête absente du dépôt est ignorée.
    """
    readable: list[dict] = []
    for parent in parents or ():
        try:
            lot = int((parent or {}).get("lot") or 0)
        except (TypeError, ValueError):
            continue
        frozen = plan_git.local_commit(repo, _sha((parent or {}).get("frozen")))
        if not lot or not frozen:
            continue
        readable.append({"lot": lot, "frozen": frozen,
                         "source": _text((parent or {}).get("source")),
                         "target": _text((parent or {}).get("target"))})
    nearest: list[dict] = []
    for index, parent in enumerate(readable):
        hidden = False
        for other_index, other in enumerate(readable):
            if index == other_index:
                continue
            if other["frozen"] == parent["frozen"]:
                if other["lot"] < parent["lot"]:
                    hidden = True
                    break
                continue
            if _is_ancestor(repo, parent["frozen"], other["frozen"]):
                hidden = True
                break
        if not hidden:
            nearest.append(parent)
    return nearest


def find(repo: str, head: str, *, target: str = "",
         parents: Iterable[Mapping] = (), branch: str = "") -> dict | None:
    """Le refus d'empilement à donner, ou None si le gel peut passer.

    `parents` : les dépendances ouvertes candidates (`lot`, `frozen`,
    `source`). La plage du gel est lue depuis la base commune avec la cible
    (`target`, sinon la cible du dépôt). Rend le premier refus au sens de la
    « dernière copie » la plus proche de la tête, avec la commande exacte et
    la preuve d'arbre.
    """
    if not repo or not head:
        return None
    head_full = plan_git.local_commit(repo, head)
    if not head_full or _shallow(repo):
        return None
    nearest = parent_heads(repo, parents)
    if not nearest:
        return None
    target_commit = _target_commit(repo, target)
    if not target_commit:
        return None
    base = _merge_base(repo, head_full, target_commit)
    if not base or base == head_full:
        return None
    commits = _commits_between(repo, base, head_full)
    if not commits:
        return None
    found: list[dict] = []
    for parent in nearest:
        frozen = parent["frozen"]
        if _is_ancestor(repo, frozen, head_full):
            continue                      # empilé sur la tête gelée : accepté
        parent_target = _target_commit(repo, parent["target"]) or target_commit
        parent_base = _merge_base(repo, frozen, parent_target)
        if not parent_base:
            continue
        try:
            patch_ids = plan_git._patch_ids(
                repo, "%s..%s" % (parent_base, frozen))
        except Exception:  # noqa: BLE001 - patch-id illisible : pas de preuve
            continue
        copies = copy_prefix(repo, commits, patch_ids)
        if not copies:
            continue
        last = copies[-1]
        # La dernière copie doit reproduire l'arbre de la tête gelée du
        # parent : c'est la preuve que le bloc hérité EST ce parent, et que
        # le rébase proposé laisse l'arbre identique. Un bloc plus récent
        # (autre parent, tête regelée depuis) n'accuse pas celui-ci ; deux
        # arbres illisibles ne prouvent rien.
        last_tree = _tree(repo, last)
        frozen_tree = _tree(repo, frozen)
        if not last_tree or last_tree != frozen_tree:
            continue
        # Après la dernière copie, un `rebase` par défaut aplatit les
        # fusions : une résolution de conflit disparaîtrait de l'arbre. La
        # promesse « arbre identique » ne vaut que pour une suite linéaire.
        merges = plan_git._out(repo, "rev-list", "--merges",
                               "%s..%s" % (last, head_full))
        if merges is None or merges.strip():
            continue
        found.append({
            "parent": parent["lot"], "source": parent["source"],
            "frozen": frozen, "last_copy": last, "copies": copies,
            "branch": _text(branch) or "HEAD", "head": head_full,
        })
    if not found:
        return None
    return max(found, key=lambda item: (len(item["copies"]), -item["parent"]))


def message(refusal: Mapping) -> str:
    """Le refus nommé, avec la recette de ré-empilement et sa preuve d'arbre."""
    copies = list(refusal.get("copies") or ())
    shown = ", ".join(commit[:12] for commit in copies[:8])
    if len(copies) > 8:
        shown += ", …"
    frozen = _sha(refusal.get("frozen"))
    last = _sha(refusal.get("last_copy"))
    branch = _text(refusal.get("branch")) or "HEAD"
    source = _text(refusal.get("source")) or "dependency"
    lines = [
        "freeze refused (%s): the frozen range contains %d commit(s) whose "
        "patch-id equals a commit of the frozen head of open parent lot #%s "
        "(%s, %s), without containing that head: %s"
        % (STACKED_COPIES, len(copies), int(refusal.get("parent") or 0),
           frozen[:12], source, shown),
        "Re-stack the lot on the parent's frozen head and freeze again:",
        "  git rebase --onto %s %s %s" % (frozen, last, branch),
    ]
    lines.append(
        "The last copy reproduces the parent head's tree, so this rebase "
        "leaves the lot's tree identical; check it with `git diff %s HEAD` "
        "afterwards (empty when the trees are identical)."
        % _sha(refusal.get("head"))[:12])
    return "\n".join(lines)


def guard(db, item, head: str, *, repo: str | None = None,
          target: str = "") -> str:
    """Le garde du gel : lève [`work.WorkError`] sur des copies de parent.

    Rend `""` quand rien n'est prouvé (lot non empilé, parent sans gel, tête
    ou cible illisible, patch-id indisponible). Aucune écriture.
    """
    lot = int((item or {}).get("id") or 0)
    if not repo or not head or not lot:
        return ""
    parents: list[dict] = []
    for candidate in candidates(db, lot):
        try:
            row = work.get(db, candidate["lot"])
            if row is None or _done(row):
                continue
            frozen = _freeze_sha(db, candidate["lot"])
            if not frozen:
                continue
            parents.append({"lot": candidate["lot"], "frozen": frozen,
                            "source": candidate["source"],
                            "target": _text(row.get("branch_target"))})
        except Exception:  # noqa: BLE001 - un parent illisible n'est pas une preuve
            continue
    if not parents:
        return ""
    fence = find(repo, head, target=target or _text((item or {}).get("branch_target")),
                 parents=parents, branch=_text((item or {}).get("branch")))
    if fence is None:
        return ""
    raise work.WorkError(message(fence))
