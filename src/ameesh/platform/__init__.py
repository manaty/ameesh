# SPDX-License-Identifier: AGPL-3.0-only
"""Couche plateforme (L63, étude « portabilité Mac et Windows ») : le SEUL
endroit d'ameesh qui sait sur quel système il tourne.

Tout accès à `/proc`, à `sys.platform` ou au module standard `platform` passe
par ce paquet ; un test de lint (`tests/test_l63_platform.py`) le vérifie sur
le reste du code. Un sous-module par sujet :

* `process` : ascendance, heure de démarrage (secondes epoch), démarrage de
  l'hôte, détenteurs d'un fichier, processus vivant, descripteurs ouverts,
  table des processus (groupes), sous-moissonneur et moisson d'un enfant,
  environnement initial d'un processus (L124) ;
* `terminal` : le terminal de l'humain ;
* `host` : mémoire, swap, charge, alimentation, système de fichiers.

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
        super().__init__("%s indisponible sur %s : %s" % (subject, self.system, reason))


def is_linux() -> bool:
    return SYSTEM == "linux"


def is_macos() -> bool:
    return SYSTEM == "macos"


def is_windows() -> bool:
    return SYSTEM == "windows"


def has_psutil() -> bool:
    return psutil is not None


from .process import (alive, ancestry, become_subreaper, boot_time,  # noqa: E402
                      command_line, environ, holders, open_fd_count, process_table,
                      reap, start_time, same_start, ticks_to_epoch)
from .terminal import human_tty  # noqa: E402
from .host import load_average, memory, mount_of, power  # noqa: E402

__all__ = [
    "NotAvailable", "SYSTEM", "is_linux", "is_macos", "is_windows", "has_psutil",
    "alive", "ancestry", "become_subreaper", "boot_time", "command_line", "environ",
    "holders", "open_fd_count", "process_table", "reap", "start_time", "same_start",
    "ticks_to_epoch", "human_tty", "load_average", "memory", "mount_of", "power",
]
