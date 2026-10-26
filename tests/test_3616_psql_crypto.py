# SPDX-License-Identifier: AGPL-3.0-only
"""#3616 : la part psql fournit `cryptography`, l'extra du canal nexlink (#3536).

Le job psql n'a pas de `.venv-ci` : il installe ses dépendances à la main
(PyYAML pour ManifestesTest, #1511). `tests/test_3536_notify_nexlink.py` exige
`cryptography` ; sans lui, la part psql 2/16 rougit sur `main` (run
40235900946, 14 témoins en échec), alors que les parts psycopg l'installent
déjà dans leur venv. Le défaut n'est pas de sauter les témoins : c'est
l'environnement de la part, qui doit fournir l'extra que la suite exerce.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
CRYPTO = "'cryptography>=41'"
NEXLINK = REPO / "tests" / "test_3536_notify_nexlink.py"


def _job(workflow: str, name: str) -> str:
    m = re.search(r"\n  %s:\n(.*?)(?=\n  [a-z-]+:|\Z)" % re.escape(name),
                  workflow, re.S)
    if m is None:
        raise AssertionError("job introuvable : %s" % name)
    return m.group(1)


class PsqlCryptoTest(unittest.TestCase):
    def test_part_psql_installe_cryptography(self):
        # Sans cet install, test_3536_notify_nexlink échoue en psql 2/16.
        self.assertIn(CRYPTO, _job(TESTS, "psql"))

    def test_venv_psycopg_controle_et_installe_cryptography(self):
        # Le venv cache doit être reconstruit s'il ne sait plus importer
        # cryptography : sinon un cache d'avant #3536 servirait la part.
        m = re.search(r"      - id: deps\n(.*?)(?=\n      - |\n  [a-z-]+:|\Z)",
                      TESTS, re.S)
        self.assertIsNotNone(m, "étape deps introuvable")
        self.assertIn(CRYPTO, m.group(1))
        self.assertIn("import psycopg, cryptography, psutil, coverage, yaml",
                      m.group(1))

    def test_le_temoin_nexlink_ne_se_saute_pas(self):
        # Le canal nexlink n'est pas une option de la suite : un environnement
        # sans cryptography se corrige, il ne se saute pas. #3609 (h-24) garde
        # un saut **conditionnel** à l'absence de l'extra ; la part psql, elle,
        # installe cryptography (voir test_part_psql_installe_cryptography).
        source = NEXLINK.read_text(encoding="utf-8")
        self.assertIn("cryptography", source)
        self.assertIn("CRYPTOGRAPHY_AVAILABLE", source)
        self.assertIn("unittest.skipUnless", source)
        self.assertNotIn("skipUnless(True", source)
        self.assertNotIn("skipIf(True", source)


if __name__ == "__main__":
    unittest.main()
