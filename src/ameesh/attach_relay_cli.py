# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh attach-relay` : service de relais et connexion de l'hôte (#7120).

  ameesh attach-relay serve --hosts-file F [--listen H:PORT] [--mesh-cidr C]…
       [--tls-cert F --tls-key F] [--plaintext-loopback] [quotas]
  ameesh attach-relay host --relay URL --host H --token-file F
       [--tenant T] [--agent A]… [--command "…"] [—] [quotas]

`serve` écoute le réseau du mesh (jamais une adresse publique) et apparie
postes et hôtes. `host` ouvre la connexion **sortante** de l'hôte de
l'agent : aucun port entrant. Le jeton d'hôte est lu dans un fichier 0600,
jamais dans l'environnement. TLS est exigé hors boucle locale ; le mode
`--plaintext-loopback` ne sert qu'aux tests locaux.

Ce n'est pas encore `ameesh attach` distant (D3) : la commande interactive
de l'hôte est fournie par l'opérateur (`--command`) et la station est le
client de `attach_transport`. D3 branchera le bail, la session et le
journal.
"""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import socket
import ssl
import sys
import threading

from . import attach_host, config as config_mod
from . import stagnation


def _listen(value: str) -> tuple[str, int]:
    value = (value or "").strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = value.rpartition(":")
        if not host:
            host, port = value, ""
    if not host:
        raise argparse.ArgumentTypeError("adresse d'écoute attendue (hôte:port)")
    try:
        number = int(port or "8790")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port invalide") from exc
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("port hors bornes")
    return host, number


def _duration(value: str) -> float:
    try:
        seconds = stagnation.parse_duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("durée strictement positive attendue")
    return seconds


def _tls_context(cert: str | None, key: str | None) -> ssl.SSLContext | None:
    if not cert and not key:
        return None
    if not cert or not key:
        raise SystemExit("attach-relay: --tls-cert et --tls-key vont ensemble")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(os.path.abspath(os.path.expanduser(cert)),
                                os.path.abspath(os.path.expanduser(key)))
    except (OSError, ssl.SSLError) as exc:
        raise SystemExit("attach-relay: certificat TLS illisible (%s)" % exc)
    return context


def cmd_serve(args) -> int:
    from . import attach_relay as relay_mod
    try:
        hosts = relay_mod.load_hosts_file(args.hosts_file)
        config = relay_mod.RelayConfig(
            listen_host=args.listen[0], listen_port=args.listen[1],
            mesh_cidrs=tuple(args.mesh_cidr or ()), hosts=hosts,
            tls_context=_tls_context(args.tls_cert, args.tls_key),
            plaintext_loopback=args.plaintext_loopback,
            max_sessions=args.max_sessions,
            max_sessions_per_host=args.max_sessions_per_host,
            max_sessions_per_ip=args.max_sessions_per_ip,
            max_session_seconds=args.max_session,
            idle_timeout=args.idle_timeout,
            max_session_bytes=args.max_session_bytes,
            pair_timeout=args.pair_timeout)
    except relay_mod.RelayConfigError as exc:
        print("attach-relay: %s" % exc, file=sys.stderr)
        return 2
    service = relay_mod.RelayServer(config)
    stop = threading.Event()

    def on_stop(_signum, _frame):
        stop.set()

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, on_stop)
        except (ValueError, OSError):
            pass
    try:
        service.start()
        scheme = "https" if config.tls_context is not None else "http"
        print("attach-relay listening on %s://%s:%d" % (scheme, config.listen_host,
                                                       service.port), flush=True)
        while not stop.wait(1.0):
            pass
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        service.stop()
    return 0


def cmd_host(args) -> int:
    if not args.command:
        print("attach-relay host: --command est requis (argv entre guillemets, "
              "jamais passé à un shell)", file=sys.stderr)
        return 2
    try:
        argv = shlex.split(args.command)
    except ValueError as exc:
        print("attach-relay host: --command illisible (%s)" % exc, file=sys.stderr)
        return 2
    if not argv:
        print("attach-relay host: --command vide", file=sys.stderr)
        return 2
    try:
        relay_host, relay_port, context = attach_host.parse_relay_url(
            args.relay, plaintext_loopback=args.plaintext_loopback,
            ca_file=args.ca_file, insecure=args.insecure_tls)
        token = attach_host.token_from_file(args.token_file)
    except (attach_host.HostConfigError, ssl.SSLError, OSError) as exc:
        print("attach-relay host: %s" % exc, file=sys.stderr)
        return 2
    host = args.host
    if not host:
        try:
            host = config_mod.load().host
        except Exception:  # noqa: BLE001 — configuration absente : nom système
            host = ""
        host = host or socket.gethostname()
    from . import actions
    policy = actions.policy_from_env()
    if not policy.rp_id or not policy.origins:
        print("attach-relay host: AMEESH_APPROVE_RP_ID/AMEESH_APPROVE_ORIGINS absents : "
              "aucun grant ne peut être vérifié", file=sys.stderr)
        return 2
    try:
        config = attach_host.HostConfig(
            relay_host=relay_host, relay_port=relay_port, token=token, host=host,
            tenant=args.tenant or "", agents=tuple(args.agent or ()),
            tls_context=context, plaintext_loopback=args.plaintext_loopback,
            command=argv, cwd=args.cwd, max_sessions=args.max_sessions,
            max_session_seconds=args.max_session, keepalive=args.keepalive)
    except attach_host.HostConfigError as exc:
        print("attach-relay host: %s" % exc, file=sys.stderr)
        return 2
    verifier = attach_host.DbGrantVerifier(
        policy=policy, checked_by=args.checked_by or "attach-relay:%s" % host)
    connector = attach_host.HostConnector(config, verifier)
    stop = threading.Event()

    def on_stop(_signum, _frame):
        stop.set()
        connector.stop()

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, on_stop)
        except (ValueError, OSError):
            pass
    runner = threading.Thread(target=connector.run_forever, name="attach-relay-host",
                              daemon=True)
    runner.start()
    try:
        while not stop.wait(1.0):
            pass
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        connector.stop()
        runner.join(5.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh attach-relay", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="le service de relais (réseau du mesh)")
    serve.add_argument("--listen", type=_listen, default=("127.0.0.1", 8790),
                       help="hôte:port d'écoute (boucle locale ou adresse du mesh)")
    serve.add_argument("--mesh-cidr", action="append", default=[],
                       help="CIDR du réseau du mesh (répétable) ; sans lui, seule "
                            "la boucle locale est admise")
    serve.add_argument("--hosts-file", required=True,
                       help="fichier 0600 des jetons d'hôtes : {\"hôte\": \"jeton\"}")
    serve.add_argument("--tls-cert", default=None)
    serve.add_argument("--tls-key", default=None)
    serve.add_argument("--plaintext-loopback", action="store_true",
                       help="tests locaux seulement : clair sur 127.0.0.1")
    serve.add_argument("--max-sessions", type=int, default=64)
    serve.add_argument("--max-sessions-per-host", type=int, default=4)
    serve.add_argument("--max-sessions-per-ip", type=int, default=8)
    serve.add_argument("--pair-timeout", type=float, default=5.0)
    serve.add_argument("--idle-timeout", type=_duration, default=900.0)
    serve.add_argument("--max-session", type=_duration, default=7200.0)
    serve.add_argument("--max-session-bytes", type=int, default=512 * 1024 * 1024)
    serve.set_defaults(func=cmd_serve)

    host = sub.add_parser("host", help="la connexion sortante de l'hôte de l'agent")
    host.add_argument("--relay", required=True, help="https://hôte:port (TLS vérifié)")
    host.add_argument("--host", default=None, help="nom de l'hôte (défaut : configuration)")
    host.add_argument("--tenant", default="", help="tenant sur un hôte partagé")
    host.add_argument("--token-file", required=True, help="jeton du relais, fichier 0600")
    host.add_argument("--agent", action="append", default=[],
                      help="agent servi (répétable ; défaut : tous ceux de l'hôte)")
    host.add_argument("--command", required=True,
                      help="argv interactif de l'hôte (harnais), entre guillemets")
    host.add_argument("--cwd", default=None)
    host.add_argument("--ca-file", default=None, help="CA du certificat du relais")
    host.add_argument("--insecure-tls", action="store_true",
                      help="tests locaux seulement : TLS non vérifié sur la boucle locale")
    host.add_argument("--plaintext-loopback", action="store_true",
                      help="tests locaux seulement : clair sur 127.0.0.1")
    host.add_argument("--checked-by", default=None, help="trace d'audit du grant")
    host.add_argument("--max-sessions", type=int, default=4)
    host.add_argument("--max-session", type=_duration, default=7200.0)
    host.add_argument("--keepalive", type=float, default=30.0)
    host.set_defaults(func=cmd_host)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
