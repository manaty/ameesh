# SPDX-License-Identifier: AGPL-3.0-only
"""L116 — découpage de la suite en parts parallèles (scripts/test-parts.py).

Le découpage doit être déterministe (chaque part de la CI le recalcule seule),
couvrir chaque module une et une seule fois, s'équilibrer sur les durées
mesurées, et peser un module neuf par son nombre de tests.
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("test_parts", REPO / "scripts" / "test-parts.py")
parts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parts)


class RepartitionTest(unittest.TestCase):
    def test_chaque_module_dans_une_seule_part(self):
        mods = parts.modules()
        self.assertIn("test_l116_parts", mods)
        for pilote in ("psql", "psycopg"):
            for n in (1, 4, 6):
                pesees, _ = parts.poids(mods, pilote)
                decoupe = parts.repartir(pesees, n)
                self.assertEqual(len(decoupe), n)
                tous = [m for part in decoupe for m in part]
                self.assertEqual(sorted(tous), mods, f"{pilote} en {n} parts")

    def test_deterministe_et_equilibre(self):
        pesees = {"a": 10.0, "b": 7.0, "c": 5.0, "d": 4.0, "e": 3.0, "f": 1.0}
        decoupe = parts.repartir(pesees, 2)
        self.assertEqual(decoupe, parts.repartir(dict(reversed(pesees.items())), 2))
        charges = sorted(sum(pesees[m] for m in p) for p in decoupe)
        self.assertEqual(charges, [15.0, 15.0])

    def test_repli_par_nombre_de_tests(self):
        with tempfile.TemporaryDirectory() as d:
            dossier = pathlib.Path(d)
            (dossier / "test_mesure.py").write_text("def test_a(): pass\ndef test_b(): pass\n")
            (dossier / "test_neuf.py").write_text("".join(f"def test_{i}(): pass\n" for i in range(6)))
            table = {"psql": {"test_mesure": 4.0}}
            pesees, repli = parts.poids(["test_mesure", "test_neuf"], "psql", table, dossier)
        self.assertEqual(repli, ["test_neuf"])
        self.assertEqual(pesees, {"test_mesure": 4.0, "test_neuf": 12.0})  # 6 tests × 2 s

    def test_liste_pilote_connue(self):
        mods = parts.modules()
        liste = parts.lire_liste(REPO / "tests" / "parts" / "pilote-psql.txt", mods)
        self.assertIn("test_migrations", liste)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# commentaire\ntest_migrations\ntest_qui_n_existe_pas\n")
        try:
            with self.assertRaises(SystemExit):
                parts.lire_liste(f.name, mods)
        finally:
            pathlib.Path(f.name).unlink()


if __name__ == "__main__":
    unittest.main()
