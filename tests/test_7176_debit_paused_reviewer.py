# SPDX-License-Identifier: AGPL-3.0-only
"""#7176 — le débit nomme un relecteur désigné, même en pause opérateur.

Constat du 13/11 03:17Z : « Avis dus par relecteur : codex1 1 » seulement,
alors qu'ameesh-cx2 portait un siège manuel sur #6961 (P1) depuis 19:58Z et
que le gel #40142 (02:45Z) ne le nommait plus dans sa marque automatique.
`pending_reviews` le comptait ; `designations()` l'écartait parce qu'il
comparait la redésignation à l'instant du gel (#3033) au lieu de l'id de la
dernière marque qui portait le nom (#3602). Le débit doit donc lister tout
avis dû sur la tête gelée et marquer la pause (`en pause : operator paused`)
au lieu de le laisser hors de la ligne que lit le passage court de l'auditeur
(`docs/AUDITEUR.md`, point 0).

Témoins : un siège manuel redésigné après la dernière marque qui le portait
reste dû au gel suivant, avec sa pause ; sans pause il reste dû aussi ; une
marque plus récente qui le nomme le rend automatique et le gel suivant ne le
doit plus (règle #3033 conservée).
"""
from __future__ import annotations

import json
import unittest

from ameesh import debit

T = 1794539820.0            # 2026-11-13 03:17:00Z
H = 3600.0
LOT = 6961


def item(assignee="mty-amh-impl-05"):
    return {"id": LOT, "title": "comptes : plafond du tour", "app": "ameesh",
            "assignee": assignee, "priority": 1}


def frozen(mid, at, sha):
    return {"work_item_id": LOT, "id": mid, "kind": "frozen", "at_ts": at,
            "sha": sha, "actor": "auteur", "verdict": None, "note": ""}


def verdict(mid, at, actor, sha, kind="ok"):
    return {"work_item_id": LOT, "id": mid, "kind": "verdict", "at_ts": at,
            "sha": sha, "actor": actor, "verdict": kind, "note": ""}


def event(eid, at, note, *, actor="mesh-design", state="qa"):
    return {"work_item_id": LOT, "id": eid, "state": state, "at_ts": at,
            "actor": actor, "note": note}


def mark(freeze_id, names, *, state="designated", required=2, missing=0):
    return "review-designation:%d:%d: %s" % (
        LOT, freeze_id,
        json.dumps({"state": state, "required": required, "missing": missing,
                    "why": "test",
                    "reviewers": [{"name": name, "family":
                                   "codex" if "cx" in name else "claude"}
                                  for name in names],
                    "authors": [{"name": "mty-amh-impl-05",
                                 "family": "deepseek"}],
                    "freeze": {"id": freeze_id, "sha": "b" * 40}}))


def agent(name, harness, *, operator_paused=False):
    return {"name": name, "harness": harness, "status": "idle",
            "status_text": "last turn: mail", "mode": "execute",
            "team": "ameesh", "operator_paused": operator_paused}


def run(*, paused=True, verdicts=(), events=None):
    """Le rapport du débit au gel #200, avec le siège manuel de cx2."""
    events = events if events is not None else [
        # Gel #100 : cx2 était automatique.
        event(1, T - 4 * H, mark(100, ["auditeur", "ameesh-cx2"])),
        # Puis le pilote le redésigne à la main (après la marque qui le porte).
        event(2, T - 3 * H, "reviewer ameesh-cx2 designated"),
        # Gel #200 : la marque automatique ne le nomme plus (pause opérateur).
        event(3, T - 2 * H, mark(200, ["auditeur"])),
    ]
    return debit.build(
        items=[item()],
        milestones=[frozen(100, T - 4 * H, "a" * 40),
                    frozen(200, T - 2 * H, "b" * 40), *verdicts],
        events=events,
        agents=[agent("auditeur", "claude"),
                agent("ameesh-cx2", "codex", operator_paused=paused)],
        turns=[], merges=[], ci_runs=[], jobs=[], now=T,
        installed="1.51.0",
        devices={"names": [], "phrases": [], "sources": [], "unreadable": []},
        hours=6, wait_s=2 * H, ci_hours=24, ci_slow_s=30 * 60,
        families=("claude", "codex", "deepseek"), fixes=[])


class ManualSeatAfterMarkTest(unittest.TestCase):
    """Un siège manuel survit au gel qui ne le nomme plus (#3602/#7176)."""

    def test_le_siege_manuel_reste_du_et_la_pause_est_marquee(self):
        report = run(verdicts=[verdict(4, T - H, "auditeur", "b" * 40)])
        due = [row for row in report["reviews_due"] if row["reviewer"] == "ameesh-cx2"]
        self.assertEqual(len(due), 1, report["reviews_due"])
        self.assertEqual(due[0]["lot"], LOT)
        self.assertEqual(due[0]["paused"], "operator paused")
        self.assertEqual(report["reviews_by_reviewer"], [{
            "reviewer": "ameesh-cx2", "count": 1, "oldest_age_s": 2 * H,
            "lots": [LOT], "paused": "operator paused"}])
        qa = [row for row in report["qa"] if row["lot"] == LOT][0]
        self.assertEqual(qa["pending_reviewers"], ["ameesh-cx2"])
        self.assertEqual(qa["pending_paused"],
                         {"ameesh-cx2": "operator paused"})
        self.assertFalse(qa["ready"])
        # Une pause opérateur n'est pas une capacité au repos : la ligne des
        # avis dus la nomme et marque la pause, `idle_agents_with_work` non.
        self.assertEqual(report["idle_agents_with_work"], [])
        text = debit.format_text(report)
        self.assertIn("Avis dus par relecteur : ameesh-cx2 1", text)
        self.assertIn("en pause : operator paused", text)
        self.assertIn("relecteur(s) en pause : ameesh-cx2 (operator paused)",
                      text)

    def test_sans_pause_le_siege_reste_du(self):
        report = run(paused=False, verdicts=[verdict(4, T - H, "auditeur", "b" * 40)])
        due = [row for row in report["reviews_due"] if row["reviewer"] == "ameesh-cx2"]
        self.assertEqual(len(due), 1, report["reviews_due"])
        self.assertIsNone(due[0]["paused"])
        self.assertEqual(report["reviews_by_reviewer"][0]["paused"], None)
        idle = [row for row in report["idle_agents_with_work"]
                if row["agent"] == "ameesh-cx2"]
        self.assertEqual(len(idle), 1, report["idle_agents_with_work"])
        self.assertIn("relancer le relecteur ameesh-cx2", idle[0]["gesture"])

    def test_une_marque_plus_recente_le_rend_automatique(self):
        # #3033 conservé : le nom est de nouveau automatique ; sur le gel
        # suivant, qui ne le nomme pas, il n'est plus dû.
        events = [
            event(1, T - 4 * H, mark(100, ["auditeur", "ameesh-cx2"])),
            event(2, T - 3 * H, "reviewer ameesh-cx2 designated"),
            event(3, T - 3 * H + 60, mark(100, ["auditeur", "ameesh-cx2"])),
            event(4, T - 2 * H, mark(200, ["auditeur"])),
        ]
        report = run(paused=False,
                     verdicts=[verdict(5, T - H, "auditeur", "b" * 40)],
                     events=events)
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]], [])


if __name__ == "__main__":
    unittest.main()
