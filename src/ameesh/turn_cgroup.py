# SPDX-License-Identifier: AGPL-3.0-only
"""L498 : un cgroup v2 délégué par tour (propriété stable, nettoyage borné).

Ce fichier sert à deux choses.

1. Les primitives de l'exécuteur et du suivi du travail de fond : reconnaître
   la racine déléguée de l'unité (`<unité>/runner` -> `<unité>`, posée par
   `Delegate=yes` + `DelegateSubgroup=runner`), créer `turn-<id>/`, s'y
   déplacer, lire `cgroup.events`, tuer par `cgroup.kill` et enlever le
   cgroup. Les chemins manipulés sont **relatifs à la monture cgroup v2**
   (`/user.slice/…/turn-<id>`, la forme de `/proc/self/cgroup`) ; leur chemin
   noyau est `CGROUP_ROOT` + ce chemin.

2. Un trampoline autonome, lancé par l'exécuteur à la place du harnais :

       python turn_cgroup.py --cgroup <chemin> --ready-fd <n> -- <harnais…>

   Il se déplace DANS le cgroup du tour AVANT que le harnais n'exécute quoi
   que ce soit, relit `/proc/self/cgroup`, refuse si la position n'est pas
   exactement celle attendue (ou si le chemin sort de la racine par un lien),
   écrit `READY` sur le descripteur hérité, puis `exec` le harnais : même pid,
   même session, même groupe — les descendants suivent par `fork`/`exec` et
   `setsid` ne les fait plus sortir.

Aucune dépendance au paquet installé : le trampoline tourne dans le dossier de
travail du harnais, qui n'est pas forcément importable (même modèle que
`platform/audit_guard.py`). Ce module n'importe que la bibliothèque standard.
"""
from pathlib import Path
import os
import sys
import time

#: monture de la hiérarchie cgroup v2 unifiée
CGROUP_ROOT = "/sys/fs/cgroup"
#: fichier du noyau qui donne le cgroup du processus courant
PROC_CGROUP = "/proc/self/cgroup"
#: sous-groupe de l'unité où vit l'exécuteur (`DelegateSubgroup=`)
SUBGROUP = "runner"
#: préfixe des cgroups de tour sous la racine déléguée
PREFIX = "turn-"
#: signal de placement validé, lu par l'exécuteur sur le descripteur dédié
READY = "AMEESH_TURN_CGROUP_READY"
#: code de sortie du trampoline quand le placement est refusé
REFUSED = 125
#: attente maximale de `populated 0` après `cgroup.kill`
KILL_TIMEOUT = 5.0
#: caractères admis dans un identifiant de tour (uuid hex aujourd'hui)
_SAFE = frozenset("0123456789abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ-_")
_NAME_MAX = 120


# --------------------------------------------------------------------------
# chemins
# --------------------------------------------------------------------------

def own_cgroup(proc: str | None = None) -> str | None:
    """Le cgroup v2 du processus courant (`/user.slice/…`), None si inconnu.

    `proc` : un fichier au format de `/proc/self/cgroup` (les témoins)."""
    try:
        text = Path(proc or PROC_CGROUP).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:] or "/"
    return None


def delegated_root(own: str | None, *, subgroup: str = SUBGROUP) -> str | None:
    """La racine déléguée de l'exécuteur, vue de son propre cgroup.

    Avec `DelegateSubgroup=runner`, l'exécuteur vit dans `<unité>/runner/` et
    la racine déléguée est `<unité>` — libre de tout processus, donc apte à
    recevoir des enfants (contrainte « no internal process »). Un exécuteur
    qui vit ailleurs (délégation absente ou sans sous-groupe) rend None : le
    repli PID/PGID s'applique, rien n'est tenté."""
    if not own or not own.startswith("/"):
        return None
    parts = own.rstrip("/").split("/")
    if len(parts) < 2 or parts[-1] != subgroup:
        return None
    return "/".join(parts[:-1]) or "/"


def _target(path: str, root_dir: str = CGROUP_ROOT) -> Path | None:
    """Le chemin noyau de `path`, ou None s'il sort de la monture ou la
    traverse par un lien symbolique (jamais de chemin détourné)."""
    base = Path(root_dir)
    rel = str(path or "").lstrip("/")
    if not rel:
        return None
    target = base / rel
    try:
        resolved = target.resolve()
        resolved.relative_to(base.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved == target else None


def turn_name(turn_id: str) -> str:
    """`turn-<id>` ; ValueError si l'identifiant n'est pas sûr (jamais de
    chemin construit depuis une entrée non validée)."""
    text = str(turn_id)
    if not text or len(text) > _NAME_MAX or any(c not in _SAFE for c in text):
        raise ValueError("unsafe turn id: %r" % (turn_id,))
    return PREFIX + text


def turn_id_of(name: str) -> str:
    """L'identifiant d'un répertoire `turn-<id>`, "" si ce n'en est pas un."""
    if not name.startswith(PREFIX):
        return ""
    rest = name[len(PREFIX):]
    try:
        turn_name(rest)
    except ValueError:
        return ""
    return rest


def path_for(root: str, turn_id: str) -> str:
    """Le chemin (relatif à la monture) du cgroup d'un tour."""
    return "%s/%s" % (str(root).rstrip("/"), turn_name(turn_id))


def usable(root: str, *, root_dir: str = CGROUP_ROOT) -> bool:
    """La racine déléguée est-elle utilisable telle quelle ?

    Il faut : un répertoire réel (pas un lien), `cgroup.kill`,
    `cgroup.events` et `cgroup.procs`, le droit d'écriture, et **aucun
    processus** dans la racine (sinon `DelegateSubgroup=` manque et les
    enfants ne peuvent pas être créés proprement)."""
    target = _target(root, root_dir)
    if target is None:
        return False
    try:
        if not target.is_dir() or target.is_symlink():
            return False
        for name in ("cgroup.kill", "cgroup.events", "cgroup.procs"):
            if not (target / name).exists():
                return False
        if not os.access(target, os.W_OK | os.X_OK):
            return False
        return not (target / "cgroup.procs").read_text().split()
    except OSError:
        return False


def root_for_runner(*, proc: str | None = None,
                    root_dir: str = CGROUP_ROOT) -> tuple[str | None, str]:
    """`(racine, raison)` : la racine déléguée utilisable, ou None et
    pourquoi (journalisé une fois par l'exécuteur, jamais une erreur)."""
    own = own_cgroup(proc)
    if own is None:
        return None, "own cgroup unknown"
    root = delegated_root(own)
    if root is None:
        return None, ("unit not delegated with DelegateSubgroup=%s (own cgroup %s)"
                      % (SUBGROUP, own))
    if not usable(root, root_dir=root_dir):
        return None, "delegated root %s not usable" % root
    return root, ""


# --------------------------------------------------------------------------
# cgroup d'un tour
# --------------------------------------------------------------------------

def create(root: str, turn_id: str, *, root_dir: str = CGROUP_ROOT) -> str | None:
    """Crée `<racine>/turn-<id>/` ; None si refusé (jamais d'exception).

    Un répertoire qui existe déjà est refusé : on ne réutilise pas le cgroup
    d'un autre tour, même identifiant ou pas."""
    base = _target(root, root_dir)
    if base is None:
        return None
    try:
        name = turn_name(turn_id)
    except ValueError:
        return None
    target = base / name
    if _target(path_for(root, turn_id), root_dir) is None:
        return None
    try:
        target.mkdir(mode=0o755)
    except OSError:
        return None
    return path_for(root, turn_id)


def enter(path: str, *, proc: str | None = None, root_dir: str = CGROUP_ROOT) -> bool:
    """Se déplace dans `path` et vérifie la position sur `/proc/self/cgroup`.

    False (jamais d'exception) si le déplacement échoue ou si le noyau
    rapporte un autre cgroup : le harnais ne doit pas tourner ailleurs."""
    target = _target(path, root_dir)
    if target is None:
        return False
    try:
        (target / "cgroup.procs").write_text(str(os.getpid()))
    except OSError:
        return False
    return own_cgroup(proc) == str(path)


def populated(path: str, *, root_dir: str = CGROUP_ROOT) -> bool | None:
    """`cgroup.events` dit-il que le cgroup contient des processus ?
    None si le cgroup ou le fichier est illisible (jamais d'exception)."""
    target = _target(path, root_dir)
    if target is None:
        return None
    try:
        text = (target / "cgroup.events").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("populated "):
            parts = line.split()
            return len(parts) > 1 and parts[1] == "1"
    return None


def memory_peak(path: str, *, root_dir: str = CGROUP_ROOT) -> int | None:
    """Le pic mémoire du cgroup en octets (`memory.peak`, sinon
    `memory.current`), None si aucun des deux n'est lisible."""
    target = _target(path, root_dir)
    if target is None:
        return None
    for name in ("memory.peak", "memory.current"):
        try:
            return int((target / name).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def kill(path: str, *, timeout: float = KILL_TIMEOUT,
         root_dir: str = CGROUP_ROOT) -> bool:
    """`cgroup.kill`, attente bornée de `populated 0`, puis `rmdir`.

    True si le cgroup est vidé et enlevé (ou n'existait déjà plus) ; False
    sinon (il est signalé orphelin avec son chemin, jamais enlevé de force)."""
    target = _target(path, root_dir)
    if target is None:
        return False
    if not target.exists():
        return True
    try:
        (target / "cgroup.kill").write_text("1")
    except OSError:
        return False
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if not target.exists():
            return True
        if populated(path, root_dir=root_dir) is False:
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return remove(path, root_dir=root_dir)


def remove(path: str, *, root_dir: str = CGROUP_ROOT) -> bool:
    """Enlève un cgroup vide (`rmdir`). True s'il n'existe plus après l'appel
    (enlevé ou déjà absent) ; False s'il reste peuplé, a des enfants, ou
    résiste."""
    target = _target(path, root_dir)
    if target is None:
        return False
    if not target.exists():
        return True
    if populated(path, root_dir=root_dir) is not False:
        return False
    try:
        target.rmdir()
        return True
    except OSError:
        return False


def orphans(root: str, *, root_dir: str = CGROUP_ROOT) -> list[str]:
    """Les cgroups `turn-*` présents sous la racine, dans l'ordre des noms."""
    base = _target(root, root_dir)
    if base is None:
        return []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out: list[str] = []
    for name in names:
        if not turn_id_of(name):
            continue
        candidate = "%s/%s" % (str(root).rstrip("/"), name)
        target = _target(candidate, root_dir)
        try:
            if target is not None and target.is_dir() and not target.is_symlink():
                out.append(candidate)
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------
# trampoline
# --------------------------------------------------------------------------

def command(path: str, ready_fd: int, argv: list) -> list:
    """La ligne de commande du trampoline, à la place du harnais."""
    return [sys.executable, str(Path(__file__).resolve()),
            "--cgroup", str(path), "--ready-fd", str(int(ready_fd)), "--", *argv]


def _fail(message: str) -> int:
    sys.stderr.write("L498: %s\n" % message)
    return REFUSED


def main(args: list) -> int:
    cgroup = ""
    ready_fd: int | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--" and index + 1 <= len(args):
            index += 1
            break
        if arg == "--cgroup" and index + 1 < len(args):
            cgroup = args[index + 1]
            index += 2
            continue
        if arg == "--ready-fd" and index + 1 < len(args):
            try:
                ready_fd = int(args[index + 1])
            except ValueError:
                return _fail("invalid --ready-fd")
            index += 2
            continue
        return _fail("unexpected argument %r" % (arg,))
    argv = args[index:]
    if not cgroup or ready_fd is None or not argv:
        return _fail("usage: turn_cgroup.py --cgroup PATH --ready-fd FD -- ARGV")
    if _target(cgroup) is None:
        return _fail("cgroup %s outside %s or behind a link" % (cgroup, CGROUP_ROOT))
    if not enter(cgroup):
        return _fail("could not enter cgroup %s (refused)" % cgroup)
    if populated(cgroup) is not True:
        return _fail("cgroup %s is not populated after placement" % cgroup)
    try:
        os.write(ready_fd, (READY + "\n").encode("ascii"))
        os.close(ready_fd)
    except OSError as exc:
        return _fail("could not signal READY (%s)" % exc)
    try:
        os.execvpe(argv[0], argv, os.environ)
    except OSError as exc:
        return _fail("could not exec %s (%s)" % (argv[0], exc))
    return REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
