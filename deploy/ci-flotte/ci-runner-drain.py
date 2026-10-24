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

Residual race: the listener can accept a job between the idleness reading
and the signal. The reading is repeated just before acting, so the window is
the time of one `systemctl` call; a job lost there is a self-hosted job that
lost its runner, which relance-hosted (#2954) reruns once.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

UNIT_RE = re.compile(r"^actions\.runner\.[A-Za-z0-9_.@-]+\.service$")
WORKER = "Runner.Worker"
CGROUP_ROOT = "/sys/fs/cgroup"
BUSY_EXIT = 3


class Host:
    """The real host: systemctl, needrestart, cgroup v2 and /proc."""

    def __init__(self, cgroup_root=CGROUP_ROOT, proc="/proc"):
        self.cgroup_root = cgroup_root
        self.proc = proc

    def _run(self, argv):
        return subprocess.run(argv, check=True, text=True,
                              stdout=subprocess.PIPE).stdout

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
        try:
            with open(self.cgroup_root + group + "/cgroup.procs") as f:
                pids = f.read().split()
        except FileNotFoundError:
            return []
        names = []
        for pid in pids:
            try:
                with open(os.path.join(self.proc, pid, "comm")) as f:
                    names.append(f.read().strip())
            except OSError:
                continue  # the process ended meanwhile
        return names

    def act(self, verb, unit):
        subprocess.run(["systemctl", verb, unit], check=True)


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
        # read again just before acting: the window left is one systemctl call
        if busy(host, unit):
            left.append(unit)
            continue
        host.act(verb, unit)
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
    except (OSError, subprocess.CalledProcessError) as exc:
        print("ameesh-ci-runner-drain: %s" % exc, file=sys.stderr)
        return 2
    return BUSY_EXIT if left else 0


if __name__ == "__main__":
    sys.exit(main())
