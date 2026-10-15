# SPDX-License-Identifier: AGPL-3.0-only
"""#1391 — la route d'un défaut non bloquant : le backlog, pas un lot (§2).

Canon §2 (owner, 15/10/2026) : « un défaut non bloquant (N, P2, P3) ne bloque
jamais. Il ne crée pas de lot : il va au backlog (`ameesh work backlog add`,
avec sa valeur et son chantier), regroupé avec les autres défauts mineurs du
même chantier ». Sur ameesh, un P2 de la classe sensible bloque encore.

Témoins : suite N détectée et orientée, suite de tests légitime non signalée
(sans masquer un N explicite), suivis ordinaires silencieux, négation de
« bloquant » et régression explicitement mineure orientées, bloquant nommé
(B/B1/P1/incident/panne) prioritaire même avec une référence N historique, P2
sensible conservé, avertissement rendu par les deux points d'entrée de création
(`work add`, `mail --new-lot`), consigne injectée au relecteur d'après fusion
citant la route, et backlog qui accepte la suite."""
from __future__ import annotations

import unittest

from ameesh import backlog, registry, review_lot, work

from .support import PgTestCase


class FollowupHintTest(unittest.TestCase):
    """La détection seule : ce qui est une suite de relecture, et ce qui ne
    l'est pas."""

    def test_a_review_suite_is_routed_to_the_backlog(self):
        for title in (
            "Suite #956 (N R2 bureau) : ORDER BY id sur le SELECT ... FOR UPDATE",
            "Suite #1037 N1 : attribuer la famille au lot produit",
            "L242c suite (N #414): Runner.is_critical sans canon",
            "P3 : compte du transport op dans EXECUTEUR-MEDIEE",
            "N : la sonde unreserved est morte",
            "suivi de la R1 : borner le diagnostic",
            "corrections R1 : N2 et N3 de la relecture",
        ):
            out = backlog.followup_hint(title=title)
            self.assertIn("backlog", out, title)
            self.assertIn("ameesh work backlog add", out, title)

    def test_the_hint_names_the_value_options_and_the_project(self):
        out = backlog.followup_hint(title="Suite #42 N2 : borner le diagnostic", app="ameesh")
        self.assertIn("--value", out)
        self.assertIn("--score", out)
        self.assertIn("--app ameesh", out)

    def test_a_real_test_suite_is_not_a_review_suite(self):
        for title in (
            "Suite de tests : couvrir la reprise de session DeepSeek",
            "TEST-FLAKE : test_panne_de_base_definitive_tue_avant_tout_journal",
            "testsuite CI : réduire le bruit des jobs",
            "suites complètes : un seul passage",
            "suites completes : un seul passage",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)

    def test_an_explicit_n_survives_a_test_suite_locution(self):
        # R1 cx2, P2 3 : « suite de tests » ne vaut pas marqueur, mais ne doit
        # pas masquer un N/P2/P3 explicite.
        for title in (
            "N2 : améliorer les diagnostics de la suite de tests",
            "P2 : suite de tests instable",
        ):
            self.assertIn("backlog", backlog.followup_hint(title=title), title)

    def test_an_ordinary_follow_up_is_not_a_review_suite(self):
        # R1 cx2, P2 2 : « suivi »/« follow-up » ne vaut marqueur que visant une
        # relecture ou une référence ; le travail ordinaire reste silencieux.
        for title in (
            "Suivi des coûts mensuels",
            "Suivi mensuel de la file",
            "Suivi de la production",
            "follow-up commercial",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)

    def test_a_blocking_defect_keeps_its_lot(self):
        for title in (
            "B1 : le feu ouvre la PR avant les verdicts",
            "P1 : perte de messages sur un hôte saturé",
            "P1 : suite de la R1 non corrigée",
            "incident de production : base indisponible",
            "panne : le relais ne redémarre plus",
            "régression : la file perd ses lignes",
            # R1 cx2, P2 1 : un bloquant explicite garde son lot même quand le
            # titre ou la source cite une suite N historique (B seul compris).
            "P1 : corriger la panne issue du N2 de la R1",
            "N2 : corriger la panne issue de la R1",
            "B : suite #956 — perte de messages",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)

    def test_a_blocking_defect_wins_over_a_historical_n_reference(self):
        # R1 cx2, P2 1 : le bloquant explicite garde son lot quand la source ne
        # cite qu'une référence N historique.
        self.assertEqual(backlog.followup_hint(
            title="P1 : corriger la panne", source="issue du N2 de la R1"), "")

    def test_a_negated_blocking_word_is_not_a_blocking_defect(self):
        # R2 cx2, P2 : « non bloquant » / « pas bloquant » qualifie le défaut,
        # ce n'est pas une occurrence positive de « bloquant » : la suite mineure
        # garde sa route vers le backlog.
        for title in (
            "non bloquant : simplifier le diagnostic",
            "pas bloquant : améliorer le libellé",
        ):
            self.assertIn("backlog", backlog.followup_hint(title=title), title)
        # un « bloquant » positif reste prioritaire, même avec un marqueur N
        self.assertEqual(backlog.followup_hint(
            title="N2 : défaut bloquant confirmé par la mesure"), "")

    def test_an_explicit_minor_regression_goes_to_the_backlog(self):
        # R2 cx2, P2 : « régression » ne vaut pas à lui seul un bloquant ; avec
        # un marqueur mineur explicite (N2), la suite va au backlog.
        out = backlog.followup_hint(title="N2 : régression cosmétique du libellé")
        self.assertIn("backlog", out)

    def test_a_plain_french_suite_keeps_its_lot(self):
        # « suite à / suite au » : du français ordinaire, souvent une demande
        # d'humain — pas une suite de relecture (canon §2, avertissement ciblé).
        for title in (
            "Suite à la demande de l'owner, ajouter un bouton d'export",
            "Suite au courrier du 15/10, préciser la consigne",
            "Faire suite à la réunion : noter la décision",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)

    def test_a_sensitive_p2_still_blocks_on_ameesh(self):
        self.assertEqual(backlog.followup_hint(title="P2 sensible : rattacher la famille",
                                               app="ameesh"), "")

    def test_a_sensitive_p2_goes_to_the_backlog_elsewhere(self):
        out = backlog.followup_hint(title="P2 sensible : rattacher la famille",
                                    app="nexlink")
        self.assertIn("backlog", out)

    def test_a_human_request_passes_without_a_hint(self):
        self.assertEqual(backlog.followup_hint(title="Ajouter un bouton d'export"), "")

    def test_the_review_lot_is_the_rule_not_a_suite(self):
        title = review_lot.title_of({"title": "petit lot"}, 123)
        self.assertEqual(backlog.followup_hint(title=title,
                                               issue_ref=review_lot.ref_of(123)), "")
        self.assertEqual(backlog.followup_hint(title=title), "")

    def test_the_ref_is_read_as_well_as_the_title(self):
        out = backlog.followup_hint(title="corrections diverses",
                                    source="suite de la relecture R2 (N1)")
        self.assertIn("backlog", out)


class ReviewInstructionTest(unittest.TestCase):
    """La consigne injectée au relecteur d'après fusion cite la route."""

    def test_the_after_merge_instruction_cites_the_backlog(self):
        body = review_lot.body_of({"title": "petit lot", "branch_target": "main"}, 7,
                                  klass={"class": "light", "why": ""},
                                  merged_sha="a" * 40)
        self.assertIn("ne crée pas de lot", body)
        self.assertIn("ameesh work backlog add", body)
        self.assertIn("P2 de la classe sensible bloque encore", body)
        self.assertIn("ameesh work milestone 7 verdict", body)


class CreationRouteTest(PgTestCase):
    """Les deux points d'entrée de création rendent l'avertissement sans rien
    refuser, et le backlog accepte la suite."""

    def test_work_add_returns_the_hint_without_refusing(self):
        item = work.add(self.db, title="Suite #956 (N R2) : ORDER BY id")
        self.assertIn("backlog", item.get("route_hint", ""))
        self.assertEqual(work.get(self.db, item["id"])["title"],
                         "Suite #956 (N R2) : ORDER BY id")

    def test_a_blocking_lot_has_no_hint(self):
        item = work.add(self.db, title="B1 : perte de messages")
        self.assertNotIn("route_hint", item)

    def test_a_light_request_has_no_hint(self):
        item = work.add(self.db, title="Ajouter un bouton d'export")
        self.assertNotIn("route_hint", item)

    def test_the_work_add_cli_warns_and_still_creates(self):
        proc = self.mesh("work", "add", "--title",
                         "Suite #956 (N R2 bureau) : ORDER BY id")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("backlog", proc.stderr)
        self.assertEqual(len(work.list_items(self.db)), 1)

    def test_mail_new_lot_warns_and_still_delivers(self):
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host)
        env = self.env(AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.cli("send", "dev1", "Voici la suite de la relecture.", "--from", "orch",
                        "--new-lot", "Suite #956 (N R2) : ORDER BY id", "--estimate", "1h",
                        "--app", "p", "--new-project", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("backlog", proc.stderr)
        rows = work.list_items(self.db, assignee="dev1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Suite #956 (N R2) : ORDER BY id")

    def test_the_backlog_accepts_the_suite(self):
        hinted = work.add(self.db, title="Suite #956 (N R2) : ORDER BY id")
        self.assertTrue(hinted.get("route_hint"))
        element = backlog.add(self.db, title=hinted["title"],
                              expected_value="un ORDER BY stable", value_score=40,
                              actor="human:smichea")
        self.assertEqual(element["type"], backlog.ITEM_TYPE)
        self.assertEqual(element["state"], "intake")
        self.assertIsNone(element["assignee"])
