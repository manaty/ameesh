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
  * DeepSeek : usage par étape (`status`/`step_end`) × barème.

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

import glob
import json
import os
import re
import math
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence

from . import config as config_mod
from . import harnesses
from . import storage

#: Barème par défaut du poste de travail (reprise de l'outil de comptage
#: existant). USD par million de jetons : [entrée, entrée en cache, sortie].
DEFAULT_PRICES = {
    "deepseek-flash": [0.27, 0.07, 1.10],
    "deepseek-pro": [0.55, 0.14, 2.19],
    "gpt-6.1-sol": [1.25, 0.125, 10.0],
    "default-codex": [1.25, 0.125, 10.0],
    "default-deepseek": [0.27, 0.07, 1.10],
}

#: Fenêtres nommées par les jauges, en secondes.
KNOWN_WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}

#: Plafond horaire de l'usage payé au jeton, pour l'ensemble des agents
#: (décision 0019 §2 : référence 10 USD/h).
DEFAULT_HOURLY_USD = 10.0


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
        raise CostError("aucun descripteur de harnais lisible : le plafond horaire "
                        "payé au token ne peut pas être appliqué")
    return tuple(sorted(ident for ident, d in descriptors.items() if d.paid_per_token))

#: Marge de rythme : on ne vise pas 100 % d'un forfait, mais la part écoulée + 10.
PACE_MARGIN = 0.10
PACE_CEILING = 0.90

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

    def elapsed(self, now: float) -> float:
        """Part écoulée de la fenêtre, 1.0 quand on ne sait pas la dater."""
        if not self.resets_at:
            return 1.0
        return min(max(1.0 - (self.resets_at - now) / self.window_s, 0.0), 1.0)

    def pace_cap(self, now: float) -> float:
        """Le plafond de rythme : min(90 %, part écoulée + 10 points)."""
        return min(PACE_CEILING, self.elapsed(now) + PACE_MARGIN)

    def exceeded(self, now: float) -> bool:
        return self.used >= self.pace_cap(now)

    def reset_passed(self, now: float) -> bool:
        """La fenêtre publiée est-elle déjà remise à zéro (`resets_at` passé) ?

        Le relevé est alors périmé : la fenêtre suivante est vierge tant qu'aucun
        tour n'a publié de nouveau relevé (L30, retour au primaire).
        """
        return bool(self.resets_at) and now >= float(self.resets_at)


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


def price_of(prices: dict, harness: str, model: str | None) -> list:
    """Le triplet de prix d'un modèle, avec le repli par harnais."""
    if model and model in prices:
        return prices[model]
    return prices.get("default-%s" % harness) or prices["default-deepseek"]


def max_price_of(prices: dict, harness: str) -> list:
    """Un **majorant sûr** du tarif d'un modèle inconnu de la famille du harnais.

    Le maximum se prend **composante par composante** — entrée, entrée en cache,
    sortie — sur tous les tarifs connus de la famille. Le triplet obtenu coûte,
    pour n'importe quel mélange de jetons, **au moins autant que n'importe quel
    modèle connu** : un barème où le modèle le plus cher en entrée est le moins
    cher en sortie (`flash [10,1,1]`, `pro [5,1,100]`) donne `[10,1,100]`, jamais
    `flash` seul (B6 : on ne comparait que l'entrée, et un modèle inconnu qui
    produit 1 M de jetons de sortie était facturé 1 $ au lieu de 100 $).

    Un modèle inconnu (ou vide) ne doit jamais être facturé au tarif par défaut,
    qui est le moins cher (0019 §3, repli fail-closed). Si la famille n'a aucun
    tarif connu, on prend le maximum composante par composante de **tout** le
    barème utilisable plutôt que le défaut d'un harnais.
    """
    prefixes = PRICE_FAMILY.get(harness, ())

    def connus(retenir) -> list:
        return [triplet for key, value in prices.items()
                if not key.startswith("default-") and retenir(key)
                for triplet in [_valid_triplet(value)] if triplet is not None]

    candidats = connus(lambda key: any(key.startswith(prefixe) for prefixe in prefixes))
    if not candidats:
        candidats = connus(lambda key: True)
    if not candidats:
        return list(prices.get("default-%s" % harness) or prices["default-deepseek"])
    return [max(triplet[i] for triplet in candidats) for i in range(3)]


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

    def tool_of(self, agent: str) -> str:
        """Le harnais d'un agent : injecté, sinon l'état local (`<agent>/tool`)."""
        if agent in self.tools:
            return self.tools[agent]
        try:
            with open(os.path.join(self.agent_dir(agent), "tool"), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def model_of(self, agent: str) -> str:
        try:
            with open(os.path.join(self.agent_dir(agent), "model"), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def events(self, agent: str, start: int = 0, tail: bool = False) -> list:
        """Les événements JSONL d'un agent, à partir de la ligne `start`."""
        path = os.path.join(self.agent_dir(agent), "events.jsonl")
        raw = _read_tail(path) if tail else _read_all(path)
        out = []
        for index, line in enumerate(raw.splitlines()):
            if index < start:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Un `[]` ou un `null` est une ligne JSON valide mais pas un événement :
            # la garder ferait planter le premier `.get` venu (sonde codex2).
            if isinstance(event, dict):
                out.append(event)
        return out

    # -- jauges de forfait --------------------------------------------------
    def claude_gauges(self, account: str | None = None, primary: str | None = None) -> list:
        """Le dernier `rate_limit_event` vu par un agent Claude.

        Le forfait est partagé par tout le compte : la jauge la plus récente de
        n'importe quel agent Claude vaut pour les autres.

        `account` (L30) : seulement les événements de ce compte, reconnus par le
        marqueur qui précède chaque tour ; un événement sans marqueur avant lui
        appartient au compte `primary`.
        """
        if account is not None:
            return self._claude_gauges_of(account, primary or account)
        newest = None
        for agent in self.agents():
            if self.tool_of(agent) != "claude":
                continue
            path = os.path.join(self.agent_dir(agent), "events.jsonl")
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if newest is not None and mtime <= newest[0]:
                continue
            for line in reversed(self.events(agent, tail=True)):
                if line.get("type") != "rate_limit_event":
                    continue
                gauges = _claude_windows(line)
                if gauges is None:
                    break
                newest = (mtime, gauges)
                break
        return newest[1] if newest else []

    def _claude_gauges_of(self, account: str, primary: str) -> list:
        """Les jauges Claude d'UN compte (L30) : le dernier relevé de ce compte.

        Chaque tour est précédé d'un marqueur daté ; le relevé le plus récent est
        celui du tour au marqueur le plus récent, tous agents confondus. Dans un
        flux sans aucun marqueur (agent d'avant L30), les relevés sont du compte
        primaire et datés par le fichier.
        """
        best = None
        for agent in self.agents():
            if self.tool_of(agent) != "claude":
                continue
            path = os.path.join(self.agent_dir(agent), "events.jsonl")
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            events = self.events(agent, tail=True)
            marked = any(e.get("type") == ACCOUNT_MARKER for e in events)
            current, stamp = primary, (0.0 if marked else mtime)
            found = None
            for event in events:
                kind = event.get("type")
                if kind == ACCOUNT_MARKER:
                    if event.get("harness") not in (None, "", "claude"):
                        continue
                    current = str(event.get("account") or primary)
                    try:
                        stamp = float(event.get("ts") or 0.0)
                    except (TypeError, ValueError):
                        stamp = 0.0
                    continue
                if kind != "rate_limit_event" or current != account:
                    continue
                gauges = _claude_windows(event)
                if gauges is not None:
                    found = (stamp, gauges)
            if found and (best is None or found[0] >= best[0]):
                best = found
        return best[1] if best else []

    def codex_gauges(self, sessions: str | None = None) -> list:
        """Les `rate_limits` primaire et secondaire du journal de session le plus récent.

        Lecture **JSON structurée**, pas une expression régulière : l'ordre des clés
        n'est pas garanti (`plan_type` peut précéder `primary`), `secondary` peut
        être `null`, et une regex y perdait la jauge — donc la pause (sonde codex2).

        `sessions` (L30) : le dossier de sessions d'un compte
        (`<CODEX_HOME>/sessions`) ; par défaut celui de la machine.
        """
        racine = sessions if sessions is not None else self.codex_sessions
        files = sorted(
            glob.glob(os.path.join(racine, "**", "*.jsonl"), recursive=True),
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
        )[-CODEX_FILES:]
        for path in reversed(files):
            limits = None
            for line in _read_tail(path, 2_000_000).splitlines():
                line = line.strip()
                if not line or "rate_limits" not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                found = _find_rate_limits(event)
                if found:
                    limits = found          # le dernier de la session gagne
            if not limits:
                continue
            gauges = []
            for key in ("primary", "secondary"):
                value = limits.get(key)
                if not isinstance(value, dict):
                    continue                    # `secondary: null` n'est pas une jauge
                minutes = float(value.get("window_minutes") or 0)
                gauges.append(Gauge(
                    "codex",
                    "codex-%dmin" % int(minutes or 0),
                    _fraction(value.get("used_percent"), percent=True),
                    _epoch(value.get("resets_at")),
                    minutes * 60 if minutes else float(KNOWN_WINDOWS["seven_day"]),
                ))
            if gauges:
                return gauges
        return []

    def gauges(self, harness: str | None = None) -> list:
        """Les jauges de forfait, par **source déclarée** dans les descripteurs (L16).

        `ameesh.cost.gauges` nomme le lecteur (`transcript` pour Claude,
        `sessions` pour Codex) : le code ne connaît que des formats de journaux,
        un harnais nouveau se décrit. Un harnais sans source de jauges n'en a
        pas (le solde des fournisseurs payés au token est lu ailleurs, L26).
        """
        out: list = []
        for ident, descriptor in harnesses.scan()[0].items():
            if harness not in (None, ident):
                continue
            lecteur = {"transcript": self.claude_gauges,
                       "sessions": self.codex_gauges}.get(
                           str(descriptor.cost.get("gauges") or ""))
            if lecteur is not None:
                out += [replace(gauge, harness=ident) for gauge in lecteur()]
        self.record_gauges(out)
        return out

    def record_gauges(self, gauges: Sequence[Gauge], account: str | None = None) -> int:
        """Historique des jauges (L26, migration 0027), là où elles sont lues.

        Une ligne seulement quand la jauge change (ou toutes les dix minutes) ;
        sans base, rien. **Ne lève jamais** : l'historique ne doit pas casser
        la garde de budget qui lit les jauges avant chaque tour.

        `account` (L30, migration 0028) : les jauges sont celles de ce compte.
        """
        if self.db is None or not gauges:
            return 0
        try:
            lignes = []
            for g in gauges:
                ligne = {"harness": g.harness, "key": g.key, "used": g.used,
                         "resets_at": g.resets_at, "window_s": g.window_s}
                if account is not None:
                    ligne["account"] = account
                lignes.append(ligne)
            return storage.of(self.db).operations.record_gauges(lignes)
        except Exception:  # historique facultatif : jamais fatal
            return 0

    def pace_exceeded(self, harness: str) -> str:
        """Le forfait d'un harnais avance-t-il trop vite ? La raison, ou ''.

        Le seuil est celui de la décision 0019 §2 :
        `utilisation >= min(90 %, part écoulée de la fenêtre + 10 points)`.
        """
        now = self.clock()
        for gauge in self.gauges(harness):
            if gauge.exceeded(now):
                return ("forfait %s %s : %.0f%% utilisé pour %.0f%% de la fenêtre "
                        "écoulée (plafond de rythme %.0f%%)" % (
                            harness, gauge.key, gauge.used * 100,
                            gauge.elapsed(now) * 100, gauge.pace_cap(now) * 100))
        return ""

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


    def session_of(self, agent: str) -> str | None:
        """La session (ou le fil) courante d'un agent, lue sur **tout** son flux.

        Pas sur la tranche du dernier tour : un `turn.completed` peut arriver sans
        nouveau `thread.started`, et la session se perdrait alors sur la ligne —
        le relevé suivant, comparé au mauvais repère, compterait un tour de trop
        (sonde codex2, B5). La session se résout donc en amont et se transmet.
        """
        return _last_session(self.events(agent)) or None

    def turn_usage(self, agent: str, start: int = 0, session: str | None = None,
                   model: str | None = None) -> TurnUsage:
        """Ce que le tour qui vient de finir a coûté, sans rien écrire.

        Le repère de cumul (dernier `total_cost_usd` Claude, dernier usage Codex)
        est lu dans le grand livre : la différence se calcule donc entre deux
        tours, pas entre deux lectures d'un même tour — et un tour sans relevé ne
        l'efface pas.
        """
        harness = self.tool_of(agent)
        # Le modèle **effectif du lancement** est passé par l'exécuteur ; le
        # relire ici prendrait un fichier d'état mutable (verdicts codex3 L13 B4).
        # `None` = paramètre absent (repli historique sur l'état local) ;
        # `""` = aucun modèle annoncé/figé : on facture le **plus cher connu** de
        # la famille, jamais le défaut (mesh-design, 0019 §3, fail-closed).
        if model is None:
            model = self.model_of(agent)
        prices = load_prices(self.prices_path)
        if model and model in prices:
            pin, pcache, pout = prices[model]
            modele = model
        else:
            pin, pcache, pout = max_price_of(prices, harness or "deepseek")
            modele = model or "inconnu"
        events = self.events(agent, start)
        # La session se résout AVANT le repère : c'est elle qui dit lequel comparer.
        session = session or self.session_of(agent)
        cum = self.baseline(agent, harness, session)

        if harness == "claude":
            totals = [float(e["total_cost_usd"]) for e in events
                      if e.get("type") == "result" and isinstance(e.get("total_cost_usd"), (int, float))]
            usage = [e.get("usage") or {} for e in events if e.get("type") == "result"]
            last = cum.get("cum_usd")
            if not totals:
                usd = 0.0
            elif last is None or session != cum.get("session"):
                # Session neuve (ou inconnue) : son `total_cost_usd` cumulé part de
                # zéro, donc le dernier relevé EST le coût du tour. Le soustraire
                # d'un repère d'une autre session compterait faux.
                usd = float(totals[-1])
            else:
                usd = float(totals[-1]) - float(last)
                if usd < 0:  # session reprise : le cumul repart de plus bas
                    usd = float(totals[-1])
            return TurnUsage(
                agent, harness, modele, max(usd, 0.0),
                input_tokens=_sum(usage, "input_tokens"),
                cached_input_tokens=_sum(usage, "cache_read_input_tokens"),
                output_tokens=_sum(usage, "output_tokens"),
            )

        if harness == "codex":
            completed = [e.get("usage") or {} for e in events if e.get("type") == "turn.completed"]
            if not completed:
                return TurnUsage(agent, harness, modele, 0.0)
            last = completed[-1]
            current = [int(last.get("input_tokens", 0)), int(last.get("cached_input_tokens", 0)),
                       int(last.get("output_tokens", 0))]
            previous = cum.get("cum_usage")
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
                             output_tokens=delta[2])

        # DeepSeek (et tout harnais qui publie un usage par étape).
        steps = [e.get("usage") or {} for e in events
                 if e.get("type") == "status" and e.get("phase") == "step_end"]
        tin = _sum(steps, "inputTokens")
        tcache = _sum(steps, "cacheReadTokens")
        tout = _sum(steps, "outputTokens")
        usd = (tin * pin + tcache * pcache + tout * pout) / 1e6
        return TurnUsage(agent, harness or "deepseek", modele, usd,
                         input_tokens=tin, cached_input_tokens=tcache, output_tokens=tout)

    def record(self, agent: str, start: int = 0, turn: str | None = None,
               session: str | None = None, model: str | None = None) -> TurnUsage:
        """Compte le tour qui vient de finir : **une** ligne `turn_costs`.

        La ligne porte le cumul brut du harnais, qui sert de repère au tour
        suivant : insertion et repère sont la même écriture. Si l'insertion
        échoue, rien n'a bougé et un tour rejoué retrouve le même delta.

        La base est donc obligatoire ici : sans elle, il n'y a pas de grand livre,
        et un repère en fichier serait exactement le défaut que ce lot corrige.
        """
        if self.db is None:
            raise CostError("record sans base : le repère de cumul vit dans turn_costs")
        events = self.events(agent, start)
        # **Résolue une fois**, sur tout le flux, puis transmise au calcul comme à
        # l'insertion : la ligne et le repère doivent porter la même session.
        session_id = session or self.session_of(agent)
        usage = self.turn_usage(agent, start, session=session_id, model=model)
        # L30 : le compte du tour est celui du dernier marqueur AVANT le tour.
        account = self.account_at(agent, start, usage.harness)
        cum_usd = cum_input = cum_cached = cum_out = None
        if usage.harness == "claude":
            totals = [float(e["total_cost_usd"]) for e in events
                      if e.get("type") == "result" and isinstance(e.get("total_cost_usd"), (int, float))]
            if totals:
                cum_usd = totals[-1]
        elif usage.harness == "codex":
            completed = [e.get("usage") or {} for e in events if e.get("type") == "turn.completed"]
            if completed:
                last = completed[-1]
                cum_input = int(last.get("input_tokens", 0))
                cum_cached = int(last.get("cached_input_tokens", 0))
                cum_out = int(last.get("output_tokens", 0))
        row = usage.row()
        row["turn"] = turn or usage.turn
        row["session"] = session_id
        storage.of(self.db).turn_costs.insert(
            agent=row["agent"], harness=row["harness"], turn=row["turn"],
            model=row["model"], session=row["session"], usd=row["usd"],
            input_tokens=row["input_tokens"], cached_input_tokens=row["cached_input_tokens"],
            output_tokens=row["output_tokens"], cum_usd=cum_usd, cum_input_tokens=cum_input,
            cum_cached_input_tokens=cum_cached, cum_output_tokens=cum_out,
            account=account)
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

    def over(self, agent: str = "all", *,
             paid_harnesses: Sequence[str] | None = None, pace: bool = True) -> str:
        """Le garde-fou : raison si l'agent (ou le compte) doit s'arrêter, sinon ''.

        Le plafond horaire ne somme que l'usage payé au token
        (`paid_harnesses`, défaut `paid_harnesses_of()`, lu dans les descripteurs) ;
        les forfaits sont couverts par `pace_exceeded`, pas par cette somme
        (0019 §2).

        `pace=False` (L30) : le rythme est jugé compte par compte par
        `ameesh.accounts`, qui bascule au lieu de mettre en pause ; seul le
        plafond horaire reste ici.
        """
        harness = self.tool_of(agent) if agent != "all" else ""
        reason = self.pace_exceeded(harness) if (harness and pace) else ""
        if reason:
            return reason
        paid = paid_harnesses_of() if paid_harnesses is None else tuple(paid_harnesses)
        hourly = self.spent("all", 3600, harnesses=paid)
        if hourly >= self.hourly_usd:
            return ("budget horaire (payé au token) : %.2f $ sur les 60 dernières minutes "
                    "(plafond %.2f $)" % (hourly, self.hourly_usd))
        return ""

    # -- rapport ------------------------------------------------------------
    def report(self, accounts: dict | None = None) -> list:
        """Une ligne par agent : harnais, modèle, dépense 1 h/24 h, jauges.

        `accounts` (L30) : `{harnais: (compte actif, [Gauge…])}` pour les
        harnais à comptes déclarés ; leurs jauges sont alors celles du compte
        actif, et la ligne nomme ce compte (`account`).
        """
        rows = []
        now = self.clock()
        accounts = accounts or {}
        # Le forfait est partagé par tout le compte : les jauges se lisent une fois
        # par harnais, pas une fois par agent.
        by_harness: dict = {name: list(value[1]) for name, value in accounts.items()}
        for agent in self.agents():
            harness = self.tool_of(agent) or "?"
            # Les forfaits à jauges sont déclarés par les descripteurs (L16) :
            # la jauge se lit une fois par harnais, jamais par agent.
            if harness not in by_harness and gauges_source(harness) in ("transcript",
                                                                        "sessions"):
                by_harness[harness] = self.gauges(harness)
            if harness in accounts:
                rows.append({"account": accounts[harness][0]})
            else:
                rows.append({})
            rows[-1].update({
                "agent": agent,
                "harness": harness,
                "model": self.model_of(agent) or "défaut",
                "spent_1h": self.spent(agent, 3600),
                "spent_24h": self.spent(agent, 86400),
                "gauges": [
                    {
                        "key": g.key,
                        "used": g.used,
                        "cap": g.pace_cap(now),
                        "elapsed": g.elapsed(now),
                        "resets_at": g.resets_at,
                    }
                    for g in by_harness.get(harness, [])
                ],
            })
        return rows


# --------------------------------------------------------------------------
# utiles
# --------------------------------------------------------------------------

def format_report(rows: Iterable[dict]) -> str:
    """Le tableau de `ameesh cost report`."""
    lines = ["%-16s %-9s %-16s %10s %10s  %s" % (
        "agent", "harnais", "modèle", "1 h (USD)", "24 h (USD)", "jauges")]
    for row in rows:
        gauges = ", ".join(
            "%s %.0f%% (rythme %.0f%%)" % (g["key"], g["used"] * 100, g["cap"] * 100)
            for g in row["gauges"]) or "—"
        lines.append("%-16s %-9s %-16s %10.4f %10.4f  %s" % (
            row["agent"],
            row["harness"] + ("/%s" % row["account"] if row.get("account") else ""),
            row["model"],
            row["spent_1h"], row["spent_24h"], gauges))
    return "\n".join(lines)


def _claude_windows(event: dict) -> list | None:
    """Les fenêtres d'un `rate_limit_event` Claude, ou None si illisibles."""
    info = event.get("rate_limit_info") or {}
    windows = info.get("unifiedWindows") or {} if isinstance(info, dict) else None
    if not isinstance(windows, dict):
        return None
    return [
        Gauge("claude", key, _fraction(value.get("utilization")),
              _epoch(value.get("resetsAt")),
              float(KNOWN_WINDOWS.get(key, KNOWN_WINDOWS["seven_day"])))
        for key, value in windows.items() if isinstance(value, dict)
    ]


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


def _last_session(events: Iterable[dict]) -> str:
    """L'identifiant de session/fil le plus récent du flux, s'il y en a un."""
    found = None
    for event in events:
        for key in ("session_id", "thread_id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                found = value
        session = event.get("session")
        if isinstance(session, str) and session:
            found = session
    return found or ""


def _read_all(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _read_tail(path: str, limit: int = TAIL_BYTES) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", "ignore")
    except OSError:
        return ""


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
