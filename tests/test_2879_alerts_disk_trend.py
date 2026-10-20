# SPDX-License-Identifier: AGPL-3.0-only
"""#2879 : la tendance disque ne rapatrie plus des Mo par hôte.

`ameesh alerts` ne rendait rien en 240 s sur pc-smichea : `hosts.disk_history`
lisait 4 835 relevés (un toutes les ~4 s) et 3,3 Mo de `volumes` jsonb par
hôte, sur un lien mesuré à 30 KiB/s — 53-63 s par hôte — alors que la même
requête prend ~0,1 s côté serveur. Le coût était le transfert, pour n'en tirer
qu'une droite.

La lecture est maintenant sous-échantillonnée en base : un point par tranche
de 5 min (le dernier de la tranche, pour garder le point le plus frais), et
chaque volume ne porte plus que les champs de la tendance (`path`, `paths`,
`disk_free_bytes`). Les témoins ci-dessous bornent la taille rendue, gardent
le repli des relevés d'avant 0359 et vérifient que la droite — donc l'alerte
`host_disk_soon_full` — ne bouge pas.
"""
from __future__ import annotations

import json
import time
import unittest

from ameesh import exploitation, resources, storage

from .support import PgTestCase

GIB = 1024 ** 3
HOUR = 3600.0
WINDOW = resources.DISK_TREND_WINDOW_S
STEP = resources.DISK_TREND_STEP_S
#: plafond du témoin : 72 points de quelques dizaines d'octets. Avant #2879,
#: les relevés pleins de cet hôte dépassent le mégaoctet.
MAX_READ_BYTES = 64 * 1024


def payload_bytes(rows) -> int:
    return len(json.dumps(rows, ensure_ascii=False).encode())


def volume(path: str = "/", free: int = 500 * GIB, total: int = 1000 * GIB) -> dict:
    """Un volume complet, tel que `resources.volumes` l'écrit : la lecture
    étroite ne doit en garder que les champs de la tendance."""
    return {"path": path, "paths": [path], "fstype": "ext4",
            "disk_free_bytes": free, "disk_total_bytes": total,
            "btrfs_unallocated_bytes": None,
            "btrfs_metadata_headroom_bytes": None}


class DiskHistoryReadTest(PgTestCase):
    """La lecture de la tendance est bornée, côté base."""

    def setUp(self) -> None:
        super().setUp()
        self.hosts = storage.of(self.db).hosts

    def test_une_fenetre_dense_tient_dans_quelques_ko(self):
        dense = 4835  # 6 h à un relevé toutes les 4 s, comme pc-smichea
        volumes = json.dumps([volume("/", 500 * GIB), volume("/work", 100 * GIB)])
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, disk_path,"
            " disk_free_bytes, volumes)"
            " SELECT 'pc', now() - i * interval '4 seconds', '/', %s, %s::jsonb"
            " FROM generate_series(1, %s) AS i",
            (500 * GIB, volumes, dense))
        rows = self.hosts.disk_history("pc", WINDOW, STEP)
        # un point par tranche de 5 min au plus, même sur 4 835 relevés
        self.assertLessEqual(len(rows), int(WINDOW // STEP) + 1)
        self.assertGreater(len(rows), 60, "la fenêtre reste couverte")
        buckets = [int(row["sampled_ts"] // STEP) for row in rows]
        self.assertEqual(len(buckets), len(set(buckets)),
                         "deux points dans la même tranche")
        self.assertEqual(buckets, sorted(buckets))
        # le dernier relevé de la tranche est gardé : le plus récent ne part
        # jamais (l'appelant exclut le relevé courant par son id, ce point
        # reste le plus frais de l'historique)
        newest = self.db.query(
            "SELECT extract(epoch FROM max(sampled_at))::float8 AS ts"
            " FROM host_resources WHERE host = 'pc'")[0]["ts"]
        self.assertEqual(rows[-1]["sampled_ts"], newest)
        self.assertLessEqual(payload_bytes(rows), MAX_READ_BYTES,
                             "la tendance ne doit plus rapatrier des Mo")
        # le volume rendu ne porte que les champs de la tendance
        for row in rows:
            self.assertEqual(set(row),
                             {"id", "sampled_ts", "volumes", "disk_path",
                              "disk_free_bytes"})
            self.assertTrue(row["volumes"])
            for vol in row["volumes"]:
                self.assertEqual(set(vol), {"path", "paths", "disk_free_bytes"})

    def test_un_releve_sans_volumes_garde_son_repli(self):
        """Un relevé d'avant 0359 n'a pas de `volumes` : `disk_path` et
        `disk_free_bytes` restent lus (le repli de `_volume_series`)."""
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, disk_path, disk_free_bytes)"
            " VALUES ('old', now() - interval '10 minutes', '/w', 123),"
            "        ('old', now() - interval '5 minutes', '/w', 120)")
        rows = self.hosts.disk_history("old", WINDOW, STEP)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["volumes"] for row in rows], [[], []])
        self.assertEqual(resources._volume_series(rows, "/w"),
                         [(rows[0]["sampled_ts"], 123),
                          (rows[1]["sampled_ts"], 120)])

    def test_aucun_releve_ne_rend_rien(self):
        self.assertEqual(self.hosts.disk_history("absent", WINDOW, STEP), [])


class TrendPreservedTest(unittest.TestCase):
    """Le sous-échantillonnage ne change pas la droite de la tendance."""

    def test_la_meme_eta_sur_des_points_denses(self):
        floor = resources.DEFAULT_MIN_DISK_FREE
        base = 300 * GIB
        slope = -10 * GIB / HOUR
        # un relevé toutes les 4 s sur la fenêtre, avec une oscillation de
        # 2 GiB : le bruit ne doit pas être amplifié par les tranches
        raw = [(i * 4.0,
                int(base + slope * i * 4.0 + (2 * GIB if i % 2 else 0)))
               for i in range(int(WINDOW // 4) + 1)]
        # ce que la base rend : le dernier point de chaque tranche
        sampled: dict[int, tuple[float, int]] = {}
        for point in raw:
            sampled[int(point[0] // STEP)] = point
        down = [sampled[bucket] for bucket in sorted(sampled)]
        self.assertGreaterEqual(len(down), resources.DISK_TREND_MIN_POINTS)
        eta_raw = resources.disk_floor_eta(raw, floor)
        eta_down = resources.disk_floor_eta(down, floor)
        self.assertIsNotNone(eta_raw)
        self.assertIsNotNone(eta_down)
        self.assertLess(abs(eta_down - eta_raw) / eta_raw, 0.02,
                        "la tranche de 5 min fausse la tendance")


class DiskTrendAlertTest(PgTestCase):
    """L'alerte `host_disk_soon_full` se calcule sur la lecture étroite."""

    def test_l_alerte_de_tendance_survit_au_sous_echantillonnage(self):
        base = 200 * GIB            # libre au relevé courant
        fall_per_step = 10 * GIB * 4 / 60  # 10 GiB/h, un point toutes les 4 min
        total = 1000 * GIB
        points = 90                 # 6 h, un point toutes les 4 min
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, disk_path,"
            " disk_free_bytes, volumes)"
            " SELECT 'pc', now() - i * interval '4 minutes', '/',"
            "        (base.free)::bigint,"
            "        jsonb_build_array(jsonb_build_object("
            "            'path', '/', 'paths', jsonb_build_array('/'),"
            "            'fstype', 'ext4', 'disk_total_bytes', %s,"
            "            'disk_free_bytes', (base.free)::bigint))"
            "   FROM (SELECT %s + %s * i AS free, i"
            "           FROM generate_series(1, %s) AS i) AS base",
            (total, base, fall_per_step, points))
        current = {"host": "pc", "sampled_ts": time.time(), "id": -1,
                   "disk_path": "/", "disk_free_bytes": base,
                   "volumes": [volume("/", base, total)]}
        alert = exploitation._disk_soon_full_alert(
            self.db, current, resources.thresholds(None))
        self.assertIsNotNone(alert)
        self.assertEqual(alert["type"], "host_disk_soon_full")
        self.assertEqual(alert["reason"], "seuil")
        self.assertIn("floor in", alert["detail"])
        # (200 - 2) GiB à 10 GiB/h : 19,8 h, que la lecture soit dense ou
        # sous-échantillonnée (une droite exacte garde sa pente)
        self.assertAlmostEqual(alert["floor_eta_s"] / HOUR, 19.8, delta=0.35)


if __name__ == "__main__":
    unittest.main()
