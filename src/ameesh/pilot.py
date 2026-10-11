# SPDX-License-Identifier: AGPL-3.0-only
"""Pilot report of a lending host (lot L159g, study L159 §4.4).

Each step of the pilot is measured one week before and one week after,
from the readings of the hosts (`host_resources`, 7 days). The report gives,
per day and over the whole window: the time under pressure (no new turn),
the critical episodes, the load, the `/tmp` tmpfs, the PSI, the turns and
the guest CPU hosted, and the lendable capacity; then the study's targets,
met or not.

Times are weighted by the gap to the next reading, capped: a gap longer
than `MAX_GAP_S` (no runner published) counts for nothing rather than for
an unknown state.
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


def _weighted(readings: list[dict]) -> list[tuple[dict, float]]:
    """Each reading with the time it stands for (gap to the next one)."""
    out = []
    for i, row in enumerate(readings):
        gap = 0.0
        if i + 1 < len(readings):
            gap = float(readings[i + 1]["sampled_ts"]) - float(row["sampled_ts"])
            if gap < 0 or gap > MAX_GAP_S:
                gap = 0.0
        out.append((row, gap))
    return out


def _summary(rows: list[tuple[dict, float]], limits: dict) -> dict:
    pressure_s = turns_s = guest_cpu_s = 0.0
    episodes = 0
    previous_critical = False
    for row, gap in rows:
        verdict = resources.pressure(row, limits=limits)
        if verdict["blocked"]:
            pressure_s += gap
        if verdict["critical"] and not previous_critical:
            episodes += 1
        # a hole in the measure ends an episode
        previous_critical = verdict["critical"] and gap > 0
        turns_s += (row.get("turns_in_progress") or 0) * gap
        guest_cpu_s += (row.get("guest_cpu") or 0.0) * gap
    readings = [row for row, _gap in rows]
    cpus = [r.get("cpu_count") for r in readings if r.get("cpu_count")]

    def values(key):
        return [r.get(key) for r in readings]

    tmpfs = [resources.tmpfs_fraction(r) for r in readings]
    return {
        "samples": len(readings),
        "measured_s": round(sum(gap for _row, gap in rows), 1),
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


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def report(host: str, readings: list[dict], limits: dict, *, days: float) -> dict:
    """The pilot report of `host` (schema `ameesh-pilot-report/1`) from its
    readings, oldest first, judged with its effective `limits`."""
    rows = _weighted(sorted(readings, key=lambda r: float(r["sampled_ts"])))
    by_day: dict[str, list] = {}
    for row, gap in rows:
        by_day.setdefault(_day(row["sampled_ts"]), []).append((row, gap))
    total = _summary(rows, limits)
    measured_days = total["measured_s"] / 86400.0
    cpus = total["cpu_count"]
    checks = []

    def check(key, value, target, ok):
        checks.append({"key": key, "value": value, "target": target,
                       "ok": None if value is None else ok})

    per_day = (total["pressure_min"] / measured_days) if measured_days > 0 else None
    check("pressure_min_per_day", _round(per_day, 1), TARGET_PRESSURE_MIN_PER_DAY,
          per_day is not None and per_day < TARGET_PRESSURE_MIN_PER_DAY)
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
        "first_ts": rows[0][0]["sampled_ts"] if rows else None,
        "last_ts": rows[-1][0]["sampled_ts"] if rows else None,
        "total": total,
        "by_day": [dict(_summary(day_rows, limits), day=day)
                   for day, day_rows in sorted(by_day.items())],
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
    out.append("  lendable   CPU min %s ; memory min %s, median %s" % (
        num(total["lendable_cpu_min"], "%.2f"), _fmt_bytes(total["lendable_memory_min"]),
        _fmt_bytes(total["lendable_memory_median"])))
    for c in rep["checks"]:
        state = "unknown" if c["ok"] is None else "ok" if c["ok"] else "MISSED"
        out.append("  target     %-22s %-8s value %s, target %s"
                   % (c["key"], state, "—" if c["value"] is None else c["value"],
                      "—" if c["target"] is None else c["target"]))
    return out
