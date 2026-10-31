# SPDX-License-Identifier: AGPL-3.0-only
"""#5099 : le courrier reste livrable avant les migrations additives."""
import contextlib
import io
import os
from unittest.mock import patch

from ameesh import cli, mail, registry, storage, work
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
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN harness_bin")
        try:
            rows = {row["name"]: row for row in registry.overview(self.db)}
            self.assertEqual(set(rows), {"sender", "pilot", "other"})
            self.assertIsNone(rows["pilot"]["review_hold"])
            self.assertIsNone(rows["pilot"]["operator_pause_until_ts"])
            self.assertIsNone(rows["pilot"]["harness_bin"])
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
            self.db.execute(
                "ALTER TABLE agent_registry ADD COLUMN operator_pause_until timestamptz")
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN harness_bin text")

    def test_overview_evalue_le_jsonb_une_fois(self):
        # #5101 (B1 auditeur) : `overview()` projetait `to_jsonb(agent_registry)`
        # dans chaque colonne absente de la vue (9 évaluations par ligne) et
        # faisait passer une lecture de ~0,2-0,8 s à ~1,8-2,0 s sur la base
        # commune ; une seule latérale par ligne, partagée, suffit.
        from ameesh.storage.postgres.registry import OVERVIEW_SQL
        self.assertEqual(OVERVIEW_SQL.count("to_jsonb(agent_registry)"), 1)
        self.assertIn("LEFT JOIN LATERAL", OVERVIEW_SQL)
        self.assertIn("OFFSET 0", OVERVIEW_SQL)

    def test_get_evalue_le_jsonb_une_fois(self):
        # #5101 : `GET_COLUMNS` projetait `to_jsonb(agent_registry)` dans
        # chaque date (17 évaluations par ligne) ; la latérale le fait une fois.
        self.assertEqual(GET_SQL.count("to_jsonb(agent_registry)"), 1)
        self.assertIn("CROSS JOIN LATERAL", GET_SQL)

    def test_reachability_avec_colonnes_de_la_tete_absentes(self):
        # #5101 (N1 auditeur) : la passe de redésignation lit `review_hold` et
        # `retired_at` ; sur un schéma en retard elle ne doit pas lever 42703.
        registry.upsert(self.db, "sender", status="idle", harness="codex")
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN review_hold")
        self.db.execute("ALTER TABLE agent_registry DROP COLUMN retired_at")
        try:
            rows = storage.of(self.db).agents.reachability()
            row = next(row for row in rows if row["name"] == "sender")
            self.assertIsNone(row["review_hold"])
            self.assertIsNone(row["retired_ts"])
        finally:
            self.db.execute("ALTER TABLE agent_registry ADD COLUMN review_hold jsonb")
            self.db.execute(
                "ALTER TABLE agent_registry ADD COLUMN retired_at timestamptz")
