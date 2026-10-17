# SPDX-License-Identifier: AGPL-3.0-only
"""L243-4: choose a lot's directory without changing the agent's primary cwd."""
from __future__ import annotations

import dataclasses
import os

from . import canon, mail, projects, storage, work
from . import db as db_mod


class WorkdirError(ValueError):
    """The destination cannot safely run a project session."""


class ProjectNotDeclared(WorkdirError):
    """Le projet du lot est hors des projets déclarés par l'agent (#811 N1)."""


#: #811 N1 : préfixe du refus « projet déclaré dont le dossier manque » — le
#: seul qui mérite une note sur le lot ; un projet non déclaré n'est pas un
#: défaut du lot, il est simplement hors du périmètre de l'agent.
MISSING_DIR_PREFIX = "Host working directory absent"


def project(db, lot) -> str:
    if not lot or not str(lot).isdigit():
        return ""
    item = work.get(db, int(lot))
    if item is None:
        raise WorkdirError("unknown lot #%s" % lot)
    # app is the lot's repository project; team may describe its staffing.
    if item.get("app") or item.get("team"):
        return (item.get("app") or item.get("team")).strip()
    for key in ("package_id", "package_parent"):
        if item.get(key):
            package = storage.of(db).packages.get(item[key])
            if package and package.get("team"):
                return package["team"].strip()
    return ""


def destination(cfg, agent, lot_project, *, canons=None) -> str | None:
    names = projects.agent_projects(agent)
    primary = names[0] if names else ""
    if not lot_project or lot_project == primary:
        return agent.get("cwd")
    if lot_project not in names:
        raise ProjectNotDeclared("project %s is not declared for %s" %
                                 (lot_project, agent["name"]))
    canons = canon.load_configured(cfg) if canons is None else canons
    paths = set()
    for card in canons:
        if not card.owns(agent.get("canon")):
            continue
        host = card.host(agent.get("host") or cfg.host)
        if host is None:
            continue
        # A work_dirs entry describes the primary cwd, not every repository.
        policy = dataclasses.replace(host.policy, work_dirs=None)
        path = policy.work_dir(lot_project, agent["name"])
        if path:
            paths.add(os.path.abspath(os.path.expanduser(path)))
    if len(paths) != 1:
        raise WorkdirError("project %s needs one unambiguous Host work root" % lot_project)
    path = paths.pop()
    if agent.get("cwd") and os.path.realpath(path) == os.path.realpath(agent["cwd"]):
        raise WorkdirError("project %s resolves to the primary working directory" % lot_project)
    return path


def problem(cfg, agent, lot_project, *, canons=None) -> str | None:
    """Raison de laisser un lot secondaire dans la file ; None s'il est prêt.

    Chaîne vide : le projet du lot est hors du périmètre déclaré de l'agent —
    rien à noter sur le lot (#811 N1)."""
    names = projects.agent_projects(agent)
    if not lot_project or (names and lot_project == names[0]):
        return None
    if lot_project not in names:      # hors périmètre : rien à noter (N1)
        return ""
    if getattr(cfg, "host_mediated", False) and agent.get("host") == cfg.host:
        return "mediated executor has no per-project depot"
    try:
        path = destination(cfg, agent, lot_project, canons=canons)
    except (WorkdirError, canon.CanonError) as exc:
        return str(exc)
    return None if path and os.path.isdir(path) else "%s: %r" % (MISSING_DIR_PREFIX, path)


def mail_hold(cfg, db, agent, lot, *, canons=None) -> bool:
    """Le courrier de ce lot attend-il son dossier ? (#811 B1)

    Règle PARTAGÉE entre l'exécuteur et le hook de harnais : un message dont
    l'unique lot vise un projet DÉCLARÉ autre que le primaire, dont le dossier
    n'est pas utilisable, ne se remet pas dans la session d'un autre lot — le
    hook le marquerait livré et l'exécuteur ne le verrait jamais pour ce lot.
    Faux pour le projet primaire, un projet non déclaré, un lot inconnu ou un
    dossier prêt : ces messages suivent le flux normal. Un canon illisible ne
    retient rien (flux normal), comme `problem` le fait pour son propre repli."""
    names = projects.agent_projects(agent)
    if len(names) < 2:                # mono-projet : pas de dossier secondaire
        return False
    try:
        lot_project = project(db, lot)
    except (WorkdirError, canon.CanonError):
        return False                  # lot inconnu : flux normal
    if not lot_project or lot_project == names[0]:
        return False                  # projet primaire : flux normal
    try:
        canons = canon.load_configured(cfg) if canons is None else canons
    except canon.CanonError:
        return False
    return bool(problem(cfg, agent, lot_project, canons=canons))


def deliverable(cfg, db, agent, rows, *, deferred=None, canons=None) -> list[dict]:
    """#811 R3 : les messages de `rows` remisables, ceux des lots retenus ôtés.

    Même règle que `mail_hold`, appliquée à une page de non-lus : les lignes
    portent leur lot (`work_item_id`, lu par `mail.unread`). `deferred(lot)`
    dit une retenue déjà connue de l'appelant (mémoire de l'exécuteur) ; le
    verdict est mémoïsé par lot, et un message sans lot passe. Mono-projet :
    page rendue telle quelle."""
    if not rows or len(projects.agent_projects(agent)) < 2:
        return rows
    if canons is None:
        try:
            canons = canon.load_configured(cfg)
        except canon.CanonError:
            return rows
    verdicts = {}
    gardes = []
    for row in rows:
        lot = str(row.get("work_item_id") or "").strip()
        if lot and lot not in verdicts:
            verdicts[lot] = bool((deferred and deferred(lot))
                                 or mail_hold(cfg, db, agent, lot, canons=canons))
        if lot and verdicts[lot]:
            continue
        gardes.append(row)
    return gardes


#: #811 R3 : messages lus par page en franchissant un préfixe retenu
MAIL_PAGE = mail.UNREAD_LIMIT


def deliverable_unread(cfg, db, agent, *, besoin, mode="unread", deferred=None) -> list[dict]:
    """#811 R3 : les non-lus remisables, au-delà d'un préfixe qui les masquait.

    `mail.unread` — et ses variantes — borne la lecture aux plus anciens : un
    lot secondaire sans dossier peut remplir ce préfixe et cacher un message
    remisable plus récent, au hook comme au pick. On lit donc par pages
    croissantes, en écartant les messages retenus SANS les réserver ni les
    marquer, jusqu'à `besoin` remisables ou la fin de la boîte. Mono-projet :
    lecture simple, flux inchangé. Un backend qui borne lui-même la page
    (exécuteur médié) ne fait pas boucler : on s'arrête quand la page ne
    progresse plus."""
    lire = {"unread": mail.unread, "active": mail.unread_active,
            "urgent": mail.unread_urgent}[mode]
    name = agent.get("name")
    besoin = max(1, int(besoin))
    if cfg is None:                   # construction partielle : lecture simple
        return lire(db, name, limit=besoin)
    if len(projects.agent_projects(agent)) < 2:
        return lire(db, name, limit=besoin)
    canons = None
    try:
        canons = canon.load_configured(cfg)
    except canon.CanonError:
        return lire(db, name, limit=besoin)
    page = max(MAIL_PAGE, besoin)
    lus = 0
    while True:
        rows = lire(db, name, limit=page)
        try:
            gardes = deliverable(cfg, db, agent, rows, deferred=deferred, canons=canons)
        except db_mod.DbError:
            return rows               # dossier illisible : page brute, rien perdu
        if len(gardes) >= besoin or len(rows) < page or len(rows) <= lus:
            return gardes
        lus = len(rows)
        page = page * 2
