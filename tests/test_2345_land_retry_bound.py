# SPDX-License-Identifier: AGPL-3.0-only
"""#2345 : les réessais d'une lecture passagère sont bornés.

#2306 garde la ligne en file quand la forge répond mal pour une raison
passagère (5xx, délai, DNS, GOAWAY, sortie vide). Sans borne, une panne
durable fait tourner la file indéfiniment sans jamais rendre la main à
l'assigné. Au-delà de `TRANSIENT_RETRY_LIMIT` réessais, la ligne est donc
éjectée en fermé (rien n'a été fusionné) et l'assigné est réveillé.

Le compteur vit dans le journal (`RETRY_NOTE`), pas dans `queue.reason` :
le radar réécrit `reason` à chaque passage (ordre, report), ce qui
remettrait un compteur porté par la raison à zéro et rendrait la borne
inopérante.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import land

from .test_2286_queue_required_checks import FakeHeadGh
from .test_l160b_radar import _git
from .test_l160e2_land import _Land


class RetryBoundTest(_Land):
    """Bout en bout de la voie file locale : le passager finit par éjecter."""

    def setUp(self):
        super().setUp()
        self.project = dataclasses.replace(self.project, required_checks=("tests",))

    @staticmethod
    def _transient():
        # la lecture des checks requis échoue de façon passagère à chaque
        # passage : #2306 la laisse en file, #2345 doit l'arrêter.
        return FakeHeadGh(runs_error="gh: HTTP 502")

    def test_durable_transient_failures_are_bounded_then_eject_with_wake_mail(self):
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        with mock.patch.object(land, "TRANSIENT_RETRY_LIMIT", 2):
            first = self.land(4, gh=self._transient())
            self.assertEqual(first["result"], "retry", first)
            self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
            self.assertIn("retry 1/2", self.fake.queue_rows[4]["reason"])
            second = self.land(4, gh=self._transient())
            self.assertEqual(second["result"], "retry", second)
            self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
            self.assertIn("retry 2/2", self.fake.queue_rows[4]["reason"])
            third = self.land(4, gh=self._transient())
        self.assertEqual(third["result"], "ejected", third)
        self.assertEqual(self.fake.queue_rows[4]["state"], "ejected")
        self.assertIn("still failing after 2 retries", self.fake.queue_rows[4]["reason"])
        # rien n'a été fusionné, et le bail est rendu au passage suivant
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertIsNone(self.locks.held["p:main"][0])
        # l'assigné est réveillé avec la marche à suivre, pas laissé en attente
        to, body, kw = self.sent[-1]
        self.assertEqual(to, "a4")
        self.assertEqual(kw["payload"]["merge_queue"]["lot"], 4)
        self.assertIn("requeue the lot", body)
        self.assertIn("ameesh work queue add 4", body)

    def test_the_counter_survives_a_radar_rewrite_of_reason(self):
        # Le radar réécrit `reason` à chaque passage ; un compteur porté par
        # elle retomberait à zéro et la borne ne s'appliquerait jamais.
        self.queued(4)
        with mock.patch.object(land, "TRANSIENT_RETRY_LIMIT", 2):
            out = self.land(4, gh=self._transient())
            self.assertEqual(out["result"], "retry", out)
            self.fake.queue_rows[4]["reason"] = "report: radar rewrote the reason"
            out = self.land(4, gh=self._transient())
        self.assertEqual(out["result"], "retry", out)
        self.assertIn("retry 2/2", self.fake.queue_rows[4]["reason"])
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")

    def test_a_new_admission_resets_the_counter(self):
        """#2371 N2 : une note de réessai de l'admission précédente ne compte
        pas ; le faux journal honore `since_ts` comme le vrai."""
        self.queued(4)
        old = dict(self.fake.queue_rows[4])
        with mock.patch.object(land, "TRANSIENT_RETRY_LIMIT", 2):
            first = self.land(4, gh=self._transient())
            self.assertEqual(first["result"], "retry", first)
            self.assertIn("retry 1/2", self.fake.queue_rows[4]["reason"])
            self.fake.queue_remove(4, note="removed", actor="orch")
            self.fake.queue_add(4, target=old["target"], frozen_sha=old["frozen_sha"],
                                frozen_id=old["frozen_id"], source=old["source"],
                                note="reentered", actor="orch")
            self.assertGreater(self.fake.queue_rows[4]["queued_ts"], old["queued_ts"])
            again = self.land(4, gh=self._transient())
        self.assertEqual(again["result"], "retry", again)
        self.assertIn("retry 1/2", self.fake.queue_rows[4]["reason"],
                      "la nouvelle admission n'a pas remis le compteur à zéro")


class RetryLimitImportTest(unittest.TestCase):
    """#2371 N1 : une valeur d'environnement invalide ne casse plus l'import."""

    def test_invalid_value_falls_back_with_a_warning(self):
        with self.assertWarns(RuntimeWarning):
            self.assertEqual(land._retry_limit(
                {"AMEESH_LAND_TRANSIENT_RETRIES": "not-a-number"}),
                land.DEFAULT_TRANSIENT_RETRY_LIMIT)

    def test_absent_blank_and_negative_values(self):
        self.assertEqual(land._retry_limit({}), land.DEFAULT_TRANSIENT_RETRY_LIMIT)
        self.assertEqual(land._retry_limit(
            {"AMEESH_LAND_TRANSIENT_RETRIES": "  "}), land.DEFAULT_TRANSIENT_RETRY_LIMIT)
        self.assertEqual(land._retry_limit({"AMEESH_LAND_TRANSIENT_RETRIES": "3"}), 3)
        self.assertEqual(land._retry_limit({"AMEESH_LAND_TRANSIENT_RETRIES": "-2"}), 0)

    def test_the_module_imports_with_an_invalid_value(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        env = dict(os.environ,
                   PYTHONPATH=src + os.pathsep + (os.environ.get("PYTHONPATH") or ""),
                   AMEESH_LAND_TRANSIENT_RETRIES="not-a-number")
        proc = subprocess.run([sys.executable, "-c", "import ameesh.land"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("AMEESH_LAND_TRANSIENT_RETRIES", proc.stderr)


if __name__ == "__main__":
    unittest.main()
