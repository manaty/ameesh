# SPDX-License-Identifier: AGPL-3.0-only
"""#6323 : raccord du rapport U1 (#6313) aux enveloppes U2 (#6315) et à la
corrélation U4 (#6317).

Témoins du lot : A1 construit ses enveloppes par
``repair_signals.from_alert``/``summary`` (clé ``data->>'key'`` des
``alert.raised``) ; A4 corrèle par ``repair_correlation.correlate`` avec les
vues ``active_view``/``journal_view`` ; une alerte qui ne porte pas de révision
observée reste ``inconnu``, comptée et motivée, jamais un zéro inventé ; une
alerte sans clé est refusée sans arrêter le rapport et sans recopier son texte.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from ameesh import repair_correlation, repair_report, repair_signals

from .support import PgTestCase

#: 2026-11-08T11:30:00Z, la borne de l'étude ; fenêtre de sept jours.
END = datetime(2026, 11, 8, 11, 30, tzinfo=timezone.utc).timestamp()
START = END - 7 * 86400
SHA = "a" * 40
KEY = "c" * 64
KEY2 = "d" * 64


def alert_row(**overrides):
    """Une ligne de ``_SIGNAL_ALERTS_SQL`` telle que la requête la rend."""
    values = {"cle": KEY, "alerte": "slo_breach", "hote": "ameesh-agents-1",
              "revision": "", "version_observee": "", "depuis": None,
              "cree": END - 3600.0}
    values.update(overrides)
    return values


def build(**overrides):
    values = dict(
        project="ameesh", at=END, start=START, end=END,
        cohort=None, alerts=[], occurrences=None,
        open_duplicates=None, duplicate_links=None,
        qa_returns=None, staging=[],
        signal_alerts=[], active={}, journal=[],
    )
    values.update(overrides)
    return repair_report.build(**values)


def capability(report, ident):
    return next(entry for entry in report["capabilities"]
                if entry["id"] == ident)


def active(sha=SHA, host="ameesh-agents-1", at=None):
    """La vue ``active_view`` d'un hôte attesté (format staging_pending)."""
    return {host: {"sha": sha, "absent": False,
                   "at": END - 3600.0 if at is None else at,
                   "attestation": "témoin"}}


class A1Test(unittest.TestCase):
    """Le rapport construit les enveloppes U2 et publie le résumé A1."""

    def test_enveloppe_et_resume_par_source(self):
        report = build(signal_alerts=[alert_row()])
        a1 = capability(report, "A1")["counters"]
        self.assertEqual(a1["signaux_recus"], 1)
        self.assertEqual(a1["signaux_occurrences"], 1)
        self.assertEqual(a1["signaux_par_source"], {"alerts": 1})
        self.assertEqual(a1["signaux_par_environnement"], {"staging": 1})
        self.assertEqual(a1["signaux_par_issue"], {"pending": 1})
        self.assertEqual(a1["signaux_sans_cle_cause"], 1)
        self.assertEqual(a1["signaux_sans_version_observee"], 1)
        self.assertEqual(a1["signaux_sans_date_observation"], 0)
        self.assertEqual(a1["signaux_refuses"], 0)
        self.assertEqual(a1["signaux_refuses_raisons"], {})
        # l'enveloppe est bien celle du contrat U2 (lecture stricte)
        signal = repair_signals.from_alert(
            {"schema": "ameesh-alert/1", "key": KEY, "project": "ameesh",
             "since": END - 3600.0},
            project="ameesh", environment="staging",
            received_at=repair_report.iso_z(END))
        self.assertEqual(signal.record()["schema"], repair_signals.SCHEMA)
        self.assertEqual(signal.record()["source"], "alerts")
        self.assertEqual(signal.record()["native_id"], KEY)
        self.assertEqual(signal.record()["observed_at"],
                         repair_report.iso_z(END - 3600))

    def test_alerte_sans_cle_refusee_sans_arreter_le_rapport(self):
        report = build(signal_alerts=[alert_row(cle=None),
                                      alert_row(cle=KEY2, cree=END - 60.0)])
        a1 = capability(report, "A1")["counters"]
        self.assertEqual(a1["signaux_recus"], 1)
        self.assertEqual(a1["signaux_refuses"], 1)
        self.assertIn("alerts: an alert key is required",
                      a1["signaux_refuses_raisons"])
        # un refus ne recopie aucun texte de l'alerte
        self.assertNotIn("slo_breach", json.dumps(a1))

    def test_sans_aucune_alerte_le_zero_est_mesure(self):
        report = build(signal_alerts=[])
        a1 = capability(report, "A1")
        self.assertEqual(a1["counters"]["signaux_recus"], 0)
        self.assertEqual(a1["counters"]["signaux_refuses"], 0)
        self.assertNotIn("signaux_recus", a1["unknown"])
        a4 = capability(report, "A4")
        self.assertEqual(a4["counters"]["signaux_correles"], 0)
        self.assertNotIn("signaux_correles", a4["unknown"])

    def test_resume_non_lu_reste_une_inconnue_nommee(self):
        report = build(signal_alerts=None, active=None, journal=None)
        a1 = capability(report, "A1")
        self.assertIsNone(a1["counters"]["signaux_recus"])
        self.assertIn("signaux_recus", a1["unknown"])
        self.assertTrue(a1["unknown"]["signaux_recus"])
        self.assertIsNone(capability(report, "A4")["counters"]["signaux_correles"])


class A4Test(unittest.TestCase):
    """Le rapport corrèle les enveloppes U2 à la version active (U4)."""

    def test_revision_active_correlee(self):
        report = build(signal_alerts=[alert_row(revision=SHA)],
                       active=active(SHA))
        a4 = capability(report, "A4")["counters"]
        self.assertEqual(a4["signaux_correles"], 1)
        self.assertEqual(a4["correlations_inconnues"], 0)
        self.assertEqual(a4["correlations_par_verdict"], {"version-active": 1})
        self.assertEqual(a4["correlations_raisons"], {})
        self.assertEqual(
            capability(report, "A1")["counters"]["signaux_sans_version_observee"],
            0)

    def test_sans_revision_reste_inconnu_et_compte(self):
        report = build(signal_alerts=[alert_row()], active=active(SHA))
        a4 = capability(report, "A4")["counters"]
        self.assertEqual(a4["signaux_correles"], 0)
        self.assertEqual(a4["correlations_inconnues"], 1)
        self.assertEqual(a4["correlations_par_verdict"], {"inconnu": 1})
        self.assertEqual(a4["correlations_raisons"],
                         {"révision observée absente ou illisible": 1})
        # zéro corrélé est une mesure réelle, pas une inconnue
        self.assertNotIn("signaux_correles", capability(report, "A4")["unknown"])

    def test_version_anterieure_avec_ascendance_injectee(self):
        report = build(signal_alerts=[alert_row(revision="b" * 40)],
                       active=active(SHA),
                       is_ancestor=lambda observe, actif: observe == "b" * 40)
        a4 = capability(report, "A4")["counters"]
        self.assertEqual(a4["correlations_par_verdict"],
                         {"version-anterieure": 1})
        self.assertEqual(a4["signaux_correles"], 1)

    def test_sans_hote_aucune_portee_supposee(self):
        report = build(signal_alerts=[alert_row(revision=SHA, hote=None)],
                       active=active(SHA))
        a4 = capability(report, "A4")["counters"]
        self.assertEqual(a4["signaux_correles"], 0)
        self.assertEqual(a4["correlations_raisons"],
                         {"aucun hôte à corréler": 1})

    def test_vue_des_attestations_absente_reste_inconnue(self):
        report = build(signal_alerts=[alert_row(revision=SHA)], active=None)
        a1 = capability(report, "A1")["counters"]
        self.assertEqual(a1["signaux_recus"], 1)
        a4 = capability(report, "A4")
        self.assertIsNone(a4["counters"]["signaux_correles"])
        self.assertIsNone(a4["counters"]["correlations_par_verdict"])
        self.assertIn("signaux_correles", a4["unknown"])
        self.assertTrue(a4["unknown"]["signaux_correles"])


class RepairReportRaccordDatabaseTest(PgTestCase):
    """Les deux raccords sur le schéma réel : lecture seule, bornes et clés."""

    def setUp(self):
        super().setUp()
        # Le journal est append-only : le vider ne passe pas par le DELETE gardé.
        self.db.execute("TRUNCATE ameesh_events, ameesh_events_pruned")

    def raised(self, key, created, *, host=None, revision=None):
        data = {"alert": "slo_breach", "key": key}
        if host is not None:
            data["host"] = host
        if revision is not None:
            data["revision"] = revision
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, project, data, created_at)"
            " VALUES ('alert.raised', %s, 'ameesh', %s::jsonb, to_timestamp(%s))",
            ("alert:slo_breach", json.dumps(data), created))

    def attested(self, sha, created, host="ameesh-agents-1"):
        data = {"schema": "ameesh-staging-actif/1", "sha": sha,
                "date": repair_report.iso_z(created),
                "hotes": {host: {"actif": sha}}, "reportes": {}}
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, project, data, created_at)"
            " VALUES ('staging.attested', %s, 'ameesh', %s::jsonb, to_timestamp(%s))",
            ("staging:" + sha, json.dumps(data), created))

    def test_read_raccorde_a1_et_a4(self):
        self.raised(KEY, END - 3600.0, host="ameesh-agents-1", revision=SHA)
        self.raised(KEY2, END - 1800.0, host="ameesh-agents-1")
        self.raised("e" * 64, END - 900.0)
        self.attested(SHA, END - 7200.0)
        # La vue réelle lit le dossier d'attestations du poste (#5804) : le
        # témoin injecte les deux vues, jamais l'état d'exploitation de l'hôte.
        with mock.patch.object(repair_correlation, "active_view",
                               return_value=active(SHA)), \
                mock.patch.object(repair_correlation, "journal_view",
                                 return_value=[]):
            report = repair_report.read(
                self.db, project="ameesh", now=END,
                start=repair_report.iso_z(START),
                end=repair_report.iso_z(END))
        a1 = capability(report, "A1")["counters"]
        self.assertEqual(a1["signaux_recus"], 3)
        self.assertEqual(a1["signaux_occurrences"], 3)
        self.assertEqual(a1["signaux_par_source"], {"alerts": 3})
        self.assertEqual(a1["signaux_par_environnement"], {"staging": 3})
        self.assertEqual(a1["signaux_par_issue"], {"pending": 3})
        self.assertEqual(a1["signaux_sans_version_observee"], 2)
        self.assertEqual(a1["signaux_refuses"], 0)
        a4 = capability(report, "A4")["counters"]
        self.assertEqual(a4["signaux_correles"], 1)
        self.assertEqual(a4["correlations_inconnues"], 2)
        self.assertEqual(a4["correlations_par_verdict"],
                         {"inconnu": 2, "version-active": 1})
        self.assertEqual(a4["correlations_raisons"],
                         {"révision observée absente ou illisible": 2})
        # le rapport n'écrit rien : les trois alertes et l'attestation sont
        # toujours là, et aucun lot n'a été ouvert
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM ameesh_events")[0]["n"], 4)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM work_items")[0]["n"], 0)

    def test_borne_de_fin_exclusive(self):
        self.raised(KEY, END - 3600.0, host="ameesh-agents-1")
        self.raised(KEY2, END)
        with mock.patch.object(repair_correlation, "active_view",
                               return_value=active(SHA)), \
                mock.patch.object(repair_correlation, "journal_view",
                                 return_value=[]):
            report = repair_report.read(
                self.db, project="ameesh", now=END,
                start=repair_report.iso_z(START),
                end=repair_report.iso_z(END))
        self.assertEqual(
            capability(report, "A1")["counters"]["signaux_recus"], 1)
