# SPDX-License-Identifier: AGPL-3.0-only
"""#3308 — la cible résolue ne dépend pas du champ `baseRefOid` de gh.

gh 2.45 (le poste h-05) n'expose pas `baseRefOid` dans `gh pr view --json` :
la liste demandée faisait échouer toute la lecture et l'armement de la fusion
automatique du paquet h-05 (#2483). La branche se lit par `baseRefName`, puis
se résout par l'API `git/ref/heads/<base>`, disponible sur toutes les
versions. Un ref illisible refuse l'armement : aucune cible n'est devinée.

Témoins sans réseau ni base : le faux `gh` de #2813 pour l'armement, plus un
`gh 2.45` qui refuse `baseRefOid` comme le vrai.
"""
from __future__ import annotations

import json
import subprocess
import unittest

from ameesh import fire, land, merge_pr, packet_automerge

from . import test_2813_packet_auto_merge as banc

SLUG = banc.SLUG
NUM = banc.NUM
HEAD = banc.HEAD
#: une cible résolue distincte de la tête (la preuve #3091 la transporte)
BASE = banc.NEW


def _proc(args, code=0, out="", err=""):
    return subprocess.CompletedProcess(["gh", *args], code, out, err)


class Gh245(banc.FakeGh):
    """gh 2.45 : `pr view --json` refuse `baseRefOid` ; `gh api` lit la ref."""

    def answer(self, args):
        if args[:2] == ["pr", "view"]:
            fields = args[args.index("--json") + 1].split(",")
            if "baseRefOid" in fields:
                self.calls.append(list(args))
                return 1, "", 'Unknown JSON field: "baseRefOid"'
        return super().answer(args)


class SlashedBaseGh(Gh245):
    """Une cible à slash n'existe que sous sa forme encodée : le slash brut
    répond 404, comme GitHub (#3314)."""

    def answer(self, args):
        if args[:1] == ["api"] and "/git/ref/heads/" in args[-1]:
            self.calls.append(list(args))
            if args[-1].endswith("/git/ref/heads/release%2F1.2.0"):
                return 0, json.dumps({"ref": "refs/heads/release/1.2.0",
                                      "object": {"type": "commit", "sha": BASE}}), ""
            return 1, "", "gh: Not Found (HTTP 404)"
        return super().answer(args)


class BaseRefArmTest(banc.Journal):
    """L'armement du paquet passe sur un gh qui n'a pas `baseRefOid`."""

    def test_pr_status_reads_the_base_without_the_unsupported_field(self):
        gh = Gh245(base_oid=BASE)
        status = packet_automerge.pr_status(SLUG, NUM, gh=gh)
        self.assertEqual(status["base"], "main")
        self.assertEqual(status["base_oid"], BASE)
        fields = [call[call.index("--json") + 1]
                  for call in gh.calls if "--json" in call]
        self.assertTrue(fields)
        self.assertNotIn("baseRefOid", [name for field in fields
                                        for name in field.split(",")])
        self.assertTrue(any(call[:1] == ["api"]
                            and "/git/ref/heads/main" in call[1]
                            for call in gh.calls), gh.calls)

    def test_gh_2_45_arms_and_merges_the_published_head(self):
        gh = Gh245(tests="success", base_oid=BASE)
        out = self.arm(gh)
        self.assertEqual(out["state"], "armed", out)
        self.assertEqual(out["merge"]["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])
        self.assertTrue(self.notes()[-1].startswith("auto-merge merged"), self.notes())

    def test_an_unreadable_base_ref_arms_nothing(self):
        gh = Gh245(ref_error="HTTP 502")
        out = self.arm(gh)
        self.assertEqual(out["state"], "error", out)
        self.assertIn("git/ref/heads/main", out["why"])
        self.assertEqual(gh.landed, [])
        self.assertIsNone(self.active())
        self.assertTrue(self.notes()[-1].startswith("auto-merge not-armed"), self.notes())

    def test_a_pr_without_a_base_never_guesses_the_target(self):
        gh = Gh245(base="")
        out = self.arm(gh)
        self.assertEqual(out["state"], "error", out)
        self.assertIn("no base branch", out["why"])
        self.assertEqual(gh.landed, [])
        self.assertIsNone(self.active())

    def test_a_non_commit_answer_is_refused(self):
        calls = []

        def gh(args, timeout=60.0):
            calls.append(list(args))
            return _proc(args, 0, json.dumps({"object": {"sha": "not-a-commit"}}))

        with self.assertRaises(land.LandError):
            land.base_oid(SLUG, "main", gh=gh)
        self.assertEqual(calls, [["api", "repos/%s/git/ref/heads/main" % SLUG]])

    def test_a_slashed_base_is_encoded_in_the_rest_path(self):
        calls = []

        def gh(args, timeout=60.0):
            calls.append(list(args))
            return _proc(args, 0, json.dumps({"object": {"sha": HEAD,
                                                         "type": "commit"}}))

        self.assertEqual(land.base_oid(SLUG, "release/1.2.0", gh=gh), HEAD)
        self.assertEqual(calls, [["api", "repos/%s/git/ref/heads/release%%2F1.2.0"
                                  % SLUG]])

    def test_a_slashed_packet_target_resolves_through_the_encoded_ref(self):
        # #3314 : la cible d'un paquet peut porter un slash (`release/…`) ; le
        # consommateur réel (pr_status → land.base_oid) lit la forme encodée,
        # là où le slash brut, que ce faux `gh` refuse, ferait 404.
        gh = SlashedBaseGh(base="release/1.2.0")
        status = packet_automerge.pr_status(SLUG, NUM, gh=gh)
        self.assertEqual(status["base"], "release/1.2.0")
        self.assertEqual(status["base_oid"], BASE)
        self.assertEqual([call for call in gh.calls if call[:1] == ["api"]],
                         [["api", "repos/%s/git/ref/heads/release%%2F1.2.0"
                           % SLUG]])


VIEW = {"number": NUM, "state": "OPEN", "isDraft": False, "baseRefName": "main",
        "headRefOid": HEAD, "headRefName": "integration/paquet-h-3308",
        "mergeStateStatus": "CLEAN", "mergeCommit": None,
        "statusCheckRollup": [], "url": "https://example.invalid/3308",
        "title": "paquet", "autoMergeRequest": None, "isCrossRepository": False}


class Readers:
    """Un `gh` qui enregistre les champs `--json` demandés par les lecteurs."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, timeout=60.0):
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            return _proc(args, 0, json.dumps(VIEW))
        if args[:2] == ["pr", "list"]:
            return _proc(args, 0, json.dumps([VIEW]))
        if args[:1] == ["api"]:
            return _proc(args, 0, json.dumps({"object": {"type": "commit",
                                                         "sha": HEAD}}))
        return _proc(args, 0, "{}")


class OtherReadersTest(unittest.TestCase):
    """`merge_pr` et `fire` ne demandent pas non plus le champ absent."""

    def test_merge_pr_and_fire_do_not_request_baseRefOid(self):
        gh = Readers()
        merge_pr.pr_view(SLUG, NUM, gh=gh)
        fire._pr_status(SLUG, "integration/paquet-h-3308", gh=gh)
        fields = [call[call.index("--json") + 1]
                  for call in gh.calls if "--json" in call]
        self.assertTrue(fields, gh.calls)
        self.assertNotIn("baseRefOid", [name for field in fields
                                        for name in field.split(",")])


if __name__ == "__main__":
    unittest.main()
