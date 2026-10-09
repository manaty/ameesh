# SPDX-License-Identifier: AGPL-3.0-only
"""Rôles adressables (lot L58, décisions 0032 et 0033 §7-8, étude v2 volet C).

Un rôle est une fonction dans un périmètre (une équipe), déclarée au canon par
une fiche `Role` : un titulaire, puis des suppléants dans l'ordre. On écrit au
rôle plutôt qu'à un nom :

    agent-mail send role:relecteur@ima "…"
    agent-mail send role:relecteur "…"        # équipe de l'expéditeur

L'adresse se résout **au moment de l'envoi** vers le premier de la chaîne qui
est disponible : une persona inscrite au registre, ni arrêtée ni morte. Un
humain de la chaîne ne reçoit pas de courrier ameesh : s'il précède toute
persona disponible, la résolution s'arrête sur lui et l'envoi est refusé avec
l'indication de l'humain à prévenir. Si personne n'est disponible, la
résolution indique l'escalade : les humains de la chaîne, le responsable du
titulaire et ses supérieurs (0033 §7).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

ADDRESS_RE = re.compile(r"^role:([a-z0-9][a-z0-9-]{0,63})(?:@([A-Za-z0-9][A-Za-z0-9._-]{0,63}))?$")
UNAVAILABLE = ("stopped", "dead")


class RoleError(ValueError):
    pass


@dataclass
class Resolution:
    role: str
    team: str | None
    name: str | None = None          # persona qui recevra le message
    rank: str = ""                   # « titulaire » ou « suppléant n »
    human: str | None = None         # humain qui tient le rôle à ce rang (pas de courrier)
    skipped: list = field(default_factory=list)    # [(qui, raison)]
    escalate: list = field(default_factory=list)   # humains à prévenir si personne

    @property
    def ok(self) -> bool:
        return self.name is not None

    def to_dict(self) -> dict:
        return {"role": self.role, "team": self.team, "name": self.name, "rank": self.rank,
                "human": self.human, "skipped": [list(s) for s in self.skipped],
                "escalate": self.escalate}

    def describe(self) -> str:
        cible = "role:%s%s" % (self.role, "@" + self.team if self.team else "")
        if self.ok:
            text = "%s → %s (%s)" % (cible, self.name, self.rank)
        elif self.human:
            text = "%s → %s (%s, humain : pas de courrier ameesh)" % (cible, self.human, self.rank)
        else:
            text = "%s → personne de disponible ; à prévenir : %s" % (
                cible, ", ".join(self.escalate) or "aucun humain trouvé")
        if self.skipped:
            text += " ; écartés : " + ", ".join("%s (%s)" % s for s in self.skipped)
        return text


def parse_address(address: str) -> tuple[str, str | None]:
    m = ADDRESS_RE.match(address or "")
    if not m:
        raise RoleError("adresse de rôle illisible : %r (role:<rôle>[@<équipe>])" % address)
    return m.group(1), m.group(2)


def is_address(dest: str) -> bool:
    return (dest or "").startswith("role:")


def find(canons, role: str, team: str | None):
    """(canon, fiche Role) ; un rôle sans équipe vaut pour toutes les équipes de
    son canon, après les rôles de l'équipe demandée."""
    exact, generic = [], []
    for c in canons:
        for r in getattr(c, "role_cards", []):
            if r.title != role:
                continue
            if team and r.team == team:
                exact.append((c, r))
            elif not r.team:
                generic.append((c, r))
            elif not team:
                exact.append((c, r))
    found = exact or generic
    if len(found) > 1:
        raise RoleError("rôle %s ambigu (%s) : précisez l'équipe, role:%s@<équipe>" % (
            role, ", ".join("%s@%s" % (r.title, r.team or "*") for _c, r in found), role))
    return found[0] if found else (None, None)


def _superiors(canon, human: str | None) -> list[str]:
    if not human:
        return []
    ident = human[len("human:"):] if human.startswith("human:") else human
    for m in canon.members:
        if m.title == ident:
            return list(m.superiors or [])
    return []


def resolve(db, canons, address: str, *, sender_team: str | None = None) -> Resolution:
    role, team = parse_address(address)
    team = team or sender_team
    canon, fiche = find(canons, role, team)
    if fiche is None:
        raise RoleError("aucun rôle %s%s au canon" % (role, "@" + team if team else ""))
    res = Resolution(role=role, team=fiche.team or team)
    humans: list[str] = []
    for i, who in enumerate(fiche.chain):
        rank = "titulaire" if i == 0 else "suppléant %d" % i
        if who.startswith("human:"):
            humans.append(who)
            if res.human is None and res.name is None:
                res.human, res.rank = who, rank
                break
            continue
        rows = db.query("SELECT status, stop_reason FROM agent_registry WHERE name = %s", (who,))
        if not rows:
            res.skipped.append((who, "absent du registre"))
            continue
        if rows[0].get("status") in UNAVAILABLE:
            res.skipped.append((who, rows[0]["status"]))
            continue
        res.name, res.rank = who, rank
        break
    if not res.ok:
        holder = fiche.holder or ""
        agent = canon.agent(holder) if holder and not holder.startswith("human:") else None
        responsible = agent.responsible if agent else None
        chain = humans + ([responsible] if responsible else []) + _superiors(canon, responsible)
        res.escalate = list(dict.fromkeys(h for h in chain if h))
    return res


def main(argv) -> int:
    """`ameesh roles [--team T] [--json]` : rôles du canon et leur résolution présente."""
    from . import canon as canon_mod, db as db_mod
    from .config import load as load_config
    p = argparse.ArgumentParser(prog="ameesh roles", description="Rôles du canon (L58).")
    p.add_argument("--team")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    canons = canon_mod.load_configured(cfg)
    db = db_mod.connect(cfg)
    out = []
    try:
        for c in canons:
            for r in getattr(c, "role_cards", []):
                if args.team and r.team not in (args.team, None):
                    continue
                try:
                    res = resolve(db, [c], "role:%s%s" % (r.title, "@" + r.team if r.team else ""))
                    out.append(res)
                except RoleError as exc:
                    print("ameesh roles : %s" % exc, file=sys.stderr)
    finally:
        db.close()
    if args.json:
        print(json.dumps([r.to_dict() for r in out], ensure_ascii=False))
    else:
        for r in out:
            print(r.describe())
        if not out:
            print("aucun rôle déclaré")
    return 0
