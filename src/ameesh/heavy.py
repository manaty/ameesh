# SPDX-License-Identifier: AGPL-3.0-only
"""L131 — heavy suites capped per host: `ameesh heavy -- <command>`.

On 2026-10-10, 11 agents started jest, SQL suites and builds at the same time:
load 43 for 12 CPUs and about 80 minutes without a new turn. A heavy command
(full test suite, build, SQL suite) now runs through `ameesh heavy`, which
holds one of the host's slots for its whole duration. At most
`max_heavy_suites` run at once on a host (default: a quarter of the CPUs, at
least 1); the others wait for a free slot, then run.

A slot is an exclusive `flock` on a file of the host's state directory: the
kernel releases it when the holder dies, so a killed turn never keeps a slot.
The holder (pid, agent, command, start) is written in the slot file for
`ameesh heavy --status`. The command runs under `nice` (default 10), so the
runner and the orchestrator's short turns stay responsive.

The command inherits the locked slot descriptor (codex review r1, P2): the
slot lives as long as the command, even if the wrapper is killed, and a
process the command leaves behind keeps it until it exits too. SIGTERM,
SIGINT and SIGHUP sent to the wrapper are relayed to the command, which the
wrapper then waits for.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time

DEFAULT_NICE = 10
POLL_S = 5.0
WAIT_LOG_S = 60.0
#: signals the wrapper relays to the command it runs
RELAYED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def limit(cfg) -> int:
    """Slots of the host: `max_heavy_suites` (config, `AMEESH_MAX_HEAVY_SUITES`),
    default a quarter of the CPUs, at least 1."""
    raw = os.environ.get("AMEESH_MAX_HEAVY_SUITES") or getattr(cfg, "max_heavy_suites", 0)
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else max(1, (os.cpu_count() or 4) // 4)


def slots_dir(cfg) -> str:
    return os.path.join(cfg.state_dir, ".heavy")


def _slot_paths(cfg) -> list[str]:
    root = slots_dir(cfg)
    os.makedirs(root, mode=0o700, exist_ok=True)
    return [os.path.join(root, "slot-%d" % i) for i in range(limit(cfg))]


def _try(path: str):
    """The open slot file, locked; None if another holder has it."""
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def _holder(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.loads(fh.read() or "{}")
    except (OSError, ValueError):
        return {}


def status(cfg) -> list[dict]:
    """One entry per slot: `slot`, `busy`, and the holder when busy."""
    out = []
    for i, path in enumerate(_slot_paths(cfg)):
        fh = _try(path)
        if fh is not None:
            fh.close()
            out.append({"slot": i, "busy": False})
        else:
            out.append(dict(_holder(path), slot=i, busy=True))
    return out


def acquire(cfg, *, command: str = "", log=None, sleep=time.sleep,
            clock=time.monotonic, max_wait_s: float | None = None):
    """Waits for a free slot and holds it: (slot index, open locked file), or
    None if `max_wait_s` passed. `log(text)` is told who holds the slots,
    once a minute while waiting."""
    started = clock()
    last_log = None
    while True:
        paths = _slot_paths(cfg)
        for i, path in enumerate(paths):
            fh = _try(path)
            if fh is None:
                continue
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "since": time.time(),
                                 "agent": os.environ.get("AGENT_MAIL_NAME") or "",
                                 "command": command[:200]}))
            fh.flush()
            return i, fh
        waited = clock() - started
        if max_wait_s is not None and waited >= max_wait_s:
            return None
        if log is not None and (last_log is None or clock() - last_log >= WAIT_LOG_S):
            last_log = clock()
            busy = ", ".join("%s (%s)" % (h.get("agent") or "pid %s" % h.get("pid"),
                                          (h.get("command") or "?")[:40])
                             for h in (_holder(p) for p in paths))
            log("ameesh heavy: %d/%d heavy suites running on this host (%s); waiting "
                "%ds for a slot" % (len(paths), len(paths), busy, int(waited)))
        sleep(POLL_S)


def release(fh) -> None:
    try:
        fh.seek(0)
        fh.truncate()
        fh.flush()
    finally:
        fh.close()                  # closing drops the flock


def _note_child(fh, pid: int) -> None:
    """Adds the command's pid to the holder written in the slot file."""
    try:
        fh.seek(0)
        holder = json.loads(fh.read() or "{}")
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(dict(holder, child=pid)))
        fh.flush()
    except (OSError, ValueError):
        pass


def run(cfg, argv: list, *, nice: int = DEFAULT_NICE) -> int:
    """Runs `argv` holding a heavy slot; its exit code (127 if not found,
    128 + n if killed by signal n)."""
    command = shlex.join(argv)
    held = acquire(cfg, command=command,
                   log=lambda text: print(text, file=sys.stderr, flush=True))
    slot, fh = held
    child = None
    pending: list = []

    def relay(signum, frame):
        if child is None:
            pending.append(signum)
        elif child.poll() is None:
            child.send_signal(signum)

    previous = {sig: signal.signal(sig, relay) for sig in RELAYED}
    try:
        def lower():
            if nice:
                os.nice(nice)
        try:
            child = subprocess.Popen(argv, preexec_fn=lower, pass_fds=(fh.fileno(),))
        except FileNotFoundError:
            print("ameesh heavy: command not found: %s" % argv[0], file=sys.stderr)
            return 127
        _note_child(fh, child.pid)
        for signum in pending:
            relay(signum, None)
        code = child.wait()
        return code if code >= 0 else 128 - code
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        release(fh)


def cmd_heavy(cfg, args) -> int:
    if args.status:
        rows = status(cfg)
        if args.json:
            print(json.dumps({"schema": "ameesh-heavy/1", "limit": len(rows),
                              "slots": rows}, ensure_ascii=False, indent=2))
            return 0
        for row in rows:
            if row["busy"]:
                pid = row.get("pid")
                if row.get("child"):
                    pid = "%s (command %s)" % (pid, row["child"])
                print("slot %d  busy  %s  pid %s  since %s  %s" % (
                    row["slot"], row.get("agent") or "?", pid,
                    time.strftime("%H:%M:%S", time.localtime(row.get("since") or 0)),
                    row.get("command") or ""))
            else:
                print("slot %d  free" % row["slot"])
        return 0
    argv = list(args.command or [])
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: ameesh heavy [--nice N] -- <command> [args…]", file=sys.stderr)
        return 2
    return run(cfg, argv, nice=args.nice)


def add_parser(sub) -> None:
    p = sub.add_parser("heavy", help="run a heavy suite (tests, build) in one of the "
                                     "host's capped slots (L131)")
    p.add_argument("--status", action="store_true", help="show the slots and their holders")
    p.add_argument("--json", action="store_true")
    p.add_argument("--nice", type=int, default=DEFAULT_NICE,
                   help="niceness of the command (default %d, 0: unchanged)" % DEFAULT_NICE)
    p.add_argument("command", nargs="...", help="-- then the command")
    p.set_defaults(func=cmd_heavy)
