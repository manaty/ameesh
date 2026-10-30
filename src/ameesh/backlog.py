# SPDX-License-Identifier: AGPL-3.0-only
"""File d'amélioration continue et prise automatique (lot L119, décision 0037
« ameesh ne s'arrête jamais, il s'améliore »).

  ameesh work backlog add --title T --value "valeur attendue" --score N
                          [--estimate 30m] [--estimate-source S] [--size s|m|l]
                          [--priority 1|2|3] [--source S] [--team E]
                          [--requires CAP …] [--body B] [--package F]
                          [--chantier C] [--review-of LOT]
  ameesh work backlog list [--all] [--json] [--limit N]

Un élément de la file est un lot (`work_items`) de type `improvement`, en
`intake` et sans assigné, qui porte une valeur attendue (phrase et score de 1
à 100), une priorité (1 haute, 2 normale, 3 basse), éventuellement une équipe
et des capacités exigées. Pas de table neuve : le cycle, le journal, les
jalons, la vue d'avancement et les alertes des lots s'appliquent tels quels.

**Panier du chantier** (#2584, canon §2 l.129, owner 15/10 puis 19/10/2026 :
« un défaut non bloquant ne crée pas de lot […] il ne devient un lot qu'à
partir d'une valeur de 30, et un défaut trouvé en relisant une suite d'avis ne
crée rien ») : quand le canon déclare le seuil (`backlog.lot_min_value` de
`processus/mecanique.yaml`, `rules`), un élément de valeur inférieure n'est
plus un lot. Il devient une **ligne** du panier ouvert de son chantier pour le
jour (heure du canon) : un seul lot `improvement` par chantier et par jour,
dont la source est la clé `backlog-basket:<projet>:<chantier>:<jour>`. Le
chantier est `--chantier`, sinon la fiche (`--package`), sinon la fiche ou le
numéro du lot relu, sinon le projet. Un panier déjà pris est fermé : la ligne
suivante en ouvre un autre. Au seuil ou au-dessus, l'élément reste un lot.
Un élément issu de la relecture d'une **suite d'avis** (panier, élément de
backlog né d'une relecture, lot marqué N/P2/P3) est refusé : le relecteur le
corrige sur place ou le laisse. Le lot relu est `--review-of`, sinon le `#N`
d'une source de relecture (« relecture #313 », « verdict … #2577 »), sinon
celui d'un titre de suite (« #2464 N : … ») ; un lot de relecture après fusion
renvoie au lot qu'il relit. Sans seuil au canon (bloc absent, canon
illisible), rien de ceci ne s'applique : aucun seuil n'est supposé (§7).

Tant qu'un lot de roadmap du projet est prenable, un élément sous le seuil
(panier, élément ancien) n'est pas confié à un agent (`work assign`,
`assign_refusal`) : un agent libre ne le prend que faute de travail plus
utile. La prise au repos (`auto_take`) tenait déjà plus large : toute la file
attend tant qu'un lot d'un projet attend un preneur (0037, ci-dessous).

**Durée estimée (#4117)** : comme `work add`, `work backlog add` reçoit
`--estimate`, `--estimate-source` et `--size`, et un agent qui crée un **lot**
sans durée est refusé (L157). Une ligne sous le seuil ne crée pas de lot : le
panier qu'elle ouvre prend l'estimation de la table publiée par l'auditeur —
le groupe du type (`improvement`) et de la taille déclarée, à défaut un groupe
plus large — avec la source « référence table #N » ; `--estimate` explicite
l'emporte. Sans table de référence, l'agent doit donner l'estimation pour
ouvrir un panier ; une ligne ajoutée à un panier déjà ouvert ne crée pas de
lot et n'en exige pas. Aucun rétro-remplissage : les lots existants, y
compris un panier déjà ouvert, gardent leur estimation.

**Prise automatique** (`auto_take`, appelée à chaque passage d'`ameesh
notify`, et par l'exécuteur de chaque hôte pour ses propres agents — #804) :
un agent réveillable au repos sans lot depuis `idle_s` prend l'élément de
plus forte valeur qui correspond à son équipe et à ses capacités. Le lot lui
est assigné (attribution gardée de L37) et lui est annoncé par un courrier
lié au lot (`--lot`), qui le réveille. `agents` restreint les **preneurs** à
un ensemble de noms (ceux de l'exécuteur) : la liste du mesh reste entière
pour les gardes et l'avertissement des orchestrateurs. Garde-fous :

* **aucun lot ne l'attend** : l'agent n'a ni lot ouvert, ni courrier non lu,
  ni consigne en attente, ni lot de session encore ouvert ; et tant qu'un lot
  du projet attend un preneur (ouvert, sans assigné, hors file), personne ne
  prend d'amélioration — c'est à l'orchestrateur ou à l'humain de le confier
  (`idle_capacity` le signale) ;
* **jamais un geste irréversible ou de production** : le courrier de prise
  l'interdit en toutes lettres (déploiement, fusion, suppression de données,
  serveur de production, dépense engagée) ; l'agent le propose à un humain et
  s'arrête là. Le circuit du projet (relecture, intégration par qui en a le
  droit) reste le seul chemin vers la production ;
* **jamais un agent éphémère** (`ameesh agent spawn --ttl`, #3411) : un agent
  de mesure, vivant ou expiré, ne prend ni lot d'équipe (L165) ni élément de
  la file (L119), et ne reçoit pas non plus de relecture proposée (#1677) ;
  ce n'est pas un preneur de travail, c'est un instrument de mesure ;
* les agents au forfait d'abord : ils prennent ce qui leur va, et les agents
  payés au token prennent ce qu'ils laissent, dans le même passage — jamais
  pendant une alerte `balance_low` ;
* un plafond de budget atteint (`CostBook.over`), une pression ou une batterie
  faible de l'hôte de l'agent : pas de prise ; un agent en pause budget ne
  prend rien lui-même, mais n'arrête plus les autres (`idle_agents` l'écarte
  déjà) ;
* au plus `max_per_hour` prises par heure glissante, comptées en base pour
  tout le mesh (plusieurs `ameesh notify` ne se cumulent pas) ;
* l'attribution est conditionnelle (assigné encore vide) : deux preneurs
  concurrents ne prennent pas le même élément.
* une dépendance explicite encore ouverte exclut l'élément avant la borne
  de sélection ; le graphe est revérifié dans la transaction de prise. Un
  interblocage de cette transaction avec une autre (#1496) vaut une prise
  perdue entre-temps : l'élément reste libre, le passage continue.
* **§4 (owner 14/10/2026) : la prise refuse le troisième lot en `build`** —
  le compte et l'écriture sont dans la même transaction sous le verrou de
  l'agent (`assign_capped`), donc deux prises simultanées ne le franchissent
  pas. Le surplus des lots déjà en `build` est rendu à la file par le
  balayage (`ameesh work wip`, `ameesh.wip`).

**Lots de l'équipe** (L165, avant la file) : un agent réveillable au repos
sans lot en cours depuis `lot_idle_s` (15 min par défaut) prend lui-même le
lot non assigné le plus prioritaire de SON équipe qu'il sait faire, sans
attendre qu'on le lui confie (constat du 11/10 : 9 agents Nexlink sur 13 au
repos, 3 lots Nexlink sans agent). Mêmes garde-fous que la file (repos, rien ne
l'attend, forfait d'abord, budget, hôte), et en plus :

* seuls les lots `intake` sans assigné, hors file : jamais un lot qui attend
  un humain (`waiting_human`), déjà confié (`build`), de décision (demande de
  décision en attente, jalon de décision ouvert, capacité `decision` exigée)
  ni de production (capacité `production` exigée) ;
* l'équipe du lot est la sienne, sinon celle de sa fiche (ou de la fiche
  parente), sinon son application ; elle doit être celle de l'agent. Un lot
  sans équipe connue n'est pas pris ;
* l'agent sait le faire : les capacités exigées sont les siennes, et
  `harness:<nom>` désigne son harnais ;
* un lot de relecture après fusion (`issue_ref` ou `source` =
  `review-after-merge:#<lot>`, #2377/#2552) n'est pris que par un agent de la
  famille opposée à celle de l'auteur au gel (`review_lot.reviewer_family`) ;
  famille d'auteur inconnue : le lot n'est pas pris au repos, il attend une
  attribution explicite ;
* un lot dont le début prévu (`planned_start`) est après aujourd'hui attend
  sa date ;
* un lot avec une dépendance explicite encore ouverte attend son amont ;
  les dépendances sont revérifiées dans la transaction de prise ;
* ordre : priorité, puis dates prévues (début, fin, livraison), puis
  ancienneté ; un lot créé il y a moins de 5 min est laissé à qui l'a créé
  (il le confie sans doute dans la foulée) ;
* un orchestrateur ne se sert pas lui-même ; ses candidats passent en dernier
  et, quand un appel central (`agents` absent) ne trouve aucun autre preneur,
  il peut prendre un lot faute d'autre preneur — la note d'attribution garde
  la trace du repli (#1739) ; en prise locale (#804, `agents` fourni, même
  vide), aucun repli : son orchestrateur reste exclu (L804) ; ceux de
  l'équipe (sinon tous) reçoivent un courrier `event` lié au lot et peuvent
  le réassigner (`ameesh work assign`) ;
* au plus `lot_max_per_hour` prises par heure glissante, comptées à part de
  la file.
* **L165b** (constat du 12/10 : 13 agents au repos, 10 lots sans agent, aucun
  pris) : un lot de l'agent qui attend un autre que lui (`qa`,
  `waiting_human`, `blocked`), ou dont le début prévu est après aujourd'hui,
  ne l'occupe pas ; il reste libre tant que son dernier tour n'a laissé ni
  courrier ni consigne en attente. Le lot qu'il prend, lui, l'occupe.
* **§4** : un lot `build` sans assigné est un lot rendu à la file par la
  limite d'en-cours — il reste prenable (avant les `intake` de même priorité),
  avec sa branche et ses avis ; l'agent le reprend sans repartir de zéro.

Le cœur reste générique : rien ici ne nomme un métier. La source d'un élément
est un texte libre ; le circuit (relecture, vérifications, intégration) est
celui du projet de l'agent.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime

from . import storage
from .db import Db, DbError, is_deadlock
from .storage import interface

ITEM_TYPE = "improvement"
PRIORITIES = (1, 2, 3)
DEFAULT_PRIORITY = 2
#: prise automatique : au repos depuis 30 min, au plus 2 prises par heure
DEFAULT_TAKE_IDLE_S = 1800.0
DEFAULT_TAKE_MAX_PER_HOUR = 2
#: préfixe de la note de journal d'une prise automatique (compte du débit) —
#: texte défini dans `storage.interface.ASSIGN_NOTE_PREFIXES`, avec tous les
#: débuts de note d'une attribution (une prise postérieure efface une exception
#: pilote §4 antérieure)
AUTO_TAKE_NOTE = interface.AUTO_TAKE_NOTE
#: L165 : un lot non assigné de l'équipe est pris après 15 min de repos, au
#: plus 6 par heure pour tout le mesh ; débit compté à part de la file
DEFAULT_LOT_IDLE_S = 900.0
DEFAULT_LOT_MAX_PER_HOUR = 6
LOT_TAKE_NOTE = interface.LOT_TAKE_NOTE
#: âge minimal d'un lot pris : l'orchestrateur qui vient de le créer le confie
LOT_MIN_AGE_S = 300.0
#: #1677 (c) : quand aucun lot n'est prenable, la prise propose une relecture
#: (lot en qa sans verdict) ou une correction (dernier verdict bloquant, auteur
#: occupé) à un agent au repos d'une autre famille de modèles. Le débit est
#: borné par heure comme les prises, et un lot n'est pas reproposé avant
#: `REVIEW_PROPOSAL_WINDOW_S`.
REVIEW_PROPOSAL_NOTE = "review proposal"
REVIEW_PROPOSAL_MAX_PER_HOUR = 6
REVIEW_PROPOSAL_WINDOW_S = 6 * 3600.0
#: capacités exigées qui réservent un lot à une attribution explicite
RESERVED_CAPABILITIES = ("decision", "production")
#: capacités déclarées qui marquent la classe d'un lot (canon §1/#803) : un lot
#: qui en exige une n'est tiré que si une autre famille de harnais est au
#: registre (canon §2). `sensitive` est la variante anglaise de `sensible`.
CLASS_CAPABILITIES = ("sensible", "sensitive", "production", "decision")
#: capacité exigée qui désigne un harnais (`harness:codex`)
HARNESS_PREFIX = "harness:"
TAKE_WINDOW_S = 3600.0
#: alertes qui empêchent une prise sur l'hôte de l'agent. #4433 :
#: `runner_pressure` est absent volontairement — la pression d'un runner ne
#: retient que ses propres tours, pas la prise des autres agents de l'hôte.
HOST_BLOCKING_ALERTS = ("host_pressure", "host_power_low", "host_not_ready", "host_disk_low")
SENDER = "ameesh"
#: #2584 : `source` d'un panier — `backlog-basket:<projet>:<chantier>:<jour>`
BASKET_PREFIX = "backlog-basket:"
#: #2584 : marque structurée de la relecture dont vient un élément au seuil
#: (`--review-of`), en tête de sa source : l'élément est une suite d'avis
REVIEW_OF_PREFIX = "review-of:#"
BASKET_LINE_NOTE = interface.BASKET_LINE_NOTE
#: #4117 : la source d'une estimation prise dans la table de référence
REFERENCE_SOURCE = "référence table #%s"


class BacklogError(ValueError):
    """Élément refusé — message toujours actionnable."""


# --------------------------------------------------------------------------
# the route of a non-blocking defect (canon §2, owner 15/10/2026)
# --------------------------------------------------------------------------

#: what marks a review follow-up: N1/N2, P2, P3, "non bloquant", a lot
#: "suite", a "suivi"/"follow-up" of a review. The bare `N` is matched in
#: uppercase (`STANDALONE_N`): the French `n'` is not an N. #1622 N3: the
#: number marker is uppercase-only too — "Nexlink n2" is not an N2.
FOLLOWUP_MARKERS = re.compile(
    r"(?:^|[\s\[(\"«])("
    r"(?-i:N[0-9]+\b)"                 # N1, N2…
    r"|p[23]\b"                        # P2, P3
    r"|non[ -]?bloquant\w*|pas bloquant"
    # "Suite #956", "L242c suite (N #414)", "suite de la R1", "suite du lot":
    # the suite must point at a reference. "suite à la demande de l'owner",
    # "suite au courrier" are ordinary French, not a review follow-up — a
    # human request keeps its lot without a warning.
    r"|suites?\s*(?:#|\(|(?-i:N[0-9]+)|N\b(?!\s*[°º])|de la r[0-9]|du lot|r[0-9]\b)"
    # "suivi de la R1", "suivi du lot #956", "follow-up on the review": the
    # follow-up must aim at a review or a reference. "suivi des coûts
    # mensuels", "follow-up commercial" are ordinary work, not a review
    # follow-up (R1 cx2, P2 2). #1622 N2: "avis" and "lot(s)" only count with
    # a reference — "Suivi des avis clients" and "Suivi des lots en retard"
    # stay silent.
    r"|suivis?\s+(?:(?:de|des|du|la|le|un|une)\s+|d'|l')*"
    r"(?:r[0-9]\b|#[0-9]|relectures?|verdicts?|d[eé]fauts?|correctifs?"
    r"|avis\s+(?:de\s+la\s+)?(?:relecture|r[0-9]\b|#[0-9]|(?-i:N[0-9]+))"
    r"|lots?\s+(?:#|(?-i:N[0-9]+)|r[0-9]\b|de\s+la\s+relecture)"
    r"|(?-i:N[0-9]+)|p[23]\b)"
    r"|follow[ -]?ups?\s*(?:(?:of|on|for|to|the)\s+)*"
    r"(?:r[0-9]\b|#[0-9]|reviews?|defects?|findings?)"
    r")", re.I)
#: the bare "N" (uppercase), the canon's marker for a non-blocking defect.
#: #1622 N1: "N° 5" / "Nº 5" is a number, not an N — the degree sign after
#: the N (even spaced) disqualifies the match.
STANDALONE_N = re.compile(r"(?:^|[\s\[(\"«])N\b(?!\s*[°º])")

#: a suite of **tests** is not a review follow-up: the phrase is neutralised
#: before the examination, without hiding an explicit N/P2/P3 (R1 cx2, P2 3)
TEST_SUITE = re.compile(
    r"suites? (?:de |des |du )?tests?\b|tests?[ -]?suites?\b|suites? compl[eè]?tes?\b"
    r"|suites? ci\b", re.I)
#: a blocking defect (B/P1), a real incident, an outage, a regression: the lot
#: is legitimate — except an explicitly minor regression (N/P2/P3), see
#: `followup_hint`
BLOCKING_DEFECT = re.compile(
    r"(?:^|[\s\[(\"«])(?:b[0-9]+\b|bloquant\w*)|(?:^|[\s\[(\"«])p1\b"
    r"|\bincidents?\b|\bpannes?\b|\bindisponib\w*|\br[eé]gressions?\b", re.I)
#: the canon's bare "B", uppercase ("B : suite #956 — perte de messages",
#: R1 cx2, P2 1): a lone lowercase b is not a verdict
BLOCKING_B = re.compile(r"(?:^|[\s\[(\"«])B[0-9]*\b")
#: "P2 sensible": on ameesh only, it keeps its blocking nature
SENSITIVE_P2 = re.compile(r"p2[^.;\n]{0,30}sensible|sensible[^.;\n]{0,30}p2", re.I)
#: a review-after-merge lot is the rule, not a review follow-up
REVIEW_LOT_TITLE = re.compile(r"^\s*revue\s*#", re.I)


def followup_hint(*, title: str, source: str = "", issue_ref: str = "",
                  app: str = "") -> str:
    """The route warning, or "": does this lot look like a review follow-up
    (N, P2, P3) that must **not** be a lot?

    Canon §2 (owner, 15/10/2026): "a non-blocking defect (N, P2, P3) never
    blocks. It does not create a lot: it goes to the backlog (`ameesh work
    backlog add`, with its value and its workstream), grouped with the other
    minor defects of the same workstream". On ameesh, a P2 of the sensitive
    class still blocks: "P2 sensible" is not reported there (`app="ameesh"`),
    but is on the other projects, where the general rule applies.

    The warning is **advisory**: nothing is refused, a human request and a real
    incident pass. Only an explicit follow-up marker is reported (N1/N2,
    P2/P3, "suite #lot", "suivi d'une relecture", "non bloquant"). A named
    blocking defect (B/B1, P1, incident, outage) keeps its lot even when the
    title or the source quotes a historical N reference: the non-blocking
    marker does not downgrade a blocking one (R1 cx2, P2 1). "non bloquant"/
    "pas bloquant" negates the blocking word, it is not a positive occurrence;
    a regression announced as explicitly minor (N/P2/P3) stays a minor
    follow-up, "régression" alone not being a blocking defect (R2 cx2, P2). A
    suite-of-**tests** phrase is neutralised before the examination: it is not
    a marker, but does not hide an explicit N/P2/P3 (R1 cx2, P2 3). "Suivi"/
    "follow-up" is a marker only when aiming at a review or a reference;
    "suivi des coûts mensuels" stays ordinary work, without a warning (R1 cx2,
    P2 2). #1622: the N marker is uppercase-only ("Nexlink n2" is not an N2),
    "N° 5" is a number, and "suivi des avis clients" / "suivi des lots en
    retard" stay silent because "avis" and "lot(s)" need a reference."""
    text = " ".join([str(title or ""), str(source or "")]).strip()
    if not text:
        return ""
    # #2560 : la référence de relecture se lit comme partout ailleurs
    # (`review_lot.review_ref`) : issue_ref d'abord, puis la source des lots
    # historiques (#1218, #1231, #1269) qui ne l'ont que là.
    from . import review_lot
    if review_lot.review_ref({"issue_ref": issue_ref, "source": source}) \
            or REVIEW_LOT_TITLE.search(str(title or "")):
        return ""                       # the review-after-merge lot is the rule
    # A suite-of-**tests** phrase must not serve as a marker: it is neutralised,
    # without hiding an explicit N/P2/P3 (R1 cx2, P2 3).
    text = TEST_SUITE.sub(" ", text)
    if SENSITIVE_P2.search(text) and str(app or "").strip().lower() == "ameesh":
        return ""                       # ameesh: a sensitive P2 still blocks
    if not (FOLLOWUP_MARKERS.search(text) or STANDALONE_N.search(text)):
        return ""
    # An explicit blocking defect (B/B1, P1, incident, outage) wins: it keeps
    # its lot even with a historical N reference (R1 cx2, P2 1).
    # "non bloquant" / "pas bloquant" qualifies the defect: it is not a
    # positive occurrence of "bloquant". A regression can be explicitly minor
    # (N/P2/P3), without cancelling B/P1/incident/outage.
    blocking_text = re.sub(r"\b(?:non[ -]?bloquant\w*|pas bloquant\w*)", " ",
                           text, flags=re.I)
    explicit_minor = bool(re.search(
        r"(?:^|[\s\[(\"«])(?:(?-i:N[0-9]+\b)|p[23]\b|non[ -]?bloquant\w*|pas bloquant)",
        text, re.I) or STANDALONE_N.search(text))
    if explicit_minor:
        blocking_text = re.sub(r"\br[eé]gressions?\b", " ", blocking_text, flags=re.I)
    if BLOCKING_DEFECT.search(blocking_text) or BLOCKING_B.search(text):
        return ""
    where = " --app %s" % app if app else ""
    short = " ".join(str(title or "").split())[:60]
    return ("this looks like a non-blocking follow-up (N, P2, P3): canon §2 does not "
            "create a lot for it — record it in the backlog "
            "(`ameesh work backlog add --title \"%s\" --value \"<valeur attendue>\" "
            "--score <1-100>`%s, and `--review-of <lot>` for a review finding): under "
            "the canon's lot threshold it becomes a line of its workstream's basket, "
            "and a defect found reviewing a review follow-up creates nothing" % (short, where))


# --------------------------------------------------------------------------
# #2584 : seuil du canon, panier du chantier, suites d'avis
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    """Le seuil de lot d'un défaut non bloquant, compilé du canon (#2584)."""

    lot_min_value: int
    timezone: str
    commit: str


#: `(dépôt, commit, projet) -> Rules | None` : le canon est relu au commit
#: exact, une fois par commit et par projet dans le processus
_RULES: dict = {}


def rules(cfg, project: str) -> Rules | None:
    """Le seuil de lot du canon pour `project` (`backlog.lot_min_value` de
    `processus/mecanique.yaml`, lu au commit exact par `mechanics.load`), ou
    None : canon non configuré ou illisible, mécanique absente, projet non
    déclaré, bloc `backlog` absent. None garde la route historique (un élément
    = un lot) : aucun seuil n'est supposé (canon §7)."""
    project = str(project or "").strip()
    if cfg is None or not project or not getattr(cfg, "canon", ""):
        return None
    from . import canon as canon_mod
    from . import mechanics
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception:  # noqa: BLE001 - canon illisible : aucun seuil supposé
        return None
    for loaded in canons:
        for source in getattr(loaded, "sources", ()) or ():
            if source.mode != "git" or not source.repo or not source.commit:
                continue
            key = (source.repo, source.commit, project)
            if key not in _RULES:
                try:
                    compiled = mechanics.load(source.repo, source.commit, project)
                except Exception:  # noqa: BLE001 - canon sans mécanique : le suivant
                    compiled = None
                threshold = getattr(compiled, "backlog_lot_min_value", None)
                _RULES[key] = (None if threshold is None else
                               Rules(int(threshold), compiled.timezone, compiled.commit))
            if _RULES[key] is not None:
                return _RULES[key]
    return None


#: une source de relecture : « relecture #313 », « verdict dev7 #2525 »,
#: « R2 #1300 », « review of #12 », « en relisant #N », « avis … #N »
REVIEW_WORDS = re.compile(
    r"\b(?:relectures?|relire|relis|relit|relisant|relu|relue|relus|verdicts?|reviews?|reviewed|"
    r"avis|r[0-9])\b", re.I)
#: une référence de lot `#N` (jamais `owner/repo#N`, une issue GitHub)
LOT_REF = re.compile(r"(?<![\w/])#([0-9]+)\b")
#: ce qui précède un `#N` qui n'est pas un lot : « PR #1943 », « pull request
#: #1943 », « pull-request #1943 », « issue #12 »
NOT_LOT_REF = re.compile(r"\b(?:prs?|pull(?:[\s-]+requests?)?|issues?)\s*$", re.I)


def _lot_ref(text: str) -> int | None:
    """Le premier `#N` de `text` qui désigne un lot (ni PR, ni issue)."""
    text = str(text or "")
    for match in LOT_REF.finditer(text):
        if not NOT_LOT_REF.search(text[:match.start()]):
            return int(match.group(1))
    return None


def review_origin(*, title: str = "", source: str = "") -> int | None:
    """Le lot dont la relecture a produit cet élément, lu sur son texte, ou None.

    La marque structurée (`review-of:#N`) d'abord ; puis le premier `#N` d'une
    source de relecture (« relecture #313 (qa-01, N1) », « verdict … sur
    #2577 ») ; puis le premier `#N` d'un titre de suite (« #2464 N (dev7) : … »,
    marqueurs de `followup_hint`). Un constat (« #2577 dry-run complet ») ou une
    source sans relecture (« finding ») n'a pas d'origine."""
    source = str(source or "")
    if source.startswith(REVIEW_OF_PREFIX):
        match = re.match(r"[0-9]+", source[len(REVIEW_OF_PREFIX):])
        if match:
            return int(match.group(0))
    if REVIEW_WORDS.search(source):
        lot = _lot_ref(source)
        if lot is not None:
            return lot
    if followup_hint(title=title):
        return _lot_ref(title)
    return None


def sensitive_p2(*, title: str = "", source: str = "", app: str = "") -> bool:
    """Canon §2 : « sauf P2 sensible ameesh » — sur ameesh, un P2 de la classe
    sensible bloque encore : ni panier, ni seuil, ni refus de suite d'avis ;
    il garde son lot."""
    return (str(app or "").strip().lower() == "ameesh"
            and bool(SENSITIVE_P2.search(" ".join([str(title or ""), str(source or "")]))))


def is_basket(item: dict) -> bool:
    """Le lot est-il un panier de chantier (#2584) ?"""
    return (item.get("type") == ITEM_TYPE
            and str(item.get("source") or "").startswith(BASKET_PREFIX))


def is_review_followup(item: dict) -> bool:
    """Le lot est-il une **suite d'avis** (canon §2, owner 19/10/2026) ?

    Un panier ; un élément du backlog né d'une relecture (marque `review-of:#`,
    ou origine lisible sur sa source ou son titre, `review_origin`) ; un lot
    marqué comme suite non bloquante (N, P2, P3 : `followup_hint`). Un lot de
    relecture après fusion n'en est pas une : il relit le lot d'origine ; un
    P2 sensible d'ameesh non plus (`sensitive_p2`), même né d'une relecture."""
    item = item or {}
    if is_basket(item):
        return True
    title, source = str(item.get("title") or ""), str(item.get("source") or "")
    if sensitive_p2(title=title, source=source, app=item.get("app") or ""):
        return False
    if followup_hint(title=title, source=source, issue_ref=item.get("issue_ref") or "",
                     app=item.get("app") or ""):
        return True
    return item.get("type") == ITEM_TYPE and review_origin(title=title, source=source) is not None


def _reviewed_lot(db: Db, review_of, *, title: str, source: str) -> dict | None:
    """Le lot relu dont vient l'élément : `--review-of`, sinon `review_origin`.
    Un lot de relecture après fusion renvoie au lot qu'il relit. Un `--review-of`
    inconnu est refusé ; une origine lue sur le texte et introuvable est ignorée."""
    from . import review_lot, work
    explicit = review_of is not None and str(review_of).strip() != ""
    if explicit:
        try:
            lot = int(str(review_of).strip().lstrip("#"))
        except ValueError:
            raise BacklogError("--review-of: a lot number is required, got %r"
                               % review_of) from None
    else:
        lot = review_origin(title=title, source=source)
        if lot is None:
            return None
    item = work.get(db, lot)
    if item is None:
        if explicit:
            raise BacklogError("--review-of: lot #%d not found" % lot)
        return None
    ref = review_lot.review_ref(item)
    if ref:
        try:
            origin = work.get(db, int(ref[len(review_lot.PREFIX):].strip().lstrip("#")))
        except ValueError:
            origin = None
        if origin is not None:
            return origin
    return item


def _chantier(chantier, fiche, reviewed, project: str) -> tuple[str, dict | None]:
    """Le chantier d'une ligne de panier et la fiche qui le porte, s'il y en a
    une : `--chantier`, sinon la fiche (`--package`), sinon la fiche ou le
    numéro du lot relu, sinon le projet."""
    label = " ".join(str(chantier or "").split())
    if label:
        return label, fiche
    if fiche:
        return str(fiche["id"]), fiche
    if reviewed is not None:
        package = (reviewed.get("package_id") or "").strip()
        if package:
            return package, {"id": package, "parent": reviewed.get("package_parent")}
        return "#%d" % int(reviewed["id"]), None
    return project, None


def basket_key(project: str, chantier: str, day: str) -> str:
    """La source d'un panier : un panier par projet, chantier et jour."""
    return "%s%s:%s:%s" % (BASKET_PREFIX, project, " ".join(chantier.split()).casefold(), day)


def _basket_day(rule: Rules, now: float | None) -> str:
    from zoneinfo import ZoneInfo
    stamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(stamp, ZoneInfo(rule.timezone)).date().isoformat()


def followup_refusal(reviewed: dict) -> str:
    """Le refus d'un élément issu de la relecture d'une suite d'avis."""
    return ("lot #%d is a review follow-up (%s): a defect found reviewing it creates "
            "nothing — no lot, no basket line (canon §2 l.129, owner 19/10/2026). Fix it "
            "on the branch if it fits in the review (canon §2), otherwise leave it" % (
                int(reviewed["id"]), " ".join(str(reviewed.get("title") or "").split())[:80]))


# --------------------------------------------------------------------------
# la file
# --------------------------------------------------------------------------

def _capabilities(values) -> list:
    out = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def add(db: Db, *, title: str, expected_value: str, value_score: int,
        priority: int = DEFAULT_PRIORITY, source: str = "", team: str | None = None,
        required_capabilities=None, body: str = "", package: str | None = None,
        actor: str = "", app: str | None = None, creator: str | None = None,
        cfg=None, new_project: bool = False, chantier: str | None = None,
        review_of=None, now: float | None = None, estimate=None,
        estimate_source: str | None = None, size: str | None = None) -> dict:
    """Ajoute un élément à la file. La valeur attendue est obligatoire : la
    file ne reçoit pas de travail pour occuper.

    `app` (L118c, #495): the item's project. Omitted, it is inherited from
    `creator` (default `actor`) as `work add` does; given, it must be a known
    project (`work_projects.known_projects`), unless `new_project` (#506)
    introduces it, which the item's history records.

    #2584 : quand le canon déclare le seuil de lot (`rules`), un élément issu
    de la relecture d'une suite d'avis est refusé, et un élément sous le seuil
    devient une ligne du panier de son chantier (`chantier`, sinon la fiche,
    sinon le lot relu, sinon le projet) : le lot rendu est alors le panier,
    avec `basket` vrai, `basket_lines` et `basket_opened`. `review_of` nomme
    le lot relu ; au seuil, il est gardé en tête de la source (`review-of:#N`).
    Un P2 sensible d'ameesh (`sensitive_p2`) garde la route historique.

    #4117 : `estimate` (durée), sa source et `size` (`s`, `m`, `l`) sont posés
    dès la création, comme `work add` (L157). Un agent qui crée un **lot**
    sans estimation est refusé ; une ligne sous le seuil ne crée pas de lot,
    et le panier qu'elle ouvre prend l'estimation de la table de référence
    (type et taille, source « référence table #N »). Sans référence, l'agent
    doit estimer pour ouvrir un panier ; une ligne ajoutée à un panier déjà
    ouvert n'en exige pas. Un panier déjà ouvert n'est jamais réestimé par
    une ligne. Les lots existants ne sont pas rétro-remplis."""
    title = (title or "").strip()
    if not title:
        raise BacklogError("title required")
    expected_value = " ".join((expected_value or "").split())
    if not expected_value:
        raise BacklogError("expected value required (--value): no work just to keep "
                           "an agent busy")
    try:
        value_score = int(value_score)
    except (TypeError, ValueError):
        raise BacklogError("value score must be an integer (1 to 100)") from None
    if not 1 <= value_score <= 100:
        raise BacklogError("value score out of range: %d (1 to 100)" % value_score)
    if int(priority) not in PRIORITIES:
        raise BacklogError("priority 1 (high), 2 (normal) or 3 (low): %r" % priority)
    from . import estimates, work, work_projects
    minutes = estimates.parse(estimate) if estimate not in (None, "") else None
    if size not in (None, "") and size not in work.SIZES:
        raise BacklogError("unknown size: %r (s, m or l)" % (size,))
    estimate_source = " ".join(str(estimate_source or "").split()) or None
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    if new_project and app is None:
        raise BacklogError("--new-project goes with --app <project>")
    introduced = ""
    try:
        project = work_projects.creation_project(cfg, db, creator or actor, app,
                                                 new_project=new_project)
        if app is not None and not project:
            raise BacklogError("--app: empty project")
        if new_project:   # read before the item exists (it makes its project known)
            introduced = work_projects.introduction(cfg, db, project, creator or actor)
    except work.WorkError as exc:
        raise BacklogError(str(exc)) from None
    fiche = None
    if package:
        fiche = work._package(db, package)
    source = " ".join(str(source or "").split())
    team = (team or "").strip() or None
    capabilities = _capabilities(required_capabilities) or None
    rule = rules(cfg, project)
    if rule is not None and sensitive_p2(title=title, source=source, app=project):
        rule = None                     # canon §2 : le P2 sensible d'ameesh garde son lot
    reviewed = _reviewed_lot(db, review_of, title=title, source=source)
    if rule is not None and reviewed is not None and is_review_followup(reviewed):
        raise BacklogError(followup_refusal(reviewed))
    if reviewed is not None and review_of is not None \
            and not source.startswith(REVIEW_OF_PREFIX):
        # la relecture d'origine reste lisible : l'élément est une suite d'avis
        source = ("%s%d %s" % (REVIEW_OF_PREFIX, int(reviewed["id"]), source)).strip()
    basket_route = rule is not None and value_score < rule.lot_min_value
    estimate_by = None
    if basket_route and minutes is None:
        # #4117 : le panier n'est pas un lot choisi ; il prend la référence
        # publiée par l'auditeur (le groupe du type et de la taille déclarée).
        ref = estimates.proposal(db, type=ITEM_TYPE, size=size or None)
        median = int(round(float(ref.get("median_minutes") or 0))) if ref else 0
        if median > 0:
            minutes = median
            estimate_source = REFERENCE_SOURCE % ref.get("id")
            estimate_by = estimates.REFERENCE_ESTIMATOR
    if not basket_route:
        # Un lot est bien créé : la garde L157 s'applique avant l'écriture.
        estimates.check(db, creator or actor, minutes, option="--estimate 2h")
    if minutes and estimate_by is None:
        estimate_by = actor or ""
    # #4186 : la file d'amélioration est un chemin de création comme un autre ;
    # le panier unionne les chemins de ses lignes, le lot direct les porte.
    footprint: list = []
    if project:
        from . import work_footprints
        footprint = work_footprints.estimate_for_creation(
            cfg, app=project, title=title, body=body or "")
    if not basket_route:
        return storage.of(db).work.backlog_add(
            title=title, body=body or "", source=source, expected_value=expected_value,
            value_score=value_score, priority=int(priority), team=team,
            required_capabilities=capabilities,
            package_id=fiche["id"] if fiche else None,
            package_parent=fiche.get("parent") if fiche else None,
            note="improvement backlog (value %d): %s%s%s" % (
                value_score, expected_value,
                "; estimate: %s" % estimates.label(minutes) if minutes else "",
                "; %s" % introduced if introduced else ""),
            actor=actor, app=project, estimate_minutes=minutes,
            estimate_source=estimate_source, estimate_by=estimate_by,
            change_size=size or None, footprint=footprint)
    label, fiche = _chantier(chantier, fiche, reviewed, project)
    day = _basket_day(rule, now)
    line = "- [value %d, P%d] %s — %s (source: %s; by %s)" % (
        value_score, int(priority), " ".join(title.split()), expected_value,
        source or "—", actor or creator or "?")
    if (body or "").strip():
        line += "\n" + "\n".join("  " + row for row in str(body).strip().splitlines())
    try:
        item = storage.of(db).work.backlog_basket_add(
            key=basket_key(project, label, day),
            title="Panier « %s » du %s : défauts non bloquants (N, P2, P3)" % (label, day),
            expected_value=("défauts non bloquants du chantier « %s », regroupés (canon §2 "
                            "l.129) : chacun sous le seuil de lot (%d)"
                            % (label, rule.lot_min_value)),
            line=line, value_score=value_score, priority=int(priority), team=team,
            required_capabilities=capabilities,
            package_id=fiche["id"] if fiche else None,
            package_parent=fiche.get("parent") if fiche else None,
            note="%s (value %d < lot threshold %d, canon %s): %s%s" % (
                BASKET_LINE_NOTE, value_score, rule.lot_min_value, rule.commit[:12],
                " ".join(title.split())[:120], "; %s" % introduced if introduced else ""),
            actor=actor, app=project, estimate_minutes=minutes,
            estimate_source=estimate_source, estimate_by=estimate_by,
            change_size=size or None, footprint=footprint,
            # La garde est lue SOUS le verrou du panier : une ligne ajoutée à
            # un panier déjà ouvert ne crée pas de lot et n'est pas refusée.
            estimate_required=estimates.is_agent(db, creator or actor))
    except interface.BasketEstimateRequired:
        raise estimates.EstimateError(
            estimates.REQUIRED % (creator or actor, "add --estimate 2h")) from None
    return dict(item, basket=True)


def items(db: Db, *, open_only: bool = True, limit: int = 100) -> list:
    """Les éléments dans l'ordre de prise."""
    return storage.of(db).work.backlog(open_only=open_only, limit=limit)


def auditor_may_take(item: dict, name: str, card=None) -> bool:
    """L234 : bugs seulement, hors consigne, décision et fiche de l'auditeur.

    Un lot ne déclare pas les fichiers qu'il changera : les références dans
    son titre/corps/fiche/branche sont une garde conservatrice ; l'agent doit
    aussi rendre un lot dont le périmètre protégé apparaît en travaillant.
    """
    if item.get("type") != "bug":
        return False
    text = " ".join(str(item.get(k) or "") for k in
                    ("title", "body", "package_id", "package_parent", "branch")).casefold()
    protected = ("auditeur.md", "0036", "agents/auditeur", "agents/%s" % name.casefold(),
                 "capabilities", "capacités", "permissions")
    from . import auditor as auditor_mod
    if any(marker in text for marker in (*protected, *auditor_mod.card_markers(card))):
        return False
    # La fiche d'un autre agent n'est pas celle de l'auditeur.
    names = r"(?:auditeur|auditor|%s)" % re.escape(name.casefold())
    return not re.search(r"(?:fiche|card)[^.;\n]{0,80}" + names
                         + r"|(?:sa|ses|own)\s+(?:fiche|card)", text)


def declared_class(item: dict) -> bool:
    """Le lot déclare-t-il une capacité de classe (sensible/production/decision) ?

    #803 : la classe d'un lot n'est lisible sur le diff gelé qu'à la porte de
    fusion ; à la prise, c'est le marqueur déclaré qui fait foi (le doute du
    canon §8 se tranche au gel, pas ici, sinon tout lot `intake` serait
    sensible). `sensitive` est accepté comme variante de `sensible`.
    """
    return bool(set(item.get("required_capabilities") or ())
                & set(CLASS_CAPABILITIES))


def other_family_available(agent: dict, others) -> bool:
    """Une famille de harnais autre que celle du preneur est-elle au registre ?

    Garde **structurelle** de la classe (#803, canon §2) : elle ne regarde pas
    la disponibilité d'un relecteur — un relecteur peut revenir, et la
    relecture croisée joue au moment voulu — mais seulement l'impossibilité de
    principe d'une relecture par une autre famille (mesh mono-famille). Elle ne
    mord donc que sur un lot marqué. Sans registre lisible, ou sans harnais
    connu pour le preneur, on refuse (doute -> sensible).
    """
    mine = str((agent or {}).get("harness") or "").strip().lower()
    if not mine:
        return False
    name = (agent or {}).get("name") or ""
    for row in others or ():
        if (row.get("name") or "") == name:
            continue
        harness = str(row.get("harness") or "").strip().lower()
        if harness and harness != mine:
            return True
    return False


def fits(item: dict, agent: dict, *, auditor: bool = False, card=None,
         others=None) -> bool:
    """L'élément correspond-il aux projets et aux capacités de l'agent ?

    `others` : les lignes du registre, pour la garde structurelle de classe
    (#803) — sans elles, un lot marqué n'est pas pris (doute -> sensible).
    """
    if auditor and not auditor_may_take(item, agent["name"], card):
        return False
    team = (item.get("team") or "").strip()
    if team:
        from . import projects as projects_mod
        if team not in projects_mod.agent_projects(agent):   # L243: membre, pas égal
            return False
    required = set(item.get("required_capabilities") or ())
    if not required <= set(agent.get("capabilities") or ()):
        return False
    return not declared_class(item) or other_family_available(agent, others)


# --------------------------------------------------------------------------
# prise automatique
# --------------------------------------------------------------------------

def take_message(item: dict) -> str:
    return ("Lot #%d, taken from the improvement backlog (no human request or project "
            "lot was waiting for you): %s\nExpected value: %s%s\n"
            "Follow your project's normal flow (review, checks, integration "
            "by whoever has the right). Forbidden in this lot: any irreversible or "
            "production action (deployment, merge, data deletion, production "
            "server, committed spending): propose it to a human and stop there. "
            "If a lot or a message reaches you, it comes before this one. If the item no "
            "longer has value, say so and hand it back (ameesh work note, then ameesh work "
            "close --abandoned) rather than working just to keep busy." % (
                int(item["id"]), item["title"], item.get("expected_value") or "",
                "\n%s" % item["body"] if (item.get("body") or "").strip() else ""))


def _blocked_hosts(alerts: list) -> set:
    return {a.get("host") for a in alerts or () if a.get("type") in HOST_BLOCKING_ALERTS
            and a.get("host")}


def _awaited(row: dict, *, waiting_free: bool = False) -> bool:
    """Un travail attend-il déjà cet agent ? (lot, courrier, consigne, lot de
    session encore ouvert) — il passe avant toute amélioration. `waiting_free`
    (L165b, lots de l'équipe) : un lot, de session ou assigné, qui attend un
    autre que lui (relecture, humain, autre lot) ou commence après aujourd'hui
    ne l'occupe pas."""
    from . import sous_utilisation
    if not sous_utilisation.free_of_lots(row, waiting_free=waiting_free) \
            or int(row.get("unread") or 0):
        return True
    if row.get("has_pending_prompt"):
        return True
    return sous_utilisation.session_lot_busy(row, waiting_free=waiting_free)


def _default_budget_check(cfg, db):
    """`agent -> raison | ''` : plafonds du mesh et de l'agent (L70)."""
    from . import budget as budget_mod
    from . import cost as cost_mod
    limits = budget_mod.current(cfg, db)
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, **limits.book_kwargs())
    # #3923 : une passe, une lecture des soldes. `over` était appelé pour
    # chaque candidat de chaque boucle (lots, propositions, file), soit ~4
    # lectures de `provider_balances` par passe et par exécuteur ; le cache
    # ne vit que le temps de cette passe (le lambda).
    cache: dict = {}
    return lambda agent: book.over(
        agent, pace=False,
        account_checked=bool(cfg.accounts.get(book.tool_of(agent))), cache=cache)


def lot_take_message(item: dict, orchestras: list) -> str:
    branch = (item.get("branch") or "").strip()
    head = (item.get("branch_head") or "").strip()
    resumed = ("" if not branch else
               "It comes back from the queue with its branch %s%s: resume it, "
               "do not restart from scratch.\n" % (branch, " (%s)" % head[:12] if head else ""))
    return ("Lot #%d, taken on your own (L165): you were idle and this lot of your team "
            "had no taker: %s%s\n"
            "%s"
            "%s can reassign it; if they give you something else, that comes first. "
            "Follow your project's normal flow (review, checks, integration "
            "by whoever has the right). Forbidden in this lot: any irreversible or "
            "production action (deployment, merge, data deletion, production "
            "server, committed spending): propose it to a human and stop there. "
            "If the lot is not for you (capability, context), say so right away "
            "(ameesh work note %d, and a message to %s) rather than keeping it."
            % (
                int(item["id"]), item["title"],
                "\n%s" % item["body"] if (item.get("body") or "").strip() else "",
                resumed,
                ", ".join(orchestras) if orchestras else "The orchestrator",
                int(item["id"]), ", ".join(orchestras) if orchestras else "the orchestrator"))


def review_proposal_message(item: dict, agent: str, *, kind: str, family: str) -> str:
    """#1677 (c) : le courrier qui propose une relecture — ou la correction d'un
    lot à verdict bloquant — à un agent au repos, quand aucun lot n'est
    prenable. `family` est l'autre famille de modèles que celle de l'auteur."""
    lot = int(item["id"])
    title = (item.get("title") or "").strip()
    branch = (item.get("branch") or "").strip() or "(branche inconnue)"
    head = (item.get("frozen_sha") or "").strip() or "(tête gelée inconnue)"
    author = (item.get("assignee") or "?").strip()
    if kind == "correction":
        return ("Lot #%d \"%s\" is in qa with a blocking last verdict, and its author %s is "
                "busy on other lots: the correction waits. You can take it over — branch %s, "
                "frozen head %s — fix at the source, push on the branch, then the verdict is "
                "given again (a correction stays on the lot's branch, never a new lot). "
                "Coordinate with the author through the lot journal; if you cannot take it, "
                "say so rather than leaving it waiting."
                % (lot, title, author, branch, head))
    return ("Lot #%d \"%s\" is in qa without a verdict: the review is due, and the author %s "
            "does not review their own work. Your family is the other one (%s). The frozen "
            "head is %s of branch %s: read it, then deliver your verdict —\n"
            "  ameesh work milestone %d verdict ok|blocked --sha %s\n"
            "Review comes before anything else (canon §2): a blocking defect (B, P1) blocks "
            "the merge. The reviewer fixes a small defect himself on the branch and pushes; "
            "a correction too big goes back to the author. If the lot is not for you, say so "
            "rather than leaving it waiting."
            % (lot, title, author, family, head, branch, lot, head))


def lot_notice(item: dict, agent: str, idle_for_s: float) -> str:
    """L'avis aux orchestrateurs d'une prise L165. #1677 (b) : le motif est
    « aucun lot actionnable », jamais le temps depuis le dernier tour (N9) —
    `idle_for_s` reste en signature pour les appels existants, il ne décrit
    plus la règle."""
    return ("Automatic take (L165): %s, without an actionable lot, took lot #%d of "
            "team %s, which had no assignee: %s. To give it to someone else: "
            "ameesh work assign %d <agent>." % (
                agent, int(item["id"]), item.get("lot_team"),
                item["title"], int(item["id"])))


def _take_holds(db, rows, *, cfg=None):
    """#3556 : une lecture ciblée des holds de pilote pour les candidats."""
    from . import planned_waits
    ids = [int(row["id"]) for row in rows]
    if not ids:
        return {}
    events = storage.of(db).progress.lot_hold_events(
        ids, planned_waits.HOLD_NOTE_PATTERNS)
    return planned_waits.pilot_holds(db, events, cfg=cfg, strict=True)


def _hold_blocks(item: dict, hold, now: float | None = None) -> bool:
    """#3556/#3565 : ce hold retient-il encore ce lot ?

    Un hold vivant retient jusqu'à sa levée ou son réveil échu ; seul un
    réveil échu POSTÉRIEUR à la pose libère. Un réveil plus ancien, jamais
    consommé sur un lot sans assigné, ne lève pas un hold posé ensuite."""
    if not hold:
        return False
    stamp = time.time() if now is None else float(now)
    wake = item.get("wake_ts")
    return (wake is None or float(wake) > stamp
            or float(wake) < float(hold.get("since") or 0.0))


def _held_now(db, item: dict, *, cfg=None, now: float | None = None) -> bool:
    """#3565 : les holds de pilote du lot, relus dans la transaction ouverte
    (donc sous son verrou de ligne), avec la même règle que la sélection.

    Une note `hold:`/`unhold:` de pilote prend le verrou de ligne du lot avant
    d'écrire (`work.note`), et les transitions de lot passent par leur UPDATE :
    une pose concurrente est donc soit déjà visible à cette relecture, soit
    ordonnée après l'écriture de la prise — jamais perdue entre les deux."""
    from . import planned_waits
    ident = int(item["id"])
    events = storage.of(db).progress.lot_hold_events(
        [ident], planned_waits.HOLD_NOTE_PATTERNS)
    holds = planned_waits.pilot_holds(db, events, cfg=cfg, strict=True)
    return _hold_blocks(item, holds.get(ident), now)


def lot_takeable(item: dict, today: str | None = None, *, holds=None,
                 now: float | None = None) -> bool:
    """Un lot du projet peut-il être pris sans qu'on le confie ? Jamais un lot
    qui attend un humain (seuls les lots `intake` ou `build` sans assigné
    arrivent ici), de décision (demande de décision en attente, jalon de
    décision ouvert, capacité `decision` exigée) ni de production (capacité
    `production` exigée) ; jamais un lot sans équipe connue, ni un lot dont le
    début prévu est après `today` (« AAAA-MM-JJ »). #3447 : jamais un lot
    réservé à un exécutant externe déclaré (`external_executor`) — cette part
    attend son exécutant, elle n'est pas à la file.

    #3556 : `holds` est la lecture groupée stricte des holds de pilote.
    Un hold actif retient le lot jusqu'à sa levée ou son réveil échu, si ce
    réveil tombe après la pose du hold.

    §4 : un lot `build` sans assigné n'est prenable que s'il a été RENDU à la
    file par la limite d'en-cours (`returned_to_queue` : la note de retour est
    postérieure à la dernière attribution) — un lot en cours sans preneur
    (prérequis, travail confié par courrier) n'est pas à la file."""
    if item.get("state") not in lifecycle.ACTIVE \
            or (item.get("assignee") or "").strip():
        return False
    if (item.get("external_executor") or "").strip():
        return False
    # #4756 : le lot de découpage est un geste du pilote, jamais pris au repos.
    from .file_decoupage import is_split_lot
    if is_split_lot(item):
        return False
    if item.get("state") == lifecycle.IN_PROGRESS and not item.get("returned_to_queue"):
        return False
    if _hold_blocks(item, (holds or {}).get(int(item.get("id") or 0)), now):
        return False
    start = str(item.get("planned_start") or "")[:10]
    if today and start and start > today:
        return False
    if item.get("pending_decision") or item.get("decision_milestone"):
        return False
    if set(item.get("required_capabilities") or ()) & set(RESERVED_CAPABILITIES):
        return False
    return bool((item.get("lot_team") or "").strip())


def _lot_project(item: dict) -> str:
    """Le projet d'un lot pour le dossier de travail : son dépôt (`app`)
    d'abord, son équipe ensuite — même clé que `lot_workdir.project` (#811 N2 :
    `lot_fits` filtrait sur `lot_team`, le dossier se résout sur `app`)."""
    return (item.get("app") or item.get("lot_team") or item.get("team") or "").strip()


def roadmap_waiting(db: Db, project: str, *, now: float | None = None) -> list:
    """#2584 : les lots de roadmap de `project` prenables maintenant — du
    travail plus utile qu'un élément sous le seuil de lot. Un lot non assigné
    hors file d'amélioration (`unassigned_lots`, dépendances fusionnées),
    prenable (`lot_takeable` : ni décision, ni production, équipe connue, début
    prévu échu), sans attente planifiée (`waiting_project_lots`), qui n'est ni
    une relecture après fusion ni une suite d'avis.

    #4250 : la portée réelle est celle de `unassigned_lots` — la file des lots
    non assignés de TOUS les projets, dans la limite que fixe l'appelant — puis
    le filtre du projet ; seuls les holds des candidats du projet sont lus
    (#3565)."""
    from . import review_lot, sous_utilisation
    project = str(project or "").strip()
    if not project:
        return []
    stamp = time.time() if now is None else float(now)
    today = time.strftime("%Y-%m-%d", time.localtime(stamp))
    # #3565 : le projet filtre AVANT la lecture des holds — seuls les holds des
    # candidats du projet sont lus. La file elle-même (`unassigned_lots(None)`)
    # couvre tous les projets, sans borne : c'est le coût réel de ce passage,
    # revu au panier #4250.
    candidates = [item for item in storage.of(db).work.unassigned_lots(None)
                  if _lot_project(item) == project]
    holds = _take_holds(db, candidates)
    out = [item for item in candidates
           if lot_takeable(item, today, holds=holds, now=stamp)
           and not review_lot.review_ref(item) and not is_review_followup(item)]
    if not out:
        return []
    from . import project_plan
    out = project_plan.eligible(db, out, cfg=getattr(db, "cfg", None), now=stamp)
    return sous_utilisation.waiting_project_lots(out, db=db, now=stamp)


def roadmap_first(db: Db, item: dict, *, cfg=None, now: float | None = None) -> list:
    """#2584 : les lots de roadmap qui passent avant cet élément du backlog, ou
    `[]`. Seul un élément sous le seuil de lot du canon (`rules`) attend : un
    panier, un élément ancien de faible valeur. Sans seuil au canon, rien
    n'attend (aucun seuil supposé)."""
    item = item or {}
    if item.get("type") != ITEM_TYPE:
        return []
    project = str(item.get("app") or item.get("team") or "").strip()
    rule = rules(cfg, project)
    if rule is None or sensitive_p2(title=item.get("title") or "",
                                    source=item.get("source") or "", app=project):
        return []
    try:
        score = int(item.get("value_score") or 0)
    except (TypeError, ValueError):
        score = 0
    if score >= rule.lot_min_value:
        return []
    return roadmap_waiting(db, project, now=now)


def assign_refusal(db: Db, item: dict, *, cfg=None, now: float | None = None) -> str:
    """#2584 : pourquoi cet élément du backlog ne se confie pas à un agent
    maintenant, ou "" (`work assign`, comme la prise au repos)."""
    waiting = roadmap_first(db, item, cfg=cfg, now=now)
    if not waiting:
        return ""
    project = str(item.get("app") or item.get("team") or "").strip()
    rule = rules(cfg, project)
    return ("lot #%d is a backlog %s of value %s, under the canon's lot threshold (%d): it "
            "is not given to an agent while a roadmap lot of %s can be taken (%s%s). Canon "
            "§2 l.129: a free agent takes it only for lack of more useful work — assign "
            "the roadmap lot first" % (
                int(item["id"]), "basket" if is_basket(item) else "item",
                item.get("value_score"), rule.lot_min_value if rule else 0, project,
                ", ".join("#%d" % int(lot["id"]) for lot in waiting[:5]),
                ", …" if len(waiting) > 5 else ""))


def lot_fits(item: dict, agent: dict, *, auditor: bool = False, card=None,
             others=None, db=None) -> bool:
    """Le lot vise-t-il un des projets de l'agent, et l'agent sait-il le faire ?
    Une capacité exigée `harness:<nom>` désigne le harnais de l'agent.

    `others` : les lignes du registre, pour la garde structurelle de classe
    (#803) — sans elles, un lot marqué n'est pas pris (doute -> sensible).
    `db` : requis pour un lot de relecture après fusion (#2377, #2552) — sa
    famille de preneur se relit sur le gel du lot d'origine ; sans base, un tel
    lot n'est pas pris (doute -> refus).
    """
    if auditor and not auditor_may_take(item, agent["name"], card):
        return False
    team = _lot_project(item)
    from . import projects as projects_mod
    if not team or team not in projects_mod.agent_projects(agent):   # L243: membre
        return False
    have = set(agent.get("capabilities") or ())
    if agent.get("harness"):
        have.add("%s%s" % (HARNESS_PREFIX, agent["harness"]))
    if not set(item.get("required_capabilities") or ()) <= have:
        return False
    if declared_class(item) and not other_family_available(agent, others):
        return False
    from . import review_lot
    return review_lot.may_take(db, item, agent)


def _primary_project(agent: dict) -> str:
    """Le premier projet de l'agent ; '' s'il n'en déclare aucun (L243)."""
    from . import projects as projects_mod
    names = projects_mod.agent_projects(agent)
    return names[0] if names else ""


#: #1538 : marque d'un refus de dossier dans le journal d'un élément. La
#: relecture se fait sur ce préfixe, jamais sur le texte complet : la note
#: nomme l'agent qui l'a rencontré, mais elle est unique PAR ÉLÉMENT.
#: #1900 N2 : c'est ce même préfixe que lit le relevé borné de la passe.
DIRECTORY_NOTE_PREFIX = "L243-4:"


class _DirNotes:
    """Les éléments dont le refus de dossier a déjà été noté, lus une fois par
    passe (#1900 N2), bornés aux candidats de la passe (#2162).

    La lecture est paresseuse — aucun item ne manque de dossier, aucune
    requête — et bornée au préfixe DIRECTORY_NOTE_PREFIX et aux ids candidats
    (lots et améliorations, au plus 200 chacun) ; `has` répond ensuite en
    mémoire, `add` retient l'élément que `_directory_ready` vient d'écrire.
    Sans elle, chaque passe d'`auto_take` relisait les 100 derniers événements
    de chaque lot dont le dossier manque, pour chaque agent candidat, et le
    relevé non borné relisait tout le journal des lots clos."""

    def __init__(self, store, ids):
        self._store = store
        # #2162 : déduplication exacte par id — le filtre porte sur les ids
        # des candidats, jamais sur une fenêtre temporelle, sinon une note
        # ancienne d'un vieux lot serait réécrite.
        self._ids = sorted({int(item_id) for item_id in ids})
        self._items: set[int] | None = None

    def _load(self) -> set[int]:
        if self._items is None:
            self._items = {int(row["work_item_id"])
                           for row in self._store.events_noted(
                               DIRECTORY_NOTE_PREFIX, 0.0, ids=self._ids)}
        return self._items

    def has(self, item_id) -> bool:
        return int(item_id) in self._load()

    def add(self, item_id) -> None:
        self._load().add(int(item_id))


def _directory_ready(cfg, db, agent, item, project, *, canons, dry_run, notes=None):
    from . import lot_workdir, work
    reason = lot_workdir.problem(cfg, agent, project, canons=canons)
    if reason is None:
        return True
    # #811 N1 : hors périmètre (raison vide) — aucune note ; #1538 : une raison
    # réelle (dossier déclaré absent ou ambigu) est notée une fois PAR ÉLÉMENT,
    # quel que soit l'agent qui bute dessus — pas une note par (agent, élément).
    # `notes` (le relevé borné de la passe) retient les éléments déjà notés :
    # le journal n'est pas relu pour chaque couple (agent, élément) du lot.
    if not dry_run and reason:
        note = "%s %s cannot pick up project %s: %s" % (
            DIRECTORY_NOTE_PREFIX, agent["name"], project, reason)
        if notes is not None:
            # #1900 N2 : le relevé borné de la passe porte les éléments déjà
            # notés (une requête), plus les 100 derniers événements du lot
            # relus par élément tant que le dossier manque.
            if not notes.has(item["id"]):
                work.note(db, item["id"], note, actor=SENDER)
                notes.add(item["id"])
        elif not any((e.get("note") or "").startswith(DIRECTORY_NOTE_PREFIX)
                     for e in work.events(db, item["id"])):
            work.note(db, item["id"], note, actor=SENDER)
    return False


def _team_orchestrators(cfg, db, listing: list, team: str, orchestras=None) -> list:
    """Les orchestrateurs du registre à prévenir d'une prise : ceux d'un des
    projets du lot, sinon tous."""
    from . import projects as projects_mod
    from . import sous_utilisation
    if orchestras is None:
        orchestras = sous_utilisation.orchestrators(cfg, db, listing)
    teams = {row["name"]: set(projects_mod.agent_projects(row)) for row in listing}
    known = [name for name in orchestras if name in teams]
    return sorted([name for name in known if team in teams[name]] or known)


class _HoldPlaced(Exception):
    """#3565 : un hold de pilote est apparu pendant la transaction de prise —
    l'assignation déjà écrite est annulée avec la transaction."""


def _assign_ready(db, item: dict, assignee: str, note: str, take,
                  cap: int, *, cfg=None, hold_guard: bool = False,
                  now: float | None = None, plan_can_take=None) -> dict | None:
    """La prise conditionnelle d'un élément prêt (`assign_capped`,
    `ready_only`), ou None s'il a été pris, bloqué, ou si l'agent a atteint
    le plafond §4 entre-temps.

    #1496 : la prise verrouille l'élément et ses amonts ; une transaction
    hors du graphe qui les verrouille dans l'autre ordre peut l'interbloquer.
    Le serveur annule alors la prise entière (rien d'écrit) : c'est une prise
    perdue entre-temps, comme une course perdue, et non une panne du passage.
    L'élément reste libre et le prochain passage le revérifie en entier ;
    toute autre erreur remonte. `cap` : le plafond du canon (`work.build_limit`).

    #3565 (`hold_guard`) : les holds de pilote sont relus DANS la transaction
    de la prise, après le verrou de ligne du lot (celui d'`assign_capped`,
    le même que les notes hold/levée). Un hold posé entre la sélection et
    l'écriture est donc vu : l'assignation est annulée avec la transaction,
    sans courrier. Celle-ci est un point de sauvegarde sous psycopg et se
    joint à la session sous psql. Une connexion d'exécuteur médiée n'a pas
    de transaction : elle garde la lecture de la sélection (garde non
    applicable)."""
    st = storage.of(db)
    args = dict(current=None, note=note, actor=SENDER, max_build=cap,
                take=take, ready_only=True)
    try:
        if not hold_guard or storage.is_remote(db):
            return st.work.assign_capped(int(item["id"]), assignee, **args)
        with db.transaction() as tx:
            done = storage.of(tx).work.assign_capped(int(item["id"]), assignee, **args)
            if done is not None:
                if _held_now(tx, done, cfg=cfg, now=now):
                    raise _HoldPlaced
                if done.get("type") != ITEM_TYPE:
                    from . import project_plan
                    if not project_plan.assignment_ready(tx, item, done, cfg=cfg, now=now,
                                                         can_take=plan_can_take):
                        raise _HoldPlaced
            return done
    except _HoldPlaced:
        return None
    except DbError as exc:
        if not is_deadlock(exc):
            raise
        return None


def _propose_reviews(db, st, listing, *, pool, room, dry_run=False, canons=(),
                     skip=(), within_budget=None, wakeable=None, cfg=None,
                     now: float | None = None) -> list:
    """#1677 (c) : apparie les lots en attente de relecture — ou de correction —
    aux agents au repos. Une relecture va à l'autre famille de modèles que
    l'auteur (canon §2) ; une correction peut aller à n'importe quel agent au
    repos, du moment que l'auteur du lot est occupé (corps du lot #1677). Les
    relectures passent avant les corrections.

    L'appariement suit les mêmes gardes qu'une prise (B4, N1) : `lot_fits`
    (équipe résolue comme `unassigned_lots`, capacités, garde de classe #803)
    pour une correction comme pour une relecture — le canon §2 ajoute la
    famille, il n'en dispense pas —, `may_take` de l'auditeur, plafond de
    budget, agent réveillable (#1021), jamais un orchestrateur. L'auteur du
    lot est écarté (B3) et sa famille se lit au tour du gel
    (`review_lot._author_at_freeze`, #1053/#1777). Une relecture écarte en
    outre **toutes** les familles d'auteur de la plage gelée (#2864), lues sur
    la marque de désignation du dernier gel (`frozen_authors`) : la même
    lecture que la désignation et la porte. Sans cette lecture (pas de marque,
    famille d'auteur inconnue), aucune proposition n'est faite (échec fermé).

    Rend les propositions dans la forme des prises (`item`, `agent`,
    `message_id`, `kind` valant `review` ou `correction`, `notified`). Le lot
    est marqué au journal (`REVIEW_PROPOSAL_NOTE`), ce qui l'écarte des
    passages suivants le temps de `REVIEW_PROPOSAL_WINDOW_S` ; le contrôle de
    la fenêtre et la note sont pris sous un verrou consultatif par lot
    (#1738 N5), et une relecture désigne son relecteur (#705, #1738 N7) — la
    double relecture est écartée et `stale_lot` sait à qui s'adresser.

    #3565 : un lot sous hold pilote vivant n'est ni relu ni corrigé par
    proposition — la même lecture que la prise (#3556), réveil échu
    postérieur à la pose compris."""
    from . import mail, registry, review_designation as rd, review_lot
    from . import review_designation_activation as activation
    if room <= 0 or not pool:
        return []
    # N6 : lire plus large que la pièce demandée — l'appariement (famille,
    # équipe, capacités) écarte en Python, un LIMIT trop court laisserait six
    # lots non appariables bloquer tous les suivants.
    rows = st.work.review_proposals(max(int(room) * 6, 24),
                                    proposal_prefix=REVIEW_PROPOSAL_NOTE,
                                    window_s=REVIEW_PROPOSAL_WINDOW_S)
    if not rows:
        return []
    # #3565 : le hold de pilote suspend aussi cette voie. `review_proposals`
    # lit `w.*`, sans l'alias `wake_ts` : un lot effectivement tenu est relu
    # une fois par son `get` pour connaître son réveil (aucune lecture pour
    # les autres).
    holds = _take_holds(db, rows, cfg=cfg)
    if holds:
        stamp = time.time() if now is None else float(now)
        kept = []
        for row in rows:
            hold = holds.get(int(row["id"]))
            if hold and "wake_ts" not in row:
                wake = (st.work.get(int(row["id"])) or {}).get("wake_ts")
                row = dict(row, wake_ts=wake)
            if not _hold_blocks(row, hold, stamp):
                kept.append(row)
        rows = kept
        if not rows:
            return []
    # relectures d'abord, corrections ensuite (l'ordre du SQL est conservé)
    rows = sorted(rows, key=lambda r: 1 if (r.get("last_verdict") or "") == "blocked" else 0)
    by_name = {row.get("name"): row for row in listing}
    # #2864 : la lecture des auteurs ne dépend que du lot — une fois par lot,
    # pas une fois par couple (agent, lot). None (lecture absente) est mémorisé.
    frozen_reading: dict = {}
    out: list = []
    used: set = set()
    for row in pool:
        if len(out) >= room:
            break
        name = row["name"]
        if name in skip:
            continue
        if within_budget is not None and not within_budget(row):
            continue
        if wakeable is not None:
            # N1 : la garde d'attribution (#1021, agent retenu) vaut aussi pour
            # une proposition — un courrier à un agent sans tour bloquerait le
            # lot jusqu'à la fenêtre suivante.
            name = wakeable(name)
            if not name:
                continue
        reg = dict(row, **(registry.get(db, name) or {}))
        mine = registry.live_harness(reg).strip().lower()
        if not mine:
            continue
        from . import auditor as auditor_mod
        if not auditor_mod.may_take(reg, canons):
            continue
        auditors = auditor_mod.cards([reg], canons)
        for item in rows:
            lot = int(item["id"])
            if lot in used:
                continue
            author = (item.get("assignee") or "").strip()
            if name == author:
                continue                  # B3 : on ne relit pas son propre lot
            # B4 : la prise d'un lot exige son équipe, ses capacités et la
            # garde de classe (#803) ; une proposition suit la même règle.
            if not lot_fits(item, reg, auditor=name in auditors,
                            card=auditors.get(name), others=listing, db=db):
                continue
            correction = (item.get("last_verdict") or "") == "blocked"
            family = ""
            if correction:
                author_row = by_name.get(author)
                # auteur occupé : il a un autre lot actionnable que celui-ci
                if author_row is not None and int(author_row.get("actionable_lots") or 0) <= 1:
                    continue
            else:
                # relecture : l'autre famille que celle de l'auteur (canon §2),
                # lue au tour du gel (#1053/#1777 : l'assigné courant ne prouve
                # pas la production) ; famille d'auteur inconnue, toute famille
                # connue fait l'affaire
                author_info = review_lot._author_at_freeze(db, lot, item)
                # #2664 : les familles viennent du canon compilé, au dernier
                # commit audité ; illisible -> aucune proposition (fail closed).
                try:
                    families = review_lot.compiled_families(db)
                except review_lot.FamiliesError:
                    continue
                # #2864 : écarter **toutes** les familles d'auteur de la plage
                # gelée, avec la lecture de la désignation au gel (sa marque),
                # pas seulement celle de l'assigné au gel. Provenance inconnue
                # (pas de marque lisible, auteur sans famille) : aucune
                # proposition, l'indépendance ne se suppose pas (échec fermé).
                if lot not in frozen_reading:
                    frozen_reading[lot] = activation.frozen_authors(db, item)
                authors = frozen_reading[lot]
                if authors is None:
                    continue
                author_names, forbidden_families = rd.families_of(authors)
                if rd.unknown_families(authors) or name in author_names:
                    continue
                eligible = [family for family in families
                            if family not in forbidden_families]
                mine_family = review_lot.family_of(mine, families)
                if not mine_family or not eligible:
                    continue
                # la famille opposée à l'auteur de référence reste préférée ;
                # si elle est elle-même auteur de la plage, une autre famille
                # indépendante convient (même règle que la désignation).
                reference = next((row.get("family") for row in authors
                                  if row.get("name") == author_info.get("name")), "")
                want = review_lot.reviewer_family(
                    reference or author_info.get("harness"), families)
                if want != review_lot.UNKNOWN_FAMILY and want in eligible:
                    if mine_family != want:
                        continue
                    family = want
                else:
                    if mine_family not in eligible:
                        continue
                    family = mine_family
            kind = "correction" if correction else "review"
            # #4779 (B1 dev4) : une relecture proposée désigne son relecteur
            # (#705) ; sa fiche doit donc prouver la capacité `propose` et un
            # rôle, comme une désignation manuelle (`check_reviewer`) et la
            # sélection au gel. Sans ce garde, un canari `[read]` / `roles: []`
            # recevait la proposition et occupait le siège sans pouvoir rendre
            # d'avis (test_1677).
            if kind == "review":
                eligible, _reason = rd.reviewer_eligibility(reg)
                if not eligible:
                    continue
            payload = {"review_proposal": {"lot": lot, "agent": name, "kind": kind}}
            message_id = None
            if not dry_run:
                # #1738 (N5/N7) : le contrôle de la fenêtre, la désignation du
                # relecteur (#705, pour une relecture) et la note tiennent sous
                # un verrou consultatif par lot — deux passages, même sur des
                # hôtes distincts, ne proposent pas le même lot deux fois.
                if not st.work.claim_review_proposal(
                        lot, agent=name, kind=kind,
                        proposal_prefix=REVIEW_PROPOSAL_NOTE,
                        window_s=REVIEW_PROPOSAL_WINDOW_S, actor=SENDER):
                    used.add(lot)     # pris par un autre passage : on n'y revient pas
                    continue
                message_id = mail.send(
                    db, SENDER, name,
                    review_proposal_message(item, name, kind=kind, family=family),
                    kind="event", work_item_id=str(lot), payload=payload)
            used.add(lot)
            out.append({"item": item, "agent": name, "message_id": message_id,
                        "kind": kind, "notified": []})
            break
    return out


def _still_free(st, item_id) -> bool:
    """Le lot est-il encore prenable (sans assigné, dans un état de prise) ?

    `assign_capped` rend None pour des refus distincts — le plafond §4 atteint
    (le lot n'a pas bougé : le garder pour un autre preneur), une prise
    concurrente (le lot a bougé : le retirer) ou un refus sous verrou (une
    dépendance est apparue depuis la sélection, L165 : le lot reste prenable
    mais plus pour cette passe) : cette relecture et `_cap_reached` les
    distinguent."""
    item = st.work.get(int(item_id))
    if item is None:
        return False
    return (not (item.get("assignee") or "").strip()
            and not (item.get("external_executor") or "").strip()
            and item.get("state") in lifecycle.ACTIVE)


def _cap_reached(st, agent: str, cap: int) -> bool:
    """§4 : l'agent est-il au plafond d'en-cours ?

    Distingue, après un refus `None` de `assign_capped` sur un lot encore
    prenable, le plafond (arrêter cet agent : il ne peut plus rien prendre) du
    refus sous verrou — dépendance apparue (L165) : passer au lot suivant.
    Relecture hors du verrou de la prise : un agent redescendu entre-temps fait
    seulement retenter la prise, un agent remonté au plafond est refusé de
    nouveau par `assign_capped`."""
    return st.work.count_builds(agent) >= cap


def auto_take(cfg, db, *, now: float | None = None, idle_s: float = DEFAULT_TAKE_IDLE_S,
              max_per_hour: int = DEFAULT_TAKE_MAX_PER_HOUR,
              alerts: list | None = None, listing: list | None = None, paid=None,
              budget_check=None, dry_run: bool = False,
              lot_idle_s: float = DEFAULT_LOT_IDLE_S,
              lot_max_per_hour: int = DEFAULT_LOT_MAX_PER_HOUR,
              review_max_per_hour: int = REVIEW_PROPOSAL_MAX_PER_HOUR,
              orchestras=None,
              agents: set[str] | None = None) -> list:
    """Un passage de prise automatique ; rend les prises faites (ou prévues
    en `dry_run`) : `[{item, agent, message_id, kind, notified}]`, `kind`
    valant `lot` (lot non assigné de l'équipe, L165), `review`/`correction`
    (#1677 : relecture d'un lot en qa sans verdict, correction d'un lot à
    verdict bloquant, proposées à un agent au repos d'une autre famille quand
    aucun lot n'est prenable) ou `backlog` (file d'amélioration, L119). Les
    lots de l'équipe passent d'abord ; tant qu'un lot du projet attend,
    personne ne prend d'amélioration. Un seuil nul désactive la prise
    correspondante — `review_max_per_hour` à 0 coupe les propositions
    (#1738 N3). Une proposition faite passe avant la file d'amélioration du
    même passage (« du travail réel sur des lots en cours ») ; propositions
    coupées, la file reprend la main.

    Les candidats sont triés forfaits d'abord : chaque agent au forfait prend
    ce qui lui va, puis les agents payés au token prennent ce qu'il reste
    dans le même passage (#754, owner 13/10). Un agent en pause budget ou
    au-dessus de son plafond ne prend rien et ne bloque personne ; un agent
    au token ne prend jamais sous une alerte `balance_low`.

    `agents`, quand il est donné, borne les preneurs à ces noms (l'exécuteur
    de leurs hôtes, #804) sans rétrécir `listing` : les gardes et les
    orchestrateurs prévenus restent ceux du mesh entier."""
    from . import mail, registry, sous_utilisation, work

    now = time.time() if now is None else float(now)
    st = storage.of(db)
    caps: list = []

    def cap() -> int:
        """§4 : le plafond du canon compilé, lu à la première prise tentée ;
        sans document lisible, `mechanics.MechanicsError` arrête le passage
        (l'appelant l'isole) au lieu de supposer un plafond (#2451)."""
        if not caps:
            caps.append(work.build_limit(cfg))
        return caps[0]

    # #804 : la place restante est relue avant **chaque** prise, pas une fois
    # par passe. Les compteurs sont en base et partagés par tous les
    # exécuteurs ; avec plusieurs passes simultanées (un exécuteur par hôte),
    # une lecture unique laissait chacune consommer la même place. La relecture
    # ramène la fenêtre à la durée d'une attribution ; la place reste une
    # lecture hors de la transaction d'écriture, donc deux passes strictement
    # simultanées peuvent encore se dépasser de leur nombre — les passes
    # suivantes, elles, voient le compteur à jour.
    def lot_place() -> int:
        return int(lot_max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S,
                                                                LOT_TAKE_NOTE)

    def place() -> int:
        return int(max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S,
                                                            AUTO_TAKE_NOTE)

    lot_room = 0
    if lot_idle_s > 0 and lot_max_per_hour > 0:
        lot_room = lot_place()
    lots = st.work.unassigned_lots(None) if lot_room > 0 else []
    # #4174: read the full candidate queue before phase filtering: a lower
    # priority bug beyond the former 200-row window must remain dispatchable.
    # un lot tout juste créé va sans doute être confié : on lui laisse le temps
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    lots = [item for item in lots
            if lot_takeable(item, today, now=now)
            and now - float(item.get("created_ts") or now) >= LOT_MIN_AGE_S]
    from . import project_plan
    plan_snapshots = {}
    lots = project_plan.eligible(db, lots, cfg=cfg, now=now, phase=False,
                                 snapshots=plan_snapshots)
    room = 0
    if idle_s > 0 and max_per_hour > 0:
        room = int(max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S, AUTO_TAKE_NOTE)
    queue = st.work.backlog(open_only=True, limit=200, ready_only=True) if room > 0 else []
    # #4741 : la file d'amélioration suit la même garde de hold pilote que les
    # lots d'équipe — un élément tenu (ou dont le hold attend encore son
    # réveil) n'est pas pris. `backlog(ready_only=True)` ne lit que
    # `planned_start` et les dépendances, jamais le hold ni son réveil.
    if queue:
        queue_holds = _take_holds(db, queue, cfg=cfg)
        if queue_holds:
            queue = [item for item in queue
                     if not _hold_blocks(item, queue_holds.get(int(item["id"])), now)]
    if not lots and not queue:
        # #1677 (c) : même sans lot prenable, un lot en qa sans verdict (ou à
        # correction due) peut occuper un agent au repos — la sonde est bon
        # marché, le reste du passage ne se paie que s'il y a de quoi proposer.
        # #1738 (N3) : propositions coupées, ce passage n'a plus rien à faire.
        if review_max_per_hour <= 0 \
                or not st.work.review_proposals(1, proposal_prefix=REVIEW_PROPOSAL_NOTE,
                                                window_s=REVIEW_PROPOSAL_WINDOW_S):
            return []
    # #1900 / #2162: notes remain restricted to the candidate ids, never the
    # journal of closed lots. #4174 reads all open lot candidates so a bug
    # outside a truncated window cannot starve behind enhancements.
    dir_notes = _DirNotes(st.work, [item["id"] for item in lots]
                                   + [item["id"] for item in queue])
    listing = st.operations.listing() if listing is None else listing
    from . import auditor as auditor_mod
    try:
        canons = auditor_mod.load_all(cfg, strict=False) if cfg is not None else []
    except Exception:
        # Pas de prise quand le périmètre des rôles est illisible.
        return []
    # #754 : un agent en pause budget ne prend rien — il n'est déjà pas un
    # candidat (`idle_agents` écarte l'état `blocked`), et rien ici ne doit
    # arrêter les autres : un forfait au seuil (Claude, Codex) privait tout le
    # mesh de prise, y compris les agents au token qui avaient du budget
    # (owner, 13/10 : « quand les forfaits Claude sont au seuil, le travail
    # passe à DeepSeek et Codex »).
    alerts = alerts or []
    blocked = _blocked_hosts(alerts)
    # #254 : un hôte en pause ne prend pas de travail, ni ses agents
    from . import host_holds
    holds = host_holds.live(db)
    blocked |= set(holds)
    held = host_holds.held_agents(listing, holds)
    balance_low = any(a.get("type") == "balance_low" for a in alerts)
    if paid is None:
        paid = sous_utilisation.paid_harnesses(getattr(cfg, "host", None))
    if budget_check is None:
        try:
            budget_check = _default_budget_check(cfg, db)
        except Exception:      # plafonds illisibles : on ne devine pas
            return []

    def is_paid(row) -> bool:
        # harnais inconnus (descripteurs illisibles) : traité comme payé
        return paid is None or row.get("harness") in paid

    def candidates(threshold: float, skip=(), waiting_free: bool = False) -> list:
        out = []
        for row in sous_utilisation.idle_agents(listing, now, threshold,
                                                waiting_free=waiting_free,
                                                actionable_idle=True):
            # #804 : un exécuteur ne fait prendre que ses propres agents ; les
            # autres restent dans `listing` pour les gardes et l'avertissement.
            if agents is not None and row["name"] not in agents:
                continue
            # #3411 : un éphémère (`ameesh agent spawn --ttl`) ne prend rien
            # automatiquement — ni un lot d'équipe (L165), ni un élément de la
            # file (L119, la prise de #3403), ni une relecture proposée
            # (#1677). Vivant ou expiré : l'échéance ne le rend pas preneur.
            # #3460 : un agent retiré non plus.
            if row.get("ephemeral") or row.get("retired_ts"):
                continue
            if row["name"] in skip or row["name"] in held \
                    or _awaited(row, waiting_free=waiting_free) or row.get("host") in blocked:
                continue
            if is_paid(row) and balance_low:
                continue
            out.append(row)
        # forfaits d'abord, puis le repos le plus long
        out.sort(key=lambda r: (is_paid(r), sous_utilisation.idle_since(r), r["name"]))
        return out

    def within_budget(row) -> bool:
        try:
            return not budget_check(row["name"])
        except Exception:
            return False

    taken: list = []
    # un orchestrateur répartit : il ne se sert pas lui-même, ni d'un lot ni
    # d'une proposition de relecture (#1677)
    if orchestras is None:
        try:
            orchestras = sous_utilisation.orchestrators(cfg, db, listing)
        except Exception:
            orchestras = []
    if lots:
        # L165b : un agent dont tous les lots attendent un autre est libre
        pool = candidates(lot_idle_s, skip=set(orchestras), waiting_free=True)
        # #1739 : plus aucun autre preneur ? un orchestrateur peut se servir,
        # mais ses candidats passent en dernier : il ne prend que les lots que
        # les autres ont laissés, et la note d'attribution garde la trace.
        # L804 : la prise locale d'un exécuteur (`agents` fourni, même vide)
        # garde son périmètre et n'offre jamais ce repli à son orchestrateur —
        # le repli ne vaut que pour les appels centraux (`agents is None`).
        taker_count = len(pool)
        if lots and agents is None:
            seen = {row["name"] for row in pool}
            pool += [row for row in candidates(lot_idle_s, skip=(), waiting_free=True)
                     if row["name"] not in seen]
        for index, row in enumerate(pool):
            fallback = index >= taker_count
            # #804 : place relue avant chaque prise (compteur partagé)
            if not lots or len(taken) >= lot_room or lot_place() <= 0:
                break
            if not within_budget(row):
                continue
            # §4 canon : plafond de lots en build par agent — la prise
            # automatique refuse au-delà (le témoin concurrent est dans
            # le stockage : `assign_capped` recompte sous le verrou de l'agent)
            if st.work.count_builds(row["name"]) >= cap():
                continue
            agent = registry.get(db, row["name"])
            if agent is None:
                continue
            agent = dict(row, **agent)
            if not auditor_mod.may_take(agent, canons):
                continue
            auditors = auditor_mod.cards([agent], canons)
            candidate_ids = {int(item["id"]) for item in lots}

            # Assignment rechecks stay read-only. During selection, record
            # directory refusals for this pass's candidates before the plan
            # filters them out; otherwise the reporting loop never sees them.
            def plan_can_take(item, *, note=False):
                return (lot_fits(item, agent, auditor=row["name"] in auditors,
                                 card=auditors.get(row["name"]), others=listing, db=db)
                        and _directory_ready(cfg, db, agent, item, _lot_project(item),
                                             canons=canons,
                                             dry_run=(dry_run if note and int(item["id"]) in candidate_ids
                                                      else True), notes=dir_notes))

            agent_lots = project_plan.eligible(db, lots, cfg=cfg, now=now,
                                               can_take=lambda item: plan_can_take(item, note=True),
                                               snapshots=plan_snapshots)
            for item in agent_lots:
                if not lot_fits(item, agent, auditor=row["name"] in auditors,
                                card=auditors.get(row["name"]), others=listing, db=db):
                    continue
                if not _directory_ready(cfg, db, agent, item, _lot_project(item),
                                        canons=canons, dry_run=dry_run, notes=dir_notes):
                    continue
                # un orchestrateur qui se sert ne se prévient pas lui-même
                notified = [name for name in _team_orchestrators(
                    cfg, db, listing, item["lot_team"], orchestras) if name != row["name"]]
                if dry_run:
                    taken.append({"item": item, "agent": row["name"], "message_id": None,
                                  "kind": "lot", "notified": notified,
                                  "fallback": fallback})
                    lots.remove(item)
                    project_plan.mark_taken(plan_snapshots, item["id"])
                    break
                # #804 : la place est relue juste avant l'écriture — une passe
                # concurrente a pu consommer la place depuis la sélection.
                if lot_place() <= 0:
                    break
                try:
                    check = work.check_assignee(db, row["name"], cfg=cfg)
                except work.WorkError:
                    break                   # agent non réveillable : au suivant
                note = "%s par %s, au repos" % (LOT_TAKE_NOTE, check["assignee"])
                if fallback:
                    note += " (aucun autre preneur ; orchestrateur)"
                try:
                    # #540 : sous le verrou de l'agent, pas de second lot
                    # non démarré (il en a reçu un depuis sa sélection).
                    # #1242 (L165) : la prise ne vise qu'un lot intake dont
                    # toutes les dépendances sont satisfaites.
                    done = _assign_ready(db, item, check["assignee"], note,
                                         work.takes_rule(check), cap(),
                                         cfg=cfg, hold_guard=True, now=now,
                                         plan_can_take=plan_can_take)
                except storage.PendingLot:
                    break                   # le lot reste à l'agent suivant
                if done is None:
                    # refus sous le verrou de l'agent : plafond §4 (rien n'a
                    # bougé : le lot reste à prendre par un autre agent), lot
                    # devenu non prenable (dépendance apparue, L165) ou pris
                    # entre-temps
                    if not _still_free(st, item["id"]):
                        lots.remove(item)
                        project_plan.mark_taken(plan_snapshots, item["id"])
                        continue        # pris entre-temps : lot suivant
                    if _cap_reached(st, check["assignee"], cap()):
                        break           # agent au plafond : au suivant
                    continue            # refus sous verrou : lot suivant
                lots.remove(item)
                project_plan.mark_taken(plan_snapshots, item["id"])
                done = dict(done, lot_team=item["lot_team"])
                payload = {"lot_take": {"lot": int(done["id"]), "agent": check["assignee"],
                                        "team": item["lot_team"]}}
                message_id = mail.send(db, SENDER, check["assignee"],
                                       lot_take_message(done, notified), kind="event",
                                       work_item_id=str(done["id"]), payload=payload)
                idle_for = now - sous_utilisation.idle_since(row)
                # #3451 : l'avis aux pilotes n'attend aucun geste (une
                # réattribution reste possible) : il est lu à leur prochain
                # tour sans en ouvrir un — 32 avis pour 14 lots en 24 h
                # ouvraient autant de tours de pilote.
                avis = dict(payload, info=True)
                for name in notified:
                    mail.send(db, SENDER, name, lot_notice(done, check["assignee"], idle_for),
                              kind="event", work_item_id=str(done["id"]), payload=avis)
                taken.append({"item": done, "agent": check["assignee"],
                              "message_id": message_id, "kind": "lot",
                              "notified": notified, "fallback": fallback})
                break
    # #1677 (c) : aucun lot n'a été pris — proposer aux agents au repos une
    # relecture (lot en qa sans verdict courant, autre famille que l'auteur),
    # puis une correction (verdict courant bloquant, auteur occupé). Ces
    # propositions passent avant la file d'améliorations : du travail réel sur
    # des lots en cours. Elles suivent les mêmes gardes qu'une prise (B4, N1).
    if not taken and review_max_per_hour > 0:
        proposal_room = max(0, int(review_max_per_hour)
                            - st.work.auto_takes_since(TAKE_WINDOW_S, REVIEW_PROPOSAL_NOTE))

        def wakeable(name):
            try:
                return work.check_assignee(db, name, cfg=cfg)["assignee"]
            except work.WorkError:
                return None

        taken.extend(_propose_reviews(
            db, st, listing,
            pool=candidates(idle_s, skip=set(orchestras), waiting_free=True),
            room=proposal_room, dry_run=dry_run, canons=canons,
            skip=set(orchestras), within_budget=within_budget, wakeable=wakeable,
            cfg=cfg, now=now))
    if not queue:
        return taken
    # un lot du projet attend un preneur (ou vient d'être pris) : il passe
    # avant toute amélioration ; un lot qu'une attente planifiée retient
    # (dépendance, engagement, date) n'attend pas un agent (#1625)
    if taken or sous_utilisation.waiting_project_lots(
            st.operations.open_lots_activity(500), db=db, now=now):
        return taken
    pool = candidates(idle_s)
    for row in pool:
        # #804 : place relue avant chaque prise (compteur partagé)
        if not queue or len(taken) >= room or place() <= 0:
            break
        if not within_budget(row):
            continue
        # §4 : le plafond de lots en build vaut aussi pour la file
        if st.work.count_builds(row["name"]) >= cap():
            continue
        agent = registry.get(db, row["name"])
        if agent is None:
            continue
        agent = dict(row, **agent)
        if not auditor_mod.may_take(agent, canons):
            continue
        auditors = auditor_mod.cards([agent], canons)
        for item in list(queue):
            if not fits(item, agent, auditor=row["name"] in auditors,
                        card=auditors.get(row["name"]), others=listing):
                continue
            if not _directory_ready(cfg, db, agent, item, _lot_project(item),
                                    canons=canons, dry_run=dry_run, notes=dir_notes):
                continue
            if dry_run:
                taken.append({"item": item, "agent": row["name"], "message_id": None,
                              "kind": "backlog", "notified": []})
                queue.remove(item)
                break
            # #804 : la place est relue juste avant l'écriture — une passe
            # concurrente a pu consommer la place depuis la sélection.
            if place() <= 0:
                break
            try:
                check = work.check_assignee(db, row["name"], cfg=cfg)
            except work.WorkError:
                break                       # agent non réveillable : au suivant
            note = "%s par %s (valeur %s) : %s" % (
                AUTO_TAKE_NOTE, check["assignee"], item.get("value_score"),
                item.get("expected_value") or "")
            try:
                done = _assign_ready(db, item, check["assignee"], note,
                                     work.takes_rule(check), cap(),
                                     cfg=cfg, hold_guard=True, now=now)
            except storage.PendingLot:
                break                       # #540 : l'élément reste au suivant
            if done is None:
                # idem lots d'équipe : plafond (garder), élément devenu non
                # prenable (dépendance apparue) ou pris entre-temps
                if not _still_free(st, item["id"]):
                    queue.remove(item)
                    continue                # pris entre-temps : élément suivant
                if _cap_reached(st, check["assignee"], cap()):
                    break                   # agent au plafond : au suivant
                continue                    # refus sous verrou : élément suivant
            queue.remove(item)
            message_id = mail.send(db, SENDER, check["assignee"], take_message(done),
                                   kind="event", work_item_id=str(done["id"]),
                                   payload={"backlog": {"lot": int(done["id"]),
                                                        "value_score": done.get("value_score")}})
            taken.append({"item": done, "agent": check["assignee"],
                          "message_id": message_id, "kind": "backlog", "notified": []})
            break
    return taken


# --------------------------------------------------------------------------
# CLI (`ameesh work backlog …`, branchée par mesh_cli)
# --------------------------------------------------------------------------

def add_parsers(work_sub, func) -> None:
    from . import work
    p = work_sub.add_parser("backlog", help="continuous improvement backlog (L119, 0037)")
    sub = p.add_subparsers(dest="backlog_command")
    p_add = sub.add_parser("add", help="add an item with an expected value")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--value", required=True, dest="expected_value",
                       help="expected value, in one sentence")
    p_add.add_argument("--score", required=True, type=int, dest="value_score",
                       help="value score from 1 to 100 (highest is taken first)")
    p_add.add_argument("--priority", type=int, default=DEFAULT_PRIORITY,
                       choices=list(PRIORITIES), help="1 high, 2 normal (default), 3 low")
    p_add.add_argument("--estimate", default=None, metavar="DURATION",
                       help="#4117: estimated duration of the item (L157): 90m, 2h, 1h30, "
                            "1,5h, 2d (or minutes) — mandatory for an agent creating a lot; "
                            "a basket line without one takes the published reference")
    p_add.add_argument("--estimate-source", default=None, metavar="SOURCE",
                       help="#4117: where the estimate comes from (design, bug history ×1.3…)")
    p_add.add_argument("--size", default=None, choices=list(work.SIZES),
                       help="#4117: size of the change: s small, m medium, l large — a key "
                            "of the reference estimate (L157b)")
    p_add.add_argument("--source", default="",
                       help="where the item comes from (finding, alert, decision, debt...)")
    p_add.add_argument("--team", default=None, help="team that can take it")
    p_add.add_argument("--requires", action="append", default=[],
                       help="required capability (repeatable, or comma-separated list)")
    p_add.add_argument("--body", default="")
    p_add.add_argument("--package", default=None, help="WorkPackage sheet (L29)")
    p_add.add_argument("--chantier", default=None,
                       help="#2584: the workstream whose basket receives an item under the "
                            "canon's lot threshold (default: the sheet, else the reviewed "
                            "lot, else the project)")
    p_add.add_argument("--review-of", default=None, dest="review_of", metavar="LOT",
                       help="#2584: the lot whose review found this defect; a defect found "
                            "reviewing a review follow-up is refused (canon §2)")
    p_add.add_argument("--actor", default="")
    p_add.add_argument("--app", default=None,
                       help="its project (L118c); default: the creator's project")
    p_add.add_argument("--new-project", action="store_true",
                       help="#506: accept an --app project nobody declares yet "
                            "(journaled on the item); refused otherwise")
    p_add.add_argument("--json", action="store_true")
    p_add.set_defaults(func=func)
    p_list = sub.add_parser("list", help="the backlog, in take order")
    p_list.add_argument("--all", action="store_true",
                        help="also items already taken and not finished")
    p_list.add_argument("--limit", type=int, default=100)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=func)


def _line(item: dict) -> str:
    extra = []
    if item.get("team"):
        extra.append("team %s" % item["team"])
    if item.get("required_capabilities"):
        extra.append("requires %s" % ", ".join(item["required_capabilities"]))
    if item.get("assignee"):
        extra.append("%s, %s" % (item["state"], item["assignee"]))
    return "#%d  P%s  value %3s  %s%s\n      → %s" % (
        int(item["id"]), item.get("priority") or DEFAULT_PRIORITY, item.get("value_score"),
        item["title"], "  [%s]" % "; ".join(extra) if extra else "",
        item.get("expected_value") or "")


def run(db: Db, args, cfg=None) -> int:
    import sys
    command = getattr(args, "backlog_command", None)
    if command == "add":
        from . import estimates, identity
        cfg = cfg if cfg is not None else getattr(db, "cfg", None)
        # L118c: the creator is this session, whatever `--actor` says (`work add`)
        binding = identity.resolve_binding(cfg, db) if cfg is not None else None
        try:
            item = add(db, title=args.title, expected_value=args.expected_value,
                       value_score=args.value_score, priority=args.priority,
                       source=args.source, team=args.team,
                       required_capabilities=args.requires, body=args.body,
                       package=args.package, actor=args.actor,
                       app=getattr(args, "app", None),
                       creator=binding.name if binding is not None and binding.ok
                       else args.actor, cfg=cfg,
                       new_project=getattr(args, "new_project", False),
                       chantier=getattr(args, "chantier", None),
                       review_of=getattr(args, "review_of", None),
                       estimate=getattr(args, "estimate", None),
                       estimate_source=getattr(args, "estimate_source", None),
                       size=getattr(args, "size", None))
        except BacklogError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(item, ensure_ascii=False, default=str))
        else:
            suffix = (" — estimated %s" % estimates.label(item["estimate_minutes"])
                      if item.get("estimate_minutes") else "")
            if item.get("basket"):
                print("line added to basket #%d (%s, %d line(s), value %d): %s — under the "
                      "canon's lot threshold, no lot is created (canon §2)%s" % (
                          item["id"], "opened" if item.get("basket_opened") else "open",
                          item.get("basket_lines") or 0, item["value_score"],
                          item["title"], suffix))
            else:
                print("item #%d added to the backlog (value %d, priority %d): %s%s" % (
                    item["id"], item["value_score"], item["priority"], item["title"],
                    suffix))
            # L157b/#4117 : la référence en vigueur, LUE (comme `work add`)
            print("reference: %s" % estimates.proposal_label(
                estimates.proposal(db, type=item.get("type"),
                                   size=item.get("change_size"))))
            if not item.get("estimate_minutes"):
                print("warning: %s" % estimates.missing(item["id"]), file=sys.stderr)
        return 0
    if command == "list":
        rows = items(db, open_only=not args.all, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("improvement backlog empty")
            return 0
        for row in rows:
            print(_line(row))
        return 0
    print("usage: ameesh work backlog add|list", file=sys.stderr)
    return 2
