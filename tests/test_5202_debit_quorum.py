# SPDX-License-Identifier: AGPL-3.0-only
"""#5202 : un désigné en surplus, sans verdict, n'est plus « avis dû » sur un
lot dont la porte a acquis le quorum de la tête gelée (#4423).

Témoin du 01/11 vers 01:00Z (auditeur) : #5083 validé par report (ok cx1 +
ok cx3 au gel #29420), ameesh-dev3 resté désigné sans avis ; `ameesh debit`
le comptait « avis dû », proposait de réveiller dev3 et `--check` sortait 2.
"""
from __future__ import annotations

import json
import unittest

from tests.test_3292_debit import H, T, event, frozen, item, run, verdict

OLD = "d" * 40
HEAD = "a" * 40


def carried_mark(ident, freeze_id):
    """La marque de report réelle de #5083 : trois désignés, deux ok reportés."""
    return "review-designation-carried:%d:%d: %s" % (ident, freeze_id, json.dumps({
        "carried": [{"actor": "ameesh-cx1", "from": OLD, "verdict": "ok"},
                    {"actor": "ameesh-cx3", "from": OLD, "verdict": "ok"}],
        "freeze": {"id": freeze_id, "sha": HEAD},
        "reviewers": [{"family": "", "name": "ameesh-cx1"},
                      {"family": "", "name": "ameesh-cx3"},
                      {"family": "deepseek", "name": "ameesh-dev3"}],
        "state": "carried", "why": ""}))


def lot_5083(*extra_events, extra_milestones=()):
    """Gel à T-4 h, report des avis cx1 et cx3, validation par la porte."""
    milestones = [frozen(5083, 10, T - 4 * H, sha=HEAD),
                  verdict(5083, 11, T - 4 * H + 60, "ameesh-cx1", "ok", sha=HEAD),
                  verdict(5083, 12, T - 4 * H + 60, "ameesh-cx3", "ok", sha=HEAD),
                  *extra_milestones]
    events = [event(5083, 1, T - 4 * H, "build → qa — frozen (#3467)"),
              event(5083, 2, T - 4 * H + 30, carried_mark(5083, 10)),
              event(5083, 3, T - 4 * H + 90,
                    "qa → validated — carried review quorum (#4423)",
                    actor="ameesh", state="validated"),
              # La passe de report écrit encore en `qa` après la validation.
              event(5083, 4, T - 4 * H + 120,
                    "carried quorum pending: freeze #10 — lot 5083 already in "
                    "validated — attempt 5/5, no further retry (#4834)",
                    actor="ameesh", state="qa"),
              *extra_events]
    return run(items=[item(5083, assignee="ameesh-dev2")],
               milestones=milestones, events=events,
               agents=[{"name": "ameesh-dev3", "status": "idle"}])


def waits(report, lot):
    return [row for row in report["slow_waits"] if row["lot"] == lot]


class QuorumAcquisTest(unittest.TestCase):
    def test_temoin_5083_ni_avis_du_ni_attente_de_relecteur(self):
        report = lot_5083()
        self.assertEqual(report["reviews_due"], [])
        self.assertEqual(report["qa"][0]["pending_reviewers"], [])
        self.assertTrue(report["qa"][0]["ready"])
        self.assertEqual(report["idle_agents_with_work"], [])
        # L'attente reste dite, mais c'est celle du paquet, pas de dev3.
        self.assertEqual([(row["kind"], row["who"]) for row in waits(report, 5083)],
                         [("qa_merge", "paquet")])

    def test_sans_validation_le_designe_reste_du(self):
        """Témoin rouge : sans passage en validated, dev3 reste dû."""
        report = run(items=[item(5083, assignee="ameesh-dev2")],
                     milestones=[frozen(5083, 10, T - 4 * H, sha=HEAD),
                                 verdict(5083, 11, T - 4 * H + 60, "ameesh-cx1",
                                         "ok", sha=HEAD)],
                     events=[event(5083, 2, T - 4 * H + 30, carried_mark(5083, 10))])
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]],
                         ["ameesh-cx3", "ameesh-dev3"])

    def test_quorum_perdu_rend_l_avis_du(self):
        report = lot_5083(
            event(5083, 5, T - 3 * H, "validated → qa — review quorum changed (#4351)",
                  actor="ameesh", state="qa"))
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]],
                         ["ameesh-dev3"])

    def test_retour_en_build_leve_le_quorum(self):
        report = lot_5083(
            event(5083, 5, T - 3 * H, "paquet-horaire: exclu, retour en build",
                  actor="mesh-design", state="build"))
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]],
                         ["ameesh-dev3"])

    def test_designation_posterieure_a_la_validation_reste_due(self):
        report = lot_5083(
            event(5083, 5, T - 3 * H, "reviewer ameesh-dev4 designated",
                  actor="mesh-design", state="validated"))
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]],
                         ["ameesh-dev4"])

    def test_validation_d_un_gel_precedent_ne_vaut_pas(self):
        report = lot_5083(extra_milestones=[frozen(5083, 20, T - 2 * H, sha="b" * 40)])
        self.assertEqual(report["qa"][0]["sha"], "b" * 12)
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]], [])
        self.assertTrue(report["qa"][0]["missing_reviewer"])


if __name__ == "__main__":
    unittest.main()
