# SPDX-License-Identifier: AGPL-3.0-only
"""#5556 — la marge du bloc des règles se voit avant la fusion.

Incident home#248 (02/11) : une PR du canon a poussé le bloc des règles 43
octets au-delà de la limite de la plus ancienne version servie ; rien ne l'a
dit avant `main`. Témoins : le gel d'un lot du canon qui coupe le bloc est
refusé, la marge est écrite dans la note de gel sinon ; un lot qui raccourcit
un bloc déjà coupé passe ; `canon check` affiche la marge sans constat.
"""
from __future__ import annotations

import io
import os
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import canon, freeze_gate, lot_class
from ameesh.work import WorkError

from .test_canon import commit_all, git, write
from .test_l538_process_inject import RACINE, _canon_approuve, carte

ITEM = {"branch": "canon/5556-lot", "branch_target": "main"}


def _corps(octets: int) -> str:
    return "# Processus de lot\n\n- " + "a" * octets + "\n"


class GelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clone = _canon_approuve(tmp.name, contenu=carte(body=_corps(100)))
        git(self.clone, "checkout", "-qb", "canon/5556-lot")
        patcher = mock.patch.object(lot_class, "canon_clones",
                                    return_value=[(self.clone, "")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lot(self, octets: int) -> str:
        write(self.clone, RACINE, carte(body=_corps(octets)))
        return commit_all(self.clone, "lot", push=False)

    def _gate(self, sha, item=ITEM):
        db = types.SimpleNamespace(cfg=None)
        return freeze_gate._canon_rules_block(db, item, sha, self.clone)

    def test_marge_dans_la_note_de_gel(self):
        note = self._gate(self._lot(200))
        self.assertRegex(note, r"^canon-rules-block: rules block \d+/%d bytes, "
                         r"margin \d+ bytes" % canon.RULES_BLOC_LIMIT_COMPAT)

    def test_bloc_coupe_a_la_tete_refuse_le_gel(self):
        sha = self._lot(canon.RULES_BLOC_ROOM_COMPAT + 50)
        with self.assertRaisesRegex(WorkError,
                                    r"process-rules-block-too-long.*CUT.*bytes over"):
            self._gate(sha)

    def test_lot_qui_raccourcit_un_bloc_deja_coupe_passe(self):
        git(self.clone, "checkout", "-q", "main")
        write(self.clone, RACINE, carte(body=_corps(canon.RULES_BLOC_ROOM_COMPAT + 500)))
        commit_all(self.clone, "base coupée")
        git(self.clone, "checkout", "-qB", "canon/5556-lot")
        note = self._gate(self._lot(canon.RULES_BLOC_ROOM_COMPAT + 100))
        self.assertIn("already cut at the base", note)

    def test_lot_qui_fait_grossir_un_bloc_deja_coupe_est_refuse(self):
        git(self.clone, "checkout", "-q", "main")
        write(self.clone, RACINE, carte(body=_corps(canon.RULES_BLOC_ROOM_COMPAT + 100)))
        commit_all(self.clone, "base coupée")
        git(self.clone, "checkout", "-qB", "canon/5556-lot")
        with self.assertRaises(WorkError):
            self._gate(self._lot(canon.RULES_BLOC_ROOM_COMPAT + 500))

    def test_cible_implicite_ne_prend_pas_le_lot_pour_sa_base(self):
        # revue cx3 B1 : sans branch_target, HEAD (le lot) n'est pas la base ;
        # la vraie cible (main, saine) est résolue et le dépassement refusé
        sha = self._lot(canon.RULES_BLOC_ROOM_COMPAT + 50)
        for item in ({"branch": "canon/5556-lot"},
                     {"branch": "canon/5556-lot", "branch_target": ""}):
            with self.subTest(item=item), self.assertRaises(WorkError):
                self._gate(sha, item)

    def test_base_illisible_n_exempte_pas(self):
        sha = self._lot(canon.RULES_BLOC_ROOM_COMPAT + 50)
        with self.assertRaises(WorkError):
            self._gate(sha, {"branch": "canon/5556-lot",
                             "branch_target": "cible-absente"})

    def _gate_mesures(self, tete, base):
        """revue docs-01 : vraies têtes git, mesures (règles, manque) imposées."""
        git(self.clone, "checkout", "-q", "main")
        write(self.clone, RACINE, carte(body=_corps(canon.RULES_BLOC_ROOM_COMPAT + 100)))
        commit_all(self.clone, "base coupée")
        git(self.clone, "checkout", "-qB", "canon/5556-lot")
        sha = self._lot(canon.RULES_BLOC_ROOM_COMPAT + 200)

        def bloc(rules, over):
            return canon.ProcessBlock(
                size=canon.RULES_BLOC_LIMIT_COMPAT, rules=rules,
                limit=canon.RULES_BLOC_LIMIT_COMPAT, room=canon.RULES_BLOC_ROOM_COMPAT,
                cut=True, cards="T (%d)" % rules, over=over)
        with mock.patch.object(canon, "process_block",
                               side_effect=[bloc(*tete), bloc(*base)]):
            return self._gate(sha)

    def test_regles_egales_et_un_diagnostic_de_plus_refuse(self):
        with self.assertRaises(WorkError):
            self._gate_mesures(tete=(14943, 230), base=(14943, 143))

    def test_regles_plus_longues_et_un_diagnostic_de_moins_exempte(self):
        note = self._gate_mesures(tete=(14939, 124), base=(14903, 190))
        self.assertIn("already cut at the base, 190 bytes over", note)

    def test_tete_mesuree_dans_le_depot_du_gel_pas_dans_le_clone_configure(self):
        # revue qa-01 : clone configuré A sain, clone d'auteur B porteur de la
        # tête coupée (home#248) ; A ne connaît pas la tête, B la mesure
        auteur = self.clone + "-auteur"
        git(os.path.dirname(self.clone), "clone", "-q", self.clone, auteur)
        git(auteur, "checkout", "-qB", "canon/5556-lot", "origin/main")
        write(auteur, RACINE, carte(body=_corps(canon.RULES_BLOC_ROOM_COMPAT + 50)))
        sha = commit_all(auteur, "lot", push=False)
        db = types.SimpleNamespace(cfg=None)
        with self.assertRaisesRegex(WorkError, "process-rules-block-too-long"):
            freeze_gate._canon_rules_block(db, ITEM, sha, auteur)

    def test_lot_ordinaire_non_mesure(self):
        self.assertEqual(self._gate("a" * 40, {"branch": "lot/1-x",
                                               "branch_target": "main"}), "")

    def test_tete_illisible_dit_non_mesure_sans_refuser(self):
        note = self._gate("b" * 40)
        self.assertIn("not measured", note)


class MargeTest(unittest.TestCase):
    """Revue impl-02 B2 : coupe due aux diagnostics alors que rules <= room."""

    def _bloc(self, regles: str, diag: str):
        from ameesh import reprise
        process = types.SimpleNamespace(valid=True, title="T", fiche=None)
        loaded = types.SimpleNamespace(processes=[process])
        with mock.patch.object(reprise, "_process_fragment",
                               return_value=(regles, diag)):
            return canon.process_block(loaded)

    def test_marge_negative_si_et_seulement_si_coupe(self):
        from ameesh import reprise
        regles, diag = "a" * 14800, "x" * 500
        self.assertLessEqual(len(regles), canon.RULES_BLOC_ROOM_COMPAT)
        mesure = self._bloc(regles, diag)
        self.assertTrue(mesure.cut)
        self.assertLess(mesure.margin, 0)
        self.assertNotIn("-", mesure.line().split("bytes over")[0])
        # la marge est le manque exact : un octet de moins ne suffit pas
        limite = canon.RULES_BLOC_LIMIT_COMPAT
        juste = regles[:mesure.margin]
        self.assertIn(juste, reprise._rules_bloc(juste, limite, [diag]))
        presque = regles[:mesure.margin + 1]
        self.assertNotIn(presque, reprise._rules_bloc(presque, limite, [diag]))

    def test_bloc_entier_marge_positive(self):
        mesure = self._bloc("a" * 1000, "")
        self.assertFalse(mesure.cut)
        self.assertEqual(mesure.margin, mesure.limit - mesure.size)
        self.assertGreater(mesure.margin, 0)


class CheckTest(unittest.TestCase):
    def test_canon_check_affiche_la_marge(self):
        from ameesh import canon_cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clone = _canon_approuve(tmp.name, contenu=carte(body=_corps(100)))
        loaded = canon.load(clone)
        mesure = canon.process_block(loaded)
        self.assertFalse(mesure.cut)
        self.assertEqual(mesure.margin, canon.RULES_BLOC_LIMIT_COMPAT - mesure.size)
        cfg = types.SimpleNamespace(host="h")
        args = types.SimpleNamespace(json=False, host="h")
        out = io.StringIO()
        with mock.patch.object(canon_cli, "_recorded", return_value=None), \
                redirect_stdout(out):
            canon_cli._check_one(cfg, args, loaded)
        self.assertIn("process   : rules block %d/%d bytes, margin %d bytes"
                      % (mesure.size, mesure.limit, mesure.margin), out.getvalue())


if __name__ == "__main__":
    unittest.main()
