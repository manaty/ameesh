# SPDX-License-Identifier: AGPL-3.0-only
"""#2446 : le contrat CI du train est celui du portier et de la suite courants.

Un nom de job conservé ne prouve rien si le workflow, le portier ou le
planificateur a changé : ce témoin refuse un contrat dont un blob audité ne
correspond plus au commit. Toute modification de ces sources demande donc un
nouveau contrat audité (et ses deux familles de relecture).

#3198 : la suite complète sous macOS a quitté `tests.yml` pour `macos.yml`
(nocturne et `ci-macos`, hors du chemin du paquet) ; elle n'est donc pas une
source de ce contrat. Le job `plateforme macOS` qu'il exige est le verdict
court L63/L66 resté dans `tests.yml`.
"""
from __future__ import annotations

import os
import unittest

from ameesh import plan_git, train_contracts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ContractTest(unittest.TestCase):
    def test_packet_gate_and_suite_blobs_are_the_current_ones(self):
        contract, = train_contracts.ameesh()
        self.assertEqual(contract.path, ".github/workflows/paquet.yml")
        self.assertEqual(contract.event, "push")
        self.assertEqual(contract.called, ".github/workflows/tests.yml")
        paths = [path for path, _blob in contract.sources]
        self.assertIn(".github/workflows/paquet.yml", paths)
        self.assertIn(".github/workflows/tests.yml", paths)
        for path, blob in contract.sources:
            with self.subTest(path=path):
                self.assertEqual(plan_git._out(ROOT, "rev-parse", "HEAD:" + path), blob)

    def test_full_packet_matrix_and_aggregate_are_required(self):
        contract, = train_contracts.ameesh()
        jobs = set(contract.jobs)
        self.assertIn("tests", jobs)
        self.assertIn("plan", jobs)
        self.assertIn("plateforme macOS", jobs)
        self.assertEqual({job for job in jobs if job.startswith("psycopg ")},
                         {"psycopg %d/8" % n for n in range(1, 9)})
        self.assertEqual({job for job in jobs if job.startswith("psql ")},
                         {"psql %d/16" % n for n in range(1, 17)})

    def test_the_full_macos_suite_is_not_a_packet_proof_source(self):
        # #3198 : la suite complète macOS (macos.yml) est nocturne et
        # `ci-macos`, hors du chemin du paquet ; le contrat ne l'épingle pas.
        contract, = train_contracts.ameesh()
        self.assertNotIn(".github/workflows/macos.yml",
                         [path for path, _blob in contract.sources])


if __name__ == "__main__":
    unittest.main()
