# SPDX-License-Identifier: AGPL-3.0-only
"""#6534 : la garde d'`export-v0`.

L'incident du 09/11 12:03Z : un `export-v0` sans `--agents` a marqué remis
1 138 non-lus de 33 boîtes, dont les deux décisions owner ouvertes — leur
`delivered_at` les clôt « sans réponse ni retrait » — et deux désignations de
relecture. La garde exige un périmètre explicite, refuse un mesh vivant sans
geste nommé, ne marque jamais une demande de décision remise, et journalise
l'acteur et le nombre de lignes.
"""
from __future__ import annotations

import json
import os

from ameesh import mail

from .support import PgTestCase

AGENT = "deepseek7"
HUMAN = "human:alice"


class ExportV0GuardTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register(AGENT, "deepseek", cwd=self.tmp)
        self.state = os.path.join(self.tmp, "v0back")
        mail.send(self.db, "orchestrateur", AGENT, "premier", host=self.cfg.host)
        mail.send(self.db, "codex3", AGENT, "deuxième", host=self.cfg.host)
        mail.send(self.db, "bureau", HUMAN, "pour l'humain", host=self.cfg.host)
        self.question = "question ouverte ?"
        self.decision = mail.send(
            self.db, AGENT, HUMAN, self.question, kind="request",
            payload={"decision": {"state": "pending", "question": self.question}},
            host=self.cfg.host)

    def _lease(self) -> None:
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'test-runner',"
            " lease_expires_at = clock_timestamp() + interval '1 hour'"
            " WHERE name = %s", (AGENT,))

    def _event(self, after: int) -> dict:
        rows = self.db.query(
            "SELECT data FROM ameesh_events WHERE type = 'mail.export'"
            " AND id > %s ORDER BY id DESC LIMIT 1", (int(after),))
        self.assertTrue(rows, "export-v0 did not journal an event")
        return rows[0]["data"]

    def _decision_delivered(self) -> bool:
        return self.db.query(
            "SELECT delivered_at IS NOT NULL AS delivered FROM agent_mailbox"
            " WHERE id = %s", (self.decision,))[0]["delivered"]

    def test_perimetre_explicite_obligatoire(self):
        proc = self.mesh("export-v0", "--state", self.state)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--agents", proc.stderr)
        self.assertIn("--all", proc.stderr)
        # rien n'a bougé
        self.assertEqual(len(mail.unread(self.db, AGENT)), 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", AGENT)))

    def test_all_sur_un_mesh_vivant_refuse_sans_geste(self):
        self._lease()
        proc = self.mesh("export-v0", "--state", self.state, "--all")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("live executor", proc.stderr)
        self.assertIn(AGENT, proc.stderr)
        self.assertEqual(len(mail.unread(self.db, AGENT)), 2)
        self.assertFalse(self._decision_delivered())

        # un motif vide ne vaut pas geste explicite
        proc = self.mesh("export-v0", "--state", self.state, "--all",
                         "--allow-live-executors", "")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("live executor", proc.stderr)

        before = self.db.query(
            "SELECT coalesce(max(id), 0) AS id FROM ameesh_events")[0]["id"]
        proc = self.mesh("export-v0", "--state", self.state, "--all",
                         "--allow-live-executors", "rollback 1.46",
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(os.listdir(os.path.join(self.state, "inbox", AGENT))), 2)
        self.assertFalse(self._decision_delivered())
        data = self._event(before)
        self.assertEqual(data["actor"], AGENT)
        self.assertEqual(data["live_executors"], [AGENT])
        self.assertEqual(data["override"], "rollback 1.46")

    def test_la_decision_n_est_jamais_marquee_remise(self):
        before = self.db.query(
            "SELECT coalesce(max(id), 0) AS id FROM ameesh_events")[0]["id"]
        proc = self.mesh("export-v0", "--state", self.state, "--agents", HUMAN,
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("1 decision request(s) kept pending", proc.stdout)
        self.assertFalse(self._decision_delivered())
        # seule la question de la boîte humaine est retenue : le courrier
        # ordinaire, lui, est bien exporté
        files = sorted(os.listdir(os.path.join(self.state, "inbox", HUMAN)))
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.state, "inbox", HUMAN, files[0]), encoding="utf-8") as fh:
            exported = json.load(fh)
        self.assertEqual(exported["text"], "pour l'humain")
        self.assertIn("%s: 1 message(s) exported" % HUMAN, proc.stdout)
        data = self._event(before)
        self.assertEqual(data["exported"], 1)
        self.assertEqual(data["kept_decisions"], 1)
        self.assertFalse(data["all"])

    def test_all_exporte_toutes_les_boites_et_journalise(self):
        before = self.db.query(
            "SELECT coalesce(max(id), 0) AS id FROM ameesh_events")[0]["id"]
        proc = self.mesh("export-v0", "--state", self.state, "--all",
                         env=self.env(AGENT_MAIL_NAME=AGENT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("3 message(s) exported", proc.stdout)
        self.assertEqual(len(os.listdir(os.path.join(self.state, "inbox", AGENT))), 2)
        self.assertEqual(len(os.listdir(os.path.join(self.state, "inbox", HUMAN))), 1)
        self.assertFalse(self._decision_delivered())
        self.assertEqual(mail.unread(self.db, AGENT), [])
        self.assertEqual([row["body"] for row in mail.unread(self.db, HUMAN)],
                         [self.question])
        data = self._event(before)
        self.assertEqual(data["actor"], AGENT)
        self.assertEqual(data["boxes"], 2)
        self.assertEqual(data["exported"], 3)
        self.assertEqual(data["kept_decisions"], 1)
        self.assertTrue(data["all"])
