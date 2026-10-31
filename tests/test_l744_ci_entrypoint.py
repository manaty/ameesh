# SPDX-License-Identifier: AGPL-3.0-only
"""Le point d'entrée PUBLIC d'`ameesh ci status <lot>` (#744, L161c).

Le parseur existait déjà dans `mesh_cli` (sous-commande `ci`, `cmd_ci`) et la
doc le décrivait, mais `ci` n'était dans aucune liste de routage de `main.py` :
la commande sortait en code 2 « unknown subcommand » avant même de regarder la
base. Un utilisateur ne passe pas par `mesh_cli` directement, il passe par
`main.py` — donc c'est ici qu'il faut le prouver.

Le second test est la garde demandée par le lot : il énumère les sous-commandes
du parseur de `mesh_cli` et exige que chacune soit routée. La prochaine
sous-commande ajoutée au parseur mais oubliée dans une liste de `main.py` fera
donc échouer la suite, au lieu d'attendre qu'un lecteur de la doc s'en
aperçoive.

Sans base, l'échec attendu est celui du stockage : ce qui est testé, c'est que
le routage n'est PAS ce qui refuse.
"""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import argparse
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ameesh import db as db_mod  # noqa: E402
from ameesh import main as main_mod  # noqa: E402
from ameesh import mesh_cli  # noqa: E402


def mesh_subcommands() -> list[str]:
    """Les sous-commandes de premier niveau de `mesh_cli`, dans l'ordre du parseur.

    `build_parser` est le point d'entrée du module ; `_SubParsersAction` est la
    façon standard (argparse) de retrouver le groupe de sous-commandes.
    """
    parser = mesh_cli.build_parser()
    groups = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if len(groups) != 1:  # pragma: no cover - garde-fou de structure
        raise AssertionError("mesh_cli.build_parser n'a pas un unique groupe de sous-commandes")
    return list(groups[0].choices)


def run_entrypoint(argv: list[str]) -> tuple[int, str]:
    """Le code rendu et stderr, base indisponible — `--help` sort par SystemExit."""
    err = io.StringIO()
    with patch.object(db_mod, "connect", side_effect=db_mod.Unavailable("test")):
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = isolated_entrypoint(main_mod.main, argv)
        except SystemExit as exit_:  # argparse : `--help` sort ainsi, avec le code 0
            code = int(exit_.code or 0)
    return code, err.getvalue()


class CiEntrypointTest(unittest.TestCase):
    def test_ci_est_route_par_le_point_d_entree(self):
        code, err = run_entrypoint(["ci", "status", "744"])
        self.assertNotIn("unknown subcommand", err, "ci doit être routé par main.py")
        self.assertNotEqual(code, 2, "2 est le code d'un routage refusé")

    def test_chaque_sous_commande_de_mesh_cli_est_routee(self):
        subcommands = mesh_subcommands()
        self.assertIn("ci", subcommands, "le parseur de mesh_cli doit porter `ci`")
        for name in subcommands:
            with self.subTest(command=name):
                code, err = run_entrypoint([name, "--help"])
                self.assertNotIn("unknown subcommand", err, name)
                self.assertEqual(code, 0, "l'aide de %r doit sortir en 0" % name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
