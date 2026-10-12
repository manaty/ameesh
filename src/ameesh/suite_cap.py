# SPDX-License-Identifier: AGPL-3.0-only
"""#453 (2) — a test suite a turn launches outside `ameesh heavy` is capped.

On 2026-10-12, on the mesh database host, four agents were paused under
critical pressure, but their jest suites kept running for an hour; the bureau
killed them by hand. `ameesh heavy` has a duration cap (`heavy.max_duration`);
a suite launched without it gets the same cap from the runner, checked every
`CAP_EVERY_S`: past it, the suite is stopped (SIGTERM, then SIGKILL after
`TERM_GRACE_S`) and the agent is told, by mail and in its project thread.

* Candidates: the runner's own descendants (the harnesses of the turns in
  progress, and, the runner being their subreaper, what finished turns left
  behind), never the runner's own group (its psql clients). A process is
  never chosen through a bare historical PID.
* A suite is recognised on its executable and its arguments (`argv[0]`, the
  script an interpreter runs, the `python -m` module, the package manager's
  script), never on the flattened command line; only the top of a suite
  counts, and never under `ameesh heavy` (it has its own cap).
* Signals go through process handles (`pidfd`), opened while the process is
  still the one seen in the process table (same start time): a reused PID is
  never signalled. The suite and all its descendants are taken at SIGTERM
  time; the SIGKILL goes to those handles, so a child that ignores SIGTERM and
  is reparented when the suite exits is still stopped. A suite that leads its
  own process group (neither a harness's nor the runner's) also gets the
  signals on its group (`killpg`), for what it starts in between.
* No database protection on this path: a stopped suite is ended, not frozen,
  and the server releases its locks when its connections close. The freeze of
  the tools under pressure is a separate lot (take 2).
"""
from __future__ import annotations

import errno
import os
import re
import signal
import time
from dataclasses import dataclass, field

from . import platform

#: the cap is checked at most this often (seconds)
CAP_EVERY_S = 30.0
#: SIGTERM, then this delay, then SIGKILL
TERM_GRACE_S = 5.0

#: test runners, by executable or script name (extension dropped)
RUNNERS = frozenset(("jest", "vitest", "mocha", "pytest", "py.test", "tox", "nox"))
#: `python -m <module>` that runs a suite
MODULES = frozenset(("pytest", "unittest", "nose2", "tox", "nox"))
#: package managers whose `test` script (or `run test…`) runs a suite
MANAGERS = frozenset(("npm", "pnpm", "yarn", "bun"))
#: executors of a package (`npx jest`, `pnpm exec vitest`)
EXECUTORS = frozenset(("npx", "pnpx", "bunx"))
#: interpreters: what counts is the script they run (or the module)
INTERPRETERS = re.compile(r"^(python[0-9.]*|node|nodejs|bun|deno|bash|sh|dash|zsh|ksh)$")
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


def _operands(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _script(argv: list[str]) -> tuple[str | None, list[str]]:
    """For an interpreter: (`-m` module or script, the arguments after it);
    (None, []) for inline code (`-c`, `-e`) or nothing to run."""
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-c", "-e", "--eval", "-p", "--print"):
            return None, []
        if arg == "-m" and i + 1 < len(argv):
            return "-m " + argv[i + 1], argv[i + 2:]
        if arg in ("-X", "-W", "-O", "--require", "-r") and i + 1 < len(argv):
            i += 2
            continue
        if not arg.startswith("-"):
            return arg, argv[i + 1:]
        i += 1
    return None, []


def is_suite(argv: list[str]) -> bool:
    """Is `argv` a test suite? On the executable and its arguments only."""
    if not argv:
        return False
    head, rest = _base(argv[0]), argv[1:]
    if head in RUNNERS:
        return True
    if head in ("go", "cargo"):
        return rest[:1] == ["test"]
    if head in EXECUTORS:
        ops = _operands(rest)
        return bool(ops) and _base(ops[0]) in RUNNERS
    if head in MANAGERS:
        ops = _operands(rest)
        if ops[:1] in (["exec"], ["dlx"], ["x"]):
            return len(ops) > 1 and _base(ops[1]) in RUNNERS
        if ops[:1] == ["run"]:
            ops = ops[1:]
        return bool(ops) and (ops[0] == "t" or bool(TEST_SCRIPT.match(ops[0])))
    if INTERPRETERS.match(head):
        script, args = _script(argv)
        if script is None:
            return False
        if script.startswith("-m "):
            return script[3:].split(".")[0] in MODULES
        return _base(script) in RUNNERS or (_base(script) in MANAGERS and is_suite(
            [script] + args))
    return False


def is_heavy(argv: list[str]) -> bool:
    """Is `argv` `ameesh heavy` (its command has its own cap)?"""
    if not argv:
        return False
    head = _base(argv[0])
    if head in ("ameesh", "agent-mesh"):
        return _operands(argv[1:])[:1] == ["heavy"]
    if INTERPRETERS.match(head):
        script, args = _script(argv)
        if script is None:
            return False
        name = script[3:].split(".")[0] if script.startswith("-m ") else _base(script)
        return name in ("ameesh", "agent-mesh") and _operands(args)[:1] == ["heavy"]
    return False


class Handle:
    """A stopped process, held by a pidfd when the OS has them (opened while
    it still had the start time seen in the table): signals never reach a
    reused PID. Without pidfd (ENOSYS), the start time is checked again before
    each signal (a narrow window)."""

    def __init__(self, pid: int, start, fd: int | None) -> None:
        self.pid, self.start, self.fd = pid, start, fd

    @classmethod
    def open(cls, pid: int, start) -> "Handle | None":
        if start is None:
            return None
        fd = None
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                return None
            except OSError as exc:
                if exc.errno != errno.ENOSYS:
                    return None
        try:
            same = platform.start_time(pid) == start
        except Exception:
            same = False
        if not same:
            if fd is not None:
                os.close(fd)
            return None
        return cls(pid, start, fd)

    def send(self, signum: int) -> bool:
        try:
            if self.fd is not None:
                signal.pidfd_send_signal(self.fd, signum)
            else:
                if platform.start_time(self.pid) != self.start:
                    return False
                os.kill(self.pid, signum)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

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
    handles: list = field(default_factory=list)
    #: the suite's own process group, signalled too (None: not its own)
    pgid: int | None = None


def _descendants(table: list[dict], roots) -> set[int]:
    children: dict[int, list[int]] = {}
    for p in table:
        children.setdefault(p["ppid"], []).append(p["pid"])
    out, todo = set(), list(roots)
    while todo:
        pid = todo.pop()
        if pid in out:
            continue
        out.add(pid)
        todo.extend(children.get(pid, ()))
    return out


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

    # -- whose processes ------------------------------------------------------
    def _harnesses(self) -> dict[int, str]:
        out = {}
        for name, worker in dict(getattr(self.runner, "workers", {}) or {}).items():
            proc = getattr(worker, "proc", None)
            if proc is not None:
                out[int(proc.pid)] = name
        return out

    def _agent(self, pid: int, by_pid: dict, harnesses: dict) -> str | None:
        """The agent of a candidate: the harness above it, else the background
        work of a finished turn whose group it is in (None: unknown)."""
        seen, cur = set(), pid
        while cur in by_pid and cur not in seen:
            seen.add(cur)
            if cur in harnesses:
                return harnesses[cur]
            cur = by_pid[cur]["ppid"]
        tracker = getattr(self.runner, "background", None)
        groups = {int(e.pgid): e.agent for e in list(getattr(tracker, "entries", ()) or ())
                  if getattr(e, "pgid", None)}
        return groups.get(by_pid[pid]["pgid"])

    # -- one pass -------------------------------------------------------------
    def cap(self, now: float) -> list[int]:
        """Escalates the suites past their grace, then stops those past the
        cap. Returns the pids sent SIGTERM in this pass."""
        from . import heavy

        for pid, item in list(self.capped.items()):
            if now - item.term_at >= TERM_GRACE_S:
                self._kill(item)
                del self.capped[pid]
        cap = heavy.max_duration(getattr(self.runner, "cfg", None))
        if cap <= 0:
            return []
        table = [p for p in platform.process_table() if not p["zombie"]]
        by_pid = {p["pid"]: p for p in table}
        me = os.getpid()
        try:
            own_group = os.getpgrp()
        except OSError:
            own_group = None
        # the subtrees of the runner's children outside its own group
        # (harnesses, orphans adopted as subreaper); never its psql clients
        roots = {p["pid"] for p in table if p["ppid"] == me and p["pgid"] != own_group}
        candidates = _descendants(table, roots)
        harnesses = self._harnesses()
        argvs: dict[int, list[str]] = {}

        def argv(pid: int) -> list[str]:
            if pid not in argvs:
                try:
                    argvs[pid] = argv_of(platform.command_argv(pid))
                except Exception:
                    argvs[pid] = []
            return argvs[pid]

        def above(pid: int) -> bool:
            """Under `ameesh heavy`, or inside a suite already (not its top)."""
            seen, cur = set(), by_pid[pid]["ppid"]
            while cur in candidates and cur not in seen:
                seen.add(cur)
                if is_heavy(argv(cur)) or is_suite(argv(cur)):
                    return True
                cur = by_pid[cur]["ppid"]
            return False

        stopped: list[int] = []
        for pid in sorted(candidates):
            p = by_pid[pid]
            if pid in self.capped or pid in harnesses or p.get("start") is None:
                continue
            if now - float(p["start"]) < cap or not is_suite(argv(pid)) or above(pid):
                continue
            what = _describe(argv(pid), pid)
            item = self._term(pid, table, harnesses, own_group, now,
                              self._agent(pid, by_pid, harnesses), what)
            if item is None:
                continue
            self.capped[pid] = item
            stopped.append(pid)
            text = ("Test suite stopped after %d min, the duration cap of a heavy command "
                    "(heavy_max_seconds): %s. Run it through ameesh heavy (a slot of the "
                    "host, its own cap: --max-duration), or narrow it."
                    % (int(cap // 60), what))
            self._trace(item.agent, text)
        return stopped

    def _term(self, pid, table, harnesses, own_group, now, agent, what) -> Capped | None:
        """SIGTERM to the suite and its descendants, taken now by handle; to
        its group too if it leads one of its own."""
        by_pid = {p["pid"]: p for p in table}
        leader = Handle.open(pid, by_pid[pid].get("start"))
        if leader is None:
            return None             # gone, or no longer that process
        handles = [leader]
        for victim in sorted(_descendants(table, {pid}) - {pid}):
            h = Handle.open(victim, by_pid[victim].get("start"))
            if h is not None:
                handles.append(h)
        pgid = by_pid[pid]["pgid"]
        group = pgid if (pgid == pid and pgid not in harnesses and pgid != own_group) \
            else None
        if group is not None and leader.send(0):
            try:
                os.killpg(group, signal.SIGTERM)
            except OSError:
                pass
        for h in handles:
            h.send(signal.SIGTERM)
            h.send(signal.SIGCONT)  # a stopped process gets its SIGTERM
        return Capped(agent, what, now, handles, group)

    def _kill(self, item: Capped) -> None:
        """SIGKILL to what is left: the handles taken at SIGTERM time, and the
        suite's group while one of them is still in it."""
        if item.pgid is not None:
            for h in item.handles:
                try:
                    if h.send(0) and os.getpgid(h.pid) == item.pgid:
                        os.killpg(item.pgid, signal.SIGKILL)
                        break
                except OSError:
                    continue
        for h in item.handles:
            h.send(signal.SIGKILL)
            h.close()

    def _trace(self, agent: str | None, text: str) -> None:
        self.log("[%s] %s" % (agent or "?", text))
        if not agent:
            return
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
