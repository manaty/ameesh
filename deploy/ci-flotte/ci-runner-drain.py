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
  cgroup (the listener spawns one per job); otherwise it is idle;
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
passed. Exit status: 0 nothing left to do, 3 runners still busy (they keep
their job; the timer, or the operator, comes back), 2 usage or system error.

The final reading is made with the unit frozen (#3317). Busy units are
immediately thawed. For an idle unit, cgroup.kill removes its processes
before systemctl can thaw it (stop/restart automatically thaw a unit).
This requires cgroup v2 and Restart=no, the upstream runner service default:
no listener may respawn between the kill and the stop/restart. Unsupported
configurations fail closed. Concurrent drain invocations share a host lock.
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


class Host:
    """The real host: systemctl, needrestart, cgroup v2 and /proc."""

    def __init__(self, cgroup_root=CGROUP_ROOT, proc="/proc", lock=LOCK):
        self.cgroup_root = cgroup_root
        self.proc = proc
        self.lock = lock

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
    return WORKER in host.process_names(unit)


def one_pass(host, every, verb, dry_run, log):
    """Act on every idle target; return the targets still busy."""
    targets = runner_units(host.active_units() if every
                           else host.outdated_units())
    left = []
    for unit in targets:
        if busy(host, unit):
            left.append(unit)
            continue
        if dry_run:
            log("would %s %s (idle)" % (verb, unit))
            continue
        if not host.act_if_idle(verb, unit):
            left.append(unit)
            continue
        log("%s %s (idle)" % (verb, unit))
    for unit in left:
        log("busy, kept until its job ends: %s" % unit)
    return left


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
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be done, change nothing")
    args = parser.parse_args(argv)
    if args.wait < 0 or args.interval < 1:
        parser.error("--wait must be >= 0 and --interval >= 1")
    host = host or Host()
    log = log or (lambda msg: print(msg, flush=True))
    verb = "stop" if args.stop else "restart"
    deadline = clock() + args.wait
    try:
        while True:
            left = one_pass(host, args.all, verb, args.dry_run, log)
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
