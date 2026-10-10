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
* les agents au forfait d'abord ; un agent payé au token ne prend rien sauf
  `allow_paid`, et jamais pendant une alerte `balance_low` ;
* un plafond de budget atteint (`CostBook.over`), un agent en pause budget
  dans le mesh, une pression ou une batterie faible de l'hôte de l'agent : pas
  de prise ;
* au plus `max_per_hour` prises par heure glissante, comptées en base pour
  tout le mesh (plusieurs `ameesh notify` ne se cumulent pas) ;
* l'attribution est conditionnelle (assigné encore vide) : deux preneurs
  concurrents ne prennent pas le même élément.

Le cœur reste générique : rien ici ne nomme un métier. La source d'un élément
est un texte libre ; le circuit (relecture, vérifications, intégration) est
celui du projet de l'agent.
"""
from __future__ import annotations

import json
import time

from . import storage
from .db import Db

ITEM_TYPE = "improvement"
PRIORITIES = (1, 2, 3)
DEFAULT_PRIORITY = 2
#: prise automatique : au repos depuis 30 min, au plus 2 prises par heure
DEFAULT_TAKE_IDLE_S = 1800.0
DEFAULT_TAKE_MAX_PER_HOUR = 2
#: préfixe de la note de journal d'une prise automatique (compte du débit)
AUTO_TAKE_NOTE = "prise automatique"
TAKE_WINDOW_S = 3600.0
#: alertes qui empêchent une prise sur l'hôte de l'agent
HOST_BLOCKING_ALERTS = ("host_pressure", "host_power_low", "host_not_ready")
SENDER = "ameesh"


class BacklogError(ValueError):
    """Élément refusé — message toujours actionnable."""


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
        actor: str = "") -> dict:
    """Ajoute un élément à la file. La valeur attendue est obligatoire : la
    file ne reçoit pas de travail pour occuper."""
    title = (title or "").strip()
    if not title:
        raise BacklogError("titre obligatoire")
    expected_value = " ".join((expected_value or "").split())
    if not expected_value:
        raise BacklogError("valeur attendue obligatoire (--value) : pas de travail pour "
                           "occuper un agent")
    try:
        value_score = int(value_score)
    except (TypeError, ValueError):
        raise BacklogError("score de valeur entier attendu (1 à 100)") from None
    if not 1 <= value_score <= 100:
        raise BacklogError("score de valeur hors bornes : %d (1 à 100)" % value_score)
    if int(priority) not in PRIORITIES:
        raise BacklogError("priorité 1 (haute), 2 (normale) ou 3 (basse) : %r" % priority)
    fiche = None
    if package:
        from . import work
        fiche = work._package(db, package)
    return storage.of(db).work.backlog_add(
        title=title, body=body or "", source=source or "", expected_value=expected_value,
        value_score=value_score, priority=int(priority), team=(team or "").strip() or None,
        required_capabilities=_capabilities(required_capabilities) or None,
        package_id=fiche["id"] if fiche else None,
        package_parent=fiche.get("parent") if fiche else None,
        note="file d'amélioration (valeur %d) : %s" % (value_score, expected_value),
        actor=actor)


def items(db: Db, *, open_only: bool = True, limit: int = 100) -> list:
    """Les éléments dans l'ordre de prise."""
    return storage.of(db).work.backlog(open_only=open_only, limit=limit)


def fits(item: dict, agent: dict) -> bool:
    """L'élément correspond-il à l'équipe et aux capacités de l'agent ?"""
    team = (item.get("team") or "").strip()
    if team and team != (agent.get("team") or "").strip():
        return False
    required = set(item.get("required_capabilities") or ())
    return required <= set(agent.get("capabilities") or ())


# --------------------------------------------------------------------------
# prise automatique
# --------------------------------------------------------------------------

def take_message(item: dict) -> str:
    return ("Lot #%d, pris dans la file d'amélioration (aucune demande humaine ni lot du "
            "projet ne t'attendait) : %s\nValeur attendue : %s%s\n"
            "Suis le circuit normal de ton projet (relecture, vérifications, intégration "
            "par qui en a le droit). Interdit dans ce lot : tout geste irréversible ou de "
            "production (déploiement, fusion, suppression de données, serveur de "
            "production, dépense engagée) — propose-le à un humain et arrête-toi là. "
            "Si un lot ou un courrier t'arrive, il passe avant celui-ci. Si l'élément n'a plus "
            "de valeur, dis-le et rends-le (ameesh work note, puis ameesh work close "
            "--abandoned) plutôt que de travailler pour occuper." % (
                int(item["id"]), item["title"], item.get("expected_value") or "",
                "\n%s" % item["body"] if (item.get("body") or "").strip() else ""))


def _blocked_hosts(alerts: list) -> set:
    return {a.get("host") for a in alerts or () if a.get("type") in HOST_BLOCKING_ALERTS
            and a.get("host")}


def _awaited(row: dict) -> bool:
    """Un travail attend-il déjà cet agent ? (lot, courrier, consigne, lot de
    session encore ouvert) — il passe avant toute amélioration."""
    from . import stagnation
    if row.get("assigned_lot_id") is not None or int(row.get("unread") or 0):
        return True
    if row.get("has_pending_prompt"):
        return True
    state = row.get("session_lot_state")
    return bool(state) and state not in stagnation.DONE_STATES


def _default_budget_check(cfg, db):
    """`agent -> raison | ''` : plafonds du mesh et de l'agent (L70)."""
    from . import budget as budget_mod
    from . import cost as cost_mod
    limits = budget_mod.current(cfg, db)
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, **limits.book_kwargs())
    return lambda agent: book.over(agent, pace=False)


def auto_take(cfg, db, *, now: float | None = None, idle_s: float = DEFAULT_TAKE_IDLE_S,
              max_per_hour: int = DEFAULT_TAKE_MAX_PER_HOUR, allow_paid: bool = False,
              alerts: list | None = None, listing: list | None = None, paid=None,
              budget_check=None, dry_run: bool = False) -> list:
    """Un passage de prise automatique ; rend les prises faites (ou prévues
    en `dry_run`) : `[{item, agent, message_id}]`. Un seuil nul désactive."""
    from . import budget as budget_mod
    from . import mail, registry, sous_utilisation, work

    if idle_s <= 0 or max_per_hour <= 0:
        return []
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    room = int(max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S, AUTO_TAKE_NOTE)
    if room <= 0:
        return []
    queue = items(db, open_only=True, limit=200)
    if not queue:
        return []
    # un lot du projet attend un preneur : il passe avant toute amélioration
    if sous_utilisation.waiting_project_lots(st.operations.open_lots_activity(500)):
        return []
    listing = st.operations.listing() if listing is None else listing
    # un agent du mesh en pause budget : le plafond est atteint, on ne charge pas
    if any(row.get("status") == "blocked"
           and (row.get("status_text") or "").startswith(budget_mod.PAUSE_PREFIX)
           for row in listing):
        return []
    idle = [row for row in sous_utilisation.idle_agents(listing, now, idle_s)
            if not _awaited(row)]
    if not idle:
        return []
    alerts = alerts or []
    blocked = _blocked_hosts(alerts)
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

    candidates = []
    for row in idle:
        if row.get("host") in blocked:
            continue
        if is_paid(row) and (not allow_paid or balance_low):
            continue
        candidates.append(row)
    # forfaits d'abord, puis le repos le plus long
    candidates.sort(key=lambda r: (is_paid(r), sous_utilisation.idle_since(r), r["name"]))
    taken: list = []
    for row in candidates:
        if len(taken) >= room or not queue:
            break
        try:
            if budget_check(row["name"]):
                continue
        except Exception:
            continue
        agent = registry.get(db, row["name"]) or {}
        agent = dict(row, capabilities=agent.get("capabilities"))
        for item in list(queue):
            if not fits(item, agent):
                continue
            if dry_run:
                taken.append({"item": item, "agent": row["name"], "message_id": None})
                queue.remove(item)
                break
            try:
                check = work.check_assignee(db, row["name"], cfg=cfg)
            except work.WorkError:
                break                       # agent non réveillable : au suivant
            note = "%s par %s (valeur %s) : %s" % (
                AUTO_TAKE_NOTE, check["assignee"], item.get("value_score"),
                item.get("expected_value") or "")
            done = st.work.assign(int(item["id"]), check["assignee"], current=None,
                                  note=note, actor=SENDER)
            queue.remove(item)
            if done is None:
                continue                    # pris entre-temps : élément suivant
            message_id = mail.send(db, SENDER, check["assignee"], take_message(done),
                                   kind="event", work_item_id=str(done["id"]),
                                   payload={"backlog": {"lot": int(done["id"]),
                                                        "value_score": done.get("value_score")}})
            taken.append({"item": done, "agent": check["assignee"],
                          "message_id": message_id})
            break
    return taken


# --------------------------------------------------------------------------
# CLI (`ameesh work backlog …`, branchée par mesh_cli)
# --------------------------------------------------------------------------

def add_parsers(work_sub, func) -> None:
    p = work_sub.add_parser("backlog", help="file d'amélioration continue (L119, 0037)")
    sub = p.add_subparsers(dest="backlog_command")
    p_add = sub.add_parser("add", help="ajouter un élément à valeur attendue")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--value", required=True, dest="expected_value",
                       help="valeur attendue, en une phrase")
    p_add.add_argument("--score", required=True, type=int, dest="value_score",
                       help="valeur chiffrée de 1 à 100 (la plus forte est prise d'abord)")
    p_add.add_argument("--priority", type=int, default=DEFAULT_PRIORITY,
                       choices=list(PRIORITIES), help="1 haute, 2 normale (défaut), 3 basse")
    p_add.add_argument("--source", default="",
                       help="d'où vient l'élément (constat, alerte, décision, dette…)")
    p_add.add_argument("--team", default=None, help="équipe qui peut le prendre")
    p_add.add_argument("--requires", action="append", default=[],
                       help="capacité exigée (répétable, ou liste à virgules)")
    p_add.add_argument("--body", default="")
    p_add.add_argument("--package", default=None, help="fiche WorkPackage (L29)")
    p_add.add_argument("--actor", default="")
    p_add.add_argument("--json", action="store_true")
    p_add.set_defaults(func=func)
    p_list = sub.add_parser("list", help="la file, dans l'ordre de prise")
    p_list.add_argument("--all", action="store_true",
                        help="aussi les éléments déjà pris et non terminés")
    p_list.add_argument("--limit", type=int, default=100)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=func)


def _line(item: dict) -> str:
    extra = []
    if item.get("team"):
        extra.append("équipe %s" % item["team"])
    if item.get("required_capabilities"):
        extra.append("exige %s" % ", ".join(item["required_capabilities"]))
    if item.get("assignee"):
        extra.append("%s, %s" % (item["state"], item["assignee"]))
    return "#%d  P%s  valeur %3s  %s%s\n      → %s" % (
        int(item["id"]), item.get("priority") or DEFAULT_PRIORITY, item.get("value_score"),
        item["title"], "  [%s]" % " ; ".join(extra) if extra else "",
        item.get("expected_value") or "")


def run(db: Db, args) -> int:
    import sys
    command = getattr(args, "backlog_command", None)
    if command == "add":
        try:
            item = add(db, title=args.title, expected_value=args.expected_value,
                       value_score=args.value_score, priority=args.priority,
                       source=args.source, team=args.team,
                       required_capabilities=args.requires, body=args.body,
                       package=args.package, actor=args.actor)
        except BacklogError as exc:
            print("erreur : %s" % exc, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(item, ensure_ascii=False, default=str))
        else:
            print("élément #%d ajouté à la file (valeur %d, priorité %d) : %s" % (
                item["id"], item["value_score"], item["priority"], item["title"]))
        return 0
    if command == "list":
        rows = items(db, open_only=not args.all, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("file d'amélioration vide")
            return 0
        for row in rows:
            print(_line(row))
        return 0
    print("usage : ameesh work backlog add|list", file=sys.stderr)
    return 2
