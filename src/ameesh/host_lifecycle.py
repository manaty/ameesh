# SPDX-License-Identifier: AGPL-3.0-only
"""L232e: persistent drain/return requests; host effects use the handoff helper."""
import argparse
import json
import time

from . import config, db as db_mod, estimates, handoffs, host_preflight, registry, storage, work
from .config import NAME_RE


#: L232e/L165 : a host is stale after three readings, and never under 180 s
#: (a resource poll faster than a minute would otherwise declare a lost host
#: after a hiccup). The election uses the same threshold for a candidate's
#: reading and its standing, so one rule decides freshness everywhere.
MIN_STALENESS_S = 180.0


def staleness_s(interval_s) -> float:
    """The staleness threshold of a host whose readings come every `interval_s`."""
    try:
        interval = max(0.0, float(interval_s or 0.0))
    except (TypeError, ValueError):
        interval = 0.0
    return max(MIN_STALENESS_S, 3.0 * interval)


def drain(cfg, db, host, *, duration=None, critical=(), actor=None,
          automatic=False):
    """`actor`: the human the API authenticated (L87); otherwise the session's
    corrector (a human, an orchestrator or a design agent)."""
    if not isinstance(host, str) or not NAME_RE.fullmatch(host):
        raise ValueError("invalid host name")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int)
                                 or not 30 <= duration <= 7 * 86400):
        raise ValueError("drain duration outside 30 seconds..7 days")
    if actor is None:
        actor = work.corrector(cfg, db)
    elif not actor.startswith(work.HUMAN_PREFIX):
        raise ValueError("drain: an explicit actor is a human (human:<id>)")
    critical = set(critical) | critical_names(cfg, db)
    # #4148: only an external adapter requires the durable claim gate; without
    # one, `draining` alone closes claims and `returning` must reopen at once
    # for an old binary's agents to come home.
    from . import host_release
    try:
        canons = work.configured_canons(cfg)
    except Exception:  # an unreadable canon must not reopen the gate
        canons = []
    try:
        close_claims = bool(host_release.hook_for(cfg, canons, host))
    except ValueError:
        close_claims = True  # ambiguous adapter config: fail closed
    return storage.of(db).host_lifecycle.drain(
        host, actor=actor, duration=duration, critical=critical,
        automatic=bool(automatic), close_claims=close_claims)


def undrain(cfg, db, host, *, actor=None, runner=None, clock=None):
    """#4148: return a withdrawn host in the strict order (`busy` first)."""
    from . import host_release
    if not isinstance(host, str) or not NAME_RE.fullmatch(host):
        raise ValueError("invalid host name")
    if actor is None:
        actor = work.corrector(cfg, db)
    kwargs = {} if clock is None else {"clock": clock}
    return host_release.request_return(cfg, db, host, actor=actor,
                                       runner=runner, **kwargs)


def enter_draining(cfg, db, host, *, actor, runner=None):
    """Emit the `draining` acknowledgement of a withdrawal (best effort).

    Used by the command after a manual drain; the runner scanner retries
    whatever this pass could not do, and a failure keeps claims closed."""
    from . import host_release
    return host_release.enter_draining(cfg, db, host, actor=actor,
                                       runner=runner)


def critical_names(cfg, db, *, canons=None):
    """Declared critical agents and every agent L242c treats as critical.

    #3643: the roles are each canon's `critical_roles` (`federation.yaml`; the
    code default applies when it declares none), never a list kept here. An
    organisation that leaves `relecture` out (manaty/home#179) therefore no
    longer counts its reviewers as critical when the host is drained.

    #3641: the verdict comes from `sous_utilisation.critical_verdict`, the
    same safe reading as the runner and the auditor (#424): a canon whose
    owner cannot be read (or is only partly read) falls back to the last
    recorded verdict, and to critical when none is known; the auditor persona
    is critical without a card. A role list kept here could only lose those
    cases silently.
    """
    from . import canon as canon_mod, sous_utilisation
    names = set(getattr(cfg, "critical_agents", ()) or ())
    # #4377: a caller that already holds every configured canon passes them
    # (`host_idle._complete_canons`); otherwise the all-or-nothing read.
    canons = list(canons) if canons is not None else work.configured_canons(cfg)
    try:
        # A canon configured but unreadable yields no loaded canon; it is
        # still a configured canon, and its agents must fall back to the last
        # verdict (or critical), never to "no canon on this host".
        configured = bool(canons) or bool(canon_mod.configured(cfg))
    except Exception:  # noqa: BLE001 - an unreadable configuration is not "no canon"
        configured = True
    known = sous_utilisation.critical_memory(cfg)
    for agent in registry.overview(db):
        verdict, _unknown = sous_utilisation.critical_verdict(
            agent, canons, configured=configured,
            known=sous_utilisation.remembered(known, agent["name"]))
        if verdict:
            names.add(agent["name"])
    return names


def reconcile(cfg, db, host, *, actor=None):
    """#3035: verified reopening of a lost host, for a lifecycle pass or the command.

    The proof is database state read under the host gate lock (readings in
    database time covering one detection window without a hole, origins home
    and unfenced, no active handoff, no recovery attempt since the loss),
    never the returning machine's own declaration. A live lease of an agent
    that is home is the returning host's own runner and does not block the
    proof (#3180); a recovery that moved the agent elsewhere still closes it.
    A refusal names the missing proof instead of forcing the host open.
    """
    if not isinstance(host, str) or not NAME_RE.fullmatch(host):
        raise ValueError("invalid host name")
    if actor is None:
        actor = work.corrector(cfg, db)
    fresh_within = max(180, int(getattr(cfg, "resource_interval", 60) * 3))
    return storage.of(db).host_lifecycle.reconcile_loss(
        host, actor=actor, fresh_within=fresh_within)


def _prerequisite_gaps(adapter, requirements_for):
    """The same named prerequisites for ``readiness`` and ``advance_host``."""
    missing = []
    if adapter is None:
        missing.append("helper:L232b")
    if requirements_for is None:
        missing.append("requirements:L232c")
    return missing


def readiness(db, agent, target, *, adapter=None, requirements_for=None, account=None):
    """Evaluate helper inventory and host-scoped logins, exposing only named gaps.

    ``account`` is the frozen plan account for a post-switch step: the switch
    clears the registry's ``session_account``, so re-reading it would refuse
    the exact account the operation was admitted with (H4 freezes it in the
    plan). Without an override the registry value is used, as for a new plan.
    """
    if account is None:
        account = agent.get("session_account") or ""
    required = host_preflight.Requirements(target, agent["harness"], account)
    missing = _prerequisite_gaps(adapter, requirements_for)
    if requirements_for is not None:
        try:
            required = requirements_for(dict(agent, session_account=account), target)
        except Exception:
            # A refused agent/host identity is the same prerequisite as an
            # absent factory: named, never a raw factory diagnostic.
            return {"host": target, "ready": False,
                    "missing": sorted(set(missing + ["requirements:L232c"]))}
        if (not isinstance(required, host_preflight.Requirements) or required.host != target
                or required.harness != agent["harness"]):
            raise ValueError("requirements must name the agent's harness and receiving host")
    inventory = None
    if adapter is not None:
        try:
            inventory = adapter.inventory(target)
        except Exception:
            missing.append("host_inventory:" + target)
    result = host_preflight.check(required, inventory, storage.of(db).credentials.listing(target))
    result["missing"] = sorted(set(result["missing"] + missing))
    result["ready"] = not result["missing"]
    return result


class _ReadyAdapter:
    def __init__(self, db, adapter, requirements_for):
        self.db, self.adapter, self.requirements_for = db, adapter, requirements_for

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def revalidate(self, op):
        agent = registry.get(self.db, op["agent"])
        # H4 freezes the exact account in the plan; after the switch the
        # registry's session_account is cleared, so readiness must use the
        # frozen value for every post-switch step.
        if agent is None or not readiness(
                self.db, agent, op["target"], adapter=self.adapter,
                requirements_for=self.requirements_for,
                account=op["plan"].get("account"))["ready"]:
            return False
        # A return request supersedes a move still on the source. Once the
        # target may have work, complete reconciliation there before return.
        checkpoint = op["evidence"].get("held_at", op["state"]) if op["state"] == "held" else op["state"]
        if checkpoint not in ("switched", "activated", "verified"):
            plan = op["plan"]
            if "lifecycle_host" not in plan:
                return False
            control = storage.of(self.db).host_lifecycle.get(plan["lifecycle_host"])
            expected = {"host_drain": "draining", "host_return": "returning", "host_loss": "lost"}[plan["reason"]]
            if not control or control["mode"] != expected or control["generation"] != plan["lifecycle_generation"]:
                return False
        return self.adapter.revalidate(op)


def advance_host(db, host, *, limits_for, adapter=None, requirements_for=None):
    """One critical-first controller pass; no host effect without the enrolled helper."""
    api = storage.of(db)
    # #4148: never open claims here; the runner already expired with
    # closed claims, and a due planned return must wait for `busy`.
    api.host_lifecycle.expire_returns(open_claims=False)
    control = api.host_lifecycle.get(host)
    if not control or control["mode"] not in ("draining", "returning", "lost"):
        return []
    outcomes = []
    for origin in api.host_lifecycle.agents(host, control["generation"]):
        agent = registry.get(db, origin["agent"])
        if not agent or agent.get("mode") != "execute" or agent.get("status") == "stopped":
            continue
        # L165 (EP1) : un rôle élu se reprend par élection (bail expiré), sans
        # preuve de source L232e ; il n'a rien à faire dans un handoff.
        if agent.get("releve") == "election":
            continue
        demand = api.host_lifecycle.demand(agent["name"], agent["host"])
        op = api.handoffs.active(agent["name"])
        missing = _prerequisite_gaps(adapter, requirements_for)
        if adapter is not None and op:
            if (control["mode"] == "lost" and op["source"] == host
                    and op["plan"].get("reason") != "host_loss"):
                try:
                    op = handoffs.promote_loss(db, op["id"], adapter) or op
                except handoffs.HandoffError:
                    missing.append("handoff_requires_reconciliation")
                    api.host_lifecycle.progress(host, control["generation"], agent["name"],
                                                operation=op["id"], missing=missing)
                    outcomes.append({"agent": agent["name"], "operation": op, "missing": missing})
                    continue
            wrapped = _ReadyAdapter(db, adapter, requirements_for)
            if control["mode"] == "returning" and op["plan"].get("reason") == "host_drain":
                checkpoint = op["evidence"].get("held_at", op["state"])
                if checkpoint not in ("switched", "activated", "verified"):
                    op = handoffs.cancel(db, op["id"], wrapped)
                else:
                    op = handoffs.advance(db, op["id"], wrapped)
            else:
                op = handoffs.advance(db, op["id"], wrapped)
        elif adapter is not None and demand:
            def ready(row, target):
                result = readiness(db, row, target, adapter=adapter, requirements_for=requirements_for)
                missing.extend(result["missing"])
                return result
            candidate = handoffs.plan(db, agent, limits_for=limits_for, demand=demand, readiness_for=ready)
            if candidate:
                try:
                    repository = adapter.repository(agent)
                    op = handoffs.request(db, candidate, repository=repository)
                    missing = []
                except (handoffs.HandoffError, AttributeError):
                    missing.append("source_lease_or_repository:" + agent["name"])
            elif not missing:
                reading = api.hosts.latest(host)
                if (demand["reason"] == "host_loss" and reading
                        and time.time()-reading["sampled_ts"] < demand["loss_after"]):
                    missing = ["source_loss_grace"]
                else:
                    missing = ["fresh_admitted_capacity"]
        if op and op["state"] == "held":
            missing.append("handoff_requires_reconciliation")
            if op["plan"].get("reason") == "host_loss" and not op["evidence"].get("source_recovered"):
                missing.append("source_quiescence_and_published_checkpoint")
        if not op and origin.get("operation"):
            op = api.handoffs.get(origin["operation"])
        api.host_lifecycle.progress(host, control["generation"], agent["name"],
                                    operation=op["id"] if op else None, missing=sorted(set(missing)))
        outcomes.append({"agent": agent["name"], "operation": op, "missing": sorted(set(missing))})
    if control["mode"] == "returning":
        api.host_lifecycle.complete_return(host, control["generation"])
    return outcomes


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh host")
    commands = parser.add_subparsers(dest="command", required=True)
    withdraw = commands.add_parser("drain", help="close new turns and prepare agents' evacuation")
    withdraw.add_argument("host")
    withdraw.add_argument("--for", dest="duration", help="request return after this duration, e.g. 8h")
    withdraw.add_argument("--actor", help="who drains (human:<id>) from an unbound terminal")
    restore = commands.add_parser("undrain", help="reopen the host and request agents' return")
    restore.add_argument("host")
    restore.add_argument("--actor", help="who reopens (human:<id>) from an unbound terminal")
    recovered = commands.add_parser(
        "reconcile", help="verified reopening of a lost host whose readings returned")
    recovered.add_argument("host")
    recovered.add_argument("--actor", help="who reconciles (human:<id>) from an unbound terminal")
    status = commands.add_parser("status", help="show durable drain and return progress")
    status.add_argument("host", nargs="?")
    args = parser.parse_args(argv)
    cfg = config.load()
    db = db_mod.connect(cfg)
    try:
        api = storage.of(db).host_lifecycle
        if args.command == "drain":
            duration = estimates.parse(args.duration) * 60 if args.duration else None
            # An explicit --actor is authenticated like every other correction:
            # a bound agent session must not claim a human's name.
            actor = work.corrector(cfg, db, args.actor) if args.actor else None
            result = drain(cfg, db, args.host, duration=duration, actor=actor)
            # #4148: best-effort adapter acknowledgement; the runner scanner
            # retries, and a failure keeps the claims closed.
            result = enter_draining(cfg, db, args.host,
                                    actor=(actor or result.get("actor") or "ameesh")) or result
        elif args.command == "undrain":
            # #4148: the `busy` hook answers before any claim reopens.
            result = undrain(cfg, db, args.host,
                             actor=work.corrector(cfg, db, args.actor))
            if result is None:
                raise ValueError("host has no drain request")
        elif args.command == "reconcile":
            result = reconcile(cfg, db, args.host,
                               actor=work.corrector(cfg, db, args.actor))
        elif args.host:
            row = api.get(args.host)
            if row is not None:
                # #4148: the acknowledged adapter transitions are the evidence
                # an operator reads; one line per generation and state.
                row["transitions"] = api.transition_journal(args.host)
            result = [row]
        else:
            result = api.listing()
        print(json.dumps(result, ensure_ascii=False))
        if args.command == "reconcile" and not (
                result.get("reconciled") or result.get("mode") == "available"):
            return 1
        return 0
    except (ValueError, work.WorkError, db_mod.DbError) as exc:
        print("host request refused: " + str(exc))
        return 1
    finally:
        db.close()
