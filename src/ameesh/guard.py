# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh guard`: the capacity guard of a Linux VM that lends its unused
capacity to ameesh agents (L159e, study L159 §1).

The guard runs on the host, as a system service, next to the host's own
application. It reads the host's **offer** (what the host lends, `lend` of
the canon's `Host`, copied in its local configuration), measures the host
every `eval_s` seconds, decides a **level** (§1.6) and writes the guest's
state (`ameesh-host-state/1`, extended as in §1.5) that the guest executor
obeys:

| level | state | meaning |
|---|---|---|
| 0, ready | `available`, `caps` = lendable | the application leaves room |
| 1, reduced | `available`, lowered `caps` | the application wakes up: no new turn |
| 2, freeze | `available` + `freeze` | CPU or I/O pressure: guest frozen, 60 s at most |
| 3, drain | `draining` | the application needs the host back: turns stop at a safe point |
| 4, stop | `stopped` | no `drained` acknowledgement 30 s after the deadline |

The guard goes up a level as soon as a threshold is crossed (10 s averages to
react, 60 s to decide) and comes down only after `calm_seconds` under every
threshold (hysteresis); `seq` grows on every change. The state expires
(`expires_ts`, `state_ttl_s`, rewritten every third of it): a guard that
stops leaves a state that soon reads as `stopped`.

Levels 0 to 2 are enforced (tranche 2): the state's caps become properties
of the guest's slice and of its agents' units (`systemctl set-property
--runtime`: CPUWeight, IOWeight, CPUQuota, MemoryHigh, MemoryMax; MemoryMax per
agent), and level 2 freezes the slice through a transient service that
freezes, sleeps until the bound and thaws (systemd owns the freeze; stopped
when the level drops, when the guard stops and when it starts). `caps.enforced` lists
what is actually in force. `--observe` decides and publishes only.

The drain and the stop (tranche 3): a drain thaws and waits for the guest's
`drained` acknowledgement; at its deadline the offloaded jobs
(`ameesh-job-*.scope`, which do not acknowledge) are stopped; 30 s later
without the acknowledgement, level 4 kills the agents' units and the jobs
(`systemctl kill`) then stops the guest's slice (`systemctl stop`); the guard
does not start them again when the level drops. `ameesh guard drain`
asks the running guard for a drain (`--for`, `--cancel`), through a request
file it reads at every evaluation.

    ameesh guard check [--config F] [--json]       one measure, the decision, the state
    ameesh guard run [--observe] [--config F]      the loop; writes the state file
    ameesh guard drain [--for S | --cancel]        asks the running guard for a drain

Configuration (`/etc/ameesh/guard.json`, `--config`, `AMEESH_GUARD_CONFIG`):

    {"host": "vm-example", "max_agents": 3,
     "state_path": "/run/ameesh-gate/state/state.json",
     "ack_path": "/run/ameesh-gate/ack/ack.json",
     "slice": "ameesh-guest.slice", "agent_units": "ameesh-guest-runner@*.service",
     "job_units": "ameesh-job-*.scope",
     "drain_request_path": "/run/ameesh-gate/drain.json",
     "work_path": "/srv/ameesh-guest",
     "lend": {"cpu_max": 3, "cpu_weight": 10, "memory_max": "14GiB",
              "agent_memory_max": "4.5GiB", "disk_max": "80GiB",
              "windows": ["mon-fri 18:00-14:00 UTC", "sat-sun"],
              "reclaim": {"memory_available_min": "6GiB", "cpu_pressure_max": "20%",
                          "memory_pressure_max": "2%", "io_pressure_max": "20%",
                          "busy_units": ["actions.runner.*"], "busy_max": 4,
                          "disk_free_min": "2GiB", "disk_paths": ["/"]},
              "drain_seconds": 90, "calm_seconds": 600}}
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable

from . import resources
from .platform import host

SCHEMA_HOST_STATE = "ameesh-host-state/1"
SCHEMA_HOST_ACK = "ameesh-host-ack/1"
SCHEMA_DRAIN_REQUEST = "ameesh-guard-drain/1"
GUARD_KIND = "ameesh-guard"
DEFAULT_CONFIG = "/etc/ameesh/guard.json"
DEFAULT_STATE_PATH = "/run/ameesh-gate/state/state.json"
DEFAULT_ACK_PATH = "/run/ameesh-gate/ack/ack.json"
#: the guest's slice and its agents' units (study §2.1)
DEFAULT_SLICE = "ameesh-guest.slice"
DEFAULT_AGENT_UNITS = "ameesh-guest-runner@*.service"
#: the offloaded jobs (mode b, study §1.8) and the manual drain request
DEFAULT_JOB_UNITS = "ameesh-job-*.scope"
DEFAULT_DRAIN_REQUEST_PATH = "/run/ameesh-gate/drain.json"
#: the guest's work directory (study §3): its free disk is what the guest uses
DEFAULT_WORK_PATH = "/srv/ameesh-guest"

#: evaluation period, state lifetime, longest freeze, grace after the drain
#: deadline before the stop (seconds, study §1.5 and §1.6)
EVAL_S = 5.0
STATE_TTL_S = 90.0
FREEZE_MAX_S = 60.0
STOP_GRACE_S = 30.0
#: level 1 starts at this share of a reclaim threshold (memory: at 1 / share
#: of the minimum available)
WARN_SHARE = 0.5
#: defaults of the offer
DEFAULT_DRAIN_S = 90.0
DEFAULT_CALM_S = 600.0
DEFAULT_DISK_FREE_MIN = 2 * 1024 ** 3
#: with no history, what the guard lends (study §1.4: 25 % of the memory, 1 CPU)
WARMUP_MEMORY_SHARE = 0.25
WARMUP_CPU = 1.0

LEVELS = {0: "available", 1: "available", 2: "available", 3: "draining", 4: "stopped"}
LEVEL_NAMES = {0: "ready", 1: "reduced", 2: "freeze", 3: "drain", 4: "stop"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class GuardError(ValueError):
    """An unreadable configuration or offer."""


# --------------------------------------------------------------------------
# the offer
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Window:
    """A lending window: days, and optionally a time range in UTC (an end
    before the start runs past midnight, into the next day)."""

    days: frozenset
    start_min: int | None = None
    end_min: int | None = None

    def _span(self, day_start: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
        if DAYS[day_start.weekday()] not in self.days:
            return None
        if self.start_min is None:
            return day_start, day_start + dt.timedelta(days=1)
        start = day_start + dt.timedelta(minutes=self.start_min)
        end = day_start + dt.timedelta(minutes=self.end_min)
        if end <= start:
            end += dt.timedelta(days=1)
        return start, end

    def end_if_open(self, now: float) -> float | None:
        """The end of the window if it is open at `now` (epoch), else None."""
        at = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        today = at.replace(hour=0, minute=0, second=0, microsecond=0)
        for day in (today - dt.timedelta(days=1), today):
            span = self._span(day)
            if span and span[0] <= at < span[1]:
                # whole days chain: a window open on consecutive days ends
                # at the end of the last one
                end = span[1]
                while self.start_min is None and DAYS[end.weekday()] in self.days \
                        and end - at < dt.timedelta(days=7):
                    end += dt.timedelta(days=1)
                return end.timestamp()
        return None


_WINDOW_RE = re.compile(r"^\s*(?P<days>[a-z]{3}(?:-[a-z]{3})?|daily)"
                        r"(?:\s+(?P<start>\d{2}:\d{2})-(?P<end>\d{2}:\d{2}))?"
                        r"(?:\s+(?P<tz>UTC))?\s*$")


def parse_window(text: Any) -> Window:
    """"mon-fri 18:00-14:00 UTC", "sat-sun", "daily 20:00-06:00" (UTC only)."""
    match = _WINDOW_RE.match(str(text).lower().replace("utc", "UTC")) \
        if isinstance(text, str) else None
    if not match:
        raise GuardError("window %r: expected 'mon-fri 18:00-14:00 UTC', 'sat-sun'" % (text,))
    spec = match.group("days")
    if spec == "daily":
        days = frozenset(DAYS)
    else:
        first, _, last = spec.partition("-")
        if first not in DAYS or (last and last not in DAYS):
            raise GuardError("window %r: unknown day" % text)
        i, j = DAYS.index(first), DAYS.index(last or first)
        days = frozenset(DAYS[(i + k) % 7] for k in range((j - i) % 7 + 1))

    def minutes(hhmm: str | None) -> int | None:
        if hhmm is None:
            return None
        hours, mins = int(hhmm[:2]), int(hhmm[3:])
        if hours > 24 or mins > 59 or (hours == 24 and mins):
            raise GuardError("window %r: invalid time %s" % (text, hhmm))
        return hours * 60 + mins
    return Window(days, minutes(match.group("start")), minutes(match.group("end")))


@dataclasses.dataclass(frozen=True)
class Offer:
    """What the host lends (`lend`, study §1.3), parsed."""

    cpu_max: float | None = None
    cpu_weight: int = 10
    memory_max: int | None = None
    agent_memory_max: int | None = None
    disk_max: int | None = None
    windows: tuple = ()
    memory_available_min: int | None = None
    cpu_pressure_max: float | None = None
    memory_pressure_max: float | None = None
    io_pressure_max: float | None = None
    busy_units: tuple = ()
    busy_max: int | None = None
    disk_free_min: int = DEFAULT_DISK_FREE_MIN
    disk_paths: tuple = ("/",)
    drain_seconds: float = DEFAULT_DRAIN_S
    calm_seconds: float = DEFAULT_CALM_S
    raw: dict = dataclasses.field(default_factory=dict, compare=False)


def _percent(value: Any, key: str) -> float | None:
    if value is None:
        return None
    number = resources.parse_percent(value)
    if number is None:
        raise GuardError("lend.reclaim.%s: percentage expected, got %r" % (key, value))
    return number


def _bytes(value: Any, key: str) -> int | None:
    if value is None:
        return None
    number = resources.parse_bytes(value)
    if number is None:
        raise GuardError("lend.%s: size expected (e.g. 6GiB), got %r" % (key, value))
    return number


def _positive(value: Any, key: str, default: float | None = None) -> float | None:
    if value is None:
        return default
    # finite: drain_seconds of 1e309 (inf) or nan kept the guard draining
    # forever (codex #211 r5)
    number = resources.finite_float(value) if isinstance(value, (int, float)) else None
    if number is None or number <= 0:
        raise GuardError("lend.%s: positive finite number expected, got %r" % (key, value))
    return number


def parse_offer(lend: Any) -> Offer:
    """`Offer` from the `lend` object; `GuardError` on any unreadable key."""
    if not isinstance(lend, dict) or not lend:
        raise GuardError("lend: object expected (no offer: the host lends nothing)")
    reclaim = lend.get("reclaim") or {}
    if not isinstance(reclaim, dict):
        raise GuardError("lend.reclaim: object expected")
    weight = lend.get("cpu_weight", 10)
    if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 10000:
        raise GuardError("lend.cpu_weight: integer from 1 to 10000 expected")
    units = reclaim.get("busy_units") or []
    if not isinstance(units, list) or not all(isinstance(u, str) and u for u in units):
        raise GuardError("lend.reclaim.busy_units: list of unit patterns expected")
    busy_max = reclaim.get("busy_max")
    if busy_max is not None and (isinstance(busy_max, bool) or not isinstance(busy_max, int)
                                 or busy_max < 0):
        raise GuardError("lend.reclaim.busy_max: integer >= 0 expected")
    paths = reclaim.get("disk_paths") or ["/"]
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise GuardError("lend.reclaim.disk_paths: list of paths expected")
    windows = lend.get("windows") or []
    if not isinstance(windows, list):
        raise GuardError("lend.windows: list expected")
    disk_min = _bytes(reclaim.get("disk_free_min"), "reclaim.disk_free_min")
    return Offer(
        cpu_max=_positive(lend.get("cpu_max"), "cpu_max"), cpu_weight=weight,
        memory_max=_bytes(lend.get("memory_max"), "memory_max"),
        agent_memory_max=_bytes(lend.get("agent_memory_max"), "agent_memory_max"),
        disk_max=_bytes(lend.get("disk_max"), "disk_max"),
        windows=tuple(parse_window(w) for w in windows),
        memory_available_min=_bytes(reclaim.get("memory_available_min"),
                                    "reclaim.memory_available_min"),
        cpu_pressure_max=_percent(reclaim.get("cpu_pressure_max"), "cpu_pressure_max"),
        memory_pressure_max=_percent(reclaim.get("memory_pressure_max"),
                                     "memory_pressure_max"),
        io_pressure_max=_percent(reclaim.get("io_pressure_max"), "io_pressure_max"),
        busy_units=tuple(units), busy_max=busy_max,
        disk_free_min=DEFAULT_DISK_FREE_MIN if disk_min is None else disk_min,
        disk_paths=tuple(paths),
        drain_seconds=_positive(lend.get("drain_seconds"), "drain_seconds", DEFAULT_DRAIN_S),
        calm_seconds=_positive(lend.get("calm_seconds"), "calm_seconds", DEFAULT_CALM_S),
        raw=dict(lend))


@dataclasses.dataclass(frozen=True)
class GuardConfig:
    host: str
    offer: Offer
    max_agents: int = 1
    state_path: str = DEFAULT_STATE_PATH
    ack_path: str = DEFAULT_ACK_PATH
    slice: str = DEFAULT_SLICE
    agent_units: str = DEFAULT_AGENT_UNITS
    job_units: str = DEFAULT_JOB_UNITS
    drain_request_path: str = DEFAULT_DRAIN_REQUEST_PATH
    work_path: str = DEFAULT_WORK_PATH


def load_config(path: str | None = None) -> GuardConfig:
    path = path or os.environ.get("AMEESH_GUARD_CONFIG") or DEFAULT_CONFIG
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise GuardError("configuration %s unreadable: %s" % (path, exc.strerror or exc))
    except ValueError as exc:
        raise GuardError("configuration %s: invalid JSON (%s)" % (path, exc))
    if not isinstance(raw, dict):
        raise GuardError("configuration %s: object expected" % path)
    host = raw.get("host") or os.uname().nodename
    agents = raw.get("max_agents", 1)
    if isinstance(agents, bool) or not isinstance(agents, int) or agents < 0:
        raise GuardError("max_agents: integer >= 0 expected")
    slice_name = raw.get("slice") or DEFAULT_SLICE
    if freeze_unit(slice_name) is None:
        raise GuardError("slice: a systemd slice name expected (letters, digits, "
                         "':_.-', ending in .slice), got %r" % (slice_name,))
    return GuardConfig(host=str(host), offer=parse_offer(raw.get("lend")), max_agents=agents,
                       state_path=str(raw.get("state_path") or DEFAULT_STATE_PATH),
                       ack_path=str(raw.get("ack_path") or DEFAULT_ACK_PATH),
                       slice=slice_name,
                       agent_units=str(raw.get("agent_units") or DEFAULT_AGENT_UNITS),
                       job_units=str(raw.get("job_units") or DEFAULT_JOB_UNITS),
                       drain_request_path=str(raw.get("drain_request_path")
                                              or DEFAULT_DRAIN_REQUEST_PATH),
                       work_path=str(raw.get("work_path") or DEFAULT_WORK_PATH))


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

def _psi_file(resource: str, window: str) -> float | None:
    """PSI "some" `window` (avg10, avg60) of a resource, in %, or None. Used
    when `resources.psi` (L159d) is not there."""
    found = host.pressure(resource, base=os.environ.get("AMEESH_PSI_DIR") or None)
    return found.get("some", {}).get(window)


def read_psi(window: str) -> dict:
    """{psi_cpu_some, psi_mem_some, psi_io_some} for `window` (avg10 | avg60)."""
    psi = getattr(resources, "psi", None)
    if psi is not None:
        try:
            found = psi(window=window)
            return {k: found.get(k) for k in ("psi_cpu_some", "psi_mem_some", "psi_io_some")}
        except TypeError:
            if window == "avg60":
                found = psi()
                return {k: found.get(k)
                        for k in ("psi_cpu_some", "psi_mem_some", "psi_io_some")}
    return {"psi_cpu_some": _psi_file("cpu", window),
            "psi_mem_some": _psi_file("memory", window),
            "psi_io_some": _psi_file("io", window)}


def busy_count(patterns: tuple, run: Callable = subprocess.run) -> int | None:
    """Active systemd units matching `patterns` (CI jobs), None if unknown."""
    if not patterns:
        return None
    try:
        proc = run(["systemctl", "list-units", "--state=active", "--plain", "--no-legend",
                    "--no-pager", *patterns], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return sum(1 for line in proc.stdout.splitlines() if line.strip())


def _mem_total() -> int | None:
    """Physical memory, when the reading lacks it (before L159d)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def read_signals(cfg: GuardConfig, now: float, *, sample: Callable | None = None,
                 psi: Callable = read_psi, busy: Callable = busy_count) -> dict:
    """One flat reading of what the decision looks at."""
    sample = sample or (lambda: resources.sample(cfg.host, path=cfg.work_path, now=now))
    reading = dict(sample())
    if reading.get("mem_total_bytes") is None:
        reading["mem_total_bytes"] = _mem_total()
    react, decide = psi("avg10"), psi("avg60")
    disks = [resources.disk_free(p) for p in cfg.offer.disk_paths]
    disks = [d for d in disks if d is not None]
    reading.update({
        "ts": now,
        "psi_cpu_10": react.get("psi_cpu_some"), "psi_io_10": react.get("psi_io_some"),
        "psi_cpu_60": decide.get("psi_cpu_some"), "psi_mem_60": decide.get("psi_mem_some"),
        "psi_io_60": decide.get("psi_io_some"),
        "busy": busy(cfg.offer.busy_units),
        "app_disk_free_bytes": min(disks) if disks else None,
    })
    return reading


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Decision:
    level: int = 0
    reason: str = "idle"
    since_ts: float = 0.0          #: entry in the current level
    calm_since_ts: float | None = None   #: first instant under the current level
    window_end_ts: float | None = None


def window_end(offer: Offer, now: float) -> tuple[bool, float | None]:
    """(open, nearest end): no window declared means always open."""
    if not offer.windows:
        return True, None
    ends = [e for e in (w.end_if_open(now) for w in offer.windows) if e is not None]
    return (bool(ends), max(ends) if ends else None)


def raw_level(offer: Offer, signals: dict, now: float) -> tuple[int, str]:
    """The level the signals call for right now, before hysteresis, and why."""
    def over(value, limit, share=1.0):
        return value is not None and limit is not None and value > limit * share

    open_, _end = window_end(offer, now)
    mem = signals.get("mem_available_bytes")
    mem_min = offer.memory_available_min
    if signals.get("drain_requested"):
        return 3, "manual"
    if not open_:
        return 3, "window_end"
    if mem_min is not None and (mem is None or mem < mem_min):
        return 3, "app_memory"
    if over(signals.get("psi_mem_60"), offer.memory_pressure_max):
        return 3, "app_memory"
    if over(signals.get("psi_cpu_60"), offer.cpu_pressure_max) \
            or over(signals.get("psi_io_60"), offer.io_pressure_max):
        return 3, "app_cpu"
    busy = signals.get("busy")
    if offer.busy_max is not None and busy is not None and busy > offer.busy_max:
        return 3, "app_busy"
    disk = signals.get("app_disk_free_bytes")
    if disk is not None and disk < offer.disk_free_min:
        return 3, "disk_low"
    # a freeze frees CPU and I/O, never memory (§1.6)
    if over(signals.get("psi_cpu_10"), offer.cpu_pressure_max) \
            or over(signals.get("psi_io_10"), offer.io_pressure_max):
        return 2, "app_cpu"
    if mem_min is not None and mem < mem_min / WARN_SHARE:
        return 1, "app_memory"
    if over(signals.get("psi_mem_60"), offer.memory_pressure_max, WARN_SHARE):
        return 1, "app_memory"
    if over(signals.get("psi_cpu_60"), offer.cpu_pressure_max, WARN_SHARE) \
            or over(signals.get("psi_io_60"), offer.io_pressure_max, WARN_SHARE):
        return 1, "app_cpu"
    if offer.busy_max is not None and busy is not None and busy >= offer.busy_max:
        return 1, "app_busy"
    return 0, "idle"


def decide(offer: Offer, signals: dict, previous: Decision, now: float, *,
           drained: bool = False) -> Decision:
    """The next decision (§1.6): up at once, down after `calm_seconds` under
    the current level; a freeze lasts `FREEZE_MAX_S` at most, then drains; a
    drain not acknowledged `STOP_GRACE_S` after its deadline stops. A manual
    drain ends with its request when the host is healthy."""
    level, reason = raw_level(offer, signals, now)
    _open, end = window_end(offer, now)
    current = previous.level
    if level > current:
        return Decision(level, reason, now, None, end)
    if current >= 3 and previous.reason == "manual" and level < 3:
        # a manual drain expired or cancelled, on a healthy host: its episode
        # ends at once, at level 3 as at level 4 (no stop, no kill retried,
        # no hysteresis: the application never asked)
        return Decision(level, reason, now, None, end)
    # same or lower: hold the current level, counting the calm
    if current == 2:
        if level == 2 and now - previous.since_ts >= FREEZE_MAX_S:
            return Decision(3, previous.reason, now, None, end)
        if level < 2:
            # thaw at once (the freeze is short), stay reduced while calming
            return Decision(1, previous.reason, now, now, end)
    if current == 3 and not drained \
            and now >= previous.since_ts + offer.drain_seconds + STOP_GRACE_S:
        return Decision(4, previous.reason, now, previous.calm_since_ts, end)
    if level == current:
        return dataclasses.replace(previous, calm_since_ts=None, window_end_ts=end)
    calm = previous.calm_since_ts if previous.calm_since_ts is not None else now
    if now - calm >= offer.calm_seconds:
        return Decision(level, reason, now, None, end)
    return dataclasses.replace(previous, calm_since_ts=calm, window_end_ts=end)


# --------------------------------------------------------------------------
# lendable capacity, caps and the state
# --------------------------------------------------------------------------

def lendable(offer: Offer, reading: dict) -> dict:
    """{cpu, memory_bytes, disk_bytes}, capped by the offer: `resources.lendable`
    (L159d) when there, else the warm-up values of §1.4 (25 % of the memory,
    1 CPU)."""
    out = None
    found = getattr(resources, "lendable", None)
    if found is not None:
        try:
            got = found(reading, None)
            out = {"cpu": got.get("lendable_cpu"),
                   "memory_bytes": got.get("lendable_memory_bytes"),
                   "disk_bytes": got.get("lendable_disk_bytes")}
        except Exception:  # noqa: BLE001 — the measure never stops the guard
            pass
    if out is None:
        total = reading.get("mem_total_bytes")
        free = reading.get("disk_free_bytes")
        out = {"cpu": WARMUP_CPU,
               "memory_bytes": int(total * WARMUP_MEMORY_SHARE) if total else None,
               "disk_bytes": None if free is None else max(0, free - DEFAULT_DISK_FREE_MIN)}
    for key, limit in (("cpu", offer.cpu_max), ("memory_bytes", offer.memory_max),
                       ("disk_bytes", offer.disk_max)):
        if out[key] is not None and limit is not None:
            out[key] = min(out[key], limit)
    return out


def caps_for(cfg: GuardConfig, level: int, lend: dict, reading: dict,
             in_turn: int | None) -> dict:
    """The `caps` of the state for a level (§1.5, §1.6)."""
    offer = cfg.offer
    count = reading.get("cpu_count") or 0
    cpu = lend.get("cpu")
    caps: dict = {"max_concurrent": cfg.max_agents, "cpu_weight": offer.cpu_weight,
                  "io_weight": offer.cpu_weight, "enforced": []}
    if cpu is not None and count:
        caps["cpu_share"] = round(cpu / count, 4)
    if lend.get("memory_bytes") is not None:
        caps["memory_mb"] = int(lend["memory_bytes"] // 2 ** 20)
        caps["memory_high_mb"] = caps["memory_mb"]
    if offer.agent_memory_max is not None:
        caps["agent_memory_mb"] = int(offer.agent_memory_max // 2 ** 20)
    if lend.get("disk_bytes") is not None:
        caps["disk_mb"] = int(lend["disk_bytes"] // 2 ** 20)
    if level >= 1:
        # no turn beyond those in progress, half the CPU quota, the guest's
        # memory brought back to its use (the kernel reclaims its cache)
        caps["max_concurrent"] = min(cfg.max_agents, in_turn or 0)
        if "cpu_share" in caps:
            caps["cpu_share"] = round(caps["cpu_share"] / 2, 4)
        guest = reading.get("guest_mem_bytes")
        if guest is not None and "memory_high_mb" in caps:
            caps["memory_high_mb"] = min(caps["memory_high_mb"], int(guest // 2 ** 20))
    if level >= 3:
        caps["max_concurrent"] = 0
    return caps


def measure_of(reading: dict) -> dict:
    """The common `measure` object (§1.4): `resources.measure` (L159d) when
    there, else what the reading has."""
    found = getattr(resources, "measure", None)
    if found is not None:
        try:
            out = dict(found(reading, None))
        except Exception:  # noqa: BLE001
            out = None
        if out is not None:
            # what the guard measured itself (busy units, application disks)
            # and L159d leaves null
            for key, value in out.items():
                if value is None and not isinstance(reading.get(key), (dict, type(None))):
                    out[key] = reading[key]
            return out
    keys = ("cpu_count", "mem_total_bytes", "mem_available_bytes", "swap_used_bytes",
            "disk_free_bytes", "app_disk_free_bytes", "busy", "guest_mem_bytes",
            "app_cpu", "guest_cpu")
    out = {"ts": reading.get("ts"), "window_s": 60}
    out.update({k: reading.get(k) for k in keys if k in reading})
    out["psi"] = {"cpu_some": reading.get("psi_cpu_60"), "mem_some": reading.get("psi_mem_60"),
                  "io_some": reading.get("psi_io_60")}
    return out


def build_state(cfg: GuardConfig, decision: Decision, seq: int, now: float, *,
                reading: dict, in_turn: int | None = None, ttl: float = STATE_TTL_S,
                observe: bool = True) -> dict:
    """The state document (`ameesh-host-state/1` extended, §1.5)."""
    from . import version
    lend = lendable(cfg.offer, reading)
    state: dict = {
        "schema": SCHEMA_HOST_STATE, "state": LEVELS[decision.level], "seq": seq,
        "until_ts": decision.window_end_ts, "drain_deadline_ts": None,
        "caps": caps_for(cfg, decision.level, lend, reading, in_turn),
        "reason": decision.reason, "expires_ts": now + ttl,
        "level": decision.level,
        "offer": cfg.offer.raw, "measure": dict(measure_of(reading), lendable=lend),
        "guard": {"kind": GUARD_KIND, "version": version(), "host_kind": "vm",
                  "mode": "observe" if observe else "enforce"},
    }
    if decision.level == 2:
        state["freeze"] = {"since_ts": decision.since_ts,
                           "until_ts": decision.since_ts + FREEZE_MAX_S}
    if decision.level >= 3:
        state["drain_deadline_ts"] = decision.since_ts + cfg.offer.drain_seconds \
            if decision.level == 3 else None
    return state


def _key(state: dict) -> str:
    """What makes a new `seq`: everything but the measure and the instants."""
    keep = {k: v for k, v in state.items()
            if k not in ("seq", "expires_ts", "measure", "guard")}
    return json.dumps(keep, sort_keys=True, default=str)


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(path: str, state: dict) -> None:
    """Atomic write (temporary file then rename), readable by the guest."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, mode=0o755, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


#: the largest acknowledgement the guard reads (a real one is under 1 KiB)
ACK_MAX_BYTES = 64 * 1024


def read_guest_json(path: str, limit: int = ACK_MAX_BYTES) -> dict | None:
    """A JSON object from a file the guest may replace: never blocks (no
    FIFO, device or symlink: a regular file only, opened without waiting),
    never reads more than `limit` bytes. None otherwise."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > limit:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        # RecursionError: 64 KiB of nested arrays (codex #211 r4)
        return None
    return doc if isinstance(doc, dict) else None


def read_ack(path: str) -> dict | None:
    """The guest's acknowledgement (`ameesh-host-ack/1`), None if absent,
    unreadable or ill-typed: the guest writes it, the guard never trusts it."""
    ack = read_guest_json(path)
    if not ack or ack.get("schema") != SCHEMA_HOST_ACK:
        return None
    seq, in_turn = ack.get("seq"), ack.get("in_turn")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    if not isinstance(ack.get("drained", False), bool) \
            or not isinstance(ack.get("state", ""), str):
        return None
    if in_turn is not None and not (isinstance(in_turn, list)
                                    and all(isinstance(t, str) for t in in_turn)):
        return None
    return ack


def _number(value) -> bool:
    # finite: -Infinity or 400 digits in the state file never restore
    return isinstance(value, (int, float)) and resources.finite_float(value) is not None


def restore_decision(previous: dict, now: float) -> tuple[Decision, int | None]:
    """The decision and the drain episode a restarted guard takes back from
    its last state (`guard.decision`): a restart neither lowers the level nor
    moves a freeze bound or a drain deadline. Nothing usable: level 0 now."""
    saved = (previous.get("guard") or {}).get("decision") \
        if isinstance(previous.get("guard"), dict) else None
    if not isinstance(saved, dict):
        return Decision(since_ts=now), None
    level, reason, since = saved.get("level"), saved.get("reason"), saved.get("since_ts")
    calm, episode = saved.get("calm_since_ts"), saved.get("drain_seq")
    if not (isinstance(level, int) and level in LEVELS and isinstance(reason, str)
            and _number(since) and since <= now and (calm is None or _number(calm))):
        return Decision(since_ts=now), None
    if not (isinstance(episode, int) and not isinstance(episode, bool)) or level != 3:
        episode = None
    return Decision(level, reason, float(since),
                    None if calm is None else float(calm)), episode


# --------------------------------------------------------------------------
# enforcement (tranche 2): kernel caps and freeze through systemd
# --------------------------------------------------------------------------

def slice_properties(caps: dict, cpu_count: int | None) -> dict:
    """systemd properties of the guest's slice for `caps` (study §1.7)."""
    props: dict = {}
    if caps.get("cpu_weight") is not None:
        props["CPUWeight"] = str(int(caps["cpu_weight"]))
    if caps.get("io_weight") is not None:
        props["IOWeight"] = str(int(caps["io_weight"]))
    if caps.get("cpu_share") is not None and cpu_count:
        # CPUQuota is a share of ONE CPU: 150 % = one and a half CPU
        props["CPUQuota"] = "%d%%" % max(1, round(caps["cpu_share"] * cpu_count * 100))
    if caps.get("memory_high_mb") is not None:
        props["MemoryHigh"] = "%dM" % int(caps["memory_high_mb"])
    if caps.get("memory_mb") is not None:
        props["MemoryMax"] = "%dM" % int(caps["memory_mb"])
    return props


#: a slice name the guard accepts: it goes into the name of the freezing unit
#: and into its ExecStopPost= line, so no space, quote, `%`, `$` or backslash
SLICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.-]*\.slice")
#: the transient service that holds a freeze: it freezes the slice, sleeps
#: until the bound (monotonic), then thaws, retried every 2 s. Stopping it
#: thaws too (ExecStopPost=), and so does RuntimeMaxSec=, `FREEZE_BACKSTOP_S`
#: past the bound. systemd owns the freeze: a guard killed at any instant
#: never leaves the slice frozen past the bound. Its name carries the slice:
#: a guard never touches the freeze of another slice
FREEZE_UNIT = "ameesh-guard-freeze"
FREEZE_BACKSTOP_S = 30
FREEZE_SCRIPT = ('systemctl freeze "$0" && sleep "$1"; '
                 'until systemctl thaw "$0"; do sleep 2; done')


def freeze_unit(slice_name: str) -> str | None:
    """The freezing unit of a slice (`ameesh-guard-freeze-<slice>.service`),
    None for a slice name the guard refuses."""
    if not isinstance(slice_name, str) or not SLICE_RE.fullmatch(slice_name):
        return None
    return "%s-%s.service" % (FREEZE_UNIT, slice_name[:-len(".slice")])


#: which `caps` key each property enforces
PROPERTY_CAPS = {"CPUWeight": "cpu_weight", "IOWeight": "io_weight", "CPUQuota": "cpu_share",
                 "MemoryHigh": "memory_high_mb", "MemoryMax": "memory_mb"}


class Enforcer:
    """Applies the state's caps with `systemctl set-property --runtime` (the
    caps live as long as the boot, and stay in place if the guard stops) and
    the level 2 freeze with `systemctl freeze` / `thaw`. Never raises: a
    failed command is reported on stderr, and its caps are left out of
    `caps.enforced`.

    The guard never freezes the slice itself: one transient service per
    slice (`freeze_unit`) freezes it, sleeps until the bound and thaws it, so
    no delay of the guard, nor a wall clock going back, can leave a freeze
    without its thaw (codex #237 r4). The guard thaws early by stopping that
    service (its ExecStopPost= thaws), then by `systemctl thaw`. A freeze
    started counts as possibly in force until a thaw succeeds: every
    evaluation below level 2, and the guard's exit, thaw again until it
    does."""

    def __init__(self, cfg: GuardConfig, run: Callable | None = None,
                 log: Callable | None = None, clock: Callable = time.time):
        self.cfg = cfg
        self.run = run or subprocess.run
        self.clock = clock
        self.log = log or (lambda text: print("ameesh guard: %s" % text, file=sys.stderr))
        self.applied: dict = {}          # unit -> {property: value} last applied
        self.frozen = False              # possibly frozen: a thaw is owed
        self.freeze_done = False         # the freezing service was started
        self.freeze_unit = freeze_unit(cfg.slice)
        self.jobs_stopped = False        # the drain's deadline is past: jobs stopped
        self.guest_stopped = False       # level 4: slice killed and stopped

    def _command(self, argv: list[str]) -> bool:
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("%s: %s" % (" ".join(argv), exc))
            return False
        if proc.returncode != 0:
            self.log("%s: code %s %s" % (" ".join(argv), proc.returncode,
                                         (proc.stderr or "").strip()[:200]))
            return False
        return True

    def _systemctl(self, *args: str) -> bool:
        return self._command(["systemctl", *args])

    def agent_units(self) -> list[str]:
        """The loaded agent units of the guest."""
        try:
            proc = self.run(["systemctl", "list-units", "--all", "--plain", "--no-legend",
                             "--no-pager", self.cfg.agent_units],
                            capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        # the first word that is a unit name (some systemd versions prefix a
        # failed unit with "●", even with --plain)
        units = []
        for line in proc.stdout.splitlines():
            name = next((w for w in line.split() if "." in w), None)
            if name:
                units.append(name)
        return units

    def _set(self, unit: str, props: dict) -> set:
        """Applies the properties that changed; the set of those in force."""
        last = self.applied.setdefault(unit, {})
        changed = {k: v for k, v in props.items() if last.get(k) != v}
        if changed:
            if not self._systemctl("set-property", "--runtime", unit,
                                   *("%s=%s" % kv for kv in sorted(changed.items()))):
                for key in changed:
                    last.pop(key, None)
                return {k for k in props if k not in changed}
            last.update(changed)
        return set(props)

    def _quiet(self, *args: str) -> None:
        # absent unit or failing command: harmless, not reported
        try:
            self.run(["systemctl", *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def freeze(self, on: bool, until: float | None = None) -> bool:
        """Freezes the guest's slice until `until` through its freezing
        service, or thaws it; True if the slice is as asked."""
        if not on:
            if not self.frozen:
                return True
            if self.freeze_unit is not None:
                self._quiet("stop", self.freeze_unit)   # thaws as it stops
            if not self._systemctl("thaw", self.cfg.slice):
                return False          # owed: retried; the service still bounds it
            self.frozen = self.freeze_done = False
            return True
        if self.freeze_done:
            return True
        now = self.clock()
        if self.freeze_unit is None or until is None or until - now < 1:
            self.log("freeze refused: %s" % ("slice name %r refused" % self.cfg.slice
                                             if self.freeze_unit is None else "bound past"))
            self.freeze(False)
            return False
        # never more than the longest freeze, whatever the wall clock says
        seconds = min(int(FREEZE_MAX_S), int(math.ceil(until - now)))
        # a service left by a failed attempt or an earlier guard: stopped
        # (it thaws), forgotten, then started anew
        self._quiet("stop", self.freeze_unit)
        self._quiet("reset-failed", self.freeze_unit)
        # started: possibly in force even if systemd-run does not return
        self.frozen = True
        self.freeze_done = self._command([
            "systemd-run", "--unit=%s" % self.freeze_unit, "--collect", "--quiet",
            "--property=RuntimeMaxSec=%d" % (seconds + FREEZE_BACKSTOP_S),
            "--property=ExecStopPost=systemctl thaw %s" % self.cfg.slice,
            "/bin/sh", "-c", FREEZE_SCRIPT, self.cfg.slice, str(seconds)])
        return self.freeze_done

    def thaw_at_start(self) -> None:
        """A guard that died during a freeze must not leave the guest frozen:
        the slice counts as possibly frozen until a thaw succeeds (retried
        at every evaluation below level 2)."""
        self.frozen = True
        self.freeze(False)

    def stop_jobs(self) -> bool:
        """Stops the offloaded jobs (they do not acknowledge a drain)."""
        return self._systemctl("stop", self.cfg.job_units)

    def stop_guest(self) -> bool:
        """Level 4: kills the agents' units and the jobs (`systemctl kill`,
        aimed at the units: not every systemd kills a slice), then stops the
        slice, its units with it. Done once both succeeded (a unit pattern
        that matches nothing is not a failure)."""
        killed = self._systemctl("kill", "--signal=SIGKILL", self.cfg.agent_units,
                                 self.cfg.job_units)
        stopped = self._systemctl("stop", self.cfg.slice)
        return killed and stopped

    def apply(self, state: dict, cpu_count: int | None,
              now: float | None = None) -> list[str]:
        """Enforces `state`: caps at every level, freeze at level 2; a drain
        thaws, and stops the jobs at its deadline; a stop kills and stops the
        guest's slice. Each is done once, retried while it fails. The `caps`
        keys actually enforced.

        `now` is the instant `state` was decided at: the deadline is judged
        on the same clock as the decision (a manual request's expiry
        included), never on a later one (codex #238 r4)."""
        now = self.clock() if now is None else now
        caps = state.get("caps") or {}
        level = state.get("level", 0)
        deadline = state.get("drain_deadline_ts")
        if level < 3:
            self.jobs_stopped = self.guest_stopped = False
        elif not self.jobs_stopped and (level == 4 or (
                deadline is not None and now >= deadline)):
            self.jobs_stopped = self.stop_jobs()
        if level == 4 and not self.guest_stopped:
            self.guest_stopped = self.stop_guest()
        done = self._set(self.cfg.slice, slice_properties(caps, cpu_count))
        enforced = {PROPERTY_CAPS[p] for p in done}
        if caps.get("agent_memory_mb") is not None:
            units = self.agent_units()
            per_agent = {"MemoryMax": "%dM" % int(caps["agent_memory_mb"])}
            # every unit, even after a failure on one of them
            done = [("MemoryMax" in self._set(u, per_agent)) for u in units]
            if done and all(done):
                enforced.add("agent_memory_mb")
        freeze = state.get("freeze")
        if freeze:
            if self.freeze(True, freeze.get("until_ts")):
                enforced.add("freeze")
        else:
            self.freeze(False)
        return sorted(enforced)


# --------------------------------------------------------------------------
# manual drain (`ameesh guard drain`)
# --------------------------------------------------------------------------

def drain_requested(path: str, now: float) -> bool:
    """A drain asked with `ameesh guard drain` and not yet over."""
    request = read_json(path)
    if not request or request.get("schema") != SCHEMA_DRAIN_REQUEST:
        return False
    until = request.get("until_ts")
    return until is None or (isinstance(until, (int, float)) and now < until)


def request_drain(path: str, now: float, seconds: float | None) -> dict:
    """Writes the drain request (until `now + seconds`, or until cancelled)."""
    request = {"schema": SCHEMA_DRAIN_REQUEST, "ts": now,
               "until_ts": None if seconds is None else now + seconds}
    write_state(path, request)
    return request


def cancel_drain(path: str) -> bool:
    """Removes the drain request; False if there was none."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

class Guard:
    """The guard's state between two evaluations."""

    def __init__(self, cfg: GuardConfig, *, observe: bool = True,
                 signals: Callable | None = None, clock: Callable = time.time,
                 enforcer: Enforcer | None = None):
        self.cfg = cfg
        self.observe = observe
        self.enforcer = None if observe else (enforcer or Enforcer(cfg, clock=clock))
        if self.enforcer is not None:
            self.enforcer.thaw_at_start()
        self.clock = clock
        self.signals = signals or (lambda now: read_signals(cfg, now))
        previous = read_json(cfg.state_path) or {}
        # seq keeps growing across restarts: a reader ignores a seq it saw
        self.seq = previous.get("seq") if isinstance(previous.get("seq"), int) else 0
        # so do the level, the freeze bound and the drain deadline
        self.decision, self.drain_seq = restore_decision(previous, clock())
        self.state: dict | None = None
        self.written_ts = 0.0
        self.last_key = None
        self.written_decision = None

    def stop(self) -> None:
        """Leaving: thaw the guest; the caps stay (slice properties)."""
        if self.enforcer is not None:
            self.enforcer.freeze(False)

    def step(self) -> dict:
        """One evaluation: measure, decide, write when changed or due."""
        reading = self.signals(self.clock())
        # measuring takes time: the request's expiry, the decision and the
        # drain deadline are judged on the clock read after it, right
        # before enforcing (codex #238 r4)
        now = self.clock()
        reading["drain_requested"] = drain_requested(self.cfg.drain_request_path, now)
        ack = read_ack(self.cfg.ack_path)
        # an acknowledgement holds for the whole drain episode: a seq the
        # guard published since the drain began, whatever changed since
        drained = bool(ack and ack.get("drained") is True and self.decision.level == 3
                       and self.drain_seq is not None
                       and self.drain_seq <= ack["seq"] <= self.seq)
        in_turn = len(ack.get("in_turn") or []) if ack else None
        previous = self.decision
        self.decision = decide(self.cfg.offer, reading, previous, now, drained=drained)
        state = build_state(self.cfg, self.decision, self.seq, now, reading=reading,
                            in_turn=in_turn, observe=self.observe)
        if self.enforcer is not None:
            state["caps"]["enforced"] = self.enforcer.apply(state, reading.get("cpu_count"),
                                                            now)
        key = _key(state)
        if key != self.last_key:
            self.seq += 1
            state["seq"] = self.seq
        if self.decision.level != 3:
            self.drain_seq = None
        elif self.drain_seq is None or previous.level != 3 \
                or previous.since_ts != self.decision.since_ts:
            self.drain_seq = self.seq
        decision = dict(dataclasses.asdict(self.decision), drain_seq=self.drain_seq)
        state["guard"]["decision"] = decision
        # a decision that changed (a calm timer started or reset) is written
        # at once: a restart must not take back a stale one
        due = now - self.written_ts >= STATE_TTL_S / 3
        if key != self.last_key or due or decision != self.written_decision:
            write_state(self.cfg.state_path, state)
            self.written_ts, self.written_decision = now, decision
        self.last_key, self.state = key, state
        return state


def _terminate(*_args) -> None:
    """SIGTERM (systemctl stop): leave through `Guard.stop` (thaw)."""
    raise KeyboardInterrupt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh guard",
                                     description="capacity guard of a lending VM (L159e)")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="one measure, the decision and the state")
    check.add_argument("--config")
    check.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="the guard loop")
    run.add_argument("--config")
    run.add_argument("--observe", action="store_true",
                     help="decide and publish the state, enforce nothing")
    run.add_argument("--once", action="store_true", help="one evaluation, then exit")
    drain = sub.add_parser("drain", help="ask the running guard for a drain")
    drain.add_argument("--config")
    span = drain.add_mutually_exclusive_group()
    span.add_argument("--for", dest="seconds", type=float, metavar="SECONDS",
                      help="drain for this long (default: until --cancel)")
    span.add_argument("--cancel", action="store_true", help="withdraw the request")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except GuardError as exc:
        print("ameesh guard: %s" % exc, file=sys.stderr)
        return 1
    if args.command == "check":
        now = time.time()
        reading = read_signals(cfg, now)
        reading["drain_requested"] = drain_requested(cfg.drain_request_path, now)
        decision = decide(cfg.offer, reading, Decision(since_ts=now), now)
        state = build_state(cfg, decision, 0, now, reading=reading)
        if args.json:
            print(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print("level %d (%s), state %s, reason %s"
                  % (decision.level, LEVEL_NAMES[decision.level], state["state"],
                     state["reason"]))
            print("caps: %s" % json.dumps(state["caps"], sort_keys=True))
        return 0
    if args.command == "drain":
        if args.cancel:
            found = cancel_drain(cfg.drain_request_path)
            print("drain request %s" % ("withdrawn" if found else "absent: nothing to withdraw"))
            return 0
        if args.seconds is not None and args.seconds <= 0:
            print("ameesh guard: --for: positive number of seconds expected", file=sys.stderr)
            return 2
        try:
            request = request_drain(cfg.drain_request_path, time.time(), args.seconds)
        except OSError as exc:
            print("ameesh guard: %s: %s" % (cfg.drain_request_path, exc), file=sys.stderr)
            return 1
        until = request["until_ts"]
        print("drain requested %s" % ("until --cancel" if until is None else "until %s" % (
            dt.datetime.fromtimestamp(until, dt.timezone.utc).isoformat(timespec="seconds"))))
        return 0
    guard = Guard(cfg, observe=args.observe)
    signal.signal(signal.SIGTERM, _terminate)
    try:
        while True:
            guard.step()
            if args.once:
                return 0
            time.sleep(EVAL_S)
    except KeyboardInterrupt:
        return 0
    finally:
        guard.stop()
