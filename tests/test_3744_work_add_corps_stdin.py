# SPDX-License-Identifier: AGPL-3.0-only
"""#3744 — `ameesh work add --body -` lit l'énoncé sur l'entrée standard.

Constat (26/10 11:56Z) : `work add --body - <<'EOF'` a créé #3741 avec le
corps « - » ; l'énoncé du heredoc était perdu. #3683 avait corrigé `work note`
et `send`, pas `work add`. Une entrée vide, ou réduite à « - », est refusée
avant toute création, et `work.add` refuse le corps « - » quel que soit
l'appelant.

Postgres réel.
"""
from __future__ import annotations

import subprocess
import sys

from ameesh import work

from .support import PgTestCase


class WorkAddBodyStdinTest(PgTestCase):
    def _titles(self) -> list[str]:
        return [item["title"] for item in work.list_items(self.db)]

    def _add(self, title: str, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.main", "work", "add", "--title", title,
             "--estimate", "30m", "--body", "-"],
            input=stdin, capture_output=True, text=True, timeout=60,
            env=self.env(), cwd=self.tmp)

    def test_tiret_lit_le_corps_sur_l_entree_standard(self):
        text = "Constat : `ameesh work close 12` cité, $(touch x) non lancé.\nFin."
        proc = self._add("corps par heredoc", text + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        [item] = [i for i in work.list_items(self.db) if i["title"] == "corps par heredoc"]
        self.assertEqual(work.get(self.db, item["id"])["body"], text)

    def test_entree_vide_ou_reduite_au_tiret_ne_cree_rien(self):
        for stdin in ("", " \n\t\n", "-\n"):
            with self.subTest(stdin=stdin):
                proc = self._add("refusé %r" % stdin, stdin)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("standard input", proc.stderr)
                self.assertNotIn("refusé %r" % stdin, self._titles())

    def test_work_add_refuse_le_corps_tiret_quel_que_soit_l_appelant(self):
        for body in ("-", "  -\n"):
            with self.subTest(body=body):
                with self.assertRaises(work.WorkError) as raised:
                    work.add(self.db, title="corps %r" % body, body=body)
                self.assertIn("`-`", str(raised.exception))
                self.assertNotIn("corps %r" % body, self._titles())
