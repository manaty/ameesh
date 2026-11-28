# SPDX-License-Identifier: AGPL-3.0-only
"""#8485 : le lint de classement des colonnes refuse le gel, pas la prévalidation seule.

Constat du 28/11 : la prévalidation est souvent non concluante (gate-preempted,
run-incomplete), et trois lots ont gelé une migration qui ajoute une colonne
sans l'accorder dans `deploy/sql/role-superviseur.sql` ni la classer dans
`EXCLUES` (#8458 0614, #8459 0615, #8422 0616) ; les deux premiers ont rendu
rouge la composition du paquet h-07 et coûté un cycle.

Témoins : le contrôle est branché dans les `CHECKS` du gel, sans condition de
diff, avec l'identifiant de la classe #5697 ; un arbre ameesh copié (comme
l'arbre extrait du gel) passe sans base ni variable d'environnement (PATH vide,
aucun DSN) ; une migration témoin qui ajoute une colonne ni accordée ni exclue
y fait refuser le gel, en nommant la colonne, le contrat et `EXCLUES`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "freeze_checks_script_8485", REPO / "scripts" / "freeze-checks.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

#: le contrôle attendu, tel qu'il doit tourner à chaque gel
CHECK = ("classement des colonnes #5697",
         ["-m", "unittest", "-q", "tests.test_cluster.ContratDesColonnesTest"])
#: la colonne témoin, ni accordée dans le contrat ni classée dans EXCLUES
COLUMN = "temoin_8485"


def copied_tree(case: unittest.TestCase) -> pathlib.Path:
    """L'arbre extrait du gel : `src`, `tests` et `deploy` d'un arbre ameesh,
    sans `.git` ni cache d'interpréteur, dans un dossier jetable."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="ameesh-8485-"))
    case.addCleanup(shutil.rmtree, root, True)
    for rel in ("src", "tests", "deploy"):
        shutil.copytree(REPO / rel, root / rel,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return root


def witness_migration(root: pathlib.Path) -> str:
    """Ajoute une migration qui crée une colonne non tranchée ; rend son
    libellé (nom du fichier sans `.sql`), pris au prochain numéro libre pour
    ne pas entrer en collision avec une migration amont."""
    folder = root / "src" / "ameesh" / "migrations"
    versions = [int(path.name[:4]) for path in folder.glob("*.sql")
                if path.name[:4].isdigit()]
    label = "%04d_temoin_8485" % (max(versions) + 1)
    (folder / (label + ".sql")).write_text(
        "ALTER TABLE ci_runner_state ADD COLUMN %s boolean;\n" % COLUMN,
        encoding="utf-8")
    return label


class FreezeClassementTest(unittest.TestCase):
    """Le contrôle du gel, joué sur un arbre copié comme l'arbre extrait."""

    def setUp(self) -> None:
        # un PATH vide prouve que le contrôle n'appelle ni psql ni un autre
        # binaire de l'hôte
        self.bin = pathlib.Path(tempfile.mkdtemp(prefix="ameesh-8485-bin-"))
        self.addCleanup(shutil.rmtree, self.bin, True)

    def main(self, root: pathlib.Path) -> tuple[int, str]:
        """Le script de gel, ses contrôles réduits au seul contrôle visé.

        L'environnement est vidé (`clear=True`) : ni DSN, ni variable ameesh,
        ni binaire dans le PATH. Ne restent que de quoi démarrer Python et
        écrire un temporaire — c'est la preuve que l'import de
        `tests.test_cluster` ne lit ni base ni environnement.
        """
        environ = {"PATH": str(self.bin),
                   "HOME": os.environ.get("HOME", str(self.bin)),
                   "TMPDIR": os.environ.get("TMPDIR", str(self.bin)),
                   script.GATE_ENV: "[]"}
        out = io.StringIO()
        with mock.patch.object(script.os, "getcwd", return_value=str(root)), \
                mock.patch.dict(script.os.environ, environ, clear=True), \
                mock.patch.object(script, "CHECKS", (CHECK,)), \
                mock.patch.object(script, "WARN_CHECKS", ()), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = script.main([])
        return code, out.getvalue()

    def test_le_controle_est_branche_dans_les_checks_du_gel(self):
        """Un contrôle absent de [`CHECKS`] ne refuserait aucun gel."""
        self.assertIn(CHECK, script.CHECKS)
        # `-k` rendrait 0 sur zéro test sélectionné : le contrôle doit nommer
        # la classe entière, jamais un filtre
        self.assertNotIn("-k", CHECK[1])
        self.assertEqual(CHECK[1][-1], "tests.test_cluster.ContratDesColonnesTest")

    def test_l_arbre_extrait_passe_sans_base_ni_environnement(self):
        code, out = self.main(copied_tree(self))
        self.assertEqual(code, 0, out)
        self.assertIn("ok  classement des colonnes #5697", out)

    def test_une_colonne_non_classee_refuse_le_gel_et_nomme_la_cause(self):
        root = copied_tree(self)
        label = witness_migration(root)
        code, out = self.main(root)
        self.assertEqual(code, 1, out)
        self.assertIn("RED classement des colonnes #5697", out)
        self.assertIn("ci_runner_state." + COLUMN, out)
        self.assertIn("deploy/sql/role-superviseur.sql", out)
        self.assertIn("EXCLUES", out)
        self.assertIn("migration %s" % label, out)


if __name__ == "__main__":
    unittest.main()
