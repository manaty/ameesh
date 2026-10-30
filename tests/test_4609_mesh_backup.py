# SPDX-License-Identifier: AGPL-3.0-only
"""#4609 : `mesh_backup_unseen`, la lecture complète quotidienne de la base.

Le constat du 29/10 (#4300), sur le mesh alors en PostgreSQL 17 : les 150
tables encore lues par le `pg_dump` de 03:37Z portaient le même
`last_seq_scan` à la microseconde ; 121 autres, relues depuis par l'activité
normale, avaient avancé cet instant.
Le critère (B de relecture du 29/10) : un groupe d'au moins 5 % des tables
lisibles partage un instant T dans la fenêtre, et au moins 90 % des tables
lisibles ont un `last_seq_scan >= T` — une table relue après le dump a pu être
lue par lui, elle ne compte pas contre la sauvegarde. Le plancher de groupe
écarte la petite transaction ordinaire qui donnerait sinon 100 % (faux vert).
Les témoins ci-dessous simulent ces statistiques : ils ne dépendent ni de la
base, ni de l'heure murale, ni d'un `pg_dump` réel.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest import mock

from ameesh import config as config_mod, db as db_mod, exploitation, mesh_backup, notify

from .support import EMPTY_CONFIG, PgTestCase
from .test_l94_underuse import _Base

DAY_US = 86400 * 1000000


def ts(hour, minute=0, second=0, micro=0):
    """Un instant UTC du 2026-10-29, en microsecondes depuis l'époque."""
    at = datetime(2026, 10, 29, hour, minute, second, tzinfo=timezone.utc)
    return int(at.timestamp()) * 1000000 + micro


#: 16:00Z : la fenêtre 03:30–04:30Z du jour est close.
NOW = ts(16) / 1000000
#: l'instant du `pg_dump` relevé le 29/10 (03:37:04.834105Z)
DUMP = ts(3, 37, 4, 834105)


def rows(*instants):
    return [{"relname": "table_%d" % i, "last_seq_scan_us": value}
            for i, value in enumerate(instants)]


def scan_alert(alerts):
    return [a for a in alerts if a["type"] == "mesh_backup_unseen"]


class ProjectionTest(unittest.TestCase):
    def test_temoin_vert_un_instant_commun_dans_la_fenetre(self):
        result = mesh_backup.project(rows(*([DUMP] * 10), ts(10), ts(11), ts(12)),
                                     NOW)
        self.assertTrue(result["ok"])
        self.assertEqual((result["tables"], result["in_window"], result["shared"]),
                         (13, 10, 10))
        self.assertEqual(result["covered"], 13)
        self.assertEqual(result["min_group"], 2)
        self.assertEqual(result["share"], 1.0)
        self.assertEqual(result["instant_us"], DUMP)

    def test_rouge_aucun_instant_commun(self):
        instants = [ts(3, 31 + i) for i in range(10)]
        result = mesh_backup.project(rows(*instants), NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["in_window"], 10)
        self.assertEqual(result["shared"], 1)
        self.assertAlmostEqual(result["share"], 0.1)

    def test_hors_fenetre_aucune_preuve(self):
        result = mesh_backup.project(rows(ts(2), ts(2, 30), ts(3)), NOW)
        self.assertFalse(result["ok"])
        self.assertEqual((result["tables"], result["in_window"], result["shared"]),
                         (3, 0, 0))
        self.assertIsNone(result["instant_us"])
        detail = mesh_backup.alert_detail(result, 0.9)
        self.assertIn("no table still shows a sequential scan", detail)

    def test_les_tables_relues_ne_diluent_pas_le_seuil(self):
        """Le relevé du 29/10 : 150 tables à 03:37Z, 121 relues ensuite."""
        result = mesh_backup.project(
            rows(*([DUMP] * 150), *([ts(10, 16, 59, 51466)] * 121)), NOW)
        self.assertTrue(result["ok"])
        self.assertEqual((result["tables"], result["in_window"], result["shared"]),
                         (271, 150, 150))
        self.assertEqual(result["share"], 1.0)

    def test_faux_positif_0430_les_tables_relues_apres_le_dump_ne_comptent_pas(self):
        """B de relecture : à 04:30:30, 150 tables au dump et 59 relues depuis
        dans la fenêtre ; l'ancien dénominateur donnait 150/209 < 0,9."""
        result = mesh_backup.project(
            rows(*([DUMP] * 150), *([ts(3, 50)] * 59), *([ts(4, 10)] * 62)),
            ts(4, 30, 30) / 1000000)
        self.assertTrue(result["ok"])
        self.assertEqual((result["tables"], result["in_window"], result["shared"]),
                         (271, 271, 150))
        self.assertEqual(result["covered"], 271)
        self.assertEqual(result["share"], 1.0)

    def test_faux_positif_auditeur_20_tables_relues_dans_la_fenetre(self):
        """#4618, N P2 auditeur : à 04:30:30, 150 tables au dump et 20 relues
        à 03:50Z, encore dans la fenêtre. L'ancien dénominateur « tables
        encore dans la fenêtre » donnait 150/170 = 88 % < 90 % : une fausse
        alerte quotidienne qui s'éteint quand ces tables sortent de la
        fenêtre. Une table relue après T a pu être lue par le dump : la
        couverture est 170/170 et le seuil ne la compte plus contre lui."""
        re_read = ts(3, 50)
        result = mesh_backup.project(
            rows(*([DUMP] * 150), *([re_read] * 20)), ts(4, 30, 30) / 1000000)
        self.assertTrue(result["ok"])
        self.assertEqual((result["tables"], result["in_window"], result["shared"]),
                         (170, 170, 150))
        self.assertEqual((result["covered"], result["share"]), (170, 1.0))
        self.assertEqual(result["instant_us"], DUMP)
        # l'ancien calcul (part des tables encore dans la fenêtre) était rouge
        self.assertLess(150 / result["in_window"], 0.9)

    def test_faux_vert_un_petit_groupe_seul_ne_prouve_pas_le_dump(self):
        """Sans dump, 3 tables d'une même transaction ne font pas 100 %."""
        result = mesh_backup.project(rows(*([ts(3, 35)] * 3),
                                          *([ts(2)] * 97)), NOW)
        self.assertFalse(result["ok"])
        self.assertEqual((result["tables"], result["in_window"]), (100, 3))
        self.assertEqual(result["shared"], 3)
        self.assertEqual(result["min_group"], 5)
        self.assertAlmostEqual(result["share"], 0.03)
        detail = mesh_backup.alert_detail(result, 0.9)
        self.assertIn("below the 5-table floor", detail)

    def test_faux_vert_les_tables_jamais_scannees_comptent_dans_le_total(self):
        """B/P2 cx2 : 3 tables scannées ensemble et 97 jamais scannées après
        une remise à zéro ne doivent pas verdir la base."""
        result = mesh_backup.project(rows(*([ts(3, 35)] * 3), *([None] * 97)),
                                     NOW)
        self.assertFalse(result["ok"])
        self.assertEqual((result["tables"], result["never_scanned"]), (100, 97))
        self.assertEqual((result["shared"], result["covered"]), (3, 3))
        self.assertAlmostEqual(result["share"], 0.03)
        self.assertIn("97 never scanned", mesh_backup.alert_detail(result, 0.9))

    def test_quelques_tables_jamais_scannees_ne_bloquent_pas_un_dump(self):
        """Une poignée de tables NULL (créées après le dump, non scannées)
        reste sous la tolérance de 10 %."""
        result = mesh_backup.project(rows(*([DUMP] * 150), *([None] * 3)), NOW)
        self.assertTrue(result["ok"])
        self.assertEqual((result["tables"], result["never_scanned"]), (153, 3))
        self.assertAlmostEqual(result["share"], 150 / 153)

    def test_un_groupe_au_plancher_sans_couverture_alerte(self):
        """Une transaction incomplète : 89 tables lues, 11 restées avant T."""
        result = mesh_backup.project(rows(*([DUMP] * 89), *([ts(2)] * 11)), NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["shared"], 89)
        self.assertEqual(result["covered"], 89)
        self.assertAlmostEqual(result["share"], 0.89)

    def test_le_plancher_de_groupe_est_reglable(self):
        """3 tables à T et 97 relues après, une par seconde : le plancher par
        défaut refuse le petit groupe, un plancher plus bas le laisse passer
        (la couverture, elle, est de 100 %)."""
        instants = [ts(3, 35)] * 3 + [ts(10) + i * 1000000 for i in range(97)]
        self.assertFalse(mesh_backup.project(rows(*instants), NOW)["ok"])
        self.assertTrue(mesh_backup.project(rows(*instants), NOW,
                                            min_group_share=0.01)["ok"])

    def test_avant_la_fin_la_derniere_fenetre_close_est_celle_de_la_veille(self):
        result = mesh_backup.project(rows(*([DUMP - DAY_US] * 10)), ts(4) / 1000000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["window_start"], (ts(3, 30) - DAY_US) / 1000000)
        self.assertEqual(result["window_end"], (ts(4, 30) - DAY_US) / 1000000)

    def test_dump_du_jour_avant_0430_prouve_la_fenetre_de_la_veille(self):
        """B/P2 auditeur : à 04:00, le dump du jour (03:37) a avancé toutes
        les tables hors de la fenêtre de la veille ; il prouve au moins
        autant et ne doit pas lever l'alerte."""
        result = mesh_backup.project(rows(*([DUMP] * 271)), ts(4) / 1000000)
        self.assertTrue(result["ok"])
        self.assertEqual((result["tables"], result["in_window"]), (271, 0))
        self.assertEqual(result["instant_us"], DUMP)
        self.assertEqual(result["share"], 1.0)

    def test_la_fenetre_du_jour_est_jugee_apres_sa_fin(self):
        result = mesh_backup.project(rows(*([DUMP] * 10)), NOW)
        self.assertEqual(result["window_start"], ts(3, 30) / 1000000)
        self.assertEqual(result["window_end"], ts(4, 30) / 1000000)

    def test_seuil_reglable(self):
        # 8 tables au dump, 2 lues avant T : la couverture est 8/10.
        instants = [DUMP] * 8 + [ts(3, 31), ts(3, 32)]
        self.assertTrue(mesh_backup.project(rows(*instants), NOW,
                                            min_share=0.75)["ok"])
        self.assertFalse(mesh_backup.project(rows(*instants), NOW,
                                             min_share=0.9)["ok"])

    def test_fenetre_reglable(self):
        midnight = ts(0, 10)
        result = mesh_backup.project(rows(*([midnight] * 5), ts(2)),
                                     ts(12) / 1000000, window=(0, 3600))
        self.assertTrue(result["ok"])
        self.assertEqual(result["in_window"], 5)

    def test_fenetre_invalide_revient_au_defaut(self):
        for value in ((4 * 3600, 3 * 3600), None, ("03:30", "04:30"), object()):
            with self.subTest(value=value):
                result = mesh_backup.project(rows(*([DUMP] * 3)), NOW, window=value)
                self.assertEqual(result["window_start"], ts(3, 30) / 1000000)


class AlertTest(unittest.TestCase):
    def test_rouge_leve_une_alerte_complete(self):
        with mock.patch.object(mesh_backup, "read",
                               return_value=rows(*[ts(3, 31 + i) for i in range(10)])):
            out = scan_alert(mesh_backup.alerts(None, NOW))
        self.assertEqual(len(out), 1)
        alert = out[0]
        self.assertEqual(alert["schema"], exploitation.ALERT_SCHEMA)
        self.assertIsNone(alert["agent"])
        self.assertAlmostEqual(alert["since"], ts(3, 30) / 1000000)
        self.assertEqual(alert["value"], 0.1)
        self.assertEqual(alert["threshold"], 0.9)
        self.assertEqual((alert["tables"], alert["in_window"], alert["shared"]),
                         (10, 10, 1))
        self.assertIn("mesh_backup_unseen", exploitation.ALERT_TYPES)
        self.assertIn("mesh_backup_unseen", notify.DEFAULT_TYPES)
        self.assertIn("mesh_backup_unseen", notify.TYPE_LABELS)

    def test_vert_ne_leve_rien(self):
        with mock.patch.object(mesh_backup, "read",
                               return_value=rows(*([DUMP] * 10), ts(10))):
            self.assertEqual(mesh_backup.alerts(None, NOW), [])

    def test_hors_fenetre_leve_une_alerte(self):
        with mock.patch.object(mesh_backup, "read",
                               return_value=rows(ts(2), ts(3))):
            out = scan_alert(mesh_backup.alerts(None, NOW))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["value"], 0.0)
        self.assertIn("no table still shows a sequential scan", out[0]["detail"])

    def test_seuil_nul_desactive_sans_lire(self):
        with mock.patch.object(mesh_backup, "read") as read:
            self.assertEqual(mesh_backup.alerts(None, NOW, min_share=0), [])
        read.assert_not_called()

    def test_detail_dit_la_verification_et_ce_qu_il_ne_prouve_pas(self):
        result = mesh_backup.project(rows(*[ts(3, 31 + i) for i in range(10)]), NOW)
        detail = mesh_backup.alert_detail(result, 0.9)
        self.assertIn('psql "$AMEESH_DSN"', detail)
        self.assertIn("pg_stat_user_tables", detail)
        self.assertIn("not that the dump file was written", detail)
        self.assertIn("copied to S3", detail)

    def test_detail_montre_l_instant_a_la_microseconde(self):
        result = mesh_backup.project(rows(*([DUMP] * 3)), NOW)
        detail = mesh_backup.alert_detail(result, 0.9)
        self.assertIn("2026-10-29T03:37:04.834105Z", detail)


class ReadTest(unittest.TestCase):
    def test_lit_pg_stat_user_tables_sous_pg17(self):
        db = mock.Mock(cfg=mock.Mock(backend="pg"))
        db.query.side_effect = [[{"version": 170011}], [{"relname": "t",
                                                         "last_seq_scan_us": 1}]]
        self.assertEqual(mesh_backup.read(db), [{"relname": "t",
                                                 "last_seq_scan_us": 1}])
        self.assertIn("pg_stat_user_tables", db.query.call_args_list[-1].args[0])
        self.assertIn("last_seq_scan", db.query.call_args_list[-1].args[0])

    def test_pg16_lit_les_statistiques(self):
        # #4617 (N cx2) : `last_seq_scan` existe dès PostgreSQL 16 ; la garde
        # ne doit plus exclure une base PG16 de la vérification.
        db = mock.Mock(cfg=mock.Mock(backend="pg"))
        db.query.side_effect = [[{"version": 160000}], [{"relname": "t",
                                                         "last_seq_scan_us": 1}]]
        self.assertEqual(mesh_backup.read(db), [{"relname": "t",
                                                 "last_seq_scan_us": 1}])
        self.assertEqual(db.query.call_count, 2)

    def test_pg15_ne_peut_pas_prouver_et_se_tait(self):
        # La colonne est apparue en PostgreSQL 16 : un serveur plus ancien ne
        # peut pas fournir la preuve, l'alerte se tait au lieu de mentir.
        db = mock.Mock(cfg=mock.Mock(backend="pg"))
        db.query.return_value = [{"version": 150000}]
        self.assertIsNone(mesh_backup.read(db))
        self.assertEqual(db.query.call_count, 1)

    def test_backend_medie_ne_lit_pas_de_sql(self):
        db = mock.Mock(cfg=mock.Mock(backend="mediated"))
        self.assertIsNone(mesh_backup.read(db))
        db.query.assert_not_called()

    def test_sans_connexion_sql_ne_leve_pas(self):
        self.assertIsNone(mesh_backup.read(object()))

    def test_passe_a_blanc_ne_conclut_pas(self):
        # `db.batched` rend des listes vides à la passe à blanc : ne pas
        # conclure, la vraie passe relira.
        db = mock.Mock(cfg=mock.Mock(backend="pg"))
        db.query.return_value = []
        self.assertIsNone(mesh_backup.read(db))


class ReadOnPostgresTest(PgTestCase):
    """La requête réelle, sur la base du banc (PostgreSQL 16 ou plus récent)."""

    def test_la_requete_lit_les_statistiques_reelles(self):
        stats = mesh_backup.read(self.db)
        if stats is None:
            self.skipTest("PostgreSQL 16 required for last_seq_scan")
        self.assertTrue(stats)
        for row in stats:
            self.assertEqual(set(row), {"relname", "last_seq_scan_us"})

    def test_la_lecture_traverse_db_batched(self):
        # `alert_ack.current` passe par `db.batched` : la passe à blanc rend
        # des listes vides, la vraie passe doit relire sans lever.
        stats = db_mod.batched(self.db, lambda d: mesh_backup.read(d))
        self.assertTrue(stats is None or isinstance(stats, list))


class ConfigurationTest(unittest.TestCase):
    def load(self, **env):
        base = {"AMEESH_CONFIG": EMPTY_CONFIG}
        base.update(env)
        return config_mod.load(env=base)

    def test_defauts(self):
        cfg = config_mod.Config()
        self.assertEqual(cfg.mesh_backup_window, (12600, 16200))
        self.assertEqual(cfg.mesh_backup_min_share, 0.9)
        self.assertEqual(cfg.mesh_backup_min_group_share, 0.05)

    def test_environnement(self):
        cfg = self.load(AMEESH_MESH_BACKUP_WINDOW="04:00-05:00",
                        AMEESH_MESH_BACKUP_MIN_SHARE="0.8",
                        AMEESH_MESH_BACKUP_MIN_GROUP_SHARE="0.02")
        self.assertEqual(cfg.mesh_backup_window, (14400, 18000))
        self.assertEqual(cfg.mesh_backup_min_share, 0.8)
        self.assertEqual(cfg.mesh_backup_min_group_share, 0.02)

    def test_fenetre_illisible_ou_inversee_garde_le_defaut(self):
        for value in ("04:30-03:30", "minuit-midi", "03:30", "25:00-26:00"):
            with self.subTest(value=value):
                cfg = self.load(AMEESH_MESH_BACKUP_WINDOW=value)
                self.assertEqual(cfg.mesh_backup_window, (12600, 16200))

    def test_part_hors_bornes_garde_le_defaut(self):
        for value in ("2", "-1", "part"):
            with self.subTest(value=value):
                self.assertEqual(self.load(AMEESH_MESH_BACKUP_MIN_SHARE=value)
                                 .mesh_backup_min_share, 0.9)

    def test_plancher_de_groupe_hors_bornes_garde_le_defaut(self):
        for value in ("2", "-1", "part"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.load(AMEESH_MESH_BACKUP_MIN_GROUP_SHARE=value)
                    .mesh_backup_min_group_share, 0.05)

    def test_fichier_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"mesh_backup_window": ["03:30", "04:30"],
                           "mesh_backup_min_share": 0.75,
                           "mesh_backup_min_group_share": 0.02}, fh)
            cfg = config_mod.load(env={"AMEESH_CONFIG": path})
        self.assertEqual(cfg.mesh_backup_window, (12600, 16200))
        self.assertEqual(cfg.mesh_backup_min_share, 0.75)
        self.assertEqual(cfg.mesh_backup_min_group_share, 0.02)


class AlertsWiringTest(_Base):
    def enabled(self):
        # le banc désactive l'alerte (tests/support.py) : ce témoin l'active.
        return replace(self.cfg, mesh_backup_min_share=0.9)

    def test_l_alerte_sort_de_alerts(self):
        instants = [ts(3, 31 + i) for i in range(10)]
        with mock.patch.object(mesh_backup, "read", return_value=rows(*instants)):
            out = scan_alert(exploitation.alerts(self.enabled(), self.db, now=NOW))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "mesh_backup_unseen")

    def test_vert_dans_alerts(self):
        with mock.patch.object(mesh_backup, "read",
                               return_value=rows(*([DUMP] * 10), ts(10))):
            out = scan_alert(exploitation.alerts(self.enabled(), self.db, now=NOW))
        self.assertEqual(out, [])

    def test_lit_la_configuration_de_l_hote(self):
        cfg = replace(self.cfg, mesh_backup_window=(0, 60),
                      mesh_backup_min_share=0.9)
        with mock.patch.object(mesh_backup, "read",
                               return_value=rows(*([ts(0, 0, 10)] * 5))):
            out = scan_alert(exploitation.alerts(cfg, self.db, now=NOW))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
