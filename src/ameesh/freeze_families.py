# SPDX-License-Identifier: AGPL-3.0-only
"""#3742 — refuser le gel d'une tête que ses auteurs rendent non relisable (§2).

Le canon §2 exclut de la relecture la famille de tout auteur de la tête, et
pour deux avis exige deux familles distinctes. Quand les familles des auteurs
prouvés, **correcteurs compris** (#2888), laissent moins de familles éligibles
que d'avis requis, aucune désignation ne peut couvrir la tête : la porte de
fusion restera fermée quoi qu'on écrive. Le cas réel (Nexlink #3725) était un
lot à trois familles d'auteurs et deux avis requis : la désignation du gel a
rendu « 0 eligible families » et l'orchestrateur a dû désigner à la main des
relecteurs des familles des auteurs, un geste contraire au canon.

Le gel est donc refusé **avant** d'écrire le jalon, avec les commits à
renvoyer à l'assigné. La décision se prend sur les entrées de la désignation
déjà lues (`review_designation_activation.prepare`) : mêmes auteurs, mêmes
correcteurs, mêmes familles, même nombre d'avis, et les drapeaux compilés
`exclude_author_family` / `distinct_families`. Aucun nombre n'est en dur.

La lecture est prudente :

* une entrée indisponible (pas de dépôt, pas de politique compilée) ne refuse
  pas : le doute n'est pas une preuve d'impossibilité, et un gel distant reste
  permis ;
* une famille d'auteur inconnue n'est pas supposée : la borne calculée est le
  **maximum** de familles éligibles (les inconnues ne peuvent qu'en retirer).
  Elle ne refuse que si même ce maximum est sous le besoin ;
* les brouillons de tests seuls (`test_only`, #3518/#3577) n'excluent aucune
  famille : ils ne sont pas des auteurs du contenu produit ;
* une dispense `-none` (`waiver`) ou un gel reporté (#2487) ne passe pas ici :
  la désignation du gel garde son cours, comme avant.

Le module n'écrit rien : `enforce` lève [`work.WorkError`] et la transaction du
jalon, tenue par `add_milestone`, est annulée avec lui.
"""
from __future__ import annotations

from typing import Mapping

from . import lot_class, review_designation as rd, work

#: cause d'un refus, nommée dans le message
CAUSE = "author-families"
#: commits affichés au plus dans un refus : au-delà, la liste est tronquée
MAX_COMMITS = 40


def _lower(value) -> str:
    return str(value or "").strip().lower()


def _rows(inputs) -> dict:
    """Les porteurs de famille : `{nom: {name, families, corrector, test_only}}`.

    `authors` porte déjà les correcteurs promus par #2888 ; `correctors` garde
    la trace des correctifs de premier tour, y compris les tests seuls. Les
    deux listes sont fusionnées par nom pour ne pas compter deux fois une
    famille et pour ne pas perdre la marque « correction de relecteur ».
    """
    out: dict = {}
    for source, corrector in ((getattr(inputs, "authors", ()) or (), False),
                              (getattr(inputs, "correctors", ()) or (), True)):
        for raw in source:
            row = dict(raw or {}) if isinstance(raw, Mapping) else {"name": str(raw or "")}
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            proven = rd.proven_families(row)
            if not proven:
                continue
            key = name.lower()
            entry = out.get(key)
            if entry is None:
                entry = {"name": name, "families": set(), "corrector": False}
                out[key] = entry
            entry["families"].update(proven)
            entry["corrector"] = entry["corrector"] or corrector
    return out


def _commits_of(inputs, name: str) -> list:
    """Les SHA attribués à `name` par la lecture des auteurs (#3742)."""
    mapping = getattr(inputs, "author_commits", None) or {}
    wanted = str(name or "").strip().lower()
    for key, values in mapping.items():
        if str(key or "").strip().lower() == wanted:
            return [str(value) for value in values or () if str(value or "").strip()]
    return []


def _seats(inputs) -> list:
    seats = []
    for raw in getattr(inputs, "manual_seats", ()) or ():
        row = dict(raw or {}) if isinstance(raw, Mapping) else {}
        family = _lower(row.get("family"))
        if family:
            seats.append({"name": str(row.get("name") or "").strip(),
                          "family": family})
    return seats


def assess(inputs) -> dict:
    """Le besoin d'avis est-il couvert par les familles restées éligibles ?

    Rend `{state, cause, required, need, families, forbidden, eligible,
    manual_seats, authors, unknown_authors, assignee, why}`. `state` vaut
    `ok`, ou `short` quand même le maximum de familles éligibles (familles de
    la politique moins les familles d'auteurs **prouvées**) ne couvre pas les
    avis encore à désigner. Une lecture indisponible ne produit pas d'`Inputs` :
    l'appelant ne refuse jamais sur un doute.
    """
    policy = getattr(inputs, "policy", None)
    required = int(getattr(policy, "required", 0) or 0)
    families = [_lower(value) for value in (getattr(policy, "families", ()) or ())
                if _lower(value)]
    seats = _seats(inputs)
    rows = _rows(inputs)
    # La porte de fusion (`merge_queue.review_gate`) exclut la famille d'un
    # auteur dès deux avis, et pour un lot léger ou sensible même à un seul :
    # `exclude_author_family` n'étend cette stricte aux autres classes. Le
    # gel applique la même règle, avec la classe et le drapeau compilés.
    risk = _lower(getattr(policy, "risk", ""))
    strict = (required >= 2 or risk in (lot_class.LIGHT, lot_class.SENSITIVE)
              or bool(getattr(policy, "exclude_author_family", True)))
    forbidden = set()
    if strict:
        for entry in rows.values():
            forbidden.update(entry["families"])
    forbidden.discard("")
    available = [family for family in families if family not in forbidden]
    distinct = bool(getattr(policy, "distinct_families", True))
    if distinct:
        seat_families = {seat["family"] for seat in seats}
        pool = [family for family in available if family not in seat_families]
        need = max(required - len(seats), 0)
    else:
        # Deux avis peuvent venir de la même famille : une famille non
        # interdite suffit à couvrir tous les sièges encore à désigner.
        pool = available
        need = 1 if required > len(seats) else 0
    out = {
        "state": "short" if need > len(pool) else "ok",
        "cause": CAUSE,
        "required": required,
        "need": need,
        "families": sorted(set(families)),
        "forbidden": sorted(forbidden),
        "eligible": sorted(set(pool)),
        "manual_seats": seats,
        "authors": [dict(entry, commits=_commits_of(inputs, entry["name"]))
                    for _key, entry in sorted(rows.items())],
        "unknown_authors": sorted(str(value) for value in
                                  (getattr(inputs, "unknown_authors", ()) or ())),
        "assignee": str((getattr(inputs, "item", {}) or {}).get("assignee") or "").strip(),
        "why": "",
    }
    if out["state"] == "short":
        out["why"] = ("%d independent review(s) still to designate; eligible: %s; "
                      "forbidden: %s"
                      % (need, ", ".join(out["eligible"]) or "none",
                         ", ".join(out["forbidden"]) or "none"))
    return out


def message(inputs, verdict: Mapping | None = None) -> str:
    """Le refus de gel : familles, besoin, assigné et commits à renvoyer.

    Le message nomme l'assigné, chaque famille d'auteur prouvée et les commits
    que la lecture lui attribue (plage courante et gels antérieurs reportés).
    Il rappelle la phrase du §2 (« retour à l'auteur »). Aucun contournement
    n'est proposé : la tête serait refusée par la porte de fusion.
    """
    verdict = dict(verdict or assess(inputs))
    assignee = str(verdict.get("assignee") or "").strip()
    target = ("assignee %s" % assignee) if assignee else "the assignee"
    forbidden = set(verdict.get("forbidden") or ())
    declared = set(verdict.get("families") or ())
    authors = [row for row in verdict.get("authors") or ()
               if set(row.get("families") or ()) & forbidden & declared]
    lines = [
        "freeze refused (%s): %d required review(s), %d still to designate; the "
        "proven author families leave %d eligible family(ies) — %s; §2: retour "
        "à l'auteur."
        % (CAUSE, int(verdict.get("required") or 0),
           int(verdict.get("need") or 0),
           len(verdict.get("eligible") or ()),
           verdict.get("why") or "the head cannot be reviewed"),
        "Return these commits to the %s, then freeze again:" % target,
    ]
    shown = 0
    for row in authors:
        names = ", ".join(sorted(row.get("families") or ()))
        role = " (reviewer correction)" if row.get("corrector") else ""
        commits = [str(sha) for sha in row.get("commits") or () if str(sha).strip()]
        if not commits:
            lines.append("  - %s (%s)%s: no commit attributed in this read"
                         % (row.get("name") or "?", names, role))
            continue
        for sha in commits:
            if shown >= MAX_COMMITS:
                lines.append("  - … (list truncated at %d commits)" % MAX_COMMITS)
                return "\n".join(lines)
            lines.append("  - %s — %s (%s)%s"
                         % (sha[:12], row.get("name") or "?", names, role))
            shown += 1
    if not authors:
        lines.append("  - no proven author commit consumes a family: the policy "
                     "itself declares fewer families than reviews")
    if verdict.get("manual_seats"):
        lines.append("manual seats already counted: %s" % ", ".join(
            "%s (%s)" % (seat.get("name") or "?", seat.get("family") or "?")
            for seat in verdict["manual_seats"]))
    if verdict.get("unknown_authors"):
        lines.append("author families still unknown (not assumed): %s"
                     % ", ".join(verdict["unknown_authors"]))
    return "\n".join(lines)


def enforce(inputs) -> dict:
    """Refuse le gel si la tête ne peut structurellement pas être relue.

    Rend l'évaluation (`ok`) ou lève [`work.WorkError`] avec le message qui
    nomme les commits et les auteurs à renvoyer à l'assigné. Aucune écriture ;
    l'exception annule la transaction du jalon `frozen`.
    """
    verdict = assess(inputs)
    if verdict["state"] == "short":
        raise work.WorkError(message(inputs, verdict))
    return verdict
