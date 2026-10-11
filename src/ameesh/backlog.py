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

**Lots de l'équipe** (L165, avant la file) : un agent réveillable au repos
sans lot depuis `lot_idle_s` (15 min par défaut) prend lui-même le lot non
assigné le plus prioritaire de SON équipe qu'il sait faire, sans attendre
qu'on le lui confie (constat du 11/10 : 9 agents Nexlink sur 13 au repos, 3
lots Nexlink sans agent). Mêmes garde-fous que la file (repos, rien ne
l'attend, forfait d'abord, budget, hôte), et en plus :

* seuls les lots `intake` sans assigné, hors file : jamais un lot qui attend
  un humain (`waiting_human`), déjà confié (`build`), de décision (demande de
  décision en attente, jalon de décision ouvert, capacité `decision` exigée)
  ni de production (capacité `production` exigée) ;
* l'équipe du lot est la sienne, sinon celle de sa fiche (ou de la fiche
  parente), sinon son application ; elle doit être celle de l'agent. Un lot
  sans équipe connue n'est pas pris ;
* l'agent sait le faire : les capacités exigées sont les siennes, et
  `harness:<nom>` désigne son harnais ;
* un lot dont le début prévu (`planned_start`) est après aujourd'hui attend
  sa date ;
* ordre : priorité, puis dates prévues (début, fin, livraison), puis
  ancienneté ; un lot créé il y a moins de 5 min est laissé à qui l'a créé
  (il le confie sans doute dans la foulée) ;
* un orchestrateur ne se sert pas lui-même ; ceux de l'équipe (sinon tous)
  reçoivent un courrier `event` lié au lot et peuvent le réassigner (`ameesh
  work assign`) ;
* au plus `lot_max_per_hour` prises par heure glissante, comptées à part de
  la file.

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
#: L165 : un lot non assigné de l'équipe est pris après 15 min de repos, au
#: plus 6 par heure pour tout le mesh ; débit compté à part de la file
DEFAULT_LOT_IDLE_S = 900.0
DEFAULT_LOT_MAX_PER_HOUR = 6
LOT_TAKE_NOTE = "lot de l'équipe pris de lui-même"
#: âge minimal d'un lot pris : l'orchestrateur qui vient de le créer le confie
LOT_MIN_AGE_S = 300.0
#: capacités exigées qui réservent un lot à une attribution explicite
RESERVED_CAPABILITIES = ("decision", "production")
#: capacité exigée qui désigne un harnais (`harness:codex`)
HARNESS_PREFIX = "harness:"
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
        raise BacklogError("title required")
    expected_value = " ".join((expected_value or "").split())
    if not expected_value:
        raise BacklogError("expected value required (--value): no work just to keep "
                           "an agent busy")
    try:
        value_score = int(value_score)
    except (TypeError, ValueError):
        raise BacklogError("value score must be an integer (1 to 100)") from None
    if not 1 <= value_score <= 100:
        raise BacklogError("value score out of range: %d (1 to 100)" % value_score)
    if int(priority) not in PRIORITIES:
        raise BacklogError("priority 1 (high), 2 (normal) or 3 (low): %r" % priority)
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
        note="improvement backlog (value %d): %s" % (value_score, expected_value),
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
    return ("Lot #%d, taken from the improvement backlog (no human request or project "
            "lot was waiting for you): %s\nExpected value: %s%s\n"
            "Follow your project's normal flow (review, checks, integration "
            "by whoever has the right). Forbidden in this lot: any irreversible or "
            "production action (deployment, merge, data deletion, production "
            "server, committed spending): propose it to a human and stop there. "
            "If a lot or a message reaches you, it comes before this one. If the item no "
            "longer has value, say so and hand it back (ameesh work note, then ameesh work "
            "close --abandoned) rather than working just to keep busy." % (
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


def lot_take_message(item: dict, orchestras: list) -> str:
    return ("Lot #%d, taken on your own (L165): you were idle and this lot of your team "
            "had no taker: %s%s\n"
            "%s can reassign it; if they give you something else, that comes first. "
            "Follow your project's normal flow (review, checks, integration "
            "by whoever has the right). Forbidden in this lot: any irreversible or "
            "production action (deployment, merge, data deletion, production "
            "server, committed spending): propose it to a human and stop there. "
            "If the lot is not for you (capability, context), say so right away "
            "(ameesh work note %d, and a message to %s) rather than keeping it." % (
                int(item["id"]), item["title"],
                "\n%s" % item["body"] if (item.get("body") or "").strip() else "",
                ", ".join(orchestras) if orchestras else "The orchestrator",
                int(item["id"]), ", ".join(orchestras) if orchestras else "the orchestrator"))


def lot_notice(item: dict, agent: str, idle_for_s: float) -> str:
    return ("Automatic take (L165): %s, idle for %d min, took lot #%d of "
            "team %s, which had no assignee: %s. To give it to someone else: "
            "ameesh work assign %d <agent>." % (
                agent, int(idle_for_s // 60), int(item["id"]), item.get("lot_team"),
                item["title"], int(item["id"])))


def lot_takeable(item: dict, today: str | None = None) -> bool:
    """Un lot du projet peut-il être pris sans qu'on le confie ? Jamais un lot
    qui attend un humain (seuls les lots `intake` sans assigné arrivent ici),
    de décision (demande de décision en attente, jalon de décision ouvert,
    capacité `decision` exigée) ni de production (capacité `production`
    exigée) ; jamais un lot sans équipe connue, ni un lot dont le début prévu
    est après `today` (« AAAA-MM-JJ »)."""
    if item.get("state") != "intake" or (item.get("assignee") or "").strip():
        return False
    start = str(item.get("planned_start") or "")[:10]
    if today and start and start > today:
        return False
    if item.get("pending_decision") or item.get("decision_milestone"):
        return False
    if set(item.get("required_capabilities") or ()) & set(RESERVED_CAPABILITIES):
        return False
    return bool((item.get("lot_team") or "").strip())


def lot_fits(item: dict, agent: dict) -> bool:
    """Le lot est-il de l'équipe de l'agent, et l'agent sait-il le faire ?
    Une capacité exigée `harness:<nom>` désigne le harnais de l'agent."""
    team = (agent.get("team") or "").strip()
    if not team or (item.get("lot_team") or "").strip() != team:
        return False
    have = set(agent.get("capabilities") or ())
    if agent.get("harness"):
        have.add("%s%s" % (HARNESS_PREFIX, agent["harness"]))
    return set(item.get("required_capabilities") or ()) <= have


def _team_orchestrators(cfg, db, listing: list, team: str, orchestras=None) -> list:
    """Les orchestrateurs du registre à prévenir d'une prise : ceux de
    l'équipe, sinon tous."""
    from . import sous_utilisation
    if orchestras is None:
        orchestras = sous_utilisation.orchestrators(cfg, db, listing)
    teams = {row["name"]: (row.get("team") or "").strip() for row in listing}
    known = [name for name in orchestras if name in teams]
    return sorted([name for name in known if teams[name] == team] or known)


def auto_take(cfg, db, *, now: float | None = None, idle_s: float = DEFAULT_TAKE_IDLE_S,
              max_per_hour: int = DEFAULT_TAKE_MAX_PER_HOUR, allow_paid: bool = False,
              alerts: list | None = None, listing: list | None = None, paid=None,
              budget_check=None, dry_run: bool = False,
              lot_idle_s: float = DEFAULT_LOT_IDLE_S,
              lot_max_per_hour: int = DEFAULT_LOT_MAX_PER_HOUR, orchestras=None) -> list:
    """Un passage de prise automatique ; rend les prises faites (ou prévues
    en `dry_run`) : `[{item, agent, message_id, kind, notified}]`, `kind`
    valant `lot` (lot non assigné de l'équipe, L165) ou `backlog` (file
    d'amélioration, L119). Les lots de l'équipe passent d'abord ; tant qu'un
    lot du projet attend, personne ne prend d'amélioration. Un seuil nul
    désactive la prise correspondante."""
    from . import budget as budget_mod
    from . import mail, registry, sous_utilisation, work

    now = time.time() if now is None else float(now)
    st = storage.of(db)
    lot_room = 0
    if lot_idle_s > 0 and lot_max_per_hour > 0:
        lot_room = int(lot_max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S,
                                                                    LOT_TAKE_NOTE)
    lots = st.work.unassigned_lots(200) if lot_room > 0 else []
    # un lot tout juste créé va sans doute être confié : on lui laisse le temps
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    lots = [item for item in lots if lot_takeable(item, today)
            and now - float(item.get("created_ts") or now) >= LOT_MIN_AGE_S]
    room = 0
    if idle_s > 0 and max_per_hour > 0:
        room = int(max_per_hour) - st.work.auto_takes_since(TAKE_WINDOW_S, AUTO_TAKE_NOTE)
    queue = items(db, open_only=True, limit=200) if room > 0 else []
    if not lots and not queue:
        return []
    listing = st.operations.listing() if listing is None else listing
    # un agent du mesh en pause budget : le plafond est atteint, on ne charge pas
    if any(row.get("status") == "blocked"
           and (row.get("status_text") or "").startswith(budget_mod.PAUSE_PREFIX)
           for row in listing):
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

    def candidates(threshold: float, skip=()) -> list:
        out = []
        for row in sous_utilisation.idle_agents(listing, now, threshold):
            if row["name"] in skip or _awaited(row) or row.get("host") in blocked:
                continue
            if is_paid(row) and (not allow_paid or balance_low):
                continue
            out.append(row)
        # forfaits d'abord, puis le repos le plus long
        out.sort(key=lambda r: (is_paid(r), sous_utilisation.idle_since(r), r["name"]))
        return out

    def within_budget(row) -> bool:
        try:
            return not budget_check(row["name"])
        except Exception:
            return False

    taken: list = []
    if lots:
        # un orchestrateur répartit : il ne se sert pas lui-même
        if orchestras is None:
            try:
                orchestras = sous_utilisation.orchestrators(cfg, db, listing)
            except Exception:
                orchestras = []
        for row in candidates(lot_idle_s, skip=set(orchestras)):
            if len(taken) >= lot_room or not lots:
                break
            if not within_budget(row):
                continue
            agent = registry.get(db, row["name"]) or {}
            agent = dict(row, capabilities=agent.get("capabilities"))
            for item in list(lots):
                if not lot_fits(item, agent):
                    continue
                notified = _team_orchestrators(cfg, db, listing, item["lot_team"],
                                               orchestras)
                if dry_run:
                    taken.append({"item": item, "agent": row["name"], "message_id": None,
                                  "kind": "lot", "notified": notified})
                    lots.remove(item)
                    break
                try:
                    check = work.check_assignee(db, row["name"], cfg=cfg)
                except work.WorkError:
                    break                   # agent non réveillable : au suivant
                done = st.work.assign(int(item["id"]), check["assignee"], current=None,
                                      note="%s par %s, au repos" % (LOT_TAKE_NOTE,
                                                                    check["assignee"]),
                                      actor=SENDER)
                lots.remove(item)
                if done is None:
                    continue                # pris entre-temps : lot suivant
                done = dict(done, lot_team=item["lot_team"])
                payload = {"lot_take": {"lot": int(done["id"]), "agent": check["assignee"],
                                        "team": item["lot_team"]}}
                message_id = mail.send(db, SENDER, check["assignee"],
                                       lot_take_message(done, notified), kind="event",
                                       work_item_id=str(done["id"]), payload=payload)
                idle_for = now - sous_utilisation.idle_since(row)
                for name in notified:
                    mail.send(db, SENDER, name, lot_notice(done, check["assignee"], idle_for),
                              kind="event", work_item_id=str(done["id"]), payload=payload)
                taken.append({"item": done, "agent": check["assignee"],
                              "message_id": message_id, "kind": "lot", "notified": notified})
                break
    if not queue:
        return taken
    # un lot du projet attend un preneur (ou vient d'être pris) : il passe
    # avant toute amélioration
    if taken or sous_utilisation.waiting_project_lots(st.operations.open_lots_activity(500)):
        return taken
    for row in candidates(idle_s):
        if len(taken) >= room or not queue:
            break
        if not within_budget(row):
            continue
        agent = registry.get(db, row["name"]) or {}
        agent = dict(row, capabilities=agent.get("capabilities"))
        for item in list(queue):
            if not fits(item, agent):
                continue
            if dry_run:
                taken.append({"item": item, "agent": row["name"], "message_id": None,
                              "kind": "backlog", "notified": []})
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
                          "message_id": message_id, "kind": "backlog", "notified": []})
            break
    return taken


# --------------------------------------------------------------------------
# CLI (`ameesh work backlog …`, branchée par mesh_cli)
# --------------------------------------------------------------------------

def add_parsers(work_sub, func) -> None:
    p = work_sub.add_parser("backlog", help="continuous improvement backlog (L119, 0037)")
    sub = p.add_subparsers(dest="backlog_command")
    p_add = sub.add_parser("add", help="add an item with an expected value")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--value", required=True, dest="expected_value",
                       help="expected value, in one sentence")
    p_add.add_argument("--score", required=True, type=int, dest="value_score",
                       help="value score from 1 to 100 (highest is taken first)")
    p_add.add_argument("--priority", type=int, default=DEFAULT_PRIORITY,
                       choices=list(PRIORITIES), help="1 high, 2 normal (default), 3 low")
    p_add.add_argument("--source", default="",
                       help="where the item comes from (finding, alert, decision, debt...)")
    p_add.add_argument("--team", default=None, help="team that can take it")
    p_add.add_argument("--requires", action="append", default=[],
                       help="required capability (repeatable, or comma-separated list)")
    p_add.add_argument("--body", default="")
    p_add.add_argument("--package", default=None, help="WorkPackage sheet (L29)")
    p_add.add_argument("--actor", default="")
    p_add.add_argument("--json", action="store_true")
    p_add.set_defaults(func=func)
    p_list = sub.add_parser("list", help="the backlog, in take order")
    p_list.add_argument("--all", action="store_true",
                        help="also items already taken and not finished")
    p_list.add_argument("--limit", type=int, default=100)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=func)


def _line(item: dict) -> str:
    extra = []
    if item.get("team"):
        extra.append("team %s" % item["team"])
    if item.get("required_capabilities"):
        extra.append("requires %s" % ", ".join(item["required_capabilities"]))
    if item.get("assignee"):
        extra.append("%s, %s" % (item["state"], item["assignee"]))
    return "#%d  P%s  value %3s  %s%s\n      → %s" % (
        int(item["id"]), item.get("priority") or DEFAULT_PRIORITY, item.get("value_score"),
        item["title"], "  [%s]" % "; ".join(extra) if extra else "",
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
            print("error: %s" % exc, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(item, ensure_ascii=False, default=str))
        else:
            print("item #%d added to the backlog (value %d, priority %d): %s" % (
                item["id"], item["value_score"], item["priority"], item["title"]))
        return 0
    if command == "list":
        rows = items(db, open_only=not args.all, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("improvement backlog empty")
            return 0
        for row in rows:
            print(_line(row))
        return 0
    print("usage: ameesh work backlog add|list", file=sys.stderr)
    return 2
