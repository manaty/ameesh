"""#7185 — un « ~ » littéral dans un chemin de bascule.env n'est pas une expansion.

Les TMPDIR d'agents (`.menage/tmp/<agent>~<id>`) portent un « ~ » au milieu du
chemin. Le shell ne le développe qu'en tête d'un mot non cité ou après « : »
dans une affectation ; `_sourced_literal` refusait tout « ~ », si bien que 11
tests de FingerprintTest échouaient sous un TMPDIR d'agent (la CI, sans « ~ »,
ne le voyait pas). Le témoin tourne dans un dossier dont le nom porte « ~ »,
quel que soit le TMPDIR de l'exécution.
"""
import os
import tempfile
import unittest

from tests import support  # noqa: F401  (L142 : environnement isolé)
from ameesh import staging_executor as executor


class SourcedLiteralTildeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agent~7185-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.env = os.path.join(self.root, "bascule.env")

    def read(self, line):
        with open(self.env, "w", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return executor._sourced_literal(self.env, "QUIESCENCE_NOTIFIER")

    def test_a_tilde_inside_a_path_is_literal(self):
        target = os.path.join(self.root, "notifier")
        for line in ("QUIESCENCE_NOTIFIER=%s" % target,
                     "export QUIESCENCE_NOTIFIER='%s'" % target,
                     'QUIESCENCE_NOTIFIER="%s"' % target):
            with self.subTest(line=line):
                self.assertEqual(self.read(line), (target, None))

    def test_a_tilde_the_shell_would_expand_is_refused(self):
        for line in ("QUIESCENCE_NOTIFIER=~/notifier",
                     "QUIESCENCE_NOTIFIER=~ameesh/notifier",
                     "QUIESCENCE_NOTIFIER=/usr/bin:~/bin"):
            with self.subTest(line=line):
                value, problem = self.read(line)
                self.assertIsNone(value)
                self.assertIn("is not a plain path", problem)

    def test_a_quoted_leading_tilde_is_literal(self):
        # entre apostrophes ou guillemets, le shell ne développe pas « ~ »
        self.assertEqual(self.read("QUIESCENCE_NOTIFIER='~/notifier'"),
                         ("~/notifier", None))

    def test_other_expansions_stay_refused(self):
        for line in ("QUIESCENCE_NOTIFIER=$HOME/notifier",
                     "QUIESCENCE_NOTIFIER=/a/$(id)/b",
                     "QUIESCENCE_NOTIFIER=/a/*/b"):
            with self.subTest(line=line):
                value, problem = self.read(line)
                self.assertIsNone(value)
                self.assertIn("is not a plain path", problem)


if __name__ == "__main__":
    unittest.main()
