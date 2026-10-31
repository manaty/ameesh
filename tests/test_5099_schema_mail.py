# SPDX-License-Identifier: AGPL-3.0-only
"""#5099 : le courrier reste livrable avant les migrations additives."""
import contextlib
import io
import os
from unittest.mock import patch

from ameesh import cli, mail, registry, work
from ameesh.backend import PgBackend
from ameesh.storage.postgres.registry import GET_SQL
from .support import PgTestCase


class SchemaMailTest(PgTestCase):
    def test_courrier_avec_colonnes_de_la_tete_absentes(self):
        registry.upsert(self.db, "sender", status="idle", harness="codex")
        registry.upsert(self.db, "pilot", status="idle", harness="claude")
        # DDL uniquement dans le schéma isolé du banc. Une colonne normale et
        # une date : aucune projection de get() ne doit nommer une absente.
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN review_hold")
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN operator_pause_until")
        try:
            row = registry.get(self.db, "sender")
            self.assertIsNone(row["review_hold"])
            self.assertIsNone(row["operator_pause_until_ts"])
            self.assertIsInstance(row["created_ts"], float)
            body = "Migration 0529 non posée : verrou indisponible, aucun geste."
            self.assertEqual(cli.cmd_send(PgBackend(self.cfg, self.db), self.cfg,
                                         ["pilot", body, "--from", "sender"]), 0)
            messages = mail.unread(self.db, "pilot")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["body"], body)
            after = registry.get(self.db, "sender")
            self.assertGreater(after["last_mail_ts"], 0)
            self.assertIn("last_mail_host", after)
            self.assertNotIn("created_at", after)
        finally:
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN review_hold jsonb")
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN operator_pause_until timestamptz")

    def test_alerte_du_pilote_avec_lot_libre(self):
        registry.upsert(self.db, "sender", status="idle", harness="codex")
        registry.upsert(self.db, "pilot", status="idle", harness="claude")
        item = work.add(self.db, title="Échec de pose", actor="sender",
                        estimate=30, cfg=self.cfg)
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN review_hold")
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN operator_pause_until")
        try:
            body = "Migration non posée : aucun geste."
            stderr = io.StringIO()
            with patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": "pilot"}), \
                    contextlib.redirect_stderr(stderr):
                result = cli.cmd_send(PgBackend(self.cfg, self.db), self.cfg,
                                      ["sender", body, "--from", "pilot",
                                       "--lot", str(item["id"])])
            self.assertEqual(result, 0)
            messages = mail.unread(self.db, "sender")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["body"], body)
            self.assertEqual(str(messages[0]["work_item_id"]), str(item["id"]))
            self.assertIsNone(work.get(self.db, item["id"])["assignee"])
            self.assertIn("assignment check unavailable (schema not aligned)",
                          stderr.getvalue())
        finally:
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN review_hold jsonb")
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN operator_pause_until timestamptz")

    def test_diffusion_avec_colonnes_de_la_tete_absentes(self):
        # #5101 : la diffusion lit le registre par `overview()` ; elle ne doit
        # pas nommer une colonne additive absente avant l'alignement du schéma.
        registry.upsert(self.db, "sender", status="idle", harness="codex")
        registry.upsert(self.db, "pilot", status="idle", harness="claude")
        registry.upsert(self.db, "other", status="idle", harness="deepseek")
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN review_hold")
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN operator_pause_until")
        try:
            rows = {row["name"]: row for row in registry.overview(self.db)}
            self.assertEqual(set(rows), {"sender", "pilot", "other"})
            self.assertIsNone(rows["pilot"]["review_hold"])
            self.assertIsNone(rows["pilot"]["operator_pause_until_ts"])
            self.assertFalse(rows["pilot"]["operator_paused"])
            body = "Diffusion avant migration : le courrier part quand même."
            self.assertEqual(cli.cmd_send(PgBackend(self.cfg, self.db), self.cfg,
                                         ["all", body, "--from", "sender"]), 0)
            for name in ("pilot", "other"):
                messages = mail.unread(self.db, name)
                self.assertEqual([m["body"] for m in messages], [body], name)
            self.assertEqual(mail.unread(self.db, "sender"), [])
        finally:
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN review_hold jsonb")
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN operator_pause_until timestamptz")

    def test_get_evalue_le_jsonb_une_fois(self):
        # #5101 : `GET_COLUMNS` projetait `to_jsonb(agent_registry)` dans
        # chaque date (17 évaluations par ligne) ; la latérale le fait une fois.
        self.assertEqual(GET_SQL.count("to_jsonb(agent_registry)"), 1)
        self.assertIn("CROSS JOIN LATERAL", GET_SQL)
