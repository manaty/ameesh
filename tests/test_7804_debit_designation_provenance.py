# SPDX-License-Identifier: AGPL-3.0-only
"""#7804 : une désignation en attente de provenance n'est pas « relue ».

Témoin Nexlink #7678 (auditeur, 18/11 15:19Z) : `debit --check` sortait 2
depuis plus de 24 h pour ce seul lot. Le débit le classait `qa_merge` (« lot
relu, en attente du paquet ») parce que la marque de report (`carried`) et les
deux verdicts reportés ressemblaient à un quorum, alors que le journal du gel
#43120 porte une autre attente : plancher non couvert et rafraîchissement de
canon « waiting; author provenance unknown for 6 commit(s) ».

La lecture doit suivre la **dernière** preuve de la désignation du gel — marque
JSON, `review-designation-floor:… not covered`, `review-designation-unavailable:`
ou `review-canon-refresh:… waiting/unavailable` — et rendre une attente
`qa_designation` avec sa cause, jamais `qa_merge`. Une provenance inconnue dont
la voie owner est consignée (hold `attente owner`) est une attente humaine,
hors `--check` ; une indisponibilité technique reste une attente d'agent.
"""
from __future__ import annotations

import json
import unittest

from ameesh import debit
from tests.test_3292_debit import H, T, event, frozen, item, run, verdict

HEAD = "42e3a5a734a9cc4fad9782e90946c7a0a4c18994"
OLD = "6c20625a1fa56a4253f50fcd15b6fff8dde92f2b"
SIX = ("92875edbb726", "a81efdfbf694", "366213f3beef", "f726d9fb9cdf",
       "474dd09e4452", "b08f9c9c2bc8")
PROVENANCE = ("author provenance unknown for 6 commit(s) of the frozen range: "
              + ", ".join(SIX))
OWNER_HOLD = ("hold: attente owner : séance d'attestations owner signées "
              "commit par commit pour les 6 commits codex-pc, puis "
              "requalification et nouvel avis")


def carried_mark(ident=7678, freeze_id=43120):
    return "review-designation-carried:%d:%d: %s" % (ident, freeze_id, json.dumps({
        "carried": [{"actor": "auditeur", "from": OLD, "verdict": "ok"},
                    {"actor": "codex3", "from": OLD, "verdict": "ok"}],
        "freeze": {"id": freeze_id, "sha": HEAD},
        "provisional": False,
        "reviewers": [{"family": "", "name": "auditeur"},
                      {"family": "", "name": "codex3"}],
        "state": "carried", "why": ""}))


def waiting_provenance_mark(ident=7678, freeze_id=43120):
    """La marque #1617 d'une désignation qui attend la provenance."""
    return "review-designation:%d:%d: %s" % (ident, freeze_id, json.dumps({
        "state": "waiting", "required": 2, "missing": 2,
        "reviewers": [],
        "authors": [{"family": "deepseek", "name": "deepseek5"}],
        "why": PROVENANCE,
        "freeze": {"id": freeze_id, "sha": HEAD},
        "sent": [], "unknown_authors": list(SIX), "policy_ref": ""},
        ensure_ascii=False, sort_keys=True))


def floor_note(ident=7678, freeze_id=43120):
    return ("review-designation-floor:%d:%d:0: review floor 0 not covered at"
            " freeze #%d: %s; kept: none" % (ident, freeze_id, freeze_id,
                                             PROVENANCE))


def refresh_note(ident=7678, freeze_id=43120, state="waiting", why=PROVENANCE):
    return ("review-canon-refresh:%d:%d:%s:notice: canon policy rechecked:"
            " %s; %s" % (ident, freeze_id, "ab" * 32, state, why))


def witness_7678(*extra_events, extra_milestones=()):
    """Le gel, le report, son plancher et le rafraîchissement réels de #7678."""
    milestones = [frozen(7678, 43120, T - 8 * H, sha=HEAD),
                  verdict(7678, 43121, T - 8 * H + 120, "auditeur", "ok",
                          sha=HEAD),
                  verdict(7678, 43122, T - 8 * H + 120, "codex3", "ok",
                          sha=HEAD),
                  *extra_milestones]
    events = [event(7678, 1, T - 8 * H, "build → qa — frozen (#3467)"),
              event(7678, 2, T - 8 * H + 60, carried_mark()),
              event(7678, 3, T - 8 * H + 120, floor_note()),
              event(7678, 4, T - 8 * H + 600, refresh_note()),
              *extra_events]
    return run(items=[item(7678, assignee="deepseek5", app="nexlink")],
               milestones=milestones, events=events,
               agents=[{"name": "auditeur", "status": "running",
                        "harness": "claude"},
                       {"name": "codex3", "status": "running",
                        "harness": "codex"}],
               families=("claude", "codex", "deepseek"))


def waits(report, kind=None):
    return [row for row in report["slow_waits"]
            if row["lot"] == 7678 and (kind is None or row["kind"] == kind)]


def human_waits(report, kind=None):
    return [row for row in report["human_waits"]
            if row["lot"] == 7678 and (kind is None or row["kind"] == kind)]


class ProvenanceWaitTest(unittest.TestCase):
    def test_temoin_7678_n_est_pas_qa_merge(self):
        report = witness_7678()
        row = report["qa"][0]
        self.assertFalse(row["ready"])
        self.assertFalse(row["missing_reviewer"])
        self.assertIsNotNone(row["designation_wait"])
        self.assertEqual(row["designation_wait"]["state"], "waiting")
        self.assertTrue(row["designation_wait"]["provenance"])
        self.assertEqual(row["designation_wait"]["unknown_authors"], list(SIX))
        self.assertEqual(row["pending_reviewers"], [])
        self.assertEqual(waits(report, "qa_merge"), [])
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_designation"])
        self.assertIn("author provenance unknown", waits(report)[0]["cause"])
        # Aucune voie owner consignée : c'est une attente d'agent, --check 2.
        self.assertEqual(report["totals"]["designation_waits"], 1)

    def test_voie_owner_consignee_rend_l_attente_humaine(self):
        report = witness_7678(event(7678, 10, T - 8 * H + 900, OWNER_HOLD,
                                    actor="orchestrateur"))
        self.assertEqual(waits(report), [])
        human = human_waits(report)
        self.assertEqual([w["kind"] for w in human], ["qa_designation"])
        self.assertEqual(human[0]["who"], "owner")
        self.assertIn("attestations owner", human[0]["gesture"])
        # Le hold externe couvre la même attente, hors `--check`.
        self.assertEqual(report["totals"]["slow_waits"], 0)

    def test_une_marque_waiting_de_provenance_n_est_pas_relue(self):
        """Le test de l'énoncé : la marque seule, avec deux verdicts acquis."""
        report = run(
            items=[item(7678, assignee="deepseek5", app="nexlink")],
            milestones=[frozen(7678, 43120, T - 8 * H, sha=HEAD),
                        verdict(7678, 43121, T - 7 * H, "cx-claude", "ok",
                                sha=HEAD),
                        verdict(7678, 43122, T - 7 * H, "cx-codex", "ok",
                                sha=HEAD)],
            events=[event(7678, 1, T - 8 * H + 60,
                          waiting_provenance_mark())],
            agents=[{"name": "cx-claude", "status": "running",
                     "harness": "claude"},
                    {"name": "cx-codex", "status": "running",
                     "harness": "codex"}],
            families=("claude", "codex", "deepseek"))
        row = report["qa"][0]
        # Avant #7804 : shortfall 0 (deux familles acquises) → ready, qa_merge.
        self.assertFalse(row["ready"])
        self.assertIsNotNone(row["designation_wait"])
        self.assertTrue(row["designation_wait"]["provenance"])
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_designation"])
        self.assertEqual(waits(report, "qa_merge"), [])

    def test_une_marque_finale_plus_recente_leve_l_attente(self):
        final = "review-designation:%d:%d: %s" % (7678, 43120, json.dumps({
            "state": "designated", "required": 2, "missing": 0,
            "reviewers": [{"family": "claude", "name": "auditeur"},
                          {"family": "codex", "name": "codex3"}],
            "authors": [{"family": "deepseek", "name": "deepseek5"}],
            "why": "designated reviewer(s)",
            "freeze": {"id": 43120, "sha": HEAD},
            "sent": [], "unknown_authors": [], "policy_ref": ""}))
        report = witness_7678(event(7678, 10, T - 7 * H, final))
        row = report["qa"][0]
        self.assertIsNone(row["designation_wait"])
        self.assertTrue(row["ready"])
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_merge"])

    def test_indisponibilite_technique_reste_une_attente_d_agent(self):
        report = run(
            items=[item(7678, assignee="deepseek5", app="nexlink")],
            milestones=[frozen(7678, 43120, T - 8 * H, sha=HEAD)],
            events=[event(7678, 1, T - 8 * H + 60, refresh_note(
                state="unavailable",
                why="review floor completion: no repository holds the frozen "
                    "head " + HEAD + ": cannot read the frozen diff"))],
            families=("claude", "codex", "deepseek"))
        row = report["qa"][0]
        self.assertFalse(row["ready"])
        self.assertTrue(row["designation_wait"]["unavailable"])
        self.assertFalse(row["designation_wait"]["provenance"])
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_designation"])
        self.assertEqual(waits(report, "qa_merge"), [])
        self.assertEqual(human_waits(report), [])

    def test_plancher_seul_suffit_a_dire_l_attente(self):
        """Une note de plancher non couverte porte déjà la cause (#3557)."""
        report = run(
            items=[item(7678, assignee="deepseek5", app="nexlink")],
            milestones=[frozen(7678, 43120, T - 8 * H, sha=HEAD),
                        verdict(7678, 43121, T - 8 * H + 90, "auditeur", "ok",
                                sha=HEAD),
                        verdict(7678, 43122, T - 8 * H + 90, "codex3", "ok",
                                sha=HEAD)],
            events=[event(7678, 1, T - 8 * H + 60, carried_mark()),
                    event(7678, 2, T - 8 * H + 120, floor_note())],
            families=("claude", "codex", "deepseek"))
        row = report["qa"][0]
        self.assertEqual(row["designation_wait"]["kind"], "floor")
        self.assertTrue(row["designation_wait"]["provenance"])
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_designation"])

    def test_note_unavailable_sans_rafraichissement(self):
        note = ("review-designation-unavailable:7678:43120: automatic reviewer"
                " designation not computed: no repository holds the frozen"
                " head " + HEAD)
        report = run(
            items=[item(7678, assignee="deepseek5", app="nexlink")],
            milestones=[frozen(7678, 43120, T - 8 * H, sha=HEAD)],
            events=[event(7678, 1, T - 8 * H + 60, note)],
            families=("claude", "codex", "deepseek"))
        row = report["qa"][0]
        self.assertTrue(row["designation_wait"]["unavailable"])
        self.assertFalse(row["missing_reviewer"])
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_designation"])

    def test_une_marque_sans_liste_mais_avec_cause_de_provenance(self):
        """La liste peut manquer : la cause nomme les commits (repli)."""
        mark = "review-designation:%d:%d: %s" % (7678, 43120, json.dumps({
            "state": "waiting", "required": 2, "missing": 2,
            "reviewers": [],
            "authors": [{"family": "deepseek", "name": "deepseek5"}],
            "why": PROVENANCE, "freeze": {"id": 43120, "sha": HEAD}}))
        report = run(
            items=[item(7678, assignee="deepseek5", app="nexlink")],
            milestones=[frozen(7678, 43120, T - 8 * H, sha=HEAD)],
            events=[event(7678, 1, T - 8 * H + 60, mark)],
            families=("claude", "codex", "deepseek"))
        row = report["qa"][0]
        self.assertTrue(row["designation_wait"]["provenance"])
        self.assertEqual(row["designation_wait"]["unknown_authors"], list(SIX))
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_designation"])

    def test_rafraichissement_posterieur_leve_la_dispense(self):
        """#7804 : une attente de désignation plus récente que la dispense."""
        report = run(
            items=[item(7678, assignee="deepseek5", app="nexlink")],
            milestones=[frozen(7678, 43120, T - 8 * H, sha=HEAD)],
            events=[event(7678, 1, T - 8 * H + 60,
                          "verdict not required: no re-verdict due (#1979)"),
                    event(7678, 2, T - 8 * H + 120, refresh_note())],
            families=("claude", "codex", "deepseek"))
        row = report["qa"][0]
        self.assertIsNotNone(row["designation_wait"])
        self.assertEqual([w["kind"] for w in waits(report)], ["qa_designation"])

    def test_dispense_posterieure_efface_l_attente(self):
        report = run(
            items=[item(7678, assignee="deepseek5", app="nexlink")],
            milestones=[frozen(7678, 43120, T - 8 * H, sha=HEAD)],
            events=[event(7678, 1, T - 8 * H + 60, refresh_note()),
                    event(7678, 2, T - 8 * H + 120,
                          "verdict not required: no re-verdict due (#1979)")],
            families=("claude", "codex", "deepseek"))
        row = report["qa"][0]
        self.assertIsNone(row["designation_wait"])
        self.assertTrue(row["ready"])


class DesignationsOnlyTest(unittest.TestCase):
    def test_designations_rend_l_attente_et_sa_cause(self):
        events = [event(7678, 2, T - 8 * H + 60, carried_mark()),
                  event(7678, 3, T - 8 * H + 120, floor_note()),
                  event(7678, 4, T - 8 * H + 600, refresh_note())]
        out = debit.designations(
            [frozen(7678, 43120, T - 8 * H, sha=HEAD)], events)
        self.assertIsNotNone(out["designation_wait"])
        self.assertFalse(out["missing"])
        self.assertEqual(out["unknown_authors"], list(SIX))
        self.assertIn("author provenance unknown", out["designation_wait"]["why"])


if __name__ == "__main__":
    unittest.main()
