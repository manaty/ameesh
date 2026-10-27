# SPDX-License-Identifier: AGPL-3.0-only
"""#4023 — `--note -` de `work move`, `merged` et `milestone` lit l'entrée standard.

Constat (27/10 17:12Z) : `work move 4020 qa --note - <<'EOF'` a enregistré
« - » au journal du lot ; le texte du heredoc était perdu. #3683 (`work note`)
et #3744 (`work add --body`) lisaient déjà l'entrée standard, pas l'option
`--note` des transitions. Une entrée vide, ou réduite à « - », est refusée
avant toute écriture.

Postgres réel.
"""
from __future__ import annotations

import subprocess
import sys

from ameesh import work

from .support import PgTestCase


class NoteTiretStdinTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("dev-a", "claude")
        self.lot = work.add(self.db, title="note de transition par heredoc",
                            assignee="dev-a")

    def _notes(self) -> list[str]:
        return [event.get("note") or ""
                for event in work.events(self.db, self.lot["id"])]

    def _work(self, *argv: str, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.main", "work", *argv],
            input=stdin, capture_output=True, text=True, timeout=60,
            env=self.env(AGENT_MAIL_NAME="dev-a"), cwd=self.tmp)

    def test_move_note_tiret_lit_l_entree_standard(self):
        text = "Gel : `ameesh work close 12` cité, $(touch x) non lancé.\nFin."
        proc = self._work("move", str(self.lot["id"]), "build", "--note", "-",
                          stdin=text + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "build")
        self.assertIn(text, self._notes())
        self.assertNotIn("-", self._notes())

    def test_move_entree_vide_ou_reduite_au_tiret_ne_transite_pas(self):
        for stdin in ("", " \n\t\n", "-\n"):
            with self.subTest(stdin=stdin):
                before = self._notes()
                proc = self._work("move", str(self.lot["id"]), "build",
                                  "--note", "-", stdin=stdin)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("standard input", proc.stderr)
                self.assertEqual(work.get(self.db, self.lot["id"])["state"], "intake")
                self.assertEqual(self._notes(), before)

    def test_merged_et_milestone_lisent_l_entree_standard_avant_d_ecrire(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        for argv in (("merged", str(self.lot["id"]), "--sha", sha, "--note", "-"),
                     ("milestone", str(self.lot["id"]), "frozen", "--sha", sha,
                      "--note", "-")):
            with self.subTest(command=argv[0]):
                before = self._notes()
                proc = self._work(*argv, stdin="-\n")
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("standard input", proc.stderr)
                self.assertEqual(work.get(self.db, self.lot["id"])["state"], "intake")
                self.assertEqual(self._notes(), before)
