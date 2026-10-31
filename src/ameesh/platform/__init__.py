# SPDX-License-Identifier: AGPL-3.0-only
"""Couche plateforme (L63, étude « portabilité Mac et Windows ») : le SEUL
endroit d'ameesh qui sait sur quel système il tourne.

Tout accès à `/proc`, à `sys.platform` ou au module standard `platform` passe
par ce paquet ; un test de lint (`tests/test_l63_platform.py`) le vérifie sur
le reste du code. Un sous-module par sujet :

* `process` : ascendance, heure de démarrage (secondes epoch), démarrage de
  l'hôte, détenteurs d'un fichier, processus vivant, descripteurs ouverts et
  chemin d'un descripteur, table des processus (groupes), sous-moissonneur et
  moisson d'un enfant, environnement initial d'un processus (L124) ;
* `terminal` : le terminal de l'humain ;
* `notify` : la notification de bureau (L66) — `notify-send` sous Linux,
  `osascript` sous macOS, toast Windows par `powershell.exe` sous WSL2 ;
* `service` : les services utilisateur (L65) — LaunchAgents launchd sur macOS,
  chargés dans `gui/<uid>` ; Linux garde les unités de `deploy/systemd/` ;
* `host` : mémoire, swap, charge, alimentation, système de fichiers,
  allocation btrfs (#246) ;
* `offload_shell` : le côté VM du déport (L159b), Linux seulement par
  construction ; script autonome (bibliothèque standard seule, aucun import
  d'ameesh), installé seul et lancé en `python3 -I` : il lit `/proc`
  lui-même.
* `turn_cgroup` : le cgroup v2 délégué d'un tour (L498) — création, placement,
  `cgroup.events`, kill et enlèvement ; trampoline autonome (bibliothèque
  standard seule, aucun import d'ameesh) lancé par son chemin de fichier, il
  lit `/proc/self/cgroup` lui-même pour valider son placement avant l'exec du
  harnais.

Appuyé sur psutil (roues binaires Linux, macOS, Windows) ; sous Linux, un
repli par `/proc` reste en place si psutil est absent.

Règle (fail-closed) : une fonction qui sert une GARANTIE de sécurité et ne
sait pas répondre sur cet OS lève `NotAvailable` ; elle ne rend jamais une
liste vide ou None qui voudrait dire « rien trouvé ». Les mesures non
sensibles (charge, terminal, batterie) rendent None quand l'OS ne les donne
pas : c'est dit fonction par fonction.

Nom : `ameesh.platform` ne masque pas le module standard `platform` (imports
absolus depuis Python 3 : `import platform` donne toujours la bibliothèque
standard) ; le lint réserve aussi l'import de ce dernier à ce paquet, si bien
qu'aucun module d'ameesh ne mélange les deux.
"""
from __future__ import annotations

import sys

try:  # psutil est une dépendance ; son absence n'est tolérée que sous Linux
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - dépend de l'environnement
    psutil = None  # type: ignore

#: « linux », « macos », « windows » ou le `sys.platform` brut
SYSTEM = ("linux" if sys.platform.startswith("linux")
          else "macos" if sys.platform == "darwin"
          else "windows" if sys.platform in ("win32", "cygwin")
          else sys.platform)


class NotAvailable(RuntimeError):
    """Le système ne permet pas de répondre (sujet, OS, raison).

    L'appelant décide : refus explicite (garantie de sécurité), ou mesure
    marquée inconnue (mesure non sensible)."""

    def __init__(self, subject: str, reason: str, system: str | None = None) -> None:
        self.subject = subject
        self.system = system or SYSTEM
        self.reason = reason
        super().__init__("%s unavailable on %s: %s" % (subject, self.system, reason))


def is_linux() -> bool:
    return SYSTEM == "linux"


def is_macos() -> bool:
    return SYSTEM == "macos"


def is_windows() -> bool:
    return SYSTEM == "windows"


def has_psutil() -> bool:
    return psutil is not None


def machine() -> str:
    """The CPU architecture (`x86_64`, `arm64`…), "" if unknown (#248: native
    node modules are built for it)."""
    import platform as stdlib_platform
    return stdlib_platform.machine() or ""


from .process import (PROC, HoldersUnreadable, alive, ancestry,  # noqa: E402
                      become_subreaper, become_undumpable, boot_time, children, publish_venv_attestation,
                      command_argv, command_line, environ, env_values, fd_path, holders,
                      open_fd_count, parent_pid, process_table, process_group_exists,
                      reap, exited_child, exited_child_status, self_cgroup_paths, start_time, same_start, ticks_to_epoch, user_paths, UserPaths)
from .terminal import human_tty  # noqa: E402
from .host import (CGROUP_ROOT, btrfs_allocation, btrfs_fs_info, cgroup_cpu_max,  # noqa: E402
                   cgroup_cpu_seconds, cgroup_cpu_stat, cgroup_dir, cgroup_memory_bytes,
                   cgroup_memory_max, cgroup_pressure, cpu_busy_seconds, in_container, load_average, memory,
                   mount_of, own_cgroup, own_cgroup_bounded, parse_pressure, power, pressure,
                   mountinfo, covering_mount, mount_id)

#: L68 — fonctions qui servent une GARANTIE de sécurité : sur un hôte qui ne
#: peut pas répondre (ni psutil, ni /proc, interface absente), elles lèvent
#: `NotAvailable` ; elles ne rendent jamais une liste vide ni un `None` qui
#: voudrait dire « rien trouvé ». `tests/test_l63_platform.py::GuaranteeTest`
#: exerce cette liste sur un OS muet et échoue si une garantie ajoutée ici
#: n'a pas son témoin.
GUARANTEES = (
    "ancestry",
    "boot_time",
    "children",
    "env_values",
    "environ",
    "holders",
    "open_fd_count",
    "process_table",
    "self_cgroup_paths",
    "start_time",
)

__all__ = [
    "NotAvailable", "HoldersUnreadable", "SYSTEM", "is_linux", "is_macos", "is_windows", "has_psutil",
    "GUARANTEES",
    "alive", "ancestry", "become_subreaper", "become_undumpable", "boot_time", "command_line", "environ",
    "publish_venv_attestation",
    "env_values", "fd_path",
    "children", "holders", "open_fd_count", "process_table", "process_group_exists", "reap", "exited_child", "exited_child_status", "start_time", "same_start",
    "command_argv", "parent_pid", "self_cgroup_paths",
    "ticks_to_epoch", "human_tty", "load_average", "memory", "mount_of", "power",
    "CGROUP_ROOT", "cgroup_cpu_seconds", "cgroup_memory_bytes", "cpu_busy_seconds",
    "parse_pressure", "pressure", "user_paths", "UserPaths",
    "btrfs_allocation", "btrfs_fs_info", "mountinfo", "covering_mount", "mount_id",
    "cgroup_cpu_max", "cgroup_cpu_stat", "cgroup_dir", "cgroup_memory_max", "cgroup_pressure",
    "in_container", "own_cgroup", "own_cgroup_bounded",
]
