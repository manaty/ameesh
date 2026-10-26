# SPDX-License-Identifier: AGPL-3.0-only
"""#3693 — un argument réduit à « - » après nettoyage n'est pas un corps.

`agent-mail send <dest> " - "` échappait au marqueur d'entrée standard
(comparé à l'argument exact) puis était livré comme un courrier « - ».
Le corps nettoyé égal au marqueur est refusé avant tout dépôt.

Postgres réel.
"""
from __future__ import annotations

from ameesh import registry

from .support import PgTestCase


class SendMarqueurBlancsTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host)

    def _writes(self) -> tuple[int, int]:
        """Témoin d'écriture : courriers déposés et index des fils."""
        rows = self.db.query(
            "SELECT (SELECT count(*) FROM agent_mailbox) AS mails, "
            "(SELECT count(*) FROM thread_index) AS threads")
        return int(rows[0]["mails"]), int(rows[0]["threads"])

    def test_marqueur_entoure_de_blancs_est_refuse_sans_ecriture(self):
        for body in (" - ", "  -", "-  ", "\t-\n", " \t- \n", "   -   ",
                     "\u00a0-\u00a0"):
            with self.subTest(body=body):
                proc = self.cli("send", "alpha", body,
                                env=self.env(AGENT_MAIL_NAME="beta"))
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn("just `-`", proc.stderr)
                self.assertEqual(self._writes(), (0, 0))

    def test_marqueur_seul_lit_toujours_l_entree_standard(self):
        # le refus ne doit pas fermer le chemin stdin de #3504/#3683
        proc = self.cli("send", "alpha", "-",
                        env=self.env(AGENT_MAIL_NAME="beta"), stdin="corps\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bodies = [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'alpha'")]
        self.assertEqual(bodies, ["corps"])

    def test_corps_contenant_le_marqueur_reste_livre(self):
        proc = self.cli("send", "alpha", "  le marqueur - n'est pas un corps  ",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bodies = [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'alpha'")]
        self.assertEqual(bodies, ["le marqueur - n'est pas un corps"])
