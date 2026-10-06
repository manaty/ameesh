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

#: facteurs de gravité : au-delà, la pression est CRITIQUE (pause des agents
#: les moins prioritaires, jamais au milieu d'un tour).
CRITICAL_MEM_FACTOR = 0.5      # mémoire disponible <= la moitié du plancher
CRITICAL_DISK_FACTOR = 0.5     # disque libre <= la moitié du plancher
CRITICAL_SWAP_FACTOR = 2.0     # swap >= le double du plafond
CRITICAL_LOAD_FACTOR = 2.0     # charge >= le double du plafond

#: clés de seuil reconnues dans `policy.resources` (liste fermée)
THRESHOLD_KEYS = ("min_mem_available", "max_swap_used", "max_load", "min_disk_free")

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


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def sample(host: str, *, path: str | None = None, now: float | None = None,
           turns_in_progress: int | None = None) -> dict:
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
    cpus = cpu_count() or 1
    return {
        "min_mem_available": DEFAULT_MIN_MEM_AVAILABLE if mem is None else mem,
        "max_swap_used": DEFAULT_MAX_SWAP_USED if swap is None else swap,
        "max_load": (DEFAULT_MAX_LOAD_FACTOR * cpus) if load is None else load,
        "min_disk_free": DEFAULT_MIN_DISK_FREE if disk is None else disk,
    }


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
    out.sort(key=lambda b: (not b["critical"], b["key"]))
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
    }


def history(db: Db, host: str, limit: int = 10) -> list[dict]:
    return storage.of(db).hosts.history(host, limit)


def current(db: Db, host: str | None = None) -> list[dict]:
    return storage.of(db).hosts.current(host)
