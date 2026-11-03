#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
# Drain of the self-hosted GitHub Actions runners of a CI VM (lot #3312).
# Installed by deploy/ci-flotte/install.sh as
# /usr/local/sbin/ameesh-ci-runner-drain; system Python, standard library
# only (the fleet VMs carry no ameesh venv).
"""Restart or stop a CI VM's runners only between two jobs.

A runner unit (`actions.runner.<org>-<repo>.<name>.service`) that receives
SIGTERM during a job cancels it: the job concludes `failure` with "The
runner has received a shutdown signal", and the suite is red without a
failing test (lot #3312, run 40068028315). This tool never signals a busy
runner:

* a runner is **busy** while a `Runner.Worker` process lives in its unit's
  cgroup (the listener spawns one per job), **or** while its listener has
  accepted a job that has not completed yet (#5656), read in its current
  `_diag/Runner_*.log`: the listener prints `Running job: …` before it
  spawns the Worker, and a restart in that window loses the job with
  "self-hosted runner lost communication" and no test played. A missing or
  unreadable log counts as busy (fail closed);
* an idle runner is restarted (default) or stopped (`--stop`); a busy one is
  left alone and reported, and the next pass takes it.

Which runners:

* `--needrestart` (default): those needrestart lists as running outdated
  libraries (`needrestart -b -r l`, `NEEDRESTART-SVC:` lines). The
  needrestart-ameesh-ci.conf snippet makes needrestart defer them instead of
  restarting them after an unattended upgrade; the timer
  (ameesh-ci-runner-drain.timer) then runs this pass every few minutes;
* `--all`: every active runner unit, for a planned stop of the host
  (reboot, resize, image change): `ameesh-ci-runner-drain --all --stop
  --wait 3600 && systemctl reboot` stops each runner as soon as its job
  ends, and a stopped runner takes no new job.

`--wait S` repeats the pass until no target is left or S seconds have
passed. Exit status: 0 nothing left to do, 3 runners still busy or left
alone after a recent restart (they keep their job; the timer, or the
operator, comes back), 2 usage or system error.

The final reading is made with the unit frozen (#3317). Busy units are
immediately thawed. For an idle unit, cgroup.kill removes its processes
before systemctl can thaw it (stop/restart automatically thaw a unit).
This requires cgroup v2 and Restart=no, the upstream runner service default:
no listener may respawn between the kill and the stop/restart. Unsupported
configurations fail closed. Concurrent drain invocations share a host lock.

`--cooldown S` (default 900) bounds the restart loop of #5656: the upstream
runner unit is `KillMode=process`, so a job process can survive a plain
`systemctl restart` and keep needrestart listing the unit; the next pass then
restarts it again, every five minutes. cgroup.kill removes that leftover
before the restart, and a unit started less than S seconds ago is never
restarted again by the needrestart path (the `--all` path ignores this).
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import os
import re
import signal
import subprocess
import sys
import time

UNIT_RE = re.compile(r"^actions\.runner\.[A-Za-z0-9_.@-]+\.service$")
WORKER = "Runner.Worker"
CGROUP_ROOT = "/sys/fs/cgroup"
BUSY_EXIT = 3
LOCK = "/run/lock/ameesh-ci-runner-drain.lock"
DIAG = "_diag"
LOG_PREFIX = "Runner_"
LOG_SUFFIX = ".log"
#: Marker written by the listener before it spawns Runner.Worker (#5656).
JOB_STARTED = b"Running job:"
#: Markers of an idle listener: the job ended, or this session never took one.
JOB_FINISHED = b"completed with result:"
LISTENING = b"Listening for Jobs"
SESSION_START = b"Runner is built for"
MARKERS = ((JOB_STARTED, True), (JOB_FINISHED, False),
           (LISTENING, False), (SESSION_START, False))
LOG_CHUNK = 65536


def last_marker(path, chunk=LOG_CHUNK):
    """Job state of one listener log, read backwards: True, False or None.

    True when the last marker is `Running job:` (a job was accepted and has
    not completed), False when it is an idle marker, None when the file holds
    no marker at all (a paged log: the caller looks at the previous page).
    Chunks are read from the end because act_if_idle calls this with the
    runner frozen: a listener log can be large, and only its tail is needed
    when the runner has been doing something.
    """
    longest = max(len(marker) for marker, _ in MARKERS)
    with open(path, "rb") as log:
        log.seek(0, os.SEEK_END)
        pos = log.tell()
        tail = b""
        while pos > 0:
            start = max(0, pos - chunk)
            log.seek(start)
            data = log.read(pos - start) + tail
            found = -1
            state = None
            for marker, busy in MARKERS:
                at = data.rfind(marker)
                if at > found:
                    found, state = at, busy
            if found >= 0:
                return state
            tail = data[:longest - 1]
            pos = start
    return None


class Host:
    """The real host: systemctl, needrestart, cgroup v2 and /proc."""

    def __init__(self, cgroup_root=CGROUP_ROOT, proc="/proc", lock=LOCK,
                 log_chunk=LOG_CHUNK):
        self.cgroup_root = cgroup_root
        self.proc = proc
        self.lock = lock
        self.log_chunk = log_chunk

    def _run(self, argv):
        return subprocess.run(argv, check=True, text=True,
                              stdout=subprocess.PIPE, timeout=60).stdout

    def outdated_units(self):
        out = self._run(["needrestart", "-b", "-r", "l"])
        units = []
        for line in out.splitlines():
            if line.startswith("NEEDRESTART-SVC:"):
                units.append(line.split(":", 1)[1].strip())
        return units

    def active_units(self):
        out = self._run(["systemctl", "list-units", "--type=service",
                         "--state=active", "--plain", "--no-legend",
                         "actions.runner.*"])
        return [line.split()[0] for line in out.splitlines() if line.strip()]

    def process_names(self, unit):
        group = self._run(["systemctl", "show", "-p", "ControlGroup",
                           "--value", unit]).strip()
        if not group:
            return []
        names = []
        def fail(exc):
            raise exc
        # A worker in a descendant cgroup also makes the runner busy.
        for path, _, _ in os.walk(self.cgroup_root + group, onerror=fail):
            with open(os.path.join(path, "cgroup.procs")) as f:
                pids = f.read().split()
            for pid in pids:
                try:
                    with open(os.path.join(self.proc, pid, "comm")) as f:
                        names.append(f.read().strip())
                except FileNotFoundError:
                    continue  # the process ended meanwhile
        return names

    def act(self, verb, unit):
        subprocess.run(["systemctl", verb, unit], check=True, timeout=60)

    def active_seconds(self, unit):
        """Seconds since the unit entered active state, or None if unknown."""
        out = self._run(["systemctl", "show", "-p",
                         "ActiveEnterTimestampMonotonic", "--value",
                         unit]).strip()
        if not out or not out.isdigit() or out == "0":
            return None
        return max(0.0, time.monotonic() - int(out) / 1_000_000)

    def job_in_flight(self, unit):
        """True when the listener accepted a job that has not completed.

        The listener prints `Running job: …` before it spawns Runner.Worker
        (#5656): in that window the cgroup has no Worker yet, and the
        process check alone would call the runner idle. The listener's
        current `_diag/Runner_*.log` is the only trace of that job, and
        act_if_idle reads it with the unit frozen. Fail closed: a missing,
        unreadable or marker-less log counts as busy, so the worst case is a
        runner left alone, never a job cut.
        """
        root = self._run(["systemctl", "show", "-p", "WorkingDirectory",
                          "--value", unit]).strip()
        if not root or root == "-" or not os.path.isdir(root):
            return True
        diag = os.path.join(root, DIAG)
        try:
            names = sorted(name for name in os.listdir(diag)
                           if name.startswith(LOG_PREFIX)
                           and name.endswith(LOG_SUFFIX))
        except OSError:
            return True
        if not names:
            return True
        # Timestamps in the name sort chronologically; a paged log starts a
        # new file, and a restarted listener starts a new one too. Walk the
        # pages backwards until the last marker of the current session.
        for name in reversed(names):
            try:
                state = last_marker(os.path.join(diag, name), self.log_chunk)
            except OSError:
                return True
            if state is not None:
                return state
        return True

    def _wait_event(self, group, key, value, removed_ok=False):
        deadline = time.monotonic() + 5
        while True:
            try:
                with open(group + "/cgroup.events") as f:
                    events = dict(line.split() for line in f)
            except FileNotFoundError:
                if removed_ok:
                    return
                raise
            if events.get(key) == value:
                return
            if time.monotonic() >= deadline:
                raise OSError("runner cgroup timed out waiting for %s=%s" % (key, value))
            time.sleep(0.01)

    def _set_freezer(self, group, value):
        # r+ refuses missing/unsupported interfaces instead of creating a file.
        with open(group + "/cgroup.freeze", "r+") as f:
            f.write(value)

    def _kill_frozen(self, group):
        with os.fdopen(os.open(group + "/cgroup.kill", os.O_WRONLY), "w") as f:
            f.write("1")
        # SIGKILL is asynchronous, even for frozen tasks. Do not let systemd
        # thaw any survivor. A removed cgroup is also empty (Restart=no).
        self._wait_event(group, "populated", "0", removed_ok=True)

    def act_if_idle(self, verb, unit):
        """Freeze, recheck, retire the idle listener, then stop/restart it."""
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            restart = self._run(["systemctl", "show", "-p", "Restart",
                                 "--value", unit]).strip()
            if restart != "no":
                raise OSError("safe drain requires Restart=no: " + unit)
            forced = self._run(["systemctl", "show", "-p", "RestartForceExitStatus",
                                "--value", unit]).strip()
            if forced:
                raise OSError("safe drain refuses RestartForceExitStatus: " + unit)
            group = self._run(["systemctl", "show", "-p", "ControlGroup",
                               "--value", unit]).strip()
            if not group or group == "/" or not group.startswith("/"):
                raise OSError("runner has no usable cgroup: " + unit)
            group = self.cgroup_root + group
            with open(group + "/cgroup.freeze") as f:
                if f.read().strip() != "0":
                    raise OSError("runner cgroup is already frozen: " + unit)
            try:
                # Use the kernel freezer: systemctl freeze retains a frozen
                # unit state after SIGKILL and can then refuse its restart.
                self._set_freezer(group, "1")
                self._wait_event(group, "frozen", "1")
                if busy(self, unit):
                    return False
                self._kill_frozen(group)
            finally:
                try:
                    self._set_freezer(group, "0")
                except OSError as exc:
                    if exc.errno not in (errno.ENOENT, errno.ENODEV):
                        raise
            self.act(verb, unit)
            return True


def runner_units(units):
    return sorted({u for u in units if UNIT_RE.match(u)})


def busy(host, unit):
    return WORKER in host.process_names(unit) or host.job_in_flight(unit)


def one_pass(host, every, verb, dry_run, log, cooldown=0):
    """Act on every idle target; return the targets still busy or deferred.

    In the needrestart restart path, a unit started less than `cooldown`
    seconds ago is left alone: restarting it again within that window is the
    #5656 loop (a leftover job process keeps needrestart listing the unit),
    and the cgroup.kill in act_if_idle is what removes that leftover. The
    --all path (planned host stop) always acts.
    """
    targets = runner_units(host.active_units() if every
                           else host.outdated_units())
    busy_units = []
    deferred = []
    for unit in targets:
        if busy(host, unit):
            busy_units.append(unit)
            continue
        if cooldown > 0 and not every and verb == "restart":
            age = host.active_seconds(unit)
            if age is not None and age < cooldown:
                log("active for %d s (< %d s cooldown), not restarted again: %s"
                    % (age, cooldown, unit))
                deferred.append(unit)
                continue
        if dry_run:
            log("would %s %s (idle)" % (verb, unit))
            continue
        if not host.act_if_idle(verb, unit):
            busy_units.append(unit)
            continue
        log("%s %s (idle)" % (verb, unit))
    for unit in busy_units:
        log("busy, kept until its job ends: %s" % unit)
    return busy_units + deferred


def main(argv=None, host=None, sleep=time.sleep, clock=time.monotonic,
         log=None):
    parser = argparse.ArgumentParser(
        prog="ameesh-ci-runner-drain",
        description="Restart or stop CI runners only between two jobs (#3312).")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--needrestart", action="store_true",
                       help="runners needrestart lists as outdated (default)")
    which.add_argument("--all", action="store_true",
                       help="every active runner (planned host stop)")
    parser.add_argument("--stop", action="store_true",
                        help="stop instead of restart")
    parser.add_argument("--wait", type=int, default=0, metavar="S",
                        help="repeat until done or S seconds elapsed")
    parser.add_argument("--interval", type=int, default=15, metavar="S",
                        help="pause between two passes with --wait")
    parser.add_argument("--cooldown", type=int, default=900, metavar="S",
                        help="needrestart restart path: do not restart again "
                             "a runner started less than S seconds ago "
                             "(default 900; 0 disables)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be done, change nothing")
    args = parser.parse_args(argv)
    if args.wait < 0 or args.interval < 1 or args.cooldown < 0:
        parser.error("--wait must be >= 0, --interval >= 1 and --cooldown >= 0")
    host = host or Host()
    log = log or (lambda msg: print(msg, flush=True))
    verb = "stop" if args.stop else "restart"
    deadline = clock() + args.wait
    try:
        while True:
            left = one_pass(host, args.all, verb, args.dry_run, log,
                            cooldown=args.cooldown)
            if not left or args.dry_run or clock() >= deadline:
                break
            sleep(min(args.interval, max(deadline - clock(), 0)))
    except (OSError, subprocess.SubprocessError) as exc:
        print("ameesh-ci-runner-drain: %s" % exc, file=sys.stderr)
        return 2
    return BUSY_EXIT if left else 0


if __name__ == "__main__":
    # A service stop must unwind the frozen-unit cleanup, like Ctrl-C does.
    def interrupted(signum, frame):
        sys.exit(2)
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
