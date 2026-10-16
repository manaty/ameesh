#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""agent-mesh — the mesh CLI: observability, keys, approvals, work items.

  agent-mesh list [--json]                     mesh list: agents, project, host, lease, unread,
                                                budget, grouped by project (L62); status
                                                prefixed "ext/": external agent (L37)
  agent-mesh show <agent> [--json]             details of an agent
  agent-mesh key generate --out DIR --i-am-the-owner   key pair (an act of the owner)
  agent-mesh key register <agent> --public-key FILE [--role owner|agent]
  agent-mesh key show <agent> | key list | key revoke <agent>
  agent-mesh approve --key FILE --action A --hash H [--kind K] [--expires 48h]
                                                signs an approval bound to an artifact
  agent-mesh approvals [--action A] [--hash H] [--json]
  agent-mesh verify --action A --hash H [--consume] [--by NAME]
                                                does a valid approval exist?
  agent-mesh cost report | cost spent <agent|all> [s] | cost over <agent>
                                                turn costs and plan gauges
  agent-mesh work add --title T [--type bug|evolution] [--app A] [--assignee N] …
  agent-mesh work list [--state S] | work show <id> | work move <id> <state> | work note <id> "…"
  agent-mesh work assign <id> <agent> [--externe] [--actor A]   reassign (guarded assignment)
  agent-mesh work merged <id> --sha S [--note …]   merge done outside a PR: any open state → merged
  agent-mesh work migration <id> [--number N | --release] [--skip-branch B]   reserve the work item's migration number (L122)
  agent-mesh work issue-ref <id> owner/repo#N | --none   correct the issue reference, journaled (#1603)
  agent-mesh work resolve <id> --with <other> [--done | --abort]   prepared conflict resolution (L160d)
  agent-mesh work queue add|remove <id> | show [--target T]   merge queue per target (L160e)
  agent-mesh work land <id> [--project P]   land the queue head now (L160e2)
  agent-mesh work move <id> merged --correct "reason"   correct a promoted state set by mistake
                                                (the actor of work commands: --actor, otherwise
                                                the session's bound identity, otherwise "inconnu")
  agent-mesh import-v0 [--agents a,b] [--dry-run]     cutover: v0 file mailbox → Postgres
  agent-mesh export-v0 [--agents a,b] [--keep]        rollback: Postgres → v0 mailbox
  agent-mesh migrate [--dry-run | --additive-only]   --additive-only: nothing if a migration is not additive (L151)
  agent-mesh doctor [--notify-test | --probe]
  agent-mesh canon check|show|sync [--json] [--host H]   OKF canon (spec §4)
  agent-mesh placement check [--agent A] [--json]        admissions and admissible hosts (C4)
  agent-mesh hosts [--json] [HOST] [--history N]         host resources (L31)
  agent-mesh housekeeping [--dry-run | --apply] [--json] agent cleanup (L73)
  agent-mesh conflicts [--project P] [--all] [--json]   conflict radar between work items (L160b)
  agent-mesh ci status <item> [--limit N] [--json]      live CI, failures (L161c)
  agent-mesh agent spawn <name> --by <creator> --ttl 2h   ephemeral agent (R14)

The owner's authority is never inferred from text: it is proven by an Ed25519
signature whose public key is in agent_registry, bound to the content and to
an expiry. `key generate` is an act of the owner: no agent may generate or
hold its private key.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time

from . import accounts as accounts_mod
from . import balance as balance_mod
from . import authority, catalog, mail, config as config_mod, cost as cost_mod, db as db_mod, identity
from . import migrations, placement, registry
from . import fil, release, signing, storage, work
from .config import Config
from .db import Db
from .deprecated import HiddenAliasParser

USAGE_HINT = "agent-mesh: %s"


def _open(cfg: Config) -> Db:
    # L61 : la vérification de schéma part avec la première requête
    return db_mod.open_db(cfg)


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def _fmt_span(seconds: float | None) -> str:
    """Une durée (pas un âge) : 42s, 12m, 3h05, 2j."""
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02d" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd" % (seconds // 86400)


#: unités binaires, du plus grand au plus petit
_BYTE_UNITS = (("TiB", 1024 ** 4), ("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024))


def _fmt_bytes(value: int | None) -> str:
    """Une taille lisible : « 3.2 GiB », « 0 B », « — » si inconnue."""
    if value is None:
        return "—"
    value = int(value)
    for label, factor in _BYTE_UNITS:
        if abs(value) >= factor:
            return "%.1f %s" % (value / factor, label)
    return "%d B" % value



def _fmt_moment(epoch: float | None) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _budget(row: dict) -> str:
    budget = row.get("budget_usd")
    spent = row.get("spent_usd")
    if budget is None:
        return "—" if not spent else "%.4f/—" % float(spent)
    return "%.4f/%.2f" % (float(spent or 0), float(budget))


def _lease(row: dict) -> str:
    owner = row.get("lease_owner")
    if not owner:
        return "—"
    remaining = float(row.get("lease_expires_ts") or 0) - time.time()
    return "%s (%ds)" % (owner[:18], max(0, int(remaining)))


# --------------------------------------------------------------------------
# mesh list / show  (spec §5 point 5)
# --------------------------------------------------------------------------

def _annotate_agents(cfg: Config, db, rows: list) -> list:
    """The fields of `list --json` added to `registry.overview` rows."""
    # C4 : verdict de placement de chaque agent (0022)
    placement.annotate(db, rows)
    # L26 : champs d'exploitation (schéma `ameesh-agent/1`,
    # docs/EXPLOITATION.md), ajoutés sans retirer les clés existantes
    from . import exploitation
    exploitation.annotate(cfg, db, rows)
    return rows


def agent_rows(cfg: Config, db) -> list:
    """The rows of `ameesh list --json` (also `GET /api/v1/agents`, L84)."""
    from .projects import agent_project
    rows = registry.overview(db)
    for row in rows:
        row["project"] = agent_project(row)
    return _annotate_agents(cfg, db, rows)


def agent_detail(db, name: str) -> dict | None:
    """The object of `ameesh show --json` (also `GET /api/v1/agents/{a}`)."""
    # L61 : la ligne et la clé en un seul aller-retour (db.batched)
    row, info = db_mod.batched(db, lambda db: (
        registry.get(db, name), authority.key_info(db, name)))
    if row is None:
        return None
    row = dict(row)
    row["key_ready"] = bool(info and info.get("public_key") and not info.get("key_revoked_ts"))
    row["public_key_fingerprint"] = (info or {}).get("public_key_fingerprint")
    row["key_updated_ts"] = (info or {}).get("key_updated_ts")
    row["key_revoked_ts"] = (info or {}).get("key_revoked_ts")
    return row


def cmd_handoff(cfg: Config, args: argparse.Namespace) -> int:
    """L232a planner/journal; activation is supplied by the L232b controller."""
    from . import canon, handoffs, identity, resources, storage
    db = _open(cfg)
    try:
        if args.handoff_command == "show":
            row = storage.of(db).handoffs.get(args.operation)
            if row is None:
                raise ValueError("unknown handoff")
            print(json.dumps({"operation": row,
                              "events": storage.of(db).handoffs.events(args.operation)}, ensure_ascii=False))
            return 0
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok or binding.name != args.agent:
            work.corrector(cfg, db)
        agent = registry.get(db, args.agent)
        if agent is None:
            raise ValueError("unknown agent")
        canons = canon.load_configured(cfg)
        def limits(host):
            effective = resources.host_limits(canons, host)
            return dict(effective["limits"], max_agents=effective["max_agents"])
        candidate = handoffs.plan(db, agent, limits_for=limits)
        if candidate is None:
            print("no fresh admitted destination with capacity")
            return 0
        result = candidate if args.dry_run else handoffs.request(db, candidate, repository=args.repository)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        db.close()


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    # L61 : agents et lots préchargés en un seul aller-retour (db.prefetch) ;
    # le code ci-dessous lit ensuite ces réponses sans retourner à la base
    db = db_mod.prefetch(db, lambda d: (registry.overview(d), work.delays(d, limit=500)))
    try:
        rows = registry.overview(db)
        # L62 : le projet de chaque agent (équipe, à défaut chantier), en
        # colonne et en clé JSON ajoutée ; le tableau est regroupé par projet
        # (L243 : la colonne dit « ameesh+1 » quand l'agent en a plusieurs)
        from .projects import agent_project, agent_project_label, project_label
        for row in rows:
            row["project"] = agent_project(row)
        if args.json:
            print(json.dumps(_annotate_agents(cfg, db, rows), ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("no known agent")
            return 0
        now = time.time()
        # Un seul balayage des lots pour toute la vue (L36) : le pied global
        # et le compte par agent en dérivent.
        lots = work.delays(db, limit=500)
        open_by_agent: dict[str, int] = {}
        for lot in lots:
            if lot.get("assignee") and lot.get("state") not in work.TERMINAL:
                open_by_agent[lot["assignee"]] = open_by_agent.get(lot["assignee"], 0) + 1
        # regroupé par projet (tri stable : dans un projet, l'ordre de
        # `overview`, vu le plus récemment d'abord) ; les sans-projet en dernier
        rows.sort(key=lambda r: (r["project"] is None, r["project"] or ""))
        print("%-20s %-12s %-9s %-10s %-11s %-22s %-8s %-5s %-13s %-4s %s" % (
            "NAME", "PROJECT", "HARNESS", "HOST", "STATUS", "LEASE", "UNREAD", "ITEMS", "BUDGET",
            "KEY", "SEEN"))
        for row in rows:
            status = row.get("status") or "?"
            if row.get("status_text"):
                status = "%s/%s" % (status, row["status_text"][:22])
            if row.get("operator_paused") and row.get("status") not in ("stopped", "dead", "blocked"):
                status = ("pause-next" if row.get("status") == "running" else "op-paused")
            if row.get("mode") == "externe":
                # L37 (0030) : session humaine, jamais réveillée par ameesh
                status = "ext/" + status
            print("%-20s %-12s %-9s %-10s %-11s %-22s %-8d %-5d %-13s %-4s %s" % (
                row["name"][:20], project_label(agent_project_label(row))[:12],
                (row.get("harness") or "?")[:9], (row.get("host") or "")[:10],
                status[:11], _lease(row), int(row.get("unread") or 0),
                open_by_agent.get(row["name"], 0), _budget(row),
                ("owner" if row.get("has_owner_key")
                 else "agent" if row.get("key_ready") else "—"),
                _fmt_age(now - float(row.get("last_seen_ts") or 0)),
            ))
        # Les lots sur la même vue (0018 point 5) : le compte par phase et ce
        # qui attend le plus, pour que `ameesh list` dise où en est le travail.
        if lots:
            counts: dict[str, int] = {}
            for lot in lots:
                name, _at = work.last_milestone(lot)
                counts[name] = counts.get(name, 0) + 1
            oldest = min((float(lot["frozen_ts"]) for lot in lots
                          if lot.get("frozen_ts") and not lot.get("reviewed_ts")),
                         default=None)
            detail = ", ".join("%s %d" % (name, counts[name])
                               for name, _at_key, _duration in reversed(work.MILESTONES)
                               if name in counts)
            waiting = " — oldest freeze: %s" % _fmt_age(now - oldest) if oldest else ""
            print("items    : %d (%s)%s" % (len(lots), detail, waiting))
        return 0
    finally:
        db.close()


def _print_hold(hold: dict | None) -> None:
    if hold:
        print("  ON HOLD    until %s, by %s: %s (alerts about its agents muted; "
              "ameesh hosts %s --release)" % (
                  time.strftime("%Y-%m-%d %H:%M", time.localtime(float(hold["until_ts"]))),
                  hold["author"], hold["reason"], hold["host"]))


def _host_hold(cfg: Config, args: argparse.Namespace) -> int:
    """#254 : `ameesh hosts HOST --hold --until … --reason …` / `--release`."""
    from . import host_holds

    if not args.host:
        print("ameesh hosts: --hold and --release name a host", file=sys.stderr)
        return 2
    if args.hold and args.release:
        print("ameesh hosts: --hold or --release, not both", file=sys.stderr)
        return 2
    if args.hold:
        if not args.until or not (args.reason or "").strip():
            print("ameesh hosts: --hold needs --until (a hold always ends) and --reason",
                  file=sys.stderr)
            return 2
        try:
            seconds, until_ts = host_holds.parse_until(args.until)
        except host_holds.HoldError as exc:
            print("ameesh hosts: %s" % exc, file=sys.stderr)
            return 2
    db = _open(cfg)
    try:
        # as for `work set-app`: a human, an orchestrator or a design agent
        try:
            author = work.corrector(cfg, db, args.actor)
        except work.WorkError as exc:
            print("ameesh hosts: %s" % str(exc).replace("correction refused",
                                                        "hold refused").replace(
                      "correction: say who corrects", "hold: say who holds"),
                  file=sys.stderr)
            return 1
        hosts = storage.of(db).hosts
        if args.hold:
            if not host_holds.known(cfg, db, args.host):
                print("ameesh hosts: %s is neither a Host of the canon nor a host of the "
                      "registry" % args.host, file=sys.stderr)
                return 1
            # the bounds are the database's (its clock, not the caller's)
            hold = hosts.hold(args.host, seconds=seconds, until_ts=until_ts,
                              reason=args.reason.strip(), author=author)
            if hold is None:
                print("ameesh hosts: %s" % (
                    host_holds.TOO_LONG if seconds is not None else
                    "--until: %s is past, or more than %d h ahead (database time)"
                    % (args.until.strip(), host_holds.MAX_HOLD_S // 3600)), file=sys.stderr)
                return 2
            text = "host %s on hold until %s: %s — alerts about its agents muted, no new " \
                "work placed there" % (
                    args.host, time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(float(hold["until_ts"]))),
                    hold["reason"])
            if args.json:
                print(json.dumps(dict(hold, action="hold"), ensure_ascii=False))
        else:
            if not hosts.release(args.host, author=author):
                print("ameesh hosts: %s is not on hold" % args.host, file=sys.stderr)
                return 1
            text = "host %s: hold lifted — its alerts are back" % args.host
            if args.json:
                print(json.dumps({"host": args.host, "action": "release"}))
        if not args.json:
            print(text)
        # the history is `host_hold_events` (same transaction); the project
        # thread keeps a readable copy, and its loss is said
        if fil.record(cfg, db, sender=author, recipients=(), text=text) is None:
            print("ameesh hosts: done and kept in host_hold_events, but not written "
                  "to the project thread", file=sys.stderr)
            return 1
        return 0
    finally:
        db.close()


def _host_parts(cfg: Config, db, canons, only: str | None, history: int) -> list[dict]:
    """What `ameesh hosts` shows of each host (all, or `only`), by name."""
    from . import canon as canon_mod
    from . import host_holds
    from . import menage as menage_mod
    from . import placement as placement_mod
    from . import resources as res

    holds = host_holds.live(db)
    latest_by_host = {row["host"]: row for row in res.current(db)}
    # L73 : bilan du ménage par hôte (dernier passage, 24 h de journal,
    # worktrees suivis)
    hk = storage.of(db).housekeeping
    measure_by_host = {row["host"]: row for row in hk.last_measure(None)}
    names = set(latest_by_host) | set(measure_by_host) | set(holds)
    for canon in canons:
        names |= {h.title for h in canon.hosts}
    if only:
        names = {only}
    parts = []
    for name in sorted(names):
        # L43 (0031) : chaque canon décrit l'hôte par sa fiche Host ; les
        # limites physiques sont les plus strictes, avec leur provenance
        fiches = canon_mod.host_fiches(canons, name)
        physical = res.host_limits(canons, name)
        latest = latest_by_host.get(name)
        parts.append({
            "name": name, "fiches": fiches, "physical": physical, "latest": latest,
            "host": fiches[0][1] if fiches else None,
            "admissions": {
                canon.id: sum(1 for p in canon.placements if p.agent and name in
                              placement_mod.admitted_hosts(canon, p))
                for canon, _fiche in fiches},
            "verdict": (res.pressure(latest, limits=physical["limits"])
                        if latest is not None else None),
            "history": res.history(db, name, history),
            "menage": _host_menage(hk, name, menage_mod.effective(canons, name),
                                   measure_by_host.get(name), latest),
            # #254: the owner's hold on the host, if any
            "hold": holds.get(name),
        })
    return parts


def _host_json(part: dict) -> dict:
    """One `ameesh-host/1` object (`hosts --json`, `GET /api/v1/hosts`)."""
    from . import resources as res
    host, physical, latest = part["host"], part["physical"], part["latest"]
    limits = physical["limits"]
    return {
        "schema": "ameesh-host/1",
        "host": part["name"],
        "responsible": host.responsible if host is not None else None,
        "limits": limits,
        "max_agents": physical["max_agents"],
        "limits_origin": physical["origin"],
        "fiches": [{"canon": canon.id, "default": canon.is_default,
                    "responsible": fiche.responsible,
                    "max_agents": fiche.policy.max_agents,
                    "admissions": part["admissions"].get(canon.id, 0)}
                   for canon, fiche in part["fiches"]],
        "latest": latest,
        # #253: every filesystem the host writes to, its own reading
        "volumes": (latest or {}).get("volumes") or [],
        # L159d: the offer, and the common measure (study §1.4)
        # whose lendable capacity it caps
        "offer": limits.get("lend"),
        "measure": (res.measure(latest, limits.get("lend"))
                    if latest is not None else None),
        "pressure": part["verdict"],
        # L64: what the last reading could not measure
        "unmeasured": res.unmeasured(latest),
        "history": part["history"],
        "housekeeping": part["menage"],
        "hold": part["hold"],
    }


def host_views(cfg: Config, db, host: str | None = None, history: int = 10) -> list[dict]:
    """The `ameesh-host/1` objects of `ameesh hosts --json` (L84)."""
    from . import canon as canon_mod
    canons = canon_mod.load_configured(cfg)
    return [_host_json(part) for part in _host_parts(cfg, db, canons, host, history)]


def cmd_hosts(cfg: Config, args: argparse.Namespace) -> int:
    """`ameesh hosts [--json] [HOST]` : ressources des hôtes (L31, 0028).

    Le dernier relevé de chaque hôte connu (canon et/ou base), ses seuils
    (politique de la fiche `Host`, valeurs par défaut comprises), l'état de
    pression et l'historique court. En `--json`, un objet `ameesh-host/1` par
    ligne.

    L43 (0031) : seuils et `max_agents` sont les limites PHYSIQUES — les plus
    strictes des fiches Host de tous les canons — avec la provenance de
    chacune (`limits_origin`) et les fiches de chaque canon (`fiches`).
    """
    from . import canon as canon_mod
    from . import resources as res

    if args.hold or args.release:
        return _host_hold(cfg, args)
    if args.until or args.reason:
        print("ameesh hosts: --until and --reason go with --hold", file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        # L42 (0031) : les fiches Host de tous les canons configurés (le canon
        # par défaut d'abord)
        canons = canon_mod.load_configured(cfg)
        if getattr(args, "report", False):
            return _host_report(cfg, db, canons, args)
        parts = _host_parts(cfg, db, canons, args.host, args.history)
        if not parts:
            print("no known host: no published measurement, no Host card in the canon")
            return 0
        now = time.time()
        many = len(canons) > 1
        for part in parts:
            if args.json:
                print(json.dumps(_host_json(part), ensure_ascii=False, sort_keys=True),
                      flush=True)
                continue
            name, fiches, host, physical = (part["name"], part["fiches"], part["host"],
                                            part["physical"])
            admissions, latest, verdict = part["admissions"], part["latest"], part["verdict"]
            history, menage = part["history"], part["menage"]
            limits, origin = physical["limits"], physical["origin"]
            resp = ("responsible %s" % host.responsible) if host is not None and host.responsible \
                else "host without a Host card" if host is None else "responsible unknown"
            if latest is None:
                print("%s — %s; no published measurement" % (name, resp))
                _print_hold(part["hold"])
                _print_host_fiches(fiches, physical, admissions, many=many)
                _print_host_menage(menage)
                continue
            age = _fmt_age(now - float(latest.get("sampled_ts") or now))
            print("%s — %s; measured %s" % (name, resp, age))
            _print_hold(part["hold"])
            _print_host_fiches(fiches, physical, admissions, many=many)

            def tag(key: str) -> str:
                return " [%s]" % origin.get(key, res.DEFAULT_ORIGIN) if fiches else ""

            print("  thresholds memory ≥ %s%s; swap ≤ %s%s; load ≤ %.2f%s; disk ≥ %s%s"
                  "; tmpfs ≤ %s%s"
                  % (_fmt_bytes(limits["min_mem_available"]), tag("min_mem_available"),
                     _fmt_bytes(limits["max_swap_used"]), tag("max_swap_used"),
                     limits["max_load"], tag("max_load"),
                     _fmt_bytes(limits["min_disk_free"]), tag("min_disk_free"),
                     res.fmt_value("max_tmpfs_used", limits.get("max_tmpfs_used")),
                     tag("max_tmpfs_used")))
            if limits.get("max_cpu_pressure") is not None:
                print("  thresholds CPU pressure ≤ %s%s"
                      % (res.fmt_value("max_cpu_pressure", limits["max_cpu_pressure"]),
                         tag("max_cpu_pressure")))
            print("  state      memory %s; swap %s; load %s; disk %s; turns %s"
                  % (_fmt_bytes(latest.get("mem_available_bytes")),
                     _fmt_bytes(latest.get("swap_used_bytes")),
                     ("%.2f" % latest["load1"]) if latest.get("load1") is not None else "—",
                     _fmt_bytes(latest.get("disk_free_bytes")),
                     latest.get("turns_in_progress")
                     if latest.get("turns_in_progress") is not None else "—"))
            manque = res.unmeasured(latest)
            if manque:
                # L64: a measure the host cannot give, said rather than a "—"
                print("  unmeasured %s" % ", ".join(manque))
            if latest.get("on_ac") is not None or latest.get("battery_percent") is not None:
                # L106 : alimentation d'un portable
                print("  power      %s%s; battery thresholds %s %% (no more turns), %s %% "
                      "(stop)" % (
                          "mains" if latest.get("on_ac") else
                          "battery" if latest.get("on_ac") is False else "unknown",
                          " %s %%" % latest["battery_percent"]
                          if latest.get("battery_percent") is not None else "",
                          limits.get("min_battery_percent"),
                          limits.get("stop_battery_percent")))
            if latest.get("btrfs_unallocated_bytes") is not None \
                    or latest.get("btrfs_metadata_headroom_bytes") is not None:
                # #246: btrfs allocation, which `disque` (statvfs) does not show
                print("  btrfs      unallocated %s (threshold %s%s); metadata headroom %s "
                      "(threshold %s%s)" % (
                          _fmt_bytes(latest.get("btrfs_unallocated_bytes")),
                          _fmt_bytes(limits.get("min_btrfs_unallocated")),
                          tag("min_btrfs_unallocated"),
                          _fmt_bytes(latest.get("btrfs_metadata_headroom_bytes")),
                          _fmt_bytes(limits.get("min_btrfs_metadata_headroom")),
                          tag("min_btrfs_metadata_headroom")))
            for volume in latest.get("volumes") or []:
                # #253: each filesystem the guard watches; the lines above are
                # the worst of them
                others = [p for p in volume.get("paths") or [] if p != volume.get("path")]
                if volume.get("unreadable"):  # #306: mount out of reach
                    print("  volume     %s: unreadable (mount point out of reach)%s"
                          % (volume.get("path"),
                             " (also %s)" % ", ".join(others) if others else ""))
                    continue
                print("  volume     %s: free %s%s%s" % (
                    volume.get("path"), _fmt_bytes(volume.get("disk_free_bytes")),
                    "; btrfs unallocated %s, metadata headroom %s" % (
                        _fmt_bytes(volume.get("btrfs_unallocated_bytes")),
                        _fmt_bytes(volume.get("btrfs_metadata_headroom_bytes")))
                    if volume.get("btrfs_unallocated_bytes") is not None
                    or volume.get("btrfs_metadata_headroom_bytes") is not None else "",
                    " (also %s)" % ", ".join(others) if others else ""))

            for line in _host_measure_lines(latest, limits.get("lend")):
                print(line)
            if latest.get("gate_state") is not None or limits.get("lend"):
                # L159f: the host gate its executor obeys; relocation reads it
                print("  gate       %s" % (
                    "%s (%s, seq %s)" % (latest["gate_state"], latest.get("gate_reason") or "?",
                                         latest.get("gate_seq"))
                    if latest.get("gate_state") is not None
                    else "none recorded: closed to relocation (lending host)"))

            if verdict and verdict["breaches"]:
                tag = "CRITICAL" if verdict["critical"] else "PRESSURE"
                detail = "; ".join(
                    "%s %s (threshold %s)" % (b["label"], res.fmt_value(b["key"], b["value"]),
                                          res.fmt_value(b["key"], b["limit"]))
                    for b in verdict["breaches"])
                print("  %s  %s" % (tag, detail))
            else:
                print("  pressure   none")
            _print_host_menage(menage)
            if history:
                trace = " ".join(
                    "%s→%s" % (_fmt_bytes(row.get("mem_available_bytes")),
                               ("%.1f" % row["load1"]) if row.get("load1") is not None else "—")
                    for row in history)
                print("  history    %d reading(s): %s" % (len(history), trace))
        return 0
    finally:
        db.close()


def _host_report(cfg: Config, db, canons, args: argparse.Namespace) -> int:
    """`ameesh hosts HOST --report [--days N]` (L159g): the pilot report of
    a host over its last N days of readings (study L159 §4.4)."""
    from . import pilot
    from . import resources as res

    if not args.host:
        print("ameesh hosts --report: name the host", file=sys.stderr)
        return 2
    days = float(args.days)
    if not 0 < days <= res.HISTORY_RETENTION_S / 86400.0:
        print("ameesh hosts --report: --days between 0 and %g (readings are kept "
              "%g days)" % ((res.HISTORY_RETENTION_S / 86400.0,) * 2), file=sys.stderr)
        return 2
    readings = storage.of(db).hosts.since(args.host, days * 86400.0)
    limits = res.host_limits(canons, args.host)["limits"]
    rep = pilot.report(args.host, readings, limits, days=days)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    else:
        for line in pilot.format_report(rep):
            print(line)
    return 0


def _host_measure_lines(latest: dict, offer: dict | None) -> list[str]:
    """`ameesh hosts` (L159d): PSI, application and guest shares, lendable
    capacity; nothing for a reading without these measures."""
    from . import resources as res

    def pct(value) -> str:
        return "—" if value is None else "%.1f %%" % float(value)

    def cpus(value) -> str:
        return "—" if value is None else "%.2f" % float(value)

    out = []
    if any(latest.get(column) is not None for column, _r, _l in res.PSI_FIELDS):
        out.append("  PSI        cpu %s; memory %s (full %s); io %s  (some, 60 s)" % (
            pct(latest.get("psi_cpu_some")), pct(latest.get("psi_mem_some")),
            pct(latest.get("psi_mem_full")), pct(latest.get("psi_io_some"))))
    if latest.get("app_cpu") is not None or latest.get("guest_mem_bytes") is not None:
        out.append("  shares     CPU application %s, guest %s of %s; guest memory %s" % (
            cpus(latest.get("app_cpu")), cpus(latest.get("guest_cpu")),
            latest.get("cpu_count") or "—", _fmt_bytes(latest.get("guest_mem_bytes"))))
    # without an offer the host lends nothing: show what it could lend
    lent = res.offered(latest, offer if offer is not None else {})
    if lent is not None:
        out.append("  lendable   CPU %s; memory %s; disk %s  (%s)" % (
            cpus(lent["cpu"]), _fmt_bytes(lent["memory_bytes"]),
            _fmt_bytes(lent["disk_bytes"]),
            "capped by the offer" if offer is not None else "no offer: lends nothing"))
    return out


def _host_menage(hk, host: str, policy, measure_row: dict | None,
                 latest: dict | None) -> dict:
    """`ameesh hosts` (L73) : /tmp du système, dossiers gérés, journal 24 h,
    worktrees suivis."""
    from . import resources as res
    data = (measure_row or {}).get("data") or {}
    tmp = None
    if latest is not None and latest.get("tmp_size_bytes"):
        tmp = {"path": latest.get("tmp_path"), "fstype": latest.get("tmp_fstype"),
               "size_bytes": latest.get("tmp_size_bytes"),
               "used_bytes": latest.get("tmp_used_bytes"),
               "fraction": res.tmpfs_fraction(latest)}
    worktrees = hk.worktrees(host, ("active", "kept"))
    return {
        "policy": policy.as_dict(),
        "system_tmp": tmp,
        "tmpfs_alert": bool(tmp and tmp["fraction"] is not None
                            and tmp["fraction"] >= policy.tmpfs_alert),
        "measure": data or None,
        "measured_ts": (measure_row or {}).get("at_ts"),
        "last_24h": hk.summary(host, 86400.0),
        "worktrees": {"active": sum(1 for w in worktrees if w["status"] == "active"),
                      "kept": [{"path": w["path"], "agent": w["agent"], "lot": w["lot"],
                                "detail": w["detail"]}
                               for w in worktrees if w["status"] == "kept"]},
    }


def _print_host_menage(menage: dict) -> None:
    tmp = menage.get("system_tmp")
    policy = menage["policy"]
    if tmp:
        part = tmp.get("fraction")
        print("  /tmp       %s %s / %s%s%s" % (
            tmp.get("fstype") or "?", _fmt_bytes(tmp.get("used_bytes")),
            _fmt_bytes(tmp.get("size_bytes")),
            " (%d %%)" % round(part * 100) if part is not None else " (on disk)",
            " — ALERT: above %d %% (ameesh menage to see what fills it)"
            % round(policy["tmpfs_alert"] * 100) if menage.get("tmpfs_alert") else ""))
    m = menage.get("measure")
    if menage.get("tmpfs_alert") and (m or {}).get("system_tmp_top"):
        # L162 : les plus grosses entrées relevées par le dernier bilan
        from .menage import describe_top
        print("  /tmp large %s" % describe_top(m["system_tmp_top"]))
    if m:
        print("  cleanup    temporary directories %s (quota %s per agent); caches %s / %s"
              "%s" % (_fmt_bytes(m.get("tmp_bytes")), _fmt_bytes(m.get("tmp_quota")),
                      _fmt_bytes(m.get("cache_bytes")), _fmt_bytes(m.get("cache_quota")),
                      "; %d /tmp entries reported (%s)" % (
                          m["orphans"], _fmt_bytes(m.get("orphan_bytes")))
                      if m.get("orphans") else ""))
    conteneurs = (m or {}).get("containers") or {}
    if conteneurs.get("ameesh") is not None or conteneurs.get("others") is not None:
        ours = conteneurs.get("ameesh") or []
        autres = conteneurs.get("others") or []
        total = conteneurs.get("others_total")
        total = len(autres) if total is None else total
        print("  containers %d started by an ameesh turn%s; %d other(s) reported%s" % (
            len(ours),
            " (%s)" % "; ".join("%s: %s" % (c["path"], c.get("detail") or c["action"])
                                 for c in ours[:3]) if ours else "",
            total,
            " (never removed: %s)" % "; ".join(c["command"] for c in autres[:5])
            if autres else ""))
    faits = menage.get("last_24h") or []
    if faits:
        print("  24 h       %s" % "; ".join(
            "%s %s ×%d%s" % (r["kind"], r["action"], r["n"],
                             " (%s)" % _fmt_bytes(r["bytes"]) if r.get("bytes") else "")
            for r in faits))
    wt = menage.get("worktrees") or {}
    if wt.get("active") or wt.get("kept"):
        print("  worktrees  %d tracked, waiting for their work item to end; %d kept%s" % (
            wt.get("active", 0), len(wt.get("kept") or []),
            ": " + "; ".join("%s (%s)" % (k["path"], k["detail"]) for k in wt["kept"][:5])
            if wt.get("kept") else ""))


def cmd_conflicts(cfg: Config, args: argparse.Namespace) -> int:
    """`ameesh conflicts` (L160b) : voir `ameesh.radar.cmd_conflicts`."""
    from . import radar
    return radar.cmd_conflicts(cfg, args)


def cmd_ci(cfg: Config, args: argparse.Namespace) -> int:
    """`ameesh ci status` (L161c) : voir `ameesh.ci.cmd_ci`."""
    from . import ci
    return ci.cmd_ci(cfg, args)


def cmd_release(cfg: Config, args: argparse.Namespace) -> int:
    """`ameesh release open|check|reason` (L750/L756) : voir `ameesh.release`."""
    return release.run(cfg, args)


def cmd_housekeeping(cfg: Config, args: argparse.Namespace) -> int:
    """`ameesh housekeeping` (L73, L148) : voir `ameesh.housekeeping.cmd_housekeeping`."""
    from . import housekeeping
    return housekeeping.cmd_housekeeping(cfg, args)


def cmd_menage(cfg: Config, args: argparse.Namespace) -> int:
    """`ameesh menage` (L73), déprécié (L148) : alias caché de `housekeeping`."""
    sys.stderr.write("menage is deprecated, use housekeeping\n")
    return cmd_housekeeping(cfg, args)


def _print_host_fiches(fiches: list, physical: dict, admissions: dict, *, many: bool) -> None:
    """`ameesh hosts` (L43, 0031) : les fiches Host de l'hôte par canon, et le
    `max_agents` physique (le plus strict) avec sa provenance."""
    if not fiches:
        return
    if many or len(fiches) > 1:
        print("  cards      %s" % "; ".join(
            "%s (%s, max %s, %d admission(s))" % (
                canon.id, fiche.responsible or "no responsible",
                "—" if fiche.policy.max_agents is None else fiche.policy.max_agents,
                admissions.get(canon.id, 0))
            for canon, fiche in fiches))
    total = sum(admissions.values())
    cap = physical["max_agents"]
    if cap is None:
        print("  agents     %d admission(s); max_agents not declared" % total)
        return
    print("  agents     %d admission(s); max_agents %d [%s]%s" % (
        total, cap, physical["origin"].get("max_agents", "?"),
        " — ABOVE the physical maximum: the runner only runs %d persona(s) "
        "at a time" % cap if total > cap else ""))


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        row = agent_detail(db, args.agent)
        if row is None:
            print(USAGE_HINT % ("unknown agent: %s" % args.agent), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2))
            return 0
        print("agent    : %s" % row["name"])
        print("workspace: %s" % (row.get("chantier") or "—"))
        print("harness  : %-9s host: %-12s status: %s%s" % (
            row.get("harness") or "?", row.get("host") or "—", row.get("status"),
            (" (%s)" % row["status_text"]) if row.get("status_text") else ""))
        if row.get("stop_reason"):
            print("stopped  : %s" % row["stop_reason"])
        # L37 (0030) : mode explicite ; un agent externe exige un responsable
        if row.get("mode") == "externe":
            print("mode     : externe (human session, cannot be woken)%s" % (
                "" if row.get("responsible")
                else " — NO human responsible: mandatory for an external agent"))
        else:
            print("mode     : execute (driven by a runner, under lease)")
        # L39 (0033) : compte d'origine de la session, quand il est connu
        print("session  : %s%s" % (row.get("session_id") or "—",
                                   " (account %s)" % row["session_account"]
                                   if row.get("session_id") and row.get("session_account")
                                   else ""))
        print("lease    : %s" % _lease(row))
        print("model    : %-9s budget: %s" % (row.get("model") or "—", _budget(row)))
        print("responsible: %s   team: %s%s" % (
            row.get("responsible") or "none (not claimable if required)",
            row.get("team") or "—",
            "   ephemeral, created by %s, expires %s" % (
                row.get("created_by") or "?", _fmt_moment(row.get("ephemeral_expires_ts")))
            if row.get("ephemeral") else ""))
        if row.get("canon_ref"):
            print("canon    : %s" % row["canon_ref"])
        print("key      : %s" % (
            "%s (role %s%s)" % (row.get("public_key_fingerprint") or "",
                                row.get("key_role") or "agent",
                                ", revoked" if row.get("key_revoked_ts") else "")
            if row.get("key_ready") else "no public key registered"))
        print("turns    : %s   last turn: %s" % (
            row.get("turns"), _fmt_moment(row.get("last_turn_ts"))))
        print("error    : %s" % (row.get("last_error") or "—"))
        print("directory: %s" % (row.get("cwd") or "—"))
        print("seen     : %s" % _fmt_age(time.time() - float(row.get("last_seen_ts") or 0)))
        return 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# clés
# --------------------------------------------------------------------------

def _note_owner_key(db: Db, result: dict) -> None:
    """Une clé owner d'humain vient d'être enregistrée (#326 r1) : son chat
    (`chat-<id>`, s'il existe) en reçoit la note, que le geste vienne du
    propriétaire ou non ; `ameesh alerts` lève aussi `owner_key_registered`."""
    chat = "chat-" + result["agent"]
    if not storage.of(db).agents.get(chat):
        return
    mail.send(db, work.SYSTEM_SENDER, chat,
              "An owner key was %s for %s: fingerprint %s. If this was not your "
              "gesture, revoke it at once (ameesh key revoke %s) and register "
              "yours with the owner's database role (key register --human %s "
              "--replace)." % (result.get("status") or "registered", result["human"],
                               result["fingerprint"], result["agent"], result["agent"]),
              kind="event")


def cmd_key(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        if args.key_command == "generate":
            if not args.i_am_the_owner:
                print(
                    "refused: the owner's key is not generated without --i-am-the-owner.\n"
                    "No agent may generate or hold this key: the ceremony is\n"
                    "an act of the owner (docs/BASCULE.md).",
                    file=sys.stderr)
                return 1
            private_path, public_path, fingerprint = signing.write_keypair(
                os.path.expanduser(args.out), args.name)
            print("private key: %s (0600 — keep it offline, never commit it)" % private_path)
            print("public key : %s" % public_path)
            print("fingerprint: %s" % fingerprint)
            print("register it: ameesh key register --human %s --public-key %s --role owner"
                  % (args.name, public_path))
            return 0
        if args.key_command == "register":
            if bool(args.agent) == bool(args.human):
                print(USAGE_HINT % "key register: give an agent, or --human <id> "
                      "for a human's key (the owner's)", file=sys.stderr)
                return 2
            if args.replace and not args.human:
                print(USAGE_HINT % "key register --replace: only with --human <id>",
                      file=sys.stderr)
                return 2
            public = signing.read_public(os.path.expanduser(args.public_key))
            try:
                if args.human:
                    result = authority.register_human_key(
                        db, args.human, public, role=args.role, replace=args.replace)
                else:
                    result = authority.register_key(
                        db, args.agent, public, role=args.role, note=args.note)
            except authority.AuthorityError as exc:
                print("refused: %s" % exc, file=sys.stderr)
                return 1
            print("key of %s %s: %s (role %s)%s" % (
                result["agent"], "replaced" if result.get("status") == "replaced"
                else "registered", result["fingerprint"], result["role"],
                " — %s, key holder only (no agent)" % result["human"]
                if result.get("human") else ""))
            if result["role"] == "owner":
                print("warning: this key carries the owner's authority for %s; "
                      "its private key must exist only with the owner."
                      % result["agent"])
                if result.get("human"):
                    _note_owner_key(db, result)
            return 0
        if args.key_command == "confirm":
            try:
                event = authority.confirm_key(db, args.agent)
            except authority.AuthorityError as exc:
                print("refused: %s" % exc, file=sys.stderr)
                return 1
            print("key of %s confirmed by the owner (event %d): its owner_key_registered "
                  "alerts end" % (args.agent, event))
            return 0
        if args.key_command == "revoke":
            if not authority.revoke_key(db, args.agent):
                print(USAGE_HINT % ("no active key for %s" % args.agent), file=sys.stderr)
                return 1
            print("key of %s revoked (existing signatures are no longer worth anything)" % args.agent)
            return 0
        if args.key_command == "show":
            info = authority.key_info(db, args.agent)
            if not info or not info.get("public_key"):
                print("no public key registered for %s" % args.agent, file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0
            print("%-11s: %s" % ("human" if info.get("holder") == "human" else "agent",
                                   info["name"]))
            print("fingerprint: %s" % info.get("public_key_fingerprint"))
            print("role       : %s%s" % (
                info.get("key_role") or "agent",
                " (owner's authority)" if info.get("key_role") == "owner"
                else " (provenance only)"))
            print("key        : %s" % info.get("public_key"))
            print("registered : %s" % _fmt_moment(info.get("key_updated_ts")))
            print("revoked    : %s" % (_fmt_moment(info.get("key_revoked_ts"))
                                       if info.get("key_revoked_ts") else "no"))
            return 0
        if args.key_command == "list":
            rows = storage.of(db).keys.registered()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print("no key registered")
                return 0
            for row in rows:
                print("%-20s %-6s %-8s %s  %s" % (
                    row["name"], row.get("holder") or "agent", row.get("key_role") or "agent",
                    row["public_key_fingerprint"],
                    "revoked %s" % _fmt_moment(row["key_revoked_ts"])
                    if row.get("key_revoked_ts") else "registered %s"
                    % _fmt_moment(row.get("key_updated_ts"))))
            return 0
        print(USAGE_HINT % "unknown key subcommand", file=sys.stderr)
        return 2
    finally:
        db.close()


# --------------------------------------------------------------------------
# approbations
# --------------------------------------------------------------------------

def cmd_approve(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        seed = signing.read_private(os.path.expanduser(args.key))
        # Signer une approbation est un acte d'autorité : l'approbateur ne se
        # déduit pas du dossier. Identité liée, ou --as explicite.
        binding = identity.resolve_binding(cfg, db)
        approver = args.as_agent or (binding.name if binding.ok else "")
        if not approver:
            print("approver not bound: set AGENT_MAIL_NAME (the runner does it) "
                  "or pass --as NAME", file=sys.stderr)
            return 2
        ttl = authority.parse_ttl(args.expires)
        meta = json.loads(args.meta) if args.meta else {}
        built = authority.create_approval(
            db, seed, approver=approver, action=args.action, artifact_kind=args.kind,
            artifact_hash=args.hash, decision=args.decision, ttl=ttl, meta=meta)
        print("approval #%d recorded: %s %s %s" % (
            built["id"], built["decision"], built["action"], built["artifact_hash"][:16] + "…"))
        print("key: %s   expires: %s" % (
            built["signature_key"][:16] + "…",
            _fmt_moment(time.time() + ttl)))
        return 0
    finally:
        db.close()


def cmd_approvals(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        rows = authority.list_approvals(
            db, action=args.action, artifact_hash=args.hash, limit=args.limit)
        for row in rows:
            verdict = authority.verify_approval(db, row)
            row["verdict"] = verdict.ok
            row["reason"] = verdict.reason
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("no approvals")
            return 0
        for row in rows:
            print("#%-4d %-9s %-10s %-12s %s  %s" % (
                row["id"], row["decision"], row["action"], row["artifact_kind"],
                row["artifact_hash"][:16] + "…",
                "VALID" if row["verdict"] else "invalid (%s)" % row["reason"]))
            print("      %s  expires %s%s" % (
                row["approver"], _fmt_moment(row.get("expires_ts")),
                "  consumed by %s" % row["consumed_by"] if row.get("consumed_ts") else ""))
        return 0
    finally:
        db.close()


def cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        row, verdict = authority.find_approval(
            db, args.action, args.hash, consume_by=args.by if args.consume else None)
        if args.json:
            print(json.dumps({"ok": verdict.ok, "reason": verdict.reason,
                              "approval": row}, ensure_ascii=False, indent=2))
        elif verdict.ok:
            print("VALID approval #%s: %s %s → %s" % (
                row["id"], row["approver"], row["action"],
                "consumed by %s" % args.by if args.consume else "available"))
            print("artifact %s   expires %s" % (
                row["artifact_hash"], _fmt_moment(row.get("expires_ts"))))
        else:
            print("no valid approval: %s" % verdict.reason, file=sys.stderr)
        return 0 if verdict.ok else 1
    finally:
        db.close()


# --------------------------------------------------------------------------
# lots (work_items)
# --------------------------------------------------------------------------

def _delays_cell(row: dict | None, now: float) -> str:
    """La phase courante d'un lot et l'âge de son dernier jalon (R19)."""
    if not row:
        return "—"
    name, at = work.last_milestone(row)
    if at is None:
        return name
    return "%s %s" % (name, _fmt_span(now - at))


def _print_delays(row: dict) -> None:
    """La frise d'un lot : jalons datés, durées entre eux."""
    now = time.time()
    parts = []
    for name, at_key, _duration in work.MILESTONES:
        at = row.get(at_key)
        parts.append("%s %s" % (name, _fmt_age(now - float(at)) if at else "—"))
    print("milestones: %s" % " · ".join(reversed(parts)))
    spans = " · ".join("%s %s" % (label, _fmt_span(value))
                       for label, value in work.durations(row))
    if row.get("blocked_verdicts"):
        spans += " · blocked verdicts %d/%d" % (row["blocked_verdicts"], row["verdicts"])
    print("delays   : %s" % spans)


def _print_deep_analysis(result: dict | None) -> None:
    """L317c : ce que `work add` a déclenché chez l'auditeur."""
    from . import deep_analysis
    if not result:
        return
    if result.get("error"):
        print("warning: deep analysis not requested (%s)" % result["error"], file=sys.stderr)
    elif not result.get("requested"):
        print("analysis : not requested again (%s)" % (result.get("reason") or "?"))
    elif result.get("auditors"):
        print("analysis : deep analysis requested from %s, first report due within %d min"
              % (", ".join(result["auditors"]), int(deep_analysis.DEADLINE_S // 60)))
    else:
        print("warning: deep analysis requested on the lot, but no auditor is registered "
              "to receive it", file=sys.stderr)


def _deep_analysis_line(found: dict) -> str:
    """L317c : la demande d'analyse profonde d'un lot et le délai de son
    premier rapport (« deep analysis (bug_p1) requested … — report after … »)."""
    from . import deep_analysis
    head = "deep analysis (%s) requested %s" % (found["trigger"] or "?",
                                                _fmt_moment(found["requested_ts"]))
    deadline = int(deep_analysis.DEADLINE_S // 60)
    if found["report_ts"] is not None:
        return "%s — first report after %s by %s%s" % (
            head, _fmt_span(found["delay_s"]), found["reporter"] or "?",
            " (over the %d min deadline)" % deadline if found["overdue"] else "")
    return "%s — no report yet, waiting for %s%s" % (
        head, _fmt_span(found["delay_s"]),
        " (over the %d min deadline)" % deadline if found["overdue"] else "")


def cmd_review_class(cfg: Config, args: argparse.Namespace) -> int:
    """La classe de revue d'un changement (décision 0018 point 1, R19).

    La politique est lue au canon (`review_policies.risk_classes`), par projet
    (`--project`, §8) ; les fichiers viennent des arguments ou de `--diff REF`
    (ref..HEAD + arbre de travail). Une déclaration invalide refuse de classer :
    jamais une classe par défaut rassurante sur une politique illisible.
    """
    from . import canon as canon_mod
    from . import canon_cli, review as review_mod

    canon = canon_cli.load_canon(cfg, args)
    if not canon.readable:
        print("error: unreadable canon (%s)" % canon.root, file=sys.stderr)
        return 1
    scoped = review_mod.scoped_from_federation(canon.federation)
    project = args.project
    if project is None and scoped.projects:
        # Sans projet, la politique d'un dépôt ne se devine pas : chaque projet
        # déclare ses propres chemins sensibles (§8), les mélanger serait pire
        # qu'une erreur.
        print(USAGE_HINT % ("review-class: name the project (--project): declared here: %s"
                            % ", ".join(sorted(scoped.projects))), file=sys.stderr)
        return 1
    policy, problems = scoped.policy_of(project)
    if problems:
        for code, _severity, message in problems:
            print("%s: %s" % (code, message), file=sys.stderr)
        return 1
    if args.diff and args.paths:
        print(USAGE_HINT % "review-class: --diff REF or files, not both",
              file=sys.stderr)
        return 2
    in_repo = canon_mod.git_toplevel(os.getcwd())
    if args.diff and in_repo is None:
        print(USAGE_HINT % "review-class: --diff REF requires a git repository", file=sys.stderr)
        return 1
    # Hors dépôt, les chemins donnés sont lus depuis le dossier courant : c'est
    # ce que l'appelant voit, et la politique n'a pas de racine à leur donner.
    repo = in_repo or os.getcwd()
    if args.diff:
        try:
            paths = canon_mod.git_changed_paths(repo, args.diff)
        except canon_mod.GitError as exc:
            # une référence illisible n'est pas « aucun fichier changé » : rendre
            # une classe partielle ferait croire à un changement anodin (B2).
            print(USAGE_HINT % ("review-class: %s" % exc), file=sys.stderr)
            return 1
        if not paths:
            print("no file changed since %s" % args.diff)
            return 0
    elif args.paths:
        # Les chemins sont donnés depuis le dossier courant : on les ramène à la
        # racine du dépôt, parce que la politique est écrite en chemins de dépôt.
        here = os.path.relpath(os.getcwd(), repo).replace(os.sep, "/")
        paths = [p if here == "." else "%s/%s" % (here, p) for p in args.paths]
    else:
        print(USAGE_HINT % "review-class: files or --diff REF", file=sys.stderr)
        return 2
    try:
        result = review_mod.classify(policy, paths)
    except review_mod.ReviewError as exc:
        print(USAGE_HINT % ("review-class: %s" % exc), file=sys.stderr)
        return 1
    if args.json:
        # `policy_ref` : le commit du canon lu, pour que la classe affichée soit
        # rattachable à la politique qui l'a produite (mesh-design).
        policy_ref = canon.sources[0].commit if canon.sources else ""
        print(json.dumps(dict(result, root=canon.root, repo=repo, project=project,
                              policy_ref=policy_ref),
                         ensure_ascii=False, indent=2))
        return 0
    print("class: %s" % result["class_display"])
    for entry in result["files"]:
        rule = ", ".join(entry["rule"]) if entry["rule"] else "default"
        print("  %-52s %-9s %s" % (entry["path"], review_mod.DISPLAY[entry["class"]], rule))
    return 0


def _print_assignment(check: dict | None) -> None:
    """Ce que l'attribution gardée (L37) a à dire : session externe forcée,
    agent arrêté."""
    if not check:
        return
    if check.get("externe"):
        print("external session: %s will not be woken by ameesh; the work item waits for %s "
              "(responsible)" % (check["assignee"], check["responsible"]))
    if check.get("warning"):
        print("warning: %s" % check["warning"], file=sys.stderr)


#: sous-commandes `work` dont `--actor` a son propre sens (le délégant de
#: `delegate`) ou son propre défaut non vide (relevés, échéances)
_OWN_ACTOR = ("delegate", "expire-delegations", "sync-branches")


def _work_actor(cfg: Config, db, args: argparse.Namespace) -> None:
    """L'acteur des commandes `work` (correctif du 2026-10-11) : `--actor`,
    sinon l'identité liée de la session, jamais vide (« inconnu », averti).
    Une correction d'état (`move --correct`) vérifie en plus qui corrige, un
    forçage (`add`/`assign --force`, #540) qui force."""
    if args.work_command == "move" and getattr(args, "correct", None) is not None:
        args.actor = work.corrector(cfg, db, args.actor)
        return
    if args.work_command in ("add", "assign") and (getattr(args, "force", None) or "").strip():
        # #540 : l'auteur d'un forçage est l'identité de la session
        args.actor = work.forcer(cfg, db, args.actor)
        return
    if args.work_command in (*_OWN_ACTOR, "set-app", "issue-ref", "backfill-app") or getattr(args, "actor", None) is None:
        return
    args.actor, warning = work.resolve_actor(cfg, db, args.actor)
    if warning:
        print("warning: %s" % warning, file=sys.stderr)


def work_rows(db, *, state: str | None = None, assignee: str | None = None,
              limit: int | None = None, threshold: float | None = None,
              freeze: bool = False) -> list:
    """The rows of `ameesh work list --json` (also `GET /api/v1/lots`, L84).

    `freeze` ajoute à chaque ligne le gel effectif dérivé (#1756), en un seul
    balayage de jalons : le composeur du paquet horaire lit ce champ au lieu
    de JOINdre lui-même le dernier jalon `frozen`."""
    from . import plan, plan_git, stagnation
    if threshold is None:
        threshold = stagnation.DEFAULT_THRESHOLD_S

    def lire(db):
        rows = work.list_items(db, state=state, assignee=assignee, limit=limit)
        plan.annotate(db, rows, threshold=threshold)
        # Les délais viennent d'un seul balayage, restreint aux lots
        # affichés (jamais une requête par ligne) ; le JSON les porte
        # aussi, pour la frise (L24). Le filtre porte sur les lignes
        # rendues, pas sur une fenêtre globale : un lot ancien garde sa
        # frise (B3).
        delays = {row["work_item_id"]: row for row in
                  work.delays(db, ids=[row["id"] for row in rows])}
        for row in rows:
            row["delays"] = delays.get(row["id"])
        if freeze:
            marks: dict = {}
            for mark in storage.of(db).progress.lot_milestones(
                    [row["id"] for row in rows]):
                marks.setdefault(int(mark["work_item_id"]), []).append(mark)
            for row in rows:
                row["freeze"] = plan_git.freeze_candidate(marks.get(int(row["id"]), []))
        return rows

    # L61 : lectures regroupées — deux allers-retours en tout
    return db_mod.batched(db, lire)


def work_detail(cfg: Config, db, lot_id: int) -> dict | None:
    """The object of `ameesh work show --json` (also `GET /api/v1/lots/{id}`)."""
    from . import canon as canon_mod, plan

    def lire(db):
        item = work.get(db, lot_id)
        if item is None:
            return None
        item["events"] = work.events(db, lot_id)
        item["milestones"] = work.milestones(db, lot_id)
        from . import plan_git
        item["freeze"] = plan_git.freeze_candidate(item["milestones"])
        from .modules.storage import ModuleStore
        item["module_milestones"] = ModuleStore(db).milestones(lot_id)
        item["delays"] = work.timeline(db, lot_id)
        plan.annotate(db, [item])
        # L96 : dates prévues (feuille de route)
        planned = (storage.of(db).roadmap.item_plans([lot_id]) or [{}])[0]
        item["planned"] = {k: planned.get("planned_" + k)
                           for k in ("start", "end", "delivery", "source", "by")}
        # L157 : durée estimée, réel mesuré, écart ; historique des estimations
        from . import estimates
        delays = item["delays"] or {}
        item["estimate"] = estimates.view(
            item, merged_ts=delays.get("merged_ts") or delays.get("delivered_ts"))
        item["estimates"] = storage.of(db).work.estimates(lot_id)
        # L157b : estimation de référence (lecture de la table publiée)
        item["estimate_reference"] = estimates.proposal(
            db, type=item.get("type"), size=item.get("change_size"),
            assignee=item.get("assignee"))
        # L160d : résolutions préparées où le lot est l'un des deux côtés
        from . import resolutions
        item["resolutions"], error = resolutions.of_lot_or_error(db, lot_id)
        if error:  # e.g. a database without migration 0057
            item["resolutions_error"] = error
        # L160e : place du lot dans la file de fusion de sa cible
        try:
            item["queue"] = next((r for r in storage.of(db).work.queue()
                                  if int(r["work_item_id"]) == lot_id), None)
        except Exception:  # noqa: BLE001 - e.g. a database without migration 0058
            item["queue"] = None
        # L539 : la classe de revue du lot (lecture seule ; ne lève jamais, un
        # doute rend `sensitive` et le dit)
        from . import lot_class
        try:
            item["review_class"] = lot_class.of_lot(db, lot_id, cfg=cfg, item=item)
        except Exception as exc:  # noqa: BLE001 - l'affichage ne casse pas le lot
            item["review_class"] = {"lot": lot_id, "class": lot_class.DOUBT,
                                    "why": "unreadable (%s)" % exc, "files": []}
        # L157c: actual time by phase and merged diff size, once delivered
        from . import lot_measures
        item["measure"] = lot_measures.view(storage.of(db).work.measure(lot_id))
        # L317c: the auditor's automatic deep analysis and its measured delay
        from . import deep_analysis
        item["deep_analysis"] = deep_analysis.status(db, lot_id, cfg=cfg)
        return item

    # L61/L521: the detail's independent reads travel together (one query per
    # section before: 23 round trips, ~7 s from a laptop), and the canon read
    # by the sub-calls is read once per command, not once per dry pass.
    with canon_mod.configured_cache():
        return db_mod.batched(db, lire)


def cmd_work(cfg: Config, args: argparse.Namespace) -> int:
    from . import plan, plan_cli, stagnation  # plan de travail (L29)
    db = _open(cfg)
    try:
        _work_actor(cfg, db, args)
        if args.work_command in plan_cli.COMMANDS:
            return plan_cli.run(db, args)
        if args.work_command == "backlog":   # file d'amélioration (L119, 0037)
            from . import backlog
            return backlog.run(db, args, cfg)
        if args.work_command == "unrecorded":   # demandes d'humains sans lot (L130)
            from . import unrecorded
            return unrecorded.run(cfg, db, args)
        if args.work_command == "resolve":   # prepared conflict resolutions (L160d)
            from . import resolutions
            return resolutions.run(cfg, db, args)
        if args.work_command == "dependencies":
            from . import work_dependencies
            return work_dependencies.run(db, args)
        if args.work_command == "queue":   # merge queue per target (L160e)
            from . import merge_queue
            return merge_queue.run(cfg, db, args)
        if args.work_command == "land":   # merge of the head of the queue (L160e2)
            from . import land
            return land.run(cfg, db, args)
        if args.work_command == "merge-pr":   # REL-11 (#1003) : fusion hors API gh
            from . import merge_pr
            return merge_pr.run(cfg, db, args)
        if args.work_command == "add":
            # L37 (0030, règle 2) : attribution gardée — un assigné non
            # réveillable est refusé AVANT la création (WorkError, code 1).
            # L157 : durée estimée, obligatoire pour un agent (refus AVANT la
            # création), avertissement pour un humain
            from . import estimates
            from . import identity, work_projects
            binding = identity.resolve_binding(cfg, db)
            creator = binding.name if binding.ok else args.actor
            if args.new_project and args.app is None:
                raise work.WorkError("--new-project goes with --app <project>")
            args.app = work_projects.creation_project(cfg, db, creator, args.app,
                                                      new_project=args.new_project)
            # #506: read before the lot exists (it makes its project known)
            introduced = (work_projects.introduction(cfg, db, args.app, creator)
                          if args.new_project and args.app else "")
            minutes = estimates.parse(args.estimate) if args.estimate else None
            warn_estimate = estimates.check(db, args.actor, minutes)
            check = (work.check_assignee(db, args.assignee, externe=args.externe, cfg=cfg)
                     if args.assignee else None)
            item = work.add(
                db, title=args.title, type=args.type, source=args.source, app=args.app,
                body=args.body or "", issue_ref=args.issue_ref, workstream=args.workstream,
                assignee=args.assignee, budget_usd=args.budget, actor=args.actor,
                package=args.package, externe=args.externe, cfg=cfg,
                branch=args.branch, branch_target=args.target, priority=args.priority,
                estimate=minutes, estimate_source=args.estimate_source, size=args.size,
                note=introduced,
                force=args.force,
                parent_lot=args.parent_lot)
            print("lot #%d created in %s: %s%s%s%s" % (
                item["id"], item["state"], item["title"],
                " (plan: %s)" % item["package_id"] if item.get("package_id") else "",
                " (priority %s)" % work.PRIORITY_LABELS.get(item.get("priority"))
                if item.get("priority") else "",
                " — estimated %s" % estimates.label(minutes) if minutes else ""))
            # L157b : l'estimation de référence, LUE dans la table de l'auditeur
            ref = estimates.proposal(db, type=item.get("type"), size=item.get("change_size"),
                                     assignee=item.get("assignee"))
            print("reference: %s" % estimates.proposal_label(ref))
            # L157c: planned dates at the p80, in the assignee's queue ordered
            # at the median (only with a published reference table)
            from . import lot_schedule
            scheduled = lot_schedule.schedule_new(db, item, actor=args.actor)
            if scheduled:
                print("planned  : %s → %s (%s)" % (
                    scheduled["planned_start"], scheduled["planned_end"], scheduled["source"]))
            if warn_estimate:
                print("warning: %s" % estimates.missing(item["id"]), file=sys.stderr)
            # canon §2 (15/10/2026) : une suite de relecture (N, P2, P3) ne
            # crée pas de lot, elle va au backlog — avertissement, pas refus.
            if item.get("route_hint"):
                print("warning: %s" % item["route_hint"], file=sys.stderr)
            _print_deep_analysis(item.get("deep_analysis"))
            _print_assignment(check)
            if item.get("queue_warning"):
                print("warning: %s" % item["queue_warning"], file=sys.stderr)
            return 0
        if args.work_command == "assign":
            out = work.assign(db, args.id, args.agent, externe=args.externe,
                              actor=args.actor, cfg=cfg, branch=args.branch,
                              branch_target=args.target, force=args.force)
            if out.get("queue_warning"):
                print("lot #%d not assigned to %s (before: %s)" % (
                    out["item"]["id"], args.agent, out["previous"] or "nobody"))
                print("warning: %s" % out["queue_warning"], file=sys.stderr)
            elif out["previous"] == out["item"]["assignee"]:
                print("lot #%d: branch %s (assigned to %s)" % (
                    out["item"]["id"], out["item"].get("branch"), out["item"]["assignee"]))
            else:
                print("lot #%d assigned to %s (before: %s)%s" % (
                    out["item"]["id"], out["item"]["assignee"], out["previous"] or "nobody",
                    "; branch %s" % out["item"]["branch"] if args.branch else ""))
            _print_assignment(out["check"])
            return 0
        if args.work_command == "delegate":
            # L40 (0030, point 5) : délégation à échéance, gardée comme `assign`
            out = work.delegate(db, args.id, args.agent, within=args.within,
                                actor=args.actor, cfg=cfg)
            item = out["item"]
            print("lot #%d delegated to %s by %s, due in %s (%s)" % (
                item["id"], item["assignee"], item["delegated_by"],
                work.span(float(item["due_ts"]) - float(item["delegated_ts"])),
                _fmt_moment(item.get("due_ts"))))
            if out.get("replaced"):
                print("previous delegation replaced (%s, delegated by %s)" % (
                    out["replaced"]["delegate"], out["replaced"]["delegated_by"]))
            if out.get("message_id"):
                print("event #%d filed for %s" % (out["message_id"], item["assignee"]))
            if out.get("warning"):
                print("warning: %s" % out["warning"], file=sys.stderr)
            return 0
        if args.work_command == "expire-delegations":
            rows = work.expire_delegations(db, actor=args.actor or work.SYSTEM_SENDER,
                                           dry_run=args.dry_run)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print("no expired delegation")
                return 0
            for row in rows:
                issue = {
                    "rendue": "returned to %s (no turn by %s)" % (row["delegated_by"],
                                                                row["delegate"]),
                    "soldee": "delegation settled (work by %s)" % row["delegate"],
                    "annulee": "delegation cancelled (work item closed or reassigned)",
                }.get(row["outcome"], row["outcome"])
                print("%slot #%s: %s" % ("[dry-run] " if row.get("dry_run") else "",
                                          row["work_item_id"], issue))
            return 0
        if args.work_command == "list":
            rows = work_rows(db, state=args.state, assignee=args.assignee, limit=args.limit,
                             threshold=stagnation.stale_after(args.stale_after),
                             freeze=getattr(args, "freeze", False))
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print("no work items")
                return 0
            now = time.time()
            print("%-5s %-10s %-14s %-12s %-10s %-12s %-12s %s" % (
                "ID", "TYPE", "STATE", "ASSIGNEE", "UPDATED", "DELAY", "EPIC", "TITLE"))
            for row in rows:
                print("%-5d %-10s %-14s %-12s %-10s %-12s %-12s %s" % (
                    row["id"], row["type"], row["state"], (row.get("assignee") or "—")[:12],
                    _fmt_age(now - float(row.get("updated_ts") or 0)),
                    _delays_cell(row.get("delays"), now), (row.get("epic") or "—")[:12],
                    row["title"][:60]))
                notes = []
                if row.get("stale"):
                    notes.append("STALE for %s" % plan.describe_age(row["stale"]["idle_s"]))
                if row.get("planned_wait"):
                    from .work_dependencies import wait_label
                    notes.append(wait_label(row["planned_wait"]))
                if row.get("waiting_for"):
                    notes.append("waiting: %s" % row["waiting_for"]["label"])
                if row.get("delegation"):
                    notes.append(row["delegation"]["label"])
                if notes:
                    print("      %s" % " · ".join(notes))
            return 0
        if args.work_command == "show":
            from . import estimates, lot_measures, resolutions
            item = work_detail(cfg, db, args.id)
            if item is None:
                print(USAGE_HINT % ("lot %s not found" % args.id), file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
                return 0
            print("lot      : #%d %s" % (item["id"], item["title"]))
            print("type     : %-10s state: %-14s assignee: %s" % (
                item["type"], item["state"], item.get("assignee") or "—"))
            print("app      : %-10s source: %-12s workstream: %s" % (
                item.get("app") or "—", item.get("source") or "—", item.get("workstream") or "—"))
            print("issue    : %-12s loops: %-3s budget: %s" % (
                item.get("issue_ref") or "—", item.get("loops"), _budget(item)))
            if item.get("reviewer"):
                print("reviewer : %s (designated for the review, #705)" % item["reviewer"])
            if item.get("priority"):
                print("priority : %s (%s)" % (
                    item["priority"], work.PRIORITY_LABELS.get(item["priority"], "?")))
            if item.get("package_id") or item.get("epic"):
                print("plan     : card %-12s epic: %s" % (
                    item.get("package_id") or "—", item.get("epic") or "—"))
            if any(item["planned"].get(k) for k in ("start", "end", "delivery")):
                print("planned  : start %s · end %s · delivery %s%s" % (
                    item["planned"]["start"] or "—", item["planned"]["end"] or "—",
                    item["planned"]["delivery"] or "—",
                    " (source: %s)" % item["planned"]["source"]
                    if item["planned"].get("source") else ""))
            est = item["estimate"]
            print("estimate : %s%s" % (est["label"], "".join([
                " (source: %s)" % est["source"] if est.get("source") else "",
                " — by %s, on %s" % (est["by"] or "?", _fmt_moment(est["at_ts"]))
                if est.get("minutes") else "",
                "" if est.get("started_ts") is None
                else " — measured start %s" % _fmt_moment(est["started_ts"])])))
            print("reference: %s%s" % (
                estimates.proposal_label(item["estimate_reference"]),
                " — size %s" % estimates.SIZE_LABELS.get(item["change_size"])
                if item.get("change_size") else ""))
            if item["measure"]:
                print("phases   : %s" % lot_measures.describe_phases(item["measure"]))
                print("size     : %s" % lot_measures.describe_size(item["measure"]))
            if len(item["estimates"]) > 1:
                print("estimates: %s" % " → ".join(
                    "%s (%s)" % (estimates.label(e["minutes"]), e.get("estimated_by") or "?")
                    for e in item["estimates"]))
            if item.get("state") == "closed":
                delivered = next((m.get("sha") for m in item.get("milestones", [])
                                  if m.get("kind") == "delivered" and m.get("sha")), "")
                print("closed   : %s%s" % (
                    work.close_label(item),
                    " (head %s)" % delivered[:12] if delivered else ""))
            if item.get("pr_ref"):
                print("PR       : %s" % item["pr_ref"])
            if item.get("branch"):
                # #1899 : `branch_head` est le dernier commit vu par le balayage,
                # pas la tête réelle de la branche (elle peut avoir bougé depuis).
                # Le dire comme un cache, avec son instant, évite les triages
                # faux (qa-01, 16/10 : rebase cru fait, verdict demandé sur une
                # tête périmée) ; le calcul de `waiting` n'est pas touché.
                avance = ""
                if item.get("branch_head"):
                    vu = "tête en avance vue au dernier balayage : %s" % item["branch_head"][:12]
                    if item.get("branch_head_ts"):
                        vu += " le %s" % _fmt_moment(item["branch_head_ts"])
                    avance = " (%s — vérifier à la forge)" % vu
                print("branch   : %s → %s%s" % (
                    item["branch"], item.get("branch_target") or "repository default target",
                    avance))
            if item.get("migration"):
                print("migration: %s reserved in %s (L122)" % (
                    work.migration_layout(item, cfg).label(item["migration"]),
                    item.get("migration_repo")))
            for mark in item["module_milestones"]:
                print("module   : %s/%s · %s · by %s" % (
                    mark["module"], mark["kind"], mark["subject"], mark["actor"]))
            for line in resolutions.show_lines(item["resolutions"], args.id):
                print("resolut. : %s" % line)
            if item.get("resolutions_error"):
                print("resolut. : unavailable (%s)" % item["resolutions_error"])
            if item["deep_analysis"]:
                print("analysis : %s" % _deep_analysis_line(item["deep_analysis"]))
            if item.get("planned_wait"):
                from .work_dependencies import wait_label
                print("planned  : %s" % wait_label(item["planned_wait"]))
            for edge in item.get("dependencies", []):
                print("depends  : #%d (%s, %s)" % (
                    edge["blocker_id"], edge["blocker_state"],
                    "blocking" if edge["blocking"] else "satisfied"))
            if item.get("queue"):
                q = item["queue"]
                print("queue    : %s to %s, rank %s, frozen %s%s" % (
                    q["state"], q["target"], q.get("rank") or "—", q["frozen_sha"][:12],
                    " (%s)" % q["reason"] if q.get("reason") else ""))
            if item.get("waiting_for"):
                print("waiting  : %s" % item["waiting_for"]["label"])
            if item.get("delegation"):
                delegation = item["delegation"]
                print("delegated: %s (on %s%s)" % (
                    delegation["label"], _fmt_moment(delegation.get("delegated_ts")),
                    ", due %s" % _fmt_moment(delegation["due_ts"])
                    if delegation.get("due_ts") else ""))
            if item.get("stale"):
                print("STALE    : no activity for %s"
                      % plan.describe_age(item["stale"]["idle_s"]))
            if item.get("body"):
                print("body     : %s" % item["body"])
            print("created  : %s   updated: %s   closed: %s" % (
                _fmt_moment(item.get("created_ts")), _fmt_moment(item.get("updated_ts")),
                _fmt_moment(item.get("closed_ts"))))
            if item["delays"]:
                _print_delays(item["delays"])
            for event in item["events"]:
                print("  %s  %-14s %-12s %s" % (
                    _fmt_moment(event.get("created_ts")), event["state"],
                    (event.get("actor") or "—")[:12], event["note"]))
            return 0
        if args.work_command == "migration":
            # L122 : numéro de migration réservé au lot, avant d'écrire le fichier
            item = work.reserve_migration(db, args.id, args.number, release=args.release,
                                          repo=args.repo, actor=args.actor, cfg=cfg,
                                          skip_branches=tuple(args.skip_branch or ()))
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
            elif item.get("migration") is None:
                print("lot #%d: no migration reserved" % item["id"])
            else:
                print("lot #%d: migration %s reserved in %s — file %s" % (
                    item["id"], item["migration_label"], item["migration_repo"],
                    item.get("migration_file") or "<migrations>/%s_<name>.sql"
                    % item["migration_label"]))
            return 0
        if args.work_command == "move":
            item = work.move(db, args.id, args.state, note=args.note or "", actor=args.actor,
                             correct=args.correct, reviewer=args.reviewer)
            print("lot #%d → %s (loops %s)%s%s" % (
                item["id"], item["state"], item["loops"],
                " — reviewer %s" % item["reviewer"] if item.get("reviewer") else "",
                " — correction recorded in the log, by %s" % args.actor
                if args.correct is not None else ""))
            return 0
        if args.work_command == "reviewer":
            # #705 : désigner (ou changer) le relecteur d'un lot DÉJÀ en qa,
            # sans aller-retour build/qa ; c'est la commande que l'alerte
            # `stale_lot` recommande à l'orchestrateur du projet.
            item = work.designate_reviewer(cfg, db, args.id, args.agent, actor=args.actor)
            print("lot #%d: reviewer %s (state %s, designated by %s)" % (
                item["id"], item["reviewer"], item["state"],
                args.actor or "the bound identity"))
            return 0
        if args.work_command == "merged":
            done = work.merged(db, args.id, sha=args.sha, note=args.note or "",
                               actor=args.actor)
            if done["result"] == "already":
                print("lot #%d already %s: nothing to do" % (args.id, done["item"]["state"]))
            else:
                print("lot #%d → merged (commit %s, declared by %s)" % (
                    args.id, args.sha.strip().lower()[:12], args.actor))
            return 0
        if args.work_command == "note":
            work.note(db, args.id, args.text, actor=args.actor)
            print("note added to lot #%d" % args.id)
            return 0
        if args.work_command == "set-app":
            from . import work_projects
            item = work_projects.set_app(cfg, db, args.id, args.project, actor=args.actor)
            print("lot #%d: project %s" % (item["id"], item["app"]))
            return 0
        if args.work_command == "issue-ref":
            if bool(args.ref) == bool(args.none):
                raise work.WorkError("issue-ref: pass owner/repo#N, or --none to clear")
            item = work.set_issue_ref(db, args.id, None if args.none else args.ref,
                                      actor=args.actor, cfg=cfg)
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
            else:
                print("lot #%d: issue_ref %s" % (item["id"], item.get("issue_ref") or "—"))
            return 0
        if args.work_command == "backfill-app":
            from . import work_projects
            rows = work_projects.backfill_app(cfg, db, actor=args.actor, dry_run=args.dry_run)
            for row in rows:
                print("lot #%d: %s — %s" % (row["id"], row["status"],
                                           row.get("project") or row.get("reason", "changed concurrently")))
            return 0
        if args.work_command == "milestone":
            row = work.milestone(
                db, args.id, args.kind, sha=args.sha or "", actor=args.actor or "",
                verdict=args.verdict, note=args.note or "")
            print("milestone %s added to lot #%d%s" % (
                row["kind"], args.id,
                " (%s)" % row["verdict"] if row.get("verdict") else ""))
            return 0
        print(USAGE_HINT % "unknown work subcommand", file=sys.stderr)
        return 2
    finally:
        db.close()


# --------------------------------------------------------------------------
# passage v0 → v1 : importer/exporter la boîte fichier
# --------------------------------------------------------------------------

def cmd_import_v0(cfg: Config, args: argparse.Namespace) -> int:
    """Importe les non-lus de la boîte v0 dans Postgres (bascule, étape 4).

    Les fichiers importés sont déplacés dans `inbox/<nom>/imported/`, jamais
    supprimés : le retour arrière consiste à les remettre en place.

    Chaque message importé est écrit dans son fil, sauf s'il y est déjà (déposé
    par le repli fichier de la v1, qui note le fichier v0 dans l'entrée). Le
    corps est repris tel quel : un import ne refuse pas un message déjà envoyé.
    """
    from . import fil, mail, registry
    state = os.path.expanduser(args.state or cfg.v0_state)
    inbox_root = os.path.join(state, "inbox")
    if not os.path.isdir(inbox_root):
        print("no v0 mailbox in %s" % inbox_root, file=sys.stderr)
        return 1
    db = _open(cfg)
    total = 0
    try:
        deja_au_fil = set() if args.dry_run else fil.v0_recorded(cfg)
        names = ([n.strip() for n in args.agents.split(",") if n.strip()]
                 if args.agents else sorted(os.listdir(inbox_root)))
        for name in names:
            directory = os.path.join(inbox_root, name)
            if not os.path.isdir(directory):
                continue
            # le registre v0 (agents/<nom>.json) donne harnais, dossier, session ;
            # un essai (--dry-run) n'écrit rien, registre compris
            agent_file = os.path.join(state, "agents", name + ".json")
            if os.path.exists(agent_file) and not args.dry_run:
                try:
                    with open(agent_file, encoding="utf-8") as fh:
                        seen = json.load(fh)
                    registry.upsert(
                        db, name, harness=seen.get("tool") or None, host=cfg.host,
                        cwd=seen.get("cwd"), session_id=seen.get("session_id"))
                except (OSError, ValueError) as exc:
                    print("  (%s: unreadable v0 registry: %s)" % (name, exc), file=sys.stderr)
            fichiers = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
            importes = 0
            for filename in fichiers:
                path = os.path.join(directory, filename)
                try:
                    with open(path, encoding="utf-8") as fh:
                        message = json.load(fh)
                except (OSError, ValueError) as exc:
                    print("  (%s: %s unreadable: %s)" % (name, filename, exc), file=sys.stderr)
                    continue
                ts = float(message.get("ts") or time.time())
                if args.dry_run:
                    print("%s ← %s: %s" % (name, message.get("from", "?"),
                                            (message.get("text") or "")[:60]))
                    importes += 1
                    continue
                cle_v0 = "%s/%s" % (name, filename)
                mail.send(db, message.get("from") or "inconnu", name,
                          message.get("text") or "", host=message.get("host"),
                          created_us=int(ts * 1_000_000), allow_structured=True,
                          thread=cle_v0 not in deja_au_fil,
                          thread_meta={"importe_v0": cle_v0})
                destination = os.path.join(directory, "imported")
                os.makedirs(destination, exist_ok=True)
                os.replace(path, os.path.join(destination, filename))
                importes += 1
            if importes:
                print("%s: %d message(s) %s" % (
                    name, importes, "to import" if args.dry_run else "imported"))
            total += importes
        print("%d message(s) %s" % (total, "to import" if args.dry_run else "imported"))
        return 0
    finally:
        db.close()


def cmd_export_v0(cfg: Config, args: argparse.Namespace) -> int:
    """Réécrit les non-lus d'un agent dans la boîte v0 (retour arrière).

    Par défaut c'est un déplacement : les messages sont marqués remis en base,
    pour que la v0 reprenne la main sans doublon.
    """
    from . import mail
    state = os.path.expanduser(args.state or cfg.v0_state)
    db = _open(cfg)
    try:
        names = ([n.strip() for n in args.agents.split(",") if n.strip()]
                 if args.agents else storage.of(db).mailbox.pending_recipients_sorted())
        total = 0
        for name in names:
            rows = mail.unread(db, name)
            if not rows:
                continue
            directory = os.path.join(state, "inbox", name)
            os.makedirs(directory, exist_ok=True)
            for row in rows:
                filename = "%d-%s-%d.json" % (
                    int(float(row["created_ts"]) * 1000), row["sender"], os.getpid())
                tmp = os.path.join(directory, "." + filename)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"from": row["sender"], "to": name, "ts": row["created_ts"],
                               "text": row["body"], "host": row.get("host")},
                              fh, ensure_ascii=False)
                os.replace(tmp, os.path.join(directory, filename))
            if not args.keep:
                mail.mark_delivered(db, [row["id"] for row in rows])
            print("%s: %d message(s) exported%s" % (
                name, len(rows), "" if args.keep else " (marked delivered in the database)"))
            total += len(rows)
        print("%d message(s) exported to %s" % (total, state))
        return 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# migrations / diagnostic (délégués à la CLI agent-mail)
# --------------------------------------------------------------------------

def cmd_migrate(cfg: Config, args: argparse.Namespace) -> int:
    from . import cli
    return cli.cmd_migrate(cfg, dry_run=args.dry_run, additive_only=args.additive_only)


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from . import cli
    return cli.cmd_doctor(cfg, notify_test=args.notify_test, probe=args.probe,
                          harness=getattr(args, "harness", False))


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def _etat_chemin(cfg: Config, agent: str, cle: str) -> str:
    return os.path.join(cfg.agent_dir(agent), cle)


def _lit_etat(cfg: Config, agent: str, cle: str) -> str:
    try:
        with open(_etat_chemin(cfg, agent, cle), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _ecrit_etat(cfg: Config, agent: str, cle: str, valeur: str) -> None:
    """Écrit (ou efface) un réglage local ; jamais fatal, `set` le dit."""
    chemin = _etat_chemin(cfg, agent, cle)
    if not valeur:
        try:
            os.unlink(chemin)
        except OSError:
            pass
        return
    os.makedirs(cfg.agent_dir(agent), mode=0o700, exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write(valeur + "\n")


#: clés de `ameesh set` (L13 : model, effort ; L26 : tier, session_policy ;
#: L37 : mode, `execute` | `externe`, décision 0030 ; L60 : context_max_tokens ;
#: L105 : turn_max_seconds, turn_mail_max ; travail de fond : turn_grace_seconds ;
#: L125 : mail_batch, réglage LOCAL de l'hôte de l'agent, sans migration — comme
#: `effort` et `tier` avant L26) ; L223: permission_mode, permission mode of
#: interactive sessions (`ameesh attach`), also local; #408: lot_wake, on|off
#: (rule 1, the runner's wake on an actionable lot), in the database; L76:
#: resident (`on` | `off`) and resident_sleep, the resident process of the
#: agent, local
SET_KEYS = ("model", "effort", "tier", "session_policy", "mode", "context_max_tokens",
            "turn_max_seconds", "turn_mail_max", "turn_grace_seconds", "mail_batch",
            "permission_mode", "lot_wake", "resident", "resident_sleep")
#: L76: settings written in the local state of the agent's host only
LOCAL_SET_KEYS = ("mail_batch", "permission_mode", "resident", "resident_sleep")
#: suffixes acceptés par `context_max_tokens` (`15M`, `500k`)
_TOKEN_SUFFIXES = {"k": 1_000, "m": 1_000_000}


def parse_token_count(valeur: str) -> int:
    """`15000000`, `15M`, `1.5m`, `500k` → entier ≥ 0 ; ValueError sinon (L60)."""
    texte = valeur.strip().lower().replace("_", "")
    facteur = 1
    if texte and texte[-1] in _TOKEN_SUFFIXES:
        facteur = _TOKEN_SUFFIXES[texte[-1]]
        texte = texte[:-1]
    nombre = float(texte)
    if nombre != nombre or nombre < 0 or nombre * facteur > 2 ** 62:
        raise ValueError(valeur)
    return int(round(nombre * facteur))


#: suffixes acceptés par `turn_max_seconds` (`30m`, `2h`, `90s`)
_DURATION_SUFFIXES = {"s": 1, "m": 60, "h": 3600}


def parse_duration(valeur: str) -> int:
    """`1800`, `90s`, `30m`, `1.5h` → secondes entières ≥ 0 ; ValueError sinon (L105)."""
    texte = valeur.strip().lower().replace("_", "")
    facteur = 1
    if texte and texte[-1] in _DURATION_SUFFIXES:
        facteur = _DURATION_SUFFIXES[texte[-1]]
        texte = texte[:-1]
    nombre = float(texte)
    if nombre != nombre or nombre < 0 or nombre * facteur > 2 ** 40:
        raise ValueError(valeur)
    return int(round(nombre * facteur))


def parse_count(valeur: str) -> int:
    """Entier ≥ 0 (`5`) ; ValueError sinon (L105)."""
    nombre = int(valeur.strip())
    if nombre < 0 or nombre > 2 ** 31:
        raise ValueError(valeur)
    return nombre


#: un tier est un identifiant court (passé tel quel au harnais par son descripteur)
_TIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def cmd_set(cfg: Config, args) -> int:
    """`ameesh set <agent> model=… effort=… tier=… session_policy=… mode=…
    context_max_tokens=… turn_max_seconds=… turn_mail_max=… turn_grace_seconds=…`
    (L13, L26, L37, L60, L105, travail de fond).

    Écrit l'état d'exécution : le modèle dans le registre (visible par `list`)
    et dans l'état local ; l'effort et le tier dans l'état local **et** en base
    (L26 : `list --json` les rend quel que soit l'hôte) ; la politique de
    session (`par-lot` | `taille` | `jamais`, décision 0025) en base seulement.
    Le tour suivant les lit et les applique ; une valeur vide revient au
    défaut. Le tier n'a d'effet que sur un harnais dont le descripteur le
    déclare (Codex : `service_tier`). Le mode (`execute` | `externe`, L37) est
    écrit en base ; vide = `execute`. Le plafond de contexte
    (`context_max_tokens`, L60 : `15M`, `500k`, `0` = désactivé) est écrit en
    base ; vide = défaut de l'exécuteur. Les plafonds du tour (L105) aussi :
    `turn_max_seconds` (`30m`, `2h`, `0` = sans limite) et `turn_mail_max`
    (messages remis par le hook pendant un tour, `0` = sans borne). Le délai
    de grâce du travail de fond d'un tour (`turn_grace_seconds`, `20m`, `0` =
    nettoyage immédiat) aussi. L125 :
    `mail_batch` (`90s`, `2m`, `0` = réveil immédiat), fenêtre de regroupement
    du courrier, écrite dans l'état LOCAL — à poser sur l'hôte de l'agent,
    que son exécuteur lit à chaque sondage (sans migration de la base).
    """
    from . import adapters, harnesses, runner
    from .config import SESSION_POLICIES

    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db, defer=True)
        agent = registry.get(db, args.agent)
        if agent is None:
            print("unknown agent: %s" % args.agent, file=sys.stderr)
            return 1
        harness = agent.get("harness") or ""
        incarnation = runner.agent_incarnation(agent)
        valeurs: dict = {}
        for couple in args.values:
            cle, sep, valeur = couple.partition("=")
            valeur = valeur.strip()
            if not sep or cle not in SET_KEYS:
                print("usage: ameesh set <agent> model=… effort=… tier=… "
                      "session_policy=%s mode=%s context_max_tokens=N|15M|0 "
                      "turn_max_seconds=30m|2h|0 turn_mail_max=N|0 "
                      "turn_grace_seconds=20m|0 mail_batch=90s|2m|0 "
                      "permission_mode=auto|acceptEdits|… lot_wake=on|off "
                      "resident=on|off resident_sleep=30m|0"
                      % ("|".join(SESSION_POLICIES), "|".join(registry.MODES)),
                      file=sys.stderr)
                return 2
            if cle == "mode" and valeur and valeur not in registry.MODES:
                print("invalid mode: %r (%s)" % (valeur, " | ".join(registry.MODES)),
                      file=sys.stderr)
                return 2
            if cle == "lot_wake" and valeur and valeur not in ("on", "off"):
                print("invalid lot_wake: %r (on | off)" % valeur, file=sys.stderr)
                return 2
            if cle == "session_policy" and valeur and valeur not in SESSION_POLICIES:
                print("invalid session_policy: %r (%s)"
                      % (valeur, " | ".join(SESSION_POLICIES)), file=sys.stderr)
                return 2
            if cle == "tier" and valeur and not _TIER_RE.match(valeur):
                print("invalid tier: %r (e.g. fast, flex)" % valeur, file=sys.stderr)
                return 2
            if cle == "context_max_tokens" and valeur:
                try:
                    valeur = str(parse_token_count(valeur))
                except ValueError:
                    print("invalid context_max_tokens: %r (e.g. 15M, 500k, 0 = "
                          "disabled)" % valeur, file=sys.stderr)
                    return 2
            if cle == "turn_max_seconds" and valeur:
                try:
                    valeur = str(parse_duration(valeur))
                except ValueError:
                    print("invalid turn_max_seconds: %r (e.g. 30m, 2h, 0 = no "
                          "limit)" % valeur, file=sys.stderr)
                    return 2
            if cle == "turn_grace_seconds" and valeur:
                try:
                    valeur = str(parse_duration(valeur))
                except ValueError:
                    print("invalid turn_grace_seconds: %r (e.g. 20m, 1h, 0 = immediate "
                          "cleanup)" % valeur, file=sys.stderr)
                    return 2
            if cle == "turn_mail_max" and valeur:
                try:
                    valeur = str(parse_count(valeur))
                except ValueError:
                    print("invalid turn_mail_max: %r (e.g. 20, 0 = unbounded)" % valeur,
                          file=sys.stderr)
                    return 2
            if cle == "mail_batch" and valeur:
                try:
                    valeur = str(parse_duration(valeur))
                except ValueError:
                    print("invalid mail_batch: %r (e.g. 90s, 2m, 0 = immediate wake-up)"
                          % valeur, file=sys.stderr)
                    return 2
            if cle == "resident" and valeur and valeur.lower() not in ("on", "off"):
                print("invalid resident: %r (on | off)" % valeur, file=sys.stderr)
                return 2
            if cle == "resident_sleep" and valeur:
                try:
                    valeur = str(parse_duration(valeur))
                except ValueError:
                    print("invalid resident_sleep: %r (e.g. 30m, 1h, 0 = never asleep)"
                          % valeur, file=sys.stderr)
                    return 2
            if cle == "permission_mode" and valeur:
                # codex r3 (#182): checked against the agent harness's allowlist
                descriptor = harnesses.get(harness)
                rule = descriptor.interactive_permission if descriptor else {}
                if not rule:
                    print("permission_mode: harness %r declares no interactive "
                          "permission mode" % harness, file=sys.stderr)
                    return 2
                allowed = rule.get("choices") or ()
                if not harnesses.PERMISSION_MODE_RE.match(valeur) \
                        or (allowed and valeur not in allowed):
                    print("invalid permission_mode: %r (%s)"
                          % (valeur, " | ".join(allowed) or "e.g. auto, acceptEdits"),
                          file=sys.stderr)
                    return 2
                if not incarnation:
                    # L685 r2 (P1): sans l'identité d'enregistrement le mode ne
                    # peut pas être lié à l'incarnation de l'agent : refusé
                    # plutôt que d'écrire une autorisation au nom seul
                    print("permission_mode: cannot bind the mode to the registration "
                          "of %s (creation date unreadable)" % args.agent,
                          file=sys.stderr)
                    return 2
            valeurs[cle] = valeur
        for cle, valeur in valeurs.items():
            if cle == "permission_mode":
                # L685: le mode est enregistré avec le harnais et l'incarnation
                # de l'agent qui l'autorisent ; `attach` refuse de l'appliquer
                # ensuite (autre harnais, ou agent supprimé puis recréé).
                try:
                    runner.write_attach_permission_mode(
                        cfg, args.agent, harness, incarnation, valeur)
                except runner.PermissionModeWriteError as exc:
                    print("permission_mode: %s" % exc, file=sys.stderr)
                    return 2
            elif cle in ("model", "effort", "tier") or cle in LOCAL_SET_KEYS:
                _ecrit_etat(cfg, args.agent, cle,
                            valeur.lower() if cle == "resident" else valeur)
            if cle == "model":
                if valeur:
                    registry.upsert(db, args.agent, model=valeur)
                else:
                    storage.of(db).agents.clear_model(args.agent)
        if "mode" in valeurs:
            registry.set_mode(db, args.agent, valeurs["mode"] or "execute")
        reglages = {k: v for k, v in valeurs.items()
                    if k not in ("model", "mode", "permission_mode")}
        if reglages:
            storage.of(db).operations.set_settings(args.agent, reglages)
        agent = registry.get(db, args.agent) or {}
        modele = agent.get("model") or "default"
        effort = _lit_etat(cfg, args.agent, "effort") or agent.get("effort") or "default"
        tier = _lit_etat(cfg, args.agent, "tier") or agent.get("tier") or "default"
        politique = agent.get("session_policy") or "default (%s)" % cfg.session_policy
        from .exploitation import (effective_context_max, effective_turn_grace_seconds,
                                   effective_turn_mail_max, effective_turn_max_seconds)

        def _txt(valeur: int, cle: str, zero: str) -> str:
            return (zero if valeur == 0 else "%d" % valeur) + (
                "" if agent.get(cle) is not None else " (default)")
        plafond_txt = _txt(effective_context_max(cfg, agent), "context_max_tokens",
                           "disabled")
        duree_txt = _txt(effective_turn_max_seconds(cfg, agent), "turn_max_seconds",
                         "no limit")
        courrier_txt = _txt(effective_turn_mail_max(cfg, agent), "turn_mail_max",
                            "unbounded")
        grace_txt = _txt(effective_turn_grace_seconds(cfg, agent), "turn_grace_seconds",
                         "none")
        # L125 : réglage local de l'hôte de l'agent, sinon défaut de l'hôte
        regroupement = _lit_etat(cfg, args.agent, "mail_batch")
        try:
            secondes = int(float(regroupement)) if regroupement else int(cfg.mail_batch)
        except (ValueError, OverflowError):  # état illisible : l'exécuteur l'ignore
            regroupement, secondes = "", int(cfg.mail_batch)
        regroupement_txt = ("immediate" if secondes == 0 else "%ds" % secondes) + (
            "" if regroupement else " (default)")
        reveil_txt = agent.get("lot_wake") or "default (%s)" % ("on" if cfg.lot_wake else "off")
        print("%s: model=%s effort=%s tier=%s session=%s mode=%s context=%s "
              "turn_max_s=%s mail_per_turn=%s background_grace_s=%s mail_batch=%s "
              "lot_wake=%s (takes effect at the next turn)"
              % (args.agent, modele, effort, tier, politique,
                 agent.get("mode") or "execute", plafond_txt, duree_txt, courrier_txt,
                 grace_txt, regroupement_txt, reveil_txt))
        for local in LOCAL_SET_KEYS:
            if local in valeurs and (agent.get("host") or "") not in ("", cfg.host):
                print("warning: %s is read from the local state of the agent's host "
                      "(%s), not from the database: run this setting on %s"
                      % (local, agent.get("host"), agent.get("host")), file=sys.stderr)
        if valeurs.get("permission_mode"):
            print("interactive permission mode (attach): %s"
                  % valeurs["permission_mode"])
        if agent.get("mode") == "externe" and not agent.get("responsible"):
            # 0030 : un agent externe a obligatoirement un responsable humain
            print("warning: external agent without a human responsible: its work items and its "
                  "alerts have nobody to go to (designate one in the canon)",
                  file=sys.stderr)
        if valeurs.get("tier") and not adapters.supports_tier(agent.get("harness") or ""):
            print("warning: harness %s declares no tier: setting has no effect"
                  % (agent.get("harness") or "?"), file=sys.stderr)
        return 0
    finally:
        db.close()


def _catalog_store(cfg):
    db = db_mod.connect(cfg)
    return db, storage.of(db).catalog


def cmd_models_list(cfg, args) -> int:
    db, store = _catalog_store(cfg)
    try:
        rows = store.listing(harness=getattr(args, "harness", None))
    finally:
        db.close()
    if getattr(args, "json", False):
        print(json.dumps(rows, default=str, ensure_ascii=False))
        return 0
    for row in rows:
        retired = " (retired)" if row.get("retired_at") else ""
        harness = row.get("harness") or "-"
        print(f"{row['provider']}/{row['model_id']}  [{harness}]{retired}")
    return 0


def cmd_models_show(cfg, args) -> int:
    db, store = _catalog_store(cfg)
    try:
        rows = store.show(args.model)
    finally:
        db.close()
    if getattr(args, "json", False):
        print(json.dumps(rows, default=str, ensure_ascii=False))
        return 0
    for row in rows:
        print(f"{row['provider']}/{row['model_id']}  ctx={row.get('context_window')}  "
              f"price={row.get('price_input')}/{row.get('price_cached')}/{row.get('price_output')}  "
              f"source={row.get('source')}  harness={row.get('harness') or '-'}  "
              f"efforts={list(row.get('efforts') or [])}")
    return 0


#: La correspondance entre le vocabulaire de `--source` et les modules de découverte.
DISCOVERY_SOURCES = {
    "anthropic": "ameesh.discovery.anthropic",
    "openai": "ameesh.discovery.openai",
    "deepseek": "ameesh.discovery.deepseek",
    "harness": "ameesh.discovery.harness",
    "prices": "ameesh.discovery.prices",
}


def _source_module(name: str):
    import importlib

    return importlib.import_module(DISCOVERY_SOURCES[name])


def cmd_models_discover(cfg, args) -> int:
    """Découvre, enregistre, et prévient — sans jamais dépenser un token."""
    names = [args.source] if getattr(args, "source", None) else list(DISCOVERY_SOURCES)
    db, store = _catalog_store(cfg)
    failures = 0
    try:
        for name in names:
            result = _source_module(name).listing()
            if getattr(args, "dry_run", False):
                print(f"{name}: {len(result.models)} model(s), complete={result.complete} "
                      f"{result.detail}".rstrip())
                continue
            sent: list[dict] = []
            notify = getattr(args, "notify", None)

            def emit(body: str, payload: dict, _sent=sent, _name=name) -> None:
                # Le fil de l'équipe est DURABLE dans les deux cas (revue B4) :
                # `mail.send` dépose un message et l'écrit ; sans destinataire, on
                # écrit le fil directement, sans inventer de destinataire — mesh-design
                # a demandé que --notify reste explicite, codex3 que le fil survive.
                if notify:
                    mail.send(db, args.sender, notify, body, kind="event", payload=payload,
                              allow_structured=True)
                else:
                    # La cfg de la COMMANDE, pas `db.cfg` : un appelant de test peut
                    # fournir une base sans configuration, et le fil doit quand même
                    # s'écrire (sonde B4).
                    fil.record(
                        cfg, db, sender=args.sender, recipients=[], text=body,
                        project=getattr(args, "project", None),
                        meta={"kind": "event", "source": _name, "payload": payload},
                    )
                    print(body)
                _sent.append({"body": body, "payload": payload})

            decision = catalog.record(store, result, emit=emit)
            if not result.complete:
                failures += 1
            print(f"{name}: {len(result.models)} seen, {len(decision.to_retire)} retired"
                  + (f", {decision.refused_reason}" if decision.refused_reason else ""))
    finally:
        db.close()
    return 1 if failures and not getattr(args, "dry_run", False) else 0


#: L148 : commandes encore acceptées pour la transition mais absentes de
#: l'aide (`menage` → `housekeeping`), retirées au stage 7. argparse affiche
#: les choix en clair dans la ligne d'usage : `build_parser` lui donne la
#: liste visible par `metavar`.
HIDDEN_COMMANDS = frozenset({"menage"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-mesh", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    # L148 : les sous-parseurs héritent de `HiddenAliasParser`, pour que les
    # alias cachés (ancienne graphie d'une option) ne captent jamais une
    # abréviation visible ni n'apparaissent dans un message d'ambiguïté.
    sub = parser.add_subparsers(dest="command", parser_class=HiddenAliasParser)

    p_list = sub.add_parser("list", help="mesh list: all agents")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="details of an agent")
    p_show.add_argument("agent")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_hosts = sub.add_parser("hosts", help="host resources (L31)")
    p_hosts.add_argument("host", nargs="?", default=None, help="a single host (otherwise all)")
    p_hosts.add_argument("--history", type=int, default=10,
                         help="number of readings in the short history (default 10)")
    p_hosts.add_argument("--json", action="store_true", help="one JSON object per host")
    p_hosts.add_argument("--report", action="store_true",
                         help="pilot report of HOST: pressure, load, tmpfs, PSI, hosted "
                              "turns, lendable capacity, per day (L159g)")
    p_hosts.add_argument("--days", type=float, default=7.0,
                         help="window of --report, in days (default 7)")
    p_hosts.add_argument("--hold", action="store_true",
                         help="hold HOST on purpose (#254): alerts and nudges about its "
                              "agents stay quiet until --until (required, 24 h at most)")
    p_hosts.add_argument("--release", action="store_true", help="lift the hold of HOST")
    p_hosts.add_argument("--until", default=None,
                         help="end of the hold: a span (90m, 2h) or an ISO date")
    p_hosts.add_argument("--reason", default=None, help="why the host is held (required)")
    p_hosts.add_argument("--actor", default=None,
                         help="who holds or releases, without a session identity "
                              "(human:<id>); a human, an orchestrator or a design agent")
    p_hosts.set_defaults(func=cmd_hosts)

    p_housekeeping = sub.add_parser(
        "housekeeping", help="cleanup of what agents create (L73); dry run by default")
    mode = p_housekeeping.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="show only (default): nothing is removed")
    mode.add_argument("--apply", action="store_true",
                      help="apply (human or runner only, never an agent "
                           "session)")
    p_housekeeping.add_argument("--by", default="", help="who triggers it (log), e.g. human:alice")
    p_housekeeping.add_argument("--json", action="store_true", help="`ameesh-menage/1` report")
    p_housekeeping.set_defaults(func=cmd_housekeeping)

    # L148 : `ameesh menage` reste accepté (une ligne sur stderr) ; sans
    # `help=`, il ne figure pas dans la liste des commandes.
    p_menage = sub.add_parser("menage")
    mode = p_menage.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="show only (default): nothing is removed")
    mode.add_argument("--apply", action="store_true",
                      help="apply (human or runner only, never an agent "
                           "session)")
    p_menage.add_argument("--by", default="", help="who triggers it (log), e.g. human:alice")
    p_menage.add_argument("--json", action="store_true", help="`ameesh-menage/1` report")
    p_menage.set_defaults(func=cmd_menage)

    p_conflicts = sub.add_parser(
        "conflicts", help="conflict radar between work items, last pass on this host (L160b)")
    p_conflicts.add_argument("--project", default="", help="a single project")
    p_conflicts.add_argument("--all", action="store_true",
                             help="also pairs that overlap without conflict")
    p_conflicts.add_argument("--json", action="store_true", help="`ameesh-radar/1` report")
    p_conflicts.set_defaults(func=cmd_conflicts)

    p_ci = sub.add_parser("ci", help="CI read live by ameesh notify (L161c)")
    ci_sub = p_ci.add_subparsers(dest="ci_command", required=True)
    p_ci_status = ci_sub.add_parser(
        "status", help="latest CI runs of a work item and their failing tests")
    p_ci_status.add_argument("id", type=int, help="work item number")
    p_ci_status.add_argument("--limit", type=int, default=5,
                             help="maximum runs shown (default 5)")
    p_ci_status.add_argument("--json", action="store_true", help="`ameesh-ci-status/1` report")
    p_ci_status.set_defaults(func=cmd_ci)

    # L750/L756 : le périmètre d'une release et sa porte (`ameesh release`)
    release.add_parser(sub, cmd_release)

    p_models = sub.add_parser("models", help="model catalog (L14)")
    models_sub = p_models.add_subparsers(dest="models_command")

    p_models_list = models_sub.add_parser("list", help="list the catalog")
    p_models_list.add_argument("--harness")
    p_models_list.add_argument("--json", action="store_true")
    p_models_list.set_defaults(func=cmd_models_list)

    p_models_show = models_sub.add_parser("show", help="details of a model")
    p_models_show.add_argument("model")
    p_models_show.add_argument("--json", action="store_true")
    p_models_show.set_defaults(func=cmd_models_show)

    p_models_discover = models_sub.add_parser("discover", help="discover without spending a token")
    p_models_discover.add_argument("--source", choices=sorted(DISCOVERY_SOURCES))
    p_models_discover.add_argument("--dry-run", action="store_true")
    p_models_discover.add_argument("--notify", help="agent to wake with an event")
    p_models_discover.add_argument("--sender", default=os.environ.get("AMEESH_AGENT") or "catalogue")
    p_models_discover.set_defaults(func=cmd_models_discover)

    p_handoff = sub.add_parser("handoff", help="plan a reserved lot-boundary host move or inspect its journal")
    handoff_sub = p_handoff.add_subparsers(dest="handoff_command")
    p_handoff_plan = handoff_sub.add_parser("plan")
    p_handoff_plan.add_argument("agent")
    p_handoff_plan.add_argument("--repository", required=True,
                                help="enrolled Git remote identity, never inferred from a directory name")
    p_handoff_plan.add_argument("--dry-run", action="store_true")
    p_handoff_plan.set_defaults(func=cmd_handoff)
    p_handoff_show = handoff_sub.add_parser("show")
    p_handoff_show.add_argument("operation")
    p_handoff_show.set_defaults(func=cmd_handoff)

    p_key = sub.add_parser("key", help="owner public keys")
    key_sub = p_key.add_subparsers(dest="key_command")
    p_gen = key_sub.add_parser("generate")
    p_gen.add_argument("--out", required=True)
    p_gen.add_argument("--name", default="owner")
    p_gen.add_argument("--i-am-the-owner", action="store_true",
                       help="confirms that the owner is the one acting")
    p_gen.set_defaults(func=cmd_key)
    p_reg = key_sub.add_parser("register")
    p_reg.add_argument("agent", nargs="?")
    p_reg.add_argument("--human", metavar="ID",
                       help="a human's key (human:ID or ID), held outside the agents (#330)")
    p_reg.add_argument("--public-key", required=True)
    p_reg.add_argument("--role", choices=list(authority.KEY_ROLES), default=authority.DEFAULT_KEY_ROLE,
                       help="owner = owner's authority; agent = provenance only")
    p_reg.add_argument("--note", default=None)
    p_reg.add_argument("--replace", action="store_true",
                       help="with --human: replace or rotate the key, even revoked — the "
                            "owner's own gesture, refused by the database outside the "
                            "owner's database role (#326)")
    p_reg.set_defaults(func=cmd_key)
    p_kshow = key_sub.add_parser("show")
    p_kshow.add_argument("agent")
    p_kshow.add_argument("--json", action="store_true")
    p_kshow.set_defaults(func=cmd_key)
    p_klist = key_sub.add_parser("list")
    p_klist.add_argument("--json", action="store_true")
    p_klist.set_defaults(func=cmd_key)
    p_krev = key_sub.add_parser("revoke")
    p_krev.add_argument("agent")
    p_krev.set_defaults(func=cmd_key)
    p_kconf = key_sub.add_parser(
        "confirm", help="the owner confirms the registrations of a key: its "
        "owner_key_registered alert ends (owner's database role, #326)")
    p_kconf.add_argument("agent")
    p_kconf.set_defaults(func=cmd_key)

    p_app = sub.add_parser("approve", help="sign an approval")
    p_app.add_argument("--key", required=True, help="owner's private key")
    p_app.add_argument("--action", required=True)
    p_app.add_argument("--hash", required=True, help="hex sha256 of the artifact")
    p_app.add_argument("--kind", default="text")
    p_app.add_argument("--decision", default="approved", choices=["approved", "rejected"])
    p_app.add_argument("--expires", default=None, help="48h, 7d… (default 24h)")
    p_app.add_argument("--as", dest="as_agent", default=None)
    p_app.add_argument("--meta", default=None, help="free-form JSON")
    p_app.set_defaults(func=cmd_approve)

    p_apps = sub.add_parser("approvals", help="list approvals")
    p_apps.add_argument("--action", default=None)
    p_apps.add_argument("--hash", default=None)
    p_apps.add_argument("--limit", type=int, default=50)
    p_apps.add_argument("--json", action="store_true")
    p_apps.set_defaults(func=cmd_approvals)

    p_ver = sub.add_parser("verify", help="check that a valid approval exists")
    p_ver.add_argument("--action", required=True)
    p_ver.add_argument("--hash", required=True)
    p_ver.add_argument("--consume", action="store_true", help="consume it (single use)")
    p_ver.add_argument("--by", default=None)
    p_ver.add_argument("--json", action="store_true")
    p_ver.set_defaults(func=cmd_verify)

    p_work = sub.add_parser("work", help="work items (work_items)")
    work_sub = p_work.add_subparsers(dest="work_command")
    pw_add = work_sub.add_parser("add")
    pw_add.add_argument("--title", required=True)
    pw_add.add_argument("--force", metavar="REASON",
                        help="#540: give the lot to an assignee who already has a lot not "
                             "started (the reason goes to the lot's journal)")
    pw_add.add_argument("--type", default="evolution",
                        help="an improvement backlog item goes through "
                             "`ameesh work backlog add` (L119)")
    pw_add.add_argument("--source", default="")
    pw_add.add_argument("--app", default=None,
                        help="project; default: the creating agent's registry or own canon")
    pw_add.add_argument("--new-project", action="store_true",
                        help="#506: accept an --app project nobody declares yet "
                             "(journaled on the lot); refused otherwise")
    pw_add.add_argument("--body", default=None)
    pw_add.add_argument("--issue-ref", default=None)
    pw_add.add_argument("--workstream", default=None)
    pw_add.add_argument("--assignee", default=None)
    pw_add.add_argument("--budget", type=float, default=None)
    pw_add.add_argument("--actor", default="")
    pw_add.add_argument("--package", default=None, help="WorkPackage card of the work item (L29)")
    pw_add.add_argument("--parent-lot", type=int, default=None, metavar="LOT",
                        help="#1193: parent of a suite lot: its blocking prerequisite, linked "
                             "with the creation (unknown parent refused)")
    pw_add.add_argument("--externe", action="store_true",
                        help="force assignment to an external agent that has a human "
                             "responsible (L37, 0030)")
    pw_add.add_argument("--branch", default=None,
                        help="work item branch: its merge into the target closes the work item (L118)")
    pw_add.add_argument("--target", default=None,
                        help="target branch (default: the repository's git config ameesh.target, "
                             "otherwise origin/HEAD)")
    pw_add.add_argument("--priority", type=int, choices=list(work.PRIORITIES), default=None,
                        help="1 high, 2 normal, 3 low (L130: every request from a "
                             "human becomes a work item, with its priority)")
    pw_add.add_argument("--estimate", default=None, metavar="DURATION",
                        help="estimated duration of the work item: 90m, 2h, 1h30, 1,5h, 2d (or "
                             "minutes) — mandatory for an agent (L157)")
    pw_add.add_argument("--estimate-source", default=None, metavar="SOURCE",
                        help="where the estimate comes from (design, bug history ×1.3…)")
    pw_add.add_argument("--size", default=None, choices=list(work.SIZES),
                        help="size of the change: s small, m medium, l large — a key of "
                             "the reference estimate (L157b)")
    pw_add.set_defaults(func=cmd_work)
    pw_list = work_sub.add_parser("list")
    pw_list.add_argument("--state", default=None)
    pw_list.add_argument("--assignee", default=None)
    pw_list.add_argument("--limit", type=int, default=50)
    pw_list.add_argument("--json", action="store_true")
    pw_list.add_argument("--freeze", action="store_true",
                         help="#1756: add the effective freeze per row — the declared "
                              "freeze, or the last favourable verdict's commit when it "
                              "covers the head (origin, provenance, explicit causes)")
    pw_list.add_argument("--stale-after", default=None,
                         help="work item is stale with no activity for (default 6h, L29)")
    pw_list.set_defaults(func=cmd_work)
    pw_show = work_sub.add_parser("show")
    pw_show.add_argument("id", type=int)
    pw_show.add_argument("--json", action="store_true")
    pw_show.set_defaults(func=cmd_work)
    from . import work_dependencies
    work_dependencies.add_parser(work_sub, cmd_work)
    pw_mig = work_sub.add_parser(
        "migration", help="reserve the next free migration number for the work item (L122), "
                          "before writing the file; idempotent, visible in work show")
    pw_mig.add_argument("id", type=int)
    pw_mig.add_argument("--number", type=int, default=None,
                        help="reserve this exact number (refused if it already exists in the "
                             "lot's repository or is reserved by another work item)")
    pw_mig.add_argument("--release", action="store_true",
                        help="release the reservation (the work item no longer adds a migration)")
    pw_mig.add_argument("--repo", default=None,
                        help="local clone of the lot's repository (that of its issue), whose "
                             "worktree and branches count (default: the clone declared in "
                             "radar.projects, else the current git repository; its origin "
                             "must be the lot's repository)")
    pw_mig.add_argument("--skip-branch", action="append", default=None, metavar="BRANCH",
                        help="also ignore this branch in the scan, in addition to the lot's own "
                             "branch (repeatable): the integration branch of a package being "
                             "composed, whose renumbered files must not count against the "
                             "reservation (#1603)")
    pw_mig.add_argument("--json", action="store_true")
    pw_mig.add_argument("--actor", default="",
                        help="default: the session's bound identity (AGENT_MAIL_NAME)")
    pw_mig.set_defaults(func=cmd_work)
    pw_move = work_sub.add_parser("move")
    pw_move.add_argument("id", type=int)
    pw_move.add_argument("state", choices=list(work.STATES))
    pw_move.add_argument("--note", default=None)
    pw_move.add_argument("--actor", default="",
                         help="default: the session's bound identity (AGENT_MAIL_NAME)")
    pw_move.add_argument("--correct", default=None, metavar="REASON",
                         help="correct a state set by mistake (promoted → merged, merged → build), "
                              "outside the state machine, recorded in the log; reserved for humans "
                              "and for orchestrators or design agents")
    pw_move.add_argument("--reviewer", default=None, metavar="AGENT",
                         help="#705: designate the reviewer of the review that starts, only "
                              "when moving to qa: waiting(), the progress view and the stale_lot "
                              "alert name them before any verdict")
    pw_move.set_defaults(func=cmd_work)
    pw_reviewer = work_sub.add_parser(
        "reviewer",
        help="#705: designate (or change) the reviewer of a lot already in qa, "
             "without a build/qa round trip")
    pw_reviewer.add_argument("id", type=int)
    pw_reviewer.add_argument("agent", metavar="AGENT",
                             help="a registry agent, or human:<id>; never the lot's assignee")
    pw_reviewer.add_argument("--actor", default="",
                             help="who designates: the session's bound identity by default; "
                                  "a human or an orchestrator (same authority as --correct)")
    pw_reviewer.set_defaults(func=cmd_work)
    pw_merged = work_sub.add_parser(
        "merged", help="declare a work item merged outside a PR and without freeze (local merge "
                       "on the target): any open state → merged, merged milestone with the commit")
    pw_merged.add_argument("id", type=int)
    pw_merged.add_argument("--sha", required=True, help="the merge commit")
    pw_merged.add_argument("--note", default="")
    pw_merged.add_argument("--actor", default="",
                           help="default: the session's bound identity (AGENT_MAIL_NAME)")
    pw_merged.set_defaults(func=cmd_work)
    pw_assign = work_sub.add_parser(
        "assign", help="reassign a work item to a wakeable agent (L37, 0030)")
    pw_assign.add_argument("id", type=int)
    pw_assign.add_argument("--force", metavar="REASON",
                           help="#540: assign even though the agent already has a lot not "
                                "started (the reason goes to the lot's journal)")
    pw_assign.add_argument("agent")
    pw_assign.add_argument("--externe", action="store_true",
                           help="force assignment to an external agent that has a "
                                "human responsible")
    pw_assign.add_argument("--branch", default=None,
                           help="work item branch (L118); allowed on a work item already assigned "
                                "to this agent")
    pw_assign.add_argument("--target", default=None, help="target branch (see work add)")
    pw_assign.add_argument("--actor", default="")
    pw_assign.set_defaults(func=cmd_work)
    pw_delegate = work_sub.add_parser(
        "delegate", help="delegate a work item to a wakeable agent, with a deadline (L40, 0030): "
                         "without a turn by the delegate by the deadline, it returns to the delegator")
    pw_delegate.add_argument("id", type=int)
    pw_delegate.add_argument("agent")
    pw_delegate.add_argument("--within", required=True,
                             help="deadline: 30m, 2h, 1d (or seconds)")
    pw_delegate.add_argument("--actor", default="",
                             help="the delegator, to whom the work item will return (default: "
                                  "the current assignee; human:… allowed)")
    pw_delegate.set_defaults(func=cmd_work)
    pw_expire = work_sub.add_parser(
        "expire-delegations",
        help="process expired delegations (also done by the runner on each pass)")
    pw_expire.add_argument("--dry-run", action="store_true",
                           help="say what would be done, without writing anything")
    pw_expire.add_argument("--json", action="store_true")
    pw_expire.add_argument("--actor", default="")
    pw_expire.set_defaults(func=cmd_work)

    pw_note = work_sub.add_parser("note")
    pw_note.add_argument("id", type=int)
    pw_note.add_argument("text")
    pw_note.add_argument("--actor", default="")
    pw_note.set_defaults(func=cmd_work)

    pw_app = work_sub.add_parser("set-app", help="correct a lot's project, with a journal entry")
    pw_app.add_argument("id", type=int)
    pw_app.add_argument("project")
    pw_app.add_argument("--actor", default="",
                        help="human, orchestrator or design agent; default: bound session")
    pw_app.set_defaults(func=cmd_work)
    pw_iref = work_sub.add_parser(
        "issue-ref", help="correct a lot's issue reference (owner/repo#N), with a journal entry")
    pw_iref.add_argument("id", type=int)
    pw_iref.add_argument("ref", nargs="?", default=None,
                         help="the new reference, owner/repo#N (default: keep, use --none to clear)")
    pw_iref.add_argument("--none", action="store_true",
                         help="clear the issue reference (the lot stays where it is)")
    pw_iref.add_argument("--json", action="store_true")
    pw_iref.add_argument("--actor", default="",
                         help="human, orchestrator or design agent; default: bound session")
    pw_iref.set_defaults(func=cmd_work)
    pw_backfill = work_sub.add_parser("backfill-app", help="fill uniquely inferred projects of open lots")
    pw_backfill.add_argument("--dry-run", action="store_true")
    pw_backfill.add_argument("--actor", default="")
    pw_backfill.set_defaults(func=cmd_work)
    pw_ms = work_sub.add_parser(
        "milestone", help="declare a timeline milestone (freeze, verdict) — R19")
    pw_ms.add_argument("id", type=int)
    pw_ms.add_argument("kind", choices=list(work.MANUAL_KINDS),
                       help="frozen (branch frozen) or verdict (review delivered)")
    pw_ms.add_argument("--sha", default="", help="the frozen or reviewed commit")
    # le verdict est un positionnel (`work milestone 1 verdict ok`), comme la
    # frise se lit ; `frozen` n'en prend pas
    pw_ms.add_argument("verdict", nargs="?", choices=list(work.VERDICTS), default=None,
                       help="for the verdict milestone: ok or blocked")
    pw_ms.add_argument("--note", default="")
    pw_ms.add_argument("--actor", default="")
    pw_ms.set_defaults(func=cmd_work)
    from . import plan_cli
    plan_cli.add_parsers(work_sub, cmd_work)  # plan de travail (L29)
    from . import backlog
    backlog.add_parsers(work_sub, cmd_work)  # file d'amélioration (L119, 0037)
    from . import unrecorded
    unrecorded.add_parser(work_sub, cmd_work)  # demandes d'humains sans lot (L130)
    from . import resolutions
    resolutions.add_parser(work_sub, cmd_work)  # prepared conflict resolutions (L160d)
    from . import merge_queue
    merge_queue.add_parser(work_sub, cmd_work)  # merge queue per target (L160e)
    from . import land
    land.add_parser(work_sub, cmd_work)  # merge of the head of the queue (L160e2)
    from . import merge_pr
    merge_pr.add_parser(work_sub, cmd_work)  # fusion hors API gh (REL-11, #1003)

    p_cost = sub.add_parser("cost", help="turn costs and plan gauges (L12)")
    cost_sub = p_cost.add_subparsers(dest="cost_command")
    pc_rep = cost_sub.add_parser("report", help="table: agent, harness, model, spend, gauges")
    # Pas d'option de fenêtre : le rapport rend 1 h et 24 h, et `cost spent <agent> [s]`
    # couvre déjà les fenêtres libres. Une option qui ne change rien se retire.
    pc_rep.add_argument("--json", action="store_true")
    pc_rep.set_defaults(func=cmd_cost)
    pc_spent = cost_sub.add_parser("spent", help="spend (USD) over a sliding window")
    pc_spent.add_argument("agent", nargs="?", default="all")
    pc_spent.add_argument("seconds", nargs="?", type=float, default=3600.0)
    pc_spent.set_defaults(func=cmd_cost)
    pc_over = cost_sub.add_parser("over", help="exit code 0 if the plan pace is exceeded")
    pc_over.add_argument("agent", nargs="?", default="all")
    pc_over.set_defaults(func=cmd_cost)
    # L26 : usage par tour, historique des jauges, solde du fournisseur payé au token
    pc_turns = cost_sub.add_parser("turns", help="usage per turn (in, cached, out, usd)")
    pc_turns.add_argument("--agent", default=None)
    pc_turns.add_argument("--since", default="24h", help="duration (16h, 2d) or ISO date")
    pc_turns.add_argument("--limit", type=int, default=500)
    pc_turns.add_argument("--json", action="store_true")
    pc_turns.set_defaults(func=cmd_cost)
    pc_gauges = cost_sub.add_parser("gauges", help="plan gauge history")
    pc_gauges.add_argument("--harness", default=None)
    pc_gauges.add_argument("--since", default="7d")
    pc_gauges.add_argument("--json", action="store_true")
    pc_gauges.add_argument("--record", action="store_true",
                           help="first read the gauges from local logs (writes "
                                "the history); without it, read-only")
    pc_gauges.set_defaults(func=cmd_cost)
    pc_bal = cost_sub.add_parser(
        "balance", help="balance of the pay-per-token provider, actual spend per hour and day")
    pc_bal.add_argument("--provider", default=None, help="deepseek (default: all)")
    pc_bal.add_argument("--record", action="store_true",
                        help="read the balance now (read-only, free)")
    pc_bal.add_argument("--since", default="7d")
    pc_bal.add_argument("--json", action="store_true")
    pc_bal.set_defaults(func=cmd_cost)
    # L95 : correction tracée du grand livre (essai par défaut)
    from . import grand_livre
    grand_livre.add_parser(cost_sub, cmd_cost)

    # L30 (0027) : comptes multiples par fournisseur
    p_acc = sub.add_parser("accounts", help="accounts per provider: active, gauges, forcing (L30)")
    acc_sub = p_acc.add_subparsers(dest="accounts_command")
    pa_list = acc_sub.add_parser("list", help="declared accounts, active account, gauges, switches")
    pa_list.add_argument("--json", action="store_true")
    pa_list.add_argument("--last", type=int, default=5, help="latest switches shown")
    pa_list.set_defaults(func=cmd_accounts)
    pa_use = acc_sub.add_parser("use", help="force an account (no more automatic choice)")
    pa_use.add_argument("harness")
    pa_use.add_argument("account")
    pa_use.set_defaults(func=cmd_accounts)
    pa_auto = acc_sub.add_parser("auto", help="hand back to the automatic choice (0034)")
    pa_auto.add_argument("harness", nargs="?", default=None,
                         help="harness (default: all those that have accounts)")
    pa_auto.set_defaults(func=cmd_accounts)
    pa_ok = acc_sub.add_parser(
        "healthy", help="declare a reconnected account healthy (L226b: lifts auth_blocked)")
    pa_ok.add_argument("harness")
    pa_ok.add_argument("account", nargs="?", default="",
                       help="declared account (default: the harness's default credential)")
    pa_ok.set_defaults(func=cmd_accounts)
    pa_check = acc_sub.add_parser(
        "check", help="probe every credential of this host and report its health (L226a)")
    pa_check.add_argument("--json", action="store_true")
    pa_check.add_argument("--dry-run", action="store_true", help="probe only, write nothing")
    pa_check.set_defaults(func=cmd_accounts)
    p_acc.set_defaults(func=cmd_accounts)

    # L70 : plafonds de budget du mesh, en base (`ameesh budget [set|unset]`)
    from . import budget as budget_mod
    budget_mod.add_parsers(sub)
    # L131 : suites lourdes plafonnées par hôte
    from . import heavy as heavy_mod
    heavy_mod.add_parser(sub)

    p_set = sub.add_parser("set", help="settings of an agent, effective at the next turn (L13, L26)")
    p_set.add_argument("agent")
    p_set.add_argument("values", nargs="+", metavar="key=value",
                       help="model=… effort=… tier=… session_policy=par-lot|taille|jamais "
                            "lot_wake=on|off "
                            "context_max_tokens=15M|0 "
                            "turn_max_seconds=30m|2h|0 turn_mail_max=20|0 "
                            "turn_grace_seconds=20m|0 "
                            "mail_batch=90s|2m|0 (L125, local state of the host) "
                            "mode=execute|externe (empty value = default)")
    p_set.set_defaults(func=cmd_set)

    p_imp = sub.add_parser("import-v0", help="import the v0 file mailbox into Postgres")
    p_imp.add_argument("--state", default=None, help="v0 state (default AGENT_MAIL_STATE)")
    p_imp.add_argument("--agents", default=None, help="comma-separated list of names")
    p_imp.add_argument("--dry-run", action="store_true")
    p_imp.set_defaults(func=cmd_import_v0)
    p_exp = sub.add_parser("export-v0", help="write unread messages back into the v0 file mailbox")
    p_exp.add_argument("--state", default=None)
    p_exp.add_argument("--agents", default=None)
    p_exp.add_argument("--keep", action="store_true",
                       help="do not mark delivered in the database (copy instead of move)")
    p_exp.set_defaults(func=cmd_export_v0)

    p_mig = sub.add_parser("migrate")
    p_mig.add_argument("--dry-run", action="store_true",
                       help="missing migrations, additive or not; nothing is written")
    p_mig.add_argument("--additive-only", action="store_true",
                       help="apply nothing if a missing migration is not additive (L151)")
    p_mig.set_defaults(func=cmd_migrate)
    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--notify-test", action="store_true")
    p_doc.add_argument("--probe", action="store_true",
                       help="light probe: database, schema, migrations up to date; nothing else")
    p_doc.add_argument("--harness", action="store_true",
                       help="harness binaries served by the host (path, provenance, "
                            "interpreter) and runner units (L106)")
    p_doc.set_defaults(func=cmd_doctor)

    from . import canon_cli
    canon_cli.add_parsers(sub)

    from . import harness_cli
    harness_cli.add_parsers(sub)

    p_review = sub.add_parser(
        "review-class", help="review class of a change (0018): light, normal, sensitive")
    p_review.add_argument("paths", nargs="*", metavar="FILE",
                          help="files to classify, relative to the repository")
    p_review.add_argument("--diff", default=None, metavar="REF",
                          help="files changed since REF (ref..HEAD + working tree)")
    p_review.add_argument("--project", default=None, metavar="NAME",
                          help="project whose risk_classes apply (required when the "
                               "canon declares several)")
    canon_cli._canon_options(p_review)
    p_review.set_defaults(func=cmd_review_class)

    # L148 : la ligne d'usage liste les sous-commandes en clair, y compris
    # celles cachées ; on lui donne la liste visible (retirée au stage 7).
    sub.metavar = "{%s}" % ",".join(
        name for name in sub.choices if name not in HIDDEN_COMMANDS)
    return parser


def cmd_cost(cfg: Config, args) -> int:
    """`agent-mesh cost` — la comptabilité par tour et les jauges de forfait.

    Le module `ameesh.cost` ne lit les journaux des harnais qu'en lecture seule :
    ce qui est écrit ici, c'est la base (`turn_costs`, migration 0014).
    """
    what = getattr(args, "cost_command", None) or "report"
    if what == "correct":
        # L95 : l'essai lit aussi une base d'avant la migration 0043
        from . import grand_livre
        return grand_livre.main(cfg, args)
    db = _open(cfg)
    try:
        if what in ("turns", "gauges", "balance"):
            return _cost_l26(cfg, db, what, args)
        if what == "report":
            # L61 : le rapport lisait la dépense agent par agent (deux
            # requêtes chacun, ~67 s vers une base distante) : ses lectures
            # sont préchargées en quelques allers-retours (db.prefetch)
            db = db_mod.prefetch(db, lambda d: _cost_report_reads(cfg, d))
        # L70 : les plafonds en vigueur (base > configuration > défaut), ceux
        # que la garde des exécuteurs applique
        from . import budget as budget_mod
        limites = budget_mod.current(cfg, db)
        book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, **limites.book_kwargs())
        if what == "spent":
            seconds = float(getattr(args, "seconds", 3600.0) or 3600.0)
            print("%.4f" % book.spent(getattr(args, "agent", "all") or "all", seconds))
            return 0
        if what == "over":
            agent = getattr(args, "agent", "all") or "all"
            harness = book.tool_of(agent) if agent != "all" else ""
            items = accounts_mod.parse(cfg.accounts).get(harness) if harness else None
            if items:
                # L30 : le forfait n'est « dépassé » que si TOUS les comptes sont au seuil
                now = time.time()
                evals = [accounts_mod.evaluate(book, p, items, now) for p in items]
                reason = "" if any(e.ok for e in evals) else (
                    "all %s accounts at the threshold — %s" % (harness, "; ".join(
                        "%s: %s" % (e.profile.name, e.reason) for e in evals)))
                reason = reason or book.over(agent, pace=False)
            else:
                reason = book.over(agent)
            if reason:
                print(reason)
                return 0          # dépassé : c'est le code 0 de la référence
            return 1
        # L30 : jauges par compte et compte actif, pour les harnais à comptes
        try:
            # L71 : un affichage ne relève pas les jauges (lecture seule)
            comptes = accounts_mod.report(cfg, db, book, record=False)
        except accounts_mod.AccountError as exc:
            print("accounts: invalid configuration: %s" % exc, file=sys.stderr)
            comptes = []
        actifs = {}
        rythmes = {}
        for ligne in comptes:
            if ligne["active"]:
                actifs[ligne["harness"]] = (ligne["account"], ligne["_gauges"])
                rythmes[ligne["harness"]] = ligne.get("pace_multiplier", 1.0)
        rows = book.report(accounts=actifs, multipliers=rythmes)
        for ligne in comptes:
            for cle in ("_gauges", "_limits", "_ceilings"):
                ligne.pop(cle, None)
        if comptes:
            for row in rows:
                if row["harness"] in actifs:
                    row["accounts"] = [c for c in comptes if c["harness"] == row["harness"]]
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, sort_keys=True, default=str))
        else:
            print(cost_mod.format_report(rows))
            # L95 : dépense réelle (soldes) face à l'estimation, sur 24 h
            for entry in _real_spend_24h(db):
                print(balance_mod.describe(entry))
            print()
            print(budget_mod.summary_line(limites))
            for agent, caps in sorted(limites.agents.items()):
                print("  cap of %s: %s" % (agent, " · ".join(
                    "%s %.2f $" % (budget_mod.LABELS[w], v) for w, v in sorted(caps.items()))))
            if comptes:
                print()
                print("accounts (host %s):" % cfg.host)
                print(accounts_mod.format_rows(comptes))
        return 0
    finally:
        db.close()


def _real_spend_24h(db) -> list:
    """L95 : dépense réelle des fournisseurs payés au token sur 24 h, face à
    l'estimation du grand livre ; vide si rien n'est lisible."""
    try:
        paid = cost_mod.paid_harnesses_of()
    except cost_mod.CostError:
        return []
    now = time.time()
    return [dict(entry, period="24h")
            for entry in balance_mod.compare(db, paid, now - 86400.0, now)]


def _cost_report_reads(cfg: Config, db) -> None:
    """Les lectures de `cost report`, jouées à blanc par `db.prefetch` (L61) :
    plafonds (L70), comptes et rapport par agent. Rien n'est affiché."""
    from . import budget as budget_mod
    limites = budget_mod.current(cfg, db)
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, **limites.book_kwargs())
    try:
        comptes = accounts_mod.report(cfg, db, book)
    except accounts_mod.AccountError:
        comptes = []
    actifs = {ligne["harness"]: (ligne["account"], ligne["_gauges"])
              for ligne in comptes if ligne["active"]}
    rythmes = {ligne["harness"]: ligne.get("pace_multiplier", 1.0)
               for ligne in comptes if ligne["active"]}
    book.report(accounts=actifs, multipliers=rythmes)
    _real_spend_24h(db)


def cmd_accounts(cfg: Config, args) -> int:
    """`ameesh accounts list|use <harnais> <compte>|auto [harnais]|healthy
    <harnais> [<compte>]` (L30, 0027 ; L226b).

    Les profils viennent de la configuration de l'hôte ; seul l'état (compte
    actif, forçage, journal des bascules) est en base. Rien n'affiche de secret :
    un profil se montre par son type et son emplacement.
    """
    what = getattr(args, "accounts_command", None) or "list"
    declares = accounts_mod.parse(cfg.accounts)  # AccountError -> code 1, message
    qui = "human:%s" % (os.environ.get("USER") or getpass.getuser())
    db = _open(cfg)
    try:
        if what == "use":
            items = declares.get(args.harness)
            if not items:
                print("no account declared for %s (key `accounts` of the host "
                      "configuration)" % args.harness, file=sys.stderr)
                return 1
            accounts_mod.force(db, cfg.host, args.harness, items, args.account, by=qui)
            texte = ("Account %s forced for %s on %s by %s: no automatic choice "
                     "until \"ameesh accounts auto\"." % (args.account, args.harness,
                                                            cfg.host, qui))
            fil.record(cfg, db, sender=qui, recipients=[], text=texte,
                       meta={"action": "compte", "type": "manuel", "harnais": args.harness,
                             "vers": args.account, "audit": "accounts"})
            print(texte)
            return 0
        if what == "auto":
            noms = [args.harness] if args.harness else sorted(declares)
            for nom in noms:
                if nom not in declares:
                    print("no account declared for %s" % nom, file=sys.stderr)
                    return 1
                if accounts_mod.automatic(db, cfg.host, nom, by=qui):
                    texte = ("Accounts %s on %s: back to automatic choice (%s)."
                             % (nom, cfg.host, qui))
                    fil.record(cfg, db, sender=qui, recipients=[], text=texte,
                               meta={"action": "compte", "type": "auto", "harnais": nom,
                                     "audit": "accounts"})
                    print(texte)
                else:
                    print("accounts %s: already automatic" % nom)
            return 0
        if what == "check":
            from . import sentinel
            rows = sentinel.check(cfg, db, dry_run=args.dry_run)
            if args.json:
                print(json.dumps({"schema": "ameesh-credentials-check/1", "host": cfg.host,
                                  "credentials": rows}, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print("%-8s %-12s %-8s %s" % (row["harness"], row["account"] or "(default)",
                                                  row["state"],
                                                  row.get("kept") or row["reason"]))
            return 0
        if what == "healthy":
            items = declares.get(args.harness) or []
            if items and not args.account:
                print("accounts declared for %s: say which one (%s)"
                      % (args.harness, ", ".join(p.name for p in items)), file=sys.stderr)
                return 1
            if args.account and not accounts_mod.by_name(items, args.account):
                print("unknown account for %s: %r" % (args.harness, args.account),
                      file=sys.stderr)
                return 1
            storage.of(db).credentials.report(
                cfg.host, args.harness, args.account, "healthy",
                reason="declared healthy by %s" % qui, source="manual")
            texte = ("Account %s/%s declared healthy on %s by %s: the agents auth_blocked "
                     "on this account resume." % (args.harness, args.account or "(default)",
                                                  cfg.host, qui))
            fil.record(cfg, db, sender=qui, recipients=[], text=texte,
                       meta={"action": "compte", "type": "sain", "harnais": args.harness,
                             "compte": args.account, "audit": "accounts"})
            print(texte)
            return 0
        book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db)
        # L71 : `accounts list` est un affichage — aucun relevé écrit
        rows = accounts_mod.report(cfg, db, book, record=False)
        for row in rows:
            for cle in ("_gauges", "_limits", "_ceilings"):
                row.pop(cle, None)
        bascules = storage.of(db).accounts.switches(cfg.host, None, max(0, getattr(args, "last", 5))) \
            if declares else []
        if getattr(args, "json", False):
            try:
                sante = storage.of(db).credentials.listing(cfg.host)
            except db_mod.DbError:
                sante = []  # base sans la migration 0060
            print(json.dumps({"schema": "ameesh-accounts/1", "host": cfg.host,
                              "accounts": rows, "switches": bascules,
                              "credentials": sante},
                             ensure_ascii=False, indent=2, default=str))
            return 0
        print(accounts_mod.format_rows(rows))
        if bascules:
            print()
            print("latest switches:")
            for b in bascules:
                print("  %s  %-8s %-7s %s → %s  %s" % (
                    _fmt_moment(b["at_ts"]), b["harness"], b["kind"],
                    b["from_account"] or "—", b["to_account"], b["reason"] or ""))
        return 0
    finally:
        db.close()


def _since_seconds(text: str) -> float:
    from . import progress
    now = time.time()
    return max(0.0, now - progress.parse_since(text, now))


def _cost_l26(cfg: Config, db, what: str, args) -> int:
    """`cost turns|gauges|balance` (L26) : lectures du grand livre, de
    l'historique des jauges et des soldes ; le calcul des coûts n'est pas
    touché. Schémas : docs/EXPLOITATION.md."""
    ops = storage.of(db).operations
    if what == "turns":
        rows = ops.turns(agent=args.agent, since_s=_since_seconds(args.since),
                         limit=args.limit)
        if args.json:
            print(json.dumps({"schema": "ameesh-turns/1", "turns": rows},
                             ensure_ascii=False, indent=2))
            return 0
        print("%-19s %-16s %-6s %-9s %10s %10s %9s %10s" % (
            "WHEN", "AGENT", "LOT", "HARNESS", "INPUT", "CACHE", "OUTPUT", "USD"))
        for row in rows:
            print("%-19s %-16s %-6s %-9s %10d %10d %9d %10.4f" % (
                _fmt_moment(row["recorded_ts"]), row["agent"][:16],
                "#%d" % row["work_item_id"] if row.get("work_item_id") else "—",
                row["harness"][:9],
                row["input_tokens"], row["cached_input_tokens"], row["output_tokens"],
                row["usd"]))
        return 0
    if what == "gauges":
        if args.record:
            # `--record` : un relevé frais d'abord, depuis les journaux locaux
            # de l'hôte, comme `cost report` (une ligne seulement si la jauge
            # a bougé). L30 : un harnais à comptes déclarés est relevé compte
            # par compte. Sans l'option (L61), la commande ne fait que lire
            # l'historique : l'exécuteur relève déjà avant chaque tour.
            book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db)
            declares = accounts_mod.parse(cfg.accounts)
            accounts_mod.report(cfg, db, book,
                                harnesses=[args.harness] if args.harness else None)
            for nom in ([args.harness] if args.harness else ["claude", "codex"]):
                if nom not in declares:
                    book.gauges(nom)
        rows = ops.gauge_history(since_s=_since_seconds(args.since), harness=args.harness)
        if args.json:
            print(json.dumps({"schema": "ameesh-gauges/1", "readings": rows},
                             ensure_ascii=False, indent=2))
            return 0
        for row in rows:
            print("%s  %-7s %-16s %5.1f%%  resets %s" % (
                _fmt_moment(row["observed_ts"]),
                row["harness"] + ("/%s" % row["account"] if row.get("account") else ""),
                row["key"],
                float(row["used"]) * 100, _fmt_moment(row.get("resets_at_ts"))))
        return 0
    from . import balance as balance_mod
    names = [args.provider] if args.provider else None
    if args.provider and args.provider not in balance_mod.SOURCES:
        print(USAGE_HINT % ("provider without a balance source: %s (known: %s)"
                            % (args.provider, ", ".join(balance_mod.SOURCES))),
              file=sys.stderr)
        return 2
    errors = []
    if args.record:
        # L30 : un relevé par compte de clé d'API déclaré
        for source, compte in balance_mod.account_sources(cfg, names=names):
            nom = source.provider + ("/%s" % compte if compte else "")
            if not source.configured():
                errors.append("%s: key missing from the environment" % nom)
                continue
            try:
                balance_mod.record(db, source, compte)
            except balance_mod.BalanceError as exc:
                errors.append("%s: %s" % (nom, exc))
    rows = ops.balances(provider=args.provider, since_s=_since_seconds(args.since))
    report = dict(balance_mod.spend(rows), schema="ameesh-balance/1", errors=errors)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for row in report["latest"]:
            print("balance %s: %.4f %s (read %s)" % (
                row["provider"] + ("/%s" % row["account"] if row.get("account") else ""),
                row["total"], row["currency"],
                _fmt_moment(row["observed_ts"])))
        for slot in report["daily"]:
            print("day   %s  %s %.4f %s" % (slot["start"][:10], slot["provider"],
                                            slot["spent"], slot["currency"]))
        for slot in report["hourly"][-24:]:
            print("hour  %s  %s %.4f %s" % (slot["start"][:16], slot["provider"],
                                            slot["spent"], slot["currency"]))
        for error in errors:
            print("error: %s" % error, file=sys.stderr)
    return 1 if errors and args.record and not rows else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    cfg = config_mod.load()
    try:
        return args.func(cfg, args)
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, authority.AuthorityError, work.WorkError, ValueError,
            FileNotFoundError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
