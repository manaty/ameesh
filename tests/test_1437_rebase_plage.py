# SPDX-License-Identifier: AGPL-3.0-only
"""#1437 : contrôle des sujets sur la plage rejouée, sans borne arbitraire."""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from tests import test_1277_rebase_sujet as banc

_git, LANCEUR, GIT_ENV = banc._git, banc.LANCEUR, banc.GIT_ENV


class RebasePlageTest(banc.RebaseSujetTest):
    def _branches(self, lots=1, upstream=1):
        os.makedirs(self.depot)
        _git(self.depot, "init", "-q", "-b", "main")
        _git(self.depot, "commit", "--allow-empty", "-qm", "#111 base commune")
        _git(self.depot, "checkout", "-qb", "lot")
        for i in range(lots):
            Path(self.depot, "f%d.txt" % i).write_text(str(i))
            _git(self.depot, "add", ".")
            _git(self.depot, "commit", "-qm", "#900 lot %d" % i)
        _git(self.depot, "checkout", "-q", "main")
        for i in range(upstream):
            _git(self.depot, "commit", "--allow-empty", "-qm", "amont %d" % i)
        _git(self.depot, "checkout", "-q", "lot")

    def test_main_eloignee_ne_fait_pas_perdre_la_base(self):
        self._branches(upstream=110)
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "#900 lot 0")

    def test_plus_de_cent_commits_de_lot(self):
        self._branches(lots=105)
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_perte_au_dela_de_cinquante_commits(self):
        self._branches(lots=55)
        # L'éditeur supprime volontairement le premier sujet, plus ancien que
        # l'ancienne fenêtre de collecte de cinquante commits.
        editor = os.path.join(self.base, "sequence-editor.py")
        with open(editor, "w") as fh:
            fh.write("#!/usr/bin/env python3\nimport sys\np=sys.argv[1]\ns=open(p).read()\ns=s.replace('pick ', 'drop ', 1)\nopen(p,'w').write(s)\n")
        os.chmod(editor, 0o700)
        with mock.patch.dict(GIT_ENV, GIT_SEQUENCE_EDITOR=editor):
            proc = self._lancer("bash", LANCEUR, "-i", "main")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("#900 lot 0", proc.stderr)

    def test_onto_ne_controle_pas_les_ancetres_exclus(self):
        self._branches(lots=2)
        _git(self.depot, "branch", "nouvelle", "main")
        proc = self._lancer("bash", LANCEUR, "--onto", "nouvelle", "lot~1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "#900 lot 1")

    def test_branche_positionnelle(self):
        self._branches()
        _git(self.depot, "checkout", "-q", "main")
        proc = self._lancer("bash", LANCEUR, "main", "lot")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "#900 lot 0")

    def test_sans_argument_emploie_upstream_configure(self):
        self._branches()
        _git(self.depot, "branch", "--set-upstream-to=main", "lot")
        proc = self._lancer("bash", LANCEUR)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_onto_et_option_a_valeur_attachee(self):
        self._branches(lots=2)
        proc = self._lancer("bash", LANCEUR, "--exec", "true", "--onto=main", "lot~1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "#900 lot 1")

    def test_cherry_pick_amont_au_sujet_modifie_est_exclu(self):
        self._branches()
        original = _git(self.depot, "rev-parse", "HEAD").stdout.strip()
        _git(self.depot, "checkout", "-q", "main")
        _git(self.depot, "cherry-pick", original)
        _git(self.depot, "commit", "--amend", "-qm", "correctif repris dans main")
        _git(self.depot, "checkout", "-q", "lot")
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "correctif repris dans main")

    def test_fork_point_ne_controle_pas_amont_rebobine(self):
        self._branches()
        _git(self.depot, "checkout", "-q", "main")
        _git(self.depot, "commit", "--allow-empty", "-qm", "#777 ancien amont")
        ancien = _git(self.depot, "rev-parse", "HEAD").stdout.strip()
        _git(self.depot, "checkout", "-qb", "reprise")
        Path(self.depot, "r.txt").write_text("lot repris")
        _git(self.depot, "add", ".")
        _git(self.depot, "commit", "-qm", "#901 reprise")
        _git(self.depot, "update-ref", "refs/heads/main", ancien + "~1")
        _git(self.depot, "branch", "--set-upstream-to=main", "reprise")
        proc = self._lancer("bash", LANCEUR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("#777 ancien amont", _git(self.depot, "log", "--format=%s").stdout)
        self.assertEqual(self._sujet(), "#901 reprise")

    def test_root_et_branche_positionnelle(self):
        self._branches()
        _git(self.depot, "checkout", "-q", "main")
        proc = self._lancer("bash", LANCEUR, "--root", "lot")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "#900 lot 0")

    def test_fusion_omise_par_rebase_ordinaire(self):
        self._branches()
        _git(self.depot, "checkout", "-qb", "side", "main~1")
        Path(self.depot, "side.txt").write_text("side")
        _git(self.depot, "add", ".")
        _git(self.depot, "commit", "-qm", "side")
        _git(self.depot, "checkout", "-q", "lot")
        _git(self.depot, "merge", "--no-ff", "-qm", "#999 fusion", "side")
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("#999 fusion", _git(self.depot, "log", "--format=%s").stdout)

    def test_option_inconnue_refuse_avant_modification(self):
        self._branches()
        before = _git(self.depot, "rev-parse", "HEAD").stdout
        proc = self._lancer("bash", LANCEUR, "--unknown-range", "main")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(_git(self.depot, "rev-parse", "HEAD").stdout, before)

    def test_abort_ne_signale_pas_de_sujet_perdu(self):
        self._depot()
        self._conflit("bash", LANCEUR, "main")
        proc = self._lancer("bash", LANCEUR, "--abort")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "#900 sujet du lot")


if __name__ == "__main__":
    unittest.main()
