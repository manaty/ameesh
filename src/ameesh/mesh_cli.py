#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""agent-mesh — the mesh CLI: observability, keys, approvals, {works}.

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
  agent-mesh work wake <id> --at ISO | --in DURATION | --cancel   scheduled wake, even under hold (#2466)
  agent-mesh work assign <id> <agent> [--externe] [--actor A]   reassign (guarded assignment)
  agent-mesh work external-executor <id> <agent> | --clear   reserve a {work} to an external executor (#3447)
  agent-mesh work merged <id> --sha S | --pr owner/repo#N [--note …]   merge done outside a PR: any open state → merged
  agent-mesh work migration <id> [--number N] [--release [--number N]] [--skip-branch B]   reserve one of the {work}'s migration numbers (L122, #2481)
  agent-mesh work issue-ref <id> owner/repo#N | --none   correct the issue reference, journaled (#1603)
  agent-mesh work set-type <id> bug|evolution   retype a {work}, journaled (#2954)
  agent-mesh work set-priority <id> 1|2|3 --reason "…"   change an open {work}'s priority, journaled (#3370)
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
  agent-mesh conflicts [--project P] [--all] [--json]   conflict radar between {works} (L160b)
  agent-mesh ci status <item> [--limit N] [--json]      live CI, failures (L161c)
  agent-mesh ci run <item> [--avis N] [--exception …]   the CI of a frozen {work}, once,
        [--full] [--sha S] [--force] [--dry-run]        on the frozen commit (#1343)
  agent-mesh ci cancel <item> [--all] [--dry-run]       cancel the stale runs of a {work}
  agent-mesh agent spawn <name> --by <creator> --ttl 2h   ephemeral agent (R14)
  agent-mesh agent move NOM --to HOST [--keep-session]   manual move of a `releve: hote`
                                                          agent, fenced (#3299)
  agent-mesh agent retire NOM --reason R | restore NOM   retired: never designated,
                                                          claimed nor mailed (#3460)

The owner's authority is never inferred from text: it is proven by an Ed25519
signature whose public key is in agent_registry, bound to the content and to
an expiry. `key generate` is an act of the owner: no agent may generate or
hold its private key.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import argparse
import getpass
import json
import os
import re
import sys
import time

from . import accounts as accounts_mod
from . import agent_pause
from . import account_window
from . import balance as balance_mod
from . import authority, catalog, mail, config as config_mod, cost as cost_mod, db as db_mod, identity
from . import migrations, placement, progress, registry
from . import rebalance as rebalance_mod
from . import fil, release, signing, storage, vocab, work, work_lifecycle
from .config import Config
from .db import Db
from .deprecated import HiddenAliasParser

USAGE_HINT = "agent-mesh: %s"


def _vocabulary(vocabulary=None):
    """The work-item vocabulary of the active module (L102 T2, #4392).

    ``vocabulary`` is the test seam; without it the dev module's DEV ("lot")
    is read through :func:`ameesh.vocab.active`, CORE ("tâche") when no active
    module declares one."""
    return vocab.active() if vocabulary is None else vocabulary


def _words(vocabulary=None) -> dict:
    """The words the help texts substitute into their ``{…}`` placeholders."""
    v = _vocabulary(vocabulary)
    return {"work": v.noun, "works": v.plural}


def _doc(vocabulary=None) -> str:
    """``agent-mesh --help``: the module description in the active vocabulary."""
    return __doc__.format(**_words(vocabulary))


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
    # #1070 : the budget pause, distinct from the `state` "paused" of a
    # `blocked` for another cause (host not ready, authentication) — a
    # machine reader must not have to decode a French `status_text`. Set
    # here rather than in `exploitation.agent_view` (§8 prod): `agent_rows`
    # also serves `GET /api/v1/agents`; both paths carry it without touching
    # prod.
    for row in rows:
        row["paused_on_budget"] = progress.paused_on_budget(row)
        # #2814 : l'état « queued » d'un tour différé, lisible par un client
        # sans décoder le texte français (`ameesh-agents/1`,
        # `GET /api/v1/agents`) ; None hors d'une attente de pression.
        row["queue"] = _queue_of(row)
    return rows


def _queue_of(row) -> dict | None:
    """#2814 : le `queued` object d'une ligne d'agent (None sinon)."""
    from . import resources as resources_mod
    return resources_mod.parse_queue_status((row or {}).get("status_text"))


def agent_rows(cfg: Config, db) -> list:
    """The rows of `ameesh list --json` (also `GET /api/v1/agents`, L84)."""
    from .projects import agent_project
    rows = registry.overview(db)
    for row in rows:
        row["project"] = agent_project(row)
    return _annotate_agents(cfg, db, rows)


def agent_detail(db, name: str, *, cfg=None, footprint: bool = False) -> dict | None:
    """The object of `ameesh show --json` (also `GET /api/v1/agents/{a}`)."""
    # L61 : la ligne et la clé en un seul aller-retour (db.batched)
    row, info = db_mod.batched(db, lambda db: (
        registry.get(db, name), authority.key_info(db, name)))
    if row is None:
        return None
    row = dict(row)
    # #1070 : the budget pause reaches `show --json` too, as it does
    # `list --json` and `/api/v1/agents` (set in `_annotate_agents`)
    row["paused_on_budget"] = progress.paused_on_budget(row)
    # #2814 : même objet `queued` que `ameesh list --json`
    row["queue"] = _queue_of(row)
    row["key_ready"] = bool(info and info.get("public_key") and not info.get("key_revoked_ts"))
    row["public_key_fingerprint"] = (info or {}).get("public_key_fingerprint")
    row["key_updated_ts"] = (info or {}).get("key_updated_ts")
    row["key_revoked_ts"] = (info or {}).get("key_revoked_ts")
    row["footprint"] = None
    row["footprint_unavailable"] = None
    # #4375 : l'empreinte de l'hôte (#4150) coûte ~15 s (host_idle.snapshot :
    # ~170 requêtes et le canon rechargé) ; `show` est lu par vm.sh sous une
    # borne de 30 s et par les runners. Elle n'est calculée que sur demande ;
    # `ameesh hosts` la porte.
    if footprint and cfg is not None and row.get("host"):
        from . import host_idle, resources, canon as canon_mod
        try:
            readings = {r["host"]: r for r in resources.current(db, row["host"])}
            controls = {r["host"]: r
                        for r in storage.of(db).host_lifecycle.listing()}
            row["footprint"] = host_idle.snapshot(
                cfg, db, canon_mod.load_configured(cfg), readings,
                controls=controls).get(row["host"])
        except Exception as exc:  # optional diagnostics must not hide the agent
            row["footprint_unavailable"] = " ".join(str(exc).split())[:120]
    return row


def _handoff_operator(cfg, db, args):
    """H6: one authorized controller step and a closed public response."""
    from . import handoffs, host_lifecycle
    work.corrector(cfg, db, args.actor, what="handoff operator", history=False)
    operation = storage.of(db).handoffs.get(args.operation)
    if operation is None:
        raise ValueError("unknown handoff")
    try:
        from . import handoff_factory
        binding = handoff_factory.load(db)
        adapter = binding.adapter
        if "lifecycle_host" in operation.get("plan", {}):
            adapter = host_lifecycle._ReadyAdapter(db, adapter,
                                                  binding.requirements_for)
        if args.handoff_command == "retry":
            result = handoffs.retry(db, args.operation, adapter,
                                    renew_ttl=args.renew_ttl)
        elif args.handoff_command == "cancel":
            result = handoffs.cancel(db, args.operation, adapter)
        else:
            result = handoffs.advance(db, args.operation, adapter)
    except Exception as exc:
        if isinstance(exc, handoffs.HandoffRefusal) and isinstance(exc.reason, handoffs.Refusal):
            reason = exc.reason
            # Read only the closed category, never str(exc) or helper diagnostics.
            raise ValueError("handoff step refused; %s: %s" %
                             (reason.name.lower(), reason.value)) from None
        raise ValueError("handoff step refused; inspect state and evidence with "
                         "ameesh handoff show OPERATION") from None
    public = {key: result[key] for key in
              ("id", "agent", "source", "target", "state", "reserved_until_ts")
              if key in result}
    print(json.dumps({"operation": public}, ensure_ascii=False))
    return 0


def _handoff_ttl(value):
    try:
        ttl = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("TTL must be seconds in 30..7200") from None
    if not 30 <= ttl <= 7200:
        raise argparse.ArgumentTypeError("TTL must be seconds in 30..7200")
    return ttl


def cmd_handoff(cfg: Config, args: argparse.Namespace) -> int:
    """L232a planner/journal; activation is supplied by the L232b controller."""
    from . import canon, handoffs, identity, resources, storage
    db = _open(cfg)
    try:
        if args.handoff_command in ("advance", "cancel", "retry"):
            return _handoff_operator(cfg, db, args)
        if args.handoff_command == "show":
            row = storage.of(db).handoffs.get(args.operation)
            if row is None:
                raise ValueError("unknown handoff")
            print(json.dumps({"operation": row,
                              "events": storage.of(db).handoffs.events(args.operation)}, ensure_ascii=False))
            return 0
        from .agent_names import resolve
        args.agent = resolve(args.agent, db=db, operation="handoff.agent")
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
    v = _vocabulary()
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
        build_by_agent: dict[str, int] = {}
        # §4 : le plafond du canon compilé (#2451) ; illisible, la vue montre
        # l'en-cours sans juger le dépassement
        from . import mechanics
        try:
            cap: int | None = work.build_limit(cfg)
        except mechanics.MechanicsError:
            cap = None
        for lot in lots:
            if lot.get("assignee") and work_lifecycle.phase(lot) not in ("promoted", "closed"):
                open_by_agent[lot["assignee"]] = open_by_agent.get(lot["assignee"], 0) + 1
                if lot.get("state") == lifecycle.IN_PROGRESS:
                    # §4 (owner 14/10/2026) : le compte d'en-cours est tous
                    # projets confondus, comme le plafond lui-même
                    build_by_agent[lot["assignee"]] = build_by_agent.get(lot["assignee"], 0) + 1
        # regroupé par projet (tri stable : dans un projet, l'ordre de
        # `overview`, vu le plus récemment d'abord) ; les sans-projet en dernier
        rows.sort(key=lambda r: (r["project"] is None, r["project"] or ""))
        # #1782 : la colonne HARNESS fait 10, pas 9 — la valeur étoilée d'un
        # harnais vivant de 9 lettres (`(live or '?')[:9] + '*'`) en fait 10 ;
        # à 9, la ligne entière se décalait d'un caractère.
        print("%-20s %-12s %-10s %-10s %-11s %-22s %-8s %-5s %-5s %-13s %-4s %s" % (
            "NAME", "PROJECT", "HARNESS", "HOST", "STATUS", "LEASE", "UNREAD", "ITEMS",
            "BUILD", "BUDGET", "KEY", "ACTIVITY"))
        session_differs = False
        for row in rows:
            status = row.get("status") or "?"
            if row.get("status_text"):
                status = "%s/%s" % (status, row["status_text"][:22])
            if row.get("operator_paused") and row.get("status") not in ("stopped", "dead", "blocked"):
                status = ("pause-next" if row.get("status") == "running" else "op-paused")
            if progress.paused_on_budget(row):
                # #1070 : the word "pause" must survive the column truncation
                # — "blocked/bud" hid the budget pauses (the bureau's tally
                # counted none).
                status = "paused/%s" % row["status_text"][:22]
            if row.get("mode") == "externe":
                # L37 (0030) : session humaine, jamais réveillée par ameesh
                status = "ext/" + status
            # #1250 : la colonne dit le harnais de la session VIVANTE (L242b) ;
            # une étoile signale qu'il diffère du harnais déclaré, et le pied
            # explique la marque (la déclaration reste lisible dans `show`).
            live = registry.live_harness(row)
            declared = row.get("harness") or ""
            starred = bool(live) and live != declared
            if starred:
                session_differs = True
            wip = build_by_agent.get(row["name"], 0)
            # #3337 : la colonne dit le dernier signe de vie, pas seulement
            # l'inscription au registre — une session `externe` qui écrit ne
            # touche jamais `last_seen` et paraissait absente.
            presence_ts, presence_source = registry.presence(row)
            last = _fmt_age(now - presence_ts)
            if presence_source == "wrote":
                last = "wrote " + last
                # #3729 : un signe de vie peut venir d'un autre hôte que celui
                # déclaré (session externe : bureau) ; le nommer évite de lire
                # la colonne HOST comme la preuve de la dernière activité.
                wrote_from = (row.get("last_mail_host") or "").strip()
                if wrote_from and wrote_from != (row.get("host") or "").strip():
                    last += " from " + wrote_from
            print("%-20s %-12s %-10s %-10s %-11s %-22s %-8d %-5d %-5s %-13s %-4s %s" % (
                row["name"][:20], project_label(agent_project_label(row))[:12],
                ((live or "?")[:9] + ("*" if starred else "")),
                (row.get("host") or "")[:10],
                status[:11], _lease(row), int(row.get("unread") or 0),
                open_by_agent.get(row["name"], 0),
                "%d!" % wip if cap is not None and wip > cap else str(wip), _budget(row),
                ("owner" if row.get("has_owner_key")
                 else "agent" if row.get("key_ready") else "—"),
                last,
            ))
        if session_differs:
            print("* session harness: the live session runs under another "
                  "harness than the declared one (L242b)")
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
        # §4 : qui dépasse le plafond d'en-cours (le balayage rend le surplus)
        over = sorted(name for name, count in build_by_agent.items()
                      if cap is not None and count > cap)
        if over:
            print(('wip      : %d agent(s) above the §4 cap (%d %s in build): %s — ameesh work wip [--apply] returns the surplus to the team queue') % (len(over), cap, v.plural, ", ".join(over)))
        elif cap is None:
            print("wip      : §4 cap unknown (build.max_in_build absent from the "
                  "compiled canon): no overrun judged")
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


def _host_reading_staleness(cfg: Config, latest: dict | None) -> tuple[bool, float | None]:
    """#3452 : l'âge du dernier relevé et sa péremption.

    Le seuil est celui de l'alerte `host_unsampled` (`exploitation`), pour que
    `ameesh hosts` ne calcule pas de pression sur un relevé que l'alerte
    déclare muet. Un relevé sans `sampled_ts` n'est pas datable : ni âge ni
    péremption déduits (fail-open sur la seule datation, jamais sur la mesure).
    """
    if latest is None:
        return False, None
    from . import exploitation
    from . import resources as res

    sampled = res.finite_float(latest.get("sampled_ts"))
    if sampled is None:
        return False, None
    age = max(0.0, time.time() - sampled)
    return age > exploitation.host_unsampled_after_s(cfg), age


def _host_parts(cfg: Config, db, canons, only: str | None, history: int) -> list[dict]:
    """What `ameesh hosts` shows of each host (all, or `only`), by name."""
    from . import canon as canon_mod
    from . import host_holds
    from . import menage as menage_mod
    from . import placement as placement_mod
    from . import resources as res

    holds = host_holds.live(db)
    latest_by_host = {row["host"]: row for row in res.current(db)}
    from . import host_idle
    # #4148: show the durable release state next to the read-only footprint.
    controls = {row["host"]: row for row in storage.of(db).host_lifecycle.listing()}
    footprints = host_idle.snapshot(cfg, db, canons, latest_by_host, controls=controls)
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
        latest = latest_by_host.get(name)
        # #2825 N4 : le défaut de CET hôte vient de SA lecture (ou de sa
        # fiche) ; pour l'hôte local sans relevé, de sa propre détection.
        default_scope = res.pressure_scope(None) if name == cfg.host else None
        physical = res.host_limits(canons, name, reading=latest,
                                   default_scope=default_scope)
        # #3452 : un relevé périmé n'est pas une mesure du présent — la vue
        # publie son âge et ne calcule pas de pression dessus.
        stale, age = _host_reading_staleness(cfg, latest)
        parts.append({
            "name": name, "fiches": fiches, "physical": physical, "latest": latest,
            "footprint": footprints.get(name),
            "host": fiches[0][1] if fiches else None,
            "admissions": {canon.id: _host_admissions(canon, name)
                           for canon, _fiche in fiches},
            "stale": stale, "sampled_age_s": age,
            "verdict": (None if stale else res.pressure(latest, limits=physical["limits"]))
                       if latest is not None else None,
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
    measurement = res.measure(latest, limits.get("lend")) if latest is not None else None
    if measurement is not None:
        measurement.update(part.get("footprint") or {})
    return {
        "schema": "ameesh-host/1",
        "host": part["name"],
        "responsible": host.responsible if host is not None else None,
        "limits": limits,
        # #2814 : le périmètre d'admission, à côté des seuils qu'il gouverne
        "pressure_scope": limits.get("pressure_scope"),
        "scope_fallback": physical.get("scope_fallback"),
        "max_agents": physical["max_agents"],
        "limits_origin": physical["origin"],
        "fiches": [{"canon": canon.id, "default": canon.is_default,
                    "responsible": fiche.responsible,
                    "max_agents": fiche.policy.max_agents,
                    "admissions": part["admissions"].get(canon.id, 0)}
                   for canon, fiche in part["fiches"]],
        "latest": latest,
        "footprint": part.get("footprint"),
        # #3452 : l'âge du relevé et sa péremption, à côté de la mesure
        # qu'ils qualifient (un relevé périmé ne porte pas de pression)
        "sampled_age_s": part.get("sampled_age_s"),
        "stale": bool(part.get("stale")),
        # #253: every filesystem the host writes to, its own reading
        "volumes": (latest or {}).get("volumes") or [],
        # L159d: the offer, and the common measure (study §1.4)
        # whose lendable capacity it caps
        "offer": limits.get("lend"),
        "measure": measurement,
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

    if getattr(args, "send", None) and (
            not getattr(args, "fleet", False)
            or any(getattr(args, flag, False)
                   for flag in ("json", "publish", "report", "hold", "release"))):
        print("ameesh hosts: --send requires --fleet in text mode, "
              "without --publish, --report, --hold or --release", file=sys.stderr)
        return 2
    if args.hold or args.release:
        return _host_hold(cfg, args)
    if getattr(args, "publish", False):
        return _host_publish(cfg, args)
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
        if getattr(args, "fleet", False):
            return _host_fleet(cfg, db, canons, args)
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
            if part.get("footprint"):
                from . import host_idle
                print("  " + host_idle.line(part["footprint"]))
            _print_hold(part["hold"])
            _print_host_fiches(fiches, physical, admissions, many=many)

            def tag(key: str) -> str:
                return " [%s]" % origin.get(key, res.DEFAULT_ORIGIN) if fiches else ""

            # #2354: no declared swap ceiling and no PSI to corroborate one —
            # the host is under the 0028 conservative fallback, the stock is
            # then the only memory signal; say it instead of a bare "—".
            swap = limits["max_swap_used"]
            if swap is None and latest.get("psi_mem_some") is None:
                swap = res.DEFAULT_MAX_SWAP_USED_WITHOUT_PSI
                swap_tag = " [fallback: no PSI]"
                swap_note = "counts alone, no PSI"
            else:
                # #2296: the declared ceiling, or none at all — "— [défaut]"
                # would claim a default value that does not exist
                swap_tag = tag("max_swap_used") if swap is not None else " [no default]"
                swap_note = "counts only under memory pressure"

            print("  thresholds memory ≥ %s%s; swap ≤ %s%s (%s); load ≤ %.2f%s; disk ≥ %s%s"
                  "; tmpfs ≤ %s%s"
                  % (_fmt_bytes(limits["min_mem_available"]), tag("min_mem_available"),
                     _fmt_bytes(swap), swap_tag, swap_note,
                     limits["max_load"], tag("max_load"),
                     _fmt_bytes(limits["min_disk_free"]), tag("min_disk_free"),
                     res.fmt_value("max_tmpfs_used", limits.get("max_tmpfs_used")),
                     tag("max_tmpfs_used")))
            # #2296: the admission signal that follows the real pressure
            print("  thresholds memory pressure ≤ %s%s"
                  % (res.fmt_value("max_mem_pressure", limits["max_mem_pressure"]),
                     tag("max_mem_pressure")))
            # #2814 : le périmètre de pression (cgroup du processus ou hôte) ;
            # en `own_cgroup`, la charge 1 min n'est plus un critère
            # #2825 N2 : un repli faute de mesure cgroup est dit ici.
            scope_note = "; load advisory only" \
                if limits.get("pressure_scope") == "own_cgroup" else ""
            if physical.get("scope_fallback"):
                scope_note = "; " + physical["scope_fallback"]
            print("  scope      pressure on %s%s%s" % (
                res.scope_label(limits.get("pressure_scope")),
                tag("pressure_scope") if fiches else "",
                scope_note))
            if limits.get("pressure_scope") == "own_cgroup":
                print("  thresholds cgroup memory ≤ %s%s; cgroup throttled ≤ %s%s"
                      % (res.fmt_value("max_cgroup_memory_used",
                                       limits.get("max_cgroup_memory_used")),
                         tag("max_cgroup_memory_used"),
                         res.fmt_value("max_cgroup_throttled",
                                       limits.get("max_cgroup_throttled")),
                         tag("max_cgroup_throttled")))
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
            # #2814 : la mesure du cgroup de l'exécuteur (conteneur), publiée
            # avec le relevé ; c'est elle que `own_cgroup` gouverne
            cgroup = latest.get("cgroup") or {}
            if cgroup:
                parts = []
                if cgroup.get("path"):
                    parts.append("path %s" % cgroup["path"])
                if cgroup.get("mem_used") is not None:
                    parts.append("memory %s / %s (%s)" % (
                        _fmt_bytes(cgroup.get("mem_current_bytes")),
                        _fmt_bytes(cgroup.get("mem_max_bytes")),
                        res.fmt_value("max_cgroup_memory_used", cgroup["mem_used"])))
                if cgroup.get("cpu_quota"):
                    parts.append("CPU quota %.2f" % cgroup["cpu_quota"])
                if cgroup.get("throttled") is not None:
                    parts.append("throttled %s" % res.fmt_value(
                        "max_cgroup_throttled", cgroup["throttled"]))
                if cgroup.get("cpu_some") is not None:
                    parts.append("CPU PSI %.1f %%" % cgroup["cpu_some"])
                if cgroup.get("mem_some") is not None:
                    parts.append("memory PSI %.1f %%" % cgroup["mem_some"])
                if parts:
                    print("  cgroup     %s" % "; ".join(parts))
            # #4433 : la PSI mémoire de l'hôte suit un runner à son propre
            # memory.max — la brèche est nominative, l'hôte n'est pas retenu.
            for runner in (verdict or {}).get("attributed") or ():
                print("  pressure   runner %s at %s of its memory.max (%s); its own "
                      "memory PSI %s — its new turns are held, the host's are not"
                      % (runner.get("agent") or runner.get("unit") or "?",
                         res.fmt_value("max_cgroup_memory_used", runner.get("mem_used")),
                         _fmt_bytes(runner.get("mem_max_bytes")),
                         res.fmt_value("max_mem_pressure", runner.get("mem_some"))))
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

            if part.get("stale"):
                print("  pressure   unknown (reading stale, no verdict)")
            elif verdict and verdict["breaches"]:
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
    limits = res.host_limits(
        canons, args.host, reading=readings[-1] if readings else None,
        default_scope=res.pressure_scope(None) if args.host == cfg.host else None,
    )["limits"]
    rep = pilot.report(args.host, readings, limits, days=days)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    else:
        for line in pilot.format_report(rep):
            print(line)
    return 0


def _host_publish(cfg: Config, args) -> int:
    """`ameesh hosts --publish` (#2022): one reading of THIS host, published
    like a runner's, for a host that runs no agent (CI VMs, the mesh server):
    a systemd timer runs it every few minutes. The reading carries the
    runner id `publish@HOST`."""
    import dataclasses

    from . import resources as res

    if not cfg.host:
        print("ameesh hosts --publish: no host name in the configuration", file=sys.stderr)
        return 2
    if args.host and args.host != cfg.host:
        print("ameesh hosts --publish: a host publishes its own reading only (%s)"
              % cfg.host, file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        reading = res.collect(dataclasses.replace(cfg, runner_id="publish@%s" % cfg.host), db)
    finally:
        db.close()
    if args.json:
        print(json.dumps(reading, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print("%s: reading published (disk %s free, %s zombies)" % (
            cfg.host, _fmt_bytes(reading.get("disk_free_bytes")),
            "—" if reading.get("procs_zombie") is None else reading["procs_zombie"]))
    return 0


def _host_fleet(cfg, db, canons, args) -> int:
    """`ameesh hosts --fleet` (#2022): one line per measured or canonical host —
    age of its last reading (stale beyond 1 h), worst disk, zombies, what it
    breaches or warns about; the hosts with something to say first. Meant
    for a daily summary in the bureau's and the owner's thread."""
    from . import resources as res

    now = time.time()
    lines = []
    readings = {r["host"]: r for r in storage.of(db).hosts.current(args.host)}
    names = ({args.host} if args.host else set(readings) | {
        host.title for canon in canons for host in canon.hosts})
    for name in sorted(names):
        reading = readings.get(name, {"host": name})
        # #2825 N4 : chaque hôte a SON périmètre, lu dans son relevé ; seul
        # l'hôte local sans relevé retombe sur sa propre détection.
        limits = res.host_limits(
            canons, reading["host"], reading=readings.get(name),
            default_scope=res.pressure_scope(None) if name == cfg.host else None,
        )["limits"]
        history = (storage.of(db).hosts.disk_history(
            reading["host"], res.DISK_TREND_WINDOW_S, res.DISK_TREND_STEP_S)
            if name in readings else [])
        lines.append(res.fleet_line(reading, limits, [h for h in history
                                                      if h.get("id") != reading.get("id")],
                                    now=now))
    lines.sort(key=lambda line: (line["ok"], line["host"] or ""))
    if args.json:
        for line in lines:
            print(json.dumps(line, ensure_ascii=False, sort_keys=True))
        return 0
    text = "\n".join(["fleet: %d host(s), %d to look at"
                      % (len(lines), sum(not l["ok"] for l in lines))]
                     + ["  " + _fleet_text(line) for line in lines])
    print(text)
    # #2022: the daily summary, mailed as is to each recipient's thread (the
    # bureau's, read by the owner); any failure exits non-zero so the timer
    # unit shows failed instead of losing the summary
    failed = 0
    for name in getattr(args, "send", None) or ():
        try:
            mail.send(db, FLEET_SENDER, name, text, kind="notify")
        except Exception as exc:
            failed += 1
            print("ameesh hosts --fleet: not sent to %s (%s: %s)"
                  % (name, type(exc).__name__, exc), file=sys.stderr)
    return 1 if failed else 0


#: #2022: the sender of the mailed fleet summary
FLEET_SENDER = "ameesh"


def _fleet_text(line: dict) -> str:
    """« agents-1 [!] measured 2m ago; disk / 91 % used, 9.0 GiB free; … »."""
    parts = ["no published measurement (STALE)" if line["age_s"] is None else
             "measured %s%s" % (_fmt_age(line["age_s"]), " (STALE)" if line["stale"] else "")]
    disk = line.get("disk")
    if disk is not None:
        parts.append("disk %s %s%s free" % (
            disk["path"], "" if disk["used"] is None else "%d %% used, " % round(
                disk["used"] * 100), _fmt_bytes(disk["free"])))
    eta = [w["floor_eta_s"] for w in line["disk_warnings"] if w.get("floor_eta_s") is not None]
    if eta:
        parts.append("pause floor in %.1f h" % (min(eta) / 3600))
    if line.get("zombies") is not None:
        parts.append("%d zombies of %s processes" % (line["zombies"], line.get("procs")))
    if line["breaches"]:
        parts.append("breaches %s" % ", ".join(line["breaches"]))
    return "%s %s %s" % (line["host"], "ok " if line["ok"] else "[!]", "; ".join(parts))


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
    # #2022: processes and zombies, of the host and of the publishing runner
    if latest.get("procs_total") is not None:
        out.append("  processes  %d, zombies %d; runner %s: %s, zombies %s" % (
            latest["procs_total"], latest.get("procs_zombie") or 0,
            latest.get("runner_id") or "—", latest.get("runner_procs", "—"),
            latest.get("runner_zombies", "—")))
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
    # #3802 : un passage sauté derrière un verrou trop vieux est dit ici,
    # avec son détenteur ; une passe aboutie (relevé plus récent) le clôt.
    host_pass = None
    try:
        rows = [row for row in hk.latest_by_path(host, "host_pass")
                if row.get("action") == "blocked"]
        if rows and float(rows[0].get("at_ts") or 0) >= float(
                (measure_row or {}).get("at_ts") or 0):
            row = rows[0]
            item = row.get("data") or {}
            item = dict(item, at_ts=row.get("at_ts"), detail=row.get("detail") or "")
            host_pass = item
    except Exception:  # un journal illisible ne prouve pas un blocage
        host_pass = None
    return {
        "policy": policy.as_dict(),
        "system_tmp": tmp,
        "tmpfs_alert": bool(tmp and tmp["fraction"] is not None
                            and tmp["fraction"] >= policy.tmpfs_alert),
        "measure": data or None,
        "measured_ts": (measure_row or {}).get("at_ts"),
        "host_pass": host_pass,
        "last_24h": hk.summary(host, 86400.0),
        "worktrees": {"active": sum(1 for w in worktrees if w["status"] == "active"),
                      "kept": [{"path": w["path"], "agent": w["agent"], "lot": w["lot"],
                                "detail": w["detail"]}
                               for w in worktrees if w["status"] == "kept"]},
    }


def _print_host_menage(menage: dict) -> None:
    v = _vocabulary()
    tmp = menage.get("system_tmp")
    policy = menage["policy"]
    blocked = menage.get("host_pass")
    if blocked:
        print("  blocked    housekeeping pass skipped: host-pass lock held by %s "
              "(pid %s)%s since %s — alert housekeeping_blocked"
              % (blocked.get("runner") or "?",
                 blocked.get("pid") if blocked.get("pid") is not None else "?",
                 " in phase %r" % blocked.get("phase") if blocked.get("phase") else "",
                 time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(
                     float(blocked.get("since") or 0))) if blocked.get("since") else "?"))
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
        # #3728 : les archives des worktrees gardés pour fichiers modifiés
        # (ce que leur retrait a libéré est dans la ligne 24 h : `worktree
        # archived`)
        arch = m.get("worktree_archives") or {}
        print("  cleanup    temporary directories %s (quota %s per agent); caches %s / %s"
              "%s%s" % (_fmt_bytes(m.get("tmp_bytes")), _fmt_bytes(m.get("tmp_quota")),
                        ("at least " if m.get("cache_partial") else "")
                        + _fmt_bytes(m.get("cache_bytes")), _fmt_bytes(m.get("cache_quota")),
                        "; %d /tmp entries reported (%s)" % (
                            m["orphans"], _fmt_bytes(m.get("orphan_bytes")))
                        if m.get("orphans") else "",
                        "; %d worktree archive(s) (%s)" % (
                            arch["count"], _fmt_bytes(arch.get("bytes")))
                        if arch.get("count") else ""))
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
        print(('  worktrees  %d tracked, waiting for their %s to end; %d kept%s') % (wt.get("active", 0), v.noun, len(wt.get("kept") or []), ": " + "; ".join("%s (%s)" % (k["path"], k["detail"]) for k in wt["kept"][:5])
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


def _host_admissions(canon, name: str) -> int:
    """Admissions de `name` dans un canon : les épinglées, plus les rôles élus
    qui y tiennent leur siège (#3101 : un rôle élu ne compte qu'une fois)."""
    from . import placement as placement_mod
    pinned, seats = placement_mod.host_load(canon)
    return pinned.get(name, 0) + sum(1 for seat in seats.values() if seat == name)


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


def _agent_credits(db, row: dict) -> tuple[str, list]:
    """#4164 : (harnais, lignes de crédit à montrer) d'un agent — le compte
    de sa session s'il est suivi, sinon les comptes non `ok` du harnais."""
    from . import provider_credits
    harness = registry.live_harness(row) or row.get("harness") or ""
    credits = [r for r in provider_credits.listing(db) if r["harness"] == harness]
    own = [r for r in credits
           if (r.get("account") or "") == (row.get("session_account") or "")]
    return harness, own or [r for r in credits if r.get("state") != provider_credits.OK]


def agent_detail_json(db, name: str, *, cfg=None) -> dict | None:
    """`show --json` et `/api/v1/agents/<agent>` : un seul sérialiseur (#4164)."""
    row = agent_detail(db, name, cfg=cfg)
    if row is None:
        return None
    return dict(row, provider_credits=_agent_credits(db, row)[1])


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    from . import progress
    db = _open(cfg)
    try:
        row = agent_detail(db, args.agent, cfg=cfg)
        if row is None:
            print(USAGE_HINT % ("unknown agent: %s" % args.agent), file=sys.stderr)
            return 1
        from . import host_idle
        if args.json:
            print(json.dumps(agent_detail_json(db, args.agent, cfg=cfg), ensure_ascii=False, indent=2))
            return 0
        # #2200 : la pause opérateur se lit dans le résumé texte, pas seulement
        # dans le JSON — un `idle` avec bail vivant ne dit pas « ne reprendra
        # pas » et a fait router des lots vers un agent pausé.
        pause = progress.operator_pause_of(row)
        status = row.get("status")
        if pause and status not in ("stopped", "dead", "blocked"):
            status = "pause-next" if pause.get("after_turn") else "op-paused"
        if row.get("footprint"):
            print(host_idle.line(row["footprint"]))
        elif row.get("footprint_unavailable"):
            print("footprint: not checked (%s)" % row["footprint_unavailable"])
        print("agent    : %s" % row["name"])
        print("workspace: %s" % (row.get("chantier") or "—"))
        print("harness  : %-9s host: %-12s status: %s%s" % (
            row.get("harness") or "?", row.get("host") or "—", status,
            (" (%s)" % row["status_text"]) if row.get("status_text") else ""))
        # L242b/#1250 : le harnais DÉCLARÉ ne dit pas sous lequel la session
        # tourne (harnais emprunté) ; quand la session vivante en a un autre,
        # la ligne le dit — c'est la lecture qui a produit le cas #1024.
        live = registry.live_harness(row)
        if live and live != (row.get("harness") or ""):
            print("session harness: %s" % live)
        if row.get("stop_reason"):
            print("stopped  : %s" % row["stop_reason"])
        if pause:
            who = pause.get("by") or "?"
            if pause.get("for") and pause["for"] != who:
                who += " for %s" % pause["for"]
            print("paused   : operator pause since %s by %s — %s" % (
                _fmt_moment(pause.get("since_ts")), who,
                pause.get("reason") or "(no reason given)"))
            if pause.get("until_ts"):
                print("           deadline %s: the runner lifts it then (#3961)"
                      % agent_pause.until_label(pause["until_ts"]))
            else:
                print("           no deadline (--indefinite)")
            if pause.get("after_turn"):
                print("           this turn finishes, then no new turn starts")
            else:
                print("           no new turn starts until it is lifted "
                      "(ameesh resume %s --operator --reason <reason>)" % row["name"])
        # #3460 : un agent retiré n'est plus ni désigné, ni réclamé, ni joint
        if row.get("retired_ts"):
            print("retired  : since %s by %s — %s (ameesh agent restore %s)" % (
                _fmt_moment(row["retired_ts"]), row.get("retired_by") or "?",
                row.get("retired_reason") or "(no reason given)", row["name"]))
        # L37 (0030) : mode explicite ; un agent externe exige un responsable
        # (sauf retiré : il ne travaille plus)
        if row.get("mode") == "externe":
            print("mode     : externe (human session, cannot be woken)%s" % (
                "" if row.get("responsible") or row.get("retired_ts")
                else " — NO human responsible: mandatory for an external agent"))
        else:
            print("mode     : execute (driven by a runner, under lease)")
        # L39 (0033) : compte d'origine de la session, quand il est connu
        print("session  : %s%s" % (row.get("session_id") or "—",
                                   " (account %s)" % row["session_account"]
                                   if row.get("session_id") and row.get("session_account")
                                   else ""))
        # #4164 : l'état réel des crédits du compte fournisseur de l'agent
        from . import provider_credits
        credit_harness, shown = _agent_credits(db, row)
        if shown:
            for r in shown:
                print("credits  : %s %s" % (provider_credits.who(r["harness"], r.get("account")),
                                           provider_credits.describe(r)))
        elif credit_harness:
            print("credits  : %s ok (no credit exhaustion seen)" % credit_harness)
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
        # #3729 : le dernier signe de vie, comme la colonne ACTIVITY de `list`
        # (#3337). `last_seen` seul fait passer une session `externe` pour
        # absente alors qu'elle vient d'écrire ; l'hôte du dernier dépôt est
        # nommé quand il diffère de l'hôte déclaré.
        presence_ts, presence_source = registry.presence(row)
        activity = _fmt_age(time.time() - presence_ts)
        if presence_source == "wrote":
            activity = "wrote " + activity
            wrote_from = (row.get("last_mail_host") or "").strip()
            if wrote_from and wrote_from != (row.get("host") or "").strip():
                activity += " from " + wrote_from
        else:
            activity = "seen " + activity
        print("activity : %s" % activity)
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

def _note_arg(note: str | None) -> str:
    """#4023 : `--note -` de `work move`, `merged` et `milestone` se lit sur
    l'entrée standard (heredoc `<<'EOF'`), comme `work note <lot> -` (#3683)
    et `work add --body -` (#3744), jamais « - » tel quel ; une entrée vide
    ou réduite à « - » est refusée avant toute écriture."""
    if note != "-":
        return note or ""
    from . import cli
    text, error = cli._stdin_body("note")
    if error:
        raise work.WorkError(error)
    return text


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
    v = _vocabulary()
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
        print(('warning: deep analysis requested on the %s, but no auditor is registered to receive it') % v.noun, file=sys.stderr)


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
    v = _vocabulary()
    if not check:
        return
    if check.get("externe"):
        print(('external session: %s will not be woken by ameesh; the %s waits for %s (responsible)') % (check["assignee"], v.noun, check["responsible"]))
    if check.get("warning"):
        print("warning: %s" % check["warning"], file=sys.stderr)


#: sous-commandes `work` dont `--actor` a son propre sens (le délégant de
#: `delegate`) ou son propre défaut non vide (relevés, échéances)
_OWN_ACTOR = ("delegate", "expire-delegations", "sync-branches", "device-gates",
              "fingerprint")


def _work_actor(cfg: Config, db, args: argparse.Namespace) -> None:
    """L'acteur des commandes `work` (correctif du 2026-10-11) : `--actor`,
    sinon l'identité liée de la session, jamais vide (« inconnu », averti).
    Une correction d'état (`move --correct`) vérifie en plus qui corrige, un
    forçage (`add`/`assign --force`, #540) qui force."""
    v = _vocabulary()
    if args.work_command == "move" and getattr(args, "correct", None) is not None:
        args.actor = work.corrector(cfg, db, args.actor)
        return
    if args.work_command == "move" and getattr(args, "package_exclusion", False):
        # #4043 : le retrait d'un paquet est le geste du pilote (paquet-horaire)
        args.actor = work.corrector(cfg, db, args.actor, what="package exclusion")
        return
    if args.work_command in ("add", "assign") and (getattr(args, "force", None) or "").strip():
        # #540 : l'auteur d'un forçage est l'identité de la session
        args.actor = work.forcer(cfg, db, args.actor)
        return
    if args.work_command == "estimates" and getattr(args, "publish", False):
        # #3501 : la table de référence (L157b) est le passage de l'auditeur
        args.actor = work.publisher(cfg, db, args.actor)
        return
    if args.work_command == "milestone" and getattr(args, "kind", "") in ("frozen", "verdict"):
        # #2928 : une session liée ne pose un gel ou un verdict qu'en son nom.
        # Sans cette garde, `--actor <autre agent>` signait un avis au nom d'un
        # relecteur d'une autre famille ; le jalon restait sans reçu (#1465) et
        # la porte le comptait par le journal des tours.
        explicit = (args.actor or "").strip()
        args.actor = work.milestone_actor(cfg, db, args.actor, kind=args.kind)
        if not explicit and args.actor == work.UNKNOWN_ACTOR:
            print(('warning: unknown actor: no identity bound to this session (AGENT_MAIL_NAME, ameesh mail bind); the %s journal will say "%s"; pass --actor human:<id>') % (v.noun, work.UNKNOWN_ACTOR),
                  file=sys.stderr)
        return
    if args.work_command in (*_OWN_ACTOR, "set-app", "set-type", "set-priority",
                            "issue-ref", "backfill-app") or getattr(args, "actor", None) is None:
        return
    args.actor, warning = work.resolve_actor(cfg, db, args.actor)
    if warning:
        print("warning: %s" % warning, file=sys.stderr)


def _wake_deadline(args: argparse.Namespace) -> float | None:
    """#2466 : l'échéance d'un `work wake` / `work note --wake-at`."""
    at = getattr(args, "wake_at", None)
    delay = getattr(args, "wake_in", None)
    if not at and not delay:
        return None
    from . import watch
    try:
        return watch.parse_wake_deadline(delay, at)
    except watch.WatchError as exc:
        raise work.WorkError(str(exc)) from exc


def work_rows(db, *, state: str | None = None, assignee: str | None = None,
              limit: int | None = None, threshold: float | None = None,
              cfg=None, freeze: bool = False, repo: str | None = None,
              target: str | None = None) -> list:
    """The rows of `ameesh work list --json` (also `GET /api/v1/lots`, L84).

    `cfg` : le contexte de classe de revue (#1822), pour que la ligne d'un lot
    léger attende la fusion, pas un verdict.

    `freeze` ajoute à chaque ligne le gel effectif dérivé (#1756), en un seul
    balayage de jalons : le composeur du paquet horaire lit ce champ au lieu
    de JOINdre lui-même le dernier jalon `frozen`. `repo` (dépôt git local) et
    `target` (défaut : la cible du lot, sinon `origin/main`) permettent en plus
    de porter un avis sur un gel republié quand le delta du lot est
    mécaniquement constant (#1905) ; sans `repo`, le champ est inchangé."""
    from . import merge_queue, plan, plan_git, stagnation
    if threshold is None:
        threshold = stagnation.DEFAULT_THRESHOLD_S

    def lire(db):
        rows = work.list_items(db, state=state, assignee=assignee, limit=limit)
        plan.annotate(db, rows, threshold=threshold, cfg=cfg)
        # Les délais viennent d'un seul balayage, restreint aux lots
        # affichés (jamais une requête par ligne) ; le JSON les porte
        # aussi, pour la frise (L24). Le filtre porte sur les lignes
        # rendues, pas sur une fenêtre globale : un lot ancien garde sa
        # frise (B3).
        delays = {row["work_item_id"]: row for row in
                  work.delays(db, ids=[row["id"] for row in rows])}
        for row in rows:
            row["delays"] = delays.get(row["id"])
            # #4349 : le nom générique à côté de l'orthographe persistée v1.
            row["lifecycle"] = work_lifecycle.lifecycle_name(row["state"])
        if freeze:
            marks: dict = {}
            for mark in storage.of(db).progress.lot_milestones(
                    [row["id"] for row in rows]):
                marks.setdefault(int(mark["work_item_id"]), []).append(mark)
            for row in rows:
                row["freeze"] = plan_git.freeze_candidate(
                    marks.get(int(row["id"]), []), repo=repo,
                    target=target or row.get("branch_target") or plan_git.DEFAULT_TARGET,
                    waived=merge_queue.is_waived(row))
        return rows

    # L61 : lectures regroupées — deux allers-retours en tout
    return db_mod.batched(db, lire)


def work_detail(cfg: Config, db, lot_id: int, repo: str | None = None,
                target: str | None = None) -> dict | None:
    """The object of `ameesh work show --json` (also `GET /api/v1/lots/{id}`)."""
    from . import canon as canon_mod, merge_queue, plan, stagnation

    def lire(db):
        item = work.get(db, lot_id)
        if item is None:
            return None
        # #4349 : le nom générique du cycle de vie, à côté de l'état v1.
        item["lifecycle"] = work_lifecycle.lifecycle_name(item["state"])
        item["events"] = work.events(db, lot_id)
        item["milestones"] = work.milestones(db, lot_id)
        # #2988 : la tête relue écrite à la création (`reviewed head:`), ou
        # `absent` pour un lot né d'une revue sans tête.
        from . import intake_neighbors
        item["reviewed"] = intake_neighbors.reviewed_head_of(item["events"])
        creation_seen = any(str(event.get("note") or "").startswith("created")
                            for event in item["events"])
        if (item["reviewed"] is None and not creation_seen
                and intake_neighbors.review_born(
                    title=item.get("title") or "", source=item.get("source") or "",
                    issue_ref=item.get("issue_ref") or "")):
            # journal plus long que la fenêtre de `work show` : la création
            # seule porte la marque, on la relit sans tronquer
            try:
                full = work.events(db, lot_id, limit=None)
            except Exception:  # noqa: BLE001 - l'affichage ne casse pas le lot
                full = []
            item["reviewed"] = intake_neighbors.reviewed_head_of(full)
        # #3183 : le plancher d'avis du pilote et sa source.
        from . import review_floor
        try:
            item["reviews_floor"] = review_floor.of_lot(db, lot_id, cfg=cfg)
        except Exception as exc:  # noqa: BLE001 - l'affichage ne casse pas le lot
            item["reviews_floor"] = {"floor": None, "error": str(exc)[:200]}
        from . import plan_git
        item["freeze"] = plan_git.freeze_candidate(
            item["milestones"], repo=repo,
            target=target or item.get("branch_target") or plan_git.DEFAULT_TARGET,
            waived=merge_queue.is_waived(item))
        from .modules import registry as module_registry
        item["module_milestones"] = module_registry.milestones(db, lot_id)
        item["delays"] = work.timeline(db, lot_id)
        # L539 : la classe de revue du lot (lecture seule ; ne lève jamais, un
        # doute rend `sensitive` et le dit) — résolue avant l'annotation pour
        # que #1822 la lise sans la relire.
        from . import lot_class
        try:
            item["review_class"] = lot_class.of_lot(db, lot_id, cfg=cfg, item=item)
        except Exception as exc:  # noqa: BLE001 - l'affichage ne casse pas le lot
            item["review_class"] = {"lot": lot_id, "class": lot_class.DOUBT,
                                    "why": "unreadable (%s)" % exc, "files": []}
        plan.annotate(db, [item], cfg=cfg,
                      review_class={lot_id: stagnation.class_value(item["review_class"])})
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
        # #2443 : les paquets de composition enregistrés, lus en base — jamais
        # le manifeste. Une base d'avant 0465 n'empêche pas la fiche de sortir.
        from . import packet_link
        try:
            item["packets"] = packet_link.for_item(db, lot_id)
        except Exception as exc:  # noqa: BLE001 - l'affichage ne casse pas le lot
            item["packets"], item["packets_error"] = [], str(exc)
        # L160e : place du lot dans la file de fusion de sa cible
        try:
            item["queue"] = next((r for r in storage.of(db).work.queue()
                                  if int(r["work_item_id"]) == lot_id), None)
        except Exception:  # noqa: BLE001 - e.g. a database without migration 0058
            item["queue"] = None
        # L157c: actual time by phase and merged diff size, once delivered
        from . import lot_measures
        item["measure"] = lot_measures.view(storage.of(db).work.measure(lot_id))
        # L317c: the auditor's automatic deep analysis and its measured delay
        from . import deep_analysis
        item["deep_analysis"] = deep_analysis.status(db, lot_id, cfg=cfg)
        # #3070 (canon §12): a blocked lot waiting for a device session
        from . import device_gates
        item["device_gate"] = device_gates.of_item(
            db, item, canons=work.configured_canons(cfg)) \
            if item.get("state") == "blocked" else None
        return item

    # L61/L521: the detail's independent reads travel together (one query per
    # section before: 23 round trips, ~7 s from a laptop), and the canon read
    # by the sub-calls is read once per command, not once per dry pass.
    with canon_mod.configured_cache():
        return db_mod.batched(db, lire)


def cmd_work(cfg: Config, args: argparse.Namespace) -> int:
    v = _vocabulary()
    from . import plan, plan_cli, prevalidation, stagnation  # plan de travail (L29)
    db = _open(cfg)
    try:
        _work_actor(cfg, db, args)
        if args.work_command in prevalidation.COMMANDS:   # #1770 prévalidation au gel
            return prevalidation.run(cfg, db, args)
        if args.work_command in plan_cli.COMMANDS:
            return plan_cli.run(db, args)
        if args.work_command == "backlog":   # file d'amélioration (L119, 0037)
            from . import backlog
            return backlog.run(db, args, cfg)
        if args.work_command in ("wip", "return"):   # limite d'en-cours (§4)
            from . import wip
            return wip.run(db, args, cfg)
        if args.work_command == "unrecorded":   # demandes d'humains sans lot (L130)
            from . import unrecorded
            return unrecorded.run(cfg, db, args)
        if args.work_command == "resolve":   # prepared conflict resolutions (L160d)
            from . import resolutions
            return resolutions.run(cfg, db, args)
        if args.work_command == "dependencies":
            from . import work_dependencies
            return work_dependencies.run(db, args)
        if args.work_command == "links":     # #4167 : liens typés entre lots
            from . import work_dependencies
            return work_dependencies.run_links(db, args)
        if args.work_command == "footprint":   # #4173 : empreinte de lot (#3185)
            from . import work_footprints
            return work_footprints.run(cfg, db, args)
        if args.work_command == "landing":   # #1945 : atterrissage planifié sur un véhicule
            from . import work_dependencies
            return work_dependencies.run_landing(db, args)
        if args.work_command == "queue":   # merge queue per target (L160e)
            from . import merge_queue
            return merge_queue.run(cfg, db, args)
        if args.work_command == "land":   # merge of the head of the queue (L160e2)
            from . import land
            return land.run(cfg, db, args)
        if args.work_command == "merge-pr":   # REL-11 (#1003) : fusion hors API gh
            from . import merge_pr
            return merge_pr.run(cfg, db, args)
        if args.work_command == "fire":   # #1374 : le feu du pilote au gel
            from . import fire
            return fire.run(cfg, db, args)
        if args.work_command == "coherence":     # #4172 : passe de cohérence
            from . import backlog_coherence
            reports = backlog_coherence.tick(
                cfg, db, apply=args.apply, project=args.project,
                repo=args.repo, actor=args.actor)
            if args.json:
                print(json.dumps({"schema": backlog_coherence.SCHEMA,
                                  "projects": reports}, ensure_ascii=False,
                                 indent=2, default=str))
                return 0
            if not reports:
                print(('no open %s') % v.noun)
                return 0
            for report in reports:
                print(backlog_coherence.format_report(report))
            return 1 if any(report.get("error") for report in reports) else 0
        if args.work_command == "neighbors":
            # #2988 : lecture seule, AVANT la création — les lots voisins du
            # projet (même fichier, titre proche, ligne déjà changée depuis la
            # tête relue) et l'état du contrôle git.
            from . import intake_neighbors
            try:
                head = intake_neighbors.normalize_head(args.reviewed_head)
            except intake_neighbors.IntakeNeighborsError as exc:
                raise work.WorkError(str(exc)) from None
            found = intake_neighbors.neighbors(
                db, args.app or "", title=args.title, body=args.body or "",
                reviewed_head=head, cfg=cfg, repo=args.repo)
            if args.json:
                print(json.dumps(found, ensure_ascii=False, indent=2, default=str))
                return 0
            if found.get("git") == "non vérifié":
                print("git non vérifié : aucun clone local du dépôt du projet",
                      file=sys.stderr)
            if not found.get("neighbors"):
                print("no neighbor found (project %s, %d days)"
                      % (args.app or "—", found.get("since_days")))
                return 0
            for neighbor in found["neighbors"]:
                print(intake_neighbors.format_neighbor(neighbor))
            return 0
        if args.work_command == "add":
            # L37 (0030, règle 2) : attribution gardée — un assigné non
            # réveillable est refusé AVANT la création (WorkError, code 1).
            # L157 : durée estimée, obligatoire pour un agent (refus AVANT la
            # création), avertissement pour un humain
            from . import estimates
            from . import identity, intake_neighbors, work_projects
            binding = identity.resolve_binding(cfg, db)
            creator = binding.name if binding.ok else args.actor
            if args.new_project and args.app is None:
                raise work.WorkError("--new-project goes with --app <project>")
            if args.body == "-":
                # #3744 : comme `work note <lot> -` (#3683), le corps vient de
                # l'entrée standard (heredoc `<<'EOF'`), jamais « - » tel quel ;
                # une entrée vide ou réduite à « - » est refusée avant création.
                from . import cli
                args.body, error = cli._stdin_body("body")
                if error:
                    raise work.WorkError(error)
            args.app = work_projects.creation_project(cfg, db, creator, args.app,
                                                      new_project=args.new_project)
            # #506: read before the lot exists (it makes its project known)
            introduced = (work_projects.introduction(cfg, db, args.app, creator)
                          if args.new_project and args.app else "")
            minutes = estimates.parse(args.estimate) if args.estimate else None
            warn_estimate = estimates.check(db, args.actor, minutes)
            check = (work.check_assignee(db, args.assignee, externe=args.externe, cfg=cfg)
                     if args.assignee else None)
            # #2988 : tête relue, détection d'une revue et voisins calculés
            # AVANT `work.add` — la recherche git ne doit pas tourner dans
            # `work._create`, appelé aussi par incident.py, assignments.py et
            # review_lot.py. Le segment est préfixé à la note de création.
            try:
                segment, infos = intake_neighbors.creation_note(
                    db, cfg, app=args.app or "", title=args.title,
                    body=args.body or "", source=args.source or "",
                    issue_ref=args.issue_ref or "",
                    reviewed_head=args.reviewed_head)
            except intake_neighbors.IntakeNeighborsError as exc:
                raise work.WorkError(str(exc)) from None
            note = "; ".join(part for part in (segment, introduced) if part)
            # #4173 : l'empreinte d'intake (chemins cités), écrite par la
            # création même ; jamais d'échec de la création pour l'estimer.
            from . import work_footprints
            try:
                footprint = work_footprints.estimate_lot(cfg, {
                    "app": args.app or "", "title": args.title, "body": args.body or "",
                    "branch_target": args.target or ""})
            except Exception as exc:  # noqa: BLE001 - la création n'attend pas l'estimation
                footprint = []
                print("warning: footprint not estimated: %s" % exc, file=sys.stderr)
            add_kwargs = dict(
                title=args.title, type=args.type, source=args.source, app=args.app,
                body=args.body or "", issue_ref=args.issue_ref, workstream=args.workstream,
                budget_usd=args.budget, actor=args.actor,
                package=args.package, externe=args.externe, cfg=cfg,
                external_executor=args.external_executor,
                branch=args.branch, branch_target=args.target, priority=args.priority,
                estimate=minutes, estimate_source=args.estimate_source, size=args.size,
                note=note,
                force=args.force,
                parent_lot=args.parent_lot, footprint=footprint)
            try:
                item = work.add(db, assignee=args.assignee, **add_kwargs)
            except storage.FootprintOverlap as exc:
                # #4173 : attribué, le lot chevaucherait un lot en cours du même
                # dépôt — il est créé libre et attend la fusion (prise L165).
                # #4186 : le lot naît SANS assigné, donc `--externe` (qui force
                # l'attribution à un agent externe) n'a plus d'objet : le garder
                # ferait échouer la seconde création (« --externe without
                # assignee ») et le lot ne naîtrait pas du tout.
                add_kwargs["externe"] = False
                item = work.add(db, assignee=None, **add_kwargs)
                # le lot est libre : l'attribution de la tentative refusée ne
                # doit plus s'afficher (`_print_assignment` plus bas).
                check = None
                print("warning: created unassigned: %s"
                      % work_footprints.refusal(item["id"], exc.overlaps), file=sys.stderr)
            print(('%s #%d created in %s: %s%s%s%s') % (v.noun, item["id"], item["state"], item["title"], " (plan: %s)" % item["package_id"] if item.get("package_id") else "", " (priority %s)" % work.PRIORITY_LABELS.get(item.get("priority"))
                if item.get("priority") else "", " — estimated %s" % estimates.label(minutes) if minutes else ""))
            if item.get("external_executor"):
                print("executor : %s (external, reserved; not assigned — L165 does not "
                      "take it)" % item["external_executor"])
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
            # #2988 : la tête relue manquante, les voisins trouvés et l'état du
            # contrôle git sont dits au rédacteur (stderr), jamais refusés.
            if infos.get("reviewed_warning"):
                print("warning: %s" % infos["reviewed_warning"], file=sys.stderr)
            if infos.get("neighbors_error"):
                print("warning: voisins non recherchés: %s" % infos["neighbors_error"],
                      file=sys.stderr)
            if infos.get("neighbors_git") == "non vérifié":
                print("warning: git non vérifié (aucun clone local du dépôt du projet)",
                      file=sys.stderr)
            for neighbor in infos.get("neighbors") or ():
                print("neighbor: %s" % intake_neighbors.format_neighbor(neighbor),
                      file=sys.stderr)
            if footprint:
                print("footprint: %s" % ", ".join(footprint))
            return 0
        if args.work_command == "fingerprint":
            # #3885 : primitive idempotente de la règle §16 — un lot par
            # empreinte nouvelle, les doublons notés, P1 si un utilisateur est
            # touché, récidive après clôture.
            from . import fingerprints
            if getattr(args, "fingerprint_command", None) != "open":
                print("ameesh work fingerprint: `open` is the only command "
                      "(ameesh work fingerprint open --app P --key K --title T)",
                      file=sys.stderr)
                return 2
            result = fingerprints.open_lot(
                db, app=args.app, key=args.key, title=args.title,
                users=args.users, count=args.count, route=args.route,
                code=args.code, revision=args.revision,
                first_seen=args.first_seen, last_seen=args.last_seen,
                actor=args.actor)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if result["created"]:
                print(('%s #%d opened for %s (priority %s%s)') % (v.noun, result["lot"], result["source"], result["priority"], ", récidive de #%d" % result["recurrence_of"]
                         if result.get("recurrence_of") else ""))
            else:
                print(('%s #%d already open for %s: occurrence noted%s') % (v.noun, result["lot"], result["source"], " (P1: a user is touched)" if result.get("escalated")
                         else ""))
            return 0
        if args.work_command == "assign":
            out = work.assign(db, args.id, args.agent, externe=args.externe,
                              actor=args.actor, cfg=cfg, branch=args.branch,
                              branch_target=args.target, force=args.force,
                              claim=args.claim, if_holder=args.if_holder,
                              wip_exception=args.wip_exception)
            if out.get("queue_warning"):
                print(('%s #%d not assigned to %s (before: %s)') % (v.noun, out["item"]["id"], args.agent, out["previous"] or "nobody"))
                if args.claim:
                    # #1787 N2 : un claim qui retombe dans la file d'équipe
                    # (#540) n'a pas pris : refus lisible, code 1.
                    print("claim refused: %s" % out["queue_warning"], file=sys.stderr)
                    return 1
                print("warning: %s" % out["queue_warning"], file=sys.stderr)
            elif out.get("noop"):
                # #1787 N3 : le porteur qui re-claim son propre lot : no-op.
                print(('%s #%d already assigned to %s: claim is a no-op, nothing changed') % (v.noun, out["item"]["id"], out["item"]["assignee"]))
            elif out["previous"] == out["item"]["assignee"]:
                if args.wip_exception:
                    print(('%s #%d: WIP exception recorded for %s%s') % (v.noun, out["item"]["id"], out["item"]["assignee"], "; branch %s" % out["item"]["branch"] if args.branch else ""))
                else:
                    print(('%s #%d: branch %s (assigned to %s)') % (v.noun, out["item"]["id"], out["item"].get("branch"), out["item"]["assignee"]))
            else:
                print(('%s #%d assigned to %s (before: %s)%s%s') % (v.noun, out["item"]["id"], out["item"]["assignee"], out["previous"] or "nobody", "; branch %s" % out["item"]["branch"] if args.branch else "", "; WIP exception (§4)" if args.wip_exception else ""))
            _print_assignment(out["check"])
            return 0
        if args.work_command == "external-executor":
            # #3447 : réservation déclarative à un exécutant externe (bureau,
            # owner), jamais une attribution.
            if args.clear and args.agent:
                raise work.WorkError("--clear goes without an executor name")
            if not args.clear and not args.agent:
                raise work.WorkError("name the external executor, or pass --clear")
            out = work.set_external_executor(db, args.id, args.agent,
                                             actor=args.actor, cfg=cfg,
                                             clear=args.clear)
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
                return 0
            if args.clear:
                print(('%s #%d: external executor cleared (was %s); the %s is back in the ordinary queue') % (v.noun, out["item"]["id"], out["previous"] or "nobody", v.noun))
            else:
                print(('%s #%d: reserved to %s (external executor; not assigned — L165 does not take it)') % (v.noun, out["item"]["id"], out["executor"]))
            return 0
        if args.work_command == "branch":
            # #2102 : détachement explicite, ou (ré)association explicite
            if args.detach:
                if args.target:
                    raise work.WorkError("--target goes with a branch to set, not --detach")
                before = work.get(db, args.id) or {}
                item = work.detach_branch(db, args.id, args.branch, reason=args.reason,
                                          actor=args.actor)
                name = args.branch or before.get("branch")
                print(('%s #%d: branch %s detached (state %s, assignee %s kept); the merge sweep no longer associates it') % (v.noun, item["id"], name, item["state"], item.get("assignee") or "nobody"))
                return 0
            if not args.branch:
                raise work.WorkError("name the branch to set, or pass --detach")
            again = work.is_detached(storage.of(db), args.id, args.branch)
            cleared = args.target is not None and not args.target.strip()
            item = work.set_branch(db, args.id, args.branch, args.target, actor=args.actor,
                                   source="explicit association%s" % (
                                       " after its detachment" if again else ""),
                                   clear_target=cleared)
            print(('%s #%d: branch %s%s%s') % (v.noun, item["id"], item.get("branch"), " to %s" % (
                    item["branch_target"]) if item.get("branch_target") else "", " (target cleared)" if cleared else ""))
            return 0
        if args.work_command == "delegate":
            # L40 (0030, point 5) : délégation à échéance, gardée comme `assign`
            out = work.delegate(db, args.id, args.agent, within=args.within,
                                actor=args.actor, cfg=cfg)
            item = out["item"]
            print(('%s #%d delegated to %s by %s, due in %s (%s)') % (v.noun, item["id"], item["assignee"], item["delegated_by"], work.span(float(item["due_ts"]) - float(item["delegated_ts"])), _fmt_moment(item.get("due_ts"))))
            if out.get("replaced"):
                print("previous delegation replaced (%s, delegated by %s)" % (
                    out["replaced"]["delegate"], out["replaced"]["delegated_by"]))
            if out.get("message_id"):
                print("event #%d filed for %s" % (out["message_id"], item["assignee"]))
            if out.get("warning"):
                print("warning: %s" % out["warning"], file=sys.stderr)
            return 0
        if args.work_command == "device-gates":
            # #3070 (canon §12) : séances sur un appareil indisponible
            from . import device_gates
            canons = work.configured_canons(cfg)
            if args.apply:
                rows = device_gates.lift(db, canons=canons,
                                         actor=args.actor or device_gates.ACTOR)
            else:
                rows = device_gates.candidates(db, canons=canons)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print(("no %s lifted" % v.noun) if args.apply
                      else ("no blocked %s declares a device session" % v.noun))
                return 0
            for row in rows:
                if args.apply:
                    print(('%s #%d → %s (%s, verification %s #%d)') % (v.noun, row["lot"], row["state"], device_gates.TRACE, v.noun, row["verify"]))
                else:
                    print(('%s #%d: %s') % (v.noun, row["lot"], device_gates.label(row)))
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
                    "annulee": "delegation cancelled (%s closed or reassigned)" % v.noun,
                }.get(row["outcome"], row["outcome"])
                print("%s%s #%s: %s" % ("[dry-run] " if row.get("dry_run") else "",
                                         v.noun, row["work_item_id"], issue))
            return 0
        if args.work_command == "list":
            rows = work_rows(db, state=args.state, assignee=args.assignee, limit=args.limit,
                             threshold=stagnation.stale_after(args.stale_after),
                             cfg=cfg, freeze=getattr(args, "freeze", False),
                             repo=getattr(args, "git_dir", None),
                             target=getattr(args, "freeze_target", None))
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print(('no %s') % v.plural)
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
                if row.get("external_executor"):
                    notes.append("reserved to %s (external executor)"
                                 % row["external_executor"])
                if notes:
                    print("      %s" % " · ".join(notes))
            return 0
        if args.work_command == "show":
            from . import estimates, lot_measures, packet_link, resolutions
            item = work_detail(cfg, db, args.id,
                               repo=getattr(args, "git_dir", None),
                               target=getattr(args, "freeze_target", None))
            if item is None:
                print(USAGE_HINT % ("%s %s not found" % (v.noun, args.id)),
                      file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
                return 0
            print(('%-9s: #%d %s') % (v.noun, item["id"], item["title"]))
            print("type     : %-10s state: %-14s lifecycle: %-14s assignee: %s" % (
                item["type"], item["state"], item["lifecycle"],
                item.get("assignee") or "—"))
            if item.get("external_executor"):
                print("executor : %s (external, reserved; not assignable)" % (
                    item["external_executor"]))
            print("app      : %-10s source: %-12s workstream: %s" % (
                item.get("app") or "—", item.get("source") or "—", item.get("workstream") or "—"))
            print("issue    : %-12s loops: %-3s budget: %s" % (
                item.get("issue_ref") or "—", item.get("loops"), _budget(item)))
            from . import intake_neighbors
            reviewed = intake_neighbors.format_reviewed_line(item.get("reviewed"))
            if reviewed:
                print("reviewed : %s" % reviewed)
            if item.get("reviewer") == work.REVIEWER_NONE:
                print("reviewer : — (verdict not required, #1979)")
            else:
                from .work_reviewers import designated
                if designated(item):
                    print("reviewer : %s (designated for the review, #2076)" %
                          ", ".join(designated(item)))
            floor = item.get("reviews_floor") or {}
            if floor.get("floor"):
                print("floor    : %d review(s) of distinct families (by %s, %s; #3183)" % (
                    floor["floor"], floor.get("actor") or "?",
                    _fmt_moment(floor.get("at_ts"))))
            elif floor.get("error"):
                print("floor    : unreadable (%s): the gate requires two reviews"
                      % floor["error"])
            if item.get("priority"):
                print("priority : %s (%s)" % (
                    item["priority"], work.PRIORITY_LABELS.get(item["priority"], "?")))
            if item.get("package_id") or item.get("epic"):
                print("plan     : card %-12s epic: %s" % (
                    item.get("package_id") or "—", item.get("epic") or "—"))
            for member in item.get("packets") or []:
                print("package  : %s" % packet_link.describe(member))
            if item.get("packets_error"):
                print("package  : unavailable (%s)" % item["packets_error"])
            if any(item["planned"].get(k) for k in ("start", "end", "delivery")):
                print("planned  : start %s · end %s · delivery %s%s" % (
                    item["planned"]["start"] or "—", item["planned"]["end"] or "—",
                    item["planned"]["delivery"] or "—",
                    " (source: %s)" % item["planned"]["source"]
                    if item["planned"].get("source") else ""))
            if item.get("wake_ts"):
                print("wake     : %s (scheduled wake)"
                      % _fmt_moment(float(item["wake_ts"])))
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
                    work.close_how(item.get("close_reason"), item.get("superseded_by")),
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
            migration = work.migration_line(item, cfg)
            if migration:
                print("migration: %s" % migration)
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
            if item.get("device_gate"):
                from . import device_gates
                print("device   : %s" % device_gates.label(item["device_gate"]))
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
            # #2786 : un gel qui ne nomme pas un commit hexadécimal (HEAD,
            # une branche, un sha vide) n'est jamais une preuve de livraison :
            # le balayage le tient pour un doute, jamais pour une fusion. Le
            # dire une fois, sans noyer la frise.
            from . import plan_git as _plan_git
            last_freeze = _plan_git.latest_freeze(item["milestones"])
            if last_freeze is not None and not _plan_git.commit_id(
                    str(last_freeze.get("sha") or "")):
                print("gel      : jalon non rattaché à un commit (%r) — le balayage "
                      "des fusions ne le résout pas (doute, jamais fusion) ; regeler "
                      "sur un SHA complet (`git rev-parse <ref>`)"
                      % (str(last_freeze.get("sha") or "") or "sans sha",))
            # #2674 : les verdicts écrits avant la règle de la tête relue ne
            # comptent pas à la porte ; le dire une fois, sans noyer la frise.
            unattached = [m for m in item["milestones"]
                          if m.get("kind") == "verdict"
                          and not str(m.get("sha") or "").strip()]
            if unattached:
                print("verdicts : avis non rattaché à une tête%s (anciens verdicts sans "
                      "sha : ils ne comptent pas à la porte de fusion ; redonner l'avis "
                      "sur la tête gelée)"
                      % (" ×%d" % len(unattached) if len(unattached) > 1 else ""))
            for event in item["events"]:
                print("  %s  %-14s %-12s %s" % (
                    _fmt_moment(event.get("created_ts")), event["state"],
                    (event.get("actor") or "—")[:12], event["note"]))
            return 0
        if args.work_command == "migration":
            from . import migration_reservations
            if (args.slot or args.list or args.landed or args.target_branch
                    or args.target_sha or args.reservation_id):
                return migration_reservations.run(db, args, cfg)
            # L122 : numéro de migration réservé au lot, avant d'écrire le fichier
            item = work.reserve_migration(db, args.id, args.number, release=args.release,
                                          repo=args.repo, actor=args.actor, cfg=cfg,
                                          skip_branches=tuple(args.skip_branch or ()),
                                          repository=getattr(args, "repository", None))
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
            elif not item.get("migration"):
                print(('%s #%d: no migration reserved') % (v.noun, item["id"]))
            elif len(item.get("migrations") or ()) <= 1:
                print(('%s #%d: migration %s reserved in %s — file %s') % (v.noun, item["id"], item["migration_label"], item["migration_repo"], item.get("migration_file") or "<migrations>/%s_<name>.sql"
                    % item["migration_label"]))
            else:
                rows = item["migrations"]
                print(('%s #%d: migrations %s reserved in %s') % (v.noun, item["id"], ", ".join(row["label"] for row in rows), ", ".join(sorted({row["repo"] for row in rows}))))
                for row in rows:
                    print("  %s — file %s" % (
                        row["label"], row.get("file")
                        or "<migrations>/%s_<name>.sql" % row["label"]))
            return 0
        if args.work_command == "promote":
            item = work.promote(db, args.id, note=_note_arg(args.note), actor=args.actor)
            print("lot #%d → %s (%s)" % (item["id"], item["state"],
                                        item.get("legacy_state") or "promoted"))
            return 0
        if args.work_command == "move":
            note = _note_arg(args.note)
            if args.device:
                # #3070 : la séance attendue sur un appareil, déclarée en note
                from . import device_gates
                if args.state != "blocked":
                    print(('ameesh work move: --device declares the device session a blocked %s waits for: ameesh work move %s blocked --device <device>') % (v.noun, args.id), file=sys.stderr)
                    return 2
                note = device_gates.session_note(args.device, note)
            item = work.move(db, args.id, args.state, note=note, actor=args.actor,
                             correct=args.correct, reviewer=args.reviewer,
                             package_exclusion=args.package_exclusion)
            print(('%s #%d → %s (loops %s)%s%s') % (v.noun, item["id"], item["state"], item["loops"], " — reviewer %s" % item["reviewer"] if item.get("reviewer") else "", " — correction recorded in the log, by %s" % args.actor
                if args.correct is not None else ""))
            # #800 : la désignation est conservée ; l'avertissement de famille
            # est indicatif (canon §2).
            if item.get("reviewer_warning"):
                print("warning: %s" % item["reviewer_warning"], file=sys.stderr)
            # #2478 : la désignation est écrite ; un courrier non remis est dit.
            if item.get("reviewer_request_warning"):
                print("warning: %s" % item["reviewer_request_warning"], file=sys.stderr)
            return 0
        if args.work_command == "reviewer":
            # #2076 : ajouter ou retirer un relecteur d'un lot DÉJÀ en qa,
            # sans aller-retour build/qa ; c'est la commande que l'alerte
            # `stale_lot` recommande à l'orchestrateur du projet.
            # #1979 : `--remove` retire la désignation, `--no-verdict` marque
            # « verdict non requis » (résolution de composition mécanique,
            # canon §11 bis/#87) : l'attente passe à la fusion, sans rappel.
            if args.no_verdict and (args.agent or args.remove):
                print("ameesh work reviewer: AGENT, --remove and --no-verdict are "
                      "mutually exclusive", file=sys.stderr)
                return 2
            if args.floor is not None and (args.remove or args.no_verdict):
                print("ameesh work reviewer: --floor goes alone or with AGENT, not with "
                      "--remove or --no-verdict", file=sys.stderr)
                return 2
            if args.floor is not None:
                # #3183 : le plancher d'avis du pilote, posé avant la désignation
                # (il vaut dans tout état ouvert, la désignation exige qa).
                from . import review_floor
                # #3557 : avec un AGENT, la désignation manuelle est le geste
                # du pilote ; sans lui, le plancher complète la désignation du
                # dernier gel par la sélection déterministe.
                floor = review_floor.set_floor(
                    cfg, db, args.id, args.floor, reason=args.reason,
                    actor=args.actor, complete=not bool(args.agent))
                if floor["changed"]:
                    print(('%s #%d: review floor %d (was %d, by %s)') % (v.noun, floor["lot"], floor["floor"], floor["previous"], floor["actor"]))
                else:
                    print(('%s #%d: review floor already %d: nothing written') % (v.noun, floor["lot"], floor["floor"]))
                completion = floor.get("designation")
                if completion:
                    state = completion.get("state")
                    if state == "designated":
                        names = ", ".join(
                            "%s (%s)" % (r.get("name"), r.get("family"))
                            for r in completion.get("reviewers") or ())
                        print("designation: %s%s" % (
                            "completed — " if completion.get("completed") else "",
                            names))
                    elif state in ("unavailable", "waiting", "stale", "unfrozen",
                                   "none", "manual"):
                        print("designation: %s — %s" % (
                            state, completion.get("why") or ""))
                    else:
                        print("designation: %s" % state)
                if not args.agent:
                    return 0
            if not args.agent and not (args.remove or args.no_verdict):
                print("ameesh work reviewer: give AGENT, --remove, --no-verdict or --floor",
                      file=sys.stderr)
                return 2
            if args.remove and args.agent:
                item = work.designate_reviewer(cfg, db, args.id, args.agent,
                                               actor=args.actor, remove=True)
                if item.get("reviewer_changed") is False:
                    # #2084/N2 : dire le non-effet, sans écrire un faux
                    # « removed » au journal.
                    print(('%s #%d: reviewer %s was not designated: nothing removed (state %s, by %s)') % (v.noun, item["id"], args.agent, item["state"], args.actor or "the bound identity"))
                else:
                    print(('%s #%d: reviewer %s removed (state %s, by %s)') % (v.noun, item["id"], args.agent, item["state"], args.actor or "the bound identity"))
                # #4779 : les demandes encore non remises du siège sont soldées
                # sans tour ; le dire, même quand le siège n'était déjà plus là.
                _settled = item.get("reviewer_settled") or []
                if _settled:
                    print(('%s #%d: %d pending review request(s) settled without a turn: %s') % (v.noun, item["id"], len(_settled), ", ".join("#%d" % int(i) for i in _settled)))
            elif args.remove:
                item = work.clear_reviewer(cfg, db, args.id, actor=args.actor)
                if item.get("reviewer_changed") is False:
                    print(('%s #%d: no reviewer designation to remove (state %s, by %s)') % (v.noun, item["id"], item["state"], args.actor or "the bound identity"))
                else:
                    print(('%s #%d: reviewer designation removed (state %s, by %s)') % (v.noun, item["id"], item["state"], args.actor or "the bound identity"))
                _settled = item.get("reviewer_settled") or []
                if _settled:
                    print(('%s #%d: %d pending review request(s) settled without a turn: %s') % (v.noun, item["id"], len(_settled), ", ".join("#%d" % int(i) for i in _settled)))
            elif args.no_verdict:
                item = work.waive_review(cfg, db, args.id, actor=args.actor)
                print(('%s #%d: verdict not required (state %s, by %s)') % (v.noun, item["id"], item["state"], args.actor or "the bound identity"))
            else:
                item = work.designate_reviewer(cfg, db, args.id, args.agent,
                                               actor=args.actor)
                print(('%s #%d: reviewer %s (state %s, designated by %s)') % (v.noun, item["id"], item["reviewer"], item["state"], args.actor or "the bound identity"))
                # #800 : la désignation est conservée ; l'avertissement de
                # famille est indicatif (canon §2).
                if item.get("reviewer_warning"):
                    print("warning: %s" % item["reviewer_warning"], file=sys.stderr)
                # #2478 : la désignation est écrite ; un courrier non remis est dit.
                if item.get("reviewer_request_warning"):
                    print("warning: %s" % item["reviewer_request_warning"], file=sys.stderr)
            return 0
        if args.work_command == "merged":
            if not (args.sha or args.pr):
                print("ameesh work merged: --sha S or --pr owner/repo#N", file=sys.stderr)
                return 2

            def _declare_constat(constat: dict) -> None:
                # #1400 : le constat (lot/app/titre + sujet du commit, et la
                # forme de la preuve) est montré AVANT l'écriture ; l'état du
                # lot ne change qu'après.
                v = _vocabulary()
                print(('declaring merge: %s #%d (%s: %s)') % (v.noun, constat["id"], constat["app"] or "?", constat["title"]))
                print("commit %s (%s): %s"
                      % (constat["sha"][:12], constat["how"], constat["subject"]))

            done = work.merged(db, args.id, sha=args.sha or "", note=_note_arg(args.note),
                               actor=args.actor, repo=args.repo,
                               migration_landed=args.migration_landed,
                               cfg=cfg, announce=_declare_constat, pr=args.pr)
            if done["result"] == "already":
                # #3413 : dire ce qui a été fait (ou pourquoi rien ne l'a été) ;
                # « nothing to do » cachait la relecture non ouverte et son motif.
                detail = str(done.get("detail") or "").strip()
                prefix = "already %s" % done["item"]["state"]
                if detail.startswith(prefix):
                    detail = detail[len(prefix):].lstrip(" —-")
                print(('%s #%d already %s%s') % (v.noun, args.id, done["item"]["state"], ": %s" % detail if detail else ""))
            elif done["result"] == "landed":
                print(('%s #%d already %s: %s') % (v.noun, args.id, done["item"]["state"], done["detail"]))
            else:
                print(('%s #%d → merged (commit %s, declared by %s)') % (v.noun, args.id, (done.get("sha") or args.sha or "?")[:12], args.actor))
                print("%s: %s" % (done["item"].get("app") or "?", done["item"]["title"]))
            return 0
        if args.work_command == "note":
            text = args.text
            if text == "-":
                # #3683 : comme `agent-mail send <dest> -`, le texte vient de
                # l'entrée standard (heredoc `<<'EOF'`), jamais « - » tel quel.
                from . import cli
                text, error = cli._stdin_body("note")
                if error:
                    raise work.WorkError(error)
            work.note(db, args.id, text, actor=args.actor,
                      wake_ts=_wake_deadline(args))
            print(('note added to %s #%d') % (v.noun, args.id))
            return 0
        if args.work_command == "wake":
            if args.cancel:
                if getattr(args, "wake_at", None) or getattr(args, "wake_in", None):
                    raise work.WorkError("work wake: --cancel goes alone")
                item = work.schedule_wake(db, args.id, None, text=args.why,
                                          actor=args.actor)
                print(('%s #%d: wake cancelled') % (v.noun, item["id"]))
                return 0
            deadline = _wake_deadline(args)
            if deadline is None:
                raise work.WorkError(
                    "work wake: give --at TIME (ISO with timezone), --in DURATION, "
                    "or --cancel")
            item = work.schedule_wake(db, args.id, deadline, text=args.why,
                                      actor=args.actor)
            print(('%s #%d: wake scheduled for %s') % (v.noun, item["id"], time.strftime("%Y-%m-%d %H:%M %Z",
                                               time.localtime(deadline))))
            return 0
        if args.work_command == "set-app":
            from . import work_projects
            item = work_projects.set_app(cfg, db, args.id, args.project, actor=args.actor)
            print(('%s #%d: project %s') % (v.noun, item["id"], item["app"]))
            return 0
        if args.work_command == "set-type":
            item = work.set_type(db, args.id, args.lot_type, actor=args.actor, cfg=cfg)
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
            else:
                print(('%s #%d: type %s') % (v.noun, item["id"], item["type"]))
            return 0
        if args.work_command == "set-priority":
            item = work.set_priority(db, args.id, args.priority, reason=args.reason,
                                     actor=args.actor, cfg=cfg)
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
            else:
                print(('%s #%d: priority %s (%s)') % (v.noun, item["id"], item.get("priority"), work.PRIORITY_LABELS.get(item.get("priority"), "?")))
            return 0
        if args.work_command == "issue-ref":
            if bool(args.ref) == bool(args.none):
                raise work.WorkError("issue-ref: pass owner/repo#N, or --none to clear")
            item = work.set_issue_ref(db, args.id, None if args.none else args.ref,
                                      actor=args.actor, cfg=cfg)
            if args.json:
                print(json.dumps(item, ensure_ascii=False, indent=2))
            else:
                print(('%s #%d: issue_ref %s') % (v.noun, item["id"], item.get("issue_ref") or "—"))
            return 0
        if args.work_command == "backfill-app":
            from . import work_projects
            rows = work_projects.backfill_app(cfg, db, actor=args.actor, dry_run=args.dry_run)
            for row in rows:
                print(('%s #%d: %s — %s') % (v.noun, row["id"], row["status"], row.get("project") or row.get("reason", "changed concurrently")))
            return 0
        if args.work_command == "pending-reviews":
            # #3033 : les avis qu'un agent doit encore rendre (le calcul de la
            # charge de désignation, #3004) ; l'agent lié par défaut.
            from . import review_designation_activation as activation
            agent = None if args.all else (args.agent or "").strip()
            if agent == "":
                # `identity` est local à `cmd_work` (import de la branche `add`) :
                # le nom du module n'est pas lié ici (relecture dev3, B).
                from .identity import resolve_binding
                binding = resolve_binding(cfg, db)
                if not binding.ok:
                    print("ameesh work pending-reviews: give AGENT or --all (no bound "
                          "identity)", file=sys.stderr)
                    return 2
                agent = binding.name
            rows = activation.pending_reviews(db, agent)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print("no pending review%s" % (" for %s" % agent if agent else ""))
                return 0
            from .sous_utilisation import _duration
            now = time.time()
            for row in rows:
                since = row.get("designated_ts")
                print("#%d [%s] %s — %s, head %s, designated %s ago" % (
                    row["lot"], row["state"] or "?", row["title"], row["name"],
                    row["sha"][:12],
                    _duration(now - since) if since is not None else "?"))
            return 0
        if args.work_command == "fixes-owed":
            # #3288 : les corrections dues après un avis bloquant — l'autre
            # moitié du calcul des avis dus (#3033) ; l'agent lié par défaut.
            from . import review_designation_activation as activation
            agent = None if args.all else (args.agent or "").strip()
            if agent == "":
                from .identity import resolve_binding
                binding = resolve_binding(cfg, db)
                if not binding.ok:
                    print("ameesh work fixes-owed: give AGENT or --all (no bound "
                          "identity)", file=sys.stderr)
                    return 2
                agent = binding.name
            rows = activation.due_fixes(db, agent)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print("no fix owed%s" % (" by %s" % agent if agent else ""))
                return 0
            from .sous_utilisation import _duration
            now = time.time()
            for row in rows:
                since = row.get("blocked_ts")
                print("#%d [%s] %s — fix owed by %s, blocked by %s, head %s, "
                      "blocked %s ago" % (
                          row["lot"], row["state"] or "?", row["title"], row["name"],
                          row.get("blocked_by") or "?", row["sha"][:12],
                          _duration(now - since) if since is not None else "?"))
            return 0
        if args.work_command == "reviewers":
            from . import review_designation_activation as activation
            if getattr(args, "if_corrector", ""):
                # #3416 : aperçu en lecture seule avant de pousser un correctif.
                if args.apply:
                    raise work.WorkError(
                        "--if-corrector is an aperçu: --apply is refused")
                from . import review_correction

                room = review_correction.check(cfg, db, args.id,
                                               args.if_corrector, fetch=True)
                if args.json:
                    print(json.dumps(room, ensure_ascii=False, indent=2))
                else:
                    print("correction: %s — %s"
                          % (room.get("state"), room.get("why") or ""))
                if room.get("state") == review_correction.STATE_TOO_TIGHT:
                    return 1
                return 0
            out = activation.reviewers(cfg, db, args.id, apply=args.apply)
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            else:
                freeze = out.get("freeze")
                if not freeze:
                    print(('%s #%d is not frozen: no automatic designation') % (v.noun, args.id))
                else:
                    print(('%s #%d: freeze #%s %s') % (v.noun, args.id, freeze["id"], str(freeze.get("sha") or "")[:12]))
                    designation = out.get("designation")
                    if not designation:
                        print("designation: none recorded")
                    elif designation.get("state") == "designated":
                        names = ", ".join("%s (%s)" % (r.get("name"), r.get("family"))
                                          for r in designation.get("reviewers") or ())
                        print("designation: %s" % names)
                        if designation.get("policy_ref"):
                            print("policy     : %s" % designation["policy_ref"])
                    elif designation.get("state") == "carried":
                        print("designation: kept from carried verdicts (%s)"
                              % ", ".join(str(r.get("name"))
                                          for r in designation.get("reviewers") or ()))
                    elif designation.get("state") == "waiting":
                        # #3596 : l'attente peut déjà porter des sièges.
                        names = ", ".join("%s (%s)" % (r.get("name"), r.get("family"))
                                          for r in designation.get("reviewers") or ())
                        print("designation: waiting%s — %s" % (
                            (" (already designated: %s)" % names) if names else "",
                            designation.get("why") or ""))
                    else:
                        print("designation: %s — %s" % (
                            designation.get("state"), designation.get("why") or ""))
                    if out.get("manual"):
                        print("manual     : %s" % ", ".join(out["manual"]))
                    if out.get("error"):
                        print("mark       : %s" % out["error"])
                    if out.get("historical"):
                        # #3685 : la marque enregistrée nomme un siège qui
                        # n'est plus désigné (hôte perdu, retrait du pilote) ;
                        # la reprise ci-dessous est la désignation vivante.
                        print("mark       : historical — %s" % out["historical"])
            applied = out.get("applied")
            if applied is not None and not args.json:
                state = applied.get("state")
                if state == "designated":
                    names = ", ".join("%s (%s)" % (r.get("name"), r.get("family"))
                                      for r in applied.get("reviewers") or ())
                    print("designated: %s" % names)
                elif state == "waiting":
                    # #3596 : une désignation partielle reste `waiting` mais a
                    # déjà pourvu des sièges ; les montrer avant l'attente.
                    names = ", ".join("%s (%s)" % (r.get("name"), r.get("family"))
                                      for r in applied.get("reviewers") or ())
                    if names:
                        print("designated: %s (partial; review pending)" % names)
                    print("waiting    : %s" % (applied.get("why") or ""))
                elif state == "manual":
                    print("manual     : %s" % (applied.get("why") or ""))
                elif state == "carried":
                    print("kept       : %s" % ", ".join(
                        str(r.get("name")) for r in applied.get("reviewers") or ()))
                elif state in ("unavailable", "none", "unfrozen"):
                    print("%-11s: %s" % (state, applied.get("why") or ""))
                else:
                    print("designation: %s" % state)
            return 0
        if args.work_command == "milestone":
            from . import review_designation_activation as activation
            from . import prevalidation_calls
            # #4073 : une seule échéance pour le geste de gel entier, créée
            # avant les contrôles statiques ; la prévalidation ne reçoit que ce
            # qui reste, sous son propre plafond d'appelant, pour que la
            # commande rende la main sous les 600 s du compositeur.
            freeze_deadline = (prevalidation_calls.freeze_deadline()
                               if args.kind == "frozen" else None)
            outcome: dict = {}
            note = _note_arg(args.note)
            sha = args.sha or ""
            if args.kind == "frozen":
                # #2786 : la tête du gel est un commit complet, résolu dans le
                # dépôt de l'auteur AVANT les contrôles statiques ; une ref
                # symbolique (HEAD, branche) est refusée ici.
                item = work.get(db, args.id)
                if item is None:
                    raise work.WorkError("%s %s not found" % (v.noun, args.id))
                sha = work.freeze_head(db, item, sha, repo=args.repo)
                # #2542 : un contrôle statique rouge refuse le gel, avant le paquet
                from . import freeze_checks
                note = freeze_checks.before_freeze(
                    cfg, db, args.id, sha, repo=args.repo,
                    skip=args.no_static_checks, note=note,
                    deadline=freeze_deadline)
            elif args.no_static_checks is not None or args.prevalidation_map:
                raise work.WorkError("--no-static-checks and --prevalidation-map "
                                     "only apply to a freeze")
            if args.supersedes and args.kind != "frozen":
                raise work.WorkError("--supersedes only applies to a freeze")
            # `--repo` vaut aussi pour un verdict (#2674) : il y résout
            # l'abréviation du SHA relu, et fait alors autorité.
            row = work.milestone(
                db, args.id, args.kind, sha=sha, actor=args.actor or "",
                verdict=args.verdict, note=note, outcome=outcome, repo=args.repo,
                supersedes=args.supersedes or None)
            print(('milestone %s added to %s #%d%s') % (row["kind"], v.noun, args.id, " (%s)" % row["verdict"] if row.get("verdict") else ""))
            if row.get("verdict") == "blocked":
                # #3416 : le relecteur désigné qui bloque corrige souvent
                # lui-même ; prévenu ici, il sait que sa famille deviendrait
                # auteur et que le canon §2 renvoie le correctif à l'auteur.
                from . import review_correction

                advice = review_correction.warning(
                    cfg, db, args.id, row.get("actor") or args.actor or "")
                if advice:
                    print("warning: %s" % advice, file=sys.stderr)
            if args.kind == "frozen":
                # #2962 : l'avertissement de publication (ou de gel distant non
                # vérifié) est dans la note du jalon ; la sortie le dit aussi,
                # sans en faire un refus.
                for line in str(row.get("note") or "").splitlines():
                    if line.startswith((work.FREEZE_UNPUBLISHED_MARK,
                                        work.FREEZE_UNVERIFIED_MARK)):
                        print("warning: %s" % line, file=sys.stderr)
                # #2442 : un réalignement mécanique sur la cible garde les
                # verdicts de la tête relue ; sinon l'exécuteur retente.
                from . import verdict_carry
                carried = verdict_carry.carry(cfg, db, args.id)
                if carried["result"] != "idle":
                    print(verdict_carry.describe(carried))
                # #2487 : la désignation s'est abstenue sur le plan du report.
                # Si le report refuse finalement, les sièges manquants sont
                # désignés maintenant, hors du gel, sans attendre un passage.
                # `busy`/`moved` ne sont pas des refus : un autre hôte porte le
                # report ou le gel a bougé, la désignation attend.
                if outcome.get("state") == "carried" \
                        and carried.get("result") in ("not-carried", "no-repository",
                                                      "error", "idle"):
                    outcome = activation.designate(cfg, db, args.id, force=True)
                    print("review designation after the carry refused: %s"
                          % (outcome.get("state") or "?"))
            state = str(outcome.get("state") or "")
            if state == "designated":
                print("reviewers: %s" % ", ".join(
                    "%s (%s)" % (r.get("name"), r.get("family"))
                    for r in outcome.get("reviewers") or ()))
            elif state == "carried":
                print("reviewers: kept from carried verdicts (%s)" % ", ".join(
                    str(r.get("name")) for r in outcome.get("reviewers") or ()))
            elif state == "waiting":
                # #3596 : une attente peut déjà avoir pourvu des sièges.
                names = ", ".join("%s (%s)" % (r.get("name"), r.get("family"))
                                  for r in outcome.get("reviewers") or ())
                if names:
                    print("reviewers: %s (partial)" % names)
                print("review pending: %s" % (outcome.get("why") or ""))
            elif state == "manual":
                print("reviewers: manual designation kept (%s)"
                      % ", ".join(r.get("name") for r in outcome.get("reviewers") or ()))
            elif state == "unavailable":
                print("review designation unavailable: %s" % (outcome.get("why") or ""))
            elif state == "none":
                # #2561: a light lot has no pre-merge reviewer; say why instead
                # of printing nothing and letting "no reviewer" look like a failure
                print("review: none — %s" % (outcome.get("why") or ""))
            if args.kind == "frozen":
                # #2644 : le résultat de prévalidation s'attache au gel, après
                # le jalon et la désignation. Il ne défait jamais le gel : le
                # run peut être rouge, incomplet ou indisponible, la frise est
                # déjà écrite et l'échec est journalisé.
                prevalidation_calls.after_freeze(
                    cfg, db, args.id, row.get("sha") or args.sha or "",
                    repo=args.repo or "",
                    map_path=args.prevalidation_map or "",
                    deadline=prevalidation_calls.caller_deadline(
                        getattr(args, "prevalidation_budget", None),
                        limit=freeze_deadline))
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
                          harness=getattr(args, "harness", False),
                          profiles=getattr(args, "profiles", False))


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
#: agent, local ; L81 : resident_reread_max (relu cumulé avant rotation)
SET_KEYS = ("model", "effort", "tier", "session_policy", "mode", "context_max_tokens",
            "turn_max_seconds", "turn_mail_max", "turn_grace_seconds", "mail_batch",
            "permission_mode", "lot_wake", "resident", "resident_sleep",
            "resident_reread_max",
            # #4293 : binaire du harnais pour cet agent (canari d'adoption) ;
            # vide = revenir au binaire de l'hôte (PATH / harness_bins)
            "harness_bin")
#: L76: settings written in the local state of the agent's host only
LOCAL_SET_KEYS = ("mail_batch", "permission_mode", "resident", "resident_sleep",
                  "resident_reread_max")
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
    v = _vocabulary()
    from . import adapters, harnesses, runner
    from .config import SESSION_POLICIES

    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db, defer=True)
        from .agent_names import resolve
        args.agent = resolve(args.agent, db=db, operation="settings.permission_mode")
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
                      "resident=on|off resident_sleep=30m|0 resident_reread_max=50M|0 "
                      "harness_bin=/absolute/path (declared on the host; "
                      "empty = host binary)"
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
            if cle == "harness_bin" and valeur:
                # #4293 : un chemin absolu, jamais un nom cherché dans le PATH
                # (le canari doit nommer exactement le binaire évalué) ; la
                # présence est vérifiée à la résolution, sur l'hôte de l'agent.
                if "\x00" in valeur or "\n" in valeur or not os.path.isabs(valeur):
                    print("invalid harness_bin: %r (an absolute path; empty = the "
                          "host binary)" % valeur, file=sys.stderr)
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
            if cle == "resident_reread_max" and valeur:
                try:
                    valeur = str(parse_token_count(valeur))
                except ValueError:
                    print("invalid resident_reread_max: %r (e.g. 50M, 10M, "
                          "0 = never rotate on the cumulative re-read)" % valeur,
                          file=sys.stderr)
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
              "lot_wake=%s harness_bin=%s (takes effect at the next turn)"
              % (args.agent, modele, effort, tier, politique,
                 agent.get("mode") or "execute", plafond_txt, duree_txt, courrier_txt,
                 grace_txt, regroupement_txt, reveil_txt,
                 agent.get("harness_bin") or "host binary"))
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
            print(('warning: external agent without a human responsible: its %s and its alerts have nobody to go to (designate one in the canon)') % v.plural,
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


def build_parser(vocabulary=None) -> argparse.ArgumentParser:
    v = _vocabulary(vocabulary)
    parser = argparse.ArgumentParser(
        prog="agent-mesh", description=_doc(v),
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
    p_hosts.add_argument("--publish", action="store_true",
                         help="publish one reading of this host, for a host without a "
                              "runner (CI VM, server; run by a timer) (#2022)")
    p_hosts.add_argument("--fleet", action="store_true",
                         help="fleet summary: one line per host, stale readings, disk, "
                              "zombies, the hosts to look at first (#2022)")
    p_hosts.add_argument("--send", action="append", default=None, metavar="NAME",
                         help="with --fleet: also mail the summary to NAME's thread "
                              "(repeatable); exits 1 if a send fails (#2022)")
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
        "conflicts", help=("conflict radar between %s, last pass on this host (L160b)") % v.plural)
    p_conflicts.add_argument("--project", default="", help="a single project")
    p_conflicts.add_argument("--all", action="store_true",
                             help="also pairs that overlap without conflict")
    p_conflicts.add_argument("--json", action="store_true", help="`ameesh-radar/1` report")
    p_conflicts.set_defaults(func=cmd_conflicts)

    p_ci = sub.add_parser("ci", help="CI read live by ameesh notify (L161c)")
    ci_sub = p_ci.add_subparsers(dest="ci_command", required=True)
    p_ci_status = ci_sub.add_parser(
        "status", help=("latest CI runs of a %s and their failing tests") % v.noun)
    p_ci_status.add_argument("id", type=int, help=("%s number") % v.noun)
    p_ci_status.add_argument("--limit", type=int, default=5,
                             help="maximum runs shown (default 5)")
    p_ci_status.add_argument("--json", action="store_true", help="`ameesh-ci-status/1` report")
    p_ci_status.set_defaults(func=cmd_ci)
    # #1343 (canon §3) : la CI d'un lot part au gel, après les verdicts requis,
    # par une étiquette `ci-lot-<sha>`/`ci-full-<sha>` posée sur la PR.
    p_ci_run = ci_sub.add_parser(
        "run", help=("request the CI of a frozen %s, once, on the frozen head, "
                    "by the ci-lot/ci-full label (#1343)") % v.noun)
    p_ci_run.add_argument("id", type=int, help=("%s number") % v.noun)
    p_ci_run.add_argument("--sha", default="", help="the frozen commit (default: the "
                          "last `frozen` milestone)")
    p_ci_run.add_argument("--avis", type=int, default=1,
                          help=("required independent verdicts on the frozen commit: 0 (%s), 1 (sensible), 2 (SQL/sécurité/prod)") % (v.noun + " " + v.agree("léger", "légère")))
    p_ci_run.add_argument("--exception", choices=("incident", "unblock"), default="",
                          help="incident|unblock: CI in parallel of the review, marked "
                               "(canon §3); requires --reason")
    p_ci_run.add_argument("--reason", default="",
                          help=("why (the exception, the full suite): written to the %s") % v.noun)
    p_ci_run.add_argument("--full", action="store_true",
                          help=("whole suite (release, package, explicit request), not the "
                               "%s selection; requires --reason") % v.noun)
    p_ci_run.add_argument("--force", action="store_true",
                          help="request the CI again although this composition already "
                               "ran, none green (red or cancelled): explicit resumption")
    p_ci_run.add_argument("--repo", default="", help=("owner/name (default: the %s's "
                          "issue_ref or github.projects)") % v.noun)
    p_ci_run.add_argument("--prevalidation-repo", default="",
                          help="#2644: the tree carrying the packet head (default: cwd, "
                               "then the assignee's worktree, then the project repository)")
    p_ci_run.add_argument("--prevalidation-map", default="",
                          help="#2644: measured map (carte.json.gz) for the L161f plan; "
                               "absent, the last carte artifact is used, or the prevalidation "
                               "is reported unavailable (never blocking)")
    p_ci_run.add_argument("--prevalidation-budget", type=float, default=None,
                          metavar="SECONDS",
                          help="#4067: the whole packet prevalidation (host queue "
                               "included) returns within SECONDS (default 420, under the "
                               "composer's 600 s); spent, it is unavailable (timeout), "
                               "never blocking, and the remote job is cancelled")
    p_ci_run.add_argument("--dry-run", action="store_true",
                          help="decide and print, pose no label")
    p_ci_run.add_argument("--json", action="store_true", help="`ameesh-ci-gel/1` report")
    p_ci_run.set_defaults(func=cmd_ci)
    p_ci_cancel = ci_sub.add_parser(
        "cancel", help=("cancel the open runs of a %s that do not carry its frozen commit") % v.noun)
    p_ci_cancel.add_argument("id", type=int, help=("%s number") % v.noun)
    p_ci_cancel.add_argument("--all", action="store_true",
                             help="also cancel the run of the frozen commit")
    p_ci_cancel.add_argument("--repo", default="", help=("owner/name (default: the %s's "
                             "issue_ref or github.projects)") % v.noun)
    p_ci_cancel.add_argument("--dry-run", action="store_true",
                             help="list the runs, cancel nothing")
    p_ci_cancel.add_argument("--json", action="store_true", help="`ameesh-ci-cancel/1` report")
    p_ci_cancel.set_defaults(func=cmd_ci)

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

    p_handoff = sub.add_parser("handoff", help=("plan a reserved host move at a %s boundary or inspect its journal") % v.noun)
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
    for command in ("advance", "cancel", "retry"):
        operator = handoff_sub.add_parser(command, help="one authorized recoverable step")
        operator.add_argument("operation")
        operator.add_argument("--actor", default=None)
        if command == "retry":
            operator.add_argument("--renew-ttl", type=_handoff_ttl, default=None,
                                  metavar="SECONDS",
                                  help="explicit renewal after reconciliation (30..7200)")
        operator.set_defaults(func=cmd_handoff)

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

    p_work = sub.add_parser("work", help=("%s (work_items)") % v.plural)
    work_sub = p_work.add_subparsers(dest="work_command")
    pw_coherence = work_sub.add_parser(
        "coherence", help=("#4172: backlog coherence pass over the open %s of a "
                          "project (already done, obsolete, duplicates, "
                          "contradictions); deterministic closures and one digest "
                          "per project with --apply") % v.plural)
    pw_coherence.add_argument("--project", "--app", dest="project", default=None,
                              help=("one project (default: every project with open %s)") % v.plural)
    pw_coherence.add_argument("--repo", default=None, metavar="DIR",
                              help="local clone of the project's repository (default: the "
                                   "one declared for the project, else the current one)")
    pw_coherence.add_argument("--apply", action="store_true",
                              help="close the deterministic findings and send the digest "
                                   "(default: read-only report)")
    pw_coherence.add_argument("--actor", default="ameesh",
                              help="journal actor of the closures (default: ameesh)")
    pw_coherence.add_argument("--json", action="store_true")
    pw_coherence.set_defaults(func=cmd_work)
    pw_neighbors = work_sub.add_parser(
        "neighbors", help=("#2988: read-only preview of the %s already touching this "
                          "constat (same file, close title, line changed since the "
                          "reviewed head)") % v.plural)
    pw_neighbors.add_argument("--app", default="", help=("the %s's project") % v.noun)
    pw_neighbors.add_argument("--title", required=True)
    pw_neighbors.add_argument("--body", default="")
    pw_neighbors.add_argument("--reviewed-head", default=None, metavar="SHA",
                              help="#2988: the head the review read (7 to 64 hex)")
    pw_neighbors.add_argument("--repo", default=None, metavar="DIR",
                              help="local clone of the project's repository (default: the "
                                   "one declared for the project, else the current one)")
    pw_neighbors.add_argument("--json", action="store_true")
    pw_neighbors.set_defaults(func=cmd_work)
    pw_add = work_sub.add_parser("add")
    pw_add.add_argument("--title", required=True)
    pw_add.add_argument("--force", metavar="REASON",
                        help=("#540: give the %s to an assignee who already has a %s not "
                             "started (the reason goes to the %s's journal)") % (v.noun, v.noun, v.noun))
    pw_add.add_argument("--type", default="evolution",
                        help="an improvement backlog item goes through "
                             "`ameesh work backlog add` (L119)")
    pw_add.add_argument("--source", default="")
    pw_add.add_argument("--app", default=None,
                        help="project; default: the creating agent's registry or own canon")
    pw_add.add_argument("--new-project", action="store_true",
                        help=("#506: accept an --app project nobody declares yet "
                             "(journaled on the %s); refused otherwise") % v.noun)
    pw_add.add_argument("--body", default=None,
                        help=("the %s's statement; `-` reads it on standard input "
                             "(heredoc <<'EOF', #3744)") % v.noun)
    pw_add.add_argument("--issue-ref", default=None)
    pw_add.add_argument("--workstream", default=None)
    pw_add.add_argument("--assignee", default=None)
    pw_add.add_argument("--budget", type=float, default=None)
    pw_add.add_argument("--actor", default="")
    pw_add.add_argument("--package", default=None, help=("WorkPackage card of the %s (L29)") % v.noun)
    pw_add.add_argument("--parent-lot", type=int, default=None, metavar="LOT",
                        help=("#1193: parent of a suite %s: its blocking prerequisite, linked "
                             "with the creation (unknown parent refused)") % v.noun)
    pw_add.add_argument("--externe", action="store_true",
                        help="force assignment to an external agent that has a human "
                             "responsible (L37, 0030)")
    pw_add.add_argument("--external-executor", default=None, metavar="AGENT",
                        help=("#3447: declare the %s reserved to an external executor "
                             "(an `externe` agent with a human owner, or human:<id>) "
                             "without assigning it; L165 does not take it") % v.noun)
    pw_add.add_argument("--branch", default=None,
                        help=("%s branch: its merge into the target closes the %s (L118)") % (v.noun, v.noun))
    pw_add.add_argument("--target", default=None,
                        help="target branch (default: the repository's git config ameesh.target, "
                             "otherwise origin/HEAD)")
    pw_add.add_argument("--priority", type=int, choices=list(work.PRIORITIES), default=None,
                        help=("1 high, 2 normal, 3 low (L130: every request from a "
                             "human becomes a %s, with its priority)") % v.noun)
    pw_add.add_argument("--estimate", default=None, metavar="DURATION",
                        help=("estimated duration of the %s: 90m, 2h, 1h30, 1,5h, 2d (or "
                             "minutes) — mandatory for an agent (L157)") % v.noun)
    pw_add.add_argument("--estimate-source", default=None, metavar="SOURCE",
                        help="where the estimate comes from (design, bug history ×1.3…)")
    pw_add.add_argument("--size", default=None, choices=list(work.SIZES),
                        help="size of the change: s small, m medium, l large — a key of "
                             "the reference estimate (L157b)")
    pw_add.add_argument("--reviewed-head", default=None, metavar="SHA",
                        help=("#2988: the head the review (ultrareview, verdict, "
                             "re-reading) actually read; written as `reviewed head: "
                             "<sha>` in the creation note. A review-born %s without it "
                             "is still created, marked `absent` and warned") % v.noun)
    pw_add.set_defaults(func=cmd_work)
    pw_fp = work_sub.add_parser(
        "fingerprint", help=("#3885: one %s per server-failure fingerprint (canon §16), "
                            "idempotent: an open %s is noted, a closed one opens a "
                            "recurrence %s") % (v.noun, v.noun, v.noun))
    fp_sub = pw_fp.add_subparsers(dest="fingerprint_command")
    pw_fp_open = fp_sub.add_parser(
        "open", help=("open a %s for empreinte:<app>:<key>, or note the open one; "
                     "P1 as soon as one user is touched") % v.noun)
    pw_fp_open.add_argument("--app", required=True, help="the project of the failure")
    pw_fp_open.add_argument("--key", required=True,
                            help="the stable fingerprint key (route+code+revision, a hash, …)")
    pw_fp_open.add_argument("--title", required=True, help=("the %s title") % v.noun)
    pw_fp_open.add_argument("--users", type=int, default=0,
                            help="number of users touched (P1 when > 0)")
    pw_fp_open.add_argument("--count", type=int, default=0,
                            help="occurrences reported by the source")
    pw_fp_open.add_argument("--route", default="")
    pw_fp_open.add_argument("--code", default="", help="SQLSTATE or exception name")
    pw_fp_open.add_argument("--revision", default="", help="deployed revision")
    pw_fp_open.add_argument("--first-seen", default="", help="ISO timestamp of the first view")
    pw_fp_open.add_argument("--last-seen", default="", help="ISO timestamp of the last view")
    pw_fp_open.add_argument("--actor", default="",
                            help="default: AMEESH_ROLE, else the bound identity")
    pw_fp_open.add_argument("--json", action="store_true")
    pw_fp_open.set_defaults(func=cmd_work)
    pw_list = work_sub.add_parser("list")
    pw_list.add_argument("--state", default=None,
                         help="filter by state: v1 spelling (build, qa…) or its "
                              "generic name (in_progress, to_validate…, #4349)")
    pw_list.add_argument("--assignee", default=None)
    pw_list.add_argument("--limit", type=int, default=50)
    pw_list.add_argument("--json", action="store_true")
    pw_list.add_argument("--freeze", action="store_true",
                         help="#1756: add the effective freeze per row — the declared "
                              "freeze, or the last favourable verdict's commit when it "
                              "covers the head (origin, provenance, explicit causes)")
    pw_list.add_argument("--stale-after", default=None,
                         help=("%s is stale with no activity for (default 6h, L29)") % v.noun)
    pw_list.add_argument("--git-dir", default=None, metavar="D",
                         help="#1905: local repository used to carry a verdict across a "
                              "mechanically constant rebase (with --freeze)")
    pw_list.add_argument("--freeze-target", default=None, metavar="REF",
                         help=("#1905: shared target of the rebase (default: the %s's "
                              "target, else origin/main)") % v.noun)
    pw_list.set_defaults(func=cmd_work)
    pw_show = work_sub.add_parser("show")
    pw_show.add_argument("id", type=int)
    pw_show.add_argument("--json", action="store_true")
    pw_show.add_argument("--git-dir", default=None, metavar="D",
                         help="#1905: local repository used to carry a verdict across a "
                              "mechanically constant rebase")
    pw_show.add_argument("--freeze-target", default=None, metavar="REF",
                         help=("#1905: shared target of the rebase (default: the %s's "
                              "target, else origin/main)") % v.noun)
    pw_show.set_defaults(func=cmd_work)
    from . import work_dependencies
    work_dependencies.add_parser(work_sub, cmd_work)
    work_dependencies.add_landing_parser(work_sub, cmd_work)
    work_dependencies.add_links_parser(work_sub, cmd_work)
    from . import work_footprints
    work_footprints.add_parser(work_sub, cmd_work)
    pw_mig = work_sub.add_parser(
        "migration", help=("reserve the next free migration number for the %s (L122), "
                          "before writing the file; idempotent, visible in work show") % v.noun)
    pw_mig.add_argument("id", type=int)
    pw_mig.add_argument("--number", type=int, default=None,
                        help=("reserve this exact number (refused if it already exists on the "
                             "%s's target branch or is reserved by another %s; branches "
                             "not merged into the target do not count, #2620; the target is the "
                             "%s's, else its project's, else `ameesh.target` — never the clone's "
                             "`origin/HEAD`, and none declared is a refusal, #2660); added to the "
                             "%s's other reservations (#2481)") % (v.noun, v.noun, v.noun, v.noun))
    pw_mig.add_argument("--release", action="store_true",
                        help=("release all the %s's reservations, or only --number when given "
                             "(the %s no longer adds those migrations)") % (v.noun, v.noun))
    pw_mig.add_argument("--repo", default=None,
                        help=("local clone of the %s's repository (that of its issue), whose "
                             "worktree and branches count (default: the clone declared in "
                             "radar.projects, else the current git repository; its origin "
                             "must be the %s's repository)") % (v.noun, v.noun))
    pw_mig.add_argument("--skip-branch", action="append", default=None, metavar="BRANCH",
                        help=("also ignore this branch in the next-free scan, in addition to the "
                             "%s's own branch (repeatable): the integration branch of a package "
                             "being composed, whose renumbered files must not count against the "
                             "reservation (#1603); an exact number reads the target only (#2620)") % v.noun)
    pw_mig.add_argument("--repository", default=None, metavar="OWNER/REPO",
                        help=("#3885: reserve in this repository when the code lives in another "
                             "repository than the %s's issue (a mirror ticket); the local clone "
                             "given by --repo must be a clone of it (default: the issue's "
                             "repository)") % v.noun)
    pw_mig.add_argument("--json", action="store_true")
    pw_mig.add_argument("--actor", default="",
                        help="default: the session's bound identity (AGENT_MAIL_NAME)")
    from . import migration_reservations
    migration_reservations.add_options(pw_mig)
    pw_mig.set_defaults(func=cmd_work)
    pw_promote = work_sub.add_parser("promote", help="record a development delivery promotion")
    pw_promote.add_argument("id", type=int)
    pw_promote.add_argument("--note", default="")
    pw_promote.add_argument("--actor", default="")
    pw_promote.set_defaults(func=cmd_work)
    pw_move = work_sub.add_parser("move")
    pw_move.add_argument("id", type=int)
    pw_move.add_argument("state", choices=list(work_lifecycle.ACCEPTED_STATES),
                         help="v1 spelling or generic lifecycle name (#4349)")
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
                              "alert name them before any verdict; a reviewer of the author's "
                              "declared harness warns (indicative, #800)")
    pw_move.add_argument("--package-exclusion", action="store_true",
                         help=("#4043: qa → build of a %s excluded from a package for a "
                              "negative test (paquet-horaire): no author loop is consumed "
                              "(loops unchanged, never refused at the limit); reserved for "
                              "humans and for orchestrators or design agents") % v.noun)
    pw_move.add_argument("--device", action="append", default=None, metavar="DEVICE",
                         help=("#3070: with blocked, the device (canon Host fiche) whose "
                              "session the %s waits for (repeatable); --note then names the "
                              "cases not played. Declared unavailable (canon §12), the gate "
                              "lifts by itself: see ameesh work device-gates") % v.noun)
    pw_move.set_defaults(func=cmd_work)
    pw_reviewer = work_sub.add_parser(
        "reviewer",
        help=("#2076: add a designated reviewer of a %s already in qa, "
             "without a build/qa round trip") % v.noun)
    pw_reviewer.add_argument("id", type=int)
    pw_reviewer.add_argument("agent", nargs="?", metavar="AGENT",
                             help=("a registry agent, or human:<id>; never the %s's assignee") % v.noun)
    pw_reviewer.add_argument("--remove", action="store_true",
                             help="#2076: remove AGENT's designation; without AGENT, "
                                  "remove all designations (#1979)")
    pw_reviewer.add_argument("--no-verdict", dest="no_verdict", action="store_true",
                             help=("#1979: mark « verdict not required » (mechanical composition "
                                  "resolution, canon §11 bis/#87): the %s waits for its merge "
                                  "and no reminder goes to a reviewer") % v.noun)
    pw_reviewer.add_argument("--actor", default="",
                             help="who designates: the session's bound identity by default; "
                                  "a human or an orchestrator (same authority as --correct)")
    pw_reviewer.add_argument("--floor", type=int, default=None, choices=(0, 1, 2),
                             metavar="N",
                             help="#3183: pilot's review floor: the gate, the freeze "
                                  "designation and paquet-candidats require max(scale, N) "
                                  "reviews of distinct families; raised after a freeze, it "
                                  "completes that freeze's designation (#3557; with an "
                                  "AGENT, the floor alone — --apply completes); journaled, "
                                  "kept across refreezes; 0 withdraws it (a human's floor: "
                                  "by a human)")
    pw_reviewer.add_argument("--reason", default="",
                             help="#3183: why the floor (journaled with it)")
    pw_reviewer.set_defaults(func=cmd_work)
    pw_pending = work_sub.add_parser(
        "pending-reviews",
        help=("#3033: the reviews an agent still owes — open %s whose latest freeze "
             "designates them without a verdict of theirs on that head") % v.plural)
    pw_pending.add_argument("agent", nargs="?", metavar="AGENT",
                            help="default: the session's bound identity")
    pw_pending.add_argument("--all", action="store_true",
                            help="every reviewer's pending reviews")
    pw_pending.add_argument("--json", action="store_true")
    pw_pending.set_defaults(func=cmd_work)
    pw_fixes = work_sub.add_parser(
        "fixes-owed",
        help=("#3288: the fixes still owed after a blocking verdict — open %s whose "
             "latest freeze carries a blocked verdict no later freeze replaced") % v.plural)
    pw_fixes.add_argument("agent", nargs="?", metavar="AGENT",
                          help="default: the session's bound identity")
    pw_fixes.add_argument("--all", action="store_true",
                          help="every assignee's owed fixes")
    pw_fixes.add_argument("--json", action="store_true")
    pw_fixes.set_defaults(func=cmd_work)
    pw_reviewers = work_sub.add_parser(
        "reviewers",
        help="#2447: read the automatic reviewer designation of the latest freeze, "
             "or retry it outside the freeze hook (recovery)")
    pw_reviewers.add_argument("id", type=int)
    pw_reviewers.add_argument("--apply", action="store_true",
                              help="retry the designation of the latest freeze (same head); "
                                   "a waiting state yields to the designation when capacity "
                                   "or the canon policy comes back")
    pw_reviewers.add_argument(
        "--if-corrector", default="", metavar="AGENT",
        help="#3416: read-only preview — would a first-round correction by AGENT "
             "keep enough independent families for the required opinions? "
             "exit 1 when canon §2 sends it back to the author")
    pw_reviewers.add_argument("--json", action="store_true")
    pw_reviewers.set_defaults(func=cmd_work)
    pw_merged = work_sub.add_parser(
        "merged", help=("declare a %s merged outside a PR and without freeze (local merge "
                       "on the target), or by a merged PR of another repository: any open state "
                       "→ merged, merged milestone with the commit") % v.noun)
    pw_merged.add_argument("id", type=int)
    pw_merged.add_argument("--sha", default="",
                           help="the merge commit (required without --pr; with --pr, an equality "
                                "check on the reported merge commit)")
    pw_merged.add_argument("--pr", default=None, metavar="OWNER/REPO#N",
                           help=("#2603: a merged PR outside the %s's repository (canon, home); "
                                "its head must be the %s's freeze, and its merge commit must be "
                                "in the repository's default branch") % (v.noun, v.noun))
    pw_merged.add_argument("--migration-landed", type=int, default=None, metavar="N",
                           help=("the migration number that actually landed (L122/#1567), when "
                                "the landing renumbered the reservation; recorded with the "
                                "merge, or corrected on an already merged %s") % v.noun)
    pw_merged.add_argument("--repo", default=None,
                           help=("local clone of the %s's repository (origin verified); no fetch") % v.noun)
    pw_merged.add_argument("--note", default="")
    pw_merged.add_argument("--actor", default="",
                           help="default: the session's bound identity (AGENT_MAIL_NAME)")
    pw_merged.set_defaults(func=cmd_work)
    pw_assign = work_sub.add_parser(
        "assign", help=("reassign a %s to a wakeable agent (L37, 0030)") % v.noun)
    pw_assign.add_argument("id", type=int)
    pw_assign.add_argument("--force", metavar="REASON",
                           help=("#540: assign even though the agent already has a %s not "
                                "started (the reason goes to the %s's journal)") % (v.noun, v.noun))
    pw_assign.add_argument("agent")
    pw_assign.add_argument("--externe", action="store_true",
                           help="force assignment to an external agent that has a "
                                "human responsible")
    pw_assign.add_argument("--claim", action="store_true",
                           help=("assign only if the %s has no assignee (atomic "
                                "claim); refused, naming its holder, if it is already taken") % v.noun)
    pw_assign.add_argument("--if-holder", default=None, metavar="AGENT",
                           help="#2016: refuse unless the current assignee is still AGENT "
                                "(the holder read when the alert was written)")
    pw_assign.add_argument("--branch", default=None,
                           help=("%s branch (L118); allowed on a %s already assigned "
                                "to this agent") % (v.noun, v.noun))
    pw_assign.add_argument("--target", default=None, help="target branch (see work add)")
    pw_assign.add_argument("--wip-exception", default=None, metavar="REASON",
                           help=("explicit pilot exception to the §4 WIP limit (%s in build "
                                "per agent, capped by the compiled canon): this %s may stay "
                                "beyond the cap; reserved to humans and orchestrator/design "
                                "agents") % (v.plural, v.noun))
    pw_assign.add_argument("--actor", default="")
    pw_assign.set_defaults(func=cmd_work)
    pw_executor = work_sub.add_parser(
        "external-executor",
        help=("#3447: declare the external executor a %s is reserved to (an `externe` "
             "agent with a human owner, or a human), or clear the reservation") % v.noun)
    pw_executor.add_argument("id", type=int)
    pw_executor.add_argument("agent", nargs="?", default=None,
                             help="the external executor (registered `externe` agent, or "
                                  "human:<id>); omitted with --clear")
    pw_executor.add_argument("--clear", action="store_true",
                             help=("clear the reservation (the %s returns to the ordinary queue)") % v.noun)
    pw_executor.add_argument("--json", action="store_true")
    pw_executor.add_argument("--actor", default="")
    pw_executor.set_defaults(func=cmd_work)
    pw_branch = work_sub.add_parser(
        "branch", help=("set a %s's branch explicitly (L118), or detach an obsolete one "
                       "(#2102: closed or superseded PR); journaled, state and assignee kept") % v.noun)
    pw_branch.add_argument("id", type=int)
    pw_branch.add_argument("branch", nargs="?", default=None,
                           help=("the branch to set; with --detach, the branch to detach "
                                "(default: the %s's)") % v.noun)
    pw_branch.add_argument("--target", default=None,
                           help=("target branch (see work add); an empty value clears "
                                "the explicit target and the %s falls back to its "
                                "project's or the repository's default") % v.noun)
    pw_branch.add_argument("--detach", action="store_true",
                           help=("remove the branch from the %s; the merge sweep no longer "
                                "associates it implicitly (lot/<id>-…); Git is not touched") % v.noun)
    pw_branch.add_argument("--reason", default="", help="why the branch is detached (required "
                                                         "with --detach, goes to the journal)")
    pw_branch.add_argument("--actor", default="")
    pw_branch.set_defaults(func=cmd_work)
    pw_delegate = work_sub.add_parser(
        "delegate", help=("delegate a %s to a wakeable agent, with a deadline (L40, 0030): "
                         "without a turn by the delegate by the deadline, it returns to the delegator") % v.noun)
    pw_delegate.add_argument("id", type=int)
    pw_delegate.add_argument("agent")
    pw_delegate.add_argument("--within", required=True,
                             help="deadline: 30m, 2h, 1d (or seconds)")
    pw_delegate.add_argument("--actor", default="",
                             help=("the delegator, to whom the %s will return (default: "
                                  "the current assignee; human:… allowed)") % v.noun)
    pw_delegate.set_defaults(func=cmd_work)
    pw_expire = work_sub.add_parser(
        "expire-delegations",
        help="process expired delegations (also done by the runner on each pass)")
    pw_expire.add_argument("--dry-run", action="store_true",
                           help="say what would be done, without writing anything")
    pw_expire.add_argument("--json", action="store_true")
    pw_expire.add_argument("--actor", default="")
    pw_expire.set_defaults(func=cmd_work)
    pw_gates = work_sub.add_parser(
        "device-gates",
        help=("#3070 (canon §12): blocked %s waiting for a session on a device; a device "
             "declared unavailable lifts the gate (also done by the runner)") % v.plural)
    pw_gates.add_argument("--apply", action="store_true",
                          help=("lift the gates: trace, verification %s, back to the state "
                               "before blocked") % v.noun)
    pw_gates.add_argument("--json", action="store_true")
    pw_gates.add_argument("--actor", default="")
    pw_gates.set_defaults(func=cmd_work)

    pw_note = work_sub.add_parser(
        "note", help="journal note; a `hold:`/lift note claiming a pilot is authenticated",
        epilog="#3498: a pilot hold may declare an external wait — "
               "\"hold: attente bureau : <geste>\" (or owner), with an optional "
               "\"(due 2026-10-25 12:00 UTC)\"; ameesh debit lists it as an "
               "external wait, not an agent wait. #3773: it may also declare "
               "a third party — \"hold: attente externe : revue Apple\". #4235: "
               "a pilot hold may declare an observation window — \"hold: fenêtre "
               ": <motif>\" with --wake-at; ameesh debit lists it apart until "
               "the deadline, outside --check.")
    pw_note.add_argument("id", type=int)
    pw_note.add_argument("text", help="the note, or - to read it on standard input "
                                      "(heredoc <<'EOF': nothing is expanded)")
    pw_note.add_argument("--wake-at", default=None, metavar="TIME",
                         help="#2466: also arm a scheduled wake at this absolute time "
                              "(ISO 8601 with timezone, e.g. 2026-10-18T13:45:00+02:00); "
                              "the note carries its deadline and the runner wakes the "
                              "assignee even under the hold")
    pw_note.add_argument("--wake-in", dest="wake_in", default=None, metavar="DURATION",
                         help="#2466: same, relative to now (e.g. 90m, 2h)")
    pw_note.add_argument("--actor", default="")
    pw_note.set_defaults(func=cmd_work)
    pw_wake = work_sub.add_parser(
        "wake", help=("#2466: arm, replace or cancel a %s's scheduled wake (the runner "
                     "wakes its assignee at the deadline, even under a hold or in "
                     "waiting_human/blocked)") % v.noun)
    pw_wake.add_argument("id", type=int)
    pw_wake.add_argument("--at", dest="wake_at", default=None, metavar="TIME",
                         help="absolute deadline, ISO 8601 with timezone "
                              "(the wake time is not read twice: a timezone is required)")
    pw_wake.add_argument("--in", dest="wake_in", default=None, metavar="DURATION",
                         help="deadline relative to now (e.g. 90m, 2h)")
    pw_wake.add_argument("--cancel", action="store_true",
                         help="remove the armed wake, journaled")
    pw_wake.add_argument("--why", default="", metavar="TEXT",
                         help=("why this wake (kept in the %s's journal)") % v.noun)
    pw_wake.add_argument("--actor", default="")
    pw_wake.set_defaults(func=cmd_work)

    pw_app = work_sub.add_parser("set-app", help=("correct a %s's project, with a journal entry") % v.noun)
    pw_app.add_argument("id", type=int)
    pw_app.add_argument("project")
    pw_app.add_argument("--actor", default="",
                        help="human, orchestrator or design agent; default: bound session")
    pw_app.set_defaults(func=cmd_work)
    pw_type = work_sub.add_parser(
        "set-type", help=("retype a %s (bug/evolution), with a journal entry") % v.noun)
    pw_type.add_argument("id", type=int)
    pw_type.add_argument("lot_type", choices=work.RETYPE_TARGETS,
                         help=("the new %s type; a basket `improvement` can only be left") % v.noun)
    pw_type.add_argument("--json", action="store_true")
    pw_type.add_argument("--actor", default="",
                         help="human, orchestrator or design agent; default: bound session")
    pw_type.set_defaults(func=cmd_work)
    pw_priority = work_sub.add_parser(
        "set-priority", help=("change an open %s's priority (1/2/3), with a journal entry") % v.noun)
    pw_priority.add_argument("id", type=int)
    pw_priority.add_argument("priority", type=int, choices=list(work.PRIORITIES),
                             help="1 high, 2 normal, 3 low")
    pw_priority.add_argument("--reason", default="",
                             help="why the priority changes (required, journaled)")
    pw_priority.add_argument("--json", action="store_true")
    pw_priority.add_argument("--actor", default="",
                             help="human, orchestrator or design agent; default: bound session")
    pw_priority.set_defaults(func=cmd_work)
    pw_iref = work_sub.add_parser(
        "issue-ref", help=("correct a %s's issue reference (owner/repo#N), with a journal entry") % v.noun)
    pw_iref.add_argument("id", type=int)
    pw_iref.add_argument("ref", nargs="?", default=None,
                         help="the new reference, owner/repo#N (default: keep, use --none to clear)")
    pw_iref.add_argument("--none", action="store_true",
                         help=("clear the issue reference (the %s stays where it is)") % v.noun)
    pw_iref.add_argument("--json", action="store_true")
    pw_iref.add_argument("--actor", default="",
                         help="human, orchestrator or design agent; default: bound session")
    pw_iref.set_defaults(func=cmd_work)
    pw_backfill = work_sub.add_parser("backfill-app", help=("fill uniquely inferred projects of open %s") % v.plural)
    pw_backfill.add_argument("--dry-run", action="store_true")
    pw_backfill.add_argument("--actor", default="")
    pw_backfill.set_defaults(func=cmd_work)
    pw_ms = work_sub.add_parser(
        "milestone", help="declare a timeline milestone (freeze, verdict) — R19")
    pw_ms.add_argument("id", type=int)
    # le feu (#1374) a sa propre commande (`ameesh work fire`) : lui seul
    # contrôle qu'il porte sur le gel courant et retire l'auto-fusion.
    pw_ms.add_argument("kind", choices=["frozen", "verdict"],
                       help="frozen (branch frozen) or verdict (review delivered); "
                            "the pilot's fire is `ameesh work fire`")
    pw_ms.add_argument("--sha", default="",
                       help="the frozen or reviewed commit (frozen: a full commit "
                            "resolved in the author's repository, or an abbreviation "
                            "of at least 12 hex characters resolved there; a symbolic "
                            "ref is refused. verdict: a full commit resolved, the "
                            "current frozen head, or an abbreviation of at least 12 "
                            "hex characters resolved in a repository; the resolved "
                            "commit is written complete, since the CI request compares "
                            "the full head)")
    # le verdict est un positionnel (`work milestone 1 verdict ok`), comme la
    # frise se lit ; `frozen` n'en prend pas
    pw_ms.add_argument("verdict", nargs="?", choices=list(work.VERDICTS), default=None,
                       help="for the verdict milestone: ok or blocked")
    pw_ms.add_argument("--note", default="")
    pw_ms.add_argument("--actor", default="")
    # #2542 : les contrôles statiques du projet passent avant le gel ;
    # #2674 : pour un verdict, le dépôt où résoudre l'abréviation du SHA relu
    pw_ms.add_argument("--repo", default=None,
                       help=("frozen|verdict: the repository holding --sha (freeze default: "
                            "the current directory, then the %s's project repository; "
                            "verdict: the abbreviation must resolve there)") % v.noun)
    pw_ms.add_argument("--prevalidation-map", default="",
                       help="#2644: measured map (carte.json.gz) for the freeze "
                            "prevalidation; default: the last carte artifact")
    pw_ms.add_argument("--prevalidation-budget", type=float, default=None,
                       metavar="SECONDS",
                       help="#4067: the freeze prevalidation (host queue included) "
                            "returns within SECONDS (default 420, under the composer's "
                            "600 s); spent, it is journalled unavailable (timeout) and "
                            "the remote job is cancelled; the freeze stays written")
    pw_ms.add_argument("--no-static-checks", dest="no_static_checks", default=None,
                       metavar="REASON",
                       help="frozen: freeze without the project's static checks "
                            "(scripts/freeze-checks.py); the reason goes in the note")
    pw_ms.add_argument("--supersedes", type=int, action="append", default=[],
                       metavar="FROZEN_ID",
                       help=("#2989/#3399: frozen only: a prior freeze of the same "
                            "%s this freeze replaces (repeatable); only a freeze "
                            "absent from every known repository or left out of every "
                            "ref by a force-push (a proven ghost, dangling in the "
                            "object database) can be replaced, and the note keeps "
                            "the machine trace") % v.noun)
    pw_ms.set_defaults(func=cmd_work)
    from . import plan_cli
    plan_cli.add_parsers(work_sub, cmd_work)  # plan de travail (L29)
    from . import prevalidation
    prevalidation.add_parsers(work_sub, cmd_work)  # #1770 prévalidation au gel
    from . import backlog
    backlog.add_parsers(work_sub, cmd_work)  # file d'amélioration (L119, 0037)
    from . import wip
    wip.add_parsers(work_sub, cmd_work)  # limite d'en-cours §4 (owner 14/10)
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
    from . import fire
    fire.add_parser(work_sub, cmd_work)  # feu du pilote au gel (#1374)

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
    pc_turns.add_argument("--message", default=None,
                          help="only turns that served this message (mail id); "
                               "the cost shown is the turn's")
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
    pa_use.add_argument(
        "--for", dest="duration", default="1h", metavar="DUREE",
        help="forcing expires after this duration (default 1h: 90m, 6h, 2d) and "
             "the automatic choice returns; --for 0 is refused")
    pa_use.add_argument(
        "--force", metavar="RAISON", default=None,
        help="force even an account in overage or above its pace; the reason is "
             "journalized in account_switches")
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
    from . import login_cli as login_cli_mod
    pa_login = acc_sub.add_parser(
        "login", help="private OAuth login challenge for a declared account (L232d)")
    pa_login.add_argument("harness", help="harness: " + ", ".join(login_cli_mod.adapter_names()))
    pa_login.add_argument("account", nargs="?", default=None,
                          help="declared account (default: the harness's primary account)")
    pa_login.add_argument("--approver", default=None,
                          help="human:<id> who will sign the challenge")
    pa_login.add_argument("--token-file", metavar="FILE",
                          help="import a human-issued Claude setup-token (0600 file, or - for stdin)")
    pa_login.add_argument("--expires-at", type=float, default=None, metavar="UNIX_TIMESTAMP",
                          help="issuer expiry of an imported setup-token; omitted means unknown")
    pa_login.add_argument("--scope", default="", help="short text shown in the signed summary")
    pa_login.add_argument("--wait", type=float, default=login_cli_mod.TTL_WAIT_S,
                          help="bounded wait for the receipt, in seconds (default 900)")
    pa_login.add_argument("--json", action="store_true")
    pa_login.set_defaults(func=login_cli_mod.cmd_accounts_login)
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
    p_doc.add_argument("--profiles", action="store_true",
                       help="#3345: runner profiles against the host config.json; "
                            "non-zero on a divergence of train, github or release "
                            "(database-free, for deployment checks)")
    p_doc.set_defaults(func=cmd_doctor)

    from . import canon_cli
    canon_cli.add_parsers(sub)

    from . import harness_cli
    harness_cli.add_parsers(sub)

    from . import veille_cli
    veille_cli.add_parsers(sub)

    from . import turn_observe
    turn_observe.add_parsers(sub)

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
            # #3680 : le verdict est celui de la garde — comptes au rythme du
            # canon (`with_canon_pace` : multiplicateur et plancher de
            # sous-utilisation), pas le réglage local de l'hôte.
            items = (accounts_mod.with_canon_pace(
                cfg, accounts_mod.parse(cfg.accounts)).get(harness)
                if harness else None)
            if items:
                # L30 : le forfait n'est « dépassé » que si TOUS les comptes sont au seuil
                now = time.time()
                evals = [accounts_mod.evaluate(book, p, items, now) for p in items]
                reason = "" if any(e.ok for e in evals) else (
                    "all %s accounts at the threshold — %s" % (harness, "; ".join(
                        "%s: %s" % (e.profile.name, e.reason) for e in evals)))
                reason = reason or book.over(agent, pace=False, account_checked=True)
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
        plafonds = {}
        for ligne in comptes:
            if ligne["active"]:
                actifs[ligne["harness"]] = (ligne["account"], ligne["_gauges"])
                rythmes[ligne["harness"]] = ligne.get("pace_multiplier", 1.0)
                # #3680 : le plafond de la garde par fenêtre (`limits` calculé
                # par accounts.report : plancher de sous-utilisation, plafond
                # de rythme et réserve compris), jamais un rythme recalculé
                # sans plancher.
                plafonds[ligne["harness"]] = {
                    g["key"]: float(g["cap"]) for g in ligne["gauges"]
                    if g.get("cap") is not None}
        rows = book.report(accounts=actifs, multipliers=rythmes, caps=plafonds)
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
    # #3680 : les mêmes plafonds de la garde que `cmd_cost` (mêmes lectures,
    # même cache `db.prefetch`).
    plafonds = {ligne["harness"]: {g["key"]: float(g["cap"])
                                   for g in ligne["gauges"] if g.get("cap") is not None}
                for ligne in comptes if ligne["active"]}
    book.report(accounts=actifs, multipliers=rythmes, caps=plafonds)
    _real_spend_24h(db)


def _config_origin(cfg: Config) -> dict:
    """#1466 : provenance de la configuration lue — fichier, base héritée et
    clés qu'un profil écrase (les comptes, notamment)."""
    return {"path": cfg.config_path, "base": cfg.config_base,
            "shadowed": list(cfg.config_shadowed or ()),
            "source": cfg.config_source}


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
            # #2572 (#313 N2) : un compte refusé par la politique de partage
            # (`account_sharing: strict`, `accounts_shared: refuse`) est refusé
            # ici, avec sa raison, au lieu d'être forcé puis rendu sans
            # placement au tour suivant.
            from . import account_claims
            items = account_claims.mark_refused(cfg, items, db=db)
            # #3755 : un forçage a une échéance (courte par défaut) et ne
            # contourne pas la garde : le compte est évalué, et refusé s'il
            # est en overage ou au-delà de son rythme, sauf --force "raison".
            from . import stagnation
            try:
                duree = stagnation.parse_duration(
                    getattr(args, "duration", None) or "1h")
            except stagnation.StaleError as exc:
                print("accounts use: %s" % exc, file=sys.stderr)
                return 1
            raison = getattr(args, "force", None)
            now = time.time()
            until = now + duree
            book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db)
            accounts_mod.force(db, cfg.host, args.harness, items, args.account, by=qui,
                               book=book, now=now, until=until,
                               allow=bool(raison), reason=raison or "")
            echeance = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
            texte = ("Account %s forced for %s on %s by %s for %s (until %s): "
                     "automatic choice returns then." % (
                         args.account, args.harness, cfg.host, qui,
                         accounts_mod.duration(duree), echeance))
            if raison:
                texte += " Forced above the guard: %s." % " ".join(str(raison).split())
            fil.record(cfg, db, sender=qui, recipients=[], text=texte,
                       meta={"action": "compte", "type": "manuel", "harnais": args.harness,
                             "vers": args.account, "audit": "accounts",
                             "duree_s": duree, "echeance_ts": until,
                             "raison": raison or ""})
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
        # #1580 point 2 : la marge des forfaits et les agents déplaçables
        # (lecture seule : aucune bascule n'est faite ici)
        prop = rebalance_mod.proposal(cfg, db, book)
        # #3779 : la fenêtre courte du fournisseur (bucket de rafale partagé
        # par les sessions du compte), lue dans le journal central — la
        # « jauge » demandée par le témoin du lot.
        courte = account_window.rows(db, rows, profiles=declares)
        bascules = storage.of(db).accounts.switches(cfg.host, None, max(0, getattr(args, "last", 5))) \
            if declares else []
        if getattr(args, "json", False):
            try:
                sante = storage.of(db).credentials.listing(cfg.host)
            except db_mod.DbError:
                sante = []  # base sans la migration 0060
            print(json.dumps({"schema": "ameesh-accounts/1", "host": cfg.host,
                              "config": _config_origin(cfg),
                              "accounts": rows, "switches": bascules,
                              "credentials": sante, "proposal": prop.as_dict(),
                              "short_window": courte},
                             ensure_ascii=False, indent=2, default=str))
            return 0
        if cfg.config_base:
            # #1466 : d'où viennent ces comptes — un profil peut diverger de
            # la configuration de l'hôte sans que personne ne le voie
            note = ("config     : %s (profile; host config %s)"
                    % (cfg.config_path, cfg.config_base))
            if cfg.config_shadowed:
                note += "; overrides: %s" % ", ".join(cfg.config_shadowed)
            if "accounts" in (cfg.config_shadowed or ()):
                note += ("\n             WARNING: this profile's `accounts` override the "
                         "host's; a stale profile diverges silently from other runners")
            print(note)
        print(accounts_mod.format_rows(rows))
        if courte:
            print()
            print("short window (provider burst bucket shared by sessions, #3779):")
            for ligne in account_window.format_rows(courte):
                print(ligne)
        print(prop.text())
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
    now = time.time()
    return max(0.0, now - progress.parse_since(text, now))


def _cost_l26(cfg: Config, db, what: str, args) -> int:
    """`cost turns|gauges|balance` (L26) : lectures du grand livre, de
    l'historique des jauges et des soldes ; le calcul des coûts n'est pas
    touché. Schémas : docs/EXPLOITATION.md."""
    ops = storage.of(db).operations
    if what == "turns":
        limit = int(args.limit)
        rows = ops.turns(agent=args.agent, since_s=_since_seconds(args.since),
                         limit=limit + 1 if limit > 0 else limit,
                         message=getattr(args, "message", None))
        # #3452 : le plafond s'appliquait sans le dire — 500 tours sur ~4 h 30
        # d'une nuit se lisaient comme la fenêtre entière. La troncature est
        # désormais dite, en JSON comme en texte.
        truncated = limit > 0 and len(rows) > limit
        if truncated:
            rows = rows[:limit]
        if args.json:
            payload = {"schema": "ameesh-turns/1", "turns": rows,
                       "truncated": truncated, "limit": limit}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if truncated:
                print("note: window truncated at %d turn(s); raise --limit to see "
                      "the rest" % limit, file=sys.stderr)
            return 0
        if truncated:
            print("note: window truncated at %d turn(s); raise --limit to see the rest"
                  % limit, file=sys.stderr)
        if getattr(args, "message", None) is not None:
            # L80b : le coût est celui du TOUR qui a servi le message, partagé
            # avec les autres messages du même tour — jamais divisé.
            print("turns serving message %s (turn cost, not divided)" % args.message)
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
            # #1132 : la provenance — instant de production et journal d'origine
            # — est affichée quand le relevé la porte.
            produit = ("  produit %s" % _fmt_moment(row["produced_ts"])
                       if row.get("produced_ts") else "")
            source = ("  source %s" % row["source"]) if row.get("source") else ""
            print("%s  %-7s %-16s %5.1f%%  resets %s%s%s" % (
                _fmt_moment(row["observed_ts"]),
                row["harness"] + ("/%s" % row["account"] if row.get("account") else ""),
                row["key"],
                float(row["used"]) * 100, _fmt_moment(row.get("resets_at_ts")),
                produit, source))
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
