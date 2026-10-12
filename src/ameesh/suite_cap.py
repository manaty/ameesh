# SPDX-License-Identifier: AGPL-3.0-only
"""#453 (2) — a test suite a turn launches outside `ameesh heavy` is capped.

On 2026-10-12, on the mesh database host, four agents were paused under
critical pressure, but their jest suites kept running for an hour; the bureau
killed them by hand. `ameesh heavy` has a duration cap (`heavy.max_duration`);
a suite launched without it gets the same cap from the runner, checked every
`CAP_EVERY_S`: past it, the suite is stopped (SIGTERM, then SIGKILL after
`TERM_GRACE_S`) and the agent is told, by mail and in its project thread.

* A suite is recognised on its executable and its arguments (`argv[0]`, the
  script an interpreter runs, the `python -m` module, the package manager's
  script), never on the flattened command line; an option whose value could
  be read as the command (`npm --prefix test install`) makes it no suite.
  Only the top of a suite counts, and never under `ameesh heavy` (it has its
  own cap).
* Every signal goes through a process handle (`pidfd`), opened while the
  process still has the start time seen in the process table: a reused PID
  is never signalled. Without pidfd (no API, or ENOSYS from the kernel),
  nothing is signalled (the cap is off on that host, said once in the log). No process group is signalled.
* The process table is only a list of candidates: every link is checked
  again, live. A suite is stopped only if, parent by parent, re-read while
  each one is held by a handle, it descends from the runner through a child
  outside the runner's own group (its psql clients); its descendants are
  taken the same way, at SIGTERM time and again at SIGKILL time, so a child
  that ignores SIGTERM, even reparented once the suite is gone, is stopped.
* The trace (thread note, mail) is written by a single trace thread, from a
  bounded queue (the oldest trace dropped, and counted, when it is full): a
  lock a suite holds never delays the SIGKILL nor the next checks, and never
  piles up threads.
* No database protection on this path: a stopped suite is ended, not frozen,
  and the server releases its locks when its connections close. The freeze of
  the tools under pressure is a separate lot (#460).
"""
from __future__ import annotations

import errno
import os
import queue
import re
import signal
import threading
import time
from dataclasses import dataclass, field

from . import platform

#: the cap is checked at most this often (seconds)
CAP_EVERY_S = 30.0
#: SIGTERM, then this delay, then SIGKILL
TERM_GRACE_S = 5.0
#: traces waiting for the trace thread at most
TRACE_QUEUE = 32

#: test runners, by executable or script name (extension dropped)
RUNNERS = frozenset(("jest", "vitest", "mocha", "pytest", "py.test", "tox", "nox"))
#: `python -m <module>` that runs a suite
MODULES = frozenset(("pytest", "unittest", "nose2", "tox", "nox"))
#: package managers whose `test` script (or `run test…`) runs a suite
MANAGERS = frozenset(("npm", "pnpm", "yarn", "bun"))
#: executors of a package (`npx jest`)
EXECUTORS = frozenset(("npx", "pnpx", "bunx"))
#: options of a package manager or executor known to take no value; any
#: other option without `=` may take the next word: then no suite is read
MANAGER_FLAGS = frozenset(("-s", "--silent", "-q", "--quiet", "--verbose", "-d",
                           "--if-present", "-r", "--recursive", "--color", "--no-color",
                           "--offline", "--frozen-lockfile", "-y", "--yes", "--no"))
#: interpreters: what counts is the script they run (or the module), by
#: family: the options that take a value, the options that run inline code
PYTHON = re.compile(r"^python[0-9.]*$")
FAMILIES = {
    "python": ({"-X", "-W", "--check-hash-based-pycs"}, {"-c"}),
    "node": ({"-r", "--require", "--import", "--loader", "--experimental-loader", "-C",
              "--conditions", "--title", "--input-type"},
             {"-e", "--eval", "-p", "--print"}),
    "shell": ({"-o", "-O", "+o", "+O"}, {"-c"}),
}
SHELLS = frozenset(("bash", "sh", "dash", "zsh", "ksh"))
SCRIPT_EXT = re.compile(r"\.(js|mjs|cjs|ts|py)$")
TEST_SCRIPT = re.compile(r"^test(:.*)?$")


def argv_of(raw: list[str]) -> list[str]:
    """The arguments of a process as read from the OS. A program that sets
    its title (`process.title`: npm shows `npm test`) leaves one string in
    place of its arguments: split back into words."""
    if len(raw) == 1 and " " in raw[0].strip():
        return raw[0].split()
    return list(raw)


def _base(arg: str) -> str:
    return SCRIPT_EXT.sub("", os.path.basename(arg or ""))


def _family(head: str) -> str | None:
    if PYTHON.match(head):
        return "python"
    if head in ("node", "nodejs"):
        return "node"
    if head in SHELLS:
        return "shell"
    return None


def _command(args: list[str], flags=MANAGER_FLAGS) -> tuple[str | None, list[str]]:
    """The first word of `args` that is a command, and what follows it; None
    when an option that may take a value stands before it (its value could
    be read as the command), or at `--`."""
    for i, arg in enumerate(args):
        if arg == "--":
            return None, []
        if arg.startswith("-") or arg.startswith("+"):
            if "=" in arg or arg in flags:
                continue
            return None, []
        return arg, args[i + 1:]
    return None, []


def _script(argv: list[str]) -> tuple[str | None, list[str]]:
    """For an interpreter: (`-m` module or script, the arguments after it);
    (None, []) for inline code or nothing to run."""
    family = _family(_base(argv[0]))
    if family is None:
        return None, []
    valued, inline = FAMILIES[family]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in inline:
            return None, []
        if family == "python" and arg == "-m":
            return ("-m " + argv[i + 1], argv[i + 2:]) if i + 1 < len(argv) else (None, [])
        if arg in valued:
            i += 2
            continue
        if not arg.startswith("-") and not arg.startswith("+"):
            return arg, argv[i + 1:]
        i += 1
    return None, []


def _manager_suite(head: str, rest: list[str]) -> bool:
    cmd, after = _command(rest)
    if cmd is None:
        return False
    if cmd in ("exec", "dlx", "x"):
        runner, _ = _command(after)
        return runner is not None and _base(runner) in RUNNERS
    if cmd in ("run", "run-script"):
        cmd, _ = _command(after)
        if cmd is None:
            return False
    return cmd == "t" or bool(TEST_SCRIPT.match(cmd))


def is_suite(argv: list[str]) -> bool:
    """Is `argv` a test suite? On the executable and its arguments only."""
    if not argv:
        return False
    head, rest = _base(argv[0]), argv[1:]
    if head in RUNNERS:
        return True
    if head in ("go", "cargo", "deno"):
        cmd, _ = _command(rest, flags=frozenset())
        return cmd == "test"
    if head in EXECUTORS:
        runner, _ = _command(rest)
        return runner is not None and _base(runner) in RUNNERS
    if head in MANAGERS:
        return _manager_suite(head, rest)
    script, args = _script(argv)
    if script is None:
        return False
    if script.startswith("-m "):
        return script[3:].split(".")[0] in MODULES
    name = _base(script)
    if name in RUNNERS:
        return True
    return name in MANAGERS and _manager_suite(name, args)


def is_heavy(argv: list[str]) -> bool:
    """Is `argv` `ameesh heavy` (its command has its own cap)?"""
    if not argv:
        return False
    head = _base(argv[0])
    if head in ("ameesh", "agent-mesh"):
        return _command(argv[1:], flags=frozenset())[0] == "heavy"
    script, args = _script(argv)
    if script is None:
        return False
    name = script[3:].split(".")[0] if script.startswith("-m ") else _base(script)
    return name in ("ameesh", "agent-mesh") and \
        _command(args, flags=frozenset())[0] == "heavy"


#: set when the kernel refuses pidfd at run time (ENOSYS): the cap is off
_PIDFD_ENOSYS = False


def pidfd_supported() -> bool:
    return (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
            and not _PIDFD_ENOSYS)


class Handle:
    """A process held by a pidfd: signals never reach a reused PID. No
    handle without pidfd support (nothing is signalled then)."""

    def __init__(self, pid: int, fd: int) -> None:
        self.pid, self.fd = pid, fd

    @classmethod
    def open(cls, pid: int, start) -> "Handle | None":
        """The handle of `pid` if it is still the process that had `start`
        (exact start time, as in the table) — None otherwise."""
        if start is None:
            return None
        h = cls.open_any(pid)
        if h is None:
            return None
        try:
            same = platform.start_time(pid) == start
        except Exception:
            same = False
        if not same:
            h.close()
            return None
        return h

    @classmethod
    def open_any(cls, pid: int) -> "Handle | None":
        """The handle of whatever process has `pid` now (its identity is
        then established by the caller, e.g. as a live parent)."""
        global _PIDFD_ENOSYS
        if not pidfd_supported() or not pid or pid <= 0:
            return None
        try:
            return cls(pid, os.pidfd_open(pid))
        except OSError as exc:
            if exc.errno == errno.ENOSYS:
                _PIDFD_ENOSYS = True    # the API exists, the kernel has none
            return None

    def send(self, signum: int) -> bool:
        if self.fd is None:
            return False
        try:
            signal.pidfd_send_signal(self.fd, signum)
            return True
        except OSError:
            return False

    def alive(self) -> bool:
        return self.send(0)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


@dataclass
class Capped:
    agent: str | None
    what: str
    term_at: float
    #: the suite and its descendants, by pid (checked links only)
    handles: dict = field(default_factory=dict)


def lineage(h: Handle, me: int, own_group: int | None) -> list[tuple[int, list[str]]] | None:
    """The ancestors of the process held by `h` up to the runner `me`, as
    (pid, argv), nearest first, each link checked live: the parent read now,
    then held by a handle, then the child's parent read again — a parent
    cannot be replaced by a younger process, so the handle holds the real
    parent. None if a link breaks, if the chain does not reach `me`, or if it
    reaches it through the runner's own group."""
    held: list[Handle] = []
    out: list[tuple[int, list[str]]] = []
    cur = h
    try:
        for _ in range(64):
            ppid = platform.parent_pid(cur.pid)
            if ppid is None or not cur.alive():
                return None
            if ppid == me:
                try:
                    group = os.getpgid(cur.pid)
                except OSError:
                    return None
                return out if cur.alive() and group != own_group else None
            if ppid <= 1:
                return None
            parent = Handle.open_any(ppid)
            if parent is None:
                return None
            held.append(parent)
            if platform.parent_pid(cur.pid) != ppid or not cur.alive():
                return None
            argv = argv_of(platform.command_argv(ppid))
            if not parent.alive():
                return None
            out.append((ppid, argv))
            cur = parent
        return None
    finally:
        for parent in held:
            parent.close()


def _children(table: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for p in table:
        out.setdefault(p["ppid"], []).append(p)
    return out


def gather(held: dict[int, Handle], table: list[dict]) -> None:
    """Adds to `held` the descendants of its live processes, each one checked:
    held by a handle with the table's start time, its parent re-read and
    found to be a held process still alive."""
    children = _children(table)
    todo = [pid for pid, h in held.items() if h.alive()]
    while todo:
        parent = todo.pop()
        for p in children.get(parent, ()):
            pid = p["pid"]
            if pid in held:
                continue
            h = Handle.open(pid, p.get("start"))
            if h is None:
                continue
            if platform.parent_pid(pid) != parent or not held[parent].alive():
                h.close()
                continue
            held[pid] = h
            todo.append(pid)


class SuiteCap:
    """The runner's cap of test suites outside `ameesh heavy` (one per
    runner); `tick` never raises."""

    def __init__(self, runner, log=print, *, clock=time.time) -> None:
        self.runner = runner
        self.log = log
        self.clock = clock
        #: suites sent SIGTERM, waiting for their SIGKILL, by suite pid
        self.capped: dict[int, Capped] = {}
        self._at = float("-inf")
        self._said_off = False
        #: traces waiting for the single trace thread (bounded: the oldest is
        #: dropped, and counted, when a lock holds the thread)
        self._traces: queue.Queue = queue.Queue(maxsize=TRACE_QUEUE)
        self.traces_dropped = 0
        self._tracer: threading.Thread | None = None
        self._trace_lock = threading.Lock()

    def _say_off(self) -> None:
        if not self._said_off:
            self._said_off = True
            self.log("test suite cap off on this host: no pidfd (a PID could be "
                     "reused between a check and a signal)")

    def tick(self, now: float | None = None) -> list[int]:
        now = self.clock() if now is None else float(now)
        if not self.capped and now - self._at < CAP_EVERY_S:
            return []
        self._at = now
        try:
            return self.cap(now)
        except Exception as exc:
            self.log("test suite cap failed (%s)" % exc)
            return []

    def _harnesses(self) -> dict[int, str]:
        out = {}
        for name, worker in dict(getattr(self.runner, "workers", {}) or {}).items():
            proc = getattr(worker, "proc", None)
            if proc is not None:
                out[int(proc.pid)] = name
        return out

    def _agent(self, h: Handle, ancestors, harnesses: dict) -> str | None:
        """The agent of a suite: the harness among its checked ancestors, else
        the background work of a finished turn whose group it is in (None:
        unknown; only the message depends on it)."""
        for pid, _ in ancestors:
            if pid in harnesses:
                return harnesses[pid]
        try:
            group = os.getpgid(h.pid)
        except OSError:
            return None
        if not h.alive():
            return None
        tracker = getattr(self.runner, "background", None)
        for e in list(getattr(tracker, "entries", ()) or ()):
            if getattr(e, "pgid", None) and int(e.pgid) == group:
                return e.agent
        return None

    def cap(self, now: float) -> list[int]:
        """Escalates the suites past their grace, then stops those past the
        cap. Returns the pids sent SIGTERM in this pass."""
        from . import heavy

        if not pidfd_supported():
            self._say_off()
            return []
        if self.capped:
            table = [p for p in platform.process_table() if not p["zombie"]]
            for pid, item in list(self.capped.items()):
                if now - item.term_at >= TERM_GRACE_S:
                    self._kill(item, table)
                    del self.capped[pid]
        cap = heavy.max_duration(getattr(self.runner, "cfg", None))
        if cap <= 0:
            return []
        table = [p for p in platform.process_table() if not p["zombie"]]
        me = os.getpid()
        try:
            own_group = os.getpgrp()
        except OSError:
            own_group = None
        harnesses = self._harnesses()
        # candidates only: the subtrees of the runner's children outside its
        # group, as the table shows them; every link is checked live below
        children = _children(table)
        todo = [p["pid"] for p in children.get(me, ()) if p["pgid"] != own_group]
        # the runner's children themselves too: an orphaned suite it adopted
        # as subreaper is one of them (a harness is never a suite)
        candidates: dict[int, dict] = {p["pid"]: p for p in children.get(me, ())
                                       if p["pgid"] != own_group}
        while todo:
            pid = todo.pop()
            for p in children.get(pid, ()):
                if p["pid"] not in candidates:
                    candidates[p["pid"]] = p
                    todo.append(p["pid"])
        stopped: list[int] = []
        for pid, p in sorted(candidates.items()):
            if pid in self.capped or pid in harnesses or p.get("start") is None:
                continue
            if now - float(p["start"]) < cap:
                continue
            try:
                if not is_suite(argv_of(platform.command_argv(pid))):
                    continue
            except Exception:
                continue
            h = Handle.open(pid, p["start"])
            if h is None:
                if not pidfd_supported():
                    self._say_off()
                    break
                continue
            item = None
            try:
                argv = argv_of(platform.command_argv(pid))
                ancestors = lineage(h, me, own_group) if h.alive() else None
                if ancestors is None or not is_suite(argv):
                    continue
                if any(is_heavy(a) or is_suite(a) for _, a in ancestors):
                    continue    # under ameesh heavy, or inside a suite already
                what = _describe(argv, pid)
                item = Capped(self._agent(h, ancestors, harnesses), what, now, {pid: h})
                self._term(item, table)
            finally:
                if item is None:
                    h.close()
            self.capped[pid] = item
            stopped.append(pid)
            self._trace(item.agent, (
                "Test suite stopped after %d min, the duration cap of a heavy command "
                "(heavy_max_seconds): %s. Run it through ameesh heavy (a slot of the "
                "host, its own cap: --max-duration), or narrow it."
                % (int(cap // 60), item.what)))
        return stopped

    def _term(self, item: Capped, table: list[dict]) -> None:
        """SIGTERM to the suite and its descendants, each held by a handle."""
        gather(item.handles, table)
        for h in item.handles.values():
            h.send(signal.SIGTERM)
            h.send(signal.SIGCONT)  # a stopped process gets its SIGTERM

    def _kill(self, item: Capped, table: list[dict]) -> None:
        """SIGKILL to what is left: the handles taken at SIGTERM time (a
        reparented child included), and the descendants started since."""
        try:
            gather(item.handles, table)
        except Exception as exc:
            self.log("test suite cap: descendants unreadable (%s)" % exc)
        for h in item.handles.values():
            h.send(signal.SIGKILL)
            h.close()

    def _trace(self, agent: str | None, text: str) -> None:
        """Logged at once; the thread note and the mail by the single trace
        thread (a lock a stopped suite holds never delays the SIGKILL, and
        never piles up threads: a full queue drops its oldest trace)."""
        self.log("[%s] %s" % (agent or "?", text))
        if not agent:
            return
        with self._trace_lock:
            while True:
                try:
                    self._traces.put_nowait((agent, text))
                    break
                except queue.Full:
                    try:
                        self._traces.get_nowait()
                        self.traces_dropped += 1
                        self.log("test suite cap: oldest trace dropped (%d so far)"
                                 % self.traces_dropped)
                    except queue.Empty:
                        pass
            if self._tracer is None or not self._tracer.is_alive():
                self._tracer = threading.Thread(target=self._write_traces, daemon=True,
                                                name="plafond-trace")
                self._tracer.start()

    def _write_traces(self) -> None:
        while True:
            try:
                agent, text = self._traces.get(timeout=60)
            except queue.Empty:
                with self._trace_lock:
                    if self._traces.empty():
                        self._tracer = None
                        return
                continue
            worker = (getattr(self.runner, "workers", {}) or {}).get(agent)
            if worker is not None:
                try:
                    worker._fil_note(text, meta={"action": "plafond-suite",
                                                 "hote": str(getattr(self.runner, "host", ""))})
                except Exception:
                    pass
            try:
                from . import mail as mail_mod
                mail_mod.send(self.runner.db, "ameesh", agent, text, kind="event")
            except Exception as exc:
                self.log("[%s] test suite stop not mailed (%s)" % (agent, exc))


def _describe(argv: list[str], pid: int) -> str:
    line = " ".join(argv)
    if len(line) > 60:
        line = line[:59] + "…"
    return "pid %d%s" % (pid, " \"%s\"" % line if line else "")
