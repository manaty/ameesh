# SPDX-License-Identifier: AGPL-3.0-only
"""L232e: persistent drain/return requests; host effects use the handoff helper."""
import argparse
import json
import time

from . import config, db as db_mod, estimates, handoffs, host_preflight, registry, storage, work
from .config import NAME_RE


def drain(cfg, db, host, *, duration=None, critical=()):
    if not isinstance(host, str) or not NAME_RE.fullmatch(host):
        raise ValueError("invalid host name")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int)
                                 or not 30 <= duration <= 7 * 86400):
        raise ValueError("drain duration outside 30 seconds..7 days")
    actor = work.corrector(cfg, db)
    critical = set(critical) | critical_names(cfg, db)
    return storage.of(db).host_lifecycle.drain(host, actor=actor, duration=duration, critical=critical)


def critical_names(cfg, db):
    """Declared critical agents and their own canon's coordination/review roles."""
    from . import sous_utilisation
    names = set(getattr(cfg, "critical_agents", ()) or ())
    canons = work.configured_canons(cfg)
    roles = ("orchestrateur", "orchestrator", "conception", "design", "auditeur", "auditor", "reviewer")
    for agent in registry.overview(db):
        own = [canon for canon in canons if canon.owns(agent.get("canon"))]
        if agent["name"] in sous_utilisation.role_orchestrators(own, roles):
            names.add(agent["name"])
    return names


def readiness(db, agent, target, *, adapter=None, requirements_for=None):
    """Evaluate helper inventory and host-scoped logins, exposing only named gaps."""
    required = host_preflight.Requirements(target, agent["harness"], agent.get("session_account") or "")
    missing = []
    if requirements_for is None:
        missing.append("requirements:L232c")
    else:
        required = requirements_for(agent, target)
        if (not isinstance(required, host_preflight.Requirements) or required.host != target
                or required.harness != agent["harness"]):
            raise ValueError("requirements must name the agent's harness and receiving host")
    inventory = None
    if adapter is None:
        missing.append("helper:L232b")
    else:
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
        if agent is None or not readiness(self.db, agent, op["target"], adapter=self.adapter,
                                         requirements_for=self.requirements_for)["ready"]:
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
    api.host_lifecycle.expire_returns()
    control = api.host_lifecycle.get(host)
    if not control or control["mode"] not in ("draining", "returning", "lost"):
        return []
    outcomes = []
    for origin in api.host_lifecycle.agents(host, control["generation"]):
        agent = registry.get(db, origin["agent"])
        if not agent or agent.get("mode") != "execute" or agent.get("status") == "stopped":
            continue
        demand = api.host_lifecycle.demand(agent["name"], agent["host"])
        op = api.handoffs.active(agent["name"])
        missing = []
        if adapter is None:
            missing = ["helper:L232b"]
        elif op:
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
        elif demand:
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
    restore = commands.add_parser("undrain", help="reopen the host and request agents' return")
    restore.add_argument("host")
    status = commands.add_parser("status", help="show durable drain and return progress")
    status.add_argument("host", nargs="?")
    args = parser.parse_args(argv)
    cfg = config.load()
    db = db_mod.connect(cfg)
    try:
        api = storage.of(db).host_lifecycle
        if args.command == "drain":
            duration = estimates.parse(args.duration) * 60 if args.duration else None
            result = drain(cfg, db, args.host, duration=duration)
        elif args.command == "undrain":
            result = api.undrain(args.host, actor=work.corrector(cfg, db))
            if result is None:
                raise ValueError("host has no drain request")
        else:
            result = [api.get(args.host)] if args.host else api.listing()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, db_mod.DbError) as exc:
        print("host request refused: " + str(exc))
        return 1
    finally:
        db.close()
