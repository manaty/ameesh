#!/opt/ameesh/venv/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
# The GitHub Actions runner of the ameesh GUEST, behind the lending host's
# gate (lot #1256, R1 fix B2). Installed by deploy/guest/ci-runners.sh as
# /srv/ameesh-guest/bin/ci-runner-gate, inside the guest's slice.
"""Run the guest's GitHub runner only while the host gate is open.

The GitHub runner (`run.sh`) knows nothing of the capacity gate the guard
publishes (L159e/L159f, `ameesh-host-state/1` in
`/run/ameesh-gate/state/state.json`). This wrapper is the guest's obedience:
it starts `run.sh` only while the published state is `available` **and** the
guard that publishes it runs in `enforce` — an observing guard stops nothing,
so the guests stay off (plan §6). While the runner runs, the wrapper watches
the state; when it closes, disappears or goes stale (the guard died or hung:
a lending host is closed by default), it stops the runner — SIGTERM to its
process group, the runner ends the job at a safe point — and waits for the
state to open again. A guest runner therefore never outlives the guard that
answers for it, and no job starts while the application needs the host.

The gate contract is read by its canonical reader
(`ameesh.capacity_guard.gate_state`): a venv that cannot answer is a refused
start, never an open gate.

    sudo deploy/guest/ci-runners.sh --prepare      # installs this wrapper
    ExecStart=/srv/ameesh-guest/bin/ci-runner-gate --dir /srv/ameesh-guest/ci/r%i
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

#: the guard's state document (L159f); `AMEESH_HOST_GATE_PATH` overrides
DEFAULT_GATE = "/run/ameesh-gate/state/state.json"
#: the guard writes every `STATE_TTL_S / 3` (30 s); 5 s is a cheap watch
DEFAULT_INTERVAL = 5.0
#: after SIGTERM, wait for the runner's own drain; the unit's TimeoutStopSec
#: (180 s) is the hard bound systemd applies next
DEFAULT_GRACE = 150.0
#: sysexits EX_CONFIG: a host that cannot read the gate starts no runner
EXIT_CONFIG = 78


def load_reader():
    """`capacity_guard.gate_state`, or None when the venv cannot answer."""
    try:
        from ameesh.capacity_guard import gate_state
    except ImportError as exc:  # a broken venv is a closed gate, not an open one
        print("ci-runner-gate: cannot read the gate contract (%s): "
              "no runner starts" % exc, file=sys.stderr)
        return None
    return gate_state


def read_gate(reader, path, now):
    """(open, why): the gate is open only for a fresh `available` state
    published by a guard in `enforce`. An unreadable, absent, stale, unknown
    or observed state is closed (the application wins)."""
    doc = None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    state = reader(doc, now, lending=True)
    if state != "available":
        return False, state
    guard = (doc or {}).get("guard")
    mode = guard.get("mode") if isinstance(guard, dict) else None
    if mode != "enforce":
        return False, "observe"
    return True, state


def stop_child(child, grace):
    """SIGTERM the runner's process group, its job ending with it; SIGKILL
    only after `grace` (systemd's own SIGKILL follows at TimeoutStopSec)."""
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def supervise(reader, gate_path, run_dir, interval, grace):
    """The loop: `run.sh` runs only while the gate is open."""
    run_sh = os.path.join(run_dir, "run.sh")
    if not os.access(run_sh, os.X_OK):
        print("ci-runner-gate: %s is missing or not executable" % run_sh,
              file=sys.stderr)
        return EXIT_CONFIG
    stopping = False

    def on_term(signum, frame):  # pragma: no cover - a signal, not a branch
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    child = None
    while True:
        open_now, why = read_gate(reader, gate_path, time.time())
        if stopping:
            if child is not None:
                stop_child(child, grace)
            print("ci-runner-gate: signal: runner stopped (gate %s)" % why, flush=True)
            return 0
        if open_now:
            if child is None:
                print("ci-runner-gate: gate available: starting %s" % run_sh, flush=True)
                child = subprocess.Popen([run_sh], cwd=run_dir, start_new_session=True)
            elif child.poll() is not None:
                code = child.returncode
                print("ci-runner-gate: %s exited %s" % (run_sh, code), flush=True)
                return code
        else:
            if child is not None:
                print("ci-runner-gate: gate %s: stopping the runner" % why, flush=True)
                stop_child(child, grace)
                child = None
                print("ci-runner-gate: runner stopped, waiting for the gate", flush=True)
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="run the guest's GitHub runner only while the host gate is open")
    parser.add_argument("--dir", required=True,
                        help="the runner's directory (its run.sh inside)")
    parser.add_argument("--gate", default=os.environ.get("AMEESH_HOST_GATE_PATH", DEFAULT_GATE),
                        help="the guard's state document (default: %s)" % DEFAULT_GATE)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between two readings (default: %(default)s)")
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE,
                        help="seconds to wait after SIGTERM (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.grace < 0:
        parser.error("--interval must be > 0 and --grace >= 0")
    reader = load_reader()
    if reader is None:
        return EXIT_CONFIG
    return supervise(reader, args.gate, args.dir, args.interval, args.grace)


if __name__ == "__main__":
    sys.exit(main())
