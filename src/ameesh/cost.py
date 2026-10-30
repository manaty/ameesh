#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Coût des tours et jauges de forfait (L12, R20, décision 0019).

  ameesh cost report                  tableau : agent, harnais, modèle, dépense 1 h/24 h, jauges
  ameesh cost spent <agent|all> [s]   dépense (USD) sur les <s> dernières secondes (3600 par défaut)
  ameesh cost over <agent>            code 0 si le rythme du forfait de l'agent est dépassé

**Lecture seule des harnais.** On ne fait que lire leurs journaux, jamais y
écrire :

  * Claude : `rate_limit_event.unifiedWindows` (fenêtres cinq heures et sept
    jours) dans le stream-json `events.jsonl` de l'agent ;
  * Codex  : `rate_limits.primary`/`secondary` (`used_percent`,
    `window_minutes`, `resets_at`) dans les journaux de session
    `<sessions>/**/*.jsonl` — sessions : argument > `AMEESH_CODEX_SESSIONS`
    (surcharge explicite) > `$CODEX_HOME/sessions` > `~/.codex/sessions`.

**Comptabilité par tour** (décision 0019 §3) :

  * Claude   : différence du `total_cost_usd` cumulé de la session ;
  * Codex    : différence de l'usage cumulé porté par `turn.completed` ;
  * DeepSeek : usage par étape (`status`/`step_end`) × barème ;
  * résident : l'événement `resident.turn` écrit une fois par tour depuis son
    `TurnResult` (Codex app-server, ACP ; L80a), qui fait foi seul pour ce
    tour — les relevés natifs de la même tranche ne sont pas recomptés.

Le barème est un fichier JSON (USD par million de jetons, entrée
`[entrée, entrée en cache, sortie]`), lu dans `$AMEESH_PRICES` ou au chemin par
défaut du poste de travail ; les modèles qu'il ne nomme pas retombent sur les
défauts ci-dessous. Il est modifiable sans migration.

**Garde-fou de rythme** : un forfait avance trop vite quand son utilisation
atteint `min(90 %, part écoulée de la fenêtre + 10 points)`. La marge de dix
points couvre l'orchestrateur et les humains, qui partagent le même forfait
(décision 0019 §4).

Rien ici n'écrit dans les journaux d'un harnais, ni même sur le disque : `record`
écrit **une** ligne `turn_costs` (migration 0014), qui porte à la fois le coût du
tour et le cumul brut dont le tour suivant fera la différence. Insertion et repère
sont donc la même écriture — un tour rejoué après panne ne perd pas son coût.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import math
import sys
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Iterator, Sequence

from . import config as config_mod
from . import context_metrics
from . import harnesses
from . import storage
from .gauge_cache import CLAUDE_GAUGES
from .resident import INPUTS_EVENT as RESIDENT_INPUTS_EVENT
from .resident import USAGE_EVENT as RESIDENT_USAGE_EVENT


def log(message: str) -> None:
    """Journal de la comptabilité : un échec d'écriture ne lève jamais."""
    try:
        sys.stderr.write("[cost] %s\n" % message)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


#: Barème par défaut du poste de travail (reprise de l'outil de comptage
#: existant). USD par million de jetons : [entrée, entrée en cache, sortie].
DEFAULT_PRICES = {
    # #475: DeepSeek's official peak price, read on 2026-10-12 at
    # https://api-docs.deepseek.com/quick_start/pricing (cache miss, cache hit,
    # output). This is the peak price; off-peak hours are in `PRICE_PERIODS`.
    "deepseek-flash": [0.30, 0.006, 1.20],
    "deepseek-pro": [0.55, 0.14, 2.19],
    "gpt-6.1-sol": [1.25, 0.125, 10.0],
    "default-codex": [1.25, 0.125, 10.0],
    "default-deepseek": [0.30, 0.006, 1.20],
    # L225 : barème officiel Anthropic, relevé le 2026-10-11 sur
    # https://platform.claude.com/docs/en/about-claude/pricing (entrée de base,
    # « cache hits and refreshes », sortie ; tarif standard, hors mode rapide,
    # lots et résidence des données). Les modèles retirés encore servis
    # (Opus 4/4.1) restent : ils bornent le majorant de la famille.
    "claude-fable-5-1": [10.0, 0.25, 50.0],
    "claude-mythos-5-1": [10.0, 0.25, 50.0],
    "claude-fable-5": [10.0, 1.0, 50.0],
    "claude-mythos-5": [10.0, 1.0, 50.0],
    "claude-opus-5-5": [4.0, 0.20, 20.0],
    "claude-opus-5": [5.0, 0.50, 25.0],
    "claude-opus-4-8": [5.0, 0.50, 25.0],
    "claude-opus-4-7": [5.0, 0.50, 25.0],
    "claude-opus-4-6": [5.0, 0.50, 25.0],
    "claude-opus-4-5": [5.0, 0.50, 25.0],
    "claude-opus-4-1": [15.0, 1.50, 75.0],
    "claude-opus-4": [15.0, 1.50, 75.0],
    "claude-sonnet-5-5": [2.0, 0.10, 10.0],
    "claude-sonnet-5": [2.0, 0.20, 10.0],
    "claude-sonnet-4-6": [3.0, 0.30, 15.0],
    "claude-sonnet-4-5": [3.0, 0.30, 15.0],
    "claude-sonnet-4": [3.0, 0.30, 15.0],
    # Haiku 5.5 : tarif des invites de plus de 100 000 jetons, le plus cher des
    # deux paliers (majorant) ; chaque requête Claude est facturée à son palier
    # (`PROMPT_TIERS`)
    "claude-haiku-5-5": [0.50, 0.05, 2.50],
    "claude-haiku-4-5": [1.0, 0.10, 5.0],
}

#: L225 : modèles facturés selon la longueur de l'invite de CHAQUE requête
#: (entrée + cache relu + cache écrit) : jusqu'au seuil, le palier bas ;
#: au-delà, le tarif de `DEFAULT_PRICES`. Un usage agrégé sur plusieurs
#: requêtes n'a le palier bas que si son total reste sous le seuil (exact :
#: chaque requête y est alors aussi).
PROMPT_TIERS = {
    "claude-haiku-5-5": (100_000, [0.10, 0.01, 0.50]),
}

#: #513 : heures de pointe DeepSeek, relues le 2026-10-12 sur
#: https://api-docs.deepseek.com/quick_start/pricing : 01:00-04:00 et
#: 06:00-10:00 UTC, du lundi au vendredi ; toute autre heure est creuse,
#: week-ends compris. Les jours fériés chinois, creux eux aussi, ne sont pas
#: connus ici : ils restent au prix de pointe (majorant).
DEEPSEEK_PEAK_UTC = ((1, 4), (6, 10))
#: jours de la semaine des heures de pointe (`datetime.weekday`, lundi = 0)
DEEPSEEK_PEAK_DAYS = (0, 1, 2, 3, 4)

#: #513 : modèles facturés selon l'HEURE de chaque tour. `DEFAULT_PRICES` porte
#: le prix de pointe ; `off_peak`, celui des heures creuses (même page, même
#: date : la moitié du prix de pointe). Un tour entièrement creux paie
#: `off_peak` ; un tour dont une partie est en pointe, ou dont l'heure est
#: inconnue, paie la pointe (majorant). Seulement au tarif officiel : un
#: barème local qui renomme le prix du modèle n'a pas d'heures creuses.
PRICE_PERIODS = {
    "deepseek-flash": {"peak_utc": DEEPSEEK_PEAK_UTC, "peak_days": DEEPSEEK_PEAK_DAYS,
                       "off_peak": [0.15, 0.003, 0.60]},
}

#: Fenêtres nommées par les jauges, en secondes.
KNOWN_WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}

#: Plafond horaire de l'usage payé au jeton, pour l'ensemble des agents
#: (décision 0019 §2 : référence 10 USD/h).
DEFAULT_HOURLY_USD = 10.0

#: L70 : fenêtres glissantes des plafonds (secondes → libellés de la raison)
WINDOW_LABELS = {3600: ("hourly", "the last 60 minutes"),
                 86400: ("daily", "the last 24 hours")}


def gauges_source(harness: str) -> str:
    """La source de jauges déclarée par le descripteur d'un harnais, ou ''."""
    descriptor = harnesses.get(harness)
    return str(descriptor.cost.get("gauges") or "") if descriptor else ""


def paid_harnesses_of(host: str | None = None) -> tuple[str, ...]:
    """Harnais dont l'usage est **payé au token** (0019 §2).

    Lu dans les descripteurs (`ameesh.cost.paid_per_token`), plus dans une liste
    fermée : un harnais nouveau se déclare. Si **aucun** descripteur n'est
    lisible, la garde de budget refuse de deviner (fail-closed) plutôt que de
    considérer tout l'usage comme un forfait.
    """
    descriptors = harnesses.scan(host=host)[0]
    if not descriptors:
        raise CostError("no readable harness descriptor: the pay-per-token hourly cap "
                        "cannot be applied")
    return tuple(sorted(ident for ident, d in descriptors.items() if d.paid_per_token))

#: Marge de rythme : on ne vise pas 100 % d'un forfait, mais la part écoulée + 10.
PACE_MARGIN = 0.10
#: Plafond de rythme par défaut (0019 §2). Un compte peut régler le sien
#: (`accounts.<harnais>[i].pace_ceiling`, #1176) et le relever dans les
#: dernières heures de la fenêtre quand l'overage y est impossible
#: (`pace_ceiling_end`).
PACE_CEILING = 0.90
#: L242b : plancher du rapport réel/estimé qui corrige l'estimation entre deux
#: relevés de solde (`CostBook.paid_hour`) — jamais une dépense comptée nulle
PAID_RATIO_FLOOR = 0.10
#: L242b : le compte d'une pause sans comptes déclarés (comme `auth_blocked`)
DEFAULT_ACCOUNT_LABEL = "(default)"


def until_label(ts: float, now: float | None = None) -> str:
    """L'heure de fin d'une pause : « 14:20 », précédée de la date si ce
    n'est pas aujourd'hui (#373 r2 : une pause de deux jours n'affiche pas
    une heure qui semble à 30 min)."""
    end = time.localtime(float(ts))
    if time.strftime("%Y-%m-%d", end) == time.strftime(
            "%Y-%m-%d", time.localtime(time.time() if now is None else now)):
        return time.strftime("%H:%M", end)
    return time.strftime("%d/%m %H:%M", end)


#: Bornes de lecture des journaux : on ne charge jamais un fichier entier, les
#: sessions des harnais grossissent sans fin.
TAIL_BYTES = 4_000_000
CODEX_FILES = 5

#: marqueur de compte (L30, décision 0027) : l'exécuteur l'écrit dans le flux
#: `events.jsonl` de l'agent juste avant chaque tour, quand des comptes sont
#: déclarés pour le harnais. Les événements qui suivent (dont les
#: `rate_limit_event` de Claude) appartiennent à ce compte. Un flux sans
#: marqueur est celui du compte primaire (hôte d'avant L30).
ACCOUNT_MARKER = "ameesh.account"

#: marqueur de tour (L71) : l'exécuteur l'écrit dans le flux juste avant
#: chaque tour, avec la session qu'il **reprend** (`resume`, vide pour une
#: session neuve). Sans lui, le premier tour d'une session reprise dont le
#: grand livre ne connaît aucun relevé (session adoptée, base neuve) était
#: compté au cumul de toute la session (Claude `total_cost_usd`, Codex
#: `turn.completed`), pas à la différence.
TURN_MARKER = "ameesh.turn"


@dataclass(frozen=True)
class Overage:
    """Paid usage beyond the plan for one account (L226b), as its harness
    last reported it. Never a secret: two flags.

    * `capable`: the account may go beyond its plan and be billed (Claude
      `overageStatus` allowed, Codex `credits.has_credits`); None if the
      harness did not say;
    * `in_use`: the account is consuming paid usage now (Claude
      `isUsingOverage`, Codex a window at 100 % with credits).
    """

    capable: bool | None = None
    in_use: bool = False


class Gauges(list):
    """The gauges of one reading, with the overage flags of the same reading
    (`overage`, None if unknown), and read refusals keyed by harness (`errors`).
    Refused readings contain no gauges; budget guards must honor their error.
    """

    overage: Overage | None = None

    def __init__(self, items=(), overage: Overage | None = None, *,
                 errors: dict[str, str] | None = None):
        super().__init__(items)
        self.overage = overage
        self.errors = dict(errors or {})

    def in_overage(self, now: float) -> bool:
        """Is the account billed beyond its plan at `now`? A reading in
        overage goes stale once the windows that ran out have reset (L71):
        the next window is fresh, and an unused account would otherwise never
        publish the reading that clears the flag. A saturated window with no
        reset date keeps the flag (never overage when in doubt)."""
        if self.overage is None or not self.overage.in_use:
            return False
        spent = self.spent()
        return not spent or any(not g.reset_passed(now) for g in spent)

    def spent(self) -> list:
        """The windows that ran out (all of them if the reading names none)."""
        return [g for g in self if g.used >= 1.0] or list(self)


class CostError(Exception):
    """Erreur de comptabilité (repère illisible, base absente)."""


@dataclass(frozen=True)
class Gauge:
    """Une fenêtre de forfait, telle que le harnais la publie."""

    harness: str
    key: str
    used: float                     #: 0..1 (le harnais publie des pourcents)
    resets_at: float | None         #: epoch, ou None si le harnais ne le dit pas
    window_s: float
    #: #1561 : quand le relevé a été produit (epoch), None si la source ne le
    #: date pas. Un même relevé porte le même âge pour toutes ses fenêtres,
    #: mais `codex_gauges.reconcile` conserve chaque fenêtre depuis sa propre
    #: observation : une fenêtre réconciliée peut donc être plus récente
    #: qu'une autre du même compte (#2843).
    read_at: float | None = None
    #: #1132 : instant de PRODUCTION du relevé (epoch), None quand le flux ne
    #: le date pas (sans marqueur, ou Codex) — l'heure d'insertion n'en tient
    #: pas lieu, un relevé périmé ne doit pas se faire passer pour frais.
    produced_at: float | None = None
    #: #1132 : journal d'origine du relevé (nom de l'agent), '' si inconnu.
    source: str = ""
    #: #1580 (tranche 3) : le modèle auquel ce seau est propre (le `title` du
    #: widget omarchy, ex. « Fable Weekly » → « fable »), '' pour une fenêtre
    #: générale. Un seau propre à un modèle ne compte que pour une session de
    #: ce modèle (`accounts.gauges_of`) ; l'affichage les montre tous.
    scope: str = ""
    #: #1702 (N2) : cette fenêtre vient de l'historique partagé, pas de la
    #: lecture locale. Elle sert à la garde — c'est la mesure la plus fraîche —
    #: mais ne doit jamais être réécrite dans l'historique : chaque hôte
    #: évaluateur réinscrivait sinon le relevé d'un autre avec sa source (échos).
    from_history: bool = False

    def elapsed(self, now: float) -> float:
        """Part écoulée de la fenêtre, 1.0 quand on ne sait pas la dater."""
        if not self.resets_at:
            return 1.0
        return min(max(1.0 - (self.resets_at - now) / self.window_s, 0.0), 1.0)

    def pace_cap(self, now: float, multiplier: float = 1.0,
                 ceiling: float = PACE_CEILING, floor: float = 0.0) -> float:
        """Le plafond de rythme : min(`ceiling`, part écoulée × `multiplier` + 10 points).

        `multiplier` (#1075, bureau 13/10) : multiplicateur de rythme propre au
        compte (`accounts`, `pace_multiplier`, défaut 1.0), posé à 2.0 sur le
        compte principal Codex pour consommer sa réserve de forfait sans
        remise à zéro. Il ne touche que la part écoulée : l'utilisation
        relevée, les remises à zéro et les plafonds fixes ne changent pas.

        `ceiling` (#1176) : le plafond du compte (`accounts`, `pace_ceiling`,
        défaut `PACE_CEILING`), ou son plafond de fin de fenêtre
        (`pace_ceiling_end`) quand le relèvement s'applique. Les autres termes
        — marge, part écoulée, utilisation relevée — sont inchangés.

        `floor` (#3647, décision owner #141793 a) : le rythme relevé d'une
        fenêtre dont la perte prévue au reset dépasse le seuil du canon
        (`accounts.underused_floor`) ; jamais au-dessus de `ceiling`, et les
        plafonds fixes s'appliquent ensuite (`accounts.limits`).
        """
        return min(ceiling, max(self.elapsed(now) * multiplier + PACE_MARGIN, floor))

    def used_at(self, now: float) -> float:
        """L'utilisation **valable à `now`** : 0 si la fenêtre est échue (L71).

        Un relevé dont `resets_at` est passé décrit une fenêtre close ; la
        suivante est vierge. Le garder à sa dernière valeur rendait un compte
        inutilisé « au seuil » pour toujours : il ne servait pas, donc son
        relevé n'était jamais rafraîchi.
        """
        return 0.0 if self.reset_passed(now) else self.used

    def held_cap(self, now: float, reserve: float = 0.0,
                 multiplier: float = 1.0, ceiling: float = PACE_CEILING,
                 floor: float = 0.0) -> float:
        """Le seuil effectif de la fenêtre : le rythme moins `reserve` (L242b),
        jamais sous `PACE_MARGIN` quand une réserve s'applique.

        Exactement `min(PACE_CEILING, multiplier × écoulé + PACE_MARGIN) −
        reserve`, plancher `PACE_MARGIN` :
        * **début de fenêtre** : tant que `multiplier × écoulé ≤ reserve`
          (soit `≤ PACE_MARGIN` quand la réserve vaut la marge), la réserve ne
          s'applique pas et le seuil vaut la marge (10 points), pas
          `multiplier × écoulé + marge − réserve` — une fenêtre à 1 % d'écoulé
          garde donc 10 points, pas 1 ; à 8 % d'écoulé avec `multiplier = 2`
          le seuil vaut 16 %, pas 10 % (#1028 P3). C'est l'énoncé de la
          décision du propriétaire du 2026-10-13 (#777) : « pas de réserve sur
          une fenêtre écoulée à moins de 10 %, plafond = max(écoulé, marge) »,
          exact pour `multiplier = 1` et `reserve = PACE_MARGIN` tant qu'on
          reste sous le plafond de fin de fenêtre ;
        * **fin de fenêtre** : le seuil plafonne à `PACE_CEILING − reserve`
          (80 % avec la réserve de 10 points) et ne suit plus l'écoulé —
          l'énoncé `max(écoulé, marge)` n'y vaut plus (#1028 N1).

        `ceiling` (#1176) est transmis à `pace_cap` : le plafond du compte ou
        son plafond de fin de fenêtre.
        """
        cap = max(0.0, self.pace_cap(now, multiplier, ceiling, floor) - reserve)
        return max(cap, PACE_MARGIN) if reserve else cap

    def exceeded(self, now: float, reserve: float = 0.0,
                 multiplier: float = 1.0, ceiling: float = PACE_CEILING) -> bool:
        """Au seuil effectif ; `reserve` (L242b) l'abaisse d'autant,
        `ceiling` (#1176) est celui du compte."""
        return self.used_at(now) >= self.held_cap(now, reserve, multiplier, ceiling)

    def free_at(self, now: float, reserve: float = 0.0,
                multiplier: float = 1.0, ceiling: float = PACE_CEILING) -> float | None:
        """L'heure où la fenêtre cesse de bloquer, ou None si elle ne la dit
        pas (`resets_at` absent).

        C'est l'heure où la part écoulée rejoint l'usage sous le plafond
        effectif — quand le rythme rattrape l'avance —, pas la remise à zéro
        (remarque de l'auditeur, #777 : « l'heure de pause … until doit être
        celle où l'écoulé dépasse l'usage »). Un usage au-delà du plafond
        (`PACE_CEILING` moins la réserve) n'est jamais rattrapé : seule la
        remise à zéro libère la fenêtre, et c'est elle qu'on annonce.

        La part cherchée est l'inverse exact de `held_cap` :
        `(used + reserve − PACE_MARGIN) / multiplier`. Aucun plancher
        `PACE_MARGIN` ne s'y applique (#1028 N2) : une jauge bloquée a
        `used ≥ held_cap ≥ PACE_MARGIN`, donc la part est déjà positive ; le
        plancher ne changeait rien pour `reserve ≥ PACE_MARGIN` et annonçait
        une libération trop tardive en dessous (le seuil, lui, garde le sien).

        `ceiling` (#1176) est le plafond effectif du compte à `now`.
        """
        if not self.resets_at:
            return None
        used = self.used_at(now)
        if used >= ceiling - reserve:
            return float(self.resets_at)
        share = (used + reserve - PACE_MARGIN) / multiplier
        share = min(max(share, 0.0), 1.0)
        return float(self.resets_at) - self.window_s * (1.0 - share)

    def reset_passed(self, now: float) -> bool:
        """La fenêtre publiée est-elle déjà remise à zéro (`resets_at` passé) ?

        Le relevé est alors périmé : la fenêtre suivante est vierge tant qu'aucun
        tour n'a publié de nouveau relevé (L30, retour au primaire).
        """
        return bool(self.resets_at) and now >= float(self.resets_at)

    def expiring(self, now: float) -> tuple[float, float] | None:
        """(échéance, capacité inutilisée) de la fenêtre en cours, ou None (L74).

        La capacité inutilisée (`1 − utilisé`, part de la fenêtre) est perdue à
        la remise à zéro `resets_at` si rien ne la consomme d'ici là (0034).
        None : fenêtre non datée, ou échue (la suivante ne court qu'au premier
        usage : rien n'expire).
        """
        if not self.resets_at or self.reset_passed(now):
            return None
        return float(self.resets_at), max(0.0, 1.0 - float(self.used or 0.0))


def stale_reading(gauges, now: float) -> bool:
    """Le relevé n'est plus un instantané courant du compte (#1561).

    * **absent** (aucune jauge) : rien ne dit l'état du compte ;
    * **partiel** : une fenêtre datée s'est remise à zéro alors qu'une autre
      court encore — la fenêtre ouverte n'est plus qu'une borne inférieure
      (le compte a pu servir depuis, ici ou ailleurs) ; une fenêtre **non
      datée** ne prouve pas sa remise à zéro, elle compte donc comme encore
      ouverte (#1566 N2) ;
    * **non daté** : l'âge du relevé (`read_at`) dépasse sa plus courte
      fenêtre.

    Un relevé dont **toutes** les fenêtres sont remises à zéro n'est pas
    périmé : la suivante est vierge, 0 % (L71) — ce qui exige que toutes les
    fenêtres soient datées, une fenêtre non datée interdisant de conclure.
    Un relevé sans âge connu n'est pas présumé périmé : une source qui ne
    date pas garde son sens, et le choix la traite comme avant.
    """
    gauges = list(gauges)
    if not gauges:
        return True
    dated = [g for g in gauges if g.resets_at]
    if dated:
        passed = [g.reset_passed(now) for g in dated]
        if not any(passed):
            return False
        # #1566 N2 : `all(passed)` ne portait que sur les fenêtres datées ; une
        # fenêtre non datée encore là rendait le mélange « datée échue + non
        # datée » faussement courant. Elle ne prouve pas sa remise à zéro.
        return not all(passed) or any(not g.resets_at for g in gauges)
    stamps = [stamp for stamp in (getattr(g, "read_at", None) for g in gauges) if stamp]
    if not stamps:
        return False
    return now - min(stamps) > min(g.window_s for g in gauges)


def reading_outdated(gauges, now: float) -> bool:
    """#1672 : le relevé **partiel** est-il trop vieux pour engager un tour ?

    `stale_reading` dit qu'un relevé partiel — une fenêtre datée remise à zéro,
    une autre encore ouverte — n'est plus un instantané courant ; il reste
    néanmoins informatif pour les fenêtres encore ouvertes tant que l'âge de
    **chacune** (`now - read_at` de son observation) ne dépasse pas ce qui lui
    reste (`resets_at - now`). Une fenêtre court échue conservée par
    `codex_gauges.reconcile` ne vieillit donc pas une semaine récemment
    observée, sans relever l'âge d'une semaine réellement ancienne (#2843).
    Au-delà, la remise à zéro qui rendra le relevé caduc est plus proche que le
    trou d'observation : **prudence sur une information ancienne**, pas la
    preuve d'un quota atteint. Le cas réel du 15/10 : relevé de 30 % vieux de
    quatre jours, fenêtre 7 j à 9 h de sa remise à zéro — le choix automatique
    y a envoyé un tour, refusé par la limite hebdomadaire.

    Faux pour un relevé **courant** (aucune fenêtre datée remise à zéro) et
    pour un relevé dont **toutes** les fenêtres datées sont échues (L71, la
    suivante est vierge à 0 %). Faux sans âge connu (`read_at` absent : une
    date manquante ne prouve aucune péremption de plus). Faux dès qu'**une**
    fenêtre encore ouverte reste plus longue que son propre âge : le relevé
    garde du sens pour elle.
    """
    dated = [g for g in gauges if g.resets_at]
    if not any(g.reset_passed(now) for g in dated):
        return False
    opened = [g for g in dated if not g.reset_passed(now)]
    if not opened:
        return False                    # L71 : toutes échues, la suivante est vierge
    for gauge in opened:
        stamp = getattr(gauge, "read_at", None)
        if not stamp:
            return False                # âge inconnu : aucune péremption de plus
        if now - float(stamp) <= float(gauge.resets_at) - now:
            return False                # cette fenêtre reste plus longue que son âge
    return True


@dataclass(frozen=True)
class TurnUsage:
    """Ce qu'un tour a coûté : la matière d'une ligne `turn_costs`."""

    agent: str
    harness: str
    model: str
    usd: float
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    turn: str | None = None
    session: str | None = None
    #: L225 : non None = tour au barème sans aucun barème ; la ligne est
    #: écartée des sommes et l'alerte `turn_unpriced` le dit
    void_reason: str | None = None
    #: L225 : non None = tour facturé au repli prudent (maximum du barème),
    #: faute de tarif du modèle et de sa famille ; alerte `turn_unpriced`
    price_fallback: str | None = None
    #: L225 : écritures de cache Claude du tour (None : harnais sans)
    cache_write_tokens: int | None = None

    def row(self) -> dict:
        return {
            "agent": self.agent,
            "harness": self.harness,
            "turn": self.turn,
            "model": self.model,
            "session": self.session,
            "usd": round(self.usd, 6),
            "input_tokens": int(self.input_tokens),
            "cached_input_tokens": int(self.cached_input_tokens),
            "output_tokens": int(self.output_tokens),
        }

    @property
    def reread_tokens(self) -> int:
        """Jetons d'entrée **relus** par le tour, cache compris (L60).

        Mesure du coût du tour : chaque étape d'un tour relit tout le contexte
        de la session. Ce n'est pas la taille du contexte — le plafond de
        contexte mesure le dernier appel (#2336) — ni la mesure de l'alerte
        `session_too_big`, qui lit `S_hist` de la session (`session_total`,
        #4448). Codex compte déjà l'entrée en cache dans `input_tokens` ; les
        autres harnais la comptent à part."""
        if self.harness == "codex":
            return max(int(self.input_tokens), int(self.cached_input_tokens))
        return int(self.input_tokens) + int(self.cached_input_tokens)


#: CTX-02b : type de l'événement qui publie, par tour, les mesures de contexte
#: étiquetées ``ameesh-context/1`` à côté des compteurs legacy.
CONTEXT_EVENT = "turn.context"

#: Les compteurs legacy publiés **à côté** des mesures normalisées. Tous les
#: champs sont présents, ``None`` quand la mesure manque : le lecteur n'a pas à
#: distinguer une clé absente d'une absence mesurée.
CONTEXT_LEGACY_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reread_tokens",
    "context_tokens",
    "subagent_reread_tokens",
    "session_tokens",
    "session_turns",
    "context_max_tokens",
    "session_max_tokens",
)


def context_publication(document: dict, *, legacy: dict, decision: dict,
                        agent: str, host: str, label: str | None = None,
                        kind: str | None = None,
                        work_item_id: str | None = None,
                        started_ts: float | None = None,
                        ended_ts: float | None = None) -> dict:
    """La charge d'un événement ``turn.context`` (CTX-02b, observation seule).

    Le document ``ameesh-context/1`` de :class:`context_metrics.TurnNormalizer`
    (mesures étiquetées : qualité, raisons, granularité, source, cache,
    sous-agents) est publié **à côté** des compteurs legacy du tour, sans
    changer ni la ligne ``turn_costs`` ni une décision de rotation. ``legacy``
    porte les valeurs que l'exécuteur et le grand livre utilisent déjà ;
    ``decision`` porte la politique en vigueur et les motifs de clôture /
    rotation observés, pour que l'écart legacy/cible soit lisible.

    Lève :class:`CostError` si le document n'est pas un ``ameesh-context/1``
    clos ou s'il manque un champ legacy : un appelant fautif doit le voir,
    l'exécuteur attrape l'erreur pour ne jamais retenir un tour.
    """
    if not isinstance(document, dict) \
            or document.get("schema") != context_metrics.SCHEMA:
        raise CostError("context publication: an %s document is required"
                        % context_metrics.SCHEMA)
    if document.get("finished") is not True:
        raise CostError("context publication: the turn document is not finished")
    measures = document.get("measures")
    if not isinstance(measures, dict) or "C_last" not in measures \
            or "R_turn" not in measures:
        raise CostError("context publication: normalized measures are missing")
    if not isinstance(legacy, dict):
        raise CostError("context publication: legacy counters must be a mapping")
    missing = [name for name in CONTEXT_LEGACY_FIELDS if name not in legacy]
    if missing:
        raise CostError("context publication: legacy counters missing: %s"
                        % ", ".join(missing))
    if not isinstance(decision, dict):
        raise CostError("context publication: decision must be a mapping")
    policy = decision.get("policy")
    if not isinstance(policy, dict):
        raise CostError("context publication: decision.policy must be a mapping")
    for name, value in (("started_ts", started_ts), ("ended_ts", ended_ts)):
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))):
            raise CostError("context publication: %s must be a number" % name)
    turn_id = document.get("turn")
    return {
        "schema": context_metrics.SCHEMA,
        "observation_id": "turn:" + str(turn_id) if turn_id else None,
        "agent": str(agent),
        "host": str(host),
        "turn_id": turn_id or None,
        "turn": label or None,
        "kind": kind or None,
        "started_ts": float(started_ts) if started_ts is not None else None,
        "ended_ts": float(ended_ts) if ended_ts is not None else None,
        "work_item_id": str(work_item_id) if work_item_id else None,
        "session": document.get("session"),
        "harness": document.get("harness"),
        "model": document.get("model"),
        "mode": document.get("mode"),
        "role": document.get("role"),
        "policy": dict(policy),
        "decision": {key: value for key, value in decision.items()
                     if key != "policy"},
        "legacy": {name: legacy.get(name) for name in CONTEXT_LEGACY_FIELDS},
        "context": document,
    }


def session_reread(db, agent: str, session: str) -> int | None:
    """Le cumul ``R_session`` des tours **précédents** d'une session, ou None.

    Lecture seule du grand livre, pour la base ``session_reread`` du
    normalisateur de contexte (CTX-02b) : entrée + cache **lu**, cache écrit
    exclu ; Codex compte déjà le cache dans son entrée. Aucun tour inscrit
    (session neuve, base neuve) ou lecture impossible : None — le normalisateur
    publie alors ``R_session`` inconnu avec sa raison, jamais un zéro supposé.
    Une erreur de base ne retient jamais un tour : la mesure est d'observation.
    """
    if not session:
        return None
    try:
        row = storage.of(db).turn_costs.session_reread(agent, session)
    except Exception:  # noqa: BLE001 — mesure d'observation, jamais fatale
        return None
    if not row or not int(row.get("turns") or 0):
        return None
    return int(row.get("tokens") or 0)


def load_prices(path: str | None = None) -> dict:
    """Le barème : les défauts, corrigés par le fichier JSON s'il existe.

    Un fichier illisible ou mal formé ne doit pas casser la comptabilité : on
    garde les défauts (et l'appelant peut le dire).
    """
    prices = dict(DEFAULT_PRICES)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    triplet = _valid_triplet(value)
                    if triplet is not None:
                        prices[str(key)] = triplet
        except (OSError, ValueError):
            pass
    return prices


def _valid_triplet(value) -> list | None:
    """Un triplet de prix exploitable, ou None.

    Trois nombres **finis et positifs ou nuls**. Un prix négatif, `NaN` ou
    infini ne doit jamais entrer dans le barème : il baisserait une dépense, ou
    rendrait le majorant incalculable (L13 B6, fail-closed).
    """
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        triplet = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(v) or v < 0 for v in triplet):
        return None
    return triplet


#: familles de modèles par harnais (préfixes de clés du barème) : sert au
#: repli fail-closed « tarif le plus cher connu du fournisseur » (0019 §3).
PRICE_FAMILY = {
    "deepseek": ("deepseek-",),
    "codex": ("gpt-", "codex-"),
    "claude": ("claude-",),
}


def foreign_harness_model(harness: str, model: str | None) -> bool:
    """Le modèle nommé appartient-il **positivement** à la famille d'un autre
    harnais ? (#1268)

    Garde de lancement : une ligne de registre héritée d'une bascule de harnais
    (#1037) peut porter `model=claude-…` avec `harness=codex` ; passer ce modèle
    au harnais du tour provoque un 400 côté fournisseur (incident orchestrateur
    du 14/10). On ne refuse jamais sur une supposition : un identifiant dont
    aucune famille n'est reconnue n'est pas étranger.
    """
    nom = str(model or "").strip().lower()
    if not nom:
        return False
    for harnais, prefixes in PRICE_FAMILY.items():
        if harnais == harness:
            continue
        if any(nom.startswith(prefixe) for prefixe in prefixes):
            return True
    return False


#: suffixes qu'un harnais ajoute à l'identifiant d'un modèle sans en changer
#: le tarif : variante de contexte (`claude-opus-4-6[1m]`), instantané daté
#: (`claude-haiku-4-5-20251001`)
_MODEL_SUFFIX = re.compile(r"(\[[^\]]*\]|-\d{8})$")


def priced_name(prices: dict, model: str | None) -> str | None:
    """Le nom sous lequel un modèle est **nommé** au barème, ou None (L225).

    L'identifiant exact d'abord, puis sans ses suffixes de variante ou de date."""
    name = str(model or "")
    while name:
        if name in prices and not name.startswith("default-"):
            return name
        stripped = _MODEL_SUFFIX.sub("", name)
        if stripped == name:
            return None
        name = stripped
    return None


def known_price(prices: dict, model: str | None) -> list | None:
    """Le triplet de prix **nommé** d'un modèle au barème, ou None (L225)."""
    name = priced_name(prices, model)
    return list(prices[name]) if name else None


def prompt_tier(prices: dict, model: str | None) -> tuple | None:
    """L225 : (seuil, triplet du palier bas) d'un modèle facturé selon la
    longueur de l'invite, ou None. Seulement au tarif officiel : un barème
    local qui renomme le prix du modèle n'a pas de palier."""
    name = priced_name(prices, model)
    tier = PROMPT_TIERS.get(name or "")
    if tier is None or list(prices[name]) != DEFAULT_PRICES.get(name):
        return None
    return tier


def is_peak(period: dict, ts: float) -> bool:
    """#513 : l'instant `ts` est-il dans les heures de pointe de `period` ?"""
    moment = _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc)
    return moment.weekday() in period["peak_days"] and any(
        low <= moment.hour < high for low, high in period["peak_utc"])


#: #513 : au-delà, un tour n'est jamais creux (horloge déréglée, marqueur
#: ancien) — la marche heure par heure reste bornée
PERIOD_MAX_S = 2 * 86400


def period_price(prices: dict, model: str | None, started: float | None,
                 ended: float | None) -> list | None:
    """#513 : le prix creux d'un tour de `started` à `ended`, ou None (pointe).

    Creux seulement si le modèle a des heures creuses (`PRICE_PERIODS`) au
    tarif officiel, et si TOUT l'intervalle est hors pointe : les bornes de
    pointe tombent sur des heures pleines UTC, on vérifie donc chaque heure
    commencée. Heure inconnue, intervalle à l'envers ou trop long : None."""
    name = priced_name(prices, model)
    period = PRICE_PERIODS.get(name or "")
    if period is None or list(prices[name]) != DEFAULT_PRICES.get(name):
        return None
    try:
        low, high = float(started), float(ended)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(low) and math.isfinite(high)) or high < low \
            or high - low > PERIOD_MAX_S:
        return None
    moment = low
    while moment <= high:
        if is_peak(period, moment):
            return None
        moment = (math.floor(moment / 3600.0) + 1) * 3600.0
    return None if is_peak(period, high) else list(period["off_peak"])


def price_of(prices: dict, harness: str, model: str | None) -> list:
    """Le triplet de prix d'un modèle, avec le repli par harnais."""
    return known_price(prices, model) or prices.get("default-%s" % harness) \
        or prices["default-deepseek"]


def max_price_of(prices: dict, harness: str) -> list | None:
    """Un **majorant sûr** du tarif d'un modèle inconnu de la famille du harnais.

    Le maximum se prend **composante par composante** — entrée, entrée en cache,
    sortie — sur tous les tarifs connus de la famille. Le triplet obtenu coûte,
    pour n'importe quel mélange de jetons, **au moins autant que n'importe quel
    modèle connu** : un barème où le modèle le plus cher en entrée est le moins
    cher en sortie (`flash [10,1,1]`, `pro [5,1,100]`) donne `[10,1,100]`, jamais
    `flash` seul (B6 : on ne comparait que l'entrée, et un modèle inconnu qui
    produit 1 M de jetons de sortie était facturé 1 $ au lieu de 100 $).

    Un modèle inconnu (ou vide) ne doit jamais être facturé au tarif par défaut,
    qui est le moins cher (0019 §3, repli fail-closed). L225 : si la famille n'a
    **aucun** tarif connu, il n'y a pas de majorant sûr — None. L'appelant
    prend alors le repli prudent (`worst_price_of`) **et** le marque
    (`price_fallback`, alerte `turn_unpriced`) : sans prix claude, ce repli
    facturait un tour opus aux tarifs codex, sans un mot.
    """
    prefixes = PRICE_FAMILY.get(harness, ())
    candidats = [triplet for key, value in prices.items()
                 if not key.startswith("default-")
                 and any(key.startswith(prefixe) for prefixe in prefixes)
                 for triplet in [_valid_triplet(value)] if triplet is not None]
    if not candidats:
        return None
    return [max(triplet[i] for triplet in candidats) for i in range(3)]


def worst_price_of(prices: dict) -> list | None:
    """L225 : le repli prudent, maximum composante par composante de **tout** le
    barème utilisable (défauts compris) ; None s'il n'y a aucun barème."""
    candidats = [triplet for value in prices.values()
                 for triplet in [_valid_triplet(value)] if triplet is not None]
    if not candidats:
        return None
    return [max(triplet[i] for triplet in candidats) for i in range(3)]


def price_for(prices: dict, harness: str, model: str | None) -> tuple[list | None, dict]:
    """L225 : (triplet, marque) du tarif d'un tour compté au barème.

    Tarif du modèle, sinon majorant de sa famille (0019 §3) : marque vide.
    Famille sans tarif (ou harnais sans famille) : repli prudent sur tout le
    barème, marqué `price_fallback` — le tour reste dans la dépense et les
    plafonds. Aucun barème du tout : (None, `void_reason`), la ligne est écartée.
    Les deux marques lèvent l'alerte `turn_unpriced`."""
    triplet = known_price(prices, model) or max_price_of(prices, harness)
    if triplet:
        return triplet, {}
    what = "modèle %s (harnais %s)" % (model or "inconnu", harness or "?")
    worst = worst_price_of(prices)
    if worst:
        return worst, {"price_fallback": "%s : %s sans tarif au barème ni dans sa famille, "
                       "facturé au maximum du barème" % (FALLBACK_REASON, what)}
    return None, {"void_reason": "%s : %s, aucun barème" % (UNPRICED_REASON, what)}


#: L225 : début du `void_reason` d'un tour sans aucun barème, et de la
#: `price_fallback` d'un tour facturé au repli prudent (alerte `turn_unpriced`)
UNPRICED_REASON = "prix inconnu"
FALLBACK_REASON = "repli prudent"

#: L225 : écritures de cache Claude, en multiple du prix d'entrée de base
#: (barème officiel : 5 min ×1,25, 1 h ×2 ; durée inconnue : ×1,25)
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


class CostBook:
    """Les jauges et la comptabilité d'une machine.

    Tous les chemins sont injectables : les tests travaillent sur des journaux
    JSONL factices et ne lisent jamais `~/.codex` ni `~/.claude`.
    """

    def __init__(
        self,
        state_dir: str | None = None,
        prices_path: str | None = None,
        codex_sessions: str | None = None,
        db=None,
        tools: dict | None = None,
        clock: Callable[[], float] = time.time,
        hourly_usd: float = DEFAULT_HOURLY_USD,
        daily_usd: float = 0.0,
        agent_limits: dict | None = None,
        codex_homes: Sequence[str] | None = None,
    ) -> None:
        self.state_dir = state_dir if state_dir is not None else config_mod.load().state_dir
        self.prices_path = prices_path if prices_path is not None else os.environ.get(
            "AMEESH_PRICES") or os.path.expanduser("~/.config/nexlink-agents/prices.json")
        # sessions Codex : argument > AMEESH_CODEX_SESSIONS (surcharge explicite)
        # > $CODEX_HOME/sessions (comme Codex lui-même) > ~/.codex/sessions
        self.codex_sessions = codex_sessions if codex_sessions is not None else (
            os.environ.get("AMEESH_CODEX_SESSIONS") or os.path.join(
                os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"), "sessions"))
        self.db = db
        self.tools = dict(tools or {})
        self.clock = clock
        self.hourly_usd = hourly_usd
        self._registry: dict | None = None
        #: L70 : plafond du mesh par jour glissant (0 = aucun) et plafonds
        #: propres aux agents, `{agent: {3600|86400: usd}}` (`ameesh.budget`)
        self.daily_usd = daily_usd
        self.agent_limits = dict(agent_limits or {})
        #: L95 : dossiers Codex des comptes déclarés (`CODEX_HOME`), où chercher
        #: le journal d'un fil (modèle, total avant le tour) en plus du défaut
        self.codex_homes = [h for h in (codex_homes or []) if h]
        # Workers create a CostBook each poll: reuse verified observations,
        # not account selection or budget decisions, across those instances.
        self._claude_gauge_cache = CLAUDE_GAUGES

    # -- état local ---------------------------------------------------------
    def agent_dir(self, agent: str) -> str:
        return os.path.join(self.state_dir, agent)

    def agents(self) -> list:
        """Les agents qui ont un état local sur cette machine."""
        try:
            names = os.listdir(self.state_dir)
        except OSError:
            return []
        return sorted(n for n in names if os.path.isdir(self.agent_dir(n)))

    def registry_tools(self) -> dict:
        """`{agent: harnais}` lu **une fois** dans le registre (L71), ou `{}`.

        Le fichier d'état `<agent>/tool` n'est plus écrit : sans le registre,
        les jauges Claude ne trouvaient aucun agent Claude (« — » dans
        `accounts list`, `cost report`, `progress`) et `cost report` affichait
        le harnais « ? ». Sans base (ou base illisible), rien : on retombe sur
        l'état local et sur le contenu du flux.
        """
        if self._registry is None:
            self._registry = {}
            if self.db is not None:
                try:
                    self._registry = dict(storage.of(self.db).agents.harnesses())
                except Exception:  # lecture facultative : jamais fatale à la garde
                    self._registry = {}
        return self._registry

    def tool_of(self, agent: str) -> str:
        """Le harnais d'un agent : injecté, sinon le registre (L71), sinon
        l'état local historique (`<agent>/tool`)."""
        if agent in self.tools:
            return self.tools[agent]
        known = self.registry_tools().get(agent)
        if known:
            return known
        try:
            with open(os.path.join(self.agent_dir(agent), "tool"), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _claude_candidate(self, agent: str) -> bool:
        """Cet agent peut-il porter des relevés Claude ? (L71)

        Un harnais connu et autre que `claude` : non. Un harnais **inconnu**
        (exécuteur sans base, agent absent du registre) : oui — seul Claude
        écrit des `rate_limit_event`, que le lecteur reconnaît à leur type ;
        avant L71, un exécuteur ne voyait que son propre flux et la garde
        inter-agents ignorait les relevés des autres agents Claude.
        """
        harness = self.tool_of(agent)
        return harness in ("", "claude")

    def model_of(self, agent: str) -> str:
        try:
            with open(os.path.join(self.agent_dir(agent), "model"), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def events(self, agent: str, start: int = 0, tail: bool = False,
               end: int | None = None) -> list:
        """Les événements JSONL d'un agent, de la ligne `start` (incluse) à la
        ligne `end` (exclue ; None = fin du flux).

        #2096 : le flux est lu **en flux**, jamais chargé entier. Un journal de
        200 Mo rendait une liste de tous ses événements (pic de plusieurs
        centaines de Mo par lecture, et plusieurs lectures par tour) alors
        qu'un tour n'en veut que la tranche ; les lignes hors de la tranche
        sont comptées en C, jamais analysées, et la lecture reprend à l'octet
        de `start`. Avec `tail`, seule la fin du fichier est lue et les index
        partent du début de cette fin (inchangé)."""
        path = os.path.join(self.agent_dir(agent), "events.jsonl")
        if tail:
            return _parse_events(_read_tail(path).splitlines(), start, end)
        try:
            with open(path, "rb") as flux:
                offset = _byte_offset_of_line(flux, start)
                if offset is None:
                    return []
                fin = None if end is None else _byte_offset_of_line(flux, end)
                if fin is not None and fin <= offset:
                    return []  # tranche vide (end <= start)
                flux.seek(offset)
                brut = flux.read() if fin is None else flux.read(fin - offset)
        except OSError:
            return []
        # la tranche lue est exactement [start, end) : tout ce qu'elle contient
        # est dans la tranche, et un octet invalide écarte sa ligne (JSON
        # illisible) au lieu de faire échouer toute la lecture. Coupée sur
        # "\n" seul, comme le compte de l'exécuteur (`events_start`) : un
        # `splitlines()` couperait aussi sur U+2028 ou U+0085, qu'un harnais
        # écrit tels quels dans une chaîne JSON, et perdrait l'événement.
        texte = brut.decode("utf-8", "replace")
        return _parse_events(texte.split("\n"), 0)

    # -- jauges de forfait --------------------------------------------------
    def claude_gauges(self, account: str | None = None,
                      primary: str | None = None) -> "Gauges":
        """Read the latest Claude rate-limit gauges, shared across an account.

        With `account` (L30), use the marker preceding each turn, including
        markers outside the read tail. Only a stream with no account marker
        belongs to `primary` — the harness's default account (`accounts.
        primary_of`), and only when one is declared: `primary=None` means no
        account claims them (L1078), never "the account itself". Leading
        readings before any established marker in a marked stream stay
        unknown, never primary.

        #1132 : une mesure est classée par son instant de **production** (le ts
        du marqueur du tour qui l'a publiée), jamais par le mtime du fichier —
        un flux sans marqueur est non daté et ne remplace pas une mesure datée.

        #1661 : TOUS les chemins rendent un `Gauges` (liste + `overage`),
        jamais une liste nue : un appelant qui lit `.overage` en direct ne
        doit pas tomber sur le chemin vide. Le chemin par compte rend une
        copie (le relevé est mis en cache et partagé entre CostBook).

        #1702 (N4) : la lecture sans compte récupère elle aussi le marqueur de
        compte hors de la tranche lue, comme le chemin par compte — sans cela
        une mesure dont le marqueur précédait la tranche restait non datée et
        perdait contre toute mesure datée d'un autre flux.
        """
        if account is not None:
            return self._claude_gauges_of(account, primary)
        newest = None
        for agent in self.agents():
            if not self._claude_candidate(agent):
                continue
            path = os.path.join(self.agent_dir(agent), "events.jsonl")
            try:
                # #1702 (N4) : même récupération du marqueur hors tail que le
                # chemin par compte ; la mtime ne sert qu'à départager deux
                # mesures non datées, jamais à en dater une.
                events, stat, _complete = _claude_account_tail(path)
            except OSError:
                continue
            found = _last_claude_reading(events, agent, stat.st_mtime)
            if found is None:
                continue
            if newest is None or _reading_rank(found) >= _reading_rank(newest):
                newest = found
        return newest[2] if newest else Gauges()

    def _claude_gauges_of(self, account: str, primary: str | None) -> "Gauges":
        """Read the newest gauges for one Claude account (L30).

        Compare turn-marker timestamps across agents. Only a stream without
        any marker uses the `primary` account (the harness's default one,
        L1078), dated by the event itself or left undated, never by the file
        modification time (#1720); with `primary` None, no account claims
        them.

        #1132 : la comparaison porte sur l'instant de production, puis la
        lecture est complétée par l'historique partagé (`_gauges_with_history`)
        — un exécuteur qui ne voit que des flux figés ne juge plus un compte
        sur la mesure périmée quand un autre état a publié plus frais.

        #1661 : rend un `Gauges` (copie du relevé caché, `overage` compris),
        jamais une liste nue — même quand aucun relevé n'existe.
        """
        best = None
        for agent in self.agents():
            if not self._claude_candidate(agent):
                continue
            path = os.path.abspath(os.path.join(self.agent_dir(agent), "events.jsonl"))
            try:
                current = os.stat(path)
                cached = self._claude_gauge_cache.get(path)
                # Recheck the age on a hit too: after a clock step back, a
                # settled journal can be racy again and its signature blind.
                if (cached is not None and cached[0] == _journal_signature(current)
                        and not _racy(current, time.time_ns())):
                    readings = cached[1]
                else:
                    # Age measured before the read: slow processing must not
                    # make a racy snapshot old enough to be cached.
                    seen = time.time_ns()
                    events, stat, complete = _claude_account_tail(path)
                    readings = _claude_account_readings(events, agent)
                    if complete and not _racy(stat, seen):
                        self._claude_gauge_cache[path] = (_journal_signature(stat), readings)
                    else:
                        self._claude_gauge_cache.pop(path, None)
            except OSError:
                self._claude_gauge_cache.pop(path, None)
                continue
            found = readings.get(account)
            if found is None and primary is not None and account == primary:
                found = readings.get(None)  # fully verified, unmarked legacy stream
            if found and (best is None or _reading_rank(found) >= _reading_rank(best)):
                best = found
        # A caller must not mutate the cached list seen by another CostBook;
        # the copy keeps the reading's overage flags (L226b, #1649).
        # No reading: an empty Gauges (overage None), the same type as a
        # reading, so a direct `.overage` read never meets a bare list (N1 impl-04).
        # #1661 : le relevé caché est immuable et partagé entre CostBooks ; on
        # en rend une copie fraîche, en gardant le type `Gauges` et son overage.
        # #1132 : la lecture locale (overage compris) est complétée par
        # l'historique partagé ; le type `Gauges` est conservé sur ce chemin
        # aussi — #1132 rendait une liste nue, l'historique ne porte pas les
        # drapeaux d'overage.
        reading = Gauges(best[2], best[2].overage) if best else Gauges()
        return self._gauges_with_history(account, reading)

    def _gauges_with_history(self, account: str, gauges: "Gauges") -> "Gauges":
        """#1132 : complète la lecture locale par l'historique partagé (0027/0434).

        Un exécuteur ne voit que SON dossier d'état : la mesure la plus récente
        y est vite périmée alors qu'un autre état (ou hôte) a publié plus frais
        pour le même compte. Fenêtre par fenêtre, l'historique l'emporte quand
        son instant de production est STRICTEMENT plus récent que celui de la
        mesure locale ; une mesure non datée ne remplace jamais une mesure
        datée. Lecture seule, jamais fatale : sans base, en panne, ou sans
        relevé daté, la lecture locale est rendue.

        L'historique ne porte pas les drapeaux d'overage : la copie rendue
        garde ceux de la lecture locale, avec le type `Gauges` (#1649).

        #1702 (N2) : une fenêtre reprise d'ici porte `from_history=True` ; la
        garde la lit comme la mesure la plus fraîche, mais `record_gauges` ne
        la réécrit jamais — chaque hôte évaluateur réinscrivait sinon le relevé
        d'un autre sous sa source (échos toutes les dix minutes).

        Sur un exécuteur `backend=mediated`, `operations.gauge_latest` n'est
        pas admise au contrat médié : l'appel est refusé, l'exception est
        avalée et la lecture locale est rendue sans réconciliation. C'est
        documenté dans `docs/COMPTES.md` ; l'admettre toucherait le contrat
        d'autorisation (deux avis), ce n'est pas le geste de ce lot.
        """
        if self.db is None:
            return gauges
        try:
            rows = self._gauge_latest("claude", account)
        except Exception:  # historique facultatif : jamais fatal à la garde
            return gauges
        known = {row.get("key"): row for row in rows if row.get("produced_ts") is not None}
        if not known:
            return gauges
        merged, changed = [], False
        for gauge in gauges:
            row = known.get(gauge.key)
            if row is not None and (gauge.produced_at is None
                                    or float(row["produced_ts"]) > float(gauge.produced_at)):
                produced = float(row["produced_ts"])
                merged.append(Gauge("claude", gauge.key, float(row["used"]),
                                    row.get("resets_at_ts"), float(row["window_s"]),
                                    read_at=produced, produced_at=produced,
                                    source=row.get("source") or "", from_history=True))
                changed = True
            else:
                merged.append(gauge)
        if not gauges:
            for key in sorted(known):
                row, produced = known[key], float(known[key]["produced_ts"])
                merged.append(Gauge("claude", key, float(row["used"]),
                                    row.get("resets_at_ts"), float(row["window_s"]),
                                    read_at=produced, produced_at=produced,
                                    source=row.get("source") or "", from_history=True))
                changed = True
        if not changed:
            return gauges
        return Gauges(merged, gauges.overage)

    def _gauge_latest(self, harness: str, account: str) -> list:
        """Le dernier relevé connu de chaque jauge d'un compte (#1132)."""
        return storage.of(self.db).operations.gauge_latest(harness=harness, account=account)

    def codex_gauges(self, sessions: str | None = None, *, plan: str | None = None) -> list:
        """Les fenêtres `rate_limits` des journaux récents du compte.

        #2839 : une lecture partielle ne masque aucune fenêtre ; des relevés
        vivants contradictoires refusent le compte au lieu de choisir au mtime.
        #3034 : `plan` est le forfait du compte vivant (`auth.json`) ; quand il
        est connu et l'identité unique, les relevés d'un autre forfait sont
        ignorés au lieu de rendre le compte ambigu.
        #3122 : la date du plus récent journal non lu (écarté du scan borné,
        # préfixe tronqué, lecture en échec) borne ce qu'il a pu observer ;
        `reconcile` ne prouve le repos #3044 que si elle est plus vieille
        qu'une fenêtre courte.

        Lecture **JSON structurée**, pas une expression régulière : l'ordre des clés
        n'est pas garanti (`plan_type` peut précéder `primary`), `secondary` peut
        être `null`, et une regex y perdait la jauge — donc la pause (sonde codex2).

        `sessions` (L30) : le dossier de sessions d'un compte
        (`<CODEX_HOME>/sessions`) ; par défaut celui de la machine.
        """
        racine = sessions if sessions is not None else self.codex_sessions
        from .codex_gauges import recent_files, reconcile, session_reading

        files, truncated, unread_before = recent_files(racine)

        def unread(stamp):
            nonlocal unread_before
            if unread_before is None or stamp > unread_before:
                unread_before = stamp

        now = self.clock()
        readings = []
        for read_at, path in files:
            try:
                sample = session_reading(path, read_at)
            except OSError:
                # Journal sauté : sa date reste une borne de ce qu'il a pu
                # observer (#3122).
                truncated = True
                unread(read_at)
                continue
            readings.extend(sample)
            if sample.truncated:
                truncated = True
                unread(read_at)
        return reconcile(readings, now, truncated=truncated, plan=plan,
                         unread_before=unread_before)

    def gauges(self, harness: str | None = None, *, record: bool = True) -> Gauges:
        """Les jauges de forfait, par **source déclarée** dans les descripteurs (L16).

        `ameesh.cost.gauges` nomme le lecteur (`transcript` pour Claude,
        `sessions` pour Codex) : le code ne connaît que des formats de journaux,
        un harnais nouveau se décrit. Un harnais sans source de jauges n'en a
        pas (le solde des fournisseurs payés au token est lu ailleurs, L26).

        `record=False` (L61) : lecture seule, rien n'est écrit dans
        l'historique — les commandes de lecture (`progress`, `cost gauges`
        sans `--record`) ; l'exécuteur, lui, relève avant chaque tour.
        """
        from .codex_gauges import AmbiguousCodexGauges

        out = Gauges()
        for ident, descriptor in harnesses.scan()[0].items():
            if harness not in (None, ident):
                continue
            lecteur = {"transcript": self.claude_gauges,
                       "sessions": self.codex_gauges}.get(
                           str(descriptor.cost.get("gauges") or ""))
            if lecteur is not None:
                try:
                    out += [replace(gauge, harness=ident) for gauge in lecteur()]
                except AmbiguousCodexGauges as exc:
                    # The default account has no accounts.evaluate boundary.
                    # Keep the refusal for its budget guard and reports.
                    out.errors[ident] = str(exc)
        if record:
            self.record_gauges(out)
        return out

    def record_gauges(self, gauges: Sequence[Gauge], account: str | None = None) -> int:
        """Historique des jauges (L26, migration 0027), là où elles sont lues.

        Une ligne seulement quand la jauge change (ou toutes les dix minutes) ;
        sans base, rien. **Ne lève jamais** : l'historique ne doit pas casser
        la garde de budget qui lit les jauges avant chaque tour.

        `account` (L30, migration 0028) : les jauges sont celles de ce compte.

        #1702 (N2) : une fenêtre reprise de l'historique (`from_history`) n'y
        est jamais réécrite — elle y est déjà, et la réinscrire sous la source
        de l'hôte évaluateur fabriquait des échos toutes les dix minutes.

        #3755 : les deux drapeaux d'overage du relevé sont publiés avec les
        jauges (`overage_capable`, `overage_in_use`) : la passe d'alertes d'un
        autre hôte peut alors lever `account_overage` pour ce compte.
        """
        if self.db is None or not gauges:
            return 0
        try:
            overage = getattr(gauges, "overage", None)
            capable = None if overage is None else overage.capable
            in_use = None if overage is None else bool(overage.in_use)
            lignes = []
            for g in gauges:
                if getattr(g, "from_history", False):
                    continue
                ligne = {"harness": g.harness, "key": g.key, "used": g.used,
                         "resets_at": g.resets_at, "window_s": g.window_s,
                         "produced_at": getattr(g, "produced_at", None),
                         "source": getattr(g, "source", ""),
                         "overage_capable": capable, "overage_in_use": in_use}
                if account is not None:
                    ligne["account"] = account
                lignes.append(ligne)
            if not lignes:
                return 0
            return storage.of(self.db).operations.record_gauges(lignes)
        except Exception:  # historique facultatif : jamais fatal
            return 0

    def pace_exceeded(self, harness: str, reserve: float = 0.0) -> str:
        """Le forfait d'un harnais avance-t-il trop vite ? La raison, ou ''.

        Le seuil est celui de la décision 0019 §2 :
        `utilisation >= min(90 %, part écoulée de la fenêtre + 10 points)`,
        moins `reserve` pour un agent non critique (L242b (2), `held_cap`).

        Sans compte déclaré : c'est le rythme **nominal** (`multiplier = 1.0`)
        qui s'applique. Un harnais à comptes déclarés passe par
        `accounts.choose`/`accounts.evaluate`, qui portent le
        `pace_multiplier` de son profil (#1075, #1028).
        """
        now = self.clock()
        gauges = self.gauges(harness)
        errors = getattr(gauges, "errors", {})
        if errors:
            return "pause (account %s): %s" % (
                DEFAULT_ACCOUNT_LABEL, "; ".join(errors.values()))
        held = [gauge for gauge in gauges if gauge.exceeded(now, reserve)]
        if not held:
            return ""
        # L242b (4), #373 r1 : la pause en court d'abord, comme avec des
        # comptes déclarés (`accounts.pause_label`) ; sans compte déclaré,
        # c'est le compte par défaut du harnais. #373 r2 : la pause dure
        # jusqu'à la libération la plus tardive des fenêtres qui bloquent —
        # aucune date si l'une d'elles n'est pas datée. #777, #898 : la
        # libération est l'heure où le rythme rattrape l'usage (`free_at`),
        # pas la remise à zéro, sauf pour un usage que le rythme ne rattrape
        # plus.
        head = ""
        if all(gauge.resets_at for gauge in held):
            free, gauge = max(((float(g.free_at(now, reserve)), g) for g in held),
                              key=lambda pair: pair[0])
            head = "pause (account %s, %s %.0f %%) until %s; " % (
                DEFAULT_ACCOUNT_LABEL, gauge.key, gauge.used * 100,
                until_label(free, now))
        else:
            gauge = held[0]
        return head + ("plan %s %s: %.0f%% used with %.0f%% of the window "
                       "elapsed (pace cap %.0f%%%s)" % (
                           harness, gauge.key, gauge.used * 100,
                           gauge.elapsed(now) * 100,
                           gauge.held_cap(now, reserve) * 100,
                           ", %.0f points kept for critical agents" % (reserve * 100)
                           if reserve else ""))

    def default_account_error(self, harness: str) -> str:
        """Hard refusal of a default reading, independent of the pace guard."""
        if gauges_source(harness) != "sessions":
            return ""
        errors = getattr(self.gauges(harness, record=False), "errors", {})
        return ("pause (account %s): %s" % (
            DEFAULT_ACCOUNT_LABEL, "; ".join(errors.values()))) if errors else ""

    # -- comptabilité par tour ---------------------------------------------
    def baseline(self, agent: str, harness: str, session: str | None = None) -> dict:
        """Le dernier relevé **connu** du tour précédent, lu sur le grand livre.

        Il n'est pas tenu dans un fichier : un repère écrit avant l'insertion
        avancerait alors qu'aucune ligne n'existe, et le tour rejoué après panne
        compterait zéro — le coût serait perdu en silence (sonde codex2, B1).

        Et c'est le dernier relevé **connu**, pas la dernière ligne : un tour sans
        résultat (erreur du harnais, tour interrompu) écrit une ligne légitime mais
        sans cumul, et la prendre pour repère ferait compter le tour suivant en
        entier au lieu de sa différence (sonde codex2, B4). Un relevé de la
        **session courante** est préféré quand il existe : deux sessions qui
        s'entrelacent ne doivent pas se prendre l'une l'autre pour repère.
        """
        if self.db is None:
            return {}
        ledger = storage.of(self.db).turn_costs
        row = ledger.last_reading(agent, harness)
        if session and row is not None and row["session"] != session:
            own = ledger.last_reading(agent, harness, session)
            if own is not None:
                row = own
        if row is None:
            return {}
        return {
            "session": row["session"],
            "cum_usd": float(row["cum_usd"]) if row["cum_usd"] is not None else None,
            "cum_usage": [int(row["cum_input_tokens"] or 0),
                          int(row["cum_cached_input_tokens"] or 0),
                          int(row["cum_output_tokens"] or 0)] if row["cum_input_tokens"] is not None else None,
        }


    def session_reading(self, agent: str, harness: str, session: str,
                        start: int) -> dict:
        """Le dernier total **connu** d'une session, hors relevés de l'agent (L71).

        D'abord le grand livre, tous agents confondus (session reprise sous un
        autre nom) ; sinon le flux local de l'agent **avant** le tour (relevés
        d'avant une base neuve). `{}` si rien n'est connu.
        """
        if self.db is not None:
            try:
                row = storage.of(self.db).turn_costs.last_reading(None, harness, session)
            except Exception:
                row = None
            if row is not None:
                return {
                    "session": row["session"],
                    "cum_usd": float(row["cum_usd"]) if row["cum_usd"] is not None else None,
                    "cum_usage": [int(row["cum_input_tokens"] or 0),
                                  int(row["cum_cached_input_tokens"] or 0),
                                  int(row["cum_output_tokens"] or 0)]
                    if row["cum_input_tokens"] is not None else None,
                }
        # #2096 : le préfixe seul est demandé — inutile de lire (et d'analyser)
        # les événements du tour qui suit, encore moins de charger le journal.
        before = self.events(agent, end=max(0, start)) if start else []
        current = ""
        found: dict = {}
        for event in before:
            if event.get("parent_tool_use_id"):
                continue             # L221 : ni la session ni le cumul d'un sous-agent
            kind = event.get("type")
            if kind == RESIDENT_USAGE_EVENT:
                ident = event.get("session")
            else:
                ident = event.get("session_id") or event.get("thread_id")
            if isinstance(ident, str) and ident:
                current = ident
            if current != session:
                continue
            if harness == "claude" and kind == "result" \
                    and isinstance(event.get("total_cost_usd"), (int, float)):
                found = {"session": session, "cum_usd": float(event["total_cost_usd"]),
                         "cum_usage": None}
            elif harness == "codex" and kind == "turn.completed":
                u = event.get("usage") or {}
                found = {"session": session, "cum_usd": None,
                         "cum_usage": [int(u.get("input_tokens", 0)),
                                       int(u.get("cached_input_tokens", 0)),
                                       int(u.get("output_tokens", 0))]}
            elif kind == RESIDENT_USAGE_EVENT and harness != "claude":
                # L80a : le dernier repère publié par un tour résident de la
                # même session (base neuve, ou session reprise sous un autre
                # agent) — le cumul USD d'ACP, celui du fil Codex.
                total = _amount(event.get("cost_total"))
                totals = event.get("usage_total")
                if total is not None:
                    found = {"session": session, "cum_usd": total, "cum_usage": None}
                elif isinstance(totals, dict) and totals:
                    found = {"session": session, "cum_usd": None,
                             "cum_usage": [_num(totals, "input_tokens"),
                                           _num(totals, "cached_input_tokens"),
                                           _num(totals, "output_tokens")]}
        return found

    def resumed_session(self, agent: str, start: int) -> str | None:
        """La session que le tour commençant à `start` **reprend** (L71).

        Lue dans le dernier marqueur de tour avant `start` ; `""` pour une
        session neuve, None sans marqueur (flux d'avant L71 : comportement
        historique, le premier relevé d'une session vaut le tour).
        """
        marker = self.turn_marker(agent, start)
        return None if marker is None else str(marker.get("resume") or "")

    def turn_started(self, agent: str, start: int) -> float | None:
        """#513 : l'heure de début du tour, lue dans son marqueur, ou None."""
        marker = self.turn_marker(agent, start) or {}
        value = marker.get("ts")
        return float(value) if isinstance(value, (int, float)) \
            and not isinstance(value, bool) else None

    def turn_marker(self, agent: str, start: int) -> dict | None:
        """Le dernier marqueur de tour (L71) avant la ligne `start`, ou None."""
        path = os.path.join(self.agent_dir(agent), "events.jsonl")
        found = None
        try:
            with open(path, encoding="utf-8") as fh:
                for index, line in enumerate(fh):
                    if index >= start:
                        break
                    if TURN_MARKER not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == TURN_MARKER:
                        found = event
        except OSError:
            return None
        return found

    def codex_thread_total_before_turn(self, thread: str) -> list | None:
        """L'usage cumulé d'un fil Codex **avant** son dernier tour (L71).

        Lu dans le journal de session du fil (`rollout-…-<fil>.jsonl`) : le
        dernier `token_count` qui précède le dernier `task_started`. None si le
        journal est introuvable ou muet (le tour est alors compté au cumul,
        fail-closed).
        """
        path = self.codex_rollout(thread)
        if not path:
            return None
        last_total = None
        before_turn = None
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if "task_started" not in line and "token_count" not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    payload = event.get("payload") if isinstance(event, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "task_started":
                        before_turn = last_total
                    elif payload.get("type") == "token_count":
                        total = (payload.get("info") or {}).get("total_token_usage")
                        if isinstance(total, dict):
                            last_total = [int(total.get("input_tokens", 0)),
                                          int(total.get("cached_input_tokens", 0)),
                                          int(total.get("output_tokens", 0))]
        except (OSError, ValueError, TypeError):
            return None
        return before_turn

    def codex_last_call_usage(self, thread: str | None) -> dict | None:
        """L'usage du dernier appel au modèle d'un fil Codex (#2932).

        `codex exec --json` ne publie qu'un usage de fin de tour, cumulé ; le
        contexte d'un appel (la taille de la session, #2336) se lit dans le
        journal de session du fil : `info.last_token_usage` du dernier
        `token_count`, lu depuis la fin. None si le journal est introuvable ou
        n'en porte aucun.
        """
        path = self.codex_rollout(thread)
        if not path:
            return None
        for ligne in _lines_reverse(path):
            if b"last_token_usage" not in ligne:
                continue
            try:
                event = json.loads(ligne)
            except ValueError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
            if isinstance(usage, dict):
                return usage
        return None

    def codex_session_roots(self) -> list:
        """Les dossiers de sessions Codex de la machine : le défaut, puis ceux
        des comptes déclarés (L95), sans doublon."""
        out: list = []
        for root in [self.codex_sessions] + [os.path.join(h, "sessions")
                                             for h in self.codex_homes]:
            root = os.path.abspath(os.path.expanduser(root))
            if root not in out:
                out.append(root)
        return out

    def codex_rollout(self, thread: str | None) -> str | None:
        """Le journal de session d'un fil Codex (`rollout-…-<fil>.jsonl`), le
        plus récent de tous les dossiers de sessions connus, ou None."""
        if not thread or not re.fullmatch(r"[A-Za-z0-9._-]+", thread):
            return None
        paths: list = []
        for root in self.codex_session_roots():
            paths += glob.glob(os.path.join(root, "**", "*%s.jsonl" % thread), recursive=True)
        if not paths:
            return None
        return max(paths, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)

    def codex_thread_model(self, thread: str | None) -> str:
        """Le modèle d'un fil Codex (L95), ou ''.

        `codex exec --json` n'annonce pas son modèle : il se lit dans le
        journal de session du fil (dernier `turn_context.model`, ou
        `thread_settings_applied`) ; à défaut, dans le `model` de la
        configuration (`config.toml`) du dossier Codex où vit le fil, sinon de
        celui de la machine. Lecture seule ; '' si rien n'est lisible (le tour
        reste « inconnu », facturé au plus cher connu).
        """
        path = self.codex_rollout(thread)
        found = ""
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        if '"model"' not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        found = _codex_event_model(event) or found
            except OSError:
                pass
        if found:
            return found
        homes = []
        if path:
            # <home>/sessions/AAAA/MM/JJ/rollout-….jsonl
            homes.append(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.dirname(path))))))
        homes += [os.path.dirname(root) for root in self.codex_session_roots()]
        for home in homes:
            model = _codex_config_model(os.path.join(home, "config.toml"))
            if model:
                return model
        return ""

    def session_of(self, agent: str) -> str | None:
        """La session (ou le fil) courante d'un agent, lue sur **tout** son flux.

        Pas sur la tranche du dernier tour : un `turn.completed` peut arriver sans
        nouveau `thread.started`, et la session se perdrait alors sur la ligne —
        le relevé suivant, comparé au mauvais repère, compterait un tour de trop
        (sonde codex2, B5). La session se résout donc en amont et se transmet.

        #2096 : lue **de la fin vers le début**, elle n'a plus à charger ni à
        analyser le journal entier à chaque tour ; la dernière session est
        presque toujours dans les derniers blocs. Le parcours reste complet
        quand il faut (flux sans session, ou que des sous-agents en fin de
        flux), et rend la même session que le parcours direct.
        """
        path = os.path.join(self.agent_dir(agent), "events.jsonl")
        for ligne in _lines_reverse(path):
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                event = json.loads(ligne)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            trouve = _session_event(event)
            if trouve:
                return trouve
        return None

    def turn_usage(self, agent: str, start: int = 0, session: str | None = None,
                   model: str | None = None, ended: float | None = None) -> TurnUsage:
        """Ce que le tour qui vient de finir a coûté, sans rien écrire.

        Le repère de cumul (dernier `total_cost_usd` Claude, dernier usage Codex)
        est lu dans le grand livre : la différence se calcule donc entre deux
        tours, pas entre deux lectures d'un même tour — et un tour sans relevé ne
        l'efface pas.

        #513 : un modèle à heures creuses (`PRICE_PERIODS`) est facturé à
        l'heure du tour, de son marqueur à `ended` (défaut : maintenant ; un
        compte rejoué plus tard n'en est que plus souvent en pointe).
        """
        harness = self.tool_of(agent)
        # Le modèle **effectif du lancement** est passé par l'exécuteur ; le
        # relire ici prendrait un fichier d'état mutable (verdicts codex3 L13 B4).
        # `None` = paramètre absent (repli historique sur l'état local) ;
        # `""` = aucun modèle annoncé/figé : on facture le **plus cher connu** de
        # la famille, jamais le défaut (mesh-design, 0019 §3, fail-closed).
        if model is None:
            model = self.model_of(agent)
        events = self.events(agent, start)
        # La session se résout AVANT le repère : c'est elle qui dit lequel comparer.
        session = session or self.session_of(agent)
        if not model and harness == "codex":
            # L95 : Codex n'annonce pas son modèle dans `exec --json` ; il se
            # lit dans le journal de session du fil, ou sa configuration.
            model = self.codex_thread_model(session)
        prices = load_prices(self.prices_path)
        modele = model if known_price(prices, model) else (model or "inconnu")
        # L225 : la marque (`price_fallback` ou `void_reason`) ne vaut que pour
        # un tour effectivement compté au barème, avec des jetons.
        triplet, marque = price_for(prices, harness or "deepseek", model)
        if triplet and not marque:
            triplet = period_price(prices, model, self.turn_started(agent, start),
                                   time.time() if ended is None else ended) or triplet
        pin, pcache, pout = triplet or (0.0, 0.0, 0.0)
        tier = prompt_tier(prices, model) if not marque else None
        # L80a : un tour résident Codex/ACP porte sa mesure dans un événement
        # `resident.turn`, écrit depuis son TurnResult (le flux brut est du
        # JSON-RPC). Il fait foi seul pour ce tour : les relevés natifs de la
        # même tranche ne sont pas recomptés.
        resident = _resident_event(events)
        cum = self.baseline(agent, harness, session)
        # L71 : session reprise sans relevé de l'agent — le dernier total CONNU
        # de la session (autre agent, ou flux local d'avant le tour) sert de repère.
        if (harness in ("claude", "codex") or resident is not None) \
                and session and cum.get("session") != session:
            known = self.session_reading(agent, harness, session, start)
            if known:
                cum = known
        # Reprise d'une session dont aucun total n'est connu : le cumul du
        # harnais porte toute l'histoire de la session, pas ce tour.
        unknown_resume = (bool(session) and cum.get("session") != session
                          and self.resumed_session(agent, start) == session)

        if resident is not None and harness != "claude":
            return _resident_turn_usage(agent, harness, modele, resident,
                                        pin, pcache, pout, marque,
                                        cum=cum, session=session,
                                        unknown_resume=unknown_resume)

        if harness == "claude":
            # L221 : seuls les `result` du fil principal (pas d'un sous-agent,
            # session du tour) disent le coût et le cumul du tour.
            results = _claude_main_results(events, session)
            totals = [float(e["total_cost_usd"]) for e in results
                      if isinstance(e.get("total_cost_usd"), (int, float))]
            usage = [e["usage"] for e in results if _has_tokens(e.get("usage"))]
            last = cum.get("cum_usd")
            au_bareme = True
            # L221 : c'est le `result` principal **final** qui décide. Sans usage,
            # il ne dit rien des jetons du tour ; le garder (`[{}]`, vrai pour
            # Python) sautait le repli L105 et faisait du cumul du processus le
            # coût d'un tour à 0 jeton (tour 2260).
            if not results or not _has_tokens(results[-1].get("usage")):
                # L105 : tour clos avant son `result` (plafond du tour, arrêt,
                # préemption), ou `result` sans usage (L221) — l'usage de chaque
                # appel, lu sur les messages du modèle (une fois par id), au
                # barème ; le cumul n'en fait jamais le coût du tour.
                usage = _claude_call_usages(events)
                usd = sum(_claude_usd(u, pin, pcache, pout, tier) for u in usage)
            elif not totals:
                usd = 0.0
                au_bareme = False
            elif unknown_resume:
                # L71 : total d'avant le tour inconnu — estimation par les
                # jetons du tour (`usage` de `result` est celui du tour), au
                # barème ; le cumul de la ligne sert de repère au tour suivant.
                usd = sum(_claude_usd(u, pin, pcache, pout, tier) for u in usage)
                if tier is not None and any(_prompt_tokens(u) > tier[0] for u in usage):
                    # L225 : un usage agrégé au-delà du seuil ne dit pas le
                    # palier de chaque requête — il se lit appel par appel ;
                    # sans appels qui couvrent l'invite, palier haut marqué.
                    par_appel = _claude_tiered_usd(usage, _claude_call_usages(events),
                                                   pin, pcache, pout, tier)
                    if par_appel is not None:
                        usd = par_appel
                    else:
                        marque = {"price_fallback": "%s : %s, palier de chaque requête "
                                  "inconnu, facturé au palier haut" % (FALLBACK_REASON, modele)}
            elif last is None or session != cum.get("session"):
                au_bareme = False
                # Session neuve (ou inconnue) : son `total_cost_usd` cumulé part de
                # zéro, donc le dernier relevé EST le coût du tour. Le soustraire
                # d'un repère d'une autre session compterait faux.
                usd = float(totals[-1])
            else:
                au_bareme = False
                usd = float(totals[-1]) - float(last)
                if usd < 0:  # session reprise : le cumul repart de plus bas
                    usd = float(totals[-1])
            return TurnUsage(
                agent, harness, modele, max(usd, 0.0),
                input_tokens=_sum(usage, "input_tokens"),
                cached_input_tokens=_sum(usage, "cache_read_input_tokens"),
                output_tokens=_sum(usage, "output_tokens"),
                cache_write_tokens=_sum(usage, "cache_creation_input_tokens"),
                **(marque if au_bareme and any(map(_has_tokens, usage)) else {}))

        if harness == "codex":
            completed = [e.get("usage") or {} for e in events if e.get("type") == "turn.completed"]
            if not completed:
                return TurnUsage(agent, harness, modele, 0.0)
            last = completed[-1]
            current = [int(last.get("input_tokens", 0)), int(last.get("cached_input_tokens", 0)),
                       int(last.get("output_tokens", 0))]
            previous = cum.get("cum_usage")
            if unknown_resume:
                # L71 : fil repris sans relevé connu — le total du fil avant le
                # tour se lit dans son journal de session Codex, s'il est là.
                before = self.codex_thread_total_before_turn(session)
                if before is not None:
                    previous, cum = before, dict(cum, session=session)
            if session == cum.get("session") and isinstance(previous, list) and len(previous) == 3 \
                    and all(c >= p for c, p in zip(current, previous)):
                # Même fil : `turn.completed` porte l'usage **cumulé** du fil.
                delta = [c - p for c, p in zip(current, previous)]
            elif previous is None or session != cum.get("session"):
                # Fil neuf : le cumul du fil EST le tour.
                delta = current
            else:
                delta = [0, 0, 0]
            uncached = max(delta[0] - delta[1], 0)
            usd = (uncached * pin + delta[1] * pcache + delta[2] * pout) / 1e6
            return TurnUsage(agent, harness, modele, usd,
                             input_tokens=delta[0], cached_input_tokens=delta[1],
                             output_tokens=delta[2],
                             **(marque if any(delta) else {}))

        # DeepSeek (et tout harnais qui publie un usage par étape).
        steps = [e.get("usage") or {} for e in events
                 if e.get("type") == "status" and e.get("phase") == "step_end"]
        tin = _sum(steps, "inputTokens")
        tcache = _sum(steps, "cacheReadTokens")
        tout = _sum(steps, "outputTokens")
        usd = (tin * pin + tcache * pcache + tout * pout) / 1e6
        return TurnUsage(agent, harness or "deepseek", modele, usd,
                         input_tokens=tin, cached_input_tokens=tcache, output_tokens=tout,
                         **(marque if tin or tcache or tout else {}))

    def _insert_turn_cost(self, *, inputs=None, **kwargs) -> None:
        """Écrit la ligne du tour ; l'attribution de messages est facultative.

        L80b : un exécuteur médié peut parler à un serveur **1.6**, dont la
        table ne connaît pas `inputs`. Le refus (`bad_args`) ne doit jamais
        suspendre la comptabilité : on réessaie sans l'attribution — le coût du
        tour reste compté, l'attribution est perdue et journalisée. Sans
        `inputs`, le paramètre n'est pas transmis du tout (compatible 1.6)."""
        insert = dict(kwargs)
        if inputs is not None:
            insert["inputs"] = inputs
        try:
            storage.of(self.db).turn_costs.insert(**insert)
        except Exception as exc:  # noqa: BLE001 - réessai limité au refus 1.6
            from . import db as db_mod
            texte = str(exc)
            if inputs is None or not isinstance(exc, db_mod.DbError) \
                    or ("bad_args" not in texte and "paramètre inconnu" not in texte):
                raise
            log("serveur médié sans `inputs` (%s) : tour compté sans attribution "
                "de messages" % texte[:160])
            insert.pop("inputs", None)
            storage.of(self.db).turn_costs.insert(**insert)

    def record(self, agent: str, start: int = 0, turn: str | None = None,
               session: str | None = None, model: str | None = None,
               key: str | None = None, ended: float | None = None,
               work_item_id: int | None = None,
               started_at: float | None = None,
               turn_id: str | None = None) -> TurnUsage:
        """Compte le tour qui vient de finir : **une** ligne `turn_costs`.

        La ligne porte le cumul brut du harnais, qui sert de repère au tour
        suivant : insertion et repère sont la même écriture. Si l'insertion
        échoue, rien n'a bougé et un tour rejoué retrouve le même delta.

        La base est donc obligatoire ici : sans elle, il n'y a pas de grand livre,
        et un repère en fichier serait exactement le défaut que ce lot corrige.

        `key` (L60) : la clé du marqueur comptable du tour. Un marqueur rejoué
        après une écriture réussie mais pas effacée (exécuteur arrêté entre les
        deux) ne réécrit pas la ligne : avant L60, il la doublait.

        `work_item_id` et `started_at` (#796) : le lot servi par le tour et son
        instant de début (epoch, horloge de la base), lus sur le marqueur
        comptable. Ils viennent du **marqueur**, jamais de la mémoire de
        l'appelant : une réparation par un autre worker écrit la même
        attribution, ou aucune si elle n'y était pas. `turn_id` (#796) :
        l'identifiant du tour (`turn_resources.turn_id`), même provenance, pour
        relier la ligne à ses ressources.
        """
        if self.db is None:
            raise CostError("record without a database: the cumulative baseline lives in turn_costs")
        events = self.events(agent, start)
        # **Résolue une fois**, sur tout le flux, puis transmise au calcul comme à
        # l'insertion : la ligne et le repère doivent porter la même session.
        session_id = session or self.session_of(agent)
        usage = self.turn_usage(agent, start, session=session_id, model=model, ended=ended)
        # L30 : le compte du tour est celui du dernier marqueur AVANT le tour.
        account = self.account_at(agent, start, usage.harness)
        cum_usd = cum_input = cum_cached = cum_out = None
        resident = _resident_event(events)
        if usage.harness == "claude":
            # L221 : le repère est le cumul du fil principal, jamais d'un sous-agent.
            totals = [float(e["total_cost_usd"]) for e in _claude_main_results(events, session_id)
                      if isinstance(e.get("total_cost_usd"), (int, float))]
            if totals:
                cum_usd = totals[-1]
        elif usage.harness == "codex":
            if resident is not None:
                # L80a : cumul natif du fil (`tokenUsage.total`), ou à défaut
                # dernier relevé de la même session + le tour ; le repère du
                # tour suivant reste ainsi le cumul du fil, pas le tour seul.
                cum_input, cum_cached, cum_out = _resident_codex_cumulative(
                    self, agent, session_id, resident)
            else:
                completed = [e.get("usage") or {} for e in events
                             if e.get("type") == "turn.completed"]
                if completed:
                    last = completed[-1]
                    cum_input = int(last.get("input_tokens", 0))
                    cum_cached = int(last.get("cached_input_tokens", 0))
                    cum_out = int(last.get("output_tokens", 0))
        if resident is not None and usage.harness != "claude":
            # L80a : le coût USD cumulé d'une session résidente (ACP) est le
            # repère durable du tour suivant, y compris après un redémarrage
            # du processus qui a perdu sa base en mémoire.
            total = _amount(resident.get("cost_total"))
            if total is not None:
                cum_usd = total
        usage = replace(usage, session=session_id or None)
        row = usage.row()
        row["turn"] = turn or usage.turn
        row["session"] = session_id
        # L80b : les messages servis par ce tour. Codex/ACP les mettent dans
        # `resident.turn.inputs` ; Claude, dont le flux porte déjà l'usage, et
        # depuis #3452 tout tour non résident, dans l'événement séparé
        # `resident.inputs` (écrit par l'exécuteur avant la ligne).
        attribution = resident if resident is not None \
            else _resident_inputs_event(events)
        inputs = None
        if attribution is not None \
                and isinstance(attribution.get("inputs"), (list, tuple)):
            inputs = tuple(str(i) for i in attribution["inputs"] if i is not None)
        self._insert_turn_cost(
            inputs=inputs,
            agent=row["agent"], harness=row["harness"], turn=row["turn"],
            model=row["model"], session=row["session"], usd=row["usd"],
            input_tokens=row["input_tokens"], cached_input_tokens=row["cached_input_tokens"],
            output_tokens=row["output_tokens"], cum_usd=cum_usd, cum_input_tokens=cum_input,
            cum_cached_input_tokens=cum_cached, cum_output_tokens=cum_out,
            account=account, spend_key=key, void_reason=usage.void_reason,
            price_fallback=usage.price_fallback,
            cache_write_tokens=usage.cache_write_tokens,
            # #796 (0423) : le lot du tour et son début, portés par le marqueur
            work_item_id=None if work_item_id is None else int(work_item_id),
            started_at=None if started_at is None else float(started_at),
            # #796 (0423) : l'identifiant du tour, pour le relier à ses ressources
            turn_id=turn_id)
        return usage

    def account_at(self, agent: str, start: int, harness: str | None = None) -> str | None:
        """Le compte du tour qui commence à la ligne `start` du flux (L30), ou None.

        C'est le dernier marqueur de compte écrit avant cette ligne : l'exécuteur
        le pose juste avant de compter l'index de début du tour. None quand aucun
        marqueur ne précède (hôte sans comptes déclarés).
        """
        path = os.path.join(self.agent_dir(agent), "events.jsonl")
        found = None
        try:
            with open(path, encoding="utf-8") as fh:
                for index, line in enumerate(fh):
                    if index >= start:
                        break
                    if ACCOUNT_MARKER not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or event.get("type") != ACCOUNT_MARKER:
                        continue
                    if harness and event.get("harness") not in (None, "", harness):
                        continue
                    found = str(event.get("account") or "") or None
        except OSError:
            return None
        return found

    # -- dépense ------------------------------------------------------------
    def spent(self, agent: str = "all", seconds: float = 3600.0,
              harnesses: Sequence[str] | None = None,
              account: str | None = None) -> float:
        """La dépense des `seconds` dernières secondes, lue sur `turn_costs`.

        `harnesses` restreint la somme à ces harnais : le plafond horaire de
        0019 §2 ne vise que l'**usage payé au token** (`paid_harnesses_of()`), pas
        les estimations des forfaits (Claude, Codex), dont la vraie limite est
        la garde de rythme.
        """
        if self.db is None:
            return 0.0
        if account is not None:
            return storage.of(self.db).turn_costs.spent(seconds, agent=agent,
                                                         harnesses=harnesses,
                                                         account=account)
        return storage.of(self.db).turn_costs.spent(seconds, agent=agent,
                                                     harnesses=harnesses)

    def paid_hour(self, paid: Sequence[str]) -> float:
        """L242b (décision de mesh-design du 2026-10-12) : la dépense payée au
        token du mesh sur les 60 dernières minutes, comptée sur la **baisse
        réelle des soldes**, série par série (fournisseur, compte) :

        * chaque baisse entre deux relevés compte pour sa part dans l'heure,
          répartie comme l'estimation du grand livre de CE compte sur
          l'intervalle (au prorata du temps s'il porte moins de
          `balance.GAP_MIN_USD` estimés) ;
        * hors de l'intervalle couvert par ses relevés, l'estimation du
          compte, corrigée par son rapport réel/estimé (s'il porte au moins
          `balance.GAP_MIN_USD` estimés ; jamais sous `PAID_RATIO_FLOOR`) ;
        * un compte sans relevé : son estimation, telle quelle (#373 r1 : un
          compte relevé ne couvre jamais les autres). Une série sans compte
          nommé couvre les lignes qu'aucune série nommée ne couvre.

        Soldes ou grand livre illisibles : l'estimation seule."""
        return self.paid_spent(paid, 3600.0)

    def paid_spent(self, paid: Sequence[str], seconds: float = 3600.0,
                   agent: str = "all", *, cache: dict | None = None) -> float:
        """#513 : `paid_hour` sur les `seconds` dernières secondes, pour le
        mesh (`agent="all"`) ou pour un agent. La dépense mesurée (baisse des
        soldes) fait foi ; l'estimation du grand livre ne sert qu'entre deux
        relevés, corrigée par le rapport réel/estimé de son compte.

        La fenêtre décide d'abord ce que le mesh compte de chaque baisse
        (L242b, `paid_hour`) ; un agent reçoit, de ce montant, la part de ses
        estimations dans celles de son compte **dans la fenêtre**. Une baisse
        sans estimation dans la fenêtre n'est attribuable à personne : la part
        de l'agent est nulle (le mesh garde, lui, le repli temporel).

        `cache` (#3923) : un dict fourni par une **passe** (la prise
        automatique) partage la lecture des soldes entre ses appels — les
        fenêtres du mesh sont les mêmes pour chaque candidat. Sans cache,
        chaque appel relit comme avant : un appelant qui rend la main entre
        deux passes n'obtient jamais une valeur d'une autre passe."""
        key = (tuple(paid), float(seconds), agent) if cache is not None else None
        if key is not None and key in cache:
            return cache[key]
        ledger = self.spent(agent, seconds, harnesses=paid)
        if self.db is None or not paid:
            return ledger
        # l'heure réelle, comme les relevés et le grand livre (datés par la
        # base) — pas `self.clock`, l'horloge des jauges
        now = time.time()
        try:
            total = self._paid_spent(list(paid), now - float(seconds), now, agent)
        except Exception:  # noqa: BLE001 - soldes illisibles : l'estimation
            total = ledger
        if key is not None:
            cache[key] = total
        return total

    def _paid_spent(self, paid: list, start: float, now: float, agent: str) -> float:
        from . import balance
        rows = storage.of(self.db).operations.balances(provider=None,
                                                       since_s=now - start + 1.0)
        series: dict = {}
        for row in sorted(rows, key=lambda r: float(r["observed_ts"])):
            if row["provider"] in paid and row["currency"] == "USD" \
                    and float(row["observed_ts"]) <= now:
                series.setdefault((row["provider"], row.get("account")), []).append(row)
        series = {key: points for key, points in series.items() if len(points) >= 2}
        # tout l'intervalle depuis le plus ancien relevé, sans coupure (#373
        # r2) : une estimation écartée ferait paraître toute la baisse dans
        # la fenêtre. Il reste borné : `balances` ne rend, avant la fenêtre,
        # que le dernier relevé de chaque série.
        oldest = min([float(points[0]["observed_ts"]) for points in series.values()]
                     + [start])
        lines = storage.of(self.db).turn_costs.paid_lines(oldest, harnesses=paid)

        def estimated(mine, low, high):
            return sum(float(line["usd"]) for line in mine if low < float(line["ts"]) <= high)

        def counted(mine):
            return mine if agent == "all" else [line for line in mine
                                                if line.get("agent") == agent]

        total = 0.0
        for harness in paid:
            own = {acct: pts for (prov, acct), pts in series.items() if prov == harness}
            named = {acct for acct in own if acct is not None}
            spent = [line for line in lines if line["harness"] == harness]
            covered = set(named) | ({None} if None in own else set())
            for acct, points in own.items():
                mine = [line for line in spent
                        if (line.get("account") == acct if acct is not None
                            else line.get("account") not in named)]
                ours = counted(mine)
                pairs = [(float(prev["observed_ts"]), float(cur["observed_ts"]),
                          max(0.0, float(prev["total"]) - float(cur["total"])))
                         for prev, cur in zip(points, points[1:])]
                first, last = pairs[0][0], pairs[-1][1]
                est_all = estimated(mine, first, last)
                ratio = 1.0
                if est_all >= balance.GAP_MIN_USD:
                    ratio = max(PAID_RATIO_FLOOR, sum(p[2] for p in pairs) / est_all)
                real_in = 0.0
                for low, high, drop in pairs:
                    if high <= start or not drop:
                        continue
                    pair = estimated(mine, low, high)
                    # la fenêtre décide d'abord ce que le mesh compte de cette
                    # baisse (L242b, inchangé) : l'intervalle entier, sinon la
                    # part de l'estimation du compte, sinon le prorata temporel
                    if low >= start:
                        share = drop
                    elif pair >= balance.GAP_MIN_USD:
                        share = drop * estimated(mine, start, high) / pair
                    else:
                        share = drop * (high - start) / (high - low)
                    if agent == "all":
                        real_in += share
                        continue
                    # #513 r3 (B/P2 de la relecture) : l'agent reçoit une part
                    # de ce que le mesh compte DÉJÀ sur la fenêtre, répartie
                    # comme ses propres estimations dans la fenêtre ; `part <=
                    # window` (mêmes lignes, filtrées), donc sa part est bornée
                    # par `share`. #513 r2 : dès qu'il y a une estimation
                    # (`window > 0`), l'ancien repli `part * ratio` (ratio 1,0
                    # sous le seuil) laissait l'agent sous son plafond quand le
                    # compte entier restait sous `GAP_MIN_USD` (0,40 $ estimés
                    # pour 0,80 $ de baisse). Sans estimation dans la fenêtre,
                    # rien n'est attribuable (`part` est nul, `part * ratio`
                    # aussi) : le mesh garde le repli temporel de L242b.
                    window = estimated(mine, max(low, start), high)
                    part = estimated(ours, max(low, start), high)
                    real_in += share * part / window if window > 0 else part * ratio
                outside = estimated(ours, start, min(first, now)) \
                    + estimated(ours, max(last, start), now)
                total += real_in + outside * ratio
            # les comptes qu'aucune série ne couvre : leur estimation
            if None not in covered:
                total += sum(float(line["usd"]) for line in counted(spent)
                             if float(line["ts"]) > start
                             and line.get("account") not in named)
        return total

    def over(self, agent: str = "all", *,
             paid_harnesses: Sequence[str] | None = None, pace: bool = True,
             reserve: float = 0.0, hourly_share: float = 1.0,
             account_checked: bool = False, cache: dict | None = None) -> str:
        """Le garde-fou : raison si l'agent (ou le compte) doit s'arrêter, sinon ''.

        Le plafond horaire ne somme que l'usage payé au token
        (`paid_harnesses`, défaut `paid_harnesses_of()`, lu dans les descripteurs) ;
        les forfaits sont couverts par `pace_exceeded`, pas par cette somme
        (0019 §2), et un agent au forfait n'y est pas soumis (L49).

        `reserve` (L242b (2)) : un agent non critique s'arrête autant sous
        le plafond de rythme. `hourly_share` (L242b) : il s'arrête à cette
        part du plafond horaire du mesh (`noncritical_hourly_share`).

        `pace=False` (L30) : le rythme est jugé compte par compte par
        `ameesh.accounts`, qui bascule au lieu de mettre en pause ; seul le
        plafond horaire reste ici si `account_checked=True`. Sans compte
        déjà évalué, un relevé ambigu reste interdit même sans rythme.

        L70 : même règle pour le plafond par jour glissant du mesh
        (`daily_usd`) et pour les plafonds propres à l'agent
        (`agent_limits`, sa seule dépense payée au token). Un plafond nul ou
        absent ne s'applique pas.

        #513 : tous ces plafonds comptent la dépense mesurée sur les soldes
        (`paid_spent`), l'estimation ne servant qu'entre deux relevés.

        `cache` (#3923) : le dict d'une passe de prise, partagé par ses
        candidats — les fenêtres du mesh ne sont pas relues pour chacun.
        """
        harness = self.tool_of(agent) if agent != "all" else ""
        reason = ""
        if harness:
            reason = (self.pace_exceeded(harness, reserve) if pace else
                      "" if account_checked else self.default_account_error(harness))
        if reason:
            return reason
        paid = paid_harnesses_of() if paid_harnesses is None else tuple(paid_harnesses)
        # L49 : un agent au forfait (harnais connu, non payé au token) n'est
        # jamais mis en pause par le plafond payé au token des autres (0019 §2) ;
        # un harnais inconnu reste soumis au plafond (fail-closed).
        if harness and harness not in paid:
            return ""
        # L70 : plafonds du mesh (heure, jour glissant), puis ceux de l'agent
        for window, cap in ((3600, self.hourly_usd), (86400, self.daily_usd)):
            if cap and cap > 0:
                total = self.paid_spent(paid, window, cache=cache)
                held = cap * hourly_share if window == 3600 else cap
                if total >= held:
                    return ("%s budget (pay-per-token): %.2f $ over %s "
                            "(cap %.2f $%s)" % (
                                WINDOW_LABELS[window][0], total, WINDOW_LABELS[window][1],
                                cap, ", %.2f $ for a non-critical agent" % held
                                if held < cap else ""))
        if agent != "all":
            for window, cap in sorted((self.agent_limits.get(agent) or {}).items()):
                if cap and cap > 0 and window in WINDOW_LABELS:
                    total = self.paid_spent(paid, window, agent, cache=cache)
                    if total >= cap:
                        return ("%s budget of agent %s (pay-per-token): %.2f $ over %s "
                                "(cap %.2f $)" % (WINDOW_LABELS[window][0], agent, total,
                                                      WINDOW_LABELS[window][1], cap))
        return ""

    # -- rapport ------------------------------------------------------------
    def report(self, accounts: dict | None = None, *, record: bool = False,
               multipliers: dict | None = None, caps: dict | None = None) -> list:
        """Une ligne par agent : harnais, modèle, dépense 1 h/24 h, jauges.

        `accounts` (L30) : `{harnais: (compte actif, [Gauge…])}` pour les
        harnais à comptes déclarés ; leurs jauges sont alors celles du compte
        actif, et la ligne nomme ce compte (`account`). `multipliers` (#1028,
        #1075) : `{harnais: multiplicateur du compte actif}`, le
        `pace_multiplier` du profil (`accounts.report`) — le plafond affiché
        suit le rythme du compte, jamais le rythme nominal ; un harnais absent
        garde 1.0. `caps` (#3680) : `{harnais: {clé de jauge: plafond}}`, les
        plafonds de la **garde** déjà calculés par `accounts.report`
        (`gauges[].cap`, `accounts.limits` : plancher de sous-utilisation,
        plafond de rythme et réserve compris) ; ils priment sur le rythme
        recalculé ici, qui n'applique ni plancher ni plafond de compte.

        L71 : une **lecture** — l'historique des jauges n'est écrit que si
        `record` est vrai. Avec un registre, seuls ses agents sont listés (les
        dossiers d'état `fils`, `hooks`, `notify`… ne sont pas des agents).
        """
        rows = []
        now = self.clock()
        accounts = accounts or {}
        multipliers = multipliers or {}
        caps = caps or {}
        try:
            paid = set(paid_harnesses_of())
        except CostError:
            paid = None          # aucun descripteur : on ne devine pas
        # Le forfait est partagé par tout le compte : les jauges se lisent une fois
        # par harnais, pas une fois par agent.
        by_harness: dict = {name: list(value[1]) for name, value in accounts.items()}
        registre = self.registry_tools()
        for agent in self.agents():
            if registre and agent not in registre and agent not in self.tools:
                continue
            harness = self.tool_of(agent) or "?"
            # Les forfaits à jauges sont déclarés par les descripteurs (L16) :
            # la jauge se lit une fois par harnais, jamais par agent.
            if harness not in by_harness and gauges_source(harness) in ("transcript",
                                                                        "sessions"):
                by_harness[harness] = self.gauges(harness, record=record)
            if harness in accounts:
                rows.append({"account": accounts[harness][0]})
            else:
                rows.append({})
            rows[-1].update({
                "agent": agent,
                "harness": harness,
                "model": self.model_of(agent) or "default",
                # L95 : payé au token (vrais dollars) ou forfait (équivalent
                # théorique au barème) ; None si les descripteurs sont illisibles
                "paid": (harness in paid) if paid is not None else None,
                "spent_1h": self.spent(agent, 3600),
                "spent_24h": self.spent(agent, 86400),
                "gauges": [
                    {
                        "key": g.key,
                        "used": g.used_at(now),          # L71 : 0 si fenêtre échue
                        "last_used": g.used,
                        "reset_passed": g.reset_passed(now),
                        "cap": (caps.get(harness) or {}).get(
                            g.key, g.pace_cap(now, multipliers.get(harness, 1.0))),
                        "elapsed": g.elapsed(now),
                        "resets_at": g.resets_at,
                    }
                    for g in by_harness.get(harness, [])
                ],
            })
            error = getattr(by_harness.get(harness), "errors", {}).get(harness)
            if error:
                rows[-1]["gauge_error"] = error
        return rows


# --------------------------------------------------------------------------
# utiles
# --------------------------------------------------------------------------

def format_report(rows: Iterable[dict]) -> str:
    """Le tableau de `ameesh cost report`, puis ses deux totaux (L95)."""
    rows = list(rows)
    lines = ["%-16s %-9s %-16s %-8s %10s %10s  %s" % (
        "agent", "harness", "model", "payment", "1 h (USD)", "24 h (USD)", "gauges")]
    for row in rows:
        gauges = ", ".join(
            "%s %.0f%% (pace %.0f%%%s)" % (
                g["key"], g["used"] * 100, g["cap"] * 100,
                ", reset passed, last reading %.0f%%" % (g["last_used"] * 100)
                if g.get("reset_passed") else "")
            for g in row["gauges"]) or row.get("gauge_error") or "—"
        lines.append("%-16s %-9s %-16s %-8s %10.4f %10.4f  %s" % (
            row["agent"],
            row["harness"] + ("/%s" % row["account"] if row.get("account") else ""),
            row["model"], PAYMENT_LABELS.get(row.get("paid"), "?"),
            row["spent_1h"], row["spent_24h"], gauges))
    lines.append("")
    lines += format_totals(totals(rows))
    return "\n".join(lines)


#: libellé du mode de paiement d'une ligne de rapport (L95)
PAYMENT_LABELS = {True: "token", False: "plan", None: "?"}


def totals(rows: Iterable[dict]) -> dict:
    """Les deux totaux d'un rapport, jamais additionnés (L95) : `paid` =
    dépense payée au token (de vrais dollars, estimés tour par tour) ;
    `plan_value` = valeur consommée sur les forfaits (équivalent théorique au
    barème : un tour au forfait ne coûte rien de plus). Une ligne dont le
    mode est inconnu compte dans `unknown`."""
    out = {key: {"1h": 0.0, "24h": 0.0} for key in ("paid", "plan_value", "unknown")}
    for row in rows:
        key = {True: "paid", False: "plan_value"}.get(row.get("paid"), "unknown")
        out[key]["1h"] += float(row.get("spent_1h") or 0.0)
        out[key]["24h"] += float(row.get("spent_24h") or 0.0)
    return {k: {w: round(v, 6) for w, v in d.items()} for k, d in out.items()}


def format_totals(sums: dict) -> list:
    lines = ["spent (pay-per-token, estimated): 1 h %.4f $ · 24 h %.4f $" % (
                 sums["paid"]["1h"], sums["paid"]["24h"]),
             "value consumed on plans:          1 h %.4f $ · 24 h %.4f $ "
             "(price-list equivalent, not paid per turn)" % (
                 sums["plan_value"]["1h"], sums["plan_value"]["24h"])]
    if sums["unknown"]["1h"] or sums["unknown"]["24h"]:
        lines.append("unknown payment mode:             1 h %.4f $ · 24 h %.4f $" % (
            sums["unknown"]["1h"], sums["unknown"]["24h"]))
    return lines


def _claude_main_results(events: Sequence[dict], session: str | None) -> list[dict]:
    """L221 : les `result` du fil principal d'un tour Claude.

    Ni ceux d'un sous-agent (`parent_tool_use_id`), ni ceux d'une autre session :
    leur cumul n'est pas celui du processus du tour."""
    return [e for e in events
            if e.get("type") == "result" and not e.get("parent_tool_use_id")
            and (not session or e.get("session_id") in (None, "", session))]


def _claude_usd(usage: dict, pin: float, pcache: float, pout: float,
                tier: tuple | None = None) -> float:
    """Coût d'un usage Claude au barème (USD), écritures de cache comprises (L225).

    `cache_creation_input_tokens` au prix d'entrée ×1,25 (cache 5 min), sauf la
    part que `cache_creation.ephemeral_1h_input_tokens` déclare en cache 1 h
    (×2) ; durée non déclarée : ×1,25. `tier` (`prompt_tier`) : invite d'au
    plus le seuil (entrée + cache relu + cache écrit) au palier bas."""
    written = _num(usage, "cache_creation_input_tokens")
    if tier is not None and (_num(usage, "input_tokens") + written
                             + _num(usage, "cache_read_input_tokens")) <= tier[0]:
        pin, pcache, pout = tier[1]
    hour = _hour_written(usage)
    return (_num(usage, "input_tokens") * pin
            + ((written - hour) * CACHE_WRITE_5M + hour * CACHE_WRITE_1H) * pin
            + _num(usage, "cache_read_input_tokens") * pcache
            + _num(usage, "output_tokens") * pout) / 1e6


def _hour_written(usage) -> int:
    """L225 : les écritures de cache qu'un usage Claude déclare en cache 1 h."""
    detail = usage.get("cache_creation") if isinstance(usage, dict) else None
    if not isinstance(detail, dict):
        return 0
    return min(_num(detail, "ephemeral_1h_input_tokens"),
               _num(usage, "cache_creation_input_tokens"))


#: L225 : les jetons d'une invite Claude (entrée, cache écrit, cache relu)
PROMPT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _prompt_tokens(usage) -> int:
    """L225 : la longueur d'une invite Claude, celle qui fixe le palier."""
    return sum(_num(usage, k) for k in PROMPT_KEYS)


def _claude_tiered_usd(totals: Sequence[dict], calls: Sequence[dict], pin: float,
                       pcache: float, pout: float, tier: tuple) -> float | None:
    """L225 : le coût d'un usage agrégé, chaque requête à son palier.

    `calls` (`_claude_call_usages`) doit couvrir exactement l'invite de
    `totals` ; sinon None (l'appelant facture au palier haut, marqué). Le flux
    peut sous-déclarer la sortie d'un appel : le reste de la sortie agrégée va
    au palier haut. De même pour la durée du cache : les écritures que
    l'agrégat déclare en cache 1 h et que les appels ne disent pas passent du
    tarif 5 min au tarif 1 h sur les écritures sans durée des appels, les plus
    chères (palier haut) d'abord — un majorant, jamais sous l'agrégat."""
    calls = [u for u in calls if _has_tokens(u)]
    if not calls or any(_sum(calls, k) != _sum(totals, k) for k in PROMPT_KEYS):
        return None
    reste = max(_sum(totals, "output_tokens") - _sum(calls, "output_tokens"), 0)
    usd = sum(_claude_usd(u, pin, pcache, pout, tier) for u in calls) + reste * pout / 1e6
    heure = sum(map(_hour_written, totals)) - sum(map(_hour_written, calls))
    sans_duree = sorted(((tier[1][0] if _prompt_tokens(u) <= tier[0] else pin,
                          _num(u, "cache_creation_input_tokens") - _hour_written(u))
                         for u in calls), reverse=True)
    for prix, ecrits in sans_duree:
        if heure <= 0:
            break
        part = min(heure, ecrits)
        usd += part * (CACHE_WRITE_1H - CACHE_WRITE_5M) * prix / 1e6
        heure -= part
    return usd


def _has_tokens(usage) -> bool:
    """L221 : un usage Claude qui compte au moins un jeton."""
    return isinstance(usage, dict) and any(
        _num(usage, k) > 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                     "cache_read_input_tokens", "output_tokens"))


def _claude_call_usages(events: Sequence[dict]) -> list[dict]:
    """L105 : l'usage de chaque appel au modèle d'un tour Claude sans `result`.

    Le flux répète l'usage d'un message sur chacun de ses blocs : il n'est
    compté qu'une fois par id de message."""
    vus: set = set()
    out: list[dict] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        ident = message.get("id")
        if ident:
            if ident in vus:
                continue
            vus.add(ident)
        out.append(usage)
    return out


def _claude_windows(event: dict, read_at: float | None = None) -> list | None:
    """Les fenêtres d'un `rate_limit_event` Claude, ou None si illisibles.

    `read_at` (#1561) : l'horodatage du relevé — le marqueur de compte qui
    précède le tour, ou la date de l'événement pour un flux sans marqueur
    (#1720, 0.0 = non daté).
    """
    info = event.get("rate_limit_info") or {}
    windows = info.get("unifiedWindows") or {} if isinstance(info, dict) else None
    if not isinstance(windows, dict):
        return None
    return [
        Gauge("claude", key, _fraction(value.get("utilization")),
              _epoch(value.get("resetsAt")),
              float(KNOWN_WINDOWS.get(key, KNOWN_WINDOWS["seven_day"])),
              read_at)
        for key, value in windows.items() if isinstance(value, dict)
    ]


def _claude_overage(event: dict) -> Overage:
    """The overage flags of a Claude `rate_limit_event` (L226b)."""
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return Overage()
    status = info.get("overageStatus")
    capable = None if not isinstance(status, str) or not status \
        else status.startswith("allowed")
    return Overage(capable, info.get("isUsingOverage") is True)


def _codex_overage(limits: dict, gauges: list) -> Overage:
    """The overage flags of a Codex `rate_limits` object (L226b): credits
    let the account go on, billed, once a window is at 100 %.

    #1176 (B1) : un objet `credits` vide, partiel, ou dont les champs ne sont
    pas des booléens ne prouve RIEN — `capable` reste None (inconnu), et
    aucun relèvement de fin de fenêtre ne s'appuie dessus. Seule une capacité
    explicite (`has_credits` ou `unlimited` vrai) rend True ; une incapacité
    n'est rendue False que si elle est explicite (`has_credits` faux) et non
    contredite par `unlimited` vrai.
    """
    credits = limits.get("credits")
    if not isinstance(credits, dict):
        return Overage()
    has_credits, unlimited = credits.get("has_credits"), credits.get("unlimited")
    if has_credits is True or unlimited is True:
        capable: bool | None = True
    elif has_credits is False:
        capable = False
    else:
        capable = None
    return Overage(capable, capable is True and any(g.used >= 1.0 for g in gauges))


def _find_rate_limits(node, depth: int = 0):
    """Le premier objet « rate_limits » d'un événement, quel que soit son ordre.

    On reconnaît la forme au fond, pas au chemin : un objet qui porte `primary` ou
    `secondary` avec un `used_percent`. Les journaux des harnais changent de
    nesting sans prévenir, et une jauge manquée est une pause manquée.
    """
    if depth > 6:
        return None
    if isinstance(node, dict):
        for key in ("primary", "secondary"):
            value = node.get(key)
            if isinstance(value, dict) and "used_percent" in value:
                return node
        for value in node.values():
            found = _find_rate_limits(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_rate_limits(value, depth + 1)
            if found:
                return found
    return None


def _codex_event_model(event) -> str:
    """Le modèle porté par un événement du journal de session Codex, ou ''."""
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    kind = event.get("type")
    if kind in ("turn_context", "session_meta") and isinstance(payload.get("model"), str):
        return payload["model"].strip()
    if payload.get("type") == "thread_settings_applied":
        settings = payload.get("thread_settings")
        if isinstance(settings, dict) and isinstance(settings.get("model"), str):
            return settings["model"].strip()
    return ""


_TOML_MODEL = re.compile(r'^\s*model\s*=\s*"([^"]+)"\s*(?:#.*)?$')


def _codex_config_model(path: str) -> str:
    """`model = "…"` de premier niveau d'un `config.toml` Codex, ou ''."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.lstrip().startswith("["):
                    break               # une table : on sort du premier niveau
                match = _TOML_MODEL.match(line)
                if match:
                    return match.group(1).strip()
    except OSError:
        pass
    return ""


def _byte_offset_of_line(flux, numero: int, bloc: int = 1 << 20) -> int | None:
    """L'offset du début de la ligne `numero` (0 = la première) d'un flux binaire.

    Compte les sauts de ligne par blocs, sans décoder ni garder une ligne :
    c'est le saut rapide de `events(start=…)` sur un journal de plusieurs
    centaines de Mo. None quand le flux a moins de lignes (appelant : aucun
    événement dans la tranche)."""
    if numero <= 0:
        return 0
    flux.seek(0)
    vues = 0
    offset = 0
    while True:
        morceau = flux.read(bloc)
        if not morceau:
            return None
        debut = 0
        while True:
            i = morceau.find(b"\n", debut)
            if i < 0:
                break
            vues += 1
            if vues == numero:
                return offset + i + 1
            debut = i + 1
        offset += len(morceau)


def _parse_events(lines: Iterable[str], start: int, end: int | None = None) -> list:
    """Les événements JSONL des lignes `[start, end)` (#2096).

    Les lignes hors de la tranche sont comptées sans être analysées : c'est
    tout l'intérêt du flux — le journal peut faire des centaines de Mo, la
    tranche d'un tour quelques kilo-octets. Un `[]` ou un `null` est une ligne
    JSON valide mais pas un événement : le garder ferait planter le premier
    `.get` venu (sonde codex2)."""
    out = []
    for index, line in enumerate(lines):
        if index < start:
            continue
        if end is not None and index >= end:
            break
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _lines_reverse(path: str, bloc: int = 1 << 20) -> Iterator[bytes]:
    """Les lignes d'un fichier, de la dernière à la première (#2096).

    Lit par blocs depuis la fin et recolle la ligne à cheval sur deux blocs.
    Les morceaux de cette ligne s'accumulent dans une liste et ne sont joints
    qu'une fois, quand elle est complète : une ligne géante (un événement de
    plusieurs Mo) coûte alors O(n), au lieu de la concaténation quadratique
    d'un `bloc + reste` qui recopiait toute la ligne à chaque tour (#2125).
    La mémoire reste bornée par le bloc et la plus longue ligne, jamais par le
    fichier. Les lignes sont rendues en octets, décodées par l'appelant."""
    try:
        with open(path, "rb") as flux:
            flux.seek(0, os.SEEK_END)
            pos = flux.tell()
            morceaux: list[bytes] = []  # la ligne en cours, de sa fin vers son début
            while pos > 0:
                debut = max(0, pos - bloc)
                flux.seek(debut)
                donnees = flux.read(pos - debut)
                pos = debut
                parties = donnees.split(b"\n")
                if len(parties) == 1:
                    # aucun saut de ligne : tout le bloc prolonge la ligne en cours
                    morceaux.append(parties[0])
                    continue
                morceaux.append(parties[-1])
                yield b"".join(reversed(morceaux))
                morceaux = [parties[0]]  # peut-être la fin d'une ligne antérieure
                for ligne in reversed(parties[1:-1]):
                    yield ligne
            if morceaux:
                ligne = b"".join(reversed(morceaux))
                if ligne:                # même contrat que l'ancien `if reste`
                    yield ligne
    except OSError:
        return


def _session_event(event: dict) -> str:
    """Ce qu'un événement apporte à la session courante (L60, L221).

    Vide pour un événement de sous-agent ; sinon sa clé `session` si elle est
    remplie, à défaut le dernier identifiant non vide (`session_id`,
    `thread_id`, `sessionId` — l'ordre des remplacements du relevé direct)."""
    if event.get("parent_tool_use_id"):
        return ""                # L221 : la session d'un sous-agent n'est pas celle du tour
    found = ""
    for key in ("session_id", "thread_id", "sessionId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            found = value
    session = event.get("session")
    if isinstance(session, str) and session:
        found = session
    return found


def _last_session(events: Iterable[dict]) -> str:
    """L'identifiant de session/fil le plus récent du flux, s'il y en a un.

    `sessionId` est la clé du harnais DeepSeek (`{"type": "session",
    "sessionId": …}`) et du pont ACP : sans elle, les tours DeepSeek étaient
    inscrits au grand livre sans session (L60)."""
    found = ""
    for event in events:
        found = _session_event(event) or found
    return found


def _resident_event(events: Iterable[dict]) -> dict | None:
    """L'événement `resident.turn` de la tranche, s'il y en a un (L80a).

    Un tour logique n'en écrit qu'un ; le dernier est retenu, comme le
    `turn.completed` d'un flux natif. La présence de cet événement fait foi :
    `turn_usage` ne retombe pas sur les relevés natifs de la tranche, pour ne
    jamais compter deux fois le même tour."""
    found = None
    for event in events:
        if event.get("type") == RESIDENT_USAGE_EVENT:
            found = event
    return found


def turn_attribution_lot(spec: dict, session_lot: str | None) -> str | None:
    """#3452 : le lot imputé au grand livre pour un tour.

    Le lot explicite du tour (courrier lié, réveil de lot, consigne) fait foi.
    Un tour de **courrier** sans lot déductible (pilotage, courrier ambigu,
    avis) n'hérite pas du lot de session : il n'y a pas travaillé, et le grand
    livre l'imputait au lot de session (206 tours de mesh-design sur #1606,
    117 USD, constat de l'analyse du 25/10). Les tours sans courrier (suite,
    réveil, résumé de session) gardent le lot de session."""
    lot = str((spec or {}).get("lot") or "").strip()
    if lot:
        return lot
    if (spec or {}).get("ids"):
        return None
    return str(session_lot or "").strip() or None


def _resident_inputs_event(events: Iterable[dict]) -> dict | None:
    """L'événement `resident.inputs` de la tranche, s'il y en a un (L80b).

    Le flux de Claude porte lui-même l'usage du tour, donc il n'écrit pas de
    `resident.turn` ; les messages servis voyagent dans cet événement, écrit
    après la barrière steer. Depuis #3452, un tour non résident écrit le même
    événement (même type, même lecture) : c'est une attribution seule —
    l'usage reste celui du flux ou de `turn.completed`."""
    found = None
    for event in events:
        if event.get("type") == RESIDENT_INPUTS_EVENT:
            found = event
    return found


def _resident_turn_usage(agent: str, harness: str, modele: str, event: dict,
                         pin: float, pcache: float, pout: float,
                         marque: dict, *, cum: dict, session: str | None,
                         unknown_resume: bool) -> "TurnUsage":
    """La ligne d'un tour résident, lue sur son `resident.turn` (L80a).

    Codex publie `usage.last` : l'entrée du `TurnResult` est hors cache, le
    cache à part. Le grand livre Codex compte l'entrée cache comprise
    (`TurnUsage.input_tokens`), d'où la somme. Les autres harnais résidents
    (ACP) publient un contexte `used` et, souvent, un coût USD cumulé par
    session (`cost`/`cost_total`) : le coût du tour, quand il est là, fait foi
    plutôt qu'un barème sur des jetons qui ne sont pas des jetons facturés.

    Une reprise de processus (sommeil, réglages changés, redémarrage du
    worker) perd la base en mémoire du pilote : `cost_total` seul reste alors
    le cumul de la session, et son delta se calcule sur le dernier repère USD
    **durable** de la même session (le grand livre), jamais sur zéro — sinon
    le premier prompt d'une session reprise compterait tout son passé."""
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    if harness == "codex":
        uncached = _num(usage, "input_tokens")
        cached = _num(usage, "cache_read_input_tokens")
        output = _num(usage, "output_tokens")
        usd = (uncached * pin + cached * pcache + output * pout) / 1e6
        return TurnUsage(agent, harness, modele, usd,
                         input_tokens=uncached + cached,
                         cached_input_tokens=cached,
                         output_tokens=output,
                         **(marque if (uncached or cached or output) else {}))
    input_tokens = _num(usage, "input_tokens")
    cached = _num(usage, "cache_read_input_tokens")
    output = _num(usage, "output_tokens")
    cost = _amount(event.get("cost"))
    if cost is not None:
        usd = max(cost, 0.0)
        mark = {}
    else:
        total = _amount(event.get("cost_total"))
        usd = None
        if total is not None:
            base = None
            if cum.get("session") == session and cum.get("cum_usd") is not None:
                base = float(cum["cum_usd"])
            elif not unknown_resume and cum.get("session") != session:
                # session neuve : son cumul USD part de zéro
                base = 0.0
            if base is not None:
                usd = total - base
                if usd < 0:      # session rouverte : le cumul est reparti
                    usd = total
                mark = {}
        if usd is None:
            # Aucun repère USD durable : le tour se facture au barème sur ses
            # jetons (le seul estimable), comme avant L80a.
            usd = (input_tokens * pin + cached * pcache + output * pout) / 1e6
            mark = marque if (input_tokens or cached or output) else {}
    return TurnUsage(agent, harness, modele, usd,
                     input_tokens=input_tokens,
                     cached_input_tokens=cached,
                     output_tokens=output,
                     **mark)


def _resident_codex_cumulative(book: "CostBook", agent: str, session: str | None,
                               event: dict) -> tuple[int, int, int]:
    """Cumul du fil Codex après un tour résident (L80a).

    `usage_total` vient de `thread/tokenUsage/updated.total` : c'est le cumul
    natif du fil, qui sert de repère au tour suivant. À défaut, le cumul
    s'obtient en ajoutant le tour au dernier relevé connu de la même session
    (jamais à celui d'une autre session)."""
    total = event.get("usage_total")
    if isinstance(total, dict) and total:
        return (_num(total, "input_tokens"), _num(total, "cached_input_tokens"),
                _num(total, "output_tokens"))
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    previous = [0, 0, 0]
    base = book.baseline(agent, "codex", session)
    if base.get("session") == session and isinstance(base.get("cum_usage"), list):
        previous = base["cum_usage"]
    uncached = _num(usage, "input_tokens")
    cached = _num(usage, "cache_read_input_tokens")
    return (int(previous[0]) + uncached + cached,
            int(previous[1]) + cached,
            int(previous[2]) + _num(usage, "output_tokens"))


def _read_tail(path: str, limit: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > limit:
                fh.seek(size - limit)
            return fh.read(limit).decode("utf-8", "ignore")
    except OSError:
        return ""


def _is_claude_marker(event: dict) -> bool:
    return (event.get("type") == ACCOUNT_MARKER
            and event.get("harness") in (None, "", "claude"))


def _last_claude_marker(fh, end: int) -> dict | None:
    """Find the last complete Claude marker before end by reverse block scan.

    Memory holds one block plus the JSONL line crossing its boundary. Stop at
    the first matching marker; scan to the beginning to prove a legacy stream.
    Read failures propagate so callers cannot mistake them for legacy input.
    """
    pending: list[bytes] = []
    while end:
        start = max(0, end - 64_000)
        fh.seek(start)
        block = fh.read(end - start)
        if len(block) != end - start:
            raise OSError("Account marker prefix changed during read")
        lines = block.split(b"\n")
        if len(lines) > 1:
            lines[-1] += b"".join(reversed(pending))
            pending = []
        if start:
            pending.append(lines.pop(0))
        else:
            lines[0] += b"".join(reversed(pending))
        for line in reversed(lines):
            if ACCOUNT_MARKER.encode() not in line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict) and _is_claude_marker(event):
                return event
        end = start
    return None


def _journal_signature(stat) -> tuple:
    # Inode/device cover replacement; nanosecond ctime covers in-place writes
    # even when a producer restores mtime. Size detects append/truncation.
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


#: below this age a journal's ctime cannot vouch for its content: the kernel
#: stamps ctime from a coarse clock (one tick, several ms), so a same-size
#: rewrite with mtime restored inside the tick keeps the whole signature
RACY_CTIME_NS = 1_000_000_000


def _racy(stat, now_ns: int) -> bool:
    """A journal changed less than RACY_CTIME_NS before `now_ns` is not cached
    ("racy git"): a write in the same clock tick would leave its signature
    intact. A ctime after `now_ns` (written since, or clock stepped back) is
    racy too."""
    return now_ns - stat.st_ctime_ns < RACY_CTIME_NS


#: #1132 : tolérance entre le ts du marqueur d'un tour et le `resetsAt` d'une
#: fenêtre qu'il publie. Au-delà, la fenêtre était déjà close quand le tour a
#: commencé : le harnais a rejoué une mesure périmée (incident du 13/10, la
#: five_hour du relevé figé était échue de ~24 h sous un marqueur récent).
REPLAY_TOLERANCE_S = 60.0


def _marker_ts(event: dict) -> float | None:
    """Le ts d'un marqueur de compte, ou None s'il est absent/illisible."""
    try:
        value = float(event.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _reading_rank(found) -> tuple:
    """#1132 : classe une mesure par son instant de PRODUCTION.

    Une mesure non datée (flux sans marqueur) ne date pas le fichier : l'heure
    du fichier ne sert qu'à départager deux mesures non datées entre elles,
    jamais à battre une mesure datée (`(False, mtime) < (True, ts)`).
    """
    stamp, produced = found[0], found[1]
    return (produced is not None, produced if produced is not None else stamp)


def _stamp_gauges(gauges: list, produced: float | None, agent: str,
                  overage: Overage | None) -> Gauges:
    """#1132 : chaque fenêtre porte sa provenance (production, journal).

    `read_at` (#1561) garde l'horodatage du relevé posé par `_claude_windows` ;
    `produced_at` (#1132) est la provenance que lisent l'historique partagé
    (0434) et la réconciliation.
    """
    return Gauges([replace(gauge, produced_at=produced, source=agent or "")
                   for gauge in gauges], overage)


def _replayed(gauges: list, produced: float | None) -> bool:
    """#1132 : une fenêtre déjà close avant le tour qui la publie est un rejeu.

    Un `rate_limit_event` ne porte pas d'instant ; le ts du marqueur de compte
    borne le début du tour. Si le `resetsAt` d'une fenêtre publiée est
    antérieur à cette borne (tolérance `REPLAY_TOLERANCE_S`, même horloge), le
    harnais a rejoué une mesure d'un tour antérieur : elle ne doit jamais
    remplacer une mesure fraîche.
    """
    if produced is None:
        return False
    return any(gauge.resets_at is not None
               and float(gauge.resets_at) < produced - REPLAY_TOLERANCE_S
               for gauge in gauges)


def _last_claude_reading(events: list, agent: str, mtime: float):
    """Le MEILLEUR `rate_limit_event` d'un flux, daté par son marqueur (#1132).

    Rend `(estampille, production, Gauges)` : `production` est le ts du dernier
    marqueur de compte vu avant l'événement (None pour un flux sans marqueur ou
    pour un rejeu), `estampille` l'heure du fichier quand il n'y a pas de
    marqueur — elle ne sert qu'à départager deux mesures non datées. Les
    mesures du flux sont classées par `_reading_rank`, jamais par leur ordre de
    lecture : un rejeu publié APRÈS une mesure fraîche du même journal ne
    masque plus celle-ci.
    """
    produced, best = None, None
    for event in events:
        if _is_claude_marker(event):
            produced = _marker_ts(event)
        elif event.get("type") == "rate_limit_event":
            stamp = produced if produced is not None else mtime
            gauges = _claude_windows(event, stamp)
            if gauges is None:
                continue
            at = produced
            if _replayed(gauges, at):
                at = None                 # rejeu : jamais daté
            found = (stamp, at,
                     _stamp_gauges(gauges, at, agent, _claude_overage(event)))
            if best is None or _reading_rank(found) >= _reading_rank(best):
                best = found
    return best


def _claude_event_date(event: dict) -> float:
    """La date que l'événement porte lui-même, 0.0 s'il n'en porte pas (#1720).

    Un `rate_limit_event` Claude réel n'a ni `ts` ni `timestamp` : un flux sans
    marqueur reste donc **non daté**, au lieu d'être daté de la mtime du journal
    (dernière écriture de n'importe quel événement), qui le faisait passer pour
    plus frais qu'un widget plus récent (#1648).
    """
    for key in ("ts", "timestamp"):
        date = _epoch(event.get(key))
        if date:
            return date
    return 0.0


def _claude_account_readings(events: list, agent: str = "") -> dict:
    """Attribute all accounts in one pass; None is a verified legacy stream.

    Each reading is (read_at, produced, Gauges): the overage flags stay on the
    gauges (L226b, #1649) and the production instant (#1132) is the marker ts
    of the publishing turn. `read_at` (#1720) est le ts du marqueur pour un flux
    marqué, la date de l'événement pour un flux sans marqueur, 0.0 (non daté)
    quand il n'en porte pas — jamais la mtime du fichier.

    #1132 : chaque mesure porte sa provenance. `produced_at` est le ts du
    marqueur du tour qui l'a publiée, jamais le mtime du fichier. Une mesure
    dont une fenêtre était déjà close au début du tour est un rejeu : elle est
    rendue NON datée, pour ne pas remplacer une mesure fraîche. Un rejeu publié
    après une mesure fraîche du même compte ne la remplace pas non plus : le
    compte garde la meilleure mesure du flux (`_reading_rank`).

    #1720 : un flux sans marqueur ne se date plus de la mtime du journal
    (dernière écriture de n'importe quel événement), qui le faisait passer pour
    plus frais qu'un widget plus récent (#1648) ; il se date de l'événement
    lui-même (`_claude_event_date`), sinon il reste non daté.
    """
    marked = any(_is_claude_marker(e) for e in events)
    current, stamp, produced = None, 0.0, None
    readings = {}
    for event in events:
        if _is_claude_marker(event):
            current = str(event.get("account") or "") or None
            produced = _marker_ts(event)
            stamp = produced if produced is not None else 0.0
        elif event.get("type") == "rate_limit_event" and (current is not None or not marked):
            read_at = stamp if marked else _claude_event_date(event)
            gauges = _claude_windows(event, read_at)
            if gauges is None:
                continue
            at = produced
            if _replayed(gauges, at):
                at = None             # rejeu : jamais daté
            # #1661 : `_stamp_gauges` recopie chaque fenêtre (`replace`) et rend
            # un `Gauges` neuf : le cache ne partage jamais la liste mutable du
            # producteur, et l'overage reste porté par les `Gauges` (#1649).
            found = (read_at, at, _stamp_gauges(gauges, at, agent,
                                                _claude_overage(event)))
            old = readings.get(current)
            if old is None or _reading_rank(found) >= _reading_rank(old):
                readings[current] = found
    return readings


def _claude_account_tail(path: str) -> tuple[list, os.stat_result, bool]:
    """Read a snapshot tail and recover a preceding marker only if needed.

    A visible marker before every tail reading makes prefix recovery redundant.
    The final flag allows caching only complete, stable snapshots. Leading
    readings in a marked stream without a preceding marker stay unknown.
    """
    with open(path, "rb") as fh:
        stat = os.fstat(fh.fileno())
        start = max(0, stat.st_size - TAIL_BYTES)
        if start:
            # Keep complete lines only; the prefix scan includes a marker that
            # straddles the raw tail boundary.
            fh.seek(start - 1)
            if fh.read(1) != b"\n":
                fh.readline(stat.st_size - start)
                start = fh.tell()
        fh.seek(start)
        raw = fh.read(stat.st_size - start)
        if len(raw) != stat.st_size - start:
            raise OSError("Account marker tail changed during read")
        events = []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        need_prefix = False
        for event in events:
            if _is_claude_marker(event):
                break
            if event.get("type") == "rate_limit_event":
                need_prefix = True
                break
        complete = True
        if start and need_prefix:
            try:
                marker = _last_claude_marker(fh, start)
            except OSError:
                # Preserve later explicit markers, but ignore the unknown turn.
                marker = {"type": ACCOUNT_MARKER, "harness": "claude"}
                complete = False
            if marker is not None:
                events.insert(0, marker)
        if _journal_signature(os.fstat(fh.fileno())) != _journal_signature(stat):
            raise OSError("Account marker snapshot changed during read")
        return events, stat, complete


def _num(usage: dict, key: str) -> int:
    value = usage.get(key) if isinstance(usage, dict) else None
    return int(value) if isinstance(value, (int, float)) else 0


def _amount(value) -> float | None:
    """Un montant USD exploitable, ou None (un booléen n'est pas un montant)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _sum(usages: Iterable[dict], key: str) -> int:
    total = 0
    for usage in usages:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def _fraction(value, percent: bool = False) -> float:
    """Une utilisation en 0..1, que le harnais publie 0..1 ou 0..100."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if percent or number > 1.0:
        number = number / 100.0
    return min(max(number, 0.0), 1.0)


def _epoch(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
