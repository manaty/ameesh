# SPDX-License-Identifier: AGPL-3.0-only
"""#1070 : a budget pause no longer reads as any other `blocked`.

The bureau's tally counted "no agent in error/pause" while ten agents were in
a budget pause: in the `ameesh list` table the STATUS column is truncated to
eleven characters and "blocked/budget : pause (…" became "blocked/bud", the
word "pause" disappearing. The view now shows "paused/…" for a budget pause,
keeps "blocked/…" for a `blocked` of another cause (host not ready,
authentication), and every stable schema that publishes an agent's state
carries `paused_on_budget`: `list --json` (`ameesh-agent/1`), `show --json`,
`progress --json` (`ameesh-progress/1`) and `projects --json`
(`ameesh-projects/1`) — the predicate is `handoff.paused` (L233b), not a
second definition.

Witness: budget-blocked against non-budget-blocked — no false positive.
"""
from __future__ import annotations

import json

from ameesh import progress, registry

from .support import PgTestCase

BUDGET_TEXT = ("budget : pause (account secondaire, codex-300min 21 %) until "
               "20/10 06:57; all codex accounts at threshold")
OTHER_TEXT = "auth_blocked: codex/compte — log in again"


class PauseBudgetVisibleTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("budget-pause", "codex", cwd=self.tmp)
        self.register("other-blocked", "codex", cwd=self.tmp)
        registry.set_status(self.db, "budget-pause", "blocked", status_text=BUDGET_TEXT)
        registry.set_status(self.db, "other-blocked", "blocked", status_text=OTHER_TEXT)

    def _lines(self, output: str) -> dict:
        return {name: [l for l in output.splitlines() if l.startswith(name)][0]
                for name in ("budget-pause", "other-blocked")}

    def test_table_shows_budget_pause(self):
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = self._lines(proc.stdout)
        # budget-blocked: the word "pause" survives the truncation
        self.assertIn("paused", lines["budget-pause"])
        self.assertNotIn("blocked", lines["budget-pause"])
        # non-budget-blocked: no false positive
        self.assertIn("blocked", lines["other-blocked"])
        self.assertNotIn("paused", lines["other-blocked"])

    def test_json_distinguishes_budget_pause(self):
        proc = self.mesh("list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = {r["name"]: r for r in json.loads(proc.stdout)}
        self.assertTrue(rows["budget-pause"]["paused_on_budget"])
        self.assertFalse(rows["other-blocked"]["paused_on_budget"])
        # `state` alone does not separate them: a non-budget `blocked` is
        # "paused" too (L13) — hence the dedicated field.
        self.assertEqual(rows["budget-pause"]["state"], "paused")
        self.assertEqual(rows["other-blocked"]["state"], "paused")

    def test_show_json_carries_field(self):
        # #1070 N2: `show --json` carries the field too, as `list --json` does
        for name, expected in (("budget-pause", True), ("other-blocked", False)):
            proc = self.mesh("show", name, "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["paused_on_budget"], expected)

    def test_progress_and_projects_carry_field(self):
        # #1070 : the other stable schemas that publish an agent's state
        env = self.env(HOME=self.tmp)
        snap = self.mesh("progress", "--json", "--no-versions", "--no-gantt", env=env)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        agents = {a["name"]: a for a in json.loads(snap.stdout)["agents"]}
        self.assertTrue(agents["budget-pause"]["paused_on_budget"])
        self.assertFalse(agents["other-blocked"]["paused_on_budget"])

        proc = self.mesh("projects", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        view = json.loads(proc.stdout)
        agents = {a["name"]: a for p in view["projects"] for a in p["agents"]}
        self.assertTrue(agents["budget-pause"]["paused_on_budget"])
        self.assertFalse(agents["other-blocked"]["paused_on_budget"])

    def test_strict_predicate(self):
        self.assertTrue(progress.paused_on_budget(
            {"status": "blocked", "status_text": BUDGET_TEXT}))
        self.assertFalse(progress.paused_on_budget(
            {"status": "blocked", "status_text": OTHER_TEXT}))
        self.assertFalse(progress.paused_on_budget(
            {"status": "blocked", "status_text": None}))
        # the prefix alone is not enough: it is the ANNOUNCED pause (blocked)
        self.assertFalse(progress.paused_on_budget(
            {"status": "running", "status_text": BUDGET_TEXT}))
        self.assertFalse(progress.paused_on_budget(
            {"status": "stopped", "status_text": BUDGET_TEXT}))
