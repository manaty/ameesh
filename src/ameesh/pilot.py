# SPDX-License-Identifier: AGPL-3.0-only
"""Pilot report of a lending host (lot L159g, study L159 §4.4).

Each step of the pilot is measured one week before and one week after,
from the readings of the hosts (`host_resources`, 7 days). The report gives,
per day and over the whole window: the time under pressure (no new turn),
the critical episodes, the load, the `/tmp` tmpfs, the PSI, the turns and
the guest CPU hosted, and the lendable capacity; then the study's targets,
met or not.

A state read at a reading (pressure, turns in progress) holds until the
next reading; an average over the window before it (`guest_cpu`, over
`cpu_window_s`) stands for that window. A gap longer than `MAX_GAP_S` (no
runner published) counts for nothing rather than for an unknown state.
Every interval is cut at midnight (local time) into the days it covers.
"""
from __future__ import annotations

import time

from . import resources

#: a longer gap between two readings is a hole in the measure
MAX_GAP_S = 300.0

#: study §4.4 targets
TARGET_PRESSURE_MIN_PER_DAY = 10.0
TARGET_LOAD_P95_PER_CPU = 1.5       # 18 for the 12 CPUs of the workstation
TARGET_TMPFS_MAX = 0.8
TARGET_PSI_MEM_P95 = 1.0


def percentile(values, q: float) -> float | None:
    """The `q` percentile (0 to 1) of `values`, interpolated; None if empty."""
    data = sorted(float(v) for v in values if v is not None)
    if not data:
        return None
    pos = (len(data) - 1) * q
    low = int(pos)
    high = min(low + 1, len(data) - 1)
    return data[low] + (data[high] - data[low]) * (pos - low)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def _next_midnight(ts: float) -> float:
    t = time.localtime(float(ts))
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return midnight if midnight > ts else ts + 1.0  # never stalls on a DST oddity


def _by_day(start: float, end: float) -> list[tuple[str, float]]:
    """[start, end) cut at midnight: (day, seconds) for each day it covers."""
    out = []
    while end > start:
        cut = min(end, _next_midnight(start))
        out.append((_day(start), cut - start))
        start = cut
    return out


def _intervals(readings: list[dict]) -> list[tuple[str, dict, float, float]]:
    """The time each reading stands for, cut by day: (day, row, held,
    window) seconds — `held`, its state up to the next reading; `window`,
    its averages (`guest_cpu`) over the window before it, at most the gap
    to the previous reading (the earlier time is that reading's)."""
    out = []
    for i, row in enumerate(readings):
        ts = float(row["sampled_ts"])
        if i + 1 < len(readings):
            gap = float(readings[i + 1]["sampled_ts"]) - ts
            if 0 < gap <= MAX_GAP_S:
                out += [(day, row, secs, 0.0) for day, secs in _by_day(ts, ts + gap)]
        window = row.get("cpu_window_s")
        before = ts - float(readings[i - 1]["sampled_ts"]) if i else None
        if window is None:
            window = before if before is not None and before <= MAX_GAP_S else 0.0
        elif before is not None:
            window = min(float(window), before)
        window = min(max(float(window), 0.0), MAX_GAP_S)
        if window > 0:
            out += [(day, row, 0.0, secs) for day, secs in _by_day(ts - window, ts)]
    return out


def _episode_starts(readings: list[dict], limits: dict) -> list[dict]:
    """The readings that open a critical episode; a hole in the measure
    ends an episode."""
    out = []
    previous_critical = False
    previous_ts = None
    for row in readings:
        ts = float(row["sampled_ts"])
        if previous_ts is not None and not 0 < ts - previous_ts <= MAX_GAP_S:
            previous_critical = False
        critical = resources.pressure(row, limits=limits)["critical"]
        if critical and not previous_critical:
            out.append(row)
        previous_critical, previous_ts = critical, ts
    return out


def _summary(readings: list[dict], intervals, episodes: int, limits: dict) -> dict:
    pressure_s = turns_s = guest_cpu_s = measured_s = 0.0
    for _day_, row, held, window in intervals:
        if held:
            measured_s += held
            if resources.pressure(row, limits=limits)["blocked"]:
                pressure_s += held
            turns_s += (row.get("turns_in_progress") or 0) * held
        guest_cpu_s += (row.get("guest_cpu") or 0.0) * window
    cpus = [r.get("cpu_count") for r in readings if r.get("cpu_count")]

    def values(key):
        return [r.get(key) for r in readings]

    tmpfs = [resources.tmpfs_fraction(r) for r in readings]
    return {
        "samples": len(readings),
        "measured_s": round(measured_s, 1),
        "pressure_min": round(pressure_s / 60.0, 1),
        "critical_episodes": episodes,
        "cpu_count": max(cpus) if cpus else None,
        "load_p95": _round(percentile(values("load1"), 0.95)),
        "load_max": _round(max((v for v in values("load1") if v is not None), default=None)),
        "tmpfs_max": _round(max((v for v in tmpfs if v is not None), default=None), 4),
        "psi_cpu_p95": _round(percentile(values("psi_cpu_some"), 0.95)),
        "psi_mem_p95": _round(percentile(values("psi_mem_some"), 0.95)),
        "psi_mem_full_max": _round(max((v for v in values("psi_mem_full")
                                        if v is not None), default=None)),
        "turn_hours": round(turns_s / 3600.0, 2),
        "guest_cpu_hours": round(guest_cpu_s / 3600.0, 2),
        "lendable_cpu_min": _round(min((v for v in values("lendable_cpu")
                                        if v is not None), default=None)),
        "lendable_memory_min": min((v for v in values("lendable_memory_bytes")
                                    if v is not None), default=None),
        "lendable_memory_median": _int(percentile(values("lendable_memory_bytes"), 0.5)),
    }


def _round(value, digits: int = 2):
    return None if value is None else round(float(value), digits)


def _int(value):
    return None if value is None else int(value)


def report(host: str, readings: list[dict], limits: dict, *, days: float) -> dict:
    """The pilot report of `host` (schema `ameesh-pilot-report/1`) from its
    readings, oldest first, judged with its effective `limits`."""
    rows = sorted(readings, key=lambda r: float(r["sampled_ts"]))
    intervals = _intervals(rows)
    starts = _episode_starts(rows, limits)
    daily: dict[str, dict] = {}

    def day_of(name):
        return daily.setdefault(name, {"readings": [], "intervals": [], "episodes": 0})

    for row in rows:
        day_of(_day(row["sampled_ts"]))["readings"].append(row)
    for interval in intervals:
        day_of(interval[0])["intervals"].append(interval)
    for row in starts:
        day_of(_day(row["sampled_ts"]))["episodes"] += 1
    total = _summary(rows, intervals, len(starts), limits)
    by_day = [dict(_summary(d["readings"], d["intervals"], d["episodes"], limits), day=name)
              for name, d in sorted(daily.items())]
    cpus = total["cpu_count"]
    checks = []

    def check(key, value, target, ok):
        checks.append({"key": key, "value": value, "target": target,
                       "ok": None if value is None else ok})

    # the target holds for each day, not on average: the worst measured day
    worst = max((d["pressure_min"] for d in by_day if d["measured_s"] > 0), default=None)
    check("pressure_min_per_day", worst, TARGET_PRESSURE_MIN_PER_DAY,
          worst is not None and worst < TARGET_PRESSURE_MIN_PER_DAY)
    load_target = TARGET_LOAD_P95_PER_CPU * cpus if cpus else None
    check("load_p95", total["load_p95"], load_target,
          load_target is not None and total["load_p95"] is not None
          and total["load_p95"] < load_target)
    check("critical_episodes", total["critical_episodes"] if rows else None, 0,
          total["critical_episodes"] == 0)
    check("tmpfs_max", total["tmpfs_max"], TARGET_TMPFS_MAX,
          total["tmpfs_max"] is not None and total["tmpfs_max"] <= TARGET_TMPFS_MAX)
    check("psi_mem_p95", total["psi_mem_p95"], TARGET_PSI_MEM_P95,
          total["psi_mem_p95"] is not None and total["psi_mem_p95"] < TARGET_PSI_MEM_P95)
    return {
        "schema": "ameesh-pilot-report/1",
        "host": host,
        "days": days,
        "first_ts": rows[0]["sampled_ts"] if rows else None,
        "last_ts": rows[-1]["sampled_ts"] if rows else None,
        "total": total,
        "by_day": by_day,
        "checks": checks,
    }


def format_report(rep: dict) -> list[str]:
    """`ameesh hosts HOST --report`, as text."""
    from .mesh_cli import _fmt_bytes

    def num(value, fmt="%.1f"):
        return "—" if value is None else fmt % value

    total = rep["total"]
    out = ["%s — pilot report, %s day(s), %d reading(s), %.1f h measured"
           % (rep["host"], num(rep["days"], "%g"), total["samples"],
              total["measured_s"] / 3600.0)]
    if not total["samples"]:
        out.append("  no reading in the window")
        return out
    out.append("  %-10s %8s %5s %7s %7s %6s %7s %7s %7s %7s"
               % ("day", "press.", "crit", "load95", "loadmax", "tmpfs", "cpuPSI",
                  "memPSI", "turns h", "guest h"))
    for day in rep["by_day"] + [dict(total, day="total")]:
        out.append("  %-10s %6s m %5d %7s %7s %6s %7s %7s %7s %7s" % (
            day["day"], num(day["pressure_min"]), day["critical_episodes"],
            num(day["load_p95"]), num(day["load_max"]),
            "—" if day["tmpfs_max"] is None else "%d %%" % round(day["tmpfs_max"] * 100),
            num(day["psi_cpu_p95"]), num(day["psi_mem_p95"]),
            num(day["turn_hours"], "%.2f"), num(day["guest_cpu_hours"], "%.2f")))
    out.append("  lendable   CPU min %s; memory min %s, median %s" % (
        num(total["lendable_cpu_min"], "%.2f"), _fmt_bytes(total["lendable_memory_min"]),
        _fmt_bytes(total["lendable_memory_median"])))
    for c in rep["checks"]:
        state = "unknown" if c["ok"] is None else "ok" if c["ok"] else "MISSED"
        out.append("  target     %-22s %-8s value %s, target %s"
                   % (c["key"], state, "—" if c["value"] is None else c["value"],
                      "—" if c["target"] is None else c["target"]))
    return out
