# SPDX-License-Identifier: AGPL-3.0-only
"""#7745 — l'indépendance des avis se lit par delta d'auteur.

Le canon §2 n'exige pas que les deux familles soient distinctes de la famille de
**tout** auteur de la tête : chaque commit, ou chaque delta contigu d'un même
auteur, reçoit le nombre d'avis requis de familles distinctes de la sienne,
portant sur une tête qui le contient. Une tête qui mêle deux familles d'auteurs
reste donc validable avec trois familles : chaque delta est relu par les deux
autres, et une même famille peut relire les deltas qu'elle n'a pas écrits.

Ce module porte la règle unique lue par le gel (`freeze_families.assess`), la
désignation (`review_designation.select`) et la porte de fusion
(`merge_queue.review_gate`). Il ne lit ni base ni dépôt : les familles par
commit viennent de `review_authors.reading` (`commit_families_out`), les
familles « globales » — celles qu'on ne peut rattacher à aucun commit de la
tête, un commit réattribué (#7324) ou un auteur de gel antérieur dont le
contenu a été réécrit (#2924) — restent interdites pour **tous** les deltas.

Aucun nombre n'est en dur : `required` est le compte lu dans la politique
compilée, les familles sont celles qu'elle déclare.
"""
from __future__ import annotations

from itertools import combinations
from typing import Iterable

#: une famille de relecture au plus par delta quand les familles doivent être
#: distinctes ; `select` ne désigne de toute façon qu'un agent par famille.
DISTINCT = True


def _family(value) -> str:
    return str(value or "").strip().lower()


def _families(values: Iterable) -> frozenset:
    return frozenset(family for family in (_family(value) for value in values or ())
                     if family)


def delta_families(commits: Iterable, by_commit, global_families: Iterable = ()):
    """Les familles d'auteur de chaque commit gelé, dans l'ordre de la tête.

    `by_commit` est la lecture de `review_authors.reading` :
    `{sha: {famille, ...}}`. Une famille **globale** (un commit réattribué que
    la provenance ne rattache à aucun commit, #7324, ou un auteur de gel
    antérieur dont le contenu survit sous une autre écriture, #2924) s'ajoute à
    chaque delta : elle ne se lave pas par un découpage de la tête.

    Rend `None` quand un commit de la tête n'a pas de lecture : l'appelant
    échoue fermé, il ne suppose jamais une famille (canon §2). Un commit sans
    famille prouvée — un humain, une adaptation de tests seule — rend un
    ensemble vide : aucun relecteur n'est exclu par la famille pour ce delta.
    """
    glob = _families(global_families)
    out = []
    for raw in commits or ():
        sha = str(raw or "").strip()
        if not sha:
            continue
        proven = (by_commit or {}).get(sha)
        if proven is None:
            return None
        out.append(_families(proven) | glob)
    return out


def eligible_families(delta, reviewer_families: Iterable, *, distinct: bool = DISTINCT):
    """Les familles de relecteurs qui peuvent relire ce delta.

    La famille de l'auteur du delta est exclue ; les autres sont éligibles, y
    compris la famille d'un auteur d'un **autre** delta. Sans `distinct`, deux
    avis de la même famille comptent (la politique compilée l'a permis), sinon
    une famille ne fournit qu'un avis.
    """
    families = [_family(value) for value in reviewer_families or ()]
    families = [family for family in families if family and family not in delta]
    if distinct:
        return sorted(set(families))
    return families


def uncovered(deltas: Iterable, reviewer_families: Iterable, required: int,
              *, distinct: bool = DISTINCT) -> list[dict]:
    """Les deltas que les avis retenus ne couvrent pas encore.

    Chaque entrée est `{index, families, eligible, missing}` : `index` est le
    rang du commit dans la tête, `families` les familles d'auteur du delta,
    `eligible` les familles de relecture recevables et `missing` le nombre
    d'avis indépendants qui manquent. Rendre les manques permet à la porte de
    refuser en nommant le delta, au lieu de dire « famille d'auteur » pour un
    avis qui compte sur les autres deltas.
    """
    required = max(int(required or 0), 0)
    if required == 0:
        return []
    out = []
    for index, delta in enumerate(deltas or ()):
        got = eligible_families(delta, reviewer_families, distinct=distinct)
        if len(got) < required:
            out.append({"index": index, "families": sorted(delta),
                        "eligible": got, "missing": required - len(got)})
    return out


def covered(deltas: Iterable, reviewer_families: Iterable, required: int,
            *, distinct: bool = DISTINCT) -> bool:
    """Tous les deltas ont-ils leurs `required` avis de familles recevables ?"""
    return not uncovered(deltas, reviewer_families, required, distinct=distinct)


def minimal_seats(deltas: Iterable, available: Iterable, required: int, *,
                  already: Iterable = (), distinct: bool = DISTINCT):
    """Le plus petit ensemble de familles à ajouter pour couvrir tous les deltas.

    `already` sont les familles des sièges déjà pourvus (manuels ou d'un gel
    antérieur), `available` les familles des candidats disponibles. Rend la
    liste des familles à ajouter, ou `None` quand aucun sous-ensemble de
    `available` ne couvre la tête : le gel ne peut pas être relu et le dit.

    Le balayage est borné : les familles d'une politique compilée sont peu
    nombreuses (trois dans le canon ameesh) et `required` vaut 1 ou 2. L'ordre
    de `available` fait foi pour départager deux couvertures de même taille :
    l'appelant y met l'ordre de préférence (plan, charge, nom).
    """
    required = max(int(required or 0), 0)
    if required == 0:
        return []
    seats = [_family(value) for value in already or () if _family(value)]
    pool = []
    seen = set(seats)
    for raw in available or ():
        family = _family(raw)
        if not family or family in seen:
            continue
        seen.add(family)
        pool.append(family)
    # Un delta vide (un humain, une adaptation de tests seule) n'exclut aucune
    # famille : les sièges déjà pourvus peuvent suffire.
    if covered(deltas, seats, required, distinct=distinct):
        return []
    if distinct:
        target: tuple = ()
        for size in range(1, len(pool) + 1):
            for combo in combinations(pool, size):
                if covered(deltas, seats + list(combo), required, distinct=distinct):
                    target = combo
                    break
            if target:
                return list(target)
        return None
    # Sans familles distinctes, plusieurs avis peuvent venir de la même
    # famille. On cherche le plus petit nombre d'avis, famille par famille :
    # chaque siège ajouté est une famille, et une famille peut être répétée.
    for size in range(1, len(pool) + 1):
        for combo in combinations(pool + pool, size):
            if covered(deltas, seats + list(combo), required, distinct=False):
                return list(combo)
    return None
