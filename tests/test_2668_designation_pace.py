# SPDX-License-Identifier: AGPL-3.0-only
"""#2668 — the designation prefers the family furthest behind its plan pace.

Witnesses of the lot: with a DeepSeek author and one review, Codex 16 points
ahead and Claude at its pace designates Claude, even when Codex's agents are
less loaded; two required reviews still take Claude and Codex whatever the
pace; unreadable gauges keep the load rule. A stale reading (#1561) is
neutral, and the pace never removes an eligible family. The scale is raw:
nominal multiplier and default ceiling, so declared ×10 profiles cannot invert
the comparison (B of mesh-design on 4123b99).
"""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd
from ameesh import review_pace

NOW = 1_800_000_000.0
WEEK = 7 * 86400.0
SHA = "a" * 40


def row(name, *, team="ameesh", harness="deepseek", session="", status="idle",
        mode="execute", text=""):
    return {"name": name, "team": team, "harness": harness, "session_harness": session,
            "status": status, "mode": mode, "status_text": text}


def candidates(*rows, loads=None):
    return rd.candidates_from_registry(rows, project="ameesh", loads=loads)


def gauge(harness, account, used, elapsed, *, key="seven_day", window=WEEK,
          observed=NOW, produced=NOW):
    """One history row at `elapsed` of its window (0 = just reset, 1 = due)."""
    return {"harness": harness, "account": account, "key": key, "used": used,
            "resets_at_ts": NOW + window * (1.0 - elapsed), "window_s": window,
            "observed_ts": observed, "produced_ts": produced, "source": "test"}


class FamilyAdvanceTest(unittest.TestCase):
    """The reading itself: pace, reserve, stale, one family, several accounts."""

    def test_codex_sixteen_points_ahead_and_claude_at_its_pace(self):
        advances = review_pace.family_advances(
            [gauge("codex", "secondaire", 0.35, 0.19),
             gauge("claude", "secondaire", 0.60, 0.60)],
            now=NOW)
        self.assertAlmostEqual(advances["codex"], 0.16, places=6)
        self.assertAlmostEqual(advances["claude"], 0.0, places=6)

    def test_a_model_scoped_bucket_does_not_contaminate_the_family(self):
        # cx3 B/P1: seven_day@fable at 95 % is a Fable-only bucket; it must
        # not make Claude look ahead for a reviewer of another model.
        rows = [gauge("claude", "secondaire", 0.60, 0.60),
                gauge("claude", "secondaire", 0.95, 0.60, key="seven_day@fable"),
                gauge("codex", "secondaire", 0.35, 0.19)]
        advances = review_pace.family_advances(rows, now=NOW)
        self.assertAlmostEqual(advances["claude"], 0.0, places=6)
        self.assertAlmostEqual(advances["codex"], 0.16, places=6)
        picked = rd.select(
            required=1,
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex")),
            advances=advances)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])

    def test_a_scoped_only_family_is_neutral(self):
        scoped = gauge("claude", "secondaire", 0.95, 0.60, key="seven_day@fable")
        self.assertEqual(review_pace.family_advances([scoped], now=NOW), {})

    def test_the_reserve_of_the_critical_agents_is_deducted(self):
        # #2648: a non-critical reviewer stops 10 points below the pace. At
        # 19 % elapsed the allowed pace is 19 % (the margin is 10, the
        # reserve 10), not 29 %: Codex is 16 points ahead, as the lot says.
        advances = review_pace.family_advances(
            [gauge("codex", "secondaire", 0.35, 0.19)], now=NOW)
        self.assertAlmostEqual(advances["codex"], 0.16, places=6)

    def test_a_stale_reading_is_neutral(self):
        stale_claude = [
            gauge("claude", "secondaire", 0.30, 0.90, key="five_hour", window=5 * 3600),
            gauge("claude", "secondaire", 0.60, 0.60),
        ]
        # a dated window has reset while the other is still open: #1561
        stale_claude[0]["resets_at_ts"] = NOW - 60.0
        advances = review_pace.family_advances(
            stale_claude + [gauge("codex", "secondaire", 0.35, 0.19)], now=NOW)
        self.assertNotIn("claude", advances)
        self.assertIn("codex", advances)

    def test_an_absent_reading_is_not_a_measured_zero(self):
        self.assertEqual(review_pace.family_advances([], now=NOW), {})

    def test_the_family_reads_its_most_constrained_current_account(self):
        # a fresh sibling does not hide the plan that is ahead; a stale
        # sibling is ignored, never counted as a fresh account
        advances = review_pace.family_advances(
            [gauge("codex", "primaire", 0.30, 0.60),
             gauge("codex", "secondaire", 0.35, 0.19)], now=NOW)
        self.assertAlmostEqual(advances["codex"], 0.16, places=6)
        stale = gauge("codex", "tertiaire", 0.05, 0.05)
        stale["observed_ts"] = NOW - 8 * 86400.0
        stale["produced_ts"] = NOW - 8 * 86400.0
        advances = review_pace.family_advances(
            [gauge("codex", "secondaire", 0.35, 0.19), stale], now=NOW)
        self.assertAlmostEqual(advances["codex"], 0.16, places=6)

    def test_anonymous_readings_are_dropped_when_named_accounts_exist(self):
        # the default account is already counted by its named row
        advances = review_pace.family_advances(
            [{"harness": "claude", "account": None, "key": "seven_day",
              "used": 0.0, "resets_at_ts": NOW - 60.0, "window_s": WEEK,
              "observed_ts": NOW, "produced_ts": NOW, "source": "test"},
             gauge("claude", "secondaire", 0.60, 0.60)], now=NOW)
        self.assertAlmostEqual(advances["claude"], 0.0, places=6)


class SelectionPaceTest(unittest.TestCase):
    """The selection: pace before load, two families, neutral fallback."""

    def setUp(self):
        self.authors = [{"name": "author", "family": "deepseek"}]
        self.advances = {"claude": 0.0, "codex": 0.16}

    def test_one_review_prefers_claude_even_when_codex_is_less_loaded(self):
        picked = rd.select(
            required=1, authors=self.authors, advances=self.advances,
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 5, "codex-one": 0}))
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])
        self.assertIn("plan pace", picked["why"])

    def test_two_reviews_take_claude_and_codex_whatever_the_pace(self):
        # a sensitive lot (the merge gate, the review itself): two distinct
        # families, even when one is far behind and the other far ahead
        picked = rd.select(
            required=2, authors=self.authors,
            advances={"claude": -0.50, "codex": 0.80},
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 0, "codex-one": 9}))
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["family"] for r in picked["reviewers"]],
                         ["claude", "codex"])
        self.assertEqual(len({r["family"] for r in picked["reviewers"]}), 2)

    def test_unreadable_gauges_keep_the_load_rule(self):
        picked = rd.select(
            required=1, authors=self.authors, advances={},
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 5, "codex-one": 0}))
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-one"])
        self.assertNotIn("plan pace", picked["why"])

    def test_a_stale_family_is_neutral_and_the_load_decides(self):
        picked = rd.select(
            required=1, authors=self.authors, advances={"codex": 0.16},
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 0, "codex-one": 9}))
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])

    def test_a_negative_advance_is_behind_and_comes_first(self):
        picked = rd.select(
            required=1, authors=self.authors,
            advances={"claude": 0.10, "codex": -0.20},
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 0, "codex-one": 9}))
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-one"])


class _Port:
    """The minimal Port of `apply`: one freeze, one write."""

    def __init__(self):
        self.commits = []

    def latest_freeze(self, lot):
        return {"id": 1, "sha": SHA}

    def recorded(self, lot, freeze_id):
        return None

    def commit(self, lot, *, freeze, required, state, reviewers, missing, why,
               authors, policy_ref="", notify="", messages=()):
        out = {"state": state, "required": required,
               "reviewers": [dict(r) for r in reviewers], "missing": missing,
               "why": why, "freeze": dict(freeze), "authors": [dict(a) for a in authors],
               "messages": [dict(m) for m in messages]}
        self.commits.append(out)
        return out


class ApplyPaceTest(unittest.TestCase):
    """`apply` hands the family advances to the selection."""

    def apply(self, **kw):
        args = dict(
            policy={"rules": [{"paths": ["src/ameesh/work.py"], "reviews": 1}]},
            paths=["src/ameesh/work.py"], port=_Port(), freeze_sha=SHA,
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 5, "codex-one": 0}),
            risk="sensitive")
        args.update(kw)
        return rd.apply({"id": 2668, "title": "pace", "app": "ameesh"}, **args)

    def test_apply_uses_the_advances(self):
        out = self.apply(advances={"claude": 0.0, "codex": 0.16})
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["claude-one"])

    def test_apply_without_advances_keeps_the_load_rule(self):
        out = self.apply()
        self.assertEqual([r["name"] for r in out["reviewers"]], ["codex-one"])


if __name__ == "__main__":
    unittest.main()
