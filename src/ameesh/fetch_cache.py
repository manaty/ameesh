# SPDX-License-Identifier: AGPL-3.0-only
"""#4457 — cache négatif des objets de forge refusés.

La lecture d'auctorialité demande à la forge, par SHA exact, le commit d'un
relecteur retiré (`review_authors._rewritten_after_freeze`). Une forge qui ne
sert pas l'objet le refuse en ~0,5 s ; sans mémoire, chaque lecture (gel,
porte, train) répétait les mêmes refus et le coût croissait avec l'activité de
l'agent.

Ce cache retient, sur l'état de l'hôte comme `grep_cache`, les couples
(dépôt, SHA) **refusés** pendant une durée bornée : la deuxième lecture ne
redemande pas. Un refus n'est pas une preuve et ne change aucune décision : le
SHA ignoré reste ignoré, seul le coût du fetch disparaît. `AMEESH_FETCH_CACHE=off`
coupe le cache ; `AMEESH_FETCH_CACHE_DIR` choisit le dossier, sinon
`<AMEESH_STATE>/git-fetch-refused`.
"""
from __future__ import annotations

import hashlib
import os
import time

#: âge au-delà duquel un refus n'est plus cru (s) : la forge peut réparer
TTL_S = 6 * 3600
#: entrées gardées au plus (les plus anciennes retirées)
MAX_ENTRIES = 4096
#: sous-dossier de l'état de l'hôte
CACHE_DIR = "git-fetch-refused"
#: interrupteur du processus (les tests peuvent le couper)
ENABLED = True


def enabled() -> bool:
    return ENABLED and str(os.environ.get("AMEESH_FETCH_CACHE") or "").strip().lower() not in (
        "off", "0", "no", "false")


def root() -> str:
    explicit = str(os.environ.get("AMEESH_FETCH_CACHE_DIR") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    state = (os.environ.get("AMEESH_STATE") or os.environ.get("AGENT_MESH_STATE")
             or "~/.local/state/ameesh")
    return os.path.join(os.path.expanduser(state), CACHE_DIR)


def _key(repo: str, sha: str) -> str:
    text = str(repo or "") + "\0" + str(sha or "").strip().lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path(repo: str, sha: str) -> str:
    return os.path.join(root(), _key(repo, sha))


def refused(repo: str, sha: str) -> bool:
    """La forge a-t-elle déjà refusé ce commit récemment ?"""
    if not enabled():
        return False
    path = _path(repo, sha)
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return False
    if age <= TTL_S:
        return True
    try:
        os.unlink(path)
    except OSError:
        pass
    return False


def note(repo: str, sha: str) -> None:
    """Retient un refus ; jamais d'exception (le cache ne bloque rien)."""
    if not enabled():
        return
    directory = root()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = _path(repo, sha)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.utime(path, None)
        _prune(directory)
    except OSError:
        pass


def _prune(directory: str) -> None:
    try:
        names = os.listdir(directory)
    except OSError:
        return
    if len(names) <= MAX_ENTRIES:
        return
    rows = []
    for name in names:
        try:
            rows.append((os.stat(os.path.join(directory, name)).st_mtime, name))
        except OSError:
            continue
    rows.sort()
    keep = MAX_ENTRIES // 2
    for _stamp, name in rows[:max(len(rows) - keep, 0)]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            continue
