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

#: #2451 : familles explicites ; l'activation passe celles du canon compilé
FAMILIES = ("claude", "codex", "deepseek")
from ameesh import review_pace

NOW = 1_800_000_000.0
WEEK = 7 * 86400.0
SHA = "a" * 40


def row(name, *, team="ameesh", harness="deepseek", session="", status="idle",
        mode="execute", text="", model="", capabilities=("read", "report-drift", "propose"),
        roles=("implementation",)):
    """#4779 : une ligne de relecteur porte sa capacité `propose` et un rôle."""
    return {"name": name, "team": team, "harness": harness, "session_harness": session,
            "status": status, "mode": mode, "status_text": text, "model": model,
            "capabilities": list(capabilities), "roles": list(roles)}


def candidates(*rows, loads=None):
    return rd.candidates_from_registry(rows, project="ameesh", loads=loads,
                                     families=FAMILIES)


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
        self.assertAlmostEqual(advances["codex"][""], 0.16, places=6)
        self.assertAlmostEqual(advances["claude"][""], 0.0, places=6)

    def test_a_five_hour_burst_does_not_override_the_weekly_plan(self):
        # #2675: the plan advance is read on the longest window; a 5 h burst
        # is a capacity signal, not the weekly gap the designation balances.
        # Codex's weekly plan is 20 points behind while its 5 h window is 85
        # points ahead: the former max() read +0.85 and flipped the family.
        advances = review_pace.family_advances(
            [gauge("codex", "secondaire", 0.20, 0.40),
             gauge("codex", "secondaire", 0.95, 0.10, key="five_hour",
                   window=5 * 3600),
             gauge("claude", "secondaire", 0.60, 0.60)],
            now=NOW)
        self.assertAlmostEqual(advances["codex"][""], -0.20, places=6)
        self.assertAlmostEqual(advances["claude"][""], 0.0, places=6)

    def test_a_model_scoped_bucket_does_not_contaminate_another_model(self):
        # cx3 B/P1: seven_day@fable at 95 % is a Fable-only bucket; it must
        # not make Claude look ahead for a reviewer of another model. The
        # general reading stays 0, the Fable bucket is 35 points ahead.
        rows = [gauge("claude", "secondaire", 0.60, 0.60),
                gauge("claude", "secondaire", 0.95, 0.60, key="seven_day@fable"),
                gauge("codex", "secondaire", 0.35, 0.19)]
        advances = review_pace.family_advances(rows, now=NOW)
        self.assertAlmostEqual(advances["claude"][""], 0.0, places=6)
        self.assertAlmostEqual(advances["claude"]["fable"], 0.35, places=6)
        self.assertAlmostEqual(advances["codex"][""], 0.16, places=6)
        picked = rd.select(families=FAMILIES,
            required=1,
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=candidates(row("claude-opus", harness="claude", session="claude",
                                      model="claude-opus-5-5"),
                                  row("codex-one", harness="codex", session="codex")),
            advances=advances)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-opus"])

    def test_a_model_scoped_bucket_counts_for_its_model(self):
        rows = [gauge("claude", "secondaire", 0.60, 0.60),
                gauge("claude", "secondaire", 0.95, 0.60, key="seven_day@fable"),
                gauge("codex", "secondaire", 0.35, 0.19)]
        advances = review_pace.family_advances(rows, now=NOW)
        picked = rd.select(families=FAMILIES,
            required=1,
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=candidates(row("claude-fable", harness="claude", session="claude",
                                      model="fable"),
                                  row("codex-one", harness="codex", session="codex")),
            advances=advances)
        # the Fable bucket binds Claude (35 points ahead): Codex is behind
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-one"])

    def test_a_scoped_only_family_is_neutral_for_another_model(self):
        scoped = gauge("claude", "secondaire", 0.95, 0.60, key="seven_day@fable")
        advances = review_pace.family_advances(
            [scoped, gauge("codex", "secondaire", 0.35, 0.19)], now=NOW)
        self.assertEqual(set(advances["claude"]), {"fable"})
        picked = rd.select(families=FAMILIES,
            required=1,
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=candidates(row("claude-opus", harness="claude", session="claude",
                                      model="claude-opus-5-5"),
                                  row("codex-one", harness="codex", session="codex")),
            advances=advances)
        # Claude has no general reading: neutral 0, ahead of Codex's +16
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-opus"])

    def test_a_stale_model_scope_is_neutral_not_the_account_general(self):
        # cx3 B/P1: the expired @fable reading makes the Fable candidate
        # neutral; the account's general -0.30 must not be re-injected.
        expired = gauge("claude", "secondaire", 0.95, 1.01, key="seven_day@fable")
        rows = [gauge("claude", "secondaire", 0.30, 0.60), expired,
                gauge("codex", "secondaire", 0.20, 0.30)]
        advances = review_pace.family_advances(rows, now=NOW)
        self.assertAlmostEqual(advances["claude"][""], -0.30, places=6)
        self.assertIsNone(advances["claude"]["fable"])
        self.assertAlmostEqual(advances["codex"][""], -0.10, places=6)
        fable = rd.select(families=FAMILIES,
            required=1,
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=candidates(row("claude-fable", harness="claude", session="claude",
                                      model="fable"),
                                  row("codex-one", harness="codex", session="codex")),
            advances=advances)
        # the Fable candidate is neutral (0), Codex is behind (-10): Codex
        self.assertEqual([r["name"] for r in fable["reviewers"]], ["codex-one"])
        opus = rd.select(families=FAMILIES,
            required=1,
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=candidates(row("claude-opus", harness="claude", session="claude",
                                      model="claude-opus-5-5"),
                                  row("codex-one", harness="codex", session="codex")),
            advances=advances)
        # the Opus candidate does not match the stale scope: general -30 wins
        self.assertEqual([r["name"] for r in opus["reviewers"]], ["claude-opus"])

    def test_the_reserve_of_the_critical_agents_is_deducted(self):
        # #2648: a non-critical reviewer stops 10 points below the pace. At
        # 19 % elapsed the allowed pace is 19 % (the margin is 10, the
        # reserve 10), not 29 %: Codex is 16 points ahead, as the lot says.
        advances = review_pace.family_advances(
            [gauge("codex", "secondaire", 0.35, 0.19)], now=NOW)
        self.assertAlmostEqual(advances["codex"][""], 0.16, places=6)

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
        self.assertAlmostEqual(advances["codex"][""], 0.16, places=6)
        stale = gauge("codex", "tertiaire", 0.05, 0.05)
        stale["observed_ts"] = NOW - 8 * 86400.0
        stale["produced_ts"] = NOW - 8 * 86400.0
        advances = review_pace.family_advances(
            [gauge("codex", "secondaire", 0.35, 0.19), stale], now=NOW)
        self.assertAlmostEqual(advances["codex"][""], 0.16, places=6)

    def test_anonymous_readings_are_dropped_when_named_accounts_exist(self):
        # the default account is already counted by its named row
        advances = review_pace.family_advances(
            [{"harness": "claude", "account": None, "key": "seven_day",
              "used": 0.0, "resets_at_ts": NOW - 60.0, "window_s": WEEK,
              "observed_ts": NOW, "produced_ts": NOW, "source": "test"},
             gauge("claude", "secondaire", 0.60, 0.60)], now=NOW)
        self.assertAlmostEqual(advances["claude"][""], 0.0, places=6)


class SelectionPaceTest(unittest.TestCase):
    """The selection: pace before load, two families, neutral fallback."""

    def setUp(self):
        self.authors = [{"name": "author", "family": "deepseek"}]
        self.advances = {"claude": 0.0, "codex": 0.16}

    def test_one_review_prefers_claude_even_when_codex_is_less_loaded(self):
        picked = rd.select(families=FAMILIES,
            required=1, authors=self.authors, advances=self.advances,
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 5, "codex-one": 0}))
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])
        self.assertIn("plan pace", picked["why"])

    def test_the_family_reduction_compares_the_applicable_pace_before_load(self):
        # cx3 B/P1: Opus (load 2, 0 pt) is the Claude representative, not the
        # less-loaded Fable agent (35 pt ahead); without the fix the Fable
        # agent wins the family then loses to Codex, designating Codex.
        picked = rd.select(families=FAMILIES,
            required=1, authors=self.authors,
            advances={"claude": {"": 0.0, "fable": 0.35},
                      "codex": {"": 0.16}},
            candidates=candidates(
                row("claude-opus", harness="claude", session="claude",
                    model="claude-opus-5-5"),
                row("claude-fable", harness="claude", session="claude", model="fable"),
                row("codex-one", harness="codex", session="codex"),
                loads={"claude-opus": 2, "claude-fable": 0, "codex-one": 0}))
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-opus"])

    def test_two_reviews_take_claude_and_codex_whatever_the_pace(self):
        # a sensitive lot (the merge gate, the review itself): two distinct
        # families, even when one is far behind and the other far ahead
        picked = rd.select(families=FAMILIES,
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
        picked = rd.select(families=FAMILIES,
            required=1, authors=self.authors, advances={},
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 5, "codex-one": 0}))
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-one"])
        self.assertNotIn("plan pace", picked["why"])

    def test_a_stale_family_is_neutral_and_the_load_decides(self):
        picked = rd.select(families=FAMILIES,
            required=1, authors=self.authors, advances={"codex": 0.16},
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex"),
                                  loads={"claude-one": 0, "codex-one": 9}))
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])

    def test_a_negative_advance_is_behind_and_comes_first(self):
        picked = rd.select(families=FAMILIES,
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
               authors, policy_ref="", notify="", messages=(), unknown_authors=()):
        out = {"state": state, "required": required,
               "reviewers": [dict(r) for r in reviewers], "missing": missing,
               "why": why, "freeze": dict(freeze), "authors": [dict(a) for a in authors],
               "unknown_authors": [str(value) for value in unknown_authors],
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
        return rd.apply({"id": 2668, "title": "pace", "app": "ameesh"},
                        **dict({"families": FAMILIES}, **args))

    def test_apply_uses_the_advances(self):
        out = self.apply(advances={"claude": 0.0, "codex": 0.16})
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["claude-one"])

    def test_apply_without_advances_keeps_the_load_rule(self):
        out = self.apply()
        self.assertEqual([r["name"] for r in out["reviewers"]], ["codex-one"])


if __name__ == "__main__":
    unittest.main()
