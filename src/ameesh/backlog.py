# SPDX-License-Identifier: AGPL-3.0-only
"""File d'amélioration continue et prise automatique (lot L119, décision 0037
« ameesh ne s'arrête jamais, il s'améliore »).

  ameesh work backlog add --title T --value "valeur attendue" --score N
                          [--priority 1|2|3] [--source S] [--team E]
                          [--requires CAP …] [--body B] [--package F]
  ameesh work backlog list [--all] [--json] [--limit N]

Un élément de la file est un lot (`work_items`) de type `improvement`, en
`intake` et sans assigné, qui porte une valeur attendue (phrase et score de 1
à 100), une priorité (1 haute, 2 normale, 3 basse), éventuellement une équipe
et des capacités exigées. Pas de table neuve : le cycle, le journal, les
jalons, la vue d'avancement et les alertes des lots s'appliquent tels quels.

**Prise automatique** (`auto_take`, appelée à chaque passage d'`ameesh
notify`) : un agent réveillable au repos sans lot depuis `idle_s` prend
l'élément de plus forte valeur qui correspond à son équipe et à ses
capacités. Le lot lui est assigné (attribution gardée de L37) et lui est
annoncé par un courrier lié au lot (`--lot`), qui le réveille. Garde-fous :

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
* un lot dont le début prévu (`planned_start`) est après aujourd'hui attend
  sa date ;
* un lot avec une dépendance explicite encore ouverte attend son amont ;
  les dépendances sont revérifiées dans la transaction de prise ;
* ordre : priorité, puis dates prévues (début, fin, livraison), puis
  ancienneté ; un lot créé il y a moins de 5 min est laissé à qui l'a créé
  (il le confie sans doute dans la foulée) ;
* un orchestrateur ne se sert pas lui-même ; ceux de l'équipe (sinon tous)
  reçoivent un courrier `event` lié au lot et peuvent le réassigner (`ameesh
  work assign`) ;
* au plus `lot_max_per_hour` prises par heure glissante, comptées à part de
  la file.
* **L165b** (constat du 12/10 : 13 agents au repos, 10 lots sans agent, aucun
  pris) : un lot de l'agent qui attend un autre que lui (`qa`,
  `waiting_human`, `blocked`), ou dont le début prévu est après aujourd'hui,
  ne l'occupe pas ; il reste libre tant que son dernier tour n'a laissé ni
  courrier ni consigne en attente. Le lot qu'il prend, lui, l'occupe.

Le cœur reste générique : rien ici ne nomme un métier. La source d'un élément
est un texte libre ; le circuit (relecture, vérifications, intégration) est
celui du projet de l'agent.
"""
from __future__ import annotations

import json
import re
import time

from . import storage
from .db import Db, DbError, is_deadlock

ITEM_TYPE = "improvement"
PRIORITIES = (1, 2, 3)
DEFAULT_PRIORITY = 2
#: prise automatique : au repos depuis 30 min, au plus 2 prises par heure
DEFAULT_TAKE_IDLE_S = 1800.0
DEFAULT_TAKE_MAX_PER_HOUR = 2
#: préfixe de la note de journal d'une prise automatique (compte du débit)
AUTO_TAKE_NOTE = "prise automatique"
#: L165 : un lot non assigné de l'équipe est pris après 15 min de repos, au
#: plus 6 par heure pour tout le mesh ; débit compté à part de la file
DEFAULT_LOT_IDLE_S = 900.0
DEFAULT_LOT_MAX_PER_HOUR = 6
LOT_TAKE_NOTE = "lot de l'équipe pris de lui-même"
#: âge minimal d'un lot pris : l'orchestrateur qui vient de le créer le confie
LOT_MIN_AGE_S = 300.0
#: capacités exigées qui réservent un lot à une attribution explicite
RESERVED_CAPABILITIES = ("decision", "production")
#: capacités déclarées qui marquent la classe d'un lot (canon §1/#803) : un lot
#: qui en exige une n'est tiré que si une autre famille de harnais est au
#: registre (canon §2). `sensitive` est la variante anglaise de `sensible`.
CLASS_CAPABILITIES = ("sensible", "sensitive", "production", "decision")
#: capacité exigée qui désigne un harnais (`harness:codex`)
HARNESS_PREFIX = "harness:"
TAKE_WINDOW_S = 3600.0
#: alertes qui empêchent une prise sur l'hôte de l'agent
HOST_BLOCKING_ALERTS = ("host_pressure", "host_power_low", "host_not_ready", "host_disk_low")
SENDER = "ameesh"


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
REVIEW_REF = "review-after-merge:"


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
    if str(issue_ref or "").startswith(REVIEW_REF) \
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
            "--score <1-100>`%s), grouped with the other minor defects of the same "
            "workstream" % (short, where))


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
        cfg=None, new_project: bool = False) -> dict:
    """Ajoute un élément à la file. La valeur attendue est obligatoire : la
    file ne reçoit pas de travail pour occuper.

    `app` (L118c, #495): the item's project. Omitted, it is inherited from
    `creator` (default `actor`) as `work add` does; given, it must be a known
    project (`work_projects.known_projects`), unless `new_project` (#506)
    introduces it, which the item's history records."""
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
    from . import work, work_projects
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
    return storage.of(db).work.backlog_add(
        title=title, body=body or "", source=source or "", expected_value=expected_value,
        value_score=value_score, priority=int(priority), team=(team or "").strip() or None,
        required_capabilities=_capabilities(required_capabilities) or None,
        package_id=fiche["id"] if fiche else None,
        package_parent=fiche.get("parent") if fiche else None,
        note="improvement backlog (value %d): %s%s" % (
            value_score, expected_value, "; %s" % introduced if introduced else ""),
        actor=actor, app=project)


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
    return lambda agent: book.over(agent, pace=False)


def lot_take_message(item: dict, orchestras: list) -> str:
    return ("Lot #%d, taken on your own (L165): you were idle and this lot of your team "
            "had no taker: %s%s\n"
            "%s can reassign it; if they give you something else, that comes first. "
            "Follow your project's normal flow (review, checks, integration "
            "by whoever has the right). Forbidden in this lot: any irreversible or "
            "production action (deployment, merge, data deletion, production "
            "server, committed spending): propose it to a human and stop there. "
            "If the lot is not for you (capability, context), say so right away "
            "(ameesh work note %d, and a message to %s) rather than keeping it." % (
                int(item["id"]), item["title"],
                "\n%s" % item["body"] if (item.get("body") or "").strip() else "",
                ", ".join(orchestras) if orchestras else "The orchestrator",
                int(item["id"]), ", ".join(orchestras) if orchestras else "the orchestrator"))


def lot_notice(item: dict, agent: str, idle_for_s: float) -> str:
    return ("Automatic take (L165): %s, idle for %d min, took lot #%d of "
            "team %s, which had no assignee: %s. To give it to someone else: "
            "ameesh work assign %d <agent>." % (
                agent, int(idle_for_s // 60), int(item["id"]), item.get("lot_team"),
                item["title"], int(item["id"])))


def lot_takeable(item: dict, today: str | None = None) -> bool:
    """Un lot du projet peut-il être pris sans qu'on le confie ? Jamais un lot
    qui attend un humain (seuls les lots `intake` sans assigné arrivent ici),
    de décision (demande de décision en attente, jalon de décision ouvert,
    capacité `decision` exigée) ni de production (capacité `production`
    exigée) ; jamais un lot sans équipe connue, ni un lot dont le début prévu
    est après `today` (« AAAA-MM-JJ »)."""
    if item.get("state") != "intake" or (item.get("assignee") or "").strip():
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


def lot_fits(item: dict, agent: dict, *, auditor: bool = False, card=None,
             others=None) -> bool:
    """Le lot vise-t-il un des projets de l'agent, et l'agent sait-il le faire ?
    Une capacité exigée `harness:<nom>` désigne le harnais de l'agent.

    `others` : les lignes du registre, pour la garde structurelle de classe
    (#803) — sans elles, un lot marqué n'est pas pris (doute -> sensible).
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
    return not declared_class(item) or other_family_available(agent, others)


def _primary_project(agent: dict) -> str:
    """Le premier projet de l'agent ; '' s'il n'en déclare aucun (L243)."""
    from . import projects as projects_mod
    names = projects_mod.agent_projects(agent)
    return names[0] if names else ""


def _directory_ready(cfg, db, agent, item, project, *, canons, dry_run):
    from . import lot_workdir, work
    reason = lot_workdir.problem(cfg, agent, project, canons=canons)
    if reason is None:
        return True
    # #811 N1 : hors périmètre (raison vide) — aucune note ; un projet déclaré
    # qui attend son dossier est noté une fois par lot.
    if not dry_run and reason:
        note = "L243-4: %s cannot pick up project %s: %s" % (agent["name"], project, reason)
        if not any(e.get("note") == note for e in work.events(db, item["id"])):
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


def _assign_ready(st, item: dict, assignee: str, note: str, take) -> dict | None:
    """La prise conditionnelle d'un élément prêt (`assign`, `ready_only`), ou
    None s'il a été pris, ou bloqué, entre-temps.

    #1496 : la prise verrouille l'élément et ses amonts ; une transaction
    hors du graphe qui les verrouille dans l'autre ordre peut l'interbloquer.
    Le serveur annule alors la prise entière (rien d'écrit) : c'est une prise
    perdue entre-temps, comme une course perdue, et non une panne du passage.
    L'élément reste libre et le prochain passage le revérifie en entier ;
    toute autre erreur remonte."""
    try:
        return st.work.assign(int(item["id"]), assignee, current=None, note=note,
                              actor=SENDER, take=take, ready_only=True)
    except DbError as exc:
        if not is_deadlock(exc):
            raise
        return None


def auto_take(cfg, db, *, now: float | None = None, idle_s: float = DEFAULT_TAKE_IDLE_S,
              max_per_hour: int = DEFAULT_TAKE_MAX_PER_HOUR,
              alerts: list | None = None, listing: list | None = None, paid=None,
              budget_check=None, dry_run: bool = False,
              lot_idle_s: float = DEFAULT_LOT_IDLE_S,
              lot_max_per_hour: int = DEFAULT_LOT_MAX_PER_HOUR, orchestras=None) -> list:
    """Un passage de prise automatique ; rend les prises faites (ou prévues
    en `dry_run`) : `[{item, agent, message_id, kind, notified}]`, `kind`
    valant `lot` (lot non assigné de l'équipe, L165) ou `backlog` (file
    d'amélioration, L119). Les lots de l'équipe passent d'abord ; tant qu'un
    lot du projet attend, personne ne prend d'amélioration. Un seuil nul
    désactive la prise correspondante.

    Les candidats sont triés forfaits d'abord : chaque agent au forfait prend
    ce qui lui va, puis les agents payés au token prennent ce qu'il reste
    dans le même passage (#754, owner 13/10). Un agent en pause budget ou
    au-dessus de son plafond ne prend rien et ne bloque personne ; un agent
    au token ne prend jamais sous une alerte `balance_low`."""
    from . import mail, registry, sous_utilisation, work

    now = time.time() if now is None else float(now)
    st = storage.of(db)
    lot_room = 0
    if lot_idle_s > 0 and lot_max_per_hour > 0:
        lot_room = int(lot_max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S,
                                                                    LOT_TAKE_NOTE)
    lots = st.work.unassigned_lots(200) if lot_room > 0 else []
    # un lot tout juste créé va sans doute être confié : on lui laisse le temps
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    lots = [item for item in lots if lot_takeable(item, today)
            and now - float(item.get("created_ts") or now) >= LOT_MIN_AGE_S]
    room = 0
    if idle_s > 0 and max_per_hour > 0:
        room = int(max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S, AUTO_TAKE_NOTE)
    queue = st.work.backlog(open_only=True, limit=200, ready_only=True) if room > 0 else []
    if not lots and not queue:
        return []
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
                                                waiting_free=waiting_free):
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
    if lots:
        # un orchestrateur répartit : il ne se sert pas lui-même
        if orchestras is None:
            try:
                orchestras = sous_utilisation.orchestrators(cfg, db, listing)
            except Exception:
                orchestras = []
        # L165b : un agent dont tous les lots attendent un autre est libre
        pool = candidates(lot_idle_s, skip=set(orchestras), waiting_free=True)
        for row in pool:
            if len(taken) >= lot_room or not lots:
                break
            if not within_budget(row):
                continue
            agent = registry.get(db, row["name"])
            if agent is None:
                continue
            agent = dict(row, **agent)
            if not auditor_mod.may_take(agent, canons):
                continue
            auditors = auditor_mod.cards([agent], canons)
            for item in list(lots):
                if not lot_fits(item, agent, auditor=row["name"] in auditors,
                                card=auditors.get(row["name"]), others=listing):
                    continue
                if not _directory_ready(cfg, db, agent, item, _lot_project(item),
                                        canons=canons, dry_run=dry_run):
                    continue
                notified = _team_orchestrators(cfg, db, listing, item["lot_team"],
                                               orchestras)
                if dry_run:
                    taken.append({"item": item, "agent": row["name"], "message_id": None,
                                  "kind": "lot", "notified": notified})
                    lots.remove(item)
                    break
                try:
                    check = work.check_assignee(db, row["name"], cfg=cfg)
                except work.WorkError:
                    break                   # agent non réveillable : au suivant
                try:
                    # #540 : sous le verrou de l'agent, pas de second lot
                    # non démarré (il en a reçu un depuis sa sélection).
                    # #1242 (L165) : la prise ne vise qu'un lot intake dont
                    # toutes les dépendances sont satisfaites.
                    done = _assign_ready(st, item, check["assignee"],
                                         "%s par %s, au repos" % (LOT_TAKE_NOTE,
                                                                  check["assignee"]),
                                         work.takes_rule(check))
                except storage.PendingLot:
                    break                   # le lot reste à l'agent suivant
                lots.remove(item)
                if done is None:
                    continue                # pris entre-temps : lot suivant
                done = dict(done, lot_team=item["lot_team"])
                payload = {"lot_take": {"lot": int(done["id"]), "agent": check["assignee"],
                                        "team": item["lot_team"]}}
                message_id = mail.send(db, SENDER, check["assignee"],
                                       lot_take_message(done, notified), kind="event",
                                       work_item_id=str(done["id"]), payload=payload)
                idle_for = now - sous_utilisation.idle_since(row)
                for name in notified:
                    mail.send(db, SENDER, name, lot_notice(done, check["assignee"], idle_for),
                              kind="event", work_item_id=str(done["id"]), payload=payload)
                taken.append({"item": done, "agent": check["assignee"],
                              "message_id": message_id, "kind": "lot", "notified": notified})
                break
    if not queue:
        return taken
    # un lot du projet attend un preneur (ou vient d'être pris) : il passe
    # avant toute amélioration
    if taken or sous_utilisation.waiting_project_lots(
            st.operations.open_lots_activity(500), db=db):
        return taken
    pool = candidates(idle_s)
    for row in pool:
        if len(taken) >= room or not queue:
            break
        if not within_budget(row):
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
                                    canons=canons, dry_run=dry_run):
                continue
            if dry_run:
                taken.append({"item": item, "agent": row["name"], "message_id": None,
                              "kind": "backlog", "notified": []})
                queue.remove(item)
                break
            try:
                check = work.check_assignee(db, row["name"], cfg=cfg)
            except work.WorkError:
                break                       # agent non réveillable : au suivant
            note = "%s par %s (valeur %s) : %s" % (
                AUTO_TAKE_NOTE, check["assignee"], item.get("value_score"),
                item.get("expected_value") or "")
            try:
                done = _assign_ready(st, item, check["assignee"], note,
                                     work.takes_rule(check))
            except storage.PendingLot:
                break                       # #540 : l'élément reste au suivant
            queue.remove(item)
            if done is None:
                continue                    # pris entre-temps : élément suivant
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
    p_add.add_argument("--source", default="",
                       help="where the item comes from (finding, alert, decision, debt...)")
    p_add.add_argument("--team", default=None, help="team that can take it")
    p_add.add_argument("--requires", action="append", default=[],
                       help="required capability (repeatable, or comma-separated list)")
    p_add.add_argument("--body", default="")
    p_add.add_argument("--package", default=None, help="WorkPackage sheet (L29)")
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
        from . import identity
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
                       new_project=getattr(args, "new_project", False))
        except BacklogError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(item, ensure_ascii=False, default=str))
        else:
            print("item #%d added to the backlog (value %d, priority %d): %s" % (
                item["id"], item["value_score"], item["priority"], item["title"]))
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
