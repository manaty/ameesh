# SPDX-License-Identifier: AGPL-3.0-only
"""#3683 — un texte réduit à « - » n'est jamais enregistré.

Constat (26/10 07:07 et 07:17Z) : `ameesh work note <lot> - <<'EOF'` a
enregistré quatre notes « - » (le texte était positionnel, l'entrée standard
jamais lue) ; `git format-patch … --stdout | agent-mail send <dest> -` a livré
un corps « - » (#142626, hôte encore sans #3504). `work note <lot> -` lit
désormais l'entrée standard comme `send` ; une entrée vide, ou réduite à
« - », est refusée des deux côtés sans rien écrire, et `work.note` refuse la
note « - » quel que soit l'appelant.

Postgres réel.
"""
from __future__ import annotations

import subprocess
import sys

from ameesh import registry, work

from .support import PgTestCase


class WorkNoteStdinTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("dev-a", "claude")
        self.lot = work.add(self.db, title="note sur l'entrée standard",
                            assignee="dev-a")

    def _notes(self) -> list[str]:
        return [event.get("note") or ""
                for event in work.events(self.db, self.lot["id"])]

    def _note(self, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.main", "work", "note",
             str(self.lot["id"]), "-"],
            input=stdin, capture_output=True, text=True, timeout=60,
            env=self.env(AGENT_MAIL_NAME="dev-a"), cwd=self.tmp)

    def test_tiret_lit_la_note_sur_l_entree_standard(self):
        text = "Relu : `ameesh work close 12` cité, $(touch x) non lancé.\nFin."
        proc = self._note(text + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("note added to lot #%d" % self.lot["id"], proc.stdout)
        self.assertIn(text, self._notes())
        self.assertNotIn("-", self._notes())

    def test_entree_vide_ou_reduite_au_tiret_est_refusee(self):
        for stdin in ("", " \n\t\n", "-\n"):
            with self.subTest(stdin=stdin):
                before = self._notes()
                proc = self._note(stdin)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("standard input", proc.stderr)
                self.assertEqual(self._notes(), before)

    def test_work_note_refuse_le_tiret_seul_quel_que_soit_l_appelant(self):
        for text in ("-", "  -\n"):
            with self.subTest(text=text):
                with self.assertRaises(work.WorkError) as raised:
                    work.note(self.db, self.lot["id"], text, actor="dev-a")
                self.assertIn("`-`", str(raised.exception))
        self.assertNotIn("-", self._notes())


class SendStdinTiretTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host)

    def _bodies(self) -> list[str]:
        return [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'alpha' ORDER BY id")]

    def test_commande_amont_en_echec_ne_livre_rien(self):
        # la commande en amont du tube échoue : l'entrée standard est vide
        script = "false | %s -m ameesh.cli send alpha -" % sys.executable
        proc = subprocess.run(["bash", "-c", script], cwd=self.tmp,
                              env=self.env(AGENT_MAIL_NAME="beta"),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("empty body on standard input", proc.stderr)
        self.assertEqual(self._bodies(), [])

    def test_corps_reduit_au_tiret_est_refuse(self):
        proc = self.cli("send", "alpha", "-", env=self.env(AGENT_MAIL_NAME="beta"),
                        stdin="-\n")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("just `-`", proc.stderr)
        self.assertEqual(self._bodies(), [])
