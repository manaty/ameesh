# SPDX-License-Identifier: AGPL-3.0-only
"""#8231 : une attente de désignation se complète seule quand la famille revient."""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import budget, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh.work_reviewers import designated

from . import test_2447_review_designation_activation as base
from .support import PgTestCase


class ResumeWaitingTest(base._ActivationFixture, PgTestCase):
    def pause(self, name):
        self.db.execute(
            "UPDATE agent_registry SET status = 'blocked', status_text = %s"
            " WHERE name = %s",
            (budget.PAUSE_PREFIX + "plan pace: codex +34 pt", name))

    def resume(self, name):
        self.db.execute("UPDATE agent_registry SET status = 'idle', status_text = ''"
                        " WHERE name = %s", (name,))

    def waiting_freeze(self):
        self.pause("r2")
        _, outcome = self.freeze()
        self.assertEqual(outcome.get("state"), rd.WAITING, outcome)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])

    def test_returning_family_completes_the_missing_seat_once(self):
        self.waiting_freeze()
        memo: dict = {}
        before = len(self.mailbox())
        idle = act.resume_waiting(self.cfg, self.db, memo=memo)
        self.assertEqual(idle["errors"], [], idle)
        self.assertEqual(idle["attempted"], 0, idle)  # codex toujours en pause
        self.resume("r2")
        done = act.resume_waiting(self.cfg, self.db, memo=memo)
        self.assertEqual(done["errors"], [], done)
        self.assertEqual(done["attempted"], 1, done)
        self.assertEqual(done["designated"], 1, done)
        self.assertEqual(sorted(designated(work.get(self.db, self.lot))), ["r1", "r2"])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["r2"])
        again = act.resume_waiting(self.cfg, self.db, memo=memo)
        self.assertEqual(again["attempted"], 0, again)  # plus d'attente
        self.assertEqual(len(self.mailbox()), before + 1)

    def test_same_situation_is_not_replayed_every_pass(self):
        self.waiting_freeze()
        self.resume("r2")
        memo: dict = {}
        calls = []

        def still_waiting(cfg, db, lot, freeze, **kwargs):
            calls.append(lot)
            return {"state": rd.WAITING, "reviewers": [], "why": "corrector"}

        original = act.designate_deferred
        act.designate_deferred = still_waiting
        try:
            act.resume_waiting(self.cfg, self.db, memo=memo)
            act.resume_waiting(self.cfg, self.db, memo=memo)
        finally:
            act.designate_deferred = original
        self.assertEqual(calls, [self.lot])

    def replay(self, outcomes, steps):
        """`steps` : un geste avant chaque passe ; rend les `attempted`."""
        memo: dict = {}
        calls = iter(outcomes)

        def deferred(cfg, db, lot, freeze, **kwargs):
            outcome = next(calls)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        attempted = []
        with mock.patch.object(act, "designate_deferred", side_effect=deferred):
            for step in steps:
                step()
                attempted.append(act.resume_waiting(self.cfg, self.db,
                                                    memo=memo)["attempted"])
        return attempted

    def test_candidate_leaving_then_returning_is_replayed(self):
        self.waiting_freeze()
        waiting = {"state": rd.WAITING, "reviewers": [], "why": "corrector"}
        attempted = self.replay([waiting, waiting], [
            lambda: self.resume("r2"), lambda: self.pause("r2"),
            lambda: self.resume("r2")])
        self.assertEqual(attempted, [1, 0, 1])

    def test_transient_error_does_not_freeze_the_memo(self):
        self.waiting_freeze()
        self.resume("r2")
        waiting = {"state": rd.WAITING, "reviewers": [], "why": "corrector"}
        attempted = self.replay([RuntimeError("git read"), waiting],
                                [lambda: None, lambda: None])
        self.assertEqual(attempted, [1, 1])

    def test_resumable_ignores_author_and_seated_families(self):
        payload = {"authors": [{"name": "a", "family": "deepseek"}],
                   "reviewers": [{"name": "r1", "family": "claude"}]}
        available = {"r1": "claude", "x": "deepseek", "r2": "codex", "u": ""}
        self.assertEqual(act._resumable(payload, available), ["r2"])


class RunnerCadenceTest(unittest.TestCase):
    def test_one_pass_per_interval_and_shared_memo(self):
        from ameesh import config, runner
        fake = types.SimpleNamespace(cfg=config.Config(), dry_run=False)
        report = {"results": [{"lot": 5, "result": "designated", "detail": ""}],
                  "errors": []}
        with mock.patch.object(act, "enabled", return_value=True), \
                mock.patch.object(act, "resume_waiting", return_value=report) as run, \
                mock.patch.object(runner, "fresh_radar", side_effect=lambda cfg: cfg), \
                mock.patch.object(runner, "log_async") as log:
            runner.Runner.resume_waiting_designations(fake, None)
            runner.Runner.resume_waiting_designations(fake, None)
        self.assertEqual(run.call_count, 1)
        self.assertIs(run.call_args.kwargs["memo"], fake._resume_waiting_memo)
        self.assertTrue(any("lot #5 designated" in c.args[0]
                            for c in log.call_args_list))


if __name__ == "__main__":
    unittest.main()
