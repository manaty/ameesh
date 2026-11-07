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
  the hook (`busy`, `draining`, `idle-drained` only, owner decision §3.3).
  A host declares them through `runs_interface` (Host card or the
  `runs_interfaces` configuration, #5905): `none` makes zero a known count.
  An undeclared host keeps a placeholder source that is unknown, never zero,
  and forbids both the automatic drain and the idle-drained proof; the
  scanner reports it (`host_release_no_runs`);
* failures keep the previous acknowledged state and the closed claims, and
  raise `host_release_failed` / `host_release_overdue`.
* #5932: an adapter that accepts the transition asynchronously (the VM queue
  hook exits `ADAPTER_WAIT_CODE`) is waiting, not failing. The scanner records
  `hook_wait_*`, keeps the previous acknowledged state and the closed claims,
  and raises `host_release_adapter_stalled` only when the queued worker stops
  ticking, keeps a durable poweroff intent too long, or leaves an obsolete
  intent; #6074: also at once for a halt past its deadline, an archive the
  provider does not allow, a `locked` VM or a `stopped in place` VM without
  a halt intent of the wave.

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
#: #5932: exit code of an adapter that QUEUED the transition asynchronously.
#: It is neither an acknowledgement (exit 0) nor a failure: the worker owns
#: the completion, claims stay closed and the scanner records a wait.
ADAPTER_WAIT_CODE = 75
# #6074: VM adapter request markers (`ameesh.vm_adapter`). The first ones age
# against the stall bound (an archive still `stopping` refreshes its row); the
# halt phase has its own enrolled deadline; the last ones alert at once.
ADAPTER_AGING = ("poweroff intent", "archive intent", "poweroff sent")
ADAPTER_HALTING = ("halt intent", "halt sent", "halt deadline passed",
                   "archive intent", "archive refused")
ADAPTER_IMMEDIATE = {"halt deadline passed": "halt_deadline",
                     "archive refused": "archive_refused"}
# #6080: a return blocked in standby (poweron not allowed) ages against the
# stall bound from its first refusal.
ADAPTER_POWERON_REFUSED = "poweron refused"
#: bounded text kept from a hook
DETAIL_MAX = 400
#: a run count is usable only when A published a real source
RUNS_UNKNOWN_SOURCES = ("", host_idle.RUNS_UNDECLARED, "inconnue")


def _bounded(text, limit=DETAIL_MAX):
    return " ".join(str(text or "").split())[:limit]


def _canon_read(cfg):
    """(canons, unknown): `unknown` is a configured canon that did not load.

    An empty canons list may mean "no canon" (no card can declare a hook) or
    "the canon is unreadable" (its Host card may hold the only hook). The two
    must not lead to the same decision: the second never reopens a claim.
    A duplicate canon id is skipped by `load_configured` and is NOT a failure;
    a `CanonError`, an unexpected read error or `canon.readable=False` is.
    """
    from . import canon as canon_mod
    try:
        entries = canon_mod.configured(cfg)
    except Exception:
        return [], True
    if not entries:
        return [], False
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception:
        return [], True
    if len(canons) == len(entries) and all(
            getattr(canon, "readable", True) is not False for canon in canons):
        return canons, False
    # Count the real failures entry by entry: the fast path skips a duplicate
    # id too, which is not an unreadable canon.
    out, unknown = [], False
    for index, entry in enumerate(entries):
        try:
            canon = (canon_mod.from_config(cfg) if index == 0
                     else canon_mod.from_config(cfg, entry=entry))
        except canon_mod.CanonError:
            unknown = True
            continue
        except Exception:
            unknown = True
            continue
        if getattr(canon, "readable", True) is False:
            unknown = True
        if any(getattr(c, "id", None) == getattr(canon, "id", None) for c in out):
            continue
        out.append(canon)
    return out, unknown


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
    measurement of zero: it stays unknown until the host declares a
    `runs_interface` (#5905)."""
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


def release_blocker(control, listing, reading, *, footprint, cfg, now, adapter_wait=False):
    """Why a draining host cannot yet be declared `idle-drained`, or None.

    `adapter_wait` (#5925) is only for the operator's already-queued release:
    billing hysteresis may outlast the drain bound. All live safety evidence
    still applies; the native scanner keeps its original timeout.
    """
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
    if not adapter_wait and now - requested > cfg.host_release_drain_bound:
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
    if result.get("code") == ADAPTER_WAIT_CODE:
        # #5932: the adapter queued the transition; its worker will journal the
        # completion. Do not acknowledge, do not fail, and never reopen: only
        # the explicit wait is recorded. stdout/stderr stay out of the event.
        return api.wait_transition(
            host, generation, state, release_epoch=epoch, actor=actor,
            detail={"command": result.get("command"),
                    "code": result.get("code"),
                    "duration_s": detail["duration_s"],
                    "previous_hook_state": previous})
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


def _advance(cfg, api, db, row, *, controls, listing, canons, canon_unknown,
             holds, known, actor, runner, now, clock):
    """One transition decision for a controlled host. Returns a short reason."""
    host = row["host"]
    mode = row.get("mode")
    if canon_unknown:
        # A configured canon did not load: its Host card may declare the only
        # hook. Never open a claim or emit a release without knowing it.
        return "canon unreadable"
    try:
        hook = hook_for(cfg, canons, host)
    except ValueError:
        return "hook configuration ambiguous"
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
        # #5914: explicit baseline cards never lift the elected-role guard.
        footprints = host_idle.snapshot(cfg, db, canons, {host: reading},
                                        now=now, listing=listing, guards=True)
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
    canons, canon_unknown = _canon_read(cfg)
    if canon_unknown:
        return row
    try:
        hook = hook_for(cfg, canons, host)
    except ValueError:
        return row
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
    canons, canon_unknown = _canon_read(cfg)
    if canon_unknown:
        # A configured but unreadable canon may hold the only Host-card hook:
        # keep the claims closed and the return intent durable for the next
        # pass, never take the "no adapter" branch. The command fails loudly.
        raise ValueError(
            "return refused: a configured canon is unreadable, so the adapter "
            "is unknown; claims stay closed and the return intent is kept "
            "(retry after the canon loads)")
    try:
        hook = hook_for(cfg, canons, host)
    except ValueError:
        return api.get(host)
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
         clock=time.time, hosts=None):
    """One release-scanner pass; never raises (the runner thread must live).

    `local_host` is the host of the calling runner: only it may start an
    automatic withdrawal (a host knows its own quiet window best). Controlled
    hosts are advanced from any runner, serialised by the per-host lock.
    `hosts` restricts the controlled hosts advanced by this pass (the mediated
    server only scans the hosts it mediates); None keeps every one.
    """
    now = clock() if now is None else float(now)
    api = storage.of(db).host_lifecycle
    # The full control map is kept for the return targets: a host outside the
    # pass may still be closed, and it must not be read as an open host.
    controls = {row["host"]: row for row in api.listing()}
    wanted = None if hosts is None else set(hosts)
    owners = []
    for host, control in controls.items():
        if control.get("mode") in ("draining", "returning") \
                and (wanted is None or host in wanted):
            owners.append(host)
    if local_host and (wanted is None or local_host in wanted):
        owners.append(local_host)
    owners = sorted(set(owners))
    if not owners:
        return {"now": now, "hosts": []}
    # #4148: the release scanner runs on every agent runner, every minute.
    # The expensive work views are read only when a controlled host needs them
    # or the local idle scan is due, never on a quiet pass.
    cache = {"listing": None, "canons": None, "canon_unknown": False,
             "holds": None, "known": None}

    def _listing():
        if cache["listing"] is None:
            try:
                cache["listing"] = storage.of(db).operations.listing()
            except Exception:
                cache["listing"] = None
        return cache["listing"]

    def _canons():
        if cache["canons"] is None:
            cache["canons"], cache["canon_unknown"] = _canon_read(cfg)
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
                        canons = _canons()
                        if cache["canon_unknown"]:
                            continue
                        _auto_drain(cfg, api, db, host, owner=owner,
                                    listing=_listing(), canons=canons,
                                    holds=_holds(), controls=controls, actor=actor,
                                    now=now, runner=runner, clock=clock)
                    except (ValueError, OSError):
                        pass
                continue
            try:
                _advance(cfg, api, db, row, controls=controls, listing=_listing(),
                         canons=_canons(), canon_unknown=cache["canon_unknown"],
                         holds=_holds(), known=_known(),
                         actor=actor, runner=runner, now=now, clock=clock)
            except (ValueError, OSError):
                # A conflicting or malformed adapter command must not stop the
                # other hosts; the failure is named by the next alert pass
                # once it is recorded on the control.
                continue
        finally:
            api.finish_release_scan(host, owner=owner)
    return {"now": now, "hosts": owners}


def _pending_adapter_requests(db, host):
    """#5932: queued operator requests of a host, oldest first.

    Empty when the adapter is not installed (no #5925 table yet) or cannot be
    read: the release alerts must never fail because of the optional queue.
    """
    try:
        from .storage.postgres.vm_adapter import Queue
        return Queue(db).pending_all(host)
    except Exception:
        return []


def _adapter_worker_seen(db, host):
    """#5932: last operator tick of the VM adapter for a host, or None.

    The worker writes `vm_adapter_observations` on every tick; a silent
    observation is the direct evidence that the worker disappeared, even
    before a durable poweroff intent exists.
    """
    try:
        from .storage.postgres.vm_adapter import Queue
        return Queue(db).worker_seen(host)
    except Exception:
        return None


def _adapter_provider_state(db, host):
    """#6074: provider state seen by the last VM adapter tick, or None."""
    try:
        from .storage.postgres.vm_adapter import Queue
        return Queue(db).provider_state(host)
    except Exception:
        return None


def alerts(cfg, db, now, *, listing=None, orchestrators=()):
    """Agent-facing warnings derived from the durable release controls."""
    from .exploitation import _alert
    api = storage.of(db).host_lifecycle
    controls = api.listing()
    _canons, canon_unknown = _canon_read(cfg)
    if listing is None:
        try:
            listing = storage.of(db).operations.listing()
        except Exception:
            listing = []
    out = []
    for control in controls:
        host = control["host"]
        if (canon_unknown and control.get("mode") in ("draining", "returning")
                and not control.get("claims_open")):
            since = (resources.finite_float(control.get("return_requested_ts"))
                     or resources.finite_float(control.get("requested_ts"))
                     or resources.finite_float(control.get("updated_ts"))
                     or now)
            detail = ("Host %s is under a host release but a configured canon is "
                      "unreadable: its Host card may declare the lifecycle adapter, "
                      "so ameesh keeps claims closed and cannot emit busy/idle-drained. "
                      "Fix the canon (`ameesh canon check`); the return intent is kept "
                      "and the scanner retries. State: mode %s, hook %s, claims %s." % (
                          host, control.get("mode"), control.get("hook_state") or "none",
                          "open" if control.get("claims_open") else "closed"))
            out.append(_alert("host_release_canon_unreadable", None, since,
                              max(0.0, now - since), 0.0, detail,
                              host=host, mode=control.get("mode"),
                              orchestrators=list(orchestrators)))
            continue
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
        # #5932: a queued adapter request is a normal asynchronous wait, not a
        # failure. Only a durable poweroff intent that stops progressing (or a
        # queued request whose wave no longer matches the control) names the
        # operator worker as stalled. Claims stay closed in every case.
        generation = control.get("generation")
        epoch = control.get("release_epoch")
        # A request is queued only while the gate is closed and the host is
        # under release (`Queue.request` enforces it). Once a return has
        # reopened the gate, an older pending row is superseded by definition:
        # the worker settles it on its next tick, and it must not raise a
        # permanent "obsolete" alert while the host is serving again
        # (auditeur B1 on #5932).
        gate_closed = (control.get("mode") in ("draining", "returning")
                       and not control.get("claims_open"))
        pending = _pending_adapter_requests(db, host) if gate_closed else []
        live = [row for row in pending
                if row.get("generation") == generation
                and row.get("release_epoch") == epoch]
        live_keys = {(row.get("generation"), row.get("release_epoch"))
                     for row in live}
        obsolete = [row for row in pending
                    if (row.get("generation"), row.get("release_epoch"))
                    not in live_keys]
        stall = float(cfg.host_release_adapter_stall)

        def _age(row):
            since = resources.finite_float(row.get("updated_ts"))
            return None if since is None else max(0.0, now - since)

        stalled = None
        reason = None
        worker_seen = _adapter_worker_seen(db, host) if live else None
        if live:
            # A queued request is a normal asynchronous wait. Name the worker
            # only when it stopped progressing: a durable poweroff intent older
            # than the bound, or no worker tick for that long.
            poweroff = next((row for row in live
                             if row.get("result") in ADAPTER_AGING), None)
            oldest = live[0]
            immediate = next((row for row in live
                              if row.get("result") in ADAPTER_IMMEDIATE), None)
            refused = next((row for row in live
                            if row.get("result") == ADAPTER_POWERON_REFUSED), None)
            provider = _adapter_provider_state(db, host)
            if immediate is not None:
                stalled, reason = immediate, ADAPTER_IMMEDIATE[immediate["result"]]
            elif provider == "locked":
                stalled, reason = oldest, "provider_locked"
            elif refused is not None and (_age(refused) or 0.0) > stall:
                stalled, reason = refused, "poweron_refused"
            elif provider == "stopped in place" and not any(
                    row.get("state") == "busy"
                    or row.get("result") in ADAPTER_HALTING + ("poweroff sent",)
                    for row in live):
                # Standby without our halt intent: still billed, never
                # archived automatically (#6074).
                stalled, reason = oldest, "stopped_in_place"
            elif poweroff is not None and (_age(poweroff) or 0.0) > stall:
                stalled, reason = poweroff, "poweroff"
            elif ((worker_seen is None or now - worker_seen > stall)
                    and (_age(oldest) or 0.0) > stall):
                stalled, reason = oldest, "worker"
        else:
            stalled = next((row for row in obsolete
                            if (_age(row) or 0.0) > stall), None)
            reason = "obsolete"
        if stalled is not None:
            since = resources.finite_float(stalled.get("updated_ts")) or now
            if reason == "obsolete":
                detail = ("Host %s: the VM adapter still has a queued %s request "
                          "for generation %s/epoch %s, %ds old, while the live "
                          "control is generation %s/epoch %s: the intent is "
                          "obsolete and the operator worker has not reconciled it. "
                          "Claims %s; the acknowledged state (%s) is kept. Check "
                          "the worker timer/journal (`systemctl status "
                          "ameesh-vm-adapter.timer`, `journalctl -u "
                          "ameesh-vm-adapter`); do not clear the hook while a VM "
                          "may still be stopped." % (
                              host, stalled.get("state"),
                              stalled.get("generation"), stalled.get("release_epoch"),
                              int(_age(stalled) or 0.0), generation, epoch,
                              "open" if control.get("claims_open") else "closed",
                              control.get("hook_state") or "none"))
            elif reason in ("halt_deadline", "archive_refused",
                            "provider_locked", "stopped_in_place",
                            "poweron_refused"):
                what = {
                    "halt_deadline": "the OS halt was not proven before the "
                                     "enrolled halt deadline (provider not `stopped "
                                     "in place`, or SSH still answers); no provider "
                                     "poweroff is sent",
                    "archive_refused": "the OS halt is proven but the server "
                                       "GET does not allow `poweroff` "
                                       "(allowed_actions); no archive is sent and "
                                       "the VM stays in standby, still billed",
                    "provider_locked": "the provider reports the VM `locked`",
                    "stopped_in_place": "the provider reports the VM `stopped in "
                                        "place` (standby, still billed) without a "
                                        "halt intent of this wave; it is never "
                                        "archived automatically",
                    "poweron_refused": "a return found the VM `stopped in "
                                       "place` (standby, still billed) and the "
                                       "server GET has not allowed `poweron` "
                                       "(allowed_actions) for longer than the "
                                       "stall bound; the VM is not started and "
                                       "claims are not reopened",
                }[reason]
                detail = ("Host %s, generation %s/epoch %s: %s. Claims %s and the "
                          "acknowledged state (%s) are kept; the host is not "
                          "lost. Check the VM and the worker journal "
                          "(`journalctl -u ameesh-vm-adapter`); a return "
                          "request powers a standby VM back on." % (
                              host, stalled.get("generation"),
                              stalled.get("release_epoch"), what,
                              "open" if control.get("claims_open") else "closed",
                              control.get("hook_state") or "none"))
            elif reason == "worker":
                seen = ("never been seen" if worker_seen is None
                        else "not been seen for %ds" % int(max(0.0, now - worker_seen)))
                detail = ("Host %s: the VM adapter queued %s %ds ago but its "
                          "operator worker has %s (stall bound %ds): the request "
                          "is not progressing. Claims %s and the acknowledged "
                          "state (%s) are kept. A deliberate host hold also "
                          "pauses the adapter: check `ameesh hosts %s` before "
                          "acting. Otherwise check the worker timer/journal "
                          "(`systemctl status ameesh-vm-adapter.timer`, "
                          "`journalctl -u ameesh-vm-adapter`); do not clear the "
                          "hook while a VM may be stopped." % (
                              host, stalled.get("state"),
                              int(_age(stalled) or 0.0), seen, int(stall),
                              "open" if control.get("claims_open") else "closed",
                              control.get("hook_state") or "none", host))
            else:
                detail = ("Host %s: the VM adapter queued idle-drained with a "
                          "durable poweroff intent %ds ago (stall bound %ds). The VM "
                          "may be stopped or archiving; claims %s and the "
                          "acknowledged state (%s) are kept. Check the worker "
                          "timer/journal (`systemctl status "
                          "ameesh-vm-adapter.timer`, `journalctl -u "
                          "ameesh-vm-adapter`) and the provider state; do not "
                          "clear the hook while the VM is stopped." % (
                              host, int(_age(stalled) or 0.0), int(stall),
                              "open" if control.get("claims_open") else "closed",
                              control.get("hook_state") or "none"))
            out.append(_alert("host_release_adapter_stalled", None, since,
                              max(0.0, now - since), stall, detail,
                              host=host, state=stalled.get("state"),
                              generation=stalled.get("generation"),
                              release_epoch=stalled.get("release_epoch"),
                              reason=reason,
                              orchestrators=list(orchestrators)))
            continue
        if live:
            # Normal wait: the operator worker owns the completion. The native
            # drain bound must not be reported as a stuck drain while it ticks.
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
        # #5866: a manual drain still inside its `--for` is an announced
        # operator window; following the usual advice would end it mid-way.
        return_ts = resources.finite_float(control.get("return_ts"))
        if not control.get("automatic") and return_ts is not None and return_ts > now:
            action = ("This is a manual drain by %s until %s: an operator window, "
                      "do not undrain it; it returns on its own at that time, or "
                      "its requester undrains it." % (
                          control.get("actor") or "?",
                          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(return_ts))))
        else:
            action = ("Check the adapter and the host reading, then `ameesh host "
                      "undrain %s` to give it back." % host)
        detail = ("Host %s has been draining for %ds (bound %ds) without reaching "
                  "idle-drained: release state %s, hook %s, claims %s; declared runs "
                  "come from A's host measure (%s). %s Turn/lease holders: %s. %s" % (
                      host, int(now - requested), int(cfg.host_release_drain_bound),
                      control.get("release_state"), control.get("hook_state") or "none",
                      "open" if control.get("claims_open") else "closed",
                      "unknown unless the host declares runs_interface",
                      "Agents: %s." % (", ".join(agents) or "none"),
                      ", ".join(leases) or "none", action))
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
                  "unknown (%s): the host declares no runs_interface, so the "
                  "automatic release is refused (a run would be killed). Declare "
                  "it on the Host card (`policy.runs_interface: none`) or in the "
                  "configuration (`runs_interfaces`) once nothing runs there "
                  "outside turns and leases (#5905, #4147); until then no "
                  "automatic drain of a burst host. The lifecycle adapter is "
                  "configured and not at fault." % (
                      host, lock.get("blocked_source")
                      or host_idle.RUNS_UNDECLARED))
        out.append(_alert("host_release_no_runs", None, since,
                          max(0.0, now - since), 0.0, detail,
                          host=host, runs_source=lock.get("blocked_source"),
                          orchestrators=list(orchestrators)))
    return out
