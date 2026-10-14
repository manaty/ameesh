# SPDX-License-Identifier: AGPL-3.0-only
"""#706 : un tour autorisé ne suffit pas à déclencher une rotation de latence."""
from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace

from ameesh.config import Config
from ameesh.runner import AgentWorker


class RotationLatencyTest(unittest.TestCase):
    def worker(self, *, latency=900.0, cap=1800.0, agent_cap=None):
        # rotation_due ne lit que l'état en mémoire, pas le registre.
        worker = AgentWorker.__new__(AgentWorker)
        worker.cfg = dataclasses.replace(Config(), session_max_turn_seconds=latency,
                                         turn_max_seconds=cap)
        worker.agent = {} if agent_cap is None else {"turn_max_seconds": agent_cap}
        worker.runner = SimpleNamespace(session_min_turns=3, session_max_tokens=150_000,
                                        session_max_turn_seconds=latency)
        worker.proc = None
        worker.session_turns = 3
        worker.session_tokens = 0
        worker.last_turn_seconds = 0
        return worker

    def test_default_latency_exceeds_default_turn_cap(self):
        cfg = Config()
        self.assertGreater(cfg.session_max_turn_seconds, cfg.turn_max_seconds)

    def test_old_latency_setting_does_not_rotate_at_turn_cap(self):
        worker = self.worker()
        for seconds in (900, 1799, 1800, 1801, 3599):
            with self.subTest(seconds=seconds):
                worker.last_turn_seconds = seconds
                self.assertFalse(worker.rotation_due())
        worker.last_turn_seconds = 3600
        self.assertTrue(worker.rotation_due())

    def test_agent_turn_cap_sets_the_floor(self):
        worker = self.worker(agent_cap=7200)
        for seconds in (7200, 7201, 14399):
            with self.subTest(seconds=seconds):
                worker.last_turn_seconds = seconds
                self.assertFalse(worker.rotation_due())
        worker.last_turn_seconds = 14400
        self.assertTrue(worker.rotation_due())

    def test_higher_explicit_latency_and_unlimited_turns(self):
        for worker, threshold in ((self.worker(latency=5000), 5000),
                                  (self.worker(cap=0), 900),
                                  (self.worker(agent_cap=0), 900)):
            worker.last_turn_seconds = threshold - 1
            self.assertFalse(worker.rotation_due())
            worker.last_turn_seconds = threshold
            self.assertTrue(worker.rotation_due())

    def test_size_policy_minimum_and_running_turn_still_apply(self):
        worker = self.worker()
        worker.session_tokens = 150_000
        self.assertTrue(worker.rotation_due())
        worker.session_turns = 2
        self.assertFalse(worker.rotation_due())
        worker.session_turns = 3
        worker.proc = object()
        self.assertFalse(worker.rotation_due())
        worker.proc = None
        worker.agent["session_policy"] = "jamais"
        self.assertFalse(worker.rotation_due())
