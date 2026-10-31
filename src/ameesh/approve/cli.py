# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-approve: human approval service (spec §9, C8).

  ameesh-approve serve [--config F] [--rp-id R] [--origin O]... [--public-url U]
                       [--bind 127.0.0.1] [--port 8765] [--token-file F]
                       [--state-dir D] [--proposals-dir D] [--level standard|eleve]
                       [--profile strict|compatible] [--tls-cert F --tls-key F]
        runs the service on loopback: HTTP behind an HTTPS reverse proxy,
        or local TLS (--tls-cert/--tls-key) behind a passthrough gateway;
        the certificate is reloaded when it changes (and on SIGHUP);
        strict profile by default (RP ID = host of the single https origin);
  ameesh-approve enroll-link --approver human:ID [--ttl S] [config options]
        creates a single-use enrollment link (to open ON THE PHONE);
        the passkey created becomes a canon PROPOSAL, active after a reviewed PR;
  ameesh-approve gen-token [--token-file F]
        creates the service token (256 bits, 0600 file; never overwrites).

Run it as the service's Unix user, never as the agents' user.
The database (trust registry, actions table) is read with the ameesh
configuration (AMEESH_DSN, AMEESH_SCHEMA...).
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from .. import config as ameesh_config, db as db_mod
from . import config as config_mod
from .config import ApproveConfigError


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="JSON configuration file")
    parser.add_argument("--rp-id", default=None)
    parser.add_argument("--origin", action="append", default=[],
                        help="exact allowed origin, e.g. https://approve.example.org "
                             "(repeatable)")
    parser.add_argument("--public-url", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--proposals-dir", default=None)
    parser.add_argument("--level", default=None, choices=["standard", "eleve"])
    parser.add_argument("--profile", default=None, choices=list(config_mod.PROFILES),
                        help="strict (default: RP ID = host of the single origin) | "
                             "compatible (local trials, explicit)")


def _config(args: argparse.Namespace, **extra) -> config_mod.ApproveConfig:
    overrides = {
        "rp_id": args.rp_id, "origins": tuple(args.origin) or None,
        "public_url": args.public_url, "state_dir": args.state_dir,
        "proposals_dir": args.proposals_dir, "level": args.level,
        "profile": args.profile,
    }
    overrides.update(extra)
    return config_mod.load(path=args.config, overrides=overrides)


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import make_server
    from .service import ApproveService
    from .sources import ActionSourceError, DbActionSource

    cfg = _config(args, bind=args.bind, port=args.port, token_file=args.token_file,
                  tls_cert=args.tls_cert, tls_key=args.tls_key)
    token = config_mod.read_service_token(cfg.token_file)
    login_token = None
    if cfg.login_api == config_mod.LOGIN_API_OPEN:
        # L232d (avis B1) : l'API des défis n'est ouverte qu'au porteur du
        # jeton dédié, lu ici une fois (mêmes contrôles de fichier privé).
        login_token = config_mod.read_login_token(cfg.login_token_file)
    # §16 (#3889) : GET /fingerprints n'est ouvert qu'au porteur de son jeton
    fingerprints_token = (config_mod.read_fingerprints_token(cfg.fingerprints_token_file)
                          if cfg.fingerprints_token_file else None)
    tls = None
    if cfg.tls:
        from .tls import TlsReloader
        tls = TlsReloader(cfg.tls_cert, cfg.tls_key,
                          workdir=config_mod.ensure_private_dir(cfg.state_dir))
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s ameesh-approve %(levelname)s %(message)s")
    db = db_mod.connect(ameesh_config.load(), writer=False)
    db_mod.require_schema(db)
    try:
        lock = threading.RLock()
        source = DbActionSource(db, cfg.action_table, lock=lock)
        try:
            source.check()
        except ActionSourceError as exc:
            print("warning: %s; POST /requests will answer 503 while it is missing" % exc,
                  file=sys.stderr)
        service = ApproveService(cfg, db, source, service_token=token,
                                 login_token=login_token, db_lock=lock,
                                 fingerprints_token=fingerprints_token)
        server = make_server(service, tls=tls)
        if tls is not None and hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, lambda _sig, _frame: tls.request_reload())
        host, port = server.server_address[:2]
        print("ameesh-approve listening on %s://%s:%d: profile %s, RP ID %s, origins %s, "
              "service API %s, state %s"
              % ("https" if tls is not None else "http",
                 host if ":" not in host else "[%s]" % host, port, cfg.profile, cfg.rp_id,
                 ", ".join(cfg.origins),
                 "ALSO under the public host (api_via_public)" if cfg.api_via_public
                 else "on loopback only", cfg.state_dir), file=sys.stderr)
        print("login API (connection challenges): %s"
              % ("open (dedicated login token)" if login_token is not None else "closed"),
              file=sys.stderr)
        print("server-fault journal (GET /fingerprints): %s"
              % ("open (dedicated read token)" if fingerprints_token is not None
                 else "closed"), file=sys.stderr)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        db.close()
    return 0


def cmd_enroll_link(args: argparse.Namespace) -> int:
    from .service import ApproveError, ApproveService
    from .store import Store

    cfg = _config(args)
    store = Store(cfg.state_dir)
    # Pas de base ni de jeton : on écrit seulement le jeton d'enrôlement
    # (haché) dans le dossier d'état du service.
    service = ApproveService(cfg, None, None, service_token="-" * 32, store=store)
    try:
        result = service.create_enroll_link(args.approver, args.ttl)
    except ApproveError as exc:
        print("error: %s" % exc.message, file=sys.stderr)
        return 2
    print(result["link"])
    print("valid until %s, once only: open it ON THE PHONE of %s; the passkey "
          "created will be a canon proposal (reviewed PR)."
          % (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(result["exp"])),
             result["approver"]), file=sys.stderr)
    return 0


def cmd_gen_token(args: argparse.Namespace) -> int:
    if getattr(args, "login", False):
        path = (args.token_file or os.environ.get("AMEESH_APPROVE_LOGIN_TOKEN_FILE")
                or config_mod.DEFAULT_LOGIN_TOKEN_FILE)
        written = config_mod.write_service_token(path)
        print("login token created: %s (0600) — keep it out of the agents' reach; "
              "the service accepts it only when login_api is \"open\"" % written)
        return 0
    path = (args.token_file or os.environ.get("AMEESH_APPROVE_TOKEN_FILE")
            or config_mod.DEFAULT_TOKEN_FILE)
    written = config_mod.write_service_token(path)
    print("service token created: %s (0600)" % written)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh-approve", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the service")
    _common(p_serve)
    p_serve.add_argument("--bind", default=None, help="loopback address")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--token-file", default=None)
    p_serve.add_argument("--tls-cert", default=None,
                         help="local TLS (passthrough): host PEM certificate, private 0600 "
                              "file of the service user")
    p_serve.add_argument("--tls-key", default=None,
                         help="local TLS: PEM private key, private 0600 file")
    p_serve.set_defaults(func=cmd_serve)

    p_enroll = sub.add_parser("enroll-link", help="single-use enrollment link")
    _common(p_enroll)
    p_enroll.add_argument("--approver", required=True, help="human:<id>")
    p_enroll.add_argument("--ttl", type=int, default=None, help="validity period (s)")
    p_enroll.set_defaults(func=cmd_enroll_link)

    p_token = sub.add_parser("gen-token", help="create the service token")
    p_token.add_argument("--token-file", default=None,
                         help="default: AMEESH_APPROVE_TOKEN_FILE or %s" % config_mod.DEFAULT_TOKEN_FILE)
    p_token.add_argument("--login", action="store_true",
                        help="create the DEDICATED login token instead (L232d, login_api "
                             "open): default AMEESH_APPROVE_LOGIN_TOKEN_FILE or %s"
                             % config_mod.DEFAULT_LOGIN_TOKEN_FILE)
    p_token.set_defaults(func=cmd_gen_token)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv or argv[0] in ("-h", "--help", "help"):
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except ApproveConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
