# SPDX-License-Identifier: AGPL-3.0-only
"""#453 — a pressure pause relieves the host: the tool processes are frozen.

On 2026-10-12, on the mesh database host, four agents were paused under
critical pressure, but their jest suites kept running for an hour: the pause
(L31, 0028) only held the NEXT turn, never the work in progress, and the
bureau killed the suites by hand.

Now, while the host's pressure is critical, the runner freezes (SIGSTOP) the
tool processes of every agent the pressure pauses (same rule as
`AgentWorker._held_by_pressure`, priority 0 — the orchestrator and the
auditor exemptions hold):

* the turn in progress: the processes the harness started for its tools
  (shells, or any child started after the harness's own start-up), and all
  their descendants, whatever their process group (`ameesh heavy` runs its
  command in a group of its own), plus what the turn detached into its group;
  never the harness itself nor the children it started with (MCP servers),
  so the session stays alive and the tool call resumes where it stopped;
* the background work of finished turns in their grace period
  (`background.Tracker`): the members of their group and their descendants.

When the pressure is no longer critical, or the agent is no longer paused by
it, everything frozen is resumed (SIGCONT). Frozen longer than
`pressure_freeze_max` (default 15 min, `AMEESH_PRESSURE_FREEZE_MAX_S`, 0 = no
limit), the processes are stopped (SIGTERM with SIGCONT, then SIGKILL): the
host does not wait for ever. Each freeze, resume and stop is logged, written
to the agent's project thread, and a stop is mailed to the agent. A process
is signalled only if its start time is still the one seen when it was frozen
(no signal to a reused pid).
"""
from __future__ import annotations

import os
import re
import signal
import time
from dataclasses import dataclass

from . import platform

#: frozen longer than this, the processes are stopped (seconds)
DEFAULT_FREEZE_MAX_S = 900.0
#: SIGTERM, then this delay, then SIGKILL
TERM_GRACE_S = 5.0
#: shells a harness runs its tools in (as `background.SHELLS`)
SHELLS = ("bash", "sh", "dash", "zsh", "ksh", "mksh", "fish")
#: a direct child of the harness started within this delay of it is part of
#: the harness (MCP servers start with it); later, it is a tool (`bash -c`
#: execs a single command: a tool is not always a shell)
HARNESS_STARTUP_S = 10.0


#: #453 (2): a test suite launched by a turn outside `ameesh heavy`, recognised
#: by its command line; capped like a heavy command (`heavy.max_duration`)
TEST_COMMAND = re.compile(
    r"(^|[/\s])(jest|vitest|mocha|pytest|py\.test|tox|nox)(\s|$)"
    r"|\s-m\s+(unittest|pytest)(\s|$)"
    r"|(^|[/\s])(npm|pnpm|yarn|bun)(\s+run)?\s+test(\s|:|$)"
    r"|(^|[/\s])(go|cargo)\s+test(\s|$)")
#: `ameesh heavy` in a command line: its command has its own cap
HEAVY_COMMAND = re.compile(r"(^|[/\s])(ameesh|agent-mesh)(\.py)?\s+heavy(\s|$)"
                           r"|\s-m\s+ameesh\s+heavy(\s|$)")
#: the test-suite cap is checked at most this often (seconds)
CAP_EVERY_S = 30.0


@dataclass
class Frozen:
    pid: int
    start: float | None
    agent: str
    since: float
    what: str
    term_at: float | None = None


def freeze_max(cfg) -> float:
    raw = os.environ.get("AMEESH_PRESSURE_FREEZE_MAX_S")
    if raw in (None, ""):
        raw = getattr(cfg, "pressure_freeze_max", DEFAULT_FREEZE_MAX_S)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_FREEZE_MAX_S
    return value if value >= 0 and value == value else DEFAULT_FREEZE_MAX_S


#: never frozen: a stopped database client could hold a lock everyone waits on
DB_CLIENTS = ("psql", "pg_dump", "pg_restore", "postgres", "postmaster")


def db_port(cfg) -> int:
    """The port of the mesh database (`cfg.dsn`, URL or key=value), 5432 by
    default."""
    dsn = str(getattr(cfg, "dsn", "") or "")
    try:
        if "://" in dsn:
            from urllib.parse import urlsplit
            port = urlsplit(dsn).port
            if port:
                return int(port)
        for token in dsn.split():
            key, _, value = token.partition("=")
            if key == "port" and value.isdigit():
                return int(value)
    except ValueError:
        pass
    return 5432


def _db_sockets(port: int, proc: str | None = None) -> set[str]:
    """Inodes of the host's TCP sockets connected to `port`."""
    try:
        return platform.tcp_sockets_to(port, proc=proc)
    except Exception:
        return set()


def holds_db(pid: int, name: str, inodes: set[str], proc: str | None = None) -> bool:
    """Does the process talk to the mesh database (a client by name, or a TCP
    socket to its port)? Unreadable: it is spared (False only when sure)."""
    if (name or "") in DB_CLIENTS:
        return True
    if not inodes:
        return False
    mine = platform.socket_inodes(pid, proc=proc)
    return True if mine is None else bool(mine & inodes)


def _shell(name: str) -> bool:
    return (name or "").lstrip("-") in SHELLS


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


def _runner_own(table: list[dict]) -> set[int]:
    """The runner and its own group (its database clients, psql): never
    frozen nor stopped (mesh-design, #453)."""
    try:
        group = os.getpgrp()
    except OSError:
        group = None
    return {os.getpid()} | {p["pid"] for p in table if group is not None
                            and p["pgid"] == group}


def turn_tools(table: list[dict], leader: int) -> set[int]:
    """The tool processes of a turn in progress whose harness is `leader`
    (also its group's number): its direct children that are shells or that
    started after `HARNESS_STARTUP_S`, and the roots detached into the group,
    with all their descendants. Never the harness, nor a child started with it
    that is not a shell (MCP server)."""
    live = [p for p in table if not p["zombie"]]
    head = next((p for p in live if p["pid"] == leader), None)
    if head is None:
        return set()

    def tool(p: dict) -> bool:
        if _shell(p["name"]):
            return True
        if p.get("start") is None or head.get("start") is None:
            return False
        return float(p["start"]) - float(head["start"]) >= HARNESS_STARTUP_S

    roots = {p["pid"] for p in live if p["ppid"] == leader and tool(p)}
    under = _descendants(live, {leader})
    roots |= {p["pid"] for p in live if p["pgid"] == leader and p["pid"] not in under}
    return _descendants(live, roots) - {leader} - _runner_own(table)


def group_tools(table: list[dict], pgid: int, lineage=()) -> set[int]:
    """The background work of a finished turn: the members of its group and
    of its known lineage, with their descendants."""
    live = [p for p in table if not p["zombie"]]
    roots = {p["pid"] for p in live if p["pgid"] == pgid or p["pid"] in set(lineage)}
    return _descendants(live, roots) - _runner_own(table)


class Freezer:
    """The runner's freezer (one per runner); `tick` never raises."""

    def __init__(self, runner, log=print, *, kill=os.kill, clock=time.time) -> None:
        self.runner = runner
        self.log = log
        self.kill = kill
        self.clock = clock
        self.frozen: dict[int, Frozen] = {}
        #: #453 (2): suites stopped by the cap, `{pid: (start, SIGTERM at)}`
        self.capped: dict[int, tuple] = {}
        self._cap_at = float("-inf")

    # -- what the pressure pauses -------------------------------------------
    def _paused(self, worker, verdict: dict) -> bool:
        if worker is None:
            return False
        try:
            if not worker._held_by_pressure(verdict):
                return False
        except Exception:
            return False
        return int((worker.agent or {}).get("priority") or 0) <= 0

    def targets(self, table: list[dict], verdict: dict) -> dict[int, tuple[str, str]]:
        """`{pid: (agent, what)}` to freeze now."""
        out: dict[int, tuple[str, str]] = {}
        if not verdict.get("critical"):
            return out
        workers = dict(getattr(self.runner, "workers", {}) or {})
        for name, worker in workers.items():
            proc = getattr(worker, "proc", None)
            if proc is None or not self._paused(worker, verdict):
                continue
            for pid in turn_tools(table, proc.pid):
                out[pid] = (name, "turn in progress")
        tracker = getattr(self.runner, "background", None)
        for entry in list(getattr(tracker, "entries", ()) or ()):
            if entry.state != "running" or not entry.pgid:
                continue
            if not self._paused(workers.get(entry.agent), verdict):
                continue
            for pid in group_tools(table, entry.pgid, entry.lineage):
                out.setdefault(pid, (entry.agent, "background work of turn %s"
                                     % entry.turn_id[:8]))
        return out

    # -- one pass -------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else float(now)
        try:
            verdict = self.runner.host_pressure()
        except Exception:
            verdict = {}
        cap_due = bool(self.capped) or now - self._cap_at >= CAP_EVERY_S
        if not verdict.get("critical") and not self.frozen and not cap_due:
            return
        try:
            table = platform.process_table()
        except Exception:
            return
        if cap_due:
            self._cap_at = now
            try:
                self.cap_tests(table, now)
            except Exception as exc:
                self.log("test suite cap failed (%s)" % exc)
        if not verdict.get("critical") and not self.frozen:
            return
        starts = {p["pid"]: p.get("start") for p in table if not p["zombie"]}
        try:
            wanted = self.targets(table, verdict)
        except Exception as exc:
            self.log("pressure freeze: targets unreadable (%s)" % exc)
            wanted = {}
        frozen_now: dict[str, list[str]] = {}
        names = {p["pid"]: p["name"] for p in table}
        inodes = _db_sockets(db_port(getattr(self.runner, "cfg", None))) \
            if any(pid not in self.frozen for pid in wanted) else set()
        for pid, (agent, what) in wanted.items():
            if pid in self.frozen:
                continue
            if holds_db(pid, names.get(pid, ""), inodes):
                continue        # mesh-design: a stopped client may hold a lock
            if self._signal(pid, starts.get(pid), signal.SIGSTOP):
                self.frozen[pid] = Frozen(pid, starts.get(pid), agent, now,
                                          _describe(pid, what))
                frozen_now.setdefault(agent, []).append(self.frozen[pid].what)
        maximum = freeze_max(getattr(self.runner, "cfg", None))
        for agent, whats in frozen_now.items():
            self._trace(agent, "Critical pressure on host %s: %d tool process(es) frozen "
                        "(SIGSTOP), resumed when the pressure goes down%s: %s." % (
                            self._host(), len(whats),
                            "; still frozen after %d min, they are stopped (SIGTERM, then "
                            "SIGKILL)" % int(maximum // 60) if maximum > 0 else "",
                            _list(whats)), action="gel", mail=True)
        thawed: dict[str, list[str]] = {}
        stopped: dict[str, list[str]] = {}
        for pid, item in list(self.frozen.items()):
            if starts.get(pid, "gone") == "gone" or not _same(item.start, starts.get(pid)):
                del self.frozen[pid]          # ended (killed, or its parent reaped it)
                continue
            if item.term_at is not None:
                if now - item.term_at >= TERM_GRACE_S:
                    self._signal(pid, item.start, signal.SIGKILL)
                    del self.frozen[pid]
                continue
            if pid not in wanted:
                if self._signal(pid, item.start, signal.SIGCONT):
                    thawed.setdefault(item.agent, []).append(item.what)
                del self.frozen[pid]
                continue
            if maximum > 0 and now - item.since >= maximum:
                self._signal(pid, item.start, signal.SIGTERM)
                self._signal(pid, item.start, signal.SIGCONT)
                item.term_at = now
                stopped.setdefault(item.agent, []).append(item.what)
        for agent, whats in thawed.items():
            self._trace(agent, "Pressure on host %s no longer critical: %d tool process(es) "
                        "resumed (SIGCONT): %s." % (self._host(), len(whats), _list(whats)),
                        action="degel")
        for agent, whats in stopped.items():
            text = ("Critical pressure on host %s for more than %d min: %d frozen tool "
                    "process(es) stopped (SIGTERM, then SIGKILL): %s. Run them again once "
                    "the host has room, through ameesh heavy for a suite."
                    % (self._host(), int(maximum // 60), len(whats), _list(whats)))
            self._trace(agent, text, action="arret", mail=True)

    # -- #453 (2): test suites outside `ameesh heavy` --------------------------
    def tools_by_agent(self, table: list[dict]) -> dict[int, tuple[str, str]]:
        """`{pid: (agent, what)}`: the tool processes of every turn in
        progress and of the background work in grace, paused or not."""
        out: dict[int, tuple[str, str]] = {}
        for name, worker in dict(getattr(self.runner, "workers", {}) or {}).items():
            proc = getattr(worker, "proc", None)
            if proc is not None:
                for pid in turn_tools(table, proc.pid):
                    out[pid] = (name, "turn in progress")
        tracker = getattr(self.runner, "background", None)
        for entry in list(getattr(tracker, "entries", ()) or ()):
            if entry.state == "running" and entry.pgid:
                for pid in group_tools(table, entry.pgid, entry.lineage):
                    out.setdefault(pid, (entry.agent, "background work"))
        return out

    def cap_tests(self, table: list[dict], now: float) -> list[int]:
        """A test suite a turn launched outside `ameesh heavy` that runs for
        longer than the heavy cap (`heavy.max_duration`) is stopped: SIGTERM
        to it and its descendants, SIGKILL after `TERM_GRACE_S`, a message to
        the agent. Only the top of a suite counts; never inside `ameesh
        heavy`, which has its own cap. Returns the pids stopped."""
        from . import heavy

        for pid, (start, term_at) in list(self.capped.items()):
            if now - term_at >= TERM_GRACE_S:
                for victim in _descendants([p for p in table if not p["zombie"]], {pid}):
                    self._signal(victim, None if victim != pid else start, signal.SIGKILL)
                del self.capped[pid]
        cap = heavy.max_duration(getattr(self.runner, "cfg", None))
        if cap <= 0:
            return []
        by_pid = {p["pid"]: p for p in table if not p["zombie"]}
        tools = self.tools_by_agent(table)
        lines: dict[int, str] = {}

        def line(pid: int) -> str:
            if pid not in lines:
                try:
                    lines[pid] = " ".join((platform.command_line(pid) or "").split())
                except Exception:
                    lines[pid] = ""
            return lines[pid]

        def under_heavy_or_suite(pid: int) -> bool:
            seen, parent = set(), by_pid.get(pid, {}).get("ppid")
            while parent in tools and parent not in seen:
                seen.add(parent)
                if HEAVY_COMMAND.search(line(parent)) or TEST_COMMAND.search(line(parent)):
                    return True
                parent = by_pid.get(parent, {}).get("ppid")
            return False

        stopped: list[int] = []
        for pid, (agent, what) in tools.items():
            p = by_pid.get(pid)
            if p is None or p.get("start") is None or pid in self.capped:
                continue
            if now - float(p["start"]) < cap or not TEST_COMMAND.search(line(pid)):
                continue
            if HEAVY_COMMAND.search(line(pid)) or under_heavy_or_suite(pid):
                continue
            if pid in self.frozen:
                del self.frozen[pid]
            for victim in _descendants(list(by_pid.values()), {pid}):
                self._signal(victim, by_pid[victim].get("start"), signal.SIGTERM)
                self._signal(victim, by_pid[victim].get("start"), signal.SIGCONT)
            self.capped[pid] = (p.get("start"), now)
            stopped.append(pid)
            self._trace(agent, "Test suite stopped after %d min, the duration cap of a heavy "
                        "command (heavy_max_seconds): %s. Run it through ameesh heavy (a "
                        "slot of the host, its own cap: --max-duration), or narrow it."
                        % (int(cap // 60), _describe(pid, what)), action="plafond", mail=True)
        return stopped

    def thaw_all(self, reason: str) -> int:
        """Resumes everything frozen (runner stopping): never left stopped."""
        n = 0
        for pid, item in list(self.frozen.items()):
            if item.term_at is None and self._signal(pid, item.start, signal.SIGCONT):
                n += 1
            del self.frozen[pid]
        if n:
            self.log("pressure freeze: %d process(es) resumed (%s)" % (n, reason))
        return n

    # -- helpers --------------------------------------------------------------
    def _signal(self, pid: int, start, signum) -> bool:
        if start is not None:
            try:
                if not _same(start, platform.start_time(pid)):
                    return False
            except Exception:
                return False
        try:
            self.kill(pid, signum)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _host(self) -> str:
        return str(getattr(self.runner, "host", "") or "?")

    def _trace(self, agent: str, text: str, *, action: str, mail: bool = False) -> None:
        self.log("[%s] %s" % (agent, text))
        worker = (getattr(self.runner, "workers", {}) or {}).get(agent)
        if worker is not None:
            try:
                worker._fil_note(text, meta={"action": "pression-%s" % action,
                                             "hote": self._host()})
            except Exception:
                pass
        if mail:
            try:
                from . import mail as mail_mod
                mail_mod.send(self.runner.db, "ameesh", agent, text, kind="event")
            except Exception as exc:
                self.log("[%s] pressure stop not mailed (%s)" % (agent, exc))


def _same(expected, actual) -> bool:
    if expected is None or actual is None:
        return expected is None
    return platform.same_start(float(expected), float(actual))


def _describe(pid: int, what: str) -> str:
    try:
        line = platform.command_line(pid) or ""
    except Exception:
        line = ""
    line = " ".join(line.split())
    if len(line) > 60:
        line = line[:59] + "…"
    return "pid %d%s (%s)" % (pid, " \"%s\"" % line if line else "", what)


def _list(whats: list[str], cited: int = 3) -> str:
    head = "; ".join(whats[:cited])
    return head + ("; and %d more" % (len(whats) - cited) if len(whats) > cited else "")
