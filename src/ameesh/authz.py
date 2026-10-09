# SPDX-License-Identifier: AGPL-3.0-only
"""Autorisations par relations (étude v2 D2) — premier cas : la visibilité de
l'activité d'une persona (décision 0033 §9).

Le canon reste la source : les relations se lisent dans les fiches, modifiées
par PR relue. Elles sont évaluées ici, sans service extérieur ; un moteur
OpenFGA pourra plus tard recevoir les mêmes relations (0033 §4, optionnel).

Relations lues :
* `persona.responsible` → l'humain **responsable** de la persona ;
* `Member.deputies` → ses **suppléants** (ils peuvent tout faire à sa place, 0033 §7) ;
* `Member.superiors` → ses **supérieurs**, de proche en proche (chaîne
  d'escalade), bornée et protégée des cycles.

Règle 0033 §9 : un humain voit l'activité présente et passée d'une persona
s'il en est le responsable, un suppléant du responsable, ou un supérieur du
responsable (à n'importe quel niveau de la chaîne).
"""
from __future__ import annotations

MAX_DEPTH = 10


def _member(canon, human: str):
    ident = human[len("human:"):] if human.startswith("human:") else human
    for m in getattr(canon, "members", []):
        if m.title == ident:
            return m
    return None


def _persona(canons, name: str):
    for c in canons:
        agent = c.agent(name) if hasattr(c, "agent") else None
        if agent is not None:
            return c, agent
    return None, None


def viewers(canons, persona: str) -> dict[str, str]:
    """{human:<id>: relation} des humains qui voient l'activité de `persona`."""
    canon, agent = _persona(canons, persona)
    if agent is None or not agent.responsible:
        return {}
    out: dict[str, str] = {agent.responsible: "responsable"}
    responsable = _member(canon, agent.responsible)
    for d in (getattr(responsable, "deputies", None) or []) if responsable else []:
        out.setdefault(d, "suppléant du responsable")
    frontier, depth = [agent.responsible], 0
    seen = {agent.responsible}
    while frontier and depth < MAX_DEPTH:
        depth += 1
        nxt = []
        for human in frontier:
            m = _member(canon, human)
            for s in (getattr(m, "superiors", None) or []) if m else []:
                if s not in seen:
                    seen.add(s)
                    out.setdefault(s, "supérieur (niveau %d)" % depth)
                    nxt.append(s)
        frontier = nxt
    return out


def can_view_activity(canons, human: str, persona: str) -> tuple[bool, str]:
    rel = viewers(canons, persona).get(human)
    if rel:
        return True, rel
    return False, "%s n'est ni responsable de %s, ni suppléant, ni supérieur" % (human, persona)
