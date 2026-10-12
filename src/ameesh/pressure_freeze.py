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
    return _descendants(live, roots) - {leader, os.getpid()}


def group_tools(table: list[dict], pgid: int, lineage=()) -> set[int]:
    """The background work of a finished turn: the members of its group and
    of its known lineage, with their descendants."""
    live = [p for p in table if not p["zombie"]]
    roots = {p["pid"] for p in live if p["pgid"] == pgid or p["pid"] in set(lineage)}
    return _descendants(live, roots) - {os.getpid()}


class Freezer:
    """The runner's freezer (one per runner); `tick` never raises."""

    def __init__(self, runner, log=print, *, kill=os.kill, clock=time.time) -> None:
        self.runner = runner
        self.log = log
        self.kill = kill
        self.clock = clock
        self.frozen: dict[int, Frozen] = {}

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
            return
        if not verdict.get("critical") and not self.frozen:
            return
        try:
            table = platform.process_table()
        except Exception:
            return
        starts = {p["pid"]: p.get("start") for p in table if not p["zombie"]}
        try:
            wanted = self.targets(table, verdict)
        except Exception as exc:
            self.log("pressure freeze: targets unreadable (%s)" % exc)
            wanted = {}
        frozen_now: dict[str, list[str]] = {}
        for pid, (agent, what) in wanted.items():
            if pid in self.frozen:
                continue
            if self._signal(pid, starts.get(pid), signal.SIGSTOP):
                self.frozen[pid] = Frozen(pid, starts.get(pid), agent, now,
                                          _describe(pid, what))
                frozen_now.setdefault(agent, []).append(self.frozen[pid].what)
        for agent, whats in frozen_now.items():
            self._trace(agent, "Critical pressure on host %s: %d tool process(es) frozen "
                        "(SIGSTOP), resumed when the pressure goes down: %s."
                        % (self._host(), len(whats), _list(whats)), action="gel")
        maximum = freeze_max(getattr(self.runner, "cfg", None))
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
