# SPDX-License-Identifier: AGPL-3.0-only
"""Ménage de ce que les agents créent (lot L73 ; décision 0028 point 4 ; lot
de base #1, cycle de vie des worktrees).

ameesh fait le ménage de ce que SES agents créent, au lieu de le laisser à
l'humain. Trois familles, trois règles :

* **Dossier temporaire d'un agent** (jetable) : `TMPDIR`/`TMP`/`TEMP` d'un
  tour pointent vers `<tmp_root>/<agent>` (par défaut
  `<état>/.menage/tmp/<agent>`, sur disque : pas un tmpfs en mémoire). Il vit
  le temps d'une SESSION, pas d'un tour : une session reprise retrouve ses
  fichiers (bloc-notes du harnais, sorties de tests) d'un tour à l'autre. Il
  est vidé au premier tour d'une session neuve (rotation, changement de lot,
  `restart`), borné par un quota (éviction des fichiers les plus anciens à la
  fin de chaque tour) et supprimé quand l'agent n'existe plus au registre.
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

Ce qu'ameesh n'a pas créé n'est JAMAIS supprimé automatiquement : une entrée
de `/tmp` apparue pendant un tour (clone de relecture, `--cacheDirectory`
explicite, venv) est signalée (`tmp_orphan`) avec la commande à lancer, et
rien de plus. Toute suppression passe par [`_safe_rmtree`], qui refuse un
chemin hors des dossiers gérés, un lien symbolique, ou une racine sans
marqueur `.ameesh-menage`.

Tout est journalisé (`housekeeping_log`, migration 0045) et visible dans
`ameesh hosts` ; `ameesh menage [--apply]` montre ou déclenche un passage.
"""
from __future__ import annotations

import getpass
import json
import os
import shlex
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field

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

#: dossier géré sous l'état d'ameesh (un nom d'agent ne commence jamais par
#: un point : pas de collision avec `<état>/<agent>`)
MANAGED_DIR = ".menage"
#: marqueur posé par ameesh à la racine d'un dossier qu'il gère
MARKER = ".ameesh-menage"

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
               "redirect", "xdg_cache", "worktrees", "orphan_min_size")
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
    for key in ("redirect", "xdg_cache", "worktrees"):
        if raw.get(key) is None:
            continue
        if not isinstance(raw[key], bool):
            problems.append("`%s` : booléen attendu" % key)
        else:
            parsed[key] = raw[key]
    unknown = sorted(str(k) for k in set(raw) - set(POLICY_KEYS))
    return parsed, problems, unknown


def effective(canons, host: str) -> Policy:
    """Politique de ménage de `host` d'après toutes les fiches Host des canons.

    Chemins et booléens : la première fiche qui les déclare (canon par défaut
    d'abord). Quotas, taille de signalement et seuil d'alerte : le plus strict
    (le plus bas), comme les limites physiques de L43."""
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


def tmp_root(cfg, policy: Policy) -> str:
    return _expand(policy.tmp_root) if policy.tmp_root else \
        os.path.join(cfg.state_dir, MANAGED_DIR, "tmp")


def cache_root(cfg, policy: Policy) -> str:
    return _expand(policy.cache_root) if policy.cache_root else \
        os.path.join(cfg.state_dir, MANAGED_DIR, "cache")


def agent_tmp(cfg, policy: Policy, agent: str) -> str:
    return os.path.join(tmp_root(cfg, policy), agent)


def cache_names(policy: Policy) -> list[tuple[str, tuple[str, ...]]]:
    return list(CACHES) + ([XDG_CACHE] if policy.xdg_cache else [])


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


def turn_env(cfg, policy: Policy, agent: str, base_env: dict) -> dict:
    """Variables à poser dans l'environnement d'un tour (crée les dossiers).

    `TMPDIR`/`TMP`/`TEMP` sont toujours redirigés (c'est l'objet du lot) ;
    une variable de cache déjà posée dans `base_env` est respectée."""
    if not policy.redirect or disabled(base_env):
        return {}
    troot, croot = tmp_root(cfg, policy), cache_root(cfg, policy)
    _ensure_root(troot)
    _ensure_root(croot)
    mine = os.path.join(troot, agent)
    os.makedirs(mine, mode=0o700, exist_ok=True)
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
    `min_size` octets : chemin, taille, commande proposée."""
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
        out.append({"path": full, "bytes": size, "command": "rm -rf -- %s" % shlex.quote(full)})
    return out


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


def worktree_in_use(path: str, proc_root: str = "/proc") -> bool:
    """Un processus de l'hôte a-t-il son dossier courant dans ce worktree ?
    (Linux ; faux si `/proc` est illisible.)"""
    target = _real(path)
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            where = os.readlink(os.path.join(proc_root, pid, "cwd"))
        except OSError:
            continue
        if _under(where.removesuffix(" (deleted)"), target):
            return True
    return False


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

    @classmethod
    def begin(cls, cfg, policy: Policy, agent: str, cwd: str | None, *,
              new_session: bool, base_env: dict) -> "Turn":
        turn = cls(cfg=cfg, policy=policy, agent=agent, cwd=cwd, started=time.time())
        if disabled(base_env):
            turn.off = True
            return turn
        mine = agent_tmp(cfg, policy, agent)
        if new_session and policy.redirect and os.path.isdir(mine):
            freed = _clear_dir(mine, tmp_root(cfg, policy))
            if freed:
                turn.entries.append({"kind": "tmp", "action": "deleted", "path": mine,
                                     "bytes": freed, "agent": agent,
                                     "detail": "session neuve : dossier temporaire vidé"})
        turn.env = turn_env(cfg, policy, agent, base_env)
        if policy.worktrees:
            turn.worktrees_before = {w["path"] for w in list_worktrees(cwd)}
        turn.tmp_before = tmp_names()
        return turn

    def end(self, *, lot: str | None, turn_id: str, db=None, host: str = "") -> list[dict]:
        if self.off:
            return self.entries
        policy = self.policy
        if self.env:
            root = tmp_root(self.cfg, policy)
            result = evict_lru([agent_tmp(self.cfg, policy, self.agent)], root,
                               policy.tmp_quota, min_age=EVICT_MIN_AGE_S)
            if result["files"]:
                self.entries.append({
                    "kind": "tmp", "action": "evicted",
                    "path": agent_tmp(self.cfg, policy, self.agent),
                    "bytes": result["evicted"], "agent": self.agent, "lot": lot,
                    "detail": "quota %s : %d fichier(s) les plus anciens évincés"
                              % (resources_mod.fmt_value("q", policy.tmp_quota),
                                 result["files"])})
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
        for found in new_tmp_entries(self.tmp_before, min_size=policy.orphan_min_size,
                                     exclude=exclude):
            self.entries.append({
                "kind": "orphan", "action": "signaled", "path": found["path"],
                "bytes": found["bytes"], "agent": self.agent, "lot": lot,
                "detail": "apparu dans /tmp pendant le tour %s de %s (attribution probable) ; "
                          "hors des dossiers gérés : jamais supprimé automatiquement"
                          % (turn_id[:8], self.agent),
                "data": {"command": found["command"], "turn": turn_id}})
        return self.entries


# --------------------------------------------------------------------------
# un passage (exécuteur périodique, ou `ameesh menage`)
# --------------------------------------------------------------------------

def measure(cfg, policy: Policy) -> dict:
    """Occupation des dossiers gérés et du /tmp du système (lecture seule)."""
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
    return {
        "tmp_root": troot, "tmp_bytes": sum(agents.values()), "tmp_by_agent": agents,
        "tmp_quota": policy.tmp_quota,
        "tmp_root_fstype": (resources_mod.fs_usage(troot).get("tmp_fstype")
                            if os.path.isdir(troot) else None),
        "cache_root": croot, "cache_bytes": sum(caches.values()), "cache_by_name": caches,
        "cache_quota": policy.cache_quota,
        "system_tmp": system,
        "system_tmp_fraction": resources_mod.tmpfs_fraction(system),
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
    * bilan (`mesure`) et signalements de /tmp récents (rien n'y est touché).

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
    if os.path.isdir(troot) and _managed(troot):
        for name in sorted(os.listdir(troot)):
            full = os.path.join(troot, name)
            if name == MARKER or not os.path.isdir(full) or os.path.islink(full):
                continue
            if registered is not None and name not in registered:
                size = size_of(full)
                plan.append({"kind": "tmp", "path": full, "agent": name, "action": "suppression",
                             "bytes": size, "detail": "agent absent du registre"})
                if not dry_run:
                    freed = _safe_rmtree(full, troot)
                    entries.append({"kind": "tmp", "action": "deleted", "path": full,
                                    "bytes": freed, "agent": name,
                                    "detail": "agent absent du registre"})
                continue
            if name in agents_in_turn:
                continue
            result = evict_lru([full], troot, policy.tmp_quota, now=now, dry_run=dry_run)
            if result["files"]:
                plan.append({"kind": "tmp", "path": full, "agent": name, "action": "éviction",
                             "bytes": result["evicted"],
                             "detail": "%d fichier(s), quota %s" % (
                                 result["files"],
                                 resources_mod.fmt_value("q", policy.tmp_quota))})
                if not dry_run:
                    entries.append({"kind": "tmp", "action": "evicted", "path": full,
                                    "bytes": result["evicted"], "agent": name,
                                    "detail": "quota %s : %d fichier(s) évincés" % (
                                        resources_mod.fmt_value("q", policy.tmp_quota),
                                        result["files"])})
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
    # 5. signalements récents encore présents (lecture seule)
    orphans = []
    try:
        seen = set()
        for row in storage.of(db).housekeeping.recent(host, 7 * 24 * 3600.0, 500):
            if row["kind"] != "orphan" or row["path"] in seen:
                continue
            seen.add(row["path"])
            if os.path.lexists(row["path"]):
                orphans.append({"path": row["path"], "bytes": size_of(row["path"]),
                                "agent": row.get("agent"), "at_ts": row.get("at_ts"),
                                "command": (row.get("data") or {}).get("command")
                                or "rm -rf -- %s" % shlex.quote(row["path"])})
    except Exception:
        orphans = []
    report_measure = measure(cfg, policy)
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
    rows = report["plan"] if report["dry_run"] else [
        e for e in report["entries"] if e["kind"] != "mesure"]
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
        print("  hors dossiers gérés (jamais supprimé par ameesh ; à décider par un humain) :")
        for o in orphans:
            print("    %s  %s  (agent %s)  →  %s" % (_fmt(o["bytes"]), o["path"],
                                                  o.get("agent") or "?", o["command"]))


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


__all__ = ["Policy", "parse_policy", "effective", "turn_env", "Turn", "run_pass",
           "measure", "evict_lru", "worktree_state", "list_worktrees", "cmd_menage",
           "caller_refusal"]
