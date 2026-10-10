# SPDX-License-Identifier: AGPL-3.0-only
"""Ressources des hôtes (lot L31, décision 0028).

Chaque exécuteur relève périodiquement l'état de son hôte — mémoire
disponible, swap utilisé, charge CPU (1 minute), disque libre du dossier de
travail, tours en cours — et le publie en base (`host_resources`). `ameesh
hosts` montre le dernier relevé de chaque hôte et l'historique court.

Les MESURES sont de l'état d'exécution (base) ; les SEUILS sont déclaratifs
(canon, `policy.resources` de la fiche `Host`) et lus par
[`thresholds`] / [`breaches`]. Toutes les écritures passent par le stockage
(`storage.of(db).hosts`, spec §10) ; aucune écriture automatique du canon.

Unités : mémoire, swap et disque en OCTETS (un entier, ou un texte avec
unité `KiB`/`MiB`/`GiB`/`TiB` — base 1024 — ou `KB`/`MB`/`GB`/`TB` — base
1000). `max_load` est une charge système (flottant), comparée à la charge 1
minute. `min_mem_available` et `min_disk_free` sont des planchers ; les autres
sont des plafonds.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time

from . import storage
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

#: clés de seuil reconnues dans `policy.resources` (liste fermée)
THRESHOLD_KEYS = ("min_mem_available", "max_swap_used", "max_load", "min_disk_free",
                  "max_tmpfs_used", *POWER_KEYS)
#: seuils exprimés en PART (0 < x <= 1, ou texte « 80% ») et non en octets
FRACTION_KEYS = ("max_tmpfs_used",)
#: dossier des sources d'alimentation (Linux) ; surchargé par les tests
POWER_SUPPLY_ENV = "AMEESH_POWER_SUPPLY_DIR"
POWER_SUPPLY_DIR = "/sys/class/power_supply"

#: horizon de conservation des relevés (secondes) ; l'historique court de
#: `ameesh hosts` n'a pas besoin de plus, et la table ne grandit pas sans fin.
HISTORY_RETENTION_S = 7 * 24 * 3600.0

_UNITS = {
    "": 1,
    "b": 1,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
    "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
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
        return value if value >= 0 else None
    if isinstance(value, float):
        if not decimal_ok or value < 0:
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
    return int(float(number) * factor)


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
        number = float(value)
    else:
        return None
    return number if 0.0 < number <= 1.0 else None


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_percent(value) -> float | None:
    """Un pourcentage de 0 à 100 (« 25 », 25, « 25% »), ou None."""
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
    number = _as_float(value)
    if number is None or not 0.0 <= number <= 100.0:
        return None
    return number


def boot_time(path: str = "/proc/stat") -> float | None:
    """Instant du démarrage de l'hôte (epoch), ou None hors Linux (L106)."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def power(base: str | None = None) -> dict:
    """Alimentation de l'hôte (L106) : `on_ac` (secteur), `battery_percent`.

    Linux : `/sys/class/power_supply` — secteur (`Mains`, `USB*`, champ
    `online`) et batteries du SYSTÈME (`scope` ≠ `Device` : pas la souris),
    pourcentage pondéré par la capacité quand plusieurs batteries. Sans
    source de secteur déclarée, l'état de charge des batteries tranche
    (`Discharging` = sur batterie). Ailleurs, ou illisible : None (« inconnu ») —
    on ne devine pas, et aucune garde ne se déclenche sur un inconnu. Un
    autre système se branchera ici (pas de couche plateforme pour l'instant).
    """
    out: dict = {"on_ac": None, "battery_percent": None}
    base = base or os.environ.get(POWER_SUPPLY_ENV) or POWER_SUPPLY_DIR
    if not sys.platform.startswith("linux") and base == POWER_SUPPLY_DIR:
        return out
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
            percent = parse_percent(_read(os.path.join(root, "capacity")))
            poids = 1.0
            for now_key, full_key in (("energy_now", "energy_full"),
                                      ("charge_now", "charge_full")):
                now_v = _as_float(_read(os.path.join(root, now_key)))
                full_v = _as_float(_read(os.path.join(root, full_key)))
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


def _meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """(mémoire disponible, swap utilisé) en octets, lus dans /proc/meminfo.

    Un noyau sans `MemAvailable` (très ancien) est traité comme illisible :
    mieux vaut ne rien publier que publier un zéro trompeur.
    """
    values: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if not rest:
                    continue
                number = rest.strip().split()[0]
                values[key.strip()] = int(number) * 1024
    except (OSError, ValueError, IndexError):
        return {}
    out: dict[str, int] = {}
    if "MemAvailable" in values:
        out["mem_available_bytes"] = values["MemAvailable"]
    total, free = values.get("SwapTotal"), values.get("SwapFree")
    if total is not None and free is not None:
        out["swap_used_bytes"] = max(0, total - free)
    return out


def cpu_count() -> int | None:
    try:
        return os.cpu_count() or None
    except OSError:  # pragma: no cover - os.cpu_count ne lève pas, mais restons fermes
        return None


def load1() -> float | None:
    """Charge système sur 1 minute, ou None si le poste ne la donne pas."""
    try:
        return float(os.getloadavg()[0])
    except (OSError, ValueError):
        return None


def disk_free(path: str | None) -> int | None:
    if not path:
        return None
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _mount_of(path: str, mounts: str = "/proc/mounts") -> tuple[str, str] | None:
    """(point de montage, type) du système de fichiers qui porte `path`."""
    real = os.path.realpath(path)
    best: tuple[str, str] | None = None
    try:
        with open(mounts, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                point = parts[1].replace("\\040", " ")
                if real == point or real.startswith(point.rstrip("/") + "/") or point == "/":
                    if best is None or len(point) > len(best[0]):
                        best = (point, parts[2])
    except OSError:
        return None
    return best


def fs_usage(path: str | None, *, mounts: str = "/proc/mounts") -> dict:
    """Occupation du système de fichiers de `path` (L73) : chemin, type
    (`tmpfs`, `ext4`…), taille et octets utilisés ; valeurs None si
    illisibles. Lecture seule, sans parcours : `statvfs` seulement."""
    out = {"tmp_path": path, "tmp_fstype": None, "tmp_size_bytes": None,
           "tmp_used_bytes": None}
    if not path or not os.path.isdir(path):
        return out
    mount = _mount_of(path, mounts)
    if mount is not None:
        out["tmp_fstype"] = mount[1]
    try:
        st = os.statvfs(path)
    except OSError:
        return out
    size = st.f_blocks * st.f_frsize
    out["tmp_size_bytes"] = int(size)
    out["tmp_used_bytes"] = int(size - st.f_bfree * st.f_frsize)
    return out


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


def sample(host: str, *, path: str | None = None, now: float | None = None,
           turns_in_progress: int | None = None, tmp: str | None = None) -> dict:
    """Un relevé de l'hôte : les mesures lisibles, les autres à None.

    `path` est le dossier dont on mesure le disque (le dossier de travail de
    l'hôte) ; `now` permet aux tests de fixer l'instant.
    """
    out: dict = {
        "host": host,
        "sampled_ts": time.time() if now is None else float(now),
        "mem_available_bytes": None,
        "swap_used_bytes": None,
        "load1": load1(),
        "cpu_count": cpu_count(),
        "disk_free_bytes": disk_free(path),
        "disk_path": path,
        "turns_in_progress": turns_in_progress,
    }
    out.update(_meminfo())
    out.update(fs_usage(system_tmp() if tmp is None else tmp))
    out.update(power())
    return out


def collect(cfg, db: Db, *, now: float | None = None) -> dict:
    """Relève l'hôte de cet exécuteur et publie le relevé ; rend le relevé.

    Le nombre de tours en cours vient de la base (agents de l'hôte au bail
    vivant et en tour) : c'est un état, pas une mesure locale.
    """
    host = cfg.host or ""
    try:
        turns = storage.of(db).hosts.turns_in_progress(host)
    except Exception:  # un relevé ne doit jamais faire tomber la boucle
        turns = None
    path = _work_path(cfg)
    reading = sample(host, path=path, now=now, turns_in_progress=turns)
    storage.of(db).hosts.record(reading)
    return reading


def _work_path(cfg) -> str | None:
    """Dossier dont on mesure le disque : la première racine de travail de
    l'hôte qui existe, sinon le répertoire courant."""
    for root in getattr(cfg, "worktree_roots", ()) or ():
        expanded = os.path.expanduser(root)
        if os.path.isdir(expanded):
            return expanded
    return os.getcwd()


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
    load = _as_float(raw.get("max_load"))
    tmpfs = parse_fraction(raw.get("max_tmpfs_used"))
    low = parse_percent(raw.get("min_battery_percent"))
    stop = parse_percent(raw.get("stop_battery_percent"))
    cpus = cpu_count() or 1
    return {
        "min_mem_available": DEFAULT_MIN_MEM_AVAILABLE if mem is None else mem,
        "max_swap_used": DEFAULT_MAX_SWAP_USED if swap is None else swap,
        "max_load": (DEFAULT_MAX_LOAD_FACTOR * cpus) if load is None else load,
        "min_disk_free": DEFAULT_MIN_DISK_FREE if disk is None else disk,
        "max_tmpfs_used": DEFAULT_MAX_TMPFS_USED if tmpfs is None else tmpfs,
        "min_battery_percent": DEFAULT_MIN_BATTERY_PERCENT if low is None else low,
        "stop_battery_percent": DEFAULT_STOP_BATTERY_PERCENT if stop is None else stop,
    }


#: L43 (0031) : seuils PLANCHERS (plus strict = plus haut) ; les autres sont
#: des plafonds (plus strict = plus bas)
FLOOR_KEYS = ("min_mem_available", "min_disk_free", *POWER_KEYS)
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
        value = (_as_float(raw.get(key)) if key == "max_load"
                 else parse_fraction(raw.get(key)) if key in FRACTION_KEYS
                 else parse_percent(raw.get(key)) if key in POWER_KEYS
                 else parse_bytes(raw.get(key)))
        if value is not None:
            out[key] = value
    return out


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
        if value is None:
            return
        if value < limit:
            out.append({"key": key, "label": label, "value": value, "limit": limit,
                        "critical": value <= limit * critical_factor})

    def ceiling(key, label, value, limit, critical_factor):
        if value is None:
            return
        if value > limit:
            out.append({"key": key, "label": label, "value": value, "limit": limit,
                        "critical": value >= limit * critical_factor})

    floor("min_mem_available", "mémoire disponible", reading.get("mem_available_bytes"),
          limits["min_mem_available"], CRITICAL_MEM_FACTOR)
    ceiling("max_swap_used", "swap utilisé", reading.get("swap_used_bytes"),
            limits["max_swap_used"], CRITICAL_SWAP_FACTOR)
    ceiling("max_load", "charge 1 min", reading.get("load1"),
            limits["max_load"], CRITICAL_LOAD_FACTOR)
    floor("min_disk_free", "disque libre", reading.get("disk_free_bytes"),
          limits["min_disk_free"], CRITICAL_DISK_FACTOR)
    # L73 : un /tmp en tmpfs vit en mémoire et en swap ; critique à mi-chemin
    # entre le seuil et le plein.
    part = tmpfs_fraction(reading)
    limit = limits.get("max_tmpfs_used")
    if part is not None and limit is not None and part > limit:
        out.append({"key": "max_tmpfs_used", "label": "tmpfs %s occupé"
                    % (reading.get("tmp_path") or "/tmp"), "value": part, "limit": limit,
                    "critical": part >= limit + (1.0 - limit) / 2.0})
    etat = power_state(reading, limits)
    if etat["state"] in ("low", "stop"):
        # L106 : jamais `critical` — la pause des agents peu prioritaires ne
        # sert à rien ici ; l'arrêt propre (`stop`) est l'affaire de l'exécuteur
        out.append({"key": "min_battery_percent", "label": "batterie (sur batterie)",
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
    number = int(value)
    for label, factor in (("TiB", 1024 ** 4), ("GiB", 1024 ** 3), ("MiB", 1024 ** 2),
                          ("KiB", 1024)):
        if abs(number) >= factor:
            return "%.1f %s" % (number / factor, label)
    return "%d B" % number


def history(db: Db, host: str, limit: int = 10) -> list[dict]:
    return storage.of(db).hosts.history(host, limit)


def current(db: Db, host: str | None = None) -> list[dict]:
    return storage.of(db).hosts.current(host)
