# SPDX-License-Identifier: AGPL-3.0-only
"""#5430 : une marque de désignation `waiting` n'est pas un quorum.

Témoin du 02/11 vers 01:03Z (auditeur, sur origin/main) : #5356, #5364,
#5382, #5396 et #5402 sortaient `ready: true`, `pending_reviewers: []`,
`missing_reviewer: false`, alors que la marque du gel portait `state:
waiting`, `required: 2`, `missing: 2`, `reviewers: []` et « 0 eligible
families ». Un seul avis (celui du pilote, famille claude) était acquis.

`_mark()` ne lisait que `reviewers` et `authors` ; `designations()` ne
mettait `missing` à vrai que sans aucune marque, aucun désigné et aucun
verdict. Une liste `pending` vide se lisait donc « quorum atteint » alors
qu'elle disait « aucune famille éligible ».
"""
from __future__ import annotations

import json
import unittest

from tests.test_3292_debit import H, T, event, frozen, item, run, verdict

HEAD = "a" * 40
FAMILIES = ("claude", "codex", "deepseek")


def waiting_mark(ident, freeze_id, *, required=2, missing=2, reviewers=(),
                 authors=(("mty-nxl-impl-04", "deepseek"),),
                 why="2 independent reviews missing: 0 eligible families (none); "
                     "forbidden author families: deepseek"):
    """Une marque réelle de désignation en attente (#1617)."""
    return "review-designation:%d:%d: %s" % (ident, freeze_id, json.dumps({
        "state": "waiting",
        "required": required,
        "missing": missing,
        "reviewers": [{"name": name, "family": family}
                      for name, family in reviewers],
        "authors": [{"name": name, "family": family} for name, family in authors],
        "why": why,
        "freeze": {"id": freeze_id, "sha": HEAD},
        "sent": [], "unknown_authors": [], "policy_ref": "",
    }, ensure_ascii=False, sort_keys=True))


def designated_mark(ident, freeze_id, *, required=2, reviewers=(),
                    authors=(("mty-amh-docs-01", "deepseek"),),
                    why="designated reviewer(s)"):
    """Une marque réelle de sièges pourvus : elle garde l'ancien sens."""
    return "review-designation:%d:%d: %s" % (ident, freeze_id, json.dumps({
        "state": "designated",
        "required": required,
        "missing": 0,
        "reviewers": [{"name": name, "family": family}
                      for name, family in reviewers],
        "authors": [{"name": name, "family": family} for name, family in authors],
        "why": why,
        "freeze": {"id": freeze_id, "sha": HEAD},
        "sent": [], "unknown_authors": [], "policy_ref": "",
    }, ensure_ascii=False, sort_keys=True))


def witness_5356(*extra_events, extra_milestones=()):
    """Le gel et la marque réels de #5356 : auteur deepseek, avis claude seul."""
    milestones = [frozen(5356, 30394, T - 4 * H, sha=HEAD),
                  verdict(5356, 30395, T - 4 * H + 60, "orchestrateur", "ok",
                          sha=HEAD),
                  *extra_milestones]
    events = [event(5356, 1, T - 4 * H, "build → qa — frozen (#3467)"),
              event(5356, 2, T - 4 * H + 30, waiting_mark(5356, 30394)),
              event(5356, 3, T - 4 * H + 60,
                    "reviewer orchestrateur designated", actor="orchestrateur"),
              *extra_events]
    return run(items=[item(5356, assignee="mty-nxl-impl-04")],
               milestones=milestones, events=events,
               agents=[{"name": "orchestrateur", "status": "idle",
                        "harness": "claude"},
                       {"name": "mty-nxl-rev-02", "status": "blocked",
                        "harness": "codex"}],
               families=FAMILIES)


class WaitingMarkTest(unittest.TestCase):
    def test_temoin_5356_un_avis_sur_deux_n_est_pas_pret(self):
        report = witness_5356()
        row = report["qa"][0]
        self.assertFalse(row["ready"])
        self.assertTrue(row["missing_reviewer"])
        self.assertEqual(row["missing_reviews"], 1)
        self.assertEqual(row["required_reviews"], 2)
        self.assertEqual(row["acquired_reviews"], 1)
        self.assertEqual(row["pending_reviewers"], [])
        self.assertIn("aucune famille éligible", row["missing_cause"])
        self.assertIn("forbidden author families", row["missing_reason"])
        # Aucun relecteur nommé : ce n'est pas un avis dû nominatif, c'est un
        # siège impossible à pourvoir, et le débit le dit avec sa cause.
        self.assertEqual(report["reviews_due"], [])
        waits = [w for w in report["slow_waits"] if w["lot"] == 5356]
        self.assertEqual([w["kind"] for w in waits], ["qa"])
        self.assertIn("aucune famille éligible", waits[0]["gesture"])
        self.assertIn("aucune famille éligible", waits[0]["cause"])
        self.assertEqual(waits[0]["missing"], 1)
        self.assertEqual(report["totals"]["missing_reviewer"], 1)

    def test_required_un_avec_un_avis_reste_pret(self):
        report = run(
            items=[item(7, assignee="ameesh-dev2")],
            milestones=[frozen(7, 10, T - 4 * H, sha=HEAD),
                        verdict(7, 11, T - 4 * H + 60, "claude", "ok", sha=HEAD)],
            events=[event(7, 1, T - 4 * H + 30,
                          waiting_mark(7, 10, required=1, missing=0,
                                       authors=(("ameesh-dev2", "deepseek"),),
                                       why="1 independent review missing: "
                                           "1 eligible family (claude); "
                                           "forbidden author families: deepseek"))],
            families=FAMILIES)
        row = report["qa"][0]
        self.assertTrue(row["ready"])
        self.assertFalse(row["missing_reviewer"])
        self.assertEqual(row["missing_reviews"], 0)
        self.assertIsNone(row["missing_cause"])

    def test_deux_avis_de_meme_famille_ne_valent_qu_un(self):
        report = run(
            items=[item(8, assignee="mty-amh-impl-04")],
            milestones=[frozen(8, 10, T - 4 * H, sha=HEAD),
                        verdict(8, 11, T - 4 * H + 60, "deepseek4", "ok",
                                sha=HEAD),
                        verdict(8, 12, T - 4 * H + 90, "deepseek7", "ok",
                                sha=HEAD)],
            events=[event(8, 1, T - 4 * H + 30,
                          waiting_mark(8, 10, reviewers=(), authors=(
                              ("mty-amh-impl-04", "claude"),),
                              why="2 independent reviews missing: 0 eligible "
                                  "families (none); forbidden author families: claude"))],
            agents=[{"name": "deepseek4", "status": "running",
                     "harness": "deepseek"},
                    {"name": "deepseek7", "status": "running",
                     "harness": "deepseek"}],
            families=FAMILIES)
        row = report["qa"][0]
        self.assertEqual(row["acquired_reviews"], 1)
        self.assertEqual(row["missing_reviews"], 1)
        self.assertFalse(row["ready"])
        self.assertTrue(row["missing_reviewer"])

    def test_un_avis_de_la_famille_de_l_auteur_ne_compte_pas(self):
        report = run(
            items=[item(9, assignee="mty-amh-impl-04")],
            milestones=[frozen(9, 10, T - 4 * H, sha=HEAD),
                        verdict(9, 11, T - 4 * H + 60, "deepseek4", "ok",
                                sha=HEAD)],
            events=[event(9, 1, T - 4 * H + 30, waiting_mark(9, 10))],
            agents=[{"name": "deepseek4", "status": "running",
                     "harness": "deepseek"}],
            families=FAMILIES)
        row = report["qa"][0]
        self.assertEqual(row["acquired_reviews"], 0)
        self.assertEqual(row["missing_reviews"], 2)
        self.assertFalse(row["ready"])

    def test_deux_familles_distinctes_atteignent_le_quorum(self):
        report = run(
            items=[item(10, assignee="mty-amh-impl-04")],
            milestones=[frozen(10, 10, T - 4 * H, sha=HEAD),
                        verdict(10, 11, T - 4 * H + 60, "orchestrateur", "ok",
                                sha=HEAD),
                        verdict(10, 12, T - 4 * H + 120, "ameesh-cx2", "ok",
                                sha=HEAD)],
            events=[event(10, 1, T - 4 * H + 30, waiting_mark(10, 10))],
            agents=[{"name": "orchestrateur", "status": "idle",
                     "harness": "claude"},
                    {"name": "ameesh-cx2", "status": "idle",
                     "harness": "codex"}],
            families=FAMILIES)
        row = report["qa"][0]
        self.assertEqual(row["acquired_reviews"], 2)
        self.assertEqual(row["missing_reviews"], 0)
        self.assertFalse(row["missing_reviewer"])
        self.assertTrue(row["ready"])

    def test_marque_designated_garde_ses_avis_dus(self):
        report = run(
            items=[item(5347, assignee="mty-amh-docs-01")],
            milestones=[frozen(5347, 30335, T - 4 * H, sha=HEAD),
                        verdict(5347, 30336, T - 4 * H + 90, "auditeur", "ok",
                                sha=HEAD)],
            events=[event(5347, 1, T - 4 * H + 30,
                          designated_mark(5347, 30335, reviewers=(
                              ("auditeur", "claude"), ("ameesh-cx2", "codex")))
                          )],
            agents=[{"name": "auditeur", "status": "running",
                     "harness": "claude"}],
            families=FAMILIES)
        row = report["qa"][0]
        self.assertFalse(row["ready"])
        self.assertFalse(row["missing_reviewer"])
        self.assertEqual(row["missing_reviews"], 0)
        self.assertEqual(row["pending_reviewers"], ["ameesh-cx2"])

    def test_validation_depuis_le_gel_leve_le_manque(self):
        report = witness_5356(event(5356, 4, T - 3 * H,
                                    "qa → validated — review quorum (#4423)",
                                    actor="ameesh", state="validated"))
        row = report["qa"][0]
        self.assertTrue(row["ready"])
        self.assertFalse(row["missing_reviewer"])
        self.assertEqual(row["missing_reviews"], 0)
        self.assertEqual(row["pending_reviewers"], [])
        self.assertIsNotNone(row["frozen"])


if __name__ == "__main__":
    unittest.main()
