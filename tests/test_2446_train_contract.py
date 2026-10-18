# SPDX-License-Identifier: AGPL-3.0-only
"""#2446 : le contrat CI du train est celui du portier et de la suite courants.

Un nom de job conservé ne prouve rien si le workflow, le portier ou le
planificateur a changé : ce témoin refuse un contrat dont un blob audité ne
correspond plus au commit. Toute modification de ces sources demande donc un
nouveau contrat audité (et ses deux familles de relecture).
"""
from __future__ import annotations

import os
import unittest

from ameesh import plan_git, train_contracts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ContractTest(unittest.TestCase):
    def test_packet_gate_and_suite_blobs_are_the_current_ones(self):
        contract, = train_contracts.ameesh()
        self.assertEqual(contract.event, "workflow_call")
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


if __name__ == "__main__":
    unittest.main()
