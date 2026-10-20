# SPDX-License-Identifier: AGPL-3.0-only
"""#2813 — l'auto-fusion d'un paquet armée à sa publication (canon §12).

Décision owner du 20/10 : le geste qui publie le paquet (`ameesh ci run
<porteur> --full`) arme `gh pr merge --auto --merge --match-head-commit
<tête>` ; tout nouveau push désarme, la publication suivante réarme sur la
nouvelle tête ; `work merge-pr` ne désarme plus (`test_1656_auto_merge`).

Témoins sans réseau ni base : un faux `gh` (protection, PR, armement,
désarmement) et un faux journal de lot (`work.events`/`work.note`).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import itertools
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ameesh import ci, packet_automerge, plan_github, work  # noqa: E402

from .test_l161g_ci_gel import (BASE, DEPOT, GEL, LABEL_FULL, PR, GelTestCase,  # noqa: E402
                                Gh as LabelGh, Github, pull)

SLUG = "manaty/ameesh"
LOT = 2834
NUM = 2140
HEAD = "1" * 40
NEW = "2" * 40
PROTECTED = {"required_status_checks": {"strict": False, "contexts": ["tests"],
                                        "checks": [{"context": "tests", "app_id": 15368}]}}


class FakeGh:
    """Le `gh` de `packet_automerge` : protection de `main`, une PR, l'auto-fusion."""

    def __init__(self, *, protection=PROTECTED, protection_error="", head=HEAD,
                 state="OPEN", base="main", auto=False, merge_error="", disarm_error="",
                 view_error="", sticky=False):
        self.calls: list[list[str]] = []
        self.protection = protection
        self.protection_error = protection_error
        self.head, self.state, self.base, self.auto = head, state, base, auto
        self.merge_error, self.disarm_error = merge_error, disarm_error
        self.view_error = view_error
        #: plusieurs PR partagent ce faux : un désarmement ne vaut pas pour l'autre
        self.sticky = sticky

    def __call__(self, args, timeout=60.0):
        args = list(args)
        self.calls.append(args)

        def done(code=0, out="", err=""):
            return subprocess.CompletedProcess(["gh", *args], code, out, err)

        if args[:1] == ["api"] and args[-1].endswith("/protection"):
            if self.protection_error:
                return done(1, "", self.protection_error)
            if self.protection is None:
                return done(1, "", "gh: Branch not protected (HTTP 404)")
            return done(0, json.dumps(self.protection))
        if args[:1] == ["api"] and "/rules/branches/" in args[-1]:
            if self.protection_error:
                return done(1, "", self.protection_error)
            return done(0, "[[]]")
        if args[:2] == ["pr", "view"]:
            if self.view_error:
                return done(1, "", self.view_error)
            return done(0, json.dumps({
                "state": self.state, "headRefOid": self.head, "baseRefName": self.base,
                "autoMergeRequest": {"enabledAt": "2026-10-20T03:00:00Z"} if self.auto
                else None}))
        if args[:2] == ["pr", "merge"] and "--disable-auto" in args:
            if self.disarm_error:
                return done(1, "", self.disarm_error)
            self.auto = self.sticky
            return done()
        if args[:2] == ["pr", "merge"] and "--auto" in args:
            if self.merge_error:
                return done(1, "", self.merge_error)
            self.auto = True
            return done()
        return done(1, "", "unexpected gh call %s" % args)

    def arms(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"] and "--auto" in c]

    def disarms(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"] and "--disable-auto" in c]


class Journal(unittest.TestCase):
    """Le faux journal du lot : `work.events` et `work.note`."""

    def setUp(self) -> None:
        self.events: dict[int, list[dict]] = {}
        self.seq = itertools.count(1)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(work, "events", self._events))
        stack.enter_context(mock.patch.object(work, "note", self._note))
        #: le bail `auto-merge:<lot>` : libre, sauf quand un témoin le prend
        self.held: set[int] = set()
        stack.enter_context(mock.patch.object(
            packet_automerge, "_lock",
            lambda db, lot: None if int(lot) in self.held else ("bail", int(lot))))
        stack.enter_context(mock.patch.object(packet_automerge, "_unlock", lambda h: None))

    def _events(self, db, item_id, limit=100):
        return list(reversed(self.events.get(int(item_id), [])))

    def _note(self, db, item_id, text, **kw):
        self.events.setdefault(int(item_id), []).append(
            {"id": next(self.seq), "note": text, "created_ts": time.time()})
        return True

    def notes(self, lot: int = LOT) -> list[str]:
        return [e["note"] for e in self.events.get(lot, [])]

    def armed(self, sha: str = HEAD, lot: int = LOT, pr: int = NUM) -> None:
        self._note(None, lot, "auto-merge armed repo=%s pr=%d sha=%s method=merge "
                              "required=tests" % (SLUG, pr, sha))

    def arm(self, gh, sha: str = HEAD) -> dict:
        return packet_automerge.arm(None, LOT, slug=SLUG, number=NUM, sha=sha,
                                    target="main", gh=gh)


class ArmTest(Journal):
    def test_a_protected_target_arms_on_the_frozen_head(self):
        gh = FakeGh()
        out = self.arm(gh)
        self.assertEqual(out["state"], "armed", out)
        self.assertEqual(gh.arms(), [["pr", "merge", str(NUM), "--repo", SLUG, "--auto",
                                      "--merge", "--match-head-commit", HEAD]])
        self.assertEqual(gh.disarms(), [])
        self.assertTrue(self.notes()[-1].startswith(
            "auto-merge armed repo=%s pr=%d sha=%s method=merge required=tests"
            % (SLUG, NUM, HEAD)), self.notes())
        self.assertEqual(packet_automerge.active(packet_automerge.marks(None, LOT))["sha"],
                         HEAD)

    def test_the_same_head_already_armed_by_ameesh_is_left_alone(self):
        self.armed()
        gh = FakeGh(auto=True)
        out = self.arm(gh)
        self.assertEqual(out["state"], "already-armed", out)
        self.assertEqual(gh.arms(), [])
        self.assertEqual(gh.disarms(), [])

    def test_an_auto_merge_of_another_head_is_disarmed_then_rearmed(self):
        self.armed(sha=HEAD)
        gh = FakeGh(head=NEW, auto=True)
        out = self.arm(gh, sha=NEW)
        self.assertEqual(out["state"], "rearmed", out)
        merges = [c for c in gh.calls if c[:2] == ["pr", "merge"]]
        self.assertIn("--disable-auto", merges[0])
        self.assertEqual(merges[1][-2:], ["--match-head-commit", NEW])
        self.assertEqual(packet_automerge.active(packet_automerge.marks(None, LOT))["sha"],
                         NEW)

    def test_an_auto_merge_armed_by_hand_is_rearmed_by_ameesh(self):
        gh = FakeGh(auto=True)
        out = self.arm(gh)
        self.assertEqual(out["state"], "rearmed", out)
        self.assertEqual(len(gh.disarms()), 1)
        self.assertEqual(len(gh.arms()), 1)

    def test_an_unprotected_target_arms_nothing_and_names_the_fire(self):
        gh = FakeGh(protection=None)
        out = self.arm(gh)
        self.assertEqual(out["state"], "unprotected", out)
        self.assertIn("ameesh work fire %d --sha %s" % (LOT, HEAD[:12]), out["why"])
        self.assertEqual([c for c in gh.calls if c[:1] == ["pr"]], [])
        self.assertTrue(self.notes()[-1].startswith("auto-merge not-armed"), self.notes())

    def test_a_protection_without_required_check_arms_nothing(self):
        gh = FakeGh(protection={"required_status_checks": None})
        self.assertEqual(self.arm(gh)["state"], "unprotected")
        self.assertEqual(gh.arms(), [])

    def test_a_forbidden_protection_read_is_the_nexlink_case(self):
        gh = FakeGh(protection_error="Upgrade to GitHub Pro or make this repository public "
                                     "to enable this feature. (HTTP 403)")
        out = self.arm(gh)
        self.assertEqual(out["state"], "unprotected", out)
        self.assertIn("ameesh work fire", out["why"])
        self.assertEqual(gh.arms(), [])

    def test_an_unreadable_protection_arms_nothing(self):
        gh = FakeGh(protection_error="gh: Bad gateway (HTTP 502)")
        self.assertEqual(self.arm(gh)["state"], "error")
        self.assertEqual(gh.arms(), [])

    def test_a_moved_head_arms_nothing(self):
        gh = FakeGh(head=NEW)
        out = self.arm(gh, sha=HEAD)
        self.assertEqual(out["state"], "moved", out)
        self.assertEqual(gh.arms(), [])

    def test_another_base_arms_nothing(self):
        gh = FakeGh(base="develop")
        self.assertEqual(self.arm(gh)["state"], "error")
        self.assertEqual(gh.arms(), [])

    def test_a_failed_disarm_never_arms(self):
        self.armed(sha=HEAD)
        gh = FakeGh(head=NEW, auto=True, disarm_error="HTTP 502")
        out = self.arm(gh, sha=NEW)
        self.assertEqual(out["state"], "error", out)
        self.assertEqual(gh.arms(), [])
        # le premier armement reste à surveiller : `not-armed` ne l'efface pas
        self.assertEqual(packet_automerge.active(packet_automerge.marks(None, LOT))["sha"],
                         HEAD)

    def test_a_refused_arming_is_journaled(self):
        gh = FakeGh(merge_error="GraphQL: Auto merge is not allowed for this repository")
        out = self.arm(gh)
        self.assertEqual(out["state"], "error", out)
        self.assertIn("Auto merge is not allowed", self.notes()[-1])
        self.assertIsNone(packet_automerge.active(packet_automerge.marks(None, LOT)))

    def test_a_short_sha_is_refused(self):
        gh = FakeGh()
        self.assertEqual(self.arm(gh, sha=HEAD[:12])["state"], "error")
        self.assertEqual(gh.calls, [])


class LeaseTest(Journal):
    """Le bail `auto-merge:<lot>` sérialise la publication et le passage."""

    def test_a_held_lease_arms_nothing(self):
        self.held.add(LOT)
        gh = FakeGh()
        out = self.arm(gh)
        self.assertEqual(out["state"], "error", out)
        self.assertIn("ameesh ci run %d --full" % LOT, out["why"])
        self.assertEqual(gh.calls, [])

    def test_a_held_lease_defers_the_disarm_to_the_next_pass(self):
        self.armed()
        self.held.add(LOT)
        gh = FakeGh(head=NEW, auto=True)
        self.assertEqual(packet_automerge.check(None, LOT, gh=gh)["result"], "busy")
        self.assertEqual(gh.calls, [])

    def test_the_marks_are_read_again_under_the_lease(self):
        """Une publication réarme sur la nouvelle tête entre la première lecture
        du passage et son bail : le passage relit et laisse l'armement."""
        self.armed(sha=HEAD)
        gh = FakeGh(head=NEW, auto=True)
        real = packet_automerge._lock

        def lock(db, lot):
            self.armed(sha=NEW)          # la publication s'est faite juste avant
            return real(db, lot)
        with mock.patch.object(packet_automerge, "_lock", lock):
            out = packet_automerge.check(None, LOT, gh=gh)
        self.assertEqual(out["result"], "armed", out)
        self.assertEqual(gh.disarms(), [])


class MarksTest(unittest.TestCase):
    def row(self, ident, state, sha=HEAD, pr=NUM):
        return {"id": ident, "state": state, "repo": SLUG, "pr": pr, "sha": sha}

    def test_a_disarm_clears_the_arming(self):
        self.assertIsNone(packet_automerge.active([self.row(1, "armed"),
                                                   self.row(2, "disarmed")]))

    def test_a_refusal_does_not_clear_the_arming(self):
        self.assertEqual(packet_automerge.active([self.row(1, "armed"),
                                                  self.row(2, "not-armed", sha=NEW)])["id"], 1)

    def test_a_disarm_of_another_pr_does_not_clear_it(self):
        self.assertEqual(packet_automerge.active([self.row(1, "armed"),
                                                  self.row(2, "disarmed", pr=1)])["id"], 1)

    def test_only_full_heads_are_read(self):
        self.assertIsNone(packet_automerge.MARK_RE.match(
            "auto-merge armed repo=%s pr=%d sha=%s" % (SLUG, NUM, HEAD[:12])))


class WatchTest(Journal):
    def test_nothing_armed_reads_nothing(self):
        gh = FakeGh()
        self.assertEqual(packet_automerge.check(None, LOT, gh=gh)["result"], "idle")
        self.assertEqual(gh.calls, [])

    def test_the_armed_head_stays_armed(self):
        self.armed()
        gh = FakeGh(auto=True)
        self.assertEqual(packet_automerge.check(None, LOT, gh=gh)["result"], "armed")
        self.assertEqual(gh.disarms(), [])

    def test_a_new_push_disarms(self):
        self.armed()
        gh = FakeGh(head=NEW, auto=True)
        out = packet_automerge.check(None, LOT, gh=gh)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertEqual(gh.disarms(), [["pr", "merge", str(NUM), "--repo", SLUG,
                                         "--disable-auto"]])
        self.assertTrue(self.notes()[-1].startswith(
            "auto-merge disarmed repo=%s pr=%d sha=%s head=%s" % (SLUG, NUM, HEAD, NEW)))
        self.assertIn("ameesh ci run %d --full" % LOT, self.notes()[-1])
        self.assertIsNone(packet_automerge.active(packet_automerge.marks(None, LOT)))
        # le passage suivant n'a plus rien à surveiller
        self.assertEqual(packet_automerge.check(None, LOT, gh=gh)["result"], "idle")

    def test_a_dry_run_writes_nothing(self):
        self.armed()
        gh = FakeGh(head=NEW, auto=True)
        self.assertEqual(packet_automerge.check(None, LOT, gh=gh, dry_run=True)["result"],
                         "would-disarm")
        self.assertEqual(gh.disarms(), [])
        self.assertTrue(gh.auto)

    def test_a_merged_pr_is_left_alone(self):
        self.armed()
        gh = FakeGh(head=NEW, state="MERGED", auto=True)
        self.assertEqual(packet_automerge.check(None, LOT, gh=gh)["result"], "closed")
        self.assertEqual(gh.disarms(), [])

    def test_a_pr_without_auto_merge_needs_nothing(self):
        self.armed()
        gh = FakeGh(head=NEW, auto=False)
        self.assertEqual(packet_automerge.check(None, LOT, gh=gh)["result"], "unarmed")
        self.assertEqual(gh.disarms(), [])

    def test_a_failed_disarm_is_an_error_kept_armed(self):
        self.armed()
        gh = FakeGh(head=NEW, auto=True, disarm_error="HTTP 502")
        out = packet_automerge.check(None, LOT, gh=gh)
        self.assertEqual(out["result"], "error", out)
        self.assertIsNotNone(packet_automerge.active(packet_automerge.marks(None, LOT)))

    def test_watch_reads_the_open_carriers_of_the_project_only(self):
        rows = {"build": [{"id": LOT, "branch": "integration/paquet-h-20261020-0500",
                           "app": "ameesh"},
                          {"id": 7, "branch": "lot/7-x", "app": "ameesh"},
                          {"id": 8, "branch": "integration/paquet-84-nexlink",
                           "app": "nexlink"}],
                "qa": [{"id": 9, "branch": "integration/paquet-h-20261020-0400",
                        "app": "ameesh"}]}
        store = types.SimpleNamespace(items=lambda state, assignee, limit: rows.get(state, []))
        self.armed()
        self.armed(lot=9, pr=NUM - 1)
        gh = FakeGh(head=NEW, auto=True, sticky=True)
        with mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)):
            out = packet_automerge.watch(None, "ameesh", gh=gh)
        self.assertEqual(sorted(r["lot"] for r in out), [9, LOT])
        self.assertEqual({r["result"] for r in out}, {"disarmed"})
        self.assertEqual(sorted(c[2] for c in gh.calls if c[:2] == ["pr", "view"]),
                         sorted([str(NUM), str(NUM - 1)]))

    def test_one_carrier_error_does_not_stop_the_other(self):
        rows = {"build": [{"id": LOT, "branch": "integration/paquet-h-a", "app": "ameesh"},
                          {"id": 9, "branch": "integration/paquet-h-b", "app": "ameesh"}]}
        store = types.SimpleNamespace(items=lambda state, assignee, limit: rows.get(state, []))
        self.armed(lot=9)
        with mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)), \
                mock.patch.object(packet_automerge, "marks",
                                  side_effect=lambda db, lot: (_ for _ in ()).throw(
                                      RuntimeError("boom")) if lot == LOT else
                                  [{"id": 1, "state": "armed", "repo": SLUG, "pr": NUM,
                                    "sha": HEAD}]):
            out = packet_automerge.watch(None, "ameesh", gh=FakeGh(head=NEW, auto=True))
        by_lot = {r["lot"]: r["result"] for r in out}
        self.assertEqual(by_lot, {LOT: "error", 9: "disarmed"})


class TickTest(unittest.TestCase):
    """Le passage `ameesh release tick` (`packet_watch.tick`) porte le désarmement."""

    def tick(self, rows=None, error=None):
        from ameesh import packet_watch
        from .test_2601_packet_watch import Hourly, at, cfg_with
        watch = mock.Mock(return_value=rows or [], side_effect=error)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(packet_watch, "check",
                                  return_value={"project": "ameesh", "result": "grace"}), \
                mock.patch.object(packet_automerge, "watch", watch):
            out = packet_watch.tick(cfg_with(tmp), None, now=at(4, 5),
                                    rules_of=lambda name: Hourly(), gh="gh")
        return out, watch

    def test_a_quiet_carrier_says_nothing(self):
        out, watch = self.tick([{"lot": LOT, "result": "armed"},
                                {"lot": 9, "result": "idle"}])
        self.assertEqual([r["result"] for r in out], ["grace"])
        watch.assert_called_once_with(None, "ameesh", gh="gh", dry_run=False)

    def test_a_disarm_and_an_error_are_reported(self):
        out, _watch = self.tick([{"lot": LOT, "result": "disarmed", "pr": NUM},
                                 {"lot": 9, "result": "error", "error": "HTTP 502"}])
        self.assertEqual([(r["result"], r.get("lot")) for r in out],
                         [("grace", None), ("auto-merge-disarmed", LOT), ("error", 9)])
        self.assertIn("HTTP 502", out[2]["error"])

    def test_a_crash_of_the_watch_keeps_the_slot_result(self):
        out, _watch = self.tick(error=RuntimeError("boom"))
        self.assertEqual([r["result"] for r in out], ["grace", "error"])


class ArmingGh(LabelGh):
    """Le `gh` des étiquettes de `test_l161g`, plus la protection et la PR."""

    def __init__(self, *, protected=True, auto=False, **kw):
        super().__init__(**kw)
        self.protected, self.auto = protected, auto

    def run(self, argv, stdin=None):
        if argv[:1] == ["api"] and argv[-1].endswith("/protection"):
            self.calls.append(list(argv))
            if not self.protected:
                return 1, "", "gh: Branch not protected (HTTP 404)"
            return 0, json.dumps(PROTECTED), ""
        if argv[:1] == ["api"] and "/rules/branches/" in argv[-1]:
            self.calls.append(list(argv))
            return 0, "[[]]", ""
        if argv[:2] == ["pr", "view"]:
            self.calls.append(list(argv))
            return 0, json.dumps({"state": "OPEN", "headRefOid": GEL, "baseRefName": "main",
                                  "autoMergeRequest": {"enabledAt": "x"} if self.auto
                                  else None}), ""
        if argv[:2] == ["pr", "merge"]:
            self.calls.append(list(argv))
            self.auto = "--auto" in argv
            return 0, "", ""
        return super().run(argv, stdin)

    def arms(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"] and "--auto" in c]


class CiRunTest(GelTestCase):
    """`ameesh ci run <porteur> --full` : la publication arme l'auto-fusion."""

    def setUp(self) -> None:
        super().setUp()
        self.notes: list[str] = []
        self.seq = itertools.count(1)
        self.work.enter_context(mock.patch.object(work, "note", self._note))
        self.work.enter_context(mock.patch.object(packet_automerge, "_lock",
                                                  lambda db, lot: ("bail", lot)))
        self.work.enter_context(mock.patch.object(packet_automerge, "_unlock",
                                                  lambda h: None))
        self.work.enter_context(mock.patch(
            "ameesh.prevalidation_calls.gate_packet",
            return_value={"state": "unavailable", "cause": "test"}))
        self.gele()

    def _note(self, db, item_id, text, **kw):
        self.notes.append(text)
        self.events.append({"id": next(self.seq), "note": text, "created_ts": time.time()})
        return True

    def run_cmd(self, gh, *, api=None, full=True, dry_run=False):
        api = api or Github(pulls=[pull(head_ref="integration/paquet-h-20261019-0600")],
                            manifest="integration-paquet-h-20261019-0600.md")
        args = argparse.Namespace(id=self.item["id"], sha="", avis=0, exception="",
                                  reason="porteur", full=full, force=False, repo=DEPOT,
                                  dry_run=dry_run, json=True, prevalidation_repo="",
                                  prevalidation_map="")
        out = io.StringIO()
        with mock.patch("ameesh.db.open_db", lambda cfg: self.db), \
                mock.patch.object(plan_github, "Gh", lambda *a, **k: gh), \
                mock.patch.object(plan_github, "_json",
                                  lambda g, argv, *a, **k: api(argv[-1])), \
                contextlib.redirect_stdout(out):
            code = ci.cmd_ci_run(None, args)
        text = out.getvalue()
        return code, json.loads(text[text.index("{"):]) if "{" in text else {}

    def test_the_full_request_of_a_carrier_arms_the_auto_merge(self):
        gh = ArmingGh()
        code, out = self.run_cmd(gh)
        self.assertEqual(code, 0, out)
        self.assertIn(LABEL_FULL, gh.poses())
        self.assertEqual(gh.arms(), [["pr", "merge", str(PR), "--repo", DEPOT, "--auto",
                                      "--merge", "--match-head-commit", GEL]])
        self.assertEqual(out["auto_merge"]["state"], "armed", out)
        # l'armement suit la pose : l'étiquette part d'abord
        label = next(i for i, c in enumerate(gh.calls)
                     if c[:3] == ["api", "-X", "POST"] and "/labels" in c[3])
        arm = next(i for i, c in enumerate(gh.calls) if c[:2] == ["pr", "merge"])
        self.assertLess(label, arm)

    def test_an_unprotected_target_still_requests_the_ci(self):
        gh = ArmingGh(protected=False)
        code, out = self.run_cmd(gh)
        self.assertEqual(code, 0, out)
        self.assertIn(LABEL_FULL, gh.poses())
        self.assertEqual(gh.arms(), [])
        self.assertEqual(out["auto_merge"]["state"], "unprotected")

    def test_a_dry_run_arms_nothing(self):
        gh = ArmingGh()
        code, out = self.run_cmd(gh, dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(gh.arms(), [])
        self.assertNotIn("auto_merge", out)

    def test_a_crash_of_the_arming_never_fails_the_request(self):
        gh = ArmingGh()
        with mock.patch.object(packet_automerge, "arm", side_effect=RuntimeError("boom")):
            code, out = self.run_cmd(gh)
        self.assertEqual(code, 0, out)
        self.assertIn(LABEL_FULL, gh.poses())
        self.assertEqual(out["auto_merge"]["state"], "error")


class ArmPacketTest(unittest.TestCase):
    """`ci._arm_packet` : seuls un porteur et une suite complète arment."""

    def decision(self, **over):
        row = {"mode": "full", "head_ref": "integration/paquet-h-x", "action": "request",
               "repo": DEPOT, "pr": PR, "sha": GEL, "base_ref": "main", "base": BASE}
        row.update(over)
        return row

    def call(self, decision):
        args = argparse.Namespace(id=LOT)
        with mock.patch.object(packet_automerge, "arm",
                               return_value={"state": "armed", "why": "ok"}) as arm:
            out = ci._arm_packet(None, decision, LabelGh(), args)
        return out, arm

    def test_a_carrier_full_request_arms_on_the_target_branch(self):
        out, arm = self.call(self.decision())
        self.assertEqual(out["state"], "armed")
        kwargs = arm.call_args.kwargs
        self.assertEqual((kwargs["slug"], kwargs["number"], kwargs["sha"], kwargs["target"]),
                         (DEPOT, PR, GEL, "main"))

    def test_green_and_running_compositions_arm_too(self):
        for action in ("green", "running"):
            with self.subTest(action=action):
                self.assertIsNotNone(self.call(self.decision(action=action))[0])

    def test_a_lot_branch_or_a_lot_request_arms_nothing(self):
        for over in ({"head_ref": "lot/2813-x"}, {"mode": "lot"},
                     {"head_ref": "release/1.14"}):
            with self.subTest(**over):
                out, arm = self.call(self.decision(**over))
                self.assertIsNone(out)
                arm.assert_not_called()

    def test_the_rendering_says_when_nothing_is_armed(self):
        decision = self.decision(work_item=LOT, branch="integration/paquet-h-x",
                                 action="request", why="full suite: porteur", label=LABEL_FULL,
                                 auto_merge={"state": "unprotected", "why": "no check"})
        self.assertIn("  auto-merge NOT armed (unprotected): no check",
                      ci._render_gel(decision))


if __name__ == "__main__":
    unittest.main()
