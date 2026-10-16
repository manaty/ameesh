# SPDX-License-Identifier: AGPL-3.0-only
"""Lectures GitHub des scripts de veille, par le cache partagé de l'hôte (#860).

    ameesh github read <chemin> [--ttl SECONDES]

Le 2026-10-13, le quota core du jeton partagé (5000/h) a été épuisé et la
lecture de CI de tous les agents a été bloquée. Le cache de lectures partagées
(#848) couvrait les lecteurs internes d'ameesh (`notify`, `progress`,
`versions`), mais les veilleurs de CI propres à chaque agent appelaient `gh`
directement, hors cache : à ~35 agents, la marge restait nulle (décision owner
#18841, réponse b — rester au jeton partagé et étendre le cache aux veilles).

Cette commande est leur porte d'entrée : elle lit un chemin REST de l'API
GitHub par le même client que `ameesh notify` — cache disque de l'hôte,
revalidation `If-None-Match` (un 304 ne consomme pas de quota), arrêt au quota
épuisé par ressource d'API, sans boucle agressive.

    # le dernier passage du workflow des tests sur une branche
    ameesh github read "/repos/acme/web/actions/runs?branch=lot%2F42-x&per_page=1"

    # l'état d'une PR (tête, fusionnable)
    ameesh github read "/repos/acme/web/pulls/42"

Sortie : le corps JSON de la réponse, sur une ligne, tel que l'API le rend.
Codes de sortie :

* 0 : lecture servie (réseau, revalidation ou cache) ;
* 1 : erreur GitHub ou de transport (404, panne, chemin refusé) ;
* 2 : usage ;
* 3 : quota épuisé **et** rien en cache pour ce chemin : le veilleur attend le
  reset (donné par le message sur `stderr`) au lieu de boucler.

Les écritures (fusion, relance d'un run) restent à `gh` : cette commande ne
fait que des `GET`. Le jeton n'est jamais écrit ni journalisé.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config as config_mod

#: code de sortie quand le quota est épuisé et que le chemin n'est pas en cache
EXIT_RATE_LIMITED = 3
#: durées de garde acceptées par `--ttl` : les mêmes bornes que `github.ci_ttl`
MIN_TTL = 5.0


def read_path(cfg, path: str, *, ttl: float | None = None):
    """Lit `path` par le client GitHub partagé de l'hôte (#848).

    Sans `ttl`, la garde est le réglage `github.ci_ttl` de la configuration
    (défaut 60 s) : une lecture identique est servie du disque, une lecture
    périmée est revalidée sans consommer de quota si rien n'a changé."""
    from . import plan_github

    ttl = plan_github.settings_of(cfg).ci_ttl if ttl is None else float(ttl)
    client = plan_github.client(cache_dir=getattr(cfg, "state_dir", None),
                                ttl=float(ttl))
    if client is None:  # pragma: no cover - `client` ne rend None que sur demande
        raise plan_github.GithubError("no GitHub token and no local gh")
    return client.get(path)


def check_path(path: str) -> str:
    """Le chemin REST : absolu (`/repos/…`), sans blanc ni saut de ligne."""
    text = str(path or "")
    if not text.startswith("/"):
        raise ValueError("a REST path starting with / is expected, not %r" % path)
    if any(char.isspace() for char in text):
        raise ValueError("a REST path carries no whitespace, not %r" % path)
    return text


def ttl_of(value) -> float:
    """`--ttl` validé : un nombre de secondes, au moins `MIN_TTL`."""
    try:
        ttl = float(value)
    except (TypeError, ValueError):
        raise ValueError("--ttl: a number of seconds is expected, not %r" % value)
    if ttl < MIN_TTL:
        raise ValueError("--ttl: at least %d seconds, not %s" % (MIN_TTL, value))
    return ttl


def cmd_read(cfg, args) -> int:
    from . import plan_github

    try:
        path = check_path(args.path)
        ttl = None if args.ttl is None else ttl_of(args.ttl)
    except ValueError as exc:
        print("github: %s" % exc, file=sys.stderr)
        return 2
    try:
        value = read_path(cfg, path, ttl=ttl)
    except plan_github.GithubRateLimited as exc:
        when = time.strftime("%H:%M:%SZ", time.gmtime(exc.reset)) if exc.reset else "unknown"
        print("github: quota exhausted (reset at %s) and %s is not in the shared "
              "cache yet" % (when, path), file=sys.stderr)
        return EXIT_RATE_LIMITED
    except plan_github.GithubError as exc:
        print("github: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(value, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh github", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)
    p_read = sub.add_parser(
        "read", help="read a GitHub REST path through the host's shared cache (#848)")
    p_read.add_argument("path", help="/repos/owner/repo/... (REST path, query allowed)")
    p_read.add_argument(
        "--ttl", default=None, metavar="SECONDS",
        help="guard of the read in the shared cache (default github.ci_ttl, min %d)"
             % MIN_TTL)
    p_read.set_defaults(func=cmd_read)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(config_mod.load(), args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
