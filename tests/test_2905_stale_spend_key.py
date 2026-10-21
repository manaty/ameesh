# SPDX-License-Identifier: AGPL-3.0-only
"""#2905 — une clé de marqueur périmée n'est jamais la clé du tour.

Après un retour arrière de version (§14), l'ancien `PendingSpend.put` (sans
`spend_key`, base c5cd599a) réécrit l'identité du marqueur — `start_index`,
`turn`, `turn_id`, `created_at` — mais laisse la colonne `spend_key` (0471)
du tour précédent. À la remontée, `get` rendait cette clé telle quelle : la
réparation écrivait sous la clé d'un tour révolu, butait sur l'unicité de
`turn_costs.spend_key` et perdait en silence le coût du tour.

`_get_sql` ne croit `spend_key` que si c'est l'identité exacte du marqueur
(`agent:index:created_at`, le format de `put`) ; sinon la clé est traitée
comme NULL et le repli par `turn_id`/`started_at` décide.
"""
from __future__ import annotations

import os
import unittest
from urllib.parse import quote

from ameesh import registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class StaleSpendKeyTest(PgTestCase):
    """La colonne `spend_key` n'est crue que si elle EST l'identité du marqueur."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM spend_pending")

    def _db(self, digits: int = 0):
        """Une connexion qui imprime les float8 à cette précision."""
        sep = "&" if "?" in self.cfg.dsn else "?"
        dsn = self.cfg.dsn + sep + "options=" + quote(
            "-c extra_float_digits=%d" % digits)
        db = self.connect(dsn=dsn)
        reglage = int(db.query(
            "select current_setting('extra_float_digits') as v")[0]["v"])
        self.assertEqual(reglage, digits, "la connexion n'applique pas la précision")
        return db

    def _worker(self, db, name: str):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd)
        runner = Runner(self.cfg, db, once=True)
        lease = registry.claim(db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _stale_marker(self, db, name: str, stale: str, *, start: int = 1,
                      turn_id: str = "T2") -> dict:
        """Le marqueur laissé par l'ancien `put` : identité neuve, clé ancienne."""
        db.execute(
            "INSERT INTO spend_pending (agent, start_index, turn, model, turn_id,"
            " spend_key, created_at)"
            " VALUES (%s, %s, 'tour', 'm', %s, %s, now() - interval '1 minute')",
            (name, start, turn_id, stale))
        return registry.pending_spend_get(db, name)

    def _previous_line(self, db, name: str, key: str, *, turn_id: str = "T1") -> None:
        """La ligne du tour précédent, sous la clé laissée dans le marqueur."""
        db.execute(
            "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
            " turn_id, started_at, recorded_at)"
            " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, %s,"
            " now() - interval '1 hour', now() - interval '1 hour')",
            (name, key, turn_id))

    def _lines(self, db, name: str) -> list:
        return [ligne["spend_key"] for ligne in db.query(
            "SELECT spend_key FROM turn_costs WHERE agent = %s ORDER BY id",
            (name,))]

    def test_cle_perimee_replie_sur_l_identite_du_marqueur(self):
        """Témoin : une clé d'un autre `created_at` n'est jamais `ledger_key`.

        La colonne reste l'identité brute du marqueur (elle borne `clear_if`) ;
        seule la clé du grand livre est recalculée depuis l'identité du tour.
        """
        for digits in (0, -5):
            with self.subTest(extra_float_digits=digits):
                db = self._db(digits)
                try:
                    name = "stale%d" % digits
                    stale = "%s:3:2026-10-20T22:00:00.123456Z" % name
                    marker = self._stale_marker(db, name, stale)
                    self.assertEqual(marker["spend_key"], stale,
                                     "l'identité brute du marqueur est perdue")
                    self.assertFalse(marker["ledger_ambiguous"])
                    self.assertEqual(
                        marker["ledger_key"],
                        "%s:1:%s" % (name, marker["created_at"]),
                        "la clé du tour précédent est rendue comme celle du tour")
                    worker = self._worker(db, name)
                    self.assertTrue(worker._compta_ecrit(marker))
                    self.assertEqual(self._lines(db, name), [marker["ledger_key"]])
                    self.assertIsNone(registry.pending_spend_get(db, name))
                finally:
                    db.close()

    def test_cle_perimee_ne_perd_pas_le_cout_du_tour(self):
        """Le coût du tour est écrit : le conflit de la clé périmée est évité."""
        db = self._db(0)
        try:
            name = "stale-cost"
            stale = "%s:3:2026-10-20T22:00:00.123456Z" % name
            marker = self._stale_marker(db, name, stale)
            self._previous_line(db, name, stale)
            relu = registry.pending_spend_get(db, name)
            expected = "%s:1:%s" % (name, relu["created_at"])
            self.assertFalse(relu["ledger_ambiguous"])
            self.assertEqual(relu["ledger_key"], expected)
            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(relu))
            self.assertEqual(self._lines(db, name), [stale, expected],
                             "le coût du tour a été perdu en silence")
            self.assertIsNone(registry.pending_spend_get(db, name))
        finally:
            db.close()

    def test_cle_perimee_replie_sur_la_ligne_du_tour_ecrite_avant_la_remontee(self):
        """L'ancien exécuteur a déjà réparé sous sa clé dérivée : on la reprend.

        Le retour arrière a laissé une ligne du tour sous la clé historique
        (`agent:index:epoch`) : la remontée la retrouve par `turn_id` et
        n'écrit pas une seconde ligne.
        """
        db = self._db(0)
        try:
            name = "stale-repair"
            stale = "%s:3:2026-10-20T22:00:00.123456Z" % name
            marker = self._stale_marker(db, name, stale)
            self._previous_line(db, name, stale)
            old_key = "%s:1:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " turn_id, started_at, recorded_at)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, 'T2',"
                " to_timestamp(%s), now())",
                (name, old_key, float(marker["created_ts"])))
            relu = registry.pending_spend_get(db, name)
            self.assertFalse(relu["ledger_ambiguous"])
            self.assertEqual(relu["ledger_key"], old_key)
            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(relu))
            self.assertEqual(self._lines(db, name), [stale, old_key],
                             "la réparation a écrit une ligne en double")
            self.assertIsNone(registry.pending_spend_get(db, name))
        finally:
            db.close()

    def test_cle_perimee_indemontrable_ne_retombe_pas_sur_la_colonne(self):
        """Repli ambigu : la colonne périmée n'est pas rendue comme clé non plus."""
        db = self._db(0)
        try:
            name = "stale-ambiguous"
            stale = "%s:1:2026-10-20T22:00:00.123456Z" % name
            self._stale_marker(db, name, stale, start=1)
            foreign = "%s:1:%.6f" % (name, 1790000000.0)
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'settled', %s,"
                " now() - interval '1 hour')", (name, foreign))
            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"],
                              "la colonne périmée est rendue malgré l'ambiguïté")
            self.assertEqual(relu["spend_key"], stale)
        finally:
            db.close()

    def test_cle_exacte_du_marqueur_reste_autoritaire(self):
        """Une clé au format de `put` reste la clé, sans repli ni ambiguïté."""
        db = self._db(0)
        try:
            name = "exact"
            marker = registry.pending_spend_begin(db, name, 1, "tour", "m")
            self.assertIsNotNone(marker)
            self.assertEqual(marker["spend_key"], "%s:1:%s" % (
                name, marker["created_at"]))
            self.assertFalse(marker["ledger_ambiguous"])
            self.assertEqual(marker["ledger_key"], marker["spend_key"])
            self.assertEqual(registry.pending_spend_get(db, name)["ledger_key"],
                             marker["spend_key"])
        finally:
            db.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
