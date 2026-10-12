# SPDX-License-Identifier: AGPL-3.0-only
"""Ressources des hôtes (lot L31, décision 0028).

Chaque exécuteur relève périodiquement l'état de son hôte — mémoire
disponible, swap utilisé, charge CPU (1 minute), disque libre et allocation
btrfs (#246) de chaque volume où l'hôte écrit (#253), tours en cours — et le publie
en base (`host_resources`). `ameesh hosts` montre le dernier relevé de
chaque hôte et l'historique court.

Les MESURES sont de l'état d'exécution (base) ; les SEUILS sont déclaratifs
(canon, `policy.resources` de la fiche `Host`) et lus par
[`thresholds`] / [`breaches`]. Toutes les écritures passent par le stockage
(`storage.of(db).hosts`, spec §10) ; aucune écriture automatique du canon.

Unités : mémoire, swap et disque en OCTETS (un entier, ou un texte avec
unité `KiB`/`MiB`/`GiB`/`TiB` — base 1024 — ou `KB`/`MB`/`GB`/`TB` — base
1000). `max_load` est une charge système (flottant), comparée à la charge 1
minute. `min_mem_available`, `min_disk_free`, `min_btrfs_unallocated` et
`min_btrfs_metadata_headroom` sont des planchers ; les autres sont des plafonds.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time

from . import platform, storage
from .db import Db

#: valeurs par défaut prudentes des seuils (0028), quand la fiche Host n'en
#: déclare pas : 1 Gio de mémoire disponible, 8 Gio de swap, 2×CPU de charge,
#: 2 Gio de disque libre. Le plancher de mémoire est le signal de saturation ;
#: le plafond de swap est haut parce qu'un poste de développement peut garder
#: plusieurs Gio de swap ANCIEN sans pression réelle (l'incident de 0028 était
#: à ~12 Go) ; l'hôte qui veut plus strict le déclare dans `policy.resources`.
DEFAULT_MIN_MEM_AVAILABLE = 1024 ** 3
DEFAULT_MAX_SWAP_USED = 8 * 1024 ** 3
DEFAULT_MAX_LOAD_FACTOR = 2.0
DEFAULT_MIN_DISK_FREE = 2 * 1024 ** 3
#: #246: btrfs floors. On 2026-10-11 the host's btrfs was fully allocated
#: (1 MiB unallocated) with its metadata exhausted while `df` still showed
#: 4.7 then 16 GB free: `min_disk_free` never fired. Unallocated space is what
#: new metadata chunks are carved from; metadata headroom is what is left in
#: the chunks already allocated plus what they can still grow into. Only
#: measured on btrfs (None elsewhere).
DEFAULT_MIN_BTRFS_UNALLOCATED = 10 * 1024 ** 3
DEFAULT_MIN_BTRFS_METADATA_HEADROOM = 1024 ** 3
#: the btrfs floors, in their order in readings and messages
BTRFS_KEYS = ("min_btrfs_unallocated", "min_btrfs_metadata_headroom")
#: L73 : part maximale d'un /tmp en tmpfs (mémoire et swap) occupée avant la
#: contre-pression. Ne s'applique qu'à un tmpfs : un /tmp sur disque relève
#: du disque libre. Critique à mi-chemin entre le seuil et le plein.
DEFAULT_MAX_TMPFS_USED = 0.8
#: dossier temporaire du système mesuré (surchargé par AMEESH_SYSTEM_TMP)
DEFAULT_SYSTEM_TMP = "/tmp"

#: facteurs de gravité : au-delà, la pression est CRITIQUE (pause des agents
#: les moins prioritaires, jamais au milieu d'un tour).
CRITICAL_MEM_FACTOR = 0.5      # mémoire disponible <= la moitié du plancher
CRITICAL_DISK_FACTOR = 0.5     # disque libre <= la moitié du plancher
CRITICAL_BTRFS_FACTOR = 0.5    # #246: btrfs non alloué / marge métadonnées <= la moitié
CRITICAL_SWAP_FACTOR = 2.0     # swap >= le double du plafond
CRITICAL_LOAD_FACTOR = 2.0     # charge >= le double du plafond

#: L106 : alimentation d'un portable. Sur batterie, sous `min_battery_percent`
#: aucun NOUVEAU tour ne part (alerte `host_power_low`) ; sous
#: `stop_battery_percent`, l'exécuteur s'arrête proprement (tours finis ou
#: arrêtés au point sûr, consignes remises en attente, baux rendus) ; au retour
#: du secteur, il reprend seul.
#:
#: 25 % : un tour dure jusqu'à 15 min (`session_max_turn_seconds`) et une
#: dizaine d'agents vident une batterie de portable en moins d'une heure
#: (2026-10-10 : débranché à 11:06, « batterie faible » à 11:56, coupure à
#: 12:10) — les tours en cours doivent pouvoir finir avant le seuil d'arrêt.
#: 10 % : au-dessus des seuils d'UPower (critique 5 %, action 2 %), qui
#: mettent l'hôte en veille ou l'éteignent sans attendre personne ; il reste
#: quelques minutes pour l'arrêt propre (`power_stop_grace`, 120 s).
DEFAULT_MIN_BATTERY_PERCENT = 25.0
DEFAULT_STOP_BATTERY_PERCENT = 10.0
POWER_KEYS = ("min_battery_percent", "stop_battery_percent")

#: L159d: ceiling on the host CPU pressure (PSI `cpu some`, 60 s average, in
#: percent). The load says how many tasks wait, not how long the user waits:
#: on 10/10 a load of 43 for 12 CPU did not say how badly the workstation
#: suffered, the PSI does. No default: a host declares it in its Host fiche.
PRESSURE_KEYS = ("max_cpu_pressure",)
#: clés de seuil reconnues dans `policy.resources` (liste fermée)
THRESHOLD_KEYS = ("min_mem_available", "max_swap_used", "max_load", "min_disk_free",
                  "max_tmpfs_used", *POWER_KEYS, *PRESSURE_KEYS,
                  *BTRFS_KEYS)
#: L159c : plafonds mémoire des unités d'agent, dans `policy.resources` aussi.
#: Ce ne sont pas des seuils de contre-pression : systemd les fait respecter
#: (`MemoryMax`), `ameesh doctor` vérifie l'écart entre le canon et l'unité.
#: `agent_memory_max` : plafond de chaque agent de l'hôte ;
#: `agent_memory_max_by_agent` : {agent: plafond}, qui l'emporte pour cet agent.
AGENT_MEMORY_KEYS = ("agent_memory_max", "agent_memory_max_by_agent")
#: plafond dur par agent quand aucune fiche n'en déclare : 2 à 4 Go par harnais
#: actif (étude L159), 4,1 Go le 10/10 sur la VM du mesh avant l'OOM
DEFAULT_AGENT_MEMORY_MAX = 6 * 1024 ** 3
#: `MemoryHigh` (reprise de mémoire, ralentissement) à cette part du plafond dur
AGENT_MEMORY_HIGH_RATIO = 0.9
#: seuils exprimés en PART (0 < x <= 1, ou texte « 80% ») et non en octets
FRACTION_KEYS = ("max_tmpfs_used",)
#: dossier des sources d'alimentation (Linux) ; surchargé par les tests
POWER_SUPPLY_ENV = "AMEESH_POWER_SUPPLY_DIR"
POWER_SUPPLY_DIR = platform.host.POWER_SUPPLY_DIR

#: horizon de conservation des relevés (secondes) ; l'historique court de
#: `ameesh hosts` n'a pas besoin de plus, et la table ne grandit pas sans fin.
HISTORY_RETENTION_S = 7 * 24 * 3600.0

_UNITS = {
    "": 1,
    "b": 1,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
    "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    # L159c : suffixes de systemd (`MemoryMax=6G`), en base 1024 comme lui
    "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4,
}
_BYTES_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$")


def parse_bytes(value, *, decimal_ok: bool = True) -> int | None:
    """Octets d'un seuil : entier (octets), ou texte « 512MiB », « 2GB ».

    Rend None si la valeur est absente ou illisible (l'appelant décide du
    défaut ou du constat). Un texte négatif n'est pas un octet valide.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        # 1 followed by 400 zeros: no float, so no size (codex #212 r4)
        return value if value >= 0 and finite_float(value) is not None else None
    if isinstance(value, float):
        # .inf, .nan (YAML): no size, never an unbounded one
        if not decimal_ok or not math.isfinite(value) or value < 0:
            return None
        return int(value)
    if not isinstance(value, str):
        return None
    match = _BYTES_RE.match(value)
    if not match:
        return None
    number, unit = match.group(1), match.group(2).lower()
    if unit not in _UNITS:
        return None
    factor = _UNITS[unit]
    if not decimal_ok and "." in number and factor == 1:
        return None
    size = float(number) * factor
    return int(size) if math.isfinite(size) else None


def parse_fraction(value) -> float | None:
    """Une part (0 < x <= 1) : nombre, ou texte « 80% » ; None si illisible."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = finite_float(value)
    else:
        return None
    return number if number is not None and 0.0 < number <= 1.0 else None


def finite_float(value) -> float | None:
    """`value` (number or text) as a finite float; None for None, a bool,
    unreadable text, inf, nan, or an integer too large for a float (YAML or
    JSON digits: never an OverflowError). Every number of a host policy or
    of a guest state goes through it (codex #212 r4)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_percent(value) -> float | None:
    """Un pourcentage de 0 à 100 (« 25 », 25, « 25% »), ou None."""
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
    number = finite_float(value)
    if number is None or not 0.0 <= number <= 100.0:
        return None
    return number


def boot_time(path: str | None = None) -> float | None:
    """Instant du démarrage de l'hôte (epoch), ou None s'il est inconnu (L106,
    via la couche plateforme L63)."""
    try:
        return platform.boot_time(path)
    except platform.NotAvailable:
        return None


def power(base: str | None = None) -> dict:
    """Alimentation de l'hôte (L106) : `on_ac` (secteur), `battery_percent`.

    Lue par la couche plateforme (L63) : sysfs sous Linux, psutil sous macOS
    et Windows. `base` (ou `AMEESH_POWER_SUPPLY_DIR`) : un dossier au format
    /sys/class/power_supply, pour les tests. Inconnu : None — on ne devine
    pas, et aucune garde ne se déclenche sur un inconnu."""
    return platform.power(base or os.environ.get(POWER_SUPPLY_ENV) or None)


def _meminfo(path: str | None = None) -> dict[str, int]:
    """(mémoire disponible, swap utilisé) en octets, par la couche plateforme ;
    `path` : un fichier au format /proc/meminfo. Illisible : {} (rien publié)."""
    try:
        return platform.memory(path)
    except platform.NotAvailable:
        return {}


def cpu_count() -> int | None:
    try:
        return os.cpu_count() or None
    except OSError:  # pragma: no cover - os.cpu_count ne lève pas, mais restons fermes
        return None


def load1() -> float | None:
    """Charge système sur 1 minute, ou None si le poste ne la donne pas."""
    return platform.load_average()


def disk_free(path: str | None) -> int | None:
    if not path:
        return None
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def btrfs_usage(path: str | None) -> dict:
    """#246: unallocated space and metadata headroom of the btrfs holding
    `path` (bytes), None when not btrfs or unreadable."""
    return platform.btrfs_allocation(path)


def _mount_of(path: str, mounts: str | None = None) -> tuple[str, str] | None:
    """(point de montage, type) du système de fichiers qui porte `path`."""
    return platform.mount_of(path, mounts)


def _fs_mount(path: str) -> tuple[str, str] | None:
    """(point, type) of the mount `path` actually resolves to (#289 r2): the
    one whose id the kernel gives (`mount_id`), else the deepest visible one
    over the resolved path (`covering_mount`: a mount over a parent hides a
    deeper one), else `mount_of` (no mountinfo: not Linux)."""
    mounts = platform.mountinfo() if platform.is_linux() else []
    if mounts:
        wanted = platform.mount_id(path)
        found = next((m for m in mounts if wanted is not None and m.get("id") == wanted),
                     None) or platform.covering_mount(os.path.realpath(path), mounts)
        if found is not None:
            return found["point"], found["fstype"]
    return _mount_of(path)


def fs_usage(path: str | None, *, mounts: str | None = None) -> dict:
    """Occupation du système de fichiers de `path` (L73) : chemin, type
    (`tmpfs`, `ext4`…), taille et octets utilisés ; valeurs None si
    illisibles. Lecture seule, sans parcours : `shutil.disk_usage` seulement."""
    out = {"tmp_path": path, "tmp_fstype": None, "tmp_size_bytes": None,
           "tmp_used_bytes": None}
    if not path or not os.path.isdir(path):
        return out
    mount = _mount_of(path, mounts) if mounts is not None else _fs_mount(path)
    if mount is not None:
        out["tmp_fstype"] = mount[1]
    try:  # mêmes valeurs que statvfs (f_blocks, f_bfree), sur tous les OS
        usage = shutil.disk_usage(path)
    except OSError:
        return out
    out["tmp_size_bytes"] = int(usage.total)
    out["tmp_used_bytes"] = int(usage.used)
    return out


# --------------------------------------------------------------------------
# #253: every filesystem the host writes to
# --------------------------------------------------------------------------

#: the floors read on every volume: (floor, reading field)
VOLUME_FLOORS = (("min_disk_free", "disk_free_bytes"),
                 ("min_btrfs_unallocated", "btrfs_unallocated_bytes"),
                 ("min_btrfs_metadata_headroom", "btrfs_metadata_headroom_bytes"))
#: `docker info` is best effort: bounded, and a failure is retried later
DOCKER_INFO_TIMEOUT_S = 5.0
DOCKER_RETRY_S = 600.0
_docker_root: dict = {}


def docker_root(now: float | None = None) -> str | None:
    """Docker's root dir (`docker info`), best effort: None without docker or
    a daemon that answers. A found root is kept for the process; a failure is
    retried after `DOCKER_RETRY_S`."""
    now = time.monotonic() if now is None else now
    if "root" in _docker_root and (_docker_root["root"] is not None
                                   or now - _docker_root["at"] < DOCKER_RETRY_S):
        return _docker_root["root"]
    root = None
    if shutil.which("docker"):
        try:
            done = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"],
                                  capture_output=True, text=True,
                                  timeout=DOCKER_INFO_TIMEOUT_S)
            if done.returncode == 0 and done.stdout.strip().startswith("/"):
                root = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            root = None
    _docker_root.update(root=root, at=now)
    return root


def tmp_paths(cfg, policy=None) -> list[str]:
    """The turns' tmp root (with a state dir) and the system tmp (#306)."""
    from . import menage as menage_mod
    tmps = []
    if getattr(cfg, "state_dir", None):
        try:  # a reading never fails on a managed root
            tmps.append(menage_mod.tmp_root(cfg, policy or menage_mod.Policy()))
        except Exception:
            pass
    return tmps + [system_tmp()]


def disk_paths(cfg, policy=None) -> list[str]:
    """The paths whose filesystems the guard watches (#253): every work root
    that exists (the current directory without any), the state dir, the deps
    cache root, the turns' tmp root and the system tmp (both unless in memory:
    a tmpfs has its own measure, `tmpfs_fraction`), docker's root dir (best
    effort) and `/`."""
    from . import menage as menage_mod
    paths = [os.path.expanduser(root) for root in getattr(cfg, "worktree_roots", ()) or ()]
    paths = [p for p in paths if os.path.isdir(p)] or [os.getcwd()]
    state_dir = getattr(cfg, "state_dir", None)
    if state_dir:
        paths.append(state_dir)
        try:  # a reading never fails on a managed root
            paths.append(menage_mod.cache_root(cfg, policy or menage_mod.Policy()))
        except Exception:
            pass
    paths += [p for p in tmp_paths(cfg, policy) if not menage_mod.in_memory(p)]
    docker = docker_root()
    if docker:
        paths.append(docker)
    paths.append(os.sep)
    return paths


def _within(path: str, point: str) -> bool:
    return path == point or path.startswith(point.rstrip(os.sep) + os.sep)


def _anchor(path: str) -> str | None:
    """`path`, or its nearest ancestor that can be stat'ed: a missing dir (a
    cache not created yet) or one behind a dir that denies traversal
    (/data/private/docker) is read on the filesystem of that ancestor.

    #306: never above the path's own mount point (`covering_mount`, read in
    the mount table without stat): when that point is out of reach (a btrfs
    mounted AT /data/private/docker), its filesystem is read through another
    reachable mount of it; None when there is none (the volume is unreadable,
    never credited to the parent filesystem). The mount is looked up on the
    path with its symlinks resolved, and another mount is used only if the
    kernel resolves its point to that very mount (`mount_id`: not hidden by
    a mount over one of its parents) (#289 r1)."""
    path = os.path.realpath(path)
    current = path
    while True:
        try:
            os.stat(current)
            break
        except OSError:
            parent = os.path.dirname(current.rstrip(os.sep)) or os.sep
            if parent == current:
                return path
            current = parent
    mounts = platform.mountinfo() if platform.is_linux() else []
    own = platform.covering_mount(path, mounts)
    if own is None or _within(current, own["point"]):
        return current
    for other in mounts:
        if other["dev"] != own["dev"] or other["point"] == own["point"] \
                or platform.covering_mount(other["point"], mounts) is not other:
            continue  # another filesystem, or this mount is overmounted
        if other.get("id") is None or platform.mount_id(other["point"]) != other["id"]:
            continue  # out of reach, or another filesystem shows there
        if not _readable(other["point"]):
            continue  # #289 r2: the btrfs ioctls need it open read-only
        return other["point"]
    return None


def _readable(path: str) -> bool:
    """Can `path` be opened read-only (what the btrfs ioctls and a listing
    need; O_PATH alone succeeds on a mode-000 directory)?"""
    try:
        os.close(os.open(path, os.O_RDONLY))
        return True
    except OSError:
        return False


def _probe(path: str) -> str:
    """`path` (or its `_anchor`), or its nearest parent ON THE SAME FILESYSTEM
    that can be opened: the btrfs ioctls open the path (docker's root is not
    readable by the executor); never a parent across a mount point."""
    path = _anchor(path) or path
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return path
    current = path
    while not os.access(current, os.R_OK):
        parent = os.path.dirname(current.rstrip(os.sep)) or os.sep
        if parent == current:
            break
        try:
            if os.stat(parent).st_dev != dev:
                break
        except OSError:
            break
        current = parent
    return current


def volume_key(path: str) -> str:
    """The identity of the filesystem holding `path`: its btrfs fsid (two
    subvolumes of one btrfs are one volume, their st_dev differ), else its
    st_dev, else the path itself (unreadable: sampled on its own); read on
    the path's `_anchor`."""
    anchor = _anchor(path)
    if anchor is None:  # #306: an unreachable mount is a volume of its own
        own = platform.covering_mount(os.path.realpath(path), platform.mountinfo())
        return "mount:%d" % own["dev"]
    found = platform.btrfs_fs_info(_probe(path)) if platform.is_linux() else None
    if found is not None and found[0]:
        return "btrfs:%s" % found[0]
    try:
        return "dev:%d" % os.stat(anchor).st_dev
    except OSError:
        return "path:%s" % path


def volumes(paths) -> list[dict]:
    """One entry per distinct filesystem among `paths` (#253), in the order
    of `paths`: `path` (the first that names it), `paths`, `disk_free_bytes`,
    `btrfs_unallocated_bytes`, `btrfs_metadata_headroom_bytes` (None when
    unreadable or not btrfs); `unreadable` when the filesystem cannot be
    reached at all (#306)."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for path in paths or ():
        if not path:
            continue
        path = os.path.abspath(os.path.expanduser(path))
        key = volume_key(path)
        if key in seen:
            if path not in seen[key]["paths"]:
                seen[key]["paths"].append(path)
            continue
        volume = {"path": path, "paths": [path]}
        if _anchor(path) is None:  # #306: out of reach, read as unknown
            volume.update(dict.fromkeys(f for _floor, f in VOLUME_FLOORS), unreadable=True)
        else:
            probe = _probe(path)
            volume["disk_free_bytes"] = disk_free(probe)
            volume.update(btrfs_usage(probe))
        seen[key] = volume
        out.append(volume)
    return out


def worst(vols: list[dict], field: str) -> dict | None:
    """The volume with the lowest reading of `field`, None when none reads it."""
    known = [v for v in vols or () if v.get(field) is not None]
    return min(known, key=lambda v: v[field]) if known else None


def tmp_usage(tmps) -> dict:
    """`fs_usage` of the fullest tmpfs among `tmps` (#306: the turns' tmp root
    can be a tmpfs of its own), else of the last one (the system tmp). A tmp
    root not created yet is read on its `_anchor`, the filesystem it will be
    created on (#289 r1)."""
    usages = [dict(fs_usage(_anchor(p) or p), tmp_path=p) for p in tmps]
    known = [u for u in usages if tmpfs_fraction(u) is not None]
    return max(known, key=tmpfs_fraction) if known else usages[-1]


def system_tmp() -> str:
    """Le dossier temporaire du système mesuré (`AMEESH_SYSTEM_TMP`, /tmp)."""
    return os.environ.get("AMEESH_SYSTEM_TMP") or DEFAULT_SYSTEM_TMP


def tmpfs_fraction(reading: dict) -> float | None:
    """Part occupée du /tmp d'un relevé s'il est en tmpfs, sinon None."""
    if (reading or {}).get("tmp_fstype") != "tmpfs":
        return None
    size, used = reading.get("tmp_size_bytes"), reading.get("tmp_used_bytes")
    if not size or used is None:
        return None
    return round(float(used) / float(size), 4)


# --------------------------------------------------------------------------
# L159d: shared host measure (study L159 §1.4)
# --------------------------------------------------------------------------
#
# The same definitions on a VM and on a lent device: pressure stall
# information (PSI) of the host, CPU and memory used by the guest (the
# agents' slice) and by the application (everything else), and the capacity
# the host could lend. The lendable capacity is computed on the
# APPLICATION's consumption, never the guest's: otherwise the guest would
# chase itself away.

#: PSI files of the host (`cpu`, `memory`, `io`), `/proc/stat` and the
#: root of the cgroup v2 hierarchy: read by the platform layer (L63);
#: overridden by the tests
PSI_ENV = "AMEESH_PSI_DIR"
PROC_STAT_ENV = "AMEESH_PROC_STAT"
CGROUP_ROOT_ENV = "AMEESH_CGROUP_ROOT"
#: explicit guest cgroups (paths separated by `:`); EMPTY: no guest
GUEST_CGROUP_ENV = "AMEESH_GUEST_CGROUP"
#: slices that hold the guest: the lent VM's (L159e, L159g) and the agents'
#: slice of the workstation and the mesh VM (L159c)
GUEST_SLICES = ("ameesh-guest.slice", "ameesh-agents.slice")

#: margin applied to what the application left free at its worst moment
LEND_MARGIN = 0.5
#: CPU history: 95th percentile of the application's CPU per 10 min slot
LEND_SLOT_S = 600.0
LEND_PERCENTILE = 0.95
#: history window of the lendable capacity (the readings' retention)
LEND_WINDOW_S = HISTORY_RETENTION_S
#: below one day of history, the lendable capacity stays prudent: at most a
#: quarter of the memory and one CPU (study §1.4, first start)
LEND_WARMUP_S = 86400.0
LEND_PRUDENT_MEM_FRACTION = 0.25
LEND_PRUDENT_CPU = 1.0
#: disk kept for the host itself
LEND_DISK_RESERVE = 2 * 1024 ** 3
#: keys of the offer (`policy.lend`, L159a) that cap the lendable capacity
LEND_OFFER_KEYS = ("cpu_max", "memory_max", "disk_max")

#: PSI columns of a reading: (column, resource, line)
PSI_FIELDS = (("psi_cpu_some", "cpu", "some"), ("psi_mem_some", "memory", "some"),
              ("psi_mem_full", "memory", "full"), ("psi_io_some", "io", "some"))


#: the platform layer's readers, under the names of this module
parse_pressure = platform.parse_pressure
cgroup_cpu_seconds = platform.cgroup_cpu_seconds
cgroup_memory_bytes = platform.cgroup_memory_bytes


#: PSI averages a reading can take
PSI_WINDOWS = ("avg10", "avg60", "avg300")


def psi(base: str | None = None, window: str = "avg60") -> dict:
    """PSI of the host, averages in percent over `window` (`avg60` by
    default, `avg10` for a guard that must react fast): `psi_cpu_some`,
    `psi_mem_some`, `psi_mem_full`, `psi_io_some`; None when unreadable
    (no PSI before Linux 4.20, nor outside Linux, nor with `psi=0`)."""
    if window not in PSI_WINDOWS:
        raise ValueError("PSI window: %r, expected one of %s" % (window, PSI_WINDOWS))
    base = base or os.environ.get(PSI_ENV) or None
    files: dict = {}
    out: dict = {}
    for column, resource, line in PSI_FIELDS:
        if resource not in files:
            files[resource] = platform.pressure(resource, base)
        out[column] = files[resource].get(line, {}).get(window)
    return out


def guest_cgroups(root: str | None = None) -> list[str]:
    """The guest's cgroups that exist on this host.

    `AMEESH_GUEST_CGROUP` lists them explicitly (empty: no guest).
    Otherwise the guest slices are looked up where systemd puts them: a
    slice nests under its dash prefixes (`ameesh-agents.slice` lives in
    `ameesh.slice/`), at the top of the hierarchy (system units) and in each
    user manager (`user@<uid>.service`, user units of the workstation)."""
    explicit = os.environ.get(GUEST_CGROUP_ENV)
    if explicit is not None:
        return [path for path in explicit.split(":") if path and os.path.isdir(path)]
    root = root or os.environ.get(CGROUP_ROOT_ENV) or platform.CGROUP_ROOT
    bases = [root]
    users = os.path.join(root, "user.slice")
    try:
        for user in sorted(os.listdir(users)):
            for manager in sorted(os.listdir(os.path.join(users, user))):
                if manager.startswith("user@") and manager.endswith(".service"):
                    bases.append(os.path.join(users, user, manager))
    except OSError:
        pass
    paths = [os.path.join(base, slice_path(name)) for base in bases for name in GUEST_SLICES]
    return [path for path in paths if os.path.isdir(path)]


def slice_path(name: str) -> str:
    """Path of a slice unit below its root, as systemd nests it:
    `a-b-c.slice` → `a.slice/a-b.slice/a-b-c.slice`."""
    parts = name[:-len(".slice")].split("-")
    return os.path.join(*("-".join(parts[:i]) + ".slice" for i in range(1, len(parts) + 1)))


def host_cpu_seconds(path: str | None = None) -> float | None:
    """Busy CPU time of the host since boot, in seconds (platform layer);
    `path` or `AMEESH_PROC_STAT`: a file in the format of /proc/stat."""
    return platform.cpu_busy_seconds(path or os.environ.get(PROC_STAT_ENV) or None)


#: shortest window that gives a CPU share
CPU_MIN_WINDOW_S = 1.0


class CpuMeter:
    """CPU used by the application and by the guest between two readings.

    CPU times are counters: a share needs two readings. The first reading
    of a process (or one after an unreadable counter) gives None."""

    def __init__(self) -> None:
        self._prev: tuple | None = None

    def read(self, *, stat: str | None = None, cgroups: list[str] | None = None,
             now: float | None = None) -> dict:
        now = time.monotonic() if now is None else float(now)
        cgroups = guest_cgroups() if cgroups is None else cgroups
        busy = host_cpu_seconds(stat)
        guest = None
        if cgroups:
            values = [cgroup_cpu_seconds(path) for path in cgroups]
            guest = None if any(v is None for v in values) else sum(values)
        out = {"app_cpu": None, "guest_cpu": None, "cpu_window_s": None}
        prev = self._prev
        if prev is not None and 0 <= now - prev[0] < CPU_MIN_WINDOW_S:
            # under a second, the clock ticks of /proc/stat (10 ms) are noise:
            # keep the previous reading as the start of the window
            return out
        self._prev = (now, busy, guest, tuple(cgroups))
        if prev is None or busy is None or prev[1] is None or prev[3] != tuple(cgroups):
            return out
        window = now - prev[0]
        if window <= 0 or busy < prev[1]:
            return out
        total = (busy - prev[1]) / window
        guest_cpu = None
        if guest is not None and prev[2] is not None and guest >= prev[2]:
            guest_cpu = (guest - prev[2]) / window
        if cgroups and guest_cpu is None:
            return out  # a guest exists but its share is unknown: no guess
        out["cpu_window_s"] = round(window, 3)
        out["guest_cpu"] = None if guest_cpu is None else round(guest_cpu, 4)
        out["app_cpu"] = round(max(0.0, total - (guest_cpu or 0.0)), 4)
        return out


#: the executor's meter (one reading per `AMEESH_RESOURCE_INTERVAL`)
_CPU_METER = CpuMeter()


def guest_memory(cgroups: list[str] | None = None) -> int | None:
    """Memory charged to the guest's cgroups: 0 without a guest, None when
    a guest cgroup cannot be read (the application's memory is then
    unknown, never taken as zero)."""
    cgroups = guest_cgroups() if cgroups is None else cgroups
    if not cgroups:
        return 0
    values = [cgroup_memory_bytes(path) for path in cgroups]
    return None if any(v is None for v in values) else sum(values)


def lendable(reading: dict, basis: dict | None = None) -> dict:
    """Capacity the host could lend (study L159 §1.4), before the offer.

    `basis` is the history of the host (`HostResources.lend_basis`):
    `min_free_mem_bytes` (lowest `mem_available + guest_mem`, M7),
    `app_cpu_p95` (95th percentile of the application's CPU per 10 min
    slot, A7) and `span_s` (time covered by valid application CPU
    measurements). The current reading counts too.

    * memory: `h × M7`;
    * CPU: `h × (cpu_count − A7)`;
    * disk: `disk_free − 2 GiB`;
    * under a day of history, at most 25 % of the memory and one CPU.

    Values are None when a measure is missing: no guess."""
    basis = basis or {}
    free_now = reading.get("mem_available_bytes")
    guest_mem = reading.get("guest_mem_bytes")
    # unknown guest memory (None): the application's free memory is unknown
    free_now = None if free_now is None or guest_mem is None else free_now + guest_mem
    lows = [v for v in (basis.get("min_free_mem_bytes"), free_now) if v is not None]
    memory = int(LEND_MARGIN * min(lows)) if lows else None
    cpus = reading.get("cpu_count")
    a7 = basis.get("app_cpu_p95")
    if a7 is None:
        a7 = reading.get("app_cpu")
    cpu = LEND_MARGIN * (cpus - a7) if cpus and a7 is not None else None
    warmup = float(basis.get("span_s") or 0.0) < LEND_WARMUP_S
    if warmup:
        total = reading.get("mem_total_bytes")
        if memory is not None and total:
            memory = min(memory, int(LEND_PRUDENT_MEM_FRACTION * total))
        if cpu is not None:
            cpu = min(cpu, LEND_PRUDENT_CPU)
    disk = reading.get("disk_free_bytes")
    return {
        "lendable_cpu": None if cpu is None else round(max(0.0, cpu), 2),
        "lendable_memory_bytes": None if memory is None else max(0, memory),
        "lendable_disk_bytes": None if disk is None else max(0, disk - LEND_DISK_RESERVE),
        "lendable_basis": "warmup" if warmup else "history",
    }


def lend_offer(policy) -> dict | None:
    """The offer of a Host policy (`policy.lend`, L159a) reduced to its caps:
    `cpu_max` (CPU), `memory_max` and `disk_max` (bytes); None when the host
    lends nothing."""
    raw = getattr(policy, "lend", None) if policy is not None else None
    if raw is None and isinstance(policy, dict):
        raw = policy.get("lend")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raw = {key: getattr(raw, key, None) for key in LEND_OFFER_KEYS}
    cpu = finite_float(raw.get("cpu_max"))
    return {"cpu_max": cpu if cpu is not None and cpu >= 0 else None,
            "memory_max": parse_bytes(raw.get("memory_max")),
            "disk_max": parse_bytes(raw.get("disk_max"))}


def host_offer(canons, host: str) -> dict | None:
    """The strictest offer of `host` across its Host fiches (0031): the
    lowest cap per key; None when no fiche lends."""
    offers = []
    for canon in canons or ():
        fiche = canon.host(host) if hasattr(canon, "host") else None
        offer = lend_offer(getattr(fiche, "policy", None)) if fiche is not None else None
        if offer is not None:
            offers.append(offer)
    if not offers:
        return None
    out = {}
    for key in LEND_OFFER_KEYS:
        values = [o[key] for o in offers if o.get(key) is not None]
        out[key] = min(values) if values else None
    return out


def offered(reading: dict | None, offer: dict | None) -> dict | None:
    """`lendable` of a reading capped by the offer: `{cpu, memory_bytes,
    disk_bytes}`; None when the reading has no lendable capacity. Without
    an offer (None) the host lends nothing: zeros. An offer without caps
    (`{}`) gives the measured capacity as is."""
    reading = reading or {}
    if all(reading.get(k) is None for k in
           ("lendable_cpu", "lendable_memory_bytes", "lendable_disk_bytes")):
        return None
    if offer is None:
        return {"cpu": 0.0, "memory_bytes": 0, "disk_bytes": 0}

    def cap(value, limit):
        if value is None:
            return None
        return value if limit is None else min(value, limit)

    return {"cpu": cap(reading.get("lendable_cpu"), offer.get("cpu_max")),
            "memory_bytes": cap(reading.get("lendable_memory_bytes"), offer.get("memory_max")),
            "disk_bytes": cap(reading.get("lendable_disk_bytes"), offer.get("disk_max"))}


#: keys of the `psi` object of the measure, and their reading columns
MEASURE_PSI_KEYS = (("cpu_some", "psi_cpu_some"), ("mem_some", "psi_mem_some"),
                    ("mem_full", "psi_mem_full"), ("io_some", "psi_io_some"))
#: reading fields copied as is into the measure (study §1.4)
MEASURE_FIELDS = ("cpu_count", "app_cpu", "guest_cpu", "mem_total_bytes",
                  "mem_available_bytes", "swap_used_bytes", "guest_mem_bytes",
                  "disk_free_bytes", "tmp_path", "tmp_fstype", "tmp_size_bytes",
                  "tmp_used_bytes", "on_ac", "battery_percent")


def measure(reading: dict | None, offer: dict | None = None) -> dict:
    """The common measure of a reading (study L159 §1.4), as the guard
    publishes it in `ameesh-host-state/1` (`measure`) and as `ameesh hosts
    --json` shows it.

    `ts` is the instant of the reading; `window_s` the window of the CPU
    averages (`app_cpu`, `guest_cpu`: the time since the previous reading);
    PSI values are always the kernel's 60 s averages. `psi` is None when the
    host has no PSI, `lendable` (capped by `offer`) when not computed. The
    fields L159d does not measure (`app_psi`, `app_disk_free_bytes`,
    `metered`, `user_idle_s`, `busy`) are present and None: no guess."""
    reading = reading or {}
    psi_values = {key: reading.get(column) for key, column in MEASURE_PSI_KEYS}
    out: dict = {"ts": reading.get("sampled_ts"), "window_s": reading.get("cpu_window_s")}
    out.update({key: reading.get(key) for key in MEASURE_FIELDS})
    out.update({
        "psi": psi_values if any(v is not None for v in psi_values.values()) else None,
        "app_psi": None,
        "app_disk_free_bytes": None,
        "metered": None,
        "user_idle_s": None,
        "busy": None,
        "lendable": offered(reading, offer),
    })
    return out


def sample(host: str, *, path: str | None = None, now: float | None = None,
           turns_in_progress: int | None = None, tmp: str | list | None = None,
           paths: list[str] | None = None) -> dict:
    """Un relevé de l'hôte : les mesures lisibles, les autres à None.

    `paths` (#253, `disk_paths`) : les dossiers dont on mesure les systèmes de
    fichiers, un relevé par système distinct (`volumes`) ; les colonnes à plat
    gardent le PIRE relevé de chaque plancher, `disk_path` le chemin du disque
    le moins libre. `path` seul : ce seul dossier. `tmp` : le /tmp mesuré, ou
    une liste (#306, `tmp_paths`) dont le tmpfs le plus plein est gardé. `now`
    permet aux tests de fixer l'instant.
    """
    vols = volumes(paths if paths is not None else [path] if path else [])
    out: dict = {
        "host": host,
        "sampled_ts": time.time() if now is None else float(now),
        "mem_available_bytes": None,
        "swap_used_bytes": None,
        "load1": load1(),
        "cpu_count": cpu_count(),
        "turns_in_progress": turns_in_progress,
        "volumes": vols,
    }
    for _floor, field in VOLUME_FLOORS:
        low = worst(vols, field)
        out[field] = low[field] if low is not None else None
    low = worst(vols, "disk_free_bytes")
    out["disk_path"] = low["path"] if low is not None else (vols[0]["path"] if vols else path)
    out.update(_meminfo())
    out.update(tmp_usage([system_tmp()] if tmp is None
                         else [tmp] if isinstance(tmp, str) else tmp))
    out.update(power())
    # L159d: PSI, application and guest shares
    out.update(psi())
    cgroups = guest_cgroups()
    out["guest_mem_bytes"] = guest_memory(cgroups)
    out.update(_CPU_METER.read(cgroups=cgroups))
    return out


def restart_record(record) -> dict | None:
    """L159e6: a `guard.restart` record ({ts, started, skipped}) checked and
    copied; None when ill-typed."""
    if not isinstance(record, dict) or isinstance(record.get("ts"), bool) \
            or not isinstance(record.get("ts"), (int, float)) \
            or finite_float(record["ts"]) is None:
        return None
    started, skipped = record.get("started"), record.get("skipped")
    if not isinstance(started, list) or not all(isinstance(u, str) for u in started) \
            or not isinstance(skipped, dict) \
            or not all(isinstance(r, str) for r in skipped.values()):
        return None
    return {"ts": float(record["ts"]), "started": list(started), "skipped": dict(skipped)}


def guard_restart(path: str | None = None, *, now: float | None = None) -> dict | None:
    """L159e6: the last restart the capacity guard of this host published
    (`guard.restart` of its state, `path`: `guard.DEFAULT_STATE_PATH`), as
    {ts, started, skipped, age_s}; None without a guard, an unreadable state
    or an ill-typed record. `age_s`: its age on this host's clock at `now`,
    so the reader never compares two clocks (codex #288 r1)."""
    from . import guard
    try:
        with open(path or guard.DEFAULT_STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    block = state.get("guard") if isinstance(state, dict) else None
    record = restart_record(block.get("restart") if isinstance(block, dict) else None)
    if record is None:
        return None
    now = time.time() if now is None else float(now)
    record["age_s"] = max(0.0, now - record["ts"])
    return record


def collect(cfg, db: Db, *, now: float | None = None, policy=None,
            gate: dict | None = None) -> dict:
    """Relève l'hôte de cet exécuteur et publie le relevé ; rend le relevé.

    Le nombre de tours en cours vient de la base (agents de l'hôte au bail
    vivant et en tour) : c'est un état, pas une mesure locale. `policy` : la
    politique de ménage de l'hôte, pour la racine du cache des dépendances
    (#253). L159f : `gate` est l'état de la porte d'hôte que l'exécuteur suit
    (`gate_state`, `gate_seq`, `gate_reason`), None sans porte ; le
    déplacement le lit.
    """
    host = cfg.host or ""
    try:
        turns = storage.of(db).hosts.turns_in_progress(host)
    except Exception:  # un relevé ne doit jamais faire tomber la boucle
        turns = None
    reading = sample(host, paths=disk_paths(cfg, policy), now=now, turns_in_progress=turns,
                     tmp=tmp_paths(cfg, policy))
    # L159d: lendable capacity, on the 7 days of readings of the host
    try:
        basis = storage.of(db).hosts.lend_basis(host, LEND_WINDOW_S, LEND_SLOT_S,
                                                LEND_PERCENTILE)
    except Exception:  # un relevé ne doit jamais faire tomber la boucle
        basis = None
    reading.update(lendable(reading, basis))
    reading["guard_restart"] = guard_restart(now=now)
    reading.update({key: (gate or {}).get(key) for key in GATE_KEYS})
    storage.of(db).hosts.record(reading)
    return reading


#: L159f: the host gate in a reading (`host_gate.GateController.snapshot`)
GATE_KEYS = ("gate_state", "gate_seq", "gate_reason")


# --------------------------------------------------------------------------
# seuils (canon) et pression
# --------------------------------------------------------------------------

def thresholds(policy=None) -> dict:
    """Seuils effectifs d'un hôte : `policy.resources` de sa fiche `Host`,
    complétés par les valeurs par défaut prudentes.

    `policy` peut être un `HostPolicy` (le canon) ou un mapping dont la clé
    `resources` porte les seuils. Chaque seuil absent ou illisible prend sa
    valeur par défaut : une politique incomplète n'ouvre pas la porte à une
    absence de garde.
    """
    raw = None
    if policy is not None:
        raw = getattr(policy, "resources", None)
        if raw is None and isinstance(policy, dict):
            raw = policy.get("resources")
    if not isinstance(raw, dict):
        raw = {}
    mem = parse_bytes(raw.get("min_mem_available"))
    swap = parse_bytes(raw.get("max_swap_used"))
    disk = parse_bytes(raw.get("min_disk_free"))
    load = finite_float(raw.get("max_load"))
    tmpfs = parse_fraction(raw.get("max_tmpfs_used"))
    low = parse_percent(raw.get("min_battery_percent"))
    stop = parse_percent(raw.get("stop_battery_percent"))
    cpu_pressure = parse_percent(raw.get("max_cpu_pressure"))
    unallocated = parse_bytes(raw.get("min_btrfs_unallocated"))
    headroom = parse_bytes(raw.get("min_btrfs_metadata_headroom"))
    cpus = cpu_count() or 1
    return {
        "min_mem_available": DEFAULT_MIN_MEM_AVAILABLE if mem is None else mem,
        "max_swap_used": DEFAULT_MAX_SWAP_USED if swap is None else swap,
        "max_load": (DEFAULT_MAX_LOAD_FACTOR * cpus) if load is None else load,
        "min_disk_free": DEFAULT_MIN_DISK_FREE if disk is None else disk,
        "max_tmpfs_used": DEFAULT_MAX_TMPFS_USED if tmpfs is None else tmpfs,
        "min_battery_percent": DEFAULT_MIN_BATTERY_PERCENT if low is None else low,
        "stop_battery_percent": DEFAULT_STOP_BATTERY_PERCENT if stop is None else stop,
        # L159d: no default, None = not watched
        "max_cpu_pressure": cpu_pressure,
        # L159d: the offer (`policy.lend`) caps the lendable capacity; None:
        # the host lends nothing
        "lend": lend_offer(policy),
        "min_btrfs_unallocated": (DEFAULT_MIN_BTRFS_UNALLOCATED if unallocated is None
                                  else unallocated),
        "min_btrfs_metadata_headroom": (DEFAULT_MIN_BTRFS_METADATA_HEADROOM
                                        if headroom is None else headroom),
    }


#: L43 (0031) : seuils PLANCHERS (plus strict = plus haut) ; les autres sont
#: des plafonds (plus strict = plus bas)
FLOOR_KEYS = ("min_mem_available", "min_disk_free", *POWER_KEYS, *BTRFS_KEYS)
#: provenance d'une limite qu'aucune fiche Host ne déclare
DEFAULT_ORIGIN = "défaut"


def declared(policy) -> dict:
    """Seuils DÉCLARÉS (et lisibles) d'une politique, sans valeur par défaut."""
    raw = getattr(policy, "resources", None) if policy is not None else None
    if raw is None and isinstance(policy, dict):
        raw = policy.get("resources")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in THRESHOLD_KEYS:
        value = (finite_float(raw.get(key)) if key == "max_load"
                 else parse_fraction(raw.get(key)) if key in FRACTION_KEYS
                 else parse_percent(raw.get(key)) if key in POWER_KEYS + PRESSURE_KEYS
                 else parse_bytes(raw.get(key)))
        if value is not None:
            out[key] = value
    return out


def agent_memory_cap(canons, host: str, agent: str) -> dict:
    """L159c : plafond mémoire de l'unité d'exécuteur de `agent` sur `host`.

    Le plus strict des plafonds déclarés par les fiches Host de l'hôte (L43) :
    `agent_memory_max_by_agent[agent]` s'il existe dans au moins une fiche,
    sinon `agent_memory_max`, sinon le défaut (6 Gio). Rend `max` et `high`
    (octets ; `high` = 90 % de `max`) et `origin` (canon, ou « défaut »)."""
    by_agent, by_host = [], []
    for canon in canons or ():
        fiche = canon.host(host) if hasattr(canon, "host") else None
        if fiche is None:
            continue
        raw = getattr(getattr(fiche, "policy", None), "resources", None)
        if not isinstance(raw, dict):
            continue
        label = getattr(canon, "id", "") or "?"
        per_agent = raw.get("agent_memory_max_by_agent")
        if isinstance(per_agent, dict) and parse_bytes(per_agent.get(agent)):
            by_agent.append((parse_bytes(per_agent.get(agent)), label))
        if parse_bytes(raw.get("agent_memory_max")):
            by_host.append((parse_bytes(raw.get("agent_memory_max")), label))
    chosen = min(by_agent or by_host or [(DEFAULT_AGENT_MEMORY_MAX, DEFAULT_ORIGIN)])
    return {"max": chosen[0], "high": int(chosen[0] * AGENT_MEMORY_HIGH_RATIO),
            "origin": chosen[1]}


def host_limits(canons, host: str) -> dict:
    """Limites PHYSIQUES de `host` (L43, décision 0031 point 6) : les plus
    strictes de toutes les fiches Host qui le décrivent dans les canons chargés.

    * seuils de ressources : pour chaque clé, la valeur DÉCLARÉE la plus
      stricte (plancher le plus haut, plafond le plus bas) ; une clé qu'aucune
      fiche ne déclare prend sa valeur par défaut prudente. Une fiche qui se
      tait sur une clé n'impose pas le défaut à celle qui la déclare (le
      défaut n'est qu'un repli) ;
    * `max_agents` : le plus petit des maxima déclarés (None : aucun).

    `origin` dit d'où vient chaque limite (identifiant du canon, ou
    « défaut ») ; `fiches` : les canons qui décrivent l'hôte. L'admission d'un
    agent, elle, se juge toujours avec la fiche de SON canon (`placement`)."""
    fiches = []
    for canon in canons or ():
        fiche = canon.host(host) if hasattr(canon, "host") else None
        if fiche is not None:
            fiches.append((getattr(canon, "id", "") or "?", fiche))
    limits = thresholds(None)
    origin = {key: DEFAULT_ORIGIN for key in THRESHOLD_KEYS}
    best: dict = {}
    for label, fiche in fiches:
        for key, value in declared(getattr(fiche, "policy", None)).items():
            current = best.get(key)
            if current is None or (value > current[0] if key in FLOOR_KEYS
                                   else value < current[0]):
                best[key] = (value, label)
    for key, (value, label) in best.items():
        limits[key] = value
        origin[key] = label
    max_agents, max_origin = None, None
    for label, fiche in fiches:
        value = getattr(getattr(fiche, "policy", None), "max_agents", None)
        if value is not None and (max_agents is None or value < max_agents):
            max_agents, max_origin = value, label
    origin["max_agents"] = max_origin or DEFAULT_ORIGIN
    # L159d: the strictest offer (relocation, `ameesh hosts`)
    limits["lend"] = host_offer(canons, host)
    return {"host": host, "limits": limits, "max_agents": max_agents, "origin": origin,
            "fiches": [label for label, _f in fiches]}


def breaches(reading: dict, limits: dict) -> list[dict]:
    """Les seuils franchis par un relevé, du plus grave au moins grave.

    Chaque franchissement : `key`, `label`, `value`, `limit`, `critical`.
    Une mesure absente (None) ne déclenche rien : on ne devine pas. Une
    mesure CRITIQUE est franchie au-delà du facteur de gravité (voir les
    constantes `CRITICAL_*`) : l'exécuteur met alors en pause les agents les
    moins prioritaires, jamais au milieu d'un tour.
    """
    out: list[dict] = []

    def floor(key, label, value, limit, critical_factor):
        if value is None or limit is None:
            return
        if value < limit:
            out.append({"key": key, "label": label, "value": value, "limit": limit,
                        "critical": value <= limit * critical_factor})

    # #253: the disk floors hold on every volume the host writes to; the worst
    # reading of each floor decides, and the breach names its path
    vols = reading.get("volumes") or []

    def volume_floor(key, label, field, limit, critical_factor):
        low = worst(vols, field)
        if low is None:
            path = reading.get("disk_path")  # a reading from before 0359
            value = reading.get(field)
        else:
            path, value = low["path"], low[field]
        before = len(out)
        floor(key, "%s %s" % (label, path) if path else label, value, limit,
              critical_factor)
        if len(out) > before:
            out[-1]["path"] = path

    def ceiling(key, label, value, limit, critical_factor):
        if value is None:
            return
        if value > limit:
            out.append({"key": key, "label": label, "value": value, "limit": limit,
                        "critical": value >= limit * critical_factor})

    floor("min_mem_available", "available memory", reading.get("mem_available_bytes"),
          limits["min_mem_available"], CRITICAL_MEM_FACTOR)
    ceiling("max_swap_used", "swap used", reading.get("swap_used_bytes"),
            limits["max_swap_used"], CRITICAL_SWAP_FACTOR)
    ceiling("max_load", "1-min load", reading.get("load1"),
            limits["max_load"], CRITICAL_LOAD_FACTOR)
    volume_floor("min_disk_free", "free disk", "disk_free_bytes",
                 limits["min_disk_free"], CRITICAL_DISK_FACTOR)
    # #246: btrfs allocation, which `disk_free` (statvfs) does not see; like
    # the other floors, a breach closes new turns well before the critical level
    volume_floor("min_btrfs_unallocated", "btrfs unallocated", "btrfs_unallocated_bytes",
                 limits.get("min_btrfs_unallocated"), CRITICAL_BTRFS_FACTOR)
    volume_floor("min_btrfs_metadata_headroom", "btrfs metadata headroom",
                 "btrfs_metadata_headroom_bytes",
                 limits.get("min_btrfs_metadata_headroom"), CRITICAL_BTRFS_FACTOR)
    # L73 : un /tmp en tmpfs vit en mémoire et en swap ; critique à mi-chemin
    # entre le seuil et le plein.
    part = tmpfs_fraction(reading)
    limit = limits.get("max_tmpfs_used")
    if part is not None and limit is not None and part > limit:
        out.append({"key": "max_tmpfs_used", "label": "tmpfs %s used"
                    % (reading.get("tmp_path") or "/tmp"), "value": part, "limit": limit,
                    "critical": part >= limit + (1.0 - limit) / 2.0})
    # L159d: CPU pressure of the host; critical halfway between the ceiling
    # and 100 %
    cpu_pressure, limit = reading.get("psi_cpu_some"), limits.get("max_cpu_pressure")
    if cpu_pressure is not None and limit is not None and cpu_pressure > limit:
        out.append({"key": "max_cpu_pressure", "label": "CPU pressure (PSI some 60 s)",
                    "value": cpu_pressure, "limit": limit,
                    "critical": cpu_pressure >= limit + (100.0 - limit) / 2.0})
    etat = power_state(reading, limits)
    if etat["state"] in ("low", "stop"):
        # L106 : jamais `critical` — la pause des agents peu prioritaires ne
        # sert à rien ici ; l'arrêt propre (`stop`) est l'affaire de l'exécuteur
        out.append({"key": "min_battery_percent", "label": "battery (on battery power)",
                    "value": etat["battery_percent"], "limit": etat["min"],
                    "critical": False, "power": etat["state"]})
    out.sort(key=lambda b: (not b["critical"], b["key"]))
    return out


def power_state(reading: dict | None, limits: dict | None = None) -> dict:
    """État d'alimentation (L106) : `ok`, `low` (sur batterie sous le seuil :
    plus de nouveau tour), `stop` (sous le seuil d'arrêt : arrêt propre), ou
    `unknown` (pas de mesure : aucune garde)."""
    reading = reading or {}
    limits = limits or thresholds(None)
    low = float(limits.get("min_battery_percent", DEFAULT_MIN_BATTERY_PERCENT))
    stop = float(limits.get("stop_battery_percent", DEFAULT_STOP_BATTERY_PERCENT))
    on_ac, percent = reading.get("on_ac"), reading.get("battery_percent")
    out = {"on_ac": on_ac, "battery_percent": percent, "min": low, "stop": stop}
    if on_ac is None and percent is None:
        out["state"] = "unknown"
    elif on_ac is False and percent is not None and float(percent) < low:
        out["state"] = "stop" if float(percent) <= stop else "low"
    else:
        out["state"] = "ok"
    return out


def pressure(reading: dict, policy=None, *, limits: dict | None = None) -> dict:
    """État de pression d'un hôte : seuils effectifs et franchissements.

    `critical` est vrai dès qu'un franchissement est critique ; `blocked` est
    vrai dès qu'un seuil est franchi (aucun NOUVEAU tour, les tours en cours
    finissent). `limits` permet de réutiliser des seuils déjà calculés (cache
    de l'exécuteur) ; sinon ils viennent de `policy`.
    """
    if limits is None:
        limits = thresholds(policy)
    found = breaches(reading, limits)
    return {
        "limits": limits,
        "breaches": found,
        "blocked": bool(found),
        "critical": any(b["critical"] for b in found),
        "power": power_state(reading, limits),
    }


def fmt_value(key: str, value) -> str:
    """Valeur lisible d'un seuil ou d'une mesure (octets, charge ou part)."""
    if value is None:
        return "—"
    if key == "max_load":
        return "%.2f" % float(value)
    if key in FRACTION_KEYS:
        return "%d %%" % round(float(value) * 100)
    if key in POWER_KEYS:
        return "%s %%" % value
    if key in PRESSURE_KEYS:
        return "%.1f %%" % float(value)
    number = int(value)
    for label, factor in (("TiB", 1024 ** 4), ("GiB", 1024 ** 3), ("MiB", 1024 ** 2),
                          ("KiB", 1024)):
        if abs(number) >= factor:
            return "%.1f %s" % (number / factor, label)
    return "%d B" % number


def describe(found: list[dict]) -> str:
    """Les franchissements en clair : « swap utilisé 17.5 GiB (seuil 16.0 GiB) »."""
    return "; ".join("%s %s (threshold %s)" % (b["label"], fmt_value(b["key"], b["value"]),
                                            fmt_value(b["key"], b["limit"]))
                      for b in found or [])


def history(db: Db, host: str, limit: int = 10) -> list[dict]:
    return storage.of(db).hosts.history(host, limit)


def current(db: Db, host: str | None = None) -> list[dict]:
    return storage.of(db).hosts.current(host)
