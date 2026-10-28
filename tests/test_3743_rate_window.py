# SPDX-License-Identifier: AGPL-3.0-only
"""Regression witnesses for durable rolling rate-limit episodes (#3743)."""
import unittest
import threading
from unittest.mock import patch
from types import SimpleNamespace
from ameesh import rate_limit as rate
from ameesh.runner import AgentWorker


class WindowTest(unittest.TestCase):
    at = 1_793_002_000.0

    def snapshot(self, closed, ongoing=0, paused=False):
        return rate.window_state(rate.window_text(
            'turn', rate.window_total(closed, self.at, ongoing),
            len(closed) + bool(ongoing), self.at - 4000, self.at,
            closed=closed, ongoing=ongoing, paused=paused))

    def test_successful_turn_does_not_grow_850_seconds_past_threshold(self):
        closed = [(self.at - 1000, self.at - 250)]
        worker = SimpleNamespace(
            rate_limit=rate.RATE_LIMITED, rate_limit_since=self.at - 100,
            rate_limit_streak=1, rate_limit_retry_at=self.at,
            failure_streak=0, preemption_streak=0,
            rate_limit_window=closed, rate_limit_window_since=self.at - 1000)
        worker._fenetre_debit = lambda text, **kw: AgentWorker._fenetre_debit(worker, text, **kw)
        with patch('ameesh.runner.time.time', return_value=self.at):
            text = AgentWorker._marqueurs_durables(worker, 'mail turn (codex)')
        self.assertIsNotNone(rate.state(text))  # #1270 retry survives
        snapshot = rate.window_state(text)
        self.assertEqual(rate.window_seen(snapshot, rate.state(text), self.at + 300), 850)
        self.assertEqual(rate.window_seen(snapshot, rate.state(text), self.at + 4000), 0)

    def test_real_episode_edges_expire_after_restart(self):
        closed = [(self.at - 4000, self.at - 3401),
                  (self.at - 2000, self.at - 1243)]
        snapshot = self.snapshot(closed)
        self.assertEqual(snapshot['total'], 956)
        later = self.at + 1020
        self.assertEqual(rate.window_seen(snapshot, None, later), 757)
        restored, _ = rate.window_restore(snapshot, None, later)
        self.assertEqual(rate.window_total(restored, later), 757)

    def test_waiting_episode_keeps_growing_without_double_count_on_restore(self):
        closed = [(self.at - 2000, self.at - 1600)]
        ongoing = self.at - 300
        snapshot = self.snapshot(closed, ongoing)
        debit = {'kind': rate.RATE_LIMITED, 'since': ongoing}
        self.assertEqual(rate.window_seen(snapshot, debit, self.at + 250), 950)
        restored, _ = rate.window_restore(snapshot, debit, self.at + 250)
        self.assertEqual(rate.window_total(restored, self.at + 250, ongoing), 950)

    def test_interrupt_status_preserves_measured_episode_without_retry_marker(self):
        snapshot = self.snapshot([(self.at - 2000, self.at - 1600)], self.at - 300)
        restored, _ = rate.window_restore(snapshot, None, self.at + 100)
        self.assertEqual(rate.window_total(restored, self.at + 100), 700)

    def test_malformed_details_do_not_raise(self):
        text = rate.window_text('idle', 950, 1, self.at - 1000, self.at)
        for payload in ('{}', '{"closed":null}', '{"closed":[[1,2,3]]}'):
            self.assertIsNone(rate.window_state(text + ' [rate_window_v2 ' + payload + ']'))

    def test_host_withdrawal_writes_a_restorable_window(self):
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker)
        worker.runner = SimpleNamespace(host_gate=None, runner_id='runner')
        worker.name, worker.db, worker.epoch = 'agent', object(), 1
        worker.lease_lost = threading.Event()
        worker._session_en_attente = None
        worker.rate_limit = rate.RATE_LIMITED
        worker.rate_limit_since = self.at - 300
        worker.rate_limit_window = [(self.at - 2000, self.at - 1600)]
        worker.rate_limit_window_since = self.at - 2000
        with patch('ameesh.runner.time.time', return_value=self.at), \
                patch('ameesh.runner.registry.restore_prompt'), \
                patch('ameesh.runner.registry.get', return_value={'status': 'idle'}), \
                patch('ameesh.runner.registry.set_status') as write:
            worker._leave_for_host()
        text = write.call_args.kwargs['status_text']
        snapshot = rate.window_state(text)
        self.assertEqual(rate.window_seen(snapshot, None, self.at + 100), 700)
        restored, _ = rate.window_restore(snapshot, None, self.at + 100)
        self.assertEqual(rate.window_total(restored, self.at + 100), 700)
