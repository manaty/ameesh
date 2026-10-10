# SPDX-License-Identifier: AGPL-3.0-only
"""Terminal de l'humain (L63) : le pseudo-terminal où écrire le titre.

Mesure non sensible : None quand on ne trouve pas (ou sous Windows).
"""
from __future__ import annotations

import os

from . import NotAvailable
from .process import PROC, _proc_ok, _psutil, _stat_fields

#: préfixes des pseudo-terminaux : Linux (`/dev/pts/N`), macOS (`/dev/ttysNNN`)
TTY_PREFIXES = ("/dev/pts/", "/dev/ttys")

#: profondeur de remontée (comme avant L63)
_DEPTH = 12


def _is_tty(path: str | None) -> bool:
    return bool(path) and any(path.startswith(p) for p in TTY_PREFIXES)


def human_tty() -> str | None:
    """Le terminal où écrit ce processus ou son plus proche ascendant.

    Linux : la cible de ses descripteurs 1, 2, 0 dans /proc (on suit la
    sortie réelle, pas seulement le terminal de contrôle). Ailleurs : le
    terminal de contrôle donné par psutil. None si rien n'est trouvé."""
    if _proc_ok():
        return _tty_proc()
    ps = _psutil()
    if ps is None:
        return None
    pid = os.getpid()
    for _ in range(_DEPTH):
        try:
            proc = ps.Process(pid)
            terminal = proc.terminal() if hasattr(proc, "terminal") else None
            if _is_tty(terminal):
                return terminal
            pid = int(proc.ppid())
        except Exception:
            return None
        if pid <= 1:
            return None
    return None


def _tty_proc() -> str | None:
    pid = os.getpid()
    for _ in range(_DEPTH):
        for fd in ("1", "2", "0"):
            try:
                target = os.readlink(os.path.join(PROC, str(pid), "fd", fd))
            except OSError:
                continue
            if target.startswith("/dev/pts/"):
                return target
        try:
            pid = int(_stat_fields(pid)[1])
        except (OSError, ValueError, IndexError, NotAvailable):
            return None
        if pid <= 1:
            return None
    return None
