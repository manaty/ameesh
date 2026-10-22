# SPDX-License-Identifier: AGPL-3.0-only
"""Services utilisateur d'ameesh (L65).

  ameesh service install [--agent A]... [--no-runner] [--no-notify] [--dry-run]
  ameesh service uninstall [--agent A]... [--no-runner] [--no-notify]
  ameesh service status [--agent A]... [--json]

Sur macOS, installe les **LaunchAgents** de l'utilisateur (L65) : un exécuteur
et `ameesh notify`, ou un exécuteur par agent avec `--agent`. Ils démarrent à
l'ouverture de session, tournent sous le compte de l'humain et écrivent dans
`~/Library/Logs/ameesh/`. `ameesh service status` lit l'état réel dans
launchd ; `uninstall` décharge et retire les plists.

Le service source `~/.config/ameesh/env` s'il existe (le même fichier que les
unités systemd) : le DSN, `PGPASSWORD` et les jetons n'entrent donc pas dans
le plist, qui reste lisible (0644) sans secret. `AMEESH_BIN` force le chemin du
binaire ; sinon `ameesh` est cherché dans le PATH, puis dans `~/.local/bin`
(l'installation `uv tool install`).

Linux : ce sont les unités de `deploy/systemd/` (docs/BASCULE.md étape 7) ;
`--dry-run` reste disponible pour lire le plist que macOS recevrait. Windows
natif (service) viendra avec L67.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from typing import Mapping

from . import platform
from .config import NAME_RE
from .platform import service as launchd

SCHEMA = "ameesh-services/1"


class ServiceError(RuntimeError):
    """Une commande `ameesh service` refusée ou en échec (code 1)."""


def _ameesh_binary(env: Mapping[str, str] | None = None) -> str:
    """Le binaire que le plist exécutera, en chemin absolu.

    `AMEESH_BIN` force ; sinon le script `ameesh` qui porte CE processus
    (`sys.argv[0]`, l'installation `uv tool`), sinon le `ameesh` du PATH, sinon
    `~/.local/bin/ameesh`. Un binaire temporaire (arbre source, `python -m`)
    n'est jamais préféré au script installé : le service doit survivre à la
    session qui l'installe.
    """
    env = os.environ if env is None else env
    forced = env.get("AMEESH_BIN")
    if forced:
        return os.path.abspath(os.path.expanduser(forced))
    launched = sys.argv[0] if sys.argv else ""
    if launched and os.path.basename(launched) == "ameesh" and os.path.isfile(launched):
        return os.path.abspath(launched)
    found = shutil.which("ameesh", path=env.get("PATH") or None)
    if found:
        return os.path.abspath(found)
    return os.path.join(env.get("HOME") or os.path.expanduser("~"),
                        ".local", "bin", "ameesh")


def _agents(values: list[str]) -> list[str]:
    """Noms d'agents, dans l'ordre, sans doublon ; un nom invalide est refusé
    (il nomme un fichier de plist et un label launchd)."""
    out: list[str] = []
    for value in values:
        for agent in str(value).split(","):
            agent = agent.strip()
            if not agent:
                continue
            if not NAME_RE.match(agent):
                raise ServiceError("invalid agent name %r (letters, digits, . _ -)"
                                   % agent)
            if agent not in out:
                out.append(agent)
    return out


def services(agents: list[str], *, runner: bool = True, notify: bool = True,
             hostname: str | None = None) -> list[launchd.Service]:
    """Les services demandés : l'exécuteur de l'hôte (ou un par agent), et
    `ameesh notify`."""
    hostname = hostname or socket.gethostname()
    out: list[launchd.Service] = []
    if runner:
        if agents:
            for agent in agents:
                out.append(launchd.Service(
                    name="runner-%s" % agent,
                    args=("run", "--agents", agent, "--runner-id",
                          "runner-%s@%s" % (agent, hostname)),
                    description="ameesh — exécuteur de l'agent %s" % agent,
                    throttle=30,
                    label_name="%s.runner.%s" % (launchd.LABEL_PREFIX, agent)))
        else:
            out.append(launchd.Service(
                name="runner", args=("run", "--poll", "5"),
                description="ameesh — exécuteur de l'hôte", throttle=10))
    if notify:
        out.append(launchd.Service(
            name="notify", args=("notify", "--interval", "30"),
            description="ameesh notify — alertes poussées au responsable humain",
            throttle=10))
    return out


def _selection(args) -> list[launchd.Service]:
    agents = _agents(args.agent)
    if args.no_runner and args.no_notify:
        raise ServiceError("nothing to do: --no-runner and --no-notify together")
    if agents and args.no_runner:
        raise ServiceError("--agent names a runner: it cannot be combined with "
                           "--no-runner")
    return services(agents, runner=not args.no_runner, notify=not args.no_notify)


def _not_available(exc: platform.NotAvailable) -> int:
    print("ameesh service: %s" % exc, file=sys.stderr)
    print("ameesh service manages launchd agents on macOS; on Linux install the "
          "units from deploy/systemd (docs/BASCULE.md step 7)", file=sys.stderr)
    return 1


def _install(args, env: Mapping[str, str], run) -> int:
    selected = _selection(args)
    binary = _ameesh_binary(env)
    if args.dry_run:
        for service in selected:
            print("# dry run: %s (label %s)" % (launchd.plist_path(service, env),
                                                service.label))
            print(launchd.plist_bytes(service, binary, env).decode("utf-8"))
        return 0
    try:
        for service in selected:
            path = launchd.install(service, binary, run=run, env=env)
            print("installed  %-28s %s" % (service.label, path))
    except platform.NotAvailable as exc:
        return _not_available(exc)
    print("logs       %s" % launchd.log_dir(env))
    print("state      ameesh service status")
    return 0


def _uninstall(args, env: Mapping[str, str], run) -> int:
    selected = _selection(args)
    try:
        for service in selected:
            path = launchd.plist_path(service, env)
            if launchd.uninstall(service, run=run, env=env):
                print("removed    %-28s %s" % (service.label, path))
            else:
                print("absent     %-28s %s" % (service.label, path))
    except platform.NotAvailable as exc:
        return _not_available(exc)
    return 0


def _status(args, env: Mapping[str, str], run) -> int:
    selected = _selection(args)
    try:
        rows = [launchd.status(service, run=run, env=env) for service in selected]
    except platform.NotAvailable as exc:
        return _not_available(exc)
    if args.json:
        print(json.dumps({"schema": SCHEMA, "system": platform.SYSTEM,
                          "services": rows}, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        if not row["installed"]:
            state = "not installed"
        elif not row["loaded"]:
            state = "installed, not loaded"
        else:
            detail = row.get("state") or "loaded"
            if row.get("pid"):
                detail += ", pid %s" % row["pid"]
            state = "installed, loaded (%s)" % detail
        print("%-22s %-34s %s" % (row["label"], state, row["path"]))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh service",
        description="install, remove or inspect ameesh user services (launchd, macOS)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
            ("install", "write the launchd agents and load them"),
            ("uninstall", "unload and remove the launchd agents"),
            ("status", "read the installed agents and their launchd state")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--agent", action="append", default=[], metavar="AGENT",
            help="one runner per agent (repeatable, or comma-separated); "
                 "without it, one runner for the host")
        command.add_argument("--no-runner", action="store_true",
                             help="do not touch the runner")
        command.add_argument("--no-notify", action="store_true",
                             help="do not touch ameesh notify")
        if name == "install":
            command.add_argument("--dry-run", action="store_true",
                                 help="print the plists; write and load nothing")
        if name == "status":
            command.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None, *,
         run=subprocess.run) -> int:
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            return _install(args, env, run)
        if args.command == "uninstall":
            return _uninstall(args, env, run)
        return _status(args, env, run)
    except (ServiceError, launchd.ServiceError) as exc:
        print("ameesh service: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
