# SPDX-License-Identifier: AGPL-3.0-only
"""#8355 — le Postgres de service de la CI garde assez de verrous.

Une passe `migrate` sur un schéma neuf est une seule transaction (#7649) qui
garde ~1 100 verrous de relation jusqu'au COMMIT (702 relations et leurs
tables TOAST à la migration 0611) ; le clone du gabarit et DROP SCHEMA
CASCADE en prennent autant. Avec la valeur par défaut (64), la table partagée
(~7 800 verrous) cède à quelques workers parallèles d'une part : « out of
shared memory », part psycopg 11/12 du run 45976721349.

Un service GitHub ne prend pas d'arguments de commande : le réglage passe par
`POSTGRES_INITDB_ARGS` (initdb -c, PostgreSQL 16+), qui l'écrit dans
postgresql.conf avant le premier démarrage. Ce contrat lit le workflow en
texte (PyYAML n'est pas dans le venv de la CI) : chaque service `postgres:`
de tests.yml porte le réglage, à 256 au moins.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/tests.yml"
MINIMUM = 256
_IMAGE = re.compile(r"^(\s*)image:\s*postgres:", re.M)
_REGLAGE = re.compile(r"POSTGRES_INITDB_ARGS:.*max_locks_per_transaction=(\d+)")


def services_postgres(texte: str) -> list[str]:
    """Le bloc de chaque service postgres : de `image:` à la fin de son indentation."""
    blocs = []
    lignes = texte.splitlines()
    for numero, ligne in enumerate(lignes):
        trouve = _IMAGE.match(ligne)
        if not trouve:
            continue
        retrait = len(trouve.group(1))
        bloc = [ligne]
        for suite in lignes[numero + 1:]:
            if suite.strip() and len(suite) - len(suite.lstrip()) < retrait:
                break
            bloc.append(suite)
        blocs.append("\n".join(bloc))
    return blocs


class VerrousDuServiceTest(unittest.TestCase):
    def test_chaque_service_postgres_releve_max_locks(self):
        blocs = services_postgres(WORKFLOW.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(blocs), 3, "psycopg, psql et fs-class")
        for bloc in blocs:
            valeur = _REGLAGE.search(bloc)
            self.assertIsNotNone(valeur, "réglage absent :\n" + bloc)
            self.assertGreaterEqual(int(valeur.group(1)), MINIMUM, bloc)

    def test_le_bloc_s_arrete_au_job_suivant(self):
        texte = ("    services:\n      postgres:\n        image: postgres:17\n"
                 "        env:\n          POSTGRES_DB: a\n    steps:\n"
                 "      - run: echo POSTGRES_INITDB_ARGS: -c max_locks_per_transaction=999\n")
        (bloc,) = services_postgres(texte)
        self.assertIsNone(_REGLAGE.search(bloc))


if __name__ == "__main__":
    unittest.main()
