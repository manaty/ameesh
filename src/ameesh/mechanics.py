# SPDX-License-Identifier: AGPL-3.0-only
"""Paramètres mécaniques du processus, compilés depuis le canon (#1606 §7, #1616).

Le canon (`processus/mecanique.yaml`, manaty/home) déclare, un bloc par règle
mécanique, ce que l'outillage exécute sans agent :

- `cutoffs` et `timezone` : les coupures de release du train, en heure locale
  IANA ;
- `packets.cutoffs` : la cadence des paquets horaires (#1467, §11 bis horaire),
  distincte des coupures de release ; bloc absent = aucune cadence, tuple vide
  explicite et `last_packet`/`next_packet` refusés, jamais un repli sur
  `cutoffs` ni sur une durée locale (#1757) ;
- `auto_take.excluded_families`, `expire` (#5005, bloc optionnel) : familles
  exclues des prises lot/backlog jusqu’à la date ISO incluse (fuseau du canon),
  sans fermer les propositions de relecture/correction ; absent ou expiré :
  aucune exclusion ; canon illisible : passage arrêté comme le plafond §4.
- `build.max_in_build` : le plafond de lots en développement par agent ;
- `ci.packet`, `ci.incident_label`, `ci.environment_red`, `ci.faulty_lot` :
  une suite par paquet, l'étiquette qui autorise exceptionnellement la CI d'un
  lot, le sort d'un rouge seulement en CI (vert en local) et celui d'un lot
  fautif ;
- `reviews.families`, `exclude_author_family`, `distinct_families` : les
  familles de modèle et l'indépendance des avis ;
- `reviews.light_reviews` : le nombre d'avis d'un lot **léger** (§2, #2642) ;
  c'est le plancher lu par `reviews_for`, jamais une règle de chemin — une
  règle positive reclasse le chemin en sensible et ne doit pas servir de
  raccourci fourre-tout ;
- `merge.named_fire`, `repeat_freeze`, `on_green_packet`, `post_merge_red` :
  la fusion du paquet dès la suite verte, sans feu nominatif ni gel répété ;
- `backlog.lot_min_value` (#2584, bloc optionnel) : la valeur à partir de
  laquelle un défaut non bloquant devient un lot (canon §2 l.129, owner
  19/10/2026) ; en dessous, il rejoint le panier de son chantier. Bloc absent :
  `None`, et la route du panier reste inactive — aucun seuil n'est supposé ;
- `staging.waiting_lots_min`, `before_cutoff_seconds`, `before_packet_seconds`
  (#3291, bloc optionnel) : le seuil de lots retenus et les marges d'un
  déploiement staging hors créneau (canon §11 bis) ; bloc absent : `None`.
- `paquet.attente_mediane_s` et `paquet.max_paquets` (#3256 C, bloc optionnel,
  canon §11 bis) : la médiane d'attente `qa_merge` de la veille au-delà de
  laquelle deux paquets disjoints partent en parallèle, et le nombre maximal
  de CI de paquet simultanées ; bloc absent : `None`, aucun second paquet.
- `packet_tests.unit_timeout_seconds`, `packet_tests.parallel` (#4271, bloc
  optionnel) : le plafond d'une série de tests ciblés d'unité du paquet
  horaire (au-delà, non concluant, canon §3) et le nombre de séries jouées en
  même temps dans les créneaux de l'hôte ; bloc absent : `None`, et
  paquet-horaire le dit sans supposer de valeur.
  `packet_tests.total_timeout_seconds` (#4463, clé optionnelle du bloc) : la
  borne totale des tests par unité d'une composition, au-delà de laquelle les
  séries non parties sont non concluantes et le tic suivant peut partir ;
  absente : `None`, aucune borne totale.
- `projects.<projet>.production.branch` (#3022, canon §5, décision owner
  #122895) : la branche de prod d'un projet, où un lot de porte de production
  (`prod_gate`) doit être présent pour se fermer. Absente : un tel lot n'est
  jamais fermé par une fusion constatée, aucune branche n'est supposée.
- `accounts.pace_multipliers` et `accounts.underused_loss` (#3647, canon §11,
  décision owner #141793 a) : le multiplicateur de rythme de chaque compte de
  forfait, par harnais et nom de compte (absent : ×1), et la perte prévue au
  reset au-delà de laquelle `ameesh accounts` relève lui-même le rythme d'une
  fenêtre (bloc optionnel ; absent : `None`, le réglage local de l'hôte reste
  appliqué et signalé).
- `session.max_tokens` et `session.max_tokens_by_harness` (#4448, bloc
  optionnel) : le seuil de rotation de session (jetons cumulés de la session,
  L11/0018) — la valeur commune, puis la surcharge de chaque harnais
  (`claude`, `codex`, `deepseek`, une famille déclarée). Bloc absent : la
  valeur locale de l'exécuteur (`AMEESH_SESSION_MAX_TOKENS`), jamais un seuil
  supposé ; un harnais sans surcharge prend la valeur commune.

Les classes sensibles et le nombre d'avis par chemin ne sont pas répétés : ils
viennent de `federation.yaml` (`review_policies.risk_classes` et
`review_policies.train_reviews`, PR85), lus au **même commit exact**.

Ce module est le lecteur strict : [`load`] et [`load_global`] refusent (aucun
repli permissif) un document absent, un schéma inconnu, une règle manquante ou
mal formée. `ameesh canon sync` compile le document global pour le signaler
(constat, jamais bloquant) ; un consommateur raccordé — le train #1467, la
désignation #1617 — appelle [`load`] avec son projet et reçoit un objet
immuable qui porte le commit audité.
"""
from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import canon, review
from .deadline import Deadline

#: version du contrat `processus/mecanique.yaml`
SCHEMA = "ameesh-mechanics/1"
#: version du contrat portant le bloc optionnel `prevalidation` (#1770)
SCHEMA_PREVALIDATION = "ameesh-mechanics/2"
#: chemin du document dans le canon
PATH = "processus/mecanique.yaml"
#: déclaration des politiques de revue, lue au même commit
FEDERATION_PATH = "federation.yaml"
#: version du contrat des nombres d'avis versionnés (home PR85)
TRAIN_REVIEWS_SCHEMA = "ameesh-train-reviews-policy/1"
#: modes de CI par paquet acceptés
CI_PACKETS = ("full", "impacted", "none")
#: sort d'un test rouge seulement en CI et vert en local (§11 bis, 15/10/2026 14:45)
ENVIRONMENT_RED = ("does_not_block", "blocks")
#: sort d'un lot fautif dans la CI du paquet (§11 bis)
FAULTY_LOT = ("removed_without_new_review", "reviewed")
#: sort d'un rouge découvert après la fusion (§11 bis)
POST_MERGE_RED = ("forward_or_revert", "freeze")

_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")

_DOCUMENT_KEYS = ("schema", "timezone", "cutoffs", "packets", "build", "ci", "reviews",
                  "merge", "prevalidation", "backlog", "staging", "accounts",
                  "paquet", "packet_tests", "file", "session", "auto_take", "projects")
_OVERRIDE_KEYS = ("timezone", "cutoffs", "packets", "build", "ci", "production")
_PRODUCTION_KEYS = ("branch",)
#: un nom de branche Git simple (`main`, `release/prod`) : ni `..`, ni `@{`,
#: ni fin en `/`, `.` ou `.lock` — jamais une ref arbitraire ni un commit
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_PACKETS_KEYS = ("cutoffs",)
#: classes du plan L161f qu'un bloc `prevalidation` peut sélectionner
PREVALIDATION_CLASSES = ("P0", "P1", "P2", "P3")
_PREVALIDATION_KEYS = ("classes", "contracts", "hosts", "parts_script",
                       "timeout_seconds")
_BUILD_KEYS = ("max_in_build",)
_CI_KEYS = ("packet", "incident_label", "environment_red", "faulty_lot")
_REVIEWS_KEYS = ("families", "exclude_author_family", "distinct_families",
                 "light_reviews")
_MERGE_KEYS = ("named_fire", "repeat_freeze", "on_green_packet", "post_merge_red")
_BACKLOG_KEYS = ("lot_min_value",)
#: #3291 : clés du bloc optionnel `staging` (déploiement hors créneau)
_STAGING_KEYS = ("waiting_lots_min", "before_cutoff_seconds",
                 "before_packet_seconds")
#: #3647 : clés du bloc optionnel `accounts` (rythme des comptes de forfait)
_ACCOUNTS_KEYS = ("pace_multipliers", "underused_loss")
#: #3256 C : clés du bloc optionnel `paquet` (paquets en parallèle, §11 bis)
_PAQUET_KEYS = ("attente_mediane_s", "max_paquets")
#: #4271 : clés du bloc optionnel `packet_tests` (tests ciblés du paquet)
_PACKET_TESTS_KEYS = ("unit_timeout_seconds", "parallel", "total_timeout_seconds")
#: #4463 : clés du bloc `packet_tests` qui peuvent manquer
_PACKET_TESTS_OPTIONAL = ("total_timeout_seconds",)
#: #3256 A : clés du bloc optionnel `file` (déclencheur du lot de découpage)
_FILE_KEYS = ("repos_part", "repos_s", "wake_mail_s")
#: #5005 : routage temporaire des prises automatiques.
_AUTO_TAKE_KEYS = ("excluded_families", "expire")
#: #4448 : clés du bloc optionnel `session` (rotation par harnais)
_SESSION_KEYS = ("max_tokens", "max_tokens_by_harness")
#: bornes du score de valeur d'un élément de backlog (`backlog.add`)
_VALUE_SCORE = (1, 100)


class MechanicsError(canon.CanonError):
    """Règle absente, inconnue ou mal formée : le consommateur refuse."""


class MechanicsAbsent(MechanicsError):
    """Le document n'est pas un fichier ordinaire au commit exact."""


@dataclass(frozen=True)
class ProjectOverride:
    """Les blocs qu'un projet remplace, tels que déclarés (None : non déclaré)."""

    name: str
    timezone: str | None = None
    cutoffs: tuple[str, ...] | None = None
    packet_cutoffs: tuple[str, ...] | None = None
    max_in_build: int | None = None
    ci_packet: str | None = None
    ci_incident_label: str | None = None
    ci_environment_red: str | None = None
    ci_faulty_lot: str | None = None
    #: #3022 : la branche de prod du projet (`production.branch`)
    production_branch: str | None = None


@dataclass(frozen=True)
class Prevalidation:
    """Le bloc optionnel `prevalidation` du canon (#1770).

    Absent (`None`), le consommateur explicite refuse et l'accroche automatique
    reste inactive : aucune valeur ne se suppose. Présent, il porte exactement
    les paramètres relus — classes du plan L161f retenues, contrats toujours
    inclus, hôtes d'exécution autorisés, délai du run distant, et le lanceur de
    part du dépôt quand le bloc le nomme (`parts_script`, `None` sinon : la
    convention du schéma reste au consommateur, jamais au lecteur).
    """

    classes: tuple[str, ...]
    contracts: tuple[str, ...]
    hosts: tuple[str, ...]
    timeout_seconds: int
    parts_script: str | None = None

    def to_dict(self) -> dict:
        out = {
            "classes": list(self.classes),
            "contracts": list(self.contracts),
            "hosts": list(self.hosts),
            "timeout_seconds": self.timeout_seconds,
        }
        if self.parts_script is not None:
            out["parts_script"] = self.parts_script
        return out


@dataclass(frozen=True)
class Staging:
    """#3291: optional canon rule for staging outside the release cutoffs."""

    waiting_lots_min: int
    before_cutoff_seconds: int
    before_packet_seconds: int

    def to_dict(self) -> dict:
        return {"waiting_lots_min": self.waiting_lots_min,
                "before_cutoff_seconds": self.before_cutoff_seconds,
                "before_packet_seconds": self.before_packet_seconds}


@dataclass(frozen=True)
class PaquetParallel:
    """#3256 C : the compiled rule for parallel packets (canon §11 bis).

    `attente_mediane_s` is the previous day's median `qa_merge` wait above
    which the composer opens a second disjoint packet; `max_paquets` is the
    number of packet CIs allowed at once. Absent block: `None`, no parallel
    packet — never a local default."""

    attente_mediane_s: float
    max_paquets: int

    def to_dict(self) -> dict:
        return {"attente_mediane_s": self.attente_mediane_s,
                "max_paquets": self.max_paquets}


@dataclass(frozen=True)
class PacketTests:
    """#4271: optional canon rule for the targeted tests of an hourly packet."""

    unit_timeout_seconds: int
    parallel: int
    #: #4463 : borne totale des tests par unité ; None = clé absente
    total_timeout_seconds: int | None = None

    def to_dict(self) -> dict:
        payload = {"unit_timeout_seconds": self.unit_timeout_seconds,
                   "parallel": self.parallel}
        if self.total_timeout_seconds is not None:
            payload["total_timeout_seconds"] = self.total_timeout_seconds
        return payload


@dataclass(frozen=True)
class Session:
    """#4448 : le seuil de rotation de session, par harnais (canon §7).

    `max_tokens` est la valeur commune (jetons cumulés de la session) ;
    `by_harness` la surcharge d'un harnais déclaré, `()` si aucune. Un harnais
    absent de la table prend la valeur commune — jamais un seuil local
    implicite quand le bloc est là."""

    max_tokens: int
    by_harness: tuple[tuple[str, int], ...] = ()

    def max_tokens_for(self, harness: str) -> float:
        """Le seuil du harnais, sinon la valeur commune du bloc."""
        for name, value in self.by_harness:
            if name == harness:
                return float(value)
        return float(self.max_tokens)

    def to_dict(self) -> dict:
        out: dict = {"max_tokens": self.max_tokens}
        if self.by_harness:
            out["max_tokens_by_harness"] = {
                name: value for name, value in self.by_harness}
        return out


@dataclass(frozen=True)
class AccountsPace:
    """#3647 : le rythme des comptes de forfait, compilé du canon (§11).

    `multipliers` : `((harnais, compte, multiplicateur), …)`, un compte absent
    vaut ×1 ; `underused_loss` : la perte prévue au reset (part du quota de la
    fenêtre) au-delà de laquelle le rythme de la fenêtre est relevé, `None` =
    aucun relèvement déclaré."""

    multipliers: tuple[tuple[str, str, float], ...] = ()
    underused_loss: float | None = None

    def multiplier(self, harness: str, account: str) -> float:
        for item_harness, item_account, value in self.multipliers:
            if (item_harness, item_account) == (harness, account):
                return value
        return 1.0

    def to_dict(self) -> dict:
        out: dict = {"pace_multipliers": {}}
        for harness, account, value in self.multipliers:
            out["pace_multipliers"].setdefault(harness, {})[account] = value
        if self.underused_loss is not None:
            out["underused_loss"] = self.underused_loss
        return out


@dataclass(frozen=True)
class QueueFill:
    """#3256 A : le déclencheur du lot de découpage, compilé du canon (§4).

    `repos_part` : part des agents réveillables d'un projet au repos, dans
    ]0, 1] ; `repos_s` : durée (secondes) pendant laquelle cette part et
    l'absence de lot démarrable doivent tenir.

    #4610 : `wake_mail_s` est la fenêtre (secondes) du courrier récent d'un
    orchestrateur ou du pilote qui retient un agent au moment du sommeil.
    `None` (clé absente du canon) : la même durée que `repos_s`, pour que le
    bloc `file` existant garde son sens sans nouvelle clé obligatoire.

    #4619 : facultative ne veut pas dire déployable tout de suite — la clé
    n'entre au canon qu'une fois que tous les hôtes lisent ce code. Un hôte
    d'une version antérieure l'ignorerait et garderait `repos_s` : son
    sommeil changerait sans le savoir. Le déploiement de la clé suit donc la
    généralisation du lecteur, pas l'inverse."""

    repos_part: float
    repos_s: int
    wake_mail_s: int | None = None

    def mail_window(self) -> float:
        """La fenêtre du courrier récent (#4610), en secondes."""
        return float(self.wake_mail_s if self.wake_mail_s is not None else self.repos_s)

    def to_dict(self) -> dict:
        out = {"repos_part": self.repos_part, "repos_s": self.repos_s}
        if self.wake_mail_s is not None:
            out["wake_mail_s"] = self.wake_mail_s
        return out


@dataclass(frozen=True)
class AutoTake:
    """#5005 : exclusion temporaire des prises lot/backlog, pas des avis."""

    excluded_families: tuple[str, ...]
    expire: date

    def to_dict(self) -> dict:
        return {"excluded_families": list(self.excluded_families),
                "expire": self.expire.isoformat()}


@dataclass(frozen=True)
class Document:
    """Le document global compilé et validé, sans politique de revue."""

    schema: str
    timezone: str
    cutoffs: tuple[str, ...]
    max_in_build: int
    ci_packet: str
    ci_incident_label: str
    ci_environment_red: str
    ci_faulty_lot: str
    families: tuple[str, ...]
    exclude_author_family: bool
    distinct_families: bool
    #: nombre d'avis d'un lot léger (`reviews.light_reviews`, §2) ; 0 = aucune
    #: valeur déclarée (transition bornée à la PR home qui le pose à 1, #2642)
    light_reviews: int
    merge_named_fire: bool
    merge_repeat_freeze: bool
    merge_on_green_packet: bool
    merge_post_merge_red: str
    #: cadence des paquets horaires (`packets.cutoffs`) ; `()` = aucune cadence
    packet_cutoffs: tuple[str, ...] = ()
    overrides: tuple[ProjectOverride, ...] = ()
    prevalidation: Prevalidation | None = None
    #: #2584 : seuil de lot d'un défaut non bloquant ; None = bloc absent
    backlog_lot_min_value: int | None = None
    staging: Staging | None = None
    #: #3647 : rythme des comptes de forfait ; None = bloc absent
    accounts: AccountsPace | None = None
    #: #3256 C : paquets en parallèle ; None = bloc absent (aucun second paquet)
    paquet: PaquetParallel | None = None
    #: #4271 : tests ciblés du paquet horaire ; None = bloc absent
    packet_tests: PacketTests | None = None
    #: #3256 A : déclencheur du lot de découpage ; None = bloc absent
    queue_fill: QueueFill | None = None
    #: #4448 : seuil de rotation de session par harnais ; None = bloc absent
    session: Session | None = None
    auto_take: AutoTake | None = None

    def auto_take_excluded_families(self, now: float) -> tuple[str, ...]:
        """La date d'expiration est incluse, dans le fuseau du canon."""
        if self.auto_take is None:
            return ()
        today = datetime.fromtimestamp(now, ZoneInfo(self.timezone)).date()
        return self.auto_take.excluded_families if today <= self.auto_take.expire else ()

    def override(self, project: str) -> ProjectOverride | None:
        for item in self.overrides:
            if item.name == project:
                return item
        return None

    def session_max_tokens(self, harness: str, fallback: float) -> float:
        """#4448 : le seuil de rotation du harnais, lu du bloc `session`.

        Bloc absent : la valeur commune de l'exécuteur (`fallback`), jamais une
        valeur supposée. Bloc présent : la surcharge du harnais, sinon la
        valeur commune déclarée au canon.
        """
        if self.session is None:
            return float(fallback)
        return self.session.max_tokens_for(harness)

    def production_branch(self, project: str) -> str:
        """#3022 : la branche de prod déclarée du projet, sinon refuse.

        Aucun repli : un projet sans `production.branch` n'a pas de branche de
        prod connue, et le consommateur (`prod_gate`) retient la fermeture."""
        override = self.override(project)
        if override is None or not override.production_branch:
            raise MechanicsError("%s: projects.%s.production.branch is not declared"
                                 % (PATH, project))
        return override.production_branch

    def to_dict(self) -> dict:
        payload = {
            "schema": self.schema,
            "timezone": self.timezone,
            "cutoffs": list(self.cutoffs),
            "packets": {"cutoffs": list(self.packet_cutoffs)},
            "build": {"max_in_build": self.max_in_build},
            "ci": {"packet": self.ci_packet,
                   "incident_label": self.ci_incident_label,
                   "environment_red": self.ci_environment_red,
                   "faulty_lot": self.ci_faulty_lot},
            "reviews": {"families": list(self.families),
                        "exclude_author_family": self.exclude_author_family,
                        "distinct_families": self.distinct_families,
                        "light_reviews": self.light_reviews},
            "merge": {"named_fire": self.merge_named_fire,
                      "repeat_freeze": self.merge_repeat_freeze,
                      "on_green_packet": self.merge_on_green_packet,
                      "post_merge_red": self.merge_post_merge_red},
            "projects": sorted(item.name for item in self.overrides),
        }
        if self.prevalidation is not None:
            # absent, la clé n'existe pas : le document sans bloc compile à l'identique
            payload["prevalidation"] = self.prevalidation.to_dict()
        if self.backlog_lot_min_value is not None:
            payload["backlog"] = {"lot_min_value": self.backlog_lot_min_value}
        if self.staging is not None:
            payload["staging"] = self.staging.to_dict()
        if self.accounts is not None:
            payload["accounts"] = self.accounts.to_dict()
        if self.paquet is not None:
            payload["paquet"] = self.paquet.to_dict()
        if self.packet_tests is not None:
            payload["packet_tests"] = self.packet_tests.to_dict()
        if self.queue_fill is not None:
            payload["file"] = self.queue_fill.to_dict()
        if self.session is not None:
            # absent, la clé n'existe pas : un document sans bloc compile à l'identique
            payload["session"] = self.session.to_dict()
        if self.auto_take is not None:
            payload["auto_take"] = self.auto_take.to_dict()
        return payload


@dataclass(frozen=True)
class ReviewRule:
    """Un nombre d'avis par motif de chemin (`train_reviews`, PR85)."""

    paths: tuple[str, ...]
    reviews: int
    regexes: tuple = field(default=(), compare=False, repr=False)


@dataclass(frozen=True)
class ReviewNeed:
    """Ce qu'un changement demande : classe de risque et nombre d'avis."""

    risk: str
    reviews: int


@dataclass(frozen=True)
class Mechanics:
    """Les paramètres compilés d'un projet, immuables, liés à un commit exact."""

    schema: str
    commit: str
    project: str
    timezone: str
    cutoffs: tuple[str, ...]
    packet_cutoffs: tuple[str, ...]
    max_in_build: int
    ci_packet: str
    ci_incident_label: str
    ci_environment_red: str
    ci_faulty_lot: str
    families: tuple[str, ...]
    exclude_author_family: bool
    distinct_families: bool
    light_reviews: int
    merge_named_fire: bool
    merge_repeat_freeze: bool
    merge_on_green_packet: bool
    merge_post_merge_red: str
    review_policy: review.ReviewPolicy
    rules: tuple[ReviewRule, ...]
    prevalidation: Prevalidation | None = None
    source: str = ""
    #: #2584 : seuil de lot d'un défaut non bloquant ; None = bloc absent
    backlog_lot_min_value: int | None = None
    staging: Staging | None = None
    #: #3256 C : paquets en parallèle ; None = bloc absent
    paquet: PaquetParallel | None = None
    #: #4448 : seuil de rotation de session par harnais ; None = bloc absent
    session: Session | None = None

    # -- coupures -----------------------------------------------------------

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def cutoffs_on(self, day: date) -> tuple[tuple[float, str], ...]:
        """Les coupures d'un jour local, `(epoch UTC, libellé HH:MM)`."""
        zone = self.zone()
        out = []
        for label in self.cutoffs:
            stamp = datetime(day.year, day.month, day.day,
                             int(label[:2]), int(label[3:]), tzinfo=zone)
            out.append((stamp.timestamp(), label))
        return tuple(out)

    def cutoff(self, now: float) -> float:
        """Dernière coupure locale à `now` ou avant."""
        local = datetime.fromtimestamp(now, self.zone())
        for offset in (0, -1):
            before = [stamp for stamp, _ in self.cutoffs_on(local.date() + timedelta(days=offset))
                      if stamp <= now]
            if before:
                return max(before)
        raise MechanicsError("no cutoff declared before %.3f" % now)

    def next_cutoff(self, now: float) -> float:
        """Première coupure locale strictement après `now`."""
        local = datetime.fromtimestamp(now, self.zone())
        for offset in (0, 1):
            after = [stamp for stamp, _ in self.cutoffs_on(local.date() + timedelta(days=offset))
                     if stamp > now]
            if after:
                return min(after)
        raise MechanicsError("no cutoff declared after %.3f" % now)

    # -- paquets horaires ---------------------------------------------------

    def packets_on(self, day: date) -> tuple[tuple[float, str], ...]:
        """Les paquets d'un jour local, `(epoch UTC, libellé HH:MM)`.

        La cadence est celle déclarée (`packets.cutoffs`), jamais une durée
        locale ni un repli sur les coupures de release : sans bloc, la liste
        est vide (#1757). Une heure absente (passage à l'heure d'été) est
        sautée ; une heure répétée (passage à l'heure d'hiver) donne ses deux
        occurrences, distinguées par leur epoch, sans doublon d'epoch.
        """
        zone = self.zone()
        out: list[tuple[float, str]] = []
        for label in self.packet_cutoffs:
            naive = datetime(day.year, day.month, day.day,
                             int(label[:2]), int(label[3:]))
            stamps: list[float] = []
            for fold in (0, 1):
                stamp = naive.replace(tzinfo=zone, fold=fold).timestamp()
                back = datetime.fromtimestamp(stamp, zone).replace(tzinfo=None)
                if back == naive and stamp not in stamps:
                    stamps.append(stamp)
            out.extend((stamp, label) for stamp in stamps)
        out.sort(key=lambda item: item[0])
        return tuple(out)

    def last_packet(self, now: float) -> float:
        """Dernier paquet local à `now` ou avant ; refuse sans cadence déclarée."""
        if not self.packet_cutoffs:
            raise MechanicsError("no packet cadence declared")
        local = datetime.fromtimestamp(now, self.zone())
        # deux jours en arrière : une cadence dont la seule heure est sautée
        # (02:00 au passage à l'heure d'été) n'a aucun paquet ce jour-là
        for offset in (0, -1, -2):
            before = [stamp for stamp, _ in self.packets_on(local.date() + timedelta(days=offset))
                      if stamp <= now]
            if before:
                return max(before)
        raise MechanicsError("no packet declared before %.3f" % now)

    def next_packet(self, now: float) -> float:
        """Premier paquet local strictement après `now` ; refuse sans cadence."""
        if not self.packet_cutoffs:
            raise MechanicsError("no packet cadence declared")
        local = datetime.fromtimestamp(now, self.zone())
        for offset in (0, 1, 2):  # même raison que `last_packet`
            after = [stamp for stamp, _ in self.packets_on(local.date() + timedelta(days=offset))
                     if stamp > now]
            if after:
                return min(after)
        raise MechanicsError("no packet declared after %.3f" % now)

    # -- revue --------------------------------------------------------------

    def reviews_for(self, paths) -> ReviewNeed:
        """Classe et nombre d'avis d'un changement décrit par ses chemins.

        Le nombre d'avis d'un chemin léger part du plancher déclaré
        (`reviews.light_reviews`, §2, #2642) : la classe reste `light`, le
        plancher ne reclasse pas. Une règle de chemin reste prioritaire et
        positive : elle reclasse le chemin en sensible, jamais en léger.

        Un chemin non léger sans nombre d'avis déclaré fait refuser : le
        lecteur ne suppose jamais un repli (`train_reviews`, PR85).
        """
        if isinstance(paths, (str, bytes)):
            raise MechanicsError("a list of repository paths is required")
        try:
            paths = list(paths)
        except TypeError as exc:
            # une entrée non itérable (None, un nombre…) est un appel hors
            # contrat : elle refuse par MechanicsError, jamais par TypeError
            raise MechanicsError("a list of repository paths is required") from exc
        if not paths:
            # un changement sans fichier ne se classe pas : zéro avis serait un repli
            raise MechanicsError("no path to classify")
        risk, reviews = "light", self.light_reviews
        for path in paths:
            if not isinstance(path, str):
                raise MechanicsError("path is not a repository path: %r" % path)
            try:
                path.encode("utf-8")
                review.classify(self.review_policy, [path])
            except (UnicodeError, review.ReviewError) as exc:
                raise MechanicsError("path is not a repository path: %r" % path) from exc
            clean = review.normalize_path(path)
            if clean != path or "\ufffd" in path or "\0" in path:
                # un chemin non décodable (octets remplacés) ne se classe pas :
                # le deviner ferait passer un fichier sensible pour léger
                raise MechanicsError("path is not a repository path: %r" % path)
            cls, _rule = self.review_policy.class_of(clean)
            count = max((rule.reviews for rule in self.rules
                         if any(regex.fullmatch(clean) for regex in rule.regexes)),
                        default=0)
            if cls != "light" and not count:
                raise MechanicsError("review count unknown for non-light path: %s" % clean)
            if count:
                cls = "sensitive"
            if review.RANK[cls] > review.RANK[risk]:
                risk = cls
            reviews = max(reviews, count)
        return ReviewNeed(risk, reviews)

    def to_dict(self) -> dict:
        payload = {
            "schema": self.schema,
            "commit": self.commit,
            "project": self.project,
            "timezone": self.timezone,
            "cutoffs": list(self.cutoffs),
            "packets": {"cutoffs": list(self.packet_cutoffs)},
            "build": {"max_in_build": self.max_in_build},
            "ci": {"packet": self.ci_packet,
                   "incident_label": self.ci_incident_label,
                   "environment_red": self.ci_environment_red,
                   "faulty_lot": self.ci_faulty_lot},
            "reviews": {"families": list(self.families),
                        "exclude_author_family": self.exclude_author_family,
                        "distinct_families": self.distinct_families,
                        "light_reviews": self.light_reviews},
            "merge": {"named_fire": self.merge_named_fire,
                      "repeat_freeze": self.merge_repeat_freeze,
                      "on_green_packet": self.merge_on_green_packet,
                      "post_merge_red": self.merge_post_merge_red},
            "review_rules": [{"paths": list(rule.paths), "reviews": rule.reviews}
                             for rule in self.rules],
        }
        if self.prevalidation is not None:
            # absent, la clé n'existe pas : le document sans bloc compile à l'identique
            payload["prevalidation"] = self.prevalidation.to_dict()
        if self.backlog_lot_min_value is not None:
            payload["backlog"] = {"lot_min_value": self.backlog_lot_min_value}
        if self.staging is not None:
            payload["staging"] = self.staging.to_dict()
        if self.paquet is not None:
            payload["paquet"] = self.paquet.to_dict()
        if self.source:
            payload["source"] = self.source
        return payload


# --------------------------------------------------------------------------
# validation stricte du document
# --------------------------------------------------------------------------

def _mapping(value, where: str, keys) -> dict:
    if not isinstance(value, dict):
        raise MechanicsError("%s: a mapping is required" % where)
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise MechanicsError("%s: unknown key(s): %s" % (where, ", ".join(unknown)))
    return value


def _cutoffs(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise MechanicsError("%s: a non-empty list of \"HH:MM\" is required" % where)
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not _TIME.fullmatch(item):
            raise MechanicsError("%s: local time \"HH:MM\" expected, got %r" % (where, item))
        if item in seen:
            raise MechanicsError("%s: duplicate cutoff %s" % (where, item))
        seen.add(item)
        out.append(item)
    return tuple(out)


def _packets(value, where: str) -> tuple[str, ...]:
    """Le bloc `packets` (#1757) : cadence explicite, jamais une durée locale.

    Bloc présent mais vide, clé inconnue, heure mal formée ou doublon : refus.
    L'absence du bloc n'est pas un repli — `compile_document` la traduit en
    tuple vide, et `last_packet`/`next_packet` refusent alors explicitement.
    """
    data = _mapping(value, where, _PACKETS_KEYS)
    return _cutoffs(data.get("cutoffs"), where + ".cutoffs")


def _positive_int(value, where: str) -> int:
    if type(value) is not int or value <= 0:
        raise MechanicsError("%s: a positive integer is required" % where)
    return value


def _review_floor(value, where: str) -> int:
    """Le nombre d'avis d'un lot léger : 0 (aucune valeur déclarée) ou 1 (§2).

    Le canon §2 demande un avis d'une autre famille pour un lot léger ; 0
    n'est admis que le temps de la PR home qui pose `light_reviews: 1`
    (#2642). Aucun autre nombre n'est une règle de revue.
    """
    if type(value) is not int or value not in (0, 1):
        raise MechanicsError("%s: 0 or 1 expected" % where)
    return value


def _boolean(value, where: str) -> bool:
    if type(value) is not bool:
        raise MechanicsError("%s: a boolean is required" % where)
    return value


def _repository_path(value) -> bool:
    """Un chemin de dépôt canonique : relatif, POSIX, qui ne sort pas du dépôt."""
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or "\ufffd" in value or "\0" in value):
        return False
    depth = 0
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return review.normalize_path(value) == value


def _prevalidation(value, where: str) -> Prevalidation:
    """Compile le bloc `prevalidation` : chaque paramètre est relu, jamais supposé."""
    data = _mapping(value, where, _PREVALIDATION_KEYS)
    classes = data.get("classes")
    if not isinstance(classes, list) or not classes:
        raise MechanicsError("%s.classes: a non-empty list of classes is required" % where)
    seen: set[str] = set()
    for item in classes:
        if not isinstance(item, str) or item not in PREVALIDATION_CLASSES:
            raise MechanicsError("%s.classes: unknown class %r (known: %s)"
                                 % (where, item, ", ".join(PREVALIDATION_CLASSES)))
        if item in seen:
            raise MechanicsError("%s.classes: duplicate class %s" % (where, item))
        seen.add(item)
    # l'ordre du canon est canonique : P0 puis P1, quelle que soit la déclaration
    ordered = tuple(name for name in PREVALIDATION_CLASSES if name in seen)
    contracts = data.get("contracts")
    if not isinstance(contracts, list):
        raise MechanicsError("%s.contracts: a list of repository paths is required" % where)
    out_contracts: list[str] = []
    for item in contracts:
        if not _repository_path(item):
            raise MechanicsError("%s.contracts: not a repository path: %r" % (where, item))
        if item in out_contracts:
            raise MechanicsError("%s.contracts: duplicate path %s" % (where, item))
        out_contracts.append(item)
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise MechanicsError("%s.hosts: a non-empty list of hosts is required" % where)
    out_hosts: list[str] = []
    for item in hosts:
        if not isinstance(item, str) or not _LABEL.fullmatch(item):
            raise MechanicsError("%s.hosts: a host label is required, got %r" % (where, item))
        if item in out_hosts:
            raise MechanicsError("%s.hosts: duplicate host %s" % (where, item))
        out_hosts.append(item)
    # le lanceur de part est optionnel : absent, la convention du schéma
    # s'applique chez le consommateur ; déclaré, il est relu comme un chemin
    return Prevalidation(
        classes=ordered,
        contracts=tuple(out_contracts),
        hosts=tuple(out_hosts),
        timeout_seconds=_positive_int(data.get("timeout_seconds"),
                                      where + ".timeout_seconds"),
        parts_script=_parts_script(data, where),
    )


def _parts_script(data, where: str) -> str | None:
    """Le lanceur de part déclaré (`None` : la convention du schéma).

    Un `null` explicite refuse : la clé déclarée doit porter un chemin, comme
    les contrats. Absente, la clé ne suppose rien — c'est le consommateur qui
    applique `scripts/test-parts.py`."""
    if "parts_script" not in data:
        return None
    value = data["parts_script"]
    if not _repository_path(value):
        raise MechanicsError("%s.parts_script: not a repository path: %r"
                             % (where, value))
    return value


def _timezone(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise MechanicsError("%s: an IANA timezone is required" % where)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise MechanicsError("%s: unknown IANA timezone %r" % (where, value)) from exc
    return value


def _ci(value, where: str) -> tuple[str, str, str, str]:
    data = _mapping(value, where, _CI_KEYS)
    packet = data.get("packet")
    if packet not in CI_PACKETS:
        raise MechanicsError("%s.packet: %s expected" % (where, " | ".join(CI_PACKETS)))
    label = data.get("incident_label")
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise MechanicsError("%s.incident_label: a label is required" % where)
    environment = data.get("environment_red")
    if environment not in ENVIRONMENT_RED:
        raise MechanicsError("%s.environment_red: %s expected"
                             % (where, " | ".join(ENVIRONMENT_RED)))
    faulty = data.get("faulty_lot")
    if faulty not in FAULTY_LOT:
        raise MechanicsError("%s.faulty_lot: %s expected" % (where, " | ".join(FAULTY_LOT)))
    return packet, label, environment, faulty


def _merge(value, where: str) -> tuple[bool, bool, bool, str]:
    """La règle de fusion du paquet (§11 bis, allègements 15/10/2026 14:45)."""
    data = _mapping(value, where, _MERGE_KEYS)
    post = data.get("post_merge_red")
    if post not in POST_MERGE_RED:
        raise MechanicsError("%s.post_merge_red: %s expected"
                             % (where, " | ".join(POST_MERGE_RED)))
    return (_boolean(data.get("named_fire"), where + ".named_fire"),
            _boolean(data.get("repeat_freeze"), where + ".repeat_freeze"),
            _boolean(data.get("on_green_packet"), where + ".on_green_packet"),
            post)


def _backlog(value, where: str) -> int:
    """Le bloc `backlog` (#2584, canon §2 l.129) : le seuil de lot, dans les
    bornes du score de valeur d'un élément (1 à 100)."""
    data = _mapping(value, where, _BACKLOG_KEYS)
    threshold = data.get("lot_min_value")
    low, high = _VALUE_SCORE
    if type(threshold) is not int or not low <= threshold <= high:
        raise MechanicsError("%s.lot_min_value: an integer from %d to %d is required"
                             % (where, low, high))
    return threshold


def _production(value, where: str) -> str:
    """Le bloc `production` d'un projet (#3022) : sa branche de prod, nom de
    branche Git simple, jamais un commit ni une ref complète."""
    data = _mapping(value, where, _PRODUCTION_KEYS)
    branch = data.get("branch")
    if (not isinstance(branch, str) or not _BRANCH.fullmatch(branch)
            or ".." in branch or "//" in branch or branch.endswith((".", "/", ".lock"))
            or branch.startswith(("refs/", "origin/")) or _SHA.fullmatch(branch)):
        raise MechanicsError("%s.branch: a plain branch name is required" % where)
    return branch


def _families(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise MechanicsError("%s: a non-empty list of families is required" % where)
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise MechanicsError("%s: a family name is required" % where)
        if item in out:
            raise MechanicsError("%s: duplicate family %s" % (where, item))
        out.append(item)
    return tuple(out)


def _overrides(value, where: str) -> tuple[ProjectOverride, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise MechanicsError("%s: a mapping is required" % where)
    out = []
    for name, block in value.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise MechanicsError("%s: non-empty project names are required" % where)
        scope = "%s.%s" % (where, name)
        data = _mapping(block, scope, _OVERRIDE_KEYS)
        timezone = cutoffs = None
        packet_cutoffs = None
        max_in_build = None
        packet = label = environment = faulty = None
        production = None
        if "timezone" in data:
            timezone = _timezone(data["timezone"], scope + ".timezone")
        if "cutoffs" in data:
            cutoffs = _cutoffs(data["cutoffs"], scope + ".cutoffs")
        if "packets" in data:
            # bloc entier remplacé, comme `cutoffs` : jamais une fusion partielle
            packet_cutoffs = _packets(data["packets"], scope + ".packets")
        if "build" in data:
            build = _mapping(data["build"], scope + ".build", _BUILD_KEYS)
            max_in_build = _positive_int(build.get("max_in_build"),
                                         scope + ".build.max_in_build")
        if "ci" in data:
            packet, label, environment, faulty = _ci(data["ci"], scope + ".ci")
        if "production" in data:
            production = _production(data["production"], scope + ".production")
        out.append(ProjectOverride(name=name, timezone=timezone, cutoffs=cutoffs,
                                   packet_cutoffs=packet_cutoffs,
                                   max_in_build=max_in_build, ci_packet=packet,
                                   ci_incident_label=label,
                                   ci_environment_red=environment,
                                   ci_faulty_lot=faulty,
                                   production_branch=production))
    return tuple(out)


def _staging(value, where: str) -> Staging:
    data = _mapping(value, where, _STAGING_KEYS)
    return Staging(*(_positive_int(data.get(key), where + "." + key)
                     for key in _STAGING_KEYS))


def _paquet(value, where: str) -> PaquetParallel:
    """#3256 C : le bloc `paquet` (seuil d'attente et plafond de paquets).

    `attente_mediane_s` est une durée en secondes strictement positive (le
    seuil du canon est `3600`, jamais recopié) ; `max_paquets` vaut exactement 2
    (seuls deux groupes sont implémentés)."""
    data = _mapping(value, where, _PAQUET_KEYS)
    attente = _positive_int(data.get("attente_mediane_s"),
                            where + ".attente_mediane_s")
    maximum = _positive_int(data.get("max_paquets"), where + ".max_paquets")
    if maximum != 2:
        raise MechanicsError("%s.max_paquets: exactly 2 expected" % where)
    return PaquetParallel(attente_mediane_s=float(attente), max_paquets=maximum)


def _packet_tests(value, where: str) -> PacketTests:
    data = _mapping(value, where, _PACKET_TESTS_KEYS)
    return PacketTests(*(_positive_int(data.get(key), where + "." + key)
                         for key in _PACKET_TESTS_KEYS
                         if key in data or key not in _PACKET_TESTS_OPTIONAL))


def _session(value, where: str, families: tuple[str, ...]) -> Session:
    """#4448 : le bloc `session` (seuil de rotation par harnais).

    `max_tokens` est requis et strictement positif ; `max_tokens_by_harness`
    optionnel, chaque clé étant une famille de harnais déclarée par
    `reviews.families` (une faute de frappe refuse, elle ne crée pas un
    harnais silencieux), chaque valeur un entier strictement positif.
    """
    data = _mapping(value, where, _SESSION_KEYS)
    common = _positive_int(data.get("max_tokens"), where + ".max_tokens")
    table = data.get("max_tokens_by_harness", {})
    if not isinstance(table, dict):
        raise MechanicsError("%s.max_tokens_by_harness: a mapping is required" % where)
    items: list[tuple[str, int]] = []
    for harness, tokens in table.items():
        name = _name(harness, where + ".max_tokens_by_harness")
        if name not in families:
            raise MechanicsError(
                "%s.max_tokens_by_harness.%s: not a declared harness family (%s)"
                % (where, name, ", ".join(families)))
        items.append((name, _positive_int(
            tokens, "%s.max_tokens_by_harness.%s" % (where, name))))
    return Session(max_tokens=common, by_harness=tuple(sorted(items)))


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise MechanicsError("%s: a non-empty name is required" % where)
    return value


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MechanicsError("%s: a number is required" % where)
    number = float(value)
    if not math.isfinite(number):
        raise MechanicsError("%s: a finite number is required" % where)
    return number


def _accounts(value, where: str) -> AccountsPace:
    """Le bloc `accounts` (#3647, canon §11) : `pace_multipliers` par harnais
    puis compte (nombre fini > 0), `underused_loss` dans ]0, 1[ (optionnel)."""
    data = _mapping(value, where, _ACCOUNTS_KEYS)
    multipliers = []
    table = data.get("pace_multipliers", {})
    if not isinstance(table, dict):
        raise MechanicsError("%s.pace_multipliers: a mapping is required" % where)
    for harness, accounts in table.items():
        scope = "%s.pace_multipliers.%s" % (where, _name(harness, where + ".pace_multipliers"))
        if not isinstance(accounts, dict) or not accounts:
            raise MechanicsError("%s: a non-empty mapping {account: multiplier} is required"
                                 % scope)
        for account, multiplier in accounts.items():
            item = "%s.%s" % (scope, _name(account, scope))
            multiplier = _number(multiplier, item)
            if multiplier <= 0:
                raise MechanicsError("%s: a multiplier > 0 is required" % item)
            multipliers.append((harness, account, multiplier))
    loss = None
    if "underused_loss" in data:
        loss = _number(data["underused_loss"], where + ".underused_loss")
        if not 0 < loss < 1:
            raise MechanicsError("%s.underused_loss: a share in ]0, 1[ is required" % where)
    return AccountsPace(multipliers=tuple(multipliers), underused_loss=loss)


def _queue_fill(value, where: str) -> QueueFill:
    """Le bloc `file` (#3256 A, canon §4) : `repos_part` dans ]0, 1],
    `repos_s` entier > 0 ; les deux sont requis. #4610 : `wake_mail_s`,
    facultatif, entier > 0 (défaut : `repos_s`)."""
    data = _mapping(value, where, _FILE_KEYS)
    part = _number(data.get("repos_part"), where + ".repos_part")
    if not 0 < part <= 1:
        raise MechanicsError("%s.repos_part: a share in ]0, 1] is required" % where)
    wake = data.get("wake_mail_s")
    return QueueFill(repos_part=part,
                     repos_s=_positive_int(data.get("repos_s"), where + ".repos_s"),
                     wake_mail_s=(_positive_int(wake, where + ".wake_mail_s")
                                  if wake is not None else None))


def _auto_take(value, where: str, families: tuple[str, ...]) -> AutoTake:
    data = _mapping(value, where, _AUTO_TAKE_KEYS)
    excluded = data.get("excluded_families")
    if not isinstance(excluded, list):
        raise MechanicsError("%s.excluded_families: a list is required" % where)
    names = _families(excluded, where + ".excluded_families") if excluded else ()
    if any(name not in families for name in names):
        raise MechanicsError("%s.excluded_families: unknown family" % where)
    expiry = data.get("expire")
    if not isinstance(expiry, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expiry):
        raise MechanicsError("%s.expire: an ISO date YYYY-MM-DD is required" % where)
    try:
        end = date.fromisoformat(expiry)
    except ValueError as exc:
        raise MechanicsError("%s.expire: invalid date" % where) from exc
    return AutoTake(names, end)


def compile_document(text: str, *, source: str = PATH) -> Document:
    """Compile le document global. Strict : toute règle manquante ou inconnue refuse."""
    try:
        # lecteur minimal forcé : la compilation est identique avec et sans PyYAML
        parsed = canon.load_yaml(text, use_pyyaml=False)
    except canon.YamlError as exc:
        raise MechanicsError("%s: unreadable YAML: %s" % (source, exc)) from exc
    if not isinstance(parsed, dict):
        raise MechanicsError("%s: a mapping is required" % source)
    unknown = sorted(set(parsed) - set(_DOCUMENT_KEYS))
    if unknown:
        raise MechanicsError("%s: unknown key(s): %s" % (source, ", ".join(unknown)))
    schema = parsed.get("schema")
    if schema not in (SCHEMA, SCHEMA_PREVALIDATION):
        raise MechanicsError("%s: schema %s is required, got %r"
                             % (source, SCHEMA, schema))
    has_block = "prevalidation" in parsed
    if schema == SCHEMA and has_block:
        # le bloc change le contrat : un lecteur antérieur refuserait la clé
        raise MechanicsError("%s: prevalidation requires schema %s"
                             % (source, SCHEMA_PREVALIDATION))
    if schema == SCHEMA_PREVALIDATION and not has_block:
        raise MechanicsError("%s: schema %s requires the prevalidation block"
                             % (source, SCHEMA_PREVALIDATION))
    build = _mapping(parsed.get("build"), source + ".build", _BUILD_KEYS)
    reviews = _mapping(parsed.get("reviews"), source + ".reviews", _REVIEWS_KEYS)
    # `light_reviews` absent : 0, la transition explicitement bornée à la PR
    # home qui le pose à 1 (#2642) ; un zéro déclaré se dit, jamais un implicite.
    light_reviews = _review_floor(reviews.get("light_reviews", 0),
                                  source + ".reviews.light_reviews")
    packet, label, environment, faulty = _ci(parsed.get("ci"), source + ".ci")
    named_fire, repeat_freeze, on_green, post_merge = _merge(parsed.get("merge"),
                                                             source + ".merge")
    # `packets` optionnel : absent = aucune cadence (tuple vide explicite), un
    # bloc présent — même vide — est validé strictement et refusé s'il est mal formé.
    packets = _packets(parsed["packets"], source + ".packets") if "packets" in parsed else ()
    # `backlog` optionnel (#2584) : absent = None, la route du panier reste
    # inactive ; présent, il est validé strictement.
    lot_min_value = (_backlog(parsed["backlog"], source + ".backlog")
                     if "backlog" in parsed else None)
    families = _families(reviews.get("families"), source + ".reviews.families")
    return Document(
        schema=schema,
        timezone=_timezone(parsed.get("timezone"), source + ".timezone"),
        cutoffs=_cutoffs(parsed.get("cutoffs"), source + ".cutoffs"),
        max_in_build=_positive_int(build.get("max_in_build"),
                                   source + ".build.max_in_build"),
        ci_packet=packet,
        ci_incident_label=label,
        ci_environment_red=environment,
        ci_faulty_lot=faulty,
        families=families,
        exclude_author_family=_boolean(reviews.get("exclude_author_family"),
                                       source + ".reviews.exclude_author_family"),
        distinct_families=_boolean(reviews.get("distinct_families"),
                                   source + ".reviews.distinct_families"),
        light_reviews=light_reviews,
        merge_named_fire=named_fire,
        merge_repeat_freeze=repeat_freeze,
        merge_on_green_packet=on_green,
        merge_post_merge_red=post_merge,
        packet_cutoffs=packets,
        overrides=_overrides(parsed.get("projects"), source + ".projects"),
        prevalidation=(_prevalidation(parsed["prevalidation"],
                                      source + ".prevalidation") if has_block else None),
        backlog_lot_min_value=lot_min_value,
        staging=(_staging(parsed["staging"], source + ".staging")
                 if "staging" in parsed else None),
        accounts=(_accounts(parsed["accounts"], source + ".accounts")
                  if "accounts" in parsed else None),
        paquet=(_paquet(parsed["paquet"], source + ".paquet")
                if "paquet" in parsed else None),
        packet_tests=(_packet_tests(parsed["packet_tests"], source + ".packet_tests")
                      if "packet_tests" in parsed else None),
        queue_fill=(_queue_fill(parsed["file"], source + ".file")
                    if "file" in parsed else None),
        auto_take=(_auto_take(parsed["auto_take"], source + ".auto_take", families)
                   if "auto_take" in parsed else None),
        session=(_session(parsed["session"], source + ".session", families)
                 if "session" in parsed else None),
    )


# --------------------------------------------------------------------------
# lecture au commit exact
# --------------------------------------------------------------------------

def _git_timeout(deadline: Deadline | None, what: str) -> float:
    """Le délai d'une lecture git : le plafond du canon, ramené au temps restant.

    Une échéance épuisée refuse avant l'appel (#2665) : la lecture ne part pas
    pour découvrir après coup qu'elle n'avait plus de budget.
    """
    if deadline is None:
        return canon.GIT_TIMEOUT
    if deadline.expired():
        raise MechanicsError("deadline exhausted before %s" % what)
    return deadline.timeout(canon.GIT_TIMEOUT)


def _exact_commit(repo: str, commit: str, *,
                  deadline: Deadline | None = None) -> str:
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise MechanicsError("an exact canon commit is required")
    try:
        resolved = canon.git_commit(
            repo, commit, timeout=_git_timeout(deadline, "the canon commit"))
    except (canon.GitError, OSError) as exc:
        if deadline is not None and deadline.expired():
            raise MechanicsError("deadline exhausted while reading the canon commit") from None
        raise MechanicsError("cannot read the canon repository: %s" % exc) from exc
    if resolved != commit:
        raise MechanicsError("an exact canon commit is required")
    return commit


def _regular_blob(repo: str, commit: str, path: str, *,
                  deadline: Deadline | None = None) -> str:
    """Le texte d'un fichier ordinaire (`path`) au commit exact, sinon refuse.

    `_git_list` est le lecteur d'arbre du canon : il ne retient que les
    fichiers ordinaires (ni lien symbolique, ni sous-module), donc un chemin
    qui n'en est pas un est refusé comme absent. Avec ``deadline`` (#2665),
    chaque lecture git ne prend que le temps qui reste au passage.
    """
    try:
        listed = canon._git_list(
            repo, commit, path, timeout=_git_timeout(deadline, "the canon tree"))
    except (canon.GitError, OSError) as exc:
        if deadline is not None and deadline.expired():
            raise MechanicsError("deadline exhausted while reading the canon tree") from None
        raise MechanicsError("cannot read the canon tree: %s" % exc) from exc
    if len([sha for name, sha in listed if name == path]) != 1:
        raise MechanicsAbsent("%s is absent at %s" % (path, commit))
    raw = canon.git_read(
        repo, commit, path, timeout=_git_timeout(deadline, "the canon blob"))
    if raw is None:
        if deadline is not None and deadline.expired():
            raise MechanicsError("deadline exhausted while reading %s at %s"
                                 % (path, commit))
        raise MechanicsAbsent("%s is unreadable at %s" % (path, commit))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MechanicsError("%s is not UTF-8 at %s" % (path, commit)) from exc


def load_global(repo: str, commit: str, *,
                deadline: Deadline | None = None) -> Document:
    """Le document global (`processus/mecanique.yaml`) au commit exact."""
    commit = _exact_commit(repo, commit, deadline=deadline)
    return compile_document(_regular_blob(repo, commit, PATH, deadline=deadline),
                            source=PATH)


#: documents globaux déjà compilés, par `(dépôt, commit exact)` : un commit
#: ne change pas, la passe d'un exécuteur ne relit donc pas le blob par lot
_CONFIGURED: dict = {}


def configured(cfg) -> Document:
    """Le document global du canon appliqué (#2451), sans repli.

    Même source que le constat de `ameesh canon sync` (`sync_mechanics`) : la
    première source du canon par défaut, Git et épinglée, à la racine du dépôt.
    Aucun canon lisible, une source non Git, un canon en sous-répertoire ou un
    document absent refusent par `MechanicsError` (`MechanicsAbsent` pour le
    document manquant) : un consommateur historique — plafond d'en-cours,
    familles de relecture — ne se rabat jamais sur une valeur locale.
    """
    canons = canon.load_configured(cfg) if cfg is not None else []
    if not canons or not canons[0].sources:
        raise MechanicsError("no readable canon: compiled mechanics unavailable")
    source = canons[0].sources[0]
    if source.mode != "git" or not source.repo or not source.commit:
        raise MechanicsError("no exact canon commit applied: "
                             "compiled mechanics unavailable")
    if source.prefix:
        raise MechanicsError("canon under a sub-directory (%s): the mechanics are "
                             "read at the Git root only" % source.prefix)
    key = (source.repo, source.commit)
    if key not in _CONFIGURED:
        _CONFIGURED[key] = load_global(source.repo, source.commit)
    return _CONFIGURED[key]


def session_max_tokens(cfg, harness: str, fallback: float) -> float:
    """#4448 : le seuil de rotation du harnais, lu du canon appliqué.

    Le bloc `session` fait foi (surcharge du harnais, sinon sa valeur commune).
    Un canon absent ou illisible, ou un document sans bloc, laisse la valeur
    locale `fallback` — le seuil commun de l'exécuteur
    (`AMEESH_SESSION_MAX_TOKENS`) : c'est le seul repli permis, et il ne
    fournit jamais une valeur propre à un harnais.
    """
    try:
        document = configured(cfg)
    except Exception:  # noqa: BLE001 — canon absent/illisible : repli local, jamais fatal
        return float(fallback)
    return document.session_max_tokens(harness, fallback)


@functools.lru_cache(maxsize=256)
def light_reviews_at(repo: str, commit: str) -> int:
    """Le plancher d'avis d'un lot léger au commit exact (#2642).

    Un seul lecteur pour les consommateurs qui n'ont pas déjà l'objet compilé
    (le lecteur de classe, donc la porte de fusion et `paquet-candidats`) :
    `compile_document`, strict, et le même commit que la classification.
    `MechanicsAbsent` remonte tel quel : un canon sans document n'est pas un
    zéro implicite, c'est au consommateur de dire ce qu'il en fait.
    """
    return load_global(repo, commit).light_reviews


@functools.lru_cache(maxsize=256)
def review_flags_at(repo: str, commit: str) -> tuple[bool, bool]:
    """Les drapeaux `reviews` au commit exact (#2726).

    `(exclude_author_family, distinct_families)`, lus dans le même document
    `mechanismes` que le plancher léger et la politique de classe. Le lecteur
    de classe les porte dans `klass` ; la porte de fusion et le train les
    appliquent au lieu de supposer un repli. `MechanicsAbsent` et
    `MechanicsError` remontent tels quels, comme `light_reviews_at`.
    """
    document = load_global(repo, commit)
    return document.exclude_author_family, document.distinct_families


def _federation(repo: str, commit: str, *,
                deadline: Deadline | None = None) -> dict:
    try:
        parsed = canon.load_yaml(
            _regular_blob(repo, commit, FEDERATION_PATH, deadline=deadline),
            use_pyyaml=False)
    except canon.YamlError as exc:
        raise MechanicsError("%s: unreadable YAML: %s" % (FEDERATION_PATH, exc)) from exc
    if not isinstance(parsed, dict):
        raise MechanicsError("%s: a mapping is required" % FEDERATION_PATH)
    return parsed


def _declared(mapping, key: str, where: str) -> dict:
    if not isinstance(mapping, dict) or key not in mapping or not isinstance(mapping[key], dict):
        raise MechanicsError("%s is required" % where)
    return mapping[key]


def declared_projects(repo: str, commit: str, *,
                      deadline: Deadline | None = None) -> tuple[str, ...]:
    """Les projets que `review_policies.risk_classes.projects` déclare.

    Lu au commit exact, sans compiler les barèmes de chaque projet : un canon
    peut déclarer une classe sans bloc `train_reviews` complet (`omarchy`), et
    cette lecture ne le refuse pas ; c'est `load` qui dit si le projet se
    compile. Un canon **sans** `review_policies` ou sans `risk_classes` rend
    `()` (aucune politique) ; un bloc présent mais mal formé refuse, pour que
    l'appelant distingue « rien à mesurer » d'une politique cassée.
    """
    commit = _exact_commit(repo, commit, deadline=deadline)
    federation = _federation(repo, commit, deadline=deadline)
    policies = federation.get("review_policies")
    if policies is None:
        return ()
    if not isinstance(policies, dict):
        raise MechanicsError("review_policies must be a mapping")
    classes = policies.get("risk_classes")
    if classes is None:
        return ()
    if not isinstance(classes, dict):
        raise MechanicsError("review_policies.risk_classes must be a mapping")
    projects = classes.get("projects")
    if projects is None:
        return ()
    if not isinstance(projects, dict):
        raise MechanicsError("review_policies.risk_classes.projects must be a mapping")
    return tuple(sorted(name for name in projects
                        if isinstance(name, str) and name.strip()))


def _review_policy(federation: dict, project: str, commit: str
                   ) -> tuple[review.ReviewPolicy, tuple[ReviewRule, ...]]:
    """`risk_classes` et `train_reviews` du projet, au même commit exact."""
    policies = _declared(federation, "review_policies", "review_policies")
    classes = _declared(policies, "risk_classes", "review_policies.risk_classes")
    scoped = _declared(classes, "projects", "review_policies.risk_classes.projects")
    if project not in scoped:
        raise MechanicsError("project %r is not declared under risk_classes.projects"
                             % project)
    declaration = scoped[project]
    # `default` seul est une politique complète (review.parse) ; `rules` absent = []
    if not isinstance(declaration, dict) or "default" not in declaration \
            or set(declaration) - {"default", "rules"}:
        raise MechanicsError("complete risk classification required for project %r" % project)
    risk_rules = declaration.get("rules", [])
    if not isinstance(risk_rules, list) or any(
            not isinstance(rule, dict) or set(rule) != {"paths", "class"}
            for rule in risk_rules):
        raise MechanicsError("explicit risk rules required for project %r" % project)
    policy, problems = review.parse(declaration)
    if problems:
        raise MechanicsError("risk classification of %r is unusable: %s"
                             % (project, "; ".join(sorted({text for _code, _sev, text
                                                            in problems}))))
    counts = _declared(policies, "train_reviews", "review_policies.train_reviews")
    if set(counts) != {"schema", "projects"} or counts.get("schema") != TRAIN_REVIEWS_SCHEMA:
        raise MechanicsError("unknown train review policy schema: %r"
                             % (counts.get("schema") if isinstance(counts, dict) else counts))
    projects = _declared(counts, "projects", "review_policies.train_reviews.projects")
    if project not in projects:
        raise MechanicsError("project %r has no declared train review rules" % project)
    entry = projects[project]
    if not isinstance(entry, dict) or set(entry) != {"rules"} \
            or not isinstance(entry["rules"], list):
        raise MechanicsError("explicit train review rules required for project %r" % project)
    rules: list[ReviewRule] = []
    for index, rule in enumerate(entry["rules"]):
        where = "review_policies.train_reviews.projects.%s.rules[%d]" % (project, index)
        if not isinstance(rule, dict) or set(rule) != {"paths", "reviews"}:
            raise MechanicsError("%s: `paths` and `reviews` are required" % where)
        if type(rule["reviews"]) is not int or rule["reviews"] not in (1, 2):
            raise MechanicsError("%s.reviews: 1 or 2 expected" % where)
        paths = rule["paths"]
        if not isinstance(paths, list) or not paths \
                or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise MechanicsError("%s.paths: a non-empty list of patterns is required" % where)
        try:
            regexes = tuple(review.compile_glob(p) for p in paths)
        except re.error as exc:
            raise MechanicsError("%s.paths: invalid pattern (%s)" % (where, exc)) from exc
        rules.append(ReviewRule(paths=tuple(paths), reviews=rule["reviews"],
                                regexes=regexes))
    return policy, tuple(rules)


def load(repo: str, commit: str, project: str, *,
         deadline: Deadline | None = None) -> Mechanics:
    """Compile les paramètres d'un projet depuis le canon, au commit exact.

    Aucun repli : projet inconnu, bloc absent, schéma inconnu ou règle mal
    formée refusent, et `commit` doit être un commit exact (jamais une
    branche ou un tag). Avec ``deadline`` (#2665), les lectures git du canon
    ne prennent que le temps qui reste au passage.
    """
    if not isinstance(project, str) or not project.strip() or project != project.strip():
        raise MechanicsError("an explicit project is required")
    commit = _exact_commit(repo, commit, deadline=deadline)
    document = compile_document(_regular_blob(repo, commit, PATH, deadline=deadline),
                                source=PATH)
    override = document.override(project)
    federation = _federation(repo, commit, deadline=deadline)
    policy, rules = _review_policy(federation, project, commit)
    return Mechanics(
        schema=document.schema,
        commit=commit,
        project=project,
        timezone=(override.timezone if override and override.timezone
                  else document.timezone),
        cutoffs=(override.cutoffs if override and override.cutoffs
                 else document.cutoffs),
        # `packets` se remplace en bloc, présent ou absent : un override sans
        # `packets` hérite du bloc global, il ne le vide jamais.
        packet_cutoffs=(override.packet_cutoffs
                        if override and override.packet_cutoffs is not None
                        else document.packet_cutoffs),
        max_in_build=(override.max_in_build if override and override.max_in_build
                      else document.max_in_build),
        ci_packet=(override.ci_packet if override and override.ci_packet
                   else document.ci_packet),
        ci_incident_label=(override.ci_incident_label if override
                           and override.ci_incident_label
                           else document.ci_incident_label),
        ci_environment_red=(override.ci_environment_red if override
                            and override.ci_environment_red
                            else document.ci_environment_red),
        ci_faulty_lot=(override.ci_faulty_lot if override and override.ci_faulty_lot
                       else document.ci_faulty_lot),
        families=document.families,
        exclude_author_family=document.exclude_author_family,
        distinct_families=document.distinct_families,
        light_reviews=document.light_reviews,
        merge_named_fire=document.merge_named_fire,
        merge_repeat_freeze=document.merge_repeat_freeze,
        merge_on_green_packet=document.merge_on_green_packet,
        merge_post_merge_red=document.merge_post_merge_red,
        review_policy=policy,
        rules=rules,
        prevalidation=document.prevalidation,
        source="%s@%s:%s" % (repo, commit, PATH),
        backlog_lot_min_value=document.backlog_lot_min_value,
        staging=document.staging,
        paquet=document.paquet,
        session=document.session,
    )
