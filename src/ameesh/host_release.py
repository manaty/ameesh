# SPDX-License-Identifier: AGPL-3.0-only
"""#4148 (B of #3167): strict busy/draining/idle-drained transitions of a host.

The driver is read-only here: every effect on the machine belongs to an
external adapter declared by `lifecycle_hook` (host card or configuration).
ameesh calls `<command> <state> <host>` and waits for its exit code; it holds
no cluster or cloud credential.

* release: a burst host proven idle for `idle_after_s` is drained through the
  existing L232e request (claims closed first), the `draining` hook is
  acknowledged, then no turn/lease remains and `idle-drained` may be emitted.
  The adapter then releases the resource (evictable pod, stopped VM);
* return: the `busy` hook must answer before any claim reopens. After a host
  that was actually released, a *new* reading must also exist (the VM booted);
  a host that was only draining accepts a fresh post-withdrawal reading;
* runs: they come from the host measure of part A (#4147, §2.1), never from
  the hook (`busy`, `draining`, `idle-drained` only, owner decision §3.3). A
  missing or placeholder run source is unknown, never zero, and forbids both
  the automatic drain and the idle-drained proof; the scanner reports it;
* failures keep the previous acknowledged state and the closed claims, and
  raise `host_release_failed` / `host_release_overdue`.

The scanner is serialised per host (`host_release_locks`): the many runners of
a host do not each call the adapter. The expensive idle projection is spaced
by `host_release_interval`; transitions and the automatic return are checked
at every pass.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

from . import host_lifecycle, host_idle, resources, storage
from . import host_holds

#: the only states the owner decided the hook receives (§3.3)
STATES = ("busy", "draining", "idle-drained")
#: bounded text kept from a hook
DETAIL_MAX = 400
#: a run count is usable only when A published a real source
RUNS_UNKNOWN_SOURCES = ("", "aucune interface de déclaration", "inconnue")


def _bounded(text, limit=DETAIL_MAX):
    return " ".join(str(text or "").split())[:limit]


def hook_for(cfg, canons, host):
    """The adapter command of `host`, or None.

    Precedence: the per-host configuration, then the Host card, then the
    global configuration. Two different Host cards for one host are a
    configuration error: a hook is never chosen by canon order."""
    per_host = getattr(cfg, "lifecycle_hooks", None) or {}
    if isinstance(per_host, dict):
        value = per_host.get(host)
        if isinstance(value, str) and value.strip():
            return value.strip()
    cards = []
    for canon in canons or ():
        for card in getattr(canon, "hosts", ()) or ():
            if getattr(card, "title", None) != host:
                continue
            value = getattr(getattr(card, "policy", None), "lifecycle_hook", None)
            if isinstance(value, str) and value.strip():
                cards.append(value.strip())
    cards = list(dict.fromkeys(cards))
    if len(cards) > 1:
        raise ValueError("host %s declares %d different lifecycle hooks" % (host, len(cards)))
    if cards:
        return cards[0]
    global_hook = getattr(cfg, "lifecycle_hook", "") or ""
    return global_hook.strip() or None


def _argv(command):
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError("lifecycle hook is not a command line: %s" % _bounded(exc)) from None
    if not argv or any("\x00" in part for part in argv):
        raise ValueError("lifecycle hook is not a command line")
    return argv


def run_hook(cfg, command, state, host, *, generation=None, runner=None,
             timeout=None, clock=time.time):
    """Run one adapter verb. Never raises for the adapter's own failure."""
    timeout = float(getattr(cfg, "lifecycle_hook_timeout", 300.0)
                    if timeout is None else timeout)
    runner = runner or subprocess.run
    started = clock()
    result = {"state": state, "host": host, "generation": generation,
              "ok": False, "code": None, "stdout": "", "stderr": "",
              "duration_s": 0.0, "command": ""}
    try:
        argv = _argv(command)
        result["command"] = os.path.basename(argv[0])
        env = dict(os.environ)
        env.update({
            "AMEESH_HOST": host,
            "AMEESH_RELEASE_STATE": state,
            "AMEESH_LIFECYCLE_ACTION": state,
            "AMEESH_HOST_GENERATION": "" if generation is None else str(generation),
        })
        proc = runner(argv + [state, host], capture_output=True, text=True,
                      timeout=timeout, env=env)
        result.update(ok=proc.returncode == 0, code=proc.returncode,
                      stdout=_bounded(proc.stdout), stderr=_bounded(proc.stderr))
    except subprocess.TimeoutExpired:
        result["stderr"] = "timeout after %gs" % timeout
    except (OSError, ValueError) as exc:
        result["stderr"] = _bounded("%s: %s" % (type(exc).__name__, exc))
    result["duration_s"] = max(0.0, clock() - started)
    return result


def hook_error(result):
    """The bounded failure text kept in the control and the alert."""
    code = result.get("code")
    tail = result.get("stderr") or result.get("stdout") or "no output"
    return _bounded("%s %s (exit %s): %s" % (
        result.get("command") or "hook", result.get("state") or "?", code, tail))


def runs_evidence(footprint):
    """(known, count, source) of the A measurement's run field.

    #4148 guard: the adapter does not declare runs (owner decision §3.3).
    A's placeholder `runs=0` with `aucune interface de déclaration` is NOT a
    measurement of zero; until #4147 publishes a source it stays unknown."""
    footprint = footprint or {}
    source = str(footprint.get("runs_source") or "")
    runs = footprint.get("runs")
    if runs is None or source.strip() in RUNS_UNKNOWN_SOURCES \
            or source.startswith("aucune interface"):
        return False, None, source.strip() or "inconnue"
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 0:
        return False, None, source.strip()
    return True, runs, source.strip()


def _host_footprint(cfg, db, canons, host, reading, listing, controls, now):
    """One host's A projection, used only for its run declaration."""
    try:
        return host_idle.snapshot(cfg, db, canons, {host: reading or {}},
                                  now=now, listing=listing,
                                  controls=controls).get(host)
    except Exception:
        return None


def _attempt_bound(cfg):
    """How long an external adapter call is presumed alive (same as begin)."""
    return float(cfg.lifecycle_hook_timeout) + 120.0


def _fresh_reading(cfg, db, host, *, now, after=None):
    """The host's latest reading when it is fresh (and not before `after`)."""
    try:
        row = storage.of(db).hosts.latest(host)
    except Exception:
        return None
    if not row:
        return None
    sampled = resources.finite_float(row.get("sampled_ts"))
    if sampled is None or sampled > now + 5:
        return None
    if now - sampled > host_lifecycle.staleness_s(cfg.resource_interval):
        return None
    if after is not None and sampled < float(after):
        return None
    return row


def auto_drain_blocker(footprint, control, *, hook, held=None):
    """Why a burst host must not be drained automatically, or None.

    The order is intentional: an operator hold or an explicit baseline wins
    before any measurement; unknown runs are never an idle host (mesh-design's
    #4148 guard, read from A's measurement)."""
    if held:
        return "host on hold"
    if not hook:
        return "no lifecycle hook"
    if footprint is None:
        return "no footprint"
    if footprint.get("unknown"):
        return "unknown evidence: " + ", ".join(footprint["unknown"])
    if footprint.get("capacity") != "burst":
        return "host is %s" % footprint.get("capacity")
    if footprint.get("baseline_guard"):
        return "protected leader or reserve"
    if footprint.get("state") != "idle":
        return "host is %s" % footprint.get("state")
    if control is not None and control.get("mode") not in (None, "available"):
        return "host is in %s" % control.get("mode")
    known, runs, source = runs_evidence(footprint)
    if not known:
        return "declared runs unknown: " + source
    if runs > 0:
        return "%d declared run(s) (%s)" % (runs, source)
    return None


def release_blocker(control, listing, reading, *, footprint, cfg, now):
    """Why a draining host cannot yet be declared `idle-drained`, or None."""
    if listing is None:
        return "agent activity unreadable"
    known, runs, source = runs_evidence(footprint)
    if not known:
        return "declared runs unknown: " + source
    if runs > 0:
        return "%d declared run(s) (%s)" % (runs, source)
    requested = resources.finite_float(control.get("requested_ts"))
    if requested is None:
        return "withdrawal time unreadable"
    if now - requested < cfg.host_release_settle:
        return "withdrawal just started"
    if now - requested > cfg.host_release_drain_bound:
        return "drain is overdue"
    if not reading:
        return "no host reading"
    sampled = resources.finite_float(reading.get("sampled_ts"))
    if sampled is None or sampled < requested:
        return "no reading after the withdrawal"
    if now - sampled > host_lifecycle.staleness_s(cfg.resource_interval):
        return "stale host reading"
    if reading.get("turns_in_progress") != 0:
        return "a turn is still in progress"
    if any(a.get("host") == control.get("host") and a.get("lease_live")
           for a in listing or ()):
        return "a lease is still held"
    return None


def _open_hosts(controls, known_hosts, holds):
    closed = set(holds or ())
    for host, control in (controls or {}).items():
        if control.get("mode") in ("draining", "lost"):
            closed.add(host)
        elif control.get("mode") == "returning" and not control.get("claims_open"):
            closed.add(host)
    return set(known_hosts or ()) - closed


def return_targets(control, listing, controls, *, known_hosts, holds):
    """Agents whose pending work can only run on this withdrawn host.

    An elected role moves by election, never by waking a drained host. An
    agent admitted on another open host is left to the handoff/relocation
    path; waking the host for it would defeat the release."""
    if not control or control.get("mode") not in ("draining", "returning"):
        return []
    host = control.get("host")
    if host in (holds or {}):
        return []
    open_hosts = _open_hosts(controls, known_hosts, holds)
    out = []
    for row in listing or ():
        if row.get("host") != host or row.get("releve") == "election":
            continue
        if row.get("mode") not in (None, "execute") or row.get("status") == "stopped":
            continue
        work = bool(row.get("actionable_lots") or row.get("unread")
                    or row.get("has_pending_prompt")
                    or row.get("restart_requested_ts"))
        if not work:
            continue
        admitted = [h for h in (row.get("admitted_hosts") or ()) if h]
        if not admitted:
            # admitted nowhere: not a host to wake for it
            continue
        if any(h in open_hosts and h != host for h in admitted):
            continue
        out.append(row.get("name"))
    return sorted(out)


def _known_hosts(canons, listing):
    names = {row.get("host") for row in listing or () if row.get("host")}
    for canon in canons or ():
        names |= {getattr(card, "title", None) for card in getattr(canon, "hosts", ()) or ()}
    names.discard(None)
    return names


def _run_transition(cfg, api, canons, row, state, *, actor, runner=None,
                    clock=time.time):
    host, generation = row["host"], row["generation"]
    epoch = int(row.get("release_epoch") or 1)
    previous = row.get("hook_state")
    attempt = api.begin_transition(
        host, generation, state, release_epoch=epoch, actor=actor,
        stale_after=_attempt_bound(cfg),
        retry_after=float(cfg.host_release_retry))
    if attempt is None:
        return None
    command = hook_for(cfg, canons, host)
    if not command:
        return api.finish_transition(host, generation, state, release_epoch=epoch,
                                     actor=actor, detail={},
                                     error="no lifecycle hook")
    result = run_hook(cfg, command, state, host, generation=generation,
                      runner=runner, clock=clock)
    detail = {"command": result.get("command"), "code": result.get("code"),
              "duration_s": round(float(result.get("duration_s") or 0.0), 3),
              "previous_hook_state": previous,
              "stdout": result.get("stdout"), "stderr": result.get("stderr")}
    error = None if result.get("ok") else hook_error(result)
    return api.finish_transition(host, generation, state, release_epoch=epoch,
                                 actor=actor, detail=detail, error=error)


def _busy_sample_ok(cfg, api, db, row, *, now):
    """After a `busy` acknowledgement, which reading allows claims to reopen?

    The `busy` journal must be newer than *every* `idle-drained` entry of the
    host, whatever its generation or epoch: a stale hook that released the
    host after a busy acknowledgement still counts. A host that was actually
    released also needs a reading after that `busy` acknowledgement (the VM
    booted); a merely draining host requires a fresh post-ack reading too.
    A migrated `busy` (never released, no journal) is its own proof."""
    requested = resources.finite_float(row.get("requested_ts"))
    released_ts = resources.finite_float(row.get("released_ts"))
    journal = api.transition_journal(row["host"])
    busy = [item for item in journal if item.get("state") == "busy"]
    released = [item for item in journal if item.get("state") == "idle-drained"]
    latest_busy_ts = max((float(item.get("created_ts") or 0) for item in busy),
                         default=None)
    latest_release_ts = max((float(item.get("created_ts") or 0) for item in released),
                            default=None)
    legacy = (row.get("hook_state") == "busy" and released_ts is None
              and latest_busy_ts is None and latest_release_ts is None)
    if latest_release_ts is not None:
        if latest_busy_ts is None or latest_busy_ts <= latest_release_ts:
            return None
        after = latest_busy_ts
    elif released_ts is not None:
        # released but no journal (edge): the busy must postdate the release
        if latest_busy_ts is None or latest_busy_ts < released_ts:
            return None
        after = max(latest_busy_ts, released_ts)
    elif latest_busy_ts is not None:
        after = latest_busy_ts
    elif legacy:
        after = requested
    else:
        return None
    return _fresh_reading(cfg, db, row["host"], now=now, after=after)


def _advance(cfg, api, db, row, *, controls, listing, canons, holds, known,
             actor, runner, now, clock):
    """One transition decision for a controlled host. Returns a short reason."""
    host = row["host"]
    mode = row.get("mode")
    hook = hook_for(cfg, canons, host)
    if not hook:
        # No adapter: keep the historical L232e behaviour. A return opened by
        # an operator still reopens claims; nothing is released.
        if mode == "returning" and not row.get("claims_open"):
            # No adapter: a first drain (or a migrated row) has hook_state
            # NULL; there is no `busy` answer to wait for.
            api.open_claims(host, row["generation"], require_busy=False,
                            release_epoch=row.get("release_epoch"),
                            attempt_stale_after=_attempt_bound(cfg))
        return "no hook"
    if mode == "returning":
        if row.get("claims_open"):
            return "returning"
        if row.get("hook_state") == "busy":
            if _busy_sample_ok(cfg, api, db, row, now=now):
                api.open_claims(host, row["generation"],
                                release_epoch=row.get("release_epoch"),
                                attempt_stale_after=_attempt_bound(cfg))
                return "claims open"
            return "waiting for a fresh reading"
        _run_transition(cfg, api, canons, row, "busy", actor=actor,
                        runner=runner, clock=clock)
        return "busy"

    # mode == draining
    if row.get("claims_open"):
        # A hook configured after the drain (or a late canon read): close the
        # durable gate before asking the adapter for `draining`.
        if not api.close_claims(host, row["generation"], row["release_epoch"]):
            return "claims already closed"
        row = api.get(host) or row
    released = row.get("release_state") == "idle-drained"
    need_return = (return_targets(row, listing, controls, known_hosts=known,
                                  holds=holds)
                   or row.get("return_requested_ts") is not None)
    reading = _fresh_reading(cfg, db, host, now=now,
                             after=row.get("requested_ts"))
    footprint = _host_footprint(cfg, db, canons, host, reading, listing,
                                controls, now)
    runs_known, runs_count, _runs_source = runs_evidence(footprint)
    if released:
        if need_return or not runs_known or (runs_count or 0) > 0:
            _run_transition(cfg, api, canons, row, "busy", actor=actor,
                            runner=runner, clock=clock)
            return "busy"
        return "idle-drained"
    if row.get("hook_state") == "draining":
        if need_return:
            _run_transition(cfg, api, canons, row, "busy", actor=actor,
                            runner=runner, clock=clock)
            return "busy"
        blocker = release_blocker(row, listing, reading,
                                  footprint=footprint, cfg=cfg, now=now)
        if blocker is not None:
            return blocker
        _run_transition(cfg, api, canons, row, "idle-drained", actor=actor,
                        runner=runner, clock=clock)
        return "idle-drained"
    if row.get("hook_state") == "busy":
        if row.get("release_state") == "busy":
            # A busy acknowledgement for a return (idle-drained host woken for
            # work or for declared runs): reopen claims once a fresh reading
            # proves the host is back.
            if _busy_sample_ok(cfg, api, db, row, now=now):
                api.reopen_return(host, generation=row["generation"],
                                  release_epoch=row["release_epoch"], actor=actor,
                                  attempt_stale_after=_attempt_bound(cfg))
                return "returning"
            return "waiting for a fresh reading"
        # A new withdrawal: busy is where we start, draining is the next step.
        _run_transition(cfg, api, canons, row, "draining", actor=actor,
                        runner=runner, clock=clock)
        return "draining"
    if row.get("hook_state") == "idle-drained":
        # A stale release state on a host that is publishing again: restore
        # the non-evictable state before anything else.
        _run_transition(cfg, api, canons, row, "busy", actor=actor,
                        runner=runner, clock=clock)
        return "busy"
    # legacy row without an acknowledged state
    _run_transition(cfg, api, canons, row, "draining", actor=actor,
                    runner=runner, clock=clock)
    return "draining"


def _auto_drain(cfg, api, db, host, *, owner, listing, canons, holds, controls,
                actor, now, runner, clock):
    control = controls.get(host)
    if control is not None and control.get("mode") not in (None, "available"):
        api.note_release_blocked(host, owner=owner)
        return None
    if control is not None and control.get("release_state") == "idle-drained":
        # A stale release state on a host that publishes again: make it
        # non-evictable before any new withdrawal.
        api.note_release_blocked(host, owner=owner)
        _run_transition(cfg, api, canons, control, "busy", actor=actor,
                        runner=runner, clock=clock)
        return None
    if holds.get(host):
        api.note_release_blocked(host, owner=owner)
        return None
    try:
        hook = hook_for(cfg, canons, host)
    except ValueError:
        return None
    if not hook:
        api.note_release_blocked(host, owner=owner)
        return None
    reading = _fresh_reading(cfg, db, host, now=now)
    if reading is None:
        return None
    try:
        footprints = host_idle.snapshot(cfg, db, canons, {host: reading},
                                        now=now, listing=listing)
    except Exception:
        return None
    footprint = footprints.get(host)
    blocker = auto_drain_blocker(footprint, control, hook=hook,
                                 held=holds.get(host))
    if blocker is not None:
        if blocker.startswith("declared runs unknown"):
            _known, _runs, source = runs_evidence(footprint)
            api.note_release_blocked(host, owner=owner, reason=blocker,
                                     source=source)
        else:
            api.note_release_blocked(host, owner=owner)
        return None
    api.note_release_blocked(host, owner=owner)
    try:
        row = api.drain(host, actor=actor, automatic=True, close_claims=True,
                        critical=host_lifecycle.critical_names(cfg, db))
    except Exception:
        return None
    controls[host] = row
    # Emit the draining acknowledgement now; a failure keeps claims closed.
    _run_transition(cfg, api, canons, row, "draining", actor=actor,
                    runner=runner, clock=clock)
    return row


def enter_draining(cfg, db, host, *, actor, runner=None, clock=time.time):
    """Acknowledge a fresh withdrawal with the adapter (`busy` first if the
    last acknowledged state was idle-drained)."""
    api = storage.of(db).host_lifecycle
    row = api.get(host)
    if row is None or row.get("mode") not in ("draining", "returning"):
        return row
    from . import canon as canon_mod
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception:
        canons = []
    hook = hook_for(cfg, canons, host)
    if not hook:
        return row
    if row.get("hook_state") == "idle-drained":
        _run_transition(cfg, api, canons, row, "busy", actor=actor,
                        runner=runner, clock=clock)
    elif row.get("hook_state") in (None, "busy"):
        _run_transition(cfg, api, canons, row, "draining", actor=actor,
                        runner=runner, clock=clock)
    return api.get(host)


def request_return(cfg, db, host, *, actor, runner=None, now=None, clock=time.time):
    """Explicit return request: `busy` first, then claims reopen.

    Used by `ameesh host undrain`; the automatic return uses the same ordered
    steps from `once`. Returns the current control row (a busy hook may leave
    it draining until a fresh reading proves the host is back)."""
    api = storage.of(db).host_lifecycle
    row = api.get(host)
    if row is None:
        return None
    if row.get("mode") == "lost":
        raise ValueError("lost host requires verified recovery before return "
                         "(`ameesh host reconcile`); a heartbeat does not reopen it")
    if row.get("mode") == "available":
        return row
    # Persist the explicit intent BEFORE any busy attempt: an idle-drained
    # hook in flight may refuse the busy call, but the return must survive it.
    requested = api.note_return_request(host, actor=actor)
    if requested is not None:
        row = requested
    from . import canon as canon_mod
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception:
        canons = []
    hook = hook_for(cfg, canons, host)
    now = clock() if now is None else float(now)
    if not hook:
        if row.get("mode") == "returning" and not row.get("claims_open"):
            api.open_claims(host, row["generation"], require_busy=False,
                            release_epoch=row.get("release_epoch"),
                            attempt_stale_after=_attempt_bound(cfg))
        else:
            api.undrain(host, actor=actor, claims_open=True,
                        generation=row["generation"],
                        release_epoch=row.get("release_epoch"),
                        attempt_stale_after=_attempt_bound(cfg))
        return api.get(host)
    if row.get("mode") == "returning" and row.get("claims_open"):
        return row
    if row.get("hook_state") != "busy":
        _run_transition(cfg, api, canons, row, "busy", actor=actor,
                        runner=runner, clock=clock)
        row = api.get(host)
    if row is None or row.get("release_error_ts"):
        return row
    # B2: never open a claim without a valid `busy` proof. If another
    # transition (an idle-drained hook) is in flight, begin_transition returns
    # None and the row stays draining; the scanner retries after it finishes.
    if not _busy_sample_ok(cfg, api, db, row, now=now):
        return row
    api.reopen_return(host, generation=row["generation"],
                      release_epoch=row["release_epoch"], actor=actor,
                      attempt_stale_after=_attempt_bound(cfg))
    return api.get(host)


def once(cfg, db, *, local_host=None, actor="", now=None, runner=None,
         clock=time.time):
    """One release-scanner pass; never raises (the runner thread must live).

    `local_host` is the host of the calling runner: only it may start an
    automatic withdrawal (a host knows its own quiet window best). Controlled
    hosts are advanced from any runner, serialised by the per-host lock.
    """
    now = clock() if now is None else float(now)
    api = storage.of(db).host_lifecycle
    controls = {row["host"]: row for row in api.listing()}
    owners = []
    for host, control in controls.items():
        if control.get("mode") in ("draining", "returning"):
            owners.append(host)
    if local_host:
        owners.append(local_host)
    owners = sorted(set(owners))
    if not owners:
        return {"now": now, "hosts": []}
    # #4148: the release scanner runs on every agent runner, every minute.
    # The expensive work views are read only when a controlled host needs them
    # or the local idle scan is due, never on a quiet pass.
    cache = {"listing": None, "canons": None, "holds": None, "known": None}

    def _listing():
        if cache["listing"] is None:
            try:
                cache["listing"] = storage.of(db).operations.listing()
            except Exception:
                cache["listing"] = None
        return cache["listing"]

    def _canons():
        if cache["canons"] is None:
            from . import canon as canon_mod
            try:
                cache["canons"] = canon_mod.load_configured(cfg)
            except Exception:
                cache["canons"] = []
        return cache["canons"]

    def _holds():
        if cache["holds"] is None:
            try:
                cache["holds"] = host_holds.live(db)
            except Exception:
                cache["holds"] = {}
        return cache["holds"]

    def _known():
        if cache["known"] is None:
            cache["known"] = _known_hosts(_canons(), _listing()) | set(controls)
        return cache["known"]

    for host in owners:
        owner = "%s:%s" % (actor or getattr(cfg, "runner_id", "") or "ameesh", host)
        lock = api.claim_release_scan(
            host, owner=owner,
            # short enough that a stopped scanner is replaced; the transition
            # attempt (stale_after) is the real guard of a long hook.
            lock_seconds=120.0,
            scan_interval=float(cfg.host_release_interval))
        if lock is None:
            continue
        try:
            row = api.get(host)
            if row is None or row.get("mode") not in ("draining", "returning"):
                if host == local_host and lock.get("scan_due"):
                    try:
                        _auto_drain(cfg, api, db, host, owner=owner,
                                    listing=_listing(), canons=_canons(),
                                    holds=_holds(), controls=controls, actor=actor,
                                    now=now, runner=runner, clock=clock)
                    except (ValueError, OSError):
                        pass
                continue
            try:
                _advance(cfg, api, db, row, controls=controls, listing=_listing(),
                         canons=_canons(), holds=_holds(), known=_known(),
                         actor=actor, runner=runner, now=now, clock=clock)
            except (ValueError, OSError):
                # A conflicting or malformed adapter command must not stop the
                # other hosts; the failure is named by the next alert pass
                # once it is recorded on the control.
                continue
        finally:
            api.finish_release_scan(host, owner=owner)
    return {"now": now, "hosts": owners}


def alerts(cfg, db, now, *, listing=None, orchestrators=()):
    """Agent-facing warnings derived from the durable release controls."""
    from .exploitation import _alert
    api = storage.of(db).host_lifecycle
    controls = api.listing()
    if listing is None:
        try:
            listing = storage.of(db).operations.listing()
        except Exception:
            listing = []
    out = []
    for control in controls:
        host = control["host"]
        if control.get("release_error_ts"):
            since = (control.get("release_error_since_ts")
                     or control["release_error_ts"])
            detail = ("Host %s: lifecycle hook failed for %s (generation %s): %s. "
                      "Previous acknowledged state kept (%s); claims %s; no new lease "
                      "is accepted. Fix the adapter, then the scanner retries every %gs "
                      "(or `ameesh host undrain %s`)." % (
                          host, control.get("release_error_state") or "?",
                          control.get("generation"), control.get("release_error"),
                          control.get("hook_state") or "none",
                          "open" if control.get("claims_open") else "closed",
                          float(cfg.host_release_retry), host))
            out.append(_alert("host_release_failed", None, since,
                              max(0.0, now - since), 0.0, detail,
                              host=host, state=control.get("release_error_state"),
                              generation=control.get("generation"),
                              orchestrators=list(orchestrators)))
            continue
        if control.get("mode") != "draining" \
                or control.get("release_state") == "idle-drained":
            continue
        requested = resources.finite_float(control.get("requested_ts"))
        if requested is None or now - requested <= cfg.host_release_drain_bound:
            continue
        agents = sorted(row.get("name") for row in listing
                        if row.get("host") == host)
        leases = [row.get("name") for row in listing
                  if row.get("host") == host and row.get("lease_live")]
        detail = ("Host %s has been draining for %ds (bound %ds) without reaching "
                  "idle-drained: release state %s, hook %s, claims %s; declared runs "
                  "come from A's host measure (%s). %s Turn/lease holders: %s. Check "
                  "the adapter and the host reading, then `ameesh host undrain %s` "
                  "to give it back." % (
                      host, int(now - requested), int(cfg.host_release_drain_bound),
                      control.get("release_state"), control.get("hook_state") or "none",
                      "open" if control.get("claims_open") else "closed",
                      "unknown until #4147 publishes a run source",
                      "Agents: %s." % (", ".join(agents) or "none"),
                      ", ".join(leases) or "none", host))
        out.append(_alert("host_release_overdue", None, requested,
                          max(0.0, now - requested),
                          float(cfg.host_release_drain_bound), detail,
                          host=host, state=control.get("release_state"),
                          generation=control.get("generation"),
                          orchestrators=list(orchestrators)))
    # #4148 guard: a burst host that the A projection calls idle but whose run
    # source is unknown must not be drained. Name it once, and point at #4147.
    try:
        locks = api.release_locks()
    except Exception:
        locks = []
    fresh_s = max(3600.0, 3.0 * float(cfg.host_release_interval))
    for lock in locks:
        reason = str(lock.get("blocked_reason") or "")
        if "declared runs unknown" not in reason:
            continue
        seen = resources.finite_float(lock.get("updated_ts"))
        since = resources.finite_float(lock.get("blocked_since_ts")) or seen
        if seen is None or now - seen > fresh_s or since is None:
            continue
        host = lock["host"]
        detail = ("Host %s is idle and classified burst, but its run count is "
                  "unknown (%s): the A host measure publishes no run source yet, so "
                  "the automatic release is refused (a run would be killed). Follow "
                  "#4147: expose runs in the host measure; until then no automatic "
                  "drain of a burst host. The lifecycle adapter is configured and "
                  "not at fault." % (
                      host, lock.get("blocked_source")
                      or "aucune interface de déclaration"))
        out.append(_alert("host_release_no_runs", None, since,
                          max(0.0, now - since), 0.0, detail,
                          host=host, runs_source=lock.get("blocked_source"),
                          orchestrators=list(orchestrators)))
    return out
