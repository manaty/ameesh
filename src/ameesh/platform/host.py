# SPDX-License-Identifier: AGPL-3.0-only
"""Ressources de l'hôte (L63, décisions 0028 et L106) : mémoire, swap,
charge, alimentation, système de fichiers.

Mesures NON sensibles : ce qui n'est pas mesurable sur cet OS est rendu
inconnu (None, ou clé absente) — la répartition et les gardes ne se
déclenchent jamais sur un inconnu. `memory` lève `NotAvailable` pour que
l'appelant sache que rien n'a été lu.
"""
from __future__ import annotations

import os

from . import NotAvailable, is_linux
from .process import PROC, _proc_ok, _psutil

#: sources d'alimentation sous Linux
POWER_SUPPLY_DIR = "/sys/class/power_supply"


# --------------------------------------------------------------------------
# mémoire et swap
# --------------------------------------------------------------------------

def memory(meminfo: str | None = None) -> dict[str, int]:
    """`mem_available_bytes` et `swap_used_bytes` (octets), quand connus.

    `meminfo` : un fichier au format /proc/meminfo (tests, ou repli Linux).
    Un noyau sans `MemAvailable` est traité comme muet pour la mémoire :
    mieux vaut ne rien publier qu'un zéro trompeur."""
    ps = _psutil()
    if meminfo is None and ps is not None:
        out: dict[str, int] = {}
        try:
            vm = ps.virtual_memory()
            out["mem_available_bytes"] = int(vm.available)
            out["mem_total_bytes"] = int(vm.total)
        except Exception:
            pass
        try:
            out["swap_used_bytes"] = int(ps.swap_memory().used)
        except Exception:
            pass
        if not out:
            raise NotAvailable("host memory", "psutil does not report it")
        return out
    if meminfo is None:
        if not _proc_ok():
            raise NotAvailable("host memory", "neither psutil nor /proc")
        meminfo = os.path.join(PROC, "meminfo")
    values: dict[str, int] = {}
    try:
        with open(meminfo, encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if not rest:
                    continue
                values[key.strip()] = int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError) as exc:
        raise NotAvailable("host memory", "%s unreadable: %s" % (meminfo, exc)) from None
    out = {}
    if "MemAvailable" in values:
        out["mem_available_bytes"] = values["MemAvailable"]
        # L159d: total memory, for the prudent lendable share
        if "MemTotal" in values:
            out["mem_total_bytes"] = values["MemTotal"]
    total, free = values.get("SwapTotal"), values.get("SwapFree")
    if total is not None and free is not None:
        out["swap_used_bytes"] = max(0, total - free)
    return out


def load_average() -> float | None:
    """Charge sur 1 minute ; None sans équivalent fiable (Windows)."""
    getter = getattr(os, "getloadavg", None)
    if getter is None:
        return None
    try:
        return float(getter()[0])
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# alimentation (L106)
# --------------------------------------------------------------------------

def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percent(value) -> float | None:
    number = _float(value)
    if number is None or not 0.0 <= number <= 100.0:
        return None
    return number


def power(base: str | None = None) -> dict:
    """`{"on_ac": bool|None, "battery_percent": float|None}`.

    `base` : un dossier au format /sys/class/power_supply (tests). Sans
    `base` : sysfs sous Linux, `psutil.sensors_battery()` ailleurs (macOS,
    Windows). Inconnu : None — on ne devine pas."""
    if base is not None or is_linux():
        return _power_sysfs(base or POWER_SUPPLY_DIR)
    out: dict = {"on_ac": None, "battery_percent": None}
    ps = _psutil()
    sensor = getattr(ps, "sensors_battery", None) if ps is not None else None
    if sensor is None:
        return out
    try:
        battery = sensor()
    except Exception:
        return out
    if battery is None:  # pas de batterie : poste fixe, serveur
        return out
    out["battery_percent"] = _percent(getattr(battery, "percent", None))
    plugged = getattr(battery, "power_plugged", None)
    if isinstance(plugged, bool):
        out["on_ac"] = plugged
    return out


def _power_sysfs(base: str) -> dict:
    """Secteur (`Mains`, `USB*`, champ `online`) et batteries du SYSTÈME
    (`scope` ≠ `Device` : pas la souris), pourcentage pondéré par la capacité
    quand plusieurs batteries. Sans source de secteur déclarée, l'état de
    charge des batteries tranche (`Discharging` = sur batterie)."""
    out: dict = {"on_ac": None, "battery_percent": None}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    mains: list[bool] = []
    batteries: list[tuple[float, float, str]] = []  # (pourcentage, poids, état)
    for name in names:
        root = os.path.join(base, name)
        kind = (_read(os.path.join(root, "type")) or "").lower()
        if kind in ("mains", "usb", "usb_c", "usb_pd", "usb_pd_drp", "wireless"):
            online = _read(os.path.join(root, "online"))
            if online in ("0", "1", "2"):
                mains.append(online != "0")
        elif kind == "battery":
            if (_read(os.path.join(root, "scope")) or "").lower() == "device":
                continue
            if _read(os.path.join(root, "present")) == "0":
                continue
            percent = _percent((_read(os.path.join(root, "capacity")) or "").rstrip("%")
                               or None)
            poids = 1.0
            for now_key, full_key in (("energy_now", "energy_full"),
                                      ("charge_now", "charge_full")):
                now_v = _float(_read(os.path.join(root, now_key)))
                full_v = _float(_read(os.path.join(root, full_key)))
                if now_v is not None and full_v:
                    if percent is None:
                        percent = max(0.0, min(100.0, 100.0 * now_v / full_v))
                    poids = full_v
                    break
            if percent is None:
                continue
            etat = (_read(os.path.join(root, "status")) or "").lower()
            batteries.append((percent, poids, etat))
    if batteries:
        total = sum(b[1] for b in batteries) or 1.0
        out["battery_percent"] = round(sum(b[0] * b[1] for b in batteries) / total, 1)
    if any(mains):
        out["on_ac"] = True
    elif mains:
        out["on_ac"] = False
    elif batteries:
        etats = {b[2] for b in batteries}
        if "discharging" in etats:
            out["on_ac"] = False
        elif etats & {"charging", "full", "not charging"}:
            out["on_ac"] = True
    return out


# --------------------------------------------------------------------------
# système de fichiers
# --------------------------------------------------------------------------

def mount_of(path: str, mounts: str | None = None) -> tuple[str, str] | None:
    """(point de montage, type) du système de fichiers qui porte `path`, ou
    None. `mounts` : un fichier au format /proc/mounts (tests)."""
    real = os.path.realpath(path)
    entries: list[tuple[str, str]] = []
    if mounts is not None or _proc_ok():
        try:
            with open(mounts or os.path.join(PROC, "mounts"), encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3:
                        entries.append((parts[1].replace("\\040", " "), parts[2]))
        except OSError:
            return None
    else:
        ps = _psutil()
        if ps is None:
            return None
        try:
            entries = [(p.mountpoint, p.fstype) for p in ps.disk_partitions(all=True)]
        except Exception:
            return None
    best: tuple[str, str] | None = None
    for point, fstype in entries:
        root = point.rstrip("/\\")
        if real == point or real.startswith(root + os.sep) or point in ("/", os.sep):
            if best is None or len(point) > len(best[0]):
                best = (point, fstype)
    return best


# --------------------------------------------------------------------------
# pressure, CPU time and cgroups (L159d)
# --------------------------------------------------------------------------

#: pressure stall information of the host (Linux 4.20 and later)
PSI_DIR = os.path.join(PROC, "pressure")
#: busy CPU time of the host
PROC_STAT = os.path.join(PROC, "stat")
#: root of the cgroup v2 hierarchy
CGROUP_ROOT = "/sys/fs/cgroup"


def parse_pressure(text: str) -> dict:
    """A PSI file (`some avg10=0.00 avg60=0.00 avg300=0.00 total=0`):
    {line: {key: value}}; unreadable lines are skipped."""
    out: dict = {}
    for line in (text or "").splitlines():
        kind, _, rest = line.strip().partition(" ")
        if kind not in ("some", "full"):
            continue
        values = {}
        for item in rest.split():
            key, _, value = item.partition("=")
            number = _float(value)
            if number is not None:
                values[key] = number
        out[kind] = values
    return out


def pressure(resource: str, base: str | None = None) -> dict:
    """PSI of `resource` (`cpu`, `memory`, `io`), parsed; {} when unknown
    (outside Linux, before Linux 4.20, `psi=0`). `base`: a folder in the
    format of /proc/pressure, for the tests."""
    if base is None and not is_linux():
        return {}
    return parse_pressure(_read(os.path.join(base or PSI_DIR, resource)) or "")


def cpu_busy_seconds(stat: str | None = None) -> float | None:
    """Busy CPU time of the host since boot (line `cpu` of /proc/stat), in
    seconds: everything but `idle` and `iowait`; `guest` and `guest_nice`
    are already counted in `user` and `nice`. None when unknown. `stat`: a
    file in the format of /proc/stat, for the tests."""
    if stat is None and not is_linux():
        return None
    for line in (_read(stat or PROC_STAT) or "").splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        try:
            ticks = [float(x) for x in fields[1:9]]
        except ValueError:
            return None
        while len(ticks) < 8:
            ticks.append(0.0)
        user, nice, system, _idle, _iowait, irq, softirq, steal = ticks
        try:
            hz = float(os.sysconf("SC_CLK_TCK"))
        except (AttributeError, OSError, ValueError):
            hz = 100.0
        return (user + nice + system + irq + softirq + steal) / (hz or 100.0)
    return None


def cgroup_cpu_seconds(path: str) -> float | None:
    """CPU time used by a cgroup (`cpu.stat`, `usage_usec`), in seconds."""
    for line in (_read(os.path.join(path, "cpu.stat")) or "").splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            number = _float(value)
            return None if number is None else number / 1e6
    return None


def cgroup_memory_bytes(path: str) -> int | None:
    """Memory charged to a cgroup (`memory.current`), in bytes."""
    number = _float(_read(os.path.join(path, "memory.current")))
    return None if number is None else int(number)
