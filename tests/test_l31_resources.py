# SPDX-License-Identifier: AGPL-3.0-only
"""Ressources des hôtes (lot L31, décision 0028) : relevés, seuils, pression,
`ameesh hosts`, rattachement des ressources d'un tour et cache de visibilité."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from ameesh import resources, storage

from .support import PgTestCase


# ==========================================================================
# mesures et seuils (sans base)
# ==========================================================================

class MesuresTest(unittest.TestCase):
    def test_parse_bytes(self):
        cas = {
            "1GiB": 1024 ** 3, "512MiB": 512 * 1024 ** 2, "2GB": 2 * 1000 ** 3,
            "1KiB": 1024, "0": 0, 4096: 4096, " 3 GiB ": 3 * 1024 ** 3,
        }
        for value, attendu in cas.items():
            with self.subTest(value=value):
                self.assertEqual(resources.parse_bytes(value), attendu)
        for mauvais in (None, True, "nope", "1XiB", -5, "-1GiB", [], {}):
            with self.subTest(mauvais=mauvais):
                self.assertIsNone(resources.parse_bytes(mauvais))

    def test_meminfo(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo", delete=False) as fh:
            fh.write("MemTotal:       16000000 kB\n"
                     "MemAvailable:    3000000 kB\n"
                     "SwapTotal:       8000000 kB\n"
                     "SwapFree:        2000000 kB\n")
            chemin = fh.name
        try:
            out = resources._meminfo(chemin)
        finally:
            os.unlink(chemin)
        self.assertEqual(out["mem_available_bytes"], 3000000 * 1024)
        self.assertEqual(out["swap_used_bytes"], 6000000 * 1024)

    def test_meminfo_sans_memavailable(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo", delete=False) as fh:
            fh.write("MemTotal: 100 kB\nMemFree: 50 kB\n")
            chemin = fh.name
        try:
            self.assertEqual(resources._meminfo(chemin), {})
        finally:
            os.unlink(chemin)

    def test_meminfo_illisible(self):
        self.assertEqual(resources._meminfo("/chemin/qui/n/existe/pas"), {})

    def test_seuils_par_defaut(self):
        limits = resources.thresholds(None)
        self.assertEqual(limits["min_mem_available"], resources.DEFAULT_MIN_MEM_AVAILABLE)
        self.assertEqual(limits["max_swap_used"], resources.DEFAULT_MAX_SWAP_USED)
        self.assertEqual(limits["min_disk_free"], resources.DEFAULT_MIN_DISK_FREE)
        self.assertGreater(limits["max_load"], 0)

    def test_seuils_politique(self):
        class Policy:
            resources = {"min_mem_available": "2GiB", "max_swap_used": "512MiB",
                         "max_load": 3.5, "min_disk_free": "10GB"}

        limits = resources.thresholds(Policy)
        self.assertEqual(limits["min_mem_available"], 2 * 1024 ** 3)
        self.assertEqual(limits["max_swap_used"], 512 * 1024 ** 2)
        self.assertEqual(limits["max_load"], 3.5)
        self.assertEqual(limits["min_disk_free"], 10 * 1000 ** 3)

    def test_seuils_partiels_completes(self):
        class Policy:
            resources = {"max_load": 1.0}

        limits = resources.thresholds(Policy)
        self.assertEqual(limits["max_load"], 1.0)
        self.assertEqual(limits["min_mem_available"], resources.DEFAULT_MIN_MEM_AVAILABLE)

    def test_franchissements_et_gravite(self):
        limits = {"min_mem_available": 1000, "max_swap_used": 1000,
                  "max_load": 4.0, "min_disk_free": 1000}
        reading = {"mem_available_bytes": 400, "swap_used_bytes": 2500,
                   "load1": 9.0, "disk_free_bytes": 900}
        found = resources.breaches(reading, limits)
        self.assertEqual({b["key"] for b in found},
                         {"min_mem_available", "max_swap_used", "max_load", "min_disk_free"})
        # une mesure absente ne déclenche rien
        self.assertEqual(resources.breaches({"load1": None}, limits), [])

    def test_pression_bloque_sans_etre_critique(self):
        limits = {"min_mem_available": 1000, "max_swap_used": 1000,
                  "max_load": 4.0, "min_disk_free": 1000}
        # mémoire sous le plancher mais pas critique (600 > 1000/2)
        reading = {"mem_available_bytes": 600, "swap_used_bytes": 0,
                   "load1": 1.0, "disk_free_bytes": 10 ** 6}
        found = resources.breaches(reading, limits)
        self.assertTrue(found)
        self.assertFalse(any(b["critical"] for b in found))
        # mémoire sous la moitié du plancher : critique
        reading["mem_available_bytes"] = 100
        found = resources.breaches(reading, limits)
        self.assertTrue(any(b["critical"] for b in found))

    def test_pressure_utilise_les_defauts(self):
        verdict = resources.pressure({"mem_available_bytes": 0}, None)
        self.assertTrue(verdict["blocked"])
        self.assertTrue(verdict["critical"])
        self.assertEqual(verdict["limits"]["min_mem_available"],
                         resources.DEFAULT_MIN_MEM_AVAILABLE)

    def test_sample_avec_fichier_meminfo(self):
        reading = resources.sample("h", path=os.getcwd(), now=123.0)
        self.assertEqual(reading["host"], "h")
        self.assertEqual(reading["sampled_ts"], 123.0)
        self.assertEqual(reading["disk_path"], os.getcwd())


# ==========================================================================
# stockage et CLI (base réelle)
# ==========================================================================

class ResourcesDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        self.db.execute("DELETE FROM turn_resources")
        self.db.execute("DELETE FROM visibility_checks")

    def record(self, host="pc", **over):
        row = {"host": host, "mem_available_bytes": 5 * 1024 ** 3,
               "swap_used_bytes": 0, "load1": 1.5, "cpu_count": 8,
               "disk_free_bytes": 50 * 1024 ** 3, "disk_path": "/tmp",
               "turns_in_progress": 2}
        row.update(over)
        return storage.of(self.db).hosts.record(row)

    def test_releve_dernier_et_historique(self):
        self.record(mem_available_bytes=1024)
        time.sleep(0.01)
        self.record(mem_available_bytes=2048)
        hosts = storage.of(self.db).hosts
        latest = hosts.latest("pc")
        self.assertEqual(latest["mem_available_bytes"], 2048)
        self.assertIn("sampled_ts", latest)
        histoire = hosts.history("pc", 10)
        self.assertEqual([r["mem_available_bytes"] for r in histoire], [1024, 2048])
        self.assertEqual(len(hosts.current(None)), 1)
        self.assertEqual(hosts.current("autre"), [])

    def test_tours_en_cours(self):
        self.db.execute(
            "INSERT INTO agent_registry (name, host, status, lease_owner, "
            " lease_expires_at) VALUES "
            "('a', 'pc', 'running', 'r', now() + interval '5 min'),"
            "('b', 'pc', 'running', NULL, NULL),"
            "('c', 'pc', 'idle', 'r', now() + interval '5 min'),"
            "('d', 'autre', 'running', 'r', now() + interval '5 min')")
        self.assertEqual(storage.of(self.db).hosts.turns_in_progress("pc"), 1)

    def test_ressources_de_tour_et_orphelins(self):
        tr = storage.of(self.db).turn_resources
        tr.open_turn("t1", "agent", "pc", pgid=1234, label=None)
        tr.open_turn("t2", "agent", "pc", pgid=5678, label="ameesh.turn=t2")
        self.assertEqual(len(tr.open_by_agent("agent")), 2)
        tr.close_turn("t1", orphan=False)
        self.assertEqual([r["turn_id"] for r in tr.open_by_agent("agent")], ["t2"])
        tr.close_turn("t2", orphan=True)
        orphelins = tr.orphans("pc")
        self.assertEqual([r["turn_id"] for r in orphelins], ["t2"])
        self.assertEqual(orphelins[0]["status"], "orphan")
        self.assertEqual(tr.orphans("autre"), [])
        # line encore running et ancienne : un exécuteur mort l'a laissée
        tr.open_turn("t3", "agent", "pc", pgid=9, label=None)
        self.assertEqual(tr.stale_running(3600, host="pc"), [])
        self.db.execute("UPDATE turn_resources SET started_at = now() - interval '2 hours'"
                        " WHERE turn_id = 't3'")
        self.assertEqual([r["turn_id"] for r in tr.stale_running(3600, "pc")], ["t3"])

    def test_cache_visibilite(self):
        vis = storage.of(self.db).visibility
        self.assertIsNone(vis.cached("p", "h", time.time(), "ctx"))
        vis.put("p", "h", ok=True, diagnostic="", repository="git@x:y.git",
                context="ctx", ttl_s=60)
        row = vis.cached("p", "h", time.time(), "ctx")
        self.assertTrue(row["ok"])
        self.assertEqual(row["repository"], "git@x:y.git")
        # un autre contexte ne réutilise jamais le verdict
        self.assertIsNone(vis.cached("p", "h", time.time(), "autre"))
        self.assertIsNone(vis.cached("p", "h", time.time() + 120, "ctx"))
        self.assertEqual(vis.purge(time.time() + 120), 1)

    def test_cli_hosts_json_et_texte(self):
        self.record(host="pc-test", mem_available_bytes=3 * 1024 ** 3, load1=0.5,
                    disk_free_bytes=20 * 1024 ** 3, turns_in_progress=1)
        proc = self.mesh("hosts", "pc-test", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lignes = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lignes), 1)
        objet = lignes[0]
        self.assertEqual(objet["schema"], "ameesh-host/1")
        self.assertEqual(objet["host"], "pc-test")
        self.assertEqual(objet["latest"]["mem_available_bytes"], 3 * 1024 ** 3)
        self.assertFalse(objet["pressure"]["blocked"])
        self.assertIn("limits", objet)
        texte = self.mesh("hosts", "pc-test")
        self.assertEqual(texte.returncode, 0, texte.stderr)
        self.assertIn("pc-test", texte.stdout)
        self.assertIn("seuils", texte.stdout)
        self.assertIn("pression", texte.stdout)

    def test_executeur_publie_un_releve(self):
        proc = self.runner("--once", "--wait", "0",
                           env=self.env(AMEESH_RESOURCE_INTERVAL="30"))
        self.assertIn(proc.returncode, (0, 3), proc.stdout)
        rows = storage.of(self.db).hosts.current(None)
        self.assertEqual(len(rows), 1)
        self.assertIn("mem_available_bytes", rows[0])
