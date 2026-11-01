# SPDX-License-Identifier: AGPL-3.0-only
"""#5215 — `agent-mail show <id>` : le texte intégral d'un courrier reçu.

La consigne d'un tour est bornée : un message long y est tronqué et renvoyait
au fil, fichier local par hôte — illisible depuis un autre hôte. Le message
vit en base ; `show` le rend en entier à son destinataire (ou à une copie),
sans le marquer lu, et refuse un tiers.

Postgres réel.
"""
from __future__ import annotations

from ameesh import mail, registry
from ameesh.backend import PgBackend

from .support import PgTestCase


class MailShowTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.upsert(self.db, "beta", harness="codex", host="autre")
        registry.upsert(self.db, "gamma", harness="deepseek", host="autre")

    def _row(self, message_id) -> dict:
        rows = self.db.query(
            "SELECT recipient, delivered_at, status FROM agent_mailbox WHERE id = %s",
            (int(message_id),))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _show(self, message_id, name: str, **extra: str):
        extra = dict(extra, AGENT_MAIL_NAME=name)
        return self.cli("show", str(message_id), env=self.env(**extra))

    def test_un_message_long_se_lit_en_entier_sans_marquer_lu(self):
        # Le fil n'existe pas : seul le corps en base peut rendre la fin.
        body = "début\n" + "x" * 30000 + "\nfin-5215"
        message_id = mail.send(self.db, "alpha", "beta", body, thread=False)

        proc = self._show(message_id, "beta")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fin-5215", proc.stdout)
        self.assertGreaterEqual(proc.stdout.count("x"), 30000)
        row = self._row(message_id)
        self.assertIsNone(row["delivered_at"], "show a marqué le message lu")
        self.assertEqual(row["status"], "pending")

    def test_un_tiers_est_refuse_sans_fuite(self):
        marqueur = "SECRET-5215"
        message_id = mail.send(self.db, "alpha", "beta", "corps " + marqueur,
                               thread=False)
        # ni un autre destinataire, ni l'expéditeur ne lisent la ligne de beta
        for name in ("gamma", "alpha"):
            with self.subTest(name=name):
                proc = self._show(message_id, name)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("no message #%d" % message_id, proc.stderr)
                self.assertNotIn(marqueur, proc.stdout + proc.stderr)
        self.assertIsNone(self._row(message_id)["delivered_at"])

    def test_une_copie_lit_sa_propre_ligne(self):
        backend = PgBackend(self.cfg, self.db)
        backend.send("alpha", "beta", "corps copie 5215", host="laptop",
                     cc=["gamma"])
        primary = self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = 'beta'")[0]["id"]
        copy = self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = 'gamma'"
            " AND payload ->> 'cc' = 'beta'")[0]["id"]

        proc = self._show(copy, "gamma")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("corps copie 5215", proc.stdout)
        self.assertIsNone(self._row(copy)["delivered_at"])
        # le numéro du destinataire principal n'est pas celui de la copie
        refuse = self._show(primary, "gamma")
        self.assertEqual(refuse.returncode, 1)

    def test_la_troncature_renvoie_a_la_commande(self):
        message_id = mail.send(self.db, "alpha", "beta",
                               "z" * 5000 + " FIN-5215", thread=False)
        row = mail.get(self.db, message_id)

        texte, retenus, reste = mail.prompt_for(
            self.db, [row], kind="mail", budget=2500)

        self.assertEqual([r["id"] for r in retenus], [message_id])
        self.assertEqual(reste, 0)
        self.assertIn("agent-mail show %d" % message_id, texte)
        self.assertNotIn("full text in the thread", texte)
        self.assertNotIn("FIN-5215", texte)

    def test_identite_non_liee_est_refusee(self):
        message_id = mail.send(self.db, "alpha", "beta", "corps", thread=False)
        proc = self.cli("show", str(message_id), env=self.env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unbound identity", proc.stderr)

    def test_le_repli_fichier_n_a_pas_de_numero(self):
        proc = self.cli("show", "1", env=self.env(
            AGENT_MAIL_NAME="beta", AMEESH_BACKEND="file"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("requires the database", proc.stderr)

    def test_numero_invalide_ou_inconnu(self):
        for value in ("abc", "0", "-1"):
            with self.subTest(value=value):
                proc = self._show(value, "beta")
                self.assertEqual(proc.returncode, 2, proc.stderr)
        proc = self._show(999999, "beta")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no message #999999", proc.stderr)

    def test_l_aide_et_ameesh_mail_show(self):
        aide = self.cli("--help")
        self.assertEqual(aide.returncode, 0)
        self.assertIn("agent-mail show <id>", aide.stdout)

        message_id = mail.send(self.db, "alpha", "beta", "par ameesh mail",
                               thread=False)
        proc = self.mesh("mail", "show", str(message_id),
                         env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("par ameesh mail", proc.stdout)
