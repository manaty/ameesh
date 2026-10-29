# SPDX-License-Identifier: AGPL-3.0-only
"""Qualification du journal d'observations sur bureau-goal (#4425).

Ce test ne tourne que sur la VM bureau-goal, avec la configuration reelle et
les sources reelles (attestations de staging + projection d'approbation
#4006). Il est **saute** sans la fixture, conformement au lot : le code et les
tests sur fixtures ne dependent pas de #4424 ; la qualification reelle si.

Sur bureau-goal, avec la configuration de collecte (jamais dans Git) ::

    AMEESH_OBSERVATIONS_REAL_CONFIG=/etc/ameesh/observations.json \
        PYTHONPATH=src python3 -m unittest tests.test_process_observations_real -v

La configuration nomme le journal et la projection reels ; le test fait un
``refresh --dry-run`` et ne touche a aucun fichier. Il peut aussi verifier que
le jeton #4424 n'apparait nulle part dans la projection :
``AMEESH_OBSERVATIONS_REAL_TOKEN``.
"""
from __future__ import annotations

import json
import os
import unittest

from ameesh import process_observations as po


class RealObservationsQualification(unittest.TestCase):
    def setUp(self) -> None:
        path = os.environ.get("AMEESH_OBSERVATIONS_REAL_CONFIG")
        if not path:
            self.skipTest("set AMEESH_OBSERVATIONS_REAL_CONFIG on bureau-goal")
        with open(path, encoding="utf-8") as handle:
            self.config = json.load(handle)
        if not isinstance(self.config, dict):
            self.fail("config: JSON object expected")
        self.section = self.config.get("observations")
        if not isinstance(self.section, dict):
            self.fail("config: observations section expected")

    def test_the_collector_reads_the_real_sources_without_writing(self):
        source = po.source_from_config(self.section)
        projection = po.refresh(source, dry_run=True)
        self.assertEqual(projection.authority, source.authority)
        self.assertEqual(projection.source["journal_id"], source.journal_id)
        counts = po.summary(projection)["counts"]
        minimum = os.environ.get("AMEESH_OBSERVATIONS_REAL_EXPECT_MIN")
        if minimum:
            self.assertGreaterEqual(counts["observations"], int(minimum),
                                    counts)
        # Le collecteur ne lit jamais le jeton : la projection ne peut pas le
        # contenir. La variable ne sert qu'a cette recherche.
        token = os.environ.get("AMEESH_OBSERVATIONS_REAL_TOKEN")
        if token:
            raw = json.dumps(projection.as_dict(), ensure_ascii=False)
            self.assertNotIn(token, raw)


if __name__ == "__main__":
    unittest.main()
