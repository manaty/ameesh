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
        raise NotAvailable("ascendance des processus",
                           "ni psutil ni /proc pour lire le parent d'un processus")
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
            raise NotAvailable("démarrage de l'hôte", "psutil : %s" % exc) from None
    path = stat_path or os.path.join(PROC, "stat")
    if stat_path is None and not _proc_ok():
        raise NotAvailable("démarrage de l'hôte", "ni psutil ni /proc")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError) as exc:
        raise NotAvailable("démarrage de l'hôte", "%s illisible : %s" % (path, exc)) from None
    raise NotAvailable("démarrage de l'hôte", "pas de ligne btime dans %s" % path)


def ticks_to_epoch(ticks: int) -> float:
    """Ancienne heure de démarrage (tops d'horloge depuis le démarrage de
    l'hôte, Linux, migration 0036) convertie en secondes epoch. N'a de sens
    que sous Linux, sur l'hôte qui l'a mesurée : `NotAvailable` ailleurs."""
    from . import is_linux
    if not is_linux():
        raise NotAvailable("conversion des tops d'horloge",
                           "une heure en tops d'horloge n'a de sens que sous Linux")
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
            raise NotAvailable("heure de démarrage d'un processus",
                               "pid %s : %s" % (pid, type(exc).__name__)) from None
    if not _proc_ok():
        raise NotAvailable("heure de démarrage d'un processus", "ni psutil ni /proc")
    try:
        ticks = int(_stat_fields(int(pid))[22 - 3])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as exc:
        if not os.path.exists(os.path.join(PROC, str(int(pid)))):
            return None
        raise NotAvailable("heure de démarrage d'un processus",
                           "pid %s : %s" % (pid, exc)) from None
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
        raise NotAvailable("vie d'un processus", "psutil absent")
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def open_fd_count() -> int:
    """Nombre de descripteurs ouverts par ce processus (POSIX)."""
    ps = _psutil()
    if ps is not None and hasattr(ps.Process, "num_fds"):
        return int(ps.Process().num_fds())
    if _proc_ok():
        return len(os.listdir(os.path.join(PROC, "self", "fd")))
    raise NotAvailable("descripteurs ouverts", "ni psutil (POSIX) ni /proc")


# --------------------------------------------------------------------------
# table des processus, groupes, sous-moissonneur (travail de fond d'un tour)
# --------------------------------------------------------------------------

def process_table() -> list[dict]:
    """Les processus lisibles de l'hôte, en une passe : `pid`, `ppid`, `pgid`
    (groupe), `name`, `start` (secondes epoch, None si inconnue), `zombie`.

    Sert au travail de fond d'un tour (membres du groupe du tour, zombies
    adoptés par l'exécuteur). Un processus disparu pendant la lecture est
    omis. `NotAvailable` si l'OS ne permet pas de lister les processus ou
    leurs groupes : une liste vide voudrait dire « plus rien ne tourne »."""
    if not hasattr(os, "getpgid"):
        raise NotAvailable("table des processus", "pas de groupes de processus (POSIX)")
    ps = _psutil()
    if ps is not None:
        out = []
        for proc in ps.process_iter(["pid", "ppid", "name", "create_time", "status"]):
            info = proc.info
            try:
                pgid = os.getpgid(int(info["pid"]))
            except OSError:  # disparu entre la liste et la lecture
                continue
            out.append({"pid": int(info["pid"]), "ppid": int(info.get("ppid") or 0),
                        "pgid": pgid, "name": str(info.get("name") or ""),
                        "start": info.get("create_time"),
                        "zombie": info.get("status") == ps.STATUS_ZOMBIE})
        return out
    if not _proc_ok():
        raise NotAvailable("table des processus", "ni psutil ni /proc")
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
        except (OSError, ValueError, IndexError):
            continue
    return out


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


def holders(path: str, needle: str | None = None, exclude=()) -> list[dict]:
    """Les processus qui tiennent `path` : fichier ouvert (dans le dossier,
    si `path` en est un), dossier courant dans `path` (dossier seulement), ou
    `needle` sur la ligne de commande (hors argv[0] et hors les PID de
    `exclude`). Chaque détenteur : `{"pid", "why", "cmd"}`.

    Les processus illisibles (autre utilisateur, disparus) sont ignorés, mais
    si ce processus-ci ne peut même pas lire SES fichiers ouverts, ou si l'OS
    n'offre aucun moyen de les lire, `NotAvailable` : une liste vide voudrait
    dire « personne ne tient la session », ce qui n'aurait pas été vérifié."""
    real = os.path.realpath(path)
    is_dir = os.path.isdir(real)
    exclude = set(int(p) for p in exclude)
    ps = _psutil()
    if ps is not None:
        return _holders_psutil(ps, real, is_dir, needle, exclude)
    if _proc_ok():
        return _holders_proc(real, is_dir, needle, exclude)
    raise NotAvailable("détenteurs d'un fichier",
                       "ni psutil ni /proc pour lister les fichiers ouverts")


def _verdict(pid: int, hit: str, argv: list[str], needle, exclude) -> dict | None:
    if not hit and needle and pid not in exclude and any(needle in a for a in argv[1:]):
        hit = "id de session sur la ligne de commande"
    if hit:
        return {"pid": pid, "why": hit, "cmd": " ".join(argv)[:160]}
    return None


def _holders_psutil(ps, real, is_dir, needle, exclude) -> list[dict]:
    me = os.getpid()
    try:
        ps.Process(me).open_files()
    except Exception as exc:
        raise NotAvailable("détenteurs d'un fichier",
                           "fichiers ouverts illisibles même pour ce processus (%s)"
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
                            hit = "fichier ouvert"
                            break
                except (ps.AccessDenied, ps.ZombieProcess, OSError):
                    pass
                if not hit and is_dir:
                    try:
                        if _inside(os.path.realpath(proc.cwd()), real, True):
                            hit = "dossier courant"
                    except (ps.AccessDenied, ps.ZombieProcess, OSError):
                        pass
                try:
                    argv = [str(a) for a in proc.cmdline()]
                except (ps.AccessDenied, ps.ZombieProcess, OSError):
                    argv = []
        except ps.NoSuchProcess:
            continue
        row = _verdict(pid, hit, argv, needle, exclude)
        if row:
            found.append(row)
    return found


def _holders_proc(real, is_dir, needle, exclude) -> list[dict]:
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError as exc:
        raise NotAvailable("détenteurs d'un fichier", "%s illisible : %s" % (PROC, exc)) from None
    try:
        os.listdir(os.path.join(PROC, str(os.getpid()), "fd"))
    except OSError as exc:
        raise NotAvailable("détenteurs d'un fichier",
                           "fichiers ouverts illisibles même pour ce processus (%s)"
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
                hit = "fichier ouvert"
                break
        if not hit and is_dir:
            try:
                if _inside(os.readlink(os.path.join(PROC, pid, "cwd")), real, True):
                    hit = "dossier courant"
            except OSError:
                pass
        try:
            with open(os.path.join(PROC, pid, "cmdline"), "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            argv = []
        row = _verdict(int(pid), hit, argv, needle, exclude)
        if row:
            found.append(row)
    return found
