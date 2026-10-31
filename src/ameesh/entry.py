# SPDX-License-Identifier: AGPL-3.0-only
"""#1604 — point d'entrée `agent-mail` : le chemin rapide AVANT tout import lourd.

Le script de console `agent-mail` importe ce module (et non `ameesh.cli`) : les
deux commandes appelées en rafale par les harnais — `statusline` à chaque rendu,
`hook` après chaque outil — sont servies par `ameesh.fastmail`, qui n'importe
que la configuration. Tout le reste, et tout cas douteux, retombe sur
`ameesh.cli:main` : le comportement ne change pas, seule la porte d'entrée est
plus légère.

L'entrée standard est lue UNE fois puis rendue au chemin complet (StringIO) :
le JSON du harnais n'est pas perdu.
"""
from __future__ import annotations

import io
import json
import sys


def _charge_config():
    from .config import load as load_config
    return load_config()


def _nom_statusline(cfg, fastmail, data: dict) -> str:
    """Le nom d'agent du JSON, sans base : celui que le chemin complet écrira.

    Environnement (session menée par l'exécuteur), sinon la liaison déjà notée
    par un hook (`claude` : la barre d'état est celle de Claude Code), sinon
    l'alias du dossier — le même repli que `cmd_statusline`. #1628 : ce nom
    entre dans la clé du cache, pour que deux sessions d'un même dossier ne
    partagent pas la même ligne.
    """
    nom = fastmail.identite(cfg, "claude", data)
    if nom:
        return nom
    from . import identity  # léger : configuration seulement
    return identity.legacy_name(fastmail.statusline_cwd(data), cfg)


def _rapide_statusline(cfg, fastmail, rest: list) -> int | None:
    if rest:
        return None
    raw = sys.stdin.read()
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # #1689 (N3) : l'entrée est rendue au chemin complet quel que soit le
        # JSON (illisible ou pas un objet) : il la relit à l'octet près.
        sys.stdin = io.StringIO(raw)
        return None
    cle = fastmail.statusline_key(data, _nom_statusline(cfg, fastmail, data))
    out = fastmail.read_statusline(cfg, cle)
    if out is None:
        sys.stdin = io.StringIO(raw)  # la commande complète relira l'entrée
        return None
    print(out)
    return 0


def _rapide_hook(cfg, fastmail, rest: list) -> int | None:
    if len(rest) != 1:
        return None
    raw = sys.stdin.read()
    try:
        traite = fastmail.hook_rapide(cfg, rest[0], raw)
    except Exception:
        traite = False
    if traite:
        return 0
    sys.stdin = io.StringIO(raw)
    return None


def main(argv: list | None = None) -> int:
    from .platform.child_environment import managed_entry
    if not managed_entry():
        return 1
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["statusline"], ["hook"]) and not any(
            arg in ("-h", "--help") for arg in argv[1:]):
        try:
            from . import fastmail
            cfg = _charge_config()
        except Exception:
            cfg, fastmail = None, None
        if cfg is not None:
            if argv[0] == "statusline":
                rc = _rapide_statusline(cfg, fastmail, argv[1:])
            else:
                rc = _rapide_hook(cfg, fastmail, argv[1:])
            if rc is not None:
                return rc
    from .cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
