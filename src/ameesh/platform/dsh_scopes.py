# SPDX-License-Identifier: AGPL-3.0-only
"""#3226 : les scopes systemd `dsh-subprocess-*` du gestionnaire de l'utilisateur.

Sans la garde #490 (adaptateur absent, ou dégradé #1460), le fournisseur
`@deepseek-ai/dsh-subprocess-local` lance chaque commande d'outil par
`systemd-run --user --scope --unit=dsh-subprocess-<pid dsh>-<hex>` : elle vit
alors sous `user@<uid>.service/app.slice`, hors du groupe et du cgroup du tour,
et survit à la fin du tour (agents-2, 13/10 : quatre scopes du tour
1eafef02 encore là dix jours plus tard).

Ce module ne fait que RELEVER ces scopes et signaler leurs processus ; le
verdict (tour clos, délai de grâce, doute) est celui du ménage
(`housekeeping.reap_dsh_scopes`). Le signal vise des processus identifiés par
(pid, heure de démarrage), jamais un pid seul ; rien n'est écrit dans la
hiérarchie cgroup (le gestionnaire retire lui-même un scope vidé).
"""
from __future__ import annotations

import os
import re
import signal as signal_mod
import time
from pathlib import Path

from . import NotAvailable
from . import process

#: monture de la hiérarchie cgroup v2 unifiée ; surchargée par les tests
CGROUP_ROOT = "/sys/fs/cgroup"

#: nom d'unité posé par dsh-subprocess-local 0.2.0-rc.2 (cf. `scopedBindings`
#: de deepseek_cgroup.cjs) : le pid du processus dsh qui l'a lancée, puis un hex
SCOPE_RE = re.compile(r"^dsh-subprocess-(?:probe-)?(\d+)-[0-9a-f]+\.scope$")

#: attente bornée après SIGTERM, puis après SIGKILL
TERM_WAIT_S = 10.0
KILL_WAIT_S = 5.0


def manager_dir(uid: int | None = None, root_dir: str = CGROUP_ROOT) -> Path:
    """Le cgroup du gestionnaire systemd de l'utilisateur `uid` (défaut : le
    processus courant), où `systemd-run --user --scope` range ses scopes."""
    if uid is None:
        uid = os.getuid()
    return Path(root_dir) / ("user.slice/user-%d.slice/user@%d.service" % (uid, uid))


def _pids(scope: Path) -> list[int] | None:
    """Les pids du scope et de ses sous-cgroups ; None si illisible."""
    out: list[int] = []
    try:
        for directory, _dirs, files in os.walk(scope):
            if "cgroup.procs" not in files:
                continue
            with open(os.path.join(directory, "cgroup.procs"), encoding="ascii") as fh:
                out.extend(int(line) for line in fh if line.strip())
    except (OSError, ValueError):
        return None
    return sorted(set(out))


def _member(pid: int) -> dict | None:
    """Un processus du scope : `start`, `turn` (AMEESH_TURN_ID), `tmpdir`,
    `command` ; `unread` vrai si son environnement ou son heure de démarrage
    est illisible. None s'il a disparu pendant la lecture."""
    try:
        start = process.start_time(pid)
    except NotAvailable:
        return {"pid": pid, "start": None, "turn": None, "tmpdir": None,
                "command": process.command_line(pid)[:200], "unread": True}
    if start is None:
        return None
    env = process.environ(pid)
    if env is None and not process.alive(pid):
        return None
    return {"pid": pid, "start": start,
            "turn": (env or {}).get("AMEESH_TURN_ID") or None,
            "tmpdir": (env or {}).get("TMPDIR") or None,
            "command": process.command_line(pid)[:200], "unread": env is None}


def owner_alive(dsh_pid: int, members: list[dict]) -> bool:
    """Le processus dsh nommé par le scope vit-il encore ? Un pid recyclé
    (démarré APRÈS le plus ancien membre du scope) ne compte pas ; une heure
    illisible, un membre sans heure : vivant (le doute garde)."""
    try:
        start = process.start_time(dsh_pid)
    except NotAvailable:
        return True
    if start is None:
        return False
    starts = [m["start"] for m in members]
    if not starts or any(s is None for s in starts):
        return True
    return start <= min(starts) + process.START_TOLERANCE_S


def scan(*, uid: int | None = None, root_dir: str = CGROUP_ROOT) -> list[dict] | None:
    """Les scopes `dsh-subprocess-*` non vides du gestionnaire de l'utilisateur,
    par nom : `unit`, `path`, `dsh_pid`, `owner_alive`, `members` (cf.
    `_member`), `unreadable` (liste des pids illisible). None sans
    gestionnaire systemd lisible (hôte non Linux, cgroup v1, pas de session
    utilisateur) : rien n'est vu, rien n'est conclu."""
    base = manager_dir(uid, root_dir)
    if not base.is_dir():
        return None
    candidates = []
    try:
        for entry in sorted(base.iterdir()):
            # au niveau du gestionnaire, et une tranche plus bas (app.slice…)
            if entry.name.endswith(".slice") and entry.is_dir():
                candidates.extend(sorted(entry.iterdir()))
            else:
                candidates.append(entry)
    except OSError:
        return None
    out = []
    for path in candidates:
        match = SCOPE_RE.match(path.name)
        if not match or not path.is_dir():
            continue
        pids = _pids(path)
        if pids == []:
            continue  # vide : le gestionnaire le retire
        members = [m for m in (_member(pid) for pid in pids or ()) if m is not None]
        if pids is not None and not members:
            continue  # tous partis pendant la lecture
        dsh_pid = int(match[1])
        out.append({"unit": path.name, "path": str(path), "dsh_pid": dsh_pid,
                    "owner_alive": owner_alive(dsh_pid, members),
                    "members": members, "unreadable": pids is None})
    return out


def _still_there(members: list[dict]) -> list[dict]:
    left = []
    for m in members:
        try:
            start = process.start_time(m["pid"])
        except NotAvailable:
            left.append(m)
            continue
        if process.same_start(m["start"], start) and process.alive(m["pid"]):
            left.append(m)
    return left


def _signal(members: list[dict], sig: int) -> None:
    for m in members:
        try:
            os.kill(int(m["pid"]), sig)
        except (ProcessLookupError, PermissionError):
            continue


def _wait(members: list[dict], timeout: float) -> list[dict]:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        left = _still_there(members)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.05)


def terminate(members: list[dict], *, term_wait: float = TERM_WAIT_S,
              kill_wait: float = KILL_WAIT_S) -> list[dict]:
    """SIGTERM aux membres relevés, attente bornée, puis SIGKILL à ceux qui
    restent. Seul un membre au même (pid, heure de démarrage) est visé : un
    pid recyclé entre-temps est laissé. Rend les membres encore vivants."""
    members = [m for m in members if m.get("start") is not None]
    left = _still_there(members)
    if not left:
        return []
    _signal(left, signal_mod.SIGTERM)
    left = _wait(left, term_wait)
    if not left:
        return []
    _signal(left, signal_mod.SIGKILL)
    return _wait(left, kill_wait)
