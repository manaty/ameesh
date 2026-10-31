#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#2950/#3311 : attester le venv d'un processus vivant, après une bascule #1969.

Le lanceur `ExecStart` est stable (`venv/bin/ameesh`) : il résout
`venv-courant` au démarrage, si bien qu'un processus déjà lancé garde l'ancien
venv versionné après une bascule. Ni `ExecStart` ni `/proc/PID/exe` seuls ne
prouvent le venv vivant (tous les venv peuvent partager le même
`/usr/bin/python`) : le répertoire de l'`argv[0]` concret, fixé par le lanceur
au démarrage, le prouve. Utilisé par `poste.sh controler` (notify, exécuteurs)
et par `vm.sh controler` (appelé alors sur la cible, où `/proc` est lu).

    venv-processus.py MainPID VENV LIBELLE

`VENV` est le venv stable (celui de `ExecStart`) ; le vivant est sa cible
`venv-courant`, ou `VENV` lui-même avant la première adoption #1969. Aucun
contenu de cmdline/environ ni secret n'est affiché.

#5100 : un exécuteur non dumpable (#5029) cache `/proc/PID/exe` à son propre
utilisateur. Il a publié au démarrage l'identité de son exécutable
(`ameesh.platform.publish_venv_attestation`) ; elle remplace la lecture refusée
si elle vise bien ce processus (PID et heure de démarrage). Absente ou périmée :
code 3, preuve privilégiée requise ; contredite : rouge.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys


def venv_installe(venv: Path) -> Path:
    """Le venv vivant désigné par un venv stable (#1969)."""
    courant = venv.parent / "venv-courant"
    # Avant la première adoption #1969, venv est encore un vrai venv.
    return (courant if courant.is_symlink() else venv).resolve(strict=True)


def attestation(pid: str) -> dict | None:
    """L'attestation publiée pour `pid`, si ce même utilisateur l'a écrite."""
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    if not os.path.isabs(base):
        base = os.path.expanduser("~/.local/state")
    chemin = os.path.join(base, "ameesh-host", "venv-vivant", pid + ".json")
    try:
        fd = os.open(chemin, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, encoding="utf-8") as flux:
        info = os.fstat(flux.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            return None
        try:
            lu = json.load(flux)
        except ValueError:
            return None
    return lu if isinstance(lu, dict) else None


def verifier_attestation(pid: str, proc: Path, interpreteur: Path) -> None:
    """Remplace `samefile` quand le noyau refuse `/proc/PID/exe` (#5100)."""
    lu = attestation(pid)
    # Après le dernier « ) » du comm, le 20e champ est le 22e de stat(5).
    debut = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
    if not lu or lu.get("pid") != int(pid) or lu.get("start_ticks") != debut:
        raise PermissionError("attestation absente ou périmée")
    reel = interpreteur.stat()
    if (lu.get("exe_dev"), lu.get("exe_ino")) != (reel.st_dev, reel.st_ino):
        raise ValueError("l'exécutable attesté ne correspond pas à son interpréteur")


def verifier(pid: str, venv: Path) -> None:
    if not pid.isascii() or not pid.isdecimal() or int(pid) <= 0:
        raise ValueError("MainPID absent ou invalide")
    cible = venv_installe(venv)
    proc = Path("/proc") / pid
    argv = (proc / "cmdline").read_bytes().split(b"\0")
    if not argv or not argv[0]:
        raise ValueError("cmdline vide")
    interpreteur = Path(os.fsdecode(argv[0]))
    # Ne pas résoudre le binaire entier : tous les venv peuvent avoir le même
    # /proc/PID/exe (/usr/bin/python). Le répertoire de l'argv[0] concret est
    # celui fixé par le lanceur #1969 au démarrage du processus.
    if (not interpreteur.is_absolute()
            or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", interpreteur.name)
            or interpreteur.parent.resolve(strict=True) != cible / "bin"):
        raise ValueError("le processus vivant n'exécute pas le venv courant")
    try:
        meme = (proc / "exe").samefile(interpreteur)
    except PermissionError:
        verifier_attestation(pid, proc, interpreteur)
        return
    if not meme:
        raise ValueError("l'exécutable vivant ne correspond pas à son interpréteur")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: venv-processus.py MainPID VENV LIBELLE", file=sys.stderr)
        return 2
    pid, venv, libelle = argv
    try:
        verifier(pid, Path(venv))
    except PermissionError:
        # #5094 : demander une preuve privilégiée, sans accepter le processus
        # sur sa seule cmdline et sans désactiver son durcissement #5029.
        print("%s : accès /proc refusé ; preuve privilégiée requise" % libelle,
              file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError):
        # Aucun contenu de cmdline/environ ni secret n'est affiché.
        print("%s : venv courant non prouvé pour le MainPID vivant ; "
              "redémarrer son unité puis relancer controler" % libelle,
              file=sys.stderr)
        return 1
    print("%s : MainPID vivant sur le venv courant" % libelle)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
