# SPDX-License-Identifier: AGPL-3.0-only
"""#2973 : chemins Process relatifs au périmètre ameesh.

La fédération réelle (home de ThePackengers) roote le membre à `.` et déclare
`extensions.ameesh.scope: ameesh` comme un filtre de chemins ; sa carte Process
vit sous `ameesh/processus/`. Elle est une règle (valide, injectée) ; une carte
hors `processus/` ou hors périmètre ne l'est pas ; un bundle sans périmètre et
une carte à la racine `processus/` restent inchangés.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import canon, canon_sync, reprise

from .test_canon import commit_all, write
from .test_l43_perimetre_hote import HOST, _Canons, codes
from .test_l538_process_inject import REGLE, TITRE, carte

CARTE = "ameesh/processus/tpk-work-delivery.md"


class ProcessPerimetreTest(_Canons, unittest.TestCase):
    def charge(self, cartes: dict, *, scope: str | None = "ameesh", name: str = "t"):
        clone = self.acme(scope=scope, name=name)
        for chemin, contenu in cartes.items():
            write(clone, chemin, contenu)
        commit_all(clone, "cartes Process")
        loaded = canon.load(clone, default=True)
        return loaded, canon.validate(loaded)

    def test_carte_sous_le_perimetre_valide_et_injectee(self):
        loaded, found = self.charge({CARTE: carte(
            body="# Livraison\n\n%s\n- %s\n%s\n\nTexte intégral.\n" % (
                canon.PROCESS_PROJECTION_START, REGLE, canon.PROCESS_PROJECTION_END))})
        self.assertEqual(canon.errors(found), [], [f.to_dict() for f in found])
        self.assertEqual([p.title for p in loaded.processes], [TITRE])
        regle = loaded.processes[0]
        self.assertTrue(regle.valid)
        self.assertEqual((regle.responsible, regle.updated), ("human:alice", "2026-10-12"))
        self.assertIn(REGLE, regle.projection)
        self.assertTrue(regle.fiche.ref.startswith("home:%s@" % CARTE), regle.fiche.ref)
        self.assertEqual(canon_sync.assess(loaded, HOST, found)[0], canon_sync.CANON_OK)
        cfg = types.SimpleNamespace(canon="(fixture)")
        with mock.patch("ameesh.canon.load_configured", return_value=[loaded]):
            texte, diagnostics = reprise._process_rules(cfg, agent="t1", host=HOST)
        self.assertIn(REGLE, texte)
        self.assertNotIn("Texte intégral", texte)
        self.assertEqual(diagnostics, [])

    def test_perimetre_en_liste_chaque_dossier(self):
        clone = self.acme(scope="[ameesh, equipe-b]")
        write(clone, "equipe-b/processus/b.md", carte(title="Règle B"))
        write(clone, CARTE, carte())
        commit_all(clone)
        loaded = canon.load(clone, default=False)
        self.assertEqual(canon.errors(canon.validate(loaded)), [])
        self.assertEqual(sorted(p.title for p in loaded.processes), sorted(["Règle B", TITRE]))
        self.assertTrue(all(p.valid for p in loaded.processes))

    def test_carte_du_perimetre_hors_processus_refusee(self):
        for n, chemin in enumerate(("ameesh/agents/proc.md", "ameesh/plan/processus/proc.md",
                                    "ameesh/processus.md")):
            with self.subTest(chemin=chemin):
                loaded, found = self.charge({chemin: carte()}, name="t%d" % n)
                self.assertIn("process-path", codes(found, canon.ERROR))
                self.assertEqual(len(loaded.processes), 1)
                self.assertFalse(loaded.processes[0].valid)
                message = next(f.message for f in found if f.code == "process-path")
                self.assertIn("ameesh/processus/", message)

    def test_carte_hors_perimetre_jamais_lue(self):
        # aucun assouplissement au-delà du périmètre : `org/processus/` et la
        # racine `processus/` sont hors de `scope: ameesh`, donc ignorées
        for n, chemin in enumerate(("org/processus/autre.md", "processus/racine.md")):
            with self.subTest(chemin=chemin):
                loaded, found = self.charge({chemin: carte()}, name="t%d" % n)
                self.assertEqual(loaded.processes, [])
                self.assertEqual(loaded.out_of_scope, {"home": 4})
                self.assertEqual(canon.errors(found), [])

    def test_sans_perimetre_la_carte_imbriquee_reste_refusee(self):
        loaded, found = self.charge({CARTE: carte()}, scope=None)
        self.assertIn("process-path", codes(found, canon.ERROR))
        self.assertFalse([p for p in loaded.processes if p.valid])

    def test_perimetre_racine_carte_racine_inchangee(self):
        loaded, found = self.charge({"processus/racine.md": carte(),
                                     CARTE: carte(title="imbriquee")}, scope=".")
        valides = {p.title: p.valid for p in loaded.processes}
        self.assertEqual(valides, {TITRE: True, "imbriquee": False})
        self.assertIn("process-path", codes(found, canon.ERROR))


class CheminProcessTest(unittest.TestCase):
    def test_regle_de_chemin(self):
        ok = canon.process_path_ok
        self.assertTrue(ok("processus/a.md"))
        self.assertTrue(ok("processus/sous/a.md"))
        self.assertFalse(ok("processus"))
        self.assertFalse(ok("ameesh/processus/a.md"))
        self.assertFalse(ok("ameesh/processus/a.md", None))
        self.assertTrue(ok("ameesh/processus/a.md", ["ameesh"]))
        self.assertTrue(ok("a/b/processus/x.md", ["a/b"]))
        self.assertFalse(ok("a/processus/x.md", ["a/b"]))
        self.assertFalse(ok("ameeshx/processus/a.md", ["ameesh"]))
        self.assertFalse(ok("ameesh/processus", ["ameesh"]))
        self.assertFalse(ok("autre/processus/a.md", ["ameesh"]))
        self.assertFalse(ok("ameesh/processus/a.md", ["ameesh/processus"]))
        self.assertTrue(ok("processus/a.md", [""]))
        self.assertFalse(ok("ameesh/processus/a.md", [""]))


if __name__ == "__main__":
    unittest.main()
