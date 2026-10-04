#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh — la commande du mesh, et ses sous-commandes.

  ameesh mail <send|inbox|list|status|alias|hook|statusline|whoami>
        la boîte aux lettres ; `agent-mail` reste un alias (les hooks des
        trois harnais l'appellent) ;
  ameesh run [options]        l'exécuteur de la machine ; `agent-runner` reste
        un alias ;
  ameesh list | show          observabilité (hôte, bail, non-lus, budget, clé) ;
  ameesh key …                clés publiques (propriétaire / agent) ;
  ameesh approve | approvals | verify   approbations signées ;
  ameesh work …               lots (work_items) ;
  ameesh cost …               coût des tours et jauges de forfait ;
  ameesh fil list | show <projet> [<lot>] [--last N] | tail <projet> [<lot>]
        les fils lisibles : tout message passé par ameesh, en clair (R12) ;
  ameesh receipt verify | authenticator list   reçus d'approbation (spec §8) ;
  ameesh import-v0 | export-v0          bascule depuis/vers la boîte fichier v0 ;
  ameesh migrate | doctor     schéma et diagnostic ;
  ameesh canon check|show|sync          canon OKF : validation, fiches, registre ;
  ameesh agent spawn <nom> --by <créateur> --ttl <durée>   agent éphémère.

Rien d'autre : ce fichier ne fait que router vers les modules, pour que
`agent-mail` et `agent-runner` continuent de fonctionner à l'identique.
"""
from __future__ import annotations

import sys

#: sous-commandes servies par mesh_cli (parseur déjà en place)
MESH_COMMANDS = (
    "list", "show", "key", "approve", "approvals", "verify", "work", "cost",
    "migrate", "doctor", "import-v0", "export-v0", "canon", "agent",
)
#: sous-commandes des reçus d'approbation (receipts_cli)
RECEIPT_COMMANDS = ("receipt", "authenticator")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "mail":
        from . import cli
        return cli.main(rest)
    if command == "run":
        from . import runner
        return runner.main(rest)
    if command == "fil":
        from . import fil
        return fil.main(rest)
    if command in MESH_COMMANDS:
        from . import mesh_cli
        return mesh_cli.main(argv)
    if command in RECEIPT_COMMANDS:
        from . import receipts_cli
        return receipts_cli.main(argv)
    print("ameesh : sous-commande inconnue %r" % command, file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
