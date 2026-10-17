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
from types import SimpleNamespace

from ameesh.process_verifiers import (CODE_TYPES, GUARD_NAMES, HOLDS,
                                      HOST_REFUSAL_CODES, INSTALLED_TYPES,
                                      MAX_REPAIRS, OBSERVATION_KINDS,
                                      REFUSAL_CODES, RESERVED_VERIFIERS,
                                      SOURCE_COLLECTIONS, Capability, Command,
                                      Snapshot, available_sources,
                                      capabilities, capability_report,
                                      completion_policy, merged_of,
                                      reviewed_head_for, reviewed_head_of,
                                      revision_of, type_capability,
                                      type_capabilities, verify, verify_hold)

REV = "a" * 40
OTHER = "b" * 40
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
AUTHOR = "agent:dev1"
REVIEWER = "agent:reviewer"
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

#: les recus acceptes de B (#1922), chacun referencant une observation distincte.
#: Forme reellement projetee par B : `facts` porte les faits du client, le blob
#: `process-receipt/1` reste sous `evidence` (N7 de la revue #1924).
RECEIPTS = tuple(
    {"accepted": True, "guard": "deploy_observation", "event_id": index + 1,
     "command_id": "22222222-2222-4222-8222-%012d" % (index + 1),
     "workflow_stage": "work", "workflow_version": 3, "sha256": "c" * 64,
     "evidence": {"schema": "process-receipt/1",
                  "facts": {"observation": observation["id"]}},
     "facts": {"observation": observation["id"]}}
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
            {"kind": "verdict", "sha": REV, "verdict": "blocked",
             "actor": REVIEWER},
            {"kind": "verdict", "sha": REV, "verdict": "ok", "actor": REVIEWER},
        ),
        receipts=RECEIPTS,
        observations=OBSERVATIONS,
        decisions_pending=1,
        deliveries=({"id": "del-1", "sha": REV, "accepted": True},),
        sources=ALL_SOURCES,
    )
    values.update(overrides)
    return Snapshot(**values)


def blocked_snapshot(**overrides) -> Snapshot:
    """Tete gelee et blocage courant du relecteur designe (qa_repair, R3).

    Fixture nommee : le cas ne depend plus de l'ordre des jalons de
    `full_snapshot` (`milestones[:-1]`).
    """
    values = dict(milestones=(
        {"kind": "frozen", "sha": REV, "verdict": None},
        {"kind": "merged", "sha": REV, "verdict": None},
        {"kind": "verdict", "sha": REV, "verdict": "blocked",
         "actor": REVIEWER},
    ))
    values.update(overrides)
    return full_snapshot(**values)


def command_for(guard: str, **overrides) -> Command:
    values = dict(work_item_id=7, command_id=COMMAND_ID, guard=guard,
                  from_stage="work", to_stage="next", actor="agent:dev1",
                  source="cli", references=dict(REFERENCES.get(guard, {})))
    values.update(overrides)
    return Command(**values)


def hold_command(hold: str, **overrides) -> Command:
    values = dict(work_item_id=7, command_id=COMMAND_ID, action="hold",
                  hold=hold, references={})
    values.update(overrides)
    return Command(**values)


def release_command(**overrides) -> Command:
    values = dict(work_item_id=7, command_id=COMMAND_ID, action="release",
                  references={})
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
                         ("milestones", "receipts", "observations"))
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
                snapshot = blocked_snapshot() if guard == "qa_repair" else full_snapshot()
                decision = verify(command_for(guard), snapshot)
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
            ("qa_repair", {}, {"milestones": ()}, "freeze-missing"),
            ("merged_and_staging_deployed",
             {"references": {"merge": OTHER,
                             "staging": "obs-staging-deployed"}}, {},
             "merge-missing"),
            ("staging_pass", {"references": {"staging": "nope"}}, {},
             "evidence-missing"),
            ("staging_failure", {"references": {"staging": "obs-staging-pass"}},
             {}, "observation-result-mismatch"),
            ("preprod_deployed", {"references": {"preprod": "nope"}}, {},
             "evidence-missing"),
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

    def test_observation_inconnue_refuse_une_preuve_absente(self):
        """N4 #1924 : collection collectee mais observation inconnue =
        evidence-missing ; `source-unavailable` reste reserve a la collection
        non collectee (porte de verify)."""
        decision = verify(command_for("staging_pass"),
                          full_snapshot(observations=()))
        self.assertRefusal("evidence-missing", decision)
        self.assertIn("inconnue", decision.message)
        self.assertRefusal(
            "evidence-missing",
            verify(command_for("staging_pass", references={"staging": "nope"}),
                   full_snapshot()))
        self.assertRefusal(
            "source-unavailable",
            verify(command_for("staging_pass"),
                   full_snapshot(sources=("milestones", "receipts"))))

    def test_matrice_declare_les_sources_lues(self):
        """N3 #1924 : staging/preprod/repair_started lisent les jalons (la
        revision reelle), la matrice doit exiger `milestones`."""
        for guard in ("staging_pass", "staging_failure", "preprod_deployed",
                      "preprod_pass", "preprod_failure", "repair_started"):
            with self.subTest(guard=guard):
                decision = verify(
                    command_for(guard),
                    full_snapshot(sources=("receipts", "observations")))
                self.assertRefusal("source-unavailable", decision)
                self.assertIn("milestones", decision.message)

    def test_verdict_courant_par_acteur(self):
        """N1 #1924 : un blocage n'est pas masque par le ok d'un autre
        relecteur ; l'auto-verdict de l'assigne ne vaut pas revue."""
        cross = full_snapshot(milestones=(
            {"kind": "frozen", "sha": REV, "verdict": None},
            {"kind": "merged", "sha": REV, "verdict": None},
            {"kind": "verdict", "sha": REV, "verdict": "blocked",
             "actor": REVIEWER},
            {"kind": "verdict", "sha": REV, "verdict": "ok",
             "actor": "agent:other-reviewer"}))
        self.assertRefusal("completion-review-missing",
                           verify(command_for("work_completed"), cross))
        # le blocage du relecteur ouvre la reparation malgre le ok d'un autre
        self.assertTrue(verify(command_for("qa_repair"), cross).accepted)
        # le meme relecteur leve son blocage (fixture pleine)
        self.assertTrue(verify(command_for("work_completed"),
                               full_snapshot()).accepted)
        # l'ok de l'assigne ne vaut pas revue
        self_ok = full_snapshot(milestones=(
            {"kind": "frozen", "sha": REV, "verdict": None},
            {"kind": "merged", "sha": REV, "verdict": None},
            {"kind": "verdict", "sha": REV, "verdict": "ok", "actor": AUTHOR}))
        self.assertRefusal("completion-review-missing",
                           verify(command_for("work_completed"), self_ok))
        # le blocage de l'assigne n'ouvre pas la reparation
        self_blocked = full_snapshot(milestones=(
            {"kind": "frozen", "sha": REV, "verdict": None},
            {"kind": "verdict", "sha": REV, "verdict": "blocked",
             "actor": AUTHOR}))
        self.assertRefusal("verdict-missing",
                           verify(command_for("qa_repair"), self_blocked))
        # un verdict sans acteur n'est pas attribuable : pas une revue
        anonymous = full_snapshot(milestones=(
            {"kind": "frozen", "sha": REV, "verdict": None},
            {"kind": "merged", "sha": REV, "verdict": None},
            {"kind": "verdict", "sha": REV, "verdict": "ok"}))
        self.assertRefusal("completion-review-missing",
                           verify(command_for("work_completed"), anonymous))

    def test_lot_promu_est_terminal(self):
        """N5 #1924 : `promoted` est terminal, aucune reouverture."""
        promoted = full_snapshot(item=item(state="promoted"))
        self.assertRefusal("terminal-reopening",
                           verify(command_for("planning"), promoted))
        self.assertRefusal("terminal-reopening",
                           verify(hold_command("blocked"), promoted))

    def test_demande_de_deploiement_ne_prouve_pas_son_succes(self):
        without_witness = tuple(
            receipt for receipt in RECEIPTS
            if receipt["facts"]["observation"] != "obs-staging-pass")
        decision = verify(command_for("staging_pass"),
                          full_snapshot(receipts=without_witness))
        self.assertRefusal("observation-unwitnessed", decision)
        refused = tuple(
            dict(receipt, accepted=False)
            if receipt["facts"]["observation"] == "obs-staging-pass"
            else receipt for receipt in RECEIPTS)
        decision = verify(command_for("staging_pass"),
                          full_snapshot(receipts=refused))
        self.assertRefusal("observation-unwitnessed", decision)
        # un recu brut non projete (blob sous `evidence`) reste un temoin :
        # les faits sont lus dans `evidence.facts` a defaut de `facts` (N7).
        raw = ({"accepted": True, "guard": "deploy_observation",
                "evidence": {"schema": "process-receipt/1",
                             "facts": {"observation": "obs-staging-pass"}}},)
        self.assertTrue(verify(command_for("staging_pass"),
                               full_snapshot(receipts=raw)).accepted)

    def test_observation_d_une_autre_revision(self):
        other = ({"id": "obs-staging-pass", "env": "staging", "sha": OTHER,
                  "kind": "pass"},)
        decision = verify(command_for("staging_pass"),
                          full_snapshot(observations=other))
        self.assertRefusal("revision-mismatch", decision)

    def test_revision_de_travail_requise(self):
        without = full_snapshot(revision="", milestones=())
        for guard in ("staging_pass", "preprod_deployed", "repair_started",
                      "work_completed"):
            with self.subTest(guard=guard):
                self.assertRefusal("revision-missing",
                                   verify(command_for(guard), without))

    def test_revision_courante_suit_le_dernier_jalon(self):
        """Un gel ancien ne masque pas une fusion recente (revue Codex B4)."""
        ms = ({"kind": "frozen", "sha": OTHER, "verdict": None},
              {"kind": "merged", "sha": REV, "verdict": None})
        self.assertEqual(revision_of(ms), REV)
        self.assertEqual(reviewed_head_of(ms), OTHER)
        self.assertEqual(merged_of(ms), REV)
        snap = full_snapshot(milestones=ms, revision=revision_of(ms))
        self.assertTrue(verify(command_for("staging_pass"), snap).accepted)
        self.assertRefusal(
            "revision-mismatch",
            verify(command_for("staging_pass"),
                   full_snapshot(milestones=ms,
                                 revision=revision_of(ms),
                                 observations=({"id": "obs-staging-pass",
                                                "env": "staging", "sha": OTHER,
                                                "kind": "pass"},))))
        # un gel posterieur a une fusion redevient la revision courante
        third = "c" * 40
        cycle = ms + ({"kind": "frozen", "sha": third, "verdict": None},)
        self.assertEqual(revision_of(cycle), third)
        self.assertEqual(reviewed_head_of(cycle), third)
        self.assertEqual(reviewed_head_for(cycle, REV), OTHER)
        self.assertEqual(reviewed_head_for(ms, REV), OTHER)
        self.assertEqual(reviewed_head_for(cycle, third), third)

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
        # N2 : le libelle non-code ne vaut que rattache a la tete relue ; un ok
        # sur le commit de fusion ne couvre pas le gel qui a servi de revision.
        on_merge_only = full_snapshot(
            item=item(type="docs"),
            milestones=({"kind": "frozen", "sha": OTHER, "verdict": None},
                        {"kind": "merged", "sha": REV, "verdict": None},
                        {"kind": "verdict", "sha": REV, "verdict": "ok",
                         "actor": REVIEWER}),
            revision=REV)
        self.assertRefusal(
            "completion-review-missing",
            verify(command_for("work_completed",
                               references={"outcome": "note publiee"}),
                   on_merge_only))

    # -- relecture, arret, reparations, holds ------------------------------

    def test_completion_code_exige_la_revue_applicable(self):
        """Livraison acceptee sans revue : refus (revue Codex B2)."""
        without_review = full_snapshot(milestones=(
            {"kind": "frozen", "sha": REV, "verdict": None},
            {"kind": "merged", "sha": REV, "verdict": None}))
        self.assertRefusal(
            "completion-review-missing",
            verify(command_for("work_completed"), without_review))

    def test_completion_code_relie_livraison_et_tete_relue(self):
        """La revue porte sur la tete relue fusionnee, pas sur le commit de
        fusion quand il differe (revue Codex B2/B4)."""
        linked = full_snapshot(
            milestones=({"kind": "frozen", "sha": OTHER, "verdict": None},
                        {"kind": "merged", "sha": REV, "verdict": None},
                        {"kind": "verdict", "sha": OTHER, "verdict": "ok",
                         "actor": REVIEWER}),
            revision=REV)
        self.assertTrue(verify(command_for("work_completed"), linked).accepted)
        on_merge_only = full_snapshot(
            milestones=({"kind": "frozen", "sha": OTHER, "verdict": None},
                        {"kind": "merged", "sha": REV, "verdict": None},
                        {"kind": "verdict", "sha": REV, "verdict": "ok",
                         "actor": REVIEWER}),
            revision=REV)
        self.assertRefusal(
            "completion-review-missing",
            verify(command_for("work_completed"), on_merge_only))
        # un verdict bloque posterieur au ok ne complete pas
        regressed = full_snapshot(milestones=(
            {"kind": "frozen", "sha": REV, "verdict": None},
            {"kind": "merged", "sha": REV, "verdict": None},
            {"kind": "verdict", "sha": REV, "verdict": "ok",
             "actor": REVIEWER},
            {"kind": "verdict", "sha": REV, "verdict": "blocked",
             "actor": REVIEWER}))
        self.assertRefusal(
            "completion-review-missing",
            verify(command_for("work_completed"), regressed))

    def test_references_historiques_bornees(self):
        """Un gel depasse ne vaut plus (revue Codex B3)."""
        stale = full_snapshot(milestones=(
            {"kind": "frozen", "sha": OTHER, "verdict": None},
            {"kind": "frozen", "sha": REV, "verdict": None}))
        for guard in ("review_requested", "qa_started"):
            with self.subTest(guard=guard):
                self.assertRefusal(
                    "revision-mismatch",
                    verify(command_for(guard, references={"head": OTHER}),
                           stale))
                self.assertTrue(verify(command_for(guard), stale).accepted)
        blocked = full_snapshot(milestones=(
            {"kind": "frozen", "sha": OTHER, "verdict": None},
            {"kind": "verdict", "sha": OTHER, "verdict": "blocked",
             "actor": REVIEWER},
            {"kind": "frozen", "sha": REV, "verdict": None}))
        self.assertRefusal(
            "revision-mismatch",
            verify(command_for("qa_repair", references={"head": OTHER}),
                   blocked))
        self.assertRefusal(
            "verdict-missing", verify(command_for("qa_repair"), blocked))

    def test_fusion_historique_bornee(self):
        two = full_snapshot(milestones=(
            {"kind": "frozen", "sha": OTHER, "verdict": None},
            {"kind": "merged", "sha": OTHER, "verdict": None},
            {"kind": "merged", "sha": REV, "verdict": None}))
        self.assertRefusal(
            "revision-mismatch",
            verify(command_for("merged_and_staging_deployed",
                               references={"merge": OTHER,
                                           "staging": "obs-staging-deployed"}),
                   two))

    def test_reparation_refuse_un_blocage_remplace_par_un_go(self):
        """N1 #1924 : le ok du relecteur leve son propre blocage (plus de
        reparation) ; la fixture nommee rouvre la reparation sur un blocage
        courant, sans dependre de l'ordre des jalons (R3)."""
        self.assertRefusal("verdict-missing",
                           verify(command_for("qa_repair"), full_snapshot()))
        self.assertTrue(verify(command_for("qa_repair"),
                               blocked_snapshot()).accepted)

    def test_reparation_exige_un_gel_borne(self):
        """R2 #1924 : sans jalon frozen, qa_repair ne compare pas la tete a
        la revision : refus fail-closed, comme qa_started."""
        self.assertRefusal(
            "freeze-missing",
            verify(command_for("qa_repair"),
                   full_snapshot(milestones=(
                       {"kind": "verdict", "sha": REV, "verdict": "blocked",
                        "actor": REVIEWER},))))
        self.assertRefusal(
            "freeze-missing",
            verify(command_for("qa_repair"), full_snapshot(milestones=())))

    def test_staging_refuse_une_fusion_depassee_par_un_gel(self):
        milestones = full_snapshot().milestones + (
            {"kind": "frozen", "sha": OTHER},)
        self.assertRefusal(
            "revision-mismatch",
            verify(command_for("merged_and_staging_deployed"),
                   full_snapshot(milestones=milestones)))

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
        # N6 : le remplacant ne peut pas etre le lot lui-meme (item id 7),
        # entier comme chaine ; l'existence du lot remplacant n'est pas
        # verifiable cote C (aucune collection de lots).
        for replaced_by in (7, "7"):
            with self.subTest(superseded_by=replaced_by):
                self.assertRefusal(
                    "evidence-invalid",
                    verify(command_for(
                        "stop_work", references={"reason": "superseded",
                                                 "superseded_by": replaced_by}),
                        full_snapshot()))
        # N6 : un remplacant qui n'est pas un identifiant de lot ne prouve rien
        # (`_as_int` rend -1, distinct de l'id du lot : il passait).
        # Post-fusion #1937 : les formes qui passaient le pre-filtre
        # `lstrip("-").isdigit()` sans etre des entiers (`--3`, `²`) levaient
        # une ValueError au lieu du refus nomme ; elles rendent -1 comme les
        # autres, jamais une exception.
        for replaced_by in ("abc", "#1924", 0, -3, "-3", "--3", "²"):
            with self.subTest(superseded_by=replaced_by):
                self.assertRefusal(
                    "evidence-invalid",
                    verify(command_for(
                        "stop_work", references={"reason": "superseded",
                                                 "superseded_by": replaced_by}),
                        full_snapshot()))
        decision = verify(
            command_for("stop_work", references={"reason": "superseded",
                                                 "superseded_by": "1924"}),
            full_snapshot())
        self.assertTrue(decision.accepted, decision)

    def test_reparations_bornees(self):
        blocked = blocked_snapshot().milestones
        at_bound = full_snapshot(item=item(loops=MAX_REPAIRS),
                                 milestones=blocked)
        for guard in ("qa_repair", "staging_failure", "preprod_failure",
                      "repair_started"):
            with self.subTest(guard=guard):
                self.assertRefusal("repair-bound",
                                   verify(command_for(guard), at_bound))
        under = full_snapshot(item=item(loops=MAX_REPAIRS - 1),
                              milestones=blocked)
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

    def test_retenue_exige_une_decision_native(self):
        decision = verify(hold_command("waiting_human"), full_snapshot())
        self.assertTrue(decision.accepted, decision)
        self.assertEqual(decision.stage, "work")
        self.assertRefusal(
            "hold-no-decision",
            verify(hold_command("waiting_human"),
                   full_snapshot(decisions_pending=0)))
        self.assertRefusal(
            "source-unavailable",
            verify(hold_command("waiting_human"),
                   full_snapshot(sources=("milestones",))))

    def test_retenue_blocked_et_inconnue(self):
        self.assertTrue(verify(hold_command("blocked"),
                               full_snapshot()).accepted)
        self.assertRefusal("command-invalid",
                           verify(hold_command("paused"), full_snapshot()))

    def test_retenue_redondante_refusee(self):
        self.assertRefusal(
            "hold-redundant",
            verify(hold_command("blocked"),
                   full_snapshot(item=item(state="blocked"))))

    def test_reprise_seulement_depuis_une_retenue(self):
        decision = verify(release_command(),
                          full_snapshot(item=item(state="blocked")))
        self.assertTrue(decision.accepted, decision)
        self.assertEqual(decision.stage, "work")
        self.assertRefusal(
            "not-held",
            verify(release_command(), full_snapshot(item=item(state="build"))))

    def test_avance_depuis_une_retenue_refusee(self):
        for state in HOLDS:
            with self.subTest(state=state):
                decision = verify(command_for("start_work"),
                                  full_snapshot(item=item(state=state)))
                self.assertRefusal("hold-advance", decision)

    def test_terminal_jamais_rouvert_par_une_retenue(self):
        closed = full_snapshot(item=item(closed_at="2026-10-16T00:00:00Z"))
        self.assertRefusal("terminal-reopening",
                           verify(hold_command("blocked"), closed))
        self.assertRefusal("terminal-reopening",
                           verify(release_command(), closed))

    def test_kinds_observations_stables(self):
        self.assertEqual(OBSERVATION_KINDS, ("deployed", "pass", "fail"))


class FakeRegistry:
    """L'introspection publiee par B (#1922) : `installed_verifiers()`."""

    def __init__(self, *names):
        self.installed = {name: object() for name in names}

    def installed_verifiers(self):
        return dict(self.installed)


class CapacitesTest(unittest.TestCase):
    """La matrice distingue connu, installe, sources disponibles et types.

    Aucune sonde implicite : sans registre ni sources fournis, rien n'est
    declare executable ; une source non fournie n'est jamais supposee.
    """

    def test_contrat_statique_non_sonde_et_fail_closed(self):
        caps = capabilities()
        self.assertEqual(tuple(cap.guard for cap in caps), GUARD_NAMES)
        for cap in caps:
            with self.subTest(guard=cap.guard):
                self.assertTrue(cap.known)
                self.assertFalse(cap.reserved)
                self.assertIsNone(cap.installed)
                self.assertEqual(cap.available, ())
                self.assertEqual(cap.missing, ())
                self.assertFalse(cap.sources_probed)
                self.assertFalse(cap.executable)
                self.assertEqual(cap.refusal, HOST_REFUSAL_CODES[0])
        reserved = capabilities(include_reserved=True)
        self.assertEqual(tuple(cap.guard for cap in reserved),
                         GUARD_NAMES + RESERVED_VERIFIERS)
        self.assertTrue(reserved[-1].reserved)
        self.assertTrue(reserved[-1].known)

    def test_installation_sondee_par_le_registre(self):
        partly = FakeRegistry("planning", "start_work")
        caps = {cap.guard: cap for cap in capabilities(registry=partly)}
        self.assertTrue(caps["planning"].installed)
        self.assertFalse(caps["review_requested"].installed)
        self.assertIs(caps["planning"].executable, True)
        self.assertEqual(caps["review_requested"].refusal,
                         HOST_REFUSAL_CODES[0])
        full = FakeRegistry(*(GUARD_NAMES + RESERVED_VERIFIERS))
        report = capability_report(registry=full, sources=ALL_SOURCES)
        self.assertEqual(tuple(cap.guard for cap in report.guards),
                         GUARD_NAMES)
        self.assertEqual(report.executable_guards,
                         GUARD_NAMES + RESERVED_VERIFIERS)

    def test_sources_non_lues_ne_sont_pas_executables(self):
        """Sans lecture des sources, aucune garde qui en exige n'est declaree
        executable (revue Codex B1) : seule l'installation ne suffit pas."""
        full = FakeRegistry(*(GUARD_NAMES + RESERVED_VERIFIERS))
        unread = capability_report(registry=full)
        self.assertEqual(unread.executable_guards,
                         ("planning", "start_work", "stop_work", "hold"))
        self.assertFalse(unread.capability("staging_pass").executable)
        self.assertEqual(unread.refusal("staging_pass"), "source-unavailable")
        self.assertFalse(unread.capability("review_requested").sources_probed)
        read = capability_report(registry=full, sources=ALL_SOURCES)
        self.assertEqual(read.executable_guards,
                         GUARD_NAMES + RESERVED_VERIFIERS)
        self.assertTrue(read.capability("staging_pass").executable)

    def test_sources_sondees_disponibles_et_manquantes(self):
        caps = {cap.guard: cap for cap in capabilities(
            registry=FakeRegistry(*GUARD_NAMES),
            sources=("milestones", "decisions", "ghost"))}
        self.assertEqual(caps["review_requested"].available, ("milestones",))
        self.assertEqual(caps["review_requested"].missing, ())
        self.assertTrue(caps["review_requested"].executable)
        self.assertEqual(caps["staging_pass"].available, ("milestones",))
        self.assertEqual(caps["staging_pass"].missing,
                         ("receipts", "observations"))
        self.assertFalse(caps["staging_pass"].executable)
        self.assertEqual(caps["staging_pass"].refusal, "source-unavailable")
        self.assertEqual(caps["planning"].refusal, "")

    def test_installation_avant_sources_dans_le_refus(self):
        report = capability_report(registry=FakeRegistry("planning",
                                                         "staging_pass"),
                                   sources=())
        self.assertEqual(report.refusal("ghost"), "guard-unsupported")
        self.assertEqual(report.refusal("planning"), "")
        # l'installation se prouve avant les sources : B refuse
        # `verifier-uninstalled` sans meme appeler C.
        self.assertEqual(report.refusal("review_requested"),
                         HOST_REFUSAL_CODES[0])
        self.assertEqual(report.refusal("staging_pass"), "source-unavailable")

    def test_nom_reserve_et_sources_conditionnelles(self):
        report = capability_report(
            registry=FakeRegistry(*(GUARD_NAMES + RESERVED_VERIFIERS)),
            sources=ALL_SOURCES)
        hold = report.capability("hold")
        self.assertIsInstance(hold, Capability)
        self.assertTrue(hold.reserved)
        self.assertTrue(hold.known)
        self.assertEqual(hold.sources, ())
        self.assertEqual(hold.conditional_sources, ("decisions",))
        self.assertTrue(hold.executable)

    def test_types_supportes_et_refus_explicite(self):
        types = type_capabilities()
        self.assertEqual(tuple(t.type for t in types), INSTALLED_TYPES)
        for kind in types:
            with self.subTest(type=kind.type):
                self.assertTrue(kind.supported)
                policy = completion_policy(kind.type)
                self.assertEqual(
                    (kind.code, kind.requires_delivery, kind.requires_review),
                    (policy.code, policy.requires_delivery,
                     policy.requires_review))
        unknown = type_capability("epic")
        self.assertFalse(unknown.supported)
        self.assertIn("type-unsupported", unknown.reason)

    def test_available_sources_formes_acceptees(self):
        self.assertEqual(available_sources(None), ())
        self.assertEqual(available_sources({}), ())
        self.assertEqual(
            available_sources({"milestones": (), "decisions": ()}),
            ("milestones", "decisions"))
        self.assertEqual(available_sources(["milestones", "ghost"]),
                         ("milestones", "ghost"))
        self.assertEqual(available_sources("milestones"), ("milestones",))
        self.assertEqual(available_sources(full_snapshot()), ALL_SOURCES)
        self.assertEqual(
            available_sources(SimpleNamespace(sources={"milestones": ()})),
            ("milestones",))
        # aucun attribut de contexte : rien n'est declare, rien n'est lu
        self.assertEqual(available_sources(SimpleNamespace()), ())

    def test_sous_ensemble_declare_du_premier_pilote(self):
        # installation complete, observations pas encore collectees : le
        # sous-ensemble executable est explicite, le reste est refuse.
        report = capability_report(
            registry=FakeRegistry(*(GUARD_NAMES + RESERVED_VERIFIERS)),
            sources=("milestones", "receipts", "decisions", "deliveries"))
        self.assertIn("planning", report.executable_guards)
        self.assertIn("work_completed", report.executable_guards)
        self.assertIn("hold", report.executable_guards)
        self.assertNotIn("staging_pass", report.executable_guards)
        self.assertNotIn("preprod_deployed", report.executable_guards)
        self.assertEqual(report.refusal("staging_pass"), "source-unavailable")
        self.assertEqual(report.refusal("preprod_deployed"),
                         "source-unavailable")
        for cap in report.guards + report.reserved:
            with self.subTest(guard=cap.guard):
                if cap.executable:
                    self.assertEqual(cap.refusal, "")
                else:
                    self.assertIn(cap.refusal,
                                  REFUSAL_CODES + HOST_REFUSAL_CODES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
