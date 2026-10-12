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
slot lives as long as the command, even if the wrapper is killed. The command
runs in its own process group (codex review r2, P2): SIGTERM, SIGINT and
SIGHUP sent to the wrapper are relayed to the whole group — a suite's workers
too, which do not inherit the descriptor (Python closes it) — and the
wrapper keeps the slot until no process of the group is left. What the
command started and left running when it ended is part of the suite: it gets
SIGTERM, then SIGKILL after `GROUP_GRACE_S`, before the slot is released.
The command is reaped only after that (codex review r3): its zombie keeps
the group's number, so no unrelated group can take it and be signalled. On a
terminal, the command's group gets the foreground while it runs (a suite that
asks a question would otherwise stop on SIGTTIN), and the wrapper takes it
back afterwards.
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
#: seconds the command's process group has after SIGTERM, then after SIGKILL
GROUP_GRACE_S = 10.0


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


def _killpg(pgid: int, signum: int) -> bool:
    """Signals the command's process group; False once it has no process."""
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return False
    except PermissionError:         # a member changed its credentials: still there
        pass
    return True


def _can_scan() -> bool:
    """The platform layer can list processes with their group and state
    (psutil, or Linux /proc); otherwise only kill(0) tells a group apart."""
    from . import platform
    try:
        platform.process_table()
    except platform.NotAvailable:
        return False
    return True


def _group_alive(pgid: int) -> bool:
    """A process of the group `pgid` still runs. A zombie does not count: its
    reaper (init, a subreaper) may take its time, the process is gone.
    Without a process table (`platform.NotAvailable`), kill(0) decides."""
    from . import platform
    if not _killpg(pgid, 0):
        return False
    try:
        table = platform.process_table()
    except platform.NotAvailable:
        return True
    return any(row["pgid"] == pgid and not row["zombie"] for row in table)


def _group_gone(pgid: int) -> bool:
    """No live process of the group on two consecutive scans (codex review
    r3): a worker that forks a replacement and exits between the listing of
    /proc and the read of its stat escapes one scan, not two."""
    return not _group_alive(pgid) and not _group_alive(pgid)


def _drain_group(pgid: int, *, grace: float | None = None, sleep=time.sleep,
                 clock=time.monotonic) -> bool:
    """Waits until no process of the group `pgid` is left: SIGTERM to what
    the command left running, SIGKILL after `grace`, then `grace` more. True
    when the group is gone. The caller keeps the group's leader unreaped."""
    grace = GROUP_GRACE_S if grace is None else grace
    for signum in (signal.SIGTERM, signal.SIGKILL):
        if _group_gone(pgid) or not _killpg(pgid, signum):
            return True
        deadline = clock() + grace
        while clock() < deadline:
            if _group_gone(pgid):
                return True
            sleep(0.1)
    return _group_gone(pgid)


def _wait_unreaped(child) -> int:
    """The command's exit code (negative: killed by that signal), waited for
    WITHOUT reaping it (codex review r3): its zombie holds its pid, which is
    also the group's number, until `child.wait()`."""
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        return child.wait()
    info = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
    return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status


def _foreground_tty() -> int | None:
    """stdin's descriptor when it is the controlling terminal and the
    wrapper's group holds its foreground; None otherwise."""
    try:
        if os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp():
            return 0
    except OSError:
        pass
    return None


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
        else:
            _killpg(child.pid, signum)  # the group, even once the command is gone

    previous = {sig: signal.signal(sig, relay) for sig in RELAYED}
    tty = _foreground_tty()
    if tty is not None:
        # handing the terminal over and taking it back from the background
        # would otherwise stop the wrapper (SIGTTOU)
        previous[signal.SIGTTOU] = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    ttou = previous.get(signal.SIGTTOU, signal.SIG_DFL)
    ttou = ttou if ttou in (signal.SIG_DFL, signal.SIG_IGN) else signal.SIG_DFL
    try:
        def lower():
            # its own process group (pgid = its pid), set before exec — Popen
            # returns after the exec, the group exists when signals are relayed
            os.setpgid(0, 0)
            if tty is not None:
                os.tcsetpgrp(tty, os.getpgrp())     # SIGTTOU ignored, inherited
                signal.signal(signal.SIGTTOU, ttou)
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
        # without a process table the leader's zombie would look alive for
        # ever: it is reaped at once, as before codex review r3
        code = _wait_unreaped(child) if _can_scan() else child.wait()
        if not _drain_group(child.pid):
            print("ameesh heavy: processes of the command (group %d) survive SIGKILL; "
                  "slot released" % child.pid, file=sys.stderr)
        return code if code >= 0 else 128 - code
    finally:
        if tty is not None:
            try:
                os.tcsetpgrp(tty, os.getpgrp())
            except OSError:
                pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child is not None:
            child.wait()            # reaped last: the group's number is free now
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
