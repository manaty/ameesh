# SPDX-License-Identifier: AGPL-3.0-only
"""#2166 — l'identité du marqueur comptable est exacte, jamais un float8 relu.

`PendingSpend.clear_if` et `set_resident_usage` comparaient l'instant du
marqueur relu en `extract(epoch ...)::float8` à celui du tour. Une connexion
qui imprime le float8 à précision réduite (`extra_float_digits=0`, pools,
production) en perd les derniers chiffres : la comparaison échouait et le
marqueur `spend_pending` restait **après** le règlement.

Le marqueur porte désormais sa clé exacte (migration 0471) et toute écriture
conditionnelle se compare en `created_at` timestamptz + `spend_key`, y compris
sous d'autres réglages de précision. Les marqueurs d'avant 0471 (`spend_key`
NULL) gardent la clé historique dérivée du float8 : leur réparation dédouble
la ligne déjà écrite et règle la réservation existante au lieu d'en créer une
seconde.
"""
from __future__ import annotations

import os
import unittest
from urllib.parse import quote

from ameesh import registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

PRECISIONS = (0, 1, 3, -5)


class PendingSpendExactTest(PgTestCase):
    """Identité exacte du marqueur, sous plusieurs `extra_float_digits`."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM spend_pending")

    def _db(self, digits: int):
        """Une connexion qui imprime les float8 à cette précision."""
        sep = "&" if "?" in self.cfg.dsn else "?"
        dsn = self.cfg.dsn + sep + "options=" + quote(
            "-c extra_float_digits=%d" % digits)
        db = self.connect(dsn=dsn)
        reglage = int(db.query("select current_setting('extra_float_digits') as v")[0]["v"])
        self.assertEqual(reglage, digits, "la connexion n'applique pas la précision")
        return db

    def _marker(self, db, name: str, start: int = 0) -> dict:
        marker = registry.pending_spend_begin(db, name, start, "tour", "m")
        self.assertIsNotNone(marker)
        return marker

    def _worker(self, db, name: str):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd)
        runner = Runner(self.cfg, db, once=True)
        lease = registry.claim(db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_marqueur_et_cle_exacts_a_toute_precision(self):
        """La clé ne dépend plus du texte float8 : elle est le marqueur."""
        for digits in PRECISIONS:
            with self.subTest(extra_float_digits=digits):
                db = self._db(digits)
                try:
                    name = "exact%d" % digits
                    marker = self._marker(db, name)
                    relu = registry.pending_spend_get(db, name)
                    self.assertEqual(relu["created_at"], marker["created_at"])
                    self.assertEqual(relu["spend_key"], marker["spend_key"])
                    self.assertEqual(marker["spend_key"],
                                     "%s:0:%s" % (name, marker["created_at"]))
                finally:
                    db.close()

    def test_clear_if_efface_apres_reglement_a_toute_precision(self):
        """Le défaut observé : le marqueur restait après le règlement."""
        for digits in PRECISIONS:
            with self.subTest(extra_float_digits=digits):
                db = self._db(digits)
                try:
                    name = "clear%d" % digits
                    marker = self._marker(db, name)
                    self.assertTrue(registry.pending_spend_clear(db, name, marker))
                    self.assertIsNone(registry.pending_spend_get(db, name))
                finally:
                    db.close()

    def test_clear_if_epargne_un_remplacant_a_toute_precision(self):
        """Une identité d'un tour révolu n'efface jamais le marqueur suivant."""
        for digits in PRECISIONS:
            with self.subTest(extra_float_digits=digits):
                db = self._db(digits)
                try:
                    name = "replace%d" % digits
                    ancien = self._marker(db, name, 0)
                    suivant = self._marker(db, name, 1)
                    self.assertNotEqual(ancien["spend_key"], suivant["spend_key"])
                    self.assertFalse(registry.pending_spend_clear(db, name, ancien))
                    self.assertIsNotNone(registry.pending_spend_get(db, name))
                    self.assertTrue(registry.pending_spend_clear(db, name, suivant))
                    self.assertIsNone(registry.pending_spend_get(db, name))
                finally:
                    db.close()

    def test_mesure_residente_bornee_au_bon_marqueur(self):
        """L80a : même borne exacte, un remplaçant n'hérite pas de la mesure."""
        for digits in PRECISIONS:
            with self.subTest(extra_float_digits=digits):
                db = self._db(digits)
                usage = {"type": "resident.turn", "usage": {"input_tokens": 7}}
                try:
                    name = "resident%d" % digits
                    marker = self._marker(db, name)
                    self.assertTrue(registry.pending_spend_set_resident_usage(
                        db, name, usage, marker))
                    self.assertEqual(registry.pending_spend_get(db, name)["resident_usage"],
                                     usage)
                    suivant = self._marker(db, name, 1)
                    self.assertFalse(registry.pending_spend_set_resident_usage(
                        db, name, usage, marker))
                    self.assertIsNone(
                        registry.pending_spend_get(db, name)["resident_usage"])
                    self.assertTrue(registry.pending_spend_set_resident_usage(
                        db, name, usage, suivant))
                finally:
                    db.close()

    def test_marqueur_avant_0471_se_nettoie_par_son_instant_exact(self):
        """Un marqueur d'avant 0471 (clé NULL) garde son identité exacte."""
        for digits in PRECISIONS:
            with self.subTest(extra_float_digits=digits):
                db = self._db(digits)
                try:
                    name = "vieux%d" % digits
                    db.execute(
                        "INSERT INTO spend_pending (agent, start_index, turn, model,"
                        " created_at) VALUES (%s, 0, 'tour', 'm',"
                        " now() - interval '3 minutes')", (name,))
                    marker = registry.pending_spend_get(db, name)
                    self.assertIsNone(marker["spend_key"])
                    self.assertIsNotNone(marker["created_at"])
                    self.assertTrue(registry.pending_spend_set_resident_usage(
                        db, name, {"type": "resident.turn"}, marker))
                    self.assertTrue(registry.pending_spend_clear(db, name, marker))
                    self.assertIsNone(registry.pending_spend_get(db, name))
                finally:
                    db.close()

    def test_reparation_d_un_marqueur_avant_0471_ne_double_pas_la_ligne(self):
        """La clé historique dédoublonne la ligne et règle sa réservation.

        C'est l'état laissé par le défaut en production : ligne écrite, marqueur
        resté (clé NULL). Le nouveau worker répare sans écrire une 2e ligne.
        """
        db = self._db(0)
        try:
            name = "legacy"
            db.execute(
                "INSERT INTO spend_pending (agent, start_index, turn, model, created_at)"
                " VALUES (%s, 0, 'tour', 'm', now() - interval '5 minutes')", (name,))
            marker = registry.pending_spend_get(db, name)
            self.assertIsNone(marker["spend_key"])
            legacy_key = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s)", (name, legacy_key))
            # La réparation relit le marqueur APRÈS que l'ancien exécuteur a
            # écrit sa ligne : c'est cette lecture qui retrouve la clé durable.
            marker = registry.pending_spend_get(db, name)
            self.assertEqual(marker["ledger_key"], legacy_key)
            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(marker))
            lignes = db.query(
                "SELECT spend_key, usd::float8 AS usd FROM turn_costs WHERE agent = %s",
                (name,))
            self.assertEqual(len(lignes), 1, "la réparation a doublé la ligne")
            self.assertEqual(lignes[0]["spend_key"], legacy_key)
            self.assertAlmostEqual(lignes[0]["usd"], 1.5)
            self.assertIsNone(registry.pending_spend_get(db, name))
            self.assertFalse(worker._compta_en_echec)
        finally:
            db.close()

    def test_reparation_d_un_marqueur_0471_ne_double_pas_la_ligne(self):
        """La clé exacte reste celle du marqueur rejoué (L60)."""
        db = self._db(0)
        try:
            name = "current"
            marker = self._marker(db, name)
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 2.5, %s)",
                (name, marker["spend_key"]))
            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(marker))
            lignes = db.query(
                "SELECT spend_key FROM turn_costs WHERE agent = %s", (name,))
            self.assertEqual(len(lignes), 1)
            self.assertEqual(lignes[0]["spend_key"], marker["spend_key"])
            self.assertIsNone(registry.pending_spend_get(db, name))
        finally:
            db.close()

    def _legacy_marker(self, db, name: str) -> dict:
        """Un marqueur d'avant 0471, à la microseconde .123456."""
        db.execute(
            "INSERT INTO spend_pending (agent, start_index, turn, model, created_at)"
            " VALUES (%s, 0, 'tour', 'm',"
            "  date_trunc('second', now()) - interval '5 minutes'"
            "  + interval '0.123456 seconds')", (name,))
        marker = registry.pending_spend_get(db, name)
        self.assertIsNone(marker["spend_key"])
        return marker

    def test_reparation_avant_0471_sous_une_autre_precision_ne_double_pas(self):
        """0→3 : la clé durable vient de la ligne, pas du float8 recalculé.

        L'ancien exécuteur a écrit la ligne sous `extra_float_digits=0`
        (…123460) ; la réparation lit le marqueur sous 3 (…123456). Recalculer
        la clé historique doublerait le grand livre : elle se retrouve par
        l'identité du marqueur (première ligne après son `created_at`).
        """
        db0 = self._db(0)
        try:
            name = "legacy-precision"
            marker = self._legacy_marker(db0, name)
            legacy_key = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            self.assertNotEqual(legacy_key, "%s:0:%s" % (name, marker["created_at"]))
            db0.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " recorded_at) VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, now())",
                (name, legacy_key))
        finally:
            db0.close()
        db3 = self._db(3)
        try:
            marker3 = registry.pending_spend_get(db3, name)
            recalc = "%s:0:%.6f" % (name, float(marker3["created_ts"]))
            self.assertNotEqual(recalc, legacy_key,
                                "la précision n'a pas changé : le test ne couvre rien")
            self.assertEqual(marker3["ledger_key"], legacy_key,
                             "la clé durable n'a pas été retrouvée dans la ligne")
            worker = self._worker(db3, name)
            self.assertTrue(worker._compta_ecrit(marker3))
            lignes = db3.query(
                "SELECT spend_key, usd::float8 AS usd FROM turn_costs WHERE agent = %s",
                (name,))
            self.assertEqual(len(lignes), 1, "la réparation a doublé la ligne")
            self.assertEqual(lignes[0]["spend_key"], legacy_key)
            self.assertAlmostEqual(lignes[0]["usd"], 1.5)
            self.assertIsNone(registry.pending_spend_get(db3, name))
        finally:
            db3.close()

    def test_reparation_avant_0471_reservation_ouverte_autre_precision(self):
        """0→3, réservation encore ouverte : sa clé exacte est retrouvée.

        Le tour est mort avant d'écrire sa ligne : la seule clé durable est
        celle de la réservation #1469. La réparation doit écrire la ligne sous
        cette clé et régler la réservation, sans en créer une autre.
        """
        db0 = self._db(0)
        try:
            name = "legacy-reservation"
            marker = self._legacy_marker(db0, name)
            legacy_key = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db0.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open', %s, now())",
                (name, legacy_key))
        finally:
            db0.close()
        db3 = self._db(3)
        try:
            marker3 = registry.pending_spend_get(db3, name)
            recalc = "%s:0:%.6f" % (name, float(marker3["created_ts"]))
            self.assertNotEqual(recalc, legacy_key)
            self.assertEqual(marker3["ledger_key"], legacy_key,
                             "la clé de la réservation n'a pas été retrouvée")
            worker = self._worker(db3, name)
            self.assertTrue(worker._compta_ecrit(marker3))
            lignes = db3.query(
                "SELECT spend_key, billing_account FROM turn_costs WHERE agent = %s",
                (name,))
            self.assertEqual(len(lignes), 1)
            self.assertEqual(lignes[0]["spend_key"], legacy_key)
            self.assertEqual(lignes[0]["billing_account"], "deepseek-manaty")
            reservation = db3.query(
                "SELECT state FROM account_reservations WHERE spend_key = %s",
                (legacy_key,))[0]
            # Une réparation est incertaine (L13) : la réservation est gardée
            # (`kept`), mais elle a bien été retrouvée par sa clé et la ligne
            # lui est rattachée (billing_account ci-dessus).
            self.assertEqual(reservation["state"], "kept")
            self.assertIsNone(registry.pending_spend_get(db3, name))
        finally:
            db3.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
