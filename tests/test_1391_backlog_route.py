# SPDX-License-Identifier: AGPL-3.0-only
"""#1391 — the route of a non-blocking defect: the backlog, not a lot (§2).

Canon §2 (owner, 15/10/2026): "a non-blocking defect (N, P2, P3) never blocks.
It does not create a lot: it goes to the backlog (`ameesh work backlog add`,
with its value and its workstream), grouped with the other minor defects of the
same workstream". On ameesh, a P2 of the sensitive class still blocks.

Witnesses: an N follow-up is detected and routed, a legitimate test suite is not
reported (without hiding an explicit N), ordinary follow-ups stay silent, a
negated "bloquant" and an explicitly minor regression are routed, a named
blocking defect (B/B1/P1/incident/outage) wins over a historical N reference,
a sensitive P2 is kept, the warning is returned by both creation entry points
(`work add`, `mail --new-lot`), the instruction injected in the review-after-
merge reviewer cites the route, and the backlog accepts the follow-up.

#1622 — false positives of the route regexes (R Claude #1391): "N° 5" is a
number, a lowercase `n2` ("Nexlink n2") is not the canon marker, and "avis" /
"lot(s)" only count when they aim at a reference.
"""
from __future__ import annotations

import unittest

from ameesh import backlog, registry, review_lot, work

from .support import PgTestCase


class FollowupHintTest(unittest.TestCase):
    """The detection alone: what is a review follow-up, and what is not."""

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
        # R1 cx2, P2 3: "suite de tests" is not a marker, but must not hide an
        # explicit N/P2/P3.
        for title in (
            "N2 : améliorer les diagnostics de la suite de tests",
            "P2 : suite de tests instable",
        ):
            self.assertIn("backlog", backlog.followup_hint(title=title), title)

    def test_an_ordinary_follow_up_is_not_a_review_suite(self):
        # R1 cx2, P2 2: "suivi"/"follow-up" is a marker only when aiming at a
        # review or a reference; ordinary work stays silent.
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
            # R1 cx2, P2 1: an explicit blocking defect keeps its lot even when
            # the title or the source quotes a historical N follow-up (bare "B"
            # included).
            "P1 : corriger la panne issue du N2 de la R1",
            "N2 : corriger la panne issue de la R1",
            "B : suite #956 — perte de messages",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)

    def test_a_blocking_defect_wins_over_a_historical_n_reference(self):
        # R1 cx2, P2 1: the explicit blocking defect keeps its lot when the
        # source only quotes a historical N reference.
        self.assertEqual(backlog.followup_hint(
            title="P1 : corriger la panne", source="issue du N2 de la R1"), "")

    def test_a_negated_blocking_word_is_not_a_blocking_defect(self):
        # R2 cx2, P2: "non bloquant" / "pas bloquant" qualifies the defect, it
        # is not a positive occurrence of "bloquant": the minor follow-up keeps
        # its route to the backlog.
        for title in (
            "non bloquant : simplifier le diagnostic",
            "pas bloquant : améliorer le libellé",
        ):
            self.assertIn("backlog", backlog.followup_hint(title=title), title)
        # a positive "bloquant" stays ahead, even with an N marker
        self.assertEqual(backlog.followup_hint(
            title="N2 : défaut bloquant confirmé par la mesure"), "")

    def test_an_explicit_minor_regression_goes_to_the_backlog(self):
        # R2 cx2, P2: "régression" alone is not a blocking defect; with an
        # explicit minor marker (N2), the follow-up goes to the backlog.
        out = backlog.followup_hint(title="N2 : régression cosmétique du libellé")
        self.assertIn("backlog", out)

    def test_a_plain_french_suite_keeps_its_lot(self):
        # "suite à / suite au": ordinary French, often a human request — not a
        # review follow-up (canon §2, targeted warning).
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


class RouteFalsePositiveTest(unittest.TestCase):
    """#1622 — the false positives reported by the Claude review of #1391."""

    def test_a_degree_sign_is_not_an_n_marker(self):
        # N1: "N°"/"Nº" is a number, not the canon's N. The degree sign
        # disqualifies the match, spaced or not.
        for title in (
            "Numéro N° 5 : le libellé est faux",
            "Numéro Nº 5 : corriger le renvoi",
            "N° 5 : le libellé est faux",
            "N ° 5 : le libellé est faux",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)

    def test_a_lowercase_number_is_not_the_canon_marker(self):
        # N3: the marker is uppercase ("Nexlink n2" is not an N2). A lowercase
        # follow-up is not reported — silence is the safe side (no lot warning).
        for title in (
            "Nexlink n2 : le relais perd des lignes",
            "n2 : borner le diagnostic",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)

    def test_avis_and_lots_need_a_reference(self):
        # N2: "suivi des avis clients", "suivi des lots en retard" are ordinary
        # work; the suivi must aim at a review or a reference.
        for title in (
            "Suivi des avis clients",
            "Suivi des lots en retard",
            "Suivi des lots de la file",
        ):
            self.assertEqual(backlog.followup_hint(title=title), "", title)
        for title in (
            "suivi du lot #956 : corriger le témoin",
            "suivi des avis de la relecture R1 : borner",
            "suivi des avis R2 : reprendre le libellé",
        ):
            self.assertIn("backlog", backlog.followup_hint(title=title), title)

    def test_the_uppercase_n_still_routes(self):
        for title in (
            "N2 : borner le diagnostic",
            "Suite #1037 N1 : attribuer la famille au lot produit",
            "N : la sonde unreserved est morte",
        ):
            self.assertIn("backlog", backlog.followup_hint(title=title), title)


class ReviewInstructionTest(unittest.TestCase):
    """The instruction injected in the review-after-merge reviewer cites the route."""

    def test_the_after_merge_instruction_cites_the_backlog(self):
        body = review_lot.body_of({"title": "petit lot", "branch_target": "main"}, 7,
                                  klass={"class": "light", "why": ""},
                                  merged_sha="a" * 40)
        self.assertIn("ne crée pas de lot", body)
        self.assertIn("ameesh work backlog add", body)
        self.assertIn("P2 de la classe sensible bloque encore", body)
        self.assertIn("ameesh work milestone 7 verdict", body)


class CreationRouteTest(PgTestCase):
    """Both creation entry points return the warning without refusing anything,
    and the backlog accepts the follow-up."""

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
