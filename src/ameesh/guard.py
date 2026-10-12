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

Tranche 1 (this module) **observes**: it decides and publishes, it enforces
nothing (`--observe`). Kernel caps (`systemctl set-property --runtime`) and
the freeze come with tranche 2, the drain and the stop with tranche 3.

    ameesh guard check [--config F] [--json]       one measure, the decision, the state
    ameesh guard run --observe [--config F]        the loop; writes the state file

Configuration (`/etc/ameesh/guard.json`, `--config`, `AMEESH_GUARD_CONFIG`):

    {"host": "vm-example", "max_agents": 3,
     "state_path": "/run/ameesh-gate/state/state.json",
     "ack_path": "/run/ameesh-gate/ack/ack.json",
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
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable

from . import resources
from .platform import host

SCHEMA_HOST_STATE = "ameesh-host-state/1"
SCHEMA_HOST_ACK = "ameesh-host-ack/1"
GUARD_KIND = "ameesh-guard"
DEFAULT_CONFIG = "/etc/ameesh/guard.json"
DEFAULT_STATE_PATH = "/run/ameesh-gate/state/state.json"
DEFAULT_ACK_PATH = "/run/ameesh-gate/ack/ack.json"

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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise GuardError("lend.%s: positive number expected, got %r" % (key, value))
    return float(value)


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
    return GuardConfig(host=str(host), offer=parse_offer(raw.get("lend")), max_agents=agents,
                       state_path=str(raw.get("state_path") or DEFAULT_STATE_PATH),
                       ack_path=str(raw.get("ack_path") or DEFAULT_ACK_PATH))


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
    sample = sample or (lambda: resources.sample(cfg.host, now=now))
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
    drain not acknowledged `STOP_GRACE_S` after its deadline stops."""
    level, reason = raw_level(offer, signals, now)
    _open, end = window_end(offer, now)
    current = previous.level
    if level > current:
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
    """{cpu, memory_bytes, disk_bytes}: `resources.lendable` (L159d) when
    there, else the warm-up values of §1.4 (25 % of the memory, 1 CPU)."""
    found = getattr(resources, "lendable", None)
    if found is not None:
        try:
            out = found(reading, None)
            return {"cpu": out.get("lendable_cpu"),
                    "memory_bytes": out.get("lendable_memory_bytes"),
                    "disk_bytes": out.get("lendable_disk_bytes")}
        except Exception:  # noqa: BLE001 — the measure never stops the guard
            pass
    total = reading.get("mem_total_bytes")
    memory = int(total * WARMUP_MEMORY_SHARE) if total else None
    if memory is not None and offer.memory_max is not None:
        memory = min(memory, offer.memory_max)
    cpu = WARMUP_CPU if offer.cpu_max is None else min(WARMUP_CPU, offer.cpu_max)
    free = reading.get("disk_free_bytes")
    disk = None if free is None else max(0, free - DEFAULT_DISK_FREE_MIN)
    if disk is not None and offer.disk_max is not None:
        disk = min(disk, offer.disk_max)
    return {"cpu": cpu, "memory_bytes": memory, "disk_bytes": disk}


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
            return found(reading, None)
        except Exception:  # noqa: BLE001
            pass
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


def read_ack(path: str) -> dict | None:
    """The guest's acknowledgement (`ameesh-host-ack/1`), None if absent,
    unreadable or ill-typed: the guest writes it, the guard never trusts it."""
    ack = read_json(path)
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
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
# the loop
# --------------------------------------------------------------------------

class Guard:
    """The guard's state between two evaluations."""

    def __init__(self, cfg: GuardConfig, *, observe: bool = True,
                 signals: Callable | None = None, clock: Callable = time.time):
        self.cfg = cfg
        self.observe = observe
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

    def step(self) -> dict:
        """One evaluation: measure, decide, write when changed or due."""
        now = self.clock()
        reading = self.signals(now)
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
        key = _key(state)
        if key != self.last_key:
            self.seq += 1
            state["seq"] = self.seq
        if self.decision.level != 3:
            self.drain_seq = None
        elif self.drain_seq is None or previous.level != 3 \
                or previous.since_ts != self.decision.since_ts:
            self.drain_seq = self.seq
        state["guard"]["decision"] = dict(dataclasses.asdict(self.decision),
                                          drain_seq=self.drain_seq)
        due = now - self.written_ts >= STATE_TTL_S / 3
        if key != self.last_key or due:
            write_state(self.cfg.state_path, state)
            self.written_ts = now
        self.last_key, self.state = key, state
        return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh guard",
                                     description="capacity guard of a lending VM (L159e)")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="one measure, the decision and the state")
    check.add_argument("--config")
    check.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="the guard loop (tranche 1: --observe only)")
    run.add_argument("--config")
    run.add_argument("--observe", action="store_true",
                     help="decide and publish the state, enforce nothing")
    run.add_argument("--once", action="store_true", help="one evaluation, then exit")
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
    if not args.observe:
        print("ameesh guard: enforcing caps comes with tranche 2 of L159e; "
              "use --observe", file=sys.stderr)
        return 2
    guard = Guard(cfg, observe=True)
    while True:
        guard.step()
        if args.once:
            return 0
        time.sleep(EVAL_S)
