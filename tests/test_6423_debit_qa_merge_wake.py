# SPDX-License-Identifier: AGPL-3.0-only
"""#6423 : un lot relu avec un réveil non échu n'attend pas le paquet.

Témoin : #659 (nexlink) portait un réveil de l'assigné au 09/11 11:30Z ; le
débit le classait `qa_merge` et rappelait le pilote à chaque passage depuis
30 h. Un réveil armé, comme une fenêtre d'observation, est une attente voulue
et datée : rien n'est actionnable avant l'échéance. Le réveil échu, lui, garde
son geste (`wake_overdue`) et la ligne `qa_merge` reste due.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import debit, packet_candidates

from .test_4572_slo_candidate_cause import _SloBase

T = 1794186000.0            # 2026-11-09 01:00:00Z
H = 3600.0


def item(ident, *, app="nexlink", branch=None, branch_target=None,
         assignee="deepseek6"):
    return {"id": ident, "title": "lot %d" % ident, "app": app,
            "assignee": assignee, "priority": 2,
            "branch": branch, "branch_target": branch_target}


def frozen(ident, mid, at, sha="a" * 40):
    return {"work_item_id": ident, "id": mid, "kind": "frozen", "at_ts": at,
            "sha": sha, "actor": "auteur", "verdict": None, "note": ""}


def verdict(ident, mid, at, actor="orchestrateur", kind="ok", sha="a" * 40):
    return {"work_item_id": ident, "id": mid, "kind": "verdict", "at_ts": at,
            "sha": sha, "actor": actor, "verdict": kind, "note": ""}


def event(ident, eid, at, note, actor="pilote", state="qa"):
    return {"work_item_id": ident, "id": eid, "state": state, "at_ts": at,
            "actor": actor, "note": note}


def mark(ident, freeze_id, names):
    return "review-designation:%d:%d: %s" % (
        ident, freeze_id,
        json.dumps({"reviewers": [{"name": n} for n in names]}))


def reviewed(ident, freeze_id, at, reviewer="orchestrateur"):
    """Un lot relu : gel, désignation, verdict ok sur la même tête."""
    return ([frozen(ident, freeze_id, at),
             verdict(ident, freeze_id + 1, at + H)],
            [event(ident, freeze_id, at, mark(ident, freeze_id, [reviewer]))])


def run(**extra):
    values = dict(items=[], milestones=[], events=[], agents=[], turns=[],
                  merges=[], ci_runs=[], jobs=[], now=T, installed="1.45.0",
                  devices={"names": [], "phrases": [], "sources": [],
                           "unreadable": []},
                  hours=6, wait_s=2 * H, ci_hours=24, ci_slow_s=30 * 60)
    values.update(extra)
    return debit.build(**values)


def kinds(report):
    return [(row["lot"], row["kind"]) for row in report["slow_waits"]]


class QaMergeWakeTest(unittest.TestCase):
    def _lot(self, notes):
        milestones, events = reviewed(659, 100, T - 6 * H)
        for index, note in enumerate(notes):
            events = events + [event(659, 200 + index, T - H, note,
                                     actor="deepseek6")]
        return run(items=[item(659, branch="review/deepseek6-659-panier",
                               branch_target="develop", assignee="deepseek6")],
                   milestones=milestones, events=events)

    def test_reveil_futur_ne_produit_pas_de_ligne_qa_merge(self):
        report = self._lot(
            ["wake: attente du paquet (due 2026-11-09 03:00 UTC)"])
        self.assertNotIn((659, "qa_merge"), kinds(report))
        self.assertNotIn(659, [row["lot"] for row in report["slow_waits"]])
        # Le réveil reste lisible : ce n'est pas une attente perdue, elle est
        # seulement datée.
        self.assertEqual([row["lot"] for row in report["wakes"]], [659])
        self.assertEqual(report["wakes"][0]["due"], "2026-11-09T03:00:00Z")

    def test_reveil_echu_garde_la_ligne_qa_merge(self):
        report = self._lot(
            ["wake: attente du paquet (due 2026-11-08 20:00 UTC)"])
        self.assertIn((659, "qa_merge"), kinds(report))
        self.assertIn((659, "wake_overdue"), kinds(report))

    def test_fenetre_non_echue_ne_produit_pas_de_ligne_qa_merge(self):
        report = self._lot(
            ["hold: fenêtre d'observation — vérifier le paquet "
             "(due 2026-11-09 03:00 UTC)"])
        self.assertNotIn((659, "qa_merge"), kinds(report))
        self.assertNotIn(659, [row["lot"] for row in report["slow_waits"]])
        self.assertEqual([row["lot"] for row in report["window_waits"]], [659])


class PausedAuthorTest(_SloBase):
    """#6423 annexe : la relance ne nomme pas un auteur endormi (#6396)."""

    CAUSE = {"code": "freeze-not-head", "owner": "author",
             "detail": "freeze 1dd33c0e17c3 is not the published head",
             "gesture": "freeze again on the published head, then have it reviewed",
             "ready": False, "unknown": False}

    def _report(self):
        self.db.execute(
            "UPDATE work_item_milestones SET at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND kind = 'frozen'",
            (self.now - 3 * H, self.lot["id"]))
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(self.CAUSE)):
            return debit.read(self.db, cfg=self.cfg, now=self.now, jobs=False,
                              wait_s=2 * H)

    def _row(self, report):
        rows = [row for row in report["slow_waits"]
                if row["kind"] == "qa_merge" and row["lot"] == self.lot["id"]]
        self.assertEqual(len(rows), 1, report["slow_waits"])
        return rows[0]

    def test_un_auteur_en_pause_renvoie_le_geste_au_pilote(self):
        self.db.execute(
            "UPDATE agent_registry SET operator_paused = true,"
            " operator_pause_reason = 'automatic sleep' WHERE name = 'dev1'")
        row = self._row(self._report())
        self.assertEqual(row["who"], "pilote")
        self.assertIn("pause opérateur", row["gesture"])
        self.assertIn("freeze-not-head", row["gesture"])
        self.assertIn("freeze again", row["gesture"])

    def test_un_auteur_actif_reste_nomme(self):
        row = self._row(self._report())
        self.assertEqual(row["who"], "dev1")
        self.assertNotIn("pause opérateur", row["gesture"])


if __name__ == "__main__":
    unittest.main()
