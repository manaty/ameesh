# SPDX-License-Identifier: AGPL-3.0-only
"""Avatars d'humains (lot #7634, spec owner du 17/11/2026 §5).

Un avatar est une fiche Agent qui porte un bloc ``avatar:`` et dont
``responsible`` est l'humain qu'elle représente (design 5322 §1.1). Le cœur ne
connaît aucun nom d'avatar : il lit la fiche au canon et route vers l'agent
ainsi déclaré. Le nom historique du bureau Manaty reste possible — c'est la
fiche qui fait l'avatar, pas le nom.

Ce module ne lit que le canon. Il ne donne aucun droit, aucun secret : il
répond à « quel agent représente cet humain ? » et « cet agent est-il
l'avatar de qui ? ». Un canon illisible ne devine rien (None), et deux
avatars du même humain dans un même canon rendent ``None`` (fail closed,
comme ``avatar-duplicate`` du design 5322 §1.1).
"""
from __future__ import annotations

from . import canon as canon_mod

#: clé de carte qui fait d'un agent l'avatar d'un humain
CARD_SECTION = "avatar"


def _section(agent) -> dict | None:
    """Bloc ``avatar`` d'une carte Agent, ou None (absent ou illisible)."""
    data = getattr(getattr(agent, "fiche", None), "data", None) or {}
    if CARD_SECTION not in data:
        return None
    section = data.get(CARD_SECTION)
    return section if isinstance(section, dict) else None


def _responsible(agent, canon) -> str | None:
    """``human:<id>`` de la fiche, résolu dans LE canon qui la déclare."""
    human = getattr(agent, "responsible", None)
    if not isinstance(human, str) or not human.startswith("human:"):
        return None
    resolve = getattr(canon, "resolve_human", None)
    if callable(resolve) and resolve(human) is None:
        return None
    return human


def of_canon(canon) -> dict[str, str]:
    """``{human: avatar}`` d'un canon ; un doublon pour le même humain est
    écarté (fail closed, jamais un choix arbitraire)."""
    found: dict[str, str] = {}
    ambiguous: set[str] = set()
    for agent in getattr(canon, "agents", ()) or ():
        if _section(agent) is None:
            continue
        human = _responsible(agent, canon)
        title = getattr(agent, "title", None)
        if not human or not title:
            continue
        if human in found and found[human] != title:
            ambiguous.add(human)
        else:
            found[human] = str(title)
    for human in ambiguous:
        found.pop(human, None)
    return found


def canons(cfg) -> list:
    """Les canons configurés, ou [] : un canon illisible ne fait pas un avatar."""
    if cfg is None:
        return []
    try:
        return list(canon_mod.load_configured(cfg))
    except Exception:  # noqa: BLE001 — l'appelant garde la route actuelle
        return []


def configured(cfg) -> dict[str, str]:
    """``{human: avatar}`` des canons configurés.

    Le canon par défaut passe en premier : quand deux canons déclarent un
    avatar pour le même humain, le premier (le canon par défaut) fait foi —
    L43 : un agent se résout dans le canon qui le déclare, et la route
    humaine choisit le canon servi en premier.
    """
    merged: dict[str, str] = {}
    for canon in canons(cfg):
        if canon is None or not getattr(canon, "readable", True):
            continue
        for human, name in of_canon(canon).items():
            merged.setdefault(human, name)
    return merged


def name_for(cfg, human: str | None) -> str | None:
    """L'agent avatar de ``human:<id>``, ou None (pas d'avatar, canon illisible)."""
    if not human:
        return None
    return configured(cfg).get(str(human))


def by_name(cfg) -> dict[str, str]:
    """``{avatar: human}`` des canons configurés."""
    return {name: human for human, name in configured(cfg).items()}


def is_avatar(cfg, name: str | None) -> bool:
    """Cet agent est-il l'avatar d'un humain déclaré au canon ?"""
    if not name:
        return False
    return str(name) in by_name(cfg)


def human_of(cfg, name: str | None) -> str | None:
    """L'humain dont ``name`` est l'avatar, ou None."""
    if not name:
        return None
    return by_name(cfg).get(str(name))


def configured_desks(cfg, human: str | None, rows=(), project=None) -> list[str]:
    """Les agents configurés comme route d'un humain, sans avatar déclaré.

    La configuration de release nomme son destinataire (`release.projects.*
    .report`, le bureau pour ameesh). Un agent n'est retenu que si le registre
    le dit `responsible` DE CET humain : le mode `externe` seul ne fait pas une
    route, et la session d'un autre humain ne reçoit jamais son courrier
    (#7634, relecture cx1 B1). `project` restreint au projet de l'alerte."""
    if not human:
        return []
    release = getattr(cfg, "release", None) or {}
    projects = (release.get("projects") or {}) if isinstance(release, dict) else {}
    if project and project in projects:
        chosen = [projects.get(project)]
    else:
        chosen = list(projects.values())
    by_name = {str(row.get("name")): row for row in rows or ()
               if (row or {}).get("name")}
    found = []
    for entry in chosen:
        report = str((entry or {}).get("report") or "")
        row = by_name.get(report)
        if report and row is not None and row.get("responsible") == str(human):
            found.append(report)
    return list(dict.fromkeys(found))


def from_canons(canons_list) -> dict[str, str]:
    """Comme :func:`configured`, sur une liste de canons déjà chargée."""
    merged: dict[str, str] = {}
    for canon in canons_list or ():
        if canon is None or not getattr(canon, "readable", True):
            continue
        for human, name in of_canon(canon).items():
            merged.setdefault(human, name)
    return merged


def is_avatar_in(canons_list, name: str | None, *, row_canon: str | None = None) -> bool:
    """``name`` est-il l'avatar d'un humain, dans ces canons déjà chargés ?

    Même règle que :func:`is_avatar` : une fiche qui porte le bloc ``avatar``
    et dont le ``responsible`` est un Member du canon. Aucun nom d'avatar
    n'est connu du cœur ; un canon illisible ne fait pas d'avatar. Avec
    ``row_canon`` (la colonne ``canon`` du registre), seule la fiche du canon
    propriétaire compte : une fiche homonyme d'un autre canon ne donne jamais
    de droit (#324 r1, #424 r1)."""
    if not name:
        return False
    for canon in canons_list or ():
        owns = getattr(canon, "owns", None)
        if row_canon is not None or owns is not None:
            if callable(owns) and not owns(row_canon):
                continue
        if str(name) in of_canon(canon).values():
            return True
    return False
