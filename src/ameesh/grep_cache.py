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

#4475 : les lectures complètes d’arbre sont partagées entre motifs. Le LRU
borne le magasin à 256 Mio et 20 000 entrées ; une entrée dépassant seule le
budget est retirée.

#4482 : une entrée `tree-lines` d'un gros arbre pèse des dizaines de Mo en
JSON (536 944 lignes, 32,8 Mo sur `main`) et ne tenait que ~7 fois dans le
budget ; chaque lecture refaisait ce `json.load` (~0,4 s). Le contenu est donc
stocké **compressé** (gzip niveau 4 ; lignes jointes par `\\n`, 10,0 Mo pour
un arbre de 544 000 lignes, ~3,2 fois plus d'arbres dans le budget), relu sans
JSON (liste de chaînes) et **mémoïsé dans le processus** (`MEMO_MAX_BYTES` :
le même arbre n'est décodé qu'une fois par processus, pas une fois par appel). L'élagage
n'est plus refait après chaque écriture (0,14-0,31 s à 20 000 entrées) : une
grosse entrée l'applique aussitôt, les petites l'amortissent sur
`PRUNE_EVERY` écritures ou `PRUNE_PENDING_BYTES` octets. Sous concurrence, la
borne peut être dépassée de moins de `PRUNE_PENDING_BYTES` par processus
entre deux élagages ; le compte exact est refait à chaque élagage.

#4913 : une entrée au-delà de `MAX_ENTRY_BYTES` (16 Mio) n'est pas publiée ;
sa valeur est quand même rendue à l'appelant. À 22 Mio, l'entrée d'arbre de
`release/11ag` ne tenait que ~11 fois dans le budget et était réécrite en
boucle dès qu'une autre lecture l'évincait. `review_authors._tree_lines` ne
demande plus l'arbre entier pour un petit jeu de lignes : le balayage ciblé
(`git grep -F -f -`) ne cache que les lignes trouvées, donc une entrée bornée
par la demande.

#4964 : une entrée écartée par `MAX_ENTRY_BYTES` reste **mémoïsée dans le
processus** (marqueur `None` dans `_MEMO`, budget `MEMO_MAX_BYTES` porté à
48 Mio pour garder deux grosses entrées). Une demande de plus de
`TREE_LINES_WHOLE_MAX` lignes lit l'arbre entier une fois par processus au
lieu d'une fois par appel, et un appelant concurrent de la même clé retrouve
cette valeur après le verrou au lieu de recalculer. Le plafond ne change pas :
rien n'est publié, un autre processus repaie sa lecture.

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
import gzip
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import zlib
from collections import OrderedDict
from typing import Callable, Optional, Sequence

from . import canon

#: sous-dossier de l'état de l'hôte qui porte le cache
CACHE_DIR = "git-grep"
#: entrées gardées au plus (les plus anciennement lues retirées)
MAX_ENTRIES = 20000
#: octets persistés au plus ; les arbres complets peuvent faire plusieurs Mo
MAX_BYTES = 256 * 1024 * 1024
#: #4913 : une entrée plus grosse n'est pas publiée (sa valeur reste rendue à
#: l'appelant). Elle évincerait à elle seule des dizaines de lectures utiles et
#: ne tiendrait que quelques fois dans MAX_BYTES : sur release/11ag, l'entrée
#: d'arbre de 22 Mio était réécrite en boucle. Les arbres de `main` (~10 Mio)
#: restent gardés.
MAX_ENTRY_BYTES = 16 * 1024 * 1024
#: magasins entre deux élagages des petites entrées
PRUNE_EVERY = 256
#: au-delà, une écriture paie un élagage immédiat : c'est la taille des
#: arbres, seuls à pouvoir remplir le budget en quelques publications
PRUNE_LARGE_BYTES = 1024 * 1024
#: octets écrits par ce processus depuis le dernier élagage avant de le
#: refaire (les petites entrées amortissent le coût du balayage)
PRUNE_PENDING_BYTES = MAX_BYTES // 16
#: valeurs décodées gardées dans le processus, en octets encodés. 48 Mio
#: gardent deux entrées au-dessus de `MAX_ENTRY_BYTES` (21 Mio chacune sur
#: release/11ag) : sans la seconde, la lecture de l'arbre de fusion, réutilisée
#: par chaque commit, était évincée par celle, unique, de chaque parent et
#: relue à chaque appel (#4964).
MEMO_MAX_BYTES = 48 * 1024 * 1024
MEMO_MAX_ENTRIES = 64
#: niveau gzip : le niveau 4 tient 25 arbres de `main` dans 256 Mio
GZIP_LEVEL = 4
#: attente au plus du verrou d'une clé qu'un autre processus calcule (s)
LOCK_WAIT_S = canon.GIT_TIMEOUT
#: pas de l'attente du verrou (s)
LOCK_STEP_S = 0.1
#: version du format : un changement de format change toutes les clés
FORMAT = 4  # #4791: discard results from potentially incomplete blob reads
#: première version de git qui lit `GIT_ATTR_SOURCE` (absent de l'attr.c de
#: v2.40.0, présent en v2.41.0 : relevé cx2)
ATTR_SOURCE_GIT = (2, 41)
#: variable sans effet sur git qui porte l'empreinte de `info/attributes` dans
#: `env_extra`, donc dans la clé du cache mémoire de `canon._git` (#3753)
ATTR_TOKEN_ENV = "AMEESH_INFO_ATTRIBUTES"

#: interrupteur du processus (la suite de tests le met à False)
ENABLED = True

#: état du processus : racine courante, octets en attente d'élagage,
#: magasins depuis le dernier élagage, valeurs décodées (LRU) et compteurs.
#: La signature d'un mémo est `(inode, taille)` pour une entrée publiée et
#: `None` pour une valeur calculée mais trop grosse pour être publiée (#4964).
_STATE_LOCK = threading.RLock()
_STATE_ROOT: Optional[str] = None
_PENDING_BYTES = 0
_STORES = 0
_MEMO: "OrderedDict[str, tuple[Optional[tuple[int, int]], list, int]]" = OrderedDict()
_MEMO_BYTES = 0
_COUNTERS = {"reads": 0, "hits": 0, "memo_hits": 0, "misses": 0,
             "stores": 0, "prunes": 0, "pruned": 0, "oversized": 0,
             "lookups": 0, "served": 0, "deduped": 0, "computed": 0}


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


def _state() -> None:
    """Réinitialise l'état du processus si la racine du cache a changé."""
    global _STATE_ROOT, _PENDING_BYTES, _STORES, _MEMO_BYTES
    current = root()
    with _STATE_LOCK:
        if current == _STATE_ROOT:
            return
        _STATE_ROOT = current
        _PENDING_BYTES = 0
        _STORES = 0
        _MEMO.clear()
        _MEMO_BYTES = 0
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def _reset_process_state() -> None:
    """Vide l'état du processus (témoin des tests, qui changent de racine)."""
    global _STATE_ROOT, _PENDING_BYTES, _STORES, _MEMO_BYTES
    with _STATE_LOCK:
        _STATE_ROOT = root()           # la racine courante ne re-vide pas après
        _PENDING_BYTES = 0
        _STORES = 0
        _MEMO.clear()
        _MEMO_BYTES = 0
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def _count(name: str, amount: int = 1) -> None:
    with _STATE_LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + amount


def stats() -> dict:
    """Compteurs du processus (lectures, réussites, mémo, élagages).

    `lookups` compte les appels à `cached` qui ont interrogé le cache ;
    `served` ceux servis par une entrée déjà là, `deduped` ceux qu'un autre
    processus venait de publier, `computed` ceux qui ont balayé. Le taux de
    réussite du cache est `served / lookups`."""
    _state()
    with _STATE_LOCK:
        result = dict(_COUNTERS)
        result["memo_entries"] = len(_MEMO)
        result["memo_bytes"] = _MEMO_BYTES
    reads = result.get("reads") or 0
    result["hit_rate"] = (result.get("hits", 0) / reads) if reads else None
    lookups = result.get("lookups") or 0
    result["lookup_hit_rate"] = (result.get("served", 0) / lookups) if lookups else None
    return result


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


#: L'en-tête du fichier est un JSON court, suivi du corps. Les entrées de
#: chaînes (tous les appelants de production) sont des lignes jointes par
#: `\n`, sans JSON ; les autres valeurs gardent un JSON exact (contrat de
#: `cached`). Le tout est compressé : un arbre complet de 536 944 lignes passe
#: de 32,8 Mo à ~10 Mo, ce qui met ~25 arbres dans le budget de 256 Mio.
def _entry_parts(value: list):
    value = list(value)
    if all(isinstance(item, str) and "\n" not in item for item in value):
        meta = {"format": FORMAT, "kind": "lines"}
        body = "\n".join(value).encode("utf-8")
    else:
        meta = {"format": FORMAT, "kind": "json"}
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return json.dumps(meta, separators=(",", ":")).encode("ascii"), body


def _encode(value: list) -> bytes:
    """L'entrée encodée (témoin des tests ; l'écriture la produit en flux)."""
    meta, body = _entry_parts(value)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=GZIP_LEVEL, mtime=0,
                       filename="") as handle:
        handle.write(meta)
        handle.write(b"\n")
        handle.write(body)
    return buffer.getvalue()


def _write_entry(handle, value: list) -> None:
    meta, body = _entry_parts(value)
    with gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=GZIP_LEVEL, mtime=0,
                       filename="") as stream:
        stream.write(meta)
        stream.write(b"\n")
        stream.write(body)


def _decode(raw: bytes) -> list:
    meta_raw, separator, body = raw.partition(b"\n")
    if not separator:
        raise ValueError("cache entry without a header")
    meta = json.loads(meta_raw)
    if not isinstance(meta, dict) or meta.get("format") != FORMAT:
        raise ValueError("cache entry of another format")
    kind = meta.get("kind")
    if kind == "lines":
        return body.decode("utf-8").split("\n") if body else []
    if kind == "json":
        value = json.loads(body)
        if not isinstance(value, list):
            raise ValueError("cache entry is not a list")
        return value
    raise ValueError("cache entry of another kind")


def _touch(path: str) -> None:
    try:
        os.utime(path)                 # élagage : la plus anciennement lue part
    except OSError:
        pass


def _memo_get(name: str, signature: tuple) -> Optional[list]:
    with _STATE_LOCK:
        entry = _MEMO.get(name)
        if entry is None or entry[0] != signature:
            return None
        _MEMO.move_to_end(name)
        _COUNTERS["memo_hits"] += 1
        return list(entry[1])


def _memo_get_unpublished(name: str) -> Optional[list]:
    """La valeur calculée mais non publiée d'une entrée trop grosse (#4964).

    Elle n'a pas de fichier à statuer : la clé est content-addressed, donc sa
    valeur ne peut pas être périmée. `None` quand le mémo ne la garde pas."""
    with _STATE_LOCK:
        entry = _MEMO.get(name)
        if entry is None or entry[0] is not None:
            return None
        _MEMO.move_to_end(name)
        _COUNTERS["memo_hits"] += 1
        return list(entry[1])


def _memo_put(name: str, value: list, signature: Optional[tuple[int, int]],
              size: int) -> None:
    global _MEMO_BYTES
    if size > MEMO_MAX_BYTES:
        return
    with _STATE_LOCK:
        old = _MEMO.pop(name, None)
        if old is not None:
            _MEMO_BYTES -= old[2]
        _MEMO[name] = (signature, value, size)
        _MEMO_BYTES += size
        while _MEMO and (len(_MEMO) > MEMO_MAX_ENTRIES
                         or _MEMO_BYTES > MEMO_MAX_BYTES):
            _name, entry = _MEMO.popitem(last=False)
            _MEMO_BYTES -= entry[2]


def _memo_drop(name: str) -> None:
    global _MEMO_BYTES
    with _STATE_LOCK:
        old = _MEMO.pop(name, None)
        if old is not None:
            _MEMO_BYTES -= old[2]


def _read(name: str):
    """(True, valeur) si l'entrée est lisible, sinon (False, None).

    Une valeur non publiée (au-delà de `MAX_ENTRY_BYTES`) est servie depuis le
    mémo du processus (#4964) : `_write` la garde là avant de renoncer au
    fichier."""
    _state()
    _count("reads")
    path = _path(name)
    try:
        info = os.stat(path)
    except OSError:
        value = _memo_get_unpublished(name)
        if value is not None:
            _count("hits")
            return True, value
        _memo_drop(name)
        _count("misses")
        return False, None
    signature = (info.st_ino, info.st_size)
    value = _memo_get(name, signature)
    if value is not None:
        _touch(path)
        _count("hits")
        return True, value
    try:
        with gzip.open(path, "rb") as handle:
            raw = handle.read()
        value = _decode(raw)
    except (OSError, EOFError, ValueError, zlib.error):
        _count("misses")
        return False, None
    _memo_put(name, value, signature, info.st_size)
    _touch(path)
    _count("hits")
    return True, list(value)           # le mémo garde l'original


def _write(name: str, value: list) -> None:
    global _PENDING_BYTES, _STORES
    _state()
    path = _path(name)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                _write_entry(handle, value)
            size = os.path.getsize(tmp)
            if size > MAX_ENTRY_BYTES:
                os.unlink(tmp)
                _count("oversized")
                # #4964 : non publiée, mais mémoïsée dans le processus. Le
                # prochain appel de la même clé (ou l'appelant qui attendait le
                # verrou) la retrouve, au lieu de relire l'arbre entier.
                _memo_put(name, list(value), None, size)
                return
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return                         # cache best-effort : la lecture vaut
    try:
        info = os.stat(path)
    except OSError:
        info = None
    if info is not None:
        _memo_put(name, list(value), (info.st_ino, info.st_size), info.st_size)
    size = info.st_size if info is not None else 0
    with _STATE_LOCK:
        _STORES += 1
        _PENDING_BYTES += size
        _COUNTERS["stores"] += 1
        pending = _PENDING_BYTES
        stores = _STORES
    # #4482 (N3) : l'élagage (0,14-0,31 s à 20 000 entrées) n'est plus payé
    # par chaque petite publication. Une grosse entrée — un arbre — l'applique
    # aussitôt ; les petites l'amortissent, et les octets en attente bornent
    # ce qu'elles peuvent ajouter entre deux élagages.
    if (size >= PRUNE_LARGE_BYTES
            or size > MAX_BYTES
            or pending >= min(PRUNE_PENDING_BYTES, MAX_BYTES)
            or stores % PRUNE_EVERY == 0):
        prune()
        with _STATE_LOCK:
            _PENDING_BYTES = 0


def _prune_entries(limit: int, budget: int):
    """Élague ; rend (retirées, octets restants, entrées restantes)."""
    entries = []
    base = root()
    try:
        with os.scandir(base) as shards:
            shards = list(shards)
    except OSError:
        return 0, 0, 0
    for shard in shards:
        try:
            if not shard.is_dir():
                continue
        except OSError:
            continue
        try:
            with os.scandir(shard.path) as names:
                names = list(names)
        except OSError:
            continue
        for entry in names:
            if not entry.name.endswith(".json"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append((info.st_mtime, entry.path, entry.name,
                            info.st_size))
    total = sum(size for _mtime, _path, _name, size in entries)
    count = len(entries)
    removed = 0
    for _mtime, full, name, size in sorted(entries):
        if count <= limit and total <= budget:
            break
        try:
            os.unlink(full)
            removed += 1
        except FileNotFoundError:
            pass                       # déjà retiré par un autre processus
        except OSError:
            continue                   # pas retiré : ses octets comptent encore
        _memo_drop(name[:-len(".json")])
        count -= 1
        total -= size
    return removed, total, count


def prune(limit: int = MAX_ENTRIES, max_bytes: Optional[int] = None) -> int:
    """Élague les plus anciennes lectures jusqu’aux bornes de nombre/octets.

    Une entrée plus grosse que le budget est retirée elle aussi. Les fichiers
    de verrou et temporaires ne sont jamais des entrées à élaguer.
    """
    budget = MAX_BYTES if max_bytes is None else max(0, max_bytes)
    removed, _total, _count = _prune_entries(max(0, int(limit)), budget)
    with _STATE_LOCK:
        _COUNTERS["prunes"] += 1
        _COUNTERS["pruned"] += removed
    return removed


#: tranches de verrou (préfixe hexadécimal de la clé) : des fichiers stables
#: et en nombre borné, jamais supprimés (B/P2 cx3 sur bdd1baf9 : supprimer le
#: fichier à la sortie laissait un attendant sur l'ancien inode et un nouveau
#: venu sur le nouveau, deux verrous exclusifs pour la même clé)
LOCK_STRIPE_CHARS = 3
LOCK_DIR = "locks"


class _KeyLock:
    """Verrou consultatif d'une clé, pris au mieux dans `LOCK_WAIT_S`.

    Le fichier de verrou est celui de la tranche de la clé : il n'est jamais
    supprimé, donc tous les processus verrouillent le même inode."""

    def __init__(self, name: str, wait: float):
        self.path = os.path.join(root(), LOCK_DIR,
                                 name[:LOCK_STRIPE_CHARS] + ".lock")
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

    @property
    def held(self) -> bool:
        return self.fd is not None

    def __exit__(self, *_exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
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
    _state()
    _count("lookups")
    found, value = _read(name)
    if found:
        _count("served")
        return value
    with _KeyLock(name, LOCK_WAIT_S if wait is None else wait):
        found, value = _read(name)     # un autre processus a pu le poser
        if found:
            _count("deduped")
            return value
        value = compute()
        _count("computed")
        if value is not None:
            _write(name, list(value))
    return value
