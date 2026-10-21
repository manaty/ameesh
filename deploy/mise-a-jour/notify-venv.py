#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#2950 : attester le venv du MainPID vivant, après une bascule #1969."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys


def verifier(pid: str, venv: Path) -> None:
    if not pid.isascii() or not pid.isdecimal() or int(pid) <= 0:
        raise ValueError("MainPID absent ou invalide")
    courant = venv.parent / "venv-courant"
    # Avant la première adoption #1969, venv est encore un vrai venv.
    cible = (courant if courant.is_symlink() else venv).resolve(strict=True)
    proc = Path("/proc") / pid
    argv = (proc / "cmdline").read_bytes().split(b"\0")
    interpreteur = Path(os.fsdecode(argv[0]))
    # Ne pas résoudre le binaire entier : tous les venv peuvent avoir le même
    # /proc/PID/exe (/usr/bin/python). Le répertoire de l'argv[0] concret est
    # celui fixé par le lanceur #1969 au démarrage du processus.
    if (not interpreteur.is_absolute()
            or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", interpreteur.name)
            or interpreteur.parent.resolve(strict=True) != cible / "bin"):
        raise ValueError("le processus vivant n'exécute pas le venv courant ; redémarrer ameesh-notify")
    if not (proc / "exe").samefile(interpreteur):
        raise ValueError("l'exécutable vivant ne correspond pas à son interpréteur")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: notify-venv.py MainPID VENV", file=sys.stderr)
        return 2
    try:
        verifier(argv[0], Path(argv[1]))
    except (OSError, ValueError, RuntimeError):
        # Aucun contenu de cmdline/environ ni secret n'est affiché.
        print("ameesh-notify : venv courant non prouvé pour le MainPID vivant ; "
              "redémarrer notify depuis une unité indépendante puis relancer controler",
              file=sys.stderr)
        return 1
    print("ameesh-notify : MainPID vivant sur le venv courant")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
