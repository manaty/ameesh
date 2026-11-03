# SPDX-License-Identifier: AGPL-3.0-only
"""#5629 : facteur d'échelle des bornes murales des tests (`support.scaled`).

Sous psql, chaque requête est un processus : le nocturne chargé y est
plusieurs fois plus lent qu'en local, et une borne murale fixe y rougit sans
régression du code. `AMEESH_TEST_TIME_SCALE` fixe le facteur ; à défaut, 5
sous psql, 1 sinon ; la variable survit au ménage d'environnement du banc.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from tests import support


class TimeScaleTest(unittest.TestCase):
    def scale(self, **env) -> float:
        base = {name: value for name, value in os.environ.items()
                if name not in ("AMEESH_TEST_TIME_SCALE", "AMEESH_DRIVER",
                                "AGENT_MESH_DRIVER")}
        base.update(env)
        with mock.patch.dict(os.environ, base, clear=True):
            return support._time_scale()

    def test_defaut_selon_le_pilote(self):
        self.assertEqual(self.scale(), 1.0)
        self.assertEqual(self.scale(AMEESH_DRIVER="psycopg"), 1.0)
        self.assertEqual(self.scale(AMEESH_DRIVER="psql"), 5.0)
        self.assertEqual(self.scale(AGENT_MESH_DRIVER="psql"), 5.0)

    def test_la_variable_prime(self):
        self.assertEqual(self.scale(AMEESH_TEST_TIME_SCALE="2.5"), 2.5)
        self.assertEqual(self.scale(AMEESH_TEST_TIME_SCALE="1",
                                    AMEESH_DRIVER="psql"), 1.0)

    def test_une_valeur_invalide_est_refusee(self):
        # jamais une borne raccourcie en silence
        for raw in ("0.5", "0", "-2", "vite"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.scale(AMEESH_TEST_TIME_SCALE=raw)

    def test_scaled_applique_le_facteur(self):
        with mock.patch.object(support, "TIME_SCALE", 4.0):
            self.assertEqual(support.scaled(1.5), 6.0)

    def test_la_variable_survit_au_menage_du_banc(self):
        self.assertIn("AMEESH_TEST_TIME_SCALE", support.BENCH_ENV_VARS)


if __name__ == "__main__":
    unittest.main()
