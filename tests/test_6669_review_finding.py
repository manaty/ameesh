# SPDX-License-Identifier: AGPL-3.0-only
"""#6669 — M3 de l'étude #6650 : résolution explicite d'un constat de revue.

Témoins demandés par le lot : un B suivi d'un OK sans correction reste non
tranché ; plusieurs constats sur le même gel ; retrait d'une remarque ; témoin
sur le SHA lu ; correction par le relecteur ; adjudicateur indépendant ;
aucune modification de quorum ni de famille. M1 montre les trois états.
Témoins de relecture : l'auteur est borné au gel du constat (B2) et la
résolution ne prend aucun verrou de graphe ni n'écrit de lien typé (B1).
"""
from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from ameesh import db as D
from ameesh import model_evaluation_report as report
from ameesh import registry, review_finding, storage, work, work_dependencies
from ameesh import review_designation_pg as designation_pg

from .support import PgTestCase

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SHA_E = "e" * 40


class FindingTest(PgTestCase):
    """Un constat est un jalon `verdict blocked` ; son id l'identifie."""

    def setUp(self):
        super().setUp()
        for name, harness in (("author", "deepseek"), ("reviewer", "codex"),
                              ("reviewer2", "codex"), ("judge", "claude"),
                              ("outsider", "claude")):
            registry.upsert(self.db, name, harness=harness,
                            host=self.cfg.host, status="idle")
        self.lot = self.new_lot()

    # -- fixtures ---------------------------------------------------------

    def new_lot(self, **kw) -> int:
        kw.setdefault("title", "un constat de revue")
        kw.setdefault("app", "ameesh")
        return int(work.add(self.db, **kw)["id"])

    def freeze(self, lot: int | None = None, sha: str = SHA_A,
               actor: str = "author") -> dict:
        return storage.of(self.db).work.add_milestone(
            int(lot or self.lot), "frozen", sha=sha, actor=actor,
            verdict=None, note="")

    def mark(self, lot: int | None = None, freeze: dict | None = None, *,
             authors=(("author", "deepseek"),), unknown=(),
             reviewer: str = "reviewer", reviewer_family: str = "codex") -> str:
        """La marque de désignation du gel : auteurs prouvés et trous."""
        lot = int(lot or self.lot)
        freeze = freeze or self.freeze(lot)
        payload = {
            "state": "designated", "required": 1,
            "reviewers": [{"name": reviewer, "family": reviewer_family}],
            "missing": 0, "why": "",
            "freeze": {"id": int(freeze["id"]), "sha": str(freeze["sha"])},
            "authors": [{"name": name, "family": family}
                        for name, family in authors],
            "unknown_authors": list(unknown),
            "policy_ref": "test",
        }
        note = "%s %s" % (designation_pg.mark_prefix(lot, int(freeze["id"])),
                          json.dumps(payload, sort_keys=True))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'qa', %s, 'ameesh')", (lot, note))
        return note

    def blocked(self, lot: int | None = None, *, sha: str = SHA_A,
                actor: str = "reviewer", family: str | None = "codex",
                at: str | None = None, note: str = "un défaut") -> int:
        lot = int(lot or self.lot)
        row = self.db.query(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, at, sha, actor, verdict, note)"
            " VALUES (%s, 'verdict', coalesce(%s::timestamptz, now()), %s, %s,"
            "         'blocked', %s) RETURNING id",
            (lot, at, sha, actor, note))[0]
        milestone = int(row["id"])
        if family and sha:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, 'session', 'turn', %s, %s)",
                ("milestone:%d" % milestone, sha, actor, family, lot))
        return milestone

    def ok(self, lot: int | None = None, *, sha: str = SHA_A,
           actor: str = "reviewer", at: str | None = None) -> int:
        lot = int(lot or self.lot)
        return int(self.db.query(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', coalesce(%s::timestamptz, now()), %s, %s,"
            "         'ok') RETURNING id",
            (lot, at, sha, actor))[0]["id"])

    def read(self, lot: int | None = None) -> dict:
        return review_finding.read(self.db, int(lot or self.lot))

    def finding(self, lot: int | None = None, milestone: int | None = None) -> dict:
        value = self.read(lot)
        if milestone is None:
            return value["findings"][0]
        return next(row for row in value["findings"]
                    if row["milestone_id"] == milestone)

    def resolve(self, milestone: int, **kw):
        kw.setdefault("state", review_finding.STATE_UNDECIDED)
        kw.setdefault("adjudicator", "human:owner")
        return review_finding.resolve(self.db, self.lot, milestone, **kw)

    # -- états ------------------------------------------------------------

    def test_un_b_suivi_d_un_ok_sans_correction_reste_non_tranche(self):
        self.mark()
        finding = self.blocked()
        self.ok()
        value = self.read()
        row = value["findings"][0]
        self.assertEqual(row["milestone_id"], finding)
        self.assertEqual(row["state"], "undecided")
        self.assertFalse(row["resolved"])
        self.assertFalse(row["explicit"])
        self.assertIsNone(row["basis"])
        self.assertEqual(row["next_verdict"]["verdict"], "ok")
        self.assertEqual(value["counts"],
                         {"total": 1, "confirmed": 0, "refuted": 0,
                          "undecided": 1, "resolved": 0})

    def test_plusieurs_constats_sur_le_meme_gel(self):
        self.mark()
        first = self.blocked(actor="reviewer")
        second = self.blocked(actor="reviewer2")
        self.resolve(first, state="confirmed", correction_sha=SHA_C,
                     adjudicator="judge")
        self.resolve(second, state="refuted", basis="withdrawal",
                     withdrawn_by="reviewer2", adjudicator="judge")
        value = self.read()
        rows = {row["milestone_id"]: row for row in value["findings"]}
        self.assertEqual(rows[first]["state"], "confirmed")
        self.assertEqual(rows[first]["witness_sha"], SHA_A)
        self.assertEqual(rows[first]["correction_sha"], SHA_C)
        self.assertEqual(rows[second]["state"], "refuted")
        self.assertEqual(rows[second]["withdrawn_by"], "reviewer2")
        self.assertEqual(value["counts"]["total"], 2)
        self.assertEqual(value["counts"]["confirmed"], 1)
        self.assertEqual(value["counts"]["refuted"], 1)

    def test_retrait_d_une_remarque(self):
        self.mark()
        finding = self.blocked()
        self.resolve(finding, state="refuted", basis="withdrawal",
                     withdrawn_by="reviewer", adjudicator="judge")
        row = self.finding(milestone=finding)
        self.assertEqual(row["state"], "refuted")
        self.assertEqual(row["basis"], "withdrawal")
        self.assertEqual(row["withdrawn_by"], "reviewer")
        self.assertTrue(row["resolved"])

    def test_le_retrait_exige_le_nom_du_retrait(self):
        self.mark()
        finding = self.blocked()
        with self.assertRaises(review_finding.ReviewFindingError):
            self.resolve(finding, state="refuted", basis="withdrawal",
                         adjudicator="judge")
        self.assertEqual(self.read()["counts"]["total"], 1)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM review_finding_resolutions")[0]["n"], 0)

    # -- témoin -----------------------------------------------------------

    def test_le_temoin_est_le_sha_lu_et_ne_suit_pas_la_branche(self):
        self.mark()
        finding = self.blocked(sha=SHA_A)
        with self.assertRaises(review_finding.ReviewFindingError) as ctx:
            self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                         witness=SHA_B, adjudicator="judge")
        self.assertIn("not the read SHA", str(ctx.exception))
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM review_finding_resolutions")[0]["n"], 0)
        self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                     witness=SHA_A, adjudicator="judge")
        # la branche avance : le gel suivant ne déplace pas le témoin
        self.freeze(sha=SHA_B)
        row = self.finding(milestone=finding)
        self.assertEqual(row["witness_sha"], SHA_A)
        self.assertEqual(row["correction_sha"], SHA_C)

    def test_un_constat_sans_sha_ne_prouve_aucun_temoin(self):
        self.mark()
        finding = self.blocked(sha="")
        with self.assertRaises(review_finding.ReviewFindingError) as ctx:
            self.resolve(finding, state="undecided", adjudicator="judge")
        self.assertIn("no read SHA", str(ctx.exception))

    # -- correction et indépendance --------------------------------------

    def test_correction_par_le_relecteur_est_une_observation_pas_une_porte(self):
        self.mark()
        finding = self.blocked(actor="reviewer")
        room = {"state": "short", "remaining": 1, "missing": 1,
                "why": "§2 : retour à l'auteur"}
        with mock.patch.object(review_finding.review_correction, "check",
                               return_value=room) as checked:
            self.resolve(finding, state="confirmed", basis="correction",
                         correction_sha=SHA_C, corrector="reviewer",
                         adjudicator="judge", cfg=self.cfg)
        checked.assert_called_once()
        row = self.finding(milestone=finding)
        self.assertTrue(row["reviewer_correction"])
        self.assertEqual(row["corrector"], "reviewer")
        self.assertEqual(row["review_room"]["state"], "short")
        self.assertEqual(row["state"], "confirmed")

    def test_adjudicateur_independant_des_familles_jugees(self):
        self.mark()
        finding = self.blocked()
        for name in ("author", "reviewer"):
            with self.subTest(adjudicator=name):
                with self.assertRaises(review_finding.ReviewFindingError) as ctx:
                    self.resolve(finding, state="confirmed",
                                 correction_sha=SHA_C, adjudicator=name)
                self.assertIn("judged family", str(ctx.exception))
        with self.assertRaises(review_finding.ReviewFindingError):
            self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                         adjudicator="not-registered")
        # une famille différente, ou un humain, prouve l'indépendance
        self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                     adjudicator="judge")
        self.resolve(finding, state="undecided", adjudicator="human:owner")
        self.assertEqual(self.finding(milestone=finding)["state"], "undecided")

    def test_une_famille_d_auteur_inconnue_ne_prouve_pas_l_independance(self):
        self.mark(unknown=("commit-sans-reçu",))
        finding = self.blocked()
        with self.assertRaises(review_finding.ReviewFindingError) as ctx:
            self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                         adjudicator="judge")
        self.assertIn("not all proven", str(ctx.exception))
        # l'humain reste indépendant par autorité, même sur une preuve trouée
        self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                     adjudicator="human:owner")
        self.assertEqual(self.finding(milestone=finding)["state"], "confirmed")

    def test_l_auteur_est_borne_au_gel_du_constat_apres_un_nouveau_gel(self):
        # #6669 B2 (ameesh-cx2) : sans marque de désignation, l'auteur est lu
        # au gel que le B a lu, jamais au gel le plus récent d'une autre famille.
        # B sur la tête A (auteur claude), puis nouveau gel B (auteur deepseek) :
        # un adjudicateur claude reste refusé pour le constat A.
        self.db.execute("UPDATE work_items SET assignee = 'claude-author'"
                        " WHERE id = %s", (self.lot,))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'intake', 'assigned to claude-author (before: nobody)',"
            " 'orchestrateur')", (self.lot,))
        self.freeze(sha=SHA_A, actor="claude-author")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('claude-author', 'claude', 's-claude',"
            " now() + interval '1 second')")
        finding = self.blocked(sha=SHA_A, actor="reviewer")
        # la branche avance : nouveau gel, autre famille, autre assigné
        self.db.execute("UPDATE work_items SET assignee = 'author'"
                        " WHERE id = %s", (self.lot,))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'qa', 'assigned to author (before: claude-author)',"
            " 'orchestrateur')", (self.lot,))
        self.freeze(sha=SHA_B, actor="author")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('author', 'deepseek', 's-deepseek',"
            " now() + interval '1 second')")
        with self.assertRaises(review_finding.ReviewFindingError) as ctx:
            self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                         adjudicator="judge")
        self.assertIn("judged family", str(ctx.exception))
        # l'humain reste indépendant par autorité
        self.resolve(finding, state="undecided", adjudicator="human:owner")

    def test_aucune_modification_de_quorum_de_famille_ni_de_porte(self):
        self.mark()
        finding = self.blocked()
        before = dict(self.db.query(
            "SELECT reviewer, reviewers, assignee, state FROM work_items"
            " WHERE id = %s", (self.lot,))[0])
        mark_before = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id", (self.lot,))
        self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                     adjudicator="judge")
        after = dict(self.db.query(
            "SELECT reviewer, reviewers, assignee, state FROM work_items"
            " WHERE id = %s", (self.lot,))[0])
        self.assertEqual(before, after)
        mark_after = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id", (self.lot,))
        self.assertEqual([row["note"] for row in mark_before],
                         [row["note"] for row in mark_after
                          if not row["note"].startswith(
                              review_finding.NOTE_PREFIX)])
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM work_item_links")[0]["n"], 0)

    # -- journal additif --------------------------------------------------

    def test_le_journal_est_additif_et_le_dernier_etat_fait_foi(self):
        self.mark()
        finding = self.blocked()
        self.resolve(finding, state="confirmed", correction_sha=SHA_C,
                     adjudicator="judge")
        self.resolve(finding, state="refuted", basis="evidence",
                     note="le relecteur s'est trompé de tête",
                     adjudicator="judge")
        rows = self.db.query(
            "SELECT state, basis FROM review_finding_resolutions"
            " WHERE milestone_id = %s ORDER BY id", (finding,))
        self.assertEqual([row["state"] for row in rows],
                         ["confirmed", "refuted"])
        self.assertEqual(self.finding(milestone=finding)["state"], "refuted")
        with self.assertRaises(Exception):
            self.db.execute(
                "UPDATE review_finding_resolutions SET state = 'confirmed'"
                " WHERE milestone_id = %s", (finding,))

    def test_un_lot_sans_constat_ne_rend_aucun_constat(self):
        value = self.read()
        self.assertEqual(value["findings"], [])
        self.assertEqual(value["counts"]["total"], 0)

    # -- formes refusées --------------------------------------------------

    def test_les_formes_incoherentes_sont_refusees(self):
        self.mark()
        finding = self.blocked()
        cases = (
            dict(state="confirmed", adjudicator="judge"),  # pas de preuve
            dict(state="refuted", basis="correction", correction_sha=SHA_C,
                 adjudicator="judge"),
            dict(state="undecided", correction_sha=SHA_C,
                 adjudicator="judge"),
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(review_finding.ReviewFindingError):
                    self.resolve(finding, **case)
        other = self.new_lot()
        with self.assertRaises(review_finding.ReviewFindingError):
            review_finding.resolve(self.db, other, finding, state="undecided",
                                   adjudicator="judge")
        milestone = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=SHA_D, actor="author", verdict=None,
            note="")
        with self.assertRaises(review_finding.ReviewFindingError):
            review_finding.resolve(self.db, self.lot, int(milestone["id"]),
                                   state="undecided", adjudicator="judge")

    # -- régression -------------------------------------------------------

    def test_le_lot_de_regression_est_dans_le_journal_sans_lien_type(self):
        self.mark()
        finding = self.blocked()
        regression = self.new_lot(title="la régression")
        self.resolve(finding, state="confirmed", basis="regression",
                     regression_lot=regression, adjudicator="judge")
        row = self.finding(milestone=finding)
        self.assertEqual(row["regression_lot"], regression)
        self.assertEqual(row["basis"], "regression")
        # #6669 B1 : aucun genre `regressed_by` n'est écrit — un lecteur N-1 de
        # `links()` lèverait KeyError et `packet_candidates` refuserait le paquet.
        self.assertEqual(
            work_dependencies.links(self.db, [self.lot]).get(self.lot, []), [])
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM work_item_links")[0]["n"], 0)

    def test_un_lot_de_regression_propre_ou_inconnu_est_refuse(self):
        self.mark()
        finding = self.blocked()
        for lot in (self.lot, 999999):
            with self.subTest(lot=lot):
                with self.assertRaises(review_finding.ReviewFindingError):
                    self.resolve(finding, state="confirmed", basis="regression",
                                 regression_lot=lot, adjudicator="judge")
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM review_finding_resolutions")[0]["n"], 0)

    def test_deux_resolutions_concurrentes_ne_sont_pas_un_interblocage(self):
        # #6669 B1 : `resolve` ne prend que le verrou de lot, plus de verrou de
        # graphe (aucun lien typé) ; deux résolutions simultanées aboutissent et
        # la dernière ligne fait foi.
        self.mark()
        finding = self.blocked()
        start = threading.Barrier(2)
        errors = []

        def run(**kwargs):
            db = D.connect(self.cfg)
            try:
                start.wait(timeout=5)
                review_finding.resolve(db, self.lot, finding,
                                       adjudicator="human:owner", **kwargs)
            except Exception as exc:  # noqa: BLE001 - le témoin juge l'échec
                errors.append(exc)
            finally:
                db.close()

        threads = [
            threading.Thread(target=run, daemon=True, kwargs=dict(
                state=review_finding.STATE_CONFIRMED, correction_sha=SHA_C)),
            threading.Thread(target=run, daemon=True, kwargs=dict(
                state=review_finding.STATE_REFUTED, basis="evidence",
                note="le relecteur s'est trompé de tête")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        rows = self.db.query(
            "SELECT state FROM review_finding_resolutions"
            " WHERE milestone_id = %s ORDER BY id", (finding,))
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.finding(milestone=finding)["state"],
                         rows[-1]["state"])

    # -- M1 ---------------------------------------------------------------

    def test_m1_montre_confirme_infirme_et_non_tranche(self):
        reviews = [
            {"kind": "verdict", "verdict": "blocked", "family": "codex",
             "n": 3, "lots": 1},
            {"kind": "verdict", "verdict": "ok", "family": "codex",
             "n": 1, "lots": 1},
        ]
        resolutions = [
            {"family": "codex", "state": "confirmed", "independent": True},
            {"family": "codex", "state": "refuted", "independent": True},
            {"family": "codex", "state": None, "independent": None},
        ]
        value = report.build(
            project="ameesh", at=1750000000.0,
            start=1750000000.0 - 14 * 86400, end=1750000000.0,
            units=[], staged=[], period_project=[], period_mesh=[],
            reviews=reviews, designated=[], resolutions=resolutions)
        review = value["review"]
        self.assertEqual(review["b_resolutions"],
                         {"total": 3, "confirmed": 1, "refuted": 1,
                          "undecided": 1, "resolved": 2, "adjudicated": 2})
        row = {item["family"]: item for item in review["by_family"]}["codex"]
        self.assertEqual((row["b_confirmed"], row["b_refuted"],
                          row["b_undecided"], row["b_justes"]), (1, 1, 1, 1))
        self.assertEqual(value["coverage"]["denominators"]["b_justes_known"], 2)
        self.assertNotIn("b_justes", value["coverage"]["unknown"])
        text = report.format_text(value)
        self.assertIn("CONFIRMÉS", text)
        self.assertIn("B résolus", text)


if __name__ == "__main__":
    unittest.main()
