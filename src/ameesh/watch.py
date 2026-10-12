# SPDX-License-Identifier: AGPL-3.0-only
"""L121: the watch of an agent, managed by ameesh.

  ameesh watch enable --agent A [--every 30m]   installs the timer on this host
  ameesh watch disable --agent A                removes it
  ameesh watch show [--agent A] [--json]        the watches of the mesh
  ameesh watch send --agent A                   one firing (what the timer runs)

A watch is a systemd user timer (`ameesh-watch-<agent>.timer`) that runs
`ameesh watch send` every period: it sends the agent the instruction
shipped with the package (`consignes/veille.md`, versioned), from `veille`.
The watch is recorded in the database (`agent_watches`, 0362): `ameesh
projects` shows it from any host, and enabling or disabling it is noted in
the agent's thread. A timer whose watch was disabled, or moved to another
host, sends nothing.
"""
from __future__ import annotations

import argparse
import json
import os
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
#: the hand-written units L121 replaces (`ameesh-veille-<agent>.timer`)
LEGACY_PREFIX = "ameesh-veille-"
#: the directory of the user units (tests: a temporary one)
UNIT_DIR_ENV = "AMEESH_SYSTEMD_USER_DIR"
#: the configuration of the timer's commands: ameesh's environment file of the
#: host (the runner's convention), then the variables pinned at enable, which win
HOST_ENV_FILE = "%h/.config/ameesh/env"
#: variables that choose the database, the host and the configuration: those
#: set when the watch is enabled are pinned in its environment file (codex r1,
#: #303: a timer firing with the defaults reads another database, or skips
#: delivery because the host differs)
PINNED_ENV = tuple(prefix + name for name in (
    "CONFIG", "DSN", "DATABASE_URL", "SCHEMA", "DRIVER", "BACKEND", "HOST", "STATE",
    "THREADS") for prefix in ("AMEESH_", "AGENT_MESH_"))
CONSIGNE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "consignes", "veille.md")


class WatchError(Exception):
    """A refused or failed watch command (exit 1)."""


def consigne(agent: str) -> str:
    """The instruction sent to `agent`, from the versioned text."""
    with open(CONSIGNE_PATH, encoding="utf-8") as fh:
        return string.Template(fh.read()).safe_substitute(agent=agent).strip()


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


def pinned_env(env: dict) -> str:
    """The environment file of a watch: the `PINNED_ENV` variables set in
    `env`, in systemd's quoting. A value on several lines is refused."""
    lines = []
    for name in PINNED_ENV:
        value = env.get(name)
        if not value:
            continue
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
        "Environment=\"PATH=%(path)s\"\n"
        "EnvironmentFile=-%(host_env)s\n"
        "EnvironmentFile=-%(own_env)s\n"
        "ExecStart=\"%(ameesh)s\" watch send --agent %(agent)s\n"
        % {"agent": agent, "ameesh": ameesh, "path": path, "host_env": HOST_ENV_FILE,
           "own_env": os.path.join(directory, env_name(agent))})
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


def _ameesh_binary() -> str:
    found = shutil.which("ameesh")
    if found:
        return os.path.abspath(found)
    return os.path.join(os.path.expanduser("~"), ".local", "bin", "ameesh")


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


def enable(cfg: Config, db, agent: str, every_s: int, *, run: Callable = subprocess.run,
           env: dict | None = None, actor: str | None = None) -> list[str]:
    """Installs (or updates) the timer on this host, records the watch, notes
    it in the thread. The warnings to print."""
    if not platform.is_linux():
        raise WatchError("a watch needs systemd (Linux)")
    if registry.get(db, agent) is None:
        raise WatchError("unknown agent: %s" % agent)
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
    files[env_name(agent)] = pinned_env(dict(source))
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


def send(cfg: Config, db, agent: str, *, mail_send: Callable | None = None) -> str:
    """One firing: the instruction to `agent` when its watch is recorded for
    this host; else nothing (disabled, or moved). The host check and the
    sending are one transaction on the watch row (codex r1, #303): a move or a
    disable waits for the firing, and a firing after them sends nothing. What
    happened, in words."""
    if mail_send is None:
        from . import cli
        mail_send = cli.main

    def deliver() -> None:
        code = mail_send(["send", agent, consigne(agent), "--from", SENDER])
        if code != 0:
            raise WatchError("sending the instruction to %s failed (code %s)" % (agent, code))
    row = storage.of(db).watches.deliver(agent, cfg.host or "", deliver)
    if row is None:
        return "no watch of %s: nothing sent (ameesh watch disable --agent %s removes " \
               "this timer)" % (agent, agent)
    if not row["sent"]:
        return "the watch of %s is on %s: nothing sent from here" % (agent, row["host"])
    return "instruction sent to %s" % agent


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if getattr(args, "agent", None) and not NAME_RE.match(args.agent):
        print("error: invalid agent name: %r" % args.agent, file=sys.stderr)
        return 2
    cfg = config_mod.load()
    try:
        every = parse_every(args.every) if args.command == "enable" else None
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
