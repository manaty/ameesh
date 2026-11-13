# SPDX-License-Identifier: AGPL-3.0-only
"""#6214 : un lot relu dont un prérequis déclaré est encore ouvert n'est pas
une attente `qa_merge`.

Témoin du 07/11 (auditeur #199061) : #659 était classé `qa_merge` (2 h) alors
que #6209, en `qa`, était un prérequis bloquant déclaré par `ameesh work
dependencies` ; `--check` sortait 2 sur une attente que le pilote ne pouvait
pas lever. L'attente est celle du prérequis — comme `depends_on` (#3437) — et
`--check` ne sort 2 que si le prérequis lui-même dépasse son seuil.
"""
from __future__ import annotations

import json
import types
import unittest

from ameesh import debit
from tests.support import PgTestCase

T = 1792812000.0            # 2026-10-24 03:20:00Z
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


def edge(item_id, blocker_id, *, blocking=True, blocker_state="qa"):
    return {"work_item_id": item_id, "blocker_id": blocker_id,
            "created_by": "test", "created_ts": T - 40 * H,
            "blocker_state": blocker_state, "blocking": blocking}


def run(**extra):
    values = dict(items=[], milestones=[], events=[], agents=[], turns=[],
                  merges=[], ci_runs=[], jobs=[], now=T, installed="1.21.0",
                  devices={"names": [], "phrases": [], "sources": [],
                           "unreadable": []},
                  hours=6, wait_s=2 * H, ci_hours=24, ci_slow_s=30 * 60)
    values.update(extra)
    return debit.build(**values)


def waits_by_lot(report):
    return {(row["lot"], row["kind"]) for row in report["slow_waits"]}


class DebitPrerequisTest(unittest.TestCase):
    def test_intake_dependency_with_old_freeze_is_not_a_review_wait(self):
        for state, blocking, expected in (
                ("intake", True, False), ("intake", False, True),
                ("qa", True, True)):
            with self.subTest(state=state, blocking=blocking):
                report = run(
                    items=[item(7034, assignee=None), item(7107)],
                    milestones=[frozen(7034, 100, T - 29 * H)],
                    events=[event(7034, 101, T - 28 * H,
                                  "returned for prerequisite", state=state)],
                    dependencies={7034: [edge(7034, 7107, blocking=blocking)]})
                row = next(r for r in report["qa"] if r["lot"] == 7034)
                if blocking:
                    self.assertEqual(row["blocking_dependencies"], [7107])
                self.assertEqual(any(r["lot"] == 7034 for r in
                                     report["slow_waits"]), expected)

    def test_blocked_review_on_intake_keeps_dependency_as_wait_cause(self):
        report = run(
            items=[item(7034, assignee=None), item(7107)],
            milestones=[frozen(7034, 100, T - 29 * H),
                        verdict(7034, 101, T - 28 * H, kind="blocked")],
            events=[event(7034, 102, T - 27 * H,
                          "returned for prerequisite", state="intake")],
            dependencies={7034: [edge(7034, 7107)]})
        self.assertNotIn(7034, [r["lot"] for r in report["slow_waits"]])

    def test_prerequis_ouvert_pas_de_qa_merge_ni_de_sortie_2(self):
        # #659, relu depuis 4 h, dépend de #6209, relu il y a 1 h : le lot ne
        # peut pas entrer seul dans un paquet. Son attente est celle de #6209,
        # encore sous le seuil : le passage ne sort pas 2.
        child_milestones, child_events = reviewed(659, 100, T - 4 * H)
        parent_milestones, parent_events = reviewed(6209, 200, T - H)
        report = run(
            items=[item(659, branch="agent/mty-nxl-impl04-659-p12-final"),
                   item(6209, branch="review/6209-acte-message-marks")],
            milestones=child_milestones + parent_milestones,
            events=child_events + parent_events,
            dependencies={659: [edge(659, 6209)]})
        child = [row for row in report["qa"] if row["lot"] == 659][0]
        self.assertEqual(child["depends_on"], 6209)
        self.assertEqual(child["blocking_dependencies"], [6209])
        self.assertTrue(child["ready"])
        self.assertNotIn((659, "qa_merge"), waits_by_lot(report))
        self.assertNotIn(659, [row["lot"] for row in report["slow_waits"]])
        self.assertEqual(report["totals"]["slow_waits"], 0, report["slow_waits"])

    def test_prerequis_livre_rend_le_qa_merge_normal(self):
        # Le prérequis est fusionné : sa contrainte n'est plus bloquante et le
        # lot relu redevient une attente de paquet (même lecture que #3437
        # pour un parent terminé).
        child_milestones, child_events = reviewed(659, 100, T - 4 * H)
        report = run(
            items=[item(659, branch="agent/mty-nxl-impl04-659-p12-final"),
                   item(6209, branch="review/6209-acte-message-marks")],
            milestones=child_milestones + [frozen(6209, 200, T - 30 * H)],
            events=child_events,
            dependencies={659: [edge(659, 6209, blocking=False,
                                       blocker_state="merged")]})
        child = [row for row in report["qa"] if row["lot"] == 659][0]
        self.assertNotIn("depends_on", child)
        self.assertNotIn("blocking_dependencies", child)
        self.assertIn((659, "qa_merge"), waits_by_lot(report))

    def test_une_dependance_non_bloquante_ne_change_rien(self):
        # Un prérequis fermé reste lisible dans `work dependencies` (audit),
        # mais il ne retient plus rien : seul le prérequis ouvert compte.
        child_milestones, child_events = reviewed(659, 100, T - 4 * H)
        parent_milestones, parent_events = reviewed(6209, 200, T - 3 * H)
        report = run(
            items=[item(659, branch="agent/mty-nxl-impl04-659-p12-final"),
                   item(6209, branch="review/6209-acte-message-marks")],
            milestones=child_milestones + parent_milestones,
            events=child_events + parent_events,
            dependencies={659: [edge(659, 6209, blocking=False,
                                       blocker_state="merged")]})
        child = [row for row in report["qa"] if row["lot"] == 659][0]
        self.assertNotIn("blocking_dependencies", child)
        self.assertIn((659, "qa_merge"), waits_by_lot(report))

    def test_l_attente_du_prerequis_porte_le_lot_dependant(self):
        # Les deux lots sont au-delà du seuil : #6209 porte l'attente et nomme
        # #659, qui n'a plus de qa_merge propre. --check ne voit que le
        # prérequis (une attente de paquet réelle), pas une attente double.
        child_milestones, child_events = reviewed(659, 100, T - 4 * H)
        parent_milestones, parent_events = reviewed(6209, 200, T - 3 * H)
        report = run(
            items=[item(659, branch="agent/mty-nxl-impl04-659-p12-final"),
                   item(6209, branch="review/6209-acte-message-marks")],
            milestones=child_milestones + parent_milestones,
            events=child_events + parent_events,
            dependencies={659: [edge(659, 6209)]})
        self.assertNotIn(659, [row["lot"] for row in report["slow_waits"]])
        parent = [row for row in report["slow_waits"]
                  if (row["lot"], row["kind"]) == (6209, "qa_merge")][0]
        self.assertEqual(parent["dependents"], [659])
        self.assertEqual(report["totals"]["slow_waits"], 1,
                         report["slow_waits"])

    def test_plusieurs_prerequis_n_en_nomme_que_les_bloquants(self):
        child_milestones, child_events = reviewed(659, 100, T - 4 * H)
        report = run(
            items=[item(659, branch="agent/mty-nxl-impl04-659-p12-final"),
                   item(6209, branch="review/6209-acte-message-marks")],
            milestones=child_milestones + [frozen(6209, 200, T - 30 * H)],
            events=child_events,
            dependencies={659: [edge(659, 6209, blocking=False,
                                       blocker_state="merged"),
                                 edge(659, 6208)]})
        child = [row for row in report["qa"] if row["lot"] == 659][0]
        self.assertEqual(child["depends_on"], 6208)
        self.assertEqual(child["blocking_dependencies"], [6208])
        self.assertNotIn((659, "qa_merge"), waits_by_lot(report))

    def test_la_cible_de_branche_et_le_prerequis_coexistent(self):
        # #3437 et #6214 ensemble : le lot suit la branche de #700 et dépend
        # aussi de #6209. Les deux attendes sont nommées, le lot reste hors
        # qa_merge et les deux prérequis le portent.
        parent_milestones, parent_events = reviewed(700, 300, T - 5 * H)
        other_milestones, other_events = reviewed(6209, 200, T - 4 * H)
        child_milestones, child_events = reviewed(659, 100, T - 3 * H)
        report = run(
            items=[item(700, branch="agent/deepseek6-700-base"),
                   item(6209, branch="review/6209-acte-message-marks"),
                   item(659, branch="review/659-fiche",
                        branch_target="agent/deepseek6-700-base")],
            milestones=parent_milestones + other_milestones + child_milestones,
            events=parent_events + other_events + child_events,
            dependencies={659: [edge(659, 6209)]})
        child = [row for row in report["qa"] if row["lot"] == 659][0]
        self.assertEqual(child["depends_on"], 700)
        self.assertEqual(child["blocking_dependencies"], [6209])
        self.assertNotIn((659, "qa_merge"), waits_by_lot(report))
        for parent in (700, 6209):
            row = [wait for wait in report["slow_waits"]
                   if (wait["lot"], wait["kind"]) == (parent, "qa_merge")][0]
            self.assertEqual(row["dependents"], [659])


class ReadPrerequisTest(PgTestCase):
    """La lecture porte les prérequis déclarés jusqu'au calcul (#6214)."""

    def _item(self, ident, assignee, *, branch=None, branch_target=None,
              app="nexlink", state="qa"):
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state, "
            "assignee, branch, branch_target) "
            "VALUES (%s, 'evolution', 'test', %s, %s, %s, %s, %s, %s)",
            (ident, "lot %d" % ident, app, state, assignee, branch,
             branch_target))

    def _milestone(self, ident, mid, kind, at, sha="a" * 40,
                   actor="auteur", verdict=None):
        self.db.execute(
            "INSERT INTO work_item_milestones "
            "(id, work_item_id, kind, at, sha, actor, verdict, note) "
            "VALUES (%s, %s, %s, to_timestamp(%s), %s, %s, %s, '')",
            (mid, ident, kind, at, sha, actor, verdict))

    def _event(self, ident, eid, at, note, actor="pilote", state="qa"):
        self.db.execute(
            "INSERT INTO work_item_events "
            "(id, work_item_id, state, note, actor, created_at) "
            "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s))",
            (eid, ident, state, note, actor, at))

    def _reviewed(self, ident, freeze_id, at):
        self._milestone(ident, freeze_id, "frozen", at)
        self._event(ident, freeze_id, at,
                    mark(ident, freeze_id, ["orchestrateur"]), actor="ameesh")
        self._milestone(ident, freeze_id + 1, "verdict", at + H,
                        actor="orchestrateur", verdict="ok")

    def test_read_transmet_le_prerequis_bloquant(self):
        canons = types.SimpleNamespace(hosts=[], processes=[])
        self._item(7100, "mty-nxl-impl-04",
                   branch="agent/mty-nxl-impl04-659-p12-final")
        self._item(7101, "mty-nxl-impl-04",
                   branch="review/6209-acte-message-marks")
        self._reviewed(7100, 17100, T - 4 * H)
        self._reviewed(7101, 17200, T - 3 * H)
        self.db.execute(
            "INSERT INTO work_item_dependencies "
            "(work_item_id, blocker_id, created_by) VALUES (7100, 7101, 'test')")
        report = debit.read(self.db, cfg=self.cfg, now=T,
                            canons=[canons], installed="1.21.0", jobs=False)
        child = [row for row in report["qa"] if row["lot"] == 7100][0]
        self.assertEqual(child["depends_on"], 7101)
        self.assertEqual(child["blocking_dependencies"], [7101])
        self.assertNotIn(7100, [row["lot"] for row in report["slow_waits"]])
        parent = [row for row in report["slow_waits"]
                  if (row["lot"], row["kind"]) == (7101, "qa_merge")][0]
        self.assertEqual(parent["dependents"], [7100])

    def test_read_ne_bloque_pas_sur_un_prerequis_fusionne(self):
        canons = types.SimpleNamespace(hosts=[], processes=[])
        self._item(7102, "mty-nxl-impl-04",
                   branch="agent/mty-nxl-impl04-659-p12-final")
        self._item(7103, "mty-nxl-impl-04",
                   branch="review/6209-acte-message-marks", state="merged")
        self._reviewed(7102, 17102, T - 4 * H)
        self._milestone(7103, 17203, "frozen", T - 30 * H)
        self._milestone(7103, 17204, "merged", T - 20 * H)
        self.db.execute(
            "INSERT INTO work_item_dependencies "
            "(work_item_id, blocker_id, created_by) VALUES (7102, 7103, 'test')")
        report = debit.read(self.db, cfg=self.cfg, now=T,
                            canons=[canons], installed="1.21.0", jobs=False)
        child = [row for row in report["qa"] if row["lot"] == 7102][0]
        self.assertNotIn("blocking_dependencies", child)
        self.assertIn((7102, "qa_merge"), waits_by_lot(report))


if __name__ == "__main__":
    unittest.main()
