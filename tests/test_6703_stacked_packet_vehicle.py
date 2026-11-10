# SPDX-License-Identifier: AGPL-3.0-only
"""#6703 — un porteur de paquet n'est jamais « empilé » sur ses membres.

Constat (auditeur, alerte SLO #208593 du 10/11 12:15Z) : le lot porteur #6700
(branche `integration/paquet-530-nexlink`, validé) recevait une alerte
« review GO → merge » de cause `stacked-undeclared`, lui demandant de déclarer
une dépendance sur ses propres lots membres. La composition écartait bien le
porteur (`packet-vehicle`), mais la lecture du refus (`candidate_refusal`),
qui alimente l'explication SLO et le débit, ne faisait pas la même garde.

Attendu : même exemption `packet-vehicle` avant `_stacked_for` ; un porteur
validé ne reçoit aucune cause d'auteur `stacked-undeclared` et l'alerte nomme
sa vraie attente (la CI complète du paquet et sa fusion). Un lot ordinaire
empilé garde le refus de #6607.

Témoin : dépôt jetable, A ouvert sur `review/a`, porteur `integration/paquet`
qui fusionne A ; `candidate_refusal` du porteur rend `ready` sans
`stacked-undeclared`, tandis que B, branché sur A, garde la cause.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import alert_briefs, exploitation, packet_candidates, registry, work

from .support import PgTestCase
from .test_4572_slo_candidate_cause import git, write_commit

HEURE = 3600.0


def vehicle_repo(root):
    """`main` ; `review/a` (lot A) non fusionné ; porteur qui fusionne A ;
    `lot/b` branché sur A."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    base = write_commit(root, "f", "base\n", "base")
    git(root, "checkout", "-q", "-b", "review/a")
    a_tip = write_commit(root, "a", "a\n", "lot A, non relu")
    git(root, "checkout", "-q", "main")
    git(root, "checkout", "-q", "-b", "integration/paquet-test")
    git(root, "merge", "-q", "--no-edit", "review/a")
    vehicle = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "-b", "lot/b", "review/a")
    b_tip = write_commit(root, "b", "b\n", "lot B")
    git(root, "checkout", "-q", "main")
    for ref, sha in (("main", base), ("review/a", a_tip), ("lot/b", b_tip),
                     ("integration/paquet-test", vehicle)):
        git(root, "update-ref", "refs/remotes/origin/" + ref, sha)
    return base, a_tip, b_tip, vehicle


class CandidateRefusalTest(unittest.TestCase):
    """Le moteur de refus, sans base : la garde packet-vehicle."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-6703-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        (self.base, self.a_tip, self.b_tip,
         self.vehicle) = vehicle_repo(self.repo)
        self.others = [
            {"lot": 1, "branch": "review/a", "shas": [self.a_tip]},
            {"lot": 2, "branch": "lot/b", "shas": [self.b_tip]},
            {"lot": 3, "branch": "integration/paquet-test", "shas": [self.vehicle]},
        ]

    def refusal(self, lot, branch, sha):
        row = {"id": lot, "branch": branch, "branch_target": "main",
               "assignee": "dev1"}
        return packet_candidates.candidate_refusal(
            None, None, row, [{"kind": "frozen", "sha": sha, "at_ts": 1.0}],
            repo=self.repo, open_heads=self.others)

    def test_un_porteur_de_paquet_n_est_pas_empile_sur_ses_membres(self):
        # #6703 : le porteur `integration/**` porte le commit de A ; sans la
        # garde, `_stacked_for` le nommait empilé sur A et l'alerte SLO
        # demandait une dépendance absurde.
        cause = self.refusal(3, "integration/paquet-test", self.vehicle)
        self.assertEqual(cause["code"], "")
        self.assertTrue(cause["ready"])
        self.assertNotIn("stacked-undeclared", cause.get("detail", ""))

    def test_un_lot_ordinaire_empile_garde_le_refus_de_6607(self):
        cause = self.refusal(2, "lot/b", self.b_tip)
        self.assertEqual(cause["code"], "stacked-undeclared")
        self.assertEqual(cause["owner"], "author")
        self.assertEqual(cause["depends_on"], 1)
        self.assertFalse(cause["ready"])


class SloVehicleTest(PgTestCase):
    """Le chemin réel de l'alerte : le porteur nomme sa propre CI."""

    GO = 1_792_950_000.0
    HEAD = "a" * 40

    def setUp(self):
        super().setUp()
        for name in ("dev1", "orch", "bureau", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-' || name,"
            " lease_expires_at = now() + interval '1 hour'")
        self.carrier = work.add(self.db, title="porteur de paquet test",
                                app="ameesh", branch="integration/paquet-test",
                                actor="test")
        work.move(self.db, self.carrier["id"], "build")
        work.move(self.db, self.carrier["id"], "qa")
        created = self.GO - 2 * HEURE
        self.db.execute(
            "UPDATE work_items SET created_at = to_timestamp(%s), branch_head = %s"
            " WHERE id = %s", (created, self.HEAD, self.carrier["id"]))
        self.db.execute(
            "UPDATE work_item_events SET created_at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND state = 'build'",
            (created + 1800.0, self.carrier["id"]))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor)"
            " VALUES (%s, 'frozen', to_timestamp(%s), 'dev1')",
            (self.carrier["id"], self.GO - 1))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor,"
            " verdict) VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok')",
            (self.carrier["id"], self.GO))
        self.db.execute(
            "INSERT INTO ci_runs (repo, run_id, head_sha, branch, event, base,"
            " work_item_id, status, url, workflow, first_seen_at)"
            " VALUES ('manaty/ameesh', %s, %s, 'integration/paquet-test', 'push',"
            " 'main', %s, 'running', 'u', 'tests.yml', to_timestamp(%s))",
            (700001, self.HEAD, self.carrier["id"], self.GO))
        self.now = self.GO + 3 * HEURE

    def alerts(self):
        args = exploitation.build_parser().parse_args(["alerts"])
        seuils = exploitation.thresholds(args)
        seuils.update(orchestrators_declared=["orch"])
        with mock.patch.object(exploitation, "_review_gate_open",
                               return_value=True):
            rows = exploitation.alerts(self.cfg, self.db, now=self.now, **seuils)
        return [row for row in rows
                if row["type"] == "slo_breach"
                and row["lot"] == self.carrier["id"]
                and row["step"] == "verdict_merge"]

    def briefs(self, alert):
        listing = [{"name": n, "team": ""}
                   for n in ("dev1", "orch", "bureau", "auditeur")]
        return alert_briefs.briefs(alert, listing, orchestrator_names=("orch",),
                                   auditor_names=("auditeur",))

    def test_l_alerte_du_porteur_nomme_sa_ci_et_pas_un_empilement(self):
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1, alerts)
        waiting = alerts[0]["waiting"] or {}
        self.assertEqual(waiting.get("what"), "packet")
        self.assertTrue(waiting.get("self"))
        self.assertEqual(waiting.get("vehicle"), self.carrier["id"])
        self.assertEqual((waiting.get("ci") or {}).get("state"), "running")
        briefs = self.briefs(alerts[0])
        for text in briefs.values():
            self.assertIn("itself the packet vehicle", text)
            self.assertIn("run 700001", text)
            self.assertNotIn("stacked-undeclared", text)
            self.assertNotIn("include it in the next package", text)


if __name__ == "__main__":
    unittest.main()
