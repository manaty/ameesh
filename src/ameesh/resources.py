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
import sys
import time

from . import platform, storage
from .db import Db

#: valeurs par défaut prudentes des seuils (0028), quand la fiche Host n'en
#: déclare pas : 1 Gio de mémoire disponible, 2×CPU de charge, 2 Gio de disque
#: libre. Le plancher de mémoire est le signal de saturation. #2296 : pas de
#: plafond de swap par défaut — un stock de swap n'est pas une pression, et le
#: défaut de 0028 (8 Gio) faisait un faux « critical pressure » au démarrage,
#: avant que la fiche Host ne soit lue (pc-smichea, auditeur 18/10) ; seul un
#: hôte qui le déclare dans `policy.resources` a un plafond de swap.
DEFAULT_MIN_MEM_AVAILABLE = 1024 ** 3
DEFAULT_MAX_SWAP_USED = None
#: #2354 : le plafond de 0028 reste le repli d'un hôte qui ne peut pas mesurer
#: PSI et ne déclare pas `max_swap_used`. Sans pression réelle, le stock de
#: swap est le seul signal mémoire qui reste : la mémoire disponible peut
#: rester au-dessus de son plancher pendant que les pages partent en swap.
#: Il ne s'applique qu'à ce cas — un hôte qui mesure PSI n'a pas de plafond de
#: swap par défaut (#2296), et un plafond déclaré l'emporte toujours
#: (`swap_ceiling`).
DEFAULT_MAX_SWAP_USED_WITHOUT_PSI = 8 * 1024 ** 3
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
#: 25 % : un tour dure jusqu'à 30 min (`turn_max_seconds`, 1 800 s) et une
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
#:
#: #2296: ceiling on the host MEMORY pressure (PSI `memory some`, 60 s
#: average, in percent), and the admission signal that follows the real
#: pressure instead of the swap used. It DOES have a default, unlike
#: `max_cpu_pressure`: an absent or unreadable threshold must not open the
#: gate (`thresholds`). 10 % is already 6 s of memory stall per minute.
PRESSURE_KEYS = ("max_cpu_pressure", "max_mem_pressure")
DEFAULT_MAX_MEM_PRESSURE = 10.0
#: #2296: a swap-used ceiling only counts with a real memory pressure — PSI
#: memory above this share of its own ceiling (the capacity guard's
#: `WARN_SHARE`, study L159). A swap STOCK is not a pressure: pages stay in
#: swap until touched, so a ceiling on it latched after the pressure was gone
#: (pc-smichea, 17/10: 22.3 GiB of swap while 6.2-6.9 GiB were available and
#: memory PSI was 2.4-3.4 %). Without PSI (a host that cannot measure it), the
#: ceiling keeps its old meaning: no real-pressure signal, the gate does not
#: open on the stock alone.
SWAP_PRESSURE_SHARE = 0.5
#: #2022: early disk warning, well before `min_disk_free` pauses the agents
#: (16/10: agents-1 reached 99 %, 11 agents paused, nobody warned before).
#: A WARNING only: it alerts, it never closes a turn. Used share or free
#: space, whichever comes first, on every volume.
WARN_KEYS = ("warn_disk_used", "warn_disk_free")
DEFAULT_WARN_DISK_USED = 0.85
DEFAULT_WARN_DISK_FREE = 15 * 1024 ** 3
#: #2022: the trend — `min_disk_free` reached within this horizon, by a least
#: squares fit of the volume's free space over the last readings (critical
#: within the shorter one); the fit needs enough points over enough time
DISK_TREND_WINDOW_S = 6 * 3600.0
DISK_TREND_HORIZON_S = 24 * 3600.0
DISK_TREND_CRITICAL_S = 6 * 3600.0
DISK_TREND_MIN_POINTS = 6
DISK_TREND_MIN_SPAN_S = 3600.0
#: #2879: the fit reads ONE point per 5-minute bucket, not every reading.
#: pc-smichea already stores ~4 800 readings per host over the 6 h window
#: (~one every 4 s) and 3.3 MB of `volumes` jsonb: on a degraded 30 KiB/s
#: link that transfer took 53-63 s per host, while the same query runs in
#: ~0.1 s server-side. A bucket keeps the last reading of the tranche, so
#: 72 points cover the window — a few KB — and the least-squares fit sees
#: the same line (a bucket is well under `DISK_TREND_MIN_SPAN_S`).
DISK_TREND_STEP_S = 300.0
#: #2022: zombies of a host at or over this count raise `host_zombies` — a
#: warning, no pause (16/10, found by hand: 99 on ameesh-ci-3, 5 900 on the
#: workstation, 11 000 on agents-1, #1749)
ZOMBIE_KEYS = ("max_zombies",)
DEFAULT_MAX_ZOMBIES = 200
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
#: #2814: where the admission reads the pressure. `host` is the historical
#: behaviour (`/proc/pressure`, `/proc/loadavg`, host memory); `own_cgroup`
#: reads this process's cgroup v2 (PSI, `cpu.stat` throttling,
#: `memory.current` vs `memory.max`) and DROPS the node-wide load. In a
#: container the node load says nothing about the container's own capacity
#: (TPK, 19/10: node load 19.6, ameesh container 377m CPU); the default is
#: therefore `own_cgroup` when the process is in a container, `host` otherwise.
PRESSURE_SCOPES = ("own_cgroup", "host")
PRESSURE_SCOPE_KEY = "pressure_scope"
#: environment override of the scope, for one executor without a canon edit
PRESSURE_SCOPE_ENV = "AMEESH_PRESSURE_SCOPE"
#: #2814: share of `memory.max` above which a cgroup closes new turns
#: (critical halfway between the threshold and full, like the tmpfs ceiling)
DEFAULT_MAX_CGROUP_MEMORY_USED = 0.9
#: #2814: share of wall time during which the cgroup had throttled tasks
#: (`cpu.stat` `throttled_usec` delta / wall time). A CPU quota that stalls
#: tasks is real pressure even when the host load is low. Critical at
#: halfway to 1.0.
DEFAULT_MAX_CGROUP_THROTTLED = 0.5
CGROUP_KEYS = ("max_cgroup_memory_used", "max_cgroup_throttled")
#: seuils exprimés en PART (0 < x <= 1, ou texte « 80% ») et non en octets
FRACTION_KEYS = ("max_tmpfs_used", "warn_disk_used", *CGROUP_KEYS)
#: clés de seuil reconnues dans `policy.resources` (liste fermée)
THRESHOLD_KEYS = ("min_mem_available", "max_swap_used", "max_load", "min_disk_free",
                  "max_tmpfs_used", *POWER_KEYS, *PRESSURE_KEYS,
                  *BTRFS_KEYS, *WARN_KEYS, *ZOMBIE_KEYS, *CGROUP_KEYS)
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
    `decimal_ok=False` exige un nombre entier d'octets : « 4.5GiB » (exact)
    passe, « 0.001KiB » (1.024 octet) est refusé au lieu d'être tronqué à 1
    (codex #459 r2), et chaque chiffre compte, même au-delà des 28 chiffres
    du contexte décimal (codex #459 r3)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        # 1 followed by 400 zeros: no float, so no size (codex #212 r4)
        return value if value >= 0 and finite_float(value) is not None else None
    if isinstance(value, float):
        # .inf, .nan (YAML): no size, never an unbounded one
        if not math.isfinite(value) or value < 0:
            return None
        if not decimal_ok and not value.is_integer():
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
    size = float(number) * factor
    if not math.isfinite(size):
        return None
    if decimal_ok:
        return int(size)
    # arithmétique entière exacte : le texte doit désigner un nombre entier
    # d'octets, et aucun chiffre ne doit être perdu. Une multiplication
    # `Decimal` garde le contexte par défaut (28 chiffres significatifs) et
    # arrondit : « 1.0000000000000000000000000001GiB » passait pour 1 GiB
    # (codex #459 r3). On multiplie donc le nombre entier de chiffres par le
    # facteur, puis on exige que l'échelle décimale divise exactement.
    whole, _, fraction = number.partition(".")
    try:
        exact = int(whole + fraction) * factor
    except ValueError:
        # plus de chiffres que la limite de conversion des entiers de Python
        # (4300 par défaut) : aucune taille, jamais une exception (codex #459
        # r4 : `1.` suivi de 4300 zéros levait un ValueError non rattrapé)
        return None
    scale = 10 ** len(fraction)
    if exact % scale:
        return None
    return exact // scale


def bytes_digit_limit() -> int:
    """The digit count beyond which Python refuses to convert a text to an
    integer (`sys.get_int_max_str_digits()`, 4300 by default); 0 when the
    running interpreter has no such limit (before 3.11).

    A size written with more digits cannot be checked by the exact reader of
    `parse_bytes(decimal_ok=False)`: the caller refuses it rather than reading
    it through the float path (codex #459 r4)."""
    return getattr(sys, "get_int_max_str_digits", lambda: 0)()


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


def parse_count(value) -> int | None:
    """#2022: a count (`max_zombies`): a positive integer, None otherwise.
    ASCII digits only (`"²".isdigit()` is true, `int("²")` raises), and, like
    every number of a host policy, one that fits a float (codex #212 r4)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit()):
            return None
        try:
            value = int(text)
        except ValueError:
            # plus de chiffres que la limite de conversion des entiers de
            # Python (4300 par défaut) : aucun compte, jamais une exception
            # hors du lecteur de politique (#2120, codex #459 r4)
            return None
    if not isinstance(value, int) or value <= 0:
        return None
    return value if finite_float(value) is not None else None


def parse_percent(value) -> float | None:
    """Un pourcentage de 0 à 100 (« 25 », 25, « 25% »), ou None. Le signe
    `%` est unique et à la fin (codex #459 r1 : « 20%% » n'est pas 20)."""
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        if "%" in text:
            return None
        number = finite_float(text)
    else:
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


def disk_total(path: str | None) -> int | None:
    """#2022: size of the filesystem holding `path` (the used share of the
    early warning), None when unreadable."""
    if not path:
        return None
    try:
        return int(shutil.disk_usage(path).total)
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
    reached at all (#306); `fstype` from the mount table (L64: a btrfs whose
    allocation is not read is told apart from a filesystem that has none),
    None without one; `disk_total_bytes` (#2022: the used share of the early
    warning)."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    mounts = platform.mountinfo() if platform.is_linux() else []
    for path in paths or ():
        if not path:
            continue
        path = os.path.abspath(os.path.expanduser(path))
        key = volume_key(path)
        if key in seen:
            if path not in seen[key]["paths"]:
                seen[key]["paths"].append(path)
            continue
        mount = platform.covering_mount(os.path.realpath(path), mounts) if mounts else None
        volume = {"path": path, "paths": [path],
                  "fstype": mount["fstype"] if mount is not None else None}
        if _anchor(path) is None:  # #306: out of reach, read as unknown
            volume.update(dict.fromkeys(f for _floor, f in VOLUME_FLOORS), unreadable=True)
        else:
            probe = _probe(path)
            volume["disk_free_bytes"] = disk_free(probe)
            volume["disk_total_bytes"] = disk_total(probe)
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
#: #4433: prefixes of the per-agent runner units (hostcheck.RUNNER_TEMPLATE
#: and SYSTEM_RUNNER_TEMPLATE) whose cgroups live in `ameesh-agents.slice`.
#: The instance is the agent name; `ameesh-runner-agent@` (user service) and
#: `ameesh-runner@` (system service) are both measured.
RUNNER_UNIT_PREFIXES = ("ameesh-runner-agent@", "ameesh-runner@")
RUNNER_UNIT_SUFFIX = ".service"

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


def host_cgroup_bases(root: str | None = None) -> list[str]:
    """The cgroup v2 directories where systemd nests the host's slices: the
    hierarchy root (system units) and each user manager (`user@<uid>.service`,
    user units of a workstation). A cgroup directory also holds interface
    files (`cgroup.controllers`, …): they are skipped, never opened as a
    directory. A missing or unreadable tree gives only the root, never an
    error: a host without cgroup v2 measures nothing."""
    root = root or os.environ.get(CGROUP_ROOT_ENV) or platform.CGROUP_ROOT
    bases = [root]
    users = os.path.join(root, "user.slice")
    try:
        entries = sorted(os.listdir(users))
    except OSError:
        entries = []
    for user in entries:
        user_dir = os.path.join(users, user)
        if not os.path.isdir(user_dir):
            continue
        try:
            managers = sorted(os.listdir(user_dir))
        except OSError:
            continue
        for manager in managers:
            manager_dir = os.path.join(user_dir, manager)
            if manager.startswith("user@") and manager.endswith(".service") \
                    and os.path.isdir(manager_dir):
                bases.append(manager_dir)
    return bases


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
    paths = [os.path.join(base, slice_path(name))
             for base in host_cgroup_bases(root) for name in GUEST_SLICES]
    return [path for path in paths if os.path.isdir(path)]


def runner_cgroups(root: str | None = None) -> list[dict]:
    """#4433: the cgroups of this host's per-agent runners, measured.

    Every runner is one unit in `ameesh-agents.slice`
    (`ameesh-runner-agent@<agent>.service` on a workstation or a mesh VM,
    `ameesh-runner@<agent>.service` on an L55 server), under the system
    manager or each user manager. Each entry is
    `{agent, unit, path, mem_current_bytes, mem_max_bytes, mem_used, mem_some}`:
    the memory charged to the cgroup against its `memory.max` (share, None
    when unreadable or unlimited) and its own `memory.pressure` (`some`,
    60 s average). A measure the cgroup does not give stays None; nothing is
    guessed. Sorted by path so a reading is stable."""
    out: dict[str, dict] = {}
    for base in host_cgroup_bases(root):
        agents_slice = os.path.join(base, slice_path("ameesh-agents.slice"))
        try:
            units = sorted(os.listdir(agents_slice))
        except OSError:
            continue
        for unit in units:
            agent = None
            for prefix in RUNNER_UNIT_PREFIXES:
                if unit.startswith(prefix) and unit.endswith(RUNNER_UNIT_SUFFIX):
                    agent = unit[len(prefix):-len(RUNNER_UNIT_SUFFIX)]
                    break
            if not agent:
                continue
            path = os.path.join(agents_slice, unit)
            if path in out:
                continue
            current = platform.cgroup_memory_bytes(path)
            maximum = platform.cgroup_memory_max(path)
            pressure = platform.cgroup_pressure("memory", path)
            out[path] = {
                "agent": agent,
                "unit": unit,
                "path": path,
                "mem_current_bytes": current,
                "mem_max_bytes": maximum,
                "mem_used": (round(min(1.0, current / maximum), 4)
                             if current is not None and maximum else None),
                "mem_some": pressure.get("some", {}).get("avg60"),
            }
    return [out[path] for path in sorted(out)]


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


#: shortest window that gives a throttle share (`cpu.stat` counters are µs)
CGROUP_MIN_WINDOW_S = 1.0


class CgroupMeter:
    """#2814 : mesures du cgroup v2 de CE processus, pour l'admission.

    Lit `cpu.pressure`/`memory.pressure` (PSI du cgroup, pas du nœud), le
    quota (`cpu.max`), les compteurs de `cpu.stat` et `memory.current` /
    `memory.max`. Les compteurs sont cumulés : la part de temps throttlé et
    le CPU consommé demandent deux lectures, None à la première (aucune
    supposition). `path` (ou `AMEESH_CGROUP_DIR`) : un arbre de test ; sans
    lui, le cgroup du processus (`platform.host.cgroup_dir`)."""

    def __init__(self, path: str | None = None, clock=None) -> None:
        self._path = path
        self._clock = clock or time.monotonic
        self._prev: tuple | None = None
        self._prev_path: str | None = None

    def read(self, path: str | None = None, now: float | None = None) -> dict:
        base = path or self._path or os.environ.get("AMEESH_CGROUP_DIR") \
            or platform.host.cgroup_dir()
        out = {
            "path": base,
            # #2825: what the publisher itself observed, so another host can
            # read this host's default scope without asking its own process
            "container": platform.host.in_container(), "bounded": False,
            # #4150: max and unreadable must not both prove idle.
            "limits_readable": False,
            "cpu_some": None, "mem_some": None, "mem_full": None,
            "cpu_quota": None,
            "mem_current_bytes": None, "mem_max_bytes": None,
            "mem_used": None, "throttled": None, "cpu_used": None,
        }
        # VM reservation comes from machine CPU/MemTotal. Its runner may
        # have no delegated cpu controller, or no cgroup at all (macOS).
        out["limits_readable"] = not out["container"]
        if not base or not os.path.isdir(base):
            self._prev, self._prev_path = None, None
            return out
        out["bounded"] = platform.host.own_cgroup_bounded(base)
        cpu = platform.cgroup_pressure("cpu", base)
        mem = platform.cgroup_pressure("memory", base)
        out["cpu_some"] = cpu.get("some", {}).get("avg60")
        out["mem_some"] = mem.get("some", {}).get("avg60")
        out["mem_full"] = mem.get("full", {}).get("avg60")
        quota, period = platform.cgroup_cpu_max(base)
        if quota is not None and period:
            out["cpu_quota"] = round(quota / period, 4)
        stat = platform.cgroup_cpu_stat(base)
        current = platform.cgroup_memory_bytes(base)
        ceiling = platform.cgroup_memory_max(base)
        try:
            with open(os.path.join(base, "memory.max"), encoding="ascii") as stream:
                raw_ceiling = stream.read(128).strip()
            valid_ceiling = (raw_ceiling == "max" or
                             (raw_ceiling.isdigit() and int(raw_ceiling) >= 0))
            with open(os.path.join(base, "cpu.max"), encoding="ascii") as stream:
                raw_cpu = stream.read(128).split()
            valid_cpu = (len(raw_cpu) == 2 and
                         (raw_cpu[0] == "max" or
                          (raw_cpu[0].isdigit() and int(raw_cpu[0]) > 0)))
        except (OSError, UnicodeError):
            valid_ceiling = valid_cpu = False
        out["limits_readable"] = (not out["container"] or
                                  (period is not None and valid_cpu and valid_ceiling))
        out["mem_current_bytes"] = current
        out["mem_max_bytes"] = ceiling
        if current is not None and ceiling:
            out["mem_used"] = round(min(1.0, float(current) / float(ceiling)), 4)
        now = self._clock() if now is None else float(now)
        prev, prev_path = self._prev, self._prev_path
        self._prev, self._prev_path = (now, dict(stat)), base
        if prev is None or prev_path != base:
            return out
        if now <= prev[0]:
            # a clock that went back: keep the earlier window's start
            self._prev, self._prev_path = prev, prev_path
            return out
        window = now - prev[0]
        if window < CGROUP_MIN_WINDOW_S:
            # keep the previous instant: below a second the counters are noise
            self._prev, self._prev_path = prev, prev_path
            return out
        before = prev[1]
        if stat.get("throttled_usec") is not None \
                and before.get("throttled_usec") is not None:
            delta = max(0, stat["throttled_usec"] - before["throttled_usec"])
            out["throttled"] = round(min(1.0, delta / (window * 1e6)), 4)
        if stat.get("usage_usec") is not None and before.get("usage_usec") is not None:
            delta = max(0, stat["usage_usec"] - before["usage_usec"])
            out["cpu_used"] = round(delta / (window * 1e6), 4)
        return out


#: #2814: the reading's meter (a throttle share needs two samples)
_CGROUP_METER = CgroupMeter()


def guest_memory(cgroups: list[str] | None = None) -> int | None:
    """Memory charged to the guest's cgroups: 0 without a guest, None when
    a guest cgroup cannot be read (the application's memory is then
    unknown, never taken as zero)."""
    cgroups = guest_cgroups() if cgroups is None else cgroups
    if not cgroups:
        return 0
    values = [cgroup_memory_bytes(path) for path in cgroups]
    return None if any(v is None for v in values) else sum(values)


#: #2022 (0455): the process counts of a reading
PROCESS_FIELDS = ("procs_total", "procs_zombie", "runner_procs", "runner_zombies")


def processes(root: int | None = None, table: list[dict] | None = None) -> dict:
    """#2022: `procs_total` and `procs_zombie` of the host, `runner_procs` and
    `runner_zombies` among the descendants of `root` (default: this
    process, the runner); all None when the process table cannot be read —
    an empty table would read as « nothing runs »."""
    if table is None:
        try:
            table = platform.process_table()
        except Exception:  # NotAvailable, or any read error: unmeasured
            return dict.fromkeys(PROCESS_FIELDS)
    root = os.getpid() if root is None else int(root)
    kids: dict[int, list[dict]] = {}
    for proc in table:
        kids.setdefault(proc.get("ppid"), []).append(proc)
    own, stack, seen = [], [root], {root}
    while stack:
        for child in kids.get(stack.pop(), ()):
            if child["pid"] not in seen:
                seen.add(child["pid"])
                own.append(child)
                stack.append(child["pid"])
    return {"procs_total": len(table),
            "procs_zombie": sum(1 for p in table if p.get("zombie")),
            "runner_procs": len(own),
            "runner_zombies": sum(1 for p in own if p.get("zombie"))}


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


def footprint(reading: dict | None) -> dict:
    """#4147: reserved/used capacity, derived only from this host's sample.

    Container limits come from the executor's published cgroup, never from
    the viewer's machine. An unlimited container resource falls back to the
    physical machine independently of the other resource. A VM reserves the
    whole machine even when its runner lives in a bounded systemd slice.
    CPU usage is in cores (not a percentage); absent counters stay unknown.
    The sample timestamp is retained: this projection does not prove idleness.
    """
    reading = reading or {}
    cg = reading.get("cgroup") or {}
    container = cg.get("container") is True

    def nonnegative(value):
        value = finite_float(value)
        return value if value is not None and value >= 0 else None

    cpu = nonnegative(reading.get("cpu_count"))
    memory = parse_bytes(reading.get("mem_total_bytes"))
    quota = nonnegative(cg.get("cpu_quota")) if container else None
    ceiling = parse_bytes(cg.get("mem_max_bytes")) if container else None
    if container:
        used_cpu = nonnegative(cg.get("cpu_used"))
        used_memory = parse_bytes(cg.get("mem_current_bytes"))
    else:
        used_cpu = nonnegative(reading.get("app_cpu"))
        guest_cpu = nonnegative(reading.get("guest_cpu"))
        if used_cpu is not None:
            used_cpu += guest_cpu or 0.0
        available = parse_bytes(reading.get("mem_available_bytes"))
        used_memory = (max(0, memory - available)
                       if memory is not None and available is not None else None)
    return {
        "reserved_cpu": quota if quota is not None else cpu,
        "reserved_mem_bytes": ceiling if ceiling is not None else memory,
        "used_cpu": used_cpu,
        "used_mem_bytes": used_memory,
        "reserved_cpu_source": "cgroup" if quota is not None else "machine",
        "reserved_mem_source": "cgroup" if ceiling is not None else "machine",
    }


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
    out.update(footprint(reading))
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
    # #2814 : la mesure du cgroup de CE processus, publiée avec le relevé ;
    # elle sert l'admission (`own_cgroup`) et `ameesh hosts` sur un conteneur
    cgroup = _CGROUP_METER.read()
    # #4433 : les cgroups des autres exécuteurs de l'hôte, pour imputer une
    # PSI mémoire d'hôte au runner qui récupère contre son propre memory.max
    # (voir `runner_pressure_attribution`).
    cgroup["runners"] = runner_cgroups()
    out["cgroup"] = cgroup
    return out


#: L64: the measures a reading carries, and their names for a human. One
#: missing from a reading (None) is a measure this OS, or this host, cannot
#: give (PSI before Linux 4.20 or outside Linux, load on Windows, an
#: unreadable guest cgroup…): `unmeasured` says it instead of publishing
#: nothing without a word. A share that needs two readings (`app_cpu`) is
#: not listed: its first reading is always None.
MEASURES = (
    ("mem_available_bytes", "available memory"),
    ("swap_used_bytes", "swap"),
    ("load1", "load"),
    ("cpu_count", "CPU count"),
    ("disk_free_bytes", "free disk"),
    ("tmp_fstype", "type of /tmp (tmpfs ceiling)"),
    ("psi_cpu_some", "CPU pressure (PSI)"),
    ("psi_mem_some", "memory pressure (PSI)"),
    ("psi_io_some", "I/O pressure (PSI)"),
    ("guest_mem_bytes", "guest memory (cgroup)"),
)


def unmeasured(reading: dict | None) -> list[str]:
    """The names of the `MEASURES` that `reading` does not carry (None), in
    table order, then each btrfs volume whose allocation is not read (#246:
    the floors that `disk_free` cannot replace); [] without a reading."""
    if not reading:
        return []
    out = [label for key, label in MEASURES if reading.get(key) is None]
    for volume in reading.get("volumes") or ():
        if (isinstance(volume, dict) and volume.get("fstype") == "btrfs"
                and any(volume.get(f) is None for f in ("btrfs_unallocated_bytes",
                                                        "btrfs_metadata_headroom_bytes"))):
            out.append("btrfs allocation (%s)" % volume.get("path"))
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
    # #2022: zombies and processes, of the host and of this runner
    reading.update(processes())
    reading["runner_id"] = getattr(cfg, "runner_id", None) or getattr(cfg, "runner", None) or None
    storage.of(db).hosts.record(reading)
    return reading


#: L159f: the host gate in a reading (`host_gate.GateController.snapshot`)
GATE_KEYS = ("gate_state", "gate_seq", "gate_reason")


# --------------------------------------------------------------------------
# seuils (canon) et pression
# --------------------------------------------------------------------------

def pressure_scope(policy=None, *, local: bool = True,
                   default_scope: str | None = None) -> str:
    """#2814 : où l'admission lit la pression — `own_cgroup` ou `host`.

    Ordre : la fiche `Host` (`policy.resources.pressure_scope`), puis pour un
    exécuteur LOCAL `AMEESH_PRESSURE_SCOPE` (dérogation d'un exécuteur, sans
    édition de canon), puis `default_scope` (la lecture de l'hôte nommé), puis
    le défaut : `own_cgroup` dès que le processus tourne dans un conteneur
    dont le cgroup v2 est lisible et borné (`platform.host.own_cgroup_bounded`,
    #2825 N2), `host` sinon. Un service systemd borné n'est PAS un conteneur :
    pour le mesurer sur son propre cgroup, la fiche le déclare (ou
    `AMEESH_PRESSURE_SCOPE=own_cgroup`).

    `local=False` n'interroge ni l'environnement de l'exécuteur ni son propre
    conteneur : les limites d'un hôte NOMMÉ ne dépendent jamais du processus
    qui les lit (#2825 N4)."""
    raw = None
    if policy is not None:
        raw = getattr(policy, "resources", None)
        if raw is None and isinstance(policy, dict):
            raw = policy.get("resources")
    if isinstance(raw, dict):
        declared = raw.get(PRESSURE_SCOPE_KEY)
        if isinstance(declared, str) and declared in PRESSURE_SCOPES:
            return declared
    if local:
        override = os.environ.get(PRESSURE_SCOPE_ENV)
        if override in PRESSURE_SCOPES:
            return override
    if default_scope in PRESSURE_SCOPES:
        return default_scope
    if local and platform.host.in_container() \
            and platform.host.own_cgroup_bounded():
        return "own_cgroup"
    return "host"


def reading_pressure_scope(reading: dict | None) -> str:
    """#2825 N4 : le périmètre par défaut que la lecture de CET hôte justifie.

    Le relevé porte (`cgroup.container`, `cgroup.bounded`) ce que l'exécuteur
    de l'hôte a constaté sur lui-même : `own_cgroup` n'est le défaut que si
    l'hôte nommé est dans un conteneur ET que son cgroup v2 est borné. Une
    lecture absente ou ancienne (sans ces clés) garde `host` : on ne déduit
    pas le périmètre d'un autre hôte que celui qui a mesuré."""
    cgroup = (reading or {}).get("cgroup")
    if not isinstance(cgroup, dict):
        return "host"
    if cgroup.get("container") is True and cgroup.get("bounded") is True:
        return "own_cgroup"
    return "host"


#: mesures d'un cgroup v2 qui rendent `own_cgroup` utile (#2825 N2)
CGROUP_MEASURES = ("cpu_some", "mem_some", "mem_full", "cpu_quota",
                   "mem_current_bytes", "mem_max_bytes", "mem_used",
                   "throttled", "cpu_used")


def cgroup_measured(reading: dict | None) -> bool:
    """#2825 N2 : le relevé porte-t-il au moins une mesure de cgroup v2 ?

    Sans aucune mesure, le périmètre `own_cgroup` n'a plus de garde CPU/mémoire
    (la charge du nœud est écartée) : il faut alors revenir visiblement au
    périmètre `host` au lieu de perdre les gardes en silence."""
    reading = reading or {}
    stored = reading.get("cgroup")
    for key in CGROUP_MEASURES:
        if reading.get("cgroup_" + key) is not None:
            return True
        if isinstance(stored, dict) and stored.get(key) is not None:
            return True
    return False


def scope_label(scope: str | None) -> str:
    """Le nom lisible du périmètre de pression (« own cgroup », « host »)."""
    return "own cgroup" if scope == "own_cgroup" else "host"


def thresholds(policy=None, *, local: bool = True,
               default_scope: str | None = None) -> dict:
    """Seuils effectifs d'un hôte : `policy.resources` de sa fiche `Host`,
    complétés par les valeurs par défaut prudentes.

    `policy` peut être un `HostPolicy` (le canon) ou un mapping dont la clé
    `resources` porte les seuils. Chaque seuil absent ou illisible prend sa
    valeur par défaut : une politique incomplète n'ouvre pas la porte à une
    absence de garde. #2296 : c'est vrai aussi du plafond de pression mémoire
    (`max_mem_pressure`, 10 % par défaut) — un hôte qui ne le déclare pas
    garde le critère d'admission. Seule exception : `max_swap_used` n'a PAS
    de défaut (None) — un stock de swap n'est pas une pression ; seul un hôte
    qui déclare un plafond en a un, et ce plafond ne compte que corroboré par
    la pression mémoire (`swap_ceiling_applies`). #2354 : un hôte qui ne peut
    pas mesurer PSI garde le repli de 0028 (`swap_ceiling`), sans quoi il
    n'aurait plus aucune garde sur le stock.
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
    mem_pressure = parse_percent(raw.get("max_mem_pressure"))
    cg_mem_used = parse_fraction(raw.get("max_cgroup_memory_used"))
    cg_throttled = parse_fraction(raw.get("max_cgroup_throttled"))
    unallocated = parse_bytes(raw.get("min_btrfs_unallocated"))
    headroom = parse_bytes(raw.get("min_btrfs_metadata_headroom"))
    zombies = parse_count(raw.get("max_zombies"))
    warn_used = parse_fraction(raw.get("warn_disk_used"))
    warn_free = parse_bytes(raw.get("warn_disk_free"))
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
        # #2296: unlike the CPU pressure, never None — an absent or unreadable
        # threshold takes the prudent default, it never removes the guard
        "max_mem_pressure": (DEFAULT_MAX_MEM_PRESSURE if mem_pressure is None
                             else mem_pressure),
        # #2814: where the pressure is read; the cgroup ceilings only apply
        # in own_cgroup mode unless the policy overrides them
        "pressure_scope": pressure_scope(policy, local=local,
                                         default_scope=default_scope),
        "max_cgroup_memory_used": (DEFAULT_MAX_CGROUP_MEMORY_USED
                                   if cg_mem_used is None else cg_mem_used),
        "max_cgroup_throttled": (DEFAULT_MAX_CGROUP_THROTTLED
                                 if cg_throttled is None else cg_throttled),
        # L159d: the offer (`policy.lend`) caps the lendable capacity; None:
        # the host lends nothing
        "lend": lend_offer(policy),
        "min_btrfs_unallocated": (DEFAULT_MIN_BTRFS_UNALLOCATED if unallocated is None
                                  else unallocated),
        "min_btrfs_metadata_headroom": (DEFAULT_MIN_BTRFS_METADATA_HEADROOM
                                        if headroom is None else headroom),
        # #2022: early disk warning (never a pause)
        "warn_disk_used": DEFAULT_WARN_DISK_USED if warn_used is None else warn_used,
        "warn_disk_free": DEFAULT_WARN_DISK_FREE if warn_free is None else warn_free,
        # #2022: zombies of the host (a warning, never a pause)
        "max_zombies": DEFAULT_MAX_ZOMBIES if zombies is None else zombies,
    }


#: L43 (0031) : seuils PLANCHERS (plus strict = plus haut) ; les autres sont
#: des plafonds (plus strict = plus bas)
FLOOR_KEYS = ("min_mem_available", "min_disk_free", *POWER_KEYS, *BTRFS_KEYS,
              "warn_disk_free")
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
    scope = raw.get(PRESSURE_SCOPE_KEY)
    if isinstance(scope, str) and scope in PRESSURE_SCOPES:
        out[PRESSURE_SCOPE_KEY] = scope
    for key in THRESHOLD_KEYS:
        value = (finite_float(raw.get(key)) if key == "max_load"
                 else parse_count(raw.get(key)) if key in ZOMBIE_KEYS
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


def host_limits(canons, host: str, *, reading: dict | None = None,
                default_scope: str | None = None) -> dict:
    """Limites PHYSIQUES de `host` (L43, décision 0031 point 6) : les plus
    strictes de toutes les fiches Host qui le décrivent dans les canons chargés.

    * seuils de ressources : pour chaque clé, la valeur DÉCLARÉE la plus
      stricte (plancher le plus haut, plafond le plus bas) ; une clé qu'aucune
      fiche ne déclare prend sa valeur par défaut prudente. Une fiche qui se
      tait sur une clé n'impose pas le défaut à celle qui la déclare (le
      défaut n'est qu'un repli) ;
    * `max_agents` : le plus petit des maxima déclarés (None : aucun).

    #2825 N4 : sans `pressure_scope` déclaré, le défaut vient de la LECTURE de
    cet hôte (`reading`, `cgroup.container`/`cgroup.bounded`) ou de
    `default_scope` ; jamais du processus appelant (`in_container()` de
    `ameesh hosts` sur un autre hôte). Sans lecture ni défaut : `host`.

    `origin` dit d'où vient chaque limite (identifiant du canon, ou
    « défaut ») ; `fiches` : les canons qui décrivent l'hôte. L'admission d'un
    agent, elle, se juge toujours avec la fiche de SON canon (`placement`)."""
    fiches = []
    for canon in canons or ():
        fiche = canon.host(host) if hasattr(canon, "host") else None
        if fiche is not None:
            fiches.append((getattr(canon, "id", "") or "?", fiche))
    if default_scope is None and reading is not None:
        default_scope = reading_pressure_scope(reading)
    limits = thresholds({}, local=False, default_scope=default_scope)
    origin = {key: DEFAULT_ORIGIN for key in THRESHOLD_KEYS}
    origin[PRESSURE_SCOPE_KEY] = DEFAULT_ORIGIN
    best: dict = {}
    scopes: list[tuple[str, str]] = []
    for label, fiche in fiches:
        for key, value in declared(getattr(fiche, "policy", None)).items():
            if key == PRESSURE_SCOPE_KEY:
                scopes.append((value, label))
                continue
            current = best.get(key)
            if current is None or (value > current[0] if key in FLOOR_KEYS
                                   else value < current[0]):
                best[key] = (value, label)
    for key, (value, label) in best.items():
        limits[key] = value
        origin[key] = label
    # #2814 : le périmètre n'est pas un seuil ordinal. Une fiche qui déclare
    # `own_cgroup` le fait pour une raison (conteneur) : elle l'emporte sur
    # une fiche restée à `host` ; deux fiches d'accord gardent leur valeur.
    if scopes:
        value, label = next(((v, l) for v, l in scopes if v == "own_cgroup"), scopes[0])
        limits[PRESSURE_SCOPE_KEY] = value
        origin[PRESSURE_SCOPE_KEY] = label
    # #2825 N2 : un `own_cgroup` demandé sans AUCUNE mesure de cgroup dans la
    # lecture perdrait les gardes CPU/mémoire (la charge du nœud est écartée).
    # Repli visible au périmètre `host`, qui garde ses seuils.
    scope_fallback = None
    if limits[PRESSURE_SCOPE_KEY] == "own_cgroup" and reading is not None \
            and not cgroup_measured(reading):
        limits[PRESSURE_SCOPE_KEY] = "host"
        scope_fallback = ("own_cgroup requested but the reading has no cgroup v2 "
                          "measure: host scope keeps the guards")
    max_agents, max_origin = None, None
    for label, fiche in fiches:
        value = getattr(getattr(fiche, "policy", None), "max_agents", None)
        if value is not None and (max_agents is None or value < max_agents):
            max_agents, max_origin = value, label
    origin["max_agents"] = max_origin or DEFAULT_ORIGIN
    # #5207 : taire le silence d'un hôte demande l'accord de toutes ses fiches
    intermittent = bool(fiches) and all(
        getattr(getattr(fiche, "policy", None), "intermittent", False) is True
        for _label, fiche in fiches)
    # L159d: the strictest offer (relocation, `ameesh hosts`)
    limits["lend"] = host_offer(canons, host)
    return {"host": host, "limits": limits, "max_agents": max_agents,
            "intermittent": intermittent, "origin": origin,
            "fiches": [label for label, _f in fiches],
            "scope_fallback": scope_fallback}


def swap_ceiling(reading: dict, limits: dict) -> int | None:
    """#2296/#2354 : le plafond de swap utilisé qui s'applique à un relevé.

    Un plafond DÉCLARÉ (`max_swap_used`) l'emporte toujours. Sans plafond
    déclaré : None quand l'hôte mesure PSI — un stock n'est pas une pression
    (#2296), et le défaut de 0028 (8 Gio) faisait un faux « critical
    pressure » au démarrage — mais le repli prudent de 0028
    (`DEFAULT_MAX_SWAP_USED_WITHOUT_PSI`) quand l'hôte ne mesure pas PSI
    (#2354) : le stock y est le seul signal mémoire qui reste, la mémoire
    disponible pouvant rester au-dessus de son plancher pendant que les pages
    partent en swap. Le repli suit chaque relevé : un hôte qui retrouve PSI
    retrouve le critère de pression réelle.
    """
    declared = limits.get("max_swap_used")
    if declared is not None:
        return declared
    if reading.get("psi_mem_some") is None:
        return DEFAULT_MAX_SWAP_USED_WITHOUT_PSI
    return None


def swap_ceiling_applies(reading: dict, limits: dict) -> bool:
    """#2296 : le plafond de swap utilisé compte-t-il pour ce relevé ?

    Seulement pour un hôte qui a un plafond (`swap_ceiling` : déclaré, ou
    repli de 0028 quand l'hôte ne mesure pas PSI, #2354), et avec une pression
    mémoire mesurée au-dessus de `SWAP_PRESSURE_SHARE` de son propre plafond :
    le swap utilisé est un STOCK, pas une pression (les pages y restent tant
    qu'on ne les touche pas), donc un relevé où il dépasse le plafond après le
    retour au calme ne doit plus retenir les tours. Sans PSI (hôte qui ne sait
    pas le mesurer), le plafond garde son sens d'avant : pas de signal de
    pression réelle, la porte ne s'ouvre pas sur le seul stock. Sans plafond
    mémoire (appelant qui n'a pas calculé `thresholds`), même repli prudent.
    Sans aucun plafond (hôte avec PSI, aucune fiche n'en déclare), il n'y a
    rien à corroborer : le stock ne plafonne pas.
    """
    if swap_ceiling(reading, limits) is None:
        return False
    psi = reading.get("psi_mem_some")
    limit = limits.get("max_mem_pressure")
    if psi is None or limit is None:
        return True
    return psi > limit * SWAP_PRESSURE_SHARE


def runner_pressure_attribution(reading: dict, limits: dict) -> list[dict]:
    """#4433 : la pression mémoire de l'hôte expliquée par des cgroups de runner.

    Une PSI mémoire d'hôte agrège les stalls de tous ses cgroups : un runner
    qui récupère contre son propre `memory.max` (jest à six workers sans
    `--maxWorkers`, auditeur 29/10) fait monter la PSI de l'hôte alors que la
    mémoire de l'hôte reste disponible, et l'admission en périmètre `host`
    mettait tout l'hôte en pause. Un runner rend compte de la brèche quand :

    * `MemAvailable` reste au-dessus du plancher `min_mem_available` — l'hôte
      n'est pas à court de mémoire ;
    * son cgroup est à au moins `max_cgroup_memory_used` de son `memory.max` ;
    * sa propre PSI mémoire (`memory.pressure`, `some` 60 s) est au moins
      celle de l'hôte : la PSI d'un parent agrège ses enfants, donc un enfant
      qui vaut le parent rend compte de tous les stalls de l'hôte sur la
      fenêtre.

    Rend les cgroups attribués (liste vide sinon). Plusieurs runners peuvent
    être à leur plafond en même temps : ils sont tous rendus, chacun retenu
    pour ses propres tours. Aucune mesure (cgroup absent, `memory.max` infini
    ou illisible, PSI du cgroup absente) ne devine : la brèche reste celle de
    l'hôte.
    """
    available = reading.get("mem_available_bytes")
    floor = limits.get("min_mem_available")
    if available is None or floor is None or available < floor:
        return []
    psi = reading.get("psi_mem_some")
    limit = limits.get("max_mem_pressure")
    share = limits.get("max_cgroup_memory_used")
    if psi is None or limit is None or share is None or psi <= limit:
        return []
    stored = reading.get("cgroup")
    runners = stored.get("runners") if isinstance(stored, dict) else None
    out = []
    for entry in runners or ():
        if not isinstance(entry, dict):
            continue
        used = finite_float(entry.get("mem_used"))
        own = finite_float(entry.get("mem_some"))
        if used is None or own is None or used < share or own < psi:
            continue
        out.append(entry)
    return out


def breaches(reading: dict, limits: dict) -> list[dict]:
    """Les seuils franchis par un relevé, du plus grave au moins grave.

    Chaque franchissement : `key`, `label`, `value`, `limit`, `critical`.
    Une mesure absente (None) ne déclenche rien : on ne devine pas. Une
    mesure CRITIQUE est franchie au-delà du facteur de gravité (voir les
    constantes `CRITICAL_*`) : l'exécuteur met alors en pause les agents les
    moins prioritaires, jamais au milieu d'un tour.

    #2296 : le swap utilisé ne franchit son plafond que corroboré par une
    pression mémoire réelle (`swap_ceiling_applies`) ; la pression mémoire
    elle-même (PSI `memory some` 60 s) est le critère d'admission qui suit la
    pression réelle. #2354 : sans PSI et sans plafond déclaré, le repli de
    0028 s'applique (le stock est le seul signal) et le libellé le dit.

    #2814 : en périmètre `own_cgroup`, la charge 1 min (celle du NŒUD) ne
    compte pas et les pressions CPU/mémoire sont celles du cgroup ; les
    plafonds `max_cgroup_memory_used` et `max_cgroup_throttled` s'ajoutent.
    En périmètre `host`, les mesures de cgroup sont ignorées : le relevé de
    l'hôte reste la référence.

    #4433 : en périmètre `host`, une PSI mémoire franchie qui s'explique par
    des cgroups de runner à leur propre `memory.max` n'est pas un
    franchissement de l'HÔTE (`runner_pressure_attribution`) : ces
    franchissements sortent marqués `attributed` (et nomment leur runner), et
    `pressure` ne retient que ceux du runner qui interroge. La brèche de
    `min_mem_available`, elle, reste celle de l'hôte.
    """
    out: list[dict] = []
    scope = limits.get("pressure_scope", "host")
    # #2825 N2: an own-cgroup scope with no cgroup measure at all is blind;
    # use the host criteria rather than dropping load/PSI without a word. The
    # verdict (`pressure`) says the fallback; here the guards stay in force.
    own = scope == "own_cgroup" and cgroup_measured(reading)

    def cg(key):
        """The cgroup measure of the reading: the live flat key when the
        executor merged one (even a None measure, which says "unknown"), else
        the published `cgroup` mapping (#2814)."""
        flat = "cgroup_" + key
        if flat in reading:
            return reading[flat]
        stored = reading.get("cgroup")
        return stored.get(key) if isinstance(stored, dict) else None

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
        # #2296 : un plafond absent (None) ne plafonne rien — le stock de swap
        # n'a pas de défaut ; les autres plafonds en ont un.
        if value is None or limit is None:
            return
        if value > limit:
            out.append({"key": key, "label": label, "value": value, "limit": limit,
                        "critical": value >= limit * critical_factor})

    floor("min_mem_available", "available memory", reading.get("mem_available_bytes"),
          limits["min_mem_available"], CRITICAL_MEM_FACTOR)
    # #2296: swap used only counts with a measured memory pressure: a stock is
    # not a pressure (see `swap_ceiling_applies`). #2354: on a host that cannot
    # measure PSI and declares no ceiling, the 0028 conservative fallback is
    # the only memory signal left, and the label says so.
    if own:
        # #2814 : en périmètre `own_cgroup`, le swap est un stock de l'HÔTE ;
        # il ne compte que si une fiche le plafonne ET que le cgroup lui-même
        # rapporte une pression mémoire. Sans mesure du cgroup, le plafond de
        # `memory.max` est la vraie garde : on ne retient pas sur le stock.
        declared = limits.get("max_swap_used")
        psi, mem_limit = cg("mem_some"), limits.get("max_mem_pressure")
        if declared is not None and psi is not None and mem_limit is not None \
                and psi > mem_limit * SWAP_PRESSURE_SHARE:
            ceiling("max_swap_used", "swap used (own cgroup memory pressure)",
                    reading.get("swap_used_bytes"), declared, CRITICAL_SWAP_FACTOR)
    elif swap_ceiling_applies(reading, limits):
        fallback = limits.get("max_swap_used") is None
        ceiling("max_swap_used",
                "swap used (no PSI: conservative default)" if fallback else "swap used",
                reading.get("swap_used_bytes"), swap_ceiling(reading, limits),
                CRITICAL_SWAP_FACTOR)
    if not own:
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
    # #2296: memory pressure of the host (PSI some, 60 s): the admission
    # signal that follows the real pressure; critical halfway to 100 %, like
    # the CPU pressure. #2814: in own-cgroup mode this is the cgroup's own
    # memory PSI, never the node's.
    mem_pressure = cg("mem_some") if own else reading.get("psi_mem_some")
    limit = limits.get("max_mem_pressure")
    if mem_pressure is not None and limit is not None and mem_pressure > limit:
        critical = mem_pressure >= limit + (100.0 - limit) / 2.0
        attributed = [] if own else runner_pressure_attribution(reading, limits)
        if attributed:
            # #4433 : la PSI de l'hôte suit un cgroup de runner à son plafond ;
            # le franchissement nomme ce runner au lieu de retenir l'hôte.
            for entry in attributed:
                out.append({
                    "key": "max_mem_pressure",
                    "label": "memory pressure (PSI some 60 s) of runner %s at its "
                             "memory.max" % (entry.get("agent")
                                             or entry.get("unit") or "?"),
                    "value": mem_pressure, "limit": limit, "critical": critical,
                    "attributed": True,
                    "agent": entry.get("agent"), "unit": entry.get("unit"),
                    "path": entry.get("path"), "cgroup": entry.get("path"),
                    "mem_used": entry.get("mem_used"),
                    "mem_current_bytes": entry.get("mem_current_bytes"),
                    "mem_max_bytes": entry.get("mem_max_bytes"),
                    "mem_some": entry.get("mem_some")})
        else:
            out.append({"key": "max_mem_pressure",
                        "label": "memory pressure (PSI some 60 s%s)"
                        % (", own cgroup" if own else ""),
                        "value": mem_pressure, "limit": limit,
                        "critical": critical})
    # L159d: CPU pressure of the host; critical halfway between the ceiling
    # and 100 %. #2814: the cgroup's own CPU PSI in own-cgroup mode.
    cpu_pressure = cg("cpu_some") if own else reading.get("psi_cpu_some")
    limit = limits.get("max_cpu_pressure")
    if cpu_pressure is not None and limit is not None and cpu_pressure > limit:
        out.append({"key": "max_cpu_pressure",
                    "label": "CPU pressure (PSI some 60 s%s)"
                    % (", own cgroup" if own else ""),
                    "value": cpu_pressure, "limit": limit,
                    "critical": cpu_pressure >= limit + (100.0 - limit) / 2.0})
    # #2814: what the container itself is made of — memory charged against
    # `memory.max`, and wall time with throttled tasks (`cpu.stat`). Only in
    # own-cgroup mode: a host's own cgroup is not what the reading stores.
    if own:
        used, limit = cg("mem_used"), limits.get("max_cgroup_memory_used")
        if used is not None and limit is not None and used > limit:
            out.append({"key": "max_cgroup_memory_used",
                        "label": "cgroup memory used (memory.current/memory.max)",
                        "value": used, "limit": limit,
                        "critical": used >= limit + (1.0 - limit) / 2.0})
        throttled, limit = cg("throttled"), limits.get("max_cgroup_throttled")
        if throttled is not None and limit is not None and throttled > limit:
            out.append({"key": "max_cgroup_throttled",
                        "label": "cgroup CPU throttled (share of wall time)",
                        "value": throttled, "limit": limit,
                        "critical": throttled >= limit + (1.0 - limit) / 2.0})
    etat = power_state(reading, limits)
    if etat["state"] in ("low", "stop"):
        # L106 : jamais `critical` — la pause des agents peu prioritaires ne
        # sert à rien ici ; l'arrêt propre (`stop`) est l'affaire de l'exécuteur
        out.append({"key": "min_battery_percent", "label": "battery (on battery power)",
                    "value": etat["battery_percent"], "limit": etat["min"],
                    "critical": False, "power": etat["state"]})
    out.sort(key=lambda b: (not b["critical"], b["key"]))
    return out


def disk_floor_eta(points, floor: int) -> float | None:
    """#2022: seconds until the free space reaches `floor`, by a least squares
    fit of `points` ((ts, free bytes), any order) — None when the fit has too
    few points or too short a span (`DISK_TREND_MIN_*`), or when the space is
    not shrinking; 0 when already at or under the floor. Counted from the
    last point."""
    pts = sorted((float(ts), float(free)) for ts, free in points or ()
                 if ts is not None and free is not None)
    if len(pts) < DISK_TREND_MIN_POINTS or pts[-1][0] - pts[0][0] < DISK_TREND_MIN_SPAN_S:
        return None
    n = len(pts)
    mean_t = sum(t for t, _f in pts) / n
    mean_f = sum(f for _t, f in pts) / n
    var = sum((t - mean_t) ** 2 for t, _f in pts)
    if var <= 0:
        return None
    slope = sum((t - mean_t) * (f - mean_f) for t, f in pts) / var
    if slope >= 0:
        return None
    # from the fitted value at the last point: one noisy last reading does
    # not move the estimate
    fitted = mean_f + slope * (pts[-1][0] - mean_t)
    return max(0.0, (fitted - floor) / -slope)


def _volume_series(history, path: str) -> list[tuple[float, int]]:
    """(ts, free) of the volume `path` along `history` (readings); a reading
    from before 0359 (no `volumes`) counts when its `disk_path` is `path`."""
    out = []
    for past in history or ():
        ts = past.get("sampled_ts")
        vols = past.get("volumes") or []
        match = next((v for v in vols if isinstance(v, dict) and (
            v.get("path") == path or path in (v.get("paths") or ()))), None)
        if match is not None:
            out.append((ts, match.get("disk_free_bytes")))
        elif not vols and past.get("disk_path") == path:
            out.append((ts, past.get("disk_free_bytes")))
    return out


def disk_warnings(reading: dict, limits: dict, history=()) -> list[dict]:
    """#2022: the volumes of `reading` to warn about BEFORE `min_disk_free`
    pauses the agents, one entry per volume: `path`, `free`, `total`,
    `used` (share, None without a total), `reasons` (`used`: share at or
    over `warn_disk_used`; `free`: under `warn_disk_free`; `trend`: the floor
    reached within `DISK_TREND_HORIZON_S` by the fit of `history`, the
    readings of the last `DISK_TREND_WINDOW_S`), `floor_eta_s` (None without
    a trend) and `critical` (floor already crossed, or reached within
    `DISK_TREND_CRITICAL_S`). A warning, never a breach: `pressure` ignores
    it. A volume without a reading is skipped: nothing is guessed."""
    floor = limits.get("min_disk_free") or 0
    warn_used = limits.get("warn_disk_used")
    warn_free = limits.get("warn_disk_free")
    vols = reading.get("volumes") or []
    if not vols and reading.get("disk_free_bytes") is not None:
        vols = [{"path": reading.get("disk_path"),
                 "disk_free_bytes": reading.get("disk_free_bytes")}]
    out = []
    for vol in vols:
        free = vol.get("disk_free_bytes") if isinstance(vol, dict) else None
        if free is None:
            continue
        total = vol.get("disk_total_bytes")
        used = (1.0 - free / total) if total else None
        reasons = []
        if used is not None and warn_used is not None and used >= warn_used:
            reasons.append("used")
        if warn_free is not None and free < warn_free:
            reasons.append("free")
        series = _volume_series(history, vol.get("path"))
        if reading.get("sampled_ts") is not None:
            series.append((reading["sampled_ts"], free))
        eta = disk_floor_eta(series, floor)
        if eta is not None and eta <= DISK_TREND_HORIZON_S:
            reasons.append("trend")
        else:
            eta = None
        if not reasons:
            continue
        out.append({"path": vol.get("path"), "free": free, "total": total, "used": used,
                    "reasons": reasons, "floor_eta_s": eta,
                    "critical": free < floor or (eta is not None
                                                 and eta <= DISK_TREND_CRITICAL_S)})
    out.sort(key=lambda w: (not w["critical"], w["floor_eta_s"] is None,
                            w["floor_eta_s"] or 0.0, w["free"]))
    return out


#: #2022: a host whose last reading is older than this is `stale` in the
#: fleet summary (16/10: manaty-mesh-1 unmeasured for 3 days, ameesh-tests
#: deleted but still listed)
FLEET_STALE_S = 3600.0


def fleet_line(reading: dict, limits: dict, history=(), *, now: float) -> dict:
    """#2022: one host of the fleet summary (`ameesh hosts --fleet`): `host`,
    `age_s` (None without a reading), `stale`, `disk` (the worst volume: path, free, used share),
    `disk_warnings` (`disk_warnings`), `zombies`, `procs`, `breaches` (the
    keys of the pressure verdict) and `ok` (nothing to say)."""
    sampled = reading.get("sampled_ts")
    age = None if sampled is None else max(0.0, now - float(sampled))
    vols = [v for v in reading.get("volumes") or () if isinstance(v, dict)
            and v.get("disk_free_bytes") is not None]
    low = min(vols, key=lambda v: v["disk_free_bytes"]) if vols else None
    disk = None if low is None else {
        "path": low.get("path"), "free": low["disk_free_bytes"],
        "used": (1.0 - low["disk_free_bytes"] / low["disk_total_bytes"])
        if low.get("disk_total_bytes") else None}
    warnings = disk_warnings(reading, limits, history)
    breaches = [b["key"] for b in pressure(reading, limits=limits)["breaches"]]
    zombies = reading.get("procs_zombie")
    many = zombies is not None and limits.get("max_zombies") is not None \
        and zombies >= limits["max_zombies"]
    stale = age is None or age > FLEET_STALE_S
    return {"host": reading.get("host"), "age_s": None if age is None else round(age),
            "stale": stale,
            "disk": disk, "disk_warnings": warnings, "zombies": zombies,
            "procs": reading.get("procs_total"), "breaches": breaches,
            "ok": not (stale or warnings or breaches or many)}


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


def pressure(reading: dict, policy=None, *, limits: dict | None = None,
             own_path: str | None = None, own_agents=()) -> dict:
    """État de pression d'un hôte : seuils effectifs et franchissements.

    `critical` est vrai dès qu'un franchissement est critique ; `blocked` est
    vrai dès qu'un seuil est franchi (aucun NOUVEAU tour, les tours en cours
    finissent). `limits` permet de réutiliser des seuils déjà calculés (cache
    de l'exécuteur) ; sinon ils viennent de `policy`.

    #4433 : `own_path` (le cgroup de l'exécuteur qui interroge) et/ou
    `own_agents` (les agents qu'il sert) disent QUI demande. Un
    franchissement mémoire `attributed` à un runner ne bloque que ce runner :
    le verdict d'un autre exécuteur de l'hôte reste ouvert. Le chemin du
    cgroup fait foi quand il est connu (un exécuteur générique ne doit pas
    retenir ses autres agents) ; à défaut, le nom de l'agent attribué tranche.
    `attributed` porte toujours les franchissements imputés, pour l'alerte.
    """
    if limits is None:
        limits = thresholds(policy)
    scope = limits.get("pressure_scope", "host")
    scope_fallback = None
    if scope == "own_cgroup" and not cgroup_measured(reading):
        # #2825 N2: without a single cgroup measure, own_cgroup would drop the
        # node load and keep no CPU/memory guard. Fall back visibly to host.
        scope_fallback = ("own_cgroup requested but the reading has no cgroup v2 "
                          "measure: host scope keeps the guards")
        limits = dict(limits, pressure_scope="host")
        scope = "host"
    found = breaches(reading, limits)
    # #4433 : une brèche imputée à un runner n'est pas celle de l'hôte. Elle
    # ne retient que l'exécuteur qu'elle nomme ; `own_path` fait foi sur
    # `own_agents` quand il est lisible.
    attributed = [b for b in found if b.get("attributed")]
    host_found = [b for b in found if not b.get("attributed")]
    mine: list[dict] = []
    if attributed and (own_path or own_agents):
        agents = set(own_agents or ())
        for entry in attributed:
            if own_path:
                if entry.get("cgroup") == own_path:
                    mine.append(entry)
            elif entry.get("agent") in agents:
                mine.append(entry)
    kept = host_found + mine
    stored_cgroup = reading.get("cgroup") if isinstance(reading.get("cgroup"), dict) else {}

    def cgroup_value(key):
        flat = "cgroup_" + key
        if flat in reading:
            return reading[flat]
        return stored_cgroup.get(key)

    verdict = {
        "limits": limits,
        "scope": scope,
        "breaches": kept,
        "attributed": attributed,
        "blocked": bool(kept),
        "critical": any(b["critical"] for b in kept),
        "power": power_state(reading, limits),
        # #2814: the cgroup measure the verdict was made on (None keys when
        # the executor is not measuring one)
        "cgroup": {key: cgroup_value(key) for key in
                   ("path", "cpu_some", "mem_some", "mem_full", "cpu_quota",
                    "mem_current_bytes", "mem_max_bytes", "mem_used",
                    "throttled", "cpu_used")},
    }
    if scope_fallback:
        verdict["scope_fallback"] = scope_fallback
    return verdict


#: #2814 : the admission verdict is cached 10 s by the executor; a deferred
#: turn says when it will look again, never a promise beyond that cache
QUEUE_RETRY_S = 10


def queue_text(host: str, verdict: dict, *, queued_s: float | None = None,
               retry_s: int = QUEUE_RETRY_S, capacity: str | None = None) -> str:
    """#2814 : the visible text of a turn held by pressure, parseable by
    `parse_queue_status` — scope, breaches, age and next look.

    `waiting: pressure on host X (own cgroup) — …; queued 42 s; retry in 10 s`
    (`critical pressure …` for a critical verdict, prefixed by the caller)."""
    text = "pressure on host %s (%s) — %s" % (
        host, scope_label(verdict.get("scope")),
        describe(verdict.get("breaches")) or "threshold")
    if verdict.get("scope_fallback"):
        # #2825 N2: the scope the admission actually applied is not the one
        # requested; say why instead of leaving a guard silently behind.
        text += "; " + verdict["scope_fallback"]
    if queued_s is not None:
        text += "; queued %d s" % int(max(0.0, queued_s))
    if capacity:
        text += "; capacity %s" % capacity
    return text + "; retry in %d s" % int(retry_s)


_QUEUE_STATUS_RE = re.compile(
    r"^(?P<state>waiting|paused): (?P<critical>critical )?pressure on host "
    r"(?P<host>\S+) \((?P<scope>own cgroup|host)\) — (?P<detail>.*)$")
_QUEUED_RE = re.compile(r"; queued (\d+) s")
_RETRY_RE = re.compile(r"; retry in (\d+) s")
_CAPACITY_RE = re.compile(r"; capacity ([^;]+)")


def parse_queue_status(text: str | None) -> dict | None:
    """#2814 : the `queued` object a client reads, from an agent's status text.

    None for any other status. `state` is `queued` (a turn is waiting, not
    running); `scope`, `critical`, `queued_s`, `retry_s`, `capacity` and the
    human `detail` are the fields of `queue_text`. A turn never starts after
    its request expired: only an explicit `payload.request_expires_ts` is a
    deadline (`mail.request_expired`); a signature's `signature_expires_at`
    is not one, and mail without the explicit key never expires."""
    match = _QUEUE_STATUS_RE.match((text or "").strip())
    if match is None:
        return None
    detail = match.group("detail") or ""
    queued = _QUEUED_RE.search(detail)
    retry = _RETRY_RE.search(detail)
    capacity = _CAPACITY_RE.search(detail)
    return {
        "state": "queued",
        "reason": "pressure",
        "host": match.group("host"),
        "scope": "own_cgroup" if match.group("scope") == "own cgroup" else "host",
        "critical": bool(match.group("critical")),
        "detail": _CAPACITY_RE.sub("", _QUEUED_RE.sub("", _RETRY_RE.sub("", detail))
                                   ).strip(" ;"),
        "queued_s": int(queued.group(1)) if queued else None,
        "retry_s": int(retry.group(1)) if retry else None,
        "capacity": capacity.group(1) if capacity else None,
    }


def fmt_value(key: str, value) -> str:
    """Valeur lisible d'un seuil ou d'une mesure (octets, charge ou part)."""
    if value is None:
        return "—"
    if key == "max_load":
        return "%.2f" % float(value)
    if key in ZOMBIE_KEYS:
        return "%d" % int(value)
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
