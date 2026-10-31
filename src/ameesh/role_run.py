# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP5) — ``ameesh role run``: a mechanical command under a role's lease.

Study ``docs/design/etudes/election-pilote.md`` §6. The packet composer, the
GitHub watch and the CI watches are code; their timers are installed on every
candidate host, and only the host that holds the role's agent lease may run
them. This module is that gate, and nothing more:

* it **never claims** a lease. The lease belongs to the agent's runner (EP3);
  a timer that claimed would take the turn of a governance agent. A host that
  does not hold a live lease is on standby: exit 0, no effect;
* it reads the lease with the database clock, and asks for the *host of the
  current holder* and, when given, the **epoch**. The epoch is passed to the
  command (``AMEESH_LEASE_EPOCH``) so that an effect outside the database can
  be fenced by EP4 in the same transaction as its write;
* it is only the **entry** check, so a lease may change between this read and
  the command's gesture. That is intended: the command receives the role, its
  lease owner and its **epoch** (``AMEESH_ROLE``, ``AMEESH_LEASE_EPOCH``), and
  every gesture outside the database calls EP4's unique transaction guard
  (``pilot_gesture``), which compares the epoch and renews under the same
  owner. This module never writes a lease, an epoch or an event;
* the command carries an explicit actor, the **role** (``AMEESH_ROLE``), with
  its lease **epoch** (``AMEESH_LEASE_EPOCH``); the agent identity is removed.
  EP4's guard reads that pair and refuses a role without an epoch, so a timer
  can never pass as a process without identity. ``AMEESH_TURN_ID`` is removed:
  a timer is not an agent turn.

#3568: ``--pass alerts`` delegates ONE pass independently of the lease. The
alert delivery must not go silent because the elected lease fell to a host
without a push route (#3383). The wrapper reads the published candidate
standings and asks :func:`election.alerts_delegate` who runs the pass: the
holder when it can push, otherwise the first candidate that proves a remote
route. A non-delegate stands by (exit 0); the delegate runs the pass **without
the holder's epoch**, since it is not the lease holder and the alerts pass
writes no fenced pilot gesture (local notify state and the shared liveness
beat only). The other passes keep the lease-only gate.

#3568 (review cx3): the delegation read does not reserve execution. A holder
whose route returns selects itself while a delegate's child is still running,
and the notify state lock is local to one host. The alerts pass therefore
holds a **shared execution lease** in `merge_locks` (key
``alerts-pass:<role>``, as the train service borrows that table) for the whole
life of its child, re-reads the selection under it, and renews it every third
of its TTL; a lost lease stops the child instead of letting a second delivery
overlap. The renewal is a network call that can block past the database
expiry, so the lease also carries a **local monotonic deadline**, fixed before
acquisition and extended only by a successful renewal; a watchdog that never
touches the database TERMs then KILLs the child on that deadline, before
another host can take the execution lease. The other passes never take it.

#3575: the stop is a **process-group** stop. The child leads its own session
and that pgid is kept for life; after the grace a descendant that ignores
SIGTERM is KILLed even when the leader already exited or was reaped. A lost
lease therefore never leaves an auxiliary of the pass alive.

The command is an argument vector, never a shell string. Its exit code is
returned unchanged. The wrapper's own refusals are:

* ``0`` — standby (another host holds the lease, the lease is expired, the
  role is stopped, the remaining lease is below ``--min-left``, the alerts
  pass is delegated to another candidate, or another alerts child holds the
  shared execution lease): the timer did nothing, as expected on a standby
  host;
* ``1`` — the lease could not be read (database unavailable): the timer must
  be noticed, never silently considered standby;
* ``2`` — the invocation is invalid (unknown role, no host, no command).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import migrations
from . import alerts_delegation_pg
from . import config as config_mod
from . import db as db_mod

#: environment given to the command. The epoch is the fencing token of EP4.
ENV_ROLE = "AMEESH_ROLE"
ENV_HOST = "AMEESH_ROLE_HOST"
ENV_PASS = "AMEESH_ROLE_PASS"
ENV_EPOCH = "AMEESH_LEASE_EPOCH"
ENV_LEFT = "AMEESH_LEASE_LEFT"
ENV_OWNER = "AMEESH_LEASE_OWNER"

#: #3568: the SHARED execution lease of the alerts pass, one key per role in
#: `merge_locks` (the train service already borrows that table). Selecting the
#: delegate does not reserve execution: a holder whose route returns chooses
#: itself while a delegate's child is still running, and the notify state lock
#: is local to one host. The lease makes at most one alerts child run at a
#: time; its owner carries a per-process token (two processes on one host also
#: contend) and its TTL is renewed while the child runs.
ALERTS_LEASE_KEY = "alerts-pass:%s"
#: long enough for a renewal to survive one slow database round-trip, short
#: enough that a killed wrapper frees the pass within a minute
ALERTS_LEASE_TTL_S = 60.0
#: #3568 (review cx3): the database lease is renewed over the network, and a
#: suspended network can make that call block past the server expiry. The
#: child is therefore stopped on a LOCAL monotonic deadline, fixed at
#: acquisition and extended only by a successful renewal. The guard is the
#: margin kept before the database expiry: it must cover the TERM/KILL
#: sequence plus scheduling, even when the renewal never returns. With the
#: defaults (TTL 60 s, renewal every 20 s, client timeout 35 s), the first
#: renewal starts at 20 s and may answer at 55 s; the local deadline is 45 s,
#: so the child is dead before 47 s and the server lease cannot be taken
#: before 60 s.
ALERTS_LEASE_GUARD_S = 15.0
#: delay between SIGTERM and SIGKILL when the local deadline is reached
ALERTS_LEASE_KILL_GRACE_S = 2.0

#: a role the wrapper may run; its name is an agent name (aliases resolved).
_SCHEMA = "ameesh-role-run/1"

#: the row read by the gate. The database clock is read in the same statement:
#: a host clock that drifts cannot make a dead lease look alive.
_READ_SQL = """
    SELECT name, host, releve, candidats, status, responsible,
           lease_owner, lease_epoch,
           extract(epoch from lease_since)::float8    AS lease_since_ts,
           extract(epoch from lease_expires_at)::float8 AS lease_expires_ts,
           extract(epoch from clock_timestamp())::float8  AS now_ts
      FROM agent_registry
     WHERE name = (SELECT ameesh_resolve_agent_name(%s, %s))
"""


class RoleError(ValueError):
    """The role cannot be read, or the invocation is invalid."""


class _AlertsLease:
    """#3568: the shared execution lease of one role's alerts pass.

    The delegation read chooses *who should* run; this lease decides *who is
    running*. It lives in `merge_locks` under a key of its own, as the train
    service does, because that table already offers the cross-host CAS and
    the TTL the earlier host leases use. Acquisition bumps the epoch; renewal
    is fenced by the owner token and that epoch, so a lease taken over after
    an expiry cannot be renewed by the old process. A killed wrapper frees the
    pass when the TTL ends; a live one renews it while its child runs.

    The database expiry is not enough on its own: the renewal is a network
    call and can block past that expiry. The lease therefore also keeps a
    **local monotonic deadline**, fixed *before* the acquisition call and
    extended only by a renewal that succeeded. A watchdog stops the child on
    that deadline without issuing any database call (review cx3).
    """

    def __init__(self, db, role: str, host: str, *, ttl: float | None = None):
        self.db = db
        self.key = ALERTS_LEASE_KEY % (role or "")
        self.owner = "alerts-pass:%s:%s" % (host, uuid.uuid4().hex)
        self.ttl = float(ALERTS_LEASE_TTL_S if ttl is None else ttl)
        #: margin kept between the local stop and the database expiry; it
        #: must cover TERM + KILL and scheduling, not just one round-trip.
        #: The 0.15 s floor keeps a deliberately tiny TTL (the review probe
        #: uses 0.3 s) from leaving less time than a scheduler jitter.
        self.guard = min(self.ttl * 0.9,
                         max(0.15, min(ALERTS_LEASE_GUARD_S, self.ttl / 3.0)))
        self.kill_grace = max(0.02, min(ALERTS_LEASE_KILL_GRACE_S,
                                        self.guard / 3.0))
        self.epoch = None
        #: monotonic time at which the child must already be stopped; None
        #: when this process holds no lease
        self.deadline = None
        self.lock = threading.Lock()

    def _work(self):
        from . import storage
        return storage.of(self.db).work

    def acquire(self) -> int | None:
        """Take the lease, or return None when another child holds it."""
        # The server sets its expiry during the call, before the response can
        # be delayed by the network: starting from the local time *before* the
        # call keeps the local deadline conservative in every case.
        started = time.monotonic()
        epoch = self._work().land_lock(self.key, self.owner, self.ttl)
        with self.lock:
            self.epoch = None if epoch is None else int(epoch)
            self.deadline = (started + self.ttl - self.guard
                             if self.epoch is not None else None)
        return self.epoch

    @migrations.writer_tick
    def renew(self) -> bool:
        """Extend the lease we hold; False when it expired or was taken."""
        with self.lock:
            epoch = self.epoch
        if epoch is None:
            return False
        # A slow renewal extends the server expiry from when its statement
        # ran; measuring from before the call only makes the local deadline
        # earlier, never later.
        started = time.monotonic()
        try:
            got = self._work().land_lock(self.key, self.owner, self.ttl,
                                         epoch=epoch)
        except Exception:  # noqa: BLE001 - a broken renewal is a lost lease
            got = None
        if got != epoch:
            return False
        with self.lock:
            if self.epoch != epoch:
                return False
            proposed = started + self.ttl - self.guard
            if self.deadline is None or proposed > self.deadline:
                self.deadline = proposed
            # The call may itself have outlived the local deadline: in that
            # case the watchdog is already stopping the child, and reporting
            # a lost lease lets the caller fail closed without waiting.
            return self.deadline > time.monotonic()

    def note_renewed(self, epoch: int, started: float) -> bool:
        """Extend the local deadline after a CAS renewal made by the caller.

        `_record` renews the shared lease inside its own transaction, to check
        and journal under the same lock. Without this, the database expiry
        would move but the monotonic deadline would not, and a slow
        preparation could be reported as a lost lease (#4898 N1). `started`
        is the monotonic instant taken before the caller's renewal call, so
        the local deadline never moves later than the server's.
        """
        with self.lock:
            if self.epoch != epoch:
                return False
            proposed = started + self.ttl - self.guard
            if self.deadline is None or proposed > self.deadline:
                self.deadline = proposed
            return self.deadline > time.monotonic()

    def remaining(self) -> float:
        """Seconds left on the local deadline; negative when it has passed."""
        with self.lock:
            deadline = self.deadline
        if deadline is None:
            return -1.0
        return deadline - time.monotonic()

    def release(self) -> bool:
        """Release this process's lease; never another holder's."""
        with self.lock:
            epoch, self.epoch = self.epoch, None
            self.deadline = None
        if epoch is None:
            return False
        try:
            return bool(self._work().land_unlock(self.key, self.owner, epoch))
        except Exception:  # noqa: BLE001 - the TTL still frees the pass
            return False


def _signal_child_group(proc, sig: int, *, pgid: int | None = None) -> None:
    """Signal the child's whole session, not only its own process.

    #3575: the pgid is the one `start_new_session` created (the child's own
    pid), never re-read with ``os.getpgid(proc.pid)``: once the leader has
    been reaped that call raises, while the group of its descendants still
    exists. `proc.pid` remains the group id as long as a member lives.
    """
    group = int(proc.pid if pgid is None else pgid)
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _group_alive(pgid: int) -> bool:
    """Does any process still belong to this process group?

    Signal 0 asks the kernel without delivering anything. A zombie leader
    counts until it is reaped, which is why the callers reap it with
    ``proc.poll()`` before concluding.
    """
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stop_child(proc, grace: float, *, pgid: int | None = None) -> None:
    """TERM the child's group, then KILL it if it is still alive after ``grace``.

    The deadline watchdog owns the process reaping (the main thread is in
    ``proc.wait()``); this only signals and escalates, so a child that ignores
    SIGTERM cannot outlive the local deadline that precedes the lease expiry.

    #3575: the verdict is the *group's* liveness, never the leader's exit
    status. A leader that exits on TERM while a descendant ignores it must
    still receive the SIGKILL escalation; the group id created by
    ``start_new_session`` is used directly because it stays valid after the
    leader is gone (review cx3 and dev2).
    """
    group = int(proc.pid if pgid is None else pgid)
    # Reap the leader if it already exited: its zombie would otherwise hold
    # the group alive for the whole grace, without hiding a live descendant.
    proc.poll()
    _signal_child_group(proc, signal.SIGTERM, pgid=group)
    deadline = time.monotonic() + max(0.0, grace)
    while True:
        proc.poll()
        if not _group_alive(group):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    # The group still has a member after the grace, whether or not the leader
    # is alive: escalate on the group, not on `proc.poll()`.
    _signal_child_group(proc, signal.SIGKILL, pgid=group)
    # SIGKILL is synchronous, but the kernel still schedules the death: wait
    # a bounded moment so the lease is not released under a live descendant.
    limit = time.monotonic() + min(max(0.0, grace), 0.2)
    while time.monotonic() < limit:
        proc.poll()
        if not _group_alive(group):
            return
        time.sleep(0.01)


class _AlertsExecution:
    """The local, database-free stop of one alerts child.

    The shared lease is renewed over the network, and that call can block past
    the server expiry. This guard watches the lease's monotonic deadline in a
    thread that never touches the database, so it stops the child *before*
    another host can take the execution lease, even if no renewal ever
    returns. It is armed just after acquisition (before the selection
    re-reads) and its lock closes the race between the last deadline check
    and the child registration.
    """

    def __init__(self, lease: _AlertsLease):
        self.lease = lease
        self.lock = threading.Lock()
        self.proc = None
        #: #3575: the process group created by start_new_session (== child pid)
        self.pgid = None
        self.lost = threading.Event()
        self.stopped = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._watch,
                                       name="alerts-deadline", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=max(1.0, self.lease.kill_grace + 1.0))

    def _watch(self) -> None:
        # No database call here: a pending renewal must not delay the stop.
        while not self.stop.is_set():
            left = self.lease.remaining()
            if left <= 0:
                self.kill()
                return
            self.stop.wait(min(0.2, max(0.01, left)))

    def start_child(self, argv: list[str], env: dict):
        """Register and start the child, or None if the deadline has passed."""
        with self.lock:
            if self.lost.is_set() or self.lease.remaining() <= 0:
                self.lost.set()
                return None
            # Its own session lets the watchdog stop the command's children
            # too: a wrapper that leaves a helper behind is still one pass.
            # #3575: keep that pgid (== pid with start_new_session), so a
            # descendant can be signalled after the leader is reaped.
            self.proc = subprocess.Popen(argv, env=env, start_new_session=True)
            self.pgid = self.proc.pid
            return self.proc

    def kill(self) -> None:
        """Stop the child once; any caller may signal a lost lease."""
        with self.lock:
            self.lost.set()
            if self.stopped.is_set():
                return
            self.stopped.set()
            proc = self.proc
            pgid = self.pgid
        if proc is not None:
            _stop_child(proc, self.lease.kill_grace, pgid=pgid)


def _watch_alerts_lease(lease: _AlertsLease, execution: _AlertsExecution,
                        stop: threading.Event) -> None:
    """Renew the shared lease for the guarded span (child, or a slower caller).

    A failed renewal stops the child at once; the deadline watchdog is the
    second, local line of defence when the renewal call itself is blocked.
    """
    while not stop.wait(max(0.1, lease.ttl / 3.0)):
        if stop.is_set():
            return
        try:
            renewed = lease.renew()
        except db_mod.DbError:
            renewed = False
        if renewed:
            continue
        execution.kill()
        return


def _run_under_lease(argv: list[str], env: dict, lease: _AlertsLease,
                     execution: _AlertsExecution, *, watch: bool = True) -> tuple[int, bool]:
    """Run the child while the shared lease is held and renewed.

    Returns ``(exit code, lost)``. The caller releases the lease after closing
    the execution guard, so no stop thread can use the connection while it is
    released. The local deadline is already armed by the caller: it covers the
    selection re-reads that happen before this function starts the child.

    ``watch=False`` lets a caller that already renews the lease for a wider
    span (a pre-launch phase of its own) keep ONE renewal thread, instead of
    racing two renewals over the same epoch (#4898 N1).
    """
    proc = execution.start_child(argv, env)
    if proc is None:
        return 1, True
    stop = threading.Event()
    watcher = None
    if watch:
        watcher = threading.Thread(target=_watch_alerts_lease,
                                   args=(lease, execution, stop),
                                   name="alerts-lease", daemon=True)
        watcher.start()
    try:
        code = int(proc.wait())
    finally:
        stop.set()
        # No timeout: releasing while the watcher still uses the connection
        # would race the driver. The child is already stopped by the local
        # deadline even if this renewal never returns; the TTL then frees the
        # pass for the other hosts.
        if watcher is not None:
            watcher.join()
    return code, execution.lost.is_set()


def _read(db, name: str) -> dict | None:
    """One role row, with the database clock, without taking a lock.

    This is the timer's entry check, not the fencing of a pilot gesture: EP4
    owns that guard (`pilot_gesture`), which renews and writes the epoch in
    the transaction of the effect. Here a stale read only means the timer
    starts or waits; no effect is written by the wrapper itself.
    """
    if not isinstance(name, str) or not name.strip():
        raise RoleError("a non-empty agent name is required")
    rows = db.query(_READ_SQL, (name, "role-run"))
    return rows[0] if rows else None


def _report(row: dict, *, epoch: int | None = None) -> dict:
    expires = row.get("lease_expires_ts")
    now = row.get("now_ts")
    left = None if expires is None or now is None else float(expires) - float(now)
    stopped = str(row.get("status") or "") == "stopped"
    held = bool(row.get("lease_owner")) and expires is not None \
        and now is not None and float(expires) > float(now) and not stopped
    report = {
        "schema": _SCHEMA,
        "role": row["name"],
        "host": row.get("host"),
        "releve": row.get("releve"),
        "candidates": list(row.get("candidats") or ()),
        # #3575: the delegate hysteresis prefers the role's last executor and
        # needs the same required human as the election read.
        "responsible": row.get("responsible"),
        "holder": row.get("host") if held else None,
        "owner": row.get("lease_owner"),
        "epoch": row.get("lease_epoch"),
        "lease_since_ts": row.get("lease_since_ts"),
        "expires_ts": expires,
        "left_s": None if left is None else round(left, 3),
        "stopped": stopped,
        "held": held,
    }
    if epoch is not None:
        report["expected_epoch"] = int(epoch)
        if not held:
            report["code"] = "no-live-lease"
        elif row.get("lease_epoch") is None \
                or int(row["lease_epoch"]) != int(epoch):
            report["code"] = "stale-epoch"
        else:
            report["code"] = ""
    return report


def live(db, name: str, *, epoch: int | None = None) -> dict:
    """The observable lease of ``name``, without taking any lock.

    ``held`` is true only for a live lease of a non-stopped role. The
    ``epoch`` argument is an optional fencing token: when given, the report is
    refused as ``stale-epoch`` if it does not match. A role with no row raises
    :class:`RoleError`; a database error propagates (the caller must not
    conclude that a role is free).
    """
    row = _read(db, name)
    if row is None:
        raise RoleError("unknown role (no agent_registry row): %s" % name)
    return _report(row, epoch=epoch)


def holder(db, name: str, host: str, *, epoch: int | None = None) -> bool:
    """Does ``host`` hold a live lease of ``name`` (and ``epoch`` when given)?

    Never raises on a missing row or an unknown role: a train passage or a
    timer treats every unreadable role as "not the holder". A database error
    still propagates — the caller must not mistake a broken database for a
    free role.
    """
    if not host:
        return False
    try:
        report = live(db, name, epoch=epoch)
    except RoleError:
        return False
    return bool(report["held"] and report["host"] == host
                and not report.get("code"))


def role_host_authorized(cfg) -> bool:
    """Is this process a timer of the role on the host it designates?

    `role run` sets both variables for the command it launches, after checking
    the live lease. The host-designated passes (`github.host`) accept this
    host when the pair matches theirs: the role gate has already decided who
    runs, so the static host pin must not silence the elected host's timer.
    A bare `AMEESH_ROLE`, without the host of the same process, changes
    nothing (fail closed).
    """
    role = (os.environ.get(ENV_ROLE) or "").strip()
    host = (os.environ.get(ENV_HOST) or "").strip()
    here = (getattr(cfg, "host", None) or "").strip()
    # #3568: a delegated pass carries the role without the lease epoch. It
    # must not open the host-designated GitHub/CI passes that the lease gate
    # authorizes: only the lease holder's timer does (`AMEESH_ROLE_PASS` is
    # set for the delegated `alerts` pass and for a holder's `--pass alerts`).
    if (os.environ.get(ENV_PASS) or "").strip():
        return False
    return bool(role) and bool(host) and bool(here) and host == here


def host_authorized(cfg, designated: str | None) -> bool:
    """May this process run a pass pinned to ``github.host``?

    Historical behaviour without a role timer: only the designated host. A
    role timer accepts its own host, because the lease already chose it. An
    empty designation is not a match: the pass still requires the role timer.
    """
    here = (getattr(cfg, "host", None) or "").strip()
    if designated and designated == here:
        return True
    return role_host_authorized(cfg)


def _detail(code: str, report: dict) -> str:
    if code == "no-live-lease":
        return "no live lease (owner %s, expires %s)" % (
            report.get("owner") or "none", report.get("expires_ts"))
    if code == "stale-epoch":
        return "current epoch %s, expected %s" % (
            report.get("epoch"), report.get("expected_epoch"))
    if code == "role-stopped":
        return "role is stopped"
    if code == "lease-too-short":
        return "%s left" % report.get("left_s")
    if code == "alerts-delegated":
        return "alerts pass delegated to %s (%s)" % (
            report.get("delegate"), report.get("delegation") or "another candidate")
    if code == "no-alerts-delegate":
        return "no candidate proves a remote push route (%s)" % (
            report.get("delegation") or "no route")
    if code == "alerts-busy":
        return ("another alerts pass holds the shared execution lease (%s)"
                % (ALERTS_LEASE_KEY % (report.get("role") or "")))
    return code


def _alerts_delegation(db, report: dict, *, host: str) -> dict:
    """#3568: the alerts-pass delegate read from the live role row.

    The wrapper stays the entry gate only: this read chooses *which host* runs
    the `alerts` pass, it writes nothing. `report` is the fresh `live()` row
    of the role.
    """
    from . import election
    role = {"name": report.get("role"), "releve": report.get("releve"),
            "candidats": list(report.get("candidates") or ()),
            "responsible": report.get("responsible"),
            # #4468: the holder's current tenure bounds its no-beat grace.
            "lease_since_ts": report.get("lease_since_ts")}
    decision = election.alerts_delegate(db, role, holder=report.get("holder"))
    decision["this_host"] = host
    return decision


def _record(report: dict, *, host: str, argv: list[str] | None = None,
            result: str, code: str = "", detail: str = "") -> dict:
    out = dict(report)
    out.update(result=result, code=code, detail=detail, this_host=host)
    if argv is not None:
        out["command"] = list(argv)
    return out


class _ReleaseLease(_AlertsLease):
    """One elected release tick, including packet-watch, across candidates.

    The existing local deadline kills the process group even if the role or
    execution renewal blocks. A changed role stops renewing this execution.
    """
    def __init__(self, db, role, host, epoch):
        super().__init__(db, role, host)
        self.key = "release-pass:" + role
        self.role, self.host, self.role_epoch = role, host, epoch

    def renew(self):
        current = live(self.db, self.role)
        if (not current["held"] or current["stopped"]
                or current["host"] != self.host
                or current["epoch"] != self.role_epoch):
            return False
        return super().renew()


def run(cfg, args, *, credentials=()) -> int:
    """The `ameesh role run ROLE -- command` entry point."""
    name = args.role
    host = (getattr(cfg, "host", None) or "").strip()
    pass_name = str(getattr(args, "pass_name", "") or "").strip()
    argv = list(args.command)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("ameesh role run: a command is required after `--`", file=sys.stderr)
        return 2
    if not host:
        print("ameesh role run: this host has no name (AMEESH_HOST or config host)",
              file=sys.stderr)
        return 2
    if pass_name and pass_name not in ("alerts", "release"):
        print("ameesh role run: unknown delegated pass %r (only: alerts, release)" % pass_name,
              file=sys.stderr)
        return 2

    def emit(report, *, result, stream=sys.stdout):
        if args.json:
            print(json.dumps(_record(report, host=host, argv=argv, result=result,
                                     code=report.get("code") or "",
                                     detail=report.get("detail") or ""),
                             ensure_ascii=False, sort_keys=True), file=stream)
        else:
            print("role %s on %s: %s%s%s" % (
                report.get("role") or name, host, result,
                " (alerts delegated)" if report.get("delegated") else "",
                " (%s)" % (report.get("detail") or report.get("code"))
                if (report.get("detail") or report.get("code")) else ""), file=stream)

    try:
        db = db_mod.connect(cfg)
    except db_mod.DbError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1
    try:
        try:
            report = live(db, name)
        except RoleError as exc:
            print("ameesh role run: %s" % exc, file=sys.stderr)
            return 2
        delegated = False
        if report["stopped"]:
            report["code"] = "role-stopped"
        elif not report["held"]:
            report["code"] = "no-live-lease"
        elif pass_name == "alerts" and report.get("releve") == "election":
            # #3568: the alerts pass follows a route, not only the lease. The
            # holder keeps it when its standing says it can push; a holder
            # without a route leaves it to the first candidate that can.
            delegation = _alerts_delegation(db, report, host=host)
            report["pass"] = pass_name
            report["delegate"] = delegation.get("delegate")
            report["delegation"] = delegation.get("reason") or ""
            report["holder_notify_ok"] = bool(delegation.get("holder_notify_ok"))
            if delegation.get("delegate") is None:
                report["code"] = "no-alerts-delegate"
            elif delegation["delegate"] != host:
                report["code"] = "alerts-delegated"
            else:
                delegated = delegation["delegate"] != report["host"]
                if not delegated and args.min_left and (
                        report.get("left_s") is None
                        or float(report["left_s"]) < float(args.min_left)):
                    report["code"] = "lease-too-short"
        elif report["host"] != host:
            report["code"] = "other-host"
        elif args.min_left and (report.get("left_s") is None
                                or float(report["left_s"]) < float(args.min_left)):
            report["code"] = "lease-too-short"
        if report.get("code"):
            report["detail"] = _detail(report["code"], report)
            emit(report, result="standby")
            return 0
        report["delegated"] = delegated
        if args.dry_run:
            emit(report, result="would-run")
            return 0
        lease = None
        execution = None
        try:
            if pass_name == "alerts":
                lease = _AlertsLease(db, report["role"], host)
                if not lease.acquire():
                    report["code"] = "alerts-busy"
                    report["detail"] = _detail(report["code"], report)
                    emit(report, result="standby")
                    return 0
                # The local deadline is armed BEFORE the re-reads below: a
                # selection read that blocks past it must not let a child
                # start under an execution lease another host can take.
                execution = _AlertsExecution(lease)
                execution.start()
                # The selection read is not a reservation: the holder's route
                # can return between that read and the acquisition above.
                # Re-read the role AND the selection under the lease; if this
                # host is no longer the delegate, release and let it run.
                try:
                    fresh = live(db, name)
                except RoleError as exc:
                    print("ameesh role run: %s" % exc, file=sys.stderr)
                    return 2
                report.update(fresh)
                report["pass"] = pass_name
                if report["stopped"]:
                    report["code"] = "role-stopped"
                elif not report["held"]:
                    report["code"] = "no-live-lease"
                else:
                    again = _alerts_delegation(db, report, host=host)
                    report["delegate"] = again.get("delegate")
                    report["delegation"] = again.get("reason") or ""
                    report["holder_notify_ok"] = bool(
                        again.get("holder_notify_ok"))
                    delegated = again.get("delegate") != report["host"]
                    if again.get("delegate") is None:
                        report["code"] = "no-alerts-delegate"
                    elif again.get("delegate") != host:
                        report["code"] = "alerts-delegated"
                    elif delegated is False and args.min_left and (
                            report.get("left_s") is None
                            or float(report["left_s"]) < float(args.min_left)):
                        report["code"] = "lease-too-short"
                if report.get("code"):
                    report["detail"] = _detail(report["code"], report)
                    emit(report, result="standby")
                    return 0
                if execution.lost.is_set() or lease.remaining() <= 0:
                    print("ameesh role run: alerts execution lease deadline "
                          "passed before the child started", file=sys.stderr)
                    return 1
                report["delegated"] = delegated
                # #4468: remember this handover under the execution lease we
                # just took. The memory bounds a delegate whose pass never
                # beats: after its grace the pass moves to another candidate
                # instead of staying on the newest dead beat. A failure here
                # never prevents the pass from running.
                try:
                    remembered = alerts_delegation_pg.entrust(
                        db, report["role"], host, lock_key=lease.key,
                        owner=lease.owner, epoch=lease.epoch)
                except Exception as exc:  # noqa: BLE001 - never blocks the pass
                    remembered = False
                    print("ameesh role run: alerts delegation memory not "
                          "written: %s" % exc, file=sys.stderr)
                report["delegation_recorded"] = bool(remembered)
            if pass_name == "release":
                lease = _ReleaseLease(db, report["role"], host, report["epoch"])
                if not lease.acquire():
                    report["code"] = "release-busy"
                    report["detail"] = "another release tick holds the execution lease"
                    emit(report, result="standby")
                    return 0
                execution = _AlertsExecution(lease)
                execution.start()
                fresh = live(db, name)
                if (not fresh["held"] or fresh["stopped"]
                        or fresh["host"] != host or fresh["epoch"] != report["epoch"]
                        or execution.lost.is_set() or lease.remaining() <= 0):
                    emit(fresh, result="standby")
                    return 0
            env = os.environ.copy()
            # The command is not an agent turn: never inherit the shell's turn,
            # identity or a foreign tenure. Its explicit actor is the ROLE, under
            # the very lease read above; EP4's guard reads AMEESH_ROLE and the
            # epoch, and refuses a role without an epoch instead of treating the
            # timer as an anonymous process that may pass.
            for key in ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                        "AMEESH_TURN_ID", "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH",
                        "AMEESH_LEASE_OWNER", "AMEESH_LEASE_LEFT", "AMEESH_ROLE",
                        "AMEESH_ROLE_HOST", ENV_PASS):
                env.pop(key, None)
            env.update({
                # a child loads the same host, even when its AMEESH_HOST was unset
                "AMEESH_HOST": host,
                # explicit actor = the role, bound to the live lease (EP4 gate).
                ENV_ROLE: report["role"],
                ENV_HOST: host,
            })
            if pass_name:
                env[ENV_PASS] = pass_name
            # #3568: the delegated pass runs on a candidate that does NOT hold
            # the lease. It must not carry the holder's epoch: EP4 would read
            # it as a fenced gesture of the holder. The alerts pass writes no
            # pilot gesture (local notify state and the shared liveness beat).
            if not delegated:
                env.update({
                    ENV_EPOCH: ("" if report.get("epoch") is None
                                else str(report["epoch"])),
                    ENV_LEFT: ("" if report.get("left_s") is None
                               else str(int(report["left_s"]))),
                    ENV_OWNER: str(report.get("owner") or ""),
                })
            from .platform.child_environment import role_transport, grant
            env = role_transport(env, argv, getattr(cfg, "notify", {}))
            # Only trusted in-process callers can extend this one spawn; the
            # public role CLI exposes no credential-grant argument.
            env = grant(env, env.credentials | frozenset(credentials))
            if lease is not None:
                code, lost = _run_under_lease(argv, env, lease, execution)
                if lost:
                    print("ameesh role run: execution lease lost; the "
                          "child was stopped", file=sys.stderr)
                    return 1
            else:
                code = int(subprocess.run(argv, env=env, check=False).returncode)
            # The exit code of the command is the wrapper's. A report is printed
            # only on stderr in JSON mode, so a command that speaks JSON keeps
            # its stdout intact.
            if args.json:
                print(json.dumps(_record(report, host=host, argv=argv, result="ran",
                                         code="", detail=""),
                                 ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return code
        except FileNotFoundError as exc:
            print("ameesh role run: command not found: %s" % exc.filename,
                  file=sys.stderr)
            return 127
        except OSError as exc:
            print("ameesh role run: cannot start the command: %s" % exc,
                  file=sys.stderr)
            return 126
        finally:
            # Stop the local deadline thread before releasing the connection
            # it watches: a release clears the deadline, and a late stop must
            # not race the database driver.
            if execution is not None:
                execution.close()
            if lease is not None:
                lease.release()
    except db_mod.DbError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1
    finally:
        try:
            db.close()
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh role run",
        description="run a mechanical command only on the host that holds the "
                    "role's live lease (L165/EP5)",
    )
    parser.add_argument("role", help="agent name of the role (aliases accepted)")
    parser.add_argument("--pass", dest="pass_name", default="", metavar="PASS",
                        choices=("alerts", "release"),
                        help="run this pass under its own delegation instead of "
                             "the lease alone (`release` is fenced and exclusive; `alerts` "
                             "runs on the holder when it can push, else on the "
                             "first candidate that can)")
    parser.add_argument("--min-left", type=float, default=0.0,
                        help="refuse to start below S seconds of lease left "
                             "(default 0; ignored by a delegated pass)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the decision; run nothing")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per decision (schema ameesh-role-run/1)")
    return parser


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    """Options before the first ``--``, the command after it.

    The command is an argument vector: it may carry its own flags, so the
    wrapper stops parsing options at the separator (``--only`` is for
    ``ameesh notify``, never for ``role run``).
    """
    if "--" not in argv:
        return list(argv), []
    index = argv.index("--")
    return list(argv[:index]), list(argv[index + 1:])


def main(argv: list[str] | None = None) -> int:
    from .platform.child_environment import managed_entry
    if not managed_entry():
        return 1
    argv = sys.argv[1:] if argv is None else argv
    head, command = _split(argv)
    args = build_parser().parse_args(head)
    args.command = command
    try:
        cfg = config_mod.load()
    except Exception as exc:  # configuration illisible : rien ne tourne
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1
    try:
        return run(cfg, args)
    except RoleError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 2
    except db_mod.DbError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
