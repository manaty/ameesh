# SPDX-License-Identifier: AGPL-3.0-only
"""#1266: unreadable processes are compared with the whole turn interval."""
from __future__ import annotations

import unittest
from unittest import mock

from . import support  # isolate the host environment before importing the runner
from ameesh import containers, platform, resources, storage, turn_processes
from ameesh import runner as runner_mod
from ameesh.runner import Runner


class UnreadableTurnWindowTest(unittest.TestCase):
    START = 1_700_000_000.0
    END = START + 1000.0
    SLACK = runner_mod.LIVE_REVIEW_START_SLACK_S

    def setUp(self):
        self.row = {"id": 1, "turn_id": "closed-turn", "agent": "a1",
                    "pgid": 123, "status": "orphan", "containers": [],
                    "started_ts": self.START, "ended_ts": self.END}
        self.runner = Runner.__new__(Runner)
        self.runner.host, self.runner.cfg, self.runner.db = "test-host", mock.Mock(), mock.Mock()
        self.runner._live_review_after = 0
        self.runner._boot_settle_ready = mock.Mock(return_value=True)
        self.runner._running_containers = mock.Mock(return_value={})
        self.turns = mock.Mock()
        self.turns.to_review.return_value = [self.row]
        self.turns.settle_unchanged.side_effect = lambda rows: [r["turn_id"] for r in rows]

    def review(self, unread, *, groups=(), values=None, silent=(), engine=None):
        engines = [engine] if engine is not None else []
        with mock.patch.object(resources, "boot_time", return_value=self.START - 86400), \
                mock.patch.object(storage, "of", return_value=mock.Mock(turn_resources=self.turns)), \
                mock.patch.object(turn_processes, "running_groups", return_value=groups), \
                mock.patch.object(platform, "env_values",
                                  return_value={"values": values or {}, "unread": unread}), \
                mock.patch.object(containers, "all_runtimes", return_value=(engines, list(silent))), \
                mock.patch.object(runner_mod, "log"):
            return [r["turn_id"] for r in self.runner.settle_dead_turns()]

    def test_process_after_end_can_no_longer_hold_an_empty_turn(self):
        self.assertEqual(self.review([self.END + self.SLACK + 1]), ["closed-turn"])
        self.turns.settle_unchanged.assert_called_once_with([self.row])

    def test_later_process_does_not_hide_a_possible_survivor(self):
        inside, after = self.START + 500, self.END + self.SLACK + 1
        for unread in ([inside, after], [after, inside]):
            with self.subTest(unread=unread):
                self.assertEqual(self.review(unread), [])
        self.turns.settle_unchanged.assert_not_called()

    def test_inclusive_margins_keep_possible_survivors(self):
        for stamp in (self.START - self.SLACK, self.START, self.START + 500,
                      self.END, self.END + self.SLACK):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.review([stamp]), [])

    def test_processes_outside_both_margins_allow_settlement(self):
        self.assertEqual(self.review([self.START - self.SLACK - 1,
                                      self.END + self.SLACK + 1]), ["closed-turn"])

    def test_unknown_process_start_keeps_the_turn_even_with_a_later_process(self):
        for unknown in (None, float("nan"), float("inf"), -float("inf")):
            with self.subTest(unknown=unknown):
                self.assertEqual(self.review([unknown, self.END + self.SLACK + 1]), [])
        self.turns.settle_unchanged.assert_not_called()

    def test_unknown_turn_start_keeps_the_turn(self):
        for unknown in (None, float("nan"), float("inf"), -float("inf")):
            with self.subTest(unknown=unknown):
                self.row["started_ts"] = unknown
                self.assertEqual(self.review([self.END + self.SLACK + 1]), [])

    def test_invalid_end_keeps_the_turn(self):
        for invalid in (float("nan"), float("inf"), -float("inf"), self.START - 1):
            with self.subTest(invalid=invalid):
                self.row["ended_ts"] = invalid
                self.assertEqual(self.review([self.END + self.SLACK + 1]), [])

    def test_no_end_preserves_the_old_conservative_rule(self):
        for status in ("running", "orphan"):
            with self.subTest(status=status):
                self.row.update(status=status, ended_ts=None)
                self.assertEqual(self.review([self.END + self.SLACK + 1]), [])
                self.assertEqual(self.review([self.START - self.SLACK]), [])
                self.assertEqual(self.review([None]), [])
                self.assertEqual(self.review([self.START - self.SLACK - 1]), ["closed-turn"])

    def test_undated_turn_without_unreadable_processes_can_still_settle(self):
        self.row.update(started_ts=None, ended_ts=None, status="running")
        self.assertEqual(self.review([]), ["closed-turn"])

    def test_a_live_or_reused_group_still_keeps_the_turn(self):
        self.assertEqual(self.review([self.END + self.SLACK + 1], groups={123}), [])
        self.assertEqual(self.review([self.END + self.SLACK + 1], groups=None), [])
        self.turns.settle_unchanged.assert_not_called()

    def test_readable_survivor_still_keeps_the_turn(self):
        self.assertEqual(self.review([self.END + self.SLACK + 1],
                                     values={"closed-turn": [456]}), [])
        self.turns.settle_unchanged.assert_not_called()

    def test_container_or_silent_engine_still_keeps_the_turn(self):
        engine = mock.Mock(binary="docker", context="")
        after = [self.END + self.SLACK + 1]
        self.runner._running_containers.return_value = {"closed-turn": ["c1"]}
        self.assertEqual(self.review(after, engine=engine), [])
        self.runner._running_containers.return_value = None
        self.assertEqual(self.review(after, engine=engine), [])
        self.assertEqual(self.review(after, silent=["docker"]), [])
        self.row["containers"] = ["c1"]
        self.assertEqual(self.review(after), [])  # recorded container, no engine

    def test_changed_row_is_not_reported_settled(self):
        self.turns.settle_unchanged.side_effect = lambda rows: []
        self.assertEqual(self.review([self.END + self.SLACK + 1]), [])
        self.turns.settle_unchanged.assert_called_once_with([self.row])

    def test_each_turn_has_its_own_window(self):
        later = dict(self.row, id=2, turn_id="later-turn", started_ts=self.END + 1000,
                     ended_ts=self.END + 2000)
        self.turns.to_review.return_value = [self.row, later]
        self.assertEqual(self.review([self.END + 1500]), ["closed-turn"])
        self.turns.settle_unchanged.assert_called_once_with([self.row])
