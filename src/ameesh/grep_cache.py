# SPDX-License-Identifier: AGPL-3.0-only
"""#3974 — cache partagé de l'hôte des balayages `git grep -F -f -`.

La lecture d'auctorialité d'une plage gelée (`review_authors`) balaie l'arbre
d'un commit (`_tree_lines`, `_source_paths`) avec la liste des lignes copiées.
Sur un dépôt Nexlink, un balayage prend des dizaines de secondes de CPU ; les
boucles des exécuteurs, `notify --only alerts` et `queue add` relisaient la
même plage gelée, inchangée, à chaque passage, dans des processus distincts :
le cache en mémoire de #3753 ne survivait pas au processus.

Le résultat d'un balayage ne dépend que du **contenu** de l'arbre lu, des
options (fixées par l'appelant, jamais lues de la configuration : `--no-color`,
`-h`…) et des motifs. La clé est donc l'objet arbre (SHA, immuable), le mode
et l'empreinte des motifs, sur le modèle du cache de lectures GitHub (#848) :
un fichier par clé sous l'état de l'hôte, partagé par tous les exécuteurs du
même utilisateur. Seuls les résultats réussis (`grep` à 0 ou 1) se gardent :
un échec reste un doute et se relit au passage suivant.

Attributs (B/P2 cx2 sur b4e42699) : `-I` dépend de l'attribut `binary`, que git
lit dans les `.gitattributes` du dépôt de travail, `info/attributes`,
`core.attributesFile` et le fichier système. Le balayage lit donc les
attributs **de l'arbre balayé** (`GIT_ATTR_SOURCE`, git ≥ 2.41), sans fichier
global ni système (`neutral_attributes`) ; le seul contexte local restant,
`info/attributes` du dépôt commun, entre dans la clé par son contenu. Un git
sans `GIT_ATTR_SOURCE` (< 2.41) ne cache rien.

Deux processus qui calculent la même clé ne balaient pas deux fois : le second
attend le verrou de la clé (borné) puis relit le résultat du premier.

`AMEESH_GREP_CACHE=off` coupe le cache ; la suite de tests le coupe par
`ENABLED` (un test qui vide l'environnement ne le rallume pas) ;
`AMEESH_GREP_CACHE_DIR` choisit le dossier, sinon `<AMEESH_STATE>/git-grep`.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
import time
from typing import Callable, Optional, Sequence

from . import canon

#: sous-dossier de l'état de l'hôte qui porte le cache
CACHE_DIR = "git-grep"
#: entrées gardées au plus (les plus anciennement lues retirées)
MAX_ENTRIES = 20000
#: magasins entre deux élagages
PRUNE_EVERY = 256
#: attente au plus du verrou d'une clé qu'un autre processus calcule (s)
LOCK_WAIT_S = canon.GIT_TIMEOUT
#: pas de l'attente du verrou (s)
LOCK_STEP_S = 0.1
#: version du format : un changement de format change toutes les clés
FORMAT = 2
#: première version de git qui lit `GIT_ATTR_SOURCE` (absent de l'attr.c de
#: v2.40.0, présent en v2.41.0 : relevé cx2)
ATTR_SOURCE_GIT = (2, 41)
#: variable sans effet sur git qui porte l'empreinte de `info/attributes` dans
#: `env_extra`, donc dans la clé du cache mémoire de `canon._git` (#3753)
ATTR_TOKEN_ENV = "AMEESH_INFO_ATTRIBUTES"

_stores = 0
#: interrupteur du processus (la suite de tests le met à False)
ENABLED = True


def enabled() -> bool:
    return ENABLED and str(os.environ.get("AMEESH_GREP_CACHE") or "").strip().lower() not in (
        "off", "0", "no", "false")


def root() -> str:
    explicit = str(os.environ.get("AMEESH_GREP_CACHE_DIR") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    state = (os.environ.get("AMEESH_STATE") or os.environ.get("AGENT_MESH_STATE")
             or "~/.local/state/ameesh")
    return os.path.join(os.path.expanduser(state), CACHE_DIR)


def neutral_attributes(rev: str, repo: str = "") -> dict:
    """L'environnement d'un balayage dont les attributs ne dépendent que de
    l'arbre de `rev` et de `info/attributes` (lié à la clé).

    Avec `repo`, l'empreinte de `info/attributes` entre aussi dans
    l'environnement : le cache mémoire de `canon._git` (clé : args, stdin,
    `env_extra`, dépôt) ne relit jamais un balayage fait sous d'autres
    attributs (relevé cx2 sur fbf586ac). Une empreinte illisible donne une
    valeur unique : rien n'est alors relu du cache mémoire."""
    env = {"GIT_ATTR_SOURCE": str(rev), "GIT_ATTR_NOSYSTEM": "1",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.attributesFile",
           "GIT_CONFIG_VALUE_0": os.devnull}
    if repo:
        # sans `GIT_ATTR_SOURCE`, les attributs du dépôt de travail comptent
        # encore : aucune relecture du cache mémoire non plus
        token = attributes_token(repo) if _attr_source_supported() else None
        env[ATTR_TOKEN_ENV] = token if token is not None else (
            "unreadable-" + os.urandom(16).hex())
    return env


_git_version_cache: list = []


def _attr_source_supported() -> bool:
    if not _git_version_cache:
        version = None
        try:
            proc = canon._git(".", "--version", timeout=10.0)
            text = proc.stdout.decode("ascii", "replace").split()
            numbers = text[2].split(".") if len(text) >= 3 else []
            version = (int(numbers[0]), int(numbers[1]))
        except Exception:  # noqa: BLE001 - version illisible : pas de cache
            version = None
        _git_version_cache.append(version)
    version = _git_version_cache[0]
    return version is not None and version >= ATTR_SOURCE_GIT


def attributes_token(repo: str) -> Optional[str]:
    """L'empreinte du contenu de `info/attributes` du dépôt commun (vide s'il
    n'existe pas), ou None si elle ne peut pas être lue."""
    try:
        proc = canon._git(repo, "rev-parse", "--path-format=absolute",
                          "--git-common-dir", timeout=10.0)
    except Exception:  # noqa: BLE001 - dépôt illisible : pas de cache
        return None
    if proc.returncode != 0:
        return None
    common = proc.stdout.decode("utf-8", "replace").strip()
    if not common:
        return None
    try:
        with open(os.path.join(common, "info", "attributes"), "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        data = b""
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def tree_of(repo: str, rev: str) -> Optional[str]:
    """L'objet arbre de `rev`, lu dans les objets locaux, ou None."""
    try:
        proc = canon._git(repo, "rev-parse", "--verify", "--quiet",
                          "%s^{tree}" % rev, env_extra=canon.NO_LAZY_FETCH,
                          timeout=10.0)
    except Exception:  # noqa: BLE001 - arbre illisible : pas de cache
        return None
    if proc.returncode != 0:
        return None
    tree = proc.stdout.decode("ascii", "replace").strip()
    return tree if len(tree) in (40, 64) and all(
        ch in "0123456789abcdef" for ch in tree) else None


def key(tree: str, mode: str, patterns: Sequence[str],
        attributes: str = "") -> str:
    digest = hashlib.sha256()
    digest.update(("%d\0%s\0%s\0%s\0" % (FORMAT, mode, tree, attributes)
                   ).encode("utf-8"))
    for line in patterns:
        digest.update(str(line).encode("utf-8", "surrogatepass") + b"\0")
    return digest.hexdigest()


def _path(name: str) -> str:
    return os.path.join(root(), name[:2], name + ".json")


def _read(name: str):
    """(True, valeur) si l'entrée est lisible, sinon (False, None)."""
    path = _path(name)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False, None
    if not isinstance(data, dict) or data.get("format") != FORMAT \
            or not isinstance(data.get("value"), list):
        return False, None
    try:
        os.utime(path)                 # élagage : la plus anciennement lue part
    except OSError:
        pass
    return True, data["value"]


def _write(name: str, value: list) -> None:
    global _stores
    path = _path(name)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"format": FORMAT, "value": value}, handle,
                          ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return                         # cache best-effort : la lecture vaut
    _stores += 1
    if _stores % PRUNE_EVERY == 0:
        prune()


def prune(limit: int = MAX_ENTRIES) -> int:
    """Retire les entrées les plus anciennement lues au-delà de `limit`."""
    entries = []
    base = root()
    try:
        shards = os.listdir(base)
    except OSError:
        return 0
    for shard in shards:
        folder = os.path.join(base, shard)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            full = os.path.join(folder, name)
            try:
                entries.append((os.stat(full).st_mtime, full))
            except OSError:
                continue
    removed = 0
    if len(entries) > limit:
        entries.sort()
        for _mtime, full in entries[:len(entries) - limit]:
            try:
                os.unlink(full)
                removed += 1
            except OSError:
                pass
    return removed


class _KeyLock:
    """Verrou consultatif d'une clé, pris au mieux dans `LOCK_WAIT_S`."""

    def __init__(self, name: str, wait: float):
        self.path = _path(name)[:-len(".json")] + ".lock"
        self.wait = wait
        self.fd = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self.fd = None
            return self
        until = time.monotonic() + max(0.0, self.wait)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES) \
                        or time.monotonic() >= until:
                    os.close(self.fd)
                    self.fd = None     # sans verrou : on calcule soi-même
                    return self
            time.sleep(LOCK_STEP_S)

    def __exit__(self, *_exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return False


def cached(repo: str, rev: str, mode: str, patterns: Sequence[str],
           compute: Callable[[], Optional[list]], *,
           wait: Optional[float] = None) -> Optional[list]:
    """Le résultat de `compute()` pour (arbre de `rev`, `mode`, `patterns`).

    `compute` rend une liste JSON (le résultat) ou None (lecture impossible,
    jamais gardée) ; il balaie avec `neutral_attributes(rev)`. Sans cache,
    sans arbre ou attributs lisibles, sans `GIT_ATTR_SOURCE` ou sans dossier
    inscriptible, `compute` est simplement appelé."""
    if not enabled() or not repo or not rev or not _attr_source_supported():
        return compute()
    tree = tree_of(repo, rev)
    attributes = attributes_token(repo) if tree else None
    if tree is None or attributes is None:
        return compute()
    name = key(tree, mode, patterns, attributes)
    found, value = _read(name)
    if found:
        return value
    with _KeyLock(name, LOCK_WAIT_S if wait is None else wait):
        found, value = _read(name)     # un autre processus a pu le poser
        if found:
            return value
        value = compute()
        if value is not None:
            _write(name, list(value))
    return value
