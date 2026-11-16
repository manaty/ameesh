# SPDX-License-Identifier: AGPL-3.0-only
"""#7523 — l'échéance annoncée d'une pause tient compte de la consommation
critique observée.

`Gauge.free_at` fige l'usage au moment du relevé. Or les agents critiques qui
partagent le compte continuent de consommer pendant la pause : chaque point
consommé recule l'heure annoncée de 1,68 h sur la fenêtre de 7 jours, et
l'échéance « recule » à chaque contrôle. Le rythme est lu dans l'historique des
jauges, sur les seuls relevés pris alors que le compte était déjà au seuil des
non-critiques : leur pente est la consommation de ceux qui ne sont pas en
pause. L'heure projetée est stable entre deux relevés tant que le rythme se
maintient.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import accounts, cost

from .support import PgTestCase

HOUR = 3600.0
WEEK = 7 * 24 * HOUR


class FreeAtRateTest(unittest.TestCase):
    """Le calcul pur : `rate` recule l'heure, puis la stabilise."""

    def setUp(self) -> None:
        self.now = 1_800_000_000.0
        self.resets = self.now + 0.40 * WEEK
        self.gauge = cost.Gauge("claude", "seven_day", 0.67, self.resets, WEEK)

    def free(self, gauge=None, now=None, **kw):
        return (gauge or self.gauge).free_at(now or self.now, 0.10, **kw)

    def test_sans_rythme_l_heure_ne_change_pas(self):
        self.assertAlmostEqual(self.free(), self.free(rate=0.0), delta=1e-6)

    def test_la_consommation_critique_recule_l_heure(self):
        # 0,3 point par heure : rW = 0,504 ; 11,76 h deviennent 23,71 h.
        rate = 0.3 * 0.01 / HOUR
        self.assertGreater(self.free(rate=rate), self.free() + HOUR)
        self.assertAlmostEqual(
            (self.free(rate=rate) - self.now) / HOUR,
            0.07 * 168.0 / (1.0 - rate * WEEK), delta=0.01)

    def test_l_heure_projetee_est_stable_entre_deux_releves(self):
        # Le deuxième relevé voit l'usage monter du rythme annoncé : la même
        # heure absolue est annoncée, sans reculer d'un point de plus.
        rate = 0.3 * 0.01 / HOUR
        after = cost.Gauge("claude", "seven_day", 0.67 + 0.3 * 0.01,
                           self.resets, WEEK)
        first = self.free(rate=rate)
        second = after.free_at(self.now + HOUR, 0.10, rate=rate)
        # même heure absolue : le relevé suivant ne la décale plus
        self.assertAlmostEqual(second, first, delta=1.0)

    def test_une_consommation_au_moins_egale_au_rythme_attend_le_reset(self):
        for points_per_hour in (1.0 / 1.68, 1.0):
            self.assertEqual(self.free(rate=points_per_hour * 0.01 / HOUR),
                             self.resets)

    def test_un_usage_projete_au_plafond_attend_le_reset(self):
        # Sous le rythme autorisé, mais l'usage projeté franchit le plafond
        # effectif (90 % ici) avant le croisement : seule la remise libère.
        high = cost.Gauge("claude", "seven_day", 0.86, self.resets, WEEK)
        self.assertEqual(high.free_at(self.now, 0.0, rate=0.4 * 0.01 / HOUR),
                         self.resets)

    def test_un_usage_projete_au_plafond_des_non_critiques_attend_le_reset(self):
        # 78 % utilisés, plafond effectif 90 % moins la réserve de 10 points :
        # à 0,2 point/heure l'usage projeté franchit 80 % avant le
        # croisement, et le plafond des non-critiques ne remonte plus.
        nearly = cost.Gauge("claude", "seven_day", 0.78, self.resets, WEEK)
        self.assertEqual(nearly.free_at(self.now, 0.10,
                                        rate=0.2 * 0.01 / HOUR),
                         self.resets)


class ConsumptionRateTest(unittest.TestCase):
    """Le rythme lu dans l'historique : seuls les relevés au seuil comptent."""

    def setUp(self) -> None:
        self.now = 1_800_000_000.0
        self.resets = self.now + 0.40 * WEEK
        self.gauge = cost.Gauge("claude", "seven_day", 0.674, self.resets, WEEK)

    def row(self, stamp, used, *, key="seven_day", account="primaire",
            reset=None):
        return {"harness": "claude", "key": key, "account": account,
                "used": used, "resets_at_ts": reset or self.resets,
                "window_s": WEEK, "observed_ts": stamp, "produced_ts": stamp}

    def rate(self, rows, **kw):
        return cost.consumption_rate(rows, self.gauge, now=self.now,
                                     account="primaire", reserve=0.10, **kw)

    def test_la_pente_des_releves_au_seuil_est_le_rythme(self):
        rows = [self.row(self.now - 2 * HOUR, 0.670),
                self.row(self.now - HOUR, 0.672),
                self.row(self.now, 0.674)]
        self.assertAlmostEqual(self.rate(rows), 0.002 / HOUR, delta=1e-12)

    def test_un_releve_sous_le_seuil_n_entre_pas_dans_le_rythme(self):
        # Le premier relevé (50 %) date d'avant la pause des non-critiques :
        # il mesurerait leur consommation, qui vient de s'arrêter.
        rows = [self.row(self.now - 2 * HOUR, 0.50),
                self.row(self.now - HOUR, 0.672),
                self.row(self.now, 0.674)]
        self.assertAlmostEqual(self.rate(rows), 0.002 / HOUR, delta=1e-12)

    def test_moins_de_deux_releves_ou_intervalle_court_rend_zero(self):
        self.assertEqual(self.rate([self.row(self.now, 0.674)]), 0.0)
        self.assertEqual(self.rate([self.row(self.now - 600, 0.672),
                                    self.row(self.now, 0.674)]), 0.0)

    def test_un_usage_qui_recule_rend_zero(self):
        rows = [self.row(self.now - 2 * HOUR, 0.680),
                self.row(self.now - HOUR, 0.674),
                self.row(self.now, 0.672)]
        self.assertEqual(self.rate(rows), 0.0)

    def test_une_autre_fenetre_ou_un_autre_compte_est_ignore(self):
        rows = [self.row(self.now - 2 * HOUR, 0.670, reset=self.resets + WEEK),
                self.row(self.now - HOUR, 0.672),
                self.row(self.now, 0.674)]
        self.assertAlmostEqual(self.rate(rows), 0.002 / HOUR, delta=1e-12)
        rows = [self.row(self.now - 2 * HOUR, 0.670, account="secondaire"),
                self.row(self.now - HOUR, 0.672),
                self.row(self.now, 0.674)]
        self.assertAlmostEqual(self.rate(rows), 0.002 / HOUR, delta=1e-12)

    def test_sans_compte_les_releves_nommes_sont_ignores(self):
        rows = [self.row(self.now - 2 * HOUR, 0.670, account="secondaire"),
                self.row(self.now - HOUR, 0.672, account="secondaire"),
                self.row(self.now, 0.674, account="secondaire")]
        self.assertEqual(cost.consumption_rate(rows, self.gauge, now=self.now,
                                               reserve=0.10), 0.0)


class _Historique(PgTestCase):
    """Un historique de jauges réel, aux horodatages du scénario."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE quota_gauge_readings RESTART IDENTITY")
        self.now = time.time()
        self.resets = self.now + 0.40 * WEEK
        self.gauge = cost.Gauge("claude", "seven_day", 0.674, self.resets, WEEK)
        self.directory = tempfile.mkdtemp(prefix="ameesh-7523-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        os.chmod(self.directory, 0o700)
        self.profile = accounts.parse({"claude": [{
            "name": "primaire", "path": self.directory,
            "check_credentials": False}]})["claude"][0]
        self.book = cost.CostBook(state_dir=self.directory, db=self.db,
                                  clock=lambda: self.now)

    def read(self, points, *, account="primaire"):
        """Insère (décalage en secondes, usage) dans quota_gauge_readings."""
        for offset, used in points:
            stamp = self.now - offset
            self.db.execute(
                "INSERT INTO quota_gauge_readings (harness, gauge_key, used,"
                " resets_at, window_s, account, produced_at, source, observed_at)"
                " VALUES (%s, %s, %s, to_timestamp(%s), %s, %s,"
                " to_timestamp(%s), %s, to_timestamp(%s))",
                ("claude", "seven_day", used, self.resets, WEEK, account,
                 stamp, "ag1", stamp))


class EvaluateObserveeTest(_Historique):
    """`accounts.evaluate` annonce l'heure projetée, pas l'heure figée."""

    def evaluate(self):
        with mock.patch.object(self.book, "claude_gauges",
                               return_value=cost.Gauges([self.gauge])):
            return accounts.evaluate(self.book, self.profile, [self.profile],
                                     self.now, record=False, reserve=True)

    def test_l_echeance_est_reculee_par_le_rythme_observe(self):
        without = self.evaluate().until
        self.read([(2 * HOUR, 0.670), (HOUR, 0.672), (0, 0.674)])
        with_rate = self.evaluate().until
        self.assertGreater(with_rate, without + HOUR)
        # 0,2 point par heure : rW = 0,336 ; (67,4 − 60) % × 168 h / 0,664.
        self.assertAlmostEqual((with_rate - self.now) / HOUR,
                               0.074 * 168.0 / (1.0 - 0.2 * 0.01 / HOUR * WEEK),
                               delta=0.05)

    def test_sans_historique_le_calcul_d_origine_reste(self):
        expected = self.gauge.free_at(self.now, accounts.RESERVE)
        self.assertAlmostEqual(self.evaluate().until, expected, delta=1.0)

    def test_le_libelle_porte_l_echeance_projetee(self):
        self.read([(2 * HOUR, 0.670), (HOUR, 0.672), (0, 0.674)])
        ev = self.evaluate()
        label = accounts.pause_label({"primaire": ev})
        self.assertIn("@%d" % (int(ev.until) // 60 * 60), label)
        self.assertAlmostEqual(accounts.pause_deadline(label), ev.until, delta=60.0)

    def test_un_rythme_au_dessus_du_plafond_annonce_la_remise_a_zero(self):
        self.read([(2 * HOUR, 0.660), (HOUR, 0.670), (0, 0.680)])
        self.assertEqual(self.evaluate().until, self.gauge.resets_at)


class PaceExceededObserveeTest(_Historique):
    """Le chemin du compte par défaut (sans comptes déclarés) suit la même règle."""

    def reason(self):
        gauge = cost.Gauge("claude", "seven_day", 0.674, self.resets, WEEK)
        self.book.gauges = lambda harness, *a, **kw: cost.Gauges([gauge])
        return self.book.pace_exceeded("claude", accounts.RESERVE)

    def test_le_libelle_du_compte_par_defaut_recule(self):
        without = accounts.pause_deadline(self.reason())
        self.read([(2 * HOUR, 0.670), (HOUR, 0.672), (0, 0.674)], account=None)
        with_rate = accounts.pause_deadline(self.reason())
        self.assertGreater(with_rate, without + HOUR)
