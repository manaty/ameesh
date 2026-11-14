#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7319: recover failed native CI services without rearming stopped runners.

Restart=no remains mandatory for the frozen drain (#3317). This independent
pass shares its lock, starts only failed services with an empty cgroup, and
reports failures to the journal. It never stops or restarts an active service.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys

UNIT_RE = re.compile(r"^actions\.runner\.[A-Za-z0-9_.@-]+\.service$")
LOCK = "/run/lock/ameesh-ci-runner-drain.lock"


class Host:
    def __init__(self, command=("systemctl",), cgroup_root="/sys/fs/cgroup",
                 lock=LOCK):
        self.command = command
        self.cgroup_root = Path(cgroup_root)
        self.lock = lock

    def run(self, *args):
        return subprocess.run([*self.command, *args], check=True, text=True,
                              stdout=subprocess.PIPE, timeout=30).stdout

    def units(self):
        output = self.run("list-units", "--all", "--type=service", "--plain",
                          "--no-legend", "actions.runner.*")
        return sorted({line.split()[0] for line in output.splitlines()
                       if line.strip() and UNIT_RE.fullmatch(line.split()[0])})

    def properties(self, unit):
        output = self.run("show", unit, "--property=ActiveState,SubState,Result,"
                          "ControlGroup,ExecMainCode,ExecMainStatus,LoadState,Job")
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def populated(self, group):
        # A failed listener can leave a live Worker (KillMode=process). Do not
        # start a second listener while that job is still running. Read the
        # recursive kernel counter, never infer emptiness from MainPID=0.
        if not group:
            return False
        if not group.startswith("/") or ".." in group.split("/") or group == "/":
            raise OSError("unusable runner cgroup: " + group)
        try:
            data = (self.cgroup_root / group.lstrip("/") / "cgroup.events").read_text()
        except FileNotFoundError:
            # A retired cgroup has no remaining processes.
            return False
        events = dict(line.split() for line in data.splitlines())
        if events.get("populated") not in ("0", "1"):
            raise OSError("runner cgroup has no populated counter: " + group)
        return events["populated"] == "1"

    def start(self, unit):
        # Never replace an operator's pending stop job.
        self.run("start", "--job-mode=fail", unit)


def recover(host, log=print):
    """One bounded pass. Nonzero means an unavailable service needs attention."""
    errors = 0
    fd = os.open(host.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for unit in host.units():
            try:
                state = host.properties(unit)
                active = state.get("ActiveState")
                if active in ("active", "activating", "reloading", "deactivating"):
                    continue
                if active == "inactive":
                    log("<5>runner stopped (not rearmed): " + unit)
                    continue
                if active != "failed" or state.get("LoadState") != "loaded":
                    raise OSError("unreadable runner state: " + unit)
                log("<3>runner offline: %s result=%s code=%s status=%s" % (
                    unit, state.get("Result"), state.get("ExecMainCode"),
                    state.get("ExecMainStatus")))
                if host.populated(state.get("ControlGroup", "")):
                    raise OSError("failed runner still has processes; left alone: " + unit)
                # The operator can stop/reset a failed service during the pass.
                current = host.properties(unit)
                if current.get("ActiveState") != "failed" or current.get("Job"):
                    log("<5>runner state changed or job pending; left alone: " + unit)
                    continue
                host.start(unit)
                if host.properties(unit).get("ActiveState") != "active":
                    raise OSError("runner did not become active: " + unit)
                log("<5>runner recovered: " + unit)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                errors += 1
                log("<3>runner recovery needs attention: %s: %s" % (unit, exc))
    return 1 if errors else 0


def main():
    try:
        return recover(Host(), log=lambda text: print(text, flush=True))
    except (OSError, subprocess.SubprocessError) as exc:
        print("<3>runner recovery unavailable: " + str(exc), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
