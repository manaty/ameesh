# SPDX-License-Identifier: AGPL-3.0-only
"""#6313 : rapport A1–A12 en lecture seule (étude #6308, unité U1).

Témoins demandés par le lot : fenêtre UTC fixe, alertes ≠ défauts,
événements ≠ runs, valeurs inconnues, cohorte et liens.
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from ameesh import repair_report, work

from .support import PgTestCase

#: 2026-11-08T11:30:00Z, la borne de l'étude ; fenêtre de sept jours.
END = datetime(2026, 11, 8, 11, 30, tzinfo=timezone.utc).timestamp()
START = END - 7 * 86400

COHORT = {"lots": 197, "empreinte_lots": 10, "empreinte_cles": 2,
          "remplaces": 31, "recidives": 8, "recidives_cle_paquet": 8}
ALERTS = [
    {"alerte": "dead_runner", "levees": 1, "sans_reference_lot": 1},
    {"alerte": "decision_overdue", "levees": 3, "sans_reference_lot": 0},
    {"alerte": "decision_pending", "levees": 10, "sans_reference_lot": 0},
]
STAGING = [
    {"type": "staging.attested", "evenements": 14, "runs": 0},
    {"type": "staging.sequence", "evenements": 1426, "runs": 29},
]


def run(**overrides):
    values = dict(
        project="ameesh", at=END, start=START, end=END,
        cohort=dict(COHORT), alerts=list(ALERTS),
        occurrences={"notes": 3, "lots": 1}, open_duplicates=[],
        duplicate_links={"lots": 4}, qa_returns={"reprises": 19, "lots": 14},
        staging=list(STAGING),
        # #6323 : les raccords U2/U4 sont lus à vide par défaut ; leurs
        # témoins vivent dans tests/test_6323_repair_report_raccord.py.
        signal_alerts=[], active={}, journal=[],
    )
    values.update(overrides)
    return repair_report.build(**values)


def capability(report, ident):
    return next(entry for entry in report["capabilities"] if entry["id"] == ident)


class WindowTest(unittest.TestCase):
    """La fenêtre est fixe, UTC et à borne de fin exclusive."""

    def test_fenetre_explicite_conservee_a_la_seconde(self):
        start, end = repair_report.window(start="2026-11-01T11:30:00Z",
                                          end="2026-11-08T11:30:00Z")
        self.assertEqual(repair_report.iso_z(start), "2026-11-01T11:30:00Z")
        self.assertEqual(repair_report.iso_z(end), "2026-11-08T11:30:00Z")
        self.assertEqual(end - start, 7 * 86400)

    def test_start_seul_ajoute_les_jours(self):
        start, end = repair_report.window(start="2026-11-01T11:30:00Z", days=7)
        self.assertEqual(repair_report.iso_z(end), "2026-11-08T11:30:00Z")

    def test_end_seul_retire_les_jours(self):
        start, end = repair_report.window(end="2026-11-08T11:30:00Z", days=7)
        self.assertEqual(repair_report.iso_z(start), "2026-11-01T11:30:00Z")

    def test_un_decalage_horaire_est_ramene_en_utc(self):
        start, _ = repair_report.window(start="2026-11-01T12:30:00+01:00",
                                        end="2026-11-08T11:30:00Z")
        self.assertEqual(repair_report.iso_z(start), "2026-11-01T11:30:00Z")

    def test_instant_sans_fuseau_refuse(self):
        with self.assertRaises(repair_report.RepairReportError):
            repair_report.parse_instant("2026-11-01T11:30:00")
        with self.assertRaises(repair_report.RepairReportError):
            repair_report.window(start="2026-11-01 11:30",
                                 end="2026-11-08T11:30:00Z")

    def test_fenetre_inversee_ou_vide_refusee(self):
        with self.assertRaises(repair_report.RepairReportError):
            repair_report.window(start="2026-11-08T11:30:00Z",
                                 end="2026-11-01T11:30:00Z")
        with self.assertRaises(repair_report.RepairReportError):
            repair_report.window(start="2026-11-08T11:30:00Z",
                                 end="2026-11-08T11:30:00Z")

    def test_days_borne(self):
        for days in (0, -1, repair_report.MAX_DAYS + 1):
            with self.assertRaises(repair_report.RepairReportError):
                repair_report.window(days=days)


class BuildTest(unittest.TestCase):
    """Le calcul pur : aucune base, des lignes lues comme la SQL les rend."""

    def test_douze_capacites_dans_l_ordre(self):
        report = run()
        self.assertEqual(report["schema"], "ameesh-repair-report/1")
        self.assertEqual(report["source"],
                         "docs/design/etudes/boucle-auto-reparation.md#U1")
        self.assertEqual([entry["id"] for entry in report["capabilities"]],
                         ["A%d" % number for number in range(1, 13)])

    def test_compteurs_de_l_etude(self):
        report = run()
        self.assertEqual(capability(report, "A1")["counters"],
                         {"cohorte_lots_defaut": 197, "empreinte_lots": 10,
                          "empreinte_cles": 2, "defauts_sans_lot": None,
                          # #6323 : raccord U2, lu à vide par ce témoin
                          "signaux_recus": 0, "signaux_occurrences": 0,
                          "signaux_par_source": {},
                          "signaux_par_environnement": {},
                          "signaux_par_issue": {},
                          "signaux_sans_cle_cause": 0,
                          "signaux_sans_version_observee": 0,
                          "signaux_sans_date_observation": 0,
                          "signaux_refuses": 0,
                          "signaux_refuses_raisons": {},
                          "journal_signaux": None, "journal_occurrences": None,
                          "journal_groupes": None})
        a3 = capability(report, "A3")["counters"]
        self.assertEqual(a3["occurrences_empreinte_revue"], 3)
        self.assertEqual(a3["lots_avec_occurrence"], 1)
        self.assertEqual(a3["doublons_techniques_ouverts"], 0)
        self.assertEqual(a3["doublons_reconnus"], 4)
        self.assertEqual(a3["remplaces_superseded"], 31)
        self.assertEqual(a3["recidives_empreinte"], 8)
        self.assertEqual(a3["recidives_cle_paquet"], 8)
        self.assertEqual(capability(report, "A4")["counters"]["attestations_staging"], 14)
        self.assertEqual(capability(report, "A4")["counters"]["demandes_staging"], 0)
        # #6323 : le raccord U4 est mesuré (zéro signal corrélé), pas inconnu
        a4 = capability(report, "A4")["counters"]
        self.assertEqual(a4["signaux_correles"], 0)
        self.assertEqual(a4["correlations_inconnues"], 0)
        self.assertEqual(a4["correlations_par_verdict"], {})
        self.assertEqual(a4["correlations_raisons"], {})
        self.assertNotIn("signaux_correles", capability(report, "A4")["unknown"])
        self.assertEqual(capability(report, "A6")["counters"]["retours_qa_build"], 19)
        self.assertEqual(capability(report, "A9")["counters"]["alertes_decision"], 13)
        self.assertEqual(capability(report, "A11")["counters"]["snapshots_staging"], 1426)
        self.assertEqual(capability(report, "A11")["counters"]["runs_staging"], 29)
        self.assertEqual(capability(report, "A12")["counters"]["alertes_sans_reference_lot"], 1)

    def test_alertes_ne_sont_pas_des_defauts(self):
        empty = dict(COHORT, lots=0, empreinte_lots=0, empreinte_cles=0,
                     remplaces=0, recidives=0, recidives_cle_paquet=0)
        report = run(cohort=empty, alerts=[
            {"alerte": "dead_runner", "levees": 1, "sans_reference_lot": 1},
        ])
        self.assertEqual(capability(report, "A1")["counters"]["cohorte_lots_defaut"], 0)
        self.assertEqual(capability(report, "A2")["counters"]["alertes_levees"], 1)
        self.assertEqual(capability(report, "A2")["counters"]["alertes_par_type"],
                         {"dead_runner": 1})
        self.assertEqual(capability(report, "A2")["counters"]["alertes_sans_reference_lot"], 1)
        # une alerte sans lot ne devient jamais un défaut, ni un zéro de défaut
        self.assertNotIn("alertes_levees", capability(report, "A1")["counters"])
        self.assertIsNone(capability(report, "A1")["counters"]["defauts_sans_lot"])
        # une alerte de décision n'est pas une panne : A9 la compte à part
        report = run(cohort=empty, alerts=[
            {"alerte": "decision_pending", "levees": 3, "sans_reference_lot": 0},
        ])
        self.assertEqual(capability(report, "A9")["counters"]["alertes_decision"], 3)
        self.assertEqual(capability(report, "A2")["counters"]["alertes_levees"], 3)
        self.assertEqual(capability(report, "A1")["counters"]["cohorte_lots_defaut"], 0)

    def test_evenements_ne_sont_pas_des_runs(self):
        report = run(staging=[
            {"type": "staging.sequence", "evenements": 1426, "runs": 29},
            {"type": "staging.attested", "evenements": 14, "runs": 0},
        ])
        counters = capability(report, "A11")["counters"]
        self.assertEqual(counters["snapshots_staging"], 1426)
        self.assertEqual(counters["runs_staging"], 29)
        self.assertNotEqual(counters["snapshots_staging"], counters["runs_staging"])
        self.assertEqual(capability(report, "A4")["counters"]["attestations_staging"], 14)
        # un événement sans run ne fabrique pas un run
        report = run(staging=[
            {"type": "staging.sequence", "evenements": 7, "runs": 0},
            {"type": "staging.attested", "evenements": 14, "runs": 0},
        ])
        counters = capability(report, "A11")["counters"]
        self.assertEqual(counters["snapshots_staging"], 7)
        self.assertEqual(counters["runs_staging"], 0)
        self.assertNotEqual(counters["snapshots_staging"], counters["runs_staging"])

    def test_valeur_inconnue_n_est_pas_zero(self):
        report = run(cohort=None, alerts=None, occurrences=None,
                     open_duplicates=None, duplicate_links=None,
                     qa_returns=None, staging=None, signal_alerts=None,
                     active=None, journal=None)
        for entry in report["capabilities"]:
            for key, value in entry["counters"].items():
                if value is None:
                    self.assertIn(key, entry["unknown"], key)
                    self.assertTrue(entry["unknown"][key], key)
        self.assertIsNone(capability(report, "A1")["counters"]["cohorte_lots_defaut"])
        self.assertIsNone(capability(report, "A1")["counters"]["signaux_recus"])
        self.assertIsNone(capability(report, "A4")["counters"]["signaux_correles"])
        self.assertIsNone(capability(report, "A3")["counters"]["remplaces_superseded"])
        self.assertIsNone(capability(report, "A11")["counters"]["snapshots_staging"])
        declared = sum(len(entry["unknown"]) for entry in report["capabilities"])
        self.assertEqual(report["unknown_count"], declared)
        # les inconnues structurelles sont nommées, même quand tout est lisible
        report = run(signal_alerts=None, active=None, journal=None)
        for key in ("defauts_sans_lot", "signaux_correles", "politique_presente",
                    "plans_reproduction", "preuves_verification",
                    "reprises_par_origine", "syntheses"):
            self.assertTrue(any(key in entry["unknown"]
                                for entry in report["capabilities"]), key)

    def test_zero_mesure_n_est_pas_une_inconnue(self):
        report = run(open_duplicates=[], alerts=[], staging=[])
        a3 = capability(report, "A3")
        self.assertEqual(a3["counters"]["doublons_techniques_ouverts"], 0)
        self.assertNotIn("doublons_techniques_ouverts", a3["unknown"])
        self.assertEqual(capability(report, "A2")["counters"]["alertes_levees"], 0)
        self.assertEqual(capability(report, "A11")["counters"]["snapshots_staging"], 0)
        self.assertNotIn("snapshots_staging", capability(report, "A11")["unknown"])

    def test_cohorte_et_liens_restent_distincts(self):
        report = run(cohort={"lots": 5, "empreinte_lots": 2, "empreinte_cles": 1,
                             "remplaces": 31, "recidives": 8,
                             "recidives_cle_paquet": 8},
                     duplicate_links={"lots": 4})
        self.assertEqual(report["cohort"]["lots"], 5)
        self.assertEqual(report["cohort"]["remplaces"], 31)
        a3 = capability(report, "A3")["counters"]
        self.assertEqual(a3["doublons_reconnus"], 4)
        self.assertEqual(a3["remplaces_superseded"], 31)
        self.assertNotEqual(a3["doublons_reconnus"], a3["remplaces_superseded"])

    def test_texte_affiche_inconnues_et_limites(self):
        text = repair_report.format_text(run())
        self.assertIn("A1 — Réception durable des signaux", text)
        self.assertIn("A12 — Balayage et surveillance de la boucle", text)
        self.assertIn("defauts_sans_lot", text)
        self.assertIn("inconnu", text)
        self.assertIn("alertes ≠ défauts", text)
        self.assertIn("événements ≠ runs", text)


class _RecordingTx:
    """Transaction factice : enregistre ce que `read` exécute."""

    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.calls.append(("execute", sql))

    def query(self, sql, params=()):
        self.calls.append(("query", sql))
        return []

    def query_batch(self, items):
        self.calls.append(("query_batch", len(items)))
        return [
            [{"lots": 0, "empreinte_lots": 0, "empreinte_cles": 0,
              "remplaces": 0, "recidives": 0, "recidives_cle_paquet": 0}],
            [],
            [{"notes": 0, "lots": 0}],
            [],
            [{"lots": 0}],
            [{"reprises": 0, "lots": 0}],
            [],
            # #6323 : aucune alerte lue pour les enveloppes U2
            [],
        ]


class ReadOnlyTest(unittest.TestCase):
    """Le lecteur ouvre une transaction READ ONLY avant toute mesure."""

    def test_transaction_read_only(self):
        class FakeDb:
            def __init__(self):
                self.calls = []

            def transaction(self):
                return _RecordingTx(self.calls)

        fake = FakeDb()
        report = repair_report.read(fake, now=END,
                                    start=repair_report.iso_z(START),
                                    end=repair_report.iso_z(END))
        self.assertEqual(fake.calls[0], ("execute", "SET TRANSACTION READ ONLY"))
        self.assertEqual(fake.calls[1], ("query_batch", 8))
        self.assertTrue(report["read_only"])


class _KeepOpen:
    """Garde la connexion du test ouverte quand la CLI ferme son wrapper."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


class RepairReportDatabaseTest(PgTestCase):
    """Les requêtes contre le schéma réel, bornes et liens compris."""

    def setUp(self):
        super().setUp()
        # Le journal est append-only : le vider ne passe pas par le DELETE gardé.
        self.db.execute("TRUNCATE ameesh_events, ameesh_events_pruned")

    def lot(self, title, *, created, type="bug", app="ameesh", source=""):
        item = work.add(self.db, title=title, type=type, app=app, source=source)
        self.db.execute(
            "UPDATE work_items SET created_at = to_timestamp(%s) WHERE id = %s",
            (created, item["id"]))
        return item["id"]

    def event(self, lot, state, note, created):
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)"
            " VALUES (%s, %s, %s, 'témoin', to_timestamp(%s))",
            (lot, state, note, created))

    def raised(self, alert, created, *, project="ameesh", lot=None):
        data = {"alert": alert}
        if lot is not None:
            data["lot"] = lot
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, project, data, created_at)"
            " VALUES ('alert.raised', %s, %s, %s::jsonb, to_timestamp(%s))",
            ("alert:" + alert, project, json.dumps(data), created))

    def staging(self, kind, created, run=None):
        data = {} if run is None else {"run": run}
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, data, created_at)"
            " VALUES (%s, %s, %s::jsonb, to_timestamp(%s))",
            (kind, kind + ":témoin", json.dumps(data), created))

    def test_read_mesures_bornes_liens_et_alertes(self):
        # Cohorte : a (à la borne start), b (dans la fenêtre) ; c (à end),
        # d (avant start), e (évolution) et f (autre app) sont exclus.
        a = self.lot("bug A (récidive de #1)", created=START,
                     source="empreinte:test:cle")
        b = self.lot("bug B", created=END - 3600)
        c = self.lot("bug C hors fenêtre", created=END)
        d = self.lot("bug D avant fenêtre", created=START - 1)
        e = self.lot("évolution E", created=START + 3600, type="evolution")
        f = self.lot("bug F autre app", created=START + 3600, app="nexlink")
        self.db.execute("UPDATE work_items SET superseded_by = %s WHERE id = %s", (b, a))
        # trois liens duplicates, mais deux lots de la cohorte seulement
        for left, right in ((a, b), (a, e), (b, f)):
            self.db.execute(
                "INSERT INTO work_item_links (work_item_id, other_id, kind, created_by)"
                " VALUES (%s, %s, 'duplicates', 'témoin')", (left, right))
        # occurrences : deux notes dans la fenêtre, une à la borne end exclue
        self.event(a, "intake", "Empreinte revue : occurrences=1", START + 60)
        self.event(a, "intake", "Empreinte revue : occurrences=2", START + 120)
        self.event(a, "intake", "Empreinte revue : occurrences=3", END)
        # deux retours qa → build sur le même lot
        for offset in (3600, 7200):
            self.event(b, "qa", "revue", START + offset)
            self.event(b, "build", "reprise", START + offset + 60)
        # alertes : une sans lot, une de décision, une autre app, une à end
        self.raised("dead_runner", START + 60)
        self.raised("decision_pending", START + 120, lot=b)
        self.raised("decision_pending", START + 180, project="nexlink")
        self.raised("decision_overdue", END)
        # staging : trois snapshots pour deux runs, une attestation, une demande
        self.staging("staging.sequence", START + 60, "r1")
        self.staging("staging.sequence", START + 120, "r1")
        self.staging("staging.sequence", START + 180, "r2")
        self.staging("staging.attested", START + 240)
        self.staging("staging.request", START + 300, "rq")
        self.staging("staging.attested", END)

        report = repair_report.read(self.db, project="ameesh",
                                    start=repair_report.iso_z(START),
                                    end=repair_report.iso_z(END))
        self.assertEqual(report["window"]["start"], "2026-11-01T11:30:00Z")
        self.assertEqual(report["window"]["end"], "2026-11-08T11:30:00Z")
        self.assertEqual(report["cohort"], {
            "lots": 2, "empreinte_lots": 1, "empreinte_cles": 1,
            "remplaces": 1, "recidives": 1, "recidives_cle_paquet": 0})
        self.assertEqual(capability(report, "A1")["counters"]["cohorte_lots_defaut"], 2)
        self.assertEqual(capability(report, "A2")["counters"]["alertes_levees"], 2)
        self.assertEqual(capability(report, "A2")["counters"]["alertes_par_type"],
                         {"dead_runner": 1, "decision_pending": 1})
        self.assertEqual(capability(report, "A2")["counters"]["alertes_sans_reference_lot"], 1)
        a3 = capability(report, "A3")["counters"]
        self.assertEqual(a3["occurrences_empreinte_revue"], 2)
        self.assertEqual(a3["lots_avec_occurrence"], 1)
        self.assertEqual(a3["doublons_techniques_ouverts"], 0)
        self.assertEqual(a3["doublons_reconnus"], 2)
        self.assertEqual(a3["remplaces_superseded"], 1)
        self.assertEqual(capability(report, "A4")["counters"]["attestations_staging"], 1)
        self.assertEqual(capability(report, "A4")["counters"]["demandes_staging"], 1)
        self.assertEqual(capability(report, "A6")["counters"]["retours_qa_build"], 2)
        self.assertEqual(capability(report, "A6")["counters"]["lots_repris"], 1)
        self.assertEqual(capability(report, "A9")["counters"]["alertes_decision"], 1)
        a11 = capability(report, "A11")["counters"]
        self.assertEqual(a11["snapshots_staging"], 3)
        self.assertEqual(a11["runs_staging"], 2)
        self.assertEqual(capability(report, "A12")["counters"]["alertes_levees"], 2)
        self.assertIsNone(capability(report, "A1")["counters"]["defauts_sans_lot"])
        # aucune écriture du lecteur : les lots sont intacts
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM work_items")[0]["n"], 6)
        # les identifiants créés restent ceux du témoin, aucun lot ouvert par le rapport
        highest = max(a, b, c, d, e, f)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM work_items WHERE id > %s",
            (highest,))[0]["n"], 0)

    def test_commande_json(self):
        out = io.StringIO()
        with mock.patch("ameesh.config.load", return_value=self.cfg), \
                mock.patch("ameesh.db.connect", return_value=_KeepOpen(self.db)), \
                redirect_stdout(out):
            code = repair_report.main(["--json", "--start",
                                       repair_report.iso_z(START), "--end",
                                       repair_report.iso_z(END)])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["schema"], "ameesh-repair-report/1")
        self.assertEqual(data["cohort"]["lots"], 0)
        self.assertEqual(data["capabilities"][0]["counters"]["cohorte_lots_defaut"], 0)
        self.assertIsNone(data["capabilities"][0]["counters"]["defauts_sans_lot"])

    def test_commande_refuse_une_fenetre_invalide_avant_la_base(self):
        err = io.StringIO()
        with mock.patch("ameesh.db.connect") as connect, \
                mock.patch("sys.stderr", err):
            code = repair_report.main(["--days", "0"])
        self.assertEqual(code, 2)
        connect.assert_not_called()
        self.assertIn("error:", err.getvalue())


class HelpTest(unittest.TestCase):
    """N du 08/11 : `--at` est accepté par la CLI et doit être documenté."""

    def test_at_documente_dans_l_aide_generale_et_celle_de_la_commande(self):
        from ameesh import main as main_mod
        usage = [line for line in (main_mod.__doc__ or "").splitlines()
                 if "repair-report [" in line]
        self.assertEqual(len(usage), 1)
        self.assertIn("--at T", usage[0])
        self.assertIn("--at", repair_report.build_parser().format_help())


class PsycopgPlaceholderTest(unittest.TestCase):
    """psycopg refuse un `%` littéral non doublé dans une requête paramétrée.

    Le pilote psql (`db.bind`) ne réécrit que `%s` : ce témoin couvre le
    pilote normal, que la suite exécute quand psycopg est disponible.
    """

    def test_chaque_requete_passe_le_parseur_psycopg(self):
        try:
            import psycopg
            from psycopg._queries import PostgresQuery
        except ImportError:
            self.skipTest("psycopg absent (pilote psql)")
        queries = {name: getattr(repair_report, name)
                   for name in dir(repair_report) if name.endswith("_SQL")}
        self.assertTrue(queries)
        for name, sql in queries.items():
            with self.subTest(name=name):
                PostgresQuery(psycopg.adapt.Transformer()).convert(
                    sql, (1,) * sql.count("%s"))
