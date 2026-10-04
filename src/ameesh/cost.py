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
    `~/.codex/sessions/**/*.jsonl`.

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
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from . import config as config_mod

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

#: Marge de rythme : on ne vise pas 100 % d'un forfait, mais la part écoulée + 10.
PACE_MARGIN = 0.10
PACE_CEILING = 0.90

#: Bornes de lecture des journaux : on ne charge jamais un fichier entier, les
#: sessions des harnais grossissent sans fin.
TAIL_BYTES = 4_000_000
CODEX_FILES = 5


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
                    if isinstance(value, (list, tuple)) and len(value) == 3:
                        prices[str(key)] = [float(v) for v in value]
        except (OSError, ValueError):
            pass
    return prices


def price_of(prices: dict, harness: str, model: str | None) -> list:
    """Le triplet de prix d'un modèle, avec le repli par harnais."""
    if model and model in prices:
        return prices[model]
    return prices.get("default-%s" % harness) or prices["default-deepseek"]


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
        self.codex_sessions = codex_sessions if codex_sessions is not None else os.path.expanduser(
            "~/.codex/sessions")
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
    def claude_gauges(self) -> list:
        """Le dernier `rate_limit_event` vu par un agent Claude.

        Le forfait est partagé par tout le compte : la jauge la plus récente de
        n'importe quel agent Claude vaut pour les autres.
        """
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
                info = line.get("rate_limit_info") or {}
                windows = info.get("unifiedWindows") or {}
                if not isinstance(windows, dict):
                    break
                newest = (mtime, [
                    Gauge("claude", key, _fraction(value.get("utilization")),
                          _epoch(value.get("resetsAt")),
                          float(KNOWN_WINDOWS.get(key, KNOWN_WINDOWS["seven_day"])))
                    for key, value in windows.items() if isinstance(value, dict)
                ])
                break
        return newest[1] if newest else []

    def codex_gauges(self) -> list:
        """Les `rate_limits` primaire et secondaire du journal de session le plus récent.

        Lecture **JSON structurée**, pas une expression régulière : l'ordre des clés
        n'est pas garanti (`plan_type` peut précéder `primary`), `secondary` peut
        être `null`, et une regex y perdait la jauge — donc la pause (sonde codex2).
        """
        files = sorted(
            glob.glob(os.path.join(self.codex_sessions, "**", "*.jsonl"), recursive=True),
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
        out = []
        if harness in (None, "claude"):
            out += self.claude_gauges()
        if harness in (None, "codex"):
            out += self.codex_gauges()
        return out

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
        columns = ("session, cum_usd, cum_input_tokens, cum_cached_input_tokens,"
                   " cum_output_tokens")
        where = ("where agent = %s and harness = %s"
                 " and (cum_usd is not null or cum_input_tokens is not null)")
        rows = self.db.query(
            "select " + columns + " from turn_costs " + where +
            " order by recorded_at desc, id desc limit 1", (agent, harness))
        if session and rows and rows[0]["session"] != session:
            own = self.db.query(
                "select " + columns + " from turn_costs " + where + " and session = %s"
                " order by recorded_at desc, id desc limit 1", (agent, harness, session))
            if own:
                rows = own
        if not rows:
            return {}
        row = rows[0]
        return {
            "session": row["session"],
            "cum_usd": float(row["cum_usd"]) if row["cum_usd"] is not None else None,
            "cum_usage": [int(row["cum_input_tokens"] or 0),
                          int(row["cum_cached_input_tokens"] or 0),
                          int(row["cum_output_tokens"] or 0)] if row["cum_input_tokens"] is not None else None,
        }


    def turn_usage(self, agent: str, start: int = 0) -> TurnUsage:
        """Ce que le tour qui vient de finir a coûté, sans rien écrire.

        Le repère de cumul (dernier `total_cost_usd` Claude, dernier usage Codex)
        est lu dans le grand livre : la différence se calcule donc entre deux
        tours, pas entre deux lectures d'un même tour — et un tour sans relevé ne
        l'efface pas.
        """
        harness = self.tool_of(agent)
        model = self.model_of(agent)
        prices = load_prices(self.prices_path)
        events = self.events(agent, start)
        # La session se lit AVANT le repère : c'est elle qui dit lequel comparer.
        cum = self.baseline(agent, harness, _last_session(events) or None)

        if harness == "claude":
            totals = [float(e["total_cost_usd"]) for e in events
                      if e.get("type") == "result" and isinstance(e.get("total_cost_usd"), (int, float))]
            usage = [e.get("usage") or {} for e in events if e.get("type") == "result"]
            session = _last_session(events) or cum.get("session")
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
                agent, harness, model or "défaut", max(usd, 0.0),
                input_tokens=_sum(usage, "input_tokens"),
                cached_input_tokens=_sum(usage, "cache_read_input_tokens"),
                output_tokens=_sum(usage, "output_tokens"),
            )

        if harness == "codex":
            completed = [e.get("usage") or {} for e in events if e.get("type") == "turn.completed"]
            if not completed:
                return TurnUsage(agent, harness, model or "défaut", 0.0)
            last = completed[-1]
            current = [int(last.get("input_tokens", 0)), int(last.get("cached_input_tokens", 0)),
                       int(last.get("output_tokens", 0))]
            previous = cum.get("cum_usage")
            session = _last_session(events) or cum.get("session")
            if session == cum.get("session") and isinstance(previous, list) and len(previous) == 3 \
                    and all(c >= p for c, p in zip(current, previous)):
                # Même fil : `turn.completed` porte l'usage **cumulé** du fil.
                delta = [c - p for c, p in zip(current, previous)]
            elif previous is None or session != cum.get("session"):
                # Fil neuf : le cumul du fil EST le tour.
                delta = current
            else:
                delta = [0, 0, 0]
            pin, pcache, pout = price_of(prices, harness, model)
            uncached = max(delta[0] - delta[1], 0)
            usd = (uncached * pin + delta[1] * pcache + delta[2] * pout) / 1e6
            return TurnUsage(agent, harness, model or "défaut", usd,
                             input_tokens=delta[0], cached_input_tokens=delta[1],
                             output_tokens=delta[2])

        # DeepSeek (et tout harnais qui publie un usage par étape).
        steps = [e.get("usage") or {} for e in events
                 if e.get("type") == "status" and e.get("phase") == "step_end"]
        tin = _sum(steps, "inputTokens")
        tcache = _sum(steps, "cacheReadTokens")
        tout = _sum(steps, "outputTokens")
        pin, pcache, pout = price_of(prices, harness or "deepseek", model)
        usd = (tin * pin + tcache * pcache + tout * pout) / 1e6
        return TurnUsage(agent, harness or "deepseek", model or "défaut", usd,
                         input_tokens=tin, cached_input_tokens=tcache, output_tokens=tout)

    def record(self, agent: str, start: int = 0, turn: str | None = None,
               session: str | None = None) -> TurnUsage:
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
        usage = self.turn_usage(agent, start)
        session_id = session or _last_session(events) or None
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
        self.db.execute(
            "insert into turn_costs (agent, harness, turn, model, session, usd,"
            " input_tokens, cached_input_tokens, output_tokens,"
            " cum_usd, cum_input_tokens, cum_cached_input_tokens, cum_output_tokens)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (row["agent"], row["harness"], row["turn"], row["model"], row["session"],
             row["usd"], row["input_tokens"], row["cached_input_tokens"],
             row["output_tokens"], cum_usd, cum_input, cum_cached, cum_out))
        return usage

    # -- dépense ------------------------------------------------------------
    def spent(self, agent: str = "all", seconds: float = 3600.0) -> float:
        """La dépense des `seconds` dernières secondes, lue sur `turn_costs`."""
        if self.db is None:
            return 0.0
        window = "recorded_at >= now() - make_interval(secs => %s)"
        if agent == "all":
            rows = self.db.query(
                "select coalesce(sum(usd), 0) as usd from turn_costs where " + window,
                (float(seconds),))
        else:
            rows = self.db.query(
                "select coalesce(sum(usd), 0) as usd from turn_costs"
                " where agent = %s and " + window,
                (agent, float(seconds)))
        return float(rows[0]["usd"]) if rows else 0.0

    def over(self, agent: str = "all") -> str:
        """Le garde-fou : raison si l'agent (ou le compte) doit s'arrêter, sinon ''."""
        harness = self.tool_of(agent) if agent != "all" else ""
        reason = self.pace_exceeded(harness) if harness else ""
        if reason:
            return reason
        hourly = self.spent("all", 3600)
        if hourly >= self.hourly_usd:
            return ("budget horaire : %.2f $ dépensés sur les 60 dernières minutes "
                    "(plafond %.2f $)" % (hourly, self.hourly_usd))
        return ""

    # -- rapport ------------------------------------------------------------
    def report(self) -> list:
        """Une ligne par agent : harnais, modèle, dépense 1 h/24 h, jauges."""
        rows = []
        now = self.clock()
        # Le forfait est partagé par tout le compte : les jauges se lisent une fois
        # par harnais, pas une fois par agent.
        by_harness: dict = {}
        for agent in self.agents():
            harness = self.tool_of(agent) or "?"
            if harness in ("claude", "codex") and harness not in by_harness:
                by_harness[harness] = self.gauges(harness)
            rows.append({
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
            row["agent"], row["harness"], row["model"],
            row["spent_1h"], row["spent_24h"], gauges))
    return "\n".join(lines)


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
