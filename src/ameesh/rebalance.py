# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 (point 2) — la marge des forfaits et les agents facturés au token.

Demande du propriétaire (2026-10-15 10:25) : « qui répartit les agents entre
comptes pour optimiser les coûts ? ». Le choix par tour (0034, #1580 point 1)
consomme d'abord ce qui sera perdu à la remise à zéro, mais rien ne dit
combien d'agents facturés au token (DeepSeek) pourraient passer sur un forfait
(Codex, Claude) qui a de la marge.

`proposal()` lit les jauges des comptes au forfait sous le seuil des
**non-critiques** (la réserve `RESERVE` reste aux agents critiques, L242b),
estime la part de fenêtre longue qu'un agent consomme (observée sur le
harnais, plancher `DEFAULT_AGENT_SHARE`), et rend le nombre d'agents
déplaçables — jamais plus que la marge mesurée ne porte. Une famille n'est
jamais vidée : au moins un agent actif y reste, pour qu'une relecture d'une
autre famille demeure possible (canon §2).

**Aucune bascule n'est faite ici** : c'est une proposition affichée
(`ameesh accounts list`), l'humain ou le pilote décide. La garde de budget
par tour reste seule juge de l'exécution.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import accounts
from . import cost as cost_mod

#: Part de la fenêtre longue qu'un agent consomme **au minimum** : repli quand
#: aucune observation n'est disponible, plancher de l'observation sinon. Cinq
#: points d'une semaine : une proposition prudente (moins d'agents annoncés).
DEFAULT_AGENT_SHARE = 0.05
#: Une fenêtre plus courte qu'un jour se remet à zéro trop souvent pour porter
#: une bascule durable : la capacité se lit sur la fenêtre longue (L74).
LONG_WINDOW_S = 86400.0


@dataclass(frozen=True)
class Offer:
    """Ce qu'un harnais au forfait peut porter, sur sa fenêtre longue."""

    harness: str
    free: float                      #: part disponible sous le seuil non-critique
    unit: float                      #: part consommée par un agent (observée/plancher)
    capacity: int                    #: agents que `free` porte (floor(free / unit))
    accounts: tuple = ()             #: comptes qui ont une marge mesurée


@dataclass(frozen=True)
class Proposal:
    """La proposition affichée : combien d'agents facturés au token déplacer."""

    movable: int = 0
    #: répartition `(harnais, nombre)` — un harnais au forfait à la fois
    moves: tuple = ()
    offers: tuple = ()
    candidates: int = 0              #: agents facturés au token, hors critiques
    kept_for_review: int = 0         #: familles qu'on ne vide pas (relecture croisée)
    reason: str = ""                 #: pourquoi aucune proposition n'est faite

    def text(self) -> str:
        """La ligne d'affichage (`ameesh accounts list`)."""
        if self.reason:
            return "forfait margin: %s" % self.reason
        if not self.movable:
            return ("forfait margin: no movable agent (%d candidate(s), reserve of "
                    "critical agents and reviewer families kept)"
                    % self.candidates)
        detail = ", ".join("%s %d" % (harness, count) for harness, count in self.moves)
        return ("forfait margin available: %d movable agent(s) (%s); reserve of "
                "critical agents and reviewer families kept" % (self.movable, detail))

    def as_dict(self) -> dict:
        return {
            "movable": self.movable,
            "moves": [{"harness": h, "agents": n} for h, n in self.moves],
            "candidates": self.candidates,
            "kept_for_review": self.kept_for_review,
            "offers": [
                {"harness": o.harness, "free": o.free, "unit": o.unit,
                 "capacity": o.capacity, "accounts": list(o.accounts)}
                for o in self.offers],
            "reason": self.reason,
        }


def live_fleet(db, now: float, host: str | None = None) -> dict:
    """`{agent: harnais}` des agents **sous bail** à `now` (R3, L37).

    Un agent arrêté, expiré ou `externe` n'est pas une bascule : le bail seul
    autorise à tourner. `host` restreint à un hôte (les comptes déclarés sont
    ceux d'un hôte). Base illisible : `{}` — aucune proposition plutôt qu'un
    compte faux.
    """
    from . import registry  # import local : garde le module d'affichage léger
    try:
        rows = registry.overview(db)
    except Exception:  # noqa: BLE001 - lecture facultative, jamais fatale à l'affichage
        return {}
    fleet = {}
    for row in rows:
        expires = row.get("lease_expires_ts")
        if not row.get("lease_owner") or expires is None:
            continue
        if host is not None and row.get("host") != host:
            continue
        try:
            alive = float(expires) > now
        except (TypeError, ValueError):
            alive = False
        if alive:
            fleet[str(row.get("name"))] = str(row.get("harness") or "")
    return fleet


def _offer(items: list, agents: list, now: float, book) -> Offer:
    """La marge d'un harnais au forfait : ses comptes, leur fenêtre longue."""
    free = 0.0
    used = 0.0
    measured = []
    for profile in items:
        # seuil des non-critiques : la réserve reste aux agents critiques (L242b)
        result = accounts.evaluate(book, profile, items, now, record=False, reserve=True)
        if result.problems or result.hard:
            continue
        long = [g for g in result.gauges if g.window_s >= LONG_WINDOW_S]
        if not long:
            continue  # aucune fenêtre longue mesurée : rien à porter
        free += min(max(0.0, result.limits[g.key][0] - g.used_at(now)) for g in long)
        used += sum(g.used_at(now) for g in long)
        measured.append(profile.name)
    unit = DEFAULT_AGENT_SHARE
    if agents:
        # l'observation ne fait que relever le coût d'un agent (jamais l'abaisser
        # sous le plancher) : une proposition prudente
        unit = max(DEFAULT_AGENT_SHARE, min(1.0, used / len(agents)))
    harness = items[0].harness if items else ""
    return Offer(harness=harness, free=free, unit=unit,
                 capacity=int(free / unit) if unit > 0 else 0,
                 accounts=tuple(measured))


def proposal(cfg, db, book, *, now: float | None = None, fleet: dict | None = None,
             critical=()) -> Proposal:
    """La proposition de bascule, sans aucun geste : lecture seule.

    `fleet` : `{agent: harnais}` à considérer (défaut : les agents sous bail,
    `live_fleet`). `critical` : noms à ne jamais déplacer (défaut :
    `cfg.critical_agents`) — leur réserve est en plus gardée dans la marge.
    """
    now = time.time() if now is None else now
    try:
        declared = accounts.parse(getattr(cfg, "accounts", None) or {})
    except accounts.AccountError as exc:
        return Proposal(reason="invalid account configuration: %s" % exc)
    try:
        billed = set(cost_mod.paid_harnesses_of(getattr(cfg, "host", None)))
    except cost_mod.CostError as exc:
        # fail-closed (0019 §2) : sans descripteurs, on ne devine pas qui est
        # facturé au token
        return Proposal(reason="usage-billed harnesses unknown: %s" % exc)
    if fleet is None:
        fleet = live_fleet(db, now, getattr(cfg, "host", None))
    agents = {name: harness for name, harness in fleet.items() if harness}
    kept_names = set(critical or getattr(cfg, "critical_agents", ()) or ())
    candidates = sorted(name for name, harness in agents.items()
                        if harness in billed and name not in kept_names)
    if not candidates:
        return Proposal(reason="no usage-billed agent to move")
    # ne jamais vider une famille : la relecture croisée (canon §2) suppose au
    # moins deux familles représentées après la bascule
    kept = 0
    for family in set(agents.values()):
        members = [name for name, harness in agents.items() if harness == family]
        if members and all(name in candidates for name in members):
            kept += 1
    offers = [_offer(declared[harness], [name for name, h in agents.items()
                                         if h == harness], now, book)
              for harness in sorted(declared) if harness not in billed]
    ranked = sorted((o for o in offers if o.capacity > 0),
                    key=lambda o: (-o.capacity, o.harness))
    movable = min(len(candidates) - kept, sum(o.capacity for o in ranked))
    moves = []
    remaining = movable
    for offer in ranked:
        take = min(offer.capacity, remaining)
        if take:
            moves.append((offer.harness, take))
        remaining -= take
    return Proposal(movable=max(0, movable), moves=tuple(moves), offers=tuple(offers),
                    candidates=len(candidates), kept_for_review=kept)
