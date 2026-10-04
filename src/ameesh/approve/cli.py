# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-approve — service d'approbation humaine (spec §9, C8).

  ameesh-approve serve [--config F] [--rp-id R] [--origin O]… [--public-url U]
                       [--bind 127.0.0.1] [--port 8765] [--token-file F]
                       [--state-dir D] [--proposals-dir D] [--level standard|eleve]
        lance le service sur la boucle locale (HTTPS assuré par le mandataire,
        page Nexlink) ;
  ameesh-approve enroll-link --approver human:ID [--ttl S] [options de config]
        crée un lien d'enrôlement à usage unique (à ouvrir SUR LE TÉLÉPHONE) ;
        la passkey créée devient une PROPOSITION de canon, active après PR revue ;
  ameesh-approve gen-token [--token-file F]
        crée le jeton de service (256 bits, fichier 0600 ; n'écrase jamais).

À lancer sous l'utilisateur Unix du service, jamais sous celui des agents.
La base (registre de confiance, table des actions) est lue avec la
configuration d'ameesh (AMEESH_DSN, AMEESH_SCHEMA…).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

from .. import config as ameesh_config, db as db_mod
from . import config as config_mod
from .config import ApproveConfigError


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="fichier JSON de configuration")
    parser.add_argument("--rp-id", default=None)
    parser.add_argument("--origin", action="append", default=[],
                        help="origine autorisée exacte, ex. https://approve.example.org "
                             "(répétable)")
    parser.add_argument("--public-url", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--proposals-dir", default=None)
    parser.add_argument("--level", default=None, choices=["standard", "eleve"])


def _config(args: argparse.Namespace, **extra) -> config_mod.ApproveConfig:
    overrides = {
        "rp_id": args.rp_id, "origins": tuple(args.origin) or None,
        "public_url": args.public_url, "state_dir": args.state_dir,
        "proposals_dir": args.proposals_dir, "level": args.level,
    }
    overrides.update(extra)
    return config_mod.load(path=args.config, overrides=overrides)


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import make_server
    from .service import ApproveService
    from .sources import ActionSourceError, DbActionSource

    cfg = _config(args, bind=args.bind, port=args.port, token_file=args.token_file)
    token = config_mod.read_service_token(cfg.token_file)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s ameesh-approve %(levelname)s %(message)s")
    db = db_mod.connect(ameesh_config.load())
    db_mod.require_schema(db)
    try:
        lock = threading.RLock()
        source = DbActionSource(db, cfg.action_table, lock=lock)
        try:
            source.check()
        except ActionSourceError as exc:
            print("attention : %s — POST /requests répondra 503 tant qu'elle manque" % exc,
                  file=sys.stderr)
        service = ApproveService(cfg, db, source, service_token=token, db_lock=lock)
        server = make_server(service)
        host, port = server.server_address[:2]
        print("ameesh-approve écoute sur http://%s:%d — RP ID %s, origines %s, état %s"
              % (host if ":" not in host else "[%s]" % host, port, cfg.rp_id,
                 ", ".join(cfg.origins), cfg.state_dir), file=sys.stderr)
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
        print("erreur : %s" % exc.message, file=sys.stderr)
        return 2
    print(result["link"])
    print("valable jusqu'à %s, une seule fois — à ouvrir SUR LE TÉLÉPHONE de %s ; la passkey "
          "créée sera une proposition de canon (PR revue)."
          % (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(result["exp"])),
             result["approver"]), file=sys.stderr)
    return 0


def cmd_gen_token(args: argparse.Namespace) -> int:
    path = (args.token_file or os.environ.get("AMEESH_APPROVE_TOKEN_FILE")
            or config_mod.DEFAULT_TOKEN_FILE)
    written = config_mod.write_service_token(path)
    print("jeton de service créé : %s (0600)" % written)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh-approve", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="lancer le service")
    _common(p_serve)
    p_serve.add_argument("--bind", default=None, help="adresse de boucle locale")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--token-file", default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_enroll = sub.add_parser("enroll-link", help="lien d'enrôlement à usage unique")
    _common(p_enroll)
    p_enroll.add_argument("--approver", required=True, help="human:<id>")
    p_enroll.add_argument("--ttl", type=int, default=None, help="durée de validité (s)")
    p_enroll.set_defaults(func=cmd_enroll_link)

    p_token = sub.add_parser("gen-token", help="créer le jeton de service")
    p_token.add_argument("--token-file", default=None,
                         help="défaut : AMEESH_APPROVE_TOKEN_FILE ou %s" % config_mod.DEFAULT_TOKEN_FILE)
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
        print("erreur de configuration : %s" % exc, file=sys.stderr)
        return 2
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, OSError, ValueError) as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
