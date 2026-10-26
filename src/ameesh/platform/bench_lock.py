# SPDX-License-Identifier: AGPL-3.0-only
"""Exclusion hôte entre les suites du banc et sa maintenance (#3735 B/P1).

Le banc Postgres est local à l'hôte et partagé par les suites. Une suite peut
être vivante **sans client backend** entre deux échanges (`PsqlDriver` ouvre
une connexion par requête) : un compteur de sessions ne prouve donc pas son
absence. Le verrou vit sur un fichier de l'hôte : une suite le tient en
**partagé** pendant toute la vie de son processus, la maintenance ne prend
l'**exclusif** que si aucune suite ne le tient. Un processus tué rend son
verrou dans l'OS, comme `session_lock`.

Sans verrou partagé (Windows, `msvcrt`), l'exclusion effective est
impossible : les deux modes rendent None et la maintenance reste
conservatrice (aucun schéma sans marqueur n'est admis à la purge).
"""
from __future__ import annotations

import errno
import os


def try_shared(path: str):
    """Un verrou partagé ouvert sur `path`, ou None si un exclusif le tient."""
    return _try(path, shared=True)


def try_exclusive(path: str):
    """Un verrou exclusif ouvert sur `path`, ou None si un verrou le tient."""
    return _try(path, shared=False)


def _try(path: str, *, shared: bool):
    if os.name == "nt":
        return None  # msvcrt n'a pas de partagé : pas d'exclusion effective
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    handle = open(path, "a+b")
    try:
        import fcntl
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    except BaseException:
        handle.close()
        raise
    return handle
