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
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import quote

from ameesh import registry, storage
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


    def _legacy_marker_row(self, db, name: str, *, start: int = 0,
                           turn_id: str | None = None, age: str = "5 minutes") -> dict:
        """Un marqueur d'avant 0471 (clé NULL), à l'identité près."""
        db.execute(
            "INSERT INTO spend_pending (agent, start_index, turn, model, turn_id,"
            " created_at) VALUES (%s, %s, 'tour', 'm', %s,"
            " now() - %s::interval)", (name, start, turn_id, age))
        marker = registry.pending_spend_get(db, name)
        self.assertIsNone(marker["spend_key"])
        return marker

    @staticmethod
    def _epoch(created_at: str) -> float:
        return datetime.strptime(
            created_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc).timestamp()

    def _legacy_marker_at(self, db, name: str, *, offset: str,
                          turn_id: str | None = None) -> dict:
        """Marqueur d'avant 0471 posé à `seconde tronquée + offset`."""
        db.execute(
            "INSERT INTO spend_pending (agent, start_index, turn, model, turn_id,"
            " created_at) VALUES (%s, 0, 'tour', 'm', %s,"
            " date_trunc('second', now()) - interval '5 minutes'"
            " + %s::interval)", (name, turn_id, offset))
        marker = registry.pending_spend_get(db, name)
        self.assertIsNone(marker["spend_key"])
        return marker

    def _historical_key(self, name: str, marker: dict) -> str:
        """La clé écrite par l'ancien exécuteur, à la précision de sa connexion."""
        return "%s:0:%.6f" % (name, float(marker["created_ts"]))

    def test_reparation_avant_0471_ignore_une_ligne_ancienne_tardive(self):
        """B1 : la ligne d'un ancien worker écrite APRÈS le marqueur B n'est
        pas celle de B ; sa réservation ouverte, elle, identifie B.

        #1469 autorise l'ancien worker à terminer après son remplacement. La
        clé durable se retrouve par l'identité du tour (`turn_id`), jamais par
        l'ordre des écritures.
        """
        db = self._db(0)
        try:
            name = "late-a"
            marker = self._legacy_marker_row(db, name, turn_id="B", age="1 minute")
            old_key = "%s:0:%.6f" % (name, float(marker["created_ts"]) - 300.0)
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " turn_id, started_at, recorded_at)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, 'A',"
                " now() - interval '5 minutes', now())", (name, old_key))
            reservation_key = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open', %s, now())",
                (name, reservation_key))

            relu = registry.pending_spend_get(db, name)
            self.assertFalse(relu["ledger_ambiguous"])
            self.assertEqual(relu["ledger_key"], reservation_key,
                             "la clé de l'ancien worker A a été attribuée à B")

            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(relu))
            lignes = db.query(
                "SELECT spend_key FROM turn_costs WHERE agent = %s ORDER BY id",
                (name,))
            self.assertEqual([ligne["spend_key"] for ligne in lignes],
                             [old_key, reservation_key])
            reservation = db.query(
                "SELECT state FROM account_reservations WHERE spend_key = %s",
                (reservation_key,))[0]
            self.assertEqual(reservation["state"], "kept")
            self.assertIsNone(registry.pending_spend_get(db, name))
        finally:
            db.close()

    def test_reparation_avant_0471_ignore_une_ligne_ancienne_par_son_debut(self):
        """Même sans `turn_id` (marqueur d'avant 0423), `started_at` porte
        l'instant du tour : la ligne de A, commencée avant B, est écartée."""
        db = self._db(3)
        try:
            name = "late-start"
            marker = self._legacy_marker_row(db, name, age="1 minute")
            old_key = "%s:0:%.6f" % (name, float(marker["created_ts"]) - 300.0)
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " started_at, recorded_at)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s,"
                " now() - interval '5 minutes', now())", (name, old_key))
            reservation_key = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open', %s, now())",
                (name, reservation_key))

            relu = registry.pending_spend_get(db, name)
            self.assertFalse(relu["ledger_ambiguous"])
            self.assertEqual(relu["ledger_key"], reservation_key)

            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(relu))
            self.assertIsNone(registry.pending_spend_get(db, name))
        finally:
            db.close()

    def test_reparation_avant_0471_ambigue_suspend_sans_deviner(self):
        """B1 : ligne et réservation indistinguables -> aucune clé, suspension.

        L'ancienne ligne A n'a ni `turn_id` ni `started_at`, et partage
        l'index de début de B : deux clés candidates. Choisir l'une réglerait
        le mauvais tour ; le marqueur et la réservation restent en place.
        """
        db = self._db(3)
        try:
            name = "ambiguous"
            marker = self._legacy_marker_row(db, name, age="1 minute")
            old_key = "%s:0:%.6f" % (name, float(marker["created_ts"]) - 300.0)
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " recorded_at) VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, now())",
                (name, old_key))
            reservation_key = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open', %s, now())",
                (name, reservation_key))

            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"])
            worker = self._worker(db, name)
            self.assertIsNone(worker._spend_key(0, relu))
            self.assertFalse(worker._compta_ecrit(relu))
            self.assertTrue(worker._compta_en_echec)
            self.assertIsNotNone(registry.pending_spend_get(db, name),
                                 "le marqueur ambigu a été effacé")
            reservation = db.query(
                "SELECT state FROM account_reservations WHERE spend_key = %s",
                (reservation_key,))[0]
            self.assertEqual(reservation["state"], "open")
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 1,
                "la réparation ambiguë a écrit une ligne")
        finally:
            db.close()

    def test_index_reutilise_reservation_etrangere_settled(self):
        """B1 : une réservation settled d'un index réutilisé n'est pas celle
        du marqueur ; seule, elle rend la clé indémontrable (suspension)."""
        db = self._db(0)
        try:
            name = "foreign-settled"
            marker = self._legacy_marker_row(db, name, turn_id="B", age="1 minute")
            foreign = "%s:0:%.6f" % (name, float(marker["created_ts"]) - 86400.0)
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " turn_id, started_at, recorded_at)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, 'A',"
                " now() - interval '1 day', now())", (name, foreign))
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'settled', %s,"
                " now() - interval '1 day')", (name, foreign))

            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"],
                              "la réservation d'un index réutilisé a été retenue")
            worker = self._worker(db, name)
            self.assertFalse(worker._compta_ecrit(relu))
            self.assertIsNotNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 1,
                "la réparation a écrit une ligne pour la mauvaise réservation")
        finally:
            db.close()

    def test_index_reutilise_reservation_etrangere_open(self):
        """B1 : une réservation ouverte au même index mais à un autre instant
        n'est pas attribuable au marqueur ; la comptabilité suspend."""
        db = self._db(3)
        try:
            name = "foreign-open"
            marker = self._legacy_marker_row(db, name, age="1 minute")
            foreign = "%s:0:%.6f" % (name, float(marker["created_ts"]) - 86400.0)
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open', %s,"
                " now() - interval '1 day')", (name, foreign))

            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"])
            worker = self._worker(db, name)
            self.assertFalse(worker._compta_ecrit(relu))
            self.assertIsNotNone(registry.pending_spend_get(db, name))
            reservation = db.query(
                "SELECT state FROM account_reservations WHERE spend_key = %s",
                (foreign,))[0]
            self.assertEqual(reservation["state"], "open")
        finally:
            db.close()

    def test_index_reutilise_ligne_repli_etrangere(self):
        """B1 : au repli sans turn_id ni started_at, l'époque de la clé doit
        être celle du marqueur ; une ligne étrangère unique suspend."""
        db = self._db(3)
        try:
            name = "foreign-row"
            marker = self._legacy_marker_row(db, name, age="1 minute")
            foreign = "%s:0:%.6f" % (name, float(marker["created_ts"]) - 86400.0)
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " recorded_at) VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, now())",
                (name, foreign))

            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"],
                              "la ligne d'un index réutilisé a été retenue")
            worker = self._worker(db, name)
            self.assertFalse(worker._compta_ecrit(relu))
            self.assertIsNotNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 1)
        finally:
            db.close()

    def test_reservation_historique_unique_est_retrouvee(self):
        """Une réservation du tour, même settled, se retrouve par l'époque de
        sa clé (précision variable) et non par le seul index."""
        db = self._db(0)
        try:
            name = "genuine-settled"
            marker = self._legacy_marker_row(db, name, turn_id="B", age="1 minute")
            genuine = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'settled', %s,"
                " now())", (name, genuine))

            relu = registry.pending_spend_get(db, name)
            self.assertFalse(relu["ledger_ambiguous"])
            self.assertEqual(relu["ledger_key"], genuine)
            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(relu))
            self.assertIsNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT spend_key FROM turn_costs WHERE agent = %s",
                         (name,))[0]["spend_key"], genuine)
        finally:
            db.close()

    def test_reservation_etrangere_settled_ignoree_si_ligne_genuine(self):
        """Une ligne du tour identifiée par `turn_id` prime : une vieille
        réservation settled au même index ne bloque pas sa réparation."""
        db = self._db(0)
        try:
            name = "genuine-row"
            marker = self._legacy_marker_row(db, name, turn_id="B", age="1 minute")
            genuine = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd, spend_key,"
                " turn_id, started_at, recorded_at)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s, 'B',"
                " now() - interval '1 minute', now())", (name, genuine))
            foreign = "%s:0:%.6f" % (name, float(marker["created_ts"]) - 86400.0)
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'settled', %s,"
                " now() - interval '1 day')", (name, foreign))

            relu = registry.pending_spend_get(db, name)
            self.assertFalse(relu["ledger_ambiguous"])
            self.assertEqual(relu["ledger_key"], genuine)
            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(relu))
            self.assertIsNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 1)
        finally:
            db.close()

    def test_cle_historique_arrondie_a_la_seconde_est_retrouvee(self):
        """B1 : `extra_float_digits=-5` arrondit l'époque à la seconde entière.

        À 10 chiffres significatifs, une époque actuelle (~1,8e9 s) perd ses
        décimales : la clé authentique vaut `…:S.000000`, à 0,5 s près du
        marqueur, pas à 123 ms. Elle doit être retrouvée par l'arrondi exact
        (vers le bas, vers le haut, aux bornes), pas par une fenêtre.
        """
        for offset in ("0.456789", "0.499999", "0.500000", "0.500001",
                       "0.567890"):
            with self.subTest(offset=offset):
                db = self._db(-5)
                try:
                    name = "rounded" + offset.replace(".", "")
                    marker = self._legacy_marker_at(db, name, offset=offset,
                                                    turn_id="B")
                    key = self._historical_key(name, marker)
                    self.assertTrue(key.endswith(".000000"),
                                    "la clé n'a pas été arrondie à la seconde")
                    exact = self._epoch(marker["created_at"])
                    key_epoch = float(key.rsplit(":", 1)[1])
                    self.assertGreater(
                        abs(key_epoch - exact), 0.2,
                        "le cas ne dépasse pas l'ancienne tolérance")
                    self.assertLess(abs(key_epoch - exact), 0.5 + 1e-6)
                    db.execute(
                        "INSERT INTO account_reservations (billing_account, agent,"
                        " usd, period, state, spend_key, created_at)"
                        " VALUES ('deepseek-manaty', %s, 2.0, current_date,"
                        " 'open', %s, now())", (name, key))

                    relu = registry.pending_spend_get(db, name)
                    self.assertFalse(relu["ledger_ambiguous"],
                                     "la clé authentique a été jugée étrangère")
                    self.assertEqual(relu["ledger_key"], key)

                    worker = self._worker(db, name)
                    self.assertTrue(worker._compta_ecrit(relu))
                    self.assertIsNone(registry.pending_spend_get(db, name))
                    lignes = db.query(
                        "SELECT spend_key FROM turn_costs WHERE agent = %s",
                        (name,))
                    self.assertEqual([ligne["spend_key"] for ligne in lignes], [key])
                    reservation = db.query(
                        "SELECT state FROM account_reservations WHERE spend_key = %s",
                        (key,))[0]
                    self.assertEqual(reservation["state"], "kept")
                finally:
                    db.close()

    def test_cle_historique_ne_s_elargit_pas_a_une_fenetre(self):
        """B1 : une clé étrangère à 0,1 s n'est pas un arrondi du marqueur.

        L'ancienne fenêtre de 200 ms l'aurait retenue ; l'identité se prouve
        par égalité avec un arrondi historique possible, jamais par proximité.
        """
        db = self._db(-5)
        try:
            name = "rounded-foreign"
            marker = self._legacy_marker_at(db, name, offset="0.456789")
            exact = self._epoch(marker["created_at"])
            foreign_epoch = exact - 0.1
            foreign = "%s:0:%.6f" % (name, foreign_epoch)
            self.assertLess(abs(foreign_epoch - exact), 0.2,
                            "le cas n'éprouve pas l'ancienne fenêtre")
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open', %s,"
                " now())", (name, foreign))

            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"],
                              "une réservation étrangère à 0,1 s a été retenue")
            worker = self._worker(db, name)
            self.assertFalse(worker._compta_ecrit(relu))
            self.assertIsNotNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 0)
        finally:
            db.close()

    def test_reservation_etrangere_meme_seconde_avant_le_marqueur(self):
        """B1 : au même arrondi d'une seconde, une réservation antérieure au
        marqueur ne peut pas être celle de ce tour (la réservation suit la
        pose du marqueur) ; elle suspend au lieu d'être retenue."""
        db = self._db(-5)
        try:
            name = "rounded-before-res"
            marker = self._legacy_marker_at(db, name, offset="0.456789")
            key = self._historical_key(name, marker)
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open',"
                " %s, %s::timestamptz - interval '0.2 seconds')",
                (name, key, marker["created_at"]))

            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"],
                              "une réservation antérieure au marqueur a été retenue")
            worker = self._worker(db, name)
            self.assertFalse(worker._compta_ecrit(relu))
            self.assertIsNotNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 0)
        finally:
            db.close()

    def test_ligne_etrangere_meme_arrondi_avant_le_marqueur(self):
        """B1 : une ligne du même arrondi mais antérieure au marqueur est
        étrangère ; la retenir attribuerait la clé d'un tour révolu."""
        db = self._db(-5)
        try:
            name = "rounded-before-row"
            marker = self._legacy_marker_at(db, name, offset="0.456789")
            key = self._historical_key(name, marker)
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd,"
                " spend_key, recorded_at)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s,"
                " %s::timestamptz - interval '0.2 seconds')",
                (name, key, marker["created_at"]))

            relu = registry.pending_spend_get(db, name)
            self.assertTrue(relu["ledger_ambiguous"])
            self.assertIsNone(relu["ledger_key"],
                              "une ligne antérieure au marqueur a été retenue")
            worker = self._worker(db, name)
            self.assertFalse(worker._compta_ecrit(relu))
            self.assertIsNotNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 1)
        finally:
            db.close()

    def test_ligne_avant_0423_arrondie_est_retrouvee(self):
        """B1 : `started_at` et clé arrondis à la seconde désignent le tour.

        Une réparation après 0423 écrit `started_at` depuis le float8 relu :
        sous -5, les deux portent la seconde entière. Les écarter laisserait
        la réparation écrire une seconde ligne sous la clé ISO de repli.
        """
        db = self._db(-5)
        try:
            name = "rounded-line"
            marker = self._legacy_marker_at(db, name, offset="0.456789")
            key = self._historical_key(name, marker)
            debut = float(key.rsplit(":", 1)[1])
            db.execute(
                "INSERT INTO turn_costs (agent, harness, turn, model, usd,"
                " spend_key, started_at, recorded_at)"
                " VALUES (%s, 'deepseek', 'tour', 'm', 1.5, %s,"
                " to_timestamp(%s), now())", (name, key, debut))

            relu = registry.pending_spend_get(db, name)
            self.assertFalse(relu["ledger_ambiguous"])
            self.assertEqual(relu["ledger_key"], key,
                             "la ligne du tour n'a pas été retrouvée")

            worker = self._worker(db, name)
            self.assertTrue(worker._compta_ecrit(relu))
            self.assertIsNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 1,
                "la réparation a écrit une seconde ligne")
        finally:
            db.close()

    def test_repli_iso_rejoue_apres_echec_d_effacement(self):
        """N1 : le repli ISO d'un marqueur d'avant 0471 doit être relu.

        Écriture réussie puis effacement raté : au rejeu, la ligne porte la
        clé ISO `agent:index:created_at`. `cle_epoch` doit la lire comme son
        époque, sinon la ligne passe pour étrangère et la suspension devient
        définitive.
        """
        db = self._db(3)
        try:
            name = "iso-replay"
            marker = self._legacy_marker_row(db, name, age="1 minute")
            relu = registry.pending_spend_get(db, name)
            key = relu["ledger_key"]
            self.assertTrue(key.startswith(name + ":0:"))
            self.assertIn("T", key, "le repli attendu n'est pas ISO")

            worker = self._worker(db, name)
            with mock.patch.object(registry, "pending_spend_clear",
                                   return_value=False):
                self.assertTrue(worker._compta_ecrit(relu))
            self.assertIsNotNone(registry.pending_spend_get(db, name),
                                 "l'effacement n'a pas été simulé")

            relu2 = registry.pending_spend_get(db, name)
            self.assertFalse(relu2["ledger_ambiguous"],
                             "la ligne ISO a été jugée étrangère au rejeu")
            self.assertEqual(relu2["ledger_key"], key)
            self.assertTrue(worker._compta_ecrit(relu2))
            self.assertIsNone(registry.pending_spend_get(db, name))
            self.assertEqual(
                db.query("SELECT count(*) AS n FROM turn_costs WHERE agent = %s",
                         (name,))[0]["n"], 1,
                "le rejeu a doublé la ligne")
        finally:
            db.close()

    def test_get_ne_melange_pas_un_marqueur_remplace_a_sa_cle(self):
        """B1 : un remplaçant qui écrit entre les deux lectures.

        L'ancien code lisait le marqueur, puis cherchait la clé ; le
        remplaçant faisait attribuer SA clé à l'ancien `created_at`. La
        résolution en une instruction rend les deux cohérents.
        """
        db = self._db(0)
        try:
            name = "race"
            marker = self._legacy_marker_row(db, name, turn_id="B", age="1 minute")
            key_b = "%s:0:%.6f" % (name, float(marker["created_ts"]))
            db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES ('deepseek-manaty', %s, 2.0, current_date, 'open', %s, now())",
                (name, key_b))
            key_c = "%s:0:%.6f" % (name, float(marker["created_ts"]) + 10.0)
            original = db.query
            remplace = {"fait": False}

            def query(sql, params=()):
                rows = original(sql, params)
                if not remplace["fait"]:
                    remplace["fait"] = True
                    # le remplaçant écrit APRÈS la lecture du marqueur
                    db.execute("DELETE FROM spend_pending WHERE agent = %s", (name,))
                    db.execute(
                        "INSERT INTO spend_pending (agent, start_index, turn, model,"
                        " turn_id, spend_key, created_at)"
                        " VALUES (%s, 0, 'tour', 'm', 'C', %s, now())",
                        (name, key_c))
                    db.execute(
                        "UPDATE account_reservations SET spend_key = %s"
                        " WHERE agent = %s", (key_c, name))
                return rows

            with mock.patch.object(db, "query", side_effect=query):
                row = storage.of(db).pending_spend.get(name)
            self.assertEqual(row["created_at"], marker["created_at"])
            self.assertEqual(row["ledger_key"], key_b,
                             "la clé du remplaçant a été attribuée à ce tour")
            self.assertNotEqual(row["ledger_key"], key_c)
        finally:
            db.close()

    def test_get_lit_le_marqueur_et_sa_cle_en_une_seule_instruction(self):
        """B1 : pas de course entre le SELECT du marqueur et celui de la clé.

        Un remplaçant qui écrit entre les deux lectures ferait attribuer sa
        clé à l'ancien `created_at`. La résolution est une seule instruction.
        """
        db = self._db(0)
        try:
            name = "atomic"
            self._legacy_marker_row(db, name)
            api = storage.of(db).pending_spend
            with mock.patch.object(db, "query", wraps=db.query) as query:
                row = api.get(name)
            self.assertIsNotNone(row)
            self.assertEqual(query.call_count, 1,
                             "le marqueur et sa clé sont lus séparément")
        finally:
            db.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
