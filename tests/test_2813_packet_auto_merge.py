# SPDX-License-Identifier: AGPL-3.0-only
"""#2813 — la fusion automatique d'un paquet, armée à sa publication (canon §12).

Décision owner du 20/10 : le geste qui publie le paquet (`ameesh ci run
<porteur> --full`) arme sa fusion sur la tête gelée ; tout nouveau push
désarme, la publication suivante réarme sur la nouvelle tête ; `work
merge-pr` ne désarme plus (`test_1656_auto_merge`).

Relectures de 249b5911 (ameesh-cx2, mty-amh-impl-01) tenues ici :
B1 — aucune auto-fusion GitHub n'est armée (elle survivrait à un push d'un
compte en écriture) : ameesh fusionne lui-même la tête publiée, une autre
tête déjà verte ne fusionne jamais ; B2 — la cible est épinglée, une PR
reciblée est désarmée ; B3 — sans armement GitHub, aucun armement côté forge
ne peut échapper au journal ; B4 — le bail est renouvelé avant chaque geste,
un bail perdu n'agit pas. R1/R2 (ameesh-cx3, ameesh-cx2) : la porte des
verdicts du feu, le projet résolu pour toute la porte de CI, une preuve de la
forge non vide, et l'écriture `POST merges` liée à la cible et au commit
publiés (un reciblage au dernier instant ne la redirige pas).

Témoins sans réseau ni base : un faux `gh` (protection, PR, checks de la
tête, fusion, désarmement) et un faux journal de lot (`work.events`/`note`).
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

from ameesh import ci, fire, packet_automerge, plan_github, work  # noqa: E402

from .test_l161g_ci_gel import (BASE, DEPOT, GEL, LABEL_FULL, PR, GelTestCase,  # noqa: E402
                                Gh as LabelGh, Github, pull)

SLUG = "manaty/ameesh"
LOT = 2834
NUM = 2140
HEAD = "1" * 40
NEW = "2" * 40
APP = 15368
PROTECTED = {"required_status_checks": {"strict": False, "contexts": ["tests"],
                                        "checks": [{"context": "tests", "app_id": APP}]}}
#: l'issue d'un check `tests` dans le rollup et les check runs de la tête
RUNS = {"success": ("COMPLETED", "SUCCESS"), "failure": ("COMPLETED", "FAILURE"),
        "pending": ("IN_PROGRESS", "")}


def rollup(tests):
    if tests is None:
        return []
    status, conclusion = RUNS[tests]
    return [{"__typename": "CheckRun", "name": "tests", "workflowName": "tests",
             "detailsUrl": "https://github.com/manaty/ameesh/actions/runs/200/job/123",
             "startedAt": "2026-10-20T03:00:00Z", "completedAt": "2026-10-20T03:20:00Z",
             "status": status, "conclusion": conclusion}]


class Forge:
    """La PR et la protection, telles que les deux faux `gh` les rendent."""

    def __init__(self, *, protection=PROTECTED, protection_error="", head=HEAD,
                 state="OPEN", base="main", auto=False, tests="pending", app=APP,
                 merge_error="", disarm_error="", view_error="", sticky=False,
                 branch="integration/paquet-h-20261020-0500", base_oid=HEAD,
                 ref_error=""):
        self.protection, self.protection_error = protection, protection_error
        self.head, self.state, self.base, self.auto = head, state, base, auto
        self.tests, self.app, self.branch = tests, app, branch
        self.merge_error, self.disarm_error = merge_error, disarm_error
        self.view_error = view_error
        #: la cible résolue, lue par `gh api git/ref/heads/<base>` (#3308)
        self.base_oid, self.ref_error = base_oid, ref_error
        #: plusieurs PR partagent ce faux : un geste ne vaut pas pour l'autre
        self.sticky = sticky
        self.calls: list[list[str]] = []
        #: les écritures `POST merges` : (branche écrite, commit fusionné)
        self.landed: list[tuple[str, str]] = []
        #: tête déjà contenue dans la cible : GitHub répond 204 sans corps
        self.contained = False

    def answer(self, args) -> tuple[int, str, str]:
        args = list(args)
        self.calls.append(args)
        path = args[-1] if args else ""
        # #2876 : une PR verte prouve aussi qu'aucun portier invisible ne
        # tourne encore sur cette tête. Ce banc ne crée aucun run paquet.
        if args[:1] == ["api"] and "/actions/runs?" in path:
            return 0, json.dumps({"total_count": 0, "workflow_runs": []}), ""
        if args[:1] == ["api"] and path.endswith("/actions/runs/200"):
            return 0, json.dumps({"id": 200, "head_sha": self.head, "head_branch": self.branch,
                                  "event": "pull_request", "path": ".github/workflows/tests.yml",
                                  "status": "completed", "conclusion": "success"}), ""
        if args[:1] == ["api"] and "/git/ref/heads/" in path:
            if self.ref_error:
                return 1, "", self.ref_error
            return 0, json.dumps({"ref": "refs/heads/%s" % self.base,
                                  "object": {"type": "commit",
                                             "sha": self.base_oid}}), ""
        if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/merges"):
            fields = dict(a.split("=", 1) for a in args[4:] if a != "-f")
            if self.merge_error:
                return 1, "", self.merge_error
            if self.contained:
                return 0, "", ""
            self.landed.append((fields["base"], fields["head"]))
            # GitHub marque la PR fusionnée quand sa tête entre dans SA base
            if fields["base"] == self.base and fields["head"] == self.head \
                    and not self.sticky:
                self.state = "MERGED"
            return 0, json.dumps({"sha": "3" * 40, "commit": {
                "message": fields["commit_message"]}}), ""
        if args[:1] == ["api"] and path.endswith("/protection"):
            if self.protection_error:
                return 1, "", self.protection_error
            if self.protection is None:
                return 1, "", "gh: Branch not protected (HTTP 404)"
            return 0, json.dumps(self.protection), ""
        if args[:1] == ["api"] and "/rules/branches/" in path:
            if self.protection_error:
                return 1, "", self.protection_error
            return 0, "[[]]", ""
        if args[:1] == ["api"] and "/check-runs" in path:
            sha = path.split("/commits/", 1)[1].split("/", 1)[0]
            runs = []
            if self.tests is not None and sha == self.head:
                status, conclusion = RUNS[self.tests]
                runs = [{"name": "tests", "status": status.lower(),
                         "conclusion": conclusion.lower() or None, "head_sha": sha,
                         "app": {"id": self.app}}]
            return 0, json.dumps([{"total_count": len(runs), "check_runs": runs}]), ""
        if args[:2] == ["pr", "view"]:
            if self.view_error:
                return 1, "", self.view_error
            return 0, json.dumps({
                "state": self.state, "headRefOid": self.head, "headRefName": self.branch,
                "baseRefName": self.base, "isDraft": False,
                "autoMergeRequest": {"enabledAt": "2026-10-20T03:00:00Z"} if self.auto
                else None, "statusCheckRollup": rollup(self.tests)}), ""
        if args[:2] == ["pr", "merge"] and "--disable-auto" in args:
            if self.disarm_error:
                return 1, "", self.disarm_error
            self.auto = self.sticky
            return 0, "", ""
        if args[:2] == ["pr", "merge"] and "--auto" in args:
            return 1, "", "unexpected: ameesh never arms a GitHub auto-merge (B1)"
        if args[:2] == ["pr", "merge"] and "--match-head-commit" in args:
            if self.merge_error:
                return 1, "", self.merge_error
            if args[args.index("--match-head-commit") + 1] != self.head:
                return 1, "", "GraphQL: Head branch was modified. Review and try the merge again."
            if not self.sticky:
                self.state = "MERGED"
            return 0, "", ""
        return 1, "", "unexpected gh call %s" % args

    def merges(self) -> list[list[str]]:
        """Tout geste de fusion : l'écriture liée à la cible, ou un `gh pr merge`
        (qu'ameesh ne fait plus)."""
        return [c for c in self.calls if (c[:3] == ["api", "-X", "POST"]
                                          and c[3].endswith("/merges"))
                or (c[:2] == ["pr", "merge"] and "--disable-auto" not in c)]

    def disarms(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"] and "--disable-auto" in c]

    def auto_arms(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"] and "--auto" in c]


class FakeGh(Forge):
    """Le `gh` de `land`/`fire` : `gh(args, timeout=…)` → CompletedProcess."""

    def __call__(self, args, timeout=60.0):
        code, out, err = self.answer(args)
        return subprocess.CompletedProcess(["gh", *args], code, out, err)


class Journal(unittest.TestCase):
    """Le faux journal du lot (`work.events`/`events_noted`, `work.note`) et
    le bail."""

    def setUp(self) -> None:
        self.events: dict[int, list[dict]] = {}
        self.seq = itertools.count(1)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(work, "events", self._events))
        stack.enter_context(mock.patch.object(work, "events_noted",
                                              self._events_noted))
        stack.enter_context(mock.patch.object(work, "note", self._note))
        #: le bail `auto-merge:<lot>` : libre, sauf quand un témoin le prend
        self.held: set[int] = set()
        self.renew = True
        stack.enter_context(mock.patch.object(
            packet_automerge, "_lock",
            lambda db, lot: None if int(lot) in self.held else ("bail", int(lot))))
        stack.enter_context(mock.patch.object(packet_automerge, "_unlock", lambda h: None))
        stack.enter_context(mock.patch.object(packet_automerge, "_renew",
                                              lambda h: self.renew))
        #: le porteur, son gel (la tête publiée par défaut) et la porte des avis
        self.frozen = HEAD
        self.gate = (True, "", None)
        stack.enter_context(mock.patch.object(fire, "_fresh", lambda db, lot: (
            {"id": int(lot), "app": "ameesh", "assignee": "composeur",
             "branch": "integration/paquet-h-20261020-0500"},
            [{"id": 1, "kind": "frozen", "sha": self.frozen, "at_ts": 1.0}]
            if self.frozen else [])))
        stack.enter_context(mock.patch.object(
            packet_automerge, "_verdicts", lambda *a, **k: self.gate))

    def _events(self, db, item_id, limit=100):
        # #3279 : le vrai `work.events` fenêtre ; le faux doit le faire aussi,
        # sinon les notes `held` ne peuvent pas évincer la marque d'armement.
        rows = list(reversed(self.events.get(int(item_id), [])))
        return rows if limit is None else rows[:int(limit)]

    def _events_noted(self, db, item_id, prefix, since_ts=0.0):
        return [event for event in self.events.get(int(item_id), [])
                if (event.get("note") or "").startswith(prefix)
                and float(event.get("created_ts") or 0) >= float(since_ts)]

    def _note(self, db, item_id, text, **kw):
        self.events.setdefault(int(item_id), []).append(
            {"id": next(self.seq), "note": text, "created_ts": time.time()})
        return True

    def notes(self, lot: int = LOT) -> list[str]:
        return [e["note"] for e in self.events.get(lot, [])]

    def armed(self, sha: str = HEAD, lot: int = LOT, pr: int = NUM, base: str = "main",
              composition: str = ""):
        pinned = "base=%s method=merge required=tests" % base
        if composition:
            pinned += " composition=%s" % composition
        self._note(None, lot, "auto-merge armed repo=%s pr=%d sha=%s %s — test"
                   % (SLUG, pr, sha, pinned))

    def active(self, lot: int = LOT):
        return packet_automerge.active(packet_automerge.marks(None, lot))

    def arm(self, gh, sha: str = HEAD) -> dict:
        return packet_automerge.arm(None, None, LOT, slug=SLUG, number=NUM, sha=sha,
                                    target="main", gh=gh)


class ArmTest(Journal):
    def test_a_protected_target_pins_head_and_target_without_github_auto_merge(self):
        gh = FakeGh()
        out = self.arm(gh)
        self.assertEqual(out["state"], "armed", out)
        self.assertEqual(out["merge"]["result"], "pending", out)
        self.assertEqual(gh.auto_arms(), [])
        self.assertEqual(gh.merges(), [])
        self.assertTrue(self.notes()[0].startswith(
            "auto-merge armed repo=%s pr=%d sha=%s base=main method=merge required=tests"
            % (SLUG, NUM, HEAD)), self.notes())
        # #3175 : la tentative immédiate qui attend la CI laisse son motif
        self.assertTrue(self.notes()[1].startswith(
            "auto-merge held repo=%s pr=%d sha=%s base=main — " % (SLUG, NUM, HEAD)),
            self.notes())
        self.assertIn("PR #%d" % NUM, self.notes()[1])
        self.assertEqual((self.active()["sha"], self.active()["base"]), (HEAD, "main"))

    def test_a_composition_already_green_merges_at_once_on_its_head(self):
        gh = FakeGh(tests="success")
        out = self.arm(gh)
        self.assertEqual(out["merge"]["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])
        self.assertTrue(self.notes()[-1].startswith("auto-merge merged"), self.notes())
        self.assertIsNone(self.active())

    def test_the_same_publication_is_not_journaled_twice(self):
        self.armed()
        gh = FakeGh()
        out = self.arm(gh)
        self.assertEqual(out["state"], "already-armed", out)
        self.assertEqual(len([n for n in self.notes() if " armed " in n]), 1)

    def test_held_notes_do_not_evict_the_armed_mark(self):
        # #3279 : un porteur qui refuse à chaque passage écrit des notes
        # `held` (motifs alternés A/B/A, non dédupliquées par le journal).
        # Au-delà de la fenêtre réelle de `work.events` (200), l'armement
        # disparaissait : il ne doit plus être chassé par ses propres traces.
        self.armed()
        for index in range(250):
            self._note(None, LOT, "auto-merge held repo=%s pr=%d sha=%s base=main "
                                  "— motif %d" % (SLUG, NUM, HEAD, index % 2))
        self.assertEqual(len(work.events(None, LOT, limit=200)), 200)
        self.assertIn("armed", [row["state"]
                                for row in packet_automerge.marks(None, LOT)])
        self.assertIsNotNone(self.active())
        self.assertEqual(self.active()["sha"], HEAD)

    def test_a_github_auto_merge_is_disarmed_never_armed(self):
        gh = FakeGh(auto=True)
        out = self.arm(gh)
        self.assertEqual(out["state"], "armed", out)
        self.assertEqual(len(gh.disarms()), 1)
        self.assertEqual(gh.auto_arms(), [])
        self.assertFalse(gh.auto)

    def test_a_new_publication_moves_the_pin(self):
        self.armed(sha=HEAD)
        self.frozen = NEW
        gh = FakeGh(head=NEW)
        self.assertEqual(self.arm(gh, sha=NEW)["state"], "armed")
        self.assertEqual(self.active()["sha"], NEW)

    def test_an_unprotected_target_arms_nothing_and_names_the_fire(self):
        gh = FakeGh(protection=None)
        out = self.arm(gh)
        self.assertEqual(out["state"], "unprotected", out)
        self.assertIn("ameesh work fire %d --sha %s" % (LOT, HEAD[:12]), out["why"])
        self.assertEqual([c for c in gh.calls if c[:1] == ["pr"]], [])
        self.assertTrue(self.notes()[-1].startswith("auto-merge not-armed"), self.notes())
        self.assertIsNone(self.active())

    def test_a_protection_without_required_check_arms_nothing(self):
        gh = FakeGh(protection={"required_status_checks": None})
        self.assertEqual(self.arm(gh)["state"], "unprotected")
        self.assertIsNone(self.active())

    def test_a_forbidden_protection_read_is_the_nexlink_case(self):
        gh = FakeGh(protection_error="Upgrade to GitHub Pro or make this repository public "
                                     "to enable this feature. (HTTP 403)")
        out = self.arm(gh)
        self.assertEqual(out["state"], "unprotected", out)
        self.assertIn("ameesh work fire", out["why"])

    def test_an_unreadable_protection_arms_nothing(self):
        gh = FakeGh(protection_error="gh: Bad gateway (HTTP 502)")
        self.assertEqual(self.arm(gh)["state"], "error")
        self.assertIsNone(self.active())

    def test_a_moved_head_arms_nothing(self):
        gh = FakeGh(head=NEW)
        self.assertEqual(self.arm(gh, sha=HEAD)["state"], "moved")
        self.assertIsNone(self.active())

    def test_another_base_arms_nothing(self):
        gh = FakeGh(base="develop")
        self.assertEqual(self.arm(gh)["state"], "error")
        self.assertIsNone(self.active())

    def test_a_failed_disarm_arms_nothing(self):
        gh = FakeGh(auto=True, disarm_error="HTTP 502")
        out = self.arm(gh)
        self.assertEqual(out["state"], "error", out)
        self.assertIsNone(self.active())

    def test_a_short_sha_is_refused(self):
        gh = FakeGh()
        self.assertEqual(self.arm(gh, sha=HEAD[:12])["state"], "error")
        self.assertEqual(gh.calls, [])


class LeaseTest(Journal):
    """Le bail `auto-merge:<lot>` sérialise publication et passage (B4)."""

    def test_a_held_lease_arms_nothing(self):
        self.held.add(LOT)
        gh = FakeGh()
        out = self.arm(gh)
        self.assertEqual(out["state"], "error", out)
        self.assertIn("ameesh ci run %d --full" % LOT, out["why"])
        self.assertEqual(gh.calls, [])

    def test_a_held_lease_defers_the_pass(self):
        self.armed()
        self.held.add(LOT)
        gh = FakeGh(head=NEW, auto=True)
        self.assertEqual(packet_automerge.check(None, None, LOT, gh=gh)["result"], "busy")
        self.assertEqual(gh.calls, [])

    def test_the_marks_are_read_again_under_the_lease(self):
        """Une publication réarme sur la nouvelle tête entre la première lecture
        du passage et son bail : le passage relit et garde l'armement."""
        self.armed(sha=HEAD)
        self.frozen = NEW
        gh = FakeGh(head=NEW)
        real = packet_automerge._lock

        def lock(db, lot):
            self.armed(sha=NEW)          # la publication s'est faite juste avant
            return real(db, lot)
        with mock.patch.object(packet_automerge, "_lock", lock):
            out = packet_automerge.check(None, None, LOT, gh=gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(self.active()["sha"], NEW)

    def test_a_lost_lease_journals_no_arming(self):
        self.renew = False
        gh = FakeGh()
        out = self.arm(gh)
        self.assertEqual(out["state"], "error", out)
        self.assertIn("lease", out["why"])
        self.assertIsNone(self.active())

    def test_a_lost_lease_never_merges_nor_disarms(self):
        self.armed()
        self.renew = False
        for gh in (FakeGh(tests="success"), FakeGh(head=NEW, auto=True)):
            with self.subTest(head=gh.head):
                self.assertEqual(packet_automerge.check(None, None, LOT, gh=gh)["result"], "busy")
                self.assertEqual(gh.merges() + gh.disarms(), [])


class MarksTest(unittest.TestCase):
    def row(self, ident, state, sha=HEAD, pr=NUM, base="main", required=("tests",)):
        return {"id": ident, "state": state, "repo": SLUG, "pr": pr, "sha": sha,
                "base": base, "required": required}

    def test_a_disarm_or_a_merge_closes_the_arming(self):
        for closing in ("disarmed", "merged"):
            with self.subTest(closing=closing):
                self.assertIsNone(packet_automerge.active([self.row(1, "armed"),
                                                           self.row(2, closing)]))

    def test_a_refusal_does_not_close_the_arming(self):
        self.assertEqual(packet_automerge.active([self.row(1, "armed"),
                                                  self.row(2, "not-armed", sha=NEW)])["id"], 1)

    def test_a_disarm_of_another_pr_does_not_close_it(self):
        self.assertEqual(packet_automerge.active([self.row(1, "armed"),
                                                  self.row(2, "disarmed", pr=1)])["id"], 1)

    def test_an_arming_without_target_or_checks_is_not_followed(self):
        self.assertIsNone(packet_automerge.active([self.row(1, "armed", base="")]))
        self.assertIsNone(packet_automerge.active([self.row(1, "armed", required=())]))

    def test_the_pinned_checks_are_read_back_exactly(self):
        m = packet_automerge.MARK_RE.match(
            "auto-merge armed repo=%s pr=%d sha=%s base=main method=merge "
            "required=tests,couverture%%20(PR),a%%2Cb — x" % (SLUG, NUM, HEAD))
        self.assertEqual(m.group("required"), "tests,couverture%20(PR),a%2Cb")

    def test_the_journal_line_is_parsed_with_its_target(self):
        m = packet_automerge.MARK_RE.match(
            "auto-merge armed repo=%s pr=%d sha=%s base=release/production method=merge"
            % (SLUG, NUM, HEAD))
        self.assertEqual((m.group("state"), m.group("base")), ("armed", "release/production"))
        self.assertIsNone(packet_automerge.MARK_RE.match(
            "auto-merge armed repo=%s pr=%d sha=%s base=main" % (SLUG, NUM, HEAD[:12])))


class PassTest(Journal):
    """`check` : fusionner la tête publiée au vert, désarmer toute autre."""

    def check(self, gh, **kw):
        return packet_automerge.check(None, None, LOT, gh=gh, **kw)

    def test_nothing_armed_reads_nothing(self):
        gh = FakeGh()
        self.assertEqual(self.check(gh)["result"], "idle")
        self.assertEqual(gh.calls, [])

    def test_a_pending_ci_waits(self):
        self.armed()
        gh = FakeGh(tests="pending")
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.merges(), [])

    def test_a_red_or_foreign_check_never_merges(self):
        self.armed()
        for gh in (FakeGh(tests="failure"), FakeGh(tests="success", app=1),
                   FakeGh(tests=None)):
            with self.subTest(tests=gh.tests, app=gh.app):
                self.assertEqual(self.check(gh)["result"], "pending")
                self.assertEqual(gh.merges(), [])

    def test_a_green_published_head_is_merged_on_that_head(self):
        self.armed()
        gh = FakeGh(tests="success")
        out = self.check(gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])
        post = gh.merges()[0]
        self.assertEqual(post[:4], ["api", "-X", "POST", "repos/%s/merges" % SLUG])
        self.assertIn("commit_message=Merge pull request #%d from manaty/integration/"
                      "paquet-h-20261020-0500" % NUM, post[-1])
        self.assertTrue(self.notes()[-1].startswith(
            "auto-merge merged repo=%s pr=%d sha=%s base=main commit=%s"
            % (SLUG, NUM, HEAD, "3" * 40)))
        self.assertEqual(self.check(gh)["result"], "idle")

    def test_the_pr_base_oid_proves_the_composition_base(self):
        # #3091 : la cible résolue lue sur la PR (`baseRefOid`) est transmise
        # à la porte des avis, au lieu de dépendre de la ref locale du clone.
        self.armed()
        seen = {}

        def verdicts(cfg, db, row, ms, *, slug, base, base_sha=""):
            seen.update(base=base, base_sha=base_sha)
            return (True, "", None)

        with mock.patch.object(packet_automerge, "_verdicts", verdicts):
            out = self.check(FakeGh(tests="success", base_oid=NEW))
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(seen, {"base": "main", "base_sha": NEW})


    def test_b1_a_new_head_already_green_is_never_merged(self):
        """B1 : la tête poussée après la publication porte déjà un `tests` vert
        (commit déjà testé) ; elle n'est pas la tête publiée, elle est désarmée."""
        self.armed()
        gh = FakeGh(head=NEW, tests="success", auto=True)
        out = self.check(gh)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertEqual(gh.merges(), [])
        self.assertEqual(len(gh.disarms()), 1)
        self.assertTrue(self.notes()[-1].startswith(
            "auto-merge disarmed repo=%s pr=%d sha=%s base=main head=%s"
            % (SLUG, NUM, HEAD, NEW)), self.notes())
        self.assertIn("ameesh ci run %d --full" % LOT, self.notes()[-1])
        self.assertIsNone(self.active())

    def test_b2_a_retargeted_pr_is_disarmed_not_merged(self):
        self.armed()
        gh = FakeGh(base="release/production", tests="success", auto=True)
        out = self.check(gh)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertEqual(gh.merges(), [])
        self.assertEqual(len(gh.disarms()), 1)
        self.assertIn("now targets release/production", self.notes()[-1])

    def test_a_head_pushed_at_the_gesture_never_changes_the_merged_commit(self):
        """L'écriture nomme le commit publié : une poussée sur la branche du
        porteur au dernier instant n'entre pas dans la cible."""
        self.armed()
        gh = FakeGh(tests="success")
        real = gh.answer

        def answer(args):
            if args[:3] == ["api", "-X", "POST"]:
                gh.head = NEW               # push juste avant le geste
            return real(args)
        gh.answer = answer
        self.assertEqual(self.check(gh)["result"], "merged")
        self.assertEqual(gh.landed, [("main", HEAD)])

    def retarget_on(self, gh, when):
        """La cible passe à `release/production` dès l'appel `gh` que `when` désigne."""
        real = gh.answer

        def answer(args):
            out = real(args)
            if when(args):
                gh.base = "release/production"
            return out
        gh.answer = answer

    def test_r2_b2_a_retarget_during_the_gate_reads_never_merges(self):
        """Repro cx2 : la cible change après la lecture des check runs, dans
        les lectures de `ready_pr` ; la relecture fraîche la voit."""
        self.armed()
        gh = FakeGh(tests="success")
        self.retarget_on(gh, lambda args: args[:1] == ["api"] and "/check-runs" in args[-1])
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("changed just before the merge", out["why"])
        self.assertEqual(gh.merges(), [])
        self.assertEqual(gh.state, "OPEN")
        # le passage suivant désarme
        self.assertEqual(self.check(gh)["result"], "disarmed")

    def test_r2_b2_a_retarget_at_the_gesture_cannot_redirect_the_write(self):
        """Repro cx2 R2 : la PR passe à `release/production` juste avant le
        geste. L'écriture nomme la cible épinglée : rien n'entre ailleurs."""
        self.armed()
        gh = FakeGh(tests="success")
        real = gh.answer

        def answer(args):
            if args[:3] == ["api", "-X", "POST"]:
                gh.base = "release/production"
            return real(args)
        gh.answer = answer
        self.check(gh)
        self.assertEqual(gh.landed, [("main", HEAD)])
        self.assertNotIn("release/production", [base for base, _ in gh.landed])

    def test_no_gh_pr_merge_is_ever_used(self):
        self.armed()
        gh = FakeGh(tests="success", auto=True)
        self.assertEqual(self.check(gh)["result"], "merged")
        self.assertEqual([c for c in gh.calls if c[:2] == ["pr", "merge"]
                          and "--disable-auto" not in c], [])
        kinds = [c[:2] for c in gh.calls if c[:1] == ["pr"]]
        self.assertEqual(kinds, [["pr", "view"], ["pr", "merge"], ["pr", "view"]])

    def test_a_head_already_in_the_target_is_recorded(self):
        self.armed()
        gh = FakeGh(tests="success")
        gh.contained = True
        self.assertEqual(self.check(gh)["result"], "merged")
        self.assertIn("commit=already-contained", self.notes()[-1])

    def test_a_refused_write_keeps_the_arming(self):
        self.armed()
        gh = FakeGh(tests="success", merge_error="HTTP 409: Merge conflict")
        out = self.check(gh)
        self.assertEqual(out["result"], "error", out)
        self.assertIn("Merge conflict", out["error"])
        self.assertIsNotNone(self.active())

    def project(self, **over):
        values = {"name": "ameesh", "required_checks": (), "required_statuses": (),
                  "ci_required": False}
        values.update(over)
        return types.SimpleNamespace(**values)

    def test_r2_the_resolved_project_gates_the_ci_too(self):
        """Témoin cx3 R2 : la forge seule est verte, le projet exige un statut
        `full-suite` sur `integration/**` : rien ne fusionne."""
        self.armed()
        self.gate = (True, "", self.project(
            required_statuses=(("integration/**", "full-suite"),)))
        gh = FakeGh(tests="success")
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("full-suite", out["why"])
        self.assertEqual(gh.merges(), [])

    def test_r2_a_declared_check_adds_to_the_forge(self):
        self.armed()
        self.gate = (True, "", self.project(required_checks=("lint",)))
        gh = FakeGh(tests="success")
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("lint", out["why"])
        self.assertEqual(gh.merges(), [])

    def test_r2_a_declaration_never_replaces_the_forge_proof(self):
        self.armed()
        self.gate = (True, "", self.project(required_checks=("tests",)))
        gh = FakeGh(tests="success", protection={"required_status_checks": None})
        out = self.check(gh)
        self.assertEqual(out["result"], "error", out)
        self.assertIn("no longer enforces", out["error"])
        self.assertEqual(gh.merges(), [])

    def test_r1_b1_a_refused_review_gate_never_merges(self):
        """cx3 R1 : la porte des verdicts (classe, avis, membres et delta d'un
        porteur) passe avant la lecture des protections et tout geste de fusion.
        La lecture du statut CI inclut la preuve de run du portier (#2876) ; la
        cible résolue (`git/ref`, #3308) est lue pour cette porte, pas une
        protection."""
        self.armed()
        self.gate = (False, "2 independent reviews missing on 1111111111", None)
        gh = FakeGh(tests="success")
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("reviews missing", out["why"])
        self.assertEqual(gh.merges(), [])
        self.assertEqual([c for c in gh.calls if c[:1] == ["api"]
                          and "/actions/" not in c[1]
                          and "/git/ref/heads/" not in c[1]], [])

    def test_r1_b2_a_protection_emptied_after_publication_never_merges(self):
        self.armed()
        for protection in ({"required_status_checks": None},
                           {"required_status_checks": {"contexts": ["other"],
                                                       "checks": []}}):
            with self.subTest(protection=protection):
                gh = FakeGh(tests="success", protection=protection)
                out = self.check(gh)
                self.assertEqual(out["result"], "error", out)
                self.assertIn("no longer enforces", out["error"])
                self.assertEqual(gh.merges(), [])

    def test_a_refreeze_on_another_head_disarms(self):
        self.armed()
        self.frozen = NEW
        gh = FakeGh(tests="success")
        out = self.check(gh)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertIn("frozen=%s" % NEW, self.notes()[-1])
        self.assertEqual(gh.merges(), [])

    def test_a_github_auto_merge_armed_by_hand_is_disarmed_before_merging(self):
        self.armed()
        gh = FakeGh(tests="success", auto=True)
        self.assertEqual(self.check(gh)["result"], "merged")
        gestures = [c for c in gh.calls if c[:2] == ["pr", "merge"] or c[:2] == ["api", "-X"]]
        self.assertIn("--disable-auto", gestures[0])
        self.assertEqual(gestures[1][:3], ["api", "-X", "POST"])

    def test_a_dry_run_writes_nothing(self):
        self.armed()
        for gh, expected in ((FakeGh(tests="success"), "would-merge"),
                             (FakeGh(head=NEW, auto=True), "would-disarm")):
            with self.subTest(expected=expected):
                self.assertEqual(self.check(gh, dry_run=True)["result"], expected)
                self.assertEqual(gh.merges() + gh.disarms(), [])
        self.assertIsNotNone(self.active())

    def test_a_pr_merged_or_closed_elsewhere_is_left_alone(self):
        self.armed()
        for state in ("MERGED", "CLOSED"):
            with self.subTest(state=state):
                gh = FakeGh(head=NEW, state=state, auto=True)
                self.assertEqual(self.check(gh)["result"], "closed")
                self.assertEqual(gh.merges() + gh.disarms(), [])

    def test_a_failed_disarm_keeps_the_arming(self):
        self.armed()
        gh = FakeGh(head=NEW, auto=True, disarm_error="HTTP 502")
        self.assertEqual(self.check(gh)["result"], "error")
        self.assertIsNotNone(self.active())

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
            out = packet_automerge.watch(None, None, "ameesh", gh=gh)
        self.assertEqual(sorted(r["lot"] for r in out), [9, LOT])
        self.assertEqual({r["result"] for r in out}, {"disarmed"})
        self.assertEqual(sorted(c[2] for c in gh.calls if c[:2] == ["pr", "view"]),
                         sorted([str(NUM), str(NUM - 1)]))

    def test_one_carrier_error_does_not_stop_the_other(self):
        rows = {"build": [{"id": LOT, "branch": "integration/paquet-h-a", "app": "ameesh"},
                          {"id": 9, "branch": "integration/paquet-h-b", "app": "ameesh"}]}
        store = types.SimpleNamespace(items=lambda state, assignee, limit: rows.get(state, []))
        self.armed(lot=9)
        real = packet_automerge.marks

        def marks(db, lot):
            if lot == LOT:
                raise RuntimeError("boom")
            return real(db, lot)
        with mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)), \
                mock.patch.object(packet_automerge, "marks", marks):
            out = packet_automerge.watch(None, None, "ameesh", gh=FakeGh(head=NEW, auto=True))
        self.assertEqual({r["lot"]: r["result"] for r in out}, {LOT: "error", 9: "disarmed"})


class TargetMovedTest(Journal):
    """#3534 : la cible a avancé depuis la base de composition du paquet.

    Seul un atterrissage de coupure (ligne de version de `pyproject.toml` et
    notes `docs/versions/*.md`, manaty/home#173) laisse fusionner la tête
    publiée ; un commit de code retient le paquet (mise à jour + CI à
    relancer)."""

    def check(self, gh, **kw):
        return packet_automerge.check(None, None, LOT, gh=gh, **kw)

    def repo(self):
        """Un dépôt jetable : base, puis commits de coupure ou de code."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = directory.name

        def git(*args):
            proc = subprocess.run(["git", "-C", path, *args], capture_output=True,
                                  text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return (proc.stdout or "").strip()

        git("init", "-q")
        git("config", "user.email", "test@ameesh.invalid")
        git("config", "user.name", "test")
        (Path(path) / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        return path, git, git("rev-parse", "HEAD")

    def cutoff(self, path, git):
        (Path(path) / "pyproject.toml").write_text('[project]\nversion = "1.0.1"\n')
        notes = Path(path) / "docs" / "versions"
        notes.mkdir(parents=True)
        (notes / "1.0.1.md").write_text("# 1.0.1\n")
        git("add", "-A")
        git("commit", "-qm", "release: 1.0.1 [skip ci]\n\nameesh-release: 1.0.1")
        return git("rev-parse", "HEAD")

    def code(self, path, git, name: str = "x.py"):
        (Path(path) / "src").mkdir(exist_ok=True)
        (Path(path) / "src" / name).write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "fix: code")
        return git("rev-parse", "HEAD")

    def test_a_target_that_advanced_by_code_is_held(self):
        path, git, base = self.repo()
        moved = self.code(path, git)
        self.armed(composition=base)
        gh = FakeGh(tests="success", base_oid=moved)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertEqual(gh.merges(), [])
        self.assertIn("target advanced since composition", self.notes()[-1])
        self.assertIn("ci run %d --full" % LOT, self.notes()[-1])

    def test_a_target_that_advanced_by_a_cutoff_landing_merges(self):
        path, git, base = self.repo()
        landing = self.cutoff(path, git)
        self.armed(composition=base)
        gh = FakeGh(tests="success", base_oid=landing)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])

    def test_a_cutoff_landing_followed_by_code_is_held(self):
        path, git, base = self.repo()
        self.cutoff(path, git)
        moved = self.code(path, git)
        self.armed(composition=base)
        gh = FakeGh(tests="success", base_oid=moved)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertEqual(gh.merges(), [])

    def test_a_release_landing_merge_is_a_cutoff_landing(self):
        path, git, base = self.repo()
        current = git("rev-parse", "--abbrev-ref", "HEAD")
        git("checkout", "-q", "-b", "release")
        self.cutoff(path, git)
        git("checkout", "-q", current)
        git("merge", "-q", "--no-ff", "release", "-m",
            "release: 1.0.1 landing [skip ci]\n\nameesh-release: 1.0.1")
        landing = git("rev-parse", "HEAD")
        self.armed(composition=base)
        gh = FakeGh(tests="success", base_oid=landing)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])

    def test_b1_a_target_advanced_during_the_gate_never_merges(self):
        self.armed(composition=HEAD)
        gh = FakeGh(tests="success")
        real = gh.answer

        def answer(args):
            out = real(args)
            if args[:1] == ["api"] and "/check-runs" in args[-1]:
                gh.base_oid = NEW       # la cible avance pendant les portes
            return out
        gh.answer = answer
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("changed just before the merge", out["why"])
        self.assertIn("expected target", out["why"])
        self.assertEqual(gh.merges(), [])

    def packet_with_target(self, path, git, base):
        """Une tête de paquet H2 = merge(H, cible courante) ; rend (main, H2)."""
        current = git("rev-parse", "--abbrev-ref", "HEAD")
        git("checkout", "-q", "-b", "packet", base)
        (Path(path) / "packet.txt").write_text("packet\n")
        git("add", "-A")
        git("commit", "-qm", "packet commit")
        git("merge", "-q", "--no-ff", current, "-m", "packet: merge target")
        head = git("rev-parse", "HEAD")
        return current, head

    def test_b2_a_target_merged_into_the_tested_head_is_not_held(self):
        path, git, base = self.repo()
        moved = self.code(path, git)
        _, head = self.packet_with_target(path, git, base)
        self.frozen = head
        self.armed(sha=head, composition=base)
        gh = FakeGh(head=head, base_oid=moved, tests="success")
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", head)])

    def test_b2a_the_fetch_precedes_the_target_head_check(self):
        # Le clone de passage ne connaît d'abord que la cible C (main) ; H2
        # arrive par le fetch de la branche du porteur, jamais avant la garde.
        path, git, base = self.repo()
        moved = self.code(path, git)
        _, head = self.packet_with_target(path, git, base)
        pass_dir = tempfile.TemporaryDirectory()
        self.addCleanup(pass_dir.cleanup)
        clone = pass_dir.name
        subprocess.run(["git", "-C", clone, "init", "-q"], check=True)
        subprocess.run(["git", "-C", clone, "remote", "add", "origin", path],
                       check=True)
        subprocess.run(["git", "-C", clone, "fetch", "-q", "origin",
                        "refs/heads/main:refs/remotes/origin/main"], check=True)
        self.frozen = head
        self.armed(sha=head, composition=base)
        gh = FakeGh(head=head, base_oid=moved, tests="success", branch="packet")
        with mock.patch.object(packet_automerge.fire, "_repo_of", return_value=None), \
                mock.patch.object(packet_automerge.fire, "_project", return_value=None), \
                mock.patch.object(packet_automerge.fire, "_project_checkout",
                                  return_value=None), \
                mock.patch.object(packet_automerge.fire, "_same_repository",
                                  return_value=True):
            out = self.check(gh, checkout=clone)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", head)])

    def test_b2_a_cutoff_after_the_included_target_still_merges(self):
        path, git, base = self.repo()
        self.code(path, git)                    # C, inclus dans H2
        current, head = self.packet_with_target(path, git, base)
        git("checkout", "-q", current)          # main revient sur C
        landing = self.cutoff(path, git)        # D = coupure après C
        self.frozen = head
        self.armed(sha=head, composition=base)
        gh = FakeGh(head=head, base_oid=landing, tests="success")
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", head)])

    def test_b2_a_code_after_the_included_target_is_held(self):
        path, git, base = self.repo()
        self.code(path, git)                    # C, inclus dans H2
        current, head = self.packet_with_target(path, git, base)
        git("checkout", "-q", current)
        moved = self.code(path, git, name="y.py")   # code après C
        self.frozen = head
        self.armed(sha=head, composition=base)
        gh = FakeGh(head=head, base_oid=moved, tests="success")
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertEqual(gh.merges(), [])

    def test_the_review_gate_keeps_the_true_target(self):
        # #3534 (B4) : la base enregistrée ne remplace jamais le SHA de la
        # cible dans la preuve transmise au feu ; `packet_reviews` y prouve
        # ensuite la base du manifeste.
        path, git, base = self.repo()
        landing = self.cutoff(path, git)
        self.armed(composition=base)
        seen = {}

        def verdicts(cfg, db, row, ms, **kw):
            seen.update(kw)
            return True, "", None

        with mock.patch.object(packet_automerge, "_verdicts", verdicts):
            out = self.check(FakeGh(tests="success", base_oid=landing), checkout=path)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(seen.get("base_sha"), landing)

    def test_b3_the_clone_is_resolved_before_the_cutoff_guard(self):
        path, git, base = self.repo()
        landing = self.cutoff(path, git)
        self.armed(composition=base)
        gh = FakeGh(tests="success", base_oid=landing)
        project = types.SimpleNamespace(repo=path)
        with mock.patch.object(packet_automerge.fire, "_repo_of", return_value=None), \
                mock.patch.object(packet_automerge.fire, "_project",
                                  return_value=project), \
                mock.patch.object(packet_automerge.fire, "_project_checkout",
                                  return_value=path):
            out = self.check(gh)        # checkout vide, clone résolu par le radar
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])

    def test_the_package_record_supplies_the_composition_base(self):
        fake = types.SimpleNamespace(work=types.SimpleNamespace(
            packet_members=lambda **kw: [
                {"base_sha": "a" * 40, "head_sha": HEAD, "package_target": "main"}]))
        with mock.patch.object(packet_automerge.storage, "of", lambda db: fake):
            self.assertEqual(
                packet_automerge._composition_base(object(), LOT, HEAD, "main"), "a" * 40)
            self.assertEqual(
                packet_automerge._composition_base(object(), LOT, NEW, "main"), "")

    def test_the_armed_mark_carries_its_composition_base(self):
        m = packet_automerge.MARK_RE.match(
            "auto-merge armed repo=%s pr=%d sha=%s base=main method=merge "
            "required=tests composition=%s — x" % (SLUG, NUM, HEAD, NEW))
        self.assertEqual(m.group("composition"), NEW)


class VerdictsTest(unittest.TestCase):
    """`_verdicts` : la porte du feu, sur le dépôt et le projet que le feu résout."""

    def test_the_fire_gate_runs_on_the_resolved_repository(self):
        row, ms = {"id": LOT, "assignee": "composeur"}, [{"kind": "frozen", "sha": HEAD}]
        project = types.SimpleNamespace(name="ameesh")
        with mock.patch.object(fire, "_repo_of", return_value="/autre"), \
                mock.patch.object(fire, "_same_repository", return_value=False), \
                mock.patch.object(fire, "_project", return_value=project) as resolve, \
                mock.patch.object(fire, "_project_checkout", return_value="/clone"), \
                mock.patch.object(fire, "ready_verdicts",
                                  return_value=(False, "verdicts-missing", {})) as gate:
            out = packet_automerge._verdicts("cfg", "db", row, ms, slug=SLUG, base="main")
        self.assertEqual(out, (False, "verdicts-missing", project))
        resolve.assert_called_once_with("cfg", None, SLUG)
        gate.assert_called_once_with("cfg", "db", row, ms, repo="/clone", project=project,
                                     target="main")

    def test_the_pr_base_oid_is_forwarded_to_the_fire_gate(self):
        row, ms = {"id": LOT, "assignee": "composeur"}, [{"kind": "frozen", "sha": HEAD}]
        project = types.SimpleNamespace(name="ameesh")
        with mock.patch.object(fire, "_repo_of", return_value="/clone"), \
                mock.patch.object(fire, "_same_repository", return_value=True), \
                mock.patch.object(fire, "_project", return_value=project), \
                mock.patch.object(fire, "ready_verdicts",
                                  return_value=(True, "", {})) as gate:
            out = packet_automerge._verdicts("cfg", "db", row, ms, slug=SLUG,
                                             base="main", base_sha=NEW)
        self.assertEqual(out, (True, "", project))
        gate.assert_called_once_with("cfg", "db", row, ms, repo="/clone", project=project,
                                     target="main", base_sha=NEW)

    def test_pr_status_carries_the_resolved_base(self):
        status = packet_automerge.pr_status(SLUG, NUM, gh=FakeGh(tests="success",
                                                                 base_oid=NEW))
        self.assertEqual(status["base_oid"], NEW)
        self.assertEqual(status["base"], "main")
        self.assertEqual(status["head"], HEAD)

    def agents2(self, checkout, *, clone="/home/ameesh/work/ameesh-packets", same=True,
                fetch_error=""):
        """#3175 : l'hôte du passage n'a ni le dossier de l'assigné (sur un
        autre hôte) ni `radar.projects` ; seul `packet_watch` déclare un clone."""
        row, ms = {"id": LOT, "assignee": "mty-amh-impl-01"}, [{"kind": "frozen", "sha": HEAD}]
        with mock.patch.object(fire, "_repo_of", return_value=None), \
                mock.patch.object(fire, "_project", return_value=None), \
                mock.patch.object(fire, "_checkout", return_value=clone) as root, \
                mock.patch.object(fire, "_same_repository", return_value=same), \
                mock.patch.object(packet_automerge, "_fetch_into",
                                  return_value=fetch_error) as self.fetch, \
                mock.patch.object(fire, "ready_verdicts",
                                  return_value=(True, "", {})) as gate:
            out = packet_automerge._verdicts("cfg", "db", row, ms, slug=SLUG, base="main",
                                             checkout=checkout,
                                             branch="integration/paquet-h-20261023-1600")
        return out, gate, root, row, ms

    def test_3175_the_pass_clone_backs_the_review_gate(self):
        out, gate, root, row, ms = self.agents2("~/work/ameesh-packets")
        self.assertEqual(out, (True, "", None))
        root.assert_called_once_with("~/work/ameesh-packets")
        self.fetch.assert_called_once_with("/home/ameesh/work/ameesh-packets",
                                           ("integration/paquet-h-20261023-1600", "main"))
        gate.assert_called_once_with("cfg", "db", row, ms,
                                     repo="/home/ameesh/work/ameesh-packets",
                                     project=None, target="main")

    def test_3175_a_clone_that_cannot_be_refreshed_refuses_with_its_reason(self):
        (ok, why, _project), gate, *_ = self.agents2("/clone", fetch_error="HTTP 503")
        self.assertFalse(ok)
        self.assertIn("cannot be refreshed", why)
        self.assertIn("HTTP 503", why)
        gate.assert_not_called()

    def test_3175_fetch_into_brings_the_objects_without_moving_a_branch(self):
        def git(*args, cwd):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                                  text=True).stdout.strip()

        with tempfile.TemporaryDirectory() as tmp:
            origin, clone = Path(tmp, "origin"), Path(tmp, "clone")
            origin.mkdir()
            git("init", "-q", "-b", "main", cwd=origin)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                "-m", "base", cwd=origin)
            git("clone", "-q", str(origin), str(clone), cwd=tmp)
            git("checkout", "-q", "-b", "integration/paquet", cwd=origin)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                "-m", "head", cwd=origin)
            head = git("rev-parse", "HEAD", cwd=origin)
            refs = git("for-each-ref", "refs/heads", cwd=clone)
            self.assertEqual(packet_automerge._fetch_into(
                str(clone), ("integration/paquet", "main")), "")
            self.assertEqual(git("cat-file", "-t", head, cwd=clone), "commit")
            self.assertEqual(git("for-each-ref", "refs/heads", cwd=clone), refs)
            self.assertIn("integration/absente", packet_automerge._fetch_into(
                str(clone), ("integration/absente",)))

    def test_3175_a_host_without_any_clone_says_so(self):
        for checkout, clone, same in (("", None, True), ("/nulle/part", None, True),
                                      ("/autre-depot", "/autre-depot", False)):
            with self.subTest(checkout=checkout):
                (ok, why, _project), gate, _root, _row, _ms = self.agents2(
                    checkout, clone=clone, same=same)
                self.assertFalse(ok)
                self.assertIn("no checkout of %s on this host" % SLUG, why)
                self.assertIn("packet_watch.projects.<p>.repo", why)
                gate.assert_not_called()

    def test_3175_the_pass_hands_its_clone_to_the_gate(self):
        seen = {}

        def verdicts(cfg, db, row, ms, **kw):
            seen.update(kw)
            return False, "stop", None

        with mock.patch.object(packet_automerge, "open_carriers", return_value=[LOT]), \
                mock.patch.object(packet_automerge, "_verdicts", verdicts), \
                mock.patch.object(packet_automerge, "marks", return_value=[{
                    "id": 1, "state": "armed", "repo": SLUG, "pr": NUM, "sha": HEAD,
                    "base": "main", "required": ("tests",), "why": ""}]), \
                mock.patch.object(packet_automerge, "_lock", return_value="bail"), \
                mock.patch.object(packet_automerge, "_unlock"), \
                mock.patch.object(packet_automerge, "_note_fenced"), \
                mock.patch.object(fire, "_fresh", return_value=(
                    {"id": LOT, "app": "ameesh"},
                    [{"id": 1, "kind": "frozen", "sha": HEAD, "at_ts": 1.0}])):
            rows = packet_automerge.watch(None, None, "ameesh", gh=FakeGh(tests="success"),
                                          checkout="/clone")
        self.assertEqual(rows[0]["result"], "pending", rows)
        self.assertEqual(seen.get("checkout"), "/clone")

    def test_an_unreadable_gate_refuses(self):
        with mock.patch.object(fire, "_repo_of", side_effect=RuntimeError("registre")):
            ok, why, project = packet_automerge._verdicts(None, None, {"id": LOT}, [],
                                                          slug=SLUG, base="main")
        self.assertFalse(ok)
        self.assertIsNone(project)
        self.assertIn("registre", why)


class HeldTest(Journal):
    """#3175 : sur #2409 (h-1500), armé à 13:47Z, le passage n'a laissé aucune
    trace jusqu'à la fusion manuelle du bureau ; un refus du passage est
    toujours consigné au lot, avec son motif, une fois par motif."""

    def check(self, gh, **kw):
        return packet_automerge.check(None, None, LOT, gh=gh, **kw)

    def holds(self) -> list[str]:
        return [n for n in self.notes() if n.startswith("auto-merge held ")]

    def test_a_refusal_of_the_pass_is_journaled_with_its_reason(self):
        self.armed()
        out = self.check(FakeGh(tests="pending"))
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(self.holds(), [
            "auto-merge held repo=%s pr=%d sha=%s base=main — %s" % (SLUG, NUM, HEAD,
                                                                     out["why"])])
        self.assertIsNotNone(self.active())

    def test_the_same_reason_is_journaled_once_a_new_one_again(self):
        self.armed()
        for _ in range(3):
            self.check(FakeGh(tests="pending"))
        self.assertEqual(len(self.holds()), 1, self.notes())
        self.gate = (False, "2 independent reviews missing on 1111111111", None)
        out = self.check(FakeGh(tests="pending"))
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(len(self.holds()), 2, self.notes())
        self.assertIn(" ".join(out["why"].split()), self.holds()[-1])
        self.gate = (True, "", None)
        self.check(FakeGh(tests="pending"))
        self.assertEqual(len(self.holds()), 3, self.notes())

    def test_a_new_arming_journals_its_refusal_again(self):
        self.armed()
        self.check(FakeGh(tests="pending"))
        self.armed()
        self.check(FakeGh(tests="pending"))
        self.assertEqual(len(self.holds()), 2, self.notes())

    def test_a_refused_review_gate_names_its_reason(self):
        self.armed()
        self.gate = (False, "2 independent reviews missing on 1111111111", None)
        self.check(FakeGh(tests="success"))
        self.assertTrue(self.holds()[-1].endswith(
            "— 2 independent reviews missing on 1111111111"), self.notes())

    def test_an_error_of_the_pass_is_journaled_as_such(self):
        self.armed()
        out = self.check(FakeGh(tests="success",
                                protection={"required_status_checks": None}))
        self.assertEqual(out["result"], "error", out)
        self.assertIn("— error: main no longer enforces", self.holds()[-1])

    def test_a_merge_a_dry_run_or_a_busy_lease_journal_no_refusal(self):
        self.armed()
        self.assertEqual(self.check(FakeGh(tests="pending"), dry_run=True)["result"],
                         "pending")
        self.assertEqual(self.holds(), [])
        self.held.add(LOT)
        self.assertEqual(self.check(FakeGh(tests="pending"))["result"], "busy")
        self.assertEqual(self.holds(), [])
        self.held.discard(LOT)
        self.assertEqual(self.check(FakeGh(tests="success"))["result"], "merged")
        self.assertEqual(self.holds(), [])

    def test_a_journal_failure_never_changes_the_outcome(self):
        self.armed()
        with mock.patch.object(packet_automerge, "_note_fenced",
                               side_effect=RuntimeError("base injoignable")):
            out = self.check(FakeGh(tests="pending"))
        self.assertEqual(out["result"], "pending", out)

    def test_a_held_mark_is_parsed_and_never_closes_the_arming(self):
        self.armed()
        self._note(None, LOT, "auto-merge held repo=%s pr=%d sha=%s base=main — a — b"
                              % (SLUG, NUM, HEAD))
        rows = packet_automerge.marks(None, LOT)
        self.assertEqual((rows[-1]["state"], rows[-1]["why"]), ("held", "a — b"))
        self.assertEqual(self.active()["state"], "armed")


class TickTest(unittest.TestCase):
    """Le passage `ameesh release tick` (`packet_watch.tick`) porte `watch`."""

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

    def test_a_waiting_carrier_says_nothing(self):
        out, watch = self.tick([{"lot": LOT, "result": "pending"},
                                {"lot": 9, "result": "idle"}])
        self.assertEqual([r["result"] for r in out], ["grace"])
        watch.assert_called_once_with(mock.ANY, None, "ameesh", gh="gh", dry_run=False,
                                      checkout=mock.ANY)

    def test_a_merge_a_disarm_and_an_error_are_reported(self):
        out, _watch = self.tick([{"lot": LOT, "result": "merged", "pr": NUM},
                                 {"lot": 8, "result": "disarmed", "pr": NUM - 1},
                                 {"lot": 9, "result": "error", "error": "HTTP 502"}])
        self.assertEqual([(r["result"], r.get("lot")) for r in out],
                         [("grace", None), ("auto-merge-merged", LOT),
                          ("auto-merge-disarmed", 8), ("error", 9)])
        self.assertIn("HTTP 502", out[3]["error"])

    def test_a_crash_of_the_watch_keeps_the_slot_result(self):
        out, _watch = self.tick(error=RuntimeError("boom"))
        self.assertEqual([r["result"] for r in out], ["grace", "error"])


class ArmingGh(LabelGh):
    """Le `gh` des étiquettes de `test_l161g` (`run(argv)` → tuple), plus la
    forge de la PR #1047 sur le gel."""

    def __init__(self, *, forge=None, **kw):
        super().__init__(**kw)
        self.forge = forge or Forge(head=GEL)

    def run(self, argv, stdin=None):
        path = argv[-1] if argv else ""
        if argv[:2] in (["pr", "view"], ["pr", "merge"]) or (
                argv[:3] == ["api", "-X", "POST"] and argv[3].endswith("/merges")) or (
                argv[:1] == ["api"] and (path.endswith("/protection")
                                         or "/rules/branches/" in path
                                         or "/git/ref/heads/" in path
                                         or "/check-runs" in path)):
            self.calls.append(list(argv))
            return self.forge.answer(argv)
        return super().run(argv, stdin)


class CiRunTest(GelTestCase):
    """`ameesh ci run <porteur> --full` : la publication arme la fusion."""

    def setUp(self) -> None:
        super().setUp()
        self.notes: list[str] = []
        self.seq = itertools.count(1)
        self.work.enter_context(mock.patch.object(work, "note", self._note))
        self.work.enter_context(mock.patch.object(packet_automerge, "_lock",
                                                  lambda db, lot: ("bail", lot)))
        self.work.enter_context(mock.patch.object(packet_automerge, "_unlock",
                                                  lambda h: None))
        self.work.enter_context(mock.patch.object(packet_automerge, "_renew",
                                                  lambda h: True))
        self.work.enter_context(mock.patch.object(packet_automerge, "_verdicts",
                                                  lambda *a, **k: (True, "", None)))
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

    def test_the_full_request_of_a_carrier_arms_its_merge(self):
        gh = ArmingGh()
        code, out = self.run_cmd(gh)
        self.assertEqual(code, 0, out)
        self.assertIn(LABEL_FULL, gh.poses())
        self.assertEqual(out["auto_merge"]["state"], "armed", out)
        self.assertEqual(out["auto_merge"]["merge"]["result"], "pending")
        self.assertEqual(gh.forge.auto_arms() + gh.forge.merges(), [])
        self.assertTrue(any(n.startswith("auto-merge armed repo=%s pr=%d sha=%s base=main"
                                         % (DEPOT, PR, GEL)) for n in self.notes), self.notes)
        # la publication suit la pose : l'étiquette part d'abord
        label = next(i for i, c in enumerate(gh.calls)
                     if c[:3] == ["api", "-X", "POST"] and "/labels" in c[3])
        view = next(i for i, c in enumerate(gh.calls) if c[:2] == ["pr", "view"])
        self.assertLess(label, view)

    def test_an_unprotected_target_still_requests_the_ci(self):
        gh = ArmingGh(forge=Forge(head=GEL, protection=None))
        code, out = self.run_cmd(gh)
        self.assertEqual(code, 0, out)
        self.assertIn(LABEL_FULL, gh.poses())
        self.assertEqual(out["auto_merge"]["state"], "unprotected")
        self.assertEqual(gh.forge.merges(), [])

    def test_a_dry_run_arms_nothing(self):
        gh = ArmingGh()
        code, out = self.run_cmd(gh, dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("auto_merge", out)
        self.assertEqual([c for c in gh.calls if c[:1] == ["pr"]], [])

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
            out = ci._arm_packet(None, None, decision, LabelGh(), args)
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

    def test_the_rendering_says_what_happened(self):
        base = self.decision(work_item=LOT, branch="integration/paquet-h-x",
                             why="full suite: porteur", label=LABEL_FULL)
        self.assertIn("  auto-merge NOT armed (unprotected): no check", ci._render_gel(
            dict(base, auto_merge={"state": "unprotected", "why": "no check"})))
        lines = ci._render_gel(dict(base, auto_merge={"state": "armed", "why": "pinned",
                                                      "merge": {"result": "merged"}}))
        self.assertIn("  auto-merge: armed — pinned", lines)
        self.assertIn("  auto-merge now: merged", lines)


class ParallelSecondTest(Journal):
    """#3256 C : le second paquet d'une paire fusionne après le premier.

    La cible avance par la fusion du premier porteur (commit de fusion dont le
    second parent est sa tête gelée) ; la fusion de la tête du second dans
    cette cible avancée est propre. Le second n'est ni rebasé ni retesté : ses
    contrôles de gel tournent sur l'arbre fusionné, puis ameesh fusionne sa
    tête publiée inchangée.
    """

    PEER = 5000

    def check(self, gh, **kw):
        return packet_automerge.check(None, None, LOT, gh=gh, **kw)

    def repo(self):
        """Un dépôt jetable : une base, puis les commits du banc."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = directory.name

        def git(*args):
            proc = subprocess.run(["git", "-C", path, *args], capture_output=True,
                                  text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return (proc.stdout or "").strip()

        git("init", "-q")
        git("config", "user.email", "test@ameesh.invalid")
        git("config", "user.name", "test")
        (Path(path) / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        return path, git, git("rev-parse", "HEAD")

    def setUp(self):
        super().setUp()
        self.heads = {LOT: HEAD, self.PEER: HEAD}
        self.closed: list[tuple] = []

        def fresh(db, lot):
            lot = int(lot)
            return ({"id": lot, "app": "ameesh", "assignee": "composeur",
                     "state": "qa",
                     "branch": "integration/paquet-h-20261020-%04d" % lot},
                    [{"id": 1, "kind": "frozen",
                      "sha": self.heads.get(lot, HEAD), "at_ts": 1.0}])
        patch = mock.patch.object(fire, "_fresh", fresh)
        patch.start()
        self.addCleanup(patch.stop)
        self.freeze_ok = ("ok", "")
        patch = mock.patch.object(packet_automerge, "_parallel_freeze_checks",
                                  lambda *a, **k: self.freeze_ok)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(work, "close",
                                  lambda db, lot, **kw: self.closed.append((lot, kw)))
        patch.start()
        self.addCleanup(patch.stop)
        self._note(None, LOT, "paquet-horaire: second paquet parallèle de #%d "
                              "groupe=#2834" % self.PEER)

    def pair(self):
        """Un dépôt : base, premier paquet fusionné, second paquet disjoint."""
        path, git, base = self.repo()
        git("checkout", "-q", "-b", "packet1", base)
        (Path(path) / "first.py").write_text("first = 1\n")
        git("add", "-A")
        git("commit", "-qm", "packet 1: first")
        first_head = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        git("merge", "-q", "--no-ff", "packet1", "-m",
            "Merge pull request #1 from manaty/integration/paquet-h-1")
        first_merge = git("rev-parse", "HEAD")
        git("checkout", "-q", "-b", "packet2", base)
        (Path(path) / "second.py").write_text("second = 2\n")
        git("add", "-A")
        git("commit", "-qm", "packet 2: second")
        second_head = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        self.heads = {LOT: second_head, self.PEER: first_head}
        self.frozen = second_head
        return path, git, base, first_head, first_merge, second_head

    def test_the_second_waits_while_the_first_is_not_merged(self):
        path, _git, base, first_head, _first_merge, second_head = self.pair()
        self.heads = {LOT: second_head, self.PEER: first_head}
        self.armed(sha=second_head, composition=base)
        gh = FakeGh(head=second_head, tests="success", base_oid=base)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("premier #%d n'est pas encore fusionné" % self.PEER, out["why"])
        self.assertEqual(gh.merges(), [])

    def test_the_second_merges_after_the_first_on_a_clean_rebase(self):
        path, _git, base, _first_head, first_merge, second_head = self.pair()
        self.armed(sha=second_head, composition=base)
        gh = FakeGh(head=second_head, tests="success", base_oid=first_merge)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", second_head)])
        proof = [note for note in self.notes()
                 if note.startswith("parallel-second proof ")]
        self.assertTrue(proof)
        self.assertIn("freeze-checks green", proof[0])

    def test_a_conflict_returns_the_second_to_the_queue(self):
        path, git, base, _first_head, first_merge, second_head = self.pair()
        # le second touche le fichier du premier : la fusion n'est pas propre
        git("checkout", "-q", "packet2")
        (Path(path) / "first.py").write_text("first = 2\n")
        git("add", "-A")
        git("commit", "-qm", "packet 2: same file")
        second_head = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        self.heads = {LOT: second_head, self.PEER: _first_head}
        self.frozen = second_head
        self.armed(sha=second_head, composition=base)
        gh = FakeGh(head=second_head, tests="success", base_oid=first_merge)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "closed", out)
        self.assertEqual(gh.merges(), [])
        self.assertEqual([lot for lot, _kw in self.closed], [LOT])
        self.assertIn("lots rendus à la file", self.closed[0][1]["note"])

    def test_an_inconclusive_freeze_check_neither_merges_nor_closes(self):
        path, _git, base, _first_head, first_merge, second_head = self.pair()
        self.freeze_ok = ("inconclusive", "freeze checks timed out")
        self.armed(sha=second_head, composition=base)
        gh = FakeGh(head=second_head, tests="success", base_oid=first_merge)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("non concluante", out["why"])
        self.assertEqual(gh.merges(), [])
        self.assertEqual(self.closed, [])

    def test_a_red_freeze_check_returns_the_second_to_the_queue(self):
        path, _git, base, _first_head, first_merge, second_head = self.pair()
        self.freeze_ok = ("red", "freeze-checks rc 1: FAIL test_x")
        self.armed(sha=second_head, composition=base)
        gh = FakeGh(head=second_head, tests="success", base_oid=first_merge)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "closed", out)
        self.assertEqual(gh.merges(), [])
        self.assertEqual([lot for lot, _kw in self.closed], [LOT])

    def test_a_code_commit_beyond_the_first_merge_disarms(self):
        path, git, base, _first_head, first_merge, second_head = self.pair()
        (Path(path) / "later.py").write_text("later = 1\n")
        git("add", "-A")
        git("commit", "-qm", "later code")
        moved = git("rev-parse", "HEAD")
        self.armed(sha=second_head, composition=base)
        gh = FakeGh(head=second_head, tests="success", base_oid=moved)
        out = self.check(gh, checkout=path)
        self.assertEqual(out["result"], "disarmed", out)
        self.assertEqual(gh.merges(), [])
        self.assertIn("target advanced since composition", self.notes()[-1])


if __name__ == "__main__":
    unittest.main()
