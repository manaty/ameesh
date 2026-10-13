# SPDX-License-Identifier: AGPL-3.0-only
"""Alertes de sous-utilisation (lot L94) : le gaspillage alerte autant que la
surcharge.

Demande du propriétaire (2026-10-10) : « la surconso comme la sous-conso
devrait alerter ameesh ». Le même jour, les forfaits dormaient (Claude à
12 % sur 7 jours, Codex primaire à 20 % pour un rythme permis de 52 %),
quatre agents au forfait au repos sans lot (l'orchestrateur, tenu par une
session `attach`, ne répartissait plus), pendant que des agents payés au
token travaillaient et que la VM du mesh tournait presque à vide.

Quatre types d'alerte, levés par `ameesh alerts` et poussés par
`ameesh notify` (0030 §4), mêmes règles de dédoublonnage et de résolution :

* `plan_underused` : un compte au forfait dont la capacité inutilisée sera
  perdue à la prochaine remise à zéro — fin de fenêtre proche et peu
  utilisée (`fin_de_fenetre`), ou consommation très en dessous du rythme
  permis de 0019 (`sous_rythme`). Volume perdu estimé : `1 − utilisé` de la
  fenêtre si rien ne change (même définition que les « pertes à la remise à
  zéro » de L74, `Gauge.expiring`), et la projection au rythme actuel ;
* `idle_capacity` : des agents réveillables (mode `execute`, exécuteur
  vivant, au repos, sous leur budget, sans lot ni courrier) depuis plus du
  seuil, alors que du travail attend (lots ouverts sans assigné, courrier en
  souffrance chez un agent occupé) ou que des agents payés au token
  travaillent pendant que des agents au forfait dorment (`forfait_au_repos`,
  le cas aggravant). Le message propose l'action : répartir, ou réveiller
  l'orchestrateur ;
* `orchestrator_held` : un orchestrateur tenu par une session interactive
  (`ameesh attach`) depuis plus du seuil, avec du courrier non lu — il ne
  répartit plus (prolonge 0030) ;
* `host_underused` : un hôte presque à vide (charge par CPU et tours en
  cours sous les seuils) sur toute la fenêtre, alors qu'un autre hôte du mesh
  est sous pression ou que des agents y attendent. **Suggestion** de
  déplacement (0028), jamais un déplacement automatique.

Lecture seule ; tout passe par `storage.of(db)`. Un seuil nul (ou négatif)
désactive l'alerte correspondante.
"""
from __future__ import annotations

import os
import time

from . import cost as cost_mod
from . import storage

UNDERUSE_TYPES = ("plan_underused", "idle_capacity", "orchestrator_held", "host_underused",
                  "backlog_empty")

#: `balance_low` (L94, ajout validé par le propriétaire) : solde bas d'un
#: fournisseur payé au token — autonomie au rythme RÉEL sous 48 h, ou solde
#: sous 20 USD ; urgente sous 12 h ou sous 5 USD. Le rythme réel vient des
#: relevés de solde (`provider_balances`), moyenne glissante sur 6 h, les
#: hausses (recharges) ignorées — jamais de l'estimation `turn_costs`.
DEFAULT_BALANCE_HOURS = 48.0
DEFAULT_BALANCE_MIN_USD = 20.0
DEFAULT_BALANCE_WINDOW_S = 6 * 3600.0
BALANCE_URGENT_HOURS = 12.0
BALANCE_URGENT_USD = 5.0
#: en deçà de cette durée couverte par les relevés, pas de rythme
BALANCE_MIN_SPAN_S = 1800.0

#: seuils par défaut
#: `plan_underused/fin_de_fenetre` : il reste moins de 24 h (au plus le quart
#: de la fenêtre : 1 h 15 pour une fenêtre de 5 h) et moins de 50 % utilisés
DEFAULT_PLAN_TAIL_S = 24 * 3600.0
DEFAULT_PLAN_USED_PCT = 50.0
#: `plan_underused/sous_rythme` : utilisation sous le rythme permis
#: (`min(90 %, part écoulée + 10 points)`, 0019) de plus de 25 points
DEFAULT_PLAN_PACE_GAP_PCT = 25.0
#: part maximale de la fenêtre prise par `fin_de_fenetre`
PLAN_TAIL_MAX_FRACTION = 0.25
#: `sous_rythme` : fenêtres d'au moins un jour, dont au moins le quart est
#: écoulé (en début de fenêtre, l'écart ne dit encore rien)
PLAN_PACE_MIN_WINDOW_S = 86400.0
PLAN_PACE_MIN_ELAPSED = 0.25
#: âge maximal des relevés de jauge lus (une fenêtre de 7 jours + marge)
PLAN_GAUGES_SINCE_S = 8 * 86400.0
#: `idle_capacity` : au repos depuis 30 min
DEFAULT_IDLE_CAPACITY_S = 1800.0
#: `orchestrator_held` : tenu par `attach` depuis 30 min
DEFAULT_ORCHESTRATOR_HELD_S = 1800.0
#: orchestrateur « qui a assigné des lots » : dans les 30 derniers jours
ASSIGNERS_SINCE_S = 30 * 86400.0
#: `host_underused` : sur 1 h, charge 1 min par CPU toujours sous 0,25 et
#: moins d'un tour en cours en moyenne
DEFAULT_HOST_UNDERUSED_S = 3600.0
DEFAULT_HOST_UNDERUSED_LOAD = 0.25
DEFAULT_HOST_UNDERUSED_TURNS = 1.0
#: un hôte sans relevé depuis 10 min n'est pas « à vide » : il est éteint
HOST_FRESH_S = 600.0
#: états de lot qui attendent un preneur (`idle_capacity`)
WAITING_LOT_STATES = ("intake", "build")
#: L165b : un lot dans ces états attend un autre que son assigné (relecture,
#: humain, autre lot) ; l'agent qui n'a que ceux-là est libre pour un lot
#: d'équipe et compte dans `idle_capacity`
LOTS_WAITING_ON_OTHERS = ("qa", "waiting_human", "blocked")
#: mots qui désignent un orchestrateur dans `roles`/`role` d'une fiche Agent
ORCHESTRATOR_ROLES = ("orchestrateur", "orchestrator")
#: L165b : rôles de l'agent de conception, prévenu aussi d'`idle_capacity`
DESIGN_ROLES = ("conception", "design")

PLAN_REASONS = ("fin_de_fenetre", "sous_rythme")
IDLE_REASONS = ("forfait_au_repos", "travail_en_attente")
HOST_REASONS = ("pression_ailleurs", "agents_en_attente")


def _duration(seconds) -> str:
    """« 52 min », « 3 h 10 », « 2 j 4 h » (même forme que L74)."""
    minutes = max(0, int(round(float(seconds) / 60.0)))
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%d h %02d" % (hours, minutes) if minutes else "%d h" % hours
    days, hours = divmod(hours, 24)
    return "%d d %d h" % (days, hours) if hours else "%d d" % days


def _alert(kind, agent, since, value, threshold, detail, **extra) -> dict:
    from .exploitation import _alert as make
    return make(kind, agent, since, value, threshold, detail, **extra)


def _state(row: dict) -> str:
    from . import progress
    return progress.agent_state(row)


def _attached(row: dict) -> bool:
    return bool(row.get("lease_live")) and str(row.get("lease_owner") or "").startswith(
        "attach:")


def _awake_runner(row: dict) -> bool:
    """Exécuteur vivant (bail d'un runner, pas d'une session attachée)."""
    return bool(row.get("lease_live")) and bool(row.get("lease_owner")) \
        and not _attached(row)


def paid_harnesses(host: str | None = None) -> tuple | None:
    """Harnais payés au token (descripteurs), ou None s'ils sont illisibles :
    on ne devine pas, le cas aggravant n'est alors pas évalué."""
    try:
        return cost_mod.paid_harnesses_of(host)
    except Exception:
        return None


def _paid_spend(db, paid, seconds: float = 86400.0) -> tuple | None:
    """`(paid, estimated)` per token over `seconds` (#325): `paid`, the fall of
    the provider balances in USD (`balance.compare`, L95) for the paid harnesses
    that have readings, else None; `estimated`, ameesh's ledger for the others."""
    if not paid:
        return None
    from . import balance
    now = time.time()
    try:
        real = [entry for entry in balance.compare(db, paid, now - seconds, now)
                if entry["provider"] in paid and entry["currency"] == "USD"]
    except Exception:
        real = []                       # balances unreadable: the estimate for all
    try:
        rest = [h for h in paid if h not in {entry["provider"] for entry in real}]
        estimated = (float(storage.of(db).turn_costs.spent(seconds, agent="all",
                                                           harnesses=rest))
                     if rest else 0.0)
    except Exception:
        return None
    return (sum(entry["real_spent"] for entry in real) if real else None), estimated


def _common_responsible(rows) -> str | None:
    counts: dict = {}
    for row in rows:
        who = (row or {}).get("responsible")
        if who:
            counts[who] = counts.get(who, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# --------------------------------------------------------------------------
# plan_underused
# --------------------------------------------------------------------------

def _gauge(row: dict) -> cost_mod.Gauge:
    return cost_mod.Gauge(row["harness"], row["key"], float(row.get("used") or 0.0),
                          row.get("resets_at_ts"), float(row.get("window_s") or 0.0)
                          or float(cost_mod.KNOWN_WINDOWS["seven_day"]))


def expiring(gauge, now: float) -> tuple | None:
    """(échéance, capacité inutilisée) de la fenêtre en cours, ou None — la
    définition de L74 (`Gauge.expiring`), reprise telle quelle tant que L74
    n'est pas fusionné : None si la fenêtre n'est pas datée ou déjà échue (la
    suivante ne court qu'au premier usage, rien n'expire)."""
    method = getattr(gauge, "expiring", None)
    if callable(method):
        return method(now)
    if not gauge.resets_at or gauge.reset_passed(now):
        return None
    return float(gauge.resets_at), max(0.0, 1.0 - float(gauge.used or 0.0))


def latest_account_gauges(db, now: float) -> dict:
    """{(harnais, compte) : [Gauge]} : le dernier relevé de chaque jauge.

    Les relevés sans compte (`account` nul : `ameesh cost gauges`, hôte sans
    comptes déclarés) d'un harnais qui a aussi des relevés de compte sont
    écartés : ce sont ceux du compte par défaut, déjà comptés."""
    rows = storage.of(db).operations.latest_gauges(since_s=PLAN_GAUGES_SINCE_S)
    named = {row["harness"] for row in rows if row.get("account")}
    out: dict = {}
    for row in rows:
        account = row.get("account") or None
        if account is None and row["harness"] in named:
            continue
        out.setdefault((row["harness"], account), []).append(
            (_gauge(row), row.get("observed_ts")))
    return out


def plan_underused(db, listing: list, now: float, *,
                   tail_s: float = DEFAULT_PLAN_TAIL_S,
                   used_pct: float = DEFAULT_PLAN_USED_PCT,
                   pace_gap_pct: float = DEFAULT_PLAN_PACE_GAP_PCT,
                   paid=None, held=()) -> list:
    used_max = max(0.0, float(used_pct)) / 100.0
    gap_min = max(0.0, float(pace_gap_pct)) / 100.0
    if (tail_s <= 0 or used_max <= 0) and gap_min <= 0:
        return []
    out: list = []
    spend = None
    spend_read = False
    for (harness, account), gauges in sorted(latest_account_gauges(db, now).items(),
                                              key=lambda kv: (kv[0][0], kv[0][1] or "")):
        # une autre fenêtre du compte au seuil : sa capacité n'est pas
        # utilisable d'ici là, rien n'est « perdu » par négligence
        live = [(g, seen) for g, seen in gauges if expiring(g, now) is not None]
        for gauge, seen in live:
            if any(other.exceeded(now) for other, _ in live if other is not gauge):
                continue
            resets_at, unused = expiring(gauge, now)
            remaining = resets_at - now
            elapsed = gauge.elapsed(now)
            cap = gauge.pace_cap(now)
            used = float(gauge.used or 0.0)
            reason = None
            tail = min(float(tail_s), PLAN_TAIL_MAX_FRACTION * gauge.window_s)
            if tail_s > 0 and used_max > 0 and remaining <= tail and used < used_max:
                reason = "fin_de_fenetre"
            elif gap_min > 0 and gauge.window_s >= PLAN_PACE_MIN_WINDOW_S \
                    and elapsed >= PLAN_PACE_MIN_ELAPSED and cap - used >= gap_min:
                reason = "sous_rythme"
            if reason is None:
                continue
            # #254 : les agents de ce harnais tous arrêtés par une pause
            # d'hôte voulue — rien n'est perdu par négligence
            agents = [row["name"] for row in listing if row.get("harness") == harness
                      and (row.get("mode") or "execute") == "execute"]
            if held and agents and all(name in held for name in agents):
                continue
            # projection linéaire : au rythme actuel, l'utilisation à la remise à zéro
            at_pace = min(1.0, used / elapsed) if elapsed > 0 else used
            lost_at_pace = max(0.0, 1.0 - at_pace)
            who = "%s/%s" % (harness, account) if account else harness
            if reason == "fin_de_fenetre":
                head = ("account %s, window %s: %.0f %% used, resets in %s"
                        % (who, gauge.key, used * 100, _duration(remaining)))
            else:
                head = ("account %s, window %s: %.0f %% used against an allowed pace of "
                        "%.0f %% (gap %.0f points), resets in %s"
                        % (who, gauge.key, used * 100, cap * 100, (cap - used) * 100,
                           _duration(remaining)))
            detail = ("%s; estimated loss at reset: %.0f %% of the window if nothing "
                      "changes (%.0f %% at the current pace)"
                      % (head, unused * 100, lost_at_pace * 100))
            idle = sorted(row["name"] for row in listing
                          if row.get("harness") == harness
                          and (row.get("mode") or "execute") == "execute"
                          and _state(row) == "idle" and _awake_runner(row)
                          and row["name"] not in held)
            if idle:
                detail += "; idle %s agents: %s" % (harness, ", ".join(idle))
            if paid and harness not in paid:
                if not spend_read:
                    spend, spend_read = _paid_spend(db, paid), True
                real, estimated = spend or (None, 0.0)
                parts = []
                if real:
                    parts.append("$%.2f paid per token over 24 h (provider balances)" % real)
                if estimated:
                    parts.append("about $%.2f estimated per token over 24 h (no usable "
                                 "balance reading)" % estimated)
                if parts:
                    detail += "; meanwhile, " + ", plus ".join(parts)
            responsible = _common_responsible(
                [row for row in listing if row.get("harness") == harness])
            out.append(_alert(
                "plan_underused", None, resets_at - gauge.window_s,
                int(round(unused * 100)),
                int(round(used_max * 100 if reason == "fin_de_fenetre" else gap_min * 100)),
                detail, harness=harness, account=account, gauge=gauge.key, reason=reason,
                used=round(used, 4), pace_cap=round(cap, 4), resets_at=round(resets_at, 3),
                lost=round(unused, 4), lost_at_pace=round(lost_at_pace, 4),
                observed_ts=round(float(seen), 3) if seen is not None else None,
                idle_agents=idle, responsible=responsible))
    return out


# --------------------------------------------------------------------------
# orchestrateurs
# --------------------------------------------------------------------------

def _bare(name: str) -> str:
    name = str(name or "").strip()
    return name[len("agent:"):] if name.startswith("agent:") else name


def role_orchestrators(canons, roles=None) -> set:
    """Les agents dont la fiche Agent du canon nomme un rôle d'orchestrateur
    (`roles` ou `role` : `orchestrateur`, `orchestrator`), ou l'un des
    `roles` donnés."""
    wanted = {str(r).strip().lower() for r in (roles or ORCHESTRATOR_ROLES)}
    found = set()
    for canon in canons or []:
        for agent in getattr(canon, "agents", None) or []:
            data = getattr(getattr(agent, "fiche", None), "data", None) or {}
            fiche_roles = data.get("roles") or data.get("role") or []
            if isinstance(fiche_roles, str):
                fiche_roles = [fiche_roles]
            if any(str(r).strip().lower() in wanted for r in fiche_roles):
                found.add(agent.title)
    return found


def declared_orchestrators(env=None) -> set:
    """Les orchestrateurs déclarés par `AMEESH_ALERT_ORCHESTRATORS` (noms
    séparés par des virgules, `agent:` admis)."""
    value = (env if env is not None else os.environ).get("AMEESH_ALERT_ORCHESTRATORS") or ""
    return {_bare(n) for n in value.split(",") if n.strip()}


def orchestrators(cfg, db, listing: list, *, declared=(), canons=None,
                  roles=ORCHESTRATOR_ROLES, history: bool = True) -> list:
    """Les orchestrateurs connus : déclarés (`--orchestrators`,
    `AMEESH_ALERT_ORCHESTRATORS`), fiche Agent du canon dont `roles` (ou
    `role`) nomme un orchestrateur (ou l'un des `roles` donnés), ou agent qui
    a confié des lots (délégation ou `work assign`) dans les 30 derniers
    jours, sauf avec `history=False` (#740). Seulement des agents du registre."""
    names = {row["name"] for row in listing}
    found = {_bare(n) for n in declared or () if n}
    if canons is None:
        from . import canon as canon_mod
        try:
            canons = canon_mod.load_configured(cfg) if cfg is not None else []
        except Exception:
            canons = []
    found |= role_orchestrators(canons, roles)
    try:
        if history:
            found |= {_bare(n) for n in storage.of(db).operations.assigners(
                since_s=ASSIGNERS_SINCE_S)}
    except Exception:
        pass
    # L123 : le chat du propriétaire transmet, il ne répartit pas
    from .chat import is_chat_agent
    chats = {row["name"] for row in listing if is_chat_agent(row)}
    return sorted(n for n in found if n in names and n not in chats)


def orchestrator_held(listing: list, now: float, orchestras: list, *,
                      held_s: float = DEFAULT_ORCHESTRATOR_HELD_S) -> list:
    if held_s <= 0:
        return []
    out = []
    for row in listing:
        if row["name"] not in orchestras or not _attached(row):
            continue
        since = row.get("status_since_ts")
        unread = int(row.get("unread") or 0)
        if since is None or now - float(since) < held_s or not unread:
            continue
        out.append(_alert(
            "orchestrator_held", row["name"], since, unread, held_s,
            "orchestrator held by an interactive session (%s) for %s, %d unread "
            "message(s): it no longer dispatches. Hand it back (quit the session: "
            "the runner takes the lease back), or handle the mail in the session"
            % (row.get("lease_owner"), _duration(now - float(since)), unread),
            owner=row.get("lease_owner"), responsible=row.get("responsible") or None))
    return out


# --------------------------------------------------------------------------
# idle_capacity
# --------------------------------------------------------------------------

def idle_since(row: dict) -> float:
    """Début du repos : le dernier changement de statut ou le dernier tour."""
    return max(float(row.get("status_since_ts") or 0.0), float(row.get("last_turn_ts") or 0.0))


def free_of_lots(row: dict, *, waiting_free: bool = False) -> bool:
    """L'agent n'a-t-il aucun lot qui l'occupe ? Strict : aucun lot assigné
    ouvert. `waiting_free` (L165b) : chacun de ses lots attend un autre que lui
    (`LOTS_WAITING_ON_OTHERS`) ou commence après aujourd'hui (`active_lots`
    nul)."""
    if not waiting_free or "active_lots" not in row:
        return row.get("assigned_lot_id") is None
    return not int(row.get("active_lots") or 0)


def session_lot_busy(row: dict, *, waiting_free: bool = False) -> bool:
    """Le lot de session de l'agent (assigné à lui ou à un autre) est-il
    encore ouvert et l'occupe-t-il ? `waiting_free` (L165b) : pas s'il attend
    un autre (`LOTS_WAITING_ON_OTHERS`) ou commence après aujourd'hui
    (`session_lot_later`). Partagé par la prise (`backlog._awaited`) et
    `idle_agents` (#318 r2)."""
    from . import stagnation
    state = row.get("session_lot_state")
    if not state or state in stagnation.DONE_STATES:
        return False
    return not (waiting_free and (state in LOTS_WAITING_ON_OTHERS
                                  or row.get("session_lot_later")))


def idle_agents(listing: list, now: float, idle_s: float, *,
                waiting_free: bool = False) -> list:
    """Les agents réveillables au repos sans lot ni courrier depuis au moins
    `idle_s` : mode `execute`, exécuteur vivant (pas une session attachée),
    au repos (ni en tour, ni en pause de budget, ni arrêtés). Partagé par
    `idle_capacity`, `backlog_empty` et la prise automatique (L119) ;
    `waiting_free` : voir `free_of_lots` (L165b) ; une consigne en attente
    et un lot de session qui l'occupe (`session_lot_busy`) occupent l'agent,
    comme pour la prise (`backlog._awaited`)."""
    idle = []
    for row in listing:
        # un agent `externe` n'est jamais compté — dont le chat du
        # propriétaire (L123), qui n'a ni exécuteur ni lot
        if (row.get("mode") or "execute") != "execute" or _state(row) != "idle":
            continue
        if not _awake_runner(row) or int(row.get("unread") or 0):
            continue
        if not free_of_lots(row, waiting_free=waiting_free):
            continue
        if waiting_free and (row.get("has_pending_prompt")
                             or session_lot_busy(row, waiting_free=True)):
            continue
        since = idle_since(row)
        if not since or now - since < idle_s:
            continue
        idle.append(row)
    return idle


def is_backlog_item(lot: dict) -> bool:
    """Élément de la file d'amélioration encore à prendre (L119) : il attend un
    preneur automatique, ce n'est ni un lot en souffrance ni un lot stagnant."""
    return (lot.get("type") == "improvement" and not (lot.get("assignee") or "").strip()
            and lot.get("state") == "intake")


def waiting_project_lots(lots: list) -> list:
    """Les lots du projet ouverts sans assigné (hors file d'amélioration) : du
    travail demandé qui attend qu'on le confie. Tant qu'il y en a, la prise
    automatique s'abstient (0037) et `idle_capacity` le signale."""
    return [lot for lot in lots
            if not (lot.get("assignee") or "").strip()
            and lot.get("state") in WAITING_LOT_STATES and not is_backlog_item(lot)]


def idle_capacity(db, listing: list, now: float, orchestras: list, *,
                  idle_s: float = DEFAULT_IDLE_CAPACITY_S, paid=None,
                  lots: list | None = None, designers=(), held=(), held_hosts=(),
                  pressured: dict | None = None) -> list:
    """#254 : `held`, les agents d'un hôte en pause, ni comptés ni à
    réveiller ; `held_hosts` (hôtes en pause) et `pressured` ({hôte:
    verdict} des hôtes sous pression, relevé frais) ne sont jamais proposés :
    leurs agents sont laissés de côté et nommés."""
    if idle_s <= 0:
        return []
    pressured = pressured or {}
    # L165b : un agent dont tous les lots attendent un autre est au repos
    idle = [row for row in idle_agents(listing, now, idle_s, waiting_free=True)
            if row["name"] not in held]
    on_hold = sorted((row["name"], row.get("host")) for row in idle
                     if row.get("host") in held_hosts)
    idle = [row for row in idle if row.get("host") not in held_hosts]
    spared = sorted((row["name"], row.get("host")) for row in idle
                    if row.get("host") in pressured)
    idle = [row for row in idle if row.get("host") not in pressured]
    if not idle:
        return []
    if lots is None:
        lots = storage.of(db).operations.open_lots_activity(500)
    waiting_lots = waiting_project_lots(lots)
    backlog = [row for row in listing
               if _state(row) in ("working", "paused") and int(row.get("unread") or 0)
               and row.get("oldest_unread_ts") is not None
               and now - float(row["oldest_unread_ts"]) >= idle_s]
    paid_working = sorted(row["name"] for row in listing
                          if paid and row.get("harness") in paid
                          and _state(row) == "working" and not _attached(row))
    plan_idle = [row for row in idle if paid is not None and row.get("harness") not in paid]
    aggravating = bool(paid_working and plan_idle)
    if not (waiting_lots or backlog or aggravating):
        return []
    names = sorted(row["name"] for row in idle)
    parts = ["%d wakeable agent(s) idle without a lot in progress for more than %s: %s"
             % (len(names), _duration(idle_s), ", ".join(names))]
    if waiting_lots:
        parts.append("%d open lot(s) without an assignee (%s)" % (
            len(waiting_lots), ", ".join("#%d" % int(l["id"]) for l in waiting_lots[:8])
            + (", …" if len(waiting_lots) > 8 else "")))
    if backlog:
        parts.append("mail waiting at %s" % ", ".join(
            sorted(row["name"] for row in backlog)))
    if on_hold:
        parts.append("left out, their host on hold: %s" % ", ".join(
            "%s on %s" % pair for pair in on_hold))
    if spared:
        parts.append("left out, their host under pressure: %s" % "; ".join(
            "%s on %s (%s)" % (name, host, ", ".join(
                "%s %s, threshold %s" % (b["label"], b["value"], b["limit"])
                for b in pressured[host]["breaches"][:2]))
            for name, host in spared))
    if aggravating:
        parts.append("plan agents idle (%s) while per-token agents "
                     "are working (%s)" % (", ".join(sorted(r["name"] for r in plan_idle)),
                                          ", ".join(paid_working)))
    by_name = {row["name"]: row for row in listing}
    # #254 : un orchestrateur arrêté avec son hôte en pause n'est pas à réveiller
    attached = [n for n in orchestras if _attached(by_name.get(n) or {})]
    asleep = [n for n in orchestras if n not in attached and n not in held
              and _state(by_name.get(n) or {}) in ("stopped", "paused")]
    if attached:
        action = ("wake the orchestrator: %s is held by an interactive session "
                  "(attach), hand it back; or dispatch by hand "
                  "(ameesh work assign <lot> <agent>)" % ", ".join(attached))
    elif asleep:
        action = ("wake the orchestrator %s (stopped or paused), or dispatch by hand "
                  "(ameesh work assign <lot> <agent>)" % ", ".join(asleep))
    else:
        action = "dispatch: ameesh work assign <lot> <agent>, or write to the orchestrator"
    detail = "; ".join(parts) + ". Action: " + action
    responsible = None
    for name in attached + asleep + list(orchestras):
        responsible = (by_name.get(name) or {}).get("responsible")
        if responsible:
            break
    responsible = responsible or _common_responsible(idle)
    return [_alert(
        "idle_capacity", None, None, len(names), idle_s, detail,
        reason="forfait_au_repos" if aggravating else "travail_en_attente",
        agents=names, lots=[int(l["id"]) for l in waiting_lots[:50]],
        paid_working=paid_working, orchestrators=list(orchestras),
        designers=[n for n in designers if n not in orchestras],
        responsible=responsible)]


def orchestrator_briefs(alert: dict, listing: list, lot_titles: dict | None = None) -> dict:
    """L118 : `idle_capacity` part aussi aux orchestrateurs, qui peuvent agir,
    et (L165b) aux agents de conception.

    Rend `{orchestrateur: texte}` : à chacun, les agents au repos sans lot de
    SON équipe (tous s'il n'en a pas) et les lots ouverts sans agent (un lot
    ne dit pas son équipe : tous). Un orchestrateur sans agent à occuper ne
    reçoit rien ; il n'est jamais cité dans sa propre liste."""
    if alert.get("type") != "idle_capacity":
        return {}
    by_name = {row["name"]: row for row in listing}
    titles = lot_titles or {}
    lots = [int(l) for l in alert.get("lots") or ()]
    out = {}
    for orch in list(alert.get("orchestrators") or ()) + list(alert.get("designers") or ()):
        team = ((by_name.get(orch) or {}).get("team") or "").strip()
        names = [n for n in alert.get("agents") or () if n != orch and (
            not team or ((by_name.get(n) or {}).get("team") or "").strip() == team)]
        if not names:
            continue
        parts = ["Idle capacity: %d wakeable agent(s) without a lot in progress for more "
                 "than %s: %s." % (len(names), _duration(alert.get("threshold") or 0), ", ".join(names))]
        if lots:
            parts.append("Open lots without an agent: %s." % ", ".join(
                "#%d%s" % (i, " \"%s\"" % titles[i] if titles.get(i) else "")
                for i in lots[:12]) + (" …" if len(lots) > 12 else ""))
        else:
            parts.append("No open lot without an agent.")
        parts.append("Give them work tied to a lot: ameesh mail send <agent> "
                     "\"…\" --lot <id> (or --new-lot \"title\"), or ameesh work assign <lot> "
                     "<agent>.")
        out[orch] = " ".join(parts)
    return out


# --------------------------------------------------------------------------
# host_underused
# --------------------------------------------------------------------------

def host_verdicts(db, canons) -> dict:
    """{hôte: (dernier relevé, verdict de pression)}, aux limites physiques
    des fiches Host de tous les canons."""
    from . import resources as resources_mod
    verdicts = {}
    for reading in storage.of(db).hosts.current(None):
        try:
            limits = resources_mod.host_limits(canons, reading["host"])["limits"]
        except Exception:
            limits = None
        verdicts[reading["host"]] = (reading, resources_mod.pressure(reading, limits=limits))
    return verdicts


def pressured_hosts(verdicts: dict, now: float) -> dict:
    """#254 : {hôte: verdict} des hôtes sous pression d'après un relevé
    frais (un relevé vieux ne dit rien de l'hôte maintenant)."""
    return {host: verdict for host, (reading, verdict) in verdicts.items()
            if verdict["blocked"] and reading.get("sampled_ts") is not None
            and now - float(reading["sampled_ts"]) <= HOST_FRESH_S}


def host_underused(cfg, db, listing: list, now: float, *,
                   window_s: float = DEFAULT_HOST_UNDERUSED_S,
                   load_max: float = DEFAULT_HOST_UNDERUSED_LOAD,
                   turns_max: float = DEFAULT_HOST_UNDERUSED_TURNS,
                   idle_mail_s: float = 300.0, canons=None) -> list:
    if window_s <= 0 or load_max <= 0:
        return []
    hosts = storage.of(db).hosts
    if canons is None:
        from . import canon as canon_mod
        try:
            canons = canon_mod.load_configured(cfg) if cfg is not None else []
        except Exception:
            canons = []
    verdicts = host_verdicts(db, canons)
    out = []
    for host, (reading, verdict) in sorted(verdicts.items()):
        if verdict["blocked"]:
            continue
        if reading.get("sampled_ts") is None or now - float(reading["sampled_ts"]) > HOST_FRESH_S:
            continue
        pressured = [(h, v) for h, (_r, v) in sorted(verdicts.items())
                     if h != host and v["blocked"]]
        waiting = sorted(
            row["name"] for row in listing
            if (row.get("host") or "") != host and (row.get("mode") or "execute") == "execute"
            and (_state(row) == "paused"
                 or (_state(row) == "idle" and int(row.get("unread") or 0)
                     and row.get("oldest_unread_ts") is not None
                     and now - float(row["oldest_unread_ts"]) >= idle_mail_s)))
        if not pressured and not waiting:
            continue
        usage = hosts.usage(host, window_s)
        if not usage or not usage.get("samples") or usage.get("first_ts") is None:
            continue
        if now - float(usage["first_ts"]) < 0.9 * window_s:
            continue                    # fenêtre pas encore couverte par les relevés
        peak = usage.get("max_load_per_cpu")
        turns = float(usage.get("avg_turns") or 0.0)
        if peak is None or float(peak) >= load_max or turns >= turns_max:
            continue
        why = []
        if pressured:
            why.append("%s under pressure (%s)" % (", ".join(h for h, _v in pressured), "; ".join(
                "%s %s (threshold %s)" % (b["label"], b["value"], b["limit"])
                for _h, v in pressured for b in v["breaches"][:2])))
        if waiting:
            why.append("agents waiting elsewhere: %s" % ", ".join(waiting))
        detail = ("host %s underused for %s (load at most %.2f per CPU, %.1f turn(s) "
                  "running on average) while %s. Suggestion: place admitted agents there "
                  "(ameesh placement check --agent <name>, decision 0028); nothing is moved "
                  "automatically" % (host, _duration(window_s), float(peak), turns,
                                   "; ".join(why)))
        out.append(_alert(
            "host_underused", None, None, round(float(peak), 3), load_max, detail,
            host=host, reason="pression_ailleurs" if pressured else "agents_en_attente",
            pressured=[h for h, _v in pressured], waiting=waiting,
            avg_turns=round(turns, 2), samples=int(usage["samples"])))
    return out


# --------------------------------------------------------------------------
# balance_low
# --------------------------------------------------------------------------

def _local_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def balance_rate(points: list) -> tuple | None:
    """(dépense, durée couverte en s) d'une série de soldes chronologique :
    somme des baisses entre relevés successifs ; une hausse est une recharge,
    ignorée. None si la série couvre moins de `BALANCE_MIN_SPAN_S`."""
    if len(points) < 2:
        return None
    span = float(points[-1]["observed_ts"]) - float(points[0]["observed_ts"])
    if span < BALANCE_MIN_SPAN_S:
        return None
    spent = sum(max(0.0, float(a["total"]) - float(b["total"]))
                for a, b in zip(points, points[1:]))
    return spent, span


def balance_low(db, listing: list, now: float, *,
                hours: float = DEFAULT_BALANCE_HOURS,
                min_usd: float = DEFAULT_BALANCE_MIN_USD,
                window_s: float = DEFAULT_BALANCE_WINDOW_S) -> list:
    if hours <= 0 and min_usd <= 0:
        return []
    rows = storage.of(db).operations.balances(provider=None,
                                              since_s=max(window_s, BALANCE_MIN_SPAN_S))
    series: dict = {}
    for row in rows:
        series.setdefault((row["provider"], row["currency"], row.get("account")),
                          []).append(row)
    out = []
    for (provider, currency, account), points in sorted(
            series.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
        points.sort(key=lambda r: float(r["observed_ts"]))
        last = points[-1]
        total = float(last["total"])
        measured = balance_rate(points)
        rate = measured[0] / measured[1] * 3600.0 if measured else None
        autonomy_h = (total / rate) if rate else None
        causes = []
        if hours > 0 and autonomy_h is not None and autonomy_h < hours:
            causes.append("autonomie")
        usd = currency.upper() == "USD"
        if min_usd > 0 and usd and total < min_usd:
            causes.append("solde")
        if not causes:
            continue
        who = "%s/%s" % (provider, account) if account else provider
        parts = ["balance %s: %.2f %s (read on %s)" % (
            who, total, currency, _local_time(last["observed_ts"]))]
        if rate is not None:
            parts.append("actual rate %.2f %s/h (average over %s, top-ups ignored)"
                         % (rate, currency, _duration(measured[1])))
        else:
            parts.append("actual rate unknown (not enough readings)")
        empty_ts = None
        if autonomy_h is not None:
            empty_ts = float(last["observed_ts"]) + autonomy_h * 3600.0
            parts.append("runway ≈ %s, expected to run out on %s"
                         % (_duration(autonomy_h * 3600.0), _local_time(empty_ts)))
        elif rate == 0:
            parts.append("no spending seen over the window")
        parts.append("top up the account, or steer work to the plans")
        urgent = ((autonomy_h is not None and autonomy_h < BALANCE_URGENT_HOURS)
                  or (usd and total < BALANCE_URGENT_USD))
        out.append(_alert(
            "balance_low", None, None, round(total, 4),
            min_usd if causes == ["solde"] else hours, "; ".join(parts),
            provider=provider, currency=currency, account=account, causes=causes,
            balance=round(total, 4), rate_per_h=round(rate, 4) if rate is not None else None,
            autonomy_h=round(autonomy_h, 2) if autonomy_h is not None else None,
            empty_ts=round(empty_ts, 3) if empty_ts is not None else None,
            observed_ts=round(float(last["observed_ts"]), 3), urgent=bool(urgent),
            responsible=_common_responsible(
                [r for r in listing if r.get("harness") == provider])))
    return out


# --------------------------------------------------------------------------
# backlog_empty (L119, décision 0037)
# --------------------------------------------------------------------------

def backlog_empty(db, listing: list, now: float, *,
                  idle_s: float = DEFAULT_IDLE_CAPACITY_S) -> list:
    """`backlog_empty` : des agents réveillables sont au repos sans lot depuis
    plus de `idle_s` et la file d'amélioration n'a plus d'élément à prendre.
    ameesh ne doit s'arrêter que faute de travail : l'humain responsable est
    invité à remplir la file (`ameesh work backlog add`)."""
    if idle_s <= 0:
        return []
    idle = idle_agents(listing, now, idle_s)
    if not idle:
        return []
    if storage.of(db).work.backlog(open_only=True, limit=1):
        return []
    # des lots du projet attendent un preneur : c'est `idle_capacity` qui parle
    if waiting_project_lots(storage.of(db).operations.open_lots_activity(500)):
        return []
    names = sorted(row["name"] for row in idle)
    return [_alert(
        "backlog_empty", None, None, len(names), idle_s,
        "%d wakeable agent(s) idle without a lot for more than %s (%s) and the improvement "
        "backlog is empty. Action: add items with expected value "
        "(ameesh work backlog add), or assign a lot (ameesh work assign)"
        % (len(names), _duration(idle_s), ", ".join(names)),
        agents=names, responsible=_common_responsible(idle))]


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def alerts(cfg, db, listing: list, now: float | None = None, *,
           plan_tail_s: float = DEFAULT_PLAN_TAIL_S,
           plan_used_pct: float = DEFAULT_PLAN_USED_PCT,
           plan_pace_gap_pct: float = DEFAULT_PLAN_PACE_GAP_PCT,
           idle_capacity_s: float = DEFAULT_IDLE_CAPACITY_S,
           orchestrator_held_s: float = DEFAULT_ORCHESTRATOR_HELD_S,
           orchestrators_declared=(),
           host_underused_s: float = DEFAULT_HOST_UNDERUSED_S,
           host_underused_load: float = DEFAULT_HOST_UNDERUSED_LOAD,
           host_underused_turns: float = DEFAULT_HOST_UNDERUSED_TURNS,
           balance_hours: float = DEFAULT_BALANCE_HOURS,
           balance_min_usd: float = DEFAULT_BALANCE_MIN_USD,
           balance_window_s: float = DEFAULT_BALANCE_WINDOW_S,
           backlog_empty_s: float = DEFAULT_IDLE_CAPACITY_S,
           idle_mail_s: float = 300.0, held=(), held_hosts=()) -> list:
    """Les alertes de sous-utilisation en cours (non triées). `held` (#254) :
    les agents d'un hôte en pause voulue."""
    now = time.time() if now is None else float(now)
    from . import canon as canon_mod
    try:
        canons = canon_mod.load_configured(cfg) if cfg is not None else []
    except Exception:
        canons = []
    paid = paid_harnesses(getattr(cfg, "host", None))
    out = plan_underused(db, listing, now, tail_s=plan_tail_s, used_pct=plan_used_pct,
                         pace_gap_pct=plan_pace_gap_pct, paid=paid, held=held)
    orchestras = orchestrators(cfg, db, listing, declared=orchestrators_declared,
                               canons=canons)
    for alert in out:
        # L132 : `plan_underused` part aussi aux orchestrateurs (alert_briefs)
        alert["orchestrators"] = list(orchestras)
    out += orchestrator_held(listing, now, orchestras, held_s=orchestrator_held_s)
    names = {row["name"] for row in listing}
    designers = sorted(n for n in role_orchestrators(canons, DESIGN_ROLES) if n in names)
    try:
        pressured = pressured_hosts(host_verdicts(db, canons), now)
    except Exception:
        pressured = {}
    out += idle_capacity(db, listing, now, orchestras, idle_s=idle_capacity_s, paid=paid,
                         designers=designers, held=held, held_hosts=held_hosts,
                         pressured=pressured)
    out += host_underused(cfg, db, listing, now, window_s=host_underused_s,
                          load_max=host_underused_load, turns_max=host_underused_turns,
                          idle_mail_s=idle_mail_s, canons=canons)
    out += balance_low(db, listing, now, hours=balance_hours, min_usd=balance_min_usd,
                       window_s=balance_window_s)
    out += backlog_empty(db, listing, now, idle_s=backlog_empty_s)
    return out
