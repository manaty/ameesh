# SPDX-License-Identifier: AGPL-3.0-only
"""Processus (L63) : ascendance, heure de démarrage, détenteurs d'un fichier.

psutil d'abord ; sous Linux sans psutil, `/proc` ; ailleurs `NotAvailable`.
Les heures de démarrage sont en SECONDES EPOCH (float) sur tous les OS — plus
de tops d'horloge propres à Linux (ancienne colonne `pid_start`, 0036).
"""
from __future__ import annotations

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


def env_values(name: str) -> dict:
    """#339 : la valeur de la variable `name` dans l'environnement INITIAL de
    chaque processus de CET utilisateur (uid réel), zombies exclus :
    {"values": {valeur: [pid, …]}, "unread": [début, …]}. `unread` : les
    débuts (secondes epoch, None si inconnu) des processus de l'utilisateur
    dont l'environnement est illisible (non « dumpable ») — l'appelant les
    tient pour inconnus, jamais pour absents. Un processus disparu pendant
    la lecture est omis. `NotAvailable` si l'OS ne permet pas de lister les
    processus ni de lire leur environnement."""
    uid = os.getuid() if hasattr(os, "getuid") else None
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
            unread.append(start)
            continue
        for chunk in data.split(b"\0"):
            if chunk.startswith(key) and len(chunk) > len(key):
                values.setdefault(chunk[len(key):].decode("utf-8", "replace"), []).append(pid)
                break
    return {"values": values, "unread": unread}


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


def exited_child(pid: int) -> bool:
    """Observe an exited direct child without consuming its wait status."""
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "P_PID")):
        return False
    try:
        info = os.waitid(os.P_PID, int(pid), os.WEXITED | os.WNOHANG | os.WNOWAIT)
        return info is not None and info.si_pid == int(pid)
    except (ChildProcessError, OSError):
        return False


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

def _inside(target: str, real: str, is_dir: bool) -> bool:
    return target == real or (is_dir and target.startswith(real.rstrip(os.sep) + os.sep))


def holders(path: str, needle: str | None = None, exclude=(), *,
            mapped: bool = False, session: bool = False) -> list[dict]:
    """Les processus qui tiennent `path` : fichier ouvert (dans le dossier,
    si `path` en est un), dossier courant dans `path` (dossier seulement), ou
    `needle` sur la ligne de commande (hors argv[0] et hors les PID de
    `exclude`). Avec `session`, `needle` est un identifiant de session : seule
    une reprise en cours le nomme (l'argument d'un sélecteur de session,
    `codex resume <id>`, `claude --resume <id>`, `dsh --session-id <id>`,
    séparé ou lié par `=`) ; le même identifiant CITÉ dans un brief, un
    message ou un chemin n'est pas un détenteur (#1283). #248 : avec `mapped`,
    aussi l'exécutable et les fichiers projetés en mémoire (module natif
    `.node`, binaire electron). Chaque détenteur : `{"pid", "why", "cmd"}`.

    Les processus illisibles (autre utilisateur, disparus) sont ignorés, mais
    si ce processus-ci ne peut même pas lire SES fichiers ouverts, ou si l'OS
    n'offre aucun moyen de les lire, `NotAvailable` : une liste vide voudrait
    dire « personne ne tient la session », ce qui n'aurait pas été vérifié."""
    real = os.path.realpath(path)
    is_dir = os.path.isdir(real)
    exclude = set(int(p) for p in exclude)
    ps = _psutil()
    if ps is not None:
        return _holders_psutil(ps, real, is_dir, needle, exclude, mapped, session)
    if _proc_ok():
        return _holders_proc(real, is_dir, needle, exclude, mapped, session)
    raise NotAvailable("file holders",
                       "neither psutil nor /proc to list open files")


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
        return {"pid": pid, "why": hit, "cmd": " ".join(argv)[:160]}
    return None


def _holders_psutil(ps, real, is_dir, needle, exclude, mapped=False,
                    session=False) -> list[dict]:
    me = os.getpid()
    try:
        ps.Process(me).open_files()
    except Exception as exc:
        raise NotAvailable("file holders",
                           "open files unreadable even for this process (%s)"
                           % type(exc).__name__) from None
    found = []
    for proc in ps.process_iter():
        pid = proc.pid
        hit = ""
        try:
            with proc.oneshot():
                try:
                    for item in proc.open_files():
                        if _inside(os.path.realpath(item.path), real, is_dir):
                            hit = "open file"
                            break
                except (ps.AccessDenied, ps.ZombieProcess, OSError):
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
    return found


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


def _holders_proc(real, is_dir, needle, exclude, mapped=False, session=False) -> list[dict]:
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
    found = []
    for pid in pids:
        hit = ""
        fd_dir = os.path.join(PROC, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            fds = []
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
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


def user_paths(uid: int | None = None, allow=()) -> UserPaths:
    """Where the processes of `uid` (default: this user) are, from `/proc`.

    A process gone meanwhile is ignored. One that cannot be read (not
    dumpable: setuid, file capabilities) is a doubt, unless its name (`comm`)
    AND the base name of its `argv[0]` are both in `allow` or the service
    manager: its `exe` link is unreadable too, its command line is not.
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
            argv0 = _read_text(os.path.join(base, "cmdline")).split("\0", 1)[0]
            if name in allow and os.path.basename(argv0) in allow:
                if name not in SERVICE_MANAGER:
                    out.allowed.append(who)
            else:
                out.unreadable.append(who)
            continue
        for path in found:
            # `root` is `/` for every process: it only tells a chroot apart
            if path and path != "/":
                out.paths.setdefault(os.path.realpath(path), who)
    return out
