# SPDX-License-Identifier: AGPL-3.0-only
"""Prepared conflict resolutions between two lots (lot L160d, study L160 § 3.2).

    ameesh work resolve <lot> --with <other>            open the resolution
    ameesh work resolve <lot> --with <other> --done     commit, push, record
    ameesh work resolve <lot> --with <other> --abort    drop it
    ameesh work resolve <lot> [--json]                  resolutions of a lot

When the radar (L160b) finds two lots whose branches merge with a conflict,
one of them, the RESOLVER, prepares the resolution while it still knows its
code, long before either lot merges:

1. `resolve` adds a throwaway worktree of the lot branch (detached, locked,
   under `<git common dir>/ameesh-resolve/<lot>-<other>`) and merges the
   other branch into it, `rerere` off. When the pair was resolved before,
   that merge is replayed in a scratch clone whose empty `rr-cache` is
   primed with that one resolution, and the files `rerere` resolved there are
   copied into the worktree: hunks already resolved come back resolved. The
   agents' repository never gets an `rr-cache` (one would turn `rerere` on
   for their own merges);
2. the agent resolves the remaining conflicts in that worktree (a new file
   is `git add`-ed: `--done` refuses untracked files);
3. `resolve --done` checks that no conflict marker is left, commits the
   merge (trailer `ameesh-resolution: <lot> <other>`), points
   `refs/ameesh/resolutions/<lot>/<other>` at it, pushes it there on
   `origin` (or to the branch `ameesh/resolve/<lot>-<other>` when the host
   refuses refs outside `refs/heads`; never forced over a branch it did
   not push), records it (table `work_resolutions`, migration 0057) and
   removes the worktree, only when clean.

The lot branch never changes: it stays the version WITHOUT the other lot;
the resolution commit R(lot, other) is the version WITH it.

Validity, at each radar pass: in its private clone of the project
(`<state_dir>/radar/<project>.clone`, `git clone --shared`: objects read
from the agents' repository, nothing written to it; its `origin` is the
forge, never the checkout's local branches, #3001), the radar replays the
merge of the two current tips with an `rr-cache` holding the pair's own
resolution only (trained once per resolution commit with git's
`rerere-train.sh`, contrib, or a port of the same loop when the script is
not installed, then kept aside: another pair's resolution, or a replaced
one, never resolves this pair). The replay reads `.gitattributes` from the
target's tree (`attr.tree`), like the radar's trial merge and the landing
merge, so a `merge=union` carried by a single branch does not decide it
(#2189). If `rerere` resolves every conflicted path, the pair is
`prepared`; otherwise it is `stale`, and the lot that prepared it is told
(passive mail, L160c). A rebase or a new push
that leaves the conflicting hunks as they were keeps the pair `prepared`:
`rerere` recognises content, not commits.

Resolver: the lot expected to merge second. Until the merge queue (L160e)
gives the order, it is guessed: the lot with the higher priority merges
first; then the one further along (`qa` before `build` before the rest);
then the lot with the smaller change in the conflicting files resolves;
then the newer lot. The radar names the resolver in its mail.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager

from . import migrations, plan_git, review_provenance, storage, work
from .platform.process import git_process_in
from .canon import (FETCH_TIMEOUT, GIT_TIMEOUT, NO_LAZY_FETCH, GitError, _git,
                    _git_env)

REF_PREFIX = "refs/ameesh/resolutions/"
#: un `owner/name` GitHub : une option `--repo` peut le porter, mais ce n'est
#: jamais un dossier de travail (#2349).
_GITHUB_KEY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
#: fallback for hosts that refuse refs outside `refs/heads`
BRANCH_PREFIX = "ameesh/resolve/"
TRAILER = "ameesh-resolution"
# v3 keeps both merge directions and retrains the checkout -m preimages that
# v2 (#2136) could cache before #2165 fixed the Python trainer.
TRAINING_CACHE = "ameesh-rr-v3"
# v4 is trained with the target's attributes (`attr.tree`), like
# `radar.trial_merge` and a landing merge: a cache trained from a branch that
# carries `merge=union` alone does not cover the conflicts the target sees, and
# the other way round (#2189). Land and the merge queue keep TRAINING_CACHE.
TRAINING_CACHE_TARGET = "ameesh-rr-v4"
#: worktrees of open resolutions, under the repository's common directory
WORKTREE_DIR = "ameesh-resolve"
#: where git ships `rerere-train.sh`, relative to `git --exec-path` or absolute
RERERE_TRAIN = (
    "../../share/doc/git/contrib/rerere-train.sh",
    "../../share/git-core/contrib/rerere-train.sh",
    "../../share/git/contrib/rerere-train.sh",
    "/usr/share/doc/git/contrib/rerere-train.sh",
    "/usr/share/git-core/contrib/rerere-train.sh",
    "/usr/local/share/git-core/contrib/rerere-train.sh",
    "/opt/homebrew/share/git-core/contrib/rerere-train.sh",
)
_MARKER_RE = re.compile(rb"^(<{7} |>{7} |={7}$|\|{7} )", re.M)
#: how far along a lot is, to guess which of two lots merges first
_STATE_RANK = {lifecycle.VALIDATED: 3, lifecycle.QA: 2, lifecycle.IN_PROGRESS: 1}


class ResolveError(RuntimeError):
    """A resolution that cannot be opened or recorded: actionable message."""


class StaleTarget(GitError):
    """#3001 : la cible du clone radar n'est pas la pointe de la forge — forge
    illisible, ou ref récupérée plus ancienne que celle que publie
    `git ls-remote`. La cible est refusée, jamais lue à la place."""


class CloneBusy(GitError):
    """The radar clone's worktree is held by another operation (#2428).

    Nothing was read nor written: the caller defers to its next pass. A
    stacked trial that finds the clone busy is not an ejection, and an
    operation it could not run is never cached as a result."""


#: #3001 : le suivi du clone radar quand il lit la forge (son `origin`)
FORGE_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
_FORGE_REFS = "refs/remotes/origin/"


#: how long an operation waits for the clone's worktree before deferring.
#: A landing gate can hold it for a while; a radar pass or a turn that does
#: not get it retries at its next pass (`CloneBusy`), never ejects a lot.
CLONE_LOCK_WAIT_S = float(os.environ.get("AMEESH_CLONE_LOCK_WAIT", "60"))


def _local_from(repo: str, url: str) -> str:
    """#3001 : `url` lue depuis `repo` ; un chemin local relatif devient absolu.

    La règle de Git (`url_is_local_not_ssh`) : sans `://`, une adresse est un
    chemin local s'il n'y a pas de `:` ou si un `/` le précède ; sinon c'est
    la syntaxe scp d'une forge distante.

    #3041 : `repo` est résolu physiquement (`realpath`), comme Git résout `..`
    depuis la cible d'un checkout symlinké ; `abspath` lirait le voisin du
    lien, un leurre possible."""
    url = (url or "").strip()
    if not url or "://" in url:
        return url
    colon, slash = url.find(":"), url.find("/")
    if colon >= 0 and (slash < 0 or colon < slash):
        return url                      # git@hôte:chemin
    if os.path.isabs(url):
        return url
    return os.path.normpath(os.path.join(os.path.realpath(repo), url))


def ref_name(lot: int, other: int) -> str:
    return "%s%d/%d" % (REF_PREFIX, lot, other)


def branch_name(lot: int, other: int) -> str:
    return "%s%d-%d" % (BRANCH_PREFIX, lot, other)


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def _run(cwd: str, *args: str, timeout: float = GIT_TIMEOUT,
         stdin: bytes | None = None,
         env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """git dans `cwd`. #3181 : un fetch que fait échouer une corruption locale
    (`local_corruption`) est refait une fois, après la mise en quarantaine des
    fichiers vides du clone (`quarantine_empty`) ; si rien n'a été déplacé ou
    si l'erreur persiste, l'échec est rendu tel quel, à lire par
    `fetch_failure`."""
    proc = _git(cwd, *args, stdin=stdin, timeout=timeout, env_extra=env_extra)
    if (proc.returncode and _subcommand(args) == "fetch"
            and local_corruption(_err(proc)) and quarantine_empty(cwd)):
        proc = _git(cwd, *args, stdin=stdin, timeout=timeout, env_extra=env_extra)
    return proc


#: #3181 : ce que Git écrit quand le clone ou ses alternates portent un objet
#: ou une ref abîmés (coupure de courant pendant une écriture) ; jamais une
#: forge injoignable, et une attente ne le résorbe pas.
_LOCAL_CORRUPTION = re.compile(
    r"object file \S+ is empty|bad object (?:refs/|HEAD)|badRefContent"
    r"|loose object \S+ .*is corrupt|did not send all necessary objects")
#: un objet libre (`objects/xx/<reste du SHA-1 ou SHA-256>`) ; les fichiers
#: temporaires de Git (`tmp_obj_*`) ne le sont pas
_LOOSE_OBJECT = re.compile(r"[0-9a-f]{38}|[0-9a-f]{62}")
#: où vont les fichiers vides retirés, sous le dossier `.git` qui les portait
QUARANTINE_DIR = "ameesh-quarantine"


def _subcommand(args) -> str:
    """La sous-commande git de `args`, après les `-c clé=valeur` et options."""
    it = iter(args)
    for arg in it:
        if arg in ("-c", "-C"):
            next(it, None)
        elif not arg.startswith("-"):
            return arg
    return ""


def local_corruption(message: str) -> bool:
    """#3181 : l'échec de Git vient-il d'un objet ou d'une ref abîmés du clone ?"""
    return bool(_LOCAL_CORRUPTION.search(message or ""))


def fetch_failure(message: str) -> str:
    """#3181 : l'échec d'un fetch, nommé : une corruption locale n'est jamais
    lue comme une forge qui ne répond pas."""
    if local_corruption(message):
        return "local clone corrupted (unclean shutdown?): %s" % message
    return message


def _object_dirs(objects: str) -> list[str]:
    """`objects` et ses alternates (récursivement) : un clone `--shared` lit
    les objets du checkout, et un objet vide chez celui-ci casse le fetch du
    clone comme le sien propre."""
    seen: list[str] = []
    todo = [os.path.realpath(objects)]
    while todo:
        current = todo.pop(0)
        if current in seen or not os.path.isdir(current):
            continue
        seen.append(current)
        try:
            with open(os.path.join(current, "info", "alternates"), encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                todo.append(os.path.realpath(os.path.join(current, line)))
    return seen


def _empty_files(objects: str, git_dir: str | None) -> list[str]:
    """Les objets libres de 0 octet de `objects`, et les refs libres de
    0 octet de `git_dir` (le clone seul : jamais les refs d'un alternate).
    Un fichier non vide n'est jamais rendu, même illisible."""
    found = []
    try:
        fans = [e for e in os.scandir(objects)
                if len(e.name) == 2 and e.is_dir(follow_symlinks=False)]
    except OSError:
        fans = []
    for fan in fans:
        try:
            entries = list(os.scandir(fan.path))
        except OSError:
            continue
        found += [e.path for e in entries
                  if _LOOSE_OBJECT.fullmatch(e.name) and e.is_file(follow_symlinks=False)
                  and e.stat(follow_symlinks=False).st_size == 0]
    if git_dir:
        for root, _dirs, files in os.walk(os.path.join(git_dir, "refs")):
            for name in files:
                path = os.path.join(root, name)
                # une ref en cours d'écriture passe par `<ref>.lock`
                if not name.endswith(".lock") and not os.path.islink(path) \
                        and os.path.getsize(path) == 0:
                    found.append(path)
    return sorted(found)


def _move_ref(source: str, target: str) -> str:
    """Déplace la ref libre `source` vers `target` sous le verrou de Git
    (`<ref>.lock` créé en O_EXCL, comme `update-ref`) : aucun écrivain ne
    peut la remplacer entre la relecture de sa taille et le déplacement. Rend
    pourquoi elle reste en place, vide si elle a été déplacée."""
    lock = source + ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return "a git writer holds %s" % lock
    try:
        if os.lstat(source).st_size != 0:
            return "no longer empty"
        os.rename(source, target)
        return ""
    finally:
        os.close(fd)
        os.unlink(lock)


def _move_object(source: str, target: str) -> str:
    """Déplace l'objet libre `source` vers `target`, sous le verrou de
    quarantaine de son dépôt (`_store_lock`).

    La preuve de sûreté tient à Git lui-même, pas à sa configuration : un
    écrivain (`hash-object -w`, `unpack-objects`, fetch) teste d'abord la
    présence du fichier (`freshen_loose_object`, `access(F_OK)`) et n'écrit
    rien s'il est là, même vide, quel que soit `core.createObject` — c'est ce
    qui rendait l'incident permanent (témoin `test_git_never_rewrites_a_present
    _empty_object`). Tant que le fichier vide est à son chemin, aucun écrivain
    ne peut donc y mettre un objet complet ; seul un autre déménageur le
    pourrait, et le verrou exclut les autres passes. Défenses en plus : refus
    si un écrivain visible est configuré en `rename` (`_replacing_writer`), et
    relecture après déplacement — un objet complet emporté (déménageur hors
    ameesh, geste à la main) est remis par un lien qui n'écrase rien, et un
    échec de la remise est nommé avec l'endroit où l'objet se trouve. Rend
    pourquoi l'objet n'est pas en quarantaine, vide s'il l'est."""
    if os.lstat(source).st_size != 0:
        return "no longer empty"
    os.rename(source, target)
    if os.lstat(target).st_size == 0:
        return ""
    try:
        os.link(target, source)
    except FileExistsError:
        # un autre écrivain l'a déjà réécrit : la copie déplacée reste en
        # quarantaine (rien n'est supprimé), le chemin porte un objet
        return "replaced concurrently, rewritten again at its path; copy kept in %s" % target
    except OSError as exc:
        return ("moved away although replaced concurrently, and NOT restored (%s): the "
                "valid object is only at %s, link it back to %s" % (exc, target, source))
    os.unlink(target)
    return "replaced concurrently, restored at its path"


#: combien une passe attend la quarantaine d'un dépôt tenue par une autre
QUARANTINE_LOCK_WAIT_S = 30.0


def _store_lock(git_dir: str) -> int | None:
    """Le verrou de quarantaine du dépôt `git_dir` (`flock` sur
    `<.git>/ameesh-quarantine.lock`), tenu du scan au dernier déplacement : deux
    passes (clone radar et checkout qu'il lit par alternates, ou deux hôtes de
    file) ne déplacent jamais un objet qu'une autre a vu vide puis que Git a
    réécrit. Attente bornée ; None si le verrou n'est pas obtenu : rien n'est
    alors déplacé dans ce dépôt."""
    try:
        fd = os.open(os.path.join(git_dir, QUARANTINE_DIR + ".lock"),
                     os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    deadline = time.monotonic() + QUARANTINE_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)
        except OSError:
            os.close(fd)
            return None


def _replacing_writer(git_dir: str) -> bool:
    """Un écrivain Git de ce dépôt écrit-il ses objets par `rename()` (qui
    remplace un fichier présent) plutôt que par `link()` ? Le magasin d'objets
    est partagé par le dépôt et tous ses worktrees liés : la configuration
    effective de **chacun** est lue (`config.worktree` avec
    `extensions.worktreeConfig`, includes, global, système). Un seul `rename`,
    une lecture impossible ou un worktree illisible : oui (refus fermé).
    Défense en plus seulement : la configuration d'un autre utilisateur ou d'un
    `-c` n'est pas lisible d'ici, et la sûreté ne repose pas sur elle (Git ne
    réécrit jamais un objet présent, voir `_move_object`)."""
    gitdirs = [git_dir]
    worktrees = os.path.join(git_dir, "worktrees")
    try:
        names = sorted(os.listdir(worktrees)) if os.path.isdir(worktrees) else []
    except OSError:
        return True
    gitdirs += [os.path.join(worktrees, n) for n in names
                if os.path.isdir(os.path.join(worktrees, n))]
    for one in gitdirs:
        try:
            proc = _git(git_dir, "--git-dir", one, "config", "--get-all", "core.createObject")
        except GitError:
            return True
        if proc.returncode not in (0, 1):
            return True
        if any(v.strip().lower() == "rename" for v in _text(proc).splitlines()):
            return True
    return False


def quarantine_empty(path: str) -> list[str]:
    """#3181 : déplace les objets libres de 0 octet du clone `path` et de ses
    alternates, et ses refs libres de 0 octet, vers
    `<.git>/ameesh-quarantine/<date UTC>-<unique>/` du dépôt qui les portait.

    Un fichier vide n'est jamais un objet ni une ref valides : c'est le reste
    d'une écriture coupée (coupure de courant), que Git prend pour un objet
    présent et ne redemande pas. Rien n'est supprimé, aucun fichier non vide
    n'est déplacé, ni `gc` ni `prune` : chaque dépôt est traité sous son
    verrou de quarantaine (`_store_lock`, du scan au dernier déplacement), une
    ref est relue sous son verrou Git (`_move_ref`), un objet n'est déplacé
    que si Git ne peut pas le remplacer (`_replacing_writer`, `_move_object`).
    Chaque appel a son propre dossier de quarantaine :
    deux passes de la même seconde n'écrasent aucune preuve. Chaque
    déplacement (ou refus) est journalisé (`moved.log` de la quarantaine, et
    stderr). Rend les chemins déplacés."""
    common = plan_git._out(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common or not os.path.isdir(common):
        return []
    common = os.path.realpath(common)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    moved: list[str] = []
    for objects in _object_dirs(os.path.join(common, "objects")):
        git_dir = os.path.dirname(objects)
        lock = _store_lock(git_dir)
        if lock is None:
            print("ameesh: #3181 quarantine of %s busy or unreadable: nothing moved there"
                  % git_dir, file=sys.stderr)
            continue
        try:
            moved += _quarantine_store(objects, git_dir, common, stamp)
        finally:
            os.close(lock)  # rend le flock
    return moved


def _quarantine_store(objects: str, git_dir: str, common: str, stamp: str) -> list[str]:
    """Le corps de `quarantine_empty` pour un dépôt, sous son verrou."""
    moved: list[str] = []
    found = _empty_files(objects, common if git_dir == common else None)
    if not found:
        return moved
    try:
        os.makedirs(os.path.join(git_dir, QUARANTINE_DIR), exist_ok=True)
        box = tempfile.mkdtemp(prefix=stamp + "-", dir=os.path.join(git_dir, QUARANTINE_DIR))
    except OSError as exc:
        print("ameesh: #3181 cannot open a quarantine in %s: %s" % (git_dir, exc),
              file=sys.stderr)
        return moved
    refs = os.path.join(git_dir, "refs") + os.sep
    replacing = None
    for source in found:
        target = os.path.join(box, os.path.relpath(source, git_dir))
        is_ref = source.startswith(refs)
        if not is_ref and replacing is None:
            replacing = _replacing_writer(git_dir)
        try:
            if not is_ref and replacing:
                kept = ("core.createObject=rename (or unreadable): a git writer may "
                        "replace it while it moves, repair by hand")
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                kept = (_move_ref if is_ref else _move_object)(source, target)
        except OSError as exc:
            kept = "error: %s" % exc
        line = ("%s -> %s" % (source, target) if not kept
                else "%s not quarantined: %s" % (source, kept))
        try:
            with open(os.path.join(box, "moved.log"), "a", encoding="utf-8") as log:
                log.write(line + "\n")
        except OSError:
            pass
        print("ameesh: #3181 %s%s" % ("" if kept else "quarantined empty ", line),
              file=sys.stderr)
        if not kept:
            moved.append(source)
    return moved


def _text(proc) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def _err(proc) -> str:
    return (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()


def _check(proc, what: str) -> str:
    if proc.returncode != 0:
        raise GitError("git %s: %s" % (what, _err(proc)))
    return _text(proc)


def _paths(proc) -> list[str]:
    return sorted({p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p})


def unmerged(cwd: str) -> list[str]:
    return _paths(_run(cwd, "diff", "--name-only", "--diff-filter=U", "-z"))


def _merge_rr(cwd: str) -> bool:
    """`test -s "$GIT_DIR/MERGE_RR"`: a conflicted merge left something to learn."""
    path = _text(_run(cwd, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_RR"))
    return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0


def _restore_rr(cache: str, stash: str) -> None:
    """Puts the hidden `rr-cache` back under its name: the entries the trial
    merge recorded win over the stashed ones (same conflict, fresher record),
    the others are kept, and the stash goes away."""
    os.makedirs(cache, exist_ok=True)
    for name in os.listdir(stash):
        src, dst = os.path.join(stash, name), os.path.join(cache, name)
        if os.path.exists(dst):
            shutil.rmtree(src, ignore_errors=True)
        else:
            os.rename(src, dst)
    shutil.rmtree(stash, ignore_errors=True)


def rerere_remaining(cwd: str) -> list[str]:
    """Conflicted paths that `rerere` did not resolve."""
    proc = _run(cwd, "-c", "rerere.enabled=true", "rerere", "remaining")
    return sorted({p for p in _text(proc).splitlines() if p}) if proc.returncode == 0 \
        else unmerged(cwd)


def exclude_generated(cwd: str, files, generated) -> None:
    """Remove generated conflicts from the index before invoking rerere.

    The target version is only a placeholder; land regenerates it before
    committing. For a deletion on the target side there is no placeholder.
    """
    for path in sorted(set(files) & set(generated)):
        ours = _run(cwd, "checkout", "--ours", "--", path)
        if ours.returncode == 0:
            _check(_run(cwd, "add", "--", path), "add")
        else:
            _check(_run(cwd, "rm", "-f", "--", path), "rm")


def with_markers(cwd: str, paths) -> list[str]:
    """Those of `paths` that still hold a conflict marker."""
    out = []
    for path in paths:
        try:
            with open(os.path.join(cwd, path), "rb") as fh:
                if _MARKER_RE.search(fh.read()):
                    out.append(path)
        except (IsADirectoryError, FileNotFoundError):
            continue
    return out


def rerere_train_script() -> str | None:
    """git's `contrib/rerere-train.sh`, if installed."""
    exec_path = ""
    try:
        exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                                   timeout=GIT_TIMEOUT, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    for candidate in RERERE_TRAIN:
        path = candidate if os.path.isabs(candidate) or not exec_path \
            else os.path.normpath(os.path.join(exec_path, candidate))
        if os.path.isfile(path):
            return path
    return None


def _parents(cwd: str, commit: str) -> list[str]:
    return _text(_run(cwd, "rev-parse", commit + "^@")).split()


def _attr_args(attributes_tree: str | None) -> list[str]:
    """The git arguments that read `.gitattributes` from `attributes_tree`
    instead of the checked-out tree (`git merge-tree` and `git merge` honour
    the same `attr.tree`, git >= 2.40)."""
    return ["-c", "attr.tree=%s" % attributes_tree] if attributes_tree else []


def _attr_env(env: dict, attributes_tree: str | None) -> dict:
    """`_attr_args` for a child process (rerere-train.sh runs its own git)."""
    if attributes_tree:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="attr.tree",
                   GIT_CONFIG_VALUE_0=attributes_tree)
    return env


def _learn(cwd: str, commit: str, first: str, others, *,
           attributes_tree: str | None = None) -> bool:
    """Plays the merge of `others` into `first` and records, in `rr-cache`, the
    resolution the tree of `commit` carries when that merge conflicts. Returns
    whether it learnt from a conflict (a clean merge has none to record).
    Leaves `cwd` clean on a detached `first`; a failure is cleaned up by the
    caller, which also restores the commit `cwd` was on.

    The trial merge must render the conflicts itself: with the cache in place
    it would replay a recorded resolution into the files, and `rerere` would
    record that stale content as the new preimage (#2165). The cache is put
    back before `forget`, which drops the pair's entries (`--overwrite`) while
    the other pairs' entries survive."""
    cache = _check(_run(cwd, "rev-parse", "--path-format=absolute", "--git-path",
                        "rr-cache"), "rev-parse")
    _check(_run(cwd, "checkout", "-q", "-f", "--detach", first), "checkout")
    stash = "%s.learn-%d" % (cache, os.getpid())
    hidden = os.path.isdir(cache) and not os.path.isdir(stash)
    if hidden:
        os.rename(cache, stash)
    try:
        merge = _run(cwd, "-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=false",
                     *_attr_args(attributes_tree),
                     "merge", "-q", "--no-ff", "--no-commit", *others)
    finally:
        if hidden and os.path.isdir(stash):
            _restore_rr(cache, stash)
    if merge.returncode == 0:
        _run(cwd, "reset", "-q", "--hard")
        return False
    # `forget` exits 0 even when it did nothing (rerere off): the MERGE_RR
    # gate below tells whether a conflict is left to learn from, exactly as
    # `test -s "$GIT_DIR/MERGE_RR"` does in rerere-train.sh (#2165).
    _run(cwd, "-c", "rerere.enabled=true", "rerere", "forget", ".")
    if not _merge_rr(cwd):
        _run(cwd, "reset", "-q", "--hard")
        return False
    # the preimage is the conflict the merge just rendered: never re-render it
    # with `checkout -m`, whose markers ("ours"/"theirs") differ from the
    # merge's ("HEAD"/branch) and miss at replay (#2165).
    _check(_run(cwd, "-c", "rerere.enabled=true", "rerere"), "rerere")
    _check(_run(cwd, "checkout", commit, "--", "."), "checkout")
    _check(_run(cwd, "-c", "rerere.enabled=true", "rerere"), "rerere")
    _run(cwd, "reset", "-q", "--hard")
    return True


def train(cwd: str, commits, *, script: str | None = "auto",
          attributes_tree: str | None = None) -> list[str]:
    """Primes the `rr-cache` of the repository of worktree `cwd` from the
    merge commits `commits` (`--overwrite`: a replaced resolution wins),
    with `rerere-train.sh`, or the same loop in Python when the script is
    not installed (`script=None` forces the port). Leaves `cwd` clean, on
    the commit it was on. Returns the commits it learnt from.

    #2136 : a merge is not symmetric — a `.gitattributes` a merge brings in
    only drives that merge once it is checked out (so `merge=union` applies
    on one side only), so the conflicted paths can differ in each direction.
    The script and the loop only learn the first-parent direction; the other
    is learnt here from the same commit, so a resolution prepared in one
    direction replays in the landing direction of the queue.

    #2165 : the Python port hides the cache during the trial merge, so
    `--overwrite` really replaces a resolution the merge would have replayed
    from the cache; it records the preimage the merge rendered, never one
    re-rendered by `checkout -m`.

    #2189 : `attributes_tree` makes the trial merges read `.gitattributes`
    from the target, exactly like the replay and `radar.trial_merge`, so a
    conflict that only the target's attributes create is learnt too. The
    first-parent direction of rerere-train.sh gets it through the git
    environment (`GIT_CONFIG_*`), its reverse through `_learn`."""
    commits = [c for c in commits if c]
    if not commits:
        return []
    cache = _check(_run(cwd, "rev-parse", "--path-format=absolute", "--git-path",
                        "rr-cache"), "rev-parse")
    os.makedirs(cache, exist_ok=True)
    if script == "auto":
        script = rerere_train_script()
    head = _check(_run(cwd, "rev-parse", "HEAD"), "rev-parse")
    learnt = []
    try:
        if script:
            env = _attr_env(_git_env(), attributes_tree)
            proc = subprocess.run(["sh", script, "--overwrite", "--no-walk", *commits],
                                  cwd=cwd, capture_output=True, env=env,
                                  timeout=FETCH_TIMEOUT, check=False)
            out = proc.stdout.decode("utf-8", "replace")
            found = re.findall(r"^Learning from ([0-9a-f]+)", out, re.M)
            learnt = [c for c in commits if any(c.startswith(short) for short in found)]
        else:
            for commit in commits:
                parents = _parents(cwd, commit)
                if len(parents) < 2:
                    continue
                if _learn(cwd, commit, parents[0], parents[1:],
                          attributes_tree=attributes_tree):
                    learnt.append(commit)
        for commit in commits:
            parents = _parents(cwd, commit)
            if len(parents) == 2 and _learn(cwd, commit, parents[1], [parents[0]],
                                            attributes_tree=attributes_tree):
                learnt.append(commit)
    finally:
        _run(cwd, "reset", "-q", "--hard")
        _run(cwd, "checkout", "-q", "-f", "--detach", head)
    seen = set(learnt)
    return [c for c in commits if c in seen]


# --------------------------------------------------------------------------
# resolver
# --------------------------------------------------------------------------

def _priority(row: dict) -> int:
    return int(row.get("priority") or 2)


def change_size(repo: str, base: str, tip: str, files) -> int:
    """Lines added and removed between `base` and `tip` in `files`."""
    proc = _run(repo, "diff", "--numstat", "--no-renames", base, tip, "--", *files)
    total = 0
    for line in _text(proc).splitlines() if proc.returncode == 0 else ():
        added, removed = (line.split("\t") + ["", ""])[:2]
        total += (int(added) if added.isdigit() else 0) + \
            (int(removed) if removed.isdigit() else 0)
    return total


def choose_resolver(a: dict, b: dict, files, *, repo: str | None = None,
                    sizes: dict | None = None) -> dict:
    """The lot of the pair (`a`, `b`: `{work_item, priority?, state?, base,
    tip}`) that prepares the resolution: the one expected to merge second.
    Returns `{lot, reason}`."""
    pa, pb = _priority(a), _priority(b)
    if pa != pb:
        return {"lot": (b if pa < pb else a)["work_item"], "reason": "priority"}
    ra, rb = _STATE_RANK.get(a.get("state") or "", 0), _STATE_RANK.get(b.get("state") or "", 0)
    if ra != rb:
        return {"lot": (b if ra > rb else a)["work_item"], "reason": "state"}
    if repo and files:
        sizes = sizes if sizes is not None else {}
        for one in (a, b):
            key = (one["base"], one["tip"], tuple(files))
            if key not in sizes:
                sizes[key] = change_size(repo, one["base"], one["tip"], files)
        sa = sizes[(a["base"], a["tip"], tuple(files))]
        sb = sizes[(b["base"], b["tip"], tuple(files))]
        if sa != sb:
            return {"lot": (a if sa < sb else b)["work_item"], "reason": "smaller change"}
    return {"lot": max(a["work_item"], b["work_item"]), "reason": "newer lot"}


# --------------------------------------------------------------------------
# ameesh work resolve: the agent's side
# --------------------------------------------------------------------------

def repo_root(repo: str | None) -> str:
    """La racine du dépôt désigné par `--repo` (ou le dossier courant).

    `--repo` désigne un **dossier** de travail ; un `owner/name` GitHub n'est
    pas un chemin. Il est accepté quand il nomme l'`origin` du dossier courant
    (on travaille alors dans ce dépôt), sinon refusé avec un message
    explicite : un dossier absent faisait répondre « git not found » (#2349).
    """
    wanted = str(repo or "").strip()
    if wanted:
        path = os.path.expanduser(wanted)
        if os.path.isdir(path):
            wanted = path
        else:
            origin = migrations.origin_repo(os.getcwd())
            if _GITHUB_KEY.fullmatch(wanted) and migrations.repo_key(wanted) == origin:
                wanted = os.getcwd()
            else:
                raise ResolveError(
                    "--repo %s is not a local directory: pass the checkout's path, or run "
                    "from its repository (a GitHub owner/name is accepted only for the "
                    "current checkout, whose origin is %s)" % (wanted, origin or "none"))
    else:
        wanted = os.getcwd()
    try:
        root = plan_git._out(wanted, "rev-parse", "--show-toplevel")
    except GitError as exc:
        raise ResolveError("cannot read the repository at %s: %s" % (wanted, exc)) from exc
    if not root:
        raise ResolveError("%s is not inside a git repository: pass --repo" % wanted)
    return root


def worktree_path(repo: str, lot: int, other: int) -> str:
    common = _check(_run(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    "rev-parse")
    return os.path.join(common, WORKTREE_DIR, "%d-%d" % (lot, other))


def _meta_path(path: str) -> str:
    return path + ".json"


def _branch_tip(repo: str, branch: str, *, fetch: bool) -> str | None:
    """The tip of a lot branch: local, else `origin/<branch>`, refreshed
    from `origin` first when the branch is not local (another host)."""
    if fetch and not plan_git._commit(repo, "refs/heads/" + branch):
        _run(repo, "fetch", "--quiet", "origin",
             "+refs/heads/%s:refs/remotes/origin/%s" % (branch, branch), timeout=FETCH_TIMEOUT)
    return plan_git._tip(repo, branch)


def pair(db, lot: int, other: int, repo: str, *, fetch: bool = True) -> dict:
    """Both lots, their branches and tips; ResolveError when they cannot be
    merged together."""
    if lot == other:
        raise ResolveError("a lot does not conflict with itself")
    rows = {}
    for n in (lot, other):
        row = work.get(db, n)
        if row is None:
            raise ResolveError("lot #%d not found" % n)
        if not row.get("branch"):
            raise ResolveError("lot #%d has no branch (ameesh work assign %d <agent> "
                               "--branch …)" % (n, n))
        rows[n] = row
    default = None
    targets = {}
    for n, row in rows.items():
        if not row.get("branch_target") and default is None:
            default = plan_git.default_target(repo)
        targets[n] = row.get("branch_target") or default
    # #1802 : `main` et `origin/main` (le défaut du dépôt) désignent la même
    # branche : comparer les formes courtes, pas les libellés enregistrés.
    if plan_git.short_branch(targets[lot]) != plan_git.short_branch(targets[other]):
        raise ResolveError("lots #%d and #%d do not share a target (%s, %s): their branches "
                           "never merge together" % (lot, other, targets[lot], targets[other]))
    target = default or targets[lot]
    # #2189 : the landing merge and the radar's trial merge read the target's
    # `.gitattributes`; preparation and replay must use the same tree, else a
    # resolution is prepared against attributes no merge will use.
    target_head = None
    if target:
        try:
            from .radar import target_commit
            target_head, _ref, _branch, _remote = target_commit(repo, target)
        except (GitError, OSError):
            target_head = None
    tips = {}
    for n, row in rows.items():
        tips[n] = _branch_tip(repo, row["branch"], fetch=fetch and n == other)
        if not tips[n]:
            raise ResolveError("branch %s of lot #%d is neither local nor on origin"
                               % (row["branch"], n))
    return {"lot": rows[lot], "other": rows[other], "target": target,
            "target_head": target_head,
            "lot_tip": tips[lot], "other_tip": tips[other]}


def _recorded(db, lot: int, other: int) -> list[dict]:
    """Recorded resolutions of the pair, either way round, newest first."""
    return [r for r in storage.of(db).work.resolutions([lot])
            if {int(r["work_item_id"]), int(r["other_item_id"])} == {lot, other}]


def _ensure_commit(repo: str, row: dict) -> bool:
    """Makes the recorded resolution commit available locally (fetched from
    `origin` when it was prepared on another host)."""
    sha = row["commit_sha"]
    # #1749 : les deux sondes de présence restent locales ; le fetch explicite
    # ci-dessous est le seul pas réseau.
    if _run(repo, "cat-file", "-e", sha + "^{commit}",
            env_extra=NO_LAZY_FETCH).returncode == 0:
        return True
    if row.get("ref"):
        source = row["ref"] if row["ref"].startswith("refs/") else "refs/heads/" + row["ref"]
        _run(repo, "fetch", "--quiet", "origin", source, timeout=FETCH_TIMEOUT)
    return _run(repo, "cat-file", "-e", sha + "^{commit}",
                env_extra=NO_LAZY_FETCH).returncode == 0


def _status(path: str) -> list[str]:
    """`git status --porcelain` of the worktree, untracked and ignored files
    included (`!!`): removing the worktree would lose them too."""
    proc = _run(path, "status", "--porcelain", "-z", "--untracked-files=all", "--ignored")
    _check(proc, "status")
    return [e for e in proc.stdout.decode("utf-8", "replace").split("\0") if e[2:3] == " "]


def _admin_cwd(repo: str, path: str) -> str:
    """A cwd that survives the removal of `path` (#2142). Quand `--done` est
    lancé depuis le worktree de résolution, `repo` *est* `path` : le
    `git worktree prune` qui suit la suppression tournait alors dans un dossier
    disparu et git répondait `git not found`, après l'enregistrement de la
    résolution. On administre depuis le worktree principal, qui survit."""
    if os.path.realpath(repo) != os.path.realpath(path):
        return repo
    proc = _run(repo, "worktree", "list", "--porcelain")
    if proc.returncode == 0:
        for line in _text(proc).splitlines():
            if line.startswith("worktree ") and line[len("worktree "):].strip():
                main = line[len("worktree "):].strip()
                if os.path.isdir(main) and os.path.realpath(main) != os.path.realpath(path):
                    return main
                break
    parent = os.path.dirname(path)
    return parent if os.path.isdir(parent) else repo


def _remove_worktree(repo: str, path: str, *, force: bool = True) -> None:
    """Removes the worktree and its metadata. `force=False` (after `--done`):
    only a clean worktree, else ResolveError and nothing removed."""
    if not force and os.path.isdir(path):
        dirty = _status(path)
        if dirty:
            raise ResolveError("worktree %s not removed, it holds changes: %s; keep them, "
                               "or drop the worktree with: git worktree remove -f -f %s"
                               % (path, ", ".join(e[3:] for e in dirty[:10]), path))
    admin = _admin_cwd(repo, path)
    _run(admin, "worktree", "unlock", path)
    if not force:
        # no --force, no rmtree: git refuses a worktree that changed since the check
        proc = _run(admin, "worktree", "remove", path)
        if proc.returncode != 0 and os.path.isdir(path):
            _run(admin, "worktree", "lock", "--reason", "ameesh work resolve", path)
            raise ResolveError("worktree %s not removed: %s; drop it with: "
                               "git worktree remove -f -f %s" % (path, _err(proc), path))
    else:
        _run(admin, "worktree", "remove", "--force", path)
        shutil.rmtree(path, ignore_errors=True)
    _run(admin, "worktree", "prune")
    try:
        os.unlink(_meta_path(path))
    except FileNotFoundError:
        pass


def _read_meta(path: str) -> dict | None:
    try:
        with open(_meta_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and os.path.isdir(path) else None


def _current_resolution(db, lot: int, other: int, lot_tip: str,
                        other_tip: str) -> dict | None:
    """La résolution enregistrée de la paire qui couvre les deux têtes
    courantes, dans un sens ou dans l'autre (`prepared` du radar), la plus
    récente d'abord."""
    for row in _recorded(db, lot, other):
        mine, theirs = ((row["lot_tip"], row["other_tip"])
                        if int(row["work_item_id"]) == lot
                        else (row["other_tip"], row["lot_tip"]))
        if mine == lot_tip and theirs == other_tip:
            return row
    return None


def open_resolution(db, lot: int, other: int, *, repo: str | None = None) -> dict:
    """Opens (or shows, if already open) the resolution of `lot` with
    `other`. Returns `{state: open|prepared|clean, path, files, replayed,
    remaining}`."""
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    meta = _read_meta(path)
    if meta is not None:
        remaining = unmerged(path)
        return dict(meta, state="open", path=path, remaining=remaining,
                    markers=with_markers(path, meta["files"]), already=True)
    if os.path.exists(path):  # never removed without being asked: it may hold work
        raise ResolveError("%s exists without readable metadata (%s): look at it, then "
                           "ameesh work resolve %d --with %d --abort to drop it"
                           % (path, _meta_path(path), lot, other))
    ctx = pair(db, lot, other, repo)
    row = _current_resolution(db, lot, other, ctx["lot_tip"], ctx["other_tip"])
    if row is not None:
        # #2142 : la paire porte déjà une résolution à jour : ne pas rouvrir un
        # worktree pour la même chose. L'état est `prepared` (le radar la
        # rejoue), pas `open` avec un index non fusionné.
        return {"state": "prepared", "lot": lot, "other": other, "path": None,
                "commit": row["commit_sha"], "ref": row.get("ref"),
                "files": list(row.get("files") or []),
                "lot_tip": ctx["lot_tip"], "other_tip": ctx["other_tip"],
                "target": ctx["target"], "replayed": [], "remaining": [], "markers": []}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _check(_run(repo, "worktree", "add", "--quiet", "--detach", path, ctx["lot_tip"]),
           "worktree add")
    try:
        _run(repo, "worktree", "lock", "--reason",
             "ameesh work resolve %d --with %d" % (lot, other), path)
        # rerere off: neither the agents' rr-cache nor rerere.autoUpdate decide
        merge = _run(path, "-c", "rerere.enabled=false", "-c", "merge.conflictStyle=zdiff3",
                     *_attr_args(ctx.get("target_head")),
                     "merge", "--no-ff", "--no-commit", "--quiet", ctx["other_tip"])
        files = unmerged(path)
        if merge.returncode == 0 or not files:
            _remove_worktree(repo, path)
            if merge.returncode != 0:
                raise ResolveError("git merge %s into %s failed: %s"
                                   % (ctx["other"]["branch"], ctx["lot"]["branch"],
                                      _err(merge)))
            return {"state": "clean", "lot": lot, "other": other, "path": None, "files": [],
                    "lot_tip": ctx["lot_tip"], "other_tip": ctx["other_tip"]}
        primed = [r["commit_sha"] for r in _recorded(db, lot, other)
                  if _ensure_commit(repo, r)][:1]
        replayed = _replay_into(repo, path, primed[0], ctx["lot_tip"], ctx["other_tip"],
                                files, attributes_tree=ctx.get("target_head")) \
            if primed else []
        meta = {"lot": lot, "other": other, "lot_branch": ctx["lot"]["branch"],
                "other_branch": ctx["other"]["branch"], "target": ctx["target"],
                "target_head": ctx.get("target_head"),
                "lot_tip": ctx["lot_tip"], "other_tip": ctx["other_tip"], "files": files,
                "replayed": replayed, "primed": primed}
        with open(_meta_path(path), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
    except BaseException:
        if _read_meta(path) is None:
            _remove_worktree(repo, path)
        raise
    return dict(meta, state="open", path=path, remaining=unmerged(path),
                markers=with_markers(path, files), already=False)


def _replay_into(repo: str, path: str, commit: str, lot_tip: str, other_tip: str,
                 files, *, attributes_tree: str | None = None) -> list[str]:
    """Replays the merge in a scratch clone primed with `commit` only, and
    copies into the worktree `path` the files `rerere` resolved there (left
    unmerged in its index, for the agent to check). Returns them.
    `attributes_tree`: the target's, as `open_resolution` merges (#2189)."""
    scratch = tempfile.mkdtemp(prefix="ameesh-resolve-")
    try:
        replayer = Replayer(repo, os.path.join(scratch, "clone"), follow_forge=False)
        replayer.ensure()
        if replayer.prime([commit], attributes_tree=attributes_tree):
            return []
        resolved = []

        def export(clone: str, paths) -> None:
            for f in paths:
                if f in files and os.path.isfile(os.path.join(clone, f)):
                    shutil.copyfile(os.path.join(clone, f), os.path.join(path, f))
                    resolved.append(f)
        replayer.replay(lot_tip, other_tip, commit, attributes_tree=attributes_tree,
                        export=export)
        return sorted(resolved)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _merged_paths(path: str, meta: dict) -> set[str]:
    """Staged paths that differ from both the lot tip and the other tip: the
    content the merge or the agent made, where a marker may hide."""
    def changed(rev: str) -> set[str]:
        return set(_paths(_run(path, "diff", "--cached", "--name-only", "--no-renames",
                               "-z", rev)))
    return changed(meta["lot_tip"]) & changed(meta["other_tip"])


def _message(meta: dict, note: str) -> str:
    files = meta["files"]
    return ("Resolution: #%d with #%d\n\n"
            "Merge of %s (%s) into %s (%s), prepared ahead of the merge (L160d).\n"
            "Conflicts: %s.\n%s\n%s: %d %d\n"
            % (meta["lot"], meta["other"], meta["other_branch"], meta["other_tip"][:12],
               meta["lot_branch"], meta["lot_tip"][:12],
               ", ".join(files[:20]) + (" …" if len(files) > 20 else ""),
               "\n%s\n" % note.strip() if note and note.strip() else "",
               TRAILER, meta["lot"], meta["other"]))


def push(repo: str, sha: str, lot: int, other: int, *,
         previous: str | None = None) -> tuple[str | None, str | None]:
    """Pushes the resolution commit to `origin`, under its dedicated ref
    (forced: one ref per pair, written by L160d only), else as a branch,
    never forced over a branch L160d did not push: `--force-with-lease` on
    `previous`, the commit recorded there for the pair (none: the branch
    must not exist). Returns (ref pushed, error)."""
    if not plan_git._out(repo, "remote", "get-url", "origin"):
        return None, "no origin remote"
    error = None
    branch = "refs/heads/" + branch_name(lot, other)
    for ref, force in ((ref_name(lot, other), "--force"),
                       (branch, "--force-with-lease=%s:%s" % (branch, previous or ""))):
        proc = _run(repo, "push", "--quiet", force, "--no-verify", "origin",
                    "%s:%s" % (sha, ref), timeout=FETCH_TIMEOUT)
        if proc.returncode == 0:
            return ref, None
        error = _err(proc).splitlines()[-1:] or ["git push failed"]
        error = error[0]
    return None, error


def _prepared_out(row: dict, lot: int, other: int, *, already: bool) -> dict:
    """Sortie d'un `--done` dont le commit est déjà enregistré : seul le
    nettoyage restait (#2142)."""
    return {"lot": lot, "other": other, "commit": row["commit_sha"], "ref": row.get("ref"),
            "push_error": None, "local_ref": ref_name(lot, other),
            "files": list(row.get("files") or []), "state": "prepared", "already": already}


def _record_receipt(db, path: str, lot: int, actor: str) -> tuple[str | None, str | None]:
    """Le reçu exact du commit de résolution qui vient de naître (#2942).

    Mêmes bornes que le hook `post-commit` : fait de création constaté sur le
    reflog, identité du tour (session vivante, famille du canon), puis
    `record_commit`. Ne bloque jamais la résolution : sans tour managé ou si une
    garde refuse, l'origine reste inconnue — `(None, raison)` — et aucun reçu
    n'est fabriqué plus tard."""
    if db is None:
        return None, "no database"
    try:
        review_provenance.prepare_commit_creation(path, actor=actor, db=db)
        ident = review_provenance.proof_for_current_turn(db, actor=actor, cwd=path)
        if ident is None:
            return None, "outside a managed turn, or no live session"
        row = review_provenance.record_commit(db, "HEAD", identity=ident,
                                              work_item_id=lot, cwd=path)
    except review_provenance.ProvenanceError as exc:
        return None, str(exc)
    return row["subject"], None


def finish_resolution(db, lot: int, other: int, *, repo: str | None = None,
                      actor: str = "", note: str = "", push_fn=push) -> dict:
    """Commits the resolution open in its worktree, points the dedicated ref
    at it, pushes it, records it, and removes the worktree. Idempotent : si le
    commit est déjà enregistré (nettoyage manqué d'un `--done` précédent), elle
    ne repousse ni ne réenregistre, elle finit le nettoyage et rend l'état
    `prepared` (#2142)."""
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    meta = _read_meta(path)
    if meta is None:
        row = next(iter(_recorded(db, lot, other)), None)
        if row is not None and not os.path.isdir(path) and _ensure_commit(repo, row):
            # le worktree est déjà retiré et la résolution enregistrée : le
            # `.json` de méta (et l'entrée git) peuvent rester d'un nettoyage
            # interrompu ; on les enlève au lieu d'exiger une nouvelle passe.
            _remove_worktree(repo, path)
            return _prepared_out(row, lot, other, already=True)
        if os.path.exists(path):
            raise ResolveError("%s exists without readable metadata: look at it, then "
                               "ameesh work resolve %d --with %d --abort"
                               % (path, lot, other))
        raise ResolveError("no resolution of #%d with #%d is open here: "
                           "ameesh work resolve %d --with %d" % (lot, other, lot, other))
    merging = _run(path, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    receipt, receipt_error = None, "commit made by an earlier pass"
    if merging:
        left = with_markers(path, meta["files"])
        if left:
            raise ResolveError("conflict markers left in %s (%s)" % (", ".join(left), path))
        untracked = [e[3:] for e in _status(path) if e.startswith("??")]
        if untracked:
            raise ResolveError("untracked files in %s: %s; git add them if the resolution "
                               "needs them, else delete them, or drop the worktree with: "
                               "git worktree remove -f -f %s"
                               % (path, ", ".join(untracked[:10]), path))
        ignored = [e[3:] for e in _status(path) if e.startswith("!!")]
        if ignored:
            raise ResolveError("ignored files in %s: %s; removing the worktree would lose "
                               "them: git add -f them if the resolution needs them, else "
                               "move or delete them, or drop the worktree with: "
                               "git worktree remove -f -f %s"
                               % (path, ", ".join(ignored[:10]), path))
        _check(_run(path, "add", "-u"), "add")
        present = [f for f in meta["files"] if os.path.lexists(os.path.join(path, f))]
        if present:
            _check(_run(path, "add", "--", *present), "add")
        still = unmerged(path)
        if still:
            raise ResolveError("still unmerged: %s (%s)" % (", ".join(still), path))
        # a conflicted file renamed then `git add -A`-ed is not in meta["files"]:
        # check every staged path that is neither side's version
        left = with_markers(path, sorted(set(meta["files"]) | _merged_paths(path, meta)))
        if left:
            raise ResolveError("conflict markers left in %s (%s)" % (", ".join(left), path))
        # #2942 : `_run` coupe les crochets (`core.hooksPath=/dev/null`) ; la
        # paire attestation/fait de création de #1465 est donc posée ici, avec
        # les mêmes gardes de tour que les hooks, autour du commit mécanique.
        review_provenance.prepare_commit_intent(path, actor=actor, db=db)
        try:
            _check(_run(path, "-c", "rerere.enabled=false", "commit", "--quiet",
                        "--no-verify", "-F", "-", stdin=_message(meta, note).encode()),
                   "commit")
            receipt, receipt_error = _record_receipt(db, path, lot, actor)
        finally:
            review_provenance.clear_commit_intent(path)
            review_provenance.clear_commit_creation(path)
    else:
        dirty = _status(path)
        if dirty:  # a resumed --done: the commit is made, later changes are not in it
            raise ResolveError("changes in %s since the resolution commit: %s; they would "
                               "be lost: undo them, or --abort and start again"
                               % (path, ", ".join(e[3:] for e in dirty[:10])))
    sha = _check(_run(path, "rev-parse", "HEAD"), "rev-parse")
    parents = _text(_run(path, "rev-parse", sha + "^@")).split()
    if parents != [meta["lot_tip"], meta["other_tip"]]:
        raise ResolveError("HEAD of %s is not the merge of %s and %s: ameesh work resolve "
                           "%d --with %d --abort, then start again"
                           % (path, meta["lot_tip"][:12], meta["other_tip"][:12], lot, other))
    if _status(path):
        raise ResolveError("%s is not clean after the resolution commit: %s"
                           % (path, ", ".join(e[3:] for e in _status(path)[:10])))
    row = next(iter(_recorded(db, lot, other)), None)
    if row is not None and row["commit_sha"] == sha:
        # déjà poussé et enregistré par une passe précédente : ne pas repousser
        # ni réenregistrer, finir le nettoyage et rendre l'état `prepared`
        # (#2142, `--done` idempotent).
        _remove_worktree(repo, path, force=False)
        return _prepared_out(row, lot, other, already=True)
    _check(_run(repo, "update-ref", ref_name(lot, other), sha), "update-ref")
    branch = "refs/heads/" + branch_name(lot, other)
    previous = next((r["commit_sha"] for r in _recorded(db, lot, other)
                     if int(r["work_item_id"]) == lot and r.get("ref") == branch), None)
    pushed, error = push_fn(repo, sha, lot, other, previous=previous)
    row = storage.of(db).work.record_resolution(
        lot, other, commit=sha, lot_tip=meta["lot_tip"], other_tip=meta["other_tip"],
        ref=pushed, files=meta["files"],
        note="resolution prepared with #%d: %s (%s)" % (other, sha[:12],
                                                        pushed or "local only"),
        actor=actor)
    if row is None:
        raise ResolveError("lot #%d or #%d no longer exists: nothing recorded (commit %s "
                           "kept in %s)" % (lot, other, sha[:12], path))
    _remove_worktree(repo, path, force=False)
    return {"lot": lot, "other": other, "commit": sha, "ref": pushed, "push_error": error,
            "local_ref": ref_name(lot, other), "files": meta["files"],
            "state": "prepared", "already": False,
            "receipt": receipt, "receipt_error": receipt_error}


def abort_resolution(lot: int, other: int, *, repo: str | None = None) -> bool:
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    existed = os.path.exists(path) or os.path.exists(_meta_path(path))
    _remove_worktree(repo, path)
    return existed


def of_lot(db, lot: int) -> list[dict]:
    """Recorded resolutions where `lot` is either side."""
    return storage.of(db).work.resolutions([lot])


def of_lot_or_error(db, lot: int) -> tuple[list[dict], str | None]:
    """`of_lot`, and the error instead of raising (`work show` on a database
    without migration 0057)."""
    try:
        return of_lot(db, lot), None
    except Exception as exc:  # noqa: BLE001 - e.g. table work_resolutions missing
        return [], "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0] if str(exc)
                               else "")


def show_lines(rows: list[dict], lot: int) -> list[str]:
    import time
    out = []
    for r in rows:
        mine = int(r["work_item_id"]) == lot
        out.append("%s #%d: %s (%s), by %s on %s UTC%s" % (
            "resolution with" if mine else "resolved by",
            int(r["other_item_id"] if mine else r["work_item_id"]), r["commit_sha"][:12],
            r.get("ref") or "local only", r.get("created_by") or "?",
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.get("created_ts") or 0)),
            "; conflicts: %s" % ", ".join(r["files"][:6]) if r.get("files") else ""))
    return out


# --------------------------------------------------------------------------
# radar side: orphan git locks of the private clone (#5179)
# --------------------------------------------------------------------------

#: #5179 : âge minimal (s) d'un verrou git du clone radar pour le dire orphelin.
#: Bien au-delà de l'opération git la plus longue du clone (`FETCH_TIMEOUT`,
#: 180 s), réglable par `AMEESH_GIT_LOCK_STALE_S`.
DEFAULT_GIT_LOCK_STALE_S = 900.0


def _git_lock_stale_s(environ=None) -> float:
    """L'âge lu sans faire échouer l'import (#2371) ; mal formé ou non positif :
    le défaut, avec un avertissement."""
    raw = (os.environ if environ is None else environ).get("AMEESH_GIT_LOCK_STALE_S", "")
    if not raw.strip():
        return DEFAULT_GIT_LOCK_STALE_S
    try:
        value = float(raw)
        if 0 < value < float("inf"):
            return value
    except ValueError:
        pass
    warnings.warn("AMEESH_GIT_LOCK_STALE_S=%r is not a positive number: using %.0f"
                  % (raw, DEFAULT_GIT_LOCK_STALE_S), RuntimeWarning, stacklevel=2)
    return DEFAULT_GIT_LOCK_STALE_S


GIT_LOCK_STALE_S = _git_lock_stale_s()
#: suffixe de `radar.clone_dir` : seul un clone privé du radar est balayé, jamais
#: un dépôt d'agent ni un clone jetable
RADAR_CLONE_SUFFIX = ".clone"
#: les verrous de premier niveau du dépôt ; `refs/**/*.lock` s'y ajoute
GIT_TOP_LOCKS = ("index.lock", "HEAD.lock", "packed-refs.lock")


def git_locks(path: str) -> list[str]:
    """Les verrous git du clone `path`. `OSError` : arbre illisible."""
    git_dir = os.path.join(path, ".git")
    found = [os.path.join(git_dir, name) for name in GIT_TOP_LOCKS
             if os.path.lexists(os.path.join(git_dir, name))]

    def unreadable(exc: OSError) -> None:
        raise exc

    for root, _dirs, files in os.walk(os.path.join(git_dir, "refs"), onerror=unreadable):
        found += [os.path.join(root, name) for name in files if name.endswith(".lock")]
    return sorted(found)


def _journal_removed(path: str, record: dict, db=None) -> None:
    """Un verrou retiré : stderr, `<clone>.git-locks.log` à côté du clone, et
    l'événement `git.lock_removed` quand l'appelant a une base. Le journal
    n'interrompt jamais l'opération : le verrou est déjà retiré."""
    print("ameesh: #5179 orphan git lock removed: %s" % json.dumps(record, sort_keys=True),
          file=sys.stderr)
    try:
        with open(path + ".git-locks.log", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        print("ameesh: #5179 lock journal of %s not written (%s)" % (path, exc), file=sys.stderr)
    if db is None:
        return
    try:
        from . import events
        events.record(db, "git.lock_removed", "git-lock:%s" % record["path"][-280:], **record)
    except Exception as exc:  # noqa: BLE001 - journal, voir ci-dessus
        print("ameesh: #5179 lock event of %s not written (%s: %s)"
              % (path, type(exc).__name__, exc), file=sys.stderr)


def sweep_orphan_locks(path: str, *, db=None, stale_s: float | None = None,
                       now: float | None = None, proc_root: str | None = None) -> list[dict]:
    """#5179 : retire les verrous git orphelins du clone radar `path` ; rend ce
    qui a été retiré (chemin, âge, taille).

    L'appelant tient le verrou de clone du mesh (`Replayer.locked`, #2432) : aucun
    geste du mesh ne tient le clone. S'y ajoutent l'âge (`GIT_LOCK_STALE_S`) et
    l'absence de processus git (`git_process_in`). Tout état illisible laisse le
    verrou en place (fail closed) : l'atterrissage échoue alors sur ce fichier, et
    l'alerte `merge_lock_stuck` le nomme après quelques échecs."""
    stale_s = GIT_LOCK_STALE_S if stale_s is None else stale_s
    if not os.path.isdir(os.path.join(path, ".git")):
        return []  # pas encore cloné (`Replayer.ensure`)
    try:
        locks = git_locks(path)
    except OSError as exc:
        print("ameesh: #5179 locks of %s unreadable (%s): nothing removed" % (path, exc),
              file=sys.stderr)
        return []
    if not locks:
        return []
    now = time.time() if now is None else now
    removed: list[dict] = []
    checked = False
    for lock in locks:
        try:
            seen = os.lstat(lock)
            if not stat.S_ISREG(seen.st_mode) or now - seen.st_mtime < stale_s:
                continue
            if not checked:
                busy = git_process_in(path, proc_root)
                if busy is not False:
                    print("ameesh: #5179 git locks of %s kept: %s" % (
                        path, "git process at work" if busy else "processes unreadable"),
                        file=sys.stderr)
                    return removed
                checked = True
            os.unlink(lock)
        except FileNotFoundError:
            continue
        except OSError as exc:
            print("ameesh: #5179 lock %s kept (%s)" % (lock, exc), file=sys.stderr)
            continue
        record = {"path": lock, "age_s": round(now - seen.st_mtime), "size": seen.st_size}
        _journal_removed(path, record, db)
        removed.append(record)
    return removed


# --------------------------------------------------------------------------
# radar side: replay in the private clone
# --------------------------------------------------------------------------

class _CloneLock:
    """An exclusive flock on a clone's worktree, reentrant in one process.

    The radar clone is shared by every operation of the host: the executor's
    radar pass, a `work land` or `work queue add` started by a turn, the fire
    passage. `flock` carries the exclusion across those processes; the
    `RLock` serialises the threads of one process, which flock alone does not
    do, and makes a nested operation (a trial that primes resolutions, a
    landing that merges) cheap. The lock file sits beside the clone, never
    inside it: `Replayer.ensure()` may remove the clone."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.mutex = threading.RLock()
        self.depth = 0
        self.handle = None

    def acquire(self, wait: float) -> None:
        deadline = time.monotonic() + max(0.0, wait)
        if not self.mutex.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise CloneBusy("the radar clone's worktree is busy (%s)" % self.path)
        if self.depth:
            self.depth += 1
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            handle = open(self.path, "a+")
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        handle.close()
                        raise CloneBusy("the radar clone's worktree is busy (%s)"
                                        % self.path) from None
                    time.sleep(min(0.1, remaining))
            self.handle, self.depth = handle, 1
        except BaseException:
            self.mutex.release()
            raise

    def release(self) -> None:
        self.depth -= 1
        if not self.depth:
            handle, self.handle = self.handle, None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self.mutex.release()


#: one lock per clone path, shared by every Replayer of one process
_CLONE_LOCKS: dict = {}
_CLONE_LOCKS_MUTEX = threading.Lock()


def _clone_lock(path: str) -> _CloneLock:
    key = os.path.realpath(path)
    with _CLONE_LOCKS_MUTEX:
        lock = _CLONE_LOCKS.get(key)
        if lock is None:
            lock = _CLONE_LOCKS[key] = _CloneLock(key + ".worktree.lock")
        return lock


class Replayer:
    """The radar's private clone of a project. Objects come from the agents'
    repository (`--shared`); merges are replayed in the clone's worktree.
    Each resolution commit gets its own `rr-cache`, trained once and kept
    under `.git/ameesh-rr-v3/<commit>` (branches' attributes) or
    `.git/ameesh-rr-v4/<commit>` (the target's, `attributes_tree`): a replay
    sees the pair's resolution only, never another pair's nor a replaced one."""

    def __init__(self, repo: str, path: str, *, script: str | None = "auto",
                 target: str = "", follow_forge: bool = True, db=None) -> None:
        self.repo, self.path, self.script = repo, path, script
        #: #5179 : où journaliser un verrou git orphelin retiré (facultatif)
        self.db = db
        #: #3001 : le clone suit la forge (`track_forge`) ; un clone jetable
        #: qui ne rejoue que des résolutions (`rerere`) s'en passe
        self.follow_forge = follow_forge
        #: #2673: la cible déclarée du projet (`radar.projects.target`) que le
        #: clone doit porter ; vide : sa branche par défaut.
        self.target = (target or "").strip()
        #: attribute keys already read, per target tree (`attributes_key`)
        self._attr_keys: dict = {}
        #: #3001 : pourquoi la cible du clone n'est pas certifiée par la forge
        self.target_doubt = ""

    @contextmanager
    def locked(self, *, wait: float | None = None):
        """Holds the clone's worktree lock while the block reads or writes it.

        Every checkout, merge or reset in the clone's worktree — a stacked
        trial, a landing and its gates, a prepared resolution's replay —
        runs under this lock: a concurrent operation can no longer change the
        tree under a gate (#2428). The wait is bounded
        (`CLONE_LOCK_WAIT_S`); the caller then raises `CloneBusy` and retries
        at its next pass, instead of acting on a tree someone else is using.
        Reentrant in one thread.

        #5179 : à la prise (hors réentrance), les verrous git orphelins du clone
        radar sont retirés, avant toute écriture git du bloc."""
        lock = _clone_lock(self.path)
        lock.acquire(CLONE_LOCK_WAIT_S if wait is None else wait)
        try:
            if lock.depth == 1 and self.path.rstrip(os.sep).endswith(RADAR_CLONE_SUFFIX):
                sweep_orphan_locks(self.path, db=self.db)
            yield
        finally:
            lock.release()

    def sweep_locks(self) -> None:
        """#5179 : le balayage de `locked`, pour une opération hors du verrou de
        clone (`merge_queue.fetch_fresh`). Clone occupé : rien, sans attendre ; le
        passage suivant balaie."""
        try:
            with self.locked(wait=0):
                pass
        except CloneBusy:
            pass

    def ensure(self) -> None:
        with self.locked():
            if not os.path.isdir(os.path.join(self.path, ".git")):
                shutil.rmtree(self.path, ignore_errors=True)
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                _check(_run(os.path.dirname(self.path), "clone", "--quiet", "--shared",
                            "--no-checkout", self.repo, self.path, timeout=FETCH_TIMEOUT),
                       "clone")
                for key, value in (("user.name", "ameesh radar"),
                                   ("user.email", "radar@ameesh.invalid"),
                                   ("rerere.enabled", "true"), ("rerere.autoUpdate", "false"),
                                   ("gc.auto", "0"), ("core.hooksPath", os.devnull),
                                   ("advice.detachedHead", "false")):
                    _check(_run(self.path, "config", key, value), "config")
                head = plan_git._out(self.repo, "rev-parse", "HEAD")
                _check(_run(self.path, "checkout", "-q", "-f", "--detach", head), "checkout")
            else:
                _run(self.path, "reset", "-q", "--hard")
            self.track_forge()
            self.declare_target()
            self.refresh_target()

    def forge_url(self) -> str:
        """#3001 : la forge du projet, l'URL `origin` du checkout source ; vide
        quand le checkout n'en a pas (il est alors la seule copie).

        Un chemin local relatif (`../forge.git`) se lit depuis le checkout,
        comme Git le lit là-bas : relu depuis le clone, il viserait une autre
        forge, ou un homonyme voisin du clone. Les URL (`ssh://`, `https://`,
        `file://`) et la syntaxe scp (`git@hôte:chemin`) restent telles quelles ;
        la configuration du checkout n'est jamais touchée."""
        url = plan_git._out(self.repo, "remote", "get-url", "origin") or ""
        return _local_from(self.repo, url)

    def _tracks_forge(self, url: str) -> bool:
        return (plan_git._out(self.path, "remote", "get-url", "origin") == url
                and plan_git._out(self.path, "config", "--get-all",
                                  "remote.origin.fetch") == FORGE_REFSPEC)

    def _drop_refs(self, prefix: str, *, keep: str = "") -> None:
        listing = _text(_run(self.path, "for-each-ref", "--format=%(refname)", prefix))
        doomed = [r for r in listing.splitlines() if r and r != keep]
        if doomed:
            _check(_run(self.path, "update-ref", "--no-deref", "--stdin",
                        stdin="".join("delete %s\n" % r for r in doomed).encode()),
                   "update-ref")

    def track_forge(self) -> bool:
        """#3001 : le clone suit la forge, jamais les branches du checkout.

        `git clone --shared <checkout>` donne au clone le checkout pour
        `origin` : ses `refs/remotes/origin/*` sont les branches **locales** du
        checkout, figées ou divergentes (nexlink : `origin/develop` au 11/10).
        Le choix : l'`origin` du clone devient l'URL de la forge (celle du
        checkout), les objets restent lus du checkout par les alternates de
        `--shared`. Importer `refs/remotes/origin/*` du checkout dépendrait
        d'un checkout rafraîchi, ce que rien ne garantit.

        Un clone qui ne suit pas encore la forge (création, ou clone existant
        à l'ancienne URL ou refspec) est migré sans être recloné : ses refs
        copiées du checkout (`refs/remotes/origin/*`, branches locales hors
        celle de HEAD) sont effacées, toutes les branches de la forge sont
        récupérées, puis l'URL et la refspec sont réécrites. Une forge
        illisible laisse l'URL à migrer au passage suivant, sans aucune ref du
        checkout à lire entre-temps. Rend True si le clone suit la forge."""
        url = self.forge_url()
        if (not self.follow_forge or not url
                or os.path.realpath(self.path) == os.path.realpath(self.repo)):
            return False
        if self._tracks_forge(url):
            return True
        head = plan_git._out(self.path, "symbolic-ref", "--quiet", "HEAD") or ""
        self._drop_refs(_FORGE_REFS)
        self._drop_refs("refs/heads/", keep=head)
        try:
            proc = _run(self.path, "fetch", "--quiet", "--prune", "--no-tags", url,
                        FORGE_REFSPEC, timeout=FETCH_TIMEOUT)
        except GitError as exc:
            self.target_doubt = "the forge %s is unreadable: %s" % (url, exc)
            return False
        if proc.returncode:
            error = _err(proc)
            self.target_doubt = (fetch_failure(error)[:300] if local_corruption(error)
                                 else "the forge %s is unreadable: %s" % (url, error[-300:]))
            return False
        _check(_run(self.path, "remote", "set-url", "origin", url), "remote set-url")
        _check(_run(self.path, "config", "--replace-all", "remote.origin.fetch",
                    FORGE_REFSPEC), "config")
        # `origin/HEAD` suit la branche par défaut de la forge ; illisible, il
        # reste absent et la cible déclarée (`ameesh.target`) fait foi
        _run(self.path, "remote", "set-head", "origin", "--auto", timeout=FETCH_TIMEOUT)
        return True

    def _target_branches(self) -> list[str]:
        """Les cibles que lisent les lecteurs du clone, dans l'ordre où
        `plan_git.default_target` les consomme : la cible déclarée du clone
        (`ameesh.target`, jamais écrasée par `declare_target`), puis celle du
        projet (la base `develop` d'un lot nexlink, #3025), puis la branche par
        défaut de la forge (`origin/HEAD`)."""
        branches: list[str] = []
        for name in (plan_git.declared_target(self.path) or "", self.target,
                     plan_git._out(self.path, "symbolic-ref", "--quiet", "--short",
                                   _FORGE_REFS + "HEAD") or ""):
            branch = plan_git.short_branch(name)
            if (branch and branch != "HEAD" and not plan_git._SHA_RE.match(branch)
                    and branch not in branches):
                branches.append(branch)
        return branches

    def refresh_target(self) -> str:
        """#3001 : `refs/remotes/origin/<cible>` du clone est la pointe que la
        forge publie (`git ls-remote`), récupérée au besoin, pour **chaque**
        cible lue (`_target_branches`). Une cible que la forge ne certifie pas
        — forge illisible, ref restée plus ancienne — est effacée du clone et le
        doute noté (`target_doubt`) : un lecteur trouve une cible absente,
        jamais une pointe périmée ni la branche locale du checkout. Rend la
        pointe certifiée de la première cible ; vide sans forge, sans cible ou
        si elle n'est pas certifiée."""
        url = self.forge_url()
        branches = self._target_branches()
        if not url or not branches or not self._tracks_forge(url):
            return ""
        tips, doubts = {}, []
        for branch in branches:
            try:
                tips[branch] = self.forge_tip(branch)
            except StaleTarget as exc:
                doubts.append(str(exc))
                _run(self.path, "update-ref", "-d", _FORGE_REFS + branch)
        self.target_doubt = "; ".join(doubts)
        return tips.get(branches[0], "")

    def forge_tip(self, branch: str) -> str:
        """La pointe de `branch` à la forge, amenée sous
        `refs/remotes/origin/<branch>` du clone ; StaleTarget sinon."""
        url = self.forge_url()
        ref = _FORGE_REFS + branch
        try:
            listing = _run(self.path, "ls-remote", "--exit-code", url,
                           "refs/heads/" + branch, timeout=FETCH_TIMEOUT)
        except GitError as exc:
            raise StaleTarget("origin/%s: the forge is unreadable (%s): doubt, the "
                              "target is not read" % (branch, exc)) from exc
        published = _text(listing).split()[0] if not listing.returncode and _text(listing) \
            else ""
        if not plan_git._SHA_RE.match(published):
            # `--exit-code` : 2 = la forge répond sans cette branche
            raise StaleTarget("origin/%s: %s: doubt, the target is not read" % (
                branch, "the forge does not publish it" if listing.returncode == 2
                else "the forge is unreadable (%s)" % (_err(listing)[-200:] or "no answer")))
        error = ""
        if plan_git._commit(self.path, ref) != published:
            try:
                proc = _run(self.path, "fetch", "--quiet", "--no-tags", url,
                            "+refs/heads/%s:%s" % (branch, ref), timeout=FETCH_TIMEOUT)
                error = _err(proc) if proc.returncode else ""
            except GitError:
                pass
        held = plan_git._commit(self.path, ref)
        if held != published:
            raise StaleTarget("origin/%s is at %s, the forge publishes %s: an older "
                              "target is refused, not read%s"
                              % (branch, (held or "nothing")[:12], published[:12],
                                 " (%s)" % fetch_failure(error)[:300]
                                 if local_corruption(error) else ""))
        return published

    def _declared_ref(self) -> str:
        """La branche à déclarer dans le clone : la cible du projet quand elle
        nomme une branche récupérée, sinon la branche par défaut du clone."""
        if self.target:
            name = plan_git.short_branch(self.target)
            if name and plan_git._commit(self.path, "refs/remotes/origin/" + name):
                return "origin/" + name
            if plan_git._commit(self.path, self.target):
                return self.target
            # #2680 : une cible irrésolue ne s'écrit pas. Écrite, elle ne serait
            # jamais écrasée et `default_target` lèverait MergeProbeError au
            # lieu de retomber sur origin/HEAD/main.
            return ""
        for candidate in plan_git.DEFAULT_TARGETS:
            if plan_git._commit(self.path, candidate):
                return candidate
        return ""

    def declare_target(self) -> str:
        """#2673 : écrit `ameesh.target` dans le clone (création/rafraîchissement).

        Un clone radar hérite de `origin/HEAD` du checkout source : quand
        celui-ci est détaché sur une release, `plan_git.default_target` y lit
        `release/*` et `lot_class` classe « release » les lots sans cible. Si
        la clé manque, la cible du projet est écrite, sinon la branche par
        défaut du clone (`origin/main`, puis main/master). Une cible déjà posée
        n'est jamais écrasée (geste d'exploitation, #2673). Rend la cible
        écrite, vide si le clone n'existe pas, si la cible du projet ne s'y
        résout pas (#2680) ou s'il n'offre aucune branche."""
        if not os.path.isdir(os.path.join(self.path, ".git")):
            return ""
        if os.path.realpath(self.path) == os.path.realpath(self.repo):
            # le clone est le dépôt source : jamais de configuration chez lui
            return ""
        configured = plan_git.declared_target(self.path) or ""
        if configured:
            return configured
        value = self._declared_ref()
        if not value:
            return ""
        _check(_run(self.path, "config", plan_git.TARGET_GIT_KEY, value), "config")
        return value

    def _rr_cache(self) -> str:
        return os.path.join(self.path, ".git", "rr-cache")

    def _store(self, sha: str = "", attributes_tree: str | None = None) -> str:
        # Retrain legacy first-parent caches and v2 checkout -m preimages.
        root = TRAINING_CACHE_TARGET if attributes_tree else TRAINING_CACHE
        return os.path.join(self.path, ".git", root, sha)

    def attributes_key(self, attributes_tree: str) -> str:
        """The `.gitattributes` paths and entries of `attributes_tree`: two target trees
        with the same key drive the same merges, so a cache trained for one
        serves the other; a different key retrains it (#2189)."""
        if attributes_tree not in self._attr_keys:
            proc = _run(self.repo, "ls-tree", "-r", "-z", "--full-tree",
                        attributes_tree)
            blobs = []
            if proc.returncode == 0:
                for entry in proc.stdout.decode("utf-8", "replace").split("\0"):
                    _info, _, more = entry.partition("\t")
                    if more and more.rsplit("/", 1)[-1] == ".gitattributes":
                        # The directory determines a rule's scope; the mode
                        # also matters (Git does not follow attribute symlinks).
                        blobs.append(entry)
            # unreadable tree: fall back on its name, never reuse a wrong cache
            self._attr_keys[attributes_tree] = json.dumps(blobs) if proc.returncode == 0 \
                else attributes_tree
        return self._attr_keys[attributes_tree]

    def trained(self, attributes_tree: str | None = None) -> list[str]:
        try:
            root = self._store(attributes_tree=attributes_tree)
            key = self.attributes_key(attributes_tree) if attributes_tree else None
            names = []
            for name in os.listdir(root):
                store = self._store(name, attributes_tree)
                if name.startswith(".") or not os.path.isdir(store):
                    continue
                if key is not None:
                    try:
                        with open(os.path.join(store, ".attr-tree"), encoding="utf-8") as fh:
                            trained_key = fh.read().strip()
                    except OSError:
                        continue
                    if trained_key != key:
                        continue
                names.append(name)
            return sorted(names)
        except FileNotFoundError:
            return []

    def has(self, sha: str) -> bool:
        return _run(self.path, "cat-file", "-e", sha + "^{commit}").returncode == 0

    def fetch(self) -> None:
        """Resolution refs of this host (agents' repository) and of the
        others (its `origin`)."""
        specs = ("+%s*:%s*" % (REF_PREFIX, REF_PREFIX),
                 "+refs/heads/%s*:refs/ameesh/resolve/*" % BRANCH_PREFIX)
        _run(self.path, "fetch", "--quiet", self.repo, *specs, timeout=FETCH_TIMEOUT)
        url = self.forge_url()
        if url:
            _run(self.path, "fetch", "--quiet", url, *specs, timeout=FETCH_TIMEOUT)
        try:
            self.declare_target()
        except GitError as exc:
            # #2680 : deux fetch concurrents peuvent se heurter au verrou de
            # `git config` (`.git/config.lock`) à la première écriture. Le
            # fetch lui-même est fait ; la cible sera déclarée à la passe
            # suivante, jamais au prix d'une erreur pour cet échec transitoire.
            message = str(exc)
            if ("could not lock config file" not in message
                    or not message.endswith(": File exists")):
                raise

    def _empty_cache(self) -> None:
        shutil.rmtree(self._rr_cache(), ignore_errors=True)
        os.makedirs(self._rr_cache())

    def prime(self, commits, *, attributes_tree: str | None = None) -> list[str]:
        """Trains a cache of its own for each commit not learnt yet; returns
        the commits still missing, or whose training failed. With
        `attributes_tree`, the trial merges read the target's attributes
        (#2189) and the cache lives apart from the branches' one."""
        with self.locked():
            return self._prime(commits, attributes_tree=attributes_tree)

    def _prime(self, commits, *, attributes_tree: str | None = None) -> list[str]:
        commits = list(dict.fromkeys(commits))
        missing = [c for c in commits if not self.has(c)]
        if missing:
            self.fetch()
            missing = [c for c in commits if not self.has(c)]
        done = set(self.trained(attributes_tree))
        for commit in commits:
            if commit in done or commit in missing:
                continue
            self._empty_cache()
            try:
                train(self.path, [commit], script=self.script,
                      attributes_tree=attributes_tree)
            except (GitError, OSError, subprocess.SubprocessError):
                # this pair only: the others are still primed and replayed
                missing.append(commit)
                continue
            tmp = self._store("." + commit, attributes_tree)
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(self._rr_cache(), tmp)
            if attributes_tree:
                with open(os.path.join(tmp, ".attr-tree"), "w", encoding="utf-8") as fh:
                    fh.write(self.attributes_key(attributes_tree) + "\n")
            store = self._store(commit, attributes_tree)
            shutil.rmtree(store, ignore_errors=True)  # another target key, or a retrain
            os.replace(tmp, store)
        return missing

    def replay(self, a: str, b: str, commit: str, *, attributes_tree: str | None = None,
               export=None, generated=()) -> dict:
        """Merges `b` into `a` with `rerere` and the cache of `commit` only:
        `{state: prepared|stale|clean|error, remaining, files}`. `export(clone,
        resolved paths)` runs before the clone is reset. `generated` (L160g):
        paths that `land` regenerates — never left to rerere, never remaining.
        `attributes_tree`: read `.gitattributes` from that tree, as the radar's
        trial merge does (#2189), whichever tip is checked out."""
        with self.locked():
            return self._replay(a, b, commit, attributes_tree=attributes_tree,
                                export=export, generated=generated)

    def _replay(self, a: str, b: str, commit: str, *, attributes_tree: str | None = None,
                export=None, generated=()) -> dict:
        self._empty_cache()
        if commit in self.trained(attributes_tree):
            shutil.rmtree(self._rr_cache())
            shutil.copytree(self._store(commit, attributes_tree), self._rr_cache())
        try:
            _check(_run(self.path, "checkout", "-q", "-f", "--detach", a), "checkout")
            merge = _run(self.path, "-c", "rerere.enabled=false", "-c", "rerere.autoUpdate=false",
                         *_attr_args(attributes_tree),
                         "merge", "--no-ff", "--no-commit", "--quiet", b)
            if merge.returncode == 0:
                return {"state": "clean", "files": [], "remaining": []}
            files = unmerged(self.path)
            if not files:
                return {"state": "error", "files": [], "remaining": [],
                        "error": _err(merge)[:300]}
            exclude_generated(self.path, files, generated)
            _check(_run(self.path, "-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=false",
                        "rerere"), "rerere")
            remaining = [f for f in rerere_remaining(self.path) if f not in generated]
            if export is not None:
                export(self.path, sorted(set(files) - set(remaining) - set(generated)))
            return {"state": "stale" if remaining else "prepared", "files": files,
                    "remaining": remaining}
        finally:
            _run(self.path, "reset", "-q", "--hard")
            _run(self.path, "clean", "-q", "-f", "-d")
            self._empty_cache()  # rerere never learns from what is left here


def annotate(report: dict, rows: list[dict], lots: dict, *, replayer: Replayer | None,
             cache=None, generated=()) -> None:
    """Completes a radar report: each pair in conflict gets its resolver,
    and, if a resolution of the pair is recorded, its replay (`prepared` or
    `stale`). `rows`: recorded resolutions (newest first); `lots`: open lots
    by id (priority, state)."""
    branches = {b["work_item"]: b for b in report.get("branches") or ()}
    latest: dict = {}
    for row in rows:
        key = frozenset((int(row["work_item_id"]), int(row["other_item_id"])))
        latest.setdefault(key, row)
    sizes = getattr(cache, "sizes", None) if cache is not None else None
    replays = getattr(cache, "replays", None) if cache is not None else None
    sizes = {} if sizes is None else sizes
    replays = {} if replays is None else replays
    conflicted = [p for p in report.get("pairs") or ()
                  if p["state"] in ("conflict", "target-conflict", "unclassified", "superseded")]

    def target_tree(p: dict) -> str | None:
        """The target's tree, whose `.gitattributes` decide the pair's merge
        (#2189), like `radar.trial_merge`'s `attributes_tree`."""
        if p.get("target_head"):
            return p["target_head"]
        for n in p["lots"]:
            head = (branches.get(n) or {}).get("target_head")
            if head:
                return head
        return None

    wanted: dict = {}
    for p in conflicted:
        row = latest.get(frozenset(p["lots"]))
        if row is not None:
            wanted.setdefault(target_tree(p), []).append(row["commit_sha"])
    missing: set = set()
    if wanted and replayer is not None:
        try:
            replayer.ensure()
            for tree, commits in wanted.items():
                missing.update((c, tree) for c in
                               replayer.prime(commits, attributes_tree=tree))
        except (GitError, OSError) as exc:
            report["resolution_error"] = "%s: %s" % (type(exc).__name__, exc)
            replayer = None
    for p in conflicted:
        one, two = (dict(branches.get(n) or {"work_item": n}, **{
            k: (lots.get(n) or {}).get(k) for k in ("priority", "state")}) for n in p["lots"])
        if one.get("base") and two.get("base") and not p.get("attribution_pending"):
            p["resolver"] = choose_resolver(one, two, p["files"], repo=report.get("repo"),
                                            sizes=sizes)
        row = latest.get(frozenset(p["lots"]))
        if row is None:
            continue
        resolution = {"by": int(row["work_item_id"]), "commit": row["commit_sha"],
                      "ref": row.get("ref"), "created_by": row.get("created_by"),
                      "created_ts": row.get("created_ts")}
        p["resolution"] = resolution
        tree = target_tree(p)
        if replayer is None or (row["commit_sha"], tree) in missing:
            resolution["replay"] = "unavailable"
            continue
        # only the target's `.gitattributes` can change the replay: a target
        # move that leaves them alone reuses the cached result (#2189)
        attrs = replayer.attributes_key(tree) if tree else None
        key = (p["tips"][0], p["tips"][1], row["commit_sha"], attrs,
               tuple(sorted(generated)))
        # #2254: an `error` is not a result of these inputs — a checkout
        # disturbed by another pass, an unreadable object, a saturated disk —
        # it is retried at the next pass. Keeping it would freeze a transient
        # failure on this executor for as long as the tips stay put, and make
        # it alternate with the `prepared` another executor reports: the very
        # input-keyed cache that saves a replay must not keep a non-answer.
        result = replays.get(key)
        if result is None or result.get("state") == "error":
            try:
                result = replayer.replay(*p["tips"], row["commit_sha"],
                                         attributes_tree=tree, generated=generated)
            except (GitError, OSError) as exc:
                result = {"state": "error", "error": str(exc)[:300]}
            if result.get("state") != "error":
                replays[key] = result
        resolution["replay"] = result["state"]
        resolution["remaining"] = result.get("remaining") or []
        if (p["state"] == "conflict" and result["state"] in ("prepared", "stale")
                and (result["state"] == "prepared" or not p.get("attribution_pending"))):
            p["state"] = result["state"]


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "resolve", help="prepare the resolution of a conflict between two lots, ahead of "
                        "the merge (L160d): ameesh work resolve <lot> --with <other>")
    parser.add_argument("id", type=int, help="your lot (the resolver)")
    parser.add_argument("--with", dest="other", type=int, default=None,
                        help="the lot whose branch conflicts with yours")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--done", action="store_true",
                        help="commit the resolution, push it under refs/ameesh/resolutions/, "
                             "record it and remove the worktree")
    action.add_argument("--abort", action="store_true", help="drop the open resolution")
    parser.add_argument("-m", "--message", default="",
                        help="with --done: a note added to the resolution commit")
    parser.add_argument("--repo", default=None,
                        help="the project repository (default: the current git repository)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="",
                        help="default: the identity bound to the session (AGENT_MAIL_NAME)")
    parser.set_defaults(func=func)


def _print_open(out: dict) -> None:
    if out["state"] == "clean":
        print("#%d and #%d merge without conflict (%s + %s): nothing to prepare"
              % (out["lot"], out["other"], out["lot_tip"][:12], out["other_tip"][:12]))
        return
    if out["state"] == "prepared":
        print("resolution of #%d with #%d already prepared: %s (%s)" % (
            out["lot"], out["other"], out["commit"][:12], out.get("ref") or "local only"))
        print("nothing to open; the radar replays it at each pass and tells the "
              "resolver when a tip moves and it goes stale")
        return
    print("resolution of #%d with #%d %s: %s" % (
        out["lot"], out["other"], "already open" if out.get("already") else "open",
        out["path"]))
    print("merge of %s (%s) into %s (%s), target %s" % (
        out["other_branch"], out["other_tip"][:12], out["lot_branch"], out["lot_tip"][:12],
        out["target"]))
    print("conflicts: %s" % ", ".join(out["files"]))
    if out.get("replayed"):
        print("replayed by rerere from an earlier resolution: %s"
              % ", ".join(out["replayed"]))
    left = sorted(set(out.get("markers") or ()) | set(out.get("remaining") or ()))
    print("to resolve: %s" % (", ".join(left) if left else "nothing left"))
    print("then: ameesh work resolve %d --with %d --done   (or --abort)"
          % (out["lot"], out["other"]))


def run(cfg, db, args) -> int:
    try:
        if args.other is None:
            if args.done or args.abort:
                raise ResolveError("--done and --abort need --with <other lot>")
            rows = of_lot(db, args.id)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print("\n".join(show_lines(rows, args.id)) if rows
                      else "lot #%d: no prepared resolution" % args.id)
            return 0
        if args.abort:
            existed = abort_resolution(args.id, args.other, repo=args.repo)
            out = {"lot": args.id, "other": args.other, "aborted": existed}
            print(json.dumps(out) if args.json else
                  "resolution of #%d with #%d %s" % (args.id, args.other,
                                                     "dropped" if existed else "was not open"))
            return 0
        if args.done:
            out = finish_resolution(db, args.id, args.other, repo=args.repo,
                                    actor=args.actor, note=args.message)
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
                return 0
            if out.get("already"):
                print("resolution of #%d with #%d already prepared: %s, %s" % (
                    out["lot"], out["other"], out["commit"][:12],
                    "pushed to origin %s" % out["ref"] if out["ref"] else
                    "kept in %s only" % out["local_ref"]))
                print("the worktree was cleaned up; nothing re-committed, nothing re-recorded")
                return 0
            print("resolution of #%d with #%d recorded: %s, %s" % (
                out["lot"], out["other"], out["commit"][:12],
                "pushed to origin %s" % out["ref"] if out["ref"] else
                "kept in %s only (push failed: %s)" % (out["local_ref"], out["push_error"])))
            if out.get("receipt"):
                print("exact origin recorded: %s" % out["receipt"])
            else:
                print("origin unknown (no exact receipt): %s" % out.get("receipt_error"))
            print("the radar replays it at each pass: prepared while the conflict stays the "
                  "same, stale (and you are told) when it changes")
            return 0
        out = open_resolution(db, args.id, args.other, repo=args.repo)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            _print_open(out)
        return 0
    except (ResolveError, GitError, ValueError, plan_git.MergeProbeError) as exc:
        import sys
        print("error: %s" % exc, file=sys.stderr)
        return 1
