# SPDX-License-Identifier: AGPL-3.0-only
"""#5804 : le banc n'écrit jamais dans l'état d'exploitation ameesh du poste.

Constat : le 27/10, un extrait de diagnostic a appelé `read_attestation()` avec
le vrai HOME ; la preuve partagée a été matérialisée dans
`~/.local/state/ameesh/mise-a-jour/actif-aaaaaaaaaaaa.json` (sha de fixture,
date 2026-10-18T09:00:00Z). `staging_pending.active_hosts()` lit tous les
`actif-*.json` de ce dossier réel : le coupeur refusait (#5748).

Le banc garde le HOME du poste (L142 : `~/.pgpass` se lit là), mais un test
n'a aucune raison d'écrire l'état d'exploitation qui vit dessous. Ce garde
refuse l'écriture au moment du geste, en nommant le chemin ; `tests/__init__`
l'installe pour toute la suite et `tests/test_5804_real_state_guard.py` le
prouve.

Sont protégés les deux dossiers que lisent la coupure et le staging
(`mise-a-jour/` : attestations et journal ; `release/` : état, verrou,
rapports), pas toute la racine : les bacs jetables (`$TMPDIR`), les clones de
travail des agents et leurs sessions vivent sous le même `state/ameesh` et un
test doit pouvoir y écrire.

Le garde ne voit que le processus de test, pas ses sous-processus (qui
héritent en général d'un HOME de bac) ; c'est la portée du constat : un test,
ou un extrait lancé comme lui, écrivait dans l'état réel.
"""
from __future__ import annotations

import os
import sys

#: Racine de l'état de l'utilisateur (L142 : HOME du poste).
STATE_SUBDIR = os.path.join(".local", "state", "ameesh")

#: Sous-dossiers d'exploitation que la coupure lit et n'écrit qu'en production.
PROTECTED_SUBDIRS = ("mise-a-jour", "release")

#: Événements d'audit qui modifient un chemin (cf. `sys.addaudithook`).
_WRITE_EVENTS = frozenset((
    "open", "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.truncate",
    "os.chmod", "os.chown", "os.utime", "os.symlink", "os.link",
))

#: `open(path, mode, flags)` : les drapeaux d'écriture d'`os.open`.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

_roots: tuple = ()


class RealStateWrite(RuntimeError):
    """Un test a tenté d'écrire dans l'état d'exploitation ameesh du poste."""


def default_state() -> str:
    """La racine de l'état réel, d'après le HOME du poste."""
    return os.path.join(os.path.expanduser("~"), STATE_SUBDIR)


def default_roots() -> tuple:
    """Les dossiers d'exploitation protégés par défaut."""
    state = default_state()
    return tuple(os.path.join(state, name) for name in PROTECTED_SUBDIRS)


def installed_roots() -> tuple:
    """Les dossiers protégés, vide tant que le garde n'est pas installé."""
    return _roots


def _normalise(path):
    """Le chemin en absolu texte, ou None si ce n'est pas un chemin.

    Les événements d'audit peuvent porter un chemin `bytes` (open de
    `os.fsencode(x)`). `os.fspath` le laisserait en `bytes`, et la comparaison
    `bytes.startswith(str)` lèverait TypeError sur une écriture pourtant
    permise (B1 ameesh-cx2, #5804) ; `os.fsdecode` accepte `str` et `bytes`
    et rend toujours `str`. Un descripteur de fichier (`open(fd)`) n'est pas
    un chemin : TypeError, traité comme tel.
    """
    try:
        return os.path.abspath(os.fsdecode(path))
    except (TypeError, ValueError):
        return None


def _inside(path, root: str) -> bool:
    """Le chemin lexical `path` est-il sous `root` ?

    Comparaison de chemins absolus, sans `realpath` : le garde tourne à chaque
    `open` du processus et ne doit pas déclencher d'appels système. Un lien
    symbolique qui contournerait le chemin lexical n'est pas le constat visé.
    """
    path = _normalise(path)
    if path is None:
        return False
    return path == root or path.startswith(root + os.sep)


def _writing(mode, flags) -> bool:
    """L'ouverture d'audit `open` modifie-t-elle le fichier ?"""
    if isinstance(mode, str):
        return any(char in mode for char in "wax+")
    if isinstance(flags, int):
        return bool(flags & _WRITE_FLAGS)
    return False


def _modified_paths(event: str, args) -> tuple:
    """Les chemins que l'événement d'audit écrit ou supprime."""
    if event == "open":
        path, mode, flags = args
        return (path,) if _writing(mode, flags) else ()
    if event == "os.rename":
        # (src, dst, src_dir_fd, dst_dir_fd) : les deux côtés changent.
        return tuple(args[:2])
    if event in ("os.link", "os.symlink"):
        # (src, dst, ...) : seule la destination est créée.
        return (args[1],)
    if event in _WRITE_EVENTS:
        return (args[0],)
    return ()


def check(event: str, args) -> None:
    """Refuse l'écriture sous un dossier protégé (no-op sinon)."""
    if event not in _WRITE_EVENTS or not _roots:
        return
    for path in _modified_paths(event, args):
        for root in _roots:
            if _inside(path, root):
                raise RealStateWrite(
                    "#5804 : un test écrit dans l'état d'exploitation ameesh "
                    "(%s). Un test isole son état : rediriger HOME vers un "
                    "dossier temporaire (voir tests/test_2448_release_cutoff.py, "
                    "ReleaseFixtureMixin) ; l'état réel ne se salit pas depuis "
                    "un test." % _normalise(path))


def _hook(event: str, args) -> None:
    check(event, args)


def install(roots=None) -> tuple:
    """Installe le garde sur `roots` (défaut : le HOME du poste). Idempotent."""
    global _roots
    if _roots:
        return _roots
    _roots = tuple(os.path.abspath(root) for root in (roots or default_roots()))
    sys.addaudithook(_hook)
    return _roots
