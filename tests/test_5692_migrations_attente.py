# SPDX-License-Identifier: AGPL-3.0-only
"""#5692 : migrations non additives en attente, signalées avant tout staging.

Le 03/11 23:42Z, la séquence d4cc43a1 a échoué en migrations sur l'ancienne
base de pc-smichea (0518 non additive, jamais appliquée) après 38 min de
préflight et de sauvegarde. Rien ne l'avait dit : `doctor` n'affichait qu'un
compteur, `require_compatible_writer` ne garde que la base en avance, et
`config-ancienne-base.json` n'était sondée par aucune alerte. Ici : la
détection en lecture seule, le WARN du doctor, le KO de la sonde, l'alerte par
base configurée et le dry-run du préflight (`poste.sh prevol`).
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import config, exploitation, migration_alerts, migrations, notify, pgbouncer_local

from .support import PgTestCase

SWITCH = 518


class PendingNonAdditiveTest(PgTestCase):
    """Le code en avance sur la base : `--additive-only` refusera."""

    def setUp(self):
        super().setUp()
        self.switch = next(m for m in migrations.discover() if m.version == SWITCH)
        # La base du banc a reçu 0518 à la migration du gabarit : la retirer du
        # registre la rend « en attente » sans toucher au schéma (aucune
        # migration n'est rejouée).
        self.db.execute("DELETE FROM schema_migrations WHERE version = %s", (SWITCH,))
        self.addCleanup(self._restore)

    def _restore(self):
        self.db.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s,%s,%s) "
            "ON CONFLICT (version) DO NOTHING",
            (self.switch.version, self.switch.name, self.switch.checksum))

    def test_pending_non_additive_lists_the_switch_with_its_reason(self):
        pending = migrations.pending_non_additive(self.db)
        self.assertEqual([m.label for m, _reasons in pending],
                         ["0518_generic_delivery_state"])
        self.assertIn("marked non-additive", pending[0][1])
        self.assertIn("0518_generic_delivery_state",
                      migrations.describe_pending_non_additive(pending))

    def test_an_additive_pending_migration_is_not_a_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "9998_ajout.sql"), "w",
                      encoding="utf-8") as stream:
                stream.write("alter table work_items add column if not exists "
                             "t_5692_additif text;")
            self.assertEqual(migrations.pending_non_additive(self.db, directory), [])

    def test_doctor_warns_and_the_probe_is_ko(self):
        out = self.mesh("doctor", timeout=180)
        self.assertIn("migrations : WARNING", out.stdout)
        self.assertIn("0518_generic_delivery_state", out.stdout)
        self.assertIn("--additive-only", out.stdout)
        probe = self.mesh("doctor", "--probe")
        self.assertEqual(probe.returncode, 1, probe.stdout + probe.stderr)
        self.assertIn("non-additive", probe.stdout)
        self.assertIn("0518_generic_delivery_state", probe.stdout)

    def test_migrate_dry_run_additive_only_gives_the_guard_verdict(self):
        out = self.mesh("migrate", "--dry-run", "--additive-only")
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("0518_generic_delivery_state: NOT additive", out.stdout)
        self.assertIn("nothing applied (--additive-only)", out.stderr)
        # Le diagnostic seul garde son code 0 et n'écrit rien.
        before = migrations.applied(self.db)
        out = self.mesh("migrate", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("0518_generic_delivery_state: NOT additive", out.stdout)
        self.assertEqual(migrations.applied(self.db), before)

    def test_primary_alert_is_one_stable_alert(self):
        first = migration_alerts.primary(self.cfg, self.db)
        second = migration_alerts.primary(self.cfg, self.db)
        self.assertEqual(len(first), 1, first)
        alert = first[0]
        self.assertEqual(alert["type"], "migration_pending_non_additive")
        self.assertEqual(alert["base"], migration_alerts._base_label(self.cfg))
        self.assertEqual(alert["value"], 1)
        self.assertIn("0518_generic_delivery_state", alert["detail"])
        self.assertEqual([m["label"] for m in alert["migrations"]],
                         ["0518_generic_delivery_state"])
        # La clé ne bouge pas d'un passage à l'autre : une alerte qui dure.
        self.assertEqual(exploitation.alert_key(alert),
                         exploitation.alert_key(second[0]))
        self.assertIn("migration_pending_non_additive", exploitation.ALERT_TYPES)
        self.assertIn("migration_pending_non_additive", notify.DEFAULT_TYPES)
        self.assertIn("migration_pending_non_additive", notify.TYPE_LABELS)

    def test_alert_is_absent_without_a_pending_switch(self):
        self._restore()
        self.assertEqual(migration_alerts.primary(self.cfg, self.db), [])


class SecondaryBaseTest(PgTestCase):
    """`config-ancienne-base.json` est une AUTRE base, pas un profil."""

    def setUp(self):
        super().setUp()
        self.ancienne = self.schema + "_ancienne"
        self.db.execute('CREATE SCHEMA "%s"' % self.ancienne)
        self.db.execute(
            'CREATE TABLE "%s".schema_migrations AS '
            'SELECT * FROM schema_migrations WHERE version <> %s'
            % (self.ancienne, SWITCH))
        self.addCleanup(self._drop_ancienne)
        self.dossier = tempfile.mkdtemp(prefix="t5692-config-")
        self.addCleanup(shutil.rmtree, self.dossier, True)
        self.primaire = os.path.join(self.dossier, "config.json")
        self.secondaire = os.path.join(self.dossier, "config-ancienne-base.json")
        for path, schema in ((self.primaire, self.schema),
                            (self.secondaire, self.ancienne)):
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"dsn": self.cfg.dsn, "schema": schema,
                           "host": "pc-smichea"}, stream)
        self.cfg2 = config.load_file(self.primaire)

    def _drop_ancienne(self):
        self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % self.ancienne)

    def test_only_the_known_sibling_config_is_a_base(self):
        for name in ("config.json.bak-20260101", "config-vieille.json",
                     "runner-profiles/config.json"):
            with open(os.path.join(self.dossier, name.replace("/", "-")), "w",
                      encoding="utf-8") as stream:
                stream.write("{}")
        self.assertEqual(config.secondary_configs(self.cfg2),
                         [("config-ancienne-base.json", self.secondaire)])
        self.assertEqual(config.secondary_configs(self.cfg), [])

    def test_load_file_does_not_take_the_callers_dsn_or_schema(self):
        with mock.patch.dict(os.environ, {
                "AMEESH_DSN": "postgresql://intrus@127.0.0.1:1/intrus",
                "AMEESH_SCHEMA": "schema_intrus"}):
            loaded = config.load_file(self.secondaire)
        self.assertEqual(loaded.dsn, self.cfg.dsn)
        self.assertEqual(loaded.schema, self.ancienne)

    def test_local_alert_per_secondary_base(self):
        alerts, errors = migration_alerts.local(self.cfg2)
        self.assertEqual(errors, [])
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual(alert["type"], "host_migration_pending_non_additive")
        self.assertEqual(alert["config"], "config-ancienne-base.json")
        self.assertEqual(alert["host"], "pc-smichea")
        self.assertIn(self.ancienne, alert["base"])
        self.assertEqual([m["label"] for m in alert["migrations"]],
                         ["0518_generic_delivery_state"])
        self.assertIn("host_migration_pending_non_additive", notify.LOCAL_TYPES)
        self.assertIn("host_migration_pending_non_additive", notify.DEFAULT_TYPES)
        self.assertIn("host_migration_pending_non_additive", notify.TYPE_LABELS)

    def test_local_pass_raises_the_alert_for_a_secondary_base(self):
        """Le passage local du notifieur porte l'alerte de la base secondaire
        (elle n'est visible que de cet hôte, type LOCAL, gardé par `host`)."""
        cfg = dataclasses.replace(config.load_file(self.primaire), host="pc-smichea")
        ncfg = notify.parse_config({"default": ["desktop"]})
        state = notify.empty_state()
        notifier = notify.Notifier(cfg, ncfg, state=state, sender=mock.Mock(),
                                   log=lambda text: None, clock=lambda: 100.0)
        with mock.patch.object(pgbouncer_local, "probe", return_value=[]):
            notifier.run_local_pass(now=100.0)
        raised = [entry["alert"] for entry in state["active"].values()]
        self.assertEqual([a["type"] for a in raised],
                         ["host_migration_pending_non_additive"])
        self.assertEqual(raised[0]["host"], "pc-smichea")
        self.assertIn("config-ancienne-base.json", raised[0]["detail"])

    def test_unreadable_secondary_base_is_non_conclusive_not_resolved(self):
        os.unlink(self.secondaire)
        with open(self.secondaire, "w", encoding="utf-8") as stream:
            json.dump({"dsn": "postgresql://u:p@127.0.0.1:1/x"}, stream)
        alerts, errors = migration_alerts.local(self.cfg2)
        self.assertEqual(alerts, [])
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("config-ancienne-base.json", errors[0])


if __name__ == "__main__":
    unittest.main()
