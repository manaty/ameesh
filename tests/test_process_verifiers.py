# SPDX-License-Identifier: AGPL-3.0-only
"""#1924 : verificateurs finis de workflows Process (#1915, tranche C).

Aucune base : le coeur teste est une API pure. Chaque garde du registre fini a
son accord et son refus ; les refus portent un code stable, une source absente
refuse au lieu d'accepter, un booleen client ne vaut jamais une preuve, et une
preuve de deploiement exige un recu accepte referencant une observation reelle
distincte (une demande ne prouve pas son succes).
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from ameesh.process_verifiers import (CODE_TYPES, GUARD_NAMES, HOLDS,
                                      INSTALLED_TYPES, MAX_REPAIRS,
                                      OBSERVATION_KINDS, REFUSAL_CODES,
                                      SOURCE_COLLECTIONS, Command, Snapshot,
                                      capabilities, completion_policy, verify,
                                      verify_hold)

REV = "a" * 40
OTHER = "b" * 40
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
ALL_SOURCES = ("milestones", "receipts", "observations", "decisions",
               "deliveries")

OBSERVATIONS = (
    {"id": "obs-staging-deployed", "env": "staging", "sha": REV,
     "kind": "deployed"},
    {"id": "obs-staging-pass", "env": "staging", "sha": REV, "kind": "pass"},
    {"id": "obs-staging-fail", "env": "staging", "sha": REV, "kind": "fail"},
    {"id": "obs-preprod-deployed", "env": "preprod", "sha": REV,
     "kind": "deployed"},
    {"id": "obs-preprod-pass", "env": "preprod", "sha": REV, "kind": "pass"},
    {"id": "obs-preprod-fail", "env": "preprod", "sha": REV, "kind": "fail"},
)

#: les recus acceptes de B, chacun referencant une observation distincte.
RECEIPTS = tuple(
    {"guard": "deploy_observation", "action": "advance", "event_id": index + 1,
     "accepted": True, "evidence": {"observation": observation["id"]}}
    for index, observation in enumerate(OBSERVATIONS))

#: references minimales qui font accepter chaque garde sur la fixture pleine.
REFERENCES = {
    "planning": {},
    "start_work": {},
    "review_requested": {"head": REV},
    "qa_started": {"head": REV},
    "qa_repair": {"head": REV},
    "merged_and_staging_deployed": {"merge": REV,
                                    "staging": "obs-staging-deployed"},
    "staging_pass": {"staging": "obs-staging-pass"},
    "staging_failure": {"staging": "obs-staging-fail"},
    "preprod_deployed": {"preprod": "obs-preprod-deployed"},
    "preprod_pass": {"preprod": "obs-preprod-pass"},
    "preprod_failure": {"preprod": "obs-preprod-fail"},
    "repair_started": {"failure": "obs-staging-fail"},
    "stop_work": {"reason": "abandoned"},
    "work_completed": {"delivery": "del-1"},
}


def item(**overrides) -> dict:
    values = {"id": 7, "type": "evolution", "title": "lot temoin",
              "state": "build", "assignee": "agent:dev1",
              "reviewer": "agent:reviewer", "loops": 0, "closed_at": None}
    values.update(overrides)
    return values


def full_snapshot(**overrides) -> Snapshot:
    values = dict(
        item=item(),
        workflow_ref="canon@f12867b7:workflows/acme.json",
        workflow_stage="work",
        workflow_version=3,
        revision=REV,
        milestones=(
            {"kind": "frozen", "sha": REV, "verdict": None},
            {"kind": "merged", "sha": REV, "verdict": None},
            {"kind": "verdict", "sha": REV, "verdict": "blocked"},
            {"kind": "verdict", "sha": REV, "verdict": "ok"},
        ),
        receipts=RECEIPTS,
        observations=OBSERVATIONS,
        decisions_pending=1,
        deliveries=({"id": "del-1", "sha": REV, "accepted": True},),
        sources=ALL_SOURCES,
    )
    values.update(overrides)
    return Snapshot(**values)


def command_for(guard: str, **overrides) -> Command:
    values = dict(work_item_id=7, command_id=COMMAND_ID, guard=guard,
                  from_stage="work", to_stage="next", actor="agent:dev1",
                  source="cli", references=dict(REFERENCES.get(guard, {})))
    values.update(overrides)
    return Command(**values)


class VerificateursTest(unittest.TestCase):

    def assertRefusal(self, code, decision):
        self.assertFalse(decision.accepted, decision)
        self.assertEqual(decision.code, code)
        self.assertIn(decision.code, REFUSAL_CODES)

    # -- registre et capacites --------------------------------------------

    def test_registre_fini_du_contrat(self):
        self.assertEqual(GUARD_NAMES, (
            "planning", "start_work", "review_requested", "qa_started",
            "qa_repair", "merged_and_staging_deployed", "staging_pass",
            "staging_failure", "preprod_deployed", "preprod_pass",
            "preprod_failure", "repair_started", "stop_work",
            "work_completed"))
        self.assertEqual(len(set(GUARD_NAMES)), 14)

    def test_registre_aligne_sur_le_compilateur_a(self):
        try:
            from ameesh.process_workflow import VERIFIER_NAMES
        except ImportError:  # pragma: no cover - A fusionne ensuite
            self.skipTest("#1918 (A) pas encore fusionne dans ce worktree")
        self.assertEqual(tuple(VERIFIER_NAMES), GUARD_NAMES)

    def test_max_reparations_aligne_sur_la_borne_native(self):
        try:
            from ameesh.work import MAX_QA_LOOPS
        except ImportError:  # pragma: no cover
            self.skipTest("borne native indisponible")
        self.assertEqual(MAX_REPAIRS, MAX_QA_LOOPS)

    def test_capabilities_couvre_le_registre(self):
        caps = capabilities()
        self.assertEqual(tuple(cap.guard for cap in caps), GUARD_NAMES)
        by_name = {cap.guard: cap for cap in caps}
        self.assertEqual(by_name["staging_pass"].sources,
                         ("receipts", "observations"))
        self.assertEqual(by_name["merged_and_staging_deployed"].references,
                         ("merge", "staging"))
        self.assertEqual(by_name["review_requested"].references, ("head",))
        self.assertEqual(by_name["work_completed"].references,
                         ("delivery", "outcome", "pr"))
        for cap in caps:
            for source in cap.sources:
                self.assertIn(source, SOURCE_COLLECTIONS)

    def test_codes_de_refus_stables(self):
        self.assertEqual(len(REFUSAL_CODES), len(set(REFUSAL_CODES)))
        self.assertEqual(tuple(sorted(REFUSAL_CODES)), REFUSAL_CODES)
        self.assertNotIn("accepted", REFUSAL_CODES)

    # -- un accord et un refus par garde ----------------------------------

    def test_un_accord_par_garde(self):
        for guard in GUARD_NAMES:
            with self.subTest(guard=guard):
                decision = verify(command_for(guard), full_snapshot())
                self.assertTrue(decision.accepted, (guard, decision))
                self.assertEqual(decision.code, "accepted")
                self.assertEqual(decision.stage, "next")

    def test_un_refus_par_garde(self):
        cases = (
            ("planning", {}, {"item": item(title="")}, "evidence-missing"),
            ("start_work", {}, {"item": item(assignee="")},
             "evidence-missing"),
            ("review_requested", {"references": {"head": OTHER}}, {},
             "freeze-missing"),
            ("qa_started", {}, {"milestones": ()}, "freeze-missing"),
            ("qa_repair", {}, {"milestones": ()}, "verdict-missing"),
            ("merged_and_staging_deployed",
             {"references": {"merge": OTHER,
                             "staging": "obs-staging-deployed"}}, {},
             "merge-missing"),
            ("staging_pass", {"references": {"staging": "nope"}}, {},
             "source-unavailable"),
            ("staging_failure", {"references": {"staging": "obs-staging-pass"}},
             {}, "observation-result-mismatch"),
            ("preprod_deployed", {"references": {"preprod": "nope"}}, {},
             "source-unavailable"),
            ("preprod_pass",
             {"references": {"preprod": "obs-preprod-fail"}}, {},
             "observation-result-mismatch"),
            ("preprod_failure",
             {"references": {"preprod": "obs-preprod-pass"}}, {},
             "observation-result-mismatch"),
            ("repair_started", {"references": {"failure": "nope"}}, {},
             "failure-missing"),
            ("stop_work", {"references": {"reason": "finished"}}, {},
             "evidence-invalid"),
            ("work_completed", {"references": {"delivery": "del-1"}},
             {"deliveries": ()}, "completion-evidence-missing"),
        )
        for guard, command_over, snapshot_over, code in cases:
            with self.subTest(guard=guard, code=code):
                decision = verify(command_for(guard, **command_over),
                                  full_snapshot(**snapshot_over))
                self.assertRefusal(code, decision)

    # -- portes communes ---------------------------------------------------

    def test_garde_inconnue_refusee(self):
        decision = verify(command_for("nope"), full_snapshot())
        self.assertRefusal("guard-unsupported", decision)

    def test_forme_de_commande_refusee(self):
        cases = (
            ({"command_id": "pas-un-uuid"}, "UUID"),
            ({"from_stage": "work", "to_stage": "work"}, "inchangee"),
        )
        for overrides, needle in cases:
            with self.subTest(overrides=overrides):
                decision = verify(command_for("planning", **overrides),
                                  full_snapshot())
                self.assertRefusal("command-invalid", decision)
                self.assertIn(needle, decision.message)
        decision = verify(replace(command_for("planning"), guard=""),
                          full_snapshot())
        self.assertRefusal("command-invalid", decision)
        self.assertIn("garde", decision.message)

    def test_booleen_client_refuse(self):
        decision = verify(
            command_for("review_requested", references={"head": True}),
            full_snapshot())
        self.assertRefusal("command-invalid", decision)
        self.assertIn("booleen", decision.message)

    def test_reference_inconnue_refusee(self):
        decision = verify(command_for("planning", references={"go": "yes"}),
                          full_snapshot())
        self.assertRefusal("evidence-invalid", decision)

    def test_reference_requise_absente(self):
        decision = verify(command_for("review_requested", references={}),
                          full_snapshot())
        self.assertRefusal("evidence-missing", decision)

    def test_etape_perimee(self):
        self.assertRefusal(
            "stage-stale",
            verify(command_for("planning", from_stage="other"),
                   full_snapshot()))

    def test_lot_absent_ou_different(self):
        self.assertRefusal("item-missing",
                           verify(command_for("planning"), full_snapshot(item={})))
        self.assertRefusal("item-mismatch",
                           verify(command_for("planning"),
                                  full_snapshot(item=item(id=8))))

    def test_workflow_absent_refuse(self):
        decision = verify(command_for("planning"),
                          full_snapshot(workflow_ref=""))
        self.assertRefusal("workflow-absent", decision)

    def test_lot_terminal_ne_se_rouvre_pas(self):
        decision = verify(
            command_for("planning"),
            full_snapshot(item=item(closed_at="2026-10-16T00:00:00Z")))
        self.assertRefusal("terminal-reopening", decision)

    def test_type_hors_registre_refuse(self):
        decision = verify(command_for("planning"),
                          full_snapshot(item=item(type="mystere")))
        self.assertRefusal("type-unsupported", decision)

    def test_source_non_collectee_refuse_au_lieu_d_accepter(self):
        decision = verify(command_for("staging_pass"),
                          full_snapshot(sources=("receipts",)))
        self.assertRefusal("source-unavailable", decision)
        self.assertIn("observations", decision.message)

    # -- preuves de deploiement : observation reelle et recu accepte -------

    def test_observation_non_installee_refuse(self):
        decision = verify(command_for("staging_pass"),
                          full_snapshot(observations=()))
        self.assertRefusal("source-unavailable", decision)
        self.assertIn("non installee", decision.message)

    def test_demande_de_deploiement_ne_prouve_pas_son_succes(self):
        without_witness = tuple(
            receipt for receipt in RECEIPTS
            if receipt["evidence"]["observation"] != "obs-staging-pass")
        decision = verify(command_for("staging_pass"),
                          full_snapshot(receipts=without_witness))
        self.assertRefusal("observation-unwitnessed", decision)
        refused = tuple(
            dict(receipt, accepted=False)
            if receipt["evidence"]["observation"] == "obs-staging-pass"
            else receipt for receipt in RECEIPTS)
        decision = verify(command_for("staging_pass"),
                          full_snapshot(receipts=refused))
        self.assertRefusal("observation-unwitnessed", decision)

    def test_observation_d_une_autre_revision(self):
        other = ({"id": "obs-staging-pass", "env": "staging", "sha": OTHER,
                  "kind": "pass"},)
        decision = verify(command_for("staging_pass"),
                          full_snapshot(observations=other))
        self.assertRefusal("revision-mismatch", decision)

    def test_revision_de_travail_requise(self):
        without = full_snapshot(revision="")
        for guard in ("staging_pass", "preprod_deployed", "repair_started",
                      "work_completed"):
            with self.subTest(guard=guard):
                self.assertRefusal("revision-missing",
                                   verify(command_for(guard), without))

    # -- types et completion ----------------------------------------------

    def test_matrice_garde_type(self):
        """Chaque type installe passe une garde de base et sa completion."""
        for kind in INSTALLED_TYPES:
            with self.subTest(kind=kind):
                snap = full_snapshot(item=item(type=kind))
                self.assertTrue(verify(command_for("start_work"), snap).accepted)
                if kind in CODE_TYPES:
                    decision = verify(command_for("work_completed"), snap)
                else:
                    decision = verify(
                        command_for("work_completed",
                                    references={"outcome": "resultat"}),
                        snap)
                self.assertTrue(decision.accepted, (kind, decision))

    def test_types_installes_et_politiques(self):
        self.assertEqual(set(INSTALLED_TYPES), {
            "bug", "evolution", "docs", "investigation",
            "governance-tracking", "promotion", "human_operation"})
        for kind in CODE_TYPES:
            with self.subTest(kind=kind):
                policy = completion_policy(kind)
                self.assertTrue(policy.code)
                self.assertTrue(policy.requires_delivery)
                self.assertTrue(policy.requires_review)
        for kind in ("docs", "investigation", "governance-tracking",
                     "promotion", "human_operation"):
            with self.subTest(kind=kind):
                policy = completion_policy(kind)
                self.assertFalse(policy.code)
                self.assertFalse(policy.requires_delivery)
                self.assertTrue(policy.requires_review)
        with self.assertRaises(KeyError):
            completion_policy("mystere")

    def test_completion_code_exige_une_livraison_acceptee(self):
        self.assertRefusal(
            "completion-evidence-missing",
            verify(command_for("work_completed"),
                   full_snapshot(deliveries=())))
        self.assertRefusal(
            "evidence-missing",
            verify(command_for("work_completed", references={}),
                   full_snapshot()))
        self.assertRefusal(
            "completion-not-accepted",
            verify(command_for("work_completed"),
                   full_snapshot(deliveries=({"id": "del-1", "sha": REV,
                                              "accepted": False},))))
        self.assertRefusal(
            "revision-mismatch",
            verify(command_for("work_completed"),
                   full_snapshot(deliveries=({"id": "del-1", "sha": OTHER,
                                              "accepted": True},))))

    def test_promotion_pr_seule_ne_complete_pas(self):
        decision = verify(
            command_for("work_completed",
                        references={"pr": "https://exemple.invalid/1"}),
            full_snapshot(item=item(type="promotion"), deliveries=()))
        self.assertRefusal("completion-evidence-missing", decision)

    def test_non_code_resultat_et_revue(self):
        snap = full_snapshot(item=item(type="docs"))
        decision = verify(
            command_for("work_completed", references={"outcome": "note publiee"}),
            snap)
        self.assertTrue(decision.accepted, decision)
        self.assertRefusal(
            "completion-evidence-missing",
            verify(command_for("work_completed", references={}), snap))
        without_review = full_snapshot(
            item=item(type="docs"),
            milestones=({"kind": "frozen", "sha": REV, "verdict": None},))
        self.assertRefusal(
            "completion-review-missing",
            verify(command_for("work_completed",
                               references={"outcome": "note publiee"}),
                   without_review))

    # -- relecture, arret, reparations, holds ------------------------------

    def test_relecteur_independant(self):
        self.assertRefusal(
            "reviewer-not-independent",
            verify(command_for("review_requested"),
                   full_snapshot(item=item(reviewer="agent:dev1"))))
        self.assertRefusal(
            "reviewer-missing",
            verify(command_for("review_requested"),
                   full_snapshot(item=item(reviewer=""))))

    def test_arret_explicite(self):
        self.assertTrue(verify(command_for("stop_work"),
                               full_snapshot()).accepted)
        self.assertRefusal(
            "evidence-missing",
            verify(command_for("stop_work",
                               references={"reason": "superseded"}),
                   full_snapshot()))
        decision = verify(
            command_for("stop_work", references={"reason": "superseded",
                                                 "superseded_by": 1924}),
            full_snapshot())
        self.assertTrue(decision.accepted, decision)

    def test_reparations_bornees(self):
        at_bound = full_snapshot(item=item(loops=MAX_REPAIRS))
        for guard in ("qa_repair", "staging_failure", "preprod_failure",
                      "repair_started"):
            with self.subTest(guard=guard):
                self.assertRefusal("repair-bound",
                                   verify(command_for(guard), at_bound))
        under = full_snapshot(item=item(loops=MAX_REPAIRS - 1))
        self.assertTrue(verify(command_for("qa_repair"), under).accepted)

    def test_holds(self):
        self.assertTrue(verify_hold("blocked",
                                    full_snapshot(sources=())).accepted)
        self.assertTrue(verify_hold("waiting_human",
                                    full_snapshot()).accepted)
        self.assertRefusal(
            "hold-no-decision",
            verify_hold("waiting_human", full_snapshot(decisions_pending=0)))
        self.assertRefusal(
            "source-unavailable",
            verify_hold("waiting_human",
                        full_snapshot(sources=("milestones",))))
        self.assertRefusal("hold-unsupported",
                           verify_hold("paused", full_snapshot()))
        self.assertEqual(set(HOLDS), {"blocked", "waiting_human"})

    def test_kinds_observations_stables(self):
        self.assertEqual(OBSERVATION_KINDS, ("deployed", "pass", "fail"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
