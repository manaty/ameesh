# SPDX-License-Identifier: AGPL-3.0-only
"""#5099 : le courrier reste livrable avant les migrations additives."""
import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ameesh import cli, db, mail, registry
from ameesh.backend import PgBackend
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


class MigrationDiagnosticTest(unittest.TestCase):
    def test_sqlstate_transmis_sans_dependre_du_texte_localise(self):
        database = mock.Mock()
        error = db.DbError("verrou indisponible", sqlstate="55P03")
        output = io.StringIO()
        with mock.patch.object(cli.db_mod, "connect", return_value=database), \
                mock.patch.object(cli.migrations, "migrate", side_effect=error), \
                redirect_stderr(output):
            self.assertEqual(cli.cmd_migrate(mock.Mock()), 1)
        self.assertIn("[SQLSTATE 55P03]", output.getvalue())
        database.close.assert_called_once()
