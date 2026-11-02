# SPDX-License-Identifier: AGPL-3.0-only
"""#5466 — le compte Codex lit le relevé partagé de sa fenêtre courante.

Après une remise à zéro du forfait, agents-1 et agents-3 restaient en pause
sur leur journal local (81 %, échéance J+2) alors qu'agents-2 avait publié un
relevé neuf (1 %, échéance J+7) : l'échéance plus tardive prouve que l'ancienne
fenêtre est close. Le relevé de l'ancienne échéance est écarté, local ou
partagé ; à échéance égale, le relevé partagé le plus récent l'emporte sur une
lecture locale plus ancienne. Sans relevé partagé, la pause locale demeure.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from ameesh import accounts, config as config_mod, storage

from .support import PgTestCase
from .test_cost import Sandbox, write

WEEK = 7 * 86400


def _timestamp(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _rollout(home: str, used_percent: float, resets_at: float,
             observed_at: float, *, secondary: dict | None = None) -> str:
    """Un journal Codex local : une fenêtre hebdomadaire, datée."""
    limits = {"plan_type": "team",
              "credits": {"has_credits": True},
              "primary": {"used_percent": used_percent,
                          "window_minutes": 10080,
                          "resets_at": resets_at}}
    if secondary is not None:
        limits["secondary"] = secondary
    path = os.path.join(home, "sessions", "2026", "11", "02", "rollout.jsonl")
    write(path,
          {"type": "session_meta",
           "payload": {"creator_account_id": "account-a"}},
          {"timestamp": _timestamp(observed_at),
           "payload": {"rate_limits": limits}})
    return path


def _history(used: float, resets_at: float, *, account: str = "primaire",
             source: str = "ameesh-agents-2", key: str = "codex-10080min",
             window: float = WEEK, produced_at: float | None = None) -> dict:
    return {"harness": "codex", "key": key, "used": used,
            "resets_at": resets_at, "window_s": float(window),
            "account": account, "produced_at": produced_at, "source": source,
            "overage_capable": True, "overage_in_use": False}


class _CodexBase(Sandbox, PgTestCase):
    """Compte Codex déclaré, historique vide, horloge fixe du bac à sable."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE quota_gauge_readings RESTART IDENTITY")
        self.home = os.path.join(self.tmp_cost, "codex-home")
        os.makedirs(self.home, mode=0o700)
        with open(os.path.join(self.home, "auth.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}\n")
        self.items = accounts.parse({"codex": [
            {"name": "primaire", "path": self.home}]})["codex"]
        self.profile = self.items[0]
        self.ops = storage.of(self.db).operations

    def evaluate(self):
        return accounts.evaluate(self.book(), self.profile, self.items, self.now)

    @staticmethod
    def gauge(evaluation):
        return {g.key: g for g in evaluation.gauges}["codex-10080min"]


class FenetrePlusRecenteTest(_CodexBase):
    """Le choix du compte retient la fenêtre à échéance plus tardive."""

    def test_un_releve_partage_plus_recent_debloque_le_compte(self):
        _rollout(self.home, 81, self.now + 2 * 86400, self.now - 3600)
        self.ops.record_gauges([_history(0.01, self.now + WEEK)])

        evaluation = self.evaluate()

        self.assertTrue(evaluation.ok, evaluation.reason)
        gauge = self.gauge(evaluation)
        self.assertAlmostEqual(gauge.used, 0.01)
        self.assertTrue(gauge.from_history)
        self.assertEqual(gauge.source, "ameesh-agents-2")

    def test_sans_releve_partage_la_pause_locale_demeure(self):
        _rollout(self.home, 81, self.now + 2 * 86400, self.now - 3600)

        evaluation = self.evaluate()

        self.assertFalse(evaluation.ok)
        self.assertIn("81%", evaluation.reason)
        self.assertAlmostEqual(self.gauge(evaluation).used, 0.81)

    def test_remplacer_une_fenetre_saturee_leve_l_overage(self):
        """B #5466 : 100 % local + crédits, remplacé par 1 % à échéance J+7.

        L'ancien drapeau `in_use` restait collé aux jauges fusionnées ; sans
        fenêtre à 100 %, `spent()` retombait sur toutes les jauges et le
        compte était refusé « in overage » jusqu'à la NOUVELLE échéance.
        """
        _rollout(self.home, 100, self.now + 2 * 86400, self.now - 3600)
        self.ops.record_gauges([_history(0.01, self.now + WEEK)])

        evaluation = self.evaluate()

        self.assertTrue(evaluation.ok, evaluation.reason)
        self.assertAlmostEqual(self.gauge(evaluation).used, 0.01)
        self.assertFalse(evaluation.gauges.overage.in_use)
        self.assertFalse(accounts.in_overage(evaluation.gauges, self.now))

    def test_une_fenetre_locale_conservee_saturee_garde_l_overage(self):
        """Le remplacement d'une fenêtre n'efface pas l'overage d'une autre.

        La fenêtre courte locale, conservée, est encore à 100 % : le compte
        reste en overage (aucun refus levé par erreur).
        """
        _rollout(self.home, 100, self.now + 2 * 86400, self.now - 3600,
                 secondary={"used_percent": 100,
                            "window_minutes": 300,
                            "resets_at": self.now + 3600})
        self.ops.record_gauges([_history(0.01, self.now + WEEK)])

        evaluation = self.evaluate()

        self.assertTrue(evaluation.gauges.overage.in_use)
        self.assertTrue(accounts.in_overage(evaluation.gauges, self.now))
        self.assertFalse(evaluation.ok)
        self.assertIn("in overage", evaluation.reason)

    def test_une_fenetre_partagee_close_est_ecartee(self):
        """Le relevé partagé d'une échéance ANTÉRIEURE ne remplace pas le local."""
        _rollout(self.home, 1, self.now + WEEK, self.now - 60)
        self.ops.record_gauges([_history(0.81, self.now + 2 * 86400,
                                         source="ameesh-agents-1")])

        evaluation = self.evaluate()

        self.assertTrue(evaluation.ok, evaluation.reason)
        gauge = self.gauge(evaluation)
        self.assertAlmostEqual(gauge.used, 0.01)
        self.assertFalse(gauge.from_history)

    def test_a_echeance_egale_la_production_la_plus_recente_gagne(self):
        """Même fenêtre : un relevé partagé plus récent remplace le local."""
        _rollout(self.home, 10, self.now + WEEK, self.now - 3600)
        self.ops.record_gauges([_history(0.90, self.now + WEEK,
                                         produced_at=self.now - 60)])

        evaluation = self.evaluate()

        gauge = self.gauge(evaluation)
        self.assertAlmostEqual(gauge.used, 0.90)
        self.assertTrue(gauge.from_history)
        self.assertAlmostEqual(gauge.read_at, self.now - 60, places=0)

    def test_une_fenetre_absente_du_journal_n_est_pas_ressuscitee(self):
        """Une fenêtre que le journal local ne publie plus ne vient pas de
        l'historique (une courte périmée ne tient pas le compte en pause)."""
        _rollout(self.home, 5, self.now + WEEK, self.now - 60)
        self.ops.record_gauges([_history(1.0, self.now + 3600,
                                         key="codex-300min", window=300 * 60,
                                         produced_at=self.now - 3600)])

        evaluation = self.evaluate()

        self.assertEqual([g.key for g in evaluation.gauges], ["codex-10080min"])
        self.assertAlmostEqual(self.gauge(evaluation).used, 0.05)

    def test_l_ecriture_porte_l_hote_et_la_date_du_releve(self):
        _rollout(self.home, 81, self.now + 2 * 86400, self.now - 3600)

        self.evaluate()

        rows = self.ops.gauge_history(since_s=3600, harness="codex",
                                      account="primaire")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], config_mod.hostname())
        self.assertAlmostEqual(rows[0]["produced_ts"], self.now - 3600, places=0)


class FenetreStockageTest(_CodexBase):
    """La lecture d'historique classe la fenêtre, pas la dernière insertion."""

    def test_l_echeance_plus_tardive_gagne_meme_inseree_avant(self):
        self.ops.record_gauges([_history(0.01, self.now + WEEK,
                                         source="ameesh-agents-2")])
        # l'ancienne fenêtre est réinsérée APRÈS : `gauge_latest` la retient,
        # la lecture par fenêtre doit garder la neuve.
        self.ops.record_gauges([_history(0.81, self.now + 2 * 86400,
                                         source="ameesh-agents-1")])

        latest = self.ops.gauge_latest(harness="codex", account="primaire")
        self.assertEqual(len(latest), 1)
        self.assertAlmostEqual(latest[0]["used"], 0.81)

        window = self.ops.gauge_latest_window(harness="codex", account="primaire")
        self.assertEqual(len(window), 1)
        self.assertAlmostEqual(window[0]["used"], 0.01)
        self.assertEqual(window[0]["source"], "ameesh-agents-2")


if __name__ == "__main__":
    unittest.main()
