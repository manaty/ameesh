# SPDX-License-Identifier: AGPL-3.0-only
"""#3183 — le plancher d'avis posé par un pilote relève le compte requis.

Constat (auditeur 23/10, #3169 nexlink) : `scripts/actor-root.mjs` (graine
racine, clés d'hôte, écritures prod) tombe sous `**` = 1 avis ; le pilote a
ajouté une seconde famille (`ameesh work reviewer`), mais la porte ne lisait
que le barème par chemin et passait sur un seul GO.

Témoins de la décision mesh-design : barème 1 + plancher 2 → un GO refusé, deux
familles passent ; plancher posé par un agent non pilote → refus ; regel →
plancher conservé ; plancher 2 sur un véhicule → exigé sur son delta propre.
"""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from ameesh import (assignments, identity, lot_class, merge_queue, packet_reviews,
                    registry, review_floor, storage, work)
from ameesh.mediated_executor import scope as scope_mod

from .support import PgTestCase
from . import test_2766_vehicle_reviews as vehicle_tests

HEAD = "a" * 40
REFREEZE = "b" * 40
FAMILIES = ("claude", "codex", "deepseek")
#: la classe que le canon nexlink rend pour #3169 : sensible, un avis
NEXLINK = {"class": "sensitive", "doubt": False, "project": "nexlink",
           "files": [{"path": "scripts/actor-root.mjs", "class": "sensitive"}]}


def _binding(name=""):
    if name:
        return types.SimpleNamespace(name=name, ok=True, source="runner", reason="")
    return types.SimpleNamespace(name="", ok=False, source="none", reason="")


class ScaleTest(unittest.TestCase):
    """`required_verdicts` lit le plancher sans jamais baisser le barème."""

    def test_the_floor_raises_the_scale(self):
        self.assertEqual(merge_queue.required_verdicts(NEXLINK), 1)
        self.assertEqual(merge_queue.required_verdicts(dict(NEXLINK, reviews_floor=2)), 2)

    def test_the_floor_never_lowers_the_scale(self):
        critical = dict(NEXLINK, files=[{"path": "x", "critical": True}])
        self.assertEqual(merge_queue.required_verdicts(dict(critical, reviews_floor=1)), 2)

    def test_a_lot_without_changed_file_stays_at_zero(self):
        light = {"class": "light", "files": [], "doubt": False}
        self.assertEqual(merge_queue.required_verdicts(dict(light, reviews_floor=2)), 0)

    def test_an_unreadable_floor_requires_two_reviews(self):
        klass = review_floor.apply(dict(NEXLINK), mock.Mock(), 1)
        self.assertIn("reviews_floor_unreadable", klass)
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_parse(self):
        self.assertEqual(review_floor.parse("reviews-floor: 2 — clés"), 2)
        self.assertEqual(review_floor.parse("reviews-floor: 0"), 0)
        self.assertIsNone(review_floor.parse("reviews-floor: 3"))
        self.assertIsNone(review_floor.parse("reviews-floor: deux"))
        self.assertIsNone(review_floor.parse("note reviews-floor: 0"))


class FloorGateTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM provenance_receipts")
        for name, harness in (("author", "deepseek"), ("r_claude", "claude"),
                              ("r_codex", "codex"), ("pilot", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp, status="idle")
        self.item = work.add(self.db, title="racine d'acteur", app="nexlink",
                             assignee="author", branch="lot/3169")
        self.lot = int(self.item["id"])
        self.ops = storage.of(self.db).work
        self.ops.add_milestone(self.lot, "frozen", sha=HEAD, actor="author",
                               verdict=None, note="")
        os.environ.pop("AGENT_MAIL_NAME", None)

    def as_session(self, name="", *, pilots=("pilot",)):
        """Une session liée à `name` (ou un terminal humain sans identité)."""
        stack = [mock.patch.object(identity, "resolve_binding",
                                   return_value=_binding(name)),
                 mock.patch.object(assignments, "is_orchestrator",
                                   side_effect=lambda _c, _d, who, **_k: who in pilots),
                 mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": ""})]
        for patch in stack:
            patch.start()
            self.addCleanup(patch.stop)

    def floor(self, value, actor=None, reason="clés et prod hors barème"):
        return review_floor.set_floor(self.cfg, self.db, self.lot, value,
                                      reason=reason, actor=actor)

    def verdict(self, actor, sha=HEAD):
        mark = self.ops.add_milestone(self.lot, "verdict", sha=sha, actor=actor,
                                      verdict="ok", note="")
        harness = {"r_claude": "claude", "r_codex": "codex"}[actor]
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("milestone:%d" % int(mark["id"]), sha, actor, "s-" + harness, "t",
             harness, self.lot))

    def gate(self, sha=HEAD):
        klass = review_floor.apply(dict(NEXLINK), self.db, self.lot, cfg=self.cfg)
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), sha, klass=klass,
            item=work.get(self.db, self.lot), families=FAMILIES)

    def test_one_go_passes_without_a_floor(self):
        self.verdict("r_codex")
        self.assertTrue(self.gate()["ok"])

    def test_a_floor_of_two_refuses_one_go_and_passes_two_families(self):
        self.as_session("pilot")
        out = self.floor(2)
        self.assertEqual((out["floor"], out["previous"], out["actor"]), (2, 0, "pilot"))
        self.verdict("r_codex")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["required"], 2)
        self.verdict("r_claude")
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 2)

    def test_the_floor_is_journaled_with_its_reason(self):
        self.as_session("pilot")
        self.floor(2)
        notes = [e["note"] for e in work.events(self.db, self.lot)]
        self.assertIn("reviews-floor: 2 — clés et prod hors barème", notes)
        found = review_floor.of_lot(self.db, self.lot)
        self.assertEqual((found["floor"], found["actor"]), (2, "pilot"))
        # même valeur : rien n'est réécrit
        self.assertFalse(self.floor(2)["changed"])

    def test_a_non_pilot_agent_is_refused(self):
        self.as_session("author")
        with self.assertRaises(work.WorkError) as caught:
            self.floor(2)
        self.assertIn("review floor refused", str(caught.exception))
        self.assertEqual(review_floor.of_lot(self.db, self.lot)["floor"], 0)

    def test_the_floor_survives_a_refreeze(self):
        self.as_session("pilot")
        self.floor(2)
        self.ops.add_milestone(self.lot, "frozen", sha=REFREEZE, actor="author",
                               verdict=None, note="")
        self.verdict("r_codex", sha=REFREEZE)
        out = self.gate(sha=REFREEZE)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["required"], 2)

    def test_the_class_reader_carries_the_floor(self):
        self.as_session("pilot")
        self.floor(2)
        with mock.patch.object(lot_class, "_of_lot", return_value=dict(NEXLINK)):
            klass = lot_class.of_lot(self.db, self.lot, cfg=self.cfg)
        self.assertEqual((klass["reviews_floor"], klass["reviews_floor_by"]), (2, "pilot"))
        self.assertEqual(klass["class"], "sensitive")  # jamais reclassé
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_a_pilot_withdraws_with_floor_zero(self):
        self.as_session("pilot")
        self.floor(2)
        self.floor(0, reason="outil déplacé sous infra/prod")
        self.assertEqual(review_floor.of_lot(self.db, self.lot)["floor"], 0)
        self.verdict("r_codex")
        self.assertTrue(self.gate()["ok"])

    def test_a_human_floor_is_lowered_only_by_a_human(self):
        self.as_session("")
        self.floor(2, actor="human:smichea")
        self.as_session("pilot")
        with self.assertRaises(work.WorkError) as caught:
            self.floor(0)
        self.assertIn("only a human lowers it", str(caught.exception))
        self.as_session("")
        self.floor(0, actor="human:smichea")
        self.assertEqual(review_floor.of_lot(self.db, self.lot)["floor"], 0)

    def test_a_human_recharge_at_the_same_value_keeps_the_floor(self):
        # #3196 : pilote 2 puis humain 2 ; sans ligne humaine, `human_floor`
        # restait à 0 et le pilote pouvait ensuite poser 0.
        self.as_session("pilot")
        self.floor(2)
        self.as_session("")
        out = self.floor(2, actor="human:smichea")
        self.assertTrue(out["changed"])
        found = review_floor.of_lot(self.db, self.lot, cfg=self.cfg)
        self.assertEqual((found["floor"], found["actor"], found["human_floor"]),
                         (2, "human:smichea", 2))
        # une seconde recharge identique n'écrit rien
        self.assertFalse(self.floor(2, actor="human:smichea")["changed"])
        self.as_session("pilot")
        with self.assertRaises(work.WorkError) as caught:
            self.floor(0)
        self.assertIn("only a human lowers it", str(caught.exception))
        self.assertEqual(review_floor.of_lot(self.db, self.lot)["floor"], 2)

    def test_a_copied_line_does_not_lower_the_floor(self):
        # d'autres écritures du journal portent un texte libre : la ligne
        # copiée par l'assigné ne compte pas, faute d'autorité de pilote.
        self.as_session("pilot")
        self.floor(2)
        self.ops.note(self.lot, "build", "reviews-floor: 0 — plus besoin", "author")
        self.assertEqual(review_floor.of_lot(self.db, self.lot, cfg=self.cfg)["floor"], 2)
        self.verdict("r_codex")
        self.assertFalse(self.gate()["ok"])

    def test_a_move_note_claiming_a_pilot_does_not_lower_the_floor(self):
        # B1 cx2 : `work.move(..., note=…, actor="pilot")` écrit sans contrôle
        # d'autorité ; l'acteur revendiqué ne suffit pas, la ligne n'a pas de
        # preuve structurée.
        self.as_session("pilot")
        self.floor(2)
        self.as_session("author")
        work.move(self.db, self.lot, "build", note="reviews-floor: 0", actor="pilot")
        self.ops.note(self.lot, "build", "reviews-floor: 0", "human:smichea")
        found = review_floor.of_lot(self.db, self.lot, cfg=self.cfg)
        self.assertEqual((found["floor"], found["actor"]), (2, "pilot"))
        self.verdict("r_codex")
        self.assertFalse(self.gate()["ok"])

    def test_an_identical_copy_does_not_take_over_a_human_floor(self):
        # B2 cx2 : la copie identique de l'assigné ne remplace pas le
        # détenteur humain ; le pilote ne peut toujours pas le baisser.
        self.as_session("")
        self.floor(2, actor="human:smichea")
        self.ops.note(self.lot, "build", "reviews-floor: 2", "author")
        found = review_floor.of_lot(self.db, self.lot, cfg=self.cfg)
        self.assertEqual((found["floor"], found["actor"]), (2, "human:smichea"))
        self.as_session("pilot")
        with self.assertRaises(work.WorkError) as caught:
            self.floor(0)
        self.assertIn("only a human lowers it", str(caught.exception))
        self.assertEqual(review_floor.of_lot(self.db, self.lot)["floor"], 2)

    def test_a_pilot_raise_keeps_the_human_minimum(self):
        self.as_session("")
        self.floor(1, actor="human:smichea")
        self.as_session("pilot")
        self.floor(2)
        with self.assertRaises(work.WorkError):
            self.floor(0)
        self.floor(1)
        found = review_floor.of_lot(self.db, self.lot)
        self.assertEqual((found["floor"], found["human_floor"]), (1, 1))

    def test_work_note_refuses_the_reserved_prefix(self):
        with self.assertRaises(work.WorkError) as caught:
            work.note(self.db, self.lot, "reviews-floor: 0", actor="author")
        self.assertIn("reserved to the review floor", str(caught.exception))

    def test_a_merged_lot_takes_no_floor(self):
        self.as_session("pilot")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (self.lot,))
        with self.assertRaises(work.WorkError):
            self.floor(2)


class MediatedNoteTest(unittest.TestCase):
    def test_the_assignee_cannot_write_the_floor_line(self):
        rules = scope_mod.HostScopeRules.__new__(scope_mod.HostScopeRules)
        principal = types.SimpleNamespace(agent="author")
        with mock.patch.object(scope_mod.HostScopeRules, "_require_assigned"):
            with self.assertRaises(scope_mod.ScopeError):
                rules._apply_work_note(principal, {"item_id": 1,
                                                   "text": "reviews-floor: 0"}, None)
            rules._apply_work_note(principal, {"item_id": 1, "text": "hold: CI"}, None)


class VehicleFloorTest(vehicle_tests._Base):
    """Un plancher posé sur un véhicule s'exige sur son delta propre."""

    # le banc de #2766, sans rejouer ses témoins
    _Landings = vehicle_tests.NeedTest._Landings
    need = vehicle_tests.NeedTest.need
    setUpResolution = vehicle_tests.NeedTest.setUpResolution

    def need_with_floor(self, floor):
        rows = [{"work_item_id": 101, "pinned_sha": self.m1},
                {"work_item_id": 102, "pinned_sha": self.m2}]
        marks = {101: [{"id": 1, "kind": "frozen", "sha": self.m1, "at_ts": 1.0}],
                 102: [{"id": 2, "kind": "frozen", "sha": self.m2, "at_ts": 2.0}]}
        found = {"floor": floor, "actor": "pilot", "at_ts": 1.0, "note": ""}
        with mock.patch.object(review_floor, "of_lot", return_value=found) as reader:
            out = self.need(rows, marks, self.head)
        return out, reader

    def test_the_floor_applies_to_the_proper_delta(self):
        self.setUpResolution()
        out, reader = self.need_with_floor(2)
        self.assertIsNone(out["ok"])
        self.assertEqual(out["required"], 2)
        self.assertEqual(reader.call_args.args[1], 999)

    def test_no_proper_delta_needs_no_vehicle_review(self):
        self.compose()
        out, _reader = self.need_with_floor(2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["required"], 0)


if __name__ == "__main__":
    unittest.main()
