# SPDX-License-Identifier: AGPL-3.0-only
"""#3742/#7745 — refuser le gel d'une tête que ses auteurs rendent non relisable (§2).

Le canon §2 exclut de la relecture la famille de l'auteur d'un delta, et pour
deux avis exige deux familles distinctes **de celle de ce delta**. Depuis
#7745, la famille d'un commit n'interdit plus de relire les autres commits de
la même tête : chaque commit, ou chaque delta contigu d'un même auteur, reçoit
les avis requis de familles distinctes de la sienne. Une tête à deux familles
d'auteurs reste donc validable avec trois familles ; auparavant, la famille de
tout auteur était exclue de toute la tête et la désignation rendait « 0 eligible
families » (Nexlink #3725, #7678 ; ameesh #7743).

Quand les familles de la politique ne couvrent pas un delta, aucune
désignation ne peut rendre la tête relisable : la porte de fusion restera
fermée quoi qu'on écrive. Le gel est donc refusé **avant** d'écrire le jalon,
avec les commits à renvoyer à l'assigné.

La décision se prend sur les entrées de la désignation déjà lues
(`review_designation_activation.prepare`) : mêmes auteurs, mêmes correcteurs,
mêmes familles, même nombre d'avis, et les drapeaux compilés
`exclude_author_family` / `distinct_families`. Les familles par commit sont
celles de `review_authors.reading` (`commit_families`, `global_families`) ;
aucun nombre n'est en dur.

La lecture est prudente :

* une entrée indisponible (pas de dépôt, pas de politique compilée) ne refuse
  pas : le doute n'est pas une preuve d'impossibilité, et un gel distant reste
  permis ;
* une famille d'auteur inconnue n'est pas supposée : la borne calculée est le
  **maximum** de familles éligibles (les inconnues ne peuvent qu'en retirer).
  Elle ne refuse que si même ce maximum est sous le besoin ;
* les brouillons de tests seuls (`test_only`, #3518/#3577) n'excluent aucune
  famille : ils ne sont pas des auteurs du contenu produit ;
* les commits d'un **prérequis déclaré** (`work dependencies`) dont une tête
  gelée précède celle du lot sont hors de la lecture d'auteurs (#4343) : ils
  sont relus dans le parent, et leur provenance inconnue ne bloque pas
  l'enfant ;
* une famille **globale** — un commit réattribué (#7324) ou un auteur de gel
  antérieur dont le contenu a été réécrit (#2924) — reste interdite pour tous
  les deltas : on ne lave pas une provenance en découpant la tête ;
* une dispense `-none` (`waiver`) ou un gel reporté (#2487) ne passe pas ici :
  la désignation du gel garde son cours, comme avant.

Le module n'écrit rien : `enforce` lève [`work.WorkError`] et la transaction du
jalon, tenue par `add_milestone`, est annulée avec lui.
"""
from __future__ import annotations

from typing import Mapping

from . import lot_class, review_deltas as rdl, review_designation as rd, work

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


def _deltas_of(inputs):
    """Les familles d'auteur de chaque commit, ou None quand la lecture manque.

    Une lecture absente garde la règle historique (familles de **tous** les
    auteurs exclues) : la porte et la désignation ont leur propre repli, et un
    lot distant ne doit pas être refusé sur un doute.
    """
    # L'ordre de `commit_families` est celui de la tête relue (la lecture a
    # déjà filtré les commits dans la cible) : il fait foi, pas la liste brute
    # des commits de la plage.
    by_commit = getattr(inputs, "commit_families", None) or {}
    if not by_commit:
        return None
    return rdl.delta_families(list(by_commit), by_commit,
                              getattr(inputs, "global_families", ()) or ())


def _strict(policy, required: int) -> bool:
    flags = tuple(getattr(policy, "review_independence", ()) or ())
    if len(flags) >= 2:
        exclude_author_family = bool(flags[0])
    else:
        exclude_author_family = bool(getattr(policy, "exclude_author_family", True))
    risk = _lower(getattr(policy, "risk", ""))
    return (required >= 2 or risk in (lot_class.LIGHT, lot_class.SENSITIVE)
            or exclude_author_family)


def _distinct(policy) -> bool:
    flags = tuple(getattr(policy, "review_independence", ()) or ())
    if len(flags) >= 2:
        return bool(flags[1])
    return bool(getattr(policy, "distinct_families", True))


def _delta_why(required: int, deltas, gaps, eligible, forbidden) -> str:
    text = ("%d independent review(s) per delta; %d delta(s); eligible: %s; "
            "forbidden for every delta: %s"
            % (required, len(deltas), ", ".join(eligible) or "none",
               ", ".join(forbidden) or "none"))
    if gaps:
        text += ("; uncovered: %s" % ", ".join(
            "delta %d (%s): %d missing"
            % (gap["index"] + 1,
               ", ".join(gap["families"]) or "no author family",
               gap["missing"]) for gap in gaps))
    return text


def _assess_deltas(inputs, required: int, families, seats, rows, deltas,
                   distinct: bool) -> dict:
    """La règle par delta : chaque commit a ses `required` autres familles.

    Les familles des sièges déjà pourvus comptent pour les deltas qu'elles
    n'ont pas écrits. `minimal_seats` rend le plus petit ensemble à ajouter,
    ou None quand aucun sous-ensemble de la politique ne couvre la tête.
    """
    commits = list(getattr(inputs, "commit_families", {}) or {})
    seat_families = sorted({seat["family"] for seat in seats})
    available = [family for family in families if family not in seat_families]
    target = rdl.minimal_seats(deltas, available, required,
                               already=seat_families, distinct=bool(distinct))
    author_families = sorted({family for delta in deltas for family in delta})
    common: set = set(deltas[0]) if deltas else set()
    for delta in deltas[1:]:
        common &= set(delta)
    forbidden = sorted(common)
    eligible = sorted({family for family in available if family not in common})
    gaps = rdl.uncovered(deltas, seat_families, required,
                         distinct=bool(distinct))
    reachable = rdl.uncovered(deltas, list(available) + seat_families, required,
                              distinct=bool(distinct))
    out = {
        "state": "ok" if target is not None else "short",
        "cause": CAUSE,
        "required": required,
        "need": (len(target) if target is not None else
                 max((gap["missing"] for gap in reachable),
                     default=max(required - len(seats), 0))),
        "families": sorted(set(families)),
        "forbidden": forbidden,
        "author_families": author_families,
        "eligible": eligible,
        "manual_seats": seats,
        "authors": [dict(entry, commits=_commits_of(inputs, entry["name"]))
                    for _key, entry in sorted(rows.items())],
        "unknown_authors": sorted(str(value) for value in
                                  (getattr(inputs, "unknown_authors", ()) or ())),
        "assignee": str((getattr(inputs, "item", {}) or {}).get("assignee") or "").strip(),
        "deltas": [
            {"sha": sha, "families": sorted(delta),
             "missing": next((gap["missing"] for gap in reachable
                              if gap["index"] == index), 0)}
            for index, (sha, delta) in enumerate(zip(commits, deltas))],
        "why": "",
    }
    if out["state"] == "short":
        out["why"] = _delta_why(required, deltas, reachable, eligible, forbidden)
    return out


def assess(inputs) -> dict:
    """Le besoin d'avis est-il couvert par les familles restées éligibles ?

    Rend `{state, cause, required, need, families, forbidden, eligible,
    manual_seats, authors, unknown_authors, assignee, why}`, plus
    `author_families` (toutes les familles prouvées) et `deltas` quand la
    lecture par commit est disponible. `state` vaut `ok`, ou `short` quand
    aucun ensemble de familles de la politique ne peut donner à chaque delta
    ses avis indépendants. Une lecture indisponible ne produit pas d'`Inputs` :
    l'appelant ne refuse jamais sur un doute.
    """
    policy = getattr(inputs, "policy", None)
    required = int(getattr(policy, "required", 0) or 0)
    families = [_lower(value) for value in (getattr(policy, "families", ()) or ())
                if _lower(value)]
    seats = _seats(inputs)
    rows = _rows(inputs)
    strict = _strict(policy, required)
    distinct = _distinct(policy)
    deltas = _deltas_of(inputs) if strict and required > 0 else None
    if deltas is not None:
        return _assess_deltas(inputs, required, families, seats, rows, deltas,
                              distinct)
    # Repli historique : la famille de tout auteur est exclue de toute la tête
    # (`exclude_author_family` et deux avis la rendent stricte même à un avis
    # sur une classe normale).
    forbidden = set()
    if strict:
        for entry in rows.values():
            forbidden.update(entry["families"])
    forbidden.discard("")
    available = [family for family in families if family not in forbidden]
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
        "author_families": sorted(forbidden),
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
    n'est proposé : la tête serait refusée par la porte de fusion. #7324 :
    « rendre les commits » a été lu comme « les signer au nom de l'assigné »
    (nexlink #7272) ; le message dit que l'assigné les **refait** et que
    réécrire l'identité Git est interdit.
    """
    verdict = dict(verdict or assess(inputs))
    assignee = str(verdict.get("assignee") or "").strip()
    target = ("assignee %s" % assignee) if assignee else "assignee"
    # Le filtre des auteurs nommés reste celui des familles **déclarées** par
    # la politique : une famille d'auteur hors politique n'est pas la cause du
    # refus, c'est la politique qui manque de familles. Par delta, seuls les
    # deltas découverts nomment leurs auteurs : un delta couvert n'a pas à
    # être refait.
    declared = set(verdict.get("families") or ())
    forbidden = set(verdict.get("forbidden") or ())
    deltas = list(verdict.get("deltas") or ())
    if deltas:
        wanted = {family for delta in deltas if delta.get("missing")
                  for family in delta.get("families") or ()}
        authors = [row for row in verdict.get("authors") or ()
                   if set(row.get("families") or ()) & wanted & declared]
    else:
        authors = [row for row in verdict.get("authors") or ()
                   if set(row.get("families") or ()) & forbidden & declared]
    if deltas:
        lines = [
            "freeze refused (%s): %d required review(s) per delta; the head's %d"
            " delta(s) cannot each be covered by families other than their author's"
            " — %s; §2: retour à l'auteur."
            % (CAUSE, int(verdict.get("required") or 0), len(deltas),
               verdict.get("why") or "the head cannot be reviewed"),
            "The uncovered deltas must be redone by %s (or an agent of their"
            " family), then the head frozen again; the Git author or committer"
            " cannot be rewritten to change who wrote it (#7324):" % target,
        ]
        for delta in deltas:
            if not delta.get("missing"):
                continue
            lines.append("  - delta %s (%s): %d independent review(s) missing"
                         % (str(delta.get("sha") or "")[:12],
                            ", ".join(delta.get("families") or ())
                            or "no author family",
                            int(delta.get("missing") or 0)))
    else:
        lines = [
            "freeze refused (%s): %d required review(s), %d still to designate; the "
            "proven author families leave %d eligible family(ies) — %s; §2: retour "
            "à l'auteur."
            % (CAUSE, int(verdict.get("required") or 0),
               int(verdict.get("need") or 0),
               len(verdict.get("eligible") or ()),
               verdict.get("why") or "the head cannot be reviewed"),
            "The %s (or an agent of their family) must redo these commits in "
            "their own turn, then freeze again; rewriting the Git author or "
            "committer does not change who wrote them and is forbidden (#7324):"
            % target,
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
    if forbidden and deltas:
        lines.append("families forbidden for every delta: %s"
                     % ", ".join(sorted(forbidden)))
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
