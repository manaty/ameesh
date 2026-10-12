# SPDX-License-Identifier: AGPL-3.0-only
"""L232a: plan and recover a host handoff, using the L232b host adapter.

No SSH, root operation or credential access is performed in this module.
An adapter must make each external step idempotent by operation id, enforce
its enrolled host/approved artifact policy, and return only allowlisted proofs.
"""
from __future__ import annotations

import math
from functools import wraps
import os
import re
import subprocess
import time
import uuid
from urllib.parse import urlsplit

from . import projects, resources, storage
from .db import DbError

GIB = 1024 ** 3
MAX_AGE = 60
SAMPLES = 3
COOLDOWN = 1800
SHA = re.compile(r"^[0-9a-f]{40,64}$")
STEPS = {"planned": ("prepare", "prepared"), "drained": ("publish", "published"),
         "published": ("transfer", "transferred"), "transferred": ("session", "session_ready"),
         "switched": ("admit", "activated"), "activated": ("verify", "verified")}


class HandoffError(ValueError):
    pass


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _fresh(reading, now):
    ts = reading.get("sampled_ts") if reading else None
    return isinstance(ts, (int, float)) and math.isfinite(ts) and 0 <= now-ts <= MAX_AGE


def _readings_ready(rows, now):
    return len(rows) >= SAMPLES and all(_fresh(r,now) and all(
        isinstance(r.get(k),(int,float)) and not isinstance(r[k],bool) and math.isfinite(r[k])
        for k in ("mem_available_bytes","swap_used_bytes","load1","cpu_count","disk_free_bytes"))
        and r["cpu_count"] > 0 for r in rows)


def _capacity(reading, limits):
    offer = limits.get("lend")
    offered = resources.offered(reading, offer) if offer is not None else None
    if offer is not None and (not offered or offered.get("memory_bytes") is None
                              or offered.get("disk_bytes") is None):
        return 0, 0
    memory = reading.get("mem_available_bytes")
    disk = reading.get("disk_free_bytes")
    if offered:
        memory = min(memory or 0, offered["memory_bytes"])
        disk = min(disk or 0, offered["disk_bytes"])
    return max(0, (memory or 0)-limits["min_mem_available"]), max(0, (disk or 0)-limits["min_disk_free"])


def plan(db, agent, *, limits_for, queued=True, now=None, memory_bytes=4*GIB, disk_bytes=2*GIB):
    """Fresh admitted targets; sustained pressure or materially better spare capacity.

    limits_for returns the effective physical limits (strictest across canons),
    including max_agents. Never invent a capacity cap when the card is absent.
    """
    now = time.time() if now is None else now
    if agent.get("status") in ("stopped", "paused") or agent.get("mode") != "execute":
        return None
    api = storage.of(db)
    if api.handoffs.active(agent["name"]) or api.handoffs.cooling(agent["name"], COOLDOWN):
        return None
    history = api.hosts.history(agent["host"], SAMPLES)
    if not _readings_ready(history, now):
        return None
    limits = limits_for(agent["host"])
    if not limits:
        return None
    current_pressure = all(resources.pressure(r, limits=limits)["blocked"] for r in history)
    cap = limits.get("max_agents")
    if isinstance(cap,int) and cap > 0 and api.handoffs.occupancy(agent["host"]) >= cap:
        current_pressure = True
    current_mem, _ = _capacity(history[-1], limits)
    found = []
    for target in agent.get("admitted_hosts") or []:
        if target == agent["host"]:
            continue
        rows = api.hosts.history(target, SAMPLES)
        target_limits = limits_for(target)
        if (not target_limits or not isinstance(target_limits.get("max_agents"), int)
                or target_limits["max_agents"] < 1 or not _readings_ready(rows,now)
                or any(resources.pressure(r, limits=target_limits)["blocked"] for r in rows)):
            continue
        canon = agent.get("canon") or ""
        source_state = api.canon.state(agent["host"], canon)
        target_state = api.canon.state(target, canon)
        if (not source_state or not target_state or source_state["status"] != "ok"
                or target_state["status"] != "ok" or not source_state.get("last_good_commit")
                or source_state["last_good_commit"] != target_state.get("last_good_commit")):
            continue
        memory, disk = _capacity(rows[-1], target_limits)
        if memory < memory_bytes or disk < disk_bytes:
            continue
        if not current_pressure and (not queued or memory < current_mem+memory_bytes):
            continue
        found.append({"agent": agent["name"], "source": agent["host"], "target": target,
                      "owner": agent.get("lease_owner"), "epoch": agent.get("lease_epoch"),
                      "canon": canon, "canon_revision": source_state["last_good_commit"],
                      "memory_bytes": memory_bytes, "disk_bytes": disk_bytes,
                      "available_memory": memory, "available_disk": disk,
                      "max_agents": target_limits["max_agents"],
                      "sampled_ts": rows[-1]["sampled_ts"], "harness": agent["harness"],
                      "reason": "source_pressure" if current_pressure else "spare_capacity",
                      "source_cwd": agent.get("cwd") or "",
                      "checkpoint_lot": agent.get("session_work_item"),
                      "project": projects.agent_project(agent) or ""})
    return max(found, key=lambda p: (p["available_memory"], p["available_disk"], p["target"])) if found else None


def request(db, candidate, *, repository, ttl=900, now=None):
    """Reserve an admitted target; repository is an enrolled identity, never a directory label."""
    now = time.time() if now is None else now
    if not candidate or not _fresh(candidate, now) or not repository or not candidate.get("owner"):
        raise HandoffError("fresh plan, source lease and enrolled repository required")
    if not 30 <= ttl <= 7200:
        raise HandoffError("reservation TTL outside 30..7200 seconds")
    parsed = urlsplit(repository)
    if (parsed.password is not None or parsed.query or parsed.fragment
            or (parsed.scheme == "https" and parsed.username is not None)
            or any(ord(c) < 32 for c in repository)):
        raise HandoffError("repository reference contains credentials or control characters")
    for key in ("memory_bytes", "disk_bytes", "max_agents"):
        if isinstance(candidate.get(key), bool) or not isinstance(candidate.get(key), int) or candidate[key] <= 0:
            raise HandoffError("invalid reservation capacity")
    candidate = dict(candidate, repository=repository)
    row = storage.of(db).handoffs.reserve(uuid.uuid4().hex, candidate, ttl=ttl)
    if not row:
        raise HandoffError("destination capacity or source identity changed")
    if row["plan"]["repository"] != repository or row["target"] != candidate["target"]:
        raise HandoffError("agent already has a different handoff")
    return row


def published_checkout(directory, repository):
    """Read actual Git identity and confirm a clean exact commit on the remote.

    Never pushes, commits, copies private files or prints raw remote URLs.
    """
    def git(*args):
        try:
            return subprocess.run(["git", "-C", directory, *args], text=True, capture_output=True,
                                  check=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise HandoffError("repository verification failed") from exc
    if git("remote", "get-url", "origin") != repository:
        raise HandoffError("repository identity mismatch")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise HandoffError("unpublished work: dirty source checkout")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    commit = git("rev-parse", "HEAD")
    remote = git("ls-remote", "--exit-code", "origin", "refs/heads/"+branch).split()
    if not _sha(commit) or not remote or remote[0] != commit:
        raise HandoffError("unpublished work: remote commit differs")
    return {"repository": repository, "branch": branch, "commit": commit}


def _proof(step, result, op):
    """Copy known metadata only, so arbitrary adapter output cannot enter the journal."""
    plan, evidence = op["plan"], op["evidence"]
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise HandoffError("host step not ready: "+step)
    if step in ("prepare", "admit"):
        if (result.get("canon_revision") != plan["canon_revision"]
                or not all(result.get(k) is True for k in ("credentials_ready", "caps_ready", "tooling_ready"))):
            raise HandoffError("destination admission prerequisites changed")
        if (plan["harness"] in ("deepseek", "dsh")
                and result.get("dsh_profile_ready") is not True):
            raise HandoffError("secret-free dsh profile/hooks not prepared")
        if not _sha(result.get("release_revision")):
            raise HandoffError("destination release is not pinned")
        if step == "admit" and result["release_revision"] != evidence.get("release_revision"):
            raise HandoffError("destination release changed after preparation")
        return {"prepared": True, "release_revision": result["release_revision"]}
    if step == "publish":
        if (result.get("repository") != plan["repository"]
                or not isinstance(result.get("branch"), str) or not result["branch"]
                or any(ord(c) < 32 for c in result["branch"])
                or not _sha(result.get("commit")) or result.get("clean") is not True
                or result.get("remote_commit") != result.get("commit")):
            raise HandoffError("source work is not published at its exact commit")
        return {k: result[k] for k in ("repository", "branch", "commit")}
    if step == "transfer":
        if (result.get("commit") != evidence.get("commit") or result.get("repository") != plan["repository"]
                or result.get("branch") != evidence.get("branch") or result.get("clean") is not True
                or not isinstance(result.get("cwd"), str) or not os.path.isabs(result["cwd"])):
            raise HandoffError("destination checkout mismatch or local work")
        return {"cwd": result["cwd"]}
    if step == "session":
        expected = "fresh" if plan["harness"] in ("deepseek", "dsh") else "brief"
        if result.get("mode") != expected or result.get("executor_enabled") is not False:
            raise HandoffError("session must be prepared before executor enable")
        return {"session_mode": expected}
    if step == "verify":
        if (result.get("commit") != evidence.get("commit")
                or not all(result.get(k) is True for k in ("turn_completed", "authenticated", "caps_ready"))):
            raise HandoffError("destination authenticated turn, caps or checkout not verified")
        return {"verified": True}
    raise HandoffError("unknown host step")


def brief(op):
    return ("Resuming after host handoff %s from %s to %s. Repository %s, branch %s at %s; "
            "work directory %s. Your former native session stayed on the source. "
            "Checkpoint lot %s in project %s; read the current assignment, its thread and mailbox "
            "before continuing. Do not reclaim a reassigned lot. "
            "Temporary files belong in $TMPDIR."
            % (op["id"], op["source"], op["target"], op["plan"]["repository"],
               op["evidence"]["branch"], op["evidence"]["commit"], op["evidence"]["cwd"],
               op["plan"].get("checkpoint_lot") or "none", op["plan"].get("project") or "unclassified"))


def _adapter_call(adapter, step, op):
    """Contain host failures without persisting potentially private diagnostics."""
    try:
        return getattr(adapter, step)(op)
    except Exception:
        raise HandoffError("host adapter failed: " + step) from None


def _serialized(function):
    @wraps(function)
    def step(db, operation, adapter, **kwargs):
        try:
            with storage.of(db).handoffs.controller(operation, reconcile=function.__name__ == "retry") as tx:
                if tx is None:
                    raise HandoffError("another controller or unfinished host effect is processing this handoff")
                return function(tx, operation, adapter, **kwargs)
        except DbError:
            raise HandoffError("handoff controller ownership lost; explicit reconciliation required") from None
    return step


def _effect(api, adapter, step, op):
    intent = api.begin_effect(op["id"], op["state"], step)
    if intent is None:
        raise HandoffError("host effect refused: reservation or controller intent changed")
    intent["controller_epoch"] = intent["evidence"]["controller_epoch"]
    result = _adapter_call(adapter, step, intent)
    # An exception or lock loss leaves the durable intent in place: nobody
    # can overtake an external effect whose completion is unknown.
    api.finish_effect(op["id"], intent["controller_epoch"])
    return result


@_serialized
def advance(db, operation, adapter):
    """One recoverable step. L232b supplies the enrolled, idempotent host adapter.

    prepare, stop_source, publish, transfer, session, admit, enable, verify
    are typed adapter operations. Revalidate runs before every step and must
    check current physical capacity, canon revision and policy permissions.
    A retry observes already-completed external effects using the operation id.
    """
    api = storage.of(db).handoffs
    op = api.get(operation)
    if not op:
        raise HandoffError("unknown handoff")
    state = op["state"]
    if state in ("verified", "cancelled", "held"):
        return op
    if op["evidence"].get("cancel_requested"):
        raise HandoffError("source restoration requested; only cancellation may resume")
    try:
        if op["reserved_until_ts"] <= time.time():
            raise HandoffError("reservation expired; explicit replan required")
        if _effect(api, adapter, "revalidate", op) is not True:
            raise HandoffError("host policy, capacity or credentials changed")
        if state == "prepared":
            return api.drain(operation) or op  # active lot/turn keeps running
        if state == "session_ready":
            switched = api.switch(operation, cwd=op["evidence"]["cwd"], brief=brief(op))
            if switched is None:
                raise HandoffError("source identity or destination admission changed before switch")
            return switched
        if state == "drained":
            if _effect(api, adapter, "stop_source", op) is not True:
                raise HandoffError("source executor not stopped")
        if state == "activated":
            op = api.unfence(operation, state, state)
            if op is None:
                raise HandoffError("destination admission changed before enable")
            if _effect(api, adapter, "enable", op) is not True:
                raise HandoffError("destination executor not enabled")
        step, next_state = STEPS[state]
        result = _effect(api, adapter, step, op)
        if step == "verify" and isinstance(result, dict) and result.get("ok") is True and result.get("pending") is True:
            return op  # destination's first authenticated turn is still running
        proof = _proof(step, result, op)
        if state in ("switched", "activated"):
            updated = api.unfence(operation, state, next_state)
            if updated is None:
                raise HandoffError("destination admission changed")
            return updated
        return api.transition(operation, state, next_state, evidence=proof, detail=step) or api.get(operation)
    except HandoffError:
        # The source remains fenced after drain. No guessing, no automatic
        # rollback over new destination work, and no second executor.
        return api.hold(operation, state) or api.get(operation)


@_serialized
def cancel(db, operation, adapter):
    """Only before switching hosts: reconcile stopped source, then release its fence."""
    api = storage.of(db).handoffs
    op = api.get(operation)
    if not op:
        raise HandoffError("unknown handoff")
    state = op["evidence"].get("held_at") if op["state"] == "held" else op["state"]
    if op["state"] == "cancelled":
        return op
    if state in ("switched", "activated", "verified"):
        raise HandoffError("destination may have new work; recovery requires reconciliation")
    if _effect(api, adapter, "restore_source", op) is not True:
        raise HandoffError("source recovery not verified")
    return api.unfence(operation, op["state"], "cancelled") or api.get(operation)


@_serialized
def retry(db, operation, adapter, *, renew_ttl=None):
    """Retry an idempotent step. renew_ttl explicitly reconciles an expired reservation.

    Revalidate must include the current reservation footprint, host capacity,
    stopped/active executors and existing checkout/session effects. Renewal
    preserves placement and checkpoint; it never restores the source.
    """
    api = storage.of(db).handoffs
    op = api.get(operation)
    if not op or op["state"] != "held":
        # Lock loss can leave an intent before a checkpoint was held.
        if not op or not op["evidence"].get("pending_effect"):
            raise HandoffError("only a held or unfinished operation can be retried")
    pending = op["evidence"].get("pending_effect")
    if pending:
        # This adapter operation MUST fence and quiesce the exact former
        # epoch on the hosts, including delayed jobs, before returning proof.
        # No replay or forward progress is allowed on a missing/false proof.
        op = api.prepare_reconcile(operation, pending["epoch"])
        op["controller_epoch"] = op["evidence"]["controller_epoch"]
        proof = _adapter_call(adapter, "reconcile_effect", op)
        if (not isinstance(proof, dict) or proof.get("ok") is not True
                or proof.get("quiesced") is not True or isinstance(proof.get("epoch"), bool)
                or proof.get("epoch") != pending["epoch"]):
            raise HandoffError("unfinished host effect not quiesced")
        op = api.finish_effect(operation, pending["epoch"])
        if op["state"] != "held":
            op = api.hold(operation, op["state"])
        if op["evidence"].get("cancel_requested"):
            return op  # explicit cancellation may now restore the source
    if _effect(api, adapter, "revalidate", op) is not True:
        raise HandoffError("host reconciliation or prerequisites changed")
    if renew_ttl is not None:
        if isinstance(renew_ttl, bool) or not isinstance(renew_ttl, int) or not 30 <= renew_ttl <= 7200:
            raise HandoffError("reservation TTL outside 30..7200 seconds")
        op = api.renew(operation, ttl=renew_ttl)
        if op is None:
            raise HandoffError("host identity, admission or fresh readings changed")
    if op["reserved_until_ts"] <= time.time():
        raise HandoffError("reservation expired; explicit host reconciliation and renewal required")
    previous = op["evidence"].get("held_at")
    if previous not in (*STEPS, "prepared", "session_ready"):
        raise HandoffError("missing recovery checkpoint")
    return api.transition(operation, "held", previous, detail="explicit idempotent retry")
