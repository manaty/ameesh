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
`ameesh watch send` every period: it sends the agent the instruction
shipped with the package (`consignes/veille.md`, versioned), from `veille`.
The watch is recorded in the database (`agent_watches`, 0362): `ameesh
projects` shows it from any host, and enabling or disabling it is noted in
the agent's thread. A timer whose watch was disabled, or moved to another
host, sends nothing.

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


def instruction(agent: str) -> str:
    """The instruction sent to `agent`, from the versioned text."""
    with open(CONSIGNE_PATH, encoding="utf-8") as fh:
        return string.Template(fh.read()).safe_substitute(agent=agent).strip()


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


def units(agent: str, every_s: int, ameesh: str, path: str,
          directory: str = "") -> dict[str, str]:
    """The service and the timer of the watch of `agent` (`directory`: where
    its environment file is written)."""
    service = (
        "[Unit]\n"
        "Description=ameesh watch of %(agent)s (L121, written by ameesh watch enable)\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=%(timeout)d\n"
        "Environment=\"PATH=%(path)s\"\n"
        "EnvironmentFile=-%(host_env)s\n"
        "EnvironmentFile=-%(own_env)s\n"
        "ExecStart=\"%(ameesh)s\" watch send --agent %(agent)s\n"
        % {"agent": agent, "ameesh": ameesh, "path": path, "host_env": HOST_ENV_FILE,
           "own_env": os.path.join(directory, env_name(agent)),
           "timeout": FIRING_TIMEOUT_S})
    timer = (
        "[Unit]\n"
        "Description=ameesh watch of %(agent)s, every %(every)s\n"
        "[Timer]\n"
        "OnActiveSec=%(s)d\n"
        "OnUnitActiveSec=%(s)d\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
        % {"agent": agent, "every": every_text(every_s), "s": every_s})
    return {unit_name(agent, "service"): service, unit_name(agent, "timer"): timer}


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


def wake_units(agent: str, slug: str, delay_s: int, ameesh: str, path: str,
               directory: str = "", *, deadline: float | None = None) -> dict[str, str]:
    """The service and the timer of the one-shot wake `<slug>` of `agent`.

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
        % {"agent": agent, "slug": slug, "ameesh": ameesh, "path": path,
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


def _ameesh_binary() -> str:
    found = shutil.which("ameesh")
    if found:
        return stable_binary(os.path.abspath(found))
    return os.path.join(os.path.expanduser("~"), ".local", "bin", "ameesh")


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
    systemd 255), so the identity must be set on the command itself."""
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
    before = storage.of(db).watches.list(agent)
    if before and before[0]["host"] != host:
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
    path = source.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    files = units(agent, every_s, _ameesh_binary(), path, directory)
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
          "Watch of %s enabled on %s by %s: every %s, instruction consignes/veille.md."
          % (agent, host, actor, every_text(every_s)),
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


def send(cfg: Config, db, agent: str, *, mail_send: Callable | None = None,
         env: dict | None = None) -> str:
    """One firing: the instruction to `agent` when its watch is recorded for
    this host; else nothing (disabled, or moved). The host check, the message
    and `last_sent_ts` are one transaction on the watch row (codex r1 and r2,
    #303): a move or a disable waits for a firing in progress, a firing after
    them sends nothing, and a firing that fails drops no message, so that the
    next one never sends a second copy. `mail_send(tx, agent, text)` drops the
    message in the transaction (tests). What happened, in words."""
    from . import mail, undeliverable
    _refuse_foreign(cfg, env, "watch of %s" % agent,
                    "ameesh watch enable --agent %s" % agent)
    host = cfg.host or ""
    text = instruction(agent)
    rows = storage.of(db).watches.list(agent)
    # the project of the thread, read BEFORE the delivery (codex #303 r3): once
    # the message is committed, nothing may fail the firing (the timer would
    # retry, and send it twice)
    agent_row = registry.get(db, agent)
    if rows and rows[0]["host"] == host:
        # the checks of `agent-mail send`, before the transaction (read only)
        try:
            undeliverable.check_send(cfg, db, sender=SENDER, dest=agent, explicit=True)
        except undeliverable.Refused as exc:
            raise WatchError("instruction to %s not sent: %s" % (agent, exc)) from exc
    if mail_send is None:
        def mail_send(tx, recipient: str, body: str) -> int:
            return mail.send(tx, SENDER, recipient, body, host=host, thread=False)
    sent: list[int] = []

    def deliver(tx) -> None:
        sent.append(mail_send(tx, agent, text))
    try:
        row = storage.of(db).watches.deliver(agent, host, deliver)
    except WatchError:
        raise
    except Exception as exc:  # noqa: BLE001 — the unit fails, nothing was dropped
        raise WatchError("sending the instruction to %s failed: %s" % (agent, exc)) from exc
    if row is None:
        return "no watch of %s: nothing sent (ameesh watch disable --agent %s removes " \
               "this timer)" % (agent, agent)
    if not row["sent"]:
        return "the watch of %s is on %s: nothing sent from here" % (agent, row["host"])
    # committed: the readable thread, as `agent-mail send` writes it
    # (fil.record never raises)
    fil.record(cfg, db, sender=SENDER, recipients=[agent], text=text,
               project=fil.agent_project(agent_row) if agent_row else None,
               mailbox_ids=[int(m) for m in sent if isinstance(m, int)],
               meta={"host": host})
    return "instruction sent to %s" % agent


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
    path = source.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    files = wake_units(agent, slug, delay_s, _ameesh_binary(), path, directory,
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
    return [dict(row, every=every_text(int(row["every_s"])))
            for row in storage.of(db).watches.list(agent)]


def format_show(rows: list[dict], now: float) -> list[str]:
    from .notify import duration
    if not rows:
        return ["no watch"]
    lines = []
    for r in rows:
        last = ("last sent %s ago" % duration(now - r["last_sent_ts"])
                if r.get("last_sent_ts") else "never sent")
        lines.append("%s: every %s on %s, enabled by %s %s ago, %s" % (
            r["agent"], r["every"], r["host"], r["enabled_by"],
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
    from .platform.child_environment import harden
    if not harden():
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
