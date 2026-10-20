# SPDX-License-Identifier: AGPL-3.0-only
"""#2759 : une lecture d'historique qui échoue n'avorte pas la transaction
appelante, et l'aide de --plan-pace-gap dit ce que le seuil fait vraiment."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import time
import unittest
from unittest import mock

from . import support
from ameesh import db as db_mod
from ameesh import exploitation, storage, sous_utilisation
from ameesh.config import Config

WEEK = 7 * 24 * 3600.0
HOUR = 3600.0


class GaugeHistoryIsolationTest(support.PgTestCase):
    """Le repli moyenne ne doit pas coûter l'avortement de la transaction."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE quota_gauge_readings, provider_balances, turn_costs "
                        "RESTART IDENTITY CASCADE")

    def _readings(self, now: float) -> float:
        """Deux relevés d'une semaine qui projettent 36 % de perte au rythme
        récent (0,06 -> 0,10 sur 5 h), et une jauge courante à 0,10 pour
        60 % de fenêtre écoulée : la moyenne de fenêtre projette 83 % de
        perte. Les deux sources alertent, mais se distinguent par
        `rate_source` — le témoin du repli."""
        resets_at = now + 0.40 * WEEK
        values = (
            (0.06, now - 5 * HOUR - 1.0),
            (0.10, now - 1.0),
        )
        for used, stamp in values:
            self.db.execute(
                "INSERT INTO quota_gauge_readings"
                " (harness, gauge_key, used, resets_at, window_s, account,"
                "  produced_at, observed_at, source)"
                " VALUES ('codex', 'seven_day', %s, to_timestamp(%s), %s, 'primaire',"
                "         to_timestamp(%s), to_timestamp(%s), 'test')",
                (used, resets_at, WEEK, stamp, stamp))
        return resets_at

    def _cfg(self) -> Config:
        return Config(accounts={"codex": [{"name": "primaire"}]})

    def test_a_failed_history_read_leaves_the_callers_transaction_usable(self):
        now = time.time()
        self._readings(now)
        cfg = self._cfg()
        real_connect = db_mod.connect
        absent = dataclasses.replace(self.cfg, schema=self.schema + "_absent")

        with self.db.transaction() as tx:
            # Témoin : la connexion indépendante lit l'historique réel.
            [control] = sous_utilisation.plan_underused(tx, [], now, cfg=cfg)
            self.assertEqual(control["rate_source"], "recent")
            # Puis elle échoue pour de vrai (schéma absent), sans toucher la
            # transaction appelante.
            with mock.patch.object(
                    sous_utilisation.db_mod, "connect",
                    side_effect=lambda _cfg: real_connect(absent)) as connect:
                [alert] = sous_utilisation.plan_underused(tx, [], now, cfg=cfg)
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(alert["rate_source"], "window_average")
            # La dépense au token, lue après la lecture d'historique dans la
            # même boucle, aboutit encore : la transaction n'est pas avortée.
            self.assertEqual(
                storage.of(tx).turn_costs.spent(86400.0, agent="all",
                                                harnesses=("codex",)), 0.0)

    def test_outside_a_transaction_the_history_read_uses_the_given_connection(self):
        now = time.time()
        self._readings(now)
        with mock.patch.object(sous_utilisation.db_mod, "connect",
                               side_effect=AssertionError("connexion inutile")) as connect:
            rows = sous_utilisation._gauge_history(self.db, since_s=6 * HOUR)
        self.assertEqual(len(rows), 2)
        connect.assert_not_called()


class PlanPaceGapHelpTest(unittest.TestCase):
    def test_the_help_says_the_threshold_also_gates_the_end_of_window(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(out):
            exploitation.build_parser().parse_args(["alerts", "--help"])
        self.assertEqual(caught.exception.code, 0)
        text = out.getvalue()
        self.assertIn("also gates the end-of-window trigger", text)
        self.assertNotIn("windows of at least one day", text)
