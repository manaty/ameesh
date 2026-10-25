# SPDX-License-Identifier: AGPL-3.0-only
"""#3437 : un lot relu qui cible la branche d'un autre lot ouvert attend ce lot.

Témoin du 25/10 : #3260 (Nexlink, panier de #693) était classé `qa_merge`
alors qu'il cible `agent/deepseek6-693-eco-exp-1`, la branche de #693, pas
`develop` : le pilote ne peut pas le ranger seul dans un paquet horaire.
L'attente se rattache à #693 et suit son hold ; le rapport la marque
`depends_on` sans produire de `qa_merge` ni de sortie 2 pour elle.
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


def run(**extra):
    values = dict(items=[], milestones=[], events=[], agents=[], turns=[],
                  merges=[], ci_runs=[], jobs=[], now=T, installed="1.21.0",
                  devices={"names": [], "phrases": [], "sources": [],
                           "unreadable": []},
                  hours=6, wait_s=2 * H, ci_hours=24, ci_slow_s=30 * 60)
    values.update(extra)
    return debit.build(**values)


class DebitDependanceTest(unittest.TestCase):
    def test_lot_relu_ciblant_la_branche_d_un_lot_ouvert_suit_son_hold(self):
        parent_milestones, parent_events = reviewed(693, 100, T - 30 * H)
        child_milestones, child_events = reviewed(3260, 200, T - 25 * H)
        report = run(
            items=[item(693, branch="agent/deepseek6-693-eco-exp-1"),
                   item(3260, branch="review/codex2-3260-expiry",
                        branch_target="agent/deepseek6-693-eco-exp-1",
                        assignee="codex2")],
            milestones=parent_milestones + child_milestones,
            events=parent_events + child_events + [
                event(693, 300, T - 3 * H,
                      "hold: pilote — #3260 entre avec #693",
                      actor="orchestrateur")])
        child = [row for row in report["qa"] if row["lot"] == 3260][0]
        self.assertEqual(child["depends_on"], 693)
        self.assertTrue(child["ready"])
        self.assertNotIn((3260, "qa_merge"),
                         [(row["lot"], row["kind"]) for row in report["slow_waits"]])
        self.assertNotIn(3260, [row["lot"] for row in report["slow_waits"]])
        # L'attente reste celle du parent, dont le hold porte la cause.
        self.assertIn((693, "hold"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])

    def test_cible_develop_reste_une_attente_de_paquet(self):
        child_milestones, child_events = reviewed(3260, 200, T - 25 * H)
        report = run(
            items=[item(3260, branch="review/codex2-3260-expiry",
                        branch_target="develop", assignee="codex2")],
            milestones=child_milestones, events=child_events)
        child = report["qa"][0]
        self.assertNotIn("depends_on", child)
        self.assertIn((3260, "qa_merge"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])

    def test_un_lot_parent_termine_ne_couvre_plus_l_attente(self):
        child_milestones, child_events = reviewed(3260, 200, T - 25 * H)
        report = run(
            items=[item(693, branch="agent/deepseek6-693-eco-exp-1"),
                   item(3260, branch="review/codex2-3260-expiry",
                        branch_target="agent/deepseek6-693-eco-exp-1",
                        assignee="codex2")],
            milestones=child_milestones + [
                frozen(693, 100, T - 30 * H),
                {"work_item_id": 693, "id": 101, "kind": "merged",
                 "at_ts": T - 20 * H, "sha": "b" * 40, "actor": "ameesh",
                 "verdict": None, "note": ""}],
            events=child_events)
        child = [row for row in report["qa"] if row["lot"] == 3260][0]
        self.assertNotIn("depends_on", child)
        self.assertIn((3260, "qa_merge"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])

    def test_les_refs_origin_et_refs_heads_sont_le_meme_nom(self):
        parent_milestones, parent_events = reviewed(693, 100, T - 10 * H)
        child_milestones, child_events = reviewed(3260, 200, T - 5 * H)
        report = run(
            items=[item(693, branch="refs/heads/agent/deepseek6-693-eco-exp-1"),
                   item(3260, branch="review/codex2-3260-expiry",
                        branch_target="origin/agent/deepseek6-693-eco-exp-1",
                        assignee="codex2")],
            milestones=parent_milestones + child_milestones,
            events=parent_events + child_events)
        child = [row for row in report["qa"] if row["lot"] == 3260][0]
        self.assertEqual(child["depends_on"], 693)

    def test_meme_branche_d_un_autre_projet_ne_cree_pas_de_dependance(self):
        parent_milestones, parent_events = reviewed(693, 100, T - 10 * H)
        child_milestones, child_events = reviewed(3260, 200, T - 5 * H)
        report = run(
            items=[item(693, app="ameesh", branch="agent/x-693"),
                   item(3260, app="nexlink", branch="review/codex2-3260",
                        branch_target="agent/x-693", assignee="codex2")],
            milestones=parent_milestones + child_milestones,
            events=parent_events + child_events)
        child = [row for row in report["qa"] if row["lot"] == 3260][0]
        self.assertNotIn("depends_on", child)
        self.assertIn((3260, "qa_merge"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])

    def test_un_avis_encore_du_reste_une_attente_de_relecture(self):
        parent_milestones, parent_events = reviewed(693, 100, T - 10 * H)
        report = run(
            items=[item(693, branch="agent/deepseek6-693-eco-exp-1"),
                   item(3260, branch="review/codex2-3260-expiry",
                        branch_target="agent/deepseek6-693-eco-exp-1",
                        assignee="codex2")],
            milestones=[frozen(3260, 200, T - 5 * H)],
            events=parent_events + [
                event(3260, 200, T - 5 * H,
                      mark(3260, 200, ["ameesh-cx1"]))])
        child = [row for row in report["qa"] if row["lot"] == 3260][0]
        self.assertEqual(child["depends_on"], 693)
        self.assertIn((3260, "qa"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])


class ReadDependanceTest(PgTestCase):
    """La lecture porte bien `branch`/`branch_target` jusqu'au calcul (#3437)."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, ci_runs CASCADE")

    def _item(self, ident, assignee, *, branch=None, branch_target=None,
              app="nexlink"):
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state, "
            "assignee, branch, branch_target) "
            "VALUES (%s, 'evolution', 'test', %s, %s, 'qa', %s, %s, %s)",
            (ident, "lot %d" % ident, app, assignee, branch, branch_target))

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
                    "review-designation:%d:%d: %s" % (
                        ident, freeze_id,
                        json.dumps({"reviewers": [
                            {"name": "orchestrateur"}]})), actor="ameesh")
        self._milestone(ident, freeze_id + 1, "verdict", at + H,
                        actor="orchestrateur", verdict="ok")

    def test_read_transmet_la_cible_et_le_parent(self):
        canons = types.SimpleNamespace(hosts=[], processes=[])
        self._item(7000, "deepseek6",
                   branch="agent/deepseek6-7000-eco-exp-1")
        self._item(7001, "codex2", branch="review/codex2-7001-expiry",
                   branch_target="agent/deepseek6-7000-eco-exp-1")
        self._reviewed(7000, 17000, T - 30 * H)
        self._reviewed(7001, 17010, T - 25 * H)
        self._event(7000, 47000, T - 3 * H,
                    "hold: pilote — 7001 entre avec 7000",
                    actor="orchestrateur")
        report = debit.read(self.db, cfg=self.cfg, now=T,
                            canons=[canons], installed="1.21.0", jobs=False)
        child = [row for row in report["qa"] if row["lot"] == 7001][0]
        self.assertEqual(child["depends_on"], 7000)
        self.assertNotIn(7001, [row["lot"] for row in report["slow_waits"]])
        self.assertIn((7000, "hold"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])


if __name__ == "__main__":
    unittest.main()
