# SPDX-License-Identifier: AGPL-3.0-only
"""Ressources orphelines (lot L31, décision 0028) : rattachement des ressources
d'un tour, détection en lecture seule et alerte `orphan_resource`."""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import containers, exploitation, storage

from .support import PgTestCase


class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class ContainersTest(unittest.TestCase):
    def test_etiquettes(self):
        self.assertEqual(containers.turn_labels("t1", "agent"),
                         "ameesh.turn=t1,ameesh.agent=agent")

    def test_runtime_desactive_ou_absent(self):
        self.assertIsNone(containers.runtime_binary("none"))
        self.assertIsNone(containers.runtime_binary("off"))
        self.assertIsNone(containers.runtime_binary("binaire-qui-n-existe-pas"))

    def test_liste_des_conteneurs_du_tour(self):
        appels = []

        def run(argv, capture_output, text, timeout):
            appels.append((argv, timeout))
            return FakeProc(stdout="abc123\ndef456\n")

        runtime = containers.Runtime(binary="docker", timeout=2.0, run=run)
        self.assertEqual(runtime.running_for_turn("t1"), ["abc123", "def456"])
        # le filtre porte l'étiquette du tour, et le résultat est mis en cache
        self.assertIn("label=ameesh.turn=t1", appels[0][0])
        self.assertEqual(appels[0][1], 2.0)
        runtime.running_for_turn("t1")
        self.assertEqual(len(appels), 1)

    def test_moteur_en_erreur_rend_vide(self):
        runtime = containers.Runtime(binary="docker",
                                     run=lambda *a, **k: FakeProc(returncode=1))
        self.assertEqual(runtime.running_for_turn("t1"), [])

    def test_from_config(self):
        class Cfg:
            container_runtime = "none"
            container_timeout = 5.0

        self.assertIsNone(containers.Runtime.from_config(Cfg()))
        with mock.patch.object(containers, "runtime_binary", return_value="/usr/bin/docker"):
            runtime = containers.Runtime.from_config(Cfg())
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.binary, "/usr/bin/docker")


class OrphanDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")

    def test_executeur_rattache_puis_ferme_le_tour(self):
        self.register("agent", "claude", cwd=self.tmp, prompt="un tour")
        proc = self.runner("--once", "--agents", "agent")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lignes = storage.of(self.db).turn_resources.open_by_agent("agent")
        # plus aucune ressource ouverte : le tour est fermé proprement
        self.assertEqual(lignes, [])
        rows = self.db.query(
            "SELECT turn_id, pgid, label, status, containers,"
            " extract(epoch from ended_at)::float8 AS ended_ts FROM turn_resources")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["pgid"])
        self.assertIsNotNone(rows[0]["ended_ts"])
        self.assertEqual(rows[0]["status"], "done")
        self.assertIn("ameesh.turn=", rows[0]["label"])

    def test_alerte_orphan_resource(self):
        storage.of(self.db).turn_resources.open_turn(
            "tour-1", "agent", "pc", pgid=4242, label="ameesh.turn=tour-1")
        storage.of(self.db).turn_resources.mark_orphan("tour-1", containers=["abc123"])
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "orphan_resource"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["agent"], "agent")
        self.assertEqual(alertes[0]["turn"], "tour-1")
        self.assertEqual(alertes[0]["resource_status"], "orphan")

    def test_tour_laisser_par_un_executeur_mort(self):
        storage.of(self.db).turn_resources.open_turn(
            "tour-2", "agent", "pc", pgid=99, label=None)
        self.db.execute("UPDATE turn_resources SET started_at = now() - interval '2 hours'"
                        " WHERE turn_id = 'tour-2'")
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "orphan_resource"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["resource_status"], "running")
        self.assertIn("aucun exécuteur", alertes[0]["detail"])
