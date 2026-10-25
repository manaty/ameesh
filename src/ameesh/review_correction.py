# SPDX-License-Identifier: AGPL-3.0-only
"""#3416 — la place laissée aux avis avant un correctif de relecteur.

Le canon §2 fait de tout relecteur qui pousse un correctif un auteur de la
nouvelle tête (#2888) : sa famille quitte les familles éligibles. Sur un lot
sensible qui exige deux avis, une correction de premier tour peut donc ne plus
laisser qu'une seule famille et condamner la porte de fusion. Le cas réel
#3186 (auteur claude, deux avis, correcteur codex : seule deepseek restait) a
obligé à annuler le commit et à faire réécrire le correctif par l'auteur.

Ce module répond **avant** la poussée :

* `outlook` calcule, à partir des entrées d'une désignation, le nombre de
  familles qui resteraient éligibles si `reviewer` devenait auteur ;
* `check` relit ces entrées pour un lot (les mêmes que la désignation de son
  dernier gel) et rend l'état `ok`, `short` (« §2 : retour à l'auteur »),
  `unknown` (famille non prouvée) ou `unavailable` (lecture impossible) ;
* `warning` ne parle que pour un relecteur **désigné** et seulement si la
  correction est trop serrée : c'est le texte que la CLI affiche sur stderr
  quand ce relecteur rend un verdict `blocked`.

La lecture est prudente : une famille inconnue ne vaut jamais « il reste de la
place », et une lecture impossible se tait au lieu de retarder ou de refuser un
verdict. La désignation du gel suivant reste l'autorité ; ce module n'écrit
rien.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from . import review_designation as rd, work

#: la phrase du canon que le relecteur doit lire avant de pousser
BACK_TO_AUTHOR = "§2 : retour à l'auteur"
#: états rendus par `outlook` et `check`
STATE_ENOUGH = "ok"
STATE_TOO_TIGHT = "short"
STATE_UNSURE = "unknown"
STATE_UNAVAILABLE = "unavailable"


def _family_of(db, inputs, name: str) -> str:
    """La famille réelle de `name` dans les entrées de la désignation.

    Les candidats de la désignation portent déjà la famille de session
    (`registry.live_harness`) ; un relecteur manuel absent de cette liste
    (autre équipe, pilote retiré des candidats) est relu au registre, jamais
    déduit de son nom.
    """
    wanted = str(name or "").strip()
    for seat in inputs.candidates or ():
        if str(getattr(seat, "name", "") or "").strip() == wanted:
            return str(getattr(seat, "family", "") or "").strip().lower()
    from . import registry

    try:
        rows = registry.overview(db) or ()
    except Exception:  # noqa: BLE001 - registre illisible : famille inconnue
        return ""
    for row in rows:
        if str((row or {}).get("name") or "").strip() != wanted:
            continue
        return rd.live_family(row, families=inputs.policy.families)
    return ""


def outlook(*, required: int, reviewer: Mapping, authors: Iterable = (),
            candidates: Iterable = (), families, seats: Iterable = (),
            earlier: Iterable = ()) -> dict:
    """Les familles qui resteraient éligibles après le correctif de `reviewer`.

    `authors` sont les auteurs de la tête gelée, `seats` les relecteurs manuels
    qui couvrent déjà un avis, `earlier` les correcteurs antérieurs (déjà
    auteurs de la tête, #2739). La correction examinée ajoute `reviewer` à ces
    derniers : la sélection de désignation est rejouée avec la même politique,
    les mêmes candidats et les mêmes exclusions, ce qui donne la place réelle
    — disponibilité des agents comprise, pas une famille théorique.

    Rend `{state, required, reviewer, family, families, seats, remaining,
    missing, why}`. `short` porte `missing` (le nombre d'avis que la porte ne
    pourrait plus obtenir) et la phrase `§2 : retour à l'auteur` en tête de
    `why` ; `unknown` quand la famille du relecteur ou d'un auteur n'est pas
    prouvée — le doute ne dit jamais `ok`.
    """
    name = str((reviewer or {}).get("name") or "").strip()
    family = str((reviewer or {}).get("family") or "").strip().lower()
    need = max(int(required or 0), 0)
    out = {"state": STATE_ENOUGH, "required": need, "reviewer": name,
           "family": family, "families": [], "seats": [], "remaining": 0,
           "missing": 0, "why": ""}
    if need == 0:
        out["why"] = "aucun avis n'est requis sur cette tête"
        return out
    if not family or family in rd.PLACEHOLDER_FAMILIES:
        out.update({"state": STATE_UNSURE,
                    "why": "famille du relecteur %s inconnue : la place restante "
                           "ne peut pas être calculée" % (name or "?")})
        return out
    fixers = [dict(row or {}) for row in earlier or ()]
    fixers.append({"name": name, "family": family})
    try:
        outcome = rd.select(required=need, authors=list(authors or ()),
                            candidates=list(candidates or ()),
                            families=families, already=list(seats or ()),
                            fixers=fixers)
    except Exception as exc:  # noqa: BLE001 - entrées incohérentes : doute
        out.update({"state": STATE_UNSURE,
                    "why": "sélection illisible : %s" % (exc,)})
        return out
    out["families"] = [str(value) for value in outcome.get("families") or ()]
    out["seats"] = [dict(row or {}) for row in outcome.get("manual") or ()]
    out["remaining"] = len(out["families"]) + len(out["seats"])
    if outcome.get("state") == rd.DESIGNATED:
        out["why"] = ("%d famille(s) indépendante(s) restante(s) pour %d avis"
                      % (out["remaining"], need))
        return out
    if outcome.get("state") == rd.WAITING and rd.unknown_families(authors):
        out.update({"state": STATE_UNSURE,
                    "why": str(outcome.get("why") or "famille d'auteur inconnue")})
        return out
    out.update({
        "state": STATE_TOO_TIGHT,
        "missing": int(outcome.get("missing") or 0),
        "why": "%s — après un correctif de %s (%s), %s"
               % (BACK_TO_AUTHOR, name or "?", family,
                  outcome.get("why") or "les avis requis ne sont plus couverts"),
    })
    return out


def check(cfg, db, lot: int, reviewer: str, *, fetch: bool = True) -> dict:
    """La place restante pour `reviewer` sur le dernier gel du lot.

    Lit les mêmes entrées que la désignation (`prepare`) : diff gelé, politique
    du canon au commit audité, auteurs et correcteurs exacts, registre. Ne
    lève pas sur un refus attendu ; `unavailable` nomme la lecture qui a
    échoué, sans jamais la présenter comme un `ok`.
    """
    from . import review_designation_activation as activation

    name = str(reviewer or "").strip()
    try:
        inputs = activation.prepare(cfg, db, int(lot), fetch=fetch)
    except Exception as exc:  # noqa: BLE001 - lecture impossible : état nommé
        return {"state": STATE_UNAVAILABLE, "lot": int(lot), "reviewer": name,
                "families": [], "seats": [], "remaining": 0, "missing": 0,
                "why": "lecture de la désignation impossible : %s: %s"
                       % (type(exc).__name__, exc)}
    if isinstance(inputs, activation.Unavailable):
        return {"state": STATE_UNAVAILABLE, "lot": int(lot), "reviewer": name,
                "families": [], "seats": [], "remaining": 0, "missing": 0,
                "why": str(inputs.why or "désignation indisponible")}
    unknown = [str(value) for value in inputs.unknown_authors or ()]
    if unknown:
        return {"state": STATE_UNSURE, "lot": int(lot), "reviewer": name,
                "families": [], "seats": [], "remaining": 0, "missing": 0,
                "why": "provenance d'auteur inconnue pour : %s"
                       % ", ".join(unknown)}
    family = _family_of(db, inputs, name)
    out = outlook(required=int(inputs.policy.required),
                  reviewer={"name": name, "family": family},
                  authors=inputs.authors, candidates=inputs.candidates,
                  families=inputs.policy.families, seats=inputs.manual_seats,
                  earlier=inputs.correctors)
    out.update(lot=int(lot))
    return out


def warning(cfg, db, lot: int, actor: str) -> str:
    """L'avertissement d'un verdict bloquant rendu par un relecteur désigné.

    Vide pour l'auteur du lot (il corrige librement), pour un acteur qui n'est
    pas relecteur désigné de ce gel, et pour tout état qui n'est pas `short`.
    La lecture rafraîchit la cible hors transaction (`fetch=True`) : `prepare`
    en a besoin pour lire le diff gelé quand la cible est une branche distante
    (`review_target.resolve` refuse une cible non rafraîchie avec
    `fetch=False`), et un aperçu qui ne lit jamais la cible restait muet. Un
    échec de lecture rend le silence : le contrôle reste une aide, jamais une
    porte, et il ne refuse ni ne retarde le verdict au-delà du fetch borné.
    """
    name = (actor or "").strip()
    if not name:
        return ""
    from . import review_designation_activation as activation

    try:
        item = work.get(db, int(lot))
        if item is None:
            return ""
        freeze = activation._latest_freeze(db, int(lot))
        if freeze is None:
            return ""
        designated = activation._designated_reviewers(
            db, item, freeze, str(item.get("assignee") or ""))
        if name not in designated:
            return ""
        room = check(cfg, db, int(lot), name, fetch=True)
    except Exception:  # noqa: BLE001 - un aperçu ne bloque jamais le verdict
        return ""
    if room.get("state") != STATE_TOO_TIGHT:
        return ""
    return ("%s ; ne poussez pas ce correctif vous-même, rendez-le à l'auteur "
            "(aperçu : ameesh work reviewers %d --if-corrector %s)"
            % (room.get("why") or BACK_TO_AUTHOR, int(lot), name))
