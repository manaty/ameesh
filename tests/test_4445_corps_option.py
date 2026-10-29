# SPDX-License-Identifier: AGPL-3.0-only
"""#4445 — un corps qui commence par une option inconnue n'est pas livré.

`agent-mail send <dest> --stdin <<'EOF'` livrait un courrier « --stdin »
(l'option n'existe pas, l'entrée standard s'écrit `-`) et réveillait le
destinataire pour rien. Le corps dont le premier mot ressemble à une option
longue est refusé avant tout dépôt ; `--` devant le corps l'envoie tel quel.

Postgres réel.
"""
from __future__ import annotations

from ameesh import registry

from .support import PgTestCase


class SendCorpsOptionTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host)

    def _writes(self) -> tuple[int, int]:
        rows = self.db.query(
            "SELECT (SELECT count(*) FROM agent_mailbox) AS mails, "
            "(SELECT count(*) FROM thread_index) AS threads")
        return int(rows[0]["mails"]), int(rows[0]["threads"])

    def _bodies(self) -> list[str]:
        return [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'alpha' ORDER BY id")]

    def test_option_inconnue_comme_corps_est_refusee_sans_ecriture(self):
        for body in (["--stdin"], ["--stdin", "suite"], ["--body=texte"],
                     ["--stdin la suite"]):
            with self.subTest(body=body):
                proc = self.cli("send", "alpha", *body,
                                env=self.env(AGENT_MAIL_NAME="beta"),
                                stdin="corps\n")
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn("unknown option", proc.stderr)
                self.assertIn("`-`", proc.stderr)
                self.assertEqual(self._writes(), (0, 0))

    def test_marqueur_stdin_passe(self):
        proc = self.cli("send", "alpha", "-",
                        env=self.env(AGENT_MAIL_NAME="beta"), stdin="corps\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._bodies(), ["corps"])

    def test_double_tiret_envoie_le_corps_tel_quel(self):
        proc = self.cli("send", "alpha", "--", "--stdin", "n'existe pas",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._bodies(), ["--stdin n'existe pas"])

    def test_option_en_milieu_de_prose_reste_livree(self):
        proc = self.cli("send", "alpha", "utilise", "-", "pas", "--stdin",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        # `-` mêlé au texte reste refusé (#3504) ; sans lui, la prose passe
        self.assertEqual(proc.returncode, 2)
        proc = self.cli("send", "alpha", "l'option --stdin n'existe pas",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._bodies(), ["l'option --stdin n'existe pas"])

    def test_options_connues_restent_reconnues(self):
        proc = self.cli("send", "alpha", "--info", "rien à faire",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._bodies(), ["rien à faire"])

    def test_options_apres_le_double_tiret_restent_dans_le_corps(self):
        # B de ameesh-cx3 (gel #24841) : les options étaient extraites de toute
        # la ligne avant `--` ; `--urgent`, `--cc`, `--lot` cités devenaient
        # des gestes. Correctif de relecteur d5811e412 réécrit par l'auteur.
        body = ["--stdin", "--urgent", "--cc", "absent", "--lot", "invalid!", "-"]
        proc = self.cli("send", "alpha", "--info", "--", *body,
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._bodies(), [" ".join(body)])
        self.assertEqual(int(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox")[0]["n"]), 1)

    def test_double_tiret_avant_le_destinataire_suit_posix(self):
        # `--` clôt les options ; destinataire et corps restent positionnels
        proc = self.cli("send", "--", "alpha", "--urgent",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._bodies(), ["--urgent"])
