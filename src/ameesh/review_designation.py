# SPDX-License-Identifier: AGPL-3.0-only
"""#1617 — désignation automatique des relecteurs au gel (canon §7, #1606 point 4).

Le canon §2 demande, pour un lot sensible gelé, « une relecture par une autre
famille de modèle (deux pour SQL, sécurité ou prod) ». Jusqu'ici un agent
choisissait le relecteur à la main, et le 15/10 dix-huit lots attendaient sans
relecteur, jusqu'à 42 h. Ce module fait la **sélection** : il prend la
politique versionnée (`review_policies.train_reviews`, lue par #1616/#1467),
la tête gelée exacte et le registre, et rend soit des relecteurs désignés avec
leur courrier, soit un **état d'attente explicite** avec une notification de
capacité — jamais une admission implicite.

Ce que le module ne fait pas, volontairement :

- il ne relit ni le canon, ni le registre, ni la base : la politique lui est
  donnée en règles déjà analysées, les candidats en lignes déjà lues, et le
  raccord réel (verrou du lot, marque idempotente, courriers) est un `Port`
  injecté. La sélection est ainsi testable seule, et les lecteurs restent ceux
  de #1616 (canon compilé) et #1467 (`train_policy.Versioned`) ;
- il n'infère **jamais** une famille du nom ou de l'assignation d'un agent :
  la seule source est la famille réelle de la session
  (`registry.live_harness`, #1037 / L242b) ;
- il ne compte ni ne requalifie des verdicts : le nombre requis vient du
  maximum des règles par chemin, l'indépendance des avis rendus reste celle de
  #1465/#1467 (`train_review.qualify`), et un gel nouveau invalide la
  désignation périmée au lieu de la réutiliser.

Idempotence : la désignation est liée au **jalon `frozen`** (son identifiant et
son SHA exact), pas au lot. Un même gel rejoué rend la désignation déjà
enregistrée sans renvoyer de courrier ; un gel nouveau (nouveau jalon, nouveau
SHA) est un autre objet, donc une nouvelle désignation — l'ancienne ne compte
plus. Une désignation est **finale** pour son gel ; une attente est un marqueur
**réessayable** (une notification par gel, la désignation la remplace si la
capacité ou la preuve revient). Le `Port` refuse d'écrire pour un gel qui n'est
plus le dernier : aucun retour à une ancienne tête.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from . import review

#: le schéma de la politique versionnée dont viennent les règles (PR85, #1467)
SCHEMA = "ameesh-train-reviews-policy/1"
#: états rendus par la sélection
DESIGNATED = "designated"
WAITING = "waiting"
NONE = "none"
#: un harnais qui n'est pas une famille de modèle (registre sans session)
PLACEHOLDER_FAMILIES = frozenset(("", "none", "other", "unknown", "inconnu"))
#: les familles de repli tant que le canon compilé (#1616) ne les déclare pas :
#: mêmes valeurs que `train_review.FAMILIES`, à passer en paramètre dès que la
#: déclaration machine « familles et indépendance » est disponible
KNOWN_FAMILIES = frozenset(("claude", "codex", "deepseek"))
#: les états du registre qui interdisent la désignation
UNAVAILABLE_STATUS = frozenset(("stopped", "paused"))
#: les préfixes de `status_text` d'une pause d'exploitation ou de budget
PAUSE_PREFIXES = ("paused", "budget : ")
#: un SHA de commit (complet ou abrégé) : un nom de branche n'est pas un gel
_SHA = re.compile(r"[0-9a-f]{7,64}")


class DesignationError(ValueError):
    """Entrée refusée : le module ne devine pas, il refuse avec un motif."""


class StaleFreeze(DesignationError):
    """Le gel examiné n'est plus le dernier : le `Port` n'a **rien** écrit.

    Une désignation périmée ne se réutilise pas : le moteur relit le dernier
    gel et recommence (jamais un retour à une ancienne tête)."""


class FreezeMoved(StaleFreeze):
    """Le gel a bougé pendant l'écriture : rien n'a été écrit, et les entrées
    du gel examiné (chemins, auteurs, classe) ne valent plus.

    Le moteur ne désigne pas avec les entrées d'une autre tête : l'appelant
    recollecte les chemins, auteurs et la classe du **nouveau** gel, puis
    rejoue. Un SHA de courrier changé ne suffit pas (revue Codex cx2, B1)."""


# --------------------------------------------------------------------------
# la politique : nombre d'avis requis par chemin
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """Une règle de la politique versionnée : des motifs et un nombre d'avis.

    `paths` sont des motifs de la même famille que `review.compile_glob`
    (`**`, `*`, `?`, `[...]`), compilés une fois à la construction. `reviews`
    vaut 1 ou 2 : la politique ne connaît pas d'autre nombre (PR85), et un
    zéro déclaré serait une règle qui ne dit rien.
    """

    paths: tuple[str, ...]
    reviews: int
    #: les motifs compilés, dans l'ordre déclaré
    regexes: tuple = field(default=(), compare=False, repr=False)

    def __post_init__(self):
        paths = tuple(self.paths or ())
        if not paths or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise DesignationError("a review rule needs non-empty path patterns")
        if type(self.reviews) is not int or self.reviews not in (1, 2):
            raise DesignationError(
                "a review rule declares 1 or 2 reviews, not %r" % (self.reviews,))
        try:
            regexes = tuple(review.compile_glob(p) for p in paths)
        except re.error as exc:
            raise DesignationError("invalid review path pattern: %s" % exc) from exc
        object.__setattr__(self, "paths", paths)
        object.__setattr__(self, "regexes", regexes)

    def matches(self, path: str) -> bool:
        return any(regex.match(path) for regex in self.regexes)


def rules_of(declaration) -> tuple[Rule, ...]:
    """Les règles d'une déclaration `train_reviews.projects.<projet>.rules`.

    Forme stricte (celle de PR85, déjà validée par #1467) : une liste, chaque
    entrée `{paths: [...], reviews: 1|2}`. Toute autre forme est refusée — pas
    de repli permissif : une politique illisible ne désigne personne.
    """
    if not isinstance(declaration, dict) or set(declaration) != {"rules"} \
            or not isinstance(declaration["rules"], list) or not declaration["rules"]:
        raise DesignationError("explicit train review rules required; no implicit default")
    out = []
    for entry in declaration["rules"]:
        if not isinstance(entry, dict) or set(entry) != {"paths", "reviews"} \
                or not isinstance(entry["paths"], list):
            raise DesignationError("a review rule is {paths: [...], reviews: 1|2}")
        out.append(Rule(paths=tuple(entry["paths"]), reviews=entry["reviews"]))
    return tuple(out)


def required_count(rules: Sequence[Rule], paths: Iterable[str]) -> int:
    """Le nombre d'avis requis pour ces chemins : le **maximum** des règles.

    Un chemin qu'aucune règle ne couvre vaut zéro : c'est la règle du léger
    (« zéro pour light hors règle », PR85). La couverture des chemins non
    légers est vérifiée par le lecteur de la politique (#1467), pas ici.
    """
    rules = tuple(rules or ())
    highest = 0
    for path in paths or ():
        for rule in rules:
            if rule.matches(path):
                highest = max(highest, rule.reviews)
    return highest


# --------------------------------------------------------------------------
# les candidats : famille réelle, disponibilité, charge
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """Un relecteur possible, tel que le registre le décrit.

    `family` est la famille **réelle** de la session
    (`registry.live_harness`) ; `available` et `load` viennent du registre et
    du nombre de lots ouverts. Le module ne complète rien : une famille vide
    ou un harnais hors des familles connues rend le candidat inéligible.
    """

    name: str
    family: str
    available: bool = True
    load: int = 0

    def eligible(self, *, forbidden_names, forbidden_families,
                 families=KNOWN_FAMILIES) -> bool:
        name = (self.name or "").strip()
        family = (self.family or "").strip().lower()
        return bool(name) and self.available and family in families \
            and name not in forbidden_names and family not in forbidden_families


def live_family(row: Mapping, *, families=KNOWN_FAMILIES) -> str:
    """La famille réelle d'une ligne du registre, jamais l'étiquette déclarée.

    `registry.live_harness` (importé ici en local pour que le module reste
    importable sans base) préfère `session_harness` au harnais déclaré : un
    agent qui emprunte un harnais (L242b) est de la famille sous laquelle il
    tourne. Un harnais hors des familles connues n'est pas une famille ; la
    liste des familles vient du canon compilé (#1616) dès qu'il la déclare.
    """
    from . import registry
    family = str(registry.live_harness(dict(row or {})) or "").strip().lower()
    return family if family in families else ""


def is_available(row: Mapping) -> bool:
    """La disponibilité au registre : ni arrêté, ni en pause, ni session humaine.

    Un agent en tour reste disponible : le courrier l'attend et ouvre son tour
    suivant. Sont indisponibles un agent `stopped`/`paused`, une session
    `externe` (jamais menée par l'exécuteur, L46) et une pause d'exploitation
    ou de budget (`status_text` « paused… » ou « budget : … »). Une ligne sans
    mode ni statut lisibles est refusée : le doute ne désigne personne.
    """
    row = dict(row or {})
    if (row.get("mode") or "execute") != "execute":
        return False
    status = str(row.get("status") or "").strip().lower()
    if not status or status in UNAVAILABLE_STATUS:
        return False
    text = str(row.get("status_text") or "").strip().lower()
    return not any(text.startswith(prefix) for prefix in PAUSE_PREFIXES)


def candidates_from_registry(rows: Iterable[Mapping], *, loads: Optional[Mapping] = None,
                             project: str = "", families=KNOWN_FAMILIES) -> list[Candidate]:
    """Les candidats d'un projet : famille réelle, dispo, charge des lots ouverts.

    `loads` associe un nom au nombre de lots ouverts (`build`/`qa`) ; absent,
    la charge vaut zéro. `project` (l'`app` du lot) restreint aux agents de la
    même équipe : un lot ameesh se relit par un agent ameesh. Un projet vide
    ne filtre pas — l'appelant sait ce qu'il fait.
    """
    loads = loads or {}
    wanted = (project or "").strip()
    out = []
    for row in rows or ():
        team = str((row or {}).get("team") or "").strip()
        if wanted and team != wanted:
            continue
        name = str((row or {}).get("name") or "").strip()
        if not name:
            continue
        try:
            load = int(loads.get(name, 0))
        except (TypeError, ValueError):
            load = 0
        out.append(Candidate(name=name, family=live_family(row, families=families),
                             available=is_available(row), load=max(load, 0)))
    return out


# --------------------------------------------------------------------------
# la sélection : deux familles distinctes, hors auteurs et correcteurs
# --------------------------------------------------------------------------

def families_of(authors: Iterable) -> tuple[set, set]:
    """`(noms, familles)` interdits, tirés des auteurs du contenu gelé.

    Un auteur est `{"name", "family"}` : l'assigné du lot et chaque correcteur
    qui a poussé sur la tête (preuves exactes #1465). Une famille inconnue ne
    se devine pas : `select` attend alors au lieu de désigner (voir
    `unknown_families`).
    """
    names, families = set(), set()
    for author in authors or ():
        row = dict(author or {})
        name = str(row.get("name") or "").strip()
        family = str(row.get("family") or "").strip().lower()
        if name:
            names.add(name)
        if family and family not in PLACEHOLDER_FAMILIES:
            families.add(family)
    return names, families


def unknown_families(authors: Iterable) -> list[str]:
    """Les auteurs dont la famille réelle est inconnue (vide ou étiquette).

    Sans leur famille, l'indépendance d'un relecteur ne se prouve pas : un
    relecteur de la même famille réelle serait désigné sans le savoir."""
    return sorted({str((a or {}).get("name") or "").strip() for a in authors or ()
                   if str((a or {}).get("family") or "").strip().lower()
                   in PLACEHOLDER_FAMILIES})


def select(*, required: int, authors: Iterable = (), candidates: Iterable = (),
           families=KNOWN_FAMILIES) -> dict:
    """Choisit `required` (1 ou 2) relecteurs indépendants et les moins chargés.

    - la famille de chaque relecteur diffère de celle de **chaque** auteur ou
      correcteur, et pour deux avis les deux familles diffèrent entre elles
      (canon §2) ;
    - la famille vient de la session réelle (`Candidate.family`), jamais du nom ;
    - un candidat indisponible, sans famille connue, ou auteur lui-même est
      écarté ;
    - un auteur dont la famille réelle est inconnue (vide ou étiquette) fait
      **attendre** la désignation : sans elle, l'indépendance d'un relecteur ne
      se prouve pas (canon §2) ;
    - à charge égale, l'ordre des noms tranche : la sélection est déterministe.

    Rend `{state, required, reviewers, missing, why, forbidden}` : `waiting`
    quand il manque des familles indépendantes ou qu'une famille d'auteur est
    inconnue, avec le nombre manquant — jamais une admission implicite.
    """
    if type(required) is not int or required not in (0, 1, 2):
        raise DesignationError("required reviews must be 0, 1 or 2, not %r" % (required,))
    names, forbidden = families_of(authors)
    out = {"state": NONE, "required": required, "reviewers": [], "missing": 0,
           "why": "no review required", "forbidden": sorted(forbidden)}
    if required == 0:
        return out
    unknown = unknown_families(authors)
    if unknown:
        # Un auteur sans famille réelle connue : l'indépendance ne se prouve
        # pas, on attend au lieu d'écarter le seul nom (canon §2).
        out.update({"state": WAITING, "missing": required,
                    "why": "author family unknown: %s" % ", ".join(unknown)})
        return out
    eligible = [c for c in candidates or ()
                if c.eligible(forbidden_names=names, forbidden_families=forbidden,
                              families=families)]
    # un seul candidat par famille : le moins chargé, puis le nom
    best: dict[str, Candidate] = {}
    for candidate in eligible:
        family = candidate.family.strip().lower()
        current = best.get(family)
        if current is None or (candidate.load, candidate.name) < (current.load, current.name):
            best[family] = candidate
    ordered = [best[family] for family in sorted(best)]
    ordered.sort(key=lambda c: (c.load, c.name))
    chosen = ordered[:required]
    out["reviewers"] = [{"name": c.name, "family": c.family} for c in chosen]
    if len(chosen) < required:
        out.update({
            "state": WAITING,
            "missing": required - len(chosen),
            "reviewers": [],
            "why": ("%d independent %s missing: %d eligible famil%s (%s), "
                    "forbidden %s" % (
                        required - len(chosen), "review" if required == 1 else "reviews",
                        len(ordered), "y" if len(ordered) == 1 else "ies",
                        ", ".join(sorted(best)) or "none",
                        ", ".join(sorted(forbidden)) or "none")),
        })
        return out
    out.update({"state": DESIGNATED,
                "why": "designated %d reviewer(s)" % len(chosen)})
    return out


# --------------------------------------------------------------------------
# les courriers : la tête exacte, ou la capacité manquante
# --------------------------------------------------------------------------

def reviewer_message(lot: dict, *, freeze: Mapping, required: int, rank: int,
                     reviewer: Mapping, authors: Iterable = (), base: str = "",
                     risk: str = "", policy_ref: str = "") -> str:
    """Le courrier qui remet au relecteur la tête exacte et sa place.

    Le SHA est écrit en entier, avec son jalon `frozen` : le verdict devra le
    citer tel quel (`ameesh work milestone <lot> verdict ok --sha <sha>`). Le
    relecteur qui corrige devient auteur de sa correction : son propre avis ne
    compte plus (canon §2), la sélection suivante l'écartera.
    """
    sha = str((freeze or {}).get("sha") or "")
    fid = (freeze or {}).get("id")
    row = dict(lot or {})
    who = ", ".join(str((a or {}).get("name") or "") for a in authors or () if a) or "inconnu"
    lines = [
        "Désignation automatique au gel (canon §2, #1617) : relecture %d/%d du lot #%s."
        % (rank, required, row.get("id")),
        "",
        "Lot : #%s « %s »" % (row.get("id"), " ".join(str(row.get("title") or "").split())),
        "- Gel : jalon frozen #%s, tête exacte %s." % (fid, sha),
        "- Auteur(s) du contenu gelé : %s." % who,
    ]
    if base:
        lines.append("- Diff à relire : `git diff %s..%s` (%s)."
                     % (base[:12], sha[:12], risk or "classe inconnue"))
    else:
        lines.append("- À relire : la tête %s (%s)." % (sha[:12], risk or "classe inconnue"))
    if policy_ref:
        lines.append("- Politique de revue : %s (deux avis pour SQL, sécurité ou prod)."
                     % policy_ref)
    lines += [
        "- Ta famille : %s — les avis rendus viennent de familles distinctes, hors "
        "celles des auteurs." % ((reviewer or {}).get("family") or "?"),
        "",
        "À faire :",
        "- lire le diff **de cette tête exacte** et vérifier les cas limites ;",
        "- rendre le verdict : `ameesh work milestone %s verdict ok|blocked --sha %s` ;"
        % (row.get("id"), sha),
        "- corriger toi-même un défaut bloquant et pousser sur la branche du lot "
        "(canon §2) : tu deviens alors auteur de ta correction, ton avis sur la "
        "nouvelle tête ne compte plus, un autre relecteur reprend ;",
        "- un défaut non bloquant va au backlog, il ne bloque pas la fusion ;",
        "- ne relance pas les suites : les tests ciblés sont à l'auteur, la CI "
        "tourne sur la tête gelée quand les avis requis sont acquis.",
        "",
        "Ce courrier vient du gel : si la branche bouge, un nouveau gel annule cette "
        "désignation et une nouvelle tête te sera remise.",
    ]
    return "\n".join(lines)


def capacity_message(lot: dict, *, freeze: Mapping, required: int, missing: int,
                     why: str, families: str = "") -> str:
    """La notification d'attente : aucun relecteur indépendant désignable.

    Elle dit ce qui manque — capacité de relecture ou preuve d'auteur — et ne
    demande aucune admission : le lot reste en attente du nombre d'avis requis.
    """
    row = dict(lot or {})
    return "\n".join([
        "Désignation au gel impossible (canon §2, #1617) — relecture en attente.",
        "",
        "Lot : #%s « %s »" % (row.get("id"), " ".join(str(row.get("title") or "").split())),
        "- Gel : jalon frozen #%s, tête %s." % ((freeze or {}).get("id"),
                                                str((freeze or {}).get("sha") or "")[:12]),
        "- Avis requis : %d ; manquants : %d." % (required, missing),
        "- Motif : %s" % why,
    ] + (["- Familles disponibles : %s." % families] if families else []) + [
        "",
        "Aucune admission implicite : le lot reste en attente de ses avis. Rétablir "
        "la capacité ou la preuve manquante (une autre famille de modèle au "
        "registre, un relecteur disponible, la provenance exacte des commits) puis "
        "relancer la désignation.",
    ])


# --------------------------------------------------------------------------
# le raccord : une seule écriture transactionnelle côté Port
# --------------------------------------------------------------------------


class Port(Protocol):
    """Le raccord réel : base, registre et courrier. Le moteur ne fait aucun E/S.

    `commit` est **la** transaction : elle verrouille le lot, recontrôle que
    `freeze` est toujours le dernier gel — sinon elle lève `StaleFreeze` sans
    rien écrire (aucun retour à une ancienne tête) —, recontrôle la marque
    idempotente du gel (un appel concurrent rend la désignation du gagnant,
    sans second courrier) et dépose les courriers dans la même transaction.

    Une **désignation est finale** pour son gel : rejouée, elle rend la même
    chose sans second courrier. Une **attente** est un marqueur réessayable :
    une notification par gel, et si la capacité revient (ou la preuve
    d'auteur), la désignation remplace le marqueur pour le même gel — les
    courriers aux relecteurs ne partent qu'alors, une seule fois.
    """

    def latest_freeze(self, lot: int) -> Optional[dict]:
        """Le dernier jalon `frozen` du lot : `{id, sha}`, ou None."""

    def recorded(self, lot: int, freeze_id: int) -> Optional[dict]:
        """La désignation déjà enregistrée pour **ce** gel, ou None."""

    def commit(self, lot: int, *, freeze: Mapping, required: int, state: str,
               reviewers: Sequence[Mapping], missing: int, why: str,
               authors: Sequence[Mapping], policy_ref: str = "",
               notify: str = "", messages: Sequence[Mapping] = ()) -> dict:
        """Écrit la désignation du gel en une transaction et rend sa forme."""


def apply(lot_item: Mapping, *, policy, paths: Iterable[str], port: Port,
          freeze_sha: str, authors: Iterable = (), candidates: Iterable = (), risk: str = "light",
          base: str = "", policy_ref: str = "", notify: str = "",
          unknown_authors: Iterable = (), families=KNOWN_FAMILIES) -> dict:
    """Désigne les relecteurs du dernier gel du lot, ou dit pourquoi il attend.

    `policy` porte les règles déjà analysées (`Rule` ou déclaration
    `{rules: [...]}`) ; `risk` est la classe lue par le lecteur de politique
    (`light` ou `sensitive`) et sert de garde : une classe non légère sans
    aucun avis requis est un refus, jamais un zéro silencieux. `port` fait
    l'écriture (voir `Port`). Rend la désignation : `{state, required,
    reviewers, missing, why, freeze}` avec `replayed` quand elle existait déjà.

    `unknown_authors` nomme les commits du gel dont la provenance exacte
    (#1465) est inconnue : sans elle, l'indépendance d'un relecteur ne peut pas
    être établie, donc la désignation attend au lieu de deviner — un état
    explicite, jamais un repli.

    Le gel est relu **une fois**, et les entrées fournies (chemins, auteurs,
    classe) sont celles de ce gel : si le `Port` refuse un gel devenu périmé
    (`StaleFreeze`, rien n'a été écrit), `apply` lève `FreezeMoved` — désigner
    la nouvelle tête avec les chemins ou les auteurs de l'ancienne serait une
    admission implicite. L'appelant recollecte les entrées du nouveau gel,
    puis rejoue.

    `freeze_sha` nomme le gel dont l'appelant a tiré ces entrées : s'il n'est
    plus le dernier gel au moment de la lecture, `apply` lève aussi
    `FreezeMoved` sans rien écrire — un gel arrivé entre la collecte et
    l'appel ne reçoit jamais les entrées de l'ancien.
    """
    item = dict(lot_item or {})
    lot = int(item.get("id") or 0)
    if not lot:
        raise DesignationError("lot without id")
    rules = policy if isinstance(policy, (tuple, list)) else rules_of(policy)
    rules = tuple(rules)
    authors = tuple(authors or ())
    for author in authors:
        if not isinstance(author, Mapping) or not str(author.get("name") or "").strip():
            raise DesignationError("an author is {name, family}, not %r" % (author,))
    unknown = [str(value) for value in (unknown_authors or ()) if str(value).strip()]
    freeze = port.latest_freeze(lot)
    if not freeze or not str(freeze.get("sha") or "").strip():
        raise DesignationError("lot #%d is not frozen: no frozen milestone" % lot)
    sha = str(freeze["sha"]).strip().lower()
    if not _SHA.fullmatch(sha):
        raise DesignationError(
            "the freeze of lot #%d names %r, not a commit id" % (lot, freeze["sha"]))
    freeze = {"id": int(freeze["id"]), "sha": sha}
    expected = str(freeze_sha or "").strip().lower()
    if not _SHA.fullmatch(expected):
        raise DesignationError(
            "the inputs of lot #%d name freeze %r, not a commit id" % (lot, freeze_sha))
    if expected != sha:
        raise FreezeMoved(
            "the inputs of lot #%d were collected for freeze %s, the last freeze is %s:"
            " nothing written; recollect the paths, authors and class of the new"
            " freeze, then retry" % (lot, expected[:12], sha[:12]))
    required = required_count(rules, paths)
    if risk != "light" and required == 0:
        raise DesignationError(
            "review count unknown for a non-light lot (#%d, class %s)" % (lot, risk))
    if required == 0:
        return {"state": NONE, "required": 0, "reviewers": [], "missing": 0,
                "why": "light lot: no review before merge", "freeze": freeze}
    existing = port.recorded(lot, freeze["id"])
    if existing and existing.get("state") == DESIGNATED:
        out = dict(existing)
        out["replayed"] = True
        return out
    if unknown:
        # Une preuve d'auteur manquante interdit de prouver l'indépendance :
        # attente explicite, notification, aucun relecteur désigné.
        why = ("author provenance unknown for %d commit(s) of the frozen range: %s"
               % (len(unknown), ", ".join(unknown[:5])))
        selection = {"state": WAITING, "required": required, "reviewers": [],
                     "missing": required, "why": why}
    else:
        selection = select(required=required, authors=authors, candidates=candidates,
                           families=families)
    if selection["state"] == WAITING and existing:
        # L'attente de ce gel est déjà enregistrée et notifiée : on n'en
        # renvoie pas une seconde, mais la sélection a été retentée — si la
        # capacité est revenue, la branche ci-dessous désigne enfin.
        out = dict(existing)
        out["replayed"] = True
        return out
    state = selection["state"]
    messages = []
    if state == DESIGNATED:
        for rank, reviewer in enumerate(selection["reviewers"], start=1):
            messages.append({
                "to": reviewer["name"],
                "family": reviewer["family"],
                "body": reviewer_message(item, freeze=freeze, required=required,
                                         rank=rank, reviewer=reviewer, authors=authors,
                                         base=base, risk=risk, policy_ref=policy_ref),
            })
    else:
        messages.append({
            "to": notify,
            "family": "",
            "body": capacity_message(item, freeze=freeze, required=required,
                                     missing=selection["missing"], why=selection["why"]),
            "capacity": True,
        })
    try:
        return port.commit(lot, freeze=freeze, required=required, state=state,
                           reviewers=selection["reviewers"], missing=selection["missing"],
                           why=selection["why"], authors=authors, policy_ref=policy_ref,
                           notify=notify, messages=tuple(messages))
    except StaleFreeze as exc:
        # Rien n'a été écrit. Les entrées fournies valaient pour l'ancien gel :
        # les recollecter pour le nouveau avant de rejouer (B1, revue codex cx2).
        raise FreezeMoved(
            "the freeze of lot #%d moved during the write (was %s): nothing written; "
            "recollect the paths, authors and class of the new freeze, then retry"
            % (lot, sha[:12])) from exc
