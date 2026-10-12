# SPDX-License-Identifier: AGPL-3.0-only
"""The capacity guard contract, shared with Nexlink Compute (lot L159a;
study docs/design/etudes/l159-capacite-des-hotes.md, section 1;
docs/GARDE-DE-CAPACITE.md).

A lending host lends its unused capacity to ameesh agents. Its guard
measures, decides and enforces; the guest obeys the state the guard writes
and acknowledges it. This module holds what both sides agree on and that
ameesh reads itself:

* the offer, `policy.lend` of a `Host` fiche (`parse_lend`, called by
  `canon check`): what the host lends at most. An unreadable offer is
  dropped whole (the host lends nothing), never kept without one of its caps;
* the paths of the gate (`STATE_PATH`, `ACK_PATH`) and the names of the two
  messages (`SCHEMA_HOST_STATE`, `SCHEMA_HOST_ACK`). Their JSON schemas are
  `docs/schemas/`, and the golden sets `tests/dore/capacity_guard/`;
* the freshness of the state a guest obeys (`gate_state`): on a lending
  host, a state without a valid, near `expires_ts` is `stopped`.

The guard itself (`ameesh guard`, L159e) and the common measure
(`resources.measure`, L159d) live elsewhere.
"""
from __future__ import annotations

import os
import re

from . import resources as resources_mod

SCHEMA_HOST_STATE = "ameesh-host-state/1"
SCHEMA_HOST_ACK = "ameesh-host-ack/1"
#: written by the guard; its directory is read-only for the guest
STATE_PATH = "/run/ameesh-gate/state/state.json"
#: written by the guest; its directory is writable by the guest only
ACK_PATH = "/run/ameesh-gate/ack/ack.json"
STATES = ("available", "draining", "stopped")
#: the farthest a state's `expires_ts` may lie ahead of its reader: a guard
#: rewrites it every third of its life (90 s by default); further ahead, a
#: dead guard's `available` would outlive it
MAX_STATE_TTL_S = 300.0
#: what an unreadable or stale gate counts as (`host_gate_fallback`)
FALLBACK_LENDING, FALLBACK_CLASSIC = "stopped", "available"
#: reasons with a fixed meaning (any other text is admitted, and shown as is)
REASONS = (
    # devices (Nexlink Compute)
    "idle", "user_active", "battery", "window_end", "metered", "owner_pause",
    "lease_lost", "shutdown",
    # virtual machines (ameesh guard)
    "app_memory", "app_cpu", "app_busy", "disk_low", "maintenance", "guard_stopping")

#: caps without which an offer would lend without a bound: all required
LEND_CAPS = ("cpu_max", "memory_max", "disk_max")
LEND_SIZES = ("memory_max", "agent_memory_max", "disk_max")
LEND_KEYS = LEND_CAPS + ("cpu_weight", "agent_memory_max", "windows", "reclaim",
                         "drain_seconds", "calm_seconds")
#: the three reclaim keys the guard reads itself (L159a): the contract's
#: reader knows them too, so a valid host fiche using them is not reported
#: as a `host-lend-unknown` warning
GUARD_RECLAIM_KEYS = ("io_pressure_max", "disk_free_min", "disk_paths")
RECLAIM_KEYS = ("memory_available_min", "cpu_pressure_max", "memory_pressure_max",
                "busy_units", "busy_max") + GUARD_RECLAIM_KEYS
DEFAULT_DRAIN_SECONDS = 90
DEFAULT_CALM_SECONDS = 600
#: systemd `CPUWeight=` bounds
WEIGHT_MIN, WEIGHT_MAX = 1, 10000

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY = r"(?:%s)" % "|".join(DAYS)
_WINDOW = re.compile(
    r"^\s*(?P<days>%s(?:-%s)?(?:,%s(?:-%s)?)*)"
    r"(?:\s+(?P<start>\d{2}:\d{2})-(?P<end>\d{2}:\d{2}))?"
    r"(?:\s+(?P<tz>[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*))?\s*$" % ((_DAY,) * 4), re.I)


def _finite(value) -> float | None:
    """`value` as a finite float; None for a bool, a non-number, inf, nan, or
    an integer too large for a float (YAML or JSON digits: no OverflowError)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return resources_mod.finite_float(value)


def gate_state(doc, now: float, *, lending: bool) -> str:
    """The state a guest obeys, given the state document it read (None:
    absent or unreadable) at `now` (section 5.3 of the contract).

    On a lending host the gate is closed by default: a document that is
    unreadable, of another schema, of an unknown state, without a finite
    `expires_ts` (missing or null included), expired (`now >= expires_ts`)
    or expiring more than `MAX_STATE_TTL_S` ahead counts as `stopped`. On a
    classic host an absent or null `expires_ts` is admitted (the gate
    predates it); anything else unreadable or stale falls back to
    `available`, as before."""
    if state_problem(doc, now, lending=lending) is not None:
        return FALLBACK_LENDING if lending else FALLBACK_CLASSIC
    return doc["state"]


def state_problem(doc, now: float, *, lending: bool) -> str | None:
    """Why a guest may not obey the state document `doc` at `now`, or None
    when it may (the rule of `gate_state`, with its reason for the logs)."""
    if not isinstance(doc, dict):
        return "not an object"
    if doc.get("schema") != SCHEMA_HOST_STATE:
        return "schema %s expected" % SCHEMA_HOST_STATE
    if doc.get("state") not in STATES:
        return "unknown state %r" % (doc.get("state"),)
    seq, expires = doc.get("seq"), doc.get("expires_ts")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return "seq: integer expected"
    if expires is None and not lending:
        return None
    expires = _finite(expires)
    if expires is None:
        return "no finite expires_ts (required on a lending host)"
    if now >= expires:
        return "stale (expired %ds ago)" % int(now - expires)
    if expires > now + MAX_STATE_TTL_S:
        return "expires_ts more than %ds ahead" % int(MAX_STATE_TTL_S)
    return None


def parse_window(text) -> dict | None:
    """A lending window, `"mon-fri 18:00-14:00 UTC"` or `"sat-sun"`: `{days,
    start, end, tz}` (`days` in `DAYS` order of the text, `start`/`end`
    minutes since midnight, None for the whole day; an end before the start
    runs past midnight; `tz` UTC unless an IANA zone is named). None when
    unreadable."""
    if not isinstance(text, str):
        return None
    found = _WINDOW.match(text)
    if not found:
        return None
    days: list[str] = []
    for part in found.group("days").lower().split(","):
        first, _, last = part.partition("-")
        i, j = DAYS.index(first), DAYS.index(last or first)
        span = range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1))
        days += [DAYS[k] for k in span if DAYS[k] not in days]
    start = end = None
    if found.group("start"):
        start, end = _minutes(found.group("start")), _minutes(found.group("end"))
        if start is None or end is None or start == end:
            return None
    tz = found.group("tz") or "UTC"
    if tz.upper() == "UTC":
        tz = "UTC"
    else:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - unknown zone, or no tz database
            return None
    return {"days": days, "start": start, "end": end, "tz": tz}


def _minutes(text: str) -> int | None:
    hours, minutes = int(text[:2]), int(text[3:])
    return hours * 60 + minutes if hours < 24 and minutes < 60 else None


def _positive_int(value, minimum: int = 0) -> int | None:
    # an integer too large for a float is refused: seconds and counts meet
    # floats downstream (`now + drain_seconds`)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum \
            or _finite(value) is None:
        return None
    return value


def parse_lend(raw, warnings: list[str] | None = None) -> tuple[dict | None, list[str], list[str]]:
    """(offer, problems, unknown keys) of `policy.lend`.

    The offer keeps sizes in bytes, `cpu_max` in CPUs (float), percentages
    as numbers from 0 to 100, `windows` as written (each one checked) and
    the defaults of `drain_seconds` and `calm_seconds`. With any problem,
    the offer is None: the host lends nothing until its fiche is fixed.

    `warnings` (optional): legacy spellings of `disk_paths` that main
    accepted and that are normalized instead of refusing the offer, so the
    caller can show them (`ameesh canon check`). An upgrade must not stop
    the guard from starting on an offer that was valid before (codex #459
    r3)."""
    if not isinstance(raw, dict):
        return None, ["mapping expected"], []
    problems: list[str] = []
    offer: dict = {}
    for key in LEND_CAPS:
        if raw.get(key) is None:
            problems.append("`%s` required (an offer without it would lend without a bound)"
                            % key)
    cpu = raw.get("cpu_max")
    if cpu is not None:
        if _finite(cpu) is None or cpu <= 0:
            problems.append("`cpu_max`: finite number of CPUs > 0 expected")
        else:
            offer["cpu_max"] = _finite(cpu)
    for key in LEND_SIZES:
        if raw.get(key) is None:
            continue
        octets = resources_mod.parse_bytes(raw[key])
        if not octets:
            problems.append("`%s`: size in bytes > 0 expected (e.g. 14GiB)" % key)
        else:
            offer[key] = octets
    if offer.get("agent_memory_max") and offer.get("memory_max") \
            and offer["agent_memory_max"] > offer["memory_max"]:
        problems.append("`agent_memory_max` above `memory_max`")
    if raw.get("cpu_weight") is not None:
        weight = _positive_int(raw["cpu_weight"], WEIGHT_MIN)
        if weight is None or weight > WEIGHT_MAX:
            problems.append("`cpu_weight`: integer from %d to %d expected"
                            % (WEIGHT_MIN, WEIGHT_MAX))
        else:
            offer["cpu_weight"] = weight
    if raw.get("windows") is not None:
        windows = raw["windows"]
        if isinstance(windows, str):
            windows = [windows]
        if not isinstance(windows, list) or not windows:
            problems.append("`windows`: list of windows expected (e.g. \"mon-fri 18:00-08:00 "
                            "UTC\")")
        else:
            bad = [w for w in windows if parse_window(w) is None]
            if bad:
                problems.append("`windows`: unreadable %s (expected \"<days> [HH:MM-HH:MM] "
                                "[zone]\", days among %s)" % (", ".join(map(repr, bad)),
                                                              "/".join(DAYS)))
            else:
                offer["windows"] = [w.strip() for w in windows]
    for key, default in (("drain_seconds", DEFAULT_DRAIN_SECONDS),
                         ("calm_seconds", DEFAULT_CALM_SECONDS)):
        if raw.get(key) is None:
            offer[key] = default
            continue
        seconds = _positive_int(raw[key], 1 if key == "drain_seconds" else 0)
        if seconds is None:
            problems.append("`%s`: whole number of seconds expected" % key)
        else:
            offer[key] = seconds
    unknown = sorted(str(k) for k in raw if k not in LEND_KEYS)
    if raw.get("reclaim") is not None:
        reclaim, more, extra = _parse_reclaim(raw["reclaim"], warnings)
        problems += ["`reclaim.%s" % p[1:] if p.startswith("`") else "`reclaim`: " + p
                     for p in more]
        unknown += ["reclaim.%s" % k for k in extra]
        if reclaim:
            offer["reclaim"] = reclaim
    return (None if problems else offer), problems, unknown


def _reclaim_path(value, warnings: list[str] | None = None) -> str | None:
    """One path of `disk_paths`: absolute and normalized (codex #459 r1).

    Refused: a relative path (probed from the guard's own directory, it
    would watch another filesystem than the one the fiche names), a `..`
    component (`/srv/../etc` is not `/etc` when `/srv` is a symbolic link),
    a NUL (it raises while measuring) and a blank.

    Accepted and normalized with a warning (codex #459 r3): the spellings
    that provably name the same directory on Linux — several leading slashes
    (`//srv`), `//` inside, a `.` component (`/srv/.`), and a trailing slash
    except for the root. Main accepted them; refusing them would stop the
    guard from starting after the update. The warning tells the operator to
    fix the fiche."""
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or "\x00" in path or not path.startswith("/"):
        return None
    if ".." in path.split("/"):
        return None
    normalized = os.path.normpath("/" + path.lstrip("/"))
    if normalized != (path.rstrip("/") or "/") and warnings is not None:
        warnings.append("`reclaim.disk_paths`: %r normalized to %r (same directory; "
                        "update the fiche)" % (path, normalized))
    return normalized


def _reclaim_bytes(value) -> tuple[int | None, bool]:
    """(octets, tronqué) of a size threshold.

    An exact whole number of bytes passes as is. Otherwise the value is the
    one the previous reader returned by truncating, and `truncated` is true:
    main accepted these sizes, an update must not invalidate an existing
    offer (mesh-design, #459 r4b). Only a result <= 0 is still refused:
    zero bytes watches nothing."""
    exact = resources_mod.parse_bytes(value, decimal_ok=False)
    if exact is not None:
        return exact, False
    truncated = resources_mod.parse_bytes(value, decimal_ok=True)
    if truncated is None:
        return None, False
    return truncated, True


def _parse_reclaim(raw, warnings: list[str] | None = None) -> tuple[dict, list[str], list[str]]:
    """The thresholds of the application at which the guest gives way."""
    if not isinstance(raw, dict):
        return {}, ["mapping expected"], []
    out: dict = {}
    problems: list[str] = []
    for key in RECLAIM_KEYS:
        if key in raw and raw[key] is None:
            # an explicit null is not an absent key: it used to disable the
            # threshold or fall back to a default, silently (codex #459 r1)
            problems.append("`%s`: value expected, explicit null refused" % key)
    for key in ("memory_available_min", "disk_free_min"):
        if raw.get(key) is None:
            continue
        octets, truncated = _reclaim_bytes(raw[key])
        if octets is None or octets <= 0:
            problems.append("`%s`: size in bytes > 0 expected (e.g. %s)"
                            % (key, "6GiB" if key == "memory_available_min" else "10GiB"))
        else:
            out[key] = octets
            if truncated and warnings is not None:
                # main truncated it; the offer stays valid, the operator is
                # told to write a whole number of bytes (codex #459 r4b)
                warnings.append("`%s`: %r truncated to %d bytes (the previous reader "
                                "truncated it too; write a whole number of bytes)"
                                % (key, raw[key], octets))
    for key in ("cpu_pressure_max", "memory_pressure_max", "io_pressure_max"):
        if raw.get(key) is None:
            continue
        percent = resources_mod.parse_percent(raw[key])
        if percent is None:
            problems.append("`%s`: percentage from 0 to 100 expected (PSI some, avg60)" % key)
        else:
            out[key] = percent
    if raw.get("disk_paths") is not None:
        paths = raw["disk_paths"]
        if isinstance(paths, list) and not paths:
            # main read an empty list as its own default (`/`); refusing it
            # would stop the guard from starting after the update (codex #459 r3)
            if warnings is not None:
                warnings.append("`reclaim.disk_paths`: empty list read as [\"/\"] "
                                "(update the fiche)")
            out["disk_paths"] = ["/"]
        elif not isinstance(paths, list):
            problems.append("`disk_paths`: non-empty list of absolute, normalized paths "
                            "expected (e.g. [\"/\", \"/srv/ameesh-guest\"])")
        else:
            clean = [_reclaim_path(p, warnings) for p in paths]
            unique = list(dict.fromkeys(clean))
            refused = [p for p, kept in zip(paths, clean) if kept is None]
            if refused:
                # mesh-design #459 r4b: a relative path, `..` and NUL stay
                # refused (security); the offer falls, but the guard starts
                problems.append("`disk_paths`: refused path(s) %s (absolute, without "
                                "`..` or NUL expected, e.g. [\"/\", \"/srv/ameesh-guest\"])"
                                % ", ".join(repr(p) for p in refused))
            else:
                if len(unique) != len(clean) and warnings is not None:
                    warnings.append("`reclaim.disk_paths`: duplicate paths collapsed to %s "
                                    "(each filesystem is watched once; update the fiche)"
                                    % ", ".join(repr(p) for p in unique))
                out["disk_paths"] = unique
    if raw.get("busy_units") is not None:
        units = raw["busy_units"]
        if isinstance(units, str):
            units = [units]
        if not isinstance(units, list) or not units or not all(
                isinstance(u, str) and u.strip() for u in units):
            problems.append("`busy_units`: list of systemd unit patterns expected")
        else:
            out["busy_units"] = [u.strip() for u in units]
    if raw.get("busy_max") is not None:
        busy = _positive_int(raw["busy_max"], 0)
        if busy is None:
            problems.append("`busy_max`: whole number expected")
        else:
            out["busy_max"] = busy
    if "busy_max" in out and "busy_units" not in out:
        problems.append("`busy_max` without `busy_units`")
    return out, problems, sorted(str(k) for k in raw if k not in RECLAIM_KEYS)


#: mark of a `disk_paths` problem that drops the offer without stopping the
#: guard (mesh-design #459 r4b)
REFUSED_PATH_MARK = "refused path"


def refused_paths_only(problems) -> bool:
    """True when every problem is a refused `disk_paths` entry (relative
    path, `..`, NUL): the offer falls and the guard starts anyway
    (mesh-design #459 r4b)."""
    return bool(problems) and all(
        "disk_paths`" in p and REFUSED_PATH_MARK in p for p in problems)
