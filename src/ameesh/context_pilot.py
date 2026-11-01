# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote réversible de la politique de contexte d'un agent (CTX-06, #5250).

``ameesh context pilot`` : ``apply`` capture le profil initial et pose les valeurs
dans la même instruction ; ``revert`` les rétablit, effectif au tour suivant (le
point sûr : la rotation éventuelle se décide entre deux tours) ; ``status`` dit si
le dernier tour a pris les valeurs ; ``check`` évalue les motifs de retour de
l'engagement e23 et agit.

L'état vit dans ``agent_registry.context_pilot`` : un rôle élu garde son pilote
quel que soit l'hôte, et plusieurs ``check`` concurrents ne font qu'un retour.
Les seuils viennent des arguments : rien de propre à un agent ou un client ici.

Une mesure inconnue reste inconnue et ne décide rien, mais elle est publiée ; seul
un fait observé déclenche un retour.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time

from . import config as config_mod, db as db_mod, exploitation, mail, storage, work
from .mesh_cli import parse_token_count

SCHEMA = "ameesh-context-pilot/1"
SETTINGS = ("context_max_tokens", "session_max_tokens", "turn_reread_budget",
            "session_reread_budget")
#: e23 : hausse admise du coût par action, et au moins 10 actions par fenêtre.
COST_MARGIN = 1.20
MIN_ACTIONS = 10
#: sans publication de `turn.context` sur cette fenêtre, malgré des tours : monitoring perdu.
MONITORING_WINDOW_S = 3600.0
MONITORING_MIN_TURNS = 3
CLOCK_MARGIN_S = 30.0
#: alertes de l'agent ou de son hôte qui disent une garde franchie.
GUARD_ALERTS = frozenset({"account_overage", "overage_in_use", "guard_restart"})


def _open(state: dict | None) -> bool:
    return bool(state) and state.get("returned_ts") is None


def over_cost(window: dict, baseline: float) -> bool:
    """La fenêtre est-elle recevable (≥ 10 actions) et au-delà de +20 % ?"""
    actions = window.get("actions")
    cost = window.get("cost")
    if not isinstance(actions, int) or actions < MIN_ACTIONS \
            or not isinstance(cost, (int, float)) or baseline <= 0:
        return False
    return cost / actions > baseline * COST_MARGIN


def cost_verdict(windows: list, baseline: float | None) -> str | None:
    """``return`` sur deux fenêtres consécutives au-delà, ``fallback`` sur la
    dernière seule, sinon None. Une fenêtre sous l'effectif n'est pas lue, et ne
    sépare pas deux fenêtres recevables."""
    if not baseline:
        return None
    usable = [w for w in windows
              if isinstance(w.get("actions"), int) and w["actions"] >= MIN_ACTIONS]
    if not usable or not over_cost(usable[-1], baseline):
        return None
    if len(usable) >= 2 and over_cost(usable[-2], baseline):
        return "return"
    return "fallback"


def overlaps(turns: list[dict]) -> list[tuple]:
    """Deux tours du même agent qui se chevauchent sur deux hôtes : double bail.
    La marge absorbe l'écart d'horloge entre hôtes, à la relève du bail."""
    bounded = sorted((t for t in turns if t.get("started_ts") and t.get("ended_ts")
                      and t.get("host")), key=lambda t: t["started_ts"])
    found = []
    for before, after in zip(bounded, bounded[1:]):
        if after["started_ts"] + CLOCK_MARGIN_S < before["ended_ts"] and after["host"] != before["host"]:
            found.append((before["host"], after["host"], after["started_ts"]))
    return found


def _context_payloads(db, agent: str, since: float, until: float) -> list[dict]:
    """Les `turn.context` de l'agent sur la fenêtre, lus jusqu'au bout."""
    out, after = [], 0
    while True:
        rows = storage.of(db).events.context_events(since, until, 1000, after_id=after)
        if not rows:
            return out
        out += [r["data"] for r in rows if isinstance(r.get("data"), dict)
                and r["data"].get("agent") == agent]
        after = int(rows[-1]["id"])
        if len(rows) < 1000:
            return out


def _turn_bounds(payload: dict) -> dict:
    return {"started_ts": payload.get("started_ts"), "ended_ts": payload.get("ended_ts"),
            "host": payload.get("host")}


def _oom_seen(agent: str, since: float) -> bool | None:
    """L'unité locale de l'agent a-t-elle vu un OOM ? None si illisible (hors systemd)."""
    unit = "ameesh-runner-agent@%s.service" % agent
    try:
        shown = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "Result"],
            capture_output=True, text=True, timeout=20, check=True).stdout
        log = subprocess.run(
            ["journalctl", "--user", "-u", unit, "--since", "@%d" % int(since),
             "--no-pager", "-q", "-g", "oom-kill|Out of memory"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if "Result=oom-kill" in shown:
        return True
    # journalctl -g sort en 1 quand rien ne correspond : une réponse, pas une panne.
    return bool(log.stdout.strip()) if log.returncode in (0, 1) else None


def _cost_windows(command: str | None) -> list | None:
    """Les fenêtres d'une heure de la procédure de référence, ou None si illisibles."""
    if not command:
        return None
    try:
        done = subprocess.run(shlex.split(command), capture_output=True, text=True,
                              timeout=300, check=True)
        windows = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return windows if isinstance(windows, list) else None


def collect(cfg, db, name: str, state: dict, now: float,
            cost_command: str | None = None) -> dict:
    """Les faits du contrôle ; None = inconnu, jamais un motif de retour."""
    since = float(state["applied_ts"])
    payloads = _context_payloads(db, name, since, now)
    recent = _context_payloads(db, name, max(since, now - MONITORING_WINDOW_S), now)
    from . import registry
    host = (registry.get(db, name) or {}).get("host")
    # Une alerte d'avant la pose (garde relancée la veille) n'est pas un effet du pilote.
    alerts = [a for a in exploitation.alerts(cfg, db)
              if a.get("type") in GUARD_ALERTS
              and (a.get("agent") == name or (host and a.get("host") == host))
              and float(a.get("since") or 0) >= since]
    turns = storage.of(db).turn_costs.count_since(name, now - MONITORING_WINDOW_S)
    monitoring_lost = (now - since >= MONITORING_WINDOW_S
                       and turns >= MONITORING_MIN_TURNS and not recent)
    return {
        "guards": [a.get("type") for a in alerts],
        "double_lease": overlaps([_turn_bounds(p) for p in payloads]),
        "oom": _oom_seen(name, since),
        "monitoring_lost": monitoring_lost,
        "cost_windows": _cost_windows(cost_command),
    }


def verdict(facts: dict, state: dict) -> tuple[str | None, str]:
    """(``return`` | ``fallback`` | None, motif), dans l'ordre de gravité d'e23."""
    if facts.get("guards"):
        return "return", "guard: %s" % ", ".join(sorted(set(facts["guards"])))
    if facts.get("double_lease"):
        return "return", "double lease: %r" % (facts["double_lease"][0],)
    if facts.get("oom"):
        return "return", "OOM on the agent's unit"
    if facts.get("monitoring_lost"):
        return "return", "monitoring lost: turns without turn.context"
    outcome = cost_verdict(facts.get("cost_windows") or [], state.get("baseline_cost"))
    if outcome == "return":
        return "return", "cost per action above +20 % on two windows"
    if outcome == "fallback" and not state.get("fallback_ts") \
            and state.get("fallback_turn_budget"):
        return "fallback", "cost per action above +20 % on one window"
    return None, ""


def _tell(db, name: str, state: dict, text: str, actor: str) -> None:
    """Prévenir l'agent et le lot ; un avis perdu n'empêche jamais le retour."""
    try:
        mail.send(db, "ameesh", name, text, kind="event", payload={"context_pilot": SCHEMA})
    except Exception as exc:  # noqa: BLE001
        print("notice to %s not sent: %s" % (name, type(exc).__name__), file=sys.stderr)
    if state.get("lot"):
        try:
            work.note(db, int(state["lot"]), text, actor=actor)
        except Exception as exc:  # noqa: BLE001
            print("lot note not written: %s" % type(exc).__name__, file=sys.stderr)


def apply_pilot(cfg, db, args, now: float) -> int:
    row = storage.of(db).operations.listing()
    row = next((r for r in row if r["name"] == args.agent), None)
    if row is None:
        print("unknown agent: %s" % args.agent, file=sys.stderr)
        return 1
    if cfg.window_maintenance_share > 0:
        print("window maintenance is on for this executor: the pilot keeps it off",
              file=sys.stderr)
        return 2
    values = {"context_max_tokens": args.context_max, "session_max_tokens": args.session_max,
              "turn_reread_budget": args.turn_budget,
              "session_reread_budget": args.session_budget}
    state = {
        "schema": SCHEMA, "applied_ts": now, "returned_ts": None, "lot": args.lot,
        "ref": args.ref, "initial": {key: row.get(key) for key in SETTINGS},
        "applied": values, "fallback_turn_budget": args.fallback_turn_budget,
        "fallback_ts": None, "baseline_cost": args.baseline_cost,
    }
    if args.dry_run:
        print(json.dumps(state, indent=2))
        return 0
    if not storage.of(db).operations.write_context_pilot(
            args.agent, values, state, opening=True):
        print("a pilot is already open on %s" % args.agent, file=sys.stderr)
        return 1
    _tell(db, args.agent, state, "context pilot applied (%s), effective at the next "
          "turn: %s; initial profile kept for the return: %s"
          % (args.ref, values, state["initial"]), args.actor)
    print(json.dumps(state, indent=2))
    return 0


def _return(db, name: str, state: dict, reason: str, actor: str, now: float) -> bool:
    closed = dict(state, returned_ts=now, return_reason=reason)
    if not storage.of(db).operations.write_context_pilot(
            name, state["initial"], closed, opening=False):
        return False
    _tell(db, name, closed, "context pilot returned to the initial profile (%s): %s; "
          "effective at the next turn" % (reason, state["initial"]), actor)
    return True


def status(cfg, db, name: str, now: float) -> dict | None:
    row = _state_of(db, name)
    if not row:
        return None
    # Le plafond de contexte et les budgets publiés par le dernier tour d'après
    # la pose : la preuve de prise en compte, pas l'écriture du réglage.
    since = float(row.get("fallback_ts") or row["applied_ts"])
    payloads = _context_payloads(db, name, since, now)
    policy = ((payloads[-1].get("decision") or {}).get("policy") or {}) if payloads else None
    wanted = row["initial"] if row.get("returned_ts") else row["applied"]
    effective = None if policy is None else all(
        int(policy.get(key) or 0) == int(wanted[key] or 0) for key in SETTINGS
        if wanted.get(key) is not None)
    return {"schema": SCHEMA, "open": _open(row), "state": row, "turns_seen": len(payloads),
            "last_policy": policy, "effective": effective}


def _state_of(db, name: str) -> dict | None:
    from . import registry
    state = (registry.get(db, name) or {}).get("context_pilot")
    return state if isinstance(state, dict) else None


def check(cfg, db, name: str, actor: str, now: float,
          cost_command: str | None = None) -> dict:
    state = _state_of(db, name)
    if not _open(state):
        return {"schema": SCHEMA, "open": False, "action": None}
    facts = collect(cfg, db, name, state, now, cost_command)
    action, reason = verdict(facts, state)
    if action == "return":
        action = "return" if _return(db, name, state, reason, actor, now) else None
    elif action == "fallback":
        budget = int(state["fallback_turn_budget"])
        moved = dict(state, fallback_ts=now,
                     applied=dict(state["applied"], turn_reread_budget=budget))
        if storage.of(db).operations.write_context_pilot(
                name, {"turn_reread_budget": budget}, moved, opening=False):
            _tell(db, name, moved, "context pilot cost fallback (%s): turn_reread_budget=%d"
                  % (reason, budget), actor)
        else:
            action = None
    return {"schema": SCHEMA, "open": True, "action": action, "reason": reason,
            "facts": facts}


def _tokens(text: str) -> int:
    return parse_token_count(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh context pilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    apply_ = sub.add_parser("apply", help="capture the initial profile, set the values")
    apply_.add_argument("--agent", required=True)
    apply_.add_argument("--context-max", type=_tokens, required=True)
    apply_.add_argument("--session-max", type=_tokens, required=True)
    apply_.add_argument("--turn-budget", type=_tokens, required=True)
    apply_.add_argument("--session-budget", type=_tokens, required=True)
    apply_.add_argument("--fallback-turn-budget", type=_tokens, default=None)
    apply_.add_argument("--baseline-cost", type=float, default=None,
                        help="reference cost per admissible action")
    apply_.add_argument("--lot", type=int, default=None)
    apply_.add_argument("--ref", required=True, help="the decision that allows it")
    apply_.add_argument("--dry-run", action="store_true")
    for name, text in (("revert", "back to the initial profile"),
                       ("status", "state, and whether the last turn took the values"),
                       ("check", "evaluate the return criteria and act")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--agent", required=True)
        if name == "revert":
            command.add_argument("--reason", required=True)
        if name == "check":
            # Sur la ligne de la minuterie, jamais en base : le registre ne dicte
            # pas ce que l'hôte exécute.
            command.add_argument(
                "--cost-command", default=None,
                help="prints a JSON list of hourly windows {actions, cost}, oldest first")
    parser.add_argument("--actor", default="ameesh")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    db = db_mod.connect(cfg)
    now = time.time()
    try:
        db_mod.require_schema(db)
        if args.command == "apply":
            return apply_pilot(cfg, db, args, now)
        if args.command == "revert":
            state = _state_of(db, args.agent)
            if not _open(state):
                print("no open pilot on %s" % args.agent, file=sys.stderr)
                return 1
            return 0 if _return(db, args.agent, state, args.reason, args.actor, now) else 1
        if args.command == "status":
            print(json.dumps(status(cfg, db, args.agent, now), indent=2, default=str))
            return 0
        print(json.dumps(check(cfg, db, args.agent, args.actor, now, args.cost_command),
                         indent=2, default=str))
        return 0
    finally:
        db.close()
