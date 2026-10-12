# SPDX-License-Identifier: AGPL-3.0-only
"""L148, stage 1a-2 : `menage` → `housekeeping`, alias de transition.

`ameesh.menage` est un alias `sys.modules` de `ameesh.housekeeping` ;
l'ancienne graphie reste utilisable jusqu'au stage 7 : un
`from ameesh.menage import *` donne encore `cmd_menage` et `agent-mesh menage`
reste accepté. En revanche, `menage` ne doit plus apparaître dans l'aide :
la ligne d'usage ne liste que les commandes visibles.
"""
from __future__ import annotations

import unittest

from ameesh import housekeeping, mesh_cli


class MenageAliasShimTest(unittest.TestCase):
    def test_star_import_exposes_cmd_menage(self) -> None:
        """P2 : `__all__` garde l'ancien nom tant que le module existe."""
        namespace: dict = {}
        exec("from ameesh.menage import *", namespace)
        self.assertIn("cmd_menage", namespace)
        self.assertIs(namespace["cmd_menage"], housekeeping.cmd_housekeeping)

    def test_menage_command_still_parses(self) -> None:
        """L'alias caché reste une commande acceptée (pas seulement absent)."""
        args = mesh_cli.build_parser().parse_args(["menage", "--dry-run"])
        self.assertIs(args.func, mesh_cli.cmd_menage)

    def test_help_hides_menage(self) -> None:
        """P3 : ni la ligne d'usage ni la liste des commandes ne le montrent."""
        aide = mesh_cli.build_parser().format_help()
        self.assertIn("housekeeping", aide)
        self.assertNotIn("menage", aide)


if __name__ == "__main__":
    unittest.main()
