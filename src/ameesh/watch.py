# SPDX-License-Identifier: AGPL-3.0-only
"""L121: the watch of an agent, managed by ameesh.

  ameesh watch enable --agent A [--every 30m]   installs the timer on this host
  ameesh watch disable --agent A                removes it
  ameesh watch show [--agent A] [--json]        the watches of the mesh
  ameesh watch send --agent A                   one firing (what the timer runs)
  ameesh watch at --agent A --in 45m --message M  one-shot wake (lot #1020)
  ameesh watch cancel --agent A --wake S        removes an armed wake
  ameesh watch fire --agent A --wake S          what a wake unit runs

A watch is a systemd user timer (`ameesh-watch-<agent>.timer`) that runs
`ameesh watch send` every period. Before waking anyone, a firing does its own
reading — the projects board (agents and open lots), the active alerts and the
agent's due lots (`actionable`, `due_wakes`) — and fingerprints it twice
(#5698, #8097). The full reading records every movement, for the passive note
and the attribution printed on the firing's line. The waking reading keeps
only what calls for a gesture of the instruction shipped with the package
(`consignes/veille.md`, versioned): an agent stopped or blocked outside a
deliberate stop or an automatic sleep, a turn over 40 minutes, a session over
its rotation threshold ("giant session"), an orchestrator left paused, the
agent's due lots, a critical alert or a new alert type. A
waking change sends the instruction, from `veille`, and stores the reading on
the watch row; a waking change seen within 30 minutes of the agent's own turn
is kept pending (nothing sent, the next firing whose turn is older sends it).
Only a NEW signal opens a turn: a resolved one (agent resumed, turn ended,
session rotated, alert resolved) does not wake on its own.
An ordinary movement (a lot passing to qa elsewhere, an assignment, an alert
key rotating) never opens a turn: if the agent had a turn within the last 30
minutes nothing is sent, otherwise a short `--info` note is deposited (read at
the next turn), the same note at most once every 6 hours (#7625). An
unreadable reading never mutes the watch: the instruction is sent.
The watch is recorded in the database (`agent_watches`, 0362): `ameesh
projects` shows it from any host, and enabling or disabling it is noted in
the agent's thread. A timer whose watch was disabled, or moved to another
host, sends nothing.

#7933: a watch attached to an **elected role** (`releve: election`, canon §11)
is not owned by the host recorded at enable time: the host that holds the
role's live lease emits, and it takes the watch row over (fenced by the lease
epoch, in the same transaction as the message). A candidate that does not
hold the lease stands by, so a handover needs no re-registration and never
produces two emitters for one epoch. Such a watch is installed on every
candidate host (`ameesh watch enable` on each, with `role run`); `ameesh
watch show` names the lease holder. The alerts pass raises `watch_silent`
when an enabled watch has deposited nothing for longer than its passive
report window, so a mute watch no longer goes unnoticed.

A **wake** (`at`) is the one-shot of the same tooling: a systemd user timer
`ameesh-wake-<len(agent)>-<agent>-<slug>.timer` that fires once and sends the
agent its own message. Outside a turn nothing sets `AGENT_MAIL_NAME` (the
runner sets it only for the session it launches), so the wake applies the
identity by construction on its exec line (`env AGENT_MAIL_NAME=<agent> …`)
and `watch fire` refuses to fire without it: seven hand-made watches lost
their wake on 2026-10-13 because their unit carried no identity (mesh-design,
lot #1020). A wake lives in the user units directory of the host
(`ameesh-wake-<len(agent)>-<agent>-<slug>.{service,timer,env,wake.json}`), is
removed once it has fired, and needs no `agent_watches` row. Its dedicated
prefix and the length of the agent keep it away from the periodic watches and
from the other wakes (lot #1039).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
import time
from typing import Callable

from . import config as config_mod
from . import db as db_mod
from . import fil, platform, registry, storage
from .config import NAME_RE, Config

#: the sender of the instruction (`ameesh mail send --from veille`)
SENDER = "veille"
DEFAULT_EVERY_S = 1800
MIN_EVERY_S = 60
UNIT_PREFIX = "ameesh-watch-"
#: lot #1039: the one-shot wakes carry their own prefix. Named
#: `ameesh-watch-<agent>-<slug>` they were exactly the units of the periodic
#: watch of an agent called `<agent>-<slug>`: arming a wake stopped that
#: watch's timer, overwrote its units and, after the firing, removed them —
#: the watch was lost while its `agent_watches` row still showed it active.
WAKE_PREFIX = "ameesh-wake-"
#: the hand-written units L121 replaces (`ameesh-veille-<agent>.timer`)
LEGACY_PREFIX = "ameesh-veille-"
#: the directory of the user units (tests: a temporary one)
UNIT_DIR_ENV = "AMEESH_SYSTEMD_USER_DIR"
#: the configuration of the timer's commands: ameesh's environment file of the
#: host (the runner's convention), then the variables pinned at enable, which win
HOST_ENV_FILE = "%h/.config/ameesh/env"
#: #8380 : le PATH écrit dans une unité de veille ou de réveil. Il est rendu,
#: jamais recopié de `os.environ["PATH"]` : le lanceur d'ameesh préfixe déjà
#: son venv, et le préflight de staging (`systemd-run --user`) ne partage pas
#: le PATH d'un shell d'agent. Même valeur que les unités de `role timers`,
#: déjà indépendantes de l'appelant.
UNIT_PATH = "%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
#: #8380 : la ligne de PATH qu'un contrôle neutralise avant de comparer une
#: unité installée (elle vient de l'appelant, pas du contrat de l'unité).
_UNIT_PATH_LINE = re.compile(r'^Environment="?PATH=[^"\n]*"?$', re.MULTILINE)
#: variables that choose the configuration, the database and the host, by
#: canonical name, with the names `config.load` also reads, in its order. The
#: values resolved when the watch is enabled are pinned under the canonical
#: name and the other names are emptied (an empty value counts as unset): a
#: timer firing with other values reads another database, or skips delivery
#: because the host differs (codex r1 and r2, #303)
PINNED_ENV = {
    "AMEESH_CONFIG": ("AMEESH_CONFIG", "AGENT_MESH_CONFIG"),
    "AMEESH_DSN": ("AMEESH_DSN", "AGENT_MESH_DSN", "AMEESH_DATABASE_URL",
                   "AGENT_MESH_DATABASE_URL"),
    "AMEESH_SCHEMA": ("AMEESH_SCHEMA", "AGENT_MESH_SCHEMA"),
    "AMEESH_DRIVER": ("AMEESH_DRIVER", "AGENT_MESH_DRIVER"),
    "AMEESH_BACKEND": ("AMEESH_BACKEND", "AGENT_MESH_BACKEND"),
    "AMEESH_HOST": ("AMEESH_HOST", "AGENT_MESH_HOST"),
    "AMEESH_STATE": ("AMEESH_STATE", "AGENT_MESH_STATE"),
    "AMEESH_THREADS": ("AMEESH_THREADS",),
}
#: a firing longer than this is stopped by systemd: it never holds the watch's
#: row (and the moves or disables waiting for it) for long (codex r2, #303)
FIRING_TIMEOUT_S = 300
#: #5698 : fenêtre d'un tour récent. Un destinataire qui a eu un tour dans
#: cette fenêtre ne reçoit pas de note « rien n'a changé » : elle serait lue
#: au tour suivant, sans rien lui apprendre de neuf.
RECENT_TURN_S = 1800
#: #8097 : un tour qui dure plus longtemps appelle un redémarrage (consigne
#: veille.md §1, « a turn over 40 min »).
LONG_TURN_S = 40 * 60
#: #8097 : les arrêts qui sont un geste délibéré (arrêt manuel, retrait du
#: canon, session externe) et non une erreur à reprendre. Un arrêt
#: automatique de l'exécuteur (`erreur`, `bail_expire`) reste un signal.
DELIBERATE_STOPS = ("manuel", "retire_du_canon", "externe")
#: #8097 : les parties de la lecture complète, dans l'ordre d'attribution.
READING_PARTS = ("agents", "lots", "alerts", "due")
#: #8097 : les parties dont un mouvement appelle un geste de la veille.
#: Les lots ouverts de TOUT le mesh n'en sont pas : seuls les lots dus à
#: l'agent comptent, et ils sont déjà dans `due`.
WAKING_PARTS = ("agents", "alerts", "due")
#: #8097 : version du document rangé dans `agent_watches.last_reading`. La
#: valeur d'avant était un sha256 nu, sans partition : elle est lue comme une
#: lecture sans précédent (une invite, puis le document prend le relais).
READING_VERSION = 2
#: #5698 : clé du méta du fil qui porte l'empreinte de la lecture d'un tir.
READING_KEY = "watch"
#: #7933 : un rôle élu n'appartient à aucun hôte inscrit ; `releve` le dit sur
#: la fiche de l'agent (`role_run.live`), et le détenteur du bail émet.
ELECTED_RELEVE = "election"
#: #7933 (ameesh-cx2) : l'unité d'un rôle élu tique à ce pas, bien plus court
#: que la période d'envoi, pour voir une relève dans les 5 min du canon §11 ;
#: `send` ne dépose qu'une fois `every_s` écoulé (et bat à chaque autre tir).
ROLE_TICK_S = 60
#: #5698 : la note passive déposée quand la lecture ne réveille pas (elle
#: n'ouvre pas de tour : elle est lue au tour suivant). #8097 : le corps reste
#: constant — un mouvement ordinaire (lot, affectation, clé d'alerte) la
#: dépose au même titre qu'une lecture inchangée, et #7625 la dédoublonne sur
#: cette constante, au plus une toutes les 6 h.
UNCHANGED_TEXT = (
    "WATCH (no action) — %s: nothing needs your attention since the last watch "
    "(no waking change in projects, alerts or due lots). No action expected; "
    "this note is read at your next turn."
)
#: lot #1020: the identity a wake unit carries BY CONSTRUCTION. The runner
#: sets it for the session it launches, but not for a systemd unit an agent
#: arms for later: without this line the wake fails at the deadline with
#: `unbound sender` and is lost (seven watches, 2026-10-13).
IDENTITY_ENV = "AGENT_MAIL_NAME"
#: the slug of a wake names its units and its state file
WAKE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
#: #4755: the exec line of a wake unit (`watch fire --agent A --wake S`)
LEGACY_FIRE_RE = re.compile(
    r"\bwatch fire --agent (?P<agent>\S+) --wake (?P<slug>[a-z0-9][a-z0-9-]{0,40})\s*$",
    re.M)
#: a wake fires at least this far in the future
MIN_WAKE_S = 10
#: the identity witness reads the installed unit back through these two
#: properties: the declaration (`Environment=`) and the copy applied on the
#: exec line (`ExecStart`), which no environment file can override
WITNESS_PROPERTY = "Environment"
WITNESS_EXEC_PROPERTY = "ExecStart"
CONSIGNE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "consignes", "veille.md")


class WatchError(Exception):
    """A refused or failed watch command (exit 1)."""


class _ReportJustSent(Exception):
    """#7625 : le dépôt a été dédoublonné dans la fenêtre des comptes rendus."""


def staging_hosts_text(topology=None) -> str:
    """#7796: the staging hosts named by the instruction, read from the canon.

    The bare profile declares none: the text says so instead of naming the
    hosts of one mesh."""
    hosts = tuple(getattr(topology, "hosts", ()) or ())
    if not hosts:
        return "none declared by the canon yet"
    workstation = getattr(topology, "workstation", None)
    return ", ".join("%s (workstation)" % host if host == workstation else host
                     for host in hosts)


def instruction(agent: str, topology=None) -> str:
    """The instruction sent to `agent`, from the versioned text.

    `topology` is the canon's staging sequence (`release_cutoff.
    staging_topology_of`); without it the text names no host (#7796)."""
    with open(CONSIGNE_PATH, encoding="utf-8") as fh:
        return string.Template(fh.read()).safe_substitute(
            agent=agent, staging_hosts=staging_hosts_text(topology)).strip()


#: L148 (stage 2c): old spelling kept for the callers not yet renamed.
consigne = instruction


def parse_every(text: str) -> int:
    """« 30m », « 2h », « 900 » → whole seconds, at least `MIN_EVERY_S`."""
    from . import stagnation
    try:
        seconds = int(stagnation.parse_duration(text))
    except stagnation.StaleError as exc:
        raise WatchError(str(exc)) from exc
    if seconds < MIN_EVERY_S:
        raise WatchError("--every: at least %d s, got %r" % (MIN_EVERY_S, text))
    return seconds


def every_text(seconds: int) -> str:
    if seconds % 3600 == 0:
        return "%dh" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%dm" % (seconds // 60)
    return "%ds" % seconds


def unit_dir(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    if env.get(UNIT_DIR_ENV):
        return env[UNIT_DIR_ENV]
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "systemd", "user")


def unit_name(agent: str, suffix: str) -> str:
    return "%s%s.%s" % (UNIT_PREFIX, agent, suffix)


def env_name(agent: str) -> str:
    """The environment file of the watch of `agent`, next to its units."""
    return "%s%s.env" % (UNIT_PREFIX, agent)


def pinned_env(env: dict, host: str) -> str:
    """The environment file of a watch: for each `PINNED_ENV` group set in
    `env`, its resolved value under the canonical name and the other names
    emptied; `AMEESH_HOST` always, the host the watch is recorded for. In
    systemd's quoting; a value on several lines is refused."""
    values: dict[str, str] = {}
    for canonical, names in PINNED_ENV.items():
        value = next((env[n] for n in names if env.get(n)), "")
        if canonical == "AMEESH_HOST":
            value = host
        if not value:
            continue
        values[canonical] = value
        for alias in names:
            if alias != canonical:
                values[alias] = ""
    lines = []
    for name, value in values.items():
        if "\n" in value or "\r" in value:
            raise WatchError("%s spans several lines: cannot pin it for the timer" % name)
        quoted = "".join("\\" + c if c in '\\"`$' else c for c in value)
        lines.append('%s="%s"' % (name, quoted))
    return "".join(line + "\n" for line in lines)


def units(agent: str, every_s: int, ameesh: str,
          directory: str = "", *, role: str | None = None) -> dict[str, str]:
    """The service and the timer of the watch of `agent` (`directory`: where
    its environment file is written).

    The PATH is `UNIT_PATH`, never the caller's (#8380): the same file must be
    rendered by an agent's shell and by the staging preflight, or a pose from
    one is "out of date" for the other.

    `role` (the agent's elected role, #7933) wraps the firing in
    ``ameesh role run <role> -- … watch send``: only the lease holder reaches
    the sending code, and the child carries the lease epoch as its fencing
    token. Its timer ticks every `ROLE_TICK_S` so a handover is seen well
    inside the canon's five minutes; `send` gates the actual deposit on
    `every_s`. A plain watch keeps its direct exec line and its period."""
    if role:
        exec_line = ("\"%(ameesh)s\" role run %(role)s -- \"%(ameesh)s\" "
                     "watch send --agent %(agent)s"
                     % {"ameesh": ameesh, "role": role, "agent": agent})
    else:
        exec_line = ("\"%(ameesh)s\" watch send --agent %(agent)s"
                     % {"ameesh": ameesh, "agent": agent})
    service = (
        "[Unit]\n"
        "Description=ameesh watch of %(agent)s (L121, written by ameesh watch enable)\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=%(timeout)d\n"
        "Environment=\"PATH=%(path)s\"\n"
        "EnvironmentFile=-%(host_env)s\n"
        "EnvironmentFile=-%(own_env)s\n"
        "ExecStart=%(exec_line)s\n"
        % {"agent": agent, "path": UNIT_PATH, "host_env": HOST_ENV_FILE,
           "own_env": os.path.join(directory, env_name(agent)),
           "timeout": FIRING_TIMEOUT_S, "exec_line": exec_line})
    tick_s = min(every_s, ROLE_TICK_S) if role else every_s
    tick_text = ("%s, lease tick every %s" % (every_text(every_s), every_text(tick_s))
                 if tick_s != every_s else every_text(every_s))
    timer = (
        "[Unit]\n"
        "Description=ameesh watch of %(agent)s, every %(every)s\n"
        "[Timer]\n"
        "OnActiveSec=%(s)d\n"
        "OnUnitActiveSec=%(s)d\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
        % {"agent": agent, "every": tick_text, "s": tick_s})
    return {unit_name(agent, "service"): service, unit_name(agent, "timer"): timer}


def _write_unit_files(files: dict[str, str], directory: str) -> list[str]:
    """Atomically write the watch files that changed; return their names.

    The environment file may hold a DSN with its password: owner only, like
    `enable` writes it."""
    os.makedirs(directory, exist_ok=True)
    changed: list[str] = []
    for name, text in files.items():
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == text:
                    continue
        except OSError:
            pass
        tmp = os.path.join(directory, "." + name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        changed.append(name)
    return changed


def elected_watch_files(host: str, db, *, directory: str, bin_path: str,
                        env: dict | None = None) -> list[dict]:
    """#7933 : the watch units this host must carry for the elected roles.

    Every `agent_watches` row whose agent's card carries `releve: election`
    and whose `candidats` name this host gets its timer, whether or not the
    row was recorded here: the lease holder emits, the others stand by. The
    caller is the candidate reconciliation (`ameesh role timers --install`),
    so a host that joins the candidates needs no second `watch enable` — the
    row (period, who enabled it) stays the one gesture that creates a watch.
    """
    from . import role_run
    source = dict(os.environ if env is None else env)
    out = []
    for watched in storage.of(db).watches.list():
        agent = str(watched.get("agent") or "")
        if not agent:
            continue
        try:
            report = role_run.live(db, agent)
        except role_run.RoleError:
            continue
        if str(report.get("releve") or "") != ELECTED_RELEVE:
            continue
        if host not in (report.get("candidates") or ()):
            continue
        role = str(report.get("role") or agent)
        files = units(agent, int(watched["every_s"]), bin_path, directory,
                      role=role)
        files[env_name(agent)] = pinned_env(source, host)
        out.append({"agent": agent, "role": role,
                    "every_s": int(watched["every_s"]), "files": files})
    return out


def reconcile_host(host: str, db, *, directory: str, bin_path: str,
                   env: dict | None = None) -> dict:
    """Write the elected-role watch units of this candidate host (#7933).

    Returns the files written and the timers to enable; the caller applies
    systemd (``role timers --install`` does it in its own transaction of
    gestures). No database row is written: installation is not a re-enable.
    """
    installed, timers, agents = [], [], []
    for entry in elected_watch_files(host, db, directory=directory,
                                     bin_path=bin_path, env=env):
        installed.extend(_write_unit_files(entry["files"], directory))
        timers.append(unit_name(entry["agent"], "timer"))
        agents.append(entry["agent"])
    return {"installed": installed, "timers": timers, "agents": agents}


def _without_unit_path(text: str) -> str:
    """The unit text with its ``Environment=PATH=…`` line blanked (#8380).

    The PATH is not part of the unit's contract: the exec line is absolute and
    the launcher prepends its venv itself. A unit written before #8380 carries
    the PATH of whoever installed it (an agent's shell, the preflight's
    `systemd-run`); comparing that line would call the same file up to date in
    one environment and stale in the other, and the staging preflight runs
    `role timers --check` with the installed CLI before installing the fix.
    """
    return _UNIT_PATH_LINE.sub('Environment="PATH="', text)


def check_host(host: str, db, *, directory: str, bin_path: str,
               env: dict | None = None) -> dict:
    """#7933 : gaps of the elected-role watch units on this candidate host.

    Compares the service and timer with what `reconcile_host` would write and
    names a missing environment file; the caller checks systemd. A watch row
    whose agent is not a candidate here is not a gap. The PATH line is
    normalised before the comparison (#8380): it is caller-dependent, not a
    property of the installed unit."""
    problems: list[str] = []
    timers: list[tuple[str, str]] = []
    for entry in elected_watch_files(host, db, directory=directory,
                                     bin_path=bin_path, env=env):
        agent = entry["agent"]
        for name in (unit_name(agent, "service"), unit_name(agent, "timer")):
            path = os.path.join(directory, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                problems.append("watch of %s: missing %s" % (agent, name))
                continue
            if _without_unit_path(content) != _without_unit_path(entry["files"][name]):
                problems.append("watch of %s: %s is out of date; rerun `ameesh "
                                "role timers --install`" % (agent, name))
        if not os.path.isfile(os.path.join(directory, env_name(agent))):
            problems.append("watch of %s: missing %s" % (agent, env_name(agent)))
        timers.append((agent, unit_name(agent, "timer")))
    return {"problems": problems, "timers": timers}


def wake_name(agent: str, slug: str, suffix: str) -> str:
    """The name of one of the files of the wake `<slug>` of `agent`.

    `ameesh-wake-<len(agent)>-<agent>-<slug>.<suffix>`: the dedicated prefix
    keeps a wake away from the periodic watch units (`ameesh-watch-<agent>.…`),
    and the length of the agent makes the pair (agent, slug) readable back, so
    `a`/`b-c` and `a-b`/`c` — which flatten to the same text — never share a
    file (lot #1039)."""
    return "%s%d-%s-%s.%s" % (WAKE_PREFIX, len(agent), agent, slug, suffix)


def wake_env_name(agent: str, slug: str) -> str:
    """The pinned environment file of a wake (its own: disabling the periodic
    watch of the agent must not take the configuration of an armed wake)."""
    return "%s%d-%s-%s.env" % (WAKE_PREFIX, len(agent), agent, slug)


def wake_units(agent: str, slug: str, delay_s: int, ameesh: str,
               directory: str = "", *, deadline: float | None = None) -> dict[str, str]:
    """The service and the timer of the one-shot wake `<slug>` of `agent`.

    The PATH is `UNIT_PATH`, never the caller's (#8380), for the same reason
    as the periodic watch.

    A wake runs outside a turn, where the runner's `AGENT_MAIL_NAME` is
    absent. `Environment=` alone does not hold it: systemd lets an
    `EnvironmentFile=` override `Environment=` whatever their order
    (documented, and verified on systemd 255), so the identity is ALSO
    applied by `env` on the exec line — the child environment carries it even
    if a host file names another agent. `Restart=on-failure` keeps a wake
    whose firing failed (database down): it retries until it is sent.
    """
    if deadline is None:
        deadline = time.time() + delay_s
    calendar = datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc) \
        .strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    service = (
        "[Unit]\n"
        "Description=ameesh one-shot wake of %(agent)s (%(slug)s)\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=%(timeout)d\n"
        "Environment=\"PATH=%(path)s\"\n"
        "EnvironmentFile=-%(host_env)s\n"
        "EnvironmentFile=-%(own_env)s\n"
        "Environment=\"%(identity)s=%(agent)s\"\n"
        "Restart=on-failure\n"
        "RestartSec=2min\n"
        "ExecStart=\"%(env)s\" \"%(identity)s=%(agent)s\" "
        "\"%(ameesh)s\" watch fire --agent %(agent)s --wake %(slug)s\n"
        % {"agent": agent, "slug": slug, "ameesh": ameesh, "path": UNIT_PATH,
           "env": _env_binary(),
           "host_env": HOST_ENV_FILE,
           "own_env": os.path.join(directory, wake_env_name(agent, slug)),
           "identity": IDENTITY_ENV, "timeout": FIRING_TIMEOUT_S})
    timer = (
        "[Unit]\n"
        "Description=ameesh one-shot wake of %(agent)s (%(slug)s), in %(delay)ds\n"
        "[Timer]\n"
        # #1737: a daemon-reload can rebase OnActiveSec. Keep the original
        # wall-clock deadline instead, including for wakes requested with --in.
        "OnCalendar=%(calendar)s\n"
        "AccuracySec=1s\n"
        % {"agent": agent, "slug": slug, "delay": int(delay_s), "calendar": calendar})
    return {wake_name(agent, slug, "service"): service,
            wake_name(agent, slug, "timer"): timer}


def parse_wake_delay(text: str | None, at: str | None, *, now: float | None = None) -> int:
    """The delay of a wake, from `--in 45m` or `--at 2026-10-13T20:00:00Z`
    (absolute, timezone required: the deadline of a wake is not read twice)."""
    reference = time.time() if now is None else now
    return int(round(parse_wake_deadline(text, at, now=reference) - reference))


def parse_wake_deadline(text: str | None, at: str | None, *,
                        now: float | None = None) -> float:
    """Resolve either CLI form once, before connecting to the database."""
    from . import stagnation
    reference = time.time() if now is None else now
    if bool(text) == bool(at):
        raise WatchError("give exactly one of --in DURATION or --at TIME")
    if text:
        try:
            delay = stagnation.parse_duration(text)
        except stagnation.StaleError as exc:
            raise WatchError("--in: %s" % exc) from exc
    else:
        try:
            when = datetime.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise WatchError("--at: unreadable time %r (%s)" % (at, exc)) from exc
        if when.tzinfo is None:
            raise WatchError("--at: %r has no timezone (use 2026-10-13T20:00:00Z)" % at)
        delay = when.timestamp() - reference
    if delay < MIN_WAKE_S:
        raise WatchError("a wake fires at least %d s from now, got %gs" % (MIN_WAKE_S, delay))
    return reference + delay


#: #4754 : un venv versionné de la bascule (`venv-<sha7>`, #1969), que son
#: ménage (#3792) retire une fois quitté : une unité ne doit jamais le nommer.
VERSIONED_VENV_RE = re.compile(r"^venv-[0-9a-f]{7,40}$")
#: le lien de la version vivante, voisin de tout venv versionné (bascule.py)
CURRENT_VENV = "venv-courant"
#: un chemin absolu vers un binaire d'un venv versionné, dans une ligne d'unité
VERSIONED_BINARY_RE = re.compile(r"/[^\s\"']*/venv-[0-9a-f]{7,40}/bin/[A-Za-z0-9_.+-]+")


def stable_binary(binary: str, home: str | None = None) -> str:
    """#4754 : le chemin à écrire dans une unité pour `binary`. Un binaire d'un
    venv versionné (`…/venv-<sha7>/bin/<nom>`) devient celui du lien vivant
    voisin (`…/venv-courant/bin/<nom>`), sinon `~/.local/bin/<nom>` ; le
    ménage des venv retirerait sinon le chemin figé (203/EXEC). Un autre
    chemin, ou faute de chemin stable exécutable, est rendu tel quel."""
    bin_dir = os.path.dirname(binary)
    venv = os.path.dirname(bin_dir)
    if os.path.basename(bin_dir) != "bin" or not VERSIONED_VENV_RE.match(os.path.basename(venv)):
        return binary
    name = os.path.basename(binary)
    home = home or os.path.expanduser("~")
    for candidate in (os.path.join(os.path.dirname(venv), CURRENT_VENV, "bin", name),
                      os.path.join(home, ".local", "bin", name)):
        if os.access(candidate, os.X_OK):
            return candidate
    return binary


def _ameesh_binary(env: dict | None = None) -> str:
    """The binary a watch or wake unit will run: the stable launcher, never
    the caller's first `ameesh` (#8390, #8458).

    `~/.local/bin/ameesh` is the launcher `UNIT_PATH` names and the stable
    entry point of an installation; a caller's `PATH` may put a private
    directory (an agent's `.menage/bin`, a temporary tree) first, and writing
    that path into `ExecStart` made the unit depend on who armed it. The
    launcher wins when it is executable. Without it the choice is REFUSED:
    falling back to the caller's `PATH` (or keeping any other non-installed
    path) would render two different `ExecStart` from two shells, and a
    private path could disappear before the deadline. #4754's remapping stays
    in `stable_binary` for an existing unit's exec line (`repin_units`)."""
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    stable = os.path.join(home, ".local", "bin", "ameesh")
    if os.access(stable, os.X_OK):
        return stable
    raise WatchError(
        "stable launcher %s is missing or not executable: a watch or wake unit "
        "names it, never the caller's PATH; install it before arming" % stable)


def repin_units(directory: str, *, run: Callable = subprocess.run, dry_run: bool = False,
                home: str | None = None) -> list[dict]:
    """#4754 : réécrit l'`ExecStart` des unités de veille et de réveil
    d'ameesh (`ameesh-watch-*`, `ameesh-wake-*`) qui nomment encore un binaire
    de venv versionné, écrites avant `stable_binary` : une veille dont le venv
    a été retiré échoue en 203/EXEC à chaque tir sans que rien ne le dise.
    Rend une entrée par unité (`path`, `before`, `after`) ; à blanc, n'écrit
    rien. Un seul `daemon-reload` après les réécritures."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    fixed = []
    for name in names:
        if not name.endswith(".service") or not name.startswith((UNIT_PREFIX, WAKE_PREFIX)):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        lines, before, after = [], [], []
        for line in text.splitlines(keepends=True):
            if line.startswith("ExecStart="):
                for old in VERSIONED_BINARY_RE.findall(line):
                    new = stable_binary(old, home=home)
                    if new != old:
                        line = line.replace(old, new)
                        before.append(old)
                        after.append(new)
            lines.append(line)
        if not before:
            continue
        fixed.append({"path": path, "before": before, "after": after})
        if dry_run:
            continue
        tmp = os.path.join(directory, "." + name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(lines))
        os.replace(tmp, path)
    if fixed and not dry_run:
        _systemctl_quiet(run, "daemon-reload")
    return fixed


def _env_binary() -> str:
    """`env` (coreutils), which applies the wake's identity on its exec line:
    systemd's `EnvironmentFile=` overrides `Environment=` (verified on
    systemd 255), so the identity must be set on the command itself.

    The canonical `/usr/bin/env` wins over the caller's `PATH` (#8390): like
    the launcher, `env` must not name a private directory of whoever armed
    the wake."""
    for candidate in ("/usr/bin/env", "/bin/env"):
        if os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("env")
    return os.path.abspath(found) if found else "/usr/bin/env"


def _systemctl_quiet(run: Callable, *args: str) -> None:
    """`systemctl --user …` whose failure is not one: a cleanup runs after the
    message is committed, and must never make the firing fail (watch send's
    rule: nothing fails a firing after its message is committed)."""
    try:
        run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _systemctl_show(run: Callable, prop: str, unit: str) -> str:
    """One property of a unit, read back from systemd (the witness of #1020)."""
    try:
        proc = run(["systemctl", "--user", "show", "-p", prop, "--value", unit],
                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WatchError("systemctl --user show -p %s %s: %s" % (prop, unit, exc)) from exc
    if proc.returncode != 0:
        raise WatchError("systemctl --user show -p %s %s: code %s %s" % (
            prop, unit, proc.returncode, (proc.stderr or "").strip()[:200]))
    return proc.stdout or ""


def _environment_tokens(text: str) -> set[str]:
    """The `NAME=value` tokens systemd renders for the Environment property."""
    try:
        return set(shlex.split(text))
    except ValueError:
        return set((text or "").split())


def _remove_files(directory: str, names: list[str]) -> None:
    for name in names:
        try:
            os.remove(os.path.join(directory, name))
        except FileNotFoundError:
            pass


def _disarm(directory: str, agent: str, slug: str, run: Callable) -> None:
    """Stops and removes the wake `<slug>` of `agent` (best effort: also the
    path taken after a firing, where nothing may fail the committed message)."""
    _systemctl_quiet(run, "stop", wake_name(agent, slug, "timer"))
    _remove_files(directory, [wake_name(agent, slug, "service"),
                              wake_name(agent, slug, "timer"),
                              wake_env_name(agent, slug),
                              wake_name(agent, slug, "wake.json")])
    _systemctl_quiet(run, "daemon-reload")


def _legacy_wake_name(agent: str, slug: str, suffix: str) -> str:
    """#4755: the name a wake carried before #1039 (`ameesh-watch-<agent>-<slug>`)."""
    return "%s%s-%s.%s" % (UNIT_PREFIX, agent, slug, suffix)


def _legacy_wake_of(directory: str, name: str) -> tuple[str, str] | None:
    """(agent, slug) when the service `name` is a wake armed before #1039, read
    from its exec line; None otherwise. The name alone does not tell: it is
    also the periodic watch of an agent called `<agent>-<slug>` (#1039), whose
    service runs `watch send`, never `watch fire`."""
    if not (name.startswith(UNIT_PREFIX) and name.endswith(".service")):
        return None
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    found = LEGACY_FIRE_RE.search(text)
    if not found or _legacy_wake_name(found["agent"], found["slug"], "service") != name:
        return None
    return found["agent"], found["slug"]


def _disarm_legacy(directory: str, agent: str, slug: str, run: Callable) -> bool:
    """Stops and removes the wake `<slug>` of `agent` armed under the prefix
    before #1039, if its service is one; True when it was removed."""
    if _legacy_wake_of(directory, _legacy_wake_name(agent, slug, "service")) != (agent, slug):
        return False
    _systemctl_quiet(run, "stop", _legacy_wake_name(agent, slug, "timer"))
    _remove_files(directory, [_legacy_wake_name(agent, slug, suffix)
                              for suffix in ("service", "timer", "env", "wake.json")])
    _systemctl_quiet(run, "daemon-reload")
    return True


def prune_legacy_wakes(directory: str, run: Callable) -> list[str]:
    """#4755: removes the wakes armed before #1039 left on this host. Their
    firing looks for the state under the new prefix, never finds it and, under
    `Restart=on-failure`, restarts every 2 min forever (8622 restarts of
    `ameesh-watch-deepseek1-1369-m24h.service` since 2026-10-18). Their
    deadline passed long ago: the stale message is not sent. The names of
    the wakes removed."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    removed = []
    for name in names:
        pair = _legacy_wake_of(directory, name)
        if pair and _disarm_legacy(directory, pair[0], pair[1], run):
            removed.append(name[:-len(".service")])
    return removed


def _systemctl(run: Callable, *args: str) -> None:
    """`systemctl --user …`; WatchError when it fails."""
    try:
        proc = run(["systemctl", "--user", *args], capture_output=True, text=True,
                   timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WatchError("systemctl --user %s: %s" % (" ".join(args), exc)) from exc
    if proc.returncode != 0:
        raise WatchError("systemctl --user %s: code %s %s" % (
            " ".join(args), proc.returncode, (proc.stderr or "").strip()[:200]))


def linger_warning(run: Callable, user: str) -> str | None:
    """Why the timer may not run while its owner is away: linger off, or
    unreadable (codex r1, #303). A user timer stops with the last session of
    its user unless linger is on."""
    hint = "run `loginctl enable-linger %s` so that the watch runs while you are away" % user
    try:
        proc = run(["loginctl", "show-user", user, "-p", "Linger", "--value"],
                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return "linger of %s unreadable (%s): %s" % (user, exc, hint)
    value = (proc.stdout or "").strip()
    if proc.returncode != 0 or value not in ("yes", "no"):
        return "linger of %s unreadable (loginctl: code %s %s): %s" % (
            user, proc.returncode, (proc.stderr or value).strip()[:120], hint)
    if value == "no":
        return ("linger is off for %s: the timer stops when your last session ends; %s"
                % (user, hint))
    return None


def authorize(cfg: Config, db, agent: str) -> str:
    """Who enables or disables the watch of `agent`; WatchError if refused.

    An agent sets its own watch. The watch of ANOTHER agent is reserved to
    humans and to orchestrator or design agents, the rule of `work move
    --correct` (`work.CORRECTOR_ROLES`, `assignments.is_orchestrator`): a
    watch makes its agent take a turn every period (mesh-design, 2026-10-12).
    A session with no agent identity is a human at their terminal."""
    from . import assignments, identity, work
    binding = identity.resolve_binding(cfg, db)
    if not binding.name:
        import getpass
        return "human:%s" % getpass.getuser()
    if binding.name == agent \
            or assignments.is_orchestrator(cfg, db, binding.name, roles=work.CORRECTOR_ROLES):
        return binding.name
    raise WatchError(
        "refused: %s sets only its own watch; the watch of %s is set by a human, an "
        "orchestrator or a design agent (role on its Agent card in the canon, "
        "AMEESH_ALERT_ORCHESTRATORS)" % (binding.name, agent))


def _note(cfg: Config, db, agent: str, actor: str, text: str, **meta) -> None:
    """In the thread of the agent's project."""
    row = registry.get(db, agent)
    fil.record(cfg, db, sender=actor, recipients=[agent], text=text,
               project=fil.agent_project(row) if row else None,
               meta=dict({"action": "watch", "audit": "watch"}, **meta))


def _refuse_retired(row: dict | None, agent: str) -> None:
    """#3460 : un agent inconnu ou retiré n'a ni veille ni réveil — jamais
    destinataire."""
    if row is None:
        raise WatchError("unknown agent: %s" % agent)
    if row.get("retired_ts"):
        from . import undeliverable
        raise WatchError(undeliverable.retired_text(row, sender=False))


def elected_role(db, agent: str) -> dict | None:
    """#7933: the live role report of `agent` when its card carries an elected
    role (`releve: election`), else None.

    The report is read without taking a lease (`role_run.live`): the watch
    never claims a role. A missing row is "not a role" — the watch keeps the
    historical host rule. A database error propagates: a doubt must never make
    a watch send from the wrong host, nor silently mute one."""
    from . import role_run
    try:
        report = role_run.live(db, agent)
    except role_run.RoleError:
        return None
    if str(report.get("releve") or "") != ELECTED_RELEVE:
        return None
    return report


def _role_epoch(agent: str, role: dict) -> int | None:
    """#7933: the lease epoch this firing is authorized under.

    A unit wrapped by `role run` carries `AMEESH_ROLE`/`AMEESH_LEASE_EPOCH`:
    the epoch it read before starting the child is the fencing token, and a
    handover between that check and the send refuses the message. A bare unit
    passes the role's live epoch, re-read in the transaction that sends. The
    role name is the canonical one (`AMEESH_ROLE`), an alias being resolved by
    `role_run` before the child starts."""
    from . import role_run
    role_env = (os.environ.get(role_run.ENV_ROLE) or "").strip()
    epoch_env = (os.environ.get(role_run.ENV_EPOCH) or "").strip()
    if role_env == (role.get("role") or agent) and epoch_env.isdigit():
        return int(epoch_env)
    epoch = role.get("epoch")
    return int(epoch) if epoch is not None else None


def enable(cfg: Config, db, agent: str, every_s: int, *, run: Callable = subprocess.run,
           env: dict | None = None, actor: str | None = None) -> list[str]:
    """Installs (or updates) the timer on this host, records the watch, notes
    it in the thread. The warnings to print."""
    if not platform.is_linux():
        raise WatchError("a watch needs systemd (Linux)")
    _refuse_retired(registry.get(db, agent), agent)
    host = cfg.host or ""
    if not host:
        raise WatchError("this host has no name (host of the configuration)")
    actor = actor or authorize(cfg, db, agent)
    warnings = []
    role = elected_role(db, agent)
    before = storage.of(db).watches.list(agent)
    if before and before[0]["host"] != host:
        if role is not None:
            # #7933 : la veille d'un rôle élu n'est pas déplacée par
            # l'activation d'un autre hôte ; chaque candidat porte sa
            # minuterie et le détenteur du bail émet. Le dire évite de faire
            # croire qu'il faut désinscrire l'ancien hôte.
            warnings.append(
                "the watch of %s follows the elected role %s: it emits from the "
                "lease holder, not from the host recorded at enable time (%s); this "
                "host now has its own timer. `ameesh role timers --install` "
                "reconciles it on every candidate; the other timers send only when "
                "their host holds the lease."
                % (agent, role.get("role") or agent, before[0]["host"]))
        else:
            warnings.append(
                "the watch of %s moves from %s to %s: its timer there sends nothing more; "
                "remove it there with `ameesh watch disable --agent %s`"
                % (agent, before[0]["host"], host, agent))
    directory = unit_dir(env)
    if os.path.exists(os.path.join(directory, "%s%s.timer" % (LEGACY_PREFIX, agent))):
        warnings.append(
            "a hand-written watch of %s is still installed: systemctl --user disable --now "
            "%s%s.timer (then remove its unit files)" % (agent, LEGACY_PREFIX, agent))
    os.makedirs(directory, exist_ok=True)
    source = os.environ if env is None else env
    files = units(agent, every_s, _ameesh_binary(source), directory,
                  role=(role.get("role") or agent) if role is not None else None)
    # the pinned variables may hold a DSN with its password: owner only
    files[env_name(agent)] = pinned_env(dict(source), host)
    for name, text in files.items():
        tmp = os.path.join(directory, "." + name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, os.path.join(directory, name))
    _systemctl(run, "daemon-reload")
    # restarted: a new period takes effect now
    _systemctl(run, "enable", unit_name(agent, "timer"))
    _systemctl(run, "restart", unit_name(agent, "timer"))
    storage.of(db).watches.put(agent, every_s, host, actor=actor)
    import getpass
    linger = linger_warning(run, getpass.getuser())
    if linger:
        warnings.append(linger)
    _note(cfg, db, agent, actor,
          "Watch of %s enabled on %s by %s: every %s, instruction consignes/veille.md%s."
          % (agent, host, actor, every_text(every_s),
             (", under the elected role %s (the lease holder emits)"
              % (role.get("role") or agent)) if role is not None else ""),
          state="enabled", every_s=every_s, host=host)
    return warnings


def disable(cfg: Config, db, agent: str, *, run: Callable = subprocess.run,
            env: dict | None = None, actor: str | None = None) -> list[str]:
    """Removes the timer of this host (if any), and the watch when it is this
    host's, or when no timer is left here (`disable` from any host); notes it.

    On a host that is not the watch's (codex r1, #303), only the local units
    go: the watch moved since and its timer keeps running where it is now."""
    actor = actor or authorize(cfg, db, agent)
    warnings = []
    host = cfg.host or ""
    directory = unit_dir(env)
    timer = os.path.join(directory, unit_name(agent, "timer"))
    local = os.path.exists(timer)
    rows = storage.of(db).watches.list(agent)
    if not rows and not local:
        raise WatchError("no watch of %s" % agent)
    if local:
        _systemctl(run, "disable", "--now", unit_name(agent, "timer"))
        for name in (unit_name(agent, "timer"), unit_name(agent, "service"), env_name(agent)):
            try:
                os.remove(os.path.join(directory, name))
            except FileNotFoundError:
                pass
        _systemctl(run, "daemon-reload")
    if rows and rows[0]["host"] != host and local:
        warnings.append("the watch of %s is on %s: only the timer of %s was removed; the "
                        "watch keeps running there" % (agent, rows[0]["host"], host))
        _note(cfg, db, agent, actor, "Old timer of the watch of %s removed from %s by %s; "
              "the watch stays on %s." % (agent, host, actor, rows[0]["host"]),
              state="cleaned", host=host)
        return warnings
    removed = storage.of(db).watches.delete(agent, rows[0]["host"]) if rows else None
    if rows and removed is None:
        raise WatchError("the watch of %s changed meanwhile (`ameesh watch show --agent %s`)"
                         % (agent, agent))
    if removed is not None and removed["host"] != host:
        warnings.append("its timer stays on %s, where it sends nothing more; remove it "
                        "there with the same command" % removed["host"])
    _note(cfg, db, agent, actor, "Watch of %s disabled by %s." % (agent, actor),
          state="disabled")
    return warnings


def foreign_pin(cfg: Config, env: dict | None = None) -> str:
    """#3408: this machine's host when the environment pins ANOTHER host,
    else "". The environment file of a watch (or a wake) pins `AMEESH_HOST`
    for the host that wrote it (#303); copied as is to another host, it made
    the copy pass for the holder there (three duplicate instructions to the
    orchestrator, agents-1, 2026-10-24). The machine's host is the one the
    host-wide environment file declares (`HOST_ENV_FILE`, loaded by every
    unit before the watch's own file), else its configuration without the pin
    (`host` key, else the machine name). No pin, or an unreadable
    configuration: "" (a doubt blocks nothing)."""
    source = dict(os.environ if env is None else env)
    names = PINNED_ENV["AMEESH_HOST"]
    if not any(source.get(name) for name in names):
        return ""
    own = _host_env_value(source, names)
    if not own:
        bare = {key: value for key, value in source.items() if key not in names}
        try:
            own = config_mod.load(bare).host or ""
        except (Exception, SystemExit):  # noqa: BLE001 — a doubt blocks nothing
            return ""
    return own if own and own != (cfg.host or "") else ""


def _host_env_value(source: dict, names: tuple[str, ...]) -> str:
    """The first of `names` set in the host-wide environment file, or ""."""
    home = source.get("HOME") or os.path.expanduser("~")
    path = HOST_ENV_FILE.replace("%h", home)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    found: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() in names:
            found[key.strip()] = value.strip().strip('"').strip("'")
    return next((found[name] for name in names if found.get(name)), "")


def _refuse_foreign(cfg: Config, env: dict | None, what: str, regenerate: str) -> None:
    own = foreign_pin(cfg, env)
    if own:
        raise WatchError(
            "%s refused: its environment pins host %s, but this machine is %s "
            "(an environment file copied from another host, #3408); nothing sent. "
            "Regenerate it on this host: %s" % (what, cfg.host, own, regenerate))


def _agent_reading(row: dict, now: float,
                   session: dict | None = None) -> dict:
    """The stable part of an agent row for the FULL reading.

    The running/idle churn of a turn is not a change the watch must see (it
    happens at every turn); a stop, a block, a pause, a mode, an assignment or
    a project change is. #8097: the projection also derives `long_turn`,
    `budget_pause` and `session_too_big` at read time — a raw turn timestamp
    or a raw session token count would change at every turn — so the waking
    projection can use them without carrying the churn. This full projection
    feeds the passive note and the attribution; `_waking_agents` is the
    restricted one that may wake the agent."""
    from . import budget

    status = str(row.get("status") or "")
    started = row.get("turn_started_ts") or row.get("status_since_ts")
    projects = row.get("projects")
    if isinstance(projects, (list, tuple)):
        projects = sorted(str(project) for project in projects)
    else:
        projects = str(projects or "")
    running = status == "running"
    return {
        "name": str(row.get("name") or ""),
        "status": "up" if status in ("running", "idle") else status,
        "mode": str(row.get("mode") or ""),
        "operator_paused": bool(row.get("operator_paused")),
        "operator_pause_reason": str(row.get("operator_pause_reason") or ""),
        "stop_reason": str(row.get("stop_reason") or ""),
        "team": str(row.get("team") or ""),
        "workstream": str(row.get("workstream") or ""),
        "projects": projects,
        "responsible": str(row.get("responsible") or ""),
        "open_lots": int(row.get("open_lots") or 0),
        "long_turn": bool(running and started
                          and now - float(started) >= LONG_TURN_S),
        "budget_pause": bool(status == "blocked"
                             and str(row.get("status_text") or "")
                             .startswith(budget.PAUSE_PREFIX)),
        "session_too_big": bool(session),
        "session_limit": int(session.get("threshold") or 0) if session else 0,
    }


def _alerts_reading(db) -> dict:
    """The active alerts, as the alerts pass persists them (`notify_state`).

    An unreadable source is a marker, not a silence: while it stays
    unreadable the fingerprint is stable (a reading failure does not wake the
    agent), and it moves when the source becomes readable again."""
    try:
        body = storage.of(db).notify_liveness.read_state("alerts") or {}
        active = body.get("active") if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001 — a reading failure is not a change
        return {"unreadable": True}
    rows = []
    for key, entry in (active or {}).items():
        alert = entry.get("alert") if isinstance(entry, dict) else None
        alert = alert if isinstance(alert, dict) else {}
        rows.append([str(key), str(alert.get("type") or ""),
                     bool(alert.get("critical"))])
    return {"active": sorted(rows)}


def _due_reading(db, agent: str, now: float) -> dict:
    """The lots due to `agent`: actionable lots and elapsed scheduled wakes."""
    st = storage.of(db)
    actionable = st.lot_sessions.actionable(agent)
    wakes = st.lot_sessions.due_wakes(agent, now)
    return {
        "actionable": sorted([
            [int(row.get("id") or 0), str(row.get("state") or ""),
             row.get("priority")] for row in actionable]),
        "due_wakes": sorted([
            [int(row.get("id") or 0),
             round(float(row.get("wake_ts") or 0.0), 3)] for row in wakes]),
    }


def _session_sizes(db, cfg) -> dict:
    """#8097: the agents whose live session is over its rotation threshold.

    The source is the one `session_too_big` already uses (#4448): the token
    total of the agent's current session in the cost ledger
    (`turn_costs.session_total`), the per-harness threshold compiled from the
    canon (or the agent's `session_max_tokens`), the same minimum number of
    turns, the same "jamais" policy. A turn in progress is never a session to
    rotate, and the session must be the one the last turn used: that is
    exactly when the rotation decision (and the alert) can be taken. Only the
    agents over the threshold are returned — an agent's raw token count grows
    at every turn and would wake the watch at every firing; the projection
    keeps the derived `session_too_big` and its `session_limit`."""
    from . import exploitation, mechanics

    try:
        document = mechanics.configured(cfg)
    except Exception:  # noqa: BLE001 — no canon: the local common threshold
        document = None

    def threshold(harness: str) -> float:
        fallback = float(getattr(cfg, "session_max_tokens", 0) or 0)
        if document is None:
            return fallback
        return float(document.session_max_tokens(harness, fallback))

    minimum = int(getattr(cfg, "session_min_turns", 0) or 0)
    out: dict[str, dict] = {}
    for row in storage.of(db).operations.listing():
        name = str(row.get("name") or "")
        session = row.get("session_id")
        if (not name or not session or row.get("retired_ts")
                or str(row.get("last_turn_session") or "") != str(session)
                or row.get("turn_started_ts")
                or exploitation.effective_policy(cfg, row) == "jamais"):
            continue
        total = storage.of(db).turn_costs.session_total(name, session)
        if not total or int(total.get("turns") or 0) < minimum:
            continue
        harness = str(total.get("harness") or row.get("harness") or "")
        limit = (float(row["session_max_tokens"]) if row.get("session_max_tokens")
                 else threshold(harness))
        tokens = int(total.get("tokens") or 0)
        if limit > 0 and tokens >= limit:
            out[name] = {"tokens": tokens, "threshold": int(limit),
                         "turns": int(total.get("turns") or 0)}
    return out


def reading(db, agent: str, now: float | None = None,
            board: dict | None = None, sessions: dict | None = None) -> dict:
    """#5698 : la lecture propre de la veille — projets, alertes, lots dus.

    The projects part keeps only what a watch acts on: an agent's stop, block,
    pause, mode, assignment or project, and the open lots' state and assignee.
    Turn timestamps, counters, unread mail and spend are left out: they change
    at every turn, not at the events the watch must see. `board` may be given
    when the caller already read it (it also carries the recent-turn check).
    #8097: `sessions` (the result of `_session_sizes`) carries the one datum
    the board does not: the agents whose session is over its rotation
    threshold. Without it the giant-session signal is absent, never guessed.
    """
    from . import projects

    now = time.time() if now is None else float(now)
    if board is None:
        board = storage.of(db).projects.board(max_lots=projects.MAX_LOTS)
    sessions = sessions or {}
    raw_lots = list(board.get("lots") or [])
    lots = sorted([
        [lot.get("id"), str(lot.get("state") or ""),
         str(lot.get("assignee") or ""), str(lot.get("app") or "")]
        for lot in raw_lots], key=lambda row: int(row[0] or 0))
    agents = sorted([_agent_reading(row, now, sessions.get(str(row.get("name") or "")))
                     for row in board.get("agents") or []],
                    key=lambda row: row["name"])
    return {
        "projects": {
            "agents": agents,
            "lots": lots,
            "total": int(raw_lots[0].get("total") or 0) if raw_lots else 0,
        },
        "alerts": _alerts_reading(db),
        "due": _due_reading(db, agent, now),
    }


def fingerprint(state: dict) -> str:
    """The stable digest of a reading (sha256 of a canonical JSON)."""
    data = json.dumps(state, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def part_digests(state: dict) -> dict:
    """#8097: one digest per part of the full reading, for the attribution.

    `projects/agents`, `projects/lots`, `alerts` and `due` are hashed apart:
    a firing says which of them moved, instead of only that "the reading"
    changed (the 18 wakes of #8097 were nearly all ordinary lot movements)."""
    projects = state.get("projects") or {}
    return {
        "agents": fingerprint(projects.get("agents") or []),
        "lots": fingerprint(projects.get("lots") or []),
        "alerts": fingerprint(state.get("alerts") or {}),
        "due": fingerprint(state.get("due") or {}),
    }


def _waking_agents(state: dict, orchestrators=(), retired=()) -> list:
    """#8097: the agent rows that may wake the watch, and only those.

    A stop outside a deliberate gesture (`DELIBERATE_STOPS`) wakes, as does a
    block outside a budget pause, a turn running longer than `LONG_TURN_S`
    (the instruction's 40 minutes), a session over its rotation threshold (the
    instruction's "giant session"), and an orchestrator left paused or
    blocked. Everything else — an assignment, a project, a turn start/end,
    an intentional operator hold, the automatic sleep of `file_decoupage`, a
    budget pause — is left out: the instruction says to preserve those, and
    their churn is what woke the watch nearly every hour. A retired agent and
    an external session (`mode == "externe"`) are never resumed by the watch,
    so they are left out too."""
    rows = []
    for row in (state.get("projects") or {}).get("agents") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name or name in retired or str(row.get("mode") or "") == "externe":
            continue
        status = str(row.get("status") or "")
        paused = bool(row.get("operator_paused"))
        signals = []
        if status in ("stopped", "dead"):
            reason = str(row.get("stop_reason") or "")
            if reason not in DELIBERATE_STOPS:
                signals.append(["stopped", reason])
        elif status == "blocked" and not paused and not row.get("budget_pause"):
            signals.append(["blocked"])
        if name in orchestrators and (paused or status == "blocked"):
            # An orchestrator that no longer dispatches is a governance
            # failure, even when the pause is an intentional hold: veille.md
            # §2 says to resume it. (The automatic sleep never lands on one.)
            signals.append(["orchestrator-paused",
                            str(row.get("operator_pause_reason") or status)])
        if row.get("long_turn"):
            signals.append(["long-turn", "over %d min" % (LONG_TURN_S // 60)])
        if row.get("session_too_big"):
            # The limit, not the token count: the count grows at every turn
            # and would move the waking fingerprint at every firing.
            signals.append(["session-too-big",
                            str(int(row.get("session_limit") or 0))])
        for signal in signals:
            rows.append([name] + signal)
    return sorted(rows)


def _waking_alerts(state: dict) -> dict:
    """#8097: the alerts that may wake the watch.

    A new alert TYPE wakes, and so does a new critical alert instance; the
    rotation of the housekeeping alert keys (a new `tmp_orphan` path, a
    `worktree_kept` count) does not, because their type stays the same. The
    notifier already persists only its `notify.types` here (`notify_state`);
    an unreadable source stays a stable marker, as in #5698."""
    alerts = state.get("alerts") or {}
    if alerts.get("unreadable"):
        return {"unreadable": True}
    rows = alerts.get("active") or []
    return {
        "types": sorted({str(row[1]) for row in rows if len(row) > 1}),
        "critical": sorted([[str(row[0]), str(row[1])] for row in rows
                            if len(row) > 2 and row[2]]),
    }


def waking(state: dict, *, orchestrators=(), retired=()) -> dict:
    """#8097: the restricted reading that may open a turn.

    Only what the instruction asks the watch to act on: agents stopped,
    blocked, paused as orchestrators, in a too long turn or with a giant
    session; critical or new alert types; the agent's due lots. The full
    reading still records the rest (a lot passing to qa, an assignment, a key
    rotating) and sends it as a passive `--info` note, never as a waking
    instruction."""
    return {
        "agents": _waking_agents(state, orchestrators, retired),
        "alerts": _waking_alerts(state),
        "due": state.get("due") or {},
    }


def _waking_signals(wake: dict) -> dict:
    """#8097: the stable token of each waking signal, per part.

    A token exists exactly while its signal exists. The next firing compares
    these sets and opens a turn only on a NEW token: a signal that disappears
    (agent resumed, turn over, session rotated, alert resolved) calls for no
    gesture, so its resolution must not wake the watch by itself."""
    agents = sorted("agent:" + ":".join(str(field) for field in row)
                    for row in wake.get("agents") or [])
    alerts = wake.get("alerts") or {}
    if alerts.get("unreadable"):
        alert_tokens = ["alerts:unreadable"]
    else:
        alert_tokens = (["alert-type:%s" % kind
                         for kind in alerts.get("types") or []]
                        + ["alert-critical:%s:%s" % (key, kind)
                           for key, kind in alerts.get("critical") or []])
    due = wake.get("due") or {}
    due_tokens = (["due-lot:%s" % row[0] for row in due.get("actionable") or []]
                  + ["due-wake:%s" % row[0] for row in due.get("due_wakes") or []])
    return {"agents": agents, "alerts": sorted(alert_tokens),
            "due": sorted(due_tokens)}


def reading_snapshot(state: dict, *, orchestrators=(), retired=()) -> dict:
    """#8097: the document stored in `agent_watches.last_reading`.

    It keeps the full digest (the passive note), the waking digest (what may
    open a turn), the per-part digests of both and the stable tokens of the
    waking signals (`_waking_signals`, the comparison `waking_moved` makes).
    Compact on purpose: the column held a bare sha256 before, and a full board
    snapshot would be rewritten at every firing."""
    wake = waking(state, orchestrators=orchestrators, retired=retired)
    return {
        "v": READING_VERSION,
        "digest": fingerprint(state),
        "wake": fingerprint(wake),
        "parts": part_digests(state),
        "waking": {part: fingerprint(wake[part]) for part in WAKING_PARTS},
        "signals": _waking_signals(wake),
    }


def previous_reading(raw) -> dict | None:
    """#8097: decode a stored `last_reading`; None when there is none.

    A bare sha256 (the #5698 format) is returned as `{"legacy": ...}`: it
    carries no partition, so the first firing after the upgrade reads it as a
    reading without precedent and sends one instruction (`waking_moved`)."""
    if raw is None or raw == "":
        return None
    text = str(raw)
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"legacy": text}
    if not isinstance(value, dict):
        return {"legacy": text}
    try:
        version = int(value.get("v") or 0)
    except (TypeError, ValueError):
        return {"legacy": text}
    if version < READING_VERSION:
        return {"legacy": text}
    return value


def changed_parts(previous: dict | None, current: dict) -> list:
    """#8097: which part of the FULL reading moved, for the firing's line."""
    if previous is None or "legacy" in previous:
        return list(READING_PARTS)
    old = previous.get("parts") or {}
    return [part for part in READING_PARTS
            if old.get(part) != (current.get("parts") or {}).get(part)]


def waking_moved(previous: dict | None, current: dict) -> tuple:
    """#8097: did a waking part gain a signal, and which ones.

    Only a NEW signal opens a turn: the comparison is between the stable
    signal tokens (`_waking_signals`), never their mere presence or absence,
    so a resolution (a resumed agent, a finished turn, a rotated session, a
    resolved alert) does not wake the watch by itself. A reading without
    precedent (first firing, the bare digest of the previous format, or a
    document written before the tokens existed) counts as a waking change: a
    signal is never missed on the strength of a format the code cannot
    compare."""
    if previous is None or "legacy" in previous:
        return True, list(WAKING_PARTS)
    old = previous.get("signals")
    if not isinstance(old, dict):
        return True, list(WAKING_PARTS)
    current_signals = current.get("signals") or {}
    moved = [part for part in WAKING_PARTS
             if set(current_signals.get(part) or ())
             - set(old.get(part) or ())]
    return bool(moved), moved


def _governance_rows(db) -> tuple:
    """#8097: the orchestrators and the retired agents, read once per firing.

    The registry carries the canon-declared `roles` (the `orchestrateur` role)
    and the retirement mark; the board does not. A read failure is not fatal:
    the caller passes no orchestrator and no retired mark, so the watch still
    sees stops, blocks, long turns, alerts and due lots."""
    from .roles import ORCHESTRATOR_ROLES

    wanted = set(ORCHESTRATOR_ROLES)
    orchestrators, retired = set(), set()
    for row in storage.of(db).agents.overview():
        name = str(row.get("name") or "")
        if not name:
            continue
        if row.get("retired_ts"):
            retired.add(name)
        elif wanted & set(row.get("roles") or ()):
            orchestrators.add(name)
    return frozenset(orchestrators), frozenset(retired)


def _moved_text(changed, moved) -> str:
    """#8097: the attribution of one firing, for its line and its thread."""
    if changed is None or moved is None:
        return "(reading unreadable)"
    return "(changed: %s; waking: %s)" % (
        ", ".join(changed) or "none", ", ".join(moved) or "none")



def _unread_note(db, agent: str) -> bool:
    """A `veille` note is already waiting in `agent`'s mailbox (#5698)."""
    try:
        rows = storage.of(db).mailbox.unread(agent, 200)
    except Exception:  # noqa: BLE001 — a doubt keeps the note from stacking
        return True
    return any(row.get("sender") == SENDER for row in rows)


def _recent_turn(board: dict | None, agent: str, now: float) -> bool:
    """Has `agent` had a turn (or a state change) within `RECENT_TURN_S`?"""
    if not board:
        return False
    row = next((row for row in board.get("agents") or []
                if row.get("name") == agent), None)
    if row is None:
        return False
    if str(row.get("status") or "") == "running":
        return True
    last = max(float(row.get("last_turn_ts") or 0.0),
               float(row.get("status_since_ts") or 0.0))
    return last > 0 and now - last < RECENT_TURN_S


def _beat(db, agent: str, host: str, reason: str, *, takeover: bool = False,
          epoch: int | None = None, attempt: bool = False,
          reading: str | None = None) -> dict | None:
    """#7933 (B1) : enregistre un tir qui ne dépose rien, sous le même fencing
    que le message. Une erreur échoue le tir (la minuterie réessaiera), elle
    ne doit jamais être avalée en silence : le battement est ce qui distingue
    une veille saine d'une veille morte.

    #8097 : `reading` enregistre l'observation du tir quand elle ne porte
    aucun signal nouveau (une résolution, une lecture inchangée). Sans elle,
    un signal résolu resterait dans la lecture précédente et sa réapparition
    passerait pour un signal déjà connu."""
    try:
        return storage.of(db).watches.deliver(
            agent, host, None, takeover=takeover, epoch=epoch,
            fired=reason, attempt=attempt, reading=reading)
    except Exception as exc:  # noqa: BLE001 — un battement non écrit est un échec
        raise WatchError("recording the firing of %s failed: %s"
                         % (agent, exc)) from exc


def send(cfg: Config, db, agent: str, *, mail_send: Callable | None = None,
         env: dict | None = None, now: float | None = None) -> str:
    """One firing: the instruction to `agent` when this host may emit its
    watch; else nothing (disabled, moved, or a role it does not hold).

    #5698 : the firing first does its own reading (projects, alerts, due lots)
    and fingerprints it. #8097 : the full reading only earns the short passive
    (`--info`) note (nothing while its last note is unread, nothing when the
    agent had a turn within `RECENT_TURN_S`); only the waking reading — stops
    outside a deliberate gesture, blocks outside a budget pause, a turn over
    40 minutes, a session over its rotation threshold, a paused orchestrator,
    the agent's due lots, a critical or new alert type — sends the instruction
    and stores the reading on the watch row. A waking change seen right after
    a turn is kept pending (nothing sent until a firing whose turn is older). A reading that cannot be made never
    mutes the watch: the instruction is sent. Every firing's line names the
    parts that moved (`changed`) and those that could wake (`waking`), the
    measurement #8097 asks for.

    The host check, the message, `last_sent_ts` and the reading are one
    transaction on the watch row (codex r1 and r2, #303): a move or a disable
    waits for a firing in progress, a firing after them sends nothing, and a
    firing that fails drops neither the message nor the reading, so that
    the next one never sends a second copy.
    `mail_send(tx, agent, text)` drops the message in the transaction (tests).
    What happened, in words.

    #7933 : for an agent whose card carries an **elected role**, the host
    recorded on the watch row is not authoritative. Only the host that holds
    the live role lease emits; it takes the row over in the delivery
    transaction, fenced by the lease epoch, so a handover needs no
    re-registration and two hosts never emit for one epoch. A candidate that
    does not hold the lease stands by."""
    from . import mail, projects, undeliverable
    _refuse_foreign(cfg, env, "watch of %s" % agent,
                    "ameesh watch enable --agent %s" % agent)
    host = cfg.host or ""
    now = time.time() if now is None else float(now)
    rows = storage.of(db).watches.list(agent)
    watch = rows[0] if rows else None
    role = elected_role(db, agent)
    epoch: int | None = None
    takeover = False
    if role is not None:
        epoch = _role_epoch(agent, role)
        if not role.get("held"):
            return ("the watch of %s follows the elected role %s: no live lease "
                    "(owner %s) — nothing sent"
                    % (agent, role.get("role") or agent, role.get("owner") or "none"))
        if role.get("host") != host:
            return ("the watch of %s follows the elected role %s, held by %s "
                    "(epoch %s): nothing sent from %s"
                    % (agent, role.get("role") or agent, role.get("host") or "?",
                       role.get("epoch"), host))
        takeover = bool(watch is not None and watch["host"] != host)
    elif watch is None:
        return "no watch of %s: nothing sent" % agent
    elif watch["host"] != host:
        return "the watch of %s is on %s: nothing sent from here" % (agent, watch["host"])
    if watch is None:
        return "no watch of %s: nothing sent" % agent
    # #7933 (ameesh-cx2) : l'unité d'un rôle élu tique toutes les 60 s pour
    # voir une relève ; la veille elle-même ne relit qu'une fois `every_s`
    # écoulé. Le battement « pas encore dû » entretient la vivacité sans
    # avancer `last_attempt_at`, sinon l'échéance reculerait à chaque tick.
    if role is not None and not takeover:
        last_attempt = watch.get("last_attempt_ts")
        every = float(watch.get("every_s") or DEFAULT_EVERY_S)
        if last_attempt is not None and now < float(last_attempt) + every:
            _beat(db, agent, host, "not-due", takeover=takeover, epoch=epoch)
            due_in = max(0, int(float(last_attempt) + every - now))
            return ("watch of %s: next instruction in %ds: nothing sent"
                    % (agent, due_in))
    # the project of the thread, read BEFORE the delivery (codex #303 r3): once
    # the message is committed, nothing may fail the firing (the timer would
    # retry, and send it twice)
    agent_row = registry.get(db, agent)
    current: dict | None = None
    document: str | None = None
    digest: str | None = None
    changed: list | None = []
    moved: list | None = []
    recent = False
    passive = False
    # a takeover is a first firing on this host: its reading has no local
    # precedent, so the instruction goes (the same epoch never sends twice).
    previous = (previous_reading(watch.get("last_reading"))
                if watch["host"] == host else None)
    # the checks of `agent-mail send`, before the transaction (read only)
    try:
        undeliverable.check_send(cfg, db, sender=SENDER, dest=agent, explicit=True)
    except undeliverable.Refused as exc:
        raise WatchError("instruction to %s not sent: %s" % (agent, exc)) from exc
    # #8097 : the canon-declared orchestrators and the retired agents, needed
    # by the waking projection; a read failure does not mute the watch.
    try:
        orchestrators, retired = _governance_rows(db)
    except Exception as exc:  # noqa: BLE001 — a doubt must not mute the watch
        print("warning: watch roles of %s unreadable: %s: %s"
              % (agent, type(exc).__name__, exc), file=sys.stderr)
        orchestrators, retired = frozenset(), frozenset()
    # #8097 : la taille des sessions (grand livre, seuil de rotation #4448),
    # absente du board ; une lecture impossible ne fait pas taire la veille.
    try:
        sessions = _session_sizes(db, cfg)
    except Exception as exc:  # noqa: BLE001 — a doubt must not mute the watch
        print("warning: watch session sizes of %s unreadable: %s: %s"
              % (agent, type(exc).__name__, exc), file=sys.stderr)
        sessions = {}
    try:
        board = storage.of(db).projects.board(max_lots=projects.MAX_LOTS)
        state = reading(db, agent, now, board=board, sessions=sessions)
        current = reading_snapshot(state, orchestrators=orchestrators,
                                   retired=retired)
        document = json.dumps(current, sort_keys=True, separators=(",", ":"))
        digest = current["digest"]
        changed = changed_parts(previous, current)
        waking_change, moved = waking_moved(previous, current)
        # a first firing always sends: the watch just got enabled and has
        # no reading yet, even if the agent had a turn a minute ago.
        recent = previous is not None and _recent_turn(board, agent, now)
    except Exception as exc:  # noqa: BLE001 — a reading failure never mutes
        print("warning: watch reading of %s unreadable: %s: %s"
              % (agent, type(exc).__name__, exc), file=sys.stderr)
        current, document, digest, changed, moved = None, None, None, None, None
        waking_change, recent = True, False
    if not waking_change:
        if recent:
            _beat(db, agent, host, "recent-turn", takeover=takeover,
                  epoch=epoch, attempt=True, reading=document)
            return ("watch of %s: no action %s, a turn within %d min: nothing sent"
                    % (agent, _moved_text(changed, moved), RECENT_TURN_S // 60))
        if _unread_note(db, agent):
            _beat(db, agent, host, "unread-note", takeover=takeover,
                  epoch=epoch, attempt=True, reading=document)
            return ("watch of %s: no action %s, its last note is not read yet: "
                    "nothing sent" % (agent, _moved_text(changed, moved)))
        passive = True
    elif recent:
        # B/P2 cx3 : never consume a change on the strength of a recent
        # turn. The change may be external or have happened after the
        # agent's last turn; it stays pending, and the first firing whose
        # turn is older sends the instruction.
        _beat(db, agent, host, "change-pending", takeover=takeover,
              epoch=epoch, attempt=True)
        return ("watch of %s: change kept pending %s, a turn within %d min: "
                "nothing sent" % (agent, _moved_text(changed, moved),
                                  RECENT_TURN_S // 60))
    if passive:
        text = UNCHANGED_TEXT % agent
    else:
        from . import release_cutoff
        text = instruction(agent, release_cutoff.staging_topology_of(cfg))
    payload = {"info": True} if passive else None
    report_fp = mail.report_digest(text) if passive else None
    if passive:
        # #7625 : le compte rendu « aucune action » est identique à chaque
        # relève. Il n'est pas redéposé avant `mail.REPORT_REMIND_S` (6 h) ;
        # la veille elle-même n'est pas muette pour autant, un mouvement
        # ordinaire part toujours (la note passive ci-dessous).
        if not mail.report_due(db, SENDER, agent, "info", report_fp, now=now):
            _beat(db, agent, host, "report-reminder", takeover=takeover,
                  epoch=epoch, attempt=True, reading=document)
            return ("watch of %s: no action %s, the same note was sent less than "
                    "%d h ago: nothing sent"
                    % (agent, _moved_text(changed, moved),
                       int(mail.REPORT_REMIND_S // 3600)))
    if mail_send is None:
        def mail_send(tx, recipient: str, body: str) -> int | None:
            return mail.send(tx, SENDER, recipient, body, host=host,
                             thread=False, payload=payload,
                             report="info" if passive else None,
                             report_fingerprint=report_fp)
    sent: list[int] = []

    def deliver(tx) -> None:
        message_id = mail_send(tx, agent, text)
        if message_id is None:
            # #7625 : un autre envoi a déposé le même compte rendu entre le
            # contrôle et le dépôt ; la transaction est annulée, rien n'est
            # marqué remis et la relève suivante réévalue.
            raise _ReportJustSent()
        sent.append(message_id)
    try:
        row = storage.of(db).watches.deliver(
            agent, host, deliver, reading=document,
            takeover=takeover, epoch=epoch)
    except _ReportJustSent:
        _beat(db, agent, host, "report-reminder", takeover=takeover,
              epoch=epoch, attempt=True, reading=document)
        return ("watch of %s: the same report was just sent: nothing sent" % agent)
    except WatchError:
        raise
    except Exception as exc:  # noqa: BLE001 — the unit fails, nothing was dropped
        raise WatchError("sending the instruction to %s failed: %s" % (agent, exc)) from exc
    if row is None:
        return "no watch of %s: nothing sent (ameesh watch disable --agent %s removes " \
               "this timer)" % (agent, agent)
    if not row["sent"]:
        if row.get("refused") == "stale-lease":
            return ("the watch of %s: the role lease moved (epoch %s): nothing sent"
                    % (agent, epoch))
        return "the watch of %s is on %s: nothing sent from here" % (agent, row["host"])
    # committed: the readable thread, as `agent-mail send` writes it
    # (fil.record never raises). #8097: the `watch` line carries the parts
    # that moved and the waking ones, so a 24 h count reads the thread.
    fil.record(cfg, db, sender=SENDER, recipients=[agent], text=text,
               project=fil.agent_project(agent_row) if agent_row else None,
               mailbox_ids=[int(m) for m in sent if isinstance(m, int)],
               meta={"host": host, READING_KEY: {
                   "digest": digest,
                   "wake_digest": current["wake"] if current else None,
                   "changed": bool(changed) if changed is not None else None,
                   "changed_parts": changed,
                   "waking": moved,
                   "passive": passive, "recent_turn": bool(recent)}})
    if passive:
        return ("watch of %s: no action %s, an information was deposited "
                "(read at its next turn)" % (agent, _moved_text(changed, moved)))
    return "instruction sent to %s %s" % (agent, _moved_text(changed, moved))


def arm(cfg: Config, db, agent: str, slug: str, delay_s: int, message: str, *,
        run: Callable = subprocess.run, env: dict | None = None,
        actor: str | None = None, deadline: float | None = None) -> str:
    """Arms (or re-arms) the one-shot wake `<slug>` of `agent` on this host.

    The unit declares the identity and applies it with `env` on its exec line
    (an environment file can override `Environment=`), and the witness reads
    both back from the installed unit: a wake whose unit does not carry
    `AGENT_MAIL_NAME` (or whose timer did not start) is refused and removed,
    never left to lose its message at the deadline (#1020, the seven muted
    watches)."""
    if not platform.is_linux():
        raise WatchError("a wake needs systemd (Linux)")
    _refuse_retired(registry.get(db, agent), agent)
    if not WAKE_RE.match(slug or ""):
        raise WatchError("--wake: %r (lowercase letters, digits and dashes)" % slug)
    message = (message or "").strip()
    if not message:
        raise WatchError("--message: empty")
    host = cfg.host or ""
    if not host:
        raise WatchError("this host has no name (host of the configuration)")
    actor = actor or authorize(cfg, db, agent)
    armed_ts = time.time()
    if deadline is None:
        deadline = armed_ts + delay_s
    if deadline <= armed_ts:
        raise WatchError("wake deadline elapsed before arming: choose a future --at")
    directory = unit_dir(env)
    os.makedirs(directory, exist_ok=True)
    prune_legacy_wakes(directory, run)
    source = os.environ if env is None else env
    files = wake_units(agent, slug, delay_s, _ameesh_binary(source), directory,
                       deadline=deadline)
    files[wake_env_name(agent, slug)] = pinned_env(dict(source), host)
    files[wake_name(agent, slug, "wake.json")] = json.dumps(
        {"agent": agent, "slug": slug, "message": message, "host": host,
         "armed_ts": armed_ts, "deadline_ts": deadline},
        ensure_ascii=False, sort_keys=True) + "\n"
    _systemctl_quiet(run, "stop", wake_name(agent, slug, "timer"))
    for name, text in files.items():
        tmp = os.path.join(directory, "." + name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, os.path.join(directory, name))
    _systemctl(run, "daemon-reload")
    _systemctl(run, "start", wake_name(agent, slug, "timer"))
    # the witness: read the installed unit back; without the identity the
    # firing fails rc=2 at the deadline and the wake is lost silently.
    try:
        carried = _systemctl_show(run, WITNESS_PROPERTY,
                                  wake_name(agent, slug, "service"))
        applied = _systemctl_show(run, WITNESS_EXEC_PROPERTY,
                                  wake_name(agent, slug, "service"))
        state = _systemctl_show(run, "ActiveState",
                                wake_name(agent, slug, "timer")).strip()
        sub = _systemctl_show(run, "SubState", wake_name(agent, slug, "timer")).strip()
    except WatchError as exc:
        _disarm(directory, agent, slug, run)
        raise WatchError("wake %s of %s not armed: witness unreadable (%s)"
                         % (slug, agent, exc)) from exc
    wanted = "%s=%s" % (IDENTITY_ENV, agent)
    if wanted not in _environment_tokens(carried):
        _disarm(directory, agent, slug, run)
        raise WatchError(
            "wake %s of %s not armed: the unit does not carry %s (read back: %r); "
            "a wake without identity loses its message at the deadline (#1020)"
            % (slug, agent, wanted, carried.strip()))
    if wanted not in (applied or ""):
        # EnvironmentFile wins over Environment= in systemd: only the exec
        # line, read back here, proves the identity the child will carry.
        _disarm(directory, agent, slug, run)
        raise WatchError(
            "wake %s of %s not armed: its exec line does not apply %s "
            "(read back: %r); an environment file may override Environment=, and "
            "a wake without identity loses its message at the deadline (#1020)"
            % (slug, agent, wanted, (applied or "").strip()))
    if sub == "elapsed":
        # #1737: an OnCalendar= deadline passed before `start` leaves the
        # timer active/elapsed; it never fires and the wake would be lost.
        _disarm(directory, agent, slug, run)
        raise WatchError("wake %s of %s not armed: its deadline passed before the "
                         "timer started; choose a later --at" % (slug, agent))
    if state != "active":
        _disarm(directory, agent, slug, run)
        raise WatchError("wake %s of %s not armed: its timer is %r after start"
                         % (slug, agent, state or "unknown"))
    return ("wake %s of %s armed in %ds on %s by %s (unit %s, %s pinned)"
            % (slug, agent, delay_s, host, actor,
               wake_name(agent, slug, "timer"), wanted))


def disarm(cfg: Config, db, agent: str, slug: str, *,
           run: Callable = subprocess.run, env: dict | None = None,
           actor: str | None = None) -> str:
    """Removes an armed wake of `agent` (its own, or by an orchestrator or a
    human, the rule of `watch disable`)."""
    if not WAKE_RE.match(slug or ""):
        raise WatchError("--wake: %r (lowercase letters, digits and dashes)" % slug)
    if registry.get(db, agent) is None:
        raise WatchError("unknown agent: %s" % agent)
    actor = actor or authorize(cfg, db, agent)
    directory = unit_dir(env)
    if not os.path.exists(os.path.join(directory, wake_name(agent, slug, "wake.json"))):
        raise WatchError("no wake %s of %s" % (slug, agent))
    _disarm(directory, agent, slug, run)
    return "wake %s of %s cancelled by %s" % (slug, agent, actor)


def fire(cfg: Config, db, agent: str, slug: str, *,
         run: Callable = subprocess.run, env: dict | None = None) -> str:
    """One firing of the wake `<slug>`: sends `agent` its own message, then
    removes the wake. Refuses without the identity its unit must carry
    (`AGENT_MAIL_NAME=<agent>`, #1020): a wake unit armed by hand without it
    is stopped here, with a readable error, instead of losing its message."""
    if not WAKE_RE.match(slug or ""):
        raise WatchError("--wake: %r (lowercase letters, digits and dashes)" % slug)
    source = os.environ if env is None else env
    identity = (source.get(IDENTITY_ENV) or "").strip()
    if identity != agent:
        raise WatchError(
            "wake %s of %s refused: %s=%r — a wake unit carries %s=%s "
            "(ameesh watch at); without it the message is lost at the deadline (#1020)"
            % (slug, agent, IDENTITY_ENV, identity, IDENTITY_ENV, agent))
    _refuse_foreign(cfg, env, "wake %s of %s" % (slug, agent),
                    "ameesh watch at --agent %s --wake %s --in <delay> --message <text>"
                    % (agent, slug))
    directory = unit_dir(env)
    state_path = os.path.join(directory, wake_name(agent, slug, "wake.json"))
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        # #4755: no state, nothing to send — and a failure would restart the
        # unit every 2 min forever (`Restart=on-failure`): the wake disarms
        # itself, under either prefix, and the firing succeeds.
        legacy = _disarm_legacy(directory, agent, slug, run)
        _disarm(directory, agent, slug, run)
        return ("wake %s of %s: no state left (%s), nothing sent; its units are removed"
                % (slug, agent, "armed before #1039" if legacy else "already fired or cancelled"))
    except (OSError, ValueError) as exc:
        raise WatchError("wake %s of %s: no readable wake (%s)"
                         % (slug, agent, exc)) from exc
    if state.get("agent") != agent or state.get("slug") != slug:
        raise WatchError("wake %s of %s: the state file names %s/%s"
                         % (slug, agent, state.get("agent"), state.get("slug")))
    message = str(state.get("message") or "").strip()
    if not message:
        raise WatchError("wake %s of %s: empty message" % (slug, agent))
    # read before the delivery: once the message is committed, nothing may
    # fail the firing (the unit would retry and send it twice)
    row = registry.get(db, agent)
    # #3460 : un réveil armé avant le retrait ne dépose rien chez un retiré ;
    # l'unité échoue et garde son état lisible.
    _refuse_retired(row, agent)
    from . import mail
    host = cfg.host or ""
    with db.transaction() as tx:
        mailbox_id = mail.send(tx, agent, agent, message, host=host, thread=False)
    fil.record(cfg, db, sender=agent, recipients=[agent], text=message,
               project=fil.agent_project(row) if row else None,
               mailbox_ids=[mailbox_id],
               meta={"action": "wake", "wake": slug, "host": host, "audit": "watch"})
    _disarm(directory, agent, slug, run)
    return "wake %s fired: message sent to %s" % (slug, agent)


def show(db, agent: str | None = None) -> list[dict]:
    """The watch rows, with the live role report of an elected role (#7933).

    `host` keeps its meaning: the host a plain watch is recorded for, or the
    last host that emitted an elected role's watch. `elected` says who holds
    the lease *now*, so `watch show` never presents a stale host as the
    emitter of a role watch."""
    out = []
    for row in storage.of(db).watches.list(agent):
        item = dict(row, every=every_text(int(row["every_s"])))
        role = elected_role(db, row["agent"])
        if role is not None:
            item["elected"] = {
                "role": role.get("role") or row["agent"],
                "held": bool(role.get("held")),
                "holder": role.get("host") if role.get("held") else None,
                "epoch": role.get("epoch"),
            }
        out.append(item)
    return out


def format_show(rows: list[dict], now: float) -> list[str]:
    from .notify import duration
    if not rows:
        return ["no watch"]
    lines = []
    for r in rows:
        last = ("last sent %s ago" % duration(now - r["last_sent_ts"])
                if r.get("last_sent_ts") else "never sent")
        where = "on %s" % r["host"]
        elected = r.get("elected") or {}
        if elected:
            if elected.get("holder"):
                where += (" (elected role %s: lease holder %s, epoch %s)"
                          % (elected.get("role"), elected["holder"], elected.get("epoch")))
            else:
                where += (" (elected role %s: no live lease)"
                          % elected.get("role"))
        lines.append("%s: every %s %s, enabled by %s %s ago, %s" % (
            r["agent"], r["every"], where, r["enabled_by"],
            duration(now - r["enabled_ts"]), last))
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh watch",
                                     description="the watch of an agent (L121)")
    sub = parser.add_subparsers(dest="command", required=True)
    on = sub.add_parser("enable", help="install the timer on this host")
    on.add_argument("--agent", required=True)
    on.add_argument("--every", default=every_text(DEFAULT_EVERY_S),
                    help="period: 30m, 2h… (default 30m)")
    off = sub.add_parser("disable", help="remove the timer and the watch")
    off.add_argument("--agent", required=True)
    look = sub.add_parser("show", help="the watches of the mesh")
    look.add_argument("--agent")
    look.add_argument("--json", action="store_true")
    fire = sub.add_parser("send", help="one firing (what the timer runs)")
    fire.add_argument("--agent", required=True)
    wake = sub.add_parser("at", help="arm a one-shot wake (identity pinned on the unit)")
    wake.add_argument("--agent", required=True)
    wake.add_argument("--in", dest="in_", metavar="DURATION",
                      help="delay: 45m, 2h… (one of --in or --at)")
    wake.add_argument("--at", metavar="TIME",
                      help="absolute time, ISO 8601 with timezone (2026-10-13T20:00:00Z)")
    wake.add_argument("--message", required=True, help="what the wake sends its agent")
    wake.add_argument("--wake", default="wake", metavar="SLUG",
                      help="name of the wake (default: wake)")
    off_wake = sub.add_parser("cancel", help="remove an armed wake")
    off_wake.add_argument("--agent", required=True)
    off_wake.add_argument("--wake", required=True, metavar="SLUG")
    run_wake = sub.add_parser("fire", help="one firing of a wake (what its unit runs)")
    run_wake.add_argument("--agent", required=True)
    run_wake.add_argument("--wake", required=True, metavar="SLUG")
    return parser


def main(argv: list[str] | None = None) -> int:
    from .platform.child_environment import managed_entry
    if not managed_entry():
        return 1
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if getattr(args, "agent", None) and not NAME_RE.match(args.agent):
        print("error: invalid agent name: %r" % args.agent, file=sys.stderr)
        return 2
    cfg = config_mod.load()
    try:
        every = parse_every(args.every) if args.command == "enable" else None
        now = time.time()
        deadline = parse_wake_deadline(args.in_, args.at, now=now) \
            if args.command == "at" else None
        delay = int(round(deadline - now)) if deadline is not None else None
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db)
            if args.command == "enable":
                for line in enable(cfg, db, args.agent, every):
                    print("warning: " + line, file=sys.stderr)
                print("watch of %s: every %s on %s" % (args.agent, every_text(every), cfg.host))
            elif args.command == "disable":
                for line in disable(cfg, db, args.agent):
                    print("warning: " + line, file=sys.stderr)
                print("watch of %s disabled" % args.agent)
            elif args.command == "send":
                print(send(cfg, db, args.agent))
            elif args.command == "at":
                print(arm(cfg, db, args.agent, args.wake, delay, args.message,
                          deadline=deadline))
            elif args.command == "cancel":
                print(disarm(cfg, db, args.agent, args.wake))
            elif args.command == "fire":
                print(fire(cfg, db, args.agent, args.wake))
            else:
                rows = show(db, args.agent)
                if args.json:
                    print(json.dumps({"schema": "ameesh-watches/1", "watches": rows},
                                     ensure_ascii=False, indent=2, sort_keys=True))
                else:
                    print("\n".join(format_show(rows, time.time())))
        finally:
            db.close()
    except WatchError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.SchemaMissing, db_mod.Unavailable, db_mod.DbError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
