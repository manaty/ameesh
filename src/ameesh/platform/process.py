# SPDX-License-Identifier: AGPL-3.0-only
"""Processus (L63) : ascendance, heure de démarrage, détenteurs d'un fichier.

psutil d'abord ; sous Linux sans psutil, `/proc` ; ailleurs `NotAvailable`.
Les heures de démarrage sont en SECONDES EPOCH (float) sur tous les OS — plus
de tops d'horloge propres à Linux (ancienne colonne `pid_start`, 0036).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

from . import NotAvailable

#: racine de /proc (repli Linux) ; surchargée par les tests
PROC = "/proc"

#: écart toléré entre deux lectures de l'heure de démarrage d'un même
#: processus : sous Linux, `btime` (/proc/stat) peut varier d'une seconde
#: d'une lecture à l'autre ; un PID recyclé dans la même seconde reste
#: improbable
START_TOLERANCE_S = 1.0

#: profondeur maximale de l'ascendance remontée
MAX_DEPTH = 64


def _psutil():
    """psutil, ou None — relu à chaque appel (les tests le retirent)."""
    return getattr(sys.modules[__package__], "psutil", None)


def _proc_ok() -> bool:
    from . import is_linux
    return is_linux() and os.path.isdir(os.path.join(PROC, "self"))


def self_cgroup_paths() -> tuple[str, ...]:
    """Kernel cgroup memberships of this process (Linux only).

    Missing, unreadable or malformed membership raises NotAvailable, never
    an empty result that could be mistaken for a confirmed native identity.
    """
    from . import is_linux
    if not is_linux():
        raise NotAvailable("process cgroup membership", "requires Linux")
    try:
        with open(os.path.join(PROC, "self", "cgroup"), encoding="utf-8") as stream:
            rows = [line.rstrip().split(":", 2) for line in stream]
    except (OSError, UnicodeError) as exc:
        raise NotAvailable("process cgroup membership", str(exc)) from exc
    if not rows or any(len(row) != 3 or not row[2].startswith("/") for row in rows):
        raise NotAvailable("process cgroup membership", "empty or malformed membership")
    return tuple(row[2] for row in rows)


def rss_bytes(pid: int | None = None) -> int | None:
    """#2096 : RSS courant en octets (`VmRSS`), ou None si indisponible.

    psutil d'abord (portable), `/proc` ensuite sous Linux. Ne lève jamais :
    un appelant qui décide sur cette mesure traite None comme « inconnu »."""
    target = os.getpid() if pid is None else int(pid)
    ps = _psutil()
    if ps is not None:
        try:
            return int(ps.Process(target).memory_info().rss)
        except Exception:
            pass
    try:
        with open(os.path.join(PROC, "self" if pid is None else str(target), "status"),
                  encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    champs = line.split()
                    return int(champs[1]) * 1024 if len(champs) >= 2 else None
    except (OSError, ValueError):
        return None
    return None


#: #2107/#2108 : motifs de `trim_free_memory` — le journal doit distinguer
#: « rien à rendre » d'une plateforme qui n'offre pas `malloc_trim`.
TRIM_RETURNED = "returned"
TRIM_NOTHING = "nothing"
TRIM_UNSUPPORTED = "unsupported"
TRIM_FAILED = "failed"

#: libellés lisibles des motifs, pour le journal du runner
TRIM_LABELS = {
    TRIM_RETURNED: "returned to the kernel",
    TRIM_NOTHING: "nothing to return (malloc_trim freed no page)",
    TRIM_UNSUPPORTED: "not supported here (no malloc_trim)",
    TRIM_FAILED: "trim failed",
}


def trim_free_memory() -> str:
    """#2107/#2108 : motif du retour de mémoire libre à l'allocateur (glibc
    `malloc_trim`) — `returned` (des pages sont rendues), `nothing` (l'appel
    est offert mais aucune page n'était libre), `unsupported` (pas de
    `malloc_trim` : musl, macOS, Windows), `failed` (l'appel a échoué).

    Best-effort : ne lève jamais. Le libellé exact évite de diagnostiquer une
    plateforme non prise en charge quand le trim a simplement rendu 0 page."""
    import ctypes
    try:
        libc = ctypes.CDLL(None)
        trim = libc.malloc_trim
    except Exception:  # OSError, AttributeError ; TypeError sous Windows (CDLL(None))
        return TRIM_UNSUPPORTED
    try:
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return TRIM_RETURNED if trim(0) else TRIM_NOTHING
    except Exception:
        return TRIM_FAILED


def release_free_memory() -> bool:
    """Rend au noyau la mémoire libre de l'allocateur (glibc `malloc_trim`).

    #2096 : l'allocateur garde le plafond d'allocation d'un pic (un gros tour)
    alors que les objets sont morts ; sans ce retour, le RSS d'un exécuteur ne
    redescend pas. Best-effort : False si l'allocateur ne l'offre pas (musl,
    macOS), si l'appel échoue, ou s'il n'y avait rien à rendre — ne lève
    jamais. `trim_free_memory` dit lequel de ces cas (#2107/#2108)."""
    return trim_free_memory() == TRIM_RETURNED


def _stat_fields(pid: int) -> list[str]:
    """Champs de /proc/<pid>/stat à partir du 3e (état). Le nom (champ 2) est
    entre parenthèses et peut en contenir : on coupe à la DERNIÈRE « ) »."""
    with open(os.path.join(PROC, str(int(pid)), "stat"), encoding="utf-8",
              errors="replace") as fh:
        data = fh.read()
    return data[data.rindex(")") + 2:].split()


# --------------------------------------------------------------------------
# ascendance
# --------------------------------------------------------------------------

def ancestry(pid: int | None = None) -> list[int]:
    """Le processus `pid` (défaut : le processus courant) puis ses ascendants,
    du plus proche au plus lointain, sans init (PID 1) ni 0.

    Un ascendant illisible (disparu, autre utilisateur) arrête la remontée :
    la chaîne est plus courte, jamais inventée. `NotAvailable` si l'OS ne
    permet pas de remonter du tout."""
    ps = _psutil()
    if ps is None and not _proc_ok():
        raise NotAvailable("process ancestry",
                           "neither psutil nor /proc to read a process's parent")
    out: list[int] = []
    current = os.getpid() if pid is None else int(pid)
    for _ in range(MAX_DEPTH):
        if current <= 1 or current in out:
            break
        out.append(current)
        try:
            if ps is not None:
                parent = int(ps.Process(current).ppid())
            else:
                parent = int(_stat_fields(current)[1])
        except Exception:  # disparu, refusé, illisible : la chaîne s'arrête
            break
        current = parent
    return out


def parent_pid(pid: int) -> int | None:
    """#453 : le parent ACTUEL de `pid`, relu à l'instant (None si disparu ou
    illisible) — jamais celui d'une table lue plus tôt."""
    ps = _psutil()
    try:
        if ps is not None:
            return int(ps.Process(int(pid)).ppid())
        return int(_stat_fields(int(pid))[1])
    except Exception:
        return None


# --------------------------------------------------------------------------
# heures de démarrage
# --------------------------------------------------------------------------

def boot_time(stat_path: str | None = None) -> float:
    """Instant du démarrage de l'hôte (secondes epoch). `stat_path` : un
    fichier au format /proc/stat (tests). `NotAvailable` si inconnu."""
    ps = _psutil()
    if stat_path is None and ps is not None:
        try:
            return float(ps.boot_time())
        except Exception as exc:
            raise NotAvailable("host boot time", "psutil: %s" % exc) from None
    path = stat_path or os.path.join(PROC, "stat")
    if stat_path is None and not _proc_ok():
        raise NotAvailable("host boot time", "neither psutil nor /proc")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError) as exc:
        raise NotAvailable("host boot time", "%s unreadable: %s" % (path, exc)) from None
    raise NotAvailable("host boot time", "no btime line in %s" % path)


def ticks_to_epoch(ticks: int) -> float:
    """Ancienne heure de démarrage (tops d'horloge depuis le démarrage de
    l'hôte, Linux, migration 0036) convertie en secondes epoch. N'a de sens
    que sous Linux, sur l'hôte qui l'a mesurée : `NotAvailable` ailleurs."""
    from . import is_linux
    if not is_linux():
        raise NotAvailable("clock tick conversion",
                           "a time in clock ticks only makes sense on Linux")
    try:
        hertz = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        hertz = 100
    return boot_time() + float(ticks) / float(hertz or 100)


def start_time(pid: int | None) -> float | None:
    """Heure de démarrage du processus `pid` en secondes epoch ; None si le
    processus n'existe pas. Avec le PID, elle identifie un processus : un PID
    recyclé a une autre heure. `NotAvailable` si l'OS ne la donne pas."""
    if not pid:
        return None
    ps = _psutil()
    if ps is not None:
        try:
            return float(ps.Process(int(pid)).create_time())
        except ps.NoSuchProcess:
            return None
        except Exception as exc:
            raise NotAvailable("process start time",
                               "pid %s: %s" % (pid, type(exc).__name__)) from None
    if not _proc_ok():
        raise NotAvailable("process start time", "neither psutil nor /proc")
    try:
        ticks = int(_stat_fields(int(pid))[22 - 3])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as exc:
        if not os.path.exists(os.path.join(PROC, str(int(pid)))):
            return None
        raise NotAvailable("process start time",
                           "pid %s: %s" % (pid, exc)) from None
    return ticks_to_epoch(ticks)


def same_start(expected: float, actual: float | None) -> bool:
    """Deux heures de démarrage désignent-elles le même processus ?"""
    if actual is None:
        return False
    return abs(float(expected) - float(actual)) <= START_TOLERANCE_S


# --------------------------------------------------------------------------
# vie d'un processus, descripteurs
# --------------------------------------------------------------------------

def alive(pid: int) -> bool:
    """Le processus existe-t-il et tourne-t-il (un zombie ne compte pas) ?"""
    ps = _psutil()
    if ps is not None:
        try:
            return ps.Process(int(pid)).status() != ps.STATUS_ZOMBIE
        except ps.NoSuchProcess:
            return False
        except ps.AccessDenied:
            return True
    if _proc_ok():
        try:
            return _stat_fields(int(pid))[0] != "Z"
        except FileNotFoundError:
            return False
        except (OSError, ValueError, IndexError):
            return os.path.exists(os.path.join(PROC, str(int(pid))))
    from . import is_windows
    if is_windows():
        raise NotAvailable("process liveness", "psutil missing")
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def environ(pid: int | None = None) -> dict | None:
    """L124 : l'environnement INITIAL du processus `pid` (défaut : le
    processus courant), celui qu'il a reçu à son lancement — une variable
    retirée ensuite par le processus y reste.

    None si le processus n'existe plus ou n'est pas lisible (autre
    utilisateur, droits refusés) : l'appelant le tient pour inconnu.
    `NotAvailable` si l'OS ne permet pas de lire l'environnement d'un
    processus du tout (ni psutil, ni /proc)."""
    current = os.getpid() if pid is None else int(pid)
    ps = _psutil()
    if ps is not None:
        try:
            return dict(ps.Process(current).environ())
        except Exception:  # disparu, refusé, zombie : inconnu, jamais inventé
            return None
    if not _proc_ok():
        raise NotAvailable("process environment",
                           "neither psutil nor /proc to read a process's environment")
    try:
        with open(os.path.join(PROC, str(current), "environ"), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    out: dict = {}
    for chunk in data.split(b"\0"):
        key, sep, value = chunk.partition(b"=")
        if sep and key:
            out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def env_values(name: str, allow=()) -> dict:
    """#339 : la valeur de la variable `name` dans l'environnement INITIAL de
    chaque processus de CET utilisateur (uid réel), zombies exclus :
    {"values": {valeur: [pid, …]}, "unread": [début, …]}. `unread` : les
    débuts (secondes epoch, None si inconnu) des processus de l'utilisateur
    dont l'environnement est illisible (non « dumpable ») — l'appelant les
    tient pour inconnus, jamais pour absents. Un processus disparu pendant
    la lecture est omis. `NotAvailable` si l'OS ne permet pas de lister les
    processus ni de lire leur environnement.

    `allow` (L230, comme `holders`) : noms des processus dont l'illisibilité
    n'est PAS un doute (choix explicite de la politique). #5038 : les services
    non-dumpables sont reconnus séparément par leur MainPID systemd, uid,
    cgroup et tops de démarrage, jamais par un ajout automatique de noms.
    Sans identité vérifiée, leur environnement illisible reste un doute."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    allow = frozenset(allow or ())
    executors = _once(executor_processes)
    values: dict = {}
    unread: list = []
    ps = _psutil()
    if ps is not None:
        for proc in ps.process_iter(["pid", "uids", "status", "create_time"]):
            info = proc.info
            if info.get("status") == ps.STATUS_ZOMBIE:
                continue
            uids = info.get("uids")
            if uid is not None and (uids is None or uids.real != uid):
                continue
            try:
                value = proc.environ().get(name)
            except ps.NoSuchProcess:
                continue
            except Exception:
                if _proc_ok() and (_trusted_executor(int(info["pid"]), executors)
                                   or _allowed_proc(int(info["pid"]), allow)):
                    continue
                unread.append(info.get("create_time"))
                continue
            if value:
                values.setdefault(value, []).append(int(info["pid"]))
        return {"values": values, "unread": unread}
    if not _proc_ok():
        raise NotAvailable("process environment",
                           "neither psutil nor /proc to read a process's environment")
    key = name.encode("utf-8") + b"="
    starts = {p["pid"]: p["start"] for p in process_table() if not p["zombie"]}
    for pid, start in starts.items():
        try:
            with open(os.path.join(PROC, str(pid), "status"), encoding="utf-8",
                      errors="replace") as fh:
                real = next((int(line.split()[1]) for line in fh
                             if line.startswith("Uid:")), None)
        except (OSError, ValueError, IndexError):
            continue                # disparu
        if uid is not None and real != uid:
            continue
        try:
            with open(os.path.join(PROC, str(pid), "environ"), "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            continue
        except OSError:
            if _trusted_executor(pid, executors) or _allowed_proc(pid, allow):
                continue
            unread.append(start)
            continue
        for chunk in data.split(b"\0"):
            if chunk.startswith(key) and len(chunk) > len(key):
                values.setdefault(chunk[len(key):].decode("utf-8", "replace"), []).append(pid)
                break
    return {"values": values, "unread": unread}


#: Linux `prctl(2)` option: make this process non-dumpable.
PR_SET_DUMPABLE = 4


def become_undumpable() -> bool:
    """Linux: make this process's `/proc` entries unreadable to its peers.

    `prctl(PR_SET_DUMPABLE, 0)` makes the kernel refuse another process of the
    same user the read of this process's `/proc/<pid>/{environ,mem,fd}` (the
    `PTRACE_MODE_READ` check refuses a non-dumpable target, even under
    `yama/ptrace_scope=1`). This is what keeps a turn from reading the
    provider keys of the other executors (#5029).

    The flag lives in the `mm`: it follows the threads, and **execve resets
    it to 1** — called from a launcher that then `exec`s, it protects nothing.
    Only the long-lived process itself calls it, after its own exec.

    True when the flag was set; False on an OS without `/proc` (macOS,
    Windows), where the question does not arise. Raises `OSError` when the
    kernel refuses: the caller decides (the executor refuses to run rather
    than leave its environment readable).
    """
    if not _proc_ok():
        return False
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, "prctl(PR_SET_DUMPABLE, 0): %s" % os.strerror(code))
    return True


def venv_attestation_dir(environ=None) -> str:
    """#5100: where the executors of this host attest their executable.

    Same rule as `deploy/mise-a-jour/venv-processus.py`, its reader, which
    imports nothing from ameesh."""
    env = os.environ if environ is None else environ
    base = str(env.get("XDG_STATE_HOME") or "").strip()
    if not os.path.isabs(base):
        base = os.path.expanduser("~/.local/state")
    return os.path.join(base, "ameesh-host", "venv-vivant")


def publish_venv_attestation() -> str | None:
    """#5100: attest this process's executable for its non-root controller.

    A non-dumpable process hides `/proc/<pid>/exe` from its peers, while the
    update's `controler` must compare it with the interpreter of the current
    venv. The process publishes what only it can still read: its pid, its start
    in clock ticks (a recycled pid has another one) and the device and inode of
    its executable. No secret, mode 0600.

    Accepted limit: a process of the same user can forge this file. `controler`
    proves the health of a host, this is not a security boundary.

    Called after `become_undumpable`, and again after an in-place `exec`, which
    runs the startup path anew. Returns the path; None on an OS without `/proc`;
    raises `OSError` when it cannot publish: the caller decides.
    """
    if not _proc_ok():
        return None
    pid = os.getpid()
    exe = os.stat(os.path.join(PROC, "self", "exe"))
    try:
        ticks = _stat_fields(pid)[22 - 3]
    except IndexError as exc:
        raise OSError("%s/%d/stat: %s" % (PROC, pid, exc)) from None
    folder = venv_attestation_dir()
    os.makedirs(folder, mode=0o700, exist_ok=True)
    for name in os.listdir(folder):
        other = name[:-len(".json")] if name.endswith(".json") else ""
        if other.isdecimal() and not os.path.exists(os.path.join(PROC, other)):
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(folder, name))
    path = os.path.join(folder, "%d.json" % pid)
    temporary = "%s.%d.tmp" % (path, pid)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"pid": pid, "start_ticks": ticks,
                   "exe_dev": exe.st_dev, "exe_ino": exe.st_ino}, stream)
    os.replace(temporary, path)
    return path


def open_fd_count() -> int:
    """Nombre de descripteurs ouverts par ce processus (POSIX)."""
    ps = _psutil()
    if ps is not None and hasattr(ps.Process, "num_fds"):
        return int(ps.Process().num_fds())
    if _proc_ok():
        return len(os.listdir(os.path.join(PROC, "self", "fd")))
    raise NotAvailable("open descriptors", "neither psutil (POSIX) nor /proc")


#: descriptors of this process, always the real `/proc` (not `PROC`, which
#: the tests replace by a fake process table)
SELF_FD = "/proc/self/fd"


def fd_path(fd: int) -> str:
    """The path the kernel gives for the open directory or file `fd` of this
    process (L230: is a descriptor still where it was planned?). Linux:
    `/proc/self/fd/N`; macOS: `F_GETPATH`. `NotAvailable` elsewhere, or if
    the descriptor's file is deleted."""
    from . import is_linux
    import fcntl
    if is_linux() and os.path.isdir(SELF_FD):
        target = os.readlink(os.path.join(SELF_FD, str(fd)))
    elif hasattr(fcntl, "F_GETPATH"):
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        target = raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    else:
        raise NotAvailable("chemin d'un descripteur", "ni /proc ni F_GETPATH")
    if not target.startswith("/") or target.endswith(" (deleted)"):
        raise NotAvailable("path of a descriptor", "file deleted or outside the tree")
    return target


# --------------------------------------------------------------------------
# table des processus, groupes, sous-moissonneur (travail de fond d'un tour)
# --------------------------------------------------------------------------

def process_table(*, strict: bool = False) -> list[dict]:
    """Les processus lisibles de l'hôte, en une passe : `pid`, `ppid`, `pgid`
    (groupe), `name`, `start` (secondes epoch, None si inconnue), `zombie`.

    Sert au travail de fond d'un tour (membres du groupe du tour, zombies
    adoptés par l'exécuteur). Un processus disparu pendant la lecture est
    omis. `NotAvailable` si l'OS ne permet pas de lister les processus ou
    leurs groupes : une liste vide voudrait dire « plus rien ne tourne ».
    With strict=True, an unreadable member fails closed instead of being
    omitted: it may still be alive and fork unseen. A member that simply
    vanished is omitted even then — it is gone and cannot fork again, and a
    replacement it left behind shows up in the next snapshot."""
    if not hasattr(os, "getpgid"):
        raise NotAvailable("process table", "no process groups (POSIX)")
    ps = _psutil()
    if ps is not None:
        out = []
        attrs = ["pid", "ppid", "name", "create_time", "status"]
        def strict_infos():
            # process_iter silently skips NoSuchProcess. A vanished member is
            # gone (it cannot fork again): skip it, but any other read error
            # keeps the whole snapshot unusable.
            try:
                pids = ps.pids()
            except ps.Error as exc:
                raise NotAvailable("process table", "unreadable process list") from exc
            for pid in pids:
                try:
                    yield ps.Process(pid).as_dict(attrs=attrs)
                except ps.NoSuchProcess:
                    continue
                except ps.Error as exc:
                    raise NotAvailable("process table", "unreadable member") from exc
        infos = strict_infos() if strict else (proc.info for proc in ps.process_iter(attrs))
        for info in infos:
            try:
                pgid = os.getpgid(int(info["pid"]))
            except ProcessLookupError:  # vanished during the scan
                continue
            except OSError:
                if strict:
                    raise
                continue
            if strict and info.get("status") is None:
                raise NotAvailable("process table", "unreadable process status")
            out.append({"pid": int(info["pid"]), "ppid": int(info.get("ppid") or 0),
                        "pgid": pgid, "name": str(info.get("name") or ""),
                        "start": info.get("create_time"),
                        "zombie": info.get("status") == ps.STATUS_ZOMBIE})
        return out
    if not _proc_ok():
        raise NotAvailable("process table", "neither psutil nor /proc")
    try:
        demarrage = boot_time()
    except NotAvailable:
        demarrage = None
    try:
        hertz = float(os.sysconf("SC_CLK_TCK") or 100)
    except (AttributeError, ValueError, OSError):
        hertz = 100.0
    out = []
    for entry in os.listdir(PROC):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(PROC, entry, "stat"), encoding="utf-8",
                      errors="replace") as fh:
                data = fh.read()
            fields = data[data.rindex(")") + 2:].split()
            out.append({"pid": int(entry), "ppid": int(fields[1]), "pgid": int(fields[2]),
                        "name": data[data.index("(") + 1:data.rindex(")")],
                        "start": (demarrage + int(fields[19]) / hertz
                                  if demarrage is not None else None),
                        "zombie": fields[0] == "Z"})
        except (FileNotFoundError, ProcessLookupError):
            # vanished during the scan: gone, so it cannot fork again
            continue
        except (OSError, ValueError, IndexError) as exc:
            if strict:
                raise NotAvailable("process table", "unreadable process member") from exc
            continue
    return out


def process_group_exists(pgid: int) -> bool:
    """Signal-zero existence probe; zombies count, uncertainty stays open."""
    if not hasattr(os, "killpg"):
        raise NotAvailable("process group", "no process groups (POSIX)")
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def command_line(pid: int) -> str:
    """La ligne de commande de `pid` (vide si illisible, disparu, zombie)."""
    ps = _psutil()
    if ps is not None:
        try:
            return " ".join(str(a) for a in ps.Process(int(pid)).cmdline())
        except Exception:
            return ""
    try:
        with open(os.path.join(PROC, str(int(pid)), "cmdline"), "rb") as fh:
            return " ".join(a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a)
    except OSError:
        return ""


def command_argv(pid: int) -> list[str]:
    """#453 : les arguments de `pid`, un par élément (vide si illisible,
    disparu, zombie) : une commande se reconnaît à son exécutable et à ses
    arguments, jamais à sa ligne aplatie."""
    ps = _psutil()
    if ps is not None:
        try:
            return [str(a) for a in ps.Process(int(pid)).cmdline()]
        except Exception:
            return []
    try:
        with open(os.path.join(PROC, str(int(pid)), "cmdline"), "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def become_subreaper() -> bool:
    """Ce processus adopte ses descendants orphelins (Linux,
    `PR_SET_CHILD_SUBREAPER`) au lieu de les laisser à systemd ou à init :
    il peut alors lire leur code de sortie. Faux si l'OS ne le permet pas."""
    from . import is_linux
    if not is_linux():
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return int(libc.prctl(36, 1, 0, 0, 0)) == 0  # 36 = PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError, ValueError):
        return False


def children(pid: int | None = None) -> list[dict]:
    """Les enfants DIRECTS de `pid` (défaut : ce processus), zombies compris :
    `pid`, `zombie`. Sous Linux, la liste du noyau
    (`/proc/<pid>/task/<tid>/children`, lue d'un coup, sans balayer tous les
    processus) ; sinon psutil ; sinon `NotAvailable` (L131c : un sous-
    moissonneur sait ainsi exactement s'il lui reste des descendants)."""
    pid = os.getpid() if pid is None else int(pid)
    if _proc_ok():
        tasks = os.path.join(PROC, str(pid), "task")
        try:
            tids = os.listdir(tasks)
        except OSError as exc:
            raise NotAvailable("enfants d'un processus", str(exc))
        pids: list[int] = []
        found = False
        for tid in tids:
            try:
                with open(os.path.join(tasks, tid, "children"), encoding="ascii") as fh:
                    pids += [int(x) for x in fh.read().split()]
                found = True
            except FileNotFoundError:
                continue            # fil terminé, ou noyau sans CONFIG_PROC_CHILDREN
            except (OSError, ValueError):
                continue
        if found:
            out = []
            for child in sorted(set(pids)):
                try:
                    zombie = _stat_fields(child)[0] in ("Z", "X")
                except (OSError, ValueError, IndexError):
                    continue        # moissonné entre-temps
                out.append({"pid": child, "zombie": zombie})
            return out
    ps = _psutil()
    if ps is not None:
        try:
            return [{"pid": c.pid, "zombie": c.status() == ps.STATUS_ZOMBIE}
                    for c in ps.Process(pid).children()]
        except (ps.NoSuchProcess, ps.AccessDenied) as exc:
            raise NotAvailable("enfants d'un processus", str(exc))
    raise NotAvailable("enfants d'un processus", "ni liste du noyau ni psutil")


def exited_child_status(pid: int) -> int | None:
    """Exit status of an exited direct child, WITHOUT consuming it.

    None while it runs, when it is not our child, or when the OS cannot
    observe it. A negative status means killed by that signal. The child
    stays a zombie, so its pid — and the process-group id that an exited
    session leader keeps — cannot be reused until the caller reaps it
    (a `killpg` on that pgid is then aimed at our group, not a recycled one).
    """
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "P_PID")):
        return None
    try:
        info = os.waitid(os.P_PID, int(pid), os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except (ChildProcessError, OSError):
        return None
    if info is None or info.si_pid != int(pid):
        return None
    if getattr(os, "CLD_EXITED", None) is not None and info.si_code == os.CLD_EXITED:
        return int(info.si_status)
    return -int(info.si_status)


def exited_child(pid: int) -> bool:
    """Observe an exited direct child without consuming its wait status."""
    return exited_child_status(pid) is not None


def reap(pid: int) -> int | None:
    """Moissonne `pid` s'il est un enfant TERMINÉ de ce processus : rend son
    code de sortie (négatif : tué par ce signal). None s'il tourne encore,
    s'il n'est pas notre enfant, ou si l'OS ne le permet pas. À n'appeler que
    sur un pid qu'aucun `subprocess.Popen` n'attend (il perdrait le code)."""
    if not hasattr(os, "WNOHANG"):
        return None
    try:
        fini, statut = os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError):
        return None
    if fini == 0:
        return None
    return os.waitstatus_to_exitcode(statut)


# --------------------------------------------------------------------------
# détenteurs d'un fichier
# --------------------------------------------------------------------------

def _inside(target: str, real: str | None, is_dir: bool) -> bool:
    if real is None:  # L63b: command line only, no file to look for
        return False
    return target == real or (is_dir and target.startswith(real.rstrip(os.sep) + os.sep))


def holders(path: str | None, needle: str | None = None, exclude=(), *,
            mapped: bool = False, session: bool = False, allow=()) -> list[dict]:
    """Les processus qui tiennent `path` : fichier ouvert (dans le dossier,
    si `path` en est un), dossier courant dans `path` (dossier seulement), ou
    `needle` sur la ligne de commande (hors argv[0] et hors les PID de
    `exclude`). Avec `session`, `needle` est un identifiant de session : seule
    une reprise en cours le nomme (l'argument d'un sélecteur de session,
    `codex resume <id>`, `claude --resume <id>`, `dsh --session-id <id>`,
    séparé ou lié par `=`) ; le même identifiant CITÉ dans un brief, un
    message ou un chemin n'est pas un détenteur (#1283). #248 : avec `mapped`,
    aussi l'exécutable et les fichiers projetés en mémoire (module natif
    `.node`, binaire electron). Chaque détenteur : `{"pid", "why", "cmd",
    "name"}` — `cmd` est l'argv, à ne jamais écrire en base (#3403) ; `name`
    sa base d'argv[0].

    Les processus illisibles (autre utilisateur, disparus) sont ignorés, mais
    si ce processus-ci ne peut même pas lire SES fichiers ouverts, si l'OS
    n'offre aucun moyen de les lire, ou (L63b) si un processus du même
    utilisateur refuse la lecture de ses fichiers ouverts (non dumpable) sans
    être le gestionnaire de services, et qu'AUCUN détenteur n'a été trouvé,
    `NotAvailable` : une liste vide voudrait dire « personne ne tient la
    session », ce qui n'aurait pas été vérifié. Un détenteur vérifié, lui,
    reste une réponse vraie quel que soit le refus d'un tiers (#1749).
    `path` None : `needle` seul, l'OS toujours exigé (L63b).

    `allow` (L230) : noms des processus de l'utilisateur dont l'illisibilité
    n'est PAS un doute — le choix explicite de l'owner de l'hôte, vide par
    défaut ; le `comm` et l'une des deux premières entrées d'`argv` doivent
    s'y trouver (même règle que `user_paths`), sinon le refus reste un doute.
    Un détenteur CONSTATÉ (fichier ouvert, dossier courant, `needle`) reste
    rapporté, même si son nom est dans `allow` : l'allow ne lève que le doute,
    jamais une observation."""
    real = os.path.realpath(path) if path else None
    is_dir = bool(real) and os.path.isdir(real)
    exclude = set(int(p) for p in exclude)
    allow = frozenset(allow or ())
    ps = _psutil()
    if ps is not None:
        return _holders_psutil(ps, real, is_dir, needle, exclude, mapped,
                               session=session, allow=allow)
    if _proc_ok():
        return _holders_proc(real, is_dir, needle, exclude, mapped,
                             session=session, allow=allow)
    raise NotAvailable("file holders",
                       "neither psutil nor /proc to list open files")


#: L63b: a doubt (open files denied) is read again this many times, this far
#: apart: a process that is only exiting (its files already closed, /proc
#: denies its `fd` until it is a zombie) is gone or a zombie by then; a live
#: holder that is not dumpable stays a doubt
DOUBT_RETRIES = 5
DOUBT_RETRY_S = 0.1

#: L63b, off Linux: system executables no user can write (SIP), e.g. macOS's
#: ContextStoreAgent, which runs as the user and denies its open files. Not
#: /usr/bin: a harness can run under Apple's python.
SYSTEM_EXE_ROOTS = ("/System/", "/usr/libexec/")


def _proc_status(pid) -> dict | None:
    """The fields of /proc/<pid>/status (`{"Uid": [...], ...}`), readable
    even when the process is not dumpable; None when it is gone. Any other
    read error is raised: unknown is not "another user" (review #377 r2)."""
    try:
        with open(os.path.join(PROC, str(pid), "status"), encoding="utf-8",
                  errors="replace") as fh:
            text = fh.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key] = value.split()
    return out


def service_managers(uid: int | None = None) -> frozenset:
    """L63b: the PIDs of the user's service manager and of its `(sd-pam)`,
    whose open files are never readable. Told by PID and credentials, never
    by name, command line or start time, which a process can borrow (review
    #377 r2-r4): the MainPID of user@<uid>.service, asked of PID 1
    (`systemctl`), and its direct children whose supplementary groups are
    EMPTY while the manager's are not. `(sd-pam)` is forked as root and
    takes the uid without initgroups; a user process cannot empty its groups
    (setgroups needs CAP_SETGID; denied in an unprivileged user namespace).
    A manager without groups exempts no child. Empty without systemd or
    /proc."""
    import subprocess
    uid = os.getuid() if uid is None else uid
    if not _proc_ok():
        return frozenset()
    try:
        done = subprocess.run(["systemctl", "show", "--property=MainPID", "--value",
                               "user@%d.service" % uid],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    main = done.stdout.strip()
    if done.returncode != 0 or not main.isdigit() or int(main) <= 1:
        return frozenset()
    main = int(main)
    out = {main}
    try:
        status = _proc_status(main)
    except OSError:
        status = None
    if not status or not status.get("Groups"):
        return frozenset(out)
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        try:
            fields = _stat_fields(int(pid))
            child = _proc_status(pid) if len(fields) > 1 and fields[1] == str(main) else None
        except (OSError, ValueError):
            continue
        if child is not None and "Groups" in child and not child["Groups"]:
            out.add(int(pid))
    return frozenset(out)


class ExecutorSnapshot(dict):
    """Per-scan identities and unavailable managers; never caches PID trust."""

    def __init__(self):
        super().__init__()
        self.unavailable = []


def executor_processes() -> dict:
    """#5038: service MainPIDs with their kernel start ticks and cgroup.

    Names/argv alone grant nothing. systemd supplies the MainPID and the exact
    unit control group; a turn process or a child in that same service is never
    its MainPID. A missing manager, malformed response or reused PID exempts
    nothing. The snapshot is local to one scan, never a cross-scan PID cache.
    """
    if not _proc_ok():
        return {}
    import subprocess
    blocks = []
    result = ExecutorSnapshot()
    for manager in (["systemctl", "--user"], ["systemctl", "--system"]):
        try:
            done = subprocess.run(
                [*manager, "show", "--no-pager",
                 "--property=Id,MainPID,ControlGroup,Transient,FragmentPath", "ameesh-*.service"],
                capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            result.unavailable.append(manager[-1])
            continue
        if done.returncode == 0:
            blocks.extend(done.stdout.strip().split("\n\n"))
        else:
            result.unavailable.append(manager[-1])
    for block in blocks:
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit, pid, group = (fields.get(k, "") for k in ("Id", "MainPID", "ControlGroup"))
        fragment = fields.get("FragmentPath", "")
        if (fields.get("Transient") != "no" or not os.path.isabs(fragment)
                or "/transient/" in fragment):
            continue
        if not (unit.startswith("ameesh-") and unit.endswith(".service")
                and pid.isdecimal() and int(pid) > 1 and group.startswith("/")
                and group.rstrip("/").split("/")[-1] == unit):
            continue
        identity = _executor_identity(int(pid), group)
        if identity is not None:
            result[int(pid)] = identity
    return result


def _executor_identity(pid, group):
    try:
        status = _proc_status(pid)
        stat = _stat_fields(int(pid))
        cgroups = _read_text(os.path.join(PROC, str(pid), "cgroup")).splitlines()
        actual = next((line[3:] for line in cgroups if line.startswith("0::")), "")
        if (not status or not status.get("Uid") or len(stat) <= 19
                or status["Uid"][1] != str(os.getuid())
                or actual not in (group, group + "/runner")):
            return None
        return (stat[19], group)
    except (OSError, ValueError, IndexError):
        return None


def _trusted_executor(pid, identities) -> bool:
    identity = identities().get(int(pid))
    return identity is not None and _executor_identity(int(pid), identity[1]) == identity


def _named_by(allow, name: str, argv) -> bool:
    """L230 : le processus se nomme (`comm`) et s'annonce dans `allow` —
    `argv[0]`, ou `argv[1]` quand un interpréteur lance un script (le cas de
    l'exécuteur : comm `ameesh`, argv[0] le python du venv, argv[1] le script
    `ameesh`). Un nom emprunté ne blanchit pas un refus : les deux doivent
    s'y trouver."""
    if not allow or not name:
        return False
    return name in allow and any(
        os.path.basename(arg) in allow for arg in list(argv)[:2] if arg)


def _allowed_proc(pid, allow) -> bool:
    """L230 : un processus dont le `comm` et l'un de ses deux premiers argv
    sont dans `allow` n'est pas un doute (`_named_by`). Un `comm` vide ou un
    `cmdline` illisible ne blanchit rien : le doute reste."""
    if not allow:
        return False
    name = _read_text(os.path.join(PROC, str(pid), "comm")).strip()
    try:
        with open(os.path.join(PROC, str(pid), "cmdline"), "rb") as fh:
            argv = [arg.decode("utf-8", "replace")
                    for arg in fh.read().split(b"\0")[:2] if arg]
    except OSError:
        return False
    return _named_by(allow, name, argv)


def _doubt_proc(pid, uid: int, managers, allow=(), executors=None) -> str | None:
    """L63b: why a process whose open files were denied leaves the answer
    unknown, read on /proc; None when it cannot hold anything of the user's:
    gone, a zombie (no files left), running as another user (effective uid:
    a setuid helper such as sudo is root's), the user's service manager
    (`managers()`, by PID), a verified service MainPID (`executors()`), or a
    process explicitly named by `allow` (L230, `comm` and one of its first two argv entries; an
    unreadable status is a doubt)."""
    try:
        status = _proc_status(pid)
    except OSError as exc:
        return "%s (status unreadable: %s)" % (pid, exc.strerror or type(exc).__name__)
    if status is None or (status.get("State") or ["?"])[0] in ("Z", "X"):
        return None
    uids = status.get("Uid") or []
    if len(uids) < 2 or not uids[1].isdigit():
        return "%s (uid unreadable)" % pid
    if int(uids[1]) != uid or int(pid) in managers():
        return None
    if ((executors is not None and _trusted_executor(pid, executors))
            or _allowed_proc(pid, allow)):
        return None
    detail = "%s %s" % (pid, " ".join(status.get("Name") or ["?"]))
    if executors is not None and "--user" in getattr(executors(), "unavailable", ()):
        detail += (" (systemctl --user unavailable; verify user session context: "
                   "XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS)")
    return detail


def _doubt_psutil(ps, pid: int, user, allow=()) -> str | None:
    """`_doubt_proc` off Linux, by psutil: gone or zombie, another user
    (effective uid, or user name on Windows), a system executable
    (`SYSTEM_EXE_ROOTS`), or a process named by `allow` (L230, name and one
    of its first two argv entries) is not a doubt; an unreadable answer is."""
    try:
        proc = ps.Process(pid)
        if proc.status() == ps.STATUS_ZOMBIE or _owner(proc) != user:
            return None
        name = proc.name()
        try:
            exe = proc.exe() or ""
        except ps.AccessDenied:
            exe = ""
    except ps.NoSuchProcess:   # ZombieProcess too
        return None
    except Exception as exc:
        return "%d (%s)" % (pid, type(exc).__name__)
    if exe.startswith(SYSTEM_EXE_ROOTS):
        return None
    if allow:
        try:
            argv = [str(a) for a in proc.cmdline()[:2]]
        except (ps.AccessDenied, ps.ZombieProcess, OSError, IndexError):
            argv = []
        if _named_by(allow, name, argv):
            return None
    return "%d %s" % (pid, name)


def _owner(proc):
    """Who a psutil process runs as: its effective uid (a setuid helper is
    root's), its user name where the OS has no uids (Windows)."""
    try:
        uids = proc.uids()
    except AttributeError:   # Windows
        return proc.username()
    return uids.effective


class HoldersUnreadable(NotAvailable):
    """#3396 : des processus du même utilisateur refusent la lecture de leurs
    fichiers ouverts, et aucun détenteur n'a été constaté. `holders` nomme
    « pid nom » ceux qui restent après les relectures : le doute se dit, il ne
    se déguise pas en « un processus travaille dedans »."""

    def __init__(self, holders) -> None:
        self.holders = [str(holder) for holder in holders]
        super().__init__("file holders",
                         "open files of process(es) of this user unreadable: %s"
                         % ", ".join(self.holders[:5]))


def _settle(doubts: dict, again) -> None:
    """L63b: `doubts` (pid -> who) read again by `again(pid)` (who, or None)
    until none is left or `DOUBT_RETRIES` is spent; what is left is
    `HoldersUnreadable` (#3396, a `NotAvailable` that names them): an empty
    list would say "nobody holds it" unchecked."""
    import time
    for _ in range(DOUBT_RETRIES):
        if not doubts:
            return
        time.sleep(DOUBT_RETRY_S)
        doubts = {pid: who for pid in doubts for who in [again(pid)] if who}
    if doubts:
        raise HoldersUnreadable(list(doubts.values()))


#: Sélecteurs de session d'une ligne de commande : une reprise en cours nomme
#: la session juste après eux (`claude --resume <id>`, `dsh --session-id <id>`,
#: ou leur valeur liée par `=`), ou après les options de la sous-commande
#: (`codex exec resume --json <id>`, `codex resume --no-alt-screen <id>`). Un
#: identifiant cité ailleurs dans un argv — brief JSON, message, prompt, valeur
#: d'option — n'est pas un détenteur (#1283).
SESSION_SELECTORS = ("resume", "--resume", "-r", "--session-id")

#: Options de reprise SANS valeur, par binaire (`basename(argv[0])`) : le
#: jeton suivant reste candidat au positional de session. Toute autre option
#: est supposée prendre une valeur — la consommer évite de prendre son
#: argument pour l'id (`codex resume -m gpt-5.1-codex <id>`) ; une valeur liée
#: par `=` tient en un seul jeton. La grammaire dépend du binaire pour un même
#: jeton : `-p` = `--print` chez Claude mais `--profile <nom>` chez Codex, `-c`
#: = `--continue` chez Claude mais `--config <clé=valeur>` chez Codex — les
#: confondre perd une vraie reprise ou prend un profil pour un id (#1283 R1 cx2,
#: aide locale Codex : `-p`/`--profile` porte une valeur). Sources : descripteurs
#: `harness_descriptors/*.json`, `dsh --help` local, revue cx2.
_CODEX_FLAGS = frozenset({
    "--json", "--no-alt-screen", "--all", "--ephemeral", "--worktree",
    "--no-daemon", "--ignore-user-config", "--ignore-rules", "--strict-config",
    "--include-non-interactive", "--search", "--oss", "--approve-for-me",
    "--full-auto",
    "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust", "-h", "--help", "-V", "--version",
})
_CLAUDE_FLAGS = frozenset({
    "-p", "--print", "-c", "--continue", "--verbose", "--fork-session",
    "--include-partial-messages", "--strict-mcp-config", "--ide",
    "--replay-user-messages", "--dangerously-skip-permissions",
    "-h", "--help", "-V", "--version",
})
_DSH_FLAGS = frozenset({
    "--json", "--no-daemon", "--dump-config", "--dump-config-schema",
    "--dump-default-config", "-h", "--help", "-V", "--version",
})
#: Repli d'un binaire inconnu (harnais porté par un script de banc) : les
#: grammaires dont le `resume` est un sous-commande à positional. Les options
#: courtes ambiguës (`-p`, `-c`) en sont absentes : supposées à valeur, comme
#: chez Codex, puisque c'est la grammaire du `resume` nu.
RESUME_FLAGS = {
    "codex": _CODEX_FLAGS,
    "claude": _CLAUDE_FLAGS,
    "dsh": _DSH_FLAGS,
    "": _CODEX_FLAGS | _DSH_FLAGS,
}

#: Options qui désignent « la session la plus récente » : aucun positional
#: d'id ne suit, un jeton cité après elles est un prompt. `--all` n'en fait
#: PAS partie : chez Codex il élargit la recherche (Show all sessions), il ne
#: désigne pas la dernière — un id le suivant est bien le positional
#: (#1283 R1 cx2, aide locale).
RESUME_LATEST = frozenset({"--last"})


def _resume_flags(argv: list[str]) -> frozenset[str]:
    """La grammaire d'options du binaire lancé (repli : binaires connus)."""
    nom = os.path.basename(argv[0]) if argv else ""
    return RESUME_FLAGS.get(nom, RESUME_FLAGS[""])


def _resume_positional(argv: list[str], index: int,
                       flags: frozenset[str]) -> str | None:
    """Le positional de session d'une reprise à partir de `index` : options
    consommées (leur valeur avec), `--` respecté ; None si `--last` désigne la
    plus récente, ou s'il n'y a plus de positional."""
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if arg in RESUME_LATEST:
            return None
        if arg.startswith("-"):
            index += 1
            if not arg.partition("=")[1] and arg not in flags \
                    and index < len(argv) and not argv[index].startswith("-"):
                index += 1
            continue
        return arg
    return None


def _session_argument(argv: list[str], needle: str) -> bool:
    """`needle` est-il nommé par une reprise en cours, et non cité ?

    Tous les sélecteurs de la ligne sont essayés : un mot `resume` du texte
    (prompt, message) ne masque pas un `--resume <id>` plus loin."""
    flags = _resume_flags(argv)
    for index, arg in enumerate(argv[1:], start=1):
        flag, sep, value = arg.partition("=")
        if sep and flag in SESSION_SELECTORS:
            if value == needle:
                return True
        elif arg == "resume":
            if _resume_positional(argv, index + 1, flags) == needle:
                return True
        elif arg in ("--resume", "-r", "--session-id"):
            if index + 1 < len(argv) and argv[index + 1] == needle:
                return True
    return False


def _verdict(pid: int, hit: str, argv: list[str], needle, exclude,
             session: bool = False) -> dict | None:
    if not hit and needle and pid not in exclude:
        if session:
            if _session_argument(argv, needle):
                hit = "session id on the command line"
        elif any(needle in a for a in argv[1:]):
            hit = "session id on the command line"
    if hit:
        # #3403 : `name` (base de argv[0]) est ce qui peut s'écrire au journal
        # partagé ; `cmd` porte l'argv (identifiant de session, secret passé
        # en argument possibles) et reste à l'usage local de l'appelant.
        return {"pid": pid, "why": hit, "cmd": " ".join(argv)[:160],
                "name": os.path.basename(argv[0]) if argv else ""}
    return None


def _holders_psutil(ps, real, is_dir, needle, exclude, mapped=False,
                    session=False, allow=()) -> list[dict]:
    me = os.getpid()
    try:
        ps.Process(me).open_files()
        user = _owner(ps.Process(me))
    except Exception as exc:
        raise NotAvailable("file holders",
                           "open files unreadable even for this process (%s)"
                           % type(exc).__name__) from None
    if _proc_ok():   # Linux: the same reading of a doubt as the /proc way
        uid, managers = os.getuid(), _once(service_managers)
        executors = _once(executor_processes)

        def doubt(pid):
            return _doubt_proc(pid, uid, managers, allow, executors)
    else:
        def doubt(pid):
            return _doubt_psutil(ps, pid, user, allow)
    found = []
    doubts: dict = {}
    for proc in ps.process_iter():
        pid = proc.pid
        hit = ""
        try:
            with proc.oneshot():
                refused = False
                try:
                    for item in proc.open_files():
                        if _inside(os.path.realpath(item.path), real, is_dir):
                            hit = "open file"
                            break
                except ps.AccessDenied:
                    refused = True
                except (ps.ZombieProcess, OSError):
                    pass
                if not hit and is_dir:
                    try:
                        if _inside(os.path.realpath(proc.cwd()), real, True):
                            hit = "working directory"
                    except (ps.AccessDenied, ps.ZombieProcess, OSError):
                        pass
                if not hit and mapped:
                    try:
                        paths = [proc.exe()] + [m.path for m in proc.memory_maps()]
                    except (ps.AccessDenied, ps.ZombieProcess, OSError, AttributeError):
                        paths = []
                    if any(p and _inside(os.path.realpath(p), real, is_dir) for p in paths):
                        hit = "mapped file"
                try:
                    argv = [str(a) for a in proc.cmdline()]
                except (ps.AccessDenied, ps.ZombieProcess, OSError):
                    argv = []
        except ps.NoSuchProcess:
            continue
        row = _verdict(pid, hit, argv, needle, exclude, session)
        if row:
            found.append(row)
        elif refused:
            who = doubt(pid)
            if who:
                doubts[pid] = who
    # L63b : la relecture du doute ne sert que si RIEN n'a été trouvé. Un
    # détenteur vérifié (fichier ouvert, dossier courant) tient l'entrée quel
    # que soit le refus d'un processus tiers du même utilisateur ; jeter cette
    # réponse positive rendait le ménage aveugle (« holders unreadable » pour
    # tout /tmp) sur un poste où gpg-agent n'est pas dumpable (#1749).
    if not found:
        _settle(doubts, doubt)
    return found


def _once(fn):
    """`fn()` computed on the first call only (the service manager is
    asked of systemd only when a doubt needs it)."""
    memo: list = []

    def get():
        if not memo:
            memo.append(fn())
        return memo[0]
    return get


def _mapped_proc(pid: str, real: str, is_dir: bool) -> str:
    """#248 : l'exécutable ou un fichier projeté (`maps`) du processus sous
    `real` ; "" sinon ou si illisible."""
    try:
        if _inside(os.readlink(os.path.join(PROC, pid, "exe")), real, is_dir):
            return "mapped file"
    except OSError:
        pass
    try:
        with open(os.path.join(PROC, pid, "maps"), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                parts = line.split(None, 5)
                if len(parts) == 6 and parts[5].startswith("/") and \
                        _inside(parts[5].rstrip("\n"), real, is_dir):
                    return "mapped file"
    except OSError:
        pass
    return ""


def _holders_proc(real, is_dir, needle, exclude, mapped=False, session=False,
                  allow=()) -> list[dict]:
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError as exc:
        raise NotAvailable("file holders", "%s unreadable: %s" % (PROC, exc)) from None
    try:
        os.listdir(os.path.join(PROC, str(os.getpid()), "fd"))
    except OSError as exc:
        raise NotAvailable("file holders",
                           "open files unreadable even for this process (%s)"
                           % exc) from None
    uid, managers = os.getuid(), _once(service_managers)
    executors = _once(executor_processes)
    found = []
    doubts: dict = {}
    for pid in pids:
        hit = ""
        fd_dir = os.path.join(PROC, pid, "fd")
        # L63b: a refusal, on the listing or on a link (the process can stop
        # being dumpable in between), is a doubt, never an empty answer
        refused = False
        try:
            fds = os.listdir(fd_dir)
        except (FileNotFoundError, ProcessLookupError):
            continue   # gone
        except OSError:
            fds, refused = [], True
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except FileNotFoundError:
                continue   # closed meanwhile
            except OSError:
                refused = True
                break
            if _inside(target, real, is_dir):
                hit = "open file"
                break
        if not hit and is_dir:
            try:
                if _inside(os.readlink(os.path.join(PROC, pid, "cwd")), real, True):
                    hit = "working directory"
            except OSError:
                pass
        if not hit and mapped:
            hit = _mapped_proc(pid, real, is_dir)
        try:
            with open(os.path.join(PROC, pid, "cmdline"), "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            argv = []
        row = _verdict(int(pid), hit, argv, needle, exclude, session)
        if row:
            found.append(row)
        elif refused:
            who = _doubt_proc(pid, uid, managers, allow, executors)
            if who:
                doubts[pid] = who
    # L63b : même règle que la voie psutil — un détenteur vérifié ne se jette
    # pas parce qu'un tiers du même utilisateur refuse ses fichiers ouverts.
    if not found:
        _settle(doubts, lambda pid: _doubt_proc(pid, uid, managers, allow, executors))
    return found


# --------------------------------------------------------------------------
# where the user's processes are (L230)
# --------------------------------------------------------------------------

#: never readable (not dumpable) and never inside a worktree: the user's
#: service manager (`systemd --user`, cwd `/`)
SERVICE_MANAGER = frozenset({"systemd", "(sd-pam)"})



class UserPaths:
    """Snapshot of the user's processes: real paths of their cwd, executable,
    open and mapped files (`paths`: path -> "pid name"), the processes that
    could not be read (`unreadable`), those of them let through by name
    (`allowed`), and whether the table could be read at all (`available`)."""

    def __init__(self, available: bool = True):
        self.paths: dict = {}
        self.unreadable: list = []
        self.allowed: list = []
        self.available = available

    def doubt(self) -> str:
        """Why nothing can be judged free of processes, or ''."""
        if not self.available:
            return "process table unreadable on this host"
        if self.unreadable:
            return "process(es) of the user unreadable: %s" % ", ".join(self.unreadable[:5])
        return ""

    def holders(self, root: str) -> list:
        root = os.path.realpath(root).rstrip(os.sep)
        prefix = root + os.sep
        return sorted({who for path, who in self.paths.items()
                       if path == root or path.startswith(prefix)})


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _link(path: str) -> str:
    target = os.readlink(path)
    if target.endswith(" (deleted)"):
        target = target[:-len(" (deleted)")]
    return target if target.startswith("/") else ""


def _zombie(pid: int) -> bool:
    """Le processus `pid` est-il un zombie (état `Z` de /proc/<pid>/stat) ?

    Un zombie n'est plus qu'une entrée de table : il ne détient aucun fichier,
    et ses liens `/proc` sont illisibles sans qu'il y ait un doute à avoir
    (#1678, constat #1665 : un « timeout <defunct> » retenait tout le ménage
    des worktrees). Un stat illisible (disparu, autre utilisateur) : False —
    le doute ordinaire s'applique.

    Le meneur d'un groupe dont un autre fil vit encore se lit aussi `Z` : il
    n'est zombie que si tous ses fils (`/proc/<pid>/task`) le sont."""
    try:
        if _stat_fields(pid)[0] != "Z":
            return False
        tasks = os.listdir(os.path.join(PROC, str(int(pid)), "task"))
        for tid in tasks:
            path = os.path.join(PROC, str(int(pid)), "task", tid, "stat")
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    data = fh.read()
            except FileNotFoundError:
                continue   # fil disparu entre-temps
            if data[data.rindex(")") + 2:].split()[0] != "Z":
                return False
        return bool(tasks)
    except (OSError, IndexError, ValueError):
        return False


def user_paths(uid: int | None = None, allow=()) -> UserPaths:
    """Where the processes of `uid` (default: this user) are, from `/proc`.

    A process gone meanwhile is ignored, and so is a zombie (`_zombie`, #1678):
    it holds nothing anymore. One that cannot be read (not dumpable: setuid,
    file capabilities) is a doubt, unless its name (`comm`) and one of its two
    first argv entries (the script an interpreter runs, for an executor) are in
    `allow` or the service manager: its `exe` link
    is unreadable too, its command line is not. What was read before a
    refusal (a `cwd`, an `exe`) is recorded all the same (#3195 R2): an
    observation is never dropped for what could not be read after it.
    Without `/proc` (other OS), `available` is false."""
    if not _proc_ok():
        return UserPaths(available=False)
    uid = os.getuid() if uid is None else uid
    allow = SERVICE_MANAGER | set(allow or ())
    out = UserPaths()
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return UserPaths(available=False)
    for pid in pids:
        base = os.path.join(PROC, pid)
        try:
            if os.lstat(base).st_uid != uid:
                continue
        except OSError:
            continue
        if _zombie(int(pid)):
            # plus un détenteur : ignoré comme un processus disparu (#1678)
            continue
        name = _read_text(os.path.join(base, "comm")).strip() or "?"
        who = "%s %s" % (pid, name)
        found = []
        try:
            found.append(_link(os.path.join(base, "cwd")))
            for link in ("exe", "root"):
                try:
                    found.append(_link(os.path.join(base, link)))
                except FileNotFoundError:
                    pass
            for fd in os.listdir(os.path.join(base, "fd")):
                try:
                    found.append(_link(os.path.join(base, "fd", fd)))
                except FileNotFoundError:
                    continue
            with open(os.path.join(base, "maps"), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split(None, 5)
                    if len(parts) == 6 and parts[5].startswith("/"):
                        path = parts[5]
                        found.append(path[:-len(" (deleted)")] if path.endswith(" (deleted)")
                                     else path)
        except (FileNotFoundError, ProcessLookupError):
            continue   # gone meanwhile
        except OSError:
            argv = _read_text(os.path.join(base, "cmdline")).split("\0")[:2]
            if _named_by(allow, name, argv):
                if name not in SERVICE_MANAGER:
                    out.allowed.append(who)
            else:
                out.unreadable.append(who)
        for path in found:
            # `root` is `/` for every process: it only tells a chroot apart
            if path and path != "/":
                out.paths.setdefault(os.path.realpath(path), who)
    return out
