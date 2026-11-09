# SPDX-License-Identifier: AGPL-3.0-only
"""#6424 : les décomptes de non-lus excluent le courrier des agents externes.

Un agent en mode `externe` (session humaine tenue hors exécuteur — bureau,
veille) n'a aucun runner pour solder sa boîte : ses messages non remis
pesaient dans chaque décompte (7 380 pour le bureau au 09/11). L'exclusion ne
touche jamais l'état du courrier (`delivered_at` reste NULL) : elle ne vaut
que pour les lectures de décompte et de service.
"""
from __future__ import annotations

from ameesh import mail, registry, storage

from .support import PgTestCase


class UnreadExternalTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "runner", harness="deepseek", host=self.cfg.host)
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host)
        registry.set_mode(self.db, "bureau", "externe")
        mail.send(self.db, "human:alice", "runner", "for runner", host=self.cfg.host)
        mail.send(self.db, "human:alice", "bureau", "for bureau", host=self.cfg.host)
        mail.send(self.db, "human:alice", "bureau", "for bureau 2", host=self.cfg.host)
        # une boîte humaine n'est pas externe : elle reste comptée
        mail.send(self.db, "runner", "human:alice", "for the owner", host=self.cfg.host)

    def _overview_unread(self, name):
        rows = {row["name"]: row for row in storage.of(self.db).agents.overview()}
        return int(rows[name]["unread"])

    def test_mailbox_counts_skip_external_boxes(self):
        box = storage.of(self.db).mailbox
        self.assertEqual(box.unread_total(), 2)
        self.assertEqual(set(box.pending_recipients()), {"human:alice", "runner"})
        # `pending_recipients_sorted` n'est pas un décompte : c'est la liste
        # du retour arrière v0 (`export-v0`), qui doit rendre TOUTES les
        # boîtes, y compris externes, avant de les marquer remises.
        self.assertEqual(box.pending_recipients_sorted(),
                         ["bureau", "human:alice", "runner"])

    def test_overview_board_and_progress_skip_external_boxes(self):
        self.assertEqual(self._overview_unread("bureau"), 0)
        self.assertEqual(self._overview_unread("runner"), 1)

        agents = {a["name"]: a for a in
                  storage.of(self.db).projects.board(max_lots=50)["agents"]}
        self.assertEqual(int(agents["bureau"]["unread"]), 0)
        self.assertEqual(int(agents["runner"]["unread"]), 1)

        rows = {r["name"]: r for r in storage.of(self.db).progress.agents(None)}
        self.assertEqual(int(rows["bureau"]["unread"]), 0)
        self.assertEqual(int(rows["runner"]["unread"]), 1)

    def test_the_mail_state_is_unchanged(self):
        # L'exclusion ne remet rien : le destinataire externe retrouve tout son
        # courrier non remis, et seul un décompte l'ignore.
        self.assertEqual(len(mail.unread(self.db, "bureau")), 2)
        rows = self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox"
            " WHERE recipient = 'bureau' AND delivered_at IS NULL")
        self.assertEqual(rows[0]["n"], 2)

    def test_an_external_box_is_never_a_dead_letter(self):
        # la boîte humaine (destinataire inconnu du registre) est une autre
        # affaire, filtrée par `undeliverable.without_human_boxes` : on l'ôte
        # pour ne garder que le témoin de souffrance.
        self.db.execute("DELETE FROM agent_mailbox WHERE recipient = 'human:alice'")
        registry.upsert(self.db, "stoppe", harness="deepseek", host=self.cfg.host,
                        status="stopped")
        mail.send(self.db, "human:alice", "stoppe", "dead", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET status = 'stopped'"
                        " WHERE name = 'bureau'")
        letters = storage.of(self.db).mailbox.dead_letters()
        self.assertEqual([(r["recipient"], int(r["n"])) for r in letters],
                         [("stoppe", 1)])
