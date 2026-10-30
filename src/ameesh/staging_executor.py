# SPDX-License-Identifier: AGPL-3.0-only
"""#4880: a durable staging request, carried by an elected service.

The service runs on every configured candidate, outside agent turns. Only
the current role holder may start an attempt. A shared execution lease keeps
an old holder and its replacement from overlapping. After an interrupted
attempt, a site-supplied quiescence probe MUST prove the old execution and
its remote operations stopped before another attempt can start.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from ameesh import role_run, staging_watch, storage

EVENT = "staging.request"


class ExecutionLease(role_run._AlertsLease):
    """Reuse the deadline, renewal and process-group guards of role passes."""
    def __init__(self, db, role, host, key):
        super().__init__(db, role, host)
        self.key = "staging-execution:" + key
        self.owner = "staging-execution:%s:%s" % (host, uuid.uuid4().hex)


def settings(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    candidates = data.get("candidates", [])
    if (not isinstance(candidates, list) or not all(
            isinstance(host, str) and host.strip() for host in candidates)
            or len(set(candidates)) < 2):
        raise ValueError("staging requires at least two declared candidates")
    for field in ("key", "project", "role", "repo", "bascule_env"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError("staging configuration missing " + field)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", data["key"]):
        raise ValueError("invalid staging execution key")
    if not isinstance(data.get("arguments", []), list) or not all(
            isinstance(arg, str) for arg in data.get("arguments", [])):
        raise ValueError("staging arguments must be an argv list")
    probe = data.get("quiescence_probe")
    if probe is not None and (not isinstance(probe, list) or not probe
                              or not all(isinstance(arg, str) for arg in probe)):
        raise ValueError("quiescence_probe must be a nonempty argv list")
    for key, default in (("timeout_s", 7200), ("start_timeout_s", 300)):
        value = float(data.get(key, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(key + " must be finite and positive")
        data[key] = value
    return data


def _record(db, key, data, *, lease):
    # A stale executor must never overwrite its successor's shared request.
    # Recheck and journal in the SAME transaction under the execution lock.
    if lease.remaining() <= 0:
        raise RuntimeError("staging execution lease expired before journal write")
    with db.transaction() as tx:
        st = storage.of(tx)
        epoch = st.work.land_lock(lease.key, lease.owner, lease.ttl, epoch=lease.epoch)
        if epoch != lease.epoch:
            raise RuntimeError("staging execution lease lost before journal write")
        st.events.record(EVENT, "staging-request:" + key, None, data)


def latest(db, key):
    rows = storage.of(db).events.recent_type(
        EVENT, 60, 1, object="staging-request:" + key)
    return rows[0]["data"] if rows else None


def submit(db, cfg, options, sha, *, included=()):
    """Queue a commit without executing any host operation in this process."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("submit requires an exact commit")
    lease = ExecutionLease(db, options["role"], cfg.host, options["key"])
    if lease.acquire() is None:
        raise RuntimeError("staging execution busy; request not replaced")
    try:
        previous = latest(db, options["key"])
        if previous and previous["state"] in ("pending", "running"):
            if previous["sha"] == sha:
                return previous
            if not previous.get("blocked_reason"):
                raise RuntimeError("an unfinished staging request already exists")
        data = dict(request=uuid.uuid4().hex, sha=sha, state="pending",
                    included=[int(lot) for lot in included], notify=options["role"],
                    deadline=time.time() + options.get("start_timeout_s", 300))
        if previous and previous["state"] not in ("completed", "partial"):
            if previous.get("run"):
                data.update(previous_host=previous["host"], previous_run=previous["run"])
            elif previous.get("previous_run"):
                data.update(previous_host=previous["previous_host"],
                            previous_run=previous["previous_run"])
        _record(db, options["key"], data, lease=lease)
        return data
    finally:
        lease.release()


def tick(db, cfg, options) -> dict:
    """Attempt one shared request on the elected, credentialed candidate."""
    host = cfg.host
    if host not in options["candidates"]:
        return {"state": "standby", "reason": "not a candidate"}
    elected = role_run.live(db, options["role"])
    if not elected["held"] or elected.get("holder") != host:
        return {"state": "standby", "reason": "another role holder"}
    lease = ExecutionLease(db, options["role"], host, options["key"])
    if lease.acquire() is None:
        return {"state": "standby", "reason": "execution still leased"}
    execution = role_run._AlertsExecution(lease)
    execution.start()
    try:
        request = latest(db, options["key"])
        if not request or request["state"] not in ("pending", "running"):
            return {"state": "idle"}
        def blocked(reason):
            if not execution.lost.is_set():
                _record(db, options["key"], dict(request, blocked_reason=reason,
                                                notify=options["role"]), lease=lease)
            return {"state": "blocked", "reason": reason}
        # Read again under the shared lease: a role change at acquisition
        # cannot authorize a new attempt under a stale election result.
        elected = role_run.live(db, options["role"])
        if not elected["held"] or elected.get("holder") != host:
            return {"state": "standby", "reason": "role changed"}
        env = dict(os.environ, BASCULE_ENV=options["bascule_env"],
                   STAGING_TIMEOUT_S=str(options.get("timeout_s", 7200)))
        env.pop("AMEESH_TURN_ID", None)
        if request["state"] == "running" or request.get("previous_run"):
            probe = options.get("quiescence_probe")
            if not probe:
                return blocked("quiescence probe missing")
            env.update(STAGING_PREVIOUS_HOST=request.get("previous_host") or request["host"],
                       STAGING_PREVIOUS_RUN=request.get("previous_run") or request["run"])
            # The probe itself is bounded by the same execution watchdog.
            # It must inspect remote operations too, never just a local PID.
            code, lost = role_run._run_under_lease(probe, env, lease, execution)
            if code or lost:
                return blocked("old execution not proved stopped")
        if not Path(options["bascule_env"]).is_file():
            return blocked("candidate deployment access missing")
        # #4885: a sequence launched by another path (by hand, synchronous
        # release, another host) holds no execution lease. Its shared
        # progress does: wait for it, without host operation. staging.sh
        # repeats the check atomically when it starts (exit BUSY).
        previous = request.get("previous_run") or request.get("run")
        others = staging_watch.in_flight(
            storage.of(db).events.latest_type(staging_watch.EVENT, 60, staging_watch.LIMIT),
            now=time.time(), exempt={previous} if previous else ())
        if others:
            reason = "another staging sequence running: " + ", ".join(others)
            if request.get("blocked_reason") == reason:
                return {"state": "blocked", "reason": reason}
            return blocked(reason)
        repo = str(Path(options["repo"]).resolve())
        sha = request["sha"]
        subprocess.run(["git", "-C", repo, "fetch", "--no-tags", "origin"],
                       check=True, timeout=60, stdout=subprocess.DEVNULL)
        # Resolve the requested commit in the candidate's dedicated clone.
        # A missing commit or one outside the declared target is a refusal.
        subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", sha,
                        options.get("target", "origin/main")], check=True,
                       timeout=30, stdout=subprocess.DEVNULL)
        from ameesh import release_cutoff, staging_pending
        project = release_cutoff.ProjectRelease(name=options["project"], repo=repo)
        if staging_pending.regressions(project, sha, db=db):
            return blocked("candidate would roll back active content")
        queued = request
        attempt = dict(request, state="running", host=host, run=uuid.uuid4().hex,
                       started=time.time(), notify=options["role"],
                       deadline=time.time() + options.get("timeout_s", 7200))
        attempt.pop("blocked_reason", None)
        attempt.pop("previous_host", None)
        attempt.pop("previous_run", None)
        _record(db, options["key"], attempt, lease=lease)
        request = attempt
        env["STAGING_RUN_ID"] = attempt["run"]
        # An immutable worktree of the requested commit is prepared by the
        # existing release primitive, and removed only after the child ends.
        folder, tree = release_cutoff._worktree(repo, sha)
        drop = True
        try:
            argv = ["bash", str(Path(tree) / options.get(
                "script", "deploy/mise-a-jour/staging.sh")),
                    *options.get("arguments", [])]
            if attempt.get("included"):
                argv += ["--lots", " ".join(str(lot) for lot in attempt["included"])]
            argv.append(sha)
            code, lost = role_run._run_under_lease(argv, env, lease, execution)
            if lost:
                # A workstation's transient unit may outlive the bash group.
                # Keep its sources until quiescence is proved on recovery.
                drop = False
                request = attempt
                return blocked("execution lease lost")
            proofs = storage.of(db).events.recent_type(
                staging_watch.EVENT, 60, 1, object="staging-run:" + attempt["run"])
            terminal = proofs[0]["data"]["state"] if proofs else None
            if code == staging_watch.BUSY and terminal is None:
                # Refused before any host operation (shared start or local
                # flock): the request is queued again, not failed.
                request = queued
                return blocked("another staging sequence running")
            result = dict(attempt, state=terminal if code == 0 and terminal in ("completed", "partial") else "failed",
                          exit=code)
            _record(db, options["key"], result, lease=lease)
            return result
        finally:
            if drop:
                release_cutoff._drop_worktree(repo, folder, tree)
    except Exception as exc:
        # Preserve an interrupted attempt for recovery, and make pre-launch
        # errors visible now rather than waiting for its deadline.
        if 'request' in locals() and request and not execution.lost.is_set():
            _record(db, options["key"], dict(request,
                    blocked_reason="executor error: " + type(exc).__name__,
                    notify=options["role"]), lease=lease)
        raise
    finally:
        execution.close()
        lease.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tick")
    submit_p = commands.add_parser("submit")
    submit_p.add_argument("sha")
    args = parser.parse_args(argv)
    from ameesh import config, db as db_mod
    cfg = config.load()
    options = settings(args.config)
    conn = db_mod.connect(cfg)
    try:
        result = (submit(conn, cfg, options, args.sha) if args.command == "submit"
                  else tick(conn, cfg, options))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("state") in ("failed", "blocked") else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
