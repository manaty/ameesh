#!/opt/ameesh/venv/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
# The GitHub Actions runner of the ameesh GUEST, single-use and behind the
# lending host's gate (lot #1256, option A of the R2 B1 arbitration).
# Installed by deploy/guest/ci-runners.sh as
# /usr/local/lib/ameesh-guest-ci/ci-runner-gate, inside the guest's slice.
"""Run **one** single-use GitHub runner while the host gate is open.

The GitHub runner (`run.sh`) knows nothing of the capacity gate the guard
publishes (L159e/L159f, `ameesh-host-state/1` in
`/run/ameesh-gate/state/state.json`). This wrapper is the guest's obedience
**and** its single-use discipline (option A):

* it waits for a fresh `available` state published by a guard in `enforce`
  **without** consuming anything — an observing guard, an absent or a stale
  state starts nothing (plan §5/§6);
* it then consumes exactly one *just-in-time* configuration from the deposit
  directory (`--spool`, one file per job, written there by the ameesh side
  outside this host: `ci-runners.sh --deposit`), validates it (base64 of a
  JSON object, safe file names, a `.runner` that declares `ephemeral: true` —
  the runner's own single-job setting), unlinks it, and starts
  `run.sh --jitconfig <configuration>`: GitHub removes the runner after that
  one job, so there is no registration token and no durable credential on the
  lending host (plan §1/§3);
* while the job runs, it watches the state; when the state closes, disappears
  or goes stale (the guard died or hung: a lending host is closed by default)
  it stops the runner — SIGTERM to its process group, the runner ends the job
  at a safe point — and the configuration is spent, never reused;
* before and after the run it removes the local configuration the runner
  writes from the JIT blob (`.runner`, `.credentials`,
  `.credentials_rsaparams`): a credential that outlived its job is dead
  anyway (the runner is already removed server side), and nothing durable
  must stay on a host of another project.

One unit start = at most one job: there is no persistent listener here, so a
job for the guest waits until the ameesh side deposits a fresh configuration
and arms the slot (plan §3, "the price of option A").

The gate contract is read by its canonical reader
(`ameesh.capacity_guard.gate_state`): a venv that cannot answer is a refused
start, never an open gate.

    sudo deploy/guest/ci-runners.sh --prepare              # installs this wrapper
    sudo deploy/guest/ci-runners.sh --deposit /root/jit-1  # one single-use configuration
    sudo deploy/guest/ci-runners.sh --enable 1             # one job, then nothing
    ExecStart=/usr/local/lib/ameesh-guest-ci/ci-runner-gate --dir /srv/ameesh-guest/ci/r%i \
        --spool /srv/ameesh-guest/ci/jit/r%i
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import signal
import stat
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
#: one JIT configuration is the base64 of a small JSON document (itself
#: mapping file names to base64 contents): far under this bound
MAX_CONFIG_BYTES = 65536
#: the runner writes these from the JIT configuration; they are removed before
#: and after the job, so a dead credential never stays on the lending host
LOCAL_CONFIG_FILES = (".runner", ".credentials", ".credentials_rsaparams")
#: the file names a JIT configuration may carry: the runner writes each of them
#: into its root (Runner.cs, `RunJitConfig`), so a slash or a `..` there would
#: escape that root. All-dots names are refused too (`.` and `..` match the
#: class but are directories, not files).
SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


class BadConfiguration(Exception):
    """A deposited `.b64` file that is not a usable configuration: it is left
    in place, named on stderr, and no runner starts from it."""


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


def clean_local_config(run_dir):
    """Remove what the runner wrote from its JIT configuration. Best effort:
    the file may be absent, and a directory of that name is left alone."""
    for name in LOCAL_CONFIG_FILES:
        try:
            os.unlink(os.path.join(run_dir, name))
        except OSError:
            pass


def read_bounded(fd):
    """The bytes of an open configuration, or None past `MAX_CONFIG_BYTES`.

    Read on the descriptor, never on the path: the file cannot be swapped
    between the check and the read, and the bound is re-applied to what was
    really read (a file grown after `fstat` is refused, not truncated into a
    half-configuration that would still look valid)."""
    raw = b""
    while len(raw) <= MAX_CONFIG_BYTES:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        raw += chunk
    if len(raw) > MAX_CONFIG_BYTES:
        return None
    return raw


def validate_config(text):
    """The decoded JIT document, or ValueError: base64 of a JSON object of
    runner files, safe file names, and a `.runner` that declares
    `ephemeral: true`.

    The runner's one-shot discipline is `settings.Ephemeral`
    (actions/runner v2.338.0 `Runner.cs`: the JIT blob only writes the files;
    the single-job path reads that setting): a configuration without it would
    leave a **persistent** runner registered on this host, so neither the
    deposit nor this gate lets one through. The name check matters as much:
    `run.sh --jitconfig` writes every entry into the runner root, so
    `../../outside` would escape it."""
    try:
        decoded = json.loads(base64.b64decode(text, validate=True).decode("utf-8"))
        if not isinstance(decoded, dict) or not decoded:
            raise ValueError("not a non-empty configuration document")
        for name, value in decoded.items():
            if (not isinstance(name, str) or not SAFE_FILE_NAME.fullmatch(name)
                    or set(name) <= {"."}):
                raise ValueError("unsafe runner file name %r" % (name,))
            if not isinstance(value, str):
                raise ValueError("runner file %s is not base64 text" % name)
            base64.b64decode(value, validate=True)
        runner = decoded.get(".runner")
        if runner is None:
            raise ValueError("no .runner: the single-use contract cannot be read")
        settings = json.loads(base64.b64decode(runner, validate=True).decode("utf-8"))
        if not isinstance(settings, dict) or settings.get("ephemeral") is not True:
            raise ValueError(".runner does not set ephemeral true: a persistent "
                             "runner is refused")
    except (UnicodeDecodeError, ValueError, TypeError,
            binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    return decoded


def take_config(spool):
    """Consume exactly one deposited configuration: read it, unlink it, return
    its text (or None when none is deposited).

    The configuration is a file of the guest's own deposit directory; it is
    read with `O_NOFOLLOW` on the descriptor, only if it is a regular file of
    bounded size (a link or a device planted there is never a configuration),
    and it is fully validated before it is unlinked — a tampered entry is
    refused, not passed to `run.sh`. A consumption that fails (the entry
    cannot be unlinked) is **refused before the start**: returning the text
    without consuming it would run the same configuration twice.
    """
    try:
        names = sorted(name for name in os.listdir(spool) if name.endswith(".b64"))
    except OSError:
        return None
    for name in names:
        path = os.path.join(spool, name)
        try:
            # A FIFO must not block before fstat can reject its file type.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                print("ci-runner-gate: %s is not a regular configuration file: "
                      "left alone" % path, file=sys.stderr)
                continue
            if info.st_size > MAX_CONFIG_BYTES:
                raise BadConfiguration(
                    "%s is larger than %d bytes: not a JIT configuration"
                    % (path, MAX_CONFIG_BYTES))
            raw = read_bounded(fd)
        finally:
            os.close(fd)
        if raw is None:
            raise BadConfiguration(
                "%s grew past %d bytes while being read: refusing it"
                % (path, MAX_CONFIG_BYTES))
        try:
            text = "".join(raw.decode("ascii").split())
            validate_config(text)
        except (UnicodeDecodeError, ValueError, TypeError,
                binascii.Error, json.JSONDecodeError) as exc:
            raise BadConfiguration(
                "%s is not a usable JIT configuration (%s): refusing to run it"
                % (path, exc)) from exc
        try:
            os.unlink(path)
        except OSError as exc:
            raise BadConfiguration(
                "%s cannot be consumed (%s): refusing to run a configuration "
                "that would stay deposited" % (path, exc)) from exc
        return text
    return None


def supervise(reader, gate_path, run_dir, spool, interval, grace):
    """The loop: wait for the gate, consume one configuration, run one job."""
    run_sh = os.path.join(run_dir, "run.sh")
    if not os.access(run_sh, os.X_OK):
        print("ci-runner-gate: %s is missing or not executable" % run_sh,
              file=sys.stderr)
        return EXIT_CONFIG
    if not os.path.isdir(spool):
        print("ci-runner-gate: %s is not a directory: no single-use configuration "
              "can be deposited (deploy/guest/ci-runners.sh --prepare creates it)"
              % spool, file=sys.stderr)
        return EXIT_CONFIG
    stopping = False

    def on_term(signum, frame):  # pragma: no cover - a signal, not a branch
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    child = None
    try:
        # 1. the gate first: a closed (or observing, or stale) state must not
        #    burn a single-use configuration
        while True:
            if stopping:
                print("ci-runner-gate: signal before the gate opened: "
                      "no configuration consumed", flush=True)
                return 0
            open_now, why = read_gate(reader, gate_path, time.time())
            if open_now:
                break
            time.sleep(interval)
        # 2. exactly one deposited configuration, consumed here
        try:
            config = take_config(spool)
        except BadConfiguration as exc:
            print("ci-runner-gate: %s" % exc, file=sys.stderr)
            return EXIT_CONFIG
        if config is None:
            print("ci-runner-gate: gate %s but no configuration deposited in %s: "
                  "nothing to run" % (why, spool), flush=True)
            return 0
        clean_local_config(run_dir)
        print("ci-runner-gate: gate available: starting %s --jitconfig <one-shot>"
              % run_sh, flush=True)
        child = subprocess.Popen([run_sh, "--jitconfig", config], cwd=run_dir,
                                 start_new_session=True)
        config = None  # never reused, never written out
        # 3. the one job runs; a gate that closes stops it (the configuration
        #    is spent: one job per deposit, never a silent retry)
        while True:
            if stopping:
                stop_child(child, grace)
                print("ci-runner-gate: signal: runner stopped (gate %s)" % why,
                      flush=True)
                return 0
            if child.poll() is not None:
                code = child.returncode
                print("ci-runner-gate: %s exited %s (single-use configuration spent)"
                      % (run_sh, code), flush=True)
                return code
            open_now, why = read_gate(reader, gate_path, time.time())
            if not open_now:
                print("ci-runner-gate: gate %s: stopping the runner" % why, flush=True)
                stop_child(child, grace)
                print("ci-runner-gate: runner stopped, its configuration is spent",
                      flush=True)
                return 0
            time.sleep(interval)
    finally:
        clean_local_config(run_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="run one single-use GitHub runner while the host gate is open")
    parser.add_argument("--dir", required=True,
                        help="the runner's directory (its run.sh inside)")
    parser.add_argument("--spool", required=True,
                        help="the directory of deposited single-use configurations")
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
    return supervise(reader, args.gate, args.dir, args.spool,
                     args.interval, args.grace)


if __name__ == "__main__":
    sys.exit(main())
