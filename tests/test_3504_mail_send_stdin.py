# SPDX-License-Identifier: AGPL-3.0-only
"""#3504 — `agent-mail send <dest> -` lit le corps sur l'entrée standard.

Constat (25/10 07:38Z, docs-01) : un corps composé entre guillemets doubles a
fait exécuter par le shell la commande qu'il citait entre backticks (table #13
publiée, rafale d'environ 70 courriers). `send <dest> -` lit le corps
sur l'entrée standard ; le heredoc à délimiteur entre apostrophes (`<<'EOF'`)
n'interprète rien, et le corps arrive verbatim. Un corps passé en arguments
qui contient la confirmation d'une commande ameesh est signalé.

Postgres réel.
"""
from __future__ import annotations

import os
import subprocess
import sys

from ameesh import adapters, registry

from .support import PgTestCase, REPO


class SendStdinTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host)

    def _bodies(self) -> list[str]:
        return [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'alpha' ORDER BY id")]

    def test_corps_lu_sur_l_entree_standard_verbatim(self):
        body = ("Le lot #13 n'était pas à publier.\n"
                "Commande citée : `ameesh work close 12 --abandoned` et $(touch /tmp/x).\n"
                "Fin.")
        proc = self.cli("send", "alpha", "-", "--kind", "event",
                        env=self.env(AGENT_MAIL_NAME="beta"), stdin=body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "delivered to alpha")
        self.assertEqual(self._bodies(), [body.strip()])

    def test_heredoc_entre_apostrophes_ne_lance_rien(self):
        temoin = os.path.join(self.tmp, "pwned")
        script = (
            "%s -m ameesh.cli send alpha - <<'EOF'\n"
            "La table #13 ne se publie pas ; commande citée :\n"
            "`touch %s` et $(touch %s)\n"
            "EOF\n" % (sys.executable, temoin, temoin)
        )
        proc = subprocess.run(["bash", "-c", script], cwd=self.tmp,
                              env=self.env(AGENT_MAIL_NAME="beta"),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("delivered to alpha", proc.stdout)
        self.assertFalse(os.path.exists(temoin),
                         "le shell a exécuté la commande citée")
        self.assertEqual(self._bodies(), [
            "La table #13 ne se publie pas ; commande citée :\n"
            "`touch %s` et $(touch %s)" % (temoin, temoin)])

    def test_entree_vide_est_refusee(self):
        proc = self.cli("send", "alpha", "-", env=self.env(AGENT_MAIL_NAME="beta"),
                        stdin=" \n\t\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("empty body on standard input", proc.stderr)
        self.assertEqual(self._bodies(), [])

    def test_melange_du_marqueur_avec_le_texte_est_refuse(self):
        proc = self.cli("send", "alpha", "-", "suite",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("standard input", proc.stderr)
        self.assertEqual(self._bodies(), [])

    def test_un_corps_en_arguments_qui_porte_une_sortie_de_commande_est_signale(self):
        proc = self.cli("send", "alpha", "delivered to alpha\ncompte rendu",
                        env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("standard input", proc.stderr)
        # l'avertissement ne refuse rien : le message part quand même
        self.assertEqual(len(self._bodies()), 1)

    def test_la_consigne_du_tour_recommande_l_entree_standard(self):
        self.assertIn("agent-mail send <name> -", adapters.MAIL_FOOTER)

    def test_les_consignes_des_agents_recommandent_le_heredoc_quote(self):
        for chemin in ("src/ameesh/consignes/veille.md", "docs/AUDITEUR.md",
                       "docs/ORCHESTRATEUR.md", "docs/EXPLOITATION.md"):
            with self.subTest(consigne=chemin):
                with open(os.path.join(REPO, chemin), encoding="utf-8") as fh:
                    texte = fh.read()
                self.assertIn("<<'EOF'", texte)
                self.assertIn("agent-mail send", texte)
