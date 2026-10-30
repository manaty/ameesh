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
wrapper keeps the slot until no process of the group is left; a group
that ignores the relayed signal gets SIGKILL after `GROUP_GRACE_S` (#4463). What the
command started and left running when it ended is part of the suite: it gets
SIGTERM, then SIGKILL after `GROUP_GRACE_S`, before the slot is released.
The command is reaped only after that (codex review r3): its zombie keeps
the group's number, so no unrelated group can take it and be signalled. On a
terminal, the command's group gets the foreground while it runs (a suite that
asks a question would otherwise stop on SIGTTIN), and the wrapper takes it
back afterwards.

L131c (codex review r4 of #224): on Linux the wrapper is a child subreaper,
so whatever the command leaves — through a fork chain, or after `setsid` —
is reparented to it; "nothing left" is "no live child but the command",
read at once from the kernel, and each leftover is signalled by pid. The
terminal is the controlling one (`/dev/tty`), even with stdin redirected.
Ctrl-Z stops the command, then the wrapper itself, so the shell's prompt
comes back; `fg` resumes both.
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
#: #453: duration cap of a heavy command (seconds; `AMEESH_HEAVY_MAX_S`,
#: `heavy_max_seconds` of the configuration, `--max-duration`); 0 = none
DEFAULT_MAX_S = 3600.0
#: #453: exit code of a command stopped by the duration cap (as `timeout`)
CAPPED_EXIT = 124


def parse_duration(text) -> float:
    """`90`, `90s`, `45m`, `2h` -> seconds; ValueError otherwise."""
    raw = str(text).strip().lower()
    factor = {"s": 1.0, "m": 60.0, "h": 3600.0}.get(raw[-1:], None)
    value = float(raw[:-1] if factor else raw) * (factor or 1.0)
    if value != value or value < 0 or value == float("inf"):
        raise ValueError(text)
    return value


def max_duration(cfg, requested=None) -> float:
    """#453: the cap of this command: `--max-duration`, else
    `AMEESH_HEAVY_MAX_S`, else `heavy_max_seconds` of the configuration,
    else `DEFAULT_MAX_S`. Unreadable: the default."""
    for raw in (requested, os.environ.get("AMEESH_HEAVY_MAX_S"),
                getattr(cfg, "heavy_max_seconds", None)):
        if raw is None or raw == "":
            continue
        try:
            return parse_duration(raw)
        except ValueError:
            continue
    return DEFAULT_MAX_S


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


def _tree_read(leader: int) -> tuple[list[int], bool] | None:
    """L131c: one read of the wrapper's children other than the command,
    once the wrapper is a child subreaper — every orphaned descendant is
    reparented to it, so a live descendant always means a live child. The
    live pids, and whether a zombie (but the command's) was reaped on the
    way: a child that forked a replacement and exited after the list was
    read had it reparented before turning zombie, so only a read that reaps
    nothing is conclusive (#266 r1). None if the list is not available."""
    from . import platform
    try:
        rows = platform.children()
    except platform.NotAvailable:
        return None
    live = []
    reaped = False
    for row in rows:
        if row["pid"] == leader:
            continue
        if row["zombie"]:
            reaped = platform.reap(row["pid"]) is not None or reaped
            continue
        live.append(row["pid"])
    return live, reaped


def _tree_gone(leader: int, reads: int = 10) -> bool:
    """No live descendant on two consecutive conclusive reads (#266 r1),
    within `reads` reads: a fork churn cannot hold the caller (L131d)."""
    quiet = 0
    for _ in range(reads):
        read = _tree_read(leader)
        if read is None:
            return True
        live, reaped = read
        quiet = quiet + 1 if not live and not reaped else 0
        if quiet >= 2:
            return True
        if live:
            return False
    return False


def _drain_tree(leader: int, *, grace: float | None = None, sleep=time.sleep,
                clock=time.monotonic) -> bool:
    """L131c: stops every descendant the command left (its group or not:
    nothing escapes a subreaper), each signalled by pid — a child's pid
    cannot be reused before its parent reaps it. SIGTERM, then SIGKILL after
    `grace`, then `grace` more; True when none is left on two consecutive
    conclusive reads. Every read signals what it lists and the deadline is
    checked after each one, so a fork churn that keeps reaping zombies
    neither delays the signals nor the escalation (L131d). A descendant that
    may not be signalled (its credentials changed: setuid) is said once and
    the others are still stopped (#266 r1)."""
    grace = GROUP_GRACE_S if grace is None else grace
    refused: set = set()
    for signum in (signal.SIGTERM, signal.SIGKILL):
        sent: set = set()
        deadline = clock() + grace
        quiet = 0
        while True:
            read = _tree_read(leader)
            if read is None:
                return True
            live, reaped = read
            quiet = quiet + 1 if not live and not reaped else 0
            if quiet >= 2:
                return True
            for pid in live:
                if pid not in sent:   # a grandchild reparented since
                    try:
                        os.kill(pid, signum)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        if pid not in refused:
                            refused.add(pid)
                            print("ameesh heavy: process %d left by the command may not "
                                  "be signalled (credentials changed)" % pid,
                                  file=sys.stderr)
                    sent.add(pid)
            if clock() >= deadline:
                break
            if live:
                sleep(0.1)
    return _tree_gone(leader)


def _wait_unreaped(child, tty: int | None = None) -> int:
    """The command's exit code (negative: killed by that signal), waited for
    WITHOUT reaping it (codex review r3): its zombie holds its pid, which is
    also the group's number, until `child.wait()`. With a terminal, a stop of
    the command (Ctrl-Z) suspends the wrapper too (`_suspend`)."""
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        return child.wait()
    while True:
        flags = os.WEXITED | os.WNOWAIT | (os.WSTOPPED if tty is not None else 0)
        info = os.waitid(os.P_PID, child.pid, flags)
        if info.si_code == os.CLD_STOPPED:
            os.waitid(os.P_PID, child.pid, os.WSTOPPED | os.WNOHANG)   # consumed
            _suspend(tty, child.pid)
            continue
        return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status


def _suspend(tty: int, pgid: int) -> None:
    """L131c: Ctrl-Z stopped the command's group. The wrapper takes the
    terminal back and stops too, so the shell, which waits for the wrapper,
    gets its prompt back; on `fg` (SIGCONT, terminal given to the wrapper)
    the command gets the terminal and continues — on `bg` it just
    continues. The terminal is taken only from the command's group: a
    command stopped in the background (SIGTTIN after `bg`) leaves it to
    whoever holds it, the shell (#266 r1)."""
    try:
        if os.tcgetpgrp(tty) == pgid:
            os.tcsetpgrp(tty, os.getpgrp())
    except OSError:
        pass
    os.kill(os.getpid(), signal.SIGSTOP)
    try:
        if os.tcgetpgrp(tty) == os.getpgrp():
            os.tcsetpgrp(tty, pgid)
    except OSError:
        pass
    _killpg(pgid, signal.SIGCONT)


def _foreground_tty() -> int | None:
    """A descriptor of the controlling terminal (`/dev/tty`, whatever stdin
    is: L131c, a command reading /dev/tty with stdin redirected) when the
    wrapper's group holds its foreground; None otherwise."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None                 # no controlling terminal
    try:
        if os.tcgetpgrp(fd) == os.getpgrp():
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def run(cfg, argv: list, *, nice: int = DEFAULT_NICE, max_s: float | None = None) -> int:
    """Runs `argv` holding a heavy slot; its exit code (127 if not found,
    128 + n if killed by signal n, `CAPPED_EXIT` if stopped by the duration
    cap `max_s`, #453: SIGTERM to its group with a message, SIGKILL after
    `GROUP_GRACE_S`). A relayed stop signal is escalated the same way."""
    import threading

    command = shlex.join(argv)
    cap = max_duration(cfg) if max_s is None else float(max_s)
    capped = threading.Event()
    timers: list = []
    held = acquire(cfg, command=command,
                   log=lambda text: print(text, file=sys.stderr, flush=True))
    slot, fh = held
    child = None
    pending: list = []
    stopping: list = []

    def relay(signum, frame):
        if child is None:
            pending.append(signum)
            return
        _killpg(child.pid, signum)  # the group, even once the command is gone
        if not stopping:
            # #4463: a group that ignores the relayed stop would keep the
            # wrapper, and the slot, for ever: SIGKILL after the same grace
            # as the duration cap
            stopping.append(signum)
            leader = child.pid
            later = threading.Timer(GROUP_GRACE_S,
                                    lambda: _killpg(leader, signal.SIGKILL))
            later.daemon = True
            timers.append(later)
            later.start()

    previous = {sig: signal.signal(sig, relay) for sig in RELAYED}
    # L131c: what the command leaves, even through a fork chain or setsid,
    # is reparented to the wrapper, which then knows it exactly
    tree = platform_subreaper()
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
                # the controlling terminal itself (stdin may be redirected)
                fd = os.open("/dev/tty", os.O_RDWR)
                os.tcsetpgrp(fd, os.getpgrp())      # SIGTTOU ignored, inherited
                os.close(fd)
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
        if cap > 0:
            leader = child.pid

            def over() -> None:
                capped.set()
                print("ameesh heavy: duration cap reached (%s): command stopped "
                      "(SIGTERM, then SIGKILL after %ds). Run a narrower suite, or raise "
                      "the cap: ameesh heavy --max-duration <90m|2h|0> -- …"
                      % (_human(cap), int(GROUP_GRACE_S)), file=sys.stderr, flush=True)
                _killpg(leader, signal.SIGCONT)      # a stopped group gets the signal too
                _killpg(leader, signal.SIGTERM)
                later = threading.Timer(GROUP_GRACE_S,
                                        lambda: _killpg(leader, signal.SIGKILL))
                later.daemon = True
                timers.append(later)
                later.start()

            timer = threading.Timer(cap, over)
            timer.daemon = True
            timers.append(timer)
            timer.start()
        if tree and _tree_read(child.pid) is not None:
            code = _wait_unreaped(child, tty)
            drained = _drain_tree(child.pid)
        else:
            # without a process table the leader's zombie would look alive
            # for ever: it is reaped at once, as before codex review r3
            code = _wait_unreaped(child, tty) if _can_scan() else child.wait()
            drained = _drain_group(child.pid)
        if not drained:
            print("ameesh heavy: processes of the command (group %d) survive SIGKILL; "
                  "slot released" % child.pid, file=sys.stderr)
        if capped.is_set():
            return CAPPED_EXIT
        return code if code >= 0 else 128 - code
    finally:
        for timer in timers:
            timer.cancel()
        if tty is not None:
            # L131d: the terminal is taken back only from the command's group
            # (or kept): stopped, sent to the background and left to exit, the
            # command leaves it with the shell, which must keep it
            try:
                if child is None or os.tcgetpgrp(tty) in (os.getpgrp(), child.pid):
                    os.tcsetpgrp(tty, os.getpgrp())
            except OSError:
                pass
            os.close(tty)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child is not None:
            child.wait()            # reaped last: the group's number is free now
        release(fh)


def platform_subreaper() -> bool:
    """The wrapper adopts the orphans of what it runs (`PR_SET_CHILD_SUBREAPER`,
    Linux); False elsewhere, where the group scan stays the rule."""
    from . import platform
    return platform.become_subreaper()


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
        print("usage: ameesh heavy [--nice N] [--max-duration 90m] -- <command> [args…]", file=sys.stderr)
        return 2
    if args.max_duration is not None:
        try:
            parse_duration(args.max_duration)
        except ValueError:
            print("ameesh heavy: invalid --max-duration %r (e.g. 90m, 2h, 0 = no cap)"
                  % args.max_duration, file=sys.stderr)
            return 2
    return run(cfg, argv, nice=args.nice, max_s=max_duration(cfg, args.max_duration))


def _human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return "%d h" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def add_parser(sub) -> None:
    p = sub.add_parser("heavy", help="run a heavy suite (tests, build) in one of the "
                                     "host's capped slots (L131)")
    p.add_argument("--status", action="store_true", help="show the slots and their holders")
    p.add_argument("--json", action="store_true")
    p.add_argument("--nice", type=int, default=DEFAULT_NICE,
                   help="niceness of the command (default %d, 0: unchanged)" % DEFAULT_NICE)
    p.add_argument("--max-duration", default=None,
                   help="duration cap (90m, 2h; 0: none); default AMEESH_HEAVY_MAX_S, "
                        "then heavy_max_seconds of the configuration, then %d min (#453)"
                        % int(DEFAULT_MAX_S // 60))
    p.add_argument("command", nargs="...", help="-- then the command")
    p.set_defaults(func=cmd_heavy)
