# SPDX-License-Identifier: AGPL-3.0-only
"""Le point d'entrée PUBLIC d'`ameesh models` (L14, revue B5).

`MESH_COMMANDS` de `main.py` ne routait pas `models` : la commande sortait en code
2 « sous-commande inconnue » AVANT même de regarder la base. Un utilisateur ne
passe pas par `mesh_cli` directement, il passe par ici — donc c'est ici qu'il faut
le prouver.

Sans base, l'échec attendu est celui du stockage : ce qui est testé, c'est que le
routage n'est PAS ce qui refuse.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ameesh import db as db_mod  # noqa: E402
from ameesh import main as main_mod  # noqa: E402
from ameesh.discovery import http as discovery_http  # noqa: E402

#: Les clés que ce test ne doit jamais laisser lire, même si le poste en a.
KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")


def run_entrypoint(argv: list[str]) -> tuple[int, str]:
    """Le code rendu et stderr — `--help` sort par SystemExit.

    **Isolé du poste**, parce que la revue a eu raison de le demander : sans cela,
    `discover --dry-run` pourrait lire les vraies clés et appeler un fournisseur si
    une base locale répondait. Trois verrous, donc : aucune clé dans
    l'environnement, `db.connect` refuse (donc pas de catalogue ni de source), et le
    transport HTTP est surveillé — s'il est appelé, le test le sait.
    """
    err = io.StringIO()
    calls: list[str] = []

    def no_network(*args, **kwargs):
        calls.append("fetch_json")
        raise AssertionError("le test du point d'entrée ne doit pas appeler le réseau")

    with patch.dict(os.environ, {name: "" for name in KEY_VARS}):
        with patch.object(db_mod, "connect", side_effect=db_mod.Unavailable("test")), \
                patch.object(discovery_http, "fetch_json", side_effect=no_network):
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = main_mod.main(argv)
            except SystemExit as exit_:  # argparse : `--help` sort ainsi, avec le code 0
                code = int(exit_.code or 0)
    if calls:  # pragma: no cover - garde-fou
        raise AssertionError(f"réseau appelé par {argv}")
    return code, err.getvalue()


class PublicEntrypointTest(unittest.TestCase):
    def test_models_est_route_et_n_est_pas_inconnu(self):
        code, err = run_entrypoint(["models", "list", "--json"])
        self.assertNotIn("inconnue", err, "models doit être routé par main.py")
        self.assertNotEqual(code, 2, "2 est le code d'un routage refusé")

    def test_les_trois_sous_commandes_sont_routees(self):
        for argv in (["models", "list"], ["models", "show", "x"], ["models", "discover", "--dry-run"]):
            with self.subTest(argv=argv):
                code, err = run_entrypoint(argv)
                self.assertNotIn("inconnue", err, str(argv))
                self.assertNotEqual(code, 2, str(argv))

    def test_les_aides_sont_disponibles(self):
        code, err = run_entrypoint(["models", "--help"])
        self.assertEqual(code, 0)
        self.assertNotIn("inconnue", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
