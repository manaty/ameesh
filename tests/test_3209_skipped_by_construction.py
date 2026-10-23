# SPDX-License-Identifier: AGPL-3.0-only
"""#3209 — un check sauté par construction n'est pas un non-concluant.

#3085 consigne tout check non concluant et ouvre un lot de correction (règle
owner du 23/10, canon home §3 : « il est consigné… ouvre si besoin un lot de
correction »). Deux checks sont sautés **par décision**, pas par panne :

- le job `suite complète du paquet` du portier `paquet`, que le portier saute
  quand une PR ouverte porte déjà la tête (`couverture` vert) — la suite
  tourne dans le run `pull_request` de cette PR ;
- les runs déclenchés par `workflow_run` (`relance hosted`, `flux
  orphelins`), dont la garde `if:` dit que le run observé n'a pas échoué.

Consignés, ils ouvraient un lot P2 à chaque fusion de paquet et à chaque
release, sous une clé par tête : jamais dédoublonnés. Le lot de correction
est désormais unique par check tant qu'il est ouvert ; une nouvelle
occurrence est notée sur lui.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import fire, packet_automerge, radar, release_cutoff, work

from . import test_2271_fire_required_checks as f2271
from . import test_l160g_nexlink as l160g

SUITE = "suite complète du paquet"
COVERAGE = "couverture (PR ouverte sur cette tête)"


def _packet(name, conclusion, *, suite_id=None, status="COMPLETED"):
    entry = {"__typename": "CheckRun", "name": name, "workflowName": "paquet",
             "status": status, "conclusion": conclusion}
    if suite_id is not None:
        entry["checkSuiteId"] = suite_id
    return entry


class PacketProofTest(unittest.TestCase):
    """`fire.ready_pr` : la suite sautée parce qu'une PR porte la tête."""

    def ready(self, rollup):
        report: dict = {}
        ok, why = fire.ready_pr(f2271._status(rollup), target="main", project=None,
                                frozen=f2271.SHA, required=(("tests", None),),
                                slug="manaty/ameesh", gh=None, report=report)
        return ok, why, report

    def test_the_suite_skipped_because_a_pr_covers_the_head_is_not_consigned(self):
        ok, why, report = self.ready([f2271._check("tests"),
                                      _packet(COVERAGE, "SUCCESS", suite_id=7),
                                      _packet(SUITE, "SKIPPED", suite_id=7)])
        self.assertTrue(ok, why)
        self.assertEqual(report.get("consigned") or [], [])

    def test_a_skipped_suite_without_a_green_coverage_stays_consigned(self):
        # couverture absente : rien ne prouve qu'une PR porte la tête
        ok, why, report = self.ready([f2271._check("tests"),
                                      _packet(SUITE, "SKIPPED")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["%s (SKIPPED)" % SUITE])

    def test_the_coverage_of_another_run_proves_nothing(self):
        ok, why, report = self.ready([f2271._check("tests"),
                                      _packet(COVERAGE, "SUCCESS", suite_id=7),
                                      _packet(SUITE, "SKIPPED", suite_id=8)])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["%s (SKIPPED)" % SUITE])

    def test_a_cancelled_suite_stays_consigned_even_when_covered(self):
        ok, why, report = self.ready([f2271._check("tests"),
                                      _packet(COVERAGE, "SUCCESS", suite_id=7),
                                      _packet(SUITE, "CANCELLED", suite_id=7)])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["%s (CANCELLED)" % SUITE])

    def test_a_failed_packet_suite_still_blocks(self):
        ok, why, _ = self.ready([f2271._check("tests"),
                                 _packet(COVERAGE, "SUCCESS", suite_id=7),
                                 _packet(SUITE + " / tests", "FAILURE", suite_id=7)])
        self.assertFalse(ok)
        self.assertIn("paquet", why)


class CommitCiTest(unittest.TestCase):
    """`radar.commit_ci` (porte de release) : la garde `workflow_run` sautée."""

    project = radar.Project("ameesh", "/r", github="manaty/ameesh", ci_required=True)
    sha = "a" * 40

    def read(self, *runs):
        gh = l160g._Gh(list(runs))
        out = radar.commit_ci(self.project, self.sha, gh=gh)
        return out, gh

    def test_a_skipped_workflow_run_guard_is_not_inconclusive(self):
        out, gh = self.read(l160g._run("tests"),
                            dict(l160g._run("relance hosted", conclusion="skipped"),
                                 event="workflow_run"))
        self.assertIn("event", gh.calls[0][gh.calls[0].index("--json") + 1].split(","))
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["inconclusive"], [])
        self.assertEqual(out["negative"], [])

    def test_a_skipped_push_run_stays_inconclusive(self):
        out, _ = self.read(l160g._run("tests"),
                           dict(l160g._run("lint", conclusion="skipped"), event="push"))
        self.assertEqual(out["inconclusive"], ["lint"])

    def test_a_cancelled_or_failed_workflow_run_guard_is_still_read(self):
        out, _ = self.read(l160g._run("tests"),
                           dict(l160g._run("relance hosted", conclusion="cancelled"),
                                event="workflow_run"))
        self.assertEqual(out["inconclusive"], ["relance hosted"])
        out, _ = self.read(l160g._run("tests"),
                           dict(l160g._run("relance hosted", conclusion="failure"),
                                event="workflow_run"))
        self.assertEqual(out["negative"], ["relance hosted"])


class _Lots:
    """Le magasin de lots du témoin : `work.add`, `work.note` et la lecture
    des lots ouverts par état (`storage.work.items`)."""

    def __init__(self, test):
        self.rows: list[dict] = []
        self.notes: list[tuple[int, str]] = []
        test.addCleanup(mock.patch.stopall)
        mock.patch.object(work, "add", self.add).start()
        mock.patch.object(work, "note", self.note).start()
        store = types.SimpleNamespace(work=types.SimpleNamespace(items=self.items))
        mock.patch.object(work.storage, "of", lambda db: store).start()

    def add(self, db, **kwargs):
        row = dict(kwargs, id=9000 + len(self.rows), state="intake")
        self.rows.append(row)
        return row

    def note(self, db, item_id, text, **kwargs):
        self.notes.append((int(item_id), text))
        return True

    def items(self, *, state, assignee, limit):
        return [row for row in self.rows if row["state"] == state][:limit]


class CorrectionLotTest(unittest.TestCase):
    """Un lot de correction par check, tant qu'il est ouvert."""

    def setUp(self):
        self.lots = _Lots(self)

    def packet(self, sha, consigned):
        packet_automerge._open_correction(
            object(), {"app": "ameesh"}, slug="manaty/ameesh", number=2427, sha=sha,
            base="main", consigned=consigned, actor="ameesh")

    def test_the_same_check_on_the_next_packet_is_noted_not_reopened(self):
        self.packet("b" * 40, ["tests (CANCELLED)"])
        self.packet("c" * 40, ["tests (TIMED_OUT)"])
        self.assertEqual(len(self.lots.rows), 1)
        self.assertIn("tests (CANCELLED)", self.lots.rows[0]["title"])
        self.assertEqual(len(self.lots.notes), 1)
        self.assertEqual(self.lots.notes[0][0], self.lots.rows[0]["id"])
        self.assertIn("c" * 12, self.lots.notes[0][1])
        self.assertIn("tests (TIMED_OUT)", self.lots.notes[0][1])

    def test_each_check_has_its_own_lot(self):
        self.packet("b" * 40, ["tests (CANCELLED)", "ci/docs* (ERROR)"])
        self.assertEqual(len(self.lots.rows), 2)
        self.assertEqual(len({row["source"] for row in self.lots.rows}), 2)

    def test_a_closed_lot_lets_the_next_occurrence_open_a_new_one(self):
        self.packet("b" * 40, ["tests (CANCELLED)"])
        self.lots.rows[0]["state"] = "closed"
        self.packet("c" * 40, ["tests (CANCELLED)"])
        self.assertEqual(len(self.lots.rows), 2)
        self.assertEqual(self.lots.notes, [])

    def test_the_release_shares_the_check_key(self):
        self.packet("b" * 40, ["tests (CANCELLED)"])
        project = types.SimpleNamespace(name="ameesh")
        plan = types.SimpleNamespace(head="d" * 40, version="1.20.0",
                                     ci={"inconclusive": ["tests", "macos"]})
        release_cutoff._open_correction(object(), project, plan)
        self.assertEqual(len(self.lots.rows), 2)
        self.assertIn("macos", self.lots.rows[1]["title"])
        self.assertEqual(len(self.lots.notes), 1)
        self.assertIn("1.20.0", self.lots.notes[0][1])

    def test_an_unreadable_store_still_opens_the_lot(self):
        def broken(**kwargs):
            raise RuntimeError("store down")
        work.storage.of(None).work.items = broken
        self.packet("b" * 40, ["tests (CANCELLED)"])
        self.assertEqual(len(self.lots.rows), 1)


if __name__ == "__main__":
    unittest.main()
