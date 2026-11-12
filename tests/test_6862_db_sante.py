# SPDX-License-Identifier: AGPL-3.0-only
"""#6862 (EP8.5) : les alertes du collecteur de santé de la grappe partent
sans la base.

`sante-base.py` (#6870) écrit `/run/ameesh-db/alertes.json` sur chaque nœud ;
le passage local du notifieur le relaie sans connexion — c'est ce qui fait
partir `db_no_primary` quand il n'y a plus de primaire, PC éteint. Le banc
fait tourner le vrai collecteur en mode hors ligne (`--lecture`), puis le
vrai `Notifier.run_local_pass` sans aucune base.
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import config, db_sante, exploitation, migration_alerts, notify, pgbouncer_local

ROOT = Path(__file__).resolve().parents[1]
SANTE = ROOT / "deploy/serveur/patroni/sante-base.py"

SAINE = {
    "patroni": {"leader": "ameesh-agents-1",
                "membres": [{"nom": "ameesh-agents-1", "role": "leader"},
                            {"nom": "ameesh-agents-2", "role": "sync_standby", "retard": 0}]},
    "etcd": [{"url": "https://10.78.0.11:2379", "sain": True},
             {"url": "https://10.78.0.12:2379", "sain": True},
             {"url": "https://10.78.0.10:2379", "sain": True}],
    "disque": {"chemin": "/var/lib/postgresql", "total": 100 * 1024 ** 3,
               "libre": 60 * 1024 ** 3},
}
SANS_PRIMAIRE = {
    "patroni": None,
    "etcd": [{"url": "https://10.78.0.11:2379", "sain": True},
             {"url": "https://10.78.0.12:2379", "sain": False},
             {"url": "https://10.78.0.10:2379", "sain": False}],
    "erreurs": {"patroni": "connexion refusée"},
}


class CollecteurTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-6862-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run/ameesh-db"
        self.fichier = self.run_dir / "alertes.json"
        self.cfg = dataclasses.replace(config.Config(), host="ameesh-agents-2",
                                       db_health_file=str(self.fichier))

    def collecter(self, lecture, maintenant):
        source = self.root / "lecture.json"
        source.write_text(json.dumps(lecture), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(SANTE), "--lecture", str(source), "--json",
             "--maintenant", str(maintenant), "--etat", str(self.root / "etat.json"),
             "--alerte-fichier", str(self.fichier)],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, AMEESH_DB_GRAPPE="ameesh"))
        self.assertEqual(done.returncode, 0, done.stderr)

    def notifier(self, clock):
        ncfg = notify.parse_config({"default": ["desktop"]})
        return notify.Notifier(self.cfg, ncfg, state=notify.empty_state(),
                               sender=mock.Mock(), log=lambda text: None,
                               clock=lambda: clock)

    def passage(self, notifier, now):
        with mock.patch.object(pgbouncer_local, "probe", return_value=[]), \
             mock.patch.object(migration_alerts, "local", return_value=([], [])):
            notifier.run_local_pass(now=now)
        return sorted(entry["alert"]["type"] for entry in notifier.state["active"].values())

    def test_the_run_directory_is_readable_by_the_notifier(self):
        self.collecter(SAINE, 1000.0)
        self.assertEqual(stat.S_IMODE(self.run_dir.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.fichier.stat().st_mode), 0o644)
        unit = (ROOT / "deploy/serveur/patroni/ameesh-db-sante.service").read_text()
        self.assertIn("RuntimeDirectoryMode=0755", unit)
        self.assertIn("RuntimeDirectoryPreserve=yes", unit)

    def test_no_primary_is_raised_without_any_database(self):
        """Plus de primaire, quorum perdu : l'alerte part du passage local, et
        le Notifier n'a jamais de base (la passe ordinaire ne tourne pas)."""
        self.collecter(SANS_PRIMAIRE, 1000.0)
        notifier = self.notifier(1010.0)
        with mock.patch("ameesh.db.connect", side_effect=AssertionError("no db")):
            types = self.passage(notifier, 1010.0)
        self.assertEqual(types, ["db_etcd_no_quorum", "db_no_primary"])
        alert = next(e["alert"] for e in notifier.state["active"].values()
                     if e["alert"]["type"] == "db_no_primary")
        self.assertEqual(alert["host"], "ameesh-agents-2")
        self.assertEqual(alert["cluster"], "ameesh")
        self.assertIn("connexion refusée", alert["detail"])
        self.assertIn("db_no_primary", notify.URGENT_TYPES)

    def test_the_real_notify_command_pushes_while_the_database_is_down(self):
        """`ameesh notify --once` sur un nœud : la base refuse (pas de
        primaire), la passe ordinaire échoue — l'alerte est déjà partie."""
        self.collecter(SANS_PRIMAIRE, 1000.0)
        sent = []

        class Sender:
            def send(self, channel, message):
                sent.append((channel.name, message.title, message.body))

        cfg = dataclasses.replace(self.cfg, state_dir=str(self.root / "state"))
        ncfg = notify.parse_config({"default": ["desktop"],
                                    "default_human": "human:proprio"})
        args = notify.build_parser().parse_args(["--once"])
        refused = notify.db_mod.Unavailable("no primary")
        with mock.patch.object(pgbouncer_local, "probe", return_value=[]), \
                mock.patch.object(migration_alerts, "local", return_value=([], [])), \
                mock.patch.object(db_sante.time, "time", return_value=1010.0), \
                mock.patch.object(notify.db_mod, "connect", side_effect=refused), \
                mock.patch.object(notify, "Sender", lambda **kw: Sender()):
            with self.assertRaises(notify.db_mod.Unavailable):
                notify.cmd_notify(cfg, ncfg, args)
        titles = " | ".join(title for _channel, title, _body in sent)
        self.assertIn("database cluster has no primary", titles)
        self.assertIn("database consensus (etcd) without quorum", titles)

    def test_same_episode_is_one_alert_across_collector_runs(self):
        """Le collecteur re-date chaque minute ; la clé ne suit pas `since`."""
        self.collecter(SANS_PRIMAIRE, 1000.0)
        first, _ = db_sante.local(self.cfg, 1010.0)
        self.collecter(SANS_PRIMAIRE, 1060.0)
        second, _ = db_sante.local(self.cfg, 1070.0)
        self.assertEqual({notify.key_of(a) for a in first},
                         {notify.key_of(a) for a in second})

    def test_healthy_cluster_resolves(self):
        self.collecter(SANS_PRIMAIRE, 1000.0)
        notifier = self.notifier(1010.0)
        self.assertTrue(self.passage(notifier, 1010.0))
        self.collecter(SAINE, 1060.0)
        self.assertEqual(self.passage(notifier, 1070.0), [])

    def test_replication_lag_stays_with_db_status(self):
        """`db_replication_lag` a déjà un propriétaire (passe ordinaire, #6871)."""
        lecture = json.loads(json.dumps(SAINE))
        lecture["patroni"]["membres"][1]["retard"] = 10 ** 9
        self.collecter(lecture, 1000.0)
        alerts, errors = db_sante.local(self.cfg, 1010.0)
        self.assertEqual((alerts, errors), ([], []))
        self.assertNotIn("db_replication_lag", notify.LOCAL_TYPES)

    def test_stale_document_keeps_alerts_and_says_so(self):
        self.collecter(SANS_PRIMAIRE, 1000.0)
        notifier = self.notifier(1010.0)
        self.passage(notifier, 1010.0)
        # Le collecteur s'est arrêté : rien n'est résolu, le silence est dit.
        types = self.passage(notifier, 1000.0 + db_sante.STALE_S + 60)
        self.assertEqual(types, ["db_etcd_no_quorum", "db_health_stale", "db_no_primary"])
        # Il repart sain : tout se résout, `db_health_stale` compris.
        now = 1000.0 + db_sante.STALE_S + 120
        self.collecter(SAINE, now)
        self.assertEqual(self.passage(notifier, now + 5), [])

    def test_missing_file_under_the_run_directory_is_not_conclusive(self):
        self.collecter(SANS_PRIMAIRE, 1000.0)
        notifier = self.notifier(1010.0)
        self.passage(notifier, 1010.0)
        self.fichier.unlink()
        self.assertEqual(self.passage(notifier, 1020.0),
                         ["db_etcd_no_quorum", "db_no_primary"])

    def test_host_outside_the_cluster_has_no_source(self):
        cfg = dataclasses.replace(self.cfg, db_health_file=str(self.root / "absent/alertes.json"))
        self.assertEqual(db_sante.local(cfg, 1.0), ([], []))

    def test_invalid_document_is_not_conclusive(self):
        self.run_dir.mkdir(parents=True)
        for content in ("{", json.dumps({"schema": "autre", "alertes": []}),
                        json.dumps({"schema": db_sante.SCHEMA, "alertes": [],
                                    "genere": "hier"})):
            with self.subTest(content=content):
                self.fichier.write_text(content, encoding="utf-8")
                alerts, errors = db_sante.local(self.cfg, 1.0)
                self.assertEqual(alerts, [])
                self.assertEqual(len(errors), 1)

    def test_types_are_declared_labelled_and_local(self):
        for kind in db_sante.TYPES:
            with self.subTest(kind=kind):
                self.assertIn(kind, exploitation.ALERT_TYPES)
                self.assertIn(kind, notify.DEFAULT_TYPES)
                self.assertIn(kind, notify.LOCAL_TYPES)
                self.assertIn(kind, notify.TYPE_LABELS)

    def test_config_reads_the_environment(self):
        cfg = config.load({"AMEESH_DB_HEALTH_FILE": "/tmp/x/alertes.json",
                           "AMEESH_DSN": "postgresql://u@h/d"},
                          register_credentials=False)
        self.assertEqual(db_sante.path_of(cfg), "/tmp/x/alertes.json")
        self.assertEqual(db_sante.path_of(config.Config()), db_sante.DEFAULT_PATH)


if __name__ == "__main__":
    unittest.main()
