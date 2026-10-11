# SPDX-License-Identifier: AGPL-3.0-only
"""Ménage de ce que les agents créent (lot L73 ; décision 0028 point 4 ; lot
de base #1, cycle de vie des worktrees).

ameesh fait le ménage de ce que SES agents créent, au lieu de le laisser à
l'humain. Quatre familles, quatre règles :

* **Dossier temporaire d'un agent** (jetable) : `TMPDIR`/`TMP`/`TEMP` d'un
  tour pointent vers `<tmp_root>/<agent>` (par défaut
  `<état>/.menage/tmp/<agent>`, sur disque : pas un tmpfs en mémoire). Il vit
  le temps d'une SESSION, pas d'un tour : une session reprise retrouve ses
  fichiers (bloc-notes du harnais, sorties de tests) d'un tour à l'autre. Il
  est vidé au premier tour d'une session neuve (rotation, changement de lot,
  `restart`), borné par un quota (éviction des fichiers les plus anciens à la
  fin de chaque tour) et supprimé quand l'agent n'existe plus au registre —
  L219 : seulement s'il appartient à CE mesh (fichier `.<agent>.owner`),
  sans tour en cours (verrou `.<agent>.lock`, bail vivant) ni activité depuis
  `orphan_idle` ; un homonyme d'un autre mesh a son propre dossier.
* **Caches partagés de l'hôte** (réutilisables) : `npm_config_cache`, store
  pnpm (`npm_config_store_dir`/`pnpm_config_store_dir`), `YARN_CACHE_FOLDER`
  — et, sur demande de la politique, `XDG_CACHE_HOME` — pointent vers
  `<cache_root>/<nom>`. Partagés entre agents (un store pnpm par agent
  multiplierait le disque), bornés par un quota global, évincés du plus
  ancien au plus récent, et jamais pendant un tour de l'hôte (un store pnpm
  modifié sous une installation casse l'installation). Une variable déjà
  posée dans l'environnement de l'exécuteur est respectée : c'est un choix
  de l'hôte.
* **Worktrees** : ceux qui APPARAISSENT pendant un tour (diff de `git
  worktree list` avant/après, sous-agents compris) sont enregistrés avec
  l'agent, le tour et le lot — s'ils sont sous le dossier de travail de
  l'agent ; ceux d'ailleurs (un autre agent qui travaille sur le même dépôt)
  sont suivis « non attribués » et jamais retirés (L73b). À la fin du lot
  (fusion ou fermeture), ils sont retirés par `git worktree remove` (jamais
  forcé) s'ils sont propres, non verrouillés, âgés d'au moins une heure,
  sans tour en cours de leur agent ni processus dedans, et que leur HEAD est
  INTÉGRÉ à la branche principale distante (ancêtre, rebase, fusion écrasée :
  `git cherry`, `git merge-tree`) ; sinon GARDÉS et signalés (`worktree_kept`) au
  responsable.

* **Entrées de `/tmp` laissées par un tour** (L162 : clone de relecture,
  `--cacheDirectory` explicite, image `docker save`) : apparues pendant le
  tour d'un agent, elles sont signalées (`tmp_orphan`) avec la commande à
  lancer ; une entrée signalée est SUPPRIMÉE par le passage périodique si
  elle est un enfant direct du `/tmp` du système, à l'utilisateur, la même
  que celle signalée (inode), attribuée à un agent du registre sans tour en
  cours, rien dedans n'a changé depuis `orphan_idle` (2 h, mtime ET ctime),
  aucun processus ne la tient, et aucune copie git dedans n'a de travail non
  commité ou non poussé (`orphan_cleanup: false` : jamais, par hôte).

Ce qu'ameesh n'a pas créé ni vu apparaître pendant un tour n'est JAMAIS
supprimé automatiquement. Toute suppression dans un dossier géré passe par
[`_safe_rmtree`], qui refuse un chemin hors des dossiers gérés, un lien
symbolique, ou une racine sans marqueur `.ameesh-menage` ; dans `/tmp`, par
[`_remove_tmp_entry`], qui revérifie l'entrée juste avant.

Les dossiers gérés ne sont jamais en mémoire (L162) : une racine déclarée sur
un tmpfs est écartée au profit de `<état>/.menage/…`.

Tout est journalisé (`housekeeping_log`, migration 0045) et visible dans
`ameesh hosts` ; `ameesh menage [--apply]` montre ou déclenche un passage.
"""
from __future__ import annotations

import getpass
import json
import os
import shlex
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field

from . import platform
from . import resources as resources_mod
from . import storage

GIB = 1024 ** 3
MIB = 1024 ** 2

#: valeurs par défaut de `policy.housekeeping` (fiche Host)
DEFAULT_TMP_QUOTA = 4 * GIB          # par agent
DEFAULT_CACHE_QUOTA = 10 * GIB       # tous caches partagés de l'hôte
DEFAULT_TMPFS_ALERT = 0.6            # alerte (la contre-pression est à 0,8)
DEFAULT_ORPHAN_MIN_SIZE = 50 * MIB   # entrée de /tmp signalée au-delà
#: âge minimal d'un fichier évincé (secondes) : jamais un fichier en écriture
EVICT_MIN_AGE_S = 3600.0
#: âge au-delà duquel un worktree SANS lot est retirable par `menage --apply`
LOTLESS_TTL_S = 7 * 24 * 3600.0
#: âge minimal d'un worktree retiré (secondes, depuis son enregistrement ET
#: depuis la dernière modification de son dossier) : jamais un worktree neuf
WORKTREE_MIN_AGE_S = 3600.0
#: préfixe du détail d'un worktree apparu pendant un tour mais pas créé par
#: l'agent du tour (L73b) : suivi pour être vu, jamais retiré automatiquement
UNATTRIBUTED = "non attribué"
#: borne d'un parcours de taille (fichiers) : au-delà, la taille est un minimum
WALK_LIMIT = 300_000
#: délai maximal d'une commande git (secondes)
GIT_TIMEOUT = 20.0
#: L162 : inactivité (secondes) au-delà de laquelle une entrée de /tmp laissée
#: par un tour est supprimée (`policy.housekeeping.orphan_idle`)
DEFAULT_ORPHAN_IDLE_S = 2 * 3600.0
#: L162 : horizon des signalements relus par le passage (le journal en garde 30 j)
ORPHAN_LOOKBACK_S = 7 * 24 * 3600.0
#: #222: an unattributed /tmp entry, never deleted, is signaled again by the
#: periodic pass once its last line is this old, so that it stays in the 24 h
#: window of the alerts for as long as it exists
UNATTRIBUTED_REFRESH_S = 12 * 3600.0
#: L162 : nombre de plus grosses entrées de /tmp dites quand le tmpfs se remplit
TMP_TOP = 5
#: L162 : systèmes de fichiers en mémoire — jamais une racine gérée
MEMORY_FS = ("tmpfs", "ramfs")
#: L162 : l'action attendue quand le tmpfs se remplit (alertes, journal)
#: #234: since #222 the housekeeping deletes by itself only a signal attributed
#: before #222; a new /tmp entry is never charged to a turn nor deleted
TMPFS_ACTION = ("action attendue : supprimer ce qui ne sert plus (commande par entrée "
                "dans `ameesh menage` ou dans l'alerte `tmp_orphan`) ; le ménage ne "
                "supprime seul qu'une entrée signalée attribuée d'avant #222, inchangée "
                "depuis %s et tenue par aucun processus ; une entrée apparue depuis n'est "
                "jamais supprimée par ameesh ; les agents écrivent dans $TMPDIR, jamais "
                "en dur dans /tmp")

#: dossier géré sous l'état d'ameesh (un nom d'agent ne commence jamais par
#: un point : pas de collision avec `<état>/<agent>`)
MANAGED_DIR = ".menage"
#: marqueur posé par ameesh à la racine d'un dossier qu'il gère
MARKER = ".ameesh-menage"
#: L219: owner file of an agent's temporary directory, next to it in the tmp
#: root (`.<agent>.owner`, never inside: the start of a session empties the
#: directory and the quota evicts its oldest files). It names the mesh that
#: created the directory (`mesh_key`).
OWNER_SUFFIX = ".owner"
#: L219: lock file of an agent's temporary directory, next to it (`flock`)
LOCK_SUFFIX = ".lock"
#: L219: directory of an agent whose homonym of another mesh owns
#: `<agent>`: `<agent>~<first ALT_LEN characters of the mesh key>`
ALT_SEP, ALT_LEN = "~", 8
#: L219: a directory without owner file (another mesh, or before L219) is
#: only touched after this long without any activity
UNOWNED_IDLE_S = 24 * 3600.0
#: L219: origin written in the owner file (after the key)
ORIGIN_CREATED, ORIGIN_ADOPTED = "created", "adopted"
#: L219: fallback directory of a turn that could not lock its own:
#: `<name>+<random>`, used once, deleted by the pass once idle
FALLBACK_SEP = "+"
#: L219: longest wait for the lock of a temporary directory at a turn start
#: (a pass holds it while deleting); past it the turn gets a fallback
#: directory. Emptying (exclusive) waits less: a live session of the agent
#: (`attach`) holds it shared, and then the directory is not emptied
LOCK_WAIT_S, CLEAR_WAIT_S = 30.0, 5.0
#: L219: fallback names tried before giving up
FALLBACK_TRIES = 5

#: caches partagés : nom du sous-dossier → variables d'environnement
CACHES = (
    ("npm", ("npm_config_cache",)),
    ("pnpm", ("npm_config_store_dir", "pnpm_config_store_dir")),
    ("yarn", ("YARN_CACHE_FOLDER",)),
)
XDG_CACHE = ("xdg", ("XDG_CACHE_HOME",))

#: états de lot qui terminent le cycle de vie des worktrees du lot
LOT_ENDED = ("merged", "promoted", "closed")

POLICY_KEYS = ("tmp_root", "cache_root", "tmp_quota", "cache_quota", "tmpfs_alert",
               "redirect", "xdg_cache", "worktrees", "orphan_min_size",
               "orphan_cleanup", "orphan_idle")
#: racines refusées : le ménage n'y supprimerait que ses sous-dossiers, mais
#: un dossier partagé par tout le système n'est pas un dossier GÉRÉ
FORBIDDEN_ROOTS = ("/", "~", "/tmp", "/var/tmp", "/dev/shm", "/home", "/root")


# --------------------------------------------------------------------------
# politique (fiche Host, `policy.housekeeping`)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    tmp_root: str | None = None
    cache_root: str | None = None
    tmp_quota: int = DEFAULT_TMP_QUOTA
    cache_quota: int = DEFAULT_CACHE_QUOTA
    tmpfs_alert: float = DEFAULT_TMPFS_ALERT
    redirect: bool = True
    xdg_cache: bool = False
    worktrees: bool = True
    orphan_min_size: int = DEFAULT_ORPHAN_MIN_SIZE
    #: L162 : supprimer les entrées de /tmp abandonnées par un tour
    orphan_cleanup: bool = True
    #: L162 : inactivité (secondes) avant cette suppression
    orphan_idle: float = DEFAULT_ORPHAN_IDLE_S
    #: provenance de chaque clé déclarée (identifiant du canon)
    origin: dict = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in POLICY_KEYS}


def _path_problem(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "chemin vide"
    text = value.strip()
    if "{" in text or "}" in text:
        return "gabarit non admis"
    if not (text.startswith("/") or text.startswith("~")):
        return "chemin absolu (ou ~) attendu"
    if os.path.normpath(text) in FORBIDDEN_ROOTS or text.rstrip("/") in FORBIDDEN_ROOTS:
        return "dossier partagé par le système (%s) : choisir un sous-dossier dédié" % text
    return None


def parse_policy(raw) -> tuple[dict, list[str], list[str]]:
    """(clés valides, problèmes, clés inconnues) de `policy.housekeeping`."""
    if not isinstance(raw, dict):
        return {}, ["mapping attendu"], []
    parsed: dict = {}
    problems: list[str] = []
    for key in ("tmp_root", "cache_root"):
        if raw.get(key) is None:
            continue
        problem = _path_problem(raw[key])
        if problem:
            problems.append("`%s` : %s" % (key, problem))
        else:
            parsed[key] = raw[key].strip()
    for key in ("tmp_quota", "cache_quota", "orphan_min_size"):
        if raw.get(key) is None:
            continue
        octets = resources_mod.parse_bytes(raw[key])
        if octets is None:
            problems.append("`%s` : taille en octets attendue (ex. 4GiB)" % key)
        else:
            parsed[key] = octets
    if raw.get("tmpfs_alert") is not None:
        part = resources_mod.parse_fraction(raw["tmpfs_alert"])
        if part is None:
            problems.append("`tmpfs_alert` : part attendue (0 < x <= 1, ou « 60% »)")
        else:
            parsed["tmpfs_alert"] = part
    if raw.get("orphan_idle") is not None:
        seconds = parse_idle(raw["orphan_idle"])
        if seconds is None:
            problems.append("`orphan_idle` : durée attendue (ex. 2h, 90m, 7200)")
        else:
            parsed["orphan_idle"] = seconds
    for key in ("redirect", "xdg_cache", "worktrees", "orphan_cleanup"):
        if raw.get(key) is None:
            continue
        if not isinstance(raw[key], bool):
            problems.append("`%s` : booléen attendu" % key)
        else:
            parsed[key] = raw[key]
    unknown = sorted(str(k) for k in set(raw) - set(POLICY_KEYS))
    return parsed, problems, unknown


def parse_idle(value) -> float | None:
    """Durée d'inactivité (L162) : secondes (nombre), ou texte « 2h », « 90m »,
    « 1d » ; None si illisible ou non positive."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    from . import stagnation  # import tardif : pas de cycle
    try:
        return stagnation.parse_duration(str(value))
    except ValueError:
        return None


def effective(canons, host: str) -> Policy:
    """Politique de ménage de `host` d'après toutes les fiches Host des canons.

    Chemins et booléens : la première fiche qui les déclare (canon par défaut
    d'abord). Quotas, taille de signalement et seuil d'alerte : le plus strict
    (le plus bas), comme les limites physiques de L43. L162 : la suppression
    des entrées de /tmp abandonnées est coupée dès qu'UNE fiche la coupe, et
    l'inactivité exigée est la plus longue."""
    values: dict = {}
    origin: dict = {}
    for canon in canons or ():
        fiche = canon.host(host) if hasattr(canon, "host") else None
        declared = getattr(getattr(fiche, "policy", None), "housekeeping", None) or {}
        label = getattr(canon, "id", "") or "?"
        for key, value in declared.items():
            if key not in POLICY_KEYS:
                continue
            if key not in values:
                values[key], origin[key] = value, label
            elif key in ("tmp_quota", "cache_quota", "orphan_min_size", "tmpfs_alert") \
                    and value < values[key]:
                values[key], origin[key] = value, label
            elif (key == "orphan_idle" and value > values[key]) or \
                    (key == "orphan_cleanup" and value is False and values[key] is not False):
                values[key], origin[key] = value, label
    return Policy(origin=origin, **values)


def disabled(env=None) -> bool:
    """`AMEESH_HOUSEKEEPING=off` : ni redirection, ni ménage (échappatoire)."""
    value = (env if env is not None else os.environ).get("AMEESH_HOUSEKEEPING", "")
    return value.strip().lower() in ("off", "0", "none", "non")


# --------------------------------------------------------------------------
# dossiers gérés
# --------------------------------------------------------------------------

def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def in_memory(path: str) -> bool:
    """`path` (existant ou non) est-il sur un système de fichiers en mémoire
    (tmpfs) ? Faux si on ne sait pas le dire (L162)."""
    try:
        mount = platform.mount_of(path)
    except Exception:  # lecture des montages impossible : on ne devine pas
        return False
    return bool(mount) and mount[1] in MEMORY_FS


def _disk_root(declared: str | None, default: str) -> str:
    """La racine déclarée, sauf si elle est en mémoire alors que le défaut
    (sous l'état d'ameesh) ne l'est pas : un dossier géré vit sur disque (L162,
    le /tmp en tmpfs de 12 Go plein le 2026-10-11 a bloqué tous les tours)."""
    if not declared:
        return default
    chosen = _expand(declared)
    if in_memory(chosen) and not in_memory(default):
        return default
    return chosen


def declared_root_refused(cfg, policy: Policy) -> list[str]:
    """Les racines déclarées écartées parce qu'en mémoire (pour le rapport)."""
    out = []
    for key, effective_root in (("tmp_root", tmp_root(cfg, policy)),
                                ("cache_root", cache_root(cfg, policy))):
        declared = getattr(policy, key)
        if declared and _expand(declared) != effective_root:
            out.append("%s %s en mémoire (tmpfs) : écarté, %s utilisé"
                       % (key, declared, effective_root))
    return out


def tmp_root(cfg, policy: Policy) -> str:
    return _disk_root(policy.tmp_root, os.path.join(cfg.state_dir, MANAGED_DIR, "tmp"))


def cache_root(cfg, policy: Policy) -> str:
    return _disk_root(policy.cache_root, os.path.join(cfg.state_dir, MANAGED_DIR, "cache"))


def agent_tmp(cfg, policy: Policy, agent: str) -> str:
    """The agent's temporary directory for THIS mesh (L219: `tmp_name`)."""
    troot = tmp_root(cfg, policy)
    return os.path.join(troot, tmp_name(troot, agent, mesh_key(cfg)))


def cache_names(policy: Policy) -> list[tuple[str, tuple[str, ...]]]:
    return list(CACHES) + ([XDG_CACHE] if policy.xdg_cache else [])


def _conninfo_fields(dsn: str) -> dict[str, str]:
    """The fields of a DSN, URI or key=value, following libpq's rules (quotes
    and backslashes, percent-encoding, query parameters, several hosts). Pure
    Python: the psql driver runs without psycopg, and the key must not depend
    on the driver."""
    from urllib.parse import parse_qsl, unquote

    fields: dict[str, str] = {}
    if re.match(r"postgres(?:ql)?://", dsn):
        rest = dsn.split("://", 1)[1]
        rest, _, query = rest.partition("?")
        netloc, _, path = rest.partition("/")
        netloc = netloc.rpartition("@")[2]
        hosts, ports = [], []
        for item in netloc.split(","):
            m = re.fullmatch(r"(\[[^\]]*\]|[^:]*)(?::(.*))?", item)
            hosts.append(unquote(m.group(1).strip("[]")))
            ports.append(unquote(m.group(2) or ""))
        if any(hosts):
            fields["host"] = ",".join(hosts)
        if any(ports):
            fields["port"] = ",".join(ports)
        if path:
            fields["dbname"] = unquote(path)
        fields.update(parse_qsl(query, keep_blank_values=True))
        return fields
    pos, n = 0, len(dsn)
    while True:
        while pos < n and dsn[pos].isspace():
            pos += 1
        if pos >= n:
            return fields
        m = re.compile(r"([^=\s]+)\s*=\s*").match(dsn, pos)
        if not m:
            return fields
        pos, value, quoted = m.end(), [], False
        if pos < n and dsn[pos] == "'":
            pos, quoted = pos + 1, True
        while pos < n:
            ch = dsn[pos]
            if ch == "\\" and pos + 1 < n:
                value.append(dsn[pos + 1])
                pos += 2
                continue
            if (quoted and ch == "'") or (not quoted and ch.isspace()):
                pos += 1
                break
            value.append(ch)
            pos += 1
        fields[m.group(1)] = "".join(value)


def _dsn_where(dsn: str) -> str:
    """Where a DSN points (host, hostaddr, port, dbname), whatever its form
    (URI, key=value, query parameters)."""
    fields = _conninfo_fields(dsn)
    # host names are case-insensitive, socket paths are not
    host = ",".join(h if h.startswith(("/", "@")) else h.lower()
                    for h in (fields.get("host") or "").split(","))
    return "%s|%s|%s|%s" % (host, fields.get("hostaddr") or "",
                            fields.get("port") or "5432", fields.get("dbname") or "")


def mesh_key(cfg) -> str:
    """L219: identity of the mesh (database and schema) shared by its runners:
    a hash of host, hostaddr, port and database of the DSN (never user,
    password or options) and of the schema. Two meshes of one host that share the state
    directory have two keys."""
    import hashlib

    where = _dsn_where(str(getattr(cfg, "dsn", "") or ""))
    text = "%s|%s" % (where, getattr(cfg, "schema", "") or "public")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def owner_file(troot: str, agent: str) -> str:
    return os.path.join(troot, ".%s%s" % (agent, OWNER_SUFFIX))


def lock_file(troot: str, name: str) -> str:
    return os.path.join(troot, ".%s%s" % (name, LOCK_SUFFIX))


def _owner_lines(troot: str, name: str) -> list[str]:
    try:
        with open(owner_file(troot, name), encoding="utf-8") as fh:
            return fh.read().split()
    except OSError:
        return []


def read_owner(troot: str, name: str) -> str | None:
    """The mesh key written for the temporary directory `name`, or None."""
    lines = _owner_lines(troot, name)
    return lines[0] if lines else None


def created_by(troot: str, name: str, key: str) -> bool:
    """L219: true if the mesh `key` CREATED the directory `name` (owner file
    `<key> created`). One it adopted (found without owner file) is never
    emptied by a new session: it may hold another mesh's files."""
    return _owner_lines(troot, name)[:2] == [key, ORIGIN_CREATED]


def _write_owner(troot: str, name: str, key: str, origin: str) -> None:
    path = owner_file(troot, name)
    tmp = "%s.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (key, origin))
    os.replace(tmp, path)


def tmp_name(troot: str, agent: str, key: str) -> str:
    """L219: name of `agent`'s temporary directory in `troot` for the mesh
    `key`: the agent's name, unless a homonymous agent of another mesh owns
    it — then `<agent>~<key prefix>` (never shared, never taken over)."""
    owner = read_owner(troot, agent)
    if owner is None or owner == key:
        return agent
    return "%s%s%s" % (agent, ALT_SEP, key[:ALT_LEN])


def tmp_agent(name: str, key: str) -> str:
    """The agent of a directory name of this mesh (`tmp_name` reversed,
    fallback suffix included)."""
    name = name.split(FALLBACK_SEP, 1)[0]
    suffix = "%s%s" % (ALT_SEP, key[:ALT_LEN])
    return name[:-len(suffix)] if name.endswith(suffix) else name


def tmp_lock(troot: str, name: str, *, exclusive: bool, timeout: float = 0.0):
    """L219: lock of the temporary directory `name` (flock on a file NEXT to
    it), shared during a turn, exclusive to empty, evict or delete it.
    Returns the open file (closing it releases the lock), None if not obtained within
    `timeout` seconds. The lock file is checked to be still the one at its
    path once locked (a deletion may have unlinked it meanwhile)."""
    import fcntl

    path = lock_file(troot, name)
    deadline = time.monotonic() + max(0.0, timeout)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        try:
            fh = open(path, "a+")
        except OSError:
            return None
        try:
            fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
            continue
        try:
            same = os.fstat(fh.fileno()).st_ino == os.stat(path).st_ino
        except OSError:
            same = False
        if same:
            return fh
        fh.close()


def last_activity(path: str) -> float | None:
    """Latest mtime or ctime (an extraction keeps old mtimes) of `path` and
    of everything under it, at any depth; None if the walk is incomplete
    (unreadable entry, mount point, too many entries): what was not seen is
    never judged idle."""
    scan = scan_activity(path)
    return float(scan["latest"]) if scan["complete"] and scan["latest"] is not None else None


def _ensure_root(root: str) -> None:
    os.makedirs(root, mode=0o700, exist_ok=True)
    marker = os.path.join(root, MARKER)
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("dossier géré par ameesh (L73) : son contenu peut être supprimé "
                     "par le ménage\n")


def _managed(root: str) -> bool:
    return os.path.isfile(os.path.join(root, MARKER)) and not os.path.islink(root)


def _shims_wanted(cfg) -> bool:
    from . import containers as containers_mod
    return containers_mod.runtime_binary(getattr(cfg, "container_runtime", "none") or "none") \
        is not None


@dataclass
class TmpHold:
    """L219: an agent's temporary directory held for a turn or a session:
    its path, and the shared lock (`tmp_lock`) to close at the end."""

    path: str
    lock: object
    entries: list = field(default_factory=list)

    def release(self) -> None:
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def exclusive_now(self) -> bool:
        """Turn the shared lock into the exclusive one without waiting: false
        if another holder of the shared lock remains (a runner taking over
        the lease). On failure the lock may be lost (flock converts by
        releasing first): only for the end of the hold."""
        import fcntl

        if self.lock is None:
            return False
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True


def hold_tmp(cfg, policy: Policy, agent: str, *, new_session: bool) -> TmpHold:
    """L219: the agent's temporary directory, locked (shared) for a turn.

    A new session empties it under the exclusive lock, and only if THIS mesh
    created it (never a homonym's of another mesh, never one adopted without
    owner file). The shared lock only waits for a pass deleting or evicting
    (exclusive, the time of an rm); if it cannot be had within
    `LOCK_WAIT_S`, the turn gets a fresh fallback directory, never an
    unlocked one (fallback names are tried `FALLBACK_TRIES` times, then
    OSError). The owner file is only written under the exclusive lock, and
    the name and owner are checked again once the lock is held; the lock is
    then turned into the shared one."""
    import fcntl

    troot, key = tmp_root(cfg, policy), mesh_key(cfg)
    _ensure_root(troot)
    entries: list = []
    lock, name, exclusive = None, "", False
    for _attempt in range(3):          # a homonym of another mesh may claim it meanwhile
        name = tmp_name(troot, agent, key)
        # the owner is only written under the exclusive lock: two meshes
        # starting a homonym at once never both see it free
        claim = new_session or read_owner(troot, name) is None
        lock = tmp_lock(troot, name, exclusive=True, timeout=CLEAR_WAIT_S) if claim else None
        exclusive = lock is not None
        if lock is None:
            lock = tmp_lock(troot, name, exclusive=False, timeout=LOCK_WAIT_S)
        if lock is None:
            break
        # checked again under the lock: the name is still ours, and an
        # unowned one is only taken under the exclusive lock
        if tmp_name(troot, agent, key) == name \
                and (exclusive or read_owner(troot, name) is not None):
            break
        lock.close()
        lock = None
    if lock is None:
        import secrets
        base = name
        for _attempt in range(FALLBACK_TRIES):
            name = "%s%s%s" % (base, FALLBACK_SEP, secrets.token_hex(3))
            lock = tmp_lock(troot, name, exclusive=True)
            if lock is not None and read_owner(troot, name) is None:
                break
            if lock is not None:
                lock.close()
                lock = None
        if lock is None:
            raise OSError("L219 : aucun dossier temporaire verrouillable pour %s dans %s"
                          % (agent, troot))
        exclusive = True
    mine = os.path.join(troot, name)
    created = not os.path.isdir(mine)
    os.makedirs(mine, mode=0o700, exist_ok=True)
    if read_owner(troot, name) is None:            # under the exclusive lock (above)
        _write_owner(troot, name, key, ORIGIN_CREATED if created else ORIGIN_ADOPTED)
    elif new_session and not created and exclusive and created_by(troot, name, key):
        freed = _clear_dir(mine, troot)
        if freed:
            entries.append({"kind": "tmp", "action": "deleted", "path": mine,
                            "bytes": freed, "agent": agent,
                            "detail": "session neuve : dossier temporaire vidé"})
    fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
    # a resumed session writes nothing at first: the pass must not see it idle
    try:
        os.utime(mine)
    except OSError:
        pass
    return TmpHold(path=mine, lock=lock, entries=entries)


def turn_env(cfg, policy: Policy, agent: str, base_env: dict, *,
             hold: TmpHold | None = None) -> dict:
    """Variables à poser dans l'environnement d'un tour (crée les dossiers).

    `TMPDIR`/`TMP`/`TEMP` sont toujours redirigés (c'est l'objet du lot) vers
    le dossier de l'agent, SUR DISQUE (L162 : une racine déclarée en tmpfs est
    écartée) : le harnais et tous ses enfants en héritent — Node
    (`os.tmpdir()`, donc le cache de Jest), Python, Go, Rust, et le client
    docker/podman (podman y copie ses images). Une variable de cache déjà
    posée dans `base_env` est respectée. L219 : le dossier est celui de
    `hold` (verrouillé pour le tour) ; sans `hold`, il est pris puis relâché
    aussitôt (préparation, tests)."""
    if not policy.redirect or disabled(base_env):
        return {}
    if hold is None:
        hold = hold_tmp(cfg, policy, agent, new_session=False)
        hold.release()
    croot = cache_root(cfg, policy)
    _ensure_root(croot)
    mine = hold.path
    out = {"TMPDIR": mine, "TMP": mine, "TEMP": mine,
           "AMEESH_TMPDIR": mine, "AMEESH_CACHE_DIR": croot}
    for name, variables in cache_names(policy):
        if any(base_env.get(v) for v in variables):
            continue
        directory = os.path.join(croot, name)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        for variable in variables:
            out[variable] = directory
    if _shims_wanted(cfg):
        # L73 : `docker|podman run|create` d'un tour portent ses étiquettes
        from . import containers as containers_mod
        out.update(containers_mod.install_shims(
            os.path.join(cfg.state_dir, MANAGED_DIR, "bin"),
            base_env.get("PATH") or os.defpath))
    return out


# --------------------------------------------------------------------------
# tailles, éviction et suppression sûre
# --------------------------------------------------------------------------

def _files(top: str, limit: int = WALK_LIMIT):
    """(chemin, taille, mtime) des fichiers sous `top`, sans suivre les liens
    ni changer de système de fichiers ; s'arrête à `limit` fichiers."""
    try:
        device = os.lstat(top).st_dev
    except OSError:
        return
    count = 0
    stack = [top]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev == device:
                            stack.append(entry.path)
                        continue
                    count += 1
                    yield entry.path, st.st_blocks * 512 if st.st_blocks else st.st_size, \
                        st.st_mtime
                    if count >= limit:
                        return
        except OSError:
            continue


def size_of(path: str, limit: int = WALK_LIMIT) -> int:
    """Octets occupés sous `path` (fichier ou dossier), bornés à `limit`
    fichiers ; 0 si illisible."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512 if st.st_blocks else st.st_size
    return sum(size for _p, size, _m in _files(path, limit))


def _located_in(path: str, root: str) -> bool:
    """Vrai si `path` lui-même (pas sa cible, s'il est un lien) est sous
    `root`, sans être `root` ni son marqueur."""
    real_root = os.path.realpath(root).rstrip("/") or "/"
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    name = os.path.basename(os.path.abspath(path))
    if not name or name in (".", "..") or (parent == real_root and name == MARKER):
        return False
    return parent == real_root or parent.startswith(real_root + "/")


class RefusedDeletion(RuntimeError):
    """Suppression refusée : hors des dossiers gérés."""


def _safe_rmtree(path: str, root: str) -> int:
    """Supprime `path` s'il est DANS une racine gérée (marqueur présent) ;
    rend les octets libérés. Un lien symbolique est retiré, jamais suivi ;
    jamais la racine elle-même ni son marqueur, jamais un chemin hors d'elle."""
    if not _managed(root):
        raise RefusedDeletion("%s n'est pas un dossier géré par ameesh" % root)
    if not _located_in(path, root):
        raise RefusedDeletion("%s est hors de %s" % (path, root))
    size = size_of(path)
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path, ignore_errors=True)
    return size


def _clear_dir(directory: str, root: str) -> int:
    """Vide `directory` (géré) sans le supprimer ; rend les octets libérés."""
    freed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        try:
            freed += _safe_rmtree(os.path.join(directory, name), root)
        except (OSError, RefusedDeletion):
            continue
    return freed


def _prune_empty_dirs(top: str) -> None:
    for current, dirs, files in os.walk(top, topdown=False):
        if current != top and not dirs and not files:
            try:
                os.rmdir(current)
            except OSError:
                pass


def evict_lru(directories: list[str], root: str, quota: int, *, now: float | None = None,
              min_age: float = EVICT_MIN_AGE_S, dry_run: bool = False) -> dict:
    """Éviction du plus ancien (mtime) au plus récent, jusqu'au quota.

    Un fichier modifié depuis moins de `min_age` n'est jamais évincé (il est
    peut-être en écriture) : le quota peut donc rester dépassé, et c'est dit
    (`over_quota`). Rend `{before, after, evicted, files, over_quota}`."""
    now = time.time() if now is None else float(now)
    found = []
    for directory in directories:
        if os.path.isdir(directory) and not os.path.islink(directory):
            found.extend(_files(directory))
    before = sum(size for _p, size, _m in found)
    total, evicted, files = before, 0, 0
    if before > quota:
        if not dry_run and not _managed(root):
            raise RefusedDeletion("%s n'est pas un dossier géré par ameesh" % root)
        for path, size, mtime in sorted(found, key=lambda item: item[2]):
            if total <= quota:
                break
            if now - mtime < min_age:
                continue
            if not _located_in(path, root):
                continue
            if not dry_run:
                try:
                    os.unlink(path)
                except OSError:
                    continue
            total -= size
            evicted += size
            files += 1
        if not dry_run:
            for directory in directories:
                _prune_empty_dirs(directory)
    return {"before": before, "after": total, "evicted": evicted, "files": files,
            "over_quota": total > quota}


# --------------------------------------------------------------------------
# /tmp du système : signalé, jamais supprimé
# --------------------------------------------------------------------------

def tmp_names(path: str | None = None) -> set[str] | None:
    """Noms de premier niveau du /tmp du système (None si illisible)."""
    try:
        return set(os.listdir(path or resources_mod.system_tmp()))
    except OSError:
        return None


def new_tmp_entries(before: set[str] | None, *, path: str | None = None,
                    min_size: int = DEFAULT_ORPHAN_MIN_SIZE,
                    exclude: tuple[str, ...] = ()) -> list[dict]:
    """Entrées de /tmp apparues depuis `before`, à nous, d'au moins
    `min_size` octets : chemin, taille, commande proposée, et l'identité de
    l'entrée (`ino`, `dev`) — le ménage ne supprime que CETTE entrée-là, pas
    une homonyme recréée depuis (L162)."""
    if before is None:
        return []
    base = path or resources_mod.system_tmp()
    after = tmp_names(base)
    if after is None:
        return []
    uid = os.getuid()
    real_excluded = {os.path.realpath(e) for e in exclude if e}
    out = []
    for name in sorted(after - before):
        full = os.path.join(base, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if st.st_uid != uid or os.path.realpath(full) in real_excluded:
            continue
        size = size_of(full)
        if size < min_size:
            continue
        out.append({"path": full, "bytes": size, "command": "rm -rf -- %s" % shlex.quote(full),
                    "ino": st.st_ino, "dev": st.st_dev})
    return out


# --------------------------------------------------------------------------
# /tmp du système : entrées laissées par un tour, supprimées si abandonnées
# (L162 ; incident du 2026-10-11 à 03:38 : tmpfs de 12 Go plein à 83 %,
# porte de pression fermée, treize agents au repos)
# --------------------------------------------------------------------------

def _blocks(st) -> int:
    return st.st_blocks * 512 if getattr(st, "st_blocks", None) else st.st_size


def fmt_idle(seconds: float) -> str:
    """« 2 h », « 90 min », « 45 s »."""
    seconds = max(0, int(round(float(seconds))))
    if seconds >= 3600 and seconds % 3600 == 0:
        return "%d h" % (seconds // 3600)
    if seconds >= 60:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def scan_activity(path: str, limit: int = WALK_LIMIT) -> dict:
    """Parcours d'une entrée pour juger de son abandon : `bytes`, `latest` (la
    plus récente des dates de modification ET de changement d'état de
    l'entrée et de tout ce qu'elle contient — le ctime parce qu'une archive
    extraite garde les mtimes de l'archive), `complete`, `why`.

    Sans suivre les liens ni changer de système de fichiers. Un point de
    montage dedans, une entrée illisible ou plus de `limit` entrées rendent le
    parcours incomplet : on ne juge pas ce qu'on n'a pas vu."""
    out: dict = {"bytes": 0, "latest": None, "complete": True, "why": ""}
    try:
        top = os.lstat(path)
    except OSError as exc:
        out.update(complete=False, why="illisible (%s)" % (exc.strerror or exc))
        return out
    latest = max(top.st_mtime, top.st_ctime)
    if not stat.S_ISDIR(top.st_mode):
        out.update(bytes=_blocks(top), latest=latest)
        return out
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        out.update(complete=False, why="entrée illisible : %s" % entry.path)
                        continue
                    count += 1
                    latest = max(latest, st.st_mtime, st.st_ctime)
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev != top.st_dev:
                            out.update(complete=False, latest=latest,
                                       why="point de montage dedans : %s" % entry.path)
                            return out
                        stack.append(entry.path)
                    else:
                        out["bytes"] += _blocks(st)
                    if count >= limit:
                        out.update(complete=False, latest=latest,
                                   why="plus de %d entrées : activité non vérifiable" % limit)
                        return out
        except OSError:
            out.update(complete=False, why="dossier illisible : %s" % current)
    out["latest"] = latest
    return out


def _git_copies(path: str) -> list[str]:
    """L'entrée et ses sous-dossiers directs qui sont des copies git (`.git`
    dossier, ou fichier d'un worktree) : clone, worktree de relecture."""
    if os.path.islink(path) or not os.path.isdir(path):
        return []
    candidates = [path]
    try:
        with os.scandir(path) as it:
            candidates += [e.path for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        pass
    return [c for c in candidates if os.path.lexists(os.path.join(c, ".git"))]


def unsaved_git_work(path: str) -> str:
    """Pourquoi une copie git de l'entrée porte du travail à garder, vide
    sinon : verrouillée, modifications non commitées (ou état illisible),
    HEAD sur aucune branche distante (commits jamais poussés)."""
    for copy in _git_copies(path):
        st = worktree_state(copy)
        if st["locked"]:
            return "%s verrouillé (git worktree lock)" % copy
        if not st["clean"]:
            return "%s : %s" % (copy, st["reason"] or "modifications non commitées")
        if not (st["pushed"] or st["integrated"]):
            return "%s : HEAD %s sur aucune branche distante" % (copy, st["head"][:12] or "?")
    return ""


def tmp_holder(path: str) -> str:
    """Le processus qui tient `path` (fichier ouvert dedans, dossier courant,
    chemin sur sa ligne de commande), en clair ; vide si aucun. Si l'OS ne
    permet pas de le savoir, une raison : l'entrée est gardée (fail-closed)."""
    try:
        found = platform.holders(_real(path), needle=os.path.abspath(path))
    except platform.NotAvailable as exc:
        return "détenteurs illisibles (%s)" % exc
    if not found:
        return ""
    first = found[0]
    why = first.get("why") or ""
    if "ligne de commande" in why:
        why = "chemin sur la ligne de commande"
    return "tenu par le processus %s (%s : %s)" % (first.get("pid"), why,
                                                   (first.get("cmd") or "")[:80])


def _tmp_entry_problem(path: str, base: str, ino=None, dev=None) -> str:
    """Pourquoi `path` n'est pas une entrée de /tmp que le ménage peut
    supprimer, vide si elle l'est : enfant direct de `base` (le /tmp du
    système, jamais ailleurs), dossier ou fichier ordinaire (pas un lien), à
    l'utilisateur, et — si connue — la MÊME entrée que celle signalée (inode)."""
    name = os.path.basename(path)
    if not name or name in (".", "..") or \
            os.path.realpath(os.path.dirname(os.path.abspath(path))) != os.path.realpath(base):
        return "hors du /tmp du système (%s)" % base
    try:
        st = os.lstat(path)
    except OSError:
        return "illisible"
    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return "ni dossier ni fichier ordinaire"
    if st.st_uid != os.getuid():
        return "appartient à un autre utilisateur"
    if ino is not None and (st.st_ino, st.st_dev) != (ino, dev):
        return "recréée depuis le signalement (autre inode)"
    return ""


def _remove_tmp_entry(path: str, base: str, ino=None, dev=None) -> int:
    """Supprime une entrée de /tmp, revérifiée juste avant
    (`_tmp_entry_problem`) ; rend les octets libérés."""
    problem = _tmp_entry_problem(path, base, ino, dev)
    if problem:
        raise RefusedDeletion("%s : %s" % (path, problem))
    before = size_of(path)
    if stat.S_ISDIR(os.lstat(path).st_mode):
        _make_owned_dirs_writable(path)
        shutil.rmtree(path, ignore_errors=True)
    else:
        os.unlink(path)
    left = size_of(path) if os.path.lexists(path) else 0
    return max(0, before - left)


def _make_owned_dirs_writable(top: str) -> None:
    """Rend lecture, écriture et traversée à NOS dossiers sous `top` (un cache
    de modules Go est en lecture seule) : sans cela, la suppression resterait
    partielle. Aucun lien suivi, aucun dossier d'un autre utilisateur touché."""
    uid = os.getuid()

    def ouvrir(full: str) -> None:
        try:
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == uid and (st.st_mode & 0o700) != 0o700:
                os.chmod(full, stat.S_IMODE(st.st_mode) | 0o700)
        except OSError:
            pass

    ouvrir(top)
    for current, dirs, _files in os.walk(top):   # followlinks=False
        for name in dirs:   # avant que os.walk n'y descende
            ouvrir(os.path.join(current, name))


def _orphan_verdict(row: dict, policy: Policy, *, base: str, registered, protected: set,
                    running: set, now: float) -> tuple[str, str]:
    """(`suppression` | `attend` | `garde`, raison) pour une entrée signalée
    encore présente. Toutes les conditions de la suppression, la moins chère
    d'abord ; le moindre doute garde l'entrée."""
    path = row["path"]
    data = row.get("data") or {}
    if not policy.orphan_cleanup:
        return "garde", "suppression coupée sur cet hôte (orphan_cleanup: false)"
    problem = _tmp_entry_problem(path, base, data.get("ino"), data.get("dev"))
    if problem:
        return "garde", problem
    agent = row.get("agent")
    if registered is None:
        return "garde", "registre illisible"
    if not agent or agent not in registered:
        return "garde", "agent %s absent du registre" % (agent or "?")
    if agent in running:
        return "attend", "tour en cours de %s" % agent
    target = _real(path)
    if any(_under(p, target) for p in protected):
        return "garde", "contient un dossier de travail d'agent ou un dossier géré"
    scan = scan_activity(path)
    if not scan["complete"]:
        return "garde", scan["why"]
    idle = now - float(scan["latest"])
    if idle < policy.orphan_idle:
        return "attend", "modifié il y a %s (supprimé après %s sans changement)" % (
            fmt_idle(max(0.0, idle)), fmt_idle(policy.orphan_idle))
    work = unsaved_git_work(path)
    if work:
        return "garde", "travail git à garder — %s" % work
    holder = tmp_holder(path)
    if holder:
        return "attend", holder
    return "suppression", "laissé par le tour %s de %s, inchangé depuis %s, tenu par aucun " \
        "processus" % (str(data.get("turn") or "?")[:8], agent, fmt_idle(idle))


def orphan_pass(db, host: str, policy: Policy, *, registered, protected: set,
                running: set, dry_run: bool, now: float) -> tuple[list, list, list]:
    """Entrées de /tmp signalées pendant un tour (L162) : (lignes du journal,
    plan lisible, entrées encore présentes avec leur sort).

    La dernière ligne de chaque chemin compte : `signaled` est candidate,
    `deleted` est close (une homonyme recréée hors d'un tour n'est pas à
    nous). Suppression seulement si `_orphan_verdict` le permet ; sinon
    l'entrée reste signalée, avec la raison. Rien n'est écrit en base ici."""
    base = resources_mod.system_tmp()
    protected_real = {_real(p) for p in protected if p}
    entries: list[dict] = []
    plan: list[dict] = []
    present: list[dict] = []
    if not host:   # sans nom d'hôte, les signalements des autres hôtes s'y mêleraient
        return entries, plan, present
    try:
        # #222: the last line of every path, with no window nor limit: an
        # unattributed entry is followed for as long as it exists
        rows = storage.of(db).housekeeping.latest_by_path(host, "orphan")
    except Exception:
        return entries, plan, present
    for row in rows:
        path = row.get("path") or ""
        if not path or row.get("action") != "signaled":
            continue
        if row.get("agent") and now - float(row.get("at_ts") or 0) > ORPHAN_LOOKBACK_S:
            continue   # an attributed signal (before #222) is read for 7 days, as before
        data = row.get("data") or {}
        base_item = {"kind": "orphan", "path": path, "agent": row.get("agent"),
                     "lot": row.get("lot")}
        if not os.path.lexists(path):
            if not row.get("agent") and not dry_run:
                # #222: the tracking of an unattributed entry ends when it is gone
                entries.append(dict(base_item, action="gone", bytes=0, data=data,
                                    detail="entrée non attribuée disparue de /tmp"))
            continue
        action, why = _orphan_verdict(row, policy, base=base, registered=registered,
                                      protected=protected_real, running=running, now=now)
        size = size_of(path)
        plan.append(dict(base_item, action=action, bytes=size, detail=why))
        if not row.get("agent") and not dry_run \
                and now - float(row.get("at_ts") or 0) >= UNATTRIBUTED_REFRESH_S:
            # #222: never deleted, an unattributed entry stays signaled, with
            # its current size, for as long as it is on disk (alerts read 24 h)
            entries.append(dict(base_item, action="signaled", bytes=size, data=data,
                                detail=row.get("detail") or UNATTRIBUTED))
        item = {"path": path, "bytes": size, "agent": row.get("agent"),
                "at_ts": row.get("at_ts"), "status": action, "detail": why,
                "command": data.get("command") or "rm -rf -- %s" % shlex.quote(path)}
        if action != "suppression" or dry_run:
            present.append(item)
            continue
        worktrees = [common_dir(c) for c in _git_copies(path)
                     if os.path.isfile(os.path.join(c, ".git"))]
        try:
            freed = _remove_tmp_entry(path, base, data.get("ino"), data.get("dev"))
        except (OSError, RefusedDeletion) as exc:
            item.update(status="garde", detail="suppression refusée : %s" % exc)
            present.append(item)
            continue
        for repo in worktrees:
            prune_worktrees(repo)
        entries.append(dict(base_item, action="deleted", bytes=freed,
                            detail="entrée de /tmp abandonnée : %s" % why,
                            data={"turn": data.get("turn"), "command": item["command"]}))
        if os.path.lexists(path):  # suppression partielle : ce qui reste est dit
            present.append(dict(item, bytes=size_of(path), status="garde",
                                detail="suppression partielle (fichiers d'un autre "
                                       "utilisateur ?)"))
    return entries, plan, present


def _owner(uid: int) -> str:
    try:
        import pwd  # Unix seulement
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def tmp_top(base: str | None = None, n: int = TMP_TOP) -> list[dict]:
    """Les `n` plus grosses entrées de premier niveau du /tmp du système
    (lecture seule ; taille bornée à `WALK_LIMIT` fichiers par entrée) :
    `path`, `bytes`, `owner`, `mtime`."""
    base = base or resources_mod.system_tmp()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(base, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        out.append({"path": full, "bytes": size_of(full), "owner": _owner(st.st_uid),
                    "mtime": st.st_mtime})
    out.sort(key=lambda e: (-e["bytes"], e["path"]))
    return out[:n]


def describe_top(top) -> str:
    """« /tmp/a 2.2 GiB (smichea, laissé par b1 : attend — …) ; … »."""
    parts = []
    for e in top or ():
        who = e.get("owner") or "?"
        if e.get("agent"):
            who += ", laissé par %s" % e["agent"]
            if e.get("status"):
                who += " : %s" % e["status"]
        parts.append("%s %s (%s)" % (e.get("path"), _fmt(e.get("bytes")), who))
    return " ; ".join(parts)


def tmpfs_hint(top, policy: Policy) -> str:
    """Le complément d'une alerte de tmpfs plein : les plus grosses entrées
    (si un bilan les a relevées) et l'action attendue."""
    plus_gros = ("plus gros dans /tmp : %s" % describe_top(top)) if top else \
        "plus gros dans /tmp : `ameesh menage` sur l'hôte"
    return "%s — %s" % (plus_gros, TMPFS_ACTION % fmt_idle(policy.orphan_idle))


# --------------------------------------------------------------------------
# worktrees
# --------------------------------------------------------------------------

def _git(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT):
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc


def list_worktrees(cwd: str | None) -> list[dict]:
    """Worktrees du dépôt de `cwd` (`git worktree list --porcelain`) ; le
    premier est le worktree principal. Vide si `cwd` n'est pas un dépôt."""
    if not cwd or not os.path.isdir(cwd):
        return []
    proc = _git(["worktree", "list", "--porcelain"], cwd)
    if proc is None or proc.returncode != 0:
        return []
    out: list[dict] = []
    current: dict = {}
    for line in proc.stdout.splitlines() + [""]:
        if not line.strip():
            if current.get("path"):
                out.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": os.path.abspath(value), "head": "", "branch": "",
                       "detached": False, "bare": False, "locked": False}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "locked":
            current["locked"] = True
    return out


def common_dir(cwd: str) -> str:
    proc = _git(["rev-parse", "--git-common-dir"], cwd)
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return ""
    return os.path.abspath(os.path.join(cwd, proc.stdout.strip()))


def _default_remote_ref(path: str) -> str:
    """Branche principale du dépôt distant (`origin/HEAD`, sinon `origin/main`,
    sinon `origin/master`) ; vide si aucune n'existe."""
    upstream = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], path)
    candidates = []
    if upstream is not None and upstream.returncode == 0 and upstream.stdout.strip() \
            and upstream.stdout.strip() != "origin/HEAD":
        candidates.append(upstream.stdout.strip())
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        found = _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], path)
        if found is not None and found.returncode == 0:
            return ref
    return ""


def worktree_state(path: str) -> dict:
    """État d'un worktree pour son retrait : `exists`, `clean`, `pushed` (HEAD
    sur une branche distante), `integrated` (HEAD contenu dans la branche
    principale distante, ou intégré par rebase), `locked` (`git worktree
    lock`), `head`, `reason` (pourquoi il ne peut pas être retiré, vide
    sinon).

    Seul `integrated` autorise un retrait : une branche poussée mais pas
    fusionnée est du travail en cours (L73b)."""
    out = {"exists": os.path.isdir(path), "clean": False, "pushed": False,
           "integrated": False, "locked": False, "head": "", "reason": ""}
    if not out["exists"]:
        out["reason"] = "dossier disparu"
        return out
    gitdir = _git(["rev-parse", "--absolute-git-dir"], path)
    if gitdir is not None and gitdir.returncode == 0 and gitdir.stdout.strip():
        out["locked"] = os.path.exists(os.path.join(gitdir.stdout.strip(), "locked"))
    status = _git(["status", "--porcelain", "--untracked-files=normal"], path)
    if status is None or status.returncode != 0:
        out["reason"] = "état git illisible"
        return out
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    out["clean"] = not dirty
    head = _git(["rev-parse", "HEAD"], path)
    out["head"] = head.stdout.strip() if head is not None and head.returncode == 0 else ""
    remote = _git(["for-each-ref", "--contains", "HEAD", "--count=1", "refs/remotes"], path)
    out["pushed"] = bool(remote is not None and remote.returncode == 0 and remote.stdout.strip())
    ref = _default_remote_ref(path) if out["head"] else ""
    if ref:
        ancestor = _git(["merge-base", "--is-ancestor", "HEAD", ref], path)
        if ancestor is not None and ancestor.returncode == 0:
            out["integrated"] = True
        else:
            # Intégré par rebase : chaque commit propre a un équivalent sur la
            # branche principale du dépôt distant (`git cherry` : lignes « - »).
            cherry = _git(["cherry", ref, "HEAD"], path)
            if cherry is not None and cherry.returncode == 0:
                lines = [line for line in cherry.stdout.splitlines() if line.strip()]
                out["integrated"] = all(line.startswith("-") for line in lines)
        if not out["integrated"]:
            # Fusion par écrasement (squash) : fusionner HEAD dans la branche
            # principale ne changerait rien (`git merge-tree`, git ≥ 2.38).
            merged = _git(["merge-tree", "--write-tree", ref, "HEAD"], path)
            tree = _git(["rev-parse", ref + "^{tree}"], path)
            if merged is not None and merged.returncode == 0 and tree is not None \
                    and tree.returncode == 0 and merged.stdout.strip():
                out["integrated"] = (merged.stdout.split()[0] == tree.stdout.strip())
    if out["locked"]:
        out["reason"] = "verrouillé (git worktree lock)"
    elif dirty:
        out["reason"] = "%d fichier(s) modifié(s) ou non suivi(s)" % len(dirty)
    elif not out["integrated"]:
        out["reason"] = "HEAD %s non intégré à %s (%s)" % (
            out["head"][:12] or "?", ref or "la branche principale distante",
            "poussé mais non fusionné" if out["pushed"] else "commits non poussés")
    return out


def _under(path: str, root: str) -> bool:
    """`path` est `root` ou se trouve dessous (chemins absolus normalisés)."""
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def owns_worktree(path: str, cwd: str | None, other_cwds) -> bool:
    """Le worktree `path`, apparu pendant un tour lancé dans `cwd`, a-t-il été
    créé par CET agent ? (L73b)

    `git worktree list` couvre tout le dépôt : un worktree apparu pendant le
    tour peut être celui d'un autre agent qui travaille en parallèle sur le
    même dépôt. Critère retenu, le plus sûr dont on dispose sans trace du
    processus créateur : le worktree est SOUS le dossier de travail de
    l'agent (sous-agents, `.claude/worktrees/…`), et aucun autre agent n'a ce
    même dossier ni un dossier plus proche du worktree. Dans le doute : non."""
    if not cwd:
        return False
    mine, target = _real(cwd), _real(path)
    if target == mine or not _under(target, mine):
        return False
    for other in other_cwds or ():
        if not other:
            continue
        theirs = _real(other)
        if theirs == mine or (_under(target, theirs) and _under(theirs, mine)):
            return False
    return True


def worktree_in_use(path: str) -> bool:
    """Un processus de l'hôte travaille-t-il dans ce worktree (dossier courant
    ou fichier ouvert dedans) ? Lu par la couche plateforme (L63). Si l'OS ne
    permet pas de le savoir, la réponse est OUI (fail-closed) : le worktree
    attend plutôt que d'être retiré sous les pieds d'un agent."""
    try:
        return bool(platform.holders(_real(path)))
    except platform.NotAvailable:
        return True


def remove_worktree(path: str, repo: str) -> tuple[bool, str]:
    """`git worktree remove` (jamais `--force`), lancé depuis le dépôt commun."""
    base = repo if repo and os.path.isdir(repo) else path
    proc = _git(["worktree", "remove", path], base)
    if proc is None:
        return False, "git indisponible"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:300] or "refusé par git"
    return True, ""


def prune_worktrees(repo: str) -> None:
    if repo and os.path.isdir(repo):
        _git(["worktree", "prune"], repo)


def _other_agent_cwds(db, agent: str):
    """Dossiers de travail des AUTRES agents du registre ; False si le
    registre est illisible (aucun worktree n'est alors attribué)."""
    try:
        return [row["cwd"] for row in storage.of(db).operations.listing()
                if row.get("name") != agent and row.get("cwd")]
    except Exception:
        return False


# --------------------------------------------------------------------------
# un tour
# --------------------------------------------------------------------------

@dataclass
class Turn:
    """Ménage autour d'un tour : préparé avant le lancement, conclu après.

    `begin` (avant le lancement) : vide le dossier temporaire de l'agent si
    la session est neuve, rend l'environnement à poser, relève les worktrees
    du dépôt et les noms de /tmp. `end` (après le tour, même en échec) :
    quota du dossier temporaire, enregistrement des worktrees apparus,
    signalement des entrées de /tmp apparues ; rend les lignes du journal."""

    cfg: object
    policy: Policy
    agent: str
    cwd: str | None
    env: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)
    worktrees_before: set = field(default_factory=set)
    tmp_before: set | None = None
    started: float = 0.0
    #: `AMEESH_HOUSEKEEPING=off` : rien n'est fait autour du tour
    off: bool = False
    #: L219: the temporary directory, locked (`TmpHold`) until `end`
    lock: object = None
    tmp: str = ""

    @classmethod
    def begin(cls, cfg, policy: Policy, agent: str, cwd: str | None, *,
              new_session: bool, base_env: dict) -> "Turn":
        turn = cls(cfg=cfg, policy=policy, agent=agent, cwd=cwd, started=time.time())
        if disabled(base_env):
            turn.off = True
            return turn
        if policy.redirect:
            # L219: locked (shared) until `end`, so that no pass deletes or
            # evicts it meanwhile
            hold = hold_tmp(cfg, policy, agent, new_session=new_session)
            turn.lock, turn.tmp = hold, hold.path
            turn.entries.extend(hold.entries)
            try:
                turn.env = turn_env(cfg, policy, agent, base_env, hold=hold)
            except Exception:
                hold.release()
                raise
        if policy.worktrees:
            turn.worktrees_before = {w["path"] for w in list_worktrees(cwd)}
        turn.tmp_before = tmp_names()
        return turn

    def end(self, *, lot: str | None, turn_id: str, db=None, host: str = "") -> list[dict]:
        if self.off:
            return self.entries
        policy = self.policy
        try:
            if self.env and self.tmp and self.lock is not None and self.lock.exclusive_now():
                # L219: under the exclusive lock, or no eviction (another
                # runner holding it shared may be using the directory)
                root = tmp_root(self.cfg, policy)
                result = evict_lru([self.tmp], root, policy.tmp_quota,
                                   min_age=EVICT_MIN_AGE_S)
                if result["files"]:
                    self.entries.append({
                        "kind": "tmp", "action": "evicted",
                        "path": self.tmp,
                        "bytes": result["evicted"], "agent": self.agent, "lot": lot,
                        "detail": "quota %s : %d fichier(s) les plus anciens évincés"
                                  % (resources_mod.fmt_value("q", policy.tmp_quota),
                                     result["files"])})
        finally:
            if self.lock is not None:      # L219: the turn is over
                self.lock.release()
                self.lock = None
        if policy.worktrees and self.cwd and db is not None:
            known_cwd = os.path.abspath(self.cwd)
            repo = ""
            others = None  # dossiers des autres agents, lus au premier besoin
            for wt in list_worktrees(self.cwd):
                if wt["path"] in self.worktrees_before or wt["path"] == known_cwd \
                        or wt.get("bare"):
                    continue
                if others is None:
                    others = _other_agent_cwds(db, self.agent)
                # L73b : `git worktree list` couvre tout le dépôt ; un worktree
                # d'un autre agent apparu pendant ce tour n'est pas à nous.
                mine = others is not False and owns_worktree(wt["path"], self.cwd, others)
                repo = repo or common_dir(self.cwd)
                hk = storage.of(db).housekeeping
                row = hk.register_worktree(
                    host=host, path=wt["path"], repo=repo, agent=self.agent,
                    lot=lot if mine else None, turn_id=turn_id,
                    branch=wt.get("branch") or "", head=wt.get("head") or "")
                if row is None:
                    continue
                if mine:
                    detail = ("apparu pendant le tour %s (%s) ; retiré à la fin du lot s'il "
                              "est propre et intégré à la branche principale"
                              % (turn_id[:8], wt.get("branch") or "détaché"))
                else:
                    detail = ("%s : apparu pendant le tour %s (%s) hors du dossier de "
                              "l'agent ; jamais retiré automatiquement"
                              % (UNATTRIBUTED, turn_id[:8], wt.get("branch") or "détaché"))
                    hk.set_worktree_status(row["id"], "active", detail)
                self.entries.append({
                    "kind": "worktree", "action": "registered", "path": wt["path"],
                    "agent": self.agent, "lot": lot if mine else None, "detail": detail})
        exclude = tuple(v for v in (self.env.get("TMPDIR"), self.env.get("AMEESH_CACHE_DIR"))
                        if v)
        found_all = new_tmp_entries(self.tmp_before, min_size=policy.orphan_min_size,
                                    exclude=exclude)
        signaled = self._signaled(db, host) if found_all else set()
        for found in found_all:
            if (found["path"], str(found["ino"]), str(found["dev"])) in signaled:
                continue  # #222: already signaled by another turn, its fate stays
            # #222: /tmp is shared by the whole host (other agents, other meshes
            # whose state directory is elsewhere, processes outside any turn):
            # nothing proves that this turn created the entry, so it is never
            # charged to it. Unattributed, it is never deleted (`_orphan_verdict`
            # keeps an entry without agent); the `tmp_orphan` alert follows it,
            # with its size and the command, for as long as it exists.
            self.entries.append({
                "kind": "orphan", "action": "signaled", "path": found["path"],
                "bytes": found["bytes"], "agent": None, "lot": None,
                "detail": "%s : apparu dans /tmp pendant le tour %s de %s ; /tmp est commun "
                          "à l'hôte (autres tours, autres meshes, processus hors tour) : "
                          "jamais supprimé automatiquement" % (UNATTRIBUTED, turn_id[:8],
                                                                 self.agent),
                "data": {"command": found["command"], "turn": turn_id, "witness": self.agent,
                         "lot": lot, "ino": found["ino"], "dev": found["dev"]}})
        return self.entries

    def _signaled(self, db, host: str) -> set:
        """#222: (path, inode, device) of the /tmp entries whose last line is
        a signal (sticky rule: a later turn does not take it again). Without
        database, or if the log cannot be read: none."""
        if db is None or not host:
            return set()
        try:
            rows = storage.of(db).housekeeping.latest_by_path(host, "orphan")
        except Exception:
            return set()
        return {(r.get("path"), str((r.get("data") or {}).get("ino")),
                 str((r.get("data") or {}).get("dev")))
                for r in rows if r.get("action") == "signaled"}


# --------------------------------------------------------------------------
# un passage (exécuteur périodique, ou `ameesh menage`)
# --------------------------------------------------------------------------

def measure(cfg, policy: Policy) -> dict:
    """Occupation des dossiers gérés et du /tmp du système (lecture seule).

    L162 : dès que le /tmp en tmpfs atteint l'alerte (ou le seuil de pression
    par défaut, s'il est plus bas), ses `TMP_TOP` plus grosses entrées
    (`system_tmp_top`), que les alertes reprennent."""
    troot, croot = tmp_root(cfg, policy), cache_root(cfg, policy)
    agents: dict = {}
    if os.path.isdir(troot):
        for name in sorted(os.listdir(troot)):
            full = os.path.join(troot, name)
            if name != MARKER and os.path.isdir(full) and not os.path.islink(full):
                agents[name] = size_of(full)
    caches: dict = {}
    for name, _vars in cache_names(policy):
        full = os.path.join(croot, name)
        if os.path.isdir(full):
            caches[name] = size_of(full)
    system = resources_mod.fs_usage(resources_mod.system_tmp())
    fraction = resources_mod.tmpfs_fraction(system)
    seuil = min(policy.tmpfs_alert, resources_mod.DEFAULT_MAX_TMPFS_USED)
    return {
        "tmp_root": troot, "tmp_bytes": sum(agents.values()), "tmp_by_agent": agents,
        "tmp_quota": policy.tmp_quota,
        "tmp_root_fstype": (resources_mod.fs_usage(troot).get("tmp_fstype")
                            if os.path.isdir(troot) else None),
        "cache_root": croot, "cache_bytes": sum(caches.values()), "cache_by_name": caches,
        "cache_quota": policy.cache_quota,
        "system_tmp": system,
        "system_tmp_fraction": fraction,
        "system_tmp_top": tmp_top() if fraction is not None and fraction >= seuil else [],
        "tmpfs_alert": policy.tmpfs_alert,
    }


def _lot_state(db, lot) -> str | None:
    try:
        ident = int(str(lot).lstrip("#"))
    except (TypeError, ValueError):
        return None
    row = storage.of(db).work.get(ident)
    return row.get("state") if row else None


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _worktree_pass(db, host: str, *, dry_run: bool, include_lotless: bool,
                   protected: set[str], now: float,
                   running: set | None = None) -> tuple[list[dict], list[dict]]:
    """Fin de lot des worktrees suivis : (lignes du journal, plan lisible).

    Garde-fous (L73b) avant tout retrait : worktree attribué à l'agent du
    tour (jamais un worktree « non attribué »), pas le dossier de travail
    d'un agent ni un dossier qui en contient un, agent propriétaire sans tour
    en cours (`running`), au moins `WORKTREE_MIN_AGE_S` depuis son
    enregistrement et depuis la dernière modification de son dossier, non
    verrouillé, aucun processus dedans, propre, et HEAD INTÉGRÉ à la branche
    principale distante (poussé ne suffit pas)."""
    entries: list[dict] = []
    plan: list[dict] = []
    running = set(running or ())
    store = storage.of(db).housekeeping
    for row in store.worktrees(host, ("active", "kept")):
        path, lot = row["path"], row.get("lot")
        base = {"kind": "worktree", "path": path, "agent": row["agent"], "lot": lot}
        if (row.get("detail") or "").startswith(UNATTRIBUTED):
            if not os.path.isdir(path):
                plan.append(dict(base, action="disparu", detail=UNATTRIBUTED))
                if not dry_run:
                    prune_worktrees(row.get("repo") or "")
                    store.set_worktree_status(row["id"], "gone",
                                              "dossier disparu (%s)" % UNATTRIBUTED)
                    entries.append(dict(base, action="gone", detail="dossier disparu ; "
                                        "git worktree prune (%s)" % UNATTRIBUTED))
            else:
                plan.append(dict(base, action="garde", detail=row.get("detail")))
            continue
        state = _lot_state(db, lot) if lot else None
        ended = state in LOT_ENDED
        lotless_due = (include_lotless and not lot
                       and now - float(row.get("created_ts") or now) >= LOTLESS_TTL_S)
        if not ended and not lotless_due:
            plan.append({"path": path, "agent": row["agent"], "lot": lot,
                         "action": "attend", "detail": "lot %s" % (state or "inconnu")
                         if lot else "sans lot"})
            continue
        why = ("lot #%s %s" % (lot, state)) if ended else "sans lot depuis plus de 7 j"
        target = _real(path)
        if any(_under(_real(cwd), target) for cwd in protected):
            plan.append(dict(base, action="garde", detail="dossier de travail d'un agent"))
            continue
        if row["agent"] in running:
            plan.append(dict(base, action="attend",
                             detail="%s ; tour en cours de %s" % (why, row["agent"])))
            continue
        moments = [float(row.get("created_ts") or now), _mtime(path)]
        youngest = max(m for m in moments if m is not None)
        if now - youngest < WORKTREE_MIN_AGE_S:
            plan.append(dict(base, action="attend",
                             detail="%s ; créé ou modifié il y a moins d'une heure" % why))
            continue
        st = worktree_state(path)
        if not st["exists"]:
            plan.append(dict(base, action="disparu", detail=why))
            if not dry_run:
                prune_worktrees(row.get("repo") or "")
                store.set_worktree_status(row["id"], "gone", "dossier disparu (%s)" % why)
                entries.append(dict(base, action="gone", detail="dossier disparu ; "
                                    "git worktree prune (%s)" % why))
            continue
        if st["locked"]:
            plan.append(dict(base, action="garde", detail="%s ; %s" % (why, st["reason"])))
            continue
        if worktree_in_use(path):
            plan.append(dict(base, action="attend",
                             detail="%s ; un processus travaille dedans" % why))
            continue
        if not st["clean"] or not st["integrated"]:
            detail = "%s : gardé — %s" % (why, st["reason"])
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run and (row["status"] != "kept" or row.get("detail") != detail):
                store.set_worktree_status(row["id"], "kept", detail)
                entries.append(dict(base, action="kept", detail=detail, data={
                    "command": "git -C %s status ; git -C %s log --branches --not --remotes"
                               % (shlex.quote(path), shlex.quote(path))}))
            continue
        size = size_of(path)
        plan.append(dict(base, action="retrait", bytes=size, detail=why))
        if dry_run:
            continue
        ok, error = remove_worktree(path, row.get("repo") or "")
        if ok:
            store.set_worktree_status(row["id"], "removed", why)
            entries.append(dict(base, action="removed", bytes=size,
                                detail="%s : propre et intégré, git worktree remove" % why))
        else:
            detail = "%s : retrait refusé par git — %s" % (why, error)
            store.set_worktree_status(row["id"], "kept", detail)
            entries.append(dict(base, action="kept", detail=detail))
    return entries, plan


def reap_containers(runtime, db, host: str, *, dry_run: bool = False,
                    only_turn: str | None = None) -> dict:
    """Conteneurs de l'hôte (L73) : ceux qu'un tour d'ameesh a lancés
    (étiquette `ameesh.turn`) sont supprimés une fois leur tour fini — ou,
    s'ils portent `ameesh.lot`, une fois le lot fini ; les autres sont
    seulement signalés, avec la commande proposée.

    Un conteneur étiqueté par un tour INCONNU de cette base, ou d'un autre
    hôte, n'est pas supprimé (signalé). Rend `{entries, plan, ameesh,
    others}` ; `ameesh`/`others` à None si le moteur ne répond pas."""
    out: dict = {"entries": [], "plan": [], "ameesh": None, "others": None}
    if runtime is None:
        return out
    rows = runtime.list_all()
    if rows is None:
        return out
    binary = os.path.basename(getattr(runtime, "binary", "docker") or "docker")
    ours, others = [], []
    turns = storage.of(db).turn_resources
    for c in rows:
        labels = c.get("labels") or {}
        turn = labels.get("ameesh.turn")
        ref = c.get("name") or c.get("id", "")[:12]
        if not turn:
            if only_turn is None:
                others.append({"id": c.get("id", "")[:12], "name": c.get("name"),
                               "image": c.get("image"), "status": c.get("status"),
                               "command": "%s rm -f %s" % (binary, shlex.quote(ref))})
            continue
        if only_turn is not None and turn != only_turn:
            continue
        lot, agent = labels.get("ameesh.lot"), labels.get("ameesh.agent")
        base = {"kind": "container", "path": ref, "agent": agent, "lot": lot}
        row = turns.get(turn)
        if row is None or (row.get("host") or "") != (host or ""):
            why = "tour %s inconnu sur cet hôte : signalé seulement" % turn[:8]
            ours.append(dict(base, action="signalé", detail=why))
            out["plan"].append(dict(base, action="signalé", detail=why))
            continue
        if row.get("status") == "running":
            ours.append(dict(base, action="attend", detail="tour %s en cours" % turn[:8]))
            continue
        if lot:
            state = _lot_state(db, lot)
            if state not in LOT_ENDED:
                detail = "lot #%s %s" % (lot, state or "inconnu")
                ours.append(dict(base, action="attend", detail=detail))
                out["plan"].append(dict(base, action="attend", detail=detail))
                continue
            why = "lot #%s %s" % (lot, state)
        else:
            why = "tour %s fini" % turn[:8]
        out["plan"].append(dict(base, action="suppression", detail=why))
        if dry_run:
            ours.append(dict(base, action="suppression", detail=why))
            continue
        ok, error = runtime.remove(c.get("id") or ref)
        entry = dict(base, action="removed" if ok else "kept",
                     detail=("%s : %s rm -f -v" % (why, binary)) if ok
                     else "%s : suppression refusée — %s" % (why, error),
                     data={"id": c.get("id"), "image": c.get("image"), "turn": turn})
        out["entries"].append(entry)
        if not ok:
            ours.append(dict(base, action="kept", detail=entry["detail"]))
    out["ameesh"] = ours
    out["others"] = others if only_turn is None else None
    return out


def tmp_problem(path: str, owner: str | None, key: str, *, registered: bool,
                idle_after: float, now: float) -> str:
    """L219: why the pass keeps the temporary directory `path` of an agent
    not in turn, empty if it may evict (`registered`) or delete it. Another
    mesh's is never touched; one without owner file (another mesh sharing
    the state directory, or before L219) only after `UNOWNED_IDLE_S`
    without any activity; this mesh's of an agent ABSENT from the registry
    after `idle_after` (a turn writes in it)."""
    if owner is not None and owner != key:
        return "appartient à un autre mesh"
    if owner is not None and registered:
        return ""
    latest = last_activity(path)
    if latest is None:
        return "activité illisible (parcours incomplet)"
    idle = now - latest
    if owner is None and idle < UNOWNED_IDLE_S:
        return "sans propriétaire (autre mesh du poste, ou antérieur à L219), " \
               "actif il y a %d h (moins de %d h)" % (idle // 3600, UNOWNED_IDLE_S // 3600)
    if not registered and idle < idle_after:
        return "actif il y a %d min (moins de %d min)" % (idle // 60, idle_after // 60)
    return ""


def run_pass(cfg, db, policy: Policy, *, host: str, actor: str, dry_run: bool = False,
             busy: bool = False, agents_in_turn: set | None = None,
             include_lotless: bool = False, now: float | None = None,
             runtime=None) -> dict:
    """Un passage de ménage sur CET hôte ; rend le rapport.

    * dossiers temporaires : quota par agent (sauf agent en tour) ; dossier
      d'un agent absent du registre supprimé ;
    * caches partagés : éviction jusqu'au quota, seulement si aucun tour ne
      tourne (`busy` faux) ;
    * worktrees suivis dont le lot est fini : retirés s'ils sont propres et
      intégrés (garde-fous de `_worktree_pass`), sinon gardés et signalés ;
    * entrées de /tmp signalées pendant un tour : supprimées si abandonnées
      (garde-fous de `_orphan_verdict`, L162), sinon dites avec la raison ;
    * bilan (`mesure`), avec les plus grosses entrées de /tmp si le tmpfs
      atteint l'alerte.

    `dry_run` : rien n'est supprimé ni écrit en base ; le plan est rendu."""
    now = time.time() if now is None else float(now)
    agents_in_turn = set(agents_in_turn or ())
    entries: list[dict] = []
    plan: list[dict] = []
    skipped: list[str] = []
    if disabled():
        return {"host": host, "dry_run": dry_run, "disabled": True, "entries": [],
                "plan": [], "skipped": ["AMEESH_HOUSEKEEPING=off"], "measure": None}
    troot, croot = tmp_root(cfg, policy), cache_root(cfg, policy)
    registered: set[str] = set()
    protected: set[str] = set()
    running: set[str] = set(agents_in_turn)
    try:
        for row in storage.of(db).operations.listing():
            registered.add(row["name"])
            if row.get("cwd"):
                protected.add(os.path.abspath(os.path.expanduser(row["cwd"])))
            if row.get("status") == "running" and row.get("lease_live"):
                running.add(row["name"])
    except Exception:  # registre illisible : on ne supprime aucun dossier d'agent
        registered = None  # type: ignore[assignment]
    # 1. dossiers temporaires des agents
    key = mesh_key(cfg)
    if os.path.isdir(troot) and _managed(troot):
        for name in sorted(os.listdir(troot)):
            full = os.path.join(troot, name)
            if name == MARKER or not os.path.isdir(full) or os.path.islink(full):
                continue
            owner = read_owner(troot, name)
            if owner is not None and owner != key:
                # L219: another mesh of this host (same state directory)
                # manages it: never deleted nor evicted here
                continue
            agent = tmp_agent(name, key)
            if registered is None and owner is None:
                continue
            # L219: an agent in turn here (`agents_in_turn`) or with a live
            # lease (another runner, a CLI) keeps its directory, registered
            # or not
            if agent in running:
                if registered is not None and agent not in registered:
                    skipped.append("tmp %s gardé : en tour" % full)
                continue
            # a fallback directory (`hold_tmp`) served one turn: deleted once
            # idle, like the directory of an agent absent from the registry
            orphan = FALLBACK_SEP in name or (registered is not None
                                              and agent not in registered)
            why = tmp_problem(full, owner, key, registered=not orphan,
                              idle_after=policy.orphan_idle, now=now)
            if why:
                skipped.append("tmp %s gardé : %s" % (full, why))
                continue
            # L219: a turn holds the lock (shared) from its start to its end;
            # everything is checked again under the exclusive lock, right
            # before deleting or evicting
            lock = None if dry_run else tmp_lock(troot, name, exclusive=True)
            if not dry_run and lock is None:
                skipped.append("tmp %s gardé : en tour (verrou pris)" % full)
                continue
            try:
                if not dry_run and (read_owner(troot, name) != owner or tmp_problem(
                        full, owner, key, registered=not orphan,
                        idle_after=policy.orphan_idle, now=now)):
                    skipped.append("tmp %s gardé : changé pendant le passage" % full)
                    continue
                if orphan:
                    size = size_of(full)
                    plan.append({"kind": "tmp", "path": full, "agent": agent,
                                 "action": "suppression", "bytes": size,
                                 "detail": "agent absent du registre"})
                    if not dry_run:
                        freed = _safe_rmtree(full, troot)
                        for leftover in (owner_file(troot, name), lock_file(troot, name)):
                            try:
                                os.unlink(leftover)
                            except OSError:
                                pass
                        entries.append({"kind": "tmp", "action": "deleted", "path": full,
                                        "bytes": freed, "agent": agent,
                                        "detail": "agent absent du registre"})
                    continue
                result = evict_lru([full], troot, policy.tmp_quota, now=now,
                                   dry_run=dry_run)
                if result["files"]:
                    plan.append({"kind": "tmp", "path": full, "agent": agent,
                                 "action": "éviction", "bytes": result["evicted"],
                                 "detail": "%d fichier(s), quota %s" % (
                                     result["files"],
                                     resources_mod.fmt_value("q", policy.tmp_quota))})
                    if not dry_run:
                        entries.append({"kind": "tmp", "action": "evicted", "path": full,
                                        "bytes": result["evicted"], "agent": agent,
                                        "detail": "quota %s : %d fichier(s) évincés" % (
                                            resources_mod.fmt_value("q", policy.tmp_quota),
                                            result["files"])})
            finally:
                if lock is not None:
                    lock.close()
    # 2. caches partagés
    directories = [os.path.join(croot, name) for name, _v in cache_names(policy)]
    if busy:
        skipped.append("caches : un tour est en cours sur l'hôte, éviction remise")
    elif os.path.isdir(croot) and _managed(croot):
        result = evict_lru(directories, croot, policy.cache_quota, now=now, dry_run=dry_run)
        if result["files"]:
            plan.append({"kind": "cache", "path": croot, "action": "éviction",
                         "bytes": result["evicted"],
                         "detail": "%d fichier(s), quota %s" % (
                             result["files"], resources_mod.fmt_value("q", policy.cache_quota))})
            if not dry_run:
                entries.append({"kind": "cache", "action": "evicted", "path": croot,
                                "bytes": result["evicted"],
                                "detail": "quota %s : %d fichier(s) les plus anciens évincés"
                                          % (resources_mod.fmt_value("q", policy.cache_quota),
                                             result["files"])})
        if result["over_quota"]:
            skipped.append("caches : quota encore dépassé (fichiers de moins d'une heure)")
    # 3. worktrees
    if policy.worktrees and registered is None:
        # sans registre, ni dossiers d'agents ni tours en cours connus
        skipped.append("worktrees : registre illisible, aucun retrait")
    elif policy.worktrees:
        wt_entries, wt_plan = _worktree_pass(db, host, dry_run=dry_run,
                                             include_lotless=include_lotless,
                                             protected=protected, now=now, running=running)
        entries += wt_entries
        plan += wt_plan
    # 4. conteneurs : ceux des tours d'ameesh finis supprimés, autres signalés
    conteneurs = reap_containers(runtime, db, host, dry_run=dry_run)
    entries += conteneurs["entries"]
    plan += conteneurs["plan"]
    # 5. entrées de /tmp laissées par un tour (L162) : supprimées si
    #    abandonnées (garde-fous de `_orphan_verdict`), sinon signalées
    gardes = protected | {troot, croot, cfg.state_dir}
    try:
        orphan_entries, orphan_plan, orphans = orphan_pass(
            db, host, policy, registered=registered, protected=gardes, running=running,
            dry_run=dry_run, now=now)
    except Exception as exc:  # jamais fatal pour le passage
        orphan_entries, orphan_plan, orphans = [], [], []
        skipped.append("/tmp : passage impossible (%s)" % type(exc).__name__)
    entries += orphan_entries
    plan += orphan_plan
    skipped += declared_root_refused(cfg, policy)
    report_measure = measure(cfg, policy)
    # les plus grosses entrées de /tmp, avec ce que le ménage en fera
    sorts = {o["path"]: o for o in orphans}
    for top in report_measure.get("system_tmp_top") or ():
        known = sorts.get(top["path"])
        if known is not None:
            top.update(agent=known.get("agent"), status=known.get("status"),
                       detail=known.get("detail"))
        top["command"] = "rm -rf -- %s" % shlex.quote(top["path"])
    report_measure["containers"] = {
        "ameesh": conteneurs["ameesh"], "others": conteneurs["others"]}
    if not dry_run:
        entries.append({"kind": "mesure", "action": "measured", "path": troot,
                        "bytes": report_measure["tmp_bytes"] + report_measure["cache_bytes"],
                        "detail": "bilan du ménage",
                        "data": dict(report_measure, orphans=len(orphans),
                                     orphan_bytes=sum(o["bytes"] for o in orphans))})
        storage.of(db).housekeeping.log(host, entries, actor=actor)
    return {"host": host, "dry_run": dry_run, "entries": entries, "plan": plan,
            "skipped": skipped, "orphans": orphans, "measure": report_measure,
            "containers": report_measure["containers"],
            "policy": policy.as_dict()}


# --------------------------------------------------------------------------
# CLI : ameesh menage [--dry-run | --apply] [--json]
# --------------------------------------------------------------------------

def caller_refusal(env=None) -> str | None:
    """Pourquoi l'appelant ne peut pas déclencher un ménage, ou None.

    Réservé à un humain habilité (session sans identité d'agent, utilisateur
    système qui possède les dossiers gérés) ou à l'exécuteur (qui appelle
    `run_pass` directement, sans passer par la CLI). Une session d'agent —
    lancée par l'exécuteur ou liée — ne déclenche jamais de suppression."""
    env = env if env is not None else os.environ
    for name in ("AMEESH_TURN_ID", "AGENT_MAIL_NAME"):
        if env.get(name):
            return ("session d'agent (%s posé) : le ménage est réservé à un humain ou à "
                    "l'exécuteur" % name)
    return None


def _fmt(value) -> str:
    return resources_mod.fmt_value("bytes", value)


def print_report(report: dict) -> None:
    mode = "ESSAI (rien n'est supprimé ; --apply pour exécuter)" if report["dry_run"] \
        else "exécuté"
    print("ménage de %s — %s" % (report["host"] or "?", mode))
    m = report.get("measure") or {}
    if m:
        print("  dossiers temporaires  %s (%s) ; quota %s par agent"
              % (_fmt(m["tmp_bytes"]), m["tmp_root"], _fmt(m["tmp_quota"])))
        print("  caches partagés       %s / %s (%s)"
              % (_fmt(m["cache_bytes"]), _fmt(m["cache_quota"]), m["cache_root"]))
        sys_tmp = m.get("system_tmp") or {}
        if sys_tmp.get("tmp_size_bytes"):
            print("  %-21s %s / %s (%s)%s" % (
                sys_tmp.get("tmp_path"), _fmt(sys_tmp.get("tmp_used_bytes")),
                _fmt(sys_tmp.get("tmp_size_bytes")), sys_tmp.get("tmp_fstype") or "?",
                " — ALERTE : au-delà de %d %%" % round(m["tmpfs_alert"] * 100)
                if (m.get("system_tmp_fraction") or 0) >= m["tmpfs_alert"] else ""))
    # en essai, les entrées de /tmp ont leur propre section (avec la commande)
    rows = [p for p in report["plan"] if p.get("kind") != "orphan"] if report["dry_run"] \
        else [e for e in report["entries"] if e["kind"] != "mesure"]
    if not rows:
        print("  rien à faire")
    for row in rows:
        print("  %-9s %-12s %-10s %s%s" % (
            row.get("kind", ""), row.get("action", ""),
            _fmt(row["bytes"]) if row.get("bytes") is not None else "",
            row.get("path", ""), " — %s" % row["detail"] if row.get("detail") else ""))
    for note in report.get("skipped") or ():
        print("  remis     %s" % note)
    conteneurs = report.get("containers") or {}
    for c in conteneurs.get("ameesh") or ():
        if c["action"] != "suppression":
            print("  conteneur %-12s %s — %s" % (c["action"], c["path"], c.get("detail") or ""))
    autres = conteneurs.get("others") or []
    if autres:
        print("  conteneurs non lancés par un tour d'ameesh (jamais supprimés par ameesh) :")
        for c in autres:
            print("    %s  %s  (%s)  →  %s" % (c.get("name") or c.get("id"), c.get("image"),
                                              c.get("status") or "?", c["command"]))
    orphans = report.get("orphans") or []
    if orphans:
        print("  entrées de /tmp laissées par un tour (supprimées seules une fois abandonnées) :")
        for o in orphans:
            print("    %s  %s  (agent %s)  %s%s  →  %s" % (
                _fmt(o["bytes"]), o["path"], o.get("agent") or "?", o.get("status") or "",
                " — %s" % o["detail"] if o.get("detail") else "", o["command"]))
    top = (report.get("measure") or {}).get("system_tmp_top") or []
    if top:
        print("  plus grosses entrées de %s :" % ((m.get("system_tmp") or {}).get("tmp_path")
                                                   or "/tmp"))
        for e in top:
            print("    %s  %s  (%s%s)  →  %s" % (
                _fmt(e.get("bytes")), e["path"], e.get("owner") or "?",
                ", laissée par %s : %s" % (e["agent"], e.get("status") or "?")
                if e.get("agent") else ", hors tour d'agent : à décider",
                e.get("command") or ""))


def cmd_menage(cfg, args) -> int:
    """`ameesh menage [--dry-run | --apply] [--json] [--by NOM]`.

    Essai par défaut : supprimer est un acte, pas un effet de bord d'une
    commande de lecture — l'exécuteur fait déjà le ménage automatique ; la
    commande sert d'abord à voir, et `--apply` à déclencher."""
    from . import canon as canon_mod
    from . import db as db_mod
    import sys

    apply = bool(getattr(args, "apply", False)) and not getattr(args, "dry_run", False)
    if apply:
        refusal = caller_refusal()
        if refusal:
            print("ameesh menage : refus — %s" % refusal, file=sys.stderr)
            return 1
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        host = cfg.host or ""
        try:
            canons = canon_mod.load_configured(cfg)
        except Exception:
            canons = []
        policy = effective(canons, host)
        busy = False
        if apply:
            try:
                busy = storage.of(db).hosts.turns_in_progress(host) > 0
            except Exception:
                busy = True
        actor = getattr(args, "by", "") or "human:%s" % getpass.getuser()
        from . import containers as containers_mod
        report = run_pass(cfg, db, policy, host=host, actor=actor, dry_run=not apply,
                          busy=busy, include_lotless=apply,
                          runtime=containers_mod.Runtime.from_config(cfg))
        if getattr(args, "json", False):
            print(json.dumps(dict(report, schema="ameesh-menage/1"), ensure_ascii=False,
                             sort_keys=True, default=str))
        else:
            print_report(report)
        return 0
    finally:
        db.close()


__all__ = ["Policy", "parse_policy", "effective", "turn_env", "hold_tmp", "Turn", "run_pass",
           "measure", "evict_lru", "worktree_state", "list_worktrees", "cmd_menage",
           "caller_refusal", "orphan_pass", "tmp_top", "tmpfs_hint", "in_memory"]
